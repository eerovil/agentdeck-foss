"""A Run proves its work with an explicit verification job (#2154).

The retired verify phase used to bind a job to a card through a checkpoint. What replaces it
is smaller: the job says it is verification, the deck names the Run of the chat that submitted
it, and the queue records what the job tested. The engine's side of that — which job credits
which commit — is covered in ``test_poller_engine_cycle``; this file covers the submission.
"""

from __future__ import annotations

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

    assert "will not count as release evidence" in capsys.readouterr().err


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
