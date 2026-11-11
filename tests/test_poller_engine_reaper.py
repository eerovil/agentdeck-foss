"""The disk-side worktree reaper (#1094).

Cleanup elsewhere in the engine only ever removes a path some record named. These tests are
about the opposite direction — starting from what is on disk — and so nearly all of them are
about *not* deleting something: the claimed set surviving two spellings of the same
directory, and every unreadable fact keeping the directory instead of releasing it.
"""

from __future__ import annotations

import json
import math
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


# --- saving idle dirty or unmerged worktrees (#4267) -------------------------------------


def _old_tree(path: Path, days: int = 400) -> None:
    """Backdate every file and folder in a worktree, as one left alone for ``days``."""
    when = datetime.now(UTC).timestamp() - days * 86400
    for parent, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            os.utime(os.path.join(parent, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when))


def _dirty_detached(checkout: Path, name: str) -> Path:
    """A detached worktree with a commit no remote has, a changed file and a new one."""
    path = checkout / ".worktrees" / name
    _git(checkout, "worktree", "add", "-q", "--detach", str(path))
    (path / "file.txt").write_text("committed\n")
    _git(path, "commit", "-qam", "local")
    (path / "file.txt").write_text("changed\n")
    (path / "new" / "deep").mkdir(parents=True)
    (path / "new" / "deep" / "note.txt").write_text("untracked\n")
    return path


def _later(monkeypatch, days: int = 400) -> datetime:
    """Move the CLI's clock ``days`` ahead: a change time cannot be set back, only waited out."""
    later = datetime.now(UTC) + timedelta(days=days)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(cli, "datetime", Clock)
    return later


def test_untouched_for_reads_every_file_not_only_the_folder(checkout):
    path = _worktree(checkout, "issue-20", branch="agent/issue-20")
    _old_tree(path)
    now = datetime.now(UTC) + timedelta(days=400)
    assert reaper.untouched_for(str(path), 30, now=now) is True
    (path / "file.txt").write_text("fresh\n")
    os.utime(path / "file.txt", (now.timestamp(), now.timestamp()))
    os.utime(path, (0, 0))
    assert reaper.untouched_for(str(path), 30, now=now) is False
    assert reaper.untouched_for(str(path / "gone"), 30, now=now) is None


def test_fresh_bytes_under_an_old_modification_time_are_not_idle(checkout):
    """``cp -p`` or ``touch -d`` keeps an old mtime; the change time still shows the write."""
    path = _worktree(checkout, "issue-21", branch="agent/issue-21")
    _old_tree(path)  # every mtime is old, but the change times are today
    assert reaper.untouched_for(str(path), 30, now=datetime.now(UTC)) is False


def test_in_use_matches_the_folder_and_below_but_not_a_sibling():
    assert reaper.in_use("/w/issue-1", ["/w/issue-1/src"])
    assert reaper.in_use("/w/issue-1", ["/w/issue-1"])
    assert not reaper.in_use("/w/issue-1", ["/w/issue-10", "/w"])


def _free(*paths):
    return ""


def test_save_and_remove_keeps_every_kind_of_work_then_removes(tmp_path, checkout):
    path = _dirty_detached(checkout, "issue-21")
    head = _git(path, "rev-parse", "HEAD")
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)

    assert reaper.save_and_remove(item, leftovers, now=NOW, busy=_free) == ""

    saved = leftovers / "repo__issue-21"
    assert (saved / "status.txt").read_text().splitlines()[:2] == [str(path), head]
    assert "+changed" in (saved / "changes.patch").read_text()
    assert (saved / "untracked" / "new" / "deep" / "note.txt").read_text() == "untracked\n"
    assert head in _git(checkout, "bundle", "list-heads", str(saved / "head.bundle"))
    assert not path.exists()
    assert str(path) not in _git(checkout, "worktree", "list", "--porcelain")


