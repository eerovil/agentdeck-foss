"""Shared fragment rendering — used by both the HTMX partial routes and the
SSE stream, so a partial fetched over HTTP and one pushed over SSE are byte
-identical.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from fastapi.templating import Jinja2Templates

from .. import chat_notes, deck_notices, issue_latest_comment
from ..agent_review import is_reviewer_prompt
from ..attachments import display_message
from ..chat_board import ChatBoardService
from ..chat_transcript import chat_event_id
from ..deployment import is_deploy_checkout
from ..git_context import GitContext, PullRequestContext
from ..head_review import reviewable_commit
from ..inject import InjectionService, is_resendable, unsent_messages
from ..meta_chat import hide_brief
from ..models import (
    BACKGROUND_ACTIVITIES,
    CAPACITY_WINDOWS,
    MONTH,
    SEVEN_DAY,
    Account,
    Capability,
    IssueRef,
    PendingInteraction,
    Session,
    TranscriptEvent,
    detailed_activity_label,
)
from ..models import activity_label as activity_label  # single impl lives in models
from ..providers import PROVIDERS
from ..providers.claude_code.auth import looks_like_auth_failure
from ..state import AppState, SessionPresentation
from ..triage import (
    issue_is_closed,
    issue_is_merged,
    presented_attention_kind,
)
from ..usage_pace import WINDOW_LABELS, WINDOW_TITLES, elapsed_share, overspend_pace
from ..voice_mode import SHORT_ANSWER_LINE, UNHEARD_LINE, VOICE_LINE, VOICE_LINE_FI, hide_voice_line
from ..worker_limits import EXIT_MEMORY_CAP

if TYPE_CHECKING:
    pass

# The first paint carries at most this many non-actionable top-level chats. Active,
# waiting, selected, queued, and delegated-child-bearing chats are exceptions: they
# remain immediately reachable even when they are older than the history window.
SESSION_HISTORY_PAGE_SIZE = 40
# Cards do not need complete latest messages: the transcript owns that content.
# Bound each source field before HTML escaping so one unusually long turn cannot
# dominate the sidebar response on its own.
SESSION_CARD_PREVIEW_BYTES = 2 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def resolve_activity_label(
    *,
    has_question: bool,
    live: bool,
    streaming: bool,
    last_event: TranscriptEvent | None,
    age_s: float,
    has_working_subagent: bool,
    lifecycle_active: bool | None = None,
    recent_events: Sequence[TranscriptEvent] = (),
) -> str | None:
    """The session's activity badge text, or None when it should show nothing.

    One home for the pipeline the initial page render and the live SSE tail must
    agree on: a surfaced question suppresses the badge, then the open-turn label
    is derived and refined, and a still-blank label finally falls back to
    "Working" when a nested subagent is active. Each caller supplies its own
    ``streaming``/``age_s`` because they read different clocks (the sweep's
    ``thinking`` flag at page load, the SSE write-clock while tailing), and its
    own ``recent_events`` tail ending at ``last_event`` so a tail sitting on a
    tool result can still name the tool (#1457); only the combination logic is
    shared, so the two paths cannot drift apart.
    """
    if has_question:
        return None
    label = detailed_activity_label(
        activity_label(
            live,
            streaming,
            last_event,
            age_s,
            lifecycle_active=lifecycle_active,
        ),
        last_event,
        recent_events,
    )
    if label is None and has_working_subagent:
        return "Working"
    return label


def reltime(value: datetime | None) -> str:
    """Human 'resets in 2h 10m' / 'reset' rendering; '' when unknown."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = (value - _now()).total_seconds()
    if delta <= 0:
        return "now"
    mins = int(delta // 60)
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def reltime_ago(value: datetime | None) -> str:
    """Human 'past' rendering: '2h 10m ago' / 'just now'; '' when unknown."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = (_now() - value).total_seconds()
    if delta < 60:
        return "just now"
    mins = int(delta // 60)
    if mins < 60:
        return f"{mins}m ago"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def chat_time(value: datetime | None) -> str:
    """Compact local timestamp for transcript rows."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone()
    now = _now().astimezone()
    if local.date() == now.date():
        return local.strftime("%H:%M")
    if local.year == now.year:
        return local.strftime("%b %d, %H:%M")
    return local.strftime("%b %d, %Y %H:%M")


def chat_time_title(value: datetime | None) -> str:
    """Full local timestamp exposed by transcript time elements."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def ktok(value) -> str:
    """Compact token count: 940 -> '940', 1200 -> '1.2k', 47000 -> '47k'."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    if n < 1000:
        return str(n)
    k = n / 1000
    return f"{k:.1f}k" if k < 10 else f"{k:.0f}k"


# Context-size colour thresholds, referenced to the 1M-token (beta) window most
# sessions run on: amber past the halfway mark, red in the near-full zone where
# auto-compaction bites.
CTX_WARN_TOKENS = 500_000
CTX_CRIT_TOKENS = 800_000


def ctx_level(value) -> str:
    """Traffic-light class for a context size: '' (green) / 'warn' / 'crit' —
    same modifier vocabulary as the usage bars."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    if n >= CTX_CRIT_TOKENS:
        return "crit"
    if n >= CTX_WARN_TOKENS:
        return "warn"
    return ""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def register_filters(templates: Jinja2Templates) -> None:
    templates.env.filters["reltime"] = reltime
    # A keyed row's revision from its own markup (#3228): it changes exactly when the row does.
    templates.env.filters["digest"] = lambda text: hashlib.sha1(
        str(text).encode(), usedforsecurity=False
    ).hexdigest()[:12]
    # A link that opens an issue lands on its newest comment (#2424).
    templates.env.filters["issue_href"] = issue_latest_comment.issue_href
    templates.env.filters["reltime_ago"] = reltime_ago
    templates.env.filters["chat_time"] = chat_time
    templates.env.filters["chat_time_title"] = chat_time_title
    templates.env.filters["ktok"] = ktok
    templates.env.filters["ctx_level"] = ctx_level
    templates.env.filters["provider_word"] = provider_word
    templates.env.filters["tool_label"] = tool_label
    templates.env.filters["auth_failure"] = looks_like_auth_failure
    templates.env.filters["card_preview"] = card_preview
    templates.env.filters["display_message"] = display_chat_message
    templates.env.globals["voice_line"] = VOICE_LINE
    templates.env.globals["voice_line_fi"] = VOICE_LINE_FI
    templates.env.globals["voice_unheard_line"] = UNHEARD_LINE
    templates.env.globals["voice_short_answer_line"] = SHORT_ANSWER_LINE
    templates.env.filters["refusal"] = humanize_refusal
    templates.env.filters["resendable"] = is_resendable
    templates.env.filters["is_deploy_checkout"] = is_deploy_checkout


def display_chat_message(message: str | None) -> str:
    """A message as the chat view shows it: no attachment envelope, no voice-mode line,
    no deck chat brief (#3403)."""
    return hide_brief(hide_voice_line(display_message(message)))


def card_preview(value: str | None) -> str:
    """A valid-UTF-8 card preview bounded by source bytes, with truncation marked."""
    value = display_chat_message(value)
    if not value:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= SESSION_CARD_PREVIEW_BYTES:
        return value
    marker = "…"
    clipped = encoded[: SESSION_CARD_PREVIEW_BYTES - len(marker.encode("utf-8"))]
    return clipped.decode("utf-8", errors="ignore") + marker


def tool_label(value: str | None) -> str:
    """Short, readable names for provider-native tool identifiers."""
    name = (value or "tool").rsplit("__", 1)[-1].replace("_", " ").strip()
    return {
        "exec": "Shell",
        "exec command": "Shell",
        "apply patch": "Edit files",
        "wait": "Wait",
        "write stdin": "Command input",
        "view image": "View image",
    }.get(name.casefold(), name.title())


def _is_stale(snap, stale_after_s: float) -> bool:
    """Age-based staleness (issue #6): the displayed numbers are old, not just
    that the last poll attempt was rate-limited. Fresh data stays un-stale even
    when the endpoint is 429-ing."""
    if snap is None:
        return False
    return (_now() - snap.fetched_at).total_seconds() > stale_after_s


def _pace_cell(window: str, *, pct, resets, now: datetime) -> dict | None:
    """The projected-overspend flag for one window, worded for a tooltip (#1137).

    The reader needs to know why a percentage that is nowhere near full is being
    flagged, so the title carries the comparison itself — the meter, how far into
    the window it is, and what staying on pace would have looked like.
    """
    pace = overspend_pace(window, pct=pct, resets_at=resets, now=now)
    if pace is None:
        return None
    return {
        "level": pace.level,
        "title": (
            f"Projected overspend: {pace.pct:.0f}% of the {WINDOW_TITLES[window]} "
            f"window used {pace.part}/10 of the way through it — "
            f"on pace would be about {pace.on_pace_pct:.0f}%."
        ),
    }


def _elapsed_pct(window: str, *, resets, now: datetime) -> float | None:
    """The elapsed share of one window as a percentage, or ``None`` if unknown."""
    share = elapsed_share(window, resets_at=resets, now=now)
    return None if share is None else round(share * 100, 1)


def _row_pace(windows: list[dict]) -> dict | None:
    """The loudest projected-overspend flag across one account's windows.

    The collapsed header shows one figure per account, so its flag is the
    account's worst window rather than the one whose number happens to be on
    screen; the title names the window so the icon is never unattributed.
    """
    flagged = [w["pace"] for w in windows if w["pace"]]
    if not flagged:
        return None
    return max(flagged, key=lambda pace: pace["level"] == "crit")


def _window_row(snap, window: str, now: datetime) -> dict:
    """One quota window's meter, with how far through the window the clock is."""
    pct, resets = snap.window(window) if snap else (None, None)
    return {
        "kind": WINDOW_LABELS[window],
        "title": WINDOW_TITLES[window],
        "window": window,
        "pct": pct,
        "resets": resets,
        # Whether this meter is running ahead of the window it has to last (#1137).
        # None means on pace, which is the silent case: the row says nothing extra.
        "pace": _pace_cell(window, pct=pct, resets=resets, now=now),
        # How far through the window the clock is, drawn as a second layer in
        # the meter so usage can be read against time at a glance (#2181).
        "elapsed_pct": _elapsed_pct(window, resets=resets, now=now),
    }


def _account_windows(account: Account) -> tuple[str, ...]:
    """The quota windows this account's provider reports, per its own declaration.

    Provider-declared rather than global: a provider may meter a billing cycle
    instead of 5h/7d (#730), and a global window set would give every Claude and Codex
    account a third bar that can only ever read "—" — vertical space the sticky
    header does not have on a phone. An unregistered provider id falls back to the
    rolling 5h and 7d windows, which is what every file-backed provider reports.
    """
    provider = PROVIDERS.get(account.provider_id)
    return provider.usage_windows if provider is not None else CAPACITY_WINDOWS


# The budget windows a planned reset ends early (#3379); the 5h window is a rate limit.
PLANNED_RESET_WINDOWS = frozenset({SEVEN_DAY, MONTH})


def _usage_rows(
    accounts: list[Account], state: AppState, auth_watch=None
) -> list[dict]:
    stale_after = state.usage_stale_after_s
    if auth_watch is not None:
        # Asking is free here: reads happen in the background, and a render shows
        # whatever is already known (#298).
        auth_watch.request_refresh(accounts)
    now = _now()
    plans = state.db.planned_resets(now) if state.db is not None else {}
    rows = []
    for acc in accounts:
        snap = state.usage.get(acc.key)
        windows = [_window_row(snap, window, now) for window in _account_windows(acc)]
        planned = plans.get(acc.key)
        if planned is not None and planned > now:
            for row in windows:
                if row["window"] in PLANNED_RESET_WINDOWS:
                    # The person plans to reset this window, so burning it fast is the
                    # point, not a warning (#3379).
                    row["planned_reset"] = planned
                    row["pace"] = None
        rows.append(
            {
                "account": acc,
                "auth": auth_watch.status(acc.key) if auth_watch is not None else None,
                "auth_flow": auth_watch.flow(acc.key) if auth_watch is not None else None,
                "usage": snap,
                # Why this account has no meter, when the reason is a refused
                # credential rather than a poll that has not happened yet (#758).
                # ``state`` already dropped the snapshot, so this only supplies
                # the wording — the row is blank either way.
                "meter_credential_error": state.usage_credential_errors.get(acc.key),
                "stale": _is_stale(snap, stale_after),
                "stale_age": reltime_age(snap),
                # epoch seconds of the fetch + the stale threshold, so the client
                # ticks "updated Ns ago" and flips the stale badge every second
                # without waiting on a server re-render.
                "fetched_epoch": (snap.fetched_at.timestamp() if snap else ""),
                "stale_after": stale_after,
                "windows": windows,
                # The collapsed line's own flag: one icon per account, from the
                # worst-paced of its windows (#1137).
                "pace": _row_pace(windows),
            }
        )
    return rows


def reltime_age(snap) -> str:
    if snap is None:
        return ""
    return _fmt_age((_now() - snap.fetched_at).total_seconds())


def _fmt_age(delta: float) -> str:
    """Seconds-granular 'X ago' — matches the client-side ticker in base.html."""
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    mins = int(delta // 60)
    if mins < 60:
        return f"{mins}m ago"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def render_chat_board_rows(templates: Jinja2Templates, context: dict[str, object]) -> str:
    """One chat-board rows renderer for the page render and the SSE push alike (#1807)."""
    return templates.get_template("partials/chat_board_rows.html").render(**context)


def render_chat_board_freshness(templates: Jinja2Templates, context: dict[str, object]) -> str:
    """The same for the freshness line, which the hourly read moves on its own (#1872)."""
    return templates.get_template("partials/chat_board_freshness.html").render(**context)


def render_limit_bars(
    templates: Jinja2Templates,
    accounts: list[Account],
    state: AppState,
    auth_watch=None,
    *,
    active_account_key: str | None = None,
) -> str:
    return templates.get_template("partials/limit_bars.html").render(
        rows=_usage_rows(accounts, state, auth_watch),
        host=state.host_stats,
        active_account_key=active_account_key,
    )


def render_deployment_status(templates: Jinja2Templates) -> str:
    """The topbar deploy pill, for page load and the SSE push alike (#786).

    The template reads current state through the ``deployment_status`` template
    global, so this renderer takes nothing."""
    return templates.get_template("partials/deployment_status.html").render()


def render_job_queue(templates: Jinja2Templates) -> str:
    """The topbar job-queue pill, for page load and the SSE push alike (#1301).

    Like the deploy pill, the template reads current state through a global —
    here ``job_queue``, bound to the AppState the collector writes."""
    return templates.get_template("partials/job_queue.html").render()


def render_mac_runner_link(templates: Jinja2Templates) -> str:
    """The top bar's Mac-approvals icon, for page load and the SSE push alike (#2864)."""
    return templates.get_template("partials/mac_runner_link.html").render()


def assistant_insight_states(assistant) -> dict[str, str]:
    """Rendered Attention state without changing insight identity or persistence."""
    states: dict[str, str] = {}
    sessions = assistant.state.sessions
    for insight in assistant.visible_insights:
        context = assistant.contexts.get(insight.session_key)
        session = sessions.get(insight.session_key)
        states[insight.session_key] = presented_attention_kind(
            insight.kind,
            merged=(
                bool(context is not None and context.has_merged_pr)
                or bool(session is not None and issue_is_merged(session))
            ),
            issue_closed=bool(session is not None and issue_is_closed(session)),
        )
    return states


def render_assistant_session(templates: Jinja2Templates, assistant, session_key: str) -> str:
    """The chat page's Deckhand line: the repository, branch and dirty state."""
    return templates.get_template("partials/assistant_session_details.html").render(
        git_context=assistant.contexts.get(session_key),
    )


def session_labels(accounts: list[Account]) -> dict[str, str]:
    return {acc.key: acc.label for acc in accounts}


#: Between two accounts in a chain badge. One character, so a two-account chat
#: costs the card no more height than a one-account chat did.
CHAIN_SEPARATOR = "▸"


def provider_word(provider_id: str) -> str:
    """A provider id as a person would say it: ``claude_code`` → ``claude``."""
    return provider_id.removesuffix("_code").replace("_", " ")


def account_chain_label(account_keys: tuple[str, ...], labels: dict[str, str]) -> str:
    """The accounts a chat has run on, compactly, oldest first (#1267).

    Account labels are the operator's own words for their accounts, so they lead.
    They are only unique per provider, though: ``main`` on Claude and ``main`` on
    Codex are two accounts with one label, and a badge reading ``main▸main``
    would say a chat had gone nowhere. Where a label is ambiguous *inside this
    chain*, the provider is named instead — which is also the part that changed,
    and the only case where naming a provider earns the width it costs.
    """
    owners: dict[str, set[str]] = {}
    for key in account_keys:
        owners.setdefault(labels.get(key, key.split(":", 1)[-1]), set()).add(key)
    parts = []
    for key in account_keys:
        label = labels.get(key, key.split(":", 1)[-1])
        parts.append(provider_word(key.split(":", 1)[0]) if len(owners[label]) > 1 else label)
    return CHAIN_SEPARATOR.join(parts)


def session_queue_summaries(sessions, injector: InjectionService) -> dict[str, dict]:
    """Pending turn summaries rendered on cards after leaving a chat page."""
    summaries = {}
    for session in sessions:
        status = injector.status(session.key)
        if status is None:
            continue
        pending = [item for item in status.items if item.is_pending]
        if pending:
            summaries[session.key] = {
                "count": len(pending),
                "text": card_preview(pending[-1].text),
            }
    return summaries


def render_session_groups(
    templates: Jinja2Templates, context: dict[str, object]
) -> tuple[str, ...]:
    """Render each admitted card family once, for the chats page's single page load."""
    template = templates.get_template("partials/session_group.html")
    return tuple(template.render(**context, s=session) for session in context["sessions"])


# A refused start reaches the deck as the runtime's own token. Those are journal
# vocabulary, not sentences: "at_capacity" under a New chat form tells the person
# who pressed Start chat nothing about what happened or what to do (#559). Reasons
# the injector itself produces are already sentences and pass through unchanged.
START_FAILURE_MESSAGES = {
    "at_capacity": (
        "Every worker slot on this account is busy right now, so the chat was not "
        "started. Your message is still here — try again in a moment, or pick "
        "another account."
    ),
    "active_turn": "That chat is mid-turn, so the new one was not started.",
    "cwd_required": "The chat needs a working directory that exists on this host.",
    "worker_exited": "The worker exited before the chat started. Try again.",
    "codex_create_failed": "Codex would not open a thread for this chat. Try again.",
    "codex_start_failed": (
        "Codex opened a thread but would not take the first message. Try again."
    ),
}


# Why this chat's worker process died, when the runtime could name it (#1546).
# A worker killed by its own memory cap must not read as a mysterious stall, so
# it gets a sentence of its own rather than the generic "stopped responding".
WORKER_EXIT_MESSAGES = {
    EXIT_MEMORY_CAP: (
        "This chat's worker was using far more memory than a worker may, so it "
        "was stopped on its own — the rest of the deck kept running. Send "
        "another message to start it again."
    ),
}


def worker_exit_message(reason: str | None) -> str:
    """Human sentence for a named worker death, or "" for an ordinary one.

    An unmapped token is deliberately silent rather than humanized: a death the
    deck cannot explain in its own words is better left to the status the chat
    already shows than dressed up as an explanation.
    """
    return WORKER_EXIT_MESSAGES.get(reason or "", "")


def humanize_refusal(reason: str) -> str:
    """A refusal token as a sentence: ``cwd_required`` → ``Cwd required.``

    The floor under every refusal surface, so an unmapped or brand-new token still
    reads as language rather than as journal vocabulary.
    """
    sentence = reason.replace("_", " ").strip()
    if not sentence:
        return ""
    if sentence[-1] not in ".!?":
        sentence += "."
    return sentence[:1].upper() + sentence[1:]


def start_failure_message(reason: str | None) -> str:
    """Human sentence for a failed new/continue chat start.

    Start and send need their own words for the same token — "the chat was not
    started" is wrong under a composer inside an existing chat — so this maps the
    start surface only and falls back to the shared floor.
    """
    if not reason:
        return "Could not start the chat."
    return START_FAILURE_MESSAGES.get(reason) or humanize_refusal(reason)


def pending_injection_messages(status) -> list:
    """App-level turns not yet linked to a durable transcript event.

    The messages the chat's newest failure left unsent are kept too, as rows
    that say so and can be sent again (#2662) — otherwise a failed send took its
    text with it, because the composer is cleared as soon as a send is queued.
    """
    if status is None:
        return []
    unsent = {item.id for item in unsent_messages(status)}
    return [item for item in status.items if item.is_pending or item.id in unsent]


#: The values the chats page's Source filter takes: the classification a card's left
#: stripe is coloured by (``Session.worker_type``, with no type meaning "you").
CHAT_LIST_SOURCES = ("you", "kanban", "cloud")


@dataclass(frozen=True)
class ChatListQuery:
    """The chats page's filters, read from its own address (#2603).

    The address is the only place they live: a filtered list is a link that can be
    bookmarked. A plain ``/chats`` renders the whole list, and ``chats_page.js`` then
    reopens it with the filters this device last applied (#3367).
    """

    text: str = ""
    hide_closed: bool = False
    accounts: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    history_limit: int = SESSION_HISTORY_PAGE_SIZE

    @classmethod
    def from_params(cls, params) -> ChatListQuery:
        """Parse a request's query parameters; anything malformed is simply not a filter."""

        def many(name: str) -> tuple[str, ...]:
            values = params.getlist(name) if hasattr(params, "getlist") else ()
            return tuple(dict.fromkeys(value for value in values if value))

        try:
            history_limit = int(params.get("history_limit", SESSION_HISTORY_PAGE_SIZE))
        except (TypeError, ValueError):
            history_limit = SESSION_HISTORY_PAGE_SIZE
        return cls(
            text=(params.get("q") or "").strip(),
            hide_closed=params.get("closed") == "0",
            accounts=many("account"),
            projects=many("project"),
            sources=tuple(value for value in many("source") if value in CHAT_LIST_SOURCES),
            history_limit=max(SESSION_HISTORY_PAGE_SIZE, history_limit),
        )

    @property
    def panel_count(self) -> int:
        """How many of the collapsed panel's choices are in force."""
        return len(self.accounts) + len(self.projects) + len(self.sources)

    @property
    def narrowing(self) -> bool:
        """Whether anything beyond the default hides a chat."""
        return bool(self.text or self.hide_closed or self.panel_count)

    def params(self, **changes) -> list[tuple[str, str]]:
        """This query as ordered address parameters, with ``changes`` applied."""
        query = replace(self, **changes)
        pairs: list[tuple[str, str]] = []
        if query.text:
            pairs.append(("q", query.text))
        if query.hide_closed:
            pairs.append(("closed", "0"))
        pairs += [("account", value) for value in query.accounts]
        pairs += [("project", value) for value in query.projects]
        pairs += [("source", value) for value in query.sources]
        if query.history_limit != SESSION_HISTORY_PAGE_SIZE:
            pairs.append(("history_limit", str(query.history_limit)))
        return pairs

    def url(self, **changes) -> str:
        """The chats page at this query, with ``changes`` applied."""
        pairs = self.params(**changes)
        return "/chats" + (f"?{urlencode(pairs)}" if pairs else "")


def _chat_matches(
    session: Session,
    query: ChatListQuery,
    *,
    children: Sequence[Session],
    badge: str,
    deckhand_status: dict,
    work_item_pill: dict,
) -> bool:
    """Whether one top-level chat passes the chats page's filters.

    The rules are the ones the browser used to apply over the rendered cards: the text
    is one case-insensitive substring of the title, the latest messages, the working
    directory, the account badge and the sub-agent rows' titles.
    """
    if query.accounts and session.account_key not in query.accounts:
        return False
    if query.projects and (session.project_name or "") not in query.projects:
        return False
    if query.sources and (session.worker_type or "you") not in query.sources:
        return False
    pill = work_item_pill.get(session.key)
    if query.hide_closed and pill is not None and not pill.is_open:
        return False
    if not query.text:
        return True
    haystack = " ".join(
        (
            session.generated_title_subject or session.display_title or "",
            session.last_prompt or "",
            session.last_text or "",
            str(session.cwd or ""),
            badge,
            *(child.display_title or "" for child in children),
        )
    )
    return query.text.casefold() in haystack.casefold()


def _live_window_days(assistant) -> int:
    window = getattr(getattr(assistant, "state", None), "live_window", None)
    return round(window.window_s / 86400) if window is not None and window.enabled else 0


def session_list_context(
    accounts: list[Account],
    presentation: SessionPresentation,
    *,
    injector: InjectionService,
    assistant,
    query: ChatListQuery | None = None,
    work_chat_ids: Collection[str] = (),
) -> dict[str, object]:
    """Canonical template context for the chats page's list.

    ``work_chat_ids`` are the chats some Work owns (#2256). They are left out: the chat
    list is for chats outside every Work, and a Work's chats are reached from the Work.
    A deck-started reviewer's chat is left out the same way (#3101).
    ``query`` filters first and pages second (#2603), so Load older pages through the
    chats that match.
    """
    query = query or ChatListQuery()
    labels = session_labels(accounts)
    in_work = frozenset(work_chat_ids)
    # A reviewer's chat is left out too (#3101): they come by the hundred a day.
    outside_works = tuple(
        session
        for session in presentation.top_level
        if session.row_key not in in_work
        and session.key not in in_work
        and not is_reviewer_prompt(session.initial_prompt)
    )
    chain_labels = {
        session.key: account_chain_label(
            session.chat_account_keys or (session.account_key,), labels
        )
        for session in presentation.visible
    }
    deckhand_status = assistant.deckhand_statuses(presentation.visible, presentation)
    work_item_pill = assistant.work_item_pills(presentation.visible)
    queue_summaries = session_queue_summaries(presentation.visible, injector)
    top_level = tuple(
        session
        for session in outside_works
        if _chat_matches(
            session,
            query,
            children=presentation.children_of.get(session.key, ()),
            badge=chain_labels.get(session.key) or labels.get(session.account_key, ""),
            deckhand_status=deckhand_status,
            work_item_pill=work_item_pill,
        )
    )
    history_limit = query.history_limit
    actionable_keys = {
        session.key
        for session in top_level
        if session.thinking
        or session.is_waiting
        or Capability.INTERACT in session.capabilities
        or presentation.children_of.get(session.key)
    }
    actionable_keys.update(queue_summaries)
    actionable_keys.update(insight.session_key for insight in assistant.view.insights)

    history = tuple(
        session
        for session in top_level
        if session.key not in actionable_keys
    )
    admitted_history = {session.key for session in history[:history_limit]}
    sessions = tuple(
        session
        for session in top_level
        if session.key in actionable_keys or session.key in admitted_history
    )
    history_shown = min(history_limit, len(history))
    history_remaining = len(history) - history_shown
    history_next_limit = history_limit + min(SESSION_HISTORY_PAGE_SIZE, history_remaining)
    # Projects come from the whole list, not the filtered one, so choosing a project
    # never hides the others' choices; a chosen one with no chats right now stays
    # offered, or the list would be empty with nothing to unpick.
    projects = sorted(
        {session.project_name for session in outside_works if session.project_name}
        | set(query.projects)
    )
    return {
        "sessions": sessions,
        "children_of": presentation.children_of,
        "labels": labels,
        "chain_labels": chain_labels,
        "deckhand_status": deckhand_status,
        "work_item_pill": work_item_pill,
        "queue_summaries": queue_summaries,
        "chat_query": query,
        "chat_projects": projects,
        "chat_sources": CHAT_LIST_SOURCES,
        "history_limit": history_limit,
        "history_shown": history_shown,
        "history_remaining": history_remaining,
        "history_next_url": query.url(history_limit=history_next_limit),
        "history_load_count": min(SESSION_HISTORY_PAGE_SIZE, history_remaining),
        # Chats quiet for longer are not held, so not listed: search still finds them (#2841).
        "live_window_days": _live_window_days(assistant),
        "session_total_count": len(top_level)
        + sum(len(presentation.children_of.get(session.key, ())) for session in top_level),
    }


def render_transcript_events(
    templates: Jinja2Templates,
    events,
    *,
    session_key: str | None = None,
    leg_index: int = 0,
    observed_messages: dict | None = None,
    store=None,
    chat_id: str | None = None,
) -> str:
    """Render one leg's events with chat-level identities on every anchor.

    ``session_key`` and ``leg_index`` describe the leg these events came from,
    not the page: on a chained chat the same page renders several legs, and an
    anchor or an image URL that named the page's own leg would
    point at the wrong message (#1265).

    ``store`` holds the deck's delivery records to ``chat_id``, which tell a message
    the deck sent from a person's own; each one renders as a one-line marker (#3200).
    """
    tmpl = templates.get_template("partials/transcript_event.html")
    observed_messages = observed_messages or {}
    events = list(events)
    texts = deck_notices.candidate_texts(events)
    records = (
        store.deck_message_ids(chat_id, texts) if store is not None and chat_id and texts else {}
    )
    parts = []
    for e in events:
        kind = (
            deck_notices.notice_kind(e.text, records, e.ts)
            if e.role == "user" and not e.answer
            else None
        )
        parts.append(
            tmpl.render(
                e=e,
                session_key=session_key,
                event_id=chat_event_id(leg_index, e.seq),
                observed_message=observed_messages.get(e.seq),
                deck_label=deck_notices.label(e.text, kind) if kind else None,
            )
        )
    return "".join(parts)


def render_chat_notes(templates: Jinja2Templates, store, chat_id: str | None) -> str:
    """The chat's notes its agent never sees (#3227), for ``static/chat_notes.js`` to place."""
    if store is None or not chat_id:
        return ""
    notes = []
    for note in store.chat_notes(chat_id):
        try:
            at = datetime.fromisoformat(note.created_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        html = chat_notes.rendered_html(note.body_html)
        notes.append(
            {
                "comment_id": note.comment_id,
                "at": at,
                "version": f"{note.updated_at}{'+html' if html else ''}",
                "url": note.url,
                "item": f"{note.repository.rsplit('/', 1)[-1]}#{note.number}",
                "author": note.author,
                "body": note.body,
                "html": html,
            }
        )
    return templates.get_template("partials/chat_notes.html").render(notes=notes)


def render_leg_switch(templates: Jinja2Templates, switch) -> str:
    """The inline "this chat moved" seam, or nothing when there was no move."""
    if switch is None:
        return ""
    return templates.get_template("partials/leg_switch.html").render(switch=switch)


def render_chat_transcript_leg(
    templates: Jinja2Templates,
    leg,
    *,
    observed_messages: dict | None = None,
    store=None,
    chat_id: str | None = None,
) -> str:
    """One leg's window, followed by the marker for the chat leaving it."""
    html = render_transcript_events(
        templates,
        leg.detail.events,
        session_key=leg.session_key,
        leg_index=leg.index,
        observed_messages=observed_messages,
        store=store,
        chat_id=chat_id,
    )
    return html + render_leg_switch(templates, leg.switch)


def render_load_earlier(templates: Jinja2Templates, session_key: str, before) -> str:
    """The "load earlier" button, or nothing when the chat starts on screen."""
    if not before:
        return ""
    return templates.get_template("partials/load_earlier.html").render(
        session_key=session_key, before=before
    )


def render_chat_transcript(
    templates: Jinja2Templates,
    transcript,
    *,
    observed_messages: dict | None = None,
    store=None,
    chat_id: str | None = None,
) -> str:
    """The whole on-screen window of a chat: every loaded leg, oldest first.

    One renderer for the full page and for the "load earlier" fragment, so the
    two cannot drift on what a message's identity is.
    """
    html = render_load_earlier(
        templates, transcript.current.session_key, transcript.earlier_before
    )
    for leg in transcript.legs:
        html += render_chat_transcript_leg(
            templates,
            leg,
            # Only the current leg has an app-side queue to reconcile against;
            # a historical leg's colliding seq must never claim a pending row.
            observed_messages=observed_messages if leg.is_current else None,
            store=store,
            chat_id=chat_id,
        )
    return html


@dataclass(frozen=True)
class IssueChip:
    """One related-issue chip below the transcript, with its GitHub state.

    ``status`` is the badge text the session card already shows (``open`` /
    ``closed`` / ``merged``) and ``status_kind`` its CSS modifier. Both are None
    for an issue whose state the deck has not resolved — a registered issue that
    is not this chat's dispatched one has no cached lookup behind it, so the chip
    reads as it always did rather than guessing.
    """

    number: int
    url: str
    status: str | None = None
    status_kind: str | None = None


def session_issue_chips(session: Session) -> tuple[IssueChip, ...]:
    """This session's issue chips, carrying the states the deck already knows.

    The state comes from the same resolved badge as the session card
    (``issue_status``), so the bottom of the transcript and the row in the list
    never disagree (#1113). Unknown states stay absent rather than being guessed.
    """
    dispatched = IssueRef.parse(session.issue_url or "")
    chips = []
    for issue in session.issues:
        known = dispatched is not None and issue.identity == dispatched.identity
        chips.append(
            IssueChip(
                number=issue.number,
                url=issue.url,
                status=session.issue_status if known else None,
                status_kind=session.issue_status_kind if known else None,
            )
        )
    return tuple(chips)


def chat_board_service(app_state: object) -> ChatBoardService | None:
    """This deck's chat board, or None when it does not have a live one.

    A deck with `[chat_board]` off still builds the service — it is the switch's own home —
    but it never tails anything, so whatever is in its store is a leftover rather than a
    reading. Off therefore answers the same as absent, and the attribute is read defensively
    because a fragment can be rendered by a request that arrives before startup finished.
    """
    service = getattr(app_state, "chat_board", None)
    if not isinstance(service, ChatBoardService) or not service.enabled:
        return None
    return service


@dataclass(frozen=True)
class HeadReviewChip:
    """Whether this chat's current commit has been reviewed (#1127).

    Three states, and each one is a different control. ``unreviewed`` is a button
    that starts a review; ``reviewing`` links to the chat doing it; ``reviewed``
    shows the verdict a review reached for exactly this commit. Nothing here reads
    GitHub or shells out — the commit comes from the git context Deckhand already
    resolved and the verdict from the deck's own record — so the chip is a plain
    signature-gated fragment like the pull-request chips beside it.

    ``dirty`` is said out loud rather than hiding the chip: uncommitted changes
    mean the checkout is no longer only what the reviewed commit contains, and a
    reader who can see that decides what the verdict is still worth.
    """

    session_key: str
    repo: str
    sha: str
    short_sha: str
    state: str  # "unreviewed" | "reviewing" | "reviewed"
    dirty: bool = False
    verdict: str = ""
    review_session_key: str = ""

    @property
    def label(self) -> str:
        if self.state == "reviewing":
            return f"{self.short_sha} · reviewing…"
        if self.state == "reviewed":
            return f"{self.short_sha} · {self.verdict}"
        if self.dirty:
            return f"{self.short_sha} · commit first"
        return f"{self.short_sha} · review & fix?"

    @property
    def verdict_kind(self) -> str:
        """CSS modifier for a recorded verdict: passed, or anything else."""
        return "pass" if self.verdict == "pass" else "changes"


def session_head_review_chip(
    session: Session,
    context: GitContext | None,
    state: AppState,
    injector: InjectionService | None = None,
) -> HeadReviewChip | None:
    """The commit chip for one session, or None when there is no commit to show.

    Absent whenever the deck cannot name both the repository and the commit: a
    chat outside a checkout, a shared checkout sitting on another chat's branch,
    a directory git could not read. A chip that could not say *which* commit it
    means would be a button with no subject.

    ``reviewing`` needs the launched chat to still be running, which only the
    injector knows — so a review that failed to start, ended without reporting, or
    has aged out of the delegation records leaves a pressable button rather than a
    chip stuck on "reviewing…" forever.
    """
    commit = reviewable_commit(context)
    if commit is None or context is None:
        return None
    repo, sha = commit
    verdict = state.review_verdict(repo, sha)
    launch = state.head_review_launch(repo, sha)
    running = (
        injector.delegation_status(launch.delegation_id)
        if launch is not None and injector is not None
        else None
    )
    if verdict is not None:
        chip_state, review_session_key = "reviewed", ""
    elif running is not None and running.state in ("starting", "running"):
        chip_state, review_session_key = "reviewing", running.session_key or ""
    else:
        chip_state, review_session_key = "unreviewed", ""
    return HeadReviewChip(
        session_key=session.key,
        repo=repo,
        sha=sha,
        short_sha=context.short_head_sha or "",
        state=chip_state,
        dirty=context.dirty,
        verdict=verdict.verdict if verdict is not None else "",
        review_session_key=review_session_key,
    )


@dataclass(frozen=True)
class SessionView:
    """One session-detail tick, shared by initial paint and the live stream."""

    current: Session
    effective: Session
    busy: bool = False
    label: str | None = None
    age: float = 0.0
    active_child_sessions: tuple[Session, ...] = ()
    interaction: PendingInteraction | None = None
    issues: tuple[IssueChip, ...] = ()
    # Resolved GitHub state for this session's registered PRs. Deckhand owns the
    # lookup; the view carries only the result, so the chips stay a plain
    # signature-gated fragment like every other live piece of the page.
    pull_requests: tuple[PullRequestContext, ...] = ()
    # This chat's current commit and its review state, rendered beside the PR
    # chips (#1127). None when the deck cannot name the commit.
    head_review: HeadReviewChip | None = None

    @property
    def background_only(self) -> bool:
        """Shown working, but the chat's own turn has ended (#3529).

        Only work it left running keeps it going: a helper agent or command the
        provider names ("Background agent", "Background task"), or a subagent or
        queued job the deck counts as the chat's work while the chat itself is not
        thinking. Voice mode reads the reply then, rather than when that work ends.
        Busy with neither is not enough: a turn just started shows busy before the
        snapshot says the chat is thinking.
        """
        if not self.busy:
            return False
        if self.current.activity in BACKGROUND_ACTIVITIES:
            return True
        return not self.current.thinking and self.effective.thinking


@dataclass(frozen=True)
class LiveFragment:
    """One signature-gated session-detail fragment and its render contract."""

    name: str
    template: str
    signature: Callable[[SessionView], tuple[object, ...]]
    context: Callable[[SessionView], dict[str, object]]

    def changed(
        self, view: SessionView, last_signatures: dict[str, tuple[object, ...]]
    ) -> bool:
        signature = self.signature(view)
        if last_signatures.get(self.name, _MISSING_SIGNATURE) == signature:
            return False
        last_signatures[self.name] = signature
        return True

    def render(self, templates: Jinja2Templates, view: SessionView) -> str:
        return templates.get_template(self.template).render(**self.context(view))


_MISSING_SIGNATURE = object()

SESSION_FRAGMENTS = (
    LiveFragment(
        name="status",
        template="partials/session_status.html",
        signature=lambda view: (
            view.current.status,
            view.busy,
            view.background_only,
            view.current.subagent_count,
            # A rename changes nothing else about the chat, so without this the
            # header would keep the old name until some unrelated flip (#1569).
            view.effective.display_title,
        ),
        context=lambda view: {
            "s": replace(view.effective, thinking=view.busy),
            "background_only": view.background_only,
        },
    ),
    LiveFragment(
        name="subagents",
        template="partials/subagent_activity.html",
        signature=lambda view: (
            view.current.subagents,
            view.active_child_sessions,
        ),
        context=lambda view: {
            "s": view.current,
            "active_subagent_sessions": view.active_child_sessions,
        },
    ),
    LiveFragment(
        name="composer-controls",
        template="partials/composer_controls.html",
        signature=lambda view: (
            Capability.INTERRUPT in view.current.capabilities,
        ),
        context=lambda view: {
            "session_key": view.current.key,
            "can_interrupt": Capability.INTERRUPT in view.current.capabilities,
        },
    ),
    LiveFragment(
        name="tools",
        template="partials/tool_activity.html",
        signature=lambda view: (view.label,),
        context=lambda view: {
            "label": view.label,
            "elapsed_s": max(0, int(view.age)),
        },
    ),
    LiveFragment(
        name="related-links",
        template="partials/session_pull_requests.html",
        signature=lambda view: (
            view.issues,
            view.pull_requests,
            view.head_review,
        ),
        context=lambda view: {
            "session_key": view.current.key,
            "issues": view.issues,
            "pull_requests": fold_pull_requests(view.pull_requests)[0],
            "pull_request_fold": fold_pull_requests(view.pull_requests)[1],
            "head_review": view.head_review,
        },
    ),
    LiveFragment(
        name="interaction",
        template="partials/pending_interaction.html",
        signature=lambda view: (
            view.interaction.id if view.interaction is not None else None,
        ),
        context=lambda view: {
            "session_key": view.current.key,
            "interaction": view.interaction,
        },
    ),
)

SESSION_FRAGMENTS_BY_NAME = {fragment.name: fragment for fragment in SESSION_FRAGMENTS}


def session_pull_request_chips(context: GitContext | None) -> tuple[PullRequestContext, ...]:
    """The PR chips for one session — empty until Deckhand has resolved a context."""
    return context.pull_requests if context is not None else ()


@dataclass(frozen=True)
class PullRequestFold:
    """Finished pull requests folded behind one chip (#2711).

    ``status`` is the chip's colour modifier: ``merged`` or ``closed`` when they all are,
    ``done`` for a mix.
    """

    pulls: tuple[PullRequestContext, ...]
    status: str

    @property
    def label(self) -> str:
        return f"{len(self.pulls)} PRs {self.status}"


def fold_pull_requests(
    pulls: Sequence[PullRequestContext],
) -> tuple[tuple[PullRequestContext, ...], PullRequestFold | None]:
    """The PR chips shown one by one, and the finished ones folded behind one chip (#2711).

    A Work that went through three pull requests had three merged chips, and nothing left to
    do with any of them. Two or more merged or closed ones become one chip that opens to show
    them; one stays as it was, because folding it saves nothing. Open and draft ones are
    never folded.
    """
    finished = tuple(pull for pull in pulls if pull.is_terminal)
    if len(finished) < 2:
        return tuple(pulls), None
    statuses = {pull.status for pull in finished}
    status = statuses.pop() if len(statuses) == 1 else "done"
    shown = tuple(pull for pull in pulls if not pull.is_terminal)
    return shown, PullRequestFold(finished, status)


def render_session_fragments(
    templates: Jinja2Templates, view: SessionView
) -> dict[str, str]:
    return {
        fragment.name: fragment.render(templates, view)
        for fragment in SESSION_FRAGMENTS
    }


def board_strip_context(request, state: AppState) -> dict:
    """The front page's board strip, from cached reads only (#1007).

    Nothing here calls GitHub: the cards come from the engine's own state files
    and the issue titles from the shared metadata cache, so a dashboard render
    never spends quota. A cache the deck could not fill leaves the strip
    incomplete, which the bar says out loud rather than showing a bare zero.
    """
    from .. import board_strip, issue_context

    backlog = request.app.state.issues.cached()
    view = issue_context.join_pending(
        backlog,
        state.all_sessions(),
        request.app.state.assistant.contexts,
        attention_report_for=state.attention_report_for,
    )
    # A row's chat link publishes the durable `/chats/<id>` address only when
    # that id will still resolve after a web restart, and falls back to the leg
    # page otherwise (#1694). One chat snapshot for the whole strip: reading
    # chat identity a session at a time is what `chat_snapshot` exists to avoid.
    durable = state.chat_identity_is_durable
    chats = state.chat_snapshot() if durable else None

    def session_path(session_key: str) -> str:
        if not durable:
            return f"/sessions/{session_key}"
        chat = state.chat_for_session(session_key, chats)
        return f"/chats/{chat.chat_id}" if chat else f"/sessions/{session_key}"

    return {
        "board_strip": board_strip.build_strip(
            view, backlog.board.scopes, session_path=session_path
        )
    }


def poller_context(
    request,
    *,
    settings_errors: dict | None = None,
    boards: dict | None = None,
) -> dict:
    """The Poller Instance views this host can see, plus what a control may ask for (#182)."""
    from .. import poller_ops, poller_settings

    config = request.app.state.config
    settings = getattr(config, "poller", None)
    root = getattr(settings, "root", "~/.local/share/agentdeck/poller")
    default_interval = poller_ops.DEFAULT_INTERVAL_S
    interval = int(getattr(settings, "interval_s", default_interval) or default_interval)
    instances = poller_ops.read_all(root, interval_s=interval)
    if boards is None:
        boards = request.app.state.issues.cached_instance_boards()
    return {
        "instances": instances,
        "boards": boards,
        # The one Overlay setting the deck can change: which account this project's workers run
        # on, and where the work goes when it fills up (#901). Read fresh, because the file is
        # equally editable by hand.
        "worker_accounts": {
            view.instance: poller_settings.read_worker_accounts(root, view.instance)
            for view in instances
        },
        "settings_errors": settings_errors or {},
        "poller_root": root,
        # The Builder launch offers the operator's own accounts, in their order (#181).
        "accounts": list(request.app.state.accounts or []),
        # Builder runs in flight, and whether each can be handed on right now (#268).
        "builder_runs": builder_run_views(request, root),
    }


@dataclass(frozen=True)
class BuilderRunView:
    """One Builder run as the operator sees it: who holds it, and what a press would do."""

    run: object
    label: str                # the account holding it, in the operator's own words
    session_key: str = ""     # the leg's chat, once the provider has answered
    working: bool = False     # its turn is still running, so handover would preempt it
    successor: str = ""       # who a press would hand it to, or nothing when none is left

    @property
    def can_hand_over(self) -> bool:
        return bool(self.successor) and not self.working


def builder_run_views(request, root: str) -> list[BuilderRunView]:
    """Resolve each recorded run against what the deck can see right now (#268).

    Nothing here is stored: which chat a leg became, whether it is mid-turn, and who has usage
    left are all live facts, and a run that cached them would go stale the moment it was written.
    """
    from .. import poller_builder

    state = getattr(request.app.state, "app_state", None)
    accounts = {account.key: account.label for account in (request.app.state.accounts or [])}
    views = []
    for run in poller_builder.read_runs(root):
        leg = run.leg
        token = leg.session_token if leg else ""
        session_key = ""
        working = False
        successor = ""
        if state is not None:
            session_key = (state.session_key_for_token(token) or "") if token else ""
            session = state.sessions.get(session_key) if session_key else None
            working = bool(session is not None and session.thinking)
            successor = run.next_account({key: state.usage.get(key) for key in run.order})
        views.append(
            BuilderRunView(
                run=run,
                label=accounts.get(run.account_key, run.account_key),
                session_key=session_key,
                working=working,
                successor=successor,
            )
        )
    return views
