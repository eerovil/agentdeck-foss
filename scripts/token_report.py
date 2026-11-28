#!/usr/bin/env python3
"""Report Claude token use and tool calls per stage and per Work (#4080).

Reads the transcripts under each account's config directory and the deck's
database, read-only, and prints what filled the weekly limit: by stage, by Work,
by kind of tool call, and the measurements that rank the savings ideas in
docs/research/token-use-2026-11.md.

    python3 scripts/token_report.py --since 2026-10-14T08:00 --until 2026-11-04T08:00

A "unit" is 1 % of the main account's week. The weights were fitted against
usage_history; alt's week is about half the size, so its tokens count double.

"Per finished Work" (#4236) is the yardstick of the plan to halve token use for the
same work (#4235). A Work is finished when its issue closed inside the window (read
live from GitHub, each repository's issues with their closes) and at least one
agent chat worked on it (the deck's ``works`` table). The
headline divides every unit spent in the window, chats with no Work, reviews and
Works still open included, by the Works finished in it, so no use is left out and
shared use is spread evenly. The life cost below it sums each finished Work's own
chats, subagent legs and review chats over its whole life (``--lookback`` days).
Baseline, Nov 3-10 2026: see docs/research/token-use-2026-11.md.

Next to it stand the Works started in the window and how fast the finished ones went
(#4467): from a Work's first turn to its last live check, or its last merge when it has no
live check (its close, with neither), so a hold before the first turn or an issue filed
days early is not slowness.
Use that leaves no transcript is counted too (#4687). The pre-review helpers' own log is a
row of its own and part of the headline. "Accounts' meters" sets the rise of the accounts'
weekly meters (``usage_history``) beside the counted units and prints the gap, so a new kind
of unsaved use shows by itself. One-off model tests (``ONE_OFF_TESTS``) stay in the totals
and are named on their own line.

A Haiku, Sonnet or Fable call is priced against Opus's (``MODEL_PRICE``),
and the code-review benchmark's own chats (``BENCH_DIR``) are a row of their own, not "no
Work".

Three splits the token-saving plan's builds are measured by print as sections of their own
(#4728; until then each was a one-off script). "Turns the deck started, by kind" splits the
wakes finer than the trigger does (``WAKE_KINDS``). "What a Work chat's call holds" is the
average context of a Work chat's call by part (``PARTS``), an estimate: a call's growth over
the last one is that call's output plus what arrived since, split by characters. "Guide text
at a chat's start" is the size of the guide files and of the deck's session guide each
repository's Work chats load.

"Output by what was written" (#4754) splits the output tokens by what the model wrote with
them: shell commands, code edits, text for GitHub and the deck, text to the person, and
thinking. It is an estimate (``written``): what a call shows is sized by its characters and
thinking is what is left of the call's output, because a transcript stores thinking empty. A
pre-review helper's run splits the same way from the lengths its usage log records.

"Work chats' restarts from a summary" (#4766) counts the chats that ran out of room and went
on from a summary (a ``compact_boundary`` row), per 100 calls, by account and by day, and what
they cost: the summarising call (an estimate from the row's ``preTokens`` and the summary's length)
and the new context the first call after it writes.

"Heaviest Works" lists the Works that used the most in the window, with the pull requests
their chats registered and the units a pull request (#4728), so a dear series of pull
requests under one issue shows. It reads the deck's database, never GitHub.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentdeck import kanban  # noqa: E402
from agentdeck.chat_use import INPUT, OUTPUT, READ, WRITE  # noqa: E402, F401
from agentdeck.command_artifacts import BASH_OUTPUT_CHARS  # noqa: E402
from agentdeck.deck_notices import without_routine  # noqa: E402
from agentdeck.models import IssueRef  # noqa: E402
from agentdeck.note_text import split_notes, without_notes  # noqa: E402
from agentdeck.pre_review_helpers import usage_log_path  # noqa: E402

ACCOUNTS = {"main": ("~/.claude", 1), "alt": ("~/.claude2", 2)}
DB = "~/.local/share/agentdeck/agentdeck.db"
COLD_GAP = 3600  # the cache lives an hour, so a longer pause writes the context again
AFTER_COMPACT = 62_000  # median context measured right after a compaction
BIG_CONTEXT = 200_000  # past this a chat should have compacted; #4059 made it so on Nov 3
# A model's list price against Opus's, the same for every kind of token: Haiku $1/$5 and
# Sonnet $3/$15 per million against Opus's $5/$25. Assumed to be how the weekly limit counts
# them (#4467). Fable is no list price: one 50-minute sample on Nov 18 moved the meter about 4
# times what Opus's weights say (#4683, an estimate). Any other model counts as Opus.
MODEL_PRICE = {"haiku": 0.2, "sonnet": 0.6, "fable": 4.0}
# One-off model tests (#4682, #4683), each Work's with the hours (UTC) its runs without a
# transcript filled on the main account, or None when its runs were subagents and have one.
ONE_OFF_TESTS = {
    "eerovil/agentdeck#4470": None,
    "eerovil/agentdeck#4473": None,
    "eerovil/agentdeck#4487": ("2026-11-18T19:00", "2026-11-18T19:30"),
    "eerovil/agentdeck#4488": ("2026-11-18T19:50", "2026-11-18T20:40"),
    "eerovil/agentdeck#4490": None,
    "eerovil/agentdeck#4494": ("2026-11-18T20:50", "2026-11-18T23:00"),
    "eerovil/agentdeck#4495": None,
    "eerovil/agentdeck#4509": ("2026-11-19T04:40", "2026-11-19T06:50"),
    "eerovil/agentdeck#4512": ("2026-11-19T07:10", "2026-11-19T08:00"),
    "eerovil/agentdeck#4530": ("2026-11-19T09:00", "2026-11-19T10:30"),
}
METER_GAP = 2 * 3600.0  # usage_history's longest gap in a month was about an hour
# Where the code-review benchmark runs its helper, coder and judge chats (#4428, #4434), as
# a transcript folder name: research spend no Work's chat made.
BENCH_DIR = "-agentdeck-review-bench-"
BENCH_JUDGE = re.compile(r"^You judge one run of a code-review benchmark")  # it runs elsewhere
# The Fable builds of the one-off test #4488 ran in worktrees of their own, with no Work.
TEST_WORK, TEST_DIR = "eerovil/agentdeck#4488", "-worktrees-r4488-"

TRIGGERS = (
    ("reviewer", r"^You are AgentDeck's Claude (fallback )?reviewer"),
    ("dream", r"^DREAM-AGENT"),
    ("start", r"^Context:"),
    ("spec_ok", r"^I approve the spec"),
    ("job", r"^Your queued job"),
    ("ci_wake", r"^AgentDeck wake-on: (commit status|check run|nothing arrived)"),
    ("wake", r"^AgentDeck (wake-on|watch:)"),
    ("review_pass", r"/reviewed pass|^AgentDeck: the review of .*passed clean"),
    (
        "review_result",
        r"^AgentDeck event: the reviewer|^AgentDeck: the deck's reviewer asks"
        r"|^AgentDeck: the review of",
    ),
    ("checklist", r"^AgentDeck: before "),
    ("merged", r"^AgentDeck: \S+ merged"),
    ("upkeep", r"^AgentDeck: (\S+ was behind|\S+ is now at|ready for merge|your last turn)"),
    # the ping that keeps a waiting chat's cache warm (#4737): a cost, not a wake
    ("keep_warm", r"^AgentDeck keep-warm: this only keeps this chat's prompt cache warm"),
    ("deck", r"^AgentDeck"),
    ("task_note", r"^<task-notification>"),
)
# A deck-started turn's finer kind (#4707): its name, the trigger it is a kind of, and what
# the first 400 characters of its message hold. The first match of its trigger wins.
WAKE_KINDS = (
    ("design passed -> coding", "deck", r"^AgentDeck: the design review of .{0,80}passed"),
    ("design gap -> redesign", "deck", r"^AgentDeck: the design review of .{0,80}found a gap"),
    (
        "review findings -> fix",
        "review_result",
        r"Review findings|asks only for minor|asks for changes|escalated",
    ),
    ("review passed -> verify job", "review_result", r"passed at|passed clean"),
    ("review waits on you", "review_result", r"has not started"),
    ("review result: other", "review_result", r"."),
    # the outcome is the message's first sentence; its command, on a later line, names the
    # helpers whether they ran or not, so the outcome is asked first
    ("job: pre-review helpers done", "job", r"^[^\n]*finished successfully.*pre-review-helpers"),
    ("job: other passed", "job", r"^[^\n]*finished successfully"),
    ("job: failed, stopped or did not run", "job", r"."),
    ("CI: nothing arrived", "ci_wake", r"nothing arrived"),
    ("CI: check or status", "ci_wake", r"."),
    ("wake: PR merged", "wake", r"was merged"),
    ("wake: deck deploy live", "wake", r"web worker serving this deck|now runs [0-9a-f]"),
    ("wake: deployment finished", "wake", r"deployment to"),
    ("wake: time set", "wake", r"the time you set|it is time to check"),
    ("wake: review chat", "wake", r"review chat"),
    ("wake: own check/watch", "wake", r"your check for|watch:"),
    ("wake: other", "wake", r"."),
    ("merged -> live check", "merged", r"."),
    ("review passed (old)", "review_pass", r"."),
    ("nudge: turn ended with no next step", "upkeep", r"your last turn"),
    ("upkeep: other", "upkeep", r"."),
    ("pre-review checklist", "checklist", r"."),
    (
        "merge blocked (checks/conflict/refused)",
        "deck",
        r"checks failed|conflicts with its base|refused to merge|cannot merge|will not merge",
    ),
    (
        "relayed comment/message",
        "deck",
        r"^AgentDeck (event: \S+ commented|message for this Work)",
    ),
    ("wayfinder: last ticket closed", "deck", r"last open ticket"),
    ("deck: other", "deck", r"."),
    ("task: subagent done", "task_note", r'<summary>Agent "'),
    ("task: background command done", "task_note", r"."),
)
EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# What a call's context holds (#4714), in the order it is printed.
STANDING, KEPT, BRIEF, MESSAGES, TOOL_OUTPUT, OWN, THINKING, OTHER = PARTS = (
    "standing start (prompt, tools, guides)",
    "kept by a compaction",
    "issue and start brief",
    "later messages",
    "tool output",
    "own text and tool calls",
    "own thinking",
    "other",
)
# The attachments a chat starts with and gets again after a compaction, and for each kind
# of attachment the keys whose text reaches the model (any other kind: all of it).
STANDING_KINDS = {
    "instructions",
    "skill_listing",
    "deferred_tools_delta",
    "deferred_tools_record",
    "mcp_instructions_delta",
    "agent_listing_delta",
    "session_context",
    "environment",
    "date",
}
SENT_KEYS = {
    "deferred_tools_delta": ("addedLines", "surfacedDefinitions"),
    "instructions": ("files",),
    "skill_listing": ("content",),
    "mcp_instructions_delta": ("addedBlocks",),
    "agent_listing_delta": ("addedLines",),
    "deferred_tools_record": ("entries",),
    "queued_command": ("prompt",),
    "session_context": ("context",),
}
KEPT_KINDS = {"file", "compact_file_reference", "task_status"}
# Characters per token, measured on this host's transcripts (#4714): a model's own text, the
# JSON of a tool call, and a message or tool result. A guide file reads at about 3.5.
TEXT_CPT, JSON_CPT, MESSAGE_CPT, GUIDE_CPT = 2.89, 2.21, 3.6, 3.5
# What a call's output was written as (#4754, from #4749's one-off script), in print order.
SHELL, THOUGHT, CODE, POSTED, TO_PERSON, SCRIPTS, OTHER_CALLS = WRITTEN = (
    "shell commands",
    "thinking (what is left of a call's output)",
    "code edits",
    "text for GitHub and the deck",
    "text to the person",
    "scripts that only read or check",
    "other tool calls",
)
# A text block and a tool call read shorter in tokens than their characters say: the
# estimate over the real output on calls with no thinking (#4749, one week of transcripts).
TEXT_FIX, TOOL_FIX = 0.94, 0.87
# A heredoc's opening; its word starts with a letter, so a shift (``1<<3``) is none.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
HEREDOC_FILE = re.compile(
    r"(?:cat|tee)\s*(?:-a\s*)?>{1,2}\s*([^\s<;&|]+)|>{1,2}\s*([^\s;&|<]+)\s*$"
    r"|tee\s+(?:-a\s+)?([^\s<;&|]+)"
)
WRITES_FILES = re.compile(r"write_text\(|\.write\(|open\([^)]*['\"][wa]\+?['\"]")
CODE_FILE = re.compile(
    r"\.(py|js|ts|tsx|jsx|html|css|scss|sh|sql|toml|ya?ml|cfg|ini|vue|php|go|rs|java|kt|service"
    r"|timer|conf|robot|po|xml|svg|csv)$"
)
SCRATCH_PATH = re.compile(r"^/(var/)?tmp/|^\$|^~?/?\.claude|/\.local/|^/dev/")
TEST_PATH = re.compile(r"(^|/)tests?(ing)?/|test_[^/]*\.py$|_test\.|\.test\.|\.spec\.")
POSTED_NAME = re.compile(r"body|^pr|comment|walk|summary|design|answer|spec|review|note")
SESSION_GUIDE = "You are running in an AgentDeck-owned session"
NOT_WORK = {"no-work", "review", "one-off test", "benchmark", "none"}
WOKEN = {"job", "ci_wake", "wake", "review_pass", "review_result", "merged", "upkeep", "deck"}
SHELL_KINDS = (
    ("read/search", r"^(grep|rg|cat|sed|ls|find|head|tail|wc|awk|jq|for|echo|stat|diff)\b"),
    ("gh", r"^gh\b"),
    ("git", r"^git\b"),
    ("test run", r"pytest|vitest|playwright"),
    ("agentdeck cli", r"agentdeck job|/agentdeck "),
    ("deck api", r"AGENTDECK_URL|curl"),
    ("script", r"^(\S*python\S*|uv|node|npm|npx|pnpm)\b"),
)


REVIEWED_WORK = re.compile(
    r"^You are AgentDeck's \w+ (?:fallback )?reviewer\..{0,400}?"
    r"(?:for the Work|asks for a review of|review the design of the Work)"
    r" ([\w.-]+/[\w.-]+#\d+)",
    re.S,
)
# A chat continued from another (an account move, a handoff, the cold-chat bar) names the
# earlier one by its key in a link or a ``chat-read`` command; a written handoff names only
# its issue.
CONTINUED = re.compile(r"Continue the work from")
EARLIER_CHAT = re.compile(r"\b[a-z_]+:[\w.-]+:([0-9a-f]{8}-[0-9a-f-]{27})\b")
ISSUE_REF = re.compile(r"\b([\w.-]+/[\w.-]+#\d+)\b")


def reviewed_work(text: str) -> str:
    """The Work a reviewer's brief names, lowercased, or "" for any other message."""
    found = REVIEWED_WORK.match(text)
    return found.group(1).lower() if found else ""


