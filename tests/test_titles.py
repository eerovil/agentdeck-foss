from agentdeck.models import Session, SessionStatus
from agentdeck.titles import normalize_agent_title, title_evidence_signature


def _session(**overrides):
    values = dict(
        key="codex:test:sid",
        account_key="codex:test",
        session_id="sid",
        status=SessionStatus.IDLE,
        title="Native",
        initial_prompt="Fix title handling",
        last_prompt="Keep it concise",
    )
    values.update(overrides)
    return Session(**values)


def test_agent_title_is_cleaned_capped_and_signed():
    session = _session()
    assert normalize_agent_title(session, '  "Clear title"  ') == "Clear title"
    assert normalize_agent_title(session, "") is None
    assert normalize_agent_title(session, "x" * 100).endswith("…")
    assert title_evidence_signature(session) == title_evidence_signature(session)


def test_agent_title_drops_prefix_that_app_state_reattaches(tmp_path):
    session = _session(
        cwd=tmp_path / "agentdeck" / ".worktrees" / "issue-246",
        issue_url="https://github.com/eerovil/agentdeck/issues/246",
    )
    assert (
        normalize_agent_title(
            session, "agentdeck#246 · Replace Deckhand models (review)"
        )
        == "Replace Deckhand models"
    )


def test_a_chat_not_scanned_yet_still_loses_an_issue_prefix():
    """No session yet means no known issue or project (#2627): strip the issue shape only."""
    assert normalize_agent_title(None, "agentdeck#2627 · Fix it (review)") == "Fix it"
    assert normalize_agent_title(None, "agentdeck · Fix it") == "agentdeck · Fix it"
    assert normalize_agent_title(None, "  ") is None


def test_generated_display_title_memo_still_follows_a_session_that_changes(tmp_path):
    """#1303: the two regexes here ran over every session on every refresh pass
    and every SSE tick, almost always re-deriving an answer already held. The
    memo is keyed on everything the formatting reads, so a chat that gains a
    project, an issue, or a kanban mode still re-renders."""
    from agentdeck.models import IssueRef
    from agentdeck.state import generated_display_title

    plain = _session(cwd=None)
    assert generated_display_title(plain, "Cut idle CPU") == "Cut idle CPU"

    checkout = tmp_path / "agentdeck"
    (checkout / ".git").mkdir(parents=True)
    in_project = _session(cwd=checkout)
    assert generated_display_title(in_project, "Cut idle CPU") == "agentdeck · Cut idle CPU"
    # The project prefix is stripped before it is re-attached, exactly once.
    assert (
        generated_display_title(in_project, "agentdeck · Cut idle CPU")
        == "agentdeck · Cut idle CPU"
    )

    carded = _session(cwd=checkout, issues=(IssueRef("eerovil/agentdeck", 1303),))
    assert (
        generated_display_title(carded, "Cut idle CPU") == "agentdeck#1303 · Cut idle CPU"
    )
    # Same session key, same semantic title, different native title: the kanban
    # mode suffix rides on `session.title`, so it has to be part of the key.
    reviewing = _session(
        cwd=checkout,
        issues=(IssueRef("eerovil/agentdeck", 1303),),
        title="agentdeck#1303 (review)",
    )
    assert generated_display_title(reviewing, "Cut idle CPU") != generated_display_title(
        carded, "Cut idle CPU"
    )
