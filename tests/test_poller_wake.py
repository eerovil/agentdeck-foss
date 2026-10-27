"""The second webhook consumer: a delivery starts a poller instance early (#1649).

Fixtures go through `WebhookLog`, the same appender the live ingress route uses, so the
filter and the routing are asserted over real stream bytes. The unit start is stubbed —
what is under test is which instances get started and how often, never systemd.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentdeck import webhook_tailer
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.poller_wake import (
    ManifestRoutes,
    PollerWakeService,
    heartbeat_path,
    wakes,
)
from agentdeck.webhook_tailer import WebhookTailer

AGENTDECK = Repository(owner="eerovil", name="agentdeck")
TRAILHEAD = Repository(owner="eerovil", name="trailhead")
STRANGER = Repository(owner="someone", name="unclaimed")


def body(repository: Repository = AGENTDECK, **fields) -> bytes:
    payload = {
        "repository": {
            "full_name": repository.slug,
            "name": repository.name,
            "owner": {"login": repository.owner},
        },
        **fields,
    }
    return json.dumps(payload).encode()


def deliver(
    root, *, event: str, delivery: str, repository: Repository = AGENTDECK, **fields
) -> None:
    WebhookLog(root).append(
        body=body(repository, **fields),
        headers={"X-GitHub-Delivery": delivery, "X-GitHub-Event": event},
    )


def manifest(poller_root, instance: str, org: str, *repos: str) -> None:
    base = poller_root / "instances" / instance
    base.mkdir(parents=True, exist_ok=True)
    lines = [f'contract = 1\n\n[board]\norg = "{org}"\nproject_number = 1\n']
    lines += [f'\n[[repos]]\nid = "{repo}"\nholds_issues = true\n' for repo in repos]
    (base / "poller.toml").write_text("".join(lines), encoding="utf-8")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "github-webhooks"


@pytest.fixture
def poller_root(tmp_path):
    root = tmp_path / "poller"
    manifest(root, "agentdeck", "eerovil", "agentdeck")
    manifest(root, "trailhead", "eerovil", "trailhead", "docker")
    return root


class Starts:
    """Stands in for `systemctl start`, recording what would have been started."""

    def __init__(self) -> None:
        self.units: list[str] = []

    def __call__(self, instance: str) -> None:
        self.units.append(instance)


@pytest.fixture
def starts() -> Starts:
    return Starts()


@pytest.fixture
def clock():
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    return Clock()


@pytest.fixture
def service(root, poller_root, starts, clock) -> PollerWakeService:
    return PollerWakeService(
        root, poller_root, start_unit=starts, clock=clock
    )


def drain(root, service: PollerWakeService, *, consumer: str = "test-wake") -> None:
    """Feed everything on the streams through the service, as its own loop would."""
    tailer = WebhookTailer(root, consumer=consumer)
    batch = tailer.read()
    asyncio.run(service.apply(batch))
    tailer.commit()


# -- the filter ------------------------------------------------------------------


@pytest.mark.parametrize(
    "event", ["issues", "issue_comment", "pull_request", "pull_request_review"]
)
def test_work_events_wake(root, service, starts, event):
    deliver(root, event=event, delivery=f"d-{event}", action="opened")
    drain(root, service)
    assert starts.units == ["agentdeck"]


@pytest.mark.parametrize("event", ["check_suite", "workflow_run"])
def test_ci_wakes_only_when_completed(root, service, starts, event):
    deliver(root, event=event, delivery=f"{event}-1", action="requested")
    deliver(root, event=event, delivery=f"{event}-2", action="in_progress")
    drain(root, service)
    assert starts.units == []

    deliver(root, event=event, delivery=f"{event}-3", action="completed")
    drain(root, service)
    assert starts.units == ["agentdeck"]


@pytest.mark.parametrize("event", ["push", "star", "create", "status", "ping"])
def test_noise_never_wakes(root, service, starts, event):
    deliver(root, event=event, delivery=f"noise-{event}", action="completed")
    drain(root, service)
    assert starts.units == []


def test_completion_event_without_an_action_does_not_wake(root, service, starts):
    deliver(root, event="check_suite", delivery="actionless")
    drain(root, service)
    assert starts.units == []


class FakeDelivery:
    def __init__(self, event: str, raw_body: bytes) -> None:
        self.event = event
        self.raw_body = raw_body


def test_wakes_reads_the_action_not_the_event_alone():
    completed = json.dumps({"action": "completed"}).encode()
    assert wakes(FakeDelivery("workflow_run", completed)) is True
    assert wakes(FakeDelivery("workflow_run", b'{"action": "requested"}')) is False


def test_a_completion_payload_that_cannot_be_read_does_not_wake():
    assert wakes(FakeDelivery("check_suite", b"not json at all")) is False
    assert wakes(FakeDelivery("check_suite", b"[]")) is False


# -- routing ---------------------------------------------------------------------


def test_routes_each_repository_to_the_instance_that_declares_it(poller_root):
    routes = ManifestRoutes(poller_root)
    assert routes.instances_for("eerovil/agentdeck") == ("agentdeck",)
    assert routes.instances_for("eerovil/docker") == ("trailhead",)
    assert routes.instances_for("eerovil/unknown") == ()


def test_routing_is_case_insensitive(poller_root):
    assert ManifestRoutes(poller_root).instances_for("EeroVil/AgentDeck") == (
        "agentdeck",
    )


def test_two_instances_claiming_one_repository_are_both_woken(
    root, poller_root, starts, clock
):
    manifest(poller_root, "second", "eerovil", "agentdeck")
    service = PollerWakeService(root, poller_root, start_unit=starts, clock=clock)
    deliver(root, event="issues", delivery="shared", action="opened")
    drain(root, service)
    assert sorted(starts.units) == ["agentdeck", "second"]


def test_unclaimed_repository_wakes_nothing(root, service, starts):
    deliver(root, event="issues", delivery="stranger", repository=STRANGER, action="opened")
    drain(root, service)
    assert starts.units == []


def test_new_manifest_routes_without_a_restart(root, poller_root, service, starts, clock):
    deliver(root, event="issues", delivery="before", repository=TRAILHEAD, action="opened")
    drain(root, service)
    assert starts.units == ["trailhead"]

    manifest(poller_root, "trailhead", "eerovil", "trailhead", "docker", "agentdeck")
    clock.now += 60.0
    deliver(root, event="issues", delivery="after", action="opened")
    drain(root, service)
    assert sorted(starts.units[1:]) == ["agentdeck", "trailhead"]


def test_unparseable_manifest_routes_nothing_but_leaves_the_rest(
    root, poller_root, service, starts
):
    (poller_root / "instances" / "agentdeck" / "poller.toml").write_text(
        "contract = [broken", encoding="utf-8"
    )
    deliver(root, event="issues", delivery="broken-manifest", action="opened")
    deliver(root, event="issues", delivery="still-fine", repository=TRAILHEAD, action="opened")
    drain(root, service)
    assert starts.units == ["trailhead"]


# -- the cooldown ----------------------------------------------------------------


def test_one_batch_wakes_an_instance_once(root, service, starts):
    for index in range(4):
        deliver(root, event="issue_comment", delivery=f"burst-{index}", action="created")
    drain(root, service)
    assert starts.units == ["agentdeck"]


def test_cooldown_holds_a_second_wake_and_then_releases_it(root, service, starts, clock):
    deliver(root, event="issues", delivery="first", action="opened")
    drain(root, service)
    clock.now += 3.0
    deliver(root, event="issues", delivery="second", action="closed")
    drain(root, service)
    assert starts.units == ["agentdeck"]

    clock.now += 8.0
    deliver(root, event="issues", delivery="third", action="reopened")
    drain(root, service)
    assert starts.units == ["agentdeck", "agentdeck"]


def test_cooldown_is_per_instance(root, service, starts):
    deliver(root, event="issues", delivery="one", action="opened")
    deliver(root, event="issues", delivery="two", repository=TRAILHEAD, action="opened")
    drain(root, service)
    assert sorted(starts.units) == ["agentdeck", "trailhead"]


# -- statelessness ---------------------------------------------------------------


def test_seek_to_end_discards_the_backlog(root, service, starts):
    for index in range(3):
        deliver(root, event="issues", delivery=f"old-{index}", action="opened")
    service.seek_to_end()

    deliver(root, event="issues", delivery="new", action="opened")
    asyncio.run(service.apply(service._tailer.read()))
    assert starts.units == ["agentdeck"]


def test_seek_to_end_on_an_empty_stream_keeps_the_next_delivery(root, service, starts):
    service.seek_to_end()
    deliver(root, event="issues", delivery="first-ever", action="opened")
    asyncio.run(service.apply(service._tailer.read()))
    assert starts.units == ["agentdeck"]


def test_disabled_service_starts_no_task(root, poller_root, starts):
    service = PollerWakeService(root, poller_root, enabled=False, start_unit=starts)

    async def run() -> None:
        service.start()
        await asyncio.sleep(0)
        await service.stop()

    asyncio.run(run())
    assert starts.units == []


def test_the_loop_wakes_on_a_live_append(root, poller_root, starts, clock, monkeypatch):
    monkeypatch.setattr(webhook_tailer, "_WATCH_DEBOUNCE_MS", 20)
    monkeypatch.setattr(webhook_tailer, "_WATCH_STEP_MS", 10)
    service = PollerWakeService(root, poller_root, start_unit=starts, clock=clock)
    beat = heartbeat_path(root)

    async def run() -> None:
        service.start()
        # The loop seeks to the end before it watches, so the delivery has to land
        # after it is up — which is the whole point of the seek. The first heartbeat
        # is touched once the watch is open, just before the start-up read.
        for _ in range(1000):
            await asyncio.sleep(0.005)
            if beat.exists():
                break
        deliver(root, event="issues", delivery="live", action="opened")
        for _ in range(1000):
            await asyncio.sleep(0.005)
            if starts.units:
                break
        await service.stop()

    asyncio.run(run())
    assert starts.units == ["agentdeck"]


def test_the_watcher_touches_its_heartbeat_beside_the_tailer_state(root, poller_root, starts):
    """#1631: the heartbeat is the watcher's liveness proof, so it must appear as soon as
    the loop runs — and exactly where `heartbeat_path` says, because the poller's quiet
    skip derives the same path from the same streams root."""
    service = PollerWakeService(root, poller_root, start_unit=starts)
    beat = heartbeat_path(root)
    assert beat == root.parent / "webhook-tailers" / "poller-wake.heartbeat"
    assert not beat.exists()

    async def run() -> None:
        service.start()
        for _ in range(200):
            await asyncio.sleep(0.05)
            if beat.exists():
                break
        await service.stop()

    asyncio.run(run())
    assert beat.exists()


def test_a_disabled_watcher_writes_no_heartbeat(root, poller_root, starts):
    """Disabled means no liveness claim at all: the pollers it would vouch for keep
    their full 30-second cadence."""
    service = PollerWakeService(root, poller_root, enabled=False, start_unit=starts)

    async def run() -> None:
        service.start()
        await asyncio.sleep(0.2)
        await service.stop()

    asyncio.run(run())
    assert not heartbeat_path(root).exists()
