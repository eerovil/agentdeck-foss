"""The deck keeps a wayfinder map's lists from its tickets' answers (#3695, from #3663).

A ticket's Work posts one ``**Answer:**`` comment (``skills/wayfinder/WAYFINDER.md``), and the
map's own chat posts ``**Ideas sorted:**`` and ``**Destination redrawn:**`` comments on the map.
Each is a record of what it changes in the map's four lists: **Decisions so far**, **Not yet
specified**, **Ideas** and **Out of scope**. When one lands, the Work router rebuilds those
lists from every record the stored webhook copies hold and saves the map body, so tickets no
longer rewrite the body by hand, and two answering at once cannot drop each other's change.

The rebuild merges the records into the lists the body already has: a line the records do not
name stays, so charted fog, a person's own edits and the gists of answers older than the
stored streams survive. A ticket that already has a **Decisions so far** line keeps it; only a
missing one is added, from the answer's first sentence. Everything outside the four lists —
the person's opening words, Destination, Notes — is left byte for byte, and a list whose items
did not change keeps its own text, so nothing changed means no write.

Everything here is pure except ``scan``, which reads one repository's stored streams.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ANSWER = "answer"
IDEAS_SORTED = "ideas sorted"
DESTINATION_REDRAWN = "destination redrawn"

DECISIONS = "decisions so far"
FOG = "not yet specified"
IDEAS = "ideas"
OUT_OF_SCOPE = "out of scope"
#: The lists, in the order a map body has them (``WAYFINDER.md``'s template).
LISTS = (DECISIONS, FOG, IDEAS, OUT_OF_SCOPE)
_TITLES = {
    DECISIONS: "Decisions so far",
    FOG: "Not yet specified",
    IDEAS: "Ideas",
    OUT_OF_SCOPE: "Out of scope",
}

#: The comment authors whose records count: the deck's own login is matched by the caller,
#: and a person writing on the repository holds one of these.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
#: How long a gist taken from an answer's first sentence may be.
MAX_GIST = 300

_RECORD_RE = re.compile(r"^\s*\*\*(Answer|Ideas sorted|Destination redrawn):\*\*", re.IGNORECASE)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(.*\S)\s*$")
_LINE_RE = re.compile(
    r"^\s*(Graduated|Fog|Out of scope|Revises|Idea|Kept):\s*(.*\S)\s*$", re.IGNORECASE
)
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
_ISSUE_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/(?:issues|pull)/(\d+)", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?](?=\s+(?:[A-Z\[(`*\"]|$))")
_TRAILING_SOURCE = re.compile(r"\s*\([^()]*\)\s*$")


def record_kind(body: object) -> str:
    """``ANSWER``, ``IDEAS_SORTED``, ``DESTINATION_REDRAWN``, or "" for any other comment."""
    match = _RECORD_RE.match(body) if isinstance(body, str) else None
    return match.group(1).casefold() if match else ""


@dataclass(frozen=True)
class Record:
    """One record comment, as its lines say it changes the map."""

    kind: str
    issue: tuple[str, int]  # the issue it is on: a ticket, or the map
    title: str  # that issue's title
    url: str  # that issue's html url
    created_at: str = ""
    gist: str = ""  # ANSWER only: the answer's first sentence
    graduated: tuple[str, ...] = ()
    fog: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    revises: tuple[str, ...] = ()  # the issue urls an answer revises
    ideas: tuple[str, ...] = ()
    sorted_ideas: tuple[str, ...] = ()  # IDEAS_SORTED only: every idea it kept or dropped


def gist(text: str) -> str:
    """The first sentence of an answer's first paragraph, without its full stop."""
    paragraph = " ".join(text.strip().split("\n\n", 1)[0].split())
    end = _SENTENCE_END.search(paragraph)
    first = paragraph[: end.start()] if end else paragraph.rstrip(".")
    if len(first) > MAX_GIST:
        first = first[: MAX_GIST - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return first.strip()


def parse(
    body: str, *, issue: tuple[str, int], title: str, url: str, created_at: str = ""
) -> Record | None:
    """The record a comment holds, or ``None`` when it is not one."""
    kind = record_kind(body)
    if not kind:
        return None
    lines: dict[str, list[str]] = {}
    for line in body.splitlines():
        match = _LINE_RE.match(line)
        if match is not None:
            lines.setdefault(match.group(1).casefold(), []).append(match.group(2))
    sorted_ideas = []
    if kind == IDEAS_SORTED:
        sorted_ideas = [kept.split("→", 1)[0].strip() for kept in lines.get("kept", ())]
        sorted_ideas += [
            dropped.rsplit("—", 1)[0].strip()
            for dropped in lines.get("out of scope", ())
            if dropped.rstrip().casefold().endswith("not picked")
        ]
    revises = []
    for line in lines.get("revises", ()):
        link = _LINK.search(line)
        if link is not None:
            revises.append(link.group(2))
    answer = _RECORD_RE.sub("", body, count=1)
    return Record(
        kind=kind,
        issue=issue,
        title=title,
        url=url,
        created_at=created_at,
        gist=gist(answer) if kind == ANSWER else "",
        graduated=tuple(lines.get("graduated", ())),
        fog=tuple(lines.get("fog", ())),
        out_of_scope=tuple(lines.get("out of scope", ())),
        revises=tuple(revises),
        ideas=tuple(lines.get("idea", ())) if kind == ANSWER else (),
        sorted_ideas=tuple(sorted_ideas),
    )


def _norm(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    return " ".join(re.sub(r"[*_`]+", "", text).split()).casefold().rstrip(".")


def _idea_key(text: str) -> str:
    """An idea without the "(where it came from)" a line ends with."""
    return _norm(_TRAILING_SOURCE.sub("", text))


def _issue_key(url: str) -> str:
    match = _ISSUE_URL.search(url or "")
    if match is None:
        return (url or "").strip().rstrip("/").casefold()
    owner, repo, number = match.groups()
    return f"{owner}/{repo}#{number}".casefold()


def _link_key(text: str) -> str:
    """A line's subject: the issue its first link names, else its own words."""
    link = _LINK.search(text)
    return _issue_key(link.group(2)) if link else _norm(text)


@dataclass
class _List:
    """One of the four lists as the body holds it."""

    start: int = -1  # the index of its heading line, -1 when the body has none
    end: int = -1  # the index just past its last line
    items: list[str] = field(default_factory=list)  # each bullet's text, continuations joined
    raw: list[list[str]] = field(default_factory=list)  # each bullet's own lines
    other: list[str] = field(default_factory=list)  # its lines that belong to no bullet


def _lists(lines: Sequence[str]) -> dict[str, _List]:
    found = {name: _List() for name in LISTS}
    current: _List | None = None
    fence = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        heading = None if fence else _HEADING.match(line)
        if heading is not None:
            if current is not None:
                current.end = index
            name = heading.group(2).strip().casefold()
            current = found[name] if name in found and found[name].start < 0 else None
            if current is not None:
                current.start = index
            continue
        if current is None:
            continue
        bullet = None if fence else _BULLET.match(line)
        if bullet is not None:
            current.items.append(bullet.group(1))
            current.raw.append([line])
        elif line.strip() and line[:1].isspace() and current.raw:
            current.items[-1] += " " + line.strip()
            current.raw[-1].append(line)
        elif line.strip() and not line.strip().startswith("<"):
            current.other.append(line)
    if current is not None:
        current.end = len(lines)
    return found


def rebuild(body: str, records: Iterable[Record]) -> str:
    """The map body with its four lists brought up to date with ``records``.

    ``records`` are every answer of the map's tickets and every record on the map itself, in
    the order they were posted. The rules are those ``WAYFINDER.md`` gave a ticket's hand
    reconcile:

    - a **Decisions so far** line for every answered ticket no record lists as out of scope;
      a ticket another answer revises ends ``(revised by [<title>](<link>))``;
    - **Not yet specified** loses each bullet a record lists as ``Graduated:`` and gains each
      ``Fog:`` bullet none does;
    - **Ideas** gains each ``Idea:`` line and loses each idea an ``**Ideas sorted:**`` lists;
    - **Out of scope** gains each ``Out of scope:`` line.
    """
    records = list(records)
    lines = body.split("\n")
    held = _lists(lines)
    graduated = {_norm(text) for record in records for text in record.graduated}
    gone = {_link_key(text) for record in records for text in record.out_of_scope}
    sorted_ideas = {_idea_key(text) for record in records for text in record.sorted_ideas}
    revisers: dict[str, list[Record]] = {}
    for record in records:
        if record.kind == ANSWER:
            for url in record.revises:
                revisers.setdefault(_issue_key(url), []).append(record)

    wanted: dict[str, list[str]] = {}

    # Decisions so far: the lines the body has, then one per answered ticket missing one.
    decisions = []
    seen = set()
    for item in held[DECISIONS].items:
        key = _link_key(item)
        if key in gone:
            continue
        seen.add(key)
        decisions.append(_revised(item, revisers.get(key, ())))
    for record in records:
        key = _issue_key(record.url)
        if record.kind != ANSWER or key in seen or key in gone:
            continue
        seen.add(key)
        line = f"[{record.title}]({record.url})" + (f" — {record.gist}" if record.gist else "")
        decisions.append(_revised(line, revisers.get(key, ())))
    wanted[DECISIONS] = decisions

    fog = [item for item in held[FOG].items if _norm(item) not in graduated]
    have = {_norm(item) for item in fog}
    for record in records:
        for text in record.fog:
            if _norm(text) not in graduated and _norm(text) not in have:
                have.add(_norm(text))
                fog.append(text)
    wanted[FOG] = fog

    ideas = [item for item in held[IDEAS].items if _idea_key(item) not in sorted_ideas]
    have = {_idea_key(item) for item in ideas}
    for record in records:
        for text in record.ideas:
            if _idea_key(text) not in sorted_ideas and _idea_key(text) not in have:
                have.add(_idea_key(text))
                ideas.append(text)
    wanted[IDEAS] = ideas

    scope = list(held[OUT_OF_SCOPE].items)
    have = {_link_key(item) for item in scope}
    for record in records:
        for text in record.out_of_scope:
            if _link_key(text) not in have:
                have.add(_link_key(text))
                scope.append(text)
    wanted[OUT_OF_SCOPE] = scope

    return _write(lines, held, wanted)


def _revised(line: str, revisers: Sequence[Record]) -> str:
    for record in revisers:
        if record.url and record.url not in line:
            line = f"{line} (revised by [{record.title}]({record.url}))"
    return line


def _write(lines: list[str], held: dict[str, _List], wanted: dict[str, list[str]]) -> str:
    """``lines`` with each list whose items changed written out again, and nothing else."""
    edits: list[tuple[int, int, list[str]]] = []
    missing = []
    for name in LISTS:
        current, items = held[name], wanted[name]
        if items == current.items:
            continue
        if current.start < 0:
            missing.append(name)
            continue
        block = [""]
        for item in items:
            # A line kept as it was keeps its own bullet and wrapping.
            if item in current.items:
                block.extend(current.raw[current.items.index(item)])
            else:
                block.append(f"- {item}")
        block.extend(["", *current.other] if current.other else [])
        block.append("")
        edits.append((current.start + 1, current.end, block))
    out = list(lines)
    for start, end, block in sorted(edits, reverse=True):
        out[start:end] = block
    text = "\n".join(out)
    for name in missing:
        bullets = "\n".join(f"- {item}" for item in wanted[name])
        text = f"{text.rstrip()}\n\n## {_TITLES[name]}\n\n{bullets}\n"
    return text


# --- the stored streams ---


@dataclass
class StreamFacts:
    """What one repository's stored webhook streams say about wayfinder maps."""

    #: Each issue's parent, as its newest ``sub_issues`` delivery left it (``None``: unlinked).
    parents: dict[tuple[str, int], tuple[str, int] | None] = field(default_factory=dict)
    #: Each record comment by id, as its newest delivery left it; a deleted one is ``None``.
    comments: dict[int, dict | None] = field(default_factory=dict)


def _issue_ref(issue: object, repository: str) -> tuple[str, int] | None:
    if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
        return None
    url = issue.get("repository_url")
    if isinstance(url, str) and url.count("/") >= 2:
        repository = "/".join(url.rstrip("/").split("/")[-2:])
    return (repository.lower(), issue["number"])


def note(facts: StreamFacts, event: str, payload: Mapping, repository: str) -> None:
    """Fold one delivery into ``facts``; every other event changes nothing."""
    repository = repository.lower()
    action = payload.get("action")
    if event == "sub_issues":
        child = _issue_ref(payload.get("sub_issue"), repository)
        parent = _issue_ref(payload.get("parent_issue"), repository)
        if child is None or parent is None:
            return
        if action in {"sub_issue_added", "parent_issue_added"}:
            facts.parents[child] = parent
        elif action in {"sub_issue_removed", "parent_issue_removed"}:
            if facts.parents.get(child) == parent:
                facts.parents[child] = None
        return
    if event != "issue_comment":
        return
    comment, issue = payload.get("comment"), payload.get("issue")
    if not isinstance(comment, dict) or not isinstance(comment.get("id"), int):
        return
    if action == "deleted":
        if comment["id"] in facts.comments:
            facts.comments[comment["id"]] = None
        return
    if not record_kind(comment.get("body")) or not isinstance(issue, dict):
        return
    if isinstance(issue.get("pull_request"), dict):
        return
    held = facts.comments.get(comment["id"])
    stamp = str(comment.get("updated_at") or "")
    if held and stamp and str(held.get("updated_at") or "") > stamp:
        return  # an older copy delivered late
    ref = _issue_ref(issue, repository)
    if ref is None:
        return
    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    facts.comments[comment["id"]] = {
        "issue": ref,
        "title": str(issue.get("title") or ""),
        "url": str(issue.get("html_url") or ""),
        "labels": [
            str(label.get("name") if isinstance(label, dict) else label)
            for label in issue.get("labels") or ()
        ],
        "kind": record_kind(comment.get("body")),
        "body": str(comment.get("body") or ""),
        "created_at": str(comment.get("created_at") or ""),
        "updated_at": stamp,
        "login": str(user.get("login") or ""),
        "association": str(comment.get("author_association") or ""),
    }


def stream_paths(root: Path, repository: str) -> list[Path]:
    """One repository's streams, oldest first: its archives, then the live file."""
    owner, _, name = repository.partition("/")
    live = Path(root).expanduser() / owner / f"{name}.jsonl"
    archives = []
    for path in live.parent.glob(f"{name}.jsonl.*.gz"):
        index = path.name[len(name) + len(".jsonl.") : -len(".gz")]
        if index.isdigit():
            archives.append((int(index), path))
    return [path for _index, path in sorted(archives, reverse=True)] + [live]


def scan(root: Path, repository: str) -> StreamFacts:
    """Read one repository's stored streams for sub-issue links and record comments.

    Only ``sub_issues`` and ``issue_comment`` lines are decoded. A stream that cannot be read,
    or a line that will not parse, is skipped: the records it held are already in the body."""
    facts = StreamFacts()
    for path in stream_paths(root, repository):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rb") as handle:
                for line in handle:
                    if b'"issue_comment"' not in line and b'"sub_issues"' not in line:
                        continue  # the payload is base64: only the envelope can match
                    try:
                        record = json.loads(line)
                        payload = json.loads(base64.b64decode(record["raw_body_base64"]))
                    except (ValueError, KeyError, TypeError):
                        continue
                    if isinstance(payload, dict):
                        note(facts, str(record.get("event") or ""), payload, repository)
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
    return facts
