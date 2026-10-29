"""The read-only multi-repository Work workspace model (#2143)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdeck.db import Db
from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.models import IssueRef, Session, SessionStatus
from agentdeck.poller_engine.workspace import WorkspaceSnapshot, _digest, _normalize_remote
from agentdeck.poller_ops import BoardScope, BoardView
from agentdeck.state import AppState
from agentdeck.work import WorkRef, work_id_for
from agentdeck.work_workspace import (
    ProjectWorkspaceFacts,
    RepositoryRole,
    SessionWorkspaceFacts,
    WorkspaceEvidence,
    WorkspaceIssueKind,
    WorkspaceProbe,
    build_work_workspace,
    gather_work_workspace,
    parse_work_id,
)

WORK = WorkRef("eerovil/agentdeck", 2143)
HTTPS = "https://github.com/eerovil/agentdeck.git"


class FakeGit:
    """A git that answers from a table and counts every command it is asked."""

    def __init__(self) -> None:
        self.repos: dict[str, tuple[str, str, str, str]] = {}  # path -> top, head, ref, remote
        self.broken: dict[str, str] = {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.timeouts: list[float] = []
        self.slow = 0.0  # seconds each command "takes", on the patched clock
        self.clock = [0.0]

    def add(self, path: Path, remote: str = HTTPS, *, branch="agent/issue-2143", head="a" * 40):
        path.mkdir(parents=True, exist_ok=True)
        self.repos[str(path.resolve())] = (str(path.resolve()), head, branch, remote)
        return path

    def __call__(self, path: str, args, timeout: float) -> tuple[int, str]:
        self.calls.append((path, tuple(args)))
        self.timeouts.append(timeout)
        if self.slow:
            self.clock[0] += self.slow
        if path in self.broken:
            return 128, self.broken[path]
        found = next(
            (
                value
                for root, value in sorted(self.repos.items(), key=lambda kv: -len(kv[0]))
                if path == root or path.startswith(root + "/")
            ),
            None,
        )
        if found is None:
            return 128, "fatal: not a git repository"
        top, head, ref, remote = found
        if args[0] == "remote":
            return (0, remote + "\n") if remote else (2, "error: No such remote 'origin'")
        return 0, f"{top}\n{head}\n{ref}\n"


@pytest.fixture
def git():
    return FakeGit()


def _project(**fields) -> ProjectWorkspaceFacts:
    return ProjectWorkspaceFacts("agentdeck", **fields)


def _held(git, *paths, purpose="implementation", work=WORK):
    """The Work's claims on these paths (#2195: the only source of its worktrees).

    Each claim names the repository its checkout's remote does, as the runtime checked when it
    made or adopted it; a path git does not know is claimed for the Work's own repository.
    """
    from agentdeck.git_context import github_repository

    claims = []
    for path in paths:
        found = git.repos.get(str(Path(path).resolve()))
        remote = found[3] if found else ""
        repository = (github_repository(remote) or "eerovil/agentdeck").lower()
        claims.append(
            _claim(path, repository=repository, purpose=purpose, work_id=work.work_id)
        )
    return claims


def _leg(
    key: str, cwd, *, runs=(), chat="chat", git_context=None, exclusive=False
) -> SessionWorkspaceFacts:
    return SessionWorkspaceFacts(
        key, chat, str(cwd) if cwd else None, tuple(runs), git_context, exclusive
    )


def _kinds(workspace) -> list[str]:
    found = [issue.kind for issue in workspace.issues]
    for repo in workspace.repositories:
        found.extend(issue.kind for issue in repo.issues)
    return sorted(found)


def test_one_repository_is_one_compact_context(tmp_path, git):
    tree = git.add(tmp_path / "issue-2143")
    (tree / "src").mkdir()
    workspace = build_work_workspace(
        WORK,
        claims=_held(git, tree),
        sessions=[_leg("claude_code:main:w", tree / "src", runs=("gen-1",))],
        probe=WorkspaceProbe(git),
    )

    assert workspace.work_id == work_id_for(IssueRef("eerovil/agentdeck", 2143))
    (repo,) = workspace.repositories
    assert repo.repository == "eerovil/agentdeck"
    assert repo.roles == (RepositoryRole.SOURCE, RepositoryRole.IMPLEMENTATION)
    assert repo.branches == ("agent/issue-2143",)
    assert [tree.path for tree in repo.worktrees] == [str(tree.resolve())]
    (cwd,) = repo.cwds
    assert cwd.registered_worktree == str(tree.resolve())
    assert cwd.provenance.evidence is WorkspaceEvidence.RUN_SESSION
    assert cwd.provenance.run_id == "gen-1"
    assert workspace.primary == "eerovil/agentdeck"
    assert workspace.problems == ()
    evidence = {item.evidence for item in repo.provenance}
    assert {WorkspaceEvidence.WORK_CLAIM, WorkspaceEvidence.WORK_SOURCE} <= evidence


def test_several_code_repositories_are_kept_and_the_primary_is_ambiguous(tmp_path, git):
    ours = git.add(tmp_path / "a")
    theirs = git.add(tmp_path / "b", "git@github.com:eerovil/trailhead.git")
    workspace = build_work_workspace(
        WORK, claims=_held(git, ours, theirs), probe=WorkspaceProbe(git)
    )

    assert [repo.repository for repo in workspace.repositories] == [
        "eerovil/agentdeck",
        "eerovil/trailhead",
    ]
    assert workspace.primary == ""
    assert _kinds(workspace) == [WorkspaceIssueKind.AMBIGUOUS_PRIMARY]
    # Ambiguity is information, not a problem to fix.
    assert workspace.problems == ()


def test_the_artifact_repository_is_labelled_and_never_primary(tmp_path, git):
    code = git.add(tmp_path / "code")
    evidence = git.add(tmp_path / "evidence", "https://github.com/eerovil/agent-evidence")
    workspace = build_work_workspace(
        WORK,
        claims=_held(git, code, evidence),
        projects=[_project(artifact_repositories=("eerovil/agent-evidence",))],
        probe=WorkspaceProbe(git),
    )

    artifacts = workspace.repository("eerovil/agent-evidence")
    assert artifacts.roles == (RepositoryRole.ARTIFACT,)
    assert workspace.primary == "eerovil/agentdeck"
    assert _kinds(workspace) == []


def test_a_borrowed_checkout_keeps_source_and_code_repositories_apart(tmp_path, git):
    """Trailhead: `issues` holds the issues and the evidence; `docker` holds the code."""
    work = WorkRef("eerovil/issues", 42)
    code = git.add(tmp_path / "issue-42-issues", "https://github.com/eerovil/docker")
    evidence = git.add(tmp_path / "evidence", "https://github.com/eerovil/issues")
    workspace = build_work_workspace(
        work,
        claims=[*_held(git, code, work=work), *_held(git, evidence, purpose="artifact", work=work)],
        projects=[
            _project(
                artifact_repositories=("eerovil/issues",),
                borrowed_repository="eerovil/docker",
            )
        ],
        probe=WorkspaceProbe(git),
    )

    source = workspace.repository("eerovil/issues")
    docker = workspace.repository("eerovil/docker")
    assert source.roles == (RepositoryRole.SOURCE, RepositoryRole.ARTIFACT)
    assert docker.roles == (RepositoryRole.IMPLEMENTATION, RepositoryRole.BORROWED)
    assert workspace.primary == "eerovil/docker"


def test_several_worktrees_for_one_repository_are_all_kept(tmp_path, git):
    first = git.add(tmp_path / "one")
    second = git.add(tmp_path / "two", branch="other")
    workspace = build_work_workspace(
        WORK, claims=_held(git, first, second), probe=WorkspaceProbe(git)
    )

    (repo,) = workspace.repositories
    assert len(repo.worktrees) == 2
    assert repo.branches == ("agent/issue-2143", "other")
    assert _kinds(workspace) == [WorkspaceIssueKind.MULTIPLE_WORKTREES]


def test_a_missing_registered_path_is_a_diagnostic_and_nothing_else(tmp_path, git):
    gone = tmp_path / "gone"
    workspace = build_work_workspace(WORK, claims=_held(git, gone), probe=WorkspaceProbe(git))

    assert _kinds(workspace) == [WorkspaceIssueKind.MISSING_WORKTREE]
    (observation,) = workspace.repository("eerovil/agentdeck").worktrees
    assert observation.registered and not observation.observation.exists
    assert git.calls == []  # a missing directory is never shelled into
    assert not gone.exists()  # and nothing was created to "repair" it


def test_a_cwd_outside_the_registered_worktree_is_a_visible_conflict(tmp_path, git):
    tree = git.add(tmp_path / "issue-2143")
    main = git.add(tmp_path / "main-checkout", branch="master")
    workspace = build_work_workspace(
        WORK,
        claims=_held(git, tree),
        sessions=[
            _leg("claude_code:main:w", tree, runs=("gen-1",)),
            _leg("claude_code:main:review", main, chat="review"),
        ],
        probe=WorkspaceProbe(git),
    )

    (repo,) = workspace.repositories
    assert {cwd.cwd for cwd in repo.cwds} == {str(tree.resolve()), str(main.resolve())}
    assert _kinds(workspace) == [
        WorkspaceIssueKind.CWD_OUTSIDE_WORKTREE,
        WorkspaceIssueKind.MULTIPLE_WORKTREES,
    ]
    outside = next(i for i in repo.issues if i.kind is WorkspaceIssueKind.CWD_OUTSIDE_WORKTREE)
    assert outside.path == str(main.resolve())


def test_a_chat_resumed_in_its_repository_after_its_worktree_went_is_no_conflict(
    tmp_path, git
):
    """#2524: once the registered worktree is gone, its chat runs in the repository."""
    gone = tmp_path / "issue-2143"
    main = git.add(tmp_path / "main-checkout", branch="master")
    workspace = build_work_workspace(
        WORK,
        claims=_held(git, gone),
        sessions=[_leg("claude_code:main:w", main, runs=("gen-1",))],
        probe=WorkspaceProbe(git),
    )

    assert _kinds(workspace) == [WorkspaceIssueKind.MISSING_WORKTREE]


