"""The disk-side worktree reaper (#1094).

Cleanup elsewhere in the engine only ever removes a path some record named. These tests are
about the opposite direction — starting from what is on disk — and so nearly all of them are
about *not* deleting something: the claimed set surviving two spellings of the same
directory, and every unreadable fact keeping the directory instead of releasing it.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentdeck.poller_engine import __main__ as cli
from agentdeck.poller_engine import reaper

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def work_claims(monkeypatch):
    """The runtime's Work workspace claims the command reads (#2149); none by default."""
    held: list[str] = []
    state = {"readable": True}

    def read(_socket: str) -> list[str] | None:
        return list(held) if state["readable"] else None

    monkeypatch.setattr(cli, "_work_claimed_paths", read)
    return held, state


def _candidate(path: str, **facts) -> reaper.Candidate:
    """A candidate that is reapable unless a test says otherwise."""
    defaults = dict(
        checkout="/repo", registered=True, age_days=30.0, dirty=0, merged=True, branch="agent/x"
    )
    return reaper.Candidate(path=path, **{**defaults, **facts})


# --- the claimed set ------------------------------------------------------------------

def test_a_tracked_worktree_is_never_reaped():
    verdicts = reaper.plan([_candidate("/repo/.worktrees/issue-1")], ["/repo/.worktrees/issue-1"])
    assert [(v.reap, v.reason) for v in verdicts] == [(False, reaper.CLAIMED)]


def test_an_untracked_worktree_is_the_orphan_the_card_is_about():
    verdicts = reaper.plan([_candidate("/repo/.worktrees/issue-1")], [])
    assert [(v.reap, v.reason) for v in verdicts] == [(True, reaper.ORPHAN)]


def test_the_two_spellings_of_one_directory_are_the_same_claim(tmp_path):
    """#1095: state.json records `/home/...` and `/var/home/...` for one directory.

    A string comparison reads the spelling it was not given as unowned, and deletes a live
    worker's uncommitted work. This is the reason the reaper resolves before comparing.
    """
    real = tmp_path / "var" / "home" / "eero" / "repo" / ".worktrees" / "issue-1"
    real.mkdir(parents=True)
    (tmp_path / "home").symlink_to(tmp_path / "var" / "home")
    alias = tmp_path / "home" / "eero" / "repo" / ".worktrees" / "issue-1"
    assert str(alias) != str(real) and alias.resolve() == real.resolve()

    found_as_real = reaper.plan([_candidate(str(real))], [str(alias)])
    found_as_alias = reaper.plan([_candidate(str(alias))], [str(real)])
    assert [v.reason for v in found_as_real] == [reaper.CLAIMED]
    assert [v.reason for v in found_as_alias] == [reaper.CLAIMED]


# --- every unknown keeps the directory ------------------------------------------------

@pytest.mark.parametrize(
    "facts, reason",
    [
        ({"registered": False}, reaper.UNREGISTERED),
        ({"age_days": None}, reaper.UNREADABLE),
        ({"dirty": None}, reaper.UNREADABLE),
        ({"merged": None}, reaper.UNREADABLE),
        ({"age_days": 0.5}, reaper.TOO_NEW),
        ({"dirty": 3}, reaper.DIRTY),
        ({"merged": False}, reaper.UNMERGED),
    ],
)
def test_anything_unproved_is_kept_with_its_reason(facts, reason):
    verdicts = reaper.plan([_candidate("/repo/.worktrees/issue-1", **facts)], [])
    assert [(v.reap, v.reason) for v in verdicts] == [(False, reason)]


def test_ownership_outranks_every_other_reason():
    """A claimed path is never even asked the other questions."""
    stale = _candidate("/repo/.worktrees/issue-1", registered=False, dirty=None, merged=False)
    assert reaper.plan([stale], ["/repo/.worktrees/issue-1"])[0].reason == reaper.CLAIMED


def test_the_age_floor_is_configurable():
    young = _candidate("/repo/.worktrees/issue-1", age_days=3.0)
    assert reaper.plan([young], [], min_age_days=7)[0].reason == reaper.TOO_NEW
    assert reaper.plan([young], [], min_age_days=1)[0].reap


def test_an_unresolvable_path_still_compares_by_its_own_spelling():
    assert reaper.resolve("/nowhere/at/all") == "/nowhere/at/all"


# --- reading the facts off a real checkout --------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    """A real checkout with an `origin/master` to measure worktrees against."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "file.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "master")
    return repo


def _worktree(repo: Path, name: str, *, branch: str) -> Path:
    path = repo / ".worktrees" / name
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path))
    return path


def test_a_worktree_holding_only_merged_work_reads_as_reapable(checkout):
    path = _worktree(checkout, "issue-1", branch="agent/issue-1")
    found = reaper.scan([str(checkout)], now=NOW + timedelta(days=365))
    assert [(item.path, item.registered, item.dirty, item.merged) for item in found] == [
        (str(path), True, 0, True)
    ]
    assert reaper.plan(found, [])[0].reap


