"""Reading and steering Poller Instances from AgentDeck (#182, designed in #107).

A Poller Instance is a oneshot timer job: it wakes, works, and exits, so there is no process to
call between wakes. Everything here follows from that.

* **Observation is reading its durable artifacts** — the state file, the health record it writes
  every wake, its activation record, and systemd's view of the timer. Never a live channel.
* **Freshness is a first-class signal, separate from state.** What the instance last reported and
  whether that is still believable are different questions, so the view carries both: an overdue
  instance reads as *stale — last seen N ago*, never as current. This is what stops a dead poller
  from showing a green light.
* **Pause and resume are direct writes.** There is nothing to call between wakes, so a control
  edits the instance's state file under its lock. The next wake reads it, and a paused instance
  keeps waking (and stays fresh) but starts no work. Pressing Pause twice leaves it paused, so a
  repeated press needs no id.

Nothing here reaches into engine internals: it reads the same durable files the engine publishes,
so the two can be deployed independently (#116).
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .poller_engine import activation
from .poller_engine import health as engine_health
from .poller_engine.config import AUTO_RELEASE, DEFAULT_OWNERSHIP_LABEL
from .poller_engine.state import EnginePaths, StateStore

DEFAULT_INTERVAL_S = engine_health.DEFAULT_INTERVAL_S


def humanize_age(seconds: float) -> str:
    """An age in words an operator can scan. Every age on the poller page reads the same way."""
    count = int(seconds)
    if count < 90:
        return f"{count}s ago"
    if count < 5400:
        return f"{count // 60}m ago"
    if count < 172800:
        return f"{count // 3600}h ago"
    return f"{count // 86400}d ago"


@dataclass(frozen=True)
class TimerView:
    """What systemd says about this instance's timer, or nothing when it cannot be asked."""

    known: bool = False
    active: bool = False
    next_wake: str = ""
    last_result: str = ""

    @property
    def firing(self) -> bool:
        return self.known and self.active


@dataclass
class InstanceView:
    """One instance as an operator should see it: what it said, and whether to believe it."""

    instance: str
    exists: bool = False
    health: object | None = None            # engine_health.HealthVerdict
    paused: bool = False
    pause_note: str = ""
    last_cycle_at: str = ""
    age_seconds: float | None = None
    budget_holding: bool = False
    errors: tuple[str, ...] = ()
    last_error: str = ""
    # Which extension degraded the instance and why, in its own words (#2384).
    degraded_extensions: tuple[str, ...] = ()
    last_error_age_seconds: float | None = None
    # A failure the wakes since have not repeated is history, not the instance's state (#2384).
    last_error_current: bool = False
    version: dict = field(default_factory=dict)
    activation: dict = field(default_factory=dict)
    timer: TimerView = field(default_factory=TimerView)

    @property
    def state_label(self) -> str:
        """The one word an operator reads first, freshness included."""
        if not self.exists:
            return "not installed"
        verdict = getattr(self.health, "state", None)
        return getattr(verdict, "value", "unknown")

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(
            reason.value for reason in getattr(self.health, "reasons", ()) or ()
        )

    @property
    def freshness(self) -> str:
        return getattr(getattr(self.health, "freshness", None), "value", "unknown")

    @property
    def believable(self) -> bool:
        """Is the last report recent enough to act on?"""
        return self.freshness == "fresh"

    @property
    def last_seen(self) -> str:
        """How long ago the instance last reported, in words an operator can scan."""
        if self.age_seconds is None:
            return "never"
        return humanize_age(self.age_seconds)

    @property
    def last_error_when(self) -> str:
        """When the last failure happened, so a weeks-old one does not read as today's."""
        if self.last_error_age_seconds is None:
            return ""
        return humanize_age(max(self.last_error_age_seconds, 0))

    @property
    def summary(self) -> str:
        """A single line for a list row: state, freshness, and what it is doing."""
        parts = [self.state_label]
        if not self.exists:
            return self.state_label
        if not self.believable:
            parts.append(f"last seen {self.last_seen}")
        if self.paused:
            parts.append("paused")
        return " · ".join(parts)


