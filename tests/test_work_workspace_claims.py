"""Work-owned repository workspace claims (#2149).

The worktree belongs to the Work: every Run, provider, account, review and verification
of it resolves the same checkout, only the Work finishing may give that checkout back,
and at any moment exactly one owner may delete it. These tests run against real Git
checkouts; only ``fetch`` is skipped, because a test must never reach a remote.
"""

from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentdeck import work_workspace_claims as claims
from agentdeck.poller_adapter import work_workspaces as adapter_claims
from agentdeck.poller_engine import github as engine_github
from agentdeck.poller_engine.ports import WorktreeCleanup
from agentdeck.run_evidence import RunEvidenceLedger

WORK = "github_issue:example/store#7"
OTHER_WORK = "github_issue:example/store#8"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _checkout(tmp_path: Path, name: str, slug: str) -> Path:
    """A clone with an ``origin/master`` whose remote names a GitHub repository."""
    origin = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "master", str(origin)], check=True)
    seed = tmp_path / f"{name}-seed"
    subprocess.run(["git", "init", "-q", "-b", "master", str(seed)], check=True)
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    (seed / "file.txt").write_text("one\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "one")
    _git(seed, "push", "-q", str(origin), "master")
    repo = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "remote", "set-url", "origin", f"https://github.com/{slug}.git")
    return repo


def _offline(args, **kwargs):
    """``subprocess.run`` with every fetch answered locally."""
    if "fetch" in args:
        return subprocess.CompletedProcess(args, 0, "", "")
    return subprocess.run(args, **kwargs)


class CountingHost(claims.GitHost):
    def __init__(self) -> None:
        super().__init__(runner=_offline)
        self.created: list[str] = []

    def create(self, checkout: str, path: str, branch: str, base_ref: str) -> str:
        self.created.append(path)
        return super().create(checkout, path, branch, base_ref)


@pytest.fixture
def store(tmp_path):
    made = claims.ClaimStore(tmp_path / "state" / "agentdeck.sqlite3")
    yield made
    made.close()


@pytest.fixture
def host():
    return CountingHost()


@pytest.fixture
def service(store, host):
    return claims.WorkWorkspaceService(store, host=host)


@pytest.fixture
def checkout(tmp_path):
    return _checkout(tmp_path, "store", "example/store")


def _request(checkout: Path, name: str = "issue-7", **fields) -> claims.EnsureRequest:
    values = {
        "work_id": WORK,
        "repository": "example/store",
        "purpose": claims.PURPOSE_IMPLEMENTATION,
        "checkout": str(checkout),
        "path": str(checkout / ".worktrees" / name),
        "branch": f"agent/{name}",
        "base_ref": "master",
        "legacy_paths": (),
        **fields,
    }
    return claims.EnsureRequest(**values)


def _registered(checkout: Path) -> list[str]:
    listed = _git(checkout, "worktree", "list", "--porcelain")
    return [line.removeprefix("worktree ") for line in listed.splitlines() if "worktree " in line]


# --- ensure and reuse -------------------------------------------------------------


def test_the_first_ensure_reserves_creates_and_confirms_one_claim(service, host, checkout):
    result = service.ensure(_request(checkout))

    assert result.outcome == claims.CREATED
    path = checkout / ".worktrees" / "issue-7"
    assert result.claim.path == str(path.resolve())
    assert result.claim.confirmed and result.claim.branch == "agent/issue-7"
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD") == "agent/issue-7"
    assert host.created == [str(path.resolve())]
    assert [claim.key for claim in service.store.active()] == [
        (WORK, "example/store", "implementation")
    ]


def test_every_later_consumer_of_the_work_resolves_the_same_workspace(service, host, checkout):
    """A new Run, provider, account, review or verification changes nothing about it."""
    first = service.ensure(_request(checkout, run_id="run-1"))
    for run in ("run-2", "run-3-on-another-account", "review-run", "verification-run"):
        again = service.ensure(_request(checkout, run_id=run))
        assert again.outcome == claims.REUSED
        assert again.claim.path == first.claim.path

    assert len(host.created) == 1
    assert len(service.store.active()) == 1
    assert service.claimed_paths() == [first.claim.path]


def test_a_later_request_naming_another_directory_still_gets_the_works_own(service, checkout):
    first = service.ensure(_request(checkout))
    later = service.ensure(
        _request(checkout, path=str(checkout / ".worktrees" / "issue-7-renamed"))
    )

    assert later.outcome == claims.REUSED
    assert later.claim.path == first.claim.path
    assert not (checkout / ".worktrees" / "issue-7-renamed").exists()


def test_one_work_holds_several_repository_claims(tmp_path, service, checkout):
    artifacts = _checkout(tmp_path, "artifacts", "example/agent-evidence")
    code = service.ensure(_request(checkout))
    evidence = service.ensure(
        _request(
            artifacts,
            name="issue-7-store-evidence",
            repository="example/agent-evidence",
            purpose=claims.PURPOSE_ARTIFACT,
        )
    )

    assert code.outcome == evidence.outcome == claims.CREATED
    assert {claim.purpose for claim in service.store.active(WORK)} == {
        "implementation",
        "artifact",
    }


def test_two_works_can_never_be_handed_one_directory(service, checkout):
    service.ensure(_request(checkout))
    with pytest.raises(claims.ClaimError, match="already claimed"):
        service.ensure(_request(checkout, work_id=OTHER_WORK))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("work_id", ""),
        ("repository", "store"),
        ("purpose", "scratch"),
        ("branch", ""),
        ("path", "/elsewhere/issue-7"),
        ("path", "relative/.worktrees/issue-7"),
    ],
)
def test_a_request_that_could_name_the_wrong_directory_is_refused(service, checkout, field, value):
    with pytest.raises(claims.ClaimError):
        service.ensure(_request(checkout, **{field: value}))
    assert service.store.active() == []


