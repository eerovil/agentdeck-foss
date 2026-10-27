"""A card shipping pull requests in several repositories says which heads still need a run (#2718).

musescore#312 shipped `score-plugins#316` and its companion `homr#94`. Review passed
and CI was green on both, yet the card never released: the tests bit wants a green run on every
open head, and a verification job credits only the head of the repository its ``--cwd`` is in.
The worker ran one job from its own worktree and nothing told it the companion's head was still
missing. A third pull request, in a repository the board never reads, was silently not part of
the card, and verdicts on it were refused as if they named the wrong commit.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import jinja2
from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, run_wake, store_for

from agentdeck.poller_adapter.runner import Workspace, compose_message
from agentdeck.poller_engine import ready_review, release
from agentdeck.poller_engine.ports import EffectKind, LinkedPRs, PrState
from agentdeck.poller_engine.reconcile import CardSnapshot
from agentdeck.poller_engine.worker_control import WorkerControl
from agentdeck.poller_ops import _card_untested_heads
from agentdeck.work_facts import OutputFact
from agentdeck.work_overview import untested_outputs

_KEY = "store#5"
_OWN = "store#7"
_LIB = "lib#9"
_OWN_HEAD = "a" * 40
_LIB_HEAD = "b" * 40
_GEN = "gen1"
_HEADS = {_OWN: _OWN_HEAD, _LIB: _LIB_HEAD}


def _port(*, status: str = "In review", comments=()) -> FakePort:
    port = FakePort(
        comments=list(comments),
        snapshots={_KEY: CardSnapshot(status=status)},
        linked={_KEY: LinkedPRs(open=dict(_HEADS))},
        pr_states={
            ref: PrState(ref=ref, head=sha, merge_state="CLEAN") for ref, sha in _HEADS.items()
        },
        review_diffs={_OWN: "diff-own", _LIB: "diff-lib"},
    )
    port.code_repos = ("store", "lib")
    return port


def _reviewed(*, green: tuple[str, ...] = (_OWN_HEAD,)) -> dict:
    """Both pull requests reviewed clean; only the card's own head has a green run."""
    rec = {"status": "In review", "started": True, "generation": _GEN}
    release.record_review_pass(
        rec, heads=dict(_HEADS), diffs={_OWN: "diff-own", _LIB: "diff-lib"}, now=_NOW
    )
    for sha in green:
        release.record_tests_pass(rec, head=sha, now=_NOW)
    return rec


def _payloads(store) -> list[str]:
    return [str(c.get("payload", "")) for c in store.read().get("controls", {}).values()]


# --- the engine names the missing head ---

def test_untested_heads_names_only_the_heads_no_green_run_covers():
    rec = _reviewed()
    assert release.untested_heads(rec, heads=_HEADS) == {_LIB: _LIB_HEAD}
    assert release.untested_heads(rec, heads={_OWN: _OWN_HEAD}) == {}
    # An unreadable head is no head to run a suite on.
    assert release.untested_heads(rec, heads={_LIB: None}) == {}


def test_the_published_reading_names_the_companion_head_holding_the_bit_red(tmp_path):
    store = store_for(tmp_path, {"issues": {_KEY: _reviewed()}})
    outcome = run_wake(store, _port(), now=_NOW, auto_release=True)

    reading = release.recorded_bits(store.read()["issues"][_KEY])
    assert reading["bits"]["tests-pass"] is False
    assert reading["untested"] == {_LIB: _LIB_HEAD}
    assert not any(f"{_OWN}@" in line and "no green run" in line for line in outcome.log)


def test_a_green_reading_carries_no_untested_list(tmp_path):
    store = store_for(tmp_path, {"issues": {_KEY: _reviewed(green=(_OWN_HEAD, _LIB_HEAD))}})
    run_wake(store, _port(), now=_NOW, auto_release=False, verify_turn=True)

    reading = release.recorded_bits(store.read()["issues"][_KEY])
    assert reading["bits"]["tests-pass"] is True
    assert "untested" not in reading


def test_the_verify_turn_names_each_missing_head_and_how_to_cover_it(tmp_path):
    store = store_for(tmp_path, {"issues": {_KEY: _reviewed()}})
    run_wake(store, _port(), now=_NOW, auto_release=True)

    (asked,) = [text for text in _payloads(store) if "A review passed" in text]
    assert f"- `{_LIB}` at `{_LIB_HEAD}`" in asked
    assert f"`{_OWN}`" not in asked, "the head already green is not asked for again"
    assert "one verification job for each of them, with `--cwd` in that pull request's" in asked


def test_a_single_pull_request_verify_turn_is_unchanged(tmp_path):
    rec = {"status": "In review", "started": True, "generation": _GEN}
    release.record_review_pass(rec, heads={_OWN: _OWN_HEAD}, diffs={_OWN: "diff-own"}, now=_NOW)
    port = FakePort(
        snapshots={_KEY: CardSnapshot(status="In review")},
        linked={_KEY: LinkedPRs(open={_OWN: _OWN_HEAD})},
        pr_states={_OWN: PrState(ref=_OWN, head=_OWN_HEAD, merge_state="CLEAN")},
        review_diffs={_OWN: "diff-own"},
    )
    store = store_for(tmp_path, {"issues": {_KEY: rec}})
    run_wake(store, port, now=_NOW, auto_release=True)

    (asked,) = [text for text in _payloads(store) if "A review passed" in text]
    assert "more than one pull request" not in asked


# --- the ready-for-review reply ---

def _working() -> dict:
    rec = {"status": "In progress", "generation": _GEN, "started": True, "processing": ["9"]}
    release.record_tests_pass(rec, head=_OWN_HEAD, now=_NOW)
    return rec


