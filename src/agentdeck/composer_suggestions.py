"""Provider-aware command and skill suggestions for the chat composer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .models import Account

_FRONTMATTER_FIELD = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$")
_VALID_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")
_MAX_METADATA_BYTES = 16 * 1024


@dataclass(frozen=True)
class ComposerSuggestion:
    value: str
    label: str
    description: str
    kind: str


_CLAUDE_SDK_COMMANDS = (
    ComposerSuggestion(
        "/compact", "/compact", "Summarize the conversation to free context", "command"
    ),
    ComposerSuggestion(
        "/context", "/context", "Show what is using the context window", "command"
    ),
    ComposerSuggestion(
        "/usage", "/usage", "Show current plan usage and limits", "command"
    ),
)


def _metadata(path: Path) -> dict[str, str]:
    """Read the small, single-line part of Markdown YAML frontmatter we need."""
    try:
        text = path.read_bytes()[:_MAX_METADATA_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = _FRONTMATTER_FIELD.match(line)
        if match and match.group(1) in {"name", "description"}:
            result[match.group(1)] = match.group(2).strip(" '\"")
    return result


def _project_roots(cwd: Path | None) -> tuple[Path, ...]:
    if cwd is None:
        return ()
    try:
        current = cwd.expanduser().resolve()
    except (OSError, RuntimeError):
        current = cwd.expanduser()
    roots = []
    for candidate in (current, *current.parents):
        roots.append(candidate)
        if (candidate / ".git").exists():
            break
        if candidate == Path.home() or len(roots) >= 12:
            break
    return tuple(roots)


def _skill_files(root: Path) -> tuple[Path, ...]:
    """Find normal and one-level namespaced skills without an unbounded walk."""
    if not root.is_dir():
        return ()
    return tuple(sorted({*root.glob("*/SKILL.md"), *root.glob("*/*/SKILL.md")}))


def _skill_roots(account: Account, cwd: Path | None) -> tuple[Path, ...]:
    project = _project_roots(cwd)
    if account.provider_id == "claude_code":
        return tuple(candidate / ".claude" / "skills" for candidate in project) + (
            Path(account.root) / "skills",
        )
    if account.provider_id == "codex":
        return tuple(
            path
            for candidate in project
            for path in (candidate / ".agents" / "skills", candidate / ".codex" / "skills")
        ) + (
            Path(account.root) / "skills",
            Path(account.root).parent / ".agents" / "skills",
        )
    return ()


def _custom_commands(
    account_root: Path, cwd: Path | None
) -> tuple[ComposerSuggestion, ...]:
    suggestions = []
    roots = tuple(project / ".claude" / "commands" for project in _project_roots(cwd))
    for root in (*roots, account_root / "commands"):
        if not root.is_dir():
            continue
        paths = {*root.glob("*.md"), *root.glob("*/*.md")}
        for path in sorted(paths):
            try:
                relative = path.relative_to(root).with_suffix("")
            except ValueError:
                continue
            name = ":".join(relative.parts)
            if not _VALID_NAME.fullmatch(name):
                continue
            meta = _metadata(path)
            value = f"/{name}"
            suggestions.append(
                ComposerSuggestion(
                    value, value, meta.get("description", "Project command"), "command"
                )
            )
    return tuple(suggestions)


def composer_suggestions(
    account: Account, cwd: Path | None
) -> tuple[ComposerSuggestion, ...]:
    """Commands and installed skills that can be invoked in this session."""
    candidates: list[ComposerSuggestion] = []
    if account.provider_id == "claude_code":
        candidates.extend(_custom_commands(Path(account.root), cwd))
        prefix = "/"
    elif account.provider_id == "codex":
        prefix = "$"
    else:
        return ()

    for root in _skill_roots(account, cwd):
        for path in _skill_files(root):
            meta = _metadata(path)
            name = meta.get("name") or path.parent.name
            if not _VALID_NAME.fullmatch(name):
                continue
            value = f"{prefix}{name}"
            candidates.append(
                ComposerSuggestion(
                    value,
                    value,
                    meta.get("description", "Installed skill"),
                    "skill",
                )
            )
    if account.provider_id == "claude_code":
        candidates.extend(_CLAUDE_SDK_COMMANDS)

    # Nearest project definitions win over user-level and built-in duplicates.
    unique: dict[str, ComposerSuggestion] = {}
    for suggestion in candidates:
        unique.setdefault(suggestion.value.casefold(), suggestion)
    return tuple(sorted(unique.values(), key=lambda item: (item.value.casefold(), item.kind)))
