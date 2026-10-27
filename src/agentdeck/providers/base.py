"""SessionProvider ABC — the contract every session source implements.

Providers without a feature return sessions whose .capabilities simply omit
it (e.g. DEEPLINK) — the web layer keys off capabilities, never off provider
type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

from watchfiles import Change

from ..models import (
    CAPACITY_WINDOWS,
    Account,
    Capability,  # re-exported: providers/base is the canonical import site  # noqa: F401
    ForkFrom,
    InjectResult,
    PendingInteraction,
    Session,
    SessionStatus,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
    UsageSnapshot,
)

if TYPE_CHECKING:
    from ..events import EventBus
    from ..state import AppState


#: How many of a session's newest events the detail page renders at once.
#: Both file-backed providers define their own copy of this; keep them equal.
DETAIL_WINDOW = 400


def sum_token_totals(items) -> TokenTotals:
    """Field-wise sum of per-event token usage."""
    total = TokenTotals()
    for item in items:
        if item is None:
            continue
        total = TokenTotals(
            input_tokens=total.input_tokens + item.input_tokens,
            output_tokens=total.output_tokens + item.output_tokens,
            cache_read_tokens=total.cache_read_tokens + item.cache_read_tokens,
            cache_creation_tokens=total.cache_creation_tokens + item.cache_creation_tokens,
        )
    return total


#: The systemd unit that owns the persistent runtime, named in the split-brain
#: 404 so the reader is told the fix rather than left guessing at an outage.
RUNTIME_UNIT = "agentdeck-codex.service"
WEB_UNIT = "agentdeck.service"


def unknown_account_detail(provider_name: str, label: str, known: Iterable[str]) -> str:
    """404 detail for a runtime asked about an account label it does not have.

    The account list is built once per process (``app.py`` and ``Collector``),
    while the runtime addresses workers and bridges by ``{provider}/{label}``.
    Renaming a label in ``config.toml`` and restarting only one service splits
    that namespace, and a bare "unknown account" reads as a runtime outage — so
    the detail names the labels this runtime *does* know and the fix (#788)."""
    return (
        f"unknown {provider_name} account {label!r}; this runtime knows "
        f"{sorted(known)} — after renaming an account label, restart both "
        f"{WEB_UNIT} and {RUNTIME_UNIT}"
    )


def log_runtime_account_split(log, provider_name: str, account_key: str, exc: object) -> bool:
    """Warn once when the runtime answered 404 for this account's label.

    A refresh that fails because the runtime is *down* is ordinary and stays at
    DEBUG — the watch loop is its own retry. A 404 is different: the runtime is
    up and rejecting this label, which no amount of retrying fixes. Live that
    ran at ~1 Hz for 13 minutes with nothing above DEBUG to say so. Callers
    invoke this only on the ok→failed edge, so it is one line per split, not one
    per poll. Duck-typed on ``exc.response`` so ``base`` stays httpx-free."""
    response = getattr(exc, "response", None)
    if response is None or getattr(response, "status_code", None) != 404:
        return False
    log.warning(
        "%s runtime rejected account %s (renamed label? restart both %s and %s): %s",
        provider_name,
        account_key,
        WEB_UNIT,
        RUNTIME_UNIT,
        getattr(response, "text", ""),
    )
    return True


def recency_deadline(
    sessions: Iterable[Session], windows: Iterable[float], now: float
) -> float | None:
    """The earliest future moment a recency window lapses for a live chat (#2865).

    A sweep's clock transitions all read "N seconds since the last write/progress",
    so each live or working top-level chat contributes each window after each of its
    two anchors. A window that does not apply to a chat only costs one sweep that
    changes nothing; one left out would leave a card wrong until the next event.
    """
    windows = tuple(windows)
    earliest: float | None = None
    for session in sessions:
        if session.parent_session_key is not None:
            continue
        if session.status != SessionStatus.LIVE and not session.thinking:
            continue
        for anchor in (session.last_progress, session.last_activity):
            if anchor is None:
                continue
            base = anchor.timestamp()
            for window in windows:
                due = base + window
                if due > now and (earliest is None or due < earliest):
                    earliest = due
    return earliest


class ModelChoice(NamedTuple):
    """One selectable model for a provider's create/turn model picker.

    ``value`` is passed verbatim to the provider (``--model`` for Claude, the
    ``thread/start`` ``model`` param for Codex). An empty value never appears
    here — "account default" is rendered by the templates as a blank option.
    """

    value: str
    label: str


class EffortChoice(NamedTuple):
    """One reasoning-effort value offered by a provider."""

    value: str
    label: str


class NewSessionDefaults(NamedTuple):
    """Account-level defaults shown beside blank new-session picker values."""

    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class ContextObservation:
    """One provider-versioned context reading for an exact keyed worker leg."""

    worker_key: str
    source_session_id: str
    tokens: int
    observed_at: datetime
    source_version: int


class SessionProvider(ABC):
    provider_id: ClassVar[str]
    supports_new_session: ClassVar[bool] = False
    # Curated models the UI offers for this provider at new-chat creation (empty
    # = no picker). The web layer validates any submitted model against this list
    # before it reaches a CLI/app-server, so an unknown slug is a 422, never a raw
    # arg. The UI currently chooses models only at spawn; provider-specific turn
    # settings such as Codex effort use their own capability-gated controls.
    selectable_models: ClassVar[tuple[ModelChoice, ...]] = ()
    selectable_efforts: ClassVar[tuple[EffortChoice, ...]] = ()
    # The quota windows this provider's meter actually reports, in the order the
    # header draws them. It is the render's window set, not the calibration
    # engine's: a provider that meters a billing cycle instead of rolling windows
    # (#730) says so here rather than filling a 5h/7d row it will never
    # have, which is also what keeps the other providers from growing a third
    # permanently-empty bar on a phone screen.
    usage_windows: ClassVar[tuple[str, ...]] = CAPACITY_WINDOWS
    # Whether account-free machine delegation prefers this provider when several
    # accounts could serve it. At most one provider should claim this; the
    # delegation route falls back to "exactly one account" otherwise.
    machine_delegation_default: ClassVar[bool] = False

    @classmethod
    def is_valid_model(cls, model: str) -> bool:
        """True if ``model`` is one of this provider's offered slugs."""
        return any(choice.value == model for choice in cls.selectable_models)

    @classmethod
    def is_valid_effort(cls, effort: str) -> bool:
        """True if ``effort`` is one of this provider's offered values."""
        return any(choice.value == effort for choice in cls.selectable_efforts)

    @classmethod
    def user_message_refusal(cls, text: str) -> str | None:
        """Why this provider's CLI would not deliver ``text`` to the model, if so.

        A provider CLI can accept a message, record it in its own transcript, and
        still drop it from the request it builds — leaving the person looking at
        an answer to a question the model never saw. Where a provider has a known
        defect of that shape, it says so here and the send is refused up front
        with the reason, because a silently eaten message is strictly worse than
        a rejected one. Declared on the provider so no send path has to test a
        provider name; the default is that every message goes through.
        """
        return None

    def interactions_unknown(self, account: Account) -> bool:
        """Whether this account's live interactions cannot be read now (#3487).

        While the runtime restarts, ``pending_interaction`` answers ``None`` for every
        chat, an open question included. A caller that acts on "nothing is asked" (the
        Work pass asking an agent for its next step) checks this first. Default: never.
        """
        del account
        return False

    def runtime_unreachable(self, account: Account, session: Session) -> bool:
        """Whether this chat is the deck's own but its runtime cannot be reached now.

        A send refused only because the runtime is restarting can wait for it to
        come back (#2847); every other refusal is not fixed by waiting. The answer
        rests on the last ownership the deck saw, so a web process that has not yet
        reached the runtime at all says False rather than guessing.
        """
        del account, session
        return False

    def new_session_defaults(
        self, account: Account, *, model_override: str | None = None
    ) -> NewSessionDefaults:
        """Resolve account defaults used when model/effort form values stay blank.

        A working directory may add provider-native project configuration later;
        this projection deliberately describes the account setting named in the
        UI. The optional model override represents an AgentDeck-owned runtime
        setting that takes precedence over account-native configuration.
        """
        return NewSessionDefaults(model=model_override)

    @abstractmethod
    async def scan_sessions(self, account: Account) -> list[Session]: ...

    async def scan_changed_sessions(
        self,
        account: Account,
        changes: frozenset[tuple[Change, Path]],
        state: AppState,
    ) -> list[Session] | None:
        """Rebuild safely attributable existing sessions, or request a full scan.

        Providers opt into this only when every source change can be answered
        without reconsidering account membership or account-wide relationships.
        ``None`` preserves the authoritative ``scan_sessions`` fallback.
        """
        return None

    async def context_observations(
        self, account: Account, sessions: Iterable[Session]
    ) -> tuple[ContextObservation, ...]:
        """Current context readings whose keyed-worker mapping is exact."""
        return ()

    @abstractmethod
    def watch_paths(self, account: Account) -> list[Path]:  # for watchfiles (v0.2+)
        ...

    def watch_roots(self, account: Account) -> list[Path]:
        """Stable roots watched non-recursively for source topology changes."""
        return []

    def watch_root_change(self, account: Account, path: Path) -> bool:
        """Whether a direct child change requires rebuilding source watches."""
        return True

    @abstractmethod
    async def read_transcript(
        self, account: Account, session: Session, after_seq: int = 0
    ) -> list[TranscriptEvent]: ...

    @abstractmethod
    async def fetch_usage(self, account: Account) -> UsageSnapshot | None: ...

    # --- optional hooks (non-abstract) ---------------------------------

    def sweep_liveness(
        self, account: Account, sessions: list[Session], state: AppState
    ) -> list[Session]:
        """Cheap pid-liveness recheck. Apply the refresh through
        ``state.apply_session_changes`` so mutation and its ``"sessions"``
        notification stay one transition; returns the sessions that changed."""
        return []

    def next_liveness_deadline(
        self, account: Account, sessions: list[Session]
    ) -> float | None:
        """When ``sweep_liveness`` next has a clock to act on, as a Unix time (#2865).

        The sweep runs when a source or registry file changes, a watched process
        exits, or this deadline falls due; "thinking ends N s after the last write"
        is a deadline, not a reason to look every few seconds. None: nothing pends.
        """
        return None

    async def transcript_cursor(self, account: Account, session: Session) -> tuple[int, int]:
        """Current end-of-transcript cursor ``(byte_offset, seq)`` — the point a
        live tail should start from so it streams only subsequent events."""
        return (0, 0)

    async def tail_transcript(
        self, account: Account, session: Session, byte_offset: int, seq: int
    ) -> tuple[list[TranscriptEvent], int, int]:
        """Incremental read from a cursor → (new_events, byte_offset, seq)."""
        return ([], byte_offset, seq)

    async def last_event(self, account: Account, session: Session) -> TranscriptEvent | None:
        """The most recent renderable event (for activity detection); None if
        the provider has no transcript."""
        return None

    async def transcript_image(
        self, account: Account, session: Session, seq: int, image_index: int
    ) -> tuple[str, bytes] | None:
        """Return one transcript image as ``(media_type, bytes)``."""
        return None

    async def load_transcript(
        self, account: Account, session: Session, before_seq: int | None = None
    ) -> TranscriptDetail:
        """The windowed bundle the session detail page renders.

        This was an undocumented duck-typed requirement until a third provider
        arrived without it and the page raised `AttributeError` (#722): both
        `web/routes_pages.py` and `web/routes_partials.py` call it, but nothing
        declared it. The default builds the window from `read_transcript()`,
        which is enough for a provider whose events come from a runtime rather
        than a file. Providers with a transcript file override it to add
        cached metadata and a skip count.
        """
        events = await self.read_transcript(account, session)
        if before_seq is not None:
            events = [event for event in events if event.seq < before_seq]
        window = events[-DETAIL_WINDOW:]
        return TranscriptDetail(
            events=window,
            tokens=sum_token_totals(event.tokens for event in events),
            model=next(
                (event.model for event in reversed(events) if event.model), session.model
            ),
            todos=[],
            total_events=len(events),
            earliest_seq=window[0].seq if window else 0,
        )

    async def recent_conversation(
        self, account: Account, session: Session, limit: int = 4
    ) -> list[TranscriptEvent]:
        """Bounded recent user/assistant messages for lightweight summaries."""
        events = await self.read_transcript(account, session)
        return [event for event in events if event.role in ("user", "assistant")][-limit:]

    def make_usage_poller(
        self, account: Account, state: AppState, bus: EventBus, **kwargs: Any
    ) -> Any | None:
        """Return an object with an ``async run()`` loop, or None if the
        provider has no usage-limit source."""
        return None

    async def start_account(self, account: Account, state: AppState) -> None:
        """Start optional provider-owned runtime services for one account."""
        return None

    async def stop_account(self, account: Account) -> None:
        """Stop optional provider-owned runtime services for one account."""
        return None

    def can_start_session(self, account: Account) -> bool:
        """Whether this configured account can start sessions right now.

        Most providers have a static capability. Providers backed by optional
        runtime services can override this with account-scoped availability.
        """
        return self.supports_new_session

    def offers_new_session(self, account: Account) -> bool:
        """Whether a new-chat form lists this account, even if it cannot start one this second.

        A form is rendered once and never swapped, so a brief runtime outage at render time
        must not drop the account from it for good (#2975); pressing Start then says why it
        cannot start yet. Defaults to ``can_start_session``.
        """
        return self.can_start_session(account)

    def fork_refusal(self, account: Account, session: Session) -> str | None:
        """Why ``session`` cannot be branched into a side chat here, or None if it can.

        A side chat is a new session started as a copy of this one cut at a finished
        turn (#2485). Only a provider whose own runtime can make that copy says yes.
        """
        return "This kind of chat cannot be branched into a side chat."

    def fork_settings(self, account: Account, session: Session) -> tuple[str | None, str | None]:
        """The ``(model, effort)`` a side chat of ``session`` starts with.

        The provider's cache only matches a request that looks like the source's,
        so a side chat runs on the source's own model and effort when they are
        known. ``(None, None)`` leaves both to the provider.
        """
        return None, None

    def fork_ref(self, account: Account, session: Session, seq: int) -> str | None:
        """The provider's name for the cut after transcript line ``seq``, or None.

        None means that line does not end a finished turn this provider can cut at.
        """
        return None

    def rewind_refusal(self, account: Account, session: Session) -> str | None:
        """Why ``session`` cannot be rolled back to before one of its messages, or None.

        A rollback continues the same session from an earlier line (#3474). Only a
        provider whose own runtime can do that in place says yes.
        """
        return "This kind of chat cannot be rolled back."

    def rewind_floor(self, account: Account, session: Session) -> int:
        """The line of the first prompt, which a rollback cannot go before; 0 if unknown."""
        return 0

    def rewind_ref(self, account: Account, session: Session, seq: int) -> str | None:
        """The provider's name for the point before the prompt on line ``seq``, or None."""
        return None

    async def rewind(
        self,
        account: Account,
        session: Session,
        at: str,
        *,
        chat_keys: Iterable[str] = (),
        since: float | None = None,
    ) -> InjectResult:
        """Make the session's next turn continue from ``at`` (``rewind_ref``'s answer).

        With ``since``, the same call cancels, unannounced, the jobs any of ``chat_keys``
        submitted from then on: the agent no longer remembers asking for them.
        """
        return InjectResult(False, "this kind of chat cannot be rolled back")

    async def inject(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        timeout_s: float,
        images: list[Path] | None = None,
        effort: str | None = None,
        allow_over_budget: bool = False,
        require_idle: bool = False,
        expected_last_result_at: float | None = None,
    ) -> InjectResult:
        """Append one turn to a safely injectable session."""
        return InjectResult(False, "this provider does not support injection")

    async def worker_version(
        self, account: Account, session: Session
    ) -> float | None:
        """Durable keyed-worker result version for an optimistic idle write."""
        return None

    async def start_session(
        self,
        account: Account,
        cwd: Path,
        message: str,
        *,
        timeout_s: float,
        images: list[Path] | None = None,
        sandbox: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
        allow_over_budget: bool = False,
        delegated: bool = False,
        session_token: str | None = None,
        fork_from: ForkFrom | None = None,
        role: str | None = None,
    ) -> InjectResult:
        """Start one persisted session with its first turn.

        ``session_token`` is the deck's self-identification token for the
        session being started; providers pass it into the instructions they
        inject so the agent can resolve its own session key.

        ``fork_from`` starts it as a copy of another session's history up to a
        cut, never as an empty chat: a provider that cannot honour it refuses.
        """
        return InjectResult(False, "this provider cannot start sessions")

    async def wait_for_session(
        self,
        account: Account,
        session_id: str,
        *,
        timeout_s: float,
    ) -> InjectResult:
        """Wait for the active turn in a newly started session to finish."""
        return InjectResult(False, "this provider cannot wait for delegated sessions")

    async def session_result(self, account: Account, session_id: str) -> str | None:
        """Return the latest assistant text for a delegated session."""
        return None

    def pending_interaction(
        self, account: Account, session: Session
    ) -> PendingInteraction | None:
        """Return only a currently actionable, runtime-available interaction.

        The INTERACT capability gate lives here once; a provider supplies only
        the read via ``_actionable_interaction``. Callers never re-check the
        capability — a session without INTERACT resolves to None.
        """
        if Capability.INTERACT not in session.capabilities:
            return None
        return self._actionable_interaction(account, session.session_id)

    def _actionable_interaction(
        self, account: Account, session_id: str
    ) -> PendingInteraction | None:
        """Provider read of a still-actionable, runtime-available interaction.

        Default: none (the provider has no interaction source). Overriding this
        is all a provider needs to make ``pending_interaction`` work."""
        return None

    async def steer(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        images: list[Path] | None = None,
        allow_over_budget: bool = False,
    ) -> InjectResult:
        return InjectResult(False, "this provider cannot steer active turns")

    async def interrupt(self, account: Account, session: Session) -> InjectResult:
        return InjectResult(False, "this provider cannot interrupt active turns")

    def permission_prompts_allowed(
        self, account: Account, session: Session
    ) -> bool:
        return False

    async def set_permission_prompts(
        self, account: Account, session: Session, enabled: bool
    ) -> InjectResult:
        return InjectResult(
            False, "this provider does not support permission prompt overrides"
        )

    def bypass_permissions_on(self, account: Account, session: Session) -> bool:
        """Whether this chat runs with every permission check skipped (#2285)."""
        return False

    async def set_bypass_permissions(
        self, account: Account, session: Session, enabled: bool
    ) -> InjectResult:
        return InjectResult(False, "this provider cannot bypass permissions")

    def permission_prompts_next_turn(self, account: Account, session: Session) -> bool:
        """Whether a change only reaches the agent on its next turn.

        Providers that apply the switch to the turn already running say False,
        so the checkbox does not promise a delay that isn't there.
        """
        return False

    async def answer_interaction(
        self,
        account: Account,
        session: Session,
        interaction_id: str,
        *,
        answers: Mapping[str, list[str]],
        decision: str | None,
    ) -> InjectResult:
        """Admit an answer: confirm INTERACT is granted and that ``interaction_id``
        is still the actionable interaction, then hand a normalized answer to the
        provider. Providers implement only ``_answer_actionable``; the guard and
        the ``dict`` normalization live here once so the two providers cannot
        drift on the admission policy."""
        if Capability.INTERACT not in session.capabilities:
            return InjectResult(False, "interaction is unavailable")
        interaction = self._actionable_interaction(account, session.session_id)
        if interaction is None or interaction.id != interaction_id:
            return InjectResult(False, "interaction is no longer pending")
        return await self._answer_actionable(
            account, session, interaction, answers=dict(answers), decision=decision
        )

    async def _answer_actionable(
        self,
        account: Account,
        session: Session,
        interaction: PendingInteraction,
        *,
        answers: dict[str, list[str]],
        decision: str | None,
    ) -> InjectResult:
        """Hand a validated, ``dict``-normalized answer to the provider's runtime.

        Called only after ``answer_interaction`` confirmed the interaction is the
        actionable one; ``interaction.id`` equals the submitted id."""
        return InjectResult(False, "this provider cannot answer interactions")
