"""The deck's merge queue (#3022): one reviewed pull request per repository at a time.

Before it, every merge put every other reviewed pull request behind; the deck updated each one
and each needed a new full verification run, so at most one merged per test cycle while the
others started over. Now only the first in line is brought up to its base and re-tested.
"""

from __future__ import annotations

import pytest

from agentdeck import work_release
from agentdeck.work_release import (
    DIFF_TOO_LARGE,
    MERGED,
    Policy,
    Ports,
    Pull,
    ReleaseStore,
    WorkReleaseService,
)

REPO = "eerovil/agentdeck"
DIGEST = "d" * 64


def work(n: int) -> str:
    return f"github_issue:{REPO}#{n}"


def pull(n: int) -> str:
    return f"{REPO}#{n}"


class Deck:
    """Three Works, each with one pull request, all reviewed and green at their heads."""

    def __init__(self, tmp_path, works=(1, 2, 3)) -> None:
        self.now = 1000.0
        self.base = 0  # commits on master; a pull request is behind when it lacks some
        self.pulls: dict[str, Pull] = {}
        self.based: dict[str, int] = {}
        self.owner: dict[str, str] = {}
        self.green: dict[str, set[str]] = {}
        self.red: dict[str, set[str]] = {}
        self.updated: list[str] = []
        self.merged: list[str] = []
        self.told: list[tuple[str, str]] = []
        self.extra: dict[str, tuple[str, ...]] = {}  # a Work's further pull requests
        self.stuck: set[str] = set()  # pull requests whose update answers yes and never lands
        self.timers: list[list] = []  # [due, look, cancelled] the release asked to run later
        self.policy = Policy(auto_release=True)
        self.starred: set[str] = set()
        self.changed: dict[tuple[str, str], str] = {}  # (pull, head) -> a diff that changed
        self.store_path = tmp_path / "work-release.json"
        for n in works:
            ref, head = pull(100 + n), f"{n:x}" * 40
            self.pulls[ref] = Pull(ref, "OPEN", False, head, "MERGEABLE", "CLEAN", "green")
            self.based[ref] = 0
            self.owner[work(n)] = ref
            self.green[work(n)] = {head}
        self.service = self.build()

    def build(self) -> WorkReleaseService:
        return WorkReleaseService(
            ReleaseStore(self.store_path),
            Ports(
                switched=lambda repository: repository == REPO,
                paused=lambda work_id: False,
                starred=lambda work_id: work_id in self.starred,
                policy=lambda repository: self.policy,
                pulls=lambda work_id: (self.owner[work_id], *self.extra.get(work_id, ())),
                chat=lambda work_id: f"chat:{work_id}",
                read_pull=self.read_pull,
                digest=self.digest,
                green_heads=lambda work_id: self._set(self.green.get(work_id, set())),
                failed_heads=lambda work_id: self._set(self.red.get(work_id, set())),
                merge=self.merge,
                update_branch=self.update_branch,
                tell=lambda chat, message_id, text: self.told.append((message_id, text)),
                clock=lambda: self.now,
                later=self.later,
            ),
        )

    def later(self, delay, look):
        timer = [self.now + delay, look, False]
        self.timers.append(timer)

        class Handle:
            def cancel(self) -> None:
                timer[2] = True

        return Handle()

    async def tick(self, seconds: float) -> None:
        """Let ``seconds`` pass, running every timed look that falls due (and no job moving)."""
        self.now += seconds
        while due := [t for t in self.timers if not t[2] and t[0] <= self.now]:
            for timer in due:
                timer[2] = True
                await timer[1]()

    @staticmethod
    async def _set(values):
        return set(values)

    async def read_pull(self, ref):
        current = self.pulls[ref]
        behind = self.base - self.based[ref]
        return Pull(
            **{
                **current.__dict__,
                "behind_by": behind,
                "merge_state": "BEHIND" if behind else current.merge_state,
            }
        )

    async def digest(self, ref, head):
        # The base merged in changes no diff the reviewer read, unless a test says it did.
        return self.changed.get((ref, head), DIGEST)

    async def merge(self, ref, method, head):
        assert self.base == self.based[ref], "merged a branch behind its base"
        self.merged.append(ref)
        self.pulls[ref] = Pull(**{**self.pulls[ref].__dict__, "state": "MERGED"})
        self.base += 1
        return MERGED, ""

    async def update_branch(self, ref):
        self.updated.append(ref)
        if ref in self.stuck:
            return True
        self.based[ref] = self.base
        old = self.pulls[ref]
        self.pulls[ref] = Pull(**{**old.__dict__, "head": f"{len(self.updated):x}e" * 20})
        return True

    async def review_all(self):
        for work_id in self.owner:
            await self.service.on_verdict(
                work_id, "pass", self.pulls[self.owner[work_id]].head, DIGEST
            )

    def retest(self, n: int, ok: bool = True) -> None:
        """The agent of Work ``n`` ran its verification on the current head."""
        head = self.pulls[self.owner[work(n)]].head
        (self.green if ok else self.red).setdefault(work(n), set()).add(head)

    def line(self) -> list[str]:
        return [row["pull"] for row in self.service.queue(REPO)]


