"""Which chats the web process keeps live (#2841)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentdeck.live_window import ON_DEMAND_S, LiveWindow
from agentdeck.models import IssueRef, Session, SessionStatus
from agentdeck.state import AppState, LetGoSessionChanged


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000_000.0

    def __call__(self) -> float:
        return self.now


def _session(key: str, **fields) -> Session:
    values = dict(
        key=key,
        account_key=key.rsplit(":", 1)[0],
        session_id=key.rsplit(":", 1)[1],
        status=SessionStatus.IDLE,
    )
    values.update(fields)
    return Session(**values)


def test_a_switched_off_window_keeps_everything():
    window = LiveWindow(0)
    assert not window.enabled
    assert window.keeps("codex:a:old", 0.0)
    window.set_works(pinned=(), finished={"codex:a:done"})
    assert not window.drops(_session("codex:a:done"))


def test_age_pins_and_opens_decide_what_an_owned_chat_keeps():
    clock = _Clock()
    window = LiveWindow(7, clock=clock)
    eight_days_ago = clock.now - 8 * 86400
    assert window.keeps("codex:a:new", clock.now - 3600)
    assert not window.keeps("codex:a:old", eight_days_ago)
    assert window.keeps("codex:a:starting", None), "no transcript yet: a worker starting"
    window.set_works(pinned={"codex:a:old"}, finished=())
    assert window.keeps("codex:a:old", eight_days_ago)
    window.set_works(pinned=(), finished=())
    window.request(["codex:a:old"])
    assert window.keeps("codex:a:old", eight_days_ago)
    clock.now += ON_DEMAND_S + 1
    assert not window.keeps("codex:a:old", eight_days_ago), "an open lasts an hour"


def test_a_finished_works_chat_is_let_go_unless_it_is_in_use():
    clock = _Clock()
    window = LiveWindow(7, clock=clock)
    window.set_works(pinned={"codex:a:both"}, finished={"codex:a:done", "codex:a:both"})
    assert window.drops(_session("codex:a:done"))
    assert not window.drops(_session("codex:a:both")), "another Work still needs it"
    assert not window.drops(_session("codex:a:done", status=SessionStatus.LIVE))
    assert not window.drops(_session("codex:a:done", thinking=True))
    assert not window.drops(_session("codex:a:done", question="Which one?"))
    window.request(["codex:a:done"])
    assert not window.drops(_session("codex:a:done")), "just opened"


def test_a_scan_lets_go_of_a_finished_works_chat_and_its_new_activity_brings_it_back():
    state = AppState()
    state.live_window = LiveWindow(7)
    state.live_window.set_works(pinned=(), finished={"codex:a:done"})
    state.replace_account_sessions(
        "codex:a", [_session("codex:a:done"), _session("codex:a:other")]
    )
    assert set(state.sessions) == {"codex:a:other"}

    with pytest.raises(LetGoSessionChanged):
        state.merge_account_sessions(
            "codex:a", [_session("codex:a:done", status=SessionStatus.LIVE)]
        )
    state.replace_account_sessions(
        "codex:a",
        [_session("codex:a:done", status=SessionStatus.LIVE), _session("codex:a:other")],
    )
    assert set(state.sessions) == {"codex:a:done", "codex:a:other"}


def test_a_held_chat_brings_its_older_legs_and_its_relatives(tmp_path):
    """#2841 review: a chat that started on another account before the window keeps
    that leg, so its transcript stays whole; a held chat's parent and children come
    along too. The legs and relatives no scan has listed yet are named for a rescan."""
    from agentdeck.db import Db

    db = Db(tmp_path / "deck.db")
    state = AppState(db=db)
    state.live_window = LiveWindow(7)
    old_leg, new_leg = "claude_code:main:old-leg", "codex:alt:new-leg"
    db.ensure_session_chats((old_leg, new_leg))
    db.append_chat_leg(old_leg, new_leg)
    recent = datetime.now(UTC) - timedelta(hours=1)
    state.replace_account_sessions("codex:alt", [_session(new_leg, last_activity=recent)])
    state.recorded_delegation_parents = {"codex:alt:child": new_leg}
    state.delegation_parents = {new_leg: "claude_code:main:parent"}

    missing = state.publish_live_companions()

    assert {old_leg, "codex:alt:child", "claude_code:main:parent"} <= missing
    window = state.live_window
    eight_days_ago = window.cutoff() - 86400
    assert window.keeps(old_leg, eight_days_ago)
    assert window.keeps("codex:alt:child", eight_days_ago)
    assert not window.keeps("claude_code:main:stranger", eight_days_ago)
    assert state.publish_live_companions() == frozenset(), "named once, not every scan"
    db.close()


def test_a_held_chat_still_ages_out_and_an_open_still_ends(tmp_path):
    """#2860 review: holding a chat must not keep it. A chat that ages past the window
    between two full scans, and an opened chat whose hour is over, are let go."""
    from agentdeck.db import Db

    clock = _Clock()
    db = Db(tmp_path / "deck.db")
    state = AppState(db=db)
    state.live_window = LiveWindow(7, clock=clock)
    window = state.live_window
    key, opened = "codex:alt:ageing", "codex:alt:opened"
    db.ensure_session_chats((key, opened))
    six_days_ago = datetime.fromtimestamp(clock.now - 6 * 86400, UTC)
    eight_days_ago = datetime.fromtimestamp(clock.now - 8 * 86400, UTC)
    window.request([opened])
    state.replace_account_sessions(
        "codex:alt",
        [
            _session(key, last_activity=six_days_ago),
            _session(opened, last_activity=eight_days_ago),
        ],
    )
    state.publish_live_companions()
    assert window.keeps(key, six_days_ago.timestamp())
    assert window.keeps(opened, eight_days_ago.timestamp())

    clock.now += 2 * 86400 + ON_DEMAND_S
    state.publish_live_companions()

    assert key not in window.companions and opened not in window.companions
    assert not window.keeps(key, six_days_ago.timestamp()), "aged past the window"
    assert not window.keeps(opened, eight_days_ago.timestamp()), "its hour is over"
    db.close()


def test_opened_survives_being_written_while_it_reads():
    """#2860 review: ``opened`` rebuilds the map instead of deleting from it in place."""
    clock = _Clock()
    window = LiveWindow(7, clock=clock)
    window.request(["a", "b"])
    clock.now += ON_DEMAND_S + 1
    window.request(["c"])
    assert window.opened() == {"c"}
    assert window.extra_keys() == {"c"}