def test_shared_execution_without_a_registered_worktree_is_valid(tmp_path, git):
    shared = git.add(tmp_path / "agentdeck", branch="master")
    workspace = build_work_workspace(
        WORK, sessions=[_leg("claude_code:main:c", shared)], probe=WorkspaceProbe(git)
    )

    (repo,) = workspace.repositories
    assert repo.cwds[0].provenance.evidence is WorkspaceEvidence.CHAT_SESSION
    assert workspace.problems == ()


def test_repository_identity_never_comes_from_a_directory_name(tmp_path, git):
    no_remote = git.add(tmp_path / "agentdeck", remote="")
    elsewhere = git.add(tmp_path / "x", remote="https://gitlab.com/eerovil/agentdeck.git")
    plain = tmp_path / "eerovil-agentdeck"
    plain.mkdir()
    workspace = build_work_workspace(
        WORK,
        sessions=[
            _leg("k:a:1", no_remote),
            _leg("k:a:2", elsewhere),
            _leg("k:a:3", plain),
        ],
        probe=WorkspaceProbe(git),
    )

    (repo,) = workspace.repositories  # only the Work source
    assert repo.cwds == ()
    assert len(workspace.unresolved_observations) == 3
    assert _kinds(workspace) == [
        WorkspaceIssueKind.UNREADABLE_GIT,
        WorkspaceIssueKind.UNRESOLVED_REPOSITORY,
        WorkspaceIssueKind.UNRESOLVED_REPOSITORY,
    ]


