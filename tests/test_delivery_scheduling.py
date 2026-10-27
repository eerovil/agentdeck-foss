"""The runtime chooses where a Poller delivery runs (#2146).

The Poller sends requirements — the Overlay's accounts in order, an avoid-account hint, usage
targets — and the runtime picks the account inside its capacity lock, freezes that choice per
``delivery_id``, and hands the delivery to that account's keyed-worker host.

The parity matrix pins what the Poller's own selector answered for each input before it was
removed (#2150): the move was characterized, not approximated, and the runtime must keep
answering the same. The rest follow one request through the runtime
app, with fake hosts standing in for the provider accounts.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck.config import AccountConfig, AppConfig
from agentdeck.db import Db, DeliveryRoute
from agentdeck.delivery_scheduling import (
    ALL_ACCOUNTS_HARD_INELIGIBLE,
    NO_CANDIDATE_ACCOUNTS,
    REQUIREMENTS_UNSATISFIED,
    RUNTIME_PROVIDER_UNAVAILABLE,
    DeliveryRequirements,
    Selector,
    request_fingerprint,
    target_of,
)
from agentdeck.ensure_delivery import ENSURE_DELIVERY_PATH, EnsureDeliveryRequest
from agentdeck.providers import account_health
from agentdeck.providers.delivery import DeliverResult
from agentdeck.runtime import create_runtime_app

pytestmark = pytest.mark.anyio

WORK = "github_issue:eerovil/agentdeck#2146"
OTHER_WORK = "github_issue:someone/agentdeck#2146"  # same short key, another repository
KEY = "agentdeck#2146"
MAIN, ALT = "claude_code:main", "claude_code:alt"
ACCEPTED = DeliverResult(True, "spawned", session_id="s1")


class FakeHost:
    """One provider account's keyed-worker host, reduced to what scheduling reads."""

    def __init__(self, result=ACCEPTED, *, workers=None, receipt=None, gate=None):
        self.result = result
        self.workers = dict(workers or {})
        self.receipt = receipt
        self.gate = gate
        self.active: set[str] = set()
        self.calls: list[str] = []

    async def deliver(self, key, message, **_kwargs):
        self.calls.append(key)
        if self.gate is not None:
            await self.gate.wait()
        if self.result.accepted:
            self.active.add(key)
        return self.result

    def snapshot(self):
        return {"workers": self.workers}

    def delivery_receipt(self, _key, _delivery_id):
        return self.receipt

    def active_role_keys(self, _role):
        return set(self.active)

    def delivery_is_finalized(self, _key, _delivery_id):
        return False

    def role_last_result_at(self, _key, _role):
        return 0.0


def _weekly(
    cache: Path,
    account: str,
    *,
    pct: float,
    resets_in: timedelta,
    five_hour: tuple[float, timedelta] | None = None,
) -> None:
    provider, _, label = account.partition(":")
    now = datetime.now(UTC)
    reading = {
        "account": account,
        "fetched_at": now.isoformat(),
        "seven_day_pct": pct,
        "seven_day_resets_at": (now + resets_in).isoformat(),
    }
    if five_hour is not None:
        reading["five_hour_pct"] = five_hour[0]
        reading["five_hour_resets_at"] = (now + five_hour[1]).isoformat()
    (cache / f"usage-{provider}-{label}.json").write_text(json.dumps(reading))


def _body(**fields) -> dict:
    return {
        "contract": "ensure-delivery-v1",
        "delivery_id": "poller:d1",
        "work_id": WORK,
        "run_id": "gen-1",
        "requirements": {"candidates": [MAIN, ALT]},
        "worker_key": KEY,
        "message": "go",
        "role": "kanban",
        "issue_ref": "eerovil/agentdeck#2146",
        **fields,
    }


def _route(db: Db, delivery_id: str, account: str, *, run_id: str = "gen-0") -> None:
    provider, label = target_of(account)
    assert db.freeze_delivery_route(
        DeliveryRoute(delivery_id, "f", WORK, run_id, provider, label, KEY, "2026-09-01T00:00:00")
    )


# --- parity: the runtime selects exactly as the Poller did -----------------------------------


