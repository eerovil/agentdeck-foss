"""Reviving the chats a runtime death cut off (#1547).

``test_restart.py`` covers the registry a chat writes *before* a restart. These
cover the other half: what the fresh runtime works out for itself from the
records the death left behind, with nothing registered in advance.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentdeck.config import AccountConfig, AppConfig
from agentdeck.providers import PROVIDERS
from agentdeck.providers.claude_code.delivery import DeliverResult
from agentdeck.restart_nudge import NUDGE_MESSAGE
from agentdeck.revival import (
    BACKGROUND,
    CUT_OFF,
    LOOP_ATTENTION_DETAIL,
    SESSION_CUT_OFF,
    RevivalLedger,
    RevivalRecord,
    RuntimeExitMarker,
    SessionCandidate,
    Survivor,
    find_session_survivors,
    find_survivors,
    revival_looped,
    revival_message,
    revival_paths,
)
from agentdeck.runtime import create_runtime_app

pytestmark = pytest.mark.anyio

TTL_S = 3600.0


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- reading the survivors off the durable records -------------------------


def _worker(**overrides):
    worker = {
        "session_id": "sid-1",
        "live": False,
        "turn_active": False,
        "effective_activity": False,
        "last_delivery_at": 100.0,
        "last_result_at": 100.0,
    }
    worker.update(overrides)
    return worker


def _snapshots(**workers):
    return {("claude_code", "main"): {"workers": workers}}


def test_a_delivered_turn_with_no_result_is_a_survivor():
    survivors = find_survivors(
        _snapshots(**{"issue-1": _worker(last_delivery_at=200.0, last_result_at=100.0)}),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert [(s.session_key, s.reason) for s in survivors] == [
        ("claude_code:main:sid-1", CUT_OFF)
    ]


def test_an_idle_chat_is_left_alone():
    """The result landed, so nothing was interrupted and nothing is owed."""
    survivors = find_survivors(
        _snapshots(**{"issue-1": _worker(last_delivery_at=100.0, last_result_at=200.0)}),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert survivors == []


def test_a_turn_that_survived_the_restart_is_not_revived():
    """Codex app-server backends outlive the runtime with their turns intact.

    Their records read exactly like a cut-off turn, so without this the deck
    would write "you were killed" into a chat that is still working.
    """
    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(
                    live=True,
                    turn_active=True,
                    effective_activity=True,
                    last_delivery_at=200.0,
                    last_result_at=100.0,
                )
            }
        ),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert survivors == []


def test_a_cut_off_turn_older_than_the_ttl_is_dropped():
    survivors = find_survivors(
        _snapshots(**{"issue-1": _worker(last_delivery_at=200.0, last_result_at=100.0)}),
        now=200.0 + TTL_S + 1,
        ttl_s=TTL_S,
    )

    assert survivors == []


def test_a_worker_with_no_session_lineage_is_skipped():
    survivors = find_survivors(
        _snapshots(
            **{"issue-1": _worker(session_id=None, last_delivery_at=200.0, last_result_at=100.0)}
        ),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert survivors == []


def test_an_idle_chat_waiting_on_background_work_is_a_survivor():
    """The kanban worker in #1547: it ended its turn saying it would pick the
    sweep back up, and the sweep died with the runtime."""
    survivors = find_survivors(
        _snapshots(**{"issue-1": _worker(last_delivery_at=100.0, last_result_at=200.0)}),
        now=300.0,
        ttl_s=TTL_S,
        background_tasks=lambda *_: {"task-9"},
    )

    assert [(s.reason, sorted(s.background_tasks)) for s in survivors] == [
        (BACKGROUND, ["task-9"])
    ]


def test_a_transcript_that_cannot_be_read_does_not_end_the_scan():
    def boom(*_args):
        raise OSError("transcript is gone")

    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(last_delivery_at=100.0, last_result_at=200.0),
                "issue-2": _worker(
                    session_id="sid-2", last_delivery_at=200.0, last_result_at=100.0
                ),
            }
        ),
        now=300.0,
        ttl_s=TTL_S,
        background_tasks=boom,
    )

    assert [s.session_key for s in survivors] == ["claude_code:main:sid-2"]


# --- the death that wrote its own result ------------------------------------
#
# What the 2026-09-11 deploy restart actually left on disk (#1844): the Claude CLI
# emits a final `result` as it is torn down, so every killed turn had a result
# newer than its delivery and read as finished. These are the real numbers from
# `claude-workers/main.json` for `agentdeck#1840`, rebased onto this file's clock.


def test_a_turn_the_death_stamped_a_result_for_is_still_a_survivor():
    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(
                    last_delivery_at=200.0,
                    last_result_at=209.7,
                    last_result_subtype="error_during_execution",
                    turn_open_at=200.0,
                )
            }
        ),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert [(s.session_key, s.reason) for s in survivors] == [
        ("claude_code:main:sid-1", CUT_OFF)
    ]


def test_a_delivery_that_died_in_the_same_moment_it_arrived_is_a_survivor():
    """`agentdeck#1839` lost a message outright: delivered, dead 35 ms later.

    No comparison of the two stamps can see that — they are equal to the clock's
    resolution and the deduction wants a strict `>`. The marker can.
    """
    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(
                    last_delivery_at=200.0,
                    last_result_at=200.035,
                    last_result_subtype="error_during_execution",
                    turn_open_at=200.0,
                )
            }
        ),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert [s.reason for s in survivors] == [CUT_OFF]


def test_a_chat_that_finished_its_turn_before_the_death_is_left_alone():
    """The marker is closed, so the result really was the end of the turn."""
    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(
                    last_delivery_at=200.0,
                    last_result_at=250.0,
                    last_result_subtype="success",
                    turn_open_at=0.0,
                )
            }
        ),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert survivors == []


def test_an_open_turn_that_survived_the_restart_is_not_revived():
    """A live worker still working is skipped, marker or no marker."""
    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(
                    live=True,
                    turn_active=True,
                    effective_activity=True,
                    turn_open_at=200.0,
                )
            }
        ),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert survivors == []


def test_an_open_turn_older_than_the_ttl_is_dropped():
    survivors = find_survivors(
        _snapshots(**{"issue-1": _worker(turn_open_at=200.0)}),
        now=200.0 + TTL_S + 1,
        ttl_s=TTL_S,
    )

    assert survivors == []


def test_a_continuation_turn_is_dated_by_its_marker_not_by_the_old_delivery():
    """A background task finishing opens a turn with no delivery of ours.

    The survivor's date is what the loop guard reads, so dating it from the last
    delivery would make every such chat look like a chat we had just revived.
    """
    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(
                    last_delivery_at=100.0,
                    last_result_at=150.0,
                    turn_open_at=260.0,
                )
            }
        ),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert [s.last_delivery_at for s in survivors] == [260.0]


# --- reviving once, then stopping ------------------------------------------


def _survivor(reason=CUT_OFF, last_delivery_at=200.0, background_tasks=frozenset()):
    return Survivor(
        session_key="claude_code:main:sid-1",
        provider_id="claude_code",
        label="main",
        worker_key="issue-1",
        reason=reason,
        last_delivery_at=last_delivery_at,
        background_tasks=background_tasks,
    )


def _record(delivered_at=210.0, background_tasks=frozenset()):
    return RevivalRecord(
        session_key="claude_code:main:sid-1",
        delivery_id="revive:claude_code:main:sid-1:200",
        delivered_at=delivered_at,
        boot_stamp=200.0,
        background_tasks=background_tasks,
    )


def test_a_chat_killed_on_its_own_revival_is_not_revived_again():
    assert revival_looped(_survivor(last_delivery_at=209.0), _record()) is True


def test_a_chat_that_was_given_new_work_after_its_revival_is_revived_again():
    """It died on the boss's turn, not on ours, so the loop guard must not fire."""
    assert revival_looped(_survivor(last_delivery_at=900.0), _record()) is False


