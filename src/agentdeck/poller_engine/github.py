"""GitHub reads and effects for the engine's port (#180).

It lives in the engine package, because a wake that cannot read GitHub cannot run at all: `gh`
over subprocess, nothing else. Merging it with the deck's own `gh_cli` and `github_cache` is a
later step (#2597). The engine holds no cards since #3732, so what is left is what a housekeeping
wake needs — the budget gate, permission and issue-state reads, opening and finding issues, the
scheduled-task sweep, comments and their tidying — plus activation's board discovery, the host
worktree removal the reaper and Work claims share, and the deck's review-diff digest.

The engine core proper stays deterministic and I/O-free; this module is the one place in the
package that talks to the outside, and it makes no decisions of its own.

Two habits run through it:

* **A read that fails answers "unknown", never "no".** An unlistable repository, an unavailable
  permission — each returns ``None`` so the engine can defer. An adapter that turned a network
  blip into a negative answer would quietly change policy.
* **One credential: the instance's non-human authority** (#2289). The owner rule in
  `github_authority` picks it (`eerovil-agent`, or the AgentDeck App's installation on the
  organization) and every read and effect speaks as it. There is no host login: a `Gh` with no
  credential refuses the call rather than letting `gh` fall back to whoever is logged in.
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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ..models import USAGE_WINDOW_KEYS
from . import (
    github_authority,
    rate_limits,
    schedules,
)
from .config import (
    DEFAULT_CORE_RESERVE,
    DEFAULT_GRAPHQL_RESERVE,
    Reachability,
    RepoCheck,
    boardless_snapshot,
)
from .ports import (
    CommentBody,
    Effect,
    EffectKind,
    WorktreeCleanup,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 45
# The runtime's Work workspace routes (#2149); `agentdeck.work_workspace_claims` owns them
# and a test pins the spelling. A release removes worktrees, so it gets longer.
WORK_WORKSPACE_ENSURE_PATH = "/work-workspaces/ensure"
WORK_WORKSPACE_RELEASE_PATH = "/work-workspaces/release"
WORK_WORKSPACE_CLAIMS_PATH = "/work-workspaces/claims"


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


BOT_MARKER = "<!-- claude-bot -->"
# The ChatGPT reviewer's handoff comments are superseded the same way (#2167): every round
# posts a fresh findings list, and only the newest is what a worker still has to act on. The
# reviewer skill in the review-chat repository ends each one with this marker; the heading is how
# the ones posted before that marker existed are recognised.
# The same spelling as ``issue_post.FINDINGS_MARKER``, which the deck appends to a review it posts.
CHATGPT_REVIEW_MARKER = "<!-- chatgpt-review -->"
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


# How many recent issues to read when looking for one this engine already opened (#393). Bounded
# because it runs only to recover an interrupted creation, and the issue being looked for was
# created moments ago by definition.
MARKED_ISSUE_SCAN = 50
# How many scheduled tasks one sweep reads per repository (#2517). Every open one, or the sweep
# refuses rather than mistake a task it never saw for a deleted one; and enough recently closed
# ones to notice a task that was stopped since the last full wake.
SCHEDULED_OPEN_PAGE = 100
SCHEDULED_CLOSED_PAGE = 30


# `gh` exits with this when this adapter refused to run it at all: no credential (#2289).
EXIT_NO_AUTHORITY = 77


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

        A *full* wake does not get that offer, and `rate_limits.GATE_MAX_AGE_S` says why: an
        extension may spend core quota without recording it, and a quiet wake is the one it is
        told not to.
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


def account_refusal(usage_cache: Path | None, account: str, *, now: datetime | None = None) -> str:
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
    project_number: int
    api: Gh = field(default_factory=Gh)  # the instance's one authority (#2289)
    # Kept as a second name for the same path so existing call sites read unchanged; left
    # unset it *is* ``api``. There is no separate host login any more.
    host: Gh | None = None
    now: datetime | None = None
    # Which label means "this engine owns the card". The Manifest names it, because it is
    # portable semantic configuration about the project's world (#108) rather than something
    # the shared engine gets to decide for every project at once.
    ownership_label: str = OWNERSHIP_LABEL
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

    # --- budget ---

    def __post_init__(self) -> None:
        if self.host is None:
            self.host = self.api

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

    # --- small helpers ---

    def _slug(self, repo: str) -> str:
        return f"{self.org}/{repo}"

    def _moment(self) -> datetime:
        return self.now or datetime.now(UTC)

    @property
    def boardless(self) -> bool:
        """No GitHub Project behind this project: status is a `status:*` label (#1481)."""
        return self.project_number == 0

    # --- authorization input (#110: the engine decides what this means) ---

    def permission(self, repo: str, login: str) -> str | None:
        """The collaborator level GitHub reports, or None when it could not be read."""
        if not login:
            return None
        value = self.api.json("api", f"repos/{self._slug(repo)}/collaborators/{login}/permission")
        if not isinstance(value, dict):
            return None
        level = value.get("permission")
        return str(level) if isinstance(level, str) else None

    # --- the GraphQL reserve ---

    def _graphql_has_room(self) -> bool:
        self._probe_rate_limits()
        return self._graphql_remaining is None or self._graphql_remaining > self.graphql_reserve

    def _probe_rate_limits(self, *, stale_reading_ok: bool = False) -> None:
        if self._rate_limits_checked:
            return
        self._rate_limits_checked = True
        max_age = rate_limits.GATE_MAX_AGE_S if stale_reading_ok else rate_limits.READING_MAX_AGE_S
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

    # --- ownership provenance ---

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
            'projectV2(number:$number){id field(name:"Status"){'
            "... on ProjectV2SingleSelectField{id options{id name}}}}}}"
        )
        value = self.api.json(
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"org={org}",
            "-F",
            f"number={project_number}",
        )
        project = None
        if isinstance(value, dict):
            project = ((value.get("data") or {}).get("organization") or {}).get("projectV2")
        if not isinstance(project, dict) or not project.get("id"):
            # A user-owned board answers under `user`, not `organization`.
            query = query.replace("organization(login:$org)", "user(login:$org)")
            value = self.api.json(
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"org={org}",
                "-F",
                f"number={project_number}",
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

    # --- opening an issue (#393) ---

    def find_marked_issue(self, repo: str, marker: str, *, since: str) -> tuple[int | None, bool]:
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
            "issue",
            "list",
            "-R",
            self._slug(repo),
            "--state",
            "all",
            "--limit",
            str(MARKED_ISSUE_SCAN),
            "--json",
            "number,body,createdAt",
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
            "issue",
            "create",
            "-R",
            self._slug(repo),
            "--title",
            title,
            "--body",
            body,
            "--label",
            self.ownership_label,
            *extra,
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
            "issue",
            "create",
            "-R",
            self._slug(repo),
            "--title",
            title,
            "--body",
            body,
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
            names = (
                {
                    str((label or {}).get("name") or "").casefold()
                    for label in ((node.get("labels") or {}).get("nodes") or [])
                }
                if isinstance(node, dict)
                else set()
            )
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
                pull_request_url = f"https://github.com/{self._slug(pr_repo)}/pull/{int(pr_number)}"
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
        if effect.kind is EffectKind.COMMENT:
            body = self._comment_body(effect.repo, effect.number, effect.value)
            landed = self.api.run("issue", "comment", number, "-R", slug, "--body", body)[0] == 0
            if landed:
                # Best-effort tidying, deliberately after the fact and deliberately ignored:
                # the comment is the durable thing and a failed retirement must never turn a
                # landed comment into one the caller thinks did not land.
                self.retire_superseded_comments(effect.repo, effect.number)
            return landed
        if effect.kind is EffectKind.SET_BODY:
            body = str(effect.value or "")
            if not body:
                return False
            return self.api.run("issue", "edit", number, "-R", slug, "--body", body)[0] == 0
        if effect.kind is EffectKind.PIN_ISSUE:
            return self.api.run("issue", "pin", number, "-R", slug)[0] == 0
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
            "api",
            "graphql",
            "-f",
            "query=" + query,
            "-f",
            "owner=" + self.org,
            "-f",
            "repo=" + repo,
            "-F",
            "number=" + str(int(number)),
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
        ids = [node_id for node_id in stale if _COMMENT_NODE_ID_RE.match(node_id)][
            :RETIRE_COMMENT_LIMIT
        ]
        if not ids or not self._graphql_has_room():
            return 0
        fields = "".join(
            f"r{index}:minimizeComment("
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
        return sum(1 for index in range(len(ids)) if isinstance(applied.get(f"r{index}"), dict))

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
    body = index_line[len("index ") :] if index_line.startswith("index ") else ""
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