def _systemctl(*args: str, runner=None) -> tuple[int, str]:
    run = runner or _run
    return run(["systemctl", "--user", *args])


def _run(command: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return done.returncode, (done.stdout or "").strip()


def timer_view(instance: str, *, runner=None) -> TimerView:
    """Ask systemd about the timer. An unanswerable question is 'unknown', not 'off'."""
    unit = f"agentdeck-poller@{instance}.timer"
    code, out = _systemctl(
        "show", unit, "-p", "ActiveState", "-p", "NextElapseUSecRealtime", "-p", "Result",
        runner=runner,
    )
    if code != 0 or not out:
        return TimerView(known=False)
    values = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line
    )
    return TimerView(
        known=True,
        active=values.get("ActiveState") == "active",
        next_wake=values.get("NextElapseUSecRealtime", ""),
        last_result=values.get("Result", ""),
    )


def read_instance(
    root: str | Path,
    instance: str,
    *,
    now: datetime | None = None,
    interval_s: int = DEFAULT_INTERVAL_S,
    runner=None,
) -> InstanceView:
    """Build one instance's view from its durable files plus systemd — never a live call."""
    moment = now or datetime.now(UTC)
    base = Path(root).expanduser() / "instances" / instance
    view = InstanceView(instance=instance, timer=timer_view(instance, runner=runner))
    state_path = base / "state.json"
    if not state_path.exists() and not (base / "state.json.bak").exists():
        return view

    store = StateStore(EnginePaths.from_base(state_path))
    try:
        state = store.read()
    except Exception:
        # A state file this AgentDeck cannot read is a fact to show, not a page that fails.
        view.errors = ("durable state could not be read",)
        view.exists = True
        return view

    view.exists = True
    record = engine_health.record(state)
    view.health = engine_health.evaluate(state, now=moment, interval_s=interval_s)
    view.paused = engine_health.is_paused(state)
    view.pause_note = engine_health.pause_note(state)
    view.last_cycle_at = str(record.get("last_cycle_at") or "")
    view.age_seconds = engine_health.age_seconds(record, moment)

    last = record.get("last_report") or {}
    view.budget_holding = bool(last.get("budget_holds"))
    view.errors = tuple(str(item) for item in last.get("errors") or ())
    view.degraded_extensions = tuple(
        f"{name}: {value.get('reason') or 'degraded'}"
        for name, value in sorted((last.get("extensions") or {}).items())
        if isinstance(value, dict) and value.get("state") == "degraded"
    )
    view.last_error = str(record.get("last_error") or "")
    view.last_error_age_seconds = engine_health.age_seconds(
        {"last_cycle_at": record.get("last_error_at")}, moment
    )
    view.last_error_current = int(record.get("error_streak") or 0) > 0
    view.activation = _read_json(base / "activation.json")
    # Read through the one normalizer, so a record the staged activation wrote before #2157
    # still shows who activated it without the project having to be applied again.
    audit = activation.audit_view(view.activation)
    view.version = {
        "contract": audit["contract"],
        "config": audit["config_fingerprint"],
        "approved_by": audit["activated_by"],
    }
    return view


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def read_all(root: str | Path, **kwargs) -> list[InstanceView]:
    """Every installed instance, in name order."""
    return [read_instance(root, name, **kwargs) for name in _instance_names(root)]


def _instance_names(root: str | Path) -> list[str]:
    instances = Path(root).expanduser() / "instances"
    try:
        return sorted(child.name for child in instances.iterdir() if child.is_dir())
    except OSError:
        return []


# --- reusing a read of the durable poller files while they have not moved ---

#: Every file an engine-published read decodes, per instance: the TOML manifest
#: and the four JSON files a ``StateStore`` load opens.
_DURABLE_INPUTS = (
    "poller.toml",
    "overlay.toml",
    "state.json",
    "state.json.bak",
    "controls.json",
    "controls.json.bak",
)

#: (reader, root) -> (fingerprint, result). One entry per reader per poller root,
#: which in practice is a handful.
_DURABLE_MEMO: dict[tuple[str, ...], tuple[tuple, object]] = {}


