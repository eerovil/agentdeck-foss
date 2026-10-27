"""A Run's verification evidence names every repository it tested (#2148).

#2154 made a verification job the one thing that proves a Run's work, and recorded the one
checkout the job ran in. A Work can span several repositories, so the job now records one
``WorkspaceSnapshot`` per repository, keyed by the repository rather than by a path, and the
engine holds every one of them to the same rule. There is no snapshot-set entity: the
evidence is the mapping itself, stored on the job row that produced it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import replace

import pytest

from agentdeck import __main__ as cli
from agentdeck.jobs import VERIFICATION, Job, JobError
from agentdeck.poller_engine.verification import (
    canonical,
    compare,
    credited_head,
    describe,
)
from agentdeck.poller_engine.verification import snapshots_after as after_of
from agentdeck.poller_engine.verification import snapshots_tested as before_of
from agentdeck.poller_engine.workspace import (
    WorkspaceSnapshot,
    collect_workspace_snapshot,
    repository_snapshots,
)
from agentdeck.work_workspace import build_work_workspace, run_verifications

WORK = "github_issue:eerovil/agentdeck#2148"
RUN = "gen-1"
_EMPTY = hashlib.sha256(b"").hexdigest()
A = "o/a"
B = "o/b"


_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        env={**os.environ, **_IDENTITY},
    )


def _repo(path, remote: str | None):
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    (path / "a.txt").write_text("one\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "one")
    return path


def _commit(path, text: str):
    (path / "a.txt").write_text(text)
    _git(path, "commit", "-q", "-am", text.strip())


def _snap(repository: str, head: str, *, root: str = "", **fields) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        root=root or f"/w/{repository.rsplit('/', 1)[-1]}",
        head=head,
        staged_sha256=_EMPTY,
        tracked_diff_sha256=_EMPTY,
        untracked_sha256=_EMPTY,
        repository=repository,
        **fields,
    )


def _keyed(*snapshots: WorkspaceSnapshot) -> dict[str, dict]:
    return {key: item.as_record() for key, item in repository_snapshots(snapshots).items()}


def _job(*snapshots: WorkspaceSnapshot, after=None, **overrides) -> dict:
    """A finished verification row as the queue writes it; the first snapshot is its cwd."""
    after = snapshots if after is None else after
    job = {
        "id": "job-1",
        "purpose": VERIFICATION,
        "work_id": WORK,
        "run_id": RUN,
        "state": "done",
        "result_class": "success",
        "submitted_at": 1.0,
        "tested_workspace": snapshots[0].as_record(),
        "verified_workspace": after[0].as_record() if after else None,
        "tested_workspaces": _keyed(*snapshots),
        "verified_workspaces": _keyed(*after) if after else None,
    }
    job.update(overrides)
    return job


async def _run(tmp_path, *, cwd, workspaces=(), purpose=VERIFICATION, command=None):
    from test_jobs import _queue, _settle

    queue = _queue(tmp_path)
    await queue.start()
    try:
        job = queue.submit(
            command=command or [sys.executable, "-c", "print('ok')"],
            cwd=str(cwd),
            purpose=purpose,
            work_id=WORK if purpose else "",
            run_id=RUN if purpose else "",
            workspaces=workspaces,
        )
        return await _settle(queue, job.id), queue
    finally:
        await queue.stop()


# --- 1, 3: what the queue records -------------------------------------------------------


async def test_a_one_repository_verification_keeps_2154s_rule(tmp_path):
    repo = _repo(tmp_path / "a", "git@github.com:o/a.git")

    job, _ = await _run(tmp_path, cwd=repo)
    row = job.to_dict()

    tested = before_of(row)
    assert list(tested) == [A]
    assert tested[A].snapshot_id == row["tested_workspace"]["snapshot_id"]
    assert credited_head(row, run_id=RUN, work_id=WORK) == row["tested_workspace"]["head"]


async def test_a_two_repository_verification_records_both_by_repository(tmp_path):
    a = _repo(tmp_path / "a", "https://github.com/o/a")
    b = _repo(tmp_path / "b", "git@github.com:o/b.git")
    _commit(b, "b4\n")

    job, _ = await _run(tmp_path, cwd=a, workspaces=[str(b)])
    row = job.to_dict()

    tested, after = before_of(row), after_of(row)
    assert set(tested) == {A, B}
    assert (
        tested[B].head
        == subprocess.check_output(["git", "-C", str(b), "rev-parse", "HEAD"], text=True).strip()
    )
    assert {key: item.snapshot_id for key, item in after.items()} == {
        key: item.snapshot_id for key, item in tested.items()
    }
    # The head credited is the one in the directory the job ran in.
    assert credited_head(row, run_id=RUN, work_id=WORK) == tested[A].head


async def test_an_edit_to_the_other_repository_mid_run_credits_nothing(tmp_path):
    a = _repo(tmp_path / "a", "https://github.com/o/a")
    b = _repo(tmp_path / "b", "https://github.com/o/b")
    edit = f"import pathlib; pathlib.Path({str(b / 'a.txt')!r}).write_text('two\\n')"

    job, _ = await _run(tmp_path, cwd=a, workspaces=[str(b)], command=[sys.executable, "-c", edit])

    assert job.state == "done"
    assert credited_head(job.to_dict(), run_id=RUN, work_id=WORK) == ""


# --- 2, 6: only an explicit verification of this exact Run ------------------------------


async def test_an_ordinary_job_cannot_name_other_workspaces_or_become_evidence(tmp_path):
    from test_jobs import _queue

    a = _repo(tmp_path / "a", "https://github.com/o/a")
    b = _repo(tmp_path / "b", "https://github.com/o/b")
    with pytest.raises(JobError, match="only a verification job"):
        _queue(tmp_path).submit(command=["true"], cwd=str(a), workspaces=[str(b)])

    (tmp_path / "plain").mkdir()
    job, _ = await _run(tmp_path / "plain", cwd=a, purpose="")
    assert job.tested_workspaces is None and job.verified_workspaces is None
    # Even a row carrying evidence proves nothing without the explicit purpose.
    forged = _job(_snap(A, "a1"), purpose="")
    assert credited_head(forged, run_id=RUN, work_id=WORK) == ""
    assert run_verifications([forged], WORK) == ()


def test_evidence_counts_only_for_its_exact_work_and_run():
    job = _job(_snap(A, "a1"), _snap(B, "b4"))

    assert credited_head(job, run_id=RUN, work_id=WORK) == "a1"
    assert credited_head(job, run_id="gen-0", work_id=WORK) == ""
    assert credited_head(job, run_id=RUN, work_id="github_issue:o/a#1") == ""
    assert [item.job_id for item in run_verifications([job], WORK)] == ["job-1"]
    assert run_verifications([job], "github_issue:o/a#1") == ()
    assert run_verifications([{**job, "work_id": ""}], WORK) == ()


def test_a_remote_lane_cannot_claim_to_have_tested_another_checkout(tmp_path):
    from test_jobs import _queue

    a = _repo(tmp_path / "a", "https://github.com/o/a")
    b = _repo(tmp_path / "b", "https://github.com/o/b")
    with pytest.raises(JobError, match="--where local"):
        _queue(tmp_path).submit(
            command=["pytest"],
            cwd=str(a),
            lane="kube",
            purpose=VERIFICATION,
            run_id=RUN,
            workspaces=[str(b)],
        )


# --- 4, 5, 9, 10, 11: comparing evidence ------------------------------------------------


def test_repository_order_does_not_matter():
    a1, b4 = _snap(A, "a1"), _snap(B, "b4")

    first, second = repository_snapshots((a1, b4)), repository_snapshots((b4, a1))

    assert canonical(first) == canonical(second)
    assert compare(first, second).verified


def test_a_later_workspace_a2_b4_is_not_the_evidence_a1_b4():
    recorded = repository_snapshots((_snap(A, "a1"), _snap(B, "b4")))
    later = repository_snapshots((_snap(A, "a2"), _snap(B, "b4")))

    assert canonical(recorded) != canonical(later)
    check = compare(recorded, later)
    assert not check.verified
    assert check.stale == (A,)


def test_another_hosts_root_does_not_change_the_identity(tmp_path):
    here = _repo(tmp_path / "here", "https://github.com/o/a")
    subprocess.run(
        ["git", "clone", "-q", str(here), str(tmp_path / "there")], check=True, capture_output=True
    )
    _git(tmp_path / "there", "remote", "set-url", "origin", "git@github.com:o/a.git")

    one = collect_workspace_snapshot(str(here))
    other = collect_workspace_snapshot(str(tmp_path / "there"))

    assert one.root != other.root
    assert one.snapshot_id != other.snapshot_id
    assert one.comparison == other.comparison
    assert compare(repository_snapshots((one,)), repository_snapshots((other,))).verified


def test_matching_evidence_verifies_and_missing_or_mismatched_does_not():
    recorded = repository_snapshots((_snap(A, "a1"), _snap(B, "b4")))

    assert compare(recorded, dict(recorded)).verified
    missing = compare(recorded, repository_snapshots((_snap(A, "a1"),)))
    assert missing.missing == (B,) and not missing.verified
    assert not compare({}, {}).verified
    # Required repositories the evidence never covered are missing, not quietly skipped.
    partial = repository_snapshots((_snap(A, "a1"),))
    assert compare(partial, recorded, required=[A, B]).missing == (B,)


def test_a_job_whose_second_reading_lost_a_repository_credits_nothing():
    a1, b4 = _snap(A, "a1"), _snap(B, "b4")

    assert credited_head(_job(a1, b4, after=(a1,)), run_id=RUN, work_id=WORK) == ""
    assert (
        credited_head(_job(a1, b4, after=(a1, _snap(B, "b5"))), run_id=RUN, work_id=WORK)
        == ""
    )
    dirty = replace(b4, tracked_diff_sha256="dirty")
    assert credited_head(_job(a1, dirty), run_id=RUN, work_id=WORK) == ""


def test_two_differing_checkouts_of_one_repository_are_not_one_answer():
    keyed = repository_snapshots((_snap(A, "a1", root="/x"), _snap(A, "a9", root="/y")))

    assert list(keyed) == [A]
    assert not keyed[A].provable
    assert any("another checkout" in reason for reason in keyed[A].unprovable)


# --- 7, 8: unresolved and unprovable ------------------------------------------------------


async def test_an_unresolved_repository_makes_multi_repository_evidence_unprovable(tmp_path):
    a = _repo(tmp_path / "a", "https://github.com/o/a")
    loose = _repo(tmp_path / "loose", None)

    job, _ = await _run(tmp_path, cwd=a, workspaces=[str(loose)])
    row = job.to_dict()

    tested = before_of(row)
    unresolved = next(key for key in tested if key != A)
    assert unresolved.startswith("unresolved:")
    assert credited_head(row, run_id=RUN, work_id=WORK) == ""
    check = compare(tested, tested)
    assert [key for key, _ in check.unprovable] == [unresolved]
    assert "no repository remote" in dict(check.unprovable)[unresolved]


def test_each_repositorys_own_unprovable_reason_is_kept():
    a1 = _snap(A, "a1")
    b4 = _snap(B, "b4", unprovable=("untracked files",))
    current = repository_snapshots((a1, _snap(B, "b4", unprovable=("tracked diff",))))

    check = compare(repository_snapshots((a1, b4)), current)

    assert not check.verified
    assert check.unprovable == ((B, ("tested: untracked files", "current: tracked diff")),)
    assert credited_head(_job(a1, b4), run_id=RUN, work_id=WORK) == ""


# --- 12, 13: old rows and history --------------------------------------------------------


def test_an_old_row_with_one_tested_workspace_reads_as_nothing():
    old = {
        "id": "old",
        "command": ["pytest"],
        "cwd": "/w",
        "log_path": "/w/log",
        "submitted_at": 1.0,
        "state": "done",
        "result_class": "success",
        "purpose": VERIFICATION,
        "work_id": WORK,
        "run_id": RUN,
        "tested_workspace": _snap("", "h1", root="/w").as_record(),
        "verified_workspace": _snap("", "h1", root="/w").as_record(),
    }

    job = Job.from_dict(old)
    assert job is not None and job.tested_workspaces is None and job.workspaces == []
    row = job.to_dict()
    assert before_of(row) == {}
    assert credited_head(row, run_id=RUN, work_id=WORK) == ""


async def test_recorded_evidence_outlives_the_worktree_and_later_work(tmp_path):
    a = _repo(tmp_path / "a", "https://github.com/o/a")
    b = _repo(tmp_path / "b", "https://github.com/o/b")
    job, queue = await _run(tmp_path, cwd=a, workspaces=[str(b)], command=["true"])
    recorded = before_of(job.to_dict())
    before = canonical(recorded)

    _commit(a, "a2\n")
    shutil.rmtree(b)
    from test_jobs import _queue

    reloaded_queue = _queue(tmp_path)
    reloaded_queue.load()
    reloaded = reloaded_queue.get(job.id)

    assert reloaded is not None
    assert canonical(before_of(reloaded.to_dict())) == before
    now = repository_snapshots((collect_workspace_snapshot(str(a)),))
    check = compare(recorded, now, required=[A, B])
    assert check.stale == (A,) and check.missing == (B,)


# --- 14: compact rendering ---------------------------------------------------------------


def test_the_cli_and_the_log_name_each_repository_at_its_head():
    job = _job(_snap(A, "a1a1a1a1a1"), _snap(B, "b4b4b4b4b4"))

    assert describe(before_of(job)) == "o/a@a1a1a1a, o/b@b4b4b4b"
    job.update(command=["pytest"], label="")
    assert cli._job_line(job).endswith("[tested o/a@a1a1a1a, o/b@b4b4b4b]")


def test_the_work_workspace_shows_each_runs_verification_compactly(tmp_path):
    from test_web import _app_with_state
    from test_work_workspace import WORK as WORK_REF

    from agentdeck.web.work_workspace_view import render_work_workspace

    work = WORK_REF.work_id
    green = _job(_snap(A, "a1a1a1a1a1"), _snap(B, "b4b4b4b4b4"), work_id=work, id="job-green")
    dirty = _job(
        _snap(A, "a2a2a2a2a2"),
        replace(_snap(B, "b4b4b4b4b4"), tracked_diff_sha256="dirty"),
        work_id=work,
        id="job-dirty",
        submitted_at=2.0,
    )
    verifications = run_verifications([green, dirty], work)
    assert [item.status for item in verifications] == ["unproven", "verified"]

    workspace = build_work_workspace(WORK_REF, verifications=verifications)
    html = render_work_workspace(_app_with_state(tmp_path).state.templates, workspace)

    assert 'class="ww-verification is-verified" data-ww-verification="job-green"' in html
    assert 'class="ww-verification is-unproven" data-ww-verification="job-dirty"' in html
    assert "o/a@<code>a1a1a1a</code>" in html and "o/b@<code>b4b4b4b</code>" in html
    assert 'class="ww-tested is-problem" data-ww-tested="o/b"' in html
    assert workspace.as_json()["verifications"][1]["tested"][B]


def test_a_keyed_record_that_leaves_out_the_credited_checkout_credits_nothing():
    a1, b4 = _snap(A, "a1"), _snap(B, "b4")
    job = _job(a1, b4)
    job["tested_workspaces"] = job["verified_workspaces"] = _keyed(b4)

    assert credited_head(job, run_id=RUN, work_id=WORK) == ""


def test_required_repositories_are_matched_regardless_of_case():
    recorded = repository_snapshots((_snap(A, "a1"),))

    assert compare(recorded, recorded, required=[A.upper()]).verified


def test_only_a_provable_github_repository_names_a_checkout(tmp_path):
    named = collect_workspace_snapshot(str(_repo(tmp_path / "n", "git@github.com:Owner/Repo.git")))
    elsewhere = collect_workspace_snapshot(str(_repo(tmp_path / "e", "https://gitlab.com/o/r.git")))

    assert named.repository == "owner/repo"
    assert elsewhere.repository == "" and elsewhere.repository_key.startswith("unresolved:")
    # The existing identity is untouched: still a digest of the normalized remote.
    assert elsewhere.repo_identity and len(elsewhere.repo_identity) == 64


async def test_a_runs_evidence_survives_the_queue_forgetting_the_job(tmp_path):
    """Review of #2182: the queue drops a finished row a day later, and the evidence must not
    go with it."""
    from test_jobs import _Clock, _queue, _settle

    from agentdeck.jobs import FINISHED_RETENTION_S

    a = _repo(tmp_path / "a", "https://github.com/o/a")
    b = _repo(tmp_path / "b", "https://github.com/o/b")
    clock = _Clock()
    queue = _queue(tmp_path, clock=clock)
    await queue.start()
    try:
        job = queue.submit(
            command=[sys.executable, "-c", "print('ok')"],
            cwd=str(a),
            purpose=VERIFICATION,
            work_id=WORK,
            run_id=RUN,
            workspaces=[str(b)],
        )
        job = await _settle(queue, job.id)
    finally:
        await queue.stop()
    recorded = canonical(before_of(job.to_dict()))

    clock.now += FINISHED_RETENTION_S + 60
    _commit(a, "a2\n")
    shutil.rmtree(b)
    later = _queue(tmp_path, clock=clock)
    later.load()
    later._prune(clock())

    assert later.get(job.id) is None  # the queue has forgotten it
    rows = later.evidence(work_id=WORK)
    assert [row["id"] for row in rows] == [job.id]
    assert canonical(before_of(rows[0])) == recorded
    [shown] = run_verifications(rows, WORK)
    assert shown.verified and [name for name, _ in shown.tested] == [A, B]
    assert later.evidence(work_id="github_issue:o/a#1") == []


def test_an_evidence_line_is_recorded_once_and_a_torn_line_is_skipped(tmp_path):
    from agentdeck.run_evidence import RunEvidenceLedger

    ledger = RunEvidenceLedger(tmp_path / "evidence")
    row = _job(_snap(A, "a1"))
    assert ledger.record(row) and ledger.record(row)
    with ledger.path(WORK).open("a") as handle:
        handle.write('{"id": "torn"')
    assert ledger.record({**row, "id": "job-2"})
    assert [item["id"] for item in ledger.rows(work_id=WORK)] == ["job-1", "job-2"]
    assert credited_head(ledger.rows(work_id=WORK)[-1], run_id=RUN, work_id=WORK) == "a1"


def test_no_amount_of_other_evidence_evicts_a_runs_lines(tmp_path, monkeypatch):
    """Review of #2182: retention follows the Work, never a global count."""
    from agentdeck import run_evidence
    from agentdeck.run_evidence import RunEvidenceLedger

    # Durability is not what this counts, and a disk flush per line took seconds.
    monkeypatch.setattr(run_evidence.os, "fsync", lambda _fd: None)
    ledger = RunEvidenceLedger(tmp_path / "evidence")
    ours = _job(_snap(A, "a1"), _snap(B, "b4"))
    assert ledger.record(ours)
    other = "github_issue:o/busy#1"
    for index in range(50):
        assert ledger.record({**ours, "id": f"busy-{index}", "work_id": other})

    assert [row["id"] for row in ledger.rows(work_id=WORK)] == ["job-1"]
    assert len(ledger.rows(work_id=other)) == 50
    assert len(ledger.rows()) == 51


