"""The shared staging deck, backed by a copy of production's state (#2250, spec #2249).

``scripts/staging.sh`` is the only entry point; this module is what it runs. One shared
staging deck (``~/agentdeck-staging`` on :8757) gets a writable copy of production's state
on request, runs any branch or commit on it, and reports every Work's classification so a
change to the Work read model is seen against the real Work population before it merges.

* ``refresh``  copies production's history database, GitHub cache and poller files into
  staging's own paths, scrubbing what would make staging act on production's behalf.
* ``deploy``   checks a ref out on staging (``staging-checkout.sh``).
* ``works save|diff``  saves ``/api/works/population`` and compares two saves.
* ``status`` / ``release`` / ``fence``  the lease, the snapshot, and the write fence.

Production is only ever read. The fence is that every path staging can write resolves to a
staging-owned location, which ``check_isolation`` proves from both configs and both sets of
units before anything is copied or deployed, plus a ``gh`` on the staging web's ``PATH`` that
refuses. Nothing in the application itself asks whether it is staging.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

HOME = Path.home()
DATA = HOME / ".local" / "share" / "agentdeck"
CONFIG_DIR = HOME / ".config" / "agentdeck"
PROD_CONFIG = CONFIG_DIR / "config.toml"
STAGING_CONFIG = CONFIG_DIR / "config-staging.toml"
STAGING_DIR = Path(os.environ.get("AGENTDECK_STAGING_DIR", HOME / "agentdeck-staging"))
PROD_CHECKOUT = Path(os.environ.get("AGENTDECK_REPO", HOME / "agentdeck"))
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd" / "user"
LEASE_FILE = DATA / "staging-lease.json"
MANIFEST_FILE = DATA / "staging-snapshot.json"
WORKS_DIR = DATA / "staging-works"
STUB_DIR = DATA / "staging-bin"

PROD_UNITS = ("agentdeck.service", "agentdeck-codex.service")
STAGING_WEB_UNIT = "agentdeck-staging.service"
STAGING_RUNTIME_UNIT = "agentdeck-staging-codex.service"
STAGING_UNITS = (STAGING_WEB_UNIT, STAGING_RUNTIME_UNIT)
FENCE_DROPIN = "staging-fence.conf"
STAGING_GITHUB_CACHE = "%h/.cache/agentdeck-staging/github_metadata.sqlite3"

LEASE_TTL = timedelta(minutes=60)
STUB_REFUSAL = "staging: GitHub disabled"
# Tables that would make staging act on production's behalf: deliver production's
# scheduled messages, notify production's phones, or accept production's session tokens.
SCRUBBED_TABLES = ("scheduled_messages", "push_subscriptions", "push_vapid", "session_tokens")
# Copied from each poller instance, and nothing else from it: never `jobs/`, tokens,
# `card-control/`, `generations/`, `cycle.log` or turn files.
INSTANCE_FILES = ("poller.toml", "overlay.toml", "state.json", "controls.json", "activation.json")
INSTANCE_DIRS = ("dispatch-deferrals",)
# Overlay keys naming a program the staging web could start against a real account (the
# ChatGPT nudge press). The copy points them at a program that refuses rather than dropping
# them: the deck reads the reviewer's verdicts only for an instance that names a
# ``review_program`` (``poller_nudge.read_external_reviews``), so a dropped key would hide
# every verdict from staging's Work classification.
PROGRAM_KEYS = ("nudge_program", "review_program")
REFUSING_PROGRAM = STUB_DIR / "refuse"
# Config paths staging is meant to share with production: the agent accounts it monitors,
# and the nudge program, which is a program and not state.
SHARED_FIELDS = {"accounts", "chat_board.nudge_program"}
_PATH_NAME = re.compile(r"(^|_)(path|dir|file|root)$")
_WORKS_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class StagingError(RuntimeError):
    """A refusal: printed as it is, and the command exits non-zero."""


# --- the write fence -------------------------------------------------------------------------


def _expand_specifiers(value: str) -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    out, i = [], 0
    while i < len(value):
        if value[i] == "%" and i + 1 < len(value):
            spec = value[i + 1]
            out.append({"h": str(HOME), "t": runtime, "%": "%"}.get(spec, value[i : i + 2]))
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def unit_environment(unit: str, unit_dir: Path = UNIT_DIR) -> dict[str, str]:
    """The ``Environment=`` a user unit ends up with: its file, then its drop-ins in order."""
    files = [unit_dir / unit]
    dropins = unit_dir / f"{unit}.d"
    if dropins.is_dir():
        files += sorted(dropins.glob("*.conf"))
    env: dict[str, str] = {}
    for path in files:
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("Environment="):
                continue
            for item in shlex.split(line.removeprefix("Environment=")):
                key, sep, value = item.partition("=")
                if sep:
                    env[key] = _expand_specifiers(value)
    return env


def _runtime_dir(env: dict[str, str]) -> Path:
    runtime = env.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) if runtime else HOME / ".cache"


def _env_paths(units: tuple[str, ...], unit_dir: Path) -> dict[str, Path]:
    """The two process-environment paths the units decide, defaulted the way the code does."""
    found: dict[str, Path] = {}
    for unit in units:
        env = unit_environment(unit, unit_dir)
        cache = env.get("AGENTDECK_GITHUB_CACHE", "~/.cache/agentdeck/github_metadata.sqlite3")
        socket = env.get("AGENTDECK_CODEX_SOCKET") or str(
            _runtime_dir(env) / "agentdeck" / "codex-runtime.sock"
        )
        found[f"{unit}:AGENTDECK_GITHUB_CACHE"] = Path(cache).expanduser()
        found[f"{unit}:AGENTDECK_CODEX_SOCKET"] = Path(socket).expanduser()
    return found


def config_paths(config: BaseModel, prefix: str = "") -> dict[str, Path]:
    """Every resolved path field in a config, by dotted name.

    A field counts as a path by its name (``*_path``, ``*_dir``, ``*_file``, ``root``,
    ``file``), so a section added later is compared without anyone editing this. An empty
    ``usage.shared_cache_dir`` resolves the way the deck resolves it.
    """
    from .providers.usage import shared_cache_dir

    found: dict[str, Path] = {}
    for name in type(config).model_fields:
        dotted = f"{prefix}{name}"
        if dotted in SHARED_FIELDS:
            continue
        value = getattr(config, name)
        if isinstance(value, BaseModel):
            found.update(config_paths(value, f"{dotted}."))
        elif isinstance(value, str) and _PATH_NAME.search(name):
            if dotted == "usage.shared_cache_dir":
                found[dotted] = shared_cache_dir(value)
            elif value:
                found[dotted] = Path(value).expanduser()
    return found


def _overlaps(a: Path, b: Path) -> bool:
    a, b = a.resolve(strict=False), b.resolve(strict=False)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _gh_on(path_value: str) -> Path | None:
    found = shutil.which("gh", path=path_value)
    return Path(found) if found else None


def check_isolation(
    prod_config: BaseModel,
    staging_config: BaseModel,
    *,
    unit_dir: Path = UNIT_DIR,
    manager_path: str = "",
) -> tuple[list[str], list[str]]:
    """``(refusals, warnings)``: why staging could write production, and what to know.

    Refused: any staging path equal to, inside, or containing a production path; a staging
    GitHub cache or runtime socket shared with production; a staging web whose ``gh`` is not
    the refusing stub; and the settings that reach production on their own (the webhook
    wake, which starts production's poller units, the chat board and the issue refresh).
    """
    prod = {**config_paths(prod_config), **_env_paths(PROD_UNITS, unit_dir)}
    staging = {**config_paths(staging_config), **_env_paths(STAGING_UNITS, unit_dir)}
    refusals = [
        f"staging {s_name} = {s_path} overlaps production {p_name} = {p_path}"
        for s_name, s_path in sorted(staging.items())
        for p_name, p_path in sorted(prod.items())
        if _overlaps(s_path, p_path)
    ]
    web_env = unit_environment(STAGING_WEB_UNIT, unit_dir)
    # A unit without its own PATH runs with the service manager's.
    gh = _gh_on(web_env.get("PATH") or manager_path or os.environ.get("PATH", os.defpath))
    try:
        stubbed = gh is not None and STUB_REFUSAL in gh.read_text()
    except (OSError, UnicodeDecodeError):
        stubbed = False
    if not stubbed:
        refusals.append(
            f"the staging web's gh is {gh or 'missing'}, not the refusing stub:"
            " run `scripts/staging.sh fence`"
        )
    settings = {
        "github_webhook.enabled": (staging_config.github_webhook.enabled, False),
        "chat_board.enabled": (staging_config.chat_board.enabled, False),
        "poller.issue_refresh_interval_s": (staging_config.poller.issue_refresh_interval_s, 0),
    }
    for name, (value, wanted) in settings.items():
        if value != wanted:
            refusals.append(f"staging {name} is {value!r}; it must be {wanted!r}")
    warnings = []
    if staging_config.inject.enabled:
        warnings.append(
            "staging inject is ON: Send from staging reaches real chats. Turn it back off"
            " after the execution-canary test that needed it."
        )
    return refusals, warnings


def install_fence(
    *, unit_dir: Path = UNIT_DIR, stub_dir: Path = STUB_DIR, path_value: str
) -> list[Path]:
    """Write the refusing ``gh`` and the staging units' fence drop-ins; returns what it wrote.

    Drop-ins rather than the units themselves: the units are symlinked out of the staging
    worktree, so deploying an older commit would otherwise deploy an unfenced unit too.
    """
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "gh"
    stub.write_text(
        "#!/bin/sh\n"
        "# The staging web's gh (#2250): staging never writes to or reads from GitHub.\n"
        f'echo "{STUB_REFUSAL}" >&2\n'
        "exit 1\n"
    )
    stub.chmod(0o755)
    refuse = stub_dir / REFUSING_PROGRAM.name
    refuse.write_text(
        "#!/bin/sh\n"
        "# Staging's copy of each poller overlay names this as its ChatGPT nudge and review\n"
        "# program (#2250), so a press on staging never starts a run on a real account.\n"
        'echo "staging: no ChatGPT runs from staging" >&2\n'
        "exit 1\n"
    )
    refuse.chmod(0o755)
    entries = [
        str(stub_dir),
        *(p for p in path_value.split(os.pathsep) if p and p != str(stub_dir)),
    ]
    written = [stub, refuse]
    for unit, lines in {
        STAGING_WEB_UNIT: [
            f"Environment=AGENTDECK_GITHUB_CACHE={STAGING_GITHUB_CACHE}",
            f'Environment="PATH={os.pathsep.join(entries)}"',
        ],
        STAGING_RUNTIME_UNIT: [f"Environment=AGENTDECK_GITHUB_CACHE={STAGING_GITHUB_CACHE}"],
    }.items():
        directory = unit_dir / f"{unit}.d"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / FENCE_DROPIN
        path.write_text(
            "# Written by scripts/staging.sh fence (#2250). Staging's own GitHub cache, and a\n"
            "# gh that refuses on the web's PATH, whatever commit the staging worktree is on.\n"
            "[Service]\n" + "\n".join(lines) + "\n"
        )
        written.append(path)
    return written


# --- the copy ------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_entry(path: Path) -> dict[str, Any]:
    return {"path": str(path), "size": path.stat().st_size, "sha256": _sha256(path)}


def _remove_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def copy_sqlite(source: Path, target: Path, *, scrub: tuple[str, ...] = ()) -> dict[str, int]:
    """Online-backup ``source`` (opened read-only) to ``target``; returns rows scrubbed per table.

    The backup lands in a temporary file beside the target, is scrubbed with
    ``secure_delete`` so deleted rows do not survive in free pages, and replaces the target
    atomically after the target's own ``-wal``/``-shm`` are removed.
    """
    if not source.is_file():
        raise StagingError(f"no production database at {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.refresh-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    _remove_sidecars(temporary)
    scrubbed: dict[str, int] = {}
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(temporary)
        try:
            src.backup(dst)
            dst.execute("PRAGMA secure_delete = ON")
            present = {
                row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in scrub:
                if table not in present:
                    scrubbed[table] = 0
                    continue
                scrubbed[table] = dst.execute(f'DELETE FROM "{table}"').rowcount
            dst.commit()
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dst.close()
    finally:
        src.close()
    _remove_sidecars(temporary)
    _remove_sidecars(target)
    os.replace(temporary, target)
    return scrubbed


@contextlib.contextmanager
def instance_lock(base: Path) -> Iterator[None]:
    """The poller engine's own exclusive lock on an instance's state (``state.json.lock``)."""
    from .poller_engine.state import file_lock

    with file_lock(base / "state.json.lock"):
        yield


def _spellings(path: Path) -> list[str]:
    """How a path may be written in an overlay: absolute, and ``~/``-relative under home."""
    expanded = path.expanduser()
    found = [str(expanded), str(expanded.resolve(strict=False))]
    for home in {HOME, HOME.resolve(strict=False)}:
        with contextlib.suppress(ValueError):
            found.append("~/" + str(expanded.relative_to(home)))
    # Longest first, so a spelling is never rewritten through a shorter one inside it.
    return sorted(set(found), key=len, reverse=True)


def _strings(value: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                yield key, item
            else:
                yield from ((k, v) for k, v in _strings(item))
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _items(value: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _items(item)
    elif isinstance(value, list):
        for item in value:
            yield from _items(item)


def rewrite_overlay(
    text: str, prod_root: Path, staging_root: Path, program: Path = REFUSING_PROGRAM
) -> str:
    """Point an instance's TOML at the staging root, and its nudge programs at ``program``.

    Every spelling of production's poller root becomes the staging root, so ``state_file``
    names the copied reviewer file. The result is re-read, and anything still resolving
    under production's root, or a program key not pointing at ``program``, refuses the
    refresh.
    """
    for old in _spellings(prod_root):
        new = str(staging_root.expanduser())
        text = re.sub(re.escape(old) + r"(?=[/\"'])", lambda _m, new=new: new, text)
    replacements = {
        "nudge_program": json.dumps(str(program)),
        "review_program": json.dumps([str(program)]),
    }
    keyed = re.compile(r"^(\s*)(" + "|".join(PROGRAM_KEYS) + r")(\s*=).*$", re.MULTILINE)
    text = keyed.sub(lambda m: f"{m[1]}{m[2]}{m[3]} {replacements[m[2]]}", text)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise StagingError(f"rewritten overlay no longer parses: {exc}") from exc
    root = prod_root.expanduser().resolve(strict=False)
    for key, value in _strings(data):
        if value.startswith(("/", "~")) and Path(value).expanduser().resolve(
            strict=False
        ).is_relative_to(root):
            raise StagingError(f"overlay key {key} still points into production: {value}")
    for key, value in _items(data):
        if key in PROGRAM_KEYS and json.dumps(value) != replacements[key]:
            raise StagingError(f"overlay {key} still names a real program: {value!r}")
    return text


def _state_files(text: str) -> list[Path]:
    with contextlib.suppress(tomllib.TOMLDecodeError):
        return [
            Path(value).expanduser()
            for key, value in _strings(tomllib.loads(text))
            if key == "state_file" and value
        ]
    return []


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    shutil.copyfile(source, temporary)  # follows the generation symlinks
    os.replace(temporary, target)


class _Placement:
    """Where a production-root-relative path is copied during one refresh."""

    def __init__(self, staging_root: Path, fresh: Path) -> None:
        self.staging_root = staging_root
        self.fresh = fresh

    def __call__(self, relative: Path) -> Path:
        if relative.parts and relative.parts[0] == "instances":
            return self.fresh.joinpath(*relative.parts[1:])
        return self.staging_root / relative


def copy_instance(prod_root: Path, instance: str, place: _Placement) -> list[Path]:
    """Copy one poller instance while holding its lock; returns the files written.

    ``place`` maps a path relative to production's poller root to where its copy goes, so
    the whole copy can be built beside staging's live ``instances/`` and swapped in at once.
    """
    base = prod_root / "instances" / instance
    staging_base = place(Path("instances") / instance)
    written: list[Path] = []
    with instance_lock(base):
        texts: dict[str, str] = {}
        for name in INSTANCE_FILES:
            source = base / name
            if not source.is_file():
                continue
            if name.endswith(".toml"):
                texts[name] = source.read_text()
            else:
                _atomic_copy(source, staging_base / name)
                written.append(staging_base / name)
        for name in INSTANCE_DIRS:
            source = base / name
            if source.is_dir():
                shutil.copytree(source, staging_base / name, dirs_exist_ok=True)
                written.extend(p for p in (staging_base / name).rglob("*") if p.is_file())
        root = prod_root.resolve(strict=False)
        for text in texts.values():
            for state_file in _state_files(text):
                resolved = state_file.resolve(strict=False)
                if not resolved.is_relative_to(root):
                    # Staging writes press markers beside this file; outside the copied
                    # root it would be writing beside production's.
                    raise StagingError(
                        f"{instance}: state_file {state_file} is outside {prod_root};"
                        " staging cannot copy it"
                    )
                if not resolved.is_file():
                    continue
                target = place(resolved.relative_to(root))
                _atomic_copy(resolved, target)
                written.append(target)
        for name, text in texts.items():
            target = staging_base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rewrite_overlay(text, prod_root, place.staging_root))
            written.append(target)
    return written


def copy_poller(prod_root: Path, staging_root: Path) -> list[Path]:
    """Replace staging's ``instances/`` with a fresh copy of production's; returns the files.

    The copy is built beside the live directory and renamed into place, so a failed refresh
    leaves the previous copy whole.
    """
    prod_root, staging_root = prod_root.expanduser(), staging_root.expanduser()
    staging_root.mkdir(parents=True, exist_ok=True)
    fresh = staging_root / f".instances.refresh-{os.getpid()}"
    shutil.rmtree(fresh, ignore_errors=True)
    fresh.mkdir()
    place = _Placement(staging_root, fresh)
    instances = prod_root / "instances"
    written: list[Path] = []
    try:
        for base in sorted(instances.iterdir()) if instances.is_dir() else []:
            if base.is_dir():
                written += copy_instance(prod_root, base.name, place)
    except BaseException:
        shutil.rmtree(fresh, ignore_errors=True)
        raise
    current = staging_root / "instances"
    old = staging_root / f".instances.old-{os.getpid()}"
    if current.exists():
        current.rename(old)
    fresh.rename(current)
    shutil.rmtree(old, ignore_errors=True)
    return [current / p.relative_to(fresh) if p.is_relative_to(fresh) else p for p in written]


# --- the lease -----------------------------------------------------------------------------


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{path}.lock", "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _live(lease: dict | None, now: datetime) -> bool:
    expires = _parse_time((lease or {}).get("expires_at"))
    return expires is not None and expires > now


def describe_lease(lease: dict) -> str:
    who = lease.get("holder") or "someone"
    session = lease.get("session")
    ref = lease.get("ref")
    return (
        f"{who}{f' (session {session})' if session else ''}"
        f"{f' on {ref}' if ref else ''} until {lease.get('expires_at')}"
    )


def acquire_lease(
    path: Path,
    holder: str,
    *,
    session: str = "",
    ref: str = "",
    take: bool = False,
    now: datetime | None = None,
    ttl: timedelta = LEASE_TTL,
) -> dict:
    """Take or renew staging for ``holder``; another holder's unexpired lease refuses by name."""
    if not holder.strip():
        raise StagingError("a staging lease needs a holder")
    now = now or datetime.now(UTC)
    with _locked(path):
        current = _read_json(path)
        mine = current is not None and current.get("holder") == holder and _live(current, now)
        if current is not None and _live(current, now) and not mine and not take:
            raise StagingError(
                f"staging is leased to {describe_lease(current)}; pass --take to take it over"
            )
        kept = current if mine and current is not None else {}
        lease = {
            "holder": holder,
            "session": session or kept.get("session", ""),
            "ref": ref or kept.get("ref", ""),
            "started_at": kept.get("started_at") or now.isoformat(),
            "expires_at": (now + ttl).isoformat(),
        }
        _write_json(path, lease)
    return lease


def release_lease(
    path: Path, holder: str, *, take: bool = False, now: datetime | None = None
) -> bool:
    """Clear the lease if it is ``holder``'s, expired, or ``take`` says so."""
    now = now or datetime.now(UTC)
    with _locked(path):
        current = _read_json(path)
        if current is None:
            return False
        if current.get("holder") != holder and _live(current, now) and not take:
            raise StagingError(
                f"staging is leased to {describe_lease(current)}; pass --take to release it"
            )
        path.unlink(missing_ok=True)
    return True


def default_holder(cwd: Path) -> str:
    """The Work a worktree belongs to (``.worktrees/issue-<n>``), else the directory itself.

    A deck-started Work's worktree keeps its spare's name (``work-spare-<hex>``, #3590), so
    its ``agent/issue-<n>`` branch names the Work instead.
    """
    configured = os.environ.get("AGENTDECK_STAGING_HOLDER", "").strip()
    if configured:
        return configured
    match = re.search(r"/\.worktrees/issue-(\d+)(?:/|$)", str(cwd))
    if match is None and "/.worktrees/" in str(cwd):
        branch = subprocess.run(
            ["git", "-C", str(cwd), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        match = re.fullmatch(r"agent/issue-(\d+)", branch)
    if match:
        remote = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        slug = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", remote)
        if slug:
            return f"github_issue:{slug.group(1)}#{match.group(1)}"
    return str(cwd)


# --- the Work population ---------------------------------------------------------------------


def _short(work_id: str) -> str:
    return work_id.removeprefix("github_issue:")


def _line(row: dict) -> str:
    parts = [row.get("bucket") or row.get("section") or "?"]
    if row.get("reason"):
        parts.append(row["reason"])
    actions = row.get("actions") or []
    return " · ".join(parts) + (f" · [{', '.join(actions)}]" if actions else "")


COMPARED = ("section", "bucket", "attention", "reason", "actions")


def bucket_counts(rows: list[dict]) -> Counter:
    return Counter(row.get("bucket") or row.get("section") or "?" for row in rows)


def works_diff(before: dict, after: dict) -> str:
    """Bucket counts, then every Work whose bucket, attention, reason or actions changed."""
    a = {row["work_id"]: row for row in before.get("works", [])}
    b = {row["work_id"]: row for row in after.get("works", [])}
    lines = [
        f"Works: {len(a)} → {len(b)}"
        f"  ({before.get('name', 'before')} {str(before.get('commit', ''))[:8]}"
        f" → {after.get('name', 'after')} {str(after.get('commit', ''))[:8]})"
    ]
    snap_a, snap_b = before.get("snapshot") or {}, after.get("snapshot") or {}
    if snap_a.get("captured_end") != snap_b.get("captured_end"):
        lines.append(
            "WARNING: the two saves come from different refreshes, so production state moved"
            " between them as well as the code."
        )
    counts_a, counts_b = bucket_counts(list(a.values())), bucket_counts(list(b.values()))
    lines.append("Buckets:")
    for name in sorted(set(counts_a) | set(counts_b)):
        was, now = counts_a.get(name, 0), counts_b.get(name, 0)
        mark = "" if was == now else "   *"
        lines.append(f"  {name:<28} {was:>4} → {now:<4}{mark}")
    changed, unchanged = [], 0
    for work_id in sorted(set(a) | set(b)):
        old, new = a.get(work_id), b.get(work_id)
        if old is not None and new is not None:
            if all(old.get(key) == new.get(key) for key in COMPARED):
                unchanged += 1
                continue
        changed.append(f"  {_short(work_id)}")
        if old is not None:
            changed.append(f"    - {_line(old)}")
        if new is not None:
            changed.append(f"    + {_line(new)}")
    lines.append(f"Changed: {sum(1 for c in changed if not c.startswith('    '))}")
    lines.extend(changed)
    lines.append(f"Unchanged: {unchanged}")
    return "\n".join(lines)


def works_summary(saved: dict) -> str:
    snapshot = saved.get("snapshot") or {}
    rows = saved.get("works", [])
    lines = [
        f"Saved {saved.get('name')}: {len(rows)} Works on {str(saved.get('commit', ''))[:8]}"
        f" {saved.get('subject', '')}".rstrip(),
        f"Snapshot: refreshed {snapshot.get('captured_end', 'never')}"
        f" from production {str(snapshot.get('production_revision', '?'))[:8]}",
    ]
    for name, count in sorted(bucket_counts(rows).items()):
        lines.append(f"  {name:<28} {count:>4}")
    return "\n".join(lines)


def _save_path(name: str, directory: Path = WORKS_DIR) -> Path:
    """Where ``works save <name>`` writes: always a plain file directly in ``directory``."""
    if not _WORKS_NAME.fullmatch(name) or set(name) == {"."}:
        raise StagingError(f"a save name is letters, digits, '.', '_' or '-': {name!r}")
    target = directory / f"{name}.json"
    if target.resolve(strict=False).parent != directory.resolve(strict=False):
        raise StagingError(f"a save name must stay in {directory}: {name!r}")
    return target


def _saved_path(name: str, directory: Path = WORKS_DIR) -> Path:
    """What ``works diff`` reads: a save's name, or the path of an existing save file."""
    candidate = Path(name)
    if candidate.suffix == ".json" and candidate.is_file():
        return candidate
    return _save_path(name, directory)


# --- talking to staging and systemd ----------------------------------------------------------


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _systemctl(run: Runner, *args: str) -> None:
    done = run(["systemctl", "--user", *args])
    if done.returncode != 0:
        raise StagingError(f"systemctl --user {' '.join(args)} failed: {done.stderr.strip()}")


def _http_get(url: str, *, timeout: float = 30.0) -> Any:
    import httpx

    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def runtime_activity(socket_path: Path) -> dict | None:
    """The staging runtime's ``/activity``, or None when it is not running."""
    import httpx

    if not socket_path.exists():
        return None
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket_path)), timeout=5.0
        ) as client:
            response = client.get("http://runtime/activity")
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _served(answer: dict, expect: str | None) -> str | None:
    """Why ``answer`` came from other code than ``expect``, or None when it cannot tell that.

    A web process older than #2628 reports no ``revision`` and is not refused for it.
    """
    served = answer.get("revision")
    if not expect or not served or str(served).lower() == expect.lower():
        return None
    return f"staging is still serving {str(served)[:8]}, not {expect[:8]}"