def _durable_signature(root: str | Path) -> tuple:
    """A (size, mtime) fingerprint of every durable file these readers decode."""
    parts: list[tuple] = []
    for instance in _instance_names(root):
        parts.extend(_instance_signature(root, instance))
    return tuple(parts)


def _instance_signature(root: str | Path, instance: str) -> tuple:
    """``_durable_signature``'s part for one instance: its own files."""
    base_root = Path(root).expanduser() / "instances"
    mine: list[tuple] = []
    for name in _DURABLE_INPUTS:
        try:
            stat = (base_root / instance / name).stat()
        except OSError:
            mine.append((instance, name))
        else:
            mine.append((instance, name, stat.st_size, stat.st_mtime_ns))
    return tuple(mine)


def _durable_memo[T](name: str, root: str | Path, compute: Callable[[str | Path], T]) -> T:
    """``compute(root)``, reused for as long as the files it reads have not moved.

    The engine republishes these on its wake cycle — every ~30 s on this host —
    with ``os.replace`` of a fresh temp file, so size and mtime move whenever the
    content does. The deck reads them far more often than that: once per attached
    chat stream every five seconds, and again on every triage refresh. Stating
    all five instances costs ~100 us against ~11 ms to decode the 1.6 MB they
    hold, so asking before decoding is the whole saving (#1362).

    Only readers whose result is immutable and content-determined may use this.
    The fingerprint is taken *before* the decode, so a view built while a writer
    was mid-publish is stored under the pre-write fingerprint and replaced by the
    next call rather than served on.
    """
    key = (name, str(Path(root).expanduser()))
    signature = _durable_signature(root)
    remembered = _DURABLE_MEMO.get(key)
    if remembered is not None and remembered[0] == signature:
        return remembered[1]  # type: ignore[return-value]
    value = compute(root)
    _DURABLE_MEMO[key] = (signature, value)
    return value


def forget_durable_reads() -> None:
    """Drop every memoized read. For tests that rewrite state within one mtime tick."""
    _DURABLE_MEMO.clear()


# --- the board as a read model: which repositories hold issues, and which instance reads them ---


@dataclass(frozen=True)
class BoardScope:
    """What one instance's published manifest says about where its issues live."""

    instance: str
    org: str = ""
    # No GitHub Project behind this instance: card status is a `status:*` label (#1481). The
    # deck reads status out of engine state either way, so this only says which fact makes the
    # scope complete — a boardless instance has no project number to be missing.
    boardless: bool = False
    project_number: int = 0
    ownership_label: str = ""
    ownership_labels: tuple[str, ...] = ()
    # The label that limits a card to a written spec (``board.spec_only_label``), or empty.
    spec_only_label: str = ""
    # ``owner/repo`` for every ``[[repos]]`` entry with ``holds_issues = true``.
    issue_repositories: tuple[str, ...] = ()
    # ``owner/repo`` of the repository declared ``holds_artifacts``, and each issue
    # repository that borrows another's checkout paired with the one it borrows (#2143).
    artifact_repositories: tuple[str, ...] = ()
    borrowed_workspaces: tuple[tuple[str, str], ...] = ()
    readable: bool = True
    state_readable: bool = True
    # The instance's ``state.json``, set only when it was read: where a deck request for one of
    # its repositories goes (#2748).
    state_path: str = ""

    @property
    def usable(self) -> bool:
        return self.readable and bool(self.org)

    @property
    def complete(self) -> bool:
        backend = self.boardless or self.project_number > 0
        return self.usable and backend and self.state_readable