FRESH_TURN = {"live": True, "turn_active": True}
STALE_TURN = {"live": True, "turn_active": True, "last_activity_at": 1.0}
EXPIRING_ALT = [(MAIN, 40.0, timedelta(days=5)), (ALT, 85.0, timedelta(hours=12))]

PARITY = {
    "operator order": dict(expect=MAIN),
    "hard circuit on the first": dict(expect=ALT, circuits=[MAIN]),
    "every account circuited": dict(expect=ALL_ACCOUNTS_HARD_INELIGIBLE, circuits=[MAIN, ALT]),
    "expiring budget leads": dict(expect=ALT, weekly=EXPIRING_ALT),
    "soft refusal passed over": dict(expect=ALT, weekly=[(MAIN, 100.0, timedelta(days=2))]),
    "every account refuses, earliest clears": dict(
        expect=ALT,
        weekly=[(MAIN, 100.0, timedelta(days=3)), (ALT, 100.0, timedelta(days=1))]
    ),
    "warm account first": dict(expect=ALT, warm=ALT),
    "expiring budget outranks warm": dict(expect=ALT, warm=MAIN, weekly=EXPIRING_ALT),
    "mid-turn pins the warm account": dict(
        expect=MAIN, warm=MAIN, worker=FRESH_TURN, weekly=EXPIRING_ALT
    ),
    "mid-turn pin withholds on a circuit": dict(
        expect=ALL_ACCOUNTS_HARD_INELIGIBLE, warm=MAIN, worker=FRESH_TURN, circuits=[MAIN]
    ),
    "a stale turn does not pin": dict(
        expect=ALT, warm=MAIN, worker=STALE_TURN, weekly=EXPIRING_ALT
    ),
    "a soft refusal cannot move a mid-turn card": dict(
        expect=MAIN,
        warm=MAIN,
        worker=FRESH_TURN,
        weekly=[(MAIN, 100.0, timedelta(days=3)), (ALT, 100.0, timedelta(days=1))],
    ),
    "avoid outranks warm": dict(expect=ALT, avoid=MAIN, warm=MAIN),
    "avoid with no alternative": dict(expect=MAIN, accounts=[MAIN], avoid=MAIN, circuits=[]),
    "avoid with no alternative skips affinity": dict(
        expect=MAIN,
        accounts=[MAIN], avoid=MAIN, warm=MAIN, worker=FRESH_TURN
    ),
    "no accounts at all": dict(expect=NO_CANDIDATE_ACCOUNTS, accounts=[]),
    "codex first in the operator order": dict(expect="codex:work", accounts=["codex:work", MAIN]),
}


@pytest.mark.parametrize("case", list(PARITY), ids=list(PARITY))
def test_the_runtime_selects_exactly_as_the_poller_did(tmp_path, case):
    """Each ``expect`` is what ``poller_wake.legacy_account_for`` answered for the same inputs,
    recorded by running both side by side before that selector was removed (#2150)."""
    spec = PARITY[case]
    accounts = spec.get("accounts", [MAIN, ALT])
    warm, worker = spec.get("warm", ""), spec.get("worker")
    cache = tmp_path / "usage"
    cache.mkdir()
    for account in spec.get("circuits", []):
        assert account_health.trip_hard_quota(cache, account, reason="window exhausted")
    for account, pct, resets_in in spec.get("weekly", []):
        _weekly(cache, account, pct=pct, resets_in=resets_in)

    db = Db(tmp_path / "control.db", 30)
    if warm:
        _route(db, "poller:earlier", warm)
    hosts = {
        target_of(name): FakeHost(workers={KEY: worker} if name == warm and worker else {})
        for name in accounts
    }
    requirements = DeliveryRequirements(candidates=accounts, avoid_account=spec.get("avoid"))
    placement = Selector(hosts=hosts, db=db, usage_cache=cache).place(
        EnsureDeliveryRequest.model_validate(_body(requirements=requirements.model_dump())),
        requirements,
    )

    assert (placement.reason or placement.account) == spec["expect"]
    db.close()


