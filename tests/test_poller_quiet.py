"""The host half of the webhook-quiet skip (#1631/#1673), against real streams.

Fixtures go through `WebhookLog`, the same appender the ingress route uses. Every test
holds the module to its one safety property: whatever cannot be proved — a missing or
stale heartbeat, a repository with no stream, an unreadable read — answers "not quiet",
and not quiet means the ordinary full 30-second wake.
"""

from __future__ import annotations

import json
import os

import pytest

from agentdeck import poller_quiet
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.poller_wake import heartbeat_path

AGENTDECK = Repository(owner="eerovil", name="agentdeck")
TRAILHEAD = Repository(owner="eerovil", name="trailhead")

SLUGS = [AGENTDECK.slug]


def deliver(
    root, repository: Repository = AGENTDECK, *, delivery: str, log: WebhookLog | None = None
) -> None:
    body = json.dumps(
        {
            "action": "opened",
            "repository": {
                "full_name": repository.slug,
                "name": repository.name,
                "owner": {"login": repository.owner},
            },
        }
    ).encode()
    (log or WebhookLog(root)).append(
        body=body, headers={"X-GitHub-Delivery": delivery, "X-GitHub-Event": "issues"}
    )


@pytest.fixture
def root(tmp_path):
    return tmp_path / "github-webhooks"


def beat(root, *, age_s: float = 0.0) -> None:
    """A watcher heartbeat, aged on demand."""
    path = heartbeat_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    if age_s:
        moment = path.stat().st_mtime - age_s
        os.utime(path, (moment, moment))


# --- the heartbeat -------------------------------------------------------------------


def test_a_missing_heartbeat_is_not_fresh(root):
    assert poller_quiet.heartbeat_fresh(root) is False


def test_a_recent_heartbeat_is_fresh(root):
    beat(root)
    assert poller_quiet.heartbeat_fresh(root) is True


def test_a_stale_heartbeat_is_a_dead_watcher(root):
    beat(root, age_s=poller_quiet.HEARTBEAT_STALE_S + 1)
    assert poller_quiet.heartbeat_fresh(root) is False


def test_a_heartbeat_from_the_future_is_clock_trouble_not_liveness(root):
    beat(root, age_s=-300)
    assert poller_quiet.heartbeat_fresh(root) is False


# --- new deliveries ------------------------------------------------------------------


def test_the_backlog_counts_as_new_and_a_committed_read_does_not(root):
    deliver(root, delivery="d-1")
    assert poller_quiet.new_deliveries("agentdeck", SLUGS, root) is True
    # The read committed, so the same delivery forces exactly one full wake.
    assert poller_quiet.new_deliveries("agentdeck", SLUGS, root) is False
    deliver(root, delivery="d-2")
    assert poller_quiet.new_deliveries("agentdeck", SLUGS, root) is True


def test_another_repositorys_delivery_is_not_this_instances_business(root):
    deliver(root, AGENTDECK, delivery="mine")
    deliver(root, TRAILHEAD, delivery="theirs")
    assert poller_quiet.new_deliveries("agentdeck", SLUGS, root) is True  # drains mine
    deliver(root, TRAILHEAD, delivery="more-theirs")
    assert poller_quiet.new_deliveries("agentdeck", SLUGS, root) is False


def test_a_repository_with_no_stream_at_all_forces_a_full_wake(root):
    # The agentdeck stream exists; the trailhead one has never received a delivery. Silence
    # from a stream that does not exist proves nothing, so the pair reads as not quiet —
    # which is also what keeps a host with no webhook ingress on full 30-second polling.
    deliver(root, AGENTDECK, delivery="d-1")
    poller_quiet.new_deliveries("agentdeck", SLUGS, root)  # drain and commit
    assert (
        poller_quiet.new_deliveries("agentdeck", [AGENTDECK.slug, TRAILHEAD.slug], root)
        is True
    )


def test_a_stream_is_matched_without_regard_to_case(root):
    """The manifest's spelling need not be the stream's (#1685).

    Streams are written under the owner and repository name GitHub sends. Lowercasing the
    requested slug looked for `northwind/store.jsonl`, which does not exist on a
    case-sensitive filesystem, so every repository with a capital letter in it silently
    kept full polling.
    """
    store = Repository(owner="Northwind", name="Store")
    deliver(root, store, delivery="d-1")

    assert poller_quiet.new_deliveries("trailhead", ["northwind/store"], root) is True
    # The read committed against the on-disk stream, so the same delivery does not repeat.
    assert poller_quiet.new_deliveries("trailhead", ["northwind/store"], root) is False
    # And the manifest's own spelling reaches the same stream and the same offsets.
    assert poller_quiet.new_deliveries("trailhead", [store.slug], root) is False