def prompt_work(text: str) -> str:
    """The Work a chat's first message names, for a chat the database ties to none.

    A worker's prompt is the deck's own ``issue_url`` evidence (``kanban.parse_ref``); a
    reviewer's brief names the Work it reviews.
    """
    inferred = kanban.parse_ref(text)
    url = IssueRef.parse(kanban.issue_url(inferred) or "") if inferred else None
    return f"{url.repository}#{url.number}".lower() if url else reviewed_work(text)


def continued_from(text: str) -> tuple[str, str]:
    """For a chat continued from another, the earlier chat's transcript id and the first
    issue the message names, each "" when it names none; ("", "") for any other message."""
    start = CONTINUED.search(text)
    if not start:
        return "", ""
    rest = text[start.start() :]
    chat, issue = EARLIER_CHAT.search(rest), ISSUE_REF.search(rest)
    return chat.group(1) if chat else "", issue.group(1).lower() if issue else ""


def model_price(model: str | None) -> float:
    """A model's price against Opus's, by its family's name in the model id."""
    return next((price for name, price in MODEL_PRICE.items() if name in (model or "")), 1.0)


def trigger(text: str) -> str:
    """What started a turn, from the first words of its message; deck notes in front of it
    (#4303) and a success notice's routine line (#4253) are read past, so the message counts
    as the kind it was before."""
    head = without_routine(without_notes(text))[:300]
    for name, pattern in TRIGGERS:
        if re.search(pattern, head, re.S):
            return name
    return "person"


def wake_kind(kind: str, text: str) -> str:
    """A deck-started turn's finer kind (``WAKE_KINDS``), or "" for a turn it does not name."""
    head = without_routine(without_notes(text))[:400]
    for name, coarse, pattern in WAKE_KINDS:
        if coarse == kind and re.search(pattern, head, re.S):
            return name
    return ""


def stage_turns(messages: list[str]) -> list[tuple[str, str]]:
    """The (stage, trigger) of each turn of one chat.

    A turn the deck or a person starts keeps the stage it arrived in; only the
    triggers below move it. A chat that never had its spec approved has no spec stage.
    """
    kinds = [trigger(text) for text in messages]
    if kinds and kinds[0] == "reviewer":
        return [("review", kind) for kind in kinds]
    if kinds and kinds[0] == "dream":
        return [("no-work", kind) for kind in kinds]
    stage = "spec" if "spec_ok" in kinds else "build"
    moves = {
        "spec_ok": "build",
        "checklist": "pre-review",
        "review_result": "fix",
        "review_pass": "release",
        "merged": "after-merge",
    }
    out = []
    for kind, text in zip(kinds, messages, strict=True):
        # a note is a message that did not wake the chat: it moves the stage as it did when
        # it came on its own, then the message it rides on does
        for note in split_notes(text)[0]:
            stage = moves.get(trigger(note), stage)
        stage = moves.get(kind, stage)
        out.append(("upkeep" if kind == "upkeep" else stage, kind))
    return out


def shell_kind(command: str) -> str:
    command = re.sub(r"^(cd \S+ *(&&|;) *)+", "", command.strip())
    command = re.sub(r"^([A-Z_]+=\S* +)+", "", command)
    for name, pattern in SHELL_KINDS:
        if re.search(pattern, command):
            return f"Bash: {name}"
    return "Bash: other"


