"""An outside reviewer's verdict, taken in and turned into the review release bit (#2108).

#2101 gave the poller a review bit, and only its own merge-review turn could write it. On a
repository whose reviews come from the ChatGPT reviewer instead, a card resting on a clean
external review just logged its bits and waited. These tests are about the intake that closes
that gap: `/reviewed pass <head> <digest>` on the card, authorized at Release tier because it
can
end in a merge with nobody in the loop, and bound to the content the reviewer actually read
rather than to whatever the pull request happens to hold when the poller gets round to it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, run_wake, store_for

from agentdeck.poller_engine import authorization as auth
from agentdeck.poller_engine import intake, release
from agentdeck.poller_engine.ports import EffectKind, LinkedPRs, PrState
from agentdeck.poller_engine.reconcile import CardSnapshot

_KEY = "store#5"
_PR = "store#7"
_HEAD = "a" * 40
# What `gh pr diff` hashes to — the reviewer states this, and it is what the pass binds to.
_DIFF = "d" * 64
_REACTION_REFUSED = "confused"


# --- fixtures ---

def resting(**extra) -> CardSnapshot:
    return CardSnapshot(status="In review", **extra)


def card(*, green: str | None = _HEAD) -> dict:
    """A resting card with a ready pull request, tested green, and no review recorded yet."""
    rec = {"status": "In review", "started": True, "generation": "gen1"}
    if green:
        release.record_tests_pass(rec, head=green, now=_NOW)
    return rec


def port_for(*, head=_HEAD, diffs=None, comments=(), merge_state="CLEAN", prs=None) -> FakePort:
    return FakePort(
        comments=list(comments),
        snapshots={_KEY: resting()},
        linked={_KEY: LinkedPRs(open={_PR: head})},
        pr_states=prs or {_PR: PrState(ref=_PR, head=head, merge_state=merge_state)},
        review_diffs={_PR: _DIFF} if diffs is None else diffs,
    )


def verdict_comment(body: str, *, author: str = "eero", cid: str = "c1") -> dict:
    return comment(cid, body, author=author)


def clean_verdict(*, head=_HEAD, digest=_DIFF, **kwargs) -> dict:
    return verdict_comment(f"/reviewed pass {head} {digest}", **kwargs)


def run(store, port, *, auto_release=True, **kwargs):
    return run_wake(store, port, now=_NOW, auto_release=auto_release, **kwargs)


def rec_of(store, key=_KEY) -> dict:
    return store.read()["issues"].get(key)


def comments_of(port) -> list[str]:
    return [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]


def reactions_of(port) -> list[str]:
    return [
        str((e.value or {}).get("reaction"))
        for e in port.applied
        if e.kind is EffectKind.REACT
    ]


# --- the grammar ---

def test_a_clean_verdict_names_the_commit_and_the_diff_it_read():
    parsed = intake.parse_control(f"/reviewed pass {_HEAD} {_DIFF}")
    assert parsed is not None
    assert parsed.kind is intake.CommandKind.REVIEWED
    assert parsed.verdict == "pass"
    assert parsed.head == _HEAD
    assert parsed.digest == _DIFF
    assert parsed.targets == ()


def test_a_verdict_may_name_the_pull_request_it_read():
    parsed = intake.parse_control(f"/reviewed changes-needed {_HEAD} org/store#7")
    assert parsed is not None
    assert parsed.verdict == "changes-needed"
    assert parsed.targets == ("store#7",)


def test_changes_needed_needs_no_digest_because_it_only_ever_clears():
    parsed = intake.parse_control(f"/reviewed changes-needed {_HEAD}")
    assert parsed is not None
    assert parsed.kind is intake.CommandKind.REVIEWED
    assert parsed.digest == ""


def test_no_code_states_its_digest_and_the_poller_reads_it_as_changes_needed():
    """#3322: only a Work the deck merges skips the review of its fix; a Poller card clears."""
    [deck] = intake.parse_verdicts(f"/reviewed no-code {_HEAD} {_DIFF} store#7")
    assert (deck.verdict, deck.digest, deck.targets) == ("no-code", _DIFF, ("store#7",))
    poller = intake.parse_control(f"/reviewed No-Code {_HEAD} {_DIFF}")
    assert poller.verdict == "changes-needed" and poller.digest == _DIFF
    assert not intake.parse_control(f"/reviewed no-code {_HEAD}").is_valid