def test_unreadable_git_state_is_reported(tmp_path, git):
    tree = git.add(tmp_path / "broken")
    git.broken[str(tree.resolve())] = "fatal: bad object HEAD"
    workspace = build_work_workspace(WORK, claims=_held(git, tree), probe=WorkspaceProbe(git))

    (issue,) = workspace.repository("eerovil/agentdeck").issues
    assert issue.kind is WorkspaceIssueKind.UNREADABLE_GIT
    assert "bad object HEAD" in issue.detail


def test_remote_spellings_normalize_to_one_repository(tmp_path, git):
    https = git.add(tmp_path / "one", "https://github.com/EeroVil/AgentDeck.git")
    ssh = git.add(tmp_path / "two", "git@github.com:eerovil/agentdeck")
    workspace = build_work_workspace(
        WORK,
        sessions=[_leg("k:a:1", https), _leg("k:a:2", ssh)],
        probe=WorkspaceProbe(git),
    )

    assert [repo.repository for repo in workspace.repositories] == ["eerovil/agentdeck"]


def test_a_remote_disagreeing_with_git_context_or_snapshot_is_reported(tmp_path, git):
    tree = git.add(tmp_path / "tree")
    context = GitContext(repository="eerovil/trailhead", branch="b", dirty=False)
    snapshot = WorkspaceSnapshot(
        root=str(tree.resolve()), repo_identity=_digest(_normalize_remote("github.com/x/y"))
    )
    workspace = build_work_workspace(
        WORK,
        sessions=[_leg("k:a:1", tree, git_context=context)],
        probe=WorkspaceProbe(git),
        snapshots={str(tree): snapshot},
    )

    assert _kinds(workspace) == [
        WorkspaceIssueKind.REMOTE_MISMATCH,
        WorkspaceIssueKind.REMOTE_MISMATCH,
    ]
    matching = WorkspaceSnapshot(
        root=str(tree.resolve()),
        repo_identity=_digest(_normalize_remote(HTTPS)),
        base_ref="origin/master",
    )
    clean = build_work_workspace(
        WORK,
        sessions=[_leg("k:a:1", tree)],
        probe=WorkspaceProbe(git),
        snapshots={str(tree): matching},
    )
    assert _kinds(clean) == []
    assert clean.repositories[0].base_refs == ("origin/master",)


def test_detached_head_is_shown_but_is_not_a_problem(tmp_path, git):
    tree = git.add(tmp_path / "tree", branch="HEAD", head="b" * 40)
    workspace = build_work_workspace(WORK, claims=_held(git, tree), probe=WorkspaceProbe(git))

    (repo,) = workspace.repositories
    assert repo.branches == ()
    assert repo.heads == ("b" * 40,)
    assert repo.worktrees[0].observation.detached
    assert _kinds(workspace) == [WorkspaceIssueKind.DETACHED_HEAD]
    assert workspace.problems == ()


def test_pull_requests_attach_to_their_own_repository(tmp_path, git):
    tree = git.add(tmp_path / "tree")
    pull = PullRequestContext(
        "eerovil/agentdeck",
        2180,
        "t",
        "https://github.com/eerovil/agentdeck/pull/2180",
        "open",
        head_branch="agent/issue-2143",
        base_branch="master",
    )
    context = GitContext("eerovil/agentdeck", "agent/issue-2143", False, (pull,))
    workspace = build_work_workspace(
        WORK,
        claims=_held(git, tree),
        sessions=[_leg("k:a:1", tree, git_context=context, exclusive=True)],
        probe=WorkspaceProbe(git),
    )

    (repo,) = workspace.repositories
    (found,) = repo.pull_requests
    assert (found.number, found.status, found.base_branch) == (2180, "open", "master")
    assert {p.evidence for p in found.provenance} == {WorkspaceEvidence.GIT_CONTEXT}
    assert repo.base_refs == ("master",)


def test_each_path_is_inspected_once_and_the_pass_is_capped(tmp_path, git):
    tree = git.add(tmp_path / "tree")
    (tree / "sub").mkdir()
    legs = [_leg(f"k:a:{n}", tree / "sub") for n in range(5)]
    build_work_workspace(
        WORK, claims=_held(git, tree, tree), sessions=legs, probe=WorkspaceProbe(git)
    )
    # One rev-parse and one remote read for the worktree, and the same again for the
    # one distinct cwd: five legs in one subdirectory still cost one inspection.
    assert len(git.calls) == 4

    git.calls.clear()
    many = [git.add(tmp_path / f"t{n}") for n in range(4)]
    capped = build_work_workspace(
        WORK, claims=_held(git, *many), probe=WorkspaceProbe(git, limit=2)
    )
    assert len(git.calls) == 4
    assert capped.inspected_paths == 2
    assert _kinds(capped).count(WorkspaceIssueKind.NOT_INSPECTED) == 2


def test_the_json_shape_is_collection_shaped(tmp_path, git):
    tree = git.add(tmp_path / "tree")
    payload = build_work_workspace(
        WORK, claims=_held(git, tree, tmp_path / "gone"), probe=WorkspaceProbe(git)
    ).as_json()

    assert payload["work_id"] == "github_issue:eerovil/agentdeck#2143"
    assert isinstance(payload["repositories"], list)
    assert payload["repositories"][0]["roles"] == ["source", "implementation"]
    assert payload["unresolved_observations"] == []
    assert payload["repositories"][0]["issues"][0]["kind"] == "missing_worktree"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("github_issue:eerovil/agentdeck#2143", WORK),
        ("github_issue:EeroVil/AgentDeck#2143", WORK),
        ("github_issue:agentdeck#2143", None),
        ("github_issue:eerovil/agentdeck#x", None),
        ("github_issue:eerovil/agentdeck#12#3", None),
        ("github_issue:eerovil/agent deck#3", None),
        ("other:eerovil/agentdeck#1", None),
        ("", None),
    ],
)
def test_parse_work_id(raw, expected):
    assert parse_work_id(raw) == expected