def test_an_avoided_account_is_matched_however_either_side_spells_it(tmp_path):
    """#2200: the Poller now learns a refused account from the Run's outcome, which spells it
    ``provider:label``; an Overlay that wrote a bare label must still have it avoided."""
    db = Db(tmp_path / "control.db", 30)
    hosts = {target_of(name): FakeHost() for name in ("main", "alt")}
    requirements = DeliveryRequirements(candidates=["main", "alt"], avoid_account=MAIN)

    placement = Selector(hosts=hosts, db=db, usage_cache=None).place(
        EnsureDeliveryRequest.model_validate(_body(requirements=requirements.model_dump())),
        requirements,
    )

    assert placement.account == "alt"
    db.close()


# --- a new card starts where the most budget is left per hour (#2757) -----------------------

# The operator's reading on 2026-10-09: main 48% with five days to its reset, alt 11% with
# three and a half. Alt has 84 points over 84 hours, main 47 over 119.
UNEVEN = [(MAIN, 48.0, timedelta(hours=119)), (ALT, 11.0, timedelta(hours=84))]


def _place(
    tmp_path, *, weekly=(), accounts=(MAIN, ALT), warm="", targets=None, circuits=(), five=None
):
    cache = tmp_path / "usage"
    cache.mkdir(parents=True, exist_ok=True)
    for account in circuits:
        assert account_health.trip_hard_quota(cache, account, reason="window exhausted")
    for account, pct, resets_in in weekly:
        _weekly(cache, account, pct=pct, resets_in=resets_in, five_hour=(five or {}).get(account))
    db = Db(tmp_path / "control.db", 30)
    if warm:
        _route(db, "poller:earlier", warm)
    requirements = DeliveryRequirements(
        candidates=list(accounts), usage_targets=dict(targets or {})
    )
    hosts = {target_of(name): FakeHost() for name in accounts}
    placement = Selector(hosts=hosts, db=db, usage_cache=cache).place(
        EnsureDeliveryRequest.model_validate(_body(requirements=requirements.model_dump())),
        requirements,
    )
    db.close()
    return placement


def test_a_new_card_starts_on_the_account_with_the_most_budget_left_per_hour(tmp_path):
    placement = _place(tmp_path, weekly=UNEVEN)

    assert placement.account == ALT
    assert any("1.0 points/hour" in note and "0.4" in note for note in placement.notes)


def test_a_new_card_skips_an_account_whose_five_hour_window_is_nearly_full(tmp_path):
    """#3580: the budget pace sent every card to alt until its five-hour window stopped them."""
    hour = timedelta(hours=1)
    placement = _place(tmp_path / "full", weekly=UNEVEN, five={ALT: (92.0, hour)})
    assert placement.account == MAIN
    assert any("five-hour window is 92% full" in note for note in placement.notes)

    assert _place(tmp_path / "room", weekly=UNEVEN, five={ALT: (89.0, hour)}).account == ALT
    # A window already reset has refilled, and with no account having room the order stands.
    assert _place(tmp_path / "reset", weekly=UNEVEN, five={ALT: (95.0, -hour)}).account == ALT
    both = {ALT: (95.0, hour), MAIN: (91.0, hour)}
    assert _place(tmp_path / "both", weekly=UNEVEN, five=both).account == ALT
    # An account opted out with a target of 0 is not offered the card for having room.
    opted = _place(tmp_path / "opted", weekly=UNEVEN, targets={MAIN: 0}, five={ALT: (92.0, hour)})
    assert opted.account == ALT
    # The leader need not be the full one: an opted-out main ahead of a full alt (review, #3582).
    third = "claude_code:third"
    level = [(name, 95.0, timedelta(hours=100)) for name in (MAIN, ALT, third)]
    three = _place(
        tmp_path / "three",
        weekly=level,
        accounts=(MAIN, ALT, third),
        targets={MAIN: 0},
        five={ALT: (92.0, hour), third: (50.0, hour)},
    )
    assert three.account == third
    assert any("opted out" in note for note in three.notes)
    # A card that already ran keeps its account; moving it costs a cold start.
    warm = _place(tmp_path / "warm", weekly=UNEVEN, warm=ALT, five={ALT: (95.0, hour)})
    assert warm.account == ALT


def test_a_card_that_already_ran_keeps_its_account(tmp_path):
    """Moving a running card costs a cold start, so only the first delivery is ranked."""
    assert _place(tmp_path, weekly=UNEVEN, warm=MAIN).account == MAIN


