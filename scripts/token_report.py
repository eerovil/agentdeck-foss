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
live from GitHub's closed-issue list of each repository with a Work) and at least one
agent chat worked on it (the deck's ``works`` table). The
headline divides every unit spent in the window, chats with no Work, reviews and
Works still open included, by the Works finished in it, so no use is left out and
shared use is spread evenly. The life cost below it sums each finished Work's own
chats, subagent legs and review chats over its whole life (``--lookback`` days).
Baseline, Nov 3-10 2026: see docs/research/token-use-2026-11.md.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import json
import re
import sqlite3
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentdeck import kanban  # noqa: E402
from agentdeck.chat_use import INPUT, OUTPUT, READ, WRITE  # noqa: E402, F401
from agentdeck.deck_notices import without_routine  # noqa: E402
from agentdeck.models import IssueRef  # noqa: E402

ACCOUNTS = {"main": ("~/.claude", 1), "alt": ("~/.claude2", 2)}
DB = "~/.local/share/agentdeck/agentdeck.db"
COLD_GAP = 3600  # the cache lives an hour, so a longer pause writes the context again
AFTER_COMPACT = 62_000  # median context measured right after a compaction
BIG_CONTEXT = 200_000  # past this a chat should have compacted; #4059 made it so on Nov 3

TRIGGERS = (
    ("reviewer", r"^You are AgentDeck's Claude (fallback )?reviewer"),
    ("dream", r"^DREAM-AGENT"),
    ("start", r"^Context:"),
    ("spec_ok", r"^I approve the spec"),
    ("job", r"^Your queued job"),
    ("ci_wake", r"^AgentDeck wake-on: (commit status|check run|nothing arrived)"),
    ("wake", r"^AgentDeck wake-on"),
    ("review_pass", r"/reviewed pass|^AgentDeck: the review of .*passed clean"),
    (
        "review_result",
        r"^AgentDeck event: the reviewer|^AgentDeck: the deck's reviewer asks"
        r"|^AgentDeck: the review of",
    ),
    ("checklist", r"^AgentDeck: before "),
    ("merged", r"^AgentDeck: \S+ merged"),
    ("upkeep", r"^AgentDeck: (\S+ was behind|\S+ is now at|ready for merge|your last turn)"),
    ("deck", r"^AgentDeck"),
    ("task_note", r"^<task-notification>"),
)
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
    r"(?:for the Work|asks for a review of) ([\w.-]+/[\w.-]+#\d+)",
    re.S,
)


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


def trigger(text: str) -> str:
    """What started a turn, from the first words of its message; a success notice's routine
    line (#4253) is read past, so the notice counts as the kind it was before."""
    head = without_routine(text)[:300]
    for name, pattern in TRIGGERS:
        if re.search(pattern, head, re.S):
            return name
    return "person"


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
    for kind in kinds:
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
    text: int = 0
    stage: str = "none"
    kind: str = "none"
    work: str = ""
    leg: str = ""
    sid: str = ""

    @property
    def context(self) -> int:
        return self.fresh + self.write + self.read

    @property
    def units(self) -> float:
        return self.weight * (
            self.fresh * INPUT + self.write * WRITE + self.read * READ + self.output * OUTPUT
        )