# --- why it is not quiet -------------------------------------------------------------


def test_a_missing_stream_is_named_and_a_case_difference_is_not(root):
    deliver(root, Repository(owner="Northwind", name="store"), delivery="d-1")
    beat(root)
    slugs = ["northwind/store", "Northwind/soi"]

    reason = poller_quiet.not_quiet_reason("trailhead", slugs, streams_root=root)
    assert reason is not None
    assert "Northwind/soi" in reason
    assert "northwind/store" not in reason


def test_every_not_quiet_answer_carries_its_reason(root):
    assert "heartbeat" in (
        poller_quiet.not_quiet_reason("agentdeck", SLUGS, streams_root=root) or ""
    )

    beat(root)
    assert "no usable repository" in (
        poller_quiet.not_quiet_reason("agentdeck", [], streams_root=root) or ""
    )

    deliver(root, delivery="d-1")
    assert "unread webhook deliveries" in (
        poller_quiet.not_quiet_reason("agentdeck", SLUGS, streams_root=root) or ""
    )
    assert poller_quiet.not_quiet_reason("agentdeck", SLUGS, streams_root=root) is None


def test_an_unchanged_reason_is_announced_once(root):
    reason = "no webhook stream for eerovil/trailhead"

    assert poller_quiet.reason_is_new("agentdeck", reason, root) is True
    assert poller_quiet.reason_is_new("agentdeck", reason, root) is False
    # Going quiet and back again is a change both ways, so the reason is said again.
    assert poller_quiet.reason_is_new("agentdeck", None, root) is True
    assert poller_quiet.reason_is_new("agentdeck", reason, root) is True
    # Another instance keeps its own note.
    assert poller_quiet.reason_is_new("trailhead", reason, root) is True


def test_no_usable_repositories_forces_a_full_wake(root):
    assert poller_quiet.new_deliveries("agentdeck", [], root) is True
    assert poller_quiet.new_deliveries("agentdeck", ["not-a-slug"], root) is True


def test_two_instances_judge_their_own_streams_independently(root):
    deliver(root, delivery="d-1")
    assert poller_quiet.new_deliveries("agentdeck", SLUGS, root) is True
    # The other instance's consumer has not read this delivery yet, so it still wakes full.
    assert poller_quiet.new_deliveries("trailhead", SLUGS, root) is True
    assert poller_quiet.new_deliveries("agentdeck", SLUGS, root) is False


# --- the whole quiet fact ------------------------------------------------------------


def test_quiet_needs_both_the_heartbeat_and_an_empty_read(root):
    deliver(root, delivery="d-1")
    poller_quiet.new_deliveries("agentdeck", SLUGS, root)  # drain the backlog

    assert poller_quiet.quiet("agentdeck", SLUGS, streams_root=root) is False  # no beat
    beat(root)
    assert poller_quiet.quiet("agentdeck", SLUGS, streams_root=root) is True
    deliver(root, delivery="d-2")
    assert poller_quiet.quiet("agentdeck", SLUGS, streams_root=root) is False
    assert poller_quiet.quiet("agentdeck", SLUGS, streams_root=root) is True


def test_a_dead_watcher_leaves_the_backlog_for_the_first_check_after_it_returns(root):
    deliver(root, delivery="d-1")
    poller_quiet.new_deliveries("agentdeck", SLUGS, root)

    beat(root, age_s=poller_quiet.HEARTBEAT_STALE_S + 1)
    deliver(root, delivery="while-dead")
    assert poller_quiet.quiet("agentdeck", SLUGS, streams_root=root) is False

    beat(root)
    # The delivery that landed while the watcher was down still forces one full wake.
    assert poller_quiet.quiet("agentdeck", SLUGS, streams_root=root) is False
    assert poller_quiet.quiet("agentdeck", SLUGS, streams_root=root) is True


# --- the wake script's environment ---------------------------------------------------


