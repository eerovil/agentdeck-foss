"""The worker-facing control seam is lease-bound and uses the engine's one write port."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from poller_engine_fakes import FakePort, store_for

from agentdeck.poller_engine import chat_answers
from agentdeck.poller_engine.config import Role
from agentdeck.poller_engine.github import (
    Gh,
    GitHubAdapter,
)
from agentdeck.poller_engine.ports import CommentBody, EffectKind, SnapshotBatch
from agentdeck.poller_engine.reconcile import CardSnapshot
from agentdeck.poller_engine.rounds import count_round
from agentdeck.poller_engine.worker_control import WorkerControl

_KEY = "store#5"
_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _store(tmp_path, **extra):
    rec = {"generation": "gen-1", "status": "In progress", **extra}
    return store_for(tmp_path, {"issues": {_KEY: rec}})


def _port(*, snapshot=None, **kwargs):
    return FakePort(
        snapshots={_KEY: snapshot or CardSnapshot(status="In progress")},
        **kwargs,
    )


def test_enabled_requires_the_exact_current_generation_and_a_live_owned_card(tmp_path):
    store = _store(tmp_path)

    assert WorkerControl(store, _port()).enabled("store", 5, "gen-1").ok
    assert not WorkerControl(store, _port()).enabled("store", 5, "old").ok
    assert not WorkerControl(
        _store(tmp_path / "claimed", label_claim=True),
        _port(snapshot=CardSnapshot(status="In progress", owned=False)),
    ).enabled("store", 5, "gen-1").ok
    # A card that never wore the label — started by a `/claude` comment — is not refused for
    # the absence of one the engine no longer writes (#2053).
    assert WorkerControl(
        store, _port(snapshot=CardSnapshot(status="In progress", owned=False))
    ).enabled("store", 5, "gen-1").ok
    assert not WorkerControl(store, FakePort()).enabled("store", 5, "gen-1").ok


def test_enabled_explains_when_reportless_recovery_already_handed_the_work_off(tmp_path):
    port = _port()
    store = _store(
        tmp_path,
        generation="gen-2",
        reportless_resolved="gen-1",
    )

    result = WorkerControl(store, port).enabled("store", 5, "gen-1")

    assert not result.ok
    assert result.reason.startswith(
        "disabled: this generation's work was handed to a new worker after a turn ended "
        "without a report."
    )
    # The revive usually lands in this very chat, so the refusal says where to finish (#2529).
    assert "that worker is you" in result.reason
    assert "Otherwise stop and leave the work where it is" in result.reason
    assert port.snapshot_batches == []


def test_enabled_tells_a_stopped_generation_to_stop_with_nothing_to_acknowledge(tmp_path):
    store = _store(
        tmp_path,
        generation="gen-2",
        stopping=True,
        stop_waiting_generation="gen-1",
    )
    control = WorkerControl(store, _port())

    expected = control.enabled("store", 5, "gen-1")
    unrelated = control.enabled("store", 5, "old")

    assert not expected.ok
    assert expected.reason == "disabled: stop requested for this generation; stop now"
    assert not unrelated.ok
    assert unrelated.reason == (
        "disabled: a stop is in progress for a different generation; stop now"
    )
    # #2159 test 3: no refusal asks the model to run anything to prove it stopped.
    assert "stop-ack" not in expected.reason + unrelated.reason


def test_every_way_a_lease_cannot_be_proved_gets_its_own_words(tmp_path):
    """One string for six unrelated situations made a worker read a deferred card read as a
    dead lease and throw away a finished turn (#1651). Each cause says what it is, and the
    two that are only a failed look at GitHub say so and ask to be retried."""
    # 1. No record of the card at all — including a worker addressing another engine slot.
    empty = store_for(tmp_path / "empty", {"issues": {}})
    assert WorkerControl(empty, _port()).enabled("store", 5, "gen-1").reason == (
        "disabled: this poller instance has no record of that card; check you are "
        "addressing the instance that dispatched you"
    )

    # 3. A genuinely superseded generation: stale, and the only one that word belongs to.
    assert WorkerControl(_store(tmp_path / "old"), _port()).enabled(
        "store", 5, "gen-0"
    ).reason == "stale: this generation has been superseded; stop now"

    # 4. The shared GraphQL reserve deferred the whole batch before spending a point.
    deferred = WorkerControl(_store(tmp_path / "deferred"), _port(graphql_deferred=True))
    assert deferred.enabled("store", 5, "gen-1").reason == (
        "unavailable: the card read was deferred by the shared GitHub query reserve — "
        "the lease is not stale; wait and retry"
    )

    # 5. The read was attempted and produced nothing for this card.
    assert WorkerControl(_store(tmp_path / "unread"), FakePort()).enabled(
        "store", 5, "gen-1"
    ).reason == (
        "unavailable: the card could not be read right now — the lease is not stale; "
        "wait and retry"
    )

    # 6a. The ownership label removed stays the explicit safe-stop boundary it documents —
    # for a card that was seen wearing one (#2053).
    unowned = _port(snapshot=CardSnapshot(status="In progress", owned=False))
    assert WorkerControl(
        _store(tmp_path / "unowned", label_claim=True), unowned
    ).enabled(
        "store", 5, "gen-1"
    ).reason == (
        "disabled: the card's ownership label was removed; stop and leave the work "
        "where it is"
    )

    # 6b. Closed and no longer active is a different, equally permanent fact.
    closed = _port(snapshot=CardSnapshot(open=False, status="In review"))
    assert WorkerControl(
        _store(tmp_path / "closed", status="In review"), closed
    ).enabled("store", 5, "gen-1").reason == (
        "disabled: the card is closed and no longer active"
    )


def test_a_deferred_card_read_refuses_every_write_and_asks_for_a_retry(tmp_path):
    """The refusal is unchanged and fail-closed: nothing may land on a lease nobody could
    confirm. What changes is that the worker is told to come back rather than to give up."""
    retryable = "wait and retry"
    for operation in (
        lambda control: control.finish("store", 5, "gen-1", "resolved", "Done"),
        lambda control: control.review("store", 5, "gen-1", "pass", "Looks good"),
    ):
        port = _port(graphql_deferred=True)
        control = WorkerControl(_store(tmp_path / str(id(operation)), mode="merge-review"), port)

        result = operation(control)

        assert not result.ok
        assert result.reason.startswith("unavailable:")
        assert retryable in result.reason
        assert port.applied == []


class _DefersAfterTheFirstRead:
    """A port whose second live read is deferred by the shared reserve, like a real one."""

    def __init__(self, port):
        self._port = port
        self._reads = 0

    def __getattr__(self, name):
        return getattr(self._port, name)

    def snapshot_cards(self, cards):
        self._reads += 1
        if self._reads == 1:
            return self._port.snapshot_cards(cards)
        return SnapshotBatch(deferred=True)


def test_a_deferred_reread_after_the_terminal_marker_asks_for_the_idempotent_retry(tmp_path):
    """The marker lands, then the re-read before the board write is deferred. Refusing the
    status write is right, but the card is now half-reported, so the worker is told the repeat
    path exists rather than left thinking its lease died."""
    port = _DefersAfterTheFirstRead(_port())
    control = WorkerControl(_store(tmp_path), port)

    result = control.finish("store", 5, "gen-1", "in_review", "Done")

    assert not result.ok
    assert "unavailable:" in result.reason
    assert "the repeat is idempotent" in result.reason
    assert [effect.kind for effect in port.applied] == [EffectKind.COMMENT]


def test_closed_card_keeps_its_active_generation_enabled_until_the_worker_reports(tmp_path):
    closed = _port(snapshot=CardSnapshot(open=False, status="In progress"))
    assert WorkerControl(_store(tmp_path), closed).enabled("store", 5, "gen-1").ok

    idle = _port(snapshot=CardSnapshot(open=False, status="In review"))
    assert not WorkerControl(
        _store(tmp_path / "idle", status="In review"), idle
    ).enabled("store", 5, "gen-1").ok


def test_a_parked_card_closed_by_hand_can_still_be_finished_by_its_worker(tmp_path):
    """The person answered in the chat and closed the issue; the worker's report must land
    rather than be refused as no longer active, which left the card stuck (#2822)."""
    port = _port(snapshot=CardSnapshot(open=False, status="Needs More Info"))
    control = WorkerControl(_store(tmp_path, status="Needs More Info"), port)

    result = control.finish("store", 5, "gen-1", "resolved", "Nothing was missing")

    assert result.ok, result.reason
    # Already closed: the marker and the board move land, and no second close is tried.
    assert [effect.kind for effect in port.applied] == [
        EffectKind.COMMENT,
        EffectKind.SET_STATUS,
    ]
    # Another lease's parked card is still refused.
    assert not control.enabled("store", 5, "gen-0").ok


def test_closed_merge_review_can_still_report_its_verdict(tmp_path):
    port = _port(snapshot=CardSnapshot(open=False, status="In progress"))
    control = WorkerControl(_store(tmp_path, mode="merge-review"), port)

    result = control.review("store", 5, "gen-1", "changes-needed", "Found a blocker")

    assert result.ok
    assert [effect.kind for effect in port.applied] == [EffectKind.COMMENT]


def test_a_deferred_live_query_budget_disables_the_worker_rather_than_guessing(tmp_path):
    """The lease check reads the live card through the same bounded batch a wake uses, so it
    inherits the shared GraphQL reserve. A deferred batch returns no cards, and a card nobody
    could read must not be treated as still owned."""
    port = _port(graphql_deferred=True)
    control = WorkerControl(_store(tmp_path), port)

    assert not control.enabled("store", 5, "gen-1").ok
    assert not control.finish("store", 5, "gen-1", "in_review", "Done").ok
    assert port.applied == []


def test_the_lease_check_carries_both_generations_into_the_batched_read(tmp_path):
    """A single card still goes through `snapshot_cards`, carrying the lease generation and the
    generation a stop is awaiting — the same query a wake issues, so markers resolve identically."""
    port = _port()
    store = _store(tmp_path, stopping=False, stop_waiting_generation="gen-0")

    assert WorkerControl(store, port).enabled("store", 5, "gen-1").ok
    assert port.snapshot_batches == [[_KEY]]
    assert port.snapshot_leases == [(_KEY, "gen-1", "gen-0", "")]


def test_a_new_generation_drops_the_old_session_but_keeps_the_cards_pull_request(tmp_path):
    rec = {
        "generation": "new",
        "comment_links": {
            "session_generation": "old",
            "session_url": "https://deck.example/sessions/old",
            "pull_request_url": "https://github.com/example/store/pull/12",
        },
    }

    comment = CommentBody.from_record("Restarting", rec)

    assert comment.session_url == ""
    assert comment.pull_request_url == "https://github.com/example/store/pull/12"


def test_finish_posts_each_terminal_marker_before_its_board_transition(tmp_path):
    for token, canonical in (
        ("in_review", "In review"),
        ("needs_more_info", "Needs More Info"),
        ("blocked", "Blocked"),
    ):
        port = _port()
        result = WorkerControl(_store(tmp_path / token), port).finish(
            "store", 5, "gen-1", token, "Worker summary"
        )

        assert result.ok
        assert [effect.kind for effect in port.applied] == [
            EffectKind.COMMENT,
            EffectKind.SET_STATUS,
        ]
        assert f"<!-- claude-terminal generation=gen-1 status={token} -->" in str(
            port.applied[0].value
        )
        assert port.applied[1].value == canonical


def test_a_finish_after_a_reportless_park_is_saved_as_the_cards_status(tmp_path):
    """#2300: the engine parked this lease in Needs More Info while its worker was only waiting
    on its queued suite. The worker's later finish is the newer truth, and the saved status —
    which the Work page reads — must follow it now, not at the next full reconcile."""
    store = _store(
        tmp_path, status="Needs More Info", reportless_resolved="gen-1", processing=[]
    )
    port = _port(snapshot=CardSnapshot(status="Needs More Info"))

    result = WorkerControl(store, port).finish("store", 5, "gen-1", "in_review", "done")

    assert result.ok, result.reason
    assert (EffectKind.SET_STATUS, "In review") in [
        (effect.kind, effect.value) for effect in port.applied
    ]
    assert store.read()["issues"][_KEY]["status"] == "In review"


def test_a_repeat_finish_after_a_reportless_park_saves_the_status_too(tmp_path):
    """An earlier attempt landed the marker and the board write and lost its reply."""
    store = _store(
        tmp_path, status="Needs More Info", reportless_resolved="gen-1", processing=[]
    )
    port = _port(snapshot=CardSnapshot(status="In review", terminal_marker="In review"))

    result = WorkerControl(store, port).finish("store", 5, "gen-1", "in_review", "done")

    assert result.ok and result.reason == "already finished -> In review"
    assert store.read()["issues"][_KEY]["status"] == "In review"


def test_an_ordinary_finish_leaves_the_status_for_reconciliation_to_adopt(tmp_path):
    """A record still In progress is how reconciliation tells the worker ended and does that
    bookkeeping (``WORKER_ENDED``), so an ordinary finish must not pre-empt it."""
    store = _store(tmp_path, processing=["9"])

    assert WorkerControl(store, _port()).finish("store", 5, "gen-1", "in_review", "done").ok

    assert store.read()["issues"][_KEY]["status"] == "In progress"


def test_finish_resolved_closes_the_issue_nothing_else_would_ever_close(tmp_path):
    """``resolved`` says there is nothing to merge, so no pull request will close it (#1581)."""
    port = _port()

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "resolved", "Answered in comments"
    )

    assert result.ok
    assert [effect.kind for effect in port.applied] == [
        EffectKind.COMMENT,
        EffectKind.SET_STATUS,
        EffectKind.CLOSE_ISSUE,
    ]
    assert port.applied[1].value == Role.DONE.value


def test_spec_only_cards_refuse_an_implementation_result(tmp_path):
    snapshot = CardSnapshot(status="In progress", spec_only=True)
    refused = WorkerControl(
        _store(tmp_path / "review"), _port(snapshot=snapshot), spec_only_label="spec-only"
    ).finish("store", 5, "gen-1", "in_review", "Done")

    allowed = WorkerControl(
        _store(tmp_path / "needs-info"), _port(snapshot=snapshot), spec_only_label="spec-only"
    ).finish("store", 5, "gen-1", "needs_more_info", "Spec ready for approval")

    assert not refused.ok
    assert "`spec-only` label" in refused.reason
    assert allowed.ok


def test_a_spec_only_card_closes_when_a_person_asked_for_it(tmp_path):
    """#2661: ``resolved`` builds nothing, so the label does not stand in its way."""
    store = _store(tmp_path)
    port = _port(snapshot=CardSnapshot(status="In progress", spec_only=True))
    control = WorkerControl(store, port, spec_only_label="spec-only")

    result = control.finish("store", 5, "gen-1", "resolved", "Closed as the owner asked")

    assert result.ok, result.reason
    assert [effect.kind for effect in port.applied] == [
        EffectKind.COMMENT,
        EffectKind.SET_STATUS,
        EffectKind.CLOSE_ISSUE,
    ]
    assert port.applied[1].value == Role.DONE.value
    assert store.read()["issues"][_KEY]["resolved_finish"]["generation"] == "gen-1"


def test_spec_only_cards_refuse_a_merge_review_verdict(tmp_path):
    store = _store(tmp_path, mode="merge-review")
    control = WorkerControl(
        store,
        _port(snapshot=CardSnapshot(status="In progress", spec_only=True)),
        spec_only_label="spec-only",
    )

    result = control.review("store", 5, "gen-1", "pass", "Looks good")

    assert not result.ok
    assert "`spec-only` label" in result.reason


def test_a_refused_close_is_retried_by_the_repeat_finish_and_not_reported_as_done(tmp_path):
    store = _store(tmp_path)
    refusing = _port(refuse={EffectKind.CLOSE_ISSUE})

    first = WorkerControl(store, refusing).finish(
        "store", 5, "gen-1", "resolved", "Answered in comments"
    )

    assert not first.ok
    assert first.reason == "terminal marker landed; issue close deferred"

    # Everything the first attempt did land is durable, so the retry takes the repeat path —
    # which has to finish the close rather than call the card done on the strength of the board.
    retry_port = _port(
        snapshot=CardSnapshot(status=Role.DONE.value, terminal_marker=Role.DONE.value)
    )
    retry = WorkerControl(store, retry_port).finish(
        "store", 5, "gen-1", "resolved", "Answered in comments"
    )

    assert retry.ok
    assert [effect.kind for effect in retry_port.applied] == [EffectKind.CLOSE_ISSUE]


def test_an_already_closed_card_does_not_get_a_second_close(tmp_path):
    port = _port(snapshot=CardSnapshot(status="In progress", open=False))

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "resolved", "Answered in comments"
    )

    assert result.ok
    assert EffectKind.CLOSE_ISSUE not in [effect.kind for effect in port.applied]


