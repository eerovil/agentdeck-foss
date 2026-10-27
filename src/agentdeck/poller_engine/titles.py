"""Recovering a card's real title when nobody wrote one (#338, #595).

An issue filed in a hurry — from a phone, mostly — is named by something other than its
request, and what everything downstream reads is that: the board column, the issue list, and
the deck's session card, which resolves the issue's own title (#310). Two shapes produce it:

* **an issue template's pre-filled placeholder**, kept because the request was typed after it
  or straight into the body (#338);
* **markup or a link**, because GitHub fills the title from the first line of what was pasted,
  so a dropped screenshot becomes ``<img width="1080" …`` and a shared link becomes the URL
  itself (#595). Both name no work in any project, which is why — unlike a placeholder, whose
  wording only the project knows — this needs no configuration to recognise.

The repair is deliberately literal. It never invents a title; it only recovers text the
requester already wrote, in the two places they write it:

* **after the placeholder in the title** — ``Requested changes fix the export`` — which is what
  typing into a pre-filled field produces;
* **the body's first real line**, when the title itself says nothing.

Everything else is left alone: a title that carries no placeholder and reads as a statement, a
placeholder with nothing after it and an empty body, a body whose every line is more markup. A
title this module declines to replace is still an honest signal that the issue was filed
without one, which beats a guessed one.

Comments are not read. The body is the brief a template asks for, and a comment thread is where
instructions, bot markers and control commands live — the first line of one is no more likely to
name the work than the placeholder it would replace.

Pure and stdlib-only like the rest of the engine core: this decides what a title *should* be,
and the caller decides whether to write it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# GitHub accepts far longer, but a title is read in a list, on a card and on a phone. This is
# the deck's own ceiling for a generated title, so a repaired issue title and the card it
# produces are cut at the same place rather than at two.
MAX_TITLE_CHARS = 80

# Below this a "title" is punctuation or a stray character, not a statement of the work.
_MIN_TITLE_CHARS = 3

_SPACE_RE = re.compile(r"\s+")
# Template guidance lives in HTML comments, which are invisible on GitHub and would otherwise
# be the first thing a body-derived title picked up.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# The markdown a first line commonly opens with: a heading, a bullet, a quote, a numbered item.
_LEAD_RE = re.compile(r"^(?:[#>*+-]+|\d+[.)])\s+")
# A line has to say a word to be a title: a horizontal rule or a row of markers is layout.
_WORD_RE = re.compile(r"\w")
# An embed rather than a sentence: an HTML element (a pasted screenshot arrives as `<img …`)
# or a markdown image. Matching the opening alone is enough — GitHub truncates a title, so the
# tag on one is rarely even closed.
_MARKUP_RE = re.compile(r"^(?:<[A-Za-z!/]|!\[)")
# A link and nothing else. A URL *inside* a sentence is fine; a title that is only a URL says
# where to look rather than what the work is.
_LINK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S*$")
# What may sit between the placeholder and what the requester typed after it.
_JOINERS = " \t:;,.-–—·|/"


def _clean(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _names_the_work(text: str) -> bool:
    """Could this line be read as a title, or is it layout, markup, a bare link, or too short?"""
    if len(text) < _MIN_TITLE_CHARS or not _WORD_RE.search(text):
        return False
    return not _MARKUP_RE.match(text) and not _LINK_RE.match(text)


def _trim(value: str) -> str:
    """One line, collapsed, and cut to a readable length at a word boundary when there is one."""
    text = _clean(value)
    if len(text) <= MAX_TITLE_CHARS:
        return text
    cut = text[: MAX_TITLE_CHARS - 1]
    # Only break at a space in the second half: cutting at an early one would throw away most
    # of a title that happens to start with a short word.
    tail = cut.rfind(" ")
    if tail > MAX_TITLE_CHARS // 2:
        cut = cut[:tail]
    return cut.rstrip(" .,:;-") + "…"


def _from_body(body: object) -> str:
    """The body's first line that says something, or empty when it says nothing.

    A pasted screenshot or link is usually the *body's* first line too — it is the same paste
    that named the issue — so the line that names the work is the one after it.
    """
    for line in _COMMENT_RE.sub(" ", str(body or "")).splitlines():
        text = _clean(_LEAD_RE.sub("", _clean(line)))
        if _names_the_work(text):
            return _trim(text)
    return ""


def repaired_title(
    title: object, body: object, placeholders: Iterable[str] = ()
) -> str | None:
    """What this card should be called instead, or None to leave its title exactly as it is.

    A title qualifies two ways. It *starts* with one of the project's configured placeholders,
    matched case-insensitively — both the untouched template title and the one someone typed
    after — or it names no work at all, being markup or a bare link. The second needs no
    placeholder list: no project wants a card called ``<img width="1080"``.

    The replacement always names the work, so applying this is idempotent — what it writes can
    match neither test again, and a card is retitled once rather than every wake.
    """
    current = _clean(title)
    if not current:
        return None
    if not _names_the_work(current):
        # The title is the first line of a paste. Whatever the requester wrote about it is
        # further down the same paste, and there is nowhere else to look.
        candidate = _from_body(body)
        return candidate or None
    for raw in placeholders or ():
        placeholder = _clean(raw)
        if not placeholder or not current.casefold().startswith(placeholder.casefold()):
            continue
        candidate = _trim(current[len(placeholder):].strip(_JOINERS))
        if not _names_the_work(candidate):
            # The template title survived untouched, so the ask is in the body — the other
            # place the template itself tells the requester to write it.
            candidate = _from_body(body)
        if _names_the_work(candidate) and candidate.casefold() != placeholder.casefold():
            return candidate
    return None
