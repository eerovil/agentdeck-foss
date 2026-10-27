"""A card whose worker waits on a person's prompt rests in Needs More Info (#2887).

The Runtime reads a worker stopped on a question, a permission or approval request, or a
blocked command as ``waiting_human``. The wake parks that card in Needs More Info under the same
lease, with no comment, and puts it back In progress as soon as the wait is gone.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, run_wake, store_for

from agentdeck.poller_engine import chat_answers
from agentdeck.poller_engine.github import GitHubAdapter
from agentdeck.poller_engine.ports import EffectKind
from agentdeck.poller_engine.reconcile import CardSnapshot
from agentdeck.poller_ops import _card_parked_as
from agentdeck.work_overview import parked_for_person

GEN = "gen0001"
KEY = "store#5"
CHAT = "https://deck.example/chats/abc123"


class BoardPort(FakePort):
    """A fake whose live snapshot follows the board writes, as GitHub's would."""

    def apply(self, effect):
        landed = super().apply(effect)
        if landed and effect.kind is EffectKind.SET_STATUS:
            key = f"{effect.repo}#{effect.number}"
            old = self.snapshots.get(key) or CardSnapshot()
            self.snapshots[key] = CardSnapshot(
                status=effect.value, terminal_marker=old.terminal_marker
            )
        return landed


def _running(extra=None):
    rec = {
        "status": "In progress",
        "generation": GEN,
        "started": True,
        "processing": ["c1"],
        "pending": [],
        "run_session": {"session_url": CHAT, "generation": GEN},
    }
    rec.update(extra or {})
    return {"issues": {KEY: rec}}


def _port(waiting=True, status="In progress"):
    port = BoardPort(snapshots={KEY: CardSnapshot(status=status)})
    port.waiting[KEY] = waiting
    return port


def _comments(port):
    return [effect.value.body for effect in port.applied if effect.kind is EffectKind.COMMENT]


def _statuses(port):
    return [effect.value for effect in port.applied if effect.kind is EffectKind.SET_STATUS]


def _rec(store):
    return store.read()["issues"][KEY]


def _parked_store(tmp_path):
    port = _port()
    store = store_for(tmp_path, _running())
    run_wake(store, port, now=_NOW)
    assert _rec(store)["status"] == "Needs More Info"
    port.applied.clear()
    return store, port


def test_a_worker_waiting_on_a_prompt_parks_its_card_without_a_comment(tmp_path):
    port = _port()
    store = store_for(tmp_path, _running())

    run_wake(store, port, now=_NOW)

    assert _statuses(port) == ["Needs More Info"]
    assert _comments(port) == []
    rec = _rec(store)
    assert rec["status"] == "Needs More Info"
    assert rec["generation"] == GEN
    assert rec["prompt_parked"] == GEN
    assert rec["parked_as"] == "prompt"
    # The worker's own commands stay with it: nothing about its turn was resolved.
    assert rec["processing"] == ["c1"]

    # Still waiting on the next wake: nothing more is written.
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert _statuses(port) == ["Needs More Info"]
    assert _rec(store)["prompt_parked"] == GEN


def test_the_card_goes_back_in_progress_once_the_prompt_is_answered(tmp_path):
    store, port = _parked_store(tmp_path)
    port.waiting[KEY] = False

    run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert _statuses(port) == ["In progress"]
    assert _comments(port) == []
    rec = _rec(store)
    assert rec["status"] == "In progress"
    assert rec["generation"] == GEN
    assert "prompt_parked" not in rec and "parked_as" not in rec
    assert rec["processing"] == ["c1"]


def test_a_turn_that_ended_while_parked_is_handled_as_any_ending(tmp_path):
    """The prompt goes with the turn. The card is back In progress before reconciliation, so
    the ending is the ordinary reportless one rather than a card left parked for nothing."""
    store, port = _parked_store(tmp_path)
    port.waiting[KEY] = False
    port.turn_ends[KEY] = {"observed": "ended", "subtype": "success", "generation": GEN}

    run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert _statuses(port)[0] == "In progress"
    rec = _rec(store)
    assert "prompt_parked" not in rec
    assert rec.get("parked_as") != "prompt"
    assert port.waiting_reads.count(KEY) >= 1


def test_an_unreadable_runtime_moves_nothing_either_way(tmp_path):
    port = _port(waiting=None)
    store = store_for(tmp_path, _running())
    run_wake(store, port, now=_NOW)
    assert _statuses(port) == [] and _rec(store)["status"] == "In progress"

    store, port = _parked_store(tmp_path / "parked")
    port.waiting[KEY] = None
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert _statuses(port) == [] and _rec(store)["prompt_parked"] == GEN


def test_a_board_write_that_does_not_land_is_tried_again(tmp_path):
    port = _port()
    port.refuse = ((EffectKind.SET_STATUS, "Needs More Info"),)
    store = store_for(tmp_path, _running())

    run_wake(store, port, now=_NOW)
    assert _rec(store)["status"] == "In progress"
    assert "prompt_parked" not in _rec(store)

    port.refuse = ()
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert _statuses(port) == ["Needs More Info"]
    assert _rec(store)["prompt_parked"] == GEN


