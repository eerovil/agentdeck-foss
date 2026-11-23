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
them fixed (``no_code_fixed``) that review counts as a pass, with no new one; a push of doc
files alone after a standing pass keeps that pass the same way (#4257). ``minor`` (#4474) is
``no-code`` with small code fixes allowed: the router lets a fix push through only when it
stays small (``work_router.MINOR_LINES``). A recorded verdict,
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
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import live_check_scope, mechanism_check, review_history, work_rounds
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

#: Why a held press was dropped (#4574): its CI fix's review asked for changes.
FIX_REFUSED = "had a CI fix whose review asked for changes"
#: Why a press was dropped by a refusal of a head it never stood for (#4574).
REVIEW_REFUSED = "had a new head whose review asked for changes"
#: Added to the red-checks message while a person's Merge press stands (#4574).
RED_PRESS_TEXT = (
    " The person's Merge press stands through your fix: once the fix's review passes and its"
    " checks are green, the deck merges with no second press. A review of the fix that asks"
    " for changes drops the press."
)
#: How many of a pull request's own commits ``replaced_checks`` walks back (#4574).
FIX_WALK_MAX = 10

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
#: A light pull request (#4399) whose bench said no worse, with no green ``bin/test.sh`` run
#: the deck could start itself.
LIGHT_TESTS_TEXT = (
    "AgentDeck: the bench verdict for {ref} at {head} is no worse than master, so it merges with"
    " no reviewer once a run of exactly `bin/test.sh` is green there. Run it as a verification"
    " job from a checkout of that head: `~/agentdeck/.venv/bin/agentdeck job submit"
    ' --verification --session-token "$AGENTDECK_SESSION_TOKEN" --work {work} --cwd'
    " <checkout> -- bin/test.sh`, with no arguments and no shell around it."
)
REVIEW_PASSED_TEXT = "AgentDeck: the review of {ref} passed at {head}. " + VERIFY_TEXT
#: A pass whose digest could not be read at the new head: no answer, so a place in the merge
#: queue stands (#3022 review).
DIGEST_UNREADABLE = "could not be read to carry its review pass over"
#: How long the deck waits for an update it made to show on GitHub, and how many times it asks
#: for one head, before a pull request that stays behind leaves the merge queue (#3033).
UPDATE_GRACE_S = 600.0
#: When a light pull request's read failed, the release looks again this much later (#4399).
LIGHT_UNREAD_S = 60.0
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
#: How long a pull request's checks may read pending before the deck says its CI never
#: reported (#4328). A CI can finish its build and lose the status write that ends it: Drone
#: built a pull request in minutes and its ``drone/push`` status read pending for good, so the Work
#: waited on it until its agent's wake-on ran out a day later. Drone's runs here take 15
#: minutes at most; an hour is four of them.
CHECKS_STUCK_S = 3600.0
#: Ends ``_hold_reason``'s answer for checks pending past ``CHECKS_STUCK_S`` (#4328).
CHECKS_NEVER_REPORTED = "and have not reported for over an hour"
#: The release entry's ``ref@head -> first seen`` for checks pending with no start time GitHub
#: gave (#4328 review): kept only while each still reads pending, so a re-run starts afresh.
PENDING_SEEN = "pending_seen"
#: How a status line says it: only a person moves it, by re-running that CI or merging by hand.
CHECKS_NEVER_REPORTED_WORDS = (
    "held: its checks never reported (pending over an hour); re-run its CI or merge by hand"
)
#: The one message to a Work's agent when a pull request conflicts with its base (#4217), once
#: per head, whether the deck found it before offering Merge or while merging.
CONFLICT_TEXT = (
    "AgentDeck: {ref} conflicts with its base at {head}, so it cannot merge. Merge the base"
    " in, resolve the conflict, verify, push, and mark it ready again."
)
#: Added when the head that conflicts is one the owner shipped with Ship now (#4576): a base
#: merge that leaves its code as it was keeps that pass, so no new review is needed.
SHIP_CONFLICT_TEXT = (
    " The owner shipped this commit with Ship now: resolve it with a merge commit of the base"
    " only (`git merge`, no rebase) and push nothing else, keeping its code changes as they"
    " are. Then the Ship now pass carries over to the merge and no new review starts; any"
    " other code change is reviewed again."
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
#: How a reviewer marks a problem that came back after a fix (#4208): the earlier findings, their
#: patches and the one question they all answer. On store#3943 the agent was asked twice whether
#: to accept such a problem and recommended one more narrow fix both times; on issues#796 it
#: answered three escalations itself with three more patches. The owner chose (#4476) that the
#: agent writes a redesign at once and asks them to approve it before building it; the design
#: comment also holds code review until the deck's design reviewer passes it (#4332).
DESIGN_FINDING_MARK = "Design finding:"
#: Ends ``SIBLING_SWEEP_ASK`` (#4497): a finding that comes back is fixed at the cause it
#: shares with the earlier one. In #4490's 16 costliest Works, fixes that brought a flaw back
#: cost 47 rounds; #4360 patched one text-and-time match four times before its shared cause
#: was fixed. A repeat the reviewer marks as a design finding follows ``DESIGN_FINDING_ASK``
#: instead (#4497 helper checks): a redesign the owner approves, not a code change at once.
SHARED_CAUSE_ASK = (
    "When a finding repeats an earlier round's finding (the same flaw, or the same code or"
    " mechanism flagged again), fix the cause they share, not only the place named, and say in"
    " your reply which cause that was; a finding whose line starts"
    f' "{DESIGN_FINDING_MARK}" is not fixed this way: it gets a redesign the owner approves'
    " first."
)
#: The one message to a Work's agent once its last open pull request merged (#3141): check the
#: change live, then record the result, which the Work's status line and its Finish read.
LIVE_CHECK_TEXT = (
    "AgentDeck: {refs} merged. Check that it works live now. {deploy} Then check the change"
    " yourself with what you can reach: try it in the live app the way a person would use it,"
    " or read production's logs, Sentry, its data or its API. When you can make the changed"
    " path run yourself (start a test or trial job, load the page, call the endpoint), run it"
    " now and check that run: wait for real use only for what you cannot start yourself"
    " (#4309). When the changed path is a scheduled or nightly job, do not register a time"
    " wait for its schedule: offer to run that job once now in production, the repository's"
    " own way (its guide, a saved Mac script, or a Mac command such as `kubectl create job"
    " --from=cronjob/<name> <name>-manual-<date>`), run it only once the person allowed it,"
    " then check that run. A Mac request that answers `pending` is allowed by its tap, so"
    " name the job and the merged pull request in it and post its approval link; before any"
    " other route, a Mac script marked safe included (`scripts.json` says which: it starts"
    " with no tap), ask the person"
    " to allow that run once with your question tool. First check that the job is not"
    " already running, so the two runs do not overlap"
    " (#4320). Unit tests and a deploy alone do not prove a fix: for a backend or Sentry fix"
    " whose path you cannot start, look in production now for the evidence. Once that first"
    " check passes and a command can tell the problem is back (the error recurs, the job fails,"
    " the number is wrong again), register a `watch` wake-on of about 3 days with that command"
    " (`GET $AGENTDECK_URL/api/guide/waits` gives its body), record `verified` and close the"
    " issue: the deck runs the command, and reopens the issue and messages you only if the"
    " problem comes back (#4466). Only when you need to see the fixed path run, or a result no"
    " command can judge, register a time wait (about a day), with a `check` command when one"
    " can tell (the deck then wakes you only when it finds it). If no evidence shows in 3 days"
    " because the path simply did not run, record `verified`, say no evidence showed, and"
    " close (#4210). When only the owner's Mac reaches"
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
#: The live check of a merge that changes only tests or docs (#4500,
#: ``live_check_scope.docs_only``): nothing runs differently live, so no deploy wait and no
#: probe. ``{close}`` is ``DOCS_ONLY_CLOSE``, or "" when a Ship now addition keeps it open.
DOCS_ONLY_TEXT = (
    "AgentDeck: {refs} merged. It changes only tests or docs, so nothing runs differently"
    " live, and there is no deploy to wait for. Record the live check with"
    " `PUT $AGENTDECK_URL/api/sessions/<your session key>/live-check` and"
    ' `{{"result": "nothing_to_check", "note": "<one line naming the files, under 500'
    ' characters>"}}`{close}.'
)
DOCS_ONLY_CLOSE = (
    ", then close the issue yourself with a one-line summary; do not ask anyone whether to"
    " close it (#3437). If a person asked for more work on the issue after the merge, keep"
    " it open and carry on with that work (#3905). If a step the issue needs is left for a"
    " person to do (switch on a key, type a password), keep the issue open and make that step"
    " the question in your reply's last paragraph (#4151)"
)
#: The live check of any other merge into this deck (#4500): a short one. #4489 found the
#: cost in a few long checks, while the one live-only fault (#4474) showed on real use after
#: a short probe passed. So: one run of the changed path, no staging by hand, a wait for
#: real use when the probe shows nothing, and ``verified`` still only once the path ran.
DECK_SHORT_TEXT = (
    "AgentDeck: {refs} merged. Check that it works live now, in a short check of about 10"
    " tool calls (#4500). {deploy}{processes}{skills} Then make the changed path run once"
    " yourself and check that run (#4309): one request to the endpoint, one page load with a"
    " screenshot cropped to the change, or one log line or database row it writes. Do not"
    " stage a path only real use starts: no throwaway pull requests, seeded rows or fake"
    " webhooks. At about 10 calls, stop and record by what you have, with"
    " `PUT $AGENTDECK_URL/api/sessions/<your session key>/live-check` and"
    ' `{{"result": "verified" | "failed" | "not_live" | "needs_person", "note": "<one line>"}}`.'
    " The path ran and works: `verified`; when a command can tell the problem is back, first"
    " register a `watch` wake-on of about 3 days with it (`GET $AGENTDECK_URL/api/guide/waits`"
    " gives its body), and the deck reopens the issue if it comes back (#4466). Merged but"
    " not running yet: `not_live`, naming what (#3220). The probe could not make the path"
    " run, or its result does not show whether it works: record nothing yet, register a"
    " time wait with a `check` command that finds the path's next real run (or a `watch`"
    " when a command can tell the fault), end the turn, and check that run when it wakes"
    " you; if no evidence shows in 3 days because the path did not run, record `verified`"
    " and say no evidence showed (#4210). Only a person can judge it: `needs_person` with a"
    " note saying what to try, and end your turn with no question (#4210). Post what you"
    " checked on the issue in one comment. Once you record `verified`, close the issue"
    " yourself with a one-line summary; do not ask anyone whether to close it (#3437). If a"
    " person asked for more work on the issue after the merge, record `verified` all the"
    " same, keep the issue open and carry on with that work (#3905). If a step the issue"
    " needs is left for a person to do (switch on a key, type a password), also record"
    " `verified`, keep the issue open, and make that step the question in your reply's last"
    " paragraph (#4151); trying it is not such a step. If it failed, record `failed`, reopen"
    " the issue, say there what failed, and fix it in a new pull request."
)
#: ``{processes}`` of ``DECK_SHORT_TEXT``, from ``live_check_scope.loading_processes``.
PROCESSES_TEXT = (
    " It changes code the {names}: confirm {which} restarted after the merge (start time)"
    " before you probe (#2835)."
)
PROCESSES_UNREAD_TEXT = (
    " The deck could not read which process loads it: before you probe, confirm that each"
    " of the web, services and runtime processes the change touches restarted after the"
    " merge (their start times, #2835)."
)
#: ``{skills}``: a merge the deploy's skills install carries to every agent (#4500).
SKILLS_TEXT = (
    " It changes what the deploy installs into every agent (bundled skills or the lessons):"
    " confirm the deploy's `deploy.log` (in `$AGENTDECK_DEPLOY_STATE`, by default"
    " `~/.local/share/agentdeck/deploy/`) says `deployed bundled skills at <sha>`, where"
    " `<sha>` is the merge commit or a later master commit that contains it (one deploy can"
    " carry several merges; `git merge-base --is-ancestor <merge> <sha>` tells)."
)
#: Also in ``{skills}``: a merge into the poller, the deploy or a service unit, which a deploy
#: can break with no page showing it (#4500 helper checks).
DEPLOY_TEXT = (
    " It changes the poller, the deploy or a service unit: confirm the deploy's `deploy.log`"
    " shows the deploy of this merge finished with no refusal, and for the poller its next"
    " cycle in its own log."
)
#: Part of ``SIBLING_SWEEP_ASK`` and ``MINOR_TEXT``, so every message that asks for a code fix
#: carries it (#4499, from #4492):
#: the fixes that swapped one mechanism for another drew their later bugs from the new call's
#: own defaults, which nobody had looked up.
OUTSIDE_DEFAULTS_ASK = (
    "If your fix starts calling an outside API, library or operating-system feature it did not"
    " call before, look up that call's defaults first (timeouts, retries, caching, limits,"
    " encodings), and say in your reply which defaults you checked and why each fits."
)
#: Put before "push" in every message that asks a Work's agent to fix review findings
#: (#4406, #4430): 37 later-round findings in five days were the same flaw at another place,
#: and a sweep of real fix commits found 6 of 10 of them in about a minute each.
SIBLING_SWEEP_ASK = (
    "Before you push, look for the same flaw elsewhere: in other places, inputs and states the"
    " code reaches, and in your own fix. Fix only the cases you confirm with a test or a"
    " concrete code path, and name them in the commit message."
    " " + SHARED_CAUSE_ASK + " " + OUTSIDE_DEFAULTS_ASK
)
#: Added to ``LIVE_CHECK_TEXT`` when a merged pull request shipped with Ship now (#4212
#: review): its findings are this Work's to fix, so recording the live check must not close it.
SHIPPED_LIVE_CHECK = (
    " But {refs} shipped with Ship now, its review findings left to fix in this same Work,"
    " and that fix has not merged yet: record the live check as above, then keep the issue"
    " open, whatever the result. Fix those findings in a pull request for this issue. "
    + SIBLING_SWEEP_ASK
    + ' Register it with `"fixes": "<the shipped pull request>"` (POST …/pull-requests) unless'
    " the deck already named it. Close the issue only once that fix has merged and its own"
    " live check is recorded. If that fix is a draft kept until this merge, merge its base"
    " into it now and mark it ready."
)
#: Added instead when the deck could not read what this Work shipped with Ship now (#4212
#: review): unknown is not "nothing left", so the close is not authorized.
SHIPPED_UNREAD_LIVE_CHECK = (
    " The deck could not read whether this Work shipped a pull request with Ship now whose"
    " review findings are still to fix: record the live check as above, but before closing"
    " the issue, check the Work's pull requests yourself and keep it open while any shipped"
    " one's findings are unfixed."
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
    " live. If it is not running yet because it only takes effect at a later deploy (done by"
    " hand, or another repository's deploy applies it, as a chart or infra repository's change"
    " does), do not wait for that next deploy, and do not just mention it as a step someone"
    " could take: redeploy it now through the route the repository guide gives (#4320)."
    " If that deploy is done from the boss's Mac, switch the Mac tool on (free, no tap) and"
    " ask for that repository's production deploy script by name, naming the merged pull"
    " request in the explanation. One that needs approval answers `pending`, so post its"
    " approval link: its tap is the person's yes (#4285). A script marked safe starts at once,"
    " with no tap, so before you request it, ask the person to allow that one deploy once"
    " with your question tool: every production deploy is asked first (#4320). If the route"
    " is one your own tools refuse for production (a Drone promote, which the `drone` helper"
    " refuses), a yes does not make the tool do it: use the Mac deploy script if there is"
    " one, or ask the person to make that promote themselves, naming the repository, build"
    " and target, and follow its outcome. Before you"
    " redeploy, check that it will not cut a running job (a cron job mid-run, an import):"
    " wait for it to finish first. Wait for the outcome, then check the change live"
    " as below. One that waits on a deploy by hand is `not_live` until it runs;"
    " `nothing_to_check` is only for a change with no live behaviour at all."
)

#: Said in place of ``deploy_sentence`` when the deck registered the deploy waits itself
#: (#4253): the live-check request then rides on the wait, and reaches the chat once it lands.
DEPLOY_WAITED_TEXT = (
    "The deck registered the wait for its deploy when it merged, and this message is that"
    " wait's answer: it is live now."
)
#: The one sentence the deck appends to a ready Work's summary (#4253), in Finnish as the rest
#: of the summary is, in place of asking the agent to rewrite it (``READY_TEXT``).
EVIDENCE_TEXT = (
    "Pakka tarkisti ennen yhdistämistä: katselmointi hyväksyi muutoksen ja testit ovat"
    " vihreät samassa commitissa (tai katselmointi yksin todistaa sen): {refs}."
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
    #: When the oldest check still pending started, as epoch seconds; ``None`` when none is
    #: pending or GitHub gave no time (#4328).
    pending_since: float | None = None
    #: Commits the base has that the head lacks (the Poller's ``behind_by``, #2101); ``None``
    #: when the comparison could not be read, which is never "up to date".
    behind_by: int | None = 0
    base: str = ""  # the branch it merges into, as GitHub names it
    #: The commit its merge made on the base, lowercased; "" until it merged or when unread.
    merge_commit: str = ""

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
    #: The production deploy's name in each of ``deploys`` that gives one (``[[repos]]
    #: deploy_name``, #4445), by lowercased ``owner/repo``: the deck's deploy wait on a merge
    #: there is named by it, so it reads commit statuses and counts a later deploy containing
    #: the merge.
    deploy_names: tuple[tuple[str, str], ...] = ()
    #: The manifest's ``[staging]`` (#3180): its branch copies (``StagingSite``), and the
    #: repositories, lowercased ``owner/repo``, that deploy into them.
    staging: object | None = None
    staging_repos: frozenset[str] = frozenset()
    #: The repositories that merge on their own (``[[repos]] auto_release = true``, #4172),
    #: lowercased ``owner/repo``, in a project whose own ``auto_release`` is off: a pass there
    #: merges with nobody pressing Merge, while the project's other repositories still wait.
    auto_release_repos: frozenset[str] = frozenset()
    #: The repositories whose pre-review-done also checks the tests (``[[repos]] check_tests
    #: = true``, #4326), lowercased ``owner/repo``: read by the router, not the release.
    tests_checked: frozenset[str] = frozenset()
    #: The repositories with lighter review outside their guarded code (#4399): ``(lowercased
    #: owner/repo, guarded list's path, review point limit, checkout on this host or "")``.
    light_review: tuple[tuple[str, str, int, str], ...] = ()

    def light(self, repository: str) -> tuple[str, int, str] | None:
        """``(guarded path, limit, checkout)`` where lighter review is on, else ``None``."""
        wanted = repository.lower()
        for repo, path, limit, checkout in self.light_review:
            if repo == wanted:
                return path, limit, checkout
        return None

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


NEEDS_PASS = "needs_pass"
#: The words of the release's hold on a pull request no review passed at its head.
NO_PASS = " has no review pass at "
#: Whether the tests of the head ``NO_PASS`` stopped at were green, from the look that stopped
#: there; ``_note_hold`` keeps it on that hold as ``green`` (#4579).
UNREVIEWED_GREEN = "unreviewed_green"
#: The open pull requests whose review waits on their agent's ``pre-review-done`` this look
#: (#4589); ``_note_hold`` words a ``NO_PASS`` hold at one of them so.
PRE_REVIEW_OWED = "pre_review_owed"
#: Set by a look that got past the holds that keep the chat quiet (a pause, a holding label,
#: a Ship now press, its agent's turn): only then is it told it owes ``pre-review-done``.
#: Read and dropped by ``_evaluate`` in the same look.
PRE_REVIEW_SAYABLE = "pre_review_sayable"
#: What a pull request lacks while its review waits on its agent's checklist (#4589).
PRE_REVIEW_WORDS = "its review waits on its agent's pre-review-done"
#: The one message to a Work's agent whose turn ended with a pull request's review waiting on
#: its ``pre-review-done`` (#4589). One pull request's checklist was printed in a ``gh pr
#: ready`` reply among others, its agent waited on a deploy, and its sibling, reviewed and
#: green, sat unmerged with nothing telling the chat why. Once per head.
PRE_REVIEW_HOLD_TEXT = (
    "AgentDeck: the review of {ref} at {head} has not started: it waits on you. The deck sent"
    " its pre-review checklist when you marked it ready, and no review runs until you go"
    " through it and call `POST $AGENTDECK_URL/api/sessions/<session-key>/pre-review-done` with"
    ' `{{"pull_request": "{ref}"}}`. That is the call after a fix push, not `review-request`,'
    " which asks again at a commit already reviewed.{siblings}"
)
#: Said beside a hold that one pull request's review or tests decide for the others (#4589):
#: the deck merges a Work's open pull requests together, so a reviewed and green one waits.
SIBLINGS_TEXT = (
    " This Work's pull requests merge together, so {names} {verb} for it too, though {pron}"
    " reviewed."
)
#: Beside the call to verify, when another open pull request of the Work has no pass yet.
VERIFY_SIBLINGS_TEXT = (
    " This Work's pull requests merge together, so it also waits for the review of {names}."
)


def _needs_pass(entry: dict, ref: str) -> None:
    """Mark ``ref`` as needing a new review or bench pass at its head this look; the merge
    queue then lets it go whatever its checks say (``_queue_hold``)."""
    needs = entry.setdefault(NEEDS_PASS, [])
    if ref not in needs:
        needs.append(ref)


def light_hold_words(verdict: str, cases: Iterable[str]) -> str:
    """What holds a light pull request on its bench verdict (#4399)."""
    if verdict == "pending":
        named = ", ".join(cases)
        return f"waits for its bench verdict (pending: {named or 'master'})"
    if verdict == "unrun":
        return "has a bench verdict that could not run"
    if verdict == "worse":
        return "has a bench verdict of worse; its agent calls pre-review-done for a review"
    if verdict == "running":
        return "waits for its bench verdict (running)"
    if verdict == "limit":
        return (
            "no longer merges light (the review point is reached); its agent calls pre-review-done"
        )
    return "waits for its bench verdict"


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


def pending_since(rollup: object) -> float | None:
    """When the oldest still-pending check in a ``statusCheckRollup`` started, as epoch
    seconds; ``None`` when none is pending or none says when (#4328).

    A commit status gives ``startedAt`` (its creation), a check run ``startedAt``; the deck's
    own review check is left out, as in ``checks_state``.
    """
    if not isinstance(rollup, list):
        return None
    found: list[float] = []
    for item in rollup:
        if not isinstance(item, dict) or review_history.deck_check(item.get("name")):
            continue
        value = str(item.get("conclusion") or item.get("state") or "").upper()
        if value in _GREEN or value in _RED:
            continue
        stamp = item.get("startedAt") or item.get("createdAt")
        if not isinstance(stamp, str) or not stamp.strip():
            continue
        try:
            when = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None or when.year < 2000:
            continue  # GitHub's zero time for a run not started yet says nothing
        found.append(when.timestamp())
    return min(found, default=None)


def verifies(rows: Iterable[object], work_id: str, command: list[str] | None = None) -> bool:
    """Is a verification job of this Work queued or running among the queue's ``rows``?

    The queue's per-Work read answers every unfinished job (``jobs.rows_for_work``), so only
    the ones naming this Work, compared case-folded, count. With ``command``, only a job of
    exactly that command (#4401 review).
    """
    from .jobs import QUEUED, RUNNING, VERIFICATION

    wanted = work_id.strip().casefold()
    return bool(wanted) and any(
        isinstance(row, dict)
        and str(row.get("work_id") or "").strip().casefold() == wanted
        and row.get("purpose") == VERIFICATION
        and row.get("state") in {QUEUED, RUNNING}
        and (command is None or [str(part) for part in row.get("command") or []] == command)
        for row in rows
    )


#: How ``on_verdict`` answers a pass it recorded, and findings about a commit the branch left.
PASS_RECORDED = "review pass recorded for"
#: Why a Ship now pass holds the merge (#4212 review).
SHIP_UNFINISHED = "is not finished; press Ship now again"
#: While the press itself still runs (#4558): pressing again only joins it.
SHIP_UNDER_WAY = "is under way; the Work's agent hears when it ends"
FINDINGS_DROPPED = "findings dropped"
#: What the agent does once the owner settled every finding at a head with no code change
#: (#3828): the review counts as a pass there, with no new review. Asking for a fresh review
#: instead held a head already tested green for a second review round the owner never wanted.
ACCEPTED_ASK = (
    "When their answer settles every finding without a code change, tell the deck so: `POST"
    " $AGENTDECK_URL/api/sessions/<session-key>/review-accepted` with"
    ' `{"pull_request": "<owner/repo#number>", "note": "<their decision>"}`. That review then'
    " counts as a pass at the same commit and no new review runs. A fix you push needs no"
    " request: once its pull request is ready, go through the pre-review checklist the deck"
    " then sends for that commit and call `pre-review-done`, which starts its review."
    " Ask for a fresh review of the same commit (`review-request`) only when you changed"
    " something the reviewer should read again without a push, such as the pull request body."
)
#: The owner's Ship now offered in the agent's own question (#4212): the owner chose it as
#: one more choice beside the reviewer's options, with the Work page's button for findings
#: the agent never asks about. It ships the commit with its findings left to this Work.
SHIP_ASK = (
    'Add one more choice to that question: "Ship now, fix later", which merges this commit as'
    " it is once its tests are green and leaves the findings to a new pull request of this"
    " same Work. When the owner picks it, post their decision on the issue and `POST"
    " $AGENTDECK_URL/api/sessions/<session-key>/ship-now` with"
    ' `{"pull_requests": [{"pull_request": "<owner/repo#number>", "head": "<full commit id>"}]}`,'
    " one entry for each of this Work's pull requests whose findings the question showed, at"
    " the commit those findings were on, as the Work page's Ship now button ships them all."
    " A commit you pushed a fix on top of since ships in a pull request the deck opens for"
    " it; the reply names it. The reply's `state` is `recorded` (it merges once its tests are"
    " green) or `running` (this chat hears when it ends), and its `next` says what to do; a"
    " repeat with the same body is safe and never ships twice. Then fix the findings in a new"
    " pull request once it merges."
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
DESIGN_FINDING_ASK = (
    f'A finding whose line starts "{DESIGN_FINDING_MARK}" is a problem that came back after'
    " an earlier fix. Do not patch the new case, and do not apply any standing rule to make the"
    " narrow fix (such as doing the narrow fix you recommend): write a redesign. It is one rule"
    " or change that answers the question the finding names and replaces the patches it lists,"
    " and it lists every case the earlier rounds found. Post it on the issue as a comment"
    " opening with `**Design:**`, call `POST $AGENTDECK_URL/api/sessions/<session-key>/"
    "design-review`, and in the same turn ask the owner in your question box: build this"
    " redesign, rethink it together, or pause the Work. Change no code for that problem until"
    " they answer (fix the message's other findings as usual), and post their answer on the"
    " issue. If the design review finds a gap, fix the design, and ask"
    " the owner again only if what they would approve changed."
)
#: Under every reviewer's findings the Work's agent hears (#3314): a finding only the owner can
#: settle, sent as one to fix, held a Work in review for two days until it repeated.
OWNER_CALL_ASK = (
    DESIGN_FINDING_ASK
    + " A finding is the owner's call only when "
    + OWNER_CALL_TEST
    + ". For such a finding, do not work around it: ask the owner in your question box"
    " now, with the options. Once they answer, post their decision on the issue. "
    + ACCEPTED_ASK
    + " "
    + SHIP_ASK
)

#: The verdict a reviewer sends when none of its findings needs a change to code or tests
#: (#3322): a walkthrough, the pull request body, staging evidence or doc files. It holds the
#: merge as ``changes-needed`` does, and fixing it needs no new review.
NO_CODE = "no-code"
#: The verdict a reviewer sends when every finding is minor (#4474): a nitpick, wording, a
#: comment or docstring, a doc file, or a small code fix the finding spells out. It holds the
#: merge as ``no-code`` does, and is kept as a ``no_code`` row marked ``minor``: its fix needs
#: no new review either, when the push stays as small as the router checks.
MINOR = "minor"
#: What counts as a doc file for a ``no-code`` fix (#3322): by name alone. Not ``.txt``, which
#: is also ``requirements.txt`` and ``tests/too_slow.txt``; a comment or docstring in a code
#: file is code.
DOC_SUFFIXES = (".md", ".mdx", ".mdc", ".rst")
DOC_DIRS = ("docs/",)
DOC_FILES_TEXT = "`*.md`, `*.mdx`, `*.mdc`, `*.rst` or anything under `docs/`"
#: The most lines a fix push after a ``minor`` verdict may add plus remove outside doc and test
#: files and still need no new review (#4474; assumed, the owner may move it).
MINOR_LINES = 100
#: The folders whose test files a ``minor`` fix may change at any size (#4474): a module named
#: ``test_*`` elsewhere, such as ``src/agentdeck/test_timings.py``, is code. ``testing`` and
#: ``specs`` since #4575: a Django project keeps its tests in ``<app>/testing/``, and a new
#: ``testing/test_*.py`` in a minor fix cost a pull request nine hours.
MINOR_TEST_DIRS = frozenset({"tests", "test", "testing", "__tests__", "spec", "specs"})
#: A test file by its name alone, wherever it sits (#4604): a Nuxt project keeps
#: ``frontend/admin/a.test.ts`` beside its code. Not ``.spec.``: ``openapi.spec.ts`` is code.
MINOR_TEST_NAME = re.compile(r"[^/]+\.test\.[cm]?[jt]sx?", re.IGNORECASE)
#: How the reviewers and the agent are told which test files count (#4575): the same folders
#: and names.
TEST_FILES_TEXT = (
    "test files (one inside a "
    + ", ".join(f"`{name}/`" for name in sorted(MINOR_TEST_DIRS))
    + " folder, or a JavaScript or TypeScript file named like `a.test.ts` anywhere)"
)
#: When every finding is minor (#4474), one test for every reviewer: the chat reviewer's
#: ``chat_review.NO_CODE_NOTE`` and the deck's own reviewer's brief (``agent_review``).
MINOR_TEST = (
    "a nitpick, wording, a comment or docstring, a doc file (" + DOC_FILES_TEXT + "), the pull"
    " request body, a use-case walkthrough, staging evidence, test-only, or a small code fix"
    " whose exact change the finding spells out (which line, what to change) and that raises"
    " no design question. It is never minor when it could lose data, is about security,"
    " reaches a person (sends, posts, emails), changes a database migration, locking or"
    " concurrency, or is the owner's call"
)
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
#: Why a ``changes`` or ``no-code`` verdict about a settled round moves nothing (#4386).
SETTLED_ROUND = (
    "that review round is settled (the owner accepted it, or a newer review was asked since),"
    " so this verdict moves nothing"
)
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

#: The one message a ``minor`` verdict sends the Work's chat (#4474).
MINOR_TEXT = (
    "AgentDeck: the review of {pull} at {head} ({reviewer}) asks only for minor fixes, so it"
    " will not merge yet. Fix them in one push that changes only doc files, "
    + TEST_FILES_TEXT
    + " and files this pull request already changes, with at most {lines} lines of other code"
    " added plus removed. No new review runs then: that review counts as a pass, and the merge"
    " still waits for the tests wherever it does. A bigger push, or one that adds a new code"
    " file, is reviewed again as usual: the deck then tells you why, and that head's review"
    " starts only once you call `pre-review-done`. If you pushed nothing, turn the pull request"
    ' to draft and mark it ready again. A finding marked "'
    + EDGE_CASE_MARK
    + '" is a rare edge case (#3574): fix it, or accept it as a known limit under'
    ' "Known limits" in the pull request body. '
    + OUTSIDE_DEFAULTS_ASK
)


#: The data key of a pass a doc-only push kept from a standing pass (#4257): that pass's head.
DOC_ONLY = "doc-only"


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


#: How ``on_verdict`` answers a ``minor`` or ``no-code`` verdict it took after the fix push
#: (#4474): the router then checks that push against the verdict, as a ready press does.
LATE_TAKEN = "late verdict taken"

#: What the Work's chat hears of such a verdict (#4474): its fix is already pushed.
LATE_TEXT = (
    "AgentDeck: the review of {pull} at {head} ({reviewer}) asks only for {what}, and its"
    " verdict came after you pushed {live}. The deck checks that push against it now: if it"
    " stays within the limit, no new review runs and that review counts as a pass. Otherwise"
    " the review of {live} goes on as usual."
)


def verdict_settled(answer: str) -> bool:
    """Whether ``on_verdict``'s answer leaves nothing for the comment itself to tell the chat
    (#3192): a pass the release already told it about, one it told it is held or refused
    (#4124), or findings about a commit it left."""
    return answer.startswith((PASS_RECORDED, LATE_TAKEN)) or answer.endswith(
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
    #: (chat id, note id, text): keep a note for the chat's next message without waking it
    #: (#4303). Whether it is held; ``None`` stores nothing.
    note: Callable[[str, str, str], bool] | None = None
    #: Is one of the Work's chats mid-turn? Then its agent may still push: hold the merge.
    busy: Callable[[str], bool] = lambda work_id: False
    #: Did a person star the Work (#3116)? Its pull requests join behind the starred ones.
    starred: Callable[[str], bool] = lambda work_id: False
    #: ``pulls`` for every Work from one walk taken now (#3993), for a look at many Works;
    #: ``None`` asks ``pulls`` once per Work.
    pulls_lookup: Callable[[], Callable[[str], Iterable[str]]] | None = None
    #: The Work's current round (``work_rounds``, #4585), ``None`` for none: an entry's keys
    #: about an earlier round's merge and live check are dropped once a newer one starts.
    round_of: Callable[[str], object] | None = None
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
    #: pass then holds the merge until a press finishes it: its findings' Work recorded, the head
    #: marked shipped and, where the Work waits for one, its Merge press recorded.
    ship_unfinished: Callable[[str, str], bool] = lambda pull, head: False
    #: The head a Ship now press of ``pull`` is running at now, or "" (#4558,
    #: ``work_router.ShipRuns.head_of``). The hold line then says so before any other hold.
    ship_running_head: Callable[[str], str] = lambda pull: ""
    #: What this Work (``work_id``) shipped with Ship now, its findings left to fix in it
    #: (#4212 review): ``(pull, fix)`` per shipped head, ``fix`` the pull request named as
    #: fixing them or "". Every live check keeps the issue open until each ``fix`` merged.
    shipped_findings: Callable[[str], list[tuple[str, str]]] = lambda work_id: []
    #: The files of ``pull``'s diff now, or ``None`` when unread (#3322): kept with a
    #: ``no-code`` verdict, so a fix that reverts one of them is not taken for a base merge.
    pull_files: Callable[[str], Awaitable[set[str] | None]] | None = None
    #: The deck's processes that load these merges (``(merge commit, files)`` each), or
    #: ``None`` when unread (#4500, ``live_check_scope.loading_processes``); ``None`` here
    #: tells the agent to confirm every process restarted.
    loading_processes: (
        Callable[[list[tuple[str, set[str]]]], Awaitable[frozenset[str] | None]] | None
    ) = None
    #: For a ``minor`` or ``no-code`` verdict naming a head its fix push already left (#4474):
    #: ``(pull, old, live)`` -> whether ``live`` descends from ``old``, and the pull request's
    #: files at ``old`` (``None`` when unread: then only doc files pass its fix).
    late_read: Callable[[str, str, str], Awaitable[tuple[bool, set[str] | None]]] | None = None
    #: ``(pull, old, new)``: the checks state (``checks_state``) of each of the pull request's
    #: own commits a push to ``new`` replaced, ``old`` included, by sha (#4574). ``None`` when
    #: unread, or when ``new`` does not reach ``old`` along first parents within the walk.
    replaced_checks: Callable[[str, str, str], Awaitable[dict[str, str] | None]] | None = None
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
    #: Register, for the Work's chat, one wake-on wait per body (``wake_on.parse_request``'s
    #: keywords: ``deck_deploy`` or ``deployment``) carrying ``then`` for when it ends
    #: (#4253); whether every one was stored. ``None``: the chat is told to register them.
    #: Its waits on these merged pull requests end quietly: this wait answers them.
    wait_deploy: Callable[[str, list[str], list[dict], str], bool] | None = None
    #: Append one sentence of evidence to the Work's summary's ``correctness`` part (#4253);
    #: whether it was written. ``None``: the chat is asked to rewrite it (``READY_TEXT``).
    note_evidence: Callable[[str, str], bool] | None = None
    #: Run this Work's last verification of a repository again at a head (#4253); whether a
    #: run was queued. ``None`` or ``False``: the chat is told to run it.
    #: Called as ``(work_id, repository, head)``, and with ``command=["bin/test.sh"]`` for a
    #: light pull request (#4399).
    rerun_verification: Callable[..., Awaitable[bool]] | None = None
    #: Lighter review (#4399, ``light_review.LightReview``): ``None`` where it is off.
    light: object | None = None
    #: The heads a green verification of exactly ``bin/test.sh`` proves (#4399).
    script_green_heads: Callable[[str], Awaitable[set[str]]] | None = None
    #: The heads a verification of exactly ``bin/test.sh`` failed at, and whether one runs
    #: (#4401 review): only these hold off the light rerun and its tell, never another run.
    script_failed_heads: Callable[[str], Awaitable[set[str]]] | None = None
    script_verifying: Callable[[str], Awaitable[bool]] | None = None
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
        #: ``(work_id, pull)``: check the fix pushed before a late ``minor`` or ``no-code``
        #: verdict came against it (#4474); the router's ``late_fixed``, set once it is made.
        self.on_late: Callable[[str, str], Awaitable[object]] | None = None
        self._task: asyncio.Task | None = None
        self._sweep: asyncio.Task | None = None
        #: Works that became first in a merge queue, evaluated once the lock that moved them up
        #: is let go (#3022).
        self._wakes: set[str] = set()
        #: The one timed look per Work, for an update that has not landed (#3033 review).
        self._timers: dict[str, object] = {}
        self._looks: set[asyncio.Task] = set()
        #: The bench's queued look per Work, removed once it starts (#4399, rev 11).
        self._bench_look_queued: dict[str, asyncio.Task] = {}
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
            entry = self._round_entry(work_id)
            if self._holds_pass(work_id) or self._has_pass(work_id, pulls):
                self._mark_retry(work_id, entry, "the look raised")
                self.store.put(work_id, entry)

    async def look_after_ship(self, work_id: str) -> str:
        """The one look a Ship now press makes once it left the running list (#4558
        redesign), on success, refusal or crash alike. One that raises leaves the Work due for
        a timed look, with a pass or without one: nothing else may come by, as every look
        during the press was spent on "under way"."""
        try:
            return await self.evaluate(work_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - looked at again after the back-off
            log.warning(
                "work-release: the look after Ship now of %s failed", work_id, exc_info=True
            )
            async with self._lock(work_id):
                entry = self._round_entry(work_id)
                self._mark_retry(work_id, entry, "the look after a Ship now press raised")
                self.store.put(work_id, entry)
            return ""

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
            entry = self._round_entry(work_id)
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
            entry = self._round_entry(work_id)
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
        stamp: str = "",
    ) -> str:
        """Take a reviewer's verdict; answer what became of it, for a log or a receipt.

        ``by`` is the GitHub login that posted it; ``None`` is the deck's own reviewer through
        ``/api/deck-request``, which the deck trusts as the Poller trusts its request file. A
        login must hold the tier the Poller asks of a ``/reviewed`` here (``_verdict_tier``).
        ``source`` is the comment's or the request's own id, which keys its history event, and
        ``at`` the comment's own time, which dates it (the deck's clock when ``None``).
        ``stamp`` is the round id the comment was stamped with (#4386), "" when none.
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
        if verdict in ("changes-needed", NO_CODE, MINOR):
            standing = await self._standing_head(pull, head)
            if standing is None and verdict in (NO_CODE, MINOR):
                late = await self._late_hold(pull, head, digest, stamp)
                if late is not None:
                    answer = await self._take_late(
                        work_id, pull, verdict, late, by=by, source=source, at=at, stamp=stamp
                    )
                    if answer.startswith(LATE_TAKEN) and self.on_late is not None:
                        # Outside the lock: the router's check takes it afresh (#4474).
                        try:
                            await self.on_late(work_id, pull)
                        except Exception:  # noqa: BLE001 - unchecked: the live head is reviewed
                            log.warning(
                                "work-release: %s's late fix unchecked", pull, exc_info=True
                            )
                    return answer
            if standing is None:
                if verdict == "changes-needed":
                    # A refusal of a CI fix the agent has pushed past still drops a press held
                    # for that fix (#4574): the newer head would otherwise take its place.
                    await self._refuse_left_fix(work_id, pull, head)
                # Findings about a commit the branch has left are dropped (#3192): the chat
                # that asked for them was replaced, and the current head has its own review.
                return f"{pull} moved past {head[:12]}; {FINDINGS_DROPPED}"
            # A no-code verdict that does not match the diff now holds as changes-needed: its
            # fix is reviewed again (#3322).
            held = (
                await self._no_code_hold(pull, head, digest)
                if verdict in (NO_CODE, MINOR)
                else None
            )
            merged = bool(standing) and await self._merged(pull)
            async with self._lock(work_id):
                replaced = self._replaced(pull, head, stamp)
                if replaced:
                    return replaced
                entry = self._round_entry(work_id)
                # A late comment older than the newest verdict is not the newer word, since
                # history dates each by its own time: only its row is kept.
                late = self._outdated(entry, pull, head, at)
                at_head = standing or self._full_head(pull, head)
                # A verdict answers the newest round at the head it names (#4386), and moves
                # nothing once that round is settled: the reviewer's own line on findings the
                # owner already accepted is kept as a row at its head, where it counts for
                # nothing as a late escalation copy does not (#4069); an old round's answer
                # after a newer review was asked at the head its change was carried to is not
                # written, since at its own head it would read as the newest verdict. A review
                # asked again at the same head is a new chat only once no reviewer holds the
                # old one (``chat_review.ReviewBusy``), so the old round's line was posted before
                # that ask and ``late`` (its comment's own time) already keeps it from moving
                # anything; a line posted after it is the new chat's.
                reviewed = self._full_head(pull, head)
                settled = (
                    ""
                    if late
                    else review_history.settled_round(self._history(pull), reviewed, standing or "")
                )
                if settled:
                    if settled == review_history.ACCEPTED:
                        kind = review_history.NO_CODE if held else review_history.CHANGES
                        data = {"by": by or "", "chat": self.review_chat(pull, head) or ""}
                        if held and verdict == MINOR:
                            data["minor"] = True
                        if not held and verdict in (NO_CODE, MINOR):
                            data[mechanism_check.ASKED] = verdict  # still a minor round (#4596)
                        key = f"{source}:{pull}" if source else f"settled:{pull}@{reviewed}:{at}"
                        self._note(pull, reviewed, kind, key, data, at=at)
                    return f"{pull} at {head[:12]}: {SETTLED_ROUND}"
                if not late:
                    if not (pull in _held_for_fix(entry) and verdict in (NO_CODE, MINOR)):
                        # A held press keeps through a minor fix, whose pass is carried
                        # (#4574); any other refusal drops a press, visibly where it was not for
                        # this head.
                        self._refuse_press(work_id, entry, pull, reviewed)
                        entry.pop("asked", None)
                    self._drop_held(entry, pull)  # not read again
                    # Out of line with all its pull requests, not only this one: the Work
                    # merges nothing without this pass, so another of them first in line would
                    # hold the line until the Work's next event (#3033).
                    self._leave(work_id)
                # Read by the Work's line until a push or a pass answers them (#3097).
                told = bool(self.ports.chat(work_id)) and not late
                word = (MINOR if verdict == MINOR else NO_CODE) if held else "changes-needed"
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
                        f" findings in a follow-up pull request. {SIBLING_SWEEP_ASK}"
                    )
                elif held and verdict == MINOR:
                    text = MINOR_TEXT.format(
                        pull=pull, head=head[:12], reviewer=reviewer, lines=MINOR_LINES
                    )
                elif held:
                    text = NO_CODE_TEXT.format(pull=pull, head=head[:12], reviewer=reviewer)
                else:
                    text = (
                        f"AgentDeck: the review of {pull} at {head[:12]} ({reviewer}) asks for"
                        f" changes, so it will not merge. Fix the findings. {SIBLING_SWEEP_ASK}"
                        " Then push and mark it ready again."
                    )
                    if verdict == "changes-needed" and told:
                        # The reviewer's findings came first, before it said this round is not
                        # minor: its mechanism check goes with this message (#4596).
                        mechanism = self._verdict_mechanism(pull, at_head, reviewed, message_key)
                        text = f"{text}\n\n{mechanism}" if mechanism else text
                if not late:
                    self._tell(work_id, entry, message_key, text)
                self.store.put(work_id, entry)
                kind = review_history.NO_CODE if held else review_history.CHANGES
                data = {"by": by or "", "chat": self.review_chat(pull, head) or ""}
                data["told"] = told
                if held and "files" in held:
                    data["files"] = held["files"]
                if held and verdict == MINOR:
                    data["minor"] = True
                if not held and verdict in (NO_CODE, MINOR):
                    data[mechanism_check.ASKED] = verdict  # still a minor round (#4596)
                key = f"{source}:{pull}" if source else message_key
                if not self._note(pull, at_head, kind, key, data, head, at=at, hold=entry):
                    self.store.put(work_id, entry)
                # Ended only for a verdict taken and told (#3183 review): a refused one leaves
                # the wait to its timeout, so the agent still looks.
                if not late:
                    self._review_answered(work_id, entry, message_key, [pull])
            if late:
                return f"{pull}'s verdict is older than its newest; {FINDINGS_DROPPED}"
            await self._wake_next(work_id)  # the next in line, once this lock is let go
            if verdict in (NO_CODE, MINOR) and not held:
                return (
                    f"changes needed on {pull}: its {verdict} verdict does not match the diff"
                    f" at {head[:12]}, so a fix is reviewed again"
                )
            return f"{verdict} fixes needed on {pull}" if held else f"changes needed on {pull}"
        return await self._take_pass(
            work_id, pull, head, digest, by=by, source=source, at=at, stamp=stamp
        )

    def _late_ok(self, pull: str, old: str, stamp: str) -> bool:
        """Whether a ``minor`` or ``no-code`` verdict about ``old``, a head the branch left,
        still answers ``pull``'s newest round (#4474): findings stand at ``old``, the stamped
        chat (when stamped) reviewed ``old``, and no head after them has an answer of its own.
        A review asked or a chat opened since is allowed: the fix push started it."""
        history = self._history(pull)
        findings = [
            event
            for event in review_history.at_head(history, old)
            if event.kind == review_history.FINDINGS
        ]
        if not findings:
            return False
        since = max((event.at, event.seq) for event in findings)
        stamp = (stamp or "").lower()
        if stamp:
            own = [
                event
                for event in history
                if event.kind == review_history.CHAT
                and str(event.data.get("round") or "").lower() == stamp
            ]
            if not own or not all(same_commit(event.head, old) for event in own):
                return False
        answers = {
            review_history.PASS,
            review_history.CHANGES,
            review_history.NO_CODE,
            review_history.FINDINGS,
            review_history.ESCALATED,
        }
        return not any(
            event.kind in answers
            and not same_commit(event.head, old)
            and (event.at, event.seq) > since
            for event in history
        )

    async def _late_hold(self, pull: str, head: str, digest: str, stamp: str) -> dict | None:
        """What a ``minor`` or ``no-code`` verdict about ``head`` keeps when the agent pushed
        its fix before the verdict came (#4474), or ``None``: then it is dropped, as any word
        about a head the branch left is. ``_late_ok`` holds, the digest is the one the deck
        gave the reviewer at that head, and the live head descends from it."""
        if self.ports.late_read is None or len(head or "") < 7:
            return None
        old = self._full_head(pull, head)
        if not self._late_ok(pull, old, stamp):
            return None
        given = {
            str(event.data.get("digest") or "").lower()
            for event in review_history.at_head(self._history(pull), old)
            if event.kind == review_history.ASKED
        }
        if not digest or digest.lower() not in given:
            return None
        facts = await self.ports.read_pull(pull)
        if facts is None or facts.state != "OPEN" or not facts.head:
            return None
        live = facts.head.lower()
        if same_commit(live, old):
            return None
        try:
            ahead, files = await self.ports.late_read(pull, old, live)
        except Exception:  # noqa: BLE001 - unread is dropped, and the live head is reviewed
            log.warning("work-release: %s's late verdict unread", pull, exc_info=True)
            return None
        if not ahead:
            return None
        held: dict = {"head": old, "live": live}
        if files is not None:
            held["files"] = sorted(files)
        return held

    async def _take_late(
        self,
        work_id: str,
        pull: str,
        verdict: str,
        late: dict,
        *,
        by: str | None,
        source: str,
        at: float | None,
        stamp: str,
    ) -> str:
        """Keep a late ``minor`` or ``no-code`` verdict at the head it names (#4474) and tell
        the agent its pushed fix is checked against it. The live head's review, its ask and
        the Work's place in line are left alone: the router's check of the fix ends them, or
        that review goes on. The same round rules as ``on_verdict``, read under the lock."""
        old, live = late["head"], late["live"]
        async with self._lock(work_id):
            history = self._history(pull)
            replaced = review_history.replaced_round(history, stamp, late_from=old)
            if replaced:
                return (
                    f"{pull} at {old[:12]}: from review chat {replaced}, which a newer review"
                    f" replaced; {FINDINGS_DROPPED}"
                )
            if not self._late_ok(pull, old, stamp):
                return f"{pull} moved past {old[:12]}; {FINDINGS_DROPPED}"
            entry = self._round_entry(work_id)
            if self._outdated(entry, pull, old, at):
                return f"{pull}'s verdict is older than its newest; {FINDINGS_DROPPED}"
            reviewer = self._reviewer(pull, old, by)
            data: dict = {"by": by or "", "chat": self.review_chat(pull, old) or ""}
            data["late"] = live
            if "files" in late:
                data["files"] = late["files"]
            if verdict == MINOR:
                data["minor"] = True
            word = MINOR if verdict == MINOR else NO_CODE
            message_key = _round_key(
                entry, f"verdict:{word}:{pull}@{old[:12]}", self.review_chat(pull, old)
            )
            key = f"{source}:{pull}" if source else f"late:{pull}@{old}:{at}"
            settled = review_history.settled_round(history, old, "")
            if settled == review_history.ACCEPTED:
                self._note(pull, old, review_history.NO_CODE, key, data, at=at)
                return f"{pull} at {old[:12]}: {SETTLED_ROUND}"
            data["told"] = bool(self.ports.chat(work_id))
            if not self._note(pull, old, review_history.NO_CODE, key, data, at=at):
                return f"{pull}'s verdict at {old[:12]} {UNRECORDED}"
            what = (
                f"minor fixes (at most {MINOR_LINES} lines of code)"
                if verdict == MINOR
                else "fixes that need no code"
            )
            text = LATE_TEXT.format(
                pull=pull, head=old[:12], reviewer=reviewer, what=what, live=live[:12]
            )
            self._tell(work_id, entry, message_key, text)
            self.store.put(work_id, entry)
        return f"{LATE_TAKEN} on {pull} at {old[:12]}; its fix at {live[:12]} is checked"

    def _replaced(self, pull: str, head: str, stamp: str) -> str:
        """Why a verdict stamped ``stamp`` answers nothing, when its review chat was replaced
        by a newer one (#4386), else "". One rule for every verdict, a pass as findings, read
        under the Work's lock: an old round's answer, whatever its time or head, writes no
        row, so the new round stays unanswered and its reviewer still starts, and ends with
        ``FINDINGS_DROPPED``, so its comment is not handed on either."""
        replaced = review_history.replaced_round(self._history(pull), stamp)
        if not replaced:
            return ""
        return (
            f"{pull} at {head[:12]}: from review chat {replaced}, which a newer review"
            f" replaced; {FINDINGS_DROPPED}"
        )

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
        stamp: str = "",
    ) -> str:
        """Record a pass at ``pull``'s current head and diff; answer what became of it.

        A read that failed says nothing about the pass, so it is held and read again (#4124).
        Anything that does say no is told to the Work's chat, which otherwise reads the pass
        comment and waits for a merge that will not come.
        """
        args = (work_id, pull, head, digest, by, source, at, stamp)
        async with self._lock(work_id):
            # A pass delivered late neither replaces a newer one held here nor is announced
            # over newer findings, which history keeps first whatever becomes of it.
            entry = self._round_entry(work_id)
            replaced = self._replaced(pull, head, stamp)
            if replaced:
                # A pass held for a read goes only when it is this same replaced chat's: the
                # newer chat's own held pass is its round's answer, read again later (#4386).
                held = (entry.get(HELD_PASSES) or {}).get(pull) or {}
                if str(held.get("stamp") or "").lower() == stamp.lower() and self._drop_held(
                    entry, pull
                ):
                    self.store.put(work_id, entry)
                return replaced
            if self._outdated(entry, pull, head, at):
                # A hold no newer than this pass was overtaken too: a read would only find so.
                if not _older_than_held(entry, pull, at) and self._drop_held(entry, pull):
                    self.store.put(work_id, entry)
                return f"{pull}'s pass is older than its newest verdict; {FINDINGS_DROPPED}"
            facts = await self.ports.read_pull(pull)
            if facts is None:
                return self._hold_pass(*args)
            if facts.state == "MERGED":  # needs no pass any more
                entry = self._round_entry(work_id)
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
            entry = self._round_entry(work_id)
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
        stamp: str = "",
    ) -> str:
        """Keep a pass nothing could be read about, to read again after the back-off (#4124).

        Under the Work's lock. Kept in the entry, so a restart loses only the timer, and the
        start's look sets it again. A day of failed reads ends in a refusal, told to the chat.
        """
        now = self.ports.clock()
        entry = self._round_entry(work_id)
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
            "stamp": stamp,  # read again with the pass: its chat may be replaced by then
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
        entry = self._round_entry(work_id)
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
                stamp=str(held.get("stamp") or ""),
            )
            log.info("work-release: held pass of %s: %s", pull, answer)

    async def _announce_pass(self, work_id: str, pull: str, head: str, lead: str) -> str:
        """Evaluate a pass just recorded at ``head`` and tell the Work's chat, once per head."""
        # Its own message carries the call to verify, so the agent hears one, not two (#2999).
        answer = await self.evaluate(work_id, tell_untested=False)
        # The Work's chat hears the pass once per head, with what the release made of it, so
        # its agent never reads "no verdict yet" off GitHub while the deck holds one (#2992).
        async with self._lock(work_id):
            entry = self._round_entry(work_id)
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
            if not untested:
                # Tested, or the deck runs its tests again itself (#4253): the merge, the Merge
                # press or the run's own result follows, and anything that holds it has its
                # own message. Nothing to wake the chat for, but the pass counts as told, so
                # the chat's review wait ends quietly and a later pass at this head is a
                # repeat. Its agent still hears it, with its next message (#4303). A note
                # the deck could not keep is sent as a message instead, as before #4253, and
                # one it could not send either stays untold, for the next pass to try again.
                if fresh:
                    if self._keep_note(work_id, f"work-release:{work_id}:{message_key}", text):
                        told = entry.setdefault("told", [])
                        told.append(message_key)
                        del told[:-200]
                    else:
                        self._tell(work_id, entry, message_key, text)
                self.store.put(work_id, entry)
                self._review_answered(work_id, entry, message_key, [pull])
                return f"{PASS_RECORDED} {pull} at {head[:12]}"
            if fresh:
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

    def _ship_running_head(self, pull: str) -> str:
        try:
            return str(self.ports.ship_running_head(pull) or "")
        except Exception:  # noqa: BLE001 - unread: the line says press again, which joins it
            log.warning("work-release: running Ship now of %s unreadable", pull, exc_info=True)
            return ""

    def _ship_under_way(self, refs: Iterable[str]) -> str:
        """The hold line of the first of ``refs`` a Ship now press runs for now, or ""."""
        for ref in refs:
            running_at = self._ship_running_head(ref)
            if running_at:
                return f"{ref}'s Ship now at {running_at[:12]} {SHIP_UNDER_WAY}"
        return ""

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

    def _doc_fix_verdict(self, pull: str) -> review_history.ReviewEvent | None:
        """The verdict a push of doc files alone keeps for ``pull``: its newest verdict at any
        head, when that is a ``no-code`` one (#3322) or a pass still standing at its head
        (#4257). A pass a review was asked for again after keeps nothing: that review decides."""
        history = self._history(pull)
        held = review_history.newest_verdict(history)
        if held is None or held.kind == review_history.NO_CODE:
            return held
        if not review_history.passed(held):
            return None
        standing = review_history.standing_verdict(review_history.at_head(history, held.head))
        return held if standing is not None and standing.key == held.key else None

    def no_code_head(
        self, work_id: str, pull: str
    ) -> tuple[str, frozenset[str] | None] | tuple[str, frozenset[str] | None, bool] | None:
        """The head whose verdict a doc-only push keeps (``_doc_fix_verdict``), and its diff's
        files then (``None`` when unread, and always for a pass: then only doc files count), or
        ``None`` when ``pull``'s newest verdict is neither a ``no-code`` one nor a standing pass.
        A ``minor`` verdict (#4474) adds a third item, ``True``: its fix may change a little
        code in the files of that diff."""
        del work_id
        held = self._doc_fix_verdict(pull)
        if held is None:
            return None
        files = held.data.get("files") if held.kind == review_history.NO_CODE else None
        found = frozenset(files) if isinstance(files, list) else None
        if held.kind == review_history.NO_CODE and held.data.get("minor"):
            return held.head, found, True
        return held.head, found

    async def no_code_fixed(self, work_id: str, pull: str, head: str) -> str:
        """The agent fixed a ``no-code`` review's findings at ``head`` (#3322): the router saw
        it marked ready at that verdict's head, or proved the push to ``head`` touched only
        doc files. That review counts as a pass at ``head``, with no new review. A push of doc
        files alone after a standing pass keeps that pass the same way (#4257): the reviewer
        reads those files again at the pull request's next code round."""
        if not self.handles(work_id):
            return "not released by the deck"
        pull = pull.lower()
        async with self._lock(work_id):
            held = self._doc_fix_verdict(pull)
            if held is None:
                return f"no {NO_CODE} verdict or standing pass holds {pull}"
            kept = held.kind != review_history.NO_CODE
            if kept and same_commit(held.head, head):
                return f"{pull} already passed at {head[:12]}"
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
                (DOC_ONLY if kept else NO_CODE): held.head,
            }
            minor = not kept and bool(held.data.get("minor"))
            if minor:
                passed["minor"] = True
            for name in ("chat", "by", "note"):
                if held.data.get(name):
                    passed[name] = held.data[name]
            # Keyed by the verdict it answers, so a second one at the same head is a row of its
            # own rather than the first one's, read as already stored.
            word = "doc-only" if kept else "minor-fixed" if minor else "no-code-fixed"
            key = f"{word}:{held.key}@{facts.head}"
            # Carried from the no-code head, so a later base merge keeps it too (#3314).
            if not self._note(pull, facts.head, review_history.PASS, key, passed, held.head):
                return f"{pull}'s pass at {facts.head[:12]} {UNRECORDED}"
        old = held.head
        if kept:
            lead = (
                f"AgentDeck: {pull} at {facts.head[:12]} changed only doc files since"
                f" {old[:12]}, which passed review, so no new review was started and that pass"
                " carries over. The reviewer reads those files at this pull request's next"
                " code round; a push that changes any other file is reviewed as usual."
            )
            return await self._announce_pass(work_id, pull, facts.head, lead)
        if minor:
            lead = (
                f"AgentDeck: {pull} at {facts.head[:12]} fixes a review that asked only for minor"
                f" fixes at {old[:12]}, and any push since stayed small, so no new review was"
                " started and that review counts as a pass here."
            )
            return await self._announce_pass(work_id, pull, facts.head, lead)
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
        (#3957), which a base merge after them may keep (#3314 review).

        Findings or an escalation stamped by a review chat a newer review replaced, on any pull
        request of this Work its heading names, are an old round's too (#4386): dropped whole,
        before anything else reads them."""
        pulls = {pull.lower() for pull in self.ports.pulls(work_id)}
        stamp = review_history.round_stamp(text)
        for named in heading_targets(text, repository) if stamp else ():
            replaced = named in pulls and review_history.replaced_round(self._history(named), stamp)
            if replaced:
                return f"{named}: from review chat {replaced}, which a newer review replaced"
        target = findings_target(text, repository)
        if target is None or target[0] not in pulls:
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
                entry = self._round_entry(work_id)
                self._note_findings(entry, key, False, text)
                self.store.put(work_id, entry)
            return False
        body = text.strip()
        if len(body) > MAX_FINDINGS:
            body = body[:MAX_FINDINGS].rstrip() + " …"
        # Findings on a pull request merged by hand while its review ran can only be fixed
        # after it, in a follow-up pull request (#4189).
        found = self._findings_heads.get(key)
        ask = (
            f"Fix the findings. {SIBLING_SWEEP_ASK} Then push and mark the pull request ready"
            " again."
        )
        if found is not None and await self._merged(found[0]):
            ask = (
                f"{found[0]} had already merged before the review finished: fix the findings"
                f" in a follow-up pull request. {SIBLING_SWEEP_ASK}"
            )
        async with self._lock(work_id):
            # Nothing below drops the message: a refused store sends it again under this key.
            # Findings never carry the mechanism check: their verdict's message does (#4596).
            message = (
                f"AgentDeck: the deck's reviewer asks for changes on {_ref_of(work_id)}:"
                f"\n\n{body}\n\n{url}\n\n{ask} {OWNER_CALL_ASK}"
            )
            if self.ports.refusal(chat, message) is not None:
                message = (
                    f"AgentDeck: the deck's reviewer asks for changes on {_ref_of(work_id)}. Its"
                    " text cannot be passed on here, because this chat's CLI would drop it before"
                    f" the model saw it; read it on GitHub: {url}"
                    f"\n\n{ask} {OWNER_CALL_ASK}"
                )
            entry = self._round_entry(work_id)
            self._tell(work_id, entry, f"findings:{key}", message)
            told = f"findings:{key}" in (entry.get("told") or [])
            self._note_findings(entry, key, told, text)
            self.store.put(work_id, entry)
            # The deck's reviewer reviews the Work's one chat, which covers all its pull
            # requests; told first, so a refused message keeps the wait (#3183 review).
            self._review_answered(work_id, entry, f"findings:{key}", None)
        return told

    def _verdict_mechanism(self, pull: str, head: str, reviewed: str, key: str) -> str:
        """The mechanism check's ask for the message of a ``changes-needed`` verdict about
        ``pull`` at ``head`` (#4596), or ``""``: the findings the reviewer sent before it, read
        back from their ``findings`` row there, or at ``reviewed``, the head it read, where the
        router keeps them when a base merge moved the branch since, checked under the verdict
        message's ``key``."""
        if len(head or "") != 40:
            return ""
        try:
            history = self._history(pull)
            text = mechanism_check.findings_at(history, head) or (
                mechanism_check.findings_at(history, reviewed) if reviewed else ""
            )
        except Exception:  # noqa: BLE001 - the verdict goes on without the ask
            log.warning("work-release: %s's findings unread", pull, exc_info=True)
            return ""
        if not text:
            return ""
        target = mechanism_check.Target(
            pull,
            head,
            lambda: self._history(pull),
            lambda data: self._note(
                pull, head, review_history.MECHANISM, f"mechanism:{pull}", data
            ),
        )
        return mechanism_check.asks([target], text, key)

    async def on_design_review(
        self, work_id: str, key: str, text: str, url: str, *, gap: bool, notes: bool = False
    ) -> bool:
        """Hand a design review's verdict to the Work's chat once (#4332); ``False`` with no
        chat, or when the store would not keep the message, so the same ``key`` sent again
        tries again. ``key`` is the reviewer's request id; ``url`` where it is on GitHub;
        ``gap`` whether the verdict found one (the route has read it, ``design_review``);
        ``notes`` whether the deck recorded it as passed with notes after the last round
        (#4463)."""
        chat = self.ports.chat(work_id)
        if not chat:
            return False
        body = text.strip()
        if len(body) > MAX_FINDINGS:
            body = body[:MAX_FINDINGS].rstrip() + " …"
        ref = _ref_of(work_id)
        if gap:
            head = f"AgentDeck: the design review of {ref} found a gap."
            ask = (
                "Fix the design, not code: post the fixed design as a new comment on the issue"
                " opening with `**Design:**`, then call `POST"
                " $AGENTDECK_URL/api/sessions/<session-key>/design-review` again. A gap only"
                " the owner can settle is their call: ask them in your question box."
            )
        elif notes:
            head = f"AgentDeck: the design review of {ref} passed with notes."
            ask = (
                "A design gets two review rounds (#4463), and this was the last: start coding."
                " Do not post another design for these notes. Make the code answer each one;"
                " the code review checks the code against them."
            )
        else:
            head = f"AgentDeck: the design review of {ref} passed."
            # #4476: a redesign that answers a design finding waits on the owner too.
            ask = (
                "Build to this design; the code review checks the code against it. If it is a"
                " redesign answering a design finding, build it only once the owner has"
                " approved it in your question box."
            )
        message = f"{head}\n\n{body}\n\n{url}\n\n{ask}"
        if self.ports.refusal(chat, message) is not None:
            message = f"{head} Read it on GitHub: {url}\n\n{ask}"
        async with self._lock(work_id):
            entry = self._round_entry(work_id)
            self._tell(work_id, entry, f"design:{key}", message)
            told = f"design:{key}" in (entry.get("told") or [])
            self.store.put(work_id, entry)
        return told

    async def on_design_dropped(self, work_id: str, comment: str, token: str) -> bool:
        """Tell the Work's chat its design reviewer on ``comment`` stopped with no verdict
        (#4458): once per reviewer start ``token``, so asking again and that one stopping too
        is told again, and a sweep after a crash tells nothing twice. Whether it was told now."""
        ref = _ref_of(work_id)
        message = (
            f"AgentDeck: the design reviewer of {ref} (design comment {comment}) stopped"
            " without a verdict. Call `POST $AGENTDECK_URL/api/sessions/<session-key>"
            "/design-review` again to start a new one, and write no code until a review"
            " passes."
        )
        async with self._lock(work_id):
            entry = self._round_entry(work_id)
            told = self._tell(work_id, entry, f"design-dropped:{comment}:{token}", message)
            if told:
                self.store.put(work_id, entry)
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
            entry = self._round_entry(work_id)
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
            entry = self._round_entry(work_id)
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
        if not entry.get("asked"):
            return ""
        words = hold_line(hold) if isinstance(hold, dict) else ""
        if _held_for_fix(entry):
            # Held for the agent's CI fix (#4574): says so even before a hold is kept.
            fixed = ", ".join(_short(ref) for ref in _held_for_fix(entry))
            line = (
                f"Merge held: CI failed after your press and the agent fixed it ({fixed})."
                " It merges with no second press once the fix's review passes and its checks"
                " are green"
            )
            return f"{line}. Now: {words}." if words else f"{line}."
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
        elif cleared.get("why") == FIX_REFUSED:
            head = str(cleared.get("head") or "")[:12]
            what = f"{cleared['pull']}'s CI fix at {head} did not pass review"
        elif cleared.get("why") == REVIEW_REFUSED:
            head = str(cleared.get("head") or "")[:12]
            what = f"{cleared['pull']}'s review at {head} asked for changes"
        else:
            what = f"{cleared['pull']} joined the Work"
        return f"Merge approval cleared: {what}. Press Merge again to merge it."

    async def _check_press(self, work_id: str, entry: dict, facts: dict[str, Pull]) -> None:
        """Clear a person's Merge press once a diff it was for changed (#3320).

        A head that moved is read once: the same digest (a merge of the base) moves the
        press's head, a changed one clears it, and a failed read changes nothing.
        An open pull request the press never covered clears it too.

        A diff changed after CI failed on a head the press stood for is the agent's CI fix
        (#4574): the press is held instead, until the fix's head passes review (``_evaluate``
        takes that pass as the press's own) or a review since the hold began asks for changes,
        which drops it.
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
            held = row.get("held")
            if isinstance(held, dict):
                # Read from every head's verdicts since the hold began, so a push made after
                # the fix was refused cannot hide it.
                refused = self._refused_since(ref, entry, held)
                if refused:
                    return self._clear_press(work_id, entry, ref, refused, FIX_REFUSED)
                row["head"] = pull.head  # more pushes while held: the hold stands for them
                continue
            if row.get("head") and same_commit(pull.head, str(row["head"])):
                continue
            current = await self.ports.digest(ref, pull.head)
            if not current:
                continue
            if current == row.get("digest"):
                row["head"] = pull.head
                continue
            old = str(row.get("head") or "")
            replaced = await self._ci_fix(work_id, ref, old, pull.head) if old else []
            if replaced:
                log.info(
                    "work-release: %s: %s fixed CI at %s after the press; press held",
                    work_id,
                    ref,
                    pull.head,
                )
                row["held"] = held = {"from": old, "at": self.ports.clock(), "replaced": replaced}
                row["head"] = pull.head
                # A refusal of the fix written before this look saw it counts at once.
                refused = self._refused_since(ref, entry, held)
                if refused:
                    return self._clear_press(work_id, entry, ref, refused, FIX_REFUSED)
                continue
            return self._clear_press(work_id, entry, ref, pull.head, "changed")
        return None

    def _refused_since(self, ref: str, entry: dict, held: dict) -> str:
        """The first head of ``ref`` the press never stood for whose standing verdict, written
        since the press, asks for changes or escalated, else "" (#4574).

        Every head is read, so a push after the refusal cannot hide it, and by the head's own
        standing verdict, so an escalation the owner then accepted is no refusal. A refusal
        written before the look that began the hold counts too: the press's time bounds it.
        """
        asked = entry.get("asked")
        since = asked.get("at") if isinstance(asked, dict) else None
        since = float(since if isinstance(since, (int, float)) else held.get("at") or 0)
        covered = [str(held.get("from") or ""), *(str(h) for h in held.get("replaced") or ())]
        history = list(self._history(ref))
        for head in dict.fromkeys(event.head for event in history):
            if any(same_commit(head, old) for old in covered if old):
                continue
            verdict = review_history.standing_verdict(review_history.at_head(history, head))
            if (
                verdict is not None
                and verdict.kind in (review_history.CHANGES, review_history.ESCALATED)
                and verdict.at >= since
            ):
                return head
        return ""

    def _refuse_press(self, work_id: str, entry: dict, pull: str, head: str) -> None:
        """A review of ``pull`` at ``head`` asked for changes: a press held for its CI fix is
        dropped as such, and a press that stood for another head of it as refused; a press for
        exactly this head goes silently as before (#4574)."""
        row = (_asked_pulls(entry) or {}).get(pull)
        if row is None:
            return
        if isinstance(row.get("held"), dict):
            self._clear_press(work_id, entry, pull, head, FIX_REFUSED)
        elif row.get("head") and not same_commit(head, str(row["head"])):
            self._clear_press(work_id, entry, pull, head, REVIEW_REFUSED)

    async def _refuse_left_fix(self, work_id: str, pull: str, head: str) -> None:
        async with self._lock(work_id):
            entry = self._round_entry(work_id)
            row = (_asked_pulls(entry) or {}).get(pull)
            held = row.get("held") if row else None
            if not isinstance(held, dict):
                return
            replaced = [str(held.get("from") or ""), *(str(h) for h in held.get("replaced") or ())]
            if any(old and same_commit(head, old) for old in replaced):
                return  # a head the fix replaced: not a verdict on the fix
            self._clear_press(work_id, entry, pull, head, FIX_REFUSED)
            self.store.put(work_id, entry)

    async def _ci_fix(self, work_id: str, ref: str, old: str, new: str) -> list[str]:
        """Did CI fail on a commit of ``ref`` the push to ``new`` replaced (#4574)?

        Read now, when the changed diff is seen: the pull request's own commits from ``new``
        back along first parents to ``old``, ``old`` itself included (a look may have moved
        the press to the base-merged head that went red). Red GitHub checks there, or a failed
        verification of this Work, make it a CI fix. Anything unread is not one: the press is
        cleared as before.
        """
        if not self._holds_fixes():
            return []
        try:
            checks = await self.ports.replaced_checks(ref, old, new)
        except Exception:  # noqa: BLE001 - unread: not a CI fix
            log.warning("work-release: checks before %s of %s unread", new, ref, exc_info=True)
            return []
        if not checks:
            return []
        replaced = sorted(checks)
        if any(state == "red" for state in checks.values()):
            return replaced
        if self.ports.failed_heads is None:
            return []
        try:
            failed = await self.ports.failed_heads(work_id)
        except Exception:  # noqa: BLE001 - unread: not a CI fix
            return []
        if any(same_commit(sha.lower(), head) for sha in failed for head in checks):
            return replaced
        return []

    def _holds_fixes(self) -> bool:
        """Can a press be held for a CI fix here (#4574)? Not where the press is the review
        (#3623): nothing would ever review the fix."""
        return not self.ports.press_is_review and self.ports.replaced_checks is not None

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
                entry = self._round_entry(work_id)
                if entry.pop("needs_verify", None) is not None:
                    self.store.put(work_id, entry)
                if self.handles(work_id):
                    self._leave(work_id, refs=[pull.lower()])
            await self._wake_next(work_id)
            return
        if action == "closed" and merged and self.ports.light is not None:
            # Counted toward the review point only when the deck merged it light (#4399).
            with contextlib.suppress(Exception):
                await self.ports.light.merged(pull.lower())
        if action == "closed" and merged and self.handles(work_id):
            # A merge seen as it happened, by the deck or by hand: once the Work has nothing
            # open, its agent checks it live (#3141). Only a fresh merge, never an evaluation
            # of a Work that merged long ago.
            # An earlier round's merge delivered late is no fresh merge (#4585): checking it
            # live again would ask about the current round's merges instead.
            async with self._lock(work_id):
                entry = self._round_entry(work_id)
                if not self._is_earlier(work_id, pull):
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
            entry = self.store.get(work_id)  # read only; the look reads it through the round
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
            entry = self._round_entry(work_id)
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
            entry = self._round_entry(work_id)
            entry.pop("retry", None)
            self.store.put(work_id, entry)

    async def _green_now(self, work_id: str, policy: Policy, pull: Pull) -> bool:
        """``pull``'s head has green tests, read as the merge reads them past its review
        (#4579): GitHub's checks, else a green verification there; True with no tests asked.
        A failed read is not green."""
        if pull.repository in policy.review_only:
            return True
        if pull.repository in policy.github_checks:
            return pull.checks == "green"
        try:
            green = await self.ports.green_heads(work_id)
        except Exception:  # noqa: BLE001 -- unknown is not green
            log.warning("work-release: %s: green heads unread", work_id, exc_info=True)
            return False
        return any(same_commit(str(sha).lower(), pull.head) for sha in green)

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
        entry.pop(NEEDS_PASS, None)
        entry.pop(UNREVIEWED_GREEN, None)
        entry.pop(PRE_REVIEW_OWED, None)
        entry.pop(PRE_REVIEW_SAYABLE, None)
        # Only the pull requests read pending in this look keep their first-seen time.
        seen = entry.pop(PENDING_SEEN, None)
        seen = seen if isinstance(seen, dict) else {}
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
        # A Ship now press running says so before the holds it outlasts (#4559 review): the
        # agent's turn, a split's source kept a draft, a pass not yet recorded, at whichever
        # head its caller sent or it resolved to. They hold again once it ends, as a press
        # that ends looks again.
        under_way = self._ship_under_way(open_pulls)
        if under_way:
            return under_way
        if self.ports.busy(work_id):
            # Its agent may still push (#2481): looked at again once its chats are idle.
            entry["waiting_turn"] = True
            return "the Work's agent is still working"
        entry[PRE_REVIEW_SAYABLE] = True
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
            if self._light_on(policy, pull):
                light = await self._light_hold(work_id, entry, pull, seen, policy)
                if light is None:
                    pass  # not light at this head: held on its review as anywhere else
                elif light:
                    return light
                else:
                    continue
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
                passed_at = review_history.ship_pass_head(self._history(ref), pull.head)
                if passed_at:
                    return f"{ref}'s Ship now at {pull.head[:12]} {SHIP_UNFINISHED}"
                _needs_pass(entry, ref)
                entry[UNREVIEWED_GREEN] = await self._green_now(work_id, policy, pull)
                return f"{ref}{NO_PASS}{pull.head[:12]}"
            if pull.repository in policy.review_only:
                continue
            if pull.checks == "red":
                # Before anything else waits on it (#3163): no merge press, re-test or person
                # turns red checks green, so only the agent told here moves the Work.
                self._tell_red(work_id, entry, pull)
                return f"{ref}'s checks are red at {pull.head[:12]}"
            if pull.repository in policy.github_checks:
                if pull.checks == "pending":
                    return self._checks_pending(work_id, entry, pull, seen)
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
                    # The deck runs the Work's last verification again there itself when it
                    # can (#4253): its result tells the chat, so nothing is told now.
                    rerun = await self._rerun(work_id, entry, pull, key)
                    if not rerun and tell_untested:
                        # "Merges once it is green" was all one agent heard while its
                        # unreviewed sibling held it too (#4589).
                        unpassed = await self._awaiting_review(policy, facts, open_pulls, ref)
                        also = (
                            VERIFY_SIBLINGS_TEXT.format(names=", ".join(unpassed))
                            if unpassed
                            else ""
                        )
                        self._tell(
                            work_id,
                            entry,
                            f"verify:{key}",
                            REVIEW_PASSED_TEXT.format(ref=ref, head=pull.head[:12]) + also,
                        )
                    elif not rerun:
                        entry["untested"] = key
                    if unclean:
                        # Before ``moved``: a run on a clean tree is the first thing it needs.
                        # The deck's own run is one, in a fresh checkout (#4253).
                        if not rerun:
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
                        if not rerun:
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
                return self._checks_pending(work_id, entry, pull, seen)
            if pull.repository in policy.checks_required and pull.checks != "green":
                # Its CI has not reported on this head yet (#3919): the job proves only part.
                entry["waiting_checks"] = True
                return f"{ref}'s checks are {pull.checks} at {pull.head[:12]}"
        return ""

    def _light_on(self, policy: Policy, pull: Pull) -> bool:
        return self.ports.light is not None and policy.light(pull.repository) is not None

    async def _light_hold(
        self, work_id: str, entry: dict, pull: Pull, seen: dict, policy: Policy
    ) -> str | None:
        """A light pull request's hold (#4399): ``None`` when its head is not light, "" when
        its bench said no worse and exactly ``bin/test.sh`` is green there with checks not
        pending, else what holds it. No reviewer is asked or waited for."""
        ref, head = pull.ref, pull.head
        try:
            updated = str((entry.get("updated") or {}).get(ref) or "")
            look = await self.ports.light.look(ref, head, updated)
        except Exception:  # noqa: BLE001 - unread: held, never merged on a guess
            log.warning("work-release: light review of %s unread", ref, exc_info=True)
            look = {"verdict": "unread", "cases": []}
        out = entry.setdefault("light_out", {})
        if look is None:
            out.pop(ref, None)  # the reviewed path: only a light hold reads it
            return None
        verdict = look.get("verdict") or ""
        if verdict == "unread":
            # Its place is kept (``_failed_read``), and it is looked at again (rule 1).
            self._look_later(work_id, LIGHT_UNREAD_S)
            why = look.get("why") or "a read failed"
            return f"{ref}'s light review could not be read at {head[:12]} ({why})"
        if verdict == "agent":
            # It cannot merge before its agent answers: out of line (``NEEDS_PASS``).
            out[ref] = head
            _needs_pass(entry, ref)
            return f"{ref}@{head[:12]} is a new head; its agent calls pre-review-done"
        if verdict == "running":
            # The first in line keeps its place while its bench runs (rule 5).
            entry["waiting_tests"] = True
            return f"{ref} {light_hold_words(verdict, [])} at {head[:12]}"
        if verdict != "pass":
            # Out of the merge queue at once (rule 5): the way back is the agent's
            # pre-review-done, or the one retry out of line.
            out[ref] = head
            _needs_pass(entry, ref)
            return f"{ref} {light_hold_words(verdict, look.get('cases') or [])} at {head[:12]}"
        out.pop(ref, None)
        if pull.checks == "red":
            self._tell_red(work_id, entry, pull)
            return f"{ref}'s checks are red at {head[:12]}"
        green = set()
        if self.ports.script_green_heads is not None:
            green = {sha.lower() for sha in await self.ports.script_green_heads(work_id)}
        if not any(same_commit(sha, head) for sha in green):
            entry["waiting_tests"] = True
            if not await self._script_tested(work_id, head):
                key = f"{ref}@{head[:12]}"
                told = entry.setdefault("told", [])
                rerun = False
                if self.ports.rerun_verification is not None and f"light-rerun:{key}" not in told:
                    told.append(f"light-rerun:{key}")
                    del told[:-200]
                    try:
                        rerun = bool(
                            await self.ports.rerun_verification(
                                work_id, pull.repository, head, command=["bin/test.sh"]
                            )
                        )
                    except Exception:  # noqa: BLE001 - the chat is told to run it instead
                        log.warning(
                            "work-release: bin/test.sh of %s not queued", ref, exc_info=True
                        )
                if not rerun:
                    self._tell(
                        work_id,
                        entry,
                        f"light-tests:{key}",
                        LIGHT_TESTS_TEXT.format(
                            ref=ref, head=head[:12], work=work_id.partition(":")[2]
                        ),
                    )
            return f"{ref} has no green bin/test.sh run at {head[:12]}"
        if pull.checks == "pending":
            return self._checks_pending(work_id, entry, pull, seen)
        if pull.repository in policy.checks_required and pull.checks != "green":
            # The repository's required checks hold a light merge too (#4401 review).
            entry["waiting_checks"] = True
            return f"{ref}'s checks are {pull.checks} at {head[:12]}"
        return ""

    def queue_bench_look(self, work_id: str) -> None:
        """One look at ``work_id`` after a bench answer (#4399, rev 10, 11), never awaited by
        the caller: a call joins a queued look that has not started yet, so a burst of answers
        is one look, and an answer that lands while a look runs gets its own."""
        if not self._enabled:
            return
        queued = self._bench_look_queued.get(work_id)
        if queued is not None and not queued.done():
            return

        async def run() -> None:
            task = asyncio.current_task()
            if self._bench_look_queued.get(work_id) is task:
                self._bench_look_queued.pop(work_id, None)
            try:
                await self.evaluate(work_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("work-release: a bench look at %s failed", work_id, exc_info=True)
                await self._retry_raised_look(work_id)

        task = asyncio.get_running_loop().create_task(run(), name=f"bench-look:{work_id}")

        def done(finished: asyncio.Task) -> None:
            if self._bench_look_queued.get(work_id) is finished:
                self._bench_look_queued.pop(work_id, None)

        self._bench_look_queued[work_id] = task
        self._looks.add(task)
        task.add_done_callback(self._looks.discard)
        task.add_done_callback(done)

    def _checks_pending(self, work_id: str, entry: dict, pull: Pull, seen: dict) -> str:
        """``_hold_reason``'s answer for checks that read pending at ``pull``'s head.

        Past ``CHECKS_STUCK_S`` since the oldest pending one started, the answer says its CI
        never reported, which only a person moves (#4328); before that, a timed look comes
        when it would, since a lost status sends no delivery that would bring one. ``seen`` is
        the entry's ``PENDING_SEEN`` before this look.
        """
        entry["waiting_checks"] = True
        said = f"{pull.ref}'s checks are pending at {pull.head[:12]}"
        now = self.ports.clock()
        since = pull.pending_since
        if since is None:
            # No start time from GitHub (#4328 review): the hour runs from the first look that
            # read this head pending, so it never waits silently either.
            key = f"{pull.ref}@{pull.head}"
            before = seen.get(key)
            since = float(before) if isinstance(before, (int, float)) else now
            entry.setdefault(PENDING_SEEN, {})[key] = since
        left = since + CHECKS_STUCK_S - now
        if left <= 0:
            return f"{said} {CHECKS_NEVER_REPORTED}"
        self._look_soon(work_id, left + 1.0)
        return said

    async def _pull_waits(
        self,
        work_id: str,
        policy: Policy,
        facts: dict[str, Pull],
        open_pulls: list[str],
        seen: dict | None = None,
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
            elif self._light_now(policy, pull):
                missing = await self._light_lacks(work_id, pull, policy)
            elif not review_history.passed(self._verdict_at(ref, pull.head)):
                missing = (
                    PRE_REVIEW_WORDS if self._checklist_owed(ref, pull.head) else "not reviewed"
                )
            elif pull.repository in policy.review_only:
                pass
            elif pull.checks == "red":
                missing = "checks are red"
            elif pull.repository in policy.github_checks:
                missing = (
                    ""
                    if pull.checks == "green"
                    else CHECKS_NEVER_REPORTED_WORDS
                    if pull.checks == "pending" and self._checks_stuck(pull, seen)
                    else f"checks are {pull.checks}"
                )
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
                elif pull.checks == "pending" and self._checks_stuck(pull, seen):
                    missing = CHECKS_NEVER_REPORTED_WORDS
                elif pull.checks == "pending" or (
                    pull.repository in policy.checks_required and pull.checks != "green"
                ):
                    missing = f"checks are {pull.checks}"
            if missing:
                said.append({"pull": ref, "lacks": missing})
        return said

    def _checklist_owed(self, ref: str, head: str) -> bool:
        """Does ``ref``'s review at ``head`` wait on its agent's ``pre-review-done`` (#4589)?

        Only a checklist sent for this head counts: one left at an older head says nothing
        about the commit the agent would be told to name."""
        history = self._history(ref)
        return review_history.checklist_waits(history) and any(
            event.kind == review_history.CHECKLIST and same_commit(event.head, head)
            for event in history
        )

    async def _awaiting_review(
        self, policy: Policy, facts: dict[str, Pull], open_pulls: list[str], ref: str
    ) -> list[str]:
        """The Work's other open pull requests still waiting for a review pass (#4589).

        A light head needs none (#4399). One whose newest pass would carry to its head (the
        same diff, no review asked since, as ``_hold_reason`` carries it) is left out, as is
        one whose diff is unread: naming it would send the agent after a review that may never
        be needed. A pass for a diff the new head changed is no carry, so that one is named."""
        waiting = []
        for other in open_pulls:
            pull = facts[other]
            if other == ref or self._light_now(policy, pull):
                continue
            if review_history.passed(self._verdict_at(other, pull.head)):
                continue
            history = self._history(other)
            newest = review_history.newest_verdict(history)
            if (
                review_history.passed(newest)
                and newest.data.get("digest")
                and not any(
                    event.kind == review_history.ASKED
                    and (event.at, event.seq) > (newest.at, newest.seq)
                    for event in history
                )
            ):
                try:
                    current = await self.ports.digest(other, pull.head)
                except Exception:  # noqa: BLE001 - unread: not named, as nothing is known
                    continue
                if not current or current == newest.data["digest"]:
                    continue
            waiting.append(other)
        return waiting

    def _tell_pre_review(
        self,
        work_id: str,
        entry: dict,
        policy: Policy,
        facts: dict[str, Pull],
        open_pulls: list[str],
    ) -> None:
        """Tell an idle chat, once per head, that a review waits on its ``pre-review-done``.

        Read from every open pull request, not only the one the hold stopped at: a reviewed
        sibling with red or untested checks earlier in line would otherwise hide it, and the
        Work stalls as before (#4589 review). Only once the look got past the holds that keep
        the chat quiet (``PRE_REVIEW_SAYABLE``): never while its agent works, nor under a pause,
        a holding label or a Ship now press."""
        owed = [
            row["pull"]
            for row in entry.get("pull_waits") or ()
            if row.get("lacks") == PRE_REVIEW_WORDS
        ]
        if not owed:
            return
        entry[PRE_REVIEW_OWED] = owed
        for ref in owed:
            reviewed = [
                other
                for other in open_pulls
                if other not in owed
                and not self._light_now(policy, facts[other])
                and review_history.passed(self._verdict_at(other, facts[other].head))
            ]
            siblings = (
                SIBLINGS_TEXT.format(
                    names=", ".join(reviewed),
                    verb="waits" if len(reviewed) == 1 else "wait",
                    pron="it is" if len(reviewed) == 1 else "they are",
                )
                if reviewed
                else ""
            )
            head = facts[ref].head[:12]
            self._tell(
                work_id,
                entry,
                f"pre-review:{ref}@{head}",
                PRE_REVIEW_HOLD_TEXT.format(ref=ref, head=head, siblings=siblings),
            )

    def _light_now(self, policy: Policy, pull: Pull) -> bool:
        """Is ``pull``'s head light (#4399)? A local read; never GitHub."""
        if not self._light_on(policy, pull):
            return False
        try:
            return bool(self.ports.light.is_light(pull.ref, pull.head))
        except Exception:  # noqa: BLE001 - unread: not light, so its review holds it
            return False

    async def _light_lacks(self, work_id: str, pull: Pull, policy: Policy) -> str:
        """``_pull_waits``' words for a light pull request (#4399)."""
        from .light_review import head_cases, head_state

        answer = self.ports.light.answer_at(pull.ref, pull.head)
        verdict = head_state(answer)
        if verdict != "pass":
            return light_hold_words(verdict, head_cases(answer))
        if pull.checks == "red":
            return "checks are red"
        green: set[str] = set()
        if self.ports.script_green_heads is not None:
            with contextlib.suppress(Exception):
                green = {sha.lower() for sha in await self.ports.script_green_heads(work_id)}
        if not any(same_commit(sha, pull.head) for sha in green):
            return "bin/test.sh is not green"
        if pull.checks == "pending" or (
            pull.repository in policy.checks_required and pull.checks != "green"
        ):
            return f"checks are {pull.checks}"
        return ""

    def _checks_stuck(self, pull: Pull, seen: dict | None = None) -> bool:
        """Have ``pull``'s checks read pending past ``CHECKS_STUCK_S`` (#4328)? ``seen`` is the
        entry's ``PENDING_SEEN``, read when GitHub gave no start time."""
        since = pull.pending_since
        if since is None and seen:
            first = seen.get(f"{pull.ref}@{pull.head}")
            since = float(first) if isinstance(first, (int, float)) else None
        return since is not None and self.ports.clock() >= since + CHECKS_STUCK_S

    async def _rerun(self, work_id: str, entry: dict, pull: Pull, key: str) -> bool:
        """Have the runtime run this Work's last verification again at ``pull``'s head (#4253).

        Once per head: asked only while nothing runs there and nothing failed (``_tested``), so
        a second ask means the first run ended with no result (cut off), and the chat is then
        told to run it as before. Whether a run was queued now.
        """
        if self.ports.rerun_verification is None:
            return False
        told = entry.setdefault("told", [])
        mark = f"rerun:{key}"
        if mark in told:
            return False
        told.append(mark)
        del told[:-200]
        try:
            return bool(await self.ports.rerun_verification(work_id, pull.repository, pull.head))
        except Exception:  # noqa: BLE001 - the chat is told to run it instead
            log.warning("work-release: re-run of %s not queued", work_id, exc_info=True)
            return False

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

    async def _script_tested(self, work_id: str, head: str) -> bool:
        """``_tested`` for exactly ``bin/test.sh`` (#4401 review): a run of another command
        proves nothing about the script, so it neither runs it nor tells the agent to."""
        if self.ports.script_verifying is not None and await self.ports.script_verifying(work_id):
            return True
        if self.ports.script_failed_heads is None:
            return False
        red = await self.ports.script_failed_heads(work_id)
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

    def _work_round(self, work_id: str) -> object:
        """The Work's current round; ``None`` for none or unread (#4585)."""
        if self.ports.round_of is None:
            return None
        try:
            return self.ports.round_of(work_id)
        except Exception:  # noqa: BLE001 - unread: no round, everything counts as before
            log.warning("work-release: %s round unread", work_id, exc_info=True)
            return None

    def _is_earlier(self, work_id: str, pull: str) -> bool:
        return work_rounds.is_earlier(pull, self._work_round(work_id))

    def _round_entry(self, work_id: str) -> dict:
        """The Work's entry, with an earlier round's merge and live-check keys dropped (#4585).

        Every locked read of an entry goes through here, before anything is read or written
        in it, so an old round's ask, Merge press or ``merged_now`` never acts on the new
        round's pull requests, and a key written in the new round is never cleared as an old
        one's. The services process is the store's only writer; the caller puts it back."""
        entry = self.store.get(work_id)
        if work_rounds.drop_release_keys(entry, self._work_round(work_id)):
            log.info("work-release: %s: a new round cleared its entry", work_id)
        return entry

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
                NEEDS_PASS,
                "queued",
            ):
                entry.pop(key, None)
            self._leave(work_id)
            if any(facts[ref].state == "OPEN" for ref in refs):
                # A Ship now head merged while its fix waits as a draft for it (#4212 review):
                # its agent hears now, once, rather than when nothing is left open.
                fresh = [
                    ref
                    for ref in refs
                    if facts[ref].state == "MERGED" and ref not in entry.get("merged_pulls", ())
                ]
                if entry.get("merged_now") and self._shipped_merged(work_id, entry, fresh):
                    entry.pop("merged_now", None)
                    await self._ask_live_check(work_id, entry, policy, fresh)
                # A split keeps its source a draft, and opens its new pull request as one,
                # while the press runs (#4559 review): that press is what this waits on.
                drafts = [ref for ref in refs if facts[ref].state == "OPEN"]
                return self._ship_under_way(drafts) or "only drafts are open"
            # A merge seen live is kept through a failed read or a pull request still open,
            # until a look finds the Work with nothing open (#3141).
            if entry.pop("merged_now", None):
                await self._ask_live_check(
                    work_id, entry, policy, [r for r in refs if facts[r].state == "MERGED"]
                )
            return "no open pull request"
        # A pull request that left the Work's offer (closed, landed, a draft again) leaves the
        # merge queue too.
        self._leave(work_id, keep=open_pulls)
        reason = await self._hold_reason(work_id, entry, policy, facts, open_pulls, tell_untested)
        sayable = bool(entry.pop(PRE_REVIEW_SAYABLE, None))
        verdicts = {ref: self._verdict_at(ref, facts[ref].head) for ref in open_pulls}
        reviewed = all(review_history.passed(verdict) for verdict in verdicts.values())
        # The diff each open pull request's pass is for ("" with none), which a press covers.
        digests = {
            ref: str(verdict.data.get("digest") or "") if review_history.passed(verdict) else ""
            for ref, verdict in verdicts.items()
        }
        for ref in open_pulls:
            if self._light_now(policy, facts[ref]):
                # A light pull request has no reviewer's pass (#4399): its own diff's digest,
                # stored with its decision, is what a press covers.
                reviewed_light = self.ports.light.answer_at(ref, facts[ref].head)
                if reviewed_light.get("verdict") == "pass":
                    digests[ref] = str(reviewed_light.get("digest") or "")
        if not policy.auto_release and entry.get("asked"):
            # A held press takes its CI fix's pass as its own (#4574), before the check below
            # compares the press with the diffs passed now.
            held_rows = _asked_pulls(entry) or {}
            for ref in _held_for_fix(entry):
                if ref in digests and digests[ref]:
                    row = held_rows[ref]
                    row.update(digest=digests[ref], head=facts[ref].head)
                    row.pop("held", None)
                    log.info("work-release: %s: %s's CI fix passed; press stands", work_id, ref)
            if reviewed and not _held_for_fix(entry) and not _press_stands(entry, digests):
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
            entry["pull_waits"] = await self._pull_waits(
                work_id, policy, facts, open_pulls, entry.get(PENDING_SEEN)
            )
            if sayable:
                self._tell_pre_review(work_id, entry, policy, facts, open_pulls)
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
            self._ready(work_id, entry, f"ready:{','.join(heads)}", heads)
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
                await self._ask_live_check(work_id, entry, policy, [*merged, *open_pulls])
            elif self._shipped_merged(work_id, entry, open_pulls):
                # A draft is left open: only a Ship now head whose fix waits as that draft is
                # told now (#4212 review); its merge event then finds it asked already.
                entry.pop("merged_now", None)
                await self._ask_live_check(work_id, entry, policy, open_pulls)
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
            if self._light_now(policy, pull):
                # Saved before the update (#4399, rule 6): the head GitHub makes from this one
                # is proven the deck's, so the release decides and benches it without the
                # agent, even when the deck stops before ``updated`` below is written.
                try:
                    noted = await self.ports.light.updating(ref, pull.head)
                except Exception:  # noqa: BLE001
                    log.warning("work-release: light update of %s unread", ref, exc_info=True)
                    noted = False
                if not noted:
                    self._look_later(work_id, LIGHT_UNREAD_S)
                    return False, f"{ref}'s light review could not be read before its update"
            if await self.ports.update_branch(ref):
                # Written only once it worked, so a failed update is tried again.
                entry.setdefault("updated", {})[ref] = pull.head
                entry.setdefault("update_at", {})[ref] = [self.ports.clock(), 1]
                self._look_later(work_id, UPDATE_GRACE_S)
                # Its re-test is what it waits on now: a finished job looks again.
                entry["waiting_tests"] = True
                if queued:
                    self._round(pull.repository, ref)
                # No message (#4253): the new head's CI is what the merge waits on, and where a
                # verification job proves it, the deck runs the Work's last one again there
                # itself once the head lands (``_rerun``), telling the chat only when it cannot.
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
        # In a light-review repository, a head with no review pass merges only through the
        # light check (#4401 review), read again right here, and GitHub merges exactly this
        # head (``sha``, rule 3).
        light = self._light_on(policy, pull) and not review_history.passed(
            self._verdict_at(ref, pull.head)
        )
        if light:
            try:
                go, why = await self.ports.light.before_merge(work_id, ref, pull.head)
            except Exception:  # noqa: BLE001 - never merged on a guess
                log.warning("work-release: light check of %s failed", ref, exc_info=True)
                go, why = False, f"{ref}'s light check before the merge failed"
            if not go:
                return False, why
        outcome, words = await self.ports.merge(ref, policy.merge_method, pull.head)
        if outcome in (MERGED, ALREADY_MERGED):
            self._leave(work_id, refs=[ref])
        if outcome == MERGED and light:
            with contextlib.suppress(Exception):
                await self.ports.light.merged(ref, pull.head)
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

        A pull request that needs a new review or bench pass at its head leaves at once
        (``NEEDS_PASS``), whatever its checks say. Otherwise it keeps its place only while it
        waits on its own tests or turn (the first in line re-testing after an update), on
        GitHub's checks, or on an unreadable label. A failed
        re-test, or anything else that stops it (no pass at the head, a draft, a pause, a
        Poller card), takes it out of line and the next one goes (operator decision).
        """
        needs = entry.pop(NEEDS_PASS, None) or []
        if needs:
            # One rule for every head that needs a new pass, reviewed or light (#4401 review):
            # out of line at once, as no pending check can bring that pass.
            self._leave(work_id, refs=list(needs))
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
            CONFLICT_TEXT.format(ref=pull.ref, head=pull.head[:12])
            + (SHIP_CONFLICT_TEXT if self._shipped_head(pull) else ""),
        )
        self._leave(work_id, refs=[pull.ref])

    def _shipped_head(self, pull: Pull) -> bool:
        """Whether ``pull``'s head stands on a Ship now pass (``ship_pass_head``)."""
        return bool(review_history.ship_pass_head(self._history(pull.ref), pull.head))

    def _tell_red(self, work_id: str, entry: dict, pull: Pull) -> None:
        """Tell the Work's agent once per head that its pull request's checks failed."""
        text = (
            f"AgentDeck: {pull.ref}'s GitHub checks failed at {pull.head[:12]}, so it will not"
            " merge. Fix them, push, and mark it ready again."
        )
        if pull.ref in (_asked_pulls(entry) or {}) and self._holds_fixes():
            text += RED_PRESS_TEXT
        self._tell(work_id, entry, f"red:{pull.ref}@{pull.head[:12]}", text)
        self._leave(work_id, refs=[pull.ref])

    def _ready(self, work_id: str, entry: dict, key: str, heads: list[str]) -> None:
        """Ready for merge (#3199): the deck writes the evidence into the Work's summary itself
        and wakes nobody (#4253); only when it cannot is the agent asked to (``READY_TEXT``)."""
        told = entry.setdefault("told", [])
        if key in told:
            return
        refs = ", ".join(heads)
        if self.ports.note_evidence is not None:
            try:
                written = self.ports.note_evidence(work_id, EVIDENCE_TEXT.format(refs=refs))
            except Exception:  # noqa: BLE001 - the agent is asked to write it instead
                log.warning("work-release: evidence of %s not written", work_id, exc_info=True)
                written = False
            if written:
                told.append(key)
                del told[:-200]
                # Its agent hears what was added (#4306), so its next summary write keeps it.
                self._keep_note(
                    work_id,
                    f"work-release:{work_id}:evidence:{key}",
                    f"AgentDeck: {refs} is ready for merge. The deck appended this to the Work"
                    " summary's `correctness` part; keep it when you next write the summary:"
                    f" {EVIDENCE_TEXT.format(refs=refs)}",
                )
                return
        self._tell(work_id, entry, key, READY_TEXT.format(refs=refs))

    def _keep_note(self, work_id: str, note_id: str, text: str) -> bool:
        """Keep ``text`` for the Work's chat's next message, waking nothing (#4303)."""
        chat = self.ports.chat(work_id)
        if not chat or self.ports.note is None:
            return False
        try:
            return bool(self.ports.note(chat, note_id, text))
        except Exception:  # noqa: BLE001 - a note lost is a quiet pass, as before #4303
            log.warning("work-release: could not keep a note for %s", work_id, exc_info=True)
            return False

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

    async def _ask_live_check(
        self, work_id: str, entry: dict, policy: Policy, merged: Iterable[str]
    ) -> None:
        """Ask the Work's agent once to check its merged change live (#3141).

        Keyed by the merged pull requests, so the deck's own merge and GitHub's event for it
        are one message. ``live_check_asked`` is what the Work's status line reads: from then
        until the agent records a result, a closed issue is not yet a person's Finish.

        When the deck watches every one's deploy, it registers those waits itself and the
        request rides on them, so the chat is woken once, when the change is live (#4253).
        """
        refs = sorted(set(merged))
        if not refs:
            return
        shipped = self._findings_left(work_id, entry, refs)
        # Every pull request of this Work that merged, so a shipped head's fix counts as
        # merged whichever live check it came in (#4212 review).
        seen = entry.setdefault("merged_pulls", [])
        seen.extend(ref for ref in refs if ref not in seen)
        del seen[:-200]
        key = f"live-check:{','.join(refs)}"
        if key in (entry.get("told") or []):
            return
        docs, text = await self._live_text(policy, refs, shipped)
        # A docs-only merge changes nothing a deploy brings live: no wait for it (#4500).
        if not docs and await self._wait_deploys(work_id, entry, policy, refs, key, text):
            entry["live_check_asked"] = {"at": self.ports.clock(), "pulls": refs}
            return
        if self._tell(work_id, entry, key, text(False)):
            entry["live_check_asked"] = {"at": self.ports.clock(), "pulls": refs}

    async def _live_text(
        self, policy: Policy, refs: list[str], shipped: list[str] | None
    ) -> tuple[bool, Callable[[bool], str]]:
        """Whether ``refs`` change only docs or tests, and their live-check message by whether
        the deck registered their deploy waits (#4500). Files unread: today's full text."""

        def full(waited: bool) -> str:
            return _live_check_text(policy, refs, shipped, waited=waited)

        files = await self._merged_files(refs)
        if files is None:
            return False, full
        if all(live_check_scope.docs_only(_repository(ref), files[ref]) for ref in refs):
            return True, lambda waited: _docs_only_text(refs, shipped)
        if not all(_repository(ref) == DECK_REPOSITORY for ref in refs):
            return False, full
        processes = await self._loading(refs, files)
        touched = {path for ref in refs for path in files[ref]}

        def short(waited: bool) -> str:
            return _deck_short_text(policy, refs, shipped, touched, processes, waited=waited)

        return False, short

    async def _merged_files(self, refs: list[str]) -> dict[str, set[str]] | None:
        """Each merged pull request's files, or ``None`` when any is unread."""
        if self.ports.pull_files is None:
            return None
        files: dict[str, set[str]] = {}
        for ref in refs:
            try:
                read = await self.ports.pull_files(ref)
            except Exception:  # noqa: BLE001 - unread: today's full live check
                log.warning("work-release: files of %s unreadable", ref, exc_info=True)
                return None
            if read is None:
                return None
            files[ref] = set(read)
        return files

    async def _loading(self, refs: list[str], files: dict[str, set[str]]) -> frozenset[str] | None:
        """The deck's processes that load ``refs``' merges, read from each merge commit's
        own tree (#4500); ``None`` when unread, which names every process."""
        if self.ports.loading_processes is None:
            return None
        try:
            merges = []
            for ref in refs:
                facts = await self.ports.read_pull(ref)
                merged = facts is not None and facts.state == "MERGED"
                merges.append(((facts.merge_commit if merged else "") or "", files[ref]))
            return await self.ports.loading_processes(merges)
        except Exception:  # noqa: BLE001 - unread names every process
            log.warning("work-release: loading processes of %s unread", refs, exc_info=True)
            return None

    async def _wait_deploys(
        self,
        work_id: str,
        entry: dict,
        policy: Policy,
        refs: list[str],
        key: str,
        text: Callable[[bool], str],
    ) -> bool:
        """Register the deploy waits of ``refs`` for the Work's chat (#4253); whether it did.

        Only for one merged pull request whose deploy the deck watches (this deck's own, or a
        repository in ``auto_deploys``), its merge commit read. Several merged at once would
        need one wake once every deploy landed, which no wait gives: each would wake the chat
        to check all of them while the others are not live yet (#4253 review). Those, and a
        merge nothing watches, are still the chat's to follow, so then it is told as before.
        """
        chat = self.ports.chat(work_id)
        if self.ports.wait_deploy is None or not chat or len(refs) != 1:
            return False
        repositories = [ref.partition("#")[0].lower() for ref in refs]
        if not all(
            repository == DECK_REPOSITORY or repository in policy.deploys
            for repository in repositories
        ):
            return False
        bodies = []
        for ref, repository in zip(refs, repositories, strict=True):
            facts = await self.ports.read_pull(ref)
            sha = facts.merge_commit if facts is not None and facts.state == "MERGED" else ""
            if not sha:
                return False
            if repository == DECK_REPOSITORY:
                bodies.append({"deck_deploy": sha})
            else:
                body = {"deployment": f"{repository}@{sha}"}
                name = dict(policy.deploy_names).get(repository, "")
                if name:
                    body["name"] = name
                bodies.append(body)
        try:
            stored = self.ports.wait_deploy(chat, refs, bodies, text(True))
        except Exception:  # noqa: BLE001 - the chat is told to register them instead
            log.warning("work-release: deploy waits of %s not stored", work_id, exc_info=True)
            return False
        if not stored:
            return False
        told = entry.setdefault("told", [])
        told.append(key)
        del told[:-200]
        return True

    def _shipped_merged(self, work_id: str, entry: dict, merged: list[str]) -> bool:
        """Whether one of ``merged`` shipped with Ship now and its fix has yet to merge, so
        its agent must hear of the merge before the Work's draft fix can go on (#4212)."""
        if not merged:
            return False
        left = self._findings_left(work_id, entry, merged)
        return bool(left) and bool({ref.lower() for ref in merged} & set(left))

    def _findings_left(self, work_id: str, entry: dict, refs: list[str]) -> list[str] | None:
        """The pull requests this Work shipped with Ship now whose findings' fix has not
        merged (#4212 review): no fix named yet, or one that is neither among ``refs`` nor
        merged before. Any other pull request merging fixes nothing; only the named one does,
        and never the shipped one itself. One shipped in an earlier round of the Work is that
        round's history (#4585) and holds nothing now. ``None`` when the record could not be
        read."""
        merged = {ref.lower() for ref in (*refs, *entry.get("merged_pulls", ()))}
        try:
            records = list(self.ports.shipped_findings(work_id))
            round_ = self._work_round(work_id)
        except Exception:  # noqa: BLE001 - unread: unknown, not "none left"
            log.warning("work-release: shipped heads of %s unreadable", work_id, exc_info=True)
            return None
        left = {
            pull.lower()
            for pull, fix in records
            if not work_rounds.is_earlier(pull, round_)
            and (not fix or fix.lower() == pull.lower() or fix.lower() not in merged)
        }
        return sorted(left)


def _live_check_text(
    policy: Policy, refs: list[str], shipped: Iterable[str] | None = (), *, waited: bool = False
) -> str:
    """``LIVE_CHECK_TEXT`` for these merged pull requests; ``shipped`` are the ones Ship now
    shipped with findings still to fix in this Work (#4212 review), ``None`` when unread.
    ``waited``: the deck registered their deploy waits itself, and this rides on them (#4253)."""
    deploy = DEPLOY_WAITED_TEXT if waited else deploy_sentence(policy, refs)
    text = LIVE_CHECK_TEXT.format(refs=", ".join(refs), deploy=deploy)
    return text + _shipped_suffix(shipped)


def _shipped_suffix(shipped: Iterable[str] | None) -> str:
    """What keeps the issue open after a live check while a Ship now head's findings are
    unfixed (#4212 review), or "" when none is; ``None`` is an unread record."""
    if shipped is None:
        return SHIPPED_UNREAD_LIVE_CHECK
    shipped = list(shipped)
    return SHIPPED_LIVE_CHECK.format(refs=", ".join(shipped)) if shipped else ""


def _docs_only_text(refs: list[str], shipped: Iterable[str] | None) -> str:
    """``DOCS_ONLY_TEXT``: no close while a Ship now addition keeps the issue open."""
    suffix = _shipped_suffix(shipped)
    close = "" if suffix else DOCS_ONLY_CLOSE
    return DOCS_ONLY_TEXT.format(refs=", ".join(refs), close=close) + suffix


def _deck_short_text(
    policy: Policy,
    refs: list[str],
    shipped: Iterable[str] | None,
    files: set[str],
    processes: frozenset[str] | None,
    *,
    waited: bool = False,
) -> str:
    """``DECK_SHORT_TEXT`` for merges into this deck (#4500), naming the processes that
    load them (all of them when unread) and the skills install when it carries them."""
    deploy = DEPLOY_WAITED_TEXT if waited else deploy_sentence(policy, refs)
    if processes is None:
        said = PROCESSES_UNREAD_TEXT
    else:
        names = [
            name
            for name in (live_check_scope.SERVICES, live_check_scope.RUNTIME)
            if name in processes
        ]
        plural = len(names) > 1
        noun = f"{' and '.join(names)} " + ("processes load" if plural else "process loads")
        which = "each" if plural else "it"
        said = PROCESSES_TEXT.format(names=noun, which=which) if names else ""
    skills = SKILLS_TEXT if live_check_scope.touches_skills(files) else ""
    if live_check_scope.touches_deploy(files):
        skills += DEPLOY_TEXT
    text = DECK_SHORT_TEXT.format(
        refs=", ".join(refs), deploy=deploy, processes=said, skills=skills
    )
    return text + _shipped_suffix(shipped)


def _repository(ref: str) -> str:
    return ref.partition("#")[0].lower()


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


def _held_for_fix(entry: dict) -> list[str]:
    """The pull requests whose CI fix a press is held for (#4574), sorted."""
    pulls = _asked_pulls(entry) or {}
    return sorted(
        ref
        for ref, row in pulls.items()
        if isinstance(row, dict) and isinstance(row.get("held"), dict)
    )


def _press_stands(entry: dict, digests: dict[str, str]) -> bool:
    """Does a person's Merge press cover the open pull requests at the diffs passed now
    (``digests``, by pull request; #3175)?"""
    if entry.get("asked") is True:
        return True  # pressed on a deck that kept no digests: for whatever is open
    pulls = _asked_pulls(entry)
    if not pulls:
        return False
    if _held_for_fix(entry):
        return False  # waiting for its CI fix's review (#4574)
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
    green = entry.pop(UNREVIEWED_GREEN, None)
    owed = entry.pop(PRE_REVIEW_OWED, None)
    if answer == NO_PULLS_KNOWN or _failed_read(answer):
        return  # no answer: the last one stands, and so does its time
    if not answer or answer == AWAITING_MERGE:
        entry.pop("hold", None)
        return
    words, person = hold_words(answer, needs_verify=bool(entry.get("needs_verify")))
    named = unreviewed_at(answer)
    if named and isinstance(owed, list) and named[0] in {ref.lower() for ref in owed}:
        words = PRE_REVIEW_WORDS
    if pressed and not person:
        words = f"Merge pressed; {words}"
    old = entry.get("hold")
    kept = isinstance(old, dict) and old.get("why") == answer
    green = bool(green) if green is not None and unreviewed(answer) else None
    if kept and green is not None and old.get("green") is not green:
        # Tests that turned green start the wait for a review afresh (#4579): a red head
        # could not ask for one.
        kept = False
    since = old.get("since") if kept else None
    entry["hold"] = {"why": answer, "words": words, "person": person, "since": since or now}
    if pulls:
        entry["hold"]["pulls"] = pulls
    if green is not None:
        entry["hold"]["green"] = green


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


def unreviewed(answer: str) -> bool:
    """The release holds the Work on a pull request with no review pass at its head (#4579).

    Reached only past the earlier holds: not paused or labelled, its agent idle, out of draft,
    not conflicting with its base.
    """
    return NO_PASS in answer


def unreviewed_at(answer: str) -> tuple[str, str] | None:
    """The pull request (lowercased ``owner/repo#n``) and head prefix an ``unreviewed``
    answer names, or None for any other answer."""
    ref, found, head = answer.partition(NO_PASS)
    if not found or not _HOLD_PULL_RE.fullmatch(ref) or not head:
        return None
    return ref.lower(), head.lower()


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
    if unreviewed(answer):
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
    if answer.endswith(CHECKS_NEVER_REPORTED):
        return CHECKS_NEVER_REPORTED_WORDS, True
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
    fields = (
        "state,isDraft,headRefOid,baseRefName,mergeable,mergeStateStatus,statusCheckRollup,"
        "mergeCommit"
    )
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
        pending_since=pending_since(raw.get("statusCheckRollup")),
        merge_commit=_merge_commit(raw.get("mergeCommit")),
    )


def _merge_commit(raw: object) -> str:
    """``mergeCommit``'s sha, lowercased; "" for none or an unexpected shape."""
    oid = raw.get("oid") if isinstance(raw, dict) else None
    return oid.strip().lower() if isinstance(oid, str) else ""


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


def first_parent_walk(parents: dict[str, list[str]], old: str, new: str) -> list[str] | None:
    """The commits from ``new``'s first parent back to ``old``, ``old`` included (#4574).

    ``parents`` holds only the pull request's own commits (GitHub's pull request commit list),
    so a base merge's base-branch commits are never walked. ``None`` when ``old`` is not reached
    within ``FIX_WALK_MAX`` commits of that list.
    """
    old, sha = old.lower(), new.lower()
    walked: list[str] = []
    while len(walked) < FIX_WALK_MAX:
        firsts = parents.get(sha) or []
        if not firsts:
            return None
        sha = firsts[0].lower()
        walked.append(sha)
        if same_commit(sha, old):
            return walked
        if sha not in parents:
            return None
    return None


def rollup_items(nodes: object) -> list[dict]:
    """A GraphQL ``statusCheckRollup.contexts`` node list as ``checks_state`` reads it: check
    runs and commit statuses alike, each with its ``name``."""
    items: list[dict] = []
    for node in nodes if isinstance(nodes, list) else ():
        if not isinstance(node, dict):
            continue
        if "context" in node:
            items.append({"name": node.get("context"), "state": node.get("state")})
        else:
            items.append(
                {
                    "name": node.get("name"),
                    "conclusion": node.get("conclusion"),
                    "state": node.get("status"),
                }
            )
    return items


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPLACED_QUERY = """query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {%s}
}"""
_COMMIT_CHECKS = """
    c%d: object(oid: "%s") { ... on Commit { statusCheckRollup { contexts(first: 100) { nodes {
      ... on CheckRun { name conclusion status }
      ... on StatusContext { context state } } } } } }"""


async def replaced_checks(ref: str, old: str, new: str) -> dict[str, str] | None:
    """``Ports.replaced_checks`` from GitHub: the pull request's commit list (with parents),
    then one GraphQL read of the walked commits' checks (#4574)."""
    from . import gh_cli

    repository, _, number = ref.partition("#")
    owner, _, name = repository.partition("/")
    if not number.isdigit() or not owner or not name:
        return None
    try:
        code, out = await gh_cli.run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repository}/pulls/{number}/commits?per_page=100",
                "--jq",
                ".[] | {sha: .sha, parents: [.parents[].sha]}",
            ],
            timeout=_GH_TIMEOUT_S,
            repo=repository,
        )
        if code != 0:
            return None
        parents: dict[str, list[str]] = {}
        for line in out.decode().splitlines():
            if line.strip():
                row = json.loads(line)
                parents[str(row["sha"]).lower()] = [str(sha) for sha in row["parents"]]
    except (OSError, TimeoutError, ValueError, KeyError, TypeError):
        return None
    walked = first_parent_walk(parents, old, new)
    if not walked or not all(_SHA_RE.match(sha) for sha in walked):
        return None
    query = _REPLACED_QUERY % "".join(_COMMIT_CHECKS % (i, sha) for i, sha in enumerate(walked))
    try:
        code, out = await gh_cli.run(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
            ],
            timeout=_GH_TIMEOUT_S,
            repo=repository,
        )
        repo = json.loads(out)["data"]["repository"] if code == 0 else None
    except (OSError, TimeoutError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(repo, dict):
        return None
    states: dict[str, str] = {}
    for i, sha in enumerate(walked):
        commit = repo.get(f"c{i}")
        rollup = commit.get("statusCheckRollup") if isinstance(commit, dict) else None
        contexts = rollup.get("contexts") if isinstance(rollup, dict) else None
        nodes = contexts.get("nodes") if isinstance(contexts, dict) else None
        states[sha] = checks_state(rollup_items(nodes)) if nodes is not None else "none"
    return states


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
