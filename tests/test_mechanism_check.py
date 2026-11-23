"""The mechanism check: a third round of findings citing one file asks the owner (#4498)."""

from __future__ import annotations

from agentdeck.mechanism_check import (
    Target,
    asks,
    cited,
    findings_at,
    minor_at,
    repeat,
    same_file,
)
from agentdeck.review_history import ReviewEvent

PULL = "owner/repo#9"
A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)


def row(head, text, kind="findings", seq=1, **data):
    return ReviewEvent(PULL, head, float(seq), kind, f"{kind}:{seq}", {"text": text, **data}, seq)


def test_only_code_files_count_by_name():
    text = (
        "src/x/gate.py:12 races; gate.py again; tests/test_gate.py, src/x/tests/helper.py,"
        " docs/a.md, README.md, test_other.py, web/static/list.js:3 and web/app.tsx"
    )
    assert cited(text) == ["src/x/gate.py", "gate.py", "web/static/list.js", "web/app.tsx"]
    assert cited("data.jsonl, mod.pyc") == []  # a longer extension is not a shorter one
    # Every watched repository's source counts, and its tests do not (#4508 review).
    java = (
        "src/main/java/x/Gate.java:7, Gate.kt, src/test/java/x/GateTest.java, a_test.go, b.spec.ts"
    )
    assert cited(java) == ["src/main/java/x/Gate.java", "Gate.kt"]
    others = ["lib/x.php", "src/y.cpp", "z.rs", "w.go"]
    assert cited("lib/x.php, src/y.cpp:3, z.rs and w.go") == others
    assert cited("call review_history.full_head and self.history") == []
    assert same_file("gate.py", "src/x/gate.py") and not same_file("a/gate.py", "b/gate.py")


def test_a_third_round_at_a_third_head_citing_a_file_fires():
    history = [row(A, "gate.py:1 races", seq=1), row(B, "src/gate.py:9 too", seq=2)]
    assert repeat(history, "gate.py again", C) == ("src/gate.py", (A[:12], B[:12], C[:12]), 3)
    # Rounds at the head handed on now, minor and no-code ones, and textless ones do not count.
    for other in (
        row(C, "gate.py", seq=2),
        row(B, "gate.py", seq=2, minor=True),
        row(B, "gate.py", kind="no_code", seq=2),
        row(B, "", kind="changes", seq=2),
        row(B, "router.py", seq=2),
    ):
        assert repeat([history[0], other], "gate.py again", C) is None
    assert repeat(history, "tests/test_gate.py only", C) is None
    java = [row(A, "src/main/java/x/Gate.java:3", seq=1), row(B, "Gate.java:9", seq=2)]
    assert repeat(java, "x/Gate.java:12 again", C) == (
        "src/main/java/x/Gate.java",
        (A[:12], B[:12], C[:12]),
        3,
    )
    # Another file of the same name is not this one, and a round a base merge carried to a
    # later head is still one round.
    assert repeat(history, "lib/gate.py again", C) is None
    carried = row(B, "gate.py:1 races", seq=2, carried_from=A)
    assert repeat([history[0], carried], "gate.py again", C) is None
    # A minor round is no round (#4596): its findings row at a head holding a ``no_code`` row,
    # or a ``changes`` row marked with the reviewer's ``minor`` or ``no-code`` word.
    for verdict in (
        row(B, "", kind="no_code", seq=3),
        row(B, "", kind="changes", seq=3, asked="minor"),
    ):
        assert minor_at([verdict], B[:12]) and repeat([*history, verdict], "gate.py", C) is None
    plain = row(B, "", kind="changes", seq=3)
    assert not minor_at([plain], B) and repeat([*history, plain], "gate.py", C) is not None
    # Rounds citing other files count toward this round's number, not toward the file.
    other = row(D, "router.py", seq=0)
    assert repeat([other, *history], "gate.py", C) == ("src/gate.py", (A[:12], B[:12], C[:12]), 4)


class Store:
    """Review histories of several pull requests, a stored key adding nothing."""

    def __init__(self, *events):
        self.events = list(events)

    def target(self, pull, head):
        def write(data):
            if not any(e.key == f"mechanism:{pull}" for e in self.events):
                seq = len(self.events) + 1
                self.events.append(
                    ReviewEvent(pull, head, float(seq), "mechanism", f"mechanism:{pull}", data, seq)
                )

        return Target(pull, head, lambda: [e for e in self.events if e.pull == pull], write)

    def rows(self):
        return [event for event in self.events if event.kind == "mechanism"]


def rounds(pull, path, *heads):
    return [
        ReviewEvent(pull, head, float(n), "findings", f"f:{pull}:{n}", {"text": path}, n)
        for n, head in enumerate(heads)
    ]


