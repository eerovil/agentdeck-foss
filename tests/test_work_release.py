"""A Work merges its own pull requests: the release, step 2 (#2895, ADR 0008).

GitHub, the jobs and the Work's chat are fakes; what is under test is which verdicts are
taken, when a Work merges (directly, at its exact head), in which order, and what its agent is
told when it cannot.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentdeck.poller_ops import release_policies
from agentdeck.work_release import (
    ALREADY_MERGED,
    DIFF_TOO_LARGE,
    FAILED,
    MERGED,
    OWNER_CALL_ASK,
    Policy,
    Ports,
    PressRefused,
    Pull,
    ReleaseStore,
    WorkReleaseService,
    checks_state,
    deploy_sentence,
    findings_target,
    merge_outcome,
    review_chat_of,
    same_commit,
    verdict_settled,
    verifies,
)

WORK = "github_issue:eerovil/agentdeck#7"
PR = "eerovil/agentdeck#9"
CHAT_PR = "eerovil/review-chats#3"
HEAD = "a" * 40
OTHER = "b" * 40
DIGEST = "d" * 64


class Fake:
    def __init__(self, tmp_path) -> None:
        self.now = 1000.0
        self.pulls = {PR: Pull(PR, "OPEN", False, HEAD, "MERGEABLE", "CLEAN", "green")}
        self.digests = {(PR, HEAD): DIGEST}
        self.green: set[str] = {HEAD}
        self.policy = Policy(auto_release=True)
        self.switched = True
        self.paused = False
        self.merged: list[tuple[str, str]] = []
        self.merge_heads: list[str] = []
        self.updated: list[str] = []
        self.told: list[tuple[str, str, str]] = []
        self.merge_answer = MERGED
        self.busy = False
        self.labels: set[str] | None = set()
        self.update_ok = True
        self.verifying = False
        self.red: set[str] = set()
        self.moved: set[str] = set()
        self.unclean: set[str] = set()
        self.service = WorkReleaseService(
            ReleaseStore(tmp_path / "work-release.json"),
            Ports(
                switched=lambda repository: self.switched and repository == "eerovil/agentdeck",
                paused=lambda work_id: self.paused,
                policy=lambda repository: self.policy,
                pulls=lambda work_id: tuple(self.pulls),
                chat=lambda work_id: "chat-a",
                read_pull=self.read_pull,
                digest=self.digest,
                green_heads=self.green_heads,
                merge=self.merge,
                update_branch=self.update_branch,
                tell=lambda chat, message_id, text: self.told.append((chat, message_id, text)),
                clock=lambda: self.now,
                busy=lambda work_id: self.busy,
                issue_labels=self.issue_labels,
                verifying=self.is_verifying,
                failed_heads=self.failed_heads,
                moved_heads=self.moved_heads,
                unclean_heads=self.unclean_heads,
            ),
        )

    async def read_pull(self, ref):
        return self.pulls.get(ref)

    async def digest(self, ref, head):
        return self.digests.get((ref, head), "")

    async def failed_heads(self, work_id):
        return set(self.red)

    async def moved_heads(self, work_id):
        return set(self.moved)

    async def unclean_heads(self, work_id):
        return set(self.unclean)

    async def is_verifying(self, work_id):
        return self.verifying

    async def green_heads(self, work_id):
        return set(self.green)

    async def merge(self, ref, method, head):
        self.merged.append((ref, method))
        self.merge_heads.append(head)
        if self.merge_answer == MERGED:
            self.set(ref, state="MERGED")
        return self.merge_answer, "Required status check is expected"

    async def update_branch(self, ref):
        self.updated.append(ref)
        return self.update_ok

    async def issue_labels(self, work_id):
        return self.labels

    def set(self, ref, **changes):
        self.pulls[ref] = Pull(**{**self.pulls[ref].__dict__, **changes})

    @property
    def entry(self):
        return self.service.store.works.get(WORK, {})


@pytest.fixture
def fake(tmp_path):
    return Fake(tmp_path)


async def passed(fake, head=HEAD, digest=DIGEST, targets=()):
    return await fake.service.on_verdict(WORK, "pass", head[:12], digest, targets)


def problems(fake):
    """What the Work's chat was told, leaving out the verdicts themselves (#2992) and the call
    to check a merge live (#3141)."""
    return [
        told for told in fake.told if ":verdict:" not in told[1] and ":live-check:" not in told[1]
    ]


def live_checks(fake):
    return [text for _chat, message_id, text in fake.told if ":live-check:" in message_id]


# --- the verdict ---


async def test_a_pass_at_the_current_head_and_diff_merges_a_tested_work_at_that_head(fake):
    answer = await passed(fake)
    assert "review pass recorded" in answer
    assert fake.merged == [(PR, "merge")] and fake.merge_heads == [HEAD]
    # #3141: its agent is asked once to check the merge live, on this deck's own deploy, and
    # GitHub's event for the same merge asks nothing more.
    assert len(live_checks(fake)) == 1 and '"deck_deploy"' in live_checks(fake)[0]
    asked_at = fake.entry["live_check_asked"]["at"]
    fake.now += 60
    await fake.service.on_pull_event(WORK, PR, "closed", True)
    assert len(live_checks(fake)) == 1
    assert fake.entry["live_check_asked"]["at"] == asked_at


async def test_a_merge_seen_live_asks_for_a_live_check_once_nothing_is_open(fake):
    """#3141: a merge by hand, seen as GitHub's event, asks once the last pull request landed;
    a Work that merged before is never asked by a later look, and the deploy sentence follows
    where it merged."""
    other = "eerovil/site#4"
    fake.policy = Policy(auto_release=False, deploys=frozenset({"eerovil/site"}))
    fake.pulls = {other: Pull(other, "MERGED", False, HEAD, "", "", "green")}
    await fake.service.evaluate(WORK)
    assert live_checks(fake) == [], "an old merge is not a fresh one"
    fake.pulls[PR] = Pull(PR, "OPEN", True, OTHER, "MERGEABLE", "CLEAN", "green")
    await fake.service.on_pull_event(WORK, other, "closed", True)
    assert live_checks(fake) == [], "a draft next step is still open"
    fake.set(PR, state="CLOSED")
    await fake.service.on_pull_event(WORK, PR, "closed", False)
    await fake.service.evaluate(WORK)
    [text] = live_checks(fake)
    assert other in text and "`deployment` wake-on for eerovil/site" in text
    assert "Do not read the GitHub deployments API" in text  # #3584
    assert "/live-check" in text and "`not_live`" in text
    assert "close the issue yourself" in text and "do not ask anyone" in text  # #3437
    # #3220: each pull request is told how it goes live, not the deck's way for all of them.
    said = deploy_sentence(Policy(), ["eerovil/agentdeck#3196", "eerovil/other-tool#7"])
    assert said.startswith("eerovil/agentdeck#3196: This deck deploys it")
    assert "eerovil/other-tool#7: Nothing on this host watches" in said
    assert "deploy by hand is `not_live`" in said, "not a nothing_to_check and a Finish"


async def test_a_merge_github_refuses_is_told_once_and_tried_again(fake):
    fake.merge_answer = FAILED
    await passed(fake)
    await fake.service.evaluate(WORK)
    assert len(fake.merged) == 2, "tried again on the next wake"
    assert [m for _c, m, _t in problems(fake)] == [f"work-release:{WORK}:merge:{PR}@{HEAD[:12]}"]
    assert "Required status check" in problems(fake)[0][2]


async def test_an_already_merged_pull_request_is_not_told(fake):
    fake.merge_answer = ALREADY_MERGED
    assert "already merged" in (await passed(fake) and await fake.service.evaluate(WORK))
    assert problems(fake) == []


# --- watching ---


async def test_the_release_survives_a_restart(fake, tmp_path):
    fake.green = set()
    await passed(fake)
    again = Fake(tmp_path)
    assert again.entry["passes"][PR]["head"] == HEAD


@pytest.mark.parametrize("checks_gate", [True, False])
async def test_a_green_commit_status_at_the_passed_head_merges_a_checks_work(fake, checks_gate):
    """#3019: Drone reports by commit status, not check suite; the status re-checks the Work.
    #3132: a Work proven by its verification job waits for its own running CI just the same."""
    fake.policy = Policy(
        auto_release=True, github_checks=frozenset({"eerovil/agentdeck"} if checks_gate else ())
    )
    fake.set(PR, checks="pending")
    await passed(fake)
    assert fake.merged == []
    assert await fake.service.evaluate(WORK) == f"{PR}'s checks are pending at {HEAD[:12]}"
    fake.set(PR, checks="green")
    await fake.service.on_status("eerovil/agentdeck", OTHER)
    await fake.service.on_status("eerovil/other", HEAD)
    assert fake.merged == [], "another commit or repository is not this Work's"
    await fake.service.on_status("EeroVil/AgentDeck", HEAD)
    assert fake.merged == [(PR, "merge")] and fake.merge_heads == [HEAD]


@pytest.mark.parametrize("checks_gate", [True, False])
@pytest.mark.parametrize("auto_release", [True, False])
async def test_red_checks_refuse_a_tested_work_and_tell_its_agent(fake, checks_gate, auto_release):
    """#3132: a green verification job does not stand in for the pull request's failed CI.
    #3163: nor does a Work proven by its checks, or one waiting for a person's Merge, sit on
    red checks with its agent never told."""
    fake.policy = Policy(
        auto_release=auto_release,
        github_checks=frozenset({"eerovil/agentdeck"} if checks_gate else ()),
    )
    fake.set(PR, checks="red")
    await passed(fake)
    assert fake.merged == []
    assert await fake.service.evaluate(WORK) == f"{PR}'s checks are red at {HEAD[:12]}"
    assert [m for _c, m, _t in problems(fake)] == [f"work-release:{WORK}:red:{PR}@{HEAD[:12]}"]
    assert "awaiting_merge" not in fake.entry


async def test_a_commit_status_does_not_read_a_work_its_checks_do_not_gate(fake):
    fake.policy = Policy(auto_release=True, review_only=frozenset({"eerovil/agentdeck"}))
    fake.busy = True
    await passed(fake)
    reads = []
    fake.service.ports.read_pull = reads.append  # would fail if awaited
    await fake.service.on_status("eerovil/agentdeck", HEAD)
    assert reads == []


# --- small parts ---


def test_checks_state_reads_the_rollup():
    assert checks_state([]) == "none"
    assert checks_state([{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}]) == "green"
    assert checks_state([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]) == "pending"
    assert checks_state([{"conclusion": "FAILURE"}, {"state": "PENDING"}]) == "red"


def test_merge_outcome_and_same_commit():
    assert merge_outcome(0, "") == MERGED
    assert merge_outcome(1, "Pull request is already merged") == ALREADY_MERGED
    assert merge_outcome(1, "Head branch was modified") == FAILED
    assert same_commit(HEAD, HEAD[:7]) and not same_commit(HEAD, HEAD[:6])
    assert not same_commit("", HEAD[:7])


def test_merge_asks_github_through_the_api_so_a_ruleset_bypass_counts(monkeypatch):
    """``gh pr merge`` refuses a BLOCKED pull request before asking GitHub (#3728)."""
    from agentdeck import gh_cli, work_release

    calls: list[list[str]] = []
    pull = {"merged": False, "ref": "agent/x", "repo": "EeroVil/AgentDeck"}

    async def read(argv, **_kw):
        calls.append(list(argv))
        return 0, json.dumps(pull).encode()

    async def write(argv, **_kw):
        calls.append(list(argv))
        return 0, b"{}", b""

    monkeypatch.setattr(gh_cli, "run", read)
    monkeypatch.setattr(gh_cli, "run_once", write)
    assert asyncio.run(work_release.merge(PR, "squash", HEAD))[0] == MERGED
    assert not any("pr" in argv for argv in calls)
    assert calls[1] == [
        "gh", "api", "-X", "PUT", "repos/eerovil/agentdeck/pulls/9/merge",
        "-f", "merge_method=squash", "-f", f"sha={HEAD}",
    ]  # fmt: skip
    assert calls[2][-1] == "repos/eerovil/agentdeck/git/refs/heads/agent/x"

    # A fork's branch is not ours to delete; an already merged pull request is not merged again.
    calls.clear()
    pull["repo"] = "someone/agentdeck"
    assert asyncio.run(work_release.merge(PR, "merge", HEAD))[0] == MERGED
    assert len(calls) == 2
    calls.clear()
    pull["merged"] = True
    assert asyncio.run(work_release.merge(PR, "merge", HEAD))[0] == ALREADY_MERGED
    assert len(calls) == 1


def test_release_policies_come_from_switched_manifests(tmp_path):
    path = tmp_path / "instances" / "agentdeck" / "poller.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'contract = 1\n[board]\norg = "Eerovil"\nproject_number = 1\n'
        'spec_only_label = "spec-only"\n'
        '[policy]\nmerge_method = "squash"\n'
        "[capabilities]\nworks_router = true\nauto_release = true\n"
        '[authorization.release]\nallow = ["Review-Bot[bot]"]\n'
        '[[repos]]\nid = "agentdeck"\nholds_issues = true\n'
        '[[repos]]\nid = "review-chats"\nverification = "review_only"\n'
        '[[repos]]\nid = "site"\nauto_deploys = true\n'
        '[staging]\nenvironment_prefix = "staging-"\nrepos = ["site"]\n'
        'links = [{label = "Site", url = "https://{slug}.example/"},'
        ' {label = "Bad", url = "http://{slug}.example/"}]\n'
    )
    policies = release_policies(tmp_path)
    policy = policies["eerovil/agentdeck"]
    assert policy.auto_release and policy.merge_method == "squash"
    assert policy.review_only == {"eerovil/review-chats"}
    from agentdeck.poller_engine.authorization import Tier

    assert policy.authorization.allows(Tier.RELEASE, "review-bot[bot]")
    assert policy.spec_only_label == "spec-only"
    assert policy.deploys == {"eerovil/site"}
    # [staging] (#3180): plain-http links are dropped.
    assert policy.staging_repos == {"eerovil/site"}
    assert policy.staging.prefix == "staging-"
    assert policy.staging.links == (("Site", "https://{slug}.example/"),)
    assert policies["eerovil/review-chats"] is policy