@dataclass(frozen=True)
class BoardView:
    """Every instance's declared scope, plus whether the read was whole.

    ``complete`` carries the same rule ``GitContexts`` states for pull requests:
    **an incomplete lookup is not absence.** An instance whose manifest or state
    file could not be read leaves ``complete`` false, so a caller reports what it
    would have found as *unknown* rather than as nothing.
    """

    scopes: tuple[BoardScope, ...] = ()
    complete: bool = True

    @property
    def issue_repositories(self) -> tuple[str, ...]:
        """Every ``owner/repo`` any instance declares with ``holds_issues``, deduped."""
        seen: dict[str, str] = {}
        for scope in self.scopes:
            for repository in scope.issue_repositories:
                seen.setdefault(repository.lower(), repository)
        return tuple(seen[key] for key in sorted(seen))

    def managing(self, repository: str) -> tuple[BoardScope, ...]:
        """Every readable instance whose manifest holds ``repository``'s issues (#2748).

        A deck request for one of its issues goes to the instance that reads that repository.
        """
        lowered = repository.lower()
        return tuple(
            scope
            for scope in self.scopes
            if scope.state_path
            and any(item.lower() == lowered for item in scope.issue_repositories)
        )

    def ownership_label(self, repository: str) -> str:
        """The label that hands work to the engine for this repository, if declared."""
        lowered = repository.lower()
        for scope in self.scopes:
            if any(item.lower() == lowered for item in scope.issue_repositories):
                return scope.ownership_label
        return ""

    def ownership_labels(self, repository: str) -> tuple[str, ...]:
        """Every label that hands work to the engine for this repository."""
        lowered = repository.lower()
        for scope in self.scopes:
            if any(item.lower() == lowered for item in scope.issue_repositories):
                return scope.ownership_labels or (
                    (scope.ownership_label,) if scope.ownership_label else ()
                )
        return ()


@dataclass(frozen=True)
class _ManifestFacts:
    org: str = ""
    boardless: bool = False
    project_number: int = 0
    ownership_label: str = ""
    ownership_labels: tuple[str, ...] = ()
    spec_only_label: str = ""
    paused_label: str = ""
    issue_repos: tuple[str, ...] = ()
    artifact_repos: tuple[str, ...] = ()
    borrowed: tuple[tuple[str, str], ...] = ()
    # The engine merges a passed review itself (``auto_release``, #2101). Without it a pass
    # waits for a person's ``/merge``, never for the deck (#2399).
    releases_itself: bool = False
    # The repositories whose GitHub checks are the verification (``verification =
    # "github_checks"``, #2364), as the manifest names them.
    ci_repos: tuple[str, ...] = ()
    # The repositories whose review pass is the verification (``verification =
    # "review_only"``, #2729, #2832), as the manifest names them.
    review_only_repos: tuple[str, ...] = ()
    # The repositories that deploy what merges (``auto_deploys``, #2760).
    deploy_repos: tuple[str, ...] = ()
    # Every ``[[repos]]`` id, issue repository or not, as the manifest names it.
    repos: tuple[str, ...] = ()
    # ``[policy] merge_method``: how a release merges (#2895 step 2 merges in the deck).
    merge_method: str = "merge"
    # The ``[authorization]`` slot as the engine parses it (``authorization.parse_policy``):
    # who holds each tier, for the deck's own release (#2895). ``None`` when absent.
    authorization: Any = None
    # ``[staging]`` (#3180): the project's branch staging copies, and which of its
    # repositories deploy into them. ``None`` when the manifest declares none.
    staging: Any = None
    staging_repos: tuple[str, ...] = ()
    readable: bool = True


