"""Command intake grammar (#176 slice 2).

Parses one issue-comment body into at most one control command. The grammar is
deliberately strict: a command must start at the first non-whitespace character,
so examples in prose or code never fire, and a comment carrying the engine's own
bot marker can never self-trigger. Malformed near-misses are returned as explicit
``INVALID_*`` kinds so the caller can reject them visibly rather than silently.

Rebuilt fresh from the reference engine's ``_parse_control`` (see #176 plan).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

# Stamped into every comment the engine posts, so its own comments are never read
# as human commands (the PAT author is the same human, so author checks can't tell
# them apart). Configurable per project; this is the reference default.
DEFAULT_BOT_MARKER = "<!-- claude-bot -->"

_CLAUDE_RE = re.compile(r"^\s*/claude(?:\s+(.*?))?\s*$", re.IGNORECASE | re.DOTALL)
_MERGE_RE = re.compile(r"^\s*/merge(?:\s+(.*?))?\s*$", re.IGNORECASE)
_MERGE_REVIEW_RE = re.compile(r"^\s*/merge\s+review(?:\s+(.*?))?\s*$", re.IGNORECASE)
# One named release target: `repo#12`, or `owner/repo#12` for the same thing spelled in full.
# The owner is dropped, because a card's targets all live under the project's own org and the
# rest of the engine keys a pull request as `repo#number`.
_REF_RE = re.compile(r"^(?:[A-Za-z0-9._-]+/)?(?P<repo>[A-Za-z0-9._-]+)#(?P<number>\d+)$")
# An outside reviewer's verdict on one pull request (#2108):
# `/reviewed pass <head> <digest>`, optionally naming the pull request when the card has more
# than one. The digest is what the reviewer read, hashed the way the engine hashes it:
# `github.review_diff_digest` over `gh pr diff <n> -R <owner>/<repo>` — *not* a `sha256sum` of
# those bytes, which #2122 replaced with the contributed lines (#2272). That is the whole
# point of stating it — a head says which commit was reviewed but not what the diff under
# it was, while the digest
# *is* the content, so the engine can confirm it still matches instead of trusting whatever
# GitHub happens to return later. It is not optional-with-a-default, because this is one of
# the three bits that let the poller merge. `changes-needed` needs no digest: it only clears.
# `no-code` (#3322) is a review whose findings need no code change, and states the digest as a
# pass does: on a Work the deck merges, fixing them needs no new review. The Poller's own intake
# (`parse_control`) reads it as `changes-needed`, as it always would have.
_REVIEWED_RE = re.compile(
    r"^\s*/reviewed\s+(?P<verdict>pass|changes-needed|no-code)\s+(?P<head>[0-9a-fA-F]{7,40})"
    r"(?:\s+(?P<digest>[0-9a-fA-F]{64}))?"
    r"(?:\s+(?P<ref>\S+))?\s*$",
    re.IGNORECASE,
)
# HTML comments trailing a verdict, such as the ChatGPT reviewer's `<!-- chatgpt-review -->`
# marker line (#2673). They render as nothing, so they are dropped before the verdict is
# matched; only trailing ones, so anything visible after the reference is still refused.
_TRAILING_HTML_COMMENTS_RE = re.compile(r"(?:\s*<!--(?:(?!-->).)*-->)+\s*$", re.DOTALL)


class CommandKind(Enum):
    EXTENSION = "extension"  # a command an extension declared (#179), routed to it
    CLAUDE = "claude"
    STOP = "stop"
    MERGE = "merge"
    MERGE_REVIEW = "merge-review"
    REVIEWED = "reviewed"  # an external reviewer's verdict on a commit (#2108)
    INVALID_CLAUDE = "invalid-claude"
    INVALID_STOP = "invalid-stop"
    INVALID_MERGE = "invalid-merge"
    INVALID_REVIEWED = "invalid-reviewed"


_INVALID = {
    CommandKind.INVALID_CLAUDE,
    CommandKind.INVALID_STOP,
    CommandKind.INVALID_MERGE,
    CommandKind.INVALID_REVIEWED,
}


@dataclass(frozen=True)
class Command:
    """A parsed control command. ``instruction`` is set only for ``CLAUDE``.

    ``targets`` is set only for the release kinds, and only when the asker named the pull
    requests to release. It is the operator's hatch out of a wrong automatic mapping, so an
    empty tuple means "discover them" and never "release nothing".

    ``verdict``, ``head`` and ``digest`` are set only for ``REVIEWED``: what an outside reviewer
    decided, the commit it decided about, and the hash of the diff it actually read.
    ``targets`` on that kind names the one pull request the verdict covers, which a card with a
    single open pull request never has to spell out.
    """

    kind: CommandKind
    instruction: str = ""
    targets: tuple[str, ...] = ()
    verdict: str = ""
    head: str = ""
    digest: str = ""

    @property
    def is_valid(self) -> bool:
        return self.kind not in _INVALID


def parse_control(
    body: str | None,
    *,
    bot_marker: str = DEFAULT_BOT_MARKER,
    extension_words: frozenset[str] | set[str] | None = None,
) -> Command | None:
    """Parse a comment body into a Command, or None if it isn't a control command.

    A bot-marked comment always returns None (no self-triggering). ``extension_words`` are the
    ``/word`` commands enabled extensions declared (#179): they are recognized only when their
    extension is actually enabled, so a project without that extension treats the word as
    ordinary prose rather than as a command it cannot serve.
    """
    if bot_marker and bot_marker in (body or ""):
        return None
    text = (body or "").strip()

    match = _CLAUDE_RE.match(text)
    if match:
        instruction = (match.group(1) or "").strip()
        if not instruction:
            return Command(CommandKind.INVALID_CLAUDE)
        lowered = instruction.lower()
        if lowered == "stop":
            return Command(CommandKind.STOP)
        if lowered.startswith("stop "):
            return Command(CommandKind.INVALID_STOP)
        return Command(CommandKind.CLAUDE, instruction)

    if extension_words:
        head = text.split(maxsplit=1)
        word = head[0].lower() if head else ""
        if word in {value.lower() for value in extension_words}:
            return Command(CommandKind.EXTENSION, text)

    if text.lower().startswith("/reviewed"):
        command = _reviewed(_TRAILING_HTML_COMMENTS_RE.sub("", text))
        if command.verdict == "no-code":
            return replace(command, verdict="changes-needed")
        return command

    if text.lower().startswith("/merge"):
        review = _MERGE_REVIEW_RE.match(text)
        if review:
            return _release(CommandKind.MERGE_REVIEW, review.group(1))
        plain = _MERGE_RE.match(text)
        if plain:
            return _release(CommandKind.MERGE, plain.group(1))
        return Command(CommandKind.INVALID_MERGE)

    return None


def parse_verdicts(body: str | None, *, bot_marker: str = "") -> tuple[Command, ...] | None:
    """Every ``/reviewed`` verdict in a comment, one per line, or None if it carries none.

    A Work with more than one pull request gets one verdict line for each, and a reviewer may
    send them in one comment (#3161). Every non-blank line must be a verdict: one line that is
    not makes the whole comment a single ``INVALID_REVIEWED``, so a half-read comment never
    records some of its verdicts and drops the rest without a word. The Poller's own intake
    still reads one command per comment through ``parse_control``.
    """
    if bot_marker and bot_marker in (body or ""):
        return None
    text = _TRAILING_HTML_COMMENTS_RE.sub("", (body or "").strip())
    if not text.lower().startswith("/reviewed"):
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    commands = tuple(_reviewed(line) for line in lines)
    if any(not command.is_valid for command in commands):
        return (Command(CommandKind.INVALID_REVIEWED),)
    return commands


def _reviewed(text: str) -> Command:
    """One ``/reviewed`` verdict, or ``INVALID_REVIEWED`` when it does not read as one."""
    reviewed = _REVIEWED_RE.match(text)
    if not reviewed:
        return Command(CommandKind.INVALID_REVIEWED)
    verdict = reviewed.group("verdict").lower()
    digest = (reviewed.group("digest") or "").lower()
    if verdict in ("pass", "no-code") and not digest:
        return Command(CommandKind.INVALID_REVIEWED)
    named = reviewed.group("ref")
    targets: tuple[str, ...] = ()
    if named:
        ref = _REF_RE.match(named)
        if not ref:
            return Command(CommandKind.INVALID_REVIEWED)
        targets = (f"{ref.group('repo')}#{int(ref.group('number'))}",)
    return Command(
        CommandKind.REVIEWED,
        targets=targets,
        verdict=verdict,
        head=reviewed.group("head").lower(),
        digest=digest,
    )


def _release(kind: CommandKind, tail: str | None) -> Command:
    """A release command, with the pull requests the asker named if they named any.

    Anything in the tail that is not a well-formed ref makes the whole command invalid rather
    than a bare release of everything discovered: a typo'd ref is someone trying to narrow the
    release, and quietly widening it back to "all of them" is the failure this hatch exists to
    prevent.
    """
    words = (tail or "").split()
    if not words:
        return Command(kind)
    targets = []
    for word in words:
        match = _REF_RE.match(word)
        if not match:
            return Command(CommandKind.INVALID_MERGE)
        targets.append(f"{match.group('repo')}#{int(match.group('number'))}")
    return Command(kind, targets=tuple(dict.fromkeys(targets)))
