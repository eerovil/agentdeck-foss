"""Scheduled work tasks created and managed from GitHub issues (#2517)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from poller_engine_fakes import FakePort, run_wake, store_for
from test_poller_engine_github import FakeGh, adapter, ok

from agentdeck.poller_engine import scheduled_work, schedules
from agentdeck.poller_engine.ports import EffectKind
from agentdeck.poller_engine.schedules import Schedule, TaskIssue

# Friday 2 October 2026, 12:00 Helsinki (UTC+3). The Monday after is 5 October, ISO week 41.
NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
MONDAY_6 = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)  # 06:00 Helsinki

BODY = "When: every Monday 06:00\n\n## Prompt\n\nAudit the store's SEO and file fixes.\n"


def task(number=7, *, body=BODY, open_=True, author="eero", editor="", title="SEO audit"):
    return {
        f"store#{number}": TaskIssue(
            repo="store", number=number, title=title, body=body, open=open_,
            author=author, editor=editor,
        )
    }


def comments(port, number=7):
    return [
        str(effect.value)
        for effect in port.applied
        if effect.kind is EffectKind.COMMENT and effect.number == number
    ]


def wake(store, port, now, **kwargs):
    kwargs.setdefault("issue_repos", frozenset({"store"}))
    return run_wake(store, port, now=now, **kwargs)


# --- the When line ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("every Monday 06:00", Schedule(6, 0, weekdays=(0,))),
        ("every Monday at 6:00", Schedule(6, 0, weekdays=(0,))),
        ("Mondays 06.00", Schedule(6, 0, weekdays=(0,))),
        ("every day 07:30", Schedule(7, 30)),
        ("daily at 23:59", Schedule(23, 59)),
        ("every weekday 09:00", Schedule(9, 0, weekdays=(0, 1, 2, 3, 4))),
        ("every Tuesday and Friday 12:00", Schedule(12, 0, weekdays=(1, 4))),
        ("first day of the month 08:00", Schedule(8, 0, month_day=1)),
        ("day 15 of the month 08:00", Schedule(8, 0, month_day=15)),
        ("on the 31st of every month 08:00", Schedule(8, 0, month_day=31)),
        ("last day of the month 16:00", Schedule(16, 0, month_day=-1)),
    ],
)
def test_plain_words_are_understood(text, expected):
    assert schedules.parse_when(text) == expected


@pytest.mark.parametrize(
    "text", ["", "every Monday", "every Mondy 06:00", "every 2 weeks 06:00",
             "every Monday 06:00 and 18:00", "every month 08:00", "06:00"],
)
def test_a_when_line_that_cannot_be_understood_says_why(text):
    assert isinstance(schedules.parse_when(text), str)


def test_the_when_line_and_prompt_are_read_from_markdown():
    body = "Some intro.\n\n**When:** every day 07:30.\n\n### Prompt\nDo it.\n\n## Notes\nnope"
    assert schedules.when_line(body) == "every day 07:30"
    assert schedules.prompt_section(body) == "Do it."
    assert schedules.prompt_section("When: every day 07:30\nJust do it.") == "Just do it."
    assert schedules.when_line("Whenever you like") is None


def test_the_next_run_is_helsinki_wall_clock_across_daylight_saving():
    weekly = Schedule(6, 0, weekdays=(0,))
    assert weekly.next_after(NOW) == MONDAY_6
    # Clocks go back on Sunday 25 October 2026: Monday 26 October 06:00 is UTC+2.
    assert weekly.next_after(datetime(2026, 10, 20, tzinfo=UTC)) == datetime(
        2026, 10, 26, 4, 0, tzinfo=UTC
    )
    # Strictly after: the run time itself asks for the following week.
    assert weekly.next_after(MONDAY_6) == MONDAY_6 + timedelta(days=7)
    # A month without day 31 runs on its last day.
    assert Schedule(8, 0, month_day=31).next_after(datetime(2026, 11, 1, tzinfo=UTC)) == (
        datetime(2026, 11, 30, 6, 0, tzinfo=UTC)
    )


def test_a_run_card_is_titled_by_its_week_day_or_month():
    assert schedules.run_title("SEO audit", Schedule(6, 0, weekdays=(0,)), MONDAY_6) == (
        "SEO audit — week 41"
    )
    assert schedules.run_title("Digest", Schedule(7, 30), MONDAY_6) == "Digest — 2026-10-05"
    assert schedules.run_title("Report", Schedule(8, 0, month_day=1), MONDAY_6) == (
        "Report — October 2026"
    )


# --- the wake ---


def test_a_new_task_is_announced_listed_and_pinned_without_running(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task())
    port.list_opened = [90]

    wake(store, port, NOW)

    [note] = comments(port)
    assert "every Monday at 06:00" in note
    assert "Monday 5 October 2026 at 06:00" in note
    assert port.opened_calls == []
    [listing] = port.list_opened_calls
    assert listing["title"] == "Scheduled tasks"
    assert "| #7 SEO audit | every Monday at 06:00 |" in listing["body"]
    assert any(e.kind is EffectKind.PIN_ISSUE and e.number == 90 for e in port.applied)
    state = store.read()
    assert state["scheduled_tasks"]["store#7"]["next"] == MONDAY_6.isoformat()
    assert state["scheduled_lists"]["store"]["number"] == 90


def test_a_board_without_tasks_writes_nothing(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort()

    wake(store, port, NOW)

    state = store.read()
    assert "scheduled_tasks" not in state and "scheduled_lists" not in state
    assert port.list_opened_calls == [] and port.applied == []


def test_the_due_run_files_exactly_one_labelled_card(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task(), opened=[40])
    port.list_opened = [90]
    wake(store, port, NOW)

    wake(store, port, MONDAY_6 - timedelta(seconds=30))
    assert port.opened_calls == []

    wake(store, port, MONDAY_6 + timedelta(seconds=5))
    wake(store, port, MONDAY_6 + timedelta(seconds=35))

    [card] = port.opened_calls
    assert card["title"] == "SEO audit — week 41"
    assert "Scheduled run of #7" in card["body"]
    assert "Audit the store's SEO and file fixes." in card["body"]
    assert "<!-- poller-scheduled-run: store#7 2026-10-05T03:00:00+00:00 -->" in card["body"]
    rec = store.read()["scheduled_tasks"]["store#7"]
    assert rec["last_run"]["number"] == 40
    assert rec["next"] == (MONDAY_6 + timedelta(days=7)).isoformat()
    # The list now names the run and the next one.
    bodies = [e.value for e in port.applied if e.kind is EffectKind.SET_BODY]
    assert "#40" in bodies[-1] and "Monday 12 October 2026" in bodies[-1]


def test_a_run_is_skipped_with_a_comment_while_the_previous_card_is_open(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task(), opened=[40, 41])
    port.list_opened = [90]
    wake(store, port, NOW)
    wake(store, port, MONDAY_6)
    port._ref_states["store#40"] = "OPEN"

    wake(store, port, MONDAY_6 + timedelta(days=7))

    assert len(port.opened_calls) == 1
    assert "Skipped the run due Monday 12 October 2026 at 06:00" in comments(port)[-1]
    assert "#40 is still open" in comments(port)[-1]
    assert store.read()["scheduled_tasks"]["store#7"]["next"] == (
        MONDAY_6 + timedelta(days=14)
    ).isoformat()

    port._ref_states["store#40"] = "CLOSED"
    wake(store, port, MONDAY_6 + timedelta(days=14))
    assert len(port.opened_calls) == 2


def test_after_downtime_only_one_missed_run_is_filed(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task(), opened=[40, 41, 42])
    port.list_opened = [90]
    wake(store, port, NOW)

    later = MONDAY_6 + timedelta(days=22)  # three Mondays missed
    wake(store, port, later)
    wake(store, port, later + timedelta(seconds=30))

    assert len(port.opened_calls) == 1
    # Monday 2 November, 06:00 Helsinki: winter time by then, so UTC+2.
    assert store.read()["scheduled_tasks"]["store#7"]["next"] == (
        datetime(2026, 11, 2, 4, 0, tzinfo=UTC).isoformat()
    )


def test_an_interrupted_filing_looks_before_it_files_again(tmp_path):
    store = store_for(tmp_path, {})
    marker = "<!-- poller-scheduled-run: store#7 2026-10-05T03:00:00+00:00 -->"
    port = FakePort(scheduled=task(), opened=[40])
    port.list_opened = [90]
    wake(store, port, NOW)
    state = store.read()
    state["scheduled_tasks"]["store#7"]["opening"] = {
        "occurrence": MONDAY_6.isoformat(), "at": MONDAY_6.isoformat(),
    }
    store = store_for(tmp_path / "again", state)
    port._marked[marker] = 39

    wake(store, port, MONDAY_6 + timedelta(seconds=30))

    assert port.opened_calls == []
    rec = store.read()["scheduled_tasks"]["store#7"]
    assert rec["last_run"]["number"] == 39 and "opening" not in rec


def test_an_inconclusive_lookup_never_files_a_second_card(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task(), opened=[40])
    port.list_opened = [90]
    wake(store, port, NOW)
    port.markers_readable = False

    wake(store, port, MONDAY_6)

    assert port.opened_calls == []
    assert "opening" in store.read()["scheduled_tasks"]["store#7"]


def test_editing_closing_and_reopening_take_effect_and_are_announced(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task())
    port.list_opened = [90]
    wake(store, port, NOW)

    port.scheduled = task(body="When: every day 07:30\n\nDo the digest.")
    wake(store, port, NOW + timedelta(minutes=1))
    assert "Saturday 3 October 2026 at 07:30" in comments(port)[-1]

    port.scheduled = task(body="When: every day 07:30\n\nDo the digest.", open_=False)
    wake(store, port, NOW + timedelta(minutes=2))
    assert "stopped" in comments(port)[-1]
    assert not store.read()["scheduled_tasks"]["store#7"]["active"]
    wake(store, port, datetime(2026, 10, 3, 5, 0, tzinfo=UTC))
    assert port.opened_calls == []

    port.scheduled = task(body="When: every day 07:30\n\nDo the digest.")
    wake(store, port, datetime(2026, 10, 3, 5, 0, tzinfo=UTC))
    assert "Sunday 4 October 2026 at 07:30" in comments(port)[-1]


def test_editing_only_the_prompt_keeps_the_promised_run(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task())
    port.list_opened = [90]
    wake(store, port, NOW)

    port.scheduled = task(body=BODY + "\nAlso check the sitemap.")
    wake(store, port, NOW + timedelta(minutes=1))

    rec = store.read()["scheduled_tasks"]["store#7"]
    assert rec["next"] == MONDAY_6.isoformat()
    assert "Also check the sitemap." in rec["prompt"]
    assert len(comments(port)) == 2


def test_an_unreadable_when_line_is_reported_and_never_runs(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task(body="When: every Mondy 06:00\n\nDo it."))
    port.list_opened = [90]

    wake(store, port, NOW)
    wake(store, port, MONDAY_6 + timedelta(days=1))

    assert "I do not understand `mondy`" in comments(port)[0]
    assert port.opened_calls == []


def test_only_people_who_may_start_work_may_schedule_it(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(
        scheduled=task(author="eero", editor="mallory"), permissions={"mallory": "read"}
    )
    port.list_opened = [90]

    wake(store, port, NOW)
    wake(store, port, MONDAY_6)

    assert "`mallory` may not start work" in comments(port)[0]
    assert port.opened_calls == []


def test_the_engines_own_account_may_write_a_task_and_other_apps_may_not(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled={
        **task(7, author="claude-bot"),
        **task(8, author="some-app[bot]"),
    })
    port.list_opened = [90]

    wake(store, port, NOW)

    assert "Next run" in comments(port, 7)[0]
    assert "may not start work" in comments(port, 8)[0]
    assert port.permission_calls == []


def test_uncertain_authorization_waits_rather_than_deciding(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task(author="someone"), permissions={"someone": None})

    wake(store, port, NOW)

    assert comments(port) == []
    assert "store#7" not in (store.read().get("scheduled_tasks") or {})


def test_an_unreadable_sweep_keeps_every_task(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task())
    port.list_opened = [90]
    wake(store, port, NOW)
    port.scheduled = None

    wake(store, port, NOW + timedelta(minutes=1))

    assert store.read()["scheduled_tasks"]["store#7"]["active"]


def test_an_unreadable_sweep_files_no_new_run_from_cached_state(tmp_path):
    """The task may have been closed or edited while the sweep failed (#2519 review)."""
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task(), opened=[40])
    port.list_opened = [90]
    wake(store, port, NOW)
    port.scheduled = None

    wake(store, port, MONDAY_6 + timedelta(seconds=5))
    assert port.opened_calls == []
    assert store.read()["scheduled_tasks"]["store#7"]["next"] == MONDAY_6.isoformat()

    port.scheduled = task(open_=False)
    wake(store, port, MONDAY_6 + timedelta(seconds=35))
    assert port.opened_calls == []


