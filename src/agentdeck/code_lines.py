"""Changed lines of a diff with whole-line comments left out, counted in a local checkout.

GitHub's ``additions`` and ``deletions`` count every line, so a change that explains itself
in comments looks bigger, and grades worse, than its code (#4812). Telling a comment from
code needs the diff's text, and the owner asked for that without another GitHub call: so it
is read with ``git diff`` in a checkout of the repository on this host that already holds
both commits. Nothing here fetches. A checkout or commit that is missing, a git error or a
timeout is "unknown" (``None``), and every caller then keeps GitHub's own number.

A line is a comment only when it plainly is one on its own (``is_comment``). A line a diff
without context cannot place, like a block comment's middle ``* …`` (or C's ``*ptr = 1;``),
counts as code: a wrong guess may only overcount, never hide code from the size checks a
review depends on.

A count is kept for good: commits never change, so it stays true. An unknown is kept five
minutes, and only for the checkouts it was asked in, as a checkout or a fetch may bring the
commits in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

log = logging.getLogger(__name__)

_TIMEOUT_S = 6.0
_PARALLEL = 6
_CACHED = 500
# Checkouts remembered per repository, newest first: any one holding the commits will do,
# and a worktree shares its main checkout's objects.
_CHECKOUTS = 8
# How long an unknown answer stands before the commits are looked for again: long enough that
# a refresh pass does not run git for an uncountable head every time (a diff that times out
# waits 6 s), short enough that a fetch or a new checkout is picked up soon.
_UNKNOWN_S = 300.0

# YAML is left out: a block scalar (a workflow's ``run: |``) holds shell whose ``#`` lines a
# diff without context cannot tell from YAML's own comments.
_HASH = frozenset({".py", ".pyi", ".sh", ".bash", ".zsh", ".toml", ".rb", ".pl"})
_SLASH = frozenset(
    {
        *(".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".c", ".h", ".cc", ".cpp", ".hpp"),
        *(".go", ".rs", ".java", ".kt", ".kts", ".swift", ".cs", ".scala", ".dart"),
    }
)
# ``#`` lines that steer how the file runs or is read, so they are code.
_HASH_CODE = ("#!", "# -*-", "# coding:", "# coding=", "# vim: set fileencoding", "# frozen_string")
_BLOCK = _SLASH | {".css", ".scss", ".less"}
# Sass and Less have ``//`` line comments; plain CSS does not.
_SLASH_ONLY = frozenset({".scss", ".less"})
_DASH = frozenset({".sql", ".lua", ".hs"})
_MARKUP = frozenset({".html", ".htm", ".xml", ".svg"})
# ``//`` lines that steer the build or the compiler, so they are code: Go's build and
# generate directives and TypeScript's triple-slash references.
_SLASH_CODE = ("//go:", "// +build", "/// <")


def is_comment(path: str, line: str) -> bool:
    """Whether ``line`` (a diff line's text, its ``+``/``-`` taken off) of ``path`` is a whole
    comment and nothing else, by the file's type. A blank line and a file type not listed
    here are not; nor is a line starting ``*`` or ``/*`` that does not close on itself."""
    text = line.strip()
    if not text:
        return False
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _HASH:
        # A shebang, an encoding declaration or Ruby's magic comment changes how the file
        # runs, so it is code (#4812 review).
        return text.startswith("#") and not text.startswith(_HASH_CODE)
    if suffix in _DASH:
        return text.startswith("--")
    if suffix in _SLASH | _SLASH_ONLY and text.startswith("//"):
        return not text.startswith(_SLASH_CODE)
    if suffix in _BLOCK:
        return _closes_on_itself(text, "/*", "*/")
    if suffix in _MARKUP:
        return _closes_on_itself(text, "<!--", "-->")
    return False


def _closes_on_itself(text: str, start: str, end: str) -> bool:
    """A one-line block comment with nothing after it: ``/* a */``, not ``/* a */ x();``."""
    return (
        text.startswith(start)
        and len(text) >= len(start) + len(end)
        and text.find(end, len(start)) == len(text) - len(end)
    )


@dataclass(frozen=True)
class Counted:
    """A diff's code lines: ``files`` maps each changed file (its new name; a deleted file's
    old one) to its added and removed lines that are not comments, ``renamed`` holds both
    names of every rename."""

    files: Mapping[str, tuple[int, int]]
    renamed: frozenset[str] = frozenset()

    @property
    def total(self) -> int:
        return sum(added + removed for added, removed in self.files.values())

    def sizes(self) -> dict[str, int]:
        """Lines added plus removed per file, as ``work_router.compared_changes`` gives them."""
        return {name: added + removed for name, (added, removed) in self.files.items()}


def _header_path(value: str) -> str | None:
    """The path of a ``--- a/x``/``+++ b/x`` header's value; None for ``/dev/null``.

    Git ends a name holding a space with a tab, which is cut. A quoted name (one git had to
    escape) gives "": the caller then cannot name the file and counts nothing."""
    value = value.split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        return ""
    return value[2:] if value[:2] in ("a/", "b/") else value


def _git_header(line: str) -> str:
    """The new name in a ``diff --git a/x b/y`` header, "" when it cannot be split surely."""
    body = line[len("diff --git ") :]
    if body.startswith('"') or body.count(" b/") != 1:
        return ""
    old, new = body.split(" b/")
    return new if old.startswith("a/") and old[2:] == new else ""


def count_patch(patch: str) -> Counted | None:
    """``Counted`` from ``git diff -U0`` output; None when any file in it cannot be named."""
    files: dict[str, tuple[int, int]] = {}
    renamed: set[str] = set()
    name: str | None = None
    old = new = ""
    in_hunk = False
    added = removed = 0

    def close() -> bool:
        if name is None:
            return True
        path = new or old or name
        if not path:
            return False
        held = files.get(path, (0, 0))
        files[path] = (held[0] + added, held[1] + removed)
        return True

    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            if not close():
                return None
            name, old, new, in_hunk, added, removed = _git_header(line), "", "", False, 0, 0
            continue
        if name is None:
            continue
        if in_hunk:
            if line.startswith("+") and not is_comment(new or old or name, line[1:]):
                added += 1
            elif line.startswith("-") and not is_comment(new or old or name, line[1:]):
                removed += 1
            elif line.startswith("@@"):
                continue
            continue
        if line.startswith("@@"):
            in_hunk = True
        elif line.startswith("rename from "):
            old = line[len("rename from ") :]
            renamed.add(old)
        elif line.startswith("rename to "):
            new = line[len("rename to ") :]
            renamed.add(new)
        elif line.startswith("--- "):
            old = _header_path(line[4:]) or old
        elif line.startswith("+++ "):
            new = _header_path(line[4:]) or ""
            # A deleted file keeps its old name; a quoted new name stays "" and fails close.
            if line[4:].split("\t", 1)[0] == "/dev/null":
                new = old
    if not close():
        return None
    if any(item.startswith('"') or not item for item in renamed):
        return None
    return Counted(files, frozenset(renamed))


class LocalCounter:
    """The checkouts the deck has seen per repository, and the counts made in them.

    One per process (``COUNTER``), used from the event loop only: the dicts change with no
    ``await`` between a read and its write, so it needs no lock."""

    def __init__(self) -> None:
        self._checkouts: dict[str, list[str]] = {}
        self._counted: OrderedDict[tuple[str, str, str, bool], Counted] = OrderedDict()
        self._unknown: OrderedDict[tuple, float] = OrderedDict()
        self._limit = asyncio.Semaphore(_PARALLEL)

    def remember(self, repository: str, checkout: str) -> None:
        """``checkout`` is a checkout of ``repository`` (its ``origin``), newest first."""
        held = self._checkouts.setdefault(repository.lower(), [])
        if checkout in held:
            held.remove(checkout)
        held.insert(0, checkout)
        del held[_CHECKOUTS:]

    def held(self, repository: str, base: str, head: str) -> Counted | None:
        """The count already made for ``base...head``, or None: a synchronous read for the
        Work overview, which runs no git. Keyed by the base too, as a pull request retargeted
        or a base that took in commits the head shares has another diff (#4812 review)."""
        return self._counted.get((repository.lower(), base.lower(), head.lower(), False))

    async def count(
        self, repository: str, base: str, head: str, *, ancestor: bool = False
    ) -> Counted | None:
        """The code lines of ``base...head`` in a remembered checkout of ``repository``, or
        None when none holds both commits or git fails. ``ancestor`` also requires ``base``
        to be an ancestor of ``head`` (a fix on top of a reviewed head, not a rewrite)."""
        if not repository or not base or not head:
            return None
        key = (repository.lower(), base.lower(), head.lower(), ancestor)
        done = self._counted.get(key)
        if done is not None:
            return done
        checkouts = tuple(self._checkouts.get(repository.lower(), ()))
        # An unknown is kept only briefly and for the checkouts it was asked in: one remembered
        # since is tried at once (#4812 helpers).
        if self._unknown.get(key + (checkouts,), 0.0) > time.monotonic():
            return None
        for checkout in checkouts:
            counted = await self._count_in(checkout, base, head, ancestor)
            if counted is not None:
                self._keep(key, counted)
                return counted
        self._unknown[key + (checkouts,)] = time.monotonic() + _UNKNOWN_S
        while len(self._unknown) > _CACHED:
            self._unknown.popitem(last=False)
        return None

    def _keep(self, key: tuple[str, str, str, bool], counted: Counted) -> None:
        self._counted[key] = counted
        self._counted.move_to_end(key)
        while len(self._counted) > _CACHED:
            self._counted.popitem(last=False)

    async def _count_in(
        self, checkout: str, base: str, head: str, ancestor: bool
    ) -> Counted | None:
        for sha in (base, head):
            if await self._git(checkout, "cat-file", "-e", f"{sha}^{{commit}}") is None:
                return None
        if ancestor:
            if await self._git(checkout, "merge-base", "--is-ancestor", base, head) is None:
                return None
        patch = await self._git(
            checkout,
            *("-c", "core.quotePath=false", "diff", "-U0", "-M", "--no-color"),
            *("--no-ext-diff", "--no-textconv", f"{base}...{head}"),
        )
        return count_patch(patch) if patch is not None else None

    async def _git(self, checkout: str, *args: str) -> str | None:
        """Git's output, or None for any failure: a non-zero exit, an error or the timeout."""
        try:
            async with self._limit:
                process = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    checkout,
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    stdout, _ = await asyncio.wait_for(process.communicate(), _TIMEOUT_S)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
                    return None
                except asyncio.CancelledError:
                    # A cancelled caller leaves no git running behind it.
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    raise
        except OSError as exc:
            log.debug("code lines: git in %s failed: %s", checkout, exc)
            return None
        if process.returncode != 0:
            return None
        return stdout.decode("utf-8", "replace")


COUNTER = LocalCounter()
