"""The host's store of reusable base-branch results, and the identity it keys them by (#1737)."""

from __future__ import annotations

import subprocess

from agentdeck.poller_engine.workspace import collect_workspace_snapshot
from agentdeck.verification_baselines import BaselineLedger


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(path):
    """A repository with an origin and a branch off it, as a card's worktree really has."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "remote", "add", "origin", "git@github.com:owner/repo.git")
    (path / "file.txt").write_text("base\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/remotes/origin/master", base)
    return base


# ---- what a snapshot now knows about its base ------------------------------


def test_a_snapshot_records_the_commit_this_checkout_branched_off(tmp_path):
    repo = tmp_path / "repo"
    base = _repo(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "file.txt").write_text("changed\n")
    _git(repo, "commit", "-qam", "work")

    snapshot = collect_workspace_snapshot(str(repo), command=["pytest", "-q"])

    assert snapshot.provable
    assert snapshot.base_ref == "origin/master"
    assert snapshot.base_head == base
    assert snapshot.head != base
    assert snapshot.repo_identity


def test_the_same_repository_has_one_identity_however_its_remote_is_written(tmp_path):
    ssh = tmp_path / "ssh"
    https = tmp_path / "https"
    _repo(ssh)
    _repo(https)
    _git(https, "remote", "set-url", "origin", "https://github.com/owner/repo.git")

    assert (
        collect_workspace_snapshot(str(ssh)).repo_identity
        == collect_workspace_snapshot(str(https)).repo_identity
    )


def test_a_checkout_with_no_remote_stays_provable_and_simply_cannot_compare(tmp_path):
    # Losing the ability to compare must never cost a card its workspace proof: an unprovable
    # snapshot parks the card, and a repository without an origin has done nothing wrong.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("only\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "only")

    snapshot = collect_workspace_snapshot(str(repo))

    assert snapshot.provable
    assert snapshot.repo_identity == ""
    assert snapshot.base_head == ""


def test_the_base_fields_stay_out_of_the_id_a_checkpoint_is_fenced_against(tmp_path):
    # The expected and tested snapshots are collected by different processes at different
    # moments. If a base field entered this id, every checkpoint already in flight would stop
    # matching the job it is waiting for and its card would park as stale.
    repo = tmp_path / "repo"
    _repo(repo)
    snapshot = collect_workspace_snapshot(str(repo), command=["pytest", "-q"])

    from dataclasses import replace

    assert (
        replace(snapshot, base_head="somethingelse", base_ref="origin/main").snapshot_id
        == snapshot.snapshot_id
    )
    assert replace(snapshot, repo_identity="other").snapshot_id == snapshot.snapshot_id


def test_a_committed_tree_is_clean_and_an_edited_one_is_not(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)

    assert collect_workspace_snapshot(str(repo)).clean is True

    (repo / "file.txt").write_text("edited\n")
    assert collect_workspace_snapshot(str(repo)).clean is False

    _git(repo, "checkout", "--", "file.txt")
    (repo / "scratch.txt").write_text("untracked\n")
    assert collect_workspace_snapshot(str(repo)).clean is False


# ---- the ledger ------------------------------------------------------------


def test_a_recorded_result_is_found_again_by_its_key(tmp_path):
    ledger = BaselineLedger(tmp_path / "baselines.json")

    ledger.record({"key": "k1", "head": "sha1", "failures": ["a"], "recorded_at": 10.0})

    assert ledger.lookup("k1")["failures"] == ["a"]
    assert ledger.lookup("k2") is None
    assert ledger.lookup("") is None


def test_a_newer_result_for_the_same_key_replaces_the_older_one(tmp_path):
    ledger = BaselineLedger(tmp_path / "baselines.json")
    ledger.record({"key": "k1", "failures": ["a"], "recorded_at": 10.0})

    ledger.record({"key": "k1", "failures": ["a", "b"], "recorded_at": 20.0})

    assert ledger.lookup("k1")["failures"] == ["a", "b"]


def test_the_ledger_is_bounded_by_count_and_age(tmp_path):
    ledger = BaselineLedger(
        tmp_path / "baselines.json", max_entries=2, max_age_s=100.0, clock=lambda: 1000.0
    )
    ledger.record({"key": "ancient", "recorded_at": 1.0})
    ledger.record({"key": "k1", "recorded_at": 950.0})
    ledger.record({"key": "k2", "recorded_at": 960.0})
    ledger.record({"key": "k3", "recorded_at": 970.0})

    assert ledger.lookup("ancient") is None
    assert ledger.lookup("k1") is None
    assert ledger.lookup("k2") is not None
    assert ledger.lookup("k3") is not None


def test_an_unreadable_ledger_is_a_missing_baseline_and_never_an_error(tmp_path):
    path = tmp_path / "baselines.json"
    path.write_text("{not json", encoding="utf-8")
    ledger = BaselineLedger(path)

    assert ledger.lookup("k1") is None
    assert ledger.record({"key": "k1", "recorded_at": 1.0}) is True
    assert ledger.lookup("k1") is not None


def test_a_result_with_no_key_is_never_stored(tmp_path):
    ledger = BaselineLedger(tmp_path / "baselines.json")

    assert ledger.record({"head": "sha1"}) is False
    assert not (tmp_path / "baselines.json").exists()