def test_a_work_job_with_no_run_proves_its_work_head():
    from agentdeck.poller_engine.verification import (
        credited_head_for_work,
        credited_head_for_work_alone,
    )

    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    snapshot = {
        "root": "/w",
        "head": HEAD,
        "staged_sha256": empty,
        "tracked_diff_sha256": empty,
        "untracked_sha256": empty,
        "untracked_count": 0,
    }
    job = {
        "purpose": "verification",
        "work_id": WORK,
        "run_id": "",
        "state": "done",
        "result_class": "success",
        "tested_workspace": snapshot,
        "verified_workspace": snapshot,
        "tested_workspaces": {"r": snapshot},
        "verified_workspaces": {"r": snapshot},
    }
    credited = credited_head_for_work_alone(job, work_id=WORK.upper())
    other = credited_head_for_work_alone(job, work_id="github_issue:eerovil/agentdeck#8")
    ordinary = credited_head_for_work_alone({**job, "purpose": ""}, work_id=WORK)
    assert credited_head_for_work(job, work_id=WORK) == "", "a card never takes a Run-less job"
    assert (credited, other, ordinary) == (HEAD, "", "")


def test_the_queue_takes_verification_for_a_work_alone(tmp_path):
    import sys

    from agentdeck.jobs import VERIFICATION, JobQueue

    pool = tmp_path / "slots"
    pool.mkdir()
    queue = JobQueue(state_dir=tmp_path / "jobs", slot_paths=[pool / "slot-0"])
    job = queue.submit(
        command=[sys.executable, "-c", "print(1)"],
        cwd=str(tmp_path),
        purpose=VERIFICATION,
        work_id=WORK,
    )
    assert job.work_id == WORK and job.run_id == ""


# --- the router hands the release what it acts on ---