@pytest.mark.parametrize(
    "body",
    [
        "/reviewed",                            # no verdict, no commits
        "/reviewed pass",                       # a verdict about nothing
        f"/reviewed pass {_HEAD}",              # a head, with no word on what was under it
        f"/reviewed pass {_HEAD} {'d' * 63}",   # not a sha256 of anything
        "/reviewed pass HEAD " + _DIFF,         # not a commit
        f"/reviewed looks-good {_HEAD} {_DIFF}",  # not a verdict this engine knows
        f"/reviewed pass {_HEAD} {_DIFF} store#",  # a malformed pull request reference
    ],
)
def test_a_verdict_that_does_not_say_what_it_read_is_refused_not_ignored(body):
    parsed = intake.parse_control(body)
    assert parsed is not None
    assert parsed.kind is intake.CommandKind.INVALID_REVIEWED
    assert not parsed.is_valid


# The ChatGPT reviewer's own comment, verbatim from agentdeck#2663 (#2673): its marker line
# trails the verdict, with or without a blank line before it.
_MARKED_HEAD = "3de788a3b4caeae329a9046235116a6b305886e9"
_MARKED_DIFF = "672227b6f745d55c4fdc0d2174260839bcdf7268ea54ba25bf0d86af54dda0da"
_MARKED_VERDICT = f"/reviewed pass {_MARKED_HEAD} {_MARKED_DIFF} eerovil/agentdeck#2670"


@pytest.mark.parametrize(
    "body",
    [
        f"{_MARKED_VERDICT}\n<!-- chatgpt-review -->",
        f"{_MARKED_VERDICT}\n\n<!-- chatgpt-review -->\n",
        f"{_MARKED_VERDICT} <!-- chatgpt-review --><!-- another -->\n<!--\nspans lines\n-->\n",
    ],
)
def test_a_verdict_followed_only_by_html_comments_is_taken(body):
    parsed = intake.parse_control(body)
    assert parsed == intake.Command(
        intake.CommandKind.REVIEWED,
        targets=("agentdeck#2670",),
        verdict="pass",
        head=_MARKED_HEAD,
        digest=_MARKED_DIFF,
    )


@pytest.mark.parametrize(
    "body",
    [
        f"{_MARKED_VERDICT}\nLooks good to me.\n<!-- chatgpt-review -->",
        f"{_MARKED_VERDICT}\n<!-- chatgpt-review -->\nLooks good to me.",
        f"{_MARKED_VERDICT}\n<!-- a --> visible <!-- b -->",
        f"{_MARKED_VERDICT}\n<!-- never closed",
        f"/reviewed pass {_MARKED_HEAD} <!-- chatgpt-review -->",
    ],
)
def test_anything_visible_after_the_verdict_is_still_refused(body):
    parsed = intake.parse_control(body)
    assert parsed is not None
    assert parsed.kind is intake.CommandKind.INVALID_REVIEWED
    assert intake.parse_verdicts(body) == (intake.Command(intake.CommandKind.INVALID_REVIEWED),)


def test_one_comment_may_carry_a_verdict_per_pull_request():
    """#3161: the reviewer sent both of a Work's verdicts in one comment."""
    other = f"/reviewed changes-needed {_HEAD} eerovil/other-repo#21"
    body = f"{_MARKED_VERDICT}\n\n{other}\n\n<!-- chatgpt-review -->"
    first, second = intake.parse_verdicts(body)
    assert first == intake.parse_control(_MARKED_VERDICT)
    assert (second.kind, second.verdict, second.targets) == (
        intake.CommandKind.REVIEWED,
        "changes-needed",
        ("other-repo#21",),
    )
    assert intake.parse_control(body).kind is intake.CommandKind.INVALID_REVIEWED  # the Poller's
    assert intake.parse_verdicts("/claude fix it") is None
    marked = f"{body}<!-- claude-bot -->"
    assert intake.parse_verdicts(marked, bot_marker="<!-- claude-bot -->") is None


def test_prose_about_a_review_is_not_a_verdict():
    assert intake.parse_control(f"I reviewed it, /reviewed pass {_HEAD} {_DIFF}") is None


def test_the_verdict_needs_the_authority_to_merge():
    assert auth.REQUIRED_TIERS[intake.CommandKind.REVIEWED] is auth.Tier.RELEASE


# --- what the pass is bound to ---

