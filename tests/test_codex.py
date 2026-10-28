from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from watchfiles import Change

from agentdeck import kanban
from agentdeck.attachments import ATTACHMENT_HEADER, ATTACHMENT_MARKER
from agentdeck.config import AccountConfig
from agentdeck.models import (
    Account,
    Capability,
    InjectResult,
    PendingInteraction,
    Session,
    SessionStatus,
    TranscriptEvent,
)
from agentdeck.providers import PROVIDERS
from agentdeck.providers import _filecache as filecache_mod
from agentdeck.providers.codex import provider as provider_mod
from agentdeck.providers.codex import transcripts
from agentdeck.providers.codex.provider import CodexProvider
from agentdeck.providers.instructions import (
    agentdeck_session_instructions,
    wrap_session_guide,
)
from agentdeck.providers.transcript_reader import FORWARD_SCAN_LIMIT
from agentdeck.state import AppState


def _line(type_: str, payload: dict, timestamp: str = "2026-07-13T10:00:00Z") -> dict:
    return {"timestamp": timestamp, "type": type_, "payload": payload}


def test_new_session_defaults_use_account_config_and_model_fallback(tmp_path):
    account = Account("codex:test", "codex", "test", tmp_path)
    provider = CodexProvider()

    assert provider.new_session_defaults(account) == ("gpt-5.6-sol", "medium")

    (tmp_path / "config.toml").write_text(
        'model = "gpt-5.6-terra"\nmodel_reasoning_effort = "high"\n'
    )
    assert provider.new_session_defaults(account) == ("gpt-5.6-terra", "high")


def test_astra_is_selectable_without_becoming_the_fallback_model(tmp_path):
    account = Account("codex:test", "codex", "test", tmp_path)
    provider = CodexProvider()

    assert "gpt-6-astra" in [choice.value for choice in provider.selectable_models]
    assert provider.new_session_defaults(
        account, model_override="gpt-6-astra"
    ) == ("gpt-6-astra", "medium")
    # Listing a pricier model must not silently move every new chat onto it.
    assert provider.new_session_defaults(account).model == "gpt-5.6-sol"


async def test_new_session_state_hides_the_attachment_transport_note(tmp_path):
    account = Account("codex:test", "codex", "test", tmp_path)
    provider = CodexProvider()
    client = MagicMock()
    client.start_thread = AsyncMock(return_value=InjectResult(True, session_id="thread-1"))
    client.owns.return_value = True
    client.active_turn.return_value = None
    client.interaction.return_value = None
    client.permission_prompts_available.return_value = False
    provider._clients[account.key] = client
    state = MagicMock()
    provider._states[account.key] = state
    path = tmp_path / "1-0123456789abcdef01234567.upload"
    message = (
        f"Read the brief{ATTACHMENT_MARKER}\n{ATTACHMENT_HEADER}\n"
        f'- {{"name": "brief.pdf", "path": "{path}"}}\n'
        "</agentdeck-attachments>"
    )

    result = await provider.start_session(account, tmp_path, message, timeout_s=1)

    assert result.accepted
    session = state.update_session.call_args.args[0]
    assert session.title == "Read the brief"
    assert session.initial_prompt == "Read the brief"
    assert session.last_prompt == "Read the brief"
    client.start_thread.assert_awaited_once()


def _message(role: str, text: str, *, phase: str | None = None) -> dict:
    block_type = "output_text" if role == "assistant" else "input_text"
    payload = {
        "type": "message",
        "role": role,
        "content": [{"type": block_type, "text": text}],
    }
    if phase is not None:
        payload["phase"] = phase
    return _line("response_item", payload)


def test_codex_user_image_is_kept_with_message_and_private_path_wrapper_is_hidden(tmp_path):
    path = tmp_path / "rollout.jsonl"
    image_url = "data:image/png;base64,iVBORw0KGgo="
    message = _message("user", "Inspect this screenshot")
    message["payload"]["content"].extend(
        [
            {
                "type": "input_text",
                "text": '<image name=[Image #1] path="/private/upload.png">',
            },
            {"type": "input_image", "image_url": image_url},
            {"type": "input_text", "text": "</image>"},
        ]
    )
    path.write_text(json.dumps(message) + "\n")

    (event,) = transcripts.read_events(path).events

    assert event.text == "Inspect this screenshot"
    assert event.image_media_types == ("image/png",)
    assert transcripts.transcript_image(path, 1, 0) == ("image/png", b"\x89PNG\r\n\x1a\n")


def test_codex_image_only_message_is_visible_and_unsafe_image_url_is_ignored(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(transcripts, "_MAX_IMAGE_URL_CHARS", 40)
    path = tmp_path / "rollout.jsonl"
    message = _message("user", "")
    message["payload"]["content"] = [
        {"type": "input_image", "image_url": "data:image/jpeg;base64,/9j/"},
        {"type": "input_image", "image_url": "https://example.com/tracker.png"},
        {"type": "input_image", "image_url": "data:image/png;base64," + "a" * 41},
    ]
    path.write_text(json.dumps(message) + "\n")

    (event,) = transcripts.read_events(path).events

    assert event.text is None
    assert event.image_media_types == ("image/jpeg",)
    assert transcripts.transcript_image(path, 1, 0) == ("image/jpeg", b"\xff\xd8\xff")


def _usage(last: dict, total: dict) -> dict:
    return _line(
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "last_token_usage": last,
                "total_token_usage": total,
                "model_context_window": 258_400,
            },
            "rate_limits": None,
        },
    )


def test_context_observation_keeps_token_event_time_and_version(tmp_path):
    path = tmp_path / "rollout.jsonl"
    old = _usage({"input_tokens": 80_000}, {"input_tokens": 80_000})
    old["timestamp"] = "2026-09-04T10:00:00Z"
    current = _usage({"input_tokens": 55_000}, {"input_tokens": 135_000})
    current["timestamp"] = "2026-09-04T10:01:00Z"
    path.write_text("".join(json.dumps(line) + "\n" for line in (old, current)))

    observed = transcripts.context_observation(path)

    assert observed is not None
    assert observed.tokens == 55_000
    assert observed.observed_at == datetime(2026, 9, 4, 10, 1, tzinfo=UTC)
    assert observed.source_version == 1_788_516_060_000_000


async def test_context_observations_require_exact_current_worker_mapping(tmp_path):
    path = tmp_path / "rollout.jsonl"
    usage = _usage({"input_tokens": 55_000}, {"input_tokens": 55_000})
    usage["timestamp"] = "2026-09-04T10:01:00Z"
    path.write_text(json.dumps(usage) + "\n")
    account = _account(tmp_path)
    provider = CodexProvider()
    client = MagicMock()
    client.refresh = AsyncMock(return_value=set())
    client.worker_key.side_effect = lambda session_id: (
        "issue-1488" if session_id == "source" else None
    )
    provider._clients[account.key] = client
    provider._refresh_ok[account.key] = True
    provider._paths[(str(account.root), "source")] = path
    source = Session(
        f"{account.key}:source", account.key, "source", SessionStatus.IDLE,
        context_tokens=55_000,
    )
    unmapped = Session(
        f"{account.key}:other", account.key, "other", SessionStatus.IDLE,
        context_tokens=55_000,
    )

    observations = await provider.context_observations(account, [source, unmapped])

    assert len(observations) == 1
    assert observations[0].worker_key == "issue-1488"
    assert observations[0].source_session_id == "source"


def test_recent_conversation_reads_bounded_tail_and_filters_tools(tmp_path):
    path = tmp_path / "rollout.jsonl"
    lines = [
        _message("user", "old objective"),
        _line("event_msg", {"type": "agent_message_delta", "delta": "x" * 2_000}),
        _message("user", "new objective"),
        _message("assistant", "working on it"),
        _line(
            "response_item",
            {"type": "function_call_output", "call_id": "c", "output": "private output"},
        ),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    events = transcripts.recent_conversation(path, limit=4, tail=900)

    assert [(event.role, event.text) for event in events] == [
        ("user", "new objective"),
        ("assistant", "working on it"),
    ]


def test_delegated_session_keys_requires_machine_status_tool_output(tmp_path):
    child_key = "codex:local:019f6085-5dbc-7f41-80b1-d32de9d80c14"
    quoted_key = "codex:local:019f6085-6cb1-7920-891f-9403d202a6f0"
    status = f"AgentDeck delegation: running (/sessions/{child_key})"
    quoted = f"AgentDeck delegation: running (/sessions/{quoted_key})"
    path = tmp_path / "rollout.jsonl"
    lines = [
        _message("user", f"A quoted example says {quoted}"),
        _line(
            "response_item",
            {"type": "custom_tool_call", "name": "exec", "input": quoted},
        ),
        _line(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "delegate",
                "output": [
                    {
                        "type": "input_text",
                        "text": f"diagnostic: {quoted}\nstarted\n{status}\n",
                    }
                ],
            },
        ),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    assert transcripts.delegated_session_keys(path) == frozenset({child_key})


def test_delegated_session_keys_accepts_json_encoded_output_lines(tmp_path):
    path = tmp_path / "rollout.jsonl"
    child_key = "codex:codex:019f6ee6-394d-7402-80fd-b1f762ebadcd"
    line = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "output": "=== 45285 ===\n"
            + json.dumps(
                {
                    "session_id": 45285,
                    "output": f"AgentDeck delegation: running (/sessions/{child_key})\\n",
                }
            ),
        },
    }
    path.write_text(json.dumps(line) + "\n")

    assert transcripts.delegated_session_keys(path) == frozenset({child_key})


async def test_machine_delegation_status_marks_legacy_child_delegated(tmp_path):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    child_sid = "019f6085-5dbc-7f41-80b1-d32de9d80c14"
    parent = _rollout(tmp_path, parent_sid)
    _rollout(tmp_path, child_sid)
    status = f"AgentDeck delegation: running (/sessions/codex:local:{child_sid})"
    with parent.open("a") as handle:
        handle.write(
            json.dumps(
                _line(
                    "response_item",
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "delegate",
                        "output": status,
                    },
                )
            )
            + "\n"
        )

    sessions = await CodexProvider().scan_sessions(_account(tmp_path))
    found = {session.session_id: session for session in sessions}

    assert found[parent_sid].is_delegated is False
    assert found[child_sid].is_delegated is True
    # The delegated child nests under the chat that delegated it.
    assert found[child_sid].parent_session_key == found[parent_sid].key
    assert found[parent_sid].parent_session_key is None


async def test_legacy_delegation_evidence_outside_session_cap_is_used(
    tmp_path, monkeypatch
):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    child_sid = "019f6085-5dbc-7f41-80b1-d32de9d80c14"
    parent = _rollout(tmp_path, parent_sid)
    child = _rollout(tmp_path, child_sid)
    with parent.open("a") as handle:
        handle.write(
            json.dumps(
                _line(
                    "response_item",
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "delegate",
                        "output": (
                            f"AgentDeck delegation: running (/sessions/codex:local:{child_sid})"
                        ),
                    },
                )
            )
            + "\n"
        )
    old_time = time.time() - 3600
    os.utime(parent, (old_time, old_time))
    os.utime(child, None)
    monkeypatch.setattr(provider_mod, "MAX_SESSIONS", 1)

    (session,) = await CodexProvider().scan_sessions(_account(tmp_path))

    assert session.session_id == child_sid
    assert session.is_delegated is True


