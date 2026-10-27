"""The heavy-slot lease pool: taking a slot, keeping it, and losing it.

The property that matters most is the last one. A ``flock`` dies with the
process holding it, so the existing queue cannot strand a slot; an HTTP lease
can, and this host has one heavy slot to strand. So the reaper is tested as hard
as the happy path.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck.config import AppConfig, HistoryConfig, JobsConfig
from agentdeck.host_slots import SLOT_DIR_ENV, MemoryGate, slot_paths, try_take_slot
from agentdeck.runtime import create_runtime_app
from agentdeck.slot_leases import DEFAULT_TTL_S, LeaseError, SlotLeases


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_pool(tmp_path, clock, *, count: int = 1, ttl_s: float = 100.0) -> SlotLeases:
    return SlotLeases(
        slot_paths=slot_paths("heavy-test", count, tmp_path),
        ttl_s=ttl_s,
        clock=clock,
    )


async def test_a_lease_takes_the_only_slot_and_the_next_caller_is_told_so(
    tmp_path, clock
):
    pool = make_pool(tmp_path, clock)

    first = await pool.acquire(label="scan")
    assert first is not None
    assert first.label == "scan"

    assert await pool.acquire(label="another scan") is None
    assert pool.short_of_memory is False


async def test_a_lease_waits_while_the_host_is_short_of_memory(tmp_path, clock):
    """#3614: a lease competes with a queued suite for memory too, so it waits for it."""
    memory = [False]
    pool = SlotLeases(
        slot_paths=slot_paths("heavy-test", 1, tmp_path), clock=clock,
        memory_gate=MemoryGate(available=lambda: 8 * 1024**3 if memory[0] else 0, floor=lambda: 1),
    )

    assert await pool.acquire(label="scan") is None
    assert pool.short_of_memory is True
    memory[0] = True
    assert await pool.acquire(label="scan") is not None
    assert pool.short_of_memory is False


async def test_releasing_frees_the_slot_for_the_next_caller(tmp_path, clock):
    pool = make_pool(tmp_path, clock)
    first = await pool.acquire()
    assert first is not None

    assert pool.release(first.id) is True

    second = await pool.acquire()
    assert second is not None
    assert second.id != first.id


async def test_releasing_a_lease_that_is_already_gone_is_not_an_error(tmp_path, clock):
    pool = make_pool(tmp_path, clock)
    lease = await pool.acquire()
    assert lease is not None
    assert pool.release(lease.id) is True
    assert pool.release(lease.id) is False


async def test_a_lease_nobody_heartbeats_loses_its_slot(tmp_path, clock):
    """The whole reason a lease is not just a flock."""
    pool = make_pool(tmp_path, clock, ttl_s=30.0)
    abandoned = await pool.acquire(label="a client that died")
    assert abandoned is not None
    assert await pool.acquire() is None

    clock.advance(31.0)

    replacement = await pool.acquire(label="someone else")
    assert replacement is not None
    assert pool.get(abandoned.id) is None


async def test_a_heartbeat_keeps_the_slot(tmp_path, clock):
    pool = make_pool(tmp_path, clock, ttl_s=30.0)
    lease = await pool.acquire()
    assert lease is not None

    clock.advance(20.0)
    assert pool.heartbeat(lease.id) is not None
    clock.advance(20.0)
    assert pool.heartbeat(lease.id) is not None

    # 40s of a 30s ttl have passed and it is still held, because it was renewed.
    assert await pool.acquire() is None


async def test_a_heartbeat_after_expiry_says_the_lease_is_gone(tmp_path, clock):
    """The caller has to be able to tell "still mine" from "someone else may be
    running now"; a renewed-anyway heartbeat would hide exactly that."""
    pool = make_pool(tmp_path, clock, ttl_s=30.0)
    lease = await pool.acquire()
    assert lease is not None

    clock.advance(31.0)

    assert pool.heartbeat(lease.id) is None


async def test_a_heartbeat_for_an_unknown_lease_is_none(tmp_path, clock):
    pool = make_pool(tmp_path, clock)
    assert pool.heartbeat("never-issued") is None


async def test_releasing_a_just_expired_lease_still_reads_as_a_release(tmp_path, clock):
    """The caller did the right thing and should not be told otherwise."""
    pool = make_pool(tmp_path, clock, ttl_s=30.0)
    lease = await pool.acquire()
    assert lease is not None

    clock.advance(31.0)

    assert pool.release(lease.id) is True


async def test_a_lease_and_a_direct_flock_compete_for_one_pool(tmp_path, clock):
    """A lease and a queued suite are meant to contend: same cores, same pool."""
    paths = slot_paths("heavy-test", 1, tmp_path)
    pool = SlotLeases(slot_paths=paths, clock=clock)

    held = try_take_slot(paths)
    assert held is not None
    try:
        assert await pool.acquire() is None
    finally:
        import os

        os.close(held)

    assert await pool.acquire() is not None