def test_a_second_save_of_the_same_name_does_not_overwrite_the_first(tmp_path, checkout):
    path = _worktree(checkout, "issue-22", branch="agent/issue-22")
    (path / "scratch.txt").write_text("second\n")
    leftovers = tmp_path / "leftovers"
    (leftovers / "repo__issue-22").mkdir(parents=True)
    (item,) = reaper.scan([str(checkout)], now=NOW)

    assert reaper.save_and_remove(item, leftovers, now=NOW, busy=_free) == ""

    assert list((leftovers / "repo__issue-22").iterdir()) == []
    assert (leftovers / f"repo__issue-22-{NOW:%Y%m%d%H%M%S}" / "untracked" / "scratch.txt").exists()
    assert not (leftovers / f"repo__issue-22-{NOW:%Y%m%d%H%M%S}" / "head.bundle").exists()


def test_a_locked_worktree_is_neither_saved_nor_removed(tmp_path, checkout):
    path = _worktree(checkout, "issue-23", branch="agent/issue-23")
    (path / "scratch.txt").write_text("x\n")
    _git(checkout, "worktree", "lock", str(path))
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)

    assert "locked" in reaper.save_and_remove(item, leftovers, now=NOW, busy=_free)
    assert path.exists() and list(leftovers.iterdir()) == []


def test_apply_saves_only_idle_unused_dirty_worktrees(tmp_path, checkout, capsys, monkeypatch):
    idle = _dirty_detached(checkout, "issue-24")
    busy = _dirty_detached(checkout, "issue-25")
    fresh = _dirty_detached(checkout, "issue-26")
    for path in (idle, busy, fresh):
        _old_tree(path)
    today = _later(monkeypatch).timestamp()
    (fresh / "file.txt").write_text("today\n")
    os.utime(fresh / "file.txt", (today, today))
    monkeypatch.setattr(
        reaper,
        "paths_in_use",
        lambda: reaper.ProcessUse(frozenset({os.path.realpath(busy / "new")}), ()),
    )
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    files = _instance(tmp_path, checkout)
    flags = ("--save-after-days", "30", "--leftovers", str(leftovers))

    assert _reap(files, *flags) == 0
    assert f"would save {idle}" in capsys.readouterr().out
    assert idle.exists() and list(leftovers.iterdir()) == []

    assert _reap(files, "--apply", *flags) == 0
    out = capsys.readouterr().out
    assert f"saved {idle}" in out and str(busy) not in out.split("released")[-1]
    assert not idle.exists() and busy.exists() and fresh.exists()
    assert [entry.name for entry in leftovers.iterdir()] == ["repo__issue-24"]


def test_a_missing_leftovers_folder_saves_nothing(tmp_path, checkout, capsys):
    path = _dirty_detached(checkout, "issue-27")
    _old_tree(path)
    files = _instance(tmp_path, checkout)

    flags = ("--save-after-days", "30", "--leftovers", str(tmp_path / "unmounted"))
    assert _reap(files, "--apply", *flags) == 0

    assert "is missing; nothing saved" in capsys.readouterr().out
    assert path.exists()


def test_a_file_written_during_the_save_keeps_the_worktree(tmp_path, checkout):
    path = _worktree(checkout, "issue-28", branch="agent/issue-28")
    (path / "scratch.txt").write_text("x\n")
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)

    def runner(cmd, **kwargs):
        if "rev-list" in cmd:  # between the save and the second status read
            # A writer that held the folder open before the move writes into it there.
            (path.with_name(".issue-28.saving") / "late.txt").write_text("written mid-save\n")
        return subprocess.run(cmd, **kwargs)

    assert reaper.save_and_remove(item, leftovers, now=NOW, runner=runner, busy=_free) == (
        "it changed while being saved"
    )
    assert (path / "late.txt").exists()


