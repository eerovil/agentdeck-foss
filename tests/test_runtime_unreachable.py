"""Which refused sends may wait for a restarting runtime (#2847).

Only a chat the deck already knows is its own, whose runtime cannot be reached right
now, is held; every other refusal stays a refusal.
"""

from types import SimpleNamespace

import httpx

from agentdeck.models import (
    InteractionOption,
    InteractionQuestion,
    PendingInteraction,
    open_questions_id,
)
from agentdeck.providers.base import SessionProvider
from agentdeck.providers.claude_code.provider import ClaudeCodeProvider
from agentdeck.providers.claude_code.worker_client import ClaudeWorkerClient
from agentdeck.providers.codex.provider import CodexProvider
from agentdeck.providers.codex.runtime_client import CodexRuntimeClient

ACCOUNT = SimpleNamespace(key="test:alt")
SESSION = SimpleNamespace(session_id="sid")


class _Owner:
    def __init__(self, owned, available=True):
        self._owned = owned
        self.available = available

    def owns(self, session_id):
        return session_id in self._owned


def test_claude_holds_only_for_its_own_chat_while_the_runtime_is_down():
    provider = ClaudeCodeProvider()
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False  # never reached it

    provider._workers[ACCOUNT.key] = _Owner({"sid"}, available=False)
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is True

    provider._workers[ACCOUNT.key] = _Owner({"sid"}, available=True)
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False

    provider._workers[ACCOUNT.key] = _Owner({"other"}, available=False)
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False


def test_codex_holds_only_for_its_own_thread_while_refresh_fails():
    provider = CodexProvider()
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False

    provider._clients[ACCOUNT.key] = _Owner({"sid"})
    provider._refresh_ok[ACCOUNT.key] = False
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is True

    provider._refresh_ok[ACCOUNT.key] = True
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False

    provider._clients[ACCOUNT.key] = _Owner({"other"})
    provider._refresh_ok[ACCOUNT.key] = False
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False


def test_a_provider_without_a_runtime_never_holds():
    assert SessionProvider.runtime_unreachable(object(), ACCOUNT, SESSION) is False


def _account():
    return SimpleNamespace(key="test:alt", label="alt", socket_path=None, provider_id="x")


def _claude(entry):
    """A Claude provider whose runtime went away, its client keeping the last good read."""
    changes = []
    provider = ClaudeCodeProvider()
    client = ClaudeWorkerClient(_account(), on_change=lambda: changes.append("sid"))
    client._owned = {"sid": entry}
    client._snapshot = b"last read"
    provider._workers[ACCOUNT.key] = client
    return provider, client, changes


def _open_questions():
    questions = [
        {"id": "q1", "header": "One", "prompt": "First?"},
        {"id": "q2", "header": "Two", "prompt": "Second?"},
    ]
    return {
        "id": open_questions_id(["q1", "q2"]),
        "kind": "question",
        "thread_id": "sid",
        "title": "Claude is asking",
        "dismissible": True,
        "questions": questions,
    }


def test_a_claude_box_outlives_the_runtime_going_away():
    entry = {"open_questions": _open_questions()}
    provider, _client, _changes = _claude(entry)
    session = SimpleNamespace(session_id="sid", capabilities=frozenset())
    box = provider.pending_interaction(ACCOUNT, session)
    assert box.id == open_questions_id(["q1", "q2"])
    # Delivery reads the runtime's own view, which has nothing to say yet.
    assert provider.live_interaction(ACCOUNT, session) is None


def test_forgetting_one_of_two_open_questions_leaves_the_other_under_its_own_id():
    provider, client, changes = _claude({"open_questions": _open_questions()})
    session = SimpleNamespace(session_id="sid", capabilities=frozenset())

    provider.forget_held_interaction(ACCOUNT, session, {"q1"})

    box = provider.pending_interaction(ACCOUNT, session)
    assert [question.id for question in box.questions] == ["q2"]
    assert box.id == open_questions_id(["q2"])
    assert client._snapshot is None  # the runtime's next read lands, even if it is identical
    assert changes == ["sid"]

    provider.forget_held_interaction(ACCOUNT, session, {"q2"})
    assert provider.pending_interaction(ACCOUNT, session) is None


def test_forgetting_a_permission_prompt_leaves_the_open_questions_behind_it():
    entry = {
        "open_questions": _open_questions(),
        "pending_interaction": {"id": "p1", "kind": "command_approval", "title": "Run it?"},
    }
    provider, _client, _changes = _claude(entry)
    session = SimpleNamespace(session_id="sid", capabilities=frozenset())
    assert provider.pending_interaction(ACCOUNT, session).id == "p1"

    provider.forget_held_interaction(ACCOUNT, session, set())

    assert provider.pending_interaction(ACCOUNT, session).id == open_questions_id(["q1", "q2"])


def _codex():
    provider = CodexProvider()
    changes = []
    client = CodexRuntimeClient(_account(), on_change=changes.append)
    client._state = {"sid": {"interaction": {"id": "tok-1", "kind": "question", "title": "Pick"}}}
    client._snapshot = b"last read"
    provider._clients[ACCOUNT.key] = client
    provider._refresh_ok[ACCOUNT.key] = False
    return provider, client, changes


def test_a_codex_box_outlives_a_failed_refresh_until_its_answer_is_held():
    provider, client, changes = _codex()
    session = SimpleNamespace(session_id="sid", capabilities=frozenset())
    assert provider.pending_interaction(ACCOUNT, session).id == "tok-1"
    assert provider.live_interaction(ACCOUNT, session) is None

    provider.forget_held_interaction(ACCOUNT, session, ())

    assert provider.pending_interaction(ACCOUNT, session) is None
    assert client._snapshot is None
    assert changes == ["sid"]


def test_a_prompt_replayed_under_a_new_id_keeps_its_fingerprint():
    def asked(interaction_id, prompt="Which one?", options=(), **details):
        return PendingInteraction(
            id=interaction_id,
            kind="question",
            thread_id="sid",
            turn_id=None,
            title="Pick",
            questions=(
                InteractionQuestion(id="q", header="", prompt=prompt, options=options),
            ),
            **details,
        )

    assert asked("tok-1").fingerprint() == asked("tok-2").fingerprint()
    assert (
        asked("tok-1", cwd="/repo").fingerprint() == asked("tok-2", cwd="/repo").fingerprint()
    )
    # The same words asking for something else: a held "allow" must not approve it.
    for changed in (
        asked("tok-1", "Which other?"),
        asked("tok-1", cwd="/repo/other"),
        asked("tok-1", url="https://example.test/other"),
        asked("tok-1", decisions=("accept",)),
        asked("tok-1", options=(InteractionOption("Yes", value="all"),)),
    ):
        assert asked("tok-1").fingerprint() != changed.fingerprint()


async def test_only_a_lost_connection_marks_a_failed_write_as_unreachable():
    def transport(error):
        def handle(_request):
            raise error

        return httpx.MockTransport(handle)

    account = _account()
    for error, unreachable in (
        (httpx.ConnectError("gone"), True),
        (httpx.ReadTimeout("slow"), False),
    ):
        client = CodexRuntimeClient(account, transport=transport(error))
        result = await client.answer("sid", "tok-1", answers={})
        assert (result.accepted, result.runtime_unreachable) == (False, unreachable)
        await client.stop()