def test_equal_paces_keep_the_operators_order(tmp_path):
    weekly = [(MAIN, 45.0, timedelta(hours=100)), (ALT, 45.0, timedelta(hours=100))]

    placement = _place(tmp_path, weekly=weekly)

    assert placement.account == MAIN
    assert not placement.notes

    # 0.45 against 0.5 points/hour: within the tie band, so still the operator's order
    close = [(MAIN, 50.0, timedelta(hours=100)), (ALT, 45.0, timedelta(hours=100))]
    assert _place(tmp_path / "close", weekly=close).account == MAIN


def test_an_account_past_its_target_or_opted_out_ranks_last(tmp_path):
    weekly = [(MAIN, 96.0, timedelta(hours=10)), (ALT, 50.0, timedelta(hours=150))]
    assert _place(tmp_path / "past", weekly=weekly).account == ALT

    assert _place(tmp_path / "zero", weekly=UNEVEN, targets={ALT: 0}).account == MAIN


def test_a_missing_reading_leaves_the_operators_order(tmp_path):
    """Ignorance must not move work, even towards the account that does report."""
    assert _place(tmp_path, weekly=UNEVEN[1:]).account == MAIN


def test_another_providers_empty_meter_never_jumps_the_queue(tmp_path):
    """Codex reads 0% whatever it has spent; it keeps its fallback slot."""
    codex = "codex:codex"
    weekly = [*UNEVEN, (codex, 0.0, timedelta(hours=160))]

    placement = _place(tmp_path, weekly=weekly, accounts=(MAIN, codex, ALT))

    assert placement.account == ALT


def test_a_full_account_is_still_passed_over_after_ranking(tmp_path):
    weekly = [(MAIN, 48.0, timedelta(hours=119)), (ALT, 100.0, timedelta(hours=84))]
    assert _place(tmp_path, weekly=weekly).account == MAIN
    assert _place(tmp_path / "circuit", weekly=UNEVEN, circuits=[ALT]).account == MAIN


def test_an_expiring_budget_still_has_the_final_say(tmp_path):
    """Main at 80% with ten hours left out-ranks alt's faster pace: that budget is about to go."""
    weekly = [(MAIN, 80.0, timedelta(hours=10)), (ALT, 0.0, timedelta(hours=60))]
    # paces: main 1.5/h, alt 1.6/h; only main is inside its last quarter, 15 points short
    assert _place(tmp_path, weekly=weekly).account == MAIN


def test_a_planned_reset_sends_its_account_work_first_until_then(tmp_path):
    """#3379: the person will reset alt's week in 15 hours, so alt is filled to 100% first."""
    weekly = [(MAIN, 22.0, timedelta(days=6)), (ALT, 25.0, timedelta(days=5))]
    assert _place(tmp_path / "none", weekly=weekly).account == MAIN

    # Kept in the deck's database, which the Selector reopens here as the runtime would after
    # a restart (a file in the usage folder was deleted by every runtime restart).
    now = datetime.now(UTC)
    for case in ("cold", "warm", "past", "full"):
        (tmp_path / case).mkdir()
        db = Db(tmp_path / case / "control.db", 30)
        at = now - timedelta(hours=1) if case == "past" else now + timedelta(hours=15)
        db.record_planned_reset(ALT, at)
        db.close()
    assert _place(tmp_path / "cold", weekly=weekly).account == ALT
    # The budget about to be lost outranks the card's warm account.
    assert _place(tmp_path / "warm", weekly=weekly, warm=MAIN).account == ALT
    assert _place(tmp_path / "past", weekly=weekly).account == MAIN
    # A target of 0 opts the account out, plan or not (the owner's choice).
    assert _place(tmp_path / "cold", weekly=weekly, targets={ALT: 0}).account == MAIN
    # Still nothing goes to a window that would refuse the turn.
    full = [(MAIN, 22.0, timedelta(days=6)), (ALT, 100.0, timedelta(days=5))]
    assert _place(tmp_path / "full", weekly=full).account == MAIN


# --- the runtime app: selection, admission and the frozen route ------------------------------


