"""The half of a wake that acts (#180 slice 4).

The engine's decisions were already tested to death. What was never tested — because nothing
called it — is what happens to a directive after the engine prints it. Until now the answer was
"it goes into cycle.log", so these tests exist to make that answer impossible again.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys as _sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentdeck.poller_adapter.runner import (
    CheckoutFreshness,
    PreparedWorkspaces,
    SingleCheckoutWorkspaceAdapter,
    Workspace,
    act_on,
    changed_files,
    compose_message,
    default_branch,
    drain_review_verdicts,
    owner_of,
    read_directives,
    recent_change_warning,
    refresh_checkout,
    stale_checkout_brief,
)
from agentdeck.poller_adapter.work_workspaces import ClaimedWorkspace
from agentdeck.poller_adapter.worker import (
    SPEC_APPROVAL_RULE,
    SPEC_APPROVED_ASK,
    SPEC_ONLY_QUESTION_RULE,
    Delivery,
    DeliveryOutcome,
    _compact_poller_brief,
)
from agentdeck.work_workspace_claims import GitHost

_DIRECTIVE = {
    "ref": "store#5",
    "repo": "store",
    "number": 5,
    "action": "new",
    "generation": "gen1",
    "dispatch_id": "d1",
    "messages": [{"payload": "fix the login bug"}],
}

class _Dispatcher:
    """A dispatcher that records what it was asked to deliver."""

    def __init__(self, outcome=DeliveryOutcome.DELIVERED, reason=""):
        self.outcome = outcome
        self.reason = reason
        self.calls: list[dict] = []

    def deliver(self, directive, *, cwd=None, message="", worktrees=None, now=None):
        self.calls.append(
            {
                "directive": directive,
                "cwd": cwd,
                "message": message,
                "worktrees": worktrees,
            }
        )
        return Delivery(self.outcome, self.reason)

    def refuse(self, directive, reason, *, now=None):
        self.calls.append({"directive": directive, "refused": reason})
        return Delivery(DeliveryOutcome.REFUSED, reason)


class _RecordingWorks:
    """The runtime's Work workspace service, claiming whatever it is asked for (#2149).

    Records each ask as ``(checkout, number, branch, base, suffix)``; a purpose in ``refuse``
    is answered unusable, which is every answer but a claim now that nothing falls back to a
    card-owned worktree (#2195).
    """

    def __init__(self, calls=None, *, refuse=()):
        self.calls = [] if calls is None else calls
        self.refuse = set(refuse)

    def ensure(self, directive, *, purpose, checkout, name, branch, base, repository=""):
        number = int(directive.get("number") or 0)
        self.calls.append((checkout, number, branch, base, name.removeprefix(f"issue-{number}")))
        if purpose in self.refuse:
            return ClaimedWorkspace(detail="refused")
        path = Path(checkout) / ".worktrees" / name
        path.mkdir(parents=True, exist_ok=True)
        return ClaimedWorkspace(path=path, branch=branch, base_ref=base, created=True)


def _prepared(_tmp_path, *, refuse=()):
    return _RecordingWorks(refuse=refuse)


def _change_reader(changes_by_worktree, *, failing=()):
    def run(command, **_kwargs):
        worktree = str(command[2])
        if worktree in failing:
            return subprocess.CompletedProcess(command, 1, "", "unreadable")
        if any(part.startswith("origin/") and part.endswith("...HEAD") for part in command):
            paths = changes_by_worktree.get(worktree, ())
            output = "\0".join(paths) + ("\0" if paths else "")
            return subprocess.CompletedProcess(command, 0, output, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def _registered_for_test(path):
    worktree = Path(path)
    return worktree.parent.parent, True


def test_changed_files_combines_committed_local_and_untracked_paths():
    outputs = iter(("committed.py\0", "local.py\0", "untracked.py\0"))

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, next(outputs), "")

    assert changed_files(Path("/repo/.worktrees/issue-5"), runner=run) == {
        "committed.py", "local.py", "untracked.py",
    }


def test_a_directive_becomes_a_worker_in_its_own_worktree(tmp_path):
    dispatcher = _Dispatcher()
    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
    )
    assert result.delivered == 1
    call = dispatcher.calls[0]
    assert call["cwd"].endswith("/.worktrees/issue-5")
    # The worktree is reported with the delivery, because the receipt is the only account of
    # what was created on disk and the engine releases exactly what it was told about.
    assert call["worktrees"] == [call["cwd"]]
    assert "fix the login bug" in call["message"]


@pytest.mark.parametrize(
    ("level", "present", "absent"),
    [
        (None, "Do not deploy from this worker.", "Deploy only when"),
        ("none", "Do not deploy from this worker.", "Deploy only when"),
        (
            "staging",
            "Deploy only when a requester explicitly asked for that deploy",
            "or production environments",
        ),
        ("production", "canary or production environments", "never to production"),
    ],
)
def test_the_project_deploy_level_reaches_both_briefs(tmp_path, level, present, absent):
    """#2656: the composed brief and the compacted one the runtime delivers say the same."""
    dispatcher = _Dispatcher()
    kwargs = {} if level is None else {"worker_may_deploy": level}
    act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
        **kwargs,
    )
    call = dispatcher.calls[0]
    compact = _compact_poller_brief(
        call["directive"], call["message"], cwd=call["cwd"], worktrees=call["worktrees"]
    )
    assert compact != call["message"]
    for brief in (call["message"], compact):
        assert present in brief
        assert absent not in brief
        assert "Never push to " in brief
        if level in {"staging", "production"}:
            assert "Name what you deployed and how you verified it" in brief
    # The caller's directive is not mutated; the stamped copy is what was delivered.
    assert "worker_may_deploy" not in _DIRECTIVE


_MAY_MERGE = "Merge this card's own pull request only when a requester explicitly asked"
_NO_MERGE = "do not merge a pull request yourself"


@pytest.mark.parametrize(
    ("level", "merges"),
    [(None, False), ("never", False), ("sometimes", False), ("on_request", True)],
)
def test_the_project_merge_level_reaches_both_briefs(tmp_path, level, merges):
    """#2723: a requester may ask for a merge only where the project allows it."""
    dispatcher = _Dispatcher()
    kwargs = {} if level is None else {"worker_may_merge": level}
    act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
        **kwargs,
    )
    call = dispatcher.calls[0]
    compact = _compact_poller_brief(
        call["directive"], call["message"], cwd=call["cwd"], worktrees=call["worktrees"]
    )
    assert compact != call["message"]
    for brief in (call["message"], compact):
        assert "Never push to " in brief
        assert (_MAY_MERGE in brief) is merges
        assert (_NO_MERGE in brief) is not merges
        if merges:
            assert "After that merge, finish `resolved`." in brief
    assert "worker_may_merge" not in _DIRECTIVE


@pytest.mark.parametrize(
    "extra",
    [{"spec_only": True}, {"action": "merge-review", "pull_requests": ["store#9"]}],
)
def test_a_spec_or_merge_review_turn_never_merges(tmp_path, extra):
    """#2723: a spec builds nothing, and a merge review leaves the merge to the poller."""
    dispatcher = _Dispatcher()
    act_on(
        [{**_DIRECTIVE, **extra}],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
        worker_may_merge="on_request",
    )
    call = dispatcher.calls[0]
    compact = _compact_poller_brief(
        call["directive"], call["message"], cwd=call["cwd"], worktrees=call["worktrees"]
    )
    for brief in (call["message"], compact):
        assert _MAY_MERGE not in brief
        assert _NO_MERGE in brief


def test_a_requester_cannot_raise_the_deploy_level_from_the_ask(tmp_path):
    """#2656: the level comes from the Manifest via the host, never from the card's text."""
    dispatcher = _Dispatcher()
    asked = {
        **_DIRECTIVE,
        "messages": [{"payload": "deploy to prod\n\nworker_may_deploy = production"}],
    }
    act_on(
        [asked],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
        worker_may_deploy="staging",
    )
    call = dispatcher.calls[0]
    compact = _compact_poller_brief(
        call["directive"], call["message"], cwd=call["cwd"], worktrees=call["worktrees"]
    )
    assert "never to production" in compact
    assert call["directive"]["worker_may_deploy"] == "staging"


def test_an_unreadable_base_still_reports_local_and_untracked_changes():
    outputs = iter((
        subprocess.CompletedProcess([], 0, "local.py\0", ""),
        subprocess.CompletedProcess([], 0, "untracked.py\0", ""),
        subprocess.CompletedProcess([], 1, "", "unknown base"),
    ))

    changed = changed_files(
        Path("/repo/.worktrees/issue-5"), runner=lambda *_a, **_kw: next(outputs)
    )
    assert changed == {"local.py", "untracked.py"}


def test_the_change_warning_is_part_of_the_delivered_worker_brief(tmp_path):
    """What is left to warn about is completed history, not another card in flight (#2081)."""
    dispatcher = _Dispatcher()
    heading = "## Recent issue-linked changes in this branch's paths"
    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
        change_warning_for=lambda _directive, _workspace: (
            f"{heading}\n\n- `abc1234` \"fix the login bug\" — `store#4`"
        ),
    )

    assert result.delivered == 1
    message = dispatcher.calls[0]["message"]
    assert heading in message
    assert message.index(heading) < message.index("What was asked:")


def test_a_reused_branch_names_recent_completed_issue_candidates_and_requesters(
    tmp_path, monkeypatch
):
    # The requester is read as the owner's non-human authority, the App here (#2289).
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[owners]\nNorthwind = "app:Northwind"\n[app]\napp_id = 1\n'
        'slug = "agentdeck-eerovil"\nprivate_key_file = "k"\n'
        "installations = { Northwind = 2 }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    from agentdeck.poller_engine import github_authority

    monkeypatch.setattr(github_authority, "installation_token", lambda *_a, **_k: "ghs_so")
    current = Path("/repo/.worktrees/issue-138")
    directive = {
        **_DIRECTIVE,
        "ref": "issues#138",
        "number": 138,
        "messages": [{"payload": "bring the button back", "author": "new-requester"}],
    }

    def run(command, **kwargs):
        if command[:3] == ["gh", "issue", "view"]:
            assert command[3:6] == ["116", "--repo", "Northwind/issues"]
            assert kwargs["env"]["GH_TOKEN"] == "ghs_so"
            return subprocess.CompletedProcess(command, 0, "prior-requester\n", "")
        if "log" in command:
            output = (
                "dfbe59276b62ef38907add93a6ae51417f9c51ee\x00"
                "fix(packing): remove Posti pikkupaketti button\x00"
                "Refs Northwind/issues#116\x00\x1e"
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if any(part.startswith("origin/") and part.endswith("...HEAD") for part in command):
            return subprocess.CompletedProcess(command, 0, "packing.vue\x00", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    warning = recent_change_warning(
        directive,
        PreparedWorkspaces(
            cwd=current,
            workspaces=(Workspace(current, "agent/issue-138"),),
            owner="Northwind",
        ),
        runner=run,
        validator=_registered_for_test,
    )

    assert "## Recent issue-linked changes" in warning
    assert "`dfbe59276b62`" in warning
    assert "`Northwind/issues#116`" in warning
    assert '"prior-requester"' in warning
    assert '"packing.vue"' in warning
    assert "file-level candidates, not proof" in warning


def test_a_fresh_or_unreadable_branch_has_no_completed_history_claim():
    current = Path("/repo/.worktrees/issue-138")
    calls = []

    def fresh(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    assert recent_change_warning(
        {**_DIRECTIVE, "messages": []},
        Workspace(current, "agent/issue-138"),
        runner=fresh,
        validator=_registered_for_test,
    ) == ""
    assert not any("log" in command for command in calls)
    assert not any(command[:3] == ["gh", "issue", "view"] for command in calls)

    def unreadable(command, **_kwargs):
        if any(part.startswith("origin/") and part.endswith("...HEAD") for part in command):
            return subprocess.CompletedProcess(command, 0, "packing.vue\x00", "")
        return subprocess.CompletedProcess(command, 1, "", "unreadable")

    assert recent_change_warning(
        {**_DIRECTIVE, "messages": [{"payload": "fix it", "author": "eero"}]},
        Workspace(current, "agent/issue-138"),
        runner=unreadable,
        validator=_registered_for_test,
    ) == ""


def test_a_steer_goes_straight_to_the_runtime_with_its_plain_payload(tmp_path):
    """The runtime sends a steer to the account holding its Run (#2146); the host names none."""
    class ExplodingAdapter:
        def prepare(self, _directive):
            raise AssertionError("a steer must not prepare or register a workspace")

    dispatcher = _Dispatcher()
    directive = {
        **_DIRECTIVE,
        "steer": True,
        "messages": [{"payload": "urgent correction"}],
    }

    result = act_on(
        [directive],
        dispatcher=dispatcher,
        workspace_adapter=ExplodingAdapter(),
        register=lambda *_args: (_ for _ in ()).throw(
            AssertionError("a steer must not register a workspace")
        ),
    )

    assert result.delivered == 1
    assert dispatcher.calls == [{
        "directive": directive,
        "cwd": None,
        "message": "",
        "worktrees": None,
    }]


def test_a_directive_with_no_checkout_on_this_host_is_skipped_not_half_delivered(tmp_path):
    """The engine keeps its lease and decides next wake, which is better than a worker with
    nowhere to work or a receipt for a delivery that never happened."""
    dispatcher = _Dispatcher()
    result = act_on(
        [_DIRECTIVE], dispatcher=dispatcher, checkouts={}, work_workspaces=_prepared(tmp_path)
    )
    # Refused, never delivered: the refusal is what tells the engine nothing was sent.
    assert [call["refused"] for call in dispatcher.calls] == [
        "workspace_unprepared: no checkout for 'store' on this host"
    ]
    assert result.delivered == 0
    assert "no checkout" in result.skipped[0]


def test_a_project_adapter_can_deliver_a_no_checkout_card_with_zero_worktrees(tmp_path):
    class IssueOnlyAdapter:
        def prepare(self, directive):
            return PreparedWorkspaces(cwd=tmp_path)

    dispatcher = _Dispatcher()
    result = act_on(
        [_DIRECTIVE], dispatcher=dispatcher, workspace_adapter=IssueOnlyAdapter()
    )

    assert result.delivered == 1
    assert dispatcher.calls[0]["cwd"] == str(tmp_path)
    assert dispatcher.calls[0]["worktrees"] == []
    assert "none yet" in dispatcher.calls[0]["message"]


def test_a_zero_worktree_delivery_is_still_fenced_to_its_generation(tmp_path):
    class IssueOnlyAdapter:
        def prepare(self, directive):
            return PreparedWorkspaces(cwd=tmp_path)

    registered = []
    dispatcher = _Dispatcher()
    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        workspace_adapter=IssueOnlyAdapter(),
        register=lambda directive, worktrees: registered.append(
            (directive["generation"], list(worktrees))
        ) or False,
    )

    assert registered == [("gen1", [])]
    assert dispatcher.calls == []
    assert "could not register" in result.skipped[0]


def test_a_project_adapter_can_prepare_and_register_multiple_repositories(tmp_path):
    paths = (tmp_path / "store-wt", tmp_path / "abacus-wt")
    for path in paths:
        path.mkdir()

    class MultiRepoAdapter:
        def prepare(self, directive):
            return PreparedWorkspaces(
                cwd=paths[0],
                workspaces=(
                    Workspace(paths[0], "agent/issue-5-store"),
                    Workspace(paths[1], "agent/issue-5-abacus"),
                ),
                owner="eerovil",
            )

    registered = []
    dispatcher = _Dispatcher()
    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        workspace_adapter=MultiRepoAdapter(),
        register=lambda directive, worktrees: registered.append(
            (directive["generation"], list(worktrees))
        ) or True,
    )

    assert result.delivered == 1
    # One registering act for the whole delivery: a per-path loop could accept half a card's
    # worktrees and leave the rest owned by nobody.
    assert registered == [("gen1", [str(path) for path in paths])]
    assert dispatcher.calls[0]["worktrees"] == [str(path) for path in paths]


def _code_adapter(path):
    class Adapter:
        def prepare(self, directive):
            return PreparedWorkspaces(
                cwd=path, workspaces=(Workspace(path, "agent/issue-5"),), owner="eerovil"
            )

    return Adapter()


def test_a_cards_evidence_worktree_is_registered_and_named_in_its_brief(tmp_path):
    """#1791 review: the evidence worktree is the Work's artifact claim, fenced like the rest."""
    code = tmp_path / "worktree"
    code.mkdir()
    _, checkout = _evidence_origin(tmp_path)

    registered = []
    dispatcher = _Dispatcher()
    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        workspace_adapter=_code_adapter(code),
        artifact_repo="eerovil/shots",
        artifact_checkout=str(checkout),
        work_workspaces=_prepared(tmp_path),
        register=lambda directive, worktrees: registered.append(list(worktrees)) or True,
    )

    assert result.delivered == 1
    evidence = str(checkout / ".worktrees" / "issue-5-store-evidence")
    # Registered in the same single act as the code worktree, not afterwards.
    assert registered == [[str(code), evidence]]
    assert dispatcher.calls[0]["worktrees"] == [str(code), evidence]
    assert evidence in dispatcher.calls[0]["message"]
    assert "Commit them to `eerovil/shots`" in dispatcher.calls[0]["message"]