def test_repeated_finish_is_a_clean_noop_for_the_same_generation_and_status(tmp_path):
    port = _port(
        snapshot=CardSnapshot(
            status="In review",
            terminal_marker="In review",
        )
    )

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "in_review", "Repeated summary"
    )

    assert result.ok
    assert result.reason == "already finished -> In review"
    assert port.applied == []


def test_each_terminal_report_counts_a_round_the_worker_delivered(tmp_path):
    """What a head-watching reviewer reads to find the rounds that pushed no commit (#1840).

    Two rounds are two dispatches, so they carry two generations. Two ``finish`` calls on one
    generation are one report sent twice, which the repeat test below pins separately.
    """
    store = _store(tmp_path)

    assert WorkerControl(store, _port()).finish(
        "store", 5, "gen-1", "in_review", "First report"
    ).ok
    with store.transaction() as state:
        assert state["issues"][_KEY]["worker_rounds"] == 1
        state["issues"][_KEY]["generation"] = "gen-2"

    assert WorkerControl(store, _port()).finish(
        "store", 5, "gen-2", "in_review", "Evidence, no new head"
    ).ok
    with store.transaction() as state:
        assert state["issues"][_KEY]["worker_rounds"] == 2


def test_a_repeated_finish_is_one_round_reported_twice_and_not_two_rounds(tmp_path):
    """The marker is this generation's, so the round is real — and counted exactly once."""
    store = _store(tmp_path)
    port = _port(snapshot=CardSnapshot(status="In review", terminal_marker="In review"))
    control = WorkerControl(store, port)

    assert control.finish("store", 5, "gen-1", "in_review", "Again").ok
    with store.transaction() as state:
        assert state["issues"][_KEY]["worker_rounds"] == 1

    assert control.finish("store", 5, "gen-1", "in_review", "And again").ok
    with store.transaction() as state:
        assert state["issues"][_KEY]["worker_rounds"] == 1


