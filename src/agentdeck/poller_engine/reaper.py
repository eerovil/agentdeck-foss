"""Find and release host worktrees no Work claims (#1094).

Cleanup everywhere else runs from the *record* side: the runtime removes a worktree only
because a Work claimed that exact path and the Work has ended (#2149, #2195). That is the
right rule for a wake, because guessing a path from a naming convention is how an engine
deletes a directory it never created. Its cost is that a directory nothing ever claimed —
left by a pre-claims card, a refused registration, or a person — has no owner, and nothing
looks at it again. They only accumulate.

This module is the other direction, as a command rather than a wake: walk what is
actually on disk under each configured checkout's ``.worktrees``, subtract every path a Work
still claims, and judge the remainder. It is deliberately not wired into a
wake. Deleting a worker's unfinished work is a failure this project has already paid for.
It began operator-only, with a person reading the list first; since #3394 a daily timer
(``scripts/reap_all_worktrees.sh``) applies it to every instance, because worktrees filled
the disk, so the rules below are the whole of the safety.

Two rules carry that safety:

* **Paths are compared resolved, never as strings** (``paths.resolve``). On an ostree host
  ``/home`` is a symlink to ``var/home``, and the same directory can be recorded both
  spellings in one ``state.json`` (#1095). A string comparison would read a live, tracked
  worktree as an orphan and delete a worker's uncommitted work. Records are now written
  resolved too, but this side never relies on that: a state file written by an older engine,
  or a candidate path spelled differently by the filesystem walk, must still compare equal.
* **Every unknown keeps the directory.** A fact that cannot be read — an unreadable status,
  a checkout with no base branch, a missing timestamp — is never treated as permission. Only
  a candidate positively proved unclaimed, old, clean and fully merged is reapable.
"""

from __future__ import annotations

import math
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .github import registered_worktree
from .paths import resolve

# How old a directory must be before it is even considered. A card lives for hours, so days
# of quiet is a generous floor: it costs nothing to leave a recent worktree alone for another
# run, and it is the difference between reaping a forgotten card and racing a live one.
DEFAULT_MIN_AGE_DAYS = 7

# Why a candidate was kept. These are shown to an operator verbatim, so they read as reasons
# rather than codes.
CLAIMED = "claimed"
UNREGISTERED = "unregistered"
TOO_NEW = "too-new"
DIRTY = "dirty"
UNMERGED = "unmerged"
UNREADABLE = "unreadable"
# ...and the one verdict that releases anything.
ORPHAN = "orphan"

# Remote-tracking branches to try when a checkout has no ``origin/HEAD`` to ask.
_BASE_FALLBACKS = ("origin/master", "origin/main")

_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class Candidate:
    """One directory found under a checkout's ``.worktrees``, and what could be read about it.

    Every optional fact is ``None`` when it could not be read rather than a guessed default,
    because the judgement below turns any unknown into "keep".
    """

    path: str
    checkout: str
    registered: bool
    age_days: float | None = None
    dirty: int | None = None
    merged: bool | None = None
    branch: str = ""


@dataclass(frozen=True)
class Verdict:
    """What the reaper decided about one candidate, and in one word why."""

    candidate: Candidate
    reap: bool
    reason: str

    @property
    def path(self) -> str:
        return self.candidate.path


def plan(
    candidates: Iterable[Candidate],
    claimed: Iterable[str],
    *,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
) -> list[Verdict]:
    """Judge every candidate against the claimed set. Pure: no disk, no Git, no clock."""
    owned = {resolve(path) for path in claimed}
    return [Verdict(item, *_judge(item, owned, min_age_days)) for item in candidates]


def _judge(item: Candidate, owned: set[str], min_age_days: float) -> tuple[bool, str]:
    """The order is the safety argument: ownership first, then every way of not knowing."""
    if resolve(item.path) in owned:
        return False, CLAIMED
    # Not a worktree this checkout's Git registers. The engine has no validated way to remove
    # it, and a bare `rm -rf` from a poller is not something this module is willing to own.
    if not item.registered:
        return False, UNREGISTERED
    if item.age_days is None or item.dirty is None or item.merged is None:
        return False, UNREADABLE
    if item.age_days < min_age_days:
        return False, TOO_NEW
    # Uncommitted work is somebody's unfinished card, whatever the records say about it.
    if item.dirty > 0:
        return False, DIRTY
    if not item.merged:
        return False, UNMERGED
    return True, ORPHAN


