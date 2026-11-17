"""Lighter review for pull requests that stay outside a repository's guarded code (#4399).

A repository whose manifest entry says ``light_review = "guarded.json"`` gets it: a pull request
that changes nothing its guarded list names merges on a green run of exactly ``bin/test.sh``
and a bench verdict of "no worse than master" (``bin/bench verdict``), with no reviewer.
Everything else, and every pull request a full review ever started for, keeps full review. The
design is on the issue (comments 6893451306 and its revisions up to 6893654956).

Two parts:

- ``decide``: the pure rule. It takes the guarded list read at the base branch's tip, the
  pull request's changed paths and a reader of file text, and returns light or the first rule
  that sends it to full review. Any error is full review.
- ``LightReview``: the service in the services process. It keeps one row per pull request in
  the control database (``light_review``), runs the bench in a pinned checkout of the base tip,
  and sends each answer to the Work's chat. The router asks it at ``pre-review-done`` and before
  any review starts. The release asks it in its look and right before the merge.

What runs at once: the router, the release's look under its per-Work lock, the bench tasks
and the retry timers, all in one event loop. Every write to a row is one statement made under
that pull request's asyncio lock, after reading the row again inside it. The release's Work
lock always comes first: nothing here awaits the release while holding a pull request's lock,
and a look the bench needs is queued (``look``), never awaited.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import itertools
import json
import logging
import os
import re
import signal
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from . import review_history

log = logging.getLogger(__name__)

#: The command whose green run a light merge needs, exactly: no arguments, no shell around it.
TEST_COMMAND = ("bin/test.sh",)
#: The bench's limit, and the one automatic retry's delay after a run that could not run.
BENCH_TIMEOUT_S = 600.0
RETRY_S = 15 * 60.0
# A retry whose live head could not be read looks again this much later, still unspent.
LIVE_UNREAD_S = 60.0
#: A fetch's limit; then SIGTERM, and SIGKILL after the grace (rule 7).
FETCH_TIMEOUT_S = 120.0
FETCH_GRACE_S = 5.0
#: Head entries kept per pull request.
KEEP_HEADS = 20
DEFAULT_LIMIT = 15

LIGHT = "light"
FULL = "full"
#: Why a head is full: the guarded list (files or functions) and the merge limit make the pull
#: request full for good; any other rule makes only that head full (rule 2).
GUARDED, LIMIT, OTHER = "guarded", "limit", "other"
#: Places in the bench line (rule 7): the release's bench of the first in line goes first.
FIRST, NEXT = 0, 1
#: Who commits GitHub's "update branch" merge (rule 6).
GITHUB_COMMITTER = "GitHub <noreply@github.com>"
#: This process's start, for the in-flight marker of a bench (rule 1).
BOOT = uuid.uuid4().hex
#: Bench answers stored on a head.
PASS, WORSE, PENDING, UNRUN = "pass", "worse", "pending", "unrun"
#: The review events that make a pull request sticky (a full review was asked, opened or
#: answered), whichever process wrote them: a Review press in the web writes only these.
STICKY_KINDS = frozenset({review_history.ASKED, review_history.CHAT}) | review_history.VERDICTS

DOCS = ("docs/",)
README = "README.md"


class LightError(Exception):
    """A decision that could not be read or stored: no review starts, the agent asks again."""


@dataclass(frozen=True)
class Decision:
    light: bool
    rule: str = ""
    #: Git could not be read (fetch, base branch, a moved head): nothing was decided or
    #: written (rule 1).
    unread: bool = False
    #: ``guarded``, ``limit`` or ``other`` for a full decision (rule 2).
    kind: str = ""


@dataclass(frozen=True)
class Change:
    """One line of ``git diff --name-status -M``: ``status`` is its first letter."""

    status: str
    path: str
    old: str = ""


# --- the rule -----------------------------------------------------------------------------


def parse_changes(text: str) -> list[Change]:
    """``git diff --name-status -M -z`` output as changes."""
    parts = text.split("\0")
    found: list[Change] = []
    index = 0
    while index < len(parts):
        status = parts[index].strip()
        if not status:
            index += 1
            continue
        letter = status[0]
        if letter in "RC":
            found.append(Change(letter, parts[index + 2], parts[index + 1]))
            index += 3
        else:
            found.append(Change(letter, parts[index + 1]))
            index += 2
    return found


def _covered(path: str, entries: Iterable[str]) -> str:
    for entry in entries:
        if path == entry or path.startswith(entry):
            return entry
    return ""


def _allowed(path: str) -> bool:
    return path.endswith(".py") or path.startswith(DOCS) or path == README


def _is_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _find(tree: ast.Module, name: str) -> ast.AST | None:
    """A top-level function or class, or ``Class.method``, in ``tree``."""
    body: list[ast.stmt] = tree.body
    node: ast.AST | None = None
    for part in name.split("."):
        node = next(
            (
                item
                for item in body
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and item.name == part
            ),
            None,
        )
        if node is None:
            return None
        body = node.body if isinstance(node, ast.ClassDef) else []
    return node


def _blank_tests(tree: ast.Module, safety: frozenset[str]) -> str:
    """``tree`` dumped with the body of each test function not named in ``safety`` blanked.

    Test functions are the top-level ``test_*`` functions and the ``test_*`` methods of
    top-level classes; their names, arguments and decorators still count.
    """

    def blank(items: list[ast.stmt]) -> None:
        for item in items:
            if (
                isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and item.name.startswith("test_")
                and item.name not in safety
            ):
                item.body = [ast.Pass()]
            elif isinstance(item, ast.ClassDef):
                blank(item.body)

    blank(tree.body)
    return ast.dump(tree)


def decide(
    guarded_text: str | None,
    changes: list[Change],
    read_base: Callable[[str], str | None],
    read_head: Callable[[str], str | None],
) -> Decision:
    """Light or full for one pull request (#4399): the first rule that fails names the reason.

    ``guarded_text`` is ``guarded.json`` at the base branch's tip (``None`` when missing).
    ``changes`` is the pull request's own change, merge base to head. ``read_base`` and
    ``read_head`` read a file at the merge base and at the head (``None`` when missing). Any
    error is full review.
    """
    try:
        return _decide(guarded_text, changes, read_base, read_head)
    except Exception as exc:  # noqa: BLE001 - any error while deciding means full review
        return Decision(False, f"the decision failed: {exc}"[:200])


def _guarded_paths(guarded_text: str | None) -> set[str]:
    """The files ``guarded_text`` lists functions in, to read on both sides; none when it does
    not parse (``decide`` then says so)."""
    try:
        return {str(path) for path in json.loads(guarded_text or "")["functions"]}
    except (ValueError, KeyError, TypeError, AttributeError):
        return set()


def _decide(
    guarded_text: str | None,
    changes: list[Change],
    read_base: Callable[[str], str | None],
    read_head: Callable[[str], str | None],
) -> Decision:
    if guarded_text is None:
        return Decision(False, "the guarded list is missing at the base branch's tip")
    try:
        guarded = json.loads(guarded_text)
        files = [str(entry) for entry in guarded["files"]]
        functions = {
            str(path): [str(n) for n in names] for path, names in guarded["functions"].items()
        }
        safety = frozenset(str(name) for name in guarded.get("safety_tests") or ())
    except (ValueError, KeyError, TypeError, AttributeError):
        return Decision(False, "the guarded list at the base branch's tip does not parse")
    if not changes:
        return Decision(False, "the pull request changes nothing")
    paths = [path for change in changes for path in (change.path, change.old) if path]
    for path in paths:
        entry = _covered(path, files)
        if entry:
            return Decision(
                False, f"it changes {path}, which the guarded list covers ({entry})", kind=GUARDED
            )
    for path in paths:
        if not _allowed(path):
            return Decision(False, f"it changes {path}, which is not Python, docs/ or README.md")
    for path in paths:
        if PurePosixPath(path).name == "conftest.py":
            return Decision(False, f"it changes {path}")
    for change in changes:
        if change.path.endswith(".py") or change.old.endswith(".py"):
            if change.status in "ADRC":
                what = {"A": "adds", "D": "deletes", "R": "renames", "C": "copies"}[change.status]
                return Decision(False, f"it {what} the Python file {change.path}")
    changed_py = [change.path for change in changes if change.path.endswith(".py")]
    # Every guarded function is read on both sides, untouched files too (#4401 review): a
    # stale name in the list is full review, never light unchecked.
    for path, names in sorted(functions.items()):
        base, head = read_base(path), read_head(path)
        if base is None or head is None:
            return Decision(False, f"{path} is missing on one side", kind=GUARDED)
        base_tree, head_tree = ast.parse(base, path), _parse(head, path)
        if head_tree is None:
            return Decision(False, f"{path} does not parse", kind=GUARDED)
        for name in names:
            before, after = _find(base_tree, name), _find(head_tree, name)
            if before is None or after is None:
                return Decision(
                    False, f"the guarded {path}:{name} is missing on one side", kind=GUARDED
                )
            if ast.dump(before) != ast.dump(after):
                return Decision(False, f"it changes the guarded {path}:{name}", kind=GUARDED)
    for path in changed_py:
        if not _is_test(path):
            continue
        base, head = read_base(path), read_head(path)
        if base is None:
            return Decision(False, f"the test file {path} is new")
        if head is None:
            return Decision(False, f"{path} is missing at the head")
        head_tree = _parse(head, path)
        if head_tree is None:
            return Decision(False, f"{path} does not parse")
        if _blank_tests(ast.parse(base, path), safety) != _blank_tests(head_tree, safety):
            return Decision(
                False,
                f"{path} changes more than the bodies of tests the guarded list does not name"
                " as safety tests",
            )
    for path in changed_py:
        head = read_head(path)
        if head is None:
            # A changed Python file read as absent is never an empty module (#4401 review).
            return Decision(False, f"{path} is missing at the head")
        if _parse(head, path) is None:
            return Decision(False, f"{path} does not parse")
    return Decision(True)


def _parse(text: str, path: str) -> ast.Module | None:
    try:
        return ast.parse(text, path)
    except (SyntaxError, ValueError):
        return None


# --- the bench's answer -------------------------------------------------------------------

_WORSE = re.compile(r"^worse: (.+?) failed twice in a row on ", re.M)
_PENDING_CASES = re.compile(r"^pending: re-run on \S+: (.+)$", re.M)
_PENDING = re.compile(r"^pending: .*$", re.M)


@dataclass(frozen=True)
class Answer:
    verdict: str  # PASS, WORSE, PENDING or UNRUN
    cases: tuple[str, ...] = ()
    reason: str = ""
    base_tip: str = ""


def parse_bench(code: int | None, out: str, err: str) -> Answer:
    """``bin/bench verdict``'s exit code and output as an answer (robot#72).

    0 is no worse; 4 worse, with the cases it names (none named counts as could not run); 3
    pending, with the cases to re-run, or ``master`` when master has no finished run yet;
    anything else could not run.
    """
    if code == 0:
        return Answer(PASS)
    if code == 4:
        found = _WORSE.search(out)
        cases = tuple(c.strip() for c in found.group(1).split(",") if c.strip()) if found else ()
        if cases:
            return Answer(WORSE, cases)
        return Answer(UNRUN, reason="the bench said worse but named no case")
    if code == 3:
        found = _PENDING_CASES.search(out)
        if found:
            cases = tuple(re.findall(r"--case (\S+)", found.group(1)))
            if cases:
                return Answer(PENDING, cases, found.group(0))
        line = _PENDING.search(out)
        return Answer(PENDING, ("master",), line.group(0) if line else "pending")
    last = (err.strip().splitlines() or out.strip().splitlines() or [""])[-1]
    why = last if code is not None else (last or "it did not finish")
    return Answer(UNRUN, reason=(why or f"bin/bench exited {code}")[:300])


# --- the stored row -----------------------------------------------------------------------


def _short(sha: str) -> str:
    return sha[:12]


def _heads(record: dict) -> dict:
    return record.get("heads") or {}


def newest_head(record: dict) -> str:
    """The newest head with a decision, "" for none."""
    decided = {sha: entry for sha, entry in _heads(record).items() if entry.get("decision")}
    if not decided:
        return ""
    return max(decided, key=lambda sha: float(decided[sha].get("seen_at") or 0))


def full_for_good(record: dict, reviewed: bool) -> str:
    """Why the pull request takes a full review for good, "" when it does not (rule 2).

    Worked out from records written once: a review asked for or run (``reviewed``), a head
    the guarded list or the merge limit sent to full review, a light head the merge limit
    overtook (``past_limit``), or a worse verdict. Any other full decision makes only its
    own head full.
    """
    if reviewed:
        return "a full review was asked for or ran"
    if record.get("past_limit"):
        # A light head the review point overtook before it merged: the pull request's own
        # fact, beside its heads' decisions, which stay as written.
        return str(record["past_limit"])
    for sha, entry in sorted(_heads(record).items(), key=lambda kv: kv[1].get("seen_at") or 0):
        if entry.get("decision") == FULL and entry.get("kind") in (GUARDED, LIMIT):
            return str(entry.get("rule") or "full review")
        if entry.get("verdict") == WORSE:
            cases = ", ".join(entry.get("cases") or [])
            return f"the bench said worse at {_short(sha)} ({cases})"
    return ""


def _owed_verdict(item: dict) -> str:
    """The verdict an owed message's id names: ``light-review:<pull>@<head>:<verdict>…``."""
    _pull, _, rest = str(item.get("id") or "").partition("@")
    parts = rest.split(":")
    return parts[1] if len(parts) > 1 else ""


def light_head(record: dict, head: str, reviewed: bool) -> bool:
    entry = _heads(record).get(head) or {}
    return entry.get("decision") == LIGHT and not full_for_good(record, reviewed)


def head_state(entry: dict) -> str:
    """``running``, the verdict, the last attempt's answer, or "" for none yet."""
    if entry.get("running"):
        return "running"
    return str(entry.get("verdict") or (entry.get("attempt") or {}).get("verdict") or "")


def head_cases(entry: dict) -> list[str]:
    if entry.get("verdict"):
        return list(entry.get("cases") or [])
    return list((entry.get("attempt") or {}).get("cases") or [])


# --- the service --------------------------------------------------------------------------


@dataclass
class LightPorts:
    """What the service reads and writes outside itself; ``work_services`` wires the real ones."""

    #: ``owner/repo`` -> ``(guarded path, limit, checkout)``, or ``None`` where it is off.
    policy: Callable[[str], tuple[str, int, str] | None]
    #: The control database (``db.AgentDeckDB``'s ``light_review`` methods).
    store: Any
    #: A pull request's review events (``state.review_events``).
    reviews: Callable[[str], Iterable[review_history.ReviewEvent]]
    #: The live head of an open pull request, "" when it is not open, ``None`` when unread.
    live_head: Callable[[str], Awaitable[str | None]]
    #: The base branch a pull request merges into, "" when unread.
    base_branch: Callable[[str], Awaitable[str]]
    #: The review digest of a pull request's diff at a head ("" when unread).
    digest: Callable[[str, str], Awaitable[str]]
    #: The chat that owns a Work (``github_issue:owner/repo#n``).
    chat: Callable[[str], str | None]
    #: ``(chat, message id, text)``: the release's at-most-once scheduled message.
    tell: Callable[[str, str, str], bool | None]
    #: Queue one release look at a Work (``WorkReleaseService.queue_bench_look``).
    look: Callable[[str], None] = lambda work_id: None
    #: ``(checkout, base branch, number)`` -> ``(base tip, pull head)`` after a fetch.
    fetch: Callable[[str, str, int], Awaitable[tuple[str, str]]] | None = None
    #: ``(checkout, args)`` -> git's stdout; raises when git fails.
    git: Callable[[str, list[str]], Awaitable[str]] | None = None
    #: ``(checkout, base tip, head)`` -> ``(exit code or None on timeout, stdout, stderr)``;
    #: raises when no checkout could be made.
    bench: Callable[[str, str, str], Awaitable[tuple[int | None, str, str]]] | None = None
    #: ``(repository, title)`` -> an open issue's URL, "" for none, ``None`` when unread.
    find_issue: Callable[[str, str], Awaitable[str | None]] | None = None
    #: ``(repository, title, body)`` -> the filed issue's URL, "" when not filed.
    file_issue: Callable[[str, str, str], Awaitable[str]] | None = None
    #: Did a pull request merge? ``None`` when unread.
    merged: Callable[[str], Awaitable[bool | None]] | None = None
    clock: Callable[[], float] = time.time
    #: Run a coroutine function after a delay; returns something with ``cancel()``.
    later: Callable[[float, Callable[[], Awaitable[object]]], Any] | None = None


def _repository(pull: str) -> str:
    return pull.partition("#")[0].lower()


def _number(pull: str) -> int:
    return int(pull.rpartition("#")[2])


class BenchLine:
    """One bench at a time on this host (rule 7), the first in a merge queue first.

    A waiting list under one condition: when the running bench ends, the lowest ticket goes,
    ``(priority, order asked)``. ``FIRST`` is the release's bench of a head it made for the
    first in line; every other bench is ``NEXT``.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._busy = False
        self._waiting: list[tuple[int, int]] = []
        self._order = itertools.count()

    @contextlib.asynccontextmanager
    async def turn(self, priority: int):  # noqa: ANN201 - an async context manager
        ticket = (priority, next(self._order))
        async with self._cond:
            self._waiting.append(ticket)
            try:
                await self._cond.wait_for(lambda: not self._busy and min(self._waiting) == ticket)
            finally:
                self._waiting.remove(ticket)
                self._cond.notify_all()
            self._busy = True
        try:
            yield
        finally:
            async with self._cond:
                self._busy = False
                self._cond.notify_all()


class LightReview:
    """The rows, bench runs and messages of lighter review (#4399)."""

    def __init__(self, ports: LightPorts) -> None:
        self.ports = ports
        self._locks: dict[str, asyncio.Lock] = {}
        #: The bench task of each ``pull@head`` in this process.
        self._runs: dict[str, asyncio.Task] = {}
        self._timers: dict[str, Any] = {}
        self._tasks: set[asyncio.Task] = set()
        #: ``(repository, limit)`` -> the lock one review-point filing holds.
        self._point_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._line = BenchLine()
        #: Every fetch into a checkout's ``refs/agentdeck/light/`` holds it (rule 7).
        self._fetch_lock = asyncio.Lock()

    # --- reads, synchronous and local ---

    def handles(self, repository: str) -> bool:
        return self.ports.policy(repository.lower()) is not None

    def _load(self, pull: str) -> dict:
        found = self.ports.store.light_review_row(pull)
        record = dict(found) if found else {}
        record.setdefault("pull", pull)
        record.setdefault("heads", {})
        record.setdefault("owed", [])
        record.setdefault("told", [])
        return record

    def _reviewed(self, pull: str) -> bool:
        return any(event.kind in STICKY_KINDS for event in self.ports.reviews(pull))

    def full_why(self, pull: str) -> str:
        """Why ``pull`` takes a full review for good, "" when it does not."""
        pull = pull.lower()
        return full_for_good(self._load(pull), self._reviewed(pull))

    def is_light(self, pull: str, head: str) -> bool:
        """Is ``head`` decided light, with nothing making the pull request full for good?"""
        pull = pull.lower()
        if not self.handles(_repository(pull)):
            return False
        try:
            return light_head(self._load(pull), head, self._reviewed(pull))
        except Exception:  # noqa: BLE001 - unread: not light, so a review may start
            return False

    def head_moving(self, pull: str, head: str) -> bool:
        """The router's backstop question: light, or a head the release made from a light head
        and has not decided yet. One local read; never GitHub."""
        pull = pull.lower()
        if self.is_light(pull, head):
            return True
        try:
            record = self._load(pull)
            reviewed = self._reviewed(pull)
        except Exception:  # noqa: BLE001
            return False
        made_from = str(record.get("update_from") or "")
        return (
            bool(made_from)
            and head not in _heads(record)
            and newest_head(record) == made_from
            and light_head(record, made_from, reviewed)
        )

    def view(self, pull: str, head: str) -> dict | None:
        """The Work page's line for ``pull`` at ``head``, or ``None`` when nothing applies."""
        pull = pull.lower()
        if not self.handles(_repository(pull)):
            return None
        try:
            return line_for(self._load(pull), head, self._reviewed(pull))
        except Exception:  # noqa: BLE001
            return None

    def answer_at(self, pull: str, head: str) -> dict:
        try:
            return dict(self._load(pull.lower())["heads"].get(head) or {})
        except Exception:  # noqa: BLE001
            return {}

    # --- writes ---

    def _lock(self, pull: str) -> asyncio.Lock:
        return self._locks.setdefault(pull, asyncio.Lock())

    def _put(self, pull: str, record: dict, *, merged: bool | None = None) -> bool:
        heads = _heads(record)
        if len(heads) > KEEP_HEADS:
            keep = sorted(heads, key=lambda sha: float(heads[sha].get("seen_at") or 0))
            kept = set(keep[-KEEP_HEADS:]) | {
                sha
                for sha, entry in heads.items()
                if entry.get("running")
                or entry.get("verdict") == WORSE
                or entry.get("kind") in (GUARDED, LIMIT)
            }
            record["heads"] = {sha: heads[sha] for sha in heads if sha in kept}
        record["told"] = list(record.get("told") or [])[-200:]
        return bool(
            self.ports.store.put_light_review(pull, _repository(pull), record, merged=merged)
        )

    def _alive(self, pull: str, head: str, running: dict) -> bool:
        task = self._runs.get(f"{pull}@{head}")
        return running.get("boot") == BOOT and task is not None and not task.done()

    def _reap(self, pull: str, record: dict) -> None:
        """A running marker with no live bench task here is a bench that died (rule 1): from
        another boot it was cut off by a restart. It counts as could not run, under the one
        retry rule. Under the caller's lock, in its write."""
        for head, entry in _heads(record).items():
            running = entry.get("running")
            if not running or self._alive(pull, head, running):
                continue
            why = (
                "the deck restarted during the bench"
                if running.get("boot") != BOOT
                else "the deck's bench task ended with no answer"
            )
            self._record(pull, record, head, Answer(UNRUN, reason=why), running)

    def _record(self, pull: str, record: dict, head: str, answer: Answer, ran: dict) -> None:
        """One bench answer, its message and the cleared marker, in the caller's write."""
        now = self.ports.clock()
        entry = _heads(record).setdefault(head, {"seen_at": now})
        entry.pop("running", None)
        round_ = int(ran.get("round") or 0)
        if answer.verdict in (PASS, WORSE):
            if not entry.get("verdict"):
                # Written once (rule 1): no bench runs on a head with a verdict again.
                entry.update(verdict=answer.verdict, cases=list(answer.cases), answered_at=now)
                entry["answer_base"] = answer.base_tip
            entry.pop("attempt", None)
            self._cancel_retry(pull, head, entry)
            # A settled verdict supersedes its head's undelivered attempt notices.
            record["owed"] = [
                item
                for item in record["owed"]
                if item.get("head") != head or _owed_verdict(item) not in (PENDING, UNRUN)
            ]
        else:
            entry["attempt"] = {
                "verdict": answer.verdict,
                "cases": list(answer.cases),
                "reason": answer.reason,
                "at": now,
            }
        if full_for_good(record, self._reviewed(pull)) and answer.verdict != WORSE:
            return  # a full review started meanwhile: stored for the page, nothing to tell
        key = f"light-review:{pull}@{head}:{answer.verdict}"
        if answer.verdict == UNRUN:
            if not entry.get("retried") and not entry.get("retry_at"):
                # The one automatic retry, kept across restarts (rule 1).
                entry["retry_at"] = now + RETRY_S
                self._arm_retry(pull, head, float(entry["retry_at"]))
                text = answer_text(pull, head, answer, retry_left=True)
                self._owe(record, f"{key}:first:r{round_}", head, text)
                return
            text = answer_text(pull, head, answer, retry_left=False)
            self._owe(record, f"{key}:last:r{round_}", head, text)
            return
        if answer.cases:
            key += ":" + ",".join(sorted(answer.cases))
        if answer.verdict != PASS:
            # One message per head and case set; only a pass is told again on each call.
            self._owe(record, key, head, answer_text(pull, head, answer))
            return
        self._owe(record, f"{key}:r{round_}", head, answer_text(pull, head, answer))

    def _owe(self, record: dict, key: str, head: str, text: str) -> None:
        if key in record["told"] or any(item["id"] == key for item in record["owed"]):
            return
        record["owed"].append({"id": key, "head": head, "text": text})

    def _mark_running(self, entry: dict, round_: int, *, first: bool) -> dict:
        """The in-flight marker (rule 1), written in the caller's write before the bench."""
        ran = {"boot": BOOT, "at": self.ports.clock(), "round": round_, "first": first}
        entry["running"] = ran
        return ran

    def _start(self, pull: str, head: str, ran: dict) -> None:
        task = asyncio.get_running_loop().create_task(self._run(pull, head, ran))
        self._runs[f"{pull}@{head}"] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, pull: str, head: str, ran: dict) -> None:
        """One bench run and its answer."""
        try:
            answer = await self._bench(pull, head, first=bool(ran.get("first")))
        except asyncio.CancelledError:
            raise  # its marker is reaped as could not run
        except Exception as exc:  # noqa: BLE001 - no answer is not a pass
            log.warning("light-review: the bench of %s failed", pull, exc_info=True)
            answer = Answer(UNRUN, reason=f"the deck's bench task failed: {exc}"[:300])
        async with self._lock(pull):
            self._runs.pop(f"{pull}@{head}", None)
            record = self._load(pull)
            entry = _heads(record).get(head) or {}
            if (entry.get("running") or {}).get("at") != ran.get("at"):
                return  # not this run's marker any more
            self._record(pull, record, head, answer, ran)
            if not self._put(pull, record):
                log.warning("light-review: the answer for %s at %s not stored", pull, head[:12])
                return
        await self._send_owed(pull)
        self._queue_look(pull)

    async def _fetch(self, checkout: str, base: str, number: int) -> tuple[str, str]:
        if self.ports.fetch is None:
            raise LightError("no fetch on this host")
        async with self._fetch_lock:
            return await self.ports.fetch(checkout, base, number)

    async def _bench(self, pull: str, head: str, *, first: bool) -> Answer:
        policy = self.ports.policy(_repository(pull))
        if policy is None or not policy[2] or self.ports.bench is None:
            return Answer(UNRUN, reason="no checkout of the repository on this host")
        checkout = policy[2]
        base = await self.ports.base_branch(pull)
        if not base:
            return Answer(UNRUN, reason="the pull request's base branch could not be read")
        async with self._line.turn(FIRST if first else NEXT):
            try:
                try:
                    tip, fetched = await self._fetch(checkout, base, _number(pull))
                except Exception:  # noqa: BLE001 - tried once more at once (rule 7)
                    tip, fetched = await self._fetch(checkout, base, _number(pull))
            except Exception as exc:  # noqa: BLE001
                return Answer(UNRUN, reason=f"the fetch failed: {exc}"[:300])
            if fetched != head:
                return Answer(UNRUN, reason=f"the head moved to {_short(fetched)}")
            try:
                code, out, err = await self.ports.bench(checkout, tip, head)
            except Exception as exc:  # noqa: BLE001 - no checkout made is not a pass
                return Answer(UNRUN, reason=f"no checkout to run the bench in: {exc}"[:300])
        answer = parse_bench(code, out, err)
        if code is None:
            answer = Answer(UNRUN, reason=f"the bench ran past {int(BENCH_TIMEOUT_S)} s")
        return Answer(answer.verdict, answer.cases, answer.reason, tip)

    async def _decide_git(self, pull: str, head: str) -> tuple[Decision, str]:
        """``decide`` from git in the repository's checkout; ``(decision, base tip)``."""
        policy = self.ports.policy(_repository(pull))
        if policy is None:
            return Decision(False, "lighter review is off here"), ""
        path, _limit, checkout = policy
        if not checkout or self.ports.fetch is None or self.ports.git is None:
            return Decision(False, "no checkout of the repository on this host", True), ""
        try:
            base = await self.ports.base_branch(pull)
            if not base:
                why = "the pull request's base branch could not be read"
                return Decision(False, why, True), ""
            tip, fetched = await self._fetch(checkout, base, _number(pull))
            if fetched != head:
                why = f"the pull request's head is {_short(fetched)} now"
                return Decision(False, why, True), tip
            git = self.ports.git
            merge_base = (await git(checkout, ["merge-base", tip, head])).strip()
            changes = parse_changes(
                await git(checkout, ["diff", "--name-status", "-M", "-z", merge_base, head])
            )

            async def blobs(sha: str, names: set[str]) -> dict[str, str]:
                """The files of ``names`` at ``sha``: ``ls-tree`` alone says a file is
                missing, and any read that fails raises, so the read is unread and never
                taken for a missing or empty file."""
                if not names:
                    return {}
                listed = await git(checkout, ["ls-tree", "-r", "-z", "--name-only", sha, "--"])
                present = {name for name in listed.split("\0") if name} & names
                return {
                    name: await git(checkout, ["show", f"{sha}:{name}"]) for name in sorted(present)
                }

            guarded = (await blobs(tip, {path})).get(path)
            names = {c.path for c in changes} | {c.old for c in changes if c.old}
            names |= _guarded_paths(guarded)
            base_text = await blobs(merge_base, names)
            head_text = await blobs(head, names)
        except Exception as exc:  # noqa: BLE001 - unread: nothing decided
            log.warning("light-review: %s at %s not decided", pull, head[:12], exc_info=True)
            return Decision(False, f"git could not be read: {exc}"[:300], True), ""
        return decide(guarded, changes, base_text.get, head_text.get), tip

    async def _deck_made(self, pull: str, head: str, made_from: set[str]) -> bool | None:
        """Is ``head`` GitHub's update commit on top of a head the deck updated from (rule 6)?
        ``None`` when git could not be read."""
        if not made_from:
            return False
        policy = self.ports.policy(_repository(pull))
        if policy is None or not policy[2] or self.ports.git is None:
            return None
        checkout, git = policy[2], self.ports.git
        try:
            base = await self.ports.base_branch(pull)
            if not base:
                return None
            tip, fetched = await self._fetch(checkout, base, _number(pull))
            if fetched != head:
                return None
            parents = (await git(checkout, ["rev-list", "--parents", "-n", "1", head])).split()
            if len(parents) != 3 or parents[1].lower() not in made_from:
                return False
            committer = (await git(checkout, ["log", "-1", "--format=%cn <%ce>", head])).strip()
            if committer != GITHUB_COMMITTER:
                return False
            # The second parent is on the base branch: nothing of it is missing from the tip.
            left = await git(checkout, ["rev-list", "--count", parents[2], f"^{tip}"])
            return left.strip() == "0"
        except Exception:  # noqa: BLE001 - unread: looked at again
            log.warning("light-review: %s at %s unproven", pull, head[:12], exc_info=True)
            return None

    # --- the review point ---

    async def _limit(self, pull: str, work_id: str) -> str:
        """The review point's rule when the limit is reached, else ""; files it then."""
        repository = _repository(pull)
        policy = self.ports.policy(repository)
        if policy is None:
            return ""
        limit = policy[1]
        count, pulls = await self._merged_light(repository)
        if count < limit:
            return ""
        await self._file_point(repository, limit, pulls, work_id)
        return f"the review point of {limit} light merges is reached"

    async def _merged_light(self, repository: str) -> tuple[int, list[str]]:
        """Merged pull requests whose last head is light with a pass (worked out, rule 2)."""
        pulls = []
        for pull, record, merged in self.ports.store.light_review_rows(repository):
            if record.get("merged_light"):
                # The deck's own light merge: counted even if a review was asked after it.
                pulls.append(pull)
                continue
            last = newest_head(record)
            entry = _heads(record).get(last) or {}
            if entry.get("decision") != LIGHT or entry.get("verdict") != PASS:
                continue
            if full_for_good(record, self._reviewed(pull)):
                continue
            if not merged and self.ports.merged is not None:
                with contextlib.suppress(Exception):
                    if await self.ports.merged(pull):
                        # A cache of GitHub's merged state, which never goes back.
                        self.ports.store.set_light_review_merged(pull)
                        merged = True
            if merged:
                pulls.append(pull)
        return len(pulls), sorted(pulls)

    async def _file_point(
        self, repository: str, limit: int, pulls: list[str], work_id: str
    ) -> None:
        """File the review point's issue once per limit, in the Work's issue repository, with
        no labels, after looking for an open one of the same title."""
        store = self.ports.store
        find, file = self.ports.find_issue, self.ports.file_issue
        if find is None or file is None:
            return
        lock = self._point_locks.setdefault((repository, limit), asyncio.Lock())
        async with lock:
            if store.light_review_point(repository, limit) is not None:
                return
            issues = work_id.partition(":")[2].rpartition("#")[0] or repository
            title = f"Lighter review of {repository}: review point of {limit} light merges reached"
            found = await find(issues, title)
            if found is None:
                return  # unread: looked at again on the next decision
            if not found:
                body = (
                    f"{len(pulls)} pull requests in {repository} merged with lighter review (no"
                    " reviewer; a green `bin/test.sh` and a bench verdict of no worse). The deck"
                    " stopped making light decisions there. Review them, then raise"
                    " `light_review_limit` in the manifest to resume, or leave it to keep full"
                    " review (agentdeck#4399).\n\n" + "\n".join(f"- {pull}" for pull in pulls)
                )
                found = await file(issues, title, body)
            if found:
                store.note_light_review_point(repository, limit, found)

    # --- decisions ---

    async def _decision(self, pull: str, head: str, work_id: str) -> tuple[Decision, str, str]:
        """A new decision for ``head``: ``(decision, base tip, digest)``."""
        rule = await self._limit(pull, work_id)
        if rule:
            return Decision(False, rule, kind=LIMIT), "", ""
        decision, tip = await self._decide_git(pull, head)
        digest = ""
        if decision.light:
            with contextlib.suppress(Exception):
                digest = await self.ports.digest(pull, head)
        return decision, tip, digest

    @staticmethod
    def _write_decision(entry: dict, decision: Decision, tip: str, digest: str) -> Decision:
        """Written once (rule 1): an earlier writer's decision stands, and is answered."""
        if entry.get("decision"):
            return Decision(
                entry["decision"] == LIGHT, str(entry.get("rule") or ""), kind=entry.get("kind")
            )
        entry.update(
            decision=LIGHT if decision.light else FULL,
            rule=decision.rule,
            kind=decision.kind or ("" if decision.light else OTHER),
            base_tip=tip,
        )
        if digest:
            entry["digest"] = digest
        return decision

    async def checklist(self, pull: str, head: str, work_id: str) -> Decision | None:
        """``pre-review-done``'s decision for ``head``: ``None`` where lighter review is off.

        Raises ``LightError`` when git could not be read (nothing is written, no review
        starts) or the decision could not be stored. A light head with no verdict and no bench
        running starts its bench; one with a pass is told it again."""
        pull = pull.lower()
        if not self.handles(_repository(pull)):
            return None
        why = self.full_why(pull)
        if why:
            return Decision(False, f"it takes a full review: {why}")
        entry = self._load(pull)["heads"].get(head) or {}
        tip = digest = ""
        if entry.get("decision"):
            decision = Decision(entry["decision"] == LIGHT, str(entry.get("rule") or ""))
            if decision.light:
                # Decided light before the review point was reached: it stays written,
                # and the limit sends the pull request to full review for good.
                rule = await self._limit(pull, work_id)
                if rule:
                    await self._overtaken(pull, rule)
                    return Decision(False, f"it takes a full review: {rule}", kind=LIMIT)
        else:
            decision, tip, digest = await self._decision(pull, head, work_id)
            if decision.unread:
                raise LightError(
                    f"{pull}'s diff at {_short(head)} could not be read ({decision.rule}), so"
                    " nothing was decided and no review started; call pre-review-done again"
                )
        start = None
        async with self._lock(pull):
            record = self._load(pull)
            self._reap(pull, record)
            record["work"] = work_id
            entry = record["heads"].setdefault(head, {"seen_at": self.ports.clock()})
            decision = self._write_decision(entry, decision, tip, digest)
            why = full_for_good(record, self._reviewed(pull))
            if decision.light and not why:
                round_ = int(record.get("round") or 0) + 1
                record["round"] = round_
                if entry.get("verdict") == PASS:
                    text = answer_text(pull, head, Answer(PASS))
                    self._owe(record, f"light-review:{pull}@{head}:pass:r{round_}", head, text)
                elif not entry.get("verdict") and not entry.get("running"):
                    # Pending and could not run rerun here only (rule 1).
                    self._cancel_retry(pull, head, entry)
                    start = self._mark_running(entry, round_, first=False)
            if not self._put(pull, record):
                raise LightError(f"the decision for {pull} at {_short(head)} could not be stored")
        if start is not None:
            self._start(pull, head, start)
        await self._send_owed(pull)
        if decision.light and why:
            return Decision(False, f"it takes a full review: {why}")
        return decision

    # --- the release's questions ---

    async def look(self, pull: str, head: str, updated: str = "") -> dict | None:
        """The release's look at an open pull request's live ``head``.

        ``None`` takes the reviewed path: lighter review is off, the pull request is full for
        good, it never had a light head, or ``head`` is decided full. Otherwise
        ``{"verdict": ..., "cases": [...]}``, where the verdict is ``pass``, ``worse``,
        ``pending``, ``unrun``, ``running``, ``agent`` (a head the deck did not make, waiting
        for ``pre-review-done``, rule 6) or ``unread``. ``updated`` is the release's own record
        of the head it updated from.
        """
        pull = pull.lower()
        if not self.handles(_repository(pull)):
            return None
        record = self._load(pull)
        if full_for_good(record, self._reviewed(pull)):
            return None
        entry = record["heads"].get(head) or {}
        if not entry.get("decision"):
            last = newest_head(record)
            if not last or record["heads"][last].get("decision") != LIGHT:
                return None  # never light: its review holds it, as anywhere else
            entry = await self._new_head(pull, head, record, updated)
            if "verdict" in entry:
                return entry
        if entry.get("decision") != LIGHT:
            return None
        async with self._lock(pull):
            record = self._load(pull)
            self._reap(pull, record)
            entry = record["heads"].get(head) or {}
            if not self._put(pull, record):
                return {"verdict": "unread", "cases": []}
        await self._send_owed(pull)
        state = head_state(entry)
        if state == PASS:
            rule = await self._limit(pull, str(record.get("work") or ""))
            if rule:
                return await self._past_limit(pull, head, rule)
        return {"verdict": state, "cases": head_cases(entry)}

    async def _overtaken(self, pull: str, rule: str) -> None:
        """The review point overtook a light head before it merged: the pull request takes
        a full review for good (``full_for_good``), so the router starts one and the release
        holds for it. Written once; the heads' decisions are left as written."""
        async with self._lock(pull):
            record = self._load(pull)
            if not record.get("past_limit"):
                record["past_limit"] = rule
                if not self._put(pull, record):
                    raise LightError(f"the review point for {pull} could not be stored")

    async def _past_limit(self, pull: str, head: str, rule: str) -> dict:
        """A light pass that waited while the review point was reached: it does not merge
        light, and its agent is told once to ask for the full review."""
        await self._overtaken(pull, rule)
        async with self._lock(pull):
            record = self._load(pull)
            self._owe(
                record,
                f"light-review:{pull}@{head}:limit",
                head,
                f"AgentDeck: {pull} at {_short(head)} no longer merges light: {rule}. Call"
                " pre-review-done; it starts a full review.",
            )
            self._put(pull, record)
        await self._send_owed(pull)
        return {"verdict": "limit", "cases": []}

    async def _new_head(self, pull: str, head: str, record: dict, updated: str) -> dict:
        """A head with no decision after a light one (rule 6): decided and benched here only
        when the deck made it; else held for the agent's ``pre-review-done``."""
        made_from = {sha.lower() for sha in (record.get("update_from"), updated) if sha}
        made = await self._deck_made(pull, head, made_from)
        if made is None:
            return {"verdict": "unread", "cases": [], "why": "the new head could not be read"}
        if not made:
            async with self._lock(pull):
                record = self._load(pull)
                self._owe(
                    record,
                    f"light-review:{pull}@{head}:agent",
                    head,
                    f"AgentDeck: {pull} moved to {_short(head)}, which the deck did not make;"
                    " call pre-review-done when it is ready. Until then it does not merge.",
                )
                self._put(pull, record)
            await self._send_owed(pull)
            return {"verdict": "agent", "cases": []}
        decision, tip, digest = await self._decision(pull, head, str(record.get("work") or ""))
        if decision.unread:
            return {"verdict": "unread", "cases": [], "why": decision.rule}
        start = None
        async with self._lock(pull):
            record = self._load(pull)
            entry = record["heads"].setdefault(head, {"seen_at": self.ports.clock()})
            decision = self._write_decision(entry, decision, tip, digest)
            if decision.light:
                if not (entry.get("verdict") or entry.get("running") or entry.get("attempt")):
                    start = self._mark_running(entry, 0, first=True)
            else:
                self._owe(
                    record,
                    f"light-review:{pull}@{head}:full",
                    head,
                    f"AgentDeck: the deck brought {pull} up to its base, and at {_short(head)}"
                    f" it takes a full review ({decision.rule}). Call pre-review-done for it.",
                )
            if not self._put(pull, record):
                return {"verdict": "unread", "cases": [], "why": "the decision was not stored"}
            entry = dict(entry)
        if start is not None:
            self._start(pull, head, start)
        await self._send_owed(pull)
        return entry

    async def updating(self, pull: str, head: str) -> bool:
        """The release is about to update ``pull`` from ``head`` (rule 6): saved first."""
        pull = pull.lower()
        async with self._lock(pull):
            record = self._load(pull)
            record["update_from"] = head
            return self._put(pull, record)

    async def before_merge(self, work_id: str, pull: str, head: str) -> tuple[bool, str]:
        """Right before a light merge (rule 3): ``(go, what holds it)``. The merge itself is
        pinned to ``head`` by GitHub's ``sha``."""
        pull = pull.lower()
        rule = await self._limit(pull, work_id)
        if rule:
            return False, f"{pull} does not merge light: {rule}"
        async with self._lock(pull):
            record = self._load(pull)
            why = full_for_good(record, self._reviewed(pull))
            entry = record["heads"].get(head) or {}
        if why:
            return False, f"{pull} takes a full review now ({why})"
        if entry.get("decision") != LIGHT:
            return False, f"{pull} is not decided light at {_short(head)}"
        if entry.get("running"):
            return False, f"{pull}'s bench runs at {_short(head)}"
        if entry.get("verdict") != PASS:
            return False, f"{pull} has no bench pass at {_short(head)}"
        return True, ""

    async def merged(self, pull: str, head: str = "") -> None:
        """``pull`` merged: count it now and file the review point when this merge reached
        the limit. ``head`` is given only by the deck's own light merge of that head, and is
        kept as the fact that it merged light, whatever review events come after."""
        pull = pull.lower()
        if not self.handles(_repository(pull)):
            return
        async with self._lock(pull):
            record = self._load(pull)
            if head and not record.get("merged_light"):
                record["merged_light"] = head
            self._put(pull, record, merged=True)
        with contextlib.suppress(Exception):
            await self._limit(pull, str(record.get("work") or ""))

    # --- messages, the retry timer ---

    async def _send_owed(self, pull: str) -> None:
        async with self._lock(pull):
            record = self._load(pull)
            owed = list(record["owed"])
            work = str(record.get("work") or "")
        if not owed:
            return
        chat = self.ports.chat(work) if work else None
        if not chat:
            return  # kept owed: sent once the Work has a chat
        sent = [item["id"] for item in owed if self.ports.tell(chat, item["id"], item["text"])]
        if not sent:
            return
        async with self._lock(pull):
            record = self._load(pull)
            record["owed"] = [item for item in record["owed"] if item["id"] not in sent]
            record["told"] = [*record["told"], *sent]
            self._put(pull, record)

    def _queue_look(self, pull: str) -> None:
        with contextlib.suppress(Exception):
            work = str(self._load(pull).get("work") or "")
            if work:
                self.ports.look(work)

    def _call_later(self, delay: float, fn: Callable[[], Awaitable[object]]) -> Any:
        if self.ports.later is not None:
            return self.ports.later(delay, fn)
        loop = asyncio.get_running_loop()

        def fire() -> None:
            task = loop.create_task(fn())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return loop.call_later(max(0.0, delay), fire)

    def _arm_retry(self, pull: str, head: str, at: float) -> None:
        key = f"{pull}@{head}"
        if key in self._timers:
            return

        async def fire() -> None:
            self._timers.pop(key, None)
            await self._retry(pull, head)

        self._timers[key] = self._call_later(max(0.0, float(at) - self.ports.clock()), fire)

    def _cancel_retry(self, pull: str, head: str, entry: dict) -> None:
        entry.pop("retry_at", None)
        timer = self._timers.pop(f"{pull}@{head}", None)
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.cancel()

    async def _retry(self, pull: str, head: str) -> None:
        """The one automatic retry of a could-not-run answer, out of the merge queue."""
        try:
            live = await self.ports.live_head(pull)
        except Exception:  # noqa: BLE001 - unread: the retry waits for a read
            live = None
        start = None
        async with self._lock(pull):
            record = self._load(pull)
            self._reap(pull, record)
            entry = record["heads"].get(head) or {}
            if not entry.get("retry_at") or entry.get("verdict") or entry.get("running"):
                return
            if live is None:
                # The retry is spent only on a head proven live: an outage keeps it.
                at = self.ports.clock() + LIVE_UNREAD_S
                entry["retry_at"] = at
                if self._put(pull, record):
                    self._arm_retry(pull, head, at)
                return
            entry.pop("retry_at", None)
            if live == head and light_head(record, head, self._reviewed(pull)):
                entry["retried"] = True
                start = self._mark_running(entry, 0, first=False)
            if not self._put(pull, record):
                return
        if start is not None:
            self._start(pull, head, start)

    # --- restart ---

    def start(self) -> None:
        task = asyncio.get_running_loop().create_task(self.resume())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def resume(self) -> None:
        """After a restart: a bench that died counts as could not run, a due retry is armed
        again, and what is owed is sent."""
        try:
            rows = self.ports.store.light_review_rows(None)
        except Exception:  # noqa: BLE001
            log.warning("light-review: rows unread at start", exc_info=True)
            return
        for pull, _record, merged in rows:
            if merged:
                continue
            try:
                async with self._lock(pull):
                    record = self._load(pull)
                    self._reap(pull, record)
                    for head, entry in record["heads"].items():
                        if entry.get("retry_at"):
                            self._arm_retry(pull, head, float(entry["retry_at"]))
                    self._put(pull, record)
                await self._send_owed(pull)
            except Exception:  # noqa: BLE001 - one row must not stop the rest
                log.warning("light-review: %s not resumed", pull, exc_info=True)

    async def stop(self) -> None:
        for handle in self._timers.values():
            with contextlib.suppress(Exception):
                handle.cancel()
        self._timers.clear()


def answer_text(pull: str, head: str, answer: Answer, *, retry_left: bool = False) -> str:
    at = f"{pull} at {_short(head)}"
    if answer.verdict == PASS:
        return (
            f"AgentDeck: the bench verdict for {at} is no worse than master. It merges once a"
            " run of exactly `bin/test.sh` is green there."
        )
    if answer.verdict == WORSE:
        return (
            f"AgentDeck: the bench verdict for {at} is worse than master:"
            f" {', '.join(answer.cases)} failed twice in a row. It now takes a full review for"
            " good and holds no place in the merge queue: call pre-review-done and a reviewer"
            " starts."
        )
    if answer.verdict == PENDING:
        if answer.cases == ("master",):
            return (
                f"AgentDeck: the bench verdict for {at} is pending: {answer.reason}. Once that"
                " run finishes, call pre-review-done again; until then it holds no place in the"
                " merge queue."
            )
        cases = " ".join(f"--case {case}" for case in answer.cases)
        return (
            f"AgentDeck: the bench verdict for {at} is pending: re-run `bin/bench run {cases}`"
            " on this head, then call pre-review-done again; until then it holds no place in"
            " the merge queue."
        )
    if retry_left:
        return (
            f"AgentDeck: the bench verdict for {at} could not run ({answer.reason}), so it"
            " holds no place in the merge queue. The deck tries once more in 15 minutes."
        )
    return (
        f"AgentDeck: the bench verdict for {at} could not run ({answer.reason}), and the"
        " deck's one retry is used. Call pre-review-done again once the bench works; that runs"
        " it again."
    )


def line_for(record: dict, head: str, reviewed: bool) -> dict | None:
    """The Work page's lighter-review line for ``head``."""
    why = full_for_good(record, reviewed)
    if why:
        return {"light": False, "text": f"Full review: {why}"}
    entry = _heads(record).get(head)
    if not entry or not entry.get("decision"):
        last = newest_head(record)
        if last and _heads(record)[last].get("decision") == LIGHT:
            return {
                "light": True,
                "text": f"Light review: {_short(head)} is a new head; its agent calls"
                " pre-review-done, unless the deck made it",
            }
        return None
    if entry.get("decision") != LIGHT:
        return {"light": False, "text": f"Full review: {entry.get('rule') or 'full review'}"}
    state = {
        PASS: "bench no worse",
        PENDING: "bench pending: " + ", ".join(head_cases(entry)),
        UNRUN: "bench could not run"
        + (
            ", one retry scheduled"
            if entry.get("retry_at")
            else ", the retry could not run either"
            if entry.get("retried")
            else ""
        ),
        "running": "bench running",
        "": "bench not run yet",
    }.get(head_state(entry), head_state(entry))
    return {
        "light": True,
        "text": "Light review: no reviewer; merges on a green `bin/test.sh` and a bench"
        f" verdict of no worse ({state})",
    }


# --- the real ports -----------------------------------------------------------------------


async def git_read(
    checkout: str, args: list[str], *, timeout: float = 120.0, grace: float = 0.0
) -> str:
    """Run git in ``checkout``; object reads and the deck's own fetch only. Raises when git
    fails. At ``timeout`` it is stopped: with a ``grace``, SIGTERM first, so a fetch can drop
    its ref locks, and SIGKILL only once the grace runs out (rule 7).

    GitHub is reached through the deck's own credential helper, never a prompt: the runtime
    has no ``gh`` login of its own, so a plain fetch over https failed with "unable to get
    password" and no light review could start (#4438)."""
    from .github_identity import github_git_environment

    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        os.path.expanduser(checkout),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=github_git_environment(),
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        if grace:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=grace)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise RuntimeError(f"git {args[0]} ran past {int(timeout)} s") from None
    if process.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace").strip()[:300] or f"git {args[0]} failed")
    return out.decode("utf-8", "replace")