def test_a_round_survives_a_landed_marker_whose_board_write_failed(tmp_path):
    """The report is the marker, not the board write (#1840 review, finding 1).

    The count used to hang off `SET_STATUS`. A marker that landed while the board write failed
    was a round the worker really delivered and the reviewer could never see.
    """
    store = _store(tmp_path, worker_rounds=1, counted_rounds=["gen-0"])
    port = _port(
        snapshot=CardSnapshot(status="In progress"), refuse={EffectKind.SET_STATUS}
    )

    result = WorkerControl(store, port).finish("store", 5, "gen-1", "in_review", "Evidence")

    assert not result.ok, "the board write really did fail"
    with store.transaction() as state:
        assert state["issues"][_KEY]["worker_rounds"] == 2


def test_a_marker_that_never_landed_is_not_a_round(tmp_path):
    store = _store(tmp_path)
    port = _port(refuse={EffectKind.COMMENT})

    assert not WorkerControl(store, port).finish("store", 5, "gen-1", "in_review", "Lost").ok
    with store.transaction() as state:
        assert "worker_rounds" not in state["issues"][_KEY]


def test_recovery_and_the_workers_retry_are_one_round_between_them(tmp_path):
    """Marker lands, board write fails, reconciliation finishes it, the worker retries.

    Three observers of one durable report. The reviewer asked for the count to become 2 exactly
    once across all of them, and for the unchanged-head review to become eligible.
    """
    store = _store(tmp_path, worker_rounds=1, counted_rounds=["gen-0"])
    refusing = _port(
        snapshot=CardSnapshot(status="In progress"), refuse={EffectKind.SET_STATUS}
    )
    assert not WorkerControl(store, refusing).finish(
        "store", 5, "gen-1", "in_review", "Evidence"
    ).ok

    # Reconciliation finishes the card from the marker the failed attempt left behind.
    with store.transaction() as state:
        count_round(state["issues"][_KEY], "gen-1")
        assert state["issues"][_KEY]["worker_rounds"] == 2

    # And the worker comes back and retries the same finish.
    settled = _port(snapshot=CardSnapshot(status="In review", terminal_marker="In review"))
    assert WorkerControl(store, settled).finish(
        "store", 5, "gen-1", "in_review", "Evidence"
    ).ok
    with store.transaction() as state:
        assert state["issues"][_KEY]["worker_rounds"] == 2


def test_a_later_generations_report_is_a_new_round(tmp_path):
    store = _store(tmp_path, worker_rounds=2, counted_rounds=["gen-0", "gen-1"])
    with store.transaction() as state:
        state["issues"][_KEY]["generation"] = "gen-2"
    port = _port(snapshot=CardSnapshot(status="In progress"))

    assert WorkerControl(store, port).finish("store", 5, "gen-2", "in_review", "Next").ok
    with store.transaction() as state:
        assert state["issues"][_KEY]["worker_rounds"] == 3


def test_finish_does_not_trust_the_board_status_without_this_generations_marker(tmp_path):
    port = _port(snapshot=CardSnapshot(status="In review"))

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "in_review", "First report"
    )

    assert result.ok
    assert [effect.kind for effect in port.applied] == [
        EffectKind.COMMENT,
        EffectKind.SET_STATUS,
    ]


def test_finish_reuses_a_landed_marker_when_retrying_the_board_transition(tmp_path):
    class RecordingRefusal(FakePort):
        def __init__(self):
            super().__init__(
                snapshots={
                    _KEY: CardSnapshot(
                        status="In progress",
                        terminal_marker="In review",
                    )
                },
                refuse={EffectKind.SET_STATUS},
            )
            self.attempted = []

        def apply(self, effect):
            self.attempted.append(effect)
            return super().apply(effect)

    port = RecordingRefusal()

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "in_review", "Repeated summary"
    )

    assert not result.ok
    assert result.reason == "terminal marker landed; board transition deferred"
    assert [effect.kind for effect in port.attempted] == [EffectKind.SET_STATUS]


# --- a terminal marker GitHub fumbled (#2714) ---


class _FumblingPort(FakePort):
    """Fails the first marker writes with what a real `Gh` would remember of GitHub's answer."""

    def __init__(self, *, failures, said, transient, lands_anyway=False):
        super().__init__(snapshots={_KEY: CardSnapshot(status="In progress")})
        self.failures = failures
        self.api = type("Api", (), {"last_failure": said, "last_failure_transient": transient})()
        self.lands_anyway = lands_anyway
        self.comment_attempts = 0

    def apply(self, effect):
        if effect.kind is EffectKind.COMMENT:
            self.comment_attempts += 1
            if self.comment_attempts <= self.failures:
                if self.lands_anyway:
                    # The 5xx hid a write that did land: the next read shows the marker.
                    self.snapshots[_KEY] = CardSnapshot(
                        status="In progress", terminal_marker="In review"
                    )
                return False
        return super().apply(effect)


def test_a_marker_lost_to_a_github_hiccup_is_posted_once_more(tmp_path):
    port = _FumblingPort(
        failures=1, said="github answered: POST /repos/o/r/issues/5/comments -> HTTP 502",
        transient=True,
    )

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "in_review", "Evidence"
    )

    assert result.ok, result.reason
    assert port.comment_attempts == 2
    assert port.kinds() == [EffectKind.COMMENT, EffectKind.SET_STATUS]


def test_a_hiccup_that_landed_the_marker_after_all_is_never_posted_twice(tmp_path):
    port = _FumblingPort(failures=1, said="HTTP 502", transient=True, lands_anyway=True)

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "in_review", "Evidence"
    )

    assert result.ok, result.reason
    assert port.comment_attempts == 1
    assert port.kinds() == [EffectKind.SET_STATUS]