def test_a_refused_registration_keeps_the_works_evidence_worktree(tmp_path):
    """The evidence worktree is the Work's claim: a refused delivery never deletes it (#2195)."""
    code = tmp_path / "worktree"
    code.mkdir()
    _, checkout = _evidence_origin(tmp_path)

    result = act_on(
        [_DIRECTIVE],
        dispatcher=_Dispatcher(),
        workspace_adapter=_code_adapter(code),
        artifact_repo="eerovil/shots",
        artifact_checkout=str(checkout),
        work_workspaces=_prepared(tmp_path),
        register=lambda _directive, _worktrees: False,
    )

    assert result.delivered == 0
    assert (checkout / ".worktrees" / "issue-5-store-evidence").is_dir()


def test_a_card_that_cannot_get_an_evidence_worktree_is_still_worked(tmp_path):
    """Screenshots are not worth failing a card over — and never worth a shared checkout."""
    code = tmp_path / "worktree"
    code.mkdir()

    dispatcher = _Dispatcher()
    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        workspace_adapter=_code_adapter(code),
        artifact_repo="eerovil/shots",
        artifact_checkout=str(tmp_path / "not-a-clone"),
    )

    assert result.delivered == 1
    assert any("screenshots stay with the code" in line for line in result.log)
    message = dispatcher.calls[0]["message"]
    assert "eerovil/shots" not in message
    assert "https://github.com/<owner>/<repo>/blob/<commit-sha>/<path>?raw=1" in message


def test_a_prepared_worktree_is_not_delivered_when_generation_registration_is_refused(tmp_path):
    path = tmp_path / "worktree"
    path.mkdir()

    class Adapter:
        def prepare(self, directive):
            return PreparedWorkspaces(
                cwd=path, workspaces=(Workspace(path, "agent/issue-5"),)
            )

    dispatcher = _Dispatcher()
    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        workspace_adapter=Adapter(),
        register=lambda _directive, _worktrees: False,
    )

    assert dispatcher.calls == []
    assert "could not register" in result.skipped[0]


def test_an_abandoned_delivery_deletes_nothing_and_names_what_it_left(tmp_path):
    """Nothing in a wake removes a worktree any more (#2195). A Work's claim is kept for its
    next Run; anything else an adapter handed back is named, never silently dropped."""
    claimed, loose = tmp_path / "claimed", tmp_path / "loose"
    for path in (claimed, loose):
        path.mkdir()

    class Adapter:
        def prepare(self, directive):
            return PreparedWorkspaces(
                cwd=tmp_path / "gone",
                workspaces=(
                    Workspace(claimed, "agent/issue-5", created=True, work_claimed=True),
                    Workspace(loose, "agent/issue-5-other", created=True),
                ),
            )

    for register in (lambda _d, _w: True, lambda _d, _w: False):
        result = act_on(
            [_DIRECTIVE], dispatcher=_Dispatcher(), workspace_adapter=Adapter(), register=register
        )
        assert claimed.is_dir() and loose.is_dir()
        assert str(loose) in result.skipped[0] and str(claimed) not in result.skipped[0]


def test_a_workspace_that_cannot_be_prepared_stops_the_delivery(tmp_path):
    dispatcher = _Dispatcher()
    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path, refuse={"implementation"}),
    )
    assert all("refused" in call for call in dispatcher.calls) and len(dispatcher.calls) == 1
    assert "could not prepare" in result.skipped[0]


def test_an_unprepared_workspace_is_a_refusal_the_engine_can_read(tmp_path):
    """#2816: the skip wrote nothing back, so the engine read three skipped dispatches as
    three silent ones and parked the card saying a worker might be running."""

    class Refusing:
        def ensure(self, directive, **kwargs):
            return ClaimedWorkspace(detail="legacy: /x/.worktrees/issue-5 exists")

    dispatcher = _Dispatcher()
    act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=Refusing(),
    )
    assert dispatcher.calls == [{
        "directive": dispatcher.calls[0]["directive"],
        "refused": "workspace_unprepared: base `origin/master`: "
        "legacy: /x/.worktrees/issue-5 exists",
    }]


def test_an_uncertain_delivery_is_counted_but_not_retried_here(tmp_path):
    dispatcher = _Dispatcher(DeliveryOutcome.UNCERTAIN)
    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
    )
    assert result.uncertain == 1
    assert len(dispatcher.calls) == 1, "an uncertain delivery must never be re-sent here"


def test_the_engines_instructions_are_passed_through_verbatim(tmp_path):
    """The poller decides *whether* work happens, never what the work is."""
    directive = {**_DIRECTIVE, "messages": [{"payload": "one"}, {"payload": "two"}]}
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))
    assert "one" in message and "two" in message
    assert "/w/issue-5" in message


def test_an_ordinary_brief_carries_no_phase_or_checkpoint_instructions():
    """#2154 test 12: a worker is told to do the work and report it — never to checkpoint, to
    wait for a next phase, or to stop for context pressure."""
    message = compose_message(
        {**_DIRECTIVE, "messages": [{"payload": "fix it"}]},
        Workspace(path=Path("/w/issue-5"), branch="b"),
    )
    lowered = message.lower()
    for retired in ("checkpoint", "non-final phase", "phase packet", "next phase", "pressure"):
        assert retired not in lowered, retired
    assert "in_review --body-file <path>" in message


def test_a_work_brief_says_how_to_submit_verification_and_a_review_brief_does_not():
    """#2154: verification is explicit Run evidence, so the work brief names the one command
    that makes a job count; a reviewer runs no suite for the release and is not told to."""
    work = compose_message(
        {**_DIRECTIVE, "messages": [{"payload": "fix it"}]},
        Workspace(path=Path("/w/issue-5"), branch="b"),
    )
    review = compose_message(
        {**_DIRECTIVE, "action": "merge-review"},
        Workspace(path=Path("/w/issue-5"), branch="b"),
    )

    assert "## Verification evidence" in work
    assert "~/agentdeck/.venv/bin/agentdeck job submit --verification" in work
    assert "## Verification evidence" not in review
    # #2355: the resolution ask rides with the verification job, so a project without
    # auto_release (no engine verify turn) still asks its worker to write one.
    assert "/api/sessions/<session-key>/work-resolution" in work
    assert "work-resolution" not in review


def test_the_work_brief_puts_the_final_suite_after_review_without_promising_a_poller():
    """#2253: the suite is the last gate before release, not the ticket into review.

    The brief is generic — it does not know whether the project has ``auto_release`` on — so
    it must not promise that the poller will ask for the run; only the engine's own verify
    turn, which only such projects receive, says that.
    """
    work = compose_message(
        {**_DIRECTIVE, "messages": [{"payload": "fix it"}]},
        Workspace(path=Path("/w/issue-5"), branch="b"),
    )
    flat = " ".join(work.split())

    assert "last gate before release, not the ticket into review" in flat
    assert "report `in_review` on focused tests" in flat
    assert "on the head a clean review passed" in flat
    assert "send the changed head back to review before running the suite again" in flat
    assert "poller asks" not in flat and "poller will ask" not in flat
    assert "run it again after any later change" not in flat


def test_the_instruction_requester_is_named_for_the_history_comparison():
    directive = {
        **_DIRECTIVE,
        "messages": [{"payload": "bring the button back", "author": "current-requester"}],
    }
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "bring the button back" in message
    assert 'Requester: "current-requester"' in message


def test_an_ask_that_names_a_skill_is_read_as_a_request_to_run_it():
    """#1110: a card asking for `review-fix-pr` got a hand-rolled review, not the skill.

    The name arrived as one line under thousands of words of poller contract, so the worker read
    it as a description of the job. The rule is generic on purpose: the worker knows its own skill
    list, so the poller never matches names or tracks one provider's skill layout.
    """
    directive = {**_DIRECTIVE, "messages": [{"payload": "review-fix-pr"}]}
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "If the ask names something you have as a skill, invoke that skill" in message
    assert "not a description of the work to improvise" in message
    # The poller's own wall of instructions is what made the ask look like prose in the first
    # place; without this the worker can still read them as the procedure it was asked for.
    assert "they never replace the procedure the skill defines" in message
    assert "If you cannot find the named skill, say so in your terminal report" in message
    assert "The poller dispatch owns the issue workflow" in message
    assert "do not invoke a generic repository or manual issue-management skill" in message
    assert "only when the issue or request explicitly names the skill" in message
    assert message.index("What was asked:") < message.index("If the ask names something")


def test_codex_uses_an_injected_root_guide_only_when_its_contents_are_present():
    """#685: another project's skill names a control document this repository does not have.

    Pointing the worker at "the applicable AGENTS.md" left that conflict unresolved, so a worker
    following the skill spent its first act on a guaranteed missing read and then improvised a
    replacement source. The prompt has to say which one wins.
    """
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        account="codex:main",
    )

    assert "actual contents of the root guide" in message
    assert "present in your context" in message
    assert "do not read the guide again" in message
    assert "If they are absent, read the root guide now" in message
    assert "root guide for your starting repository is already in your context" not in message
    assert "Read the root guide of every repository you touch" not in message
    assert "If you touch another repository, read that repository's root guide" in message
    assert "AGENTS.md" in message and "CLAUDE.md" in message
    assert "design of record" in message
    assert (
        "only the deeper documents it routes to for the paths you actually inspect or change"
        in message
    )
    # The conflict itself, not just the preference: without this a worker reads both.
    assert "another project's skill does not apply" in message
    assert "guessing a local substitute" in message


def test_a_non_codex_worker_is_still_told_to_read_an_agents_only_root_guide():
    """#1004: Claude finds CLAUDE.md itself, but supported repositories may have only AGENTS."""
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        account="claude_code:main",
    )

    assert "Read the root guide for your starting repository" in message
    assert "AGENTS.md" in message and "CLAUDE.md" in message
    assert "do not read it again" not in message


def test_unmerged_documentation_does_not_claim_its_new_behavior_is_already_live():
    """#1059: agent-facing docs must remain truthful while their pull request is open."""
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "## Documentation in unmerged changes" in message
    assert "what this pull request introduces or proposes" in message
    assert "not as behavior the repository already has" in message
    assert "already exists on the pull request's base branch" in message
    assert "do not add a `LIVE` tag for behavior this pull request introduces" in message


def test_the_worker_avoids_repeated_short_polls_for_long_commands():
    """#1004: every empty poll resends the worker's full model context.

    The execution protocol belongs to the agent provider, but the shared brief can stop workers
    from reflexively spending one model call every 30 seconds while a test suite is still running.
    """
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "## Long-running commands" in message
    assert "Do not repeatedly poll a running command at short intervals" in message
    assert "continue independent work before checking it" in message
    assert "take the completion signal the tool already gives you" in message
    assert "a queued host job messages this chat when it finishes" in message


def test_the_worker_is_told_not_to_leave_a_timer_that_wakes_a_finished_chat():
    """#1798: a worker waited on its tests with several detached `sleep` commands.

    Each countdown that finished re-invoked the chat with a completion notification, so the
    last five turns of a finished card were spent announcing that another stale timer had
    fired. The brief has to rule out the timer itself, not just the polling loop it replaced.
    """
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "Never start a command whose only purpose is to let time pass" in message
    assert "finishing it wakes this chat and spends a whole turn" in message
    assert "end the turn and name what you are waiting for" in message
    assert "stop any timer you already started that no longer has a purpose" in message


