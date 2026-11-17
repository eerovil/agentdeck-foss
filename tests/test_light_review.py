"""Lighter review of robot pull requests outside their guarded code (#4399)."""

from __future__ import annotations

import asyncio
import json

import pytest

from agentdeck import light_review as lr
from agentdeck import review_history
from agentdeck.db import Db

GUARDED = json.dumps(
    {
        "about": "x",
        "files": ["bin/", "robot/safety.py"],
        "functions": {"robot/core.py": ["drive", "Arm.move"]},
        "safety_tests": ["test_stops"],
    }
)
CORE = (
    "def drive(x):\n    return x\n\n"
    "class Arm:\n    def move(self):\n        return 1\n\n"
    "def helper():\n    return 2\n"
)
TESTS = "def test_stops():\n    assert 1\n\ndef test_other():\n    assert 2\n"


def _decide(changes, base, head, guarded=GUARDED):
    # Every guarded file is read on both sides, untouched ones too (#4401 review).
    base, head = {"robot/core.py": CORE, **base}, {"robot/core.py": CORE, **head}
    return lr.decide(guarded, changes, base.get, head.get)


M = lr.Change


@pytest.mark.parametrize(
    ("changes", "base", "head", "light", "words"),
    [
        (
            [M("M", "robot/core.py")],
            {"robot/core.py": CORE},
            {"robot/core.py": CORE.replace("return 2", "return 3")},
            True,
            "",
        ),
        ([M("M", "docs/a.md"), M("M", "README.md")], {}, {}, True, ""),
        ([M("M", "bin/test.sh")], {}, {}, False, "guarded list covers (bin/)"),
        ([M("M", "robot/safety.py")], {}, {}, False, "covers (robot/safety.py)"),
        ([M("R", "robot/new.py", "robot/safety.py")], {}, {}, False, "covers"),
        ([M("M", "setup.cfg")], {}, {}, False, "not Python, docs/ or README.md"),
        ([M("M", "tests/conftest.py")], {}, {}, False, "conftest.py"),
        ([M("A", "robot/new.py")], {}, {}, False, "adds the Python file"),
        ([M("D", "robot/old.py")], {}, {}, False, "deletes the Python file"),
        (
            [M("M", "robot/core.py")],
            {"robot/core.py": CORE},
            {"robot/core.py": CORE.replace("return x", "return -x")},
            False,
            "robot/core.py:drive",
        ),
        (
            [M("M", "robot/core.py")],
            {"robot/core.py": CORE},
            {"robot/core.py": CORE.replace("return 1", "return 0")},
            False,
            "Arm.move",
        ),
        (
            [M("M", "tests/test_a.py")],
            {"tests/test_a.py": TESTS},
            {"tests/test_a.py": TESTS.replace("assert 2", "assert 3")},
            True,
            "",
        ),
        (
            [M("M", "tests/test_a.py")],
            {"tests/test_a.py": TESTS},
            {"tests/test_a.py": TESTS.replace("assert 1", "assert 0")},
            False,
            "safety tests",
        ),
        (
            [M("M", "tests/test_a.py")],
            {"tests/test_a.py": TESTS},
            {"tests/test_a.py": TESTS.replace("test_other", "test_renamed")},
            False,
            "safety",
        ),
        (
            # #4401 review: a helper whose name only starts with "test" is not a test.
            [M("M", "tests/test_a.py")],
            {"tests/test_a.py": TESTS + "\ndef testing_helper():\n    return 1\n"},
            {"tests/test_a.py": TESTS + "\ndef testing_helper():\n    return 0\n"},
            False,
            "safety",
        ),
        (
            [M("M", "robot/x.py")],
            {"robot/x.py": "a = 1\n"},
            {"robot/x.py": "a = (\n"},
            False,
            "does not parse",
        ),
        ([], {}, {}, False, "changes nothing"),
    ],
)
def test_the_rule_names_the_first_reason_for_full_review(changes, base, head, light, words):
    decision = _decide(changes, base, head)
    assert decision.light is light, decision.rule
    assert words in decision.rule


