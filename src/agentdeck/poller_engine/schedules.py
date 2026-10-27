"""Scheduled work: an issue that files a fresh card on a repeating schedule (#2517).

A *task* is an ordinary issue in one of the project's issue repositories that carries the
``scheduled`` label, a ``When:`` line written in plain words, and a prompt. Every time the
schedule comes round the engine files a new card in the same repository, labelled with the
ownership label so a normal worker picks it up, and linking back to the task. The task issue is
the whole interface: editing its ``When:`` line or prompt changes the task, closing it stops the
task, reopening it starts it again. Nothing about a task lives anywhere a person cannot see.

This module is the pure half: reading a task's body, understanding its ``When:`` line, working
out the next run, and writing the text the engine posts. It does no I/O and reads no clock —
the cycle passes ``now`` in — so every rule here is testable with plain values.

Times are Europe/Helsinki wall-clock times. A schedule is a promise about the clock on the
requester's wall, so ``every Monday 06:00`` stays at six in the morning across a daylight-saving
change rather than drifting to five or seven.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

LABEL = "scheduled"
TIMEZONE = "Europe/Helsinki"
ZONE = ZoneInfo(TIMEZONE)

# Every card this engine files for a run carries this marker, so an interrupted creation can be
# recognised again rather than filed twice (the #393 pattern).
RUN_MARKER = "<!-- poller-scheduled-run: {key} {occurrence} -->"
LIST_MARKER = "<!-- poller-scheduled-list -->"
LIST_TITLE = "Scheduled tasks"

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY_WORDS = {
    **{name: index for index, name in enumerate(WEEKDAYS)},
    **{name[:3]: index for index, name in enumerate(WEEKDAYS)},
    **{f"{name}s": index for index, name in enumerate(WEEKDAYS)},
    "tues": 1, "wednes": 2, "thur": 3, "thurs": 3,
}
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10,
}
_MONTHS = (
    "", "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December",
)
_TIME = re.compile(r"\b(?:at\s+)?([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_WHEN = re.compile(r"^\W*when\W*:\W*(.*?)\W*$", re.IGNORECASE)
_PROMPT_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(prompt|task|what to do)\b.*$", re.IGNORECASE)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
# How far ahead `next_after` searches. A year and a bit covers every schedule this grammar can
# express (the rarest is a monthly day 31, which recurs within two months).
_SEARCH_DAYS = 400

EXAMPLES = "`every Monday 06:00`, `every day 07:30`, `first day of the month 08:00`"


@dataclass(frozen=True)
class Schedule:
    """One understood ``When:`` line.

    ``weekdays`` (Monday is 0) is set for a weekly schedule; ``month_day`` for a monthly one,
    where ``-1`` means the month's last day and a day the month does not have (31 in April)
    falls on that month's last day. Neither set means every day.
    """

    hour: int
    minute: int
    weekdays: tuple[int, ...] = ()
    month_day: int = 0

    def matches(self, day: date) -> bool:
        if self.month_day:
            last = calendar.monthrange(day.year, day.month)[1]
            wanted = last if self.month_day == -1 else min(self.month_day, last)
            return day.day == wanted
        if self.weekdays:
            return day.weekday() in self.weekdays
        return True

    def next_after(self, moment: datetime) -> datetime:
        """The first run strictly after ``moment``, as an aware UTC instant."""
        local = moment.astimezone(ZONE)
        day = local.date()
        for _ in range(_SEARCH_DAYS):
            if self.matches(day):
                candidate = datetime.combine(day, time(self.hour, self.minute), tzinfo=ZONE)
                if candidate > moment:
                    return candidate.astimezone(UTC)
            day += timedelta(days=1)
        raise ValueError("no run within a year")  # unreachable for any parsed schedule

    def describe(self) -> str:
        clock = f"{self.hour:02d}:{self.minute:02d}"
        if self.month_day == -1:
            return f"last day of every month at {clock}"
        if self.month_day:
            return f"day {self.month_day} of every month at {clock}"
        if self.weekdays == (0, 1, 2, 3, 4):
            return f"every weekday (Monday to Friday) at {clock}"
        if self.weekdays:
            names = [WEEKDAYS[index].capitalize() for index in self.weekdays]
            joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            return f"every {joined} at {clock}"
        return f"every day at {clock}"

    def run_label(self, occurrence: datetime) -> str:
        """What distinguishes one run's card from the next: its week, day or month."""
        local = occurrence.astimezone(ZONE)
        if self.month_day:
            return f"{_MONTHS[local.month]} {local.year}"
        if len(self.weekdays) == 1:
            return f"week {local.isocalendar().week}"
        return local.date().isoformat()