def test_a_project_whose_code_spans_two_repositories_names_the_other_one():
    """#1578: nothing else in the brief says an owned code repository exists."""
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        code_checkouts={"store": "/home/me/store", "fork": "/home/me/fork"},
    )

    assert "## Other code this project owns" in message
    assert "- `fork` — /home/me/fork" in message
    # The worktree's own repository is where the worker already is.
    assert "- `store`" not in message
    # And the reference form a cross-repo release can actually see.
    assert "references the card in full — `store#5`" in message
    assert "A bare `#number` is deliberately not trusted there" in message
    assert "Never describe a companion pull request you have not actually opened" in message


def test_a_single_repository_project_is_told_nothing_about_other_checkouts():
    """The paragraph is dead weight for the projects that have one repository."""
    only_its_own = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        code_checkouts={"store": "/home/me/store"},
    )
    unset = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "## Other code this project owns" not in only_its_own
    assert "## Other code this project owns" not in unset


def test_a_borrowed_workspace_is_not_offered_back_as_somewhere_else_to_work():
    """An issue-only card already starts in a code repo's checkout; that is not a sibling."""
    directive = {**_DIRECTIVE, "ref": "issues#5", "repo": "issues"}
    message = compose_message(
        directive,
        PreparedWorkspaces(
            cwd=Path("/w/issue-5-issues"),
            workspaces=(Workspace(path=Path("/w/issue-5-issues"), branch="b"),),
            borrowed_from="store",
        ),
        code_checkouts={"store": "/home/me/store", "fork": "/home/me/fork"},
    )

    assert "- `store`" not in message
    assert "- `fork` — /home/me/fork" in message


def test_the_worker_maps_unfamiliar_external_apis_without_speculative_writes():
    """#1067: route discovery must not mutate a live third-party account."""
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "## Unfamiliar external APIs" in message
    assert "Probe only with `GET`, `HEAD`, or `OPTIONS`" in message
    assert "never send `POST`, `PUT`, `PATCH`, or `DELETE` speculatively" in message
    assert "Read the vendor documentation or ask instead" in message
    assert "use a sandbox account or get explicit human approval" in message


@pytest.mark.parametrize("action", ["new", "merge-review"])
def test_every_work_or_review_brief_checks_for_recent_cross_issue_reversals(action):
    """#1069: locally correct cards must not silently oscillate deliberate behavior."""
    directive = {**_DIRECTIVE, "action": action}
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "## Recent cross-issue reversals" in message
    assert "authoritative source for this card's requester" in message
    assert "gh issue view 5 --json title,body,comments,author" in message
    assert "last 30 days of history" in message
    assert "`git blame` or `git log -L`" in message
    assert "`git log -S`" in message
    assert "A commit author is not necessarily the requester" in message
    assert "Name the commit, prior issue, and requesters" in message
    assert "report `needs_more_info`" in message
    assert "unresolved issue link is not itself a conflict" in message


def test_a_merge_review_is_briefed_instead_of_being_sent_at_the_issue(tmp_path):
    """A review dispatch carries no work comment, so "the issue is the brief" would be wrong."""
    directive = {**_DIRECTIVE, "action": "merge-review", "messages": []}
    message = compose_message(
        directive, Workspace(path=Path("/w/issue-5"), branch="b"), owner="eerovil"
    )

    assert "Nothing beyond the issue itself" not in message
    assert "review-fix-pr" in message
    assert "eerovil/store#5" in message
    assert "Do not open a second pull request" in message
    assert "never push to the base branch" in message
    assert "the poller merges once you report `pass`" in message
    # The verdict interface is how it reports, and it must not merge on its own.
    assert "review store 5 gen1 pass --body-file" in message


def test_an_ordinary_dispatch_is_not_given_the_review_brief():
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))
    assert "review-fix-pr" not in message
    assert "fix the login bug" in message
    assert "The poller dispatch owns the issue workflow" in message
    assert "merely because the repository and issue match its description" in message


def test_a_revived_card_tells_its_worker_it_is_finishing_somebody_elses_turn():
    """#835: the engine restarts a card whose worker never reported, so this worker inherits a
    worktree, a branch and possibly a pull request. Told nothing, its most likely first act on
    work that was already finished is to do it again and open a second pull request."""
    directive = {**_DIRECTIVE, "continuation": True}

    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "continuing this card, not starting it" in message
    assert "without posting its terminal report" in message
    # What to do about it: look first, then finish what is missing, on the same pull request.
    assert "Look at what is already there first" in message
    assert "instead of opening a second one" in message
    assert "If a requested repository change is missing" in message
    assert "If the existing pull request only archives investigation results" in message
    assert "do not update it" in message
    assert "put the findings and the pull request link in the terminal issue comment" in message
    # And it must not read leftovers as a verdict either way — that is the #835 constraint.
    assert "not treat the leftovers as proof the work succeeded" in message
    # The original instruction is still there; a continuation adds to the brief, never replaces.
    assert "fix the login bug" in message


def test_a_revived_card_gets_the_old_transcript_as_a_fallback_not_as_reading():
    """#1485: the predecessor's transcript is unbounded, so it is a targeted fallback only.

    A 24-hour audit found at least fifteen sessions that opened by ingesting a previous
    session's whole transcript, some of them inheriting a predecessor that had produced
    nothing. The link still has to be here — a question the issue thread cannot answer needs
    somewhere to go — but the brief must not send the worker there to find out what happened.
    """
    directive = {
        **_DIRECTIVE,
        "continuation": True,
        "previous_session_url": "https://deck.example/sessions/claude_code:main:old",
    }

    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert (
        "https://deck.example/sessions/claude_code:main:old/transcript.json" in message
    )
    assert "Do not read it to find out what happened" in message
    assert "only for a specific question" in message
    # #1228's case survives the bound. That issue added this pointer so a revived worker would
    # recover a choice the user already made instead of re-interviewing them; bounding the
    # default must not quietly put that question back, so it is named as the worked example of
    # a legitimate targeted lookup rather than left to the general permission.
    assert "A choice the user made in that conversation" in message
    assert "rather than asking the user to decide it again" in message
    # The bounded source is named, and named first: the issue thread is where the previous
    # worker actually published its plan, its decisions and its report.
    assert "this card's issue thread" in message
    assert message.index("this card's issue thread") < message.index("only for a specific")
    # And the old instruction to go and read the whole conversation is gone for good.
    assert "Read the previous worker's conversation" not in message


def test_a_revived_card_does_not_render_an_untrusted_session_link():
    directive = {
        **_DIRECTIVE,
        "continuation": True,
        "previous_session_url": "http://127.0.0.1:8756/sessions/secret",
    }

    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "127.0.0.1:8756" not in message
    # Assert on wording the brief still contains when a link *does* render, so this keeps
    # testing the refusal rather than passing because the phrase was reworded (#1485).
    assert "transcript.json" not in message
    assert "Do not read it to find out what happened" not in message
    # The rest of the continuation brief is unaffected by the missing link.
    assert "continuing this card, not starting it" in message


def test_an_ordinary_dispatch_is_not_told_it_is_continuing_anything():
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))
    assert "continuing this card" not in message
    assert "terminal operation is" not in message


def test_a_normal_implementation_pr_closes_its_source_issue():
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        owner="eerovil",
    )

    assert "## Source issue closure" in message
    assert "`Closes #5` when the pull request is in `eerovil/store`" in message
    assert "`Closes eerovil/store#5` when it is in another repository" in message
    assert "`(#123)`, a bare issue reference" in message
    assert "`Follow-up on #123` does not close the issue" in message


def test_partial_work_has_an_explicit_non_closing_opt_out():
    directive = {
        **_DIRECTIVE,
        "messages": [{"payload": "PR intent: non-closing — ships parser only"}],
    }
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "PR intent: non-closing — ships parser only" in message
    assert "`PR intent: non-closing — <reason>`" in message
    assert "do not add a closing keyword" in message
    assert "remove any existing closing keyword for this source issue" in message
    assert "keep the non-closing reason and an ordinary issue link" in message
    assert "Do not infer non-closing intent" in message


def test_both_closing_policies_explain_a_step_pull_request():
    """#2730: a card that needs a merge before it can go on says so with a marker."""
    for policy in ("required", "prohibited"):
        message = compose_message(
            {**_DIRECTIVE, "ref": "store#5", "repo": "store"},
            Workspace(path=Path("/w/issue-5"), branch="b"),
            owner="eerovil",
            closing_keywords={"store": policy},
        )
        assert "### Step pull requests" in message
        assert "`<!-- agentdeck:release=step -->`" in message
        assert "`Part of eerovil/store#5`" in message
        assert "`PR intent: step — <why the rest needs this merged first>`" in message
        assert "never just to make a pull request smaller" in message


def test_followup_and_review_briefs_preserve_the_closing_reference():
    followup = compose_message(
        {**_DIRECTIVE, "action": "followup"},
        Workspace(path=Path("/w/issue-5"), branch="b"),
    )
    review = compose_message(
        {**_DIRECTIVE, "action": "merge-review", "messages": []},
        Workspace(path=Path("/w/issue-5"), branch="b"),
    )

    for message in (followup, review):
        assert "preserve an existing closing reference to this card verbatim" in message
        assert (
            "Review, fix, base-update, and follow-up work must not weaken or remove it"
            in message
        )


def test_every_worker_protects_remote_commits_before_pushing_to_an_existing_pr():
    """#1063: a follow-up may inherit a branch after a reviewer pushed to it remotely."""
    followup = compose_message(
        {**_DIRECTIVE, "action": "followup"},
        Workspace(path=Path("/w/issue-5"), branch="b"),
    )

    assert "Before pushing to an existing pull request" in followup
    assert "fetch its current remote branch" in followup
    assert "git merge-base --is-ancestor FETCH_HEAD HEAD" in followup
    assert "merge that remote head into your branch (`git merge FETCH_HEAD`)" in followup
    assert "Never rewrite or force-push commits already on the remote" in followup
    assert "rebase your local work" not in followup


def test_a_retired_automerge_flag_adds_no_review_to_a_work_dispatch(tmp_path):
    """#2502: no work dispatch reviews and ships its own result, whatever a directive says."""
    directive = {**_DIRECTIVE, "automerge": True}
    message = compose_message(
        directive, Workspace(path=Path("/w/issue-5"), branch="b"), owner="eerovil"
    )

    assert "fix the login bug" in message
    assert "review-fix-pr" not in message
    assert "the poller merges once you report `pass`" not in message
    assert "the verdict above is the terminal operation" not in message


def test_a_spec_only_worker_is_told_to_comment_a_spec_and_wait_for_approval():
    message = compose_message(
        {**_DIRECTIVE, "spec_only": True},
        Workspace(path=Path("/w/issue-5"), branch="b"),
    )

    assert "## Specification-only card" in message
    assert "do not edit any repository file" in message
    assert "commit, push, or open a pull request" in message
    # The specification itself is an ordinary comment; only the handoff is lifecycle (#2155).
    assert "ordinary issue comment with `gh`" in message
    assert "comment store 5 gen1" not in message
    assert "finish store 5 gen1 needs_more_info --body-file" in message
    assert "finish store 5 gen1 in_review --body-file" not in message
    # Closing stays possible, but only on a person's explicit ask (#2661).
    assert "finish store 5 gen1 resolved --body-file" in message
    assert "explicitly asked" in message
    assert "never choose `resolved` on your own judgement" in message
    assert "Do not report `in_review`;" in message
    # Approval in the chat removes the label; the poller starts the build (#2706).
    assert message.count(SPEC_APPROVAL_RULE) == 1
    assert "do not ask anyone to post `/claude`" in message
    assert SPEC_APPROVED_ASK not in message
    assert message.count(SPEC_ONLY_QUESTION_RULE) == 1
    # The numbers are for the issue comment; a chat reply restates the decision (#2779).
    assert "never cite a decision or question by its number alone" in SPEC_ONLY_QUESTION_RULE


def test_a_spec_worker_is_handed_the_brief_to_build_what_it_wrote(tmp_path):
    """The person approves in the spec worker's chat and asks it to build (#2716): the worker
    already holds this Run's build brief, written beside its control command, and its spec
    brief says how to switch to it."""
    dispatcher = _Dispatcher()
    root = tmp_path / "card-control"
    act_on(
        [{**_DIRECTIVE, "spec_only": True, "build_flags": {"ready_for_review": True}}],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
        control_root=root,
    )

    call = dispatcher.calls[0]
    path = Path(call["directive"]["build_brief"])
    wrapper = str(path).removesuffix(".build.md")
    assert path.parent == root and Path(wrapper).exists()
    build = path.read_text(encoding="utf-8")
    # The build brief is an ordinary, compacted implementation brief for the same Run.
    assert SPEC_APPROVED_ASK in build
    assert "## Specification-only card" not in build
    assert "Specification-only" not in build
    assert f"{wrapper} finish in_review --body-file <path>" in build
    assert f"{wrapper} ready-for-review" in build
    # The spec brief, as delivered after compaction, points at it.
    compact = _compact_poller_brief(
        call["directive"], call["message"], cwd=call["cwd"], worktrees=call["worktrees"]
    )
    for message in (call["message"], compact):
        flat = " ".join(message.split())
        assert "`start-implementation --body-file <path>`" in flat
        assert f"`{path}`" in flat
        assert "do not ask anyone to post `/claude`" in flat
        assert f"{wrapper} finish in_review" not in message


def test_a_spec_worker_with_no_wrapper_keeps_the_pollers_build_start(tmp_path):
    dispatcher = _Dispatcher()
    act_on(
        [{**_DIRECTIVE, "spec_only": True}],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
    )

    call = dispatcher.calls[0]
    assert "build_brief" not in call["directive"]
    assert SPEC_APPROVAL_RULE in call["message"]
    assert "start-implementation" not in call["message"]


def test_the_build_of_an_approved_spec_is_told_to_implement_it():
    message = compose_message(
        {**_DIRECTIVE, "spec_approved": True},
        Workspace(path=Path("/w/issue-5"), branch="b"),
    )

    assert SPEC_APPROVED_ASK in message
    assert "## Specification-only card" not in message
    assert "finish store 5 gen1 in_review --body-file" in message


