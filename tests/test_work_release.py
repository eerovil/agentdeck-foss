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
from agentdeck.review_history import CHECK_NAME, ReviewEvent, newest_verdict
from agentdeck.review_refs import head_digest
from agentdeck.work_release import (
    ALREADY_MERGED,
    FAILED,
    HELD_PASSES,
    MERGED,
    OWNER_CALL_ASK,
    PASS_HELD,
    RETRY_FIRST_S,
    RETRY_MAX_AGE_S,
    RETRY_MAX_S,
    Policy,
    Ports,
    PressRefused,
    Pull,
    ReleaseStore,
    WorkReleaseService,
    checks_state,
    deploy_sentence,
    findings_target,
    heading_targets,
    hold_line,
    hold_words,
    merge_outcome,
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
        self.green_heads_fail = False
        self.labels: set[str] | None = set()
        self.update_ok = True
        self.verifying = False
        self.red: set[str] = set()
        self.moved: set[str] = set()
        self.unclean: set[str] = set()
        self.history: list[tuple] = []  # (pull, head, kind, key, data) each event (#3957)
        self.events: list[ReviewEvent] = []  # what ``reviews`` reads back (#3956)
        self.forgotten: list[str] = []
        self.merge_commit = ""  # what GitHub reads back as a merge's commit (#4253)
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
                review_event=self.note,
                reviews=lambda pull: [event for event in self.events if event.pull == pull],
                forget_reviews=self.forgotten.append,
            ),
        )

    def note(self, pull, head, kind, key, at, data):
        """The deck's ``review_events`` table: a stored key adds nothing (#3957)."""
        self.history.append((pull, head, kind, key, data, at))
        if all(event.key != key for event in self.events):
            seq = len(self.events) + 1
            self.events.append(ReviewEvent(pull, head.lower(), at, kind, key, data or {}, seq))

    def passed(self, ref=PR):
        """The newest verdict on ``ref`` at any head when it is a pass, else ``None`` (#3956)."""
        verdict = newest_verdict(event for event in self.events if event.pull == ref)
        return verdict if verdict is not None and verdict.kind in ("pass", "accepted") else None

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
        if self.green_heads_fail:
            raise OSError("no answer")
        return set(self.green)

    async def merge(self, ref, method, head):
        self.merged.append((ref, method))
        self.merge_heads.append(head)
        if self.merge_answer == MERGED:
            self.set(ref, state="MERGED", merge_commit=self.merge_commit)
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
    assert "keep the issue open and carry on" in text  # #3905
    assert "left for a person" in text and "your reply's last paragraph" in text  # #4151
    # #4210: no close on unit tests alone, a 3-day cap, a read-only Mac command last, and a
    # check only a person can make rests as Waiting for a try.
    assert "Unit tests and a deploy alone" in text and "3 days" in text
    assert "read-only Mac command" in text and "last resort" in text
    assert "`needs_person`" in text and "trying it is not such a step" in text
    # #3220: each pull request is told how it goes live, not the deck's way for all of them.
    said = deploy_sentence(Policy(), ["eerovil/agentdeck#3196", "eerovil/other-tool#7"])
    assert said.startswith("eerovil/agentdeck#3196: This deck deploys it")
    assert "eerovil/other-tool#7: Nothing on this host watches" in said
    assert "deploy by hand is `not_live`" in said, "not a nothing_to_check and a Finish"


MERGE_SHA = "e" * 40


def waiting_deploys(fake, ok=True):
    """The release's ``wait_deploy`` port (#4253): each call it makes, answered ``ok``."""
    waits = []

    def wait_deploy(chat, refs, bodies, then):
        waits.append((chat, bodies, then))
        return ok

    fake.service.ports.wait_deploy = wait_deploy
    return waits


async def test_a_merge_the_deck_deploys_registers_the_deploy_wait_instead_of_asking(fake):
    """#4253: the deck registers the wait the live-check message asked for, carrying that
    request, so the chat wakes once, when it is live; GitHub's event asks nothing more."""
    waits = waiting_deploys(fake)
    fake.merge_commit = MERGE_SHA
    await passed(fake)
    assert fake.merged == [(PR, "merge")] and live_checks(fake) == []
    [(chat, bodies, then)] = waits
    assert chat == "chat-a" and bodies == [{"deck_deploy": MERGE_SHA}]
    assert then.startswith(f"AgentDeck: {PR} merged.") and "/live-check" in then
    assert "The deck registered the wait for its deploy" in then and '"deck_deploy"' not in then
    assert fake.entry["live_check_asked"]["pulls"] == [PR]
    await fake.service.on_pull_event(WORK, PR, "closed", True)
    await fake.service.evaluate(WORK)
    assert len(waits) == 1 and live_checks(fake) == []


@pytest.mark.parametrize(
    "why", ["unwatched", "no merge commit", "refused", "two in one repo", "two watched repos"]
)
async def test_a_merge_the_deck_cannot_wait_on_is_told_as_before(fake, why):
    """#4253: a repository nothing on this host watches, a merge commit not read, a store that
    refused the wait, or two merges at once, in one repository or two watched ones (each wait
    would wake the chat to check both before the other is live): the chat waits itself."""
    site = "eerovil/site#4"
    waits = waiting_deploys(fake, ok=why != "refused")
    fake.policy = Policy(
        auto_release=False, deploys=frozenset({"eerovil/site", "eerovil/shop"})
    )
    refs = {
        "two in one repo": [site, "eerovil/site#5"],
        "two watched repos": [site, "eerovil/shop#6"],
    }.get(why, [site])
    if why == "unwatched":
        fake.policy = Policy(auto_release=False)
    fake.pulls = {
        ref: Pull(ref, "OPEN", False, HEAD, "MERGEABLE", "CLEAN", "green") for ref in refs
    }
    await fake.service.evaluate(WORK)
    for ref in refs:
        sha = "" if why == "no merge commit" else MERGE_SHA
        fake.set(ref, state="MERGED", merge_commit=sha)
        await fake.service.on_pull_event(WORK, ref, "closed", True)
    await fake.service.evaluate(WORK)
    [text] = live_checks(fake)
    assert "registered the wait" not in text and fake.entry["live_check_asked"]
    assert len(waits) == (1 if why == "refused" else 0)


async def test_a_merge_in_an_auto_deploying_repository_waits_on_that_deploy(fake):
    site = "eerovil/site#4"
    waits = waiting_deploys(fake)
    fake.policy = Policy(auto_release=False, deploys=frozenset({"eerovil/site"}))
    fake.pulls = {site: Pull(site, "OPEN", False, HEAD, "MERGEABLE", "CLEAN", "green")}
    await fake.service.evaluate(WORK)
    fake.set(site, state="MERGED", merge_commit=MERGE_SHA)
    await fake.service.on_pull_event(WORK, site, "closed", True)
    await fake.service.evaluate(WORK)
    assert live_checks(fake) == []
    [(_chat, bodies, _then)] = waits
    assert bodies == [{"deployment": f"eerovil/site@{MERGE_SHA}"}]


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
    again.events = fake.events  # the review history is the deck's database, not this store
    assert again.service.passed_since(WORK, PR, HEAD, 0.0)


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
    # #3993: one walk of the deck per status, however many Works the release keeps.
    for number in (1, 2, 3):
        fake.service.store.works.setdefault(f"github_issue:eerovil/agentdeck#{number}", {})
    walks = []
    fake.service.ports.pulls = None  # would fail if asked once per Work
    fake.service.ports.pulls_lookup = lambda: walks.append(1) or (
        lambda work_id: (PR,) if work_id == WORK else ()
    )
    await fake.service.on_status("eerovil/agentdeck", OTHER)
    await fake.service.on_status("eerovil/other", HEAD)
    assert fake.merged == [], "another commit or repository is not this Work's"
    assert len(walks) == 2
    fake.service.ports.pulls = lambda work_id: (PR,) if work_id == WORK else ()
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


@pytest.mark.parametrize("required", [True, False])
async def test_a_tested_work_with_no_checks_waits_only_where_they_are_required(fake, required):
    """#3919: a job that proves only part of the suite: CI that has not reported is no pass."""
    fake.policy = Policy(
        auto_release=True, checks_required=frozenset({"eerovil/agentdeck"} if required else ())
    )
    fake.set(PR, checks="none")
    await passed(fake)
    if not required:
        assert fake.merged == [(PR, "merge")]
        return
    assert fake.merged == []
    assert await fake.service.evaluate(WORK) == f"{PR}'s checks are none at {HEAD[:12]}"
    assert fake.entry.get("waiting_checks")
    fake.set(PR, checks="green")
    await fake.service.on_checks_done(WORK)
    assert fake.merged == [(PR, "merge")]


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
    # The deck's own review check is no CI, whatever it says (#4152).
    review = {"name": CHECK_NAME, "conclusion": "FAILURE"}
    assert checks_state([review]) == "none"
    assert checks_state([review, {"name": "build", "conclusion": "SUCCESS"}]) == "green"
    assert checks_state([{"name": CHECK_NAME, "status": "IN_PROGRESS"}]) == "none"
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
        "[capabilities]\nauto_release = true\n"
        '[authorization.release]\nallow = ["Review-Bot[bot]"]\n'
        '[[repos]]\nid = "agentdeck"\nholds_issues = true\n'
        '[[repos]]\nid = "review-chats"\nverification = "review_only"\n'
        '[[repos]]\nid = "site"\nauto_deploys = true\ngithub_checks_required = true\n'
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
    assert policy.checks_required == {"eerovil/site"} and policy.github_checks == set()
    # [staging] (#3180): plain-http links are dropped.
    assert policy.staging_repos == {"eerovil/site"}
    assert policy.staging.prefix == "staging-"
    assert policy.staging.links == (("Site", "https://{slug}.example/"),)
    assert policies["eerovil/review-chats"] is policy
    assert policy.auto_release_repos == set()
    # One repository's own switch (#4172), as the deck reads it from [[repos]].
    path.write_text(
        path.read_text()
        .replace("auto_release = true\n", "")
        .replace('id = "review-chats"\n', 'id = "review-chats"\nauto_release = true\n')
    )
    policy = release_policies(tmp_path)["eerovil/agentdeck"]
    assert not policy.auto_release and policy.auto_release_repos == {"eerovil/review-chats"}


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
    dated = []  # each verdict's history key and time (#3957)

    class Release:
        async def on_verdict(self, *args, by=None, source="", at=None):
            calls.append(("verdict", *args, by))
            dated.append((source, at))
            return "ok"

        async def on_pull_event(self, *args):
            calls.append(("pull", *args))

        async def evaluate(self, work_id):
            calls.append(("evaluate", work_id))

        async def on_checks_done(self, work_id):
            calls.append(("checks", work_id))

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
        comment={"body": body, "id": 31, "created_at": "2026-11-01T10:00:00Z"},
    )
    deliver(
        "d2",
        "pull_request",
        action="closed",
        sender={"login": "x", "type": "User"},
        pull_request={"number": 9, "merged": True, "state": "closed", "head": {"sha": HEAD}},
    )
    deliver("d3", "check_suite", action="completed", check_suite={"pull_requests": [{"number": 9}]})
    # A finished run too (#3911), but not one just created.
    deliver("d3b", "check_run", action="completed", check_run={"pull_requests": [{"number": 9}]})
    deliver("d3c", "check_run", action="created", check_run={"pull_requests": [{"number": 9}]})
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
    )
    await service.apply(list(WebhookTailer(root, consumer="t").deliveries()))
    assert calls[:4] == [
        ("verdict", WORK, "pass", HEAD[:12], DIGEST, ("agentdeck#9",), reviewer["login"]),
        ("verdict", WORK, "changes-needed", OTHER[:12], "", ("other-repo#21",), reviewer["login"]),
        ("unreadable", WORK, "https://x/7#c", reviewer["login"]),
        ("verdict", WORK, "pass", HEAD[:12], DIGEST, (PR,), reviewer["login"]),
    ]
    assert ("pull", WORK, PR, "closed", True) in calls
    # Keyed and dated by the comment itself, so a late delivery is not the newest (#3957).
    assert dated[-1] == ("comment:31", 1793527200.0)
    assert [call for call in calls if call[0] == "checks"] == [("checks", WORK)] * 2
    assert ("label", WORK, "paused", True) in calls
    assert [call for call in calls if call[0] == "status"] == [
        ("status", "eerovil/agentdeck", HEAD)
    ], "a finished status is looked at; a pending one is not"


