"""The shim itself: one ``gh`` invocation, cached, bounded, or passed through.

Two rules govern every path out of here, and they are the ones #814 says must
not break:

* **An unanswered read is unknown, never empty.**  A read refused at the floor,
  or one whose refresh was throttled, serves the last trusted answer and says
  on stderr that it did.  With nothing cached it fails loudly.  Returning an
  empty list would be worse than failing, because the caller would believe it.
* **A write is never blocked.**  A worker that cannot comment, push or report
  its result is a worse failure than one reading stale data, and writes are not
  what exhausts a rate limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .. import github_identity
from ..github_cache import GitHubMetadata, GitHubMetadataCache
from ..poller_engine import github_authority
from ..poller_engine import rate_limits as shared_limits
from . import limits
from .classify import (
    Invocation,
    classify,
    credential_identity,
    is_terminal,
    ownership_label_to_add,
    repo_identity,
    target_owner,
    target_repo,
)
from .classify import repo_flag as classify_repo_flag
from .install import RECURSION_GUARD, real_gh

# Exit code for a read this shim declined to make.  Distinct from `gh`'s own
# codes so a caller can tell "GitHub said no" from "the deck said wait", and
# deliberately not 0: a refusal that looks like success is a silent wrong
# answer.
EXIT_REFUSED = 75
# No non-human authority may make this call (#2289): nothing ran, and no other
# identity will be tried. EX_NOPERM, so it reads as "not allowed", not "wait".
EXIT_NO_AUTHORITY = 77
EXIT_NO_GH = 127

# Asking for a fresh answer.  A worker that has just pushed must be able to see
# its own write; getting this wrong in the direction of too-fresh costs one API
# call, while getting it wrong the other way teaches workers to route around
# the shim entirely.
FRESH_ENV = "AGENTDECK_GH_FRESH"
FRESH_FLAG = "--agentdeck-fresh"

_TIMEOUT_S = 120.0
# Telling the deck about a filed issue must not hold the caller up (#3371).
_DECK_TIMEOUT_S = 3.0

# Filing an issue without the poller's ownership label, which `gh issue create` otherwise
# gains in a repository a poller manages (#2505). An environment variable rather than a
# flag: it is harmless where this shim is not on PATH, and the real `gh` never sees it.
NO_OWNER_LABEL_ENV = "AGENTDECK_NO_OWNER_LABEL"

# `gh issue create` refuses a label the repository lacks before it creates anything, so
# this one refusal is safe to answer by filing again without the label (#2505).
_LABEL_REFUSED = re.compile(r"could not add label", re.IGNORECASE)

# Every failed call, one JSON line each: which authority, installation and token spoke, and
# what GitHub answered (#2714). Beside the metadata cache, so a test's cache is its log too.
FAILURE_LOG_NAME = "gh-shim-failures.jsonl"
_FAILURE_LOG_MAX_BYTES = 2_000_000


def _await(coro):
    """Drive one coroutine from this synchronous entry point.

    The shim is its own short-lived process, so ``asyncio.run`` is normally
    right.  It is also called in-process by the deck's own tests, which already
    own a loop on this thread, and ``asyncio.run`` refuses that outright — so a
    thread of its own carries the coroutine there rather than making every
    caller of the shim asynchronous.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    carried: dict[str, object] = {}

    def run() -> None:
        carried["value"] = asyncio.run(coro)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return carried.get("value")


def _note(message: str) -> None:
    sys.stderr.write(f"agentdeck gh shim: {message}\n")


def _failure_log() -> Path:
    cache = os.environ.get("AGENTDECK_GITHUB_CACHE", "~/.cache/agentdeck/github_metadata.sqlite3")
    return Path(cache).expanduser().with_name(FAILURE_LOG_NAME)


