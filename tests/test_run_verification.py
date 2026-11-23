"""A Run proves its work with an explicit verification job (#2154).

The retired verify phase used to bind a job to a card through a checkpoint. What replaces it
is smaller: the job says it is verification, the deck names the Run of the chat that submitted
it, and the queue records what the job tested. The engine's side of that — which job credits
which commit — is covered in ``test_poller_engine_cycle``; this file covers the submission.
"""

from __future__ import annotations

import subprocess
import sys
import time

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck import __main__ as cli
from agentdeck.app import create_app
from agentdeck.config import AppConfig, HistoryConfig
from agentdeck.job_client import JobClientError
from agentdeck.jobs import VERIFICATION, JobError, JobQueue
from agentdeck.poller_engine.verification import credited_head, unfinished_jobs

WORK = "github_issue:eerovil/agentdeck#2154"


# --- the deck names the calling chat's Run ------------------------------------


def _deck(tmp_path):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
    )
    return create_app(config)


async def _by_token(app, token: str) -> dict:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get(f"/api/sessions/by-token/{token}")
    assert response.status_code == 200
    return response.json()


async def test_the_token_lookup_names_the_run_the_chat_most_recently_joined(tmp_path):
    app = _deck(tmp_path)
    state = app.state.app_state
    state.bind_session_token("tok-1", "claude_code:main:s1")
    state.bind_session_run("claude_code:main:s1", WORK, "gen-1")
    time.sleep(0.001)
    state.bind_session_run("claude_code:main:s1", WORK, "gen-2")

    body = await _by_token(app, "tok-1")

    assert body["session_key"] == "claude_code:main:s1"
    assert (body["work_id"], body["run_id"]) == (WORK, "gen-2")


async def test_a_continued_leg_names_the_run_of_the_leg_it_continues(tmp_path):
    """#2881: a Continue's new leg is the same chat, so `--verification` from it lands."""
    app = _deck(tmp_path)
    state = app.state.app_state
    old, new = "claude_code:alt:old", "claude_code:main:new"
    state.bind_session_token("tok-old", old)
    state.budget_db.bind_session_run(old, WORK, "gen-1", delivery_id="poller:d1")
    assert state.budget_db.reserve_chat_successor("tok-new", old) == "new"

    state.bind_session_token("tok-new", new)
    body = await _by_token(app, "tok-new")

    assert body["session_key"] == new
    assert (body["work_id"], body["run_id"]) == (WORK, "gen-1")
    # A person's answer in the new leg still reaches a parked card (#2348).
    assert state.budget_db.latest_session_run(new).first_delivery_id == "poller:d1"


async def test_a_new_chat_that_continues_nothing_joins_no_run(tmp_path):
    app = _deck(tmp_path)
    state = app.state.app_state
    state.bind_session_token("tok-old", "claude_code:alt:old")
    state.bind_session_run("claude_code:alt:old", WORK, "gen-1")

    state.bind_session_token("tok-new", "claude_code:main:new")

    assert (await _by_token(app, "tok-new"))["run_id"] == ""


async def test_a_chat_in_no_run_names_none(tmp_path):
    app = _deck(tmp_path)
    app.state.app_state.bind_session_token("tok-1", "claude_code:main:s1")

    body = await _by_token(app, "tok-1")

    assert (body["work_id"], body["run_id"]) == ("", "")


# --- `agentdeck job submit --verification` ------------------------------------


class _Args:
    def __init__(self, **values):
        self.command = ["--", sys.executable, "-c", "print('ok')"]
        self.cwd = None
        self.where = "local"
        self.wait = False
        self.label = ""
        self.session = None
        self.session_token = None
        self.verification = False
        self.workspace = []
        self.work = ""
        self.max_minutes = None
        self.progress = False
        self.__dict__.update(values)


def _submit(monkeypatch, tmp_path, args, *, resolved: dict | None = None) -> dict:
    sent: dict = {}

    def submit_job(_socket, **kwargs):
        sent.update(kwargs)
        return {"id": "job-1", "log_path": str(tmp_path / "log"), "purpose": kwargs["purpose"]}

    monkeypatch.setattr(cli, "submit_job", submit_job)
    monkeypatch.setattr(
        cli, "_resolve_session_token", lambda _token: resolved or {"session_key": "s:1"}
    )
    args.cwd = str(tmp_path)
    cli._job_submit(None, tmp_path / "runtime.sock", args)
    return sent