@pytest.mark.parametrize(
    "directive",
    [
        _DIRECTIVE,
        {**_DIRECTIVE, "action": "followup"},
        {**_DIRECTIVE, "continuation": True},
        {**_DIRECTIVE, "action": "merge-review", "messages": []},
    ],
    ids=["new", "followup", "continuation", "merge-review"],
)
def test_no_dispatch_is_told_to_search_for_duplicate_issues(directive):
    """#2155: a dispatched card is authoritative enough to start; duplicates are triage's."""
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "## Duplicate issue check" not in message
    assert "credible duplicate" not in message


def test_the_reviewer_only_dispatch_is_not_told_to_do_work_of_its_own():
    directive = {**_DIRECTIVE, "action": "merge-review", "messages": []}
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "Nothing beyond the issue itself" not in message
    assert "the verdict above is the terminal operation" in message
    assert "A person requested `/merge review`" in message


def test_the_review_brief_says_the_review_runs_once():
    """#1002: told only to be sure, a worker re-reviews its own fixes without bound.

    #994 ran four rounds and 14 reviewers over a one-line label for $43.
    """
    directive = {**_DIRECTIVE, "action": "merge-review", "messages": []}
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "`review-fix-pr` skill **once**" in message
    assert "do not start the review over on the fixed commit" in message
    # Being unsure has somewhere to go that is not another pass.
    assert "another review pass to settle a doubt is itself `changes-needed`" in message


def test_the_review_brief_checks_available_production_data():
    """#1068: runtime evidence must settle data-dependent doubts before a verdict."""
    directive = {**_DIRECTIVE, "action": "merge-review", "messages": []}
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "Before treating uncertainty as `changes-needed`" in message
    assert "report says behavior used to work" in message
    assert "available read-only production query tools" in message
    assert "Do not infer the runtime cause from the shape of the code alone" in message


def test_an_explicit_merge_review_owns_a_review():
    """#963: /merge review has an action and no work of its own; it still owns a review."""
    directive = {**_DIRECTIVE, "action": "merge-review", "messages": []}
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "Nothing beyond the issue itself" not in message
    assert "A person requested `/merge review`" in message
    assert "review-fix-pr" in message
    assert "the verdict above is the terminal operation" in message
    assert "do not report `finish in_review`" in message


def test_a_merge_review_names_every_pull_request_its_verdict_would_release():
    """#1338: three PRs in three repos, and the review covered only the worktree's own."""
    directive = {
        **_DIRECTIVE,
        "ref": "issues#228",
        "repo": "issues",
        "number": 228,
        "action": "merge-review",
        "messages": [],
        "pull_requests": ["docker#80", "store#3054", "abacus#1810"],
    }

    message = compose_message(
        directive, Workspace(path=Path("/w/issue-228"), branch="b"), owner="Northwind"
    )

    assert "This card has 3 open pull requests, and your verdict covers all of them" in message
    for ref in ("docker#80", "store#3054", "abacus#1810"):
        assert f"Northwind/{ref}" in message
    assert "each of the 3 pull requests listed above" in message
    assert "repository your worktree does not contain" in message
    assert "If you cannot review one of them, that is `changes-needed`" in message


def test_a_single_pull_request_review_is_told_which_one_without_a_release_list():
    directive = {
        **_DIRECTIVE,
        "action": "merge-review",
        "messages": [],
        "pull_requests": ["store#9"],
    }

    message = compose_message(
        directive, Workspace(path=Path("/w/issue-5"), branch="b"), owner="eerovil"
    )

    assert "open pull requests, and your verdict covers all" not in message
    assert "against eerovil/store#9" in message


def test_a_review_with_no_discovered_pull_request_still_briefs_the_card():
    """Discovery can be unreadable; the brief must not claim a set the engine cannot confirm."""
    directive = {**_DIRECTIVE, "action": "merge-review", "messages": []}

    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "open pull requests, and your verdict covers all" not in message
    assert "against store#5's open pull request" in message


def test_a_merge_review_does_not_mistake_a_base_sync_for_changes_needed():
    directive = {**_DIRECTIVE, "action": "merge-review", "messages": []}
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "branch-up-to-date check is not `changes-needed`" in message
    assert "Do not merge or rebase the base branch" in message
    assert "effective release content did not change" in message


def test_the_worker_receives_exact_control_commands_instead_of_raw_markers():
    control = "/repo/scripts/poller_engine.py worker-control --manifest m --overlay o --binding b"
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        control_command=control,
    )

    assert f"{control} finish store 5 gen1 in_review --body-file" in message
    assert f"{control} review store 5 gen1 pass --body-file" in message
    # #2155: every command the brief names is lifecycle — `finish` or `review` — and nothing
    # else: no lease check before editing, no comment proxy, no link registration.
    verbs = set(re.findall(re.escape(control) + r" (\S+) store 5 gen1", message))
    assert verbs == {"finish", "review"}, verbs
    assert "There is no separate lease check" in message
    # #2159 test 11: the Runtime ends a stopped turn itself, so nothing asks for a stop-ack.
    assert "stop-ack" not in message
    assert "handoff-report" not in message
    assert "checkpoint" not in message
    assert "there is nothing to acknowledge" in message
    assert "<!-- claude-terminal generation=" not in message


def test_the_worker_is_required_to_attach_visual_evidence_to_the_issue_for_ui_changes():
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "capture screenshots using the repository's screenshot procedure" in message
    assert "exercise the actual interaction or data path" in message
    assert "include the relevant screenshots in the same terminal issue comment" in message
    assert "No visual change" in message
    assert "Do not duplicate visual evidence in the pull request description" in message


def test_screenshots_are_shown_small_and_repeated_in_every_terminal_comment():
    """#2498 reverses #2116's fold: the reader mostly wants the images, so they are open.

    A fixed width keeps them from filling the page, and width rather than height because GitHub
    shrinks a too-wide image to the screen and a fixed height would then squash it. The
    terminal comment still carries them every time: the poller hides its own superseded
    comments, so "screenshots posted previously" points the reader at something collapsed.
    """
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "Put the images inside one collapsed" not in message
    assert "Show the screenshots open, right after the outcome" in message
    assert '<img src="<embed URL>" width="400" alt="<what it shows>">' in message
    assert "Show at most 8" in message
    assert "`More screenshots (N)`" in message
    assert "Embed the images in the terminal comment every time" in message
    assert "Never write that screenshots were posted previously" in message
    assert "hidden as outdated once it lands" in message


def test_screenshots_stay_open_and_repeated_with_an_evidence_repository():
    """The same asks survive the other branch of `visual_evidence_brief` (#2116, #2498).

    A project that named an evidence repository gets a different destination, not a different
    standard — and that branch is the one every AgentDeck card actually takes.
    """
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        artifact_repo="eerovil/shots",
        artifact_workspace=Workspace(
            path=Path("/home/me/shots/.worktrees/issue-5-store-evidence"),
            branch="evidence/store-5",
            base_branch="main",
        ),
    )

    assert '<img src="<embed URL>" width="400" alt="<what it shows>">' in message
    assert "Embed the images in the terminal comment every time" in message


def test_the_terminal_comment_leads_with_the_outcome_and_folds_the_rest():
    """#2498: a 19,600-character report buried the outcome and the images the reader wanted."""
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert message.count("## Comment layout") == 1
    layout = message.index("## Comment layout")
    assert layout < message.index("## Issue review packet")
    assert "at most three plain sentences" in message
    # #2689: the newest report is the one left open, so it carries the whole card's outcome.
    assert "The outcome of the whole card" in message
    assert "lists this turn's fixes under a `Review fixes` line inside Details" in message
    assert "Only what the reader has to act on" in message
    assert (
        "`<details><summary>Details — review surface, verification, notes</summary>`" in message
    )
    assert "when unsure, fold it" in message
    # The packet now opens the fold instead of leading the comment.
    assert "Lead the comment with a one-line `Review surface`" not in message
    assert "Start the collapsed `Details` block from Comment layout with a" in message
    assert "nested at the end of the `Details` block" in message
    assert "In the `Details` block, under `Review surface`, put one `Beyond the card`" in message


def test_an_implementation_worker_walks_the_use_case_to_the_persons_goal():
    """#2919: "Campaign created" with no campaign in the list passes every test and still fails."""
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert message.count("## Use-case walkthrough") == 1
    assert message.index("## Use-case walkthrough") < message.index("## Worker control")
    assert "do not stop at the confirmation" in message
    assert "end only when their goal is met" in message
    assert '"Create campaign"' in message
    assert "never use production" in message
    assert "`Use-case walkthrough: <n> steps, <no gaps | gaps: ...>`" in message
    assert "`Use-case walkthrough: not applicable — <reason>`" in message
    # #2919 review: Comment layout must place that line open, not fold it with "everything else".
    layout = message[message.index("## Comment layout") : message.index("## Issue review packet")]
    assert "3. The one `Use-case walkthrough:` line, open" in layout
    assert layout.index("2. The screenshots") < layout.index("3. The one `Use-case walkthrough:`")


@pytest.mark.parametrize(
    "directive",
    [{**_DIRECTIVE, "spec_only": True}, {**_DIRECTIVE, "action": "merge-review", "messages": []}],
    ids=["spec-only", "merge-review"],
)
def test_turns_that_write_no_code_get_no_walkthrough_section(directive):
    message = compose_message(directive, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "## Use-case walkthrough" not in message


def test_a_reviewer_checks_for_the_use_case_walkthrough():
    message = compose_message(
        {**_DIRECTIVE, "action": "merge-review", "messages": []},
        Workspace(path=Path("/w/issue-5"), branch="b"),
    )

    assert "use-case walkthrough that follows the person to their goal" in message
    assert "one that stops at a success message" in message


def test_screenshots_go_to_the_evidence_repo_when_the_project_named_one():
    """#1791: keep images out of the repository the card ships from."""
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        artifact_repo="eerovil/shots",
        artifact_workspace=Workspace(
            path=Path("/home/me/shots/.worktrees/issue-5-store-evidence"),
            branch="evidence/store-5",
            base_branch="main",
        ),
    )

    # The ask itself is unchanged; only where the images land is.
    assert "capture screenshots using the repository's screenshot procedure" in message
    assert "Commit them to `eerovil/shots`" in message
    # This card's own worktree of the evidence repo, never the shared clone.
    assert "/home/me/shots/.worktrees/issue-5-store-evidence" in message
    assert "yours alone — no other card writes in it" in message
    # Named per card, so one evidence repo can hold several projects without collisions.
    assert "under `store/issue-5/`" in message
    assert "push origin HEAD:main" in message
    assert "pull --rebase origin main" in message
    assert "Never commit screenshots in your code worktree" in message
    # And the embed points at the evidence repo, not at the code repo.
    assert "https://github.com/eerovil/shots/blob/<commit-sha>/<path>?raw=1" in message
    assert "https://github.com/<owner>/<repo>/blob/<commit-sha>/<path>?raw=1" not in message


def test_a_card_with_no_evidence_worktree_commits_screenshots_where_it_always_did():
    """A shared clone is never offered as a substitute for this card's own worktree."""
    unprepared = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        artifact_repo="eerovil/shots",  # named, but no worktree could be prepared
    )
    unset = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    for message in (unprepared, unset):
        assert "https://github.com/<owner>/<repo>/blob/<commit-sha>/<path>?raw=1" in message
        assert "eerovil/shots" not in message


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Poller",
    "GIT_AUTHOR_EMAIL": "poller@example.invalid",
    "GIT_COMMITTER_NAME": "Poller",
    "GIT_COMMITTER_EMAIL": "poller@example.invalid",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/nonexistent",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=_GIT_ENV, capture_output=True, text=True, check=check
    )