def _speaker(environment: dict[str, str]) -> tuple[str, dict[str, object]]:
    """Who spoke, in words and as log fields; never the token itself (#2714)."""
    authority = environment.get(github_authority.AUTHORITY_ENV, "") or "unknown"
    try:
        facts = github_authority.cached_token_facts(environment)
    except Exception:  # noqa: BLE001 — a diagnostic must never fail the call it describes
        facts = {}
    token = environment.get("GH_TOKEN", "")
    fields: dict[str, object] = {
        "authority": authority,
        "credential": credential_identity(environment),
        "token_digest": hashlib.sha256(token.encode()).hexdigest()[:12] if token else "",
        **facts,
    }
    words = authority
    if facts.get("installation_id"):
        words += f" (installation {facts['installation_id']}"
        if facts.get("token_is_cached") and "token_expires_at" in facts:
            expires = datetime.fromtimestamp(float(facts["token_expires_at"]), UTC)
            words += f", cached token expiring {expires:%H:%M}Z"
        elif "token_is_cached" in facts and not facts["token_is_cached"]:
            words += ", a token other than the cached one"
        words += ")"
    return words, fields


def _log_failure(record: dict[str, object]) -> Path | None:
    """Append one failed call to the failure log. Observational: never fails the call."""
    path = _failure_log()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _FAILURE_LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        return None
    return path


def _report_failure(
    argv: list[str], environment: dict[str, str], raw_stderr: str, code: int, kind: str
) -> None:
    """Say what GitHub actually answered a failed call, and log who asked (#2714).

    A missing permission is named only when GitHub's answer shows one; anything else is
    reported as the status and body it was, so nobody is sent to the App settings for a 5xx.
    """
    target = target_repo(argv, remote=repo_identity())
    hint = github_authority.failure_hint(raw_stderr, environment, target)
    if hint:
        # Name the missing grant or App permission, never a second identity (#2289).
        _note(hint)
    summary = github_authority.failure_summary(raw_stderr)
    words, fields = _speaker(environment)
    path = _log_failure(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": kind,
            # The verb, never the rest of argv: a comment body is not log material.
            "command": " ".join(part for part in argv[:2] if not part.startswith("-")),
            "target": target,
            "exit": code,
            "hint": hint,
            "responses": [
                {
                    "method": response.method,
                    "path": response.path,
                    "status": response.status,
                    "request_id": response.headers.get("x-github-request-id", ""),
                    "failed": response.failed,
                }
                for response in github_authority.api_responses(raw_stderr)
            ],
            **fields,
        }
    )
    if summary or kind == "write":
        logged = f"; logged in {path}" if path else ""
        _note(f"{summary or 'gh failed'}; spoke as {words}{logged}")


def _run_real(
    argv: list[str],
    environment: dict[str, str],
    *,
    stderr_seen: list[str] | None = None,
    kind: str = "",
    quiet: bool = False,
) -> tuple[int, str]:
    """Run the actual ``gh``, capturing stdout and leaving stderr to the user.

    ``stderr_seen`` collects a copy of what was written to stderr, for the one caller
    that has to tell a refusal apart from any other failure. ``kind`` names the call in
    the failure log. ``quiet`` is for the shim's own quota probes (#3344): the caller
    never ran them, so their stderr is not shown and a failure is only logged.
    """
    def gave_up(code: int, why: str) -> tuple[int, str]:
        if quiet:
            _log_quiet_failure(argv, environment, "", code, kind, why=why)
        else:
            _note(why)
        return (code, "")

    binary = real_gh(environment)
    if binary is None:
        return gave_up(EXIT_NO_GH, "no real `gh` found on PATH")
    child = {**environment, RECURSION_GUARD: "1", "GH_DEBUG": "api"}
    try:
        done = subprocess.run(  # noqa: S603 — argv comes from our own caller
            [binary, *argv],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
            env=child,
        )
    except subprocess.TimeoutExpired:
        return gave_up(124, "`gh` timed out")
    except OSError as exc:
        return gave_up(EXIT_NO_GH, f"`gh` could not run: {exc}")
    clean_stderr, readings = shared_limits.strip_api_trace(done.stderr or "")
    store = shared_limits.RateLimitStore()
    credential = credential_identity(environment)
    if argv[:2] == ["api", "user"] and done.returncode == 0:
        store.bind(credential, shared_limits.user_id(done.stdout or ""))
    store.record_headers(credential, readings)
    if quiet:
        if done.returncode != 0:
            _log_quiet_failure(argv, environment, done.stderr or "", done.returncode, kind)
        return (done.returncode, done.stdout or "")
    sys.stderr.write(clean_stderr)
    if stderr_seen is not None:
        stderr_seen.append(clean_stderr)
    if done.returncode != 0:
        _report_failure(argv, environment, done.stderr or "", done.returncode, kind)
    return (done.returncode, done.stdout or "")