def test_a_worktree_with_its_own_commit_reads_as_unmerged(checkout):
    path = _worktree(checkout, "issue-2", branch="agent/issue-2")
    (path / "file.txt").write_text("two\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "two")
    found = reaper.scan([str(checkout)], now=NOW + timedelta(days=365))
    assert [item.merged for item in found] == [False]
    assert reaper.plan(found, [])[0].reason == reaper.UNMERGED


def test_uncommitted_work_is_counted_and_keeps_the_worktree(checkout):
    path = _worktree(checkout, "issue-3", branch="agent/issue-3")
    (path / "scratch.txt").write_text("unfinished\n")
    found = reaper.scan([str(checkout)], now=NOW + timedelta(days=365))
    assert [item.dirty for item in found] == [1]
    assert reaper.plan(found, [])[0].reason == reaper.DIRTY


def test_a_plain_directory_under_worktrees_is_not_a_worktree_this_command_removes(checkout):
    stray = checkout / ".worktrees" / "leftover"
    stray.mkdir(parents=True)
    found = reaper.scan([str(checkout)], now=NOW)
    assert [(item.path, item.registered) for item in found] == [(str(stray), False)]
    assert reaper.plan(found, [])[0].reason == reaper.UNREGISTERED


def test_a_recently_touched_worktree_reads_as_young(checkout):
    _worktree(checkout, "issue-4", branch="agent/issue-4")
    found = reaper.scan([str(checkout)], now=datetime.now(UTC))
    assert found[0].age_days is not None and found[0].age_days < 1
    assert reaper.plan(found, [])[0].reason == reaper.TOO_NEW


def test_a_checkout_with_no_worktrees_directory_contributes_nothing(tmp_path):
    assert reaper.scan([str(tmp_path / "absent")], now=NOW) == []


def test_a_checkout_with_no_base_branch_cannot_prove_anything_merged(tmp_path):
    repo = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "file.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    _worktree(repo, "issue-1", branch="agent/issue-1")
    found = reaper.scan([str(repo)], now=NOW + timedelta(days=365))
    assert [item.merged for item in found] == [None]
    assert reaper.plan(found, [])[0].reason == reaper.UNREADABLE


def test_directory_size_reports_what_a_sweep_would_reclaim(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 2048)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 1024)
    assert reaper.directory_size(str(tmp_path)) >= 3072
    assert reaper.directory_size(str(tmp_path / "absent")) is None


def test_a_git_read_that_cannot_run_is_unknown_rather_than_zero(checkout, monkeypatch):
    _worktree(checkout, "issue-5", branch="agent/issue-5")

    def refuse(*_args, **_kwargs):
        raise OSError("no git here")

    monkeypatch.setattr(reaper.subprocess, "run", refuse)
    found = reaper.scan([str(checkout)], now=NOW, runner=refuse)
    assert [(item.registered, item.dirty, item.merged) for item in found] == [(False, None, None)]
    assert reaper.plan(found, [])[0].reason == reaper.UNREGISTERED


def test_the_reaper_never_looks_outside_a_checkouts_worktrees_directory(checkout):
    """`registered_worktree` is the guard, and the reaper inherits it unchanged."""
    outside = checkout.parent / "not-a-worktree"
    outside.mkdir()
    assert reaper.scan([str(checkout.parent)], now=NOW) == []
    assert os.path.isdir(outside)


# --- the command an operator actually runs ---------------------------------------------

_MANIFEST = """
contract = 1

[board]
org = "example"
project_number = 7

[board.statuses]
todo = "Todo"
in_progress = "In Progress"
in_review = "In review"
done = "Done"
needs_more_info = "Needs More Info"
blocked = "Blocked"

[policy]
merge_method = "merge"
worker_cap = 2

[[repos]]
id = "store"
holds_issues = true
holds_code = true
verification = "agentdeck_job"

[authorization]
release = ["eero"]
work = ["eero"]
"""


def _instance(tmp_path, checkout: Path, state: dict | None = None) -> dict:
    manifest = tmp_path / "poller.toml"
    manifest.write_text(_MANIFEST)
    overlay = tmp_path / "overlay.toml"
    state_path = tmp_path / "state.json"
    overlay.write_text(
        "contract = 1\n\n"
        f'[paths]\nstate = "{state_path}"\n\n'
        f'[checkouts]\nstore = "{checkout}"\n\n'
        '[worker]\naccount = "claude_code:main"\n'
    )
    state_path.write_text(json.dumps(state or {}))
    return {"manifest": str(manifest), "overlay": str(overlay), "state": state_path}


def _reap(files, *flags: str) -> int:
    return cli.main(
        [
            "reap-worktrees",
            "--manifest",
            files["manifest"],
            "--overlay",
            files["overlay"],
            "--runtime-socket",
            "/run/fake-runtime.sock",  # read through the `work_claims` fixture
            *flags,
        ]
    )


def _old(path: Path, days: int = 400) -> None:
    """Backdate a worktree past any sane age floor."""
    when = datetime.now(UTC).timestamp() - days * 86400
    for entry in (path / ".git", path):
        os.utime(entry, (when, when))


