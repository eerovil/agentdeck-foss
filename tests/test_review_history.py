"""One pull request head's review history and the answer read from it (#3957)."""

from __future__ import annotations

import itertools

import pytest

from agentdeck.db import Db
from agentdeck.review_history import (
    ACCEPTED,
    ASKED,
    CHANGES,
    CHAT,
    CHECKLIST,
    CHECKLIST_DONE,
    ESCALATED,
    FINDINGS,
    PASS,
    UNSTARTED,
    VERDICTS,
    HeadReview,
    ReviewEvent,
    answered,
    at_head,
    checklist_waits,
    head_review,
    last_digest,
    newest_verdict,
    passed,
    same_change,
)
from agentdeck.work_release import note_escalation

PR = "eerovil/agentdeck#9"
HEAD = "a" * 40
CHATS = "eerovil/review-chats"

SIX = (CHECKLIST, ASKED, CHAT, ESCALATED, ACCEPTED, PASS)
DATA = {CHAT: {"chat": f"{CHATS}#3"}, ESCALATED: {"url": "https://x/1"}, ACCEPTED: {"note": "ok"}}


def _event(kind: str, at: float, seq: int) -> ReviewEvent:
    return ReviewEvent(PR, HEAD, at, kind, f"{kind}-key", DATA.get(kind, {}), seq)


@pytest.mark.parametrize("arrival", ["in time order", "reversed", "rotated"])
def test_every_order_of_six_events_gives_one_answer_and_the_newest_verdict_decides(arrival):
    """All 720 time orders of six events at one head, each arriving out of its time order."""
    for order in itertools.permutations(SIX):
        dated = [(kind, float(at)) for at, kind in enumerate(order)]
        arrived = {
            "in time order": dated,
            "reversed": dated[::-1],
            "rotated": dated[3:] + dated[:3],
        }[arrival]
        events = [_event(kind, at, seq) for seq, (kind, at) in enumerate(arrived)]
        answer = head_review(events)
        assert answer == head_review(sorted(events, key=lambda event: event.at))
        # An escalation after the owner's acceptance with no ask between is that round's
        # late copy, and counts for nothing, unless the acceptance came with an ask still
        # unanswered: then it is that review's result (#4069).
        between = order[order.index(ACCEPTED) + 1 : order.index(ESCALATED)]
        before = order[: order.index(ACCEPTED)]
        out = ASKED in before and PASS not in before[before.index(ASKED) :]
        if order.index(ESCALATED) > order.index(ACCEPTED) and ASKED not in between and not out:
            order = tuple(kind for kind in order if kind != ESCALATED)
        newest = [kind for kind in order if kind in VERDICTS][-1]
        assert answer.verdict == (PASS if newest == ACCEPTED else newest)
        assert answer.accepted == (newest == ACCEPTED)
        # A chat after a pass is a sibling's review listing this head as passed (#3341, #3956
        # review): it starts a review only after an escalation, or when an ask follows it.
        after = order[order.index(newest) + 1 :]
        reviewing = ASKED in after or (CHAT in after and newest == ESCALATED)
        assert (answer.review_since is not None) == reviewing
        # The checklist waits until anything answering it: an ask, a verdict, or a review chat.
        since = order[order.index(CHECKLIST) + 1 :]
        counted_chat = CHAT in since and (CHAT in after and (newest == ESCALATED or ASKED in after))
        answered = ASKED in since or any(k in VERDICTS for k in since) or counted_chat
        assert (answer.checklist_since is not None) == (not answered)


def test_a_head_answers_its_checklist_review_findings_and_unstarted():
    def review(*kinds: str, data: dict | None = None) -> HeadReview:
        return head_review(
            ReviewEvent(PR, HEAD, 10.0 + n, kind, str(n), data or {}, n)
            for n, kind in enumerate(kinds)
        )

    assert review() == HeadReview()
    assert review(CHECKLIST).checklist_since == 10.0
    assert review(CHECKLIST, CHECKLIST_DONE).checklist_since is None
    waiting = review(CHECKLIST, ASKED, CHAT, data={"chat": f"{CHATS}#4"})
    assert waiting.checklist_since is None and waiting.review_since == 12.0
    assert waiting.review_chat == f"{CHATS}#4" and waiting.verdict == ""
    found = review(ASKED, FINDINGS)
    assert found.verdict == "changes" and found.findings_at == 11.0 and found.review_since is None
    assert review(ASKED, FINDINGS, PASS).findings_at is None
    assert review(UNSTARTED).unstarted and not review(UNSTARTED, CHECKLIST).unstarted
    # Two events at one moment: arrival decides.
    tied = [
        ReviewEvent(PR, HEAD, 5.0, kind, kind, {}, seq) for seq, kind in ((2, PASS), (1, "changes"))
    ]
    assert head_review(tied).verdict == PASS
    # A sibling's chat listing this passed head (#3341) is not a review of it; an ask is, and
    # takes that chat (#3956 review).
    chat = {"chat": f"{CHATS}#7"}
    background = [
        ReviewEvent(PR, HEAD, 1.0, PASS, "p", {}, 1),
        ReviewEvent(PR, HEAD, 2.0, CHAT, "c", chat, 2),
    ]
    assert head_review(background).verdict == PASS and head_review(background).review_since is None
    asked = head_review([*background, ReviewEvent(PR, HEAD, 3.0, ASKED, "a", {}, 3)])
    assert asked.review_since == 3.0 and asked.review_chat == f"{CHATS}#7"
    # One answer for whether the pass stands, which the release and the Work card both read.
    assert head_review(background).passed and not asked.passed