async def test_the_router_hands_a_reviewer_verdict_and_pull_events_to_the_release(tmp_path):
    from agentdeck.github_webhook_log import Repository, WebhookLog
    from agentdeck.webhook_tailer import WebhookTailer
    from agentdeck.work_router import WorkRouterService

    calls = []

    class Release:
        async def on_verdict(self, *args, by=None):
            calls.append(("verdict", *args, by))
            return "ok"

        async def on_pull_event(self, *args):
            calls.append(("pull", *args))

        async def evaluate(self, work_id):
            calls.append(("evaluate", work_id))

        async def on_label(self, work_id, label, added):
            calls.append(("label", work_id, label, added))

        async def on_status(self, repository, sha):
            calls.append(("status", repository, sha))

        async def on_unreadable_verdict(self, work_id, where, *, by):
            calls.append(("unreadable", work_id, where, by))
            return "told"

    root = tmp_path / "github-webhooks"
    repo = Repository(owner="eerovil", name="agentdeck")

    def deliver(delivery, event, **fields):
        payload = {"repository": {"full_name": repo.slug}, **fields}
        WebhookLog(root).append(
            body=json.dumps(payload).encode(),
            headers={"X-GitHub-Delivery": delivery, "X-GitHub-Event": event},
        )

    reviewer = {"login": "review-bot[bot]", "type": "Bot"}
    # Two verdicts in one comment are each taken (#3161), on the issue as on a pull request.
    body = (
        f"/reviewed pass {HEAD[:12]} {DIGEST} eerovil/agentdeck#9\n"
        f"/reviewed changes-needed {OTHER[:12]} eerovil/other-repo#21\n<!-- chatgpt-review -->"
    )
    deliver(
        "d0",
        "issue_comment",
        action="created",
        sender=reviewer,
        issue={"number": 7},
        comment={"body": body},
    )
    deliver(
        "d00",
        "issue_comment",
        action="created",
        sender=reviewer,
        issue={"number": 7},
        comment={"body": body + "\nLooks good.", "html_url": "https://x/7#c"},
    )
    body = f"/reviewed pass {HEAD[:12]} {DIGEST}\n<!-- chatgpt-review -->"
    deliver(
        "d1",
        "issue_comment",
        action="created",
        sender=reviewer,
        issue={"number": 9, "pull_request": {}},
        comment={"body": body},
    )
    deliver(
        "d2",
        "pull_request",
        action="closed",
        sender={"login": "x", "type": "User"},
        pull_request={"number": 9, "merged": True, "state": "closed", "head": {"sha": HEAD}},
    )
    deliver("d3", "check_suite", action="completed", check_suite={"pull_requests": [{"number": 9}]})
    deliver("d4", "issues", action="labeled", issue={"number": 7}, label={"name": "paused"})
    deliver("d5", "status", sha=OTHER, state="pending")
    deliver("d6", "status", sha=HEAD.upper(), state="success")
    service = WorkRouterService(
        root,
        switched=lambda repository: True,
        owner_of_issue=lambda repository, number: (
            ("chat-a", "eerovil/agentdeck#7") if number == 7 else None
        ),
        owner_of_pull=lambda repository, number: ("chat-a", "eerovil/agentdeck#7"),
        schedule=lambda message: message,
        start_review=lambda pull, work: None,
        own_logins=frozenset({"eerovil-agent"}),
        release=Release(),
        reviewed=SimpleNamespace(__contains__=lambda key: True),
    )
    await service.apply(list(WebhookTailer(root, consumer="t").deliveries()))
    assert calls[:4] == [
        ("verdict", WORK, "pass", HEAD[:12], DIGEST, ("agentdeck#9",), reviewer["login"]),
        ("verdict", WORK, "changes-needed", OTHER[:12], "", ("other-repo#21",), reviewer["login"]),
        ("unreadable", WORK, "https://x/7#c", reviewer["login"]),
        ("verdict", WORK, "pass", HEAD[:12], DIGEST, (PR,), reviewer["login"]),
    ]
    assert ("pull", WORK, PR, "closed", True) in calls
    assert ("evaluate", WORK) in calls
    assert ("label", WORK, "paused", True) in calls
    assert [call for call in calls if call[0] == "status"] == [
        ("status", "eerovil/agentdeck", HEAD)
    ], "a finished status is looked at; a pending one is not"


def test_merge_on_a_work_the_deck_releases_is_a_deck_merge(tmp_path):

    from test_work_actions import GREEN, _passed, _record, _work

    from agentdeck.work_actions import plan
    from agentdeck.work_attention import operator_attention
    from agentdeck.work_story import work_status

    work = _work(tmp_path, _record("Ready to merge", **_passed()), verifications=GREEN)
    step = plan("merge", work, operator_attention(work, work_status(work)), deck_release=True)
    assert step.kind == "deck_merge"


def test_a_chat_bound_to_a_work_alone_may_submit_verification():
    from agentdeck.__main__ import _job_run
    from agentdeck.job_client import JobClientError

    args = SimpleNamespace(verification=True)
    assert _job_run(args, {"work_id": WORK, "run_id": ""}) == (WORK, "")
    with pytest.raises(JobClientError, match="not part of any poller Run"):
        _job_run(args, {"work_id": "", "run_id": ""})


async def test_the_deck_reviewers_verdict_reaches_the_release(fake):
    from agentdeck.web.routes_works import _deck_released_verdict

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(work_release=fake.service)))
    command = f"/reviewed pass {HEAD[:12]} {DIGEST} agentdeck#9"
    # A second line is a second verdict, answered beside the first (#3161).
    both = f"{command}\n/reviewed changes-needed {OTHER[:12]} eerovil/other#3"
    receipt = await _deck_released_verdict(request, "Eerovil/AgentDeck", 7, both)
    assert "review pass recorded" in receipt and fake.merged == [(PR, "merge")]
    assert "; the verdict names no single pull request" in receipt
    assert await _deck_released_verdict(request, "eerovil/agentdeck", 7, "/claude fix") is None
    fake.switched = False
    assert await _deck_released_verdict(request, "eerovil/agentdeck", 7, command) is None


async def test_the_deck_reviewers_findings_reach_the_works_chat_once(fake):
    """Findings on a Work the deck runs go to its agent, at most once per request (#3004)."""
    url = "https://github.com/eerovil/agentdeck/issues/7#issuecomment-1"
    for _ in range(2):
        assert await fake.service.on_findings(WORK, "abcdefgh12", "Review of #9: fix x", url)
    assert fake.entry["findings"]["told"] is True  # read by the Work's line (#3097)
    assert [m for _c, m, _t in fake.told] == [f"work-release:{WORK}:findings:abcdefgh12"]
    chat, _id, text = fake.told[0]
    assert chat == "chat-a" and "Review of #9: fix x" in text and url in text
    assert not fake.merged


async def test_findings_the_chats_cli_would_drop_are_sent_as_a_link(fake):
    fake.service.ports.refusal = lambda chat, text: "drops it" if "Stuck?" in text else None
    url = "https://github.com/eerovil/agentdeck/issues/7#issuecomment-2"
    assert await fake.service.on_findings(WORK, "abcdefgh13", "Stuck? fix x", url)
    text = fake.told[0][2]
    assert "Stuck?" not in text and url in text
    assert OWNER_CALL_ASK in text  # #3314: the link form still says to ask the owner


async def test_findings_with_no_chat_to_hear_them_say_so(fake):
    fake.service.ports.chat = lambda work_id: None
    assert not await fake.service.on_findings(WORK, "abcdefgh14", "fix x", "https://x")
    assert fake.told == [] and fake.entry["findings"]["told"] is False


async def test_a_deck_reviewers_verdict_lands_when_its_caller_hangs_up(fake):
    """A reviewer's client that gives up cancels the request; the pass is recorded anyway (#2963).

    On 2026-10-12/13 the verdict's GitHub reads outlasted the client's 30 s, the hang-up
    cancelled them, and four clean reviews ended labelled blocked with nothing recorded.
    """
    import asyncio

    from agentdeck.web.routes_works import _deck_released_verdict

    reading = asyncio.Event()
    slow = asyncio.Event()
    plain_read = fake.read_pull

    async def read_pull(ref):
        reading.set()
        await slow.wait()
        return await plain_read(ref)

    fake.service.ports.read_pull = read_pull
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(work_release=fake.service)))
    command = f"/reviewed pass {HEAD[:12]} {DIGEST} agentdeck#9"
    caller = asyncio.create_task(_deck_released_verdict(request, "eerovil/agentdeck", 7, command))
    await reading.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    slow.set()
    for _ in range(50):
        if fake.merged:
            break
        await asyncio.sleep(0)
    assert fake.merged == [(PR, "merge")]


# --- the round-3 review fixes ---


def release_policy(**slot):
    from agentdeck.poller_engine.authorization import parse_policy

    return Policy(auto_release=True, authorization=parse_policy(slot))


async def test_a_merging_verdict_needs_a_release_tier_login(fake):
    fake.policy = release_policy(release={"allow": ["review-bot[bot]"]})
    answer = await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, by="someone")
    assert "no Release tier" in answer and fake.merged == []
    answer = await fake.service.on_verdict(
        WORK, "pass", HEAD[:12], DIGEST, by="Review-Bot[bot]"
    )
    assert "review pass recorded" in answer and fake.merged == [(PR, "merge")]


