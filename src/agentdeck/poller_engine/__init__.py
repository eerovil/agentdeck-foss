"""Shared Poller Engine core (planning issue #105, build ticket #176).

The deterministic control machinery that interprets a Poller Manifest and owns
command intake, authorization, durable queues, worker leases, lifecycle
transitions, and guarded release — rebuilt fresh against the #108/#112
interfaces (see #176 for the plan).

IMPORT BOUNDARY. The engine runs from the checkout under its venv (#2153, #2597), so it
may import the deck's shared modules and third-party packages. It must never import
``agentdeck.web`` or ``agentdeck.app``, directly or through another module, and must not
load the providers package, whose import cost every worker-control call would pay.
``tests/test_poller_engine_boundary.py`` enforces this.

The engine never talks to the async worker client directly: delivery belongs to the host
adapter, not the engine. Instead the engine emits durable dispatch directives over a
stable protocol; an app-side adapter performs delivery and writes dispatch-acks.
"""

from __future__ import annotations

from .activation import ProvisionStep
from .authorization import Policy, Tier, Verdict
from .config import (
    CONTRACT,
    BoardSnapshot,
    ConfigError,
    Discovery,
    DriftError,
    Manifest,
    Overlay,
    PollerConfig,
    Problem,
    Reachability,
    RepoCheck,
    Report,
    ResolvedBinding,
    Role,
)
from .cycle import CycleOutcome, run_cycle
from .dispatch import DispatchDecision
from .extensions import Extension, Hook, Mode, Request, RequestKind
from .health import CycleReport, Freshness, HealthState
from .intake import Command, CommandKind, parse_control
from .ports import (
    ArmOutcome,
    DeployState,
    Effect,
    EffectKind,
    EnginePort,
    LinkedPRs,
    PrState,
    ScanResult,
)
from .reconcile import CardSnapshot, ReconcileDecision
from .release import ReleaseAdmission, ReviewOutcome
from .state import (
    CURRENT_SCHEMA_VERSION,
    EnginePaths,
    LoadReport,
    StateError,
    StateSchemaError,
    StateStore,
)

__all__ = [
    "ArmOutcome",
    "BoardSnapshot",
    "CONTRACT",
    "CURRENT_SCHEMA_VERSION",
    "CardSnapshot",
    "Command",
    "CommandKind",
    "ConfigError",
    "CycleOutcome",
    "CycleReport",
    "DeployState",
    "Discovery",
    "DispatchDecision",
    "DriftError",
    "Effect",
    "EffectKind",
    "EnginePaths",
    "EnginePort",
    "Extension",
    "Freshness",
    "HealthState",
    "Hook",
    "LinkedPRs",
    "LoadReport",
    "Manifest",
    "Mode",
    "Overlay",
    "Policy",
    "PollerConfig",
    "PrState",
    "Problem",
    "ProvisionStep",
    "Reachability",
    "ReconcileDecision",
    "ReleaseAdmission",
    "RepoCheck",
    "Report",
    "Request",
    "RequestKind",
    "ResolvedBinding",
    "ReviewOutcome",
    "Role",
    "ScanResult",
    "StateError",
    "StateSchemaError",
    "StateStore",
    "Tier",
    "Verdict",
    "parse_control",
    "run_cycle",
]
