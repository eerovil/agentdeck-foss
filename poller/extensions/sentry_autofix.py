#!/usr/bin/env python3
"""sentry-autofix: propose work for unresolved production errors (#179, a #113 reference).

The engine has no idea what Sentry is, and should not: this extension watches it and *asks*
for work. Each proposal carries a stable key — the issue's short id — so the engine's
exact-once ledger deduplicates it, and the proposal then queues, dispatches, and is
authorized exactly like a human's instruction. Nothing here can bypass a lease, jump the
worker cap, or start a second worker on a card.

Three deliberate restraints:

* **A bounded number of proposals per wake.** A quiet week followed by a bad deploy can turn
  hundreds of issues unresolved at once; proposing them all would flood the board and the
  budget. Whatever is not proposed this wake is still there next wake — and the bound is spent
  newest-first on incidents the engine has *not* already decided (#1709), because a card does
  not resolve its Sentry issue and re-asking for one would starve every new error behind it.
* **Only issues it can route.** An issue whose Sentry project maps to no repository in the
  Manifest is reported as a problem to fix, not silently dropped and not guessed at.
* **One card per incident.** Configured infrastructure signatures are dropped, and similar issues
  first seen in one short repository-local burst are represented by one proposal. Opening remains
  the engine's job and is exactly-once there (#393); this only asks.
* **Only what the project asked to hear about.** Each project states its own Sentry search,
  and a project marked ``suppressed`` is never even fetched — a browser project that reports
  every user's extension noise should cost nothing, not merely be discarded after paying for it.

These bound what is *asked for*. What is *honored* is the engine's business: a per-day ceiling
and a cap on concurrent proposal-driven workers live in ``[policy.proposals]``, because an
extension that polices its own budget is trusting the thing being bounded (#112).

Hooks: validate-config, observe, propose, health. Stdlib only.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

DEFAULT_MAX_PROPOSALS = 3
DEFAULT_WINDOW = "24h"
DEFAULT_QUERY = "is:unresolved"
DEFAULT_BURST_WINDOW_MINUTES = 15
DEFAULT_TITLE_SIMILARITY = 0.8
_TIMEOUT_S = 10


def _read_payload() -> dict:
    try:
        value = json.load(sys.stdin)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _answer(**parts) -> int:
    print(json.dumps(parts))
    return 0


# --- the configured projects ---

_LOCATION = "manifest.extensions.sentry-autofix"


def _problem(location: str, what: str, fix: str) -> dict:
    return {"location": location, "what": what, "fix": fix}


def _projects(config: dict) -> tuple[list[dict], list[dict]]:
    """The configured projects, normalized and ordered, plus every shape problem found.

    One entry per project with everything it does stated inline, rather than a routing map
    shadowed by parallel maps of queries and exclusions. That shape is what the Manifest
    already rejected for repositories: it turns "what happens to this project?" into a
    cross-reference exercise, and the parallel halves drift apart.
    """
    problems: list[dict] = []
    if "project_to_repo" in config:
        # A clean break rather than two accepted shapes: sentry-autofix is enabled on no
        # instance, so the only thing back-compat could buy is two ways to say one thing.
        problems.append(
            _problem(
                f"{_LOCATION}.project_to_repo",
                "replaced by one [[extensions.sentry-autofix.projects]] entry per project",
                'write [[extensions.sentry-autofix.projects]] with id, repo, and optionally '
                "query or suppressed",
            )
        )
    raw = config.get("projects")
    if not isinstance(raw, list) or not raw:
        problems.append(
            _problem(
                f"{_LOCATION}.projects",
                "missing or empty, so no issue could ever be routed to a repository",
                "add one [[extensions.sentry-autofix.projects]] entry per Sentry project",
            )
        )
        return [], problems

    entries: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{_LOCATION}.projects[{index}]"
        if not isinstance(item, dict):
            problems.append(_problem(where, "is not a table", "write it as [[…projects]]"))
            continue
        pid = str(item.get("id") or "").strip()
        repo = str(item.get("repo") or "").strip()
        if not pid:
            problems.append(
                _problem(f"{where}.id", "missing", "set id to the Sentry project slug")
            )
        if not repo:
            problems.append(
                _problem(
                    f"{where}.repo",
                    "missing, so an issue here could not be routed",
                    "set repo to the repository that owns this project's code",
                )
            )
        if pid in seen:
            problems.append(
                _problem(f"{where}.id", f"{pid!r} is configured twice", "remove the duplicate")
            )
        query = item.get("query")
        if query is not None and (not isinstance(query, str) or not query.strip()):
            problems.append(
                _problem(
                    f"{where}.query",
                    "must be a non-empty Sentry search string when set",
                    'set query to a Sentry search, e.g. "is:unresolved !level:warning"',
                )
            )
            query = None
        suppressed = item.get("suppressed", False)
        if not isinstance(suppressed, bool):
            problems.append(
                _problem(f"{where}.suppressed", "must be true or false", "set suppressed = true"),
            )
            suppressed = bool(suppressed)
        if not pid or not repo:
            continue
        seen.add(pid)
        entries.append(
            {
                "id": pid,
                "repo": repo,
                "query": query.strip() if isinstance(query, str) else None,
                "suppressed": suppressed,
            }
        )
    return sorted(entries, key=lambda entry: entry["id"]), problems


# --- validate-config ---

def _validate(config: dict) -> list[dict]:
    problems = []
    if not str(config.get("org") or ""):
        problems.append(
            {
                "location": "manifest.extensions.sentry-autofix.org",
                "what": "missing",
                "fix": "set org to the Sentry organization slug",
            }
        )
    problems.extend(_projects(config)[1])
    token = str(config.get("token_file") or "")
    if not token:
        problems.append(
            {
                "location": "manifest.extensions.sentry-autofix.token_file",
                "what": "missing",
                "fix": "point token_file at the file holding a Sentry read token",
            }
        )
    elif not Path(token).expanduser().exists():
        # A path only this host can satisfy: an authored Manifest cannot know it exists.
        problems.append(
            {
                "location": "manifest.extensions.sentry-autofix.token_file",
                "what": f"{token} does not exist on this host",
                "fix": "create the token file, or correct the path in the Host Overlay",
            }
        )
    cap = config.get("max_proposals", DEFAULT_MAX_PROPOSALS)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        problems.append(
            {
                "location": "manifest.extensions.sentry-autofix.max_proposals",
                "what": "must be a positive integer",
                "fix": "set how many issues may be proposed in one wake",
            }
        )
    signatures = config.get("deny_signatures", [])
    if not isinstance(signatures, list):
        problems.append(
            {
                "location": f"{_LOCATION}.deny_signatures",
                "what": "must be a list of non-empty title fragments",
                "fix": "set deny_signatures to a list of strings",
            }
        )
    else:
        for index, signature in enumerate(signatures):
            if not isinstance(signature, str) or not signature.strip():
                problems.append(
                    {
                        "location": f"{_LOCATION}.deny_signatures[{index}]",
                        "what": "must be a non-empty string",
                        "fix": "set a case-insensitive fragment found in the Sentry title",
                    }
                )
    burst_window = config.get("burst_window_minutes", DEFAULT_BURST_WINDOW_MINUTES)
    if not isinstance(burst_window, int) or isinstance(burst_window, bool) or burst_window < 1:
        problems.append(
            {
                "location": f"{_LOCATION}.burst_window_minutes",
                "what": "must be a positive integer",
                "fix": "set the maximum minutes between grouped Sentry issues",
            }
        )
    similarity = config.get("title_similarity", DEFAULT_TITLE_SIMILARITY)
    if (
        not isinstance(similarity, (int, float))
        or isinstance(similarity, bool)
        or not 0 <= similarity <= 1
    ):
        problems.append(
            {
                "location": f"{_LOCATION}.title_similarity",
                "what": "must be a number from 0 to 1",
                "fix": "set the minimum normalized-title similarity for burst grouping",
            }
        )
    return problems


# --- observing Sentry ---

def _token(config: dict) -> str:
    try:
        return Path(str(config.get("token_file") or "")).expanduser().read_text().strip()
    except OSError:
        return ""


def _issues(config: dict, project: str, search: str | None = None) -> list[dict] | None:
    """Issues matching this project's search, or None when Sentry could not be read.

    ``search`` is the project's own override; without one the block-level default applies. The
    filtering happens at Sentry, so a suppressed class never crosses the wire at all.
    """
    host = str(config.get("host") or "https://sentry.io").rstrip("/")
    org = str(config.get("org") or "")
    query = urllib.parse.urlencode(
        {
            "query": search or str(config.get("query") or DEFAULT_QUERY),
            "statsPeriod": str(config.get("window") or DEFAULT_WINDOW),
        }
    )
    url = f"{host}/api/0/projects/{org}/{project}/issues/?{query}"
    request = urllib.request.Request(url)
    token = _token(config)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
            body = json.loads(response.read().decode() or "[]")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body if isinstance(body, list) else None


def _issue(config: dict, reference: str) -> dict | None:
    """Fetch one issue exactly, without the automatic observer's search filters."""
    raw = reference.strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        try:
            issue_id = parts[parts.index("issues") + 1]
        except (ValueError, IndexError):
            return None
    else:
        issue_id = raw.strip("/")
    if not issue_id:
        return None
    host = str(config.get("host") or "https://sentry.io").rstrip("/")
    org = str(config.get("org") or "")
    endpoint = "issues" if issue_id.isdigit() else "shortids"
    url = f"{host}/api/0/organizations/{org}/{endpoint}/{urllib.parse.quote(issue_id)}/"
    request = urllib.request.Request(url)
    token = _token(config)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
            body = json.loads(response.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if endpoint == "shortids" and isinstance(body, dict):
        body = body.get("group")
    return body if isinstance(body, dict) and body.get("id") else None


def _manual_request(config: dict, issue: dict, entries: list[dict]) -> dict | None:
    """Build one explicit open-work request without automatic burst filtering."""
    project = str((issue.get("project") or {}).get("slug") or "")
    route = next((entry for entry in entries if entry["id"] == project), None)
    if route is None:
        return None
    short_id = str(issue.get("shortId") or issue.get("id") or "")
    if not short_id:
        return None
    target = str(config.get("issue_repo") or route["repo"])
    title = str(issue.get("title") or "").strip() or short_id
    return {
        "kind": "open-work",
        "payload": {
            "repo": target,
            "key": short_id,
            "title": f"{short_id}: {title}",
            "instruction": (
                f"Triage and fix Sentry {short_id}: {title} "
                f"({issue.get('permalink') or ''})"
            ),
            "url": str(issue.get("permalink") or ""),
        },
    }


_QUOTED_TEXT = re.compile(r'''(["']).*?\1''')
_NUMBER = re.compile(r"\b\d+\b")
_SPACE = re.compile(r"\s+")


def _normalized_title(title: str) -> str:
    """Remove volatile details while keeping the error's recognizable shape."""
    value = _QUOTED_TEXT.sub("<text>", title.casefold())
    value = _NUMBER.sub("<n>", value)
    return _SPACE.sub(" ", value).strip()


def _first_seen(issue: dict) -> float | None:
    raw = issue.get("firstSeen")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _deny_signatures(config: dict) -> tuple[str, ...]:
    raw = config.get("deny_signatures", [])
    if not isinstance(raw, list):
        return ()
    return tuple(
        signature.strip().casefold()
        for signature in raw
        if isinstance(signature, str) and signature.strip()
    )


def _burst_groups(candidates: list[dict], config: dict) -> list[list[dict]]:
    """Group one short burst without making Sentry's issue id the incident id."""
    raw_window = config.get("burst_window_minutes", DEFAULT_BURST_WINDOW_MINUTES)
    window = (
        raw_window
        if isinstance(raw_window, int) and not isinstance(raw_window, bool) and raw_window > 0
        else DEFAULT_BURST_WINDOW_MINUTES
    )
    raw_similarity = config.get("title_similarity", DEFAULT_TITLE_SIMILARITY)
    similarity = (
        float(raw_similarity)
        if isinstance(raw_similarity, (int, float))
        and not isinstance(raw_similarity, bool)
        and 0 <= raw_similarity <= 1
        else DEFAULT_TITLE_SIMILARITY
    )
    groups: list[list[dict]] = []
    for candidate in candidates:
        match = None
        for group in groups:
            leader = group[0]
            if candidate["repo"] != leader["repo"]:
                continue
            if candidate["first_seen"] is None or leader["first_seen"] is None:
                continue
            times = [item["first_seen"] for item in group]
            if max(*times, candidate["first_seen"]) - min(
                *times, candidate["first_seen"]
            ) > window * 60:
                continue
            if (
                SequenceMatcher(
                    None, candidate["normalized_title"], leader["normalized_title"]
                ).ratio()
                < similarity
            ):
                continue
            match = group
            break
        if match is None:
            groups.append([candidate])
        else:
            match.append(candidate)
    for group in groups:
        group.sort(
            key=lambda item: (
                item["first_seen"] is None,
                item["first_seen"] if item["first_seen"] is not None else item["index"],
                item["short_id"],
            )
        )
    return groups


def _settled(context: dict | None) -> frozenset[str]:
    """The keys the engine has already decided, so a slot is never spent re-asking (#1709).

    Filing a card does not resolve or assign the Sentry issue, so ``is:unresolved
    is:unassigned`` keeps reporting it for the whole window. Without this the bounded number
    of proposals per wake goes entirely to work the engine drops as already-seen, and a new
    incident waits for the old ones to age out — which is not "bounded", it is stalled.
    """
    raw = (context or {}).get("settled")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(item.strip() for item in raw if isinstance(item, str) and item.strip())


def _newest_first(group: list[dict]) -> tuple[int, float, int]:
    """Sort key putting the most recent incident first, and undated ones last.

    Ordering is global rather than per project, because ``groups[:cap]`` in project order
    means the first configured project with ``cap`` candidates is the only one ever heard
    from. Recency is the right tiebreak: a brand-new error is the one worth a worker now.
    """
    times = [item["first_seen"] for item in group if item["first_seen"] is not None]
    if not times:
        return (1, 0.0, group[0]["index"])
    return (0, -max(times), group[0]["index"])


def _observe(config: dict, context: dict | None = None) -> tuple[list[dict], list[dict], dict]:
    """Propose work for new incidents, bounded, filtered, and routable only."""
    settled = _settled(context)
    entries, problems = _projects(config)
    issue_repo = str(config.get("issue_repo") or "")
    cap = int(config.get("max_proposals", DEFAULT_MAX_PROPOSALS) or DEFAULT_MAX_PROPOSALS)
    candidates: list[dict] = []
    seen = 0
    denied = 0
    deny_signatures = _deny_signatures(config)
    unreadable: list[str] = []
    suppressed = [entry["id"] for entry in entries if entry["suppressed"]]

    for entry in entries:
        if entry["suppressed"]:
            # Not fetched at all: a suppressed project should cost nothing, and asking Sentry
            # for issues only to throw them away is the expensive way to say "no".
            continue
        project, repo = entry["id"], entry["repo"]
        issues = _issues(config, project, entry["query"])
        if issues is None:
            unreadable.append(project)
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            seen += 1
            short_id = str(issue.get("shortId") or issue.get("id") or "")
            if not short_id:
                continue
            title = str(issue.get("title") or "").strip() or short_id
            if any(signature in title.casefold() for signature in deny_signatures):
                denied += 1
                continue
            candidates.append(
                {
                    "index": len(candidates),
                    "repo": repo,
                    "target": issue_repo or str(repo),
                    "short_id": short_id,
                    "title": title,
                    "normalized_title": _normalized_title(title),
                    "first_seen": _first_seen(issue),
                    "url": str(issue.get("permalink") or ""),
                }
            )

    groups = _burst_groups(candidates, config)
    # Grouping runs over every candidate, settled or not, so an incident keeps the same leader
    # and the same key from wake to wake; only then is a whole already-decided incident dropped.
    fresh = [
        group for group in groups if not any(item["short_id"] in settled for item in group)
    ]
    fresh.sort(key=_newest_first)
    requests: list[dict] = []
    for group in fresh[:cap]:
        first = group[0]
        short_id = first["short_id"]
        title = first["title"]
        grouped = len(group)
        grouped_note = ""
        if grouped > 1:
            ids = ", ".join(item["short_id"] for item in group)
            grouped_note = f" ({grouped} grouped Sentry issues: {ids})"
        requests.append(
            {
                "kind": "open-work",
                "payload": {
                    "repo": first["target"],
                    "key": short_id,
                    "title": (
                        f"{short_id}: {title}"
                        + (f" ({grouped} Sentry issues)" if grouped > 1 else "")
                    ),
                    "instruction": (
                        f"Triage and fix Sentry {short_id}: {title}{grouped_note} "
                        f"({first['url']})"
                    ),
                    "url": first["url"],
                },
            }
        )
    health = {
        "state": "degraded" if unreadable else "ok",
        "projects": len(entries),
        "issues_seen": seen,
        "denied": denied,
        "collapsed": len(candidates) - len(groups),
        "settled": len(groups) - len(fresh),
        "waiting": max(len(fresh) - len(requests), 0),
        "proposed": len(requests),
    }
    if suppressed:
        # Named, not merely counted: a project silently producing nothing looks identical to
        # one nobody configured, and that is the failure this should never be mistaken for.
        health["suppressed"] = suppressed
    if unreadable:
        health["unreadable"] = unreadable
    return requests, problems, health


HOOKS = ['health', 'observe', 'propose', 'validate-config']


def main(argv: list[str]) -> int:
    hook = argv[1] if len(argv) > 1 else ""
    payload = _read_payload()
    config = payload.get("config") or {}

    if hook == "hooks":
        # The engine asks rather than assuming: calling a hook that is not implemented would make
        # "unimplemented" indistinguishable from "broken".
        return _answer(hooks=HOOKS)
    if hook == "validate-config":
        return _answer(problems=_validate(config))
    if hook == "observe":
        requests, problems, health = _observe(config, payload.get("context") or {})
        return _answer(requests=requests, problems=problems, health=health)
    if hook == "propose":
        reference = str((payload.get("context") or {}).get("reference") or "").strip()
        issue = _issue(config, reference)
        if issue is None:
            return _answer(
                problems=[
                    _problem(
                        "reference",
                        f"could not fetch {reference!r}",
                        "check the Sentry reference and token",
                    )
                ]
            )
        entries = _projects(config)[0]
        request = _manual_request(config, issue, entries)
        if request is None:
            project = str((issue.get("project") or {}).get("slug") or "")
            return _answer(
                problems=[
                    _problem(
                        "reference",
                        f"Sentry project {project!r} is not mapped",
                        "map the project before proposing this issue",
                    )
                ]
            )
        return _answer(requests=[request])
    if hook == "health":
        entries = _projects(config)[0]
        return _answer(
            health={
                "state": "ok" if _token(config) else "degraded",
                "org": config.get("org"),
                "projects": len(entries),
                "suppressed": [entry["id"] for entry in entries if entry["suppressed"]],
                "token": bool(_token(config)),
            }
        )
    print(f"sentry-autofix does not implement {hook!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