def test_a_pass_is_kept_with_the_diff_it_covered():
    rec: dict = {}
    assert release.record_reviewed_ref(
        rec, ref=_PR, head=_HEAD, open_refs={_PR}, diff=_DIFF, by="eero", now=_NOW
    )
    snapshot = release.review_pass(rec)
    assert snapshot is not None
    assert snapshot["heads"] == {_PR: _HEAD}
    assert snapshot["diffs"] == {_PR: _DIFF}
    assert snapshot["sources"][_PR]["by"] == "eero"


def test_a_pass_with_no_readable_content_is_not_recorded():
    rec: dict = {}
    assert not release.record_reviewed_ref(
        rec, ref=_PR, head=_HEAD, open_refs={_PR}, diff=None, tree=None, now=_NOW
    )
    assert release.review_pass(rec) is None


def test_an_external_pass_carries_across_a_rebase_that_changed_no_diff():
    rec: dict = {}
    release.record_reviewed_ref(
        rec, ref=_PR, head=_HEAD, open_refs={_PR}, diff=_DIFF, now=_NOW
    )
    moved = "b" * 40
    # The engine's own rebase of this reference is what lets a moved head be judged by its
    # contributed diff (#2122): a digest says what a change contains, never where it went, so
    # a head somebody pushed to is judged strictly however familiar its digest looks.
    release.note_rebase_round(rec, {_PR: moved})
    assert release.review_pass_fresh(
        rec, open_refs={_PR}, heads={_PR: moved}, review_diffs={_PR: _DIFF},
        rebase_moved=release.rebase_round(rec),
    )
    assert not release.review_pass_fresh(
        rec, open_refs={_PR}, heads={_PR: moved}, review_diffs={_PR: "diff-2"},
        rebase_moved=release.rebase_round(rec),
    )
    # And without that provenance the same unchanged digest on a moved head is not enough —
    # nor is it on a *second* head the engine never produced.
    assert not release.review_pass_fresh(
        rec, open_refs={_PR}, heads={_PR: moved}, review_diffs={_PR: _DIFF}
    )
    assert not release.review_pass_fresh(
        rec, open_refs={_PR}, heads={_PR: "c" * 40}, review_diffs={_PR: _DIFF},
        rebase_moved=release.rebase_round(rec),
    )


def test_each_open_pull_request_needs_its_own_verdict():
    other = "store#8"
    rec: dict = {}
    release.record_reviewed_ref(
        rec, ref=_PR, head=_HEAD, open_refs={_PR, other}, diff=_DIFF, now=_NOW
    )
    assert not release.review_pass_fresh(
        rec, open_refs={_PR, other}, heads={_PR: _HEAD, other: "c" * 40},
        review_diffs={_PR: _DIFF, other: "diff-2"},
    )
    release.record_reviewed_ref(
        rec, ref=other, head="c" * 40, open_refs={_PR, other},
        diff="diff-2", now=_NOW,
    )
    assert release.review_pass_fresh(
        rec, open_refs={_PR, other}, heads={_PR: _HEAD, other: "c" * 40},
        review_diffs={_PR: _DIFF, other: "diff-2"},
    )


def test_a_closed_pull_request_does_not_hold_the_bit_red_against_its_successor():
    old, new = "store#7", "store#9"
    rec: dict = {}
    release.record_reviewed_ref(
        rec, ref=old, head=_HEAD, open_refs={old}, diff=_DIFF, now=_NOW
    )
    release.record_reviewed_ref(
        rec, ref=new, head="d" * 40, open_refs={new}, diff="diff-3", now=_NOW
    )
    assert release.review_pass(rec)["heads"] == {new: "d" * 40}


# --- one whole wake ---

def test_a_clean_external_verdict_turns_the_bit_green_and_releases(tmp_path):
    port = port_for(comments=[clean_verdict()])
    store = store_for(tmp_path, {"issues": {_KEY: card()}})

    run(store, port)

    rec = rec_of(store)
    assert release.review_pass(rec)["diffs"] == {_PR: _DIFF}
    assert rec["mode"] == "merge"
    assert rec["auto_release"] is True
    assert any("Releasing." in body for body in comments_of(port))


def test_a_verdict_without_the_authority_to_merge_releases_nothing(tmp_path):
    port = port_for(comments=[clean_verdict(author="drive-by")])
    port.permissions["drive-by"] = "write"  # below the Release bar, and not allowlisted
    store = store_for(tmp_path, {"issues": {_KEY: card()}})

    run(store, port)

    rec = rec_of(store)
    assert release.review_pass(rec) is None
    assert rec.get("mode") != "merge"
    assert _REACTION_REFUSED in reactions_of(port)


