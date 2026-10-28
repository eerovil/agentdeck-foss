"""#182: the operations read model — what an instance said, and whether to believe it."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from agentdeck import poller_ops
from agentdeck.poller_engine import health as engine_health
from agentdeck.poller_engine.state import EnginePaths, StateStore
from agentdeck.poller_ops import InstanceView, read_all, read_instance, set_paused

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def _instance(tmp_path, name="reference", *, at=None, **state_parts):
    base = tmp_path / "instances" / name
    store = StateStore(EnginePaths.from_base(base / "state.json"))
    with store.transaction() as state:
        state.update(state_parts)
        report = engine_health.CycleReport(
            scans=1,
            queue_depth=len(state.get("controls") or {}),
            active_workers=sum(
                1 for card in (state.get("issues") or {}).values()
                if card.get("status") == "In progress"
            ),
        )
        for key, value in (state_parts.get("_report") or {}).items():
            setattr(report, key, value)
        engine_health.record_cycle(state, report, now=at or _NOW)
    return base


def _no_timer(command):
    return 1, ""


def _timer(active=True, next_wake="1780000000000000"):
    def runner(command):
        return 0, (
            f"ActiveState={'active' if active else 'inactive'}\n"
            f"NextElapseUSecRealtime={next_wake}\nResult=success"
        )

    return runner


# --- reading an instance ---

def test_an_instance_that_was_never_installed_reads_as_such(tmp_path):
    view = read_instance(tmp_path, "reference", runner=_no_timer)
    assert view.exists is False
    assert view.state_label == "not installed"
    assert view.summary == "not installed"


def test_a_healthy_instance_reads_as_ok_and_idle(tmp_path):
    _instance(tmp_path)
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer())
    assert view.exists is True
    assert view.state_label == "ok"
    assert view.believable is True
    assert view.summary == "ok"
    assert view.timer.firing is True


def test_a_dead_poller_never_reads_as_current(tmp_path):
    _instance(tmp_path, at=_NOW - timedelta(hours=2))
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer())
    # State and freshness are different questions, and the answer to the second is what matters.
    assert view.state_label == "stale"
    assert view.believable is False
    assert view.last_seen == "2h ago"
    assert "last seen 2h ago" in view.summary


def test_a_paused_instance_is_visibly_paused_rather_than_looking_dead(tmp_path):
    base = _instance(tmp_path)
    store = StateStore(EnginePaths.from_base(base / "state.json"))
    with store.transaction() as state:
        engine_health.set_paused(state, True, note="release freeze")
        engine_health.record_cycle(state, engine_health.CycleReport(), now=_NOW)
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer())
    assert view.paused is True
    assert view.state_label == "paused"
    assert view.believable is True          # it still wakes, so it stays fresh
    assert view.pause_note == "release freeze"
    assert "paused" in view.summary


def test_card_records_left_in_state_are_not_shown_as_work(tmp_path):
    """The poller holds no cards since #3732; an old record is history, not a worker."""
    _instance(tmp_path, issues={"store#5": {"status": "In progress", "blocked_by": ["store#4"]}})
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer())
    assert "working" not in view.summary and "blocked" not in view.summary


def test_a_degraded_instance_names_its_reasons(tmp_path):
    _instance(tmp_path, _report={"worker_backend_ok": False, "state_fallback": True})
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer())
    assert view.state_label == "degraded"
    assert "worker-delivery-degraded" in view.reasons
    assert "state-fallback" in view.reasons


def test_a_budget_hold_is_shown_without_being_called_ill_health(tmp_path):
    _instance(tmp_path, _report={"budget_holds": 2})
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer())
    assert view.budget_holding is True
    assert view.state_label == "ok"


def test_errors_and_the_last_failure_are_surfaced(tmp_path):
    _instance(tmp_path, _report={"errors": ["gh: server error 502"]})
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer())
    assert view.errors == ("gh: server error 502",)
    assert view.last_error == "gh: server error 502"


def test_a_failure_later_wakes_ran_past_is_dated_and_no_longer_current(tmp_path):
    """A weeks-old "Last failure" read as today's on the page (#2384)."""
    _instance(tmp_path, at=_NOW - timedelta(days=19), _report={"errors": ["drift"]})
    failing = read_instance(tmp_path, "reference", now=_NOW - timedelta(days=19), runner=_timer())
    assert failing.last_error_current is True
    assert failing.last_error_when == "0s ago"

    _instance(tmp_path)
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer())
    assert view.last_error == "drift"
    assert view.last_error_when == "19d ago"
    assert view.last_error_current is False


def test_the_version_surface_reads_the_record_a_single_apply_writes(tmp_path):
    """The page shows three facts from the record an apply writes (#2157)."""
    base = _instance(tmp_path)
    (base / "activation.json").write_text(
        '{"contract": 1, "activated_by": "owner", "config_fingerprint": "def456",'
        ' "binding_fingerprint": "b", "fingerprint": "d"}'
    )
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer())
    assert view.version == {"contract": 1, "config": "def456", "approved_by": "owner"}