async def test_runtime_owned_thread_outside_session_cap_is_still_scanned(
    tmp_path, monkeypatch
):
    old_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    new_sid = "019f6085-5dbc-7f41-80b1-d32de9d80c14"
    old = _rollout(tmp_path, old_sid)
    _rollout(tmp_path, new_sid)
    old_time = time.time() - 3600
    os.utime(old, (old_time, old_time))
    monkeypatch.setattr(provider_mod, "MAX_SESSIONS", 1)
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.refresh = AsyncMock()
    client.owned_thread_ids.return_value = frozenset({old_sid})
    client.owns.side_effect = lambda thread_id: thread_id == old_sid
    client.active_turn.return_value = None
    client.interaction.return_value = None
    provider._clients[account.key] = client

    sessions = await provider.scan_sessions(account)

    assert {session.session_id for session in sessions} == {old_sid, new_sid}


def _rollout(root: Path, sid: str, *, old: bool = False) -> Path:
    directory = root / "sessions" / "2026" / "07" / "13"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-07-13T10-00-00-{sid}.jsonl"
    first_usage = {
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
        "total_tokens": 120,
    }
    second_usage = {
        "input_tokens": 80,
        "cached_input_tokens": 30,
        "output_tokens": 10,
        "reasoning_output_tokens": 2,
        "total_tokens": 90,
    }
    total_usage = {
        "input_tokens": 180,
        "cached_input_tokens": 90,
        "output_tokens": 30,
        "reasoning_output_tokens": 7,
        "total_tokens": 210,
    }
    lines = [
        _line(
            "session_meta",
            {
                "session_id": sid,
                "id": sid,
                "timestamp": "2026-07-13T10:00:00Z",
                "cwd": "/tmp/codex-project",
                "originator": "codex-tui",
                "cli_version": "0.144.3",
                "source": "cli",
                "thread_source": "user",
                "model_provider": "openai",
                "base_instructions": {},
                "history_mode": "save-all",
                "context_window": {"window_id": "window-id"},
                "git": {"branch": "main"},
            },
        ),
        _line("turn_context", {"model": "gpt-5.6-sol", "cwd": "/tmp/codex-project"}),
        _message("user", "<environment_context>synthetic metadata</environment_context>"),
        _message("user", "Build the parser safely"),
        _line(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec_command",
                "call_id": "call-1",
                "input": json.dumps({"cmd": "uv run pytest -q"}),
            },
        ),
        _line(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": "89 passed",
            },
        ),
        _usage(first_usage, first_usage),
        _message("assistant", "First update", phase="commentary"),
        _message("user", "Now finish it"),
        _message("assistant", "Implemented and tested", phase="final_answer"),
        _usage(second_usage, total_usage),
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in lines))
    if old:
        old_time = time.time() - 3600
        os.utime(path, (old_time, old_time))
    return path


def _account(root: Path) -> Account:
    return Account(key="codex:local", provider_id="codex", label="local", root=root)


async def test_codex_session_discovery_and_metadata(tmp_path):
    live_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    idle_sid = "019f5b22-ec93-7e52-9a15-d0cdb5c0e8c3"
    _rollout(tmp_path, live_sid)
    _rollout(tmp_path, idle_sid, old=True)

    provider = CodexProvider()
    sessions = await provider.scan_sessions(_account(tmp_path))
    found = {session.session_id: session for session in sessions}

    live = found[live_sid]
    assert live.status == SessionStatus.LIVE
    assert live.thinking is True
    assert live.title == "Build the parser safely"
    assert live.last_prompt == "Now finish it"
    assert live.last_text == "Implemented and tested"
    assert live.last_role == "agent"
    assert live.cwd == Path("/tmp/codex-project")
    assert live.model == "gpt-5.6-sol"
    assert live.kind is None
    assert live.started_at.isoformat() == "2026-07-13T10:00:00+00:00"
    assert live.context_tokens == 80
    assert live.tokens.input_tokens == 90  # 180 input includes 90 cached
    assert live.tokens.output_tokens == 30
    assert live.tokens.cache_read_tokens == 90
    assert live.tokens.total == 210
    assert live.capabilities == frozenset({Capability.TRANSCRIPT})
    assert live.deep_link is None
    assert live.show_when_idle is True
    assert found[idle_sid].status == SessionStatus.IDLE
    assert found[idle_sid].thinking is False
    assert found[idle_sid].show_when_idle is True


async def test_approval_review_rollout_cannot_replace_parent_session(tmp_path):
    parent_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    reviewer_file_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c07"
    _rollout(tmp_path, parent_sid)
    reviewer = _rollout(tmp_path, reviewer_file_sid)

    lines = [json.loads(line) for line in reviewer.read_text().splitlines()]
    lines[0]["payload"]["session_id"] = parent_sid
    lines[0]["payload"]["id"] = parent_sid
    lines[0]["payload"]["base_instructions"] = {
        "text": "You are judging one planned coding-agent action.\nAssess its risk."
    }
    lines[3] = _message("user", "Internal approval assessment prompt")
    reviewer.write_text("".join(json.dumps(item) + "\n" for item in lines))

    assert transcripts.transcript_meta(reviewer).is_approval_review is True
    provider = CodexProvider()
    (session,) = await provider.scan_sessions(_account(tmp_path))
    assert session.session_id == parent_sid
    assert session.title == "Build the parser safely"


def _helper_rollout(root: Path, child_sid: str, parent_sid: str, **payload) -> Path:
    """One helper rollout that reuses ``parent_sid`` as its own ``session_id``.

    Both real shapes name their parent as a top-level ``parent_thread_id``; only
    a spawned agent also carries the structured ``thread_spawn``, so ``source``
    is what the caller varies.
    """
    path = _rollout(root, child_sid)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    lines[0]["payload"].update(
        {
            "session_id": parent_sid,
            "id": child_sid,
            "parent_thread_id": parent_sid,
            "thread_source": "subagent",
            **payload,
        }
    )
    path.write_text("".join(json.dumps(item) + "\n" for item in lines))
    return path


def _guardian(root: Path, guardian_sid: str, parent_sid: str) -> Path:
    """A real auto-review rollout: no ``thread_spawn``, top-level parent id."""
    return _helper_rollout(
        root,
        guardian_sid,
        parent_sid,
        source={"subagent": {"other": "guardian"}},
        base_instructions={
            "text": "You are judging one planned coding-agent action.\nAssess it."
        },
    )


def _spawned(root: Path, child_sid: str, parent_sid: str) -> Path:
    """A spawned agent: the structured shape, which names its parent twice."""
    return _helper_rollout(
        root,
        child_sid,
        parent_sid,
        source={
            "subagent": {"thread_spawn": {"parent_thread_id": parent_sid, "depth": 1}}
        },
    )


def test_guardian_rollout_names_its_parent_from_the_top_level_field(tmp_path):
    parent_sid = "019fd2d7-3d1d-72d1-9aaf-1d8deb84d74a"
    guardian_sid = "019fd30a-4571-7393-aef5-62189d043f92"

    meta = transcripts.transcript_meta(_guardian(tmp_path, guardian_sid, parent_sid))

    assert meta.is_approval_review is True
    assert meta.is_subagent is True
    assert meta.is_spawned_subagent is False
    assert meta.agent_id == guardian_sid
    assert meta.parent_session_id == parent_sid


async def _scan_with_state(root: Path) -> tuple[list[Session], AppState]:
    scanned = await CodexProvider().scan_sessions(_account(root))
    state = AppState()
    for session in scanned:
        state.update_session(session)
    return scanned, state


async def test_the_path_cache_does_not_cross_account_roots(tmp_path):
    """The provider is a process-wide singleton, so the cache has to be keyed by
    the directory a session id resolves against — not by an account label two
    roots could share. Mirrors the Claude fix from #737 (#790)."""
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    provider = CodexProvider()
    path = _rollout(tmp_path / "one", sid)
    (tmp_path / "two" / "sessions").mkdir(parents=True)

    one = _account(tmp_path / "one")
    (session,) = await provider.scan_sessions(one)
    assert provider._transcript_path(one, session) == path

    # The other root has no such rollout, and must not inherit this one's.
    two = _account(tmp_path / "two")
    assert provider._transcript_path(two, session) is None


async def test_subagent_rollout_cannot_replace_parent_session(tmp_path):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    helper_sid = "019f6089-5b05-76d2-b480-f5cb5b793bfb"
    parent = _rollout(tmp_path, parent_sid)
    helper = _rollout(tmp_path, helper_sid)

    lines = [json.loads(line) for line in helper.read_text().splitlines()]
    # This is the real structured shape emitted by spawned agents and the
    # guardian/auto-review helper: a distinct file id but the parent's session.
    lines[0]["payload"].update(
        {
            "session_id": parent_sid,
            "source": {"subagent": {"other": "guardian"}},
            "thread_source": "subagent",
            "originator": "agentdeck",
        }
    )
    lines[3] = _message("user", "Internal guardian assessment prompt")
    helper.write_text("".join(json.dumps(item) + "\n" for item in lines))

    meta = transcripts.transcript_meta(helper)
    assert meta.session_id == parent_sid
    assert meta.is_subagent is True
    assert meta.is_spawned_subagent is False

    provider = CodexProvider()
    (session,) = await provider.scan_sessions(_account(tmp_path))
    assert session.session_id == parent_sid
    assert session.title == "Build the parser safely"

    # The fallback path lookup must apply the same filter; otherwise losing the
    # in-memory path cache can temporarily bring the helper transcript back.
    provider._paths.clear()
    assert provider._transcript_path(_account(tmp_path), session) == parent


