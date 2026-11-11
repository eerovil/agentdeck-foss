"""Ship now of a commit a fix was pushed on top of (#4212 review): ``ship_split``."""

from __future__ import annotations

import json

import pytest

from agentdeck.ship_split import SplitFailed, branch_name, open_ship_pull

REPO = "eerovil/agentdeck"
HEAD = "c" * 40


class GitHub:
    """The ``gh`` calls ``open_ship_pull`` makes, against a small in-memory repository."""

    def __init__(self, *, fork: bool = False, lose_create: bool = False) -> None:
        self.refs: dict[str, str] = {}
        self.pulls: dict[str, int] = {}
        self.drafts: list[int] = []
        self.calls: list[list[str]] = []
        self.fork, self.lose_create = fork, lose_create

    async def __call__(self, argv, repository):
        argv = list(argv)
        self.calls.append(argv)
        assert repository == REPO
        out = b""
        fields = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("repos"))
        if argv[:3] == ["gh", "pr", "view"]:
            facts = {"baseRefName": "master", "isCrossRepository": self.fork, "state": "OPEN"}
            out = json.dumps(facts).encode()
        elif argv[:3] == ["gh", "pr", "ready"]:
            assert argv[4] == "--undo"
            self.drafts.append(int(argv[3]))
        elif argv[2:5] == ["-X", "POST", f"repos/{REPO}/git/refs"]:
            name = fields["ref"].removeprefix("refs/heads/")
            if name in self.refs:
                return 1, b"", b"Reference already exists"
            self.refs[name] = fields["sha"]
        elif argv[2:5] == ["-X", "POST", f"repos/{REPO}/pulls"]:
            assert fields["draft"] == "true" and fields["base"] == "master"
            assert fields["body"].startswith("Refs #4212\n")
            self.pulls[fields["head"]] = 20
            if self.lose_create:
                return 1, b"", b"timeout"
            out = b"20"
        elif argv[2].startswith(f"repos/{REPO}/git/ref/heads/"):
            name = argv[2].removeprefix(f"repos/{REPO}/git/ref/heads/")
            if name not in self.refs:
                return 1, b"", b"Not Found"
            out = json.dumps(self.refs[name]).encode()
        elif argv[2].startswith(f"repos/{REPO}/pulls?"):
            branch = argv[2].rpartition(":")[2]
            out = json.dumps([self.pulls[branch]] if branch in self.pulls else []).encode()
        else:
            raise AssertionError(argv)
        return 0, out, b""


async def test_it_opens_a_draft_at_the_reviewed_commit_and_turns_the_original_into_a_draft():
    github = GitHub()
    assert await open_ship_pull(REPO, 9, HEAD.upper(), 4212, run=github) == 20
    assert github.refs == {branch_name(9, HEAD): HEAD}
    assert github.drafts == [9]
    # Pressed again: the branch and the pull request an earlier press made are kept.
    assert await open_ship_pull(REPO, 9, HEAD, 4212, run=github) == 20
    assert github.drafts == [9, 9]
    assert sum(1 for call in github.calls if call[-2:] == ["--jq", ".number"]) == 1


async def test_a_lost_create_answer_finds_the_pull_request_it_opened():
    github = GitHub(lose_create=True)
    assert await open_ship_pull(REPO, 9, HEAD, 4212, run=github) == 20


async def test_a_branch_at_another_commit_or_a_fork_ships_nothing():
    github = GitHub()
    github.refs[branch_name(9, HEAD)] = "d" * 40
    with pytest.raises(SplitFailed, match="could not be made"):
        await open_ship_pull(REPO, 9, HEAD, 4212, run=github)
    with pytest.raises(SplitFailed, match="fork"):
        await open_ship_pull(REPO, 9, HEAD, 4212, run=GitHub(fork=True))
    assert github.drafts == []


async def test_the_new_pull_request_joins_every_chat_and_work_the_original_was_registered_for():
    from types import SimpleNamespace

    from agentdeck.models import PullRequestRef
    from agentdeck.work_services import register_split

    added = []
    state = SimpleNamespace(
        session_pull_requests={
            "leg-a": (PullRequestRef("eerovil/agentdeck", 9, work_id="github_issue:x#7"),),
            "leg-b": (PullRequestRef("eerovil/other", 9),),
        },
    )
    took = [True]

    async def add(leg, ref):
        added.append((leg, ref, ref.work_id))
        return took[0]

    assert await register_split(state, add, "EerOvil/AgentDeck", 9, 20)
    assert added == [("leg-a", PullRequestRef("eerovil/agentdeck", 20), "github_issue:x#7")]
    assert not await register_split(state, add, "eerovil/agentdeck", 8, 21)
    took[0] = False
    assert not await register_split(state, add, "eerovil/agentdeck", 9, 20), "the web refused"