def _evidence_origin(tmp_path):
    """A bare evidence repository with one commit on `main`, and a host clone of it."""
    origin = tmp_path / "shots.git"
    _git("init", "--bare", "--initial-branch=main", str(origin), cwd=tmp_path)
    seed = tmp_path / "seed"
    _git("clone", str(origin), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("evidence\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    checkout = tmp_path / "shots"
    _git("clone", str(origin), str(checkout), cwd=tmp_path)
    return origin, checkout


def _evidence_worktree(checkout, repo, number):
    """What the runtime's artifact claim makes for one card: its own worktree and branch."""
    name, branch = f"issue-{number}-{repo}-evidence", f"evidence/{repo}-{number}"
    path = checkout / ".worktrees" / name
    assert GitHost().create(str(checkout), str(path), branch, "main") == ""
    return Workspace(path=path, branch=branch, base_branch="main", work_claimed=True)


def test_two_cards_writing_evidence_at_once_cannot_capture_each_others_files(tmp_path):
    """#1791 review: a shared checkout is one index and one working tree for every card.

    Exercised against real git, because the failure is git's own behavior: staging a directory
    while a neighbour's file sits uncommitted in the same tree, and rebasing on top of it.
    """
    origin, checkout = _evidence_origin(tmp_path)

    first = _evidence_worktree(checkout, "store", 5)
    second = _evidence_worktree(checkout, "store", 6)
    assert first.path != second.path
    assert first.base_branch == "main" and second.base_branch == "main"

    # Both workers have uncommitted screenshots on disk at the same time — the situation a
    # shared checkout could not survive.
    for workspace, card in ((first, 5), (second, 6)):
        directory = workspace.path / "store" / f"issue-{card}"
        directory.mkdir(parents=True)
        (directory / "shot.png").write_bytes(b"png")
    assert _git("status", "--porcelain", cwd=first.path).stdout.strip() == "?? store/"
    assert _git("status", "--porcelain", cwd=second.path).stdout.strip() == "?? store/"

    # Each stages and commits everything it can see, and still commits only its own file.
    for workspace, card in ((first, 5), (second, 6)):
        _git("add", "-A", cwd=workspace.path)
        _git("commit", "-m", f"evidence: store#{card}", cwd=workspace.path)
        committed = _git("show", "--name-only", "--format=", cwd=workspace.path).stdout.split()
        assert committed == [f"store/issue-{card}/shot.png"]

    # The first push wins; the second is rejected and recovers by rebasing its own commit.
    _git("push", "origin", "HEAD:main", cwd=first.path)
    rejected = _git("push", "origin", "HEAD:main", cwd=second.path, check=False)
    assert rejected.returncode != 0
    _git("pull", "--rebase", "origin", "main", cwd=second.path)
    _git("push", "origin", "HEAD:main", cwd=second.path)

    # Both cards' evidence is on the default branch, and neither lost the other's.
    landed = _git("ls-tree", "-r", "--name-only", "main", cwd=origin).stdout.split()
    assert "store/issue-5/shot.png" in landed
    assert "store/issue-6/shot.png" in landed


def test_the_evidence_commands_still_run_when_the_host_path_contains_spaces(tmp_path):
    """#1791 re-review: an Overlay checkout is a path an operator wrote, spaces and all.

    The commands are pasted into a shell by the worker, so this runs the emitted lines rather
    than only reading them: an unquoted path splits into two argv words and `git` fails.
    """
    root = tmp_path / "host dir with spaces"
    root.mkdir()
    origin, checkout = _evidence_origin(root)
    workspace = _evidence_worktree(checkout, "store", 5)
    assert " " in str(workspace.path)

    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        artifact_repo="eerovil/shots",
        artifact_workspace=workspace,
    )
    commands = [
        line.strip() for line in message.splitlines() if line.startswith("    git -C ")
    ]
    assert len(commands) == 4
    # The worktree survives as one argument rather than being split on its spaces.
    for command in commands:
        assert shlex.split(command)[2] == str(workspace.path)

    directory = workspace.path / "store" / "issue-5"
    directory.mkdir(parents=True)
    (directory / "shot.png").write_bytes(b"png")
    for command in commands[:3]:  # add, commit, push — the rebase line is the retry path
        done = subprocess.run(
            command, shell=True, cwd=str(tmp_path), env=_GIT_ENV, capture_output=True, text=True
        )
        assert done.returncode == 0, f"{command}\n{done.stderr}"

    landed = _git("ls-tree", "-r", "--name-only", "main", cwd=origin).stdout.split()
    assert landed == ["README.md", "store/issue-5/shot.png"]


def test_each_card_gets_its_own_evidence_branch_so_two_worktrees_never_collide(tmp_path):
    """git refuses a second worktree on a branch already checked out."""
    from agentdeck.poller_adapter.runner import _evidence_workspace

    _, checkout = _evidence_origin(tmp_path)
    works = _RecordingWorks()

    first = _evidence_workspace(_DIRECTIVE, checkout, "eerovil/shots", "store", works)
    second = _evidence_workspace(_DIRECTIVE, checkout, "eerovil/shots", "issues", works)
    assert first is not None and second is not None
    # Same issue number in two repositories, which is routine on a multi-repo project.
    assert first.branch == "evidence/store-5"
    assert second.branch == "evidence/issues-5"
    assert first.path != second.path
    assert first.work_claimed and second.work_claimed
    # Without the runtime's service there is no card-owned evidence worktree to fall back to.
    assert _evidence_workspace(_DIRECTIVE, checkout, "eerovil/shots", "store", None) is None


def test_the_worker_is_given_an_embed_form_a_private_repo_reviewer_can_actually_see():
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "https://github.com/<owner>/<repo>/blob/<commit-sha>/<path>?raw=1" in message
    assert "raw.githubusercontent.com" in message
    assert "branch ref dies when the branch is deleted" in message


def test_every_worker_puts_the_final_review_packet_in_the_terminal_issue_comment():
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "## Issue review packet" in message
    assert "terminal worker-control comment on the issue" in message
    assert "actual base branch" in message
    assert "after all fixes are committed" in message
    assert "When no pull request exists because no repository change was needed" in message
    assert "report a zero diff" in message
    assert "investigation produced no repository change" in message
    assert "body file passed to `finish` or `review`" in message
    assert "one-line `Review surface`" in message
    assert "source, tests, docs, and generated files" in message
    assert "source-file diffs verbatim in review-priority order" in message
    assert "at most 150 diff lines" in message
    assert "250 diff lines and 25,000 characters" in message
    # One wrapper, not one collapsed block per file: the packet is what made poller threads
    # unreadable, and ten collapsed headers still cost a reader a screenful (#2116).
    assert "one outer collapsed `<details>` block" in message
    assert "Full diff — N files, M lines" in message
    assert "nested inside that wrapper" in message
    assert "Not inlined — review these in the Files changed tab" in message
    assert "whether it exceeded the per-file or packet limit" in message
    assert "Do not inline tests, docs, or generated files" in message
    assert "No source code changed." in message
    assert "terminal comment's short human summary" in message
    assert "Build it through the terminal body file" in message
    assert "Keep the pull request description concise" in message
    assert "do not duplicate the review packet there" in message


def test_the_terminal_comment_names_work_the_card_never_asked_for():
    """#1104: a card said "triage and fix this Sentry error" and what shipped also changed
    refund behaviour on purpose. The worker mentioned it in prose; nothing required the
    reader to be shown it as scope the card never asked for."""
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "`Beyond the card` line" in message
    assert "anything in the change the card did not ask for" in message
    assert "whether it could have shipped separately" in message
    assert "`Beyond the card: nothing`" in message
    # Disclosure is not authorisation, and the fix is not to delete the work.
    assert "not the same as having been asked for it" in message
    assert "Do not unwind that work to make the line empty" in message


def test_a_normal_run_starts_without_any_plan_comment_ceremony():
    """#2155: the deck shows the Run as it happens, so no turn owes a GitHub plan comment."""
    for action in ("new", "followup"):
        message = compose_message(
            {**_DIRECTIVE, "action": action}, Workspace(path=Path("/w/issue-5"), branch="b")
        )

        assert "## Plan comment" not in message
        assert "post one short plan comment" not in message
        assert "Say the same plan in your own chat too" not in message
        assert "No plan comment is required" in message


def test_the_session_link_goes_in_the_pull_request_and_is_never_registered():
    """#2155: the deck correlates Work, chat and pull request, so nothing is registered."""
    control = "/repo/scripts/poller_engine.py worker-control --manifest m --overlay o --binding b"
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        control_command=control,
    )

    assert "public AgentDeck session URL" in message
    assert "pull request description if it is absent there" in message
    assert "Never publish the loopback `AGENTDECK_URL`" in message
    assert "nothing to register with the poller" in message
    assert "## Comment links" not in message
    assert f"{control} links" not in message
    assert "--session-url" not in message
    assert "--pull-request-url" not in message


def test_a_normal_work_workspace_needs_no_worktree_registration():
    """#2155: the Work owns its workspace (#2149), so the ordinary brief registers nothing."""
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        register_command="poller-engine register-worktree --generation gen1 store 5",
    )

    assert "register-worktree" not in message
    assert "then register it right away" not in message


def test_a_worktree_made_in_another_project_repository_is_still_registered():
    """The one case the Work does not own: a checkout the worker makes in a sibling repository."""
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        register_command="poller-engine register-worktree --generation gen1 store 5",
        code_checkouts={"store": "/w/store", "store-fork": "/w/store-fork"},
    )

    section = message[message.index("## Other code this project owns"):]
    assert "create it first with `git worktree add`, then register it right away" in section
    command = "register-worktree --generation gen1 store 5 /absolute/path/to/worktree"
    assert command in section
    # A command a worker can copy: nothing may share its line, or the paste runs as arguments.
    line = next(part for part in message.splitlines() if command in part)
    assert line.strip().endswith("/absolute/path/to/worktree"), line


def test_only_json_directives_are_read_from_the_stream():
    """A stray line must never abort a wake that already made durable decisions."""
    stream = "\n".join([
        json.dumps(_DIRECTIVE),
        "cycle: something human-readable slipped onto stdout",
        "{not json}",
        json.dumps({"no_ref": True}),
        "",
    ])
    assert [d["ref"] for d in read_directives(stream)] == ["store#5"]


def test_the_default_branch_comes_from_the_checkout(tmp_path):
    """#1166: a repository on `main` had its worktree created from `origin/master`, which does
    not exist there, and the card reached Blocked saying only that a delivery could not be
    prepared."""
    seen = []

    def runner(command, cwd=None, timeout=300.0, env=None):
        seen.append(command)
        return SimpleNamespace(returncode=0, stdout="origin/main\n", stderr="")

    assert default_branch(tmp_path / "songs", runner=runner) == "main"
    assert seen[0][-1] == "refs/remotes/origin/HEAD"


@pytest.mark.parametrize(
    "returncode, stdout",
    [(1, ""), (0, ""), (0, "origin/\n"), (0, "refs/heads/main\n")],
    ids=["no origin/HEAD", "empty", "bare prefix", "unexpected shape"],
)
def test_an_unreadable_default_branch_keeps_the_fallback(tmp_path, returncode, stdout):
    """A checkout cloned single-branch, or with a remote added by hand, has no `origin/HEAD`.
    That is not a reason to guess: it keeps the caller's fallback, which is what every project
    already on `master` resolves to anyway."""
    def runner(command, cwd=None, timeout=300.0, env=None):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    assert default_branch(tmp_path / "repo", fallback="master", runner=runner) == "master"


