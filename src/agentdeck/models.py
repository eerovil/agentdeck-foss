"""Provider-neutral data model.

The web layer imports only this module (and the PROVIDERS registry);
providers translate their native session sources into these types.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic, time

_ISSUE_URL_RE = re.compile(
    r"https://github\.com/[^/]+/([^/]+)/(?:issues|pull)/(\d+)"
)
_PULL_URL_RE = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?$",
    re.IGNORECASE,
)
_PULL_QUALIFIED_RE = re.compile(
    r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)$"
)
_ISSUE_REF_URL_RE = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)/?$",
    re.IGNORECASE,
)
_ISSUE_QUALIFIED_RE = re.compile(
    r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)$"
)

_UNATTRIBUTABLE_CWD_ROOTS = frozenset({Path("/tmp"), Path("/var/tmp")})

# --- worker snapshot capabilities ----------------------------------------
#
# What a runtime says its worker snapshot is able to report, listed under
# ``SNAPSHOT_CAPABILITIES_KEY`` at the top level of the snapshot beside ``workers``.
#
# The web process and the persistent runtime are deployed apart on purpose: the web side
# restarts on every deploy while the runtime keeps every owned worker alive across it. A
# reader running today's code therefore routinely reads a snapshot built by a runtime from
# before a field existed — and a missing field then means two incompatible things: this one
# record predates it, or nothing in this runtime can ever report it. The list is how the
# runtime says which, and a snapshot carrying no list at all is the older runtime (#1407).
SNAPSHOT_CAPABILITIES_KEY = "snapshot_capabilities"
# Worker records carry ``last_activity_at``: when that worker last received input or
# produced any output. Without it there is no way to measure silence at all, so a reader
# that treats its absence as silence would revive a working worker (#1400, #1402).
SNAPSHOT_ACTIVITY_CLOCK = "activity_clock"
SNAPSHOT_CONTEXT_MANAGEMENT_V1 = "context_management_v1"
# The runtime can start a new session as a copy of another one cut at a chosen point
# (#2485). A web process sending a fork to a runtime without it would get a fresh chat
# with none of the history it asked for, so the deck offers a side chat only when the
# runtime says it can make one.
SNAPSHOT_FORK_V1 = "fork_v1"
# The runtime can roll an idle chat back to before one of its messages, in place (#3474):
# it ends the process and resumes the next turn at the line before that message. A web
# process asking a runtime without it would hide the messages while the agent kept them.
SNAPSHOT_REWIND_V1 = "rewind_v1"
# The runtime resumes an idle chat whose working directory is gone in the repository that
# held it, keeping the same conversation (#2524). A web process sending into such a chat
# through a runtime without it would wake a CLI in a directory that no longer exists and
# never hear back, so the deck lets the send through only when the runtime says it can.
SNAPSHOT_RELOCATE_V1 = "relocate_v1"
# The runtime keeps a booted Claude CLI waiting for a new chat when asked (#3420), so the
# web asks only a runtime that says it can.
SNAPSHOT_SPARE_V1 = "spare_v1"
# The snapshot carries ``SNAPSHOT_BOOTED_AT_KEY``: when the runtime process serving it
# started. A restart replaces every owned worker process at once while the fresh runtime
# wakes the chats that had a turn in flight, so without this a reader cannot tell that shape
# apart from an OOM — and calls a live worker dead (#1659).
SNAPSHOT_RUNTIME_BOOT = "runtime_boot"
# Epoch seconds, matching every other clock in the snapshot, at the top level beside
# ``workers``. Per runtime process rather than per worker: it answers "did this runtime
# restart", which is not a question about any one worker.
SNAPSHOT_BOOTED_AT_KEY = "runtime_booted_at"
# What a runtime built from this source publishes. Each provider names it explicitly in its
# own snapshot rather than inheriting it, because a provider that cannot keep one of these
# facts has to be able to say so.
# Each worker names the ``work_id``/``run_id`` of its most recent correlated delivery still on
# record (#2142), so a reader can tell "no correlation" from a runtime that cannot publish one.
SNAPSHOT_RUN_CORRELATION = "run_correlation"
WORKER_SNAPSHOT_CAPABILITIES = (
    SNAPSHOT_ACTIVITY_CLOCK,
    SNAPSHOT_RUNTIME_BOOT,
    SNAPSHOT_RUN_CORRELATION,
)
# When this process started, near enough: every worker host in a runtime shares it, because
# the fact being reported is the runtime's own life and not any one host's. Read at import
# rather than per snapshot, so a restart moves it and nothing else does.
RUNTIME_BOOTED_AT = time()


@dataclass(frozen=True)
class SessionProject:
    """One Session Project resolution together with the strength of its evidence.

    ``authoritative`` marks a name that came from something which actually names
    a repository: a registered issue URL, a checkout carrying a ``.git`` marker,
    or a ``.worktrees`` layout. The bare directory-name fallback is deliberately
    *not* authoritative — a scratch directory's name is a label, not a project,
    and only authoritative names are worth remembering past the evidence that
    produced them (#689).
    """

    name: str | None
    authoritative: bool = False


_UNRESOLVED_PROJECT = SessionProject(None, False)


#: How long a resolved project is trusted before its directory is walked again.
#: Long enough that the refresh loops never pay for the walk, short enough that
#: `git init` in a directory a chat is already sitting in shows up on the card
#: within a minute.
_PROJECT_CACHE_TTL_S = 60.0
#: Cwds remembered at once. One per directory any chat has ever run in, so this
#: is generous; the bound only exists so a long-lived worker cannot grow it
#: without limit.
_PROJECT_CACHE_MAX = 4096
_project_cache: dict[Path, tuple[float, SessionProject]] = {}


def reset_session_project_cache() -> None:
    """Forget every remembered cwd resolution. For tests that move directories."""
    _project_cache.clear()


def resolve_session_project(cwd: Path | None) -> SessionProject:
    """Resolve the repository-scale Session Project from cwd evidence.

    Memoized per cwd. This is a pure function of a directory layout, and it
    walks every parent of the cwd stat-ing for a ``.git`` marker — a real
    syscall walk per session, and the refresh loops ask it for every session
    several times a second, so an idle deck paid for that walk continuously
    (#1303). The answer is re-derived after ``_PROJECT_CACHE_TTL_S`` so a
    checkout that gains or loses its marker is not remembered wrong forever.
    """
    if cwd is None or cwd in _UNATTRIBUTABLE_CWD_ROOTS:
        return _UNRESOLVED_PROJECT
    now = monotonic()
    cached = _project_cache.get(cwd)
    if cached is not None and now - cached[0] < _PROJECT_CACHE_TTL_S:
        return cached[1]
    resolved = _resolve_session_project_uncached(cwd)
    # Re-insert at the end so the eviction below drops the least recently
    # resolved cwd rather than an actively used one.
    _project_cache.pop(cwd, None)
    _project_cache[cwd] = (now, resolved)
    while len(_project_cache) > _PROJECT_CACHE_MAX:
        _project_cache.pop(next(iter(_project_cache)), None)
    return resolved


def _resolve_session_project_uncached(cwd: Path) -> SessionProject:
    # A scratch checkout under /tmp may well carry a .git marker, but its
    # directory name still names nobody's repository — remembering it as
    # authoritative filed spend under projects that do not exist (#671, #790).
    # The name is still the best label for the session itself, so it is kept,
    # downgraded to the same strength as the bare directory-name fallback.
    throwaway = any(root in cwd.parents for root in _UNATTRIBUTABLE_CWD_ROOTS)
    for candidate in (cwd, *cwd.parents):
        if candidate in _UNATTRIBUTABLE_CWD_ROOTS:
            # Nothing above a temporary root is evidence either (a stray
            # /tmp/.git exists in the wild); stop walking.
            break
        if candidate.parent.name == ".worktrees":
            return _repository_project(candidate.parent.parent.name, weak=throwaway)
        if (candidate / ".git").exists():
            return _repository_project(candidate.name, weak=throwaway)
    return SessionProject(cwd.name or None, False)


def _repository_project(name: str, *, weak: bool = False) -> SessionProject:
    return SessionProject(name, not weak) if name else _UNRESOLVED_PROJECT


def session_project_from_cwd(cwd: Path | None) -> str | None:
    """Derive the repository-scale Session Project from transcript cwd evidence."""
    return resolve_session_project(cwd).name


@dataclass(frozen=True, order=True)
class PullRequestRef:
    """One pull request a session declares as its own work.

    Only the identity is stored: title, state, draft and merge status are read
    live from GitHub, so a registration made when a PR was opened still reads
    ``merged`` later without anyone re-registering it.
    """

    repository: str  # "owner/repo"
    number: int
    # The Work this pull request was registered for (#3392), as a ``work_id``; "" when the
    # chat named none. Not part of the identity: one pull request is one registration.
    work_id: str = field(default="", compare=False)

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repository}/pull/{self.number}"

    @property
    def identity(self) -> tuple[str, int]:
        """Case-insensitive identity — GitHub repository names are not case-sensitive."""
        return (self.repository.lower(), self.number)

    @classmethod
    def parse(cls, value: str) -> PullRequestRef | None:
        """Accept a full PR URL or ``owner/repo#123``; reject anything else.

        A bare ``#123`` is deliberately unsupported: guessing which repository a
        loose number belongs to is exactly the ambiguity this registry replaces.
        """
        text = value.strip()
        match = _PULL_URL_RE.match(text) or _PULL_QUALIFIED_RE.match(text)
        if match is None:
            return None
        return cls(f"{match.group(1)}/{match.group(2)}", int(match.group(3)))


@dataclass(frozen=True, order=True)
class IssueRef:
    """One GitHub issue a session declares as related work."""

    repository: str  # "owner/repo"
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repository}/issues/{self.number}"

    @property
    def identity(self) -> tuple[str, int]:
        return (self.repository.lower(), self.number)

    @classmethod
    def parse(cls, value: str) -> IssueRef | None:
        """Accept a full issue URL or ``owner/repo#123``."""
        text = value.strip()
        match = _ISSUE_REF_URL_RE.match(text) or _ISSUE_QUALIFIED_RE.match(text)
        if match is None:
            return None
        return cls(f"{match.group(1)}/{match.group(2)}", int(match.group(3)))


class SessionStatus(StrEnum):
    LIVE = "live"  # owning process running locally — read-only + deep-link
    IDLE = "idle"  # transcript exists, no live pid — read-only
    REMOTE = "remote"  # cloud-only, no local transcript — deep-link only


class Capability(StrEnum):
    TRANSCRIPT = "transcript"
    INJECT = "inject"
    REVIVE = "revive"
    DEEPLINK = "deeplink"
    STEER = "steer"
    INTERRUPT = "interrupt"
    INTERACT = "interact"
    EFFORT = "effort"
    PERMISSION_PROMPTS = "permission_prompts"
    BYPASS_PERMISSIONS = "bypass_permissions"
    CONTEXT_COMPACT = "context_compact"
    CONTEXT_ROLLOVER = "context_rollover"
    # The owning runtime resumes this chat in its repository when its own directory is
    # gone (#2524), so a gone directory does not stop a person answering it.
    RESUME_ELSEWHERE = "resume_elsewhere"


# The control capabilities a deck-owned runtime agent can grant. Read-only
# affordances (TRANSCRIPT, DEEPLINK) are derived separately from the transcript
# and are never part of this set. Providers strip this whole set before
# reapplying the live projection so a stale control capability cannot linger.
CONTROL_CAPABILITIES = frozenset(
    {
        Capability.INJECT,
        Capability.REVIVE,
        Capability.STEER,
        Capability.INTERRUPT,
        Capability.INTERACT,
        Capability.PERMISSION_PROMPTS,
        Capability.BYPASS_PERMISSIONS,
        Capability.CONTEXT_COMPACT,
        Capability.CONTEXT_ROLLOVER,
        Capability.RESUME_ELSEWHERE,
    }
)


def runtime_control_capabilities(
    *, available: bool, active_turn: bool, actionable_interaction: bool
) -> frozenset[Capability]:
    """Control capabilities an owned runtime agent grants right now.

    This is the single home for the "what does ownership grant" policy that both
    providers apply on top of their own read-only capabilities. It is a pure
    projection of three facts each provider reads its own way:

    - ``available`` — the owning runtime is reachable and owns this session;
      without it no control capability is granted (an idle owned worker whose
      runtime is unreachable is read-only).
    - ``active_turn`` — a turn is in flight, so it can be steered or interrupted.
    - ``actionable_interaction`` — a pending interaction is currently answerable.

    Ownership grants INJECT (queue the next turn) and REVIVE (start a replacement
    turn through the deck's steering contract); an active turn adds STEER
    and INTERRUPT; an actionable interaction adds INTERACT.
    """
    if not available:
        return frozenset()
    capabilities = {Capability.INJECT, Capability.REVIVE}
    if active_turn:
        capabilities |= {Capability.STEER, Capability.INTERRUPT}
    if actionable_interaction:
        capabilities.add(Capability.INTERACT)
    return frozenset(capabilities)


def runtime_turn_state(
    *, active_turn: bool, actionable_interaction: bool, stalled_evidence: bool
) -> tuple[bool, bool]:
    """Turn-presentation state an owned runtime agent projects right now.

    Sibling of ``runtime_control_capabilities``: the single home for the "how
    does ownership project turn state" precedence rule that both providers apply
    on top of their own read of the raw facts. An actionable interaction outranks
    stall evidence and suppresses thinking; a session is only thinking when a turn
    is active, no interaction is pending, and it is not stalled.

    Returns ``(stalled, thinking)`` — stalled first, because thinking is derived
    from the overridden stalled value, not the raw evidence.
    """
    stalled = False if actionable_interaction else stalled_evidence
    thinking = active_turn and not actionable_interaction and not stalled
    return stalled, thinking


@dataclass(frozen=True)
class Account:
    key: str  # "claude_code:main" — provider_id ":" label-slug
    provider_id: str
    label: str  # from config: "main", "alt"
    root: Path  # CLAUDE_CONFIG_DIR for claude_code


@dataclass(frozen=True)
class GeneratedTitle:
    """A persisted semantic display title for one provider session."""

    title: str
    evidence_signature: str
    updated_at: datetime
    # "deckhand" (generated) or "agent" (the session set its own title through
    # the local API). An agent-authored title is never regenerated.
    source: str = "deckhand"


@dataclass(frozen=True)
class AttentionReport:
    """A session's durable attention state for its current user turn."""

    state: str  # "waiting" | "finished"
    updated_at: datetime
    detail: str = ""


@dataclass(frozen=True)
class PollerReviewVerdict:
    """What one poller merge review decided about one commit (#1122).

    ``repo`` and ``sha`` name the reviewed pull request's head commit and are the key: a
    verdict is about a commit, and re-reviewing the same commit replaces this record rather
    than adding another. ``verdict`` is what the reviewer reported (``pass`` or
    ``changes-needed``); ``result`` is what the release progression then did with it
    (``release`` or ``handback``, and ``none`` for a review nothing acted on — one
    started from a session's commit chip, #1127). ``auto`` is read only from records written
    before #2502, where it marked a review the retired automerge label started.
    """

    repo: str
    sha: str
    verdict: str
    result: str
    recorded_at: datetime
    pull_request: str = ""
    issue_repo: str = ""
    issue_number: int = 0
    auto: bool = False
    generation: str = ""
    reviewed_at: datetime | None = None


@dataclass(frozen=True)
class HeadReviewLaunch:
    """A review this deck started for one commit from a session's chip (#1127).

    In-memory only, and deliberately so: it says "a review is running right now",
    which a web restart cannot know is still true. Forgetting it makes the chip
    pressable again, which is the safe direction — the verdict itself is the
    durable half, and it is keyed by commit, so a second review of the same
    commit replaces rather than duplicates the answer.

    It holds the delegation id rather than a session key because the chat does not
    exist yet when the button answers: the provider names it a moment later, and
    the delegation's own status is where that name and its liveness both live.
    """

    repo: str
    sha: str
    delegation_id: str
    started_at: datetime

    @property
    def key(self) -> tuple[str, str]:
        """The commit this review is about, spelled the way the store keys it."""
        return (self.repo.lower(), self.sha)


# Mute lease sources and their lifetimes (#823). The boss pressing the button is
# an instruction and gets a long renewable lease with no cap; a lease the deck
# takes on its own is a guess and gets a shorter one plus a hard total cap, so it
# has to re-earn itself. Both still expire — fail-open is not negotiable.
MUTE_SOURCE_BOSS = "boss"


@dataclass(frozen=True)
class MuteLeasePolicy:
    """How long one kind of claim on a chat lasts, and whether it may repeat.

    One entry per source, so adding a third is a row in ``MUTE_LEASE_POLICIES``
    rather than an edit in three files. ``hard_cap_seconds`` of ``None`` means
    renewable indefinitely.

    ``precedence`` orders the sources against each other: a grant never
    downgrades a lease a stronger source holds — the chat stays muted under the
    stronger claim rather than silently inheriting the weaker one's lifetime
    and cap.
    """

    source: str
    lease_seconds: float
    hard_cap_seconds: float | None
    precedence: int = 0


MUTE_LEASE_POLICIES = {
    policy.source: policy
    for policy in (
        MuteLeasePolicy(MUTE_SOURCE_BOSS, 3600.0, None, precedence=1),
    )
}
MUTE_SOURCES = tuple(MUTE_LEASE_POLICIES)


@dataclass(frozen=True)
class MuteLease:
    """One session nobody needs to be told about, for a bounded while (#823).

    Deliberately a lease and not a flag. It expires on its own, is renewed only
    while whatever granted it still holds, and every uncertainty — a lost
    renewal, a web restart — resolves to *unmuted*, because a boss who is never
    told is the one failure the deck cannot observe.
    """

    session_key: str
    source: str
    granted_at: datetime
    expires_at: datetime
    # ``None`` means renewable indefinitely (the boss-pressed lease).
    hard_expires_at: datetime | None = None
    # The chat's attempt window when the lease was taken (#472). A spent hard cap
    # is re-earnable only in a *later* window — that is, once the boss has
    # engaged with the chat — so back-to-back re-grants cannot launder the cap.
    attempt_window: str = ""

    def active_at(self, now: datetime) -> bool:
        return now < self.expires_at and (
            self.hard_expires_at is None or now < self.hard_expires_at
        )


@dataclass(frozen=True)
class TokenTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


@dataclass
class SubagentProgress:
    """Compact progress for one agent spawned by a parent chat.

    Used by every provider whose spawned agents have no addressable session of
    their own: Codex reads it from spawned-rollout records. A provider whose children
    *are* addressable nests real sessions with ``parent_session_key`` instead.
    """

    agent_id: str
    nickname: str | None = None
    role: str | None = None
    task: str | None = None
    status: str = "working"  # working | quiet | finished | failed
    result: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Session:
    key: str  # f"{account_key}:{session_id}" — one URL-safe provider/account leg
    account_key: str
    session_id: str  # provider-scoped source id; child agents may use their agent id
    status: SessionStatus
    # User-facing Working Session state. This stays true through quiet tool-use
    # gaps; it turns false only when the turn completes, waits on the operator,
    # or crosses the stall threshold.
    thinking: bool = False
    stalled: bool = False  # an Active Turn with no Turn Progress for STALL_S
    # The latest turn ended in a failure the owning runtime reported, rather
    # than in a reply (#778). Deliberately not a flavour of ``stalled``: nothing
    # is running and nothing will move again, so no timer can ever catch it —
    # a wedged agent looks exactly like a resting one until this says otherwise.
    turn_failed: bool = False
    # The chat's process died with its turn still open — a runtime restart or crash cut it
    # off — and nothing has run since (#3008). The runtime's revival answers it; until then,
    # or when the revival is refused, the turn neither ended nor failed on its own.
    turn_cut_off: bool = False
    # When that turn was last fed, and whether the runtime still owes it a revival: it
    # revives only at its own boot, so a worker that died alone is owed none (#3008).
    turn_cut_off_at: datetime | None = None
    revival_due: bool = False
    # A provider-declared instant when this failed turn can be retried automatically. None
    # means either that the failure was not quota exhaustion or that no trustworthy boundary
    # was supplied. Provider adapters normalize this; views never parse provider prose.
    automatic_retry_at: datetime | None = None
    # The turn running now was opened by a background notification — a task the chat started
    # finishing, or a queued job reporting back — not by a message anyone sent it (#2760).
    # Only meaningful while ``thinking``; False when the provider cannot tell.
    background_turn: bool = False
    # Why this chat's worker process died, when the owning runtime could name it
    # (#1546). "" for every ordinary ending — a process that simply finished
    # needs no explanation, and a reason nobody can prove is worse than silence.
    # Today only ``memory_cap``: the worker was killed for passing the memory
    # limit its own cgroup enforces.
    worker_exit_reason: str = ""
    # The role the deck started this owned chat with (``work`` for a Work's agent), as its
    # runtime recorded it; None when the provider does not say (#3362).
    owned_role: str | None = None
    # Explicit provider lifecycle, when one exists. None means transcript
    # structure/brief recency is the best available Active Turn evidence.
    lifecycle_active: bool | None = None
    cwd: Path | None = None
    title: str | None = None  # provider-native title
    generated_title: str | None = None  # Deckhand display title; native title stays intact
    initial_prompt: str | None = None  # first real user prompt; stable ownership/reference context
    last_prompt: str | None = None  # user's most recent prompt
    last_text: str | None = None  # agent's most recent response text
    last_role: str | None = None  # "user" | "agent": who sent the most recent message
    # Timestamp of that real user/agent text message. Deliberately separate from
    # activity/progress, which also advances for reasoning, tools and descendants.
    last_message_at: datetime | None = None
    last_event_seq: int = 0  # provider-normalized cursor for derived transcript state
    question: str | None = None  # trailing question from the agent's latest reply (awaiting you)
    muted: bool = False  # a live mute lease is held on this chat (#823)
    activity: str | None = None  # what it's doing now: "Using tools" / "Working"
    subagent_count: int = 0  # currently-running Codex spawned agents owned by this chat
    subagents: tuple[SubagentProgress, ...] = ()  # active + recently-finished agents
    parent_session_key: str | None = None  # set on a subagent session: the parent it nests under
    #  (kept out of the top-level list and shown as a compact row under the parent)
    model: str | None = None  # last assistant line's model (v0.2)
    kind: str | None = None  # "interactive" | "sdk-cli" | RC worker …
    worker_type: str | None = None  # "kanban" | "cloud" | "you"
    # Broad background/child-work marker used for Deckhand exclusion; recorded
    # delegation lineage is tracked separately in AppState.
    is_delegated: bool = False
    # GitHub work related to this chat. Issues are explicitly registered or
    # inferred structurally from a poller dispatch; PRs are registration-only.
    # Conversation text and shared-checkout branches are never guessed.
    issues: tuple[IssueRef, ...] = ()
    pull_requests: tuple[PullRequestRef, ...] = ()
    issue_url: str | None = None  # provider-inferred issue for kanban worker sessions
    issue_status: str | None = None  # GitHub state text: open / closed / merged
    issue_status_kind: str | None = None  # badge colour: open|merged|done|dropped|closed
    pid: int | None = None
    proc_start: str | None = None  # /proc starttime token — pid-reuse guard
    started_at: datetime | None = None
    # When the chat itself last moved — what the card's "ago" reads, what the
    # deck sorts on, and what a hidden/dismissed marker is compared against.
    # It is the newest real transcript event, NOT the transcript file's mtime:
    # a provider may rewrite a finished transcript's bookkeeping lines hours
    # later, and that write is not activity (#335).
    last_activity: datetime | None = None
    # Latest execution advancement (assistant/reasoning output, tool call/result,
    # or descendant work). Presentation metadata must not move this clock.
    last_progress: datetime | None = None
    tokens: TokenTotals | None = None  # summed from transcript usage blocks (v0.2)
    context_tokens: int | None = None  # context-window occupancy (input side of latest usage block)
    deep_link: str | None = None  # provider-native URL when applicable
    deep_link_label: str | None = None
    show_when_idle: bool = False  # keep in the dashboard after the native process exits
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    # Deck-owned identity above this concrete provider/account leg. Providers do
    # not set it; AppState attaches it from the durable control-plane store.
    chat_id: str | None = None
    # Ordered, de-duplicated account keys for the resolved legs of this chat.
    # Empty on provider-owned records; the chat-list projection fills it on the
    # current leg so the existing account badge can show a compact chain.
    chat_account_keys: tuple[str, ...] = ()

    @property
    def display_state(self) -> str:
        """User-facing state, in the vocabulary that matters: a session with a
        live process but no Active Turn is **idle** (alive but resting); a
        Working Session is **thinking**, including quiet tool-use gaps.
        (``status`` stays process-based — LIVE means a process exists; providers
        may still keep no-process sessions in the dashboard.)"""
        if self.thinking:
            return "thinking"
        if self.turn_failed:
            # A failed turn must not read as a healthy resting chat: nothing is
            # running and nothing will move again, so this is the only signal
            # the list view gets (#778, #788).
            return "failed"
        if self.status == SessionStatus.LIVE:
            return "idle"
        return self.status.value

    @property
    def display_title(self) -> str:
        return self.generated_title or self.title or self.session_id[:8]

    @property
    def detail_path(self) -> str:
        """Canonical human-facing URL; leg-scoped APIs keep using ``key``."""
        return f"/chats/{self.chat_id}" if self.chat_id else f"/sessions/{self.key}"

    @property
    def row_key(self) -> str:
        """Stable list identity while the concrete current leg changes."""
        return self.chat_id or self.key

    @property
    def primary_issue(self) -> IssueRef | None:
        """The provider-inferred issue first, otherwise the first explicit relation."""
        inferred = IssueRef.parse(self.issue_url or "")
        return inferred or (self.issues[0] if self.issues else None)

    @property
    def project(self) -> SessionProject:
        """Session Project resolution plus how strong the evidence behind it is.

        A registered issue URL names its repository outright, so it outranks any
        cwd inference — including a session working out of a generic temporary
        root, whose cwd alone resolves to nothing.
        """
        if self.primary_issue is not None:
            return _repository_project(self.primary_issue.repository.rsplit("/", 1)[-1])
        return resolve_session_project(self.cwd)

    @property
    def project_name(self) -> str | None:
        """Stable project identity used to prefix generated titles."""
        return self.project.name

    @property
    def generated_title_subject(self) -> str | None:
        """Generated title without the project prefix rendered above dashboard cards."""
        if self.generated_title is None or self.project_name is None:
            return self.generated_title
        prefix = re.escape(self.project_name)
        pattern = (
            rf"^{prefix}(?=#\d+\s*(?:·|[-:]))"
            if self.primary_issue is not None
            else rf"^{prefix}\s*(?:·|[-:])\s*"
        )
        return re.sub(
            pattern,
            "",
            self.generated_title,
            count=1,
            flags=re.IGNORECASE,
        ).lstrip()

    @property
    def is_waiting(self) -> bool:
        """Waiting Session (CONTEXT.md): paused on a question directed at the operator."""
        return bool(self.question)

    @property
    def is_subagent(self) -> bool:
        """A Task/Agent-tool child agent rather than a chat of its own.

        Providers stamp exactly these with ``kind="subagent"``. They exist only
        as nested rows under the chat that spawned them: you cannot answer or
        steer one, so the parent — not the subagent — owns the handoff.
        """
        return self.kind == "subagent"


@dataclass
class UsageSnapshot:
    account_key: str
    five_hour_pct: float | None
    five_hour_resets_at: datetime | None
    seven_day_pct: float | None
    seven_day_resets_at: datetime | None
    fetched_at: datetime
    stale: bool = False  # true when backoff/errors mean this is old data
    # The billing-cycle window (#730; added for the Cursor provider removed in
    # #2600). It sits after ``stale`` only because a dataclass cannot put a
    # defaulted field before ``fetched_at``; read it as the third member of the
    # window group above.
    month_pct: float | None = None
    month_resets_at: datetime | None = None

    def window(self, window: str) -> tuple[float | None, datetime | None]:
        """This snapshot's ``(percentage, resets_at)`` for one quota window."""
        if window == FIVE_HOUR:
            return (self.five_hour_pct, self.five_hour_resets_at)
        if window == MONTH:
            return (self.month_pct, self.month_resets_at)
        return (self.seven_day_pct, self.seven_day_resets_at)

    def percentages(self) -> list[float]:
        """Every window this snapshot carries a real numeric meter for.

        The one place that enumerates the window fields for admission: a worker
        ceiling and the poller both ask "how full is this
        account", and none of them should have to know which windows the
        account's provider happens to report. Adding a window here is what makes
        it count against every ceiling at once.
        """
        return [
            value
            for value in (self.five_hour_pct, self.seven_day_pct, self.month_pct)
            if isinstance(value, int | float) and not isinstance(value, bool)
        ]

    def max_pct(self) -> float | None:
        """The fullest reported window, or None when no window has a meter."""
        values = self.percentages()
        return max(values) if values else None


# The normalized quota windows an adapter can report. A window is usable for
# capacity calibration only where the adapter produced a real numeric meter *and*
# a reset boundary; anything else stays "Unavailable" rather than synthesized.
FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
# A provider may meter a billing cycle rather than a rolling window (#730). It is a
# meter only, and deliberately *not* in ``CAPACITY_WINDOWS``, the rolling windows.
MONTH = "month"
CAPACITY_WINDOWS = (FIVE_HOUR, SEVEN_DAY)
# Every window the header can draw a bar for. Which of them one account shows is
# the provider's own answer (``SessionProvider.usage_windows``), so a Claude or
# Codex account never grows a third empty row it will never fill.
USAGE_WINDOWS = (FIVE_HOUR, SEVEN_DAY, MONTH)
# Each window with the ``UsageSnapshot`` fields — and published usage-cache keys — that hold
# its percentage and its reset boundary. The Poller Engine reads the cache by these (#2597).
USAGE_WINDOW_KEYS = tuple(
    (window, f"{window}_pct", f"{window}_resets_at") for window in USAGE_WINDOWS
)


@dataclass(frozen=True)
class ForkFrom:
    """Start a new session as a copy of ``session_id`` cut after ``ref`` (#2485).

    ``ref`` is the provider's own name for the cut: the uuid of the turn's last
    assistant line for Claude Code, the turn id for Codex. Only the provider that
    resolved it reads it.
    """

    session_id: str
    ref: str


@dataclass(frozen=True)
class InjectResult:
    accepted: bool
    reason: str | None = None
    session_id: str | None = None
    transcript_expected: bool = True

    @classmethod
    def from_wire(cls, data: object, *, source: str = "runtime") -> InjectResult:
        """Decode the ``{accepted, reason, session_id}`` envelope the runtime
        returns over its socket. Tolerant of a malformed reply: a non-dict or
        non-string field degrades to a rejection / None rather than raising."""
        if not isinstance(data, dict):
            return cls(False, f"invalid response from {source}")
        reason = data.get("reason")
        session_id = data.get("session_id")
        return cls(
            bool(data.get("accepted")),
            reason if isinstance(reason, str) else None,
            session_id if isinstance(session_id, str) else None,
        )

    def to_wire(self) -> dict:
        """The wire envelope a runtime endpoint sends back. ``transcript_expected``
        is intentionally web-side only and never crosses the socket."""
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class InteractionOption:
    label: str
    description: str = ""
    value: str | None = None


@dataclass(frozen=True)
class InteractionQuestion:
    id: str
    header: str
    prompt: str
    options: tuple[InteractionOption, ...] = ()
    allow_other: bool = False
    secret: bool = False
    multiselect: bool = False  # render checkboxes (many answers) rather than radios


@dataclass(frozen=True)
class PendingInteraction:
    """A provider-neutral request that blocks an active agent turn."""

    id: str
    kind: str
    thread_id: str
    turn_id: str | None
    title: str
    message: str | None = None
    questions: tuple[InteractionQuestion, ...] = ()
    command: str | None = None
    cwd: str | None = None
    url: str | None = None
    decisions: tuple[str, ...] = ()
    # A Claude chat's open questions, kept by the deck past the turn that asked them (#3451):
    # any of them may be answered or dismissed on its own, so none is required.
    dismissible: bool = False

    def answer_json(self) -> dict:
        """The API's shape of this interaction: everything needed to answer it (#590, #3481)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "message": self.message,
            "command": self.command,
            "cwd": self.cwd,
            "url": self.url,
            "decisions": list(self.decisions),
            # Any one question may be answered, or dismissed with ``dismiss:<question id>``.
            "dismissible": self.dismissible,
            "questions": [
                {
                    "id": question.id,
                    "header": question.header,
                    "prompt": question.prompt,
                    "allow_other": question.allow_other,
                    "secret": question.secret,
                    "options": [
                        {
                            "label": option.label,
                            "description": option.description,
                            "value": option.value,
                        }
                        for option in question.options
                    ],
                }
                for question in self.questions
            ],
        }

    @classmethod
    def from_dict(cls, data: object) -> PendingInteraction | None:
        """Rebuild a PendingInteraction from its ``asdict`` wire form — the single
        inverse of ``dataclasses.asdict``, so the two provider runtimes that
        serialize this neutral shape over their sockets deserialize it the same
        way. Tolerant of a partial/legacy dict: a missing or non-string ``id``
        yields None."""
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            return None
        questions = tuple(
            InteractionQuestion(
                id=str(q.get("id")),
                header=q.get("header") or "",
                prompt=q.get("prompt") or "",
                options=tuple(
                    InteractionOption(
                        label=o.get("label") or "",
                        description=o.get("description") or "",
                        value=o["value"] if isinstance(o.get("value"), str) else None,
                    )
                    for o in (q.get("options") or [])
                    if isinstance(o, dict)
                ),
                allow_other=bool(q.get("allow_other")),
                secret=bool(q.get("secret")),
                multiselect=bool(q.get("multiselect")),
            )
            for q in (data.get("questions") or [])
            if isinstance(q, dict)
        )
        return cls(
            id=data["id"],
            kind=data.get("kind") or "question",
            thread_id=data.get("thread_id") or "",
            turn_id=data["turn_id"] if isinstance(data.get("turn_id"), str) else None,
            title=data.get("title") or "",
            message=data["message"] if isinstance(data.get("message"), str) else None,
            questions=questions,
            command=data["command"] if isinstance(data.get("command"), str) else None,
            cwd=data["cwd"] if isinstance(data.get("cwd"), str) else None,
            url=data["url"] if isinstance(data.get("url"), str) else None,
            decisions=tuple(str(d) for d in (data.get("decisions") or [])),
            dismissible=bool(data.get("dismissible")),
        )


#: The id prefix of a chat's open questions taken together (#3451).
OPEN_QUESTIONS_PREFIX = "questions-"


def open_questions_id(question_ids: Iterable[str]) -> str:
    """One id for a set of open questions, the same in the runtime and the web.

    It changes whenever a question is added, answered or dismissed, so the page redraws
    the box then and only then.
    """
    digest = hashlib.sha256("\n".join(question_ids).encode()).hexdigest()[:16]
    return OPEN_QUESTIONS_PREFIX + digest


# An open turn older than this is considered stalled (hung worker), not busy —
# without it, a dead-but-LIVE process whose last line is a tool call would show
# "Using tools" forever.
STALL_S = 600.0

# The activity a chat carries when only a command it started in the background keeps it
# working: its turn has ended and the command has not reported back. A dev server never does,
# so a Work does not count this as working (#2783); the chat's own row still shows it.
BACKGROUND_TASK_ACTIVITY = "Background task"
# The same for a helper agent the chat started in the background (#617): its own turn has
# ended and only the agent still runs.
BACKGROUND_AGENT_ACTIVITY = "Background agent"
BACKGROUND_ACTIVITIES = frozenset({BACKGROUND_TASK_ACTIVITY, BACKGROUND_AGENT_ACTIVITY})



def event_turn_open(last_ev: TranscriptEvent | None) -> bool | None:
    """Structural Active Turn evidence carried by the latest transcript event."""
    if last_ev is None:
        return None
    if last_ev.tool_name == "AskUserQuestion" or last_ev.question:
        return False
    if last_ev.turn_continues is not None:
        return last_ev.turn_continues
    if last_ev.role in {"user", "tool"} or last_ev.tool_name:
        return True
    return None


def event_progress_at(
    last_ev: TranscriptEvent | None, fallback: datetime | None
) -> datetime | None:
    """Latest execution-progress timestamp, excluding non-event file writes."""
    return last_ev.ts if last_ev is not None and last_ev.ts is not None else fallback


def transcript_event_is_progress(event: TranscriptEvent) -> bool:
    """Whether a normalized transcript event advances execution.

    Usage/token bookkeeping is intentionally excluded. Visible model output,
    prompts, tool calls/results, reasoning heartbeats, and descendant lifecycle
    events are progress.
    """
    return bool(
        event.role == "tool"
        or event.text
        or event.tool_name
        or event.question
        or event.answer
        or event.image_media_types
        or event.subagent_status
    )


def turn_stalled(
    *,
    live: bool,
    lifecycle_active: bool | None,
    last_ev: TranscriptEvent | None,
    age_s: float,
    stall_s: float = STALL_S,
) -> bool:
    """Whether current normalized evidence represents a Stalled Turn."""
    if not live or (last_ev is not None and last_ev.tool_name == "AskUserQuestion"):
        return False
    active = lifecycle_active
    if active is None:
        active = event_turn_open(last_ev)
    return active is True and age_s >= stall_s

def activity_label(
    live: bool,
    streaming: bool,
    last_ev,
    age_s: float = 0.0,
    stall_s: float = STALL_S,
    *,
    lifecycle_active: bool | None = None,
) -> str | None:
    """What the agent is doing right now, or None when idle/dead/stalled.

    Keyed off the *open turn*, not just recent writes, so a long tool run or a
    slow first token doesn't read as idle:
    - last line is an unanswered AskUserQuestion → None (the agent is paused on
      *your* answer, not working — the question is surfaced separately);
    - last line is a tool call / tool result → "Using tools" (persists through
      long tools, where the transcript is quiet for the tool's whole duration);
    - last line is an unanswered user/queued prompt → "Working";
    - actively writing (recent transcript write) → "Working";
    - open turn but no write for ``stall_s`` → stalled, treated as idle;
    - otherwise (LIVE but quiet, last line a finished reply) → None (idle)."""
    if not live:
        return None
    # An unanswered AskUserQuestion is the agent waiting on the user, not busy —
    # regardless of how recently it was written (streaming just finished a
    # question). Surfaced as a question on the card instead of an activity badge.
    if last_ev is not None and last_ev.tool_name == "AskUserQuestion":
        return None
    active = lifecycle_active
    if active is None:
        active = event_turn_open(last_ev)
    # Explicit lifecycle/structure closes immediately; recency is only a
    # fallback for an ambiguous assistant event or an empty live tail.
    if active is False:
        return None
    if active is None:
        return "Working" if streaming else None
    if age_s >= stall_s:
        return None
    if last_ev is not None and (last_ev.role == "tool" or last_ev.tool_name):
        return "Using tools"
    return "Working"


# How far back the activity detail may look for the tool call driving the
# current line, and how much of a message it may quote. Both are small on
# purpose: the badge is one line beside a ticking seconds counter, and a turn
# only ever puts a couple of results between a call and the tail.
ACTIVITY_LOOKBACK = 12
ACTIVITY_SNIPPET_CHARS = 60
# Bullets, headings, quotes and ordered-list markers are decoration at badge
# size. The words after them are the part that says what is being worked on.
_MARKDOWN_LEAD = re.compile(r"^(?:[#>]+|[-*+•]|\d+[.)])\s+")


def activity_snippet(text: str | None) -> str | None:
    """One short, plain sentence from a transcript message, for the badge."""
    for raw in (text or "").splitlines():
        line = _MARKDOWN_LEAD.sub("", raw.strip())
        line = " ".join(line.replace("`", "").replace("*", "").split())
        if not line:
            continue
        if len(line) <= ACTIVITY_SNIPPET_CHARS:
            return line
        head, _, _tail = line[:ACTIVITY_SNIPPET_CHARS].rpartition(" ")
        return f"{head or line[:ACTIVITY_SNIPPET_CHARS]}…"
    return None


def _driving_tool_event(recent):
    """The tool call the tail is still sitting on, from ``recent`` (#1457).

    A tool *result* carries no tool name, so once it lands in last position the
    call that produced it is one line out of reach and the badge falls back to
    the bare "Using tools". The call is always just behind it in the same turn,
    so walk back to it — stopping at a user prompt, which starts a new turn and
    whose tools have nothing to do with what is running now.
    """
    for event in reversed(list(recent)[-ACTIVITY_LOOKBACK:]):
        if event.role == "user":
            return None
        if event.tool_name:
            return event
    return None


def detailed_activity_label(label: str | None, last_ev, recent=()) -> str | None:
    """Add a compact user-facing detail to a generic activity label.

    ``recent`` is the tail of transcript events ending at ``last_ev``, oldest
    first. It is optional: without it this reads ``last_ev`` alone and a tail
    sitting on a tool result keeps the generic label.
    """
    if label == "Working":
        # Nothing in last position names a tool, so the line driving the turn
        # is the only answer to what is being worked on: the model's own latest
        # sentence, or the prompt it was just handed (#1457). A queued prompt
        # is excluded — the agent has not been given it yet, so it is not what
        # it is working on.
        if last_ev is None or last_ev.queued:
            return label
        snippet = activity_snippet(last_ev.text)
        return f"Working: {snippet}" if snippet else label
    if label != "Using tools" or last_ev is None:
        return label
    if not last_ev.tool_name:
        last_ev = _driving_tool_event(recent) or last_ev
    if not last_ev.tool_name:
        return label
    name = last_ev.tool_display_name or (
        last_ev.tool_name.rsplit("__", 1)[-1].replace("_", " ").strip()
    )
    folded_name = name.casefold()
    if folded_name == "reasoning":
        return "Thinking"
    if folded_name in ("wait", "write stdin"):
        return "Waiting for command output"
    if folded_name == "wait for agents":
        return "Waiting for subagents"
    if folded_name == "start agents":
        return "Starting subagents"
    summary = (last_ev.tool_summary or "").strip()
    if folded_name == "approval":
        reason = summary.removeprefix("reason: ")
        return f"Requesting approval: {reason}" if reason else "Requesting approval"
    # The provider's own id for its shell tool is not a user-facing word, in
    # either sentence this can end in.
    display_name = {"exec": "shell", "exec command": "shell"}.get(folded_name, name)
    if not summary:
        return f"Using {display_name}" if display_name else label

    key, separator, value = summary.partition(": ")
    if separator:
        action = {
            "cmd": "Running",
            "command": "Running",
            "path": "Accessing",
            "query": "Searching",
            "url": "Opening",
        }.get(key.casefold())
        if action:
            return f"{action}: {value}"
    if last_ev.tool_name == "apply_patch" and summary.startswith("***"):
        return "Editing files"
    return f"{display_name.title()}: {summary}" if display_name else label


@dataclass
class TranscriptEvent:  # normalized transcript line (parsed from v0.2)
    seq: int  # monotonically increasing per session (line number)
    role: str  # "user" | "assistant" | "tool" | "system"
    text: str | None = None
    tool_name: str | None = None
    tool_display_name: str | None = None  # optional user-facing override
    tool_summary: str | None = None  # short rendering of tool_use input
    question: str | None = None  # AskUserQuestion prompt, when this line asks one
    answer: str | None = None  # your reply to an AskUserQuestion (from its tool_result)
    model: str | None = None
    tokens: TokenTotals | None = None  # normalized token usage for this line
    ts: datetime | None = None
    subagent: str | None = None  # set when from <uuid>/subagents/
    queued: bool = False  # user message typed while the agent was busy (enqueued)
    # Provider-declared continuation on this event: True keeps the Active Turn
    # open, False closes it, None leaves structure/brief recency to decide.
    turn_continues: bool | None = None
    tool_detail: str | None = None  # expandable provider-native tool input
    subagent_status: str | None = None  # compact spawned-agent lifecycle update
    subagent_id: str | None = None
    subagent_name: str | None = None
    subagent_identities: tuple[tuple[str, str], ...] = ()  # spawn output id/name pairs
    image_media_types: tuple[str, ...] = ()  # safe raster images on this message
    # This event is the runtime reporting that the turn failed, not the model
    # replying. Providers write their failures into the transcript in the shape
    # of an ordinary message, so without this the deck cannot tell an apology
    # about an API error from a real answer (#1463).
    failed: bool = False
    # A background notification that opened this turn (a task the chat started has
    # finished), as one short line. It renders as a grey notice so the chat shows where
    # the person's turn ended and the automatic one began (#3377).
    notice: str | None = None


@dataclass
class TranscriptDetail:  # v0.2 — the bundle a session detail page needs
    events: list[TranscriptEvent]  # windowed (most-recent slice) for display
    tokens: TokenTotals  # summed over the WHOLE transcript
    model: str | None
    todos: list[dict]
    total_events: int  # count across the whole transcript
    earliest_seq: int  # smallest seq in ``events`` (for "load earlier")
    skipped: int = 0