# --- gathering from AppState: Work/Chat many-to-many isolation --------------


def _session(key: str, **fields) -> Session:
    account, _, session_id = key.rpartition(":")
    return Session(
        key=key, account_key=account, session_id=session_id, status=SessionStatus.IDLE, **fields
    )


@pytest.fixture
def state(tmp_path):
    db = Db(tmp_path / "control.db")
    try:
        yield AppState(db=db, budget_db=db)
    finally:
        db.close()


def test_a_chat_in_two_works_lends_each_only_its_own_run(tmp_path, state, git):
    other = WorkRef("eerovil/agentdeck", 9)
    tree_a = git.add(tmp_path / "issue-2143")
    tree_b = git.add(tmp_path / "issue-9")
    shared_old, shared_new = "claude_code:main:s-1", "claude_code:main:s-2"
    only_b = "claude_code:main:b-1"
    bystander = "claude_code:main:x-1"
    state.budget_db.ensure_session_chats((shared_old, only_b, bystander))
    state.budget_db.append_chat_leg(shared_old, shared_new)
    state.sessions = {
        shared_old: _session(shared_old, cwd=tree_a),
        shared_new: _session(shared_new, cwd=tree_b),
        only_b: _session(only_b, cwd=tree_b),
        bystander: _session(bystander, cwd=tree_a),  # a matching cwd is not membership
    }
    state.set_issues(
        shared_old, (IssueRef("eerovil/agentdeck", 2143), IssueRef("eerovil/agentdeck", 9))
    )
    state.set_issues(only_b, (IssueRef("eerovil/agentdeck", 9),))
    state.bind_session_run(shared_old, WORK.work_id, "gen-a")
    state.bind_session_run(shared_new, other.work_id, "gen-b")

    mine = gather_work_workspace(state, WORK).build(WorkspaceProbe(git))
    theirs = gather_work_workspace(state, other).build(WorkspaceProbe(git))

    (repo,) = mine.repositories
    assert [(c.session_key, c.provenance.run_id) for c in repo.cwds] == [(shared_old, "gen-a")]
    (repo,) = theirs.repositories
    assert sorted((c.session_key, c.provenance.run_id) for c in repo.cwds) == [
        (only_b, ""),
        (shared_new, "gen-b"),
    ]


def test_gather_reads_the_projects_manifest_roles(tmp_path, state, git):
    board = BoardView(
        scopes=(
            BoardScope(
                "trailhead",
                org="eerovil",
                issue_repositories=("eerovil/issues",),
                artifact_repositories=("eerovil/issues",),
                borrowed_workspaces=(("eerovil/issues", "eerovil/docker"),),
            ),
        ),
    )
    facts = gather_work_workspace(state, WorkRef("eerovil/issues", 42), board=board)
    (project,) = facts.projects
    assert (project.instance, project.borrowed_repository) == ("trailhead", "eerovil/docker")
    workspace = facts.build(WorkspaceProbe(git))
    docker = workspace.repository("eerovil/docker")
    assert RepositoryRole.BORROWED in docker.roles
    # A repository no project declares gets no roles.
    assert not gather_work_workspace(state, WorkRef("eerovil/other", 43), board=board).projects


# --- the routes and the rendered section ---------------------------------------


def _real_repo(path: Path, remote: str, branch: str = "agent/issue-2143") -> Path:
    import subprocess

    path.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", branch),
        ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"),
        ("remote", "add", "origin", remote),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


def workspace_app(tmp_path):
    """A deck whose Work has a code worktree, an artifact worktree, a registered path
    that is gone, and a second chat running outside the registered worktree."""
    from test_web import _app_with_state

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    code = _real_repo(tmp_path / "wt" / "issue-2143", "git@github.com:eerovil/agentdeck.git")
    main = _real_repo(tmp_path / "agentdeck", "https://github.com/eerovil/agentdeck", "master")
    evidence = _real_repo(
        tmp_path / "wt" / "issue-2143-evidence", "https://github.com/eerovil/agent-evidence"
    )
    worker, review = "claude_code:test:worker-1", "claude_code:test:review-1"
    state.update_session(_session(worker, cwd=code))
    state.update_session(_session(review, cwd=main))
    ref = IssueRef("eerovil/agentdeck", 2143)
    state.set_issues(worker, (ref,))
    state.set_issues(review, (ref,))
    # The Work's claims are the only source of its worktrees (#2195).
    from agentdeck.work_workspace_claims import ClaimStore

    store = ClaimStore(app.state.config.history.db_path)
    for claim in (
        _claim(code),
        _claim(evidence, repository="eerovil/agent-evidence", purpose="artifact"),
        _claim(tmp_path / "wt" / "issue-2143-gone", purpose="review"),
    ):
        store.reserve(claim)
        store.confirm(claim)
    store.close()
    board = BoardView(
        scopes=(
            BoardScope(
                "agentdeck",
                org="eerovil",
                issue_repositories=("eerovil/agentdeck",),
                artifact_repositories=("eerovil/agent-evidence",),
            ),
        ),
    )
    app.state.issues.read_board = lambda: board
    return app


SECTION_URL = "/__test__/work-workspace"  # served by Playwright, not by the deck


async def _section_html(app) -> str:
    from agentdeck.web.work_workspace_view import load_work_workspace, render_work_workspace

    workspace = await load_work_workspace(app, WORK)
    return render_work_workspace(app.state.templates, workspace)


@pytest.mark.asyncio
async def test_the_loader_builds_the_model_and_knows_an_unknown_work(tmp_path):
    from agentdeck.web.work_workspace_view import load_work_workspace

    app = workspace_app(tmp_path)
    assert await load_work_workspace(app, WorkRef("eerovil/agentdeck", 9999)) is None

    workspace = await load_work_workspace(app, WORK)
    assert [repo.repository for repo in workspace.repositories] == [
        "eerovil/agentdeck",
        "eerovil/agent-evidence",
    ]
    assert workspace.repository("eerovil/agent-evidence").roles == (RepositoryRole.ARTIFACT,)
    assert workspace.primary == "eerovil/agentdeck"
    assert {
        WorkspaceIssueKind.MISSING_WORKTREE,
        WorkspaceIssueKind.CWD_OUTSIDE_WORKTREE,
        WorkspaceIssueKind.MULTIPLE_WORKTREES,
    } <= set(_kinds(workspace))


