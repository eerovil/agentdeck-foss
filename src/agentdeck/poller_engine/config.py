"""Poller Manifest and Host Overlay: the config layer (#177, designed in #108).

One Poller Project is described by two authored files and one derived artifact:

* **Manifest** — committed, portable, TOML. Names its world *semantically*: the board by
  org and project number, statuses by **role**, repos by slug with their roles inline. A
  project with no board at all leaves ``[board]`` out and names its ``org`` at the top level;
  status then lives in ``status:*`` labels on the issue (#1481, proposed). Holds
  the rules, plus typed slots whose meaning belongs to sibling tickets (authorization #110,
  capabilities #113, one ``[extensions.<name>]`` block each #112). No secrets, no machine
  paths, no GitHub node ids — those are not portable policy.
* **Host Overlay** — uncommitted, machine-local, TOML. Credentials, the worker account, and
  paths: a checkout per repo plus where durable state lives. Rides the Manifest's contract.
* **Resolved binding** — engine-written JSON, never hand-authored: the GitHub node ids that
  read-only discovery looked up at activation, frozen together with a fingerprint of what was
  activated. Regenerating it is re-activation's job (#109), never a wake's. Nothing in it
  records an extension's code: its authority is its Manifest ``hooks`` list (#2158).

The split criterion is *authored vs derived vs secret*, so each fact has exactly one home.

Three stages, matching #116's ``inspect`` / ``activate`` / ``cycle``:

1. ``inspect`` — parse, validate shapes, cross-reference the two files. No network, no
   secrets, so the Builder's draft loop can run it as often as it likes.
2. ``resolve`` — the offline check plus **read-only** discovery: look the board's node ids up
   once, verify every repository is reachable, and freeze both into the binding with a
   fingerprint of what was activated.
3. ``load`` — the hot path. Parse the two files, cross-check them, read the frozen binding,
   and verify the fingerprint. The fingerprint is what lets a wake skip revalidation: if it
   still matches, the manifest is byte-identical to the one that was validated at activation.
   Nothing is ever re-discovered per wake, and drift fails closed.

Two rules make the loader safe to build on:

* **Collect all problems, then fail closed.** Validation never stops at the first mistake:
  it returns every problem with a location and a fix, and nothing activates while the report
  is non-empty. Two files that must agree with each other and with a board produce mistakes
  in bunches, and a Builder conversation that surfaces them one wake at a time is useless.
* **One contract number, exact match, refuse and point.** The engine is pinned to a single
  contract; an older or newer file is refused with the migration to run, never interpreted.
  This is #116's "no historical compatibility matrix" applied to configuration.

Stdlib-only: the engine reads these files on every wake and stays free of third-party imports,
so parsing is ``tomllib``. Authoring and migrating them is the Builder's job (#181), not
this module's.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

# The one contract this engine speaks. Bump only with a Builder migration that rewrites
# every project's files before the new engine goes live (#116).
CONTRACT = 1

# The label a project owns cards with, when its Manifest does not name one. Defaulted rather
# than required, so adding the field needs no contract bump and an existing project that never
# mentions it keeps behaving exactly as before.
DEFAULT_OWNERSHIP_LABEL = "claude"
DEFAULT_TERMINAL_RECHECK_S = 900
DEFAULT_GRAPHQL_RESERVE = 200
DEFAULT_CORE_RESERVE = 500
# How long a timed wake may skip its live GitHub queries while the host proves the webhook
# streams quiet (#1631). The ceiling on trust in webhooks, not a poll interval: any missing
# proof — dead watcher, unread streams, no recorded full wake — restores full 30s polling.
# Zero turns the skip off entirely.
#
# Raised from 300 to 900 (#2082). At 300 this knob, and nothing else, was what produced every
# full wake on the host: seven instances × 12 forced wakes an hour, against a delivery-driven
# rate of approximately zero, because the streams really are quiet almost all of the time. The
# number is a statement about how long an at-most-once channel may be believed without a second
# opinion, and 15 minutes is still far inside the window in which a dropped delivery matters —
# anything a person is actually waiting on arrives by its own webhook wake within seconds.
DEFAULT_WEBHOOK_SKIP_S = 900

# Whether a repository lets a pull request description carry a GitHub closing keyword (#1734).
# AgentDeck's generic brief used to demand `Closes #n` from every worker, which is a repository
# convention rather than a global invariant: a repository whose own guide bans closing keywords
# handed its worker two trusted instructions it could not both obey. Declaring it here resolves
# the question once, before dispatch, instead of asking the model to pick a rule to break.
CLOSING_KEYWORDS_REQUIRED = "required"
CLOSING_KEYWORDS_PROHIBITED = "prohibited"
CLOSING_KEYWORD_POLICIES = (CLOSING_KEYWORDS_REQUIRED, CLOSING_KEYWORDS_PROHIBITED)


class Role(Enum):
    """A board status by role. The Manifest names roles; the board's own spelling is a
    display name that discovery maps onto them (case-insensitively)."""

    BACKLOG = "Backlog"
    TODO = "Todo"
    IN_PROGRESS = "In progress"
    IN_REVIEW = "In review"
    READY_TO_MERGE = "Ready to merge"
    HUMAN_REVIEW_PENDING = "Human review pending"
    DONE = "Done"
    NEEDS_INFO = "Needs More Info"
    BLOCKED = "Blocked"


# Optional columns: a board without any of them still works. Backlog simply never has anything
# parked in it. Ready to merge (#1099) is where a release attempt that did not merge comes to
# rest — a failed merge, or a review that asked for changes — instead of being mixed into
# Blocked and In review, where neither reads as "approved, just did not land". Human review
# pending (#1465) is the one column nothing automatic ever writes: a person moves a card into
# it, or a worker is explicitly asked to report it, and the engine only has to leave it alone
# afterwards. A project that has not made a column keeps exactly the old destinations, so all
# of this is additive.
OPTIONAL_ROLES = (Role.BACKLOG, Role.READY_TO_MERGE, Role.HUMAN_REVIEW_PENDING)
REQUIRED_ROLES = tuple(role for role in Role if role not in OPTIONAL_ROLES)

# What a worker's terminal marker calls each ending, and the board role that ending means.
#
# A marker carries the worker's own finish token, not a column name, and most tokens happen to
# spell their column once the underscores become spaces — which is why a reader that only maps
# column spellings looked correct for years. ``resolved`` is the one that does not: it is #1410's
# "Done, with nothing to merge", and no board has a *Resolved* column. Read as a column name it
# survives as the raw word, reaches a status write that no binding has an option for, and is
# retried on every wake forever (#1939). The mapping is here rather than beside the worker-control
# command because both the command that writes a marker and the adapter that reads one back have
# to agree about it.
FINISH_ROLES: dict[str, str] = {
    "in_review": Role.IN_REVIEW.value,
    "needs_more_info": Role.NEEDS_INFO.value,
    "blocked": Role.BLOCKED.value,
    "human_review_pending": Role.HUMAN_REVIEW_PENDING.value,
    "resolved": Role.DONE.value,
    # Not an ending a worker may report: only what a map card's ``resolved`` comes to rest as.
    # A marker still has to read back as Todo, or recovering that ending writes the Done the
    # marker literally says and closes the map.
    "todo": Role.TODO.value,
}


def role_of_finish_token(token: str | None) -> str | None:
    """The board role a terminal marker's token names, or None when it names none.

    Tolerant about spacing and case because the marker is parsed out of a comment body and the
    engine has spelled the token with spaces in the past; strict about the vocabulary, because
    the whole point is that an ending nobody defined must not become a status write.
    """
    if not token:
        return None
    return FINISH_ROLES.get(str(token).strip().casefold().replace(" ", "_"))

_MERGE_METHODS = ("merge", "squash", "rebase")

# How far a worker may deploy when a requester on its card explicitly asks it to (#2656). The
# brief used to forbid every deploy, so a one-line staging ``kubectl apply`` the owner asked for
# came back Blocked with a command for the owner to paste. Staging, dev-cluster and canary
# environments are the default; production needs the project to opt in. Pushing to the base
# branch stays forbidden at every level.
WORKER_DEPLOY_NONE = "none"
WORKER_DEPLOY_STAGING = "staging"
WORKER_DEPLOY_PRODUCTION = "production"
WORKER_DEPLOY_LEVELS = (WORKER_DEPLOY_NONE, WORKER_DEPLOY_STAGING, WORKER_DEPLOY_PRODUCTION)
DEFAULT_WORKER_MAY_DEPLOY = WORKER_DEPLOY_STAGING

# Whether a worker may merge its own card's pull request when a requester explicitly asks it to
# (#2723). The brief used to forbid it outright, so "merge it" in the worker's chat was refused
# even though the worker's account could merge. Pushing to the base branch stays forbidden.
WORKER_MERGE_ON_REQUEST = "on_request"
WORKER_MERGE_NEVER = "never"
WORKER_MERGE_LEVELS = (WORKER_MERGE_ON_REQUEST, WORKER_MERGE_NEVER)
DEFAULT_WORKER_MAY_MERGE = WORKER_MERGE_ON_REQUEST

# --- the boardless status backend (#1481) ---
#
# A project with no `[board]` table keeps each card's status in a `status:*` label on the issue
# itself. The engine core never learns about this: it still speaks role strings in and out, and
# only the GitHub adapter knows whether that role is a Project column or a label.
#
# The two sentinel ids stand where a Project's node ids would sit in the frozen binding. They are
# there so the binding says which backend was approved without changing its *shape* — adding a
# field would make every already-activated instance recompute a different derived digest and
# refuse to run, which is the #251 failure. A project that switches backend gets a different
# `project_id` and therefore has to be activated again, which is the drift we do want.
BOARDLESS_PROJECT_ID = "boardless"
BOARDLESS_STATUS_FIELD_ID = "status-labels"
STATUS_LABEL_PREFIX = "status:"


def status_label(role: Role) -> str:
    """The `status:*` label a boardless project uses for one role.

    Spelled from the role itself rather than configured: the whole point of boardless mode is
    that there is no board whose capitalization could differ, so a project has nothing to name.
    """
    return f"{STATUS_LABEL_PREFIX}{role.value.strip().lower().replace(' ', '-')}"


def status_labels() -> tuple[str, ...]:
    """Every `status:*` label, in role order. What provisioning creates in a repository."""
    return tuple(status_label(role) for role in Role)


def role_of_status_labels(labels: Iterable[str]) -> Role | None:
    """The unambiguous role in a card's labels, or None.

    Exactly one status label per card is what the engine writes. More than one needs label-event
    order to distinguish a person's new label from the stale one; callers without that evidence
    must not guess from role declaration order.
    """
    wanted = {str(name).strip().lower() for name in labels}
    found = [role for role in Role if status_label(role) in wanted]
    return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class Problem:
    """One thing wrong, where it is, and what to do about it."""

    location: str
    what: str
    fix: str

    def __str__(self) -> str:
        return f"{self.location}: {self.what} — {self.fix}"


@dataclass
class Report:
    """Every problem found, in the order found. Empty means usable."""

    problems: list[Problem] = field(default_factory=list)

    def add(self, location: str, what: str, fix: str) -> None:
        self.problems.append(Problem(location, what, fix))

    def extend(self, problems: list[Problem]) -> None:
        self.problems.extend(problems)

    @property
    def ok(self) -> bool:
        return not self.problems

    def __bool__(self) -> bool:  # a truthy report is a broken config
        return bool(self.problems)

    def __len__(self) -> int:
        return len(self.problems)

    def render(self) -> str:
        """The whole report as one message, one problem per line."""
        return "\n".join(str(problem) for problem in self.problems)


# What proves a pull request (#2364), named on every code repository (#2832). ``github_checks``
# is for a repository whose CI owns the suite: its checks on the exact head are the verification,
# and no reported check is not green.
# ``review_only`` is for a repository with no tests at all (#2729): a review pass on the exact
# head is all the proof there is. It loosens a release gate, so it is refused on a repository
# that deploys, and its pull requests merge before the others in the same release.
VERIFICATION_AGENTDECK_JOB = "agentdeck_job"
VERIFICATION_GITHUB_CHECKS = "github_checks"
VERIFICATION_REVIEW_ONLY = "review_only"
VERIFICATION_SOURCES = (
    VERIFICATION_AGENTDECK_JOB, VERIFICATION_GITHUB_CHECKS, VERIFICATION_REVIEW_ONLY,
)


@dataclass(frozen=True)
class Repo:
    """One repository and everything it does for this project, in one place."""

    id: str
    holds_issues: bool = False
    holds_code: bool = False
    auto_deploys: bool = False
    # Where this project's screenshot evidence is committed (#1791). A project that names one
    # keeps images out of the repositories it ships from: the evidence still has to live in a
    # repository, because the only embed a reviewer of a private repository can actually see is
    # a commit-pinned file on the GitHub host. At most one repository per project holds it, and
    # it needs its own Overlay checkout to push from — unlike `workspace`, which borrows one.
    holds_artifacts: bool = False
    # An issue-only repository may send its cards to a code repository's checkout. The target is
    # another Manifest repo id, never a host path: the relationship is portable, while the
    # Overlay remains the one place that says where this host cloned the code.
    workspace: str = ""
    # Whether a pull request opened here may carry `Closes #n` (#1734). Belongs to the repository
    # the pull request lands in, because that is whose guide the worker and any command guardian
    # are both reading. `required` is what every project did before this field existed.
    closing_keywords: str = CLOSING_KEYWORDS_REQUIRED
    # What proves a pull request here (#2364): ``agentdeck_job``, a worker's verification job,
    # ``github_checks``, the repository's own CI reported on GitHub for the exact head, or
    # ``review_only``, the review pass on the exact head alone (#2729).
    verification: str = VERIFICATION_AGENTDECK_JOB

    @property
    def name(self) -> str:
        return self.id

    @property
    def prohibits_closing_keywords(self) -> bool:
        return self.closing_keywords == CLOSING_KEYWORDS_PROHIBITED


@dataclass(frozen=True)
class Board:
    """The project's status backend, named semantically: no node ids live here.

    Boardless projects (#1481) are the same value with ``project_number`` zero: they have an
    owner and an ownership label like everybody else, and only differ in where a card's status
    is written. Keeping one shape means nothing downstream has to learn a second one.
    """

    org: str
    project_number: int
    status_names: dict[Role, str] = field(default_factory=dict)
    # Which label means "this engine owns the card". Optional and defaulted, because the shared
    # engine must not decide it for every project at once (#108) — and because a project that
    # never says so keeps working exactly as it did.
    ownership_label: str = DEFAULT_OWNERSHIP_LABEL
    # Every label accepted as a hand-over. ``ownership_label`` remains the first, canonical
    # spelling the engine writes; provisioning creates every accepted spelling so a live project
    # can add an alias without orphaning cards that still carry the old label (#2128).
    ownership_labels: tuple[str, ...] = ()
    # Which label limits a worker to writing a specification on the issue (#2128). Empty keeps
    # the feature off for manifests that predate it.
    spec_only_label: str = ""
    # Which label pauses a card (#2548): while an issue carries it the engine starts, revives
    # and releases nothing on it. Empty keeps the feature off.
    paused_label: str = ""

    @property
    def accepted_ownership_labels(self) -> tuple[str, ...]:
        accepted: list[str] = []
        seen: set[str] = set()
        for label in (self.ownership_label, *self.ownership_labels):
            identity = label.casefold()
            if identity not in seen:
                seen.add(identity)
                accepted.append(label)
        return tuple(accepted)

    @property
    def boardless(self) -> bool:
        """No GitHub Project: status lives in `status:*` labels on the issue itself (#1481)."""
        return self.project_number == 0

    def display_name(self, role: Role) -> str:
        """The board's own spelling for a role, defaulting to the role's canonical name."""
        return self.status_names.get(role, role.value)


@dataclass(frozen=True)
class CommandOutputPolicy:
    """Whether this project's workers see bounded command output (#2154).

    With it on, a covered shell command's full output is kept as a private artifact and the
    model is shown a bounded result it can slice further. It is a property of the worker's
    tools, independent of anything the poller orchestrates, so it rides every work dispatch
    unchanged. Off — the default and the absent-configuration answer — leaves command output
    exactly as the provider shows it.
    """

    enabled: bool = False
    visible_tool_result_chars: int = 8_000
    retention_days: int = 30
    slice_max_bytes: int = 64 * 1024
    slice_call_limit: int = 8
    no_archive_commands: tuple[str, ...] = ()

    def wire(self, *, card: str, generation: str) -> dict:
        """The policy a dispatch carries to the worker's hooks.

        The runtime and its hooks speak the original wire shape (#1484), and they are not
        restarted by an engine change, so this keeps that shape exactly: an enabled policy is
        the old ``enforce``.
        """
        return {
            "mode": "enforce" if self.enabled else "off",
            "visible_tool_result_chars": self.visible_tool_result_chars,
            "artifact_retention_days": self.retention_days,
            "artifact_slice_max_bytes": self.slice_max_bytes,
            "artifact_slice_call_limit": self.slice_call_limit,
            "no_archive_commands": list(self.no_archive_commands),
            "card": card,
            "generation": generation,
        }


_ARTIFACT_SLICE_BYTE_CEILING = 1024 * 1024
_ARTIFACT_SLICE_CALL_CEILING = 64

# Every limit, its default, and what a project is actually setting when it names one.
_COMMAND_OUTPUT_LIMITS: dict[str, tuple[int, str]] = {
    "visible_tool_result_chars": (8_000, "how much of one command result the model may see"),
    "retention_days": (30, "how long a full command artifact is retained"),
    "slice_max_bytes": (64 * 1024, "the largest bounded artifact slice read"),
    "slice_call_limit": (8, "how many slices one artifact may serve to its worker"),
}
_COMMAND_OUTPUT_CEILINGS = {
    "slice_max_bytes": _ARTIFACT_SLICE_BYTE_CEILING,
    "slice_call_limit": _ARTIFACT_SLICE_CALL_CEILING,
}

@dataclass(frozen=True)
class Manifest:
    """The authored, portable half: what this project is and how it behaves."""

    contract: int
    board: Board
    repos: tuple[Repo, ...] = ()
    merge_method: str = "merge"
    worker_cap: int = 10
    terminal_recheck_s: int = DEFAULT_TERMINAL_RECHECK_S
    graphql_reserve: int = DEFAULT_GRAPHQL_RESERVE
    core_reserve: int = DEFAULT_CORE_RESERVE
    webhook_skip_s: int = DEFAULT_WEBHOOK_SKIP_S
    # How far a worker may deploy when a requester asks on its card (#2656).
    worker_may_deploy: str = DEFAULT_WORKER_MAY_DEPLOY
    # Whether a worker may merge its card's pull request when a requester asks (#2723).
    worker_may_merge: str = DEFAULT_WORKER_MAY_MERGE
    authorization: dict = field(default_factory=dict)   # shape here, rules in #110
    capabilities: dict = field(default_factory=dict)    # shape here, set in #113
    extensions: dict = field(default_factory=dict)      # one block per extension (#112)
    # Each extension's hook allow-list, from the ``hooks`` key of its block (#2158). It is policy
    # the engine enforces rather than a setting the extension reads, so it is lifted out of the
    # block the program is handed. An extension whose block states none is absent here.
    extension_hooks: dict = field(default_factory=dict)  # name -> tuple of hook names
    # The issue-template titles this project treats as "no title was written" (#338). Naming
    # them here rather than in the engine is the same call ``ownership_label`` is: it is a fact
    # about one project's templates, and a shared engine must not rewrite titles on behalf of a
    # project that never asked. Empty — the default — repairs nothing.
    placeholder_titles: tuple[str, ...] = ()
    # Ceilings on extension-proposed work (#371). An extension bounds what it *asks* for, but
    # a budget guard an extension enforces on itself is trusting the thing being bounded, so
    # the engine owns these. Both default to "no ceiling of their own": a project that never
    # named one keeps behaving exactly as it did, and proposals stay bounded by ``worker_cap``
    # like every other card.
    proposal_daily_cap: int | None = None
    proposal_worker_cap: int | None = None
    # The control guide appended to every engine comment, already rendered (#373). Empty — the
    # default — appends nothing, so a project that never wrote a guide keeps posting bare
    # comments. The engine holds no prose of its own: the wording, and the language it is
    # written in, are facts about the project's own board.
    comment_guide: str = ""
    # Whether this project's workers see bounded command output (#2154). Off unless it asks.
    command_output: CommandOutputPolicy = field(default_factory=CommandOutputPolicy)

    def repo(self, repo_id: str) -> Repo | None:
        return next((repo for repo in self.repos if repo.id == repo_id), None)

    def issue_repos(self) -> tuple[str, ...]:
        return tuple(repo.id for repo in self.repos if repo.holds_issues)

    def code_repos(self) -> tuple[str, ...]:
        return tuple(repo.id for repo in self.repos if repo.holds_code)

    def auto_deploy_repos(self) -> frozenset[str]:
        return frozenset(repo.id for repo in self.repos if repo.auto_deploys)

    def ci_verified_repos(self) -> frozenset[str]:
        """The repositories whose GitHub checks are the verification (#2364)."""
        return frozenset(
            repo.id for repo in self.repos if repo.verification == VERIFICATION_GITHUB_CHECKS
        )

    def review_only_repos(self) -> frozenset[str]:
        """The repositories whose review pass alone proves a pull request (#2729)."""
        return frozenset(
            repo.id for repo in self.repos if repo.verification == VERIFICATION_REVIEW_ONLY
        )

    def artifact_repo(self) -> str:
        """The repository this project commits screenshot evidence to, if it named one."""
        return next((repo.id for repo in self.repos if repo.holds_artifacts), "")


@dataclass(frozen=True)
class Overlay:
    """The machine-local half: credentials, the worker account, and paths."""

    contract: int
    state_path: str
    checkouts: dict[str, str] = field(default_factory=dict)
    account: str = ""
    # Accounts this project may run work on, in the order it should fall back through. The
    # primary account is always first, so a host that names none still has a one-entry list.
    accounts: tuple[str, ...] = ()
    # Optional per-account budget ceilings. Accounts not named here keep the engine default.
    # The keys are provider-qualified account identities from ``accounts``.
    usage_ceilings: dict[str, float] = field(default_factory=dict)
    # Optional per-account "spend the window this full before it resets" targets (#1965).
    # Accounts not named here use the engine default; naming one with ``0`` opts it out.
    usage_targets: dict[str, float] = field(default_factory=dict)
    # Where the deck publishes provider-qualified usage snapshots. Machine-local, so the
    # Overlay states it; absent means the engine cannot see usage and never blocks a wake.
    usage_cache: str = ""
    credentials: dict = field(default_factory=dict)
    log_path: str = ""
    env_files: tuple[str, ...] = ()
    # A folder holding extension programs this host shares across its instances (#377). Without
    # it, an extension is only findable under one of the project's own code checkouts — which
    # works for a project whose checkout happens to be AgentDeck itself, and forces every other
    # project to vendor a copy of shared code. It is a machine path, so it lives here.
    extensions_dir: str = ""
    # The machine-local half of each extension's settings, one block per extension (#386).
    # An extension's schema lives in the Manifest, but a credential or a path in it cannot: those
    # are the two things this file exists to hold. Merged over the Manifest's block at load, so an
    # extension sees one settings dict and needs no idea the split happened.
    extensions: dict = field(default_factory=dict)


def _read_toml(path: Path, label: str, report: Report) -> dict | None:
    """Parse one TOML file, turning every failure into a problem rather than an exception."""
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        report.add(label, "file not found", f"create {path}")
    except IsADirectoryError:
        report.add(label, f"{path} is a directory, not a file", "point at the TOML file")
    except tomllib.TOMLDecodeError as exc:
        report.add(label, f"is not valid TOML ({exc})", "fix the syntax and try again")
    except OSError as exc:
        report.add(label, f"could not be read ({exc})", "check permissions")
    return None


def _contract(raw: dict, label: str, report: Report) -> int:
    value = raw.get("contract")
    if not isinstance(value, int) or isinstance(value, bool):
        report.add(f"{label}.contract", "missing or not an integer", f"set contract = {CONTRACT}")
        return 0
    if value != CONTRACT:
        # Never interpret a foreign shape: say which way it is wrong and what to run.
        direction = "older than" if value < CONTRACT else "newer than"
        report.add(
            f"{label}.contract",
            f"contract {value} is {direction} this engine's contract {CONTRACT}",
            "run the Builder migration for this project before activating",
        )
    return value


def parse_manifest(raw: dict, report: Report) -> Manifest:
    """Turn raw Manifest TOML into a typed value, recording every shape problem."""
    contract = _contract(raw, "manifest", report)
    board = _parse_board(raw, report)
    repos = _parse_repos(raw.get("repos"), report)
    _retired_poller_phases(raw, report)

    policy = raw.get("policy") if isinstance(raw.get("policy"), dict) else {}
    merge_method = policy.get("merge_method", "merge")
    if merge_method not in _MERGE_METHODS:
        report.add(
            "manifest.policy.merge_method",
            f"{merge_method!r} is not a GitHub merge method",
            f"use one of {', '.join(_MERGE_METHODS)}",
        )
        merge_method = "merge"
    worker_cap = policy.get("worker_cap", 10)
    if not isinstance(worker_cap, int) or isinstance(worker_cap, bool) or worker_cap < 1:
        report.add(
            "manifest.policy.worker_cap",
            "must be a positive integer",
            "set worker_cap to the most workers this host should run at once",
        )
        worker_cap = 10

    terminal_recheck_s = policy.get("terminal_recheck_s", DEFAULT_TERMINAL_RECHECK_S)
    if (
        not isinstance(terminal_recheck_s, int)
        or isinstance(terminal_recheck_s, bool)
        or terminal_recheck_s < 1
    ):
        report.add(
            "manifest.policy.terminal_recheck_s",
            "must be a positive integer",
            "set how many seconds an idle terminal card may wait between live checks",
        )
        terminal_recheck_s = DEFAULT_TERMINAL_RECHECK_S

    graphql_reserve = policy.get("graphql_reserve", DEFAULT_GRAPHQL_RESERVE)
    if (
        not isinstance(graphql_reserve, int)
        or isinstance(graphql_reserve, bool)
        or graphql_reserve < 0
    ):
        report.add(
            "manifest.policy.graphql_reserve",
            "must be a non-negative integer",
            "set the shared GraphQL points the poller must leave unused",
        )
        graphql_reserve = DEFAULT_GRAPHQL_RESERVE

    core_reserve = policy.get("core_reserve", DEFAULT_CORE_RESERVE)
    if (
        not isinstance(core_reserve, int)
        or isinstance(core_reserve, bool)
        or core_reserve < 0
    ):
        report.add(
            "manifest.policy.core_reserve",
            "must be a non-negative integer",
            "set the shared REST core requests the poller must leave unused",
        )
        core_reserve = DEFAULT_CORE_RESERVE

    webhook_skip_s = policy.get("webhook_skip_s", DEFAULT_WEBHOOK_SKIP_S)
    if (
        not isinstance(webhook_skip_s, int)
        or isinstance(webhook_skip_s, bool)
        or webhook_skip_s < 0
    ):
        report.add(
            "manifest.policy.webhook_skip_s",
            "must be a non-negative integer",
            "set how many seconds a webhook-quiet wake may skip live GitHub queries, "
            "or 0 to poll fully every wake",
        )
        webhook_skip_s = DEFAULT_WEBHOOK_SKIP_S

    worker_may_deploy = policy.get("worker_may_deploy", DEFAULT_WORKER_MAY_DEPLOY)
    if worker_may_deploy not in WORKER_DEPLOY_LEVELS:
        report.add(
            "manifest.policy.worker_may_deploy",
            f"{worker_may_deploy!r} is not a deploy level",
            f"use one of {', '.join(WORKER_DEPLOY_LEVELS)}",
        )
        worker_may_deploy = DEFAULT_WORKER_MAY_DEPLOY

    worker_may_merge = policy.get("worker_may_merge", DEFAULT_WORKER_MAY_MERGE)
    if worker_may_merge not in WORKER_MERGE_LEVELS:
        report.add(
            "manifest.policy.worker_may_merge",
            f"{worker_may_merge!r} is not a merge level",
            f"use one of {', '.join(WORKER_MERGE_LEVELS)}",
        )
        worker_may_merge = DEFAULT_WORKER_MAY_MERGE

    placeholder_titles = _parse_placeholder_titles(policy.get("placeholder_titles", ()), report)
    proposal_daily_cap, proposal_worker_cap = _parse_proposals(policy.get("proposals"), report)

    extensions = raw.get("extensions", {})
    if not isinstance(extensions, dict):
        report.add(
            "manifest.extensions",
            "is not a table of extension blocks",
            "write one [extensions.<name>] table per enabled extension",
        )
        extensions = {}
    blocks: dict = {}
    extension_hooks: dict = {}
    for name, block in extensions.items():
        if not isinstance(block, dict):
            report.add(
                f"manifest.extensions.{name}",
                "is not a table",
                f"write it as [extensions.{name}] with its own settings",
            )
            continue
        settings = dict(block)
        if EXTENSION_HOOKS_KEY in settings:
            hooks = _parse_extension_hooks(name, settings.pop(EXTENSION_HOOKS_KEY), report)
            if hooks is not None:
                extension_hooks[name] = hooks
        blocks[name] = settings

    capabilities = _table(raw, "capabilities", report, "manifest")
    capabilities = _checked_auto_release(capabilities, report)
    return Manifest(
        contract=contract,
        board=board,
        repos=repos,
        merge_method=merge_method,
        worker_cap=worker_cap,
        terminal_recheck_s=terminal_recheck_s,
        graphql_reserve=graphql_reserve,
        core_reserve=core_reserve,
        webhook_skip_s=webhook_skip_s,
        worker_may_deploy=worker_may_deploy,
        worker_may_merge=worker_may_merge,
        authorization=_table(raw, "authorization", report, "manifest"),
        capabilities=capabilities,
        extensions=blocks,
        extension_hooks=extension_hooks,
        placeholder_titles=placeholder_titles,
        proposal_daily_cap=proposal_daily_cap,
        proposal_worker_cap=proposal_worker_cap,
        comment_guide=_parse_comment_guide(raw.get("comments"), capabilities, report),
        command_output=_parse_command_output(raw.get("command_output"), report),
    )


def _retired_poller_phases(raw: dict, report: Report) -> None:
    """Refuse the ``[poller_phases]`` table #2154 retired rather than quietly ignoring it.

    Its one surviving meaning was bounded command output, now ``[command_output]``. Ignoring a
    leftover table would switch that output off without a word, which is worse than refusing.
    """
    if "poller_phases" in raw:
        report.add(
            "manifest.poller_phases",
            "was retired (#2154)",
            "move its output settings to [command_output] and apply this Manifest again",
        )


# The key in an ``[extensions.<name>]`` block that is the project's hook allow-list (#2158).
EXTENSION_HOOKS_KEY = "hooks"


def _parse_extension_hooks(name: str, value: object, report: Report) -> tuple[str, ...] | None:
    """One extension's hook allow-list as sorted hook names, or None when it is unusable."""
    from .extensions import parse_hooks  # the hook vocabulary lives with the protocol

    hooks, wrong = parse_hooks(value)
    if hooks is None:
        report.add(
            f"manifest.extensions.{name}.{EXTENSION_HOOKS_KEY}",
            wrong,
            "list the hooks this project allows it, e.g. hooks = [\"validate-config\", \"health\"]",
        )
        return None
    return tuple(sorted(hook.value for hook in hooks))


# Merging on the three release bits is the guarded release with the reviewer's own pass
# standing in for the person who would have typed `/merge` (#2101), so it can only exist where
# both halves of that sentence are switched on. Refused offline, the same cross-reference the
# comment guide gets: a Manifest that says a project merges itself, on a project whose release
# controls are off, is drift somebody has to find at activation rather than while wondering why
# nothing ships.
AUTO_RELEASE = "auto_release"
_AUTO_RELEASE_CAPABILITIES = ("guarded_release", "merge_review")


# The engine's own verify turn, and the rebase turn beside it, without the merge (#2375). A
# project whose releases a person still starts — one that ships to a live store — needs its
# reviewed heads verified all the same, and before this only a project that merged on its own
# bits ever got the turn. Unset, it follows `auto_release`, so a project that says nothing keeps
# exactly the behaviour it had.
VERIFY_TURN = "verify_turn"


def verify_turn(capabilities: Mapping) -> bool:
    """Whether the engine reads the release bits to ask for rebase and verify turns (#2375).

    ``auto_release`` implies it: merging on the bits needs the bits read. ``verify_turn = false``
    beside ``auto_release = true`` is not a way to merge unverified, so it is ignored there.
    """
    return bool(capabilities.get(AUTO_RELEASE) or capabilities.get(VERIFY_TURN))


def _checked_auto_release(capabilities: dict, report: Report) -> dict:
    """Switch off a bits-driven release the project's other capabilities do not permit."""
    if not capabilities.get(AUTO_RELEASE):
        return capabilities
    missing = [
        name for name in _AUTO_RELEASE_CAPABILITIES if not capabilities.get(name)
    ]
    if not missing:
        return capabilities
    report.add(
        f"manifest.capabilities.{AUTO_RELEASE}",
        f"needs capabilities this project has not enabled: {', '.join(missing)}",
        f"set {' and '.join(f'capabilities.{name} = true' for name in missing)}, "
        f"or set capabilities.{AUTO_RELEASE} = false",
    )
    return {**capabilities, AUTO_RELEASE: False}


# Each guide line is keyed by the control it explains, and each control belongs to a capability
# the project either enabled or did not (#113). Keying rather than free prose is what lets the
# offline check catch a guide that advertises a control its project turned off — the same
# cross-reference drift the Manifest already refused for repos and Sentry projects.
_GUIDE_LINES = {
    "work": "work_control",
    "stop": "work_control",
    "merge": "guarded_release",
    "merge_review": "merge_review",
    "shot": "artifacts",
}


def _parse_comment_guide(raw: object, capabilities: dict, report: Report) -> str:
    """The rendered control guide for this project's comments, or nothing at all (#373).

    A line whose capability is switched off is **reported, not quietly dropped**. Silently
    omitting it would leave an operator reading their own configured wording on a board that
    never shows it, which is the same trade the placeholder-title list already refuses: a
    complaint before activation beats half a config nobody checked.
    """
    if raw is None:
        return ""
    if not isinstance(raw, dict):
        report.add(
            "manifest.comments",
            "is not a table",
            "write it as [comments.guide] with a heading and one line per control",
        )
        return ""
    guide = raw.get("guide")
    if guide is None:
        return ""
    if not isinstance(guide, dict):
        report.add(
            "manifest.comments.guide",
            "is not a table",
            "write it as [comments.guide] with a heading and one line per control",
        )
        return ""

    for key in sorted(set(guide) - {"heading", *_GUIDE_LINES}):
        report.add(
            f"manifest.comments.guide.{key}",
            "is not a control this engine offers",
            f"use a heading or one of: {', '.join(sorted(_GUIDE_LINES))}",
        )
    lines = []
    for key, capability in _GUIDE_LINES.items():
        value = guide.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            report.add(
                f"manifest.comments.guide.{key}",
                "must be a non-empty string",
                f"write the line explaining {key}, or leave it out",
            )
            continue
        if not capabilities.get(capability):
            report.add(
                f"manifest.comments.guide.{key}",
                f"explains a control this project has not enabled ({capability})",
                f"enable capabilities.{capability}, or remove this line from the guide",
            )
            continue
        lines.append(f"- {value.strip()}")

    heading = guide.get("heading")
    if heading is not None and (not isinstance(heading, str) or not heading.strip()):
        report.add(
            "manifest.comments.guide.heading",
            "must be a non-empty string",
            "write the line introducing the guide, or leave it out",
        )
        heading = None
    if not lines:
        return ""
    # Folded under its heading (#2498): it repeats on every comment, and the people who read the
    # board already know the commands. GitHub renders no Markdown inside `<summary>`, so the
    # heading's bold and code spans become their HTML forms there.
    summary = heading.strip() if isinstance(heading, str) and heading.strip() else "Commands"
    return "\n".join(
        ["---", f"<details><summary>{_summary_html(summary)}</summary>", "", *lines, "",
         "</details>"]
    )


def _summary_html(text: str) -> str:
    """``**bold**`` and `` `code` `` as HTML, for a `<summary>` GitHub renders no Markdown in."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def _parse_command_output(raw: object, report: Report) -> CommandOutputPolicy:
    """The command-output policy, or the off default a project that never asked keeps.

    A named-but-wrong value is reported and defaulted rather than silently honored: a project
    that thinks it bounded its workers' output and did not is the case where carrying on
    quietly is the wrong answer.
    """
    if raw is None:
        return CommandOutputPolicy()
    if not isinstance(raw, dict):
        report.add(
            "manifest.command_output",
            "is not a table",
            "write it as [command_output] with enabled and its limits",
        )
        return CommandOutputPolicy()

    known = {"enabled", "no_archive_commands", *_COMMAND_OUTPUT_LIMITS}
    for key in sorted(set(raw) - known):
        report.add(
            f"manifest.command_output.{key}",
            "is not a command-output setting this engine offers",
            f"use one of: {', '.join(sorted(known))}",
        )

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        report.add(
            "manifest.command_output.enabled",
            "must be true or false",
            "set enabled = true to bound workers' command output",
        )
        enabled = False

    values: dict[str, int] = {}
    for key, (default, what) in _COMMAND_OUTPUT_LIMITS.items():
        value = raw.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            report.add(
                f"manifest.command_output.{key}",
                "must be a positive integer",
                f"set {key} to {what}",
            )
            value = default
        ceiling = _COMMAND_OUTPUT_CEILINGS.get(key)
        if ceiling is not None and value > ceiling:
            report.add(
                f"manifest.command_output.{key}",
                f"must be no larger than {ceiling}",
                "keep repeated artifact reads bounded",
            )
            value = default
        values[key] = value

    no_archive_raw = raw.get("no_archive_commands", [])
    no_archive_commands: tuple[str, ...] = ()
    if not isinstance(no_archive_raw, list) or not all(
        isinstance(item, str) and item.strip() for item in no_archive_raw
    ):
        report.add(
            "manifest.command_output.no_archive_commands",
            "must be an array of non-empty command fragments",
            "list commands whose output must never be archived",
        )
    else:
        no_archive_commands = tuple(item.strip() for item in no_archive_raw)

    return CommandOutputPolicy(
        enabled=enabled, no_archive_commands=no_archive_commands, **values
    )


def _parse_proposals(raw: object, report: Report) -> tuple[int | None, int | None]:
    """The ceilings on extension-proposed work, or no ceilings at all (#371).

    These sit in ``[policy.proposals]`` rather than in the proposing extension's own block for
    two reasons: the core loader deliberately knows nothing about what is inside an extension
    block (#112), and a cap is a gate — the one thing an extension may never enforce on itself.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        report.add(
            "manifest.policy.proposals",
            "is not a table",
            "write it as [policy.proposals] with daily_cap and/or worker_cap",
        )
        return None, None

    caps: list[int | None] = []
    for key, what in (
        ("daily_cap", "how many proposals may be honored in one day"),
        ("worker_cap", "how many proposal-driven workers may run at once"),
    ):
        value = raw.get(key)
        if value is None:
            caps.append(None)
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            report.add(
                f"manifest.policy.proposals.{key}",
                "must be a positive integer",
                f"set {key} to {what}, or leave it out for no ceiling",
            )
            caps.append(None)
            continue
        caps.append(value)
    return caps[0], caps[1]


def _parse_placeholder_titles(raw: object, report: Report) -> tuple[str, ...]:
    """The template titles this project wants repaired, or nothing at all (#338).

    A malformed list is refused rather than filtered down to its usable entries: the setting
    decides which titles the engine rewrites on GitHub, and half of a list nobody checked is a
    worse answer than an offline complaint the operator can read before activating.
    """
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        if raw in ((), None):
            return ()
        report.add(
            "manifest.policy.placeholder_titles",
            "is not a list of titles",
            'write it as placeholder_titles = ["Requested changes"]',
        )
        return ()
    titles = [str(value).strip() for value in raw if isinstance(value, str) and str(value).strip()]
    if len(titles) != len(list(raw)):
        report.add(
            "manifest.policy.placeholder_titles",
            "contains an entry that is not a title",
            "list each issue-template title exactly as the template writes it",
        )
        return ()
    return tuple(titles)


def _table(raw: dict, key: str, report: Report, label: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        report.add(f"{label}.{key}", "is not a table", f"write it as [{key}]")
        return {}
    return value


def _parse_board(manifest_raw: dict, report: Report) -> Board:
    """The project's status backend, from `[board]` or — when there is none — the top level.

    An absent `[board]` table is not an omission, it is the boardless declaration (#1481): the
    project keeps status in `status:*` labels and has no Project to name. What survives the
    table's absence moves up a level, because `org` and `ownership_label` are facts about the
    project's world rather than about a board.
    """
    raw = manifest_raw.get("board")
    if raw is None:
        return _parse_boardless(manifest_raw, report)
    if not isinstance(raw, dict):
        report.add(
            "manifest.board",
            "is not a table",
            "write it as [board] with org and project_number, "
            "or drop it entirely to keep status in status:* labels",
        )
        return Board(org="", project_number=0)
    for key in ("org", "ownership_label", "spec_only_label", "paused_label"):
        if key in manifest_raw:
            # Two spellings of one fact is a mistake somebody has to notice here rather than at
            # 3am, when the two disagree and only one of them is the one the engine reads.
            report.add(
                f"manifest.{key}",
                "is a boardless key, but this project has a [board] table",
                f"keep {key} under [board], or drop [board] to run boardless",
            )
    org = raw.get("org")
    if not isinstance(org, str) or not org:
        report.add("manifest.board.org", "missing", "set org to the GitHub owner")
        org = ""
    number = raw.get("project_number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        report.add(
            "manifest.board.project_number",
            "missing or not a positive integer",
            "set project_number to the board's number in its URL",
        )
        number = 0

    names: dict[Role, str] = {}
    statuses = raw.get("statuses", {})
    if not isinstance(statuses, dict):
        report.add(
            "manifest.board.statuses",
            "is not a table",
            "write [board.statuses] mapping each role to the board's own spelling",
        )
        statuses = {}
    by_role = {role.value.lower().replace(" ", "_"): role for role in Role}
    for key, value in statuses.items():
        role = by_role.get(str(key).lower().replace(" ", "_"))
        if role is None:
            report.add(
                f"manifest.board.statuses.{key}",
                "is not a status role this engine knows",
                f"use one of {', '.join(r.value for r in Role)}",
            )
            continue
        if not isinstance(value, str) or not value:
            report.add(
                f"manifest.board.statuses.{key}",
                "must be the board's display name for that role",
                "give the exact column name, or drop the line to use the role name",
            )
            continue
        names[role] = value
    labels, spec_only = _parse_labels(raw, "manifest.board", report)
    paused = _parse_paused_label(raw, "manifest.board", report, (*labels, spec_only))
    return Board(
        org=org,
        project_number=number,
        status_names=names,
        ownership_label=labels[0],
        ownership_labels=labels,
        spec_only_label=spec_only,
        paused_label=paused,
    )


def _parse_boardless(raw: dict, report: Report) -> Board:
    """A project with no GitHub Project: owner and labels at the top level, status in labels.

    There is nothing to say about status spellings here. Boardless roles are spelled from the
    role itself (``status_label``), so unlike a board — whose columns a person named and may
    rename — there is no wording that could drift out of step with reality.
    """
    org = raw.get("org")
    if not isinstance(org, str) or not org.strip():
        report.add(
            "manifest.org",
            "missing",
            "set org to the GitHub owner, or add [board] with org and project_number",
        )
        org = ""
    labels, spec_only = _parse_labels(raw, "manifest", report)
    paused = _parse_paused_label(raw, "manifest", report, (*labels, spec_only))
    semantic_labels = (
        *(("ownership_label", label) for label in labels),
        ("spec_only_label", spec_only),
        ("paused_label", paused),
    )
    for key, value in semantic_labels:
        if value.lower().startswith(STATUS_LABEL_PREFIX):
            # The status labels are this project's board. A control label sharing that namespace
            # would be swapped away by the next status write, silently disowning the card.
            report.add(
                f"manifest.{key}",
                f"starts with {STATUS_LABEL_PREFIX!r}, which is reserved for card status",
                "give it a label outside the status: namespace",
            )
    return Board(
        org=org.strip(),
        project_number=0,
        ownership_label=labels[0],
        ownership_labels=labels,
        spec_only_label=spec_only,
        paused_label=paused,
    )


def _parse_labels(raw: dict, location: str, report: Report) -> tuple[tuple[str, ...], str]:
    """The ownership and specification labels in a Manifest.

    ``automerge_label`` is no longer read (#2502). An installed Manifest that still names it
    keeps loading: ignoring it switches the label's merge path off, which is the point, while
    refusing it would halt every instance still running such a Manifest until it is applied
    again.
    """
    configured = raw.get("ownership_label", DEFAULT_OWNERSHIP_LABEL)
    values = [configured] if isinstance(configured, str) else configured
    valid = (
        isinstance(values, list)
        and bool(values)
        and all(isinstance(value, str) and value.strip() for value in values)
    )
    labels = tuple(value.strip() for value in values) if valid else ()
    identities = {label.casefold() for label in labels}
    if not valid or len(identities) != len(labels):
        report.add(
            f"{location}.ownership_label",
            "is not a non-empty string or a list of unique non-empty strings",
            "name every label this engine accepts, or drop the line for "
            f"{DEFAULT_OWNERSHIP_LABEL!r}",
        )
        labels = (DEFAULT_OWNERSHIP_LABEL,)
    spec_only = raw.get("spec_only_label", "")
    if not isinstance(spec_only, str):
        report.add(
            f"{location}.spec_only_label",
            "is not a string",
            "name the specification-only label, or drop the line to leave the feature off",
        )
        spec_only = ""
    spec_only = spec_only.strip()
    if spec_only and spec_only.casefold() in identities:
        report.add(
            f"{location}.spec_only_label",
            "reuses another control label",
            "give specification-only work its own label",
        )
        spec_only = ""
    return labels, spec_only


def _parse_paused_label(
    raw: dict, location: str, report: Report, taken: tuple[str, ...]
) -> str:
    """The label that pauses a card (#2548), or "" when the Manifest names none."""
    paused = raw.get("paused_label", "")
    if not isinstance(paused, str):
        report.add(
            f"{location}.paused_label",
            "is not a string",
            "name the label that pauses a card, or drop the line to leave the feature off",
        )
        return ""
    paused = paused.strip()
    if paused and paused.casefold() in {label.casefold() for label in taken if label}:
        report.add(
            f"{location}.paused_label",
            "reuses another control label",
            "give pausing its own label",
        )
        return ""
    return paused


def _parse_repos(raw: object, report: Report) -> tuple[Repo, ...]:
    if raw is None:
        report.add(
            "manifest.repos",
            "missing",
            "add one [[repos]] entry per repository, with its roles inline",
        )
        return ()
    if not isinstance(raw, list):
        report.add("manifest.repos", "is not a list of tables", "use [[repos]] entries")
        return ()
    repos: list[Repo] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        location = f"manifest.repos[{index}]"
        if not isinstance(entry, dict):
            report.add(location, "is not a table", "use [[repos]] with id and roles")
            continue
        repo_id = entry.get("id")
        if not isinstance(repo_id, str) or not repo_id:
            report.add(f"{location}.id", "missing", "set id to the repository name")
            continue
        if repo_id in seen:
            report.add(f"{location}.id", f"{repo_id!r} is listed twice", "keep one entry per repo")
            continue
        seen.add(repo_id)
        flags = {}
        for flag in ("holds_issues", "holds_code", "auto_deploys", "holds_artifacts"):
            value = entry.get(flag, False)
            if not isinstance(value, bool):
                report.add(
                    f"{location}.{flag}",
                    "must be true or false",
                    f"set {flag} = true only if this repo {flag.replace('_', ' ')}",
                )
                value = False
            flags[flag] = value
        workspace = entry.get("workspace", "")
        if not isinstance(workspace, str):
            report.add(
                f"{location}.workspace",
                "must be a repository id",
                "name the code repository whose checkout should hold these cards",
            )
            workspace = ""
        # Fail closed on anything unrecognized (#1734): an unreadable policy becomes the
        # prohibiting one, so a typo can never be the reason a worker is told to write a
        # closing keyword the repository bans. The Problem stops activation either way.
        closing = entry.get("closing_keywords", CLOSING_KEYWORDS_REQUIRED)
        if not isinstance(closing, str) or closing.strip() not in CLOSING_KEYWORD_POLICIES:
            report.add(
                f"{location}.closing_keywords",
                f"{closing!r} is not a closing-keyword policy",
                f"use one of {', '.join(CLOSING_KEYWORD_POLICIES)}",
            )
            closing = CLOSING_KEYWORDS_PROHIBITED
        verification = entry.get("verification", VERIFICATION_AGENTDECK_JOB)
        if not isinstance(verification, str) or verification.strip() not in VERIFICATION_SOURCES:
            report.add(
                f"{location}.verification",
                f"{verification!r} is not a verification source",
                f"use one of {', '.join(VERIFICATION_SOURCES)}",
            )
            verification = VERIFICATION_AGENTDECK_JOB
        repo = Repo(
            id=repo_id,
            workspace=workspace.strip(),
            closing_keywords=closing.strip(),
            verification=verification.strip(),
            **flags,
        )
        if not (repo.holds_issues or repo.holds_code or repo.holds_artifacts):
            report.add(
                location,
                f"{repo_id!r} has no role, so the poller would never touch it",
                "set holds_issues, holds_code and/or holds_artifacts, or remove the entry",
            )
        if repo.auto_deploys and not repo.holds_code:
            report.add(
                f"{location}.auto_deploys",
                f"{repo_id!r} auto-deploys but holds no code",
                "set holds_code = true, or drop auto_deploys",
            )
        if repo.verification == VERIFICATION_REVIEW_ONLY and (
            repo.auto_deploys or not repo.holds_code
        ):
            # Review alone releases here (#2729). A deploy would put an untested change in
            # front of users on the merge, and a repository holding no code has no pull
            # request to excuse — either way the gate would be loosened for nothing it needs.
            report.add(
                f"{location}.verification",
                f"{repo_id!r} cannot be review_only: it "
                + ("auto-deploys" if repo.auto_deploys else "holds no code"),
                "use review_only only on a code repository with no tests that does not deploy",
            )
        if repo.prohibits_closing_keywords and not repo.holds_code:
            # The policy governs pull request descriptions, and pull requests are opened in the
            # repository that holds the code. Declaring it on an issue-only repository would
            # look like it applied and silently do nothing (#1734).
            report.add(
                f"{location}.closing_keywords",
                f"{repo_id!r} holds no code, so no pull request is opened in it",
                "declare closing_keywords on the repository the pull request lands in",
            )
        if repo.holds_code and repo.workspace:
            report.add(
                f"{location}.workspace",
                f"{repo_id!r} holds code, so it owns its checkout",
                "drop workspace and give this repository its own Overlay checkout",
            )
        repos.append(repo)
    holding = [repo.id for repo in repos if repo.holds_artifacts]
    if len(holding) > 1:
        # Two evidence repositories would make "where do the screenshots go" a choice the worker
        # has to make per card, and the brief can only name one place without inviting half a
        # project's evidence into each.
        report.add(
            "manifest.repos",
            f"more than one repository holds artifacts: {', '.join(sorted(holding))}",
            "keep holds_artifacts on exactly one repository",
        )
    if not repos:
        report.add(
            "manifest.repos",
            "no usable repository entries",
            "add at least one [[repos]] entry that holds issues",
        )
    return tuple(repos)


def _account_percentages(
    worker: dict,
    key: str,
    accounts: tuple[str, ...],
    report: Report,
    *,
    example: str,
    remedy: str,
) -> dict[str, float]:
    """One `[worker]` table mapping an account key to a percentage, validated.

    A bad entry is reported and dropped rather than refusing the whole Overlay: the account
    order it decorates is still usable, and the engine's defaults still apply to it.
    """
    raw = worker.get(key, {})
    parsed: dict[str, float] = {}
    if not isinstance(raw, dict):
        report.add(
            f"overlay.worker.{key}", "must be a table from account key to percentage", example
        )
        return parsed
    for name, value in raw.items():
        location = f"overlay.worker.{key}.{name}"
        if not isinstance(name, str) or not isinstance(value, int | float):
            report.add(location, "must be a numeric percentage", "use a number from 0 to 100")
            continue
        if name not in accounts:
            report.add(location, "names an account the worker order does not offer", remedy)
            continue
        if not 0 <= float(value) <= 100:
            report.add(location, "must be between 0 and 100", "use a valid percentage")
            continue
        parsed[name] = float(value)
    return parsed


def _host_path(value: str, where: str, report: Report) -> str:
    """One Overlay path, resolved once here rather than at each use.

    ``~`` is expanded, because an Overlay is hand-written and every other host file on this
    machine accepts it. A path that is still not absolute afterwards is refused: it would
    resolve against the working directory, so the same instance would use one state file when
    an operator runs a wake by hand and a different one when systemd runs it — two divergent
    histories for one project, with nothing to show that it happened.
    """
    resolved = str(Path(value).expanduser())
    if not Path(resolved).is_absolute():
        report.add(
            where,
            f"{value!r} is relative, so it depends on the working directory",
            "give an absolute path (or one starting with ~)",
        )
        return ""
    return resolved


def parse_overlay(raw: dict, report: Report) -> Overlay:
    """Turn raw Overlay TOML into a typed value, recording every shape problem."""
    contract = _contract(raw, "overlay", report)

    paths = _table(raw, "paths", report, "overlay")
    state_path = paths.get("state")
    if not isinstance(state_path, str) or not state_path:
        report.add(
            "overlay.paths.state",
            "missing",
            "set state to the file this instance's durable state lives in",
        )
        state_path = ""
    else:
        state_path = _host_path(state_path, "overlay.paths.state", report)
    log_path = paths.get("log", "")
    if not isinstance(log_path, str):
        report.add("overlay.paths.log", "must be a path", "set log to a file path or drop it")
        log_path = ""
    elif log_path:
        log_path = _host_path(log_path, "overlay.paths.log", report)

    extensions_dir = paths.get("extensions", "")
    if not isinstance(extensions_dir, str):
        report.add(
            "overlay.paths.extensions",
            "must be a path",
            "point extensions at the folder holding shared extension programs, or drop it",
        )
        extensions_dir = ""
    elif extensions_dir:
        extensions_dir = _host_path(extensions_dir, "overlay.paths.extensions", report)

    env_files = paths.get("env_files", [])
    if not isinstance(env_files, list) or any(not isinstance(item, str) for item in env_files):
        report.add(
            "overlay.paths.env_files",
            "must be a list of paths",
            "list the files holding this host's environment, or drop the key",
        )
        env_files = []

    checkouts_raw = _table(raw, "checkouts", report, "overlay")
    checkouts: dict[str, str] = {}
    for repo_id, path in checkouts_raw.items():
        if not isinstance(path, str) or not path:
            report.add(
                f"overlay.checkouts.{repo_id}",
                "must be a filesystem path",
                "point it at the working copy on this host",
            )
            continue
        checkouts[str(repo_id)] = _host_path(path, f"overlay.checkouts.{repo_id}", report)

    worker = _table(raw, "worker", report, "overlay")
    account = worker.get("account", "")
    if not isinstance(account, str):
        report.add("overlay.worker.account", "must be a string", "name the agent account to use")
        account = ""

    # The fallback order. The primary leads it whether or not it is repeated here, so a host
    # cannot accidentally exclude the account it just named.
    fallbacks = worker.get("fallback_accounts", [])
    if not isinstance(fallbacks, list) or any(not isinstance(key, str) for key in fallbacks):
        report.add(
            "overlay.worker.fallback_accounts",
            "must be a list of account keys",
            'e.g. fallback_accounts = ["claude_code:alt"]',
        )
        fallbacks = []
    accounts = tuple(key for key in dict.fromkeys([account, *fallbacks]) if key)

    usage_ceilings = _account_percentages(
        worker,
        "usage_ceilings",
        accounts,
        report,
        example='e.g. usage_ceilings = { "claude_code:main" = 50, "codex:codex" = 90 }',
        remedy="add it to account/fallback_accounts or remove this ceiling",
    )
    usage_targets = _account_percentages(
        worker,
        "usage_targets",
        accounts,
        report,
        example='e.g. usage_targets = { "claude_code:alt" = 99 }',
        remedy="add it to account/fallback_accounts or remove this target",
    )

    usage_cache = worker.get("usage_cache", "")
    if not isinstance(usage_cache, str):
        report.add(
            "overlay.worker.usage_cache",
            "must be a string path",
            "point at the directory where the deck publishes provider-qualified usage snapshots",
        )
        usage_cache = ""
    elif usage_cache:
        usage_cache = _host_path(usage_cache, "overlay.worker.usage_cache", report)

    return Overlay(
        contract=contract,
        state_path=state_path,
        checkouts=checkouts,
        account=account,
        accounts=accounts,
        usage_ceilings=usage_ceilings,
        usage_targets=usage_targets,
        usage_cache=usage_cache,
        credentials=_table(raw, "credentials", report, "overlay"),
        log_path=log_path,
        env_files=tuple(env_files),
        extensions_dir=extensions_dir,
        extensions=_parse_overlay_extensions(raw.get("extensions"), report),
    )


def _parse_overlay_extensions(raw: object, report: Report) -> dict:
    """The Overlay's per-extension blocks: one table each, or nothing at all (#386)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        report.add(
            "overlay.extensions",
            "is not a table of extension blocks",
            "write one [extensions.<name>] table per extension whose settings this host supplies",
        )
        return {}
    blocks = {}
    for name, block in raw.items():
        if not isinstance(block, dict):
            report.add(
                f"overlay.extensions.{name}",
                "is not a table",
                f"write it as [extensions.{name}] with this host's settings for it",
            )
            continue
        blocks[str(name)] = block
    return blocks


def effective_extensions(manifest: Manifest, overlay: Overlay) -> dict:
    """Each enabled extension's settings, with the host's half merged over the project's (#386).

    Only extensions the *Manifest* enables exist: a block appearing solely in the Overlay would be
    a host quietly enabling something the project never asked for, which the cross-check refuses
    rather than honors. Merging per key rather than per block means the Overlay supplies the
    credential without having to restate the settings around it.
    """
    merged = {}
    for name, block in manifest.extensions.items():
        host = overlay.extensions.get(name) or {}
        # Always a copy, even with nothing to merge: handing back the Manifest's own dict would
        # let a caller mutate the portable half through the merged view.
        merged[name] = {**block, **host}
    return merged


def cross_check(manifest: Manifest, overlay: Overlay, report: Report) -> None:
    """Check the two files against each other — the mistakes neither can see alone."""
    if manifest.contract and overlay.contract and manifest.contract != overlay.contract:
        report.add(
            "overlay.contract",
            f"contract {overlay.contract} does not match the manifest's {manifest.contract}",
            "regenerate the overlay with the Builder so both files share one contract",
        )
    by_id = {repo.id: repo for repo in manifest.repos}
    for repo in manifest.repos:
        if repo.holds_code and repo.id not in overlay.checkouts:
            report.add(
                f"overlay.checkouts.{repo.id}",
                f"{repo.id!r} holds code but has no checkout path on this host",
                f"add {repo.id} = \"/path/to/{repo.id}\" under [checkouts]",
            )
        # An evidence repository with no checkout here is *not* a problem: it goes dormant, and
        # the worker gets the brief it always got — commit the images beside the code. It cannot
        # borrow another repository's checkout the way `workspace` does, because the evidence is
        # pushed to this repository itself, so a host that has not cloned it simply cannot use
        # it yet. Refusing instead would make naming one unshippable (#1791): a promotion
        # publishes the committed Manifest but never the machine-local Overlay, so between the
        # merge and the host's own clone every one of this project's wakes would be rejected for
        # a path only a human can add. Dormant makes the order provably safe in either
        # direction — merge, promote, clone, then add the path.
        if repo.holds_issues and not repo.holds_code and not repo.workspace:
            report.add(
                f"manifest.repos.{repo.id}.workspace",
                f"{repo.id!r} holds issues but neither code nor a workspace",
                "name the code repository whose checkout should hold these cards",
            )
        if repo.workspace:
            target = by_id.get(repo.workspace)
            if target is None:
                report.add(
                    f"manifest.repos.{repo.id}.workspace",
                    f"{repo.workspace!r} is not a repository this manifest names",
                    "name one of this Manifest's code repositories",
                )
            elif not target.holds_code:
                report.add(
                    f"manifest.repos.{repo.id}.workspace",
                    f"{repo.workspace!r} does not hold code",
                    "name a repository with holds_code = true",
                )
    known = set(by_id)
    checkout_roots: dict[Path, str] = {}
    for repo_id in overlay.checkouts:
        if repo_id not in known:
            report.add(
                f"overlay.checkouts.{repo_id}",
                f"{repo_id!r} is not a repository this manifest names",
                "remove the path, or add the repo to the manifest",
            )
            continue
        if not (by_id[repo_id].holds_code or by_id[repo_id].holds_artifacts):
            report.add(
                f"overlay.checkouts.{repo_id}",
                f"{repo_id!r} holds neither code nor artifacts, so it cannot own a checkout",
                "remove this path and declare workspace on its Manifest repo entry",
            )
            continue
        root = Path(overlay.checkouts[repo_id]).resolve()
        previous = checkout_roots.get(root)
        if previous is not None:
            report.add(
                f"overlay.checkouts.{repo_id}",
                f"resolves to the same directory as {previous!r}",
                "keep one owning checkout and declare workspace on any borrowing repo",
            )
        else:
            checkout_roots[root] = repo_id
    for name, block in overlay.extensions.items():
        if EXTENSION_HOOKS_KEY in block:
            # What an extension may do is the project's policy, approved with its Manifest; a
            # host that could widen it would grant authority no apply ever saw (#2158).
            report.add(
                f"overlay.extensions.{name}.{EXTENSION_HOOKS_KEY}",
                "a host cannot change which hooks an extension is allowed",
                f"set hooks under [extensions.{name}] in the Manifest, and remove it here",
            )
        if name not in manifest.extensions:
            # A host may fill in an extension's credential; it may not switch one on. Enabling is
            # the project's decision, and the Manifest is where a reader looks to see what runs.
            report.add(
                f"overlay.extensions.{name}",
                f"{name!r} is not an extension this manifest enables",
                f"enable it with [extensions.{name}] in the Manifest, or remove this block",
            )


ExtensionValidator = object  # a callable (name, block, report) -> None; see #112


def _validate_extensions(
    manifest: Manifest, overlay: Overlay, validators: dict | None, report: Report
) -> None:
    """Hand each extension block to that extension to check, folding complaints into one report.

    The core loader deliberately knows nothing about any extension's settings: #112 defines
    how an extension declares its schema, and a new extension must not require a change here.

    The block handed over is the *merged* one (#386), so an extension validates the settings it
    will actually run with rather than the portable half of them — a required credential supplied
    by the host must not read as missing.
    """
    validators = validators or {}
    for name, block in effective_extensions(manifest, overlay).items():
        validator = validators.get(name)
        if validator is None:
            report.add(
                f"manifest.extensions.{name}",
                "no extension of that name is installed, so its settings cannot be checked",
                f"install the {name} extension, or remove the block",
            )
            continue
        problems: list[Problem] = []
        validator(block, problems)
        for problem in problems:
            report.extend([problem])


@dataclass(frozen=True)
class Draft:
    """A parsed, checked pair of files — usable only when its report is empty."""

    manifest: Manifest
    overlay: Overlay
    report: Report

    @property
    def ok(self) -> bool:
        return self.report.ok


def inspect(
    manifest_path: str | Path,
    overlay_path: str | Path,
    *,
    extension_validators: dict | None = None,
) -> Draft:
    """Stage 1 — the offline check: parse, validate shapes, cross-reference the two files.

    No network and no credentials are needed, so the Builder's draft loop and a read-only
    ``inspect`` can both run it as often as they like. Every problem found is reported at
    once; a non-empty report means nothing may activate.
    """
    report = Report()
    manifest_raw = _read_toml(Path(manifest_path), "manifest", report)
    overlay_raw = _read_toml(Path(overlay_path), "overlay", report)
    manifest = parse_manifest(manifest_raw or {}, report) if manifest_raw is not None else Manifest(
        contract=0, board=Board(org="", project_number=0)
    )
    overlay = parse_overlay(overlay_raw or {}, report) if overlay_raw is not None else Overlay(
        contract=0, state_path=""
    )
    if manifest_raw is not None:
        _verification_named(manifest_raw.get("repos"), report)
    if manifest_raw is not None and overlay_raw is not None:
        cross_check(manifest, overlay, report)
        _validate_extensions(manifest, overlay, extension_validators, report)
    return Draft(manifest=manifest, overlay=overlay, report=report)


def _verification_named(raw: object, report: Report) -> None:
    """Every code repository names what proves its pull requests (#2832).

    No silent default: a repository with no tests that fell back to ``agentdeck_job`` waited
    at "Verification not run" for a suite nothing would run, and nothing at onboarding asked.
    Defaulting to review_only instead would loosen a release gate by omission. Checked here, on
    the activation path only: a wake's ``load`` keeps reading an installed Manifest from before
    this rule (as ``agentdeck_job``) until it is re-activated, so a deploy never stops one.
    """
    if not isinstance(raw, list):
        return
    for index, entry in enumerate(raw):
        if isinstance(entry, dict) and entry.get("holds_code") is True and (
            "verification" not in entry
        ):
            report.add(
                f"manifest.repos[{index}].verification",
                f"{entry.get('id')!r} holds code but does not say what proves its pull requests",
                f"set verification to one of {', '.join(VERIFICATION_SOURCES)}",
            )


# --- stage 2: resolve at activation ---


class ConfigError(Exception):
    """The configuration cannot be used. Carries the report that says why."""

    def __init__(self, message: str, report: Report | None = None) -> None:
        super().__init__(message)
        self.report = report or Report()


class DriftError(ConfigError):
    """What is on disk is no longer what was activated, so the engine refuses to run."""


@dataclass(frozen=True)
class BoardSnapshot:
    """What read-only discovery found on the board: ids, and the columns it actually has."""

    project_id: str
    status_field_id: str
    status_options: dict[str, str] = field(default_factory=dict)  # display name -> option id


def boardless_snapshot() -> BoardSnapshot:
    """What discovery finds for a project with no board: every role, backed by its label (#1481).

    Every role is present, so activation binds all of them — including the optional ones, whose
    gating asks the binding rather than the Manifest. A boardless project therefore gets Ready
    to merge and Human review pending for free, because no column has to be made first.
    """
    return BoardSnapshot(
        project_id=BOARDLESS_PROJECT_ID,
        status_field_id=BOARDLESS_STATUS_FIELD_ID,
        status_options={role.value: status_label(role) for role in Role},
    )


class Reachability(Enum):
    """What one repository lookup established (#707).

    The failure side is three-way because the remedies are three different things. A missing
    repository is a typo in the Manifest; a refused one needs a grant; a GitHub that could not
    answer needs nothing at all — the shared quota refills and the next deploy tick passes.
    Collapsing the last two is what sent an on-call reader hunting a permissions problem that
    did not exist, because a rate-limited read and a real credential failure said the same thing.
    """

    REACHABLE = "reachable"
    ABSENT = "absent"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RepoCheck:
    """One repository lookup: what it established, and the failure it established it from.

    ``detail`` is the adapter's own words for the failure, kept short and carried into the
    activation report. It is what turns "this is transient" from a claim into evidence.
    """

    reach: Reachability
    detail: str = ""


@runtime_checkable
class Discovery(Protocol):
    """The read-only lookups activation needs. Implemented by the host, never by the engine."""

    def board(self, org: str, project_number: int) -> BoardSnapshot | None:
        """The board's node ids and columns, or None when it could not be read."""

    def repo_check(self, org: str, repo_id: str) -> RepoCheck:
        """Whether a repository is there, absent, refused, or simply unanswerable right now."""


@dataclass(frozen=True)
class ResolvedBinding:
    """The derived facts, frozen at activation. Never hand-authored, never guessed.

    ``extension_pins`` is a fingerprint input only and grants nothing (#2196). The derived
    fingerprint has always covered it, so it is read back exactly as published: an edited value
    changes the digest. A new activation writes ``{}``, and ``load`` refuses any other value,
    because only a pre-#2158 activation wrote pins.
    """

    project_id: str
    status_field_id: str
    status_option_ids: dict[str, str] = field(default_factory=dict)  # role value -> option id
    extension_pins: object = field(default_factory=dict)  # as published; grants nothing
    approval: dict = field(default_factory=dict)  # audit metadata; not an operational input
    fingerprint: dict = field(default_factory=dict)

    def option_id(self, role: Role) -> str:
        return self.status_option_ids.get(role.value, "")

    def as_json(self) -> dict:
        return {
            "project_id": self.project_id,
            "status_field_id": self.status_field_id,
            "status_option_ids": dict(self.status_option_ids),
            "extension_pins": self.extension_pins,
            "approval": dict(self.approval),
            "fingerprint": dict(self.fingerprint),
        }


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def derived_of(binding: ResolvedBinding) -> dict:
    """The derived half of the fingerprint, from one recipe used by both sides.

    Activation fingerprints this and every wake recomputes it from the published binding, so a
    shape that differs between the two reports drift on an instance that never moved — the #251
    failure, which is terminal because the remedy it prints produces the same refusal.
    """
    return {
        "project_id": binding.project_id,
        "status_field_id": binding.status_field_id,
        "status_option_ids": dict(binding.status_option_ids),
        # Empty since #2158; read back as published so an edit to it is drift like any other.
        "extension_pins": binding.extension_pins,
    }


# What an activation approves. The engine's own code is deliberately absent (#2153): it deploys
# with AgentDeck as ordinary software, so a code change alone must never invalidate a binding.
FINGERPRINT_PARTS = ("contract", "manifest", "binding")


def fingerprint_of(
    manifest_path: Path,
    binding: dict,
    *,
    contract: int,
) -> dict:
    """The fingerprint of one activation: what was authored and what was derived.

    The Overlay is deliberately *not* fingerprinted: rotating a credential or fixing a path
    on one host must not require re-activating the project. The cheap Manifest-Overlay
    cross-check on every load is what keeps an edited Overlay honest instead.
    """
    parts = {
        "contract": contract,
        "manifest": _file_digest(manifest_path),
        "binding": _digest(binding),
    }
    return {**parts, "digest": _digest(parts)}


@dataclass(frozen=True)
class PollerConfig:
    """One immutable configuration value: the authored files plus the frozen binding."""

    manifest: Manifest
    overlay: Overlay
    binding: ResolvedBinding

    def option_id(self, role: Role) -> str:
        return self.binding.option_id(role)

    def checkout(self, repo_id: str) -> str:
        return workspace_checkout(self.manifest, self.overlay, repo_id)

    def worker_checkouts(self) -> dict[str, str]:
        """Each issue repository's effective code checkout on this host."""
        return worker_checkouts(self.manifest, self.overlay)

    def borrowed_workspaces(self) -> dict[str, str]:
        """Issue repositories that borrow a checkout, and the code repository each borrows."""
        return borrowed_workspaces(self.manifest)

    def code_checkouts(self) -> dict[str, str]:
        """Every code repository this project holds, and its checkout on this host."""
        return code_checkouts(self.manifest, self.overlay)

    def artifact_evidence(self) -> tuple[str, str]:
        """Where screenshot evidence is committed: `owner/repo` and its checkout here."""
        return artifact_evidence(self.manifest, self.overlay)

    def closing_keyword_policies(self) -> dict[str, str]:
        """Each code repository's declared pull request closing-keyword policy (#1734)."""
        return closing_keyword_policies(self.manifest)

    @property
    def org(self) -> str:
        return self.manifest.board.org


def workspace_checkout(manifest: Manifest, overlay: Overlay, repo_id: str) -> str:
    repo = manifest.repo(repo_id)
    owner = repo.workspace if repo and repo.workspace else repo_id
    return overlay.checkouts.get(owner, "")


def worker_checkouts(manifest: Manifest, overlay: Overlay) -> dict[str, str]:
    """Each issue repository's effective code checkout on this host."""
    return {
        repo.id: checkout
        for repo in manifest.repos
        if repo.holds_issues and (checkout := workspace_checkout(manifest, overlay, repo.id))
    }


def borrowed_workspaces(manifest: Manifest) -> dict[str, str]:
    """Issue repositories that borrow a checkout, and the code repository each borrows."""
    return {repo.id: repo.workspace for repo in manifest.repos if repo.workspace}


def code_checkouts(manifest: Manifest, overlay: Overlay) -> dict[str, str]:
    """Every code repository this project holds, and where this host cloned it (#1578).

    ``worker_checkouts`` answers "where is this card worked", which is one directory. This
    answers "what code does this project own", which can be more: a project whose work spans
    a second repository — an owned fork that holds no issues, say — needs its worker to know
    that the other checkout exists, because the card cannot live in a tracker the fork does
    not have and nothing else in the brief would ever name it.
    """
    return {
        repo.id: checkout
        for repo in manifest.repos
        if repo.holds_code and (checkout := overlay.checkouts.get(repo.id, ""))
    }


def closing_keyword_policies(manifest: Manifest) -> dict[str, str]:
    """Every code repository this project holds, and whether `Closes #n` is allowed there.

    Only code repositories appear, because only they receive a pull request. A repository
    absent from the answer is the default: AgentDeck's own generic instruction stands, which
    is exactly what every project got before the field existed (#1734).
    """
    return {
        repo.id: repo.closing_keywords for repo in manifest.repos if repo.holds_code
    }


def artifact_evidence(manifest: Manifest, overlay: Overlay) -> tuple[str, str]:
    """The evidence repository as `owner/repo`, and where this host cloned it (#1791).

    Both halves or neither: a name with no clone is somewhere the worker cannot push, and a
    clone with no name is a directory it cannot address in an embed. Answering `("", "")` is
    what leaves a project that named no evidence repository committing screenshots exactly
    where it always did.
    """
    repo_id = manifest.artifact_repo()
    checkout = overlay.checkouts.get(repo_id, "") if repo_id else ""
    if not repo_id or not checkout:
        return "", ""
    return f"{manifest.board.org}/{repo_id}", checkout


def resolve(
    manifest_path: str | Path,
    overlay_path: str | Path,
    discovery: Discovery,
    *,
    extension_validators: dict | None = None,
) -> tuple[PollerConfig | None, Report]:
    """Stage 2 — the offline check plus read-only discovery, frozen into a binding.

    Runs once, at activation. A broken pair of files is never taken to GitHub: the offline
    report is returned as-is. Discovery that cannot answer is a problem, not an assumption —
    an unverifiable board or repository fails closed rather than activating on a guess.
    """
    draft = inspect(manifest_path, overlay_path, extension_validators=extension_validators)
    if not draft.ok:
        return None, draft.report

    report = draft.report
    manifest = draft.manifest
    if manifest.board.boardless:
        # Nothing to discover: the status backend is the issue's own labels, and what they are
        # spelled is settled by the engine rather than by anything live (#1481).
        board = boardless_snapshot()
    else:
        board = discovery.board(manifest.board.org, manifest.board.project_number)
    if board is None:
        report.add(
            "board",
            f"project {manifest.board.project_number} of {manifest.board.org} could not be read",
            "check the project number and that the credentials can read the board",
        )
        return None, report

    by_display = {name.strip().lower(): option for name, option in board.status_options.items()}
    option_ids: dict[str, str] = {}
    for role in Role:
        display = manifest.board.display_name(role)
        option = by_display.get(display.strip().lower())
        if option is None:
            if role in OPTIONAL_ROLES:
                continue  # an optional column; a board without it keeps the old destinations
            report.add(
                f"board.statuses.{role.value}",
                f"the board has no column named {display!r} for the {role.value} role",
                "add the column, or map the role to an existing one under [board.statuses]",
            )
            continue
        option_ids[role.value] = option

    for repo in manifest.repos:
        check = discovery.repo_check(manifest.board.org, repo.id)
        if check.reach is Reachability.REACHABLE:
            continue
        because = f" ({check.detail})" if check.detail else ""
        if check.reach is Reachability.ABSENT:
            # No detail: a 404 says only "Not Found", which the wording already says better.
            report.add(
                f"manifest.repos.{repo.id}",
                f"{manifest.board.org}/{repo.id} does not exist or is not visible",
                "fix the repository id, or remove the entry",
            )
        elif check.reach is Reachability.DENIED:
            report.add(
                f"manifest.repos.{repo.id}",
                f"could not be checked with these credentials{because}",
                "grant access to the repository, then activate again",
            )
        else:
            # Unavailable, and anything a later reach could add: nothing to grant and nothing to
            # edit, or the next reader repeats the hunt this wording exists to end. The escape
            # hatch is the detail, because "transient" is an inference and the detail is a fact.
            report.add(
                f"manifest.repos.{repo.id}",
                f"GitHub could not answer right now{because}",
                "treat as transient: wait and activate again, and read the detail if it repeats",
            )

    if not report.ok:
        return None, report

    binding = ResolvedBinding(
        project_id=board.project_id,
        status_field_id=board.status_field_id,
        status_option_ids=option_ids,
    )
    binding = replace(
        binding,
        fingerprint=fingerprint_of(
            Path(manifest_path), derived_of(binding), contract=manifest.contract
        ),
    )
    return PollerConfig(manifest=manifest, overlay=draft.overlay, binding=binding), report


@contextmanager
def config_lock(binding_path: str | Path, *, exclusive: bool) -> Iterator[None]:
    """Hold the instance's Manifest, binding and audit as one unit (#2157).

    An apply replaces them under the exclusive lock and every load reads under the shared one,
    so a wake never pairs a new Manifest with the old binding and refuses it as drift.

    The two sides fail differently on purpose. An **exclusive** caller is an apply, and its
    atomicity rests on this lock, so a lock that cannot be opened or taken raises ``OSError``
    and nothing is replaced. A **shared** caller is a reader: where it cannot lock (a
    read-only instance directory) it proceeds unlocked, because every file is still replaced
    atomically and a torn pair still fails closed as drift.
    """
    target = Path(binding_path)
    handle = None
    try:
        handle = open(target.with_name(target.name + ".lock"), "a")  # noqa: SIM115
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except OSError:
        if handle is not None:
            handle.close()
            handle = None
        if exclusive:
            raise
    try:
        yield
    finally:
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


class ManifestChanged(ConfigError):
    """The candidate Manifest is no longer the bytes its binding was resolved from."""


# The apply's one crash-atomic unit (#2157). Every apply writes a complete, immutable
# generation directory, then switches one `current` link to it with a single rename. The fixed
# paths every reader opens (`poller.toml`, the binding, `activation.json`) are links through
# `current`, so a process that dies at any point leaves either the old set or the new set
# selected, never a mix, and nothing needs rolling back.
GENERATIONS = "generations"
CURRENT = "current"
_GENERATION_FILES = {
    "manifest": "poller.toml",
    "binding": "binding.json",
    "audit": "activation.json",
}
# The generation `current` names plus the one before it, so a reader that could not take the
# shared lock is never left holding a path that was just deleted.
KEEP_GENERATIONS = 2


def publish(
    binding_path: str | Path,
    binding: ResolvedBinding,
    *,
    manifest_path: str | Path,
    candidate_path: str | Path,
    audit_path: str | Path | None = None,
    audit_for: Callable[[bytes | None], bytes] | None = None,
) -> None:
    """Commit one apply: the Manifest, the binding resolved from it, and its audit (#2157).

    Under the instance's exclusive config lock the candidate is read once and checked against
    the binding's ``manifest`` fingerprint part — also when it *is* the installed file — and a
    candidate edited after it was resolved is refused with ``ManifestChanged``. ``audit_for``
    is handed the current audit bytes (``None`` when there is none) and returns the new ones.
    The three are written into a new generation and selected by one atomic switch of
    ``current``; until that rename nothing a reader opens has changed. An instance still
    holding plain files from before generations is refused (``ConfigError``) with nothing written.
    """
    target = Path(binding_path)
    base = target.parent
    base.mkdir(parents=True, exist_ok=True)
    live = {"manifest": Path(manifest_path), "binding": target}
    if audit_path is not None and audit_for is not None:
        live["audit"] = Path(audit_path)
    binding_bytes = (json.dumps(binding.as_json(), indent=2, sort_keys=True) + "\n").encode()
    with config_lock(target, exclusive=True):
        _refuse_plain_files(base, live)
        data = Path(candidate_path).read_bytes()
        if hashlib.sha256(data).hexdigest() != (binding.fingerprint or {}).get("manifest"):
            report = Report()
            report.add(
                "manifest",
                f"{candidate_path} changed after it was resolved",
                "apply again so the binding is resolved from the Manifest as it is now",
            )
            raise ManifestChanged("the Manifest changed during this apply", report)
        contents = {"manifest": data, "binding": binding_bytes}
        if "audit" in live:
            contents["audit"] = audit_for(_read_bytes(live["audit"]))
        _switch_current(base, _write_generation(base, contents))
        _link_through_current(base, live)
        _prune_generations(base)


def _refuse_plain_files(base: Path, live: dict[str, Path]) -> None:
    """Refuse an instance whose live paths are not links through ``current``.

    Every installed instance was moved onto generations by its first apply after #2157, and
    that conversion is gone (#2196). Publishing past a plain file would leave readers on the old
    file while ``current`` named the new set, so it stops here, before anything is written. The
    one plain file allowed is the Manifest of an instance that was never applied: with no
    binding there is no wake reading it, so the apply may replace it with a link.
    """
    never_applied = not (live["binding"].exists() or live["binding"].is_symlink())
    plain = [
        str(path)
        for role, path in live.items()
        if (path.exists() or path.is_symlink())
        and not _links_through_current(base, role, path)
        and not (role == "manifest" and never_applied)
    ]
    if plain:
        report = Report()
        report.add(
            "binding",
            "is not on the generations layout: " + ", ".join(plain),
            "restore this instance's files as links through current before applying",
        )
        raise ConfigError("the instance predates the generations layout", report)


def _link_through_current(base: Path, live: dict[str, Path]) -> None:
    """Make each fixed path a link through ``current``: new ones, and a never-applied Manifest."""
    for role, path in live.items():
        if _links_through_current(base, role, path):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        link = path.with_name(f".{path.name}.{uuid.uuid4().hex}.link")
        link.symlink_to(os.path.relpath(base / CURRENT / _GENERATION_FILES[role], path.parent))
        try:
            os.replace(link, path)
        except BaseException:
            link.unlink(missing_ok=True)
            raise


def _links_through_current(base: Path, role: str, path: Path) -> bool:
    if not path.is_symlink():
        return False
    expected = os.path.relpath(base / CURRENT / _GENERATION_FILES[role], path.parent)
    return os.readlink(path) == expected


def _write_generation(base: Path, contents: dict[str, bytes | None]) -> str:
    """Write a complete generation under a temporary name, then give it its final one."""
    root = base / GENERATIONS
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
    try:
        for role, data in contents.items():
            if data is None:
                continue  # a file the instance does not have yet stays absent
            path = staging / _GENERATION_FILES[role]
            with open(path, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(0o644)
        staging.chmod(0o755)
        name = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
        staging.rename(root / name)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _fsync_dir(root)
    return name


def _switch_current(base: Path, name: str) -> None:
    """The commit: one rename replaces ``current``, so it names the old generation or the new."""
    link = base / f".{CURRENT}.{uuid.uuid4().hex}.link"
    link.symlink_to(f"{GENERATIONS}/{name}")
    try:
        os.replace(link, base / CURRENT)
    except BaseException:
        link.unlink(missing_ok=True)
        raise
    _fsync_dir(base)


def _prune_generations(base: Path) -> None:
    """Drop generations nothing selects any more. Best effort: the commit already landed."""
    root = base / GENERATIONS
    try:
        selected = os.readlink(base / CURRENT).rsplit("/", 1)[-1]
        entries = sorted(root.iterdir(), key=lambda path: path.name)
        final = [path for path in entries if not path.name.startswith(".")]
        keep = {selected} | {path.name for path in final[-KEEP_GENERATIONS:]}
        for path in entries:
            if path.name not in keep:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def write_binding(path: str | Path, binding: ResolvedBinding) -> None:
    """Publish the resolved binding. Engine-written JSON; never hand-edited."""
    # Through the link, when there is one: an applied instance's binding lives in its current
    # generation (#2157), and replacing the link with a plain file would detach it from the
    # Manifest it was published with.
    target = Path(path).resolve() if Path(path).is_symlink() else Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A private temporary name, so two writers of the same binding never rename each other's file.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(binding.as_json(), indent=2, sort_keys=True) + "\n")
        tmp.chmod(0o644)  # what the file always had; mkstemp's 0600 is for the name, not the data
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def binding_from_json(raw: object) -> ResolvedBinding | None:
    """Rehydrate a binding from its published JSON shape, or None when it is not one.

    One parser for the file a wake reads and for the copy an activation record keeps, so a
    rollback restores exactly what the wake would have loaded rather than a second opinion
    about the same bytes.
    """
    if not isinstance(raw, dict):
        return None
    options = raw.get("status_option_ids")
    pins = raw.get("extension_pins", {})
    approval = raw.get("approval")
    fingerprint = raw.get("fingerprint")
    return ResolvedBinding(
        project_id=str(raw.get("project_id") or ""),
        status_field_id=str(raw.get("status_field_id") or ""),
        status_option_ids=dict(options) if isinstance(options, dict) else {},
        extension_pins=dict(pins) if isinstance(pins, dict) else pins,
        approval=dict(approval) if isinstance(approval, dict) else {},
        fingerprint=dict(fingerprint) if isinstance(fingerprint, dict) else {},
    )


def read_binding(path: str | Path) -> ResolvedBinding | None:
    """Read a published binding, or None when it is absent or unreadable."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return binding_from_json(raw)


# --- stage 3: the per-cycle load ---


def load(
    manifest_path: str | Path,
    overlay_path: str | Path,
    binding_path: str | Path,
) -> PollerConfig:
    """Stage 3 — the hot path: read what was activated and prove it has not moved.

    Raises ``ConfigError`` when the files or the binding cannot be used, and ``DriftError``
    when what is on disk no longer matches the activation. Nothing is re-discovered: a wake
    must be cheap, and only re-activation (#109) may resolve the board again. A binding from
    before #2153, which also fingerprinted the engine build, is drift like any other change.
    """
    with config_lock(binding_path, exclusive=False):
        return _load(manifest_path, overlay_path, binding_path)


def _load(
    manifest_path: str | Path,
    overlay_path: str | Path,
    binding_path: str | Path,
) -> PollerConfig:
    report = Report()
    manifest_raw = _read_toml(Path(manifest_path), "manifest", report)
    overlay_raw = _read_toml(Path(overlay_path), "overlay", report)
    if manifest_raw is None or overlay_raw is None:
        raise ConfigError("configuration files could not be read", report)

    manifest = parse_manifest(manifest_raw, report)
    overlay = parse_overlay(overlay_raw, report)
    # The Overlay is not fingerprinted, so this cheap cross-check is what keeps a hand-edited
    # host file from quietly breaking an invariant the activation had verified.
    cross_check(manifest, overlay, report)
    if not report.ok:
        raise ConfigError("configuration is no longer valid", report)

    binding = read_binding(binding_path)
    if binding is None:
        report.add(
            "binding",
            f"{binding_path} is missing or unreadable",
            "activate this project so the engine can resolve its board",
        )
        raise ConfigError("the resolved binding is unavailable", report)

    current = fingerprint_of(Path(manifest_path), derived_of(binding), contract=manifest.contract)
    stored = binding.fingerprint or {}
    if current.get("digest") != stored.get("digest"):
        changed = [part for part in FINGERPRINT_PARTS if current.get(part) != stored.get(part)]
        report.add(
            "binding.fingerprint",
            "does not match what was activated (" + ", ".join(changed or ["unknown"]) + " changed)",
            "re-activate this project so its binding is resolved again",
        )
        raise DriftError("configuration drifted from the activation", report)
    if binding.extension_pins != {}:
        report.add(
            "binding.extension_pins",
            "was written by an activation from before #2158, and pins no longer grant hooks",
            "state each extension's hooks in the Manifest and re-activate this project",
        )
        raise DriftError("the binding predates Manifest hook lists", report)

    return PollerConfig(manifest=manifest, overlay=overlay, binding=binding)
