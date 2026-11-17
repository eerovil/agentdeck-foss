"""The deck-started Claude/Codex Work reviewer (#3640)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentdeck import agent_review
from agentdeck.poller_engine.intake import CommandKind, parse_control, parse_verdicts
from agentdeck.review_refs import ReviewBusy, ReviewError
from agentdeck.work_release import findings_target
from agentdeck.work_router import covers

HEAD = "a" * 40
DIGEST = "d" * 64
PULL = "owner/repo#7"
WORK = "github_issue:owner/repo#3"


class _Deck:
    """Fake ports: a pull request, the git it runs, and the chats it starts."""

    def __init__(self, tmp_path: Path, *, head: str = HEAD) -> None:
        self.root = tmp_path / "repo"
        self.pull = {"state": "OPEN", "isDraft": False, "headRefOid": head, "baseRefName": "main"}
        self.git_calls: list[tuple[str, ...]] = []
        self.started: list[tuple[str, Path, str, str]] = []
        self.alive: dict[str, bool | None] = {}
        self.accept = True
        self.now = 1000.0
        self.others: list[Path] = []
        self.checkout: Path | None = self.root / ".worktrees" / "issue-3"
        # Each candidate directory's ``origin``.
        self.origins = {self.checkout: "https://github.com/Owner/repo.git"}

    async def git(self, *args: str, cwd: Path) -> tuple[int, str]:
        self.git_calls.append(args)
        if args[:2] == ("remote", "get-url"):
            return (0, self.origins[cwd]) if cwd in self.origins else (2, "no such remote")
        if args[0] == "ls-remote":
            return 0, "ref: refs/heads/main\tHEAD\n" + HEAD + "\tHEAD"
        if args[0] == "rev-parse":
            return 0, str(cwd.parent.parent / ".git") if ".worktrees" in str(cwd) else str(
                cwd / ".git"
            )
        return 0, ""

    async def start(self, kind: str, cwd: Path, text: str, token: str) -> tuple[bool, str]:
        self.started.append((kind, cwd, text, token))
        return self.accept, "" if self.accept else "account over its limit"

    async def read_pull(self, repository: str, number: int) -> dict:
        assert (repository, number) == ("owner/repo", 7)
        return self.pull

    async def digest(self, repository: str, number: int, head: str) -> tuple[str, str]:
        return DIGEST, ""

    def reviewer(self, tmp_path: Path, *, kind: str = "claude", cap: int = 1):
        return agent_review.AgentReviewer(
            kind=kind,
            max_reviewers=cap,
            ledger=tmp_path / "agent-reviews.json",
            start=self.start,
            alive=lambda token: self.alive.get(token),
            checkout_of=lambda pull: [path for path in (self.checkout, *self.others) if path],
            digest=self.digest,
            read_pull=self.read_pull,
            git=self.git,
            clock=lambda: self.now,
        )


def test_the_setting_picks_who_reviews():
    assert agent_review.effective("chatgpt", review_chats=True) == "chatgpt"
    # The public copy has no review chats: its default is no reviewer, as before.
    assert agent_review.effective("chatgpt", review_chats=False) == "off"
    assert agent_review.effective("claude", review_chats=False) == "claude"
    assert agent_review.effective("codex", review_chats=True) == "codex"
    assert agent_review.effective("off", review_chats=True) == "off"


@pytest.mark.asyncio
async def test_a_ready_head_starts_one_reviewer_in_its_own_checkout(tmp_path):
    deck = _Deck(tmp_path)
    reviewer = deck.reviewer(tmp_path)

    outcome = await reviewer.start(PULL, WORK)

    assert covers(outcome, PULL, HEAD)
    ((kind, cwd, text, token),) = deck.started
    assert kind == "claude"
    # Under the main checkout's .worktrees/, not the Work's own checkout.
    assert cwd.parent == deck.root / ".worktrees" and cwd.name.startswith(f"review-7-{HEAD[:12]}")
    assert ("worktree", "add", "--detach", str(cwd), HEAD) in deck.git_calls
    assert ("fetch", "-q", "origin", "pull/7/head", "main") in deck.git_calls
    assert agent_review.is_reviewer_prompt(text)
    # The commands the brief hands out are ones the deck's release and intake read.
    pass_line = f"/reviewed pass {HEAD} {DIGEST} {PULL}"
    changes_line = f"/reviewed changes-needed {HEAD} {PULL}"
    findings = f"/claude Review of {PULL} at {HEAD}"
    for line in (pass_line, changes_line, findings, "deck-request owner/repo#3 "):
        assert line in text
    (verdict,) = parse_verdicts(pass_line)
    assert (verdict.kind, verdict.verdict, verdict.digest) == (CommandKind.REVIEWED, "pass", DIGEST)
    assert parse_control(findings + "\n- a finding", bot_marker="").kind == CommandKind.CLAUDE
    assert findings_target(findings, "owner/repo") == (PULL, HEAD)
    (line,) = json.loads((tmp_path / "agent-reviews.json").read_text())
    assert line["token"] == token and line["head"] == HEAD

    # The same head asked again while its reviewer runs starts nobody new.
    deck.alive[token] = True
    again = await reviewer.start(PULL, WORK)
    assert covers(again, PULL, HEAD) and len(deck.started) == 1


@pytest.mark.asyncio
async def test_the_cap_holds_and_a_stopped_reviewer_frees_its_place_and_checkout(tmp_path):
    deck = _Deck(tmp_path)
    reviewer = deck.reviewer(tmp_path, cap=1)
    await reviewer.start(PULL, WORK)
    token = deck.started[0][3]
    deck.alive[token] = True
    deck.pull["headRefOid"] = "b" * 40

    with pytest.raises(ReviewBusy):
        await reviewer.start(PULL, WORK)

    deck.alive[token] = False
    deck.now += agent_review.START_FLOOR_S
    await reviewer.start(PULL, WORK, note="a person kept the finding", again=True)
    assert any(call[:2] == ("worktree", "remove") for call in deck.git_calls)
    assert len(deck.started) == 2
    assert "a person kept the finding" in deck.started[1][2]
    (line,) = json.loads((tmp_path / "agent-reviews.json").read_text())
    assert line["head"] == "b" * 40


@pytest.mark.asyncio
async def test_a_design_review_reads_the_default_branch_once_per_design(tmp_path):
    """#4332: one reviewer per design comment, on a checkout of the default branch."""
    deck = _Deck(tmp_path)
    reviewer = deck.reviewer(tmp_path)
    design = "https://github.com/owner/repo/issues/3#issuecomment-55"

    started = await reviewer.start_design("owner/repo#3", design)

    assert "started on the design" in started.summary
    ((kind, cwd, text, token),) = deck.started
    assert cwd.parent == deck.root / ".worktrees" and cwd.name.startswith("design-3-")
    assert ("fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main") in deck.git_calls
    assert ("worktree", "add", "--detach", str(cwd), "origin/main") in deck.git_calls
    assert agent_review.is_reviewer_prompt(text)
    assert "/claude Design review of owner/repo#3: pass 55" in text
    # Only this reviewer gives the verdict on this design (#4332 review); its brief carries
    # its token, for a Codex shell that has no variable for it.
    ledger = tmp_path / "agent-reviews.json"
    assert agent_review.reviews_design(ledger, token, "Owner/Repo#3", "55")
    assert f"--session-token {token} " in text
    for other in ((token, "owner/repo#3", "56"), ("x", "owner/repo#3", "55"), ("", "o/r#3", "")):
        assert not agent_review.reviews_design(ledger, *other)
    deck.alive[token] = True
    again = await reviewer.start_design("owner/repo#3", design)
    assert "already reviews" in again.summary and len(deck.started) == 1
    # A newer design waits for a place like any review.
    with pytest.raises(ReviewBusy):
        await reviewer.start_design("owner/repo#3", design.replace("55", "56"))
    with pytest.raises(ReviewError):
        await reviewer.start_design("owner/repo#3", "https://github.com/owner/repo/issues/3")


