"""The change a chat has made in its own checkout, framed the way its PR frames it.

A session's worktree is the only place its work exists before review, and often
the only place it exists at all — a branch nobody has pushed yet still shows up
here. This module answers one question about that directory: *what would this
chat's pull request contain?*

The answer is the working tree compared against the merge base with its base
branch. That deliberately spans two things a plain ``git diff`` keeps apart —
what the chat has committed on its branch, and what it has edited but not
committed yet. A reader looking at a running chat wants both: work in progress
is exactly what they are checking on. Untracked files are named but not read,
because an ignored-by-accident build directory can be enormous and reading it
would cost far more than it tells anyone.

The base branch comes from the pull request the session registered when there is
one, so the diff matches what a reviewer sees on GitHub — but only for a checkout
of *that pull request's own repository*. A chat often works in more than one
repository, and reading a second repository's ``origin/master`` while claiming a
first repository's pull request framed it is simply a false statement (#1120).
With no matching pull request the base comes from the checkout's own
``origin/HEAD``. The base branch is refreshed on a deliberate open, throttled to
one fetch per repository per five minutes, because nobody fetches in a finished
worker's worktree and a day-old base draws other people's merges as this chat's
work (#1125). That fetch is the only write, and it moves one remote-tracking ref:
nothing here reads GitHub, touches the index, or changes a working tree, and a
failed read produces an explanation rather than an exception.

Nobody waits for that fetch. Every other command here is a local read costing a
few milliseconds, while the fetch is a network round trip — on a host whose
credentials cannot reach the remote it is a slow *failure*, paid on every open.
So a refresh runs in the background and the diff is rendered straight away from
the refs already on disk; the caller is told a refresh is in flight and can ask
again with ``await_refresh=True`` to get the comparison the fetch produced. The
reader still ends up measured against a freshly fetched base — it simply is not
what they wait for (#1133).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from . import gh_cli

log = logging.getLogger(__name__)

# A diff pane is a glance, not an archive. These caps keep one runaway checkout
# from turning a fragment request into a multi-megabyte page; whatever they trim
# is reported in the fragment rather than silently dropped.
MAX_FILES = 60
MAX_LINES_PER_FILE = 400
MAX_TOTAL_LINES = 4000
MAX_UNTRACKED = 40
MAX_PATCH_BYTES = 4_000_000
COMMAND_TIMEOUT_S = 8.0
# A fetch talks to the network, so it gets its own budget and its own throttle.
# One refresh per repository per interval is enough: the base branch is what
# other people merge into, and it does not move second by second (#1125).
FETCH_TIMEOUT_S = 20.0
FETCH_INTERVAL_S = 300.0
# A refresh that failed gets a shorter leash than a refresh that worked, but it
# does get one. Releasing the throttle entirely means a host whose credentials
# cannot fetch — a 403 on every attempt — starts a doomed network call on every
# single open, forever, and never learns anything from the last one (#1133).
FETCH_RETRY_INTERVAL_S = 60.0
_HTTPS_INSTEAD_OF = (
    "-c",
    "url.https://github.com/.insteadOf=git@github.com:",
    "-c",
    "url.https://github.com/.insteadOf=ssh://git@github.com/",
)

_STATUS_LABELS = {
    "added": "added",
    "deleted": "deleted",
    "renamed": "renamed",
    "modified": "modified",
    "untracked": "untracked",
}

# What a reader came to the pane for is the source change. Its tests, its docs
# and the screenshots committed beside it are context, and listing all of them
# in one flat run buries the two files worth reading between forty that are not
# — so each changed file is named as source, tests, docs or a non-code asset and
# the last three are grouped away below the source (#1684).
KIND_SOURCE = "source"
KIND_TESTS = "tests"
KIND_DOCS = "docs"
KIND_ASSETS = "assets"

# Every kind but source; the order here is the order the groups are shown in.
KIND_LABELS = (
    (KIND_TESTS, "Tests"),
    (KIND_DOCS, "Docs"),
    (KIND_ASSETS, "Screenshots and other non-code files"),
)

_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec"})
# A whole file called `tests.py` — Django's own convention — carries no prefix or
# suffix for the rules below to catch, so it is named outright (#1688).
_TEST_NAMES = frozenset({"tests.py", "test.py", "tests.js", "tests.ts", "test.js", "test.ts"})
_TEST_PREFIXES = ("test_", "conftest.")
_TEST_SUFFIXES = (
    "_test.py",
    ".test.js",
    ".test.ts",
    ".test.jsx",
    ".test.tsx",
    ".spec.js",
    ".spec.ts",
)
_DOC_DIRS = frozenset({"docs", "doc"})
# No `.txt`: a text file is as often data or a fixture as it is prose, and
# guessing wrong folds a file the reader came for out of sight.
# `.mdc` is a Cursor rule file: Markdown with a small header, and prose either
# way, so it belongs beside `.md` rather than in the source list (#1688).
_DOC_SUFFIXES = (".md", ".mdc", ".rst", ".adoc")
_ASSET_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".pdf",
    ".mp4",
    ".webm",
    ".woff",
    ".woff2",
    ".ttf",
    ".zip",
    ".gz",
)
# Machine-written files. Nobody reads a lockfile's diff on a phone.
_GENERATED_NAMES = frozenset(
    {"package-lock.json", "poetry.lock", "uv.lock", "yarn.lock", "pnpm-lock.yaml", "cargo.lock"}
)
_GENERATED_SUFFIXES = (".lock", ".min.js", ".min.css", ".map", ".snap")


def classify_path(path: str) -> str:
    """Name a changed path as source, tests, docs or a non-code asset.

    First rule that matches wins, and tests are asked first on purpose: a
    fixture image under ``tests/`` belongs with the tests it feeds, not with the
    screenshots. Anything left over is source, so a file this does not recognise
    stays where a reviewer will see it rather than being tucked away.
    """
    lowered = path.strip("/").lower()
    parts = lowered.split("/")
    name = parts[-1]
    directories = frozenset(parts[:-1])

    if directories & _TEST_DIRS:
        return KIND_TESTS
    if name in _TEST_NAMES or name.startswith(_TEST_PREFIXES) or name.endswith(_TEST_SUFFIXES):
        return KIND_TESTS
    if name.endswith(_ASSET_SUFFIXES):
        return KIND_ASSETS
    if directories & _DOC_DIRS or name.endswith(_DOC_SUFFIXES):
        return KIND_DOCS
    if name in _GENERATED_NAMES or name.endswith(_GENERATED_SUFFIXES):
        return KIND_ASSETS
    return KIND_SOURCE


@dataclass(frozen=True)
class DiffLine:
    """One rendered line. ``kind`` is the CSS/semantic class, not the raw prefix."""

    kind: str  # "hunk" | "add" | "del" | "ctx" | "meta"
    text: str


@dataclass(frozen=True)
class DiffFile:
    path: str
    status: str
    added: int
    removed: int
    lines: tuple[DiffLine, ...] = ()
    old_path: str | None = None
    binary: bool = False
    truncated: bool = False

    @property
    def status_label(self) -> str:
        return _STATUS_LABELS.get(self.status, self.status)

    @property
    def has_body(self) -> bool:
        return bool(self.lines)

    @property
    def kind(self) -> str:
        kind = classify_path(self.path)
        # A binary blob has no text to read however it is named, so it is
        # non-code even when its extension is not one we listed.
        if kind == KIND_SOURCE and self.binary:
            return KIND_ASSETS
        return kind


@dataclass(frozen=True)
class DiffGroup:
    """The non-source files of one kind, shown together and collapsed by default."""

    kind: str
    label: str
    files: tuple[DiffFile, ...]

    @property
    def added(self) -> int:
        return sum(item.added for item in self.files)

    @property
    def removed(self) -> int:
        return sum(item.removed for item in self.files)


@dataclass(frozen=True)
class WorktreeDiff:
    """A chat's pending change, or an explanation of why there isn't one to show.

    ``unavailable`` and ``files`` are mutually exclusive in practice: a diff that
    could not be read carries the reason, and an empty ``files`` with no reason
    means the checkout genuinely matches its base.
    """

    files: tuple[DiffFile, ...] = ()
    untracked: tuple[str, ...] = ()
    branch: str | None = None
    base_label: str | None = None
    base_source: str = "unknown"  # "pull-request" | "default-branch" | "head"
    unavailable: str | None = None
    notes: tuple[str, ...] = ()
    # Which checkout this is. A chat can have several, so a reader who sees two
    # sections needs to be told which repository each one is.
    path: str | None = None
    repository: str | None = None
    # The base branch could not be refreshed, so it is whatever this checkout
    # last fetched — possibly behind, which overstates the change (#1125).
    stale_base: bool = False
    # A refresh of the base branch is running right now and this diff was read
    # without waiting for it. Whoever shows this can ask again for the settled
    # answer; nothing here is wrong, it is just one fetch old (#1133).
    refreshing: bool = False

    @property
    def added(self) -> int:
        return sum(item.added for item in self.files)

    @property
    def removed(self) -> int:
        return sum(item.removed for item in self.files)

    @property
    def is_empty(self) -> bool:
        return not self.files and not self.untracked

    @property
    def source_added(self) -> int:
        return sum(item.added for item in self.source_files)

    @property
    def source_removed(self) -> int:
        return sum(item.removed for item in self.source_files)

    @property
    def source_files(self) -> tuple[DiffFile, ...]:
        """The files shown first and expanded — everything that is not grouped away."""
        return tuple(item for item in self.files if item.kind == KIND_SOURCE)

    @property
    def groups(self) -> tuple[DiffGroup, ...]:
        """The non-source files, in kind order, skipping the kinds nothing landed in."""
        groups = []
        for kind, label in KIND_LABELS:
            files = tuple(item for item in self.files if item.kind == kind)
            if files:
                groups.append(DiffGroup(kind=kind, label=label, files=files))
        return tuple(groups)

    @property
    def base_description(self) -> str:
        """How the base was chosen, in words a reader can check against reality."""
        if self.base_source == "pull-request":
            return "the pull request's base branch"
        if self.base_source == "default-branch":
            return "this checkout's default branch"
        if self.base_source == "head":
            return "the last commit — no base branch was found"
        return "an unknown base"


@dataclass
class _BaseRefresh:
    """What is known about one repository's base-branch refresh.

    One of these is shared by every worktree of a repository, because they share
    the refs a fetch moves. It is deliberately mutable: the background fetch
    writes its outcome here, and the next opened pane reads it.
    """

    at: float
    # The last completed attempt failed, so the ref on disk may be behind. Kept
    # between attempts so a throttled open still tells the reader the truth.
    stale: bool = False
    task: asyncio.Task[bool] | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def due(self) -> bool:
        interval = FETCH_RETRY_INTERVAL_S if self.stale else FETCH_INTERVAL_S
        return monotonic() - self.at >= interval


class WorktreeDiffReader:
    """Runs the read-only git commands behind :class:`WorktreeDiff`.

    Kept a class so the ``git`` lookup happens once per process rather than once
    per opened pane; every read names its own directory.
    """

    def __init__(self) -> None:
        self._git = shutil.which("git")
        # Each repository's base-branch refresh, by ``owner/name``. Shared
        # across worktrees of one repository, because they share the refs.
        self._refreshes: dict[str, _BaseRefresh] = {}

    async def _run(
        self, cwd: str, *args: str, timeout: float = COMMAND_TIMEOUT_S
    ) -> tuple[int, str]:
        if self._git is None:
            return (1, "")
        argv = [self._git, "-C", cwd, "-c", "core.quotepath=false", *args]
        try:
            code, stdout = await gh_cli.run(argv, timeout=timeout)
        except (OSError, TimeoutError) as exc:
            log.debug("worktree diff command failed: %s", exc)
            return (1, "")
        return (code, stdout.decode("utf-8", "replace"))

    async def read_many(
        self,
        checkouts: Sequence[Path | str],
        *,
        pull_request_base: str | None = None,
        pull_request_repo: str | None = None,
        await_refresh: bool = False,
    ) -> tuple[WorktreeDiff, ...]:
        """Every checkout this chat worked in, in the order it was given them.

        Read one after another rather than at once: these are git processes in
        someone's repository, and a chat with four worktrees should not start
        four of them because a pane opened.
        """
        results = []
        for checkout in checkouts:
            results.append(
                await self.read(
                    checkout,
                    pull_request_base=pull_request_base,
                    pull_request_repo=pull_request_repo,
                    await_refresh=await_refresh,
                )
            )
        return tuple(results)

    async def pull_request_checkouts(
        self,
        checkouts: Sequence[Path | str | None],
        pulls: Sequence[Any],
        *,
        clones: Sequence[Path | str] = (),
    ) -> tuple[tuple[str, Any], ...]:
        """The worktree each pull request's head branch is checked out in, when the
        deck has not recorded it.

        A chat started in a repository's main checkout often makes a worktree for
        its branch and does all its work there; nothing tells the deck about that
        directory, and the start folder — sitting on some old, merged branch —
        reads as "nothing has changed" (#2993). Git itself knows every worktree of
        a repository, so each pull request's head branch is looked up there,
        starting from a known checkout of the same repository. ``clones`` are more
        checkouts to start from that are not themselves being read: a local clone of a
        pull request's repository the Work has no checkout of, as when the issue is on
        one repository and the code in another (#3129). Returns ``(path, pull)`` for each
        one found that is not already in ``checkouts``.
        """
        known: dict[str, str | None] = {}
        for checkout in checkouts:
            if checkout is not None and Path(checkout).is_dir():
                directory = str(Path(checkout).resolve())
                known.setdefault(directory, await self._repository(directory))
        search = dict(known)
        for clone in clones:
            if Path(clone).is_dir():
                directory = str(Path(clone).resolve())
                search.setdefault(directory, await self._repository(directory))
        found: dict[str, Any] = {}
        for pull in pulls:
            branch = getattr(pull, "head_branch", None)
            repository = getattr(pull, "repository", None)
            if not branch or not repository:
                continue
            for directory, checkout_repo in search.items():
                if not checkout_repo or not _same_repository(checkout_repo, repository):
                    continue
                path = await self._worktree_of_branch(directory, branch)
                if path is not None:
                    if path not in known:
                        found.setdefault(path, pull)
                    break
        return tuple(found.items())

    async def _worktree_of_branch(self, directory: str, branch: str) -> str | None:
        code, text = await self._run(directory, "worktree", "list", "--porcelain")
        if code != 0:
            return None
        wanted = f"branch refs/heads/{branch}"
        path: str | None = None
        for line in text.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree ") :]
            elif line == wanted and path is not None and Path(path).is_dir():
                return str(Path(path).resolve())
        return None

    async def read(
        self,
        cwd: Path | str | None,
        *,
        pull_request_base: str | None = None,
        pull_request_repo: str | None = None,
        await_refresh: bool = False,
    ) -> WorktreeDiff:
        if cwd is None:
            return WorktreeDiff(unavailable="This chat has no recorded directory.")
        path = Path(cwd)
        if not path.is_dir():
            return WorktreeDiff(
                path=str(path), unavailable=f"{path} no longer exists on this host."
            )
        if self._git is None:
            return WorktreeDiff(path=str(path), unavailable="git is not installed on this host.")
        directory = str(path)

        code, _ = await self._run(directory, "rev-parse", "--is-inside-work-tree")
        if code != 0:
            return WorktreeDiff(path=directory, unavailable=f"{path} is not a git checkout.")

        repository = await self._repository(directory)
        # A pull request frames only its own repository's checkout. Where the
        # chat registered a pull request in some *other* repository, this one
        # falls back to its own default branch and says so (#1120).
        if pull_request_repo and repository and not _same_repository(repository, pull_request_repo):
            pull_request_base = None
        stale, refreshing = await self._refresh_base(
            directory, repository, pull_request_base, await_refresh=await_refresh
        )
        branch = await self._branch(directory)
        base_ref, base_source = await self._base_ref(directory, pull_request_base)
        identity = {
            "path": directory,
            "repository": repository,
            "stale_base": stale,
            "refreshing": refreshing,
        }
        if base_ref is None:
            return await self._diff_against(
                directory,
                "HEAD",
                branch=branch,
                base_label=None,
                base_source="head",
                **identity,
            )
        merge_base = await self._merge_base(directory, base_ref)
        return await self._diff_against(
            directory,
            merge_base or base_ref,
            **identity,
            branch=branch,
            base_label=base_ref,
            base_source=base_source,
        )

    async def _branch(self, directory: str) -> str | None:
        code, text = await self._run(directory, "rev-parse", "--abbrev-ref", "HEAD")
        name = text.strip()
        if code != 0 or not name or name == "HEAD":
            return None
        return name

    async def _base_ref(
        self, directory: str, pull_request_base: str | None
    ) -> tuple[str | None, str]:
        """The ref this branch is measured against, and where that choice came from."""
        if pull_request_base:
            for candidate in (f"origin/{pull_request_base}", pull_request_base):
                if await self._exists(directory, candidate):
                    return (candidate, "pull-request")
        code, text = await self._run(
            directory, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"
        )
        head = text.strip()
        if code == 0 and head.startswith("refs/remotes/"):
            candidate = head[len("refs/remotes/") :]
            if await self._exists(directory, candidate):
                return (candidate, "default-branch")
        for candidate in ("origin/main", "origin/master", "main", "master"):
            if await self._exists(directory, candidate):
                return (candidate, "default-branch")
        return (None, "head")

    async def _refresh_base(
        self,
        directory: str,
        repository: str | None,
        pull_request_base: str | None,
        *,
        await_refresh: bool,
    ) -> tuple[bool, bool]:
        """Bring this checkout's copy of the base branch up to date.

        Nobody fetches in a finished worker's worktree, so its ``origin/master``
        can be a day old — and then every commit merged upstream since is drawn
        as this chat's own work (#1125). This is the one write the module makes,
        and it moves exactly one remote-tracking ref: no checkout, no index, no
        working tree, and no effect on whatever an agent is doing in there.

        The fetch runs as its own task, and by default this does not wait for it:
        every other command in a read is a local few-millisecond thing, so
        waiting for the network is the whole cost of opening a pane (#1133). Pass
        ``await_refresh`` to get the settled answer instead.

        Returns ``(stale, refreshing)`` — whether the last completed attempt
        failed, so the reader can be told the comparison is against the copy
        already on disk, and whether one is running right now. A checkout with no
        ``origin`` is neither: there is nothing to be behind.
        """
        if repository is None:
            return (False, False)
        wanted = pull_request_base or await self._default_branch(directory)
        if not wanted:
            return (False, False)
        entry = self._refreshes.get(repository)
        if entry is None or (not entry.running and entry.due):
            # Claimed before the await, so two panes opening together fetch once.
            entry = _BaseRefresh(at=monotonic(), stale=entry.stale if entry else False)
            self._refreshes[repository] = entry
            entry.task = asyncio.ensure_future(self._fetch_base(directory, wanted, entry))
        if entry.running and not await_refresh:
            return (entry.stale, True)
        if entry.running:
            # Shielded so a reader who gives up — a closed pane, a dropped
            # request — does not cancel the fetch every other reader is on.
            await asyncio.shield(entry.task)
        return (entry.stale, False)

    async def _fetch_base(self, directory: str, wanted: str, entry: _BaseRefresh) -> bool:
        """The fetch itself, recording its outcome for whoever reads next.

        Nothing here may raise. This runs as a task nobody is required to await,
        and a reader who *does* await it is holding an open pane — neither should
        be handed an exception because a git process misbehaved.
        """
        code = 1
        try:
            code, _ = await self._run(
                directory,
                # The web process has no ssh agent — the host's keys live in a
                # locked 1Password agent — so an `git@github.com:` remote would
                # fail every time. These rewrites send the fetch over https
                # instead, where `gh`'s credential helper already authenticates
                # it. They apply to this one command and change nothing on disk.
                *_HTTPS_INSTEAD_OF,
                "fetch",
                "--quiet",
                "origin",
                f"+refs/heads/{wanted}:refs/remotes/origin/{wanted}",
                timeout=FETCH_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 - a refresh failing is a note, never an error
            log.debug("base branch refresh failed in %s", directory, exc_info=True)
        # Timed from when it finished, so a slow failure is not immediately
        # retried by the pane that opens a moment later.
        entry.at = monotonic()
        entry.stale = code != 0
        return entry.stale

    async def _default_branch(self, directory: str) -> str | None:
        """The branch name behind ``origin/HEAD``, without guessing."""
        code, text = await self._run(
            directory, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"
        )
        head = text.strip()
        prefix = "refs/remotes/origin/"
        if code != 0 or not head.startswith(prefix):
            return None
        return head[len(prefix) :] or None

    async def _repository(self, directory: str) -> str | None:
        """``owner/name`` from this checkout's ``origin``, or None if it has none."""
        code, text = await self._run(directory, "remote", "get-url", "origin")
        if code != 0:
            return None
        return _repository_from_url(text.strip())

    async def _exists(self, directory: str, ref: str) -> bool:
        code, _ = await self._run(
            directory, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"
        )
        return code == 0

    async def _merge_base(self, directory: str, base_ref: str) -> str | None:
        code, text = await self._run(directory, "merge-base", base_ref, "HEAD")
        revision = text.strip()
        return revision if code == 0 and revision else None

    async def _diff_against(
        self,
        directory: str,
        revision: str,
        *,
        branch: str | None,
        base_label: str | None,
        base_source: str,
        path: str | None = None,
        repository: str | None = None,
        stale_base: bool = False,
        refreshing: bool = False,
    ) -> WorktreeDiff:
        code, patch = await self._run(
            directory,
            "diff",
            "--no-color",
            "--find-renames",
            "--unified=3",
            revision,
            "--",
        )
        if code != 0:
            return WorktreeDiff(
                branch=branch,
                base_label=base_label,
                base_source=base_source,
                path=path,
                repository=repository,
                stale_base=stale_base,
                refreshing=refreshing,
                unavailable="git could not read this checkout's diff.",
            )
        notes: list[str] = []
        if len(patch) > MAX_PATCH_BYTES:
            patch = patch[:MAX_PATCH_BYTES]
            notes.append("The diff was too large to read in full and is cut short.")
        files, parse_notes = parse_patch(patch)
        notes.extend(parse_notes)
        untracked, untracked_note = await self._untracked(directory)
        if untracked_note:
            notes.append(untracked_note)
        return WorktreeDiff(
            files=files,
            untracked=untracked,
            branch=branch,
            base_label=base_label,
            base_source=base_source,
            path=path,
            repository=repository,
            stale_base=stale_base,
            refreshing=refreshing,
            notes=tuple(notes),
        )

    async def _untracked(self, directory: str) -> tuple[tuple[str, ...], str | None]:
        code, text = await self._run(directory, "ls-files", "--others", "--exclude-standard")
        if code != 0:
            return ((), None)
        names = [line for line in text.splitlines() if line]
        if len(names) > MAX_UNTRACKED:
            trimmed = len(names) - MAX_UNTRACKED
            return (
                tuple(names[:MAX_UNTRACKED]),
                f"{trimmed} more untracked file{'s' if trimmed != 1 else ''} are not listed.",
            )
        return (tuple(names), None)


def _repository_from_url(url: str) -> str | None:
    """``owner/name`` from any of the remote URL shapes git accepts."""
    if not url:
        return None
    text = url.removesuffix(".git")
    if "://" in text:
        text = text.split("://", 1)[1]
        text = text.split("/", 1)[1] if "/" in text else ""
    elif ":" in text:  # scp-style git@github.com:owner/name
        text = text.split(":", 1)[1]
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


def _same_repository(left: str, right: str) -> bool:
    return left.strip().lower() == right.strip().lower()


def _classify(line: str) -> str:
    if line.startswith("@@"):
        return "hunk"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "del"
    if line.startswith("\\"):
        return "meta"
    return "ctx"


def _header_path(header: str) -> str | None:
    """The path from a ``--- a/x``/``+++ b/x`` header, or None for /dev/null."""
    value = header.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if len(value) > 2 and value[1] == "/":
        return value[2:]
    return value


def parse_patch(patch: str) -> tuple[tuple[DiffFile, ...], list[str]]:
    """Split ``git diff`` output into per-file entries, applying the display caps.

    Parsing is deliberately forgiving: an unrecognised header line is kept as
    part of the file's body rather than aborting the whole diff, because git's
    output shape varies with the change (mode bits, renames, binary blobs) and a
    reader is better served by a slightly odd panel than by an error.
    """
    chunks = _split_files(patch)
    notes: list[str] = []
    # The caps are spent on source first. A change that ships forty screenshots
    # or a rewritten test suite used to exhaust the line budget before the two
    # files worth reading were reached, and they arrived cut short or missing
    # (#1684). Display order is still the diff's own order; only the order the
    # budget is handed out in changes.
    order = sorted(
        range(len(chunks)), key=lambda index: (_chunk_is_not_source(chunks[index]), index)
    )
    if len(order) > MAX_FILES:
        skipped = len(order) - MAX_FILES
        notes.append(f"{skipped} more changed file{'s' if skipped != 1 else ''} are not shown.")
        order = order[:MAX_FILES]

    parsed_by_index: dict[int, DiffFile] = {}
    budget = MAX_TOTAL_LINES
    for index in order:
        parsed = _parse_file(chunks[index], budget)
        if parsed is None:
            continue
        parsed_by_index[index] = parsed
        budget -= len(parsed.lines)
    files = [parsed_by_index[index] for index in sorted(parsed_by_index)]
    if any(item.truncated for item in files):
        notes.append("Long files are cut short — open the full diff to read them.")
    return (tuple(files), notes)


def _chunk_is_not_source(chunk: Sequence[str]) -> bool:
    """Sort key for the budget: ``False`` for source, so source sorts first.

    Read off the ``diff --git`` header alone, because this decides the order the
    chunks are parsed in and so cannot wait for them to be parsed. A header that
    does not yield a path counts as source, which spends the budget on it early
    rather than risking a real source file being cut.
    """
    old_name, new_name = _git_header_paths(chunk[0])
    path = new_name or old_name
    if path is None:
        return False
    return classify_path(path) != KIND_SOURCE


def _split_files(patch: str) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = [line]
            chunks.append(current)
        elif current is not None:
            current.append(line)
    return chunks


def _git_header_paths(line: str) -> tuple[str | None, str | None]:
    """``diff --git a/x b/y`` → (x, y). Quoted or space-bearing paths give None."""
    body = line[len("diff --git ") :]
    if body.startswith('"'):
        return (None, None)
    parts = body.split(" ")
    if len(parts) != 2:
        return (None, None)
    return (
        parts[0][2:] if parts[0].startswith("a/") else parts[0],
        parts[1][2:] if parts[1].startswith("b/") else parts[1],
    )


def _parse_file(chunk: Sequence[str], budget: int) -> DiffFile | None:
    old_name, new_name = _git_header_paths(chunk[0])
    path = new_name or old_name
    old_path: str | None = None
    status = "modified"
    binary = False
    body_start = len(chunk)

    for index, line in enumerate(chunk[1:], start=1):
        if line.startswith("@@"):
            body_start = index
            break
        if line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "deleted"
        elif line.startswith("rename from "):
            status = "renamed"
            old_path = line[len("rename from ") :]
        elif line.startswith("rename to "):
            path = line[len("rename to ") :] or path
        elif line.startswith("--- "):
            resolved = _header_path(line[4:])
            if resolved is not None:
                old_name = resolved
        elif line.startswith("+++ "):
            resolved = _header_path(line[4:])
            if resolved is not None:
                path = resolved
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            binary = True
            body_start = len(chunk)
            break

    if path is None:
        path = old_name or "(unknown path)"
    if status == "deleted" and old_name:
        path = old_name
    if status == "renamed" and old_path is None:
        old_path = old_name

    if binary:
        return DiffFile(
            path=path, status=status, added=0, removed=0, binary=True, old_path=old_path
        )

    body = list(chunk[body_start:])
    added = sum(1 for line in body if line.startswith("+"))
    removed = sum(1 for line in body if line.startswith("-"))
    allowed = max(0, min(MAX_LINES_PER_FILE, budget))
    truncated = len(body) > allowed
    lines = tuple(DiffLine(_classify(line), line) for line in body[:allowed])
    return DiffFile(
        path=path,
        status=status,
        added=added,
        removed=removed,
        lines=lines,
        old_path=old_path,
        truncated=truncated,
    )