async def test_running_spawned_agents_are_counted_on_parent_session(tmp_path):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    _rollout(tmp_path, parent_sid, old=True)

    def helper(sid: str, source: dict, boundaries: list[str]) -> Path:
        path = _rollout(tmp_path, sid)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        lines[0]["payload"].update(
            {
                "session_id": parent_sid,
                "source": {"subagent": source},
                "thread_source": "subagent",
                "agent_nickname": "Faraday" if "thread_spawn" in source else None,
                "agent_role": "scout" if "thread_spawn" in source else None,
            }
        )
        lines.extend(_line("event_msg", {"type": boundary}) for boundary in boundaries)
        path.write_text("".join(json.dumps(item) + "\n" for item in lines))
        return path

    active = helper(
        "019f6085-5dbc-7f41-80b1-d32de9d80c14",
        {"thread_spawn": {"parent_thread_id": parent_sid, "depth": 1}},
        ["task_started"],
    )
    completed = helper(
        "019f6085-6cb1-7920-891f-9403d202a6f0",
        {"thread_spawn": {"parent_thread_id": parent_sid, "depth": 1}},
        ["task_started", "task_complete"],
    )
    expired = helper(
        "019f6085-7db2-7031-8be2-a51f6aa6b9bd",
        {"thread_spawn": {"parent_thread_id": parent_sid, "depth": 1}},
        ["task_started", "task_complete"],
    )
    helper(
        "019f6089-5b05-76d2-b480-f5cb5b793bfb",
        {"other": "guardian"},
        ["task_started"],
    )

    assert transcripts.transcript_meta(active).is_spawned_subagent is True
    assert transcripts.transcript_meta(active).task_active is True
    assert transcripts.transcript_meta(completed).task_active is False

    # Durable task boundaries outrank a quiet file: a long-running subagent must
    # remain visible instead of disappearing after the generic 30-second window.
    # Quiet for eight minutes is still an Active Turn; it stalls at ten.
    old_time = time.time() - 8 * 60
    os.utime(active, (old_time, old_time))
    expired_time = time.time() - 60 * 60
    os.utime(expired, (expired_time, expired_time))

    provider = CodexProvider()
    scanned = await provider.scan_sessions(_account(tmp_path))
    # The parent stays top-level; spawned subagents are now also child sessions
    # nested under it (parent_session_key set), while still counted on the parent.
    (session,) = [s for s in scanned if s.parent_session_key is None]
    assert session.session_id == parent_sid
    assert session.status == SessionStatus.IDLE
    assert session.thinking is False
    assert session.activity is None
    assert session.subagent_count == 1
    assert len(session.subagents) == 2
    assert session.subagents[0].nickname == "Faraday"
    assert session.subagents[0].role == "scout"
    assert session.subagents[0].status == "quiet"
    assert session.subagents[0].task == "Build the parser safely"
    assert session.subagents[1].status == "finished"
    assert session.subagents[1].result == "Implemented and tested"
    assert provider._subagent_names["019f6085-7db2-7031-8be2-a51f6aa6b9bd"] == "Faraday"
    # The recent spawned subagents surface as child sessions under the parent.
    children = [s for s in scanned if s.parent_session_key == session.key]
    assert {s.session_id for s in children} == {
        "019f6085-5dbc-7f41-80b1-d32de9d80c14",  # active
        "019f6085-6cb1-7920-891f-9403d202a6f0",  # completed (recent)
    }
    # A cheap liveness sweep only reads the quiet parent rollout; it must not
    # invent provider-local activity for work represented by the child.
    assert provider.sweep_liveness(_account(tmp_path), [session], AppState()) == []
    assert session.thinking is False


def test_codex_compacts_subagent_notifications_without_clobbering_last_prompt(tmp_path):
    path = tmp_path / "rollout.jsonl"
    notification = (
        "<subagent_notification>\n"
        + json.dumps(
            {
                "agent_path": "019f6085-5dbc-7f41-80b1-d32de9d80c14",
                "status": {
                    "completed": "Read-only audit complete.\n\nFound one parser gap."
                },
            }
        )
        + "\n</subagent_notification>"
    )
    path.write_text(
        "".join(
            json.dumps(item) + "\n"
            for item in [
                _message("user", "Review the UX"),
                _message("user", notification),
            ]
        )
    )

    meta = transcripts.transcript_meta(path)
    events = transcripts.read_events(path).events

    assert meta.last_prompt == "Review the UX"
    assert len(events) == 2
    update = events[1]
    assert update.role == "system"
    assert update.subagent_status == "finished"
    assert update.subagent_id == "019f6085-5dbc-7f41-80b1-d32de9d80c14"
    assert update.tool_summary == "Read-only audit complete."
    assert update.text == "Read-only audit complete.\n\nFound one parser gap."


def test_codex_compacts_turn_aborted_without_clobbering_last_prompt(tmp_path):
    path = tmp_path / "rollout.jsonl"
    detail = (
        "The user interrupted the previous turn on purpose. Any running commands "
        "may have partially executed."
    )
    aborted = f"<turn_aborted>\n{detail}\n</turn_aborted>"
    path.write_text(
        "".join(
            json.dumps(item) + "\n"
            for item in [
                _message("user", "Review the UX"),
                _message("user", aborted),
            ]
        )
    )

    meta = transcripts.transcript_meta(path)
    events = transcripts.read_events(path).events

    assert meta.last_prompt == "Review the UX"
    assert len(events) == 2
    update = events[1]
    assert update.role == "system"
    assert update.text is None
    assert update.tool_name == "turn_aborted"
    assert update.tool_display_name == "Turn aborted"
    assert update.tool_summary == "Interrupted by user"
    assert update.tool_detail == detail
    assert update.turn_continues is False


def test_codex_labels_wrapped_subagent_operations(tmp_path):
    path = tmp_path / "rollout.jsonl"
    lines = [
        _line(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": (
                    "const a = await tools.multi_agent_v1__spawn_agent("
                    "{agent_type:\"scout\"});"
                ),
            },
        ),
        _line(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": "const r = await tools.multi_agent_v1__wait_agent({targets:[\"id\"]});",
            },
        ),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    spawn, wait = transcripts.read_events(path).events
    assert spawn.tool_display_name == "Start agents"
    assert wait.tool_display_name == "Wait for agents"


def test_codex_finds_latest_task_boundary_outside_bounded_meta_windows(tmp_path):
    path = tmp_path / "large-rollout.jsonl"
    lines = [
        _line("session_meta", {"session_id": "parent", "id": "child"}),
        _line(
            "event_msg",
            {"type": "task_started"},
            timestamp="2026-07-17T06:28:00Z",
        ),
        _message("assistant", "x" * (160 * 1024)),
        _line(
            "event_msg",
            {"type": "task_complete", "last_agent_message": "done"},
            timestamp="2026-07-17T06:33:00Z",
        ),
        _message("assistant", "y" * (300 * 1024)),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    meta = transcripts.transcript_meta(path)

    assert meta.task_active is False
    assert meta.task_status == "finished"
    assert meta.task_started_at.isoformat() == "2026-07-17T06:28:00+00:00"
    assert meta.task_finished_at.isoformat() == "2026-07-17T06:33:00+00:00"


async def test_subagent_roster_stays_scoped_to_latest_completed_parent_turn(tmp_path):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    parent = _rollout(tmp_path, parent_sid)
    with parent.open("a") as handle:
        for item in [
            _line("event_msg", {"type": "task_started"}, "2026-07-17T10:01:00Z"),
            _line("event_msg", {"type": "task_complete"}, "2026-07-17T10:02:00Z"),
            _line("event_msg", {"type": "task_started"}, "2026-07-17T10:03:00Z"),
            _line("event_msg", {"type": "task_complete"}, "2026-07-17T10:04:00Z"),
        ]:
            handle.write(json.dumps(item) + "\n")

    def completed_child(sid: str, nickname: str, started: str, finished: str) -> None:
        path = _rollout(tmp_path, sid)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        lines[0]["timestamp"] = started
        lines[0]["payload"].update(
            {
                "session_id": parent_sid,
                "id": sid,
                "timestamp": started,
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_sid,
                            "agent_nickname": nickname,
                            "agent_role": "scout",
                        }
                    }
                },
                "thread_source": "subagent",
            }
        )
        lines.extend(
            [
                _line("event_msg", {"type": "task_started"}, started),
                _line("event_msg", {"type": "task_complete"}, finished),
            ]
        )
        path.write_text("".join(json.dumps(item) + "\n" for item in lines))

    completed_child(
        "019f6085-5dbc-7f41-80b1-d32de9d80c14",
        "OldAgent",
        "2026-07-17T10:01:10Z",
        "2026-07-17T10:01:50Z",
    )
    completed_child(
        "019f6085-6cb1-7920-891f-9403d202a6f0",
        "CurrentAgent",
        "2026-07-17T10:03:10Z",
        "2026-07-17T10:03:50Z",
    )

    provider = CodexProvider()
    scanned = await provider.scan_sessions(_account(tmp_path))
    (session,) = [s for s in scanned if s.parent_session_key is None]

    assert session.subagent_count == 0
    assert [agent.nickname for agent in session.subagents] == ["CurrentAgent"]


async def test_spawn_output_names_fast_subagent_notification_before_periodic_scan(tmp_path):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    path = _rollout(tmp_path, parent_sid)
    agent_id = "019f6085-5dbc-7f41-80b1-d32de9d80c14"
    notification = (
        "<subagent_notification>\n"
        + json.dumps(
            {"agent_path": agent_id, "status": {"completed": "Audit complete."}}
        )
        + "\n</subagent_notification>"
    )
    with path.open("a") as handle:
        for item in [
            _line(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "output": [
                        {"type": "input_text", "text": "Script completed"},
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {"agent_id": agent_id, "nickname": "Faraday"}
                            ),
                        },
                    ],
                },
            ),
            _message("user", notification),
        ]:
            handle.write(json.dumps(item) + "\n")

    provider = CodexProvider()
    (session,) = await provider.scan_sessions(_account(tmp_path))
    detail = await provider.load_transcript(_account(tmp_path), session)
    update = next(event for event in detail.events if event.subagent_status)

    assert update.subagent_name == "Faraday"


async def test_completed_exec_session_is_injectable(tmp_path):
    sid = "019f5b5b-6281-7a00-a197-d020a1243d2d"
    directory = tmp_path / "sessions" / "2026" / "07" / "13"
    directory.mkdir(parents=True)
    path = directory / f"rollout-2026-07-13T10-00-00-{sid}.jsonl"
    lines = [
        _line(
            "session_meta",
            {"session_id": sid, "cwd": str(tmp_path), "source": "exec"},
        ),
        _line("event_msg", {"type": "task_started"}),
        _line("event_msg", {"type": "task_complete"}),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    old_time = time.time() - 3600
    os.utime(path, (old_time, old_time))

    provider = CodexProvider()
    (session,) = await provider.scan_sessions(_account(tmp_path))
    assert session.kind == "exec"
    assert Capability.INJECT in session.capabilities
    assert Capability.DEEPLINK not in session.capabilities


async def test_codex_transcript_parsing_and_token_totals(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    path = _rollout(tmp_path, sid)
    provider = CodexProvider()
    (session,) = await provider.scan_sessions(_account(tmp_path))
    detail = await provider.load_transcript(_account(tmp_path), session)

    visible = [event for event in detail.events if event.text or event.tool_name]
    assert [event.role for event in visible] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    assert visible[0].text == "Build the parser safely"
    assert visible[1].tool_name == "exec_command"
    assert visible[1].tool_summary == "cmd: uv run pytest -q"
    assert visible[1].model == "gpt-5.6-sol"
    assert visible[2].text == "89 passed"
    assert visible[-1].text == "Implemented and tested"
    assert visible[-1].model == "gpt-5.6-sol"
    assert detail.tokens.input_tokens == 90
    assert detail.tokens.cache_read_tokens == 90
    assert detail.tokens.output_tokens == 30
    assert detail.tokens.total == 210
    assert detail.model == "gpt-5.6-sol"
    assert detail.skipped == 0
    token_events = [event for event in detail.events if event.tokens]
    assert token_events[-1].tokens.cache_read_tokens == 30
    assert (await provider.last_event(_account(tmp_path), session)).text == (
        "Implemented and tested"
    )

    offset, seq = transcripts.transcript_cursor(path)
    assert offset == path.stat().st_size
    assert seq == 11


def test_codex_partial_and_malformed_lines_resume(tmp_path):
    path = tmp_path / "rollout.jsonl"
    first = _message("user", "hello")
    partial = '{"timestamp":"2026-07-13T10:00:01Z","type":"response_item"'
    path.write_text(json.dumps(first) + "\n" + "{not json}\n" + partial)

    read = transcripts.read_events(path)
    assert [event.text for event in read.events] == ["hello"]
    assert read.skipped == 1
    assert read.seq == 2
    assert read.byte_offset < path.stat().st_size

    with path.open("a") as handle:
        handle.write(
            ',"payload":{"type":"message","role":"assistant",'
            '"content":[{"type":"output_text","text":"done"}]}}\n'
        )
    tail = transcripts.read_events(path, byte_offset=read.byte_offset, seq=read.seq)
    assert [event.text for event in tail.events] == ["done"]
    assert tail.seq == 3
    assert tail.byte_offset == path.stat().st_size


def test_codex_extracts_edited_file_from_apply_patch(tmp_path):
    path = tmp_path / "rollout.jsonl"
    call = _line(
        "response_item",
        {
            "type": "custom_tool_call",
            "name": "apply_patch",
            "input": "*** Begin Patch\n*** Update File: src/agentdeck/app.py\n@@\n*** End Patch",
        },
    )
    path.write_text(json.dumps(call) + "\n")

    (event,) = transcripts.read_events(path).events
    assert event.tool_name == "apply_patch"
    assert event.tool_summary == "path: src/agentdeck/app.py"
    assert "*** Update File: src/agentdeck/app.py" in event.tool_detail


def test_codex_summarizes_wrapped_tools_and_keeps_private_reasoning_heartbeat(tmp_path):
    path = tmp_path / "rollout.jsonl"
    lines = [
        _line(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": (
                    'const r = await tools.exec_command({cmd:"uv run pytest -q",'
                    'workdir:"/tmp"}); text(r.output)'
                ),
            },
        ),
        _line(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": (
                    'const patch = "*** Begin Patch\\n*** Update File: src/app.py\\n'
                    '@@\\n-old\\n+new\\n*** End Patch"; tools.apply_patch(patch)'
                ),
            },
        ),
        _line("response_item", {"type": "reasoning", "summary": []}),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    tool, edit, heartbeat = transcripts.read_events(path).events
    assert tool.tool_name == "exec"
    assert tool.tool_summary == "cmd: uv run pytest -q"
    assert tool.tool_detail == (
        "Command\nuv run pytest -q\n\nWorking directory\n/tmp"
    )
    assert edit.tool_summary == "path: src/app.py"
    assert edit.tool_detail.startswith("*** Begin Patch\n*** Update File: src/app.py")
    assert heartbeat.role == "system"
    assert heartbeat.tool_name == "reasoning"
    assert heartbeat.tool_summary == "Thinking"
    assert heartbeat.text is None


