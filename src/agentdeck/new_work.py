"""Start a new Work from the deck: file its issue at once (#2415).

A person types a rough idea — on the front page, or from a chat they are watching — and the
deck files it as an issue in a Poller-managed repository, labelled so the Poller picks it up.
There is no drafting agent and no approval step: when the card is spec-only, the worker the
Poller starts writes the spec and asks its questions on the card.

Attachments never reach GitHub (#4135). They are saved on this deck's disk before the issue
exists, under ``FILES_ROOT``, and the issue carries only each file's name, deck link and path.
The issue is what every start of a Work reads, so an early start, a usual start and a Poller
worker all find the files with no code of their own. Files older than ``FILES_TTL_S`` are
swept away, so an old issue's path can be gone.

The issue is created with one REST request (#3590), not ``gh issue create``, which first looks
the repository and its labels up. ``file`` hands the body to ``on_body`` just before that
request, so a caller can start the Work's agent while GitHub creates the issue.

Every GitHub call is a write through ``gh_cli.run_once``, as the repository owner's
non-human authority (#2289). Nothing here is retried: the route reserves the press's client
action id first, and a create that ended without proof either way is left for the person to
check rather than made again.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from starlette.datastructures import FormData, UploadFile

from . import gh_cli
from .public_origin import public_url
from .web.uploads import _safe_file_name

CREATE_TIMEOUT_S = 30.0

# Where attachments are kept, one folder per press: a Work outlives the turn-long upload spool.
FILES_ROOT = Path.home() / ".local/share/agentdeck/new-work-files"
FILES_TTL_S = 90 * 24 * 60 * 60

# GitHub answers these before anything is created: a label or repository it does not know, a
# permission it refuses, or a request it rejects outright. Anything else from a non-zero exit
# may have created the issue.
_DEFINITE_REFUSAL = re.compile(
    r"could not add label|Could not resolve to a Repository|"
    r"does not have the correct permissions|HTTP 40[134]\b|HTTP 422\b",
    re.IGNORECASE,
)


class Refused(ValueError):
    """The request cannot be filed as sent; nothing was written."""


@dataclass(frozen=True)
class Target:
    """One repository a Work can be filed in, with what filing there needs."""

    repository: str
    project: str
    ownership_label: str
    spec_only_label: str = ""
    # The repository the form lands on when only the project is known (#3882).
    project_default: bool = False


@dataclass(frozen=True)
class Attachment:
    name: str
    data: bytes


@dataclass(frozen=True)
class Kept:
    """An attachment saved on this deck, by its absolute path."""

    name: str
    path: str


@dataclass(frozen=True)
class Outcome:
    """``done``, ``failed`` (nothing was created) or ``uncertain`` (it may have been)."""

    state: str
    text: str
    url: str = ""


def targets(board) -> tuple[Target, ...]:
    """Every issue repository a Poller manifest on this host declares, sorted, deduplicated."""
    found: dict[str, Target] = {}
    for scope in getattr(board, "scopes", ()) or ():
        labels = scope.ownership_labels or (
            (scope.ownership_label,) if scope.ownership_label else ()
        )
        if not labels:
            continue
        home = _project_home(scope)
        for repository in scope.issue_repositories:
            found.setdefault(
                repository.lower(),
                Target(
                    repository=repository,
                    project=scope.instance,
                    # The first label is the canonical one for creating an issue.
                    ownership_label=labels[0],
                    spec_only_label=scope.spec_only_label,
                    project_default=repository.lower() == home,
                ),
            )
    return tuple(found[key] for key in sorted(found))


def _project_home(scope) -> str:
    """The issue repository a project's new Work goes to by default, lowercased (#3882).

    A project's own issue tracker — an issue repository that holds no code and borrows
    another's checkout, such as an ``issues`` tracker — else the first in sorted order. The
    sorted first alone put such a project's Works in a code repository like ``docker``.
    """
    repositories = sorted(item.lower() for item in scope.issue_repositories)
    borrowing = {
        str(repository).lower()
        for repository, _code in getattr(scope, "borrowed_workspaces", ()) or ()
    }
    trackers = [item for item in repositories if item in borrowing]
    return (trackers or repositories or [""])[0]


def target_for(options: Iterable[Target], repository: object) -> Target | None:
    wanted = str(repository or "").strip().lower()
    return next((item for item in options if item.repository.lower() == wanted), None)


def default_repository(options: Iterable[Target], issues: Iterable[object] = ()) -> str:
    """The first of a chat's registered issues whose repository can take a new Work."""
    options = tuple(options)
    for issue in issues:
        target = target_for(options, getattr(issue, "repository", ""))
        if target is not None:
            return target.repository
    return ""