@pytest.mark.parametrize(
    ("login", "verdict", "counts"),
    [
        ("review-bot[bot]", "pass", "review pass recorded"),
        ("review-bot[bot]", "changes-needed", "changes needed"),
        ("someone", "pass", "no Work tier"),
    ],
)
async def test_a_verdict_needs_only_work_where_a_person_still_merges(fake, login, verdict, counts):
    """#3157: Trailhead's reviewer holds Work only, and with ``auto_release`` off its pass cannot
    merge, so the Poller takes it at Work (#2375) and the deck must too."""
    from agentdeck.poller_engine.authorization import parse_policy

    fake.policy = Policy(
        authorization=parse_policy(
            {
                "work": {"role": "triage", "allow": ["review-bot[bot]"]},
                "release": {"role": "maintain"},
            }
        )
    )
    answer = await fake.service.on_verdict(WORK, verdict, HEAD[:12], DIGEST, by=login)
    assert counts in answer and fake.merged == []


@pytest.mark.parametrize(
    ("level", "counts"), [("admin", True), ("maintain", True), ("write", False), (None, False)]
)
async def test_release_tier_by_live_permission_at_the_role_bar(fake, level, counts):
    """A project naming no allow list: Release is ``role = "maintain"`` alone."""
    fake.policy = release_policy(release={"role": "maintain"})
    asked = []

    async def permission(repository, login):
        asked.append((repository, login))
        return level

    fake.service.ports.permission = permission
    answer = await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, by="eerovil")
    assert asked == [("eerovil/agentdeck", "eerovil")]
    assert ("review pass recorded" in answer) is counts


@pytest.mark.parametrize(
    ("login", "association", "level", "counts", "looked_up"),
    [
        ("eerovil", "OWNER", "admin", True, True),
        ("helper", "COLLABORATOR", "triage", True, True),
        ("stranger", "NONE", "read", False, False),  # no relationship: no lookup
        ("reader", "CONTRIBUTOR", "read", False, True),
        ("reader", "MEMBER", "read", False, True),
        ("eerovil", "OWNER", None, False, True),  # unreadable is not a grant
        ("review-bot[bot]", "NONE", None, True, False),
    ],
)
async def test_a_comment_author_holds_work_by_role_or_allowlist(
    fake, login, association, level, counts, looked_up
):
    """#2895 review: the router forwards a comment only from a Work-tier login."""
    fake.policy = release_policy(work={"role": "triage", "allow": ["review-bot[bot]"]})
    asked = []

    async def permission(repository, who):
        asked.append((repository, who))
        return level

    fake.service.ports.permission = permission
    assert await fake.service.holds_work("Eerovil/AgentDeck", login, association) is counts
    assert bool(asked) is looked_up


async def test_an_unswitched_repository_admits_no_comment_author(fake):
    fake.policy = Policy(auto_release=True)
    assert await fake.service.holds_work("eerovil/agentdeck", "eerovil", "OWNER") is False


async def test_no_authorization_slot_admits_only_the_deck_reviewer(fake):
    fake.policy = Policy(auto_release=True)
    assert "no Release tier" in await fake.service.on_verdict(
        WORK, "pass", HEAD[:12], DIGEST, by="eerovil"
    )
    assert "review pass recorded" in await fake.service.on_verdict(
        WORK, "pass", HEAD[:12], DIGEST, by=None
    )


async def test_a_merge_press_does_not_outlive_its_pull_requests(fake):
    fake.policy = Policy(auto_release=False)
    await passed(fake)
    fake.merge_answer = FAILED
    await fake.service.request_merge(WORK)
    assert fake.entry["asked"]["pulls"] == {PR: {"digest": DIGEST, "head": HEAD}}, (
        "a press stands while its merge has not landed"
    )
    fake.merge_answer = MERGED
    await fake.service.on_pull_event(WORK, PR, "synchronize", False)
    assert "asked" not in fake.entry, "cleared once what it was for landed"
    nxt = "eerovil/agentdeck#10"
    fake.pulls = {nxt: Pull(nxt, "OPEN", False, OTHER, "MERGEABLE", "CLEAN", "green")}
    fake.digests[(nxt, OTHER)] = DIGEST
    fake.green = {OTHER}
    await passed(fake, head=OTHER)
    assert (nxt, "merge") not in fake.merged, "the next pull request waits for its own press"


async def test_a_merge_press_covers_a_draft_open_when_it_was_pressed(fake):
    """#2901 with draft #2904, and #3320 D6: a press covers the draft open beside it, so the
    draft merges once it goes ready unchanged, with no second press."""
    fake.policy = Policy(auto_release=False)
    nxt = "eerovil/agentdeck#10"
    fake.pulls[nxt] = Pull(nxt, "OPEN", True, OTHER, "MERGEABLE", "CLEAN", "green")
    fake.digests[(nxt, OTHER)] = "e" * 64  # a press reads every open diff, drafts too (#3320)
    assert f"the deck counts {nxt}, {PR}, and it names none of them" in await passed(
        fake, targets=("other#1",)
    ), "#3127: a refusal names what the deck counts for the Work"
    await passed(fake, targets=(PR,))
    await fake.service.request_merge(WORK)
    assert fake.merged == [(PR, "merge")]
    assert set(fake.entry["asked"]["pulls"]) == {PR, nxt}, "kept for the draft it covered"
    fake.set(nxt, draft=False)
    fake.green = {OTHER}
    await passed(fake, head=OTHER, digest="e" * 64, targets=(nxt,))
    assert fake.merged == [(PR, "merge"), (nxt, "merge")], "no second press"
    assert "asked" not in fake.entry


async def test_with_no_reviewer_a_merge_press_is_the_review_pass(fake):
    """The public copy has no reviewer (#3623): a person's Merge press passes the diffs it was
    for, so the Work merges once its tests are green there, and a red head still waits."""
    fake.policy = Policy(auto_release=False)
    fake.service.ports = replace(fake.service.ports, press_is_review=True)
    fake.green = set()
    answer = await fake.service.request_merge(WORK)
    assert fake.entry["passes"][PR] == {
        "digest": DIGEST, "head": HEAD, "at": fake.now, "by": "a Merge press"
    }
    assert fake.merged == [] and "no review pass" not in answer
    fake.green = {HEAD}
    await fake.service.evaluate(WORK)
    assert fake.merged == [(PR, "merge")]


async def test_merge_when_ready_merges_once_the_same_diff_passes_and_is_green(fake):
    """#3320: pressed before the review, it needs no second press for the diff it was for."""
    fake.policy = Policy(auto_release=False)
    assert fake.service.approval(WORK) == "offered"
    answer = await fake.service.request_merge(WORK)
    assert "no review pass" in answer and fake.merged == []
    assert fake.service.approval(WORK) == "approved"
    assert fake.entry["hold"]["words"].startswith("Merge pressed; ")
    # A merge of the base: a new head with the same diff keeps the press and moves its head.
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = DIGEST
    await fake.service.evaluate(WORK)
    assert fake.entry["asked"]["pulls"][PR]["head"] == OTHER
    fake.green = {OTHER}
    await passed(fake, head=OTHER)
    assert fake.merged == [(PR, "merge")]
    assert "asked" not in fake.entry


async def test_merge_when_ready_is_cleared_by_a_changed_diff_or_a_new_pull_request(fake):
    fake.policy = Policy(auto_release=False)
    await fake.service.request_merge(WORK)
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = "e" * 64
    fake.green = {OTHER}
    await fake.service.evaluate(WORK)
    assert "asked" not in fake.entry and fake.service.approval(WORK) == "offered"
    line = fake.service.approval_cleared(WORK)
    assert line == f"Merge approval cleared: {PR} changed at {OTHER[:12]}."
    await passed(fake, head=OTHER, digest="e" * 64)
    assert fake.merged == [] and "awaiting_merge" in fake.entry, "it waits at Ready for merge"
    # A new press is for the new diff, and drops the cleared line; a pull request that joins
    # the Work after it clears it again.
    await fake.service.request_merge(WORK)
    assert fake.merged == [(PR, "merge")] and fake.service.approval_cleared(WORK) == ""
    nxt = "eerovil/agentdeck#10"
    fake.pulls = {PR: fake.pulls[PR], nxt: Pull(nxt, "OPEN", True, HEAD, "MERGEABLE", "CLEAN")}
    fake.set(PR, state="OPEN", head=HEAD)
    fake.digests[(nxt, HEAD)] = DIGEST
    fake.green = set()
    entry = fake.service.store.get(WORK)
    entry["asked"] = {PR: DIGEST}  # a press from before #3320 still counts
    fake.service.store.put(WORK, entry)
    assert fake.service.approval(WORK) == "approved"
    await fake.service.evaluate(WORK)
    assert fake.service.approval_cleared(WORK) == f"Merge approval cleared: {nxt} joined the Work."