def _config(tmp_path: Path, *, limit: int = 4) -> AppConfig:
    return AppConfig.model_validate({
        "claude_workers": {"enabled": True, "state_dir": str(tmp_path)},
        "keyed_workers": {"state_dir": str(tmp_path), "max_kanban_workers": limit},
        "restart_nudge": {"file": str(tmp_path / "restart-nudge.json")},
        "history": {"db_path": str(tmp_path / "control.db")},
        "usage": {"shared_cache_dir": str(tmp_path / "usage")},
        "accounts": [
            AccountConfig(
                provider="claude_code", label=label, config_dir=str(tmp_path)
            ).model_dump()
            for label in ("main", "alt")
        ],
    })


def _app(tmp_path: Path, *, limit: int = 4, **hosts: FakeHost):
    (tmp_path / "usage").mkdir(exist_ok=True)
    app = create_runtime_app(_config(tmp_path, limit=limit))
    for label in ("main", "alt"):
        app.state.claude_workers.hosts[label] = hosts.get(label) or FakeHost()
    return app


async def _post(app, body: dict) -> dict:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        response = await client.post(ENSURE_DELIVERY_PATH, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_runtime_chooses_freezes_and_reports_the_target(tmp_path):
    main, alt = FakeHost(), FakeHost()
    account_health.trip_hard_quota(tmp_path / "usage", MAIN, reason="window exhausted")
    app = _app(tmp_path, main=main, alt=alt)

    answer = await _post(app, _body())

    assert answer["accepted"] is True
    assert answer["session_key"] == "claude_code:alt:s1"
    assert answer["schedule"]["decision"] == "selected"
    assert answer["schedule"]["account"] == ALT
    assert any("hard quota circuit" in note for note in answer["schedule"]["notes"])
    assert (main.calls, alt.calls) == ([], [KEY])
    route = app.state.budget_db.delivery_route("poller:d1")
    assert (route.provider, route.account, route.worker_key) == ("claude_code", "alt", KEY)
    assert (route.work_id, route.run_id) == (WORK, "gen-1")


async def test_the_frozen_route_says_whether_its_account_could_run_the_turn(tmp_path):
    """#1519, #2200: judged when the route is frozen, because that is the moment in question —
    a window read an hour later may have filled while the turn worked. The Run's outcome
    carries it to the Poller, which tells a refused turn from a dead worker by it."""
    app = _app(tmp_path)
    _weekly(tmp_path / "usage", MAIN, pct=100.0, resets_in=timedelta(days=3))
    _weekly(tmp_path / "usage", ALT, pct=100.0, resets_in=timedelta(days=1))

    answer = await _post(app, _body())

    assert answer["schedule"]["account"] == ALT, "every account refuses; the earliest clears"
    route = app.state.budget_db.delivery_route("poller:d1")
    assert route.refused_at_delivery.startswith("seven_day window at 100% until ")


async def test_an_ordinary_route_stamps_no_refusal(tmp_path):
    app = _app(tmp_path)

    await _post(app, _body())

    assert app.state.budget_db.delivery_route("poller:d1").refused_at_delivery == ""


async def test_an_ambiguous_delivery_never_moves_even_when_its_account_turns_unhealthy(tmp_path):
    uncertain = DeliverResult(True, "uncertain", session_id="s1", provider_write_state="unknown")
    main, alt = FakeHost(uncertain), FakeHost()
    app = _app(tmp_path, main=main, alt=alt)
    assert (await _post(app, _body()))["schedule"]["account"] == MAIN

    # Now main is circuited and alt is where fresh selection would go. The retry of the same
    # delivery reaches main anyway: its write may have started there.
    account_health.trip_hard_quota(tmp_path / "usage", MAIN, reason="window exhausted")
    answer = await _post(app, _body())

    assert answer["schedule"]["decision"] == "frozen"
    assert answer["schedule"]["account"] == MAIN
    assert (main.calls, alt.calls) == ([KEY, KEY], [])


async def test_a_restarted_runtime_replays_the_route_it_froze(tmp_path):
    uncertain = DeliverResult(True, "uncertain", session_id="s1", provider_write_state="unknown")
    first = _app(tmp_path, main=FakeHost(uncertain))
    await _post(first, _body())
    first.state.budget_db.close()

    account_health.trip_hard_quota(tmp_path / "usage", MAIN, reason="window exhausted")
    main, alt = FakeHost(uncertain), FakeHost()
    answer = await _post(_app(tmp_path, main=main, alt=alt), _body())

    assert (answer["schedule"]["decision"], answer["schedule"]["account"]) == ("frozen", MAIN)
    assert (main.calls, alt.calls) == ([KEY], [])


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"message": "something else"}, "delivery_id_conflict"),
        ({"worker_key": "agentdeck#9"}, "delivery_id_conflict"),
        ({"run_id": "gen-2"}, "delivery_correlation_conflict"),
    ],
)
async def test_a_conflicting_request_under_a_frozen_id_is_refused(tmp_path, change, reason):
    uncertain = DeliverResult(True, "uncertain", session_id="s1", provider_write_state="unknown")
    main = FakeHost(uncertain)
    app = _app(tmp_path, main=main)
    await _post(app, _body())

    answer = await _post(app, _body(**change))

    assert (answer["accepted"], answer["reason"]) == (False, reason)
    assert main.calls == [KEY]  # the conflicting request reached no host