def test_merge_on_a_work_the_deck_releases_is_a_deck_merge(tmp_path):

    from test_work_actions import _ready

    from agentdeck.work_actions import plan
    from agentdeck.work_attention import operator_attention
    from agentdeck.work_story import work_status

    work = _ready()
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
    assert fake.told == []


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


async def test_the_reviewers_refused_verdict_is_told_once_per_head(fake, monkeypatch):
    """#3901: holdfast never named its reviewer at Release, and the chat, told nothing, asked
    for three reviews. A stranger's refused ``/reviewed`` still reaches nobody."""
    from agentdeck.poller_engine import authorization

    monkeypatch.setattr(authorization, "reviewer_logins", lambda: frozenset({"review-bot[bot]"}))
    fake.policy = release_policy(release={"allow": ["owner"]})
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, by="someone")
    for _ in range(2):
        answer = await fake.service.on_verdict(
            WORK, "pass", HEAD[:12], DIGEST, by="Review-Bot[bot]"
        )
    assert "no Release tier" in answer and fake.merged == []
    assert [m for _c, m, _t in fake.told] == [
        f"work-release:{WORK}:verdict:untrusted:review-bot[bot]:{HEAD[:12]}"
    ]
    told = fake.told[0][2]
    assert "[authorization.release]" in told and "Merge press does not" in told


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
    ("verdict", "asked_at", "counts"),
    [
        ("pass", HEAD, "review pass recorded"),
        ("changes-needed", HEAD, "changes needed"),
        ("pass", OTHER, "no Release tier"),  # asked about another head
    ],
)
async def test_the_reviewer_counts_on_a_self_merging_repository_only_where_asked(
    fake, monkeypatch, verdict, asked_at, counts
):
    """#4187: the bot's verdict counts there at Work tier only for the head the deck sent it.
    No allowlist names it: being the ``[reviewers]`` account gives it Work (#2441)."""
    from agentdeck.poller_engine import authorization
    from agentdeck.poller_engine.authorization import parse_policy

    monkeypatch.setattr(authorization, "reviewer_logins", lambda: frozenset({"review-bot[bot]"}))
    fake.policy = Policy(
        auto_release_repos=frozenset({"eerovil/agentdeck"}),
        authorization=parse_policy({"work": {"role": "admin"}, "release": {"role": "admin"}}),
    )
    fake.note(PR, asked_at, "asked", "asked:1", 1.0, {})
    answer = await fake.service.on_verdict(WORK, verdict, HEAD[:12], DIGEST, by="review-bot[bot]")
    assert counts in answer
    assert fake.merged == ([(PR, "merge")] if counts == "review pass recorded" else [])


async def test_one_repository_merges_on_its_own_in_a_project_that_does_not(fake, monkeypatch):
    """#4172: A robot repository merges on a pass and green tests while store waits for a press,
    and the Work-tier reviewer's pass can merge neither: robot needs Release from a login."""
    from agentdeck.poller_engine import authorization
    from agentdeck.poller_engine.authorization import parse_policy
    from agentdeck.work_release import AWAITING_MERGE

    monkeypatch.setattr(authorization, "reviewer_logins", lambda: frozenset({"review-bot[bot]"}))
    fake.policy = Policy(
        auto_release_repos=frozenset({"eerovil/agentdeck"}),
        authorization=parse_policy(
            {
                "work": {"role": "triage", "allow": ["review-bot[bot]"]},
                "release": {"role": "maintain"},
            }
        ),
    )
    shown = [(PR, "open", False)]  # as the Work page shows them (``_shown_pulls``)
    assert fake.service.approval(WORK, shown) == "", "no Merge offered where none is needed"
    answer = await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, by="Review-Bot[bot]")
    assert "no Release tier" in answer and fake.merged == []
    told = [text for _c, key, text in fake.told if ":verdict:untrusted:" in key]
    assert len(told) == 1 and "[[repos]]" in told[0] and "[authorization.release]" not in told[0]
    # #4187: the ChatGPT review chats post as that bot, so its pass counts once it answers a
    # review the deck asked for at that very head, and only from the reviewer's account.
    fake.note(PR, HEAD, "asked", "asked:1", 1.0, {})
    answer = await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, by="someone")
    assert "no Release tier" in answer and fake.merged == []
    answer = await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, by="Review-Bot[bot]")
    assert "review pass recorded" in answer
    assert fake.merged == [(PR, "merge")]

    # One pull request in a repository that waits holds the Work's others for a press, and the
    # reviewer's pass there needs only Work, as before.
    store = "eerovil/store#4"
    fake.merged.clear()
    fake.pulls[PR] = Pull(PR, "OPEN", False, OTHER, "MERGEABLE", "CLEAN", "green")
    fake.pulls[store] = Pull(store, "OPEN", False, OTHER, "MERGEABLE", "CLEAN", "green")
    fake.digests.update({(PR, OTHER): DIGEST, (store, OTHER): DIGEST})
    fake.green = {OTHER}
    shown = [(PR, "open", False), (store, "open", False)]
    assert fake.service.approval(WORK, shown) == "offered"
    answer = await fake.service.on_verdict(
        WORK, "pass", OTHER[:12], DIGEST, (store,), by="review-bot[bot]"
    )
    assert "review pass recorded" in answer
    answer = await fake.service.on_verdict(
        WORK, "pass", OTHER[:12], DIGEST, (PR,), by="review-bot[bot]"
    )
    assert "no Release tier" in answer
    await passed(fake, head=OTHER, targets=(PR,))
    assert fake.merged == [] and await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert fake.entry["awaiting_merge"] == {PR: OTHER, store: OTHER}
    assert fake.service.approval_reason(WORK, shown) == (
        "agentdeck merges on its own, but this Work also has a pull request in"
        " store, so all of them wait for your Merge."
    )

    # A draft next step there is not yet offered, so it holds nothing (D8), and the Work page
    # offers no Merge for it while its tests still run (#4172 review).
    fake.set(store, draft=True)
    fake.green = set()
    await fake.service.evaluate(WORK)
    assert fake.merged == []
    shown = [(PR, "open", False), (store, "open", True)]
    assert fake.service.approval(WORK, shown) == ""
    assert fake.service.approval_reason(WORK, shown) == ""
    fake.green = {OTHER}
    await fake.service.evaluate(WORK)
    assert fake.merged == [(PR, "merge")]


def test_a_policy_merges_itself_only_when_every_pull_request_does():
    own = Policy(auto_release_repos=frozenset({"o/robot"}))
    assert own.merges_itself(["O/Robot#1", "o/robot#2"])
    assert not own.merges_itself(["o/robot#1", "o/store#2"])
    assert not own.merges_itself([]), "nothing open is no reason to skip a press"
    assert Policy(auto_release=True).merges_itself([])
    assert own.for_pulls(["o/robot#1"]).auto_release and own.for_pulls(["o/x#1"]) is own


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
    pressed = fake.passed()
    assert (pressed.head, pressed.data) == (HEAD, {"digest": DIGEST, "by": "a Merge press"})
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


async def test_a_press_on_a_behind_head_merges_after_its_ci_with_no_second_press(fake):
    """#3869, store#3756: pressed at Ready, the base had moved; the deck updates it, Drone
    re-tests the new head (a status, not a job), and master moving again during that run
    updates it once more. Each round the Work page says the press is armed and what it waits
    for, and the press merges the last green head."""
    fake.policy = Policy(auto_release=False, github_checks=frozenset({"eerovil/agentdeck"}))
    await passed(fake)
    assert fake.service.approval_waits(WORK) == "", "nothing pressed"
    fake.set(PR, merge_state="BEHIND", behind_by=1)
    answer = await fake.service.request_merge(WORK)
    assert answer.endswith("is behind its base; the deck merged the base in for a re-test")
    assert fake.updated == [PR] and fake.merged == []
    third = "c" * 40
    for head, then_behind in ((OTHER, True), (third, False)):
        # GitHub shows the update: the same diff at a new head, its CI still running.
        fake.set(PR, head=head, merge_state="BLOCKED", behind_by=0, checks="pending")
        fake.digests[(PR, head)] = DIGEST
        fake.now += 60
        await fake.service.evaluate(WORK)  # the look the update set: the pass moves to it
        assert fake.passed().head == head and fake.merged == []
        assert fake.service.approval_waits(WORK) == (
            "Merge armed: it merges with no second press once nothing holds it."
            " Now: agentdeck#9: its checks are pending."
        )
        if then_behind:
            # Master moved during the run: green, but behind again, so updated again.
            fake.set(PR, checks="green", merge_state="BEHIND", behind_by=1)
            await fake.service.on_status("eerovil/agentdeck", head)
            assert fake.updated == [PR, PR] and fake.merged == []
            assert fake.service.approval(WORK) == "approved"
    fake.set(PR, checks="green", merge_state="CLEAN")
    await fake.service.on_status("eerovil/agentdeck", third)
    assert fake.merged == [(PR, "merge")] and fake.merge_heads == [third]
    assert fake.service.approval_waits(WORK) == ""