def test_the_workspace_is_served_only_inside_the_work_page(tmp_path):
    """#2147's canonical Work routes are the workspace's only public surface."""
    from test_work_ui import _route_paths

    paths = _route_paths(workspace_app(tmp_path).routes)
    assert {path for path in paths if "works" in path} == {
        "/works",
        "/works/{work_id:path}",
        # Message this Work (#2218) sends a message; it serves no workspace.
        "/works/{work_id:path}/message",
        # The Work action route (#2237) performs an operator action; it serves no workspace.
        "/works/{work_id:path}/action",
        # Take over another deck's Work (#3834) reassigns its issue; no workspace.
        "/works/{work_id:path}/take-over",
        # Hide and Bring back (#2238) change this deck's display only; no workspace.
        "/works/{work_id:path}/visibility",
        "/works/{work_id:path}/star",
        "/api/works/{work_id:path}/star",
        "/api/works/{work_id:path}/map-layout",
        "/api/works/{work_id:path}/question",
        # Rename (#2328) stores this deck's own name for the Work; no workspace.
        "/works/{work_id:path}/title",
        # New chat (#2388) reads the workspace only to pick the new chat's folder; it serves
        # no workspace.
        "/works/{work_id:path}/new-chat",
        # New work (#2415) files a new issue; there is no Work, so no workspace, yet.
        "/works/new",
        "/works/new/guess-project",
        # The form keeping a Work spare (#3590) asks for a worktree before any Work exists.
        "/works/new/warm",
        # The New work receipt's chat link (#3590) redirects to a chat; no workspace.
        "/works/{work_id:path}/chat",
        "/works/seen",
        # The staging population report (#2250) reads the built overview; no workspace.
        "/api/works/filed",
        "/api/works/population",
        # What needs the owner (#3773) reads the same overview; no workspace.
        "/api/works/needs-you",
        # The status log (#2751) reads this deck's own record of status changes; no workspace.
        "/api/works/status-log",
        "/api/works/status-log/flaps",
        # The Diff fold (#2379) serves the checkouts' diff, never the workspace itself.
        "/partials/works/{work_id:path}/diff",
        # The Work page's own stream (#3228) sends the page's sections, the workspace's among
        # them: the same section the page renders, never the workspace on its own.
        "/events/works/{work_id:path}",
    }


@pytest.mark.asyncio
async def test_the_section_renders_stacked_repositories_with_problems_visible(tmp_path):
    html = await _section_html(workspace_app(tmp_path))

    assert 'id="work-workspace"' in html
    # Two repositories; every worktree is a claim naming its repository, so nothing is unknown.
    assert html.count('<article class="ww-repo') == 2
    assert 'data-ww-issue="missing_worktree"' in html
    assert 'data-ww-issue="cwd_outside_worktree"' in html
    # Session ids stay in the collapsed inspector, not in the compact view.
    compact, _, inspector = html.partition('<details class="ww-inspector">')
    repo_compact = compact.split('data-ww-repo="eerovil/agentdeck"', 1)[1]
    assert "worker-1" not in repo_compact and "review-1" not in repo_compact
    assert "worker-1" in inspector and "review-1" in inspector


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_section_fits_a_phone_screen(tmp_path):
    """The rendered section, swapped by HTMX into the real deck page and its stylesheet."""
    from conftest import async_playwright
    from test_web import _live_server

    app = workspace_app(tmp_path)
    html = await _section_html(app)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.route(
            base + SECTION_URL,
            lambda route: route.fulfill(status=200, content_type="text/html", body=html),
        )
        await page.goto(base + "/")
        await page.evaluate(
            """async (url) => {
                const host = document.createElement('div');
                host.id = 'ww-host';
                (document.querySelector('main') || document.body).prepend(host);
                await htmx.ajax('GET', url, {target: '#ww-host', swap: 'innerHTML'});
            }""",
            SECTION_URL,
        )
        section = page.locator("#work-workspace")
        await section.wait_for()
        assert await page.locator(".ww-repo").count() == 2
        assert await page.locator(".ww-issue.is-problem").first.is_visible()
        assert await page.locator(".ww-inspector").first.evaluate("el => el.open") is False
        overflow = await section.evaluate(
            """el => [...el.querySelectorAll('*')].filter(
                node => node.getBoundingClientRect().right > window.innerWidth + 1
            ).map(node => node.className || node.tagName)"""
        )
        assert overflow == []
        await browser.close()


def test_a_spent_time_budget_leaves_paths_uninspected(tmp_path, git):
    trees = [git.add(tmp_path / f"t{n}") for n in range(2)]
    workspace = build_work_workspace(
        WORK, claims=_held(git, *trees), probe=WorkspaceProbe(git, budget_s=0)
    )
    assert git.calls == []
    assert _kinds(workspace) == [WorkspaceIssueKind.NOT_INSPECTED] * 2


def test_provenance_is_ordered_strongest_first(tmp_path, git):
    tree = git.add(tmp_path / "tree")
    workspace = build_work_workspace(
        WORK,
        claims=_held(git, tree),
        sessions=[_leg("k:a:1", tree, runs=("g",)), _leg("k:a:2", tree)],
        probe=WorkspaceProbe(git),
    )
    order = [item.evidence for item in workspace.repositories[0].provenance]
    assert order == [
        WorkspaceEvidence.WORK_CLAIM,
        WorkspaceEvidence.WORK_SOURCE,
        WorkspaceEvidence.RUN_SESSION,
        WorkspaceEvidence.CHAT_SESSION,
    ]