def test_a_missing_or_broken_guarded_list_is_full_review():
    assert "missing" in lr.decide(None, [M("M", "docs/a.md")], {}.get, {}.get).rule
    assert "does not parse" in lr.decide("{", [M("M", "docs/a.md")], {}.get, {}.get).rule
    boom = lr.decide(GUARDED, [M("M", "robot/core.py")], lambda p: 1 / 0, {}.get)
    assert not boom.light and "failed" in boom.rule


def test_a_stale_guarded_name_in_an_untouched_file_is_full_review():
    """#4401 review: every guarded function must be there on both sides, not only those in
    the files the pull request changes."""
    stale = json.dumps({**json.loads(GUARDED), "functions": {"robot/core.py": ["gone"]}})
    decision = _decide([M("M", "docs/a.md")], {}, {}, guarded=stale)
    assert not decision.light and "robot/core.py:gone is missing" in decision.rule
    missing = lr.decide(GUARDED, [M("M", "docs/a.md")], {}.get, {}.get)
    assert not missing.light and "robot/core.py is missing" in missing.rule


def test_name_status_output_parses_renames():
    text = "M\0a.py\0R087\0old.py\0new.py\0A\0b.md\0"
    assert lr.parse_changes(text) == [
        M("M", "a.py"),
        M("R", "new.py", "old.py"),
        M("A", "b.md"),
    ]


@pytest.mark.parametrize(
    ("code", "out", "err", "verdict", "cases"),
    [
        (0, "no worse: fine\n", "", lr.PASS, ()),
        (4, "worse: a, b failed twice in a row on abc123456789\n", "", lr.WORSE, ("a", "b")),
        (4, "worse\n", "", lr.UNRUN, ()),
        (3, "pending: re-run on abc123456789: --case a --case b\n", "", lr.PENDING, ("a", "b")),
        (
            3,
            "pending: no finished quick run on master's code; run the quick set\n",
            "",
            lr.PENDING,
            ("master",),
        ),
        (2, "", "could not run: no store\n", lr.UNRUN, ()),
        (None, "", "", lr.UNRUN, ()),
    ],
)
def test_the_bench_answer_parses(code, out, err, verdict, cases):
    answer = lr.parse_bench(code, out, err)
    assert (answer.verdict, answer.cases) == (verdict, cases)
    if code == 2:
        assert "no store" in answer.reason


# --- the service ---

PULL = "acme/robot#5"
WORK = "github_issue:acme/issues#9"
HEAD = "a" * 40
HEAD2 = "b" * 40
TIP = "c" * 40
BASE = "d" * 40