def _git_ok(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.parametrize("first_branch", ["main", "master"])
def test_a_clone_of_an_empty_repository_finds_its_first_branch(tmp_path, first_branch):
    """#2778: a new project's repository was cloned while still empty, so the clone never
    recorded `origin/HEAD`. Its first push made `origin/main`, the fallback said `master`, and
    no card in that project could be prepared."""
    remote = tmp_path / "remote.git"
    _git_ok("init", "--bare", "--quiet", str(remote), cwd=tmp_path)
    clone = tmp_path / "clone"
    _git_ok("clone", "--quiet", str(remote), str(clone), cwd=tmp_path)
    _git_ok("checkout", "--quiet", "-b", first_branch, cwd=clone)
    _git_ok("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "--allow-empty",
         "-m", "start", cwd=clone)
    _git_ok("push", "--quiet", "origin", first_branch, cwd=clone)

    assert default_branch(clone, fallback="master") == first_branch


def test_a_checkout_with_no_origin_head_prefers_its_existing_fallback(tmp_path):
    """Both `origin/master` and `origin/main` and no `origin/HEAD`: the configured fallback
    still wins, so no project already resolving to `master` changes base."""
    present = {"refs/remotes/origin/master", "refs/remotes/origin/main"}

    def runner(command, cwd=None, timeout=300.0, env=None):
        found = command[-1] in present and "rev-parse" in command
        return SimpleNamespace(returncode=0 if found else 1, stdout="", stderr="")

    assert default_branch(tmp_path / "repo", fallback="master", runner=runner) == "master"


def test_a_refused_workspace_names_its_base_and_the_runtimes_reason(tmp_path):
    """#2778: the skip line said only "could not prepare the delivery", and a person went
    looking at accounts while the real fault was a base branch that did not exist."""

    class Refusing:
        def ensure(self, directive, **kwargs):
            return ClaimedWorkspace(detail="failed: invalid reference: origin/master")

    result = act_on(
        [_DIRECTIVE],
        dispatcher=_Dispatcher(),
        checkouts={"store": tmp_path / "store"},
        work_workspaces=Refusing(),
    )
    assert result.skipped[0].endswith(
        "could not prepare the delivery — base `origin/master`: "
        "failed: invalid reference: origin/master"
    )


def test_the_adapter_bases_a_new_worktree_on_the_checkouts_own_default(tmp_path):
    """The whole point of #1166: what the Work's claim is asked to base a worktree on."""
    checkout = tmp_path / "songs"
    checkout.mkdir()
    calls = []
    adapter = SingleCheckoutWorkspaceAdapter(
        checkouts={"songs": checkout},
        work_workspaces=_recording_prepare(calls),
        read_default_branch=lambda path, fallback="master": "main",
    )

    prepared = adapter.prepare({"repo": "songs", "number": 11})

    assert calls == [(checkout, 11, "agent/issue-11", "main", "")]
    # And the worker is told not to push to the branch that actually exists.
    assert "Never push to main" in compose_message({"ref": "songs#11", "number": 11}, prepared)


def test_the_checkouts_default_is_read_once_per_wake(tmp_path):
    """A wake can carry several directives into one repository, and the answer cannot change
    under them — so asking git per directive would be pure cost."""
    checkout = tmp_path / "songs"
    checkout.mkdir()
    reads = []
    adapter = SingleCheckoutWorkspaceAdapter(
        checkouts={"songs": checkout},
        work_workspaces=_recording_prepare([]),
        read_default_branch=lambda path, fallback="master": reads.append(path) or "main",
    )

    adapter.prepare({"repo": "songs", "number": 11})
    adapter.prepare({"repo": "songs", "number": 12})

    assert reads == [checkout]


def _recording_prepare(calls):
    return _RecordingWorks(calls)


def test_a_borrowed_checkout_namespaces_its_worktree_and_default_branch(tmp_path):
    """Issue numbers are unique per repository, not per directory. Two repo ids sharing one
    checkout must not be able to name the same worktree or branch (#1087)."""
    checkout = tmp_path / "docker"
    checkout.mkdir()
    calls = []
    adapter = SingleCheckoutWorkspaceAdapter(
        checkouts={"issues": checkout, "docker": checkout},
        borrowed={"issues": "docker"},
        work_workspaces=_recording_prepare(calls),
    )

    borrowing = adapter.prepare({"repo": "issues", "number": 5})
    owning = adapter.prepare({"repo": "docker", "number": 5})

    assert borrowing is not None and owning is not None
    assert borrowing.cwd.name == "issue-5-issues"
    assert owning.cwd.name == "issue-5"
    assert borrowing.cwd != owning.cwd
    assert borrowing.branch == "agent/issue-5-issues"
    assert owning.branch == "agent/issue-5"
    assert calls == [
        (checkout, 5, "agent/issue-5-issues", "master", "-issues"),
        (checkout, 5, "agent/issue-5", "master", ""),
    ]
    assert borrowing.borrowed_from == "docker"
    assert owning.borrowed_from == ""


def test_a_borrowed_checkout_preserves_an_existing_pull_request_branch(tmp_path):
    """A follow-up turn has to land on the branch its pull request already uses, so a branch the
    engine named wins over the namespaced default."""
    checkout = tmp_path / "docker"
    checkout.mkdir()
    calls = []
    adapter = SingleCheckoutWorkspaceAdapter(
        checkouts={"issues": checkout},
        borrowed={"issues": "docker"},
        work_workspaces=_recording_prepare(calls),
    )

    adapter.prepare({"repo": "issues", "number": 5, "branch": "agent/issue-5-existing"})

    assert calls == [(checkout, 5, "agent/issue-5-existing", "master", "-issues")]


def test_a_borrowing_worker_opens_only_the_implementation_pull_request(tmp_path):
    """The borrowed checkout is a starting point, not a reason to make a companion docs PR.

    Trailhead tracker cards begin in docker, but the actual product change may belong to Abacus.
    The terminal issue report already carries the diagnosis and evidence (#1209).
    """
    checkout = tmp_path / "docker"
    checkout.mkdir()
    adapter = SingleCheckoutWorkspaceAdapter(
        checkouts={"issues": checkout},
        borrowed={"issues": "docker"},
        work_workspaces=_recording_prepare([]),
    )
    prepared = adapter.prepare({"repo": "issues", "number": 5})

    text = compose_message({"ref": "issues#5", "number": 5}, prepared, owner="trailhead")

    assert "If the card requires a repository change" in text
    assert "repository that owns the requested implementation" in text
    assert "may be docker or another repository you inspect" in text
    assert "leave this starting branch unchanged" in text
    assert "do not create a companion pull request in docker" in text
    assert "Reference the card as trailhead/issues#5" in text
    assert "rather than as a bare issue number" in text


def test_investigation_results_always_stay_on_the_issue():
    """#1209: findings are issue evidence, never the sole content of a pull request."""
    text = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "## Investigation results" in text
    assert "Never open or update a pull request whose sole purpose" in text
    assert "investigation, diagnosis, research, findings, or evidence" in text
    assert "Put those results in the terminal issue comment" in text
    assert "even when the card asks you to investigate or document the findings" in text
    assert (
        "If no requested repository change is needed, end the turn without a pull request" in text
    )
    assert "If the card requires a repository change, open a pull request" in text
    assert "explicitly requests a change to repository documentation" in text
    assert "it does not archive the investigation" in text


def test_the_wake_does_not_drop_directives_over_an_unvalidated_extension(tmp_path, monkeypatch):
    """The wake checks the config to find out where a worker may go. Doing that without
    locating the extension programs reports every enabled extension as missing, and the
    directives the engine just produced are dropped for a reason that has nothing to do with
    them — the same defect #245 fixed in `activate`, repeated in the runner.
    """
    import subprocess

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import poller_wake

    root = tmp_path / "poller"
    instance = root / "instances" / "demo"
    instance.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    (checkout / "poller" / "extensions").mkdir(parents=True)
    (checkout / "poller" / "extensions" / "probe.py").write_text(
        "import json, sys\n"
        'hook = sys.argv[1] if len(sys.argv) > 1 else ""\n'
        "sys.stdin.read()\n"
        'if hook == "hooks":\n'
        '    print(json.dumps({"hooks": ["validate-config"]}))\n'
        'elif hook == "validate-config":\n'
        '    print(json.dumps({"problems": []}))\n'
        "else:\n"
        "    sys.exit(2)\n"
    )
    (instance / "poller.toml").write_text(
        "contract = 1\n\n[board]\norg = \"example\"\nproject_number = 7\n\n"
        "[board.statuses]\ntodo = \"Todo\"\nin_progress = \"In Progress\"\n"
        "in_review = \"In review\"\ndone = \"Done\"\n"
        "needs_more_info = \"Needs More Info\"\nblocked = \"Blocked\"\n\n"
        "[[repos]]\nid = \"store\"\nholds_issues = true\nholds_code = true\n"
        "verification = \"agentdeck_job\"\n\n"
        '[extensions.probe]\nsetting = "value"\n'
    )
    (instance / "overlay.toml").write_text(
        "contract = 1\n\n"
        f'[paths]\nstate = "{instance / "state.json"}"\n\n'
        f'[checkouts]\nstore = "{checkout}"\n\n'
        '[worker]\naccount = "claude_code:main"\n'
    )

    directive = dict(_DIRECTIVE)

    real_run = subprocess.run

    def fake_engine(command, **kwargs):
        # Only the engine invocation is faked; the extension probe must really run, or this
        # test would not exercise discovery at all.
        if any("poller_engine.py" in str(part) for part in command):
            return subprocess.CompletedProcess(command, 0, json.dumps(directive) + "\n", "")
        return real_run(command, **kwargs)

    delivered = []
    monkeypatch.setattr(poller_wake.subprocess, "run", fake_engine)
    monkeypatch.setattr(
        poller_wake, "act_on",
        lambda directives, **kwargs: delivered.append(directives) or _Result(),
    )

    code = poller_wake.main(["--root", str(root), "--instance", "demo"])
    assert code == 0, "the wake refused a config whose extension is present"
    assert delivered and delivered[0][0]["ref"] == "store#5"


class _Result:
    log: list = []
    skipped: list = []

    def summary(self):
        return "1 directive(s)"


def test_the_worker_is_told_how_to_report_that_it_finished():
    """The poller cannot observe a worker stopping. It waits for one lease-bound comment, and a
    worker that never posts it leaves the card In progress until the engine re-dispatches the
    work to a second worker — which is what happened on the first real card driven end to end.

    The generation is what binds the report to this dispatch, so it has to be in the prompt.
    """
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert f"finish store 5 {_DIRECTIVE['generation']} in_review" in message
    # The other two outcomes a worker can honestly report.
    assert "needs_more_info" in message and "blocked" in message
    assert "raw engine markers" in message


def test_every_status_the_prompt_offers_resolves_to_a_board_column():
    """Every outcome offered in prose is an exact status accepted by the shipped CLI.

    `status=in_review` once became "in review", which is not the board's "In review", so the
    recovered status write missed its option id and failed silently on every wake (#279). The
    prompt no longer asks for a raw marker, so this half checks the token the prompt hands the
    worker really parses; the rest of that seam — token to rendered marker to board role — is
    walked in `test_poller_engine_worker_control.py`.
    """
    import re

    from agentdeck.poller_engine import __main__ as cli

    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))
    tokens = set(re.findall(r"finish store 5 gen1 ([a-z_]+) --body-file", message))
    assert tokens == {"in_review", "needs_more_info", "blocked", "resolved"}
    for token in tokens:
        parsed = cli.build_parser().parse_args([
            "worker-control", "--manifest", "m", "--overlay", "o", "--binding", "b",
            "finish", "store", "5", "gen1", token, "--body-file", "body.md",
        ])
        assert parsed.status == token


def test_the_report_survives_an_instruction_that_says_to_post_one_comment():
    """The card that wedged was told "post one comment, then stop", which read as forbidding the
    report. The instruction is the operator's and is passed through verbatim, so the prompt has
    to settle the conflict itself (#279)."""
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "required even when the instruction above says" in message
    # And it says so *after* the terminal command, where a worker reading top-down meets it in time.
    assert message.index("required even when") > message.index("finish store 5 gen1 in_review")


def test_the_reporting_command_is_exactly_what_the_engine_cli_parses():
    """The worker gets an executable command, not prose it must translate into a marker."""
    import shlex

    from agentdeck.poller_engine import __main__ as cli

    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))
    line = next(part.strip() for part in message.splitlines() if " finish " in part)
    args = shlex.split(line)
    parsed = cli.build_parser().parse_args([
        "worker-control", "--manifest", "m", "--overlay", "o", "--binding", "b", *args[2:]
    ])
    assert parsed.worker_operation == "finish"
    assert parsed.generation == _DIRECTIVE["generation"]


def test_a_card_started_by_its_label_is_told_to_read_the_issue(tmp_path):
    """A label-driven start carries no instruction, and that is a real start (#311).

    The old poller was driven this way: label an issue, and its first step was to read the issue.
    The prompt used to say "- (no instruction given)" and stop there, which is a worker with no
    idea what it is for. Reading the issue beats inlining its body — comments and images are part
    of the ask and a body is not.
    """
    bare = {**_DIRECTIVE, "messages": []}
    message = compose_message(bare, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "(no instruction given)" not in message
    assert "started by its label" in message
    # `--json` rather than `--comments`: the human rendering has returned exit 0 with zero
    # bytes for a real issue, and JSON output is never legitimately empty (#787).
    assert "gh issue view 5 --json title,body,comments,author" in message
    # The exit code carries no signal for that failure, so the brief itself must.
    assert "failed read, not an empty issue" in message
    # A 404 is a missing grant to report, never a reason to retry as the host login (#2289).
    assert "never retry as another identity" in message
    assert "env -u GH_TOKEN" not in message
    # It must still be unmistakable which card this is.
    assert "store#5" in message


def test_a_label_started_card_reads_from_its_qualified_issue_repository():
    bare = {**_DIRECTIVE, "ref": "issues#140", "number": 140, "messages": []}
    message = compose_message(
        bare,
        Workspace(path=Path("/w/issue-140"), branch="b"),
        owner="Northwind",
    )

    assert (
        "gh issue view 140 --repo Northwind/issues "
        "--json title,body,comments,author"
    ) in message


def test_a_label_started_card_quotes_unexpected_repository_syntax():
    import shlex

    bare = {
        **_DIRECTIVE,
        "ref": "Northwind/issues; touch /tmp/pwn#140",
        "number": 140,
        "messages": [],
    }
    message = compose_message(bare, Workspace(path=Path("/w/issue-140"), branch="b"))
    command = next(
        line.strip() for line in message.splitlines() if line.strip().startswith("gh issue view")
    )

    assert shlex.split(command) == [
        "gh", "issue", "view", "140", "--repo",
        "Northwind/issues; touch /tmp/pwn",
        "--json", "title,body,comments,author",
    ]


def test_an_instruction_still_wins_over_the_issue(tmp_path):
    """The fallback must not leak into the ordinary case: an instruction is the brief."""
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))

    assert "fix the login bug" in message
    assert "started by its label" not in message


# --- the card reference the worker (and the deck) is given (#310) ---

def test_the_brief_names_the_card_with_its_owner_when_the_host_knows_it():
    """The engine names a card `repo#n`; unqualified, nothing downstream can resolve it."""
    message = compose_message(
        _DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"), owner="eerovil"
    )
    assert "You are working eerovil/store#5 for the AgentDeck poller." in message


def test_an_unknown_owner_leaves_the_engines_own_reference_alone():
    message = compose_message(_DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"))
    assert "You are working store#5 for the AgentDeck poller." in message


def test_the_owner_comes_from_the_checkouts_own_remote(tmp_path):
    calls: list[list[str]] = []

    def runner(command, cwd=None, timeout=300.0, env=None):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="git@github.com:eerovil/agentdeck.git\n")

    assert owner_of(tmp_path, runner=runner) == "eerovil"
    assert calls == [["git", "-C", str(tmp_path), "remote", "get-url", "origin"]]


def test_a_checkout_with_no_usable_remote_yields_no_owner(tmp_path):
    def failing(command, cwd=None, timeout=300.0, env=None):
        return SimpleNamespace(returncode=128, stdout="")

    def not_github(command, cwd=None, timeout=300.0, env=None):
        return SimpleNamespace(returncode=0, stdout="/srv/mirrors/agentdeck.git\n")

    assert owner_of(tmp_path, runner=failing) is None
    assert owner_of(tmp_path, runner=not_github) is None


def test_the_wake_asks_for_stops_even_when_there_are_no_directives(tmp_path, monkeypatch):
    """A card waiting on a stop produces no directive (#279, #2159).

    A wake that returned at `no directives this wake` before asking the Runtime to stop the
    Runs the engine waits on would leave every such card waiting forever. How a Run ended is
    no longer this host's to record at all: the engine reads it from the Runtime (#2200).
    """
    import subprocess

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import poller_wake

    root = tmp_path / "poller"
    instance = root / "instances" / "demo"
    instance.mkdir(parents=True)
    (instance / "poller.toml").write_text(
        "contract = 1\n\n[board]\norg = \"example\"\nproject_number = 7\n\n"
        "[board.statuses]\ntodo = \"Todo\"\nin_progress = \"In Progress\"\n"
        "in_review = \"In review\"\ndone = \"Done\"\n"
        "needs_more_info = \"Needs More Info\"\nblocked = \"Blocked\"\n\n"
        "[[repos]]\nid = \"store\"\nholds_issues = true\nholds_code = true\n"
        "verification = \"agentdeck_job\"\n"
    )
    (instance / "overlay.toml").write_text(
        "contract = 1\n\n"
        f'[paths]\nstate = "{instance / "state.json"}"\n\n'
        f'[checkouts]\nstore = "{tmp_path / "checkout"}"\n\n'
        '[worker]\naccount = "claude_code:main"\n'
    )

    real_run = subprocess.run

    def fake_engine(command, **kwargs):
        if any("poller_engine.py" in str(part) for part in command):
            # A wake with nothing to dispatch: exactly the shape the wedge produces.
            return subprocess.CompletedProcess(command, 0, "", "no directives this wake\n")
        return real_run(command, **kwargs)

    monkeypatch.setattr(poller_wake.subprocess, "run", fake_engine)
    asked: list[dict] = []
    monkeypatch.setattr(
        poller_wake.turns, "stop_runs",
        lambda **kwargs: asked.append(kwargs) or ["store#5: runtime stopped Run gen-1"],
    )

    code = poller_wake.main(["--root", str(root), "--instance", "demo"])

    assert code == 0
    assert len(asked) == 1, "the stops must be asked before the no-directives return"
    assert asked[0]["owner"] == "example"
    assert not hasattr(poller_wake.turns, "sweep")


# --- the wake refuses a prompt the engine cannot honor (#383) ---

def _wake_module():
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import poller_wake
    return poller_wake


def _wake_instance(tmp_path) -> Path:
    """A minimal but *valid* instance: the wake reads its config before it dispatches."""
    root = tmp_path / "poller"
    instance = root / "instances" / "demo"
    instance.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    (checkout / "poller" / "extensions").mkdir(parents=True)
    (instance / "poller.toml").write_text(
        "contract = 1\n\n[board]\norg = \"example\"\nproject_number = 7\n\n"
        "[board.statuses]\ntodo = \"Todo\"\nin_progress = \"In Progress\"\n"
        "in_review = \"In review\"\ndone = \"Done\"\n"
        "needs_more_info = \"Needs More Info\"\nblocked = \"Blocked\"\n\n"
        "[[repos]]\nid = \"store\"\nholds_issues = true\nholds_code = true\n"
        "verification = \"agentdeck_job\"\n"
    )
    (instance / "overlay.toml").write_text(
        "contract = 1\n\n"
        f'[paths]\nstate = "{instance / "state.json"}"\n\n'
        f'[checkouts]\nstore = "{checkout}"\n\n'
        '[worker]\naccount = "claude_code:main"\n'
    )
    return root


