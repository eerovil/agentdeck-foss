"""Unit tests for provider-neutral model policy."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentdeck.models import (
    CONTROL_CAPABILITIES,
    Capability,
    InjectResult,
    IssueRef,
    Session,
    SessionProject,
    SessionStatus,
    resolve_session_project,
    runtime_control_capabilities,
    runtime_turn_state,
    session_project_from_cwd,
)


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/eerovil/agentdeck/issues/961",
        "eerovil/agentdeck#961",
    ],
)
def test_issue_ref_accepts_unambiguous_repository_scoped_forms(value):
    assert IssueRef.parse(value) == IssueRef("eerovil/agentdeck", 961)


@pytest.mark.parametrize(
    "value",
    ["#961", "961", "https://github.com/eerovil/agentdeck/pull/961"],
)
def test_issue_ref_rejects_ambiguous_or_pull_request_forms(value):
    assert IssueRef.parse(value) is None


@pytest.mark.parametrize("cwd", [Path("/tmp"), Path("/var/tmp")])
def test_temporary_root_is_not_a_session_project(cwd):
    assert session_project_from_cwd(cwd) is None


def test_named_directory_under_temporary_root_keeps_its_fallback_name():
    assert session_project_from_cwd(Path("/tmp/review-packet")) == "review-packet"


def test_repository_under_temporary_root_keeps_its_repository_project(tmp_path):
    repository = tmp_path / "agentdeck"
    (repository / ".git").mkdir(parents=True)

    assert session_project_from_cwd(repository / "src") == "agentdeck"


def test_repository_and_worktree_evidence_is_authoritative(tmp_path, monkeypatch):
    # pytest's tmp_path itself sits under /tmp; pretend it is ordinary disk here.
    monkeypatch.setattr("agentdeck.models._UNATTRIBUTABLE_CWD_ROOTS", frozenset())
    repository = tmp_path / "agentdeck"
    (repository / ".git").mkdir(parents=True)
    worktree = repository / ".worktrees" / "issue-689"
    worktree.mkdir(parents=True)

    assert resolve_session_project(repository / "src") == SessionProject("agentdeck", True)
    assert resolve_session_project(worktree) == SessionProject("agentdeck", True)


def test_a_checkout_under_a_temporary_root_is_never_authoritative(tmp_path, monkeypatch):
    """A /tmp/<name> merge probe carries a real .git, but its name names nobody's
    repository — remembering it filed spend under projects that do not exist (#671, #790)."""
    # Pin tmp_path as the unattributable root instead of assuming it is under /tmp.
    monkeypatch.setattr("agentdeck.models._UNATTRIBUTABLE_CWD_ROOTS", frozenset({tmp_path}))
    repository = tmp_path / "agentdeck-pr-batch-019fd2d7"
    (repository / ".git").mkdir(parents=True)
    worktree = repository / ".worktrees" / "probe"
    worktree.mkdir(parents=True)

    assert resolve_session_project(repository / "src") == SessionProject(
        "agentdeck-pr-batch-019fd2d7", False
    )
    assert resolve_session_project(worktree) == SessionProject(
        "agentdeck-pr-batch-019fd2d7", False
    )


@pytest.mark.parametrize(
    "cwd, expected",
    [
        (Path("/tmp/review-packet"), SessionProject("review-packet", False)),
        (Path("/tmp"), SessionProject(None, False)),
        (None, SessionProject(None, False)),
    ],
)
def test_directory_name_fallback_is_never_authoritative(cwd, expected):
    # Remembering a scratch directory's name would turn a guess into permanent
    # truth for every later scan, which is exactly what #689 must not do.
    assert resolve_session_project(cwd) == expected


def test_issue_url_names_the_project_authoritatively_from_a_temporary_root():
    session = Session(
        key="claude_code:main:s1",
        account_key="claude_code:main",
        session_id="s1",
        status=SessionStatus.LIVE,
        cwd=Path("/tmp"),
        issue_url="https://github.com/eerovil/agentdeck/issues/689",
    )

    assert session.project == SessionProject("agentdeck", True)
    assert session.project_name == "agentdeck"


def test_session_without_identifying_evidence_has_no_project():
    session = Session(
        key="claude_code:main:s2",
        account_key="claude_code:main",
        session_id="s2",
        status=SessionStatus.LIVE,
        cwd=Path("/tmp"),
    )

    assert session.project == SessionProject(None, False)


def test_unavailable_runtime_grants_no_control_capabilities():
    # An owned session whose runtime is unreachable is read-only: neither an
    # active turn nor a pending interaction can grant control while unavailable.
    assert (
        runtime_control_capabilities(
            available=False, active_turn=True, actionable_interaction=True
        )
        == frozenset()
    )


def test_available_idle_grants_inject_and_revive():
    assert runtime_control_capabilities(
        available=True, active_turn=False, actionable_interaction=False
    ) == frozenset({Capability.INJECT, Capability.REVIVE})


def test_active_turn_adds_steer_and_interrupt():
    assert runtime_control_capabilities(
        available=True, active_turn=True, actionable_interaction=False
    ) == frozenset(
        {
            Capability.INJECT,
            Capability.REVIVE,
            Capability.STEER,
            Capability.INTERRUPT,
        }
    )


def test_actionable_interaction_adds_interact():
    assert runtime_control_capabilities(
        available=True, active_turn=False, actionable_interaction=True
    ) == frozenset({Capability.INJECT, Capability.REVIVE, Capability.INTERACT})


def test_active_turn_and_interaction_together():
    assert runtime_control_capabilities(
        available=True, active_turn=True, actionable_interaction=True
    ) == CONTROL_CAPABILITIES - {
        Capability.PERMISSION_PROMPTS,
        Capability.BYPASS_PERMISSIONS,
        Capability.CONTEXT_COMPACT,
        Capability.CONTEXT_ROLLOVER,
        Capability.RESUME_ELSEWHERE,
    }


@pytest.mark.parametrize("active_turn", [False, True])
@pytest.mark.parametrize("actionable_interaction", [False, True])
def test_result_is_always_within_the_control_set(active_turn, actionable_interaction):
    # The policy never grants a read-only capability (TRANSCRIPT/DEEPLINK); those
    # are derived separately and merged by each provider.
    result = runtime_control_capabilities(
        available=True,
        active_turn=active_turn,
        actionable_interaction=actionable_interaction,
    )
    assert result <= CONTROL_CAPABILITIES
    assert Capability.TRANSCRIPT not in result
    assert Capability.DEEPLINK not in result


def test_actionable_interaction_outranks_stall_and_suppresses_thinking():
    # An answerable interaction overrides stall evidence and forces thinking off,
    # regardless of turn activity.
    assert runtime_turn_state(
        active_turn=True, actionable_interaction=True, stalled_evidence=True
    ) == (False, False)


def test_stall_evidence_shows_without_interaction():
    assert runtime_turn_state(
        active_turn=True, actionable_interaction=False, stalled_evidence=True
    ) == (True, False)


def test_thinking_requires_active_turn_no_interaction_and_not_stalled():
    assert runtime_turn_state(
        active_turn=True, actionable_interaction=False, stalled_evidence=False
    ) == (False, True)


def test_idle_turn_is_neither_stalled_nor_thinking():
    assert runtime_turn_state(
        active_turn=False, actionable_interaction=False, stalled_evidence=False
    ) == (False, False)


_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)