class Harness:
    def __init__(self, tmp_path, *, limit=15):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.db = Db(tmp_path / "control.db")
        self.events: list[review_history.ReviewEvent] = []
        self.told: list[tuple[str, str, str]] = []
        self.looks: list[str] = []
        self.timers: list[tuple[float, object]] = []
        self.bench_answers: list[tuple[int | None, str, str]] = []
        self.benches: list[tuple[str, str]] = []
        self.live = HEAD
        self.head_files = {"robot/core.py": CORE.replace("return 2", "return 3")}
        self.filed: list[tuple[str, str]] = []
        self.now = 1000.0
        self.limit = limit
        self.fetch_fails = 0
        # GitHub's update commit on top of ``HEAD`` (rule 6).
        self.parents = {HEAD2: [HEAD, TIP]}
        self.committer = lr.GITHUB_COMMITTER
        self.left = "0"

        async def fetch(checkout, base, number):
            if self.fetch_fails:
                self.fetch_fails -= 1
                raise RuntimeError("fetch failed")
            return TIP, self.live

        async def git(checkout, args):
            if args[0] == "merge-base":
                return BASE + "\n"
            if args[0] == "diff":
                return "".join(f"M\0{path}\0" for path in self.head_files)
            if args[:2] == ["rev-list", "--parents"]:
                return " ".join([args[4], *self.parents.get(args[4], [])]) + "\n"
            if args[:2] == ["rev-list", "--count"]:
                return self.left + "\n"
            if args[0] == "log":
                return self.committer + "\n"
            if args[0] == "ls-tree":
                sha = args[4]
                tree = {TIP: ["guarded.json"], BASE: ["robot/core.py"]}.get(sha, [])
                if sha in (HEAD, HEAD2):
                    tree = list(self.head_files)
                return "".join(f"{name}\0" for name in tree)
            sha, _, path = args[1].partition(":")
            if sha == TIP and path == "guarded.json":
                return GUARDED
            if sha == BASE and path == "robot/core.py":
                return CORE
            if sha in (HEAD, HEAD2) and path in self.head_files:
                return self.head_files[path]
            raise RuntimeError("missing")

        async def bench(checkout, tip, head):
            self.benches.append((tip, head))
            return self.bench_answers.pop(0) if self.bench_answers else (0, "no worse\n", "")

        async def live_head(pull):
            return self.live

        async def base_branch(pull):
            return "main"

        async def digest(pull, head):
            return "dig1"

        async def find_issue(repository, title):
            return ""

        async def file_issue(repository, title, body):
            self.filed.append((repository, title))
            return "https://github.com/acme/issues/issues/77"

        async def merged(pull):
            return False

        self.service = lr.LightReview(
            lr.LightPorts(
                policy=lambda repo: (
                    ("guarded.json", self.limit, "~/robot") if repo == "acme/robot" else None
                ),
                store=self.db,
                reviews=lambda pull: list(self.events),
                live_head=live_head,
                base_branch=base_branch,
                digest=digest,
                chat=lambda work: "chat-1",
                tell=lambda chat, key, text: self.told.append((chat, key, text)) or True,
                look=self.looks.append,
                fetch=fetch,
                git=git,
                bench=bench,
                find_issue=find_issue,
                file_issue=file_issue,
                merged=merged,
                clock=lambda: self.now,
                later=lambda delay, fn: self.timers.append((delay, fn)) or _Handle(),
            )
        )

    async def settle_runs(self):
        while self.service._tasks:
            await asyncio.gather(*list(self.service._tasks), return_exceptions=True)

    async def checked(self, head=HEAD):
        decision = await self.service.checklist(PULL, head, WORK)
        await self.settle_runs()
        return decision

    def keys(self):
        return [key for _chat, key, _text in self.told]

    def entry(self, head=HEAD):
        return self.db.light_review_row(PULL)["heads"].get(head) or {}


class _Handle:
    def cancel(self):
        pass


def _asked():
    return review_history.ReviewEvent(PULL, HEAD, 1.0, review_history.ASKED, "k")


def test_a_light_head_runs_the_bench_and_tells_its_answer_once(tmp_path):
    h = Harness(tmp_path)
    decision = asyncio.run(h.checked())
    assert decision.light
    assert h.benches == [(TIP, HEAD)]
    assert h.keys() == [f"light-review:{PULL}@{HEAD}:pass:r1"]
    assert "no worse than master" in h.told[0][2]
    assert h.looks == [WORK]
    assert h.service.is_light(PULL, HEAD)
    entry = h.entry()
    assert entry["decision"] == lr.LIGHT and entry["verdict"] == lr.PASS
    assert "running" not in entry
    assert h.service.view(PULL, HEAD)["text"].startswith("Light review")


