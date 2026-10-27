"""Editing a Poller Instance's host settings from the deck (#901).

Only one thing here is editable so far: the ordered agent-account configuration for a project's
workers. The primary is authoritative now that model usage no longer reroutes work (#1497);
fallback entries remain editable for compatibility with already-issued directives and older
engines. Until #901, changing either required opening a shell and hand-editing the file.

Three facts shape the whole module:

* **The Overlay is the only place the engine looks.** ``[worker] account`` and
  ``fallback_accounts`` live in ``instances/<id>/overlay.toml`` and nowhere else, so a settings
  table in the deck's own database would be a setting the poller never reads. The edit has to
  land in that file.
* **An Overlay edit needs no re-activation.** ``config.fingerprint_of`` deliberately excludes the
  Overlay, so the next wake simply loads the new value. Nothing here has to re-approve anything.
* **The file is hand-written, and its comments are the operator's log.** The live instance's
  Overlay explains, in prose, why the account was switched away from ``main`` and back again, and
  why ``codex:codex`` must not be listed. Re-serializing the file would erase that history. So
  this writes *lines*, not TOML: the two keys it owns change, and every other byte survives.

The write is also reversible. After replacing the file it re-parses the result the way the engine
will; if that would not load, the original bytes go back and the caller is told why. A poller
whose Overlay this left broken would fail every wake, which is a far worse outcome than a refused
button.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .poller_engine.config import Report, parse_overlay

# A comment this module owns and rewrites. Anything else in the file is the operator's and is
# never touched.
MARKER = "# Set from AgentDeck"


class SettingsError(Exception):
    """The edit was refused, with a sentence an operator can act on."""


@dataclass(frozen=True)
class WorkerAccounts:
    """One instance's worker account order as the page shows it."""

    instance: str
    editable: bool = False        # is there an Overlay here that we could write?
    account: str = ""             # the primary — the account a wake dispatches on first
    fallbacks: tuple[str, ...] = ()
    ceilings: dict[str, float] = field(default_factory=dict)
    note: str = ""                # why it is not editable, when it is not

    @property
    def order(self) -> tuple[str, ...]:
        """The dispatch order the engine will build: the primary always leads it."""
        return tuple(key for key in dict.fromkeys([self.account, *self.fallbacks]) if key)


def overlay_path(root: str | Path, instance: str) -> Path:
    return Path(root).expanduser() / "instances" / instance / "overlay.toml"


def read_worker_accounts(root: str | Path, instance: str) -> WorkerAccounts:
    """What this instance's Overlay says today, or why we cannot offer to change it."""
    path = overlay_path(root, instance)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return WorkerAccounts(instance=instance, note="no overlay.toml on this host")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return WorkerAccounts(instance=instance, note=f"overlay.toml could not be read: {exc}")

    worker = raw.get("worker")
    if not isinstance(worker, dict):
        worker = {}
    account = worker.get("account")
    account = account if isinstance(account, str) else ""
    fallbacks = worker.get("fallback_accounts")
    if not isinstance(fallbacks, list):
        fallbacks = []
    ceilings = worker.get("usage_ceilings")
    if not isinstance(ceilings, dict):
        ceilings = {}
    return WorkerAccounts(
        instance=instance,
        editable=os.access(path, os.W_OK),
        account=account,
        fallbacks=tuple(key for key in fallbacks if isinstance(key, str) and key),
        ceilings={
            str(key): float(value)
            for key, value in ceilings.items()
            if isinstance(value, int | float)
        },
        note="" if os.access(path, os.W_OK) else "overlay.toml is not writable by the deck",
    )


def set_worker_accounts(
    root: str | Path,
    instance: str,
    *,
    account: str,
    fallbacks: Iterable[str] = (),
    by: str = "operator",
    now: datetime | None = None,
) -> WorkerAccounts:
    """Set the primary account and retain a compatibility fallback order.

    Raises ``SettingsError`` — with the reason — rather than leaving a half-written Overlay.
    """
    account = (account or "").strip()
    if not account:
        raise SettingsError("choose the account the workers should run on")

    path = overlay_path(root, instance)
    try:
        original = path.read_text()
    except OSError as exc:
        raise SettingsError(f"overlay.toml could not be read: {exc}") from exc

    try:
        raw = tomllib.loads(original)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"overlay.toml is not valid TOML: {exc}") from exc

    order = tuple(
        key for key in dict.fromkeys([account, *(key.strip() for key in fallbacks)]) if key
    )
    rest = tuple(key for key in order if key != account)

    lines = original.splitlines(keepends=True)
    lines = _drop_marker(lines, "worker")
    lines = _set_key(lines, "worker", "account", _render_string(account))
    lines = _set_key(lines, "worker", "fallback_accounts", _render_list(rest))

    # A per-account percentage may only name an account the order still offers — the engine's
    # own offline inspection refuses the Overlay otherwise, which would roll this whole edit
    # back — so an account leaving the order takes its entries with it. Both tables are pruned:
    # leaving `usage_targets` (#1965) behind would refuse the switch the operator just asked
    # for. Nothing else in the table is touched.
    worker = raw.get("worker") if isinstance(raw.get("worker"), dict) else {}
    for key in ("usage_ceilings", "usage_targets"):
        table = worker.get(key) if isinstance(worker, dict) else None
        if not isinstance(table, dict):
            continue
        kept = {name: value for name, value in table.items() if name in order}
        if kept != table:
            lines = _set_key(lines, "worker", key, _render_ceilings(kept))

    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%MZ")
    lines = _insert_before(lines, "worker", "account", f"{MARKER} on {stamp} by {by}.\n")

    updated = "".join(lines)
    _write_atomic(path, updated)

    problems = _problems(path)
    if problems:
        _write_atomic(path, original)
        raise SettingsError(f"the change was rolled back — it would not load: {problems}")

    after = read_worker_accounts(root, instance)
    if after.order != order:
        _write_atomic(path, original)
        raise SettingsError("the change was rolled back — the Overlay did not take the new order")
    return after


