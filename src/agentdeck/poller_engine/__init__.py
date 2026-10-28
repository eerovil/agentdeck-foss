"""Shared Poller Engine core (planning issue #105, build ticket #176).

The deterministic machinery that interprets a Poller Manifest and runs a project's
housekeeping wake: extensions, the issues they propose, bug reports, scheduled tasks and
health. It held cards and ran workers for them until #3732; the deck runs every Work now.

IMPORT BOUNDARY. The engine runs from the checkout under its venv (#2153, #2597), so it
may import the deck's shared modules and third-party packages. It must never import
``agentdeck.web`` or ``agentdeck.app``, directly or through another module, and must not
load the providers package, whose import cost every engine command would pay.
``tests/test_poller_engine_boundary.py`` enforces this.
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
from .extensions import Extension, Hook, Mode, Request, RequestKind
from .health import CycleReport, Freshness, HealthState
from .intake import Command, CommandKind, parse_control
from .ports import Effect, EffectKind, EnginePort
from .state import (
    CURRENT_SCHEMA_VERSION,
    EnginePaths,
    LoadReport,
    StateError,
    StateSchemaError,
    StateStore,
)

__all__ = [
    "BoardSnapshot",
    "CONTRACT",
    "CURRENT_SCHEMA_VERSION",
    "Command",
    "CommandKind",
    "ConfigError",
    "CycleOutcome",
    "CycleReport",
    "Discovery",
    "DriftError",
    "Effect",
    "EffectKind",
    "EnginePaths",
    "EnginePort",
    "Extension",
    "Freshness",
    "HealthState",
    "Hook",
    "LoadReport",
    "Manifest",
    "Mode",
    "Overlay",
    "Policy",
    "PollerConfig",
    "Problem",
    "ProvisionStep",
    "Reachability",
    "RepoCheck",
    "Report",
    "Request",
    "RequestKind",
    "ResolvedBinding",
    "Role",
    "StateError",
    "StateSchemaError",
    "StateStore",
    "Tier",
    "Verdict",
    "parse_control",
    "run_cycle",
]