def test_the_verdict_is_written_once_and_told_again_at_each_pre_review_done(tmp_path):
    """Rule 1: no bench runs on a head with a verdict again; pre-review-done re-tells it."""
    h = Harness(tmp_path)

    async def go():
        await h.checked()
        h.bench_answers = [(4, "worse: case_a failed twice in a row on abc123456789\n", "")]
        return await h.checked()

    decision = asyncio.run(go())
    assert decision.light and len(h.benches) == 1
    assert h.entry()["verdict"] == lr.PASS
    assert h.keys() == [
        f"light-review:{PULL}@{HEAD}:pass:r1",
        f"light-review:{PULL}@{HEAD}:pass:r2",
    ]


def test_a_guarded_change_is_full_for_good_and_another_full_rule_only_for_its_head(tmp_path):
    """Rule 2: the guarded list makes the pull request full for good; any other rule makes
    only that head full."""
    h = Harness(tmp_path)
    h.head_files = {"README.txt": "x"}
    decision = asyncio.run(h.checked())
    assert not decision.light and h.entry()["kind"] == lr.OTHER
    assert h.service.full_why(PULL) == ""
    h.head_files = {"robot/core.py": CORE.replace("return x", "return -x")}
    h.live = HEAD2
    decision = asyncio.run(h.checked(HEAD2))
    assert not decision.light and h.entry(HEAD2)["kind"] == lr.GUARDED
    assert "drive" in h.service.full_why(PULL)
    assert not h.service.is_light(PULL, HEAD2)
    assert h.benches == [] and h.told == []


def test_a_review_asked_makes_it_full_for_good(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.checked())
    assert h.service.is_light(PULL, HEAD)
    h.events.append(_asked())
    assert not h.service.is_light(PULL, HEAD)
    assert asyncio.run(h.service.look(PULL, HEAD)) is None
    decision = asyncio.run(h.checked())
    assert not decision.light and "full review" in decision.rule
    assert h.service.view(PULL, HEAD)["text"].startswith("Full review")


def test_worse_is_full_for_good_and_tells_the_cases(tmp_path):
    h = Harness(tmp_path)
    h.bench_answers = [(4, "worse: case_a failed twice in a row on abc123456789\n", "")]
    asyncio.run(h.checked())
    assert h.entry()["verdict"] == lr.WORSE
    assert "case_a" in h.service.full_why(PULL)
    assert h.keys() == [f"light-review:{PULL}@{HEAD}:worse:case_a"]
    assert "holds no place in the merge queue" in h.told[0][2]
    assert asyncio.run(h.service.look(PULL, HEAD)) is None


def test_pending_tells_the_cases_and_reruns_only_at_pre_review_done(tmp_path):
    h = Harness(tmp_path)
    h.bench_answers = [(3, "pending: re-run on abc123456789: --case case_b\n", "")]
    asyncio.run(h.checked())
    look = asyncio.run(h.service.look(PULL, HEAD))
    assert look == {"verdict": lr.PENDING, "cases": ["case_b"]}
    assert len(h.benches) == 1 and h.timers == []
    asyncio.run(h.checked())
    assert len(h.benches) == 2 and h.entry()["verdict"] == lr.PASS


def test_the_same_pending_answer_twice_is_told_once(tmp_path):
    """One message per head and case set, whichever pre-review-done round asked."""
    h = Harness(tmp_path)
    pending = (3, "pending: re-run on abc123456789: --case case_b\n", "")
    h.bench_answers = [pending, pending]
    asyncio.run(h.checked())
    asyncio.run(h.checked())
    assert len(h.benches) == 2
    assert h.keys() == [f"light-review:{PULL}@{HEAD}:pending:case_b"]


def test_a_pass_drops_its_heads_undelivered_could_not_run_notice(tmp_path):
    """With the chat unreachable, a could-not-run notice stays owed; the pass that follows
    supersedes it, so the chat later hears only the pass."""
    h = Harness(tmp_path)
    h.bench_answers = [(2, "", "no bench")]
    delivered = h.service.ports.tell
    h.service.ports.tell = lambda chat, key, text: False
    asyncio.run(h.checked())
    assert [item["id"] for item in h.db.light_review_row(PULL)["owed"]][0].endswith(":r1")
    asyncio.run(h.checked())  # the agent's pre-review-done: this bench passes
    h.service.ports.tell = delivered
    asyncio.run(h.service._send_owed(PULL))
    assert [key.split(":")[2] for key in h.keys()] == ["pass"]
    assert h.db.light_review_row(PULL)["owed"] == []


