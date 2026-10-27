"""Lossless topic invalidations alongside the unchanged bounded payload queue."""

import pytest

from agentdeck.events import EventBus


def test_changed_topics_survive_overflow_and_coalesce():
    bus = EventBus(maxsize=2)
    with bus.subscribe("jobs", "sessions", "deployment") as sub:
        bus.publish("jobs")
        bus.publish("deployment")
        for index in range(100):
            bus.publish("sessions", index)
        assert sub.queue.qsize() == 2
        assert sub.changed_topics() == {"jobs", "deployment", "sessions"}
        assert sub.changed_topics() == set()
        # Reading invalidations neither drains nor changes surviving payloads.
        assert sub.get_nowait() == ("sessions", 98)
        assert sub.get_nowait() == ("sessions", 99)
        assert sub.get_nowait() is None


def test_changed_topics_are_scoped_and_do_not_replay_before_subscription():
    bus = EventBus()
    bus.publish("jobs")
    with bus.subscribe("jobs") as sub:
        assert sub.changed_topics() == set()
        bus.publish("sessions")
        assert sub.changed_topics() == set()
        bus.publish("jobs")
        assert sub.changed_topics() == {"jobs"}


def test_changed_topics_have_independent_subscriber_cursors():
    bus = EventBus()
    with bus.subscribe("jobs") as first, bus.subscribe("jobs") as second:
        bus.publish("jobs")
        assert first.changed_topics() == {"jobs"}
        assert first.changed_topics() == set()
        assert second.changed_topics() == {"jobs"}
        bus.publish("jobs")
        assert first.changed_topics() == second.changed_topics() == {"jobs"}


@pytest.mark.asyncio
async def test_payload_reads_do_not_consume_topic_invalidations():
    bus = EventBus()
    with bus.subscribe("chat") as sub:
        bus.publish("chat", "session-one")
        bus.publish("chat", "session-two")
        assert await sub.get() == ("chat", "session-one")
        assert sub.get_nowait() == ("chat", "session-two")
        assert sub.changed_topics() == {"chat"}
        assert sub.changed_topics() == set()
    assert not bus._subs.get("chat")


def test_no_subscribed_topics_is_empty():
    bus = EventBus()
    with bus.subscribe() as sub:
        bus.publish("jobs")
        assert sub.changed_topics() == set()