def test_a_shared_chats_pull_requests_are_not_lent_to_every_work(tmp_path, git):
    """A chat's registered pull requests are per chat; a chat in two Works has both."""
    tree = git.add(tmp_path / "tree")
    other = PullRequestContext(
        "eerovil/agentdeck", 9, "t", "https://github.com/eerovil/agentdeck/pull/9", "open"
    )
    context = GitContext("eerovil/agentdeck", "b", False, (other,))
    shared = build_work_workspace(
        WORK,
        sessions=[_leg("k:a:1", tree, git_context=context)],
        probe=WorkspaceProbe(git),
    )
    assert shared.repositories[0].pull_requests == ()
    bound = build_work_workspace(
        WORK,
        sessions=[_leg("k:a:1", tree, git_context=context, runs=("g",))],
        probe=WorkspaceProbe(git),
    )
    assert [pull.number for pull in bound.repositories[0].pull_requests] == [9]


def test_no_command_outlasts_the_pass_budget(tmp_path, git, monkeypatch):
    """Each command's timeout is capped to what is left; the remote read is not spared."""
    import agentdeck.work_workspace as module

    monkeypatch.setattr(module.time, "monotonic", lambda: git.clock[0])
    git.slow = 1.5
    trees = [git.add(tmp_path / f"t{n}") for n in range(3)]
    workspace = build_work_workspace(
        WORK, claims=_held(git, *trees), probe=WorkspaceProbe(git, budget_s=3.5)
    )

    # t0: rev-parse (2.0 allowed) and remote (2.0). t1: rev-parse with 0.5 left, then
    # the budget is gone before its remote read, so t1 is not inspected; t2 never starts.
    assert git.timeouts == [2.0, 2.0, 0.5]
    worktrees = workspace.repositories[0].worktrees
    assert len(worktrees) == 3  # every claim is listed, inspected or not
    assert sum(tree.observation.inspected for tree in worktrees) == 1
    assert _kinds(workspace).count(WorkspaceIssueKind.NOT_INSPECTED) == 2
    assert WorkspaceIssueKind.UNRESOLVED_REPOSITORY not in _kinds(workspace)


def test_a_timed_out_remote_read_is_unreadable_not_remote_less(tmp_path):
    from agentdeck.work_workspace import TIMED_OUT

    def runner(path, args, timeout):
        if args[0] == "remote":
            return TIMED_OUT, "git remote timed out"
        return 0, f"{path}\n{'a' * 40}\nmain\n"

    tree = tmp_path / "tree"
    tree.mkdir()
    workspace = build_work_workspace(
        WORK, sessions=[_leg("k:a:1", tree, runs=("g",))], probe=WorkspaceProbe(runner)
    )
    assert _kinds(workspace) == [WorkspaceIssueKind.UNREADABLE_GIT]


def test_a_nested_checkout_of_another_repository_is_not_inside_the_worktree(tmp_path, git):
    tree = git.add(tmp_path / "issue-2143")
    nested = git.add(tree / "vendor" / "lib", "https://github.com/eerovil/lib")
    workspace = build_work_workspace(
        WORK,
        claims=_held(git, tree),
        sessions=[_leg("k:a:1", nested, runs=("g",))],
        probe=WorkspaceProbe(git),
    )

    lib = workspace.repository("eerovil/lib")
    (cwd,) = lib.cwds
    assert cwd.registered_worktree == ""
    assert [issue.kind for issue in lib.issues] == [WorkspaceIssueKind.NESTED_CHECKOUT]
    assert lib.issues[0].problem


def test_the_innermost_registered_worktree_of_the_same_repository_contains_the_cwd(tmp_path, git):
    outer = git.add(tmp_path / "outer", "https://github.com/eerovil/lib")
    inner = git.add(outer / "inner")
    (inner / "src").mkdir()
    workspace = build_work_workspace(
        WORK,
        claims=_held(git, outer, inner),
        sessions=[_leg("k:a:1", inner / "src", runs=("g",))],
        probe=WorkspaceProbe(git),
    )
    (cwd,) = workspace.repository("eerovil/agentdeck").cwds
    assert cwd.registered_worktree == str(inner.resolve())


def test_an_unfinished_read_keeps_what_git_context_already_knew(tmp_path, git):
    tree = git.add(tmp_path / "tree")
    context = GitContext("EeroVil/AgentDeck", "agent/issue-2143", False, head_sha="c" * 40)
    workspace = build_work_workspace(
        WORK,
        sessions=[_leg("k:a:1", tree, runs=("g",), git_context=context)],
        probe=WorkspaceProbe(git, budget_s=0),
    )

    assert git.calls == []
    assert workspace.unresolved_observations == ()
    (repo,) = workspace.repositories
    assert [cwd.session_key for cwd in repo.cwds] == ["k:a:1"]
    assert repo.branches == ("agent/issue-2143",)
    assert repo.heads == ("c" * 40,)
    assert WorkspaceEvidence.GIT_CONTEXT in {item.evidence for item in repo.provenance}
    # The unfinished read is still visible.
    assert _kinds(workspace) == [WorkspaceIssueKind.NOT_INSPECTED]


def test_a_remote_less_checkout_does_not_borrow_git_contexts_repository(tmp_path, git):
    """GitContext falls back to the issue's repository there, which is not an observation."""
    tree = git.add(tmp_path / "tree", remote="")
    context = GitContext("eerovil/agentdeck", "main", False)
    workspace = build_work_workspace(
        WORK,
        sessions=[_leg("k:a:1", tree, git_context=context)],
        probe=WorkspaceProbe(git),
    )
    assert len(workspace.unresolved_observations) == 1
    assert workspace.repositories[0].cwds == ()