@pytest.mark.asyncio
async def test_a_design_review_holds_its_place_through_a_slow_checkout(tmp_path):
    """#4337 review: the place is taken before the checkout, so a reader sees the reviewer
    starting and a second ask waits for it and starts no other; a failed checkout gives the
    place back; a reviewer that stopped with no verdict is replaced on the next ask."""
    deck = _Deck(tmp_path)
    reviewer = deck.reviewer(tmp_path)
    ledger = tmp_path / "agent-reviews.json"
    design = "https://github.com/owner/repo/issues/3#issuecomment-55"
    slow, plain = asyncio.Event(), deck.git

    async def git(*args: str, cwd: Path) -> tuple[int, str]:
        if args[0] == "ls-remote":
            await slow.wait()
        return await plain(*args, cwd=cwd)

    reviewer._git = git
    first = asyncio.create_task(reviewer.start_design("owner/repo#3", design))
    second = asyncio.create_task(reviewer.start_design("owner/repo#3", design))
    for _ in range(20):
        await asyncio.sleep(0)
    line = agent_review.design_reviewer(ledger, "owner/repo#3", "55")
    assert line["starting"] and not line["path"] and deck.started == []
    later = deck.now + agent_review.SPAWN_GRACE_S + 1
    assert agent_review.running(line, later, lambda token: None)
    gone = {**line, "pid": 2**22 + 12345}
    assert not agent_review.running(gone, deck.now, lambda token: None)
    slow.set()
    await first
    assert "already reviews" in (await second).summary and len(deck.started) == 1
    (line,) = json.loads(ledger.read_text())
    assert "starting" not in line and line["path"] == str(deck.started[0][1])

    # Stopped with no verdict: the next ask starts a new reviewer on the same comment.
    deck.alive[line["token"]] = False
    deck.now += agent_review.START_FLOOR_S
    await reviewer.start_design("owner/repo#3", design)
    assert len(deck.started) == 2

    async def broken(*args: str, cwd: Path) -> tuple[int, str]:
        return (128, "no remote") if args[0] == "ls-remote" else await plain(*args, cwd=cwd)

    reviewer._git = broken
    deck.alive[deck.started[1][3]] = False
    deck.now += agent_review.START_FLOOR_S
    with pytest.raises(ReviewError, match="default branch"):
        await reviewer.start_design("owner/repo#3", design)
    assert json.loads(ledger.read_text()) == []