def test_the_router_reads_its_checklist_digest_carry_and_answer_from_the_history():
    """#3955: the Work router's records, across a pull request's heads."""
    old, new, other = "1" * 40, "2" * 40, "3" * 40

    def at(head: str, kind: str, n: int, **data) -> ReviewEvent:
        return ReviewEvent(PR, head, float(n), kind, f"{kind}{n}", data, n)

    # The hold is per pull request: a checklist at any head waits until a done or an ask.
    assert checklist_waits([at(old, CHECKLIST, 1), at(new, CHECKLIST, 2, covered=True)])
    assert not checklist_waits([at(old, CHECKLIST, 1), at(new, ASKED, 2)])
    assert not checklist_waits([at(old, CHECKLIST, 1), at(old, CHECKLIST_DONE, 2)])
    assert checklist_waits([at(old, ASKED, 1), at(new, CHECKLIST, 2), at(new, PASS, 3)])
    # By each event's own time, not arrival: an ask that arrives last but is older holds none.
    late = ReviewEvent(PR, old, 0.5, ASKED, "late", {}, 9)
    assert checklist_waits([at(old, CHECKLIST, 1), late])
    # The newest digest named, at any head; the carry followed back, short shas too.
    events = [
        at(old, ASKED, 1, digest="D" * 64),
        at(old, FINDINGS, 2),
        at(new, FINDINGS, 3, digest="e" * 64, carried_from=old[:12]),
        at(other, PASS, 4, carried_from=new),
    ]
    assert last_digest(events) == (new, "e" * 64) and last_digest([]) == ("", "")
    assert last_digest(events[::-1]) == (new, "e" * 64)
    assert same_change(events, other, old[:12]) and same_change(events, new, new)
    assert not same_change(events, old, new) and not same_change(events, other, "1" * 6)
    assert answered(events, new[:7]) and not answered([at(old, ASKED, 1)], old)
    # The chat board's copy of an escalation the owner already accepted on its comment, dated
    # later, keeps the pass; a new ask opens a round whose escalation counts (#4069).
    round_ = [at(new, ASKED, 1), at(new, ESCALATED, 2), at(new, ACCEPTED, 3), at(old, ASKED, 4)]
    copied = [*round_, at(new, ESCALATED, 5), at(old, FINDINGS, 6)]
    assert head_review(at_head(copied, new)).passed and passed(newest_verdict(at_head(copied, new)))
    assert newest_verdict(copied).kind == FINDINGS, "another head's findings still count"
    again = [*copied, at(new, ASKED, 7), at(new, ESCALATED, 8)]
    assert head_review(at_head(again, new)).verdict == ESCALATED
    # Accepted while a newer review was out: what that review finds still stands.
    newer = [at(new, ASKED, 1), at(new, ESCALATED, 2), at(new, ASKED, 3), at(new, ACCEPTED, 4)]
    assert head_review(newer).passed
    assert head_review([*newer, at(new, FINDINGS, 5)]).verdict == CHANGES
    assert head_review([*newer, at(new, ESCALATED, 5)]).verdict == ESCALATED