def scan(
    checkouts: Iterable[str],
    *,
    now: datetime,
    runner=subprocess.run,
) -> list[Candidate]:
    """Read every ``.worktrees`` child of every configured checkout, and what Git says about it.

    A checkout with no ``.worktrees`` contributes nothing; an unreadable one contributes
    nothing rather than failing the sweep, because one bad checkout must not hide the other
    four from the operator.
    """
    found: list[Candidate] = []
    for checkout in checkouts:
        root = Path(str(checkout)).expanduser() / ".worktrees"
        try:
            entries = sorted(entry for entry in root.iterdir() if entry.is_dir())
        except OSError:
            continue
        base = _base_ref(root.parent, runner)
        for entry in entries:
            if entry.name == TRASH:  # ``save_and_remove``'s own private folder
                continue
            found.append(_inspect(entry, root.parent, base, now=now, runner=runner))
    return found


def _inspect(path: Path, checkout: Path, base: str, *, now: datetime, runner) -> Candidate:
    target = str(path)
    repo, present = registered_worktree(target, runner=runner)
    if repo is None or not present:
        return Candidate(path=target, checkout=str(checkout), registered=False)
    return Candidate(
        path=target,
        checkout=str(checkout),
        registered=True,
        age_days=_age_days(path, now=now),
        dirty=_dirty_count(target, runner),
        merged=_merged(repo, target, base, runner),
        branch=_branch(target, runner),
    )