def wait_healthy(
    port: int,
    *,
    expect: str | None = None,
    timeout_s: float = 120.0,
    fetch: Callable[[str], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> dict:
    """Ask ``/healthz`` until it answers, from ``expect``'s code when one is given.

    The first request also starts the socket-activated web. A reload keeps the old worker
    answering until the new one is ready, so any answer is not yet the new code (#2628).
    """
    url = f"http://127.0.0.1:{port}/healthz"
    fetch = fetch or (lambda target: _http_get(target, timeout=10.0))
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    stale: str | None = None
    while True:
        try:
            health = fetch(url)
        except Exception as exc:  # noqa: BLE001 - still starting; retried until the deadline
            last = exc
        else:
            stale = _served(health, expect)
            if stale is None:
                if expect and not health.get("revision"):
                    out(
                        "WARNING: staging's web does not report its revision (older than #2628);"
                        " not checked"
                    )
                return health
        if time.monotonic() >= deadline:
            break
        sleep(2.0)
    if stale:
        raise StagingError(f"{stale} after {timeout_s:.0f}s")
    raise StagingError(
        f"staging did not answer /healthz on :{port} within {timeout_s:.0f}s: {last}"
    )


def _git(*args: str, cwd: Path = STAGING_DIR) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    return done.stdout.strip() if done.returncode == 0 else ""


# --- the commands ----------------------------------------------------------------------------


@dataclass
class Context:
    holder: str
    session: str = ""
    take: bool = False
    prod_config_path: Path = PROD_CONFIG
    staging_config_path: Path = STAGING_CONFIG
    unit_dir: Path = UNIT_DIR
    run: Runner = _run
    out: Callable[[str], None] = lambda line: print(line, flush=True)
    warnings: list[str] = field(default_factory=list)

    def configs(self) -> tuple[Any, Any]:
        from .config import load_config

        return load_config(self.prod_config_path), load_config(self.staging_config_path)

    def lease(self, ref: str = "") -> dict:
        lease = acquire_lease(
            LEASE_FILE, self.holder, session=self.session, ref=ref, take=self.take
        )
        self.out(f"lease: {describe_lease(lease)}")
        return lease

    def manager_path(self) -> str:
        """The ``PATH`` the user service manager gives a unit that sets none."""
        manager = self.run(["systemctl", "--user", "show-environment"]).stdout
        return next(
            (
                line.removeprefix("PATH=")
                for line in manager.splitlines()
                if line.startswith("PATH=")
            ),
            os.environ.get("PATH", os.defpath),
        )

    def isolate(self) -> tuple[Any, Any]:
        prod, staging = self.configs()
        refusals, warnings = check_isolation(
            prod, staging, unit_dir=self.unit_dir, manager_path=self.manager_path()
        )
        for warning in warnings:
            self.out(f"WARNING: {warning}")
        if refusals:
            raise StagingError(
                "staging could write production state; refusing:\n  " + "\n  ".join(refusals)
            )
        return prod, staging


def refresh(ctx: Context) -> dict:
    """Copy production's state into staging (spec §1): poller, then the database, then the cache."""
    ctx.lease()
    socket = _env_paths((STAGING_RUNTIME_UNIT,), ctx.unit_dir)[
        f"{STAGING_RUNTIME_UNIT}:AGENTDECK_CODEX_SOCKET"
    ]
    activity = runtime_activity(socket)
    if activity and activity.get("active"):
        raise StagingError(
            f"the staging runtime has an active turn ({activity.get('active_turns')});"
            " replacing its database would orphan it. Try again when it is idle."
        )
    prod, staging = ctx.isolate()
    prod_env = _env_paths(PROD_UNITS, ctx.unit_dir)
    staging_env = _env_paths(STAGING_UNITS, ctx.unit_dir)
    started = datetime.now(UTC)
    ctx.out("==> stop staging")
    _systemctl(ctx.run, "stop", "agentdeck-staging.socket", STAGING_WEB_UNIT, STAGING_RUNTIME_UNIT)
    try:
        ctx.out("==> copy poller instances")
        poller_files = copy_poller(Path(prod.poller.root), Path(staging.poller.root))
        ctx.out("==> copy the history database (scrubbing " + ", ".join(SCRUBBED_TABLES) + ")")
        db_target = Path(staging.history.db_path).expanduser()
        scrubbed = copy_sqlite(
            Path(prod.history.db_path).expanduser(), db_target, scrub=SCRUBBED_TABLES
        )
        ctx.out("==> copy the GitHub cache")
        cache_target = staging_env[f"{STAGING_WEB_UNIT}:AGENTDECK_GITHUB_CACHE"]
        copy_sqlite(prod_env["agentdeck.service:AGENTDECK_GITHUB_CACHE"], cache_target)
        manifest = {
            "captured_start": started.isoformat(),
            "captured_end": datetime.now(UTC).isoformat(),
            "production_revision": _git("rev-parse", "HEAD", cwd=PROD_CHECKOUT),
            "scrubbed_rows": scrubbed,
            "files": [_file_entry(p) for p in (db_target, cache_target, *poller_files)],
        }
        _write_json(MANIFEST_FILE, manifest)
    finally:
        ctx.out("==> start staging")
        _systemctl(ctx.run, "start", "agentdeck-staging.socket")
    wait_healthy(staging.server.port)
    ctx.out(
        f"refreshed from production {manifest['production_revision'][:8]} at"
        f" {manifest['captured_end']}; scrubbed rows: {scrubbed}"
    )
    return manifest


def moves_back_past_schema(target: str, cwd: Path = STAGING_DIR) -> bool:
    """Deploying ``target`` leaves a database change the current checkout already ran."""
    head = _git("rev-parse", "HEAD", cwd=cwd)
    if not head:
        return False
    ahead = (
        subprocess.run(
            ["git", "-C", str(cwd), "merge-base", "--is-ancestor", head, target],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if ahead:
        return False
    return bool(_git("diff", "--name-only", target, head, "--", "src/agentdeck/db.py", cwd=cwd))


def deploy(ctx: Context, ref: str, *, restart_runtime: bool = False) -> None:
    # Resolved here, in the caller's checkout: ``HEAD`` or a worktree's own ref would mean
    # something else inside the staging worktree. The object store is shared, so the commit
    # itself is there without a push.
    ref = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=Path.cwd()) or ref
    ctx.lease(ref)
    ctx.isolate()
    if moves_back_past_schema(ref):
        ctx.out(
            "WARNING: this moves staging back past a database change it has already migrated;"
            " run `scripts/staging.sh refresh` if it misbehaves."
        )
    script = Path(__file__).resolve().parents[2] / "scripts" / "staging-checkout.sh"
    argv = [str(script), *(["--restart-runtime"] if restart_runtime else []), ref]
    done = subprocess.run(argv, check=False)
    if done.returncode != 0:
        raise StagingError(f"staging-checkout.sh exited {done.returncode}")
    # staging-checkout.sh's own probe can time out while the web boots, and still exits 0,
    # and it can be answered by the worker the reload is replacing (#2628).
    _, staging = ctx.configs()
    health = wait_healthy(
        staging.server.port, expect=_git("rev-parse", "HEAD") or None, out=ctx.out
    )
    ctx.out(
        f"staging healthy on {_git('log', '-1', '--format=%h %s')}"
        f" (serving {str(health.get('revision') or '?')[:8]})"
    )


def settled_population(
    port: int,
    *,
    timeout_s: float = 180.0,
    interval_s: float = 5.0,
    fetch: Callable[[str], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> dict:
    """The population once the deck has scanned every account and two reads agree.

    Right after a deploy or a refresh the deck is still scanning, and the overview it serves
    grows as chats are found; a save taken then would put that warm-up into the diff. Two reads
    served by different code do not agree, however alike their Works (#2628).
    """
    url = f"http://127.0.0.1:{port}/api/works/population"
    fetch = fetch or (lambda target: _http_get(target, timeout=120.0))
    deadline = time.monotonic() + timeout_s
    previous: dict | None = None
    while True:
        current = fetch(url)
        ready = current.get("ready", True)
        if (
            ready
            and previous is not None
            and previous.get("works") == current.get("works")
            and previous.get("revision") == current.get("revision")
        ):
            return current
        if time.monotonic() >= deadline:
            out(
                "WARNING: the Work population did not settle"
                f" ({'still scanning' if not ready else 'still changing'}); saving it anyway"
            )
            return current
        previous = current if ready else None
        sleep(interval_s)


def works_save(ctx: Context, name: str) -> dict:
    target = _save_path(name)
    ctx.lease()
    _, staging = ctx.configs()
    head = _git("rev-parse", "HEAD")
    wait_healthy(staging.server.port, expect=head or None, out=ctx.out)
    population = settled_population(staging.server.port, out=ctx.out)
    stale = _served(population, head or None)
    if stale:
        raise StagingError(f"{stale}; not saving {name!r}")
    manifest = _read_json(MANIFEST_FILE) or {}
    saved = {
        "name": name,
        "saved_at": datetime.now(UTC).isoformat(),
        "commit": head,
        "revision": population.get("revision"),
        "subject": _git("log", "-1", "--format=%s"),
        "snapshot": {
            "captured_end": manifest.get("captured_end"),
            "production_revision": manifest.get("production_revision"),
        },
        "generated_at": population.get("generated_at"),
        "works": population.get("works", []),
    }
    _write_json(target, saved)
    ctx.out(works_summary(saved))
    ctx.out(f"wrote {target}")
    return saved


def status(ctx: Context) -> None:
    now = datetime.now(UTC)
    lease = _read_json(LEASE_FILE)
    ctx.out(f"lease: {describe_lease(lease)}" if lease and _live(lease, now) else "lease: free")
    ctx.out(f"staging: {_git('log', '-1', '--format=%h %s') or 'no checkout'} ({STAGING_DIR})")
    manifest = _read_json(MANIFEST_FILE)
    if manifest:
        end = _parse_time(manifest.get("captured_end"))
        age = f"{(now - end).total_seconds() / 3600:.1f} h ago" if end else "at an unknown time"
        ctx.out(
            f"snapshot: refreshed {age} from production"
            f" {str(manifest.get('production_revision'))[:8]}"
        )
    else:
        ctx.out("snapshot: never refreshed from production")
    active = ctx.run(["systemctl", "--user", "is-active", STAGING_WEB_UNIT]).stdout.strip()
    if active == "active":
        _, staging = ctx.configs()
        try:
            health = _http_get(f"http://127.0.0.1:{staging.server.port}/healthz", timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            health = f"unhealthy ({exc})"
        ctx.out(f"web: active, /healthz {health}")
    else:
        ctx.out(f"web: {active or 'unknown'} (starts on the first request to staging)")


def fence(ctx: Context) -> None:
    for path in install_fence(unit_dir=ctx.unit_dir, path_value=ctx.manager_path()):
        ctx.out(f"wrote {path}")
    _systemctl(ctx.run, "daemon-reload")
    # A running staging keeps the environment it started with; stop it so the next request
    # starts it fenced. The socket keeps listening.
    _systemctl(ctx.run, "stop", STAGING_WEB_UNIT, STAGING_RUNTIME_UNIT)


def check(ctx: Context) -> None:
    ctx.isolate()
    ctx.out("isolation check passed: no staging path reaches production")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/staging.sh", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--holder", default="", help="who holds the lease (default: this Work)")
    parser.add_argument("--session", default=os.environ.get("AGENTDECK_SESSION_KEY", ""))
    parser.add_argument("--take", action="store_true", help="take over another holder's lease")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="copy production's state into staging")
    deploy_cmd = commands.add_parser("deploy", help="check a ref out on staging")
    deploy_cmd.add_argument("ref")
    deploy_cmd.add_argument("--restart-runtime", action="store_true")
    works = commands.add_parser("works", help="save or compare the Work population")
    works_cmds = works.add_subparsers(dest="works_command", required=True)
    works_cmds.add_parser("save").add_argument("name")
    diff_cmd = works_cmds.add_parser("diff")
    diff_cmd.add_argument("before")
    diff_cmd.add_argument("after")
    commands.add_parser("status", help="lease, checkout, snapshot and health")
    commands.add_parser("release", help="give the lease back")
    commands.add_parser("check", help="run the isolation check")
    commands.add_parser("fence", help="install the gh stub and the units' fence drop-ins")
    for sub in (*commands.choices.values(), *works_cmds.choices.values()):
        sub.add_argument("--take", action="store_true", default=argparse.SUPPRESS)
        sub.add_argument("--holder", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    ctx = Context(
        holder=args.holder or default_holder(Path.cwd()), session=args.session, take=args.take
    )
    try:
        if args.command == "refresh":
            refresh(ctx)
        elif args.command == "deploy":
            deploy(ctx, args.ref, restart_runtime=args.restart_runtime)
        elif args.command == "works" and args.works_command == "save":
            works_save(ctx, args.name)
        elif args.command == "works":
            before = _read_json(_saved_path(args.before))
            after = _read_json(_saved_path(args.after))
            if before is None or after is None:
                raise StagingError("both saves must exist; see `works save <name>`")
            ctx.out(works_diff(before, after))
        elif args.command == "status":
            status(ctx)
        elif args.command == "release":
            ctx.out(
                "lease released"
                if release_lease(LEASE_FILE, ctx.holder, take=ctx.take)
                else "lease: free"
            )
        elif args.command == "check":
            check(ctx)
        elif args.command == "fence":
            fence(ctx)
    except StagingError as exc:
        print(f"staging: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