def test_a_revival_turn_cut_off_by_a_planned_restart_is_revived_again():
    """A deploy is not the chat's doing: a question re-asked an hour ago must come back (#2965)."""
    survivor = _survivor(last_delivery_at=209.0)

    assert revival_looped(survivor, _record(), previous_exit_clean=True) is False
    assert revival_looped(survivor, _record(), previous_exit_clean=False) is True
    assert revival_looped(survivor, _record(), previous_exit_clean=None) is True


def test_a_planned_restart_does_not_revive_an_already_reported_background_task():
    survivor = _survivor(reason=BACKGROUND, background_tasks=frozenset({"task-9"}))
    previous = _record(background_tasks=frozenset({"task-9"}))

    assert revival_looped(survivor, previous, previous_exit_clean=True) is True


def test_a_first_time_survivor_is_never_looped():
    assert revival_looped(_survivor(), None) is False


def test_a_background_task_we_already_reported_does_not_justify_a_second_revival():
    survivor = _survivor(reason=BACKGROUND, background_tasks=frozenset({"task-9"}))

    assert revival_looped(survivor, _record(background_tasks=frozenset({"task-9"}))) is True


def test_a_new_background_task_does_justify_another_revival():
    survivor = _survivor(reason=BACKGROUND, background_tasks=frozenset({"task-9", "task-10"}))

    assert revival_looped(survivor, _record(background_tasks=frozenset({"task-9"}))) is False


# --- the durable files -----------------------------------------------------


def test_the_exit_marker_tells_a_crash_from_a_clean_stop(tmp_path):
    marker = RuntimeExitMarker(tmp_path / "exit.json")

    assert marker.claim() is None  # first ever boot: nothing to read
    marker.mark_clean()
    assert marker.claim() is True  # the previous runtime stopped gracefully

    # ...and this boot claimed it without marking clean, which is what an OOM
    # kill leaves behind: the lifespan teardown never runs.
    assert marker.claim() is False