async def test_a_replay_that_names_another_chat_is_refused_and_links_nothing(tmp_path):
    """The chat a delivery continues is part of the request (#2185 review): a replay naming a
    different one must not bind the already-returned session into that chat."""
    uncertain = DeliverResult(True, "uncertain", session_id="s1", provider_write_state="unknown")
    main = FakeHost(uncertain)
    app = _app(tmp_path, main=main)
    db = app.state.budget_db
    await _post(app, _body())
    chat = db.ensure_session_chats(("claude_code:alt:s0",))[0]

    answer = await _post(
        app, _body(continues_session_url=f"https://deck.example/chats/{chat.chat_id}")
    )

    assert (answer["accepted"], answer["reason"]) == (False, "delivery_id_conflict")
    assert "session_key" not in answer
    assert db.chat_by_id(chat.chat_id).legs == ("claude_code:alt:s0",)
    assert db.pending_chat_successor("claude_code:alt:s0") is None
    assert main.calls == [KEY]


async def test_an_overlay_edit_between_retries_is_not_a_conflict(tmp_path):
    uncertain = DeliverResult(True, "uncertain", session_id="s1", provider_write_state="unknown")
    main = FakeHost(uncertain)
    app = _app(tmp_path, main=main)
    await _post(app, _body())

    answer = await _post(app, _body(requirements={"candidates": [ALT]}))

    # Policy picks among fresh targets only; the frozen one still stands.
    assert (answer["schedule"]["decision"], answer["schedule"]["account"]) == ("frozen", MAIN)


async def test_a_refusal_that_provably_wrote_nothing_releases_the_route(tmp_path):
    main = FakeHost(DeliverResult(False, "rejected", reason="worker_changed"))
    app = _app(tmp_path, main=main)

    answer = await _post(app, _body())

    assert answer["reason"] == "worker_changed"
    assert app.state.budget_db.delivery_route("poller:d1") is None
    assert "route released" in " ".join(answer["schedule"]["notes"])


async def test_a_refusal_whose_receipt_shows_a_write_keeps_the_route(tmp_path):
    started = {"fingerprint": "f", "action": "rejected", "provider_write_state": "started"}
    main = FakeHost(DeliverResult(False, "rejected", reason="spawn crashed"), receipt=started)
    app = _app(tmp_path, main=main)

    await _post(app, _body())

    assert app.state.budget_db.delivery_route("poller:d1").account == "main"


async def test_selection_and_the_last_slot_are_taken_together(tmp_path):
    gate = asyncio.Event()
    main, alt = FakeHost(gate=gate), FakeHost(gate=gate)
    app = _app(tmp_path, limit=1, main=main, alt=alt)

    async def release_soon():
        await asyncio.sleep(0.05)
        gate.set()

    answers = await asyncio.gather(
        _post(app, _body()),
        _post(app, _body(delivery_id="poller:d2", worker_key="agentdeck#7",
                         work_id="github_issue:eerovil/agentdeck#7",
                         issue_ref="eerovil/agentdeck#7")),
        release_soon(),
    )

    reasons = sorted(str(answer.get("reason")) for answer in answers[:2])
    assert reasons == ["None", "at_capacity"]
    refused = next(answer for answer in answers[:2] if answer.get("reason") == "at_capacity")
    assert refused["schedule"]["reason"] == "at_capacity"
    # The refused delivery froze nothing, so its retry is free to choose again.
    frozen = [app.state.budget_db.delivery_route(d) for d in ("poller:d1", "poller:d2")]
    assert sum(route is not None for route in frozen) == 1
    assert len(main.calls) + len(alt.calls) == 1