async def test_merge_when_ready_is_cleared_by_a_changed_diff_or_a_new_pull_request(fake):
    fake.policy = Policy(auto_release=False)
    await fake.service.request_merge(WORK)
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = "e" * 64
    fake.green = {OTHER}
    await fake.service.evaluate(WORK)
    assert "asked" not in fake.entry and fake.service.approval(WORK) == "offered"
    line = fake.service.approval_cleared(WORK)
    assert line == (
        f"Merge approval cleared: {PR} changed at {OTHER[:12]}. Press Merge again to merge it."
    )
    # #4089: its agent went on saying the merge was armed. It hears once, however many looks.
    cleared = [text for _chat, key, text in fake.told if ":press-cleared:" in key]
    assert len(cleared) == 1 and "press Merge again" in cleared[0]
    assert f"{PR} changed at {OTHER[:12]}" in cleared[0]
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
    assert fake.service.approval_cleared(WORK) == (
        f"Merge approval cleared: {nxt} joined the Work. Press Merge again to merge it."
    )


async def test_a_hold_names_what_each_pull_request_lacks(fake):
    """#4089: a Work of several pull requests said "not reviewed at its head", or only that its
    agent was working, and never which one. Reviewed, green ones say nothing."""
    fake.policy = Policy(auto_release=False, github_checks=frozenset({"eerovil/store"}))
    second, third, fourth = "eerovil/metso#77", "eerovil/store#3849", "eerovil/metso#76"
    fake.pulls = {
        PR: fake.pulls[PR],
        second: Pull(second, "OPEN", False, OTHER, "MERGEABLE", "CLEAN", "none"),
        third: Pull(third, "OPEN", False, "c" * 40, "MERGEABLE", "CLEAN", "pending"),
        fourth: Pull(fourth, "OPEN", False, "e" * 40, "MERGEABLE", "CLEAN", "none"),
    }
    for ref, pull in fake.pulls.items():
        fake.digests[(ref, pull.head)] = DIGEST
    for ref in (PR, third, fourth):
        head = fake.pulls[ref].head
        fake.note(ref, head, "pass", f"pass:{ref}", fake.now, {"digest": DIGEST})
    fake.busy = True
    await fake.service.evaluate(WORK)
    hold = fake.entry["hold"]
    assert hold["words"] == "its agent is still working"
    assert hold_line(hold) == (
        "its agent is still working; metso#76: tests are not green; metso#77: not reviewed;"
        " store#3849: checks are pending"
    )
    # The reason names one of them: its own words stand for it, with no second line on it.
    fake.busy = False
    await fake.service.evaluate(WORK)
    assert hold_line(fake.entry["hold"]) == (
        "metso#76: no test run started; metso#77: not reviewed; store#3849: checks are pending"
    )
    # A failed read of the tests leaves only them out, and a draft says so.
    fake.green_heads_fail = True
    fake.set(second, draft=True)
    waits = await fake.service._pull_waits(WORK, fake.policy, fake.pulls, sorted(fake.pulls))
    assert waits == [
        {"pull": second, "lacks": "still a draft"},
        {"pull": third, "lacks": "checks are pending"},
    ]
    # #4090 review: a name that ends another is another pull request, and two of one short
    # name read in full. The first reason's own wait still shows once.
    hold = {"why": "org/api#7 has no review pass at x", "words": "not reviewed at its head"}
    hold["pulls"] = [
        {"pull": "org/api#7", "lacks": "not reviewed"},
        {"pull": "org/myapi#7", "lacks": "checks are pending"},
    ]
    assert hold_line(hold) == "api#7: not reviewed at its head; myapi#7: checks are pending"
    hold["pulls"][1]["pull"] = "other/api#7"
    assert hold_line(hold) == (
        "org/api#7: not reviewed at its head; other/api#7: checks are pending"
    )
    # A reason with no words of its own names the pull request itself, once.
    assert hold_line({"why": "Org/Api#7 failed", "words": "Org/Api#7 failed"}) == (
        "Org/Api#7 failed"
    )
    # A hold kept before these lines has only its own words.
    assert hold_line({"why": "x", "words": "Merge pressed; no test run started"}) == (
        "no test run started"
    )


async def test_a_withdrawn_merge_approval_waits_for_a_new_press(fake):
    fake.policy = Policy(auto_release=False)
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
    # The base merged in is no message (#4253): the new head's tests are what it waits on.
    assert problems(fake) == []


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
    # Its running job tells the agent the result, so the pass itself sends nothing (#4253).
    assert fake.told == []
    # The job failed: its agent heard that from the job, and is not told to run it again.
    fake.verifying = False
    fake.red = {HEAD}
    await fake.service.on_tests(jobs=True, chats=False)
    assert fake.told == []
    assert fake.entry["waiting_tests"] and "needs_verify" not in fake.entry
    # A job cut off with no result: nothing tests that head now, so the agent hears once.
    fake.red = set()
    await fake.service.on_tests(jobs=True, chats=False)
    await fake.service.on_tests(jobs=True, chats=False)
    assert [m for _c, m, _t in fake.told] == [_verify()]
    assert "passed at " + HEAD[:12] in fake.told[0][2]
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
    assert [m for _c, m, _t in fake.told] == [_verify(), _moved()]
    assert "the checkout changed while it ran" in fake.told[1][2]
    # #3397: score-plugins#444 passed twice at its head on a tree a disk-full pytest
    # had littered with untracked files. The line and the agent hear that, once.
    fake.unclean = {HEAD}
    answer = await fake.service.evaluate(WORK)
    await fake.service.evaluate(WORK)
    assert "uncommitted or untracked files" in answer and fake.merged == []
    assert fake.entry["hold"]["words"] == (
        "tests passed but the checkout had uncommitted or untracked files"
    )
    assert [m for _c, m, _t in fake.told] == [_verify(), _moved(), _unclean()]
    assert "git status --porcelain -uall" in fake.told[2][2]
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


async def test_a_pass_nothing_tests_is_run_again_by_the_deck_not_the_agent(fake):
    """#4253: a reviewed head nothing tests (a pass, or the base merged in) has the Work's last
    verification run again there by the deck; the agent hears nothing unless the deck cannot,
    or that run ends with no result."""
    reruns = []

    async def rerun(work_id, repository, head):
        reruns.append((work_id, repository, head))
        return queued

    fake.service.ports.rerun_verification = rerun
    queued = True
    fake.green = set()
    await passed(fake)
    assert reruns == [(WORK, "eerovil/agentdeck", HEAD)] and fake.told == []
    # Its run is going: nothing more is asked, and the run's result tells the chat.
    fake.verifying = True
    await fake.service.on_tests(jobs=True, chats=False)
    assert len(reruns) == 1 and fake.told == []
    # Cut off with no result: not asked twice; the agent is told to run it, once.
    fake.verifying = False
    await fake.service.on_tests(jobs=True, chats=False)
    await fake.service.on_tests(jobs=True, chats=False)
    assert len(reruns) == 1 and [m for _c, m, _t in fake.told] == [_verify()]
    # A new head the runtime has nothing to run again for: the pass carries the call.
    queued = False
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = "e" * 64
    await passed(fake, head=OTHER, digest="e" * 64)
    assert reruns[1:] == [(WORK, "eerovil/agentdeck", OTHER)]
    assert [m for _c, m, _t in fake.told][1:] == [_pass(OTHER)]
    assert "verification job" in fake.told[1][2]


async def test_a_busy_agent_is_told_to_verify_once_its_turn_ends(fake):
    fake.green = set()
    fake.busy = True
    await passed(fake)
    assert fake.told == []  # held by the turn: nothing to tell yet (#4253)
    fake.busy = False
    await fake.service.on_tests(jobs=False, chats=True)
    assert [m for _c, m, _t in fake.told] == [_verify()]
    # A new reviewed head is a new message.
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = "e" * 64
    await passed(fake, head=OTHER, digest="e" * 64)
    assert [m for _c, m, _t in fake.told][1:] == [_pass(OTHER)]
    assert "verification job" in fake.told[1][2]


async def test_a_repeat_pass_at_a_head_told_while_held_still_tells_the_agent_to_verify(fake):
    """#3001 review: the first pass was told while paused, so it carried no call to verify."""
    fake.green = set()
    fake.paused = True
    await passed(fake)
    assert fake.told == []  # held by the pause: nothing to tell yet (#4253)
    fake.paused = False
    await passed(fake)
    assert [m for _c, m, _t in fake.told] == [_verify()]
    await passed(fake)
    assert len(fake.told) == 1, "once per head"


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
    # ... still in Finnish, like the rest of the summary (#4056).
    assert "in Finnish" in ready[0]
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert len([key for _chat, key, _text in fake.told if ":ready:" in key]) == 1
    # #4253: where the deck can write the evidence into the summary itself, it does, and
    # wakes nobody; a write that fails asks the agent as before.
    notes = []
    fake.service.ports.note_evidence = lambda work_id, text: notes.append((work_id, text)) or True
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = DIGEST
    fake.green = {OTHER}
    await passed(fake, head=OTHER)
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert len([key for _chat, key, _text in fake.told if ":ready:" in key]) == 1
    [(work_id, text)] = notes
    assert work_id == WORK and f"{PR}@{OTHER[:12]}" in text and "katselmointi" in text
    fake.service.ports.note_evidence = lambda work_id, text: False
    third = "c" * 40
    fake.set(PR, head=third)
    fake.digests[(PR, third)] = DIGEST
    fake.green = {third}
    await passed(fake, head=third)
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert len([key for _chat, key, _text in fake.told if ":ready:" in key]) == 2
    fake.set(PR, head=HEAD)
    fake.green = {HEAD}
    # Anything else holding it clears the record: here, a push past the reviewed head.
    fake.set(PR, head=OTHER)
    assert await fake.service.evaluate(WORK) != AWAITING_MERGE
    assert "awaiting_merge" not in fake.entry


@pytest.mark.parametrize(
    ("mergeable", "merge_state"), [("CONFLICTING", "DIRTY"), ("CONFLICTING", ""), ("", "DIRTY")]
)
async def test_a_ready_work_whose_pull_request_starts_conflicting_is_told_and_not_ready(
    fake, mergeable, merge_state
):
    """#4217: a Work read Ready for merge; a merge elsewhere left its pull request conflicting
    with its base. It stayed Ready and its agent heard nothing until a person pressed Merge."""
    from agentdeck.work_release import AWAITING_MERGE, hold_words

    fake.policy = Policy(auto_release=False)
    await passed(fake)
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    fake.set(PR, mergeable=mergeable, merge_state=merge_state)
    answer = await fake.service.evaluate(WORK)
    assert answer == f"{PR} conflicts with its base at {HEAD[:12]}"
    assert "awaiting_merge" not in fake.entry, "the Work page no longer reads it Ready"
    assert hold_words(answer)[0].startswith("conflicts with its base")
    told = [text for _chat, key, text in problems(fake) if ":conflict:" in key]
    assert len(told) == 1 and PR in told[0] and "Merge the base in" in told[0]
    await fake.service.evaluate(WORK)
    assert len([k for _c, k, _t in problems(fake) if ":conflict:" in k]) == 1, "once per head"
    # The agent merges the base in: a new head, clean, reviewed again, reads Ready again.
    fake.set(PR, head=OTHER, mergeable="MERGEABLE", merge_state="CLEAN")
    fake.digests[(PR, OTHER)] = DIGEST
    fake.green.add(OTHER)
    await passed(fake, head=OTHER)
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert fake.merged == []