def test_a_damaged_exit_marker_reads_as_unknown(tmp_path):
    path = tmp_path / "exit.json"
    path.write_text("{not json")
    assert RuntimeExitMarker(path).claim() is None


def test_the_ledger_round_trips_and_drops_rows_past_the_ttl(tmp_path):
    ledger = RevivalLedger(tmp_path / "revivals.json")
    ledger.record(_record(delivered_at=100.0), now=100.0, ttl_s=TTL_S)
    ledger.record(
        RevivalRecord(
            session_key="claude_code:main:sid-2",
            delivery_id="revive:claude_code:main:sid-2:200",
            delivered_at=100.0 + TTL_S + 10,
            boot_stamp=200.0,
        ),
        now=100.0 + TTL_S + 10,
        ttl_s=TTL_S,
    )

    assert sorted(ledger.load()) == ["claude_code:main:sid-2"]


def test_the_durable_files_sit_beside_the_registry_they_share_isolation_with(tmp_path):
    marker, ledger = revival_paths(tmp_path / "restart-nudge.json")

    assert marker == tmp_path / "restart-nudge-exit.json"
    assert ledger == tmp_path / "restart-nudge-revivals.json"


# --- what the chat is told -------------------------------------------------


def test_the_crash_message_says_the_interruption_was_not_the_boss():
    message = revival_message(CUT_OFF, previous_exit_clean=False)

    assert "killed" in message
    assert "none of it was the boss" in message
    assert "ask it again" in message


def test_a_clean_restart_says_restarted_rather_than_killed():
    message = revival_message(CUT_OFF, previous_exit_clean=True)

    assert "restarted" in message
    assert "killed" not in message


def test_an_unknown_previous_exit_is_reported_as_a_death():
    """We cannot prove it was clean, and claiming so would vouch for a transcript
    we have no reason to trust."""
    assert "killed" in revival_message(CUT_OFF, previous_exit_clean=None)


def test_a_chat_waiting_on_background_work_is_told_that_work_is_gone():
    message = revival_message(BACKGROUND, previous_exit_clean=False)

    assert "died with the runtime" in message
    assert "never coming" in message


# --- the chats with no worker record at all --------------------------------


def _candidate(**overrides):
    candidate = {
        "session_key": "codex:main:thread-1",
        "provider_id": "codex",
        "label": "main",
        "mid_turn": True,
        "live": False,
        "last_activity": 250.0,
    }
    candidate.update(overrides)
    return SessionCandidate(**candidate)


def test_a_dashboard_chat_cut_off_mid_turn_is_a_survivor():
    survivors = find_session_survivors([_candidate()], now=300.0, ttl_s=TTL_S)

    assert [(s.session_key, s.reason, s.worker_key) for s in survivors] == [
        ("codex:main:thread-1", SESSION_CUT_OFF, "")
    ]


def test_a_dashboard_chat_that_was_idle_at_the_crash_is_left_alone():
    assert find_session_survivors(
        [_candidate(mid_turn=False)], now=300.0, ttl_s=TTL_S
    ) == []


def test_a_chat_still_running_on_its_own_backend_is_not_revived():
    """A Codex thread outlives the runtime, turn intact. Nothing was cut off."""
    assert find_session_survivors([_candidate(live=True)], now=300.0, ttl_s=TTL_S) == []


def test_a_chat_a_keyed_worker_already_covers_is_not_judged_twice():
    assert (
        find_session_survivors(
            [_candidate()],
            now=300.0,
            ttl_s=TTL_S,
            exclude={"codex:main:thread-1"},
        )
        == []
    )


def test_a_dashboard_chat_that_stopped_long_ago_is_dropped():
    assert find_session_survivors([_candidate()], now=1e6, ttl_s=TTL_S) == []


def test_a_dashboard_chat_is_revived_once_and_then_left_alone():
    """No result timestamp exists here, so one revival per ledger window is it."""
    survivor = _survivor(reason=SESSION_CUT_OFF, last_delivery_at=400.0)

    assert revival_looped(survivor, None) is False
    assert revival_looped(survivor, _record(delivered_at=210.0)) is True


# --- the boot pass end to end ----------------------------------------------


def _runtime(tmp_path):
    config = AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, "state_dir": str(tmp_path)},
            "keyed_workers": {"state_dir": str(tmp_path)},
            "restart_nudge": {
                "file": str(tmp_path / "restart-nudge.json"),
                "ttl_s": TTL_S,
            },
            "history": {"db_path": str(tmp_path / "budgets.db")},
            "accounts": [
                AccountConfig(
                    provider="claude_code", label="main", config_dir=str(tmp_path)
                ).model_dump()
            ],
        }
    )
    return create_runtime_app(config).state