def test_a_verdict_whose_content_cannot_be_read_waits_for_a_readable_diff(tmp_path):
    """An unreadable diff confirms nothing, and refutes nothing either (#2772).

    Refusing it lost a verdict whose digest still matched hours later, so it waits, and is
    taken as soon as a wake can read the diff it names.
    """
    port = port_for(comments=[clean_verdict()], diffs={})
    store = store_for(tmp_path, {"issues": {_KEY: card()}})

    outcome = run(store, port)

    rec = rec_of(store)
    assert release.review_pass(rec) is None
    assert rec.get("mode") != "merge"
    assert store.read()["controls"]["c1"]["status"] == "new"
    assert comments_of(port) == []
    assert any("reviewed deferred" in line for line in outcome.log)

    port._review_diffs = {_PR: _DIFF}
    run_wake(store, port, now=_NOW + timedelta(minutes=5), auto_release=True)

    assert release.review_pass(rec_of(store))["diffs"] == {_PR: _DIFF}


def test_a_verdict_whose_content_stays_unreadable_is_refused_in_the_end(tmp_path):
    """A diff GitHub never hands over (too large, say) must not hold the verdict silently."""
    port = port_for(comments=[clean_verdict()], diffs={})
    store = store_for(tmp_path, {"issues": {_KEY: card()}})
    run(store, port)

    run_wake(store, port, now=_NOW + timedelta(hours=1), auto_release=True)

    rec = rec_of(store)
    assert release.review_pass(rec) is None
    assert store.read()["controls"]["c1"]["status"] == "rejected"
    assert any("could not read the diff" in body for body in comments_of(port))


def test_a_verdict_on_a_commit_no_open_pull_request_is_at_releases_nothing(tmp_path):
    port = port_for(comments=[clean_verdict(head="e" * 40)])
    store = store_for(tmp_path, {"issues": {_KEY: card()}})

    run(store, port)

    rec = rec_of(store)
    assert release.review_pass(rec) is None
    assert rec.get("mode") != "merge"
    assert any("names nothing I can check" in body for body in comments_of(port))


def test_a_diff_that_changed_since_the_review_releases_nothing(tmp_path):
    """The head is exactly what the reviewer read; the diff under it is not.

    This is the case a head-only verdict cannot catch. A pull request's effective diff is
    measured against its base, so the same head over a base that has moved is different
    content — and sampling the live diff for it would file that content as reviewed.
    """
    port = port_for(diffs={_PR: "9" * 64}, comments=[clean_verdict()])
    store = store_for(tmp_path, {"issues": {_KEY: card()}})

    run(store, port)

    rec = rec_of(store)
    assert release.review_pass(rec) is None
    assert rec.get("mode") != "merge"
    assert any("not the diff" in body for body in comments_of(port))


def test_the_recorded_pass_is_the_reviewers_own_digest_never_a_fresh_sample(tmp_path):
    """The pull request moves after the check and before anything else reads it.

    Nothing is re-sampled between confirming the digest and storing it, so what lands on the
    card is the content the reviewer read. The freshness check is then free to notice, on a
    later wake, that the pull request has moved away from it — which is exactly what it is for.
    """
    port = port_for(comments=[clean_verdict()])
    store = store_for(tmp_path, {"issues": {_KEY: card()}})

    run(store, port)

    rec = rec_of(store)
    assert release.review_pass(rec)["diffs"] == {_PR: _DIFF}
    assert not release.review_pass_fresh(
        rec, open_refs={_PR}, heads={_PR: _HEAD}, review_diffs={_PR: "9" * 64}
    )


def test_changes_needed_clears_a_standing_pass(tmp_path):
    rec = card()
    release.record_reviewed_ref(
        rec, ref=_PR, head=_HEAD, open_refs={_PR}, diff=_DIFF, now=_NOW
    )
    port = port_for(comments=[verdict_comment(f"/reviewed changes-needed {_HEAD}")])
    store = store_for(tmp_path, {"issues": {_KEY: rec}})

    run(store, port)

    stored = rec_of(store)
    assert release.review_pass(stored) is None
    assert stored.get("mode") != "merge"


