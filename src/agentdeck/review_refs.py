"""Review references and diff reads every edition of the deck uses (#3623).

The names a review is about (``owner/repo#n``), a chat title's first line, a pull request's
facts and the digest of its diff at a head are plain GitHub reads and text rules. The
release needs them to record a person's Merge (``work_release``), new issues need the
title rule, and the Work page reads the references. They lived in ``chat_review``, which
the public copy replaces with a stand-in, so they live here and ``chat_review`` re-exports
them.
"""

from __future__ import annotations

import json
import logging
import re

from . import gh_cli

log = logging.getLogger(__name__)


def reviewer_available() -> bool:
    """Whether this edition of the deck has a reviewer at all.

    The public copy has none: ``chat_review`` is its stand-in there (``PUBLIC_STUB``), so a
    person's Merge press stands for the review pass, and no review is offered (#3623).
    """
    from . import chat_review

    return not getattr(chat_review, "PUBLIC_STUB", False)


# What a review issue is called. Also how a later call recognises one it already opened, so
# it is a fixed sentence naming the exact target rather than anything time-varying.
TITLE = "Review {repository}#{issue}"


DIGEST_TOO_LARGE = "too_large"


DIGEST_UNREADABLE = "unreadable"


# `owner/repo#123`, or the URL of that issue or its pull request. A person pasting from the
# address bar and the poller passing a card reference are the same call.
_TARGET_RE = re.compile(
    r"^(?P<repository>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)"
    r"(?:#|/(?:issues|pull)/)(?P<issue>\d+)/?$"
)


# `TITLE` read backwards: which target a chat on the board is about. A round-trip test pins
# this to `TITLE` itself, because the two have to agree and only one of them is a sentence.
_TITLE_RE = re.compile(r"^Review (?P<repository>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(?P<issue>\d+)$")


_TIMEOUT_S = 20.0


class ReviewError(Exception):
    """The review could not be requested, with a sentence an operator can act on."""


class ReviewBusy(ReviewError):
    """Somebody else holds this review chat and would not let go. Ask again later.

    Deliberately not the same answer as a refusal. A refusal is the deck saying no — the target
    is malformed, there is no chat board here — and #1824 made that terminal for the poller, so
    a head refused is a head not asked about again. A lock that could not be taken is the
    opposite: nothing is wrong with the question, and the next wake is exactly when to put it
    again. So the route answers this 503 rather than 400, which is what keeps the poller coming
    back (`chat-review` marks 4xx refused and 5xx retryable).
    """


def normalise(target: str) -> tuple[str, int]:
    """`owner/repo` and an issue number, or a refusal naming what was given.

    Checked before anything is written, because every later step names this target in text a
    worker will act on: an issue titled `Review ../..#0` is work somebody has to undo.

    A pull request URL is accepted and read as its number. GitHub numbers issues and pull
    requests in one sequence, and the reviewer resolves one from the other anyway, so
    refusing the link somebody actually had open would be pedantry.
    """
    value = (target or "").strip().removeprefix("https://github.com/").strip("/")
    match = _TARGET_RE.match(value)
    if not match:
        raise ReviewError(f"{target!r} is not an owner/repo#number reference")
    repository = match.group("repository")
    # A dot is legal in a repository name, so the pattern alone lets `../..` through — and
    # that is the one shape worth naming, because it looks like a path rather than a repo.
    if any(not part.strip(".") for part in repository.split("/")):
        raise ReviewError(f"{target!r} is not an owner/repo#number reference")
    issue = int(match.group("issue"))
    if issue <= 0:
        raise ReviewError(f"{target!r} does not name an issue")
    return repository, issue


def review_target(title: str) -> tuple[str, int] | None:
    """Which `owner/repo#N` a chat title is a review of, or None if it is not one (#1881).

    The exact inverse of `TITLE`, and deliberately strict: a chat somebody typed into the
    New chat box carries whatever words they used, and reading one of those as a review of
    the card it happens to mention would put a chip on a session about nothing.
    """
    match = _TITLE_RE.match((title or "").strip())
    if match is None:
        return None
    return match.group("repository"), int(match.group("issue"))


async def target_head(repository: str, number: int) -> str:
    """The commit the target's pull request points at right now, or an empty string.

    Read once, when a review chat is being written, because that is the head the chat is
    about: the poller opens a chat per ready head, and a later push gets a later chat.

    Empty is an ordinary answer and never an error. The target may be an issue with no
    pull request yet, and a review is worth opening either way — a chip that cannot say
    which commit was reviewed says less, while a refused review says nothing at all.
    """
    try:
        code, stdout = await gh_cli.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repository,
                "--json",
                "headRefOid",
            ],
            timeout=_TIMEOUT_S,
        )
    except gh_cli.GitHubRefusal as exc:
        log.warning("chat review: %s#%s head unreadable: %s", repository, number, exc)
        return ""
    if code != 0:
        log.info("chat review: %s#%s has no pull request head to record", repository, number)
        return ""
    try:
        value = json.loads(stdout or b"null")
    except ValueError:
        return ""
    head = value.get("headRefOid") if isinstance(value, dict) else None
    return head if isinstance(head, str) else ""