def test_concurrent_ensures_from_two_processes_converge_on_one_claim(tmp_path, checkout):
    """Two services on one database stand in for two processes; neither lock is shared."""
    db = tmp_path / "state" / "agentdeck.sqlite3"
    hosts = [CountingHost(), CountingHost()]
    services = [
        claims.WorkWorkspaceService(claims.ClaimStore(db), host=hosts[0]),
        claims.WorkWorkspaceService(claims.ClaimStore(db), host=hosts[1]),
    ]
    barrier = threading.Barrier(2)
    results: list[claims.EnsureResult] = []

    def run(service: claims.WorkWorkspaceService) -> None:
        barrier.wait()
        results.append(service.ensure(_request(checkout)))

    threads = [threading.Thread(target=run, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(services[0].store.active()) == 1
    assert sum(len(item.created) for item in hosts) == 1  # only one ran Git
    usable = [item for item in results if item.usable]
    assert usable and {item.claim.path for item in usable} == {services[0].store.active()[0].path}
    # The loser either saw the finished worktree or was told to wait; never a second one.
    assert {item.outcome for item in results} <= {
        claims.CREATED,
        claims.REUSED,
        claims.UNCERTAIN,
    }
    assert len(_registered(checkout)) == 2  # the checkout itself and one worktree


# --- preparation failure and uncertainty -------------------------------------------


class FailingHost(CountingHost):
    def __init__(self, *, raises: bool = False) -> None:
        super().__init__()
        self.raises = raises

    def create(self, checkout: str, path: str, branch: str, base_ref: str) -> str:
        self.created.append(path)
        if self.raises:
            raise TimeoutError("git hung")
        return "fatal: invalid reference: origin/nowhere"


def test_a_clean_preparation_failure_leaves_no_claim_behind(store, checkout):
    service = claims.WorkWorkspaceService(store, host=FailingHost())
    result = service.ensure(_request(checkout))

    assert result.outcome == claims.FAILED and "invalid reference" in result.detail
    assert store.active() == []
    # Nothing on the host, so the next ensure starts over cleanly.
    retry = claims.WorkWorkspaceService(store, host=CountingHost()).ensure(_request(checkout))
    assert retry.outcome == claims.CREATED


def test_an_unknown_creation_outcome_never_gets_a_second_candidate(store, checkout):
    uncertain = FailingHost(raises=True)
    result = claims.WorkWorkspaceService(store, host=uncertain).ensure(_request(checkout))
    assert result.outcome == claims.UNCERTAIN
    assert not result.claim.confirmed

    # A fresh reservation belongs to whoever made it: nobody else runs Git for it, and a
    # different directory is not offered either.
    host = CountingHost()
    again = claims.WorkWorkspaceService(store, host=host).ensure(
        _request(checkout, path=str(checkout / ".worktrees" / "issue-7-b"))
    )
    assert again.outcome == claims.UNCERTAIN
    assert host.created == []
    assert len(store.active()) == 1


def test_an_interrupted_creation_is_resumed_at_the_same_path_once_stale(store, checkout):
    claims.WorkWorkspaceService(store, host=FailingHost(raises=True)).ensure(_request(checkout))
    claim = store.active()[0]
    old = (datetime.now(UTC) - timedelta(seconds=claims.CREATION_STALE_S + 60)).isoformat()
    store._update("UPDATE work_workspace_claims SET reserved_at = ?", (old,))

    host = CountingHost()
    result = claims.WorkWorkspaceService(store, host=host).ensure(_request(checkout))

    assert result.outcome == claims.REUSED and result.claim.confirmed
    assert host.created == [claim.path]  # the claimed path, not a new one


def test_a_worktree_that_exists_but_is_not_confirmed_is_confirmed_by_looking(store, checkout):
    path = checkout / ".worktrees" / "issue-7"
    claims.WorkWorkspaceService(store, host=FailingHost(raises=True)).ensure(_request(checkout))
    _git(checkout, "worktree", "add", "-q", str(path), "-b", "agent/issue-7", "origin/master")

    host = CountingHost()
    result = claims.WorkWorkspaceService(store, host=host).ensure(_request(checkout))

    assert result.outcome == claims.REUSED and result.claim.confirmed
    assert host.created == []


def test_a_foreign_directory_at_the_claimed_path_is_unsettled_not_replaced(service, checkout):
    first = service.ensure(_request(checkout))
    _git(checkout, "worktree", "remove", "--force", first.claim.path)
    Path(first.claim.path).mkdir()  # something that is not the worktree

    result = service.ensure(_request(checkout))

    assert result.outcome == claims.UNCERTAIN
    assert "not a example/store worktree" in result.detail
    assert len(service.store.active()) == 1


# --- legacy adoption -----------------------------------------------------------------


def _legacy_worktree(checkout: Path, name: str) -> Path:
    path = checkout / ".worktrees" / name
    _git(checkout, "worktree", "add", "-q", str(path), "-b", f"agent/{name}", "origin/master")
    return path


def test_a_healthy_poller_registered_worktree_is_adopted_not_recreated(service, host, checkout):
    path = _legacy_worktree(checkout, "issue-7")
    (path / "work.txt").write_text("in progress\n")

    result = service.ensure(_request(checkout, legacy_paths=(str(path),)))

    assert result.outcome == claims.ADOPTED
    assert result.claim.path == str(path.resolve()) and result.claim.confirmed
    assert host.created == []
    assert (path / "work.txt").read_text() == "in progress\n"


def test_ambiguous_legacy_registrations_stay_on_legacy_ownership(service, checkout):
    path = _legacy_worktree(checkout, "issue-7")
    other = _legacy_worktree(checkout, "issue-7-second")

    result = service.ensure(_request(checkout, legacy_paths=(str(path), str(other))))

    assert result.outcome == claims.LEGACY
    assert "left on legacy ownership" in result.detail
    assert service.store.active() == []


def test_an_unreadable_legacy_registration_is_never_permission_to_adopt(service, checkout):
    _legacy_worktree(checkout, "issue-7")

    result = service.ensure(_request(checkout, legacy_paths=None))

    assert result.outcome == claims.LEGACY
    assert service.store.active() == []


def test_a_registered_sibling_whose_origin_cannot_be_read_blocks_adoption(store, checkout):
    """Unreadable is unknown, not "another repository" (#2186 review)."""
    path = _legacy_worktree(checkout, "issue-7")
    sibling = str(_legacy_worktree(checkout, "issue-7-scratch").resolve())

    def origin_unreadable_there(args, **kwargs):
        if "get-url" in args and sibling in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal: unable to read config")
        return _offline(args, **kwargs)

    host = claims.GitHost(runner=origin_unreadable_there)
    seen = host.inspect(sibling)
    assert seen.exists and seen.registered and seen.repository == ""  # present, unproven
    service = claims.WorkWorkspaceService(store, host=host)

    result = service.ensure(_request(checkout, legacy_paths=(str(path), sibling)))

    assert result.outcome == claims.LEGACY
    assert "could not be proven" in result.detail
    assert store.active() == []


def test_a_present_but_unregistered_sibling_blocks_adoption(tmp_path, service, checkout):
    path = _legacy_worktree(checkout, "issue-7")
    stray = checkout / ".worktrees" / "issue-7-copy"
    stray.mkdir()

    result = service.ensure(_request(checkout, legacy_paths=(str(path), str(stray))))

    assert result.outcome == claims.LEGACY and "could not be proven" in result.detail


@pytest.mark.parametrize("registered", [(), ("elsewhere",)])
@pytest.mark.parametrize(
    "local_only",
    ["dirty", "untracked", "unpushed"],
)
def test_an_unregistered_worktree_holding_local_work_is_never_adopted(
    service, host, checkout, registered, local_only
):
    """Looking right is not ownership: a worktree the card never registered is taken over
    only when nothing in it exists solely there, since the Work's end removes it (#2816)."""
    path = _legacy_worktree(checkout, "issue-7")
    if local_only == "dirty":
        (path / "file.txt").write_text("changed\n")
    elif local_only == "untracked":
        (path / "new.txt").write_text("new\n")
    else:
        _git(path, "-c", "user.email=t@example.com", "-c", "user.name=T",
             "commit", "-q", "--allow-empty", "-m", "local")
    legacy = tuple(str(checkout / ".worktrees" / name) for name in registered)

    result = service.ensure(_request(checkout, legacy_paths=legacy))

    assert result.outcome == claims.LEGACY
    assert "never registered it" in result.detail and "`/claude`" in result.detail
    assert service.store.active() == [] and host.created == []
    assert path.exists()


def test_an_unregistered_clean_pushed_worktree_is_taken_over(service, host, checkout):
    """#2816: the card's own earlier chat made it after its Work ended, and every later
    dispatch was refused until a person removed it."""
    path = _legacy_worktree(checkout, "issue-7")
    _git(path, "-c", "user.email=t@example.com", "-c", "user.name=T",
         "commit", "-q", "--allow-empty", "-m", "pushed")
    _git(path, "update-ref", "refs/remotes/origin/agent/issue-7", "HEAD")

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.ADOPTED
    assert result.claim.path == str(path.resolve()) and host.created == []
    assert _git(path, "branch", "--show-current") == result.claim.branch == "agent/issue-7"


def test_a_taken_over_worktree_on_another_branch_is_put_on_the_cards_branch(
    service, host, checkout
):
    """#2816 review: choir-tracks-cf's was on `agent/issue-14-zoom`, already merged, while
    the claim and the brief named `agent/issue-14`."""
    path = _legacy_worktree(checkout, "issue-7-zoom")
    target = checkout / ".worktrees" / "issue-7"
    _git(checkout, "worktree", "move", str(path), str(target))

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.ADOPTED and host.created == []
    assert _git(target, "branch", "--show-current") == result.claim.branch == "agent/issue-7"
    assert _git(target, "rev-parse", "HEAD") == _git(checkout, "rev-parse", "origin/master")


def test_a_card_branch_only_origin_has_is_checked_out_with_its_commits(
    service, host, checkout
):
    """#2816 review: a remote-only card branch was recreated off the base, losing its commits."""
    path = _legacy_worktree(checkout, "issue-7-zoom")
    target = checkout / ".worktrees" / "issue-7"
    _git(checkout, "worktree", "move", str(path), str(target))
    _git(target, "-c", "user.email=t@example.com", "-c", "user.name=T",
         "commit", "-q", "--allow-empty", "-m", "card work")
    card_head = _git(target, "rev-parse", "HEAD")
    _git(target, "update-ref", "refs/remotes/origin/agent/issue-7", card_head)
    _git(target, "update-ref", "refs/remotes/origin/agent/issue-7-zoom", card_head)
    _git(target, "checkout", "-q", "--detach", "origin/master")
    _git(checkout, "branch", "-q", "-D", "agent/issue-7-zoom")

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.ADOPTED
    assert _git(target, "branch", "--show-current") == "agent/issue-7"
    assert _git(target, "rev-parse", "HEAD") == card_head


def _interrupted_takeover(store, checkout, *, minutes_ago: int) -> Path:
    """A takeover that reserved its claim and died before switching the branch."""
    path = _legacy_worktree(checkout, "issue-7-zoom")
    target = checkout / ".worktrees" / "issue-7"
    _git(checkout, "worktree", "move", str(path), str(target))
    at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    store.reserve(
        claims.WorkspaceClaim(
            WORK, "example/store", claims.PURPOSE_IMPLEMENTATION, path=str(target.resolve()),
            branch="agent/issue-7", base_ref="master", claimed_at=at,
        )
    )
    return target


def test_a_takeover_interrupted_before_its_switch_is_settled_on_resume(service, store, checkout):
    """#2816 review: the resume path confirmed the old branch as it stood."""
    target = _interrupted_takeover(store, checkout, minutes_ago=30)

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.REUSED and result.claim.confirmed
    assert _git(target, "branch", "--show-current") == "agent/issue-7"


def test_a_takeover_still_in_flight_elsewhere_is_left_alone(service, store, checkout):
    target = _interrupted_takeover(store, checkout, minutes_ago=0)

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.UNCERTAIN and not result.claim.confirmed
    assert _git(target, "branch", "--show-current") == "agent/issue-7-zoom"


def test_an_interrupted_takeover_that_gained_local_work_is_given_up(service, store, checkout):
    target = _interrupted_takeover(store, checkout, minutes_ago=30)
    (target / "new.txt").write_text("new\n")

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.LEGACY and "uncommitted changes" in result.detail
    assert store.active() == []
    assert _git(target, "branch", "--show-current") == "agent/issue-7-zoom"


def test_local_work_appearing_before_the_reservation_stops_a_takeover(store, checkout):
    """#2816 review: the clean check ran before reserving, and a worktree already on the
    card's branch was confirmed without it being repeated."""
    path = _legacy_worktree(checkout, "issue-7")

    class WorkArrives(CountingHost):
        checks = 0

        def unsaved(self, path):
            self.checks += 1
            answer = super().unsaved(path)
            (Path(path) / "new.txt").write_text("written in the gap\n")
            return answer

    host = WorkArrives()
    service = claims.WorkWorkspaceService(store, host=host)

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert host.checks == 2
    assert result.outcome == claims.LEGACY and "uncommitted changes" in result.detail
    assert store.active() == [] and (path / "new.txt").exists()


def test_a_crashed_takeover_on_its_branch_with_local_work_is_not_confirmed(
    service, store, checkout
):
    target = _interrupted_takeover(store, checkout, minutes_ago=30)
    _git(target, "switch", "-q", "-c", "agent/issue-7")
    (target / "new.txt").write_text("new\n")

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.LEGACY and store.active() == []


def test_a_taken_over_worktree_that_cannot_switch_branch_is_refused(service, checkout):
    path = _legacy_worktree(checkout, "issue-7-zoom")
    target = checkout / ".worktrees" / "issue-7"
    _git(checkout, "worktree", "move", str(path), str(target))
    # The card's branch is checked out elsewhere, so Git refuses to switch to it.
    _git(checkout, "branch", "agent/issue-7", "origin/master")
    _git(checkout, "worktree", "add", "-q", str(checkout.parent / "elsewhere"), "agent/issue-7")

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.LEGACY
    assert "could not be put on agent/issue-7" in result.detail
    assert service.store.active() == []
    assert _git(target, "branch", "--show-current") == "agent/issue-7-zoom"


@pytest.mark.parametrize("remote", ["upstream", "origin-stale"])
def test_a_commit_only_another_remote_or_a_pruned_ref_holds_is_not_pushed(
    tmp_path, store, checkout, remote
):
    """#2816 review: only origin, refreshed, proves the claimed repository holds it."""
    path = _legacy_worktree(checkout, "issue-7")
    _git(path, "-c", "user.email=t@example.com", "-c", "user.name=T",
         "commit", "-q", "--allow-empty", "-m", "local")
    if remote == "upstream":
        _git(path, "update-ref", "refs/remotes/upstream/agent/issue-7", "HEAD")
        runner = _offline
    else:
        # A real fetch with --prune drops a branch origin no longer has.
        origin = tmp_path / "store.git"
        _git(checkout, "remote", "set-url", "origin", str(origin))
        _git(path, "update-ref", "refs/remotes/origin/agent/issue-7", "HEAD")
        runner = subprocess.run
    host = claims.GitHost(runner=runner)
    host.inspect = lambda p: claims.HostWorktree(  # the local URL names no GitHub repo
        exists=True, registered=True, checkout=str(checkout.resolve()),
        repository="example/store",
    ) if Path(p).exists() else claims.HostWorktree(exists=False)
    service = claims.WorkWorkspaceService(store, host=host)

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.LEGACY and "not on any origin branch" in result.detail


def test_an_unfetchable_origin_is_never_proof(store, checkout):
    _legacy_worktree(checkout, "issue-7")

    def fetch_fails(args, **kwargs):
        if "fetch" in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal: unable to access")
        return subprocess.run(args, **kwargs)

    service = claims.WorkWorkspaceService(store, host=claims.GitHost(runner=fetch_fails))

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.LEGACY and "could not be fetched" in result.detail


def test_the_branch_switch_runs_only_under_this_callers_reservation(store, checkout):
    """#2816 review: switching before reserving let a losing caller move the winner's tree."""
    path = _legacy_worktree(checkout, "issue-7-zoom")
    target = checkout / ".worktrees" / "issue-7"
    _git(checkout, "worktree", "move", str(path), str(target))
    held: list[bool] = []

    class Watching(CountingHost):
        def switch(self, path, branch, base_ref):
            held.append(any(c.path == path for c in store.active()))
            return super().switch(path, branch, base_ref)

    service = claims.WorkWorkspaceService(store, host=Watching())

    assert service.ensure(_request(checkout, legacy_paths=())).outcome == claims.ADOPTED
    assert held == [True]


def test_an_unregistered_worktree_whose_state_cannot_be_read_is_not_adopted(store, checkout):
    path = _legacy_worktree(checkout, "issue-7")

    def status_fails(args, **kwargs):
        if "status" in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal")
        return _offline(args, **kwargs)

    service = claims.WorkWorkspaceService(store, host=claims.GitHost(runner=status_fails))

    result = service.ensure(_request(checkout, legacy_paths=()))

    assert result.outcome == claims.LEGACY and "could not be read" in result.detail
    assert path.exists()


def test_an_unregistered_clean_worktree_still_yields_to_a_competing_registration(
    service, checkout
):
    _legacy_worktree(checkout, "issue-7")
    other = _legacy_worktree(checkout, "issue-7-second")

    result = service.ensure(_request(checkout, legacy_paths=(str(other),)))

    assert result.outcome == claims.LEGACY and "also registers" in result.detail


def test_another_spelling_of_the_registered_target_still_counts(tmp_path, service, checkout):
    path = _legacy_worktree(checkout, "issue-7")
    link = tmp_path / "linked-checkout"
    link.symlink_to(checkout)

    result = service.ensure(
        _request(checkout, legacy_paths=(str(link / ".worktrees" / "issue-7"),))
    )

    assert result.outcome == claims.ADOPTED
    assert result.claim.path == str(path.resolve())


def test_a_sibling_that_is_gone_does_not_block_adoption(tmp_path, service, checkout):
    path = _legacy_worktree(checkout, "issue-7")
    gone = checkout / ".worktrees" / "issue-7-removed"

    result = service.ensure(_request(checkout, legacy_paths=(str(path), str(gone))))

    assert result.outcome == claims.ADOPTED


def test_a_legacy_worktree_of_another_repository_is_not_ambiguity(tmp_path, service, checkout):
    path = _legacy_worktree(checkout, "issue-7")
    artifacts = _checkout(tmp_path, "artifacts", "example/agent-evidence")
    evidence = _legacy_worktree(artifacts, "issue-7-store-evidence")

    result = service.ensure(_request(checkout, legacy_paths=(str(path), str(evidence))))

    assert result.outcome == claims.ADOPTED


def test_a_worktree_of_the_wrong_repository_at_the_path_is_refused(tmp_path, service):
    other = _checkout(tmp_path, "other", "example/other")
    _legacy_worktree(other, "issue-7")

    result = service.ensure(_request(other))

    assert result.outcome == claims.FAILED
    assert "example/other checkout" in result.detail


# --- release -------------------------------------------------------------------------


def test_a_spare_the_agent_runs_in_is_adopted_without_a_fetch_and_linked(service, checkout):
    """#3590: the agent started before its issue existed; its spare becomes the Work's."""
    spare = checkout / ".worktrees" / "work-spare-1a2b3c4d"
    assert claims.GitHost(runner=_offline).create_spare(str(checkout), str(spare), "master") == ""
    assert _git(spare, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # detached
    link = checkout / ".worktrees" / "issue-7"
    ran: list[list[str]] = []

    def watched(args, **kwargs):
        ran.append(list(args))
        return _offline(args, **kwargs)

    service.host = claims.GitHost(runner=watched)
    request = _request(checkout, path=str(spare), spare=True, link=str(link))

    result = service.ensure(request)

    assert result.outcome == claims.ADOPTED and result.claim.confirmed
    assert result.claim.path == str(spare.resolve())
    assert _git(spare, "rev-parse", "--abbrev-ref", "HEAD") == "agent/issue-7"
    assert link.is_symlink() and link.resolve() == spare.resolve()
    assert not any("fetch" in args for args in ran)
    # Asked again (a Run, a review), the Work gets the same spare back.
    assert service.ensure(_request(checkout)).claim.path == result.claim.path

    outcomes, _unclaimed = service.release(WORK)

    assert [item.released for item in outcomes] == [True]
    assert not spare.exists() and not link.is_symlink()


@pytest.mark.parametrize("case", ["not-a-spare", "on-a-branch", "changed"])
def test_a_spare_that_is_not_plainly_untouched_is_never_adopted(service, checkout, case):
    name = "issue-7" if case == "not-a-spare" else "work-spare-1a2b3c4d"
    spare = checkout / ".worktrees" / name
    assert claims.GitHost(runner=_offline).create_spare(str(checkout), str(spare), "master") == ""
    if case == "on-a-branch":
        _git(spare, "switch", "-qc", "someone-elses")
    if case == "changed":
        (spare / "file.txt").write_text("edited\n")

    result = service.ensure(_request(checkout, path=str(spare), spare=True))

    assert result.outcome == claims.FAILED and result.claim is None
    assert service.store.active() == []


def test_old_untaken_spares_are_swept_and_one_in_use_never_is(checkout):
    """#3590: a spare a restart or a refused filing left behind does not stay forever."""
    import os
    import time

    from agentdeck import work_start

    host = claims.GitHost(runner=_offline)
    made = {}
    for name in ("old", "new", "adopted", "edited"):
        made[name] = checkout / ".worktrees" / f"work-spare-{name}"
        assert host.create_spare(str(checkout), str(made[name]), "master") == ""
    _git(made["adopted"], "switch", "-qc", "agent/issue-9")
    (made["edited"] / "file.txt").write_text("edited\n")
    hour_ago = time.time() - work_start.SPARE_SWEEP_AGE_S - 60
    for name in ("old", "adopted", "edited"):
        os.utime(made[name], (hour_ago, hour_ago))

    work_start.sweep_spare_worktrees(str(checkout))

    assert [name for name, path in made.items() if path.exists()] == ["new", "adopted", "edited"]
    assert str(made["old"]) not in _registered(checkout)


def test_a_run_ending_releases_nothing(service, checkout):
    """No Run-level call exists that deletes; the next Run finds the same tree."""
    first = service.ensure(_request(checkout, run_id="run-1"))
    (Path(first.claim.path) / "commit-me.txt").write_text("x\n")

    second = service.ensure(_request(checkout, run_id="run-2"))

    assert second.claim.path == first.claim.path
    assert (Path(first.claim.path) / "commit-me.txt").exists()


def test_work_terminal_release_removes_it_and_keeps_run_evidence(tmp_path, service, checkout):
    ledger = RunEvidenceLedger(tmp_path / "run_evidence")
    assert ledger.record({"id": "job-1", "work_id": WORK, "tested_workspaces": {}})
    first = service.ensure(_request(checkout))

    outcomes, unclaimed = service.release(WORK)

    assert [(item.path, item.released) for item in outcomes] == [(first.claim.path, True)]
    assert unclaimed == []
    assert not Path(first.claim.path).exists()
    assert service.store.active() == []
    assert [row["id"] for row in ledger.rows(work_id=WORK)] == ["job-1"]


def test_a_dirty_workspace_keeps_the_release_pending_until_it_is_clean(service, checkout):
    first = service.ensure(_request(checkout))
    scratch = Path(first.claim.path) / "unsaved.txt"
    scratch.write_text("not committed anywhere\n")

    outcomes, _ = service.release(WORK)

    assert outcomes[0].released is False and "uncommitted" in outcomes[0].pending
    held = service.store.active(WORK)[0]
    assert held.release_pending and "uncommitted" in held.last_error
    assert scratch.exists()

    scratch.unlink()
    retried = service.release_pending()
    assert [item.released for item in retried] == [True]
    assert service.store.active() == []


def test_release_is_idempotent(service, checkout):
    first = service.ensure(_request(checkout))
    service.release(WORK)

    outcomes, unclaimed = service.release(WORK, [first.claim.path])

    assert outcomes == []
    assert unclaimed == [first.claim.path]  # nothing claims it now; its removal is a no-op
    assert service.release_pending() == []


def test_a_live_worker_in_the_tree_defers_its_release(store, checkout):
    service = claims.WorkWorkspaceService(store, host=CountingHost())
    first = service.ensure(_request(checkout))

    outcomes, _ = service.release(WORK, execution_active=lambda _path: True)

    assert outcomes[0].pending == "a live worker is running in it"
    assert Path(first.claim.path).exists()
    assert service.release_pending(execution_active=lambda _path: None)[0].released is False
    assert service.release_pending(execution_active=lambda _path: False)[0].released is True


def test_a_reopened_work_withdraws_a_pending_release(service, checkout):
    first = service.ensure(_request(checkout))
    (Path(first.claim.path) / "unsaved.txt").write_text("x\n")
    service.release(WORK)

    again = service.ensure(_request(checkout))

    assert again.outcome == claims.REUSED
    assert not again.claim.release_pending
    assert service.release_pending() == []


def test_the_named_paths_no_claim_holds_come_back_to_their_legacy_owner(
    tmp_path, service, checkout
):
    first = service.ensure(_request(checkout))
    legacy = str(tmp_path / "somewhere" / ".worktrees" / "issue-7-legacy")

    outcomes, unclaimed = service.release(WORK, [first.claim.path, legacy])

    assert [item.path for item in outcomes] == [first.claim.path]
    assert unclaimed == [legacy]


def test_the_works_end_releases_a_claim_the_card_never_recorded(tmp_path, service, checkout):
    """A delivery refused after the claim was made leaves no card record of it (#2149)."""
    artifacts = _checkout(tmp_path, "artifacts", "example/agent-evidence")
    code = service.ensure(_request(checkout))
    evidence = service.ensure(
        _request(
            artifacts,
            name="issue-7-store-evidence",
            repository="example/agent-evidence",
            purpose=claims.PURPOSE_ARTIFACT,
        )
    )

    outcomes, unclaimed = service.release(WORK, [code.claim.path])

    assert sorted(item.path for item in outcomes if item.released) == sorted(
        [code.claim.path, evidence.claim.path]
    )
    assert unclaimed == []
    assert service.store.active() == []


def test_another_works_claim_is_never_released_through_this_one(service, checkout):
    first = service.ensure(_request(checkout))

    outcomes, unclaimed = service.release(OTHER_WORK, [first.claim.path])

    assert outcomes == [] and unclaimed == []  # not this Work's, and not the caller's either
    assert Path(first.claim.path).exists()
    assert service.store.active(WORK)[0].path == first.claim.path


def test_stale_cleanup_for_one_card_never_deletes_another_works_worktree(
    monkeypatch, adapter, service, checkout
):
    """The engine hand-off end to end: a card record naming another Work's claimed path."""
    theirs = service.ensure(_request(checkout)).claim.path  # WORK's live worktree
    (Path(theirs) / "unsaved.txt").write_text("live work\n")

    def post(_socket, path, body, **_kwargs):
        assert path == claims.RELEASE_PATH
        outcomes, unclaimed = service.release(body["work_id"], body["paths"])
        return 200, {"released": [item.as_json() for item in outcomes], "unclaimed": unclaimed}

    monkeypatch.setattr(engine_github, "runtime_post", post)
    # Card #8 is OTHER_WORK; its stale record wrongly still names #7's worktree.
    result = adapter.release_worktrees("store", 8, [theirs])

    assert result.landed and result.retained == ()
    assert Path(theirs, "unsaved.txt").exists()
    (held,) = service.store.active()
    assert held.work_id == WORK and not held.release_pending


def test_read_claims_is_read_only_and_says_unknown_when_unreadable(tmp_path, service, checkout):
    first = service.ensure(_request(checkout))

    found = claims.read_claims(service.store.path, WORK)
    assert [claim.path for claim in found] == [first.claim.path]
    assert claims.read_claims(tmp_path / "missing.sqlite3", WORK) == []
    broken = tmp_path / "broken.sqlite3"
    broken.write_text("not a database")
    assert claims.read_claims(broken, WORK) is None


# --- exactly one destructive owner ---------------------------------------------------


@pytest.fixture
def adapter():
    made = object.__new__(engine_github.GitHubAdapter)
    made.org = "example"
    made.runtime_socket = "/run/agentdeck.sock"
    return made


def test_the_engine_hands_every_path_to_the_work_and_deletes_none_itself(
    monkeypatch, adapter, tmp_path
):
    """The Work is the one destructive owner (#2195): the card never removes a path itself.

    A path no Work claims that is still on disk is retained, which releases the card and says
    which directory was left for the reaper; one already gone needs nothing at all.
    """
    sent: list[dict] = []
    removed: list[list[str]] = []
    loose = tmp_path / ".worktrees" / "legacy"
    loose.mkdir(parents=True)
    gone = str(tmp_path / ".worktrees" / "gone")

    def post(_socket, path, body, **_kwargs):
        sent.append({"path": path, **body})
        return 200, {"released": [], "unclaimed": [str(loose), gone]}

    monkeypatch.setattr(engine_github, "runtime_post", post)
    monkeypatch.setattr(
        engine_github.GitHubAdapter,
        "remove_worktrees",
        staticmethod(lambda paths: removed.append(list(paths)) or WorktreeCleanup()),
    )

    result = adapter.release_worktrees("store", 7, ["/c/.worktrees/issue-7", str(loose), gone])

    assert result.landed
    assert sent == [
        {
            "path": claims.RELEASE_PATH,
            "work_id": WORK,
            "paths": ["/c/.worktrees/issue-7", str(loose), gone],
        }
    ]
    assert removed == []
    assert [path for path, _reason in result.retained] == [str(loose)]


@pytest.mark.parametrize("answer", [(0, None), (500, {"detail": "boom"}), (200, {"odd": 1})])
def test_an_unanswered_release_deletes_nothing_and_retries(monkeypatch, adapter, answer):
    removed: list[list[str]] = []
    monkeypatch.setattr(engine_github, "runtime_post", lambda *_a, **_k: answer)
    monkeypatch.setattr(
        engine_github.GitHubAdapter,
        "remove_worktrees",
        staticmethod(lambda paths: removed.append(list(paths)) or WorktreeCleanup()),
    )

    result = adapter.release_worktrees("store", 7, ["/c/.worktrees/issue-7"])

    assert result.failed == ("/c/.worktrees/issue-7",)
    assert removed == []


@pytest.mark.parametrize("socket", ["", "/run/agentdeck.sock"])
def test_a_runtime_without_claims_leaves_every_path_pending(monkeypatch, adapter, socket):
    """No socket, or a runtime from before claims: nothing here may delete, so nothing does."""
    removed: list[list[str]] = []
    adapter.runtime_socket = socket
    monkeypatch.setattr(engine_github, "runtime_post", lambda *_a, **_k: (404, None))
    monkeypatch.setattr(
        engine_github.GitHubAdapter,
        "remove_worktrees",
        staticmethod(lambda paths: removed.append(list(paths)) or WorktreeCleanup()),
    )

    result = adapter.release_worktrees("store", 7, ["/c/.worktrees/issue-7"])

    assert result.failed == ("/c/.worktrees/issue-7",)
    assert removed == []


def test_the_route_spellings_agree_across_the_three_sides():
    assert engine_github.WORK_WORKSPACE_ENSURE_PATH == claims.ENSURE_PATH
    assert engine_github.WORK_WORKSPACE_RELEASE_PATH == claims.RELEASE_PATH
    assert engine_github.WORK_WORKSPACE_CLAIMS_PATH == claims.CLAIMS_PATH
    assert adapter_claims.ENSURE_PATH == claims.ENSURE_PATH


# --- the runtime owns the service ------------------------------------------------------


@pytest.mark.anyio
async def test_the_runtime_routes_ensure_list_and_release_one_claim(tmp_path, checkout):
    from httpx import ASGITransport, AsyncClient

    from agentdeck.config import AppConfig
    from agentdeck.runtime import create_runtime_app

    config = AppConfig.model_validate(
        {
            "keyed_workers": {"state_dir": str(tmp_path)},
            "restart_nudge": {"file": str(tmp_path / "restart-nudge.json")},
            "history": {"db_path": str(tmp_path / "control.db")},
            "accounts": [],
        }
    )
    app = create_runtime_app(config)
    app.state.work_workspaces.host = CountingHost()  # no fetch reaches a remote
    body = {
        "work_id": WORK,
        "run_id": "gen-1",
        "repository": "example/store",
        "purpose": "implementation",
        "checkout": str(checkout),
        "path": str(checkout / ".worktrees" / "issue-7"),
        "branch": "agent/issue-7",
        "base_ref": "master",
        "legacy_paths": [],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        made = (await client.post(claims.ENSURE_PATH, json=body)).json()
        again = (await client.post(claims.ENSURE_PATH, json={**body, "run_id": "gen-2"})).json()
        listed = (await client.get(claims.CLAIMS_PATH)).json()
        refused = await client.post(claims.ENSURE_PATH, json={**body, "path": "/tmp/x"})
        released = (
            await client.post(
                claims.RELEASE_PATH,
                json={"work_id": WORK, "paths": [made["claim"]["path"], "/else/.worktrees/x"]},
            )
        ).json()

    assert made["outcome"] == "created" and again["outcome"] == "reused"
    assert again["claim"]["path"] == made["claim"]["path"]
    assert [row["path"] for row in listed["claims"]] == [made["claim"]["path"]]
    assert refused.status_code == 422
    assert released == {
        "released": [{"path": made["claim"]["path"], "released": True, "pending": ""}],
        "unclaimed": ["/else/.worktrees/x"],
    }
    assert not Path(made["claim"]["path"]).exists()
    app.state.work_workspaces.store.close()
    app.state.budget_db.close()


@pytest.mark.anyio
async def test_the_release_waits_on_the_works_run_execution_not_on_a_cwd(tmp_path, checkout):
    """#2159: whether a Work's workspace may be deleted is the Runtime's Run execution state.

    The worker here runs somewhere else entirely — its cwd matches nothing — so the retired
    cwd/`turn_active` reading would have deleted the worktree under it. Its Run is routed to
    this Work, and that is what holds the release until the Run is over.
    """
    from datetime import UTC, datetime

    from httpx import ASGITransport, AsyncClient

    from agentdeck.config import AppConfig
    from agentdeck.db import DeliveryRoute
    from agentdeck.runtime import create_runtime_app

    config = AppConfig.model_validate(
        {
            "keyed_workers": {"state_dir": str(tmp_path)},
            "restart_nudge": {"file": str(tmp_path / "restart-nudge.json")},
            "history": {"db_path": str(tmp_path / "control.db")},
            "accounts": [],
        }
    )
    app = create_runtime_app(config)
    app.state.work_workspaces.host = CountingHost()
    row = {
        "cwd": "/somewhere/else",
        "live": True,
        "turn_active": True,
        "effective_activity": True,
        "last_delivery_at": datetime.now(UTC).timestamp(),
        "last_activity_at": datetime.now(UTC).timestamp(),
        "work_id": WORK,
        "run_id": "gen-1",
    }

    class Host:
        def snapshot(self):
            return {"workers": {"store#7": row}}

    app.state.run_executions.hosts = lambda: {("claude_code", "main"): Host()}
    assert app.state.budget_db.freeze_delivery_route(
        DeliveryRoute(
            "poller:d1", "f", WORK, "gen-1", "claude_code", "main", "store#7",
            datetime.now(UTC).isoformat(),
        )
    )
    body = {
        "work_id": WORK,
        "run_id": "gen-1",
        "repository": "example/store",
        "purpose": "implementation",
        "checkout": str(checkout),
        "path": str(checkout / ".worktrees" / "issue-7"),
        "branch": "agent/issue-7",
        "base_ref": "master",
        "legacy_paths": [],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        made = (await client.post(claims.ENSURE_PATH, json=body)).json()
        held = (
            await client.post(claims.RELEASE_PATH, json={"work_id": WORK, "paths": []})
        ).json()
        kept = Path(made["claim"]["path"]).exists()
        row.update(turn_active=False, effective_activity=False, live=False)
        # The Run is over: the host holds no process, so it died or was stopped.
        row["last_delivery_at"] = 1.0
        freed = (
            await client.post(claims.RELEASE_PATH, json={"work_id": WORK, "paths": []})
        ).json()

    assert made["outcome"] == "created", made
    assert held["released"][0]["released"] is False
    assert "live worker" in held["released"][0]["pending"]
    assert kept
    assert freed["released"][0]["released"] is True
    app.state.work_workspaces.store.close()
    app.state.budget_db.close()


@pytest.mark.anyio
async def test_without_execution_state_a_claimed_works_release_waits(
    tmp_path, checkout, monkeypatch
):
    """Review of #2189, round 4: a claimed Work's release does not fall back to the
    cwd/`turn_active` reading when Run execution state is unavailable. An idle-looking row
    after a restart proves nothing, so the release answers unknown and stays pending."""
    from httpx import ASGITransport, AsyncClient

    from agentdeck import run_execution
    from agentdeck.config import AppConfig
    from agentdeck.runtime import create_runtime_app

    def broken(*_args, **_kwargs):
        raise OSError("outcome store could not open")

    monkeypatch.setattr(run_execution, "OutcomeStore", broken)
    config = AppConfig.model_validate(
        {
            "keyed_workers": {"state_dir": str(tmp_path)},
            "restart_nudge": {"file": str(tmp_path / "restart-nudge.json")},
            "history": {"db_path": str(tmp_path / "control.db")},
            "accounts": [],
        }
    )
    app = create_runtime_app(config)
    assert app.state.run_executions is None
    app.state.work_workspaces.host = CountingHost()
    body = {
        "work_id": WORK,
        "run_id": "gen-1",
        "repository": "example/store",
        "purpose": "implementation",
        "checkout": str(checkout),
        "path": str(checkout / ".worktrees" / "issue-7"),
        "branch": "agent/issue-7",
        "base_ref": "master",
        "legacy_paths": [],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        made = (await client.post(claims.ENSURE_PATH, json=body)).json()
        held = (
            await client.post(claims.RELEASE_PATH, json={"work_id": WORK, "paths": []})
        ).json()

    assert held["released"][0]["released"] is False
    assert "could not be read" in held["released"][0]["pending"]
    assert Path(made["claim"]["path"]).exists()
    app.state.work_workspaces.store.close()
    app.state.budget_db.close()
