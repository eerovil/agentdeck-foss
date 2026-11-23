"""What a merged change touches, for the live check after it (#4500)."""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import time
from pathlib import Path

import pytest
from public_copy import shipped

from agentdeck import live_check_scope
from agentdeck.live_check_scope import (
    POLLER_CHECK,
    RUNTIME,
    SERVICES,
    SKILLS_INSTALL,
    WEB,
    docs_only,
    loading_processes,
    touches_skills,
)

ROOT = Path(__file__).resolve().parents[1]
DECK = "eerovil/agentdeck"


@pytest.mark.parametrize(
    ("repository", "files", "expected"),
    [
        (DECK, ["docs/x.md"], True),
        (DECK, ["tests/test_x.py", ".claude/codebase/x.md", "README.md"], True),
        (DECK, ["AGENTS.md", "CLAUDE.md", "docs/adr/0001.md"], True),
        ("EeroVil/AgentDeck", ["docs/x.md"], True),
        # The running deck reads it, so a Markdown file is not docs by its suffix.
        (DECK, ["poller/builder/PLAYBOOK.md"], False),
        (DECK, ["src/agentdeck/foo.md"], False),
        # The deploy installs these into every agent.
        (DECK, ["skills/dream/SKILL.md"], False),
        (DECK, ["docs/agents/lessons.md"], False),
        (DECK, ["scripts/install-skills.sh"], False),
        # The deploy runs these before it selects a commit: a failing one blocks it.
        (DECK, ["tests/test_poller_engine.py"], False),
        # The running deck serves these from its own checkout.
        (DECK, ["docs/diagrams/work-turn-endings.md"], False),
        (DECK, [".claude/skills/x/SKILL.md"], False),
        (DECK, [".claude/commands/x.md"], False),
        (DECK, ["docs/x.md", "src/agentdeck/app.py"], False),
        (DECK, [], False),
        # Another repository's README or docs may be what it serves.
        ("eerovil/site", ["README.md"], False),
        ("eerovil/site", ["docs/x.md"], False),
        ("eerovil/site", ["tests/x.py", ".claude/x.md", "AGENTS.md"], True),
    ],
)
def test_docs_only_is_a_fixed_list(repository, files, expected):
    assert docs_only(repository, files) is expected


def test_touches_skills_and_the_deploy():
    assert touches_skills(["src/a.py", "docs/agents/lessons.md"])
    assert not touches_skills(["docs/agents/domain.md"])
    for path in ("scripts/deploy-live.sh", "systemd/agentdeck.service", "poller/manifests/x"):
        assert live_check_scope.touches_deploy([path]), path
    assert not live_check_scope.touches_deploy(["src/agentdeck/app.py", "scripts/deploy.py"])


def _deploy_pattern(function: str) -> str:
    """The ``grep -Eq`` pattern of ``deploy-live.sh``'s ``function``."""
    text = (ROOT / "scripts" / "deploy-live.sh").read_text()
    body = text[text.index(f"{function}()") :]
    return re.search(r"grep -Eq '([^']+)'", body).group(1)


@shipped("scripts/deploy-live.sh")
@pytest.mark.parametrize(
    ("function", "pattern"),
    [("needs_skills_install", SKILLS_INSTALL), ("needs_poller_check", POLLER_CHECK)],
)
def test_the_deploys_own_patterns_are_the_ones_read(function, pattern):
    """A file the deploy installs or tests before it selects a commit is never docs-only."""
    assert pattern.pattern == _deploy_pattern(function)


#: Each docs-only path the deck's own code names, and why the running deck does not read
#: it at runtime. A new one fails: if the deck now reads that file, take it out of the
#: docs-only list in ``live_check_scope``; if it does not, add it here with its reason.
ALLOWED = {
    # A throwaway folder's files, which the check writes and has the CLI read.
    "scripts/check_locked_chat.py": {"CLAUDE.md"},
    # Which tests a change runs: the suite picker, never the running deck.
    "scripts/select_suite.py": {
        ".claude/*",
        "docs/*",
        "tests/",
        "tests/*",
        "tests/conftest.py",
        "tests/test_ruff_clean.py",
    },
    # Policy files an agent's approval is cached against; read from a worker's checkout.
    "src/agentdeck/approval_cache.py": {"AGENTS.md", "CLAUDE.md"},
    # Which changed files a light review leaves out, by name.
    "src/agentdeck/light_review.py": {"README.md", "docs/"},
    # The list itself.
    "src/agentdeck/live_check_scope.py": {
        ".claude/",
        "AGENTS.md",
        "CHANGELOG.md",
        "CLAUDE.md",
        "CONTEXT.md",
        "README.md",
        "SETUP.md",
        "TODO.md",
        "docs/",
        "tests/",
    },
    # The guide in a locked chat's own folder, never this repository's.
    "src/agentdeck/providers/claude_code/locked.py": {"CLAUDE.md"},
    # Which changed files a no-code verdict allows, by name.
    "src/agentdeck/work_release.py": {"docs/"},
}

_MAYBE = re.compile(
    r"""["'](\./)?(tests|\.claude|docs|README|CHANGELOG|CONTEXT|SETUP|TODO|AGENTS|CLAUDE)\b"""
)


