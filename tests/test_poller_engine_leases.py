"""Slice 3 of #176: generation leases."""

from __future__ import annotations

from agentdeck.poller_engine import leases


def test_new_generation_is_unique_hex():
    a, b = leases.new_generation(), leases.new_generation()
    assert a != b
    assert all(c in "0123456789abcdef" for c in a)


def test_bump_installs_and_returns():
    rec: dict = {}
    gen = leases.bump(rec)
    assert rec["generation"] == gen
    assert leases.current(rec) == gen


def test_holds_only_the_exact_current_generation():
    rec: dict = {}
    gen = leases.bump(rec)
    assert leases.holds(rec, gen)
    assert not leases.holds(rec, "other")
    assert not leases.holds(rec, "")


def test_invalidate_rejects_the_prior_holder():
    rec: dict = {}
    old = leases.bump(rec)
    new = leases.invalidate(rec)
    assert new != old
    assert not leases.holds(rec, old)  # the in-flight worker's lease is now stale
    assert leases.holds(rec, new)


def test_an_undelivered_continuation_keeps_the_lease_it_replaced_usable():
    """#1801: the successor is minted a whole wake before its delivery is settled, and the
    commonest refusal is that the very worker it continues is still mid-turn. Fencing that
    worker out in the meantime kills a turn nobody has taken over."""
    rec: dict = {}
    old = leases.bump(rec)
    new = leases.bump(rec)
    rec["dispatch"] = {"continuation": True, "supersedes": old, "generation": new}

    assert leases.outgoing(rec) == old
    assert leases.holds_or_outgoing(rec, old)
    assert leases.holds_or_outgoing(rec, new)
    assert not leases.holds_or_outgoing(rec, "someone-else")
    assert not leases.holds_or_outgoing(rec, "")
    # The fence itself is untouched: only the grace above knows about the successor.
    assert not leases.holds(rec, old)


def test_the_grace_ends_when_the_successor_dispatch_does():
    rec: dict = {}
    old = leases.bump(rec)
    leases.bump(rec)
    rec["dispatch"] = {"continuation": True, "supersedes": old}

    rec.pop("dispatch")

    assert leases.outgoing(rec) == ""
    assert not leases.holds_or_outgoing(rec, old)


def test_a_fresh_dispatch_is_not_a_continuation_and_grants_nothing():
    """A revive or a retry deliberately replaces the worker, so its predecessor stays fenced."""
    rec: dict = {}
    old = leases.bump(rec)
    new = leases.bump(rec)
    rec["dispatch"] = {"supersedes": old, "generation": new}

    assert leases.outgoing(rec) == ""
    assert not leases.holds_or_outgoing(rec, old)