def _log_quiet_failure(
    argv: list[str],
    environment: dict[str, str],
    raw_stderr: str,
    code: int,
    kind: str,
    *,
    why: str = "",
) -> None:
    """Log a failed probe the caller never sees, with what GitHub (or the shim) answered."""
    _words, fields = _speaker(environment)
    _log_failure(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": kind,
            "command": " ".join(argv[:2]),
            "exit": code,
            "reason": why,
            "responses": [
                {"method": response.method, "path": response.path, "status": response.status}
                for response in github_authority.api_responses(raw_stderr)
            ],
            **fields,
        }
    )


def _record(cache: GitHubMetadataCache, environment: dict[str, str], kind: str) -> None:
    limits.CallAccounting(cache.path).record(limits.session_identity(environment), kind)


def _passthrough(
    argv: list[str],
    environment: dict[str, str],
    cache: GitHubMetadataCache,
    *,
    kind: str,
) -> int:
    """Hand the invocation to the real ``gh`` and forward its answer verbatim."""
    _record(cache, environment, kind)
    code, stdout = _run_real(argv, environment, kind=kind)
    sys.stdout.write(stdout)
    return code


def _ownership_label(
    argv: list[str], environment: dict[str, str], remote: str | None
) -> str | None:
    """The label this ``gh issue create`` should gain, or ``None`` (#2505).

    Imported and read here, not at module load, so no other ``gh`` call pays for the
    manifests — and anything that goes wrong files the issue as written, because a
    write is never blocked.
    """
    if environment.get(NO_OWNER_LABEL_ENV, "").strip() not in ("", "0", "false", "no"):
        return None
    try:
        from .. import github_accounts, poller_ops

        targets = poller_ops.issue_label_targets(github_accounts.poller_root())
        return ownership_label_to_add(argv, target_repo(argv, remote=remote), targets)
    except Exception as exc:  # noqa: BLE001 — the label is a default, never a gate
        _note(f"could not read the poller manifests ({exc}); filing without an ownership label")
        return None


def _issue_create(
    argv: list[str],
    environment: dict[str, str],
    cache: GitHubMetadataCache,
    remote: str | None,
) -> int:
    """File an issue, with the project's ownership label unless it opted out (#2505)."""
    label = _ownership_label(argv, environment, remote)
    _record(cache, environment, "write")
    if label is None:
        code, stdout = _run_real(argv, environment, kind="write")
    else:
        seen: list[str] = []
        code, stdout = _run_real(
            [*argv, "--label", label], environment, stderr_seen=seen, kind="write"
        )
        if code != 0 and not stdout.strip() and _LABEL_REFUSED.search("".join(seen)):
            _note(f"filing again without '{label}', which GitHub refused before creating anything")
            _record(cache, environment, "write")
            code, stdout = _run_real(argv, environment, kind="write")
        elif code == 0:
            _note(
                f"added label '{label}' so the deck starts a Work on this issue; "
                f"set {NO_OWNER_LABEL_ENV}=1 to file it without one"
            )
    sys.stdout.write(stdout)
    if code == 0:
        _tell_deck_filed(stdout, environment)
    return code


def _tell_deck_filed(stdout: str, environment: dict[str, str]) -> None:
    """Tell the deck the issue GitHub just confirmed, so its Work starts now (#3371).

    GitHub's own webhook for the new issue can be held back for many minutes. Only the
    address is sent: the deck reads the issue's labels back from GitHub. Best effort and
    quiet: the webhook and the deck's backstop still start the Work if this is lost, and
    the issue is filed either way.
    """
    base = environment.get("AGENTDECK_URL", "").strip().rstrip("/")
    found = re.findall(r"https://github\.com/[^/\s]+/[^/\s]+/issues/\d+", stdout)
    if not base or not found:
        return
    body = json.dumps({"issue": found[-1]}).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/api/works/filed",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_DECK_TIMEOUT_S) as response:  # noqa: S310
            answer = json.loads(response.read() or b"{}")
    except Exception as exc:  # noqa: BLE001 — the webhook is still on its way
        _note(f"could not tell the deck about {found[-1]} ({exc}); its webhook will start it")
        return
    if isinstance(answer, dict) and answer.get("starting") is True:
        _note(f"the deck is starting the Work for {found[-1]}")