def _manifest(path: Path) -> _ManifestFacts:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, ValueError):  # TOMLDecodeError is a ValueError
        return _ManifestFacts(readable=False)
    boardless = "board" not in raw
    board = raw.get("board")
    # No [board] table is the boardless declaration (#1481): the project keeps card status in
    # `status:*` labels, and what survives the table's absence is authored at the top level.
    if boardless:
        board = raw
    elif not isinstance(board, dict):
        # Match the authoritative config parser: a present malformed board is invalid, not an
        # alternate spelling of the boardless declaration.
        return _ManifestFacts(readable=False)
    org = board.get("org")
    project_number = 0 if boardless else board.get("project_number")
    ownership = board.get("ownership_label")
    configured = [ownership] if isinstance(ownership, str) else ownership
    ownership_labels = tuple(
        item.strip()
        for item in (configured if isinstance(configured, list) else [])
        if isinstance(item, str) and item.strip()
    ) or (DEFAULT_OWNERSHIP_LABEL,)
    spec_only = board.get("spec_only_label")
    paused = board.get("paused_label")
    capabilities = raw.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    policy = raw.get("policy")
    merge_method = policy.get("merge_method") if isinstance(policy, dict) else None
    authorization = raw.get("authorization")
    repos = raw.get("repos")
    issue_repos = tuple(
        str(entry.get("id")).strip()
        for entry in (repos if isinstance(repos, list) else [])
        if isinstance(entry, dict) and entry.get("holds_issues") and entry.get("id")
    )
    return _ManifestFacts(
        org=org.strip() if isinstance(org, str) else "",
        boardless=boardless,
        project_number=(
            project_number
            if isinstance(project_number, int) and not isinstance(project_number, bool)
            else 0
        ),
        # A project that names no ownership label still has one: the engine's own
        # default is what decides which cards it picks up, so the deck must read
        # the same value rather than reporting that the project declared nothing.
        ownership_label=ownership_labels[0],
        ownership_labels=ownership_labels,
        spec_only_label=spec_only.strip() if isinstance(spec_only, str) else "",
        paused_label=paused.strip() if isinstance(paused, str) else "",
        issue_repos=issue_repos,
        releases_itself=bool(capabilities.get(AUTO_RELEASE)),
        merge_method=(
            merge_method if merge_method in {"merge", "squash", "rebase"} else "merge"
        ),
        authorization=_authorization(authorization),
        **_staging(raw.get("staging")),
        ci_repos=tuple(
            str(entry.get("id")).strip()
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict)
            and entry.get("id")
            and str(entry.get("verification") or "").strip() == "github_checks"
        ),
        review_only_repos=tuple(
            str(entry.get("id")).strip()
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict)
            and entry.get("id")
            and str(entry.get("verification") or "").strip() == "review_only"
        ),
        artifact_repos=tuple(
            str(entry.get("id")).strip()
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict) and entry.get("holds_artifacts") and entry.get("id")
        ),
        deploy_repos=tuple(
            str(entry.get("id")).strip()
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict) and entry.get("auto_deploys") is True and entry.get("id")
        ),
        borrowed=tuple(
            (str(entry.get("id")).strip(), entry["workspace"].strip())
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict)
            and entry.get("id")
            and isinstance(entry.get("workspace"), str)
            and entry["workspace"].strip()
        ),
        repos=tuple(
            str(entry.get("id")).strip()
            for entry in (repos if isinstance(repos, list) else [])
            if isinstance(entry, dict) and entry.get("id")
        ),
        readable=True,
    )


def _qualified(org: str, repo: str) -> str:
    """``owner/repo`` for a manifest repository id, which may name the repo alone."""
    return repo if "/" in repo else f"{org}/{repo}"


@dataclass(frozen=True)
class LabelTarget:
    """One issue repository and every label that hands its issues to the engine."""

    repository: str
    ownership_labels: tuple[str, ...]


def issue_label_targets(root: str | Path) -> tuple[LabelTarget, ...]:
    """Every installed instance's issue repositories with their ownership labels (#2505).

    Read from the manifests alone, never from ``state.json``: the ``gh`` shim asks this
    once per ``gh issue create`` from a fresh process, where ``read_board``'s memo is
    always empty and decoding every instance's state would cost it most of a second.
    Instances come in name order, so a repository two manifests declare keeps the first
    one's canonical label.
    """
    base_root = Path(root).expanduser() / "instances"
    found: list[LabelTarget] = []
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not facts.readable or not facts.org:
            continue
        for repo in facts.issue_repos:
            found.append(LabelTarget(_qualified(facts.org, repo), facts.ownership_labels))
    return tuple(found)


def work_repositories(root: str | Path) -> frozenset[str]:
    """Lowercased ``owner/repo`` of every repository a project declares (#2895, #3732).

    Manifest-only, like ``issue_label_targets``: the Work event router asks this for the
    repository of each delivery, and the project is named by its own manifest, never by
    what its engine last wrote.
    """
    base_root = Path(root).expanduser() / "instances"
    found: set[str] = set()
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if facts.readable and facts.org:
            found.update(_qualified(facts.org, repo).lower() for repo in facts.repos)
    return frozenset(found)