async def test_a_verification_whose_evidence_cannot_be_stored_is_not_finished(tmp_path):
    """Review of #2182: evidence is part of completing a verification job, so a failed write
    is a failed completion -- not announced, not credited, and recovered as interrupted."""
    from test_jobs import _queue, _wait_for

    from agentdeck.run_evidence import RunEvidenceLedger

    class _Refusing(RunEvidenceLedger):
        def record(self, row):
            return False

    told: list[str] = []

    async def notify(job, _message):
        told.append(job.id)
        return True

    a = _repo(tmp_path / "a", "https://github.com/o/a")
    queue = _queue(tmp_path, notify=notify)
    queue._evidence = _Refusing(tmp_path / "jobs" / "run_evidence")
    await queue.start()
    try:
        job = queue.submit(
            command=[sys.executable, "-c", "print('ok')"],
            cwd=str(a),
            session_key="s:1",
            purpose=VERIFICATION,
            work_id=WORK,
            run_id=RUN,
        )
        await _wait_for(
            lambda: job.id not in queue._running and queue.get(job.id).started_at,
            what="the run to end",
        )
    finally:
        await queue.stop()

    assert queue.get(job.id).state == "running"
    assert told == []
    assert credited_head(queue.get(job.id).to_dict(), run_id=RUN, work_id=WORK) == ""
    recovered = _queue(tmp_path)
    recovered.load()
    assert recovered.get(job.id).state != "done"


