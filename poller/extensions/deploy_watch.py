#!/usr/bin/env python3
"""deploy-watch: report whether a merge actually reached this host (#179, a #113 reference).

A merged pull request is not a deployed one. This extension answers the guarded release's
deploy gate with evidence rather than optimism: for each watched repository it checks that the
merge is live and that the service's health endpoint answers healthy. "Live" is the deployed
commit containing the merge commit when the config names where the deploy records that commit
(`deployed_commit_file`, plus the `repo_path` checkout to ask), and otherwise the service having
restarted *after* the release started waiting. Anything less is `pending`, so the release keeps
waiting instead of finishing on a deploy nobody observed, and a hard failure is `bad`, which
blocks the card for a human — a broken production deploy is not something to retry silently.

Hooks: validate-config, release-gate, health. Stdlib only, so it runs anywhere the engine does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

DEFAULT_GRACE_S = 900
_TIMEOUT_S = 5


def _read_payload() -> dict:
    try:
        value = json.load(sys.stdin)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _answer(**parts) -> int:
    print(json.dumps(parts))
    return 0


# --- validate-config ---

def _validate(config: dict) -> list[dict]:
    problems = []
    if not str(config.get("service") or "").endswith(".service"):
        problems.append(
            {
                "location": "manifest.extensions.deploy-watch.service",
                "what": "missing or not a systemd service name",
                "fix": 'set service = "agentdeck.service"',
            }
        )
    url = str(config.get("health_url") or "")
    if not url.startswith(("http://", "https://")):
        problems.append(
            {
                "location": "manifest.extensions.deploy-watch.health_url",
                "what": "missing or not an http(s) URL",
                "fix": 'set health_url to the service\'s health endpoint',
            }
        )
    grace = config.get("grace_seconds", DEFAULT_GRACE_S)
    if not isinstance(grace, int) or isinstance(grace, bool) or grace < 1:
        problems.append(
            {
                "location": "manifest.extensions.deploy-watch.grace_seconds",
                "what": "must be a positive number of seconds",
                "fix": "set how long a deploy may take before it counts as failed",
            }
        )
    if config.get("deployed_commit_file") and not config.get("repo_path"):
        problems.append(
            {
                "location": "manifest.extensions.deploy-watch.repo_path",
                "what": "deployed_commit_file needs a checkout to compare commits in",
                "fix": 'set repo_path = "~/agentdeck", the checkout the deploy updates',
            }
        )
    return problems


# --- observing the host ---

def _service_restarted_at(config: dict) -> datetime | None:
    """When the service last became active, or None when that cannot be determined."""
    command = config.get("state_command") or [
        "systemctl",
        "--user" if config.get("user_scope", True) else "--system",
        "show",
        "-p",
        "ActiveEnterTimestamp",
        "--value",
        str(config.get("service") or ""),
    ]
    try:
        done = subprocess.run(
            list(command), capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return _instant((done.stdout or "").strip())


_UTC_NAMES = frozenset({"UTC", "GMT", "UCT", "Z", "ZULU"})


def _instant(stamp: str) -> datetime | None:
    """One systemd timestamp as the absolute moment it names, or None if unreadable.

    `systemctl show -p ActiveEnterTimestamp` prints *local* time with a zone abbreviation
    (`Sun 2026-07-26 22:33:23 EEST`), and Python's `%Z` consumes an abbreviation without
    applying it — `strptime` hands back a naive datetime. Reading that as UTC moved every
    restart by the host's offset: three hours into the future on this one, which made
    `restarted < since` impossible and the gate answer `ok` for a deploy that had not
    happened (#324). Only an explicit UTC name or a real offset is absolute; anything else
    is the host's own clock, which is this process's clock too.
    """
    text = stamp.strip()
    if not text:
        return None
    zone = ""
    head, _, tail = text.rpartition(" ")
    if head and tail.isalpha():
        text, zone = head.strip(), tail.upper()
    for pattern in ("%a %Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if parsed.tzinfo:
            return parsed
        if zone in _UTC_NAMES:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone()  # naive means local, and local is where this runs
    return None


def _deployed_commit(config: dict) -> str | None:
    """The commit the deploy last recorded as live, or None when it has recorded none."""
    path = os.path.expanduser(str(config.get("deployed_commit_file") or ""))
    try:
        with open(path, encoding="utf-8") as handle:
            sha = handle.read().strip()
    except OSError:
        return None
    return sha if _is_sha(sha) else None


def _is_sha(text: str) -> bool:
    """A hex commit id — also what keeps a value starting with `-` from reaching git as a flag."""
    return bool(text) and all(ch in "0123456789abcdef" for ch in text.lower())


def _contains(config: dict, deployed: str, merged: str) -> bool | None:
    """Is ``merged`` the deployed commit or one of its ancestors? None when git cannot say."""
    repo = os.path.expanduser(str(config.get("repo_path") or ""))
    try:
        done = subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", merged, deployed],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode in (0, 1):
        return done.returncode == 0
    return None  # an unknown commit (not fetched yet) is no evidence either way


def _healthy(config: dict) -> bool | None:
    """True/False from the health endpoint, or None when it could not be reached."""
    url = str(config.get("health_url") or "")
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as response:  # noqa: S310
            body = json.loads(response.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    return str(body.get("status") or "").lower() in {"ok", "healthy", "pass"}


def _since(context: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(context.get("since") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _gate(config: dict, context: dict) -> list[dict]:
    """One release-status request per watched repository, with evidence or `pending`.

    A web deploy on this host hot-reloads its workers rather than restarting the service
    (`scripts/deploy-live.sh`), so `ActiveEnterTimestamp` stopped moving on deploy: every
    release waited out its whole grace window in silence, reported a healthy deploy as failed,
    and only then finished through the recheck (#2308). The deployed commit is the evidence that
    survives how the deploy is done, so it is preferred whenever the config names it and the
    build carries its merge commit; the restart stamp stays the fallback for everything else.
    """
    builds = context.get("builds") or {}
    if not builds:
        return []
    healthy = _healthy(config)
    since = _since(context)
    grace = int(config.get("grace_seconds", DEFAULT_GRACE_S) or DEFAULT_GRACE_S)
    by_commit = bool(config.get("deployed_commit_file"))
    deployed = _deployed_commit(config) if by_commit else None
    merged = {
        repo: str(build.get("sha") or "").strip() if isinstance(build, dict) else ""
        for repo, build in builds.items()
    }
    restarted = (
        _service_restarted_at(config)
        if not by_commit or not all(merged.values())
        else None
    )

    def status_for(repo: str) -> str:
        if by_commit and merged[repo]:
            live = (
                _contains(config, deployed, merged[repo])
                if deployed and _is_sha(merged[repo])
                else None
            )
            if live is None or healthy is None:
                return "pending"  # no evidence either way: keep waiting, never guess
            if not live:
                return _not_live_yet(since, grace)
            return "ok" if healthy else "bad"
        if restarted is None or healthy is None:
            return "pending"
        if since is not None and restarted < since:
            # The service has not restarted since the release started waiting, so this merge
            # is not live yet — unless it has been waiting longer than a deploy may take.
            return _not_live_yet(since, grace)
        return "ok" if healthy else "bad"

    return [
        {
            "kind": "release-status",
            "payload": {
                "repo": repo,
                "status": status_for(repo),
                "link": str(config.get("health_url") or ""),
            },
        }
        for repo in builds
    ]


def _not_live_yet(since: datetime | None, grace: int) -> str:
    """`pending` inside the grace window, `bad` once the deploy is overdue."""
    if since is None:
        return "pending"
    waited = (datetime.now(UTC) - since).total_seconds()
    return "bad" if waited > grace else "pending"


HOOKS = ['health', 'release-gate', 'validate-config']


def main(argv: list[str]) -> int:
    hook = argv[1] if len(argv) > 1 else ""
    payload = _read_payload()
    config = payload.get("config") or {}
    context = payload.get("context") or {}

    if hook == "hooks":
        # The engine asks rather than assuming: calling a hook that is not implemented would make
        # "unimplemented" indistinguishable from "broken".
        return _answer(hooks=HOOKS)
    if hook == "validate-config":
        return _answer(problems=_validate(config))
    if hook == "release-gate":
        return _answer(requests=_gate(config, context))
    if hook == "health":
        restarted = _service_restarted_at(config)
        healthy = _healthy(config)
        return _answer(
            health={
                "state": "ok" if healthy else "degraded",
                "service": config.get("service"),
                "restarted_at": restarted.isoformat() if restarted else None,
                "healthy": healthy,
            }
        )
    print(f"deploy-watch does not implement {hook!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