def work_start_labels(root: str | Path) -> dict[str, frozenset[str]]:
    """The ownership labels that start a Work, by issue repository.

    Lowercased ``owner/repo`` to lowercased labels (#2895 step 5b): the deck starts a
    project's Work when one of these lands on an issue no chat owns yet.
    Manifest-only, as ``work_repositories`` is.
    """
    base_root = Path(root).expanduser() / "instances"
    found: dict[str, frozenset[str]] = {}
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org):
            continue
        labels = frozenset(label.lower() for label in facts.ownership_labels)
        for repo in facts.issue_repos:
            found.setdefault(_qualified(facts.org, repo).lower(), labels)
    return found


def work_unassigned_elsewhere(root: str | Path) -> frozenset[str]:
    """The issue repositories whose unassigned issues another deck takes.

    Lowercased ``owner/repo`` of each project whose overlay sets ``[claims] unassigned =
    false`` (#3731): this deck starts only the issues assigned to its own person there. An
    overlay that cannot be read leaves its project as it was, taking unassigned issues.
    """
    base_root = Path(root).expanduser() / "instances"
    found: set[str] = set()
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org):
            continue
        try:
            with (base_root / instance / "overlay.toml").open("rb") as handle:
                claims = tomllib.load(handle).get("claims")
        except (OSError, ValueError):
            continue
        if isinstance(claims, dict) and claims.get("unassigned") is False:
            found.update(_qualified(facts.org, repo).lower() for repo in facts.issue_repos)
    return frozenset(found)


def work_hold_labels(root: str | Path) -> dict[str, frozenset[str]]:
    """The labels that keep a project's Work from starting, by issue repository.

    Lowercased ``owner/repo`` to its manifest's lowercased ``paused_label`` (#2951): a person
    put it on the issue to hold it back, so the missed-start backstop leaves it alone. Not the
    ``spec_only_label``: a spec-only issue starts an agent that writes the spec (#3134).
    Manifest-only, as ``work_start_labels`` is.
    """
    base_root = Path(root).expanduser() / "instances"
    found: dict[str, frozenset[str]] = {}
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org):
            continue
        labels = frozenset({facts.paused_label.lower()} if facts.paused_label else ())
        for repo in facts.issue_repos:
            found.setdefault(_qualified(facts.org, repo).lower(), labels)
    return found


def work_evidence_repositories(root: str | Path) -> dict[str, str]:
    """Each project's evidence repository, by lowercased issue repository (#3098).

    The ``owner/repo`` its manifest declares ``holds_artifacts`` (#1791), where a Work agent
    commits its screenshots; a project that declares none is left out. Manifest-only, as
    ``work_start_labels`` is.
    """
    base_root = Path(root).expanduser() / "instances"
    found: dict[str, str] = {}
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org and facts.artifact_repos):
            continue
        evidence = _qualified(facts.org, facts.artifact_repos[0])
        for repo in facts.issue_repos:
            found.setdefault(_qualified(facts.org, repo).lower(), evidence)
    return found


def work_accounts(root: str | Path) -> dict[str, tuple[str, ...] | None]:
    """Each project repository's worker accounts, in preference order (#2895).

    The machine-local overlay's ``[worker] account`` then ``fallback_accounts``, as the Poller
    picks its worker's account: the deck starts a project's Work on the account the
    project chose (a work project on its work subscription), not on whichever is free first.
    An empty tuple means the project names no preference. ``None`` means its overlay did not
    answer, so a caller must fail closed rather than choose an account the project may not name.
    """
    base_root = Path(root).expanduser() / "instances"
    found: dict[str, tuple[str, ...] | None] = {}
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org):
            continue
        try:
            with (base_root / instance / "overlay.toml").open("rb") as handle:
                worker = tomllib.load(handle).get("worker")
        except (OSError, tomllib.TOMLDecodeError):
            ordered = None
        else:
            if worker is None:
                ordered = ()
            elif not isinstance(worker, dict):
                ordered = None
            else:
                first = worker.get("account")
                rest = worker.get("fallback_accounts")
                valid = (first is None or isinstance(first, str)) and (
                    rest is None
                    or isinstance(rest, list)
                    and all(isinstance(key, str) for key in rest)
                )
                keys = [first] if isinstance(first, str) else []
                keys += rest if isinstance(rest, list) else []
                ordered = (
                    tuple(dict.fromkeys(key.strip() for key in keys if key.strip()))
                    if valid
                    else None
                )
        for repo in facts.issue_repos:
            found.setdefault(_qualified(facts.org, repo).lower(), ordered)
    return found


