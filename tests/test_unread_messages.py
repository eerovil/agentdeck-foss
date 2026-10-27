"""Messages queued behind a running turn survive a runtime death (#2898).

The Claude CLI holds a message written into a running turn in memory until the
turn gets to it, and a resumed CLI does not replay that queue. The worker host
therefore keeps what it wrote, and revival quotes back the ones the transcript
never shows read.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from worker_harness import claude_host as _host
from worker_harness import settle as _settle

from agentdeck.providers.claude_code.transcripts import read_message_texts
from agentdeck.revival import BACKGROUND, CUT_OFF, revival_message

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat().replace("+00:00", "Z")


def _transcript(tmp_path: Path, session_id: str, lines: list[dict]) -> Path:
    path = tmp_path / "cfg" / "projects" / "-work" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _user(text: str, at: float, **extra) -> dict:
    return {
        "type": "user",
        "timestamp": _iso(at),
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        **extra,
    }


def _queued_command(text: str, at: float) -> dict:
    return {
        "type": "attachment",
        "timestamp": _iso(at),
        "attachment": {"type": "queued_command", "prompt": text},
    }


def _enqueue(at: float) -> dict:
    # What the CLI writes for a message sent mid-turn: no text at all.
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": _iso(at)}


async def _steer_into_running_turn(tmp_path, *messages: str):
    """A chat mid-turn that receives ``messages``, then a runtime death."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    for number, message in enumerate(messages):
        result = await host.deliver("issue-1", message, delivery_id=f"m{number}", delegated=False)
        assert result.accepted and result.action == "steered"
    session_id = host.snapshot()["workers"]["issue-1"]["session_id"]
    await host.stop()
    return session_id


async def test_messages_written_into_a_running_turn_are_quoted_after_a_death(tmp_path):
    session_id = await _steer_into_running_turn(tmp_path, "first thought", "second thought")
    _transcript(tmp_path, session_id, [_user("work it", time.time() - 1), _enqueue(time.time())])

    fresh, _ = _host(tmp_path)
    unread = fresh.unread_messages("issue-1")

    assert [entry["text"] for entry in unread] == ["first thought", "second thought"]
    assert [entry["delivery_id"] for entry in unread] == ["m0", "m1"]
    await fresh.stop()


async def test_a_message_the_transcript_shows_read_is_not_quoted(tmp_path):
    """Read as its own turn, or delivered into the open turn: either way it landed."""
    session_id = await _steer_into_running_turn(tmp_path, "read as a turn", "read mid-turn", "lost")
    now = time.time()
    _transcript(
        tmp_path,
        session_id,
        [_user("read as a turn", now), _queued_command("read mid-turn", now)],
    )

    fresh, _ = _host(tmp_path)

    assert [entry["text"] for entry in fresh.unread_messages("issue-1")] == ["lost"]
    await fresh.stop()


async def test_a_message_written_to_an_idle_chat_is_not_kept(tmp_path):
    """An idle CLI takes a message at once; only a queued one is at risk."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    await host.deliver("issue-1", "next task", delegated=False)
    await host.stop()

    fresh, _ = _host(tmp_path)

    assert fresh.unread_messages("issue-1") == []
    await fresh.stop()


async def test_a_turn_that_ends_cleanly_forgets_its_queue(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    await host.deliver("issue-1", "while you work", delegated=False)
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()

    state = json.loads((tmp_path / "state" / "test.json").read_text())
    assert state["workers"][0]["unread_messages"] == []
    await host.stop()


async def test_a_teardown_result_keeps_the_queue(tmp_path):
    """The aborted result a dying CLI writes is not the turn reading its queue."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    await host.deliver("issue-1", "while you work", delegated=False)
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "error_during_execution"})
    await _settle()
    await host.stop()

    fresh, _ = _host(tmp_path)

    assert [entry["text"] for entry in fresh.unread_messages("issue-1")] == ["while you work"]
    await fresh.stop()


async def test_a_deliberate_stop_forgets_the_queue(tmp_path):
    """A person who stopped the turn ended what was queued behind it too; a later
    death must not bring it back."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    await host.deliver("issue-1", "while you work", delegated=False)
    await host.stop_worker("issue-1")
    await host.stop()

    fresh, _ = _host(tmp_path)

    assert fresh.unread_messages("issue-1") == []
    await fresh.stop()


async def test_a_live_worker_has_nothing_to_quote(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    await host.deliver("issue-1", "while you work", delegated=False)

    assert host.unread_messages("issue-1") == []
    await host.stop()


async def test_the_kept_queue_is_bounded(tmp_path):
    from agentdeck.providers.claude_code.worker import MAX_UNREAD_MESSAGES

    texts = [f"message {n}" for n in range(MAX_UNREAD_MESSAGES + 3)]
    await _steer_into_running_turn(tmp_path, *texts)

    fresh, _ = _host(tmp_path)

    assert [entry["text"] for entry in fresh.unread_messages("issue-1")] == texts[3:]
    # The three it let go are counted, so revival can say they were lost.
    assert fresh.unread_dropped("issue-1") == 3
    await fresh.stop()


async def test_the_message_is_on_disk_before_it_enters_the_child(tmp_path):
    """A death between the write and the save after it must not lose the text:
    the CLI's in-memory queue would otherwise be its only copy."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    state_path = tmp_path / "state" / "test.json"
    on_disk_at_write: list[list[str]] = []
    real_write = host._write_user_message

    async def observing_write(live, message, image_blocks, **_kwargs):
        worker = json.loads(state_path.read_text())["workers"][0]
        on_disk_at_write.append([entry["text"] for entry in worker["unread_messages"]])
        await real_write(live, message, image_blocks)

    host._write_user_message = observing_write
    await host.deliver("issue-1", "while you work", delivery_id="m0", delegated=False)

    assert on_disk_at_write == [["while you work"]]
    await host.stop()