def test_a_considered_refusal_is_not_repeated_and_says_what_github_answered(tmp_path):
    said = "github answered: POST /repos/o/r/issues/5/comments -> HTTP 422: Validation Failed"
    port = _FumblingPort(failures=2, said=said, transient=False)

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "in_review", "Evidence"
    )

    assert not result.ok
    assert port.comment_attempts == 1
    assert result.reason == f"terminal marker did not land — {said}"


def test_a_hiccup_twice_running_asks_for_the_same_finish_again(tmp_path):
    port = _FumblingPort(failures=2, said="HTTP 502", transient=True)

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "in_review", "Evidence"
    )

    assert not result.ok
    assert port.comment_attempts == 2
    assert result.reason.startswith("terminal marker did not land on a second attempt either")
    assert result.reason.endswith("run the same finish again in a while")


# --- a blocked worker naming what has to land first (#1230) ---

def test_a_blocked_worker_records_the_references_it_is_waiting_on(tmp_path):
    store = _store(tmp_path)

    result = WorkerControl(store, _port()).finish(
        "store", 5, "gen-1", "blocked", "Waiting for the prerequisite",
        blocked_by=("#122", "https://github.com/org/other/pull/7"),
    )

    assert result.ok
    assert store.read()["issues"][_KEY]["blocked_until"] == ["store#122", "other#7"]


def test_a_blocked_worker_that_names_nothing_leaves_no_hold(tmp_path):
    """The old behavior exactly: the card rests in Blocked until a person says otherwise."""
    store = _store(tmp_path)

    assert WorkerControl(store, _port()).finish(
        "store", 5, "gen-1", "blocked", "A person has to decide"
    ).ok
    assert "blocked_until" not in store.read()["issues"][_KEY]


def test_any_other_ending_drops_a_hold_an_earlier_round_left(tmp_path):
    """A resume armed off a stale hold would wake a card whose work has already landed."""
    store = _store(tmp_path, blocked_until=["store#122"])

    assert WorkerControl(store, _port()).finish(
        "store", 5, "gen-1", "in_review", "Done after all"
    ).ok
    assert "blocked_until" not in store.read()["issues"][_KEY]


def test_naming_a_dependency_while_not_reporting_blocked_is_refused(tmp_path):
    """Silently dropping the refs would leave the card resting with nothing to wake it."""
    port = _port()
    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "in_review", "Done", blocked_by=("#122",),
    )

    assert not result.ok
    assert "only meaningful with finish blocked" in result.reason
    assert port.applied == []


def test_a_dependency_that_names_no_card_is_refused_rather_than_ignored(tmp_path):
    port = _port()
    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "blocked", "Waiting", blocked_by=("the upload API",),
    )

    assert not result.ok
    assert "must name an issue or pull request" in result.reason
    assert port.applied == []


def test_an_active_merge_review_cannot_finish_in_review_without_a_verdict(tmp_path):
    """#963: accepting this leaves the engine waiting forever for a verdict marker."""
    port = _port()
    control = WorkerControl(_store(tmp_path, mode="merge-review"), port)

    result = control.finish("store", 5, "gen-1", "in_review", "Review complete")

    assert not result.ok
    assert "requires review pass or changes-needed" in result.reason
    assert port.applied == []


def test_a_merge_review_may_still_finish_when_it_cannot_produce_a_verdict(tmp_path):
    for status in ("needs_more_info", "blocked"):
        port = _port()
        control = WorkerControl(_store(tmp_path / status, mode="merge-review"), port)

        assert control.finish("store", 5, "gen-1", status, "Cannot review").ok


def test_a_human_review_finish_parked_in_needs_more_info_says_what_it_stands_for(tmp_path):
    """A board with no Human review pending column parks the finish in Needs More Info; the
    record says so, so a reader can tell a review from a question (#2235)."""
    store = _store(tmp_path)
    assert WorkerControl(store, _port()).finish(
        "store", 5, "gen-1", "human_review_pending", "Please review"
    ).ok
    rec = store.read()["issues"][_KEY]
    assert rec["parked_as"] == "human_review_pending"

    # A board with the column needs no marker, and any other finish drops an old one.
    with_column = _store(tmp_path / "column", parked_as="human_review_pending")
    assert WorkerControl(
        with_column, _port(), human_review_pending=Role.HUMAN_REVIEW_PENDING.value
    ).finish("store", 5, "gen-1", "human_review_pending", "Please review").ok
    assert "parked_as" not in with_column.read()["issues"][_KEY]
    other = _store(tmp_path / "other", parked_as="human_review_pending")
    assert WorkerControl(other, _port()).finish(
        "store", 5, "gen-1", "needs_more_info", "Which one?"
    ).ok
    assert "parked_as" not in other.read()["issues"][_KEY]


class _CardWithComment(Gh):
    """A live card whose only comment is the one a worker operation just posted."""

    def __init__(self, body: str):
        super().__init__()
        self.body = body

    def run(self, *args):
        if "graphql" not in args:
            return (0, "{}", "")
        return (0, json.dumps({"data": {
            "rateLimit": {"remaining": 4999},
            "r0": {"card": {
                "state": "OPEN",
                "labels": {"nodes": []},
                "comments": {"nodes": [{"body": self.body}]},
                "projectItems": {"nodes": []},
                "updatedAt": None,
            }},
        }}), "")


def test_the_marker_finish_renders_is_the_one_the_adapter_reads_back_as_a_board_column(tmp_path):
    """`status=in_review` once reached the board as "in review", which is not "In review", so the
    recovered status write missed its option id and failed silently on every wake (#279). The
    worker no longer writes that marker by hand, so the drift risk moved here: what `finish`
    renders has to survive a round trip through the adapter's own parser as a real board role.
    """
    roles = {role.value for role in Role}
    for token in ("in_review", "needs_more_info", "human_review_pending", "blocked"):
        port = _port()
        assert WorkerControl(
            _store(tmp_path / token),
            port,
            human_review_pending=Role.HUMAN_REVIEW_PENDING.value,
        ).finish("store", 5, "gen-1", token, "Worker summary").ok
        posted = str(port.applied[0].value)

        snapshot = GitHubAdapter(
            org="example", issue_repos=(), code_repos=(), project_number=1,
            api=_CardWithComment(posted),
        ).snapshot("store", 5, generation="gen-1")
        assert snapshot is not None
        assert snapshot.terminal_marker in roles, f"{token} reaches the engine as no board column"
        # And it is the very column `finish` asked the board for.
        assert snapshot.terminal_marker == port.applied[1].value


def test_finish_never_moves_the_board_if_the_card_is_invalidated_after_comment(tmp_path):
    class InvalidatingPort(FakePort):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def snapshot_cards(self, cards):
            self.reads += 1
            return SnapshotBatch(
                cards={
                    key: CardSnapshot(status="In progress", owned=self.reads == 1)
                    for key in cards
                }
            )

    port = InvalidatingPort()
    # The card was seen wearing the label, so the second read losing it is a hand-back (#2053).
    result = WorkerControl(_store(tmp_path, label_claim=True), port).finish(
        "store", 5, "gen-1", "blocked", "Could not continue"
    )

    assert not result.ok
    assert [effect.kind for effect in port.applied] == [EffectKind.COMMENT]


def test_merge_review_verdict_requires_the_active_review_generation(tmp_path):
    port = _port()
    control = WorkerControl(_store(tmp_path, mode="merge-review"), port)

    assert not control.review("store", 5, "old", "pass", "Clean").ok
    assert control.review("store", 5, "gen-1", "pass", "Clean").ok
    assert [effect.kind for effect in port.applied] == [EffectKind.COMMENT]
    assert "<!-- claude-merge-review generation=gen-1 verdict=pass -->" in str(
        port.applied[0].value
    )


def test_review_verdict_fails_closed_when_no_merge_review_is_active(tmp_path):
    port = _port()
    result = WorkerControl(_store(tmp_path), port).review(
        "store", 5, "gen-1", "changes-needed", "One blocker"
    )

    assert not result.ok
    assert port.applied == []


