"""A Work start brings the project's shared checkouts up to GitHub's master (#4233)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentdeck.checkout_refresh import refresh_checkout, refresh_checkouts


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(path: Path, name: str) -> str:
    (path / name).write_text(name)
    git(path, "add", name)
    git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path):
    """A checkout one commit behind its origin's master, on master."""
    origin = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "-b", "master", str(origin)], check=True)
    commit(origin, "a")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    tip = commit(origin, "b")
    return checkout, tip


def test_a_stale_checkout_on_master_moves_to_origins_tip(clone):
    clone, tip = clone
    assert refresh_checkout(str(clone)) == "updated"
    assert git(clone, "rev-parse", "HEAD") == tip
    assert refresh_checkout(str(clone)) == "current"


def test_a_detached_or_feature_branch_checkout_goes_onto_master(clone):
    clone, tip = clone
    # Left detached on an old commit, as metso was; then on a branch holding nothing new.
    git(clone, "switch", "-q", "--detach", "HEAD")
    assert refresh_checkout(str(clone)) == "updated"
    assert git(clone, "branch", "--show-current") == "master"
    assert git(clone, "rev-parse", "HEAD") == tip
    git(clone, "switch", "-q", "-c", "hotfix")
    assert refresh_checkout(str(clone)) == "updated"
    assert git(clone, "branch", "--show-current") == "master"
    assert "hotfix" in git(clone, "branch")  # the branch itself is kept


def test_nothing_is_lost_or_moved_under_local_work(clone):
    clone, _ = clone
    before = git(clone, "rev-parse", "HEAD")
    (clone / "a").write_text("edited")
    assert refresh_checkout(str(clone)) == "dirty"
    git(clone, "checkout", "-q", "--", "a")
    git(clone, "switch", "-q", "-c", "mine")
    commit(clone, "c")
    assert refresh_checkout(str(clone)) == "own commits"
    assert git(clone, "branch", "--show-current") == "mine"
    git(clone, "switch", "-q", "--detach", before)
    # A local master with commits origin lacks is never reset.
    git(clone, "branch", "-qf", "master", "mine")
    assert refresh_checkout(str(clone)) == "local master has own commits"
    assert git(clone, "rev-parse", "HEAD") == before


def test_a_deck_checkout_or_a_missing_folder_is_left_alone(clone, tmp_path):
    clone, _ = clone
    (clone / "src" / "agentdeck").mkdir(parents=True)
    before = git(clone, "rev-parse", "HEAD")
    assert refresh_checkouts([str(clone), str(tmp_path / "nowhere"), ""]) == {
        str(clone): "an AgentDeck checkout",
        str(tmp_path / "nowhere"): "not a main checkout",
    }
    assert git(clone, "rev-parse", "HEAD") == before


def test_git_runs_with_the_decks_github_environment(clone, monkeypatch):
    """The deck's GitHub rule and no-prompt settings (#2289, #3632) reach every git step:
    an environment that points `origin` nowhere makes the fetch fail."""
    import os

    from agentdeck import github_identity

    clone, _ = clone
    nowhere = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{clone.parent / 'nowhere'}.insteadOf",
        "GIT_CONFIG_VALUE_0": str(clone.parent / "origin"),
    }
    monkeypatch.setattr(github_identity, "github_git_environment", lambda: nowhere)
    assert refresh_checkout(str(clone)).startswith("fetch failed")