def test_could_not_run_retries_once_after_15_minutes_then_asks_for_pre_review_done(tmp_path):
    h = Harness(tmp_path)
    h.bench_answers = [(2, "", "no bench"), (2, "", "no bench")]

    async def go():
        await h.checked()
        assert [delay for delay, _fn in h.timers] == [lr.RETRY_S]
        assert h.entry()["retry_at"] == h.now + lr.RETRY_S
        line = lr.line_for(h.db.light_review_row(PULL), HEAD, False)["text"]
        assert "one retry scheduled" in line
        await h.timers.pop()[1]()
        await h.settle_runs()

    asyncio.run(go())
    assert len(h.benches) == 2 and h.timers == []
    assert h.entry()["retried"] and "retry_at" not in h.entry()
    assert [key.rsplit(":", 2)[1] for key in h.keys()] == ["first", "last"]
    assert (
        "the retry could not run either"
        in lr.line_for(h.db.light_review_row(PULL), HEAD, False)["text"]
    )
    assert "tries once more in 15 minutes" in h.told[0][2]
    assert "Call pre-review-done again once the bench works" in h.told[1][2]


def test_the_retry_does_nothing_once_the_head_moved(tmp_path):
    h = Harness(tmp_path)
    h.bench_answers = [(2, "", "no bench")]

    async def go():
        await h.checked()
        h.live = HEAD2
        await h.timers.pop()[1]()
        await h.settle_runs()

    asyncio.run(go())
    assert len(h.benches) == 1 and "retry_at" not in h.entry()


def test_an_unread_live_head_keeps_the_retry_until_a_read_proves_the_head(tmp_path):
    """The one retry is spent only on a head GitHub shows live: an outage re-arms it, and a
    later read of another head drops it without a bench or a message."""
    h = Harness(tmp_path)
    h.bench_answers = [(2, "", "no bench")]

    async def go():
        await h.checked()
        told = len(h.told)
        h.live = None
        await h.timers.pop()[1]()
        assert h.entry()["retry_at"] == h.now + lr.LIVE_UNREAD_S
        assert [delay for delay, _fn in h.timers] == [lr.LIVE_UNREAD_S]
        assert not h.entry().get("retried") and len(h.benches) == 1
        h.live = HEAD2
        await h.timers.pop()[1]()
        await h.settle_runs()
        return told

    told = asyncio.run(go())
    assert len(h.benches) == 1 and len(h.told) == told
    assert "retry_at" not in h.entry() and not h.entry().get("retried")


def test_a_head_that_moved_before_the_bench_could_not_run(tmp_path):
    """Rule 7: the fetch happens in the bench's turn; another head there is no answer."""
    h = Harness(tmp_path)

    async def go():
        await h.service.checklist(PULL, HEAD, WORK)
        h.live = HEAD2
        await h.settle_runs()

    asyncio.run(go())
    assert h.benches == []
    assert h.entry()["attempt"]["verdict"] == lr.UNRUN
    assert "the head moved" in h.entry()["attempt"]["reason"]


def test_a_failed_bench_fetch_is_tried_once_more_at_once(tmp_path):
    h = Harness(tmp_path)

    async def go():
        await h.service.checklist(PULL, HEAD, WORK)
        h.fetch_fails = 1
        await h.settle_runs()

    asyncio.run(go())
    assert h.entry()["verdict"] == lr.PASS