def test_codex_parses_json_quoted_exec_wrapper_fields(tmp_path):
    path = tmp_path / "rollout.jsonl"
    call = _line(
        "response_item",
        {
            "type": "custom_tool_call",
            "name": "exec",
            "input": (
                'const r = await tools.exec_command({"cmd":"sed -n \'1,20p\' app.py",'
                '"workdir":"/srv/app","yield_time_ms":10000,'
                '"max_output_tokens":12000}); text(r.output);'
            ),
        },
    )
    path.write_text(json.dumps(call) + "\n")

    (event,) = transcripts.read_events(path).events
    assert event.tool_summary == "cmd: sed -n '1,20p' app.py"
    assert event.tool_detail == (
        "Command\nsed -n '1,20p' app.py\n\n"
        "Working directory\n/srv/app\n\n"
        "Wait before update\n10 seconds\n\n"
        "Output limit\n12,000 tokens"
    )


def test_codex_filters_only_internal_system_preamble(tmp_path):
    path = tmp_path / "rollout.jsonl"
    messages = [
        _message("developer", "<permissions instructions>internal sandbox policy"),
        _message(
            "system",
            "You are /root, the primary agent in a team of agents collaborating here",
        ),
        _message("developer", "<multi_agent_mode>Do not delegate</multi_agent_mode>"),
        _message("system", "A genuine system-visible conversation message"),
        _message("user", "<permissions instructions>please explain this tag"),
    ]
    path.write_text("".join(json.dumps(message) + "\n" for message in messages))

    visible = transcripts.read_events(path).events

    assert [(event.role, event.text) for event in visible] == [
        ("system", "A genuine system-visible conversation message"),
        ("user", "<permissions instructions>please explain this tag"),
    ]


def test_codex_strips_internal_memory_citations_from_assistant_text(tmp_path):
    path = tmp_path / "rollout.jsonl"
    answer = "Implemented and tested."
    citation = """<oai-mem-citation>
<citation_entries>
MEMORY.md:40-52|note=[internal provenance]
</citation_entries>
<rollout_ids>
019f5d1e-a09f-77a3-a062-c36b844c7675
</rollout_ids>
</oai-mem-citation>"""
    path.write_text(json.dumps(_message("assistant", f"{answer}\n\n{citation}")) + "\n")

    events = transcripts.read_events(path).events
    meta = transcripts.transcript_meta(path)

    assert [event.text for event in events] == [answer]
    assert meta.last_text == answer


def test_codex_drops_truncated_internal_memory_citation(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        json.dumps(_message("assistant", "Visible answer\n<oai-mem-citation>partial")) + "\n"
    )

    assert [event.text for event in transcripts.read_events(path).events] == ["Visible answer"]


def test_codex_skips_repository_instruction_preambles_for_title(tmp_path):
    path = tmp_path / "rollout.jsonl"
    preambles = [
        "# AGENTS.md instructions for /tmp/worktree\n\n<INSTRUCTIONS>rules</INSTRUCTIONS>",
        "<INSTRUCTIONS>\nrepository rules\n</INSTRUCTIONS>",
        "@.cursorrules\n@.cursor/memory/project-notes.mdc\n\n# Codebase map",
        "@.cursor/memory/project-notes.mdc\n\n# Project notes",
    ]
    messages = [*(_message("user", text) for text in preambles), _message("user", "Fix it")]
    path.write_text("".join(json.dumps(message) + "\n" for message in messages))

    meta = transcripts.transcript_meta(path)
    visible = transcripts.read_events(path).events

    assert meta.title == "Fix it"
    assert meta.first_prompt == "Fix it"
    assert meta.last_prompt == "Fix it"
    assert [event.text for event in visible] == ["Fix it"]


def test_codex_hides_new_chat_orchestration_preamble_and_titles_from_prompt(tmp_path):
    path = tmp_path / "rollout.jsonl"
    messages = [
        _message("developer", "<permissions instructions>\ninternal sandbox policy"),
        _message(
            "developer",
            "You are `/root`, the primary agent in a team of agents "
            "collaborating to fulfill the user's goals.",
        ),
        _message(
            "developer",
            "<multi_agent_mode>Do not spawn sub-agents.</multi_agent_mode>",
        ),
        _message(
            "user",
            "<recommended_plugins>\nHere is a list of plugins that are available "
            "but not installed.</recommended_plugins>",
        ),
        _message("user", "Build an assistant inside AgentDeck"),
    ]
    path.write_text("".join(json.dumps(message) + "\n" for message in messages))

    meta = transcripts.transcript_meta(path)
    visible = transcripts.read_events(path).events

    assert meta.title == "Build an assistant inside AgentDeck"
    assert meta.first_prompt == "Build an assistant inside AgentDeck"
    assert meta.last_prompt == "Build an assistant inside AgentDeck"
    assert [(event.role, event.text) for event in visible] == [
        ("user", "Build an assistant inside AgentDeck")
    ]


async def test_codex_hides_launcher_source_but_preserves_exec_kind(tmp_path):
    vscode_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c07"
    exec_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c08"
    vscode_path = _rollout(tmp_path, vscode_sid)
    exec_path = _rollout(tmp_path, exec_sid)
    for path, source in ((vscode_path, "vscode"), (exec_path, "exec")):
        lines = path.read_text().splitlines()
        meta = json.loads(lines[0])
        meta["payload"]["source"] = source
        lines[0] = json.dumps(meta)
        path.write_text("\n".join(lines) + "\n")

    provider = CodexProvider()
    sessions = await provider.scan_sessions(_account(tmp_path))
    found = {session.session_id: session for session in sessions}

    assert transcripts.transcript_meta(vscode_path).kind == "vscode"
    assert found[vscode_sid].kind is None
    assert found[vscode_sid].is_delegated is False
    assert found[exec_sid].kind == "exec"
    assert found[exec_sid].is_delegated is True