async def test_reusing_the_cards_active_worker_takes_no_new_slot(tmp_path):
    main = FakeHost(workers={KEY: FRESH_TURN})
    main.active = {KEY}  # the card's own worker holds the only slot
    app = _app(tmp_path, limit=1, main=main)
    _route(app.state.budget_db, "poller:earlier", MAIN)

    answer = await _post(app, _body(delivery_id="poller:d2", run_id="gen-2"))

    assert answer["accepted"] is True
    assert answer["schedule"]["account"] == MAIN
    assert any("mid-turn" in note for note in answer["schedule"]["notes"])


async def test_a_candidate_outside_the_allow_list_is_never_chosen(tmp_path):
    # The card last ran on main, but the operator now offers only alt.
    main, alt = FakeHost(), FakeHost()
    app = _app(tmp_path, main=main, alt=alt)
    _route(app.state.budget_db, "poller:earlier", MAIN)

    answer = await _post(app, _body(requirements={"candidates": [ALT]}))

    assert answer["schedule"]["account"] == ALT
    assert (main.calls, alt.calls) == ([], [KEY])


async def test_the_first_delivery_after_cutover_finds_the_warm_account_from_worker_records(
    tmp_path,
):
    alt = FakeHost(workers={KEY: {"live": True, "last_result_at": 100.0, "work_id": WORK}})
    app = _app(tmp_path, alt=alt)

    answer = await _post(app, _body())

    assert answer["schedule"]["account"] == ALT


async def test_another_works_worker_under_the_same_key_is_not_this_cards_warm_account(tmp_path):
    """The worker key is a compatibility name (#2185 review): a record carrying another Work
    under the same key is neither a warm account nor a mid-turn pin for this one."""
    other = {**FRESH_TURN, "last_result_at": 100.0, "work_id": OTHER_WORK, "run_id": "gen-1"}
    main, alt = FakeHost(), FakeHost(workers={KEY: other})
    app = _app(tmp_path, main=main, alt=alt)

    answer = await _post(app, _body())

    assert answer["schedule"]["account"] == MAIN
    assert not any("mid-turn" in note for note in answer["schedule"]["notes"])
    assert (main.calls, alt.calls) == ([KEY], [])


async def test_the_mid_turn_pin_ignores_another_works_worker_on_the_warm_account(tmp_path):
    main = FakeHost(workers={KEY: {**FRESH_TURN, "work_id": OTHER_WORK}})
    app = _app(tmp_path, main=main)
    _route(app.state.budget_db, "poller:earlier", MAIN)
    account_health.trip_hard_quota(tmp_path / "usage", MAIN, reason="window exhausted")

    answer = await _post(app, _body())

    # Pinned, this would withhold on main's circuit; unpinned it moves to alt.
    assert answer["schedule"]["account"] == ALT


@pytest.mark.parametrize(
    ("requirements", "reason"),
    [
        ({"candidates": []}, NO_CANDIDATE_ACCOUNTS),
        ({"candidates": ["codex:work"]}, RUNTIME_PROVIDER_UNAVAILABLE),
        ({"candidates": ["claude_code:nobody"]}, RUNTIME_PROVIDER_UNAVAILABLE),
    ],
)
async def test_each_refusal_says_why_and_reaches_no_host(tmp_path, requirements, reason):
    main, alt = FakeHost(), FakeHost()
    app = _app(tmp_path, main=main, alt=alt)

    answer = await _post(app, _body(requirements=requirements))

    assert (answer["accepted"], answer["reason"]) == (False, reason)
    assert answer["schedule"]["reason"] == reason
    assert (main.calls, alt.calls) == ([], [])
    assert app.state.budget_db.delivery_route("poller:d1") is None


async def test_a_fleet_with_every_circuit_open_is_refused_as_such(tmp_path):
    for account in (MAIN, ALT):
        account_health.trip_hard_quota(tmp_path / "usage", account, reason="window exhausted")
    app = _app(tmp_path)

    answer = await _post(app, _body())

    assert answer["reason"] == ALL_ACCOUNTS_HARD_INELIGIBLE