@pytest.mark.asyncio
async def test_a_design_review_starts_from_the_asking_chats_checkout(tmp_path):
    """#4332 review: the design's code is the repository the asking chat's own checkout
    works in, even when the card lives in another, issues-only repository; with no
    readable checkout it is the issue's repository."""
    deck = _Deck(tmp_path)
    store = tmp_path / "store"
    (store / ".worktrees" / "issue-3").mkdir(parents=True)
    own = store / ".worktrees" / "issue-3"
    deck.origins[own] = "git@github.com:Acme/store.git"
    reviewer = deck.reviewer(tmp_path)
    design = "https://github.com/acme/issues/issues/3#issuecomment-55"

    await reviewer.start_design("acme/issues#3", design, checkout=str(own))
    ((_kind, cwd, text, _token),) = deck.started
    assert cwd.parent == store / ".worktrees", "the code's own main checkout"
    assert "/claude Design review of acme/issues#3: pass 55" in text, "verdicts stay on the card"

    for n, checkout in enumerate((str(tmp_path / "gone"), str(tmp_path))):  # gone, no origin
        other = _Deck(tmp_path / f"b{n}")
        await other.reviewer(tmp_path / f"b{n}").start_design(
            "owner/repo#3", design.replace("acme/issues", "owner/repo"), checkout=checkout
        )
        ((_kind, cwd, _text, _token),) = other.started
        assert cwd.parent == other.root / ".worktrees"


@pytest.mark.asyncio
async def test_a_start_that_cannot_happen_leaves_the_head_pending(tmp_path):
    """Every refusal is a ``ReviewError``, which the router keeps pending and asks again."""
    deck = _Deck(tmp_path)
    reviewer = deck.reviewer(tmp_path, kind="codex")
    deck.accept = False
    with pytest.raises(ReviewError, match="over its limit"):
        await reviewer.start(PULL, WORK)
    # Its checkout is removed and its place given back.
    assert any(call[:2] == ("worktree", "remove") for call in deck.git_calls)
    assert json.loads((tmp_path / "agent-reviews.json").read_text()) == []

    # The Codex brief carries the three-at-once rule (#3602); a Claude one does not.
    assert agent_review.CODEX_LANES in deck.started[0][2]
    assert agent_review.CODEX_LANES not in agent_review.brief(
        name="Claude", pull=PULL, issue="owner/repo#3", head=HEAD, base="main", digest=DIGEST
    )

    deck.checkout = None
    with pytest.raises(ReviewError, match="no checkout"):
        await reviewer.start(PULL, WORK)
    deck.pull["isDraft"] = True
    with pytest.raises(ReviewError, match="no ready head"):
        await reviewer.start(PULL, WORK)