async def clear_ref_locks(checkout: str) -> list[str]:
    """Remove the lock files a killed fetch left under ``refs/agentdeck/light/`` in the shared
    git directory (a linked worktree keeps its refs there). The caller holds the fetch lock, so
    no live fetch of the deck's owns them; each removal is logged."""
    common = (await git_read(checkout, ["rev-parse", "--git-common-dir"])).strip()
    root = os.path.join(os.path.expanduser(checkout), common, "refs", "agentdeck", "light")
    removed = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".lock"):
                path = os.path.join(folder, name)
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path)
                    removed.append(path)
                    log.warning("light-review: removed the stale ref lock %s", path)
    return removed


async def git_fetch(checkout: str, base: str, number: int) -> tuple[str, str]:
    """Fetch the base branch and the pull request's head into the deck's own refs, so the
    checkout's branches and working tree are never touched; ``(base tip, head)``. The service
    calls it under its one fetch lock."""
    base_ref = f"refs/agentdeck/light/base/{base}"
    pull_ref = f"refs/agentdeck/light/pull/{number}"
    await clear_ref_locks(checkout)
    await git_read(
        checkout,
        [
            "fetch",
            "-q",
            "--no-tags",
            "origin",
            f"+refs/heads/{base}:{base_ref}",
            f"+refs/pull/{number}/head:{pull_ref}",
        ],
        timeout=FETCH_TIMEOUT_S,
        grace=FETCH_GRACE_S,
    )
    tip = (await git_read(checkout, ["rev-parse", base_ref])).strip()
    head = (await git_read(checkout, ["rev-parse", pull_ref])).strip()
    return tip, head