def test_a_verification_job_is_attached_to_the_deck_s_answer_for_the_chat(
    monkeypatch, tmp_path
):
    sent = _submit(
        monkeypatch,
        tmp_path,
        _Args(verification=True, session_token="tok-1"),
        resolved={"session_key": "s:1", "work_id": WORK, "run_id": "gen-1"},
    )

    assert (sent["purpose"], sent["work_id"], sent["run_id"]) == (VERIFICATION, WORK, "gen-1")
    assert sent["session_key"] == "s:1"


def test_an_ordinary_job_from_a_run_s_chat_carries_its_run_but_no_purpose(
    monkeypatch, tmp_path
):
    """#2622: the poller holds a card while its worker waits on any job of its Run, so an
    ordinary job names the Run too — and stays ordinary, so it is never evidence. Before #2622
    an ordinary job carried no Run at all."""
    lookups: list[str] = []

    def resolve(token):
        lookups.append(token)
        return {"session_key": "s:1", "work_id": WORK, "run_id": "gen-1"}

    sent: dict = {}

    def submit_job(_socket, **kwargs):
        sent.update(kwargs)
        return {"id": "job-1", "log_path": str(tmp_path / "log"), "purpose": kwargs["purpose"]}

    monkeypatch.setattr(cli, "submit_job", submit_job)
    monkeypatch.setattr(cli, "_resolve_session_token", resolve)

    cli._job_submit(
        None, tmp_path / "runtime.sock", _Args(session_token="tok-1", cwd=str(tmp_path))
    )

    assert (sent["purpose"], sent["work_id"], sent["run_id"]) == ("", WORK, "gen-1")
    assert sent["session_key"] == "s:1"
    assert lookups == ["tok-1"], "one lookup names both the chat and its Run"


def test_an_ordinary_job_from_a_chat_in_no_run_is_queued_unlinked(monkeypatch, tmp_path):
    sent = _submit(
        monkeypatch,
        tmp_path,
        _Args(session_token="tok-1"),
        resolved={"session_key": "s:1", "work_id": "", "run_id": ""},
    )

    assert (sent["purpose"], sent["work_id"], sent["run_id"]) == ("", "", "")


def test_an_ordinary_job_named_by_session_key_alone_is_unlinked(monkeypatch, tmp_path):
    sent = _submit(monkeypatch, tmp_path, _Args(session="s:1"))

    assert (sent["session_key"], sent["work_id"], sent["run_id"]) == ("s:1", "", "")


def test_session_key_prints_the_token_s_chat_or_fails(monkeypatch, capsys):
    # What the brief tells a chat to run instead of curling the by-token route (#3350).
    monkeypatch.setattr(cli, "_resolve_session_token", lambda token: {"session_key": f"s:{token}"})
    assert cli._session_key(["tok"]) == 0
    assert capsys.readouterr().out == "s:tok\n"

    def unbound(_token):
        raise JobClientError("that session token is bound to no session")

    monkeypatch.setattr(cli, "_resolve_session_token", unbound)
    assert cli._session_key(["tok"]) == 1
    assert "bound to no session" in capsys.readouterr().err


def test_bookkeep_makes_each_asked_request_once_and_goes_on_past_a_failure(
    monkeypatch, capsys
):
    # A Work agent's start and end bookkeeping in one call (#4254).
    import io
    import json

    monkeypatch.setenv("AGENTDECK_URL", "http://deck")
    monkeypatch.setenv("AGENTDECK_SESSION_TOKEN", "tok")
    monkeypatch.setattr(
        cli, "_resolve_session_token", lambda token: {"session_key": f"c:a:{token}"}
    )
    sent = []

    def request(method, url, json=None, timeout=None):
        sent.append((method, url, json))
        status = 422 if url.endswith("/pull-requests") else 200
        return httpx.Response(status, json={"ok": status == 200})

    monkeypatch.setattr(cli.httpx, "request", request)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"background": "Tausta", "problem": "Ongelma"}'))
    code = cli._bookkeep(
        [
            "--title", "Cut the bookkeeping",
            "--summary", "-",
            "--pull-request", "o/r#5",
            "--wake-on", '{"review": "o/r#5"}',
        ]
    )

    assert code == 1  # the refused pull request
    assert sent == [
        ("PUT", "http://deck/api/sessions/c%3Aa%3Atok/title", {"title": "Cut the bookkeeping"}),
        (
            "PUT",
            "http://deck/api/sessions/c%3Aa%3Atok/work-resolution",
            {"background": "Tausta", "problem": "Ongelma"},
        ),
        ("POST", "http://deck/api/sessions/c%3Aa%3Atok/pull-requests", {"pull_request": "o/r#5"}),
        ("POST", "http://deck/api/sessions/c%3Aa%3Atok/wake-on", {"review": "o/r#5"}),
    ]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0] == {"session_key": "c:a:tok"}
    assert [(line["step"], line["ok"]) for line in lines[1:]] == [
        ("title", True), ("summary", True), ("pull_request", False), ("wake_on", True)
    ]