def test_the_table_keeps_each_event_once_and_forgets_a_closed_pull_request(tmp_path):
    db = Db(tmp_path / "deck.db")
    assert db.append_review_event("Eerovil/AgentDeck#9", HEAD.upper(), CHAT, "k1", 2.0, {"c": 1})
    assert not db.append_review_event(PR, HEAD, CHAT, "k1", 3.0)  # a replay adds nothing
    # ... but is stored, which is what the release asks before taking a verdict (#3956 review).
    assert db.has_review_event("k1") and not db.has_review_event("k9")
    assert db.append_review_event(PR, HEAD, PASS, "k2", 1.0)
    db.append_review_event("eerovil/agentdeck#10", HEAD, PASS, "k3", 1.0)
    rows = db.review_events(PR)
    assert [(row.kind, row.head, row.at, row.data) for row in rows] == [
        (CHAT, HEAD, 2.0, {"c": 1}),
        (PASS, HEAD, 1.0, {}),
    ]
    # The pass is older by its own time; the chat after it, with no ask, reviews nothing here.
    assert head_review(rows).verdict == PASS and head_review(rows).review_since is None
    # A head shipped with its findings left (#4212) is recorded once, outlives the history
    # its merge drops, and is found from its follow-up issue.
    assert db.claim_shipped(
        PR, HEAD, "1. off by one", "https://c/1", 4.0, "EerOvil/AgentDeck#7", "B" * 40
    )
    assert not db.claim_shipped(PR.upper(), HEAD, "again", "", 5.0)
    assert db.shipped_for("") == [] and db.shipped_for("eerovil/agentdeck#41") == []
    assert db.note_follow_up(PR, HEAD, "EerOvil/AgentDeck#41")
    # Unfinished (its pass not recorded): it did not ship, so neither the page nor the
    # follow-up's review reads it (#4212 review); the press's own resume still does.
    assert db.shipped_for("eerovil/agentdeck#41") == [] == db.shipped_by_work("eerovil/agentdeck#7")
    assert db.shipped(PR, HEAD).follow_up == "eerovil/agentdeck#41"
    # One rule the release holds a Ship now pass by and the page offers Ship now again by.
    from types import SimpleNamespace

    from agentdeck.state import AppState

    unfinished = AppState.ship_unfinished
    owner = SimpleNamespace(shipped=lambda pull, head: db.shipped(pull, head))
    assert unfinished(owner, PR.upper(), HEAD) and unfinished(owner, PR, "d" * 40)
    assert db.note_shipped(PR.upper(), HEAD, 4.5)
    assert not unfinished(owner, PR, HEAD.upper())
    db.note_shipped(PR, HEAD, 9.0)  # marked once: a later press keeps its time
    assert db.shipped(PR, HEAD).shipped == 4.5
    db.drop_review_events(PR)
    assert db.review_events(PR) == [] and len(db.review_events("eerovil/agentdeck#10")) == 1
    [shipped] = db.shipped_for("eerovil/agentdeck#41")
    assert (shipped.pull, shipped.head, shipped.findings, shipped.url) == (
        PR,
        HEAD,
        "1. off by one",
        "https://c/1",
    )
    assert db.shipped(PR, HEAD.upper()) == shipped and db.shipped(PR, "d" * 40) is None
    # Its base is kept from the press and its merge commit once the merge is heard (D7).
    assert shipped.base == "b" * 40 and shipped.merged == ""
    # Named by the pull request: the base merged in after the press is a new head (D7 review).
    assert db.note_shipped_merge(PR.upper(), "C" * 40)
    shipped = db.shipped(PR, HEAD)
    assert shipped.merged == "c" * 40
    # A pull request merges once: a later merge event does not rename it.
    db.note_shipped_merge(PR, "e" * 40)
    assert db.shipped(PR, HEAD).merged == "c" * 40
    # The confirm quotes the first line that says something (D10).
    from agentdeck.review_history import first_finding

    comment = "/claude\nReview findings for PR a/b#9 at abc\n\n1. `x.py:3` is off" + "x" * 200
    first = first_finding(comment)
    assert first.startswith("1. `x.py:3` is off") and len(first) == 160
    assert first_finding("") == "" == first_finding("<!-- marker -->\n")
    # The original Work's page reads it by its own issue (D12).
    assert db.shipped_by_work("eerovil/agentdeck#7") == [shipped] and db.shipped_by_work("") == []
    # D11: the follow-up #41's own pull request shipped with findings again, to #42. #42's
    # review reads the whole chain, newest first; #51 points back at #41, and the walk still
    # reads each issue once and ends.
    later = "eerovil/agentdeck#50"
    assert db.claim_shipped(later, "e" * 40, "2. still off", "", 6.0, "eerovil/agentdeck#41")
    db.note_follow_up(later, "e" * 40, "eerovil/agentdeck#42")
    db.note_shipped(later, "e" * 40, 6.5)
    assert db.claim_shipped("eerovil/agentdeck#51", "f" * 40, "", "", 7.0, "eerovil/agentdeck#42")
    db.note_follow_up("eerovil/agentdeck#51", "f" * 40, "eerovil/agentdeck#41")
    db.note_shipped("eerovil/agentdeck#51", "f" * 40, 7.5)
    reader = SimpleNamespace(shipped_for=db.shipped_for)
    chain = AppState.shipped_chain(reader, "EerOvil/AgentDeck#42")
    assert [(item.pull, item.work) for item in chain] == [
        ("eerovil/agentdeck#51", "eerovil/agentdeck#42"),
        (later, "eerovil/agentdeck#41"),
        (PR, "eerovil/agentdeck#7"),
    ]


def test_an_escalation_is_written_at_the_head_its_chat_reviewed_for_each_named_pull():
    heads = {3: {("eerovil/agentdeck", 9): HEAD, ("eerovil/site", 4): "b" * 40}}
    noted: list[tuple] = []

    def note(pull, head, kind, key, at=None, data=None):
        noted.append((pull, head, kind, data["chat"]))

    note_escalation(CHATS, heads, note, 3, ["agentdeck#9"], "https://x/2", "77", 5.0)
    assert noted == [(PR, HEAD, ESCALATED, f"{CHATS}#3")]
    note_escalation(CHATS, heads, note, 3, [], "", "78", 6.0)  # names none: all it covered
    assert [row[0] for row in noted[1:]] == [PR, "eerovil/site#4"]
    note_escalation(CHATS, heads, note, 99, [], "", "79", 7.0)  # a chat the deck never opened
    assert len(noted) == 3