def _host(workers, *, accepted=True, background=frozenset()):
    host = MagicMock()
    host.snapshot = MagicMock(return_value={"workers": workers})
    host.outstanding_background_tasks = MagicMock(return_value=background)
    host.find_key_by_session = MagicMock(return_value="issue-1")
    host.worker_role = MagicMock(return_value=None)
    host.deliver = AsyncMock(
        return_value=DeliverResult(
            accepted,
            "revived" if accepted else "rejected",
            reason=None if accepted else "at capacity",
            session_id="sid-1",
        )
    )
    return host


async def test_boot_revives_a_cut_off_chat_that_registered_nothing(tmp_path):
    state = _runtime(tmp_path)
    now = time.time()
    host = _host({"issue-1": _worker(last_delivery_at=now, last_result_at=now - 60)})
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.begin_boot()

    await nudges.deliver_all()

    host.deliver.assert_awaited_once()
    args, kwargs = host.deliver.await_args
    assert args[0] == "issue-1"
    assert "was killed while your turn was running" in args[1]
    assert kwargs["delivery_id"].startswith("revive:claude_code:main:sid-1:")
    assert sorted(nudges.ledger.load()) == ["claude_code:main:sid-1"]


async def test_the_revival_quotes_messages_the_dead_turn_never_read(tmp_path):
    """#2898: four messages sat in the CLI's queue behind a long tool call, and
    the restart took them with it. They ride in the one revival write."""
    state = _runtime(tmp_path)
    now = time.time()
    host = _host({"issue-1": _worker(last_delivery_at=now, last_result_at=now - 60)})
    host.unread_messages = MagicMock(
        return_value=[{"delivery_id": "m1", "text": "please also check X", "at": now}]
    )
    host.unread_dropped = MagicMock(return_value=2)
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.begin_boot()

    await nudges.deliver_all()

    host.unread_messages.assert_called_once_with("issue-1")
    host.deliver.assert_awaited_once()
    message = host.deliver.await_args.args[1]
    assert "1 message reached you" in message
    assert message.rstrip().endswith("--- End of the lost messages ---")
    assert "please also check X" in message
    assert "Up to 2 older messages" in message


async def test_an_unreadable_queue_does_not_stop_the_revival(tmp_path):
    state = _runtime(tmp_path)
    now = time.time()
    host = _host({"issue-1": _worker(last_delivery_at=now, last_result_at=now - 60)})
    host.unread_messages = MagicMock(side_effect=OSError("gone"))
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.begin_boot()

    await nudges.deliver_all()

    host.deliver.assert_awaited_once()
    assert "reached you" not in host.deliver.await_args.args[1]


async def test_a_registered_chat_that_is_also_a_survivor_gets_one_message(tmp_path):
    """The acceptance criterion the two sources exist to keep: no double delivery."""
    state = _runtime(tmp_path)
    now = time.time()
    host = _host({"issue-1": _worker(last_delivery_at=now, last_result_at=now - 60)})
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.begin_boot()
    nudges.registry.register("claude_code:main:sid-1")

    await nudges.deliver_all()

    host.deliver.assert_awaited_once()
    assert host.deliver.await_args.args[1] == NUDGE_MESSAGE


async def test_a_chat_killed_again_on_its_revival_is_flagged_rather_than_revived(tmp_path):
    state = _runtime(tmp_path)
    now = time.time()
    host = _host({"issue-1": _worker(last_delivery_at=now - 5, last_result_at=now - 60)})
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.begin_boot()
    nudges.ledger.record(
        RevivalRecord(
            session_key="claude_code:main:sid-1",
            delivery_id="revive:claude_code:main:sid-1:1",
            delivered_at=now,
            boot_stamp=now,
        ),
        now=now,
        ttl_s=TTL_S,
    )

    await nudges.deliver_all()

    host.deliver.assert_not_awaited()
    reports = state.budget_db.load_session_attention_reports()
    assert reports["claude_code:main:sid-1"].state == "waiting"
    assert reports["claude_code:main:sid-1"].detail == LOOP_ATTENTION_DETAIL


async def test_a_revival_turn_cut_off_by_a_planned_restart_is_revived_again_at_boot(tmp_path):
    """The 2026-10-13 deploys left a dozen chats' questions declined and nobody woke them."""
    state = _runtime(tmp_path)
    now = time.time()
    host = _host({"issue-1": _worker(last_delivery_at=now - 5, last_result_at=now - 60)})
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.exit_marker.mark_clean()  # the previous runtime stopped gracefully
    nudges.begin_boot()
    nudges.ledger.record(
        RevivalRecord(
            session_key="claude_code:main:sid-1",
            delivery_id="revive:claude_code:main:sid-1:1",
            delivered_at=now,
            boot_stamp=now,
        ),
        now=now,
        ttl_s=TTL_S,
    )

    await nudges.deliver_all()

    host.deliver.assert_awaited_once()
    assert "claude_code:main:sid-1" not in state.budget_db.load_session_attention_reports()