def _staging(raw: object) -> dict[str, Any]:
    """The manifest's ``[staging]`` table (#3180), or nothing when it is absent or unusable.

    ``environment_prefix`` starts the GitHub deployment environment of each branch copy,
    ``repos`` are the repositories that deploy into one, and ``links`` the copy's addresses as
    ``{label, url}`` tables whose ``url`` holds ``{slug}``.
    """
    from .staging_deploys import StagingSite

    if not isinstance(raw, dict):
        return {}
    prefix = raw.get("environment_prefix")
    repos = raw.get("repos")
    if not isinstance(prefix, str) or not prefix.strip() or not isinstance(repos, list):
        return {}
    links = tuple(
        (entry["label"].strip(), entry["url"].strip())
        for entry in raw.get("links") or ()
        if isinstance(entry, dict)
        and isinstance(entry.get("label"), str)
        and isinstance(entry.get("url"), str)
        and entry["label"].strip()
        and entry["url"].strip().startswith("https://")
    )
    return {
        "staging": StagingSite(prefix.strip(), links),
        "staging_repos": tuple(
            repo.strip() for repo in repos if isinstance(repo, str) and repo.strip()
        ),
    }


def _authorization(raw: object) -> Any:
    """The manifest's ``[authorization]`` slot parsed as the engine parses it."""
    from .poller_engine.authorization import parse_policy

    return parse_policy(raw if isinstance(raw, dict) else {})


def release_policies(root: str | Path) -> dict[str, object]:
    """Each project repository's merge policy, by lowercased ``owner/repo`` (#2895).

    What the deck's own release (``work_release``) reads in place of the engine's loaded
    manifest: ``auto_release``, the merge method, and which of the project's repositories
    are proven by their review alone or by their GitHub checks. Manifest-only, as
    ``work_repositories`` is; a repository two projects declare takes the
    first one's policy.
    """
    from .work_release import Policy

    base_root = Path(root).expanduser() / "instances"
    found: dict[str, object] = {}
    for instance in _instance_names(root):
        facts = _manifest(base_root / instance / "poller.toml")
        if not (facts.readable and facts.org):
            continue
        policy = Policy(
            auto_release=facts.releases_itself,
            merge_method=facts.merge_method,
            review_only=frozenset(
                _qualified(facts.org, repo).lower() for repo in facts.review_only_repos
            ),
            github_checks=frozenset(
                _qualified(facts.org, repo).lower() for repo in facts.ci_repos
            ),
            authorization=facts.authorization,
            spec_only_label=facts.spec_only_label,
            paused_label=facts.paused_label,
            deploys=frozenset(
                _qualified(facts.org, repo).lower() for repo in facts.deploy_repos
            ),
            staging=facts.staging,
            staging_repos=frozenset(
                _qualified(facts.org, repo).lower() for repo in facts.staging_repos
            ),
        )
        for repo in facts.repos:
            found.setdefault(_qualified(facts.org, repo).lower(), policy)
    return found


class _StateUnreadable(Exception):
    """Neither the state file nor its backup parsed — same scope as a failed read."""


def read_board(root: str | Path) -> BoardView:
    """Every installed instance's declared issue repositories.

    Reads only the durable artifacts the engine publishes — the manifest and the
    state file — so this costs no GitHub quota and works between wakes.

    Reused while those files are untouched — see ``_durable_memo``. Everything a
    ``BoardView`` carries is a frozen dataclass derived from their bytes: the
    load stamps timestamps on records it rescues from the ledger, but no board
    field is one of them, so an identical fingerprint means an identical view.
    """
    return _durable_memo("board", root, _read_board)


