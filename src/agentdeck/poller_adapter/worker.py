"""Delivering a directive to a worker, exactly once (#180 slice 4).

The engine never delivers. It mints a lease, records the send intent durably, and hands out a
directive; this is what turns that directive into a real worker and then writes the receipt the
engine reads on its next wake. That split is what makes delivery **at-most-once** instead of
best-effort:

* The intent is durable *before* anything is sent, so a crash mid-delivery leaves a dispatch the
  engine will await rather than a worker nobody recorded.
* A delivery whose outcome is *uncertain* — a timeout, a dropped connection — writes **no**
  receipt and is never retried here. The engine decides: it awaits the acknowledgement inside its
  window and only then retries under the same generation and delivery id, which the runtime
  deduplicates (#2824). Retrying here would be the one way to get two workers on one card.
* The receipt is keyed by the dispatch id, so it can only ever confirm the delivery it was
  written for.

The engine's own stable delivery id is the directive's ``dispatch_id``; AgentDeck's worker client
has its own at-most-once id, and the two are deliberately kept separate — this module maps one to
the other rather than assuming they are the same thing.

A delivery goes straight to the persistent runtime's ``ensure_delivery`` over its host-local socket
(#2144), so a web deploy or restart is not in its path. A delivery the runtime cannot take is
refused or left uncertain. It is never retried through the web, because two paths for one
delivery is exactly how one card gets two workers.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from urllib.parse import quote

from ..ensure_delivery import ENSURE_DELIVERY_CONTRACT, ENSURE_DELIVERY_PATH
from ..models import IssueRef
from ..poller_engine.config import (
    WORKER_DEPLOY_PRODUCTION,
    WORKER_DEPLOY_STAGING,
    WORKER_MERGE_ON_REQUEST,
)
from ..poller_engine.github import (
    clear_receipt,
    public_session_url,
    write_receipt,
)
from ..public_origin import public_origin
from ..work import work_id_for

# A spec-only card parks in Needs More Info whatever the spec says (unless a person asked to close
# it, #2661), and a standing "grill me" instruction reads that ending as a call for questions.
# #2424's spec asked four, and three had answers the worker had already given itself (#2427). A
# question costs the person a round trip on a spec they read in full before anything is built, so
# choices go in a numbered list they can overrule instead. The one deliberate exception is
# disagreement (#3234): on #3228 the owner asked to be grilled on their own choices, and arguing
# against them caught five real problems, so a worker that doubts a choice now challenges it
# unasked, with no cap, while the spec is open. Defined here rather than in `runner`, which
# already imports this module.
SPEC_ONLY_QUESTION_RULE = (
    "The person reviewing this spec reads every decision in it before anything is built, so a "
    "question only adds a round trip. Decide everything you can, and list each choice as a "
    "numbered **Decisions** item (D1, D2, …) right after the summary, one line each with its "
    "reason; they can overrule any of them by number while approving. In a chat reply, never "
    "cite a decision or question by its number alone: restate it in a few words (#2779). Ask "
    "a question only for a preference only the person holds and nothing you can read reveals: "
    "extra cost (API calls, "
    "tokens, money), accepting a known gap or risk, or a conflict with another requester's "
    "recent work. Give your recommendation with each question. Scope beyond what the issue "
    "asked for is not a question: leave it out, or mention it in one line. "
    "The one other question is a challenge, asked without being asked to (#3234): while the "
    "spec is still open, grill the person on anything in it you think is not a good idea, "
    "whether a choice they made, one of yours they accepted, or an earlier choice of theirs "
    "the spec reverses. Do it only when you really disagree or doubt, never as a ritual. Ask "
    "each challenge as one question in your structured question tool (where you have none, "
    "as an open question in the spec), worded `You chose X. I think Y is better, because "
    "<fact>. Which do you want?`, with your recommended option first. Ask up to four a "
    "round, and keep asking rounds while you still disagree with something; never re-ask a "
    "choice the person has answered. Write each answer into the spec comment at once, "
    "marked as the owner's choice, before the next round. Keep your own copy of the spec and "
    "edit that copy, because re-reading the comment through `gh` can return a stale cached "
    "copy that overwrites your earlier edits. Once the spec is approved, build it rather "
    "than reopen its choices. Whenever your question tool asks whether the spec is ready, "
    "include the option `Grill me on the spec`; if it is chosen, challenge every choice you "
    "have any doubt about. "
    "Most specs have no questions; then write `No open questions — approve, or overrule a "
    "decision by number.` "
    "Keep the summary, the Decisions and the questions (or that line) open, and put the "
    "detailed spec — files, wording, tests, steps — inside one collapsed `Details` block. "
    "This brief takes precedence over any standing instruction to question the requester "
    "before starting."
)

# A feature's spec can promise a saving the person never feels. #2791's "Approve spec" button
# saved ten seconds of typing, not the trip to read the spec, and the worker's first two summaries
# missed it; only a walk through what the owner actually does showed it. The owner wants that walk
# before a feature is built, not in the final report or in questions to them (#2799).
USER_STEPS_RULE = (
    "When you write a spec or plan before building a feature, first walk through what the "
    "person actually does and thinks, step by step, before and after the change. Say which "
    "steps the change removes or shortens, and which stay the same (reading, deciding). If it "
    "only shortens a small step, say so plainly in the spec, so the person can decide before "
    "any code is written. Example (#2791): 1. The owner sees \"Spec is on the issue. Approve "
    "it?\" and thinks *what spec, is it what I meant?* 2. They cannot decide from the row, so "
    "they open the issue and read the spec; that takes minutes, and it is where the decision "
    "happens. 3. They decide. 4. They act. The new button only replaces step 4. This walk "
    "belongs in the spec or plan only, not in the final report or in questions to the person. "
    "The use-case walkthrough of the built code (#2919) is a separate check with its own line "
    "in the final report."
)

# A reviewer checks the implementation worker's use-case walkthrough (#2919). Kept here, not only
# in `merge_review_brief`, because compaction replaces that brief with a one-line review ask and
# would otherwise drop the check for every compacted reviewer.
USE_CASE_REVIEW_RULE = (
    "When the change adds or changes a use case, check the card's terminal comment for a "
    "use-case walkthrough that follows the person to their goal, past the confirmation. Treat "
    "a missing walkthrough, one that stops at a success message, or one that skips a step the "
    "diff touches as `changes-needed` unless you can supply it yourself."
)

# What ends a spec: a person's approval, after which the poller starts the build itself (#2706).
# Mid-turn, only the spec worker's `--spec-approved` finish counts as that approval (#2773).
# Before that a spec worker told the person to post `/claude`, because worker-control refuses a
# build while the label is on and the spec worker's brief carries no build steps.
SPEC_APPROVAL_RULE = (
    "Implementation starts only after a person approves the spec. If a person approves it in "
    "this chat or on the issue, remove the label with `gh issue edit --remove-label`, post an "
    "issue comment quoting who approved and what they said, and end with `finish "
    "needs_more_info --spec-approved`: the poller then starts the implementation worker by "
    "itself, so do not ask anyone to post `/claude`. Without that flag no build starts, even "
    "with the label off — so if the person removed the label but asked for something else "
    "(no spec, an investigation, a question answered), do that and end with a plain `finish "
    "needs_more_info`. Never remove the label without a person's explicit approval."
)


def spec_approval_rule(build_brief: str = "") -> str:
    """The spec brief's approval rule, and how this worker builds when asked to (#2716).

    A person who approves in the worker's own chat and asks it to build should not wait a wake
    for a new worker that rediscovers the context. ``build_brief`` is the build's own brief the
    host wrote for this dispatch; without one the poller's start is the only way.
    """
    if not build_brief:
        return SPEC_APPROVAL_RULE
    return (
        "Implementation starts only after a person approves the spec, and never remove the "
        "label without a person's explicit approval. When a person approves it, remove the "
        "label with `gh issue edit --remove-label`. If they approved in this chat **and** asked "
        "you to build it, build it here: run your control command's `start-implementation "
        "--body-file <path>`, with a body quoting who approved and what they said. It posts "
        "that on the issue, moves the card back to In progress under this same generation, and "
        "keeps the poller from starting another worker. Then read "
        f"`{build_brief}` and follow it in place of this brief, ending with its terminal "
        "command. If it is refused, tell the person why and end as below. Otherwise — approval "
        "alone, or approval on the issue — post an issue comment quoting who approved and what "
        "they said, and end with `finish needs_more_info --spec-approved`: the poller then "
        "starts the implementation worker by itself, so do not ask anyone to post `/claude`. "
        "Without that flag no build starts, even with the label off — so if the person removed "
        "the label but asked for something else, do that and end with a plain `finish "
        "needs_more_info`."
    )


# The ask of the worker the poller starts for that approved spec (#2706).
SPEC_APPROVED_ASK = (
    "A person approved the specification on this card: the `spec-only` label was removed "
    "after it was posted. Implement the approved spec — its newest version on the issue, with "
    "any decision the person overruled while approving — as an ordinary card."
)

# The full brief's "use needs_more_info only when…" sentence lives in the Worker control section,
# which `_compact_poller_brief` rebuilds from its command lines alone, so no worker ever read it
# (#2427). It is restated where the rebuild keeps it.
NEEDS_MORE_INFO_GUARD = (
    "Use `needs_more_info` only when a human decision is required that nothing you can read "
    "settles; decide the rest yourself and say what you decided."
)


# A Work's title is its issue's, which is whatever the requester typed first — a pasted chat
# link named one Work for good (#2474). The worker is the one who learns what the Work is, and
# its terminal report is where it says so, so that is where it names it. Asked, never enforced:
# a missing name is cosmetic and must not hold up a report.
WORK_TITLE_ASK = (
    "Name this Work on the deck just before you run your terminal command, if no worker has "
    "named it yet: read `GET $AGENTDECK_URL/api/sessions/<session-key>/work-title`, and when "
    "its `agent_title` is empty, `PUT` the same path with `{\"title\": \"<what this Work "
    "is>\"}` — the concrete problem or objective your report describes, in English, under 80 "
    "characters, without the `repo#n` prefix. Skip it when your report says you could not tell "
    "what the Work is about. Rename it later only when a report changes what the Work is — a "
    "different problem, or a scope a person redirected — never for a fix, a rebase or a new "
    "review round. The GitHub issue is not renamed, and a name a person gave the Work on the "
    "deck still shows over yours."
)

# A finished card is often taken back: the person reading the report in this chat asks for
# more. Without saying so, a worker reopens the issue by hand and the poller releases the card
# anyway (#2522), or asks the person a second time for a `/claude` they in effect just gave
# (#2845). The verb lives in `poller_engine.worker_control`.
REOPEN_ASK = (
    "If you finished and a person in this chat then asks you to carry on or reopen the work, "
    "run your control command's `reopen --body-file <path>` first, with a body that says what "
    "the person asked. If the card is still yours it moves back to In progress at once: do the "
    "work and end with the same terminal command as usual. If the poller already merged or "
    "closed it, `reopen` queues the work instead: end your turn with no finish, and the poller "
    "brings it back to this chat. Do not ask the person a second time."
)


def deploy_rule(level: object) -> str:
    """The brief's one sentence on deploying, for a project's deploy level (#2656).

    ``level`` is the Manifest's ``policy.worker_may_deploy`` as the host stamped it on the
    directive. Anything else — a directive from before the field, or a value nobody defined —
    keeps the old rule that forbids every deploy, so a missing setting never widens it.
    """
    if level == WORKER_DEPLOY_STAGING:
        allowed = "only to staging, dev-cluster or canary environments, never to production"
    elif level == WORKER_DEPLOY_PRODUCTION:
        allowed = "to staging, dev-cluster, canary or production environments"
    else:
        return "Do not deploy from this worker."
    return (
        f"Deploy only when a requester explicitly asked for that deploy on this card or in this "
        f"chat, and {allowed}; the repository's own deploy rules still apply. Name what you "
        "deployed and how you verified it in your terminal comment."
    )


def merge_rule(level: object, base: str = "the base branch") -> str:
    """The brief's sentence on pushing to the base branch and merging (#2723).

    ``level`` is the Manifest's ``policy.worker_may_merge`` as the host stamped it on the
    directive, or ``None`` for a turn that must never merge (a spec or a merge review). Anything
    but ``on_request`` keeps the old rule, so a missing setting never widens it. Pushing to the
    base branch is forbidden either way: "merge" means merging the card's pull request.
    """
    if level != WORKER_MERGE_ON_REQUEST:
        return (
            f"Never push to {base}, and do not merge a pull request yourself: the poller "
            "merges once the card is released."
        )
    return (
        f"Never push to {base} directly. Merge this card's own pull request only when a "
        "requester explicitly asked for that merge on this card or in this chat. If the merge "
        "deploys the project, name what it deployed in your terminal comment. After that merge, "
        "finish `resolved`."
    )


def push_rules(directive: dict, base: str = "the base branch") -> str:
    """The brief's push, merge and deploy sentences, in the one form both briefs share."""
    never_merges = bool(directive.get("spec_only")) or (
        str(directive.get("action") or "") == "merge-review"
    )
    merge_level = None if never_merges else directive.get("worker_may_merge")
    return f"{merge_rule(merge_level, base)} {deploy_rule(directive.get('worker_may_deploy'))}"

class DeliveryOutcome(Enum):
    DELIVERED = "delivered"
    REFUSED = "refused"
    SAFE_RETRY = "safe-retry"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Delivery:
    """One delivery's outcome and why (#482)."""

    outcome: DeliveryOutcome
    reason: str = ""
    # The account the runtime placed a scheduled delivery on, as the Overlay spells it (#2146).
    account: str = ""
    # The session that took an accepted delivery, when the runtime named it (#2155).
    session_key: str = ""

    @property
    def detail(self) -> str:
        return f"{self.outcome.value} ({self.reason})" if self.reason else self.outcome.value


CLEAN_REFUSALS = frozenset({
    "at_capacity",
    "over_budget",
    "session_over_budget",
    "cwd_required",
    "delivery_id_conflict",
    "delivery_correlation_conflict",
    "codex_create_failed",
    "codex_start_failed",
    "active_turn",
    "worker_inactive",
    "worker_not_steerable",
    "worker_changed",
    "context_operation_pending",
    # The direct runtime path (#2144). Each is decided before the runtime writes anything.
    "runtime_unavailable",
    "runtime_upgrade_required",
    "correlation_required",
    "target_unavailable",
    "context_maintenance_unavailable",
    # The runtime's own scheduling refusals (#2146), each decided before any provider write.
    "no_candidate_accounts",
    "all_accounts_hard_ineligible",
    "requirements_unsatisfied",
    "runtime_provider_unavailable",
    "routing_unavailable",
    # Another Run of the same Work may still be writing its workspace (#2159).
    "prior_run_executing",
})

# Any host name will do over a Unix socket; this one says which process answers.
RUNTIME_HOST = "http://agentdeck-runtime"
# curl's own exit code for "could not connect". The request never left this process.
CURL_COULDNT_CONNECT = 7


@dataclass
class WorkerDispatcher:
    """Delivers directives to the persistent runtime and records what was accepted.

    ``runtime_socket`` is the runtime's host-local socket, and every delivery goes there.

    ``candidates`` and ``usage_targets`` are the Overlay's account policy. Every delivery carries
    them to the runtime as requirements, and the runtime chooses the account (#2146). ``note``
    hears where it chose and why.
    """

    receipts_dir: Path
    timeout: float = 30.0
    curl: str = "curl"
    runtime_socket: Path | None = None
    candidates: tuple[str, ...] = ()
    usage_targets: dict[str, float] = field(default_factory=dict)
    note: Callable[[str], None] | None = None
    # Every account the runtime placed a delivery on this wake, in order.
    placed: list[str] = field(default_factory=list)
    # This deck's public address, for the session link a receipt carries (#2155).
    origin: Callable[[], str] = public_origin
    _last_schedule: dict | None = field(default=None, init=False, repr=False)

    def refuse(self, directive: dict, reason: str, *, now: datetime | None = None) -> Delivery:
        write_receipt(
            self.receipts_dir.parent / "dispatch-deferrals",
            str(directive.get("repo") or ""),
            int(directive.get("number") or 0),
            str(directive.get("dispatch_id") or ""),
            now=now or datetime.now(UTC),
            extra={"reason": reason},
        )
        return Delivery(DeliveryOutcome.REFUSED, reason)

    def deliver(
        self,
        directive: dict,
        *,
        cwd: str | None = None,
        message: str = "",
        worktrees: list[str] | None = None,
        now: datetime | None = None,
    ) -> Delivery:
        """Deliver once; receipt only a positively accepted delivery."""
        moment = now or datetime.now(UTC)
        repo = str(directive.get("repo") or "")
        number = int(directive.get("number") or 0)
        dispatch_id = str(directive.get("dispatch_id") or "")
        deferrals_dir = self.receipts_dir.parent / "dispatch-deferrals"
        review_only = str(directive.get("action") or "") == "merge-review"
        delivery_message = _compact_poller_brief(
            directive,
            message or _fallback_message(directive),
            cwd=cwd,
            worktrees=worktrees or [],
        )
        # Which card this worker session is for, durably (#2127). The chat link is the deck's
        # *grouping*; this is the relation that has to survive it failing, because "which chats
        # worked this card" is the question a person asks when the grouping is exactly what went
        # wrong. `directive["ref"]` is the short form the board uses (`ledger#1913`); only the
        # brief carries the owner, so it is read back from the brief that was just rendered.
        issue_ref = _card_issue_ref(directive, delivery_message, message)
        correlation = delivery_correlation(directive, issue_ref)
        if correlation is None:
            # The engine named a Work this card is not, or a Run that is not its lease: sending
            # it would bind a session to the wrong execution, so nothing is sent (#2142).
            return self.refuse(directive, "correlation_mismatch", now=moment)
        if not correlation:
            # The runtime's contract takes only the engine's own Work and Run. Sending this
            # delivery without them would be the uncorrelated path, so it is not sent at all.
            return self.refuse(directive, "correlation_required", now=moment)
        if self.runtime_socket is None:
            # The runtime is the only path; a delivery is never routed through the web (#2144).
            return self.refuse(directive, "runtime_unavailable", now=moment)
        artifacts = directive.get("command_artifacts")
        request = {
            "contract": ENSURE_DELIVERY_CONTRACT,
            "delivery_id": f"poller:{dispatch_id}",
            # The Work and Run this delivery belongs to, as machine metadata (#2142). Never
            # rendered into the brief: the model is not the audience.
            **correlation,
            "requirements": {
                "candidates": list(self.candidates),
                "avoid_account": str(directive.get("avoid_account") or "") or None,
                "usage_targets": dict(self.usage_targets),
            },
            "worker_key": directive.get("ref"),
            "message": delivery_message,
            "cwd": cwd,
            "fresh": review_only,
            "role": "kanban",
            "allow_over_budget": bool(directive.get("allow_over_budget")),
            "require_idle": bool(directive.get("continuation")) or review_only,
            "require_active": bool(directive.get("steer")),
            "issue_ref": issue_ref or None,
            "continues_session_url": str(directive.get("previous_session_url") or "") or None,
            "command_artifacts": dict(artifacts) if isinstance(artifacts, dict) else None,
        }
        if not clear_receipt(deferrals_dir, repo, number, dispatch_id):
            return Delivery(DeliveryOutcome.REFUSED, "deferral_cleanup_failed")
        delivery = self._ensure(request)
        self._report(directive, delivery)
        if delivery.outcome is DeliveryOutcome.DELIVERED:
            # Which chat took this Run, recorded by the host rather than reported by the worker
            # (#2155): the next dispatch of this card continues that chat (#2127), and a worker
            # asked to register it was ceremony that sometimes never happened.
            session_url = _published_session_url(self.origin(), delivery.session_key)
            extra = {**(correlation or {}), **({"session_url": session_url} if session_url else {})}
            write_receipt(
                self.receipts_dir,
                repo,
                number,
                dispatch_id,
                now=moment,
                worktrees=worktrees or (),
                # The accepted delivery names its Work and Run, so the poller reads them off
                # the receipt instead of inferring them later (#2142).
                extra=extra or None,
            )
        elif (
            delivery.outcome is DeliveryOutcome.SAFE_RETRY
            or delivery.outcome is DeliveryOutcome.REFUSED
            and delivery.reason in CLEAN_REFUSALS
            or directive.get("steer")
        ):
            self.refuse(directive, delivery.reason or "refused", now=moment)
        return delivery

    def _report(self, directive: dict, delivery: Delivery) -> None:
        if delivery.account and delivery.outcome is DeliveryOutcome.DELIVERED:
            self.placed.append(delivery.account)
        if self.note is None or not self._last_schedule:
            return
        schedule = self._last_schedule
        where = schedule.get("account") or "no account"
        why = f" ({schedule['reason']})" if schedule.get("reason") else ""
        notes = "; ".join(str(line) for line in schedule.get("notes") or [])
        self.note(
            f"{directive.get('ref')}: runtime {schedule.get('decision')} {where}{why}"
            + (f" — {notes}" if notes else "")
        )

    def _ensure(self, request: dict) -> Delivery:
        """One ``ensure_delivery`` call over the runtime socket, read into an outcome.

        Every answer that proves the runtime wrote nothing is a clean refusal. That covers a
        socket nobody listens on, a runtime too old to know the route, and a request it
        rejected as invalid. Anything else that is not a readable answer stays uncertain, and
        nothing here tries again, least of all through the web.
        """
        self._last_schedule = None
        try:
            done = subprocess.run(
                [
                    self.curl,
                    "--silent",
                    "--show-error",
                    "--max-time",
                    str(int(self.timeout)),
                    "--unix-socket",
                    str(self.runtime_socket),
                    "-H",
                    "Content-Type: application/json",
                    "--data-binary",
                    json.dumps(request),
                    "--write-out",
                    "\n%{http_code}",
                    f"{RUNTIME_HOST}{ENSURE_DELIVERY_PATH}",
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout + 5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return Delivery(DeliveryOutcome.UNCERTAIN, "transport")
        if done.returncode == CURL_COULDNT_CONNECT:
            return Delivery(DeliveryOutcome.REFUSED, "runtime_unavailable")
        if done.returncode != 0:
            return Delivery(DeliveryOutcome.UNCERTAIN, "transport")
        body, _, status = (done.stdout or "").rpartition("\n")
        if status == "200":
            delivery, self._last_schedule = _read_ensure_outcome(body)
            return delivery
        if status == "404":
            # This runtime predates `ensure_delivery`. Its router refused the path before
            # anything ran. Sending the uncorrelated request instead is what must not happen.
            return Delivery(DeliveryOutcome.REFUSED, "runtime_upgrade_required")
        if status == "422" and _requirements_unknown(body):
            # A runtime from before #2146 refused the new field before anything ran. The
            # delivery waits for its upgrade; it is never re-sent with an account chosen here.
            return Delivery(DeliveryOutcome.REFUSED, "runtime_upgrade_required")
        if status in {"400", "409", "422"}:
            return Delivery(DeliveryOutcome.REFUSED, "http-error")
        return Delivery(DeliveryOutcome.UNCERTAIN, "http-error")


def _fallback_message(directive: dict) -> str:
    parts = [
        str(message.get("payload") or "").strip()
        for message in directive.get("messages") or []
        if str(message.get("payload") or "").strip()
    ]
    return "\n\n".join(parts)


def _message_section(message: str, heading: str, *, last: bool = False) -> str:
    """Return one host-authored Markdown section."""
    marker = f"## {heading}\n\n"
    start = message.rfind(marker) if last else message.find(marker)
    if start < 0:
        return ""
    end = message.find("\n\n## ", start + len(marker))
    return message[start:end if end >= 0 else len(message)].strip()


def _control_commands(host_text: str) -> list[str]:
    """Keep exact host-authored lifecycle commands, never commands quoted by the user.

    Only ``finish`` and ``review`` are lifecycle (#2155): every other verb a worker was once
    told to run was ceremony, so nothing else is carried even if an older brief still names it.
    """
    commands: list[str] = []
    for line in host_text.splitlines():
        if not line.startswith("    "):
            continue
        command = line.strip()
        if command and re.search(r"\s(?:finish|review)\s", f" {command} "):
            if command not in commands:
                commands.append(command)
    return commands


def _qualified_ref_from_brief(message: str, fallback: str) -> str:
    first = message.splitlines()[0] if message else ""
    prefix = "You are working "
    suffix = " for the AgentDeck poller."
    if first.startswith(prefix) and first.endswith(suffix):
        return first[len(prefix):-len(suffix)] or fallback
    return fallback


def _qualified_card_ref(candidate: str) -> str:
    """`owner/repo#n` if that is what this is, else `""`.

    The deck registers this against the worker session so the card survives the chat
    link failing (#2127). Only the qualified form is any use: the board's short
    `ledger#1913` names no owner, and two configured repositories can share a short
    name, so registering one would be a guess about which repository a person meant.
    Nothing qualified means nothing registered — a missing relation is recoverable, a
    wrong one sends a reader to another repository's issue.
    """
    owner, _, number = candidate.strip().partition("#")
    repo_owner, slash, repo_name = owner.partition("/")
    if number.isdigit() and slash and repo_owner and repo_name and "/" not in repo_name:
        return f"{owner}#{number}"
    return ""


def delivery_correlation(directive: dict, issue_ref: str) -> dict[str, str] | None:
    """The delivery's ``work_id`` and ``run_id``; ``{}`` when uncorrelated, None when wrong.

    Both come from the engine's durable dispatch (#2142) and are only *checked* here, never
    derived: a directive missing either is uncorrelated, and the caller sends nothing. ``run_id``
    must equal the dispatch generation, since the Run *is* the generation. ``work_id`` must be
    canonical under the same pure function the deck uses (#2140), and this card's Work whenever
    the card's own reference names one. A pair that fails any check is None, and the caller
    sends nothing.
    """
    generation = str(directive.get("generation") or "").strip()
    run_id = str(directive.get("run_id") or "").strip()
    named = str(directive.get("work_id") or "").strip()
    if not run_id or not named:
        return {}
    if generation and run_id != generation:
        return None
    ref = IssueRef.parse(issue_ref) if issue_ref else None
    card_work = work_id_for(ref) if ref is not None else ""
    kind, _, source = named.partition(":")
    source_ref = IssueRef.parse(source) if kind == "github_issue" else None
    if source_ref is None or work_id_for(source_ref) != named:
        return None
    if card_work and card_work != named:
        return None
    return {"work_id": named, "run_id": run_id}


def _card_issue_ref(directive: dict, *briefs: str) -> str:
    """This card as `owner/repo#n`, preferring the directive over rendered prose."""
    for candidate in (str(directive.get("ref") or ""), *briefs):
        # A brief names the card on its first line; a directive ref already is one.
        ref = _qualified_card_ref(candidate) or _qualified_card_ref(
            _qualified_ref_from_brief(candidate, "")
        )
        if ref:
            return ref
    return ""


def _label_issue_read(pre_ask: str) -> str:
    for line in pre_ask.splitlines():
        command = line.strip()
        if line.startswith("    ") and command.startswith("gh issue view "):
            return command
    return ""


def _compact_poller_brief(
    directive: dict,
    message: str,
    *,
    cwd: str | None,
    worktrees: list[str],
) -> str:
    """Reduce a host-composed poller brief to task-shaped wire context (#1732).

    Worker-control already enforces generation and lifecycle writes, and root guides own
    repository policy. The wire brief therefore keeps the authoritative ask, workspace
    identity, live collision evidence and the exact lifecycle commands for this generation —
    ``finish`` and ``review``, and nothing else (#2155).
    Arbitrary/manual messages pass through byte-for-byte.
    """
    worker_marker = "## Worker control\n\n"
    ask_marker = "What was asked:\n\n"
    if worker_marker not in message or not message.startswith("You are working "):
        return message

    ask_pos = message.find(ask_marker)
    pre_ask = message[:ask_pos] if ask_pos >= 0 else message
    ref = _qualified_ref_from_brief(message, str(directive.get("ref") or ""))
    repo = str(directive.get("repo") or "")
    number = directive.get("number") or ""

    asks: list[str] = []
    for item in directive.get("messages") or []:
        payload = str(item.get("payload") or "").strip()
        if not payload:
            continue
        author = str(item.get("author") or "").strip()
        asks.append(f"{payload}\nRequester: {author}" if author else payload)
    ask = "\n\n".join(asks)

    action = str(directive.get("action") or "")
    if action == "merge-review":
        prs = [str(value) for value in directive.get("pull_requests") or [] if str(value).strip()]
        targets = ", ".join(prs) if prs else "the card's existing pull request"
        ask = (
            f"Review {targets}. Run the repository's review-fix procedure once, fix actionable "
            "findings in the existing PR branch, push the fixes, and do not merge.\n\n"
            f"{USE_CASE_REVIEW_RULE}"
        )
    elif not ask:
        issue_read = _label_issue_read(pre_ask)
        read_with = f" Run `{issue_read}`." if issue_read else ""
        ask = (
            f"Read issue {ref or f'{repo}#{number}'} in full, including comments, and do what it "
            f"asks. This card was started by its ownership label.{read_with}"
        )

    if directive.get("spec_only"):
        ask += (
            "\n\nThis card carries the poller's `spec-only` label. Write a concrete "
            "implementation specification as an issue comment, but do not edit repository "
            "files, create or switch a branch, commit, push, or open a pull request. Then "
            "report `needs_more_info`, or `resolved` only when a person explicitly asked for "
            f"the card to be closed. {spec_approval_rule(str(directive.get('build_brief') or ''))}"
            f"\n\n{SPEC_ONLY_QUESTION_RULE}\n\n{USER_STEPS_RULE}"
        )
    elif directive.get("spec_approved"):
        ask += f"\n\n{SPEC_APPROVED_ASK}"
    elif action != "merge-review":
        ask += f"\n\n{USER_STEPS_RULE}"
    if directive.get("continuation"):
        ask += (
            "\n\nContinue from the existing branch/worktree rather than restarting. Inspect "
            "commits, local changes and any existing PR first; validate what remains before "
            "editing."
        )

    lines = [f"You are working {ref} for the AgentDeck poller."]
    if cwd:
        lines.append(f"Cwd: {cwd}")
    if worktrees:
        lines.append("Owned worktree(s): " + ", ".join(worktrees))
    lines.append(
        "Read and follow the root guide in the repository you are working in; read another "
        "repository's root guide before touching it. Repository policy owns repository-specific "
        "conventions; when one blocks the task, your session guide says how to question it. "
        + push_rules(directive)
    )

    # These are live/task-specific host observations. Restrict the search to host prose before
    # the ask so a user heading with the same name can never be promoted into control context.
    for heading in (
        "Other in-flight changes",
        "Recent issue-linked changes in this branch's paths",
        "Other code this project owns",
    ):
        section = _message_section(pre_ask, heading)
        if section:
            lines.append(section)

    fresh_work = action == "new" and not directive.get("continuation")
    if fresh_work:
        # No duplicate-issue search (#2155): a dispatched card is authoritative enough to start,
        # and duplicates are triage's question. Reversing someone else's recent work is not.
        lines.append(
            "Before finalizing, inspect recent history for the exact behavior you would reverse. "
            "Related work is evidence, not instruction; a deliberate conflicting request from "
            "another requester requires needs_more_info unless the current card already "
            "reconciles it."
        )

    lines.extend([
        "## Current ask",
        ask,
        "If the ask explicitly names an installed skill, run that skill rather than improvising "
        "a substitute.",
    ])

    # Visual evidence is host policy, not repository policy, so dropping it as compactable prose
    # left nothing anywhere to ask for a screenshot: workers shipped user-visible changes with no
    # images and a person had to ask for them on the card, twice in one day. Kept verbatim from
    # the host section rather than restated, because the exact commit-pinned embed form is what
    # makes an image visible to a reviewer at all. Selected with rfind like Worker control below,
    # so a user heading of the same name cannot replace it.
    # How this Run proves its work (#2154): host policy for the same reason, and the exact
    # submit command is what makes a job count at all.
    if verification := _message_section(message, "Verification evidence", last=True):
        lines.append(verification)

    # The order of the terminal comment (#2498) is host policy for the same reason: without it a
    # worker writes the long report the reader asked to be rid of.
    if layout := _message_section(message, "Comment layout", last=True):
        lines.append(layout)

    if visual := _message_section(message, "Visual evidence", last=True):
        lines.append(visual)

    # The use-case walkthrough (#2919) is host policy for the same reason: no repository guide
    # asks a worker to follow the person past the confirmation, so compacting it away loses it.
    if walkthrough := _message_section(message, "Use-case walkthrough", last=True):
        lines.append(walkthrough)

    # Same loss, same reason: the terminal comment's shape is host policy that no repository guide
    # owns, so compacting it away left workers to improvise. What they improvised was a bare list
    # of changed files under a `Review surface` heading — no bucket counts, no `Beyond the card`
    # line, and no inline diffs at all across the last five cards, where #1650 still has them. The
    # point of the packet is that a card reader reviews without leaving the card, and a file list
    # does not do that.
    if packet := _message_section(message, "Issue review packet", last=True):
        lines.append(packet)

    # The host-authored Worker control section is last. Use the last matching heading so user text
    # cannot inject a command.
    control_source = _message_section(message, "Worker control", last=True)
    commands = _control_commands(control_source)

    lines.append("## Worker control")
    # The lease fences Poller lifecycle, not GitHub access (#2152): a stale worker that read
    # "stop" as "say nothing" threw away what it had learned.
    lines.append(
        "The Poller lease fences lifecycle only: the terminal finish and a review verdict go "
        "through worker-control, which proves this generation itself when it runs, and status "
        "is never edited directly. There is no separate lease check and no plan comment to "
        "post. Ordinary issue comments, follow-up issues and GitHub reads use normal `gh` with "
        "your existing permissions; never start one with a board command such as `/claude`. "
        "`unavailable:` is retryable. `stale:`/`disabled:` means stop code and workspace "
        "changes, pushes and lifecycle operations — you may still post one ordinary comment "
        "saying what your turn reached, or open a follow-up issue."
    )
    # A parked worker answered in its own chat is resumed on the same lease (#2348), so the
    # control command it already holds is the one its next report goes through.
    lines.append(
        "If this card rests in Needs More Info and a person answers in this chat, the deck posts "
        "the answer on the issue and moves the card back to In progress for you: carry on, and "
        "end with the same control command as usual. A turn that only answers in the chat ends "
        "with one too — `finish needs_more_info` if you still need an answer; a turn that ends "
        "without one reads as a crash."
    )
    # A worker whose `resolved` closed the card can take it back when asked (#2522).
    lines.append(REOPEN_ASK)
    lines.append(NEEDS_MORE_INFO_GUARD)
    lines.append(WORK_TITLE_ASK)
    if commands:
        # Anything that is not the spelled-out engine invocation is a card control wrapper. Asked
        # this way round rather than by token position, because a state directory with a space in
        # it would put the verb somewhere else and silently cost the worker this sentence.
        # Composed prose explaining the wrapper would not survive this rebuild, and without it the
        # worker is handed an unexplained absolute path and told nothing about the one failure
        # mode the three refusal prefixes do not cover: the command itself being gone (#1772).
        if " worker-control " not in f" {commands[0]} ":
            wrapper = re.split(r"\s(?:finish|review)\s", f"{commands[0]} ", maxsplit=1)[0]
            lines.append(
                f"Control command: `{wrapper}`. That path is this dispatch's own control "
                "command: the card and this generation "
                "are already inside it, so pass only the verb and its arguments, and run it with "
                "no arguments to list the verbs it takes. If it is missing or will not run, this "
                "dispatch has been superseded — stop as if refused with `stale:` rather than "
                "moving the card or its status yourself."
            )
    if commands:
        lines.append("After all work, use exactly one applicable terminal command:")
        lines.extend(f"- `{value}`" for value in commands)
    # The closing line names only what this brief actually carries. Saying "include review-surface
    # /visual evidence only when relevant" while the sections that define both are present would
    # give the worker permission to skip them, which is how each of them came to be skipped.
    required = " and ".join(
        part for part, present in (
            ("the review packet", packet), ("the visual evidence", visual)
        ) if present
    )
    lines.append(
        f"Terminal body: concise human summary plus verification, and {required} required above. "
        "Never report work you did not finish."
        + (
            " Lay it out as Comment layout says: the outcome first, screenshots open, then only "
            "what the reader must act on, and everything else folded."
            if layout
            else ""
        )
        if required
        else "Terminal body: concise human summary plus verification; include review-surface/"
        "visual evidence only when relevant. Never report work you did not finish."
    )

    compact = "\n\n".join(part.strip() for part in lines if part and part.strip())
    return compact if ask.strip() else message


def _read_ensure_outcome(body_text: str) -> tuple[Delivery, dict | None]:
    """Read one ``ensure_delivery`` answer, strictly (#2144 review), and its schedule (#2146).

    The web-era reader acknowledges anything it cannot parse. Here, delivered means a readable
    answer to this contract that says ``accepted: true``. A body that is not JSON, names no
    contract or another one, or has no boolean ``accepted`` proves nothing. It is uncertain,
    so it is never acknowledged and never re-sent. A delivery accepted without saying
    which account took it is just as unreadable: the watch that follows its turn needs one.
    """
    try:
        body = json.loads(body_text or "")
    except ValueError:
        return Delivery(DeliveryOutcome.UNCERTAIN, "unreadable_answer"), None
    if (
        not isinstance(body, dict)
        or body.get("contract") != ENSURE_DELIVERY_CONTRACT
        or not isinstance(body.get("accepted"), bool)
    ):
        return Delivery(DeliveryOutcome.UNCERTAIN, "unreadable_answer"), None
    schedule = body.get("schedule") if isinstance(body.get("schedule"), dict) else None
    account = str((schedule or {}).get("account") or "")
    if body["accepted"]:
        if not account:
            return Delivery(DeliveryOutcome.UNCERTAIN, "unreadable_answer"), schedule
        session_key = body.get("session_key")
        return Delivery(
            DeliveryOutcome.DELIVERED,
            account=account,
            session_key=session_key.strip() if isinstance(session_key, str) else "",
        ), schedule
    refused = _read_outcome(body_text)
    return Delivery(refused.outcome, refused.reason, account=account), schedule


def _published_session_url(origin: str, session_key: str) -> str:
    """The public ``/sessions/<key>`` link for a delivered session, or ``""``.

    Nothing without a public origin: a loopback link on GitHub opens the reader's own machine.
    """
    if not origin or not session_key:
        return ""
    return public_session_url(f"{origin.rstrip('/')}/sessions/{quote(session_key, safe=':')}")


def _requirements_unknown(body_text: str) -> bool:
    """Is this 422 an older runtime refusing the ``requirements`` field it does not know?"""
    try:
        detail = json.loads(body_text or "").get("detail")
    except (ValueError, AttributeError):
        return False
    return isinstance(detail, list) and any(
        isinstance(item, dict)
        and item.get("type") == "extra_forbidden"
        and list(item.get("loc") or [])[-1:] == ["requirements"]
        for item in detail
    )


def _read_outcome(stdout: str) -> Delivery:
    try:
        body = json.loads(stdout or "")
    except ValueError:
        return Delivery(DeliveryOutcome.DELIVERED)
    if not isinstance(body, dict) or body.get("accepted") is not False:
        return Delivery(DeliveryOutcome.DELIVERED)
    reason = str(body.get("reason") or "")
    if (
        body.get("retryable_same_delivery") is True
        and body.get("provider_write_state", "not_started") == "not_started"
    ):
        return Delivery(DeliveryOutcome.SAFE_RETRY, "retryable_same_delivery")
    outcome = DeliveryOutcome.REFUSED if reason in CLEAN_REFUSALS else DeliveryOutcome.UNCERTAIN
    return Delivery(outcome, reason)
