"""What a switched Work shows on its GitHub issue (#3109).

The Poller shows a person it heard them with reactions on their ``/claude`` comment (👀 seen,
🚀 working). A Work the deck starts itself (``work_start``) has no such comment: the ownership
label is the start, so nothing on the issue said an agent was on it. This puts the same signs
where they now belong:

* **🚀 on the issue, and one comment linking the Work page,** once its agent has started.
* **👀 on a person's comment** the router passed on to the Work's chat.

It also reads one thing back: the closer's own comment on a Work's issue just before its close
(#3188).

Best effort: a sign that does not land changes nothing about the Work, so a failure is logged,
never retried. A reaction is idempotent on GitHub, so a replayed delivery adds nothing. The
start comment is posted only from the one settle of the Work's one start, so it is at most once
per start; a process crash between the settle and the post loses it.
"""

from __future__ import annotations

import json
import logging
import shutil

from . import gh_cli

log = logging.getLogger(__name__)

GH_TIMEOUT_S = 15.0
STARTED = "rocket"
SEEN = "eyes"
MARKER = "<!-- agentdeck-work-start id="
#: The line a spec-only Work's agent ends its spec comment with (#3247): the deck reads that
#: comment as the posted spec, waiting on a person's approval, and asks for no next step.
SPEC_MARKER = "<!-- agentdeck-spec -->"


def start_comment(*, action_id: str, work_url: str = "") -> str:
    """The comment that says the Work's agent is on the issue."""
    work = f"[Work page]({work_url})" if work_url else "the deck's Work page"
    return "\n".join(
        [
            f"AgentDeck started an agent on this issue. Follow it on {work}.",
            "",
            f"{MARKER}{action_id} -->",
        ]
    )


async def _gh(repository: str, *args: str, stdin: bytes | None = None) -> bool:
    gh = shutil.which("gh")
    if gh is None:
        log.warning("work signal: gh is not available on this host")
        return False
    try:
        code, _out, err = await gh_cli.run_once(
            [gh, *args], timeout=GH_TIMEOUT_S, stdin=stdin, repo=repository
        )
    except Exception:  # noqa: BLE001 - a sign that did not land changes nothing
        log.warning("work signal: gh %s on %s failed", args[0], repository, exc_info=True)
        return False
    if code != 0:
        log.warning(
            "work signal: gh %s on %s exited %s: %s",
            args[0],
            repository,
            code,
            err.decode(errors="replace").strip()[:300],
        )
        return False
    return True


async def react(repository: str, path: str, content: str) -> bool:
    """Add one reaction to ``repos/<repository>/<path>``; whether GitHub took it."""
    return await _gh(
        repository,
        "api",
        "-X",
        "POST",
        f"repos/{repository}/{path}/reactions",
        "-f",
        f"content={content}",
    )


async def announce_start(
    repository: str, number: int, *, action_id: str, work_url: str = ""
) -> None:
    """🚀 the issue and post the start comment: the Work's agent is on it."""
    await react(repository, f"issues/{number}", STARTED)
    body = start_comment(action_id=action_id, work_url=work_url)
    await _gh(
        repository,
        "issue",
        "comment",
        str(number),
        "--repo",
        repository,
        "--body-file",
        "-",
        stdin=body.encode(),
    )


async def acknowledge_comment(repository: str, comment_id: int) -> None:
    """👀 a person's comment the router passed on to the Work's chat."""
    await react(repository, f"issues/comments/{comment_id}", SEEN)


async def latest_comment(repository: str, number: int, since: str, login: str) -> dict | None:
    """``login``'s newest comment on the issue at or after ``since``; ``None`` if none or unread."""
    gh = shutil.which("gh")
    if gh is None:
        return None
    path = f"repos/{repository}/issues/{number}/comments?since={since}&per_page=100"
    try:
        code, out, _err = await gh_cli.run_once(
            [gh, "api", path], timeout=GH_TIMEOUT_S, repo=repository
        )
        comments = json.loads(out) if code == 0 else None
    except Exception:  # noqa: BLE001 - the close is still told, without the quote
        log.warning("work signal: comments of %s#%s unread", repository, number, exc_info=True)
        return None
    if not isinstance(comments, list):
        return None
    wanted = login.casefold()
    found = [
        item
        for item in comments
        if isinstance(item, dict)
        and isinstance(item.get("user"), dict)
        and str(item["user"].get("login") or "").casefold() == wanted
    ]
    return max(found, key=lambda item: str(item.get("created_at") or ""), default=None)