def _file_kind(path: str, content: str) -> str:
    """What a file written whole is: code, a script that only reads, or text to post."""
    name = path.rsplit("/", 1)[-1]
    scratch = bool(SCRATCH_PATH.search(path))
    if CODE_FILE.search(name):
        if scratch and not TEST_PATH.search(path) and not WRITES_FILES.search(content):
            return SCRIPTS
        return CODE  # a scratch script that rewrites files is an edit done the long way
    kept = re.search(r"\.(md|rst|txt|json)$", name) and not POSTED_NAME.search(name.lower())
    if kept and not scratch:
        return CODE  # a document or a fixture in the repository
    return POSTED


def _heredoc_kind(intro: str, body: str) -> str:
    """What a heredoc's body is, from the line that opens it."""
    if re.search(r"python[\d.]*\b|uv run", intro) and not re.search(r"(cat|tee)\s.*>", intro):
        return CODE if WRITES_FILES.search(body) else SCRIPTS
    if ("bookkeep" in intro and "--summary" in intro) or "work-resolution" in intro:
        return POSTED
    if found := HEREDOC_FILE.search(intro):
        return _file_kind(next(name for name in found.groups() if name).strip("\"'"), body)
    if re.search(r"\bgh\s+(pr|issue|api)\b|\bcurl\b", intro):
        return POSTED
    return SHELL


def written(tool: str, given: dict) -> list[tuple[str, float]]:
    """One tool call's output as (what was written, tokens), an estimate from its characters.

    A shell command's heredoc bodies count as what they are (an edit script, a file, a
    comment for GitHub); the rest of the call, its frame and other fields included, is the
    command.
    """
    sent = len(json.dumps(given))
    tokens = (sent / JSON_CPT + 20) / TOOL_FIX
    if tool == "Write":
        path, content = str(given.get("file_path", "")), str(given.get("content", ""))
        return [(_file_kind(path, content), tokens)]
    if tool in EDITS:
        return [(CODE, tokens)]
    if tool != "Bash":
        return [(OTHER_CALLS, tokens)]
    parts: dict[str, float] = collections.defaultdict(float)
    lines = str(given.get("command", "")).split("\n")
    index = 0
    while index < len(lines):
        intro = lines[index]
        found = HEREDOC.search(intro)
        index += 1
        if not found or "<<<" in intro:
            continue
        end = next(
            (at for at in range(index, len(lines)) if lines[at].strip() == found.group(2)), None
        )
        if end is None:
            continue  # never closed: not a heredoc (``a << b`` in a script), so plain command
        text = "\n".join(lines[index:end])
        index = end + 1
        kind = _heredoc_kind(intro, text)
        if kind != SHELL:
            parts[kind] += tokens * (len(intro) + len(text) + 1) / sent
    parts[SHELL] += max(tokens - sum(parts.values()), 0.0)
    return list(parts.items())


@dataclass
class Call:
    """One model call. Each re-sends the conversation, however many tools it asks for."""

    ts: float
    weight: int
    fresh: int = 0
    write: int = 0
    read: int = 0
    output: int = 0
    tools: list[str] = field(default_factory=list)
    gap: float | None = None
    after_compact: bool = False
    # the restart just before it (#4766): the tokens the chat held when it was summarised,
    # from its compact_boundary row, and the summary's, from its length; () with no such row
    restart: tuple[int, int] = ()
    text: int = 0
    stage: str = "none"
    kind: str = "none"
    work: str = ""
    leg: str = ""
    sid: str = ""
    price: float = 1.0  # the model's price against Opus's
    turn: int = -1  # which of its chat's turns it is in
    wake: str = ""  # the turn's finer kind, when the deck started it
    parts: tuple[float, ...] = ()  # its context in tokens by part, in PARTS order
    pull: str = ""  # a pre-review helper's run: the pull request it read
    # what its tool calls answered, each as (the tool, characters)
    results: list[tuple[str, int]] = field(default_factory=list)
    # what its output shows, each as (what was written, estimated tokens), and whether it
    # thought; ``wrote`` None is a helper's run logged before the log kept its lengths
    wrote: list[tuple[str, float]] | None = field(default_factory=list)
    thought: bool = False

    @property
    def context(self) -> int:
        return self.fresh + self.write + self.read

    @property
    def scale(self) -> float:
        """What one token costs here against one on the main account's main model."""
        return self.weight * self.price

    @property
    def units(self) -> float:
        return self.scale * (
            self.fresh * INPUT + self.write * WRITE + self.read * READ + self.output * OUTPUT
        )