def test_a_verdict_alone_does_not_release_a_card_whose_suite_never_ran(tmp_path):
    port = port_for(comments=[clean_verdict()])
    store = store_for(tmp_path, {"issues": {_KEY: card(green=None)}})

    run(store, port)

    rec = rec_of(store)
    assert release.review_pass(rec)["heads"] == {_PR: _HEAD}
    assert rec.get("mode") != "merge"


def test_a_project_without_the_capability_records_the_pass_and_merges_nothing(tmp_path):
    port = port_for(comments=[clean_verdict()])
    store = store_for(tmp_path, {"issues": {_KEY: card()}})

    run(store, port, auto_release=False)

    rec = rec_of(store)
    assert release.review_pass(rec)["heads"] == {_PR: _HEAD}
    assert rec.get("mode") != "merge"


def test_changes_needed_takes_back_a_release_the_bits_already_armed(tmp_path):
    """Two wakes: the bits start a release, and the reviewer objects before it merges.

    Clearing the standing pass is not enough by itself — arming copies that pass into the
    active approval, and from there the release progression stops reading the bit. An
    objection that only deleted the bit would be acknowledged while the merge carried on.
    """
    store = store_for(tmp_path, {"issues": {_KEY: card()}})
    run(store, port_for(comments=[clean_verdict()]))
    assert rec_of(store)["mode"] == "merge"

    second = port_for(comments=[verdict_comment(f"/reviewed changes-needed {_HEAD}", cid="c2")])
    second.snapshots[_KEY] = CardSnapshot(status="In progress")
    run(store, second)

    rec = rec_of(store)
    assert rec.get("mode") is None
    assert rec.get("auto_release") is None
    assert rec.get("review_heads") is None
    assert release.review_pass(rec) is None
    assert second.armed == []
    assert any("Held back." in body for body in comments_of(second))
    assert any(
        e.kind is EffectKind.SET_STATUS and e.value == "In review" for e in second.applied
    )


def test_a_recall_that_could_not_land_is_retried_rather_than_acknowledged(tmp_path):
    """The objection is only spent once the release is actually off.

    `_handback` refuses for two very different reasons: the release is past recall, or an
    effect did not land this wake. Reading the second as the first acknowledged the reviewer
    while its release carried straight on merging.
    """
    store = store_for(tmp_path, {"issues": {_KEY: card()}})
    run(store, port_for(comments=[clean_verdict()]))
    assert rec_of(store)["mode"] == "merge"

    objection = verdict_comment(f"/reviewed changes-needed {_HEAD}", cid="c2")
    blocked = port_for(comments=[objection])
    blocked.snapshots[_KEY] = CardSnapshot(status="In progress")
    blocked.cancel[_PR] = False  # GitHub would not cancel the merge request
    run(store, blocked)

    held = rec_of(store)
    assert held["mode"] == "merge"  # still live, so the command must not be answered yet
    assert release.review_pass(held) is None
    assert _REACTION_DONE not in reactions_of(blocked)
    assert not any("Held back." in body for body in comments_of(blocked))
    # And the release is frozen meanwhile: arming here would make the retry too late by its
    # own rule, so the objection could never win however many wakes it got.
    assert release.objection(held)["state"] == release.OBJECTION_RECALL
    assert blocked.armed == []

    retry = port_for()  # no comments: the command is replayed from the ledger
    retry.snapshots[_KEY] = CardSnapshot(status="In progress")
    run(store, retry)

    rec = rec_of(store)
    assert rec.get("mode") is None
    assert rec.get("auto_release") is None
    assert retry.armed == []
    assert any("Held back." in body for body in comments_of(retry))
    assert release.objection(rec) is None  # settled, so nothing holds the card any more


def test_a_release_past_recall_is_answered_rather_than_retried_forever(tmp_path):
    """A merge already being watched may have landed, so say so and stop asking."""
    store = store_for(tmp_path, {"issues": {_KEY: card()}})
    run(store, port_for(comments=[clean_verdict()]))
    rec = rec_of(store)
    rec["awaiting_merge"] = {"prs": [_PR]}
    store_write(store, rec)

    port = port_for(comments=[verdict_comment(f"/reviewed changes-needed {_HEAD}", cid="c2")])
    port.snapshots[_KEY] = CardSnapshot(status="In progress")
    run(store, port)

    assert release.review_pass(rec_of(store)) is None
    assert any("too late to stop the release" in body for body in comments_of(port))
    assert _REACTION_DONE in reactions_of(port)