async def test_a_conflict_is_told_before_review_and_tests_and_leaves_the_queue(fake):
    """#4217: a conflicting pull request may run no CI, so the conflict is named first."""
    fake.set(PR, mergeable="CONFLICTING", merge_state="DIRTY", checks="pending")
    fake.green.clear()
    answer = await fake.service.evaluate(WORK)
    assert answer.startswith(f"{PR} conflicts with its base")
    assert len([k for _c, k, _t in problems(fake) if ":conflict:" in k]) == 1
    assert fake.service.store.place(WORK) == [] and fake.merged == []


async def test_a_sibling_merge_and_an_unknown_merge_answer_each_look_again_once(fake):
    """#4217: GitHub works ``mergeable`` out a while after the base moves, so the look made at
    once still reads the old answer; one timed look reads it again."""
    from agentdeck.work_release import AWAITING_MERGE, MERGEABLE_SETTLE_S

    tick, timers = timed(fake)
    fake.policy = Policy(auto_release=False)
    sibling = "eerovil/agentdeck#10"
    fake.pulls[sibling] = Pull(sibling, "MERGED", False, OTHER, "", "", "green")
    await passed(fake, targets=(PR,))
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert timers == []
    await fake.service.on_pull_event(WORK, sibling, "closed", True)
    assert [t[0] - fake.now for t in timers] == [MERGEABLE_SETTLE_S]
    fake.set(PR, mergeable="CONFLICTING", merge_state="DIRTY")
    await tick(MERGEABLE_SETTLE_S)
    assert "awaiting_merge" not in fake.entry
    assert len([k for _c, k, _t in problems(fake) if ":conflict:" in k]) == 1
    # Still being worked out: looked at once more, and a held timer is not replaced.
    timers.clear()
    fake.set(PR, mergeable="UNKNOWN", merge_state="UNKNOWN")
    await fake.service.evaluate(WORK)
    await fake.service.evaluate(WORK)
    assert len([t for t in timers if not t[2]]) == 1


async def test_a_push_to_the_base_looks_again_at_a_ready_work_with_nobody_watching(fake):
    """#4217 review: another Work's merge (or any push) onto the base of a pull request waiting
    on a Merge, with no dashboard open, still reaches it: one timed look, then the conflict is
    told and Ready for merge goes."""
    from agentdeck.work_release import AWAITING_MERGE, MERGEABLE_SETTLE_S

    tick, timers = timed(fake)
    fake.policy = Policy(auto_release=False)
    fake.set(PR, base="master")
    await passed(fake)
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert fake.entry["awaiting_bases"] == {PR: "master"}
    # Another branch, another repository: nothing to look at.
    assert fake.service.on_base_moved("eerovil/agentdeck", "agent/issue-9") == []
    assert fake.service.on_base_moved("eerovil/other", "master") == []
    assert timers == []
    # The base, in any spelling of the repository, twice: one timer.
    assert fake.service.on_base_moved("EeroVil/AgentDeck", "master") == [WORK]
    assert fake.service.on_base_moved("eerovil/agentdeck", "master") == [WORK]
    assert [t[0] - fake.now for t in timers if not t[2]] == [MERGEABLE_SETTLE_S]
    fake.set(PR, mergeable="CONFLICTING", merge_state="DIRTY")
    await tick(MERGEABLE_SETTLE_S)
    assert "awaiting_merge" not in fake.entry and "awaiting_bases" not in fake.entry
    assert len([k for _c, k, _t in problems(fake) if ":conflict:" in k]) == 1
    # No longer waiting on a Merge: a later push looks at nothing.
    assert fake.service.on_base_moved("eerovil/agentdeck", "master") == []
    # A Ready entry from before bases were kept is looked at on any push to its repository.
    entry = fake.entry
    entry["awaiting_merge"] = {PR: HEAD}
    fake.service.store.put(WORK, entry)
    assert fake.service.on_base_moved("eerovil/agentdeck", "develop") == [WORK]


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


def test_an_older_decks_passes_and_findings_are_dropped_not_read(tmp_path):
    """#3956: each head's review history holds them now, and nothing is copied over."""
    path = tmp_path / "work-release.json"
    old = {"passes": {PR: {"head": HEAD}}, "findings": {"at": 1.0}, "no_code": {}, "told": ["x"]}
    path.write_text(json.dumps({"works": {WORK: old}}))
    assert ReleaseStore(path).get(WORK) == {"told": ["x"]}


async def test_an_unknown_pull_request_list_keeps_the_passes(fake):
    """#2982: a fresh web process that has not scanned the Work's chats reads no pull
    requests; that is no answer, so the pass survives the restart and still merges."""
    from agentdeck.work_release import AWAITING_MERGE

    fake.policy = Policy(auto_release=False)
    await passed(fake)
    before = dict(fake.entry)
    known, fake.pulls = fake.pulls, {}
    # #3975: a skipped Work still lets the event loop run, or a run of them blocks it.
    sweep = asyncio.create_task(fake.service.look_at_reviewed())
    seen = []

    async def other():
        seen.append(sweep.done())

    await asyncio.gather(sweep, other())
    assert seen == [False]
    assert fake.entry == before
    # #3127: a verdict then says the deck counts none, so it reads as the deck's problem.
    assert "counts no pull request for this Work, which is a deck problem" in await passed(fake)
    fake.pulls = known
    assert await fake.service.evaluate(WORK) == AWAITING_MERGE
    assert fake.passed().head == HEAD


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
    # Untested: a pass at a tested head sends nothing (#4253), so this one carries the call.
    fake.green = set()
    # The chats opened about each head are its ``chat`` rows (#3956).
    fake.note(PR, OTHER, "chat", "c0", 1.0, {"chat": "eerovil/review-chats#5165"})
    fake.note(PR, HEAD, "chat", "c1", 1.0, {"chat": "eerovil/review-chats#5166"})
    await passed(fake)
    await passed(fake)  # a resent verdict is the same pass: not told again
    verdicts = [told for told in fake.told if ":verdict:" in told[1]]
    assert [m for _c, m, _t in verdicts] == [f"work-release:{WORK}:verdict:pass:{PR}@{HEAD[:12]}"]
    text = verdicts[0][2]
    assert PR in text and HEAD[:12] in text and "eerovil/review-chats#5166" in text
    assert "passed clean" in text and "verification job" in text
    assert fake.passed().data["chat"] == "eerovil/review-chats#5166"
    assert fake.passed().at == fake.now
    # What a review asked for before this pass reads to drop its retry (#3324).
    assert fake.service.passed_since(WORK, PR.upper(), HEAD[:12], fake.now)
    assert not fake.service.passed_since(WORK, PR, HEAD, fake.now + 1)
    assert not fake.service.passed_since(WORK, PR, "b" * 40, fake.now)
    # The head reviewed again on request (#3183) passes again in a new chat: told, once, and
    # only that message ends the agent's review wait.
    answered = []
    fake.service.on_review_answer = lambda chat, pulls: answered.append(pulls)
    fake.note(PR, HEAD, "chat", "c2", 1.0, {"chat": "eerovil/review-chats#5170"})
    await passed(fake)
    await passed(fake)
    verdicts = [told for told in fake.told if ":verdict:" in told[1]]
    assert [m for _c, m, _t in verdicts][1:] == [
        f"work-release:{WORK}:verdict:pass:{PR}@{HEAD[:12]}@eerovil/review-chats#5170"
    ]
    assert "eerovil/review-chats#5170" in verdicts[1][2]
    assert answered == [[PR], [PR]]


async def test_a_clean_pass_on_a_diff_too_large_for_github_merges_at_its_head(fake):
    """#3804: GitHub serves no diff over 20,000 lines, so its digest is pinned to the head, and
    a clean review stating it merges a tested Work on its own instead of dead-ending."""
    pinned = head_digest("eerovil/agentdeck", 9, HEAD)
    fake.digests[(PR, HEAD)] = pinned
    elsewhere = head_digest("eerovil/agentdeck", 9, OTHER)
    assert "digest does not match" in await passed(fake, digest=elsewhere)
    assert fake.merged == []
    assert "review pass recorded" in await passed(fake, digest=pinned)
    assert fake.merged == [(PR, "merge")] and fake.merge_heads == [HEAD]


def verdict_messages(fake, kind):
    return [text for _chat, key, text in fake.told if f":verdict:{kind}:" in key]


@pytest.mark.parametrize("failed", ["diff", "pull"])
async def test_a_pass_whose_read_failed_is_held_and_merges_once_it_reads(fake, failed):
    """#4124: store#3860's clean pass was thrown away as a wrong digest, because ``gh pr diff``
    failed at that moment. A failed read says nothing about the pass, so it waits and is read
    again; a restart loses only the timer."""
    _tick, timers = timed(fake)
    ports = fake.service.ports
    del fake.digests[(PR, HEAD)]
    if failed == "pull":

        async def unread(ref):
            return None

        fake.service.ports = replace(ports, read_pull=unread)
    answer = await passed(fake)
    fake.service.ports = ports
    assert answer.endswith(PASS_HELD) and verdict_settled(answer)
    assert "does not match" not in answer and fake.passed() is None
    assert len(verdict_messages(fake, "held")) == 1 and "nothing is needed" in fake.told[0][2]
    # The answer the Work shows while it waits, and no second message for the next failed read.
    look = await fake.service.evaluate(WORK)
    assert PASS_HELD in look and hold_words(look)[0] == "review passed; waiting to read its diff"
    assert len(verdict_messages(fake, "held")) == 1 and fake.merged == []
    assert fake.entry[HELD_PASSES][PR]["delay"] > RETRY_FIRST_S
    # The restart: a new service over the same file has no timer and no pass to find it by.
    timers.clear()
    fake.digests[(PR, HEAD)] = DIGEST
    fake.service = WorkReleaseService(ReleaseStore(fake.service.store.path), ports)
    await fake.service.look_at_reviewed()
    assert fake.passed() is not None and fake.merged == [(PR, "merge")]
    assert HELD_PASSES not in fake.entry
    # A tested pass is recorded as told but sends nothing: the merge follows (#4253).
    assert [key for key in fake.entry["told"] if key.startswith("verdict:pass:")]
    assert not [t for t in fake.told if ":verdict:pass:" in t[1]]