def _read_board(root: str | Path) -> BoardView:
    """Assemble the board from each instance's own read, in instance order.

    Each instance is decoded again only when its own files moved (#2841). The instances
    republish on their own wakes, so with a fingerprint over all of them together one or
    another had always just written, and the whole board — about twenty megabytes of JSON
    across eleven instances — was decoded again every few seconds on the chat page's path.
    """
    scopes: list[BoardScope] = []
    complete = True
    for instance in _instance_names(root):
        scope, readable = _read_board_instance_memo(root, instance)
        scopes.append(scope)
        complete = complete and readable
    return BoardView(scopes=tuple(scopes), complete=complete)


def _read_board_instance_memo(
    root: str | Path, instance: str
) -> tuple[BoardScope, bool]:
    """``_read_board_instance``, reused while that instance's own files are untouched.

    The fingerprint is taken before the decode, as in ``_durable_memo``.
    """
    key = ("board-instance", str(Path(root).expanduser()), instance)
    signature = _instance_signature(root, instance)
    remembered = _DURABLE_MEMO.get(key)
    if remembered is not None and remembered[0] == signature:
        return remembered[1]  # type: ignore[return-value]
    value = _read_board_instance(root, instance)
    _DURABLE_MEMO[key] = (signature, value)
    return value


def _read_board_instance(
    root: str | Path, instance: str
) -> tuple[BoardScope, bool]:
    """One instance's scope, and whether its manifest and state were readable."""
    base = Path(root).expanduser() / "instances" / instance
    facts = _manifest(base / "poller.toml")
    repositories = (
        tuple(
            f"{facts.org}/{repo}" if "/" not in repo else repo
            for repo in facts.issue_repos
        )
        if facts.org
        else ()
    )
    artifact_repositories = (
        tuple(_qualified(facts.org, repo) for repo in facts.artifact_repos)
        if facts.org
        else ()
    )
    borrowed_workspaces = (
        tuple(
            (_qualified(facts.org, repo), _qualified(facts.org, code))
            for repo, code in facts.borrowed
        )
        if facts.org
        else ()
    )
    if not facts.readable or not facts.org:
        scope = (
            BoardScope(
                instance=instance,
                org=facts.org,
                boardless=facts.boardless,
                project_number=facts.project_number,
                ownership_label=facts.ownership_label,
                ownership_labels=facts.ownership_labels,
                spec_only_label=facts.spec_only_label,
                issue_repositories=repositories,
                readable=facts.readable,
            )
        )
        return scope, False
    # `StateStore.read` falls back to a default when both the state file and
    # its backup are unreadable, which would present a corrupt or never-run
    # instance as a board with nothing on it. That is precisely the
    # "incomplete lookup read as absence" mistake, so the read has to say
    # whether it found anything. It now does (`last_load.found`); this used
    # to ask by decoding the same file a second time before reading it, on
    # top of the second decode inside the load itself (#1362).
    state_path = base / "state.json"
    store = StateStore(EnginePaths.from_base(state_path))
    try:
        store.read()
        if not store.last_load.found:
            raise _StateUnreadable(state_path)
    except Exception:
        scope = (
            BoardScope(
                instance=instance,
                org=facts.org,
                boardless=facts.boardless,
                project_number=facts.project_number,
                ownership_label=facts.ownership_label,
                ownership_labels=facts.ownership_labels,
                spec_only_label=facts.spec_only_label,
                issue_repositories=repositories,
                state_readable=False,
            )
        )
        return scope, False
    scope = (
        BoardScope(
            instance=instance,
            org=facts.org,
            boardless=facts.boardless,
            project_number=facts.project_number,
            ownership_label=facts.ownership_label,
            ownership_labels=facts.ownership_labels,
            spec_only_label=facts.spec_only_label,
            issue_repositories=repositories,
            artifact_repositories=artifact_repositories,
            borrowed_workspaces=borrowed_workspaces,
            state_path=str(state_path),
        )
    )
    return scope, True


# --- controls ---

def set_paused(root: str | Path, instance: str, paused: bool, *, by: str = "") -> None:
    """Pause or resume an instance by editing its state; the next wake honours it."""
    paths = EnginePaths.from_base(Path(root).expanduser() / "instances" / instance / "state.json")
    with StateStore(paths).transaction() as state:
        if paused:
            engine_health.set_paused(state, True, note=f"paused by {by or 'an operator'}")
        else:
            engine_health.set_paused(state, False)