def _stamp(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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


def read_transcript(path: Path, weight: int, seen: set) -> tuple[list[Call], list[tuple]]:
    """A transcript's calls, and its turns as (time, message). Skips what it cannot read."""
    calls: dict[tuple, Call] = {}
    turns: list[tuple] = []
    last = None
    compacted = False
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
            if ts is None:
                continue
            if row.get("type") == "user":
                content = message.get("content")
                if row.get("isCompactSummary"):
                    compacted = True
                elif not row.get("isMeta") and not (
                    isinstance(content, list)
                    and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                ):
                    turns.append((ts, _text(content)))
            elif row.get("type") == "assistant" and message.get("usage"):
                if message.get("model") == "<synthetic>":
                    continue
                usage = message["usage"]
                key = (message.get("id"), row.get("requestId"))
                call = calls.get(key)
                if call is None:
                    if key in seen:  # a resumed chat copies its history into the new file
                        continue
                    seen.add(key)
                    call = calls[key] = Call(
                        ts,
                        weight,
                        usage.get("input_tokens") or 0,
                        usage.get("cache_creation_input_tokens") or 0,
                        usage.get("cache_read_input_tokens") or 0,
                        gap=ts - last if last else None,
                        after_compact=compacted,
                    )
                    compacted = False
                call.output = max(call.output, usage.get("output_tokens") or 0)
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        name = block.get("name") or "?"
                        if name == "Bash":
                            name = shell_kind(str((block.get("input") or {}).get("command", "")))
                        call.tools.append("mcp" if name.startswith("mcp__") else name)
                    elif block.get("type") == "text":
                        call.text += len(block.get("text", ""))
                last = ts
    return list(calls.values()), turns


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


def _gh_rows(path: str, query: str) -> list[list[str]]:
    """Every page of one GitHub REST list, as tab-separated rows."""
    found = subprocess.run(
        # the path first: the gh shim picks whose token speaks from the argument after "api"
        ["gh", "api", path, "--paginate", "-q", query],
        capture_output=True, text=True, timeout=300, check=True,
    )  # fmt: skip
    return [line.split("\t") for line in found.stdout.splitlines()]


def closed_at_end(events: list[tuple[str, float]], since: float, until: float) -> bool:
    """Whether an issue stood closed at the window's end by a close inside the window."""
    last = max(((at, event) for event, at in events if at < until), default=None)
    return last is not None and last[1] == "closed" and last[0] >= since


def github_closes(
    repository: str, since: float, until: float, numbers: set[str] | None = None
) -> set[str]:
    """The repository's issues finished in the window, as ``owner/repo#n``, lowercased.

    Finished means closed at the window's end by a close inside it, as it stood then: a
    reopen after the window does not take a close out of its week. Read from GitHub itself,
    so every close counts whether or not the deck ever cached that issue. Every issue updated
    since the window opened is listed (a close updates an issue, so none is missed); one whose
    latest close is inside the window is settled by that, and only one reopened since, or
    closed after the window, has its close and reopen events read. ``numbers`` limits it to
    those issues. Pull requests are left out. The search API is not used: it caps a query at
    1000 results and cannot see some organisations' repositories.
    """
    # GitHub lists issues updated strictly after ``since``: start a second early, so a
    # close exactly at the window's start with nothing after it is still listed
    opened = datetime.fromtimestamp(since - 1, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = _gh_rows(
        f"repos/{repository}/issues?state=all&since={opened}&per_page=100",
        ".[] | select(.pull_request | not)"
        ' | [.number, .state, .closed_at // "", .state_reason // ""] | @tsv',
    )
    finished = set()
    for row in rows:
        number, state, at, reason = (row + ["", "", "", ""])[:4]
        if not number.isdigit() or (numbers is not None and number not in numbers):
            continue
        when = _stamp(at)
        if state == "closed" and when is not None and when < until:
            settled = since <= when
        elif state == "open" and reason != "reopened":  # never closed, so not in the window
            settled = False
        else:  # reopened, or closed after the window: was it closed at the window's end?
            events = _gh_rows(
                f"repos/{repository}/issues/{number}/events?per_page=100",
                '.[] | select(.event == "closed" or .event == "reopened")'
                " | [.event, .created_at] | @tsv",
            )
            stamped = [(e[0], _stamp(e[1])) for e in events if len(e) == 2 and _stamp(e[1])]
            settled = closed_at_end(stamped, since, until)
        if settled:
            finished.add(f"{repository.lower()}#{number}")
    return finished


def finished_works(
    db_path: str, since: float, until: float, closes=github_closes
) -> tuple[set[str], list[str]]:
    """Works whose issue closed in the window, from GitHub itself.

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
    finished, unread = set(), []
    for repository in sorted({work.split("#")[0] for work in works}):
        numbers = {work.split("#")[1] for work in works if work.split("#")[0] == repository}
        try:
            finished |= closes(repository, since, until, numbers) & works
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", "") or exc
            print(f"no closes read for {repository}: {str(detail)[:200]}", file=sys.stderr)
            unread.append(repository)
    return finished, unread


def per_finished_work(
    chats: dict[tuple, list[Call]],
    finished: set[str],
    since: float,
    until: float,
    life_since: float | None = None,
    unread: list[str] = (),
) -> list[str]:
    """The headline units per finished Work, and each finished Work's life cost.

    The life cost counts calls from ``life_since`` (default: ``since``) on: a transcript is
    read whole once it changed in the lookback, so it holds older calls too. When a
    repository's closes could not be read, the finished set is short, so every figure drawn
    from it (the ratios, the life cost, each repository's row) is n/a rather than wrong.
    """
    life_since = since if life_since is None else life_since
    inside = [c for calls in chats.values() for c in calls if since <= c.ts < until]
    total = sum(c.units for c in inside)
    count = len(finished)

    def per(units: float) -> str:  # no Work finished, or unknown: no ratio, not a wrong one
        return f"{units / count:.3f}" if count and not unread else "n/a"

    units = collections.defaultdict(float)
    for c in inside:
        units["finished" if c.work in finished else "open" if c.work else "none"] += c.units
    lines = [
        f"{len(finished)} Works finished, {total:.1f} units: {per(total)} units per finished Work",
        f"  of which on them {units['finished']:.1f}u, on Works not finished in the window"
        f" {units['open']:.1f}u, no Work {units['none']:.1f}u",
        f"  as if all on one account: {per(sum(c.units / c.weight for c in inside))}"
        " per finished Work (alt's tokens not doubled, so a shift between accounts is no change)",
        f"  without calls at over {BIG_CONTEXT // 1000}k context"
        f" ({sum(c.units for c in inside if c.context > BIG_CONTEXT):.1f}u):"
        f" {per(sum(c.units for c in inside if c.context <= BIG_CONTEXT))}"
        " per finished Work",
    ]
    if unread:  # the finished set is short, so nothing drawn from it is shown
        lines.append(f"  n/a: closes not read for {', '.join(unread)}, so the count is short")
        lines.append("  life cost per finished Work: n/a, and no per-repository figures")
        return lines
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


def load(since: float, until: float, db_path: str = DB) -> dict[tuple, list[Call]]:
    """Every chat and subagent leg active in the window, keyed by (transcript id, leg)."""
    works = work_of_sessions(db_path)
    chats: dict[tuple, list[Call]] = {}
    turns: dict[str, list[tuple]] = {}
    seen: set = set()
    for root, weight in ACCOUNTS.values():
        for path in Path(root).expanduser().glob("projects/**/*.jsonl"):
            if path.stat().st_mtime < since:
                continue
            leg = path.stem if "subagents" in path.parts else ""
            sid = path.parts[-3] if leg else path.stem
            calls, messages = read_transcript(path, weight, seen)
            chats[(sid, leg)] = calls
            if not leg:
                turns[sid] = sorted(messages, key=lambda turn: turn[0])
    for sid, messages in turns.items():
        if sid in works or not messages:
            continue
        work = prompt_work(messages[0][1])
        if work:
            works[sid] = [(0.0, work)]
    for (sid, leg), calls in chats.items():
        times = [turn[0] for turn in turns.get(sid, [])]
        stages = stage_turns([turn[1] for turn in turns.get(sid, [])])
        for call in calls:
            index = bisect.bisect_right(times, call.ts) - 1
            call.stage, call.kind = stages[index] if index >= 0 else ("none", "none")
            call.work, call.leg, call.sid = work_at(works.get(sid, []), call.ts), leg, sid
            if not call.work and call.stage != "review":
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
                saved -= call.weight * (
                    AFTER_COMPACT * WRITE + 6000 * OUTPUT + (call.context - offset) * READ
                )
                offset = call.context - AFTER_COMPACT
            saved += call.weight * min(offset, call.read) * READ
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
        f" {share(sum(c.weight * 10_000 * READ for c in inside))} reads +"
        f" {share(sum(c.weight * 10_000 * WRITE for c in first))} writes"
    )
    cold = [
        c
        for c in inside
        if c.gap and c.gap > COLD_GAP and c.write > 0.5 * c.context and c.context > 20_000
    ]
    lines.append(
        f"cold wakes {len(cold)}: {share(sum(c.weight * c.write * WRITE for c in cold))},"
        f" not started by a person"
        f" {share(sum(c.weight * c.write * WRITE for c in cold if c.kind != 'person'))}"
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
                    second += two.weight * two.read * READ
    lines.append(f"back-to-back read-only calls {pairs}: {share(second)}")
    lines.append(f"turns the deck woke: {share(sum(c.units for c in inside if c.kind in WOKEN))}")
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
    chats = load(life_since, until, args.db)
    inside = [c for calls in chats.values() for c in calls if since <= c.ts < until]
    work = [c for c in inside if c.work and c.stage not in ("no-work", "review")]
    tools = collections.Counter(tool for c in inside for tool in c.tools)
    print(f"{len(inside)} model calls, {sum(tools.values())} tool calls,")
    print(f"{sum(c.units for c in inside):.0f} units\n\nPer finished Work")
    finished, unread = finished_works(args.db, since, until)
    print("\n".join(per_finished_work(chats, finished, since, until, life_since, unread)))
    print("\nBy stage")
    print("\n".join(table(inside, lambda c: c.stage)))
    print("\nBy Work")
    print("\n".join(table(work, lambda c: c.work)))
    print("\nBy what started the turn")
    print("\n".join(table(inside, lambda c: c.kind)))
    print("\nTool calls by kind")
    print("\n".join(f"{count:7} {tool}" for tool, count in tools.most_common(14)))
    print("\nIdeas")
    print("\n".join(ideas(chats, since, until)))


if __name__ == "__main__":
    main()