@pytest.fixture(scope="module")
def wake_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "poller_wake.py"
    spec = importlib.util.spec_from_file_location("poller_wake_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def instance(tmp_path):
    base = tmp_path / "instances" / "agentdeck"
    base.mkdir(parents=True)
    (base / "poller.toml").write_text(
        "contract = 1\n"
        '[board]\norg = "eerovil"\nproject_number = 1\n'
        '[[repos]]\nid = "agentdeck"\nholds_issues = true\nholds_code = true\n',
        encoding="utf-8",
    )
    (base / "overlay.toml").write_text(
        "contract = 1\n"
        f'[paths]\nstate = "{tmp_path / "state.json"}"\n'
        f'[checkouts]\nagentdeck = "{tmp_path / "checkout"}"\n',
        encoding="utf-8",
    )
    return base


def test_a_quiet_host_offers_the_engine_the_skip(wake_script, instance, root):
    from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

    deliver(root, delivery="d-1")
    poller_quiet.new_deliveries("agentdeck", SLUGS, root)  # drain the backlog
    beat(root)

    env = wake_script.wake_environment(
        "agentdeck", instance / "poller.toml", instance / "overlay.toml", streams_root=root
    )
    assert env.get(WEBHOOK_QUIET_ENV) == "1"


def test_an_unproved_host_offers_nothing(wake_script, instance, root):
    from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

    # No heartbeat, no streams: nothing is proved, so the environment stays plain.
    env = wake_script.wake_environment(
        "agentdeck", instance / "poller.toml", instance / "overlay.toml", streams_root=root
    )
    assert WEBHOOK_QUIET_ENV not in env


def test_the_knob_at_zero_skips_the_whole_check(wake_script, instance, root):
    from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

    manifest = instance / "poller.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\n[policy]\nwebhook_skip_s = 0\n",
        encoding="utf-8",
    )
    deliver(root, delivery="d-1")
    poller_quiet.new_deliveries("agentdeck", SLUGS, root)
    beat(root)

    env = wake_script.wake_environment(
        "agentdeck", manifest, instance / "overlay.toml", streams_root=root
    )
    assert WEBHOOK_QUIET_ENV not in env


def test_an_uninstalled_extension_does_not_disable_the_skip(wake_script, instance, root):
    """A manifest wart the running engine tolerates must not silently double the polling (#1680).

    `inspect` reports every declared extension it cannot find a validator for as a problem,
    which is what four of five instances on this host looked like — and what made the whole
    feature inert. The hint needs a readable manifest, not a spotless one.
    """
    from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

    manifest = instance / "poller.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + '\n[extensions.deploy-watch]\nrepo = "agentdeck"\n',
        encoding="utf-8",
    )
    from agentdeck.poller_engine import config as engine_config

    assert not engine_config.inspect(manifest, instance / "overlay.toml").ok

    deliver(root, delivery="d-1")
    poller_quiet.new_deliveries("agentdeck", SLUGS, root)  # drain the backlog
    beat(root)

    env = wake_script.wake_environment(
        "agentdeck", manifest, instance / "overlay.toml", streams_root=root
    )
    assert env.get(WEBHOOK_QUIET_ENV) == "1"


def test_a_manifest_naming_no_repositories_offers_nothing(wake_script, instance, root):
    """Without repos there is no stream to prove anything, so the plain environment stands."""
    from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

    manifest = instance / "poller.toml"
    manifest.write_text(
        "contract = 1\n" '[board]\norg = "eerovil"\nproject_number = 1\n', encoding="utf-8"
    )
    deliver(root, delivery="d-1")
    poller_quiet.new_deliveries("agentdeck", SLUGS, root)
    beat(root)

    env = wake_script.wake_environment(
        "agentdeck", manifest, instance / "overlay.toml", streams_root=root
    )
    assert WEBHOOK_QUIET_ENV not in env


def test_a_manifest_spelled_with_capitals_still_finds_its_streams(wake_script, instance, root):
    """#1685: the trailhead shape — a mixed-case owner, and its streams on disk."""
    from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

    store = Repository(owner="Northwind", name="store")
    manifest = instance / "poller.toml"
    manifest.write_text(
        "contract = 1\n"
        '[board]\norg = "Northwind"\nproject_number = 1\n'
        '[[repos]]\nid = "store"\nholds_issues = true\nholds_code = true\n',
        encoding="utf-8",
    )
    deliver(root, store, delivery="d-1")
    poller_quiet.new_deliveries("trailhead", [store.slug], root)  # drain the backlog
    beat(root)

    env = wake_script.wake_environment(
        "trailhead", manifest, instance / "overlay.toml", streams_root=root
    )
    assert env.get(WEBHOOK_QUIET_ENV) == "1"