def test_bookkeep_refuses_a_bad_summary_before_any_request(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTDECK_SESSION_TOKEN", "tok")
    monkeypatch.setattr(cli, "_resolve_session_token", lambda _t: pytest.fail("resolved"))
    summary = tmp_path / "s.json"
    summary.write_text('{"problme": "typo"}')
    with pytest.raises(SystemExit) as exit_:
        cli._bookkeep(["--summary", str(summary)])
    assert exit_.value.code == 2
    monkeypatch.delenv("AGENTDECK_SESSION_TOKEN")
    with pytest.raises(SystemExit):
        cli._bookkeep(["--title", "x"])


def test_verification_without_a_session_token_is_refused_before_anything_is_queued(
    monkeypatch, tmp_path
):
    with pytest.raises(JobClientError, match="needs --session-token"):
        _submit(monkeypatch, tmp_path, _Args(verification=True, session="s:1"))


def test_verification_from_a_chat_in_no_run_is_refused(monkeypatch, tmp_path):
    with pytest.raises(JobClientError, match="not part of any poller Run"):
        _submit(
            monkeypatch,
            tmp_path,
            _Args(verification=True, session_token="tok-1"),
            resolved={"session_key": "s:1", "work_id": "", "run_id": ""},
        )


def test_verification_from_a_chat_in_several_works_names_one_of_its_own(monkeypatch, tmp_path):
    # #3392: a chat that finished one Work and took on another was refused outright.
    works = ["github_issue:eerovil/agentdeck#3367", "github_issue:eerovil/agentdeck#3383"]
    resolved = {"session_key": "s:1", "work_id": "", "run_id": "", "works": works}
    with pytest.raises(JobClientError, match="several Works; name the one .* --work: .*#3367"):
        _submit(
            monkeypatch, tmp_path, _Args(verification=True, session_token="t"), resolved=resolved
        )
    sent = _submit(
        monkeypatch,
        tmp_path,
        _Args(verification=True, session_token="t", work="EEROVIL/agentdeck#3383"),
        resolved=resolved,
    )
    assert (sent["work_id"], sent["run_id"]) == (works[1], "")
    # A chat whose Run was for #3367 names #3383, its own later Work, outside that Run.
    run_bound = {**resolved, "work_id": works[0], "run_id": "gen-1"}
    with pytest.raises(JobClientError, match="several Works"):
        _submit(
            monkeypatch, tmp_path, _Args(verification=True, session_token="t"), resolved=run_bound
        )
    sent = _submit(
        monkeypatch,
        tmp_path,
        _Args(verification=True, session_token="t", work="eerovil/agentdeck#3383"),
        resolved=run_bound,
    )
    assert (sent["work_id"], sent["run_id"]) == (works[1], "")
    with pytest.raises(JobClientError, match="not one of this chat's Works"):
        _submit(
            monkeypatch,
            tmp_path,
            _Args(verification=True, session_token="t", work="eerovil/agentdeck#1"),
            resolved=resolved,
        )


def test_an_older_runtime_that_drops_the_purpose_is_named_at_submission(
    monkeypatch, tmp_path, capsys
):
    def submit_job(_socket, **_kwargs):
        return {"id": "job-1", "log_path": str(tmp_path / "log")}

    monkeypatch.setattr(cli, "submit_job", submit_job)
    monkeypatch.setattr(
        cli,
        "_resolve_session_token",
        lambda _token: {"session_key": "s:1", "work_id": WORK, "run_id": "gen-1"},
    )
    args = _Args(verification=True, session_token="tok-1", cwd=str(tmp_path))

    cli._job_submit(None, tmp_path / "runtime.sock", args)

    err = capsys.readouterr().err
    assert "will not count as release evidence" in err
    assert "--workspace checkout needs a verification job of its own" not in err
    # #4589: a run naming a second checkout says that only --cwd's commit is credited.
    monkeypatch.setattr(cli, "records_other_workspaces", lambda _socket: True)
    monkeypatch.setattr(
        cli,
        "submit_job",
        lambda _socket, **kwargs: {
            "id": "job-2",
            "log_path": "",
            "workspaces": kwargs.get("workspaces"),
        },
    )
    args = _Args(
        verification=True, session_token="tok-1", cwd=str(tmp_path), workspace=[str(tmp_path)]
    )
    cli._job_submit(None, tmp_path / "runtime.sock", args)
    err = capsys.readouterr().err
    assert f"only the commit in --cwd ({tmp_path}) is credited as tested" in err


# --- the queue records the purpose and keeps the evidence --------------------


def _queue(tmp_path) -> JobQueue:
    pool = tmp_path / "slots"
    pool.mkdir(exist_ok=True)
    return JobQueue(state_dir=tmp_path / "jobs", slot_paths=[pool / "slot-0"])


def test_the_queue_records_a_verification_job_and_its_run(tmp_path):
    queue = _queue(tmp_path)

    job = queue.submit(
        command=[sys.executable, "-c", "print('ok')"],
        cwd=str(tmp_path),
        purpose=VERIFICATION,
        work_id=WORK,
        run_id="gen-1",
    )

    row = job.to_dict()
    assert (row["purpose"], row["work_id"], row["run_id"]) == (VERIFICATION, WORK, "gen-1")
    fresh = _queue(tmp_path)
    fresh.load()
    reloaded = fresh.get(job.id)
    assert reloaded is not None and reloaded.run_id == "gen-1"


def test_the_queue_refuses_evidence_for_no_run_and_an_unknown_purpose(tmp_path):
    queue = _queue(tmp_path)
    command = [sys.executable, "-c", "print('ok')"]

    with pytest.raises(JobError, match="must name the Run"):
        queue.submit(command=command, cwd=str(tmp_path), purpose=VERIFICATION)
    with pytest.raises(JobError, match="unknown job purpose"):
        queue.submit(command=command, cwd=str(tmp_path), purpose="release", run_id="gen-1")


def test_an_ordinary_job_keeps_its_run_but_stays_ordinary(tmp_path):
    """#2622: before it, the queue dropped the Run from an ordinary job."""
    queue = _queue(tmp_path)
    job = queue.submit(
        command=[sys.executable, "-c", "print('ok')"], cwd=str(tmp_path),
        work_id=WORK, run_id="gen-1",
    )

    assert (job.purpose, job.work_id, job.run_id) == ("", WORK, "gen-1")
    fresh = _queue(tmp_path)
    fresh.load()
    reloaded = fresh.get(job.id)
    assert reloaded is not None and (reloaded.purpose, reloaded.run_id) == ("", "gen-1")


def test_a_finished_ordinary_job_of_a_run_is_credited_nothing_and_cancelled_never():
    row = {
        "id": "job-1", "purpose": "", "run_id": "gen-1", "work_id": WORK,
        "state": "done", "result_class": "success",
        "tested_workspace": _clean("abc"), "verified_workspace": _clean("abc"),
        "tested_workspaces": {"r": _clean("abc")}, "verified_workspaces": {"r": _clean("abc")},
    }

    assert credited_head(row, run_id="gen-1", work_id=WORK) == ""
    assert unfinished_jobs([{**row, "state": "running"}], run_id="gen-1") == []


# --- the rule, on its own ------------------------------------------------------


_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _clean(head: str) -> dict:
    return {
        "root": "/w", "head": head, "staged_sha256": _EMPTY, "tracked_diff_sha256": _EMPTY,
        "untracked_sha256": _EMPTY, "untracked_count": 0,
    }


@pytest.mark.parametrize(
    ("job", "work", "head"),
    [
        ({"purpose": VERIFICATION, "run_id": "gen-1", "work_id": WORK}, WORK, "abc"),
        # A job that names no Work is still this Run's: the Run id alone is unique.
        ({"purpose": VERIFICATION, "run_id": "gen-1"}, WORK, "abc"),
        # A deck that names no Work for the card cannot refuse on it either.
        ({"purpose": VERIFICATION, "run_id": "gen-1", "work_id": WORK}, "", "abc"),
        ({"purpose": VERIFICATION, "run_id": "gen-1", "work_id": "github_issue:x/y#1"}, WORK, ""),
        ({"purpose": VERIFICATION, "run_id": "GEN-1", "work_id": WORK}, WORK, ""),
        ({"purpose": "Verification", "run_id": "gen-1"}, WORK, ""),
        ({"purpose": VERIFICATION, "run_id": ""}, WORK, ""),
    ],
)
def test_credited_head_is_exact_about_which_run_a_job_belongs_to(job, work, head):
    row = {
        **job, "state": "done", "result_class": "success",
        "tested_workspace": _clean("abc"), "verified_workspace": _clean("abc"),
        "tested_workspaces": {"r": _clean("abc")}, "verified_workspaces": {"r": _clean("abc")},
    }

    assert credited_head(row, run_id="gen-1", work_id=work) == head


def test_a_row_without_a_repository_keyed_record_credits_nothing():
    """Every row since #2148 has one; a row without it is malformed, not old evidence (#2214)."""
    row = {
        "purpose": VERIFICATION, "run_id": "gen-1", "state": "done", "result_class": "success",
        "tested_workspace": _clean("abc"), "verified_workspace": _clean("abc"),
    }

    assert credited_head(row, run_id="gen-1") == ""


def test_credited_head_needs_a_run_to_credit():
    row = {
        "purpose": VERIFICATION, "run_id": "", "state": "done", "result_class": "success",
        "tested_workspace": _clean("abc"),
    }

    assert credited_head(row, run_id="") == ""
    assert credited_head(None, run_id="gen-1") == ""


def test_the_verification_submit_request_is_unchanged_for_ordinary_jobs(monkeypatch, tmp_path):
    """An ordinary submission's wire body is exactly what an older runtime already accepts."""
    from agentdeck import job_client

    bodies: list[dict] = []

    def call(_socket, _method, _path, *, json):
        bodies.append(json)
        return {}

    monkeypatch.setattr(job_client, "_call", call)
    job_client.submit_job(httpx.URL("http://unused"), command=["true"], cwd="/")
    job_client.submit_job(
        httpx.URL("http://unused"), command=["true"], cwd="/",
        purpose=VERIFICATION, work_id=WORK, run_id="gen-1",
    )
    # A Work chat with no Run: its Work rides along, for a star only (#3116).
    job_client.submit_job(httpx.URL("http://unused"), command=["true"], cwd="/", work_id=WORK)

    assert "purpose" not in bodies[0] and "work_id" not in bodies[0]
    assert (bodies[1]["purpose"], bodies[1]["run_id"]) == (VERIFICATION, "gen-1")
    assert "purpose" not in bodies[2] and bodies[2]["work_id"] == WORK
    from test_jobs import _queue

    job = _queue(tmp_path).submit(**bodies[2])
    assert (job.work_id, job.owner_work_id) == ("", WORK), "ordered by its Work, no evidence"


# --- the tree has to hold still while the suite runs (review of #2177) --------


def _git_repo(path):
    import subprocess

    path.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    (path / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(path), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "one"], check=True)
    return path