def test_a_filing_already_begun_is_finished_even_when_the_sweep_fails(tmp_path):
    store = store_for(tmp_path, {})
    marker = "<!-- poller-scheduled-run: store#7 2026-10-05T03:00:00+00:00 -->"
    port = FakePort(scheduled=task())
    port.list_opened = [90]
    wake(store, port, NOW)
    state = store.read()
    state["scheduled_tasks"]["store#7"]["opening"] = {
        "occurrence": MONDAY_6.isoformat(), "at": MONDAY_6.isoformat(),
    }
    store = store_for(tmp_path / "again", state)
    port._marked[marker] = 39
    port.scheduled = None

    wake(store, port, MONDAY_6 + timedelta(seconds=30))

    assert store.read()["scheduled_tasks"]["store#7"]["last_run"]["number"] == 39


def test_a_task_whose_label_was_removed_is_forgotten(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task())
    port.list_opened = [90]
    wake(store, port, NOW)
    port.scheduled = {}

    wake(store, port, NOW + timedelta(minutes=1))
    wake(store, port, MONDAY_6)

    assert "store#7" not in (store.read().get("scheduled_tasks") or {})
    assert port.opened_calls == []
    assert "There are no scheduled tasks yet." in [
        e.value for e in port.applied if e.kind is EffectKind.SET_BODY
    ][-1]


