"""The /processes sampler: CPU over a window, and who owns each process (#2663)."""

from pathlib import Path

from agentdeck import host_processes
from agentdeck.host_processes import ChatOwner, build_snapshot, sample_processes, unit_owner

TICKS = 100


def _write(
    root: Path,
    pid: int,
    *,
    ticks: int,
    ppid: int = 1,
    start: int = 500,
    cmd: str = "python app.py",
    unit: str = "agentdeck.service",
    rss_pages: int = 256,
) -> None:
    proc = root / str(pid)
    proc.mkdir(parents=True, exist_ok=True)
    utime, stime = ticks - ticks // 4, ticks // 4
    # Field 2 holds spaces and a ')' to prove the parser splits after the last one.
    fields = ["S", str(ppid)] + ["0"] * 9 + [str(utime), str(stime)] + ["0"] * 6 + [str(start)]
    (proc / "stat").write_text(f"{pid} (we ird) name) " + " ".join(fields) + " 0 0\n")
    (proc / "statm").write_text(f"1000 {rss_pages} 0 0 0 0 0\n")
    (proc / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
    (proc / "cgroup").write_text(f"0::/user.slice/user@1000.service/app.slice/{unit}\n")


def _sample(root: Path, second, chats=None, window_s=2.0):
    """Sample with the second /proc state applied while the window 'sleeps'."""
    return sample_processes(
        chats or {}, proc_root=root, window_s=window_s, sleep=lambda _s: second()
    )


def test_cpu_is_the_difference_over_the_window_not_the_lifetime_average(tmp_path, monkeypatch):
    monkeypatch.setattr(
        host_processes.os, "sysconf", lambda name: TICKS if name == "SC_CLK_TCK" else 4096
    )
    (tmp_path / "uptime").write_text("1000.0 0\n")
    _write(tmp_path, 10, ticks=50_000)  # busy for a long time, idle now
    _write(tmp_path, 11, ticks=100, unit="agentdeck-codex.service")

    def second():
        _write(tmp_path, 10, ticks=50_000)
        _write(tmp_path, 11, ticks=250, unit="agentdeck-codex.service")  # 150 ticks in 2 s

    snap = _sample(tmp_path, second)
    by_pid = {row.pid: row for row in snap.top}
    assert by_pid[10].cpu_pct == 0
    assert by_pid[11].cpu_pct == 75.0
    assert snap.top[0].pid == 11
    assert snap.owners[0].owner == "Deck runtime"
    assert snap.owners[0].cpu_pct == 75.0
    assert by_pid[11].rss_bytes == 256 * 4096
    assert by_pid[11].age_s == 995.0
    assert by_pid[11].command == "python app.py"


def test_a_process_gone_or_reused_inside_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(
        host_processes.os, "sysconf", lambda name: TICKS if name == "SC_CLK_TCK" else 4096
    )
    _write(tmp_path, 20, ticks=1_000)
    _write(tmp_path, 21, ticks=10_000, start=100)

    def second():
        import shutil

        shutil.rmtree(tmp_path / "20")
        # Same PID, new process: it has used only its own 40 ticks.
        _write(tmp_path, 21, ticks=40, start=900)

    snap = _sample(tmp_path, second)
    assert [row.pid for row in snap.top] == [21]
    assert snap.top[0].cpu_pct == 20.0


def test_an_unreadable_process_is_counted_not_raised(tmp_path):
    _write(tmp_path, 30, ticks=5)
    (tmp_path / "31").mkdir()  # no stat file: exited or not ours
    (tmp_path / "self").mkdir()

    snap = _sample(tmp_path, lambda: None, window_s=0.5)
    assert [row.pid for row in snap.top] == [30]
    assert snap.skipped == 1


def test_a_process_under_a_chat_worker_is_owned_by_that_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(
        host_processes.os, "sysconf", lambda name: TICKS if name == "SC_CLK_TCK" else 4096
    )
    unit = "agentdeck-worker-main-agentdeck-2663-89.scope"
    _write(tmp_path, 40, ticks=0, unit=unit, cmd="claude -p", start=700)
    _write(tmp_path, 41, ticks=0, ppid=40, unit=unit, cmd="bash -c pytest")
    _write(tmp_path, 42, ticks=0, ppid=41, unit=unit, cmd="pytest -q")
    _write(tmp_path, 50, ticks=0, unit=unit, cmd="stray", start=999)

    def second():
        _write(tmp_path, 42, ticks=100, ppid=41, unit=unit, cmd="pytest -q")

    chats = {
        40: ChatOwner("claude_code:main:abc", "Fix the CPU", "700"),
        # The deck recorded a different start time: PID 50 now belongs to something else.
        50: ChatOwner("claude_code:main:old", "Gone chat", "123"),
    }
    snap = _sample(tmp_path, second, chats)
    by_pid = {row.pid: row for row in snap.top}
    assert by_pid[42].owner == "Chat: Fix the CPU"
    assert by_pid[42].owner_href == "/sessions/claude_code:main:abc"
    assert by_pid[50].owner == "Agent worker (main-agentdeck-2663)"
    assert by_pid[50].owner_href is None
    chat = next(row for row in snap.owners if row.owner == "Chat: Fix the CPU")
    assert (chat.processes, chat.cpu_pct) == (3, 50.0)


def test_a_container_is_named_by_its_busiest_command(tmp_path, monkeypatch):
    monkeypatch.setattr(
        host_processes.os, "sysconf", lambda name: TICKS if name == "SC_CLK_TCK" else 4096
    )
    scope = "libpod-cc2a6b9c1d0e8528afbc599097d530b6766de202c3b88044a76608fc46d77fde.scope"

    def write(ticks_by_pid):
        for pid, (cmd, ticks) in ticks_by_pid.items():
            _write(tmp_path, pid, ticks=ticks, cmd=cmd)
            # A container's processes sit one level below its scope.
            (tmp_path / str(pid) / "cgroup").write_text(f"0::/user.slice/{scope}/container\n")

    write({60: ("mysqld", 0), 61: ("pytest app", 0)})
    snap = _sample(tmp_path, lambda: write({60: ("mysqld", 20), 61: ("pytest app", 120)}))
    assert snap.owners[0].owner == "Container cc2a6b9c (pytest app)"
    assert snap.owners[0].processes == 2
    assert {row.owner for row in snap.top} == {"Container cc2a6b9c"}


def test_two_chats_with_one_title_stay_two_owners(tmp_path):
    _write(tmp_path, 70, ticks=0, cmd="claude -p")
    _write(tmp_path, 71, ticks=0, cmd="claude -p")
    chats = {70: ChatOwner("k:a", "Same"), 71: ChatOwner("k:b", "Same")}
    snap = _sample(tmp_path, lambda: None, chats)
    assert sorted(row.owner_href for row in snap.owners) == ["/sessions/k:a", "/sessions/k:b"]


def test_unit_names_read_in_plain_words():
    assert unit_owner("agentdeck.service") == "Deck web"
    assert unit_owner("agentdeck-poller@trailhead.service") == "Poller (trailhead)"
    assert unit_owner("agentdeck-nudge-chat-worker.service") == "ChatGPT nudge"
    assert unit_owner("claude-rc-hass.service") == "Claude remote control (hass)"
    assert unit_owner("app-org.chromium.Chromium-3714099.scope") == "App (org.chromium.Chromium)"
    assert unit_owner("agentdeck-worker-main-chat-7f55759ccf39-r1a2b3c4d-9.scope") == (
        "Agent worker (main-chat-7f55759ccf39)"
    )
    # A key that itself ends in eight hex digits keeps them in either form.
    assert unit_owner("agentdeck-worker-main-chat-573dc744-9.scope") == (
        "Agent worker (main-chat-573dc744)"
    )
    assert unit_owner("mysqld.service") == "mysqld.service"
    assert unit_owner("") == "System"


def test_build_snapshot_with_no_window_reports_zero_cpu():
    snap = build_snapshot({}, {}, window_s=0, clock_ticks=TICKS, cores=4, chats={})
    assert snap.total_cpu_pct == 0 and snap.owners == () and snap.top == ()
