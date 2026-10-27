#!/usr/bin/env python3
"""One-off cutover: turn every card-held worktree into a Work claim, or give it back (#2195).

Before #2149 a Poller card owned the worktrees its wake made, and deleted them when it
finished. #2149 moved that ownership to Work workspace claims, but cards dispatched before
it still list worktrees no Work claims. This walks those, once, so card-level ownership can
be removed:

* a path that is gone from disk is dropped from the card record — there is nothing to delete;
* a path that still exists is adopted as the Work's claim through the runtime, the one writer
  of claims, which applies its own adoption rule (a registered worktree of the claimed
  repository that the card registered);
* a path that duplicates a claim the Work already holds for the same repository and purpose
  is released through the engine's dirty-safe removal, which keeps its branch and refuses a
  tree with uncommitted work, and then dropped from the card;
* anything else — an unregistered directory, an unknown remote, a detached checkout, two
  candidates and no way to tell which is the Work's — is kept exactly as it is and reported.

Nothing is ever deleted without the dirty check. It is safe to run again: a path that is
already a claim is skipped, and a dropped path is no longer on the card.

    ~/agentdeck/.venv/bin/python scripts/adopt_card_worktrees.py            # report only
    ~/agentdeck/.venv/bin/python scripts/adopt_card_worktrees.py --apply

Exits 0 when no card holds a worktree that is not a Work claim, 1 when some remain.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agentdeck.poller_adapter.runner import default_branch  # noqa: E402
from agentdeck.poller_adapter.work_workspaces import ARTIFACT, IMPLEMENTATION  # noqa: E402
from agentdeck.poller_engine import config as engine_config  # noqa: E402
from agentdeck.poller_engine import paths as path_identity  # noqa: E402
from agentdeck.poller_engine.dispatch import work_id as card_work_id  # noqa: E402
from agentdeck.poller_engine.github import (  # noqa: E402
    _runtime_get,
    runtime_post,
)
from agentdeck.poller_engine.state import EnginePaths, StateStore  # noqa: E402
from agentdeck.providers.runtime_socket_client import runtime_socket_path  # noqa: E402
from agentdeck.work_workspace_claims import (  # noqa: E402
    CLAIMS_PATH,
    ENSURE_PATH,
    GitHost,
    HostWorktree,
)

DEFAULT_ROOT = "~/.local/share/agentdeck/poller"

ADOPTED = "adopted"
ALREADY = "already a claim"
DROPPED = "dropped (gone from disk)"
RELEASED = "released"
KEPT = "kept"


@dataclass(frozen=True)
class Item:
    """One card-held path and what the migration decided for it."""

    card: str
    path: str
    action: str  # "adopt", "release", "drop", "keep", "skip"
    detail: str = ""
    work_id: str = ""
    repository: str = ""
    purpose: str = ""


@dataclass
class Host:
    """The host reads and writes the migration needs; replaced by fakes in tests."""

    inspect: Callable[[str], HostWorktree] = GitHost().inspect
    branch: Callable[[str], str] = lambda path: _git_out(path, "symbolic-ref", "--short", "HEAD")
    base: Callable[[str], str] = lambda checkout: default_branch(Path(checkout))
    remove: Callable[[str], tuple[bool, str]] = GitHost().remove
    exists: Callable[[str], bool] = lambda path: os.path.lexists(path)


def _git_out(path: str, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", path, *args], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _names_card(path: str, number: int) -> bool:
    """Whether the directory's name carries the card's number as a whole number."""
    return str(number) in re.findall(r"\d+", Path(path).name)


def plan_card(
    card: str,
    record: dict,
    *,
    owner: str,
    artifact_repo: str,
    claims: list[dict],
    host: Host,
) -> list[Item]:
    """Decide every path one card holds. Pure over ``host`` reads: nothing is changed here."""
    repo, _, raw_number = card.partition("#")
    number = int(raw_number)
    work = card_work_id(owner, repo, number)
    # Resolved only when named: an empty path would resolve to this process's cwd.
    claimed = {path_identity.resolve(str(item["path"])) for item in claims if item.get("path")}
    # What this Work already holds, by (repository, purpose): a second path for one of these
    # is a leftover, never a second claim.
    held = {
        (str(item.get("repository") or ""), str(item.get("purpose") or "")): str(item["path"])
        for item in claims
        if item.get("work_id") == work and item.get("path")
    }
    items: list[Item] = []
    groups: dict[tuple[str, str], list[str]] = {}
    for path in path_identity.resolve_all(record.get("worktrees")):
        if path in claimed:
            items.append(Item(card, path, "skip", ALREADY, work))
            continue
        if not host.exists(path):
            items.append(Item(card, path, "drop", "", work))
            continue
        seen = host.inspect(path)
        if not seen.registered:
            items.append(Item(card, path, "keep", "not a registered Git worktree", work))
            continue
        if not seen.repository:
            items.append(Item(card, path, "keep", "its origin is not a GitHub repository", work))
            continue
        purpose = ARTIFACT if seen.repository == artifact_repo else IMPLEMENTATION
        key = (seen.repository, purpose)
        if key in held:
            items.append(
                Item(
                    card, path, "release",
                    f"the Work already claims {held[key]} for {seen.repository} ({purpose})",
                    work, *key,
                )
            )
            continue
        groups.setdefault(key, []).append(path)
    for (repository, purpose), paths in groups.items():
        if len(paths) == 1:
            items.append(Item(card, paths[0], "adopt", "", work, repository, purpose))
            continue
        # Two candidates for one claim. The one named for this card is the Work's; the rest
        # were made beside it and go back. Without exactly one such name it would be a guess.
        named = [path for path in paths if _names_card(path, number)]
        if len(named) != 1:
            for path in paths:
                items.append(
                    Item(
                        card, path, "keep",
                        f"{len(paths)} worktrees of {repository} ({purpose}) and no single one"
                        " named for the card",
                        work, repository, purpose,
                    )
                )
            continue
        for path in paths:
            if path == named[0]:
                items.append(Item(card, path, "adopt", "", work, repository, purpose))
            else:
                items.append(
                    Item(
                        card, path, "release", f"{named[0]} is the one named for the card",
                        work, repository, purpose,
                    )
                )
    return items


@dataclass
class Outcome:
    item: Item
    result: str
    detail: str = ""


@dataclass
class Runtime:
    """The runtime's claim routes; replaced by a fake in tests."""

    socket: str
    post: Callable[..., tuple[int, object | None]] = runtime_post
    get: Callable[[str, str], object | None] = _runtime_get

    def claims(self) -> list[dict] | None:
        answer = self.get(self.socket, CLAIMS_PATH)
        items = answer.get("claims") if isinstance(answer, dict) else None
        if not isinstance(items, list):
            return None
        return [item for item in items if isinstance(item, dict)]

    def adopt(self, item: Item, *, branch: str, base: str, legacy: list[str]) -> tuple[bool, str]:
        status, answer = self.post(
            self.socket,
            ENSURE_PATH,
            {
                "work_id": item.work_id,
                "run_id": "",
                "repository": item.repository,
                "purpose": item.purpose,
                "checkout": str(Path(item.path).parent.parent),
                "path": item.path,
                "branch": branch,
                "base_ref": base,
                "legacy_paths": legacy,
            },
            timeout=300,
        )
        if status != 200 or not isinstance(answer, dict):
            detail = answer.get("detail") if isinstance(answer, dict) else ""
            return False, str(detail or f"runtime answered {status or 'nothing'}")
        outcome = str(answer.get("outcome") or "")
        claim = answer.get("claim") if isinstance(answer.get("claim"), dict) else {}
        raw = str(claim.get("path") or "")
        if outcome in {"adopted", "reused"} and raw and path_identity.resolve(raw) == item.path:
            return True, outcome
        return False, f"{outcome or 'no outcome'}: {answer.get('detail') or ''}".strip(": ")