def _git(runner, *args: str, cwd: str) -> tuple[int, str]:
    """One bounded, non-raising Git read. Returns ``(-1, "")`` when it could not run at all."""
    try:
        done = runner(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return done.returncode, done.stdout


def _age_days(path: Path, *, now: datetime) -> float | None:
    """Days since the directory last showed any sign of life.

    The *newest* of the directory's own timestamp and its ``.git`` link is used, so anything
    recently touched reads as young. That is the conservative direction: a worktree looking
    younger than it is gets kept for another run, while one looking older than it is would be
    reaped out from under whoever touched it.
    """
    newest: float | None = None
    for entry in (path, path / ".git"):
        try:
            stamp = entry.stat().st_mtime
        except OSError:
            continue
        newest = stamp if newest is None else max(newest, stamp)
    if newest is None:
        return None
    return max(0.0, (now.timestamp() - newest) / _SECONDS_PER_DAY)


def _dirty_count(target: str, runner) -> int | None:
    """Uncommitted and untracked files in the worktree; ``None`` when the status is unreadable."""
    code, out = _git(runner, "status", "--porcelain", cwd=target)
    if code != 0:
        return None
    return sum(1 for line in out.splitlines() if line.strip())


def _branch(target: str, runner) -> str:
    code, out = _git(runner, "rev-parse", "--abbrev-ref", "HEAD", cwd=target)
    return out.strip() if code == 0 else ""


def _base_ref(checkout: Path, runner) -> str:
    """The checkout's base branch, as a remote-tracking ref, or ``""`` when there is none.

    No fetch happens here — a reaper has no business reaching the network. A stale
    remote-tracking ref therefore makes recent work look unmerged, which keeps the directory.
    """
    root = str(checkout)
    code, out = _git(runner, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", cwd=root)
    if code == 0 and out.strip():
        return out.strip().removeprefix("refs/remotes/")
    for candidate in _BASE_FALLBACKS:
        code, _out = _git(runner, "rev-parse", "--verify", "--quiet", candidate, cwd=root)
        if code == 0:
            return candidate
    return ""


def _merged(repo: Path, target: str, base: str, runner) -> bool | None:
    """Is everything this worktree holds already contained in the checkout's base branch?

    This single question stands in for "the branch is merged or gone". A worktree whose HEAD
    is an ancestor of the base has nothing that would be lost — whether its branch was merged
    and deleted, or never had a commit of its own. Anything else, including a base branch that
    cannot be resolved, is unknown and keeps the directory.
    """
    if not base:
        return None
    code, out = _git(runner, "rev-parse", "HEAD", cwd=target)
    head = out.strip()
    if code != 0 or not head:
        return None
    code, _out = _git(runner, "merge-base", "--is-ancestor", head, base, cwd=str(repo))
    if code == 0:
        return True
    if code == 1:
        return False
    return None


def directory_size(path: str) -> int | None:
    """Bytes held under one worktree, or ``None`` when the walk fails.

    This is what an operator actually wants to know before approving a sweep, and it is the
    only reason to walk a candidate's contents at all — so it stays opt-in at the command
    line rather than being paid on every scan.
    """
    total = 0
    try:
        for parent, _dirs, files in os.walk(path, onerror=_raise):
            for name in files:
                try:
                    total += os.lstat(os.path.join(parent, name)).st_size
                except OSError:
                    continue
    except OSError:
        return None
    return total


def _raise(error: OSError) -> None:
    raise error


def _changed_at(path: str) -> float:
    """When ``path`` last changed: its change time too, since a copy can keep an old mtime."""
    info = os.lstat(path)
    return max(info.st_mtime, info.st_ctime)


def untouched_for(path: str, days: float, *, now: datetime) -> bool | None:
    """Has nothing under ``path`` changed for ``days``? ``None`` when the walk fails.

    Every file and folder counts, by its modification or change time, not only the two
    timestamps ``_age_days`` reads, because a worker editing a file deep in the tree must keep
    its worktree. The walk stops at the first recent entry, so a live worktree costs little.
    """
    if not math.isfinite(days) or days <= 0:
        return None
    cutoff = now.timestamp() - days * _SECONDS_PER_DAY
    try:
        if _changed_at(path) > cutoff:
            return False
        for parent, dirs, files in os.walk(path, onerror=_raise):
            for name in [*dirs, *files]:
                if _changed_at(os.path.join(parent, name)) > cutoff:
                    return False
    except OSError:
        return None
    return True


# The cache-directory standard's tag (https://bford.info/cachedir/): pytest, ruff and mypy
# write it into their caches.
_CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
# A moved-aside worktree's name while it is saved, so a run cut off mid-save can resume.
_SAVING = re.compile(r"^\.(?P<name>.+)\.saving$")
# The private folder under ``.worktrees`` a saved worktree is renamed into to be removed.
TRASH = ".leftovers-trash"
# Inside ``TRASH``: one note per worktree this cleanup moved to ``.<name>.saving``, the proof
# that a ``.saving`` worktree is a cut-off save and not one somebody named that way.
MOVED = ".moved"


def rebuildable(folder: Path) -> bool:
    """Is this ignored folder positively known to be output a tool rebuilds?

    The one rule for what a save may leave behind; anything else ignored is saved, because
    the forced removal deletes it too. A name alone proves nothing (a ``build/`` can hold
    data), so each kind needs its own evidence: ``node_modules`` beside a ``package.json``,
    a virtualenv's ``pyvenv.cfg``, a ``__pycache__`` holding only ``.pyc`` files, or a
    cache's ``CACHEDIR.TAG``.
    """
    try:
        if folder.is_symlink() or not folder.is_dir():
            return False
        if folder.name == "node_modules":
            return (folder.parent / "package.json").is_file()
        if folder.name in (".venv", "venv"):
            return (folder / "pyvenv.cfg").is_file()
        if folder.name == "__pycache__":
            return all(
                entry.is_file() and not entry.is_symlink() and entry.suffix == ".pyc"
                for entry in folder.iterdir()
            )
        with open(folder / "CACHEDIR.TAG", "rb") as tag:
            return tag.read(len(_CACHEDIR_SIGNATURE)) == _CACHEDIR_SIGNATURE
    except OSError:
        return False


# More ignored bytes than this to save keeps the worktree for a person to look at.
IGNORED_SAVE_LIMIT = 256 * 1024 * 1024


@dataclass(frozen=True)
class ProcessUse:
    """What a scan of ``/proc`` could and could not see.

    ``paths`` holds every path a readable process has as its folder, executable, root, open
    file or mapping. ``unreadable`` names the live processes whose files this user may not
    read, with their owner: root daemons and our own non-dumpable ones (``sshd-session``,
    ``systemd --user``). A process that ends during the scan is in neither.
    """

    paths: frozenset[str]
    unreadable: tuple[tuple[int, int], ...]


def _gone(error: OSError) -> bool:
    """Did a read fail because the process ended, rather than because it is not ours?"""
    return isinstance(error, (FileNotFoundError, ProcessLookupError))


def paths_in_use(proc: Path = Path("/proc")) -> ProcessUse | None:
    """Scan every process for the paths it uses; ``None`` when ``proc`` cannot be listed.

    An empty result must mean "nobody uses anything", so a process whose files cannot be
    read is reported in ``unreadable`` instead of being skipped as if it used nothing; only
    one that ended mid-scan is skipped. The caller decides what an unreadable one blocks.
    """
    try:
        entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    used: set[str] = set()
    unreadable: list[tuple[int, int]] = []
    for entry in entries:
        found, refused = _process_paths(entry)
        # What was read is kept even when a later read was refused: a seen cwd still counts.
        used.update(found)
        if refused:
            try:
                unreadable.append((int(entry.name), entry.stat().st_uid))
            except OSError as exc:
                if not _gone(exc):
                    unreadable.append((int(entry.name), -1))
    return ProcessUse(frozenset(used), tuple(unreadable))


def _process_paths(entry: Path) -> tuple[set[str], bool]:
    """One process's paths, and whether a read was refused while it was still running.

    The paths read before a refusal are returned too: they are positive evidence of use.
    """
    found: set[str] = set()

    def read(links: Iterable[Path]) -> None:
        for link in links:
            try:
                found.add(os.path.realpath(os.readlink(link)))
            except OSError as exc:
                # An fd closed between the listing and the read is gone, not hidden.
                if not _gone(exc):
                    raise

    try:
        read([entry / "cwd", entry / "exe", entry / "root"])
        read(list((entry / "fd").iterdir()))
        with open(entry / "maps") as maps:
            for line in maps:
                fields = line.split(None, 5)
                if len(fields) == 6 and fields[5].startswith("/"):
                    found.add(os.path.realpath(fields[5].strip()))
    except OSError as exc:
        return found, not _gone(exc) and entry.exists()
    return found, False


def in_use(path: str, used: Iterable[str]) -> bool:
    """Is a process using ``path`` or anything below it?"""
    root = os.path.realpath(path)
    return any(item == root or item.startswith(root + os.sep) for item in used)


def _snapshot(path: str) -> dict[str, tuple[int, int, int, int]] | None:
    """Every file and folder under ``path`` with its inode, size and change times.

    Two snapshots differ when anything was written, added, removed or replaced between them,
    even a write that keeps a file's name and status line; ``None`` when the walk fails.
    Keyed by path relative to ``path``, so the same tree compares equal after a rename; the
    top folder and its ``.git`` link, which every ``git worktree move`` changes, are left out.
    """
    found: dict[str, tuple[int, int, int, int]] = {}
    try:
        for parent, dirs, files in os.walk(path, onerror=_raise):
            for name in (os.path.join(parent, item) for item in [*dirs, *files]):
                rel = os.path.relpath(name, path)
                if rel == ".git":  # rewritten by every ``git worktree move``
                    continue
                info = os.lstat(name)
                found[rel] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except OSError:
        return None
    return found


def _size(path: Path) -> int:
    """Bytes a save of ``path`` copies: one file, or a folder less its rebuildable parts."""
    try:
        if not path.is_dir() or path.is_symlink():
            return path.lstat().st_size
        total = 0
        for parent, dirs, files in os.walk(path):
            dirs[:] = [name for name in dirs if not rebuildable(Path(parent) / name)]
            total += sum(os.lstat(os.path.join(parent, name)).st_size for name in files)
        return total
    except OSError:
        return 0


def _ignored_to_save(target: str, runner) -> list[str] | None:
    """Ignored files and folders a forced removal would delete and a save must keep.

    ``None`` when Git cannot list them. A folder ``rebuildable`` proves a tool's output is
    left out with everything in it; ``_copy`` skips such folders deeper down too.
    """
    code, listing = _git_bytes(
        runner,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "-z",
        cwd=target,
    )
    if code != 0:
        return None
    keep = []
    for rel in filter(None, listing.decode(errors="surrogateescape").split("\0")):
        rel = rel.rstrip("/")
        if not rebuildable(Path(target) / rel):
            keep.append(rel)
    return keep


def _copy(source: Path, copy: Path) -> None:
    copy.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():

        def skip(parent: str, names: list[str]) -> list[str]:
            return [name for name in names if rebuildable(Path(parent) / name)]

        shutil.copytree(source, copy, symlinks=True, ignore=skip)
    else:
        shutil.copy2(source, copy, follow_symlinks=False)


def private_to_us(path: str) -> bool:
    """Is ``path`` behind a folder only its owner (us) may enter?

    Then another non-root user's process cannot use it, readable or not. A path that does not
    exist (yet, or any more: a save's ``.saving`` name, a moved-aside original) is judged by
    its nearest existing folder, since nobody else could have reached it there either.
    """
    me = os.getuid()
    for folder in [Path(path), *Path(path).parents]:
        try:
            info = folder.stat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if info.st_uid == me and not info.st_mode & 0o011:
            return True
    return False


def unknown_use(use: ProcessUse | None, path: str) -> str:
    """Why the scan cannot show nobody uses ``path``; ``""`` when it can.

    One rule for every caller: every live process that could reach ``path`` must be read,
    with one exception the owner accepted (#4267, 2026-11-11). An unprivileged user can never
    read root's processes or its own protected ones (``sshd-session``, the desktop, a
    password manager): about 360 on the owner's host, so counting them would keep every
    worktree forever. They count as not using it. The accepted limit: one of them could
    write into a worktree untouched for 30 days in the instant before it is removed. Another
    user's unreadable process blocks unless ``path`` is behind our private home folder, and
    so does one whose owner could not be read.
    """
    if use is None:
        return "the process list could not be read"
    if in_use(path, use.paths):
        return "a process is using it"
    me = os.getuid()
    blocking = [
        pid
        for pid, uid in use.unreadable
        if uid == -1 or (uid not in (0, me) and not private_to_us(path))
    ]
    if blocking:
        return f"{len(blocking)} processes that could use it cannot be read"
    return ""


def _busy(*paths: str) -> str:
    """``unknown_use`` over a fresh scan, for each of ``paths``."""
    use = paths_in_use()
    for path in paths:
        why = unknown_use(use, path)
        if why:
            return why
    return ""


def _private_folder(path: Path) -> None:
    """Make ``path`` a new folder only its owner can read, whatever the umask."""
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    if path.stat().st_mode & 0o777 != 0o700:
        raise OSError(f"{path} is not private")


def _flush(dest: Path) -> None:
    """Write every saved file and folder under ``dest``, and ``dest`` itself, to the disk.

    The forced removal is durable on its own disk the moment it returns; without this, a
    power cut could keep the removal and lose a save still in the page cache.
    """
    for parent, _dirs, files in os.walk(dest, onerror=_raise):
        for name in files:
            full = os.path.join(parent, name)
            if os.path.islink(full):
                continue
            fd = os.open(full, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def saving_original(path: str) -> str | None:
    """The worktree path a moved-aside ``.<name>.saving`` came from; ``None`` for any other."""
    found = _SAVING.match(Path(path).name)
    return str(Path(path).with_name(found["name"])) if found else None


def _trash(checkout: str | Path) -> Path:
    return Path(str(checkout)).expanduser() / ".worktrees" / TRASH


def _moved_note(checkout: str | Path, aside: str) -> Path:
    return _trash(checkout) / MOVED / Path(aside).name


def _private_trash(checkout: str | Path) -> str:
    """Make sure ``TRASH`` and its ``MOVED`` notes folder are private; ``""`` or why not."""
    try:
        for folder in (_trash(checkout), _trash(checkout) / MOVED):
            if not folder.exists():
                _private_folder(folder)
            info = folder.lstat()
            if folder.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return f"{folder} is not a private folder"
    except OSError as exc:
        return f"no private folder to remove it from: {exc}"
    return ""


def note_moved(checkout: str | Path, aside: str, original: str) -> str:
    """Record, durably, that this cleanup is moving ``original`` to ``aside``; ``""`` or why not."""
    why = _private_trash(checkout)
    if why:
        return why
    note = _moved_note(checkout, aside)
    try:
        fd = os.open(note, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, original.encode(errors="surrogateescape"))
            os.fsync(fd)
        finally:
            os.close(fd)
        fd = os.open(note.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        return f"could not note the move: {exc}"
    return ""


def moved_aside(item: Candidate) -> str | None:
    """The original path when this cleanup moved ``item`` aside and was cut off; else ``None``.

    A name alone proves nothing: somebody may make a ``.<name>.saving`` worktree for their
    own reasons, and that one must wait out the idle days like any other.
    """
    original = saving_original(item.path)
    if not original:
        return None
    try:
        noted = _moved_note(item.checkout, item.path).read_bytes()
    except OSError:
        return None
    return original if noted.decode(errors="surrogateescape") == original else None


def _forget_moved(checkout: str | Path, aside: str) -> None:
    try:
        _moved_note(checkout, aside).unlink()
    except FileNotFoundError:
        pass


def save_and_remove(
    item: Candidate,
    leftovers: Path,
    *,
    now: datetime,
    runner=subprocess.run,
    busy=None,
) -> str:
    """Save a worktree's own work under ``leftovers``, then remove it; ``""`` or why it stayed.

    A dirty or unmerged worktree no Work claims is kept by ``plan``, and before #4267 that
    meant forever: 132 of them, some from July, held about 9 GB of the home disk. The owner
    chose (#4148, #4267) to save such a worktree's own work to the hard disk and remove it
    once it sat idle; the caller decides how long (``reap-worktrees --save-after-days``).

    Saved per worktree, in a private ``<checkout>__<worktree>``: ``status.txt`` (path, HEAD,
    branch and status), ``changes.patch`` (uncommitted changes to tracked files),
    ``untracked/`` (new files Git does not ignore), ``ignored/`` (every ignored file but
    folders ``rebuildable`` proves are a tool's output, such as a ``.env``) and
    ``head.bundle`` (commits no remote branch has, so a detached HEAD's commits survive
    too; a branch also stays in the checkout).

    The removal is forced, because a dirty worktree is the point, so a check in front of it
    must leave no writer a way in afterwards (see ``remove_worktrees`` in ``github.py``):

    1. ``git worktree move`` renames it to ``.<name>.saving``, so nothing opens it by its own
       path any more; a lock stops this. A run cut off after this resumes from that name.
    2. The save is taken, flushed to disk and checked against a snapshot taken before it.
    3. ``_remove_unseen`` renames it into a random name inside a private folder, where no
       other user can reach it and no program of ours knows the name, and checks again that
       the tree is unchanged and no process has anything open in it. Only then is it removed.

    Any failure moves the worktree back to its own path.
    """
    busy = busy or _busy
    original = moved_aside(item)
    if original:  # a run cut off mid-save: it is already aside
        aside, original = item.path, original
    else:
        original = item.path
        aside = str(Path(original).with_name(f".{Path(original).name}.saving"))
    why = busy(original, aside)
    if why:
        return why
    repo, present = registered_worktree(item.path, runner=runner)
    if repo is None or not present:
        return "no longer a registered worktree"
    if item.path == original:
        if os.path.lexists(aside):
            return f"{aside} already exists"
        why = note_moved(item.checkout, aside, original)
        if why:
            return why
        code, _out = _git(runner, "worktree", "move", original, aside, cwd=str(repo))
        if code != 0:
            _forget_moved(item.checkout, aside)
            return "git refused to move it aside (locked?)"
    why, before = _save_aside(item, original, aside, leftovers, now=now, runner=runner)
    if not why:
        why = _remove_unseen(original, aside, repo, before, runner=runner, busy=busy)
        if not why:
            _forget_moved(item.checkout, aside)
            return ""
    if os.path.lexists(original):
        return f"{why}; {original} exists, so it stays at {aside}"
    code, _out = _git(runner, "worktree", "move", aside, original, cwd=str(repo))
    if code != 0:
        return f"{why}; it could not be moved back and is at {aside}"
    _forget_moved(item.checkout, aside)
    return why


def _remove_unseen(original: str, aside: str, repo: Path, before, *, runner, busy) -> str:
    """Rename the saved worktree where nothing can find it, check it once more, remove it."""
    trash = _trash(repo)
    why = _private_trash(repo)
    if why:
        return why
    hidden = str(trash / f"{Path(original).name}.{secrets.token_hex(16)}")
    code, _out = _git(runner, "worktree", "move", aside, hidden, cwd=str(repo))
    if code != 0:
        return "git refused to move it into the private folder"
    why = ""
    if _snapshot(hidden) != before:
        why = "it changed while being saved"
    else:
        why = busy(original, aside, hidden)
    if not why:
        code, _out = _git(runner, "worktree", "remove", "--force", hidden, cwd=str(repo))
        if code == 0:
            return ""
        why = "git refused to remove it"
    code, _out = _git(runner, "worktree", "move", hidden, aside, cwd=str(repo))
    if code != 0:
        return f"{why}; it could not be moved back and is at {hidden}"
    return why


def recover_hidden(checkouts: Iterable[str], *, runner=subprocess.run) -> list[str]:
    """Move worktrees a cut-off run left in the private folder back to ``.<name>.saving``.

    The next ``save_and_remove`` of that name then resumes the save without waiting out the
    idle days again. Returns what it could not move back, for the log.
    """
    stuck: list[str] = []
    for checkout in checkouts:
        trash = _trash(checkout)
        try:
            entries = sorted(entry for entry in trash.iterdir() if entry.name != MOVED)
        except OSError:
            continue
        for entry in entries:
            name = entry.name.rpartition(".")[0]
            aside = entry.parent.parent / f".{name}.saving"
            if not name or aside.exists():
                stuck.append(str(entry))
                continue
            code, _out = _git(
                runner, "worktree", "move", str(entry), str(aside), cwd=str(trash.parent.parent)
            )
            if code != 0:
                stuck.append(str(entry))
    return stuck


def _save_aside(
    item: Candidate, original: str, aside: str, leftovers: Path, *, now: datetime, runner
) -> tuple[str, dict | None]:
    """Save the moved-aside worktree at ``aside``: why it must stay, and its snapshot."""
    before = _snapshot(aside)
    if before is None:
        return "files unreadable", None
    code, status = _git(runner, "status", "--porcelain", "--untracked-files=all", cwd=aside)
    code_head, head = _git(runner, "rev-parse", "HEAD", cwd=aside)
    if code != 0 or code_head != 0:
        return "status unreadable", None
    ignored = _ignored_to_save(aside, runner)
    if ignored is None:
        return "ignored files unreadable", None
    ignored_bytes = sum(_size(Path(aside) / rel) for rel in ignored)
    if ignored_bytes > IGNORED_SAVE_LIMIT:
        return f"its ignored files hold {ignored_bytes // 2**20} MB, too much to save", None
    name = f"{Path(item.checkout).name}__{Path(original).name}"
    dest = leftovers / name
    if dest.exists():
        dest = leftovers / f"{name}-{now:%Y%m%d%H%M%S}"
    try:
        _private_folder(dest)
        (dest / "status.txt").write_text(f"{original}\n{head.strip()}\n{item.branch}\n{status}")
        code, patch = _git_bytes(runner, "diff", "--binary", "HEAD", cwd=aside)
        if code != 0:
            return "changes unreadable", None
        if patch:
            (dest / "changes.patch").write_bytes(patch)
        code, listing = _git_bytes(
            runner, "ls-files", "--others", "--exclude-standard", "-z", cwd=aside
        )
        if code != 0:
            return "untracked files unreadable", None
        for rel in filter(None, listing.decode(errors="surrogateescape").split("\0")):
            _copy(Path(aside) / rel, dest / "untracked" / rel)
        for rel in ignored:
            _copy(Path(aside) / rel, dest / "ignored" / rel)
    except OSError as exc:
        return f"could not save: {exc}", None
    code, count = _git(runner, "rev-list", "--count", "HEAD", "--not", "--remotes", cwd=aside)
    if code != 0:
        return "unpushed commits unreadable", None
    if count.strip() != "0":
        code, _out = _git(
            runner,
            "bundle",
            "create",
            str(dest / "head.bundle"),
            "HEAD",
            "--not",
            "--remotes",
            cwd=aside,
        )
        if code != 0:
            return "could not bundle its commits", None
    try:
        _flush(dest)
    except OSError as exc:
        return f"could not flush the save to disk: {exc}", None
    code, again = _git(runner, "status", "--porcelain", "--untracked-files=all", cwd=aside)
    code_head, head_again = _git(runner, "rev-parse", "HEAD", cwd=aside)
    if code != 0 or code_head != 0 or again != status or head_again != head:
        return "it changed while being saved", None
    if _snapshot(aside) != before:
        return "it changed while being saved", None
    return "", before


def _git_bytes(runner, *args: str, cwd: str) -> tuple[int, bytes]:
    """``_git`` for output that may not be text: a binary patch or a NUL-separated list."""
    try:
        done = runner(["git", "-C", cwd, *args], capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return -1, b""
    return done.returncode, done.stdout
