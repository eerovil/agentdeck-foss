#!/usr/bin/env python3
"""Drive the #115 canary scenario against a Poller Project, recording evidence (#183).

#115 made acceptance a scripted end-to-end run that exercises every full-preset capability at
least once, with recorded evidence, *before* promotion. This is that script. It does not decide
whether the run passed on your behalf: it reads what the instance durably recorded, judges each
step against the evidence #115 named for it, and writes a single evidence file you can attach.

Three deliberate properties:

* **It never invents a step's outcome.** A step whose result cannot be determined is recorded as
  `unknown`, not as a pass. An acceptance script that guessed would be worse than no script.
* **It stops at the first blocking failure.** Later steps assume earlier ones happened, so
  continuing past a failure would produce evidence about a world that does not exist.
* **It reads, it does not re-perform.** The capabilities are exercised by the instance doing its
  real job; this script is the reader that says whether they were. Re-driving them here would
  mean a second, differently-shaped code path deciding what "a release happened" means.

The one exception is `inspect`, which runs the engine's offline check: it touches no network and
no board, so performing it is cheaper and stronger than reading a record of it.

`activate` is read from the instance's audit record, never re-run: activation is one apply
(#2157), and re-applying from an acceptance script would make the script the operator.

Evidence precedence, strongest first: the activation record, then durable state (health totals,
card records, acknowledgement files), then `cycle.log`. The log is a convenience channel rather
than the observable surface (#107), so a step proved only from the log says so in its detail.

    python3 scripts/poller_canary.py --root ~/.local/share/agentdeck/poller --instance agentdeck

`--manifest` defaults to the instance's own and should almost always be left alone. Pointing it
at the committed template (`poller/reference/poller.toml`) fails `inspect` on a perfectly healthy
instance — the template carries a placeholder `project_number` the offline check refuses on
purpose — and the run then overwrites the instance's evidence file with that false failure. This
example used to say exactly that, which is how it was found.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# A cycle older than this means the instance is not currently proving anything about itself.
FRESH_SECONDS = 300.0


@dataclass(frozen=True)
class Step:
    """One capability the canary has to exercise, and how to tell whether it did."""

    key: str
    capability: str
    what: str
    evidence: str
    blocking: bool = True


# Every full-preset capability from #115's scenario, in the order a real run reaches them.
SCENARIO: tuple[Step, ...] = (
    Step(
        "inspect",
        "configuration",
        "the offline check accepts the project's Manifest and Overlay",
        "an empty problem report",
    ),
    Step(
        "activate",
        "activation (the one gate)",
        "a Release Controller applies the project's configuration",
        "an activation record naming who applied which binding fingerprint",
    ),
    Step(
        "work",
        "work control",
        "a real staging issue driven /claude -> worker -> PR",
        "the dispatch directive, the worker's terminal marker, and the PR",
    ),
    Step(
        "authorization",
        "authorization (#110)",
        "a Work command accepted, and a /merge from a non-Release user refused",
        "both command records: one processing, one rejected",
    ),
    Step(
        "merge_review",
        "guarded release",
        "/merge review -> leased reviewer -> pass -> release",
        "the review verdict bound to its generation, then the armed auto-merge",
    ),
    Step(
        "repair",
        "merge repair",
        "one deliberately red PR repaired by a single merge-fix worker",
        "the fix record, then a green re-check",
    ),
    Step(
        "deploy",
        "deployment observation",
        "deploy-watch observes the systemd restart and /healthz after the merge",
        "a release-status of ok, and the card reaching Done",
    ),
    Step(
        "stop",
        "stop protocol",
        "a /claude stop, carried out by the Runtime",
        "the Runtime's stop receipt for the invalidated generation",
    ),
    Step(
        "recovery",
        "reconcile",
        "a worker the Runtime reported ended without a report, revived under a fresh lease",
        "the second dispatch with a new generation",
    ),
    Step(
        "artifacts",
        "artifacts",
        "/shot produces screenshots through the worker-side procedure",
        "the attached images",
        blocking=False,
    ),
    Step(
        "health",
        "observability",
        "the instance reports fresh, healthy structured health throughout",
        "the health record's cycles, error streak of zero, and freshness",
    ),
)


# --- what the instance durably recorded about itself ---

@dataclass
class Evidence:
    """Everything a Poller Instance leaves on disk, loaded once and read by the verifiers.

    A missing or unreadable artifact is empty rather than an error: the verifiers already have to
    answer "no evidence" as `unknown`, and a half-provisioned instance is exactly the case where
    that answer is the true one.
    """

    base: Path
    activation: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    controls: dict = field(default_factory=dict)
    dispatch_acks: int = 0
    log: str = ""

    @classmethod
    def load(cls, base: Path) -> Evidence:
        state = _read_json(base / "state.json")
        return cls(
            base=base,
            activation=_read_json(base / "activation.json"),
            # State is written under a schema envelope; the canary reads the payload.
            state=state.get("data", state),
            controls=_read_json(base / "controls.json").get("controls", {}),
            dispatch_acks=_count_files(base / "dispatch-acks"),
            log=_read_text(base / "cycle.log"),
        )

    @property
    def health(self) -> dict:
        return self.state.get("health", {}) or {}

    @property
    def totals(self) -> dict:
        return self.health.get("totals", {}) or {}

    def control_kinds(self) -> set[str]:
        return {rec.get("kind") for rec in self.controls.values() if isinstance(rec, dict)}


def _read_json(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _count_files(path: Path) -> int:
    try:
        return sum(1 for entry in path.iterdir() if entry.is_file())
    except OSError:
        return 0


def _from_log(ev: Evidence, needle: str) -> int:
    return ev.log.count(needle)


# --- judging one step against that evidence ---

Verdict = tuple[str, str]


def _verify_activate(ev: Evidence) -> Verdict:
    """Who applied which configuration."""
    record = ev.activation
    by = record.get("activated_by")
    at = record.get("activated_at")
    if not by or not at:
        return "unknown", "no activation record names who applied it"
    digest = record.get("fingerprint") if isinstance(record.get("fingerprint"), str) else ""
    if not digest:
        return "unknown", f"applied by {by}, but the record carries no binding fingerprint"
    return "pass", f"applied by {by} at {at}, binding digest {digest[:16]}…"


def _verify_work(ev: Evidence) -> Verdict:
    ingested = ev.totals.get("commands_ingested", 0)
    attempts = ev.totals.get("dispatch_attempts", 0)
    if not ingested or not attempts:
        return "unknown", (
            f"no work has been driven yet ({ingested} ingested, {attempts} dispatched)"
        )
    if "claude" not in ev.control_kinds():
        return "unknown", f"{attempts} dispatch(es) recorded, but no Work command among them"
    return "pass", (
        f"{ingested} command(s) ingested, {attempts} dispatch attempt(s), "
        f"{ev.dispatch_acks} acknowledgement(s) on disk; "
        f"{_from_log(ev, 'delivered')} delivery/deliveries logged"
    )


def _verify_authorization(ev: Evidence) -> Verdict:
    """A Work command accepted *and* a command refused **on authorization grounds**.

    `status: "rejected"` is far too broad to prove this. Most refusals are *admission* decisions
    — `reject-unowned`, `reject-busy`, `reject-no-linked-pr` — which say nothing about who asked;
    a Release Controller in good standing collects them routinely. Reading any rejected control
    as an authorization refusal turns ordinary operational noise into a passing acceptance step,
    which is the one failure mode this script exists to avoid.

    `_intake` records a denied command with `kind = "unauthorized"` (keeping the original kind as
    its payload). That is the only thing on record that means *this requester was not allowed*.
    """
    accepted = "claude" in ev.control_kinds()
    refused = [
        rec.get("url") or rec.get("id")
        for rec in ev.controls.values()
        if isinstance(rec, dict) and rec.get("kind") == "unauthorized"
    ]
    if accepted and refused:
        return "pass", (
            f"{len(refused)} authorization refusal(s) recorded alongside accepted Work "
            f"commands: {refused[:3]}"
        )
    if accepted:
        return "unknown", (
            "Work commands were accepted, but no refusal is on record. A refusal needs a command "
            "from someone below the Release bar, and this project has one human owner — there is "
            "no second identity to refuse. The authorization decision itself is covered by "
            "tests/test_poller_engine_authorization.py; what is missing is live proof, and "
            "manufacturing it by editing the allowlist would prove the edit, not the rule."
        )
    return "unknown", "no commands on record at all"


def _verify_merge_review(ev: Evidence) -> Verdict:
    releases = ev.totals.get("releases", 0)
    if "merge-review" not in ev.control_kinds():
        return "unknown", f"{releases} release(s), but no merge-review command on record"
    if not releases:
        return "unknown", "a merge-review was accepted, but no release followed it"
    passed, armed = _from_log(ev, "review passed"), _from_log(ev, "armed auto-merge")
    return "pass", (
        f"{releases} release(s) from a merge-review command; "
        f"{passed} review pass(es) and {armed} armed auto-merge(s) logged"
    )


def _verify_repair(ev: Evidence) -> Verdict:
    repaired = [ref for ref, rec in (ev.state.get("issues") or {}).items()
                if isinstance(rec, dict) and rec.get("fix")]
    logged = _from_log(ev, "merge-fix")
    if repaired:
        return "pass", f"repair attempt recorded on {', '.join(repaired)}"
    if logged:
        return "pass", (
            f"{logged} merge-fix dispatch(es) in cycle.log, and no card still carries the attempt "
            "— log-derived: the card record is pruned once the repair ends"
        )
    return "unknown", "no card has ever carried a repair attempt"


def _verify_deploy(ev: Evidence) -> Verdict:
    """A deploy *observed*, which is a narrower claim than a deploy gate having run.

    `health.totals.deploys` counts the cycles a release spent WAITING on its gate, not the
    observations that resolved. A deploy that reports `ok` on the first gate check never
    increments it at all, and a gate that waited forever increments it every cycle — so the
    convenient number is evidence that the capability engaged, and says nothing about whether
    deploy-watch ever answered `ok`. The proof of that is the deploy link the release records
    on its way to Done.
    """
    watch = ((ev.health.get("last_report") or {}).get("extensions") or {}).get("deploy-watch") or {}
    waited = ev.totals.get("deploys", 0)
    issues = ev.state.get("issues") or {}

    observed = {
        ref: links
        for ref, rec in issues.items()
        if isinstance(rec, dict)
        for links in [((rec.get("done_pending") or {}).get("deploy_links") or [])]
        if links
    }
    if observed:
        return "pass", (
            f"deploy observed and linked on {', '.join(observed)}: "
            f"{[link for links in observed.values() for link in links][:3]}; "
            f"deploy-watch reports {watch.get('state')} for {watch.get('service')}"
        )

    awaiting = [ref for ref, rec in issues.items()
                if isinstance(rec, dict) and rec.get("awaiting_deploy")]
    if awaiting:
        return "unknown", (
            f"a release is gating on a deploy right now ({', '.join(awaiting)}) but has not "
            f"resolved yet — deploy-watch reports {watch.get('state')}"
        )
    # The card record is pruned the moment the release finishes, so a run a minute later can no
    # longer see a deploy that certainly happened. The log outlives it: a card that reached
    # `awaiting deploy` and then `done` can only have got there through `gate_deploy`'s DONE
    # branch, because a deploy that failed arms `blocked_pending` and the card ends Blocked.
    gated = set(re.findall(r"release: (\S+) (?:merged -> )?awaiting deploy", ev.log))
    finished = {ref for ref in gated if f"release: {ref} done" in ev.log}
    if finished:
        return "pass", (
            f"{len(finished)} deploy(s) observed and released: {sorted(finished)[:3]}; "
            f"deploy-watch reports {watch.get('state')} for {watch.get('service')}, last restart "
            f"{watch.get('restarted_at')} — log-derived: the card record is pruned when it ends"
        )
    if gated:
        return "unknown", (
            f"{sorted(gated)[:3]} reached the deploy gate but no release completed after it — "
            "a deploy that fails ends the card Blocked rather than Done"
        )
    if waited:
        return "unknown", (
            f"a deploy gate has run ({waited} waiting cycle(s)), but no release-status of ok is "
            "recorded: no card carries a deploy link. The gate engaging is not the deploy being "
            "observed, and a finished card's record is pruned — re-run this while a release is "
            "in flight, or read the release's \"Production deploy(s)\" comment on the card"
        )
    return "unknown", (
        f"the deploy-watch extension reports {watch.get('state', 'nothing')}, but no release has "
        "ever gated on a deploy — a healthy extension is not an observation"
    )


def _verify_stop(ev: Evidence) -> Verdict:
    """A stop is proved by the stop command's own durable record, then by the cycle log.

    The Runtime's stop receipt is discarded with the card once the stop is finalized, so it
    cannot be the evidence (#2159). The command ledger survives that cleanup.
    """
    stopping = [ref for ref, rec in (ev.state.get("issues") or {}).items()
                if isinstance(rec, dict) and (rec.get("stopping") or rec.get("stop_requested_at"))]
    if stopping:
        return "unknown", (
            f"a stop was requested on {', '.join(stopping)} but the Runtime has not proved it yet"
        )
    stops = [
        rec for rec in ev.controls.values()
        if isinstance(rec, dict) and rec.get("kind") == "stop"
    ]
    landed = [rec for rec in stops if rec.get("status") == "done"]
    if landed:
        cards = sorted({
            f"{rec.get('repo')}#{rec.get('number')}" for rec in landed if rec.get("repo")
        })
        where = f" on {', '.join(cards)}" if cards else ""
        return "pass", (
            f"{len(landed)} stop command(s) requested and landed{where} — from the durable "
            "command ledger, because finalizing a stop discards the card's stop receipt"
        )
    if stops:
        # Requested and refused (already stopping, an idle card) is the command path working,
        # not the protocol completing. Naming which one it was beats calling it a pass.
        return "unknown", (
            f"{len(stops)} stop command(s) on record, none of which completed "
            f"(statuses: {', '.join(sorted({str(rec.get('status')) for rec in stops}))})"
        )
    confirmed = _from_log(ev, "stop confirmed") + _from_log(ev, "stop met by a finished turn")
    if confirmed:
        return "pass", (
            f"{confirmed} stop(s) confirmed — log-derived: the command records and the "
            "stop receipts a completed stop leaves behind are both cleaned up with the card"
        )
    return "unknown", "no stop has ever been requested"


def _verify_recovery(ev: Evidence) -> Verdict:
    recovered = _from_log(ev, "recovered terminal status")
    resumed = _from_log(ev, "dispatch(resume)")
    if not recovered and not resumed:
        return "unknown", "no card has ever been recovered or resumed"
    return "pass", (
        f"{recovered} terminal-status recovery/recoveries and {resumed} resume dispatch(es) "
        "— log-derived: a recovered card's record is pruned when it ends"
    )


def _verify_artifacts(ev: Evidence) -> Verdict:
    shots = ((ev.health.get("last_report") or {}).get("extensions") or {}).get("screenshots") or {}
    # A `/shot` is not its own command kind. Intake parses it as an `extension` control carrying
    # the word as its payload, and the extension answers by *requesting* ordinary worker-side
    # work — a second, derived `claude` control. Looking for a "shot" kind finds neither, which
    # is how this step stayed unknown through a `/shot` that had visibly been handled.
    asked = [
        rec for rec in ev.controls.values()
        if isinstance(rec, dict) and rec.get("kind") == "extension"
        and str(rec.get("payload") or "").strip().startswith("/shot")
    ]
    if not asked:
        loaded = f"reports {shots}" if shots else "has reported nothing"
        return "unknown", f"no /shot has been driven; the screenshots extension {loaded}"

    # The extension accepting the word is not the screenshots being taken: the derived
    # instruction is the work, and only its completion means an image was produced.
    derived = [
        rec for rec in ev.controls.values()
        if isinstance(rec, dict) and rec.get("kind") == "claude"
        and "screenshot" in str(rec.get("payload") or "").lower()
    ]
    done = [rec for rec in derived if rec.get("status") == "done"]
    if not derived:
        return "unknown", (
            f"{len(asked)} /shot accepted, but the extension requested no worker-side work — "
            "the command was taken and nothing was asked of a worker"
        )
    if not done:
        pending = sorted({str(rec.get("status")) for rec in derived})
        return "unknown", (
            f"{len(asked)} /shot accepted and {len(derived)} screenshot instruction(s) raised, "
            f"but none has completed yet (status: {', '.join(pending)})"
        )
    return "pass", (
        f"{len(asked)} /shot driven through the extension into {len(done)} completed "
        f"worker-side screenshot procedure(s); the screenshots extension reports {shots}"
    )


def _verify_health(ev: Evidence) -> Verdict:
    cycles, streak = ev.health.get("cycles", 0), ev.health.get("error_streak")
    last_at = ev.health.get("last_cycle_at")
    if not cycles or streak is None or not last_at:
        return "unknown", "the instance has recorded no health yet"
    age = _age_seconds(last_at)
    if age is None:
        return "unknown", f"{cycles} cycles, but last_cycle_at ({last_at!r}) cannot be read"
    # #107 makes freshness a first-class signal: a stale record is "unknown", never "current".
    if age > FRESH_SECONDS:
        return "unknown", (
            f"the last cycle was {age:.0f}s ago (> {FRESH_SECONDS:.0f}s), so this health record "
            "is stale — what it says was true when written, not now"
        )
    if streak:
        return "fail", f"error streak {streak}; last error {ev.health.get('last_error')!r}"
    report = ev.health.get("last_report") or {}
    return "pass", (
        f"{cycles} cycles, error streak 0, last cycle {age:.0f}s ago; "
        f"queue depth {report.get('queue_depth')}, backend ok={report.get('worker_backend_ok')}, "
        f"scan failures {ev.totals.get('scan_failures')}/{ev.totals.get('scans')}"
    )


def _age_seconds(stamp: str) -> float | None:
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds()


VERIFIERS: dict[str, Callable[[Evidence], Verdict]] = {
    "activate": _verify_activate,
    "work": _verify_work,
    "authorization": _verify_authorization,
    "merge_review": _verify_merge_review,
    "repair": _verify_repair,
    "deploy": _verify_deploy,
    "stop": _verify_stop,
    "recovery": _verify_recovery,
    "artifacts": _verify_artifacts,
    "health": _verify_health,
}


@dataclass
class Result:
    step: Step
    outcome: str = "unknown"     # pass | fail | unknown | skipped
    detail: str = ""
    at: str = ""
    carried_from: str = ""       # set when this result was proved by an earlier run


@dataclass
class Run:
    instance: str
    results: list[Result] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def passed(self) -> bool:
        blocking = [r for r in self.results if r.step.blocking]
        return bool(blocking) and all(r.outcome == "pass" for r in blocking)

    def as_json(self) -> dict:
        return {
            "instance": self.instance,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed": self.passed,
            "steps": [
                {
                    "key": r.step.key,
                    "capability": r.step.capability,
                    "what": r.step.what,
                    "evidence_expected": r.step.evidence,
                    "outcome": r.outcome,
                    "detail": r.detail,
                    "at": r.at,
                    **({"carried_from": r.carried_from} if r.carried_from else {}),
                }
                for r in self.results
            ],
        }

    def summary(self) -> str:
        lines = []
        for r in self.results:
            mark = "ok  " if r.outcome == "pass" else r.outcome
            if r.carried_from:
                mark = "ok*"
            lines.append(f"{mark:<8}{r.step.key:<14}{r.step.what}")
            # The reason an unresolved step is unresolved is the useful half of the report: it is
            # what an operator has to act on to make the next run say something different.
            if r.outcome not in ("pass", "skipped") and r.detail:
                lines.append(f"{'':<8}{'':<14}└─ {r.detail}")
        if any(r.carried_from for r in self.results):
            lines.append("  * carried forward from an earlier run — see `carried_from`")
        lines.append("PASSED" if self.passed else "NOT PASSED — see the evidence file")
        return "\n".join(lines)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def carry_forward(results: list[Result], prior: dict) -> int:
    """Keep a pass an earlier run recorded, for a step this run can no longer see.

    Several capabilities are only visible in durable state *while they are in flight*. A release
    gating on its deploy carries `awaiting_deploy`; when it resolves, `done_pending.deploy_links`
    holds the proof for as long as the card takes to finish, and then the whole record is pruned.
    Run the canary a minute later and the deploy that certainly happened is unreadable.

    That is fine for an operator watching a scenario and fatal for an acceptance artifact, which
    has to be true after the run rather than only during it. So a prior run's evidence is carried
    forward — under three rules that keep it honest:

    * **Only unknown → pass.** A step this run can still read wins, always. Nothing overwrites a
      `fail`, and a `fail` is never carried: a capability that broke must not be papered over by
      a run from before it broke.
    * **The original timestamp travels with it**, in `at` and in `carried_from`, so the evidence
      file never claims the observation happened now.
    * **It is visible.** The detail says which run proved it, so nobody reads a carried pass as a
      fresh one.

    Returns how many steps were carried.
    """
    by_key = {
        step.get("key"): step
        for step in (prior.get("steps") or [])
        if isinstance(step, dict)
    }
    carried = 0
    for result in results:
        if result.outcome != "unknown":
            continue
        previous = by_key.get(result.step.key)
        if not previous or previous.get("outcome") != "pass":
            continue
        when = previous.get("carried_from") or previous.get("at") or "an earlier run"
        result.outcome = "pass"
        result.detail = f"[carried from {when}] {previous.get('detail') or ''}".strip()
        result.at = previous.get("at") or result.at
        result.carried_from = when
        carried += 1
    return carried


def run_step(step: Step, *, engine: Path, paths: dict, evidence: Evidence, dry_run: bool) -> Result:
    """Judge one step: `inspect` is performed, every other step is read off durable evidence."""
    if dry_run:
        return Result(step, "skipped", "dry run: nothing was performed", _now())
    if step.key == "inspect":
        # The only step worth performing: offline, read-only, and cheaper than reading a record.
        code, out = _engine(engine, "inspect", paths)
        return Result(step, "pass" if code == 0 else "fail", out.strip()[-400:], _now())
    verify = VERIFIERS.get(step.key)
    if verify is None:
        # A step nobody taught the canary to read is unknown, never a pass.
        return Result(step, "unknown", f"no verifier; evidence sought: {step.evidence}", _now())
    outcome, detail = verify(evidence)
    return Result(step, outcome, detail, _now())


def _engine(engine: Path, op: str, paths: dict, *extra: str) -> tuple[int, str]:
    command = [
        str(engine), op,
        "--manifest", paths["manifest"],
        "--overlay", paths["overlay"],
    ]
    if op != "inspect":
        command += ["--binding", paths["binding"]]
    command += list(extra)
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="the host's poller root")
    parser.add_argument("--instance", required=True, help="the project id being canaried")
    # The instance's own Manifest, not the committed template. `poller/reference/poller.toml` is
    # the copy-me example and carries a placeholder `project_number` the offline check refuses on
    # purpose, so pointing the canary at it would fail `inspect` on a perfectly healthy instance.
    parser.add_argument("--manifest", help="the project's Manifest (default: the instance's own)")
    parser.add_argument("--evidence", help="where to write the evidence file")
    parser.add_argument("--dry-run", action="store_true", help="list the scenario without running")
    parser.add_argument(
        "--carry-forward", action="store_true",
        help="keep a pass an earlier evidence file recorded for a step this run cannot see",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    base = root / "instances" / args.instance
    paths = {
        "instance": args.instance,
        "manifest": str(
            Path(args.manifest).expanduser() if args.manifest else base / "poller.toml"
        ),
        "overlay": str(base / "overlay.toml"),
        "binding": str(base / "binding.json"),
    }
    # The engine from this same checkout: it deploys with AgentDeck, not separately (#2153).
    engine = Path(__file__).resolve().parent / "poller_engine.py"
    # Loaded once, so every step is judged against one consistent moment of the instance rather
    # than a state that moved under the scenario while it was being read.
    evidence = Evidence.load(base)

    run = Run(instance=args.instance, started_at=_now())
    for step in SCENARIO:
        result = run_step(
            step, engine=engine, paths=paths, evidence=evidence, dry_run=args.dry_run,
        )
        run.results.append(result)
        if result.outcome == "fail" and step.blocking:
            # Later steps assume this one happened; going on would document a fiction.
            break
    out = Path(args.evidence).expanduser() if args.evidence else base / "canary-evidence.json"
    if args.carry_forward and not args.dry_run:
        carried = carry_forward(run.results, _read_json(out))
        if carried:
            log = f"carried {carried} step(s) forward from {out}"
            print(log)
    run.finished_at = _now()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run.as_json(), indent=2) + "\n")
    print(run.summary())
    print(f"\nevidence: {out}")
    return 0 if run.passed or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