async def _run_one(tmp_path, *, command, purpose=""):
    from test_jobs import _queue as queue_for
    from test_jobs import _settle

    repo = _git_repo(tmp_path / "repo")
    queue = queue_for(tmp_path)
    await queue.start()
    try:
        job = queue.submit(
            command=command,
            cwd=str(repo),
            purpose=purpose,
            run_id="gen-1" if purpose else "",
        )
        return await _settle(queue, job.id)
    finally:
        await queue.stop()


def _as_row(job) -> dict:
    return job.to_dict()


async def test_a_verification_job_records_a_second_reading_that_matches_a_still_tree(tmp_path):
    job = await _run_one(
        tmp_path, command=[sys.executable, "-c", "print('ok')"], purpose=VERIFICATION
    )

    row = _as_row(job)
    assert row["verified_workspace"]["snapshot_id"] == row["tested_workspace"]["snapshot_id"]
    head = row["tested_workspace"]["head"]
    assert credited_head(row, run_id="gen-1") == head


async def test_a_tree_edited_while_the_suite_ran_credits_nothing(tmp_path):
    """The race the review found: the first reading is taken before the command starts, so a
    tree that changes mid-run must not credit the clean commit it began at."""
    edit = "import pathlib; pathlib.Path('a.txt').write_text('two\\n')"
    job = await _run_one(tmp_path, command=[sys.executable, "-c", edit], purpose=VERIFICATION)

    row = _as_row(job)
    assert row["state"] == "done"
    assert row["tested_workspace"]["clean"] is True
    assert row["verified_workspace"]["snapshot_id"] != row["tested_workspace"]["snapshot_id"]
    assert credited_head(row, run_id="gen-1") == ""