async def test_a_write_that_reached_nothing_is_not_kept(tmp_path):
    """A failure before any byte reached the child undoes the entry, and the
    older message the bound pushed out for it comes back."""
    from agentdeck.providers.claude_code.worker import MAX_UNREAD_MESSAGES, WorkerError

    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    texts = [f"m{number}" for number in range(MAX_UNREAD_MESSAGES)]
    for text in texts:
        await host.deliver("issue-1", text, delegated=False)

    async def failing_write(live, message, image_blocks, **_kwargs):
        raise WorkerError("stdin closed", write_started=False)

    host._write_user_message = failing_write
    result = await host.deliver("issue-1", "never sent", delivery_id="x", delegated=False)
    assert not result.accepted
    await host.stop()

    fresh, _ = _host(tmp_path)
    assert [entry["text"] for entry in fresh.unread_messages("issue-1")] == texts
    assert fresh.unread_dropped("issue-1") == 0
    await fresh.stop()


async def test_a_write_that_may_have_landed_is_kept(tmp_path):
    from agentdeck.providers.claude_code.worker import WorkerError

    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()

    async def half_write(live, message, image_blocks, **_kwargs):
        raise WorkerError("drain failed", write_started=True)

    host._write_user_message = half_write
    await host.deliver("issue-1", "maybe sent", delivery_id="x", delegated=False)
    await host.stop()

    fresh, _ = _host(tmp_path)
    assert [entry["text"] for entry in fresh.unread_messages("issue-1")] == ["maybe sent"]
    await fresh.stop()


async def test_a_clean_result_resets_the_dropped_count(tmp_path):
    from agentdeck.providers.claude_code.worker import MAX_UNREAD_MESSAGES

    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    for number in range(MAX_UNREAD_MESSAGES + 1):
        await host.deliver("issue-1", f"m{number}", delegated=False)
    assert host.unread_dropped("issue-1") == 1
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()

    assert host.unread_dropped("issue-1") == 0
    await host.stop()


async def test_the_same_text_sent_twice_and_read_once_leaves_one_unread(tmp_path):
    session_id = await _steer_into_running_turn(tmp_path, "yes", "yes")
    _transcript(tmp_path, session_id, [_user("yes", time.time())])

    fresh, _ = _host(tmp_path)

    unread = fresh.unread_messages("issue-1")
    assert [(entry["text"], entry["delivery_id"]) for entry in unread] == [("yes", "m1")]
    await fresh.stop()


async def test_a_read_far_behind_a_long_turn_still_counts(tmp_path):
    """A turn may print megabytes after it reads a message; that read still counts."""
    session_id = await _steer_into_running_turn(tmp_path, "check the logs")
    now = time.time()
    filler = {"type": "assistant", "timestamp": _iso(now), "message": {"content": "x" * 65536}}
    _transcript(
        tmp_path,
        session_id,
        [_user("check the logs", now)] + [filler] * 160,  # about 10 MiB after the read
    )

    fresh, _ = _host(tmp_path)

    assert fresh.unread_messages("issue-1") == []
    await fresh.stop()


def test_read_message_texts_ignores_subagents_and_older_lines(tmp_path):
    now = time.time()
    path = _transcript(
        tmp_path,
        "sid",
        [
            _user("before the turn", now - 600),
            _user("a subagent prompt", now, isSidechain=True),
            _enqueue(now),
            _user("  read  ", now),
        ],
    )

    assert read_message_texts(path, since=now - 60) == {"read": 1}


def test_the_revival_message_quotes_the_lost_messages_oldest_first():
    message = revival_message(
        CUT_OFF,
        previous_exit_clean=True,
        unread=[
            {"text": "first", "at": time.time(), "images": 0},
            {"text": "second", "at": time.time(), "images": 2},
        ],
    )

    assert message.startswith("The AgentDeck runtime was restarted")
    assert "2 messages reached you" in message
    assert message.index("first") < message.index("second")
    assert "2 images, which could not be kept" in message


def test_the_revival_names_messages_it_could_not_keep():
    message = revival_message(
        CUT_OFF,
        previous_exit_clean=True,
        unread=[{"text": "newest", "at": time.time(), "images": 0}],
        dropped=3,
    )

    assert "Up to 3 older messages" in message
    assert "send those again" in message
    assert "newest" in message
    only_dropped = revival_message(CUT_OFF, previous_exit_clean=True, dropped=1)
    assert "Up to 1 older message sent" in only_dropped


def test_a_revival_with_nothing_lost_is_unchanged():
    assert revival_message(CUT_OFF, previous_exit_clean=True, unread=[]) == revival_message(
        CUT_OFF, previous_exit_clean=True
    )
    assert "reached you" not in revival_message(BACKGROUND, previous_exit_clean=False)