@pytest.mark.asyncio
async def test_a_pull_request_in_another_repository_is_checked_out_from_that_one(tmp_path):
    """The Work's chat may sit in another repository's checkout than its pull request's."""
    deck = _Deck(tmp_path)
    reviewer = deck.reviewer(tmp_path)
    deck.origins[deck.checkout] = "git@github.com:owner/other.git"
    with pytest.raises(ReviewError, match="no checkout of owner/repo.*is owner/other"):
        await reviewer.start(PULL, WORK)
    assert deck.started == []

    elsewhere = tmp_path / "elsewhere"
    deck.others = [elsewhere]
    deck.origins[elsewhere] = "https://github.com/owner/repo"
    await reviewer.start(PULL, WORK)
    ((_kind, cwd, _text, _token),) = deck.started
    assert cwd.parent == elsewhere / ".worktrees"


def test_origin_urls_name_their_repository():
    for url in (
        "https://github.com/Owner/Repo.git",
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo",
        "https://x-access-token@github.com/owner/repo/",
    ):
        assert agent_review.origin_repository(url) == "owner/repo"
    assert agent_review.origin_repository("https://gitlab.com/owner/repo") == ""


def test_the_stand_in_reviewer_and_the_chat_list_share_one_brief_check():
    from agentdeck import review_fallback

    assert review_fallback.is_reviewer_prompt is agent_review.is_reviewer_prompt
    assert not agent_review.is_reviewer_prompt("Please review this")


def test_a_reviewer_idle_on_its_own_turn_runs_while_its_sub_agents_work():
    """Its rate-my-pr lanes outlive its own turn: the checkout stays until they finish."""
    from dataclasses import replace
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from agentdeck.models import Session, SessionStatus, SubagentProgress
    from agentdeck.state import AppState

    state = AppState()
    reviewer = Session(
        key="codex:test:rev",
        account_key="codex:test",
        session_id="rev",
        status=SessionStatus.LIVE,
        thinking=False,
        subagent_count=1,
        last_progress=datetime.now(UTC),
        subagents=(SubagentProgress(agent_id="lane-1", status="working"),),
    )
    state.update_session(reviewer)
    state.session_tokens["tok"] = reviewer.key
    hooks = agent_review.deck_hooks(
        SimpleNamespace(review=SimpleNamespace(account="")),
        injector=None,
        state=state,
        accounts=[],
        providers={},
    )
    assert hooks["alive"]("tok") is True
    state.update_session(replace(reviewer, subagent_count=0, subagents=()))
    assert hooks["alive"]("tok") is False


@pytest.mark.asyncio
async def test_git_never_opens_a_password_box(tmp_path, monkeypatch):
    """#4337: the services process inherits the desktop's ``SSH_ASKPASS``; git ran it, a
    password box waited for nobody, and every design review hung. Git now speaks to GitHub
    through the deck's helper only, never asking, and a timeout kills its whole group."""
    seen = {}

    class _Proc:
        pid = 4337
        returncode = 0

        async def communicate(self):
            await asyncio.sleep(1)
            return b"", b""

        async def wait(self):
            return 0

    async def spawn(*args, **kwargs):
        seen.update(kwargs)
        return _Proc()

    killed = []
    monkeypatch.setenv("SSH_ASKPASS", "/usr/bin/ksshaskpass")
    monkeypatch.setenv("GIT_ASKPASS", "/usr/bin/ksshaskpass")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(agent_review.os, "killpg", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(agent_review, "GIT_TIMEOUT_S", 0.01)

    assert await agent_review.git("ls-remote", "origin", cwd=tmp_path) == (124, "timed out")

    env = seen["env"]
    assert "SSH_ASKPASS" not in env and "GIT_ASKPASS" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    config = {
        env[f"GIT_CONFIG_KEY_{n}"]: env[f"GIT_CONFIG_VALUE_{n}"]
        for n in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert config["credential.interactive"] == "false"
    assert "agentdeck.git_credential" in config["credential.https://github.com.helper"]
    assert seen["start_new_session"] and killed == [4337]
