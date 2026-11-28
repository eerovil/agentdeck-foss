"""Keep a "published" label on a Work's issue while all its pull requests have merged (#4794).

A project names the label per issue repository (``[[repos]] published_label``, read by
``poller_ops.release_policies``). A tracker that reads only issue labels can then tell a
card whose changes have all shipped from one still in work.

**The rule** (the owner's, "any unmerged pull request means työn alla"): the label is on only
while the Work's current round (``work_rounds``) has at least one registered pull request and
every one of them reads as merged. Open, closed without merging and unreadable all count as
unmerged. A failed read takes the label off, never puts it on.

**Every change records an intent first.** A trigger runs no sync itself: it writes one
``published:<owner/repo#n>`` entry, due at once, into the router's pending-actions file
(``work_router.PendingActions``), which every deck process shares. The triggers: a pull
request registered, replaced or taken away for a Work (``state.AppState.set_pull_requests``,
the web's ``register_pull``), a registered pull request closed or reopened (the router's
deliveries), and any new round starting (``work_rounds.start``). The router's retry loop runs
the due entries (``sync``). An entry goes only once a sync read everything and left the label
right, and only if no trigger re-armed it meanwhile (its ``requested`` token); a failed one is
tried again on the pending file's growing back-off, never dropped after a day as other kinds
are.

**One sync at a time, in every process.** A sync holds a file lock beside the pending file
from its first read to its last write. It is taken without waiting (``LOCK_NB``), so the event
loop never blocks on another process's GitHub calls: a sync that finds it held re-arms its
entry a few seconds on, keeping its ``requested`` token. Registrations and rounds are read
from the database at sync time, never a process's memory, so the last sync decides from the
latest registered set.

**Turning it on** for a repository (or changing its label) is seen when the router compares
the manifests' labels with the ones it last reconciled (``published-labels.json`` beside the
pending file), at its start and on each batch of deliveries. It writes one
``published-pass:<owner/repo>`` entry, which lists the repository's open issues and writes an
intent for each one that has a registered pull request or carries the label. A changed label
name puts the new one on; the old name is left where it was, and turning the setting off
leaves every label as it is: neither is this deck's to take off any more.

Nothing here is awaited by a merge or the release: the router runs the syncs in a task of
their own beside its retry loop, one at a time, so slow GitHub reads hold up no review,
start or message. No timer (ADR 0007).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The pending-actions kinds this module owns.
PUBLISHED = "published"
PASS = "published-pass"
#: How long a sync that found another one running waits before it looks again.
LOCK_WAIT_S = 5.0
#: The label's colour and description when the deck creates it.
LABEL_COLOR = "0e8a16"
LABEL_DESCRIPTION = "Every pull request of this issue's Work has merged (AgentDeck)"
#: The file of the labels last reconciled, beside the pending file.
RECONCILED_FILENAME = "published-labels.json"
_GH_TIMEOUT_S = 30.0
#: The most open issues a pass lists in one repository; more is logged.
OPEN_ISSUES_LIMIT = 2000
_ISSUE_RE = re.compile(r"^[\w.-]+/[\w.-]+#\d+$")

_pending: Any = None
_nudge: Callable[[], object] | None = None
_loop: asyncio.AbstractEventLoop | None = None


def configure(pending: Any, nudge: Callable[[], object] | None = None) -> None:
    """Make ``request`` write into ``pending`` (the router's ``PendingActions``) and call
    ``nudge`` after, which wakes the router's retry loop wherever it runs. ``None`` turns
    ``request`` off. Takes the store rather than building it, so this module imports nothing
    of the router's and stays out of every test selection that reaches the router."""
    global _pending, _nudge, _loop
    _pending, _nudge = pending, (nudge if pending is not None else None)
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        _loop = None


def bind_loop() -> None:
    """Remember the running event loop, for a nudge from a worker thread. The web builds
    its app before its loop runs, so its lifespan calls this once the loop does."""
    global _loop
    _loop = asyncio.get_running_loop()


def _wake() -> None:
    """Call the nudge on the event loop's thread: a round starts in ``asyncio.to_thread``,
    where a coroutine-world nudge would find no loop and be dropped."""
    global _loop
    nudge = _nudge
    if nudge is None:
        return
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = _loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(nudge)
                return
    with contextlib.suppress(Exception):
        nudge()


def issue_of(work_id: object) -> str:
    """The lowercased ``owner/repo#n`` of a Work id or issue ref, "" for anything else."""
    issue = str(work_id or "").strip().removeprefix("github_issue:").lower()
    return issue if _ISSUE_RE.match(issue) else ""


def key_of(issue: str) -> str:
    return f"{PUBLISHED}:{issue}"


def request(work_ids: Iterable[object]) -> list[str]:
    """Record a sync of each Work in ``work_ids`` in this process's configured pending file;
    the keys written. A no-op where ``configure`` was not called (a test, a tool)."""
    pending = _pending
    if pending is None:
        return []
    keys = request_into(pending, work_ids)
    if keys:
        _wake()
    return keys


def request_into(
    pending: Any, work_ids: Iterable[object], now: float | None = None, *, strict: bool = False
) -> list[str]:
    """Write one due ``published:<issue>`` entry per Work into ``pending``, each with a new
    ``requested`` token, so a sync already running for it does not take it away. ``strict``
    raises a failed write, for a caller that can try again (the router's deliveries)."""
    import time

    when = time.time() if now is None else now
    keys = []
    for issue in sorted({issue_of(work_id) for work_id in work_ids} - {""}):
        key = key_of(issue)
        try:
            pending.put(
                key,
                {"kind": PUBLISHED, "work": issue, "requested": uuid.uuid4().hex},
                when,
                at=when,
            )
        except Exception:  # noqa: BLE001 - a trigger must never fail what triggered it
            if strict:
                raise
            log.warning("work-published: %s not recorded", issue, exc_info=True)
            continue
        keys.append(key)
    return keys


def changed_works(before: Iterable[Any], after: Iterable[Any]) -> set[str]:
    """The Works whose registered pull requests differ between two registration lists."""
    old = {(ref.identity, ref.work_id) for ref in before}
    new = {(ref.identity, ref.work_id) for ref in after}
    return {work_id for _identity, work_id in old ^ new if work_id}


@dataclass(frozen=True)
class Ports:
    """What a sync reads and writes; ``work_services`` gives the real ones."""

    #: Each issue repository's label, by lowercased ``owner/repo``.
    labels: Callable[[], dict[str, str]]
    #: The current round's registered pull requests of a Work (``owner/repo#n``), from the
    #: database. Synchronous: it runs off the event loop.
    registered: Callable[[str], Iterable[str]]
    #: The issue numbers in a repository whose Works registered a pull request (database).
    registered_issues: Callable[[str], Iterable[int]]
    #: ``MERGED``, ``OPEN`` or ``CLOSED`` for ``owner/repo#n``; "" when unreadable.
    pull_state: Callable[[str], Awaitable[str]]
    #: The issue's label names, ``None`` when unreadable.
    issue_labels: Callable[[str, int], Awaitable[Iterable[str] | None]]
    #: Add (``True``) or remove a label; whether GitHub said it did.
    edit_label: Callable[[str, int, str, bool], Awaitable[bool]]
    #: Create a label in a repository; whether it exists now.
    create_label: Callable[[str, str], Awaitable[bool]]
    #: The open issues of a repository as ``(number, labels)``, ``None`` when unreadable.
    open_issues: Callable[[str], Awaitable[list[tuple[int, list[str]]] | None]]


class Registrations:
    """The registrations a sync decides from, read from the database at the time. Every read
    raises when unread (#4794 review): "none registered" would take the label off and end
    the intent, and an unread round would count an earlier round's merged pull requests."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def _rows(self) -> list[Any]:
        if self.db is None:
            return []
        return [
            ref for refs in self.db.load_session_pull_requests_or_raise().values() for ref in refs
        ]

    def registered(self, issue: str) -> tuple[str, ...]:
        """The Work's current-round pull requests, ``owner/repo#n`` lowercased."""
        from . import work_rounds

        pulls = sorted(
            {
                f"{ref.repository}#{ref.number}".lower()
                for ref in self._rows()
                if issue_of(ref.work_id) == issue
            }
        )
        work_id = f"github_issue:{issue}"
        rounds = self.db.load_work_rounds([work_id]) if self.db is not None else {}
        return tuple(work_rounds.current(pulls, rounds.get(work_id)))

    def registered_issues(self, repository: str) -> set[int]:
        """The issue numbers in ``repository`` that have a registered pull request."""
        found = set()
        for ref in self._rows():
            owner, _, number = issue_of(ref.work_id).partition("#")
            if owner == repository.lower() and number:
                found.add(int(number))
        return found

    def works_of_pull(self, repository: str, number: int) -> list[str]:
        """The Works a pull request is registered for."""
        return [
            ref.work_id
            for ref in self._rows()
            if ref.work_id and ref.identity == (repository.lower(), number)
        ]


class PublishedLabels:
    """The router's syncs of the published label (see the module docstring)."""

    def __init__(self, ports: Ports, pending_file: Path | str, clock: Callable[[], float]):
        self.ports = ports
        folder = Path(pending_file).expanduser().parent
        self.lock_path = folder / ".published-labels.lock"
        self.reconciled_path = folder / RECONCILED_FILENAME
        self._clock = clock

    # --- the lock -----------------------------------------------------------------------

    def _try_lock(self) -> int | None:
        """The held lock's descriptor, or ``None`` when another sync holds it. Never waits."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # --- one Work -----------------------------------------------------------------------

    async def sync(self, pending: Any, key: str, entry: dict) -> None:
        """Run one ``published:`` entry: settle the label, then end or re-arm the entry."""
        issue = issue_of(entry.get("work"))
        repository, _, number = issue.partition("#")
        token = entry.get("requested")
        try:
            label = self.ports.labels().get(repository, "") if issue else ""
            fd = self._try_lock() if label else None
        except Exception:  # noqa: BLE001 - logged; tried again on the back-off
            log.warning("work-published: %s sync not started", issue, exc_info=True)
            self._finish(pending, key, token, False)
            return
        if not label:
            pending.discard(key)  # not this deck's to label (any more)
            return
        if fd is None:
            # Another process's sync runs: look again shortly, keeping the token (#4794 notes).
            with pending._file.locked():  # noqa: SLF001 - only while the entry is still there
                if key in pending.entries:
                    pending.put(key, {}, self._clock(), at=self._clock() + LOCK_WAIT_S)
            return
        try:
            settled = await self.settle(repository, int(number), label, issue)
        except Exception:  # noqa: BLE001 - logged; tried again on the back-off
            log.warning("work-published: %s sync failed", issue, exc_info=True)
            settled = False
        finally:
            self._unlock(fd)
        self._finish(pending, key, token, settled)

    def _finish(self, pending: Any, key: str, token: object, settled: bool) -> None:
        """End the entry if nothing re-armed it during the sync; else leave the re-arm."""
        with pending._file.locked():  # noqa: SLF001 - one read-modify-write of the file
            current = pending.entries.get(key)
            if current is None:
                return
            if current.get("requested") != token:
                return  # a trigger came during the sync: it is due, and runs again
            if settled:
                pending.discard(key)
            else:
                pending.put(key, {}, self._clock())

    async def settle(self, repository: str, number: int, label: str, issue: str) -> bool:
        """Make the label match the rule; whether everything was read and written."""
        import asyncio

        try:
            pulls = sorted(
                {
                    str(pull).lower()
                    for pull in await asyncio.to_thread(self.ports.registered, issue)
                }
            )
            read_all = True
        except Exception:  # noqa: BLE001 - unread registrations: the label comes off
            log.warning("work-published: registrations of %s unread", issue, exc_info=True)
            pulls, read_all = [], False
        merged = bool(pulls) and read_all
        unread = not read_all
        for pull in pulls:
            state = (await self.ports.pull_state(pull)).upper()
            if state == "MERGED":
                continue
            merged = False
            if state in {"OPEN", "CLOSED"}:
                unread = False  # one surely unmerged settles it, whatever else reads
                break
            unread = True
        names = await self.ports.issue_labels(repository, number)
        if names is None:
            if not merged:
                await self.ports.edit_label(repository, number, label, False)
            log.info("work-published: %s labels unread", issue)
            return False
        has = label.casefold() in {str(name).casefold() for name in names}
        done = True
        if merged and not has:
            done = await self.ports.edit_label(repository, number, label, True)
            if not done and await self.ports.create_label(repository, label):
                done = await self.ports.edit_label(repository, number, label, True)
        elif not merged and has:
            done = await self.ports.edit_label(repository, number, label, False)
        if (merged != has) and done:
            log.info(
                "work-published: %s %s %s", issue, "labelled" if merged else "unlabelled", label
            )
        return done and not unread

    # --- turning it on ------------------------------------------------------------------

    def check_settings(self, pending: Any) -> list[str]:
        """Write a pass for each repository whose label is new or changed since the last
        reconcile; the repositories. Records the labels once the passes are written."""
        current = self.ports.labels()
        try:
            stored = json.loads(self.reconciled_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            stored = {}
        except (OSError, ValueError):
            log.warning("work-published: ignoring unreadable %s", self.reconciled_path)
            stored = {}
        stored = stored if isinstance(stored, dict) else {}
        if stored == current:
            return []
        now = self._clock()
        changed = sorted(repo for repo, label in current.items() if stored.get(repo) != label)
        for repository in changed:
            pending.put(
                f"{PASS}:{repository}", {"kind": PASS, "repository": repository}, now, at=now
            )
        self.reconciled_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.reconciled_path.with_name(f".{RECONCILED_FILENAME}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.reconciled_path)
        if changed:
            log.info("work-published: reconciling %s", ", ".join(changed))
        return changed

    async def run_pass(self, pending: Any, key: str, entry: dict) -> None:
        """One ``published-pass:`` entry: an intent for each open issue of the repository
        that has a registered pull request or carries the label."""
        import asyncio

        repository = str(entry.get("repository") or "").lower()
        try:
            label = self.ports.labels().get(repository, "")
        except Exception:  # noqa: BLE001 - logged; the pass is tried again
            log.warning("work-published: label of %s unread", repository, exc_info=True)
            pending.put(key, {}, self._clock())
            return
        if not label:
            pending.discard(key)
            return
        issues = await self.ports.open_issues(repository)
        try:
            registered = set(await asyncio.to_thread(self.ports.registered_issues, repository))
        except Exception:  # noqa: BLE001 - logged; the pass is tried again
            log.warning("work-published: registrations in %s unread", repository, exc_info=True)
            registered = None
        if issues is None or registered is None:
            pending.put(key, {}, self._clock())
            return
        wanted = [
            f"{repository}#{number}"
            for number, names in issues
            if number in registered or label.casefold() in {str(name).casefold() for name in names}
        ]
        if len(issues) >= OPEN_ISSUES_LIMIT:
            log.warning(
                "work-published: %s has %d+ open issues; the pass saw only the first %d",
                repository,
                OPEN_ISSUES_LIMIT,
                OPEN_ISSUES_LIMIT,
            )
        request_into(pending, wanted, self._clock())
        pending.discard(key)
        log.info("work-published: pass over %s asked %d syncs", repository, len(wanted))


# --- GitHub, through the deck's one gh entry point ----------------------------------------


async def gh_pull_state(pull: str) -> str:
    from . import gh_cli

    repository, _, number = pull.partition("#")
    try:
        code, out = await gh_cli.run(
            ["gh", "pr", "view", number, "--repo", repository, "--json", "state"],
            timeout=_GH_TIMEOUT_S,
            repo=repository,
        )
        raw = json.loads(out) if code == 0 else None
    except (OSError, TimeoutError, ValueError):
        return ""
    return str(raw.get("state") or "").upper() if isinstance(raw, dict) else ""


async def gh_issue_labels(repository: str, number: int) -> list[str] | None:
    from . import gh_cli

    try:
        code, out = await gh_cli.run(
            ["gh", "issue", "view", str(number), "--repo", repository, "--json", "labels"],
            timeout=_GH_TIMEOUT_S,
            repo=repository,
        )
        raw = json.loads(out) if code == 0 else None
    except (OSError, TimeoutError, ValueError):
        return None
    labels = raw.get("labels") if isinstance(raw, dict) else None
    if not isinstance(labels, list):
        return None
    return [str(item.get("name") or "") for item in labels if isinstance(item, dict)]


async def gh_edit_label(repository: str, number: int, label: str, add: bool) -> bool:
    from . import gh_cli

    flag = "--add-label" if add else "--remove-label"
    try:
        code, _out, err = await gh_cli.run_once(
            ["gh", "issue", "edit", str(number), "--repo", repository, flag, label],
            timeout=_GH_TIMEOUT_S,
            repo=repository,
        )
    except (OSError, TimeoutError) as exc:
        log.warning("work-published: %s#%s %s %s failed: %s", repository, number, flag, label, exc)
        return False
    if code != 0:
        log.warning(
            "work-published: %s#%s %s %s: %s",
            repository,
            number,
            flag,
            label,
            err.decode("utf-8", "replace").strip()[:300],
        )
    return code == 0


async def gh_create_label(repository: str, label: str) -> bool:
    from . import gh_cli

    try:
        code, _out, err = await gh_cli.run_once(
            [
                "gh",
                "label",
                "create",
                label,
                "--repo",
                repository,
                "--color",
                LABEL_COLOR,
                "--description",
                LABEL_DESCRIPTION,
            ],
            timeout=_GH_TIMEOUT_S,
            repo=repository,
        )
    except (OSError, TimeoutError):
        return False
    # One that already exists is there: the add is tried again either way.
    return code == 0 or b"already exists" in err


async def gh_open_issues(repository: str) -> list[tuple[int, list[str]]] | None:
    from . import gh_cli

    try:
        code, out = await gh_cli.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--limit",
                str(OPEN_ISSUES_LIMIT),
                "--json",
                "number,labels",
            ],
            timeout=120.0,
            repo=repository,
        )
        raw = json.loads(out) if code == 0 else None
    except (OSError, TimeoutError, ValueError):
        return None
    if not isinstance(raw, list):
        return None
    found = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("number"), int):
            continue
        labels = item.get("labels")
        names = [
            str(label.get("name") or "")
            for label in (labels if isinstance(labels, list) else [])
            if isinstance(label, dict)
        ]
        found.append((item["number"], names))
    return found