@pytest.mark.parametrize(
    "legacy",
    [
        {"auto_review": True, "automerge_spent": True},
        {"status": "Needs More Info", "reportless_resolved": "gen-1",
         "auto_review_generation": "gen-1"},
    ],
    ids=["collapsed-arm", "parked-collapsed-arm"],
)
def test_a_retired_standing_approval_record_cannot_report_a_verdict(tmp_path, legacy):
    """#2502: only an armed `/merge review` turn reports a verdict; old label state admits none."""
    port = _port()
    store = _store(tmp_path, **legacy)

    result = WorkerControl(store, port).review("store", 5, "gen-1", "pass", "Clean")

    assert not result.ok
    assert port.applied == []
    assert store.read()["issues"][_KEY].get("mode") is None


# --- the retired phase checkpoint (#2154) ---


def test_finish_no_longer_touches_an_old_phase_block(tmp_path):
    """#2154 test 9: settled phase state from before the retirement is left exactly as it was
    and does not alter the Run's own lifecycle."""
    old_block = {"phase": "verify", "status": "waiting_job", "checkpoints": {}}
    store = _store(tmp_path, phases=dict(old_block))

    port = _port()
    result = WorkerControl(store, port).finish("store", 5, "gen-1", "in_review", "done")

    assert result.ok, result.reason
    assert port.applied[-1].value == "In review"
    assert store.read()["issues"][_KEY]["phases"] == old_block


def test_there_is_no_stop_ack_or_handoff_report_verb_any_more(tmp_path):
    """#2159 tests 11 and 12: the Runtime stops the process, and no superseded worker is left
    running to report, so neither verb survives."""
    control = WorkerControl(_store(tmp_path), _port())
    assert not hasattr(control, "stop_ack")
    assert not hasattr(control, "handoff_report")


def test_a_worker_keeps_its_control_while_its_own_successor_is_still_undelivered(tmp_path):
    """#1801, from #1798's chat. The card's full-suite job passed, so two things reacted at
    once: the host queue told the worker directly, waking it to push its branch and post its
    result, and the engine minted the `finalize` successor. The mint invalidated the running
    worker immediately, but the delivery was refused with `active_turn` — because of that same
    worker — and only landed a wake later. In between, `finish` failed with `stale:`."""
    store = _store(
        tmp_path,
        generation="gen-2",
        dispatch={"id": "d1", "continuation": True, "supersedes": "gen-1"},
    )

    assert WorkerControl(store, _port()).enabled("store", 5, "gen-1").ok
    assert WorkerControl(store, _port()).enabled("store", 5, "gen-2").ok
    assert not WorkerControl(store, _port()).enabled("store", 5, "gen-0").ok


def test_the_predecessor_is_fenced_again_once_the_successor_delivery_is_settled(tmp_path):
    store = _store(tmp_path, generation="gen-2")

    result = WorkerControl(store, _port()).enabled("store", 5, "gen-1")

    assert not result.ok
    assert result.reason == "stale: this generation has been superseded; stop now"


def test_an_outstanding_replacement_worker_still_fences_the_one_it_replaced(tmp_path):
    """Only a continuation is handed to the worker already on the card. A retry exists
    precisely to take the work away, so its predecessor stays refused."""
    store = _store(
        tmp_path,
        generation="gen-2",
        dispatch={"id": "d1", "supersedes": "gen-1"},
    )

    assert not WorkerControl(store, _port()).enabled("store", 5, "gen-1").ok


def test_a_stop_still_beats_the_undelivered_successor_grace(tmp_path):
    store = _store(
        tmp_path,
        generation="gen-2",
        stopping=True,
        stop_waiting_generation="gen-1",
        dispatch={"id": "d1", "continuation": True, "supersedes": "gen-1"},
    )

    result = WorkerControl(store, _port()).enabled("store", 5, "gen-1")

    assert not result.ok
    assert result.reason == "disabled: stop requested for this generation; stop now"


# --- reopen: a worker takes back its own `resolved` finish (#2522) ---

_FINISHED = CardSnapshot(
    status=Role.DONE.value, terminal_marker=Role.DONE.value, open=False
)


def _resolved_store(tmp_path, *, at=_NOW, **extra):
    """A card its worker `gen-1` finished `resolved`, as the record holds it after a wake."""
    fields = {
        "status": Role.DONE.value,
        "resolved_finish": {"generation": "gen-1", "at": at.isoformat()},
        **extra,
    }
    return _store(tmp_path, **fields)


def _control(store, port):
    return WorkerControl(store, port, clock=lambda: _NOW)


def test_finish_resolved_stamps_the_lease_that_closed_the_card(tmp_path):
    store = _store(tmp_path)

    assert _control(store, _port()).finish("store", 5, "gen-1", "resolved", "Done").ok

    assert store.read()["issues"][_KEY]["resolved_finish"] == {
        "generation": "gen-1",
        "at": _NOW.isoformat(),
    }


def test_no_other_ending_stamps_a_reopen_hold(tmp_path):
    for token in ("in_review", "needs_more_info", "blocked"):
        store = _store(tmp_path / token)
        assert _control(store, _port()).finish("store", 5, "gen-1", token, "x").ok
        assert "resolved_finish" not in store.read()["issues"][_KEY]


def test_reopen_puts_the_same_run_back_to_work(tmp_path):
    store = _resolved_store(tmp_path)
    port = _port(snapshot=_FINISHED)

    result = _control(store, port).reopen(
        "store", 5, "gen-1", "The person asked me to write a spec for the fix."
    )

    assert result.ok, result.reason
    assert result.reason.startswith("reopened -> In progress")
    # The approved order (#2522 D3): the comment first (it makes the finish no longer the
    # newest marker), then the issue, then the board.
    assert [effect.kind for effect in port.applied] == [
        EffectKind.COMMENT,
        EffectKind.REOPEN_ISSUE,
        EffectKind.SET_STATUS,
    ]
    body = str(port.applied[0].value)
    assert "The person asked me to write a spec" in body
    assert "<!-- claude-terminal generation=gen-1 status=resumed -->" in body
    assert port.applied[2].value == Role.IN_PROGRESS.value
    rec = store.read()["issues"][_KEY]
    assert rec["status"] == Role.IN_PROGRESS.value
    assert rec["generation"] == "gen-1"
    assert "resolved_finish" not in rec
    assert "dispatch" not in rec


def test_a_reopened_run_finishes_again_as_a_new_report(tmp_path):
    store = _resolved_store(tmp_path)
    assert _control(store, _port(snapshot=_FINISHED)).reopen("store", 5, "gen-1", "More").ok

    # The `resumed` comment is now the newest marker for the lease: it reads as unreported.
    port = _port(snapshot=CardSnapshot(status=Role.IN_PROGRESS.value))
    result = _control(store, port).finish("store", 5, "gen-1", "resolved", "Spec posted")

    assert result.ok
    assert [effect.kind for effect in port.applied] == [
        EffectKind.COMMENT,
        EffectKind.SET_STATUS,
        EffectKind.CLOSE_ISSUE,
    ]
    assert store.read()["issues"][_KEY]["resolved_finish"]["generation"] == "gen-1"


def test_a_repeated_reopen_posts_nothing_twice(tmp_path):
    store = _resolved_store(tmp_path)
    refusing = _port(snapshot=_FINISHED, refuse={EffectKind.SET_STATUS})

    first = _control(store, refusing).reopen("store", 5, "gen-1", "More")

    assert not first.ok
    assert first.reason.startswith("unavailable: the board did not move")
    assert [effect.kind for effect in refusing.applied] == [
        EffectKind.COMMENT, EffectKind.REOPEN_ISSUE,
    ]
    # Nothing in the record moved, so the hold still covers the card for the repeat.
    rec = store.read()["issues"][_KEY]
    assert rec["status"] == Role.DONE.value
    assert rec["resolved_finish"]["generation"] == "gen-1"

    # The issue is open again and the `resumed` comment is the newest marker.
    retry_port = _port(snapshot=CardSnapshot(status=Role.DONE.value, open=True))
    retry = _control(store, retry_port).reopen("store", 5, "gen-1", "More")

    assert retry.ok, retry.reason
    assert [effect.kind for effect in retry_port.applied] == [EffectKind.SET_STATUS]
    assert store.read()["issues"][_KEY]["status"] == Role.IN_PROGRESS.value