def test_control_set_excludes_read_only_capabilities():
    assert Capability.TRANSCRIPT not in CONTROL_CAPABILITIES
    assert Capability.DEEPLINK not in CONTROL_CAPABILITIES
    assert CONTROL_CAPABILITIES == frozenset(
        {
            Capability.INJECT,
            Capability.REVIVE,
            Capability.STEER,
            Capability.INTERRUPT,
            Capability.INTERACT,
            Capability.PERMISSION_PROMPTS,
            Capability.BYPASS_PERMISSIONS,
            Capability.CONTEXT_COMPACT,
            Capability.CONTEXT_ROLLOVER,
            Capability.RESUME_ELSEWHERE,
        }
    )


def test_pending_interaction_from_dict_round_trips_asdict():
    from dataclasses import asdict

    from agentdeck.models import (
        InteractionOption,
        InteractionQuestion,
        PendingInteraction,
    )

    x = PendingInteraction(
        id="int-1",
        kind="permission",
        thread_id="thread-9",
        turn_id="turn-2",
        title="Allow Bash?",
        message="run the tests",
        questions=(
            InteractionQuestion(
                id="q0",
                header="Pick",
                prompt="Which?",
                options=(
                    InteractionOption(label="A", description="first", value="a"),
                    InteractionOption(label="B"),
                ),
                allow_other=True,
                secret=True,
                multiselect=True,  # the field the Codex deserializer used to drop
            ),
        ),
        command="pytest -q",
        cwd="/repo",
        url="https://example/pr/1",
        decisions=("accept", "decline"),
    )
    # from_dict is the exact inverse of asdict — pins the round-trip and guards
    # future field additions from silently diverging deserializers.
    assert PendingInteraction.from_dict(asdict(x)) == x


