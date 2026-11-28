"""How big a repository's guide file is, for the warning a Work's start carries (#4724).

Every model call in a repository re-reads its guide: ``CLAUDE.md`` for Claude, ``AGENTS.md``
for Codex. Claude also loads the files a guide pulls in on ``@path`` lines; Codex does not.
One repository's guide had grown to 231k characters, 69k tokens in every call, and nothing
said so (#4714).
"""

from __future__ import annotations

import re
from pathlib import Path

#: Characters a guide may hold before a Work is told. Twice the 10k the oversized guides are
#: trimmed to; the guides nobody called oversized were 1k-16k on 2026-11-26.
GUIDE_LIMIT = 20_000
GUIDE_FILES = ("CLAUDE.md", "AGENTS.md")
#: An import: ``@path`` at the start of a word, wherever it stands. The deck does not tell
#: Markdown code from prose: a guide that shows an import of a file that exists has it
#: counted, so a size can read too high and never, within the two caps below, too low.
#: Copying a Markdown parser by hand got it wrong three reviews in a row (#4724).
_IMPORT = re.compile(r"(?:^|(?<=[\s`]))@([^\s`]+)", re.MULTILINE)
#: How many imports deep Claude Code follows.
_MAX_HOPS = 5
_MAX_FILES = 50


def _loaded(path: Path, hops: int = _MAX_HOPS) -> dict[Path, int]:
    """The size in characters of ``path`` and of every file it imports, up to ``hops`` deep,
    each once. A file that cannot be read, or a name that cannot be a path, counts nothing.

    Level by level, so a file is read at its shortest distance from ``path``: one met five
    deep on one chain and directly on another still has its own imports followed."""
    sizes: dict[Path, int] = {}
    level = [path]
    for depth in range(hops + 1):
        following: list[Path] = []
        for candidate in level:
            try:
                resolved = candidate.expanduser().resolve()
                if resolved in sizes or len(sizes) >= _MAX_FILES or not resolved.is_file():
                    continue
                text = resolved.read_text(encoding="utf-8", errors="replace")
            except (OSError, RuntimeError, ValueError):  # ``~nobody/x``, a NUL in the name
                continue
            sizes[resolved] = len(text)
            if depth < hops:
                for match in _IMPORT.finditer(text):
                    target = match.group(1)
                    following.append(
                        resolved.parent / target if target[:1] not in "~/" else Path(target)
                    )
        level = following
    return sizes


def oversized_guide(checkout: str | Path, limit: int = GUIDE_LIMIT) -> tuple[int, list[str]]:
    """The size of the checkout's biggest guide over ``limit`` and its files, biggest first.

    ``(0, [])`` when no guide is over it. A guide is ``CLAUDE.md`` with its imports, or
    ``AGENTS.md`` alone. The size is an upper bound: see ``_IMPORT``.
    """
    root = Path(checkout)
    worst: tuple[int, list[str]] = (0, [])
    for name in GUIDE_FILES:
        sizes = _loaded(root / name, _MAX_HOPS if name == "CLAUDE.md" else 0)
        total = sum(sizes.values())
        if total > max(limit, worst[0]):
            ordered = sorted(sizes, key=lambda path: -sizes[path])
            worst = (total, [_name(path, root) for path in ordered])
    return worst


def _name(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root.resolve()))
    except (OSError, ValueError):
        return str(path)