def test_the_unknown_bucket_is_only_warning_styled_for_a_real_problem(tmp_path, git):
    """A vanished session cwd is history; a missing registered worktree is a problem."""
    from test_web import _app_with_state

    from agentdeck.web.work_workspace_view import render_work_workspace

    templates = _app_with_state(tmp_path).state.templates
    vanished = build_work_workspace(
        WORK, sessions=[_leg("k:a:1", tmp_path / "gone")], probe=WorkspaceProbe(git)
    )
    assert _kinds(vanished) == [WorkspaceIssueKind.MISSING_CWD]
    assert not vanished.unresolved_problem
    html = render_work_workspace(templates, vanished)
    assert 'class="ww-repo ww-unresolved"' in html
    assert "has-problems" not in html

    # A claimed worktree that is missing belongs to its repository, and is a problem there.
    missing = build_work_workspace(
        WORK, claims=_held(git, tmp_path / "gone-too"), probe=WorkspaceProbe(git)
    )
    assert WorkspaceIssueKind.MISSING_WORKTREE in _kinds(missing)
    assert 'data-ww-issue="missing_worktree"' in render_work_workspace(templates, missing)


def test_declared_repositories_appear_before_any_checkout_exists(tmp_path, git):
    workspace = build_work_workspace(
        WorkRef("eerovil/issues", 42),
        projects=[
            _project(
                artifact_repositories=("eerovil/Agent-Evidence",),
                borrowed_repository="eerovil/docker",
            )
        ],
        probe=WorkspaceProbe(git),
    )

    assert [repo.repository for repo in workspace.repositories] == [
        "eerovil/issues",
        "eerovil/agent-evidence",
        "eerovil/docker",
    ]
    artifacts = workspace.repository("eerovil/agent-evidence")
    docker = workspace.repository("eerovil/docker")
    assert artifacts.roles == (RepositoryRole.ARTIFACT,)
    assert docker.roles == (RepositoryRole.BORROWED,)
    for repo in (artifacts, docker):
        assert [p.evidence for p in repo.provenance] == [WorkspaceEvidence.POLLER_MANIFEST]
        assert repo.worktrees == () and repo.issues == ()
    assert workspace.primary == "eerovil/issues"
    assert git.calls == []


def _board_with(*scopes) -> BoardView:
    return BoardView(scopes=tuple(scopes), complete=False)


def test_unreadable_poller_state_is_unknown_not_absent(tmp_path, state):
    broken = BoardScope(
        "agentdeck",
        org="eerovil",
        issue_repositories=("eerovil/agentdeck",),
        state_readable=False,
    )
    facts = gather_work_workspace(state, WORK, board=_board_with(broken))
    assert facts.known  # no chat names it, but the project's roles could exist unread
    assert facts.poller_unavailable == "instance agentdeck: state unreadable"
    workspace = facts.build(WorkspaceProbe(FakeGit()))
    (issue,) = workspace.issues
    assert issue.kind is WorkspaceIssueKind.POLLER_STATE_UNAVAILABLE and issue.problem

    # An unreadable manifest declares nothing, so it cannot be ruled out either.
    unknown = BoardScope("mystery", readable=False)
    assert gather_work_workspace(state, WORK, board=_board_with(unknown)).poller_unavailable

    # Another owner's unreadable instance, or a complete read, is ordinary absence.
    other = BoardScope("shop", org="shop", issue_repositories=("shop/store",), state_readable=False)
    assert not gather_work_workspace(state, WORK, board=_board_with(other)).known
    readable = BoardScope("agentdeck", org="eerovil", issue_repositories=("eerovil/agentdeck",))
    assert not gather_work_workspace(state, WORK, board=_board_with(readable)).known


def test_a_declared_repository_gets_its_roles(tmp_path, state):
    scope = BoardScope(
        "agentdeck",
        org="eerovil",
        issue_repositories=("eerovil/AgentDeck",),
        artifact_repositories=("eerovil/agent-evidence",),
    )
    facts = gather_work_workspace(state, WORK, board=_board_with(scope))
    assert not facts.known  # a manifest alone is not a Work
    (project,) = facts.projects
    assert project.instance == "agentdeck"
    workspace = facts.build(WorkspaceProbe(FakeGit()))
    assert workspace.repository("eerovil/agent-evidence").roles == (RepositoryRole.ARTIFACT,)


def test_an_unreadable_read_keeps_git_contexts_facts_beside_the_diagnostic(tmp_path, git):
    tree = git.add(tmp_path / "tree")
    git.broken[str(tree.resolve())] = "fatal: bad object HEAD"
    context = GitContext("eerovil/agentdeck", "agent/issue-2143", False, head_sha="d" * 40)
    workspace = build_work_workspace(
        WORK,
        sessions=[_leg("k:a:1", tree, runs=("g",), git_context=context)],
        probe=WorkspaceProbe(git),
    )
    assert workspace.unresolved_observations == ()
    (repo,) = workspace.repositories
    assert repo.branches == ("agent/issue-2143",) and repo.heads == ("d" * 40,)
    assert _kinds(workspace) == [WorkspaceIssueKind.UNREADABLE_GIT]


def test_a_broken_instance_for_another_repository_of_the_same_owner_is_not_uncertainty(
    tmp_path, state
):
    elsewhere = BoardScope(
        "trailhead", org="eerovil", issue_repositories=("eerovil/issues",), state_readable=False
    )
    assert not gather_work_workspace(state, WORK, board=_board_with(elsewhere)).known
    mine = BoardScope(
        "agentdeck", org="EeroVil", issue_repositories=("EeroVil/AgentDeck",), state_readable=False
    )
    assert gather_work_workspace(state, WORK, board=_board_with(mine)).poller_unavailable


def _pull(status="open", base="master", number=2180):
    return PullRequestContext(
        "eerovil/agentdeck",
        number,
        "t",
        f"https://github.com/eerovil/agentdeck/pull/{number}",
        status,
        head_branch="agent/issue-2143",
        base_branch=base,
    )