def when_line(body: str) -> str | None:
    """The text after the first ``When:`` line, or None when the body has none.

    Tolerant of how people write it in Markdown: ``When: …``, ``**When:** …``, ``- When: …``.
    """
    for line in (body or "").splitlines():
        found = _WHEN.match(line.strip())
        if found:
            return found.group(1).strip()
    return None


def parse_when(text: str) -> Schedule | str:
    """A ``Schedule``, or a sentence saying what could not be understood."""
    raw = " ".join(str(text or "").replace(",", " ").split()).lower()
    if not raw:
        return "the When line is empty"
    times = _TIME.findall(raw)
    if not times:
        return f"no time of day in `{text}` — write it as HH:MM, for example 06:00"
    if len(times) > 1:
        return f"more than one time of day in `{text}` — one task runs at one time"
    hour, minute = int(times[0][0]), int(times[0][1])
    words = _TIME.sub(" ", raw).split()
    words = [word for word in words if word not in {"at", "on", "the", "of", "every", "each"}]
    rest = " ".join(words)

    if rest in {"day", "daily", "days"}:
        return Schedule(hour, minute)
    if rest in {"weekday", "weekdays", "workday", "workdays"}:
        return Schedule(hour, minute, weekdays=(0, 1, 2, 3, 4))
    monthly = _month_day(words)
    if monthly is not None:
        return monthly if isinstance(monthly, str) else Schedule(
            hour, minute, month_day=monthly
        )
    days = []
    for word in words:
        if word in {"and", "week", "weekly"}:
            continue
        if word not in _DAY_WORDS:
            return f"I do not understand `{word}` in `{text}`. Examples: {EXAMPLES}"
        days.append(_DAY_WORDS[word])
    if not days:
        return f"no day in `{text}`. Examples: {EXAMPLES}"
    return Schedule(hour, minute, weekdays=tuple(sorted(set(days))))


def _month_day(words: list[str]) -> int | str | None:
    """The day of a monthly schedule, an error, or None when this is not a monthly schedule."""
    if not any(word in {"month", "monthly", "months"} for word in words):
        return None
    rest = [word for word in words if word not in {"month", "monthly", "months", "day"}]
    if rest == ["last"]:
        return -1
    if len(rest) == 1:
        word = rest[0]
        if word in _ORDINALS:
            return _ORDINALS[word]
        digits = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?", word)
        if digits and 1 <= int(digits.group(1)) <= 31:
            return int(digits.group(1))
    return (
        "I could not tell which day of the month. Write for example "
        "`first day of the month 08:00`, `day 15 of the month 08:00` or "
        "`last day of the month 08:00`"
    )


def prompt_section(body: str) -> str:
    """What the run's worker is asked to do.

    The section under a ``## Prompt`` heading when there is one, up to the next heading;
    otherwise the whole body without its ``When:`` line, so a short task needs no headings.
    Engine markers are left out either way.
    """
    lines = [line for line in (body or "").splitlines() if "<!-- poller-" not in line]
    for index, line in enumerate(lines):
        if _PROMPT_HEADING.match(line):
            section = []
            for following in lines[index + 1:]:
                if _HEADING.match(following):
                    break
                section.append(following)
            return "\n".join(section).strip()
    return "\n".join(line for line in lines if not _WHEN.match(line.strip())).strip()


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def parse_stamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def local_text(moment: datetime) -> str:
    """``Monday 5 October 2026 at 06:00`` in Helsinki time."""
    local = moment.astimezone(ZONE)
    return (
        f"{WEEKDAYS[local.weekday()].capitalize()} {local.day} {_MONTHS[local.month]} "
        f"{local.year} at {local:%H:%M}"
    )


def run_title(task_title: str, schedule: Schedule, occurrence: datetime) -> str:
    title = f"{task_title.strip() or 'Scheduled task'} — {schedule.run_label(occurrence)}"
    return title if len(title) <= 200 else title[:199] + "…"


