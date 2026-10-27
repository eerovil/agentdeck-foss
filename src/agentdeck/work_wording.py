"""The front page's words for a Work's wait (#2254): plain sentences, never the engine's.

The operator's category (``work_attention``) keeps the engine's own reason, and the Work page
shows it word for word. A front-page row has one short line, so a few known engine sentences
are rewritten here into what they mean for the person reading; anything not in the table is
shown as it is, so a new engine sentence is never hidden, only not yet shortened.

``question`` picks what a parked Work is asking out of the last thing its worker said, so a
Needs answer row can show the question instead of the column the card rests in.
"""

from __future__ import annotations

import re

# (engine sentence, plain sentence), tried in order on each part of a reason. ``\1`` and the
# like carry a number or name across.
_PLAIN: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), plain)
    for pattern, plain in (
        # ``Not merged:`` since #2327; ``Review stalled:`` is how an older deck said it.
        (r"^(?:Not merged|Review stalled): brought forward (\d+) times? and still not "
         r"merged\b[^;]*$", r"Not merged after \1 update rounds"),
        (r"^(?:Not merged|Review stalled): reviewed, up to date with its base, and still not "
         r"merged$", "Reviewed, up to date, not merged"),
        (r"^(?:Not merged|Review stalled): \d+ commits? behind its base, and GitHub would not "
         r"merge the base in\b[^;]*$", "Conflicts with its base branch"),
        (r"^The reviewer doesn't watch (\S+)$", r"No reviewer watches \1"),
        (r"^Reviewed at (\w+); ready for a person to decide$", r"Reviewed at \1 · your call"),
        (r"^In review; waiting for a person$", "Ready for a human reviewer"),
        (r"^External review gave up\b[^;]*$", "Automatic review gave up; your call"),
        (r"^In review, but no pull request is known$", "In review, but no pull request is linked"),
        (r"^.*\bdelivery never landed\b.*$", "The worker never started"),
        (r"^The worker never started$", "The worker never started"),
        (r"^The card is in Needs More Info$", "Waiting for your answer on the issue"),
        # The engine's release parks (#2332).
        (r"^Auto-merge on `?([\w./-]+#\d+)`? is no longer armed on GitHub\b.*$",
         r"Auto-merge on \1 was switched off; merge again"),
        (r"^\*\*Production deploy failed\.\*\*.*?(; AgentDeck is re-checking the deploy)?$",
         r"Merged; the production deploy failed\1"),
        (r"^Production deploy did not reach a terminal status in time\b.*$",
         "Merged; the deploy did not finish in time"),
    )
)
# ``owner/repo#n`` → ``repo#n``: the owner is the same on nearly every row.
_OWNED_REF = re.compile(r"\b[\w.-]+/([\w.-]+#\d+)")
# The reviewer's grouping of several outputs (``work_attention._reviewer_reason``).
_ON_REFS = re.compile(r"^(?P<label>.+?) on (?P<refs>[\w.#/-]+(?:(?:, | and )[\w.#/-]+)*)$")
_MORE = re.compile(r"^(?P<ref>[\w.#/-]+): (?P<label>.+?) \(\+(?P<n>\d+) more\)$")


def _plain_one(text: str) -> str:
    for pattern, plain in _PLAIN:
        if pattern.match(text):
            return pattern.sub(plain, text)
    return text


def _plain_part(part: str) -> str:
    grouped = _ON_REFS.match(part)
    if grouped:
        return f"{_plain_one(grouped['label'])} on {grouped['refs']}"
    more = _MORE.match(part)
    if more:
        return f"{more['ref']}: {_plain_one(more['label'])} (+{more['n']} more)"
    return _plain_one(part)


def plain_reason(reason: str) -> str:
    """The front page's line for an operator reason: known engine sentences made plain."""
    if not reason:
        return ""
    whole = _plain_one(reason)
    if whole == reason:
        # A reason ``work_attention`` joined from several parts is made plain part by part.
        whole = "; ".join(_plain_part(part.strip()) for part in reason.split("; "))
    return _OWNED_REF.sub(r"\1", whole)


# Markdown a chat's text carries that a one-line row must not show.
_MARKUP = re.compile(r"`+|\*\*|__|^#+\s*|^\s*[-*]\s+|^\s*\d+\.\s+|\[([^\]]*)\]\([^)]*\)", re.M)
_SENTENCE = re.compile(r"[^.!?\n]*[?](?=\s|$)|[^.!?\n]+[.!]?")
QUESTION_LIMIT = 200


_BLOCKED_LINE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?\**blocked\b", re.I | re.M)
REASON_LIMIT = 200


def blocked_reason(text: str) -> str:
    """Why a worker says it is blocked, from its report: the first clause of the first line
    that starts with "Blocked", without the word itself; "" when no line does (#2332)."""
    plain = _MARKUP.sub(lambda match: match.group(1) or "", text or "")
    found = _BLOCKED_LINE.search(plain)
    if found is None:
        return ""
    line = plain[found.start():].splitlines()[0].strip().lstrip("-* ").strip()
    line = re.sub(r"^blocked\b\s*(?:on\b|by\b|:|—|-)?\s*", "", line, flags=re.I)
    first = _SENTENCE.findall(line)
    clause = (first[0] if first else line).split(";", 1)[0]
    picked = " ".join(clause.split()).rstrip(".;:")
    if len(picked) > REASON_LIMIT:
        picked = picked[: REASON_LIMIT - 1].rstrip() + "…"
    return picked


def question(text: str) -> str:
    """What a worker's last message asks, in one line; "" when it says nothing usable.

    The last sentence that ends in a question mark. A message that asks nothing gives "":
    its first sentence is usually what the worker did, not what it needs from a person.
    """
    plain = _MARKUP.sub(lambda match: match.group(1) or "", text or "").strip()
    if not plain:
        return ""
    sentences = [item.strip() for item in _SENTENCE.findall(plain) if item.strip()]
    asked = [item for item in sentences if item.endswith("?")]
    picked = asked[-1] if asked else ""
    picked = " ".join(picked.split())
    if len(picked) > QUESTION_LIMIT:
        picked = picked[: QUESTION_LIMIT - 1].rstrip() + "…"
    return picked