def _stamp(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _result_chars(content: object) -> int:
    """How long one tool result's text is; an image or another shape counts as nothing."""
    if isinstance(content, str):
        return len(content)
    return len(_text(content))


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _size(content: object) -> int:
    """About how many characters of a message or tool result reach the model."""
    if isinstance(content, str):
        return len(content)
    total = 0
    for block in content if isinstance(content, list) else ():
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            total += len(str(block.get("text") or ""))
        elif block.get("type") == "image":
            total += 6000
        elif block.get("type") == "tool_result":
            total += _size(block.get("content"))
        else:
            total += len(json.dumps(block))
    return total


def _sent(attachment: dict) -> int:
    """The characters of an attachment that reach the model, not its bookkeeping keys."""
    keys = SENT_KEYS.get(str(attachment.get("type")))
    return _chars([attachment.get(key) for key in keys] if keys else attachment)


def _chars(value: object) -> int:
    """The characters of the text inside a value, whatever lists and tables hold it: a
    guide file's own text, not the JSON around it or its escapes."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_chars(inner) for name, inner in value.items() if name not in ("type", "path"))
    if isinstance(value, list):
        return sum(_chars(inner) for inner in value)
    return 0 if value is None else len(str(value))


class _Held:
    """What a chat's context holds part by part, estimated call by call (#4714).

    A call's context is the last call's plus that call's whole output (thinking included)
    plus what arrived since, which is split among the arrivals by their characters. The
    first call of a chat, and the first after a compaction, has no earlier one: what arrived
    is priced by its characters and the rest is the standing start, or, past the chat's first
    standing start, what the compaction kept. It is an estimate, scaled to each call's real
    context.
    """

    def __init__(self) -> None:
        self.held = dict.fromkeys(PARTS, 0.0)
        self.pending: list[tuple[str, float]] = []  # arrived since the last call, in characters
        self.own = 0.0  # the last call's visible text and tool calls, in tokens
        self.output = 0
        self.last: int | None = None  # the last call's context; None at a start
        self.start = 0.0  # the chat's first standing start
        self.compactions = 0

    def arrived(self, part: str, chars: float) -> None:
        self.pending.append((part, chars))

    def attached(self, attachment: dict) -> None:
        kind = str(attachment.get("type"))
        part = OTHER
        if kind in STANDING_KINDS:
            part = STANDING
        elif kind == "queued_command":
            part = MESSAGES
        elif kind in KEPT_KINDS and self.last is None and self.compactions:
            part = KEPT
        self.arrived(part, _sent(attachment))

    def said(self, message: dict) -> None:
        """One row of the current call's reply: its output so far and what it shows."""
        self.output = max(self.output, (message.get("usage") or {}).get("output_tokens") or 0)
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                self.own += len(json.dumps(block.get("input") or {})) / JSON_CPT + 20
            elif block.get("type") == "text":
                self.own += len(str(block.get("text") or "")) / TEXT_CPT

    def compacted(self, chars: int) -> None:
        """A compaction: the standing start is sent again, beside the summary."""
        self.pending = [(part, n) for part, n in self.pending if part == STANDING]
        self.pending.append((KEPT, chars))
        self.own, self.output, self.last = 0.0, 0, None
        self.compactions += 1

    def call(self, context: int) -> tuple[float, ...]:
        """The parts of a new call's context, which holds ``context`` tokens."""
        chars = sum(n for _, n in self.pending)
        if self.last is None:
            self.held = dict.fromkeys(PARTS, 0.0)
            arrived = min(context * 0.9, chars / MESSAGE_CPT)
            rest = context - arrived
            self.start = self.start or rest
            self.held[STANDING] = min(rest, self.start)
            self.held[KEPT] = rest - self.held[STANDING]
        else:
            grew = max(context - self.last, 0)
            output = min(self.output, grew)
            shown = min(self.own, output)
            self.held[OWN] += shown
            self.held[THINKING] += output - shown
            arrived = grew - output
            if not chars:
                self.held[OTHER] += arrived
        for part, n in self.pending if chars else ():
            self.held[part] += arrived * n / chars
        total = sum(self.held.values())
        if total:  # a context that shrank (dropped thinking, a trimmed result) shrinks each part
            self.held = {part: tokens * context / total for part, tokens in self.held.items()}
        self.pending, self.own, self.output, self.last = [], 0.0, 0, context
        return tuple(self.held.values())


def read_transcript(
    path: Path, weight: int, seen: set, guides: dict | None = None
) -> tuple[list[Call], list[tuple]]:
    """A transcript's calls, and its turns as (time, message). Skips what it cannot read.

    ``guides``, when given, is filled with what the chat loaded at its start: ``files``, each
    guide file's path with its characters, and ``session``, the deck's session guide's.
    """
    calls: dict[tuple, Call | None] = {}  # None: counted in an earlier copy of this chat
    turns: list[tuple] = []
    last = None
    compacted = False
    restart: tuple[int, int] = ()
    held = _Held()
    asked: dict[str, tuple[Call, str]] = {}  # a tool call's id: the call that made it, the tool
    with path.open(errors="ignore") as lines:
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            ts = _stamp(row.get("timestamp"))
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            attachment = row.get("attachment")
            if row.get("type") == "attachment" and isinstance(attachment, dict):
                _guides(attachment, guides)
                if attachment.get("type") != "prompt_snapshot":  # a copy, not sent again
                    held.attached(attachment)
                continue
            if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                meta = row.get("compactMetadata")
                pre = meta.get("preTokens") if isinstance(meta, dict) else None
                # an unknown shape is a restart with no known size, not a failed report
                restart = (int(pre) if isinstance(pre, int | float) else 0, 0)
                continue
            if ts is None:
                continue
            if row.get("type") == "user":
                content = message.get("content")
                results = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                )
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        call, name = asked.pop(str(block.get("tool_use_id")), (None, ""))
                        if call is not None:
                            call.results.append((name, _result_chars(block.get("content"))))
                if row.get("isCompactSummary"):
                    compacted = True
                    # postTokens is about three times the summary: it holds more than it
                    if restart:
                        restart = (restart[0], round(_size(content) / TEXT_CPT))
                    held.compacted(_size(content))
                elif results:
                    for block in content:
                        output = isinstance(block, dict) and block.get("type") == "tool_result"
                        held.arrived(TOOL_OUTPUT if output else MESSAGES, _size([block]))
                else:
                    text = _text(content)
                    meta = row.get("isMeta")
                    if not meta:
                        turns.append((ts, text))
                    start = not meta and trigger(text) == "start"
                    held.arrived(BRIEF if start else MESSAGES, _size(content))
            elif (
                row.get("type") == "assistant"
                and message.get("usage")
                and isinstance(message["usage"], dict)
            ):
                if message.get("model") == "<synthetic>":
                    continue
                usage = message["usage"]
                key = (message.get("id"), row.get("requestId"))
                if key not in calls:
                    fresh, write, read = (
                        usage.get(name) or 0
                        for name in (
                            "input_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                        )
                    )
                    parts = held.call(fresh + write + read)
                    # a resumed chat copies its history into the new file: counted once, but
                    # still what the calls after it hold
                    calls[key] = None
                    if key not in seen:
                        seen.add(key)
                        calls[key] = Call(
                            ts,
                            weight,
                            fresh,
                            write,
                            read,
                            gap=ts - last if last else None,
                            after_compact=compacted,
                            price=model_price(message.get("model")),
                            parts=parts,
                            restart=restart if compacted else (),
                        )
                    # the first call after a restart takes it, a copied one too: a resumed
                    # chat's copy of a restart was counted in the file it came from
                    compacted, restart = False, ()
                held.said(message)
                call = calls[key]
                if call is None:
                    continue
                call.output = max(call.output, usage.get("output_tokens") or 0)
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        name = block.get("name") or "?"
                        given = block.get("input")
                        call.wrote += written(name, given if isinstance(given, dict) else {})
                        if name == "Bash":
                            name = shell_kind(str((block.get("input") or {}).get("command", "")))
                        name = "mcp" if name.startswith("mcp__") else name
                        call.tools.append(name)
                        asked[str(block.get("id"))] = (call, name)
                    elif block.get("type") == "text":
                        call.text += len(block.get("text", ""))
                        size = len(str(block.get("text") or "")) / TEXT_CPT / TEXT_FIX
                        call.wrote.append((TO_PERSON, size))
                    elif block.get("type") == "thinking":
                        call.thought = True
                last = ts
    return [call for call in calls.values() if call is not None], turns


def _guides(attachment: dict, guides: dict | None) -> None:
    """Note the guide text a chat's attachment shows it loaded: its guide files the first
    time they are listed, and the deck's session guide in the newest copy of its prompt."""
    if guides is None:
        return
    files = attachment.get("files")
    if attachment.get("type") == "instructions" and isinstance(files, list):
        sizes = {
            str(file.get("path")): len(str(file.get("content") or ""))
            for file in files
            if isinstance(file, dict)
        }
        if sizes:
            guides.setdefault("files", sizes)
    elif attachment.get("type") == "prompt_snapshot":
        for part in attachment.get("systemPrompt") or []:
            if isinstance(part, str) and part.lstrip().startswith(SESSION_GUIDE):
                guides["session"] = len(part)


def work_of_sessions(db_path: str) -> dict[str, list[tuple[float, str]]]:
    """Each transcript id's Works from when each began, on evidence the deck accepts.

    Evidence is the same as ``build_work_projection``'s: a chat's Runs, else a saved binding,
    else its newest registered issue. An ambiguous binding names only candidates.
    """
    db = sqlite3.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True)
    runs: dict[str, list[tuple[float, str]]] = {}
    for key, work, bound in db.execute(
        "select session_key, work_id, bound_at from session_run_bindings order by bound_at"
    ):
        if work.startswith("github_issue:"):
            ref = work.removeprefix("github_issue:").lower()
            runs.setdefault(key.split(":")[-1], []).append((_stamp(bound) or 0.0, ref))
    works = dict(runs)
    for key, work in db.execute(
        "select session_key, work_id from session_work_bindings"
        " where evidence in ('delegated', 'backfilled', 'side_chat') order by bound_at"
    ):
        if work.startswith("github_issue:"):
            ref = work.removeprefix("github_issue:").lower()
            works.setdefault(key.split(":")[-1], [(0.0, ref)])
    for key, repo, number in db.execute(
        "select session_key, repository, number from session_issues order by added_at desc"
    ):
        works.setdefault(key.split(":")[-1], [(0.0, f"{repo}#{number}".lower())])
    return works


CLOSES_QUERY = """
query($owner: String!, $name: String!, $since: DateTime!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $endCursor, filterBy: {since: $since}) {
      pageInfo { hasNextPage endCursor }
      nodes { number timelineItems(last: 100, itemTypes: [CLOSED_EVENT, REOPENED_EVENT]) {
        nodes { __typename ... on ClosedEvent { createdAt } ... on ReopenedEvent { createdAt } }
      } }
    }
  }
}"""


def _issue_events(repository: str, since: str) -> list[list[str]]:
    """Each issue updated since then as its number, then its closes and reopens in order,
    each ``closed <time>`` or ``reopened <time>``; 100 issues a page."""
    owner, name = repository.split("/")
    # The gh shim picks whose token speaks from the checkout's remote when the call itself
    # names no repository, and a GraphQL call names none it can read: name it as that remote.
    count = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
    remote = {
        "GIT_CONFIG_COUNT": str(count + 1),
        f"GIT_CONFIG_KEY_{count}": "remote.origin.url",
        f"GIT_CONFIG_VALUE_{count}": f"https://github.com/{repository}",
    }
    found = subprocess.run(
        ["gh", "api", "graphql", "--paginate", "-f", f"query={CLOSES_QUERY}",
         "-f", f"owner={owner}", "-f", f"name={name}", "-f", f"since={since}",
         "-q", ".data.repository.issues.nodes[] | [.number, (.timelineItems.nodes[]"
         ' | (if .__typename == "ClosedEvent" then "closed " else "reopened " end)'
         " + .createdAt)] | @tsv"],
        capture_output=True, text=True, timeout=300, check=True, env=os.environ | remote,
    )  # fmt: skip
    return [line.split("\t") for line in found.stdout.splitlines()]


def closed_at_end(events: list[tuple[str, float]], since: float, until: float) -> float | None:
    """When an issue closed, if it stood closed at the window's end by a close inside it."""
    last = max(((at, event) for event, at in events if at < until), default=None)
    return last[0] if last is not None and last[1] == "closed" and last[0] >= since else None


def github_closes(
    repository: str, since: float, until: float, numbers: set[str] | None = None
) -> dict[str, float]:
    """The repository's issues finished in the window, as ``owner/repo#n``, lowercased, each
    with when it closed.

    Finished means closed at the window's end by a close inside it, as it stood then: a
    reopen after the window does not take a close out of its week. Read from GitHub itself,
    so every close counts whether or not the deck ever cached that issue. Every issue updated
    since the window opened is listed (a close updates an issue, so none is missed) with its
    closes and reopens, 100 at a time in one ``gh`` call per repository: one call per reopened
    or later-closed issue passed a chat's hourly GitHub limit on older weeks (#4687).
    ``numbers`` limits it to those issues. The search API is not used: it caps a query at
    1000 results and cannot see some organisations' repositories.
    """
    # GitHub lists issues updated from ``since`` on: start a second early, so a
    # close exactly at the window's start with nothing after it is still listed
    opened = datetime.fromtimestamp(since - 1, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    finished: dict[str, float] = {}
    for number, *events in _issue_events(repository, opened):
        if numbers is not None and number not in numbers:
            continue
        split = [event.partition(" ") for event in events]
        stamped = [(kind, at) for kind, _, stamp in split if (at := _stamp(stamp)) is not None]
        settled = closed_at_end(stamped, since, until)
        if settled is not None:
            finished[f"{repository.lower()}#{number}"] = settled
    return finished


def finished_works(
    db_path: str, since: float, until: float, closes=github_closes
) -> tuple[dict[str, float], list[str]]:
    """Works whose issue closed in the window, each with when it closed, from GitHub itself.

    A Work is a row of the deck's ``works`` inventory, which it writes for every issue a chat
    worked on: registered on a chat, or inferred from a worker's prompt, of any provider. So
    a Work moved from Claude to Codex still counts as the same work done. Its close is read
    from GitHub, as it stood at the window's end, because the deck keeps no full close
    history: it saves a close only for Works awaiting a Finish, and caches only issues it has
    shown. Returns the finished Works
    and the repositories GitHub would not list, whose Works are then unknown.
    """
    db = sqlite3.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True)
    works = {
        key.lower()
        for (key,) in db.execute("select source_key from works where source_kind = 'github_issue'")
    }
    finished: dict[str, float] = {}
    unread: list[str] = []
    for repository in sorted({work.split("#")[0] for work in works}):
        numbers = {work.split("#")[1] for work in works if work.split("#")[0] == repository}
        try:
            closed = closes(repository, since, until, numbers)
            finished |= {work: at for work, at in closed.items() if work in works}
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", "") or exc
            print(f"no closes read for {repository}: {str(detail)[:200]}", file=sys.stderr)
            unread.append(repository)
    return finished, unread