def test_a_timer_systemd_cannot_answer_about_is_unknown_not_off(tmp_path):
    _instance(tmp_path)
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_no_timer)
    assert view.timer.known is False
    assert view.timer.firing is False       # unknown is not a claim that it is running


def test_a_stopped_timer_is_visible(tmp_path):
    _instance(tmp_path)
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer(active=False))
    assert view.timer.known is True
    assert view.timer.firing is False


def test_unreadable_durable_state_is_a_fact_not_a_broken_page(tmp_path):
    base = tmp_path / "instances" / "broken"
    base.mkdir(parents=True)
    (base / "state.json").write_text('{"schema_version": 99, "data": {}}')
    view = read_instance(tmp_path, "broken", runner=_no_timer)
    assert view.exists is True
    assert view.errors


def test_every_installed_instance_is_listed_in_name_order(tmp_path):
    _instance(tmp_path, "trailhead")
    _instance(tmp_path, "agentdeck")
    views = read_all(tmp_path, now=_NOW, runner=_timer())
    assert [view.instance for view in views] == ["agentdeck", "trailhead"]


def test_reading_a_root_that_does_not_exist_is_empty(tmp_path):
    assert read_all(tmp_path / "absent") == []


# --- controls: pause and resume write the state directly (#2596) ---

def _state(root):
    return json.loads((root / "instances" / "reference" / "state.json").read_text())


def test_pausing_takes_effect_immediately(tmp_path):
    _instance(tmp_path)
    set_paused(tmp_path, "reference", True, by="eero")
    view = read_instance(tmp_path, "reference", now=_NOW, runner=_timer())
    # The pause is durable state the next wake honours, and says who pressed it.
    assert view.paused is True
    assert "eero" in view.pause_note
    assert "operator_intents" not in json.dumps(_state(tmp_path))


def test_pausing_twice_is_still_one_pause(tmp_path):
    _instance(tmp_path)
    set_paused(tmp_path, "reference", True, by="eero")
    set_paused(tmp_path, "reference", True, by="eero")
    assert read_instance(tmp_path, "reference", now=_NOW, runner=_timer()).paused is True
    set_paused(tmp_path, "reference", False)
    assert read_instance(tmp_path, "reference", now=_NOW, runner=_timer()).paused is False


def test_resuming_clears_the_pause(tmp_path):
    _instance(tmp_path)
    set_paused(tmp_path, "reference", True)
    set_paused(tmp_path, "reference", False)
    assert read_instance(tmp_path, "reference", now=_NOW, runner=_timer()).paused is False


# --- the words an operator reads ---

def test_ages_are_rendered_in_units_a_person_scans():
    def view(seconds):
        return InstanceView(instance="x", exists=True, age_seconds=seconds).last_seen

    assert view(None) == "never"
    assert view(20) == "20s ago"
    assert view(600) == "10m ago"
    assert view(7200) == "2h ago"
    assert view(200000) == "2d ago"


# --- reusing a durable read while its files have not moved (#1362) ---

def _board_instance(tmp_path, cards, *, name="reference"):
    base = tmp_path / "instances" / name
    base.mkdir(parents=True, exist_ok=True)
    (base / "poller.toml").write_text(
        'contract = 1\n\n[board]\norg = "shop"\nproject_number = 7\n\n'
        '[[repos]]\nid = "store"\nholds_issues = true\n'
    )
    (base / "state.json").write_text(json.dumps({"issues": cards}))
    return base


def test_a_board_read_is_reused_until_the_durable_files_move(tmp_path):
    base = _board_instance(tmp_path, {"store#1": {"status": "In review"}})
    poller_ops.forget_durable_reads()

    first = poller_ops.read_board(tmp_path)
    second = poller_ops.read_board(tmp_path)
    assert second is first, "unchanged files must not be decoded twice"

    (base / "state.json").write_text(json.dumps({"issues": {"store#1": {"status": "Shipped"}}}))
    moved = poller_ops.read_board(tmp_path)
    assert moved is not first


def test_one_instance_writing_decodes_only_that_instance_again(tmp_path, monkeypatch):
    """#2841: each instance republishes on its own wake, so a fingerprint over all of them
    re-decoded every instance every few seconds. Only the one that moved is read again."""
    _board_instance(tmp_path, {"store#1": {"status": "In review"}}, name="first")
    second = _board_instance(tmp_path, {"store#2": {"status": "In review"}}, name="second")
    poller_ops.forget_durable_reads()
    whole = poller_ops.read_board(tmp_path)

    decoded: list[str] = []
    real = poller_ops._read_board_instance

    def counting(root, instance):
        decoded.append(instance)
        return real(root, instance)

    monkeypatch.setattr(poller_ops, "_read_board_instance", counting)
    (second / "state.json").write_text(json.dumps({"issues": {"store#2": {"status": "Done"}}}))
    moved = poller_ops.read_board(tmp_path)

    assert decoded == ["second"]
    assert moved.scopes[0] is whole.scopes[0]
    assert [scope.instance for scope in moved.scopes] == ["first", "second"]
    assert moved.complete