def test_ignored_files_are_saved_except_rebuildable_folders(tmp_path, checkout):
    path = _worktree(checkout, "issue-29", branch="agent/issue-29")
    (path / ".gitignore").write_text(".env\nnode_modules/\nlocal/\n")
    (path / ".env").write_text("SECRET=1\n")
    (path / "local").mkdir()
    (path / "local" / "data.db").write_text("rows\n")
    (path / "package.json").write_text("{}\n")
    (path / "node_modules" / "pkg").mkdir(parents=True)
    (path / "node_modules" / "pkg" / "index.js").write_text("x\n")
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)

    assert reaper.save_and_remove(item, leftovers, now=NOW, busy=_free) == ""

    saved = leftovers / "repo__issue-29"
    assert (saved / "ignored" / ".env").read_text() == "SECRET=1\n"
    assert (saved / "ignored" / "local" / "data.db").read_text() == "rows\n"
    assert not (saved / "ignored" / "node_modules").exists()
    assert not path.exists()


def test_too_many_ignored_bytes_keep_the_worktree(tmp_path, checkout, monkeypatch):
    path = _worktree(checkout, "issue-30", branch="agent/issue-30")
    (path / ".gitignore").write_text("big.bin\n")
    (path / "big.bin").write_bytes(b"x" * 100)
    monkeypatch.setattr(reaper, "IGNORED_SAVE_LIMIT", 10)
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)

    assert "too much to save" in reaper.save_and_remove(item, leftovers, now=NOW, busy=_free)
    assert path.exists()


def test_new_bytes_in_an_already_untracked_file_keep_the_worktree(tmp_path, checkout):
    """Same name, same status line: only the change time shows the write."""
    path = _worktree(checkout, "issue-31", branch="agent/issue-31")
    note = path / "note.txt"
    note.write_text("first\n")
    _old_tree(path)
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)
    old = note.stat().st_mtime

    def runner(cmd, **kwargs):
        if "rev-list" in cmd:  # after the copy, before the removal
            held = path.with_name(".issue-31.saving") / "note.txt"  # open before the move
            held.write_text("second, not in the save\n")
            os.utime(held, (old, old))
        return subprocess.run(cmd, **kwargs)

    assert reaper.save_and_remove(item, leftovers, now=NOW, runner=runner, busy=_free) == (
        "it changed while being saved"
    )
    assert note.read_text() == "second, not in the save\n"


def test_paths_in_use_reads_open_files_and_maps_not_only_folders(tmp_path):
    proc = tmp_path / "proc"
    (proc / "12" / "fd").mkdir(parents=True)
    (proc / "12" / "cwd").symlink_to("/elsewhere")
    (proc / "12" / "fd" / "3").symlink_to("/w/issue-1/log.txt")
    (proc / "12" / "maps").write_text("7f00-7f01 r-xp 0 08:01 9 /w/issue-2/lib.so\n")
    (proc / "self").mkdir()

    (proc / "13").mkdir()  # ended mid-scan: its links are gone
    (proc / "14" / "fd").mkdir(parents=True)  # alive, but not ours to read
    (proc / "14" / "cwd").symlink_to("/w/issue-4")
    (proc / "14" / "fd").chmod(0)

    try:
        use = reaper.paths_in_use(proc)
    finally:
        (proc / "14" / "fd").chmod(0o700)

    assert reaper.in_use("/w/issue-1", use.paths) and reaper.in_use("/w/issue-2", use.paths)
    assert not reaper.in_use("/w/issue-3", use.paths)
    assert use.unreadable == ((14, os.getuid()),)
    # Its cwd was read before the refusal: that still counts, even with it exempt as ours.
    assert reaper.unknown_use(use, "/w/issue-4") == "a process is using it"
    assert reaper.paths_in_use(tmp_path / "no-proc") is None


