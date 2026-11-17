"""Pending events (#4303): deck notes a chat gets with its next message, without being woken."""

import asyncio
from datetime import UTC, datetime, timedelta

from agentdeck.action_context import current_client_action_id
from agentdeck.config import InjectConfig
from agentdeck.db import Db
from agentdeck.inject import InjectionService
from agentdeck.models import Account, Capability, InjectResult, Session, SessionStatus
from agentdeck.pending_events import NOTES_END, NOTES_HEADING, PendingEvents, with_notes

CHAT = "chat-a"


class Provider:
    """Takes every send, or answers each with the next of ``answers`` (an exception raises)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent: list[str] = []
        self.steered: list[str] = []
        self.started: list[str] = []
        #: The delivery id each steer ran under, which the runtime's fence keys on.
        self.steer_ids: list[str | None] = []

    def _answer(self):
        answer = self.answers.pop(0) if self.answers else InjectResult(True)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def inject(self, account, session, message, *, timeout_s, **_kwargs):
        self.sent.append(message)
        return self._answer()

    async def transcript_cursor(self, account, session):
        return 0, 0

    async def steer(self, account, session, message, **_kwargs):
        self.steered.append(message)
        self.steer_ids.append(current_client_action_id())
        return self._answer()

    def can_start_session(self, account):
        return True

    async def start_session(self, account, cwd, message, *, timeout_s, **_kwargs):
        self.started.append(message)
        return self._answer()


def deck(tmp_path, chats=None):
    """The control database, its notes, and a delivery service over them, as the app wires
    them; a second call on the same ``tmp_path`` is the deck after a restart."""
    db = Db(tmp_path / "control.db")
    chats = chats or {"codex:test:sid": CHAT}

    def chats_of(session_key):
        return [chats[session_key], session_key] if session_key in chats else [session_key]

    notes = PendingEvents(db, chats_of)
    return notes, InjectionService(InjectConfig(enabled=True), pending_events=notes)


def session(tmp_path, sid="sid"):
    account = Account("codex:test", "codex", "test", tmp_path)
    return account, Session(
        f"codex:test:{sid}",
        account.key,
        sid,
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )


async def send(service, provider, text, *, sid="sid", tmp_path, action=None):
    account, chat = session(tmp_path, sid)
    result = await service.start(account, chat, provider, text, client_action_id=action)
    for _ in range(50):
        await asyncio.sleep(0)
        if not service.can_queue(chat.key):
            break
    return result


async def test_a_persons_message_carries_two_notes_first_in_order_and_only_once(tmp_path):
    notes, service = deck(tmp_path)
    assert notes.add(CHAT, "n1", "Review passed at abc.")
    assert notes.add(CHAT, "n2", "Deploy landed.")
    assert notes.add(CHAT, "n1", "Review passed at abc."), "the same note twice is one"
    provider = Provider()

    await send(service, provider, "How is it going?", tmp_path=tmp_path)
    await send(service, provider, "And now?", tmp_path=tmp_path)

    first = provider.sent[0]
    assert first == with_notes(["Review passed at abc.", "Deploy landed."], "How is it going?")
    assert first.startswith(NOTES_HEADING) and first.endswith(f"{NOTES_END}\n\nHow is it going?")
    assert first.index("1. Review passed") < first.index("2. Deploy landed")
    assert provider.sent[1] == "And now?", "a note is sent once"
    assert notes.waiting(CHAT) == []
    # The optimistic row holds what the transcript will show, so the two match up.
    assert service.status("codex:test:sid").items[0].text == first


async def test_a_deck_message_carries_the_note_and_a_steer_does_too(tmp_path):
    notes, service = deck(tmp_path)
    notes.add(CHAT, "n1", "Review passed.")
    provider = Provider()
    await send(service, provider, "Merged; check it live.", tmp_path=tmp_path, action="wr:1")
    assert provider.sent == [with_notes(["Review passed."], "Merged; check it live.")]

    notes.add(CHAT, "n2", "Base merged in.")
    account, chat = session(tmp_path)
    receipt = await service.deliver_now(account, chat, provider, "stop that", client_action_id="s1")
    assert receipt.result.accepted
    assert provider.steered == [with_notes(["Base merged in."], "stop that")]
    assert provider.steer_ids == ["s1"], "the steer runs under its delivery id"
    assert notes.waiting(CHAT) == []


async def test_an_uncertain_steer_retried_under_its_id_carries_the_same_notes(tmp_path):
    """A steer cut after its handoff began may have landed: its notes stay taken, and the
    retry under the same id carries them again, so the runtime fence replays it."""
    notes, service = deck(tmp_path)
    notes.add(CHAT, "n1", "Review passed.")
    account, chat = session(tmp_path)
    cut = Provider(RuntimeError("connection lost"))
    try:
        await service.deliver_now(account, chat, cut, "stop that", client_action_id="s1")
    except RuntimeError:
        pass
    assert notes.waiting(CHAT) == []
    notes.add(CHAT, "n2", "Deploy landed.")

    retry = Provider()
    await service.deliver_now(account, chat, retry, "stop that", client_action_id="s1")
    assert retry.steered == cut.steered == [with_notes(["Review passed."], "stop that")]
    assert retry.steer_ids == cut.steer_ids == ["s1"]
    assert [note["id"] for note in notes.waiting(CHAT)] == ["n2"]


async def test_a_continued_leg_of_the_chat_carries_its_notes(tmp_path):
    """Continuing a chat on a new session (Continue, or a usage move) is the same chat."""
    notes, service = deck(tmp_path)
    notes.add(CHAT, "n1", "Review passed.")
    account, _ = session(tmp_path)
    refused = Provider(InjectResult(False, "at_capacity"))

    async def start(provider, text, action):
        result = await service.start_new(
            account,
            provider,
            tmp_path,
            text,
            client_action_id=action,
            origin="continue",
            source_session_key="codex:test:sid",
        )
        assert result.accepted
        return await service.new_start_answer(account.key)

    await start(refused, "carry on", "c1")
    assert refused.started == [with_notes(["Review passed."], "carry on")]
    assert [note["id"] for note in notes.waiting(CHAT)] == ["n1"], "a refused start: back"

    leg = Provider(InjectResult(True, session_id="leg-two"))
    await start(leg, "carry on", "c2")
    assert leg.started == [with_notes(["Review passed."], "carry on")]
    assert notes.waiting(CHAT) == []

    # A brand-new chat is not a continuation and takes nothing.
    notes.add(CHAT, "n2", "Deploy landed.")
    fresh = Provider(InjectResult(True, session_id="other"))
    await service.start_new(account, fresh, tmp_path, "new work", origin="new")
    await service.new_start_answer(account.key)
    assert fresh.started == ["new work"]


async def test_a_clean_refusal_puts_the_notes_back_and_a_slash_command_never_carries_them(
    tmp_path,
):
    notes, service = deck(tmp_path)
    notes.add(CHAT, "n1", "Review passed.")
    provider = Provider(InjectResult(False, "provider refused"))
    await send(service, provider, "first", tmp_path=tmp_path)
    assert provider.sent == [with_notes(["Review passed."], "first")]
    assert [note["id"] for note in notes.waiting(CHAT)] == ["n1"], "refused: still waiting"
    # The failed row shows the message as the person wrote it again.
    assert service.status("codex:test:sid").items[0].text == "first"

    # A slash command would stop being one with anything in front of it.
    await send(service, Provider(), "/compact", tmp_path=tmp_path)
    assert [note["id"] for note in notes.waiting(CHAT)] == ["n1"]

    later = Provider()
    await send(service, later, "second", tmp_path=tmp_path)
    assert later.sent == [with_notes(["Review passed."], "second")]


async def test_a_refusal_that_may_have_written_keeps_the_notes_on_every_path(tmp_path):
    """Only a refusal known to have written nothing gives the notes back: a runtime that
    answered ``uncertain``, or a connection lost after the request left, may have delivered
    them, and the next message must not repeat them."""
    assert InjectResult.from_wire({"accepted": False, "action": "uncertain"}).write_uncertain
    lost = InjectResult(
        False, "runtime unavailable", runtime_unreachable=True, write_uncertain=True
    )
    notes, service = deck(tmp_path)
    notes.add(CHAT, "n1", "Review passed.")
    await send(service, Provider(lost), "first", tmp_path=tmp_path)
    assert notes.waiting(CHAT) == []
    later = Provider()
    await send(service, later, "second", tmp_path=tmp_path)
    assert later.sent == ["second"]

    notes.add(CHAT, "n2", "Deploy landed.")
    account, chat = session(tmp_path)
    steer = Provider(lost)
    await service.deliver_now(account, chat, steer, "stop", client_action_id="s1")
    assert steer.steered == [with_notes(["Deploy landed."], "stop")]
    assert notes.waiting(CHAT) == []

    notes.add(CHAT, "n3", "Base merged.")
    start = Provider(InjectResult(False, "spawn failed", session_id="half-started"))
    await service.start_new(
        account, start, tmp_path, "go on", origin="continue", source_session_key=chat.key
    )
    await service.new_start_answer(account.key)
    assert start.started == [with_notes(["Base merged."], "go on")]
    assert notes.waiting(CHAT) == [], "a start that named a session may have written"


async def test_a_restart_between_a_send_and_its_retry_never_sends_a_note_twice(tmp_path):
    """A send whose outcome is unknown keeps its notes. The deck's durable retry after a
    restart carries the same text under the same action id, which the delivery fence
    replays rather than sends again; no other message takes those notes."""
    notes, service = deck(tmp_path)
    notes.add(CHAT, "n1", "Review passed.")
    await send(
        service, Provider(RuntimeError("cut")), "Deck says hi", tmp_path=tmp_path, action="m1"
    )
    assert notes.waiting(CHAT) == [], "an unknown outcome counts as sent"

    notes, service = deck(tmp_path)  # the web process restarted; the database stayed
    notes.add(CHAT, "n2", "Deploy landed.")
    retry = Provider()
    await send(service, retry, "Deck says hi", tmp_path=tmp_path, action="m1")
    assert retry.sent == [with_notes(["Review passed."], "Deck says hi")], "same text again"

    other = Provider()
    await send(service, other, "Next", tmp_path=tmp_path)
    assert other.sent == [with_notes(["Deploy landed."], "Next")]


async def test_a_retry_of_a_send_that_carried_no_notes_stays_bare(tmp_path):
    """The snapshot is frozen at the first try, an empty one too: a note that came since
    would change the retry's text, and the delivery fence would refuse it as a conflict
    instead of replaying the first try's answer."""
    notes, service = deck(tmp_path)
    await send(service, Provider(RuntimeError("cut")), "hi", tmp_path=tmp_path, action="m1")
    notes.add(CHAT, "n1", "Review passed.")

    notes, service = deck(tmp_path)  # after a restart too
    retry = Provider()
    await send(service, retry, "hi", tmp_path=tmp_path, action="m1")
    assert retry.sent == ["hi"]
    assert [note["id"] for note in notes.waiting(CHAT)] == ["n1"]

    account, chat = session(tmp_path)
    steer = Provider()
    await service.deliver_now(account, chat, steer, "stop", client_action_id="s1")
    await service.deliver_now(account, chat, steer, "stop", client_action_id="s1")
    assert steer.steered == [with_notes(["Review passed."], "stop")] * 2
    notes.prune(datetime.now(UTC) + timedelta(days=8))  # old snapshots are forgotten