def _quota(cache: GitHubMetadataCache, environment: dict[str, str]) -> limits.RateLimits:
    """Read real-call headers, using ``/rate_limit`` only to fill cold-start gaps."""
    store = shared_limits.RateLimitStore(cache.path)
    credential = credential_identity(environment)
    shared_limits.bind_authority(store, environment, credential)
    readings = store.read(credential, max_age=limits.LIMITS_TTL_S)

    # Binding token fingerprints to the authenticated user makes two tokens
    # share one budget. The probe is skipped while any real core reading is
    # fresh, so ordinary proxied calls continuously refresh it for free.
    core = readings.get("core")
    if not store.is_bound(credential) or core is None:
        _record(cache, environment, "quota-probe")
        # An App installation token cannot read `GET /user` (#3344): every probe was a 403
        # in the caller's output. Its own repository list answers with the same core headers.
        app = environment.get(github_authority.AUTHORITY_ENV, "").startswith("app:")
        probe = ["api", "installation/repositories?per_page=1"] if app else ["api", "user"]
        _run_real(probe, environment, kind="quota-probe", quiet=True)
        readings = store.read(credential, max_age=limits.LIMITS_TTL_S)

    # GraphQL may not have been used since startup. Its /rate_limit payload is
    # retained only as an explicitly untrusted estimate until a real GraphQL
    # response header replaces it.
    estimate = limits.RateLimits()
    if "graphql" not in readings:
        _record(cache, environment, "quota-probe")
        code, stdout = _run_real(["api", "rate_limit"], environment, kind="quota-probe", quiet=True)
        if code == 0:
            try:
                payload = json.loads(stdout or "null")
                estimate = limits.parse_rate_limit(payload)
                store.record_estimate(credential, payload)
            except ValueError:
                pass
        readings = store.read(credential, max_age=limits.LIMITS_TTL_S)
    shared = limits.from_shared(readings)
    return limits.RateLimits(
        core=shared.core if shared.core is not None else estimate.core,
        graphql=shared.graphql if shared.graphql is not None else estimate.graphql,
        core_source=shared.core_source or estimate.core_source,
        graphql_source=shared.graphql_source or estimate.graphql_source,
    )


def _emit(payload: object) -> int:
    """Write a cached answer out as the real ``gh`` would have."""
    if isinstance(payload, dict):
        sys.stdout.write(str(payload.get("stdout") or ""))
        code = payload.get("code")
        return code if isinstance(code, int) and not isinstance(code, bool) else 0
    return 0


# What a refused read with nothing cached tells its caller to do, by why it was refused
# (#3344). Only a quota refusal is a reason to wait for quota.
QUOTA = "quota"
BUDGET = "budget"
GITHUB = "github"
_REFUSAL_ADVICE = {
    QUOTA: "This is unknown, not empty: wait for quota rather than retrying.",
    BUDGET: "This is unknown, not empty: wait for the hour to roll over rather than retrying.",
    GITHUB: (
        "GitHub was slow or failing for this read just now, and quota is fine. "
        "This is unknown, not empty: run it once more in a minute."
    ),
}


def _refuse(
    cache: GitHubMetadataCache,
    decision: Invocation,
    why: str,
    *,
    environment: dict[str, str] | None = None,
    cause: str = QUOTA,
) -> int:
    """A read we will not make: last trusted answer, or a loud failure."""
    cached = cache.peek(decision.key)
    if cached.value is not None:
        _note(f"{why} — serving the last cached answer, which may be stale")
        return _emit(cached.value)
    _note(f"{why} — refusing this read and nothing is cached. {_REFUSAL_ADVICE[cause]}")
    _words, fields = _speaker(environment or {})
    # Counted, so a later look can tell how often reads go unanswered and why (#3344).
    _log_failure(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": "refused",
            "cause": cause,
            "reason": why,
            "command": f"{decision.group} {decision.verb}".strip(),
            "exit": EXIT_REFUSED,
            **fields,
        }
    )
    return EXIT_REFUSED