def test_an_unreadable_process_blocks_the_save_only_when_it_could_reach_it(tmp_path, monkeypatch):
    private = tmp_path / "home"
    private.mkdir(mode=0o700)
    (private / "w").mkdir()
    shared = tmp_path / "shared"
    assert reaper.private_to_us(str(private / "w")) and not reaper.private_to_us("/usr")
    # A name not made yet, or already moved away, is judged by the folders it sits in.
    assert reaper.private_to_us(str(private / "w" / ".issue-1.saving" / "x"))
    assert not reaper.private_to_us("/usr/.issue-1.saving/x")
    monkeypatch.setattr(reaper, "private_to_us", lambda path: path.startswith(str(private)))

    def use(*unreadable):
        return reaper.ProcessUse(frozenset(), unreadable)

    other = os.getuid() + 1
    assert reaper.unknown_use(use(), str(private / "w")) == ""
    assert reaper.unknown_use(use((7, other)), str(private / "w")) == ""
    assert "cannot be read" in reaper.unknown_use(use((7, other)), str(shared))
    assert "cannot be read" in reaper.unknown_use(use((7, -1)), str(private / "w"))
    # The owner's call: root's and our own protected processes count as not using it.
    assert reaper.unknown_use(use((7, 0), (8, os.getuid())), str(shared)) == ""
    assert reaper.unknown_use(None, str(private)) == "the process list could not be read"
    busy = reaper.ProcessUse(frozenset({str(private / "w" / "f")}), ())
    assert reaper.unknown_use(busy, str(private / "w")) == "a process is using it"


def test_another_users_unreadable_process_does_not_block_a_save_in_our_private_home(
    tmp_path, checkout, monkeypatch
):
    """Every scan, before the move and after it, asks about names that do not exist then."""
    assert reaper.private_to_us(str(checkout))  # pytest's own folder is ours alone
    path = _worktree(checkout, "issue-36", branch="agent/issue-36")
    (path / "scratch.txt").write_text("x\n")
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)
    foreign = reaper.ProcessUse(frozenset(), ((7, os.getuid() + 1),))
    monkeypatch.setattr(reaper, "paths_in_use", lambda: foreign)

    assert reaper.save_and_remove(item, leftovers, now=NOW) == ""
    assert not path.exists()


def test_the_save_is_private_whatever_the_umask(tmp_path, checkout):
    path = _worktree(checkout, "issue-33", branch="agent/issue-33")
    (path / ".gitignore").write_text(".env\n")
    (path / ".env").write_text("SECRET=1\n")
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)
    old = os.umask(0o022)
    try:
        assert reaper.save_and_remove(item, leftovers, now=NOW, busy=_free) == ""
    finally:
        os.umask(old)

    assert (leftovers / "repo__issue-33").stat().st_mode & 0o777 == 0o700


def test_the_worktree_is_moved_aside_before_the_save_and_back_on_failure(
    tmp_path, checkout, monkeypatch
):
    """A writer using the old path cannot reach it mid-save; any failure restores it."""
    path = _worktree(checkout, "issue-34", branch="agent/issue-34")
    (path / "scratch.txt").write_text("x\n")
    aside = path.with_name(".issue-34.saving")
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)
    seen = []

    def runner(cmd, **kwargs):
        if "rev-list" in cmd:
            seen.append((path.exists(), aside.exists()))
        return subprocess.run(cmd, **kwargs)

    def flush_fails(dest):
        raise OSError("disk full")

    monkeypatch.setattr(reaper, "_flush", flush_fails)
    why = reaper.save_and_remove(item, leftovers, now=NOW, runner=runner, busy=_free)

    assert why == "could not flush the save to disk: disk full"
    assert seen == [(False, True)]
    assert (path / "scratch.txt").exists() and not aside.exists()
    assert str(path) in _git(checkout, "worktree", "list", "--porcelain")


def test_a_process_found_after_the_save_keeps_the_worktree(tmp_path, checkout):
    path = _worktree(checkout, "issue-35", branch="agent/issue-35")
    (path / "scratch.txt").write_text("x\n")
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)
    asked = []

    def busy(*paths):
        asked.append(paths)
        return "a process is using it" if len(asked) > 1 else ""

    assert reaper.save_and_remove(item, leftovers, now=NOW, busy=busy) == "a process is using it"
    assert asked[1][:2] == (str(path), str(path.with_name(".issue-35.saving")))
    assert Path(asked[1][2]).parent == checkout / ".worktrees" / reaper.TRASH
    assert (path / "scratch.txt").exists()