async def test_a_held_pass_is_looked_at_again_when_the_look_at_start_raises(fake):
    """#4124: the restart lost the timer, so a start look that raises sets a new one."""
    del fake.digests[(PR, HEAD)]
    timed(fake)
    await passed(fake)
    ports = fake.service.ports

    async def broken(_ref):
        raise RuntimeError("GitHub is down")

    fake.service = WorkReleaseService(ReleaseStore(fake.service.store.path), ports)
    tick, _timers = timed(fake)
    fake.service.ports = replace(fake.service.ports, read_pull=broken)
    await fake.service.look_at_reviewed()
    assert HELD_PASSES in fake.entry and "retry" in fake.entry
    fake.service.ports = replace(fake.service.ports, read_pull=ports.read_pull)
    fake.digests[(PR, HEAD)] = DIGEST
    await tick(RETRY_MAX_S)
    assert fake.merged == [(PR, "merge")]


async def test_two_held_passes_and_two_looks_at_once_record_each_once(fake):
    """A Work of two pull requests: the first pass recorded looks at the Work, which takes the
    second, so the outer loop finds it gone. Looks at the same moment tell nothing twice."""
    timed(fake)
    nxt = "eerovil/agentdeck#10"
    fake.pulls[nxt] = Pull(nxt, "OPEN", False, OTHER, "MERGEABLE", "CLEAN", "green")
    fake.green.add(OTHER)
    fake.digests.clear()
    await passed(fake, targets=(PR,))
    await passed(fake, head=OTHER, digest="c" * 64, targets=(nxt,))
    assert sorted(fake.entry[HELD_PASSES]) == [nxt, PR]
    fake.digests.update({(PR, HEAD): DIGEST, (nxt, OTHER): "c" * 64})
    await asyncio.gather(fake.service.evaluate(WORK), fake.service.evaluate(WORK))
    assert HELD_PASSES not in fake.entry
    assert fake.passed(PR) is not None and fake.passed(nxt) is not None
    assert len([key for key in fake.entry["told"] if key.startswith("verdict:pass:")]) == 2
    assert not [t for t in fake.told if ":verdict:pass:" in t[1]]  # tested: quiet (#4253)


async def test_the_same_pass_sent_again_while_held_keeps_its_clock(fake):
    """A resent or replayed verdict is one hold: one message, and the day runs from the first."""
    timed(fake)
    del fake.digests[(PR, HEAD)]
    await passed(fake)
    first = fake.now
    fake.now += 300
    assert verdict_settled(await passed(fake))
    assert fake.entry[HELD_PASSES][PR]["since"] == first
    assert len(verdict_messages(fake, "held")) == 1
    # A later review's pass with another digest starts its own hold.
    await passed(fake, digest="c" * 64)
    assert fake.entry[HELD_PASSES][PR]["since"] == fake.now


async def test_a_held_pass_keeps_the_time_of_the_comment(fake):
    """#3957: a verdict recorded late is dated by its comment, so newer findings stay newer."""
    timed(fake)
    del fake.digests[(PR, HEAD)]
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:1", at=500.0)
    fake.digests[(PR, HEAD)] = DIGEST
    fake.now += 600
    await fake.service.evaluate(WORK)
    assert fake.passed().at == 500.0


async def test_a_held_pass_reads_again_by_itself(fake):
    tick, _timers = timed(fake)
    del fake.digests[(PR, HEAD)]
    await passed(fake)
    await tick(RETRY_FIRST_S + 1)
    assert fake.passed() is None and HELD_PASSES in fake.entry  # still failing: looks again
    ports = fake.service.ports

    async def broken(_ref):
        raise RuntimeError("GitHub is down")

    fake.service.ports = replace(ports, read_pull=broken)
    await tick(RETRY_MAX_S)  # a look that raises, with no pass recorded, still looks again
    assert HELD_PASSES in fake.entry and "retry" in fake.entry
    fake.service.ports = ports
    fake.digests[(PR, HEAD)] = DIGEST
    await tick(RETRY_MAX_S)
    assert fake.merged == [(PR, "merge")]


async def test_a_held_pass_that_reads_wrong_or_moved_is_refused_and_told(fake):
    """#4124: once it reads, a held pass meets the same checks as any other, and a refusal
    reaches the chat, which only had the comment to go on."""
    timed(fake)
    answered = []
    fake.service.on_review_answer = lambda chat, pulls: answered.append(pulls)
    del fake.digests[(PR, HEAD)]
    await passed(fake)
    fake.digests[(PR, HEAD)] = "e" * 64
    await fake.service.evaluate(WORK)
    assert fake.passed() is None and HELD_PASSES not in fake.entry
    (told,) = verdict_messages(fake, "refused")
    assert "does not match the diff" in told and "review-request" in told
    assert answered == [[PR]]
    # A pass refused straight away says so too, in place of the comment.
    answer = await passed(fake, digest="f" * 64)
    assert "digest does not match" in answer and verdict_settled(answer)


async def test_a_held_pass_at_a_head_the_branch_left_keeps_the_wait(fake):
    """Told, but the chat's wait is on the new head's review now."""
    timed(fake)
    answered = []
    fake.service.on_review_answer = lambda chat, pulls: answered.append(pulls)
    del fake.digests[(PR, HEAD)]
    await passed(fake)
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = "e" * 64
    await fake.service.evaluate(WORK)
    assert HELD_PASSES not in fake.entry and answered == []
    (told,) = verdict_messages(fake, "refused")
    assert f"moved on to {OTHER[:12]}" in told


async def test_a_held_pass_is_given_up_after_a_day_and_told(fake):
    timed(fake)
    del fake.digests[(PR, HEAD)]
    await passed(fake)
    fake.now += RETRY_MAX_AGE_S + 1
    await fake.service.evaluate(WORK)
    assert HELD_PASSES not in fake.entry and fake.passed() is None
    (told,) = verdict_messages(fake, "refused")
    assert "for a day" in told and "review-request" in told


@pytest.mark.parametrize("verdict", ["changes-needed", "no-code"])
async def test_a_verdict_older_than_a_held_pass_only_leaves_its_row(fake, verdict):
    """#4124: delivered late, it must not send the chat off to change a branch that passed, end
    its wait on the review or take it out of line; the pass merges once it reads."""
    timed(fake)
    answered = []
    fake.service.on_review_answer = lambda chat, pulls: answered.append(pulls)
    del fake.digests[(PR, HEAD)]
    at = fake.now + 50
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, (), at=at)
    told = list(fake.told)
    answer = await fake.service.on_verdict(WORK, verdict, HEAD[:12], "", (), at=at - 10)
    assert verdict_settled(answer) and "older than its newest" in answer
    assert fake.told == told and answered == [] and HELD_PASSES in fake.entry
    assert [row[2] for row in fake.history] == ["changes"]
    fake.digests[(PR, HEAD)] = DIGEST
    await fake.service.evaluate(WORK)
    assert fake.merged == [(PR, "merge")]


@pytest.mark.parametrize("readable", [False, True])
async def test_a_pass_older_than_a_held_pass_leaves_it_as_it_is(fake, readable):
    """#4124: passes at 30 then 10, then findings at 20: the newest word is still the pass at
    30, whether or not the diff reads by the time the late ones arrive."""
    timed(fake)
    del fake.digests[(PR, HEAD)]
    newest = fake.now + 30
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, (), source="p2", at=newest)
    held = dict(fake.entry[HELD_PASSES][PR])
    if readable:
        fake.digests[(PR, HEAD)] = DIGEST
    answer = await fake.service.on_verdict(
        WORK, "pass", HEAD[:12], DIGEST, (), source="p1", at=newest - 20
    )
    assert verdict_settled(answer) and fake.entry[HELD_PASSES][PR] == held
    told = list(fake.told)
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", (), at=newest - 10)
    assert fake.told == told and fake.entry[HELD_PASSES][PR] == held
    fake.digests[(PR, HEAD)] = DIGEST
    await fake.service.evaluate(WORK)
    assert fake.passed().at == newest and fake.merged == [(PR, "merge")]


@pytest.mark.parametrize("readable", [False, True])
async def test_a_pass_older_than_recorded_findings_is_neither_held_nor_announced(fake, readable):
    """#4124: findings at 20 stand, so a pass from 10 arriving later says nothing to the chat,
    which would otherwise tell the owner to press Merge."""
    timed(fake)
    at = fake.now + 20
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", (), at=at)
    if not readable:
        del fake.digests[(PR, HEAD)]
    told = list(fake.told)
    answer = await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, (), at=at - 10)
    assert verdict_settled(answer) and fake.told == told
    assert HELD_PASSES not in fake.entry and fake.passed() is None and fake.merged == []


@pytest.mark.parametrize("word", ["findings", "asked", "escalated"])
async def test_a_newer_word_outside_on_verdict_ends_a_held_pass_at_its_next_read(fake, word):
    """#4124: the deck's reviewer's findings, a review asked for again or an escalation come by
    other routes. The held pass ends there, unannounced, even with the diff still unreadable."""
    timed(fake)
    del fake.digests[(PR, HEAD)]
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, (), at=fake.now - 10)
    if word == "findings":  # through the route itself, with the row it writes
        text = f"Review of {PR} at {HEAD[:9]}: fix x"
        assert await fake.service.stale_findings(WORK, text, "", "r1") == ""
        assert await fake.service.on_findings(WORK, "r1", text, "https://x/1")
    else:
        fake.note(PR, HEAD, word, f"{word}:1", fake.now, {})
    look = await fake.service.evaluate(WORK)
    assert HELD_PASSES not in fake.entry and PASS_HELD not in look
    fake.digests[(PR, HEAD)] = DIGEST
    await fake.service.evaluate(WORK)
    assert fake.passed() is None and fake.merged == []
    assert verdict_messages(fake, "pass") == [] and verdict_messages(fake, "refused") == []


async def test_findings_older_than_a_recorded_pass_tell_nothing(fake):
    """#4124: the same rule the other way round; the pass stands and the Work merges on."""
    fake.busy = True
    at = fake.now + 20
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, (), at=at)
    told = list(fake.told)
    answer = await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", (), at=at - 10)
    assert verdict_settled(answer) and fake.told == told and fake.passed() is not None


async def test_a_newer_verdict_or_a_closed_pull_ends_a_held_pass(fake):
    """Findings after the pass are the newer word; a merged pull request needs no pass."""
    timed(fake)
    del fake.digests[(PR, HEAD)]
    await passed(fake)
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", ())
    assert HELD_PASSES not in fake.entry
    # Reversed arrival: findings older than the held pass, delivered late, are not newer.
    pass_at = fake.now + 50
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, (), at=pass_at)
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", (), at=pass_at - 10)
    assert HELD_PASSES in fake.entry
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", (), at=pass_at + 10)
    assert HELD_PASSES not in fake.entry
    await passed(fake)
    fake.set(PR, state="MERGED")
    fake.digests[(PR, HEAD)] = DIGEST
    await fake.service.evaluate(WORK)
    assert HELD_PASSES not in fake.entry and fake.passed() is None
    assert verdict_messages(fake, "refused") == []


