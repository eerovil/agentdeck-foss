"""The wake: one cycle composing every engine module in a safe order (#176 slice 9).

A Poller Instance has no resident process — it wakes every ~30s, does one cycle, and
exits (#116). ``run_cycle`` *is* that wake. The deck runs every Work itself (ADR 0008,
#2895), so the engine holds no cards and starts no workers (#3732); what a wake still does
is the project's own housekeeping:

* the drift and pause gates, which still report;
* the extensions' observe hooks, and the issues their proposals open (``open-work``), which
  are labelled with the ownership label so the deck starts their Works;
* the bug reports the deck leaves for this instance (#2888), filed as labelled issues;
* scheduled tasks (#2517), which file their runs as labelled issues too;
* extension health, and the health record every wake writes.

A wake always reports: health is written even when the cycle refuses to run (drift), is
paused, or a phase fails — the record is the only observability surface (#107), so an
unreported wake would be indistinguishable from a dead host.

The engine performs no I/O: reads arrive through ``EnginePort`` and outward changes leave as
``Effect`` values applied through that port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import authorization as auth
from . import bug_reports, config, health, scheduled_work
from . import extensions as ext
from .health import CycleReport, HealthVerdict
from .ports import CommentBody, Effect, EffectKind, EnginePort
from .state import StateStore


@dataclass
class CycleOutcome:
    """What one wake did: the effects it applied and what it logged."""

    report: CycleReport
    verdict: HealthVerdict | None = None
    effects: list[Effect] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    halted: str | None = None  # the cycle refused to run (drift / paused / query reserve)
    # The wake ran, with its live GitHub reads declined because the webhook streams proved
    # there was nothing to read (#1673). Not `halted`: durable state still moved.
    quiet: str = ""


def run_cycle(
    store: StateStore,
    port: EnginePort,
    *,
    now: datetime,
    issue_repos: frozenset[str] | set[str] | None = None,
    drift: str | None = None,
    enabled_extensions: list | None = None,
    mode: ext.Mode = ext.Mode.LIVE,
    policy: auth.Policy | None = None,
    proposal_daily_cap: int | None = None,
    webhook_skip_s: int = config.DEFAULT_WEBHOOK_SKIP_S,
    webhook_quiet: bool = False,
    spec_only_label: str = "",
) -> CycleOutcome:
    """Run one wake and return what it did (see module docstring).

    ``drift`` is the fail-closed signal from the config/engine fingerprint check
    (#108/#116): when set, the cycle records health and does nothing else.
    ``enabled_extensions`` are the project's extensions (#179); one loaded refused stays in the
    list because it is still what this project enables — it simply answers nothing, at any hook
    (#2158). ``mode`` is live or shadow, and in shadow mode nothing an extension asks for is
    honored (#109's dry run). ``policy`` is the project's controller policy (#110); it decides
    who may define a scheduled task.
    ``issue_repos`` are the repositories this project holds issues in, which bounds where an
    extension may cause an issue to be *opened* (#393). Naming none permits none.
    ``proposal_daily_cap`` is the ceiling on extension-proposed issues opened in one UTC day
    (#371).
    ``webhook_quiet`` is the host's claim that the webhook streams prove nothing new landed for
    this instance and the watcher writing them is alive (#1631). It is a *hint the engine may
    decline*: with the ``webhook_skip_s`` policy knob on and a full wake recent enough, the wake
    declines its live GitHub reads — and only those (#1673).
    ``spec_only_label`` is the project's spec-only label, which an ``open-work`` proposal may ask
    to be opened under (#2874). Naming none refuses such a proposal rather than opening it as a
    build nobody approved.
    """
    report = CycleReport()
    outcome = CycleOutcome(report=report)
    policy = policy or auth.Policy()

    with store.transaction() as state:
        if drift:
            report.drift = drift
            outcome.halted = "drift"
            outcome.log.append(f"cycle refused: {drift} drift — fail closed")
            _finish(state, outcome, now=now)
            return outcome

        if health.is_paused(state):
            outcome.halted = "paused"
            outcome.log.append("cycle paused by operator note")
            _finish(state, outcome, now=now)
            return outcome

        load = store.last_load
        report.state_fallback = load.used_backup

        quiet_skip = _webhook_skip(
            state, now=now, webhook_skip_s=webhook_skip_s, webhook_quiet=webhook_quiet
        )
        if quiet_skip and scheduled_work.due(state, now):
            # A scheduled run falling due is local news (#2517): nothing on GitHub changes
            # when a clock does, and filing the run needs a live read of the previous one.
            quiet_skip = None
        budget_halt = port.live_queries_halt(stale_reading_ok=bool(quiet_skip))
        if budget_halt:
            halt_reason, _, reading_source = budget_halt.partition(": ")
            outcome.halted = halt_reason
            budget_name = "REST core" if halt_reason == "core-budget" else "GraphQL"
            source_note = f" ({reading_source})" if reading_source else ""
            outcome.log.append(f"{budget_name} reserve reached{source_note} — live wake deferred")
            _finish(state, outcome, now=now)
            return outcome
        if quiet_skip:
            # The skip is of the *live GitHub queries*, not of the wake (#1673): what waits
            # for the ceiling is discovery, the only thing the webhook streams are evidence of.
            outcome.quiet = "webhook-quiet"
            outcome.log.append(quiet_skip)
        else:
            # Any wake past this point runs live GitHub queries, and this stamp is what bounds
            # how long a quiet skip may trust the streams (#1631).
            state["last_live_wake"] = now.astimezone(UTC).isoformat()

        installed = list(enabled_extensions or [])
        # A Work the deck saw stop on an AgentDeck bug (#2888): local, so read on a quiet wake.
        reported = _intake_bug_reports(state, store, outcome, issue_repos)
        _observe(
            state,
            port,
            outcome,
            installed,
            now=now,
            mode=mode,
            store=store,
            daily_cap=proposal_daily_cap,
            issue_repos=frozenset(issue_repos or ()),
            spec_only_label=spec_only_label,
        )
        store.checkpoint(state)
        bug_reports.discard(reported)

        if not outcome.quiet:
            _file_bug_issues(state, port, outcome, now=now, store=store)
            # Discovery plus the issues it files, so a full wake only (#2517). A run filed here
            # is labelled at creation, and the deck starts its Work.
            scheduled_work.run(state, port, outcome, now=now, store=store, policy=policy)

        _extension_health(outcome, installed, state, mode=mode)
        _finish(state, outcome, now=now)
    return outcome


def _finish(state: dict, outcome: CycleOutcome, *, now: datetime) -> None:
    """Always publish health, whatever the cycle did or refused to do."""
    health.record_cycle(state, outcome.report, now=now)
    outcome.verdict = health.evaluate(state, now=now)


# How the host offers the quiet hint to the pinned engine artifact (#1631). An environment
# variable rather than a CLI flag, deliberately: the wake script comes from the checkout while
# the artifact is pinned, and an unknown flag would make an older engine refuse every wake,
# where an unknown variable is simply ignored — version skew in either direction degrades to
# full polling.
WEBHOOK_QUIET_ENV = "AGENTDECK_POLLER_WEBHOOK_QUIET"


def _webhook_skip(
    state: dict, *, now: datetime, webhook_skip_s: int, webhook_quiet: bool
) -> str | None:
    """Why this wake may skip its live GitHub queries, or None to run them (#1631).

    Three conditions, and every one that cannot be proved runs the full wake — the failure
    mode always points toward more polling. The host proved two of them before offering
    ``webhook_quiet`` (no new deliveries on this instance's streams, and a fresh heartbeat
    from the watcher writing them); the engine owns the third: a *full* wake recent enough
    that trusting an at-most-once delivery channel stays bounded by ``webhook_skip_s``. A
    missing, unreadable or future ``last_live_wake`` stamp is no evidence, not a skip.
    """
    if webhook_skip_s <= 0 or not webhook_quiet:
        return None
    stamp = str(state.get("last_live_wake") or "")
    if not stamp:
        return None
    try:
        last = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age = (now - last).total_seconds()
    if not 0 <= age < webhook_skip_s:
        return None
    return (
        f"webhook streams quiet ({int(age)}s since the last full wake, "
        f"ceiling {webhook_skip_s}s) — live wake skipped"
    )


def _do(port: EnginePort, outcome: CycleOutcome, effect: Effect) -> bool:
    """Apply one outward change through the port, recording it for observability."""
    landed = bool(port.apply(effect))
    outcome.effects.append(effect)
    if not landed:
        target = f"{effect.repo}#{effect.number}"
        outcome.report.failed(f"effect {effect.kind.value} did not land on {target}")
    return landed


def _comment_effect(repo: str, number: int, body: object) -> Effect:
    """A plain comment on an issue: the engine holds no card whose links it could add."""
    return Effect(EffectKind.COMMENT, repo, int(number), CommentBody(str(body)))


def _intake_bug_reports(state: dict, store: StateStore, outcome: CycleOutcome, issue_repos) -> list:
    """Fold the bug reports the deck left for this instance into their records (#2888).

    Returns the report files to remove once the state holding them is checkpointed.
    """
    paths = getattr(store, "paths", None)
    if paths is None:
        return []
    done, log, _added = bug_reports.ingest(
        state, bug_reports.directory(paths.state), repos=frozenset(issue_repos or ())
    )
    outcome.log.extend(log)
    return done


_BUG_MARKER = "<!-- agentdeck-needs-retry-bug: {ident} -->"


def _file_bug_issues(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    store: StateStore | None,
) -> None:
    """File, or add to, the one labelled issue per Needs retry bug cause (#2888).

    Opened through ``open_issue``, which labels it with the ownership label in the same write,
    so the deck starts its Work like any other. Creating an issue is not idempotent, so this
    follows ``_open_proposal``: the intent is durable before the write, and a wake that finds
    an intent looks for the marked issue before it creates one. An issue already filed and
    still open gets a comment with the new evidence instead; a closed one means the fix did
    not hold, so a new issue is filed.
    """
    records = state.get(bug_reports.RECORDS)
    if not isinstance(records, dict):
        return
    waiting = {
        ident: record
        for ident, record in records.items()
        if isinstance(record, dict) and record.get("pending")
    }
    if not waiting:
        return
    filed_refs = [
        f"{record['repo']}#{record['number']}"
        for record in waiting.values()
        if isinstance(record.get("number"), int)
    ]
    states = port.ref_states(filed_refs) if filed_refs else {}
    for ident, record in waiting.items():
        repo = str(record.get("repo") or "")
        cause = str(record.get("cause") or "")
        pending = list(record.get("pending") or ())
        number = record.get("number")
        if isinstance(number, int):
            seen = states.get(f"{repo}#{number}")
            if seen is None:
                continue  # unreadable this wake: neither a comment nor a second issue
            if seen == "OPEN":
                if _do(
                    port,
                    outcome,
                    _comment_effect(repo, number, bug_reports.update(cause, pending)),
                ):
                    record["pending"] = []
                continue
            record.pop("number", None)
        mark = _BUG_MARKER.format(ident=ident)
        found: int | None = None
        if record.get("opening_at"):
            found, conclusive = port.find_marked_issue(repo, mark, since=str(record["opening_at"]))
            if not conclusive:
                outcome.log.append(f"bug report: cannot yet confirm whether {ident} was filed")
                continue
        else:
            record["opening_at"] = now.astimezone(UTC).isoformat()
            if store is not None:
                store.checkpoint(state)
        if found is None:
            found = port.open_issue(
                repo, bug_reports.title(cause), bug_reports.body(cause, pending, mark)
            )
        if found is None:
            outcome.log.append(f"bug report: could not open an issue for {ident}")
            continue
        record.pop("opening_at", None)
        record["number"] = int(found)
        record["pending"] = []
        outcome.log.append(f"bug report: {repo}#{found} filed for {cause}")


# --- the extension hooks (#179) ---


def _observe(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    installed: list,
    *,
    now: datetime,
    mode: ext.Mode,
    store: StateStore,
    daily_cap: int | None = None,
    issue_repos: frozenset[str] = frozenset(),
    spec_only_label: str = "",
) -> None:
    """Let each observe hook propose issues, and open what it asked for, exactly once each.

    ``daily_cap`` is the project's ceiling on proposals honored in one UTC day (#371). It is
    enforced here rather than inside the extension because an extension policing its own
    budget is trusting the thing being bounded. What it refuses is not lost: a proposal an
    extension still wants is offered again on the next wake, under the next day's budget.
    """
    if mode is ext.Mode.LIVE:
        drain_staged_proposals(
            state,
            port,
            outcome,
            now=now,
            store=store,
            daily_cap=daily_cap,
            issue_repos=issue_repos,
            spec_only_label=spec_only_label,
        )
    for extension in installed:
        if not extension.implements(ext.Hook.OBSERVE):
            continue
        response = ext.invoke(
            extension,
            ext.Hook.OBSERVE,
            context=_observe_context(state, extension=extension.name, quiet=bool(outcome.quiet)),
            mode=mode,
        )
        _note_extension(outcome, extension.name, response)
        if mode is ext.Mode.LIVE and response.ok and response.memory is not None:
            # Kept only from a live wake that answered: a dry run must leave no trace, and a
            # skipped extension keeps what it had rather than forgetting it.
            state.setdefault(_EXTENSION_MEMORY, {})[extension.name] = response.memory
        honor_proposals(
            state,
            port,
            outcome,
            extension.name,
            ext.honored(response, mode),
            now=now,
            store=store,
            daily_cap=daily_cap,
            issue_repos=issue_repos,
            spec_only_label=spec_only_label,
        )


def _observe_context(state: dict, *, extension: str = "", quiet: bool = False) -> dict:
    """What an observe hook may see: its own settled keys, its memory and the quiet fact.

    ``quiet`` is the engine's own decision for this wake, not the host's raw hint: the hint
    says the streams look silent, and the engine still forces a full wake once
    ``webhook_skip_s`` has elapsed. An extension reading the hint would skip exactly the wake
    that exists to bound trusting an at-most-once channel, so it is told the decision instead.

    It matters because an extension's GitHub reads are its own — a different token, outside the
    engine's port — so the quiet skip could never reach them.

    ``settled`` is every proposal key this extension has already had decided — opened, or
    staged in the manual inbox — with the ``ext:<name>:`` namespace stripped, because a key only
    means anything inside the extension that minted it. An extension that bounds how much it
    proposes per wake needs this: without it, a bound spent on keys the engine will only drop
    again leaves genuinely new work unproposed.

    The card-era ``cards`` list and ``works_router`` flag went with the last extension that read
    them, ``chatgpt-nudge`` (#3741).
    """
    return {
        "settled": sorted(_settled_keys(state, extension)) if extension else [],
        # What this extension's last live observe asked to be handed back (#2691).
        "memory": extension_memory(state, extension),
        # True when this wake trusted the webhook streams and asked GitHub nothing. An
        # extension that reads GitHub should do the same, and must treat it as "not read"
        # rather than "nothing there".
        "quiet": bool(quiet),
    }


def extension_memory(state: dict, extension: str) -> dict:
    """The memory this extension last left with the engine, or an empty one."""
    memory = (state.get(_EXTENSION_MEMORY) or {}).get(extension) if extension else None
    return memory if isinstance(memory, dict) else {}


def _settled_keys(state: dict, extension: str) -> set[str]:
    """The bare proposal keys this extension would be refused for re-proposing."""
    prefix = f"ext:{extension}:"
    settled = {cid[len(prefix) :] for cid in state.get(_OPENED, {}) if str(cid).startswith(prefix)}
    for item in state.get(_PROPOSAL_INBOX, []):
        if not isinstance(item, dict) or str(item.get("extension") or "") != extension:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        key = str(payload.get("key") or "")
        if key:
            settled.add(key)
    return settled


_PROPOSALS = "proposals"
# Per extension, the object its last live observe answered as ``memory`` (#2691).
_EXTENSION_MEMORY = "extension_memory"
_PROPOSAL_INBOX = "proposal_inbox"
# Keys whose issue this engine has already opened, and the number it opened (#393). This ledger
# only ever grows, because "we already filed this" never stops being true.
_OPENED = "opened"
# Keys whose creation was begun but not finished: the durable intent written before the API call,
# so a crash in between is recoverable by looking rather than by guessing.
_OPENING = "opening"


def _open_marker(cid: str) -> str:
    """The marker written into an opened issue's body, so it can be recognized again."""
    return f"<!-- poller-opened: {cid} -->"


def _proposal_budget(state: dict, now: datetime) -> dict:
    """This UTC day's proposal tally, rolled over when the day turns.

    Durable, so the ceiling survives a restart: a cap that forgets what it already spent every
    time the poller is redeployed is not a budget guard at all.
    """
    today = now.astimezone(UTC).date().isoformat()
    record = state.get(_PROPOSALS)
    if not isinstance(record, dict) or record.get("date") != today:
        record = {"date": today, "honored": 0}
        state[_PROPOSALS] = record
    return record


def stage_proposals(state: dict, name: str, requests: tuple[ext.Request, ...]) -> int:
    """Put operator-requested proposals in the durable inbox for the next wake."""
    inbox = state.setdefault(_PROPOSAL_INBOX, [])
    pending = {
        (str(item.get("extension") or ""), str((item.get("payload") or {}).get("key") or ""))
        for item in inbox
        if isinstance(item, dict)
    }
    added = 0
    for request in requests:
        if not valid_proposal_request(request):
            continue
        key = str(request.payload.get("key") or "")
        if (name, key) in pending or f"ext:{name}:{key}" in state.get(_OPENED, {}):
            continue
        inbox.append(
            {"extension": name, "kind": request.kind.value, "payload": dict(request.payload)}
        )
        pending.add((name, key))
        added += 1
    if not inbox:
        state.pop(_PROPOSAL_INBOX, None)
    return added


def drain_staged_proposals(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    *,
    now: datetime,
    store: StateStore,
    daily_cap: int | None,
    issue_repos: frozenset[str],
    spec_only_label: str = "",
) -> None:
    """Honor the manual inbox first, retaining anything the daily cap or I/O defers."""
    remaining: list[dict] = []
    for item in state.get(_PROPOSAL_INBOX, []):
        if not isinstance(item, dict):
            continue
        try:
            request = ext.Request(
                kind=ext.RequestKind(str(item.get("kind") or "")),
                payload=item.get("payload") if isinstance(item.get("payload"), dict) else {},
            )
        except ValueError:
            continue
        name = str(item.get("extension") or "")
        key = str(request.payload.get("key") or request.payload.get("instruction") or "")
        cid = f"ext:{name}:{key}"
        if key and cid in state.get(_OPENED, {}):
            continue
        if _permanently_refused_staged(
            request, issue_repos=issue_repos, spec_only_label=spec_only_label
        ):
            outcome.log.append(f"proposals: dropped malformed or refused request {cid!r}")
            continue
        if daily_cap is not None and _proposal_budget(state, now)["honored"] >= daily_cap:
            remaining.append(item)
            continue
        if not honor_proposals(
            state,
            port,
            outcome,
            name,
            (request,),
            now=now,
            store=store,
            daily_cap=daily_cap,
            issue_repos=issue_repos,
            spec_only_label=spec_only_label,
        ):
            remaining.append(item)
    if remaining:
        state[_PROPOSAL_INBOX] = remaining
        if daily_cap is not None and _proposal_budget(state, now)["honored"] >= daily_cap:
            outcome.log.append(
                f"proposals: daily cap ({daily_cap}) reached — manual requests wait for tomorrow"
            )
    else:
        state.pop(_PROPOSAL_INBOX, None)


def _permanently_refused_staged(
    request: ext.Request, *, issue_repos: frozenset[str], spec_only_label: str = ""
) -> bool:
    """True when retrying this stored shape can never change the answer."""
    if not valid_proposal_request(request):
        return True
    return str(request.payload.get("repo") or "") not in issue_repos or (
        bool(request.payload.get("spec_only")) and not spec_only_label
    )


def valid_proposal_request(request: ext.Request) -> bool:
    """Whether this is an ``open-work`` request carrying every field opening an issue needs.

    ``queue-work`` asked for work on a card the engine held; it holds none now (#3732), so such
    a request is never valid.
    """
    payload = request.payload
    return bool(
        request.kind is ext.RequestKind.OPEN_WORK
        and str(payload.get("repo") or "")
        and str(payload.get("key") or "")
        and str(payload.get("title") or "").strip()
    )


def honor_proposals(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    name: str,
    requests: tuple[ext.Request, ...],
    *,
    now: datetime,
    store: StateStore,
    daily_cap: int | None,
    issue_repos: frozenset[str],
    spec_only_label: str = "",
) -> int:
    """Open the issues proposals ask for, through one cap, repository, and exact-once path."""
    honored = 0
    capped = False
    for request in requests:
        if request.kind is ext.RequestKind.QUEUE_WORK:
            outcome.log.append(
                f"{name}: refused queue-work on {request.payload.get('repo')}#"
                f"{request.payload.get('number')} — the deck runs every Work, so there is no "
                "card to queue it on"
            )
            continue
        if request.kind is not ext.RequestKind.OPEN_WORK:
            continue
        if daily_cap is not None and _proposal_budget(state, now)["honored"] >= daily_cap:
            if not capped:
                outcome.log.append(
                    f"proposals: daily cap ({daily_cap}) reached — the rest wait for tomorrow"
                )
                capped = True
            continue
        repo = str(request.payload.get("repo") or "")
        if repo not in issue_repos:
            outcome.log.append(
                f"{name}: refused to open an issue in {repo!r}, which this project does not "
                "hold issues in"
            )
            continue
        if request.payload.get("spec_only") and not spec_only_label:
            # Opened without the label, the issue would start a build nobody approved.
            outcome.log.append(
                f"{name}: refused a spec-only issue, because this project names no spec_only_label"
            )
            continue
        if _open_proposal(
            state,
            port,
            outcome,
            name,
            request.payload,
            now=now,
            store=store,
            spec_only_label=spec_only_label,
        ):
            # Tallied whether or not a ceiling is set, so turning one on later starts from a
            # number that already means something rather than from zero mid-day.
            _proposal_budget(state, now)["honored"] += 1
            honored += 1
    return honored


def _open_proposal(
    state: dict,
    port: EnginePort,
    outcome: CycleOutcome,
    name: str,
    payload: dict,
    *,
    now: datetime,
    store: StateStore,
    spec_only_label: str = "",
) -> bool:
    """Open the labelled issue a proposal asks for — exactly once (#393).

    Creating a GitHub issue is not idempotent. So the order is: establish that this key has
    never been filed, write the intent durably, act, then record the result. A crash anywhere in
    that sequence leaves an ``opening`` record, and the next wake **looks for the issue before
    creating one** — because the failure being guarded against is a duplicate, and a read that
    could not be performed is not evidence of absence.
    """
    repo = str(payload.get("repo") or "")
    key_part = str(payload.get("key") or "")
    title = str(payload.get("title") or "").strip()
    if not repo or not key_part or not title:
        return False
    cid = f"ext:{name}:{key_part}"
    if cid in state.get(_OPENED, {}):
        return False

    opening = state.setdefault(_OPENING, {})
    marker = _open_marker(cid)
    number: int | None = None
    if cid in opening:
        # `since` is when the intent was written, so the lookup can tell "not there" from "this
        # page never reached back far enough to say".
        since = str(opening[cid].get("at") or now.astimezone(UTC).isoformat())
        found, conclusive = port.find_marked_issue(repo, marker, since=since)
        if not conclusive:
            # Unknown, not absent. Creating now is the one mistake this path exists to avoid.
            outcome.log.append(f"{name}: cannot yet confirm whether {key_part} was filed")
            return False
        number = found
    else:
        opening[cid] = {"repo": repo, "key": key_part, "at": now.astimezone(UTC).isoformat()}
        # Durable before the side effect, so a crash is recoverable by the branch above.
        store.checkpoint(state)

    if number is None:
        instruction = str(payload.get("instruction") or "")
        url = str(payload.get("url") or "")
        body = "\n\n".join(part for part in (instruction, url, marker) if part)
        # The spec-only label goes on in the same write as the ownership label (#2874): an issue
        # that exists without it, even briefly, would be started as a build.
        spec_only = bool(payload.get("spec_only")) and bool(spec_only_label)
        labels = (spec_only_label,) if spec_only else ()
        number = port.open_issue(repo, title, body, labels=labels)
    if number is None:
        # The intent stays, so the next wake looks before trying again.
        outcome.log.append(f"{name}: could not open an issue for {key_part}")
        return False

    opening.pop(cid, None)
    if not opening:
        state.pop(_OPENING, None)
    state.setdefault(_OPENED, {})[cid] = {"repo": repo, "number": int(number)}
    outcome.log.append(f"{name} opened {repo}#{number} for {key_part}")
    return True


def _extension_health(
    outcome: CycleOutcome, installed: list, state: dict, *, mode: ext.Mode
) -> None:
    """Ask each health hook how it is, and record it — a skipped one degrades, never fails.

    A refused extension is asked too, though it will not answer: `invoke` refuses it and the
    refusal is what lands in the record. Skipping it because it implements no health hook would
    leave an extension the wake deliberately did not run reported as nothing at all.

    Which file resolved, and any hook it declares that the Manifest does not allow, ride along
    in the record (#2158): what runs is deployed software, so the record is where a reader sees
    which build a project picked up and what it would do if the Manifest allowed more.
    """
    for extension in installed:
        if not (extension.refused or extension.implements(ext.Hook.HEALTH)):
            continue
        response = ext.invoke(extension, ext.Hook.HEALTH, context={}, mode=mode)
        _note_extension(outcome, extension.name, response)
        record = outcome.report.extension_health.get(extension.name)
        if record is None:
            continue
        if extension.source:
            record["source"] = extension.source
        if extension.ignored_hooks:
            record["ignored_hooks"] = sorted(hook.value for hook in extension.ignored_hooks)


def _note_extension(outcome: CycleOutcome, name: str, response) -> None:
    """Fold one extension's answer into this wake's health report."""
    if response.ok:
        outcome.report.extension_health.setdefault(name, {}).update(
            response.health or {"state": "ok"}
        )
        return
    outcome.report.extension_failures += 1
    outcome.report.extension_health[name] = {"state": "degraded", "reason": response.degraded}
    outcome.log.append(f"extension skipped: {response.degraded}")