async def test_a_refused_revival_is_not_remembered_as_delivered(tmp_path):
    """A worker at capacity gets another go next boot rather than being written off."""
    state = _runtime(tmp_path)
    now = time.time()
    host = _host(
        {"issue-1": _worker(last_delivery_at=now, last_result_at=now - 60)}, accepted=False
    )
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.begin_boot()

    await nudges.deliver_all()

    host.deliver.assert_awaited_once()
    assert nudges.ledger.load() == {}


# --- a revival the account turned away at its cap (#3009) -------------------


def _full():
    return DeliverResult(False, "rejected", reason="at_capacity")


def _landed():
    return DeliverResult(True, "revived", session_id="sid-1")


def _cut_off_chat(tmp_path, *results):
    state = _runtime(tmp_path)
    boot = state.restart_nudges._boot_stamp
    # Cut off before this boot, so nothing has reached it since.
    host = _host({"issue-1": _worker(last_delivery_at=boot - 30, last_result_at=boot - 60)})
    host.deliver = AsyncMock(side_effect=list(results))
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.retry_interval_s = 0
    nudges.begin_boot()
    return state, host, nudges


async def test_a_revival_refused_at_capacity_is_retried_until_a_slot_frees(tmp_path):
    """2026-10-13: two revivals were refused at_capacity and nothing tried again."""
    _, host, nudges = _cut_off_chat(tmp_path, _full(), _full(), _landed())

    await nudges.deliver_all()

    calls = host.deliver.await_args_list
    assert len(calls) == 3
    assert len({call.kwargs["delivery_id"] for call in calls}) == 1  # one message, at most
    assert len({call.args[1] for call in calls}) == 1
    assert all(call.kwargs["automated"] is True for call in calls)
    assert sorted(nudges.ledger.load()) == ["claude_code:main:sid-1"]


def _wakes_after_refusal(host, nudges, *rows):
    """The first delivery is refused; then another message reaches the chat.

    Each later snapshot read returns the next of ``rows``, the last one for good.
    """
    first = host.deliver.side_effect
    base = dict(host.snapshot.return_value["workers"]["issue-1"])
    states = [{**base, **row} for row in rows]

    def snapshot():
        state = states.pop(0) if len(states) > 1 else states[0]
        return {"workers": {"issue-1": state}}

    async def refuse_then_another_writes(*args, **kwargs):
        result = next(first)
        host.snapshot.side_effect = snapshot
        return result

    host.deliver = AsyncMock(side_effect=refuse_then_another_writes)


@pytest.mark.parametrize(
    "moved_on",
    [{"live": True}, {"last_delivery_at": "after-boot"}],
    ids=["worker running again", "another message landed"],
)
async def test_a_queued_revival_is_sent_late_once_the_chat_has_moved_on(tmp_path, moved_on):
    """#3073: dropping it left five Works never told why their turn stopped."""
    _, host, nudges = _cut_off_chat(tmp_path, _full(), _landed())
    row = dict(moved_on)
    if row.get("last_delivery_at") == "after-boot":
        row["last_delivery_at"] = nudges._boot_stamp + 5
    _wakes_after_refusal(host, nudges, row)

    await nudges.deliver_all()

    calls = host.deliver.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["delivery_id"] == calls[1].kwargs["delivery_id"]
    late = calls[1].args[1]
    assert late != calls[0].args[1]
    # Not the boot text, which would call the newer message part of the restart (#3009).
    assert "Everything you were sent after the restart is real" in late
    assert "none of it was the boss" not in late
    assert sorted(nudges.ledger.load()) == ["claude_code:main:sid-1"]


async def test_a_late_revival_waits_for_the_newer_turn_to_end(tmp_path):
    _, host, nudges = _cut_off_chat(tmp_path, _full(), _landed())
    after = nudges._boot_stamp + 5
    busy = {"live": True, "turn_active": True, "last_delivery_at": after}
    _wakes_after_refusal(host, nudges, busy, busy, {**busy, "turn_active": False})

    await nudges.deliver_all()

    assert host.deliver.await_count == 2
    assert host.snapshot.side_effect is not None
    assert sorted(nudges.ledger.load()) == ["claude_code:main:sid-1"]


async def test_a_late_revival_still_carries_the_messages_the_death_lost(tmp_path):
    state, host, nudges = _cut_off_chat(tmp_path, _full(), _landed())
    nudges._unread_messages = lambda survivor: [{"text": "please also fix the docs"}]
    _wakes_after_refusal(host, nudges, {"last_delivery_at": nudges._boot_stamp + 5})

    await nudges.deliver_all()

    assert "please also fix the docs" in host.deliver.await_args_list[1].args[1]


