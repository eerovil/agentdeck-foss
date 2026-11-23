"""What a merged change touches, for the live check the deck asks after it (#4500).

Two questions, both read from the merged files:

- Is the merge docs or tests only (``docs_only``)? Then nothing runs differently live, and
  the agent records ``nothing_to_check`` with no deploy wait. The set is a fixed list, never
  "any Markdown file": the running deck reads some (``poller/builder/PLAYBOOK.md``), and the
  deploy installs ``docs/agents/lessons.md`` into every agent's guide.
- Which of this deck's processes loads it (``loading_processes``)? The same import walk the
  deploy runs (``scripts/runtime_paths.py``), on the merge commit's own tree, so a change
  that makes an existing module runtime-loaded is named as the runtime's (#2835). It runs
  under the Work's lock, so the walk has a hard limit of a few seconds (the reads of the
  merged pull requests before it keep their own limits); anything unread answers
  ``None``, and the agent is then told to confirm every process restarted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)

#: This deck's own repository (``work_release.DECK_REPOSITORY``).
DECK_REPOSITORY = "eerovil/agentdeck"

#: The files ``scripts/deploy-live.sh``'s ``needs_skills_install`` installs into every agent
#: on a deploy: a merge touching one changes live behaviour. A test keeps the two the same.
SKILLS_INSTALL = re.compile(r"^(skills/|scripts/install-skills\.sh$|docs/agents/lessons\.md$)")
#: The files ``deploy-live.sh``'s ``needs_poller_check`` watches: a merge touching one makes
#: every deploy run the poller tests first, and a failing one blocks the deploy. So a
#: ``tests/test_poller_*`` change is not docs-only. A test keeps the two the same.
POLLER_CHECK = re.compile(
    r"^(src/agentdeck/poller_|scripts/(poller_|check_poller_software\.sh$)"
    r"|poller/(manifests|extensions)/|tests/test_poller_)"
)

#: Paths under a docs folder below that the running deck does read: the Work page's diagram
#: view serves ``docs/diagrams`` from its own checkout (``live_prototype``), and the composer
#: suggests the checkout's own skills and commands (``composer_suggestions``).
_READ_LIVE = ("docs/diagrams/", ".claude/skills/", ".claude/commands/")
#: The deploy itself and the units it restarts. The script is named as a prefix without its
#: suffix: a whole file name here would make the suite picker take this module for a user of
#: it, so every change would reach every test (``scripts/select_suite.py``).
_DEPLOY = ("scripts/deploy-live.", "systemd/")
#: Folders that hold only docs or tests, in every repository.
_ANY_DIRS = ("tests/", ".claude/")
_ANY_TOP = frozenset({"AGENTS.md", "CLAUDE.md"})
#: And in this deck's repository, where the running deck reads none of them.
_DECK_DIRS = ("docs/",)
_DECK_TOP = frozenset({"README.md", "CHANGELOG.md", "CONTEXT.md", "SETUP.md", "TODO.md"})

#: The processes the walk names; ``web`` loads every change.
WEB, SERVICES, RUNTIME = "web", "services", "runtime"

#: The whole step's limit: it runs under the Work's lock, which a person's Ship now waits on.
LIMIT_S = 5.0
#: A scratch tree older than this is a run killed mid-walk: far above the limit, so a running
#: one of another Work is never swept.
STALE_S = 600.0


def _docs_path(deck: bool, path: str) -> bool:
    if SKILLS_INSTALL.match(path) or POLLER_CHECK.match(path) or f"{path}/".startswith(_READ_LIVE):
        return False
    if path.startswith(_ANY_DIRS) or path in _ANY_TOP:
        return True
    return deck and (path.startswith(_DECK_DIRS) or path in _DECK_TOP)


def docs_only(repository: str, files: Iterable[str]) -> bool:
    """Does a merge of ``files`` into ``repository`` change only docs or tests? An empty
    list is not: nothing read proves nothing."""
    deck = repository.lower() == DECK_REPOSITORY
    files = list(files)
    return bool(files) and all(_docs_path(deck, path) for path in files)


def touches_skills(files: Iterable[str]) -> bool:
    """Does the merge change what the deploy's skills install puts in every agent?"""
    return any(SKILLS_INSTALL.match(path) for path in files)