def test_a_not_quiet_wake_says_why_in_the_cycle_log(wake_script, instance, root, capsys):
    """The wake's stderr is what `agentdeck-poller@.service` appends to `cycle.log`."""
    from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

    beat(root)  # a live watcher, but this instance's repository has no stream at all
    env = wake_script.wake_environment(
        "agentdeck", instance / "poller.toml", instance / "overlay.toml", streams_root=root
    )
    assert WEBHOOK_QUIET_ENV not in env
    said = capsys.readouterr().err
    assert "not quiet" in said
    assert "eerovil/agentdeck" in said

    # Said once: the wake runs every 30 seconds and the reason has not changed.
    wake_script.wake_environment(
        "agentdeck", instance / "poller.toml", instance / "overlay.toml", streams_root=root
    )
    assert capsys.readouterr().err == ""


def test_a_broken_manifest_degrades_to_a_full_wake(wake_script, instance, root):
    from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

    (instance / "poller.toml").write_text("not toml at all [", encoding="utf-8")
    beat(root)
    env = wake_script.wake_environment(
        "agentdeck", instance / "poller.toml", instance / "overlay.toml", streams_root=root
    )
    assert WEBHOOK_QUIET_ENV not in env


def test_an_evidence_repository_without_a_stream_does_not_disable_the_skip(
    wake_script, instance, root
):
    """#1791 gave every project an `agent-evidence` repository. None has a webhook.

    `_delivery_reason` correctly refuses to call a stream it cannot see quiet, so naming one
    turned "no stream for eerovil/agent-evidence" into a permanent not-quiet answer — and
    because the reason never changes it is logged once and never again. Five of seven live
    instances ran a full live wake every thirty-five seconds around the clock, with the fleet
    at 4,900 REST calls an hour against a 5,000 ceiling, and nothing said so.

    An artifacts-only repository holds screenshots a worker pushed. It never holds a card or a
    commit anyone reviews, so there is nothing on its stream for a wake to discover, and it has
    no business deciding whether this instance may trust the streams it does watch.
    """
    from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

    manifest = instance / "poller.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[[repos]]\nid = "agent-evidence"\nholds_artifacts = true\n',
        encoding="utf-8",
    )
    deliver(root, delivery="d-1")
    poller_quiet.new_deliveries("agentdeck", SLUGS, root)
    beat(root)

    env = wake_script.wake_environment(
        "agentdeck", manifest, instance / "overlay.toml", streams_root=root
    )
    assert env.get(WEBHOOK_QUIET_ENV) == "1"


def test_a_code_repository_without_a_stream_still_forces_a_full_wake(
    wake_script, instance, root
):
    """The filter is about what a repository *holds*, never about tidying away a missing hook.

    A repository this instance discovers work in still proves nothing when its stream is
    absent, and absence must keep costing a full wake — that is the whole safety of the
    feature.
    """
    from agentdeck.poller_engine.cycle import WEBHOOK_QUIET_ENV

    manifest = instance / "poller.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[[repos]]\nid = "second-code-repo"\nholds_code = true\n',
        encoding="utf-8",
    )
    deliver(root, delivery="d-1")
    poller_quiet.new_deliveries("agentdeck", SLUGS, root)
    beat(root)

    env = wake_script.wake_environment(
        "agentdeck", manifest, instance / "overlay.toml", streams_root=root
    )
    assert WEBHOOK_QUIET_ENV not in env


def test_a_delivery_swallowed_by_a_rotation_still_breaks_the_quiet(root):
    """#2754: the issue that filled the stream must still force a full wake."""
    log = WebhookLog(root, rotate_bytes=4_000, keep_rotations=3)
    deliver(root, delivery="seed")
    assert poller_quiet.new_deliveries("agentdeck", SLUGS, root) is True
    assert poller_quiet.new_deliveries("agentdeck", SLUGS, root) is False

    # The appender rotates the stream inside the append that fills it.
    archive = log.rotation_path(AGENTDECK, 1)
    index = 0
    while not archive.exists():
        deliver(root, delivery=f"d-{index}", log=log)
        index += 1
    assert log.path_for(AGENTDECK).stat().st_size == 0

    assert poller_quiet.new_deliveries("agentdeck", SLUGS, root) is True