async def test_a_queued_restart_nudge_is_dropped_once_the_chat_has_moved_on(tmp_path):
    """A nudge only asks the chat to carry on, which the newer message already did."""
    state = _runtime(tmp_path)
    boot = state.restart_nudges._boot_stamp
    host = _host({"issue-1": _worker(last_delivery_at=boot - 60, last_result_at=boot - 30)})
    host.deliver = AsyncMock(side_effect=iter([_full(), _landed()]))
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.retry_interval_s = 0
    nudges.begin_boot()
    nudges.registry.register("claude_code:main:sid-1")
    # The newer message's turn has already finished, so it is no survivor either.
    _wakes_after_refusal(host, nudges, {"last_delivery_at": boot + 5, "last_result_at": boot + 6})

    await nudges.deliver_all()

    host.deliver.assert_awaited_once()


async def test_a_retry_refused_for_another_reason_is_not_tried_again(tmp_path):
    _, host, nudges = _cut_off_chat(
        tmp_path, _full(), DeliverResult(False, "rejected", reason="cwd_required")
    )

    await nudges.deliver_all()

    assert host.deliver.await_count == 2
    assert nudges.ledger.load() == {}


async def test_a_retry_that_may_have_written_is_never_sent_again(tmp_path):
    _, host, nudges = _cut_off_chat(
        tmp_path,
        _full(),
        DeliverResult(
            False, "rejected", reason="at_capacity", provider_write_state="unknown"
        ),
    )

    await nudges.deliver_all()

    assert host.deliver.await_count == 2


async def test_a_refused_revival_is_given_up_when_the_window_closes(tmp_path):
    _, host, nudges = _cut_off_chat(tmp_path, _full(), _landed())
    nudges._boot_stamp -= TTL_S

    await nudges.deliver_all()

    host.deliver.assert_awaited_once()
    assert nudges.ledger.load() == {}


async def test_a_registered_idle_chat_refused_at_capacity_gets_its_nudge_later(tmp_path):
    state = _runtime(tmp_path)
    now = time.time()
    host = _host({"issue-1": _worker(last_delivery_at=now - 60, last_result_at=now)})
    host.deliver = AsyncMock(side_effect=[_full(), _landed()])
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.retry_interval_s = 0
    nudges.begin_boot()
    nudges.registry.register("claude_code:main:sid-1")

    await nudges.deliver_all()

    messages = [call.args[1] for call in host.deliver.await_args_list]
    assert messages == [NUDGE_MESSAGE, NUDGE_MESSAGE]
    assert nudges.ledger.load() == {}  # a nudge, not a revival


async def test_a_registered_survivor_refused_at_capacity_is_retried_once_as_a_revival(
    tmp_path,
):
    state, host, nudges = _cut_off_chat(tmp_path, _full(), _full(), _landed())
    nudges.registry.register("claude_code:main:sid-1")

    await nudges.deliver_all()

    messages = [call.args[1] for call in host.deliver.await_args_list]
    assert messages[0] == NUDGE_MESSAGE
    assert "was killed while your turn was running" in messages[1]
    assert messages[2] == messages[1]
    assert sorted(nudges.ledger.load()) == ["claude_code:main:sid-1"]


async def test_a_chat_already_told_its_background_task_died_is_left_alone_unflagged(
    tmp_path, caplog
):
    """The 'cut off again on its own revival' lines after a planned restart were
    these: chats still waiting on a task they had already been told about."""
    state = _runtime(tmp_path)
    now = time.time()
    host = _host(
        {"issue-1": _worker(last_delivery_at=now - 600, last_result_at=now - 500)},
        background=frozenset({"b1"}),
    )
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.exit_marker.mark_clean()
    nudges.begin_boot()
    nudges.ledger.record(
        RevivalRecord(
            session_key="claude_code:main:sid-1",
            delivery_id="revive:claude_code:main:sid-1:1",
            delivered_at=now - 300,
            boot_stamp=now - 300,
            background_tasks=frozenset({"b1"}),
        ),
        now=now,
        ttl_s=TTL_S,
    )

    with caplog.at_level("INFO", logger="agentdeck.runtime"):
        await nudges.deliver_all()

    host.deliver.assert_not_awaited()
    assert "already told its background tasks died" in caplog.text
    assert "claude_code:main:sid-1" not in state.budget_db.load_session_attention_reports()


async def test_an_idle_chat_is_not_touched_on_boot(tmp_path):
    state = _runtime(tmp_path)
    now = time.time()
    host = _host({"issue-1": _worker(last_delivery_at=now - 60, last_result_at=now)})
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.begin_boot()

    await nudges.deliver_all()

    host.deliver.assert_not_awaited()


async def test_a_clean_shutdown_is_recorded_for_the_next_boot(tmp_path):
    state = _runtime(tmp_path)
    nudges = state.restart_nudges
    nudges.begin_boot()
    nudges.end_boot()

    assert nudges.exit_marker.claim() is True


async def test_a_stale_registration_still_lets_the_survivor_pass_revive(tmp_path):
    """A dropped entry wrote nothing, so it must not stand in for a revival."""
    state = _runtime(tmp_path)
    now = time.time()
    host = _host({"issue-1": _worker(last_delivery_at=now, last_result_at=now - 60)})
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.begin_boot()
    nudges.registry.register("claude_code:main:sid-1", now=now - TTL_S * 2)

    await nudges.deliver_all()

    host.deliver.assert_awaited_once()
    assert "was killed while your turn was running" in host.deliver.await_args.args[1]