def test_a_loaded_relative_stays_a_companion_on_the_next_scan(tmp_path):
    """#2860 review: an old leg loaded for its recent chat must stay kept, not become
    a root of its own, drop out of the companions and be let go on the next scan."""
    from agentdeck.db import Db

    db = Db(tmp_path / "deck.db")
    state = AppState(db=db)
    state.live_window = LiveWindow(7)
    old_leg, new_leg = "claude_code:main:old-leg", "codex:alt:new-leg"
    db.ensure_session_chats((old_leg, new_leg))
    db.append_chat_leg(old_leg, new_leg)
    recent = datetime.now(UTC) - timedelta(hours=1)
    long_ago = datetime.now(UTC) - timedelta(days=10)
    state.replace_account_sessions("codex:alt", [_session(new_leg, last_activity=recent)])
    assert state.publish_live_companions() == {old_leg}

    # The old leg is loaded by its account's rescan, then the scans go on.
    state.replace_account_sessions(
        "claude_code:main", [_session(old_leg, last_activity=long_ago)]
    )
    for _ in range(3):
        assert state.publish_live_companions() == frozenset()
        assert old_leg in state.live_window.companions
        assert state.live_window.keeps(old_leg, long_ago.timestamp())
    db.close()


def test_a_one_chat_load_adds_it_beside_the_accounts_other_chats():
    """#2841 review: opening one old chat admits just that chat."""
    state = AppState()
    state.replace_account_sessions(
        "codex:a", [_session("codex:a:one"), _session("codex:a:two")]
    )
    state.add_account_sessions("codex:a", [_session("codex:a:old")])
    assert set(state.sessions) == {"codex:a:one", "codex:a:two", "codex:a:old"}


def test_a_roots_whole_delegation_family_is_kept_however_deep(tmp_path):
    """#2860 review: a recent chat's delegated grandchild, and the old legs of chats in
    between, come along; a stranger does not."""
    from agentdeck.db import Db

    db = Db(tmp_path / "deck.db")
    state = AppState(db=db)
    state.live_window = LiveWindow(7)
    root, child, grandchild = "codex:a:root", "codex:a:child", "claude_code:b:grandchild"
    child_old_leg = "claude_code:b:child-old"
    db.ensure_session_chats((root, child, grandchild, child_old_leg))
    db.append_chat_leg(child_old_leg, child)
    recent = datetime.now(UTC) - timedelta(hours=1)
    state.replace_account_sessions("codex:a", [_session(root, last_activity=recent)])
    state.recorded_delegation_parents = {child: root, grandchild: child}
    state.delegation_parents = {"codex:a:stranger": "codex:a:elsewhere"}

    missing = state.publish_live_companions()

    assert {child, grandchild, child_old_leg} <= missing
    assert "codex:a:stranger" not in state.live_window.companions
    assert root not in state.live_window.companions, "a root is judged on its own"
    db.close()


def test_a_closed_work_still_waiting_for_its_finish_keeps_its_chat():
    """#2990: an issue closed but not yet finished still needs its chat on the Work page;
    #3125: so does a finished one whose pull request is still open."""
    from types import SimpleNamespace

    from agentdeck.web.work_view import _publish_live_works

    window = LiveWindow(7)
    state = SimpleNamespace(
        live_window=window,
        session_issues={
            "codex:a:waiting": [IssueRef("o/r", 1)],
            "codex:a:done": [IssueRef("o/r", 2)],
            "codex:a:going": [IssueRef("o/r", 3)],
            "codex:a:open-pull": [IssueRef("o/r", 4)],
        },
        sessions={
            "codex:a:waiting": None, "codex:a:done": None, "codex:a:going": None,
            "codex:a:open-pull": None,
        },
        chat_session_keys=lambda key, chats: (key,),
    )
    snapshot = SimpleNamespace(
        state=state, chats=None, projection=SimpleNamespace(works=()), legs={}
    )
    closed = datetime(2026, 10, 13, tzinfo=UTC)
    facts = {
        "github_issue:o/r#1": SimpleNamespace(finished_at=closed, awaiting_finish=True),
        "github_issue:o/r#2": SimpleNamespace(
            finished_at=closed, awaiting_finish=False,
            outputs=(SimpleNamespace(settled=True),),
        ),
        "github_issue:o/r#3": SimpleNamespace(finished_at=None, awaiting_finish=False),
        # #3125: finished by a person with its pull request still open and in review.
        "github_issue:o/r#4": SimpleNamespace(
            finished_at=closed, awaiting_finish=False,
            outputs=(SimpleNamespace(settled=True), SimpleNamespace(settled=False)),
        ),
    }
    _publish_live_works(SimpleNamespace(state=SimpleNamespace()), snapshot, facts)
    assert window.pinned == {"codex:a:waiting", "codex:a:going", "codex:a:open-pull"}
    assert window.finished == {"codex:a:done"}
    assert not window.drops(_session("codex:a:waiting"))