def test_a_refused_reopen_leaves_only_the_comment(tmp_path):
    store = _resolved_store(tmp_path)
    port = _port(snapshot=_FINISHED, refuse={EffectKind.REOPEN_ISSUE})

    result = _control(store, port).reopen("store", 5, "gen-1", "More")

    assert not result.ok
    assert result.reason.startswith("unavailable: the issue did not reopen")
    assert [effect.kind for effect in port.applied] == [EffectKind.COMMENT]
    assert store.read()["issues"][_KEY]["status"] == Role.DONE.value


def test_reopen_before_the_wake_read_the_finish_keeps_the_runs_commands(tmp_path):
    """The record still says In progress and still carries this Run's commands until a wake
    reads the finish; they stay with the Run that carries on."""
    store = _resolved_store(
        tmp_path, status=Role.IN_PROGRESS.value, processing=["c1"]
    )

    result = _control(store, _port(snapshot=_FINISHED)).reopen("store", 5, "gen-1", "More")

    assert result.ok, result.reason
    assert store.read()["issues"][_KEY]["processing"] == ["c1"]


def test_reopen_refuses_what_is_not_this_runs_own_to_take_back(tmp_path):
    def reopen(store, generation="gen-1", body="More"):
        return _control(store, _port(snapshot=_FINISHED)).reopen("store", 5, generation, body)

    # Another Run now holds the card, or an older one asks.
    stale = "stale: this generation has been superseded; stop now"
    other = _resolved_store(tmp_path / "other", generation="gen-2")
    assert reopen(other).reason == stale
    assert reopen(_resolved_store(tmp_path / "older"), generation="gen-0").reason == stale
    # The Run an undelivered continuation replaces may still act, but not take the card back.
    outgoing = _resolved_store(
        tmp_path / "outgoing",
        generation="gen-2",
        dispatch={"id": "d1", "continuation": True, "supersedes": "gen-1"},
    )
    assert not reopen(outgoing).ok
    assert outgoing.read()["issues"][_KEY]["status"] == Role.DONE.value
    # Its next step is already decided.
    for name, value in (
        ("dispatch", {"id": "d1"}),
        ("pending", ["c1"]),
        ("stopping", True),
        ("release_pending", True),
    ):
        result = reopen(_resolved_store(tmp_path / name, **{name: value}))
        assert result.reason.startswith("refused: this card already has its next step"), name
        assert name in result.reason
    # Nothing to say about why.
    assert reopen(_resolved_store(tmp_path / "empty"), body="  ").reason.startswith("refused:")


# #2845: what `reopen` cannot take back in place it queues as the person's `/claude`, so the
# person who said "reopen" is never asked a second time for the board command.
_QUEUED = "queued: the Poller reopens the issue and hands the work back on its next wake"
_MERGED = CardSnapshot(status=Role.DONE.value, open=False)


def _report(generation, n=1):
    return {
        "body": f"Done.\n\n<!-- claude-terminal generation={generation} status=in_review -->",
        "createdAt": f"2026-07-27T10:{n:02d}:00Z",
    }


def _merged_port(*, history=None, **kwargs):
    """A merged, closed card whose whole comment history ends with `gen-1`'s report."""
    port = _port(snapshot=kwargs.pop("snapshot", _MERGED), **kwargs)
    port.issue_comment_history[_KEY] = list(
        history if history is not None else [_report("gen-0", 1), _report("gen-1", 2)]
    )
    return port


def _request(store, generation="gen-1"):
    path = store.paths.state.parent / "deck-releases" / f"reopen-{generation}-5.json"
    return json.loads(path.read_text()) if path.exists() else None


@pytest.mark.parametrize(
    "case",
    ["released", "merged_in_review", "past_the_hold"],
)
def test_reopen_queues_a_card_it_cannot_take_back_in_place(tmp_path, case):
    store = {
        # The wake already forgot it (#2841: merged, closed, released).
        "released": lambda: store_for(tmp_path, {"issues": {}}),
        # Finished `in_review`, then the Poller merged it: never a `resolved` of this Run.
        "merged_in_review": lambda: _store(tmp_path, status=Role.DONE.value),
        "past_the_hold": lambda: _resolved_store(
            tmp_path, at=_NOW - timedelta(hours=24, seconds=1)
        ),
    }[case]()
    port = _merged_port()

    result = _control(store, port).reopen("store", 5, "gen-1", "Chat page is still slow.")

    assert result.ok, result.reason
    assert result.reason.startswith(_QUEUED)
    assert "end your turn now with no finish" in result.reason
    assert [effect.kind for effect in port.applied] == [EffectKind.COMMENT]
    note = str(port.applied[0].value)
    assert "Reopen asked in the worker's chat" in note
    assert "Chat page is still slow." in note
    assert "<!-- agentdeck-reopen-request id=reopen-gen-1-5 -->" in note
    request = _request(store)
    assert request["command"] == "/claude Chat page is still slow."
    assert (request["key"], request["by"], request["kind"]) == (
        "store#5", "worker-reopen", "claude"
    )
    # Nothing in the record moved: the wake that reads the request does the rest.
    assert store.read().get("issues", {}).get(_KEY, {}).get("status") in {None, Role.DONE.value}

    # #3316: where the deck runs the Works, the Poller drops such a request, so it is refused
    # at once and nothing is posted or queued.
    port = _merged_port()
    refused = WorkerControl(store, port, works_router=True, clock=lambda: _NOW).reopen(
        "store", 5, "gen-1", "Chat page is still slow."
    )
    assert not refused.ok and "the deck runs this project's Works" in refused.reason
    assert port.applied == []


def test_a_multi_line_reopen_sends_its_first_line_and_posts_the_whole_body(tmp_path):
    """Approved D1 and D8: the `/claude` carries the ask's first non-empty line, and the note
    on the issue carries the whole body."""
    store = store_for(tmp_path, {"issues": {}})
    port = _merged_port()
    body = "\n  \n  Chat page is still slow.  \nMedian 0.46 s, see the production check.\n"

    assert _control(store, port).reopen("store", 5, "gen-1", body).ok

    assert _request(store)["command"] == "/claude Chat page is still slow."
    note = str(port.applied[0].value)
    assert "Chat page is still slow." in note
    assert "Median 0.46 s, see the production check." in note


def test_a_repeated_queued_reopen_asks_nothing_twice(tmp_path):
    store = store_for(tmp_path, {"issues": {}})
    assert _control(store, _merged_port()).reopen("store", 5, "gen-1", "More").ok

    again = _merged_port()
    result = _control(store, again).reopen("store", 5, "gen-1", "More")

    assert result.ok and result.reason.startswith(_QUEUED)
    assert again.applied == []
    # Once a wake turned the file into a control, the ledger answers the same.
    (store.paths.state.parent / "deck-releases" / "reopen-gen-1-5.json").unlink()
    with store.transaction() as state:
        state.setdefault("controls", {})["deck:reopen-gen-1-5"] = {"status": "queued"}
    assert _control(store, again).reopen("store", 5, "gen-1", "More").ok
    assert again.applied == []


def test_a_queued_reopen_whose_request_was_not_written_does_not_post_its_note_again(
    tmp_path, monkeypatch
):
    """Review finding: the note landed, the request write failed, and the advertised safe retry
    must only write — the note's marker says it is already on the issue."""
    from agentdeck.poller_engine import deck_releases

    store = store_for(tmp_path, {"issues": {}})
    real_write = deck_releases.write

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(deck_releases, "write", broken)
    first_port = _merged_port()
    first = _control(store, first_port).reopen("store", 5, "gen-1", "More")
    assert first.reason.startswith("unavailable: the request could not be written")
    assert [effect.kind for effect in first_port.applied] == [EffectKind.COMMENT]
    assert _request(store) is None

    monkeypatch.setattr(deck_releases, "write", real_write)
    landed = {"body": str(first_port.applied[0].value), "createdAt": "2026-07-27T11:00:00Z"}
    retry_port = _merged_port(history=[_report("gen-1", 2), landed])
    retry = _control(store, retry_port).reopen("store", 5, "gen-1", "More")

    assert retry.ok, retry.reason
    assert retry_port.applied == []
    assert _request(store)["command"] == "/claude More"