async def test_a_refused_registration_still_lets_the_survivor_pass_revive(tmp_path):
    """The refusal proves no write happened, so the chat is still owed a revival."""
    state = _runtime(tmp_path)
    now = time.time()
    host = _host(
        {"issue-1": _worker(last_delivery_at=now, last_result_at=now - 60)},
        accepted=False,
    )
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.begin_boot()
    nudges.registry.register("claude_code:main:sid-1")

    await nudges.deliver_all()

    messages = [call.args[1] for call in host.deliver.await_args_list]
    assert messages[0] == NUDGE_MESSAGE
    assert "was killed while your turn was running" in messages[1]


async def test_a_registration_that_may_have_been_written_blocks_the_revival(tmp_path):
    """An uncertain write is not a refusal: at-most-once wins over a second try."""
    state = _runtime(tmp_path)
    now = time.time()
    host = _host({"issue-1": _worker(last_delivery_at=now, last_result_at=now - 60)})
    host.deliver = AsyncMock(
        return_value=DeliverResult(
            False,
            "rejected",
            reason="the provider stopped answering",
            provider_write_state="unknown",
        )
    )
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.begin_boot()
    nudges.registry.register("claude_code:main:sid-1")

    await nudges.deliver_all()

    host.deliver.assert_awaited_once()
    assert host.deliver.await_args.args[1] == NUDGE_MESSAGE
    assert nudges.ledger.load() == {}


# --- the chats the dashboard started, end to end ----------------------------


def _session(session_id, *, thinking=True, stalled=False, last_activity=None):
    return SimpleNamespace(
        session_id=session_id,
        thinking=thinking,
        stalled=stalled,
        last_activity=datetime.fromtimestamp(
            time.time() if last_activity is None else last_activity, UTC
        ),
    )


def _session_runtime(sessions, *, live=(), accepted=True):
    """A stand-in for CodexRuntime: accounts, live keys and a fenced write."""
    runtime = MagicMock()
    runtime.accounts = {"main": MagicMock()}
    runtime.active_session_keys = MagicMock(return_value=set(live))
    runtime.nudge_session = AsyncMock(
        return_value=DeliverResult(
            accepted,
            "queued" if accepted else "rejected",
            reason=None if accepted else "unknown Codex account",
        )
    )
    provider = MagicMock()
    provider.scan_sessions = AsyncMock(return_value=list(sessions))
    return runtime, provider


def _unkeyed_worker_runtime():
    """The provider's keyed side, holding no worker for these sessions at all."""
    host = MagicMock()
    host.snapshot = MagicMock(return_value={"workers": {}})
    host.find_key_by_session = MagicMock(return_value=None)
    runtime = MagicMock()
    runtime.host = MagicMock(return_value=host)
    runtime.all_hosts = MagicMock(return_value={"main": host})
    return runtime


def _install(nudges, runtime, provider, monkeypatch):
    nudges._session_runtimes = {"codex": runtime}
    nudges._workers = {**nudges._workers, "codex": _unkeyed_worker_runtime()}
    monkeypatch.setitem(PROVIDERS, "codex", provider)


def _own(state, session_key):
    state.budget_db.record_session_token(f"token-for-{session_key}", session_key)


async def test_boot_revives_a_dashboard_chat_with_no_worker_key(tmp_path, monkeypatch):
    state = _runtime(tmp_path)
    nudges = state.restart_nudges
    runtime, provider = _session_runtime([_session("thread-1")])
    _install(nudges, runtime, provider, monkeypatch)
    _own(state, "codex:main:thread-1")
    nudges.begin_boot()

    await nudges.deliver_all()

    runtime.nudge_session.assert_awaited_once()
    label, session_id, delivery_id, message = runtime.nudge_session.await_args.args
    assert (label, session_id) == ("main", "thread-1")
    assert delivery_id.startswith("revive:codex:main:thread-1:")
    assert "was killed while your turn was running" in message
    assert sorted(nudges.ledger.load()) == ["codex:main:thread-1"]


async def test_a_dashboard_chat_the_deck_does_not_own_is_never_written_to(
    tmp_path, monkeypatch
):
    """Somebody's own terminal chat is not ours to put a message into."""
    state = _runtime(tmp_path)
    nudges = state.restart_nudges
    runtime, provider = _session_runtime([_session("thread-1")])
    _install(nudges, runtime, provider, monkeypatch)
    nudges.begin_boot()

    await nudges.deliver_all()

    runtime.nudge_session.assert_not_awaited()


