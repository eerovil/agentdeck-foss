"""A Work merges its own pull requests: the release, step 2 (#2895, ADR 0008).

Before this, every merge went through a Poller: the deck left a request file and the engine
read it on its next wake, checked the release bits on its card record, armed GitHub's
auto-merge and watched it, all on a 30-second timer. For every project (#3732) this module
merges in the web process instead, for any Work **no Poller card holds** (read live from the
Poller's board). A
Work the Poller carries finishes under the Poller, so the two never both merge one pull
request (D17).

**It merges directly, never arms** (operator decision, review round 7). A merge happens only at
a moment every condition holds at the exact head, as a merge pinned to that head (``sha``), so
GitHub refuses it if the head moved in between. Nothing is left armed that a later push, turn,
pause or label could make stale, so there is nothing to watch or call off.

The conditions, read on every wake (``_hold_reason``, in this order): no Poller card holds the
Work; it is not paused, on the deck or by its label; its issue carries no spec-only label; no
chat of it is mid-turn; no pull request is a draft; each open one has a review pass at its head
and green tests there (a verification job of the Work, green GitHub checks for a
``github_checks`` repository, or the review alone for a ``review_only`` one), and outside a
``review_only`` repository none of its GitHub checks still running (#3132). Then, per pull
request: it is up to date with its base (read as the Poller's ``classify_pr``, with
``behind_by``), does not conflict and has no red checks. ``auto_release`` merges at once; any
other project waits for the Work page's Merge press. ``review_only`` repositories (the
review chats') merge first.

A verdict is a ``/reviewed pass <head> <digest>`` from a login holding the tier the Poller
asks of one (``authorization.required_tier``): Release where ``auto_release`` lets a pass merge,
Work where a person's Merge press still follows it (#2375, #3157), or from the deck's own
reviewer through ``/api/deck-request``; it counts only at the pull request's current head and
diff (``review_refs.target_digest``). ``changes-needed`` clears the pass. ``no-code`` (#3322)
holds as ``changes-needed`` does, about findings that need no code change; once the router sees
them fixed (``no_code_fixed``) that review counts as a pass, with no new one. A recorded verdict,
either way, is one message to the Work's chat naming the pull request, the head and the review
chat (#2992): it is never posted on GitHub, so the agent would otherwise read "no review yet"
there.

Problems go to the Work's agent (operator decision, 2026-10-12): a conflict, red checks, a
branch the deck could not bring up to date, or a merge GitHub refused is one message to the
Work's chat, once per head. A branch behind its base is updated first, one per repository at a
time (#3022): reviewed pull requests wait in a merge queue, and only the first in line is
brought up to its base and re-tested, so each merge costs one re-test rather than one per
pull request behind it (operator decisions, 2026-10-13).

Woken by the router's deliveries (verdicts, pull request, label and check events), a job or a
chat moving (``jobs``, ``sessions``: only Works waiting on tests or on a turn), the Merge press
and a deck Pause or Resume. No timer (ADR 0007). Best effort, as the router is (ADR 0008 point
6): the store is replaced atomically but not fsynced, and a lost wake is covered by the next
event on the pull request. The one exception is a look that stopped on a read that failed (the
issue's labels, a pull request, the Poller's board): nothing else would look again, so it
leaves a ``retry`` mark and is looked at again on a growing back-off until a look ends on
something else (#3023).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .shared_file import SharedFile
from .watch_retry import Backoff

log = logging.getLogger(__name__)

STORE_FILENAME = "work-release.json"
#: A reviewer's findings are sent to the Work's chat up to this long, with a link to the rest.
MAX_FINDINGS = 8000

MERGED = "merged"
ALREADY_MERGED = "already-merged"
FAILED = "failed"

#: ``WorkReleaseService.approval``: a Merge press may be made, or one stands (#3320).
MERGE_OFFERED = "offered"
MERGE_APPROVED = "approved"


class PressRefused(Exception):
    """A Merge press the release did not record (#3320), in words for the person who pressed."""


#: What ``evaluate`` answers for a Work that only waits for a person's Merge press.
AWAITING_MERGE = "reviewed and tested; waiting for a person's Merge"
LABELS_UNREADABLE = "the issue's labels could not be read"
NO_PULLS_KNOWN = "no pull request is known yet"
#: A passing run at the head that proves nothing, as an answer ends (#3097).
TREE_MOVED = "but the tree changed during the run, so they do not count"
MOVED_TEXT = (
    "AgentDeck: a verification of {ref} passed at {head}, but it does not count: the checkout"
    " changed while it ran, so a test or a tool wrote to it. Keep the checkout still and run"
    " this Work's verification job on that head again; the deck merges once it is green."
)
#: A passing run at the head that started on an unclean tree, as an answer ends (#3397).
TREE_UNCLEAN = "but the checkout had uncommitted or untracked files, so they do not count"
UNCLEAN_TEXT = (
    "AgentDeck: a verification of {ref} passed at {head}, but it does not count: the checkout"
    " had uncommitted or untracked files when it started. Run `git status --porcelain -uall`"
    " there, commit or remove what it lists (left-over build or cache files may belong in"
    " .gitignore), and run this Work's verification job on that head again; the deck merges"
    " once it is green."
)
#: What the answers of a failed read say: a pull request, its diff (``DIGEST_UNREADABLE``) or
#: its comparison with its base.
_UNREADABLE_WORDS = (" could not be read", " could not be compared with its base")

#: A look stopped by a failed read is tried again after this, doubling up to the ceiling, the
#: router's pending-action back-off (#3023); given up a day after the first failure.
RETRY_FIRST_S = 60.0
RETRY_MAX_S = 30 * 60.0
RETRY_MAX_AGE_S = 24 * 3600.0
#: The one message to a Work's agent whose review passed at a head no job tests (#2999).
VERIFY_TEXT = (
    "Run this Work's verification job on that head now, as a test run the deck can see; the"
    " deck merges once it is green."
)
REVIEW_PASSED_TEXT = "AgentDeck: the review of {ref} passed at {head}. " + VERIFY_TEXT
#: A pass whose digest could not be read at the new head: no answer, so a place in the merge
#: queue stands (#3022 review).
DIGEST_UNREADABLE = "could not be read to carry its review pass over"
#: What the digest port answers for a diff GitHub will not serve at all (over its size limit).
#: No pass was ever recorded for such a diff, so it is a changed diff, not an unreadable one:
#: waiting would hold the first place in line for good (#3029).
DIFF_TOO_LARGE = "too-large"
#: How long the deck waits for an update it made to show on GitHub, and how many times it asks
#: for one head, before a pull request that stays behind leaves the merge queue (#3033).
UPDATE_GRACE_S = 600.0
UPDATE_TRIES = 2
#: How long the first in a merge queue may go unevaluated while it carries no wake mark of its
#: own (#3034): an evaluation that ended on an unreadable GitHub answer, or one a reorder raced,
#: leaves the line's head waiting on nothing.
QUEUE_LOOK_S = 120.0

#: The one message to a Work's agent once its pull requests wait only for a person's Merge
#: (#3199): that person reads the Work's summary first, so its evidence must be the final one.
READY_TEXT = (
    "AgentDeck: ready for merge: {refs}. Its review passed and its tests are green at that"
    " head (or its review alone proves it), so only a person's Merge is left. That person"
    " reads this Work's summary before pressing Merge, so rewrite its `correctness` part"
    " now with the final evidence: the review, the test run or CI build, and the head commit."
    " Send the whole record again in one `PUT $AGENTDECK_URL/api/sessions/<your session"
    " key>/work-resolution`. Change nothing else."
)
#: The one message to a Work's agent once its last open pull request merged (#3141): check the
#: change live, then record the result, which the Work's status line and its Finish read.
LIVE_CHECK_TEXT = (
    "AgentDeck: {refs} merged. Check that it works live now. {deploy} Then try the change in"
    " the live app the way a person would use it, post what you checked on the issue (with a"
    " screenshot if it shows in the UI), and record the result with `PUT"
    " $AGENTDECK_URL/api/sessions/<your session key>/live-check` and"
    ' `{{"result": "verified" | "failed" | "nothing_to_check" | "not_live", "note": "<one'
    ' line>"}}`. Record `verified` only once every one of them runs live; if any is merged but'
    " not running yet, record `not_live` and name it, which keeps the Work open (#3220)."
    " Once you record `verified`, close the issue yourself with a one-line summary; do not"
    " ask anyone whether to close it (#3437). If it failed, reopen the issue, say there what"
    " failed, and fix it in a new pull request."
)
#: This deck's own repository, whose merges its deploy timer ships (``work_bugs.HOME``). Spelled
#: here rather than imported: ``work_bugs`` reaches most of the deck, and this module is loaded
#: by every test through ``poller_ops``.
DECK_REPOSITORY = "eerovil/agentdeck"
#: How the merged change reaches production, by where it merged (``deploy_sentence``).
DECK_DEPLOY_TEXT = (
    "This deck deploys it by itself within minutes: register a wake-on with"
    ' `{"deck_deploy": "<the merge commit>"}` (`gh pr view <pr> --json mergeCommit`) and end'
    " the turn; it wakes you once the running deck includes it."
)
AUTO_DEPLOY_TEXT = (
    "Its production deploy follows the merge: register a `deployment` wake-on for {repos} and"
    " end the turn; its reply shows the latest deploy the deck has seen. Or follow the"
    " repository guide's deploy check. Do not read the GitHub deployments API: the AgentDeck"
    " App cannot read it in every organization."
)
NO_DEPLOY_TEXT = (
    "Nothing on this host watches its deploy: follow the repository guide to see when it is"
    " live. One that waits on a deploy by hand is `not_live` until it runs;"
    " `nothing_to_check` is only for a change with no live behaviour at all."
)

_GREEN = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_RED = frozenset(
    {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)


@dataclass(frozen=True)
class Pull:
    """One pull request as GitHub has it now."""

    ref: str  # lowercased owner/repo#n
    state: str = ""  # OPEN, CLOSED, MERGED
    draft: bool = False
    head: str = ""
    mergeable: str = ""  # MERGEABLE, CONFLICTING, UNKNOWN
    merge_state: str = ""  # CLEAN, BEHIND, BLOCKED, DIRTY, ...
    checks: str = ""  # green, red, pending, none
    #: Commits the base has that the head lacks (the Poller's ``behind_by``, #2101); ``None``
    #: when the comparison could not be read, which is never "up to date".
    behind_by: int | None = 0

    @property
    def behind(self) -> bool:
        """Behind its base, read as the Poller's ``classify_pr`` reads it."""
        return self.merge_state == "BEHIND" or (self.behind_by or 0) > 0

    @property
    def repository(self) -> str:
        return self.ref.partition("#")[0]

    @property
    def number(self) -> int:
        return int(self.ref.partition("#")[2])


@dataclass(frozen=True)
class Policy:
    """What a project's manifest says about merging (``poller_ops.release_policies``)."""

    auto_release: bool = False
    merge_method: str = "merge"
    review_only: frozenset[str] = frozenset()  # lowercased owner/repo
    github_checks: frozenset[str] = frozenset()
    #: The manifest's ``[authorization]`` slot (``authorization.parse_policy``): who holds
    #: Release, by allowlist or by GitHub permission at the tier's role bar. ``None`` admits no
    #: login, only the deck's own reviewer.
    authorization: object | None = None
    #: The manifest's ``spec_only_label``: a Work carrying it is a spec, never merged (D12).
    spec_only_label: str = ""
    #: The manifest's ``paused_label``: the issue carrying it is a pause.
    paused_label: str = ""
    #: The repositories whose merges deploy by themselves (``auto_deploys``), lowercased
    #: ``owner/repo``: the message after a merge says how to wait for that deploy (#3141).
    deploys: frozenset[str] = frozenset()
    #: The manifest's ``[staging]`` (#3180): its branch copies (``StagingSite``), and the
    #: repositories, lowercased ``owner/repo``, that deploy into them.
    staging: object | None = None
    staging_repos: frozenset[str] = frozenset()


def deploy_sentence(policy: Policy, refs: Iterable[str]) -> str:
    """How the agent waits for each of ``refs``' merges to go live, for ``LIVE_CHECK_TEXT``.

    One sentence per way of deploying, naming its pull requests (#3220): one sentence for the
    lot told #3192's agent that this deck deploys a connector it never runs.
    """
    ways: dict[str, list[str]] = {}
    for ref in sorted(set(refs)):
        repository = ref.partition("#")[0].lower()
        if repository == DECK_REPOSITORY:
            ways.setdefault(DECK_DEPLOY_TEXT, []).append(ref)
        elif repository in policy.deploys:
            ways.setdefault(AUTO_DEPLOY_TEXT, []).append(ref)
        else:
            ways.setdefault(NO_DEPLOY_TEXT, []).append(ref)
    said = []
    for text, pulls in ways.items():
        if text is AUTO_DEPLOY_TEXT:
            repositories = sorted({ref.partition("#")[0].lower() for ref in pulls})
            text = text.format(repos=", ".join(repositories))
        said.append(f"{', '.join(pulls)}: {text}")
    return " ".join(said)


def checks_state(rollup: object) -> str:
    """``green``, ``red``, ``pending`` or ``none`` for a ``statusCheckRollup``."""
    if not isinstance(rollup, list) or not rollup:
        return "none"
    states = []
    for item in rollup:
        if not isinstance(item, dict):
            continue
        value = str(item.get("conclusion") or item.get("state") or "").upper()
        states.append(value)
    if any(value in _RED for value in states):
        return "red"
    if states and all(value in _GREEN for value in states):
        return "green"
    return "pending"


def verifies(rows: Iterable[object], work_id: str) -> bool:
    """Is a verification job of this Work queued or running among the queue's ``rows``?

    The queue's per-Work read answers every unfinished job (``jobs.rows_for_work``), so only
    the ones naming this Work, compared case-folded, count.
    """
    from .jobs import QUEUED, RUNNING, VERIFICATION

    wanted = work_id.strip().casefold()
    return bool(wanted) and any(
        isinstance(row, dict)
        and str(row.get("work_id") or "").strip().casefold() == wanted
        and row.get("purpose") == VERIFICATION
        and row.get("state") in {QUEUED, RUNNING}
        for row in rows
    )


#: How ``on_verdict`` answers a pass it recorded, and findings about a commit the branch left.
PASS_RECORDED = "review pass recorded for"
FINDINGS_DROPPED = "findings dropped"
#: Under every reviewer's findings the Work's agent hears (#3314): a finding only the owner can
#: settle, sent as one to fix, held a Work in review for two days until it repeated.
OWNER_CALL_ASK = (
    "If a finding is the owner's decision rather than a defect (a design trade-off, or relaxing"
    " something the issue asks for), do not work around it: ask the owner in your question box"
    " now, with the options. Once they answer, post their decision on the issue; when it"
    " settles the finding without a code change, ask for a fresh review of the same commit"
    " with your session's `review-request`, quoting the decision as its note."
)

#: The verdict a reviewer sends when none of its findings needs a change to code or tests
#: (#3322): a walkthrough, the pull request body, staging evidence or doc files. It holds the
#: merge as ``changes-needed`` does, and fixing it needs no new review.
NO_CODE = "no-code"
#: What counts as a doc file for a ``no-code`` fix (#3322): by name alone. Not ``.txt``, which
#: is also ``requirements.txt`` and ``tests/too_slow.txt``; a comment or docstring in a code
#: file is code.
DOC_SUFFIXES = (".md", ".mdx", ".mdc", ".rst")
DOC_DIRS = ("docs/",)
DOC_FILES_TEXT = "`*.md`, `*.mdx`, `*.mdc`, `*.rst` or anything under `docs/`"
#: How a reviewer marks a rare edge case (#3574): a finding not worth a fix round unless the
#: agent chooses one. The owner's standing rule is to accept these as documented limits, so
#: the reviewer leaves the choice to the agent rather than escalating each one.
EDGE_CASE_MARK = "Edge case:"
EDGE_CASE_TEST = (
    "it happens only when something else has already failed (a failed request, being"
    " offline, a deck or GitHub outage) or in a race faster than a person can act, and it"
    " loses no data and reaches no person (sends nothing twice, posts nothing)"
)
#: The one message a ``no-code`` verdict sends the Work's chat (#3322).
NO_CODE_TEXT = (
    "AgentDeck: the review of {pull} at {head} ({reviewer}) asks only for fixes that need no"
    " change to code or tests, so it will not merge yet. Fix them on GitHub (a walkthrough, the"
    " pull request body, staging evidence) or in doc files (" + DOC_FILES_TEXT + ") and push."
    " If you pushed nothing, turn the pull request to draft and mark it ready again. No new"
    " review runs: that review then counts as a pass. A push that changes any other file is"
    " reviewed again as usual. A finding marked \"" + EDGE_CASE_MARK + "\" is a rare edge"
    " case (#3574): fix it, or accept it as a known limit by naming it under \"Known limits\""
    " in the pull request body, which needs no new review either. The owner's standing rule"
    " is to accept rare edge cases; ask them in your question box only when you doubt it."
)


def is_doc_path(path: str) -> bool:
    """Whether a changed file is a doc file a ``no-code`` fix may touch (#3322)."""
    name = (path or "").strip().lower()
    return bool(name) and (name.endswith(DOC_SUFFIXES) or name.startswith(DOC_DIRS))


def _set_no_code(entry: dict, pull: str, held: dict | None) -> None:
    """Keep ``held`` as the ``no-code`` verdict on ``pull``, or drop it (#3322)."""
    kept = dict(entry.get("no_code") or {})
    kept.pop(pull, None)
    if held:
        kept[pull] = held
    if kept:
        entry["no_code"] = kept
    else:
        entry.pop("no_code", None)


def verdict_settled(answer: str) -> bool:
    """Whether ``on_verdict``'s answer leaves nothing for the comment itself to tell the chat
    (#3192): a pass the release already told it about, or findings about a commit it left."""
    return answer.startswith(PASS_RECORDED) or answer.endswith(FINDINGS_DROPPED)


#: The heading a reviewer's findings open with, naming the pull request and the commit read:
#: ``Review findings for PR #12 at abc1234`` (ChatGPT) or ``Review of owner/repo#12 at abc1234``
#: (the deck's reviewer, #3004).
_FINDINGS_HEAD_RE = re.compile(
    r"\A\s*(?:/claude\s+)?Review\s+(?:findings\s+for\s+|of\s+)?(?:PR\s+)?"
    r"(?P<repository>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)?#(?P<number>\d+)"
    r"\s+at\s+`?(?P<head>[0-9a-fA-F]{7,40})\b",
    re.IGNORECASE,
)


def findings_target(text: str, repository: str) -> tuple[str, str] | None:
    """``("owner/repo#12", head)`` that a findings text says it reviewed, or ``None``.

    ``#12`` alone is in ``repository``, the repository the findings were posted in, and
    ``repo#12`` is in that repository's owner.
    """
    found = _FINDINGS_HEAD_RE.match(text or "")
    if found is None:
        return None
    named = (found.group("repository") or repository).lower()
    if "/" not in named:
        named = f"{repository.lower().partition('/')[0]}/{named}"
    return f"{named}#{found.group('number')}", found.group("head").lower()


def same_commit(full: str, given: str) -> bool:
    """A full sha and an abbreviation of it (at least 7 characters) name one commit."""
    full, given = (full or "").lower(), (given or "").lower()
    return len(given) >= 7 and bool(full) and full.startswith(given)


def review_chat_of(
    chat_repository: str, heads: dict[int, dict[tuple[str, int], str]], pull: str, head: str
) -> str | None:
    """The newest review chat opened about ``pull`` at ``head``, as ``owner/repo#n`` (#2992).

    ``heads`` is the deck's record of which commit each review chat covers per pull request
    (``AppState.chat_review_heads``); ``None`` when no chat covers that head.
    """
    repository, _, number = pull.casefold().partition("#")
    if not chat_repository or not number.isdigit():
        return None
    found = [
        chat
        for chat, covered in heads.items()
        for (target, target_number), sha in covered.items()
        if target.casefold() == repository
        and target_number == int(number)
        and (same_commit(head, sha) or same_commit(sha, head))
    ]
    return f"{chat_repository}#{max(found)}" if found else None


def _round_key(entry: dict, plain: str, chat: str | None) -> str:
    """The message key of one review's verdict on a head (#3183).

    A head is reviewed again on request in a new review chat, and its verdict must reach the
    agent even when the first review's verdict was the same. The first review of a head keeps
    the plain key and notes its chat; a verdict from another chat is keyed by that chat, so it
    is a new message, while a replay of the same review's verdict is the same one. A plain key
    told before chats were noted counts as another chat's: a repeat message beats a lost one.
    """
    if not chat:
        return plain
    chats = entry.setdefault("told_chats", {})
    if plain not in (entry.get("told") or []) or chats.get(plain) == chat:
        chats[plain] = chat
        # Bounded like ``told``: the keys of heads long merged or abandoned go first.
        while len(chats) > 200:
            del chats[next(iter(chats))]
        return plain
    return f"{plain}@{chat}"


def merge_outcome(code: int, blob: str) -> str:
    """What the merge call (``PUT …/pulls/<n>/merge`` with ``sha``) answered."""
    if code == 0:
        return MERGED
    blob = blob.lower()
    if "already" in blob and "merged" in blob:
        return ALREADY_MERGED
    return FAILED


class ReleaseStore:
    """Each Work's release facts, in one small JSON file keyed by work id.

    Per Work: ``passes`` (pull -> head and digest the reviewer passed, when, and the review chat
    or login behind it, #2992), ``asked`` (a person pressed Merge: ``{"at", "pulls": pull ->
    {"digest", "head"}}``, the diffs it was pressed for, #3175 and #3320; ``pull -> digest`` and
    ``True`` from older decks), ``asked_cleared`` (the pull request whose changed diff cleared a
    press, and its head, #3320), ``awaiting_merge`` (pull -> head, set while every open pull
    request is reviewed and tested and only a person's Merge is missing, #2960), ``updated``
    (pull -> head a behind branch was updated from), ``update_at`` (pull -> ``[when, tries]`` of
    that update, #3033), ``unlanded`` (pull -> head whose update never landed, so it waits out
    of line), ``told`` (message keys already sent to the Work's chat), the
    ``waiting_tests``/``waiting_turn`` marks that let a finished job or an idle chat look again,
    ``queued`` (the pulls held only by their place in a merge queue, looked at again once one is
    first, #3034), and ``retry`` (first failure ``since``, last wait ``delay``), set while the
    last look stopped on a failed read (#3023).

    Beside the Works, ``queues``: the merge queue (#3022), per repository (lowercased
    ``owner/repo``) a list of ``{"pull", "work", "at", "rounds"}``, first in line first.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        # Two web workers share the file during a hot reload (#3070): re-read it whenever
        # the other one wrote, and lock each read-modify-write.
        self._file = SharedFile(
            self.path, prefix=".work-release.", on_foreign_write=self._foreign_write
        )
        self._works: dict[str, dict] = {}
        self._queues: dict[str, list[dict]] = {}
        #: Told after every write, so a page that reads the release rebuilds (#2895 step 3).
        self.on_change: Callable[[], None] | None = None

    def _foreign_write(self) -> None:
        self._works = self._load()
        if self.on_change is not None:
            self.on_change()

    @property
    def works(self) -> dict[str, dict]:
        if self._file.stale():
            self._works = self._load()
        return self._works

    @property
    def queues(self) -> dict[str, list[dict]]:
        """The merge queue per repository (#3022); edit it in place, then ``put_queues``."""
        self.works  # noqa: B018 -- loads the file, queues included
        return self._queues

    def _load(self) -> dict[str, dict]:
        try:
            raw = self._file.read()
        except (OSError, ValueError):
            log.warning("work-release: ignoring unreadable %s", self.path)
            return {}
        queues = raw.get("queues") if isinstance(raw, dict) else None
        self._queues = {}
        if isinstance(queues, dict):
            self._queues = {
                repository: [
                    row
                    for row in rows
                    if isinstance(row, dict) and isinstance(row.get("pull"), str)
                ]
                for repository, rows in queues.items()
                if isinstance(repository, str) and isinstance(rows, list)
            }
        works = raw.get("works") if isinstance(raw, dict) else None
        if not isinstance(works, dict):
            return {}
        return {k: v for k, v in works.items() if isinstance(k, str) and isinstance(v, dict)}

    def get(self, work_id: str) -> dict:
        entry = self.works.get(work_id) or {}
        return json.loads(json.dumps(entry))  # a copy the caller may edit

    def put(self, work_id: str, entry: dict) -> None:
        with self._file.locked():
            works = dict(self.works)
            if entry:
                works[work_id] = entry
            else:
                works.pop(work_id, None)
            self._write(works)

    def put_queues(self) -> None:
        """Write the merge queue as it now stands in ``queues``.

        The queue edited in place wins; the Works are re-read first if another web worker
        wrote them since (#3070)."""
        with self._file.locked():
            queues = self._queues
            works = dict(self.works)
            self._queues = queues
            self._write(works)

    def place(self, work_id: str) -> list[tuple[str, int, int, int]]:
        """``(pull, place, size, rounds)`` for each of this Work's pulls in a queue; place 1 is
        first in line."""
        found = []
        for rows in self.queues.values():
            for index, row in enumerate(rows):
                if row.get("work") == work_id:
                    found.append((row["pull"], index + 1, len(rows), int(row.get("rounds") or 0)))
        return found

    def _write(self, works: dict[str, dict]) -> None:
        queues = {repository: rows for repository, rows in self._queues.items() if rows}
        self._queues = queues
        self._file.write({"works": works, "queues": queues})
        self._works = works
        if self.on_change is not None:
            self.on_change()


@dataclass
class Ports:
    """What the release reads and writes outside itself; ``app.py`` wires the real ones."""

    switched: Callable[[str], bool]
    paused: Callable[[str], bool]
    policy: Callable[[str], Policy]
    #: The Work's registered pull requests, lowercased ``owner/repo#n``.
    pulls: Callable[[str], Iterable[str]]
    #: The chat that owns the Work, for the messages to its agent.
    chat: Callable[[str], str | None]
    read_pull: Callable[[str], Awaitable[Pull | None]]
    #: The review digest of ``ref``'s diff at ``head``; "" when it could not be read just now,
    #: ``DIFF_TOO_LARGE`` when GitHub will never serve it.
    digest: Callable[[str, str], Awaitable[str]]
    #: The heads a green verification job of this Work proves.
    green_heads: Callable[[str], Awaitable[set[str]]]
    #: Merge ``ref`` at exactly ``head`` with ``method``; a ``merge_outcome`` and GitHub's words.
    merge: Callable[[str, str, str], Awaitable[tuple[str, str]]]
    update_branch: Callable[[str], Awaitable[bool]]
    #: (chat id, message id, text). ``False`` is a store that holds nothing under the id: not
    #: told, so asked again; anything else (``None`` too) is taken as stored.
    tell: Callable[[str, str, str], bool | None]
    #: Is one of the Work's chats mid-turn? Then its agent may still push: hold the merge.
    busy: Callable[[str], bool] = lambda work_id: False
    #: Did a person star the Work (#3116)? Its pull requests join behind the starred ones.
    starred: Callable[[str], bool] = lambda work_id: False
    #: The Work's issue labels, casefolded; ``None`` when unreadable (which holds the merge).
    issue_labels: Callable[[str], Awaitable[set[str] | None]] | None = None
    #: A login's collaborator permission on a repository (``admin``, ``maintain``, ...), or
    #: ``None`` when unreadable: the live half of the Poller's Release check.
    permission: Callable[[str, str], Awaitable[str | None]] | None = None
    #: Is a verification job of this Work queued or running? Then its agent already ran it,
    #: and is not told to (#2999).
    verifying: Callable[[str], Awaitable[bool]] | None = None
    #: The review chat (``owner/repo#n``) opened about a pull request at a head, or ``None``:
    #: named in the verdict's message to the Work's chat and on its Ready for merge (#2992).
    review_chat: Callable[[str, str], str | None] = lambda pull, head: None
    #: True in an edition with no reviewer (the public copy, #3623): a person's Merge press
    #: is then also the review pass for each pull request at the head it was pressed for.
    press_is_review: bool = False
    #: Whether the router proved ``pull``'s current head (first) only a base merge away from
    #: an older head (second) and reviewed nothing new for it (#3314): that head's verdict
    #: stands for the current one, findings and pass alike.
    same_change: Callable[[str, str, str], bool] = lambda pull, head, older: False
    #: Told each verdict the release took about ``pull`` at a head (#3314 review): only a head
    #: whose review came back has a verdict a base merge can keep.
    verdict_taken: Callable[[str, str], object] = lambda pull, head: None
    #: The files of ``pull``'s diff now, or ``None`` when unread (#3322): kept with a
    #: ``no-code`` verdict, so a fix that reverts one of them is not taken for a base merge.
    pull_files: Callable[[str], Awaitable[set[str] | None]] | None = None
    #: The heads a settled, failed verification job of this Work proves red (#3022): the first
    #: pull request in the merge queue leaves it when its re-test fails there.
    failed_heads: Callable[[str], Awaitable[set[str]]] | None = None
    #: The heads a passing verification of this Work ran on while its tree changed under it
    #: (#3097): it proves nothing, and the Work's line says so.
    moved_heads: Callable[[str], Awaitable[set[str]]] | None = None
    #: The heads a passing verification of this Work ran on from a tree with uncommitted or
    #: untracked files (#3397): it proves nothing, and the Work's line says so.
    unclean_heads: Callable[[str], Awaitable[set[str]]] | None = None
    #: The chat's provider's ``user_message_refusal`` for a text, or ``None`` when it would pass
    #: the words on: a reviewer's findings it would drop are sent as a link instead (#3004).
    refusal: Callable[[str, str], str | None] = lambda chat, text: None
    clock: Callable[[], float] = time.time
    #: Run a coroutine function after a delay in seconds (#3033 review): the one wake on a
    #: clock, for a branch update that has not shown on GitHub. ``None`` uses the event loop.
    later: Callable[[float, Callable[[], Awaitable[object]]], object] | None = None
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)


class WorkReleaseService:
    """Merge each switched Work, from its events, once it may."""

    def __init__(self, store: ReleaseStore, ports: Ports, *, enabled: bool = True) -> None:
        self.store = store
        self.ports = ports
        self._enabled = bool(enabled)
        self._task: asyncio.Task | None = None
        self._sweep: asyncio.Task | None = None
        #: Works that became first in a merge queue, evaluated once the lock that moved them up
        #: is let go (#3022).
        self._wakes: set[str] = set()
        #: The one timed look per Work, for an update that has not landed (#3033 review).
        self._timers: dict[str, object] = {}
        self._looks: set[asyncio.Task] = set()
        #: When each Work was last evaluated (this process's clock), for the look at each
        #: line's first (#3034).
        self._looked: dict[str, float] = {}
        #: Told the chat a verdict reached and which pull requests it is about (``None``: the
        #: whole Work), so those review waits end quietly (#3183).
        self.on_review_answer: Callable[[str, Iterable[str] | None], object] | None = None

    def _review_answered(
        self, work_id: str, entry: dict, key: str, pulls: Iterable[str] | None
    ) -> None:
        """A verdict or findings were taken and told: the chat waits on no review of ``pulls``.

        Only once the message under ``key`` is stored (#3183 review): a message the store
        refused leaves the wait, whose timeout still wakes the agent.
        """
        chat = self.ports.chat(work_id)
        if not chat or self.on_review_answer is None or key not in (entry.get("told") or []):
            return
        try:
            self.on_review_answer(chat, pulls)
        except Exception:  # noqa: BLE001 - a stale wait times out on its own
            log.warning("work-release: ending review waits of %s failed", work_id, exc_info=True)

    # --- who this is for ---

    def handles(self, work_id: str) -> bool:
        """Does the deck release this Work?"""
        repository = _repository_of(work_id)
        return (
            self._enabled
            and bool(repository)
            and self.ports.switched(repository)
        )

    # --- lifecycle: the watch on jobs and chats ---

    def start(
        self,
        bus: object | None = None,
        scanned: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        """``scanned`` waits for the first scan of the chats, which tell a Work's pull requests."""
        if self._enabled and self._task is None and bus is not None:
            self._task = asyncio.create_task(self.watch_jobs(bus), name="work-release-jobs")
            # Its own task, so the watch subscribes at once rather than after the reads.
            self._sweep = asyncio.create_task(
                self.look_at_reviewed(scanned), name="work-release-start"
            )

    async def stop(self) -> None:
        for timer in self._timers.values():
            with contextlib.suppress(Exception):
                timer.cancel()  # type: ignore[attr-defined]
        self._timers.clear()
        sweep, self._sweep = self._sweep, None
        if sweep is not None:
            sweep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def watch_jobs(self, bus: object) -> None:
        """Look again at waiting Works when the job queue or a chat moves."""
        with bus.subscribe("jobs", "sessions") as subscription:
            while True:
                await subscription.get()
                while subscription.get_nowait() is not None:
                    pass  # a burst of moves is one look
                # From the revisions, not the queued events: a full queue drops the oldest
                # event, and a dropped ``jobs`` would strand a Work waiting on its tests.
                topics = subscription.changed_topics()
                try:
                    await self.on_tests(jobs="jobs" in topics, chats="sessions" in topics)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("work-release: looking again after a job failed")

    async def look_at_reviewed(
        self, scanned: Callable[[], Awaitable[object]] | None = None
    ) -> None:
        """Evaluate every Work holding a review pass once, at start (#2960).

        ``awaiting_merge`` is written only by an evaluation, which runs on an event; a Work
        that was reviewed and tested before a restart would otherwise wait for its next event
        to read Ready for merge. After ``scanned``: before the chats are scanned a Work reads no
        pull requests, and would wait for its next event to merge (#2982).
        """
        if scanned is not None:
            await scanned()
        for work_id, entry in list(self.store.works.items()):
            if not entry.get("passes") or not self.handles(work_id):
                continue
            try:
                answer = await self.evaluate(work_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("work-release: evaluating %s at start failed", work_id, exc_info=True)
                continue
            # Every answer, so a reviewed Work that did not turn Ready for merge can be read
            # off the journal: a label, an unreadable pull request or none known yet (#3080).
            log.info("work-release: %s at start: %s", work_id, answer)

    def _mark_retry(self, work_id: str, entry: dict, reason: str) -> None:
        """Look again after the back-off: the look stopped on a failed read (#3023).

        A timed look (``_look_later``), not a stored due time: a restart loses the timer, and
        the start's look at every reviewed Work, the only Works marked, sets it again."""
        now = self.ports.clock()
        mark = entry.get("retry") if isinstance(entry.get("retry"), dict) else {}
        since = float(mark.get("since", now))
        if now - since > RETRY_MAX_AGE_S:
            log.warning(
                "work-release: giving up looking again at %s after a day of failed reads (%s)",
                work_id,
                reason,
            )
            entry.pop("retry", None)
            return
        backoff = Backoff(first=RETRY_FIRST_S, cap=RETRY_MAX_S)
        backoff.next_s = float(mark.get("delay", 0.0))
        delay = backoff.failed()
        entry["retry"] = {"since": since, "delay": delay}
        log.info("work-release: %s: %s; looking again in %.0fs", work_id, reason, delay)
        self._look_later(work_id, delay)

    # --- entry points ---

    async def on_unreadable_verdict(self, work_id: str, where: str, *, by: str) -> str:
        """Tell the Work's chat a ``/reviewed`` comment from ``by`` could not be read (#3161).

        Only from a login that may send a verdict here, so anyone's malformed comment does not
        reach the agent; once per comment (``where``, its URL).
        """
        if not self.handles(work_id):
            return "not released by the deck"
        if not await self._holds(work_id, by, self._verdict_tier(work_id)):
            return f"{by} may not send a review verdict here"
        async with self._lock(work_id):
            entry = self.store.get(work_id)
            told = self._tell(
                work_id,
                entry,
                f"verdict:unreadable:{where}",
                f"AgentDeck: {by}'s review verdict at {where} could not be read, so no verdict"
                " was recorded from it. Each line must read `/reviewed pass <head> <digest>"
                " [owner/repo#n]` or `/reviewed changes-needed <head> [owner/repo#n]`, with"
                " nothing else in the comment.",
            )
            self.store.put(work_id, entry)
        return "told the Work's chat" if told else "not told (no chat, or already told)"

    async def on_verdict(
        self,
        work_id: str,
        verdict: str,
        head: str,
        digest: str,
        targets: Iterable[str] = (),
        *,
        by: str | None = None,
    ) -> str:
        """Take a reviewer's verdict; answer what became of it, for a log or a receipt.

        ``by`` is the GitHub login that posted it; ``None`` is the deck's own reviewer through
        ``/api/deck-request``, which the deck trusts as the Poller trusts its request file. A
        login must hold the tier the Poller asks of a ``/reviewed`` here (``_verdict_tier``).
        """
        if not self.handles(work_id):
            return "not released by the deck"
        if by is not None:
            tier = self._verdict_tier(work_id)
            if not await self._holds(work_id, by, tier):
                name = tier.value.capitalize()
                return f"{by} may not send a review verdict here (it holds no {name} tier)"
        pulls = sorted(set(self.ports.pulls(work_id)))
        targets = tuple(targets)
        named = [_match(pull, target) for target in targets for pull in pulls]
        chosen = sorted({pull for pull in named if pull})
        if not targets:
            # A verdict naming a pull request this Work does not count is not about its only
            # one: beside another verdict in one comment, it names another repository (#3161).
            chosen = pulls if len(pulls) == 1 else []
        if len(chosen) != 1:
            return _unplaced(pulls, chosen)
        pull = chosen[0]
        if verdict in ("changes-needed", NO_CODE):
            if await self.moved_past(pull, head):
                # Findings about a commit the branch has left are dropped (#3192): the chat
                # that asked for them was replaced, and the current head has its own review.
                return f"{pull} moved past {head[:12]}; {FINDINGS_DROPPED}"
            # A no-code verdict that does not match the diff now holds as changes-needed: its
            # fix is reviewed again (#3322).
            held = await self._no_code_hold(pull, head, digest) if verdict == NO_CODE else None
            async with self._lock(work_id):
                entry = self.store.get(work_id)
                (entry.get("passes") or {}).pop(pull, None)
                if entry.get("passes") == {}:
                    entry.pop("passes")
                entry.pop("asked", None)
                if held:
                    chat = self.ports.review_chat(pull, head)
                    held.update({"chat": chat} if chat else {})
                    held.update({"by": by} if by else {})
                _set_no_code(entry, pull, held)
                # Read by the Work's line until a push or a pass answers them (#3097).
                told = bool(self.ports.chat(work_id))
                entry["findings"] = {"at": self.ports.clock(), "told": told}
                # Out of line with all its pull requests, not only this one: the Work merges
                # nothing without this pass, so another of them first in line would hold the
                # line until the Work's next event (#3033).
                self._leave(work_id)
                word = NO_CODE if held else "changes-needed"
                message_key = _round_key(
                    entry,
                    f"verdict:{word}:{pull}@{head[:12]}",
                    self.ports.review_chat(pull, head),
                )
                reviewer = self._reviewer(pull, head, by)
                if held:
                    text = NO_CODE_TEXT.format(pull=pull, head=head[:12], reviewer=reviewer)
                else:
                    text = (
                        f"AgentDeck: the review of {pull} at {head[:12]} ({reviewer}) asks for"
                        " changes, so it will not merge. Fix the findings, push, and mark it"
                        " ready again."
                    )
                self._tell(work_id, entry, message_key, text)
                self.store.put(work_id, entry)
                self.ports.verdict_taken(pull, head)
                # Ended only for a verdict taken and told (#3183 review): a refused one leaves
                # the wait to its timeout, so the agent still looks.
                self._review_answered(work_id, entry, message_key, [pull])
            await self._wake_next(work_id)  # the next in line, once this lock is let go
            if verdict == NO_CODE and not held:
                return (
                    f"changes needed on {pull}: its {NO_CODE} verdict does not match the diff"
                    f" at {head[:12]}, so a fix is reviewed again"
                )
            return f"{NO_CODE} fixes needed on {pull}" if held else f"changes needed on {pull}"
        async with self._lock(work_id):
            entry = self.store.get(work_id)
            passes = entry.setdefault("passes", {})
            facts = await self.ports.read_pull(pull)
            if facts is None or facts.state != "OPEN":
                return f"{pull} is not open at {head[:12]}"
            current = await self.ports.digest(pull, facts.head)
            if not same_commit(facts.head, head) and not self.ports.same_change(
                pull, facts.head, head
            ):
                # A pass about a head the branch left counts only where the router proved the
                # new head a base merge away and reviewed nothing new for it (#3314).
                return f"{pull} is not open at {head[:12]}"
            if not current or current == DIFF_TOO_LARGE or current != digest.lower():
                return f"the digest does not match {pull}'s diff at {facts.head[:12]}"
            passed = {"head": facts.head, "digest": current, "at": self.ports.clock()}
            chat = self.ports.review_chat(pull, facts.head) or self.ports.review_chat(pull, head)
            if chat:
                passed["chat"] = chat
            if by:
                passed["by"] = by
            passes[pull] = passed
            entry.pop("findings", None)  # a later pass answers them
            _set_no_code(entry, pull, None)
            self.store.put(work_id, entry)
            self.ports.verdict_taken(pull, head)
        lead = (
            f"AgentDeck: the review of {pull} at {facts.head[:12]}"
            f" ({self._reviewer(pull, facts.head, by)}) passed clean."
        )
        return await self._announce_pass(work_id, pull, facts.head, lead)

    async def _announce_pass(self, work_id: str, pull: str, head: str, lead: str) -> str:
        """Evaluate a pass just recorded at ``head`` and tell the Work's chat, once per head."""
        # Its own message carries the call to verify, so the agent hears one, not two (#2999).
        answer = await self.evaluate(work_id, tell_untested=False)
        # The Work's chat hears the pass once per head, with what the release made of it, so
        # its agent never reads "no verdict yet" off GitHub while the deck holds one (#2992).
        async with self._lock(work_id):
            entry = self.store.get(work_id)
            key = f"{pull}@{head[:12]}"
            untested = entry.pop("untested", None) == key
            text = (
                f"{lead} The deck records the pass; it is not posted on GitHub."
                f" Release: {answer or 'nothing holds it'}."
            )
            # Only a pass message this verdict sends carries the call to verify: a repeat pass
            # at a head already told about sends nothing, so the call goes on its own.
            # Each review of the head is its own message (#3183): a head reviewed again on
            # request passes again under that review chat's key, so its agent hears it.
            message_key = _round_key(
                entry, f"verdict:pass:{key}", self.ports.review_chat(pull, head)
            )
            fresh = message_key not in (entry.get("told") or [])
            if untested and fresh:
                text += " " + VERIFY_TEXT
            self._tell(work_id, entry, message_key, text)
            if untested:
                if fresh and message_key in (entry.get("told") or []):
                    entry["told"].append(f"verify:{key}")
                else:
                    self._tell(
                        work_id,
                        entry,
                        f"verify:{key}",
                        REVIEW_PASSED_TEXT.format(ref=pull, head=head[:12]),
                    )
            self.store.put(work_id, entry)
            self._review_answered(work_id, entry, message_key, [pull])
        return f"{PASS_RECORDED} {pull} at {head[:12]}"

    async def _no_code_hold(self, pull: str, head: str, digest: str) -> dict | None:
        """What a ``no-code`` verdict about ``pull`` at ``head`` keeps, or ``None`` (#3322).

        Only for the open pull request's current head and the diff the reviewer read, as a
        pass is; anything unread or different is ``None``, and holds as ``changes-needed``.
        """
        facts = await self.ports.read_pull(pull)
        if facts is None or facts.state != "OPEN" or not same_commit(facts.head, head):
            return None
        current = await self.ports.digest(pull, facts.head)
        if not current or current == DIFF_TOO_LARGE or current != (digest or "").lower():
            return None
        files = None
        if self.ports.pull_files is not None:
            with contextlib.suppress(Exception):
                files = await self.ports.pull_files(pull)
        held: dict = {"head": facts.head.lower(), "at": self.ports.clock()}
        if files is not None:
            held["files"] = sorted(files)
        return held

    def passed_since(self, work_id: str, pull: str, head: str, since: float) -> bool:
        """Whether a pass of ``pull`` at ``head`` was recorded at ``since`` or later (#3324)."""
        passed = (self.store.get(work_id).get("passes") or {}).get(pull.lower())
        if not isinstance(passed, dict) or not same_commit(str(passed.get("head") or ""), head):
            return False
        at = passed.get("at")
        return isinstance(at, (int, float)) and at >= since

    def no_code_head(self, work_id: str, pull: str) -> tuple[str, frozenset[str] | None] | None:
        """The head a ``no-code`` verdict holds ``pull`` at, and its diff's files then (``None``
        when unread), or ``None`` when no such verdict stands (#3322)."""
        held = (self.store.get(work_id).get("no_code") or {}).get(pull.lower())
        if not isinstance(held, dict) or not held.get("head"):
            return None
        files = held.get("files")
        return str(held["head"]), frozenset(files) if isinstance(files, list) else None

    async def no_code_fixed(self, work_id: str, pull: str, head: str) -> str:
        """The agent fixed a ``no-code`` review's findings at ``head`` (#3322): the router saw
        it marked ready at that verdict's head, or proved the push to ``head`` touched only
        doc files. That review counts as a pass at ``head``, with no new review."""
        if not self.handles(work_id):
            return "not released by the deck"
        pull = pull.lower()
        async with self._lock(work_id):
            entry = self.store.get(work_id)
            held = (entry.get("no_code") or {}).get(pull)
            if not isinstance(held, dict):
                return f"no {NO_CODE} verdict holds {pull}"
            facts = await self.ports.read_pull(pull)
            if facts is None or facts.state != "OPEN" or not same_commit(facts.head, head):
                return f"{pull} is not open at {head[:12]}"
            current = await self.ports.digest(pull, facts.head)
            if not current or current == DIFF_TOO_LARGE:
                return f"{pull}'s diff at {facts.head[:12]} {DIGEST_UNREADABLE}"
            passed = {
                "head": facts.head,
                "digest": current,
                "at": self.ports.clock(),
                NO_CODE: str(held.get("head") or ""),
            }
            for name in ("chat", "by"):
                if held.get(name):
                    passed[name] = held[name]
            entry.setdefault("passes", {})[pull] = passed
            entry.pop("findings", None)
            _set_no_code(entry, pull, None)
            self.store.put(work_id, entry)
            self.ports.verdict_taken(pull, facts.head)
        old = str(held.get("head") or "")
        since = ""
        if not same_commit(facts.head, old):
            since = f" changed only doc files since {old[:12]} and"
        lead = (
            f"AgentDeck: {pull} at {facts.head[:12]}{since} answers a review that asked only for"
            " fixes needing no code, so no new review was started and that review counts as a"
            " pass here."
        )
        return await self._announce_pass(work_id, pull, facts.head, lead)

    async def moved_past(self, pull: str, head: str) -> bool:
        """Whether ``pull``'s branch has left ``head``, on a positive read only (#3192).

        A reviewer still running on a review chat that a newer head replaced can only report
        on the old commit. A short or missing head, or an unreadable pull request, is not
        called moved: those findings are kept, as before. Nor is a branch that only merged its
        base since (#3314): the router proved it and reviews no new head, so these findings are
        the ones that stand.
        """
        if len(head or "") < 7:
            return False
        facts = await self.ports.read_pull(pull)
        if facts is None or not facts.head or same_commit(facts.head, head):
            return False
        return not self.ports.same_change(pull, facts.head, head)

    async def stale_findings(self, work_id: str, text: str, repository: str) -> str:
        """Why findings ``text`` posted in ``repository`` are about a commit its branch left,
        or ``""`` to hand them on (#3192). Only a pull request of this Work is read: a bare
        ``#12`` may name another repository's pull request than the one it was posted in.
        Findings handed on are a verdict about their head (``verdict_taken``, #3314 review)."""
        target = findings_target(text, repository)
        if target is None or target[0] not in {pull.lower() for pull in self.ports.pulls(work_id)}:
            return ""
        if not await self.moved_past(*target):
            self.ports.verdict_taken(*target)
            return ""
        pull, head = target
        return f"{pull} moved past {head[:12]}; {FINDINGS_DROPPED}"

    async def on_findings(self, work_id: str, key: str, text: str, url: str) -> bool:
        """Hand the deck's reviewer's findings to the Work's chat once; ``False`` with no chat,
        or when the store would not keep the message (then nothing records them as told, and
        the same ``key`` sent again tries again, #3183 review).

        ``key`` is the reviewer's request id, so a resent request is the same message. ``url``
        is where the findings are on GitHub, sent in place of a text the chat's provider would
        drop (#3004).
        """
        chat = self.ports.chat(work_id)
        if not chat:
            # Kept, so the Work's line says they never reached its agent (#3097).
            async with self._lock(work_id):
                entry = self.store.get(work_id)
                entry["findings"] = {"at": self.ports.clock(), "told": False}
                self.store.put(work_id, entry)
            return False
        body = text.strip()
        if len(body) > MAX_FINDINGS:
            body = body[:MAX_FINDINGS].rstrip() + " …"
        message = (
            f"AgentDeck: the deck's reviewer asks for changes on {_ref_of(work_id)}:\n\n{body}\n\n"
            f"{url}\n\nFix the findings, push, and mark the pull request ready again."
            f" {OWNER_CALL_ASK}"
        )
        if self.ports.refusal(chat, message) is not None:
            message = (
                f"AgentDeck: the deck's reviewer asks for changes on {_ref_of(work_id)}. Its text"
                " cannot be passed on here, because this chat's CLI would drop it before the"
                f" model saw it; read it on GitHub: {url}\n\n{OWNER_CALL_ASK}"
            )
        async with self._lock(work_id):
            entry = self.store.get(work_id)
            self._tell(work_id, entry, f"findings:{key}", message)
            told = f"findings:{key}" in (entry.get("told") or [])
            entry["findings"] = {"at": self.ports.clock(), "told": told}
            self.store.put(work_id, entry)
            # The deck's reviewer reviews the Work's one chat, which covers all its pull
            # requests; told first, so a refused message keeps the wait (#3183 review).
            self._review_answered(work_id, entry, f"findings:{key}", None)
        return told

    def _reviewer(self, pull: str, head: str, by: str | None) -> str:
        """Who reviewed, for a message: the review chat, else the login, else the deck's own."""
        chat = self.ports.review_chat(pull, head)
        if chat:
            return f"review chat {chat}"
        return f"by {by}" if by else "by the deck's reviewer"

    async def request_merge(self, work_id: str, *, fresh: bool = False) -> str:
        """A person pressed Merge, or Merge when ready (#3320), on the Work page.

        The press is for each open pull request's diff as it is now, draft or ready: it stands
        through any number of updates to base that leave those diffs the same (#3175), and a
        changed diff needs a new press. On a Work not yet ready the deck merges once the review
        passes the same diffs and the tests are green. ``PressRefused`` when nothing was
        recorded: no pull request is open, or a diff could not be read. ``fresh`` (Merge when
        ready) is refused too while a press already stands, checked under the Work's lock so two
        presses at once record one (#3320 review); a second Merge on a ready Work may retry.
        """
        if not self.handles(work_id):
            return "not released by the deck"
        async with self._lock(work_id):
            entry = self.store.get(work_id)
            if fresh and entry.get("asked"):
                raise PressRefused("Merge is already approved, so nothing new was recorded")
            refs = sorted(set(self.ports.pulls(work_id)))
            pulls: dict[str, dict] = {}
            passes = entry.get("passes") or {}
            for ref in refs:
                pull = await self.ports.read_pull(ref)
                if pull is None:
                    raise PressRefused(f"{ref} could not be read, so no Merge was recorded")
                if pull.state != "OPEN":
                    continue
                passed = passes.get(ref) or {}
                digest = ""
                if isinstance(passed, dict) and same_commit(
                    pull.head, str(passed.get("head") or "")
                ):
                    digest = str(passed.get("digest") or "")
                if not digest:
                    digest = await self.ports.digest(ref, pull.head)
                if digest == DIFF_TOO_LARGE:
                    raise PressRefused(
                        f"{ref}'s diff is too large to compare, so no Merge was recorded"
                    )
                if not digest:
                    raise PressRefused(f"{ref}'s diff could not be read, so no Merge was recorded")
                pulls[ref] = {"digest": digest, "head": pull.head}
            if not pulls:
                raise PressRefused("no pull request is open, so no Merge was recorded")
            entry["asked"] = {"at": self.ports.clock(), "pulls": pulls}
            if self.ports.press_is_review:
                # Nothing else here can review: the person pressing Merge has looked at these
                # diffs, so the press passes them at the heads it was for (#3623). Tests must
                # still be green there before anything merges.
                passes = entry.setdefault("passes", {})
                for ref, pressed in pulls.items():
                    passes[ref] = {**pressed, "at": self.ports.clock(), "by": "a Merge press"}
            entry.pop("asked_cleared", None)
            self.store.put(work_id, entry)
        return await self.evaluate(work_id)

    async def withdraw_merge(self, work_id: str) -> str:
        """A person took their Merge press back (#3320): the Work waits for a new one.

        Under the Work's lock, so a merge already under way finishes first and is answered.
        """
        if not self.handles(work_id):
            return "not released by the deck"
        async with self._lock(work_id):
            entry = self.store.get(work_id)
            if not entry.get("asked"):
                refs = sorted(set(self.ports.pulls(work_id)))
                states = [getattr(await self.ports.read_pull(ref), "state", "") for ref in refs]
                if "MERGED" in states and "OPEN" not in states:
                    return "already merged; nothing changed"
                return "no Merge approval stands; nothing changed"
            for key in ("asked", "press_waits", "asked_cleared"):
                entry.pop(key, None)
            self.store.put(work_id, entry)
        log.info("work-release: %s: Merge approval withdrawn", work_id)
        answer = await self.evaluate(work_id)
        return f"Merge approval withdrawn; {answer}" if answer else "Merge approval withdrawn"

    def approval(self, work_id: str) -> str:
        """What the Work page offers for an early Merge (#3320): "" for nothing (not released
        here, or a project that merges on its own), ``offered`` when no press stands, and
        ``approved`` while one does."""
        if not self.handles(work_id):
            return ""
        try:
            if self.ports.policy(_repository_of(work_id)).auto_release:
                return ""
            entry = self.store.works.get(work_id) or {}
        except Exception:  # noqa: BLE001 -- unreadable: nothing offered
            return ""
        return MERGE_APPROVED if entry.get("asked") else MERGE_OFFERED

    def approval_cleared(self, work_id: str) -> str:
        """The Work page's line once a changed diff cleared a press (#3320), else ""."""
        try:
            cleared = (self.store.works.get(work_id) or {}).get("asked_cleared")
        except Exception:  # noqa: BLE001 -- unreadable: no line
            return ""
        if not isinstance(cleared, dict) or not cleared.get("pull"):
            return ""
        if cleared.get("why") == "changed":
            what = f"{cleared['pull']} changed at {str(cleared.get('head') or '')[:12]}"
        else:
            what = f"{cleared['pull']} joined the Work"
        return f"Merge approval cleared: {what}."

    async def _check_press(self, work_id: str, entry: dict, facts: dict[str, Pull]) -> None:
        """Clear a person's Merge press once a diff it was for changed (#3320).

        A head that moved is read once: the same digest (a merge of the base) moves the
        press's head, a changed or too-large one clears it, and a failed read changes nothing.
        An open pull request the press never covered clears it too.
        """
        pulls = _asked_pulls(entry)
        if pulls is None:
            return
        for ref in sorted(facts):
            pull = facts[ref]
            if pull.state != "OPEN":
                continue
            row = pulls.get(ref)
            if row is None:
                return self._clear_press(work_id, entry, ref, pull.head, "joined the Work")
            if row.get("head") and same_commit(pull.head, str(row["head"])):
                continue
            current = await self.ports.digest(ref, pull.head)
            if not current:
                continue
            if current == row.get("digest"):
                row["head"] = pull.head
                continue
            return self._clear_press(work_id, entry, ref, pull.head, "changed")
        return None

    def _clear_press(self, work_id: str, entry: dict, ref: str, head: str, why: str) -> None:
        log.info("work-release: %s: %s %s at %s; Merge approval cleared", work_id, ref, why, head)
        entry.pop("asked", None)
        entry.pop("press_waits", None)
        entry["asked_cleared"] = {"pull": ref, "head": head, "why": why, "at": self.ports.clock()}

    async def on_pull_event(self, work_id: str, pull: str, action: str, merged: bool) -> None:
        """A ``pull_request`` delivery on a pull request this Work registered."""
        if action == "closed" and not merged:
            # Not looked at again, so its call to verify goes with it (#2999 review); and a
            # closed pull request first in line held every merge behind it until the Work's
            # next event (#3033).
            async with self._lock(work_id):
                entry = self.store.get(work_id)
                if entry.pop("needs_verify", None) is not None:
                    self.store.put(work_id, entry)
                if self.handles(work_id):
                    self._leave(work_id, refs=[pull.lower()])
            await self._wake_next(work_id)
            return
        if action == "closed" and merged and self.handles(work_id):
            # A merge seen as it happened, by the deck or by hand: once the Work has nothing
            # open, its agent checks it live (#3141). Only a fresh merge, never an evaluation
            # of a Work that merged long ago.
            async with self._lock(work_id):
                entry = self.store.get(work_id)
                entry["merged_now"] = True
                self.store.put(work_id, entry)
        await self.evaluate(work_id)

    async def on_label(self, work_id: str, label: str, added: bool) -> None:
        """A label went on or off the Work's issue: the conditions are read again."""
        del label, added  # ``_hold_reason`` reads the issue's labels itself
        await self.evaluate(work_id)

    async def on_status(self, repository: str, sha: str) -> None:
        """A commit status on ``sha`` in ``repository``: look again at the Works it gates (#3019).

        Drone reports through commit statuses, not check suites, and a status names a commit,
        not a pull request. Only a Work holding a review pass at that commit on a pull request
        whose checks gate it — any repository but a ``review_only`` one, as a running check
        holds every other merge (#3132) — can be moved by it, so only those are read.
        """
        repository = repository.lower()
        if len(sha) < 7:
            return
        for work_id in sorted(self.store.works):
            entry = self.store.works.get(work_id) or {}
            passes = entry.get("passes") or {}
            if not isinstance(passes, dict):
                continue
            policy = self.ports.policy(_repository_of(work_id))
            if repository in policy.review_only:
                continue
            if any(
                ref.partition("#")[0].lower() == repository
                and isinstance(passed, dict)
                and same_commit(sha, str(passed.get("head") or ""))
                for ref, passed in passes.items()
            ):
                try:
                    answer = await self.evaluate(work_id)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one Work's failure must not skip the next
                    log.warning(
                        "work-release: evaluating %s on a status failed", work_id, exc_info=True
                    )
                    continue
                log.info(
                    "work-release: %s on a status of %s@%s: %s",
                    work_id,
                    repository,
                    sha[:12],
                    answer,
                )

    async def on_tests(self, *, jobs: bool = True, chats: bool = True) -> None:
        """A job or a chat moved: look again at the Works that move could release.

        A Work waiting on its tests is looked at only when a job moved, and one waiting on a
        turn only when a chat moved and its chats are now idle (a cheap local check), so the
        deck's stream of chat changes costs no GitHub read for a Work waiting on a suite.
        """
        for work_id in sorted(self.store.works):
            entry = self.store.works.get(work_id) or {}
            if (
                (jobs and entry.get("waiting_tests"))
                or (chats and entry.get("waiting_turn") and not self.ports.busy(work_id))
                or self._moved_up(work_id, entry)
            ):
                await self.evaluate(work_id)
        await self._look_at_firsts()

    def _moved_up(self, work_id: str, entry: dict) -> bool:
        """Is a pull request held only by its place in line now first in it (#3034)?"""
        queued = entry.get("queued") or ()
        return any(ref in queued for ref in self._first(work_id))

    async def _look_at_firsts(self) -> None:
        """Evaluate each line's first that no wake mark of its own will bring back (#3034).

        Waiting on its tests or its turn, it has its own trigger. Otherwise nothing may: an
        evaluation that read GitHub badly, or that a reorder raced, leaves no mark, and the
        whole line waits behind it. Looked at again at most every ``QUEUE_LOOK_S``.
        """
        now = self.ports.clock()
        for rows in list(self.store.queues.values()):
            first = str(rows[0].get("work") or "") if rows else ""
            if not first:
                continue
            entry = self.store.works.get(first) or {}
            if entry.get("waiting_tests") or entry.get("waiting_turn"):
                continue
            if now - self._looked.get(first, float("-inf")) < QUEUE_LOOK_S:
                continue
            self._looked[first] = now
            self._wakes.add(first)
        await self._wake_next("")

    # --- the decision ---

    async def evaluate(self, work_id: str, *, tell_untested: bool = True) -> str:
        """Merge whatever this Work may merge now; answer what holds it, for a receipt.

        ``tell_untested=False`` leaves a reviewed head nothing tests marked ``untested`` rather
        than telling the agent: the caller's own message says it (#2999).

        A look that stops on a failed read leaves a ``retry`` mark, so it is looked at again
        on its own; any other answer clears it (#3023). Every answer is logged, so "why is
        this not Ready for merge?" can be read from the journal.
        """
        if not self.handles(work_id):
            await self._not_handled(work_id)
            return "not released by the deck"
        async with self._lock(work_id):
            self._looked[work_id] = self.ports.clock()
            entry = self.store.get(work_id)
            answer = await self._evaluate(work_id, entry, tell_untested)
            log.info("work-release: %s: %s", work_id, answer or "nothing holds it")
            _note_hold(entry, answer, self.ports.clock())
            if _failed_read(answer) and entry.get("passes"):
                # Without a pass nothing merges or reads Ready, and a verdict looks again.
                self._mark_retry(work_id, entry, answer)
            else:
                # Not marked again, so not left due: a due mark would spin the retry loop.
                entry.pop("retry", None)
            self.store.put(work_id, entry)
        places = self.store.place(work_id)
        if places:
            # Every hold of a Work in line, so a stalled queue can be read off the journal.
            log.info(
                "work-release: %s held in the merge queue (%s): %s",
                work_id,
                "; ".join(f"{ref} {place} of {size}" for ref, place, size, _r in places),
                answer,
            )
        await self._wake_next(work_id)
        return answer

    async def _not_handled(self, work_id: str) -> None:
        """A Work the deck no longer releases is not looked at again (#3023)."""
        if "retry" not in (self.store.works.get(work_id) or {}):
            return
        async with self._lock(work_id):
            entry = self.store.get(work_id)
            entry.pop("retry", None)
            self.store.put(work_id, entry)

    async def _hold_reason(
        self,
        work_id: str,
        entry: dict,
        policy: Policy,
        facts: dict[str, Pull],
        open_pulls: list[str],
        tell_untested: bool = True,
    ) -> str:
        """The first reason this Work may not merge now, or "" for none.

        In order: it is paused, on the deck or by its
        label; it carries the spec-only label; one of its chats is mid-turn; a pull request is
        a draft; one has no review pass at its head; one has no green tests at its head. Only
        the reason that stops it now leaves its wake mark (``waiting_turn``, ``waiting_tests``).
        """
        entry.pop("waiting_turn", None)
        entry.pop("waiting_tests", None)
        if self.ports.paused(work_id):
            return "the Work is paused"
        holding = [name for name in (policy.paused_label, policy.spec_only_label) if name]
        if holding and self.ports.issue_labels is not None:
            labels = await self.ports.issue_labels(work_id)
            if labels is None:
                return LABELS_UNREADABLE
            carried = [name for name in holding if name.casefold() in labels]
            if carried:
                return f"the issue carries {carried[0]}, so nothing merges"
        if self.ports.busy(work_id):
            # Its agent may still push (#2481): looked at again once its chats are idle.
            entry["waiting_turn"] = True
            return "the Work's agent is still working"
        if any(facts[ref].draft for ref in open_pulls):
            return "a pull request in the reviewer's repository is still a draft"
        passes = entry.get("passes") or {}
        green: set[str] | None = None
        for ref in open_pulls:
            pull = facts[ref]
            passed = passes.get(ref) or {}
            if passed and not same_commit(pull.head, str(passed.get("head") or "")):
                # A branch brought up to its base moves the head but not the diff the reviewer
                # read: the pass carries over when the digest at the new head is the same.
                if passed.get("digest"):
                    current = await self.ports.digest(ref, pull.head)
                    if not current:
                        # A failed diff read, or a head that moved between reads: not a
                        # changed diff, so not a lost pass either. Read again when a job
                        # moves, as its re-test finishing is what it waits on next.
                        entry["waiting_tests"] = True
                        return f"{ref}'s diff at {pull.head[:12]} {DIGEST_UNREADABLE}"
                    if current == passed["digest"]:
                        passed["head"] = pull.head
            if not same_commit(pull.head, str(passed.get("head") or "")):
                return f"{ref} has no review pass at {pull.head[:12]}"
            if pull.repository in policy.review_only:
                continue
            if pull.checks == "red":
                # Before anything else waits on it (#3163): no merge press, re-test or person
                # turns red checks green, so only the agent told here moves the Work.
                self._tell_red(work_id, entry, pull)
                return f"{ref}'s checks are red at {pull.head[:12]}"
            if pull.repository in policy.github_checks:
                if pull.checks != "green":
                    return f"{ref}'s checks are {pull.checks} at {pull.head[:12]}"
                continue
            if green is None:
                green = {sha.lower() for sha in await self.ports.green_heads(work_id)}
            if not any(same_commit(sha, pull.head) for sha in green):
                # Only a Work stopped here is looked at again when a job finishes.
                entry["waiting_tests"] = True
                if not await self._tested(work_id, pull.head):
                    # Only while nothing runs or failed there since: a red run is the answer
                    # then, not the green one that proved nothing (#3097 review).
                    moved = await self._moved(work_id, pull.head)
                    unclean = await self._unclean(work_id, pull.head)
                    # Read by the deck's next-step request, which then names this one ending.
                    entry["needs_verify"] = True
                    # Its review passed and nothing tests it: the agent may still be waiting
                    # for the review, or its job was cut off (#2999). Once per head.
                    key = f"{ref}@{pull.head[:12]}"
                    if tell_untested:
                        self._tell(
                            work_id,
                            entry,
                            f"verify:{key}",
                            REVIEW_PASSED_TEXT.format(ref=ref, head=pull.head[:12]),
                        )
                    else:
                        entry["untested"] = key
                    if unclean:
                        # Before ``moved``: a run on a clean tree is the first thing it needs.
                        self._tell(
                            work_id,
                            entry,
                            f"unclean:{key}",
                            UNCLEAN_TEXT.format(ref=ref, head=pull.head[:12]),
                        )
                        return f"{ref}'s tests passed at {pull.head[:12]} {TREE_UNCLEAN}"
                    if moved:
                        # Its own message, once per head: the call to verify above is usually
                        # spent by the pass before any run could move (#3097 review).
                        self._tell(
                            work_id,
                            entry,
                            f"moved:{key}",
                            MOVED_TEXT.format(ref=ref, head=pull.head[:12]),
                        )
                        return f"{ref}'s tests passed at {pull.head[:12]} {TREE_MOVED}"
                return f"{ref} has no green verification at {pull.head[:12]}"
            if pull.checks == "pending":
                # The verification job is narrow by design; the pull request's own CI at the
                # head is the rest of the proof, so a run still going holds it (#3132). A red
                # one was refused, and its agent told, above.
                return f"{ref}'s checks are pending at {pull.head[:12]}"
        return ""

    async def _tested(self, work_id: str, head: str) -> bool:
        """Is a verification of this Work running, or did one fail at ``head`` (#2999)?

        Either way its agent knows: a running job tells it when it ends, a failed one already
        did, and asking it to run the same head again would be wrong.
        """
        if self.ports.verifying is not None and await self.ports.verifying(work_id):
            return True
        if self.ports.failed_heads is None:
            return False
        red = await self.ports.failed_heads(work_id)
        return any(same_commit(sha.lower(), head) for sha in red)

    async def _moved(self, work_id: str, head: str) -> bool:
        """Did a passing verification run at ``head`` while its tree changed (#3097)?"""
        if self.ports.moved_heads is None:
            return False
        moved = await self.ports.moved_heads(work_id)
        return any(same_commit(sha.lower(), head) for sha in moved)

    async def _unclean(self, work_id: str, head: str) -> bool:
        """Did a passing verification run at ``head`` from an unclean tree (#3397)?"""
        if self.ports.unclean_heads is None:
            return False
        unclean = await self.ports.unclean_heads(work_id)
        return any(same_commit(sha.lower(), head) for sha in unclean)

    async def _evaluate(self, work_id: str, entry: dict, tell_untested: bool = True) -> str:
        # Set again only where a reviewed head is found untested now (#2999 review): a pull
        # request gone back to draft, closed or unreadable asks for no verification.
        entry.pop("untested", None)
        entry.pop("needs_verify", None)
        # Set again by ``_merge_one`` for each pull request still behind another in line. Gone
        # on an unreadable answer too, so a failed read is not retried on every chat move: the
        # look at each line's first brings it back (#3034).
        entry.pop("queued", None)
        policy = self.ports.policy(_repository_of(work_id))
        refs = sorted(set(self.ports.pulls(work_id)))
        if not refs:
            # Not knowing the pull requests is not knowing they landed: a web process that
            # has not scanned the Work's chats yet reads none, and spending the passes then
            # lost every reviewed Work at a restart (#2982). Nothing changes until it knows.
            return NO_PULLS_KNOWN
        facts: dict[str, Pull] = {}
        for ref in refs:
            read = await self.ports.read_pull(ref)
            if read is None:
                # Not an answer: a Ready for merge stands (the card checks its heads itself)
                # rather than turning into an ending nobody asked about (#2960 review).
                return f"{ref} could not be read"
            facts[ref] = read
        if not policy.auto_release:
            await self._check_press(work_id, entry, facts)
        # Set again below only while a person's Merge is all that is missing (#2960).
        awaiting = entry.pop("awaiting_merge", None)
        # A draft is not yet offered (D8): left out of what merges, so a Work whose next step is
        # open as a draft still merges its ready one. A draft in a review-only repository stays
        # in, as a hold, so the reviewer's repository still merges first.
        open_pulls = [
            ref
            for ref in refs
            if facts[ref].state == "OPEN"
            and (not facts[ref].draft or facts[ref].repository in policy.review_only)
        ]
        if not open_pulls:
            # Nothing offered is open: everything landed, or only a draft next step is left. A
            # Merge press or an update was about the pull requests it was made for, never the
            # Work's next ones, so a draft that goes ready later waits for its own press.
            if not any(facts[ref].state == "OPEN" for ref in refs):
                # Everything landed or closed. A press on an open draft stays for it (#3320).
                entry.pop("asked", None)
                entry.pop("asked_cleared", None)
            for key in (
                "updated",
                "update_at",
                "unlanded",
                "waiting_tests",
                "waiting_turn",
                "queued",
            ):
                entry.pop(key, None)
            self._leave(work_id)
            if any(facts[ref].state == "OPEN" for ref in refs):
                return "only drafts are open"
            # Every pull request landed or closed: its passes are spent, and kept they would
            # make every start read them again (#2960). Only the ones read as landed: a pass
            # for a pull request missing from a partial list stays for when it is known (#2982).
            passes = entry.get("passes") or {}
            for ref in refs:
                passes.pop(ref, None)
            if not passes:
                entry.pop("passes", None)
            # A merge seen live is kept through a failed read or a pull request still open,
            # until a look finds the Work with nothing open (#3141).
            if entry.pop("merged_now", None):
                self._ask_live_check(
                    work_id, entry, policy, [r for r in refs if facts[r].state == "MERGED"]
                )
            return "no open pull request"
        # A pull request that left the Work's offer (closed, landed, a draft again) leaves the
        # merge queue too.
        self._leave(work_id, keep=open_pulls)
        reason = await self._hold_reason(work_id, entry, policy, facts, open_pulls, tell_untested)
        passes = entry.get("passes") or {}
        reviewed = all(
            same_commit(facts[ref].head, str((passes.get(ref) or {}).get("head") or ""))
            for ref in open_pulls
        )
        if not policy.auto_release and entry.get("asked"):
            if reviewed and not _press_stands(entry, open_pulls):
                # Reviewed again at a diff the person never pressed Merge for (#3175).
                log.info("work-release: %s: the diff changed since Merge was pressed", work_id)
                entry.pop("asked", None)
            else:
                # Read by the Work's line: a press waiting on its review or re-test (#3175,
                # #3320).
                entry["press_waits"] = True
        if (
            _failed_read(reason)
            and not reason.endswith(DIGEST_UNREADABLE)  # the head moved off the Ready one
            and awaiting is not None
        ):
            entry["awaiting_merge"] = awaiting  # a failed read is no answer either
        if reason:
            return await self._queue_hold(work_id, entry, policy, facts, reason)
        if not policy.auto_release and not _press_stands(entry, open_pulls):
            # The Work page reads this as Ready for merge (#2960). Not in line until pressed.
            self._leave(work_id)
            entry["awaiting_merge"] = {ref: facts[ref].head for ref in open_pulls}
            heads = [f"{ref}@{facts[ref].head[:12]}" for ref in open_pulls]
            self._tell(
                work_id, entry, f"ready:{','.join(heads)}", READY_TEXT.format(refs=", ".join(heads))
            )
            return AWAITING_MERGE
        first = [ref for ref in open_pulls if facts[ref].repository in policy.review_only]
        rest = [ref for ref in open_pulls if ref not in first]
        said: list[str] = []
        for tier in (first, rest):
            if not tier:
                continue
            landed = True
            for ref in tier:
                done, line = await self._merge_one(work_id, entry, policy, facts[ref])
                said.append(line)
                landed = landed and done
            if not landed:
                if tier is first and rest:
                    said.append("the rest wait for the reviewer's repository to merge first")
                break
        else:
            # Everything this pass was for has landed: any update was for these pull requests.
            # The press stays only for a draft it covered that is still open (#3320 D6), which
            # then merges unchanged with no second press; any other draft waits for its own.
            covered = _asked_pulls(entry) or {}
            if not any(
                facts[ref].state == "OPEN"
                for ref in covered
                if ref in facts and ref not in open_pulls  # these just merged
            ):
                entry.pop("asked", None)
            for key in (
                "asked_cleared",
                "updated",
                "update_at",
                "unlanded",
                "press_waits",
            ):
                entry.pop(key, None)
            if not any(facts[ref].state == "OPEN" for ref in refs if ref not in open_pulls):
                # Nothing is left open, not even a draft next step: the Work merged (#3141).
                entry.pop("merged_now", None)
                merged = [ref for ref in refs if facts[ref].state == "MERGED"]
                self._ask_live_check(work_id, entry, policy, [*merged, *open_pulls])
        return "; ".join(said)

    async def _merge_one(
        self, work_id: str, entry: dict, policy: Policy, pull: Pull
    ) -> tuple[bool, str]:
        """Merge one pull request now if it is mergeable; ``(merged, what happened)``."""
        ref, key = pull.ref, f"{pull.ref}@{pull.head[:12]}"
        if pull.mergeable == "CONFLICTING" or pull.merge_state == "DIRTY":
            self._tell(
                work_id,
                entry,
                f"conflict:{key}",
                f"AgentDeck: {ref} conflicts with its base at {pull.head[:12]}, so it cannot"
                " merge. Merge the base in, resolve the conflict, verify, push, and mark it"
                " ready again.",
            )
            self._leave(work_id, refs=[ref])
            return False, f"{ref} conflicts"
        if pull.checks == "red":
            self._tell_red(work_id, entry, pull)
            return False, f"{ref}'s checks are red"
        queued = pull.repository not in policy.review_only
        if (entry.get("unlanded") or {}).get(ref) == pull.head:
            # The deck gave up updating this head (#3033): back in line only with a new one.
            return False, f"{ref} is behind its base; its agent merges the base in"
        if queued:
            # One pull request per repository at a time (#3022): only the first in line is
            # brought up to its base and re-tested, so each merge costs one re-test, not one
            # per reviewed pull request waiting behind it.
            place, size = self._join(work_id, pull.repository, ref)
            ahead = self.store.queues.get(pull.repository) or []
            while place > 1 and ahead and not self.handles(str(ahead[0].get("work") or "")):
                # Nothing evaluates a Work the deck stopped releasing: it would hold the line.
                self._leave(str(ahead[0].get("work") or ""))
                place, size = self._join(work_id, pull.repository, ref)
            if place > 1:
                # Its mark: once it is first, however it got there, the next job or chat move
                # looks at it again (#3034).
                queued = entry.setdefault("queued", [])
                if ref not in queued:
                    queued.append(ref)
                return False, f"{ref} is in the merge queue, {place} of {size}"
        if pull.behind_by is None and pull.merge_state != "BEHIND":
            # Whether it is up to date could not be read: never taken as "it is".
            return False, f"{ref} could not be compared with its base"
        if pull.behind:
            if (entry.get("updated") or {}).get(ref) == pull.head:
                return await self._unlanded(work_id, entry, pull, queued)
            if await self.ports.update_branch(ref):
                # Written only once it worked, so a failed update is tried again.
                entry.setdefault("updated", {})[ref] = pull.head
                entry.setdefault("update_at", {})[ref] = [self.ports.clock(), 1]
                self._look_later(work_id, UPDATE_GRACE_S)
                # Its re-test is what it waits on now: a finished job looks again.
                entry["waiting_tests"] = True
                rounds = self._round(pull.repository, ref) if queued else 0
                self._tell(
                    work_id,
                    entry,
                    f"behind:{key}",
                    f"AgentDeck: {ref} was behind its base, so the deck merged the base"
                    " into it. That is a new head: run the verification on it so it can"
                    " merge; the review carries over only if the diff did not change."
                    + (
                        " A person pressed Merge: the press carries over with the review, so"
                        " the deck merges once the new head is green."
                        if not policy.auto_release
                        else ""
                    )
                    + (
                        f" It is first in the merge queue (round {rounds}): the pull"
                        " requests behind it wait until it merges or leaves the queue."
                        if queued
                        else ""
                    ),
                )
            else:
                self._leave(work_id, refs=[ref])
                self._tell(
                    work_id,
                    entry,
                    f"update-failed:{key}",
                    f"AgentDeck: {ref} is behind its base and the deck could not update"
                    " it (a conflict or a protected branch). Merge the base in, verify,"
                    " push, and mark it ready again.",
                )
            return False, f"{ref} is behind its base"
        outcome, words = await self.ports.merge(ref, policy.merge_method, pull.head)
        if outcome in (MERGED, ALREADY_MERGED):
            self._leave(work_id, refs=[ref])
        if outcome == MERGED:
            log.info("work-release: merged %s at %s (Work %s)", ref, pull.head[:12], work_id)
            return True, f"{ref} merged at {pull.head[:12]}"
        if outcome == ALREADY_MERGED:
            return True, f"{ref} was already merged"
        # Out of line, so the next one goes; the next event puts it back at the end.
        self._leave(work_id, refs=[ref])
        self._tell(
            work_id,
            entry,
            f"merge:{key}",
            f"AgentDeck: GitHub refused to merge {ref} at {pull.head[:12]}"
            f" ({words.strip()[:300] or 'no reason given'}). The deck tries again on the next"
            " event; fix what holds it, or merge it by hand.",
        )
        return False, f"GitHub refused to merge {ref}"

    async def _unlanded(
        self, work_id: str, entry: dict, pull: Pull, queued: bool
    ) -> tuple[bool, str]:
        """The deck updated this head and GitHub still shows it, behind (#3033).

        ``gh pr update-branch`` can answer success and never move the branch: #3027 kept its
        one commit for hours, first in line, and every merge behind it waited. After
        ``UPDATE_GRACE_S`` the update is asked for once more; after a second grace with no move
        the pull request leaves the line and its agent is told to merge the base in itself
        (operator decision: retry once, then drop). Looked at again when a job moves, as nothing
        else would come by.
        """
        ref, key = pull.ref, f"{pull.ref}@{pull.head[:12]}"
        entry["waiting_tests"] = True
        record = (entry.get("update_at") or {}).get(ref)
        at, tries = record if isinstance(record, list) and len(record) == 2 else (None, 1)
        now = self.ports.clock()
        if at is None:
            # Updated before this was kept (or by a deck that kept no time): start the grace now.
            entry.setdefault("update_at", {})[ref] = [now, int(tries or 1)]
            self._look_later(work_id, UPDATE_GRACE_S)
            return False, f"{ref} is behind its base; GitHub has not shown the update yet"
        if now - float(at) < UPDATE_GRACE_S:
            self._look_later(work_id, UPDATE_GRACE_S - (now - float(at)))
            return False, f"{ref} is behind its base; GitHub has not shown the update yet"
        if int(tries or 1) < UPDATE_TRIES and await self.ports.update_branch(ref):
            entry.setdefault("update_at", {})[ref] = [now, int(tries or 1) + 1]
            self._look_later(work_id, UPDATE_GRACE_S)
            log.info("work-release: updating %s again: its head stayed at %s", ref, pull.head[:12])
            return False, f"{ref} is behind its base; the deck asked GitHub to update it again"
        entry.pop("waiting_tests", None)
        (entry.get("update_at") or {}).pop(ref, None)
        entry.setdefault("unlanded", {})[ref] = pull.head
        if queued:
            self._leave(work_id, refs=[ref])
        self._tell(
            work_id,
            entry,
            f"update-unlanded:{key}",
            f"AgentDeck: {ref} is behind its base and the deck's update of it never landed on"
            f" GitHub (the head is still {pull.head[:12]})"
            + (", so it left the merge queue and the next pull request goes" if queued else "")
            + ". Merge the base in yourself, verify, push, and mark it ready again.",
        )
        return False, f"{ref} is behind its base; the deck's update never landed"

    def _look_later(self, work_id: str, delay: float) -> None:
        """Evaluate this Work again after ``delay`` seconds, once (#3033 review).

        Nothing else comes by on a quiet deck: the pull requests behind it are green and run no
        jobs, so a job moving is no clock. A later call replaces the earlier timer; a restart
        loses it, and the start's look at every reviewed Work sets it again.
        """
        old = self._timers.pop(work_id, None)
        if old is not None:
            with contextlib.suppress(Exception):
                old.cancel()  # type: ignore[attr-defined]

        async def look() -> None:
            self._timers.pop(work_id, None)
            try:
                await self.evaluate(work_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("work-release: a timed look at %s failed", work_id, exc_info=True)
                # A look that raised is a failed read too: looked at again after the back-off.
                async with self._lock(work_id):
                    entry = self.store.get(work_id)
                    if entry.get("passes"):
                        self._mark_retry(work_id, entry, "the look raised")
                        self.store.put(work_id, entry)

        delay = max(1.0, float(delay))
        if self.ports.later is not None:
            self._timers[work_id] = self.ports.later(delay, look)
            return
        loop = asyncio.get_running_loop()

        def fire() -> None:
            task = loop.create_task(look(), name=f"work-release-look:{work_id}")
            self._looks.add(task)  # held, so the running look is not collected
            task.add_done_callback(self._looks.discard)

        self._timers[work_id] = loop.call_later(delay, fire)

    # --- the merge queue (#3022) ---

    def _join(self, work_id: str, repository: str, ref: str) -> tuple[int, int]:
        """Put ``ref`` at the end of its repository's line unless it is in it; its place.

        A starred Work's pull goes behind the other starred ones instead (#3116).
        """
        rows = self.store.queues.setdefault(repository, [])
        for index, row in enumerate(rows):
            if row.get("pull") == ref:
                return index + 1, len(rows)
        row = {"pull": ref, "work": work_id, "at": self.ports.clock(), "rounds": 0}
        at = self._starred_place(rows) if self.ports.starred(work_id) else len(rows)
        rows.insert(at, row)
        self.store.put_queues()
        return at + 1, len(rows)

    def _starred_place(self, rows: list[dict]) -> int:
        """Where a starred Work's pull goes in ``rows``: behind every starred Work's, but never
        before the first in line, which may already be being brought up to its base (#3116)."""
        at = 1 if rows else 0
        for index, row in enumerate(rows):
            if index and self.ports.starred(str(row.get("work") or "")):
                at = index + 1
        return at

    def star(self, work_id: str) -> None:
        """Move a newly starred Work's queued pulls up behind the starred ones (#3116)."""
        changed = False
        for rows in self.store.queues.values():
            mine = [row for index, row in enumerate(rows) if index and row.get("work") == work_id]
            if not mine:
                continue
            rest = [row for row in rows if row not in mine]
            at = self._starred_place(rest)
            moved = rest[:at] + mine + rest[at:]
            if moved != rows:
                rows[:] = moved
                changed = True
        if changed:
            self.store.put_queues()

    def _round(self, repository: str, ref: str) -> int:
        """Count one more update of the first in line; the round it is now on."""
        for row in self.store.queues.get(repository) or []:
            if row.get("pull") == ref:
                row["rounds"] = int(row.get("rounds") or 0) + 1
                self.store.put_queues()
                return row["rounds"]
        return 0

    def _leave(
        self, work_id: str, *, refs: Iterable[str] | None = None, keep: Iterable[str] = ()
    ) -> None:
        """Take this Work's pull requests out of line: ``refs``, else all but ``keep``.

        A Work that becomes first in its line is evaluated once the caller's lock is let go,
        since nothing else would bring it up to its base.
        """
        named = None if refs is None else set(refs)
        kept = set(keep)
        changed = False
        for rows in self.store.queues.values():
            first = rows[0].get("work") if rows else None
            remaining = [
                row
                for row in rows
                if row.get("work") != work_id
                or row.get("pull") in kept
                or (named is not None and row.get("pull") not in named)
            ]
            if len(remaining) == len(rows):
                continue
            changed = True
            rows[:] = remaining
            if rows and rows[0].get("work") != first:
                self._wakes.add(str(rows[0].get("work") or ""))
        if changed:
            self.store.put_queues()

    def _first(self, work_id: str) -> list[str]:
        """This Work's pull requests that are first in their line."""
        return [
            str(rows[0].get("pull"))
            for rows in self.store.queues.values()
            if rows and rows[0].get("work") == work_id
        ]

    async def _queue_hold(
        self, work_id: str, entry: dict, policy: Policy, facts: dict[str, Pull], reason: str
    ) -> str:
        """What a held Work's place in line becomes; the reason, with its place.

        It keeps its place only while it waits on its own tests or turn (the first in line
        re-testing after an update), on GitHub's checks, or on an unreadable label. A failed
        re-test, or anything else that stops it (no pass at the head, a draft, a pause, a
        Poller card), takes it out of line and the next one goes (operator decision).
        """
        places = self.store.place(work_id)
        if not places:
            return reason
        pending = any(
            facts[pull].checks == "pending"
            for pull, *_ in places
            if pull in facts and facts[pull].repository not in policy.review_only
        )
        waiting = entry.get("waiting_tests") or entry.get("waiting_turn")
        unreadable = _failed_read(reason)
        if not (waiting or pending or unreadable):
            self._leave(work_id)
            return reason
        if entry.get("waiting_tests") and self.ports.failed_heads is not None:
            first = [pull for pull in self._first(work_id) if pull in facts]
            if first:
                red = {sha.lower() for sha in await self.ports.failed_heads(work_id)}
                for pull in first:
                    head = facts[pull].head
                    if any(same_commit(sha, head) for sha in red):
                        self._leave(work_id, refs=[pull])
                        self._tell(
                            work_id,
                            entry,
                            f"queue-red:{pull}@{head[:12]}",
                            f"AgentDeck: {pull}'s verification failed at {head[:12]}, so it"
                            " left the merge queue and the next pull request goes. Fix it,"
                            " push, and mark it ready again; once reviewed and green it joins"
                            " the end of the queue.",
                        )
        places = self.store.place(work_id)
        said = [f"merge queue {place} of {size}" for _p, place, size, _r in places]
        return reason + (f" ({'; '.join(said)})" if said else "")

    async def _wake_next(self, work_id: str) -> None:
        """Evaluate each Work that became first in a merge queue (#3022)."""
        while self._wakes:
            woken = self._wakes.pop()
            if not woken:
                continue
            if woken == work_id and not self._moved_up(
                work_id, self.store.works.get(work_id) or {}
            ):
                # Its own evaluation moved it up and went on as first. One that still saw it
                # behind (a reorder raced it, #3034) is looked at again.
                continue
            if not self.handles(woken):
                # A Poller card took it, say: the deck never evaluates it again, so it would
                # hold the line for good. Out, and the one behind it goes.
                self._leave(woken)
                continue
            try:
                await self.evaluate(woken)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("work-release: waking %s in the queue failed", woken, exc_info=True)

    def queue(self, repository: str) -> list[dict]:
        """A repository's merge queue, first in line first (copies)."""
        return [dict(row) for row in self.store.queues.get(repository.lower()) or []]

    def reorder(self, repository: str, pulls: list[str]) -> list[dict]:
        """Put a repository's line in the order a person asked for (#3022).

        ``pulls`` names pull requests (``owner/repo#n`` or ``#n``) in their new order; the ones
        it leaves out keep their order after them. Unknown names raise ``ValueError``.
        """
        repository = repository.lower()
        rows = self.store.queues.get(repository) or []
        by_pull = {row["pull"]: row for row in rows}
        wanted: list[str] = []
        for name in pulls:
            ref = _match_in(repository, name)
            if ref not in by_pull:
                raise ValueError(f"{name} is not in {repository}'s merge queue")
            if ref not in wanted:
                wanted.append(ref)
        first = rows[0].get("work") if rows else None
        rows[:] = [by_pull[ref] for ref in wanted] + [r for r in rows if r["pull"] not in wanted]
        self.store.queues[repository] = rows
        self.store.put_queues()
        if rows and rows[0].get("work") != first:
            self._wakes.add(str(rows[0].get("work") or ""))
        return self.queue(repository)

    async def wake_first(self) -> None:
        """Evaluate the Works a reorder moved to the front."""
        await self._wake_next("")

    # --- helpers ---

    def _lock(self, work_id: str) -> asyncio.Lock:
        return self.ports.locks.setdefault(work_id, asyncio.Lock())

    def _verdict_tier(self, work_id: str):
        """The tier a ``/reviewed`` needs on this Work's project, as the Poller decides it.

        Release only where a pass can merge with nobody in the loop (``auto_release``); where a
        person's Merge press still follows it, Work (#2375). #2895 asked Release everywhere, so
        every verdict of one project's Work-tier reviewer was thrown away (#3157).
        """
        from .poller_engine.authorization import Tier, required_tier
        from .poller_engine.intake import CommandKind

        policy = self.ports.policy(_repository_of(work_id))
        tier = required_tier(CommandKind.REVIEWED.value, reviewed_merges=policy.auto_release)
        return tier or Tier.RELEASE

    async def _holds(self, work_id: str, login: str, tier) -> bool:
        """Does ``login`` hold ``tier`` on this Work's project, as the Poller decides it?

        The manifest's allowlist first; otherwise GitHub's live collaborator permission at or
        above the tier's role bar (``authorization.decide``). Unreadable is not a grant.
        """
        from .poller_engine.authorization import Verdict, decide

        repository = _repository_of(work_id)
        policy = self.ports.policy(repository).authorization
        if policy is None:
            return False
        verdict = decide(policy, tier, login=login, association=None, permission=None)
        if verdict is Verdict.UNCERTAIN and self.ports.permission is not None:
            level = await self.ports.permission(repository, login)
            verdict = decide(policy, tier, login=login, association=None, permission=level)
        return verdict is Verdict.AUTHORIZED

    async def holds_work(self, repository: str, login: str, association: str = "") -> bool:
        """Does ``login`` hold the Work tier on this switched repository (#2895 review)?

        The router asks before forwarding a person's comment to a Work's agent, as the Poller
        asks before acting on a ``/claude``. The allowlist and the reviewer App first, a
        comment author with no relationship to the repository refused without a lookup, then
        GitHub's live permission against the role bar. Unreadable is not a grant.
        """
        from .poller_engine.authorization import Tier, Verdict, decide

        policy = self.ports.policy(repository.lower()).authorization
        if policy is None:
            return False
        verdict = decide(policy, Tier.WORK, login=login, association=association or None)
        if verdict is Verdict.UNCERTAIN and self.ports.permission is not None:
            level = await self.ports.permission(repository.lower(), login)
            verdict = decide(
                policy, Tier.WORK, login=login, association=association or None, permission=level
            )
        return verdict is Verdict.AUTHORIZED

    def _tell_red(self, work_id: str, entry: dict, pull: Pull) -> None:
        """Tell the Work's agent once per head that its pull request's checks failed."""
        self._tell(
            work_id,
            entry,
            f"red:{pull.ref}@{pull.head[:12]}",
            f"AgentDeck: {pull.ref}'s GitHub checks failed at {pull.head[:12]}, so it will not"
            " merge. Fix them, push, and mark it ready again.",
        )
        self._leave(work_id, refs=[pull.ref])

    def _tell(self, work_id: str, entry: dict, key: str, text: str) -> bool:
        """Send ``text`` to the Work's chat once per ``key``; whether it was sent now."""
        told = entry.setdefault("told", [])
        if key in told:
            return False
        chat = self.ports.chat(work_id)
        if not chat:
            return False
        if self.ports.tell(chat, f"work-release:{work_id}:{key}", text) is False:
            log.warning("work-release: could not store %s for %s", key, work_id)
            return False
        told.append(key)
        del told[:-200]
        return True

    def _ask_live_check(
        self, work_id: str, entry: dict, policy: Policy, merged: Iterable[str]
    ) -> None:
        """Ask the Work's agent once to check its merged change live (#3141).

        Keyed by the merged pull requests, so the deck's own merge and GitHub's event for it
        are one message. ``live_check_asked`` is what the Work's status line reads: from then
        until the agent records a result, a closed issue is not yet a person's Finish.
        """
        refs = sorted(set(merged))
        if not refs:
            return
        text = LIVE_CHECK_TEXT.format(
            refs=", ".join(refs), deploy=deploy_sentence(policy, refs)
        )
        if self._tell(work_id, entry, f"live-check:{','.join(refs)}", text):
            entry["live_check_asked"] = {"at": self.ports.clock(), "pulls": refs}


def _asked_pulls(entry: dict) -> dict[str, dict] | None:
    """The press's ``pull -> {"digest", "head"}``, in place; ``None`` for no press or one
    from a deck that kept no digests (``True``).

    A press from before #3320 (``pull -> digest``) is rewritten in the new shape with no head,
    so its next look reads each digest once.
    """
    asked = entry.get("asked")
    if not isinstance(asked, dict) or not asked:
        return None
    if not isinstance(asked.get("pulls"), dict):
        asked = {
            "at": None,
            "pulls": {ref: {"digest": str(d), "head": ""} for ref, d in asked.items() if d},
        }
        entry["asked"] = asked
    return asked["pulls"]


def _press_stands(entry: dict, open_pulls: Iterable[str]) -> bool:
    """Does a person's Merge press cover these pull requests at the diffs passed now (#3175)?"""
    if entry.get("asked") is True:
        return True  # pressed on a deck that kept no digests: for whatever is open
    pulls = _asked_pulls(entry)
    if not pulls:
        return False
    passes = entry.get("passes") or {}
    return all(
        (pulls.get(ref) or {}).get("digest")
        and (pulls.get(ref) or {}).get("digest") == (passes.get(ref) or {}).get("digest")
        for ref in open_pulls
    )


def _note_hold(entry: dict, answer: str, now: float) -> None:
    """Keep what holds the Work now in plain words, and since when it has (#3097).

    The Work's line reads it when the agent's only wait is its own pull request merging, which
    says nothing about what that pull request waits for. ``since`` stays while the answer does,
    so a hold nothing moves for hours reads as stuck.
    """
    pressed = entry.pop("press_waits", None)
    if answer == NO_PULLS_KNOWN or _failed_read(answer):
        return  # no answer: the last one stands, and so does its time
    if not answer or answer == AWAITING_MERGE:
        entry.pop("hold", None)
        return
    words, person = hold_words(answer, needs_verify=bool(entry.get("needs_verify")))
    if pressed and not person:
        words = f"Merge pressed; {words}"
    old = entry.get("hold")
    since = old.get("since") if isinstance(old, dict) and old.get("why") == answer else None
    entry["hold"] = {"why": answer, "words": words, "person": person, "since": since or now}


def hold_words(answer: str, *, needs_verify: bool = False) -> tuple[str, bool]:
    """``evaluate``'s answer as a status line says it, and whether only a person moves it."""
    if answer == "the Work is paused":
        return "the Work is paused", True
    if answer.startswith("the issue carries ") and answer.endswith(", so nothing merges"):
        label = answer.removeprefix("the issue carries ").removesuffix(", so nothing merges")
        return f"held: {label} label", True
    if answer == "the Work's agent is still working":
        return "its agent is still working", False
    if "still a draft" in answer:
        return "still a draft", False
    if " has no review pass at " in answer:
        return "not reviewed at its head", False
    if TREE_MOVED in answer:  # a place in the merge queue may follow it
        return "tests passed but the tree changed during the run, so they don't count", False
    if TREE_UNCLEAN in answer:
        return "tests passed but the checkout had uncommitted or untracked files", False
    if " has no green verification at " in answer:
        return ("no test run started" if needs_verify else "its tests are not green"), False
    if "'s checks are " in answer:
        return "its checks are " + answer.split("'s checks are ", 1)[1].split(" at ")[0], False
    return answer, False


class Policies:
    """Each switched repository's merge policy, re-read when a manifest moves."""

    def __init__(self, poller_root: Path | str) -> None:
        self.root = Path(poller_root).expanduser()
        self._signature: tuple | None = None
        self._policies: dict[str, Policy] = {}

    def __call__(self, repository: str) -> Policy:
        found = []
        for path in sorted(self.root.glob("instances/*/poller.toml")):
            with contextlib.suppress(OSError):
                stat = path.stat()
                found.append((str(path), stat.st_mtime_ns, stat.st_size))
        signature = tuple(found)
        if signature != self._signature:
            from .poller_ops import release_policies

            self._policies = release_policies(self.root)  # type: ignore[assignment]
            self._signature = signature
        return self._policies.get(repository.lower(), Policy())


# --- GitHub, through the web process's one gh entry point ---

_GH_TIMEOUT_S = 30.0


async def read_pull(ref: str) -> Pull | None:
    """One pull request's state as GitHub has it now, or ``None`` when unreadable."""
    from . import gh_cli

    repository, _, number = ref.partition("#")
    fields = "state,isDraft,headRefOid,baseRefName,mergeable,mergeStateStatus,statusCheckRollup"
    try:
        code, out = await gh_cli.run(
            ["gh", "pr", "view", number, "--repo", repository, "--json", fields],
            timeout=_GH_TIMEOUT_S,
        )
        raw = json.loads(out) if code == 0 else None
    except (OSError, TimeoutError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    head = str(raw.get("headRefOid") or "")
    base = str(raw.get("baseRefName") or "")
    return Pull(
        behind_by=await _behind_by(repository, base, head) if base and head else None,
        ref=ref.lower(),
        state=str(raw.get("state") or "").upper(),
        draft=bool(raw.get("isDraft")),
        head=str(raw.get("headRefOid") or "").lower(),
        mergeable=str(raw.get("mergeable") or "").upper(),
        merge_state=str(raw.get("mergeStateStatus") or "").upper(),
        checks=checks_state(raw.get("statusCheckRollup")),
    )


async def permission(repository: str, login: str) -> str | None:
    """A login's collaborator permission on a repository, or ``None`` when unreadable."""
    from . import gh_cli

    try:
        code, out = await gh_cli.run(
            ["gh", "api", f"repos/{repository}/collaborators/{login}/permission"],
            timeout=_GH_TIMEOUT_S,
            repo=repository,
        )
        raw = json.loads(out) if code == 0 else None
    except (OSError, TimeoutError, ValueError):
        return None
    level = raw.get("permission") if isinstance(raw, dict) else None
    return level if isinstance(level, str) else None


async def issue_labels(work_id: str) -> set[str] | None:
    """The Work's issue labels, casefolded; ``None`` when unreadable."""
    from . import gh_cli

    ref = work_id.partition(":")[2]
    repository, _, number = ref.rpartition("#")
    try:
        code, out = await gh_cli.run(
            ["gh", "issue", "view", number, "--repo", repository, "--json", "labels"],
            timeout=_GH_TIMEOUT_S,
        )
        raw = json.loads(out) if code == 0 else None
    except (OSError, TimeoutError, ValueError):
        return None
    labels = raw.get("labels") if isinstance(raw, dict) else None
    if not isinstance(labels, list):
        return None
    return {
        str(label.get("name") or "").strip().casefold()
        for label in labels
        if isinstance(label, dict)
    }


async def _behind_by(repository: str, base: str, head: str) -> int | None:
    """How many commits ``base`` has that ``head`` lacks (the Poller's comparison, #2101)."""
    from . import gh_cli

    try:
        code, out = await gh_cli.run(
            ["gh", "api", f"repos/{repository}/compare/{base}...{head}", "--jq", ".behind_by"],
            timeout=_GH_TIMEOUT_S,
            repo=repository,
        )
        value = json.loads(out) if code == 0 else None
    except (OSError, TimeoutError, ValueError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def _write(argv: list[str]) -> tuple[int, str]:
    from . import gh_cli

    try:
        code, out, err = await gh_cli.run_once(argv, timeout=_GH_TIMEOUT_S)
    except (OSError, TimeoutError) as exc:
        return 1, str(exc)
    return code, (out + b" " + err).decode("utf-8", "replace")


async def merge(ref: str, method: str, head: str) -> tuple[str, str]:
    """Merge ``ref`` now, only at ``head``: GitHub refuses it if the head moved.

    Through the REST API, not ``gh pr merge`` (#3728): the CLI refuses a pull request GitHub
    reports as ``BLOCKED`` on its own side, before asking GitHub, so a ruleset the deck's
    account may bypass blocked every merge. GitHub now decides, and ``sha`` is the same head
    guard ``--match-head-commit`` was. ``--admin`` would also get through, but it skips every
    other requirement as well.
    """
    from . import gh_cli

    repository, _, number = ref.partition("#")
    # One read first, for what ``gh pr merge`` did on its own side: the already-merged answer
    # and the head branch to delete. A failed read only skips those two.
    try:
        code, out = await gh_cli.run(
            [
                "gh",
                "api",
                f"repos/{repository}/pulls/{number}",
                "--jq",
                "{merged, ref: .head.ref, repo: .head.repo.full_name}",
            ],
            timeout=_GH_TIMEOUT_S,
            repo=repository,
        )
        pull = json.loads(out) if code == 0 else {}
    except (OSError, TimeoutError, ValueError):
        pull = {}
    if not isinstance(pull, dict):
        pull = {}
    if pull.get("merged") is True:
        return ALREADY_MERGED, f"Pull request {ref} is already merged"
    code, blob = await _write(
        [
            "gh",
            "api",
            "-X",
            "PUT",
            f"repos/{repository}/pulls/{number}/merge",
            "-f",
            f"merge_method={method}",
            "-f",
            f"sha={head}",
        ]
    )
    outcome = merge_outcome(code, blob)
    branch = str(pull.get("ref") or "")
    same_repo = str(pull.get("repo") or "").casefold() == repository.casefold()
    if outcome == MERGED and branch and same_repo:
        # ``--delete-branch``'s part; a branch already gone (deleted on merge) is fine.
        await _write(
            [
                "gh",
                "api",
                "-X",
                "DELETE",
                f"repos/{repository}/git/refs/heads/{quote(branch, safe='/')}",
            ]
        )
    return outcome, blob


async def update_branch(ref: str) -> bool:
    repository, _, number = ref.partition("#")
    code, _blob = await _write(["gh", "pr", "update-branch", number, "--repo", repository])
    return code == 0


def _failed_read(answer: str) -> bool:
    """Did a look stop on a read that failed rather than on an answer (#3023)?

    Read anywhere in the answer: a merge pass answers one line per pull request, and a Work in
    the merge queue has its place added after the reason. Any one failed read is enough, since
    nothing else would make the deck look at the others again.
    """
    return any(words in answer for words in _UNREADABLE_WORDS)


def _repository_of(work_id: str) -> str:
    """``owner/repo`` of a ``github_issue:owner/repo#n`` work id, lowercased."""
    _, _, ref = work_id.partition(":")
    return ref.partition("#")[0].lower()


def _ref_of(work_id: str) -> str:
    """``owner/repo#n`` of a ``github_issue:owner/repo#n`` work id."""
    return work_id.partition(":")[2]


def _unplaced(pulls: list[str], chosen: list[str]) -> str:
    """Why a verdict placed no single pull request, naming what the deck counts (#3127).

    None counted is the deck's problem, not the review's: a pull request the Work's own chat
    registered lost its place in the Work (#3125), and the reviewer can do nothing about it.
    """
    refused = "the verdict names no single pull request of this Work"
    if not pulls:
        return (
            f"{refused}: the deck counts no pull request for this Work, which is a deck"
            " problem, not the review's"
        )
    named = "it names " + ", ".join(chosen) if chosen else "it names none of them"
    return f"{refused}: the deck counts {', '.join(pulls)}, and {named}"


def _match(pull: str, target: str) -> str:
    """``pull`` when ``target`` (``repo#n`` or ``owner/repo#n``) names it, else ""."""
    target = target.lower()
    repository, _, number = pull.partition("#")
    if target == pull or target == f"{repository.rpartition('/')[2]}#{number}":
        return pull
    return ""


def _match_in(repository: str, name: str) -> str:
    """``repository#n`` for a name of one of its pull requests: ``owner/repo#n``, ``repo#n``,
    ``#n`` or ``n``; "" when it names another repository."""
    name = str(name or "").strip().lower()
    named, _, number = name.rpartition("#") if "#" in name else ("", "", name)
    if not number.isdigit():
        return ""
    if named and named not in (repository, repository.rpartition("/")[2]):
        return ""
    return f"{repository}#{int(number)}"