def test_the_command_reports_orphans_and_removes_nothing(tmp_path, checkout, capsys):
    path = _worktree(checkout, "issue-9", branch="agent/issue-9")
    _old(path)
    files = _instance(tmp_path, checkout)

    assert _reap(files) == 0
    out = capsys.readouterr().out
    assert "1 reapable" in out and str(path) in out
    assert "dry run: nothing was removed" in out
    assert path.exists(), "a dry run must never delete anything"


def test_apply_releases_the_orphan_through_the_validated_git_path(tmp_path, checkout, capsys):
    path = _worktree(checkout, "issue-10", branch="agent/issue-10")
    _old(path)
    files = _instance(tmp_path, checkout)

    assert _reap(files, "--apply") == 0
    assert "released 1 worktrees" in capsys.readouterr().out
    assert not path.exists()
    assert str(path) not in _git(checkout, "worktree", "list", "--porcelain")


def test_a_card_record_alone_no_longer_holds_a_worktree(tmp_path, checkout, capsys):
    """Every card worktree is a Work claim (#2195), so a card record is not an owner.

    What protects a worktree is the claim; a path only a card lists is exactly the unowned
    leftover this command exists for, judged by the same age, dirt and merge rules.
    """
    path = _worktree(checkout, "issue-11", branch="agent/issue-11")
    _old(path)
    files = _instance(
        tmp_path, checkout, {"issues": {"example/store#11": {"worktrees": [str(path)]}}}
    )

    assert _reap(files, "--apply") == 0
    assert reaper.CLAIMED not in capsys.readouterr().out
    assert not path.exists()


def test_the_size_report_answers_what_a_sweep_reclaims(tmp_path, checkout, capsys):
    path = _worktree(checkout, "issue-13", branch="agent/issue-13")
    (path / "blob.bin").write_bytes(b"z" * 40000)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "blob")
    _git(path, "push", "-q", "origin", "HEAD:master")
    _git(checkout, "fetch", "-q", "origin")
    _old(path)
    files = _instance(tmp_path, checkout)

    assert _reap(files, "--size") == 0
    assert "reclaimable:" in capsys.readouterr().out


def test_dry_run_and_apply_cannot_be_asked_for_together(tmp_path, checkout):
    files = _instance(tmp_path, checkout)
    with pytest.raises(SystemExit):
        _reap(files, "--dry-run", "--apply")


def test_an_instance_with_no_checkouts_scans_nothing(tmp_path, capsys):
    manifest = tmp_path / "poller.toml"
    manifest.write_text(_MANIFEST.replace("holds_code = true", "holds_code = false"))
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        "contract = 1\n\n"
        f'[paths]\nstate = "{tmp_path / "state.json"}"\n\n'
        '[worker]\naccount = "claude_code:main"\n'
    )
    assert _reap({"manifest": str(manifest), "overlay": str(overlay)}) == 0
    assert "no checkouts configured" in capsys.readouterr().out


def test_a_work_claimed_worktree_is_kept_even_with_no_card_left(
    tmp_path, checkout, capsys, work_claims
):
    """A Work's worktree outlives its card record; the reaper subtracts it too (#2149)."""
    path = _worktree(checkout, "issue-21", branch="agent/issue-21")
    _old(path)
    work_claims[0].append(str(path))
    files = _instance(tmp_path, checkout)

    assert _reap(files, "--apply") == 0
    assert reaper.CLAIMED in capsys.readouterr().out
    assert path.exists()


def test_unreadable_work_claims_refuse_an_apply_and_say_so_on_a_report(
    tmp_path, checkout, capsys, work_claims
):
    path = _worktree(checkout, "issue-22", branch="agent/issue-22")
    _old(path)
    work_claims[1]["readable"] = False
    files = _instance(tmp_path, checkout)

    assert _reap(files) == 0
    assert "could not be read" in capsys.readouterr().err
    assert _reap(files, "--apply") == 75
    assert path.exists()


def test_the_launcher_supplies_the_apps_own_runtime_socket(monkeypatch, tmp_path):
    import importlib.util

    launcher = Path(__file__).resolve().parent.parent / "scripts" / "poller_engine.py"
    spec = importlib.util.spec_from_file_location("poller_engine_launcher", launcher)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv("AGENTDECK_CODEX_SOCKET", str(tmp_path / "x.sock"))
    assert module.with_host_defaults(["reap-worktrees", "--apply"]) == [
        "reap-worktrees",
        "--apply",
        "--runtime-socket",
        str(tmp_path / "x.sock"),
    ]
    named = ["reap-worktrees", "--runtime-socket", "/s"]
    assert module.with_host_defaults(named) == named
    assert module.with_host_defaults(["cycle"]) == ["cycle"]


def test_without_a_runtime_socket_an_apply_refuses(tmp_path, checkout, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_work_claimed_paths", lambda _s: [])  # never reached
    path = _worktree(checkout, "issue-23", branch="agent/issue-23")
    _old(path)
    files = _instance(tmp_path, checkout)

    argv = ["reap-worktrees", "--manifest", files["manifest"], "--overlay", files["overlay"]]
    assert cli.main([*argv, "--apply"]) == 75
    assert path.exists()