def test_pending_interaction_from_dict_rejects_a_non_dict_or_missing_id():
    from agentdeck.models import PendingInteraction

    assert PendingInteraction.from_dict(None) is None
    assert PendingInteraction.from_dict({"kind": "question"}) is None  # no id
    assert PendingInteraction.from_dict({"id": 5}) is None  # non-string id


def test_inject_result_wire_round_trip_drops_web_only_field():
    # to_wire carries exactly the three socket fields; transcript_expected is
    # web-side only and must never cross the socket.
    result = InjectResult(True, reason=None, session_id="s-1", transcript_expected=False)
    assert result.to_wire() == {"accepted": True, "reason": None, "session_id": "s-1"}
    decoded = InjectResult.from_wire(result.to_wire())
    assert decoded == InjectResult(True, None, "s-1")  # transcript_expected back to default


def test_inject_result_from_wire_tolerates_malformed_replies():
    assert InjectResult.from_wire("nope", source="Codex runtime") == InjectResult(
        False, "invalid response from Codex runtime"
    )
    # Non-string reason/session_id degrade to None rather than leaking through.
    assert InjectResult.from_wire({"accepted": 1, "reason": 7, "session_id": []}) == InjectResult(
        True, None, None
    )


def test_resolved_project_is_remembered_per_cwd(tmp_path, monkeypatch):
    """The refresh loops ask this for every session several times a second, and
    every miss is a stat walk up the whole directory tree (#1303)."""
    monkeypatch.setattr("agentdeck.models._UNATTRIBUTABLE_CWD_ROOTS", frozenset())
    repository = tmp_path / "agentdeck"
    source = repository / "src"
    source.mkdir(parents=True)
    (repository / ".git").mkdir()

    walks = []
    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda self: (walks.append(self), real_exists(self))[1]
    )

    assert resolve_session_project(source) == SessionProject("agentdeck", True)
    walked_once = len(walks)
    assert walked_once  # the first resolution really did touch the filesystem
    for _ in range(50):
        assert resolve_session_project(source) == SessionProject("agentdeck", True)

    assert len(walks) == walked_once  # ...and the other fifty did not


def test_a_remembered_project_is_re_derived_once_its_entry_ages_out(tmp_path, monkeypatch):
    """A directory that gains a .git marker must not read as a bare name forever."""
    from agentdeck import models

    # Stop the walk at tmp_path so this host's own /tmp cannot answer for it.
    monkeypatch.setattr(models, "_UNATTRIBUTABLE_CWD_ROOTS", frozenset({tmp_path}))
    clock = [1000.0]
    monkeypatch.setattr(models, "monotonic", lambda: clock[0])
    checkout = tmp_path / "agentdeck"
    source = checkout / "src"
    source.mkdir(parents=True)

    assert session_project_from_cwd(source) == "src"
    (checkout / ".git").mkdir()
    # Still inside the window: the deck keeps the answer it already has.
    clock[0] += models._PROJECT_CACHE_TTL_S - 1
    assert session_project_from_cwd(source) == "src"

    clock[0] += 2
    assert session_project_from_cwd(source) == "agentdeck"


def test_remembered_projects_stay_bounded(tmp_path, monkeypatch):
    from agentdeck import models

    monkeypatch.setattr(models, "_PROJECT_CACHE_MAX", 8)
    for index in range(40):
        resolve_session_project(tmp_path / f"scratch-{index}")

    assert len(models._project_cache) == 8
    # The bound evicts the oldest, so the most recent cwds are the kept ones.
    assert (tmp_path / "scratch-39") in models._project_cache
    assert (tmp_path / "scratch-0") not in models._project_cache