# --- rolling versions: a runtime older than the CLI (review of #2182) ---------------------


class _SubmitArgs:
    def __init__(self, cwd, workspaces):
        self.command = ["--", sys.executable, "-c", "print('ok')"]
        self.cwd = str(cwd)
        self.where = "local"
        self.wait = False
        self.label = ""
        self.session = None
        self.session_token = "tok-1"
        self.verification = True
        self.workspace = [str(path) for path in workspaces]
        self.work = ""
        self.max_minutes = None


def _cli(monkeypatch, *, records: bool, row: dict):
    calls: dict[str, list] = {"submit": [], "cancel": []}

    def submit_job(_socket, **kwargs):
        calls["submit"].append(kwargs)
        return dict(row)

    def cancel_job(_socket, job_id):
        calls["cancel"].append(job_id)
        return {**row, "state": "cancelled"}

    monkeypatch.setattr(cli, "submit_job", submit_job)
    monkeypatch.setattr(cli, "cancel_job", cancel_job)
    monkeypatch.setattr(cli, "records_other_workspaces", lambda _socket: records)
    monkeypatch.setattr(
        cli,
        "_resolve_session_token",
        lambda _token: {"session_key": "s:1", "work_id": WORK, "run_id": RUN},
    )
    return calls


def _old_runtime_row(tmp_path) -> dict:
    """What a runtime from before #2148 returns: purpose and Run kept, workspaces dropped."""
    return {"id": "old-1", "log_path": str(tmp_path / "log"), "purpose": VERIFICATION}