async def test_a_note_for_a_chat_that_gets_no_message_waits_and_reaches_no_other_chat(tmp_path):
    notes, service = deck(tmp_path, chats={"codex:test:sid": CHAT, "codex:test:sid2": "chat-b"})
    notes.add(CHAT, "n1", "Review passed.")
    provider = Provider()
    await send(service, provider, "hello b", sid="sid2", tmp_path=tmp_path)
    assert provider.sent == ["hello b"]
    assert [note["text"] for note in notes.waiting(CHAT)] == ["Review passed."]
    notes.prune()  # pruning forgets delivered notes only
    assert [note["id"] for note in notes.waiting(CHAT)] == ["n1"]


async def test_notes_that_would_make_the_message_too_long_wait_for_the_next_one(tmp_path):
    db = Db(tmp_path / "control.db")
    notes = PendingEvents(db, lambda key: [CHAT])
    service = InjectionService(
        InjectConfig(enabled=True, max_message_chars=400), pending_events=notes
    )
    notes.add(CHAT, "n1", "short note")
    notes.add(CHAT, "n2", "x" * 300)
    provider = Provider()
    await send(service, provider, "hi", tmp_path=tmp_path)
    assert provider.sent == [with_notes(["short note"], "hi")]
    assert [note["id"] for note in notes.waiting(CHAT)] == ["n2"]