async def test_a_withdrawn_merge_approval_waits_for_a_new_press(fake):
    fake.policy = Policy(auto_release=False)
    fake.digests[(PR, HEAD)] = DIFF_TOO_LARGE
    with pytest.raises(PressRefused, match="too large"):
        await fake.service.request_merge(WORK)
    assert "asked" not in fake.entry, "a diff that can never be compared takes no press"
    fake.digests[(PR, HEAD)] = DIGEST
    fake.green = set()
    # Two Merge when ready presses at once (#3320 review): the Work's lock records one.
    first, second = await asyncio.gather(
        fake.service.request_merge(WORK, fresh=True),
        fake.service.request_merge(WORK, fresh=True),
        return_exceptions=True,
    )
    assert isinstance(second, PressRefused) and not isinstance(first, Exception)
    assert (await fake.service.withdraw_merge(WORK)).startswith("Merge approval withdrawn")
    fake.green = {HEAD}
    await passed(fake)
    assert fake.merged == [] and fake.service.approval(WORK) == "offered"
    await fake.service.request_merge(WORK)
    assert fake.merged == [(PR, "merge")]
    assert await fake.service.withdraw_merge(WORK) == "already merged; nothing changed"


async def test_a_failed_branch_update_is_told_and_tried_again(fake):
    fake.set(PR, merge_state="BEHIND")
    fake.update_ok = False
    await passed(fake)
    await fake.service.evaluate(WORK)
    assert fake.updated == [PR, PR], "tried again on the next look"
    assert [m for _c, m, _t in problems(fake)] == [
        f"work-release:{WORK}:update-failed:{PR}@{HEAD[:12]}"
    ]


async def test_a_spec_only_work_is_never_merged(fake):
    fake.policy = Policy(auto_release=True, spec_only_label="spec-only")
    fake.labels = {"agent", "spec-only"}
    assert "carries spec-only" in (await passed(fake) and await fake.service.evaluate(WORK))
    # What holds it, in the words the Work's line uses, and since when it has (#3097).
    hold = fake.entry["hold"]
    assert (hold["words"], hold["person"]) == ("held: spec-only label", True)
    await fake.service.evaluate(WORK)
    assert fake.entry["hold"]["since"] == hold["since"]
    fake.labels = None
    assert "could not be read" in await fake.service.evaluate(WORK)
    fake.labels = {"agent"}
    await fake.service.evaluate(WORK)
    assert fake.merged == [(PR, "merge")]


async def test_a_work_whose_agent_is_mid_turn_waits_for_the_turn_to_end(fake):
    fake.busy = True
    await passed(fake)
    assert fake.merged == [] and fake.entry["waiting_turn"] is True
    await fake.service.on_tests()
    assert fake.merged == [], "still busy: not even looked at"
    fake.busy = False
    await fake.service.on_tests()
    assert fake.merged == [(PR, "merge")] and "waiting_turn" not in fake.entry


async def test_a_paused_label_on_the_issue_holds_and_calls_off(fake):
    fake.policy = Policy(auto_release=True, paused_label="paused")
    fake.labels = {"paused"}
    assert "carries paused" in (await passed(fake) and await fake.service.evaluate(WORK))
    assert fake.merged == []


def test_busy_reads_the_live_sessions_of_the_works_chats():
    from agentdeck.work_router import work_busy

    session = SimpleNamespace(thinking=False, stalled=False)
    state = SimpleNamespace(
        chat_by_id=lambda chat_id: None,
        current_session_for_chat=lambda chat: None,
        sessions={"chat-a": session},
        work_projection=lambda: SimpleNamespace(
            get=lambda ref: SimpleNamespace(memberships=(SimpleNamespace(chat_id="chat-a"),))
        ),
    )
    assert work_busy(state, WORK) is False
    session.thinking = True
    assert work_busy(state, WORK) is True
    session.thinking, session.stalled = False, True
    assert work_busy(state, WORK) is True


async def test_a_deck_pause_press_wakes_the_release(fake):
    from agentdeck.web.routes_works import _wake_release

    fake.paused = True
    assert "paused" in (await passed(fake) and await fake.service.evaluate(WORK))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(work_release=fake.service)))
    fake.paused = False
    await _wake_release(request, WORK)
    assert fake.merged == [(PR, "merge")], "a resume looks again"


# --- the conditions are plain, read on every wake (review round 7) ---


@pytest.mark.parametrize(
    ("merge_state", "behind_by", "updates"),
    [("CLEAN", 3, True), ("BEHIND", 0, True), ("CLEAN", 0, False)],
)
async def test_behind_is_read_as_the_poller_reads_it(fake, merge_state, behind_by, updates):
    fake.set(PR, merge_state=merge_state, behind_by=behind_by)
    await passed(fake)
    assert (fake.updated == [PR]) is updates
    assert (fake.merged == []) is updates


async def test_an_unreadable_comparison_never_merges(fake):
    fake.set(PR, behind_by=None)
    assert "could not be compared" in (await passed(fake) and await fake.service.evaluate(WORK))
    assert fake.merged == [] and fake.updated == []


# --- a review pass with nothing testing it (#2999) ---


def _verify(head=HEAD):
    return f"work-release:{WORK}:verify:{PR}@{head[:12]}"


def _moved(head=HEAD):
    return f"work-release:{WORK}:moved:{PR}@{head[:12]}"


def _unclean(head=HEAD):
    return f"work-release:{WORK}:unclean:{PR}@{head[:12]}"


def _pass(head=HEAD):
    return f"work-release:{WORK}:verdict:pass:{PR}@{head[:12]}"


async def test_a_pass_at_an_untested_head_tells_the_agent_to_verify_in_that_one_message(fake):
    fake.green = set()
    answered = []
    fake.service.on_review_answer = lambda chat, pulls: answered.append(pulls)
    await passed(fake)
    assert answered == [[PR]]  # an accepted pass ends the review wait (#3183)
    await fake.service.evaluate(WORK)
    assert [m for _c, m, _t in fake.told] == [_pass()], "one message, not two"
    assert "Run this Work's verification job" in fake.told[0][2]


async def test_a_pass_whose_verification_already_runs_is_not_told_to_verify(fake):
    fake.green = set()
    fake.verifying = True
    await passed(fake)
    assert [m for _c, m, _t in fake.told] == [_pass()]
    assert "verification job" not in fake.told[0][2]
    # The job failed: its agent heard that from the job, and is not told to run it again.
    fake.verifying = False
    fake.red = {HEAD}
    await fake.service.on_tests(jobs=True, chats=False)
    assert [m for _c, m, _t in fake.told] == [_pass()]
    assert fake.entry["waiting_tests"] and "needs_verify" not in fake.entry
    # A job cut off with no result: nothing tests that head now, so the agent hears once.
    fake.red = set()
    await fake.service.on_tests(jobs=True, chats=False)
    await fake.service.on_tests(jobs=True, chats=False)
    assert [m for _c, m, _t in fake.told] == [_pass(), _verify()]
    assert "passed at " + HEAD[:12] in fake.told[1][2]
    assert fake.entry["needs_verify"] is True
    assert fake.entry["hold"]["words"] == "no test run started"
    # #3097: choir-tracks-cf#46 passed twice at its head while a test rewrote a tracked file.
    # Neither run counts, and the line and the agent hear why, not "no test run started".
    fake.moved = {HEAD}
    answer = await fake.service.evaluate(WORK)
    assert answer.endswith("so they do not count") and fake.merged == []
    assert fake.entry["hold"]["words"] == (
        "tests passed but the tree changed during the run, so they don't count"
    )
    # In this order the call to verify was already spent: the agent hears why on its own, once.
    await fake.service.evaluate(WORK)
    assert [m for _c, m, _t in fake.told] == [_pass(), _verify(), _moved()]
    assert "the checkout changed while it ran" in fake.told[2][2]
    # #3397: score-plugins#444 passed twice at its head on a tree a disk-full pytest
    # had littered with untracked files. The line and the agent hear that, once.
    fake.unclean = {HEAD}
    answer = await fake.service.evaluate(WORK)
    await fake.service.evaluate(WORK)
    assert "uncommitted or untracked files" in answer and fake.merged == []
    assert fake.entry["hold"]["words"] == (
        "tests passed but the checkout had uncommitted or untracked files"
    )
    assert [m for _c, m, _t in fake.told] == [_pass(), _verify(), _moved(), _unclean()]
    assert "git status --porcelain -uall" in fake.told[3][2]
    # A red run at the same head is the answer then, not the green one that proved nothing.
    fake.red = {HEAD}
    answer = await fake.service.evaluate(WORK)
    assert "no green verification" in answer and "needs_verify" not in fake.entry