def test_an_unreadable_process_list_saves_nothing(tmp_path, checkout, capsys, monkeypatch):
    path = _dirty_detached(checkout, "issue-32")
    _old_tree(path)
    monkeypatch.setattr(reaper, "paths_in_use", lambda: None)
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    files = _instance(tmp_path, checkout)

    assert _reap(files, "--apply", "--save-after-days", "30", "--leftovers", str(leftovers)) == 0

    assert "process list could not be read; nothing saved" in capsys.readouterr().out
    assert path.exists()


def test_a_bad_save_after_days_is_refused_before_any_scan(tmp_path, checkout, capsys):
    """A negative or NaN count would let a worktree edited today pass as idle."""
    path = _dirty_detached(checkout, "issue-36")
    files = _instance(tmp_path, checkout)
    for bad in ("-7", "nan", "inf"):
        with pytest.raises(SystemExit):
            _reap(files, "--apply", "--save-after-days", bad, "--leftovers", str(tmp_path))
        assert "--save-after-days" in capsys.readouterr().err
    assert path.exists()
    for days in (-1.0, math.nan, math.inf, 0.0):
        assert reaper.untouched_for(str(path), days, now=NOW) is None


def _no_process(monkeypatch):
    monkeypatch.setattr(reaper, "paths_in_use", lambda: reaper.ProcessUse(frozenset(), ()))


def test_a_save_cut_off_after_the_first_move_resumes_on_the_next_run(
    tmp_path, checkout, monkeypatch, capsys
):
    """The move rewrites ``.git``, so the idle gate must not hold the resumed save back."""
    path = _dirty_detached(checkout, "issue-37")
    aside = path.with_name(".issue-37.saving")
    assert reaper.note_moved(checkout, str(aside), str(path)) == ""
    _git(checkout, "worktree", "move", str(path), str(aside))  # then the run died
    _no_process(monkeypatch)
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    files = _instance(tmp_path, checkout)

    assert _reap(files, "--apply", "--save-after-days", "30", "--leftovers", str(leftovers)) == 0

    assert f"saved {aside}" in capsys.readouterr().out
    saved = leftovers / "repo__issue-37"
    assert (saved / "status.txt").read_text().splitlines()[0] == str(path)
    assert (saved / "untracked" / "new" / "deep" / "note.txt").exists()
    assert not aside.exists() and not path.exists()


def test_a_save_cut_off_in_the_private_folder_resumes_on_the_next_run(
    tmp_path, checkout, monkeypatch, capsys
):
    path = _dirty_detached(checkout, "issue-38")
    hidden = checkout / ".worktrees" / reaper.TRASH / "issue-38.0123abcd"
    assert reaper.note_moved(checkout, str(path.with_name(".issue-38.saving")), str(path)) == ""
    _git(checkout, "worktree", "move", str(path), str(hidden))  # then the run died
    _no_process(monkeypatch)
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    files = _instance(tmp_path, checkout)

    assert _reap(files, "--apply", "--save-after-days", "30", "--leftovers", str(leftovers)) == 0

    out = capsys.readouterr().out
    assert "unregistered" not in out and "save-stuck" not in out
    assert (leftovers / "repo__issue-38" / "head.bundle").exists()
    assert list(hidden.parent.iterdir()) == [hidden.parent / reaper.MOVED] and not path.exists()
    assert list((hidden.parent / reaper.MOVED).iterdir()) == []