def test_a_due_run_turns_a_quiet_wake_into_a_full_one(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task(), opened=[40])
    port.list_opened = [90]
    wake(store, port, NOW)
    store_state = store.read()
    store_state["last_live_wake"] = (MONDAY_6 - timedelta(seconds=60)).isoformat()
    store = store_for(tmp_path / "quiet", store_state)

    early = wake(store, port, MONDAY_6 - timedelta(seconds=30), webhook_quiet=True)
    assert early.quiet
    assert port.opened_calls == []

    due = wake(store, port, MONDAY_6 + timedelta(seconds=1), webhook_quiet=True)
    assert not due.quiet
    assert len(port.opened_calls) == 1


def test_scheduled_work_due_reads_only_active_tasks():
    state = {"scheduled_tasks": {
        "store#1": {"active": True, "next": NOW.isoformat()},
        "store#2": {"active": False, "next": (NOW - timedelta(days=1)).isoformat()},
    }}
    assert scheduled_work.due(state, NOW)
    assert not scheduled_work.due(state, NOW - timedelta(seconds=1))


# --- the GitHub read ---


def _sweep_answer(open_nodes, closed_nodes, *, has_next=False, label=True):
    return {"data": {
        "rateLimit": {"remaining": 4000, "resetAt": "2026-10-02T10:00:00Z", "cost": 1},
        "r0": {"label": {
            "open": {"pageInfo": {"hasNextPage": has_next}, "nodes": open_nodes},
            "closed": {"nodes": closed_nodes},
        } if label else None},
    }}


