"""The cost dot: a chat's next message by its size, and its last day from its transcript."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

import agentdeck.web
from agentdeck import chat_use
from agentdeck.chat_use import ChatUse, ChatUseMeter, reading, size_level
from agentdeck.collector import Collector
from agentdeck.config import AccountConfig, AppConfig
from agentdeck.models import Account, Session, SessionStatus
from agentdeck.providers import PROVIDERS
from agentdeck.state import AppState
from agentdeck.web.render import ctx_level

NOW = datetime(2026, 11, 7, 6, tzinfo=UTC)
# 1,000,000 output tokens are 1.4 % of the week.
MILLION = {
    "input_tokens": 0,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "output_tokens": 1_000_000,
}


def row(call="m1", *, usage=MILLION, hours_ago=1.0, model="claude-opus-5-5", request="r1"):
    """One assistant line, in the shape Claude Code writes."""
    stamp = NOW - timedelta(hours=hours_ago)
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": stamp.isoformat().replace("+00:00", "Z"),
            "requestId": request,
            "message": {"id": call, "model": model, "role": "assistant", "usage": usage},
        }
    )


def write(path: Path, *lines: str, end="\n") -> Path:
    with path.open("a") as file:
        file.write("\n".join(lines) + end)
    return path


def measure(meter, path, *, scale=1.0, now=NOW):
    return meter.measure({"chat": (path, scale)}, now=now.timestamp()).get("chat")


@pytest.mark.parametrize(
    ("tokens", "level"),
    [
        (50_000, "cheap"),
        (99_999, "cheap"),
        (100_000, "middling"),
        (149_999, "middling"),
        (150_000, "expensive"),
        (900_000, "expensive"),
    ],
)
def test_a_context_size_picks_the_dot_and_the_badge(tokens, level):
    assert size_level(tokens) == level
    assert ctx_level(tokens) == {"cheap": "", "middling": "warn", "expensive": "crit"}[level]


def test_a_reading_goes_by_size_and_not_by_what_the_chat_spent():
    assert reading(170_400, False, 0.1) == ChatUse("expensive", 170_000, 0.1)
    assert reading(60_000, False, 3.0) == ChatUse("cheap", 60_000, 3.0)
    # Just under a limit stays under it, though it rounds up to it for the reader.
    assert reading(99_600, False, None) == ChatUse("cheap", 100_000, None)
    # A compact writes no size until the next message, and a chat that used nothing today
    # has a reading all the same.
    assert reading(None, True, 2.0) == ChatUse("cheap", None, 2.0)
    assert reading(None, False, 2.0) is None and reading(0, False, None) is None


def test_a_transcript_is_priced_by_the_fitted_weights(tmp_path):
    usage = {
        "input_tokens": 1_000_000,
        "cache_read_input_tokens": 10_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "output_tokens": 100_000,
    }
    path = write(tmp_path / "a.jsonl", row(usage=usage))
    # 0.28 + 0.065 + 0.28 + 0.14
    assert measure(ChatUseMeter(), path) == 0.8


def test_each_pass_reads_only_what_was_added_and_waits_for_a_whole_line(tmp_path):
    meter = ChatUseMeter()
    path = write(tmp_path / "a.jsonl", row("m1"))
    assert measure(meter, path) == 1.4
    half = row("m2")
    write(path, half[:20], end="")
    assert measure(meter, path) == 1.4
    write(path, half[20:])
    assert measure(meter, path) == 2.8
    # Unchanged file: the bytes already read are not read again.
    assert measure(meter, path) == 2.8
    # A restart keeps nothing: the first pass rebuilds the same number from the file.
    assert measure(ChatUseMeter(), path) == 2.8


def test_a_call_counts_once_however_many_rows_or_files_repeat_it(tmp_path):
    small = {**MILLION, "output_tokens": 10}
    meter = ChatUseMeter()
    path = write(tmp_path / "a.jsonl", row("m1", usage=small), row("m1"), row("m1", usage=small))
    assert measure(meter, path) == 1.4
    resumed = write(tmp_path / "b.jsonl", row("m1"), row("m2"))
    readings = meter.measure({"old": (path, 1.0), "new": (resumed, 1.0)}, now=NOW.timestamp())
    assert (readings["old"], readings["new"]) == (1.4, 1.4)


def test_what_cannot_be_read_is_skipped(tmp_path):
    empty_fields = {"input_tokens": None, "output_tokens": 1_000_000}
    path = write(
        tmp_path / "a.jsonl",
        'not json with "usage" in it',
        '["usage"]',
        json.dumps({"timestamp": "yesterday", "message": {"usage": MILLION}}),
        json.dumps({"timestamp": NOW.isoformat(), "message": {"usage": "none"}}),
        row("m0", model="<synthetic>"),
        row("m1", usage=empty_fields),
    )
    assert measure(ChatUseMeter(), path) == 1.4


def test_a_day_old_call_stops_counting_and_a_quiet_chat_loses_its_dot(tmp_path):
    meter = ChatUseMeter()
    path = write(tmp_path / "a.jsonl", row("old", hours_ago=25), row("new", hours_ago=23))
    assert measure(meter, path) == 1.4
    assert measure(meter, path, now=NOW + timedelta(hours=2)) is None
    # Rows need not be in time order: a resumed chat can copy an older call in after a newer one.
    mixed = write(tmp_path / "b.jsonl", row("new", hours_ago=23), row("old", hours_ago=25))
    assert measure(ChatUseMeter(), mixed) == 1.4


def test_a_rewritten_file_is_read_again_and_a_missing_one_is_left_alone(tmp_path):
    meter = ChatUseMeter()
    path = write(tmp_path / "a.jsonl", row("m1"), row("m2"))
    assert measure(meter, path) == 2.8
    path.write_text(row("m3") + "\n")
    assert measure(meter, path) == 1.4
    assert measure(meter, tmp_path / "gone.jsonl") is None


def test_an_old_file_is_not_read_at_all(tmp_path):
    path = write(tmp_path / "a.jsonl", row("m1"))
    later = NOW + timedelta(days=2)
    meter = ChatUseMeter()
    assert measure(meter, path, now=later) is None
    assert meter._offsets[path] == path.stat().st_size


def test_a_smaller_week_counts_its_tokens_double(tmp_path):
    usage = {**MILLION, "output_tokens": 100_000}  # 0.14 %
    path = write(tmp_path / "a.jsonl", row(usage=usage))
    assert measure(ChatUseMeter(), path) == 0.1
    assert measure(ChatUseMeter(), path, scale=2) == 0.3
    assert AccountConfig(provider="claude_code", label="alt", config_dir="/x").week_scale == 1
    with pytest.raises(ValueError):
        AccountConfig(provider="claude_code", label="alt", config_dir="/x", week_scale=0)


def test_the_tooltip_says_what_the_next_message_costs():
    # The issue's table: 170k is 0.0011 % warm and 0.048 % cold.
    assert ChatUse("expensive", 170_000, 0.8).title == (
        "Expensive: 170k in context. Next message, without its reply: 0.0011 % of the week if "
        "it follows soon, 0.048 % after a pause. Last 24 hours: 0.8 % of the week."
    )
    assert "0.0013 % of the week if it follows soon, 0.056 %" in (
        ChatUse("middling", 100_000, scale=2).title
    )
    assert "Last 24 hours" not in ChatUse("cheap", 50_000).title
    assert "under 0.1 % of the week." in ChatUse("cheap", 50_000, 0.0).title
    assert ChatUse("cheap", None, 2.0).lines == (
        "Just compacted: the exact size shows after the next message.",
        "Last 24 hours: 2.0 % of the week.",
    )


def test_only_providers_with_fitted_weights_offer_a_transcript(tmp_path):
    account = Account("codex:one", "codex", "one", tmp_path)
    assert PROVIDERS["codex"].use_transcript(account, None) is None
    assert not PROVIDERS["codex"].fitted_weights and PROVIDERS["claude_code"].fitted_weights


def _dot(html: str) -> str | None:
    found = re.search(r'data-chat-use="(\w+)"', html)
    return found[1] if found else None


def test_the_dot_says_its_colour_in_words():
    page = (
        Environment(loader=FileSystemLoader(Path(agentdeck.web.__file__).parent / "templates"))
        .get_template("partials/work_macros.html")
        .make_module({})
    )
    use = ChatUse("expensive", 170_000, 0.8)
    html = str(page.use_dot(use))
    assert _dot(html) == "expensive"
    assert 'class="use-dot use-expensive"' in html
    assert f'aria-label="{use.title}"' in html
    assert str(page.use_dot(None)) == ""


def _session(account: str, session_id: str, active: datetime, **fields) -> Session:
    return Session(
        key=f"{account}:{session_id}",
        account_key=account,
        session_id=session_id,
        status=SessionStatus.LIVE,
        last_activity=active,
        **fields,
    )


def test_a_pass_reads_active_claude_chats_at_their_accounts_scale(tmp_path):
    usage = {**MILLION, "output_tokens": 100_000}  # 0.14 %
    now = datetime.now(UTC)
    stamp = (now - timedelta(hours=1)).isoformat()
    line = json.loads(row(usage=usage))
    line["timestamp"] = stamp
    project = tmp_path / "claude" / "projects" / "proj"
    project.mkdir(parents=True)
    for name in ("active", "quiet"):
        write(project / f"{name}.jsonl", json.dumps({**line, "requestId": name}))
    (tmp_path / "codex").mkdir()
    config = AppConfig(
        accounts=[
            AccountConfig(
                provider="claude_code",
                label="alt",
                config_dir=str(tmp_path / "claude"),
                week_scale=2,
            ),
            AccountConfig(provider="codex", label="one", config_dir=str(tmp_path / "codex")),
        ]
    )
    state = AppState()
    active = _session("claude_code:alt", "active", now, context_tokens=170_000)
    quiet = _session("claude_code:alt", "quiet", now - timedelta(days=5), context_tokens=170_000)
    compacted = _session("claude_code:alt", "compacted", now, context_compacted=True)
    for session in (
        active,
        quiet,
        compacted,
        _session("claude_code:alt", "unknown", now),
        _session("codex:one", "active", now, context_tokens=170_000),
    ):
        state.update_session(session)

    readings = Collector(config, state)._chat_use_readings(now.timestamp())

    # The quiet chat's day ran out, but its size did not: waking it costs the same.
    assert readings == {
        active.key: ChatUse("expensive", 170_000, 0.3, 2),
        quiet.key: ChatUse("expensive", 170_000, None, 2),
        compacted.key: ChatUse("cheap", None, None, 2),
    }


def test_the_report_and_the_dot_share_one_set_of_weights():
    import importlib.util

    script = Path(__file__).parent.parent / "scripts" / "token_report.py"
    spec = importlib.util.spec_from_file_location("token_report_weights", script)
    report = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = report
    spec.loader.exec_module(report)
    assert (report.READ, report.WRITE, report.OUTPUT, report.INPUT) == (
        chat_use.READ,
        chat_use.WRITE,
        chat_use.OUTPUT,
        chat_use.INPUT,
    )
