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

A press and its conditions are per Work, not per pull request: nothing merges, or joins a merge
queue, until every open pull request of the Work passes them, so a reviewed, green pull request
waits for its siblings and the hold names each one that lacks something (#4089).

The conditions, read on every wake (``_hold_reason``, in this order): no Poller card holds the
Work; it is not paused, on the deck or by its label; its issue carries no spec-only label; no
chat of it is mid-turn; no pull request is a draft or conflicts with its base (#4217); each
open one has a review pass at its head and green tests there (a verification job of the
Work, green GitHub checks for a ``github_checks`` repository, or the review alone for a
``review_only`` one), and outside a ``review_only`` repository none of its GitHub checks
still running (#3132). Then, per pull request: it is up to date with its base (read as the
Poller's ``classify_pr``, with ``behind_by``), does not conflict and has no red checks.
``auto_release`` merges at once; any other project waits for the Work page's Merge press.
``review_only`` repositories (the review chats') merge first.

A verdict is a ``/reviewed pass <head> <digest>`` from a login holding the tier the Poller
asks of one (``authorization.required_tier``): Release where ``auto_release`` lets a pass merge,
Work where a person's Merge press still follows it (#2375, #3157), or from the deck's own
reviewer through ``/api/deck-request``; it counts only at the pull request's current head and
diff (``review_refs.target_digest``). A pass whose pull request or diff could not be read is no
refusal: it is held and read again (``held_passes``, #4124), and a pass refused for good is told
to the Work's chat. ``changes-needed`` clears the pass. ``no-code`` (#3322)
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
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote

from . import review_history
from .shared_file import SharedFile
from .watch_retry import Backoff

log = logging.getLogger(__name__)

STORE_FILENAME = "work-release.json"
#: A reviewer's findings are sent to the Work's chat up to this long, with a link to the rest.
MAX_FINDINGS = 8000

MERGED = "merged"
ALREADY_MERGED = "already-merged"
FAILED = "failed"

#: The pull request a hold's reason opens with, when it opens with one.
_HOLD_PULL_RE = re.compile(r"[\w.-]+/[\w.-]+#\d+")
#: A cleared press is the one hold only a person ends, and the agent is the one they ask.
PRESS_CLEARED_TEXT = (
    "AgentDeck: {ref} {why} at {head}, so the person's Merge press no longer stands: it was for"
    " the diffs they saw. Nothing of this Work merges until they press Merge again. Tell them,"
    " and do not say the merge is armed."
)

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
#: A verdict the review history would not store (#3956 review): nothing merges on it.
UNRECORDED = "could not be recorded, so nothing merges on it until a newer verdict is"
#: A pass that came while its pull request or diff could not be read (#4124): no refusal, as
#: nothing says it is wrong, so it waits under this entry key and is read again.
HELD_PASSES = "held_passes"
PASS_HELD = "is held until its diff can be read"
#: Ends the answer about a pass refused and told to the Work's chat, so the comment itself,
#: which reads as a pass, is not forwarded beside it (#4124).
REFUSAL_TOLD = "the Work's chat was told"
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
#: How long the deck waits for an update it made to show on GitHub, and how many times it asks
#: for one head, before a pull request that stays behind leaves the merge queue (#3033).
UPDATE_GRACE_S = 600.0
UPDATE_TRIES = 2
#: How long the first in a merge queue may go unevaluated while it carries no wake mark of its
#: own (#3034): an evaluation that ended on an unreadable GitHub answer, or one a reorder raced,
#: leaves the line's head waiting on nothing.
QUEUE_LOOK_S = 120.0
#: How long after a finished check the deck reads a Work's checks again when they still read
#: pending (#3911): GitHub sends a suite's ``completed`` before its rollup shows the last run done
#: (19 s late once), and no later delivery moves the Work.
CHECKS_SETTLE_S = 60.0
#: How long after something landed on a base the deck reads a Work's merge answers again
#: (#4217): GitHub works out ``mergeable`` lazily after the base moves, so a read made at once
#: still says ``UNKNOWN`` or the answer from before.
MERGEABLE_SETTLE_S = 60.0
#: The one message to a Work's agent when a pull request conflicts with its base (#4217), once
#: per head, whether the deck found it before offering Merge or while merging.
CONFLICT_TEXT = (
    "AgentDeck: {ref} conflicts with its base at {head}, so it cannot merge. Merge the base"
    " in, resolve the conflict, verify, push, and mark it ready again."
)

#: The one message to a Work's agent once its pull requests wait only for a person's Merge
#: (#3199): that person reads the Work's summary first, so its evidence must be the final one.
READY_TEXT = (
    "AgentDeck: ready for merge: {refs}. Its review passed and its tests are green at that"
    " head (or its review alone proves it), so only a person's Merge is left. That person"
    " reads this Work's summary before pressing Merge, so rewrite its `correctness` part"
    " now, in Finnish like the rest, with the final evidence: the review, the test run or CI"
    " build, and the head commit."
    " Send the whole record again, all five parts, in one"
    " `PUT $AGENTDECK_URL/api/sessions/<your session key>/work-resolution`."
    " Change nothing else."
)
#: The one message to a Work's agent once its last open pull request merged (#3141): check the
#: change live, then record the result, which the Work's status line and its Finish read.
LIVE_CHECK_TEXT = (
    "AgentDeck: {refs} merged. Check that it works live now. {deploy} Then check the change"
    " yourself with what you can reach: try it in the live app the way a person would use it,"
    " or read production's logs, Sentry, its data or its API. Unit tests and a deploy alone do"
    " not prove a fix: for a backend or Sentry fix, register a time wait (about a day) and look"
    " in production for the evidence, such as the error not coming back or a log showing the"
    " fixed path ran. If none shows in 3 days because the path simply did not run, record"
    " `verified`, say no evidence showed, and close (#4210). When only the owner's Mac reaches"
    " the evidence, propose a read-only Mac command (reading a log or the database) as the last"
    " resort, once Sentry, the log service and your data tools are not enough. Post what you"
    " checked on the issue (with a screenshot if it shows in the UI), and record the result"
    " with `PUT $AGENTDECK_URL/api/sessions/<your session key>/live-check` and"
    ' `{{"result": "verified" | "failed" | "nothing_to_check" | "not_live" | "needs_person",'
    ' "note": "<one line>"}}`. Record `verified` only once every one of them runs live; if any'
    " is merged but not running yet, record `not_live` and name it, which keeps the Work open"
    " (#3220). Once you record `verified`, close the issue yourself with a one-line summary;"
    " do not ask anyone whether to close it (#3437). Only when a person alone can see whether"
    " it works (a page behind a production login, a pick that changes real data), record"
    " `needs_person` with a note saying what to try, and end your turn with no question: the"
    " Work then waits for a try outside Needs you, and the person's Works, close finishes it"
    " (#4210). If a person asked for more work on the issue after the merge, record `verified`"
    " all the same, keep the issue open and carry on with that work (#3905). If a step the"
    " issue needs is left for a person to do (switch on a key, type a password), also record"
    " `verified` and keep the issue open, and make that step the question in your reply's last"
    " paragraph, which shows the Work as Needs you (#4151); trying it is not such a step. Work"
    " moved to another filed issue is not left: link it when you close. If it failed, reopen"
    " the issue, say there what failed, and fix it in a new pull request."
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
    base: str = ""  # the branch it merges into, as GitHub names it

    @property
    def conflicting(self) -> bool:
        """GitHub says this head conflicts with its base: nothing can merge it as it is."""
        return self.mergeable == "CONFLICTING" or self.merge_state == "DIRTY"

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
    #: Verification-job repositories whose GitHub checks must also be present and green
    #: (``github_checks_required``, #3919): no reported check is not a pass there.
    checks_required: frozenset[str] = frozenset()
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
    #: The repositories that merge on their own (``[[repos]] auto_release = true``, #4172),
    #: lowercased ``owner/repo``, in a project whose own ``auto_release`` is off: a pass there
    #: merges with nobody pressing Merge, while the project's other repositories still wait.
    auto_release_repos: frozenset[str] = frozenset()

    def merges_itself(self, pulls: Iterable[str]) -> bool:
        """Do these pull requests (``owner/repo#n``) merge with nobody pressing Merge (#4172)?

        Yes on a project that merges on its own; otherwise only when there is at least one and
        every one is in a repository that does. A press is per Work, so one pull request that
        needs it holds the Work's others too.
        """
        if self.auto_release:
            return True
        repositories = {str(pull).partition("#")[0].lower() for pull in pulls}
        return bool(repositories) and repositories <= self.auto_release_repos

    def for_pulls(self, pulls: Iterable[str]) -> Policy:
        """This policy with ``auto_release`` as it holds for these pull requests (#4172)."""
        merges = self.merges_itself(pulls)
        return self if merges == self.auto_release else replace(self, auto_release=merges)


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
    if not isinstance(rollup, list):
        return "none"
    # The deck's own review check is no CI: read as CI it would wait on itself, and tell an
    # agent its tests failed when a review only asked for changes (#4152).
    rollup = [
        item
        for item in rollup
        if not (isinstance(item, dict) and review_history.deck_check(item.get("name")))
    ]
    if not rollup:
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
#: Why a Ship now pass holds the merge (#4212 review).
SHIP_UNFINISHED = "is not finished; press Ship now again"
FINDINGS_DROPPED = "findings dropped"
#: What the agent does once the owner settled every finding at a head with no code change
#: (#3828): the review counts as a pass there, with no new review. Asking for a fresh review
#: instead held a head already tested green for a second review round the owner never wanted.
ACCEPTED_ASK = (
    "When their answer settles every finding without a code change, tell the deck so: `POST"
    " $AGENTDECK_URL/api/sessions/<session-key>/review-accepted` with"
    ' `{"pull_request": "<owner/repo#number>", "note": "<their decision>"}`. That review then'
    " counts as a pass at the same commit and no new review runs. Ask for a fresh review"
    " (`review-request`) only when you changed something the reviewer should read again."
)
#: What makes a finding the owner's call, shared by the reviewer's prompt and the agent's
#: message so both draw the line in one place. Narrowed in #3845: a reviewer escalated a test
#: the build had dropped from an approved spec, twice, and the owner only picked the
#: recommended option. Approving a spec does not make its decisions chosen (session guide).
OWNER_CALL_TEST = (
    "it is the owner's decision rather than a defect: it costs money or quota, accepts a known"
    " risk to data, a live system or a person, goes against a decision the owner chose"
    " themselves (picked or wrote, on the issue or in their question box), or changes another"
    " requester's work. A detail the spec or the agent settled without the owner picking it is"
    " not the owner's, even in an approved spec, and neither is a missing or too narrow test"
    " with a cheap fix: those are ordinary findings the agent settles, fixing them or giving"
    " its reason in the pull request body"
)
#: How a reviewer marks a problem that came back after a fix (#4208): the patches so far and the
#: one rule that replaces them. On store#3943 the agent was asked twice whether to accept such a
#: problem, recommended one more narrow fix both times, and the next round found the next hole;
#: the owner always wants a real problem fixed, so the agent builds the rule without asking.
DESIGN_FINDING_MARK = "Design finding:"
DESIGN_FINDING_ASK = (
    f'A finding whose line starts "{DESIGN_FINDING_MARK}" is a problem that came back after'
    " an earlier fix: build the one rule it names, replacing the patches it lists, rather than"
    " patching the new case. Do not ask the owner whether to fix it; they want every real"
    " problem fixed."
)
#: Under every reviewer's findings the Work's agent hears (#3314): a finding only the owner can
#: settle, sent as one to fix, held a Work in review for two days until it repeated.
OWNER_CALL_ASK = (
    DESIGN_FINDING_ASK
    + " A finding is the owner's call only when "
    + OWNER_CALL_TEST
    + ". For such a finding, do not work around it: ask the owner in your question box"
    " now, with the options. Once they answer, post their decision on the issue. " + ACCEPTED_ASK
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
    ' reviewed again as usual. A finding marked "' + EDGE_CASE_MARK + '" is a rare edge'
    ' case (#3574): fix it, or accept it as a known limit by naming it under "Known limits"'
    " in the pull request body, which needs no new review either. The owner's standing rule"
    " is to accept rare edge cases; ask them in your question box only when you doubt it."
)


def is_doc_path(path: str) -> bool:
    """Whether a changed file is a doc file a ``no-code`` fix may touch (#3322)."""
    name = (path or "").strip().lower()
    return bool(name) and (name.endswith(DOC_SUFFIXES) or name.startswith(DOC_DIRS))


def _hold_unrecorded(entry: dict, pull: str, head: str, now: float) -> None:
    """Hold ``pull``: findings about it at ``head`` were not written to its history, where an
    older pass may still read as the answer (#3956 review). The hold stands at any head a base
    update moves it to, until a newer verdict (not a carried pass) is written."""
    held = entry.setdefault("unrecorded", {})
    # The newest failed verdict holds: one made earlier, arriving later, never lowers the bar
    # a stored verdict must pass to lift it (#3956 review).
    if not isinstance(held.get(pull), dict) or float(held[pull].get("at") or 0) < now:
        held[pull] = {"head": head, "at": now}


def _older_than_held(entry: dict, pull: str, at: float | None) -> bool:
    """Is a verdict dated ``at`` older than the pass ``entry`` holds for ``pull`` (#4124)?"""
    held_at = ((entry.get(HELD_PASSES) or {}).get(pull) or {}).get("at")
    return at is not None and held_at is not None and at < held_at


def verdict_settled(answer: str) -> bool:
    """Whether ``on_verdict``'s answer leaves nothing for the comment itself to tell the chat
    (#3192): a pass the release already told it about, one it told it is held or refused
    (#4124), or findings about a commit it left."""
    return answer.startswith(PASS_RECORDED) or answer.endswith(
        (FINDINGS_DROPPED, PASS_HELD, REFUSAL_TOLD)
    )


#: The heading a reviewer's findings open with, naming the pull request and the commit read:
#: ``Review findings for PR #12 at abc1234`` (ChatGPT) or ``Review of owner/repo#12 at abc1234``
#: (the deck's reviewer, #3004).
_HEAD_TARGET = (
    r"`?(?:PR\s+)?`?(?P<repository>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)?#(?P<number>\d+)`?"
    r"\s+at\s+`?(?P<head>[0-9a-fA-F]{7,40})\b"
)
_FINDINGS_HEAD_RE = re.compile(
    r"\A\s*(?:/claude\s+)?Review\s+(?:findings\s+for\s+|of\s+)?" + _HEAD_TARGET, re.IGNORECASE
)
#: The first line of a reviewer's escalation (#4069):
#: ``Review loop escalated to the operator for PR owner/repo#12 at abc1234``.
_ESCALATION_HEAD_RE = re.compile(
    r"\A\s*(?:/claude\s+)?Review(?:\s+loop)?\s+(?:was\s+)?escalated\b[^\n#]*?\s" + _HEAD_TARGET,
    re.IGNORECASE,
)


#: A further pull request on the same line.
_MORE_TARGETS_RE = re.compile(r"(?<![\w/#.-])" + _HEAD_TARGET, re.IGNORECASE)


def _head_target(found: re.Match | None, repository: str) -> tuple[str, str] | None:
    if found is None:
        return None
    named = (found.group("repository") or repository).lower()
    if "/" not in named:
        named = f"{repository.lower().partition('/')[0]}/{named}"
    return f"{named}#{found.group('number')}", found.group("head").lower()


def findings_target(text: str, repository: str) -> tuple[str, str] | None:
    """``("owner/repo#12", head)`` that a findings text says it reviewed, or ``None``.

    ``#12`` alone is in ``repository``, the repository the findings were posted in, and
    ``repo#12`` is in that repository's owner.
    """
    return _head_target(_FINDINGS_HEAD_RE.match(text or ""), repository)


def heading_targets(text: str, repository: str) -> dict[str, str]:
    """``{"owner/repo#12": head}``, what the first line of a findings or escalation comment
    says it is about (#4069), or ``{}`` with no such heading. One review covers all a Work's
    pull requests, so the line may go on: ``… #12 at abc1234 and other/repo#3 at def5678``.
    Whatever older heads the body retells, only these count.
    """
    text = text or ""
    found = _ESCALATION_HEAD_RE.match(text) or _FINDINGS_HEAD_RE.match(text)
    if found is None:
        return {}
    rest = text[found.end() :].partition("\n")[0]
    return dict(
        target
        for each in (found, *_MORE_TARGETS_RE.finditer(rest))
        if (target := _head_target(each, repository))
    )


def same_commit(full: str, given: str) -> bool:
    """A full sha and an abbreviation of it (at least 7 characters) name one commit."""
    full, given = (full or "").lower(), (given or "").lower()
    return len(given) >= 7 and bool(full) and full.startswith(given)


def note_escalation(
    chat_repository: str,
    heads: dict[int, dict[tuple[str, int], str]],
    note: review_history.Note,
    chat: int,
    named: Iterable[str],
    url: str,
    comment: str,
    at: float,
    *,
    full_head: Callable[[str, str], str] = lambda pull, sha: "",
) -> bool:
    """Write a review chat's escalation into the history of each pull request it names, at the
    head its line says was reviewed (``[owner/]repo#n@<sha>``, #3957); of each one the chat
    covers, at the head it was opened about, when it names none.

    ``True`` only once every such pull request has its row stored (now, or before under its
    key): ``False`` has the board keep the escalation and write it again later (#3956 review).
    A short sha is made full from the chat's recorded head, else from the pull request's own
    history (``full_head``); one it cannot make full, or a name it cannot place, is ``False``,
    never a guess at another head and never zero rows called a success. ``heads`` is
    ``AppState.chat_review_heads``.
    """
    covered = {
        f"{repo}#{number}".lower(): head for (repo, number), head in (heads.get(chat) or {}).items()
    }
    targets: list[tuple[str, str]] = []
    for item in named:
        name, _, sha = item.lower().partition("@")
        # Placed only on proof: a covered pull request whose recorded head is the commit the
        # line names, or, for a ``repo#n`` whose own row was not written, the one owner whose
        # pull request has that commit in its own history. Anything else waits (#3956 review).
        matches = [pull for pull in covered if _match(pull, name)]
        if sha:
            matches = [pull for pull in matches if same_commit(covered[pull], sha)]
        found = matches[0] if len(matches) == 1 else ""
        if not found and "/" in name.partition("#")[0]:
            found = name
        elif not found and sha:
            repo, _, number = name.partition("#")
            owners = {pull.partition("#")[0].rpartition("/")[0] for pull in covered}
            proven = [
                f"{owner}/{repo}#{number}"
                for owner in owners
                if number.isdigit() and full_head(f"{owner}/{repo}#{number}", sha)
            ]
            found = proven[0] if len(proven) == 1 else ""
        if not found:
            return False
        targets.append((found, sha))
    if not targets:
        targets = [(pull, head) for pull, head in covered.items()]
    if not targets:
        return False
    chat_ref = f"{chat_repository}#{chat}".lower()
    every = True
    for pull, sha in targets:
        recorded = covered.get(pull, "")
        if not sha:
            head = recorded
        elif len(sha) == 40:
            head = sha
        elif same_commit(recorded, sha):
            head = recorded
        else:
            head = full_head(pull, sha)
        if len(head) != 40:
            every = False
            continue
        key = f"escalated:{chat_ref}:{pull}:{comment or at}"
        data = {"chat": chat_ref, "url": url}
        stored = note(pull, head, review_history.ESCALATED, key, at=at, data=data) is not False
        every = every and stored
    return every


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


def offered_pulls(policy: Policy, pulls: Iterable[tuple[str, str, bool]]) -> list[str]:
    """The pull requests a Work offers to merge, from ``(ref, state, draft)`` (#4172 review).

    Open ones, and a draft only in a ``review_only`` repository, where it holds the Work so
    the reviewer's repository merges first (D8). A state the deck never read counts as open,
    so an unknown pull request holds rather than lets the rest merge by themselves. The
    release's own look and the Work page's Merge read this one rule.
    """
    offered = set()
    for ref, state, draft in pulls:
        ref = str(ref).lower()
        if (state or "OPEN").upper() != "OPEN":
            continue
        if draft and ref.partition("#")[0] not in policy.review_only:
            continue
        offered.add(ref)
    return sorted(offered)


class ReleaseStore:
    """Each Work's release facts, in one small JSON file keyed by work id.

    Per Work: ``asked`` (a person pressed Merge: ``{"at", "pulls": pull ->
    {"digest", "head"}}``, the diffs it was pressed for, #3175 and #3320; ``pull -> digest`` and
    ``True`` from older decks), ``asked_cleared`` (the pull request whose changed diff cleared a
    press, and its head, #3320), ``awaiting_merge`` (pull -> head, set while every open pull
    request is reviewed and tested and only a person's Merge is missing, #2960), ``updated``
    (pull -> head a behind branch was updated from), ``update_at`` (pull -> ``[when, tries]`` of
    that update, #3033), ``unlanded`` (pull -> head whose update never landed, so it waits out
    of line), ``told`` (message keys already sent to the Work's chat), the
    ``waiting_tests``/``waiting_turn`` marks that let a finished job or an idle chat look again,
    ``queued`` (the pulls held only by their place in a merge queue, looked at again once one is
    first, #3034), ``retry`` (first failure ``since``, last wait ``delay``), set while the
    last look stopped on a failed read (#3023), and ``held_passes`` (pull -> a pass that came
    while its diff could not be read and its back-off, #4124). Recorded passes and findings are
    not here: each pull request head's review history holds them (``review_history``, #3956).

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
        found = {k: v for k, v in works.items() if isinstance(k, str) and isinstance(v, dict)}
        for entry in found.values():
            # Kept by decks before #3956: each head's review history holds these now, and the
            # old ones are dropped rather than copied, so the next write leaves them out.
            for retired in ("passes", "findings", "no_code"):
                entry.pop(retired, None)
        return found

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
    #: The review digest of ``ref``'s diff at ``head``; "" when it could not be read just now.
    #: A diff GitHub will never serve has one pinned to its head (``review_refs.head_digest``).
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
    #: ``pulls`` for every Work from one walk taken now (#3993), for a look at many Works;
    #: ``None`` asks ``pulls`` once per Work.
    pulls_lookup: Callable[[], Callable[[str], Iterable[str]]] | None = None
    #: The Work's issue labels, casefolded; ``None`` when unreadable (which holds the merge).
    issue_labels: Callable[[str], Awaitable[set[str] | None]] | None = None
    #: A login's collaborator permission on a repository (``admin``, ``maintain``, ...), or
    #: ``None`` when unreadable: the live half of the Poller's Release check.
    permission: Callable[[str, str], Awaitable[str | None]] | None = None
    #: Is a verification job of this Work queued or running? Then its agent already ran it,
    #: and is not told to (#2999).
    verifying: Callable[[str], Awaitable[bool]] | None = None
    #: True in an edition with no reviewer (the public copy, #3623): a person's Merge press
    #: is then also the review pass for each pull request at the head it was pressed for.
    press_is_review: bool = False
    #: Is the Ship now press that passed ``pull`` at ``head`` not finished (#4212 review)? Its
    #: pass then holds the merge until a press finishes it: the follow-up recorded, the head
    #: marked shipped and, where the Work waits for one, its Merge press recorded.
    ship_unfinished: Callable[[str, str], bool] = lambda pull, head: False
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
    #: Each head's review history (#3957): append one event (``review_history.Note``), read a
    #: pull request's events at every head, and forget them once it merged or closed. A head's
    #: pass, findings and review chat are read from it alone (#3956).
    review_event: review_history.Note = review_history.no_note
    reviews: Callable[[str], Iterable[review_history.ReviewEvent]] = lambda pull: ()
    forget_reviews: Callable[[str], object] = lambda pull: None
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
        #: The head each deck reviewer's findings stand at, by request id, from
        #: ``stale_findings`` until ``on_findings`` writes them with whether they were told.
        self._findings_heads: dict[str, tuple[str, str, str]] = {}

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
        return self._enabled and bool(repository) and self.ports.switched(repository)

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
        # One walk of the deck for the whole look, never one per Work: asking each Work for
        # its pull requests walked every registration a thousand times, 88 s on 2026-11-02
        # (#4016).
        lookup = self.ports.pulls_lookup
        pulls = lookup() if lookup is not None else None
        for work_id in list(self.store.works):
            # A skipped Work awaits nothing else: without this the whole run of them is one
            # block of the event loop, seconds long on a deck with many Works (#3975).
            await asyncio.sleep(0)
            if not self.handles(work_id):
                continue
            # A held pass too: the restart lost the timer that would read it again (#4124).
            if not self._holds_pass(work_id) and not self._has_pass(work_id, pulls):
                continue
            try:
                answer = await self.evaluate(work_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("work-release: evaluating %s at start failed", work_id, exc_info=True)
                # The restart lost its timer, and nothing else may come by.
                await self._retry_raised_look(work_id, pulls)
                continue
            # Every answer, so a reviewed Work that did not turn Ready for merge can be read
            # off the journal: a label, an unreadable pull request or none known yet (#3080).
            log.info("work-release: %s at start: %s", work_id, answer)

    def _mark_retry(self, work_id: str, entry: dict, reason: str) -> None:
        """Look again after the back-off: the look stopped on a failed read (#3023).

        A timed look (``_look_later``), not a stored due time: a restart loses the timer, and
        the start's look at every reviewed Work or one holding a pass, the only Works marked,
        sets it again."""
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

    async def _retry_raised_look(
        self, work_id: str, pulls: Callable[[str], Iterable[str]] | None = None
    ) -> None:
        """A look that raised is a failed read too: looked at again after the back-off."""
        async with self._lock(work_id):
            entry = self.store.get(work_id)
            if self._holds_pass(work_id) or self._has_pass(work_id, pulls):
                self._mark_retry(work_id, entry, "the look raised")
                self.store.put(work_id, entry)

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

    async def _answers_deck_ask(self, work_id: str, by: str, chosen: list[str], head: str) -> bool:
        """Does the reviewer's verdict answer a review the deck itself asked for at ``head``?

        Where Release is needed only because the pull request's repository merges on its own
        (#4172), the deck's ``[reviewers]`` account counts at Work tier for the one pull
        request and commit the deck sent it to review (owner's choice on #4187): the ChatGPT
        review chats post as that bot, and without this a clean review there never counted.
        It is no grant of Release, so its ``/reviewed`` about any other head or repository is
        refused as before; the pass is still checked against the head and digest.
        """
        from .poller_engine.authorization import Tier, reviewer_logins

        if len(chosen) != 1 or by.casefold() not in reviewer_logins():
            return False
        events = review_history.at_head(self._history(chosen[0]), head)
        if not any(event.kind == review_history.ASKED for event in events):
            return False
        return await self._holds(work_id, by, Tier.WORK)

    async def _tell_untrusted_reviewer(
        self, work_id: str, by: str, head: str, tier: str, *, repository_only: bool = False
    ) -> None:
        """Tell the Work's chat once per head that the reviewer's own verdict was refused (#3901).

        Only the ``[reviewers]`` account, so anyone else's ``/reviewed`` stays unheard. Without
        it the chat read "no verdict reached this chat" and asked for review after review.
        """
        from .poller_engine.authorization import reviewer_logins

        if by.casefold() not in reviewer_logins():
            return
        async with self._lock(work_id):
            entry = self.store.get(work_id)
            told = self._tell(
                work_id,
                entry,
                f"verdict:untrusted:{by.casefold()}:{head[:12]}",
                (
                    f"AgentDeck: {by}'s review verdict at {head[:12]} was not recorded, because"
                    f" its pull request is in a repository that merges on its own"
                    f" (`auto_release` on its `[[repos]]` entry), where a verdict needs the"
                    f" {tier} tier, and {by} does not hold it. There its verdict counts only"
                    " when it answers a review the deck asked for at that exact commit. Do not"
                    " ask for it to be given Release: that would let it merge the project's"
                    " other repositories too. Ask the deck for a review of the current head"
                    " (`review-request`) instead."
                    if repository_only
                    else f"AgentDeck: {by}'s review verdict at {head[:12]} was not recorded,"
                    f" because on this project a verdict needs the {tier} tier and {by} does"
                    " not hold it. Another review will end the same way. Ask the owner to add"
                    " it to `allow` under `[authorization.release]` in the project's poller"
                    " file and run `agentdeck-project update`, then ask for the review again. A"
                    " Merge press does not stand in for the missing pass."
                ),
            )
            if told:
                self.store.put(work_id, entry)

    async def on_verdict(
        self,
        work_id: str,
        verdict: str,
        head: str,
        digest: str,
        targets: Iterable[str] = (),
        *,
        by: str | None = None,
        source: str = "",
        at: float | None = None,
    ) -> str:
        """Take a reviewer's verdict; answer what became of it, for a log or a receipt.

        ``by`` is the GitHub login that posted it; ``None`` is the deck's own reviewer through
        ``/api/deck-request``, which the deck trusts as the Poller trusts its request file. A
        login must hold the tier the Poller asks of a ``/reviewed`` here (``_verdict_tier``).
        ``source`` is the comment's or the request's own id, which keys its history event, and
        ``at`` the comment's own time, which dates it (the deck's clock when ``None``).
        """
        if not self.handles(work_id):
            return "not released by the deck"
        pulls = sorted(set(self.ports.pulls(work_id)))
        targets = tuple(targets)
        named = [_match(pull, target) for target in targets for pull in pulls]
        chosen = sorted({pull for pull in named if pull})
        if not targets:
            # A verdict naming a pull request this Work does not count is not about its only
            # one: beside another verdict in one comment, it names another repository (#3161).
            chosen = pulls if len(pulls) == 1 else []
        if by is not None:
            # The tier of the pull request it is about (#4172), every one of the Work's when it
            # names none this Work counts.
            tier = self._verdict_tier(work_id, chosen)
            if not await self._holds(work_id, by, tier):
                name = tier.value.capitalize()
                # Release only because of a repository's own switch (#4172): granting it
                # project-wide would let this login merge every other repository too.
                alone = not self.ports.policy(_repository_of(work_id)).auto_release
                repository_only = alone and name == "Release"
                if not (
                    repository_only and await self._answers_deck_ask(work_id, by, chosen, head)
                ):
                    await self._tell_untrusted_reviewer(
                        work_id, by, head, name, repository_only=repository_only
                    )
                    return f"{by} may not send a review verdict here (it holds no {name} tier)"
        if len(chosen) != 1:
            return _unplaced(pulls, chosen)
        pull = chosen[0]
        if verdict in ("changes-needed", NO_CODE):
            standing = await self._standing_head(pull, head)
            if standing is None:
                # Findings about a commit the branch has left are dropped (#3192): the chat
                # that asked for them was replaced, and the current head has its own review.
                return f"{pull} moved past {head[:12]}; {FINDINGS_DROPPED}"
            # A no-code verdict that does not match the diff now holds as changes-needed: its
            # fix is reviewed again (#3322).
            held = await self._no_code_hold(pull, head, digest) if verdict == NO_CODE else None
            merged = bool(standing) and await self._merged(pull)
            async with self._lock(work_id):
                entry = self.store.get(work_id)
                # A late comment older than the newest verdict is not the newer word, since
                # history dates each by its own time: only its row is kept.
                late = self._outdated(entry, pull, head, at)
                if not late:
                    entry.pop("asked", None)
                    self._drop_held(entry, pull)  # not read again
                    # Out of line with all its pull requests, not only this one: the Work
                    # merges nothing without this pass, so another of them first in line would
                    # hold the line until the Work's next event (#3033).
                    self._leave(work_id)
                # Read by the Work's line until a push or a pass answers them (#3097).
                told = bool(self.ports.chat(work_id)) and not late
                word = NO_CODE if held else "changes-needed"
                message_key = _round_key(
                    entry, f"verdict:{word}:{pull}@{head[:12]}", self.review_chat(pull, head)
                )
                reviewer = self._reviewer(pull, head, by)
                if merged:
                    # The owner merged it by hand while the review ran (#4189): it cannot
                    # be fixed before merging any more, only after.
                    text = (
                        f"AgentDeck: the review of {pull} at {head[:12]} ({reviewer}) asks for"
                        f" changes, but {pull} had already merged before it finished. Fix the"
                        " findings in a follow-up pull request."
                    )
                elif held:
                    text = NO_CODE_TEXT.format(pull=pull, head=head[:12], reviewer=reviewer)
                else:
                    text = (
                        f"AgentDeck: the review of {pull} at {head[:12]} ({reviewer}) asks for"
                        " changes, so it will not merge. Fix the findings, push, and mark it"
                        " ready again."
                    )
                if not late:
                    self._tell(work_id, entry, message_key, text)
                self.store.put(work_id, entry)
                kind = review_history.NO_CODE if held else review_history.CHANGES
                data = {"by": by or "", "chat": self.review_chat(pull, head) or ""}
                data["told"] = told
                if held and "files" in held:
                    data["files"] = held["files"]
                key = f"{source}:{pull}" if source else message_key
                at_head = standing or self._full_head(pull, head)
                if not self._note(pull, at_head, kind, key, data, head, at=at, hold=entry):
                    self.store.put(work_id, entry)
                # Ended only for a verdict taken and told (#3183 review): a refused one leaves
                # the wait to its timeout, so the agent still looks.
                if not late:
                    self._review_answered(work_id, entry, message_key, [pull])
            if late:
                return f"{pull}'s verdict is older than its newest; {FINDINGS_DROPPED}"
            await self._wake_next(work_id)  # the next in line, once this lock is let go
            if verdict == NO_CODE and not held:
                return (
                    f"changes needed on {pull}: its {NO_CODE} verdict does not match the diff"
                    f" at {head[:12]}, so a fix is reviewed again"
                )
            return f"{NO_CODE} fixes needed on {pull}" if held else f"changes needed on {pull}"
        return await self._take_pass(work_id, pull, head, digest, by=by, source=source, at=at)

    async def _take_pass(
        self,
        work_id: str,
        pull: str,
        head: str,
        digest: str,
        *,
        by: str | None,
        source: str,
        at: float | None,
    ) -> str:
        """Record a pass at ``pull``'s current head and diff; answer what became of it.

        A read that failed says nothing about the pass, so it is held and read again (#4124).
        Anything that does say no is told to the Work's chat, which otherwise reads the pass
        comment and waits for a merge that will not come.
        """
        args = (work_id, pull, head, digest, by, source, at)
        async with self._lock(work_id):
            # A pass delivered late neither replaces a newer one held here nor is announced
            # over newer findings, which history keeps first whatever becomes of it.
            entry = self.store.get(work_id)
            if self._outdated(entry, pull, head, at):
                # A hold no newer than this pass was overtaken too: a read would only find so.
                if not _older_than_held(entry, pull, at) and self._drop_held(entry, pull):
                    self.store.put(work_id, entry)
                return f"{pull}'s pass is older than its newest verdict; {FINDINGS_DROPPED}"
            facts = await self.ports.read_pull(pull)
            if facts is None:
                return self._hold_pass(*args)
            if facts.state == "MERGED":  # needs no pass any more
                entry = self.store.get(work_id)
                changed = self._drop_held(entry, pull)
                told = ""
                # Told only for the diff that merged: a wrong digest passed nothing (#4189).
                if same_commit(facts.head, head):
                    current = await self.ports.digest(pull, facts.head)
                    if current and current == digest.lower():
                        told = self._tell_late_pass(work_id, entry, pull, facts.head, head, by)
                if changed or told:
                    self.store.put(work_id, entry)
                if told:
                    self._review_answered(work_id, entry, told, [pull])
                return f"{pull} is not open at {head[:12]}" + ("; told as merged" if told else "")
            if facts.state != "OPEN":
                return self._refuse_pass(
                    work_id,
                    pull,
                    head,
                    f"{pull} is not open at {head[:12]}",
                    "the pull request was closed without merging. Reopen it and ask for the"
                    " review again (`review-request`) if it should still merge",
                )
            current = await self.ports.digest(pull, facts.head)
            if not same_commit(facts.head, head) and not self._same_change(pull, facts.head, head):
                # A pass about a head the branch left counts only where the router proved the
                # new head a base merge away and reviewed nothing new for it (#3314).
                return self._refuse_pass(
                    work_id,
                    pull,
                    head,
                    f"{pull} is not open at {head[:12]}",
                    f"the branch has moved on to {facts.head[:12]}, and only a review of that"
                    " head counts",
                    ends_wait=False,
                )
            if not current:
                return self._hold_pass(*args)
            if current != digest.lower():
                return self._refuse_pass(
                    work_id,
                    pull,
                    head,
                    f"the digest does not match {pull}'s diff at {facts.head[:12]}",
                    f"the digest it states does not match the diff at {facts.head[:12]}. Ask for"
                    " the review again (`review-request`)",
                )
            passed = {"head": facts.head, "digest": current, "at": self.ports.clock()}
            chat = self.review_chat(pull, facts.head) or self.review_chat(pull, head)
            if chat:
                passed["chat"] = chat
            if by:
                passed["by"] = by
            key = f"{source}:{pull}" if source else f"verdict:pass:{pull}@{facts.head}@{chat or ''}"
            if not self._note(pull, facts.head, review_history.PASS, key, passed, head, at=at):
                return self._refuse_pass(
                    work_id,
                    pull,
                    head,
                    f"{pull}'s pass at {facts.head[:12]} {UNRECORDED}",
                    "the deck could not write it to the review history. Ask for the review again"
                    " (`review-request`)",
                )
            entry = self.store.get(work_id)
            if self._drop_held(entry, pull):
                self.store.put(work_id, entry)
        lead = (
            f"AgentDeck: the review of {pull} at {facts.head[:12]}"
            f" ({self._reviewer(pull, facts.head, by)}) passed clean."
        )
        return await self._announce_pass(work_id, pull, facts.head, lead)

    def _hold_pass(
        self,
        work_id: str,
        pull: str,
        head: str,
        digest: str,
        by: str | None,
        source: str,
        at: float | None,
    ) -> str:
        """Keep a pass nothing could be read about, to read again after the back-off (#4124).

        Under the Work's lock. Kept in the entry, so a restart loses only the timer, and the
        start's look sets it again. A day of failed reads ends in a refusal, told to the chat.
        """
        now = self.ports.clock()
        entry = self.store.get(work_id)
        before = (entry.get(HELD_PASSES) or {}).get(pull) or {}
        if before.get("head") != head or before.get("digest") != digest:
            before = {}
        since = float(before.get("since", now))
        if now - since > RETRY_MAX_AGE_S:
            return self._refuse_pass(
                work_id,
                pull,
                head,
                f"{pull}'s diff at {head[:12]} could not be read for a day",
                "the deck could not read the pull request's diff for a day. Ask for the review"
                " again (`review-request`)",
            )
        backoff = Backoff(first=RETRY_FIRST_S, cap=RETRY_MAX_S)
        backoff.next_s = float(before.get("delay", 0.0))
        delay = backoff.failed()
        entry.setdefault(HELD_PASSES, {})[pull] = {
            "head": head,
            "digest": digest,
            "by": by,
            "source": source,
            # A verdict with no time of its own is dated now, not when a read finally works.
            "at": now if at is None else at,
            "since": since,
            "delay": delay,
        }
        self._tell(
            work_id,
            entry,
            f"verdict:held:{pull}@{head[:12]}",
            f"AgentDeck: the review of {pull} at {head[:12]}"
            f" ({self._reviewer(pull, head, by)}) sent a pass, but the deck could not read the"
            " diff to check it, so it is not recorded yet. The deck keeps the pass and reads the"
            " diff again by itself, and you will hear what became of it. Nothing merges until"
            " then, and nothing is needed from you or the owner.",
        )
        self.store.put(work_id, entry)
        log.info("work-release: %s's pass at %s held; reading in %.0fs", pull, head[:12], delay)
        self._look_later(work_id, delay)
        return f"{pull}'s pass at {head[:12]} {PASS_HELD}"

    def _refuse_pass(
        self, work_id: str, pull: str, head: str, answer: str, why: str, *, ends_wait: bool = True
    ) -> str:
        """Tell the Work's chat a pass was not recorded, and why; ``answer`` for the log (#4124).

        Under the Work's lock. This message is what the chat's wait on the review waited for,
        unless the branch left the head: its wait is then on the new head's review.
        """
        entry = self.store.get(work_id)
        self._drop_held(entry, pull)
        key = _round_key(entry, f"verdict:refused:{pull}@{head[:12]}", self.review_chat(pull, head))
        self._tell(
            work_id,
            entry,
            key,
            f"AgentDeck: the review pass of {pull} at {head[:12]} was not recorded, so nothing"
            f" merges on it: {why}.",
        )
        self.store.put(work_id, entry)
        if ends_wait:
            self._review_answered(work_id, entry, key, [pull])
        told = key in (entry.get("told") or [])
        return f"{answer}; {REFUSAL_TOLD}" if told else answer

    @staticmethod
    def _drop_held(entry: dict, pull: str) -> bool:
        """Forget ``pull``'s held pass; whether there was one."""
        held = entry.get(HELD_PASSES)
        if not isinstance(held, dict) or held.pop(pull, None) is None:
            return False
        if not held:
            entry.pop(HELD_PASSES)
        return True

    def _holds_pass(self, work_id: str) -> bool:
        """Does this Work keep a pass waiting for a read (#4124)?"""
        return bool((self.store.works.get(work_id) or {}).get(HELD_PASSES))

    async def _read_held(self, work_id: str) -> None:
        """Try each pass this Work holds again (#4124). Outside the Work's lock, which taking
        one needs. The tier was checked when it came, so a login's lookup failing now loses
        nothing; each is looked up again, as a pass recorded here looks at the Work and may
        take the next one too."""
        for pull in sorted((self.store.works.get(work_id) or {}).get(HELD_PASSES) or ()):
            held = ((self.store.works.get(work_id) or {}).get(HELD_PASSES) or {}).get(pull)
            if not isinstance(held, dict):
                continue
            answer = await self._take_pass(
                work_id,
                pull,
                str(held.get("head") or ""),
                str(held.get("digest") or ""),
                by=held.get("by"),
                source=str(held.get("source") or ""),
                at=held.get("at"),
            )
            log.info("work-release: held pass of %s: %s", pull, answer)

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
                f" Release: {_held_now(entry, answer) or 'nothing holds it'}."
            )
            # Only a pass message this verdict sends carries the call to verify: a repeat pass
            # at a head already told about sends nothing, so the call goes on its own.
            # Each review of the head is its own message (#3183): a head reviewed again on
            # request passes again under that review chat's key, so its agent hears it.
            message_key = _round_key(entry, f"verdict:pass:{key}", self.review_chat(pull, head))
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
        if not current or current != (digest or "").lower():
            return None
        files = None
        if self.ports.pull_files is not None:
            with contextlib.suppress(Exception):
                files = await self.ports.pull_files(pull)
        held: dict = {"head": facts.head.lower(), "at": self.ports.clock()}
        if files is not None:
            held["files"] = sorted(files)
        return held

    def _outdated(self, entry: dict, pull: str, head: str, at: float | None) -> bool:
        """Is a verdict dated ``at`` about ``head`` older than the newest word there: a verdict
        held or recorded, findings, or a review asked for since (#4124)? The one rule for a
        late verdict: it moves nothing, and a held pass it describes ends at its next read."""
        if at is None:
            return False
        if _older_than_held(entry, pull, at):
            return True
        events = review_history.at_head(self._history(pull), head)
        newest = review_history.newest_verdict(events)
        times = [event.at for event in events if event.kind == review_history.ASKED]
        return at < max(times + ([newest.at] if newest else []), default=at)

    def _history(self, pull: str) -> list[review_history.ReviewEvent]:
        """``pull``'s review history at every head; none when unreadable, which passes nothing."""
        try:
            return list(self.ports.reviews(pull.lower()))
        except Exception:  # noqa: BLE001 - unread: no pass, so nothing merges on it
            log.warning("work-release: %s's review history unreadable", pull, exc_info=True)
            return []

    def _full_head(self, pull: str, head: str) -> str:
        """The full commit an abbreviated ``head`` names in ``pull``'s history, else ``head``:
        findings about a head the pull request could not be read at still replace a pass
        recorded there (#3956)."""
        found = review_history.at_head(self._history(pull), head)
        return found[-1].head if found else head

    def _same_change(self, pull: str, head: str, older: str) -> bool:
        """Whether the router proved ``head`` only base merges away from ``older`` and reviewed
        nothing new for it (#3314): ``older``'s verdict stands for it, findings and pass alike."""
        return review_history.same_change(self._history(pull), head, older)

    def _verdict_at(self, pull: str, head: str) -> review_history.ReviewEvent | None:
        """The newest verdict on ``pull`` at ``head`` (#3956): a pass there is its review pass.
        ``None`` once a review of that head was asked for again after it: its pass then waits
        for that review's own verdict (#3956 review). A review chat opened since is not an
        ask: one that lists an already passed pull request beside others posts no verdict on
        it (#3341). A Ship now pass counts only once its press finished (#4212 review)."""
        history = self._history(pull)
        verdict = review_history.standing_verdict(review_history.at_head(history, head))
        shipped = review_history.ship_pass_head(history, head)
        if shipped and self._ship_unfinished(pull, shipped):
            return None
        return verdict

    def _ship_unfinished(self, pull: str, head: str) -> bool:
        try:
            return bool(self.ports.ship_unfinished(pull, head))
        except Exception:  # noqa: BLE001 - unread is not finished: the merge holds
            log.warning("work-release: Ship now of %s unreadable", pull, exc_info=True)
            return True

    def review_chat(self, pull: str, head: str) -> str | None:
        """The newest review chat (``owner/repo#n``) opened about ``pull`` at ``head``, or
        ``None``: named in a verdict's message and on the Ready for merge (#2992)."""
        chats = [e for e in review_history.at_head(self._history(pull), head) if e.kind == "chat"]
        newest = max(chats, key=lambda event: (event.at, event.seq), default=None)
        return str(newest.data.get("chat") or "") or None if newest is not None else None

    def _has_pass(self, work_id: str, pulls: Callable[[str], Iterable[str]] | None = None) -> bool:
        """Whether the newest verdict on any pull request of the Work, at any head, is a pass.

        ``pulls`` is a lookup a caller asking about many Works took once (``pulls_lookup``)."""
        return any(
            review_history.passed(review_history.newest_verdict(self._history(ref)))
            for ref in set((pulls or self.ports.pulls)(work_id))
        )

    def passed_since(self, work_id: str, pull: str, head: str, since: float) -> bool:
        """Whether ``pull``'s verdict at ``head`` is a pass dated ``since`` or later (#3324)."""
        del work_id  # a pull request's history is its own, whichever Work holds it
        verdict = self._verdict_at(pull, head)
        return review_history.passed(verdict) and verdict.at >= since

    def no_code_head(self, work_id: str, pull: str) -> tuple[str, frozenset[str] | None] | None:
        """The head a ``no-code`` verdict holds ``pull`` at, and its diff's files then (``None``
        when unread), or ``None`` when ``pull``'s newest verdict at any head is not one (#3322)."""
        del work_id
        held = review_history.newest_verdict(self._history(pull))
        if held is None or held.kind != review_history.NO_CODE:
            return None
        files = held.data.get("files")
        return held.head, frozenset(files) if isinstance(files, list) else None

    async def no_code_fixed(self, work_id: str, pull: str, head: str) -> str:
        """The agent fixed a ``no-code`` review's findings at ``head`` (#3322): the router saw
        it marked ready at that verdict's head, or proved the push to ``head`` touched only
        doc files. That review counts as a pass at ``head``, with no new review."""
        if not self.handles(work_id):
            return "not released by the deck"
        pull = pull.lower()
        async with self._lock(work_id):
            held = review_history.newest_verdict(self._history(pull))
            if held is None or held.kind != review_history.NO_CODE:
                return f"no {NO_CODE} verdict holds {pull}"
            facts = await self.ports.read_pull(pull)
            if facts is None or facts.state != "OPEN" or not same_commit(facts.head, head):
                return f"{pull} is not open at {head[:12]}"
            current = await self.ports.digest(pull, facts.head)
            if not current:
                return f"{pull}'s diff at {facts.head[:12]} {DIGEST_UNREADABLE}"
            passed = {
                "head": facts.head,
                "digest": current,
                "at": self.ports.clock(),
                NO_CODE: held.head,
            }
            for name in ("chat", "by"):
                if held.data.get(name):
                    passed[name] = held.data[name]
            # Keyed by the no-code verdict it answers, so a second one at the same head is a row
            # of its own rather than the first one's, read as already stored.
            key = f"no-code-fixed:{held.key}@{facts.head}"
            # Carried from the no-code head, so a later base merge keeps it too (#3314).
            if not self._note(pull, facts.head, review_history.PASS, key, passed, held.head):
                return f"{pull}'s pass at {facts.head[:12]} {UNRECORDED}"
        old = held.head
        since = ""
        if not same_commit(facts.head, old):
            since = f" changed only doc files since {old[:12]} and"
        lead = (
            f"AgentDeck: {pull} at {facts.head[:12]}{since} answers a review that asked only for"
            " fixes needing no code, so no new review was started and that review counts as a"
            " pass here."
        )
        return await self._announce_pass(work_id, pull, facts.head, lead)

    async def owner_accepted(self, work_id: str, pull: str, head: str, note: str) -> str:
        """The owner settled the review's findings on ``pull`` at ``head`` with no code change
        (#3828): that review counts as a pass there, with no new review. The router checks
        the reviewer read this head; ``note`` is the owner's decision, kept with the pass."""
        if not self.handles(work_id):
            return "not released by the deck"
        pull = pull.lower()
        async with self._lock(work_id):
            facts = await self.ports.read_pull(pull)
            if facts is None or facts.state != "OPEN" or not same_commit(facts.head, head):
                return f"{pull} is not open at {head[:12]}"
            current = await self.ports.digest(pull, facts.head)
            if not current:
                return f"{pull}'s diff at {facts.head[:12]} {DIGEST_UNREADABLE}"
            at = self.ports.clock()
            chat = self.review_chat(pull, facts.head)
            key = f"accepted:{pull}@{facts.head}@{at}"
            data = {"note": note.strip(), "chat": chat or "", "digest": current}
            if not self._note(pull, facts.head, review_history.ACCEPTED, key, data, at=at):
                return f"{pull}'s pass at {facts.head[:12]} {UNRECORDED}"
        lead = (
            f"AgentDeck: the owner accepted the review's findings on {pull} at"
            f" {facts.head[:12]} with no code change, so no new review was started and that"
            " review counts as a pass here."
        )
        return await self._announce_pass(work_id, pull, facts.head, lead)

    def _tell_late_pass(
        self, work_id: str, entry: dict, pull: str, merged_head: str, head: str, by: str | None
    ) -> str:
        """Tell the Work's chat a pass came after ``pull`` merged; the message key, or "".

        The owner may merge by hand while a review still runs (#4189): its pass then counts for
        nothing, but the agent waiting on it must still hear that the review found nothing to
        follow up. Only for the head that merged, and only when the newest verdict told there is
        not a pass already: a second copy of the pass the deck merged on says nothing new.
        """
        if not same_commit(merged_head, head):
            return ""
        # The told keys, not the history: a merge forgets the pull request's history
        # (``forget_reviews``), so a replay of the pass the deck merged on would read as new.
        # The newest verdict told at this head decides, so a pass after findings is told.
        mark = f":{pull}@{head[:12]}"
        told = [k for k in entry.get("told") or [] if k.startswith("verdict:") and mark in k]
        if told and told[-1].startswith(("verdict:pass:", "verdict:late-pass:")):
            return ""
        key = _round_key(
            entry, f"verdict:late-pass:{pull}@{head[:12]}", self.review_chat(pull, head)
        )
        text = (
            f"AgentDeck: the review of {pull} at {head[:12]} ({self._reviewer(pull, head, by)})"
            f" passed after {pull} had already merged, so nothing needs a follow-up."
        )
        return key if self._tell(work_id, entry, key, text) else ""

    async def _merged(self, pull: str) -> bool:
        """Whether ``pull`` reads as merged; unreadable is not merged."""
        facts = await self.ports.read_pull(pull)
        return facts is not None and facts.state == "MERGED"

    async def _standing_head(self, pull: str, head: str) -> str | None:
        """The full head that findings about ``pull`` at ``head`` stand at ("" when unread), or
        ``None`` when its branch has left ``head``, on a positive read only (#3192).

        A reviewer still running on a review chat that a newer head replaced can only report
        on the old commit. A short or missing head, or an unreadable pull request, is not
        called moved: those findings are kept, as before. Nor is a branch that only merged its
        base since (#3314): the router proved it and reviews no new head, so these findings are
        the ones that stand, at the current head.
        """
        if len(head or "") < 7:
            return ""
        facts = await self.ports.read_pull(pull)
        if facts is None or not facts.head:
            return ""
        if same_commit(facts.head, head) or self._same_change(pull, facts.head, head):
            return facts.head
        return None

    def _note(
        self,
        pull: str,
        head: str,
        kind: str,
        key: str,
        data: dict,
        about: str = "",
        *,
        at: float | None = None,
        hold: dict | None = None,
    ) -> bool:
        """Append ``kind`` to ``pull``'s history at ``head`` (#3957), dated ``at`` (the event's
        own time; the clock when ``None``); ``about`` is the head the reviewer read, kept when a
        base merge carried it to ``head``. Whether it is stored, now or under its key before:
        the history is the only record of a verdict (#3956), so a caller fails closed on
        ``False``. Findings pass the Work's entry as ``hold``: one not stored holds the pull
        request there, dated as its row would have been, so only a verdict made after it lifts
        the hold, however late either arrives (#3956 review)."""
        data = {
            name: value
            for name, value in data.items()
            if value not in ("", None) and name not in ("head", "at")
        }
        if about and not same_commit(head, about):
            data["carried_from"] = about
        when = self.ports.clock() if at is None else at
        try:
            stored = self.ports.review_event(pull, head, kind, key, at=when, data=data) is not False
        except Exception:  # noqa: BLE001 - not stored: the caller fails closed
            log.warning("work-release: writing %s's %s failed", pull, kind, exc_info=True)
            stored = False
        if not stored:
            log.warning("work-release: %s's %s at %s not written", pull, kind, head[:12])
            if hold is not None:
                _hold_unrecorded(hold, pull, head, when)
        return stored

    async def stale_findings(
        self, work_id: str, text: str, repository: str, source: str = ""
    ) -> str:
        """Why findings ``text`` posted in ``repository`` are about a commit its branch left,
        or ``""`` to hand them on (#3192). Only a pull request of this Work is read: a bare
        ``#12`` may name another repository's pull request than the one it was posted in.
        Findings handed on are a ``findings`` event in their head's history once ``on_findings``
        with the same ``source`` (the reviewer's request id) knows whether its agent heard them
        (#3957), which a base merge after them may keep (#3314 review)."""
        target = findings_target(text, repository)
        if target is None or target[0] not in {pull.lower() for pull in self.ports.pulls(work_id)}:
            return ""
        standing = await self._standing_head(*target)
        if standing is not None:
            if source:
                while len(self._findings_heads) >= 100:
                    del self._findings_heads[next(iter(self._findings_heads))]
                at_head = standing or self._full_head(*target)
                self._findings_heads[source] = (target[0], at_head, target[1])
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
                self._note_findings(entry, key, False, text)
                self.store.put(work_id, entry)
            return False
        body = text.strip()
        if len(body) > MAX_FINDINGS:
            body = body[:MAX_FINDINGS].rstrip() + " …"
        # Findings on a pull request merged by hand while its review ran can only be fixed
        # after it, in a follow-up pull request (#4189).
        found = self._findings_heads.get(key)
        ask = "Fix the findings, push, and mark the pull request ready again."
        if found is not None and await self._merged(found[0]):
            ask = (
                f"{found[0]} had already merged before the review finished: fix the findings"
                " in a follow-up pull request."
            )
        message = (
            f"AgentDeck: the deck's reviewer asks for changes on {_ref_of(work_id)}:\n\n{body}\n\n"
            f"{url}\n\n{ask} {OWNER_CALL_ASK}"
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
            self._note_findings(entry, key, told, text)
            self.store.put(work_id, entry)
            # The deck's reviewer reviews the Work's one chat, which covers all its pull
            # requests; told first, so a refused message keeps the wait (#3183 review).
            self._review_answered(work_id, entry, f"findings:{key}", None)
        return told

    def _note_findings(self, entry: dict, key: str, told: bool, text: str = "") -> None:
        """The ``findings`` event of request ``key`` at the head ``stale_findings`` found
        (#3957): keyed by whether they were told, so a resend that reaches the agent is newer.
        Findings whose text names no head have none, and write nothing; ones not written hold
        that head in ``entry`` (#3956 review)."""
        found = self._findings_heads.get(key)
        if found is not None:
            pull, head, about = found
            told_key = f"findings:{key}:{'told' if told else 'untold'}"
            data = {"told": told, "text": review_history.clip_findings(text)}
            self._note(pull, head, review_history.FINDINGS, told_key, data, about, hold=entry)
            if told:
                del self._findings_heads[key]

    def _reviewer(self, pull: str, head: str, by: str | None) -> str:
        """Who reviewed, for a message: the review chat, else the login, else the deck's own."""
        chat = self.review_chat(pull, head)
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
            for ref in refs:
                pull = await self.ports.read_pull(ref)
                if pull is None:
                    raise PressRefused(f"{ref} could not be read, so no Merge was recorded")
                if pull.state != "OPEN":
                    continue
                verdict = self._verdict_at(ref, pull.head)
                digest = str(verdict.data.get("digest") or "") if verdict is not None else ""
                if not review_history.passed(verdict) or not digest:
                    digest = await self.ports.digest(ref, pull.head)
                if not digest:
                    raise PressRefused(f"{ref}'s diff could not be read, so no Merge was recorded")
                pulls[ref] = {"digest": digest, "head": pull.head}
            if not pulls:
                raise PressRefused("no pull request is open, so no Merge was recorded")
            pressed_at = self.ports.clock()
            if self.ports.press_is_review:
                # Nothing else here can review: the person pressing Merge has looked at these
                # diffs, so the press passes them at the heads it was for (#3623). Tests must
                # still be green there before anything merges. A pass not stored records no
                # press either, so the person can press again (#3956 review).
                for ref, pressed in pulls.items():
                    key = f"press:{ref}@{pressed['head']}@{pressed_at}"
                    data = {"digest": pressed["digest"], "by": "a Merge press"}
                    if not self._note(ref, pressed["head"], review_history.PASS, key, data):
                        raise PressRefused(
                            f"{ref}'s review pass could not be recorded, so no Merge was"
                            " recorded; press Merge again"
                        )
            entry["asked"] = {"at": pressed_at, "pulls": pulls}
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

    def approval(self, work_id: str, pulls: Iterable[tuple[str, str, bool]] | None = None) -> str:
        """What the Work page offers for an early Merge (#3320): "" for nothing (not released
        here, or a Work that merges on its own), ``offered`` when no press stands, and
        ``approved`` while one does.

        ``pulls`` are the Work's pull requests as the page shows them, ``(ref, state,
        draft)``: the web cannot ask the services process for them (#4172 review), and the
        page and the release judge them by one rule (``offered_pulls``). Without them only a
        project that merges on its own offers nothing.
        """
        if not self.handles(work_id):
            return ""
        try:
            policy = self.ports.policy(_repository_of(work_id))
            if policy.auto_release:
                return ""
            if pulls is not None and policy.merges_itself(offered_pulls(policy, pulls)):
                return ""
            entry = self.store.works.get(work_id) or {}
        except Exception:  # noqa: BLE001 -- unreadable: nothing offered
            return ""
        return MERGE_APPROVED if entry.get("asked") else MERGE_OFFERED

    def approval_reason(self, work_id: str, pulls: Iterable[tuple[str, str, bool]] = ()) -> str:
        """Why this Work waits for a person's Merge though one of its repositories merges on
        its own (#4172), else "": its pull requests elsewhere hold them all. ``pulls`` as for
        ``approval``."""
        if not self.handles(work_id):
            return ""
        try:
            policy = self.ports.policy(_repository_of(work_id))
            if policy.auto_release or not policy.auto_release_repos:
                return ""
            offered = offered_pulls(policy, pulls)
            repositories = sorted({pull.partition("#")[0] for pull in offered})
        except Exception:  # noqa: BLE001 -- unreadable: no line
            return ""
        owner = _repository_of(work_id).partition("/")[0].lower() + "/"

        def name(repo: str) -> str:  # the project's own repositories by their short name
            return repo.removeprefix(owner)

        own = [name(repo) for repo in repositories if repo in policy.auto_release_repos]
        held = [name(repo) for repo in repositories if repo not in policy.auto_release_repos]
        if not own or not held:
            return ""
        return (
            f"{', '.join(own)} merges on its own, but this Work also has a pull request in"
            f" {', '.join(held)}, so all of them wait for your Merge."
        )

    def approval_waits(self, work_id: str) -> str:
        """The Work page's line while a press stands and something still holds it (#3869).

        A press made early, or one a base update carried to a new head, looks ignored while a
        long CI run goes: this says it is armed and what it waits for now. "" with no press,
        or once nothing holds it.
        """
        try:
            entry = self.store.works.get(work_id) or {}
        except Exception:  # noqa: BLE001 -- unreadable: no line
            return ""
        hold = entry.get("hold")
        if not entry.get("asked") or not isinstance(hold, dict):
            return ""
        words = hold_line(hold)
        if not words:
            return ""
        return f"Merge armed: it merges with no second press once nothing holds it. Now: {words}."

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
        return f"Merge approval cleared: {what}. Press Merge again to merge it."

    async def _check_press(self, work_id: str, entry: dict, facts: dict[str, Pull]) -> None:
        """Clear a person's Merge press once a diff it was for changed (#3320).

        A head that moved is read once: the same digest (a merge of the base) moves the
        press's head, a changed one clears it, and a failed read changes nothing.
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
        # Its agent would otherwise go on saying the merge is armed (#4089).
        self._tell(
            work_id,
            entry,
            f"press-cleared:{ref}@{head[:12]}",
            PRESS_CLEARED_TEXT.format(ref=ref, head=head[:12], why=why),
        )

    async def on_pull_event(self, work_id: str, pull: str, action: str, merged: bool) -> None:
        """A ``pull_request`` delivery on a pull request this Work registered."""
        if action == "closed":
            with contextlib.suppress(Exception):  # its history only; the release goes on
                self.ports.forget_reviews(pull.lower())
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
        if action == "closed" and merged and self.handles(work_id):
            # Its base moved, so a sibling pull request may conflict now, and GitHub says so
            # only a while after (#4217): one more look then.
            self._look_soon(work_id, MERGEABLE_SETTLE_S)

    def on_base_moved(self, repository: str, branch: str) -> list[str]:
        """A push to ``branch`` in ``repository``: look again at each Work waiting on a Merge
        whose pull request there merges into it (#4217).

        Whatever moved the branch (another Work's merge, a hand push, a pull request no Work
        registered), a pull request waiting on a person's Merge may conflict now, and nothing
        on that pull request says so: the Work page would read Ready for merge while nobody
        has a dashboard open. GitHub works ``mergeable`` out lazily, so the look is one timed
        look after ``MERGEABLE_SETTLE_S``, at most one held per Work however many pushes land,
        and no read happens here. A pull request whose base was never read is looked at too.
        Returns the Works it will look at.
        """
        repository = repository.lower()
        looked = []
        for work_id in sorted(self.store.works):
            entry = self.store.get(work_id)
            awaiting = entry.get("awaiting_merge")
            if not isinstance(awaiting, dict) or not self.handles(work_id):
                continue
            bases = entry.get("awaiting_bases")
            bases = bases if isinstance(bases, dict) else {}
            if any(
                str(ref).partition("#")[0].lower() == repository
                and str(bases.get(ref) or branch) == branch
                for ref in awaiting
            ):
                self._look_soon(work_id, MERGEABLE_SETTLE_S)
                looked.append(work_id)
        return looked

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
        pulls = None
        for work_id in sorted(self.store.works):
            policy = self.ports.policy(_repository_of(work_id))
            if repository in policy.review_only:
                continue
            if pulls is None:
                # One walk of the deck for the whole status, never one per Work (#3993).
                lookup = self.ports.pulls_lookup
                pulls = lookup() if lookup is not None else self.ports.pulls
            if any(
                ref.partition("#")[0].lower() == repository
                and review_history.passed(self._verdict_at(ref, sha))
                for ref in set(pulls(work_id))
            ):
                try:
                    answer = await self.on_checks_done(work_id)
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

    async def on_checks_done(self, work_id: str) -> str:
        """A check or a commit status finished on one of this Work's pull requests (#3911).

        GitHub sends a suite's ``completed`` before its rollup reads the last run done, so a
        look that still reads the checks pending looks once more after ``CHECKS_SETTLE_S``.
        Nothing else would: a Work waiting on its checks is in no queue and carries no other
        wake mark, and that was the last delivery its CI sends.
        """
        answer = await self.evaluate(work_id)
        if (self.store.works.get(work_id) or {}).get("waiting_checks"):
            self._look_later(work_id, CHECKS_SETTLE_S)
        return answer

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
        await self._read_held(work_id)
        async with self._lock(work_id):
            self._looked[work_id] = self.ports.clock()
            entry = self.store.get(work_id)
            answer = await self._evaluate(work_id, entry, tell_untested)
            log.info("work-release: %s: %s", work_id, answer or "nothing holds it")
            _note_hold(entry, answer, self.ports.clock(), entry.pop("pull_waits", None))
            if _failed_read(answer) and self._has_pass(work_id):
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
        a draft; one conflicts with its base (#4217); one has no review pass at its head; one
        has no green tests at its head. Only
        the reason that stops it now leaves its wake mark (``waiting_turn``, ``waiting_tests``).
        """
        entry.pop("waiting_turn", None)
        entry.pop("waiting_tests", None)
        entry.pop("waiting_checks", None)
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
        conflicts = [facts[ref] for ref in open_pulls if facts[ref].conflicting]
        if conflicts:
            # Never offered for Merge, nor kept in line, while GitHub says it cannot merge
            # (#4217): its agent is told now, not when a person presses Merge. Before the
            # review and the tests, as a pull request that conflicts may run no CI at all.
            for pull in conflicts:
                self._tell_conflict(work_id, entry, pull)
            first = conflicts[0]
            return f"{first.ref} conflicts with its base at {first.head[:12]}"
        green: set[str] | None = None
        for ref in open_pulls:
            pull = facts[ref]
            history = self._history(ref)
            here = review_history.at_head(history, pull.head)
            newest = review_history.newest_verdict(history)
            unrecorded = (entry.get("unrecorded") or {}).get(ref)
            if isinstance(unrecorded, dict):
                # Findings that were not written hold the whole pull request, whatever head a
                # base update moved it to, until a newer verdict is written; a carried pass is
                # not one, as it only copies the older answer (#3956 review).
                reviewed = review_history.newest_verdict(
                    event for event in history if not event.key.startswith("carried:")
                )
                if reviewed is None or reviewed.at <= float(unrecorded.get("at") or 0):
                    held = str(unrecorded.get("head") or "")[:12]
                    return f"{ref}'s last verdict at {held} {UNRECORDED}"
                entry["unrecorded"].pop(ref, None)
                if not entry["unrecorded"]:
                    entry.pop("unrecorded")
            if (
                review_history.newest_verdict(here) is None
                and review_history.passed(newest)
                # A review asked for after the pass, at any head of the pull request, waits for
                # its own verdict, so no base update can carry the pass past it.
                and not any(
                    event.kind == review_history.ASKED
                    and (event.at, event.seq) > (newest.at, newest.seq)
                    for event in history
                )
                and newest.data.get("digest")
                and not same_commit(newest.head, pull.head)
            ):
                # A branch brought up to its base moves the head but not the diff the reviewer
                # read: the pass carries over when the digest at the new head is the same,
                # written as that head's own row, so a head's answer reads only its own rows.
                current = await self.ports.digest(ref, pull.head)
                if not current:
                    # A failed diff read, or a head that moved between reads: not a changed
                    # diff, so not a lost pass either. Read again when a job moves, as its
                    # re-test finishing is what it waits on next.
                    entry["waiting_tests"] = True
                    return f"{ref}'s diff at {pull.head[:12]} {DIGEST_UNREADABLE}"
                if current == newest.data["digest"]:
                    key = f"carried:{ref}@{pull.head}"
                    self._note(ref, pull.head, review_history.PASS, key, newest.data, newest.head)
                    here = review_history.at_head(self._history(ref), pull.head)
            if not review_history.passed(self._verdict_at(ref, pull.head)):
                held_pass = (entry.get(HELD_PASSES) or {}).get(ref) or {}
                if same_commit(pull.head, str(held_pass.get("head") or "")):
                    return f"{ref}'s review pass at {pull.head[:12]} {PASS_HELD}"
                if review_history.ship_pass_head(self._history(ref), pull.head):
                    return f"{ref}'s Ship now at {pull.head[:12]} {SHIP_UNFINISHED}"
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
                    if pull.checks == "pending":
                        entry["waiting_checks"] = True
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
                entry["waiting_checks"] = True
                return f"{ref}'s checks are pending at {pull.head[:12]}"
            if pull.repository in policy.checks_required and pull.checks != "green":
                # Its CI has not reported on this head yet (#3919): the job proves only part.
                entry["waiting_checks"] = True
                return f"{ref}'s checks are {pull.checks} at {pull.head[:12]}"
        return ""

    async def _pull_waits(
        self, work_id: str, policy: Policy, facts: dict[str, Pull], open_pulls: list[str]
    ) -> list[dict[str, str]]:
        """What each open pull request lacks now, as ``{"pull": ref, "lacks": "not reviewed"}``
        (#4089); ``hold_line`` words it.

        ``_hold_reason`` stops at the first thing it meets, and an agent's turn comes first of
        all, so on its own the hold named one pull request or none, and changed as chats moved.
        This only reads: it tells nobody and marks no wake. A failed read of the tests leaves
        them out, as nothing is known about them.
        """
        said: list[dict[str, str]] = []
        green: set[str] | None = None
        unread = False
        for ref in open_pulls:
            pull = facts[ref]
            missing = ""
            if pull.draft:
                missing = "still a draft"
            elif pull.conflicting:
                missing = "conflicts with its base"
            elif not review_history.passed(self._verdict_at(ref, pull.head)):
                missing = "not reviewed"
            elif pull.repository in policy.review_only:
                pass
            elif pull.checks == "red":
                missing = "checks are red"
            elif pull.repository in policy.github_checks:
                missing = "" if pull.checks == "green" else f"checks are {pull.checks}"
            else:
                if green is None and not unread:
                    try:
                        green = {sha.lower() for sha in await self.ports.green_heads(work_id)}
                    except Exception:  # noqa: BLE001 - this line only: the hold itself stands
                        log.warning("work-release: %s tests unread", work_id, exc_info=True)
                        unread = True
                if green is None:
                    continue
                if not any(same_commit(sha, pull.head) for sha in green):
                    missing = "tests are not green"
                elif pull.checks == "pending" or (
                    pull.repository in policy.checks_required and pull.checks != "green"
                ):
                    missing = f"checks are {pull.checks}"
            if missing:
                said.append({"pull": ref, "lacks": missing})
        return said

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
            # has not scanned the Work's chats yet reads none, and acting on that
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
        # Whether a pass merges by itself is decided by the repositories of the pull requests
        # offered (#4172), the same ones as ``open_pulls`` below, so a draft next step elsewhere
        # holds nothing and every reader below sees the Work's own answer.
        policy = policy.for_pulls(
            offered_pulls(policy, ((ref, facts[ref].state, facts[ref].draft) for ref in refs))
        )
        if not policy.auto_release:
            await self._check_press(work_id, entry, facts)
        # Set again below only while a person's Merge is all that is missing (#2960).
        awaiting = entry.pop("awaiting_merge", None)
        bases = entry.pop("awaiting_bases", None)
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
                "waiting_checks",
                "queued",
            ):
                entry.pop(key, None)
            self._leave(work_id)
            if any(facts[ref].state == "OPEN" for ref in refs):
                return "only drafts are open"
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
        verdicts = {ref: self._verdict_at(ref, facts[ref].head) for ref in open_pulls}
        reviewed = all(review_history.passed(verdict) for verdict in verdicts.values())
        # The diff each open pull request's pass is for ("" with none), which a press covers.
        digests = {
            ref: str(verdict.data.get("digest") or "") if review_history.passed(verdict) else ""
            for ref, verdict in verdicts.items()
        }
        if not policy.auto_release and entry.get("asked"):
            if reviewed and not _press_stands(entry, digests):
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
            if bases is not None:
                entry["awaiting_bases"] = bases
        if reason:
            entry["pull_waits"] = await self._pull_waits(work_id, policy, facts, open_pulls)
            return await self._queue_hold(work_id, entry, policy, facts, reason)
        if any(facts[ref].mergeable == "UNKNOWN" for ref in open_pulls):
            # GitHub is still working its answer out, so whether this merges is not known
            # yet: one more look once it has (#4217).
            self._look_soon(work_id, MERGEABLE_SETTLE_S)
        if not policy.auto_release and not _press_stands(entry, digests):
            # The Work page reads this as Ready for merge (#2960). Not in line until pressed.
            self._leave(work_id)
            entry["awaiting_merge"] = {ref: facts[ref].head for ref in open_pulls}
            # Which branch each merges into, so a push there looks again (#4217).
            entry["awaiting_bases"] = {ref: facts[ref].base for ref in open_pulls}
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
        if pull.conflicting:
            self._tell_conflict(work_id, entry, pull)
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
            # The Work page's "Merge armed" line reads this until the new head shows (#3869).
            return False, f"{ref} is behind its base; the deck merged the base in for a re-test"
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

    def _look_soon(self, work_id: str, delay: float) -> None:
        """``_look_later``, unless a timed look at this Work is already held (#4217): that look
        reads the merge answers too, and a shorter one must not replace a longer one."""
        if work_id not in self._timers:
            self._look_later(work_id, delay)

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
                await self._retry_raised_look(work_id)

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

    async def reorder_and_wake(self, repository: str, pulls: list[str]) -> list[dict]:
        """``reorder``, then ``wake_first``: the merge-queue route's one call (#4016)."""
        self.reorder(repository, pulls)
        await self.wake_first()
        return self.queue(repository)

    # --- helpers ---

    def _lock(self, work_id: str) -> asyncio.Lock:
        return self.ports.locks.setdefault(work_id, asyncio.Lock())

    def _verdict_tier(self, work_id: str, pulls: Iterable[str] = ()):
        """The tier a ``/reviewed`` needs on this Work's project, as the Poller decides it.

        Release only where a pass can merge with nobody in the loop (``auto_release``); where a
        person's Merge press still follows it, Work (#2375). #2895 asked Release everywhere, so
        every verdict of one project's Work-tier reviewer was thrown away (#3157).
        """
        from .poller_engine.authorization import Tier, required_tier
        from .poller_engine.intake import CommandKind

        policy = self.ports.policy(_repository_of(work_id))
        # Per pull request (#4172): a pass on one in a repository that merges on its own needs
        # Release there alone. With none named, every pull request of the Work, strictest first.
        named = tuple(pulls) or tuple(self.ports.pulls(work_id))
        merges = policy.auto_release or any(policy.merges_itself([pull]) for pull in named)
        tier = required_tier(CommandKind.REVIEWED.value, reviewed_merges=merges)
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

    def _tell_conflict(self, work_id: str, entry: dict, pull: Pull) -> None:
        """Tell the agent once per head that ``pull`` conflicts, and take it out of line."""
        self._tell(
            work_id,
            entry,
            f"conflict:{pull.ref}@{pull.head[:12]}",
            CONFLICT_TEXT.format(ref=pull.ref, head=pull.head[:12]),
        )
        self._leave(work_id, refs=[pull.ref])

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
        if self._tell(
            work_id, entry, f"live-check:{','.join(refs)}", _live_check_text(policy, refs)
        ):
            entry["live_check_asked"] = {"at": self.ports.clock(), "pulls": refs}


def _live_check_text(policy: Policy, refs: list[str]) -> str:
    """``LIVE_CHECK_TEXT`` for these merged pull requests."""
    return LIVE_CHECK_TEXT.format(refs=", ".join(refs), deploy=deploy_sentence(policy, refs))


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


def _press_stands(entry: dict, digests: dict[str, str]) -> bool:
    """Does a person's Merge press cover the open pull requests at the diffs passed now
    (``digests``, by pull request; #3175)?"""
    if entry.get("asked") is True:
        return True  # pressed on a deck that kept no digests: for whatever is open
    pulls = _asked_pulls(entry)
    if not pulls:
        return False
    return all(
        (pulls.get(ref) or {}).get("digest") and (pulls.get(ref) or {}).get("digest") == digest
        for ref, digest in digests.items()
    )


def _note_hold(
    entry: dict, answer: str, now: float, pulls: list[dict[str, str]] | None = None
) -> None:
    """Keep what holds the Work now in plain words, and since when it has (#3097).

    The Work's line reads it when the agent's only wait is its own pull request merging, which
    says nothing about what that pull request waits for. ``since`` stays while the answer does,
    so a hold nothing moves for hours reads as stuck. ``pulls`` is what each open pull request
    lacks (``_pull_waits``), read back by ``hold_line``.
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
    if pulls:
        entry["hold"]["pulls"] = pulls


def _short(ref: str) -> str:
    """``owner/metso#77`` as a person says it: ``metso#77``."""
    return ref.rpartition("/")[2]


def hold_line(hold: dict) -> str:
    """A kept hold as one line naming each pull request it is about (#4089): what stops the
    Work first, then what the others lack, as ``its agent is still working; metso#77: not
    reviewed``. Two of one short name read in full. A hold from before ``pulls`` was kept has
    only its own words."""
    words = str(hold.get("words") or "").removeprefix("Merge pressed; ")
    found = _HOLD_PULL_RE.match(str(hold.get("why") or ""))
    first = found.group(0).lower() if found else ""
    waits = [(str(w["pull"]).lower(), str(w["lacks"])) for w in hold.get("pulls") or ()]
    shorts = Counter(_short(ref) for ref in {first, *(ref for ref, _ in waits)} - {""})

    def name(ref: str) -> str:
        return _short(ref) if shorts[_short(ref)] == 1 else ref

    if words and first and first not in words.lower():
        words = f"{name(first)}: {words}"
    others = [f"{name(ref)}: {lacks}" for ref, lacks in waits if ref != first]
    return "; ".join(part for part in (words, *others) if part)


def _held_now(entry: dict, answer: str) -> str:
    """``answer`` as the Work's chat reads it: the line naming every pull request while the
    look left that answer's hold, else the answer."""
    hold = entry.get("hold")
    if answer and isinstance(hold, dict) and hold.get("why") == answer:
        return hold_line(hold) or answer
    return answer


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
    if PASS_HELD in answer:
        return "review passed; waiting to read its diff", False
    if TREE_MOVED in answer:  # a place in the merge queue may follow it
        return "tests passed but the tree changed during the run, so they don't count", False
    if TREE_UNCLEAN in answer:
        return "tests passed but the checkout had uncommitted or untracked files", False
    if " has no green verification at " in answer:
        return ("no test run started" if needs_verify else "its tests are not green"), False
    if " conflicts with its base" in answer:
        return "conflicts with its base; its agent merges the base in", False
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
        base=base,
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