def work_ends(db_path: str) -> dict[str, float]:
    """When each Work was last done: the later of its last live check and last merge.

    The deck keeps only the newest live check of a Work (``work_metadata``), whatever its
    result, and each pull request it saw merge for one (``work_merged_pulls``). A merge after
    a failed check (a reopened Work) is later than it, so the later of the two is taken.
    """
    db = sqlite3.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True)
    ends: dict[str, float] = {}
    for work, at in db.execute(
        "select work_id, max(merged_at) from work_merged_pulls group by work_id"
    ):
        ends[work.removeprefix("github_issue:").lower()] = _stamp(at) or 0.0
    for work, at in db.execute(
        "select work_id, live_check_at from work_metadata where live_check_at is not null"
    ):
        key = work.removeprefix("github_issue:").lower()
        ends[key] = max(ends.get(key, 0.0), _stamp(at) or 0.0)
    return {work: at for work, at in ends.items() if at}


def helper_calls(path: Path, since: float, until: float) -> tuple[list[Call], float | None]:
    """The pre-review helpers' runs in the window, from their own log, and when that begins.

    They keep no transcript (#4683). Each is one ``Call`` holding its whole run, so its
    context is a sum over the run, not a size.
    """
    calls: list[Call] = []
    begins = None
    for line in path.read_text(errors="ignore").splitlines() if path.exists() else ():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ts = _stamp(str(row.get("at") or "")) if isinstance(row, dict) else None
        if ts is None:
            continue
        begins = ts if begins is None else min(begins, ts)
        if since <= ts < until:
            _, weight = ACCOUNTS.get(str(row.get("account") or "").split(":")[-1], ("", 1))
            fields = ("input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens")
            call = Call(ts, weight, *(row.get(name) or 0 for name in fields))
            # a run over two models names both and does not split its tokens: the dearest
            call.price = max(map(model_price, str(row.get("model") or "").split(",")))
            call.stage = call.kind = "pre-review helpers"
            call.pull = str(row.get("pull") or "").lower()
            call.wrote, call.thought = None, True
            if "reply_chars" in row:  # logged since #4754
                reply, tools, asked = (
                    row.get(name) or 0 for name in ("reply_chars", "tool_chars", "tool_calls")
                )
                call.wrote = [
                    (TO_PERSON, reply / TEXT_CPT / TEXT_FIX),
                    (OTHER_CALLS, (tools / JSON_CPT + 20 * asked) / TOOL_FIX),
                ]
            calls.append(call)
    return calls, begins


def meter_rise(db_path: str, account: str, since: float, until: float) -> float | None:
    """How far an account's weekly meter rose in the window, in % of its own week, or None
    when its history does not reach from the window's start to its end (or to now).

    A fall of over 2 points is a new week, which starts at 0; a smaller dip is rounding.
    """
    db = sqlite3.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True)
    until = min(until, time.time())
    # a short window needs readings nearer its ends, or the rise is another time's
    gap = min(METER_GAP, (until - since) / 4)
    rise, high, seen = 0.0, None, float("-inf")
    for stamp, percent in db.execute(
        "select ts, seven_day_pct from usage_history where account_key = ?"
        " and seven_day_pct is not null order by ts",
        (f"claude_code:{account}",),
    ):
        ts = _stamp(stamp)
        if ts is None or ts >= until:
            continue
        if ts >= since and seen < since - gap:
            return None
        seen = ts
        base = 0.0 if high is None or percent < high - 2 else high
        if percent > base or base == 0.0:
            rise += percent - base if ts >= since else 0.0
            high = percent
    return rise if seen >= until - gap else None


def meter_check(
    db_path: str,
    inside: list[Call],
    helpers: list[Call],
    since: float,
    until: float,
    works: int = 0,
) -> list[str]:
    """The accounts' meters beside the units counted, the gap, and the one-off tests.

    ``works`` is how many Works finished, 0 when unknown.
    """

    def row(name: str, meter: float | None, calls: list[Call], runs: list[Call]) -> str:
        saved, unsaved = sum(c.units for c in calls), sum(c.units for c in runs)
        line = f"{name:5} transcripts {saved:6.1f}u  helpers {unsaved:5.1f}u"
        line += f"  counted {saved + unsaved:6.1f}u"
        if meter is None:
            return f"{line}  meter n/a (its history does not cover the window)"
        gap = meter - saved - unsaved
        share = f" ({gap / meter * 100:.1f}% of the meter)" if meter else ""
        return f"{line}  meter {meter:6.1f}u  gap {gap:6.1f}u{share}"

    lines, rises = [], []
    for name, (_, weight) in ACCOUNTS.items():
        # a week's percent as it stands: a call's weight already turns an account's tokens
        # into percent of that account's own week
        rises.append(meter_rise(db_path, name, since, until))
        mine = [[c for c in calls if c.weight == weight] for calls in (inside, helpers)]
        lines.append(row(name, rises[-1], *mine))
    meter = None if None in rises else sum(rises)
    lines.append(row("all", meter, inside, helpers))
    # A test's runs without a transcript are its hours' gap on the main account, which the
    # tests ran on; its runs with one are its Work's subagents, as its own chat is the Work's,
    # or have a folder of their own.
    named = [c for c in inside if c.stage == "one-off test" or (c.leg and c.work in ONE_OFF_TESTS)]
    unsaved, tests = 0.0, {c.work or TEST_WORK for c in named}
    for work, hours in ONE_OFF_TESTS.items():
        start, end = (_stamp(f"{at}+00:00") for at in hours or ("", ""))
        if not hours or end <= since or start >= until:
            continue
        start, end = max(start, since), min(end, until)
        rise = meter_rise(db_path, "main", start, end)
        if rise is None:  # no meter in its hours: left unnamed, not named at 0
            continue
        counted = sum(c.units for c in [*inside, *helpers] if c.weight == 1 and start <= c.ts < end)
        tests.add(work)
        unsaved += rise - counted
    saved, unsaved = sum(c.units for c in named), max(unsaved, 0.0)
    listed = ", ".join(sorted(work.split("/")[1] for work in tests)) or "none"
    lines.append(
        f"one-off tests {saved + unsaved:.1f}u, left in the totals: {saved:.1f}u in transcripts,"
        f" {unsaved:.1f}u without one (the gap in their hours): {listed}"
    )
    if meter is not None and works:
        lines.append(
            f"per finished Work by the meter {meter / works:.3f},"
            f" less the one-off tests {(meter - saved - unsaved) / works:.3f}"
        )
    return lines


def first_turns(chats: dict[tuple, list[Call]]) -> dict[str, float]:
    """Each Work's first model call in a chat working on it, its reviews left out."""
    first: dict[str, float] = {}
    for calls in chats.values():
        for c in calls:
            if c.work and c.stage not in ("review", "benchmark"):
                first[c.work] = min(first.get(c.work, c.ts), c.ts)
    return first


def _hours(spans: list[float]) -> str:
    """The median, p75 and p90 of some spans of time, in hours."""
    if not spans:
        return "none"
    # 19 cut points, one each 5 %: the 15th is p75 and the 18th p90
    cuts = statistics.quantiles(spans, n=20, method="inclusive") if spans[1:] else spans * 19
    return (
        f"median {statistics.median(spans) / 3600:.1f} h, p75 {cuts[14] / 3600:.1f} h,"
        f" p90 {cuts[17] / 3600:.1f} h"
    )