def touches_deploy(files: Iterable[str]) -> bool:
    """Does the merge change the poller, the deploy or a service unit, which a deploy can
    break with no page showing it?"""
    return any(POLLER_CHECK.match(path) or path.startswith(_DEPLOY) for path in files)


def _sweep(parent: Path, now: float) -> None:
    """Remove scratch trees left by a run killed mid-walk (a deploy's restart)."""
    with contextlib.suppress(FileNotFoundError):
        for child in parent.iterdir():
            try:
                if now - child.stat().st_mtime > STALE_S:
                    shutil.rmtree(child)
            except OSError:
                log.warning("live-paths: could not remove %s", child, exc_info=True)


async def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None) -> bytes:
    """Run ``args``; its output, or ``RuntimeError`` on a non-zero exit. A cancel (the
    limit) kills its whole process group (``git fetch``'s HTTPS helper too), so nothing
    outlives the step."""
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await process.communicate()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    if process.returncode != 0:
        raise RuntimeError(f"{args[:3]} exited {process.returncode}: {err[-300:]!r}")
    return out


async def _walk(checkout: Path, sha: str, scratch: Path, env: dict[str, str]) -> dict[str, bytes]:
    """The files each process beyond the web loads at ``sha``'s tree, as the walk prints
    them: fetched by commit when the checkout lacks it (no ref moves), archived into
    ``scratch``, walked there, the two walks at once."""
    try:
        await _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=checkout, env=env)
    except RuntimeError:
        await _run(
            ["git", "fetch", "-q", "--no-write-fetch-head", "origin", sha], cwd=checkout, env=env
        )
    tree = scratch / sha
    tree.mkdir()
    archive = scratch / f"{sha}.tar"
    await _run(["git", "archive", "--format=tar", "-o", str(archive), sha], cwd=checkout, env=env)
    await _run(["tar", "-xf", str(archive), "-C", str(tree)], cwd=scratch, env=None)
    # The merge tree's own walker, never this checkout's: named without its file name in one
    # string, so the suite picker does not take this module for a user of this checkout's
    # copy, which would make every change reach every test (``scripts/select_suite.py``).
    walker = str((tree / "scripts" / "runtime_paths").with_suffix(".py"))
    runtime, services = await asyncio.gather(
        _run([sys.executable, "-I", walker, str(tree)], cwd=tree, env=None),
        _run([sys.executable, "-I", walker, "--services", str(tree)], cwd=tree, env=None),
    )
    return {RUNTIME: runtime, SERVICES: services}


async def loading_processes(
    checkout: Path,
    merges: Iterable[tuple[str, Iterable[str]]],
    *,
    cache: Path | None = None,
    limit: float = LIMIT_S,
    environment: dict[str, str] | None = None,
) -> frozenset[str] | None:
    """The processes of this deck that load ``merges`` (``(merge commit, files)`` each):
    ``web`` always, ``services`` and ``runtime`` when the walk at that commit lists one of
    its files. ``None`` when any of it could not be read in ``limit`` seconds: no merge
    commit, a failed fetch, archive or walk, or the time ran out."""
    merges = [(sha, set(files)) for sha, files in merges]
    if not merges or not all(sha for sha, _files in merges):
        return None
    if environment is None:
        from .github_identity import github_git_environment

        environment = github_git_environment()
    parent = cache or Path.home() / ".cache" / "agentdeck" / "live-paths"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        _sweep(parent, time.time())
        scratch = Path(tempfile.mkdtemp(dir=parent))
    except OSError:
        log.warning("live-paths: fell back, no scratch tree", exc_info=True)
        return None
    try:
        found = {WEB}
        async with asyncio.timeout(limit):
            for sha, files in merges:
                lists = await _walk(checkout, sha, scratch, environment)
                for name, out in lists.items():
                    if files & set(out.decode().split()):
                        found.add(name)
        return frozenset(found)
    except Exception:  # noqa: BLE001 - unread names every process, which fails safe
        log.warning("live-paths: fell back for %s", [sha for sha, _f in merges], exc_info=True)
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