def _joined(node: ast.AST) -> list[str | None]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _joined(node.left) + _joined(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return [None]


def _named_paths(source: str) -> set[str]:
    """The docs-only paths ``source`` names, as a string or a ``Path`` joined with ``/``."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            names.append("/".join(part for part in _joined(node) if part))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) < 120 and " " not in node.value:
                names.append(node.value)
        for name in names:
            name = name.removeprefix("./").lstrip("/")
            if name and docs_only(DECK, [name]):
                found.add(name)
    return found


def test_the_deck_reads_no_docs_only_path_at_runtime():
    """A docs-only merge is told there is nothing to check live (#4500); that holds only
    while the deck's code reads none of those files."""
    named = {}
    for path in sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]):
        source = path.read_text()
        if _MAYBE.search(source) and (found := _named_paths(source)):
            named[str(path.relative_to(ROOT))] = found
    assert named == ALLOWED


# --- the processes that load a merge ---


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


#: Stands in for the deploy's import walk: prints the tree's own lists, so a merge that
#: changes a list is seen only when the walk runs on the merge commit's tree.
WALKER = """\
import sys, time
from pathlib import Path
args = sys.argv[1:]
services = args[:1] == ["--services"]
tree = Path(args[-1])
if (tree / "slow").exists():
    time.sleep(30)
print((tree / ("services.txt" if services else "runtime.txt")).read_text())
"""


@pytest.fixture
def repo(tmp_path):
    """A checkout whose origin has a commit the checkout has not fetched."""
    origin, checkout = tmp_path / "origin", tmp_path / "checkout"
    _git(tmp_path, "init", "-q", "-b", "master", str(origin))
    (origin / "scripts").mkdir()
    (origin / "scripts" / "runtime_paths.py").write_text(WALKER)
    (origin / "runtime.txt").write_text("src/agentdeck/runtime.py\n")
    (origin / "services.txt").write_text("src/agentdeck/work_release.py\n")
    for config in (("user.email", "t@example.com"), ("user.name", "t")):
        _git(origin, "config", *config)
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "base")
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    # The merge makes ``jobs.py`` runtime-loaded; the checkout has not fetched it.
    (origin / "runtime.txt").write_text("src/agentdeck/runtime.py\nsrc/agentdeck/jobs.py\n")
    _git(origin, "commit", "-qam", "merge")
    return checkout, _git(origin, "rev-parse", "HEAD"), tmp_path / "cache"


def _load(repo, files, **kwargs):
    checkout, sha, cache = repo
    kwargs.setdefault("environment", dict(os.environ))
    return asyncio.run(loading_processes(checkout, [(sha, files)], cache=cache, **kwargs))


def test_the_walk_runs_on_the_merge_commits_own_tree(repo):
    assert _load(repo, {"src/agentdeck/jobs.py"}) == {WEB, RUNTIME}
    assert _load(repo, {"src/agentdeck/work_release.py"}) == {WEB, SERVICES}
    assert _load(repo, {"src/agentdeck/app.py"}) == {WEB}
    assert list(repo[2].iterdir()) == [], "every scratch tree is removed"


def test_an_unread_walk_names_no_process(repo):
    checkout, _sha, cache = repo
    assert asyncio.run(loading_processes(checkout, [("", {"a"})], cache=cache)) is None
    assert asyncio.run(loading_processes(checkout, [], cache=cache)) is None
    unknown = [("f" * 40, {"a"})]
    run = loading_processes(checkout, unknown, cache=cache, environment=dict(os.environ))
    assert asyncio.run(run) is None
    assert list(cache.iterdir()) == [], "a failed walk leaves no scratch tree"


def test_a_walk_over_its_limit_is_killed_and_names_no_process(repo):
    checkout, _sha, cache = repo
    origin = checkout.parent / "origin"
    (origin / "slow").write_text("")
    _git(origin, "add", "slow")
    _git(origin, "commit", "-qm", "slow")
    sha = _git(origin, "rev-parse", "HEAD")
    started = time.monotonic()
    run = loading_processes(
        checkout, [(sha, {"a"})], cache=cache, limit=0.3, environment=dict(os.environ)
    )
    assert asyncio.run(run) is None
    assert time.monotonic() - started < 2
    assert list(cache.iterdir()) == []


def test_the_default_environment_is_the_hosts_github_one(repo, monkeypatch):
    from agentdeck import github_identity

    calls = []

    def environment():
        calls.append(1)
        return dict(os.environ)

    monkeypatch.setattr(github_identity, "github_git_environment", environment)
    checkout, sha, cache = repo
    assert asyncio.run(loading_processes(checkout, [(sha, {"x"})], cache=cache)) == {WEB}
    assert calls == [1]


def test_a_scratch_tree_left_by_a_killed_run_is_swept(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "f").write_text("")
    stale = time.time() - live_check_scope.STALE_S - 60
    os.utime(old, (stale, stale))
    live_check_scope._sweep(tmp_path, time.time())
    assert [child.name for child in tmp_path.iterdir()] == ["new"]
    live_check_scope._sweep(tmp_path / "missing", time.time())
