"""One spelling per host directory, so two records of the same tree compare equal (#1095).

On an ostree host — Fedora Silverblue and every other rpm-ostree layout — ``/home`` is a
symlink to ``var/home``, so the same worktree arrives spelled both ways depending on who
handed the path over. Recorded verbatim, one ``state.json`` ends up holding both spellings of
one directory, and anything that compares those strings later reads a live worktree as
unclaimed.

Every path this engine records or compares goes through :func:`resolve` first. It is the only
filesystem read the record-keeping does, and it buys the guarantee that string equality over
recorded paths means directory identity.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["resolve", "resolve_all"]


def resolve(path: str) -> str:
    """The comparable identity of a path: expanded, absolute, symlinks followed.

    A path that cannot be resolved — a broken link, a permission wall — degrades to its
    expanded spelling rather than raising. Two paths that fail to resolve still compare equal
    when they are spelled the same, which is the direction that keeps a directory.
    """
    expanded = Path(path).expanduser()
    try:
        return str(expanded.resolve())
    except (OSError, RuntimeError):
        return str(expanded)


def resolve_all(paths: object) -> list[str]:
    """Resolve a recorded list of paths, dropping non-strings and repeats, order preserved.

    Two spellings of one directory collapse to the first one seen, so a record written before
    resolution existed reads back as the single path it always meant.
    """
    if not isinstance(paths, list | tuple):
        return []
    seen: list[str] = []
    for path in paths:
        if not isinstance(path, str):
            continue
        resolved = resolve(path)
        if resolved not in seen:
            seen.append(resolved)
    return seen