def unique_names(names: Iterable[str]) -> list[str]:
    """Each name once: a second ``image.png`` becomes ``image-2.png``."""
    taken: set[str] = set()
    result: list[str] = []
    for name in names:
        stem, dot, suffix = name.rpartition(".")
        if not dot or not stem:
            stem, suffix = name, ""
        candidate, count = name, 1
        while candidate.lower() in taken:
            count += 1
            candidate = f"{stem}-{count}.{suffix}" if suffix else f"{stem}-{count}"
        taken.add(candidate.lower())
        result.append(candidate)
    return result


async def read_attachments(
    form: FormData, *, max_files: int, max_bytes: int, max_total: int
) -> list[Attachment]:
    """The posted files, in order, under the composer's own limits. Kept in memory."""
    parts = [(name, item) for name, item in form.multi_items() if isinstance(item, UploadFile)]
    try:
        if any(name != "images" for name, _item in parts):
            raise Refused("unexpected file field")
        chosen = [item for _name, item in parts if item.filename]
        if len(chosen) > max_files:
            raise Refused(f"attach at most {max_files} files")
        names: list[str] = []
        blobs: list[bytes] = []
        total = 0
        for item in chosen:
            data = await item.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise Refused(f"{item.filename} is larger than {max_bytes // (1024 * 1024)} MB")
            total += len(data)
            if total > max_total:
                raise Refused(f"files are larger than {max_total // (1024 * 1024)} MB in total")
            # Markdown link text and an inline code span are where the name is written, so
            # the characters that would end either are replaced.
            names.append(re.sub(r"[\[\]`]", "_", _safe_file_name(item.filename or "attachment")))
            blobs.append(data)
    finally:
        for _name, item in parts:
            await item.close()
    return [Attachment(name, data) for name, data in zip(unique_names(names), blobs, strict=True)]


def _gh() -> str:
    return shutil.which("gh") or "gh"


class _NotKept(Exception):
    pass


def _folder(target: Target, press: str) -> Path:
    return FILES_ROOT / target.repository / press


def _keep(target: Target, press: str, attachments: tuple[Attachment, ...]) -> tuple[Kept, ...]:
    """Save a press's files, or none of them: a Work missing a file it was given is worse."""
    folder = _folder(target, press)
    kept: list[Kept] = []
    attachment = attachments[0]
    try:
        FILES_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(FILES_ROOT, 0o700)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for attachment in attachments:
            path = folder / attachment.name
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
            with os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
                handle.write(attachment.data)
            kept.append(Kept(attachment.name, str(path)))
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise _NotKept(f"{attachment.name} could not be kept: {exc.strerror or exc}") from exc
    return tuple(kept)


def sweep_old_files(now: float | None = None) -> None:
    """Remove the press folders older than ``FILES_TTL_S``."""
    cutoff = (time.time() if now is None else now) - FILES_TTL_S
    try:
        presses = tuple(FILES_ROOT.glob("*/*/*"))
    except OSError:
        return
    for press in presses:
        try:
            if press.is_symlink() or not press.is_dir() or press.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(press)
        except OSError:
            continue


# GitHub refuses an issue body over 65,536 characters; the rest is left for what
# compose_body adds (the chat link and the attachment lines).
GITHUB_TEXT_MAX_CHARS = 60_000


def max_text_chars(configured: int | None) -> int:
    """The longest Work text the form takes: the deck's own limit, if any, under GitHub's."""
    if configured is None:
        return GITHUB_TEXT_MAX_CHARS
    return min(configured, GITHUB_TEXT_MAX_CHARS)


