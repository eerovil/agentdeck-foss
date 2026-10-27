"""Shell commands that must stay with Claude's classifier (#2271).

Everything that can reach GitHub with this host's token, destroy work, or run
arbitrary code. ``providers/claude_code/permission_rules.py`` refuses any allow
rule that would pre-approve one of these, and the kanban command-capture hook
leaves a command that mentions one un-rewritten, because its rewrite answers
``allow`` and an ``allow`` skips the classifier.

Stdlib only: the capture hook imports this on every shell call.
"""

from __future__ import annotations

import shlex

GUARDED_COMMANDS: frozenset[str] = frozenset(
    {
        "gh",
        "curl",
        "wget",
        "http",
        "https",
        "xh",
        "rm",
        "find",
        "xargs",
        "ssh",
        "scp",
        "rsync",
        "kubectl",
        "sudo",
        "env",
        "eval",
        "exec",
        "source",
        ".",
        "bash",
        "sh",
        "zsh",
        "dash",
        "fish",
        "python",
        "python3",
        "node",
        "perl",
        "ruby",
    }
)

# ``git`` subcommands that stay with the classifier.
GUARDED_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"push", "remote", "config", "credential", "reset", "clean", "filter-branch", "filter-repo"}
)

# The programs whose mere mention sends a whole command to the classifier.
# Narrower than GUARDED_COMMANDS on purpose: a test run starts ``python`` and a
# cleanup runs ``rm``, and those are the classifier's to judge only when no
# allow rule covers them, which the capture hook cannot tell.
NETWORK_COMMANDS: frozenset[str] = frozenset(
    {"gh", "curl", "wget", "http", "https", "xh", "ssh", "scp", "rsync"}
)


def _words(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def reaches_github(command: str) -> bool:
    """Whether ``command`` mentions a network program or a guarded ``git``
    subcommand anywhere, including inside ``$(...)``, a pipe or a chain.

    Deliberately loose: ``grep gh notes.txt`` counts too, and so does a
    command that cannot be parsed. A false alarm only means the classifier
    looks at a harmless command; a miss would let a GitHub write through
    unreviewed.
    """
    words = _words(command)
    if words is None:
        return True
    # A quoted word can itself be a command (``bash -c "gh api …"``).
    if any(" " in word and reaches_github(word) for word in words if word != command):
        return True
    names = [word.lstrip("\\`$(").rsplit("/", 1)[-1].casefold() for word in words]
    if any(name in NETWORK_COMMANDS for name in names):
        return True
    if "git" in names:
        after = names[names.index("git") + 1 :]
        return any(name in GUARDED_GIT_SUBCOMMANDS for name in after)
    return False