def per_finished_work(
    chats: dict[tuple, list[Call]],
    finished: dict[str, float],
    since: float,
    until: float,
    life_since: float | None = None,
    unread: list[str] = (),
    ends: dict[str, float] | None = None,
    helpers: list[Call] = (),
) -> list[str]:
    """The headline units per finished Work, the Works started, the finished Works' speed,
    and each finished Work's life cost.

    A Work started in the window when its first turn in the lookback fell inside it; one
    whose chats all ended before ``life_since`` cannot be seen, so a long lookback keeps
    that rare. Speed runs from a finished Work's first turn to its end in ``ends``
    (``work_ends``), or to its close in ``finished`` when it has none: a Work done with no
    pull request and no live check.

    The life cost counts calls from ``life_since`` (default: ``since``) on: a transcript is
    read whole once it changed in the lookback, so it holds older calls too. When a
    repository's closes could not be read, the finished set is short, so every figure drawn
    from it (the ratios, the life cost, each repository's row) is n/a rather than wrong.

    ``helpers`` are the pre-review helpers' runs in the window (``helper_calls``): in the
    headline, on a row of their own, and in no Work's life cost.
    """
    life_since = since if life_since is None else life_since
    inside = [c for calls in chats.values() for c in calls if since <= c.ts < until]
    helped = sum(c.units for c in helpers)
    total = sum(c.units for c in inside) + helped
    count = len(finished)

    def per(units: float) -> str:  # no Work finished, or unknown: no ratio, not a wrong one
        return f"{units / count:.3f}" if count and not unread else "n/a"

    units = collections.defaultdict(float)
    for c in inside:
        side = "bench" if c.stage == "benchmark" else "none"
        units["finished" if c.work in finished else "open" if c.work else side] += c.units
    first = first_turns(chats)
    started = sum(1 for at in first.values() if since <= at < until)
    lines = [
        f"{len(finished)} Works finished, {total:.1f} units: {per(total)} units per finished Work",
        f"  Works started {started}, {started * 7 * 86400 / (until - since):.1f} a week",
        f"  of which on them {units['finished']:.1f}u, on Works not finished in the window"
        f" {units['open']:.1f}u, review benchmark {units['bench']:.1f}u,"
        f" no Work {units['none']:.1f}u, pre-review helpers {helped:.1f}u",
        f"  as if all on one account: {per(sum(c.units / c.weight for c in [*inside, *helpers]))}"
        " per finished Work (alt's tokens not doubled, so a shift between accounts is no change)",
        f"  without calls at over {BIG_CONTEXT // 1000}k context"
        f" ({sum(c.units for c in inside if c.context > BIG_CONTEXT):.1f}u):"
        f" {per(helped + sum(c.units for c in inside if c.context <= BIG_CONTEXT))}"
        " per finished Work",
    ]
    if unread:  # the finished set is short, so nothing drawn from it is shown
        lines.append(f"  n/a: closes not read for {', '.join(unread)}, so the count is short")
        lines.append("  life cost per finished Work: n/a, and no per-repository figures")
        return lines
    ends = ends or {}
    timed, closed = {}, 0
    for work in set(finished) & set(first):
        # A check or merge after the window, or one from before this run of the Work (a
        # reopen), is not its end here: it ends at its close then, as with neither.
        end = ends.get(work, 0.0)
        fallback = not first[work] < end <= until
        end = finished[work] if fallback else end
        if end > first[work]:
            timed[work] = end - first[work]
            closed += fallback
    lines.append(
        f"  speed, first turn to last live check or merge: {_hours(list(timed.values()))}"
        f" ({len(timed)} Works, of which {closed} to their close, having neither in time;"
        f" {len(finished) - len(timed)} with no Claude turn read before it)"
    )
    life: dict[str, float] = dict.fromkeys(finished, 0.0)
    for calls in chats.values():
        for c in calls:
            if c.work in life and life_since <= c.ts < until:
                life[c.work] += c.units
    costs = sorted(life.values())
    if costs:
        cut = statistics.quantiles(costs, n=10, method="inclusive") if costs[1:] else costs
        lines.append(
            f"  life cost per finished Work: mean {statistics.mean(costs):.3f}u,"
            f" median {statistics.median(costs):.3f}u, p90 {cut[-1]:.3f}u,"
            f" with no Claude use {sum(1 for cost in costs if not cost)}"
        )
    repos: dict[str, list[float]] = collections.defaultdict(list)
    for work, cost in life.items():
        repos[work.split("#")[0]].append(cost)
    for repo, rows in sorted(repos.items(), key=lambda kv: -sum(kv[1]))[:12]:
        lines.append(
            f"  {repo:38} {len(rows):4} Works  mean {statistics.mean(rows):6.3f}u"
            f"  median {statistics.median(rows):6.3f}u  total {sum(rows):6.1f}u"
        )
    return lines


def work_at(works: list[tuple[float, str]], ts: float) -> str:
    """The Work a chat was on at a moment: the last begun, or its first before any began."""
    index = bisect.bisect_right([bound for bound, _ in works], ts) - 1
    return works[max(index, 0)][1] if works else ""


def infer_works(works: dict[str, list[tuple[float, str]]], turns: dict[str, list[tuple]]) -> None:
    """Give each chat the database ties to no Work the one its first message points to.

    A chat continued from another takes the Work the earlier chat was on when it moved,
    through any chain of moves, else the issue the message names; any other takes the Work
    its first message names (``prompt_work``).
    """
    pending: dict[str, tuple[str, str, float]] = {}
    for sid, messages in turns.items():
        if sid in works or not messages:
            continue
        earlier, issue = continued_from(messages[0][1])
        if earlier:
            pending[sid] = earlier, issue, messages[0][0]
            continue
        work = issue or prompt_work(messages[0][1])
        if work:
            works[sid] = [(0.0, work)]
    while pending:  # a chain resolves one move a pass, from its oldest end
        settled = {sid for sid, (earlier, *_) in pending.items() if earlier not in pending}
        loop = not settled  # chats naming each other: each takes only the issue it names
        for sid in settled or set(pending):
            earlier, issue, moved = pending.pop(sid)
            work = ("" if loop else work_at(works.get(earlier, []), moved)) or issue
            if work:
                works[sid] = [(0.0, work)]


def load(
    since: float, until: float, db_path: str = DB, guides: dict[str, dict] | None = None
) -> dict[tuple, list[Call]]:
    """Every chat and subagent leg active in the window, keyed by (transcript id, leg).

    ``guides``, when given, is filled with each chat's own guide text (``read_transcript``).
    """
    works = work_of_sessions(db_path)
    chats: dict[tuple, list[Call]] = {}
    turns: dict[str, list[tuple]] = {}
    bench: set[str] = set()
    tests: set[str] = set()
    seen: set = set()
    for root, weight in ACCOUNTS.values():
        for path in Path(root).expanduser().glob("projects/**/*.jsonl"):
            if path.stat().st_mtime < since:
                continue
            leg = path.stem if "subagents" in path.parts else ""
            sid = path.parts[-3] if leg else path.stem
            loaded: dict = {}
            calls, messages = read_transcript(path, weight, seen, loaded)
            chats[(sid, leg)] = calls
            if guides is not None and loaded and not leg:
                guides[sid] = loaded
            folder = path.relative_to(Path(root).expanduser()).parts[1]
            if BENCH_DIR in folder:
                bench.add(sid)
            if TEST_DIR in folder:
                tests.add(sid)
            if not leg:
                turns[sid] = sorted(messages, key=lambda turn: turn[0])
                if messages and BENCH_JUDGE.match(turns[sid][0][1]):
                    bench.add(sid)
    infer_works(works, turns)
    for (sid, leg), calls in chats.items():
        times = [turn[0] for turn in turns.get(sid, [])]
        texts = [turn[1] for turn in turns.get(sid, [])]
        stages = stage_turns(texts)
        wakes = [wake_kind(kind, text) for (_, kind), text in zip(stages, texts, strict=True)]
        for call in calls:
            index = bisect.bisect_right(times, call.ts) - 1
            call.stage, call.kind = stages[index] if index >= 0 else ("none", "none")
            call.turn, call.wake = index, wakes[index] if index >= 0 else ""
            call.work, call.leg, call.sid = work_at(works.get(sid, []), call.ts), leg, sid
            if sid in bench:
                call.stage, call.work = "benchmark", ""
            elif sid in tests:
                call.stage, call.work = "one-off test", ""
            elif not call.work and call.stage != "review":
                call.stage = "no-work"
    return chats


def table(calls: list[Call], key, limit: int = 12) -> list[str]:
    groups: dict[str, list[Call]] = collections.defaultdict(list)
    for call in calls:
        groups[key(call)].append(call)
    total = sum(call.units for call in calls) or 1
    lines = []
    for name, rows in sorted(groups.items(), key=lambda kv: -sum(c.units for c in kv[1]))[:limit]:
        units = sum(c.units for c in rows)
        tools = collections.Counter(tool for c in rows for tool in c.tools)
        top = ", ".join(tool for tool, _ in tools.most_common(3))
        lines.append(
            f"{name:40} {units:7.1f}u {units / total * 100:5.1f}%"
            f" model calls {len(rows):7} tool calls {sum(tools.values()):7}"
            f" write {sum(c.write for c in rows) / 1e6:6.1f}M"
            f" read {sum(c.read for c in rows) / 1e6:7.0f}M"
            f" out {sum(c.output for c in rows) / 1e6:5.1f}M  {top}"
        )
    return lines


def written_units(call: Call) -> dict[str, float]:
    """A call's output units by what was written. What it shows is scaled down to its real
    output when the estimate is over it, and up to it when the call did not think; the rest
    is thinking."""
    shown = sum(tokens for _, tokens in call.wrote or ())
    fit = 1.0
    if shown and (shown > call.output or not call.thought):
        fit = call.output / shown
    units: dict[str, float] = collections.defaultdict(float)
    price = call.scale * OUTPUT
    for kind, tokens in call.wrote or ():
        units[kind] += tokens * fit * price
    units[THOUGHT] += max(call.output - shown * fit, 0.0) * price
    return units


def _writer(call: Call) -> str:
    if call.stage == "review":
        return "reviews"
    if call.stage in ("benchmark", "one-off test") or not call.work or call.stage == "no-work":
        return "no Work"
    return "Work legs" if call.leg else "Work chats"