def compose_body(text: str, *, kept: Iterable[Kept] = (), chat_url: str = "") -> str:
    parts = [text.strip()]
    if chat_url:
        parts.append(f"Started from [this chat]({chat_url})")
    kept = tuple(kept)
    if kept:
        lines = []
        for item in kept:
            # No picture: GitHub cannot fetch from the deck's private address.
            link = public_url(quote(item.path, safe="/"))
            lines.append(f"- [{item.name}]({link}) `{item.path}`" if link else f"- `{item.path}`")
        parts.append(
            "### Attachments\n\n"
            "Kept on the deck, not on GitHub. Read them from these paths on the deck's host:\n\n"
            + "\n".join(lines)
        )
    return "\n\n".join(parts) + "\n"


def labels_for(target: Target, spec_only: bool) -> list[str]:
    """The labels a New work is filed with: the ownership label, and spec-only if asked."""
    labels = [target.ownership_label]
    if spec_only and target.spec_only_label:
        labels.append(target.spec_only_label)
    return labels


async def _create(
    target: Target, title: str, body: str, spec_only: bool, assignee: str = ""
) -> Outcome:
    argv = [_gh(), "api", "-X", "POST", f"repos/{target.repository}/issues", "--input", "-"]
    payload: dict = {"title": title, "body": body, "labels": labels_for(target, spec_only)}
    if assignee:
        # Filed already this deck's, where another deck takes unassigned issues (#3731).
        payload["assignees"] = [assignee]
    try:
        code, stdout, stderr = await gh_cli.run_once(
            argv,
            timeout=CREATE_TIMEOUT_S,
            stdin=json.dumps(payload).encode("utf-8"),
            repo=target.repository,
        )
    except gh_cli.GitHubRefusal as exc:
        # Refused before anything ran, so filing again after fixing it is safe.
        return Outcome("failed", str(exc))
    except (OSError, TimeoutError) as exc:
        return Outcome("uncertain", f"GitHub did not answer ({exc or 'timed out'})")
    message = stderr.decode("utf-8", "replace").strip()
    if code != 0:
        if _DEFINITE_REFUSAL.search(message):
            return Outcome("failed", message.splitlines()[-1])
        return Outcome("uncertain", message.splitlines()[-1] if message else f"gh exited {code}")
    try:
        answer = json.loads(stdout or b"{}")
    except ValueError:
        answer = {}
    found = str(answer.get("html_url") or "") if isinstance(answer, dict) else ""
    if not re.fullmatch(r"https://github\.com/[^/]+/[^/]+/issues/\d+", found):
        return Outcome("uncertain", "GitHub answered without the new issue's address")
    number = found.rsplit("/", 1)[-1]
    return Outcome("done", f"Filed {target.repository}#{number}", found)


async def file(
    target: Target,
    *,
    title: str,
    text: str,
    spec_only: bool,
    folder: str,
    attachments: Iterable[Attachment] = (),
    chat_url: str = "",
    on_body: Callable[[str], object] | None = None,
    assignee: str = "",
) -> Outcome:
    """Keep the attachments, then create the issue. Called once per reserved press.

    ``on_body`` gets the issue's body just before the create is sent, nothing on a failed
    save: the caller starts the Work's agent then rather than after GitHub answers.
    ``assignee`` is filed as the issue's assignee (``work_claims.own_assignee``).
    """
    attachments = tuple(attachments)
    kept: tuple[Kept, ...] = ()
    if attachments:
        try:
            kept = await asyncio.to_thread(_keep, target, folder, attachments)
        except _NotKept as exc:
            return Outcome("failed", str(exc))
    body = compose_body(text, kept=kept, chat_url=chat_url)
    if on_body is not None:
        on_body(body)
    outcome = await _create(target, title, body, spec_only, assignee)
    if kept and outcome.state == "failed":
        # Nothing was filed. An uncertain create keeps its files: the issue may exist.
        await asyncio.to_thread(shutil.rmtree, _folder(target, folder), True)
    return outcome
