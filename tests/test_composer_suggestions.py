from pathlib import Path

from agentdeck.composer_suggestions import composer_suggestions
from agentdeck.models import Account


def _skill(root: Path, directory: str, *, name: str, description: str) -> None:
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nInstructions.\n"
    )


def test_claude_suggestions_include_sdk_commands_project_commands_and_skills(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    account_root = tmp_path / "claude"
    _skill(account_root / "skills", "shared", name="shared", description="Shared skill")
    _skill(
        project / ".claude" / "skills",
        "project",
        name="project-skill",
        description="Project skill",
    )
    command = project / ".claude" / "commands" / "review" / "fast.md"
    command.parent.mkdir(parents=True)
    command.write_text("---\ndescription: Review the current diff\n---\n")
    user_command = account_root / "commands" / "handoff.md"
    user_command.parent.mkdir(parents=True)
    user_command.write_text("---\ndescription: Prepare a handoff\n---\n")
    account = Account("claude_code:test", "claude_code", "test", account_root)

    suggestions = composer_suggestions(account, project / "src")
    by_value = {item.value: item for item in suggestions}

    assert {"/compact", "/context", "/usage"} <= by_value.keys()
    assert by_value["/review:fast"].description == "Review the current diff"
    assert by_value["/handoff"].description == "Prepare a handoff"
    assert by_value["/project-skill"].kind == "skill"
    assert by_value["/shared"].description == "Shared skill"


def test_codex_skills_use_dollar_prefix_and_invalid_metadata_is_skipped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    account_root = tmp_path / "codex"
    _skill(
        project / ".agents" / "skills",
        "review",
        name="rate-my-pr",
        description="Review a pull request",
    )
    _skill(account_root / "skills", "bad", name="not a skill", description="Invalid")
    account = Account("codex:test", "codex", "test", account_root)

    suggestions = composer_suggestions(account, project)

    assert [(item.value, item.kind) for item in suggestions] == [
        ("$rate-my-pr", "skill")
    ]


def test_nearest_project_skill_wins_duplicate_name(tmp_path):
    project = tmp_path / "project"
    nested = project / "packages" / "app"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    _skill(project / ".claude" / "skills", "deploy", name="deploy", description="Root")
    _skill(nested / ".claude" / "skills", "deploy", name="deploy", description="Nearest")
    account = Account("claude_code:test", "claude_code", "test", tmp_path / "claude")

    suggestion = next(
        item
        for item in composer_suggestions(account, nested)
        if item.value == "/deploy"
    )

    assert suggestion.description == "Nearest"