def _control(tmp_path, rec: dict) -> tuple[object, WorkerControl]:
    store = store_for(tmp_path, {"issues": {_KEY: rec}})
    control = WorkerControl(
        store, _port(status="In progress"), parallel_review=True,
        clock=lambda: _NOW, sleep=lambda _s: None,
    )
    return store, control


def test_the_offer_on_two_pull_requests_names_the_head_still_missing_a_run(tmp_path):
    store, control = _control(tmp_path, _working())
    result = control.ready_for_review("store", 5, _GEN)

    assert result.ok, result.reason
    assert f"Still missing: {_LIB}@{_LIB_HEAD[:12]}" in result.reason
    assert f"Already green: {_OWN}@{_OWN_HEAD[:12]}" in result.reason
    assert "only the head of the repository its --cwd is in" in result.reason
    assert ready_review.current(store.read()["issues"][_KEY])["heads"] == _HEADS


def test_offering_again_still_says_what_is_missing(tmp_path):
    rec = _working()
    assert ready_review.offer(rec, heads=dict(_HEADS), generation=_GEN, now=_NOW) == "offered"
    _, control = _control(tmp_path, rec)
    result = control.ready_for_review("store", 5, _GEN)

    assert result.reason.startswith("already offered for review")
    assert f"Still missing: {_LIB}@{_LIB_HEAD[:12]}" in result.reason


def test_the_offer_says_so_when_every_head_is_already_green(tmp_path):
    rec = _working()
    release.record_tests_pass(rec, head=_LIB_HEAD, now=_NOW)
    _, control = _control(tmp_path, rec)
    result = control.ready_for_review("store", 5, _GEN)

    assert "already covers every one of these heads" in result.reason
    assert "Still missing" not in result.reason


# --- a verdict on a pull request the board never reads ---

def test_a_verdict_on_an_untracked_repository_says_why_it_was_not_counted(tmp_path):
    verdict = comment("c1", f"/reviewed pass {'e' * 40} {'d' * 64} songs#4", author="eero")
    port = _port(comments=[verdict])
    store = store_for(tmp_path, {"issues": {_KEY: _reviewed()}})
    run_wake(store, port, now=_NOW, auto_release=True)

    bodies = [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]
    (said,) = [body for body in bodies if "songs#4" in body]
    assert "is not a pull request this board tracks" in said
    assert "`lib`, `store`" in said
    assert "names nothing I can check" not in said


def test_a_verdict_on_a_tracked_repository_keeps_the_old_answer(tmp_path):
    # A tracked repository, but no pull request of this card: the old answer still fits.
    verdict = comment("c1", f"/reviewed pass {'e' * 40} {'d' * 64} lib#10", author="eero")
    port = _port(comments=[verdict])
    store = store_for(tmp_path, {"issues": {_KEY: _reviewed()}})
    run_wake(store, port, now=_NOW, auto_release=True)

    bodies = [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]
    assert any("names nothing I can check" in body for body in bodies)
    assert not any("this board tracks" in body for body in bodies)


# --- the worker brief ---

def test_a_multi_repository_brief_says_each_head_needs_its_own_job_and_what_is_tracked():
    directive = {
        "action": "work", "repo": "store", "number": 5, "url": "https://x/5",
        "messages": [{"id": "9", "payload": "do the work"}],
    }
    message = compose_message(
        directive,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        code_checkouts={"store": "/home/me/store", "lib": "/home/me/lib"},
    )
    assert "A pull request in any other repository is never reviewed, verified or released" in (
        message
    )
    assert "submit one job per pull request, with `--cwd` in that pull request's checkout" in (
        message
    )


# --- the deck ---

def test_the_deck_reads_the_untested_heads_only_from_a_red_tests_bit():
    card = {
        "release_bits": {
            "bits": {"tests-pass": False},
            "heads": dict(_HEADS),
            "untested": {_LIB: _LIB_HEAD, "junk": 3},
        }
    }
    assert _card_untested_heads(card, "org") == (
        ("https://github.com/org/lib/pull/9", _LIB_HEAD),
    )
    card["release_bits"]["bits"]["tests-pass"] = True
    assert _card_untested_heads(card, "org") == ()
    assert _card_untested_heads({"release_bits": "broken"}, "org") == ()


def _output(repo: str, number: int, head: str, state: str = "open") -> OutputFact:
    return OutputFact(
        f"https://github.com/org/{repo}/pull/{number}", f"org/{repo}", number, "card", state,
        head=head,
    )


def test_the_work_names_a_companion_head_only_while_it_is_still_the_open_head():
    card = SimpleNamespace(untested_heads=(("https://github.com/org/lib/pull/9", _LIB_HEAD),))
    both = [_output("store", 7, _OWN_HEAD), _output("lib", 9, _LIB_HEAD)]
    assert untested_outputs(card, both) == (f"org/lib#9@{_LIB_HEAD[:7]}",)
    # The head moved since the reading: it says nothing about the new one.
    moved = [_output("store", 7, _OWN_HEAD), _output("lib", 9, "c" * 40)]
    assert untested_outputs(card, moved) == ()
    # One open pull request: its own verification line already says it all.
    alone = [_output("store", 7, _OWN_HEAD, "merged"), _output("lib", 9, _LIB_HEAD)]
    assert untested_outputs(card, alone) == ()


def test_the_work_page_result_says_which_head_waits_for_a_run():
    templates = Path(__file__).resolve().parents[1] / "src/agentdeck/web/templates"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(templates)))
    card = SimpleNamespace(
        outputs=(), verification=None, review=None, untested=("org/lib#9@bbbbbbb",)
    )
    html = env.get_template("partials/work_detail.html").render(
        section="result", detail=SimpleNamespace(card=card)
    )
    assert "data-wk-untested" in html
    assert "No green run yet on org/lib#9@bbbbbbb" in html
