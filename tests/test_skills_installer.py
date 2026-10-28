"""The bundled-skill installer: every flavour, every account's home, retirements,
and the managed block that carries /dream's general lessons into every agent."""

import os
import stat
import subprocess
from pathlib import Path

from public_copy import shipped

from agentdeck.composer_suggestions import composer_suggestions
from agentdeck.models import Account

ROOT = Path(__file__).parents[1]
PROVIDER_FLAVOURS = ("claude", "codex", "cursor")
LESSONS = ROOT / "docs" / "agents" / "lessons.md"
START = "<!-- agentdeck-lessons:start -->"
END = "<!-- agentdeck-lessons:end -->"


def install(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Run the installer against throwaway homes: (claude main, claude alt, codex home)."""
    claude_main = tmp_path / "claude-main" / "skills"
    claude_alt = tmp_path / "claude-alt" / "skills"
    codex_home = tmp_path / "codex"
    for root in (claude_main, claude_alt, codex_home / "skills"):
        root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CLAUDE_SKILL_DIRS": f"{claude_main} {claude_alt}",
        "CODEX_HOME": str(codex_home),
        "CURSOR_SKILL_DIRS": str(tmp_path / "cursor" / "skills"),
    }
    subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-skills.sh")],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return claude_main.parent, claude_alt.parent, codex_home


def test_every_bundled_skill_ships_every_provider_flavour():
    # A missing flavour is not a neutral gap: a Cursor chat asked for /dream and,
    # finding no Cursor skill, grepped the repo for the word and then followed the
    # *Claude* adapter, binding SPAWN_AGENT to tools Cursor does not have (#768).
    # Every directory here, with no exemption: ChatGPT flavours of these skills
    # exist, but they live in eerovil/review-chats under skills/, in ChatGPT's own
    # format. Nothing installs a skill into ChatGPT, so there is no fourth flavour
    # for this tree to carry and no reason for a directory here to ship fewer than
    # three.
    for skill in sorted(path.name for path in (ROOT / "skills").iterdir() if path.is_dir()):
        for provider in PROVIDER_FLAVOURS:
            assert (ROOT / "skills" / skill / provider / "SKILL.md").is_file(), (
                f"{skill} has no {provider} flavour"
            )


@shipped("skills/dream/DREAM.md")
def test_skill_installer_installs_dream_for_every_provider_account(tmp_path, monkeypatch):
    claude_main = tmp_path / "claude-main" / "skills"
    claude_alt = tmp_path / "claude-alt" / "skills"
    codex_home = tmp_path / "codex"
    # Cursor's skill root is the one that does not exist before the first
    # install, so it is deliberately left uncreated here.
    cursor_home = tmp_path / "home" / ".cursor"
    cursor_skills = cursor_home / "skills"
    claude_main.mkdir(parents=True)
    claude_alt.mkdir(parents=True)
    (codex_home / "skills").mkdir(parents=True)
    cursor_home.mkdir(parents=True)
    # The old /dream (#2421): an installed copy must not outlive its source.
    for root in (claude_main, claude_alt, codex_home / "skills", cursor_skills):
        (root / "codebase-memory").mkdir(parents=True)
        (root / "codebase-memory" / "REVIEW.md").write_text("retired")
    # The upstream /wayfinder, copied in by hand (#3621): ours replaces it, extras and all.
    (claude_main / "wayfinder" / "agents").mkdir(parents=True)
    (claude_main / "wayfinder" / "agents" / "openai.yaml").write_text("upstream")
    (claude_main / "wayfinder" / "SKILL.md").write_text("upstream")
    (claude_main / "grilling" / "agents").mkdir(parents=True)
    (claude_main / "grilling" / "SKILL.md").write_text("upstream")
    env = {
        **os.environ,
        "CLAUDE_SKILL_DIRS": f"{claude_main} {claude_alt}",
        "CODEX_HOME": str(codex_home),
        "CURSOR_SKILL_DIRS": str(cursor_skills),
    }

    subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-skills.sh")],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert (claude_main / "dream" / "SKILL.md").is_file()
    assert (claude_alt / "dream" / "SKILL.md").is_file()
    assert (codex_home / "skills" / "dream" / "SKILL.md").is_file()
    assert (cursor_skills / "dream" / "SKILL.md").is_file()
    for root in (claude_main, claude_alt, codex_home / "skills", cursor_skills):
        assert not (root / "codebase-memory").exists()
        assert (root / "wayfinder" / "WAYFINDER.md").is_file()
    assert not (claude_main / "wayfinder" / "agents").exists()
    assert "WAYFINDER.md" in (claude_main / "wayfinder" / "SKILL.md").read_text()
    assert not (claude_main / "grilling" / "agents").exists()
    assert "GRILLING.md" in (claude_main / "grilling" / "SKILL.md").read_text()
    assert (claude_main / "dream" / "dream.py").is_file()
    assert (claude_main / "dream" / "DREAM.md").is_file()
    assert not (cursor_home / "skills-cursor").exists()

    claude_account = Account("claude_code:test", "claude_code", "test", claude_main.parent)
    codex_account = Account("codex:test", "codex", "test", codex_home)
    monkeypatch.setenv("HOME", str(cursor_home.parent))
    assert "/dream" in {
        suggestion.value for suggestion in composer_suggestions(claude_account, None)
    }
    assert "$dream" in {
        suggestion.value for suggestion in composer_suggestions(codex_account, None)
    }


@shipped("skills/issue/PROCEDURE.md")
def test_skill_installer_repoints_issue_and_retires_old_trailhead_controllers(tmp_path):
    claude_main = tmp_path / "claude-main" / "skills"
    claude_alt = tmp_path / "claude-alt" / "skills"
    codex_home = tmp_path / "codex"
    cursor_home = tmp_path / "cursor"
    for root in (claude_main, claude_alt):
        for retired in ("kanban-dispatch", "kanban-poller", "kanban-worker"):
            path = root / retired
            path.mkdir(parents=True)
            (path / "stale.py").write_text("retired")
    (codex_home / "skills").mkdir(parents=True)
    cursor_home.mkdir(parents=True)
    env = {
        **os.environ,
        "CLAUDE_SKILL_DIRS": f"{claude_main} {claude_alt}",
        "CODEX_HOME": str(codex_home),
        "CURSOR_SKILL_DIRS": str(cursor_home / "skills"),
    }

    subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-skills.sh")],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    for root in (claude_main, claude_alt):
        assert (root / "issue" / "SKILL.md").is_file()
        assert not any(
            (root / retired).exists()
            for retired in ("kanban-dispatch", "kanban-poller", "kanban-worker")
        )
    assert (codex_home / "skills" / "issue" / "SKILL.md").is_file()
    assert (cursor_home / "skills" / "issue" / "SKILL.md").is_file()


def test_a_provider_that_is_not_set_up_on_this_host_is_skipped(tmp_path):
    # The guard is on the provider's home, not on its skills root — but a host
    # with no such provider at all must still be left untouched, not seeded with
    # a skill directory for an agent it does not have.
    nowhere = tmp_path / "nowhere"
    roots = {
        "CLAUDE_SKILL_DIRS": nowhere / ".claude" / "skills",
        "CODEX_HOME": nowhere / ".codex",
        "CURSOR_SKILL_DIRS": nowhere / ".cursor" / "skills",
    }
    env = {**os.environ, **{key: str(path) for key, path in roots.items()}}

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install-skills.sh")],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert not nowhere.exists()
    for path in roots.values():
        assert f"skip: {path}" in result.stdout or f"skip: {path}/skills" in result.stdout


@shipped("docs/agents/lessons.md")
def test_the_general_lessons_reach_every_claude_home_and_codex(tmp_path):
    main, alt, codex = install(tmp_path)
    lessons = LESSONS.read_text().strip()

    for target in (main / "CLAUDE.md", alt / "CLAUDE.md", codex / "AGENTS.md"):
        text = target.read_text()
        assert f"{START}\n{lessons}\n{END}" in text


@shipped("docs/agents/lessons.md")
def test_the_lessons_block_is_replaced_and_hand_written_rules_survive(tmp_path):
    main = tmp_path / "claude-main"
    main.mkdir()
    claude_md = main / "CLAUDE.md"
    claude_md.write_text(f"Always grill me.\n\n{START}\nstale rule\n{END}\n\nRespond in English.\n")
    claude_md.chmod(0o600)

    install(tmp_path)
    install(tmp_path)

    text = claude_md.read_text()
    assert text.count(START) == 1
    assert "stale rule" not in text
    assert text.startswith("Always grill me.")
    assert text.rstrip().endswith("Respond in English.")
    assert stat.S_IMODE(claude_md.stat().st_mode) == 0o600


@shipped("docs/agents/lessons.md")
def test_the_general_lessons_stay_under_their_cap():
    # They go into every prompt on the host; dream.py check-lessons holds the same line.
    assert len(LESSONS.read_bytes()) <= 2048
