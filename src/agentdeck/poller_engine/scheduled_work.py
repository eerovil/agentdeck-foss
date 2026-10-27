"""The wake phase that runs scheduled tasks (#2517); the rules themselves are ``schedules``.

Three jobs, in this order, on every full wake:

1. **Read the tasks.** One sweep of every ``scheduled`` issue. A task that is new, edited,
   closed or reopened is re-read: its ``When:`` line parsed, its writer authorized, its next run
   worked out, and a comment queued on the issue saying when that is (or why it will not run).
2. **File what is due.** A due task files one card in its own repository, labelled with the
   ownership label so a worker picks it up. Creating an issue is not idempotent, so this follows
   ``_open_proposal`` (#393): the intent is durable before the write, and a wake that finds an
   intent looks for the marked card before it creates one. A task whose previous run's card is
   still open files nothing and says so. After downtime only the latest missed run is filed —
   the next run is always worked out from *now*, so an outage never floods the board.
3. **Keep the list.** One pinned "Scheduled tasks" issue per repository that has tasks, its
   body rewritten only when what it says has changed.

State lives in ``state.json`` beside the rest of the engine's state, under ``scheduled_tasks``
and ``scheduled_lists``, so a restart loses nothing and the next run time survives it.

Budget: one GraphQL point a full wake for the sweep, whatever the number of repositories. A
permission lookup only when a task's definition changes, an issue read for the previous run only
when a run is due, and writes only when there is something to say. A quiet wake does none of it,
except that a run falling due makes the wake a full one (see ``due``).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from . import authorization as auth
from . import queue, schedules
from .ports import CommentBody, Effect, EffectKind, EnginePort
from .state import StateStore

TASKS = "scheduled_tasks"
LISTS = "scheduled_lists"
# At most this many run cards a wake. A board with many tasks due at once still files them all,
# a few wakes apart, rather than in one burst.
OPENS_PER_WAKE = 5
# How long a due run waits for its previous card's state to become readable before it is filed
# anyway. A deleted card reads as unreadable for ever, and a run must not wait for ever on it.
UNREADABLE_PREVIOUS_GRACE = timedelta(hours=6)
PIN_ATTEMPTS = 3
# Pending comments kept per task. Only the newest few matter: a task edited three times while
# GitHub refused comments needs to hear the latest next-run time, not a backlog.
NOTES_KEPT = 3


def due(state: dict, now: datetime) -> bool:
    """Is a run due, so this wake must be a full one?

    A schedule is local news no webhook announces. Without this a run could wait out the whole
    webhook-quiet ceiling past its time.
    """
    for rec in (state.get(TASKS) or {}).values():
        if not isinstance(rec, dict) or not rec.get("active"):
            continue
        moment = schedules.parse_stamp(rec.get("next"))
        if moment is not None and moment <= now:
            return True
    return False


def run(
    state: dict,
    port: EnginePort,
    outcome,
    *,
    now: datetime,
    store: StateStore,
    policy: auth.Policy,
) -> None:
    """The whole phase for one full wake (see module docstring)."""
    held = state.get(TASKS)
    sweep = port.scheduled_tasks(tuple(sorted(held)) if isinstance(held, dict) else ())
    if sweep is None:
        outcome.report.failed("the scheduled-task sweep could not be read")
    elif sweep or state.get(TASKS):
        _sync(state, port, outcome, sweep, now=now, policy=policy)
    tasks = state.get(TASKS)
    if not isinstance(tasks, dict) or not tasks:
        state.pop(TASKS, None)
    else:
        # A new run is filed only on a wake that read the tasks just now. Cached state may
        # name a task that was closed or edited while the sweep was failing, and filing from
        # it would run a definition GitHub no longer holds. A filing already begun is still
        # finished: its intent was decided on a readable sweep, and the marker lookup keeps
        # it to one card.
        _file_due(
            state, port, outcome, tasks, now=now, store=store, fresh=sweep is not None,
        )
        _deliver_notes(port, outcome, tasks)
    if sweep is not None:
        _keep_lists(state, port, outcome, now=now, store=store)


# --- 1. reading the tasks ---


def _signature(task: schedules.TaskIssue, when: str | None, prompt: str) -> str:
    text = "\0".join((task.title, when or "", prompt, task.writer.lower()))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _note(rec: dict, text: str) -> None:
    rec["notes"] = [*(rec.get("notes") or []), text][-NOTES_KEPT:]


def _writer_verdict(policy: auth.Policy, port: EnginePort, task: schedules.TaskIssue):
    """May whoever wrote this definition start work here? The ``/claude`` bar (#2517).

    The engine's own account is the agent chat a person asked to write the task, exactly as its
    ownership label is accepted (#2390). GraphQL spells an app's login without ``[bot]``, so both
    spellings are its. Any other app is refused: a scheduled run is a start, and an app may
    steer a card but never start one (#2056).
    """
    login = task.writer.strip().lower()
    if not login:
        return auth.Verdict.DENIED
    if policy.bot and login in {policy.bot, policy.bot.removesuffix("[bot]")}:
        return auth.Verdict.AUTHORIZED
    if queue.is_bot_login(login):
        return auth.Verdict.DENIED
    return auth.decide(
        policy, auth.Tier.WORK, login=login, permission=port.permission(task.repo, login)
    )


def _sync(
    state: dict,
    port: EnginePort,
    outcome,
    sweep: dict[str, schedules.TaskIssue],
    *,
    now: datetime,
    policy: auth.Policy,
) -> None:
    tasks = state.setdefault(TASKS, {})
    for key in list(tasks):
        if key in sweep:
            continue
        rec = tasks[key]
        if isinstance(rec, dict) and rec.get("opening"):
            rec["active"] = False  # finish the run it was filing, then forget it
            continue
        # The sweep read every tracked task directly, so absence is GitHub's answer: the label
        # was removed or the issue deleted. Neither is a task any more.
        tasks.pop(key)
        outcome.log.append(f"scheduled: {key} is no longer a scheduled task")
    for key, task in sorted(sweep.items()):
        rec = tasks.get(key)
        if not task.open:
            if rec is None:
                continue  # a closed task never run here: there is nothing to stop
            if rec.get("open", True):
                was_active = bool(rec.get("active"))
                rec.update(open=False, active=False, status="stopped (issue closed)")
                rec.pop("next", None)
                if was_active:
                    _note(rec, schedules.stopped_note())
                outcome.log.append(f"scheduled: {key} stopped — the issue is closed")
            continue
        when = schedules.when_line(task.body)
        prompt = schedules.prompt_section(task.body)
        signature = _signature(task, when, prompt)
        if rec is not None and rec.get("open") and rec.get("signature") == signature:
            continue
        verdict = _writer_verdict(policy, port, task)
        if verdict is auth.Verdict.UNCERTAIN:
            outcome.report.failed(f"authorization uncertain for the writer of task {key}")
            continue  # the definition in force, if any, stays until this can be decided
        rec = tasks.setdefault(key, {})
        previous_when = rec.get("when") if rec.get("active") else None
        rec.update(
            open=True, signature=signature, title=task.title, prompt=prompt,
            writer=task.writer, when=when or "",
        )
        if verdict is auth.Verdict.DENIED:
            rec.update(active=False, status="not scheduled (not allowed)")
            rec.pop("next", None)
            _note(rec, schedules.denied_note(task.writer or "nobody"))
            outcome.log.append(f"scheduled: {key} refused — {task.writer} may not start work")
            continue
        parsed = schedules.parse_when(when) if when is not None else None
        if not isinstance(parsed, schedules.Schedule):
            rec.update(active=False, status="not scheduled (When line not understood)")
            rec.pop("next", None)
            _note(
                rec,
                schedules.no_when_note() if parsed is None else schedules.unreadable_note(parsed),
            )
            outcome.log.append(f"scheduled: {key} has no readable When line")
            continue
        upcoming = schedules.parse_stamp(rec.get("next"))
        if previous_when != when or upcoming is None:
            # A new or changed schedule starts counting from now. An unchanged one keeps the run
            # it already promised, so editing the prompt never skips or repeats a run.
            upcoming = parsed.next_after(now)
        rec.update(active=True, status=parsed.describe(), next=schedules.stamp(upcoming))
        _note(rec, schedules.scheduled_note(parsed, upcoming))
        outcome.log.append(
            f"scheduled: {key} runs {parsed.describe()}, next {schedules.stamp(upcoming)}"
        )


# --- 2. filing what is due ---


def _file_due(
    state: dict,
    port: EnginePort,
    outcome,
    tasks: dict,
    *,
    now: datetime,
    store: StateStore,
    fresh: bool,
) -> None:
    filed = 0
    for key, rec in sorted(tasks.items()):
        if filed >= OPENS_PER_WAKE:
            outcome.log.append("scheduled: more runs are due — the rest wait for the next wake")
            return
        if not isinstance(rec, dict):
            continue
        if rec.get("opening"):
            filed += _open_run(port, outcome, key, rec, now=now)
            continue
        occurrence = schedules.parse_stamp(rec.get("next"))
        if not rec.get("active") or occurrence is None or occurrence > now:
            continue
        if not fresh:
            outcome.log.append(f"scheduled: {key} waits — the tasks could not be read this wake")
            continue
        parsed = schedules.parse_when(str(rec.get("when") or ""))
        if not isinstance(parsed, schedules.Schedule):
            rec.update(active=False)
            rec.pop("next", None)
            continue
        repo, _number = key.split("#", 1)
        previous = (rec.get("last_run") or {}).get("number")
        if isinstance(previous, int):
            ref = f"{repo}#{previous}"
            seen = port.ref_states([ref]).get(ref)
            if seen is None and now - occurrence < UNREADABLE_PREVIOUS_GRACE:
                outcome.log.append(f"scheduled: {key} waits — cannot read its previous run {ref}")
                continue
            if seen == "OPEN":
                upcoming = parsed.next_after(max(now, occurrence))
                rec["next"] = schedules.stamp(upcoming)
                _note(rec, schedules.skipped_note(occurrence, f"#{previous}", upcoming))
                outcome.log.append(f"scheduled: {key} skipped — previous run {ref} is still open")
                continue
        rec["opening"] = {"occurrence": schedules.stamp(occurrence), "at": schedules.stamp(now)}
        # Durable before the side effect, so a crash is recoverable by looking (#393).
        store.checkpoint(state)
        filed += _open_run(port, outcome, key, rec, now=now)


def _open_run(port: EnginePort, outcome, key: str, rec: dict, *, now: datetime) -> int:
    """Finish filing the run ``rec["opening"]`` names, exactly once; 1 when a card was filed."""
    repo, number = key.split("#", 1)
    opening = rec["opening"]
    occurrence = schedules.parse_stamp(opening.get("occurrence")) or now
    marker = schedules.RUN_MARKER.format(key=key, occurrence=schedules.stamp(occurrence))
    found, conclusive = port.find_marked_issue(
        repo, marker, since=str(opening.get("at") or schedules.stamp(now))
    )
    if not conclusive:
        # Unknown, not absent: creating now is the one mistake this path exists to avoid.
        outcome.log.append(f"scheduled: cannot yet confirm whether {key}'s run was filed")
        return 0
    parsed = schedules.parse_when(str(rec.get("when") or ""))
    if not isinstance(parsed, schedules.Schedule):
        parsed = schedules.Schedule(0, 0)  # an every-day label: the run is dated, at least
    if found is None:
        found = port.open_issue(
            repo,
            schedules.run_title(str(rec.get("title") or ""), parsed, occurrence),
            schedules.run_body(
                number=int(number), prompt=str(rec.get("prompt") or ""),
                occurrence=occurrence, marker=marker,
            ),
        )
    if found is None:
        outcome.log.append(f"scheduled: could not file a run for {key}")
        return 0
    rec.pop("opening", None)
    rec["last_run"] = {"number": int(found), "occurrence": schedules.stamp(occurrence)}
    if rec.get("active"):
        # From whichever is later, the run just filed or now: after downtime only the latest
        # missed run is filed and the next one is in the future.
        rec["next"] = schedules.stamp(parsed.next_after(max(now, occurrence)))
    outcome.log.append(f"scheduled: {repo}#{found} filed for {key}")
    return 1


def _deliver_notes(port: EnginePort, outcome, tasks: dict) -> None:
    for key, rec in tasks.items():
        if not isinstance(rec, dict) or not rec.get("notes"):
            continue
        repo, number = key.split("#", 1)
        notes = list(rec["notes"])
        while notes:
            effect = Effect(EffectKind.COMMENT, repo, int(number), CommentBody(notes[0]))
            landed = bool(port.apply(effect))
            outcome.effects.append(effect)
            if not landed:
                outcome.report.failed(f"effect comment did not land on {key}")
                break
            notes.pop(0)
        if notes:
            rec["notes"] = notes
        else:
            rec.pop("notes", None)


# --- 3. the pinned list ---


def _rows(tasks: dict, repo: str) -> list[schedules.ListRow]:
    rows = []
    for key, rec in tasks.items():
        task_repo, _, number = key.partition("#")
        if task_repo != repo or not isinstance(rec, dict) or not number.isdigit():
            continue
        last = (rec.get("last_run") or {}).get("number")
        rows.append(schedules.ListRow(
            number=int(number),
            title=str(rec.get("title") or ""),
            status=str(rec.get("status") or ""),
            next_run=schedules.parse_stamp(rec.get("next")) if rec.get("active") else None,
            last_run=last if isinstance(last, int) else None,
        ))
    return rows


def _keep_lists(
    state: dict,
    port: EnginePort,
    outcome,
    *,
    now: datetime,
    store: StateStore,
) -> None:
    tasks = state.get(TASKS) if isinstance(state.get(TASKS), dict) else {}
    if not isinstance(state.get(LISTS), dict):
        state[LISTS] = {}
    lists = state[LISTS]
    repos = {key.split("#", 1)[0] for key in tasks} | set(lists)
    for repo in sorted(repos):
        rows = _rows(tasks, repo)
        body = schedules.list_body(rows)
        rec = lists.setdefault(repo, {})
        number = rec.get("number")
        if not isinstance(number, int):
            if not rows and not rec.get("opening_at"):
                lists.pop(repo, None)  # no tasks and no list yet: nothing to pin
                continue
            number = _open_list(state, port, outcome, repo, rec, body, now=now, store=store)
            if number is None:
                continue
        elif rec.get("body") != body:
            effect = Effect(EffectKind.SET_BODY, repo, number, body)
            outcome.effects.append(effect)
            if port.apply(effect):
                rec["body"] = body
            else:
                outcome.report.failed(f"effect set-body did not land on {repo}#{number}")
        if not rec.get("pinned") and int(rec.get("pin_attempts") or 0) < PIN_ATTEMPTS:
            # Best effort and bounded: a repository already holding three pinned issues refuses
            # for ever, and that must not read as a failing engine on every wake.
            rec["pin_attempts"] = int(rec.get("pin_attempts") or 0) + 1
            effect = Effect(EffectKind.PIN_ISSUE, repo, number)
            outcome.effects.append(effect)
            if port.apply(effect):
                rec["pinned"] = True
            else:
                outcome.log.append(f"scheduled: could not pin the task list {repo}#{number}")
    if not lists:
        state.pop(LISTS, None)


def _open_list(
    state: dict,
    port: EnginePort,
    outcome,
    repo: str,
    rec: dict,
    body: str,
    *,
    now: datetime,
    store: StateStore,
) -> int | None:
    found: int | None = None
    if rec.get("opening_at"):
        found, conclusive = port.find_marked_issue(
            repo, schedules.LIST_MARKER, since=str(rec["opening_at"])
        )
        if not conclusive:
            outcome.log.append(f"scheduled: cannot yet confirm whether {repo}'s list was filed")
            return None
    else:
        rec["opening_at"] = schedules.stamp(now)
        store.checkpoint(state)
    if found is None:
        found = port.open_list_issue(repo, schedules.LIST_TITLE, body)
        if found is not None:
            rec["body"] = body
    if found is None:
        outcome.log.append(f"scheduled: could not open the task list in {repo}")
        return None
    rec.pop("opening_at", None)
    rec["number"] = int(found)
    outcome.log.append(f"scheduled: {repo}#{found} is the task list")
    return int(found)