def test_a_queued_reopen_needs_this_runs_report_to_be_the_newest(tmp_path):
    for name, history in (
        ("other", [_report("gen-1", 1), _report("gen-2", 2)]),
        ("none", [{"body": "a person's comment", "createdAt": "2026-07-27T10:00:00Z"}]),
    ):
        store = store_for(tmp_path / name, {"issues": {}})
        port = _merged_port(history=history)

        result = _control(store, port).reopen("store", 5, "gen-1", "More")

        assert result.reason.startswith("disabled: this card cannot be reopened from here")
        # The deck takes no board command from an agent any more (#3025): the person posts it.
        assert "ask the person to post `/claude" in result.reason
        assert "board-commands" not in result.reason
        assert port.applied == []
        assert _request(store) is None


def test_a_queued_reopen_sees_its_report_past_a_hundred_later_comments(tmp_path):
    """Review finding: the wake's snapshot reads the newest 100 comments only, so the reopen
    reads the whole history — a long thread after the report must not end the right to reopen."""
    later = [
        {"body": f"note {n}", "createdAt": f"2026-07-27T11:{n % 60:02d}:00Z"} for n in range(150)
    ]
    store = store_for(tmp_path, {"issues": {}})
    port = _merged_port(history=[_report("gen-1", 1), *later])

    result = _control(store, port).reopen("store", 5, "gen-1", "More")

    assert result.ok, result.reason
    assert _request(store) is not None


def test_a_queued_reopen_refuses_an_open_card_it_no_longer_holds(tmp_path):
    """Review finding, approved D2: the queued path is for a closed card. An open one somebody
    reopened is theirs to start; the worker sends the `/claude` itself, without asking again."""
    store = store_for(tmp_path, {"issues": {}})
    port = _merged_port(snapshot=CardSnapshot(status=Role.DONE.value, open=True))

    result = _control(store, port).reopen("store", 5, "gen-1", "More")

    assert result.reason.startswith("disabled: this card is open but no longer yours to reopen")
    assert "ask the person to post `/claude" in result.reason
    assert port.applied == []
    assert _request(store) is None


def test_a_queued_reopen_whose_comment_did_not_land_queues_nothing(tmp_path):
    store = store_for(tmp_path, {"issues": {}})
    port = _merged_port(refuse={EffectKind.COMMENT})

    result = _control(store, port).reopen("store", 5, "gen-1", "More")

    assert result.reason.startswith("unavailable: the comment did not land")
    assert _request(store) is None
    deferred = _control(store_for(tmp_path / "d", {"issues": {}}), _port(graphql_deferred=True))
    assert deferred.reopen("store", 5, "gen-1", "More").reason.startswith("unavailable:")
    unreadable = _merged_port()
    unreadable.unreadable_issue_comments.add(_KEY)
    blind = _control(store_for(tmp_path / "u", {"issues": {}}), unreadable)
    assert blind.reopen("store", 5, "gen-1", "More").reason.startswith("unavailable:")
    assert unreadable.applied == []


def test_a_queued_reopen_is_reopened_and_started_by_the_next_wake(tmp_path):
    """The request is the deck's own `/claude`: one wake queues it and starts the worker, and
    the card's first live read reopens the issue closed before it (#2748, #2845)."""
    from poller_engine_fakes import run_wake

    store = store_for(tmp_path, {"issues": {}})
    assert _control(store, _merged_port()).reopen(
        "store", 5, "gen-1", "Chat page is still slow."
    ).ok

    port = FakePort()
    outcome = run_wake(store, port, now=_NOW + timedelta(seconds=5))

    assert len(outcome.directives) == 1, outcome.log
    assert [m.get("payload") for m in outcome.directives[0]["messages"]] == [
        "Chat page is still slow."
    ]
    assert store.read()["issues"][_KEY]["reopen_if_closed"]
    port.snapshots[_KEY] = CardSnapshot(
        status=Role.DONE.value, open=False, owned=False, closed_at="2026-07-27T11:00:00Z"
    )
    run_wake(store, port, now=_NOW + timedelta(seconds=45))
    assert port.kinds().count(EffectKind.REOPEN_ISSUE) == 1
    assert store.read()["issues"][_KEY]["status"] == Role.IN_PROGRESS.value


def test_reopen_still_honours_every_lease_refusal(tmp_path):
    store = _resolved_store(tmp_path, label_claim=True)
    unowned = _port(snapshot=CardSnapshot(status=Role.DONE.value, open=False, owned=False))

    result = _control(store, unowned).reopen("store", 5, "gen-1", "More")

    assert result.reason == (
        "disabled: the card's ownership label was removed; stop and leave the work "
        "where it is"
    )
    assert unowned.applied == []
    deferred = _control(_resolved_store(tmp_path / "d"), _port(graphql_deferred=True))
    assert deferred.reopen("store", 5, "gen-1", "More").reason.startswith("unavailable:")


def test_finish_still_refuses_a_closed_card_that_is_no_longer_active(tmp_path):
    store = _resolved_store(tmp_path)

    result = _control(store, _port(snapshot=_FINISHED)).finish(
        "store", 5, "gen-1", "in_review", "x"
    )

    assert result.reason == "disabled: the card is closed and no longer active"


def test_a_revived_worker_still_finishes_while_its_revive_is_undelivered(tmp_path):
    """#2529, from #2528's chat. A person typed into the worker's chat after it finished Needs
    More Info; the card resumed, the worker answered and ended its turn without a report, and
    the engine revived the card 47 seconds later. The revive is a continuation waiting for that
    same chat to go idle, but the person kept talking, the worker updated its spec, and its
    `finish` was refused as handed to a new worker — its own next turn. Until the revive lands
    there is nobody else, so the report goes through."""
    port = _port()
    store = _store(
        tmp_path,
        generation="gen-2",
        reportless_resolved="gen-1",
        dispatch={"id": "d1", "continuation": True, "supersedes": "gen-1"},
    )
    control = WorkerControl(store, port)

    assert control.enabled("store", 5, "gen-1").ok
    result = control.finish("store", 5, "gen-1", "needs_more_info", "the spec")

    assert result.ok, result.reason
    (comment,) = [e for e in port.applied if e.kind is EffectKind.COMMENT]
    assert "gen-1 status=needs_more_info" in str(comment.value)


def test_a_revived_worker_is_handed_off_once_its_revive_has_landed(tmp_path):
    """The grace is the undelivered window only: acknowledging the revive pops the dispatch,
    and from then on the newer turn is the one that reports."""
    store = _store(tmp_path, generation="gen-2", reportless_resolved="gen-1")

    result = WorkerControl(store, _port()).finish("store", 5, "gen-1", "needs_more_info", "x")

    assert not result.ok
    assert "that worker is you" in result.reason


def test_a_revive_does_not_reopen_an_older_generation(tmp_path):
    """Only the generation the undelivered revive took over from gets the grace."""
    store = _store(
        tmp_path,
        generation="gen-3",
        reportless_resolved="gen-1",
        dispatch={"id": "d1", "continuation": True, "supersedes": "gen-2"},
    )

    assert not WorkerControl(store, _port()).enabled("store", 5, "gen-1").ok


# --- start-implementation: a spec worker builds the spec approved in its chat (#2716) ---

_SPEC_PARKED = CardSnapshot(
    status=Role.NEEDS_INFO.value, terminal_marker=Role.NEEDS_INFO.value
)


def _spec_store(tmp_path, **extra):
    """A card spec worker `gen-1` parked, whose label a wake has not yet seen removed."""
    return _store(tmp_path, **{"status": Role.NEEDS_INFO.value, "spec_only": True, **extra})


