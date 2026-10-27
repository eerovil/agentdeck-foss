"""Which shell commands an owned Claude worker runs without asking the classifier.

Owned workers run in ``auto`` mode, where Claude's classifier reviews every
shell command before it runs. That review costs a round trip on each call and
refuses things nobody meant to stop. The commands below are everyday, local and
narrow, so they are pre-approved through ``permissions.allow`` in the settings
file the deck passes to every worker (#2271). Claude checks allow rules before
the classifier, keeps narrow rules when it enters auto mode, and approves a
chained command (``a && b``, ``a; b``, ``$(b)``) only when every part matches.

Everything that can reach GitHub with this host's token stays with the
classifier: ``gh``, ``curl``, ``git push`` and the rest of
``guarded_commands.GUARDED_COMMANDS``.
A rule configured under ``[claude_workers] classifier_skip_rules`` that would
let one of them through is dropped with a warning rather than installed.

The list protects against mistakes, not against a chat that sets out to get
around it: a worker can still write a test and run it with ``pytest``, or edit
a settings file. That was accepted when the card was specified.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from ...guarded_commands import GUARDED_COMMANDS, GUARDED_GIT_SUBCOMMANDS, reaches_github

log = logging.getLogger(__name__)

# Command prefixes, each installed as ``Bash(<prefix> *)``. The trailing `` *``
# form also matches the bare command (``git status`` with no arguments).
DEFAULT_SKIP_COMMANDS: tuple[str, ...] = (
    # Local git. Nothing here talks to a remote except fetch and pull, which
    # only read from it.
    "git add",
    "git commit",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git fetch",
    "git pull",
    "git rebase",
    "git merge",
    "git checkout",
    "git switch",
    "git branch",
    "git restore",
    "git stash",
    "git worktree",
    "git rev-parse",
    "git cherry-pick",
    "git ls-files",
    "git merge-base",
    # Tests and tooling.
    "pytest",
    "python -m pytest",
    "uv run pytest",
    "uv sync",
    "ruff",
    "mypy",
    "scripts/select_suite.py",
    "python scripts/select_suite.py",
    "~/agentdeck/.venv/bin/agentdeck job",
    "npm test",
    "npx playwright",
    # Files and text.
    "cd",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "diff",
    "grep",
    "rg",
    "sed",
    "awk",
    "jq",
    "mkdir",
    "cp",
    "mv",
    "touch",
    "echo",
    "printf",
    "date",
    "which",
    "pwd",
    "ps",
    "pgrep",
    "du",
    "df",
    "tee",
    "realpath",
    "basename",
    "dirname",
)

_BASH_RULE = re.compile(r"^\s*Bash\((?P<body>.*)\)\s*$", re.DOTALL)
_SHELL_PUNCTUATION = re.compile(r"[;&|$`'\"()<>{}\\\n\r]")


def default_rules() -> list[str]:
    return [f"Bash({prefix} *)" for prefix in DEFAULT_SKIP_COMMANDS]


def refusal(rule: str) -> str | None:
    """Why ``rule`` may not be installed, or None when it is narrow enough.

    Only ``Bash(...)`` rules are accepted. The command is read the way Claude
    reads a rule: its words up to a trailing ``*`` or ``:*``. Every word is
    checked, not just the first, so a wrapper (``timeout 5 gh``, ``FOO=1 gh``)
    cannot smuggle one in. Case is folded and a leading path is dropped, so
    ``Bash(/usr/bin/GH *)`` is still ``gh``. A narrow rule never needs shell
    punctuation, so any (``;``, ``&``, ``|``, ``$``, a backtick, quotes,
    brackets, a redirect) refuses it outright: ``echo x;gh api`` would otherwise
    hide ``gh`` inside one word.
    """
    match = _BASH_RULE.match(rule if isinstance(rule, str) else "")
    if match is None:
        return "only Bash(...) rules are accepted"
    body = match.group("body").strip()
    body = re.sub(r"(?::\*|\s*\*)$", "", body).strip()
    words = [word.rsplit("/", 1)[-1].casefold() for word in body.split()]
    if not words:
        return "a rule for every command"
    if _SHELL_PUNCTUATION.search(body) or reaches_github(body):
        return "shell punctuation or a GitHub-reaching command inside the rule"
    if any("*" in word for word in words):
        return "a wildcard inside the command"
    for index, word in enumerate(words):
        if word in GUARDED_COMMANDS:
            return f"{word} stays with the classifier"
        if word != "git":
            continue
        rest = words[index + 1 :]
        if not rest or rest[0].startswith("-"):
            return "a rule covering every git subcommand"
        if rest[0] in GUARDED_GIT_SUBCOMMANDS:
            return f"git {rest[0]} stays with the classifier"
    return None


def allow_rules(configured: Iterable[str] = ()) -> list[str]:
    """The built-in rules plus every configured rule that passes ``refusal``."""
    rules = default_rules()
    for rule in configured:
        reason = refusal(rule)
        if reason is not None:
            log.warning("ignoring claude_workers.classifier_skip_rules entry %r: %s", rule, reason)
            continue
        text = rule.strip()
        if text not in rules:
            rules.append(text)
    return rules