_EDIT = "import pathlib; pathlib.Path('a.txt').write_text('two\\n')"


def _never_waits(job):
    return False


def _waits(job):
    return True


def _check_breaks(job):
    raise OSError("db")


@pytest.mark.parametrize(
    ("code", "fields", "woken", "waits"),
    [
        ("print('ok')", {"purpose": VERIFICATION, "work_id": WORK}, False, _never_waits),
        # #4589: a second checkout named with --workspace is not credited, and the note says so.
        (
            "print('ok')",
            {"purpose": VERIFICATION, "work_id": WORK, "workspaces": "other"},
            False,
            _never_waits,
        ),
        # No ``chat_waits`` given: silent, as before #4556.
        ("print('ok')", {"purpose": VERIFICATION, "work_id": WORK}, False, None),
        # #4556: its chat has not marked a pull request ready, so it waits on this run.
        ("print('ok')", {"purpose": VERIFICATION, "work_id": WORK}, True, _waits),
        ("print('ok')", {"purpose": VERIFICATION, "work_id": WORK}, True, _check_breaks),
        (_EDIT, {"purpose": VERIFICATION, "work_id": WORK}, True, _never_waits),
        ("raise SystemExit(1)", {"purpose": VERIFICATION, "work_id": WORK}, True, _never_waits),
        # The poller holds its card until this message is delivered (``awaited_run_job``).
        (
            "print('ok')",
            {"purpose": VERIFICATION, "work_id": WORK, "run_id": "gen-1"},
            True,
            _never_waits,
        ),
        ("print('ok')", {"work_id": WORK}, True, _never_waits),
    ],
    ids=[
        "proved",
        "proved-with-workspace",
        "proved-no-check",
        "proved-pull-still-draft",
        "proved-check-failed",
        "tree-moved",
        "failed",
        "poller-run",
        "ordinary",
    ],
)
async def test_only_a_green_run_that_proves_its_commit_wakes_nobody(
    tmp_path, code, fields, woken, waits
):
    from test_jobs import _queue as queue_for
    from test_jobs import _settle, _wait_for

    told: list[str] = []
    notes: dict[str, str] = {}

    async def notify(job, message):
        told.append(message)
        return True

    repo = _git_repo(tmp_path / "repo")
    if "workspaces" in fields:
        # Two repositories prove a commit only when each names its repository (#2148).
        other = _git_repo(tmp_path / fields["workspaces"])
        for path, name in ((repo, "one"), (other, "two")):
            url = f"https://github.com/eerovil/{name}.git"
            subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)
        fields = {**fields, "workspaces": [str(other)]}
    queue = queue_for(tmp_path, notify=notify)
    # #4306: the run that wakes nobody leaves its chat a pending note, once per job and commit.
    queue._keep_note = lambda job, note_id, text: notes.setdefault(note_id, text) is not None
    queue._chat_waits = waits
    await queue.start()
    try:
        job = queue.submit(
            command=[sys.executable, "-c", code],
            cwd=str(repo),
            session_key="claude_code:main:sid",
            **fields,
        )
        await _settle(queue, job.id)
        await _wait_for(lambda: queue.get(job.id).notified, what="the queue to be done with it")
    finally:
        await queue.stop()

    assert bool(told) is woken
    assert bool(notes) is not woken
    if notes:
        head = queue.get(job.id).tested_workspace["head"]
        assert list(notes) == [f"job-proved:{job.id}@{head[:12]}"]
        note = notes[f"job-proved:{job.id}@{head[:12]}"]
        assert f"passed at {head[:12]}" in note
        assert ("named with --workspace are not credited" in note) is ("workspaces" in fields)