@pytest.fixture
def deck(tmp_path):
    return Deck(tmp_path)


async def test_only_the_first_in_line_is_updated_and_the_rest_wait_untouched(deck):
    await deck.review_all()
    # Work 1 merged at once (it was up to date); 2 and 3 lined up behind it.
    assert deck.merged == [pull(101)]
    # Only the new first in line, 102, was brought up to master: one re-test, not two.
    assert deck.updated == [pull(102)]
    assert deck.line() == [pull(102), pull(103)]
    answer = await deck.service.evaluate(work(3))
    assert "merge queue, 2 of 2" in answer
    assert deck.updated == [pull(102)], "103 is not updated while it waits"


async def test_a_green_retest_merges_the_first_and_moves_the_next_up(deck):
    await deck.review_all()
    deck.retest(2)
    await deck.service.on_tests()
    assert deck.merged == [pull(101), pull(102)]
    assert deck.updated == [pull(102), pull(103)]
    assert deck.line() == [pull(103)]
    deck.retest(3)
    await deck.service.on_tests()
    assert deck.merged == [pull(101), pull(102), pull(103)]
    assert deck.line() == []
    # Three merges cost two re-tests: each one ran once, on the tree that merged.
    assert len(deck.updated) == 2


async def test_the_agent_is_told_its_round_when_its_branch_is_updated(deck):
    await deck.review_all()
    told = [text for key, text in deck.told if ":behind:" in key]
    assert len(told) == 1 and "first in the merge queue (round 1)" in told[0]


async def test_a_failed_retest_drops_the_first_and_the_next_goes(deck):
    await deck.review_all()
    deck.retest(2, ok=False)
    await deck.service.on_tests()
    assert deck.line() == [pull(103)]
    assert deck.updated == [pull(102), pull(103)]
    assert any(":queue-red:" in key for key, _text in deck.told)


async def test_a_work_that_loses_its_pass_leaves_the_line(deck):
    await deck.review_all()
    await deck.service.on_verdict(work(3), "changes-needed", deck.pulls[pull(103)].head, DIGEST)
    assert deck.line() == [pull(102)]


async def test_a_reorder_puts_the_named_pull_request_first_and_wakes_it(deck):
    await deck.review_all()
    deck.service.reorder(REPO, ["#103"])
    await deck.service.wake_first()
    assert deck.line() == [pull(103), pull(102)]
    assert deck.updated == [pull(102), pull(103)]
    with pytest.raises(ValueError):
        deck.service.reorder(REPO, ["#999"])
    with pytest.raises(ValueError):
        deck.service.reorder(REPO, ["other/repo#103"])


async def test_a_starred_work_goes_behind_the_starred_but_never_before_the_first(tmp_path):
    """#3116: a starred Work's pull joins behind the other starred ones, and starring a queued
    one moves it up the same way, but the first in line, maybe mid re-test, keeps its place."""
    deck = Deck(tmp_path, works=(1, 2, 3, 4, 5))
    deck.starred = {work(5)}
    await deck.review_all()
    assert deck.merged == [pull(101)]
    assert deck.line() == [pull(102), pull(105), pull(103), pull(104)]
    deck.starred.add(work(4))
    deck.service.star(work(4))
    assert deck.line() == [pull(102), pull(105), pull(104), pull(103)]
    deck.starred.add(work(2))
    deck.service.star(work(2))  # already first: stays there
    assert deck.line() == [pull(102), pull(105), pull(104), pull(103)]
    assert deck.updated == [pull(102)], "a star never re-tests anyone"