def run_body(*, number: int, prompt: str, occurrence: datetime, marker: str) -> str:
    return (
        f"Scheduled run of #{number}, due {local_text(occurrence)} "
        f"({TIMEZONE}).\n\n"
        f"{prompt.strip() or '(The task has no prompt. Read the task issue.)'}\n\n"
        "Report the results on this card. Change or stop the schedule on the task issue, "
        "not here.\n\n"
        f"{marker}"
    )


def scheduled_note(schedule: Schedule, next_run: datetime) -> str:
    return (
        f"Scheduled: {schedule.describe()} ({TIMEZONE}). "
        f"Next run: **{local_text(next_run)}**.\n\n"
        "Each run files a new card that a worker picks up. Edit the `When:` line or the "
        "prompt to change this task, close the issue to stop it, reopen it to start it again."
    )


def unreadable_note(problem: str) -> str:
    return (
        f"This task is not scheduled: {problem}.\n\n"
        f"Put one line like `When: every Monday 06:00` in the issue body. Examples: {EXAMPLES}. "
        f"Times are {TIMEZONE}."
    )


def no_when_note() -> str:
    return unreadable_note("the issue body has no `When:` line")


def denied_note(login: str) -> str:
    return (
        f"This task is not scheduled: `{login}` may not start work on this board, and only "
        "people who may start a worker with `/claude` may create or change a scheduled task."
    )


def stopped_note() -> str:
    return "This task is stopped because the issue is closed. Reopen it to start it again."


def skipped_note(occurrence: datetime, previous: str, next_run: datetime) -> str:
    return (
        f"Skipped the run due {local_text(occurrence)}: the previous run {previous} is still "
        f"open. Next run: **{local_text(next_run)}**."
    )


@dataclass(frozen=True)
class ListRow:
    number: int
    title: str
    status: str
    next_run: datetime | None = None
    last_run: int | None = None


def list_body(rows: list[ListRow], *, label: str = LABEL) -> str:
    """The pinned list issue's body: every task, when it runs, and how to add or stop one."""
    parts = [
        "Every scheduled task in this repository. AgentDeck keeps this list up to date, so "
        "edits here are overwritten.",
        "",
    ]
    if rows:
        parts += ["| Task | Schedule | Next run | Last run |", "| --- | --- | --- | --- |"]
        for row in sorted(rows, key=lambda item: item.number):
            title = row.title.replace("|", "\\|")
            next_run = local_text(row.next_run) if row.next_run else "—"
            last = f"#{row.last_run}" if row.last_run else "—"
            parts.append(f"| #{row.number} {title} | {row.status} | {next_run} | {last} |")
    else:
        parts.append("There are no scheduled tasks yet.")
    parts += [
        "",
        f"Times are {TIMEZONE}.",
        "",
        "## Add a task",
        "",
        "Ask the agent, for example `/claude run an SEO audit every Monday at 06:00`, or open "
        f"an issue yourself with the `{label}` label and a body like this:",
        "",
        "```",
        "When: every Monday 06:00",
        "",
        "## Prompt",
        "",
        "What the worker should do on each run.",
        "```",
        "",
        f"`When:` understands {EXAMPLES}, `every weekday 09:00`, `every Tuesday and Friday "
        "12:00` and `last day of the month 16:00`. AgentDeck comments the next run time on the "
        "task, so a typo shows up at once.",
        "",
        "## Change or stop a task",
        "",
        "Edit the task issue's `When:` line or prompt, or ask the agent to. Close the task issue "
        "to stop it and reopen it to start it again. Each run files a new card that links back "
        "to its task; while that card is still open the next run is skipped, with a comment.",
        "",
        LIST_MARKER,
    ]
    return "\n".join(parts)


@dataclass(frozen=True)
class TaskIssue:
    """One ``scheduled`` issue as the sweep read it.

    ``editor`` is whoever last edited the body, and is empty when nobody has: then the author
    wrote the definition in force, and it is the author who must be allowed to start work.
    """

    repo: str
    number: int
    title: str = ""
    body: str = ""
    open: bool = True
    author: str = ""
    editor: str = ""

    @property
    def writer(self) -> str:
        return self.editor or self.author
