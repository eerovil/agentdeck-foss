from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from agentdeck.worktree_diff import (
    MAX_FILES,
    MAX_LINES_PER_FILE,
    MAX_TOTAL_LINES,
    WorktreeDiff,
    WorktreeDiffReader,
    classify_path,
    parse_patch,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "--initial-branch=master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "kept.txt").write_text("one\ntwo\nthree\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _branch_with_commit(repo: Path) -> None:
    _git(repo, "checkout", "-b", "agent/issue-1")
    (repo / "kept.txt").write_text("one\nchanged\nthree\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "work")


async def test_committed_and_uncommitted_work_both_appear(tmp_path):
    repo = _repo(tmp_path)
    _branch_with_commit(repo)
    (repo / "new.txt").write_text("added file\n")
    _git(repo, "add", "new.txt")

    diff = await WorktreeDiffReader().read(repo)

    assert diff.unavailable is None
    assert diff.branch == "agent/issue-1"
    assert diff.base_source == "default-branch"
    assert diff.base_label == "master"
    paths = {item.path: item for item in diff.files}
    assert set(paths) == {"kept.txt", "new.txt"}
    assert paths["kept.txt"].status == "modified"
    assert paths["kept.txt"].added == 1
    assert paths["kept.txt"].removed == 1
    assert paths["new.txt"].status == "added"
    assert any(line.kind == "add" and "changed" in line.text for line in paths["kept.txt"].lines)


async def test_pull_request_base_wins_over_the_default_branch(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-b", "release")
    (repo / "release.txt").write_text("released\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "release only")
    _branch_with_commit(repo)  # branches off release

    from_default = await WorktreeDiffReader().read(repo)
    from_pull = await WorktreeDiffReader().read(repo, pull_request_base="release")

    assert from_default.base_label == "master"
    assert from_default.base_source == "default-branch"
    # Against master the branch appears to add release.txt, because it inherited
    # that commit. Against its real base it shows only the work it did — which is
    # what the pull request shows, and why the registered base wins.
    assert {item.path for item in from_default.files} == {"kept.txt", "release.txt"}
    assert from_pull.base_label == "release"
    assert from_pull.base_source == "pull-request"
    assert {item.path for item in from_pull.files} == {"kept.txt"}


async def test_a_pull_request_in_another_repository_does_not_frame_this_one(tmp_path):
    """A chat that registered a pull request in repository A and also worked in a
    checkout of repository B must not have B's diff labelled with A's base branch:
    the same ``origin/master`` exists in both and means different things (#1120)."""
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:owner/docker.git")
    _git(repo, "checkout", "-b", "release")
    (repo / "release.txt").write_text("released\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "release only")
    _branch_with_commit(repo)

    reader = WorktreeDiffReader()
    mine = await reader.read(repo, pull_request_base="release", pull_request_repo="owner/docker")
    someone_elses = await reader.read(
        repo, pull_request_base="release", pull_request_repo="owner/abacus"
    )

    assert mine.repository == "owner/docker"
    assert mine.base_source == "pull-request"
    assert mine.base_label == "release"
    # The other repository's pull request is simply not this checkout's frame.
    assert someone_elses.base_source == "default-branch"
    assert someone_elses.base_label == "master"


async def test_every_checkout_a_chat_worked_in_is_read(tmp_path):
    first = _repo(tmp_path, "first")
    second = _repo(tmp_path, "second")
    _branch_with_commit(second)

    diffs = await WorktreeDiffReader().read_many([first, second, tmp_path / "gone"])

    assert [item.path for item in diffs] == [str(first), str(second), str(tmp_path / "gone")]
    assert diffs[0].is_empty
    assert {item.path for item in diffs[1].files} == {"kept.txt"}
    assert diffs[2].unavailable and "no longer exists" in diffs[2].unavailable


def _clone_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """An ``upstream`` repository and a checkout of it that has an ``origin``."""
    upstream = _repo(tmp_path, "upstream")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(upstream), str(clone)], check=True, capture_output=True
    )
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    # A clone names its remote by URL; the module reads owner/name from it.
    _git(clone, "remote", "set-url", "origin", f"file://{upstream}")
    return upstream, clone


async def test_the_base_branch_is_refreshed_so_other_peoples_merges_are_not_this_chats(tmp_path):
    """Nobody fetches in a finished worker's worktree. Without a refresh the diff
    charges this chat with everything merged upstream since it last fetched (#1125)."""
    upstream, clone = _clone_with_origin(tmp_path)
    _git(clone, "checkout", "-b", "agent/issue-1")
    (clone / "mine.txt").write_text("my work\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "my work")
    # The work is merged upstream, and nothing ever fetches in this worktree.
    _git(upstream, "fetch", "--quiet", str(clone), "agent/issue-1")
    _git(upstream, "merge", "--quiet", "--ff-only", "FETCH_HEAD")

    # Without a refresh the day-old ref still calls the merged work pending.
    without_refresh = WorktreeDiffReader()

    async def never_refresh(*_args, **_kwargs):
        return (False, False)

    without_refresh._refresh_base = never_refresh
    stale = await without_refresh.read(clone)
    assert {item.path for item in stale.files} == {"mine.txt"}

    diff = await WorktreeDiffReader().read(clone, await_refresh=True)

    assert diff.files == ()
    assert diff.stale_base is False
    assert diff.refreshing is False


async def test_nobody_waits_for_the_fetch_to_see_a_diff(tmp_path):
    """The fetch is the only slow thing here — every other command is a local read
    of a few milliseconds — so the first read answers from the refs on disk and
    says a refresh is running. Asking again waits for it and gets the fresh
    comparison (#1133)."""
    upstream, clone = _clone_with_origin(tmp_path)
    _git(clone, "checkout", "-b", "agent/issue-1")
    (clone / "mine.txt").write_text("my work\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "my work")
    _git(upstream, "fetch", "--quiet", str(clone), "agent/issue-1")
    _git(upstream, "merge", "--quiet", "--ff-only", "FETCH_HEAD")

    reader = WorktreeDiffReader()
    started = asyncio.Event()
    release = asyncio.Event()
    original = reader._run

    async def held(cwd, *args, **kwargs):
        if "fetch" in args:
            started.set()
            await release.wait()
        return await original(cwd, *args, **kwargs)

    reader._run = held

    # The pane opens against the ref already on disk while the fetch is held.
    first = await reader.read(clone)
    await started.wait()
    assert first.refreshing is True
    assert {item.path for item in first.files} == {"mine.txt"}

    release.set()
    settled = await reader.read(clone, await_refresh=True)

    assert settled.refreshing is False
    assert settled.files == ()


async def test_a_reader_who_gives_up_does_not_cancel_everyone_elses_fetch(tmp_path):
    """A closed pane or a dropped request must not take the shared fetch with it."""
    _, clone = _clone_with_origin(tmp_path)
    reader = WorktreeDiffReader()
    release = asyncio.Event()
    original = reader._run

    async def held(cwd, *args, **kwargs):
        if "fetch" in args:
            await release.wait()
        return await original(cwd, *args, **kwargs)

    reader._run = held
    waiting = asyncio.ensure_future(reader.read(clone, await_refresh=True))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    release.set()
    diff = await reader.read(clone, await_refresh=True)

    assert diff.stale_base is False
    assert diff.unavailable is None


async def test_one_refresh_per_repository_serves_every_pane_for_a_while(tmp_path):
    """A fetch is network in someone's repository: opening the pane again, or a
    second worktree of the same repository, must not start another one."""
    upstream, clone = _clone_with_origin(tmp_path)
    reader = WorktreeDiffReader()
    fetched = []
    original = reader._run

    async def counting(cwd, *args, **kwargs):
        if "fetch" in args:
            fetched.append(cwd)
        return await original(cwd, *args, **kwargs)

    reader._run = counting
    await reader.read(clone, await_refresh=True)
    await reader.read(clone, await_refresh=True)

    assert len(fetched) == 1
    assert str(upstream) not in fetched


async def test_a_refresh_that_fails_falls_back_to_the_ref_on_disk_and_says_so(tmp_path):
    _, clone = _clone_with_origin(tmp_path)
    _git(clone, "remote", "set-url", "origin", "file:///nowhere/at/all.git")
    _branch_with_commit(clone)

    diff = await WorktreeDiffReader().read(clone, await_refresh=True)

    # The diff is still the useful one — a stale base beats an error — and the
    # reader is told why it might overstate the change.
    assert diff.unavailable is None
    assert {item.path for item in diff.files} == {"kept.txt"}
    assert diff.stale_base is True


async def test_a_fetch_that_cannot_work_is_not_retried_on_every_open(tmp_path):
    """A host whose credentials are refused fails the same fetch every time. Before
    #1133 a failure released the throttle outright, so every single open started
    another doomed network call and none of them learned from the last."""
    _, clone = _clone_with_origin(tmp_path)
    _git(clone, "remote", "set-url", "origin", "file:///nowhere/at/all.git")
    _branch_with_commit(clone)

    reader = WorktreeDiffReader()
    fetched = []
    original = reader._run

    async def counting(cwd, *args, **kwargs):
        if "fetch" in args:
            fetched.append(cwd)
        return await original(cwd, *args, **kwargs)

    reader._run = counting
    first = await reader.read(clone, await_refresh=True)
    second = await reader.read(clone, await_refresh=True)

    assert len(fetched) == 1
    # The second open is still told the truth about the base it was given.
    assert first.stale_base is True
    assert second.stale_base is True


async def test_a_checkout_with_no_remote_is_not_called_stale(tmp_path):
    repo = _repo(tmp_path)
    _branch_with_commit(repo)

    diff = await WorktreeDiffReader().read(repo)

    assert diff.stale_base is False


async def test_a_branch_with_no_base_falls_back_to_its_last_commit(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=work")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("first\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "first")
    (repo / "a.txt").write_text("second\n")

    diff = await WorktreeDiffReader().read(repo)

    assert diff.base_source == "head"
    assert diff.base_label is None
    assert [item.path for item in diff.files] == ["a.txt"]


async def test_untracked_files_are_named_but_not_read(tmp_path):
    repo = _repo(tmp_path)
    (repo / "scratch.log").write_text("noisy\n" * 500)

    diff = await WorktreeDiffReader().read(repo)

    assert diff.untracked == ("scratch.log",)
    assert diff.files == ()
    assert not diff.is_empty


async def test_a_clean_checkout_reports_nothing_changed(tmp_path):
    diff = await WorktreeDiffReader().read(_repo(tmp_path))

    assert diff.is_empty
    assert diff.unavailable is None


@pytest.mark.parametrize(
    "make, expected",
    [
        (lambda tmp_path: None, "no recorded directory"),
        (lambda tmp_path: tmp_path / "gone", "no longer exists"),
    ],
)
async def test_a_missing_directory_explains_itself(tmp_path, make, expected):
    diff = await WorktreeDiffReader().read(make(tmp_path))

    assert diff.unavailable is not None
    assert expected in diff.unavailable
    assert diff.files == ()


async def test_a_directory_that_is_not_a_checkout_explains_itself(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    diff = await WorktreeDiffReader().read(plain)

    assert diff.unavailable is not None
    assert "not a git checkout" in diff.unavailable


async def test_git_missing_from_the_host_is_reported_not_raised(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr("agentdeck.worktree_diff.shutil.which", lambda _name: None)

    diff = await WorktreeDiffReader().read(repo)

    assert diff.unavailable == "git is not installed on this host."


def test_a_long_file_is_cut_short_and_says_so():
    body = "".join(f"+line {index}\n" for index in range(MAX_LINES_PER_FILE + 50))
    patch = (
        "diff --git a/big.txt b/big.txt\n"
        "--- a/big.txt\n"
        "+++ b/big.txt\n"
        f"@@ -0,0 +1,{MAX_LINES_PER_FILE + 50} @@\n" + body
    )

    files, notes = parse_patch(patch)

    assert len(files[0].lines) == MAX_LINES_PER_FILE
    assert files[0].truncated
    # Counts describe the whole file, not the trimmed view.
    assert files[0].added == MAX_LINES_PER_FILE + 50
    assert any("cut short" in note for note in notes)


def test_too_many_files_are_counted_rather_than_dropped_silently():
    patch = "".join(
        f"diff --git a/f{index}.txt b/f{index}.txt\n"
        f"--- a/f{index}.txt\n+++ b/f{index}.txt\n@@ -1 +1 @@\n-old\n+new\n"
        for index in range(MAX_FILES + 3)
    )

    files, notes = parse_patch(patch)

    assert len(files) == MAX_FILES
    assert any("3 more changed files are not shown." == note for note in notes)


def test_renames_and_deletions_keep_their_real_paths():
    patch = (
        "diff --git a/old/name.py b/new/name.py\n"
        "similarity index 90%\n"
        "rename from old/name.py\n"
        "rename to new/name.py\n"
        "--- a/old/name.py\n"
        "+++ b/new/name.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/dropped.py b/dropped.py\n"
        "deleted file mode 100644\n"
        "--- a/dropped.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-gone\n"
    )

    files, _notes = parse_patch(patch)

    assert files[0].path == "new/name.py"
    assert files[0].old_path == "old/name.py"
    assert files[0].status == "renamed"
    assert files[1].path == "dropped.py"
    assert files[1].status == "deleted"
    assert files[1].removed == 1


def test_a_binary_file_is_named_without_a_body():
    patch = (
        "diff --git a/logo.png b/logo.png\n"
        "index 0000000..1111111 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )

    files, _notes = parse_patch(patch)

    assert files[0].binary
    assert files[0].lines == ()
    assert not files[0].has_body


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/agentdeck/worktree_diff.py", "source"),
        ("src/agentdeck/web/static/app.css", "source"),
        ("tests/test_worktree_diff.py", "tests"),
        ("src/agentdeck/web/__tests__/session.test.js", "tests"),
        ("conftest.py", "tests"),
        # A whole file called `tests.py` has no prefix or suffix to catch (#1688).
        ("app/tests.py", "tests"),
        ("app/test.py", "tests"),
        ("app/static/tests.js", "tests"),
        # `testing.py` is not a test file, and must not be folded away as one.
        ("src/agentdeck/testing.py", "source"),
        # A fixture image belongs with the tests it feeds, not with the screenshots.
        ("tests/fixtures/expected.png", "tests"),
        ("docs/agents/domain.md", "docs"),
        # A text file is as often data as prose, so it is not guessed at.
        ("greeting.txt", "source"),
        ("AGENTS.md", "docs"),
        (".claude/codebase/poller-engine.md", "docs"),
        # A Cursor rule file is prose too (#1688).
        (".cursor/rules/style.mdc", "docs"),
        ("docs/screenshots/issue-1684-diff.png", "assets"),
        ("uv.lock", "assets"),
        ("src/agentdeck/web/static/vendor.min.js", "assets"),
        # Nothing recognised it, so it stays where a reviewer will see it.
        ("Makefile", "source"),
    ],
)
def test_a_changed_path_is_named_as_source_tests_docs_or_an_asset(path, expected):
    assert classify_path(path) == expected


def test_a_binary_with_an_unlisted_name_is_still_non_code():
    patch = (
        "diff --git a/blob b/blob\n"
        "index 0000000..1111111 100644\n"
        "Binary files a/blob and b/blob differ\n"
    )

    files, _notes = parse_patch(patch)

    assert files[0].kind == "assets"


def _one_file_patch(path: str, lines: int) -> str:
    body = "".join(f"+line {index}\n" for index in range(lines))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{lines} @@\n" + body
    )


def test_tests_and_screenshots_are_grouped_away_from_the_source():
    patch = (
        _one_file_patch("tests/test_thing.py", 3)
        + _one_file_patch("docs/agents/domain.md", 2)
        + _one_file_patch("src/agentdeck/thing.py", 4)
    )

    files, _notes = parse_patch(patch)
    diff = WorktreeDiff(files=files)

    assert [item.path for item in diff.source_files] == ["src/agentdeck/thing.py"]
    assert [(group.kind, [item.path for item in group.files]) for group in diff.groups] == [
        ("tests", ["tests/test_thing.py"]),
        ("docs", ["docs/agents/domain.md"]),
    ]
    # The whole-diff chip still counts every changed file.
    assert len(diff.files) == 3


def test_a_group_with_no_files_is_not_shown_at_all():
    files, _notes = parse_patch(_one_file_patch("src/agentdeck/thing.py", 2))

    assert WorktreeDiff(files=files).groups == ()


def test_the_line_budget_is_spent_on_source_before_tests():
    # The tests come first in the diff and would alone exhaust the budget.
    patch = _one_file_patch("tests/test_huge.py", MAX_TOTAL_LINES + 100) + _one_file_patch(
        "src/agentdeck/thing.py", 5
    )

    files, _notes = parse_patch(patch)

    # Display order is still the diff's own order…
    assert [item.path for item in files] == ["tests/test_huge.py", "src/agentdeck/thing.py"]
    # …but the source file kept its whole body — the hunk header and five adds —
    # rather than being starved.
    assert len(files[1].lines) == 6
    assert not files[1].truncated


def test_source_files_survive_a_change_that_is_mostly_screenshots():
    patch = "".join(
        _one_file_patch(f"docs/screenshots/shot-{index}.txt", 1) for index in range(MAX_FILES + 5)
    ) + _one_file_patch("src/agentdeck/thing.py", 2)

    files, notes = parse_patch(patch)

    assert "src/agentdeck/thing.py" in [item.path for item in files]
    assert any("more changed file" in note for note in notes)


async def test_a_pull_requests_branch_is_found_in_the_worktree_git_knows(tmp_path):
    """A chat started in the main checkout often makes its own worktree for its branch.
    The deck never hears of it, and the main checkout reads as unchanged (#2993)."""
    from types import SimpleNamespace

    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:Shop/Store.git")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "hotfix/3548-timeout", str(worktree))
    pull = SimpleNamespace(repository="shop/store", head_branch="hotfix/3548-timeout")
    other_repo = SimpleNamespace(repository="shop/other", head_branch="hotfix/3548-timeout")
    unknown = SimpleNamespace(repository="shop/store", head_branch="nobody-has-it")
    reader = WorktreeDiffReader()

    found = await reader.pull_request_checkouts([repo], [pull, other_repo, unknown])

    assert found == ((str(worktree.resolve()), pull),)
    # A worktree already being read is not read twice.
    assert await reader.pull_request_checkouts([repo, worktree], [pull]) == ()
    assert await reader.pull_request_checkouts([None, tmp_path / "gone"], [pull]) == ()
