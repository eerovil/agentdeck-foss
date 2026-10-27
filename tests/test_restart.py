"""Restart nudges: the registry, its consumption on boot, CLI and API (#453).

Replaces the per-session ``restart-continue`` marker tests (#20). The detached
restart trigger itself still lives in ``providers/claude_code/restart.py``.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig
from agentdeck.models import (
    Capability,
    Session,
    SessionStatus,
)
from agentdeck.providers.claude_code import restart
from agentdeck.providers.claude_code.restart import (
    _unit_from_cgroup,
    detect_runtime_unit,
    looks_like_runtime_unit,
)
from agentdeck.providers.delivery import DeliverResult
from agentdeck.restart_nudge import (
    NUDGE_MESSAGE,
    NudgeEntry,
    RestartNudgeRegistry,
)
from agentdeck.runtime import create_runtime_app

TTL_S = 3600.0


def _registry(tmp_path, ttl_s=TTL_S) -> RestartNudgeRegistry:
    return RestartNudgeRegistry(tmp_path / "restart-nudge.json", ttl_s=ttl_s)


def _keys(entries) -> list[str]:
    return [entry.session_key for entry in entries]


# --- the registry ----------------------------------------------------------


def test_register_round_trip(tmp_path):
    registry = _registry(tmp_path)
    entry = registry.register("claude_code:main:sid-1")

    assert _keys(registry.entries()) == ["claude_code:main:sid-1"]
    assert registry.is_registered("claude_code:main:sid-1")
    assert registry.entries()[0].registered_at == pytest.approx(entry.registered_at)


def test_registry_is_a_set_of_session_keys(tmp_path):
    registry = _registry(tmp_path)
    registry.register("claude_code:main:sid-1", now=1_000.0)
    registry.register("codex:main:thread-9")
    # Registering again is not a second nudge; it refreshes the wait instead.
    refreshed = registry.register("claude_code:main:sid-1", now=2_000.0)

    assert _keys(registry.entries()) == ["claude_code:main:sid-1", "codex:main:thread-9"]
    assert refreshed.registered_at == 2_000.0
    assert registry.entries()[0].registered_at == 2_000.0


def test_unregister_reports_whether_one_was_pending(tmp_path):
    registry = _registry(tmp_path)
    registry.register("claude_code:main:sid-1")

    assert registry.unregister("claude_code:main:sid-1") is True
    assert registry.unregister("claude_code:main:sid-1") is False
    assert registry.entries() == []


def test_empty_key_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _registry(tmp_path).register("   ")


def test_consume_claims_everything_and_clears_the_file(tmp_path):
    registry = _registry(tmp_path)
    registry.register("claude_code:main:sid-1")
    registry.register("codex:main:thread-9")

    assert _keys(registry.consume()) == ["claude_code:main:sid-1", "codex:main:thread-9"]
    assert registry.entries() == []
    assert not registry.path.exists()
    # A second boot finds nothing: consumption is the clear.
    assert registry.consume() == []


def test_registration_racing_a_boot_waits_for_the_next_restart(tmp_path):
    """The file is renamed away before it is read, so a late write is not lost."""
    registry = _registry(tmp_path)
    registry.register("claude_code:main:sid-1")

    claimed = registry.consume()
    registry.register("codex:main:thread-9")

    assert _keys(claimed) == ["claude_code:main:sid-1"]
    assert _keys(registry.entries()) == ["codex:main:thread-9"]


def test_missing_and_damaged_registries_read_as_empty(tmp_path):
    registry = _registry(tmp_path)
    assert registry.entries() == []
    assert registry.consume() == []

    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text("{ not json")
    assert registry.entries() == []

    registry.path.write_text('{"sessions": "nope"}')
    assert registry.entries() == []

    # Individual junk records are skipped; a good neighbour still survives.
    registry.path.write_text(
        '{"sessions": {"claude_code:main:ok": {"registered_at": 5},'
        ' "bad": {}, "worse": 3, "": {"registered_at": 1}}}'
    )
    assert _keys(registry.entries()) == ["claude_code:main:ok"]


def test_entry_owns_stable_delivery_identity_and_staleness():
    entry = NudgeEntry(session_key="claude_code:main:sid-1", registered_at=1_700_000_000.875)

    assert entry.delivery_id == "restart-nudge:claude_code:main:sid-1:1700000000"
    assert entry.is_stale(entry.registered_at + TTL_S, TTL_S) is False
    assert entry.is_stale(entry.registered_at + TTL_S + 0.001, TTL_S) is True


@pytest.mark.parametrize(
    "session_key,expected",
    [
        ("claude_code:main:sid-1", ("claude_code", "main", "sid-1")),
        # Only the first two separators are structural.
        ("codex:main:a:b:c", ("codex", "main", "a:b:c")),
        ("claude_code:main", None),
        ("claude_code::sid-1", None),
        ("sid-only", None),
    ],
)
def test_session_key_parts(session_key, expected):
    assert NudgeEntry(session_key=session_key, registered_at=0.0).parts == expected


# --- delivery on runtime boot ---------------------------------------------




def _runtime(
    tmp_path,
    *,
    claude=True,
    codex=False,
    max_kanban_workers=12,
):
    accounts = []
    if claude:
        accounts.append(
            AccountConfig(provider="claude_code", label="main", config_dir=str(tmp_path))
        )
    if codex:
        accounts.append(AccountConfig(provider="codex", label="main", config_dir=str(tmp_path)))
    config = AppConfig.model_validate(
        {
            "claude_workers": {"enabled": claude, "state_dir": str(tmp_path)},
            "keyed_workers": {
                "state_dir": str(tmp_path),
                "max_kanban_workers": max_kanban_workers,
            },
            "restart_nudge": {
                "file": str(tmp_path / "restart-nudge.json"),
                "ttl_s": TTL_S,
            },
            "history": {"db_path": str(tmp_path / "budgets.db")},
            "accounts": [account.model_dump() for account in accounts],
        }
    )
    return create_runtime_app(config).state


def _claude_host(*, key="issue-1", accepted=True):
    host = MagicMock()
    host.find_key_by_session = MagicMock(return_value=key)
    host.worker_role = MagicMock(return_value=None)
    host.deliver = AsyncMock(
        return_value=DeliverResult(
            accepted,
            "revived" if accepted else "rejected",
            reason=None if accepted else "at capacity",
            session_id="sid-1",
        )
    )
    return host


async def test_boot_nudges_a_claude_worker_session_and_clears_the_registry(tmp_path):
    state = _runtime(tmp_path)
    host = _claude_host()
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    entry = nudges.registry.register("claude_code:main:sid-1")

    await nudges.deliver_all()

    host.deliver.assert_awaited_once()
    args, kwargs = host.deliver.await_args
    assert args[0] == "issue-1"
    assert args[1] == NUDGE_MESSAGE
    assert kwargs["delivery_id"] == entry.delivery_id
    assert nudges.registry.entries() == []


async def test_boot_nudges_kanban_workers_through_global_capacity(tmp_path):
    state = _runtime(tmp_path, max_kanban_workers=1)
    host = _claude_host()
    active: set[str] = set()
    host.find_key_by_session.side_effect = ["issue-1", "issue-2"]
    host.worker_role.return_value = "kanban"
    host.active_role_keys.side_effect = lambda role: set(active)
    host.role_last_result_at.return_value = 0.0
    host.delivery_is_finalized.return_value = False

    async def deliver(key, *_args, **_kwargs):
        active.add(key)
        return DeliverResult(True, "revived", session_id=f"sid-{key[-1]}")

    host.deliver.side_effect = deliver
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    # The refused second nudge is retried after a sleep (#3009); do not wait it out.
    nudges.retry_interval_s = 0
    nudges.registry.register("claude_code:main:sid-1")
    nudges.registry.register("claude_code:main:sid-2")

    await nudges.deliver_all()

    assert host.deliver.await_count == 1
    assert active == {"issue-1"}


async def test_boot_queues_a_nudge_onto_an_owned_codex_thread(tmp_path):
    state = _runtime(tmp_path, claude=False, codex=True)
    client = MagicMock()
    client.queue_turn = AsyncMock(
        return_value=MagicMock(accepted=True, reason=None, session_id="thread-9")
    )
    state.runtime.clients["main"] = client
    nudges = state.restart_nudges
    nudges.registry.register("codex:main:thread-9")

    await nudges.deliver_all()

    client.queue_turn.assert_awaited_once_with("thread-9", NUDGE_MESSAGE)
    assert nudges.registry.entries() == []


@pytest.mark.parametrize(
    "session_key,setup",
    [
        # Session the runtime has no record of.
        ("claude_code:main:ghost", "unknown-session"),
        # Account that is not configured any more.
        ("claude_code:gone:sid-1", "none"),
        ("codex:gone:thread-9", "none"),
        # A provider this deck does not have (Cursor, removed in #2600).
        ("cursor:main:sid-1", "none"),
        # Malformed key written by hand.
        ("sid-only", "none"),
    ],
)
async def test_undeliverable_entries_are_logged_and_cleared(tmp_path, session_key, setup):
    state = _runtime(tmp_path)
    if setup == "unknown-session":
        state.claude_workers.hosts["main"] = _claude_host(key=None)
    nudges = state.restart_nudges
    nudges.registry.register(session_key)

    await nudges.deliver_all()  # must not raise

    assert nudges.registry.entries() == []


async def test_a_refused_delivery_is_not_retained(tmp_path):
    state = _runtime(tmp_path)
    state.claude_workers.hosts["main"] = _claude_host(accepted=False)
    nudges = state.restart_nudges
    nudges.registry.register("claude_code:main:sid-1")

    await nudges.deliver_all()

    assert nudges.registry.entries() == []


async def test_one_raising_entry_does_not_skip_the_rest(tmp_path):
    state = _runtime(tmp_path)
    host = _claude_host()
    host.deliver = AsyncMock(
        side_effect=[
            RuntimeError("worker host exploded"),
            DeliverResult(True, "revived", session_id="sid-2"),
        ]
    )
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.registry.register("claude_code:main:sid-1")
    nudges.registry.register("claude_code:main:sid-2")

    await nudges.deliver_all()

    assert host.deliver.await_count == 2
    assert nudges.registry.entries() == []


async def test_stale_entry_is_dropped_without_delivery(tmp_path):
    state = _runtime(tmp_path)
    host = _claude_host()
    state.claude_workers.hosts["main"] = host
    nudges = state.restart_nudges
    nudges.registry.register("claude_code:main:sid-1", now=time.time() - TTL_S - 60)

    await nudges.deliver_all()

    host.deliver.assert_not_awaited()
    assert nudges.registry.entries() == []


async def test_boot_is_a_quiet_no_op_with_nothing_registered(tmp_path):
    state = _runtime(tmp_path)
    await state.restart_nudges.deliver_all()


# --- the Secretary desk, nudged without registering (#847) ----------------


DESK_SESSION_KEY = "claude_code:main:desk-sid"




























async def test_claude_nudge_is_skipped_when_workers_are_disabled(tmp_path):
    state = _runtime(tmp_path, claude=False)
    nudges = state.restart_nudges
    nudges.registry.register("claude_code:main:sid-1")

    await nudges.deliver_all()

    assert nudges.registry.entries() == []


# --- the local API surface ------------------------------------------------


def _web_app(tmp_path):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    config.restart_nudge.file = str(tmp_path / "restart-nudge.json")
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    return app


async def test_session_agent_registers_itself_for_a_restart_nudge(tmp_path):
    app = _web_app(tmp_path)
    path = "/api/sessions/claude_code:test:sid1/restart-nudge"
    registry = _registry(tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get(path)).json() == {
            "session_key": "claude_code:test:sid1",
            "nudge_on_restart": False,
            "registered_at": None,
            "message": None,
        }

        registered = await client.put(path)
        assert registered.status_code == 200
        body = registered.json()
        assert body["nudge_on_restart"] is True
        assert body["message"] == NUDGE_MESSAGE
        assert body["registered_at"] > 0
        # Durable, not web state: the runtime reads the same file.
        assert _keys(registry.entries()) == ["claude_code:test:sid1"]

        # Idempotent — a set of chats, not a queue of messages.
        assert (await client.put(path)).json()["nudge_on_restart"] is True
        assert len(registry.entries()) == 1

        assert (await client.get(path)).json()["nudge_on_restart"] is True

        cancelled = await client.delete(path)
        assert cancelled.json()["nudge_on_restart"] is False
        assert registry.entries() == []

        unknown = await client.put("/api/sessions/claude_code:test:missing/restart-nudge")
        assert unknown.status_code == 404
        assert registry.entries() == []


# --- unit detection for the detached restart -------------------------------


def test_unit_from_cgroup_v2_leaf():
    line = (
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
        "agentdeck-staging-codex.service"
    )
    assert _unit_from_cgroup(line) == "agentdeck-staging-codex.service"


def test_unit_from_cgroup_none_when_absent():
    assert _unit_from_cgroup("0::/user.slice/session.scope") is None


def test_detect_runtime_unit_handles_missing_proc(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no /proc here")

    monkeypatch.setattr(restart.Path, "read_text", boom)
    assert detect_runtime_unit() is None


@pytest.mark.parametrize(
    "service,expected",
    [
        ("agentdeck-codex.service", True),
        ("agentdeck-staging-codex.service", True),
        ("agentdeck-staging.service", False),  # web unit, not the runtime
        ("some-other.service", False),
    ],
)
def test_looks_like_runtime_unit(service, expected):
    assert looks_like_runtime_unit(service) is expected


# --- CLI wiring ------------------------------------------------------------


def _cli_config(tmp_path, monkeypatch):
    from agentdeck import __main__ as cli

    config = AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, "state_dir": str(tmp_path)},
            "restart_nudge": {"file": str(tmp_path / "restart-nudge.json"), "ttl_s": TTL_S},
            "accounts": [
                {"provider": "claude_code", "label": "main", "config_dir": str(tmp_path)}
            ],
        }
    )
    monkeypatch.setattr(cli, "load_config", lambda _p=None: config)
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "config.toml")
    return cli


def test_cli_registers_and_cancels_a_nudge(tmp_path, monkeypatch):
    cli = _cli_config(tmp_path, monkeypatch)
    registry = _registry(tmp_path)

    cli._nudge_on_restart(["--session-key", "codex:main:thread-9"])
    assert _keys(registry.entries()) == ["codex:main:thread-9"]

    cli._nudge_on_restart(["--session-key", "codex:main:thread-9", "--cancel"])
    assert registry.entries() == []


def test_cli_defaults_to_the_calling_session(tmp_path, monkeypatch):
    cli = _cli_config(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTDECK_SESSION_KEY", "claude_code:main:sid-env")

    cli._nudge_on_restart([])
    assert _keys(_registry(tmp_path).entries()) == ["claude_code:main:sid-env"]


def test_cli_derives_a_key_from_a_bare_claude_session_id(tmp_path, monkeypatch):
    cli = _cli_config(tmp_path, monkeypatch)
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-xyz")

    cli._nudge_on_restart([])
    assert _keys(_registry(tmp_path).entries()) == ["claude_code:main:sid-xyz"]


def test_cli_requires_an_identifiable_session(tmp_path, monkeypatch):
    cli = _cli_config(tmp_path, monkeypatch)
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    with pytest.raises(SystemExit):
        cli._nudge_on_restart([])


def test_cli_refuses_an_ambiguous_claude_account(tmp_path, monkeypatch):
    from agentdeck import __main__ as cli

    config = AppConfig.model_validate(
        {
            "restart_nudge": {"file": str(tmp_path / "restart-nudge.json")},
            "accounts": [
                {"provider": "claude_code", "label": "main", "config_dir": str(tmp_path)},
                {"provider": "claude_code", "label": "alt", "config_dir": str(tmp_path)},
            ],
        }
    )
    monkeypatch.setattr(cli, "load_config", lambda _p=None: config)
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-xyz")

    with pytest.raises(SystemExit):
        cli._nudge_on_restart([])


def test_cli_restart_triggers_a_detached_restart_without_registering_anything(
    tmp_path, monkeypatch
):
    """The caller no longer has to register: the fresh runtime finds it (#1547)."""
    cli = _cli_config(tmp_path, monkeypatch)
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    triggered: list[str] = []
    monkeypatch.setattr(
        "agentdeck.providers.claude_code.restart.trigger_detached_restart",
        lambda service: triggered.append(service),
    )

    cli._restart_runtime(["--service", "agentdeck-staging-codex.service"])

    assert triggered == ["agentdeck-staging-codex.service"]
    assert _registry(tmp_path).entries() == []


def test_cli_refuses_autodetected_non_runtime_unit(tmp_path, monkeypatch):
    cli = _cli_config(tmp_path, monkeypatch)
    monkeypatch.delenv("AGENTDECK_RUNTIME_UNIT", raising=False)
    monkeypatch.setattr(
        "agentdeck.providers.claude_code.restart.detect_runtime_unit",
        lambda: "some-random.service",
    )
    with pytest.raises(SystemExit):
        cli._restart_runtime([])
    assert _registry(tmp_path).entries() == []


def test_cli_reports_a_restart_that_never_happened(tmp_path, monkeypatch):
    import subprocess

    cli = _cli_config(tmp_path, monkeypatch)

    def boom(_service):
        raise subprocess.CalledProcessError(1, "systemd-run")

    monkeypatch.setattr(
        "agentdeck.providers.claude_code.restart.trigger_detached_restart", boom
    )

    with pytest.raises(SystemExit):
        cli._restart_runtime(["--service", "agentdeck-staging-codex.service"])
    # Nothing was registered before the attempt, so there is nothing left behind
    # for an unrelated future restart to fire on.
    assert _registry(tmp_path).entries() == []