async def run_bench(checkout: str, base_tip: str, head: str) -> tuple[int | None, str, str]:
    """``bin/bench verdict --head <head> <checkout>`` from a pinned clean checkout of the base
    tip: an argument list, no shell, killed with its process group at the limit."""
    from .jobs import BENCH_CHECKOUT_PREFIX, _pinned_checkout, _remove_pinned_checkout

    source = os.path.expanduser(checkout)
    pinned = await asyncio.to_thread(_pinned_checkout, source, base_tip, BENCH_CHECKOUT_PREFIX)
    try:
        process = await asyncio.create_subprocess_exec(
            os.path.join(pinned, "bin", "bench"),
            "verdict",
            "--head",
            head,
            pinned,
            cwd=pinned,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=BENCH_TIMEOUT_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await process.wait()
            return None, "", ""
        return (
            process.returncode,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )
    finally:
        await asyncio.to_thread(_remove_pinned_checkout, pinned)


async def _pull_view(pull: str) -> dict | None:
    # gh_cli straight, not review_refs: that one imports chat_review and work, and this
    # module is imported by every test through work_release (tests/test_select_suite.py).
    from . import gh_cli

    repository, _, number = pull.partition("#")
    try:
        code, stdout = await gh_cli.run(
            [
                "gh",
                "pr",
                "view",
                number,
                "--repo",
                repository,
                "--json",
                "state,headRefOid,baseRefName",
            ],
            timeout=20.0,
        )
    except gh_cli.GitHubRefusal as exc:
        log.warning("light review: %s unreadable: %s", pull, exc)
        return None
    if code != 0:
        return None
    try:
        value = json.loads(stdout or b"null")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def gh_live_head(pull: str) -> str | None:
    """An open pull request's head, "" when it is not open, ``None`` when unread."""
    facts = await _pull_view(pull)
    if facts is None:
        return None
    if str(facts.get("state") or "").upper() != "OPEN":
        return ""
    return str(facts.get("headRefOid") or "") or None


async def gh_base_branch(pull: str) -> str:
    facts = await _pull_view(pull)
    return str((facts or {}).get("baseRefName") or "")


async def gh_merged(pull: str) -> bool | None:
    facts = await _pull_view(pull)
    if facts is None:
        return None
    return str(facts.get("state") or "").upper() == "MERGED"


async def gh_find_issue(repository: str, title: str) -> str | None:
    """An open issue titled exactly ``title``: its URL, "" for none, ``None`` when unread."""
    from . import gh_cli

    argv = [
        "gh",
        "issue",
        "list",
        "--repo",
        repository,
        "--state",
        "open",
        "--search",
        f'"{title}" in:title',
        "--json",
        "title,url",
        "--limit",
        "20",
    ]
    try:
        code, out = await gh_cli.run(argv, timeout=60, repo=repository)
        found = json.loads(out or b"[]") if code == 0 else None
    except Exception:  # noqa: BLE001 - unread
        return None
    if not isinstance(found, list):
        return None
    for item in found:
        if isinstance(item, dict) and item.get("title") == title:
            return str(item.get("url") or "")
    return ""


async def gh_file_issue(repository: str, title: str, body: str) -> str:
    """File an issue with no labels, through the API so no ownership label is added."""
    from . import gh_cli

    argv = ["gh", "api", "-X", "POST", f"repos/{repository}/issues", "--input", "-"]
    payload = json.dumps({"title": title, "body": body, "labels": []}).encode("utf-8")
    try:
        code, out, _err = await gh_cli.run_once(argv, timeout=60, stdin=payload, repo=repository)
        answer = json.loads(out or b"{}") if code == 0 else {}
    except Exception:  # noqa: BLE001 - not filed: looked for again on the next decision
        log.warning("light-review: the review point issue in %s not filed", repository)
        return ""
    return str(answer.get("html_url") or "") if isinstance(answer, dict) else ""