def apply_card(
    card: str,
    items: list[Item],
    *,
    store: StateStore | None,
    runtime: Runtime,
    host: Host,
) -> list[Outcome]:
    """Carry out one card's plan: releases and drops first, then adoptions.

    That order is what lets the runtime adopt: its rule refuses while the card still
    registers a second present worktree of the same repository, so the leftover must be
    gone before the Work's own one is claimed. ``store`` None is a dry run.
    """
    outcomes: list[Outcome] = []
    gone: set[str] = set()
    for item in items:
        if item.action == "skip":
            outcomes.append(Outcome(item, ALREADY))
        elif item.action == "keep":
            outcomes.append(Outcome(item, KEPT, item.detail))
        elif item.action == "drop":
            gone.add(item.path)
            outcomes.append(Outcome(item, DROPPED if store is not None else "would drop"))
        elif item.action == "release":
            if store is None:
                outcomes.append(Outcome(item, "would release", item.detail))
                continue
            released, why = host.remove(item.path)
            if released:
                gone.add(item.path)
                outcomes.append(Outcome(item, RELEASED, item.detail))
            else:
                outcomes.append(Outcome(item, KEPT, f"not released: {why}"))
    if store is not None and gone:
        with store.transaction() as state:
            record = (state.get("issues") or {}).get(card)
            if isinstance(record, dict):
                record["worktrees"] = [
                    path
                    for path in path_identity.resolve_all(record.get("worktrees"))
                    if path not in gone
                ]
    legacy: list[str] = []
    if store is not None:
        record = (store.read().get("issues") or {}).get(card)
        legacy = path_identity.resolve_all((record or {}).get("worktrees"))
    for item in items:
        if item.action != "adopt":
            continue
        branch = host.branch(item.path)
        if not branch:
            outcomes.append(Outcome(item, KEPT, "detached HEAD: no branch to claim it under"))
            continue
        if store is None:
            outcomes.append(Outcome(item, "would adopt", f"{item.purpose} on {branch}"))
            continue
        base = host.base(str(Path(item.path).parent.parent))
        ok, detail = runtime.adopt(item, branch=branch, base=base, legacy=legacy)
        outcomes.append(Outcome(item, ADOPTED if ok else KEPT, detail))
    order = {id(item): index for index, item in enumerate(items)}
    return sorted(outcomes, key=lambda outcome: order[id(outcome.item)])


