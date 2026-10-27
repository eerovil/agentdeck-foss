import base64
import json

from agentdeck.providers.claude_code import registry


def _si_token(session_id: str) -> str:
    """An ``sk-ant-si-<jwt>`` whose payload carries ``session_id``."""
    payload = base64.urlsafe_b64encode(json.dumps({"session_id": session_id}).encode())
    return b"sk-ant-si-hdr.".decode() + payload.rstrip(b"=").decode() + ".sig"


def _write_environ(make_proc, pid: int, **env: str) -> None:
    make_proc(pid, "19")  # creates /proc/<pid>/ (and its stat)
    blob = b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items())
    (make_proc.root / str(pid) / "environ").write_bytes(blob)


def test_read_registry_parses_and_skips_malformed(fake_config_dir):
    entries = registry.read_registry(fake_config_dir.path)
    assert len(entries) == 1  # the malformed bad.json is skipped
    e = entries[0]
    assert e.pid == 4242
    assert e.session_id == fake_config_dir.live_sid
    assert str(e.cwd) == "/tmp/proj"
    assert e.proc_start == "19"
    assert e.kind == "interactive"


def test_read_registry_missing_dir(tmp_path):
    assert registry.read_registry(tmp_path / "nope") == []


def test_is_alive_matching_starttime(fake_config_dir, make_proc):
    make_proc(4242, "19")
    (e,) = registry.read_registry(fake_config_dir.path)
    assert registry.is_alive(e, proc_root=make_proc.root) is True


def test_is_alive_dead_when_pid_absent(fake_config_dir, make_proc):
    # no proc entry created
    (e,) = registry.read_registry(fake_config_dir.path)
    assert registry.is_alive(e, proc_root=make_proc.root) is False


def test_is_alive_dead_on_pid_reuse(fake_config_dir, make_proc):
    make_proc(4242, "99999")  # pid exists but different starttime → reused
    (e,) = registry.read_registry(fake_config_dir.path)
    assert registry.is_alive(e, proc_root=make_proc.root) is False


def test_proc_starttime_none_when_missing(make_proc):
    assert registry.proc_starttime(12345, proc_root=make_proc.root) is None


def test_session_deep_link_from_token(make_proc):
    _write_environ(make_proc, 4242, CLAUDE_CODE_SESSION_ACCESS_TOKEN=_si_token("cse_abc123"))
    assert (
        registry.session_deep_link(4242, proc_root=make_proc.root)
        == "https://claude.ai/code/session_abc123"
    )


def test_session_deep_link_none_without_token(make_proc):
    _write_environ(make_proc, 4242, PATH="/usr/bin")
    assert registry.session_deep_link(4242, proc_root=make_proc.root) is None


def test_session_deep_link_none_on_garbled_token(make_proc):
    _write_environ(make_proc, 4242, CLAUDE_CODE_SESSION_ACCESS_TOKEN="sk-ant-si-notajwt")
    assert registry.session_deep_link(4242, proc_root=make_proc.root) is None


def test_session_deep_link_none_when_proc_missing(make_proc):
    assert registry.session_deep_link(9999, proc_root=make_proc.root) is None