def test_the_claiming_delivery_asks_and_its_resend_again_but_no_other():
    store = Store(row(A, "gate.py", seq=1), row(B, "gate.py", kind="escalated", seq=2))
    first = asks([store.target(PULL, C)], "gate.py:4", "comment:1")
    assert first.startswith("Mechanism check: these review findings cite `gate.py`")
    assert f"{PULL} round 3, heads {A[:12]}, {B[:12]}, {C[:12]}" in first
    assert 'opens with "Mechanism check: `gate.py`"' in first and "at most four" in first
    assert asks([store.target(PULL, C)], "gate.py:4", "comment:1") == first
    assert asks([store.target(PULL, C)], "gate.py:4", "findings:r1") == ""
    assert asks([store.target(PULL, D)], "gate.py:4", "comment:2") == ""
    assert len(store.rows()) == 1


def test_every_named_pull_request_is_checked_and_one_file_asked_once():
    """#4508 review: one delivery naming two pull requests at their third round asks about
    each, one question per file, and the two copies of a round splitting the claims still
    ask about a file once."""
    one, two, three = "o/r#1", "o/r#2", "o/r#3"
    history = [
        *rounds(one, "src/gate.py:1", A, B),
        *rounds(two, "gate.py:2", A, B),
        *rounds(three, "router.py:3", A, B),
    ]
    text = "gate.py:4 and router.py:5"
    store = Store(*history)
    asked = asks([store.target(p, C) for p in (one, two, three)], text, "comment:1")
    assert asked.count("Mechanism check: these") == 2  # gate.py once for both, router.py once
    gate, router = asked.split("\n\n")
    assert "`src/gate.py`" in gate and f"{one} round 3" in gate and f"{two} round 3" in gate
    assert "`router.py`" in router and three in router and one not in router
    assert {e.pull for e in store.rows()} == {one, two, three}
    assert asks([store.target(p, C) for p in (one, two, three)], text, "comment:1") == asked
    # The other copy of the round claims one pull request first, this one the other: the
    # file is asked about once, by the copy whose row came first, naming both.
    store = Store(*history)
    assert "`src/gate.py`" in asks([store.target(one, C)], text, "findings:r1")
    assert asks([store.target(one, C), store.target(two, C)], text, "comment:1") == ""
    assert {e.pull for e in store.rows()} == {one, two}
    again = asks([store.target(one, C), store.target(two, C)], text, "findings:r1")
    assert f"{one} round 3" in again and f"{two} round 3" in again
    # A pull request whose own history never cited the file gets no row.
    store = Store(*rounds(one, "gate.py", A, B), *rounds(two, "router.py", A, B))
    asks([store.target(one, C), store.target(two, C)], "gate.py", "comment:1")
    assert [e.pull for e in store.rows()] == [one]


def test_a_failing_read_asks_nothing():
    def broken():
        raise OSError("db")

    store = Store(*rounds("o/r#2", "gate.py", A, B))
    failing = Target(PULL, C, broken, lambda data: None)
    asked = asks([failing, store.target("o/r#2", C)], "gate.py", "comment:1")
    assert asked.count("Mechanism check: these") == 1 and PULL not in asked


def test_the_verdicts_read_back_finds_its_rounds_findings():
    """#4596: the ``changes-needed`` verdict's message asks with the findings read back from
    the newest findings row at its head."""
    earlier = (row(A, "gate.py", seq=1), row(B, "gate.py", seq=2))
    # The verdict's own read-back: the newest uncarried findings row at that head.
    history = [
        *earlier,
        row(C, "old.py", seq=3),
        row(C, "gate.py:4", seq=4),
        row(C, "carried.py", seq=5, carried_from=B),
        row(D, "other.py", seq=6),
    ]
    assert findings_at(history[:4], C) == "gate.py:4" and findings_at(history, "e" * 40) == ""
    # A base merge carried this round's findings to the standing head: still this round's.
    assert findings_at(history, C) == "carried.py"


def test_the_newest_verdict_at_a_head_says_whether_its_round_is_minor():
    """#4596 review: a ``changes-needed`` round asked again at the same head and answered
    ``minor`` is minor, and the other way round; a verdict a base merge carried from the head
    the router's heading names counts for it."""
    changes, minor = row(C, "", kind="changes", seq=3), row(C, "", kind="no_code", seq=4)
    assert minor_at([changes, minor], C)
    again = row(C, "", kind="changes", seq=5)
    assert not minor_at([changes, minor, again], C)
    carried = row(D, "", kind="no_code", seq=6, carried_from=C)
    assert minor_at([changes, carried], C) and not minor_at([changes], C)
    assert not minor_at([], C)