def _node(number, state="OPEN", editor=None):
    return {
        "number": number, "title": f"Task {number}", "body": BODY, "state": state,
        "author": {"login": "eero"}, "editor": {"login": editor} if editor else None,
    }


def test_the_adapter_reads_open_and_closed_tasks_in_one_request():
    api = FakeGh([(("graphql", "scheduled"), ok(_sweep_answer(
        [_node(7, editor="anna")], [_node(3, state="CLOSED")],
    )))])
    found = adapter(api=api).scheduled_tasks()

    assert set(found) == {"agentdeck#7", "agentdeck#3"}
    assert found["agentdeck#7"].writer == "anna" and found["agentdeck#7"].open
    assert found["agentdeck#3"].writer == "eero" and not found["agentdeck#3"].open
    assert sum(1 for call in api.calls if "graphql" in call) == 1


def test_the_adapter_reads_a_repository_without_the_label_as_no_tasks():
    api = FakeGh([(("graphql", "scheduled"), ok(_sweep_answer([], [], label=False)))])
    assert adapter(api=api).scheduled_tasks() == {}


@pytest.mark.parametrize(
    "answer",
    [_sweep_answer([_node(7)], [], has_next=True), {"errors": [{"message": "no"}]}, None],
)
def test_a_sweep_that_is_not_whole_answers_unreadable(answer):
    api = FakeGh([(("graphql", "scheduled"), ok(answer) if answer else (1, "", "boom"))])
    assert adapter(api=api).scheduled_tasks() is None


