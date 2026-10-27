"""Launching a Poller Builder session, and choosing which account runs it (#181, from #111/#114).

The Builder is deliberately **not a wizard**. #111 made it an ordinary Deck-owned coding-agent
session running a playbook, which is why almost nothing is built here: ownership, resume across
deploys, cancel, operator answers and the transcript all come from the session model that already
exists. What this module adds is the small part that is genuinely new.

* **Which account conducts the interview.** The operator picks a primary and an ordered fallback
  list. The choice only decides who *asks the questions* — the drafted configuration is
  provider-agnostic — so the rule is simply "the first account in the operator's own order that
  has usage left". An account whose usage cannot be read is treated as usable rather than skipped:
  refusing to start because a usage probe is stale would be worse than starting on it and falling
  back later.
* **Resuming on the next account when one runs dry.** Mid-interview exhaustion is not a failure;
  it is the next account's turn, carried across by the draft on disk and the transcript. When the
  whole list is exhausted the Builder holds, resumably, like a budget hold.
* **The durable run that makes that possible (#268).** What continues is the *run*, not the
  session: a session is one **leg** of it. Handover ends the current leg and starts a new session
  on the next usable account with a continuation brief — never a resume, because the operator's
  order may mix providers and no Claude session can be resumed under Codex. The record on disk is
  what survives the launching request, so the interview is still findable after a web deploy.
* **The playbook** the session runs, which is where the guided-chat experience of #114 lives:
  inline discovery, draft, validation and handoff cards are just what the agent posts into its own
  transcript.

The Builder stops at a validated draft. It never activates anything: drafting may be done by
someone holding only Work authority, while switching a project on requires a Release Controller
(#110), and the domain is explicit that approving a draft is not activation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from .poller_engine.state import read_json_with_backup, save_json_with_backup

FULL_PCT = 95.0
PLAYBOOK = Path(__file__).resolve().parent.parent.parent / "poller" / "builder" / "PLAYBOOK.md"
RUNS_DIR = "builder"
# A project id names a file under the poller root, so it may only be a plain name.
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class AccountChoice:
    """One account the operator offered, and whether it can take the interview now."""

    key: str
    label: str
    usable: bool
    reason: str = ""


@dataclass
class LaunchPlan:
    """Who runs the Builder, and who takes over when they run dry."""

    account_key: str = ""
    fallbacks: tuple[str, ...] = ()
    considered: tuple[AccountChoice, ...] = ()
    holding: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.account_key)


def _pct(snapshot) -> float | None:
    """The fullest reported usage window, or None when usage could not be read.

    Duck-typed on purpose — the builder accepts any snapshot-shaped object — so a
    real ``UsageSnapshot`` answers with the window set its own provider reports.
    """
    if snapshot is None:
        return None
    reader = getattr(snapshot, "max_pct", None)
    if callable(reader):
        return reader()
    values = [
        value
        for value in (
            getattr(snapshot, "five_hour_pct", None),
            getattr(snapshot, "seven_day_pct", None),
            getattr(snapshot, "month_pct", None),
        )
        if isinstance(value, int | float)
    ]
    return max(values) if values else None


def usable(
    snapshot, *, full_pct: float = FULL_PCT, now: datetime | None = None
) -> tuple[bool, str]:
    """Compatibility result: usage is observed but never refuses an account (#1497)."""
    del full_pct, now
    pct = _pct(snapshot)
    if pct is None:
        return True, "usage unknown"
    return True, f"{pct:.0f}% used"


def plan_launch(
    order: list[str],
    usage: dict,
    *,
    full_pct: float = FULL_PCT,
    labels: dict | None = None,
) -> LaunchPlan:
    """Pick the account that runs the Builder, keeping the operator's own order.

    ``order`` is the primary followed by its fallbacks, exactly as the operator arranged them, so
    this never second-guesses the choice. Model usage is observational and does not hold the
    interview (#1497).
    """
    labels = labels or {}
    considered: list[AccountChoice] = []
    chosen = ""
    for key in order:
        ok, reason = usable(usage.get(key), full_pct=full_pct)
        considered.append(
            AccountChoice(key=key, label=str(labels.get(key, key)), usable=ok, reason=reason)
        )
        if ok and not chosen:
            chosen = key
    if not chosen:
        return LaunchPlan(
            considered=tuple(considered),
            holding=True,
            detail="no account was offered — the Builder will wait, resumably",
        )
    remaining = tuple(key for key in order if key != chosen)
    return LaunchPlan(
        account_key=chosen,
        fallbacks=remaining,
        considered=tuple(considered),
        detail=f"running on {labels.get(chosen, chosen)}",
    )


def next_account(order: list[str], current: str, usage: dict, *, full_pct: float = FULL_PCT) -> str:
    """Who takes over when the current account runs dry mid-interview.

    Order is preserved and the current account is skipped, so a Builder that has already exhausted
    one account never lands back on it. The draft on disk and the transcript are what carry the
    effort across, which is why this is a one-line decision rather than a migration.
    """
    seen_current = False
    for key in order:
        if key == current:
            seen_current = True
            continue
        if not seen_current:
            continue
        if usable(usage.get(key), full_pct=full_pct)[0]:
            return key
    return ""


def is_project_id(project: str) -> bool:
    """A project id is a plain name, because it names this run's file under the poller root."""
    return bool(PROJECT_RE.match(project or ""))


@dataclass(frozen=True)
class BuilderLeg:
    """One session's turn at conducting the interview.

    The leg records the *token* the session was started with rather than its key: the key does not
    exist yet when the launch returns, whereas the token is minted first and bound to the key by
    the injector as soon as the provider answers. Resolving token -> key later is exact, where
    guessing from "the newest session on that account" would sometimes pick a bystander.
    """

    account_key: str
    session_token: str = ""
    started_at: str = ""

    def to_dict(self) -> dict:
        return {
            "account_key": self.account_key,
            "session_token": self.session_token,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> BuilderLeg | None:
        """A leg we cannot read is skipped, never raised: one bad record must not hide a run."""
        if not isinstance(value, dict) or not isinstance(value.get("account_key"), str):
            return None
        return cls(
            account_key=value["account_key"],
            session_token=str(value.get("session_token") or ""),
            started_at=str(value.get("started_at") or ""),
        )


@dataclass(frozen=True)
class BuilderRun:
    """One Builder effort, across however many accounts it takes (#268).

    The run is the durable thing and the session is one leg of it, so everything needed to start
    the *next* leg lives here: the drafts it is writing, where it is writing them, and the
    operator's own account order. ``attempts`` is what makes a handover at-most-once — the control
    is rendered carrying the run's current intent id, so a double-submitted page presses the same
    handover rather than starting a second one.
    """

    project: str
    manifest: str = ""
    overlay: str = ""
    cwd: str = ""
    order: tuple[str, ...] = ()
    legs: tuple[BuilderLeg, ...] = ()
    attempts: int = 0
    holding: bool = False
    detail: str = ""
    started_at: str = ""

    @property
    def leg(self) -> BuilderLeg | None:
        """The leg holding the interview now."""
        return self.legs[-1] if self.legs else None

    @property
    def account_key(self) -> str:
        return self.leg.account_key if self.leg else ""

    @property
    def fallbacks(self) -> tuple[str, ...]:
        """Who is left after the current account, in the operator's own order."""
        keys = list(self.order)
        if self.account_key in keys:
            return tuple(keys[keys.index(self.account_key) + 1 :])
        return ()

    @property
    def handover_intent(self) -> str:
        """The stable id of the handover this run would perform next."""
        return f"{self.project}:handover:{self.attempts}"

    def next_account(self, usage: dict, *, full_pct: float = FULL_PCT) -> str:
        """Who takes over from here — always over the run's *own* recorded order.

        ``next_account`` returns nothing when the current account is absent from the order it is
        given, so a re-read of the launch form (which the operator may since have changed) would
        silently report "nobody left". The run carries the pair that belong together.
        """
        return next_account(list(self.order), self.account_key, usage, full_pct=full_pct)

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "manifest": self.manifest,
            "overlay": self.overlay,
            "cwd": self.cwd,
            "order": list(self.order),
            "legs": [leg.to_dict() for leg in self.legs],
            "attempts": self.attempts,
            "holding": self.holding,
            "detail": self.detail,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, value: dict) -> BuilderRun | None:
        project = value.get("project")
        if not isinstance(project, str) or not project:
            return None
        legs = [BuilderLeg.from_dict(item) for item in value.get("legs") or ()]
        return cls(
            project=project,
            manifest=str(value.get("manifest") or ""),
            overlay=str(value.get("overlay") or ""),
            cwd=str(value.get("cwd") or ""),
            order=tuple(key for key in value.get("order") or () if isinstance(key, str)),
            legs=tuple(leg for leg in legs if leg is not None),
            attempts=value.get("attempts") if isinstance(value.get("attempts"), int) else 0,
            holding=bool(value.get("holding")),
            detail=str(value.get("detail") or ""),
            started_at=str(value.get("started_at") or ""),
        )


def run_path(root: str | Path, project: str) -> Path:
    return Path(root).expanduser() / RUNS_DIR / f"{project}.json"


def _now(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


def _save(root: str | Path, run: BuilderRun) -> BuilderRun:
    path = run_path(root, run.project)
    save_json_with_backup(path, path.with_name(path.name + ".bak"), run.to_dict())
    return run


def read_run(root: str | Path, project: str) -> BuilderRun | None:
    """This project's run, or nothing. A file that cannot be read falls back to its backup."""
    if not is_project_id(project):
        return None
    path = run_path(root, project)
    value = read_json_with_backup(path, path.with_name(path.name + ".bak"), {})
    return BuilderRun.from_dict(value) if value else None


def read_runs(root: str | Path) -> list[BuilderRun]:
    """Every recorded run, in project order."""
    directory = Path(root).expanduser() / RUNS_DIR
    try:
        names = sorted(child.stem for child in directory.iterdir() if child.suffix == ".json")
    except OSError:
        return []
    runs = (read_run(root, name) for name in names)
    return [run for run in runs if run is not None]


def forget_run(root: str | Path, project: str) -> bool:
    """Drop a finished run. The Builder stops at a validated draft, and only the operator knows
    when that has been taken up — but a record with no end would grow the page forever."""
    if not is_project_id(project):
        return False
    path = run_path(root, project)
    gone = False
    for candidate in (path, path.with_name(path.name + ".bak")):
        try:
            candidate.unlink()
            gone = True
        except OSError:
            continue
    return gone


def record_launch(
    root: str | Path,
    project: str,
    *,
    manifest: str,
    overlay: str,
    cwd: str,
    order: list[str],
    account_key: str,
    session_token: str = "",
    now: datetime | None = None,
) -> BuilderRun:
    """Persist the launch plan, so the run outlives the request that started it."""
    stamp = _now(now)
    return _save(
        root,
        BuilderRun(
            project=project,
            manifest=manifest,
            overlay=overlay,
            cwd=cwd,
            order=tuple(order),
            legs=(
                BuilderLeg(
                    account_key=account_key, session_token=session_token, started_at=stamp
                ),
            ),
            started_at=stamp,
            detail=f"running on {account_key}",
        ),
    )


def claim_handover(root: str | Path, project: str, intent_id: str) -> BuilderRun | None:
    """Take this handover exactly once, or refuse it as the press we already performed.

    The claim is durable *before* the successor is started: a second leg on one draft is the worst
    outcome here, so an uncertain handover must read as done rather than be tried again. The
    counter only ever rises, which is what lets a genuinely new press — rendered from the updated
    run — retry a handover whose spawn was refused.
    """
    run = read_run(root, project)
    if run is None or intent_id != run.handover_intent:
        return None
    return _save(root, replace(run, attempts=run.attempts + 1, detail=""))


def hold(root: str | Path, project: str, detail: str) -> BuilderRun | None:
    """Nobody is left to take over: wait visibly rather than look finished."""
    run = read_run(root, project)
    if run is None:
        return None
    return _save(root, replace(run, holding=True, detail=detail))


def note(root: str | Path, project: str, detail: str) -> BuilderRun | None:
    """Record why a handover did not happen, without holding the run."""
    run = read_run(root, project)
    if run is None:
        return None
    return _save(root, replace(run, detail=detail))


def add_leg(
    root: str | Path,
    project: str,
    *,
    account_key: str,
    session_token: str = "",
    now: datetime | None = None,
) -> BuilderRun | None:
    """The successor has started: this account now holds the run."""
    run = read_run(root, project)
    if run is None:
        return None
    leg = BuilderLeg(
        account_key=account_key, session_token=session_token, started_at=_now(now)
    )
    return _save(
        root,
        replace(
            run,
            legs=(*run.legs, leg),
            holding=False,
            detail=f"continued on {account_key}",
        ),
    )


@dataclass
class BuilderBrief:
    """The opening message that turns an ordinary session into a Builder session."""

    project: str
    repo_path: str
    overlay_path: str
    fallbacks: tuple[str, ...] = ()
    playbook: Path = field(default=PLAYBOOK)

    def render(self) -> str:
        """The prompt the session starts from: the playbook, plus this project's specifics."""
        lines = [
            "# Build a Poller Project",
            "",
            f"- Project id: `{self.project}`",
            f"- Draft the Manifest at: `{self.repo_path}`",
            f"- Draft the Host Overlay at: `{self.overlay_path}`",
        ]
        if self.fallbacks:
            lines.append(
                "- If this account becomes unavailable, the interview continues on: "
                + ", ".join(f"`{key}`" for key in self.fallbacks)
            )
        lines += ["", "---", "", _playbook_text(self.playbook)]
        return "\n".join(lines)


@dataclass
class ContinuationBrief:
    """The message that starts the next leg of a run whose account ran dry (#268).

    A successor never resumes the previous session — the operator's order may cross providers, and
    two accounts are two homes even within one — so continuity has to be carried explicitly. What
    carries it is the draft on disk; the previous transcript is a pointer, and may be missing when
    the earlier leg's session was never bound to its token. The brief therefore leads with the
    draft and tells the successor to ask only for what is not already written there, so a handover
    does not re-interview the operator.
    """

    project: str
    repo_path: str
    overlay_path: str
    fallbacks: tuple[str, ...] = ()
    transcript_url: str = ""
    playbook: Path = field(default=PLAYBOOK)

    def render(self) -> str:
        lines = [
            f"# Continue building the Poller Project `{self.project}`",
            "",
            "The account conducting this interview became unavailable, so it continues here. This",
            "is the same Builder effort, not a new one — the draft on disk is what carries it.",
            "",
            f"- Project id: `{self.project}`",
            f"- Draft Manifest, already started: `{self.repo_path}`",
            f"- Draft Host Overlay, already started: `{self.overlay_path}`",
        ]
        if self.transcript_url:
            lines += [
                f"- The previous leg's transcript, as a fallback only: {self.transcript_url}",
                "  The drafts above are what carry this interview. Fetch the transcript only",
                "  for a specific question they leave unanswered — it is the whole previous",
                "  leg, and reading it to catch up spends that context before you start.",
            ]
        if self.fallbacks:
            lines.append(
                "- If this account becomes unavailable too, the interview continues on: "
                + ", ".join(f"`{key}`" for key in self.fallbacks)
            )
        lines += [
            "",
            "Read both drafts before you ask the operator anything, and ask only for what is not",
            "already recorded in them. Repeating questions the operator has already answered is",
            "the one thing a handover must not do.",
            "",
            "---",
            "",
            _playbook_text(self.playbook),
        ]
        return "\n".join(lines)


def _playbook_text(path: Path) -> str:
    """The playbook, or the instruction itself — a missing file degrades, never empties."""
    try:
        return path.read_text()
    except OSError:
        return (
            "Run the Poller Builder interview: read-only discovery, draft the Manifest and "
            "Overlay, validate with the poller engine's `inspect` command until the report is "
            "clean, then stop at the validated draft and hand off to a Release Controller."
        )