async def test_a_reviewed_head_gone_back_to_draft_or_closed_asks_for_no_verification(fake):
    """#3001 review: the next-step request reads ``needs_verify``; it must not outlive the
    pull request it was for."""
    fake.green = set()
    await passed(fake)
    assert fake.entry["needs_verify"] is True
    fake.set(PR, draft=True)
    await fake.service.on_pull_event(WORK, PR, "converted_to_draft", False)
    assert "needs_verify" not in fake.entry
    fake.set(PR, draft=False)
    await fake.service.evaluate(WORK)
    assert fake.entry["needs_verify"] is True
    fake.set(PR, state="CLOSED")
    await fake.service.on_pull_event(WORK, PR, "closed", False)
    assert "needs_verify" not in fake.entry


async def test_a_busy_agent_is_told_to_verify_once_its_turn_ends(fake):
    fake.green = set()
    fake.busy = True
    await passed(fake)
    assert [m for _c, m, _t in fake.told] == [_pass()]
    fake.busy = False
    await fake.service.on_tests(jobs=False, chats=True)
    assert [m for _c, m, _t in fake.told] == [_pass(), _verify()]
    # A new reviewed head is a new message.
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = "e" * 64
    await passed(fake, head=OTHER, digest="e" * 64)
    assert [m for _c, m, _t in fake.told][2:] == [_pass(OTHER)]
    assert "verification job" in fake.told[2][2]


async def test_a_repeat_pass_at_a_head_told_while_held_still_tells_the_agent_to_verify(fake):
    """#3001 review: the first pass was told while paused, so it carried no call to verify."""
    fake.green = set()
    fake.paused = True
    await passed(fake)
    assert [m for _c, m, _t in fake.told] == [_pass()]
    assert "verification job" not in fake.told[0][2]
    fake.paused = False
    await passed(fake)
    assert [m for _c, m, _t in fake.told] == [_pass(), _verify()]
    await passed(fake)
    assert len(fake.told) == 2, "once per head"


def test_only_this_works_unfinished_verification_counts_as_verifying():
    def job(**fields):
        return {"work_id": WORK, "purpose": "verification", "state": "running", **fields}

    assert verifies([job()], WORK)
    assert verifies([job(state="queued", work_id=WORK.upper())], WORK)
    assert not verifies([job(work_id="github_issue:eerovil/agentdeck#8")], WORK)
    assert not verifies([job(state="done")], WORK)
    assert not verifies([job(purpose="")], WORK)
    assert not verifies([job(work_id="")], "")


# --- a wake looks only where it could release (review round 8) ---


async def test_a_chat_change_does_not_reread_a_work_waiting_on_its_tests(fake):
    fake.green = set()
    await passed(fake)
    reads = []
    real = fake.read_pull

    async def counted(ref):
        reads.append(ref)
        return await real(ref)

    fake.service.ports.read_pull = counted
    await fake.service.on_tests(jobs=False, chats=True)
    assert reads == [], "a chat moving cannot turn a suite green"
    fake.green = {HEAD}
    await fake.service.on_tests(jobs=True, chats=False)
    assert fake.merged == [(PR, "merge")]


async def test_a_job_does_not_reread_a_work_waiting_on_its_turn(fake):
    fake.busy = True
    await passed(fake)
    fake.busy = False
    await fake.service.on_tests(jobs=True, chats=False)
    assert fake.merged == []
    await fake.service.on_tests(jobs=False, chats=True)
    assert fake.merged == [(PR, "merge")]


async def test_only_the_hold_that_stops_it_now_leaves_its_mark(fake):
    fake.green = set()
    await passed(fake)
    assert fake.entry["waiting_tests"] is True
    fake.paused = True
    await fake.service.evaluate(WORK)
    assert "waiting_tests" not in fake.entry, "a pause stops it now, not its tests"


async def test_the_watch_tells_jobs_from_chats_even_when_the_queue_overflowed(fake):
    import asyncio

    from agentdeck.events import EventBus

    seen = []
    gate = asyncio.Event()

    async def on_tests(*, jobs, chats):
        seen.append((jobs, chats))
        await gate.wait()  # a slow look while events pile up

    fake.service.on_tests = on_tests
    bus = EventBus(maxsize=4)
    task = asyncio.create_task(fake.service.watch_jobs(bus))
    await asyncio.sleep(0)
    bus.publish("sessions")
    await asyncio.sleep(0.01)
    bus.publish("jobs")  # dropped from the full queue by the burst after it
    for _ in range(10):
        bus.publish("sessions")
    gate.set()
    await asyncio.sleep(0.01)
    task.cancel()
    assert seen[0] == (False, True)
    assert seen[1] == (True, True), "the jobs move is read from the revisions"


# --- drafts and a partly read board (review round 9) ---


async def test_a_paused_work_merges_nothing_and_a_lone_draft_waits(fake):
    fake.paused = True
    await passed(fake)
    assert fake.merged == []
    fake.paused = False
    fake.set(PR, draft=True)
    assert await fake.service.evaluate(WORK) == "only drafts are open"
    assert fake.merged == []


async def test_a_draft_next_step_does_not_hold_the_ready_pull_request(fake):
    nxt = "eerovil/agentdeck#10"
    fake.pulls[nxt] = Pull(nxt, "OPEN", True, OTHER)
    await passed(fake, targets=(PR,))
    assert fake.merged == [(PR, "merge")], "the draft is not yet offered, so it holds nothing"


async def test_a_draft_in_the_reviewers_repository_still_holds(fake):
    fake.pulls[CHAT_PR] = Pull(CHAT_PR, "OPEN", True, OTHER)
    fake.policy = Policy(auto_release=True, review_only=frozenset({"eerovil/review-chats"}))
    await passed(fake, targets=(PR,))
    assert fake.merged == [], "cross-repo order stands"


async def test_the_release_records_when_only_a_persons_merge_is_missing(fake):
    """#2960: the Work page reads this as Ready for merge, at exactly these heads."""
    from agentdeck.work_release import AWAITING_MERGE

    fake.policy = Policy(auto_release=False)
    await passed(fake)
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert fake.entry["awaiting_merge"] == {PR: HEAD}
    # Its agent is asked once to bring the summary's evidence up to date (#3199).
    ready = [text for _chat, key, text in fake.told if ":ready:" in key]
    assert len(ready) == 1 and f"{PR}@{HEAD[:12]}" in ready[0] and "correctness" in ready[0]
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert len([key for _chat, key, _text in fake.told if ":ready:" in key]) == 1
    # Anything else holding it clears the record: here, a push past the reviewed head.
    fake.set(PR, head=OTHER)
    assert await fake.service.evaluate(WORK) != AWAITING_MERGE
    assert "awaiting_merge" not in fake.entry


async def test_a_reviewed_work_is_looked_at_once_at_start(fake):
    """#2960: a Work reviewed and tested before a restart reads Ready for merge without
    waiting for its next event."""
    from agentdeck.work_release import AWAITING_MERGE

    fake.policy = Policy(auto_release=False)
    await passed(fake)
    entry = fake.entry
    entry.pop("awaiting_merge", None)
    fake.service.store.put(WORK, entry)
    await fake.service.look_at_reviewed()
    assert fake.entry["awaiting_merge"] == {PR: HEAD}
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE


async def test_the_start_sweep_and_a_status_log_what_holds_a_reviewed_work(fake, caplog):
    """#3080: a reviewed Work that stayed off Ready for merge left nothing in the journal; here
    the spec-only label holds it, as it held trailhead's issues#676."""
    fake.policy = Policy(
        auto_release=False,
        github_checks=frozenset({"eerovil/agentdeck"}),
        spec_only_label="spec-only",
    )
    fake.labels = {"spec-only"}
    await passed(fake)
    caplog.set_level("INFO", logger="agentdeck.work_release")
    await fake.service.look_at_reviewed()
    await fake.service.on_status("eerovil/agentdeck", HEAD)
    lines = [r.getMessage() for r in caplog.records]
    held = "the issue carries spec-only, so nothing merges"
    assert f"work-release: {WORK} at start: {held}" in lines
    assert f"work-release: {WORK} on a status of eerovil/agentdeck@{HEAD[:12]}: {held}" in lines
    assert "awaiting_merge" not in fake.entry


