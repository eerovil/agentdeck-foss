"""Ask the owner about a mechanism that keeps drawing review findings (#4498).

A pull request whose review keeps coming back about the same piece of code is often building a
mechanism that costs more than it saves: each round patches one more case of it. Once the
findings of a third round, at a third head, cite a file that two earlier rounds' findings also
cited, the message of that round's ``changes-needed`` verdict (never a findings message, nor a
``minor`` or ``no-code`` verdict's) carries one more ask: put one question to the owner in its
question box, keep, simplify or drop that mechanism, naming the simplest
alternative. The agent fixes the findings meanwhile; the owner's answer is theirs alone.

A round is a ``findings``, ``changes`` or ``escalated`` event that kept its text, at a head
other than the one being handed on now. A minor round is not one (#4596): a head that holds a
``no_code`` row (a ``minor`` or ``no-code`` verdict), or a ``changes`` row marked ``asked`` (the
reviewer said ``minor`` or ``no-code``, but the diff turned it into changes), and its own
findings are not a round either, nor asked about. A file
is one when one citation's path is the other's tail: ``src/x/work_router.py:12`` and
``work_router.py`` are the same file, ``src/a/__init__.py`` and ``src/b/__init__.py`` are not.
Tests and docs do not count, nor a verdict a base merge only carried to a later head.

Both reviewers send their findings before their verdict, so the ask goes only with the
verdict (#4596): the message a ``changes-needed`` verdict sends carries it, the findings read
back from the ``findings`` row at that head, and a findings message or a ``minor`` or
``no-code`` verdict's never does. Telling which review of one commit a verdict belonged to is
what every findings-time ask got wrong, so none is left. Each pull request is asked about at
most once, by the verdict message that stores its ``mechanism:<pull>`` event first, after all of
its own checks that may drop the message. The event names the delivery that claimed it, so a
resend of that message carries the ask again and another copy does not.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from . import review_history

log = logging.getLogger(__name__)

#: The extensions of the code files a citation can name: the source languages of the
#: repositories the deck reviews (Python, web, Java/Kotlin, PHP, C/C++, Go, Rust and more),
#: and their build, config and unit files. Prose like ``review_history.full_head`` has no such
#: extension, which is why this is a list rather than any word after a dot.
_CODE_EXTENSIONS = (
    "py pyi js mjs cjs jsx ts tsx vue svelte html css scss toml sh bash sql json yml yaml"
    " service timer socket java kt kts gradle groovy scala php rb go rs c h cc cpp cxx hpp"
    " cs swift lua dart ex exs erl clj jl zig"
)
#: A file path in findings text, with an optional ``:line``; group 1 is the path. The
#: extension ends at a word boundary, so ``app.tsx`` is not read as ``app.ts``.
_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:"
    + "|".join(_CODE_EXTENSIONS.split())
    + r"))(?![\w])(?::\d+)?"
)
#: Directories that hold tests, and the file names of tests in the languages above.
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "androidTest"})
_TEST_NAME_RE = re.compile(
    r"^test_|_test\.\w+$|_spec\.\w+$|Tests?\.\w+$|Spec\.\w+$|\.(?:test|spec)\.\w+$"
)
#: The verdicts that are a round of findings.
ROUND_KINDS = frozenset({review_history.FINDINGS, review_history.CHANGES, review_history.ESCALATED})
#: Earlier rounds that cited the file, besides the round handed on now.
EARLIER_ROUNDS = 2
#: The mark of a ``changes`` row whose reviewer said ``minor`` or ``no-code`` (its word, the
#: value) when the diff turned the verdict into changes (#4596): still a minor round.
ASKED = "asked"

MECHANISM_ASK = (
    "Mechanism check: these review findings cite `{path}`, as the findings of two earlier"
    " rounds did ({rounds}). Fix the findings as usual. Then, as the last thing in"
    " this turn, ask the owner one question about it in your question box. Its prompt opens"
    ' with "Mechanism check: `{path}`" and asks whether to keep, simplify or drop the mechanism'
    " in `{path}` these rounds keep patching. Offer three options: Keep (as it is, fixing"
    " findings as they come); Simplify, naming the simplest alternative you see: what stays,"
    " what goes, and roughly how many lines; Drop (what the pull request does without it)."
    " This is the owner's call, not yours: do not pick for them, do not answer it in prose or"
    " on the issue, and do not skip it, even under a standing rule to apply the fix you"
    " recommend. You may mark one option Recommended. Put this question, any other mechanism"
    ' check in this message, and the findings\' "Design finding:" question if they carry one'
    " and you have not asked it yet, in question boxes of at most four questions each, the"
    " design finding first; when there are more than"
    " four, open the next box right after the first is answered, in this same turn. Never drop"
    " or merge a question to fit. Once they answer, post their pick on the issue and act on it."
)


@dataclass(frozen=True)
class Target:
    """One pull request a findings delivery names: its current ``head``, ``read`` for its
    review history, and ``write(data)`` storing its ``mechanism:<pull>`` event (a key
    already stored adding nothing)."""

    pull: str
    head: str
    read: Callable[[], Iterable[review_history.ReviewEvent]]
    write: Callable[[dict], object]


def cited(text: str) -> list[str]:
    """The non-test, non-doc files ``text`` cites, each path once, in order."""
    found: list[str] = []
    for path in _PATH_RE.findall(text or ""):
        parts = path.split("/")
        if parts[0] == "docs" or _TEST_DIRS & set(parts[:-1]) or _TEST_NAME_RE.search(parts[-1]):
            continue
        if path not in found:
            found.append(path)
    return found


def same_file(one: str, other: str) -> bool:
    """Whether two cited paths name one file: equal, or one is the other's tail, so
    ``work_router.py`` is ``src/agentdeck/work_router.py`` but ``src/a/__init__.py`` is not
    ``src/b/__init__.py``."""
    return one == other or one.endswith(f"/{other}") or other.endswith(f"/{one}")


def _same(full: str, given: str) -> bool:
    full, given = (full or "").lower(), (given or "").lower()
    return len(given) >= 7 and full.startswith(given) or len(full) >= 7 and given.startswith(full)


def _at(event: review_history.ReviewEvent, head: str) -> bool:
    """Whether ``event`` is about ``head``: stored there, or carried there by a base merge
    from it (#4596 review): the router reads a findings heading's head, the deck its
    standing one."""
    return _same(event.head, head) or _same(str(event.data.get("carried_from") or ""), head)


def _verdict_at(
    history: Iterable[review_history.ReviewEvent], head: str
) -> review_history.ReviewEvent | None:
    """The newest ``changes`` or ``no_code`` row about ``head``: the round's verdict there."""
    found = [
        event
        for event in history
        if event.kind in (review_history.CHANGES, review_history.NO_CODE) and _at(event, head)
    ]
    return max(found, key=lambda event: (event.at, event.seq)) if found else None


def minor_at(history: Iterable[review_history.ReviewEvent], head: str) -> bool:
    """Whether the round at ``head`` is minor (#4596): the one reader of the mark. Its newest
    verdict there is a ``no_code`` row (a ``minor`` or ``no-code`` verdict), or a ``changes``
    row marked ``ASKED``."""
    event = _verdict_at(history, head)
    return event is not None and (
        event.kind == review_history.NO_CODE or bool(event.data.get(ASKED))
    )


def findings_at(history: Iterable[review_history.ReviewEvent], head: str) -> str:
    """The text of the newest ``findings`` row at ``head``, or ``""``: what a verdict's ask
    checks (#4596). A row a base merge carried there is this round's too: the deck stores the
    findings at the standing head, naming the head the reviewer read."""
    found = [
        event
        for event in history
        if event.kind == review_history.FINDINGS
        and _same(event.head, head)
        and event.data.get("text")
    ]
    if not found:
        return ""
    return str(max(found, key=lambda event: (event.at, event.seq)).data["text"])


def repeat(
    history: Iterable[review_history.ReviewEvent], text: str, head: str
) -> tuple[str, tuple[str, ...], int] | None:
    """``(path, heads, round)`` when findings ``text`` at ``head`` cite a file that the
    findings of two earlier rounds, at other heads, cited too; ``None`` otherwise. ``heads``
    are those two earlier heads and ``head``, abbreviated; ``round`` is this round's number
    among the pull request's rounds. A verdict a base merge carried to a later head
    (``carried_from``) is its first head's round, not another one."""
    now = cited(text)
    if not now or len(head or "") < 7:
        return None
    history = list(history)
    rounds: dict[str, list[str]] = {}
    for event in history:
        if event.kind not in ROUND_KINDS or event.data.get("minor"):
            continue
        if minor_at(history, event.head):
            continue
        said = str(event.data.get("text") or "")
        if not said or event.data.get("carried_from") or _same(event.head, head):
            continue
        rounds.setdefault(event.head, []).extend(cited(said))
    for path in now:
        matched = {
            sha: [other for other in paths if same_file(path, other)]
            for sha, paths in rounds.items()
        }
        earlier = [sha for sha, paths in matched.items() if paths]
        if len(earlier) >= EARLIER_ROUNDS:
            # The fullest form any of the rounds cited it as names it best.
            path = max((path, *(other for sha in earlier for other in matched[sha])), key=len)
            heads = (*(sha[:12] for sha in earlier[-EARLIER_ROUNDS:]), head[:12])
            return path, heads, len(rounds) + 1
    return None


def asks(targets: Iterable[Target], text: str, delivery: str) -> str:
    """The asks to append to the message of a ``changes-needed`` verdict sent under
    ``delivery``, whose round's findings are ``text``, one per file, or ``""`` (#4596). Called
    only once nothing more can drop the message.

    Every named pull request is checked: each stores its own ``mechanism:<pull>`` event when
    ``text`` is its third round citing a file. The rows of this round (at each pull request's
    current head) that name one file make one ask, naming every such pull request, carried by
    the delivery that stored the first of them. So when the deck reviewer's copy and the
    router's copy of one round each claim some of the pull requests, the owner still gets one
    question per file. Never raises: a pull request whose check fails is left out."""
    rows = [
        row for target in targets if (row := _claim(target, text, delivery)) is not None
    ]
    groups: list[list[review_history.ReviewEvent]] = []
    for row in rows:
        path = str(row.data.get("path") or "")
        group = next(
            (each for each in groups if same_file(path, str(each[0].data.get("path") or ""))),
            None,
        )
        if group is None:
            groups.append([row])
        else:
            group.append(row)
    found = []
    for group in groups:
        first = min(group, key=lambda row: (row.at, row.seq))
        if first.data.get("delivery") != delivery:
            continue
        path = max((str(row.data.get("path") or "") for row in group), key=len)
        rounds = "; ".join(
            f"{row.pull} round {row.data.get('round') or len(row.data.get('heads') or ())},"
            f" heads {', '.join(str(sha) for sha in row.data.get('heads') or ())}"
            for row in group
        )
        found.append(MECHANISM_ASK.format(path=path, rounds=rounds))
    return "\n\n".join(found)


def _claim(target: Target, text: str, delivery: str) -> review_history.ReviewEvent | None:
    """``target``'s mechanism row of this round, stored now for ``delivery`` when ``text`` is
    its third round citing a file and it has none yet; ``None`` when it has none at its head."""
    try:
        claimed = _claimed(history := list(target.read()))
        if claimed is None:
            found = repeat(history, text, target.head)
            if found is None:
                return None
            path, heads, number = found
            target.write(
                {"path": path, "heads": list(heads), "round": number, "delivery": delivery}
            )
            claimed = _claimed(target.read())
        if claimed is None or not _same(claimed.head, target.head):
            return None
        return claimed
    except Exception:  # noqa: BLE001 - the findings go on without this pull request's ask
        log.warning(
            "mechanism-check: %s at %s failed", target.pull, (target.head or "")[:12], exc_info=True
        )
        return None


def _claimed(
    history: Iterable[review_history.ReviewEvent],
) -> review_history.ReviewEvent | None:
    return next((event for event in history if event.kind == review_history.MECHANISM), None)
