"""GitHub reads and effects for the engine's port (#180).

It lives in the engine package, because a wake that cannot read GitHub cannot run at all: `gh`
over subprocess, nothing else. Merging it with the deck's own `gh_cli` and `github_cache` is a
later step (#2597). Worker delivery is the piece that stays app-side — the engine
records a durable send intent and emits a directive, and AgentDeck's own worker client performs
the delivery (#176), because that client is async and dependency-laden.

The engine core proper stays deterministic and I/O-free; this module is the one place in the
package that talks to the outside, and it makes no decisions of its own.

Every query here was chosen against real payloads, not invented: the shapes come from live
`gh` output, and the linked-PR discovery follows the reference engine's rules exactly, because
those rules were learned from production accidents rather than from first principles.

Three habits run through it:

* **A read that fails answers "unknown", never "no".** An unreadable card, an unlistable
  repository, an unavailable permission — each returns ``None`` so the engine can defer. An
  adapter that turned a network blip into a negative answer would quietly change policy.
* **One credential: the instance's non-human authority** (#2289). The owner rule in
  `github_authority` picks it (`eerovil-agent`, or the AgentDeck App's installation on the
  organization) and every read, effect and merge speaks as it. There is no host login: a `Gh`
  with no credential refuses the call rather than letting `gh` fall back to whoever is logged in.
* **Nothing is decided here.** Statuses are normalized to the roles the Manifest named and
  handed over; whether a release may proceed is the engine's judgment, not this module's.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from ..models import USAGE_WINDOW_KEYS
from . import blockers as card_blockers
from . import (
    github_authority,
    queue,
    rate_limits,
    ready_review,
    revive,
    schedules,
)
from .config import (
    DEFAULT_CORE_RESERVE,
    DEFAULT_GRAPHQL_RESERVE,
    Reachability,
    RepoCheck,
    Role,
    boardless_snapshot,
    role_of_finish_token,
    role_of_status_labels,
    status_label,
    status_labels,
)
from .dispatch import work_id as card_work_id
from .ownership import LabelEvent, LabelRemoval
from .ports import (
    ArmOutcome,
    CardQuery,
    CommentBody,
    Effect,
    EffectKind,
    LinkedPRs,
    PrState,
    ScanResult,
    SnapshotBatch,
    WorktreeChanges,
    WorktreeCleanup,
)
from .reconcile import CardSnapshot

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 45
# The runtime's Work workspace routes (#2149); `agentdeck.work_workspace_claims` owns them
# and a test pins the spelling. A release removes worktrees, so it gets longer.
WORK_WORKSPACE_ENSURE_PATH = "/work-workspaces/ensure"
WORK_WORKSPACE_RELEASE_PATH = "/work-workspaces/release"
WORK_WORKSPACE_CLAIMS_PATH = "/work-workspaces/claims"
WORK_WORKSPACE_RELEASE_TIMEOUT_S = 300
# The Runtime's Run execution routes (#2159); `agentdeck.run_execution` owns them.
EXECUTION_OUTCOME_PATH = "/executions/outcome"
EXECUTION_STOP_PATH = "/executions/stop"
# The Runtime's states that nothing can still write the Work's workspace in, and what a stop
# answers once nothing can.
EXECUTION_SAFE_STATES = frozenset({"ended", "died", "stopped", "not_started"})
# ``run_execution.WAITING_HUMAN``: the Run's worker is stopped on a person's prompt (#2887).
EXECUTION_WAITING_HUMAN = "waiting_human"
EXECUTION_STOP_PROVED = frozenset({"stopped", "already_stopped"})
# One outcome read, asked for each card with a Run on every wake: a Runtime that accepts and
# never answers must not spend the wake's whole budget, and a missed read is only "unknown".
EXECUTION_READ_TIMEOUT_S = 15
SNAPSHOT_BATCH_SIZE = 20
LABELLED_ISSUE_PAGE_SIZE = 100
LABELLED_ISSUE_PAGES = 2
LABELLED_EVENTS_PER_ISSUE = 100
# The newest label removals and comments of an issue, in one ordered page (#2417).
OWNERSHIP_TIMELINE_READ = 100

# GitHub's native issue dependencies, read inside the card snapshot so a dependency costs no
# query of its own. Bounded like every other connection here — but a *closed* dependency stays
# listed, so "the first page already stops the card" is not true: fifty closed ones on page one
# would hide an open one on page two (#684). So the page reports whether it was the whole list,
# and a card that says it was not is paginated out before its blockers are believed.
BLOCKED_BY_PAGE = 50
BLOCKED_BY_SELECTION = (
    "{nodes{number state repository{name owner{login}}}"
    "pageInfo{hasNextPage endCursor}}"
)


def comment_url(value: object) -> str:
    """A safe absolute HTTP URL for one Markdown link, or nothing.

    Worker-control stores operator-visible URLs in durable state. Refusing whitespace and
    Markdown delimiters keeps a malformed value from corrupting every later comment footer.
    """
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or any(
            char.isspace() or char in "<>()[]\\" or ord(char) < 32 or ord(char) == 127
            for char in text
        )
    ):
        return ""
    return text


#: Both forms AgentDeck publishes for one conversation. ``/chats/<id>`` is the
#: canonical work object and outlives an account or provider switch;
#: ``/sessions/<key>`` names one concrete leg and stays a permanent alias, so a
#: URL published by an older worker keeps resolving.
SESSION_URL_PREFIXES = ("/sessions/", "/chats/")


def public_session_url(value: object) -> str:
    """A non-loopback AgentDeck chat or session URL safe to publish on GitHub."""
    text = comment_url(value)
    if not text:
        return ""
    parsed = urlsplit(text)
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if loopback:
        return ""
    return text if parsed.path.startswith(SESSION_URL_PREFIXES) else ""


def github_pull_request_url(value: object) -> str:
    """A canonical github.com pull-request URL, or nothing."""
    text = comment_url(value)
    if not text:
        return ""
    parsed = urlsplit(text)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname != "github.com" or len(parts) != 4 or parts[2] != "pull":
        return ""
    return text if parts[3].isdigit() else ""
BLOCKED_BY_FIELD = f"blockedBy(first:{BLOCKED_BY_PAGE}){BLOCKED_BY_SELECTION}"
# How many further pages one card may cost. Generous against any real card and finite against a
# pathological one; a card still truncated at the cap is held rather than read forever.
BLOCKED_BY_PAGES = 10
PULL_REQUEST_CLOSING_ISSUES = 10
BLOCKED_BY_PAGE_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!,$after:String!){"
    "rateLimit{remaining resetAt cost}"
    "repository(owner:$owner,name:$repo){card:issue(number:$number){"
    f"blockedBy(first:{BLOCKED_BY_PAGE},after:$after){BLOCKED_BY_SELECTION}"
    "}}}"
)

# The deck publishes one usage snapshot per account and refuses new workers at its own ceiling;
# these mirror that so the engine does not dispatch into an account the deck will turn away.
USAGE_CEILING_PCT = 90.0
USAGE_MAX_AGE_S = 1800.0
# The reading at which an account stops being expensive and starts being unable. A window
# reported this full, with its reset still ahead, will refuse the next turn rather than run
# it slowly — which is a fact about the turn, not an opinion about the budget (#1519).
REFUSAL_PCT = 100.0
# How full a window should be by the time it resets. A subscription window that expires part
# spent is money the operator already paid for and did not get, so an account short of this
# with its reset close is the one that should receive the next card (#1965).
USAGE_TARGET_PCT = 95.0
# "Close to its reset" as a share of the window's own length, so one number means the same
# thing to a weekly window as to a monthly one. A quarter of seven days is the last ~42 hours.
EXPIRY_HORIZON_RATIO = 0.25
OWNERSHIP_LABEL = "claude"
TERMINAL_MARKER = "<!-- claude-terminal generation="
# The marker token of the comment that resumes a card. Deliberately not a finish token or a
# board role: ``_marker_role`` answers None for it, so the lease reads as unreported.
RESUMED_TOKEN = "resumed"
# A terminal marker's status token, which is how a worker's own report is told apart from the
# poller's status notes when older comments are hidden (#2689).
# A marker with no status reads as "acknowledged" (``_marker``), which is still a report.
_TERMINAL_STATUS_RE = re.compile(re.escape(TERMINAL_MARKER) + r"\S+(?:\s+status=([^\s>]+))?")
# One per posted chat answer (#2348): the id a retry looks for before it posts again.
CHAT_ANSWER_MARKER = "<!-- agentdeck-chat-answer id="
_CHAT_ANSWER_RE = re.compile(re.escape(CHAT_ANSWER_MARKER) + r"([A-Za-z0-9_-]{8,128}) -->")


_TERMINAL_GENERATION_RE = re.compile(re.escape(TERMINAL_MARKER) + r"([^\s>]+)")
# One per note a worker's queued ``reopen`` posts (#2845): the id a retry looks for before it
# posts again, so a request that could not be written after its note landed posts nothing twice.
REOPEN_REQUEST_MARKER = "<!-- agentdeck-reopen-request id="
_REOPEN_REQUEST_RE = re.compile(re.escape(REOPEN_REQUEST_MARKER) + r"([A-Za-z0-9_-]{8,128}) -->")


def newest_terminal_generation(comments: list[dict]) -> str:
    """The generation whose terminal marker is in the newest comment carrying one, or "" (#2845).

    ``comments`` oldest first, as both GitHub APIs list them.
    """
    for comment in reversed(comments):
        found = _TERMINAL_GENERATION_RE.findall(str(comment.get("body") or ""))
        if found:
            return found[-1]
    return ""


def _json_pages(text: str) -> list[list] | None:
    """Every page array ``gh api --paginate`` printed, or None when any of it is not one.

    Depending on the version, ``gh`` prints one merged array or one array per page, back to back
    (``[...][...]``); both read the same here. Anything that is not an array of pages is unknown,
    never an empty history.
    """
    decoder = json.JSONDecoder()
    pages: list[list] = []
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return pages if pages else None
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            return None
        if not isinstance(value, list):
            return None
        pages.append(value)


def reopen_request_ids(comments: list[dict]) -> frozenset[str]:
    """Every queued-reopen request id whose note is in these comments (#2845)."""
    return frozenset(
        match.group(1)
        for comment in comments
        for match in _REOPEN_REQUEST_RE.finditer(str(comment.get("body") or ""))
    )


def chat_answer_ids(comments: list[dict]) -> frozenset[str]:
    """Every chat answer id whose marker is in these comments."""
    return frozenset(
        match.group(1)
        for comment in comments
        for match in _CHAT_ANSWER_RE.finditer(str(comment.get("body") or ""))
    )
# How much of a worker's terminal comment the engine keeps as its park note (#2332).
PARK_NOTE_LIMIT = 1500
# The lines the deck reads a park note for: a "Blocked…" line and a question (#2332).
_PARK_ACTION_LINE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?\**blocked\b|\?\**[ \t]*$", re.I)


def _park_text(text: str) -> str:
    """A worker's report cut to ``PARK_NOTE_LIMIT``, never losing what the deck reads in it.

    The question and the "Blocked…" line are usually near the end of a long report, so a
    plain head cut would drop exactly them. A report over the limit keeps those lines first,
    then as much of its tail as fits: the last question is still the last one read, and the
    first "Blocked…" line is still the first.
    """
    if len(text) <= PARK_NOTE_LIMIT:
        return text
    kept = "\n".join(
        line.strip() for line in text.splitlines() if _PARK_ACTION_LINE.search(line)
    )[: PARK_NOTE_LIMIT // 2]
    if not kept:
        return text[-PARK_NOTE_LIMIT:].lstrip()
    room = PARK_NOTE_LIMIT - len(kept) - 2
    return f"{kept}\n\n{text[-room:].lstrip()}"
# Every spelling that is a board role. A terminal marker resolving to anything else names no
# status, and must not become one (#1939).
_ROLE_VALUES = frozenset(role.value for role in Role)
REVIEW_MARKER = "<!-- claude-merge-review generation="
BOT_MARKER = "<!-- claude-bot -->"
# The ChatGPT reviewer's handoff comments are superseded the same way (#2167): every round
# posts a fresh findings list, and only the newest is what a worker still has to act on. The
# reviewer skill in the review-chat repository ends each one with this marker; the heading is how
# the ones posted before that marker existed are recognised.
CHATGPT_REVIEW_MARKER = ready_review.FINDINGS_MARKER
_REVIEW_HEADING_RE = re.compile(r"\A/claude\s+Review findings for PR #\d")
# How far back one retirement pass looks for the poller's own superseded comments (#2116). A
# thread longer than this keeps whatever is behind the window visible, which is the harmless
# direction to be wrong in: every later comment retires the ones this pass did reach, so a
# thread only ever gets tidier. One page, so the read stays one GraphQL point.
RETIRE_COMMENT_SCAN = 100
# How many comments one pass may hide. The cap bounds the mutation document rather than the
# work: the rest are hidden by the next comment this card posts.
RETIRE_COMMENT_LIMIT = 50
# What a GitHub comment node id is allowed to look like before it is pasted into a mutation
# document. The ids come from GitHub itself, so this is not expected to reject anything — it
# is here because a value interpolated into a query string is exactly where a surprise would
# turn into a malformed or hostile document, and a rejected id costs only a comment that stays
# visible.
_COMMENT_NODE_ID_RE = re.compile(r"\A[A-Za-z0-9_=-]{1,200}\Z")
# A pull request body carrying this is never any card's release target (#1445).
RELEASE_OPTOUT_MARKER = "<!-- agentdeck:release=no -->"
# A pull request body carrying this is one step of its card, not the whole of it (#2730): the
# poller releases it as usual, then hands the card back to its worker instead of closing it.
RELEASE_STEP_MARKER = "<!-- agentdeck:release=step -->"
# How `gh` reports that a named repository or pull request does not exist, as opposed to a
# read it could not complete. The difference decides whether a named release target is refused
# as a typo or retried on the next wake, so it is matched on GitHub's own wording rather than
# on the exit code, which is 1 for both.
_UNRESOLVABLE_RE = re.compile(r"could not resolve to a (?:pullrequest|repository)", re.IGNORECASE)

# GitHub's own closing keywords, in the three shapes a pull request body writes a card in: a
# qualified `owner/repo#n`, the issue URL, and a bare `#n` meaning the pull request's own
# repository. Used to read which card a pull request says it belongs to, never to *find* one —
# a closing keyword adds nothing to a reference that already qualifies (#1445), but it is the
# author stating a card, so it can rule the pull request out for every other card (#2095).
# A companion pull request in a repository that tracks no issues states its card on its first
# line instead of closing it (#2729): "Companion to eerovil/agentdeck#2727", "Part of ...",
# "For ...". Only the first line counts — later in the body the same words cite background.
_BELONGS_RE = re.compile(
    r"\A\s*(?:companion\s+to|part\s+of|for)\s+"
    r"(?:(?P<owner>[\w.-]+)/)?(?P<repo>[\w.-]+)#(?P<number>\d+)(?!\d)",
    re.IGNORECASE,
)

_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s+"
    r"(?:https?://github\.com/(?P<url_owner>[\w.-]+)/(?P<url_repo>[\w.-]+)/issues/(?P<url_number>\d+)"
    r"|(?:(?P<owner>[\w.-]+)/)?(?P<repo>[\w.-]+)#(?P<number>\d+)"
    r"|#(?P<bare>\d+))(?!\d)",
    re.IGNORECASE,
)

# How many recent issues to read when looking for one this engine already opened (#393). Bounded
# because it runs only to recover an interrupted creation, and the issue being looked for was
# created moments ago by definition.
MARKED_ISSUE_SCAN = 50
# How many scheduled tasks one sweep reads per repository (#2517). Every open one, or the sweep
# refuses rather than mistake a task it never saw for a deleted one; and enough recently closed
# ones to notice a task that was stopped since the last full wake.
SCHEDULED_OPEN_PAGE = 100
SCHEDULED_CLOSED_PAGE = 30

# `gh pr checks` exits 0 when every required check passed, 8 while any is still running, and
# non-zero otherwise. A repository with no checks reports none, which gates nothing.
_CHECKS_PENDING = 8
_NO_CHECKS = "no checks reported"


# `gh` exits with this when this adapter refused to run it at all: no credential (#2289).
EXIT_NO_AUTHORITY = 77


# A check run that finished this way did not pass (#2364). ``neutral`` and ``skipped`` did not
# fail either, so they neither pass nor block on their own.
_RUN_FAILED = frozenset({"failure", "cancelled", "timed_out", "action_required", "stale"})


def ci_verdict(statuses: object, runs: object) -> str | None:
    """``green`` / ``pending`` / ``failing`` / ``none`` from one commit's statuses and runs."""
    if not isinstance(statuses, list) or not isinstance(runs, list):
        return None
    states = [str(state or "").lower() for state in statuses]
    finished = [run for run in runs if isinstance(run, dict)]
    if any(state in {"failure", "error"} for state in states) or any(
        str(run.get("conclusion") or "").lower() in _RUN_FAILED for run in finished
    ):
        return "failing"
    if any(state not in {"success"} for state in states) or any(
        str(run.get("status") or "").lower() != "completed" for run in finished
    ):
        return "pending"
    if not states and not finished:
        return "none"
    return "green"


def _gh_shim_dir() -> Path:
    """The worker `gh` shim's directory, resolved as `gh_shim.install.shim_dir` does.

    A copy, because the engine may not import `agentdeck.gh_shim` (its import boundary);
    `test_the_engine_finds_the_shim_where_the_shim_installs_itself` holds the two together.
    """
    configured = os.environ.get("AGENTDECK_GH_SHIM_DIR")
    if configured:
        return Path(configured).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return root / "agentdeck" / "gh-shim"


@dataclass
class Gh:
    """One `gh` invocation path: a credential, a timeout, and nothing to fall back to.

    ``token_source`` is called per invocation, so an App installation token that expires
    during a long wake is minted again rather than failing half the cycle. ``authority`` names
    the identity (``agent`` / ``app:<org>``) for the rate-limit store. No credential at all is
    a refusal: every inherited credential variable is removed, so `gh` never gets to use the
    host's own login in its place.
    """

    token: str = ""
    binary: str = "gh"
    timeout: float = DEFAULT_TIMEOUT_S
    env: dict = field(default_factory=dict)
    authority: str = ""
    token_source: Callable[[], str] | None = None
    # What GitHub answered this instance's last failed call, and whether it looked like a
    # hiccup a repeat may not meet (#2714). Cleared by the next call that succeeds.
    last_failure: str = field(default="", repr=False, compare=False)
    last_failure_transient: bool = field(default=False, repr=False, compare=False)

    def _environment(self) -> dict[str, str]:
        """The child environment. Raises ``github_authority.Refusal`` with no credential."""
        environment = {**os.environ, **self.env}
        # Worker-control runs from a worker's shell, whose PATH puts the worker `gh` shim
        # first. Its cache served a five-minute-old pull request head to `ready-for-review`
        # after the push had landed (#2698), and the poller must never spend through it.
        shim = _gh_shim_dir()
        environment["PATH"] = os.pathsep.join(
            part
            for part in environment.get("PATH", os.defpath).split(os.pathsep)
            if part and Path(part) != shim
        )
        for name in github_authority.TOKEN_VARS:
            environment.pop(name, None)
        environment.pop(github_authority.AUTHORITY_ENV, None)
        token = self.token_source() if self.token_source is not None else self.token
        if not token:
            raise github_authority.Refusal("this instance has no GitHub credential")
        environment["GH_TOKEN"] = token
        if self.authority:
            environment[github_authority.AUTHORITY_ENV] = self.authority
        return environment

    def run(self, *args: str) -> tuple[int, str, str]:
        try:
            environment = self._environment()
        except github_authority.Refusal as refusal:
            return EXIT_NO_AUTHORITY, "", f"github: {refusal}"
        child = {**environment, "GH_DEBUG": "api"}
        try:
            done = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=child,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"gh {args[0] if args else ''} timed out"
        except OSError as exc:
            return 127, "", str(exc)
        clean_stderr, readings = rate_limits.strip_api_trace(done.stderr or "")
        store = rate_limits.RateLimitStore()
        credential = rate_limits.credential_identity(environment)
        if args[:2] == ("api", "user") and done.returncode == 0:
            store.bind(credential, rate_limits.user_id(done.stdout or ""))
        store.record_headers(credential, readings)
        self.last_failure, self.last_failure_transient = "", False
        if done.returncode != 0:
            # Name the missing grant or App permission in what the wake logs (#2289), and
            # only when GitHub's answer shows one; otherwise say what it did answer (#2714).
            target = next(
                (args[i + 1] for i, part in enumerate(args[:-1]) if part in ("-R", "--repo")), ""
            )
            hint = github_authority.failure_hint(done.stderr or "", environment, target)
            summary = github_authority.failure_summary(done.stderr or "")
            for line in (hint, summary):
                if line:
                    clean_stderr = f"{clean_stderr.rstrip()}\n{line}\n".lstrip("\n")
            failed = github_authority.failed_responses(done.stderr or "")
            self.last_failure = summary or hint or clean_stderr.strip()[-500:]
            self.last_failure_transient = bool(failed) and all(r.transient for r in failed)
        return done.returncode, done.stdout or "", clean_stderr

    def json(self, *args: str) -> object | None:
        """Run and parse, or None — an unparseable answer is unknown, never empty."""
        code, out, _err = self.run(*args)
        if code != 0:
            return None
        try:
            return json.loads(out or "null")
        except ValueError:
            return None

    def current_rate_limits(
        self, *, max_age: float = rate_limits.READING_MAX_AGE_S
    ) -> dict[str, rate_limits.Reading]:
        """Return readings for the reserve gate, probing only when there are none that fresh.

        A webhook-quiet wake asks for `GATE_MAX_AGE_S` instead of the default freshness (#2082).
        The only thing the gate asks of the answer is whether the reserve has been reached, and a
        five-minute-old reading answers that conservatively and for free. At 30s the probe below
        fired on nearly every wake on this host, because a quiet wake makes no other call that
        could have left a fresher one behind.

        A *full* wake does not get that offer, and `rate_limits.GATE_MAX_AGE_S` says why: the
        `chatgpt-nudge` extension spends core quota without recording it, and only a quiet wake is
        one where it provably does not run.
        """
        try:
            environment = self._environment()
        except github_authority.Refusal:
            return {}
        credential = rate_limits.credential_identity(environment)
        store = rate_limits.RateLimitStore()
        rate_limits.bind_authority(store, environment, credential)
        readings = store.read(credential, max_age=max_age)
        if not store.is_bound(credential) or "core" not in readings:
            # An App installation token cannot read `GET /user`: every probe would be a 403
            # (#3344, #3711). Its own repository list answers with the same core headers.
            app = environment.get(github_authority.AUTHORITY_ENV, "").startswith("app:")
            self.run(*(("api", "installation/repositories?per_page=1") if app else ("api", "user")))
            readings = store.read(credential, max_age=max_age)
        if "graphql" not in readings:
            value = self.json("api", "rate_limit")
            store.record_estimate(credential, value)
            readings = store.read(credential, max_age=max_age)
        return readings


def _is_poller_comment(node: dict) -> bool:
    return BOT_MARKER in str(node.get("body") or "")


def _is_worker_report(node: dict) -> bool:
    """A worker's terminal report: the poller's own comment carrying a finish status (#2689).

    A ``resumed`` marker is not a report: it is a relayed chat answer or a "reopened" note,
    which says the work restarted rather than what was done, so it counts as a status note.
    """
    if not _is_poller_comment(node):
        return False
    return any(
        (match.group(1) or "").casefold() != RESUMED_TOKEN
        for match in _TERMINAL_STATUS_RE.finditer(str(node.get("body") or ""))
    )


def _is_poller_notice(node: dict) -> bool:
    """The poller's other comments — Releasing, Merged, Done and the like (#2689)."""
    return _is_poller_comment(node) and not _is_worker_report(node)


def _is_review_comment(node: dict) -> bool:
    """One of the ChatGPT reviewer's `/claude` findings handoffs, and never a person's (#2167).

    A person's own `/claude` instruction has no findings heading, so it stays visible however
    old; a comment carrying the poller's marker is the poller's, whatever it quotes.
    """
    body = str(node.get("body") or "")
    if BOT_MARKER in body:
        return False
    return CHATGPT_REVIEW_MARKER in body or bool(_REVIEW_HEADING_RE.match(body.lstrip()))


def _loads(text: str) -> object | None:
    """Parse a `gh` answer, or None — used where the exit code alone is not the whole story."""
    try:
        return json.loads(text or "null")
    except ValueError:
        return None


# How `gh` words a failure that is GitHub's or the network's rather than the credential's. The
# API's own refusals arrive as `gh: <message> (HTTP <status>)`; a transport failure arrives in
# Go's wording. Both are matched as lowercased text because neither is a documented interface,
# so an unrecognized failure is treated as unanswered rather than as a permissions problem.
_TRANSIENT_FAILURES = (
    "rate limit",
    "abuse detection",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "no such host",
    "temporary failure in name resolution",
    "error connecting",
    "dial tcp",
    "unexpected eof",
)

# A failure's own words travel into the activation report, which the promotion gate prints one
# line of into `deploy.log`. Long enough to name the failure, short enough to leave the report's
# own wording room on that line.
_DETAIL_CHARS = 120

# How `gh` words a repository these credentials genuinely may not see. A 404 is deliberately not
# here: GitHub hides a private repository behind one, which the absent branch already says.
_DENIED_FAILURES = (
    "http 401",
    "http 403",
    "bad credentials",
    "requires authentication",
    "resource not accessible",
    "must have admin rights",
    "saml enforcement",
)


def _classify_repo_failure(code: int, blob: str) -> Reachability:
    """Which kind of "no" a failed repository read was (#707).

    A rate-limited read and a token that may not see the repository are both non-zero exits
    carrying HTTP 403, so the quota wording is what separates them and is tested first.
    """
    text = (blob or "").lower()
    if "not found" in text or "404" in text:
        return Reachability.ABSENT
    if any(marker in text for marker in _TRANSIENT_FAILURES):
        return Reachability.UNAVAILABLE
    if any(marker in text for marker in _DENIED_FAILURES):
        return Reachability.DENIED
    log.debug("unrecognized gh repository failure (exit %s): %s", code, blob)
    return Reachability.UNAVAILABLE


def _failure_detail(err: str, out: str) -> str:
    """The adapter's own words for a failure, in one short line fit for an activation report."""
    for blob in (err, out):
        for line in (blob or "").splitlines():
            stripped = line.strip().removeprefix("gh: ").strip()
            if stripped:
                if len(stripped) <= _DETAIL_CHARS:
                    return stripped
                return stripped[: _DETAIL_CHARS - 1] + "…"
    return ""


def _blocked_by_cursor(page: object) -> str | None:
    """Where a dependency connection continues, or ``None`` when the page was all of it.

    The empty string means "there is more and this answer cannot say where", which is the one
    case that must not read as "that was all of them": the caller holds the card instead.
    """
    info = (page.get("pageInfo") if isinstance(page, dict) else None) or {}
    if not isinstance(info, dict) or not info.get("hasNextPage"):
        return None
    cursor = info.get("endCursor")
    return cursor if isinstance(cursor, str) and cursor else ""


def _blocker_parts(ref: str, org: str) -> tuple[str, str, int]:
    """Split ``[owner/]repo#number`` into the three GraphQL variables it needs."""
    owner, _, rest = ref.rpartition("/")
    repo, _, number = rest.partition("#")
    try:
        which = int(number)
    except ValueError:
        which = 0
    return (owner or org), repo, which


def _scheduled_task(repo: str, node: object) -> schedules.TaskIssue | None:
    """One sweep node as a task, or None when it is not a readable issue."""
    if not isinstance(node, dict):
        return None
    try:
        number = int(node.get("number"))
    except (TypeError, ValueError):
        return None
    author = node.get("author") if isinstance(node.get("author"), dict) else {}
    editor = node.get("editor") if isinstance(node.get("editor"), dict) else {}
    return schedules.TaskIssue(
        repo=repo,
        number=number,
        title=str(node.get("title") or ""),
        body=str(node.get("body") or ""),
        open=str(node.get("state") or "").upper() == "OPEN",
        author=str(author.get("login") or ""),
        editor=str(editor.get("login") or ""),
    )


def _only_missing_tracked(value: object) -> bool:
    """True when a sweep answer carries no errors but "this tracked issue does not exist".

    GraphQL reports a deleted issue as an error beside a null field. That one is an answer
    about the issue; any other error means the request itself was not read whole.
    """
    errors = value.get("errors") if isinstance(value, dict) else None
    if not errors:
        return True
    for error in errors if isinstance(errors, list) else [errors]:
        if not isinstance(error, dict) or error.get("type") != "NOT_FOUND":
            return False
        path = error.get("path")
        if not (isinstance(path, list) and path and str(path[0]).startswith("t")):
            return False
    return True


def _before(stamp: str, other: str) -> bool:
    """Is ``stamp`` strictly earlier than ``other``? Unparseable answers no.

    Used to decide whether a listing reached back past an intent, so an unreadable timestamp must
    not count as "reached back" — that would turn a bounded page into a false proof of absence.
    """
    try:
        left = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        right = datetime.fromisoformat(other.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return left < right


def _valid_timestamp(value: object) -> bool:
    """Whether a GitHub timestamp is a parseable, timezone-aware instant."""
    if not isinstance(value, str):
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return stamp.tzinfo is not None


def _age_seconds(stamp: str | None, now: datetime) -> float:
    try:
        moment = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (now.astimezone(UTC) - moment).total_seconds())


def _usage_snapshot(
    usage_cache: Path | None, account: str, *, now: datetime | None = None
) -> dict | None:
    """This account's current published usage reading, or ``None`` when there isn't one.

    The deck publishes provider-qualified snapshots plus a temporary label-only compatibility
    alias. A missing, mismatched, unparseable or stale file is all the same answer here: the
    reader does not know. Callers decide what ignorance means to them.
    """
    if usage_cache is None:
        return None
    provider, _, label = account.partition(":")
    paths = [Path(usage_cache) / f"usage-{provider}-{label}.json"] if label else []
    paths.append(Path(usage_cache) / f"usage-{label or provider}.json")
    raw = None
    for path in paths:
        try:
            candidate = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(candidate, dict) and candidate.get("account") == account:
            raw = candidate
            break
    if raw is None:
        return None
    try:
        fetched = datetime.fromisoformat(str(raw.get("fetched_at", "")))
    except ValueError:
        return None
    if ((now or datetime.now(UTC)) - fetched).total_seconds() > USAGE_MAX_AGE_S:
        return None  # a stale snapshot is not evidence of anything
    return raw


def _reported_percentages(raw: dict) -> list[float]:
    return [
        value
        # ``month_pct`` is the billing-cycle meter (#730) and is absent from
        # every cache file written before it existed, which reads as "no such
        # window" rather than as zero.
        for _window, key, _resets in USAGE_WINDOW_KEYS
        if isinstance(value := raw.get(key), int | float)
    ]


def account_window_readings(
    usage_cache: Path | None,
    account: str,
    *,
    now: datetime | None = None,
    planned_reset: datetime | None = None,
) -> tuple[tuple[str, float, datetime], ...]:
    """Every window this account reports with **both** a percentage and a reset boundary.

    ``account_usage_pct`` answers "how full", which needs no boundary. A caller asking how
    fast a window is being spent needs the boundary too, and a window missing one is left
    out rather than paired with a guessed one. ``planned_reset`` is a person's planned reset
    of this account (#3379), which the caller reads from the deck's database; it ends the
    budget windows early.
    """
    raw = _usage_snapshot(usage_cache, account, now=now)
    if raw is None:
        return ()
    moment = now or datetime.now(UTC)
    planned = planned_reset if planned_reset is not None and planned_reset > moment else None
    readings = []
    for window, pct_key, resets_key in USAGE_WINDOW_KEYS:
        pct = raw.get(pct_key)
        if not isinstance(pct, int | float) or isinstance(pct, bool):
            continue
        try:
            resets_at = datetime.fromisoformat(str(raw.get(resets_key, "")))
        except ValueError:
            continue
        if resets_at.tzinfo is None:
            continue  # an instant we cannot place is not a boundary
        if planned is not None and window in _BUDGET_WINDOW_SECONDS:
            resets_at = min(resets_at, planned)
        readings.append((window, float(pct), resets_at))
    return tuple(readings)


# How long each window lasts. Only the windows that are a *budget* are here: the five-hour
# window is a rate limit that refills on its own, so leaving it part spent costs the operator
# nothing and chasing it would flip accounts every few hours for no gain.
_BUDGET_WINDOW_SECONDS = {
    "seven_day": 7 * 24 * 3600.0,
    "month": 30 * 24 * 3600.0,
}


def _planned_target(target_pct: float, planned_reset: datetime | None, now: datetime) -> float:
    """The target, raised to the whole window while a planned reset is ahead (#3379).

    Whatever is left at the reset is lost, so the aim is every point the CLI will still run.
    A target of 0 is the operator opting the account out, and a plan does not override it.
    """
    if target_pct <= 0 or planned_reset is None or planned_reset <= now:
        return target_pct
    return max(target_pct, REFUSAL_PCT)


def account_expiring_headroom(
    usage_cache: Path | None,
    account: str,
    *,
    target_pct: float = USAGE_TARGET_PCT,
    now: datetime | None = None,
    planned_reset: datetime | None = None,
) -> float:
    """How many percentage points this account is about to lose unspent, or ``0.0``.

    This is not the budget rule #1497 removed coming back the other way round. That rule
    *withheld* work to protect a window; this one withholds nothing and refuses nothing. It
    answers which of two accounts that are both going to receive work should receive it first,
    and every account it ranks is one the caller was already willing to dispatch to.

    A window only counts while its reset is close enough that the remainder cannot plausibly
    be spent afterwards, and only a window that is a purchased budget counts at all. An
    account already at or past the target scores ``0.0`` and so does one the reader knows
    nothing about — ignorance must not promote an account over a measured one.
    """
    moment = now or datetime.now(UTC)
    target_pct = _planned_target(target_pct, planned_reset, moment)
    if target_pct <= 0:
        return 0.0
    shortfall = 0.0
    for window, pct, resets_at in account_window_readings(
        usage_cache, account, now=moment, planned_reset=planned_reset
    ):
        length = _BUDGET_WINDOW_SECONDS.get(window)
        if length is None:
            continue
        remaining = (resets_at - moment).total_seconds()
        if remaining <= 0 or remaining > length * EXPIRY_HORIZON_RATIO:
            continue
        shortfall = max(shortfall, target_pct - pct)
    return max(0.0, shortfall)


def account_budget_pace(
    usage_cache: Path | None,
    account: str,
    *,
    target_pct: float = USAGE_TARGET_PCT,
    now: datetime | None = None,
    planned_reset: datetime | None = None,
) -> float | None:
    """Points of purchased budget this account has left per hour until it resets (#2757).

    ``target - pct`` divided by the hours to the reset, for the budget windows only, and the
    largest of them when there are two. An account at or past its target answers ``0.0``. It
    answers ``None`` when there is no fresh reading of a budget window with a reset still ahead,
    so a caller can tell ignorance from an account with nothing left to spend.

    Like ``account_expiring_headroom`` this only ranks accounts that are all going to be offered
    work; it never withholds any.
    """
    moment = now or datetime.now(UTC)
    target_pct = _planned_target(target_pct, planned_reset, moment)
    pace: float | None = None
    for window, pct, resets_at in account_window_readings(
        usage_cache, account, now=moment, planned_reset=planned_reset
    ):
        if window not in _BUDGET_WINDOW_SECONDS:
            continue
        hours = (resets_at - moment).total_seconds() / 3600
        if hours <= 0:
            continue
        rate = max(0.0, target_pct - pct) / hours if target_pct > 0 else 0.0
        pace = rate if pace is None else max(pace, rate)
    return pace


def account_usage_pct(
    usage_cache: Path | None, account: str, *, now: datetime | None = None
) -> float | None:
    """The fullest window this account actually reports, or ``None`` when unknown.

    This is the measured number only — never an estimate of what running sessions might go on
    to spend. A caller comparing accounts to each other needs the reading it can defend, and
    ``account_has_room`` deliberately blends in a forward-looking reserve that this must not.
    """
    raw = _usage_snapshot(usage_cache, account, now=now)
    if raw is None:
        return None
    percentages = _reported_percentages(raw)
    return max(percentages) if percentages else None


def account_refusal_detail(
    usage_cache: Path | None, account: str, *, now: datetime | None = None
) -> tuple[str, datetime | None]:
    """``account_refusal``'s reason, paired with the instant that refusal clears.

    A caller with nowhere unblocked to send a turn still has to send it somewhere, and the
    account that will be able to run soonest is the least bad of the bad choices. That needs
    the boundary, not just the reason.

    The instant is the **latest** reset among the windows that refuse, because the account
    cannot run a turn until every one of them has reset. It is ``None`` when the account is
    not refusing at all, and also when it refuses for a reason with no readable boundary —
    the two are told apart by the reason string, which is empty only in the first case.
    """
    moment = now or datetime.now(UTC)
    reason = ""
    clears_at: datetime | None = None
    for window, pct, resets_at in account_window_readings(usage_cache, account, now=moment):
        if pct < REFUSAL_PCT or resets_at <= moment:
            continue
        if not reason:
            reason = f"{window} window at {pct:.0f}% until {resets_at.isoformat()}"
        if clears_at is None or resets_at > clears_at:
            clears_at = resets_at
    return reason, clears_at


def account_refusal(
    usage_cache: Path | None, account: str, *, now: datetime | None = None
) -> str:
    """Why this account will refuse the next turn outright, or ``""`` when it will not.

    This is not the budget rule #1497 removed, and it must not grow back into one. *Do not
    withhold work to save a window* is gone and stays gone. This answers a different
    question: will the CLI accept a turn at all? A window reported at 100% with its reset
    still in the future is a fact about the next three seconds — the delivery lands, the CLI
    answers ``usage_limit_exceeded``, and the card is left In progress with an empty chat
    (#1519). Skipping such an account withholds nothing; it moves the same work somewhere it
    can actually run.

    Ignorance answers ``""``. A missing, unparseable or stale cache is not evidence that an
    account is blocked, and ``_usage_snapshot`` already refuses to read a stale one.
    """
    return account_refusal_detail(usage_cache, account, now=now)[0]


def account_has_room(
    usage_cache: Path | None,
    account: str,
    *,
    ceiling: float = USAGE_CEILING_PCT,
    five_hour_reserve_pct: float = 0.0,
    now: datetime | None = None,
) -> bool:
    """Compatibility helper: model usage no longer withholds work (#1497)."""
    del usage_cache, account, ceiling, five_hour_reserve_pct, now
    return True


@dataclass
class GitHubAdapter:
    """The read/effect half of ``EnginePort``, over `gh`."""

    org: str
    issue_repos: tuple[str, ...]
    code_repos: tuple[str, ...]
    project_number: int
    status_names: dict = field(default_factory=dict)   # Role -> the board's own spelling
    merge_method: str = "merge"
    api: Gh = field(default_factory=Gh)                # the instance's one authority (#2289)
    # Kept as a second name for the same path so existing call sites read unchanged; left
    # unset it *is* ``api``. There is no separate host login any more.
    host: Gh | None = None
    binding: object | None = None                      # the activation's resolved ids (#177)
    receipts: object | None = None                     # where acknowledgement receipts live
    now: datetime | None = None
    worker_accounts: tuple[str, ...] = ()              # the fallback order from the Overlay
    usage_ceilings: dict[str, float] = field(default_factory=dict)
    usage_cache: Path | None = None      # where the deck publishes provider-qualified usage files
    usage_ceiling_pct: float = USAGE_CEILING_PCT
    # Which label means "this engine owns the card". The Manifest names it, because it is
    # portable semantic configuration about the project's world (#108) rather than something
    # the shared engine gets to decide for every project at once.
    ownership_label: str = OWNERSHIP_LABEL
    ownership_labels: tuple[str, ...] = ()
    spec_only_label: str = ""
    paused_label: str = ""  # the label that pauses a card (#2548), "" for none
    # The body marker that takes a pull request out of every card's release set (#1445). A
    # follow-up stacked on the PR it follows says so here rather than in prose, because prose
    # gets reworded and a card that silently starts trying to merge a follow-up cannot be
    # steered off it. A field rather than a constant so a test or an embedder can spell it
    # differently; no manifest sets it, because the marker is the engine's own vocabulary and
    # a project spelling it its own way would only break the skills that write it.
    release_optout_marker: str = RELEASE_OPTOUT_MARKER
    release_step_marker: str = RELEASE_STEP_MARKER
    jobs_path: Path = field(
        default_factory=lambda: Path("~/.local/share/agentdeck/jobs/jobs.json").expanduser()
    )
    agentdeck_url: str = ""
    # The persistent runtime's host-local socket (#2144). Handed in by the wake as a path; the
    # engine knows only the runtime's HTTP answer, never its storage or code.
    runtime_socket: str = ""
    # Whether this project tracks the three release bits (#2101). It reaches the adapter only
    # to decide whether `pr_states` pays for the branch comparison `behind_by` needs: that is
    # one extra REST read per open pull request per probe, and a project that never opted in
    # would be paying it for a field nothing it runs will look at. Off, the field stays None
    # and every classifier falls back to exactly the signals it used before.
    release_bits: bool = False
    # The project's rendered control guide, appended to every comment this engine posts (#373).
    # It lands here rather than at each call site because "every bot comment" is only true if
    # there is one place it can be true — the engine composes a dozen comment bodies across the
    # cycle and the worker API, and a guide added per call site is a guide someone forgets.
    comment_guide: str = ""
    graphql_reserve: int = DEFAULT_GRAPHQL_RESERVE
    core_reserve: int = DEFAULT_CORE_RESERVE
    _graphql_remaining: int | None = field(default=None, init=False, repr=False)
    _core_remaining: int | None = field(default=None, init=False, repr=False)
    _graphql_source: str = field(default="", init=False, repr=False)
    _core_source: str = field(default="", init=False, repr=False)
    _rate_limits_checked: bool = field(default=False, init=False, repr=False)
    # One wake may need both event ids and the actor behind the newest one. Keep the exact
    # successful response so authorization does not spend a second REST call for the same card.
    _event_histories: dict[str, list[dict]] = field(default_factory=dict, init=False, repr=False)
    # Whether this GitHub still serves ``Issue.blockedBy``. Asked for by default and turned off
    # for the rest of the process the first time the API says the field does not exist, because
    # the alternative is one retired field emptying every card snapshot the wake takes (#640).
    _blocked_by_served: bool = field(default=True, init=False, repr=False)
    # Each Run's execution outcome as this wake read it, so the prompt check (#2887) and the
    # ending read share one ``/executions/outcome`` request per card. An adapter serves one
    # wake; the one stop this adapter asks for forgets its card's entry.
    _execution_reads: dict[tuple[str, int, str], dict | None] = field(
        default_factory=dict, init=False, repr=False
    )

    # --- budget ---

    def __post_init__(self) -> None:
        if self.host is None:
            self.host = self.api

    def dispatch_ok(self) -> bool:
        """Model-account usage never blocks a worker dispatch (#1497)."""
        return True

    def account_full(self, account: str) -> bool:
        """Is this one account provably over its ceiling? (See ``EnginePort.account_full``.)

        Fails *closed* on ignorance in the sense that matters here: not knowing answers
        "no", so a revive stays on the host's ordinary account order rather than being
        pushed onto the fallback by a stopped usage collector.
        """
        return False

    def live_queries_halt(self, *, stale_reading_ok: bool = False) -> str | None:
        """Keep all live phases behind the shared REST core and GraphQL reserves.

        ``stale_reading_ok`` is the cycle saying this wake is webhook-quiet, so nothing on it can
        spend core quota without recording it and an older reading may answer the gate for free
        (#2082). The default is the careful one: a wake that did not say so gets a fresh probe.
        """
        self._probe_rate_limits(stale_reading_ok=stale_reading_ok)
        if self._core_remaining is not None and self._core_remaining < self.core_reserve:
            detail = f": {self._core_source}" if self._core_source else ""
            return f"core-budget{detail}"
        if not self._graphql_has_room():
            detail = f": {self._graphql_source}" if self._graphql_source else ""
            return f"graphql-budget{detail}"
        return None

    def _has_room(self, account: str) -> bool:
        del account
        return True

    # --- small helpers ---

    def _slug(self, repo: str) -> str:
        return f"{self.org}/{repo}"

    @property
    def accepted_ownership_labels(self) -> tuple[str, ...]:
        accepted: list[str] = []
        seen: set[str] = set()
        for label in (self.ownership_label, *self.ownership_labels):
            identity = _label_identity(label)
            if identity not in seen:
                seen.add(identity)
                accepted.append(label)
        return tuple(accepted)

    def _moment(self) -> datetime:
        return self.now or datetime.now(UTC)

    @property
    def boardless(self) -> bool:
        """No GitHub Project behind this project: status is a `status:*` label (#1481)."""
        return self.project_number == 0

    def _marker_role(self, marker: str | None) -> str | None:
        """The board role a worker's terminal marker names, or None when it names none (#1939).

        A marker is the worker's own finish token, so it is read against the closed vocabulary of
        endings first and only then against the board's column spellings — ``resolved`` means
        *Done* and matches no column, and `_role_of` hands an unmatched word straight back, which
        is how it reached a status write no binding could satisfy. An ending nobody defined
        answers None: the recovery path then leaves the card alone rather than retrying a write
        that cannot land, on every wake, forever.
        """
        if not marker:
            return None
        mapped = role_of_finish_token(marker)
        if mapped:
            return mapped
        # `_role_of` hands an unmatched word back unchanged, which is right for a board column
        # nobody has taught it about and wrong for a marker: here an unrecognised word is not a
        # status at all. Only a resolved role survives.
        column = self._role_of(marker)
        return column if column in _ROLE_VALUES else None

    def _role_of(self, display: str | None) -> str | None:
        """Map the board's own spelling onto the role the Manifest named (case-insensitively)."""
        if not display:
            return None
        wanted = str(display).strip().casefold()
        for role in Role:
            named = str(self.status_names.get(role, role.value)).strip().casefold()
            if wanted in {named, role.value.casefold()}:
                return role.value
        return str(display)

    # --- authorization input (#110: the engine decides what this means) ---

    def permission(self, repo: str, login: str) -> str | None:
        """The collaborator level GitHub reports, or None when it could not be read."""
        if not login:
            return None
        value = self.api.json(
            "api", f"repos/{self._slug(repo)}/collaborators/{login}/permission"
        )
        if not isinstance(value, dict):
            return None
        level = value.get("permission")
        return str(level) if isinstance(level, str) else None

    # --- intake ---

    def issue_comments(self, repo: str, number: int) -> list[dict] | None:
        """Every comment on one issue, oldest first, or None when it could not be read (#2845).

        The card snapshot reads only the newest 100, which is enough for a wake but not for a
        worker's ``reopen``: its own report must still count as the newest one however many
        ordinary comments came after it. A person asks for that rarely, so the whole history is
        paged here rather than widening every wake's query.
        """
        code, out, _err = self.api.run(
            "api",
            "--paginate",
            f"repos/{self._slug(repo)}/issues/{int(number)}/comments?per_page=100",
        )
        if code != 0:
            return None
        pages = _json_pages(out or "")
        if pages is None:
            return None
        return [item for page in pages for item in page if isinstance(item, dict)]

    def scan_comments(self, *, since: str) -> ScanResult:
        """Every candidate command comment across issue and code repositories.

        A repository that cannot be listed makes the scan incomplete rather than short: the
        engine holds its cursor, so nothing is skipped, and the rest of the scan still counts.
        GitHub exposes pull-request conversation comments through the same endpoint. Those
        comments are rewritten onto their one native closing issue, so a command on the PR
        steers the existing card instead of inventing a card for the PR number.
        """
        comments: list[dict] = []
        complete = True
        pull_cards: dict[tuple[str, int], tuple[tuple[str, int] | None, bool]] = {}
        for repo in dict.fromkeys((*self.issue_repos, *self.code_repos)):
            value = self.api.json(
                "api",
                "--paginate",
                f"repos/{self._slug(repo)}/issues/comments"
                f"?since={quote(str(since), safe='')}&per_page=100",
            )
            if not isinstance(value, list):
                complete = False
                continue
            for raw in value:
                if not isinstance(raw, dict):
                    continue
                pull_number = self._comment_pull_number(raw, repo)
                if pull_number is None or not str(raw.get("body") or "").lstrip().startswith("/"):
                    comments.append({**raw, "repo": repo})
                    continue
                key = (repo, pull_number)
                if key not in pull_cards:
                    pull_cards[key] = self._pull_request_command_card(repo, pull_number)
                mapped, known = pull_cards[key]
                if not known:
                    complete = False
                    continue
                if mapped is None:
                    continue
                card_repo, card_number = mapped
                comments.append(
                    {
                        **raw,
                        "repo": card_repo,
                        # GitHub computed this association against the PR repository. Once the
                        # command targets an issue elsewhere, only a permission read against that
                        # issue repository is valid authorization evidence.
                        "author_association": (
                            raw.get("author_association") if card_repo == repo else None
                        ),
                        "issue_url": (
                            f"https://api.github.com/repos/{self.org}/{card_repo}"
                            f"/issues/{card_number}"
                        ),
                        "pull_request_repo": repo,
                        "pull_request_number": pull_number,
                    }
                )
        return ScanResult(comments=comments, complete=complete, scanned_to=self._moment())

    def _comment_pull_number(self, comment: dict, repo: str) -> int | None:
        """The PR number named by a repository-wide issue-comment payload, if it is a PR."""
        try:
            parsed = urlsplit(str(comment.get("html_url") or ""))
            parts = [part for part in parsed.path.split("/") if part]
            if (
                parsed.hostname != "github.com"
                or len(parts) < 4
                or parts[:3] != [self.org, repo, "pull"]
            ):
                return None
            return int(parts[3])
        except (TypeError, ValueError):
            return None

    def _pull_request_command_card(
        self, repo: str, number: int
    ) -> tuple[tuple[str, int] | None, bool]:
        """Return a PR's one poller issue, plus whether GitHub answered completely.

        A PR may close several issues. One command cannot safely choose among several cards, so
        only one closing issue in this Poller Project is routable. Zero or many leaves the PR
        itself as the command target only when its repository also holds issue cards; a code-only
        repository has no matching poller card. An unreadable or truncated connection is unknown
        and holds the scan cursor.
        """
        if not self._graphql_has_room():
            return None, False
        query = (
            "query($owner:String!,$repo:String!,$number:Int!){"
            "rateLimit{remaining resetAt cost}"
            "repository(owner:$owner,name:$repo){pullRequest(number:$number){"
            f"closingIssuesReferences(first:{PULL_REQUEST_CLOSING_ISSUES}){{"
            "nodes{number repository{name owner{login}}}pageInfo{hasNextPage}}}}}"
        )
        value = self.api.json(
            "api", "graphql", "-f", "query=" + query,
            "-f", "owner=" + self.org, "-f", "repo=" + repo,
            "-F", "number=" + str(number),
        )
        if isinstance(value, dict):
            self._remember_graphql_budget(value)
        try:
            connection = value["data"]["repository"]["pullRequest"][
                "closingIssuesReferences"
            ]
            nodes = connection["nodes"]
            has_next = connection["pageInfo"]["hasNextPage"]
        except (KeyError, TypeError):
            return None, False
        if has_next is not False or not isinstance(nodes, list):
            return None, False
        targets: list[tuple[str, int]] = []
        for node in nodes:
            repository = node.get("repository") if isinstance(node, dict) else None
            owner = repository.get("owner") if isinstance(repository, dict) else None
            card_repo = repository.get("name") if isinstance(repository, dict) else None
            card_owner = owner.get("login") if isinstance(owner, dict) else None
            card_number = node.get("number") if isinstance(node, dict) else None
            if (
                str(card_owner or "").casefold() == self.org.casefold()
                and card_repo in self.issue_repos
                and isinstance(card_number, int)
                and not isinstance(card_number, bool)
            ):
                targets.append((str(card_repo), card_number))
        unique = list(dict.fromkeys(targets))
        if len(unique) == 1:
            return unique[0], True
        return ((repo, number) if repo in self.issue_repos else None), True

    # --- bounded live card snapshots ---

    def snapshot_cards(self, cards: dict[str, CardQuery]) -> SnapshotBatch:
        """Read metadata, comments and project status in bounded GraphQL batches.

        A failed chunk contributes no cards; successful chunks remain useful. Missing nodes are
        omitted, never interpreted as closed. The REST rate-limit view costs no GraphQL points,
        so a low shared budget can defer this whole live-query phase before it spends any more.
        """
        if not cards:
            return SnapshotBatch()
        if not self._graphql_has_room():
            return SnapshotBatch(deferred=True)

        found: dict[str, CardSnapshot] = {}
        written: dict[str, tuple[str, ...]] = {}
        truncated: dict[str, tuple[CardQuery, str]] = {}
        items = list(cards.items())
        for offset in range(0, len(items), SNAPSHOT_BATCH_SIZE):
            chunk = items[offset:offset + SNAPSHOT_BATCH_SIZE]
            if not self._graphql_has_room():
                return SnapshotBatch(cards=self._all_blockers(found, written, truncated),
                                     deferred=True)
            value = self._snapshot_batch(chunk)
            if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
                continue
            self._remember_graphql_budget(value)
            data = value["data"]
            for index, (key, request) in enumerate(chunk):
                repository = data.get(f"r{index}")
                node = repository.get("card") if isinstance(repository, dict) else None
                if isinstance(node, dict):
                    found[key] = self._snapshot_from_node(request, node)
                    cursor = _blocked_by_cursor(node.get("blockedBy"))
                    if cursor is not None:
                        truncated[key] = (request, cursor)
                    declared = card_blockers.declared(
                        str(node.get("body") or ""),
                        org=self.org, repo=request.repo, number=request.number,
                    )
                    if declared:
                        written[key] = declared
        return SnapshotBatch(cards=self._all_blockers(found, written, truncated))

    def _all_blockers(
        self,
        found: dict[str, CardSnapshot],
        written: dict[str, tuple[str, ...]],
        truncated: dict[str, tuple[CardQuery, str]],
    ) -> dict[str, CardSnapshot]:
        """Complete each card's blockers: the dependency pages that did not fit, then the body's.

        Native first, so the written convention's de-duplication compares against the whole
        native list rather than only its first page.
        """
        self._with_paged_blockers(found, truncated)
        return self._with_written_blockers(found, written)

    def _with_paged_blockers(
        self, found: dict[str, CardSnapshot], truncated: dict[str, tuple[CardQuery, str]]
    ) -> None:
        """Read the dependency pages the card query could not hold, and fail closed if any resist.

        This costs a query only for the rare card that has more dependencies than one page, and
        nothing at all for every other card. A page that cannot be read — an API failure, the
        shared GraphQL reserve, or a card still going at the page cap — leaves the card holding
        on an unnameable dependency instead of starting past one nobody saw (#684).
        """
        for key, (request, start) in truncated.items():
            snapshot = found.get(key)
            if snapshot is None:
                continue
            extra: list[str] = []
            declared: list[str] = []
            cursor: str | None = start
            complete = False
            for _page in range(BLOCKED_BY_PAGES):
                if not cursor or not self._graphql_has_room():
                    break
                page = self._blocked_by_page(request, cursor)
                if page is None:
                    break
                nodes = page.get("nodes") or []
                for ref in self._blocker_refs(nodes):
                    if ref not in snapshot.blockers and ref not in extra:
                        extra.append(ref)
                for ref in self._blocker_refs(nodes, open_only=False):
                    if ref not in snapshot.dependencies and ref not in declared:
                        declared.append(ref)
                cursor = _blocked_by_cursor(page)
                if cursor is None:
                    complete = True
                    break
            if not complete:
                log.warning("dependencies of %s could not be read in full; holding the card", key)
                extra.append(card_blockers.UNREAD_DEPENDENCIES)
                declared.append(card_blockers.UNREAD_DEPENDENCIES)
            if extra or declared:
                found[key] = replace(
                    snapshot,
                    blockers=snapshot.blockers + tuple(extra),
                    dependencies=snapshot.dependencies + tuple(declared),
                )

    def _blocked_by_page(self, request: CardQuery, after: str) -> dict | None:
        """One further page of a card's dependencies, or None when it could not be read."""
        code, out, _err = self.api.run(
            "api", "graphql", "-f", f"query={BLOCKED_BY_PAGE_QUERY}",
            "-f", f"owner={self.org}", "-f", f"repo={request.repo}",
            "-F", f"number={request.number}", "-f", f"after={after}",
        )
        value = _loads(out) if code == 0 else None
        if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
            return None
        self._remember_graphql_budget(value)
        repository = value["data"].get("repository")
        node = repository.get("card") if isinstance(repository, dict) else None
        page = node.get("blockedBy") if isinstance(node, dict) else None
        return page if isinstance(page, dict) else None

    def _snapshot_batch(self, chunk: list[tuple[str, CardQuery]]) -> object | None:
        """One snapshot chunk, degrading once if this GitHub does not serve issue dependencies.

        The dependency field rides inside the card query so it is free, which also means a
        GitHub that retires or renames it would fail the *whole* query and leave every card in
        the chunk unreadable. So a failure that names the field is answered by asking again
        without it and never asking for it again, while an ordinary API failure is left exactly
        as it was — one retry per wake at most, and none for the blips that actually happen.
        """
        code, out, err = self._snapshot_call(chunk, blockers=self._blocked_by_served)
        value = _loads(out) if code == 0 else None
        if value is not None or not self._blocked_by_served:
            return value
        if "blockedby" not in (err or "").lower():
            return None
        self._blocked_by_served = False
        log.warning("GitHub does not serve Issue.blockedBy; native dependencies ignored: %s", err)
        code, out, _err = self._snapshot_call(chunk, blockers=False)
        return _loads(out) if code == 0 else None

    def _snapshot_call(
        self, chunk: list[tuple[str, CardQuery]], *, blockers: bool
    ) -> tuple[int, str, str]:
        query, variables = self._snapshot_query(chunk, blockers=blockers)
        return self.api.run("api", "graphql", "-f", f"query={query}", *variables)

    def _with_written_blockers(
        self, found: dict[str, CardSnapshot], written: dict[str, tuple[str, ...]]
    ) -> dict[str, CardSnapshot]:
        """Merge the bodies' declared blockers in, resolved in one extra bounded query.

        Native dependencies arrive with their state, so this runs only for the written
        convention and only when a card actually used it. Every ref this cannot *prove*
        closed — unresolved, unreadable, or naming nothing — is kept as blocking, because
        starting work on an unproven dependency is the failure this exists to prevent.
        """
        if not written:
            return found
        wanted = sorted({ref for refs in written.values() for ref in refs})
        states = self._card_states(wanted)
        for key, refs in written.items():
            snapshot = found.get(key)
            if snapshot is None:
                continue
            extra = tuple(
                ref for ref in refs
                if not card_blockers.is_resolved(states.get(ref))
                and ref not in snapshot.blockers
            )
            # A written declaration whose ref has closed is still a declaration, so it joins
            # the card's dependencies even though it holds nothing (#1588).
            declared = tuple(ref for ref in refs if ref not in snapshot.dependencies)
            if extra or declared:
                found[key] = replace(
                    snapshot,
                    blockers=snapshot.blockers + extra,
                    dependencies=snapshot.dependencies + declared,
                )
        return found

    def _card_states(self, refs: list[str]) -> dict[str, str]:
        """``OPEN``/``CLOSED``/``MERGED`` per ``[owner/]repo#number``; absent means unknown."""
        states: dict[str, str] = {}
        for offset in range(0, len(refs), SNAPSHOT_BATCH_SIZE):
            chunk = refs[offset:offset + SNAPSHOT_BATCH_SIZE]
            if not self._graphql_has_room():
                break
            declarations: list[str] = []
            fields: list[str] = ["rateLimit{remaining resetAt cost}"]
            variables: list[str] = []
            for index, ref in enumerate(chunk):
                owner, repo, number = _blocker_parts(ref, self.org)
                declarations.extend(
                    [f"$owner{index}:String!", f"$repo{index}:String!", f"$number{index}:Int!"]
                )
                fields.append(
                    f"b{index}:repository(owner:$owner{index},name:$repo{index}){{"
                    f"card:issueOrPullRequest(number:$number{index}){{"
                    "... on Issue{state}... on PullRequest{state}}}"
                )
                variables.extend(
                    ["-f", f"owner{index}={owner}", "-f", f"repo{index}={repo}",
                     "-F", f"number{index}={number}"]
                )
            query = f"query({','.join(declarations)}){{{''.join(fields)}}}"
            # Parsed whatever the exit code says: a ref naming a repository or issue that does
            # not exist is an *error plus partial data*, and discarding the whole answer would
            # let one typo hold every other card that declared a real, closed dependency.
            _code, out, _err = self.api.run("api", "graphql", "-f", f"query={query}", *variables)
            value = _loads(out)
            if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
                continue
            self._remember_graphql_budget(value)
            data = value["data"]
            for index, ref in enumerate(chunk):
                repository = data.get(f"b{index}")
                node = repository.get("card") if isinstance(repository, dict) else None
                state = node.get("state") if isinstance(node, dict) else None
                if isinstance(state, str) and state:
                    states[ref] = state
        return states

    def snapshot(
        self,
        repo: str,
        number: int,
        *,
        generation: str = "",
        stop_generation: str = "",
        stop_requested_at: str = "",
    ) -> CardSnapshot | None:
        """Compatibility singleton over the same bounded path used by the engine."""
        key = f"{repo}#{number}"
        result = self.snapshot_cards(
            {key: CardQuery(repo, number, generation=generation,
                            stop_generation=stop_generation,
                            stop_requested_at=stop_requested_at)}
        )
        return result.cards.get(key)

    def project_status(self, repo: str, number: int) -> str | None:
        snapshot = self.snapshot(repo, number)
        return snapshot.status if snapshot else None

    def _graphql_has_room(self) -> bool:
        self._probe_rate_limits()
        return self._graphql_remaining is None or self._graphql_remaining > self.graphql_reserve

    def _probe_rate_limits(self, *, stale_reading_ok: bool = False) -> None:
        if self._rate_limits_checked:
            return
        self._rate_limits_checked = True
        max_age = (
            rate_limits.GATE_MAX_AGE_S if stale_reading_ok else rate_limits.READING_MAX_AGE_S
        )
        readings = self.api.current_rate_limits(max_age=max_age)
        for name, attribute, source_attribute in (
            ("graphql", "_graphql_remaining", "_graphql_source"),
            ("core", "_core_remaining", "_core_source"),
        ):
            reading = readings.get(name)
            if reading is not None:
                setattr(self, attribute, reading.remaining)
                setattr(self, source_attribute, reading.source)

    def _remember_graphql_budget(self, payload: dict) -> None:
        remaining = (payload.get("data") or {}).get("rateLimit", {}).get("remaining")
        if isinstance(remaining, int) and not isinstance(remaining, bool):
            self._graphql_remaining = remaining
            self._graphql_source = "GraphQL response body from a real GitHub call"

    def _spend_graphql_points(self, points: int) -> None:
        """Charge the cached GraphQL reserve for a call that cannot report its own cost (#2116).

        Every read asks for `rateLimit` and hands the answer to ``_remember_graphql_budget``,
        so the cache refreshes itself. A mutation cannot: `rateLimit` lives on GitHub's Query
        type only. Without this the cache would stay one point too high until the next read,
        and ``_graphql_has_room`` would authorize work the real budget no longer covers. The
        count only ever goes down here, and never below zero, so a wrong guess errs towards
        doing less rather than towards spending a reserve that is not there.

        One point is what the retirement charges for its whole batched document, because GitHub
        bills a mutation as one call regardless of how many aliases it carries. If that ever
        turns out to undercount, the correction belongs here and nowhere else.
        """
        if isinstance(self._graphql_remaining, int) and points > 0:
            self._graphql_remaining = max(0, self._graphql_remaining - points)

    def _snapshot_query(
        self, chunk: list[tuple[str, CardQuery]], *, blockers: bool = True
    ) -> tuple[str, tuple[str, ...]]:
        declarations: list[str] = []
        fields: list[str] = ["rateLimit{remaining resetAt cost}"]
        variables: list[str] = []
        card_fields = (
            "state closedAt title body updatedAt "
            "labels(first:100){nodes{name}pageInfo{hasNextPage}} "
            # Comment identity travels with the body in the same call, so the authoritative
            # input bundle can name and fence an ordinary requester comment without the
            # snapshot costing a second query (#1473).
            "comments(last:100){nodes{id createdAt body author{login}}}"
        )
        if self.boardless:
            # Event order distinguishes a human's new status label from the stale one when both
            # are briefly present. The current-label connection's pageInfo lets that answer fail
            # closed if a card carries more labels than the snapshot read (#1481).
            card_fields += (
                f" statusEvents:timelineItems(last:{LABELLED_EVENTS_PER_ISSUE},"
                "itemTypes:[LABELED_EVENT]){nodes{... on LabeledEvent{"
                "id createdAt label{name}}}pageInfo{hasPreviousPage}}"
            )
        else:
            # A boardless project reads its status out of the labels this query already asks
            # for, so the Projects fragment is not merely unused — it is not requested (#1481).
            card_fields += (
                " projectItems(first:20){nodes{project{number} "
                'fieldValueByName(name:"Status")'
                "{... on ProjectV2ItemFieldSingleSelectValue{name}}}}"
            )
        # Only an Issue has dependencies, and the same fragment text serves both card kinds.
        issue_fields = f"{card_fields} {BLOCKED_BY_FIELD}" if blockers else card_fields
        for index, (_key, request) in enumerate(chunk):
            declarations.extend(
                [f"$owner{index}:String!", f"$repo{index}:String!", f"$number{index}:Int!"]
            )
            fields.append(
                f"r{index}:repository(owner:$owner{index},name:$repo{index}){{"
                f"card:issueOrPullRequest(number:$number{index}){{"
                f"... on Issue{{{issue_fields}}}... on PullRequest{{{card_fields}}}}}}}"
            )
            variables.extend(
                [
                    "-f", f"owner{index}={self.org}",
                    "-f", f"repo{index}={request.repo}",
                    "-F", f"number{index}={request.number}",
                ]
            )
        return f"query({','.join(declarations)}){{{''.join(fields)}}}", tuple(variables)

    def _snapshot_from_node(self, request: CardQuery, node: dict) -> CardSnapshot:
        requester_comments, requester_omitted = _requester_comments(
            [
                item for item in ((node.get("comments") or {}).get("nodes") or [])
                if isinstance(item, dict)
            ]
        )
        labels = [
            str(item.get("name") or "")
            for item in ((node.get("labels") or {}).get("nodes") or [])
            if isinstance(item, dict)
        ]
        label_identities = {_label_identity(label) for label in labels}
        comments = [
            item for item in ((node.get("comments") or {}).get("nodes") or [])
            if isinstance(item, dict)
        ]
        return CardSnapshot(
            chat_answer_ids=chat_answer_ids(comments),
            open=str(node.get("state") or "").upper() == "OPEN",
            closed_at=str(node.get("closedAt") or ""),
            owned=any(
                _label_identity(label) in label_identities
                for label in self.accepted_ownership_labels
            ),
            spec_only=(
                bool(self.spec_only_label)
                and _label_identity(self.spec_only_label) in label_identities
            ),
            paused=(
                bool(self.paused_label)
                and _label_identity(self.paused_label) in label_identities
            ),
            status=self._status_of(node),
            quiet_seconds=self._quiet_seconds(request, node.get("updatedAt")),
            terminal_marker=self._marker_role(
                self._marker(comments, TERMINAL_MARKER, request.generation)
            ),
            review_verdict=self._review_verdict(comments, request.generation),
            terminal_note=self._marker_note(comments, TERMINAL_MARKER, request.generation),
            terminal_at=self._marker_at(comments, TERMINAL_MARKER, request.generation),
            stopped_terminal_marker=(
                self._marker_role(self._marker(comments, TERMINAL_MARKER, request.stop_generation))
                if request.stop_generation and request.stop_generation != request.generation
                else None
            ),
            stopped_terminal_at=(
                self._marker_at(comments, TERMINAL_MARKER, request.stop_generation)
                if request.stop_generation and request.stop_generation != request.generation
                else ""
            ),
            execution_stopped=self._execution_stopped(
                request.repo, request.number, request.stop_generation
            ),
            run_ended=self._run_ended(request.repo, request.number, request.generation),
            title=str(node.get("title") or ""),
            body=str(node.get("body") or ""),
            comments=requester_comments,
            comments_omitted=requester_omitted,
            blockers=self._blocker_refs(self._native_nodes(node)),
            dependencies=self._blocker_refs(self._native_nodes(node), open_only=False),
        )

    def _status_of(self, node: dict) -> str:
        """The role this card is in: its `status:*` label when boardless, else our column.

        Both backends answer the same way when they cannot tell — an empty string, which the
        engine reads as "no status yet" rather than as any particular column.
        """
        if self.boardless:
            return self._boardless_status(node)
        return self._role_of(self._pick_item_status(node, self.project_number)) or ""

    @staticmethod
    def _boardless_status(node: dict) -> str:
        """Resolve one status label, using GitHub's event order when a human added another.

        The current labels alone cannot say which of two roles is new. The timeline connection is
        chronological and ends at the newest event; if it cannot identify a newest currently
        present status label, the live status is unknown rather than guessed from enum order.
        """
        current = node.get("labels")
        if not isinstance(current, dict):
            return ""
        page = current.get("pageInfo")
        items = current.get("nodes")
        if (
            not isinstance(page, dict)
            or page.get("hasNextPage") is not False
            or not isinstance(items, list)
            or any(
                not isinstance(item, dict) or not isinstance(item.get("name"), str)
                for item in items
            )
        ):
            return ""
        names = [str(item["name"]) for item in items]
        role = role_of_status_labels(names)
        if role is not None:
            return role.value
        wanted = {name.strip().casefold() for name in names}
        by_label = {
            status_label(role).casefold(): role
            for role in Role
            if status_label(role).casefold() in wanted
        }
        if len(by_label) < 2:
            return ""

        history = node.get("statusEvents")
        if not isinstance(history, dict):
            return ""
        history_page = history.get("pageInfo")
        events = history.get("nodes")
        if (
            not isinstance(history_page, dict)
            or history_page.get("hasPreviousPage") is not False
            or not isinstance(events, list)
        ):
            return ""
        for event in reversed(events):
            label = event.get("label") if isinstance(event, dict) else None
            if (
                not isinstance(event, dict)
                or not event.get("id")
                or not _valid_timestamp(event.get("createdAt"))
                or not isinstance(label, dict)
                or not isinstance(label.get("name"), str)
            ):
                return ""
            latest = by_label.get(str(label["name"]).strip().casefold())
            if latest is not None:
                return latest.value
        return ""

    def _quiet_seconds(self, request: CardQuery, updated_at: object) -> float:
        """How long the issue has been silent. Reported only, never a liveness signal (#2159)."""
        del request
        return _age_seconds(str(updated_at or ""), self._moment())

    def _execution_stopped(self, repo: str, number: int, generation: str) -> bool:
        """Has the Runtime proved the Run this card's stop waits on stopped? (#2159, #2200)

        Proof is the Runtime's own outcome for exactly that Run: a stop was asked of it, and
        nothing in it can still write. The host asks for the stop after the wake that marked
        the card stopping; until the Runtime has recorded that ask, even a Run that ended by
        itself is not released, because a later job notice could still resume its worker.
        Keyed by the stopped generation, so a later stop of the same card is its own question
        (#935). Unreadable is not stopped.
        """
        outcome = self._execution_outcome(repo, number, generation)
        return bool(
            isinstance(outcome, dict)
            and outcome.get("execution_state") in EXECUTION_SAFE_STATES
            and outcome.get("stop_requested_at")
        )

    def _execution_outcome(self, repo: str, number: int, generation: str) -> dict | None:
        """The Runtime's execution outcome for this card's Run, or None when unasked/unread."""
        if not generation or not self.runtime_socket or not str(self.org or "").strip():
            return None
        read = (repo, int(number), generation)
        if read in self._execution_reads:
            return self._execution_reads[read]
        query = urlencode({"work_id": card_work_id(self.org, repo, number), "run_id": generation})
        payload = _runtime_get(
            self.runtime_socket,
            f"{EXECUTION_OUTCOME_PATH}?{query}",
            timeout=EXECUTION_READ_TIMEOUT_S,
        )
        outcome = payload if isinstance(payload, dict) else None
        self._execution_reads[read] = outcome
        return outcome

    def _stop_abandoned(self, repo: str, number: int, generation: str) -> dict | None:
        """Have the Runtime stop a wedged Run; its outcome once stopped, else None (#1400).

        A Run the Runtime calls abandoned has done nothing for hours, but it is not over until
        the Runtime has ended it: a new Run would otherwise share the Work's workspace with a
        process that may still wake up and write. The stop is safe to repeat.
        """
        # The outcome read before this stop no longer describes the Run.
        self._execution_reads.pop((repo, int(number), generation), None)
        status, answer = runtime_post(
            self.runtime_socket,
            EXECUTION_STOP_PATH,
            {
                "work_id": card_work_id(self.org, repo, number),
                "run_id": generation,
                "reason": revive.ABANDONED,
            },
        )
        result = answer.get("result") if isinstance(answer, dict) else None
        execution = answer.get("execution") if isinstance(answer, dict) else None
        if status != 200 or result not in EXECUTION_STOP_PROVED or not isinstance(execution, dict):
            log.warning(
                "%s/%s#%s: worker wedged; the runtime has not stopped Run %s yet (%s)",
                self.org, repo, number, generation, result or status or "no answer",
            )
            return None
        return execution

    def _run_ended(self, repo: str, number: int, generation: str) -> bool:
        """Did the Runtime report this generation's execution over? (#2159)"""
        observed = self.turn_ended(repo, number, generation)
        return isinstance(observed, dict) and observed.get("observed") in revive.ENDINGS

    def _native_nodes(self, node: dict) -> list:
        """GitHub's own "blocked by" list for one card, as far as the card query read it.

        Whether that was the whole list is a separate question, answered by the connection's
        own page info and settled in ``_with_paged_blockers`` (#684).
        """
        listed = (node.get("blockedBy") or {}).get("nodes") or []
        return list(listed) if isinstance(listed, list) else []

    def _blocker_refs(self, nodes: Iterable, *, open_only: bool = True) -> tuple[str, ...]:
        """Dependency nodes as card refs, minus the entries already closed (#640).

        GitHub keeps a closed dependency listed, so the state is what matters, not membership.
        A node too damaged to name a card is dropped rather than turned into a hold nobody
        could act on — the written convention and the field's absence both degrade the same way.

        ``open_only=False`` keeps the closed ones too, which is the whole declared list rather
        than what is still holding the card. That is what tells a card whose dependency has
        landed apart from one nobody ever declared a dependency for (#1588).
        """
        found: list[str] = []
        for item in nodes:
            if not isinstance(item, dict):
                continue
            if open_only and card_blockers.is_resolved(item.get("state")):
                continue
            repository = item.get("repository") if isinstance(item.get("repository"), dict) else {}
            name = str(repository.get("name") or "").strip()
            owner = str(((repository.get("owner") or {}) if isinstance(
                repository.get("owner"), dict) else {}).get("login") or "").strip()
            number = item.get("number")
            if not name or not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                continue
            ref = (
                card_blockers.card_ref(name, number)
                if not owner or owner.casefold() == self.org.casefold()
                else f"{owner}/{name}#{number}"
            )
            if ref not in found:
                found.append(ref)
        return tuple(found)

    @staticmethod
    def _pick_item_status(node: dict, project_number: int) -> str | None:
        for item in (node.get("projectItems") or {}).get("nodes") or []:
            if not isinstance(item, dict):
                continue
            if (item.get("project") or {}).get("number") != project_number:
                continue
            name = (item.get("fieldValueByName") or {}).get("name")
            if isinstance(name, str) and name:
                return name
        return None

    def _comments(self, repo: str, number: int) -> list[dict]:
        """Comments for read paths that are not part of card reconciliation."""
        value = self.api.json(
            "issue", "view", str(number), "-R", self._slug(repo), "--json", "comments"
        )
        if not isinstance(value, dict):
            return []
        return [item for item in value.get("comments") or [] if isinstance(item, dict)]

    @staticmethod
    def _marker(comments: list[dict], prefix: str, generation: str) -> str | None:
        """The value a lease-bound marker carries, or None when no comment matches this lease."""
        if not generation:
            return None
        needle = f"{prefix}{generation}"
        for comment in reversed(comments):
            body = str(comment.get("body") or "")
            if needle not in body:
                continue
            match = re.search(re.escape(needle) + r"\s+status=([^\s>]+)", body)
            return match.group(1).replace("_", " ") if match else "acknowledged"
        return None

    @staticmethod
    def _marker_at(comments: list[dict], prefix: str, generation: str) -> str:
        """When the comment a lease-bound marker is in was posted, or "" (#2448)."""
        if not generation:
            return ""
        needle = f"{prefix}{generation}"
        for comment in reversed(comments):
            if needle in str(comment.get("body") or ""):
                return str(comment.get("createdAt") or "")
        return ""

    @staticmethod
    def _marker_note(comments: list[dict], prefix: str, generation: str) -> str | None:
        """The words of the comment a lease-bound marker is in, before the marker (#2332).

        What the worker reported to a person: the marker and the board's footer after it are
        the engine's, not the worker's. Capped (``_park_text``), since the deck shows a line of
        it at most.
        """
        if not generation:
            return None
        needle = f"{prefix}{generation}"
        for comment in reversed(comments):
            body = str(comment.get("body") or "")
            if needle in body:
                text = body.split(needle, 1)[0].strip()
                return _park_text(text) if text else None
        return None

    @staticmethod
    def _review_verdict(comments: list[dict], generation: str) -> str | None:
        """The merge-review verdict in this readable card snapshot, bound to its lease."""
        if not generation:
            return None
        for comment in reversed(comments):
            body = str(comment.get("body") or "")
            match = re.search(
                re.escape(f"{REVIEW_MARKER}{generation} verdict=")
                + r"(pass|changes-needed)",
                body,
            )
            if match:
                return match.group(1)
        return None

    # --- ownership provenance ---

    def labelled_cards(self) -> dict[str, list[LabelEvent]] | None:
        """Current ownership-labelled issues and their events, across issue repositories.

        Event history rides the same bounded GraphQL sweep that discovers the cards. GitHub's
        event node ids distinguish edits that share one second, while their creation times let
        the engine separate an old omitted label from one added after its prior complete sweep.

        A repository that cannot be listed aborts the answer instead of shrinking it. A partial
        sweep is indistinguishable from "these are the labelled cards", and the caller would
        advance its ownership ledger past cards it never saw — so the whole sweep is unknown.

        Every issue repository rides one request. The sweep runs on every wake, so asking per
        repository made the fleet's largest project spend six GraphQL requests a wake on a
        label set that rarely moves — the single largest consumer of a shared hourly budget
        that was running out. Only a repository with a further page carries into a second
        round, so the common case is exactly one request for the whole project.
        """
        events: dict[str, list[LabelEvent]] = {}
        histories: dict[str, list[dict]] = {}
        pending = [
            (repo, label, "")
            for repo in self.issue_repos
            for label in self.accepted_ownership_labels
        ]
        for _round in range(LABELLED_ISSUE_PAGES):
            if not pending:
                break
            following: list[tuple[str, str, str]] = []
            for offset in range(0, len(pending), SNAPSHOT_BATCH_SIZE):
                chunk = pending[offset:offset + SNAPSHOT_BATCH_SIZE]
                if not self._graphql_has_room():
                    return self._sweep_incomplete(repo for repo, _label, _cursor in chunk)
                value = self._labelled_page(chunk)
                if isinstance(value, dict):
                    self._remember_graphql_budget(value)
                data = value.get("data") if isinstance(value, dict) else None
                if not isinstance(data, dict):
                    return self._sweep_incomplete(repo for repo, _label, _cursor in chunk)
                for index, (repo, label, _cursor) in enumerate(chunk):
                    listing = data.get(f"r{index}")
                    connection = (
                        listing.get("issues") if isinstance(listing, dict) else None
                    )
                    if not isinstance(connection, dict):
                        return self._sweep_incomplete([repo])
                    nodes = connection.get("nodes")
                    if not isinstance(nodes, list) or not self._labelled_nodes(
                        repo, label, nodes, events=events, histories=histories
                    ):
                        return self._sweep_incomplete([repo])
                    page_info = connection.get("pageInfo")
                    if not isinstance(page_info, dict):
                        return self._sweep_incomplete([repo])
                    has_next = page_info.get("hasNextPage")
                    if not isinstance(has_next, bool):
                        return self._sweep_incomplete([repo])
                    if not has_next:
                        continue
                    next_cursor = page_info.get("endCursor")
                    if not isinstance(next_cursor, str) or not next_cursor:
                        return self._sweep_incomplete([repo])
                    following.append((repo, label, next_cursor))
            pending = following
        if pending:
            # More pages than the bounded sweep reads is the same unknown as a failed read.
            return self._sweep_incomplete(repo for repo, _label, _cursor in pending)
        for card_events in events.values():
            card_events.sort(key=lambda event: event.created_at)
        self._event_histories.update(histories)
        return events

    def _sweep_incomplete(self, repos: Iterable[str]) -> None:
        """Name what could not be listed, and answer "unknown" for the whole sweep."""
        log.warning(
            "labelled-issue sweep failed listing %s (label %r); the sweep is"
            " incomplete and no card was seeded this wake",
            ", ".join(self._slug(repo) for repo in repos),
            ", ".join(self.accepted_ownership_labels),
        )
        return None

    def _labelled_page(self, chunk: list[tuple[str, str, str]]) -> object | None:
        """One bounded page of labelled issues for every repository in ``chunk`` at once."""
        declarations: list[str] = []
        fields = ["rateLimit{remaining resetAt cost}"]
        args = ["api", "graphql"]
        for index, (repo, label, cursor) in enumerate(chunk):
            declarations.extend([
                f"$owner{index}:String!", f"$repo{index}:String!", f"$label{index}:String!",
            ])
            args.extend([
                "-f", f"owner{index}={self.org}", "-f", f"repo{index}={repo}",
                "-f", f"label{index}={label}",
            ])
            after = ""
            if cursor:
                declarations.append(f"$after{index}:String!")
                args.extend(["-f", f"after{index}={cursor}"])
                after = f",after:$after{index}"
            fields.append(
                f"r{index}:repository(owner:$owner{index},name:$repo{index}){{"
                f"issues(first:{LABELLED_ISSUE_PAGE_SIZE}{after},states:OPEN,"
                f"labels:[$label{index}]){{nodes{{number"
                f" timelineItems(last:{LABELLED_EVENTS_PER_ISSUE},"
                "itemTypes:[LABELED_EVENT]){nodes{... on LabeledEvent{"
                "id createdAt label{name} actor{__typename login}}}pageInfo{hasPreviousPage}}}"
                "pageInfo{hasNextPage endCursor}}}"
            )
        query = f"query({','.join(declarations)}){{{''.join(fields)}}}"
        args[2:2] = ["-f", "query=" + query]
        return self.api.json(*args)

    def _labelled_nodes(
        self,
        repo: str,
        ownership_label: str,
        nodes: list,
        *,
        events: dict[str, list[LabelEvent]],
        histories: dict[str, list[dict]],
    ) -> bool:
        """Record one repository's labelled issues, or refuse the page as unreadable."""
        for item in nodes:
            number = item.get("number") if isinstance(item, dict) else None
            if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                return False
            key = f"{repo}#{number}"
            timeline = item.get("timelineItems")
            if not isinstance(timeline, dict) or not isinstance(timeline.get("nodes"), list):
                return False
            timeline_page = timeline.get("pageInfo")
            if not isinstance(timeline_page, dict) or not isinstance(
                timeline_page.get("hasPreviousPage"), bool
            ):
                return False
            if any(
                not isinstance(event, dict)
                or not isinstance(event.get("label"), dict)
                or not isinstance(event["label"].get("name"), str)
                or not _valid_timestamp(event.get("createdAt"))
                for event in timeline["nodes"]
            ):
                return False
            history = [
                {**event, "event": "labeled", "actor": _rest_actor(event.get("actor"))}
                for event in timeline["nodes"]
            ]
            histories[key] = history
            # A currently labelled issue whose ownership event predates this bounded window
            # is old standing state, not an actionable fresh addition. Publish it with no
            # event id so it cannot poison every other card's sweep. A later remove + re-add
            # lands in the recent window with a new id and is detected.
            known = {event.id for event in events.setdefault(key, [])}
            events[key].extend(
                LabelEvent(str(event.get("id")), event["createdAt"])
                for event in history
                if event.get("id")
                and str(event.get("id")) not in known
                and _is_label_added_event(event, ownership_label)
            )
        return True

    def ownership_actors(self, events: dict[str, str]) -> dict[str, str]:
        """Who created each exact ownership event the engine is about to authorize (#2128)."""
        actors: dict[str, str] = {}
        for key, event_id in events.items():
            value = self._event_histories.get(key)
            if value is None:
                repo, number = _split(key)
                fetched = self.api.json(
                    "api", f"repos/{self._slug(repo)}/issues/{number}/events?per_page=100"
                )
                if not isinstance(fetched, list):
                    continue
                value = [item for item in fetched if isinstance(item, dict)]
                self._event_histories[key] = value
            actors[key] = ""
            for item in value:
                if str(item.get("id") or "") != event_id:
                    continue
                actor = item.get("actor")
                actors[key] = (
                    str((actor or {}).get("login") or "") if isinstance(actor, dict) else ""
                )
                break
        return actors

    def ownership_removal(self, repo: str, number: int) -> LabelRemoval | None:
        """The latest removal of an accepted ownership label, placed among the issue's comments.

        One page of the issue's timeline, newest end, holding label removals and comments in
        the order GitHub recorded them. That order is what separates a comment from a removal
        stamped in the same second, which the stamps alone cannot (#2417). Reading from the
        newest end also keeps a long issue from pushing the removal off the page. Anything
        unreadable answers None.
        """
        if not self._graphql_has_room():
            return None
        query = (
            "query($owner:String!,$repo:String!,$number:Int!){"
            "rateLimit{remaining resetAt cost}"
            "repository(owner:$owner,name:$repo){issue(number:$number){"
            f"timelineItems(last:{OWNERSHIP_TIMELINE_READ},"
            "itemTypes:[UNLABELED_EVENT,ISSUE_COMMENT]){nodes{__typename "
            "... on UnlabeledEvent{createdAt label{name} actor{login}} "
            "... on IssueComment{databaseId}}}}}}"
        )
        value = self.api.json(
            "api", "graphql", "-f", "query=" + query,
            "-f", "owner=" + self.org, "-f", "repo=" + repo,
            "-F", "number=" + str(number),
        )
        if isinstance(value, dict):
            self._remember_graphql_budget(value)
        try:
            nodes = value["data"]["repository"]["issue"]["timelineItems"]["nodes"]
        except (KeyError, TypeError):
            return None
        if not isinstance(nodes, list):
            return None
        wanted = {_label_identity(label) for label in self.accepted_ownership_labels}
        removal: dict | None = None
        before: list[str] = []
        after: list[str] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("__typename") == "IssueComment":
                comment_id = node.get("databaseId")
                if isinstance(comment_id, int) and not isinstance(comment_id, bool):
                    after.append(str(comment_id))
                continue
            label = node.get("label")
            if isinstance(label, dict) and _label_identity(label.get("name")) in wanted:
                # A later removal replaces an earlier one: everything so far is before it.
                removal = node
                before.extend(after)
                after = []
        if removal is None:
            return LabelRemoval()
        actor = removal.get("actor")
        return LabelRemoval(
            created_at=str(removal.get("createdAt") or ""),
            actor=str(actor.get("login") or "") if isinstance(actor, dict) else "",
            comments_before=frozenset(before),
            comments_after=frozenset(after),
        )

    def linked_prs(self, repo: str, number: int) -> LinkedPRs | None:
        """This card's release target, discovered conservatively (never merge the wrong PR).

        A PR qualifies on GitHub's own closing link, on a deliberate fully-qualified reference in
        its body, or on the worker branch convention together with a bare reference. Any listing
        that fails makes the whole answer unknown, because a partial view could look like
        "nothing to release".

        The per-repository listing reads only the newest 100 pull requests, so the issue's own
        cross-references are read too and anything they name that the listing missed is fetched
        by number (#1467). Without that, a card whose work stands still while its code repository
        moves on drops out of the window and its own pull request becomes invisible, and `/merge`
        answers "I could not find a pull request linked to this card" about a pull request that
        is open and correctly referenced.

        A pull request that states its own card — a closing keyword, or the issue number in its
        worker branch — is refused for every card it does not state, ahead of the two weaker
        signals and behind GitHub's own link (#2095). On a board whose issues and code live in
        separate repositories those two weaker signals collapse into "does this card's `repo#n`
        appear in the body at all", and a passing mention of one card inside another card's pull
        request is then indistinguishable from the real thing.

        Two things disqualify a PR outright, ahead of every signal above, because both are the
        author saying *not this one* and neither can be outvoted by a reference GitHub inferred:
        being a draft, and carrying ``release_optout_marker``. They are withheld from the whole
        card, not merely from the merge — a reviewer told to read a stacked draft reviews commits
        that belong to the PR underneath it, and a card that reports a draft it can never release
        reads as work waiting on the engine.
        """
        card = self.host.json(
            "pr", "view", str(number), "-R", self._slug(repo),
            "--json", "number,state,mergeCommit,headRefOid,isDraft,body",
        )
        if isinstance(card, dict) and card.get("number"):
            # The card *is* a PR: its release target is exactly itself.
            return self._one_pr(repo, card)

        links = self._issue_links(repo, number)
        if links is None:
            return None
        native, referenced = links
        refs = (f"{repo}#{number}", f"/{repo}/issues/{number}")
        openings: dict[str, str | None] = {}
        merged: list[str] = []
        drafts: list[str] = []
        closed: list[str] = []
        steps: list[str] = []
        for code_repo in self.code_repos:
            listing = self.host.json(
                "pr", "list", "-R", self._slug(code_repo), "--state", "all", "--limit", "100",
                "--json", "number,headRefName,body,state,mergeCommit,headRefOid,isDraft",
            )
            if not isinstance(listing, list):
                return None
            listed = {pull.get("number") for pull in listing if isinstance(pull, dict)}
            older = self._referenced_pulls(
                code_repo, referenced.get(code_repo, set()) - listed
            )
            if older is None:
                return None
            for pull in (*listing, *older):
                if not isinstance(pull, dict) or not self._qualifies(
                    pull, code_repo, repo, native, refs, number
                ):
                    continue
                ref = f"{code_repo}#{pull.get('number')}"
                state = str(pull.get("state") or "").upper()
                if self._withheld(pull):
                    # Reported only so an operator can tell "the author held this back" from
                    # "no pull request exists"; it takes no part in review or release.
                    if state == "OPEN":
                        drafts.append(ref)
                    continue
                if state in {"OPEN", "MERGED"} and self._step(pull):
                    steps.append(ref)
                if state == "OPEN":
                    openings[ref] = pull.get("headRefOid") or None
                elif state == "MERGED":
                    merged.append(ref)
                elif state == "CLOSED":
                    # Kept rather than dropped: a reader that only ever sees open and merged
                    # cannot tell "everything shipped" from "one of them was abandoned" (#2069).
                    closed.append(ref)
        return LinkedPRs(
            open=openings,
            merged=merged,
            withheld=sorted(drafts),
            closed_unmerged=sorted(closed),
            step=sorted(steps),
        )

    def _withheld(self, pull: dict) -> bool:
        """Has this PR's author declared it is not a release target?"""
        if pull.get("isDraft"):
            return True
        marker = self.release_optout_marker
        return bool(marker and marker in str(pull.get("body") or ""))

    def _step(self, pull: dict) -> bool:
        """Has this PR's author declared it one step of its card rather than the whole (#2730)?"""
        marker = self.release_step_marker
        return bool(marker and marker in str(pull.get("body") or ""))

    def _one_pr(self, repo: str, card: dict) -> LinkedPRs:
        ref = f"{repo}#{card.get('number')}"
        state = str(card.get("state") or "").upper()
        if state == "OPEN":
            if self._withheld(card):
                return LinkedPRs(open={}, merged=[], withheld=[ref])
            return LinkedPRs(open={ref: card.get("headRefOid") or None}, merged=[])
        if state == "MERGED":
            return LinkedPRs(open={}, merged=[ref])
        return LinkedPRs(open={}, merged=[], closed_unmerged=[ref] if state == "CLOSED" else [])

    def resolve_prs(self, refs: list[str]) -> LinkedPRs | None:
        """Classify the pull requests a person named on a release command (#1445)."""
        openings: dict[str, str | None] = {}
        merged: list[str] = []
        withheld: list[str] = []
        closed: list[str] = []
        steps: list[str] = []
        for ref in refs:
            repo, number = _split(ref)
            code, output, error = self.host.run(
                "pr", "view", str(number), "-R", self._slug(repo),
                "--json", "number,state,headRefOid,isDraft,body",
            )
            if code != 0:
                if _UNRESOLVABLE_RE.search(error or ""):
                    continue      # GitHub says there is no such thing: the caller refuses
                return None       # anything else is this wake's failure, so retry
            try:
                view = json.loads(output or "null")
            except ValueError:
                return None
            if not isinstance(view, dict) or not view.get("number"):
                return None
            state = str(view.get("state") or "").upper()
            if state in {"OPEN", "MERGED"} and self._step(view) and not self._withheld(view):
                steps.append(ref)
            if state == "MERGED":
                merged.append(ref)
            elif state != "OPEN":
                closed.append(ref)     # nothing to release, but not nothing to know (#2069)
                continue
            elif self._withheld(view):
                withheld.append(ref)
            else:
                openings[ref] = view.get("headRefOid") or None
        return LinkedPRs(
            open=openings, merged=merged, withheld=withheld, closed_unmerged=closed,
            step=steps,
        )

    def release_trees(self, refs: list[str]) -> dict[str, str | None]:
        """Read release-content identities only when a review snapshots or needs them.

        Linked-PR discovery runs throughout a guarded release. Keeping this separate avoids
        spending one extra GraphQL request per PR on every waiting wake.
        """
        return {
            ref: self._release_tree(*_split(ref))
            for ref in refs
        }

    def review_diffs(self, refs: list[str]) -> dict[str, str | None]:
        """Hash the effective PR diffs that a reviewer actually reads.

        A branch update changes the head commit, and a moving base changes the prospective
        merge tree, even when neither changes the PR's own diff.  The diff is therefore the
        durable review identity.  An unreadable or oversized GitHub diff stays unknown so the
        release can defer or fall back to its older, stricter identity.

        What is hashed is the diff's *contribution*, not its bytes: see ``_contributed_diff``
        for why a rebase moves the bytes of a diff that nobody has changed.
        """
        return {ref: self._review_diff(*_split(ref)) for ref in refs}

    def pr_files(self, ref: str) -> list[str] | None:
        """The paths one pull request changes, for the merge note on a review-only one (#2729)."""
        repo, number = _split(ref)
        value = self.host.json("pr", "view", str(number), "-R", self._slug(repo), "--json", "files")
        files = value.get("files") if isinstance(value, dict) else None
        if not isinstance(files, list):
            return None
        return [
            str(item["path"]) for item in files if isinstance(item, dict) and item.get("path")
        ]

    def _review_diff(self, repo: str, number: int) -> str | None:
        code, output, _error = self.host.run(
            "pr", "diff", str(number), "-R", self._slug(repo),
        )
        if code != 0:
            return None
        return review_diff_digest(output)

    def _release_tree(self, repo: str, number: int) -> str | None:
        """The exact tree GitHub would release for one open pull request.

        A prospective merge commit's own OID changes when either parent changes. Its tree OID
        does not change when the head merely merges the current base, which is the distinction
        the review gate needs. An unreadable tree stays unknown and never weakens the old
        head-SHA fence.
        """
        if not self._graphql_has_room():
            return None
        query = (
            "query($owner:String!,$repo:String!,$number:Int!){"
            "rateLimit{remaining resetAt cost}"
            "repository(owner:$owner,name:$repo){pullRequest(number:$number){"
            "potentialMergeCommit{tree{oid}}}}}"
        )
        value = self.host.json(
            "api", "graphql", "-f", "query=" + query,
            "-f", "owner=" + self.org, "-f", "repo=" + repo, "-F", "number=" + str(number),
        )
        if isinstance(value, dict):
            self._remember_graphql_budget(value)
        try:
            tree = value["data"]["repository"]["pullRequest"]["potentialMergeCommit"]["tree"][
                "oid"
            ]
        except (KeyError, TypeError):
            return None
        return str(tree) if tree else None

    def _issue_links(self, repo: str, number: int) -> tuple[set[int], dict[str, set[int]]] | None:
        """Which pull requests GitHub itself ties to this issue.

        Two different ties, and they mean different things. The closing references are
        authoritative — GitHub says this PR closes this issue. The cross-references are only
        *candidates*: a mention anywhere in a PR makes one, so they widen the pool the listing
        draws from and decide nothing. Everything they turn up still has to qualify on the same
        signals as any other pull request, so a passing mention cannot nominate itself.

        A response with no cross-reference block at all yields no candidates rather than an
        unknown answer: the listing still covers every recent pull request, and refusing to
        answer would stall every release over a part of the reply nothing used before.
        A present cross-reference connection is paginated to completion; an unreadable or
        malformed continuation keeps the whole answer unknown instead of silently truncating it.
        """
        query = (
            f'{{rateLimit{{remaining resetAt cost}}repository(owner:"{self.org}",name:"{repo}")'
            f'{{issue(number:{number}){{'
            "closedByPullRequestsReferences(first:50,includeClosedPrs:true){nodes{number}}"
            "timelineItems(first:100,itemTypes:[CROSS_REFERENCED_EVENT]){nodes{"
            "... on CrossReferencedEvent{source{... on PullRequest{number "
            "repository{name owner{login}}}}}}pageInfo{hasNextPage endCursor}}}}}"
        )
        value = self.host.json("api", "graphql", "-f", f"query={query}")
        if not isinstance(value, dict):
            return None
        data = value.get("data")
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("rateLimit"), dict):
            self._remember_graphql_budget(value)
        try:
            issue = data["repository"]["issue"]
            nodes = issue["closedByPullRequestsReferences"]["nodes"]
        except (KeyError, TypeError):
            return None
        native = (
            set()
            if nodes is None
            else {node["number"] for node in nodes if isinstance(node, dict) and "number" in node}
        )
        timeline = issue.get("timelineItems")
        if timeline is None:
            return native, {}
        referenced: dict[str, set[int]] = {}
        seen_cursors: set[str] = set()
        while True:
            page = self._cross_referenced(timeline)
            if page is None:
                return None
            for name, numbers in page.items():
                referenced.setdefault(name, set()).update(numbers)
            page_info = timeline.get("pageInfo")
            if not isinstance(page_info, dict) or not isinstance(
                page_info.get("hasNextPage"), bool
            ):
                return None
            if not page_info["hasNextPage"]:
                return native, referenced
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                return None
            seen_cursors.add(cursor)
            if not self._graphql_has_room():
                return None
            timeline = self._cross_reference_page(repo, number, cursor)
            if timeline is None:
                return None

    def _cross_reference_page(self, repo: str, number: int, cursor: str) -> dict | None:
        """Read one continuation page of an issue's pull-request cross-references."""
        query = (
            "query($owner:String!,$repo:String!,$number:Int!,$after:String!){"
            "rateLimit{remaining resetAt cost}"
            "repository(owner:$owner,name:$repo){issue(number:$number){"
            "timelineItems(first:100,after:$after,itemTypes:[CROSS_REFERENCED_EVENT]){nodes{"
            "... on CrossReferencedEvent{source{... on PullRequest{number "
            "repository{name owner{login}}}}}}pageInfo{hasNextPage endCursor}}}}}"
        )
        value = self.host.json(
            "api", "graphql", "-f", "query=" + query,
            "-f", "owner=" + self.org, "-f", "repo=" + repo,
            "-F", "number=" + str(number), "-f", "after=" + cursor,
        )
        data = value.get("data") if isinstance(value, dict) else None
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("rateLimit"), dict):
            self._remember_graphql_budget(value)
        try:
            timeline = data["repository"]["issue"]["timelineItems"]
        except (KeyError, TypeError):
            return None
        return timeline if isinstance(timeline, dict) else None

    def _cross_referenced(self, timeline: object) -> dict[str, set[int]] | None:
        """Group this issue's cross-referencing pull requests by the code repo they live in."""
        if not isinstance(timeline, dict) or not isinstance(timeline.get("nodes"), list):
            return None
        nodes = timeline["nodes"]
        found: dict[str, set[int]] = {}
        for node in nodes:
            source = (node or {}).get("source") if isinstance(node, dict) else None
            if not isinstance(source, dict) or not isinstance(source.get("number"), int):
                continue                     # an issue, not a pull request: no source fields
            home = source.get("repository") or {}
            owner = str((home.get("owner") or {}).get("login") or "")
            name = str(home.get("name") or "")
            # Only this project's own code repositories: a mention from somewhere else is not
            # a release target, and this adapter can only address one organization.
            if owner.casefold() != self.org.casefold() or name not in self.code_repos:
                continue
            found.setdefault(name, set()).add(source["number"])
        return found

    def _referenced_pulls(self, code_repo: str, numbers: set[int]) -> list[dict] | None:
        """Read pull requests by number that the newest-100 listing did not reach (#1467).

        ``None`` means a read failed, which keeps the whole answer unknown exactly as an
        unreadable listing does. A pull request GitHub says does not exist is simply skipped.
        """
        pulls: list[dict] = []
        for candidate in sorted(numbers):
            code, output, error = self.host.run(
                "pr", "view", str(candidate), "-R", self._slug(code_repo),
                "--json", "number,headRefName,body,state,mergeCommit,headRefOid,isDraft",
            )
            if code != 0:
                if _UNRESOLVABLE_RE.search(error or ""):
                    continue
                return None
            try:
                view = json.loads(output or "null")
            except ValueError:
                return None
            if not isinstance(view, dict) or not view.get("number"):
                return None
            pulls.append(view)
        return pulls

    def _qualifies(
        self,
        pull: dict,
        code_repo: str,
        repo: str,
        native: set[int],
        refs: tuple[str, ...],
        number: int,
    ) -> bool:
        same_repo = code_repo == repo
        body = str(pull.get("body") or "")
        if same_repo and pull.get("number") in native:
            return True                              # GitHub's own link: authoritative
        if self._declares_another_card(pull, code_repo, repo, number):
            return False                             # the author already named a different card
        if any(re.search(rf"{re.escape(ref)}(?!\d)", body) for ref in refs):
            return True                              # a deliberate fully-qualified reference
        branch = str(pull.get("headRefName") or "")
        branch_ok = bool(
            re.fullmatch(rf"agent/issue-{number}(?:-.+)?", branch)
            or branch.startswith(f"claude/issue-{number}-")
        )
        bare_ok = same_repo and re.search(rf"(?<!\d)#{number}(?!\d)", body)
        # A bare "#n" is trusted only alongside the branch convention, so a passing mention in
        # some unrelated PR cannot nominate itself for a merge.
        return bool(branch_ok and bare_ok)

    def _declares_another_card(
        self, pull: dict, code_repo: str, repo: str, number: int
    ) -> bool:
        """Has this pull request already said, itself, that it belongs to a different card?

        A pull request states its card twice: the closing keyword in its body and the issue
        number in its worker branch. Both are the author speaking, and neither can be produced
        by accident — unlike the signals below, which on a board that keeps its issues and its
        code in separate repositories come down to whether the card's `repo#n` appears anywhere
        in the body at all. `issues#405` merged `ledger#1912`, whose first line was `Closes
        example-org/issues#407` on branch `agent/issue-407-ledger`, because the body also
        mentioned the tree it builds on (#2095). So a statement of belonging is read before the
        weaker signals, and refuses every card it does not name.

        Only a statement that names *no* card of this one disqualifies. A pull request closing
        two cards names both and still releases both, and one that states nothing at all is left
        to the signals below exactly as before. GitHub's own closing link outranks this, because
        there the platform has already resolved the tie. The branch carries a number and no
        repository, so it only ever rules a card out; which repository a card lives in comes
        from the body alone.
        """
        body = str(pull.get("body") or "")
        declared = self._declared_cards(body, code_repo, code_repo == repo, number)
        # A companion in another repository names its card's repository as well as its number,
        # in the lead line and in an `agent/<repo>-<n>` branch, so either is a full statement
        # (#2729). Without it, a pull request in the review-chat repository citing a second
        # card as background joined that card's Work.
        declared |= self._companion_cards(body, str(pull.get("headRefName") or ""))
        branch_number = self._branch_issue_number(str(pull.get("headRefName") or ""))
        if branch_number is not None and branch_number != number:
            # A branch names a number but never the repository it belongs to, so it can only
            # ever rule a card out. Reading a matching number as a statement of *this* card
            # would let `agent/issue-405` vouch for `issues#405` on a pull request whose body
            # closes `ledger#405`, and the wrong card would release again — two issue
            # repositories can hold the same number. Repository identity comes from the body.
            declared.add(("", branch_number))
        return bool(declared) and (repo, number) not in declared

    def _declared_cards(
        self, body: str, code_repo: str, same_repo: bool, number: int
    ) -> set[tuple[str, int]]:
        """The cards this body closes, restricted to cards this instance actually tracks.

        A bare ``Closes #n`` means the pull request's own repository to GitHub, but workers write
        it loosely across repositories: a worker on `ledger` writes `Closes #407` meaning its own
        card `issues#407`. So a bare number in another repository is set aside — but only when it
        is the number of the card being judged, which is the whole of what that looseness can
        produce. Otherwise a cross-repo pull request that says both `Closes #407` and `issues#407`
        would disown the very card it was opened for.

        A bare number that names a *different* card still speaks (#2100). On a board where the
        code repository tracks issues of its own, `Closes #177` in `other` is the real card
        `other#177`, and dropping it let a pull request on a hand-written branch declare nothing
        at all — so a passing mention of `agentdeck#176` could qualify it for card 176, the #2095
        shape with a bare closing keyword instead of a qualified one.

        GitHub treats an owner and a repository name as case-insensitive, and a hand-written
        `Closes Example-Org/Issues#407` is the same card as `issues#407`. So both halves
        are folded before they are matched, and what lands in the set is always the configured
        spelling — otherwise a capital letter would hide the declaration and let the weaker
        mention qualify the wrong pull request all over again.
        """
        canonical = {name.casefold(): name for name in self.issue_repos}
        cards: set[tuple[str, int]] = set()
        for match in _CLOSES_RE.finditer(body):
            owner = match.group("owner") or match.group("url_owner") or self.org
            named = match.group("repo") or match.group("url_repo")
            digits = match.group("number") or match.group("url_number") or match.group("bare")
            if named is None and not same_repo and int(digits) == number:
                continue
            target = canonical.get((named or code_repo).casefold())
            if owner.casefold() != self.org.casefold() or target is None:
                continue
            cards.add((target, int(digits)))
        return cards

    def _companion_cards(self, body: str, branch: str) -> set[tuple[str, int]]:
        """The card a companion pull request states it belongs to, by lead line or branch.

        Restricted, like a closing keyword, to this organization's tracked issue repositories,
        with the configured spelling. A branch such as `agent/agentdeck-2727-x` or
        `agentdeck-2272-cutover` names both halves, so it is read the same way.
        """
        canonical = {name.casefold(): name for name in self.issue_repos}
        cards: set[tuple[str, int]] = set()
        lead = _BELONGS_RE.match(body)
        if lead and (lead.group("owner") or self.org).casefold() == self.org.casefold():
            target = canonical.get(lead.group("repo").casefold())
            if target is not None:
                cards.add((target, int(lead.group("number"))))
        named = re.fullmatch(r"(?:agent/)?(?P<repo>[\w.-]+?)-(?P<number>\d+)(?:-.+)?", branch)
        if named:
            target = canonical.get(named.group("repo").casefold())
            if target is not None:
                cards.add((target, int(named.group("number"))))
        return cards

    @staticmethod
    def _branch_issue_number(branch: str) -> int | None:
        """The card number a worker branch names, or ``None`` when it is not a worker branch."""
        match = re.fullmatch(r"(?:agent|claude)/issue-(\d+)(?:-.+)?", branch)
        return int(match.group(1)) if match else None

    # --- pull-request state ---

    def ref_states(self, refs: list[str]) -> dict[str, str]:
        """Read each blocker's own state, issue or pull request alike (#1230).

        One REST read per ref, through the issues endpoint, which serves pull requests too and
        marks them with a ``pull_request`` object. ``state`` alone would call a merged PR and an
        abandoned one both ``CLOSED``; both are in fact out of the way, but the comment the card
        gets reads better when it can say the prerequisite *landed*, so the merge is reported.

        A ref that could not be read is omitted, never guessed at.
        """
        states: dict[str, str] = {}
        for ref in dict.fromkeys(refs or []):
            try:
                repo, number = _split(ref)
            except ValueError:
                continue
            # A cross-organization blocker already carries its owner; only a bare repository
            # name is this project's, and prefixing a slug that has one would invent a path.
            slug = repo if "/" in repo else self._slug(repo)
            view = self.host.json("api", f"repos/{slug}/issues/{number}")
            if not isinstance(view, dict):
                continue
            state = str(view.get("state") or "").strip().upper()
            pull = view.get("pull_request")
            if isinstance(pull, dict) and pull.get("merged_at"):
                state = "MERGED"
            if state:
                states[ref] = state
        return states

    def pr_states(self, refs: list[str]) -> dict[str, PrState] | None:
        """Probe each PR. One unreadable PR makes the answer unknown rather than optimistic."""
        states: dict[str, PrState] = {}
        for ref in refs:
            repo, number = _split(ref)
            view = self.host.json(
                "pr", "view", str(number), "-R", self._slug(repo),
                "--json",
                "state,headRefOid,mergeStateStatus,mergeCommit,mergedAt,autoMergeRequest,"
                "baseRefName,baseRefOid",
            )
            if not isinstance(view, dict):
                return None
            check_state, failed = self.check_state(repo, number)
            states[ref] = PrState(
                ref=ref,
                state=str(view.get("state") or "").upper(),
                head=view.get("headRefOid") or None,
                behind_by=self._behind_by(repo, view),
                base=str(view.get("baseRefName") or "").strip() or None,
                check_state=check_state,
                merge_state=str(view.get("mergeStateStatus") or "").upper(),
                failed_steps=failed,
                merge_commit=(view.get("mergeCommit") or {}).get("oid") or None,
                merged_at=str(view.get("mergedAt") or "") or None,
                # Absent from the payload is not the same as GitHub saying "no request":
                # an older `gh` that never returns the field must not look like a disarm.
                auto_merge=(
                    view.get("autoMergeRequest") is not None
                    if "autoMergeRequest" in view
                    else True
                ),
            )
        return states

    def _behind_by(self, repo: str, view: dict) -> int | None:
        """How many commits the base branch has that this head does not (#2101).

        Asked of the branches rather than of ``mergeStateStatus``, because that field answers
        a different question than it appears to: GitHub sets ``BEHIND`` only where branch
        protection requires branches to be up to date. Neither this repository nor most of the
        projects this capability is switched on for has that protection, so relying on it left
        the rebase-current bit reading green on arbitrarily stale heads — the feature's own
        premise, quietly inert.

        Unreadable is ``None``, never zero. A comparison nobody could make must not be able to
        say "up to date", which is the same unverifiable-is-not-green rule the bits follow.
        """
        if not self.release_bits:
            return None  # nothing this project runs reads it; do not buy the read
        base = str(view.get("baseRefName") or "").strip()
        head = str(view.get("headRefOid") or "").strip()
        if not base or not head:
            return None
        compared = self.host.json(
            "api", f"repos/{self._slug(repo)}/compare/{base}...{head}", "--jq",
            "{behind_by: .behind_by}",
        )
        if not isinstance(compared, dict):
            return None
        value = compared.get("behind_by")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def check_state(self, repo: str, number: int) -> tuple[str, list[str]]:
        """`green` / `pending` / `failing`, and the names of the checks that failed."""
        code, out, err = self.host.run("pr", "checks", str(number), "-R", self._slug(repo))
        blob = f"{out}\n{err}"
        if code == 0 or _NO_CHECKS in blob.lower():
            return "green", []
        if code == _CHECKS_PENDING:
            return "pending", []
        failed = [
            line.split("\t")[0].strip()
            for line in out.splitlines()
            if "\tfail" in line.lower() or line.lower().startswith("x\t")
        ]
        return "failing", [name for name in failed if name]

    def commit_checks(self, repo: str, sha: str) -> str | None:
        """GitHub's CI verdict for exactly this commit (#2364), never the pull request's head.

        Both halves of what GitHub calls checks are read for the one SHA: commit statuses
        (what Drone reports) and check runs (what Actions reports). ``gh pr checks`` is not
        used because it answers for whatever the head is when it runs, so a push between
        reading the head and reading its checks would credit one commit with another's CI.
        """
        sha = str(sha or "").strip()
        if not sha:
            return None
        slug = self._slug(repo)
        status = self.api.json(
            "api", f"repos/{slug}/commits/{sha}/status",
            "--jq", "{statuses: [.statuses[] | .state]}",
        )
        runs = self.api.json(
            "api", f"repos/{slug}/commits/{sha}/check-runs?per_page=100",
            "--jq", "{runs: [.check_runs[] | {status, conclusion}]}",
        )
        if not isinstance(status, dict) or not isinstance(runs, dict):
            return None
        return ci_verdict(status.get("statuses"), runs.get("runs"))

    def sync_merge_parent(self, ref: str, sha: str) -> str | None:
        """The commit GitHub merged the base into to make ``sha``, or ``""`` if it did not (#2793).

        Only a commit with exactly two parents, committed by GitHub itself (``web-flow``) under
        a verified signature, is GitHub's own branch update; its first parent is the head that
        was updated. ``None`` when the commit could not be read.
        """
        sha = str(sha or "").strip()
        if not sha:
            return None
        repo, _number = _split(ref)
        commit = self.api.json(
            "api", f"repos/{self._slug(repo)}/commits/{sha}",
            "--jq",
            "{parents: [.parents[].sha], committer: (.committer.login // \"\"), "
            "verified: (.commit.verification.verified // false)}",
        )
        if not isinstance(commit, dict):
            return None
        parents = commit.get("parents")
        if (
            not isinstance(parents, list)
            or len(parents) != 2
            or commit.get("committer") != "web-flow"
            or commit.get("verified") is not True
        ):
            return ""
        return str(parents[0] or "").lower()

    def arm_auto_merge(self, ref: str) -> ArmOutcome:
        """Ask GitHub to merge this PR once its required checks pass."""
        repo, number = _split(ref)
        code, out, err = self.host.run(
            "pr", "merge", str(number), "-R", self._slug(repo),
            "--auto", f"--{self.merge_method}", "--delete-branch",
        )
        blob = f"{out} {err}".lower()
        if code == 0 or "clean status" in blob:
            return ArmOutcome.ARMED
        if "already" in blob and "merged" in blob:
            return ArmOutcome.ALREADY_MERGED
        if "auto" in blob and any(
            phrase in blob for phrase in ("not allow", "disable", "not enabled")
        ):
            return ArmOutcome.UNAVAILABLE
        return ArmOutcome.FAILED

    def cancel_auto_merge(self, ref: str) -> bool:
        """Ensure GitHub has no native auto-merge request left for this PR.

        Cancellation is deliberately idempotent: a crash can land the GitHub write before
        the state checkpoint, so the next wake may ask again. GitHub also answers with an
        error once the PR merged or another actor already disabled the request; both states
        satisfy the engine's requirement that this PR can no longer auto-merge later.
        """
        repo, number = _split(ref)
        code, out, err = self.host.run(
            "pr", "merge", str(number), "-R", self._slug(repo), "--disable-auto"
        )
        if code == 0:
            return True
        blob = f"{out} {err}".lower()
        if (
            ("already" in blob and "merged" in blob)
            or "not in auto-merge" in blob
            or "auto-merge is not enabled" in blob
            or "auto-merge is disabled" in blob
        ):
            return True
        # Error wording is not a protocol. Verify the resulting remote state so an accepted
        # cancellation followed by a crash remains safely retryable across gh versions.
        view = self.host.json(
            "pr", "view", str(number), "-R", self._slug(repo),
            "--json", "state,autoMergeRequest",
        )
        if not isinstance(view, dict):
            return False
        state = str(view.get("state") or "").upper()
        return state in {"MERGED", "CLOSED"} or (
            state == "OPEN" and view.get("autoMergeRequest") is None
        )

    # --- activation-time discovery (#177 stage 2) ---

    def board(self, org: str, project_number: int):
        """The board's node ids and its actual columns, or None when it could not be read."""
        from .config import BoardSnapshot

        if self.boardless:
            # There is no Project to read, and nothing here spends a GraphQL point finding
            # that out: the status backend is the issue's own labels (#1481).
            return boardless_snapshot()
        query = (
            "query($org:String!,$number:Int!){organization(login:$org){"
            "projectV2(number:$number){id field(name:\"Status\"){"
            "... on ProjectV2SingleSelectField{id options{id name}}}}}}"
        )
        value = self.api.json(
            "api", "graphql", "-f", f"query={query}", "-f", f"org={org}",
            "-F", f"number={project_number}",
        )
        project = None
        if isinstance(value, dict):
            project = ((value.get("data") or {}).get("organization") or {}).get("projectV2")
        if not isinstance(project, dict) or not project.get("id"):
            # A user-owned board answers under `user`, not `organization`.
            query = query.replace("organization(login:$org)", "user(login:$org)")
            value = self.api.json(
                "api", "graphql", "-f", f"query={query}", "-f", f"org={org}",
                "-F", f"number={project_number}",
            )
            if isinstance(value, dict):
                project = ((value.get("data") or {}).get("user") or {}).get("projectV2")
        if not isinstance(project, dict) or not project.get("id"):
            return None
        field_value = project.get("field") or {}
        return BoardSnapshot(
            project_id=str(project.get("id")),
            status_field_id=str(field_value.get("id") or ""),
            status_options={
                str(option.get("name")): str(option.get("id"))
                for option in field_value.get("options") or []
                if isinstance(option, dict) and option.get("name") and option.get("id")
            },
        )

    def repo_check(self, org: str, repo_id: str) -> RepoCheck:
        """Whether a repository is there — and when it is not, which kind of "not" (#707)."""
        code, out, err = self.api.run("api", f"repos/{org}/{repo_id}", "--jq", ".name")
        if code == 0:
            return RepoCheck(Reachability.REACHABLE)
        return RepoCheck(_classify_repo_failure(code, err or out), _failure_detail(err, out))

    def deploy_states(self, builds: dict[str, str | None]) -> dict:
        """Deploy observation belongs to an extension (#179), so this reports nothing."""
        return {}

    def dispatch_acknowledged(self, repo: str, number: int, dispatch: dict) -> bool:
        """Has the host written this exact dispatch's receipt?

        The receipt is keyed by the dispatch id, so an acknowledgement can only ever confirm the
        delivery it was written for — which is what lets the engine await an uncertain send
        instead of re-sending it.
        """
        dispatch_id = (dispatch or {}).get("id")
        if not dispatch_id or self.receipts is None:
            return False
        return receipt_path(self.receipts.dispatch_ack_dir, repo, number, dispatch_id).exists()

    def dispatch_deferred(self, repo: str, number: int, dispatch: dict) -> str | None:
        """The host's positive pre-write refusal for this exact dispatch, when known."""
        dispatch_id = (dispatch or {}).get("id")
        if not dispatch_id or self.receipts is None:
            return None
        directory = getattr(self.receipts, "dispatch_defer_dir", None)
        if directory is None:
            return None
        body = read_receipt(receipt_path(directory, repo, number, dispatch_id))
        reason = str((body or {}).get("reason") or "")
        return reason or None

    def turn_ended(self, repo: str, number: int, generation: str) -> dict | None:
        """How the Runtime says this lease's Run ended, or None if it proves no ending (#2200).

        Keyed by the *generation*, which is the Run id the delivery carried: the engine drops
        `dispatch` from the record as soon as the delivery is acknowledged, and the lease is
        what identifies the turn anyway. A superseded generation's ending is therefore never
        asked about (#279). Read straight from the Runtime each time, so nothing here keeps a
        copy of its answer; a wedged Run is stopped first and only then reads as an ending.
        """
        outcome = self._execution_outcome(repo, number, generation)
        if isinstance(outcome, dict) and outcome.get("execution_state") == revive.ABANDONED:
            outcome = self._stop_abandoned(repo, number, generation)
        return revive.observation(outcome)

    def waiting_on_person(self, repo: str, number: int, generation: str) -> bool | None:
        """Whether the Runtime says this lease's Run waits on a person's prompt (#2887)."""
        outcome = self._execution_outcome(repo, number, generation)
        state = outcome.get("execution_state") if isinstance(outcome, dict) else None
        if not isinstance(state, str) or not state:
            return None
        return state == EXECUTION_WAITING_HUMAN

    def verification_jobs(self) -> list[dict] | None:
        """The runtime-owned job rows, which may hold verification evidence (#2154).

        From the configured runtime when there is one, so the queue's own storage path is
        never duplicated here; the default file only for a caller with no runtime socket. A
        local read either way, so a quiet wake can make it too. Unreadable is None — which
        credits nothing — and never an empty success.
        """
        if self.runtime_socket:
            # Straight from the runtime that owns the queue, so a web restart cannot hide
            # finished evidence. Unreadable is None; the web is never asked instead.
            payload = _runtime_get(self.runtime_socket, "/jobs")
            rows = payload.get("jobs") if isinstance(payload, dict) else None
        else:
            try:
                rows = json.loads(self.jobs_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        if not isinstance(rows, list):
            return None
        return [dict(row) for row in rows if isinstance(row, dict)]

    def cancel_job(self, job_id: str) -> bool:
        """Cancel one queued or running job through the runtime that owns the queue (#2350).

        True only when the runtime answered the cancel. Without a runtime socket there is no
        queue owner to ask, and a cancel that cannot be delivered is reported as not done, so
        the caller tries again on a later wake.
        """
        if not self.runtime_socket or not job_id:
            return False
        status, _body = runtime_post(
            self.runtime_socket, f"/jobs/{quote(str(job_id), safe='')}/cancel", {}
        )
        return status == 200

    def dispatch_worktrees(self, repo: str, number: int, dispatch: dict) -> list[str]:
        """The host worktrees this delivery's receipt reported, so cleanup can find them."""
        dispatch_id = (dispatch or {}).get("id")
        if not dispatch_id or self.receipts is None:
            return []
        return read_receipt_worktrees(
            receipt_path(self.receipts.dispatch_ack_dir, repo, number, dispatch_id)
        )

    def dispatch_session_url(self, repo: str, number: int, dispatch: dict) -> str:
        """The chat link this delivery's receipt carries, when it is one safe to publish."""
        dispatch_id = (dispatch or {}).get("id")
        if not dispatch_id or self.receipts is None:
            return ""
        body = read_receipt(
            receipt_path(self.receipts.dispatch_ack_dir, repo, number, dispatch_id)
        )
        return public_session_url((body or {}).get("session_url"))

    # --- opening a card (#393) ---

    def find_marked_issue(
        self, repo: str, marker: str, *, since: str
    ) -> tuple[int | None, bool]:
        """An issue carrying this marker, and whether its absence was actually established.

        Deliberately a listing rather than `--search`: GitHub's index lags behind creation by
        seconds to minutes, so a search is capable of reporting an issue that exists as absent —
        and the one caller of this is trying to establish absence before creating a second one.

        A listing is bounded, though, so "not in this page" is only absence if the page reached
        back past the moment the intent was written. On a busy repository it may not, and
        concluding absence there is how the duplicate this guards against would happen anyway.
        """
        if not marker:
            return None, True
        code, out, _err = self.api.run(
            "issue", "list", "-R", self._slug(repo), "--state", "all",
            "--limit", str(MARKED_ISSUE_SCAN), "--json", "number,body,createdAt",
        )
        if code != 0:
            # Unknown is not absent: answering "absent" here would license a duplicate.
            return None, False
        try:
            issues = json.loads(out or "[]")
        except ValueError:
            return None, False
        issues = issues if isinstance(issues, list) else []
        reached_back = not issues  # an empty repository is conclusively without it
        for issue in issues:
            if marker in str((issue or {}).get("body") or ""):
                try:
                    return int(issue.get("number")), True
                except (TypeError, ValueError):
                    continue
            if _before(str((issue or {}).get("createdAt") or ""), since):
                # Something older than the intent, so the page spans the whole window in which
                # the issue could have been created. Not finding it here is real absence.
                reached_back = True
        return None, reached_back

    def open_issue(
        self, repo: str, title: str, body: str, *, labels: tuple[str, ...] = ()
    ) -> int | None:
        """Open an issue and return its number, or None when the write did not land."""
        # Labelled in the same write as the creation: a card that exists unowned, even briefly,
        # is a card the engine's own discovery would not recognize as its work.
        extra = [arg for label in labels if label for arg in ("--label", label)]
        code, out, _err = self.api.run(
            "issue", "create", "-R", self._slug(repo),
            "--title", title, "--body", body, "--label", self.ownership_label, *extra,
        )
        if code != 0:
            return None
        # `gh issue create` prints the new issue's URL. Scanned per line from the end rather than
        # splitting the whole output, so a notice printed alongside it cannot be read as a number.
        for line in reversed((out or "").strip().splitlines()):
            candidate = line.strip().rstrip("/").rsplit("/", 1)[-1]
            try:
                return int(candidate)
            except ValueError:
                continue
        return None

    def open_list_issue(self, repo: str, title: str, body: str) -> int | None:
        """Open an unlabelled issue the engine maintains itself (#2517)."""
        code, out, _err = self.api.run(
            "issue", "create", "-R", self._slug(repo), "--title", title, "--body", body,
        )
        if code != 0:
            return None
        for line in reversed((out or "").strip().splitlines()):
            candidate = line.strip().rstrip("/").rsplit("/", 1)[-1]
            try:
                return int(candidate)
            except ValueError:
                continue
        return None

    # --- scheduled tasks (#2517) ---

    def scheduled_tasks(
        self, tracked: tuple[str, ...] = ()
    ) -> dict[str, schedules.TaskIssue] | None:
        """Every ``scheduled`` issue across the issue repositories, in one GraphQL request.

        One point a full wake for the whole project: each repository rides its own alias, and
        a repository without the label answers ``label: null`` for free. Open tasks are read
        whole — a page that was cut answers None, because a task left out would look deleted.
        Closed tasks are only the most recently updated ones, which is all the engine needs to
        notice that a task it was running has been stopped.

        ``tracked`` are the tasks the engine already holds, and each is also read directly in
        the same request (#2519 review). The closed page is bounded, so a tracked task missing
        from it proves nothing; the direct read is what does. A tracked issue that no longer
        wears the label, or no longer exists, is left out of the answer — that absence is
        GitHub's own word. One that could not be read makes the whole answer None.
        """
        if not self.issue_repos and not tracked:
            return {}
        if not self._graphql_has_room():
            log.warning("scheduled-task sweep skipped: the GraphQL reserve is reached")
            return None
        declarations = ["$label:String!"]
        args = ["api", "graphql", "-f", f"label={schedules.LABEL}"]
        issue = "number title body state author{login} editor{login}"
        fields = ["rateLimit{remaining resetAt cost}"]
        for index, repo in enumerate(self.issue_repos):
            declarations.extend([f"$owner{index}:String!", f"$repo{index}:String!"])
            args.extend(["-f", f"owner{index}={self.org}", "-f", f"repo{index}={repo}"])
            fields.append(
                f"r{index}:repository(owner:$owner{index},name:$repo{index}){{"
                "label(name:$label){"
                f"open:issues(first:{SCHEDULED_OPEN_PAGE},states:[OPEN]){{"
                f"pageInfo{{hasNextPage}}nodes{{{issue}}}}}"
                f"closed:issues(first:{SCHEDULED_CLOSED_PAGE},states:[CLOSED],"
                "orderBy:{field:UPDATED_AT,direction:DESC}){"
                f"nodes{{{issue}}}}}}}}}"
            )
        refs: list[tuple[str, int]] = []
        for ref in tracked:
            repo, _, number = str(ref).partition("#")
            if repo and number.isdigit():
                refs.append((repo, int(number)))
        for index, (repo, number) in enumerate(refs):
            declarations.extend([f"$towner{index}:String!", f"$trepo{index}:String!"])
            args.extend(["-f", f"towner{index}={self.org}", "-f", f"trepo{index}={repo}"])
            fields.append(
                f"t{index}:repository(owner:$towner{index},name:$trepo{index}){{"
                f"issue(number:{number}){{{issue} labels(first:100){{nodes{{name}}}}}}}}"
            )
        query = f"query({','.join(declarations)}){{{''.join(fields)}}}"
        args[2:2] = ["-f", "query=" + query]
        # Read the body whatever the exit code: `gh` exits non-zero for any GraphQL error,
        # including the NOT_FOUND that is GitHub's answer for a deleted tracked task.
        _code, out, _err = self.api.run(*args)
        try:
            value = json.loads(out or "null")
        except ValueError:
            value = None
        if isinstance(value, dict):
            self._remember_graphql_budget(value)
        data = value.get("data") if isinstance(value, dict) else None
        if not isinstance(data, dict) or not _only_missing_tracked(value):
            log.warning("scheduled-task sweep failed: the request could not be read")
            return None
        found: dict[str, schedules.TaskIssue] = {}
        for index, (repo, number) in enumerate(refs):
            listing = data.get(f"t{index}")
            if not isinstance(listing, dict):
                log.warning("scheduled-task sweep: %s#%s could not be read", repo, number)
                return None
            node = listing.get("issue")
            if node is None:
                continue  # GitHub says the issue does not exist: no longer a task
            names = {
                str((label or {}).get("name") or "").casefold()
                for label in ((node.get("labels") or {}).get("nodes") or [])
            } if isinstance(node, dict) else set()
            if schedules.LABEL.casefold() not in names:
                continue  # the label was taken off: no longer a task
            task = _scheduled_task(repo, node)
            if task is None:
                return None
            found[f"{repo}#{task.number}"] = task
        for index, repo in enumerate(self.issue_repos):
            listing = data.get(f"r{index}")
            if not isinstance(listing, dict):
                log.warning("scheduled-task sweep: %s could not be read", repo)
                return None
            label = listing.get("label")
            if label is None:
                continue  # the repository has no `scheduled` label, so no tasks
            opened = label.get("open") if isinstance(label, dict) else None
            closed = label.get("closed") if isinstance(label, dict) else None
            if not isinstance(opened, dict) or not isinstance(closed, dict):
                return None
            if (opened.get("pageInfo") or {}).get("hasNextPage") is not False:
                log.warning("scheduled-task sweep: %s has too many open tasks to read", repo)
                return None
            for node in [*(closed.get("nodes") or []), *(opened.get("nodes") or [])]:
                task = _scheduled_task(repo, node)
                if task is not None:
                    found[f"{repo}#{task.number}"] = task
        return found

    # --- effects ---

    def _comment_body(self, repo: str, number: int, value: object) -> str:
        """One comment body: answer, guide, compact navigation, then the bot marker.

        The marker stays last so ``endswith`` remains the cheap test for "we wrote this", and
        the navigation is composed here because "every comment" is only reliable at the one
        renderer every engine and worker comment uses (#373, #957).
        """
        comment = value if isinstance(value, CommentBody) else CommentBody(str(value))
        parts = [comment.body.rstrip()]
        if self.comment_guide:
            parts.append(self.comment_guide)
        navigation = []
        if session_url := public_session_url(comment.session_url):
            navigation.append(f"[AgentDeck]({session_url})")
        pull_request_url = ""
        if comment.pull_request_ref:
            pr_repo, separator, pr_number = comment.pull_request_ref.rpartition("#")
            if separator and pr_repo and pr_number.isdigit():
                pull_request_url = (
                    f"https://github.com/{self._slug(pr_repo)}/pull/{int(pr_number)}"
                )
        if not pull_request_url:
            pull_request_url = github_pull_request_url(comment.pull_request_url)
        if pull_request_url:
            pr_number = urlsplit(pull_request_url).path.rstrip("/").rsplit("/", 1)[-1]
            label = f"PR #{pr_number}" if pr_number.isdigit() else "PR"
            navigation.append(f"[{label}]({pull_request_url})")
        issue_url = f"https://github.com/{self._slug(repo)}/issues/{int(number)}"
        navigation.append(f"[Issue #{int(number)}]({issue_url})")
        parts.append(" · ".join(navigation))
        parts.append(BOT_MARKER)
        return "\n\n".join(parts)

    def apply(self, effect: Effect) -> bool:
        """Perform one declared change, answering whether it landed."""
        slug = self._slug(effect.repo)
        number = str(effect.number)
        if effect.kind is EffectKind.SET_STATUS:
            return self.set_status(effect.repo, effect.number, str(effect.value or ""))
        if effect.kind is EffectKind.ADD_OWNERSHIP:
            return self.api.run(
                "issue", "edit", number, "-R", slug, "--add-label", self.ownership_label
            )[0] == 0
        if effect.kind is EffectKind.COMMENT:
            body = self._comment_body(effect.repo, effect.number, effect.value)
            landed = self.api.run(
                "issue", "comment", number, "-R", slug, "--body", body
            )[0] == 0
            if landed:
                # Best-effort tidying, deliberately after the fact and deliberately ignored:
                # the comment is the durable thing and a failed retirement must never turn a
                # landed comment into one the caller thinks did not land.
                self.retire_superseded_comments(effect.repo, effect.number)
            return landed
        if effect.kind is EffectKind.REACT:
            return self._react(slug, effect.value or {})
        if effect.kind is EffectKind.CLOSE_ISSUE:
            return self.host.run("issue", "close", number, "-R", slug)[0] == 0
        if effect.kind is EffectKind.REOPEN_ISSUE:
            return self.host.run("issue", "reopen", number, "-R", slug)[0] == 0
        if effect.kind is EffectKind.SET_TITLE:
            title = str(effect.value or "").strip()
            # A rename to nothing is not a write GitHub would accept, and an effect that never
            # left must answer False rather than report a change nobody made.
            if not title:
                return False
            return self.api.run("issue", "edit", number, "-R", slug, "--title", title)[0] == 0
        if effect.kind is EffectKind.UPDATE_BRANCH:
            return self.host.run("pr", "update-branch", number, "-R", slug)[0] == 0
        if effect.kind is EffectKind.SET_BODY:
            body = str(effect.value or "")
            if not body:
                return False
            return self.api.run("issue", "edit", number, "-R", slug, "--body", body)[0] == 0
        if effect.kind is EffectKind.PIN_ISSUE:
            return self.api.run("issue", "pin", number, "-R", slug)[0] == 0
        if effect.kind is EffectKind.REMOVE_WORKTREES:
            return self.release_worktrees(effect.repo, effect.number, effect.value or [])
        if effect.kind is EffectKind.DISCARD_RECEIPTS:
            return self.discard_receipts(effect.repo, effect.number, effect.value)
        return False

    def retire_superseded_comments(self, repo: str, number: int) -> int:
        """Hide this card's older poller comments as outdated, answering how many (#2116).

        A poller thread grows by re-posting: every re-dispatch writes a fresh full review
        packet, and the four on agentdeck#2108 were 64 KB of an 77 KB thread. Nothing was ever
        marked superseded, so a person opening the issue scrolled past four stale copies of a
        diff to reach the current one.

        This hides them into GitHub's own *outdated* state, which renders as a single grey row
        that expands on click. Three properties make that safe to do automatically. Nothing is
        edited and nothing is deleted, so a person can still read every word. The API still
        returns a hidden comment in full, proved against live GitHub before this was built, so
        every marker the engine reads its own state out of — terminal and merge-review
        — keeps working exactly as before. And only two kinds of comment are ever
        touched: ones carrying ``BOT_MARKER``, which the engine appends to its own and its
        workers' comments and to nothing else, and the ChatGPT reviewer's findings handoffs
        (#2167), each round of which replaces the last. A person's ``/claude`` instruction and
        any other reviewer's verdict are never hidden, however stale they get.

        The newest of each kind always stays visible, and there are three kinds: the worker's
        reports, the poller's status notes, and the reviews. A status note — Releasing,
        Merged, Done — replaces only the note before it, never the report that says what was
        done: on agentdeck#2687 the last report was hidden by the notes that followed it, and
        the thread was left with nothing explaining the work (#2689). The newest review may
        not have been answered yet. That is also why the worker is told its terminal comment
        must carry its own screenshots and the whole card's outcome rather than point back at
        an earlier one: after this, an earlier one is collapsed.
        """
        if not self._graphql_has_room():
            return 0
        query = (
            "query($owner:String!,$repo:String!,$number:Int!){"
            "rateLimit{remaining resetAt cost}"
            "repository(owner:$owner,name:$repo){issue(number:$number){"
            f"comments(last:{RETIRE_COMMENT_SCAN}){{nodes{{id isMinimized body}}}}}}}}}}"
        )
        value = self.api.json(
            "api", "graphql", "-f", "query=" + query,
            "-f", "owner=" + self.org, "-f", "repo=" + repo,
            "-F", "number=" + str(int(number)),
        )
        if isinstance(value, dict):
            self._remember_graphql_budget(value)
        try:
            nodes = value["data"]["repository"]["issue"]["comments"]["nodes"]
        except (KeyError, TypeError):
            return 0
        if not isinstance(nodes, list):
            return 0
        # Ascending, oldest first, which is what GitHub returns for either paging direction.
        # Each kind keeps its own newest: the latest worker report stays open beside the latest
        # status note, and the latest review beside both, because the worker may not have
        # answered it yet.
        stale = [
            str(node.get("id") or "")
            for kind in (_is_worker_report, _is_poller_notice, _is_review_comment)
            for node in [node for node in nodes if isinstance(node, dict) and kind(node)][:-1]
            if not node.get("isMinimized")
        ]
        ids = [
            node_id
            for node_id in stale
            if _COMMENT_NODE_ID_RE.match(node_id)
        ][:RETIRE_COMMENT_LIMIT]
        if not ids or not self._graphql_has_room():
            return 0
        fields = "".join(
            f'r{index}:minimizeComment('
            f'input:{{subjectId:"{node_id}",classifier:OUTDATED}}'
            "){minimizedComment{isMinimized}}"
            for index, node_id in enumerate(ids)
        )
        # No `rateLimit` field here, unlike every read above: it exists on GitHub's Query type
        # and not on Mutation, and asking for it makes the whole document a schema error that
        # hides nothing. So the point this spends has to be subtracted by hand instead of read
        # back — leaving it unobserved would let a run that started just above the reserve
        # authorize a later read that crosses it, because the cached remaining only refreshes
        # on the next answer that carries a `rateLimit` (#2116).
        self._spend_graphql_points(1)
        result = self.api.json("api", "graphql", "-f", "query=mutation{" + fields + "}")
        if not isinstance(result, dict):
            return 0
        # A partially applied mutation still reports the aliases that worked, so count those
        # rather than claiming the whole batch on an answer that carried an error for one.
        applied = result.get("data")
        if not isinstance(applied, dict):
            return 0
        return sum(
            1
            for index in range(len(ids))
            if isinstance(applied.get(f"r{index}"), dict)
        )

    def discard_receipts(self, repo: str, number: int, value: object = None) -> bool:
        """Drop the delivery receipts an abandoned lease left behind.

        ``value`` names one delivery (``dispatch_id``/``generation``); ``None`` means every
        receipt this card holds, which is what a released or stopped card asks for.

        This had no branch in ``apply`` at all, so it answered False like an unknown effect and
        every acknowledged dispatch recorded a cycle error nobody had a reason to look at (#279).
        A receipt whose file is already gone counts as discarded — the point is that it is not
        there afterwards, not that this call is what removed it.
        """
        if self.receipts is None:
            return True  # nothing stores receipts here, so nothing can be left behind
        directories = [
            directory
            for directory in (
                getattr(self.receipts, "dispatch_ack_dir", None),
                getattr(self.receipts, "dispatch_defer_dir", None),
            )
            if directory is not None
        ]
        if isinstance(value, dict):
            tokens = [
                str(token)
                for token in (value.get("dispatch_id"), value.get("generation"))
                if token
            ]
            return all(
                clear_receipt(directory, repo, number, token)
                for directory in directories
                for token in tokens
            )
        return all(clear_card_receipts(directory, repo, number) for directory in directories)

    def set_status(self, repo: str, number: int, role: str) -> bool:
        """Move the card on the board, using the ids frozen at activation.

        The ids are never looked up here: they were resolved once, fingerprinted, and frozen
        (#177), so a wake writes to exactly the board that was approved. Adding the card to the
        board first is idempotent, which is why a card created after activation still works.
        """
        if self.binding is None:
            return False
        option = self.binding.status_option_ids.get(role) or self.binding.status_option_ids.get(
            str(role).strip()
        )
        if not option:
            return False
        if self.boardless:
            return self._set_status_label(repo, number, option)
        node = self.api.json(
            "issue", "view", str(number), "-R", self._slug(repo), "--json", "id"
        )
        if not isinstance(node, dict) or not node.get("id"):
            return False
        item = self.api.json(
            "api", "graphql",
            "-f",
            "query=mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c})"
            "{item{id}}}",
            "-f", f"p={self.binding.project_id}",
            "-f", f"c={node['id']}",
        )
        try:
            item_id = item["data"]["addProjectV2ItemById"]["item"]["id"]
        except (KeyError, TypeError):
            return False
        wrote = self.api.json(
            "api", "graphql",
            "-f",
            "query=mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue("
            "input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}})"
            "{projectV2Item{id}}}",
            "-f", f"p={self.binding.project_id}",
            "-f", f"i={item_id}",
            "-f", f"f={self.binding.status_field_id}",
            "-f", f"o={option}",
        )
        return isinstance(wrote, dict) and "errors" not in wrote

    def _set_status_label(self, repo: str, number: int, wanted: str) -> bool:
        """Move a boardless card by swapping its `status:*` label (#1481).

        One edit adds the new label and removes every other status label the card is actually
        carrying, so the card is never briefly in two statuses and never briefly in none. Only
        labels the card has are named, because `gh` refuses a removal for a label the
        repository does not define — which a repository provisioning has not reached would
        otherwise turn into a status write that fails on every wake forever.
        """
        slug = self._slug(repo)
        view = self.api.json("issue", "view", str(number), "-R", slug, "--json", "labels")
        if not isinstance(view, dict):
            return False
        present = {
            str(item.get("name") or "").strip()
            for item in view.get("labels") or []
            if isinstance(item, dict)
        }
        stale = [name for name in status_labels() if name != wanted and name in present]
        if wanted in present and not stale:
            return True
        args = ["issue", "edit", str(number), "-R", slug, "--add-label", wanted]
        for name in stale:
            args += ["--remove-label", name]
        code, _out, _err = self.api.run(*args)
        return code == 0

    def _react(self, slug: str, value: dict) -> bool:
        comment = value.get("comment")
        reaction = value.get("reaction")
        if not comment or not reaction:
            return False
        code, _out, _err = self.api.run(
            "api",
            "--method", "POST",
            f"repos/{slug}/issues/comments/{comment}/reactions",
            "-f", f"content={reaction}",
        )
        return code == 0

    @staticmethod
    def worktree_dirty_counts(paths: list[str]) -> dict[str, int | None]:
        """Uncommitted-file count per registered worktree, ``None`` where it cannot be read.

        The same eligibility rule as ``remove_worktrees``: only a validated, still-registered
        direct child of a checkout's ``.worktrees`` is ever inspected. This is a read for a
        human's benefit, so every failure degrades to ``None`` for that path alone.
        """
        counts: dict[str, int | None] = {}
        for path in paths:
            target = str(path)
            counts[target] = None
            repo, present = registered_worktree(target)
            if repo is None or not present:
                continue
            counts[target] = _dirty_count(target)
        return counts

    @staticmethod
    def worktree_changed_paths(paths: list[str]) -> dict[str, WorktreeChanges | None]:
        """What each registered worktree has changed, ``None`` where Git could not say (#1736).

        The same eligibility rule as ``worktree_dirty_counts``: only a validated, still
        registered worktree of a checkout is inspected, and the checkout it belongs to travels
        back with the paths so the engine never compares a path across two repositories.
        """
        found: dict[str, WorktreeChanges | None] = {}
        for path in paths:
            target = str(path)
            found[target] = None
            repo, present = registered_worktree(target)
            if repo is None or not present:
                continue
            changed = _changed_paths(target)
            if changed is None:
                continue
            local, commits = changed
            found[target] = WorktreeChanges(
                checkout=str(repo), local=local, commits=commits,
            )
        return found

    def release_worktrees(self, repo: str, number: int, paths: Iterable[str]) -> WorktreeCleanup:
        """Hand a finished card's worktrees to their Work, which alone may delete them (#2149).

        Every worktree a card works in is a Work claim (#2195), so the runtime's release is
        the one destructive owner: a claimed path counts as given back here, whatever the
        Work's own release then decides (a dirty tree stays pending on the Work, visible
        there). The card deletes nothing itself. A path no Work claims that is still on disk
        is retained, which releases the card and says on it which directory was left for the
        operator's ``reap-worktrees``; one already gone needs nothing. When the runtime cannot
        answer, every path stays pending for the next wake rather than being guessed at.
        """
        paths = [str(path) for path in paths]
        if not paths:
            return WorktreeCleanup()
        if not self.runtime_socket:
            return WorktreeCleanup(tuple(paths))
        work = card_work_id(self.org, repo, number)
        status, answer = runtime_post(
            self.runtime_socket,
            WORK_WORKSPACE_RELEASE_PATH,
            {"work_id": work, "paths": paths},
            timeout=WORK_WORKSPACE_RELEASE_TIMEOUT_S,
        )
        unclaimed = answer.get("unclaimed") if status == 200 and isinstance(answer, dict) else None
        if not isinstance(unclaimed, list):
            return WorktreeCleanup(tuple(paths))
        loose = {str(item) for item in unclaimed}
        return WorktreeCleanup(
            retained=tuple(
                (path, "no Work claims it, so it was left on disk for `reap-worktrees`")
                for path in paths
                if path in loose and not _vanished(path)
            )
        )

    @staticmethod
    def remove_worktrees(paths: Iterable[str]) -> WorktreeCleanup:
        """Release validated host worktrees, keeping any that still holds uncommitted work.

        Three outcomes per path, and the middle one is the point of #1718: removed, kept
        deliberately because deleting it would destroy work, or failed. This used to be a
        ``git worktree remove --force``, which takes a dirty checkout without complaint —
        that is how a live follow-up session's uncommitted patch was deleted underneath it
        when the engine released the card that had created the directory.

        The guard is the removal itself, not a check in front of it. A ``git status`` read
        followed by a ``--force`` in a second process leaves a window: a session writing
        between the two loses exactly the work the read was meant to protect. So the
        removal is **not** forced, and Git declines a worktree holding modified or
        untracked files as one operation — it does the looking and the deleting itself,
        which is the only way this cannot race. The status read stays in front of it to
        produce a reason a person can read, and to leave an obviously live directory alone
        without attempting anything.
        """
        failed: list[str] = []
        retained: list[tuple[str, str]] = []
        for path in paths:
            target = str(path)
            repo, present = registered_worktree(target)
            if repo is None:
                # A path that is simply gone has nothing left to remove, so it is released
                # whether or not a checkout can still be attributed to it. Nested worktrees
                # make that ordinary: the outer checkout a card also created can be removed
                # first, and it takes the inner one — and the directory the inner path's
                # repository would be — with it. Reported as pending, such a path can never
                # become clean, so it dirtied every wake's report and blocked the poller
                # engine's promotion for good (#1189).
                if _vanished(target):
                    continue
                failed.append(target)
                continue
            # A path under a real checkout that is no longer registered is already clean.
            # This makes a retry after a partial previous attempt idempotent.
            if not present:
                continue
            holding = _holds_work(target)
            if holding:
                retained.append((target, holding))
                continue
            try:
                done = subprocess.run(
                    ["git", "-C", str(repo), "worktree", "remove", target],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                failed.append(target)
                continue
            if done.returncode != 0:
                # Git refused. Ask the directory why, because the two answers mean opposite
                # things: work that appeared since the read above is a deliberate keep and
                # must not hold the card, while a locked worktree or a permissions problem
                # is a real failure the next wake should retry.
                appeared = _holds_work(target, read=_dirty_count(target))
                if appeared:
                    retained.append((target, appeared))
                elif "submodules" in done.stderr:
                    # Git never removes a worktree that ever had a submodule checked out
                    # without --force, which this deliberately does not use. Retrying cannot
                    # change that, so it is a keep for a person, not a failure that fails
                    # the daily reaper over one directory every day (#3394).
                    retained.append(
                        (target, "it contains submodules; git removes it only with --force")
                    )
                else:
                    failed.append(target)
        return WorktreeCleanup(tuple(failed), tuple(retained))


#: Marks a commit header in the committed read's output, so a line of it is never mistaken
#: for a file. Git writes the header and the file names into one stream; nothing else tells
#: them apart, and a real path starting with this is not a thing anyone has.
_COMMIT_MARK = "\x01commit "


def _changed_paths(
    target: str, *, runner=subprocess.run
) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]] | None:
    """What one worktree has changed — local edits, then commit by commit — or ``None``.

    Three reads, deliberately in this order and with different strictness. The two local ones
    — modified-and-staged, then untracked — are what the worker has in front of it right now,
    and either failing means this worktree cannot be judged at all, so the answer is ``None``.
    The committed read needs a base branch, and a checkout whose ``origin/HEAD`` is not set is
    ordinary rather than broken, so a failure there only costs the committed half: a card with
    edits on disk is still reported instead of vanishing from the overlap plan.

    Two things keep somebody else's already-merged work out of this answer (#2065), because
    ``origin/master`` is only as fresh as the clone's last fetch and a branch therefore
    routinely carries commits that are on the real master and belong to nobody on this board.
    Flattened against a stale base they read as a dozen files two unrelated cards were both
    writing, which is what stopped a card that had touched none of them.

    First, the range starts at the branch's **own** base — where Git itself recorded the
    branch being created — rather than at a merge-base with a ref nobody has refreshed. That
    is what makes the boundary this card's and not a comparison with whoever else is running:
    two cards cut from *different* stale prefixes each get their own, and neither inherits
    the other's.

    Second, it is walked ``--first-parent --no-diff-merges``, so a worker that merges master
    mid-work is charged with nothing the merge brought in: the second parent is not walked,
    and the merge commit itself names no files. Both halves of that are needed — on its own,
    ``--first-parent`` makes Git show each merge's diff *against* its first parent, which is
    precisely the upstream arrival this is trying to leave out.

    The commits are kept separate rather than flattened because neither arm covers a branch
    that *rebased* onto upstream: that rewrites the card's own line, so the upstream commits
    become ordinary first-parent ancestors. Those the overlap plan can still cancel by id
    when another compared card holds them too, which is the only evidence left in that case.
    """
    local = (
        ["git", "-C", target, "diff", "--name-only", "-z", "HEAD", "--"],
        ["git", "-C", target, "ls-files", "--others", "--exclude-standard", "-z"],
    )
    found: set[str] = set()
    commits: tuple[tuple[str, tuple[str, ...]], ...] = ()
    try:
        for command in local:
            done = runner(command, capture_output=True, text=True, timeout=30, check=False)
            if done.returncode != 0:
                return None
            found.update(path for path in (done.stdout or "").split("\0") if path)
        base = _branch_base(target, runner=runner)
        if base:
            committed = runner(
                [
                    "git", "-c", "core.quotePath=false", "-C", target, "log",
                    "--first-parent", "--no-diff-merges",
                    f"--format={_COMMIT_MARK}%H", "--name-only", "-z", f"{base}..HEAD", "--",
                ],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if committed.returncode == 0:
                commits = _commit_paths(committed.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return None
    return tuple(sorted(found)), commits


def _commit_paths(output: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Split one ``git log --name-only -z`` stream into ``(commit, paths)`` pairs.

    Git separates every token — the marked header as much as each file name — with a NUL, and
    writes a newline of its own between a commit's header and its first file. A merge commit
    names no files and what it merged is not walked, so it arrives here as a header with an
    empty path list — kept as such, because it is still a commit this branch and no other has.
    """
    found: list[tuple[str, list[str]]] = []
    for token in output.split("\0"):
        text = token.lstrip("\n")
        if text.startswith(_COMMIT_MARK):
            found.append((text[len(_COMMIT_MARK):], []))
        elif text and found:
            found[-1][1].append(text)
    return tuple((commit, tuple(sorted(set(paths)))) for commit, paths in found if commit)


def _branch_base(target: str, *, runner=subprocess.run) -> str:
    """Where this worktree's branch was cut, or the clone's default branch if unknown (#2065).

    Git records the answer itself. Creating a branch writes a ``branch: Created from <sha>``
    reflog entry, and every commit since appends after it, so the oldest entry is the start
    point — read out of this clone, needing no network and no agreement with any other card.
    That is the boundary a card's own work begins at: two cards cut from two different stale
    prefixes of master each measure from their own, so neither is charged with the upstream
    commits the other happens not to have.

    The fallback is the previous rule, ``origin/HEAD``, for a branch whose reflog this clone
    never had or has since expired. A reflog that survives only in part is used as it stands:
    its oldest entry is somewhere the branch really was, so measuring from it can only leave
    the card's earliest commits out — which understates the card and holds nobody, the
    fail-open direction this read picks everywhere else.
    """
    try:
        named = runner(
            ["git", "-C", target, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        branch = (named.stdout or "").strip() if named.returncode == 0 else ""
        if branch:
            done = runner(
                ["git", "-C", target, "reflog", "show", "--format=%H", branch],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if done.returncode == 0:
                seen = [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]
                if seen:
                    return seen[-1]
    except (OSError, subprocess.SubprocessError):
        return ""
    return _base_ref(target, runner=runner)


def _base_ref(target: str, *, runner=subprocess.run) -> str:
    """The remote branch this worktree's commits should be measured against, or ``""``.

    ``origin/HEAD`` is what the clone itself says its default branch is, so this does not have
    to be told the project's base branch or guess between ``master`` and ``main``.
    """
    try:
        done = runner(
            ["git", "-C", target, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip() if done.returncode == 0 else ""


def _dirty_count(target: str, *, runner=subprocess.run) -> int | None:
    """Uncommitted and untracked files in one worktree; ``None`` when Git could not say.

    Ignored files do not count — a left-behind ``__pycache__`` is not somebody's progress.
    """
    try:
        done = runner(
            ["git", "-C", target, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return sum(1 for line in done.stdout.splitlines() if line.strip())


_UNSET = object()


def _holds_work(target: str, *, read: object = _UNSET) -> str:
    """Why this worktree must not be removed, or ``""`` when it is safe to release (#1718).

    An unreadable status keeps the directory, the same rule the operator reaper already
    follows: an unknown is never permission to delete a worker's checkout. What is *not*
    checked here is whether the branch's commits reached the base branch. A squash merge
    leaves every released worktree looking unmerged, so testing that would retain all of
    them and make the engine leak directories instead of deleting work.

    ``read`` supplies a count already taken, for the caller that has to explain a removal
    Git has *already* refused: asking again there would report on a third moment in time.
    """
    count = _dirty_count(target) if read is _UNSET else read
    if count is None:
        return "its state could not be read"
    if count:
        return f"{count} uncommitted file(s)"
    return ""


def _vanished(path: str) -> bool:
    """Is this cleanup target absent from the host altogether?

    Absolute only, and a dangling symlink still counts as present: something is there to
    remove and no repository claims it, which is exactly the case that should stay pending
    for a person to look at rather than be written off as clean.
    """
    target = Path(path)
    if not target.is_absolute():
        return False
    return not target.exists() and not target.is_symlink()


# Every comment this engine writes carries one of its own HTML markers, and they all share
# this prefix. A comment without it was written by a person, which is what makes it a
# requester instruction the authoritative bundle has to fence (#1473).
_ENGINE_COMMENT_MARKER = "<!-- claude-"

# How many of a card's most recent human comments the bundle carries. A conversation longer
# than this cannot be delivered exactly, and the identity says so rather than dropping the
# oldest quietly: enforce-mode delivery then falls back to the ordinary brief, which sends the
# worker to read the issue itself. 30 since #2416, because messages the operator also posts
# from a deck chat count as the requester's comments too.
MAX_REQUESTER_COMMENTS = 30


def _requester_comments(comments: list[dict]) -> tuple[tuple[dict, ...], int]:
    """The card's human comments, newest last, with the identity needed to fence them.

    The second value is how many older human comments were left out.  A non-zero count makes
    the authoritative bundle unprovable rather than quietly shorter, because a conversation
    this engine cannot carry exactly is one it must not paraphrase.
    """
    human = [
        {
            "id": str(comment.get("id") or ""),
            "created_at": str(comment.get("createdAt") or ""),
            "author": str((comment.get("author") or {}).get("login") or ""),
            "body": str(comment.get("body") or ""),
        }
        for comment in comments
        if isinstance(comment, dict)
        and _ENGINE_COMMENT_MARKER not in str(comment.get("body") or "")
    ]
    return tuple(human[-MAX_REQUESTER_COMMENTS:]), max(0, len(human) - MAX_REQUESTER_COMMENTS)


def registered_worktree(path: str, *, runner=subprocess.run) -> tuple[Path | None, bool]:
    """Validate one cleanup target and say whether Git still registers it.

    Only an absolute direct child of ``<git checkout>/.worktrees`` is eligible.  The
    checkout itself must be a real Git worktree root, and an existing target must appear in
    that checkout's own worktree registry before ``--force`` can ever be invoked.
    """
    target = Path(path)
    if not target.is_absolute() or target.name in {"", ".", ".."}:
        return None, False
    if target.parent.name != ".worktrees":
        return None, False
    repo = target.parent.parent
    if repo == Path("/") or not repo.is_dir():
        return None, False
    try:
        root = runner(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        listed = runner(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, False
    if root.returncode != 0 or listed.returncode != 0:
        return None, False
    try:
        if Path(root.stdout.strip()).resolve() != repo.resolve():
            return None, False
        registered = {
            Path(line.removeprefix("worktree ")).resolve()
            for line in listed.stdout.splitlines()
            if line.startswith("worktree ")
        }
        resolved_target = target.resolve()
    except (OSError, RuntimeError):
        return None, False
    present = resolved_target in registered
    if target.exists() and not present:
        return None, False
    return repo, present


def receipt_path(directory, repo: str, number: int, token: str) -> Path:
    """One receipt per (card, token). The name is a digest so no id can collide with a path."""
    digest = hashlib.sha256(f"{repo}#{int(number)}:{token}".encode()).hexdigest()
    return Path(directory) / f"{digest}.ack"


def write_receipt(
    directory,
    repo: str,
    number: int,
    token: str,
    *,
    now: datetime,
    worktrees: Iterable[str] = (),
    extra: dict | None = None,
) -> Path:
    """Record an acknowledgement atomically: a half-written receipt must never read as one.

    ``worktrees`` are the host paths the dispatcher actually created for this delivery. The
    engine never guesses them: it removes a worktree only because something told it that exact
    path exists, which is the difference between cleaning up and deleting a directory that
    happened to match a naming convention.
    """
    path = receipt_path(directory, repo, number, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    body = {
        "at": now.astimezone(UTC).isoformat(),
        # The file name is a digest and cannot be read back, so the card is recorded inside:
        # it is the only way a release can find every receipt it must discard (#279).
        "card": f"{repo}#{int(number)}",
        "worktrees": [str(p) for p in worktrees],
        # A receipt kind may carry its own facts (a turn end carries when and how it ended,
        # #279). Readers take what they know and ignore the rest, so an older engine reading
        # a newer receipt — or the reverse — is inert rather than wrong.
        **(extra or {}),
    }
    tmp.write_text(json.dumps(body, sort_keys=True) + "\n")
    tmp.replace(path)
    return path


def read_receipt_worktrees(path: Path) -> list[str]:
    """The worktree paths a receipt reported, if any.

    Receipts used to hold a bare timestamp, and one written by an older dispatcher is still a
    valid acknowledgement — it simply reports no paths. Anything unparseable is read the same
    way, because a receipt that cannot be understood must never look like a demand to delete.
    """
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(body, dict):
        return []
    return [str(item) for item in body.get("worktrees") or [] if isinstance(item, str)]


def read_receipt(path) -> dict | None:
    """One receipt's contents, or None when it is absent or unreadable."""
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def clear_receipt(directory, repo: str, number: int, token: str) -> bool:
    """Discard a receipt whose lease is over (#176 slice 13); True when it is gone.

    A receipt that was never written is already gone, so it is not a failure. Only a removal
    the filesystem actually refused answers False, which the engine records as a retryable
    effect rather than an assumption.
    """
    try:
        receipt_path(directory, repo, number, token).unlink(missing_ok=True)
    except OSError:
        return False
    return True


def clear_card_receipts(directory, repo: str, number: int) -> bool:
    """Discard every receipt this card holds; True when none of them is left.

    Receipts are named by digest, so which card a file belongs to can only be read from inside
    it. One written before that was recorded names no card and is left alone: it is inert (a
    dispatch id is a uuid, so no later delivery can collide with it) and deleting a file this
    process cannot identify would be worse than leaving it.
    """
    card = f"{repo}#{int(number)}"
    ok = True
    try:
        receipts = sorted(Path(directory).glob("*.ack"))
    except OSError:
        return False
    for path in receipts:
        try:
            body = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(body, dict) or body.get("card") != card:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            ok = False
    return ok


def _rest_actor(actor: object) -> object:
    """A GraphQL event actor in the shape REST gives it, so an app is still an app (#2384).

    GraphQL names a GitHub App ``agentdeck-eerovil`` where REST says ``agentdeck-eerovil[bot]``,
    and the ``[bot]`` suffix is the only thing the engine can tell an app's label apart by.
    Without it the app's own label read as an unknown person's and was re-asked every wake.
    """
    if not isinstance(actor, dict):
        return actor
    login = actor.get("login")
    if actor.get("__typename") == "Bot" and isinstance(login, str) and login:
        if not queue.is_bot_login(login):
            login = f"{login}[bot]"
    return {key: value for key, value in actor.items() if key != "__typename"} | {"login": login}


def _is_label_added_event(item: object, wanted: str) -> bool:
    """Is this event the named label being added? Anything unfamiliar is not it.

    Every label that means something to this engine — ownership (#108), spec-only (#2128) — is a
    per-project choice, so the name is passed in rather than read from the module: an engine
    shared by several projects must not decide which label any one of them owns.
    """
    if not isinstance(item, dict) or item.get("event") != "labeled":
        return False
    label = item.get("label")
    return (
        isinstance(label, dict)
        and _label_identity(label.get("name")) == _label_identity(wanted)
    )


def _label_identity(value: object) -> str:
    """GitHub label identity, preserving display spelling everywhere else."""
    return str(value or "").strip().casefold()


# How GitHub refuses a pull request diff over its size limit (#2478): `HTTP 406: Sorry, the
# diff exceeded the maximum number of lines (20000)`, with `too_large` as the API's own code.
# Unlike every other reason a diff read fails, this one does not go away for the same head.
_DIFF_TOO_LARGE_RE = re.compile(r"HTTP 406\b|\btoo_large\b|diff exceeded the maximum", re.I)


def review_diff_too_large(error: str) -> bool:
    """Whether a failed ``gh pr diff``'s stderr says GitHub will not serve a diff this big."""
    return bool(_DIFF_TOO_LARGE_RE.search(error or ""))


def review_diff_digest(output: str) -> str | None:
    """The digest an outside reviewer's ``/reviewed pass`` has to state (#2108, #2122, #2272).

    ``output`` is ``gh pr diff <n> -R <owner>/<repo>`` exactly as printed. This is the one
    definition: the engine's intake compares against it, and the deck's review chat writes it
    for the reviewer, which has no shell to compute it with. It is *not* ``sha256sum`` of that
    output — see ``_contributed_diff`` — so anything that hashes the raw bytes is refused.
    ``None`` when a binary section cannot be identified: unknown, never assumed equal.
    """
    contributed = _contributed_diff(output)
    if contributed is None:
        return None
    # Tagged so a digest written by an older poller — a hash of the raw output — is simply
    # a different value rather than one that might collide with a normalized one. Cards
    # holding an old digest read as changed once at the upgrade and pay one more review.
    return hashlib.sha256(("contributed-v1\n" + contributed).encode()).hexdigest()


def _contributed_diff(output: str) -> str | None:
    """The lines a pull request contributes, with where they sit in the base left out (#2122).

    A unified diff says two things at once: what this branch changes, and where that change
    currently lands. Only the first is what a reviewer passed a verdict on, and only the
    second moves when the branch is rebased. Three kinds of line carry the second:

    * ``index <old>..<new>`` — blob OIDs, which change whenever the base touched the file at
      all, even in a part of it this pull request never goes near;
    * ``@@ -a,b +c,d @@`` — line numbers, which shift whenever the base added or removed a
      line above the hunk;
    * context lines — lines of the *base*, quoted, which change whenever the base edited the
      few lines either side of a hunk.

    Hashing the raw output therefore made an ordinary rebase read as a changed diff, the
    review bit went red, and a card whose diff nobody had touched went back to a person for a
    second review. What is kept is the file headers, the mode and rename lines, one anonymous
    marker per hunk, and every added or removed line, in order.

    This says what a change contains and deliberately not where it sits, so on its own it
    cannot tell a block that moved to another function from one that did not. That is why the
    caller never lets a digest speak for a head an author pushed: ``_content_unchanged`` will
    only compare digests across a *moved* head when the engine is the one that moved it
    (``release.rebase_moved``). Position is the base's business, and whose head it is is the
    engine's — neither is this function's.

    One deliberate loss remains here. A base edit landing in the few lines *between* two
    nearby hunks can split one hunk in two, which changes the marker count and costs a review;
    the markers earn that, because without them one hunk's removals and the next one's
    additions run together into a digest that cannot tell ``-foo`` here and ``+bar`` there
    from the two of them in one place.

    Split on newlines and nothing else. ``str.splitlines`` also breaks on a form feed, a
    vertical tab and a handful of Unicode separators, any of which can sit inside a line of
    source this pull request adds; the tail after such a break can begin with a space and be
    dropped as context, quietly leaving contributed text out of the digest.

    A binary file is the one place where dropping ``index`` would throw the content away
    rather than the position. Git writes no ``+``/``-`` lines for one — just ``Binary files a/x
    and b/x differ`` — and that sentence names the path twice and says nothing about what is
    in the file, so two entirely different replacements of the same image would normalize to
    the same thing and carry a review nobody gave onto content nobody saw. The *post-image*
    OID is the contribution: unlike the pre-image beside it, it moves only when this pull
    request changes the file, not when the base does. So a binary section carries its
    post-image OID, and one whose OID cannot be read returns ``None`` — an unknown digest
    defers the release, where a content-independent one would have shipped it. Once that OID
    is in, the rest of the section is skipped: a ``GIT binary patch`` body is encoded against
    the pre-image, so hashing it would put the base's churn straight back in.
    """
    kept: list[str] = []
    index_line = ""
    binary = False
    for line in output.split("\n"):
        if line.startswith("diff --git "):
            # A new file section, so the previous one's blobs and its binary payload both stop
            # applying. Without this reset a binary file carrying no ``index`` of its own would
            # silently inherit the OID of whatever file came before it.
            index_line, binary = "", False
            kept.append(line)
            continue
        if binary:
            continue  # the section's identity is already its post-image OID
        if not line or line.startswith(_POSITION_RECORDS):
            if line.startswith("index "):
                index_line = line
            continue
        if line.startswith(("Binary files ", "GIT binary patch")):
            post = _post_image(index_line)
            if not post:
                return None
            kept.append("binary " + post)
            binary = True
            continue
        kept.append("@@" if line.startswith("@@") else line)
    return "\n".join(kept)


#: Records that say where a change sits, or how much of the base it still resembles — never
#: what the pull request contributes. Every one of them moves under a rebase that changed
#: nothing: ``index`` carries blob OIDs, a context line is the base's own text quoted back, and
#: the similarity percentages are computed from the pre- and post-images, so a base edit to a
#: renamed file moves the number while the rename and the contributed lines stay put.
#:
#: Deliberately a discard list and not a whitelist of what to keep. An unrecognized record that
#: is kept costs at worst one extra review; an unrecognized record that is dropped makes the
#: digest blind to part of the contribution, which is the failure this whole function exists to
#: prevent. Unknown records therefore still count, and this list grows when one is shown to be
#: derived from the base.
_POSITION_RECORDS = (" ", "index ", "similarity index ", "dissimilarity index ")


def _post_image(index_line: str) -> str:
    """The second blob OID of an ``index <pre>..<post>[ mode]`` line, or ``""``.

    Only the post-image is read. The pre-image is the base's blob and moves under a rebase —
    reading it back in is the very churn this whole function exists to ignore.

    An all-zero OID is kept rather than refused: that is how git writes a deleted file, which
    is a definite statement about the content, not a missing one.
    """
    body = index_line[len("index "):] if index_line.startswith("index ") else ""
    _, sep, rest = body.partition("..")
    post = (rest.split() or [""])[0] if sep else ""
    return post if post and all(c in "0123456789abcdefABCDEF" for c in post) else ""


def _split(ref: str) -> tuple[str, int]:
    repo, number = ref.split("#", 1)
    return repo, int(number)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP over a Unix socket, which the standard library does not offer by name."""

    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("agentdeck-runtime", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def runtime_post(
    socket_path: str, path: str, body: dict, *, timeout: float = DEFAULT_TIMEOUT_S
) -> tuple[int, object | None]:
    """One JSON ``POST`` to the runtime over its socket: ``(status, body)``, 0 when unreached."""
    connection = _UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request(
            "POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        raw = response.read()
        try:
            return response.status, json.loads(raw) if raw else None
        except ValueError:
            return response.status, None
    except (OSError, http.client.HTTPException):
        return 0, None
    finally:
        connection.close()


def _runtime_get(
    socket_path: str, path: str, *, timeout: float = DEFAULT_TIMEOUT_S
) -> object | None:
    """One JSON ``GET`` from the runtime over its socket; None unless it answers 200 with JSON."""
    connection = _UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            return None
        return json.loads(response.read())
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()