def test_a_worktree_only_named_like_a_cut_off_save_waits_its_idle_days(
    tmp_path, checkout, monkeypatch, capsys
):
    """Without this cleanup's own note, ``.<name>.saving`` is just somebody's worktree."""
    path = _dirty_detached(checkout, ".issue-42.saving")
    _no_process(monkeypatch)
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    files = _instance(tmp_path, checkout)
    flags = ("--apply", "--save-after-days", "30", "--leftovers", str(leftovers))

    assert _reap(files, *flags) == 0
    assert "saved" not in capsys.readouterr().out
    assert path.exists() and list(leftovers.iterdir()) == []

    _later(monkeypatch)  # once idle, it is saved as itself, under its own name
    (item,) = [i for i in reaper.scan([str(checkout)], now=NOW) if i.path == str(path)]
    assert reaper.moved_aside(item) is None
    assert _reap(files, *flags) == 0
    assert f"saved {path}" in capsys.readouterr().out
    assert (leftovers / "repo__.issue-42.saving" / "status.txt").read_text().splitlines()[0] == (
        str(path)
    )


def test_two_runs_at_once_save_the_worktree_once(tmp_path, checkout):
    """The second run finds the worktree already moved and keeps its hands off."""
    path = _worktree(checkout, "issue-39", branch="agent/issue-39")
    (path / "scratch.txt").write_text("x\n")
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)
    second = []

    def runner(cmd, **kwargs):
        if "rev-list" in cmd and not second:
            second.append(reaper.save_and_remove(item, leftovers, now=NOW, busy=_free))
        return subprocess.run(cmd, **kwargs)

    assert reaper.save_and_remove(item, leftovers, now=NOW, runner=runner, busy=_free) == ""
    assert second == ["no longer a registered worktree"]
    assert [entry.name for entry in leftovers.iterdir()] == ["repo__issue-39"]
    assert not path.exists()


def test_a_write_into_the_private_folder_before_removal_keeps_the_worktree(tmp_path, checkout):
    """Something that still had the folder open writes after the last move: it is kept."""
    path = _worktree(checkout, "issue-40", branch="agent/issue-40")
    (path / "scratch.txt").write_text("x\n")
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)

    def runner(cmd, **kwargs):
        done = subprocess.run(cmd, **kwargs)
        if "move" in cmd and reaper.TRASH in cmd[-1]:
            (Path(cmd[-1]) / "late.txt").write_text("written after the save\n")
        return done

    assert reaper.save_and_remove(item, leftovers, now=NOW, runner=runner, busy=_free) == (
        "it changed while being saved"
    )
    assert (path / "late.txt").exists()
    assert str(path) in _git(checkout, "worktree", "list", "--porcelain")


def test_only_folders_proven_rebuildable_are_left_out_of_the_save(tmp_path, checkout):
    """A name like ``build`` proves nothing; a tool's own mark does."""
    path = _worktree(checkout, "issue-41", branch="agent/issue-41")
    (path / ".gitignore").write_text("build/\ndata/\n.cache/\n.pytest_cache/\n__pycache__/\n")
    (path / "build").mkdir()
    (path / "build" / "results.csv").write_text("hand-made\n")
    (path / "data" / "build").mkdir(parents=True)
    (path / "data" / "build" / "rows.db").write_text("rows\n")
    (path / ".cache").mkdir()
    (path / ".cache" / "notes.txt").write_text("mine\n")
    (path / ".pytest_cache").mkdir()
    (path / ".pytest_cache" / "CACHEDIR.TAG").write_text(
        "Signature: 8a477f597d28d172789f06886806bc55\n"
    )
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"x")
    leftovers = tmp_path / "leftovers"
    leftovers.mkdir()
    (item,) = reaper.scan([str(checkout)], now=NOW)

    assert reaper.save_and_remove(item, leftovers, now=NOW, busy=_free) == ""

    ignored = leftovers / "repo__issue-41" / "ignored"
    assert (ignored / "build" / "results.csv").exists()
    assert (ignored / "data" / "build" / "rows.db").exists()
    assert (ignored / ".cache" / "notes.txt").exists()
    assert not (ignored / ".pytest_cache").exists() and not (ignored / "__pycache__").exists()
