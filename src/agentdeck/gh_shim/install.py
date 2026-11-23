"""Where the shim lives on disk, and how a worker comes to run it.

The shim only works if workers reach it without knowing it exists, so it is
installed as a directory holding one executable named ``gh`` and placed first
on the worker's PATH.  The file is generated rather than checked in because it
has to name *this* deck's interpreter absolutely: a worker's PATH is not the
service's, and a ``python3`` resolved in the worker's environment may be a
different one entirely.

The poller is deliberately not given this directory.  ``poller_engine``'s
``Gh.binary`` defaults to the bare name ``gh``, so a shim leaking onto the
poller's PATH would route its reads through the same budget it is reserving
against, and it would count them twice.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Set in the environment of the real `gh` the shim itself runs, so a shim that
# somehow finds itself again exits through the passthrough instead of looping.
# Its value counts how many shims are already running above this one (#4505).
RECURSION_GUARD = "AGENTDECK_GH_SHIM_ACTIVE"

# A shim this deep refuses rather than runs anything. One nested shim is normal:
# a `gh` extension or `!` alias calling `gh` again reaches the shim with the guard
# set, and passes straight through. A second one means the `gh` found on PATH
# calls back into the shim (#4505): a wrapper script ahead of the real binary made
# shim -> wrapper -> shim -> ... and filled the host's memory.
MAX_NESTING = 2


def nesting(environment: dict[str, str]) -> int:
    """How many shims run above this process.

    The larger of ``RECURSION_GUARD``'s count and the shims among this process's
    ancestors: a wrapper that clears the variable before calling the shim back
    would otherwise start every pass at zero and loop again. Any non-numeric
    value counts as one: older shims and ``github_accounts`` set ``"1"``
    meaning only "a shim is active".
    """
    value = environment.get(RECURSION_GUARD, "").strip()
    try:
        declared = max(int(value), 1) if value else 0
    except ValueError:
        declared = 1
    return max(declared, _shim_ancestors())


def _shim_ancestors(limit: int = 64) -> int:
    """Count the running shims above this process, read from ``/proc``; 0 where none."""
    count = 0
    pid = os.getppid()
    for _ in range(limit):
        if pid <= 1:
            break
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            stat_line = Path(f"/proc/{pid}/stat").read_text()
            parent = int(stat_line.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if any(
            flag == b"-m" and module == b"agentdeck.gh_shim"
            for flag, module in zip(argv, argv[1:], strict=False)
        ):
            count += 1
        pid = parent
    return count


# What a shim exits with when no real `gh` can run: none on PATH, or the one found
# keeps calling the shim back (#4505).
EXIT_NO_GH = 127

# Every failed call, one JSON line each: which authority, installation and token spoke, and
# what GitHub answered (#2714). Beside the metadata cache, so a test's cache is its log too.
FAILURE_LOG_NAME = "gh-shim-failures.jsonl"
_FAILURE_LOG_MAX_BYTES = 2_000_000


def failure_log_path() -> Path:
    cache = os.environ.get("AGENTDECK_GITHUB_CACHE", "~/.cache/agentdeck/github_metadata.sqlite3")
    return Path(cache).expanduser().with_name(FAILURE_LOG_NAME)


def log_failure(record: dict[str, object]) -> Path | None:
    """Append one failed call to the failure log. Observational: never fails the call."""
    path = failure_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _FAILURE_LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        return None
    return path


def refuse_loop(argv: list[str], environment: dict[str, str], depth: int) -> int:
    """Refuse a shim nested ``MAX_NESTING`` deep, say why, and log it (#4505).

    Lives here, not in ``main``, so ``__main__`` can refuse before importing the
    rest of the shim: in a loop every refused process is one more on the host.
    """
    found = real_gh(environment) or "none"
    sys.stderr.write(
        f"agentdeck gh shim: refused — {depth} shims already run above this one, so the "
        f"`gh` found on PATH ({found}), or something it runs, keeps calling `gh` back. "
        "This call ran nothing. If that `gh` is a wrapper, drop the wrapper's own "
        "directory from PATH before it calls `gh`.\n"
    )
    log_failure(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": "loop",
            "command": " ".join(argv[:2]),
            "exit": EXIT_NO_GH,
            "reason": f"{depth} shims deep; gh on PATH is {found}",
            "python": sys.executable,
            "shim": str(Path(__file__).resolve()),
        }
    )
    return EXIT_NO_GH


# The line every generated shim carries, which is how ``real_gh`` knows a ``gh`` on
# PATH is a shim even when it is not the one ``shim_dir`` names (#4066).
_MARKER = "# Generated by agentdeck.gh_shim.install"

_SCRIPT = """#!/bin/sh
# Generated by agentdeck.gh_shim.install — do not edit.
exec {interpreter} -m agentdeck.gh_shim "$@"
"""


def _checkout_root(source: Path | None = None) -> Path | None:
    """Return the source checkout containing this module, when there is one."""
    module = Path(__file__) if source is None else source
    for parent in module.absolute().parents:
        if parent.name == "src" and (parent.parent / "pyproject.toml").is_file():
            return parent.parent
    return None


def _is_disposable_worktree(path: Path) -> bool:
    return ".worktrees" in path.absolute().parts


def launcher_interpreter() -> Path | None:
    """Choose an interpreter whose lifetime matches the host-wide launcher.

    Shared with ``agentdeck.heavy_shim``: both shims generate a launcher that
    outlives the checkout it was installed from, and they must not disagree
    about which interpreter that is.
    """
    checkout = _checkout_root()
    executable = Path(sys.executable).absolute()
    stable_checkout = checkout
    if checkout is not None and checkout.parent.name == ".worktrees":
        stable_checkout = checkout.parent.parent

    if _is_disposable_worktree(executable) or (
        checkout is not None and _is_disposable_worktree(checkout)
    ):
        executable = (
            stable_checkout / ".venv" / "bin" / "python"
            if stable_checkout is not None
            else executable
        )

    if _is_disposable_worktree(executable):
        log.warning("refusing to install a shim with worktree interpreter %s", executable)
        return None
    if not executable.is_file() or not os.access(executable, os.X_OK):
        log.warning("shim interpreter is not executable: %s", executable)
        return None
    return executable


def shim_dir() -> Path:
    """The directory whose ``gh`` is this shim."""
    configured = os.environ.get("AGENTDECK_GH_SHIM_DIR")
    if configured:
        return Path(configured).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    root = (
        Path(data_home).expanduser()
        if data_home
        else Path.home() / ".local" / "share"
    )
    return root / "agentdeck" / "gh-shim"


def ensure_shim(directory: Path | None = None) -> Path | None:
    """Write the shim, returning its directory or ``None`` if it cannot be.

    A shim that cannot be installed is not an error worth failing a spawn over:
    the worker then reaches the real ``gh`` exactly as it does today, which is
    slower and less bounded but entirely correct.
    """
    interpreter = launcher_interpreter()
    if interpreter is None:
        return None
    target = shim_dir() if directory is None else directory
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = target / "gh"
        path.write_text(_SCRIPT.format(interpreter=shlex.quote(str(interpreter))))
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        log.warning("gh shim could not be installed at %s: %s", target, exc)
        return None
    return target


def worker_path(environment: dict[str, str]) -> str:
    """``environment``'s PATH with the shim first, or unchanged if uninstallable."""
    directory = ensure_shim()
    existing = environment.get("PATH", os.defpath)
    if directory is None:
        return existing
    entries = [part for part in existing.split(os.pathsep) if part]
    home = str(directory)
    return os.pathsep.join([home, *(part for part in entries if part != home)])


def real_gh(environment: dict[str, str] | None = None) -> str | None:
    """Find the actual ``gh``, skipping this shim's own directory and any other shim.

    Resolution walks PATH rather than trusting a recorded location, because the
    binary a worker should reach is whichever one it would have reached without
    the shim in front of it. A ``gh`` that is itself a generated shim is skipped
    wherever it lives (#4066): the test suite points ``AGENTDECK_GH_SHIM_DIR`` at
    scratch while a chat worker's PATH still holds the host's shim, and that shim
    then took itself for the real ``gh`` and ran itself without end, one Python
    process deeper each time, until the worker's memory cap killed the chat.
    """
    env = os.environ if environment is None else environment
    home = str(shim_dir())
    entries = [
        part
        for part in env.get("PATH", os.defpath).split(os.pathsep)
        if part and Path(part) != Path(home) and not _is_shim(Path(part) / "gh")
    ]
    return shutil.which("gh", path=os.pathsep.join(entries))


def _is_shim(candidate: Path) -> bool:
    try:
        with candidate.open("rb") as handle:
            head = handle.read(256)
    except OSError:
        return False
    return _MARKER.encode() in head