async def test_passes_are_dropped_once_no_pull_request_is_open(fake):
    """#2960 review: kept passes made every start read every pull request ever reviewed."""
    fake.policy = Policy(auto_release=False)
    await passed(fake)
    assert fake.entry.get("passes")
    fake.set(PR, state="MERGED")
    assert await fake.service.evaluate(WORK) == "no open pull request"
    assert "passes" not in fake.entry


async def test_an_unknown_pull_request_list_keeps_the_passes(fake):
    """#2982: a fresh web process that has not scanned the Work's chats reads no pull
    requests; that is no answer, so the pass survives the restart and still merges."""
    from agentdeck.work_release import AWAITING_MERGE

    fake.policy = Policy(auto_release=False)
    await passed(fake)
    before = dict(fake.entry)
    known, fake.pulls = fake.pulls, {}
    await fake.service.look_at_reviewed()
    assert fake.entry == before
    # #3127: a verdict then says the deck counts none, so it reads as the deck's problem.
    assert "counts no pull request for this Work, which is a deck problem" in await passed(fake)
    fake.pulls = known
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert fake.entry["passes"][PR]["head"] == HEAD


async def test_the_start_sweep_waits_for_the_chats_to_be_scanned(fake):
    """#2982: before the first scan a Work reads no pull requests, so the sweep waits."""
    fake.policy = Policy(auto_release=True)
    fake.green = set()
    await passed(fake)
    fake.green = {HEAD}
    known, fake.pulls = fake.pulls, {}

    async def scanned():
        fake.pulls = known

    await fake.service.look_at_reviewed(scanned)
    assert [ref for ref, _method in fake.merged] == [PR]


async def test_only_the_passes_of_landed_pull_requests_are_spent(fake):
    """#2982: a pull request missing from a partial list keeps its pass."""
    fake.policy = Policy(auto_release=False)
    await passed(fake)
    entry = fake.entry
    entry["passes"][CHAT_PR] = {"head": OTHER, "digest": DIGEST, "at": 1.0}
    fake.service.store.put(WORK, entry)
    fake.set(PR, state="MERGED")
    assert await fake.service.evaluate(WORK) == "no open pull request"
    assert set(fake.entry["passes"]) == {CHAT_PR}


async def test_a_failed_read_keeps_ready_for_merge(fake):
    """#2960 review: a transient read failure is no answer, so Ready for merge stands rather
    than turning the Work into an ending its agent is asked about."""
    from agentdeck.work_release import AWAITING_MERGE

    fake.policy = Policy(auto_release=False)
    await passed(fake)
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    real = fake.service.ports.read_pull

    async def unreadable(ref):
        return None

    fake.service.ports = replace(fake.service.ports, read_pull=unreadable)
    assert "could not be read" in await fake.service.evaluate(WORK)
    assert fake.entry["awaiting_merge"] == {PR: HEAD}
    fake.service.ports = replace(fake.service.ports, read_pull=real)


# --- the verdict reaches the Work's chat (#2992) ---


async def test_a_recorded_pass_is_told_once_naming_the_pull_head_and_review_chat(fake):
    fake.policy = Policy(auto_release=False)
    fake.service.ports.review_chat = lambda pull, head: (
        "eerovil/review-chats#5166" if (pull, head) == (PR, HEAD) else None
    )
    await passed(fake)
    await passed(fake)  # a resent verdict is the same pass: not told again
    verdicts = [told for told in fake.told if ":verdict:" in told[1]]
    assert [m for _c, m, _t in verdicts] == [f"work-release:{WORK}:verdict:pass:{PR}@{HEAD[:12]}"]
    text = verdicts[0][2]
    assert PR in text and HEAD[:12] in text and "eerovil/review-chats#5166" in text
    assert "passed clean" in text and "waiting for a person's Merge" in text
    assert fake.entry["passes"][PR]["chat"] == "eerovil/review-chats#5166"
    assert fake.entry["passes"][PR]["at"] == fake.now
    # What a review asked for before this pass reads to drop its retry (#3324).
    assert fake.service.passed_since(WORK, PR.upper(), HEAD[:12], fake.now)
    assert not fake.service.passed_since(WORK, PR, HEAD, fake.now + 1)
    assert not fake.service.passed_since(WORK, PR, "b" * 40, fake.now)
    # The head reviewed again on request (#3183) passes again in a new chat: told, once, and
    # only that message ends the agent's review wait.
    answered = []
    fake.service.on_review_answer = lambda chat, pulls: answered.append(pulls)
    fake.service.ports.review_chat = lambda pull, head: "eerovil/review-chats#5170"
    await passed(fake)
    await passed(fake)
    verdicts = [told for told in fake.told if ":verdict:" in told[1]]
    assert [m for _c, m, _t in verdicts][1:] == [
        f"work-release:{WORK}:verdict:pass:{PR}@{HEAD[:12]}@eerovil/review-chats#5170"
    ]
    assert "eerovil/review-chats#5170" in verdicts[1][2]
    assert answered == [[PR], [PR]]


async def test_a_verdict_on_a_diff_too_large_to_read_is_refused(fake):
    """#3029: the port's marker for a diff GitHub never serves is no digest to match."""

    async def too_large(ref, head):
        return DIFF_TOO_LARGE

    fake.service.ports.digest = too_large
    assert "digest does not match" in await passed(fake, digest=DIFF_TOO_LARGE)
    assert "passes" not in fake.entry


async def test_an_unreadable_verdict_is_told_once_from_a_reviewer_only(fake):
    """#3161: a malformed ``/reviewed`` comment used to leave the Work waiting in silence."""
    fake.policy = release_policy(release={"allow": ["review-bot[bot]"]})
    url = "https://github.com/eerovil/agentdeck/issues/7#issuecomment-3"
    assert "may not" in await fake.service.on_unreadable_verdict(WORK, url, by="someone")
    assert fake.told == []
    for _ in range(2):
        await fake.service.on_unreadable_verdict(WORK, url, by="review-bot[bot]")
    assert [m for _c, m, _t in fake.told] == [f"work-release:{WORK}:verdict:unreadable:{url}"]
    assert "could not be read" in fake.told[0][2] and url in fake.told[0][2]


async def test_changes_needed_is_told_and_a_refused_verdict_is_not(fake):
    answered = []
    fake.service.on_review_answer = lambda chat, pulls: answered.append((chat, pulls))
    assert "digest does not match" in await passed(fake, digest="e" * 64)
    assert fake.told == []
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], DIGEST)
    # A verdict taken ends the chat's review waits without a message of their own; the
    # refused pass before it left them to their timeout (#3183).
    assert answered == [(fake.told[0][0], [PR])]
    assert [m for _c, m, _t in fake.told] == [
        f"work-release:{WORK}:verdict:changes-needed:{PR}@{HEAD[:12]}"
    ]
    assert "asks for changes" in fake.told[0][2] and "by the deck's reviewer" in fake.told[0][2]
    # Findings the store would not keep leave the wait, and are told again next time.
    ports = fake.service.ports
    fake.service.ports = replace(ports, tell=lambda chat, message_id, text: False)
    assert not await fake.service.on_findings(WORK, "req-1", "1. off by one", "https://x/1")
    assert len(answered) == 1
    assert fake.service.store.get(WORK)["findings"]["told"] is False
    fake.service.ports = ports
    assert await fake.service.on_findings(WORK, "req-1", "1. off by one", "https://x/1")
    assert fake.service.store.get(WORK)["findings"]["told"] is True
    assert answered[1:] == [(fake.told[0][0], None)]
    assert fake.told[-1][1] == f"work-release:{WORK}:findings:req-1"
    # Findings about a commit the branch has left are dropped, and nothing is told (#3192).
    told = len(fake.told)
    stale = await fake.service.on_verdict(WORK, "changes-needed", OTHER[:12], DIGEST)
    assert verdict_settled(stale) and "moved past" in stale
    assert len(fake.told) == told
    assert verdict_settled(await passed(fake)) and not verdict_settled("not released by the deck")
    # Findings comments are checked the same way, by the head their heading names (#3192).
    assert findings_target("Review findings for PR #9 at `ABCDEF1`: x", "Eerovil/AgentDeck") == (
        "eerovil/agentdeck#9", "abcdef1"
    )
    assert findings_target("/claude Review of agentdeck#9 at abcdef1", "eerovil/x") == (
        "eerovil/agentdeck#9", "abcdef1"
    )
    assert findings_target("Review of o/r#9: no head named", "eerovil/x") is None
    stale = fake.service.stale_findings
    assert "moved past" in await stale(WORK, f"Review of {PR} at {OTHER[:9]}", "")
    assert await stale(WORK, f"Review of {PR} at {HEAD[:9]}", "") == ""
    assert await stale(WORK, f"Review of {PR} at abc", "") == ""
    # A pull request that is not this Work's is never read as its own.
    assert await stale(WORK, f"Review of #9 at {OTHER[:9]}", "eerovil/issues") == ""