async def test_a_verdict_after_a_merge_by_hand_still_reaches_the_work(fake):
    """#4189: the owner merged a pull request while its review ran, and the reviewer's clean pass
    was dropped without a word, so the Work waited on a verdict that never came."""
    answered = []
    fake.service.on_review_answer = lambda chat, pulls: answered.append(pulls)
    fake.set(PR, state="MERGED")
    # A digest that is not the merged diff's passed nothing that landed (#4189 review).
    assert "told" not in await passed(fake, digest="0" * len(DIGEST))
    assert verdict_messages(fake, "late-pass") == []
    assert "told as merged" in await passed(fake)
    (told,) = verdict_messages(fake, "late-pass")
    assert "already merged" in told and "nothing needs a follow-up" in told
    assert answered == [[PR]] and fake.passed() is None and fake.merged == []
    await passed(fake)  # the same pass again, as a comment: not a second message
    assert len(verdict_messages(fake, "late-pass")) == 1
    # A pass at another head than the one that merged is about nothing that landed.
    assert "told" not in await passed(fake, head=OTHER)
    # Findings at the merged head are told as a follow-up, not as a push before merging.
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", ())
    (findings,) = verdict_messages(fake, "changes-needed")
    assert "follow-up pull request" in findings and "mark it ready" not in findings
    # So are the deck's own reviewer's findings, which come by ``on_findings`` (#4189 review).
    text = f"Review of {PR} at {HEAD[:12]}: fix x"
    assert await fake.service.stale_findings(WORK, text, "", "r1") == ""
    assert await fake.service.on_findings(WORK, "r1", text, "https://x/1")
    deck = [t for _c, m, t in fake.told if m.endswith(":findings:r1")]
    assert len(deck) == 1 and "follow-up pull request" in deck[0]
    assert "mark the pull request ready again" not in deck[0]


async def test_open_pull_findings_still_ask_for_a_push(fake):
    text = f"Review of {PR} at {HEAD[:12]}: fix x"
    assert await fake.service.stale_findings(WORK, text, "", "r1") == ""
    assert await fake.service.on_findings(WORK, "r1", text, "https://x/1")
    (told,) = [t for _c, m, t in fake.told if m.endswith(":findings:r1")]
    assert "mark the pull request ready again" in told and "follow-up" not in told


async def test_a_second_copy_of_the_pass_the_deck_merged_on_tells_nothing(fake):
    await passed(fake)
    assert fake.pulls[PR].state == "MERGED"
    # GitHub's merge event forgets the pull request's history (``forget_reviews``), so the
    # replay is told apart by what the Work's chat was told, not by the history (#4189 review).
    await fake.service.on_pull_event(WORK, PR, "closed", True)
    assert fake.forgotten
    fake.events.clear()
    before = list(fake.told)
    await passed(fake)
    assert fake.told == before
    # Findings told after the merge, then a newer pass at the same head: that pass is news.
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", ())
    assert len(verdict_messages(fake, "changes-needed")) == 1
    assert "told as merged" in await passed(fake)
    assert len(verdict_messages(fake, "late-pass")) == 1


async def test_a_held_pass_on_a_pull_closed_unmerged_is_refused_and_told(fake):
    """#4124: nothing will merge it, so the chat must not go on waiting for the pass."""
    timed(fake)
    answered = []
    fake.service.on_review_answer = lambda chat, pulls: answered.append(pulls)
    del fake.digests[(PR, HEAD)]
    await passed(fake)
    fake.set(PR, state="CLOSED")
    await fake.service.evaluate(WORK)
    assert HELD_PASSES not in fake.entry and fake.passed() is None
    (told,) = verdict_messages(fake, "refused")
    assert "closed without merging" in told and answered == [[PR]]
    assert verdict_settled(await passed(fake))  # the comment itself has nothing left to tell


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


async def test_changes_needed_on_an_unreadable_pull_still_replaces_its_pass(fake):
    """#3956: the history holds the only pass, so findings about its short head must land at
    the full head even when the pull request cannot be read just then."""
    fake.policy = Policy(auto_release=False)
    await passed(fake)
    real = fake.service.ports.read_pull

    async def unreadable(ref):
        return None

    fake.service.ports = replace(fake.service.ports, read_pull=unreadable)
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "")
    fake.service.ports = replace(fake.service.ports, read_pull=real)
    assert fake.passed() is None and fake.events[-1].head == HEAD
    assert "no review pass" in await fake.service.evaluate(WORK)


@pytest.mark.parametrize("failure", ["raises", "false"])
async def test_a_verdict_the_history_will_not_store_merges_nothing(fake, failure):
    """#3956 review: the history is the only record of a verdict, so a write that failed is
    never acknowledged as a pass, and findings that failed hold the head they were about."""
    note = fake.note

    def broken(*args, **kwargs):
        if failure == "raises":
            raise RuntimeError("database is locked")
        return False

    fake.service.ports.review_event = broken
    assert "could not be recorded" in await passed(fake)
    assert fake.merged == [] and fake.passed() is None
    assert not [told for told in fake.told if ":verdict:pass:" in told[1]]
    fake.service.ports.review_event = note
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:1")
    assert fake.merged == [(PR, "merge")]
    # An older pass and findings that were not written: held until a newer verdict lands.
    fake.set(PR, state="OPEN")
    fake.merged.clear()
    fake.service.ports.review_event = broken
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "")
    fake.service.ports.review_event = note
    assert fake.passed() is not None, "the pass is still the newest row"
    assert "could not be recorded" in await fake.service.evaluate(WORK)
    assert fake.merged == []
    # The base merged in (same diff, green): the hold follows the pull request, so the older
    # pass is neither carried to the new head nor merged there.
    fake.digests[(PR, OTHER)] = DIGEST
    fake.green = {HEAD, OTHER}
    fake.set(PR, head=OTHER)
    assert "could not be recorded" in await fake.service.evaluate(WORK)
    assert fake.merged == [] and not [e for e in fake.events if e.key.startswith("carried:")]
    fake.set(PR, head=HEAD)
    fake.now += 1
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:2")
    assert fake.merged == [(PR, "merge")] and "unrecorded" not in fake.entry


async def test_a_failed_finding_delivered_late_is_lifted_by_a_newer_pass(fake):
    """#3956 review: the hold is dated by the failed comment's own time, so a pass made after
    it lifts it even when both arrive long after they were posted."""
    note = fake.note
    fake.green = set()
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:1", at=50.0)
    fake.service.ports.review_event = lambda *args, **kwargs: False
    late = {"source": "comment:2", "at": 100.0}  # posted at 100, taken at the clock's 1000
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", **late)
    fake.service.ports.review_event = note
    fake.green = {HEAD}
    assert "could not be recorded" in await fake.service.evaluate(WORK)
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:3", at=200.0)
    assert fake.merged == [(PR, "merge")] and "unrecorded" not in fake.entry


async def test_a_merge_press_whose_pass_is_not_stored_records_nothing(fake):
    """#3956 review: with no reviewer (#3623) the press is the pass; one the history did not
    store must not leave the press approved and waiting for a pass nobody will give."""
    fake.policy = Policy(auto_release=False)
    fake.service.ports = replace(fake.service.ports, press_is_review=True)
    note = fake.service.ports.review_event
    fake.service.ports = replace(fake.service.ports, review_event=lambda *a, **k: False)
    with pytest.raises(PressRefused, match="could not be recorded"):
        await fake.service.request_merge(WORK, fresh=True)
    assert "asked" not in fake.entry and fake.service.approval(WORK) == "offered"
    fake.service.ports = replace(fake.service.ports, review_event=note)
    await fake.service.request_merge(WORK, fresh=True)
    assert fake.merged == [(PR, "merge")]


async def test_deck_findings_not_stored_hold_until_a_pass_made_after_them(fake):
    """#3956 review: the deck reviewer's findings come in as they are made, so their hold is
    dated as their row would have been, and a pass made after it lifts it."""
    note = fake.note
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:1", at=50.0)
    fake.merged.clear()
    fake.set(PR, state="OPEN")
    text = f"Review of {PR} at {HEAD[:9]}: fix x"
    assert await fake.service.stale_findings(WORK, text, "", "r1") == ""
    fake.service.ports.review_event = lambda *args, **kwargs: False
    await fake.service.on_findings(WORK, "r1", text, "https://x/1")
    fake.service.ports.review_event = note
    assert fake.entry["unrecorded"][PR]["at"] == fake.now
    assert "could not be recorded" in await fake.service.evaluate(WORK)
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="c2", at=fake.now + 1)
    assert fake.merged == [(PR, "merge")] and "unrecorded" not in fake.entry


async def test_an_older_failed_verdict_arriving_later_keeps_the_newer_hold(fake):
    """#3956 review: failed changes-needed at t=200, then an older one at t=150 delivered later
    and failing too; a stored pass at t=175 sits between and must not lift the hold."""
    note = fake.note
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:1", at=175.0)
    fake.merged.clear()
    fake.set(PR, state="OPEN")
    fake.service.ports.review_event = lambda *args, **kwargs: False
    for source, at in (("comment:3", 200.0), ("comment:2", 150.0)):
        await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", source=source, at=at)
    fake.service.ports.review_event = note
    assert fake.entry["unrecorded"][PR]["at"] == 200.0
    assert "could not be recorded" in await fake.service.evaluate(WORK)
    assert fake.merged == []


async def test_a_pass_waits_for_a_review_asked_again_at_its_head(fake):
    """#3956 review: an ask after the pass on the same head is answered before it merges; a
    review chat opened since is not an ask (one listing a passed pull request posts nothing
    about it, #3341)."""
    fake.green = set()
    await passed(fake)
    fake.note(PR, HEAD, "chat", "c1", fake.now + 1, {"chat": "eerovil/review-chats#9"})
    assert fake.service.passed_since(WORK, PR, HEAD, 0.0)
    fake.note(PR, HEAD, "asked", "a1", fake.now + 2, {})
    fake.green = {HEAD}
    assert "no review pass" in await fake.service.evaluate(WORK)
    assert fake.merged == [] and not fake.service.passed_since(WORK, PR, HEAD, 0.0)
    fake.now += 3
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:3")
    assert fake.merged == [(PR, "merge")]