def test_weaker_pull_request_facts_never_overwrite_stronger_ones(tmp_path, git):
    tree = git.add(tmp_path / "tree")
    strong = GitContext("eerovil/agentdeck", "b", False, (_pull("merged"),))
    stale = GitContext("eerovil/agentdeck", "b", False, (_pull("open", base="old"),))
    workspace = build_work_workspace(
        WORK,
        sessions=[
            _leg("k:a:1", tree, runs=("g",), git_context=strong),
            # Sorts after the Run-bound leg but would have won a last-write race.
            _leg("k:a:0", tree, git_context=stale, exclusive=True),
        ],
        probe=WorkspaceProbe(git),
    )
    (pull,) = workspace.repositories[0].pull_requests
    assert (pull.status, pull.base_branch) == ("merged", "master")
    assert {p.source for p in pull.provenance} == {"k:a:0", "k:a:1"}
    assert _kinds(workspace) == []


def test_equal_strength_pull_request_disagreement_is_reported(tmp_path, git):
    tree = git.add(tmp_path / "tree")
    one = GitContext("eerovil/agentdeck", "b", False, (_pull("open"),))
    two = GitContext("eerovil/agentdeck", "b", False, (_pull("merged"),))
    workspace = build_work_workspace(
        WORK,
        sessions=[
            _leg("k:a:1", tree, runs=("g",), git_context=one),
            _leg("k:a:2", tree, runs=("g",), git_context=two),
        ],
        probe=WorkspaceProbe(git),
    )
    (pull,) = workspace.repositories[0].pull_requests
    assert pull.status == "open"  # the first by stable order, and the disagreement is shown
    assert _kinds(workspace) == [WorkspaceIssueKind.PULL_REQUEST_CONFLICT]
    assert workspace.repositories[0].issues[0].problem


def test_partial_strong_pull_request_facts_are_filled_not_erased_by_weaker_ones(tmp_path, git):
    tree = git.add(tmp_path / "tree")
    partial = PullRequestContext(
        "eerovil/agentdeck", 2180, "t", "https://github.com/eerovil/agentdeck/pull/2180", "open"
    )
    strong = GitContext("eerovil/agentdeck", "b", False, (partial,))
    complete = GitContext("eerovil/agentdeck", "b", False, (_pull("merged", base="master"),))
    workspace = build_work_workspace(
        WORK,
        sessions=[
            _leg("k:a:1", tree, runs=("g",), git_context=strong),
            _leg("k:a:0", tree, git_context=complete, exclusive=True),
        ],
        probe=WorkspaceProbe(git),
    )
    (pull,) = workspace.repositories[0].pull_requests
    # Status comes from the stronger leg; the branches it did not know come from the weaker.
    assert (pull.status, pull.head_branch, pull.base_branch) == (
        "open",
        "agent/issue-2143",
        "master",
    )
    assert _kinds(workspace) == []


# --- Work workspace claims as the strongest evidence (#2149) ---------------------


def _claim(path, **fields):
    from agentdeck.work_workspace_claims import WorkspaceClaim

    values = {
        "work_id": WORK.work_id,
        "repository": "eerovil/agentdeck",
        "purpose": "implementation",
        "path": str(Path(path).resolve()),
        "branch": "agent/issue-2143",
        "base_ref": "master",
        "claimed_at": "2026-09-24T00:00:00+00:00",
        "confirmed_at": "2026-09-24T00:00:01+00:00",
        **fields,
    }
    return WorkspaceClaim(**values)


def test_a_claimed_worktree_with_no_card_left_is_still_the_works(tmp_path, git):
    tree = git.add(tmp_path / "issue-2143")
    artifacts = git.add(
        tmp_path / "evidence", "https://github.com/eerovil/agent-evidence.git"
    )
    workspace = build_work_workspace(
        WORK,
        claims=[
            _claim(tree),
            _claim(artifacts, repository="eerovil/agent-evidence", purpose="artifact"),
        ],
        probe=WorkspaceProbe(git),
    )

    assert [repo.repository for repo in workspace.repositories] == [
        "eerovil/agentdeck",
        "eerovil/agent-evidence",
    ]
    assert RepositoryRole.ARTIFACT in workspace.repository("eerovil/agent-evidence").roles
    assert workspace.primary == "eerovil/agentdeck"


def test_pending_release_and_unsettled_claims_are_problems_to_see(tmp_path, git):
    held = git.add(tmp_path / "issue-2143")
    workspace = build_work_workspace(
        WORK,
        claims=[
            _claim(
                held,
                release_requested_at="2026-09-24T01:00:00+00:00",
                last_error="2 uncommitted file(s)",
            ),
            _claim(tmp_path / "evidence", purpose="review", confirmed_at=""),
        ],
        probe=WorkspaceProbe(git),
    )

    kinds = _kinds(workspace)
    assert WorkspaceIssueKind.RELEASE_PENDING in kinds
    assert WorkspaceIssueKind.CLAIM_UNSETTLED in kinds
    pending = next(i for i in workspace.problems if i.kind is WorkspaceIssueKind.RELEASE_PENDING)
    assert "2 uncommitted file(s)" in pending.detail


def test_released_and_other_works_claims_are_not_this_works(tmp_path, git):
    tree = git.add(tmp_path / "issue-2143")
    workspace = build_work_workspace(
        WORK,
        claims=[
            _claim(tree, released_at="2026-09-24T02:00:00+00:00"),
            _claim(tree, work_id="github_issue:eerovil/agentdeck#1"),
        ],
        probe=WorkspaceProbe(git),
    )

    assert workspace.repository("eerovil/agentdeck").worktrees == ()


def test_unreadable_claims_are_unknown_not_absent(tmp_path, git):
    workspace = build_work_workspace(
        WORK, claims_unavailable="database locked", probe=WorkspaceProbe(git)
    )

    assert WorkspaceIssueKind.CLAIMS_UNAVAILABLE in _kinds(workspace)
