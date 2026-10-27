"""The one-off cutover that turns card-held worktrees into Work claims (#2195).

What has to be right is that nothing is deleted on a guess: a gone path only leaves the card
record, a duplicate is released only through the dirty-safe removal, an ambiguous pair is
kept, and a second run changes nothing.
"""

from __future__ import annotations

import importlib.util
import sys
from functools import cache
from pathlib import Path

from agentdeck.poller_engine.state import EnginePaths, StateStore
from agentdeck.work_workspace_claims import HostWorktree

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "adopt_card_worktrees.py"
OWNER = "acme"
ARTIFACTS = "acme/artifacts"
WORK = "github_issue:acme/app#7"


@cache
def migration():
    """The script, loaded as a module. It is a script, not a package, so it has no import path."""
    spec = importlib.util.spec_from_file_location("adopt_card_worktrees", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered first: its dataclasses resolve their annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeHost:
    """Worktrees keyed by path: ``(repository, branch)``; removal refuses the dirty ones."""

    def __init__(self, trees: dict[str, tuple[str, str]], *, dirty: set[str] = frozenset()):
        self.trees = dict(trees)
        self.dirty = set(dirty)
        self.removed: list[str] = []

    def as_host(self):
        return migration().Host(
            inspect=self.inspect,
            branch=lambda path: self.trees[path][1],
            base=lambda _checkout: "main",
            remove=self.remove,
            exists=lambda path: path in self.trees,
        )

    def inspect(self, path: str) -> HostWorktree:
        if path not in self.trees:
            return HostWorktree(exists=False)
        repository = self.trees[path][0]
        return HostWorktree(exists=True, registered=repository != "?", repository=repository)

    def remove(self, path: str) -> tuple[bool, str]:
        if path in self.dirty:
            return False, "it holds 2 uncommitted files"
        self.removed.append(path)
        del self.trees[path]
        return True, ""


class FakeRuntime:
    """The runtime's claim routes over an in-memory list, adopting whatever it is asked."""

    def __init__(self, claims: list[dict] | None = None, *, refuse: str = ""):
        self.claims_held = list(claims or [])
        self.refuse = refuse
        self.ensured: list[dict] = []

    def as_runtime(self):
        return migration().Runtime("sock", post=self.post, get=self.get)

    def get(self, _socket: str, _path: str):
        return {"claims": list(self.claims_held)}

    def post(self, _socket: str, _path: str, body: dict, *, timeout: float):
        self.ensured.append(body)
        if self.refuse:
            return 200, {"outcome": "legacy", "claim": None, "detail": self.refuse}
        claim = {key: body[key] for key in ("work_id", "repository", "purpose", "path")}
        self.claims_held.append(claim)
        return 200, {"outcome": "adopted", "claim": claim, "detail": ""}


def _store(tmp_path: Path, worktrees: list[str]) -> StateStore:
    store = StateStore(EnginePaths.from_base(tmp_path / "state"))
    with store.transaction() as state:
        state.setdefault("issues", {})["app#7"] = {"status": "In review", "worktrees": worktrees}
    return store


def _run(tmp_path, host: FakeHost, runtime: FakeRuntime, worktrees: list[str], *, apply=True):
    module = migration()
    store = _store(tmp_path, worktrees)
    record = store.read()["issues"]["app#7"]
    items = module.plan_card(
        "app#7",
        record,
        owner=OWNER,
        artifact_repo=ARTIFACTS,
        claims=runtime.claims_held,
        host=host.as_host(),
    )
    outcomes = module.apply_card(
        "app#7",
        items,
        store=store if apply else None,
        runtime=runtime.as_runtime(),
        host=host.as_host(),
    )
    return {outcome.item.path: outcome.result for outcome in outcomes}, store


def test_gone_paths_leave_the_card_and_present_ones_become_claims(tmp_path):
    code, evidence = "/r/app/.worktrees/issue-7", "/r/art/.worktrees/issue-7-ev"
    gone = "/r/x/.worktrees/old"
    host = FakeHost({code: ("acme/app", "agent/issue-7"), evidence: (ARTIFACTS, "evidence/app-7")})
    runtime = FakeRuntime()

    results, store = _run(tmp_path, host, runtime, [code, evidence, gone])

    assert results == {code: "adopted", evidence: "adopted", gone: "dropped (gone from disk)"}
    assert store.read()["issues"]["app#7"]["worktrees"] == [code, evidence]
    purposes = {body["path"]: (body["work_id"], body["purpose"]) for body in runtime.ensured}
    assert purposes == {code: (WORK, "implementation"), evidence: (WORK, "artifact")}
    # The runtime judged adoption against what the card registers after the drop.
    assert runtime.ensured[0]["legacy_paths"] == [code, evidence]
    assert host.removed == []


def test_a_second_run_changes_nothing(tmp_path):
    code, gone = "/r/app/.worktrees/issue-7", "/r/x/.worktrees/old"
    host = FakeHost({code: ("acme/app", "agent/issue-7")})
    runtime = FakeRuntime()
    _run(tmp_path, host, runtime, [code, gone])

    again, store = _run(tmp_path / "again", host, runtime, [code])

    assert again == {code: "already a claim"}
    assert len(runtime.ensured) == 1
    assert store.read()["issues"]["app#7"]["worktrees"] == [code]


def test_a_duplicate_of_a_held_claim_is_released_only_when_clean(tmp_path):
    held, clean = "/r/app/.worktrees/issue-7", "/r/app/.worktrees/spare"
    dirty = "/r/app/.worktrees/wip"
    host = FakeHost(
        {
            held: ("acme/app", "agent/issue-7"),
            clean: ("acme/app", "spare"),
            dirty: ("acme/app", "wip"),
        },
        dirty={dirty},
    )
    runtime = FakeRuntime(
        [{"work_id": WORK, "repository": "acme/app", "purpose": "implementation", "path": held}]
    )

    results, store = _run(tmp_path, host, runtime, [held, clean, dirty])

    assert results[held] == "already a claim"
    assert results[clean] == "released"
    assert results[dirty] == "kept"
    assert host.removed == [clean]
    assert dirty in host.trees
    assert store.read()["issues"]["app#7"]["worktrees"] == [held, dirty]


def test_two_candidates_prefer_the_one_named_for_the_card(tmp_path):
    named, other = "/r/app/.worktrees/issues-7", "/r/app/.worktrees/issues-71"
    host = FakeHost({named: ("acme/app", "a"), other: ("acme/app", "b")})
    runtime = FakeRuntime()

    results, _store = _run(tmp_path, host, runtime, [named, other])

    # 71 is not 7: the number is matched whole, never as a prefix.
    assert results == {named: "adopted", other: "released"}
    assert host.removed == [other]


def test_two_candidates_with_no_single_named_one_are_both_kept(tmp_path):
    first, second = "/r/app/.worktrees/alpha", "/r/app/.worktrees/beta"
    host = FakeHost({first: ("acme/app", "a"), second: ("acme/app", "b")})
    runtime = FakeRuntime()

    results, store = _run(tmp_path, host, runtime, [first, second])

    assert results == {first: "kept", second: "kept"}
    assert host.removed == [] and runtime.ensured == []
    assert store.read()["issues"]["app#7"]["worktrees"] == [first, second]


def test_unregistered_and_refused_paths_are_kept(tmp_path):
    stray, code = "/r/app/.worktrees/stray", "/r/lib/.worktrees/issue-7"
    host = FakeHost({stray: ("?", ""), code: ("acme/lib", "agent/issue-7")})
    runtime = FakeRuntime(refuse="left on legacy ownership until reconciled")

    results, store = _run(tmp_path, host, runtime, [stray, code])

    assert results == {stray: "kept", code: "kept"}
    assert host.removed == []
    assert store.read()["issues"]["app#7"]["worktrees"] == [stray, code]


def test_a_dry_run_writes_nothing(tmp_path):
    code, gone = "/r/app/.worktrees/issue-7", "/r/x/.worktrees/old"
    host = FakeHost({code: ("acme/app", "agent/issue-7")})
    runtime = FakeRuntime()

    results, store = _run(tmp_path, host, runtime, [code, gone], apply=False)

    assert results == {code: "would adopt", gone: "would drop"}
    assert runtime.ensured == [] and host.removed == []
    assert store.read()["issues"]["app#7"]["worktrees"] == [code, gone]
