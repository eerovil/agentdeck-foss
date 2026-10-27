"""#1095: one directory, one recorded spelling.

These tests build the ostree host layout by hand — a `home` symlink pointing at `var/home` —
because on a plain Linux box `/home` is a real directory and the whole defect is invisible.
"""

from __future__ import annotations

from pathlib import Path

from agentdeck.poller_engine import paths


def _ostree_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Return the real worktree path and the `/home`-style alias for the same directory."""
    real = tmp_path / "var" / "home" / "eero" / "repo" / ".worktrees" / "issue-1"
    real.mkdir(parents=True)
    (tmp_path / "home").symlink_to(tmp_path / "var" / "home")
    alias = tmp_path / "home" / "eero" / "repo" / ".worktrees" / "issue-1"
    assert str(alias) != str(real)
    return real, alias


def test_both_spellings_of_one_directory_resolve_to_one_string(tmp_path):
    real, alias = _ostree_layout(tmp_path)
    assert paths.resolve(str(alias)) == paths.resolve(str(real)) == str(real)


def test_a_path_that_cannot_be_resolved_keeps_its_own_spelling():
    """Failing to resolve must not raise and must not make two paths equal by accident."""
    assert paths.resolve("/nowhere/at/all") == "/nowhere/at/all"
    assert paths.resolve("/nowhere/at/all") != paths.resolve("/nowhere/at/other")


def test_a_relative_or_user_path_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.resolve("~/repo") == str(tmp_path.resolve() / "repo")
    assert Path(paths.resolve("repo")).is_absolute()


def test_a_recorded_list_collapses_the_two_spellings_to_the_first_one_seen(tmp_path):
    real, alias = _ostree_layout(tmp_path)
    assert paths.resolve_all([str(alias), str(real)]) == [str(real)]
    assert paths.resolve_all([str(real), str(alias)]) == [str(real)]


def test_a_recorded_list_keeps_its_order_and_drops_what_is_not_a_path():
    assert paths.resolve_all(["/a/one", 7, None, "/a/two", "/a/one"]) == ["/a/one", "/a/two"]


def test_a_missing_or_broken_record_reads_as_no_paths():
    assert paths.resolve_all(None) == []
    assert paths.resolve_all("/a/one") == []
    assert paths.resolve_all({"worktrees": "/a/one"}) == []
