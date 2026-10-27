#!/usr/bin/env python3
"""Print the repository files the persistent runtime loads, one per line.

`scripts/deploy-live.sh` restarts the runtime only when a merge touches one of
these. The list used to be kept by hand and drifted: `jobs.py` was missing, so
#2826's spill to the dev cluster deployed as a web-only change and never went
live (#2835). Walking the imports instead means a new runtime module is covered
by the deploy that adds it.

Each root below is printed, and every ``agentdeck`` module its imports reach is
printed too, imports inside functions included, because the runtime imports
lazily. ``__main__.py`` is printed but not followed: it is also the web
server's entry point and imports the whole web app at the top, so following it
would restart the runtime for every merge.

Standard library only and no ``agentdeck`` import, so it reads whichever
checkout it is pointed at. Exits non-zero when ``runtime.py`` is missing or a
file does not parse; the deploy then assumes the runtime moved.

Usage: runtime_paths.py [REPO]   (defaults to this script's repository)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE = "agentdeck"
SRC = Path("src")

# Files the runtime loads that no import walk finds, or must not follow.
FIXED = (
    "pyproject.toml",
    "uv.lock",
    "systemd/agentdeck-codex.service",
    "src/agentdeck/__main__.py",
)

# Where the walk starts: the runtime itself, and the modules its workers run.
ROOTS = (
    "src/agentdeck/runtime.py",
    "src/agentdeck/providers/claude_code/restart.py",
    "src/agentdeck/providers/claude_code/usage.py",
    "src/agentdeck/providers/claude_code/worker.py",
    "src/agentdeck/providers/codex/appserver.py",
    "src/agentdeck/providers/codex/runtime_client.py",
    "src/agentdeck/providers/codex/worker.py",
    "src/agentdeck/providers/delivery.py",
    "src/agentdeck/providers/instructions.py",
    "src/agentdeck/providers/usage.py",
)


def _module_file(repo: Path, dotted: str) -> Path | None:
    """The file a dotted ``agentdeck`` module name lives in, if it exists."""
    parts = dotted.split(".")
    if not parts or parts[0] != PACKAGE:
        return None
    base = repo / SRC / Path(*parts)
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _package_of(repo: Path, path: Path) -> list[str]:
    """The dotted package a file sits in, as parts: ``agentdeck/x/y.py`` -> x."""
    # A package's own __init__ sits in that package, like any module beside it.
    return list(path.relative_to(repo / SRC).parts[:-1])


def _imports(repo: Path, path: Path) -> set[str]:
    """Every dotted ``agentdeck`` name ``path`` imports, at any depth."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package_of(repo, path)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level > len(package):
                    continue
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            names.add(base)
            # `from . import x` and `from .pkg import mod` may name submodules.
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return {name for name in names if name == PACKAGE or name.startswith(PACKAGE + ".")}


def runtime_paths(repo: Path) -> list[str]:
    seen: set[Path] = set()
    if not (repo / ROOTS[0]).is_file():
        raise FileNotFoundError(f"{ROOTS[0]} is missing from {repo}")
    # The other roots are reached from the runtime anyway; naming them only keeps
    # a lazily imported worker module listed if that import ever moves.
    pending = [repo / root for root in ROOTS if (repo / root).is_file()]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _imports(repo, path):
            parts = name.split(".")
            # Importing a.b.c runs every package __init__ on the way down.
            for depth in range(1, len(parts) + 1):
                found = _module_file(repo, ".".join(parts[:depth]))
                if found is not None and found not in seen:
                    pending.append(found)
    walked = {str(path.relative_to(repo)) for path in seen}
    return sorted(walked | set(FIXED))


def main(argv: list[str]) -> int:
    repo = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    try:
        paths = runtime_paths(repo.resolve())
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"runtime_paths: {exc}", file=sys.stderr)
        return 1
    print("\n".join(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
