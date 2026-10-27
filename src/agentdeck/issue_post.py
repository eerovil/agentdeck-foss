"""Also post a chat message to its Work's issue (#2416).

A person often steers a worker in its chat, and nothing of that reaches the issue, which is all
a later worker and the ChatGPT reviewer read. Ticking "Also post to issue" on one message makes
the deck post that message on the issue, quoted and verbatim, as the owner's non-human
authority, at most once per action id. The message reaches the agent whatever becomes of the
post.

The quote comes from the same helper as the Poller's #2348 relay, so typed text can never read
as a control command or forge a lease marker. The ledger is ``work_actions`` under its own
action name: an unsettled row may have landed and is never repeated.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import gh_cli
from .models import IssueRef
from .poller_engine.chat_answers import quote_operator_text
from .poller_engine.ready_review import FINDINGS_MARKER
from .work import WorkRef

log = logging.getLogger(__name__)

GH_TIMEOUT_S = 15.0
# A failed or uncertain post stays on the chat page this long, so it survives a reload.
NOTICE_MAX_AGE = timedelta(hours=24)
MARKER = "<!-- agentdeck-chat-post id="
REVIEWER_MARKER = "<!-- agentdeck-reviewer-comment id="

DONE = "done"
FAILED = "failed"
UNCERTAIN = "uncertain"
RELAYED = "relayed"
# A parked card's answer held while the runtime restarts: relayed once delivered (#2847).
HELD = "held"
UNKNOWN = "unknown"
RESTARTING = "restarting"
UNRECORDED = "unrecorded"


def issue_post_target(state: Any, session_key: str) -> WorkRef | None:
    """The one Work this chat belongs to, or None when it has none or several.

    Registered issues are Work evidence already, so the projection is the whole rule.
    """
    works = state.work_projection().works_for_chat(state.chat_identity_key(session_key))
    return works[0] if len(works) == 1 else None


def shown_target(value: str) -> WorkRef | None:
    """The issue a ticked box said it would post to, as the form sent it."""
    ref = IssueRef.parse((value or "").strip())
    return WorkRef.from_issue(ref) if ref is not None else None


def box_value(target: WorkRef) -> str:
    return f"{target.repository}#{target.number}"


def issue_url(target: WorkRef) -> str:
    return f"https://github.com/{target.repository}/issues/{target.number}"


@dataclass(frozen=True)
class Outcome:
    """What became of one ticked message's post."""

    code: str
    target: WorkRef
    detail: str = ""
    url: str = ""
    action_id: str = ""

    @property
    def text(self) -> str:
        issue = f"#{self.target.number}"
        if self.code == DONE:
            return f"Posted to {issue}"
        if self.code == UNCERTAIN:
            return f"May have posted to {issue} — check the issue"
        if self.code == RELAYED:
            return f"Handed to the Poller, which posts it on {issue} on its next check"
        if self.code == HELD:
            return f"Handed to the Poller once the runtime is back; it posts it on {issue} then"
        reason = {
            UNKNOWN: "could not tell whether the Poller took it",
            RESTARTING: "the deck is restarting",
            UNRECORDED: "could not record the post",
        }.get(self.code, self.detail or "the post failed")
        return f"Not posted to {issue}: {reason}"

    @property
    def link(self) -> str:
        return self.url if self.code == DONE and self.url else issue_url(self.target)


def from_press(press: Any, target: WorkRef) -> Outcome:
    """The outcome a ledger row records. An unsettled row may have landed."""
    if press.outcome == DONE:
        return Outcome(DONE, target, url=press.result, action_id=press.action_id)
    if press.outcome == FAILED:
        return Outcome(FAILED, target, detail=press.result, action_id=press.action_id)
    return Outcome(UNCERTAIN, target, action_id=press.action_id)