def test_an_unread_diff_at_pre_review_done_writes_nothing(tmp_path):
    """Rule 1: an unreadable read decides nothing, and no review starts."""
    h = Harness(tmp_path)
    h.fetch_fails = 1
    with pytest.raises(lr.LightError, match="could not be read"):
        asyncio.run(h.checked())
    assert h.db.light_review_row(PULL) is None and h.benches == []
    assert asyncio.run(h.checked()).light


@pytest.mark.parametrize("blob", ["guarded", "head"])
def test_an_object_git_cannot_read_is_unread_never_missing_or_empty(tmp_path, blob):
    h = Harness(tmp_path)
    git = h.service.ports.git

    async def flaky(checkout, args):
        if args[0] == "show" and (args[1].endswith("guarded.json") == (blob == "guarded")):
            raise RuntimeError("object unreadable")
        return await git(checkout, args)

    h.service.ports.git = flaky
    with pytest.raises(lr.LightError):
        asyncio.run(h.checked())
    assert h.db.light_review_row(PULL) is None


def test_the_release_benches_a_head_the_deck_made_without_the_agent(tmp_path):
    """Rules 4 and 6: GitHub's update commit on a head the deck updated from is decided and
    benched at the release's look, first in the bench line."""
    h = Harness(tmp_path)

    async def go():
        await h.checked()
        assert await h.service.updating(PULL, HEAD)
        h.live = HEAD2
        assert h.service.head_moving(PULL, HEAD2)
        first = await h.service.look(PULL, HEAD2)
        await h.settle_runs()
        return first, await h.service.look(PULL, HEAD2)

    first, then = asyncio.run(go())
    assert first["verdict"] == "running" and then["verdict"] == lr.PASS
    assert h.benches == [(TIP, HEAD), (TIP, HEAD2)]
    assert h.entry(HEAD2)["decision"] == lr.LIGHT


@pytest.mark.parametrize(
    "fault", ["committer", "parent", "second parent off the base", "not noted"]
)
def test_any_other_new_head_waits_for_its_agent(tmp_path, fault):
    h = Harness(tmp_path)
    if fault == "committer":
        h.committer = "Robot <robot@example.com>"
    elif fault == "parent":
        h.parents = {HEAD2: [BASE, TIP]}
    elif fault == "second parent off the base":
        h.left = "3"

    async def go():
        await h.checked()
        if fault != "not noted":
            await h.service.updating(PULL, HEAD)
        h.live = HEAD2
        first = await h.service.look(PULL, HEAD2)
        again = await h.service.look(PULL, HEAD2)
        return first, again

    first, again = asyncio.run(go())
    assert first["verdict"] == again["verdict"] == "agent"
    assert h.benches == [(TIP, HEAD)] and HEAD2 not in h.db.light_review_row(PULL)["heads"]
    assert h.keys()[-1] == f"light-review:{PULL}@{HEAD2}:agent"
    assert h.keys().count(f"light-review:{PULL}@{HEAD2}:agent") == 1


def test_the_release_takes_the_updated_record_as_proof_too(tmp_path):
    h = Harness(tmp_path)

    async def go():
        await h.checked()
        h.live = HEAD2
        return await h.service.look(PULL, HEAD2, updated=HEAD)

    assert asyncio.run(go())["verdict"] == "running"


def test_an_unread_new_head_is_unread_and_writes_nothing(tmp_path):
    h = Harness(tmp_path)

    async def go():
        await h.checked()
        await h.service.updating(PULL, HEAD)
        h.live = HEAD2
        h.fetch_fails = 1
        return await h.service.look(PULL, HEAD2)

    assert asyncio.run(go())["verdict"] == "unread"
    assert HEAD2 not in h.db.light_review_row(PULL)["heads"]


def test_the_merge_needs_the_pass_and_no_bench_running_and_counts(tmp_path):
    h = Harness(tmp_path, limit=1)

    async def go():
        before = await h.service.before_merge(WORK, PULL, HEAD)
        await h.checked()
        go_, why = await h.service.before_merge(WORK, PULL, HEAD)
        await h.service.merged(PULL)
        after = await h.service.before_merge(WORK, PULL, HEAD)
        return before, (go_, why), after

    before, ready, after = asyncio.run(go())
    assert before[0] is False
    assert ready == (True, "")
    assert after[0] is False and "review point" in after[1]
    assert h.filed == [("acme/issues", h.filed[0][1])]
    assert h.db.light_review_rows("acme/robot")[0][2] is True