def test_the_star_api_records_the_star_and_moves_the_queue(deck, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agentdeck.db import Db
    from agentdeck.web import routes_works

    deck.service.store.queues[REPO] = [
        {"pull": pull(101), "work": work(1), "at": 1.0, "rounds": 1},
        {"pull": pull(102), "work": work(2), "at": 2.0, "rounds": 0},
        {"pull": pull(103), "work": work(3), "at": 3.0, "rounds": 0},
    ]
    db = Db(tmp_path / "control.sqlite3")
    # The live table, as ``app.py`` reads it.
    deck.service.ports.starred = lambda work_id: work_id in db.starred_works()
    app = FastAPI()
    app.include_router(routes_works.router)
    app.state.work_release = deck.service
    app.state.control_db = db
    with TestClient(app) as client:
        # The short spelling the agent guide gives resolves to the Work's own id.
        answer = client.put(f"/api/works/{REPO}/3/star")
        assert answer.json() == {"work_id": work(3), "starred": True}
        assert deck.line() == [pull(101), pull(103), pull(102)]
        assert client.put(f"/api/works/{REPO}/3/star").status_code == 200  # idempotent
        assert db.starred_works() == {work(3)}
        assert client.delete(f"/api/works/{REPO}/3/star").json()["starred"] is False
        assert db.starred_works() == frozenset()
        assert deck.line() == [pull(101), pull(103), pull(102)], "unstarring moves nothing back"
        assert client.put("/api/works/not-a-work/star").status_code == 404


async def test_the_line_survives_a_restart(deck):
    await deck.review_all()
    again = deck.build()
    assert [row["pull"] for row in again.queue(REPO)] == [pull(102), pull(103)]
    assert again.store.place(work(3)) == [(pull(103), 2, 2, 0)]
    assert again.store.place(work(2)) == [(pull(102), 1, 2, 1)]


async def test_without_auto_release_only_a_pressed_merge_joins_the_line(deck):
    deck.policy = Policy(auto_release=False)
    await deck.review_all()
    assert deck.line() == [] and deck.merged == []
    await deck.service.request_merge(work(2))
    assert deck.merged == [pull(102)]
    await deck.service.request_merge(work(3))
    await deck.service.request_merge(work(1))
    assert deck.line() == [pull(103), pull(101)]
    assert deck.updated == [pull(103)]
    # #3175: the press survives the deck's update to base, and the line says it waits.
    told = [text for key, text in deck.told if ":behind:" in key]
    assert "A person pressed Merge: the press carries over" in told[-1]
    assert deck.service.store.get(work(3))["hold"]["words"].startswith("Merge pressed; ")
    deck.retest(3)
    await deck.service.on_tests()
    assert deck.merged == [pull(102), pull(103)], "merged on the old press once green"
    assert deck.updated == [pull(103), pull(101)]
    # A diff that changed (a conflict resolved by hand, say) needs a new press, once
    # reviewed again at it, however green.
    head = deck.pulls[pull(101)].head
    deck.changed[(pull(101), head)] = "e" * 64
    deck.retest(1)
    await deck.service.on_verdict(work(1), "pass", head, "e" * 64)
    assert deck.merged == [pull(102), pull(103)]
    assert "asked" not in deck.service.store.get(work(1))
    assert deck.service.store.get(work(1))["awaiting_merge"] == {pull(101): head}
    await deck.service.request_merge(work(1))
    assert deck.merged[-1] == pull(101)


def test_only_a_settled_failure_of_the_works_verification_counts_as_red():
    from agentdeck.poller_engine.verification import failed_head_for_work_alone

    head = "a" * 40
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    snapshot = {
        "root": "/w",
        "head": head,
        "staged_sha256": empty,
        "tracked_diff_sha256": empty,
        "untracked_sha256": empty,
        "untracked_count": 0,
    }
    job = {
        "id": "j1",
        "purpose": "verification",
        "work_id": work(1),
        "state": "failed",
        "result_class": "failure",
        "tested_workspace": snapshot,
        "verified_workspace": snapshot,
    }

    def red(row, jobs=()):
        return failed_head_for_work_alone(row, work_id=work(1).upper(), jobs=[row, *jobs])

    assert red(job) == head
    assert red({**job, "result_class": "infrastructure"}) == "", "the runner failed, not the code"
    assert red({**job, "work_id": work(2)}) == ""
    assert red({**job, "purpose": ""}) == ""
    assert red({**job, "state": "done", "result_class": "success"}) == ""
    rerun = {"id": "j2", "work_id": work(1), "state": "running"}
    assert red(job, [rerun]) == "", "a follow-up still running may forgive it"
    dirty = {**snapshot, "untracked_count": 1, "untracked_sha256": "f" * 64}
    assert red({**job, "verified_workspace": dirty}) == ""
    # A green run on a tree that changed under it proves nothing, and says so (#3097).
    from agentdeck.poller_engine.verification import moved_head_for_work_alone

    green = {**job, "state": "done", "result_class": "success"}

    def moved(row):
        return moved_head_for_work_alone(row, work_id=work(1).upper())

    assert moved({**green, "verified_workspace": dirty}) == head
    assert moved(green) == "", "a run that proves its head"
    assert moved({**green, "verified_workspace": {**snapshot, "unprovable": ["x"]}}) == ""
    assert moved({**job, "verified_workspace": dirty}) == "", "a failure, not a green run"
    # A green run that started on an unclean tree proves nothing either, and says so (#3397).
    from agentdeck.poller_engine.verification import unclean_head_for_work_alone

    def unclean(row):
        return unclean_head_for_work_alone(row, work_id=work(1).upper())

    started_dirty = {**green, "tested_workspace": dirty, "verified_workspace": dirty}
    assert unclean(started_dirty) == head and moved(started_dirty) == ""
    assert unclean(green) == "" and unclean({**green, "verified_workspace": dirty}) == ""
    assert unclean({**started_dirty, "state": "failed", "result_class": "failure"}) == ""
    assert unclean({**started_dirty, "work_id": work(2)}) == ""
    unread = {**dirty, "unprovable": ["untracked files"]}
    assert unclean({**green, "tested_workspace": unread, "verified_workspace": unread}) == ""


def test_the_merge_queue_api_reads_and_reorders(deck):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agentdeck.web import routes_works

    deck.base = 1  # master moved: both are behind
    for n in (2, 3):
        head = deck.pulls[pull(100 + n)].head
        passed = {pull(100 + n): {"head": head, "digest": DIGEST}}
        deck.service.store.put(work(n), {"passes": passed})
    deck.service.store.queues[REPO] = [
        {"pull": pull(102), "work": work(2), "at": 1.0, "rounds": 1},
        {"pull": pull(103), "work": work(3), "at": 2.0, "rounds": 0},
    ]
    app = FastAPI()
    app.include_router(routes_works.router)
    app.state.work_release = deck.service
    with TestClient(app) as client:
        assert client.get("/api/merge-queue/other/repo").status_code == 404
        read = client.get(f"/api/merge-queue/{REPO}").json()["queue"]
        assert [row["pull"] for row in read] == [pull(102), pull(103)]
        assert client.put(f"/api/merge-queue/{REPO}", json={"order": "x"}).status_code == 400
        assert client.put(f"/api/merge-queue/{REPO}", json={"order": ["#1"]}).status_code == 400
        moved = client.put(f"/api/merge-queue/{REPO}", json={"order": ["agentdeck#103"]})
        assert [row["pull"] for row in moved.json()["queue"]] == [pull(103), pull(102)]
    # The one moved to the front was brought up to its base at once.
    assert deck.updated == [pull(103)]


async def test_a_work_the_deck_no_longer_releases_does_not_hold_the_line(deck):
    await deck.review_all()
    # Its project stopped being the deck's to release (``handles``: switched and enabled).
    handles = deck.service.handles
    deck.service.handles = lambda work_id: work_id != work(2) and handles(work_id)
    # The next event on the one behind it clears the line.
    await deck.service.evaluate(work(3))
    assert deck.line() == [pull(103)]
    assert deck.updated == [pull(102), pull(103)]


async def test_an_unreadable_digest_keeps_the_first_in_line(deck):
    """#3022 review: a failed diff read is no answer, not a lost pass."""
    await deck.review_all()
    readable = deck.digest

    async def unreadable(ref, head):
        return ""

    deck.service.ports.digest = unreadable
    answer = await deck.service.evaluate(work(2))
    assert "could not be read" in answer and "merge queue 1 of 2" in answer
    assert deck.line() == [pull(102), pull(103)]
    deck.service.ports.digest = readable
    deck.retest(2)
    await deck.service.on_tests()
    assert deck.merged == [pull(101), pull(102)]


async def test_a_diff_too_large_to_read_loses_the_pass_and_the_place(deck):
    """#3029: GitHub never serves it, so waiting for it would hold the line for good."""
    await deck.review_all()
    readable = deck.digest
    # Work 2's agent pushed a change too large to diff while first in line.
    ref = pull(102)
    deck.pulls[ref] = Pull(**{**deck.pulls[ref].__dict__, "head": "f0" * 20})
    deck.retest(2)

    async def too_large(ref, head):
        return DIFF_TOO_LARGE if ref == pull(102) else await readable(ref, head)

    deck.service.ports.digest = too_large
    answer = await deck.service.evaluate(work(2))
    assert "has no review pass" in answer
    assert deck.line() == [pull(103)]
    assert deck.updated == [pull(102), pull(103)], "the next in line goes"


async def test_an_update_that_never_lands_is_asked_again_then_leaves_the_line(deck):
    """#3033: #3027's update answered yes and its branch never moved, holding the line."""
    deck.stuck.add(pull(102))
    await deck.review_all()
    assert deck.updated == [pull(102)] and deck.line() == [pull(102), pull(103)]
    answer = await deck.service.evaluate(work(2))
    assert "has not shown the update yet" in answer
    assert deck.updated == [pull(102)], "no second update inside the grace"
    deck.now += work_release.UPDATE_GRACE_S
    await deck.service.on_tests()  # a job moving looks again: it waits on its tests
    assert deck.updated == [pull(102), pull(102)]
    assert deck.line() == [pull(102), pull(103)]
    deck.now += work_release.UPDATE_GRACE_S
    await deck.service.on_tests()
    assert deck.line() == [pull(103)]
    assert deck.updated == [pull(102), pull(102), pull(103)], "the next in line goes"
    assert any(":update-unlanded:" in key for key, _text in deck.told)
    # It stays out of line at that head: rejoining would hold the line again.
    answer = await deck.service.evaluate(work(2))
    assert "its agent merges the base in" in answer
    assert deck.line() == [pull(103)]
    # A new head from its agent puts it back in line.
    deck.stuck.discard(pull(102))
    ref = pull(102)
    deck.pulls[ref] = Pull(**{**deck.pulls[ref].__dict__, "head": "ab" * 20})
    deck.based[ref] = deck.base
    await deck.service.on_verdict(work(2), "pass", deck.pulls[ref].head, DIGEST)
    deck.retest(2)
    await deck.service.evaluate(work(2))
    assert deck.line() == [pull(103), pull(102)]


async def test_a_closed_pull_request_first_in_line_lets_the_next_go(deck):
    """#3033: a close without a merge was ignored, so it held the line."""
    await deck.review_all()
    deck.pulls[pull(102)] = Pull(**{**deck.pulls[pull(102)].__dict__, "state": "CLOSED"})
    await deck.service.on_pull_event(work(2), "eerovil/AgentDeck#102", "closed", False)
    assert deck.line() == [pull(103)]
    assert deck.updated == [pull(102), pull(103)]


async def test_changes_needed_on_one_pull_takes_all_of_its_work_out_of_line(tmp_path):
    """#3033: the Work merges nothing without that pass, so its other pull request first in
    line held the line until the Work's next event."""
    deck = Deck(tmp_path, works=(1, 2, 3))
    await deck.review_all()
    assert deck.line() == [pull(102), pull(103)]
    # Work 2 also registered 104, reviewed and green; 104 is not in line yet.
    ref = pull(104)
    deck.pulls[ref] = Pull(ref, "OPEN", False, "c4" * 20, "MERGEABLE", "CLEAN", "green")
    deck.based[ref] = deck.base
    deck.extra[work(2)] = (ref,)
    deck.service.store.queues[REPO][:] = [
        {"pull": pull(104), "work": work(2), "at": 0, "rounds": 0},
        *[row for row in deck.service.store.queues[REPO] if row["pull"] != pull(102)],
        {"pull": pull(102), "work": work(2), "at": 0, "rounds": 1},
    ]
    await deck.service.on_verdict(
        work(2), "changes-needed", deck.pulls[pull(102)].head, DIGEST, (pull(102),)
    )
    assert deck.line() == [pull(103)]
    assert pull(103) in deck.updated, "the next in line was woken"


async def test_a_stuck_update_clears_the_line_on_a_clock_with_no_job_moving(deck):
    """#3033 review: the queue behind is green and runs no jobs, so time alone must clear it."""
    deck.stuck.add(pull(102))
    await deck.review_all()
    assert deck.line() == [pull(102), pull(103)]
    live = [t for t in deck.timers if not t[2]]
    assert len(live) == 1, "one timed look per Work, an older one replaced"
    await deck.tick(work_release.UPDATE_GRACE_S - 1)
    assert deck.updated == [pull(102)], "not before the grace is up"
    await deck.tick(1)
    assert deck.updated == [pull(102), pull(102)], "asked again once"
    await deck.tick(work_release.UPDATE_GRACE_S)
    assert deck.line() == [pull(103)]
    assert deck.updated == [pull(102), pull(102), pull(103)], "the next in line goes"


async def test_a_work_held_by_its_place_is_looked_at_once_it_is_first(deck):
    """#3034: becoming first by a change that woke nobody still brings it up to its base."""
    await deck.review_all()
    assert deck.service.store.works[work(3)]["queued"] == [pull(103)]
    # 102 leaves the line without waking 103 (a lost wake).
    deck.service.store.queues[REPO][:] = deck.service.store.queues[REPO][1:]
    deck.service.store.put_queues()
    await deck.service.on_tests(jobs=False, chats=True)
    assert deck.updated == [pull(102), pull(103)]
    assert "queued" not in deck.service.store.works[work(3)]


async def test_a_reorder_that_races_the_works_own_evaluation_still_wakes_it(deck):
    """#3034: its evaluation saw it second, the reorder made it first meanwhile."""
    await deck.review_all()
    service = deck.service
    raced = []

    def reorder_once():
        if not raced and (service.store.works.get(work(3)) or {}).get("queued"):
            raced.append(True)
            service.reorder(REPO, ["#103"])

    service.store.put(
        work(3), {k: v for k, v in service.store.get(work(3)).items() if k != "queued"}
    )
    service.store.on_change = reorder_once
    await service.evaluate(work(3))
    assert raced
    assert deck.updated == [pull(102), pull(103)], "103 was brought up to its base"


async def test_the_first_in_line_with_no_mark_is_looked_at_again(deck, caplog):
    """#3034: an evaluation that left no mark does not hold the line for good."""
    await deck.review_all()
    deck.retest(2)
    # Its last evaluation ended on something no job or chat move answers.
    entry = deck.service.store.get(work(2))
    entry.pop("waiting_tests", None)
    deck.service.store.put(work(2), entry)
    await deck.service.evaluate(work(3))
    await deck.service.on_tests(jobs=False, chats=True)
    assert deck.merged == [pull(101)], "looked at a moment ago"
    deck.now += 120
    caplog.set_level("INFO", logger="agentdeck.work_release")
    await deck.service.on_tests(jobs=False, chats=True)
    assert deck.merged == [pull(101), pull(102)]
    # 103 moved up and was updated; its hold is in the journal with its place.
    assert deck.updated == [pull(102), pull(103)]
    assert any(
        f"held in the merge queue ({pull(103)} 1 of 1)" in record.message
        for record in caplog.records
    )


async def test_each_hold_in_line_is_logged_with_its_place(deck, caplog):
    await deck.review_all()
    caplog.set_level("INFO", logger="agentdeck.work_release")
    await deck.service.evaluate(work(3))
    assert any(
        f"{work(3)} held in the merge queue ({pull(103)} 2 of 2)" in record.message
        for record in caplog.records
    )