def body(text: str, *, action_id: str, chat_url: str = "", attachments: int = 0) -> str:
    """The comment: a heading, the quoted words, and the id marker. No session key, no paths."""
    chat = f"[deck chat]({chat_url})" if chat_url else "deck chat"
    parts = [
        f"**From the {chat}:**",
        "",
        quote_operator_text(text, cut_note="the whole message is in the chat"),
    ]
    if attachments:
        plural = "s" if attachments != 1 else ""
        parts += ["", f"({attachments} attachment{plural} not posted)"]
    parts += ["", f"{MARKER}{action_id} -->"]
    return "\n".join(parts)


def reviewer_body(text: str, *, action_id: str) -> str:
    """A Claude or Codex reviewer's plain comment, as the deck posts it on the card (#2814).

    Quoted like a chat post, so no line of it reads as a control command or a lease marker.
    The skill's own trailing ``<!-- chatgpt-review -->`` line is the one exception: it is taken
    off before quoting and put back unquoted, so the card still collapses older review rounds.
    Only that fixed string is restored, so nothing typed can forge another marker.
    """
    lines = text.rstrip().splitlines()
    reviewed = bool(lines) and lines[-1].strip() == FINDINGS_MARKER
    if reviewed:
        text = "\n".join(lines[:-1])
    parts = [
        "**From AgentDeck's reviewer:**",
        "",
        quote_operator_text(text, cut_note="the whole comment is in the review chat"),
        "",
        f"{REVIEWER_MARKER}{action_id} -->",
    ]
    if reviewed:
        parts.append(FINDINGS_MARKER)
    return "\n".join(parts)


async def post(db: Any, *, action_id: str, target: WorkRef, comment: str) -> Outcome:
    """Write the reserved comment once and settle its row; the caller already reserved it.

    Runs under ``shutdown.protect`` as one unit, so a cancelled request still settles.
    """
    gh = shutil.which("gh")
    if gh is None:
        db.settle_work_action(action_id, FAILED, "gh is not available on this host")
        return Outcome(FAILED, target, "gh is not available on this host", action_id=action_id)
    argv = [gh, "issue", "comment", str(target.number), "--repo", target.repository,
            "--body-file", "-"]
    try:
        code, stdout, stderr = await gh_cli.run_once(
            argv, timeout=GH_TIMEOUT_S, stdin=comment.encode(), repo=target.repository
        )
    except gh_cli.GitHubRefusal as exc:
        # Refused before anything ran: nothing can have landed.
        db.settle_work_action(action_id, FAILED, str(exc))
        return Outcome(FAILED, target, str(exc), action_id=action_id)
    except Exception:  # noqa: BLE001 - a timeout or crash may have written
        log.exception("posting %s to %s#%s did not finish", action_id, target.repository,
                      target.number)
        return Outcome(UNCERTAIN, target, action_id=action_id)
    if code != 0:
        # A non-zero exit does not prove nothing landed, so the row stays unsettled.
        log.warning("gh issue comment for %s exited %s: %s", action_id, code,
                    stderr.decode(errors="replace").strip()[:500])
        return Outcome(UNCERTAIN, target, action_id=action_id)
    lines = stdout.decode(errors="replace").strip().splitlines()
    url = lines[-1].strip() if lines else ""
    db.settle_work_action(action_id, DONE, url)
    return Outcome(DONE, target, url=url, action_id=action_id)


def notice(db: Any, target: WorkRef | None, *, now: datetime | None = None) -> Outcome | None:
    """The chat page's standing notice: the newest post to this issue, if it went wrong lately.

    Never raises: a store that cannot be read means no notice, not a broken page.
    """
    if target is None or db is None:
        return None
    try:
        presses = db.load_issue_posts(target.work_id, 1)
    except Exception:  # noqa: BLE001 - the page must render without it
        log.exception("could not read issue posts for %s", target.work_id)
        return None
    if not presses or presses[0].outcome == DONE:
        return None
    press = presses[0]
    when = press.reserved
    if when is None or (now or datetime.now(UTC)) - when > NOTICE_MAX_AGE:
        return None
    return from_press(press, target)