async def test_a_verdict_about_a_head_left_only_by_a_base_merge_still_counts(fake):
    """#3314: the router reviews no new head it proved a base merge away, so the old head's
    verdict is the one that stands, findings and pass alike."""
    proven = {(PR, HEAD, OTHER[:12])}
    taken = []
    fake.service.ports.same_change = lambda pull, head, older: (pull, head, older) in proven
    fake.service.ports.verdict_taken = lambda pull, head: taken.append((pull, head))
    assert await fake.service.stale_findings(WORK, f"Review of {PR} at {OTHER[:12]}", "") == ""
    # Findings handed on answer their head: a base merge after them may keep them.
    assert taken == [(PR, OTHER[:12])]
    # Not proven (a push, or the same lines moved elsewhere): moved past, and no pass.
    stale = await fake.service.stale_findings(WORK, f"Review of {PR} at {'c' * 12}", "")
    assert "moved past" in stale
    assert "not open at" in await passed(fake, head="c" * 40)
    assert "digest does not match" in await passed(fake, head=OTHER, digest="e" * 64)
    await passed(fake, head=OTHER)
    assert fake.entry["passes"][PR]["head"] == HEAD
    assert taken[-1] == (PR, OTHER[:12])


async def test_a_no_code_verdict_holds_the_merge_and_its_fix_passes_with_no_review(fake):
    """#3322: findings that need no code change hold the merge, and once they are fixed the
    review that asked for them counts as a pass at the fixed head."""
    files = {"src/a.py", "docs/a.md"}

    async def pull_files(pull):
        return set(files)

    fake.service.ports.pull_files = pull_files
    # A digest the diff does not have holds as changes-needed: its fix is reviewed again.
    answer = await fake.service.on_verdict(WORK, "no-code", HEAD[:12], "e" * 64)
    assert "reviewed again" in answer and fake.service.no_code_head(WORK, PR) is None
    assert "asks for changes" in fake.told[-1][2]
    answer = await fake.service.on_verdict(WORK, "no-code", HEAD[:12], DIGEST)
    assert answer == f"no-code fixes needed on {PR}" and fake.merged == []
    assert fake.told[-1][1] == f"work-release:{WORK}:verdict:no-code:{PR}@{HEAD[:12]}"
    assert "No new review runs" in fake.told[-1][2] and "`*.md`" in fake.told[-1][2]
    assert fake.service.no_code_head(WORK, PR) == (HEAD, frozenset(files))
    # The agent pushed doc files: the pass is at the new head, which merges once tested.
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = "e" * 64
    fake.green = {OTHER}
    assert "not open at" in await fake.service.no_code_fixed(WORK, PR, "c" * 40)
    answer = await fake.service.no_code_fixed(WORK, PR, OTHER)
    assert verdict_settled(answer) and fake.merge_heads == [OTHER]
    assert fake.entry["passes"][PR]["no-code"] == HEAD and "findings" not in fake.entry
    assert any(
        ":verdict:pass:" in key and "changed only doc files since" in text
        for _chat, key, text in fake.told
    )
    assert fake.service.no_code_head(WORK, PR) is None
    assert "no no-code verdict" in await fake.service.no_code_fixed(WORK, PR, OTHER)
    # Any other verdict on the pull request replaces a no-code one.
    fake.set(PR, state="OPEN")
    await fake.service.on_verdict(WORK, "no-code", OTHER[:12], "e" * 64)
    await fake.service.on_verdict(WORK, "changes-needed", OTHER[:12], "")
    assert fake.service.no_code_head(WORK, PR) is None


def test_the_review_chat_is_the_newest_one_opened_at_that_head():
    heads = {
        10: {("eerovil/agentdeck", 9): HEAD},
        12: {("eerovil/agentdeck", 9): HEAD, ("eerovil/other", 9): HEAD},
        14: {("eerovil/agentdeck", 9): OTHER},
        15: {("eerovil/other", 9): HEAD},
    }
    assert review_chat_of("eerovil/review-chats", heads, "Eerovil/AgentDeck#9", HEAD[:12]) == (
        "eerovil/review-chats#12"
    )
    assert review_chat_of("eerovil/review-chats", heads, PR, "c" * 40) is None
    assert review_chat_of("", heads, PR, HEAD) is None
    assert review_chat_of("eerovil/review-chats", heads, "eerovil/agentdeck", HEAD) is None


# --- a look stopped by a failed read is looked at again (#3023) ---


def timed(fake):
    """Run the release's timed looks on the fake clock; ``tick`` lets time pass."""
    timers: list[list] = []

    def later(delay, look):
        timer = [fake.now + delay, look, False]
        timers.append(timer)
        return SimpleNamespace(cancel=lambda: timer.__setitem__(2, True))

    async def tick(seconds):
        fake.now += seconds
        while due := [t for t in timers if not t[2] and t[0] <= fake.now]:
            for timer in due:
                timer[2] = True
                await timer[1]()

    fake.service.ports = replace(fake.service.ports, later=later)
    return tick, timers


async def test_unreadable_labels_are_looked_at_again_after_a_back_off(fake):
    """trailhead issues#670: a pass recorded while the labels could not be read never read
    Ready for merge, since nothing looked at the Work again."""
    from agentdeck.work_release import AWAITING_MERGE

    tick, _timers = timed(fake)
    fake.policy = Policy(auto_release=False, paused_label="paused")
    fake.labels = None
    await passed(fake)
    assert fake.entry["retry"] == {"since": 1000.0, "delay": 60.0}
    await tick(59)
    assert fake.entry["retry"]["delay"] == 60.0, "not due yet"
    await tick(1)
    assert fake.entry["retry"]["delay"] == 120.0, "the back-off grows"
    fake.labels = {"claude"}
    await tick(120)
    assert fake.entry["awaiting_merge"] == {PR: HEAD}
    assert "retry" not in fake.entry
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    # A day of failed reads gives up.
    fake.labels = None
    await fake.service.evaluate(WORK)
    fake.now += 24 * 3600
    await tick(1800)
    assert "retry" not in fake.entry


async def test_an_unreadable_pull_request_is_looked_at_again_only_once_reviewed(fake):
    tick, _timers = timed(fake)
    real = fake.service.ports.read_pull

    async def unreadable(ref):
        return None

    fake.service.ports = replace(fake.service.ports, read_pull=unreadable)
    assert "could not be read" in await fake.service.evaluate(WORK)
    assert "retry" not in fake.entry, "with no pass, a verdict is what looks again"
    fake.service.ports = replace(fake.service.ports, read_pull=real)
    fake.green = set()
    await passed(fake)
    fake.service.ports = replace(fake.service.ports, read_pull=unreadable)
    assert "could not be read" in await fake.service.evaluate(WORK)
    assert "retry" in fake.entry
    fake.service.ports = replace(fake.service.ports, read_pull=real)
    fake.green = {HEAD}
    await tick(60)
    assert fake.merged == [(PR, "merge")]
    assert "retry" not in fake.entry
    # An unreadable comparison with the base is a failed read too.
    fake.set(PR, state="OPEN", behind_by=None)
    await passed(fake)
    assert "retry" in fake.entry


async def test_a_changes_needed_verdict_during_a_failed_read_drops_the_retry(fake):
    """#3024 review: a mark left on a Work with no pass kept it being looked at."""
    tick, timers = timed(fake)
    fake.policy = Policy(auto_release=False, paused_label="paused")
    fake.labels = None
    await passed(fake)
    assert "retry" in fake.entry
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], DIGEST)
    await tick(60)
    assert "retry" not in fake.entry
    assert not [t for t in timers if not t[2]], "no look left waiting"


def test_a_failed_read_is_told_apart_with_a_merge_queue_place():
    from agentdeck.work_release import LABELS_UNREADABLE, _failed_read

    assert _failed_read(f"{LABELS_UNREADABLE} (merge queue 2 of 3)")
    assert _failed_read(f"{PR}'s diff at {HEAD[:12]} could not be read to carry its review pass")
    assert _failed_read(f"{PR} merged; {CHAT_PR} could not be compared with its base")
    assert not _failed_read(f"{PR} has no review pass at {HEAD[:12]} (merge queue 1 of 2)")
    assert not _failed_read("a Poller card holds this Work")