def test_the_wake_runs_the_engine_from_its_own_checkout(tmp_path, monkeypatch):
    """One deploy selects the wake and the engine together (#2153): no release pointer."""
    poller_wake = _wake_module()
    root = _wake_instance(tmp_path)
    ran = []

    def engine(command, **kwargs):
        ran.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(poller_wake.subprocess, "run", engine)

    assert poller_wake.main(["--root", str(root), "--instance", "demo"]) == 0
    checkout = Path(poller_wake.__file__).resolve().parent.parent
    assert poller_wake.ENGINE == checkout / "scripts" / "poller_engine.py"
    assert ran[0][:2] == [str(checkout / "scripts" / "poller_engine.py"), "cycle"]
    assert not (root / "current").exists()


def test_a_wake_with_an_unreadable_overlay_still_reaches_the_engine(tmp_path, monkeypatch):
    """Repointing wrappers must never be the reason a wake dies before its cycle."""
    poller_wake = _wake_module()
    root = _wake_instance(tmp_path)
    (root / "instances" / "demo" / "overlay.toml").write_text("this is [not toml")
    ran = []
    monkeypatch.setattr(
        poller_wake.subprocess,
        "run",
        lambda command, **kwargs: ran.append(command)
        or subprocess.CompletedProcess(command, 78, "", "refused"),
    )

    assert poller_wake.main(["--root", str(root), "--instance", "demo"]) == 78
    assert ran and ran[0][1] == "cycle"


def test_every_command_the_wake_hands_a_worker_is_one_its_engine_accepts(
    tmp_path, monkeypatch
):
    """The worker brief and the engine come from one checkout, so they cannot disagree (#2153).

    This replaces the probe that compared a checkout's prompt against a separately promoted
    artifact (#382): the commands are checked here, against this checkout's own parser.
    """
    import shlex

    from agentdeck.poller_engine.__main__ import build_parser

    poller_wake = _wake_module()
    root = _wake_instance(tmp_path)
    monkeypatch.setattr(
        poller_wake.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps({**_DIRECTIVE, "work_id": "github_issue:x/store#5"}) + "\n", ""
        ),
    )
    handed = {}

    def capture(directives, **kwargs):
        handed["register"] = kwargs["register_command"](directives[0])
        handed["control"] = kwargs["control_command"]
        return _Result()

    monkeypatch.setattr(poller_wake, "act_on", capture)

    assert poller_wake.main(["--root", str(root), "--instance", "demo"]) == 0
    parser = build_parser()
    register = shlex.split(handed["register"])
    control = shlex.split(handed["control"])
    for command in (register, control):
        assert command[0] == str(poller_wake.ENGINE)
    registered = parser.parse_args([*register[1:], str(tmp_path / "worktree")])
    # The worker's worktree is claimed for the Work it names, through this wake's runtime (#2195).
    assert registered.work_id == "github_issue:x/store#5" and registered.runtime_socket
    parser.parse_args([*control[1:], "enabled", "store", "5", "gen1"])
    parser.parse_args([*control[1:], "finish", "store", "5", "gen1", "in_review", "done"])
    parser.parse_args([*control[1:], "review", "store", "5", "gen1", "pass", "fine"])


def test_a_refusal_records_why_it_was_refused(tmp_path):
    """#482: a bare `refused` cannot be acted on.

    Without the reason, a refusal that will retry on another account looks exactly
    like one that will give up — which is how #470 and #482 both sat `Blocked`
    with nobody working them and no recorded cause.
    """
    dispatcher = _Dispatcher(DeliveryOutcome.REFUSED, reason="over_budget")

    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
    )

    assert result.refused == 1
    assert any("refused (over_budget)" in line for line in result.log), result.log


def test_a_refusal_with_no_known_reason_still_reads_as_a_refusal(tmp_path):
    """An unknown cause must degrade to today's wording, not a misleading one."""
    dispatcher = _Dispatcher(DeliveryOutcome.REFUSED)

    result = act_on(
        [_DIRECTIVE],
        dispatcher=dispatcher,
        checkouts={"store": tmp_path / "store"},
        work_workspaces=_prepared(tmp_path),
    )

    assert any(line.endswith(": refused") for line in result.log), result.log


# --- draining the engine's review history into the deck's database (#1122) ---

def _review_record(tmp_path, sha="a" * 40, verdict="pass", result="release"):

    from agentdeck.poller_engine import reviews

    return reviews.record(
        tmp_path,
        repo="org/store",
        sha=sha,
        pull_request="org/store#7",
        issue_repo="org/store",
        issue_number=5,
        verdict=verdict,
        result=result,
        generation="gen1",
        now=datetime(2026, 7, 25, 12, tzinfo=UTC),
    )


def test_an_accepted_review_verdict_is_posted_and_then_removed(monkeypatch, tmp_path):
    _review_record(tmp_path)
    posted = []

    def run(command, **_kwargs):
        posted.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"recorded": True}), "")

    monkeypatch.setattr(subprocess, "run", run)
    lines = drain_review_verdicts(tmp_path, base_url="http://deck/")

    assert len(lines) == 1 and "org/store@aaaaaaaaaaaa" in lines[0]
    assert posted[0][-1] == "http://deck/api/poller/review-verdicts"
    body = json.loads(posted[0][posted[0].index("--data-binary") + 1])
    assert body["sha"] == "a" * 40 and body["verdict"] == "pass"
    assert reviews_pending(tmp_path) == []


def test_a_refused_review_verdict_is_kept_for_the_next_wake(monkeypatch, tmp_path):
    _review_record(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "down"),
    )

    assert drain_review_verdicts(tmp_path, base_url="http://deck") == []
    assert len(reviews_pending(tmp_path)) == 1


def test_a_deck_that_did_not_store_the_record_does_not_consume_it(monkeypatch, tmp_path):
    """HTTP success is not proof of a write; only the deck saying so consumes the record."""
    _review_record(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps({"recorded": False}), ""
        ),
    )

    assert drain_review_verdicts(tmp_path, base_url="http://deck") == []
    assert len(reviews_pending(tmp_path)) == 1


def test_draining_an_empty_directory_asks_the_deck_nothing(monkeypatch, tmp_path):
    def refuse(command, **_kwargs):
        raise AssertionError("nothing to drain should mean no request")

    monkeypatch.setattr(subprocess, "run", refuse)
    assert drain_review_verdicts(tmp_path / "missing", base_url="http://deck") == []


def reviews_pending(directory):
    from agentdeck.poller_engine import reviews

    return reviews.pending(directory)