def _read(
    decision: Invocation,
    argv: list[str],
    environment: dict[str, str],
    cache: GitHubMetadataCache,
    *,
    fresh: bool,
) -> int:
    # Both gates are checked out here rather than inside the fetch, and only
    # when a live call is actually coming.  A gate refused inside the fetch
    # would look to the cache like GitHub failing, backing the shared key off
    # for every other worker — so one session's exhausted bucket would fence
    # the host.  Out here a cache hit stays free, uncounted and ungated.
    if fresh or not cache.is_fresh(decision.key):
        blocked = _quota(cache, environment).below_floor()
        if blocked:
            return _refuse(
                cache, decision, f"{blocked}, below the worker floor", environment=environment
            )
        budget = limits.FetchBudget(cache.path)
        allowed, spent = budget.spend(limits.session_identity(environment))
        if not allowed:
            return _refuse(
                cache,
                decision,
                f"this session has already made {spent} live GitHub reads this hour",
                environment=environment,
                cause=BUDGET,
            )

    served: dict[str, object] = {}

    async def fetch() -> GitHubMetadata | None:
        _record(cache, environment, "read")
        code, stdout = _run_real(argv, environment, kind="read")
        if code != 0 and not stdout.strip():
            # A failure with nothing but stderr behind it is not an answer, so
            # it is never pinned as one.  `gh` failing is very often the rate
            # limit itself, which is why this takes the cache's failure backoff
            # rather than being re-issued on the next call.
            served["failed"] = {"code": code, "stdout": stdout}
            return None
        payload: object = None
        try:
            payload = json.loads(stdout or "null")
        except ValueError:
            payload = None
        # A non-zero code carrying output *is* an answer: `gh pr checks` exits
        # 8 with the pending checks listed, and that is precisely the read a
        # worker is tempted to sit in a loop on. Caching it with its exit code
        # keeps `_CHECKS_PENDING` intact while making the loop cost nothing.
        return GitHubMetadata(
            {"code": code, "stdout": stdout},
            terminal=code == 0 and is_terminal(decision.group, decision.verb, payload),
        )

    def resolve(*, ignore_backoff: bool = False):
        return _await(
            cache.resolve(
                decision.key,
                fetch,
                force=fresh,
                fresh_after=time.time() if fresh else None,
                ignore_backoff=ignore_backoff,
            )
        )

    lookup = resolve()
    # Nothing came back without this process asking GitHub (#3344): another process is
    # fetching the same read, or its fetch failed within the back-off. Wait for the first
    # rather than refusing, and make our own one call for the second — the quota and
    # session gates above have already passed it.
    waited = lookup.value is None and "failed" not in served and lookup.reason == "leased"
    if waited:
        lookup = cache.wait_for(decision.key, cache.LEASE_S)
    if (
        lookup.value is None
        and "failed" not in served
        and (lookup.reason == "backoff" or (waited and not lookup.reason))
    ):
        lookup = resolve(ignore_backoff=True)
    failed = served.get("failed")
    if isinstance(failed, dict):
        return _emit(failed)
    if lookup.value is None:
        if lookup.reason == "leased":
            why = f"another call for this read was still running after {cache.LEASE_S:.0f} s"
        elif lookup.reason == "backoff" and lookup.failed_s is not None:
            why = f"another call for this read failed {lookup.failed_s:.0f} s ago"
        else:
            why = "GitHub did not answer"
        return _refuse(cache, decision, why, environment=environment, cause=GITHUB)
    if not lookup.complete:
        _note("could not refresh — serving the last cached answer, which may be stale")
    return _emit(lookup.value)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = dict(os.environ)
    cache = GitHubMetadataCache()

    # Already inside a shim-launched `gh`: never wrap ourselves twice.
    if environment.get(RECURSION_GUARD):
        return _passthrough(arguments, environment, cache, kind="recursive")

    fresh = environment.get(FRESH_ENV, "").strip() not in ("", "0", "false", "no")
    if FRESH_FLAG in arguments:
        arguments = [part for part in arguments if part != FRESH_FLAG]
        fresh = True

    # Which authority speaks, decided before anything else (#2105, #2289). It has
    # to come first because `credential_identity` keys both the cache and the
    # quota store. A call that names no owner, or an owner with no authority, is
    # refused here: the real `gh` is never reached with whatever this process
    # inherited, and no second identity is tried.
    #
    # The remote is resolved at most once and handed to `classify`, which would
    # otherwise spawn the same `git config` read a second time. A `--repo` flag
    # makes even that unnecessary.
    remote = None if classify_repo_flag(arguments) else repo_identity()
    try:
        environment = github_identity.environment_for(
            None, environment, owner=target_owner(arguments, remote=remote)
        )
    except github_identity.Refusal as refusal:
        reason = str(refusal)
        if reason.startswith("no target owner"):
            reason = (
                f"no target owner for gh {' '.join(arguments[:2])}: run it inside a "
                "GitHub checkout or pass --repo owner/name"
            )
        _note(f"refused — github: {reason}. Nothing ran; no other identity is tried.")
        return EXIT_NO_AUTHORITY

    decision = classify(arguments, environment=environment, remote=remote)
    if decision.kind == "refuse":
        _note(
            f"{decision.reason}. Watching is polling: ask once, act, and let the "
            "next dispatch bring you back."
        )
        return EXIT_REFUSED

    if decision.kind == "write":
        # Never retried: a write's non-zero exit is not proof that nothing landed. The
        # one exception, inside `_issue_create`, is a refusal GitHub gives before writing.
        if (decision.group, decision.verb) == ("issue", "create"):
            code = _issue_create(arguments, environment, cache, remote)
        else:
            code = _passthrough(arguments, environment, cache, kind="write")
        if decision.invalidates and code == 0:
            cache.invalidate(decision.invalidates)
        return code

    plain = _view_without_comments(decision, arguments)
    if plain is not None and not sys.stdout.isatty():
        # Off a terminal, `gh issue view --comments` prints the comments and no issue
        # body, so nearly every Work chat read its issue twice (#3584). Print the plain
        # view first, then the comments, both through the cache as any other read. Both
        # are held back until both succeeded: a failed half prints only its own answer,
        # never a body that reads like the whole result.
        plain_decision = classify(plain, environment=environment, remote=remote)
        code, body = _captured(
            lambda: _read(plain_decision, plain, environment, cache, fresh=fresh)
        )
        if code != 0:
            sys.stdout.write(body)
            return code
        code, comments = _captured(
            lambda: _read(decision, arguments, environment, cache, fresh=fresh)
        )
        if code != 0:
            sys.stdout.write(comments)
            return code
        sys.stdout.write(f"{body}\n--- comments ---\n{comments}")
        return 0

    return _read(decision, arguments, environment, cache, fresh=fresh)


def _captured(read) -> tuple[int, str]:
    """Run one ``_read`` with its standard output held back: ``(exit code, output)``."""
    held = io.StringIO()
    with contextlib.redirect_stdout(held):
        code = read()
    return code, held.getvalue()


#: Flags that make `gh … view --comments` print something other than plain text.
_FORMATTED_VIEW = frozenset({"--json", "-q", "--jq", "-t", "--template", "-w", "--web"})
_COMMENTS = frozenset({"--comments", "-c"})


def _view_without_comments(decision: Invocation, argv: list[str]) -> list[str] | None:
    """The same `issue view`/`pr view` without ``--comments``, when it asks for comments
    as plain text; ``None`` for anything else. The subcommand is the one ``classify``
    found, so a global flag in front (`gh -R o/r issue view 1 -c`) is still recognised."""
    if (decision.group, decision.verb) not in (("issue", "view"), ("pr", "view")):
        return None
    names = [part.split("=", 1)[0] for part in argv if part.startswith("-")]
    if not _COMMENTS.intersection(names) or _FORMATTED_VIEW.intersection(names):
        return None
    return [part for part in argv if part.split("=", 1)[0] not in _COMMENTS]