def _problems(path: Path) -> str:
    """Read the written file exactly as the engine will, and report anything it would refuse."""
    report = Report()
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return str(exc)
    parse_overlay(raw, report)
    return report.render()


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".deck-tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SettingsError(f"overlay.toml could not be written: {exc}") from exc


# --- line surgery -------------------------------------------------------------------------
#
# Small on purpose. A TOML round-trip library would be the general answer, but the two keys this
# owns are simple scalars and the cost of the general answer is the file's comments.


def _table_span(lines: Sequence[str], table: str) -> tuple[int, int] | None:
    """The half-open line range holding ``[table]``'s body, or nothing when it is absent."""
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("[") or not stripped.endswith("]"):
            continue
        if start is not None:
            return (start, index)
        if stripped == f"[{table}]":
            start = index + 1
    return (start, len(lines)) if start is not None else None


def _key_span(lines: Sequence[str], span: tuple[int, int], key: str) -> tuple[int, int] | None:
    """Where ``key = …`` lives inside ``span``, values spanning several lines included."""
    start, end = span
    for index in range(start, end):
        head = lines[index].lstrip()
        if not head.startswith(key):
            continue
        after = head[len(key):].lstrip()
        if not after.startswith("="):
            continue
        depth = 0
        for offset in range(index, end):
            depth += _bracket_delta(lines[offset])
            if depth <= 0:
                return (index, offset + 1)
        return (index, end)
    return None


def _bracket_delta(line: str) -> int:
    """Net open brackets on this line, ignoring anything quoted or commented."""
    depth = 0
    quote = ""
    for char in line:
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == "#":
            break
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return depth


def _set_key(lines: list[str], table: str, key: str, rendered: str) -> list[str]:
    """Replace ``key``'s line(s) in ``table``, adding the key — or the table — when missing."""
    line = f"{key} = {rendered}\n"
    span = _table_span(lines, table)
    if span is None:
        tail = lines[:]
        if tail and not tail[-1].endswith("\n"):
            tail[-1] += "\n"
        return [*tail, "\n", f"[{table}]\n", line]
    found = _key_span(lines, span, key)
    if found is not None:
        return [*lines[: found[0]], line, *lines[found[1]:]]
    at = _body_end(lines, span)
    return [*lines[:at], line, *lines[at:]]


def _insert_before(lines: list[str], table: str, key: str, line: str) -> list[str]:
    span = _table_span(lines, table)
    if span is None:
        return lines
    found = _key_span(lines, span, key)
    if found is None:
        return lines
    return [*lines[: found[0]], line, *lines[found[0]:]]


def _drop_marker(lines: list[str], table: str) -> list[str]:
    """Remove this module's own comment, so an edit leaves one stamp rather than a pile."""
    span = _table_span(lines, table)
    if span is None:
        return lines
    start, end = span
    kept = [
        line
        for index, line in enumerate(lines)
        if not (start <= index < end and line.strip().startswith(MARKER))
    ]
    return kept


def _body_end(lines: Sequence[str], span: tuple[int, int]) -> int:
    """The insertion point at the end of a table: after its last real line, before the blanks."""
    start, end = span
    at = end
    while at > start and not lines[at - 1].strip():
        at -= 1
    return at


def _render_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(_render_string(value) for value in values) + "]"


def _render_ceilings(ceilings: dict[str, float]) -> str:
    if not ceilings:
        return "{}"
    parts = []
    for key, value in ceilings.items():
        number = int(value) if float(value).is_integer() else value
        parts.append(f"{_render_string(str(key))} = {number}")
    return "{ " + ", ".join(parts) + " }"