def test_a_light_pass_waiting_when_another_reaches_the_limit_goes_to_full_review(tmp_path):
    """Two light pull requests with a pass and a limit of one: once the other merges, this
    one does not merge light, its agent is told once, and pre-review-done answers full while
    its light decision stays written."""
    h = Harness(tmp_path, limit=1)

    async def go():
        await h.checked()
        other = {"heads": {HEAD2: {"seen_at": 1.0, "decision": lr.LIGHT, "verdict": lr.PASS}}}
        h.db.put_light_review("acme/robot#6", "acme/robot", other, merged=True)
        looks = [await h.service.look(PULL, HEAD), await h.service.look(PULL, HEAD)]
        go_ = await h.service.before_merge(WORK, PULL, HEAD)
        return looks, go_, await h.service.checklist(PULL, HEAD, WORK)

    looks, go_, decision = asyncio.run(go())
    assert looks == [{"verdict": "limit", "cases": []}, None]  # then the reviewed path
    assert go_[0] is False
    assert [key for key in h.keys() if key.endswith(":limit")] == [
        f"light-review:{PULL}@{HEAD}:limit"
    ]
    assert not decision.light and "review point" in decision.rule
    assert h.entry()["decision"] == lr.LIGHT  # written once, left as it was
    line = lr.line_for(h.db.light_review_row(PULL), HEAD, False)
    assert line["light"] is False and "review point" in line["text"]
    assert not h.service.is_light(PULL, HEAD) and "review point" in h.service.full_why(PULL)
    assert asyncio.run(h.service.look(PULL, HEAD)) is None  # the reviewed path holds it now


def test_a_light_merge_stays_counted_after_a_review_asked_during_it(tmp_path):
    """The deck's own light merge is a fact kept on the row: a review asked while its merge
    call was in flight does not take it out of the review point's count."""
    h = Harness(tmp_path, limit=1)

    async def go():
        await h.checked()
        h.events.append(_asked())  # a review asked while the merge call ran
        await h.service.merged(PULL, HEAD)
        return await h.service._merged_light("acme/robot")

    assert asyncio.run(go()) == (1, [PULL])
    assert h.db.light_review_row(PULL)["merged_light"] == HEAD
    assert len(h.filed) == 1


def test_a_decision_at_the_limit_is_full_for_good(tmp_path):
    h = Harness(tmp_path, limit=0)
    decision = asyncio.run(h.checked())
    assert not decision.light and h.entry()["kind"] == lr.LIMIT
    assert "review point" in h.service.full_why(PULL)
    assert len(h.filed) == 1


def test_a_bench_cut_off_by_a_restart_could_not_run_with_its_retry(tmp_path):
    """Rule 1: a running marker of another boot is a bench that died."""
    h = Harness(tmp_path)
    asyncio.run(h.checked())
    record = h.db.light_review_row(PULL)
    record["heads"][HEAD2] = {
        "seen_at": 2000.0,
        "decision": lr.LIGHT,
        "running": {"boot": "another", "at": 1.0, "round": 2},
    }
    h.db.put_light_review(PULL, "acme/robot", record)
    asyncio.run(h.service.resume())
    entry = h.entry(HEAD2)
    assert "running" not in entry and entry["attempt"]["verdict"] == lr.UNRUN
    assert "restarted" in entry["attempt"]["reason"]
    assert [delay for delay, _fn in h.timers] == [lr.RETRY_S]
    assert h.keys()[-1] == f"light-review:{PULL}@{HEAD2}:unrun:first:r2"