_OPEN = {"state": "open", "isDraft": False}
_DRAFT = {"state": "OPEN", "isDraft": True}


@pytest.mark.parametrize(
    ("pulls", "unnamed", "waits"),
    [
        # The stalled Work of #4556: both of its pull requests still drafts.
        ([(_DRAFT, True), (_DRAFT, True)], [], True),
        ([(_OPEN, True)], [], False),
        # The webhook's spelling and ``gh``'s, an earlier round merged long ago.
        ([({"state": "MERGED", "isDraft": False}, False), (_OPEN, True)], [], False),
        ([(_OPEN, True), (_DRAFT, True)], [], True),
        ([({"state": "MERGED"}, True), ({"state": "CLOSED", "isDraft": True}, True)], [], True),
        ([], [], True),
        ([(_OPEN, True), (None, False)], [], True),
        ([("not a row", True)], [], True),
        ([(_OPEN, True), ({"isDraft": False}, True)], [], True),
        # Read ready too long ago: it may have been turned back to a draft since.
        ([(_OPEN, False)], [], True),
        # A registration that named no Work may be another Work's: it wakes, never silences.
        ([], [(_OPEN, True)], True),
        ([(_OPEN, True)], [(_DRAFT, False)], True),
        # Unknown, so waiting (#4557 review): an unnamed one stale or never read, and an
        # open one whose draft flag is missing.
        ([(_OPEN, True)], [(_OPEN, False)], True),
        ([(_OPEN, True)], [(None, False)], True),
        ([({"state": "OPEN"}, True)], [], True),
        ([(_OPEN, True)], [({"state": "MERGED"}, False), (_OPEN, True)], False),
    ],
    ids=[
        "drafts",
        "ready",
        "merged-and-ready",
        "one-still-draft",
        "none-open",
        "none-registered",
        "one-unread",
        "odd-row",
        "no-state",
        "ready-read-long-ago",
        "only-unnamed-ready",
        "unnamed-draft",
        "unnamed-read-long-ago",
        "unnamed-unread",
        "no-draft-flag",
        "unnamed-merged-and-ready",
    ],
)
def test_a_chat_waits_on_its_proof_until_its_pull_requests_are_ready(pulls, unnamed, waits):
    from agentdeck.jobs import awaits_ready

    assert awaits_ready(pulls, unnamed) is waits