def test_a_new_instance_appearing_invalidates_the_reused_board(tmp_path):
    """The fingerprint covers which instances exist, not only what they contain."""
    _board_instance(tmp_path, {"store#1": {"status": "In review"}}, name="first")
    poller_ops.forget_durable_reads()
    assert len(poller_ops.read_board(tmp_path).scopes) == 1

    _board_instance(tmp_path, {"store#2": {"status": "In review"}}, name="second")
    assert len(poller_ops.read_board(tmp_path).scopes) == 2


def test_an_instance_whose_state_cannot_be_decoded_is_unread_not_empty(tmp_path):
    """The readability answer now comes from the load itself, not a second decode."""
    base = _board_instance(tmp_path, {"store#1": {"status": "In review"}})
    (base / "state.json").write_text("{ not json")
    poller_ops.forget_durable_reads()

    board = poller_ops.read_board(tmp_path)
    assert board.complete is False
    assert board.scopes[0].state_readable is False


def test_an_instance_that_has_never_run_still_reads_as_unread(tmp_path):
    base = _board_instance(tmp_path, {})
    (base / "state.json").unlink()
    poller_ops.forget_durable_reads()

    board = poller_ops.read_board(tmp_path)
    assert board.complete is False
    assert board.scopes[0].state_readable is False


def test_a_state_file_falling_back_to_its_backup_still_reads_as_a_board(tmp_path):
    base = _board_instance(tmp_path, {"store#1": {"status": "In review"}})
    (base / "state.json.bak").write_text((base / "state.json").read_text())
    (base / "state.json").write_text("{ not json")
    poller_ops.forget_durable_reads()

    board = poller_ops.read_board(tmp_path)
    assert board.complete is True
    assert board.scopes[0].state_readable is True


def test_a_scope_names_its_artifact_repository_and_borrowed_checkouts(tmp_path):
    """The Work workspace model labels these repositories from the manifest (#2143)."""
    import json

    from agentdeck import poller_ops

    base = tmp_path / "instances" / "trailhead"
    base.mkdir(parents=True)
    (base / "poller.toml").write_text(
        """
contract = 1

[board]
org = "shop"
project_number = 7

[[repos]]
id = "docker"
holds_code = true
verification = "agentdeck_job"

[[repos]]
id = "issues"
holds_issues = true
holds_artifacts = true
workspace = "docker"

[[repos]]
id = "other-owner/evidence"
holds_artifacts = true
"""
    )
    (base / "state.json").write_text(json.dumps({"issues": {}}))

    (scope,) = poller_ops.read_board(tmp_path).scopes
    assert scope.artifact_repositories == ("shop/issues", "other-owner/evidence")
    assert scope.borrowed_workspaces == (("shop/issues", "shop/docker"),)


def test_a_scope_carries_the_projects_spec_only_label(tmp_path):
    """The deck's New work form offers Spec only with the project's own label (#2415)."""
    import json

    from agentdeck import poller_ops

    for name, board, top in (
        (
            "deck",
            '[board]\norg = "eerovil"\nproject_number = 1\nspec_only_label = " spec-only "\n',
            "",
        ),
        ("plain", '[board]\norg = "eerovil"\nproject_number = 2\n', ""),
        ("labels", "", 'org = "eerovil"\nspec_only_label = "spec"\n'),
    ):
        base = tmp_path / "instances" / name
        base.mkdir(parents=True)
        (base / "poller.toml").write_text(
            f'contract = 1\n{top}{board}\n[[repos]]\nid = "{name}"\nholds_issues = true\n'
        )
        (base / "state.json").write_text(json.dumps({"issues": {}}))

    scopes = {scope.instance: scope for scope in poller_ops.read_board(tmp_path).scopes}
    assert scopes["deck"].spec_only_label == "spec-only"
    assert scopes["plain"].spec_only_label == ""
    # A boardless project authors its board facts at the top level (#1481).
    assert scopes["labels"].spec_only_label == "spec"


def test_issue_label_targets_read_the_manifests_alone(tmp_path, monkeypatch):
    """The `gh` shim asks this from a fresh process on every `gh issue create` (#2505), so it
    reads only the manifests: issue repositories with every alias, never the state."""
    for name, body in {
        "agentdeck": (
            '[board]\norg = "eerovil"\nownership_label = ["agent", "claude"]\n\n'
            '[[repos]]\nid = "agentdeck"\nholds_issues = true\n\n'
            '[[repos]]\nid = "agent-evidence"\nholds_artifacts = true\n'
        ),
        "bazaar": '[board]\norg = "acmecorp"\n\n[[repos]]\nid = "bazaar"\nholds_issues = true\n',
        "broken": "not = [toml",
    }.items():
        instance = tmp_path / "instances" / name
        instance.mkdir(parents=True)
        (instance / "poller.toml").write_text(body, encoding="utf-8")
        (instance / "state.json").write_text("{not json", encoding="utf-8")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("the label lookup must not read engine state")

    monkeypatch.setattr(poller_ops, "StateStore", unexpected)

    assert poller_ops.issue_label_targets(tmp_path) == (
        poller_ops.LabelTarget("eerovil/agentdeck", ("agent", "claude")),
        poller_ops.LabelTarget("acmecorp/bazaar", ("claude",)),
    )