async def test_a_base_update_carries_no_pass_a_review_was_asked_again_after(fake):
    """#3956 review escalation: pass, ask again, then the base merged in: the old pass must
    not reach the new head before the asked review answers, whichever head was asked."""
    fake.green = {HEAD, OTHER}
    fake.digests[(PR, OTHER)] = DIGEST
    third = "c" * 40
    fake.green.add(third)
    fake.digests[(PR, third)] = DIGEST
    # Asked at the passed head, at the new one, or at a head in between that a second base
    # update has since left (H1 passes, asked at H2, the base merged in again to H3).
    for asked_at, current in ((HEAD, OTHER), (OTHER, OTHER), (OTHER, third)):
        fake.events.clear()
        fake.set(PR, head=HEAD, state="OPEN")
        await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:1")
        fake.merged.clear()
        fake.note(PR, asked_at, "asked", f"a-{asked_at[0]}", fake.now + 1, {})
        fake.set(PR, head=current, state="OPEN")
        assert "no review pass" in await fake.service.evaluate(WORK)
        assert fake.merged == [] and not [e for e in fake.events if e.key.startswith("carried:")]
    # With no ask since, the same base update carries it (#3314).
    fake.events.clear()
    fake.set(PR, head=HEAD, state="OPEN")
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:2")
    fake.merged.clear()
    fake.set(PR, head=OTHER, state="OPEN")
    await fake.service.evaluate(WORK)
    assert fake.merged == [(PR, "merge")]


async def test_a_second_no_code_fix_at_one_head_writes_its_own_pass(fake):
    """#3956 review escalation: each no-code fix answers its own verdict, so the second one at
    an unchanged head is a new pass row, not the first one's key read as already stored."""
    fake.policy = Policy(auto_release=False)
    for n in (1, 2):
        fake.now += 10
        source = f"comment:{n}"
        await fake.service.on_verdict(WORK, "no-code", HEAD[:12], DIGEST, source=source)
        assert fake.service.no_code_head(WORK, PR) is not None
        assert verdict_settled(await fake.service.no_code_fixed(WORK, PR, HEAD))
        # Its pass now stands, and a later doc-only push keeps it (#4257).
        assert fake.passed() is not None and fake.service.no_code_head(WORK, PR) == (HEAD, None)
    assert len([e for e in fake.events if e.key.startswith("no-code-fixed:")]) == 2


async def test_changes_needed_and_a_refused_pass_are_told(fake):
    answered = []
    fake.service.on_review_answer = lambda chat, pulls: answered.append((chat, pulls))
    assert "digest does not match" in await passed(fake, digest="e" * 64)
    # The refusal is told, which ends the chat's wait as a verdict would (#4124).
    assert answered == [(fake.told[0][0], [PR])]
    assert ":verdict:refused:" in fake.told[0][1]
    answered.clear()
    fake.told.clear()
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], DIGEST)
    # A verdict taken ends the chat's review waits without a message of their own (#3183).
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
    fake.service.ports = ports
    assert await fake.service.on_findings(WORK, "req-1", "1. off by one", "https://x/1")
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
    # An escalation's heading names its head (#4069), but it is not findings to drop as stale.
    escalation = "Review loop escalated to the operator for PR #9 at `ABCDEF1`\n\nAt 1234567: X"
    assert heading_targets(escalation, "Eerovil/x") == {"eerovil/x#9": "abcdef1"}
    assert findings_target(escalation, "eerovil/x") is None
    # One review covers a Work's pull requests, so a heading may name each; the body never does.
    both = "/claude\nReview findings for o/r#9 at abcdef1 and `q/s#3` at `1234567`:\n\n"
    both += "#4 at 7654321"
    assert heading_targets(both, "x/y") == {"o/r#9": "abcdef1", "q/s#3": "1234567"}
    old_form = "Review loop escalated to the operator\n\nFor PR #9 at abcdef1: X"
    assert heading_targets(old_form, "eerovil/x") == {} and heading_targets("", "o/r") == {}
    stale = fake.service.stale_findings
    assert "moved past" in await stale(WORK, f"Review of {PR} at {OTHER[:9]}", "")
    assert await stale(WORK, f"Review of {PR} at {HEAD[:9]}", "") == ""
    assert await stale(WORK, f"Review of {PR} at abc", "") == ""
    # A pull request that is not this Work's is never read as its own.
    assert await stale(WORK, f"Review of #9 at {OTHER[:9]}", "eerovil/issues") == ""


async def test_a_verdict_about_a_head_left_only_by_a_base_merge_still_counts(fake):
    """#3314: the router reviews no new head it proved a base merge away, so the old head's
    verdict is the one that stands, findings and pass alike."""
    # The router proved HEAD a base merge away from OTHER: it wrote OTHER's findings at HEAD,
    # carried from it, in HEAD's history (#3955).
    fake.note(PR, HEAD, "changes", f"carried:{PR}@{HEAD}", 0.0, {"carried_from": OTHER})
    assert await fake.service.stale_findings(WORK, f"Review of {PR} at {OTHER[:12]}", "") == ""
    # Not proven (a push, or the same lines moved elsewhere): moved past, and no pass.
    stale = await fake.service.stale_findings(WORK, f"Review of {PR} at {'c' * 12}", "")
    assert "moved past" in stale
    assert "not open at" in await passed(fake, head="c" * 40)
    assert "digest does not match" in await passed(fake, head=OTHER, digest="e" * 64)
    await passed(fake, head=OTHER)
    assert fake.passed().head == HEAD
    # It stands at the current head in its history, naming the head the reviewer read (#3957).
    assert [(row[1], row[2], row[4]["carried_from"]) for row in fake.history[1:]] == [
        (HEAD, "pass", OTHER[:12]),
    ]


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
    assert fake.passed().head == OTHER and fake.passed().data["no-code"] == HEAD
    # Tested, so the pass sends nothing (#4253); it is recorded as told all the same.
    assert f"verdict:pass:{PR}@{OTHER[:12]}" in fake.entry["told"]
    assert not any(":verdict:pass:" in key for _chat, key, _text in fake.told)
    # The pass it wrote is a standing pass: a doc-only push keeps it, its own head needs none.
    assert fake.service.no_code_head(WORK, PR) == (OTHER, None)
    assert "already passed at" in await fake.service.no_code_fixed(WORK, PR, OTHER)
    # Any other verdict on the pull request replaces a no-code one.
    fake.set(PR, state="OPEN")
    await fake.service.on_verdict(WORK, "no-code", OTHER[:12], "e" * 64)
    await fake.service.on_verdict(WORK, "changes-needed", OTHER[:12], "")
    assert fake.service.no_code_head(WORK, PR) is None


async def test_a_doc_only_push_keeps_a_standing_pass_with_no_review(fake):
    """#4257: a push of doc files alone after a pass (a diagram or guide fix) keeps that pass
    at the new head, with no new review; a pass asked about again, or findings after it, keep
    nothing, and the passed head itself needs no second pass."""
    fake.policy = Policy(auto_release=False)
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:1")
    assert fake.service.no_code_head(WORK, PR) == (HEAD, None)
    assert "already passed at" in await fake.service.no_code_fixed(WORK, PR, HEAD)
    # A review asked for again at the passed head: its own verdict decides.
    fake.note(PR, HEAD, "asked", f"asked:{PR}@{HEAD}", fake.now + 1, {})
    assert fake.service.no_code_head(WORK, PR) is None
    assert "no no-code verdict or standing pass" in await fake.service.no_code_fixed(
        WORK, PR, OTHER
    )
    fake.now += 5
    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:2")
    assert fake.service.no_code_head(WORK, PR) == (HEAD, None)
    # The agent pushed a diagram fix: the pass carries to the new head, from the passed one.
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = "e" * 64
    fake.now += 5
    assert verdict_settled(await fake.service.no_code_fixed(WORK, PR, OTHER))
    kept = fake.passed()
    assert kept.head == OTHER and kept.data["doc-only"] == HEAD and kept.key.startswith("doc-only:")
    assert kept.data["carried_from"] == HEAD and kept.data["digest"] == "e" * 64
    assert any(
        ":verdict:pass:" in key and "which passed review" in text for _c, key, text in fake.told
    )
    # A second doc push chains on: each row names only the head before it.
    third = "9" * 40
    assert fake.service.no_code_head(WORK, PR) == (OTHER, None)
    fake.set(PR, head=third)
    fake.digests[(PR, third)] = "f" * 64
    fake.now += 5
    assert verdict_settled(await fake.service.no_code_fixed(WORK, PR, third))
    assert fake.passed().head == third and fake.passed().data["carried_from"] == OTHER
    # Findings after it hold the pull request again: a doc push no longer keeps anything.
    await fake.service.on_verdict(WORK, "changes-needed", third[:12], "")
    assert fake.service.no_code_head(WORK, PR) is None


async def test_a_doc_only_push_after_ship_now_stays_held_by_the_press(fake):
    """#4257: a Ship now pass a doc push keeps still carries its note, so the merge stays held
    by the head the press passed (#4212), not the pushed one."""
    from agentdeck.review_history import SHIP_NOTE, ship_pass_head

    fake.policy = Policy(auto_release=False)
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "")
    await fake.service.owner_accepted(WORK, PR, HEAD, SHIP_NOTE)
    assert fake.service.no_code_head(WORK, PR) == (HEAD, None)
    fake.set(PR, head=OTHER)
    fake.digests[(PR, OTHER)] = "e" * 64
    assert verdict_settled(await fake.service.no_code_fixed(WORK, PR, OTHER))
    assert fake.passed().data["note"] == SHIP_NOTE
    assert ship_pass_head(fake.service._history(PR), OTHER) == HEAD


async def test_an_owner_accepting_findings_passes_the_head_with_no_review(fake):
    """#3828: findings the owner accepted with no code change pass the reviewed head, which
    merges once green; never a head the branch left."""
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "")
    assert fake.merged == []
    assert "not open at" in await fake.service.owner_accepted(WORK, PR, OTHER, "keep it")
    answer = await fake.service.owner_accepted(WORK, PR, HEAD, " Owner: accept as is ")
    assert verdict_settled(answer) and fake.merge_heads == [HEAD]
    assert fake.passed().kind == "accepted" and fake.passed().data["note"] == "Owner: accept as is"
    assert f"verdict:pass:{PR}@{HEAD[:12]}" in fake.entry["told"]
    assert not any(":verdict:pass:" in key for _chat, key, _text in fake.told)  # #4253