async def target_digest(repository: str, number: int, head: str) -> tuple[str, str]:
    """The review digest of the pull request's diff at ``head`` and, without one, why (#2272).

    The diff endpoint names no commit, so the head is read again afterwards and the digest kept
    only when it did not move: a push between the two reads would pair this head with the next
    commit's diff, and a verdict stating that pair would pass content nobody reviewed. The
    engine re-hashes at intake and refuses a mismatch either way; this keeps the chat from
    carrying a pair that could only ever be refused.

    Empty is fail-closed, never a guess: no head, an unreadable or oversized diff, a binary
    section the digest cannot identify, or a head that moved. A reviewer handed no digest
    posts no verdict, so nothing merges on it.

    The second value is why there is no digest (#2478): `DIGEST_TOO_LARGE` when GitHub refused a
    diff over its size limit, `DIGEST_UNREADABLE` for anything else, and "" with a digest or
    with no head to read one at.
    """
    if not head:
        return "", ""
    # Imported here: the engine's GitHub adapter is a large module this one otherwise never needs.
    from .poller_engine.github import review_diff_digest, review_diff_too_large

    try:
        # `run_once` rather than `run` only for its stderr, which is where GitHub says the diff
        # is too large. Neither retries, so a read through it is still exactly one read.
        code, stdout, stderr = await gh_cli.run_once(
            ["gh", "pr", "diff", str(number), "--repo", repository], timeout=_TIMEOUT_S
        )
    except (OSError, TimeoutError):
        # A digest the chat lacks costs one verdict; a review refused over it costs the review.
        code, stdout, stderr = 1, b"", b""
    if code != 0:
        if review_diff_too_large((stderr or b"").decode("utf-8", "replace")):
            log.info("chat review: %s#%s diff too large; no review digest", repository, number)
            return "", DIGEST_TOO_LARGE
        log.info("chat review: %s#%s diff unreadable; no review digest", repository, number)
        return "", DIGEST_UNREADABLE
    try:
        text = (stdout or b"").decode("utf-8")
    except UnicodeDecodeError:
        return "", DIGEST_UNREADABLE
    # Read the way the engine's own `gh` read reads it — a text-mode subprocess, whose universal
    # newlines turn `\r\n` and `\r` into `\n` — or a file with CRLF endings digests differently
    # here and every verdict about it is refused.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    digest = review_diff_digest(text)
    if not digest:
        return "", DIGEST_UNREADABLE
    try:
        again = await target_head(repository, number)
    except (OSError, TimeoutError):
        again = ""
    if again.lower() != head.lower():
        log.info("chat review: %s#%s moved while its diff was read; no digest", repository, number)
        return "", DIGEST_UNREADABLE
    return digest, ""


async def _read_json(argv: list[str], what: str, *, owner: str | None = None) -> dict | None:
    """One `gh` read answering a JSON object, or None however it failed.

    None is an ordinary answer: a number that is not a pull request makes `gh pr view` fail,
    and a review is worth opening whatever could not be read about its neighbours.
    """
    try:
        # Only a read that names no repository in its argv says whose it is.
        extra = {"owner": owner} if owner else {}
        code, stdout = await gh_cli.run(argv, timeout=_TIMEOUT_S, **extra)
    except gh_cli.GitHubRefusal as exc:
        log.warning("chat review: %s unreadable: %s", what, exc)
        return None
    if code != 0:
        return None
    try:
        value = json.loads(stdout or b"null")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def pull_facts(repository: str, number: int) -> dict | None:
    """A pull request's state, draft flag, head and the issues it closes; None if it is none."""
    return await _read_json(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repository,
            "--json",
            "number,state,isDraft,headRefOid,closingIssuesReferences",
        ],
        f"{repository}#{number}",
    )


# Long enough for a real sentence, short enough that the board's rows stay one line on a
# phone. A first line past this is cut at a word rather than mid-word.
_TITLE_LIMIT = 72


def first_line_title(body: str) -> str:
    """The chat's name, taken from the first line of what somebody typed (#1827).

    Asking for a title and then for the request itself is asking for the same thing twice,
    and the answer was "I don't want to give a title". The first line of a request is the
    request in short — so that is the name, and the whole text is still the body.
    """
    for line in (body or "").splitlines():
        # Markdown headings and list bullets are how people start a request; they read as
        # noise in an issue title, so the words are kept and the punctuation is not.
        subject = line.strip().lstrip("#*-•").strip()
        if not subject:
            continue
        if len(subject) <= _TITLE_LIMIT:
            return subject
        # One character past the limit, so a line that ends exactly on it keeps its last
        # word. A first line with no space in it at all — a pasted URL, say — has no word
        # boundary to cut at, so it is cut at the limit itself.
        head = subject[: _TITLE_LIMIT + 1]
        cut = head.rsplit(" ", 1)[0].rstrip(",;:.") if " " in head else ""
        return f"{cut or subject[:_TITLE_LIMIT]}…"
    return ""