def run_instance(
    instance: Path, *, runtime: Runtime, host: Host, apply: bool, out=print
) -> int | None:
    """Migrate one instance; the number of paths still not claims, or None when unreadable."""
    manifest, overlay = instance / "poller.toml", instance / "overlay.toml"
    if not manifest.exists() or not overlay.exists():
        return 0
    draft = engine_config.inspect(manifest, overlay)
    owner = draft.manifest.board.org
    artifact_repo = engine_config.artifact_evidence(draft.manifest, draft.overlay)[0].lower()
    store = StateStore(EnginePaths.from_base(draft.overlay.state_path))
    claims = runtime.claims()
    if claims is None:
        out(f"{instance.name}: the runtime did not answer {CLAIMS_PATH}; nothing done")
        return None
    remaining = 0
    for card, record in sorted((store.read().get("issues") or {}).items()):
        if not isinstance(record, dict) or not record.get("worktrees"):
            continue
        if not re.fullmatch(r"[^#]+#\d+", card):
            out(f"{instance.name} {card}: not a repo#number card; left as it is")
            remaining += len(record["worktrees"])
            continue
        items = plan_card(
            card, record, owner=owner, artifact_repo=artifact_repo, claims=claims, host=host
        )
        for outcome in apply_card(
            card, items, store=store if apply else None, runtime=runtime, host=host
        ):
            detail = f" — {outcome.detail}" if outcome.detail else ""
            out(f"{instance.name} {card} {outcome.item.path}: {outcome.result}{detail}")
            if outcome.result not in {ALREADY, ADOPTED, DROPPED, RELEASED}:
                remaining += 1
        # Later cards must see the claims this one just made.
        claims = runtime.claims() or claims
    return remaining


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=DEFAULT_ROOT, help="the Poller root (instances/)")
    parser.add_argument("--instance", action="append", default=[], help="only these instances")
    parser.add_argument("--runtime-socket", default="", help="the runtime socket")
    parser.add_argument("--apply", action="store_true", help="change things; default reports")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser() / "instances"
    runtime = Runtime(args.runtime_socket or str(runtime_socket_path()))
    host = Host()
    names = args.instance or sorted(path.name for path in root.iterdir() if path.is_dir())
    remaining, unreadable = 0, False
    for name in names:
        left = run_instance(root / name, runtime=runtime, host=host, apply=args.apply)
        if left is None:
            unreadable = True
        else:
            remaining += left
    verb = "" if args.apply else " (dry run)"
    print(f"card-held worktrees that are not Work claims{verb}: {remaining}")
    return 1 if remaining or unreadable else 0


if __name__ == "__main__":
    raise SystemExit(main())