def test_a_too_late_warning_that_did_not_post_is_retried_rather_than_acknowledged(tmp_path):
    """Saying it is the whole of what a past-recall objection can do, so silence is not done.

    The warning is the only thing that sends a person to look at what shipped. A comment
    GitHub refused this wake is not the reviewer having been answered, so the command stays
    in the ledger and the next wake posts it.
    """
    store = store_for(tmp_path, {"issues": {_KEY: card()}})
    run(store, port_for(comments=[clean_verdict()]))
    rec = rec_of(store)
    rec["awaiting_merge"] = {"prs": [_PR]}
    store_write(store, rec)

    mute = port_for(comments=[verdict_comment(f"/reviewed changes-needed {_HEAD}", cid="c2")])
    mute.snapshots[_KEY] = CardSnapshot(status="In progress")
    mute.refuse.add(EffectKind.COMMENT)
    run(store, mute)

    assert not any("too late to stop the release" in body for body in comments_of(mute))
    assert _REACTION_DONE not in reactions_of(mute)
    # And the release is held meanwhile — not to stop a merge that already happened, but so
    # the card is still here to be told on the wake that finally manages to say it.
    assert release.objection(rec_of(store))["state"] == release.OBJECTION_NOTIFY

    retry = port_for()  # no comments: the command is replayed from the ledger
    retry.snapshots[_KEY] = CardSnapshot(status="In progress")
    run(store, retry)

    assert any("too late to stop the release" in body for body in comments_of(retry))
    assert _REACTION_DONE in reactions_of(retry)
    assert release.objection(rec_of(store)) is None


def test_a_too_late_warning_outlives_the_release_finishing_in_the_same_wake(tmp_path):
    """The card has to still exist on the wake that finally says it.

    A release that runs to the end closes the card and forgets its record, and a replayed
    command with no record to work from can only defer — so the objection would go unheard
    through the very wake that promised to retry it. Holding the release is what keeps the
    card alive long enough to be told.
    """
    store = store_for(tmp_path, {"issues": {_KEY: card()}})
    run(store, port_for(comments=[clean_verdict()]))
    rec = rec_of(store)
    rec["awaiting_merge"] = {"prs": [_PR], "premerged": {}, "since": _NOW.isoformat()}
    store_write(store, rec)

    merged = {_PR: PrState(ref=_PR, state="MERGED", merge_commit="sha1")}
    mute = port_for(
        comments=[verdict_comment(f"/reviewed changes-needed {_HEAD}", cid="c2")], prs=merged
    )
    mute.snapshots[_KEY] = CardSnapshot(status="In progress")
    mute.refuse.add(EffectKind.COMMENT)
    run(store, mute)

    held = rec_of(store)
    assert held is not None  # not finished and forgotten: there is still a card to tell
    assert release.objection(held)["state"] == release.OBJECTION_NOTIFY
    assert _REACTION_DONE not in reactions_of(mute)

    retry = port_for(prs=merged)  # no comments: the command is replayed from the ledger
    retry.snapshots[_KEY] = CardSnapshot(status="In progress")
    run(store, retry)

    assert any("too late to stop the release" in body for body in comments_of(retry))
    assert _REACTION_DONE in reactions_of(retry)


def store_write(store, rec) -> None:
    """Put one card record back, for a test that has to stage mid-release state."""
    with store.transaction() as state:
        state["issues"][_KEY] = rec


_REACTION_DONE = "+1"


# --- a reviewer that cannot merge anything needs no merge authority (#2375) ---

_REVIEWER_BOT = "review-bot[bot]"
_WORK_ONLY_BOT = auth.parse_policy(
    {"work": {"allow": ["eero", _REVIEWER_BOT]}, "release": {"allow": ["eero"]}}
)


def bot_verdict() -> dict:
    return comment(
        "c1", f"/reviewed pass {_HEAD} {_DIFF}", author=_REVIEWER_BOT, user_type="Bot",
        association="NONE",
    )


def test_reviewed_needs_release_only_where_a_pass_can_merge():
    reviewed = intake.CommandKind.REVIEWED.value
    assert auth.required_tier(reviewed) is auth.Tier.RELEASE
    assert auth.required_tier(reviewed, reviewed_merges=False) is auth.Tier.WORK
    # Only the verdict moves: a merge is a merge wherever it is typed.
    for kind in (intake.CommandKind.MERGE, intake.CommandKind.MERGE_REVIEW):
        assert auth.required_tier(kind.value, reviewed_merges=False) is auth.Tier.RELEASE