def output_split(calls: list[Call], helpers: list[Call]) -> list[str]:
    """Output units by what was written (#4754), a column per kind of chat; an estimate."""
    names = ("Work chats", "Work legs", "reviews", "no Work")
    split = {name: collections.defaultdict(float) for name in names}
    for call in calls:
        for kind, units in written_units(call).items():
            split[_writer(call)][kind] += units
    totals = {name: sum(split[name].values()) for name in names}
    lines = [f"{'':44}" + "".join(f"{name:>17}" for name in names)]
    for kind in sorted(WRITTEN, key=lambda kind: -split["Work chats"][kind]):
        cells = "".join(
            f"{split[name][kind]:9.1f}u {split[name][kind] / (totals[name] or 1) * 100:5.1f}%"
            for name in names
        )
        lines.append(f"{kind:44}{cells}")
    lines.append(f"{'all output':44}" + "".join(f"{totals[name]:9.1f}u       " for name in names))
    helpers = [call for call in helpers if call.output]  # one that never ran wrote nothing
    known = [call for call in helpers if call.wrote is not None]
    if helpers:
        helped: dict[str, float] = collections.defaultdict(float)
        for call in known:
            for kind, units in written_units(call).items():
                helped[kind] += units
        old = sum(c.output * c.scale * OUTPUT for c in helpers if c.wrote is None)
        lines.append(
            f"pre-review helpers, {len(known)} runs: reply {helped[TO_PERSON]:.1f}u,"
            f" tool calls {helped[OTHER_CALLS]:.1f}u, thinking {helped[THOUGHT]:.1f}u;"
            f" {len(helpers) - len(known)} runs logged before reply lengths: {old:.1f}u"
        )
    return lines


def _cold(call: Call) -> bool:
    """A call that wrote its context again after the cache's hour ran out."""
    return bool(
        call.gap
        and call.gap > COLD_GAP
        and call.write > 0.5 * call.context
        and call.context > 20_000
    )


def wake_table(calls: list[Call], counted: float) -> list[str]:
    """The turns the deck started by finer kind (#4707): each kind's units and share of all
    counted use, its turns and Works, the share of its turns that were light (at most 3 model
    calls, subagent legs included, and no file edit) and the share of its units that wrote a
    cold context again."""
    kinds: dict[str, dict[tuple, list[Call]]] = collections.defaultdict(dict)
    for call in calls:
        if call.wake:
            kinds[call.wake].setdefault((call.sid, call.turn), []).append(call)
    lines = [
        f"{'kind':40} {'units':>7} {'share':>6} {'turns':>6} {'Works':>6} {'u/turn':>7}"
        f" {'light':>6} {'cold':>5}"
    ]

    def row(name: str, turns: list[list[Call]]) -> str:
        units = sum(c.units for turn in turns for c in turn)
        works = {c.work for turn in turns for c in turn if c.work}
        light = sum(
            1 for turn in turns if len(turn) <= 3 and not EDITS & {t for c in turn for t in c.tools}
        )
        cold = sum(c.scale * c.write * WRITE for turn in turns for c in turn if _cold(c))
        return (
            f"{name:40} {units:6.2f}u {units / (counted or 1) * 100:5.1f}% {len(turns):6}"
            f" {len(works):6} {units / len(turns):7.3f} {light / len(turns) * 100:5.0f}%"
            f" {cold / units * 100 if units else 0:4.0f}%"
        )

    ranked = sorted(kinds.items(), key=lambda kv: -sum(c.units for t in kv[1].values() for c in t))
    lines += [row(name, list(turns.values())) for name, turns in ranked]
    if ranked:
        lines.append(
            row("all the deck started", [t for _, turns in ranked for t in turns.values()])
        )
    return lines


def context_split(calls: list[Call]) -> list[str]:
    """What a Work chat's call holds on average, by part, in all its stages and in three of
    them (#4714). A chat's own calls only: no subagent legs, reviews or one-off tests."""
    mine = [
        c
        for c in calls
        if c.parts
        and not c.leg
        and c.work
        and c.stage not in NOT_WORK
        and c.work not in ONE_OFF_TESTS
    ]
    columns = [("all stages", mine)]
    columns += [
        (stage, [c for c in mine if c.stage == stage]) for stage in ("build", "fix", "release")
    ]
    lines = [
        f"{'':40}" + "".join(f" {name + f' ({len(rows)} calls)':>24}" for name, rows in columns)
    ]
    for index, part in enumerate([*PARTS, "a call's whole context"]):
        line = f"{part:40}"
        for _, rows in columns:
            whole = sum(sum(c.parts) for c in rows)
            held = whole if index == len(PARTS) else sum(c.parts[index] for c in rows)
            size = held / len(rows) / 1000 if rows else 0.0
            line += f" {size:15.1f}k {held / (whole or 1) * 100:6.1f}%"
        lines.append(line)
    return lines


def restarts(calls: list[Call]) -> list[str]:
    """Work chats' restarts from a summary (#4766): how many, per 100 calls, and what they
    cost, by account and by day. A restart is the first call after a compaction. Its cost is
    the summarising call, which no transcript keeps and is priced from the tokens it read
    and the summary it wrote (an estimate, outside the counted units), and the new context
    the first call after it writes. A chat's own calls only, as in ``context_split``."""
    mine = [
        c
        for c in calls
        if not c.leg and c.work and c.stage not in NOT_WORK and c.work not in ONE_OFF_TESTS
    ]
    names = {weight: name for name, (_, weight) in ACCOUNTS.items()}
    groups: dict[str, list[Call]] = collections.defaultdict(list)
    for call in mine:
        groups["all"].append(call)
        groups[names.get(call.weight, str(call.weight))].append(call)
        groups[datetime.fromtimestamp(call.ts, UTC).strftime("%Y-%m-%d")].append(call)
    lines = [
        f"{'':12}{'restarts':>9}{'calls':>9}{'/100':>7}{'summary':>10}{'rewrite':>10}"
        f"{'together':>10}{'share':>7}"
    ]
    for name in ["all", *names.values(), *sorted(k for k in groups if k[:1].isdigit())]:
        rows = groups.get(name, [])
        if not rows and name != "all":
            continue
        after = [c for c in rows if c.after_compact]
        summary = sum(c.scale * (c.restart[0] * READ + c.restart[1] * OUTPUT)
                      for c in after if c.restart)  # fmt: skip
        rewrite = sum(c.scale * c.write * WRITE for c in after)
        units = sum(c.units for c in rows)
        lines.append(
            f"{name:12}{len(after):9}{len(rows):9}{len(after) / (len(rows) or 1) * 100:7.2f}"
            f"{summary:9.1f}u{rewrite:9.1f}u{summary + rewrite:9.1f}u"
            f"{(summary + rewrite) / (units or 1) * 100:6.1f}%"
        )
    return lines


def work_pulls(db_path: str, chats: dict[tuple, list[Call]]) -> dict[str, str]:
    """Each pull request a chat registered, with the Work it was for: the one the registration
    names, else the Work most of that chat's calls were on. Read from the deck's database
    alone, so it holds every pull request ever registered, not only the window's."""
    path = Path(db_path).expanduser()
    if not path.exists():
        return {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "select session_key, repository, number, work_id from session_pull_requests"
            " order by rowid"
        ).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):  # a locked database is not an empty one
            raise
        return {}  # a database from before the table
    mostly: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for calls in chats.values():
        for call in calls:
            if call.work:
                mostly[call.sid][call.work] += 1
    # two chats can register one pull request: a Work a registration names beats a guess from
    # the other chat's calls, and the earliest row of each kind wins, whatever order they came in
    named: dict[str, str] = {}
    guessed: dict[str, str] = {}
    for key, repo, number, work in rows:
        pull = f"{repo}#{number}".lower()
        if ref := str(work or "").removeprefix("github_issue:").lower():
            named.setdefault(pull, ref)
        elif seen := mostly.get(str(key).split(":")[-1]):
            guessed.setdefault(pull, seen.most_common(1)[0][0])
    return guessed | named


def heaviest_works(
    calls: list[Call], helpers: list[Call], pulls: dict[str, str], counted: float, limit: int = 35
) -> list[str]:
    """The Works that used the most in the window (#4728): each one's units with its
    pre-review helpers' runs, its share of all counted use, the pull requests its chats ever
    registered, units a pull request and its turns. A series of pull requests under one issue
    shows as many pull requests at few units each. A reviewer's calls on a Work count in its
    units, as in #4719, but its turns are the builder's alone."""
    units: collections.Counter = collections.Counter()
    helped: collections.Counter = collections.Counter()
    turns: dict[str, set] = collections.defaultdict(set)
    for call in calls:
        if call.work:
            units[call.work] += call.units
            if call.stage != "review":
                turns[call.work].add((call.sid, call.turn))
    for call in helpers:
        if work := pulls.get(call.pull):
            units[work] += call.units
            helped[work] += call.units
    count = collections.Counter(pulls.values())
    lines = [
        f"{'Work':40} {'units':>7} {'share':>6} {'pulls':>6} {'u/pull':>7} {'turns':>6}"
        f" {'helpers':>8}"
    ]
    ranked = units.most_common()
    for work, used in ranked[:limit]:
        each = f"{used / count[work]:7.2f}" if count[work] else f"{'-':>7}"
        lines.append(
            f"{work:40} {used:6.2f}u {used / (counted or 1) * 100:5.1f}% {count[work]:6} {each}"
            f" {len(turns[work]):6} {helped[work]:7.2f}u"
        )
    top = sum(used for _, used in ranked[:limit])
    rest = sum(used for _, used in ranked[limit:])
    lines.append(
        f"these {len(ranked[:limit])} Works {top:.1f}u = {top / (counted or 1) * 100:.1f}% of"
        f" counted use; {len(ranked[limit:])} more Works {rest:.1f}u"
    )
    return lines