def test_the_list_issue_is_opened_without_the_ownership_label():
    url = "https://github.com/eerovil/agentdeck/issues/90\n"
    api = FakeGh([(("issue", "create"), (0, url, ""))])
    assert adapter(api=api).open_list_issue("agentdeck", "Scheduled tasks", "body") == 90
    [call] = api.calls
    assert "--label" not in call
    assert json.dumps(call)  # plain strings only


def _tracked_answer(node, *, errors=None):
    answer = _sweep_answer([], [])
    answer["data"]["t0"] = {"issue": node}
    if errors:
        answer["errors"] = errors
    return answer


def _labelled(node, *names):
    return {**node, "labels": {"nodes": [{"name": name} for name in names]}}


def test_a_tracked_task_outside_the_closed_page_is_read_directly():
    """The closed page is bounded; a tracked task missing from it proves nothing (#2519)."""
    api = FakeGh([(("graphql", "scheduled"), ok(_tracked_answer(
        _labelled(_node(7, state="CLOSED"), "Scheduled"),
    )))])

    found = adapter(api=api).scheduled_tasks(("agentdeck#7",))

    assert not found["agentdeck#7"].open
    [call] = [call for call in api.calls if "graphql" in call]
    assert any("issue(number:7)" in part for part in call)


@pytest.mark.parametrize(
    "answer",
    [
        _tracked_answer(_labelled(_node(7), "bug")),
        _tracked_answer(None, errors=[{"type": "NOT_FOUND", "path": ["t0", "issue"]}]),
    ],
)
def test_a_tracked_task_is_dropped_only_on_githubs_word(answer):
    # `gh` exits 1 whenever GraphQL reports an error, with the answer still on stdout.
    code = 1 if answer.get("errors") else 0
    api = FakeGh([(("graphql", "scheduled"), (code, json.dumps(answer), "gh: Could not"))])
    assert adapter(api=api).scheduled_tasks(("agentdeck#7",)) == {}


@pytest.mark.parametrize(
    "answer",
    [
        _tracked_answer(None, errors=[{"type": "FORBIDDEN", "path": ["t0", "issue"]}]),
        {"data": {**_sweep_answer([], [])["data"], "t0": None}},
    ],
)
def test_an_unreadable_tracked_task_makes_the_sweep_unreadable(answer):
    api = FakeGh([(("graphql", "scheduled"), ok(answer))])
    assert adapter(api=api).scheduled_tasks(("agentdeck#7",)) is None


def test_the_wake_asks_for_every_task_it_tracks(tmp_path):
    store = store_for(tmp_path, {})
    port = FakePort(scheduled=task())
    port.list_opened = [90]
    wake(store, port, NOW)
    assert port.scheduled_tracked == ()

    port.scheduled = task(open_=False)
    wake(store, port, NOW + timedelta(minutes=1))

    assert port.scheduled_tracked == ("store#7",)
    assert "stopped" in comments(port)[-1]