async def test_a_steer_goes_to_the_account_holding_its_run(tmp_path):
    # Even circuited: a steer is a follow-up into a turn already running there.
    account_health.trip_hard_quota(tmp_path / "usage", ALT, reason="window exhausted")
    main, alt = FakeHost(), FakeHost()
    app = _app(tmp_path, main=main, alt=alt)
    _route(app.state.budget_db, "poller:first", ALT, run_id="gen-1")

    answer = await _post(app, _body(delivery_id="poller:steer", require_active=True))

    assert (answer["schedule"]["decision"], answer["schedule"]["account"]) == ("run_holder", ALT)
    assert (main.calls, alt.calls) == ([], [KEY])


async def test_a_steer_for_a_run_from_before_routes_finds_its_worker(tmp_path):
    alt = FakeHost(workers={KEY: {"live": True, "work_id": WORK, "run_id": "gen-1"}})
    app = _app(tmp_path, alt=alt)

    answer = await _post(app, _body(delivery_id="poller:steer", require_active=True))

    assert answer["schedule"]["account"] == ALT


async def test_a_steer_is_not_sent_to_another_works_run_under_the_same_key(tmp_path):
    alt = FakeHost(workers={KEY: {"live": True, "work_id": OTHER_WORK, "run_id": "gen-1"}})
    app = _app(tmp_path, alt=alt)

    answer = await _post(app, _body(delivery_id="poller:steer", require_active=True))

    assert answer["reason"] == REQUIREMENTS_UNSATISFIED
    assert alt.calls == []


async def test_a_steer_nothing_holds_is_refused(tmp_path):
    app = _app(tmp_path)

    answer = await _post(app, _body(delivery_id="poller:steer", require_active=True))

    assert answer["reason"] == REQUIREMENTS_UNSATISFIED


def test_requirements_are_required_and_a_fixed_target_is_refused():
    fixed = _body(fixed_target={"provider": "claude_code", "label": "main"})
    neither = {key: value for key, value in _body().items() if key != "requirements"}
    for body in (fixed, neither):
        with pytest.raises(ValueError):
            EnsureDeliveryRequest.model_validate(body)


def test_the_fingerprint_ignores_policy_but_not_the_request():
    base = EnsureDeliveryRequest.model_validate(_body())
    edited = EnsureDeliveryRequest.model_validate(_body(requirements={"candidates": [ALT]}))
    other = EnsureDeliveryRequest.model_validate(_body(message="other"))
    chained = EnsureDeliveryRequest.model_validate(
        _body(continues_session_url="https://deck.example/chats/c1")
    )

    assert request_fingerprint(base) == request_fingerprint(edited)
    assert request_fingerprint(base) != request_fingerprint(other)
    assert request_fingerprint(base) != request_fingerprint(chained)


async def test_a_pending_older_run_is_stopped_before_a_new_run_is_placed_elsewhere(tmp_path):
    """Review of #2189: the Work/Run check runs inside the scheduler's lock, once the target
    is known. The older Run was delivered on main and has not started its turn yet, so no
    mid-turn pin holds the card there; main's circuit is open, so the new Run is placed on
    alt. That is a second worker in the same Work workspace, so the older one must be proved
    stopped first — and this host cannot stop it, so nothing is written and nothing frozen."""
    import time

    pending = {
        "live": True,
        "turn_active": False,
        "effective_activity": False,
        "last_delivery_at": time.time(),
        "last_result_at": 0.0,
        "work_id": WORK,
        "run_id": "gen-0",
    }
    main, alt = FakeHost(workers={KEY: pending}), FakeHost()
    app = _app(tmp_path, main=main, alt=alt)
    _route(app.state.budget_db, "poller:earlier", MAIN)
    account_health.trip_hard_quota(tmp_path / "usage", MAIN, reason="window exhausted")

    answer = await _post(app, _body(delivery_id="poller:d2", run_id="gen-2"))

    assert (answer["accepted"], answer["reason"]) == (False, "prior_run_executing")
    assert (main.calls, alt.calls) == ([], [])
    assert app.state.budget_db.delivery_route("poller:d2") is None