def test_the_runtime_reads_the_chats_own_pulls_of_this_work_from_the_cache(tmp_path):
    """#4556 with the real stores: the control database's registrations of the job's chat for
    its Work, under every leg of the chat, and the draft flag the webhooks wrote into the
    shared GitHub cache."""
    from agentdeck.db import Db
    from agentdeck.github_cache import GitHubMetadata, GitHubMetadataCache
    from agentdeck.jobs import Job
    from agentdeck.models import PullRequestRef
    from agentdeck.runtime import chat_waits_on_proof

    db = Db(tmp_path / "control.db")
    cache = GitHubMetadataCache(tmp_path / "github.sqlite3")
    old, key = "claude_code:main:old", "claude_code:main:sid"
    db.append_chat_leg(old, key)
    other = "github_issue:eerovil/agentdeck#1"
    # Registered on the chat's earlier leg, with the Work's id in another case.
    db.record_session_pull_request(old, PullRequestRef("Eerovil/Agentdeck", 7, WORK.upper()))
    db.record_session_pull_request(key, PullRequestRef("eerovil/agentdeck", 8, other))
    db.record_session_pull_request("claude_code:main:else", PullRequestRef("eerovil/x", 9, WORK))
    for number, draft in ((7, True), (8, True), (9, False)):
        repository = "eerovil/x" if number == 9 else "eerovil/agentdeck"
        cache.offer(
            f"pr-ref:v1:{repository}:{number}",
            GitHubMetadata({"state": "open", "isDraft": draft}),
        )
    job = Job(
        id="j",
        command=["true"],
        cwd=str(tmp_path),
        log_path="",
        submitted_at=0.0,
        session_key=key,
        work_id=WORK,
        purpose=VERIFICATION,
    )

    assert chat_waits_on_proof(db, job, cache) is True
    ready = GitHubMetadata({"state": "open", "isDraft": False})
    cache.offer("pr-ref:v1:eerovil/agentdeck:7", ready)
    # Another Work's draft and another chat's pull request say nothing about this one.
    assert chat_waits_on_proof(db, job, cache) is False
    # A draft the chat registered with no Work may be this one's: it wakes the chat.
    db.record_session_pull_request(key, PullRequestRef("eerovil/agentdeck", 10))
    cache.offer("pr-ref:v1:eerovil/agentdeck:10", GitHubMetadata(_DRAFT))
    assert chat_waits_on_proof(db, job, cache) is True


def test_a_jobs_notice_follows_its_chat_to_a_leg_added_after_it(tmp_path):
    """#4556 review: a chat continued on a new session after the job was queued hears of it
    there; a leg that was already there when the job was queued is history (#1501)."""
    import time

    from agentdeck.db import Db
    from agentdeck.jobs import Job
    from agentdeck.runtime import job_chat_leg

    db = Db(tmp_path / "control.db")
    first, second, third = (f"claude_code:main:{name}" for name in ("a", "b", "c"))
    db.append_chat_leg(first, second)

    def job(session_key: str | None, at: float) -> Job:
        return Job(
            id="j",
            command=["true"],
            cwd=str(tmp_path),
            log_path="",
            submitted_at=at,
            session_key=session_key,
        )

    now = time.time()
    # Queued after the chat moved to its second leg, from the first one handed back.
    assert job_chat_leg(db, job(first, now + 60)) == first
    assert job_chat_leg(db, job(second, now)) == second
    # Queued before the chat moved on: the notice follows it.
    assert job_chat_leg(db, job(first, now - 60)) == second
    db.append_chat_leg(second, third)
    assert job_chat_leg(db, job(first, now - 60)) == third
    # A session no chat names, and a job with no chat at all.
    assert job_chat_leg(db, job("claude_code:main:lone", 0.0)) == "claude_code:main:lone"
    assert job_chat_leg(db, job(None, 0.0)) == ""