def test_the_wake_hands_over_review_history_even_when_it_starts_nothing(
    tmp_path, monkeypatch, capsys
):
    """#1122: a wake that only consumed a verdict has no directive but still has history."""
    import subprocess

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import poller_wake

    root = tmp_path / "poller"
    instance = root / "instances" / "demo"
    instance.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir(parents=True)
    (instance / "poller.toml").write_text(
        "contract = 1\n\n[board]\norg = \"example\"\nproject_number = 7\n\n"
        "[board.statuses]\ntodo = \"Todo\"\nin_progress = \"In Progress\"\n"
        "in_review = \"In review\"\ndone = \"Done\"\n"
        "needs_more_info = \"Needs More Info\"\nblocked = \"Blocked\"\n\n"
        "[[repos]]\nid = \"store\"\nholds_issues = true\nholds_code = true\n"
        "verification = \"agentdeck_job\"\n"
    )
    (instance / "overlay.toml").write_text(
        "contract = 1\n\n"
        f'[paths]\nstate = "{instance / "state.json"}"\n\n'
        f'[checkouts]\nstore = "{checkout}"\n\n'
        '[worker]\naccount = "claude_code:main"\n'
    )

    monkeypatch.setattr(
        poller_wake.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(poller_wake.turns, "stop_runs", lambda **kwargs: [])
    drained = []
    monkeypatch.setattr(
        poller_wake,
        "drain_review_verdicts",
        lambda directory, **kwargs: drained.append(Path(directory)) or ["handed over 1"],
    )

    assert poller_wake.main(["--root", str(root), "--instance", "demo"]) == 0
    assert drained == [instance / "review-verdicts"]
    assert "handed over 1" in capsys.readouterr().out


def _origin_and_checkout(tmp_path, *, base="master"):
    """A checkout of a real remote, one commit behind it, for the freshness tests (#1853)."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", "--bare", f"--initial-branch={base}", str(remote)],
                   check=True)
    for path in (seed, checkout):
        path.mkdir()
    for command in (
        ["git", "init", "-q", f"--initial-branch={base}"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "remote", "add", "origin", str(remote)],
    ):
        subprocess.run(command, cwd=seed, check=True)
    (seed / "code.py").write_text("def present():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", base], cwd=seed, check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True)
    for command in (
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(command, cwd=checkout, check=True)
    (seed / "code.py").write_text("def present():\n    return 2\n")
    subprocess.run(["git", "commit", "-qam", "second"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", base], cwd=seed, check=True)
    return checkout


def test_even_a_checkout_it_would_be_safe_to_move_is_left_where_it_is(tmp_path):
    """The tempting case, and still not ours to touch (#1853).

    Clean, on its own base, one commit behind: every check a fast-forward could want passes.
    It is declined anyway, because the branch that receives a merge is whatever HEAD names at
    the instant of the write, and on a shared checkout no read can pin that.
    """
    checkout = _origin_and_checkout(tmp_path)
    head = _rev(checkout, "HEAD")

    state = refresh_checkout(checkout)

    assert state.behind == 1 and not state.current, "and the worker is told, rather than moved"
    assert _rev(checkout, "HEAD") == head, "the checkout's own branch must not move"
    assert (checkout / "code.py").read_text().endswith("return 1\n"), (
        "nor may its working tree"
    )
    assert _rev(checkout, "origin/master") != head, (
        "the tracking ref is still refreshed, which is the part that touches nobody"
    )


def _rev(checkout, ref):
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=checkout, capture_output=True, text=True, check=True,
    ).stdout.strip()


_MUTATING = {"merge", "reset", "checkout", "switch", "stash", "rebase", "pull", "clean"}


def _recording_runner(seen):
    def runner(command, cwd=None, timeout=300.0, env=None):
        seen.append(list(command))
        return subprocess.run(
            command, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, check=False,
        )

    return runner


def test_refreshing_a_checkout_never_runs_a_command_that_writes_a_working_tree(tmp_path):
    checkout = _origin_and_checkout(tmp_path)
    seen = []

    refresh_checkout(checkout, runner=_recording_runner(seen))

    offending = [c for c in seen if any(word in _MUTATING for word in c)]
    assert not offending, f"a refresh must only read and fetch, but ran {offending}"


def test_a_dirty_checkout_is_left_alone_and_reported(tmp_path):
    checkout = _origin_and_checkout(tmp_path)
    (checkout / "code.py").write_text("someone's work in progress\n")

    state = refresh_checkout(checkout)

    assert (checkout / "code.py").read_text() == "someone's work in progress\n", (
        "a poller must never move somebody's uncommitted work"
    )
    assert state.dirty == 1
    assert not state.current
    assert "1 uncommitted file(s)" in state.describe()


def test_a_checkout_on_someone_elses_branch_is_left_alone_and_reported(tmp_path):
    checkout = _origin_and_checkout(tmp_path)
    subprocess.run(["git", "switch", "-q", "-c", "fix/theirs"], cwd=checkout, check=True)

    state = refresh_checkout(checkout)

    assert state.branch == "fix/theirs"
    assert not state.current
    assert "not `master`" in state.describe()


def test_the_base_ref_is_refreshed_even_when_the_worktree_already_exists(tmp_path):
    checkout = _origin_and_checkout(tmp_path)
    worktree = checkout / ".worktrees" / "issue-7"
    worktree.mkdir(parents=True)
    calls = []

    adapter = SingleCheckoutWorkspaceAdapter(
        checkouts={"demo": checkout},
        refresh_checkout_state=lambda path, base="": calls.append((path, base))
        or CheckoutFreshness(path=path, base=base or "master", branch="master"),
        work_workspaces=_RecordingWorks(),
    )
    prepared = adapter.prepare({"repo": "demo", "number": 7})

    assert calls == [(checkout, "master")], (
        "a reused worktree never refetched its base, so every diff against it and the "
        "changed-paths warning could be reading a weeks-old ref"
    )
    assert prepared is not None
    assert prepared.checkout_states[0].path == checkout


def test_the_brief_names_a_stale_checkout_and_says_nothing_when_all_are_current():
    stale = CheckoutFreshness(
        path=Path("/home/me/trailhead"), base="master", branch="fix/theirs", dirty=15
    )
    behind = CheckoutFreshness(
        path=Path("/home/me/trailhead/issues"), base="main", branch="main", behind=20
    )
    current = CheckoutFreshness(path=Path("/home/me/store"), base="master", branch="master")

    named = stale_checkout_brief([stale, behind, current])

    assert "/home/me/trailhead`" in named
    assert "not `master`" in named and "15 uncommitted file(s)" in named
    assert "20 commit(s) behind `origin/main`" in named
    assert "/home/me/store" not in named, "a current checkout is not a warning"
    assert stale_checkout_brief([current]) == "", (
        "nothing to warn about must add nothing to the brief"
    )


def test_the_worker_is_told_which_checkouts_it_must_not_read_from():
    prepared = PreparedWorkspaces(
        cwd=Path("/home/me/trailhead/.worktrees/issue-365"),
        workspaces=(
            Workspace(
                path=Path("/home/me/trailhead/.worktrees/issue-365"),
                branch="agent/issue-365",
                checkout_state=CheckoutFreshness(
                    path=Path("/home/me/trailhead"), base="master", branch="fix/theirs", dirty=15
                ),
            ),
        ),
    )

    message = compose_message(
        {"ref": "issues#365", "repo": "issues", "number": 365},
        prepared,
        sibling_states=[
            CheckoutFreshness(
                path=Path("/home/me/store"), base="main", branch="main", behind=20
            )
        ],
    )

    assert "Code may be older than it looks" in message
    assert "/home/me/trailhead`" in message, "the card's own checkout is a stale read too"
    assert "/home/me/store" in message, "so is every other repository this project owns"
    assert "read code in your own worktree" in message.lower()


def test_an_unreachable_remote_still_reports_what_the_checkout_could_be_asked(tmp_path):
    checkout = _origin_and_checkout(tmp_path)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@example.invalid:nobody/nothing.git"],
        cwd=checkout, check=True,
    )
    (checkout / "code.py").write_text("someone's work in progress\n")

    state = refresh_checkout(checkout)

    assert not state.fetched
    assert state.readable, (
        "a remote this host cannot reach says nothing about whether the checkout can be read"
    )
    assert state.dirty == 1, "the facts git could still answer must survive the failed fetch"
    assert state.behind == 0, (
        "the cached ref says zero while the remote is a commit ahead, which is precisely why a "
        "failed fetch may not read as current"
    )
    assert not state.current
    described = state.describe()
    assert "1 uncommitted file(s)" in described
    assert "could not be fetched" in described


def test_unpushed_local_commits_are_not_certified_as_current(tmp_path):
    """Measuring only one direction called locally committed, unpushed code the base (#1853)."""
    checkout = _origin_and_checkout(tmp_path)
    subprocess.run(["git", "fetch", "-q", "origin", "master"], cwd=checkout, check=True)
    subprocess.run(["git", "merge", "-q", "--ff-only", "origin/master"], cwd=checkout, check=True)
    (checkout / "code.py").write_text("def present():\n    return 99\n")
    subprocess.run(["git", "commit", "-qam", "not pushed"], cwd=checkout, check=True)

    state = refresh_checkout(checkout)

    assert state.ahead == 1 and state.behind == 0 and state.dirty == 0
    assert not state.current, (
        "the file here says what somebody wrote locally and never pushed, which is not what the "
        "base branch holds"
    )
    assert "1 unpushed commit(s)" in state.describe()
    assert stale_checkout_brief([state]), "and the worker has to be told"


def _runner_firing_after(command_words, trigger):
    """A runner that lets another writer act in the window the reviewer's probe used.

    It fires once, after the first command containing every word — the read that authorises the
    fast-forward — which is exactly the interval the check-and-update sequence has to survive.
    """
    fired = []

    def runner(command, cwd=None, timeout=300.0, env=None):
        done = subprocess.run(
            command, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        if not fired and all(word in command for word in command_words):
            fired.append(True)
            trigger()
        return done

    return runner


@pytest.mark.parametrize(
    "window",
    [["fetch"], ["status", "--porcelain"], ["rev-list", "--left-right"]],
    ids=["after-fetch", "after-status", "after-the-counts"],
)
def test_no_interleaving_lets_a_refresh_touch_somebody_elses_branch(tmp_path, window):
    """The invariant the second review round asked for, at every command boundary (#1853).

    The first attempt at this guarded the window between the last read and the merge, which
    only moved the window: a switch landing inside the revalidation still handed the merge a
    branch nobody authorised, because `--ff-only <commit>` pins what is merged and never which
    branch receives it. There is no window left to protect because there is no write: whichever
    command the other writer interleaves with, the human's branch and files come out untouched.
    """
    checkout = _origin_and_checkout(tmp_path)
    subprocess.run(["git", "branch", "human/new-feature"], cwd=checkout, check=True)
    theirs = _rev(checkout, "human/new-feature")
    mine = _rev(checkout, "master")

    def switch():
        subprocess.run(["git", "switch", "-q", "human/new-feature"], cwd=checkout, check=True)
        (checkout / "code.py").write_text("someone started typing\n")

    refresh_checkout(checkout, runner=_runner_firing_after(window, switch))

    assert _rev(checkout, "human/new-feature") == theirs, (
        "a poller must never fast-forward somebody else's branch"
    )
    assert _rev(checkout, "master") == mine, "nor its own base branch"
    assert (checkout / "code.py").read_text() == "someone started typing\n", (
        "and nothing may reset, stash or discard another writer's work"
    )


def test_a_read_that_fails_is_reported_as_unknown_rather_than_as_current(tmp_path):
    """A half-read tree must never come out looking exactly like one sitting on its base."""
    checkout = _origin_and_checkout(tmp_path)
    subprocess.run(["git", "fetch", "-q", "origin", "master"], cwd=checkout, check=True)
    subprocess.run(["git", "merge", "-q", "--ff-only", "origin/master"], cwd=checkout, check=True)

    def runner(command, cwd=None, timeout=300.0, env=None):
        if "rev-list" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="broken")
        return subprocess.run(
            command, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, check=False,
        )

    state = refresh_checkout(checkout, runner=runner)

    assert not state.readable and not state.current, (
        "this checkout really is current, and still may not be reported so on a failed read"
    )
    assert "could not be read" in state.describe()
    assert stale_checkout_brief([state]), "an unknown state is a warning, not a silence"


def test_a_reused_worktree_is_never_promised_to_be_current(tmp_path):
    prepared = PreparedWorkspaces(
        cwd=Path("/home/me/trailhead/.worktrees/issue-365"),
        workspaces=(
            Workspace(
                path=Path("/home/me/trailhead/.worktrees/issue-365"),
                branch="agent/issue-365",
                created=False,
                checkout_state=CheckoutFreshness(
                    path=Path("/home/me/trailhead"), base="master", branch="master", behind=3
                ),
            ),
        ),
    )

    message = compose_message({"ref": "issues#365", "repo": "issues", "number": 365}, prepared)

    assert "the code in it is current" not in message, (
        "refreshing a tracking ref does not move the worktree's own branch"
    )
    assert "kept from earlier work" in message
    assert "rev-list --left-right --count origin/master...HEAD" in message, (
        "the worker gets the command that settles it for its own branch"
    )


def test_a_failed_fetch_is_not_dressed_up_as_a_fresh_base():
    state = CheckoutFreshness(
        path=Path("/home/me/trailhead"), base="master", branch="master", fetched=False
    )
    fresh = Workspace(
        path=Path("/home/me/trailhead/.worktrees/issue-365"), branch="b", created=True
    )

    named = stale_checkout_brief([state], workspaces=[fresh])

    assert "could not be fetched" in named
    assert "the code in it is current" not in named


# --- repository policy is resolved before the brief is written (#1734) ---


def test_a_repo_that_prohibits_closing_keywords_is_never_told_to_write_one():
    """#1734: AgentDeck must not hand a worker a rule its repository forbids.

    Two repos with intentionally opposite policies, composed the same way: `store` keeps the
    closing keyword it always had, and `guarded` — whose own guide bans one — is told to
    reference the card in the qualified form instead.
    """
    permissive = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        owner="eerovil",
        closing_keywords={"store": "required"},
    )
    prohibiting = compose_message(
        {**_DIRECTIVE, "ref": "guarded#5", "repo": "guarded"},
        Workspace(path=Path("/w/issue-5"), branch="b"),
        owner="eerovil",
        closing_keywords={"guarded": "prohibited"},
    )

    assert "Closes #5" in permissive
    assert "## Source issue closure" in permissive

    assert "Closes #5" not in prohibiting
    assert "Closes eerovil/guarded#5" not in prohibiting
    assert "must contain a GitHub-supported closing keyword" not in prohibiting
    assert "## Source issue reference" in prohibiting
    # The lifecycle tie survives: this is the form the release path actually links from.
    assert "eerovil/guarded#5" in prohibiting
    assert "prohibits GitHub closing keywords" in prohibiting


def test_an_unstated_closing_keyword_policy_keeps_the_instruction_every_project_had():
    """#1734: the field is additive — a project that never mentions it is unchanged."""
    message = compose_message(
        _DIRECTIVE, Workspace(path=Path("/w/issue-5"), branch="b"), owner="eerovil"
    )

    assert "Closes #5" in message
    assert "## Source issue closure" in message


def test_the_prohibiting_brief_keeps_the_global_lifecycle_invariants():
    """#1734: only the repository-specific syntax changes, not what the worker owes."""
    message = compose_message(
        {**_DIRECTIVE, "action": "followup", "ref": "guarded#5", "repo": "guarded"},
        Workspace(path=Path("/w/issue-5"), branch="b"),
        owner="eerovil",
        closing_keywords={"guarded": "prohibited"},
    )

    assert "Preserve an existing reference to this card verbatim" in message
    assert "Review, fix, base-update, and follow-up work must not weaken or remove it" in message
    assert "PR intent: non-closing" in message
    assert "Do not infer non-closing intent merely from words such as" in message
    assert "Never push to master, and do not merge a pull request yourself" in message
    assert "Do not deploy from this worker." in message


def test_a_sibling_repo_with_its_own_policy_is_named_where_work_is_sent_into_it():
    """#1734: the one place a worker learns the other repository exists must carry its rule."""
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        owner="eerovil",
        code_checkouts={"store": "/w/store", "guarded": "/w/guarded"},
        closing_keywords={"store": "required", "guarded": "prohibited"},
    )

    assert "## Other code this project owns" in message
    assert (
        "- `guarded` — /w/guarded (this repository prohibits closing keywords in a pull "
        "request description)"
    ) in message
    # The card's own repository still gets its own instruction, unchanged.
    assert "Closes #5" in message


def test_the_closing_keyword_policy_follows_a_borrowed_workspace_to_the_code_repo(tmp_path):
    """#1734: an issue-only repository's PR lands in the code repository it borrows."""
    checkout = tmp_path / "guarded"
    (checkout / ".git").mkdir(parents=True)
    prepared = PreparedWorkspaces(
        cwd=checkout,
        workspaces=(Workspace(path=checkout, branch="agent/issue-5-tracker"),),
        owner="eerovil",
        borrowed_from="guarded",
    )

    message = compose_message(
        {**_DIRECTIVE, "ref": "tracker#5", "repo": "tracker"},
        prepared,
        closing_keywords={"guarded": "prohibited"},
    )

    assert "Closes #5" not in message
    assert "## Source issue reference" in message


# --- a card whose worker is mid-turn is not moved for budget (#2059) ---


class _FakeWorkWorkspaces:
    """The runtime's answer for each purpose, and every request made of it."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def ensure(self, directive, *, purpose, checkout, name, branch, base, repository=""):
        from agentdeck.poller_adapter.work_workspaces import ClaimedWorkspace

        self.asked.append((purpose, str(checkout), name, branch, base, repository))
        answer = self.answers.get(purpose)
        if answer == "legacy":
            return ClaimedWorkspace(detail="left on legacy ownership until reconciled")
        if answer is None:
            return ClaimedWorkspace(detail="uncertain")
        path = Path(checkout) / ".worktrees" / answer
        path.mkdir(parents=True, exist_ok=True)
        return ClaimedWorkspace(path=path, branch=branch, base_ref=base, created=True)


def _claim_adapter(tmp_path, answers, calls):
    checkout = tmp_path / "store"
    checkout.mkdir()
    works = _FakeWorkWorkspaces(answers)
    adapter = SingleCheckoutWorkspaceAdapter(
        checkouts={"store": checkout},
        read_default_branch=lambda path, fallback="master": "main",
        work_workspaces=works,
    )
    return adapter, works, checkout


def test_the_work_claim_is_the_workspace_and_nothing_is_made_here(tmp_path):
    calls = []
    adapter, works, checkout = _claim_adapter(tmp_path, {"implementation": "issue-7"}, calls)

    prepared = adapter.prepare({"repo": "store", "number": 7, "work_id": "w"})

    assert calls == []  # the runtime made it; the host did not
    (workspace,) = prepared.workspaces
    assert workspace.work_claimed and workspace.path == checkout / ".worktrees" / "issue-7"
    assert works.asked == [
        ("implementation", str(checkout), "issue-7", "agent/issue-7", "main", "")
    ]


def test_a_runtime_that_will_not_adopt_prepares_nothing(tmp_path):
    """There is no card-owned worktree to fall back to any more (#2195): the delivery waits."""
    calls = []
    adapter, works, checkout = _claim_adapter(tmp_path, {"implementation": "legacy"}, calls)

    assert adapter.prepare({"repo": "store", "number": 7, "work_id": "w"}) is None
    assert not (checkout / ".worktrees").exists()
    unserved = SingleCheckoutWorkspaceAdapter(checkouts={"store": checkout})
    assert unserved.prepare({"repo": "store", "number": 7, "work_id": "w"}) is None


def test_an_unsettled_claim_prepares_nothing_rather_than_a_second_worktree(tmp_path):
    calls = []
    adapter, _works, _checkout = _claim_adapter(tmp_path, {}, calls)

    assert adapter.prepare({"repo": "store", "number": 7, "work_id": "w"}) is None
    assert calls == []


def test_an_abandoned_delivery_never_removes_a_work_claimed_worktree(tmp_path):
    from agentdeck.poller_adapter.runner import _abandon

    claimed = Workspace(path=tmp_path / "claimed", branch="b", created=True, work_claimed=True)
    other = Workspace(path=tmp_path / "other", branch="b", created=True)

    assert _abandon(PreparedWorkspaces(cwd=tmp_path, workspaces=(claimed,))) == ""
    note = _abandon(PreparedWorkspaces(cwd=tmp_path, workspaces=(claimed, other)))
    assert str(tmp_path / "other") in note and str(tmp_path / "claimed") not in note


def test_the_runtime_answer_is_read_strictly():
    from agentdeck.poller_adapter.work_workspaces import WorkWorkspaces

    def asking(status, answer):
        sent = []

        def post(socket, path, body, **_kwargs):
            sent.append(body)
            return status, answer

        works = WorkWorkspaces(
            runtime_socket="/s", post=post, repository_of=lambda _c: "example/store"
        )
        return works.ensure(
            {"work_id": "w", "generation": "g"},
            purpose="implementation",
            checkout=Path("/c"),
            name="issue-7",
            branch="agent/issue-7",
            base="main",
        ), sent

    claim = {"path": "/c/.worktrees/issue-7", "branch": "agent/issue-7", "base_ref": "main"}
    made, sent = asking(200, {"outcome": "created", "claim": claim})
    assert made.usable and made.created and made.path == Path("/c/.worktrees/issue-7")
    assert sent[0]["path"] == "/c/.worktrees/issue-7" and sent[0]["run_id"] == "g"
    assert sent[0]["legacy_paths"] is None  # unread legacy state is never "none registered"
    # Every answer but a claim is unusable; none of them falls back to a card-owned path.
    for status, answer in (
        (404, None),
        (200, {"outcome": "legacy", "claim": None}),
        (0, None),
        (503, {"detail": "down"}),
        (200, {"outcome": "uncertain"}),
    ):
        assert not asking(status, answer)[0].usable
    unnamed = WorkWorkspaces(runtime_socket="/s", post=lambda *a, **k: (200, {}))
    found = unnamed.ensure(
        {}, purpose="implementation", checkout=Path("/c"), name="n", branch="b", base="m"
    )
    assert not found.usable and found.detail == "the directive names no Work"
