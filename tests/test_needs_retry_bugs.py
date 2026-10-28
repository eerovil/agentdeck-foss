"""A Work that stops in Needs retry because AgentDeck broke files an AgentDeck issue (#2888).

The deck marks the bug (``work_attention``, tested in ``test_work_attention``), writes one
request per Work, cause and Run (``work_bugs``), and the agentdeck Poller folds requests into
one labelled issue per cause (``poller_engine.bug_reports``, ``cycle._file_bug_issues``).
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from poller_engine_fakes import NOW, FakePort, run_wake, store_for

from agentdeck import work_bugs
from agentdeck.poller_engine import bug_reports
from agentdeck.poller_engine.ports import EffectKind
from agentdeck.work_attention import NEEDS_ANSWER, NEEDS_RETRY, NOTES
from agentdeck.work_overview import WorkAttention

REPOS = frozenset({"agentdeck"})


def _row(work_id="github_issue:eerovil/bazaar#9", *, bug="worker_died", category=NEEDS_RETRY,
         stopped=timedelta(minutes=5)):
    card = SimpleNamespace(
        work_id=work_id, href="/works/x", source_url="https://github.com/x",
        attention=(WorkAttention("claude_code:main:w", "Worker", "waiting", "/chats/w",
                                 question="the secret is hunter2"),),
        verification=None,
    )
    return SimpleNamespace(
        card=card,
        status=SimpleNamespace(you="Press Retry"),
        attention=SimpleNamespace(category=category, bug=bug, reason="The worker stopped"),
        reason="",
        stopped_at=None if stopped is None else NOW - stopped,
    )


def _board(state_path, org="eerovil"):
    scope = SimpleNamespace(org=org, state_path=str(state_path))
    return SimpleNamespace(
        managing=lambda repository: (scope,) if repository == work_bugs.HOME else ()
    )


def _reports(state_path):
    return sorted(bug_reports.directory(state_path).glob("*.json"))


# --- the deck ----------------------------------------------------------------------------------


def test_a_settled_bug_is_written_once_for_the_instance_managing_agentdeck(tmp_path):
    state_path = tmp_path / "agentdeck" / "state.json"
    sent: set[str] = set()

    settled = {"github_issue:eerovil/bazaar#9": NOW - timedelta(minutes=3)}
    written = work_bugs.report([_row()], _board(state_path), now=NOW, sent=sent, since=settled)
    again = work_bugs.report([_row()], _board(state_path), now=NOW, sent=sent)

    assert (written, again) == (1, 0)
    (path,) = _reports(state_path)
    assert "hunter2" not in path.read_text()
    record = json.loads(path.read_text())
    assert (record["repo"], record["cause"], record["work_id"]) == (
        "agentdeck", "worker_died", "github_issue:eerovil/bazaar#9"
    )
    assert record["session_key"] == "claude_code:main:w"
    assert record["why"]["you"] == "Press Retry"
    assert record["why"]["chats"] == [{"session_key": "claude_code:main:w", "state": "waiting"}]


def test_nothing_is_written_for_a_flap_a_degraded_pass_or_an_ordinary_retry(tmp_path):
    state_path = tmp_path / "agentdeck" / "state.json"
    board = _board(state_path)

    assert work_bugs.report([_row(stopped=timedelta(seconds=30))], board, now=NOW) == 0
    assert work_bugs.report([_row(stopped=None)], board, now=NOW) == 0
    assert work_bugs.report([_row()], board, now=NOW, degraded=("cached_pulls",)) == 0
    assert work_bugs.report([_row(bug="")], board, now=NOW) == 0
    assert work_bugs.report([_row(category=NEEDS_ANSWER)], board, now=NOW) == 0
    # Stopped long ago, but in Needs retry for one pass only: a flap, as when a cache read
    # failed for every Work at once (#3553). Only the status log knows when it entered.
    entered = {"github_issue:eerovil/bazaar#9": NOW - timedelta(seconds=30)}
    assert work_bugs.report([_row(stopped=timedelta(days=2))], board, now=NOW,
                            since=entered) == 0
    assert work_bugs.report([_row()], board, now=NOW, since={}) == 0
    # No instance manages AgentDeck's issues on this deck: nowhere to file, nothing written.
    assert work_bugs.report([_row()], SimpleNamespace(managing=lambda _r: ()), now=NOW) == 0
    assert _reports(state_path) == []


def test_the_wait_counts_from_the_move_into_needs_retry_not_from_a_later_change():
    def point(category, prev, minutes):
        return SimpleNamespace(category=category, prev_category=prev,
                               at=NOW - timedelta(minutes=minutes))

    entered: dict = {}
    a, b = "github_issue:eerovil/bazaar#9", "github_issue:eerovil/bazaar#10"
    work_bugs.track_entries({a: point(NEEDS_RETRY, "", 10), b: point(NEEDS_RETRY, "", 10)},
                            entered)
    # A's kind or tone changed inside Needs retry; B left it and came back.
    since = work_bugs.track_entries(
        {a: point(NEEDS_RETRY, NEEDS_RETRY, 1), b: point(NEEDS_RETRY, "", 1)}, entered
    )
    assert since == {a: NOW - timedelta(minutes=10), b: NOW - timedelta(minutes=1)}
    assert work_bugs.track_entries({a: point("", NEEDS_RETRY, 0)}, entered) == {}
    assert work_bugs.track_entries(None, entered) is None


# --- the Poller --------------------------------------------------------------------------------


def _state() -> dict:
    return {}


def _port(**kwargs) -> FakePort:
    return FakePort(**kwargs)


def _write(state_path, work_id, cause="worker_died", repo="agentdeck", run_id="gen-1"):
    return bug_reports.write(
        state_path, repo=repo, work_id=work_id, cause=cause, run_id=run_id,
        reason="The worker stopped", work_url=f"https://deck/works/{work_id}", now=NOW,
    )


def test_the_poller_files_one_labelled_issue_per_cause_and_removes_the_reports(tmp_path):
    store = store_for(tmp_path, _state())
    _write(store.paths.state, "github_issue:eerovil/bazaar#1")
    _write(store.paths.state, "github_issue:eerovil/bazaar#2")
    _write(store.paths.state, "github_issue:eerovil/bazaar#3", cause="start_refused:cwd_required")
    port = _port(opened=[901, 902])

    run_wake(store, port, now=NOW, issue_repos=REPOS)

    assert _reports(store.paths.state) == []
    titles = {call["title"]: call for call in port.opened_calls}
    assert len(port.opened_calls) == 2
    died = titles["Needs retry bug: A worker stopped without a report after every automatic "
                  "restart"]
    assert "bazaar#1" in died["body"] and "bazaar#2" in died["body"]
    assert "<!-- agentdeck-needs-retry-bug: agentdeck::worker_died -->" in died["body"]
    refused = next(call for title, call in titles.items() if "cwd_required" in title)
    assert "bazaar#3" in refused["body"]
    records = store.read()[bug_reports.RECORDS]
    assert {record["number"] for record in records.values()} == {901, 902}

    # The same reports written again (a deck restart) are taken once only.
    _write(store.paths.state, "github_issue:eerovil/bazaar#1")
    run_wake(store, port, now=NOW, issue_repos=REPOS)
    assert len(port.opened_calls) == 2 and _reports(store.paths.state) == []


def test_another_work_on_an_open_issue_is_a_comment_and_a_closed_one_files_again(tmp_path):
    state = _state()
    state[bug_reports.RECORDS] = {
        "agentdeck::worker_died": {"repo": "agentdeck", "cause": "worker_died", "number": 77,
                                   "pending": []},
    }
    store = store_for(tmp_path, state)
    _write(store.paths.state, "github_issue:eerovil/bazaar#4")
    port = _port(ref_states={"agentdeck#77": "OPEN"})

    run_wake(store, port, now=NOW, issue_repos=REPOS)

    assert port.opened_calls == []
    comments = [
        effect for effect in port.applied
        if effect.kind is EffectKind.COMMENT and effect.number == 77
    ]
    assert len(comments) == 1 and "bazaar#4" in str(comments[0].value)

    _write(store.paths.state, "github_issue:eerovil/bazaar#5")
    closed = _port(ref_states={"agentdeck#77": "CLOSED"}, opened=[78])
    run_wake(store, closed, now=NOW, issue_repos=REPOS)
    assert len(closed.opened_calls) == 1 and "bazaar#5" in closed.opened_calls[0]["body"]
    assert store.read()[bug_reports.RECORDS]["agentdeck::worker_died"]["number"] == 78


def test_a_report_for_a_repository_this_project_does_not_hold_is_discarded(tmp_path):
    store = store_for(tmp_path, _state())
    _write(store.paths.state, "github_issue:eerovil/bazaar#1", repo="bazaar")
    _write(store.paths.state, "github_issue:eerovil/bazaar#2")
    port = _port(opened=[901])

    outcome = run_wake(store, port, now=NOW)  # naming no issue repositories permits none

    assert port.opened_calls == [] and _reports(store.paths.state) == []
    assert any("not ours" in line for line in outcome.log)


def test_a_malformed_report_is_discarded_and_files_nothing(tmp_path):
    store = store_for(tmp_path, _state())
    folder = bug_reports.directory(store.paths.state)
    folder.mkdir(parents=True)
    (folder / "junk.json").write_text("{not json")
    (folder / "bad.json").write_text(json.dumps({"id": "x", "repo": "agentdeck", "cause": "!"}))
    port = _port(opened=[901])

    run_wake(store, port, now=NOW, issue_repos=REPOS)

    assert port.opened_calls == [] and _reports(store.paths.state) == []
    assert bug_reports.write(store.paths.state, repo="agentdeck", work_id="w", cause="Bad!") is None


# --- what the person sees ------------------------------------------------------------------


def test_the_needs_retry_header_says_what_the_group_means(tmp_path, monkeypatch):
    from test_work_actions import _client, _retry_app

    app, _gh, _card = _retry_app(tmp_path, monkeypatch)
    page = _client(app).get("/").text

    header = page.split('data-front-group="needs_retry"', 1)[1].split("</h3>", 1)[0]
    assert NOTES[NEEDS_RETRY] in header
    assert page.count(NOTES[NEEDS_RETRY]) == 1