def test_a_work_tier_reviewer_pass_asks_for_the_verify_turn_where_nothing_merges(tmp_path):
    port = port_for(comments=[bot_verdict()])
    store = store_for(tmp_path, {"issues": {_KEY: card(green=None)}})

    run(store, port, auto_release=False, verify_turn=True, policy=_WORK_ONLY_BOT)

    rec = rec_of(store)
    assert release.review_pass(rec)["heads"] == {_PR: _HEAD}
    assert rec.get("mode") != "merge"
    controls = store.read().get("controls", {})
    assert any(cid.startswith("engine:release-bits:verify:") for cid in controls)


def test_the_same_reviewer_pass_is_refused_where_it_could_merge(tmp_path):
    port = port_for(comments=[bot_verdict()])
    store = store_for(tmp_path, {"issues": {_KEY: card()}})

    outcome = run(store, port, auto_release=True, policy=_WORK_ONLY_BOT)

    rec = rec_of(store)
    assert not release.review_pass(rec)
    assert rec.get("mode") != "merge"
    # Refused out loud, not with a reaction alone (#2581).
    assert store.read()["controls"]["c1"]["rejection"] == "unauthorized:reviewed"
    assert f"refused: {_KEY} /reviewed from {_REVIEWER_BOT} (not authorized)" in outcome.log


def test_a_reviewer_granted_release_passes_where_it_could_merge(tmp_path):
    # #2581: every eerovil project that merges on its own names the reviewer at Release.
    port = port_for(comments=[bot_verdict()])
    store = store_for(tmp_path, {"issues": {_KEY: card()}})
    granted = auth.parse_policy({"release": {"allow": ["eero", _REVIEWER_BOT]}})

    run(store, port, auto_release=True, policy=granted)

    assert release.review_pass(rec_of(store))["heads"] == {_PR: _HEAD}


# --- a verdict on the head the engine rebased away from (#2453) ---

_REBASED = "b" * 40


def _rebase_control(frm: str = _HEAD, ref: str = _PR) -> dict:
    cid = f"engine:release-bits:rebase:{_KEY}:{ref}@{frm}"
    return {
        "id": cid, "repo": "store", "number": 5, "kind": "claude", "payload": "rebase",
        "body": "", "url": "", "author": "engine:release-bits",
        "created_at": _NOW.isoformat(), "status": "done",
    }


def rebased_card(*, edges=((_HEAD, _REBASED),), ref: str = _PR) -> dict:
    """A card the engine rebased from ``_HEAD`` and whose rebase suite ran green on the result."""
    rec = card(green=edges[-1][1] if edges else _REBASED)
    for frm, to in edges:
        release.note_rebase_lineage(rec, ref=ref, source=frm, head=to, now=_NOW)
    return rec


def rebased_store(tmp_path, rec, *, controls=None):
    ledger = controls if controls is not None else [_rebase_control()]
    return store_for(
        tmp_path,
        {"issues": {_KEY: rec}, "controls": {c["id"]: c for c in ledger}},
    )


def late_verdict(*, head=_HEAD, target: str | None = "store#7") -> dict:
    return verdict_comment(
        f"/reviewed pass {head} {_DIFF}" + (f" org/{target}" if target else "")
    )


def test_a_pass_on_the_head_the_engine_rebased_away_from_carries_to_the_rebased_head(tmp_path):
    """The rebase's marker was already spent: only the lineage still says B came from A."""
    port = port_for(head=_REBASED, comments=[late_verdict()])
    store = rebased_store(tmp_path, rebased_card())

    run(store, port)

    rec = rec_of(store)
    assert release.review_pass(rec)["diffs"] == {_PR: _DIFF}
    assert _REACTION_DONE in reactions_of(port)
    assert not any("names nothing" in body for body in comments_of(port))
    # The ordinary carry-over moved it to the rebased head, and the card releases.
    assert release.review_pass(rec)["heads"] == {_PR: _REBASED}
    assert rec["mode"] == "merge"


def test_a_pass_carries_across_two_engine_rebases(tmp_path):
    third = "c" * 40
    port = port_for(head=third, comments=[late_verdict()])
    store = rebased_store(tmp_path, rebased_card(edges=((_HEAD, _REBASED), (_REBASED, third))))

    run(store, port)

    assert release.review_pass(rec_of(store))["heads"] == {_PR: third}