def _guide_name(path: str) -> str:
    """A guide file's path without the home it is under or the worktree it was read in."""
    path = re.sub(r"^(/var)?/home/[^/]+", "~", path)
    return re.sub(r"/\.worktrees/[^/]+/", "/…/", path)


def guide_sizes(
    chats: dict[tuple, list[Call]], guides: dict[str, dict], since: float, until: float
) -> list[str]:
    """The guide text each repository's Work chats loaded at their start (#4714): the guide
    files' characters in all and file by file, and the deck's session guide's. A chat counts
    for the repository of the first Work it was on in the window."""
    repos: dict[str, list[dict]] = collections.defaultdict(list)
    for sid, loaded in guides.items():
        works = [
            c.work
            for c in chats.get((sid, ""), [])
            if since <= c.ts < until and c.work and c.stage not in NOT_WORK
        ]
        if works:
            repos[works[0].split("#")[0]].append(loaded)
    lines = []
    ranked_repos = sorted(repos.items(), key=lambda kv: -len(kv[1]))
    for repo, loads in ranked_repos[:9]:
        totals = [sum(load["files"].values()) for load in loads if load.get("files")]
        session = [load["session"] for load in loads if load.get("session")]
        files_median = statistics.median(totals) if totals else 0
        session_median = statistics.median(session) if session else 0
        lines.append(
            f"{repo:38} chats {len(loads):4}  guide files median {files_median:7.0f} chars"
            f" = about {files_median / GUIDE_CPT / 1000:.1f}k tokens"
            f"  session guide median {session_median:6.0f} chars"
        )
        sizes: dict[str, list[int]] = collections.defaultdict(list)
        for load in loads:
            for path, chars in load.get("files", {}).items():
                sizes[_guide_name(path)].append(chars)
        ranked = sorted(sizes.items(), key=lambda kv: -statistics.median(kv[1]) * len(kv[1]))
        for name, found in ranked[:6]:
            lines.append(
                f"    {name:64} in {len(found):4} chats,"
                f" median {statistics.median(found):7.0f} chars"
            )
        if ranked[6:]:
            lines.append(f"    and {len(ranked) - 6} smaller files")
    if ranked_repos[9:]:
        lines.append(f"and {len(ranked_repos) - 9} repositories with fewer chats")
    return lines


def replay_window(chats: dict[tuple, list[Call]], since: float, until: float, window: int) -> float:
    """Units an earlier compaction would save, against never compacting."""
    saved = 0.0
    for (_, leg), calls in chats.items():
        if leg:
            continue
        offset = 0
        for call in calls:
            if not since <= call.ts < until:
                continue
            if call.after_compact:  # the real chat just shrank; nothing simulated is left
                offset = 0
            if call.context - offset > window:
                saved -= call.scale * (
                    AFTER_COMPACT * WRITE + 6000 * OUTPUT + (call.context - offset) * READ
                )
                offset = call.context - AFTER_COMPACT
            saved += call.scale * min(offset, call.read) * READ
    return saved


def ideas(chats: dict[tuple, list[Call]], since: float, until: float) -> list[str]:
    inside = [c for calls in chats.values() for c in calls if since <= c.ts < until]
    total = sum(c.units for c in inside) or 1

    def share(units: float) -> str:
        return f"{units:.1f}u = {units / total * 100:.1f}%"

    lines = [
        f"window {size}: saves {share(replay_window(chats, since, until, size))}"
        for size in (300_000, 200_000, 166_000, 120_000, 100_000)
    ]
    first = [c[0] for c in chats.values() if c and since <= c[0].ts < until]
    lines.append(
        f"fresh chats and legs {len(first)}: median first call"
        f" {statistics.median(c.context for c in first or [Call(0, 1)]):.0f}."
        f" 10k less standing prompt saves"
        f" {share(sum(c.scale * 10_000 * READ for c in inside))} reads +"
        f" {share(sum(c.scale * 10_000 * WRITE for c in first))} writes"
    )
    cold = [c for c in inside if _cold(c)]
    lines.append(
        f"cold wakes {len(cold)}: {share(sum(c.scale * c.write * WRITE for c in cold))},"
        f" not started by a person"
        f" {share(sum(c.scale * c.write * WRITE for c in cold if c.kind != 'person'))}"
    )
    # What the pings that keep waiting chats warm cost (#4737): they pay off only while
    # the cold wakes' line above fell by more than this since they began.
    pings = [c for c in inside if c.kind == "keep_warm"]
    lines.append(
        f"keep-warm pings {len({(c.sid, c.turn) for c in pings})}:"
        f" {share(sum(c.units for c in pings))}"
    )
    legs = [c for c in inside if c.leg]
    lines.append(f"subagent legs: {share(sum(c.units for c in legs))}")
    pairs = 0
    second = 0.0
    for calls in chats.values():
        for one, two in zip(calls, calls[1:], strict=False):
            if since <= two.ts < until and two.gap and two.gap < 120 and not one.text:
                if _read_only(one) and _read_only(two):
                    pairs += 1
                    second += two.scale * two.read * READ
    lines.append(f"back-to-back read-only calls {pairs}: {share(second)}")
    lines.append(f"turns the deck woke: {share(sum(c.units for c in inside if c.kind in WOKEN))}")
    results = [result for c in inside for result in c.results]
    long = [name for name, chars in results if chars > BASH_OUTPUT_CHARS]
    lines.append(
        f"tool results {len(results)}: over {BASH_OUTPUT_CHARS} characters {len(long)}"
        f" = {len(long) / (len(results) or 1) * 100:.1f}%,"
        f" from the shell {sum(name.startswith('Bash') for name in long)}"
    )
    return lines


def _read_only(call: Call) -> bool:
    return len(call.tools) == 1 and call.tools[0] in ("Read", "Bash: read/search")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True, help="ISO time, UTC")
    parser.add_argument("--until", required=True, help="ISO time, UTC")
    parser.add_argument("--db", default=DB)
    parser.add_argument(
        "--lookback", type=float, default=30, help="days before --since a Work's life cost reads"
    )
    args = parser.parse_args()
    since, until = (
        datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()
        for value in (args.since, args.until)
    )
    life_since = since - args.lookback * 86400
    guides: dict[str, dict] = {}
    chats = load(life_since, until, args.db, guides)
    inside = [c for calls in chats.values() for c in calls if since <= c.ts < until]
    work = [c for c in inside if c.work and c.stage not in ("no-work", "review")]
    tools = collections.Counter(tool for c in inside for tool in c.tools)
    helpers, begins = helper_calls(usage_log_path(), since, until)
    print(f"{len(inside)} model calls, {sum(tools.values())} tool calls,")
    print(f"{sum(c.units for c in [*inside, *helpers]):.0f} units\n\nPer finished Work")
    finished, unread = finished_works(args.db, since, until)
    ends = work_ends(args.db)
    per_work = per_finished_work(chats, finished, since, until, life_since, unread, ends, helpers)
    print("\n".join(per_work))
    print("\nAccounts' meters")
    if begins is None or begins > since:
        start = datetime.fromtimestamp(begins, UTC).strftime("%Y-%m-%d %H:%M UTC") if begins else ""
        print(f"helpers before their log began are in the gap (it begins: {start or 'never'})")
    works = 0 if unread else len(finished)
    print("\n".join(meter_check(args.db, inside, helpers, since, until, works)))
    counted = sum(c.units for c in [*inside, *helpers])
    print("\nBy stage (a pre-review helper's whole run counts as one model call)")
    print("\n".join(table([*inside, *helpers], lambda c: c.stage)))
    print("\nBy Work")
    print("\n".join(table(work, lambda c: c.work)))
    print("\nHeaviest Works (units in the window with their reviews and helpers; pulls are all a")
    print("Work's chats ever registered, so u/pull reads low for a Work begun before the window)")
    linked = [c for c in inside if c.work and c.stage != "no-work"]  # with its reviews
    print("\n".join(heaviest_works(linked, helpers, work_pulls(args.db, chats), counted)))
    print("\nOutput by what was written (an estimate; share is of the column's output)")
    print("\n".join(output_split(inside, helpers)))
    print("\nBy what started the turn")
    print("\n".join(table(inside, lambda c: c.kind)))
    print("\nTurns the deck started, by kind (share is of all counted use)")
    print("\n".join(wake_table(inside, counted)))
    print("\nWork chats' restarts from a summary (summary: the summarising call, an estimate")
    print("outside the counted units; rewrite: the next call's new context; share of the row's")
    print("Work-chat units)")
    print("\n".join(restarts(inside)))
    print("\nWhat a Work chat's call holds (average tokens, an estimate; share of its context)")
    print("\n".join(context_split(inside)))
    print("\nGuide text at a chat's start, by repository")
    print("\n".join(guide_sizes(chats, guides, since, until)))
    print("\nTool calls by kind")
    print("\n".join(f"{count:7} {tool}" for tool, count in tools.most_common(14)))
    print("\nIdeas")
    print("\n".join(ideas(chats, since, until)))


if __name__ == "__main__":
    main()