async def test_two_slots_serve_two_callers_and_no_more(tmp_path, clock):
    pool = make_pool(tmp_path, clock, count=2)
    assert await pool.acquire() is not None
    assert await pool.acquire() is not None
    assert await pool.acquire() is None


async def test_a_ttl_beyond_the_ceiling_is_clamped_not_refused(tmp_path, clock):
    """A caller asking for too long still gets a working lease — refusing would
    push it towards not taking a slot at all."""
    pool = SlotLeases(
        slot_paths=slot_paths("heavy-test", 1, tmp_path),
        ttl_s=100.0,
        max_ttl_s=300.0,
        clock=clock,
    )
    lease = await pool.acquire(ttl_s=86_400.0)
    assert lease is not None
    assert lease.ttl_s == 300.0


@pytest.mark.parametrize("bad", [0, -5, "soon"])
async def test_a_ttl_that_is_not_a_positive_number_is_refused(tmp_path, clock, bad):
    pool = make_pool(tmp_path, clock)
    with pytest.raises(LeaseError):
        await pool.acquire(ttl_s=bad)


async def test_stopping_the_pool_gives_every_slot_back(tmp_path, clock):
    pool = make_pool(tmp_path, clock)
    await pool.start()
    lease = await pool.acquire()
    assert lease is not None

    await pool.stop()

    paths = slot_paths("heavy-test", 1, tmp_path)
    reclaimed = try_take_slot(paths)
    assert reclaimed is not None
    import os

    os.close(reclaimed)


async def test_the_wire_form_reports_durations_not_the_pools_clock(tmp_path, clock):
    """``time.monotonic`` is the right clock for an expiry and a meaningless
    number in another process."""
    pool = make_pool(tmp_path, clock, ttl_s=60.0)
    lease = await pool.acquire(label="scan")
    assert lease is not None

    clock.advance(15.0)
    payload = pool.payload(lease)

    assert payload["lease"] == lease.id
    assert payload["label"] == "scan"
    assert payload["ttl_s"] == 60.0
    assert payload["expires_in_s"] == pytest.approx(45.0)
    assert payload["held_for_s"] == pytest.approx(15.0)
    assert "expires_at" not in payload
    assert "acquired_at" not in payload


# ---- the runtime's own routes ----------------------------------------------


def _runtime_app(tmp_path, monkeypatch):
    """A runtime whose slot pool is this test's, never the host's.

    Without the redirect these tests would take the machine's single real heavy
    slot and hold it — starving whatever else is running, including the suite
    they are part of.
    """
    root = tmp_path / "slots"
    # `slot_directory` makes the pool folder but not its parent — the real roots
    # (`/run/user/<uid>`, the tmp dir) always exist.
    root.mkdir()
    monkeypatch.setenv(SLOT_DIR_ENV, str(root))
    return create_runtime_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "runtime.db")),
            jobs=JobsConfig(state_dir=str(tmp_path / "jobs"), slots=1),
            accounts=[],
        )
    )


async def test_the_routes_hand_out_one_slot_renew_it_and_take_it_back(
    tmp_path, monkeypatch
):
    app = _runtime_app(tmp_path, monkeypatch)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        taken = await client.post("/heavy-slots", json={"label": "scan"})
        while_busy = await client.post("/heavy-slots", json={"label": "another"})
        lease = taken.json()["lease"]
        listed = await client.get("/heavy-slots")
        renewed = await client.post(f"/heavy-slots/{lease}/heartbeat")
        released = await client.request("DELETE", f"/heavy-slots/{lease}")
        after = await client.post("/heavy-slots", json={"label": "next in line"})

    assert taken.status_code == 200
    assert taken.json()["label"] == "scan"
    # Busy is not a fault: 503 is what a caller's retry-on-unavailable expects.
    assert while_busy.status_code == 503
    assert "retry" in while_busy.json()["detail"]
    assert listed.json()["slots"] == 1
    assert [held["lease"] for held in listed.json()["leases"]] == [lease]
    assert renewed.status_code == 200
    assert released.json() == {"lease": lease, "released": True}
    assert after.status_code == 200
    app.state.budget_db.close()


async def test_a_heartbeat_for_a_lease_nobody_holds_is_a_404(tmp_path, monkeypatch):
    app = _runtime_app(tmp_path, monkeypatch)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post("/heavy-slots/never-issued/heartbeat")

    assert response.status_code == 404
    assert "not held" in response.json()["detail"]
    app.state.budget_db.close()


async def test_an_empty_body_is_a_perfectly_good_ask(tmp_path, monkeypatch):
    app = _runtime_app(tmp_path, monkeypatch)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post("/heavy-slots", json={})

    assert response.status_code == 200
    assert response.json()["ttl_s"] == DEFAULT_TTL_S
    app.state.budget_db.close()


async def test_a_nonsense_ttl_is_refused_rather_than_quietly_defaulted(
    tmp_path, monkeypatch
):
    app = _runtime_app(tmp_path, monkeypatch)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post("/heavy-slots", json={"ttl_s": -1})

    assert response.status_code == 422
    app.state.budget_db.close()