async def test_an_idle_dashboard_chat_is_not_touched_on_boot(tmp_path, monkeypatch):
    state = _runtime(tmp_path)
    nudges = state.restart_nudges
    runtime, provider = _session_runtime([_session("thread-1", thinking=False)])
    _install(nudges, runtime, provider, monkeypatch)
    _own(state, "codex:main:thread-1")
    nudges.begin_boot()

    await nudges.deliver_all()

    runtime.nudge_session.assert_not_awaited()


async def test_a_dashboard_chat_killed_on_its_revival_is_flagged_not_revived(
    tmp_path, monkeypatch
):
    state = _runtime(tmp_path)
    nudges = state.restart_nudges
    now = time.time()
    runtime, provider = _session_runtime([_session("thread-1")])
    _install(nudges, runtime, provider, monkeypatch)
    _own(state, "codex:main:thread-1")
    nudges.begin_boot()
    nudges.ledger.record(
        RevivalRecord(
            session_key="codex:main:thread-1",
            delivery_id="revive:codex:main:thread-1:1",
            delivered_at=now,
            boot_stamp=now,
        ),
        now=now,
        ttl_s=TTL_S,
    )

    await nudges.deliver_all()

    runtime.nudge_session.assert_not_awaited()
    reports = state.budget_db.load_session_attention_reports()
    assert reports["codex:main:thread-1"].state == "waiting"


async def test_a_registered_dashboard_chat_is_not_also_revived(tmp_path, monkeypatch):
    state = _runtime(tmp_path)
    nudges = state.restart_nudges
    runtime, provider = _session_runtime([_session("thread-1")])
    _install(nudges, runtime, provider, monkeypatch)
    _own(state, "codex:main:thread-1")
    nudges.begin_boot()
    nudges.registry.register("codex:main:thread-1")

    await nudges.deliver_all()

    runtime.nudge_session.assert_awaited_once()
    assert runtime.nudge_session.await_args.args[3] == NUDGE_MESSAGE


def test_a_marker_on_a_live_worker_is_not_a_cut_off_turn():
    """The marker's claim is "this chat's process is gone and it owes an answer".

    A worker still running is making its own progress, whatever it last wrote.
    """
    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(
                    live=True,
                    turn_active=False,
                    effective_activity=False,
                    last_delivery_at=200.0,
                    last_result_at=250.0,
                    turn_open_at=200.0,
                )
            }
        ),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert survivors == []


def test_a_deliberately_ended_turn_is_not_revived_even_with_no_result():
    """A stop, a park or an acknowledged interrupt. `_terminate` escalates to
    SIGKILL, so the aborted result may never arrive and the deduction below would
    still read "delivered, never answered"."""
    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(
                    last_delivery_at=200.0,
                    last_result_at=100.0,
                    turn_open_at=0.0,
                    turn_closed_at=250.0,
                )
            }
        ),
        now=300.0,
        ttl_s=TTL_S,
        background_tasks=lambda *_: {"task-9"},
    )

    assert survivors == []


def test_a_message_delivered_after_the_cancellation_is_revived():
    """The cancellation speaks for the work it cancelled, not for what came next."""
    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(
                    last_delivery_at=260.0,
                    last_result_at=100.0,
                    turn_open_at=260.0,
                    turn_closed_at=250.0,
                )
            }
        ),
        now=300.0,
        ttl_s=TTL_S,
    )

    assert [s.reason for s in survivors] == [CUT_OFF]


def test_a_cancellation_does_not_suppress_a_later_turn_that_never_finished_arriving():
    """The open marker is stamped before the child write; the delivery stamp only
    after the write and the initialization wait both return. A message that died
    in that window therefore has an open turn and a delivery stamp still older
    than the cancellation — and the cancellation must not speak for it."""
    survivors = find_survivors(
        _snapshots(
            **{
                "issue-1": _worker(
                    last_delivery_at=100.0,
                    last_result_at=100.0,
                    turn_closed_at=200.0,
                    turn_open_at=300.0,
                )
            }
        ),
        now=350.0,
        ttl_s=TTL_S,
    )

    assert [s.reason for s in survivors] == [CUT_OFF]
    assert [s.last_delivery_at for s in survivors] == [300.0]


async def test_the_deck_is_told_which_revivals_are_still_owed(tmp_path):
    """#3008: the deck reads a cut-off turn as reviving only while the runtime owes it one.

    Every cut-off chat until the boot's scan has run; after it, only the one a full account
    turned away, while it is retried; nothing once it landed.
    """
    _, host, nudges = _cut_off_chat(tmp_path, _full(), _landed())
    assert nudges.revivals_due() == {"scanned": False, "due": []}
    seen = []
    results = iter(host.deliver.side_effect)

    async def deliver(*args, **kwargs):
        seen.append(nudges.revivals_due())
        return next(results)

    host.deliver = AsyncMock(side_effect=deliver)

    await nudges.deliver_all()

    assert seen == [
        {"scanned": False, "due": []},  # the scan's own attempt
        {"scanned": True, "due": ["claude_code:main:sid-1"]},  # its retry
    ]
    assert nudges.revivals_due() == {"scanned": True, "due": []}