def test_start_implementation_takes_the_build_over_in_the_same_run(tmp_path):
    store = _spec_store(tmp_path)
    port = _port(snapshot=_SPEC_PARKED)

    result = _control(store, port).start_implementation(
        "store", 5, "gen-1", "Eero approved the spec in my chat: “start implementing”."
    )

    assert result.ok, result.reason
    assert result.reason.startswith("started -> In progress")
    assert [effect.kind for effect in port.applied] == [EffectKind.COMMENT, EffectKind.SET_STATUS]
    body = str(port.applied[0].value)
    assert "start implementing" in body
    # The `resumed` marker makes the spec's Needs More Info no longer this lease's report.
    assert "<!-- claude-terminal generation=gen-1 status=resumed -->" in body
    assert port.applied[1].value == Role.IN_PROGRESS.value
    rec = store.read()["issues"][_KEY]
    assert rec["status"] == Role.IN_PROGRESS.value
    assert rec["generation"] == "gen-1"
    # Nothing left for a wake to start a second worker from.
    assert not ({"spec_only", "spec_approved", "spec_build"} & set(rec))
    assert rec["spec_build_in_chat"] == "gen-1"

    # The build then reports as any card does.
    built = _control(store, _port(snapshot=CardSnapshot(status=Role.IN_PROGRESS.value)))
    assert built.finish("store", 5, "gen-1", "in_review", "PR opened").ok


def test_finish_spec_approved_marks_the_approval_only_with_the_label_off(tmp_path):
    """Only the spec worker's own `--spec-approved` finish approves a spec mid-turn (#2773)."""
    store = _spec_store(tmp_path, status=Role.IN_PROGRESS.value)
    labelled = _port(snapshot=CardSnapshot(status=Role.IN_PROGRESS.value, spec_only=True))

    refused = _control(store, labelled).finish(
        "store", 5, "gen-1", "needs_more_info", "Approved", spec_approved=True
    )
    assert not refused.ok
    assert "remove the spec-only label first" in refused.reason
    assert labelled.applied == []

    port = _port(snapshot=CardSnapshot(status=Role.IN_PROGRESS.value))
    result = _control(store, port).finish(
        "store", 5, "gen-1", "needs_more_info", "Approved", spec_approved=True
    )
    assert result.ok, result.reason
    rec = store.read()["issues"][_KEY]
    assert rec["spec_approved"] is True
    assert "spec_only" not in rec


def test_finish_spec_approved_is_refused_where_it_means_nothing(tmp_path):
    port = _port(snapshot=CardSnapshot(status=Role.IN_PROGRESS.value))

    wrong_status = _control(_spec_store(tmp_path / "a"), port).finish(
        "store", 5, "gen-1", "in_review", "PR", spec_approved=True
    )
    assert not wrong_status.ok
    assert "only meaningful with needs_more_info" in wrong_status.reason

    not_spec = _store(tmp_path / "b")
    plain = _control(not_spec, port).finish(
        "store", 5, "gen-1", "needs_more_info", "Q", spec_approved=True
    )
    assert not plain.ok
    assert "only for the Run that wrote a spec" in plain.reason
    assert "spec_approved" not in not_spec.read()["issues"][_KEY]

    taken = _store(tmp_path / "c", spec_build_in_chat="gen-1")
    built = _control(taken, port).finish(
        "store", 5, "gen-1", "needs_more_info", "Q", spec_approved=True
    )
    assert not built.ok
    assert "start-implementation" in built.reason


def test_a_plain_finish_with_the_label_off_drops_the_spec(tmp_path):
    """The person removed the label but asked for something else: no approval survives (#2773)."""
    store = _spec_store(tmp_path, status=Role.IN_PROGRESS.value, spec_approved=True)
    port = _port(snapshot=CardSnapshot(status=Role.IN_PROGRESS.value))

    assert _control(store, port).finish("store", 5, "gen-1", "needs_more_info", "Done").ok

    rec = store.read()["issues"][_KEY]
    assert not ({"spec_only", "spec_approved"} & set(rec))


def test_a_plain_finish_with_the_label_on_keeps_the_spec(tmp_path):
    """Removing the label later, while the card rests, is still the approval (#2706)."""
    store = _spec_store(tmp_path, status=Role.IN_PROGRESS.value)
    port = _port(snapshot=CardSnapshot(status=Role.IN_PROGRESS.value, spec_only=True))

    assert _control(store, port).finish("store", 5, "gen-1", "needs_more_info", "Spec").ok

    assert store.read()["issues"][_KEY]["spec_only"] is True


def test_start_implementation_moves_the_board_after_posting_the_chat_answer(tmp_path):
    """#2777, from eerovil/score-plugins#376. The person approved the spec in the
    worker's chat, and no wake had posted that answer yet. ``start-implementation`` posted it
    and resumed the record, then read the resume as the board's own move and skipped it: the
    card sat in Needs More Info, under Needs you, while its build ran and verified."""
    store = _spec_store(tmp_path)
    chat_answers.write(
        store.paths.state,
        request_id="answer-0001",
        key=_KEY,
        generation="gen-1",
        text="I approve the spec. Start implementing.",
        now=_NOW - timedelta(seconds=5),
    )
    port = _port(snapshot=_SPEC_PARKED)

    result = _control(store, port).start_implementation("store", 5, "gen-1", "Approved")

    assert result.ok, result.reason
    kinds = [effect.kind for effect in port.applied]
    assert kinds == [EffectKind.COMMENT, EffectKind.COMMENT, EffectKind.SET_STATUS]
    assert "I approve the spec" in str(port.applied[0].value)
    assert port.applied[2].value == Role.IN_PROGRESS.value
    assert store.read()["issues"][_KEY]["status"] == Role.IN_PROGRESS.value


def test_start_implementation_after_a_wake_saw_the_label_go(tmp_path):
    """A wake between the label removal and the verb swaps `spec_only` for `spec_approved`;
    the card already resumed In progress from the person's chat answer."""
    store = _store(tmp_path, spec_approved=True)
    port = _port(snapshot=CardSnapshot(status=Role.IN_PROGRESS.value))

    result = _control(store, port).start_implementation("store", 5, "gen-1", "Approved")

    assert result.ok, result.reason
    assert [effect.kind for effect in port.applied] == [EffectKind.COMMENT]
    assert "spec_approved" not in store.read()["issues"][_KEY]


def test_a_repeated_start_implementation_posts_nothing_twice(tmp_path):
    store = _spec_store(tmp_path)
    refusing = _port(snapshot=_SPEC_PARKED, refuse={EffectKind.SET_STATUS})

    first = _control(store, refusing).start_implementation("store", 5, "gen-1", "Approved")

    assert not first.ok
    assert first.reason.startswith("unavailable: the board did not move")
    assert "start-implementation again" in first.reason

    retry_port = _port(snapshot=CardSnapshot(status=Role.NEEDS_INFO.value))
    retry = _control(store, retry_port).start_implementation("store", 5, "gen-1", "Approved")

    assert retry.ok, retry.reason
    assert [effect.kind for effect in retry_port.applied] == [EffectKind.SET_STATUS]
    assert store.read()["issues"][_KEY]["status"] == Role.IN_PROGRESS.value


def test_start_implementation_refuses_what_is_not_an_approved_spec_of_this_run(tmp_path):
    def start(store, snapshot=_SPEC_PARKED, generation="gen-1", body="Approved"):
        port = _port(snapshot=snapshot)
        result = _control(store, port).start_implementation("store", 5, generation, body)
        assert port.applied == [] or not result.ok
        return result

    # The label is still on: only a person's approval takes it off.
    labelled = start(
        _spec_store(tmp_path / "labelled"),
        snapshot=CardSnapshot(status=Role.NEEDS_INFO.value, spec_only=True),
    )
    assert labelled.reason.startswith("refused: remove the `spec-only` label first")
    # An ordinary card: there is no spec to take over.
    plain = start(_store(tmp_path / "plain"))
    assert plain.reason.startswith("refused: this Run did not write a spec")
    # The poller already armed its own build worker, or a person queued something.
    for name, value in (("recovery_action", "resume"), ("pending", ["c1"]), ("mode", "x")):
        held = start(_spec_store(tmp_path / name, **{name: value}))
        assert held.reason.startswith("refused: this card already has its next step"), name
    # A stop, or another Run holding the card, is the lease's own refusal.
    assert start(_spec_store(tmp_path / "stop", stopping=True)).reason.startswith("disabled:")
    stale = start(_spec_store(tmp_path / "stale", generation="gen-2"))
    assert stale.reason.startswith("stale:")
    # Nothing to say about who approved.
    assert start(_spec_store(tmp_path / "empty"), body=" ").reason.startswith("refused:")