def test_a_runtime_that_cannot_record_other_workspaces_is_never_sent_the_job(monkeypatch, tmp_path):
    from agentdeck.job_client import JobClientError

    calls = _cli(monkeypatch, records=False, row=_old_runtime_row(tmp_path))
    with pytest.raises(JobClientError, match="was not queued"):
        cli._job_submit(None, tmp_path / "sock", _SubmitArgs(tmp_path, [tmp_path]))
    assert calls["submit"] == []


def test_a_row_that_comes_back_without_its_workspaces_is_cancelled(monkeypatch, tmp_path):
    """The runtime changed between the check and the submit, so the row it took is
    cancelled. Finished or cancelled, a row with no repository-keyed record credits nothing
    (#2214); the cancel stops a suite nobody can use from running."""
    from agentdeck.job_client import JobClientError

    calls = _cli(monkeypatch, records=True, row=_old_runtime_row(tmp_path))
    with pytest.raises(JobClientError, match="cancelled"):
        cli._job_submit(None, tmp_path / "sock", _SubmitArgs(tmp_path, [tmp_path / "b"]))
    assert calls["cancel"] == ["old-1"]

    a1 = _snap(A, "a1").as_record()
    partial = {
        "id": "old-1",
        "purpose": VERIFICATION,
        "work_id": WORK,
        "run_id": RUN,
        "result_class": "success",
        "tested_workspace": a1,
        "verified_workspace": a1,
    }
    assert credited_head({**partial, "state": "done"}, run_id=RUN, work_id=WORK) == ""
    assert credited_head({**partial, "state": "cancelled"}, run_id=RUN, work_id=WORK) == ""


def test_a_current_runtime_keeps_the_job(monkeypatch, tmp_path):
    row = {**_old_runtime_row(tmp_path), "workspaces": [str(tmp_path / "b")]}
    calls = _cli(monkeypatch, records=True, row=row)

    assert cli._job_submit(None, tmp_path / "sock", _SubmitArgs(tmp_path, [tmp_path / "b"])) == 0
    assert calls["cancel"] == [] and len(calls["submit"]) == 1


@pytest.mark.parametrize(("status", "expected"), [(200, True), (404, False)])
def test_the_capability_check_reads_the_evidence_route(monkeypatch, status, expected):
    import httpx

    from agentdeck import job_client

    seen: list[str] = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(status, json={"evidence": []} if status == 200 else {})

    monkeypatch.setattr(
        job_client,
        "_client",
        lambda _socket: httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://agentdeck-runtime"
        ),
    )
    assert job_client.records_other_workspaces(None) is expected
    assert seen == ["/job-evidence"]
