from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentdeck.config import (
    AppConfig,
    ContextManagementConfig,
    KeyedWorkersConfig,
    ServerConfig,
    load_config,
    owned_session_base_url,
)


def test_context_management_is_off_by_default_and_example_observes():
    assert ContextManagementConfig().mode == "off"
    example = load_config(Path(__file__).parents[1] / "config.example.toml")
    assert example.context_management.mode == "observe"


def test_owned_session_base_url_defaults_to_loopback_web_port(monkeypatch):
    monkeypatch.delenv("AGENTDECK_URL", raising=False)
    config = AppConfig(server=ServerConfig(bind="0.0.0.0", port=9000))
    # bind may be a wildcard; the agent must reach the API over loopback.
    assert owned_session_base_url(config) == "http://127.0.0.1:9000"


def test_keyed_workers_default_to_four_active_kanban_roots():
    assert KeyedWorkersConfig().max_kanban_workers == 4


def test_keyed_workers_allow_disabling_kanban_on_a_shared_canary():
    assert KeyedWorkersConfig(max_kanban_workers=0).max_kanban_workers == 0


def test_keyed_workers_reject_a_negative_kanban_limit():
    with pytest.raises(ValidationError, match="max_kanban_workers must be non-negative"):
        KeyedWorkersConfig(max_kanban_workers=-1)


def test_owned_session_base_url_honors_env_override(monkeypatch):
    monkeypatch.setenv("AGENTDECK_URL", "https://deck.example/ ")
    config = AppConfig(server=ServerConfig(port=8756))
    assert owned_session_base_url(config) == "https://deck.example"


def test_owned_session_base_url_ignores_blank_override(monkeypatch):
    monkeypatch.setenv("AGENTDECK_URL", "   ")
    config = AppConfig(server=ServerConfig(port=8756))
    assert owned_session_base_url(config) == "http://127.0.0.1:8756"
















def test_native_model_override_resolves_each_provider_through_one_map():
    """#789: the New chat default came from a `provider_id == "claude_code"` test."""
    config = load_config(Path(__file__).parents[1] / "config.example.toml")

    assert config.native_model_override("claude_code", "main") == (
        config.claude_workers.for_account("main").model or None
    )
    assert config.native_model_override("codex", "codex") is None
    assert config.native_model_override("nope", "main") is None


def test_an_account_of_a_retired_provider_is_skipped_with_a_warning(tmp_path, caplog):
    """#2600: the live config still listed a Cursor account when the provider went.
    Refusing it would stop both services from starting on that deploy."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[cursor]\nenabled = true\nmodel = "composer-2.5"\n\n'
        '[[accounts]]\nprovider = "codex"\nlabel = "codex"\nconfig_dir = "~/.codex"\n\n'
        '[[accounts]]\nprovider = "cursor"\nlabel = "cursor"\n'
        'config_dir = "~/.config/agentdeck/cursor"\n'
    )

    with caplog.at_level(logging.WARNING, logger="agentdeck.config"):
        config = load_config(path)

    assert [account.key for account in config.build_accounts()] == ["codex:codex"]
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1
    assert "cursor:cursor" in warnings[0] and "#2600" in warnings[0]


def test_history_off_is_refused_with_a_clear_message(tmp_path):
    # The SQLite store is required (#2601); a config that still turns it off must not
    # load as if the setting were honoured.
    path = tmp_path / "config.toml"
    path.write_text("[history]\nenabled = false\n")
    with pytest.raises(ValidationError, match=r"enabled = false is no longer supported"):
        load_config(path)


@pytest.mark.parametrize("history", ["[history]\nenabled = true\n", "[history]\n", ""])
def test_history_on_or_unset_still_loads(tmp_path, history):
    path = tmp_path / "config.toml"
    path.write_text(history)
    config = load_config(path)
    assert not hasattr(config.history, "enabled")


def test_an_account_of_an_unknown_provider_is_still_refused():
    with pytest.raises(ValidationError, match="unknown provider 'nope'"):
        AppConfig.model_validate(
            {"accounts": [{"provider": "nope", "label": "x", "config_dir": "~/x"}]}
        )


def test_the_default_poller_root_is_this_tests_absent_one_not_the_hosts():
    # conftest points AGENTDECK_POLLER_ROOT at a per-test directory; a default config
    # must follow it, or a test renders this host's real poller boards (#2624).
    root = Path(AppConfig().poller.root).expanduser()
    assert root.name == "absent-poller-root"
    assert not root.is_relative_to(Path.home() / ".local/share/agentdeck")


def test_the_default_poller_root_follows_the_env_and_falls_back(monkeypatch, tmp_path):
    from agentdeck.github_accounts import DEFAULT_POLLER_ROOT, POLLER_ROOT_ENV

    monkeypatch.setenv(POLLER_ROOT_ENV, str(tmp_path / "elsewhere"))
    config_file = tmp_path / "config.toml"
    config_file.write_text("", encoding="utf-8")
    assert AppConfig().poller.root == str(tmp_path / "elsewhere")
    assert load_config(config_file).poller.root == str(tmp_path / "elsewhere")

    monkeypatch.delenv(POLLER_ROOT_ENV)
    assert AppConfig().poller.root == DEFAULT_POLLER_ROOT == "~/.local/share/agentdeck/poller"


def test_an_explicit_poller_root_wins_over_the_env(monkeypatch, tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[poller]\nroot = "~/somewhere-else"\n', encoding="utf-8")
    assert load_config(config_file).poller.root == "~/somewhere-else"


def test_a_config_with_removed_sections_and_keys_still_loads(tmp_path):
    """#2864 removed deep search, the chat auto-nudge and dead sections; a host's
    config that still names them must load unchanged rather than refuse to start."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[search]\n"
        "index_transcripts = true\n"
        'index_path = "~/.local/share/agentdeck/session-search.sqlite3"\n'
        "[cursor]\n"
        'state_dir = "~/.local/share/agentdeck/cursor"\n'
        "[work_brief]\n"
        "enabled = true\n"
        "[chat_board]\n"
        "auto_nudge = true\n"
        "auto_nudge_min_interval_s = 60\n"
        "auto_nudge_busy_backoff_s = 300\n"
        "max_workers = 6\n"
        "auto_nudge_stalled_after_s = 900\n"
    )

    config = load_config(path)

    assert not hasattr(config, "search")
    assert not hasattr(config.chat_board, "auto_nudge")
    assert config.chat_board.auto_nudge_stalled_after_s == 900