async def test_a_green_run_whose_note_cannot_be_kept_is_silent_as_before(tmp_path):
    """#4306: a note store that fails leaves the proved run as it was: done, and no wake."""
    from test_jobs import _queue as queue_for
    from test_jobs import _settle, _wait_for

    told: list[str] = []

    async def notify(job, message):
        told.append(message)
        return True

    queue = queue_for(tmp_path, notify=notify)
    queue._keep_note = lambda job, note_id, text: (_ for _ in ()).throw(OSError("disk"))
    await queue.start()
    try:
        job = queue.submit(
            command=[sys.executable, "-c", "print('ok')"],
            cwd=str(_git_repo(tmp_path / "repo")),
            session_key="claude_code:main:sid",
            purpose=VERIFICATION,
            work_id=WORK,
        )
        await _settle(queue, job.id)
        await _wait_for(lambda: queue.get(job.id).notified, what="the queue to be done with it")
    finally:
        await queue.stop()
    assert told == []


async def test_a_green_run_finished_before_a_restart_stays_silent_after_it(tmp_path):
    from test_jobs import _queue as queue_for
    from test_jobs import _settle, _wait_for

    told: list[str] = []

    async def notify(job, message):
        told.append(message)
        return True

    before = queue_for(tmp_path)  # nothing to tell it with, so the row is still owed
    await before.start()
    job = before.submit(
        command=[sys.executable, "-c", "print('ok')"],
        cwd=str(_git_repo(tmp_path / "repo")),
        session_key="claude_code:main:sid",
        purpose=VERIFICATION,
        work_id=WORK,
        progress=True,
    )
    await _settle(before, job.id)
    await before.stop()

    after = queue_for(tmp_path, notify=notify)
    await after.start()
    try:
        await _wait_for(lambda: after.get(job.id).notified, what="the queue to be done with it")
        assert after.get(job.id).progress is True, "the choice survives the reload"
    finally:
        await after.stop()

    assert told == []


def test_the_submitter_is_told_a_green_run_sends_no_message():
    work = {"purpose": VERIFICATION, "work_id": WORK}

    assert "sends no message" in cli._job_announcement(work, progress=False)
    assert "sends no message" not in cli._job_announcement(
        {**work, "run_id": "gen-1"}, progress=False
    )
    assert "sends no message" not in cli._job_announcement({"work_id": WORK}, progress=False)
    assert "every five minutes" not in cli._job_announcement({}, progress=False)
    assert "every five minutes" in cli._job_announcement({}, progress=True)


async def test_an_ordinary_job_pays_for_no_second_reading(tmp_path):
    job = await _run_one(tmp_path, command=[sys.executable, "-c", "print('ok')"])

    assert job.verified_workspace is None


def test_continuing_a_leg_linked_before_2881_copies_the_chats_run(tmp_path):
    """Review of #2882: a leg linked before #2881 has no Run of its own, so a Continue from
    it copies the Run its chat last served; a chain past the cap copies nothing."""
    from agentdeck.db import Db
    from agentdeck.run_execution import MAX_CONTINUATIONS

    db = Db(tmp_path / "control.db")
    db.bind_session_run("codex:main:leg0", WORK, "gen-1", delivery_id="poller:d1")

    def continue_to(index: int) -> None:
        token = f"{index:032d}"
        assert db.reserve_chat_successor(token, f"codex:main:leg{index - 1}") == "new"
        db.record_session_token(token, f"codex:main:leg{index}")

    def forget_copies() -> None:  # what legs linked before #2881 look like
        db._write("DELETE FROM session_run_bindings WHERE session_key != 'codex:main:leg0'")

    continue_to(1)
    forget_copies()
    continue_to(2)
    copied = db.latest_session_run("codex:main:leg2")
    assert (copied.work_id, copied.run_id, copied.first_delivery_id) == (
        WORK, "gen-1", "poller:d1"
    )

    for index in range(3, MAX_CONTINUATIONS + 1):
        continue_to(index)
    forget_copies()
    continue_to(MAX_CONTINUATIONS + 1)  # its source is MAX_CONTINUATIONS legs from leg0
    assert db.latest_session_run(f"codex:main:leg{MAX_CONTINUATIONS + 1}") is None
    db.close()


async def test_a_chat_in_no_run_names_the_one_work_it_joined(tmp_path):
    """#2895: a Work the deck releases has no Run, so its chat's verification proves the Work."""
    from agentdeck.models import IssueRef

    app = _deck(tmp_path)
    state = app.state.app_state
    state.bind_session_token("tok-1", "claude_code:main:s1")
    state.add_issue("claude_code:main:s1", IssueRef.parse("eerovil/agentdeck#2154"))

    body = await _by_token(app, "tok-1")

    assert (body["work_id"], body["run_id"]) == (WORK, "")