async def test_a_ship_now_pass_merges_only_once_its_press_finished(fake):
    """#4212 review: a Ship now press passes the head before it marks it shipped, so the pass
    holds the merge, at the head a base update carries it to too, until the press finished;
    an unreadable answer holds it as well."""
    from agentdeck.review_history import SHIP_NOTE

    unfinished = {HEAD: True}

    def ship_unfinished(pull, head):
        if unfinished.get(head) is None:
            raise RuntimeError("db locked")
        return unfinished[head]

    fake.service.ports.ship_unfinished = ship_unfinished
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "")
    await fake.service.owner_accepted(WORK, PR, HEAD, SHIP_NOTE)
    assert fake.merge_heads == []
    assert "Ship now at" in await fake.service.evaluate(WORK)
    # The merge queue's base update carries the pass; it still holds by the shipped head.
    fake.set(PR, head=OTHER, state="OPEN")
    fake.digests[(PR, OTHER)] = DIGEST
    fake.green = {OTHER}
    assert "is not finished; press Ship now again" in await fake.service.evaluate(WORK)
    unfinished.pop(HEAD)
    assert "is not finished" in await fake.service.evaluate(WORK)
    assert fake.merge_heads == []
    # A second base update carries it on: still held by the head the press passed.
    third = "9" * 40
    fake.set(PR, head=third, state="OPEN")
    fake.digests[(PR, third)] = DIGEST
    fake.green = {third}
    unfinished[HEAD] = True
    assert "is not finished" in await fake.service.evaluate(WORK)
    assert fake.history[-1][4]["carried_from"] == OTHER  # each row names only the head before
    unfinished[HEAD] = False
    await fake.service.evaluate(WORK)
    assert fake.merge_heads == [third]


async def test_a_shipped_merge_keeps_the_issue_open_until_its_fix_merges(fake):
    """#4212 review: Ship now merges a head with its findings left to this Work, so every live
    check of the Work says to keep the issue open until the pull request named as their fix
    merges; an unrelated pull request merging, with it or after it, fixes nothing. The fix's
    own merge then asks the usual live check, which closes it."""
    from agentdeck.review_history import SHIP_NOTE

    fixes = {PR: ""}
    fake.service.ports.shipped_findings = lambda work_id: (
        list(fixes.items()) if work_id == WORK else []
    )
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "")
    await fake.service.owner_accepted(WORK, PR, HEAD, SHIP_NOTE)
    assert fake.merge_heads == [HEAD]
    [shipped] = live_checks(fake)
    assert f"But {PR} shipped with Ship now" in shipped
    assert "keep the issue open, whatever the result" in shipped
    assert '"fixes": "<the shipped pull request>"' in shipped
    # Merged in one batch with an unrelated pull request, it is still left.
    entry: dict = {}
    assert fake.service._findings_left(WORK, entry, [PR, "eerovil/agentdeck#10"]) == [PR]

    async def merge(pull):
        fake.pulls[pull] = Pull(pull, "OPEN", False, OTHER, "MERGEABLE", "CLEAN", "green")
        fake.digests[(pull, OTHER)] = DIGEST
        fake.green = {OTHER}
        await fake.service.evaluate(WORK)
        fake.set(pull, state="MERGED")
        await fake.service.on_pull_event(WORK, pull, "closed", True)
        await fake.service.evaluate(WORK)
        return live_checks(fake)[-1]

    # An unrelated pull request merges before the fix: the issue still stays open.
    unrelated = await merge("eerovil/agentdeck#11")
    assert len(live_checks(fake)) == 2
    assert f"But {PR} shipped with Ship now" in unrelated
    # The agent names its fix when it registers it; that one's merge closes the issue.
    fixes[PR] = "eerovil/agentdeck#12"
    fixed = await merge("eerovil/agentdeck#12")
    assert len(live_checks(fake)) == 3
    assert "shipped with Ship now" not in fixed
    assert "close the issue yourself" in fixed
    # A split head's fix merged in an earlier live check stays merged.
    later = {"merged_pulls": ["eerovil/agentdeck#12"]}
    assert fake.service._findings_left(WORK, later, ["eerovil/agentdeck#13"]) == []
    # The shipped pull request named as its own fix fixes nothing (#4212 review).
    fixes[PR] = PR
    assert fake.service._findings_left(WORK, later, [PR]) == [PR]

    # An unreadable record is unknown, not "none left": no close is authorized.
    def unreadable(work_id):
        raise OSError("db locked")

    fake.service.ports.shipped_findings = unreadable
    unread = await merge("eerovil/agentdeck#13")
    assert "could not read whether this Work shipped" in unread
    assert "keep it open while any shipped" in unread


@pytest.mark.parametrize("by_deck", [True, False])
async def test_a_split_ship_tells_the_agent_once_when_its_head_merges_beside_the_draft_fix(
    fake, by_deck
):
    """#4212 review: Ship now split reviewed A from fix B, which waits as a draft until A
    merges. A merging, by the deck's own merge or by hand, tells the agent once, with its
    GitHub event after, though B is still open; B stays the draft the agent turns ready."""
    fix = "eerovil/agentdeck#12"
    fake.service.ports.shipped_findings = lambda work_id: [(PR, fix)] if work_id == WORK else []
    fake.pulls[fix] = Pull(fix, "OPEN", True, OTHER, "MERGEABLE", "CLEAN", "green")
    if by_deck:
        await passed(fake, targets=(PR,))
        assert fake.merge_heads == [HEAD]
    fake.set(PR, state="MERGED")
    await fake.service.on_pull_event(WORK, PR, "closed", True)
    await fake.service.evaluate(WORK)
    [told] = live_checks(fake)
    assert PR in told and fix not in told.split("shipped with Ship now")[0]
    assert "merge its base into it now and mark it ready" in told
    assert "keep the issue open" in told
    assert fake.pulls[fix].draft and fake.pulls[fix].state == "OPEN"
    # B goes ready and merges: the Work's usual live check, which closes it.
    fake.set(fix, draft=False)
    fake.digests[(fix, OTHER)] = DIGEST
    fake.green = {OTHER}
    await passed(fake, head=OTHER, targets=(fix,))
    fake.set(fix, state="MERGED")
    await fake.service.on_pull_event(WORK, fix, "closed", True)
    await fake.service.evaluate(WORK)
    assert len(live_checks(fake)) == 2
    assert "shipped with Ship now" not in live_checks(fake)[-1]


async def test_each_verdict_is_written_into_its_full_heads_history(fake):
    """#3957: every verdict writer appends one event at the full head it stands at, keyed by
    the comment or request it came in; a base merge's carried pass is its own row."""
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", source="comment:5")
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", source="comment:5")
    findings = f"Review of {PR} at {HEAD[:9]}"
    assert await fake.service.stale_findings(WORK, findings, "", "r1") == ""
    assert await fake.service.on_findings(WORK, "r1", findings, "https://x/1")
    await fake.service.owner_accepted(WORK, PR, HEAD, "keep it")
    fake.set(PR, head=OTHER, state="OPEN")
    fake.digests[(PR, OTHER)] = DIGEST
    fake.green = {OTHER}
    await fake.service.evaluate(WORK)
    assert fake.merge_heads[-1] == OTHER
    rows = [(head, kind, key) for _pull, head, kind, key, _data, _at in fake.history]
    assert rows[:3] == [
        (HEAD, "changes", f"comment:5:{PR}"),
        (HEAD, "changes", f"comment:5:{PR}"),  # the store's unique key absorbs the replay
        (HEAD, "findings", "findings:r1:told"),
    ]
    assert rows[3][:2] == (HEAD, "accepted") and rows[4] == (OTHER, "pass", f"carried:{PR}@{OTHER}")
    assert fake.history[3][4]["note"] == "keep it"
    assert fake.history[4][4]["carried_from"] == HEAD and fake.history[4][4]["digest"] == DIGEST
    await fake.service.on_pull_event(WORK, PR, "closed", True)
    assert fake.forgotten == [PR]


async def test_a_verdict_delivered_late_is_dated_by_its_comment_and_findings_say_if_told(fake):
    """#3957 review: the newer comment decides whatever order the webhooks came in, and the
    history keeps whether the deck reviewer's findings reached the agent."""
    from agentdeck.review_history import ReviewEvent, head_review

    def answer():
        return head_review(
            ReviewEvent(pull, head, at, kind, key, data, seq)
            for seq, (pull, head, kind, key, data, at) in enumerate(fake.history)
        )

    await fake.service.on_verdict(WORK, "pass", HEAD[:12], DIGEST, source="comment:2", at=200.0)
    late = {"source": "comment:1", "at": 100.0}  # the older comment, delivered second
    await fake.service.on_verdict(WORK, "changes-needed", HEAD[:12], "", **late)
    assert answer().verdict == "pass"
    fake.history.clear()
    finding = f"Review of {PR} at {HEAD[:9]}: fix x"
    ports = fake.service.ports
    fake.service.ports = replace(ports, tell=lambda chat, message_id, text: False)
    assert await fake.service.stale_findings(WORK, finding, "", "req-9") == ""
    assert not await fake.service.on_findings(WORK, "req-9", finding, "https://x/9")
    assert answer().verdict == "changes" and not answer().findings_told
    fake.service.ports = ports
    fake.now += 1
    assert await fake.service.on_findings(WORK, "req-9", finding, "https://x/9")
    assert answer().findings_told and [row[1] for row in fake.history] == [HEAD, HEAD]


def test_the_review_chat_is_the_newest_one_opened_at_that_head(fake):
    """#2992, #3956: read from the head's ``chat`` rows, the newest one winning."""
    fake.note(PR, HEAD, "chat", "c10", 5.0, {"chat": "eerovil/review-chats#10"})
    fake.note(PR, HEAD, "chat", "c12", 6.0, {"chat": "eerovil/review-chats#12"})
    fake.note(PR, HEAD, "chat", "c11", 4.0, {"chat": "eerovil/review-chats#11"})  # older
    fake.note(PR, OTHER, "chat", "c14", 9.0, {"chat": "eerovil/review-chats#14"})
    assert fake.service.review_chat(PR, HEAD[:12]) == "eerovil/review-chats#12"
    assert fake.service.review_chat(PR, "c" * 40) is None
    assert fake.service.review_chat(PR, HEAD[:6]) is None


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


@pytest.mark.parametrize("checks_gate", [True, False])
async def test_checks_still_pending_when_their_suite_completes_are_read_again(fake, checks_gate):
    """#3911: the review passed first; GitHub then sent the suite's ``completed`` while its
    rollup still read a run going, and no later delivery came, so the Work never merged."""
    tick, timers = timed(fake)
    fake.policy = Policy(
        auto_release=True, github_checks=frozenset({"eerovil/agentdeck"} if checks_gate else ())
    )
    fake.set(PR, checks="pending")
    await passed(fake)
    assert await fake.service.on_checks_done(WORK) == f"{PR}'s checks are pending at {HEAD[:12]}"
    await fake.service.on_checks_done(WORK)  # a second run of the same suite finishes
    assert fake.merged == [] and [t[2] for t in timers] == [True, False], "one look, not two"
    fake.set(PR, checks="green")  # the rollup catches up
    await tick(59)
    assert fake.merged == [], "not due yet"
    await tick(1)
    assert fake.merged == [(PR, "merge")] and fake.merge_heads == [HEAD]
    assert "waiting_checks" not in fake.entry
    await fake.service.on_checks_done(WORK)
    assert len(timers) == 2, "a Work no longer waiting on its checks sets no look"


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