async def read_map(repository: str, number: int) -> dict | None:
    """A parent issue's state, label names and sub-issue counts, its sub-issues' own
    ``repository``/``number``/``state``/``closed_at`` (``sub_issues``), and when the last of
    them closed (``last_closed``), read live; ``None`` if unread.

    The Work router reads a wayfinder map with it after one of its tickets closes (#3627).
    Two reads: the issue, and its sub-issues. Right after a close GitHub can still hand back
    the old ``sub_issues_summary``, so the router trusts each sub-issue's state (#3671)."""
    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        code, out, _err = await gh_cli.run_once(
            [
                gh,
                "api",
                f"repos/{repository}/issues/{number}",
                "--jq",
                "{state, labels: [.labels[]?.name], sub_issues_summary}",
            ],
            timeout=GH_TIMEOUT_S,
            repo=repository,
        )
        found = json.loads(out) if code == 0 else None
        if not isinstance(found, dict):
            log.warning("work signal: map %s#%s unread (gh exit %s)", repository, number, code)
            return None
        code, out, _err = await gh_cli.run_once(
            [
                gh,
                "api",
                f"repos/{repository}/issues/{number}/sub_issues?per_page=100",
                "--jq",
                # An object, since ``gh --jq`` prints a bare string unquoted.
                "{last: ([.[].closed_at | select(. != null)] | max),"
                " sub_issues: [.[] | {number, state, closed_at,"
                ' repository: (.repository_url | split("/") | .[-2:] | join("/"))}]}',
            ],
            timeout=GH_TIMEOUT_S,
            repo=repository,
        )
        held = json.loads(out) if code == 0 else None
        if not isinstance(held, dict):
            # The counts alone still decide; a lagging one then waits for the next close.
            log.warning(
                "work signal: sub-issues of %s#%s unread (gh exit %s)", repository, number, code
            )
            held = {}
        last = held.get("last")
        subs = held.get("sub_issues")
    except Exception:  # noqa: BLE001 - the map's chat is not woken this time
        log.warning("work signal: map %s#%s unread", repository, number, exc_info=True)
        return None
    return {
        **found,
        "last_closed": last if isinstance(last, str) else "",
        "sub_issues": subs if isinstance(subs, list) else None,
    }


async def read_parent(repository: str, number: int) -> tuple[str, int] | None:
    """The parent issue of a sub-issue, ``(owner/repo, number)`` lowercased, or ``None`` for
    none or unread.

    GitHub leaves ``parent_issue_url`` out of an issue's own payload, so the Work router asks
    this endpoint when an issue closes, to find a wayfinder map whose ticket it is (#3627).
    An issue with no parent answers 404, which reads as ``None``."""
    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        code, out, _err = await gh_cli.run_once(
            [
                gh,
                "api",
                f"repos/{repository}/issues/{number}/parent",
                "--jq",
                '{repository: (.repository_url | split("/") | .[-2:] | join("/")), number}',
            ],
            timeout=GH_TIMEOUT_S,
            repo=repository,
        )
        found = json.loads(out) if code == 0 else None
    except Exception:  # noqa: BLE001 - the map's chat is not woken this time
        log.warning("work signal: parent of %s#%s unread", repository, number, exc_info=True)
        return None
    if not isinstance(found, dict):
        return None
    owner_repo, parent = found.get("repository"), found.get("number")
    if not isinstance(owner_repo, str) or "/" not in owner_repo or not isinstance(parent, int):
        return None
    return (owner_repo.lower(), parent)


async def read_dependencies(repository: str, number: int, kind: str) -> list[dict] | None:
    """The issues an issue blocks (``kind`` ``blocking``) or waits on (``blocked_by``), read
    live, each ``{repository, number, state, labels}``; ``None`` if unread.

    The Work router reads them when a wayfinder ticket closes, to start a ticket it blocked
    once that ticket has no open blocker left (#3764). Live, not from the ``blocked-by`` cache:
    the close that asks may not have refreshed it yet."""
    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        code, out, _err = await gh_cli.run_once(
            [
                gh,
                "api",
                f"repos/{repository}/issues/{number}/dependencies/{kind}?per_page=100",
                "--jq",
                '[.[] | {repository: (.repository_url | split("/") | .[-2:] | join("/")),'
                " number, state, labels: [.labels[]?.name]}]",
            ],
            timeout=GH_TIMEOUT_S,
            repo=repository,
        )
        found = json.loads(out) if code == 0 else None
    except Exception:  # noqa: BLE001 - nothing is started this time; Start still can
        log.warning("work signal: %s of %s#%s unread", kind, repository, number, exc_info=True)
        return None
    if not isinstance(found, list):
        log.warning("work signal: %s of %s#%s unread (gh exit %s)", kind, repository, number, code)
        return None
    return [row for row in found if isinstance(row, dict)]


async def read_map_body(repository: str, number: int) -> dict | None:
    """A wayfinder map's ``body``, ``state`` and label names, read live; ``None`` if unread.

    The Work router reads it once before it saves the map's rebuilt lists (#3695)."""
    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        code, out, _err = await gh_cli.run_once(
            [
                gh,
                "api",
                f"repos/{repository}/issues/{number}",
                "--jq",
                "{body, state, labels: [.labels[]?.name]}",
            ],
            timeout=GH_TIMEOUT_S,
            repo=repository,
        )
        found = json.loads(out) if code == 0 else None
    except Exception:  # noqa: BLE001 - the lists wait for the next record
        log.warning("work signal: map %s#%s body unread", repository, number, exc_info=True)
        return None
    if not isinstance(found, dict):
        log.warning("work signal: map %s#%s body unread (gh exit %s)", repository, number, code)
        return None
    if found.get("body") is None:
        found["body"] = ""
    return found


async def write_map_body(repository: str, number: int, body: str) -> bool:
    """Save a wayfinder map's body (#3695). Once: a failed write may still have landed."""
    return await _gh(
        repository,
        "api",
        "-X",
        "PATCH",
        f"repos/{repository}/issues/{number}",
        "--input",
        "-",
        "--silent",
        stdin=json.dumps({"body": body}).encode(),
    )