def test_a_card_whose_next_step_is_decided_is_not_parked(tmp_path):
    for held in ({"stopping": True}, {"release_pending": True}, {"mode": "merge-review"}):
        port = _port()
        store = store_for(tmp_path / next(iter(held)), _running(held))
        run_wake(store, port, now=_NOW)
        assert "Needs More Info" not in _statuses(port), held
        assert "prompt_parked" not in store.read()["issues"].get(KEY, {}), held


def test_a_worker_s_own_needs_more_info_is_never_moved_back(tmp_path):
    """Only the engine's own prompt park is undone; a question the worker finished with stays."""
    port = _port(waiting=False, status="Needs More Info")
    store = store_for(tmp_path, _running({"status": "Needs More Info", "processing": []}))

    run_wake(store, port, now=_NOW)

    assert _statuses(port) == []
    assert _rec(store)["status"] == "Needs More Info"


def _finish(store, port, status, body="Report"):
    from agentdeck.poller_engine.worker_control import WorkerControl

    control = WorkerControl(store, port, clock=lambda: _NOW)
    return control.finish("store", 5, GEN, status, body)


def test_a_finish_while_parked_is_an_ordinary_report(tmp_path):
    """The prompt was answered and the worker reported before the wake put the card back."""
    store, port = _parked_store(tmp_path)

    result = _finish(store, port, "in_review")

    assert result.ok and result.reason == "finished -> In review", result.reason
    assert _statuses(port) == ["In review"]
    rec = _rec(store)
    assert "prompt_parked" not in rec and rec.get("parked_as") != "prompt"

    # The next wake reads the worker as ended from its report, and never undoes it.
    port.waiting[KEY] = False
    port.snapshots[KEY] = CardSnapshot(status="In review", terminal_marker="In review")
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert _statuses(port) == ["In review"]
    rec = _rec(store)
    assert rec["status"] == "In review"
    assert rec["processing"] == []


def test_a_needs_more_info_finish_while_parked_stays_parked(tmp_path):
    store, port = _parked_store(tmp_path)

    result = _finish(store, port, "needs_more_info", "A real question")

    assert result.ok and result.reason == "finished -> Needs More Info", result.reason
    port.waiting[KEY] = False
    port.snapshots[KEY] = CardSnapshot(status="Needs More Info", terminal_marker="Needs More Info")
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert _statuses(port) == ["Needs More Info"]
    assert _rec(store)["status"] == "Needs More Info"


def test_a_message_typed_into_the_parked_chat_is_posted_and_the_park_ends_with_the_prompt(
    tmp_path,
):
    """Typing into the chat instead of answering the prompt is a chat answer (#2348). The worker
    still carries its commands, so the answer does not resume the card; the prompt going away
    does."""
    store, port = _parked_store(tmp_path)
    chat_answers.write(
        store.paths.state,
        request_id="answer-0001",
        key=KEY,
        generation=GEN,
        text="Use the other option",
        session_url=CHAT,
        now=_NOW,
    )
    port.waiting[KEY] = False

    run_wake(store, port, now=_NOW + timedelta(seconds=30))

    [answer] = _comments(port)
    assert "> Use the other option" in answer
    assert _statuses(port) == ["In progress"]
    rec = _rec(store)
    assert rec["status"] == "In progress"
    assert "prompt_parked" not in rec and "parked_as" not in rec


def test_a_new_run_forgets_the_old_run_s_park(tmp_path):
    store, port = _parked_store(tmp_path)
    with store.transaction() as state:
        state["issues"][KEY]["generation"] = "gen0002"

    run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert "prompt_parked" not in _rec(store)
    assert "In progress" not in _statuses(port)


def test_the_adapter_reads_the_runtime_s_waiting_human():
    adapter = GitHubAdapter.__new__(GitHubAdapter)
    for answer, expected in (
        ({"execution_state": "waiting_human"}, True),
        ({"execution_state": "running"}, False),
        ({"execution_state": "ended"}, False),
        ({"execution_state": ""}, None),
        ({}, None),
        (None, None),
    ):
        adapter._execution_outcome = lambda repo, number, generation, answer=answer: answer
        assert adapter.waiting_on_person("store", 5, GEN) is expected, answer


def test_the_deck_does_not_read_a_prompt_park_as_a_question_about_the_work():
    """The chat's own wait says what to do; the column is not a second question (#2887)."""
    assert _card_parked_as({"parked_as": "prompt"}, "needs_more_info") == "prompt"
    assert _card_parked_as({"parked_as": "prompt"}, "in_progress") == ""
    prompt = SimpleNamespace(status_role="needs_more_info", parked_as="prompt", blocked_by=())
    asked = SimpleNamespace(status_role="needs_more_info", parked_as="", blocked_by=())
    assert parked_for_person(prompt) == ""
    assert parked_for_person(asked) == "needs_more_info"
