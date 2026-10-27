import subprocess

from agentdeck.deployment import (
    _commit_subject,
    _read_blocked_reason,
    deployment_status,
    is_deploy_checkout,
)


def test_is_deploy_checkout_matches_only_the_checkout_root(tmp_path, monkeypatch):
    """Subdirectories — .worktrees/* especially — are exactly where work is
    supposed to happen (#786)."""
    repo = tmp_path / "live"
    worktree = repo / ".worktrees" / "issue-1"
    worktree.mkdir(parents=True)
    monkeypatch.setenv("AGENTDECK_REPO", str(repo))

    assert is_deploy_checkout(repo)
    assert is_deploy_checkout(str(repo))
    assert not is_deploy_checkout(worktree)
    assert not is_deploy_checkout(tmp_path / "elsewhere")
    assert not is_deploy_checkout(None)
    assert not is_deploy_checkout("")


def test_is_deploy_checkout_resolves_symlinked_spellings(tmp_path, monkeypatch):
    repo = tmp_path / "live"
    repo.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(repo)
    monkeypatch.setenv("AGENTDECK_REPO", str(repo))

    assert is_deploy_checkout(alias)


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_commit_subject_resolves_a_deploy_marker(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "release.txt").write_text("ready\n")
    _git(tmp_path, "add", "release.txt")
    _git(tmp_path, "commit", "-m", "Show useful deployment names")
    sha = _git(tmp_path, "rev-parse", "HEAD")

    _commit_subject.cache_clear()

    assert _commit_subject(str(tmp_path), sha) == "Show useful deployment names"


def test_commit_subject_returns_none_for_an_unknown_marker(tmp_path):
    _git(tmp_path, "init")

    _commit_subject.cache_clear()

    assert _commit_subject(str(tmp_path), "a" * 40) is None


def test_a_missing_or_empty_refusal_marker_is_not_a_blocked_deploy(tmp_path):
    """Absence and a torn write are the same thing here: no red pill without a
    reason to show in it."""
    assert _read_blocked_reason(tmp_path / "blocked") is None

    empty = tmp_path / "blocked"
    empty.write_text("\n  \n")
    assert _read_blocked_reason(empty) is None


def test_a_refusal_marker_reaches_the_pill_as_one_bounded_line(tmp_path):
    """The reason is script output, but it renders in every page's topbar, so only
    the first line survives and it cannot run away."""
    marker = tmp_path / "blocked"
    marker.write_text("live checkout is dirty\nand a second line\n")
    assert _read_blocked_reason(marker) == "live checkout is dirty"

    marker.write_text("noisy\x07 reason\n")
    assert _read_blocked_reason(marker) == "noisy reason"

    marker.write_text("x" * 500)
    capped = _read_blocked_reason(marker)
    assert capped is not None
    assert len(capped) == 200
    assert capped.endswith("…")


def test_a_blocked_deploy_is_visible_without_any_release_markers(tmp_path, monkeypatch):
    """`available` gates the whole pill, and a host that has never recorded a
    release is exactly where a stalled deploy is easiest to miss (#747)."""
    monkeypatch.setenv("AGENTDECK_DEPLOY_STATE", str(tmp_path))
    (tmp_path / "blocked").write_text("live checkout is dirty and a deploy is waiting\n")

    status = deployment_status()

    assert status.blocked
    assert status.available
    assert status.blocked_reason == "live checkout is dirty and a deploy is waiting"


def test_an_unapplied_poller_manifest_shows_without_blocking(tmp_path, monkeypatch):
    """#2555: the deploy went through; only a project's settings did not."""
    monkeypatch.setenv("AGENTDECK_DEPLOY_STATE", str(tmp_path))
    (tmp_path / "manifest_failed").write_text("bazaar: exit 77 refused; trailhead: exit 75\n")

    status = deployment_status()

    assert status.available
    assert not status.blocked
    assert status.poller_note == "bazaar: exit 77 refused; trailhead: exit 75"
