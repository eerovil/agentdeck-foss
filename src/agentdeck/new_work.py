"""Start a new Work from the deck: file its issue at once, attachments included (#2415).

A person types a rough idea — on the front page, or from a chat they are watching — and the
deck files it as an issue in a Poller-managed repository, labelled so the Poller picks it up.
There is no drafting agent and no approval step: when the card is spec-only, the worker the
Poller starts writes the spec and asks its questions on the card.

GitHub has no API for attaching a file to an issue, so attachments are committed to the
project's evidence repository (the manifest's ``holds_artifacts`` repository, #1791) through
the contents API, one after another, before the issue exists. The issue then embeds or links
each file pinned to the commit its upload made, and tells the worker how to read it back.

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
import base64
import json
import re
import shlex
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import quote

from starlette.datastructures import FormData, UploadFile

from . import gh_cli
from .images import sniff_suffix
from .web.uploads import _safe_file_name

UPLOAD_TIMEOUT_S = 60.0
CREATE_TIMEOUT_S = 30.0

# A filing with attachments holds one of `gh_cli`'s four spawn threads for up to a minute per
# file. One at a time, so a burst of them cannot starve every other GitHub read on the deck.
_ATTACHMENT_FILING = asyncio.Lock()

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
    evidence_repository: str = ""


@dataclass(frozen=True)
class Attachment:
    name: str
    data: bytes

    @property
    def is_image(self) -> bool:
        return sniff_suffix(self.data) is not None


@dataclass(frozen=True)
class Uploaded:
    name: str
    path: str
    commit: str
    blob: str
    is_image: bool


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
        evidence = scope.artifact_repositories[0] if scope.artifact_repositories else ""
        for repository in scope.issue_repositories:
            found.setdefault(
                repository.lower(),
                Target(
                    repository=repository,
                    project=scope.instance,
                    # The first label is the canonical one for creating an issue.
                    ownership_label=labels[0],
                    spec_only_label=scope.spec_only_label,
                    evidence_repository=evidence,
                ),
            )
    return tuple(found[key] for key in sorted(found))


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


def _encoded(path: str) -> str:
    return quote(path, safe="/")


def _link(evidence: str, commit: str, path: str) -> str:
    return f"https://github.com/{evidence}/blob/{commit}/{_encoded(path)}?raw=1"


class _UploadFailed(Exception):
    pass


async def _upload(target: Target, folder: str, attachment: Attachment) -> Uploaded:
    path = f"{target.repository.split('/', 1)[-1]}/new-work/{folder}/{attachment.name}"
    payload = json.dumps(
        {
            "message": f"New work attachment for {target.repository}: {attachment.name}",
            "content": base64.b64encode(attachment.data).decode("ascii"),
        }
    ).encode("utf-8")
    argv = [
        _gh(),
        "api",
        "-X",
        "PUT",
        f"repos/{target.evidence_repository}/contents/{_encoded(path)}",
        "--input",
        "-",
    ]
    try:
        code, stdout, stderr = await gh_cli.run_once(
            argv, timeout=UPLOAD_TIMEOUT_S, stdin=payload, repo=target.evidence_repository
        )
    except gh_cli.GitHubRefusal as exc:
        raise _UploadFailed(str(exc)) from exc
    except (OSError, TimeoutError) as exc:
        raise _UploadFailed(f"the upload did not finish ({exc or 'timed out'})") from exc
    if code != 0:
        message = stderr.decode("utf-8", "replace").strip() or f"gh exited {code}"
        raise _UploadFailed(message.splitlines()[-1])
    try:
        answer = json.loads(stdout or b"{}")
        commit = str(answer["commit"]["sha"])
        blob = str(answer["content"]["sha"])
    except (ValueError, KeyError, TypeError) as exc:
        raise _UploadFailed("GitHub's answer named no commit") from exc
    return Uploaded(attachment.name, path, commit, blob, attachment.is_image)


# GitHub refuses an issue body over 65,536 characters; the rest is left for what
# compose_body adds (the chat link and the attachment lines).
GITHUB_TEXT_MAX_CHARS = 60_000


def max_text_chars(configured: int | None) -> int:
    """The longest Work text the form takes: the deck's own limit, if any, under GitHub's."""
    if configured is None:
        return GITHUB_TEXT_MAX_CHARS
    return min(configured, GITHUB_TEXT_MAX_CHARS)


def compose_body(
    text: str, *, evidence: str = "", uploaded: Iterable[Uploaded] = (), chat_url: str = ""
) -> str:
    parts = [text.strip()]
    if chat_url:
        parts.append(f"Started from [this chat]({chat_url})")
    uploaded = tuple(uploaded)
    if uploaded:
        shown = [
            f"![{item.name}]({_link(evidence, item.commit, item.path)})"
            if item.is_image
            else f"- [{item.name}]({_link(evidence, item.commit, item.path)})"
            for item in uploaded
        ]
        # Through a worker's `gh`, only text survives: the blob API's base64 JSON does.
        fetch = [
            f"- `gh api repos/{evidence}/git/blobs/{item.blob} --jq .content "
            f"| base64 -d > {shlex.quote(item.name)}`"
            for item in uploaded
        ]
        parts.append(
            "### Attachments\n\n"
            + "\n".join(shown)
            + f"\n\nThe files are in `{evidence}`. To read one:\n\n"
            + "\n".join(fetch)
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
    """Upload the attachments, then create the issue. Called once per reserved press.

    ``on_body`` gets the issue's body just before the create is sent, nothing on a failed
    upload: the caller starts the Work's agent then rather than after GitHub answers.
    ``assignee`` is filed as the issue's assignee (``work_claims.own_assignee``).
    """
    attachments = tuple(attachments)
    if not attachments:
        body = compose_body(text, chat_url=chat_url)
        if on_body is not None:
            on_body(body)
        return await _create(target, title, body, spec_only, assignee)
    async with _ATTACHMENT_FILING:
        uploaded: list[Uploaded] = []
        for attachment in attachments:
            try:
                uploaded.append(await _upload(target, folder, attachment))
            except _UploadFailed as exc:
                # A stray file in the evidence repository is harmless; an issue missing a
                # file the person attached is not. Stop before the issue exists.
                return Outcome("failed", f"{attachment.name} could not be uploaded: {exc}")
        body = compose_body(
            text, evidence=target.evidence_repository, uploaded=uploaded, chat_url=chat_url
        )
        if on_body is not None:
            on_body(body)
        return await _create(target, title, body, spec_only, assignee)