def test_a_due_retry_is_armed_again_after_a_restart(tmp_path):
    h = Harness(tmp_path)
    h.bench_answers = [(2, "", "no bench")]
    asyncio.run(h.checked())
    h.timers.clear()
    fresh = Harness(tmp_path)
    asyncio.run(fresh.service.resume())
    assert len(fresh.timers) == 1


def test_the_first_in_line_benches_before_the_others_waiting(tmp_path):
    line = lr.BenchLine()
    order = []

    async def bench(name, priority, gate=None):
        async with line.turn(priority):
            order.append(name)
            if gate is not None:
                await gate.wait()

    async def go():
        gate = asyncio.Event()
        running = asyncio.create_task(bench("running", lr.NEXT, gate))
        await asyncio.sleep(0)
        later = [asyncio.create_task(bench(f"next{i}", lr.NEXT)) for i in range(2)]
        await asyncio.sleep(0)
        first = asyncio.create_task(bench("first", lr.FIRST))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(running, first, *later)

    asyncio.run(go())
    assert order == ["running", "first", "next0", "next1"]


def test_stale_ref_locks_are_cleared_in_the_shared_git_dir(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    locks = repo / ".git" / "refs" / "agentdeck" / "light" / "pull"
    locks.mkdir(parents=True)
    (locks / "5.lock").write_text("")
    (locks / "6").write_text("x")
    removed = asyncio.run(lr.clear_ref_locks(str(repo)))
    assert removed == [str(locks / "5.lock")] or [p.endswith("5.lock") for p in removed] == [True]
    assert not (locks / "5.lock").exists() and (locks / "6").exists()


def test_git_reads_reach_github_through_the_decks_helper(tmp_path, monkeypatch):
    """#4438: the runtime has no ``gh`` login, so a fetch over https failed with "unable to
    get password" and no light review could start. Git now gets the deck's own credential
    helper, as a worker's git does, and never a prompt."""
    seen = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def spawn(*args, **kwargs):
        seen.update(kwargs)
        return _Proc()

    monkeypatch.setenv("SSH_ASKPASS", "/usr/bin/ksshaskpass")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    assert asyncio.run(lr.git_read(str(tmp_path), ["ls-remote", "origin"])) == "ok"

    env = seen["env"]
    assert "SSH_ASKPASS" not in env and env["GIT_TERMINAL_PROMPT"] == "0"
    config = {
        env[f"GIT_CONFIG_KEY_{n}"]: env[f"GIT_CONFIG_VALUE_{n}"]
        for n in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert "agentdeck.git_credential" in config["credential.https://github.com.helper"]


def test_the_rows_round_trip_and_unread_raises(tmp_path):
    db = Db(tmp_path / "control.db")
    assert db.light_review_row(PULL) is None
    assert db.put_light_review(PULL, "acme/robot", {"a": 1})
    assert db.put_light_review(PULL, "acme/robot", {"a": 2}, merged=True)
    assert db.put_light_review(PULL, "acme/robot", {"a": 3})  # keeps merged
    assert db.light_review_rows("acme/robot") == [(PULL, {"a": 3}, True)]
    assert db.light_review_rows("acme/other") == []
    assert db.note_light_review_point("acme/robot", 15, "u")
    assert db.note_light_review_point("acme/robot", 15, "v")
    assert db.light_review_point("acme/robot", 15) == "u"


def test_two_review_point_filings_at_once_file_one_issue(tmp_path):
    h = Harness(tmp_path, limit=1)
    find = h.service.ports.find_issue

    async def slow_find(repository, title):
        await asyncio.sleep(0)
        return await find(repository, title)

    h.service.ports.find_issue = slow_find

    async def go():
        await asyncio.gather(
            h.service._file_point("acme/robot", 1, [PULL], WORK),
            h.service._file_point("acme/robot", 1, [PULL], WORK),
        )

    asyncio.run(go())
    assert len(h.filed) == 1