def test_a_push_after_the_engine_rebase_loses_the_verdict_quietly(tmp_path):
    pushed = "c" * 40
    port = port_for(head=pushed, comments=[late_verdict()])
    store = rebased_store(tmp_path, rebased_card())

    run(store, port)

    rec = rec_of(store)
    assert release.review_pass(rec) is None
    assert rec.get("mode") != "merge"
    assert _REACTION_REFUSED in reactions_of(port)
    assert comments_of(port) == []


def test_a_head_moved_by_somebody_else_is_refused_without_a_comment(tmp_path):
    """No rebase was ever asked from that head, so the move was not the engine's."""
    port = port_for(head=_REBASED, comments=[late_verdict()])
    store = rebased_store(tmp_path, rebased_card(), controls=[])

    run(store, port)

    assert release.review_pass(rec_of(store)) is None
    assert _REACTION_REFUSED in reactions_of(port)
    assert comments_of(port) == []


def test_a_rebase_that_changed_the_diff_refuses_the_old_verdict_quietly(tmp_path):
    port = port_for(head=_REBASED, diffs={_PR: "9" * 64}, comments=[late_verdict()])
    store = rebased_store(tmp_path, rebased_card())

    run(store, port)

    assert release.review_pass(rec_of(store)) is None
    assert _REACTION_REFUSED in reactions_of(port)
    assert comments_of(port) == []


def test_an_abbreviated_head_does_not_take_the_rebase_path(tmp_path):
    port = port_for(head=_REBASED, comments=[late_verdict(head=_HEAD[:12])])
    store = rebased_store(tmp_path, rebased_card())

    run(store, port)

    assert release.review_pass(rec_of(store)) is None


def test_a_verdict_naming_no_open_pull_request_still_says_why(tmp_path):
    port = port_for(head=_REBASED, comments=[late_verdict(target="store#99")])
    store = rebased_store(tmp_path, rebased_card())

    run(store, port)

    assert release.review_pass(rec_of(store)) is None
    assert any("names nothing I can check" in body for body in comments_of(port))


def test_a_rebase_of_one_pull_request_vouches_for_nothing_on_another(tmp_path):
    other = "store#8"
    other_head = "f" * 40
    port = FakePort(
        comments=[late_verdict(target=other)],
        snapshots={_KEY: resting()},
        linked={_KEY: LinkedPRs(open={_PR: _REBASED, other: other_head})},
        pr_states={
            _PR: PrState(ref=_PR, head=_REBASED, merge_state="CLEAN"),
            other: PrState(ref=other, head=other_head, merge_state="CLEAN"),
        },
        review_diffs={_PR: _DIFF, other: _DIFF},
    )
    store = rebased_store(tmp_path, rebased_card())

    run(store, port)

    assert release.review_pass(rec_of(store)) is None
    assert comments_of(port) == []   # a moved head on the named pull request: quiet


def test_lineage_only_follows_the_engines_own_hops():
    rec: dict = {}
    b, c, d = "b" * 40, "c" * 40, "d" * 40
    release.note_rebase_lineage(rec, ref=_PR, source=_HEAD, head=b, now=_NOW)
    release.note_rebase_lineage(rec, ref=_PR, source=c, head=d, now=_NOW)   # b -> c was a push
    assert release.lineage_reaches(rec, ref=_PR, start=_HEAD, end=b)
    assert not release.lineage_reaches(rec, ref=_PR, start=_HEAD, end=d)
    assert not release.lineage_reaches(rec, ref="store#8", start=_HEAD, end=b)
    assert not release.lineage_reaches(rec, ref=_PR, start=_HEAD, end=_HEAD)
    release.note_rebase_lineage(rec, ref=_PR, source=b, head=_HEAD, now=_NOW)  # a cycle
    assert not release.lineage_reaches(rec, ref=_PR, start=_HEAD, end="e" * 40)
    release.retire_rebase_lineage(rec)
    assert release.rebase_lineage(rec) == []


def test_lineage_is_bounded_and_ignores_empty_or_unmoved_hops():
    rec: dict = {}
    release.note_rebase_lineage(rec, ref=_PR, source="", head="b" * 40, now=_NOW)
    release.note_rebase_lineage(rec, ref=_PR, source=_HEAD, head=_HEAD, now=_NOW)
    assert release.rebase_lineage(rec) == []
    for n in range(12):
        release.note_rebase_lineage(
            rec, ref=_PR, source=f"{n:040x}", head=f"{n + 1:040x}", now=_NOW
        )
    assert len(release.rebase_lineage(rec)) == 8