def test_codex_final_message_prefers_task_complete(tmp_path):
    # A delegation's final message must be Codex's canonical turn result
    # (task_complete.last_agent_message), NOT the last assistant item — otherwise
    # approval/escalation or other intermediate noise can leak in as the reported
    # result (observed: a delegation returning `{"outcome":"allow"}`).
    path = tmp_path / "rollout.jsonl"
    lines = [
        _message("user", "run the thing"),
        _message("assistant", "intermediate progress note"),
        _line(
            "event_msg",
            {"type": "task_complete", "last_agent_message": "THE CANONICAL ANSWER"},
        ),
        _message("assistant", ""),  # trailing noise must not win
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    meta = transcripts.transcript_meta(path)
    assert meta.last_agent_message == "THE CANONICAL ANSWER"
    # It overrides the last assistant item, which session_result falls back to.
    assert meta.last_text == "intermediate progress note"


def test_codex_final_message_falls_back_without_task_complete(tmp_path):
    # No completed turn -> fall back to the last assistant item.
    path = tmp_path / "rollout.jsonl"
    lines = [
        _message("user", "run the thing"),
        _message("assistant", "partial answer so far"),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    meta = transcripts.transcript_meta(path)
    assert meta.last_agent_message is None
    assert meta.last_text == "partial answer so far"


def test_codex_preserves_long_conversation_messages(tmp_path):
    path = tmp_path / "rollout.jsonl"
    long_user_text = "begin\n" + ("user content " * 500) + "\nuser end"
    long_assistant_text = "begin\n" + ("assistant content " * 500) + "\nassistant end"
    lines = [
        _message("user", long_user_text),
        _message("assistant", long_assistant_text),
        _line(
            "response_item",
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": long_assistant_text}],
            },
        ),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    events = transcripts.read_events(path).events

    assert [event.text for event in events] == [
        long_user_text,
        long_assistant_text,
        long_assistant_text,
    ]


async def test_codex_sweep_refreshes_status_and_tail_metadata(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    path = _rollout(tmp_path, sid, old=True)
    provider = CodexProvider()
    account = _account(tmp_path)
    (session,) = await provider.scan_sessions(account)
    assert session.status == SessionStatus.IDLE

    new_prompt = _message("user", "One more change")
    new_prompt["timestamp"] = datetime.now(UTC).isoformat()
    with path.open("a") as handle:
        handle.write(json.dumps(new_prompt) + "\n")
    changed = provider.sweep_liveness(account, [session], AppState())
    assert changed == [session]
    assert session.status == SessionStatus.LIVE
    assert session.thinking is True
    assert session.last_prompt == "One more change"
    assert session.initial_prompt is not None
    assert session.last_role == "user"
    assert provider.sweep_liveness(account, [session], AppState()) == []


async def test_codex_sweep_applies_unchanged_file_clock_transition(
    tmp_path, monkeypatch
):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    (session,) = await provider.scan_sessions(account)
    assert session.status == SessionStatus.LIVE
    assert provider.sweep_liveness(account, [session], AppState()) == []

    aged = datetime.now(UTC) - timedelta(seconds=provider_mod.LIVE_WINDOW_S + 1)
    session.last_activity = aged
    monkeypatch.setattr("agentdeck.providers.codex.provider._mtime", lambda _path: aged)

    assert provider.sweep_liveness(account, [session], AppState()) == [session]
    assert session.status == SessionStatus.IDLE


async def test_owned_runtime_projection_matches_scan_callback_and_sweep(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.refresh = AsyncMock()
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    client.interaction.return_value = None
    client.permission_prompts_available.return_value = True
    provider._clients[account.key] = client

    (session,) = await provider.scan_sessions(account)
    projected_fields = (
        session.status,
        session.thinking,
        session.activity,
        session.question,
        session.kind,
        session.capabilities,
    )

    session.status = SessionStatus.IDLE
    session.thinking = False
    session.activity = None
    session.question = "stale question"
    session.kind = None
    session.capabilities = frozenset()
    state = AppState()
    state.sessions = {session.key: session}
    provider._runtime_changed(account, state, sid)
    callback_fields = (
        session.status,
        session.thinking,
        session.activity,
        session.question,
        session.kind,
        session.capabilities,
    )

    session.status = SessionStatus.IDLE
    session.thinking = False
    session.activity = None
    session.question = "stale question"
    session.kind = None
    session.capabilities = frozenset()
    provider.sweep_liveness(account, [session], state)
    sweep_fields = (
        session.status,
        session.thinking,
        session.activity,
        session.question,
        session.kind,
        session.capabilities,
    )

    assert projected_fields == callback_fields == sweep_fields


async def test_owned_runtime_projection_uses_canonical_activity_policy(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.refresh = AsyncMock()
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    client.interaction.return_value = None
    client.permission_prompts_available.return_value = True
    provider._clients[account.key] = client
    provider._cached_last_event = MagicMock(
        return_value=TranscriptEvent(
            seq=1,
            role="assistant",
            tool_name="AskUserQuestion",
        )
    )

    (session,) = await provider.scan_sessions(account)

    # A question tool is waiting, not working. The owned-runtime projection
    # must preserve the canonical activity_label verdict instead of inventing
    # a provider-local "Using AskUserQuestion" label.
    assert session.status == SessionStatus.LIVE
    assert session.activity is None


async def test_owned_runtime_projection_suppresses_controls_during_outage(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.refresh = AsyncMock(side_effect=OSError("runtime unavailable"))
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    client.permission_prompts_available.return_value = True
    client.interaction.return_value = None
    provider._clients[account.key] = client

    (unavailable,) = await provider.scan_sessions(account)
    assert unavailable.capabilities == frozenset({Capability.TRANSCRIPT})

    client.refresh = AsyncMock()
    (recovered,) = await provider.scan_sessions(account)
    assert Capability.INJECT in recovered.capabilities
    assert Capability.STEER in recovered.capabilities
    assert Capability.INTERRUPT in recovered.capabilities
    assert Capability.PERMISSION_PROMPTS in recovered.capabilities


def test_watch_health_edges_reproject_stored_runtime_sessions(tmp_path):
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    client.permission_prompts_available.return_value = True
    interaction = PendingInteraction(
        id="question-1",
        kind="question",
        thread_id="thread-1",
        turn_id="turn-1",
        title="Choose a database",
    )
    client.interaction.return_value = interaction
    provider._clients[account.key] = client
    session = Session(
        key=f"{account.key}:thread-1",
        account_key=account.key,
        session_id="thread-1",
        status=SessionStatus.LIVE,
        kind="appServer",
        capabilities=frozenset(
            {
                Capability.TRANSCRIPT,
                Capability.INJECT,
                Capability.STEER,
                Capability.INTERRUPT,
            }
        ),
    )
    state = AppState()
    state.sessions = {session.key: session}
    assert provider.pending_interaction(account, session) is None

    provider._refresh_ok[account.key] = False
    provider._reproject_runtime_sessions(account, state, client)
    assert session.capabilities == frozenset({Capability.TRANSCRIPT})
    assert provider.pending_interaction(account, session) is None

    provider._refresh_ok[account.key] = True
    provider._reproject_runtime_sessions(account, state, client)
    assert Capability.INJECT in session.capabilities
    assert Capability.STEER in session.capabilities
    assert Capability.INTERRUPT in session.capabilities
    assert Capability.INTERACT in session.capabilities
    assert Capability.PERMISSION_PROMPTS in session.capabilities
    assert provider.pending_interaction(account, session) == interaction


async def test_runtime_projection_is_withdrawn_when_ownership_disappears(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.refresh = AsyncMock()
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    client.interaction.return_value = None
    provider._clients[account.key] = client
    (session,) = await provider.scan_sessions(account)
    assert session.kind == "appServer"

    client.owns.return_value = False
    session.question = "stale runtime question"
    state = AppState()
    state.sessions = {session.key: session}
    provider._runtime_changed(account, state, sid)

    assert session.kind is None
    assert session.question is None
    assert Capability.INJECT not in session.capabilities
    assert Capability.STEER not in session.capabilities
    assert Capability.INTERRUPT not in session.capabilities

    session.kind = "appServer"
    session.capabilities = frozenset({Capability.TRANSCRIPT, Capability.INJECT})
    provider.sweep_liveness(account, [session], state)
    assert session.kind is None
    assert session.capabilities == frozenset({Capability.TRANSCRIPT})


def test_transcriptless_runtime_projection_is_withdrawn_during_sweep(tmp_path):
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.owns.return_value = False
    provider._clients[account.key] = client
    session = Session(
        key=f"{account.key}:thread-1",
        account_key=account.key,
        session_id="thread-1",
        status=SessionStatus.LIVE,
        thinking=True,
        activity="Working",
        question="stale runtime question",
        kind="appServer",
        capabilities=frozenset(
            {
                Capability.TRANSCRIPT,
                Capability.INJECT,
                Capability.STEER,
                Capability.INTERRUPT,
            }
        ),
    )

    changed = provider.sweep_liveness(account, [session], AppState())

    assert changed == [session]
    assert session.status is SessionStatus.IDLE
    assert session.thinking is False
    assert session.activity is None
    assert session.question is None
    assert session.kind is None
    assert session.capabilities == frozenset()


async def test_codex_incremental_tail_and_truncation_recovery(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    path = _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    (session,) = await provider.scan_sessions(account)
    offset, seq = await provider.transcript_cursor(account, session)

    with path.open("a") as handle:
        handle.write(json.dumps(_message("assistant", "A live update")) + "\n")
    events, new_offset, new_seq = await provider.tail_transcript(account, session, offset, seq)
    assert [event.text for event in events] == ["A live update"]
    assert new_offset == path.stat().st_size
    assert new_seq == seq + 1

    path.write_text(json.dumps(_message("user", "After rotation")) + "\n")
    events, reset_offset, reset_seq = await provider.tail_transcript(
        account, session, new_offset, new_seq
    )
    assert [event.text for event in events] == ["After rotation"]
    assert reset_offset == path.stat().st_size
    assert reset_seq == 1


async def test_codex_full_reads_run_in_thread(tmp_path, monkeypatch):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    path = _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    (session,) = await provider.scan_sessions(account)
    direct_detail = provider._load_transcript_file(path, None)
    calls: list[str] = []
    original_to_thread = provider_mod.asyncio.to_thread

    async def tracking_to_thread(func, /, *args, **kwargs):
        calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(provider_mod.asyncio, "to_thread", tracking_to_thread)
    assert await provider.transcript_cursor(account, session) == transcripts.transcript_cursor(path)
    direct_events = [event for event in transcripts.read_events(path).events if event.seq > 8]
    provider_events = await provider.read_transcript(account, session, after_seq=8)
    assert provider_events == provider._apply_model(direct_events, "gpt-5.6-sol")
    assert await provider.load_transcript(account, session) == direct_detail
    assert calls == ["transcript_cursor", "_read_transcript_file", "_load_transcript_file"]


def test_codex_usage_poller_and_registration(tmp_path):
    provider = CodexProvider()
    assert provider.make_usage_poller(_account(tmp_path), object(), object()) is not None
    assert PROVIDERS["codex"].provider_id == "codex"
    config = AccountConfig(provider="codex", label="local", config_dir=str(tmp_path))
    assert config.root == tmp_path


async def test_codex_compact_command_uses_owned_runtime(tmp_path):
    provider = CodexProvider()
    account = _account(tmp_path)
    session = Session(
        key=f"{account.key}:thread-1",
        account_key=account.key,
        session_id="thread-1",
        status=SessionStatus.IDLE,
    )
    client = MagicMock()
    client.compact = AsyncMock(return_value=InjectResult(True))
    client.queue_turn = AsyncMock()
    client.owns.return_value = True
    provider._clients[account.key] = client

    result = await provider.inject(
        account,
        session,
        "/compact",
        timeout_s=30,
    )

    assert result.accepted
    assert not result.transcript_expected
    client.compact.assert_awaited_once_with("thread-1")
    client.queue_turn.assert_not_awaited()




async def test_codex_compact_rejects_args_images_and_unowned_sessions(tmp_path):
    provider = CodexProvider()
    account = _account(tmp_path)
    session = Session(
        key=f"{account.key}:thread-1",
        account_key=account.key,
        session_id="thread-1",
        status=SessionStatus.IDLE,
    )
    client = MagicMock()
    client.compact = AsyncMock()
    client.owns.return_value = True
    provider._clients[account.key] = client

    with_args = await provider.inject(
        account, session, "/compact now", timeout_s=30
    )
    with_image = await provider.inject(
        account,
        session,
        "/compact",
        timeout_s=30,
        images=[tmp_path / "screen.png"],
    )
    client.owns.return_value = False
    unowned = await provider.inject(account, session, "/compact", timeout_s=30)

    assert with_args.reason == "usage: /compact"
    assert with_image.reason == "/compact does not accept image attachments"
    assert "AgentDeck-owned" in unowned.reason
    client.compact.assert_not_awaited()


async def test_codex_owned_inject_maps_action_id_conflict(tmp_path):
    provider = CodexProvider()
    account = _account(tmp_path)
    session = Session(
        key=f"{account.key}:thread-1",
        account_key=account.key,
        session_id="thread-1",
        status=SessionStatus.IDLE,
    )
    client = MagicMock()
    client.owns.return_value = True
    client.queue_turn = AsyncMock(
        return_value=InjectResult(False, "delivery_id_conflict")
    )
    provider._clients[account.key] = client

    result = await provider.inject(account, session, "Next", timeout_s=30)

    assert result == InjectResult(False, "client_action_conflict")


def _worker_rollout(
    root: Path, sid: str, prompt: str, *, preamble: int = 0, trailing: int = 0
) -> Path:
    """A spawned worker's rollout: its first user message is the dispatch brief.

    ``preamble`` and ``trailing`` pad the bytes either side of that brief, so a
    test can put it where a real long-running worker's brief ends up: past the
    head window and out of reach of the tail one.
    """
    directory = root / "sessions" / "2026" / "07" / "13"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-07-13T10-00-00-{sid}.jsonl"
    lines = [
        _line(
            "session_meta",
            {
                "session_id": sid,
                "id": sid,
                "timestamp": "2026-07-13T10:00:00Z",
                "cwd": "/tmp/codex-project/.worktrees/issue-290",
                "originator": "codex-tui",
                "cli_version": "0.144.3",
                "source": "cli",
                "thread_source": "user",
                "model_provider": "openai",
                "history_mode": "save-all",
            },
        ),
        _line("turn_context", {"model": "gpt-5.6-sol"}),
        *([_message("user", "<recommended_plugins>\n" + "p" * preamble)] if preamble else []),
        _message("user", prompt),
        *(
            [_line("response_item", {"type": "function_call_output", "output": "o" * trailing})]
            if trailing
            else []
        ),
        _message("assistant", "On it", phase="commentary"),
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in lines))
    return path


def _offline_titles(tmp_path: Path) -> kanban.KanbanTitleCache:
    """A title cache that cannot reach GitHub — no `gh`, no network, no real issues.

    Without this the scan would resolve live issue titles through `gh` on the machine
    running the tests, which is both a network call and an assertion about whatever
    that issue happens to be called today.
    """
    cache = kanban.KanbanTitleCache(tmp_path / "titles.json")
    cache._gh = None
    return cache


_CODEX_WORKER_PROMPT = (
    "You are an autonomous **AgentDeck kanban worker** — a local Codex session. "
    "Implement exactly one\nGitHub issue, open a pull request, then stop. Nothing else.\n\n"
    "## Assignment\n"
    "- Repo: eerovil/agentdeck\n"
    "- Issue: #290 — Requested changes\n"
    "- Your worktree (your ONLY workspace, already your cwd):"
    " /tmp/codex-project/.worktrees/issue-290\n"
)


async def test_a_worker_card_is_named_after_its_issue_not_its_dispatch_prompt(tmp_path):
    """#310: every worker's brief is the same boilerplate, so every card read the same.

    Nothing writes a Codex session a title, so the deck has to name it from the issue the
    brief names. With `gh` unavailable in the test the resolved issue title cannot be
    fetched, and the bare reference is the documented fallback — which is the part that
    makes the cards distinguishable at all.
    """
    sid = "019f6085-5dbc-7f41-80b1-d32de9d80c15"
    _worker_rollout(tmp_path, sid, _CODEX_WORKER_PROMPT)

    provider = CodexProvider()
    provider._kanban = _offline_titles(tmp_path)
    (session,) = await provider.scan_sessions(_account(tmp_path))

    assert session.title == "agentdeck#290"
    assert session.display_title == "agentdeck#290"
    assert session.issue_url == "https://github.com/eerovil/agentdeck/issues/290"
    assert session.worker_type == "kanban"
    assert session.initial_prompt is not None
    assert session.initial_prompt.startswith("You are an autonomous")


async def test_a_worker_card_keeps_its_issue_link_once_the_chat_outgrows_the_windows(tmp_path):
    """#1421: the issue button is read out of the dispatch brief, and the brief moved.

    A Codex rollout now opens with well over the 128 KiB head window of base
    instructions, plugin catalogue and the deck's own session guide. While the chat
    was short the tail window still reached back over all of it; once the chat grew
    past head+tail the brief sat in the unread middle, and the card of every
    long-running worker silently lost the issue it was working.
    """
    sid = "019f6085-5dbc-7f41-80b1-d32de9d80c16"
    path = _worker_rollout(
        tmp_path,
        sid,
        "You are working eerovil/agentdeck#1421 for the AgentDeck poller.",
        preamble=200_000,
        trailing=400_000,
    )
    assert path.stat().st_size > 128 * 1024 + 256 * 1024  # the windows cannot cover it

    provider = CodexProvider()
    provider._kanban = _offline_titles(tmp_path)
    (session,) = await provider.scan_sessions(_account(tmp_path))

    assert session.issue_url == "https://github.com/eerovil/agentdeck/issues/1421"
    assert session.title == "agentdeck#1421"


async def test_the_pollers_own_brief_names_the_card_too(tmp_path):
    sid = "019f6085-5dbc-7f41-80b1-d32de9d80c16"
    _worker_rollout(
        tmp_path,
        sid,
        "You are working eerovil/agentdeck#301 for the AgentDeck poller.\n\n"
        "Your worktree — your only workspace, already your cwd: /tmp/codex-project\n",
    )

    provider = CodexProvider()
    provider._kanban = _offline_titles(tmp_path)
    (session,) = await provider.scan_sessions(_account(tmp_path))

    assert session.title == "agentdeck#301"
    assert session.worker_type == "kanban"


async def test_an_ordinary_chat_keeps_its_prompt_derived_title(tmp_path):
    """Only a worker brief is overridden; a human's chat is titled by what they said."""
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c07"
    _rollout(tmp_path, sid)

    (session,) = await CodexProvider().scan_sessions(_account(tmp_path))

    assert session.title == "Build the parser safely"
    assert session.worker_type == "you"
    assert session.issue_url is None


async def test_a_resolved_issue_title_is_what_the_card_shows(tmp_path):
    sid = "019f6085-5dbc-7f41-80b1-d32de9d80c17"
    _worker_rollout(tmp_path, sid, _CODEX_WORKER_PROMPT)
    provider = CodexProvider()
    cache = kanban.KanbanTitleCache(tmp_path / "titles.json")
    cache._gh = "/usr/bin/gh"  # pretend gh exists

    async def fake_fetch(ref):
        return (ref, {"title": "Requested changes", "state": "open", "is_pr": False})

    cache._fetch = fake_fetch  # type: ignore[method-assign]
    provider._kanban = cache

    (session,) = await provider.scan_sessions(_account(tmp_path))

    assert session.title == "agentdeck#290 · Requested changes"
    assert session.issue_status == "open"
    assert session.issue_status_kind == "open"


def test_codex_transcript_meta_finds_a_prompt_buried_under_a_huge_turn(tmp_path):
    # #470: a Codex turn's tool output routinely exceeds the meta tail window, so
    # the newest prompt landed in the unread middle and the card kept showing the
    # opening dispatch prompt while the chat had moved on.
    path = tmp_path / "rollout.jsonl"
    filler = [
        _line("response_item", {"type": "function_call_output", "output": "o" * 8000})
        for _ in range(60)
    ]
    path.write_text(
        "".join(
            json.dumps(item) + "\n"
            for item in [
                _message("user", "You are working eerovil/agentdeck#470"),
                *filler,
                _message("user", "ignore antigravity, just push our changes."),
                *filler,
                _message("assistant", "pushed"),
            ]
        )
    )
    assert path.stat().st_size > 128 * 1024 + 256 * 1024  # windows cannot cover it

    meta = transcripts.transcript_meta(path)
    assert meta.last_prompt == "ignore antigravity, just push our changes."
    assert meta.first_prompt == "You are working eerovil/agentdeck#470"
    assert meta.last_role == "agent"  # the reply is still newer than the prompt


def test_codex_transcript_meta_finds_the_opening_prompt_behind_a_huge_preamble(tmp_path):
    # #1421: the mirror of #470 at the other end of the file. A Codex rollout opens
    # with more preamble than the head window holds, so once the chat is longer than
    # head+tail the opening prompt is in the unread middle — and it is the opening
    # prompt that names a worker's issue and titles its card.
    path = tmp_path / "rollout.jsonl"
    filler = [
        _line("response_item", {"type": "function_call_output", "output": "o" * 8000})
        for _ in range(60)
    ]
    path.write_text(
        "".join(
            json.dumps(item) + "\n"
            for item in [
                _message("user", "<recommended_plugins>\n" + "p" * 200_000),
                _message("user", "You are working eerovil/agentdeck#1421"),
                *filler,
                _message("user", "carry on"),
                *filler,
                _message("assistant", "pushed"),
            ]
        )
    )
    assert path.stat().st_size > 128 * 1024 + 256 * 1024  # windows cannot cover it

    meta = transcripts.transcript_meta(path)
    assert meta.first_prompt == "You are working eerovil/agentdeck#1421"
    assert meta.title == "You are working eerovil/agentdeck#1421"
    assert meta.last_prompt == "carry on"


def test_codex_opening_prompt_cache_rejects_a_replaced_or_truncated_rollout(tmp_path):
    path = tmp_path / "rollout.jsonl"

    def content(issue: int) -> str:
        return "".join(
            json.dumps(item) + "\n"
            for item in [
                _message("user", "<recommended_plugins>\n" + "p" * 200_000),
                _message("user", f"You are working owner/repo#{issue}"),
                _line(
                    "response_item",
                    {"type": "function_call_output", "output": "o" * 400_000},
                ),
            ]
        )

    path.write_text(content(111))
    assert (
        transcripts.transcript_meta(path).first_prompt
        == "You are working owner/repo#111"
    )

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(content(222))
    replacement.replace(path)
    assert (
        transcripts.transcript_meta(path).first_prompt
        == "You are working owner/repo#222"
    )

    previous = path.stat()
    path.write_text(content(333))  # same inode and length, different prompt
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1))
    assert (
        transcripts.transcript_meta(path).first_prompt
        == "You are working owner/repo#333"
    )

    # Normal rollouts only append, but recovery or external cleanup can truncate
    # one in place. The old issue must not survive that size reversal.
    path.write_text(json.dumps(_line("session_meta", {"session_id": "new"})) + "\n")
    assert transcripts.transcript_meta(path).first_prompt is None


def test_codex_opening_prompt_cache_does_not_remember_an_exhausted_scan(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        json.dumps(
            _message("user", "<recommended_plugins>\n" + "p" * FORWARD_SCAN_LIMIT)
        )
        + "\n"
        + json.dumps(_message("user", "You are working owner/repo#1421"))
        + "\n"
    )

    assert transcripts.transcript_meta(path).first_prompt is None
    assert str(path) not in transcripts._first_prompts


def test_codex_opening_prompt_scan_retries_a_mid_read_replacement(
    tmp_path, monkeypatch
):
    path = tmp_path / "rollout.jsonl"

    def content(issue: int) -> str:
        return "".join(
            json.dumps(item) + "\n"
            for item in [
                _message("user", "<recommended_plugins>\n" + "p" * 200_000),
                _message("user", f"You are working owner/repo#{issue}"),
            ]
        )

    path.write_text(content(111))
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(content(222))
    original = transcripts.objects_forwards
    replaced = False

    def replace_while_open(*args, **kwargs):
        nonlocal replaced
        iterator = original(*args, **kwargs)
        first = next(iterator)
        if not replaced:
            replacement.replace(path)
            replaced = True
        yield first
        yield from iterator

    monkeypatch.setattr(transcripts, "objects_forwards", replace_while_open)

    assert (
        transcripts.transcript_meta(path).first_prompt
        == "You are working owner/repo#222"
    )
    assert transcripts._first_prompts[str(path)].text.endswith("#222")


def test_codex_transcript_meta_finds_a_reply_buried_under_a_huge_turn(tmp_path):
    # #558: the same window blindness on the reply side — the newest assistant
    # message fell in the unread middle behind one turn's tool output, so the card
    # showed an assistant message many turns old as the agent's latest.
    path = tmp_path / "rollout.jsonl"
    filler = [
        _line("response_item", {"type": "function_call_output", "output": "o" * 8000})
        for _ in range(60)
    ]
    path.write_text(
        "".join(
            json.dumps(item) + "\n"
            for item in [
                _message("user", "open the PR"),
                _message("assistant", "starting on it"),
                *filler,
                _message("assistant", "the PR is open"),
                *filler,
            ]
        )
    )
    blob, head, tail = path.read_bytes(), 128 * 1024, 256 * 1024
    assert head < blob.index(b"the PR is open") < len(blob) - tail  # in the unread middle
    assert blob.index(b"starting on it") < head  # the stale reply the head does see

    meta = transcripts.transcript_meta(path)
    assert meta.last_text == "the PR is open"  # not the head's opening reply
    assert meta.last_role == "agent"
    assert meta.last_prompt == "open the PR"


def test_codex_transcript_meta_skips_noise_envelopes_when_scanning_backwards(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "".join(
            json.dumps(item) + "\n"
            for item in [
                _message("user", "the real question"),
                _message("user", "<environment_context>\ncwd=/tmp\n</environment_context>"),
            ]
        )
    )
    assert transcripts.transcript_meta(path).last_prompt == "the real question"


def test_codex_transcript_meta_tracks_the_latest_real_message_timestamp(tmp_path):
    path = tmp_path / "rollout.jsonl"
    prompt = _message("user", "please inspect this")
    prompt["timestamp"] = "2026-08-13T09:00:00Z"
    reply = _message("assistant", "working on it")
    reply["timestamp"] = "2026-08-13T09:01:00Z"
    progress = _line(
        "response_item",
        {"type": "function_call_output", "output": "later tool activity"},
        timestamp="2026-08-13T09:02:00Z",
    )
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in [prompt, reply, progress])
    )

    meta = transcripts.transcript_meta(path)

    assert meta.last_role == "agent"
    assert meta.last_message_at == datetime(2026, 8, 13, 9, 1, tzinfo=UTC)


def test_codex_guide_refresh_prefix_is_not_shown_as_the_user_turn(tmp_path):
    """A refreshed turn reaches Codex as guide + message, and the rollout is the
    only record of it. Every reader — transcript, title, last prompt — must see
    the operator's own words, not 13k characters of instructions."""
    guide = agentdeck_session_instructions(provider="codex", role="work")
    path = tmp_path / "rollout.jsonl"
    lines = [
        _message("user", wrap_session_guide(guide, "rebase onto master")),
        _message("assistant", "on it"),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    events = transcripts.recent_conversation(path, limit=4, tail=100_000)

    assert [(event.role, event.text) for event in events] == [
        ("user", "rebase onto master"),
        ("assistant", "on it"),
    ]
    meta = transcripts.transcript_meta(path)
    assert meta.title == "rebase onto master"
    assert "Communicate concisely" not in (meta.title or "")


def test_a_runtime_404_on_refresh_warns_once_with_the_runtimes_explanation(
    tmp_path, caplog
):
    # #788: the rename split-brain. The runtime is up but does not know this
    # label; a DEBUG-only line left the web process 404-looping invisibly.
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.owns.return_value = False
    provider._clients[account.key] = client
    state = AppState()
    response = httpx.Response(
        404,
        text="unknown Codex account 'main'; this runtime knows ['cursor']",
        request=httpx.Request("GET", "http://runtime/accounts/main/state"),
    )
    exc = httpx.HTTPStatusError("404", request=response.request, response=response)

    with caplog.at_level(logging.WARNING, logger="agentdeck.providers.codex.provider"):
        provider._refresh_failed(account, state, client, exc)
        provider._refresh_failed(account, state, client, exc)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # edge-triggered, not once per 1 Hz poll
    assert "runtime knows ['cursor']" in warnings[0].getMessage()

    # A plain transport error stays quiet at WARNING level.
    caplog.clear()
    provider._refresh_ok[account.key] = True
    with caplog.at_level(logging.WARNING, logger="agentdeck.providers.codex.provider"):
        provider._refresh_failed(account, state, client, OSError("down"))
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_codex_permission_prompt_change_is_scoped_to_the_next_turn(tmp_path):
    # A Codex turn already in flight keeps the reviewer it started with, so the
    # checkbox must say so; Claude switches in place and says nothing.
    provider = CodexProvider()
    account = _account(tmp_path)
    session = Session(
        key="codex:codex:t-1",
        account_key="codex:codex",
        session_id="t-1",
        status=SessionStatus.LIVE,
    )

    assert provider.permission_prompts_next_turn(account, session) is True

    session.status = SessionStatus.IDLE
    assert provider.permission_prompts_next_turn(account, session) is False


async def test_scan_reuses_a_previous_workers_history_parse(tmp_path, monkeypatch):
    """A replacement web worker must inherit the per-rollout answers on disk.

    A scan asks three questions of *every* rollout the account ever wrote, not just the listed
    ones: is this a helper, and which sessions did it delegate. Those files are immutable, but
    the answers used to live in process-local memos, so each worker handoff re-parsed the whole
    history. On the live deck that was 3,482 files and 553k JSONL lines — long enough that no
    replacement worker could signal readiness inside the supervisor's 90s gate, and deploys
    stopped landing (#1335).
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    root = tmp_path / "codex"
    parent_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    _rollout(root, parent_sid)
    _guardian(root, "019f5b22-ec93-7e52-9a15-d0cdb5c0e8c3", parent_sid)
    _spawned(root, "019f5b22-ec93-7e52-9a15-d0cdb5c0e8c4", parent_sid)

    calls: dict[str, int] = {}

    def counted(name):
        original = getattr(transcripts, name)

        def wrapper(path):
            calls[name] = calls.get(name, 0) + 1
            return original(path)

        return wrapper

    for name in ("transcript_meta", "delegated_session_keys"):
        monkeypatch.setattr(transcripts, name, counted(name))

    account = _account(root)
    first = await CodexProvider().scan_sessions(account)
    assert calls, "the first scan should have parsed the history"
    first_calls = dict(calls)
    calls.clear()

    # A brand-new provider is a brand-new worker: nothing in memory, but the
    # shared records the previous one published are still on disk.
    second = await CodexProvider().scan_sessions(account)

    assert calls == {}, f"a replacement worker re-parsed history: {calls}"
    assert first_calls
    assert [session.key for session in second] == [session.key for session in first]
    assert [session.title for session in second] == [session.title for session in first]


async def test_history_walk_never_asks_for_a_whole_transcript_record(
    tmp_path, monkeypatch
):
    """The whole-corpus passes must read the role projection, not the record.

    ``MAX_SESSIONS`` bounds the chats a scan reports, but two of its loops —
    the owned-thread rescue and the delegation map — read every rollout the account ever
    wrote. Those used to ask
    ``_cached_meta``, and a ``TranscriptMeta`` carries the text a session
    displays: on the live host 4,195 of them came to ~60 MB against a 64 MB
    cap, so the walk evicted its own working set and every scan paid the disk
    tier again — 12.2 s of metadata per warm scan (#1489).

    Sizing the cache up would only move the wall. The claim pinned here is
    structural: what a scan asks the *full-record* cache for is bounded by the
    sessions it reports, so history costs a stat and a small memo however much
    of it there is.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    root = tmp_path / "codex"
    parent_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    listed = _rollout(root, parent_sid)
    guardians = [
        _guardian(root, f"019f5b22-ec93-7e52-9a15-d0cdb5c0e8{index:02x}", parent_sid)
        for index in range(24)
    ]
    # History, not the working set: the guardians sit outside the display cap,
    # which is exactly the population the walk is supposed to stop rehydrating.
    monkeypatch.setattr(provider_mod, "MAX_SESSIONS", 1)
    for index, path in enumerate(guardians):
        os.utime(path, (1_760_000_000 + index, 1_760_000_000 + index))
    os.utime(listed, (1_760_001_000, 1_760_001_000))

    account = _account(root)
    provider = CodexProvider()
    await provider.scan_sessions(account)  # publish the shared records

    asked: list[Path] = []
    original_get = provider._meta_cache.get

    def watched(path, *args, **kwargs):
        asked.append(path)
        return original_get(path, *args, **kwargs)

    monkeypatch.setattr(provider._meta_cache, "get", watched)
    (_parent,) = await provider.scan_sessions(account)

    assert set(asked) <= {listed}, (
        "a whole-corpus pass asked for full transcript records: "
        f"{sorted(str(path) for path in set(asked) - {listed})}"
    )


def _owning_client(owned: set[str]) -> MagicMock:
    client = MagicMock()
    client.refresh = AsyncMock()
    client.owned_thread_ids.return_value = frozenset(owned)
    client.owns.side_effect = lambda thread_id: thread_id in owned
    client.active_turn.return_value = None
    client.interaction.return_value = None
    return client


async def test_owned_rescue_keeps_old_helpers_out_of_the_full_record_cache(tmp_path, monkeypatch):
    """An owned chat outside the display cap brings back its own rollout only.

    Its guardians and spawned agents share its session_id, so rescuing by
    session_id alone pulled them into the full-record cache too. On the live
    host that was ~3,200 records, ~68 MB against a 64 MiB cap: each scan
    evicted what it had just parsed and re-read every one (#2652).
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    root = tmp_path / "codex"
    owned_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c07"
    newest_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c08"
    owned = _rollout(root, owned_sid)
    newest = _rollout(root, newest_sid)
    helpers = [
        _guardian(root, f"019f5b22-ec93-7e52-9a15-d0cdb5c0e8{index:02x}", owned_sid)
        for index in range(3)
    ] + [
        _spawned(root, f"019f5b22-ec93-7e52-9a15-d0cdb5c0e9{index:02x}", owned_sid)
        for index in range(3)
    ]
    monkeypatch.setattr(provider_mod, "MAX_SESSIONS", 1)
    for index, path in enumerate([owned, *helpers]):
        os.utime(path, (1_760_000_000 + index, 1_760_000_000 + index))
    os.utime(newest, (1_760_001_000, 1_760_001_000))

    account = _account(root)
    provider = CodexProvider()
    provider._clients[account.key] = _owning_client({owned_sid})
    first = await provider.scan_sessions(account)

    asked: list[Path] = []
    original_get = provider._meta_cache.get

    def watched(path, *args, **kwargs):
        asked.append(path)
        return original_get(path, *args, **kwargs)

    parses: list[Path] = []
    real_meta = transcripts.transcript_meta

    def counted(path, *args, **kwargs):
        parses.append(path)
        return real_meta(path, *args, **kwargs)

    monkeypatch.setattr(provider._meta_cache, "get", watched)
    monkeypatch.setattr(transcripts, "transcript_meta", counted)
    second = await provider.scan_sessions(account)

    assert set(asked) <= {owned, newest}, sorted(str(path) for path in set(asked) - {owned, newest})
    assert parses == [], f"a warm scan re-parsed transcripts: {parses}"
    found = {session.session_id: session for session in second}
    assert set(found) == {owned_sid, newest_sid}
    assert [session.key for session in second] == [session.key for session in first]


async def test_metadata_cache_warns_once_when_a_scan_nears_its_cap(tmp_path, monkeypatch, caplog):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    root = tmp_path / "codex"
    _rollout(root, "019f5b2b-c830-7922-a1ce-8c9c69526c09")
    account = _account(root)

    quiet = CodexProvider()
    with caplog.at_level(logging.WARNING, logger=provider_mod.__name__):
        await quiet.scan_sessions(account)
    assert "metadata cache" not in caplog.text

    monkeypatch.setattr(provider_mod, "_META_CACHE_BYTES", 64)
    provider = CodexProvider()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=provider_mod.__name__):
        await provider.scan_sessions(account)
        await provider.scan_sessions(account)
    warnings = [record for record in caplog.records if "metadata cache" in record.getMessage()]
    assert len(warnings) == 1


def test_the_role_projection_cannot_drift_from_the_full_record(tmp_path):
    """``_CorpusMeta`` answers exactly what ``TranscriptMeta`` would have.

    The projection is what decides whether a rollout is a chat, a guardian or a
    spawned agent for every historical file, so a field that stopped agreeing
    would misfile a real chat rather than merely slow a scan down.
    """
    parent_sid = "019fd2d7-3d1d-72d1-9aaf-1d8deb84d74a"
    paths = [
        _rollout(tmp_path, parent_sid),
        _guardian(tmp_path, "019fd30a-4571-7393-aef5-62189d043f92", parent_sid),
        _spawned(tmp_path, "019fd30a-4571-7393-aef5-62189d043f93", parent_sid),
    ]
    provider = CodexProvider()

    for path in paths:
        full = transcripts.transcript_meta(path)
        assert provider._corpus_meta(path) == provider_mod._CorpusMeta.of(full)
        assert provider_mod._is_helper_rollout(
            provider._corpus_meta(path)
        ) == provider_mod._is_helper_rollout(full)


async def test_known_isolated_rollout_change_skips_codex_account_walk(
    tmp_path, monkeypatch
):
    sid = "019fd2d7-3d1d-72d1-9aaf-1d8deb84d700"
    path = _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    state = AppState()
    state.replace_account_sessions(account.key, await provider.scan_sessions(account))
    loop_thread = threading.current_thread()
    offenders: list[str] = []

    def watched(name, real):
        def wrapper(path, *args, **kwargs):
            if threading.current_thread() is loop_thread:
                offenders.append(name)
            return real(path, *args, **kwargs)

        return wrapper

    for name in (
        "transcript_meta",
        "last_event",
        "delegated_session_keys",
    ):
        monkeypatch.setattr(transcripts, name, watched(name, getattr(transcripts, name)))
    with path.open("a") as handle:
        handle.write(json.dumps(_message("assistant", "Incremental reply.")) + "\n")
    monkeypatch.setattr(
        provider_mod,
        "_all_rollouts",
        lambda _root: pytest.fail("incremental scan walked all rollouts"),
    )
    monkeypatch.setattr(
        provider_mod,
        "_list_rollouts",
        lambda _root: pytest.fail("incremental scan ranked rollouts"),
    )

    changed = await provider.scan_changed_sessions(
        account, frozenset({(Change.modified, path)}), state
    )

    assert changed is not None
    assert [session.session_id for session in changed] == [sid]
    assert changed[0].last_text == "Incremental reply."
    assert offenders == []


async def test_restart_full_scan_reads_same_mtime_append_without_watcher_refresh(
    tmp_path, monkeypatch
):
    """The same-tick append a coarse-timestamp filesystem produces (#1368).

    Its mtime is the one the previous parse already cached, so a memo keyed on
    mtime alone hands back the text from before the append and the deck shows a
    chat's previous message as its latest. `os.utime` reproduces deterministically
    what a dev-cluster pod's overlay filesystem did to this suite.
    """
    sid = "019fd2d7-3d1d-72d1-9aaf-1d8deb84d703"
    path = _rollout(tmp_path, sid)
    account = _account(tmp_path)
    cache_dir = tmp_path / "shared-cache"
    monkeypatch.setattr(filecache_mod, "_shared_cache_dir", lambda: cache_dir)
    provider = CodexProvider()
    before_scan = await provider.scan_sessions(account)
    assert before_scan[0].last_text != "Same-tick reply."
    assert provider._meta_cache._cache_dir is not None
    assert provider._meta_cache._record_path(path).is_file()
    before = path.stat()
    with path.open("a") as handle:
        handle.write(json.dumps(_message("assistant", "Same-tick reply.")) + "\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    # A fresh provider is the web-process restart case: its full scan must
    # reject the stale value in the shared cache without a watcher event first.
    rescanned = await CodexProvider().scan_sessions(account)
    assert rescanned[0].last_text == "Same-tick reply."


async def test_a_coalesced_codex_batch_refreshes_every_known_rollout(
    tmp_path, monkeypatch
):
    """Issue #1487: this path used to demand a batch of exactly one change. The
    watcher debounces for 500ms and the scan loop coalesces a whole interval, so
    with more than one chat writing a batch is never a single change — Codex fell
    back to a full scan on every wake. Size is not what makes a batch unsafe."""
    first_sid = "019fd2d7-3d1d-72d1-9aaf-1d8deb84d710"
    second_sid = "019fd2d7-3d1d-72d1-9aaf-1d8deb84d711"
    first = _rollout(tmp_path, first_sid)
    second = _rollout(tmp_path, second_sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    state = AppState()
    state.replace_account_sessions(account.key, await provider.scan_sessions(account))
    with first.open("a") as handle:
        handle.write(json.dumps(_message("assistant", "First reply.")) + "\n")
    with second.open("a") as handle:
        handle.write(json.dumps(_message("assistant", "Second reply.")) + "\n")
    monkeypatch.setattr(
        provider_mod,
        "_all_rollouts",
        lambda _root: pytest.fail("a coalesced batch walked all rollouts"),
    )

    changed = await provider.scan_changed_sessions(
        account,
        frozenset({(Change.modified, first), (Change.modified, second)}),
        state,
    )

    assert changed is not None
    assert {session.session_id for session in changed} == {first_sid, second_sid}
    assert {session.last_text for session in changed} == {
        "First reply.",
        "Second reply.",
    }
def test_codex_last_event_cache_is_bounded():
    provider = CodexProvider()

    assert provider._last_ev_cache._max_entries == provider_mod._HISTORY_CACHE_ENTRIES


async def test_a_full_codex_scan_parses_no_rollout_on_the_event_loop(
    tmp_path, monkeypatch
):
    """Issue #1487: listing the
    rollout tree and parsing each rollout's metadata, delegation markers and last
    event all ran inline, which is what left the deck answering nothing while a
    scan was in flight. Nothing here asserts speed — only where the parsing runs."""
    monkeypatch.setattr(provider_mod, "_HISTORY_CACHE_ENTRIES", 2)
    for index in range(3):
        _rollout(tmp_path, f"019fd2d7-3d1d-72d1-9aaf-1d8deb84d72{index}")
    loop_thread = threading.current_thread()
    offenders: list[str] = []

    def watched(name, real):
        def wrapper(path, *args, **kwargs):
            if threading.current_thread() is loop_thread:
                offenders.append(name)
            return real(path, *args, **kwargs)

        return wrapper

    for name in (
        "transcript_meta",
        "last_event",
        "delegated_session_keys",
    ):
        monkeypatch.setattr(transcripts, name, watched(name, getattr(transcripts, name)))

    sessions = await CodexProvider().scan_sessions(_account(tmp_path))

    assert len(sessions) == 3
    assert offenders == []


async def test_unknown_codex_rollout_requires_a_full_scan(tmp_path):
    known_sid = "019fd2d7-3d1d-72d1-9aaf-1d8deb84d701"
    _rollout(tmp_path, known_sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    state = AppState()
    state.replace_account_sessions(account.key, await provider.scan_sessions(account))
    unknown = _rollout(tmp_path, "019fd2d7-3d1d-72d1-9aaf-1d8deb84d702")

    assert (
        await provider.scan_changed_sessions(
            account, frozenset({(Change.added, unknown)}), state
        )
        is None
    )


@pytest.mark.parametrize("runtime_can", [False, True])
def test_an_owned_codex_thread_resumes_elsewhere_only_when_the_runtime_can(
    tmp_path, runtime_can
):
    """#2524: the deck lets a send into a gone directory through only on the runtime's word."""
    from agentdeck.models import SNAPSHOT_RELOCATE_V1, Account, Capability

    account = Account("codex:test", "codex", "test", tmp_path)
    client = MagicMock()
    client.owns.return_value = True
    client.active_turn.return_value = None
    client.interaction.return_value = None
    client.worker_key.return_value = None
    client.permission_prompts_available.return_value = False
    client.supports.side_effect = lambda capability: (
        runtime_can and capability == SNAPSHOT_RELOCATE_V1
    )
    provider = provider_mod.CodexProvider()
    provider._clients[account.key] = client

    capabilities = provider._runtime_capabilities(account, "thread-1")

    assert (Capability.RESUME_ELSEWHERE in capabilities) is runtime_can


async def test_the_live_window_stops_rescuing_old_owned_chats(tmp_path, monkeypatch):
    """#2841: an owned chat older than the window is no longer listed beyond the newest
    cap, unless it was just opened or is in a Work still going; an older chat someone
    opened is listed even when this deck does not own it."""
    from agentdeck import live_window

    sids = {
        "new": "019f6085-5dbc-7f41-80b1-d32de9d80c14",
        "old_owned": "019f6073-17bd-7251-9db1-383bfe24c143",
        "recent_owned": "019f6074-17bd-7251-9db1-383bfe24c144",
        "pinned_owned": "019f6075-17bd-7251-9db1-383bfe24c145",
        "opened_unowned": "019f6076-17bd-7251-9db1-383bfe24c146",
    }
    paths = {name: _rollout(tmp_path, sid) for name, sid in sids.items()}
    ten_days = time.time() - 10 * 86400
    for name in ("old_owned", "pinned_owned", "opened_unowned"):
        os.utime(paths[name], (ten_days, ten_days))
    one_day = time.time() - 86400
    os.utime(paths["recent_owned"], (one_day, one_day))
    monkeypatch.setattr(provider_mod, "MAX_SESSIONS", 1)
    provider = CodexProvider()
    account = _account(tmp_path)
    owned = {sids["old_owned"], sids["recent_owned"], sids["pinned_owned"]}
    client = MagicMock()
    client.refresh = AsyncMock()
    client.owned_thread_ids.return_value = frozenset(owned)
    client.owns.side_effect = lambda thread_id: thread_id in owned
    client.active_turn.return_value = None
    client.interaction.return_value = None
    provider._clients[account.key] = client
    window = live_window.LiveWindow(7)
    window.set_works(pinned={f"{account.key}:{sids['pinned_owned']}"}, finished=())
    window.request([f"{account.key}:{sids['opened_unowned']}"])

    token = live_window.CURRENT.set(window)
    try:
        sessions = await provider.scan_sessions(account)
    finally:
        live_window.CURRENT.reset(token)

    assert {s.session_id for s in sessions} == {
        sids["new"], sids["recent_owned"], sids["pinned_owned"], sids["opened_unowned"]
    }
    # Without a window the scan lists what it always listed.
    assert {s.session_id for s in await provider.scan_sessions(account)} == (
        {sids["new"]} | owned
    )


async def test_a_targeted_codex_scan_reads_only_the_named_chat(tmp_path):
    """#2841 review: opening one old chat reads its own rollout by name and leaves the
    account-wide records of the last full scan alone."""
    wanted = "019f6073-17bd-7251-9db1-383bfe24c143"
    other = "019f6085-5dbc-7f41-80b1-d32de9d80c14"
    _rollout(tmp_path, wanted)
    _rollout(tmp_path, other)
    provider = CodexProvider()
    account = _account(tmp_path)
    await provider.scan_sessions(account)
    billable = list(provider._billable_paths[account.key])

    def no_listing(*_args):
        raise AssertionError("a one-chat load does not list the account")

    import agentdeck.providers.codex.provider as codex_provider

    original = codex_provider._all_rollouts
    codex_provider._all_rollouts = no_listing
    try:
        sessions = await provider.scan_sessions(account, only=frozenset({wanted}))
    finally:
        codex_provider._all_rollouts = original

    assert [s.session_id for s in sessions] == [wanted]
    assert provider._billable_paths[account.key] == billable
    assert (str(account.root), other) in provider._paths, "the full scan's paths stay"
