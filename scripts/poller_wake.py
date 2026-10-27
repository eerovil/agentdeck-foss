#!/usr/bin/env python3
"""One Poller Instance wake: run the engine, then act on the directives it produced (#180).

This is what a Poller Instance's systemd unit runs. It is deliberately thin — every decision
was already made by the engine, which ran read-only against the board and recorded its intent
durably before printing anything. This process only carries those decisions out.

    python3 scripts/poller_wake.py --instance agentdeck

The engine itself remains directly runnable (`scripts/poller_engine.py cycle …`) for
diagnosis: doing that prints the directives instead of acting on them, which is exactly what
you want when asking "what would this wake do?" without a worker appearing.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agentdeck.local_bin import ensure_local_bin_on_path  # noqa: E402
from agentdeck.poller_adapter import turns  # noqa: E402
from agentdeck.poller_adapter.control import card_control_dir  # noqa: E402
from agentdeck.poller_adapter.runner import (  # noqa: E402
    act_on,
    drain_review_verdicts,
    read_directives,
    recent_change_warning,
)
from agentdeck.poller_adapter.work_workspaces import WorkWorkspaces  # noqa: E402
from agentdeck.poller_adapter.worker import WorkerDispatcher  # noqa: E402
from agentdeck.poller_engine import config as engine_config  # noqa: E402
from agentdeck.poller_engine import cycle as engine_cycle  # noqa: E402
from agentdeck.poller_engine import extensions as engine_extensions  # noqa: E402
from agentdeck.poller_engine import github as engine_github  # noqa: E402
from agentdeck.poller_engine import reconcile as engine_reconcile  # noqa: E402
from agentdeck.poller_engine.__main__ import _enabled_extensions  # noqa: E402
from agentdeck.poller_engine.state import EnginePaths, StateError, StateStore  # noqa: E402
from agentdeck.providers.runtime_socket_client import runtime_socket_path  # noqa: E402

DEFAULT_ROOT = Path.home() / ".local/share/agentdeck/poller"
DEFAULT_BASE_URL = "http://127.0.0.1:8756"

# The engine this wake runs: the launcher in this same checkout (#2153). The engine deploys
# with AgentDeck as ordinary software, so the worker commands this wake writes into a prompt
# and the engine that implements them can never come from two different versions.
ENGINE = REPO / "scripts" / "poller_engine.py"


def wake_environment(
    instance_id: str,
    manifest: Path,
    overlay: Path,
    *,
    streams_root: Path | None = None,
) -> dict[str, str]:
    """The engine subprocess's environment, carrying the webhook-quiet hint when earned (#1631).

    The hint rides an environment variable rather than a flag. The host proves its two
    conditions here — the watcher
    heartbeat is fresh and this instance's streams hold no unread delivery — and the engine
    still owns the policy knob, the last-full-wake ceiling, and the right to decline.

    Every failure, including this checkout not being able to import the tailer at all,
    answers with the plain environment: a full wake, never a refused one.

    The slugs carry the manifest's own spelling, and the streams are matched without
    regard to case (#1685): lowercasing them here meant no repository whose owner or name
    has a capital letter could ever be found on a case-sensitive filesystem, so the whole
    feature was unreachable for those instances with nothing to notice it. A not-quiet
    answer now says why, on stderr and so in `cycle.log`, whenever the reason changes.

    The gate is the three facts the hint actually consumes, not a problem-free inspection
    (#1680). Requiring `draft.ok` made the feature inert fleet-wide: four of five instances
    here declare an extension that is not installed, which the running engine tolerates at
    wake time — it skips the extension and counts it — while `inspect` reports it as a
    problem. A cosmetic manifest wart silently doubling the GitHub polling load is exactly
    the invisible failure this gate must not have. A manifest that cannot be parsed at all
    still fails these three checks on its own, because `inspect` falls back to an empty
    Manifest, so genuine parse failures keep running a full wake.
    """
    env = dict(os.environ)
    try:
        draft = engine_config.inspect(manifest, overlay)
        if (
            draft.manifest.webhook_skip_s <= 0
            or not draft.manifest.board.org
            or not draft.manifest.repos
        ):
            return env
        from agentdeck import poller_quiet  # noqa: PLC0415 - degrade, never block the wake

        # Only the repositories this instance could *discover* something in. An
        # artifacts-only repository holds screenshots a worker pushed and never a card or a
        # commit anyone reviews, so there is nothing on its stream for a wake to find — and
        # demanding one silently switched this whole feature off. #1791 gave every project an
        # artifacts repository, none of them has a webhook, and `_delivery_reason`
        # correctly refuses to call a stream it cannot see quiet. The result was five of seven
        # instances running a full live wake every thirty-five seconds around the clock, and
        # saying nothing about it: the reason never changes, so it is logged once and never
        # again. The fleet sat at 4,900 REST calls an hour against a 5,000 ceiling.
        #
        # Filtering here rather than fixing the two repositories keeps the next one safe: a
        # project that adds an evidence repository must not be able to disable the feature by
        # naming it.
        slugs = [
            f"{draft.manifest.board.org}/{repo.id}"
            for repo in draft.manifest.repos
            if repo.holds_issues or repo.holds_code
        ]
        if not slugs:
            return env
        kwargs = {} if streams_root is None else {"streams_root": streams_root}
        reason = poller_quiet.not_quiet_reason(instance_id, slugs, **kwargs)
        changed = poller_quiet.reason_is_new(instance_id, reason, **kwargs)
        if reason is None:
            env[engine_cycle.WEBHOOK_QUIET_ENV] = "1"
            print(
                "webhook streams quiet; offering the engine a live-query skip",
                file=sys.stderr,
            )
        elif changed:
            print(f"webhook streams not quiet: {reason}", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - any failure here means more polling, not less
        print(f"webhook-quiet check failed ({error}); running a full wake", file=sys.stderr)
    return env


def _unchecked(_block: dict, _problems: list) -> None:
    """The settings check of an extension the wake refused: nothing to ask it."""


def main(argv: list[str] | None = None) -> int:
    ensure_local_bin_on_path()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="the host's poller root")
    parser.add_argument("--instance", required=True, help="the instance id")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="the deck's API")
    # Delivery and the worker reads that decide it go to the persistent runtime, not the web
    # (#2144): a web deploy or restart must not sit in the path of a machine delivery.
    parser.add_argument(
        "--runtime-socket",
        default="",
        help="the persistent runtime's host-local socket (default: the live runtime's)",
    )
    parser.add_argument("--engine", default="", help="override the engine launcher")
    args = parser.parse_args(argv)
    if not args.runtime_socket:
        if args.base_url != DEFAULT_BASE_URL:
            # A canary wake points --base-url at the staging deck. Defaulting the socket would
            # send that canary's workers to the live runtime, so the pair must be named together.
            parser.error("a non-default --base-url needs --runtime-socket naming the same deck")
        args.runtime_socket = str(runtime_socket_path())

    root = Path(args.root).expanduser()
    instance = root / "instances" / args.instance
    engine = Path(args.engine) if args.engine else ENGINE
    manifest, overlay = instance / "poller.toml", instance / "overlay.toml"
    binding = instance / "binding.json"

    wake = subprocess.run(
        [str(engine), "cycle", "--manifest", str(manifest), "--overlay", str(overlay),
         "--binding", str(binding), "--agentdeck-url", args.base_url,
         "--runtime-socket", args.runtime_socket],
        capture_output=True, text=True, timeout=540, check=False,
        env=wake_environment(args.instance, manifest, overlay),
    )
    # The engine's own log is on stderr and is a health fact in its own right, so it is passed
    # through whatever the exit code was.
    if wake.stderr:
        print(wake.stderr.rstrip(), file=sys.stderr)
    if wake.returncode not in (0, 75):
        print(f"engine refused this wake (exit {wake.returncode})", file=sys.stderr)
        return wake.returncode

    # How each Run ended is the Runtime's fact, and the engine reads it from the Runtime
    # itself (#2200). What is left here is the stops: the engine fenced the old Run's lease,
    # and the Runtime is what stops its process (#2159). Before the no-directives return — a
    # card waiting on a stop is precisely a card producing no directives.
    sweep_config = engine_config.inspect(manifest, overlay)
    engine_paths = EnginePaths.from_base(sweep_config.overlay.state_path)
    owner = sweep_config.manifest.board.org
    try:
        issues = StateStore(engine_paths).read().get("issues") or {}
    except StateError as error:
        print(f"stop sweep skipped: {error}", file=sys.stderr)
        issues = {}
    for line in turns.stop_runs(
        socket=args.runtime_socket,
        owner=owner,
        issues=issues,
    ):
        print(line)

    # Before the no-directives return for the same reason as the stops: a wake that consumed
    # a review verdict and started nothing still has history to hand over (#1122).
    for line in drain_review_verdicts(
        engine_paths.review_verdict_dir, base_url=args.base_url
    ):
        print(line)

    directives = read_directives(wake.stdout)
    if not directives:
        print("no directives this wake")
        return 0

    # Locate the extension programs exactly as the engine does. Checking the config without
    # them reports every enabled extension as missing, and the directives the engine just
    # produced would be dropped on the floor for a reason that has nothing to do with them.
    first = engine_config.inspect(manifest, overlay)
    installed, problems = _enabled_extensions(first.manifest, first.overlay, wake=True)
    draft = engine_config.inspect(
        manifest,
        overlay,
        extension_validators={
            # A refused extension was already reported degraded by the wake, which ran without
            # it; failing its settings check here would stop delivering the very directives
            # that wake produced — one broken extension parking every worker (#2158).
            e.name: (_unchecked if e.refused else engine_extensions.config_validator(e))
            for e in installed
        },
    )
    draft.report.extend(problems)
    if not draft.ok:
        # The engine already ran, so this cannot invalidate the wake — but without the Overlay
        # there is nowhere to put a worker, and delivering blind would be worse than waiting.
        print(draft.report.render(), file=sys.stderr)
        return 78

    def note_account(message: str) -> None:
        # stderr, because the wake passes its stderr through as a health fact in its own
        # right. An account skipped for being unable to run a turn is exactly that (#1519).
        print(f"account selection: {message}", file=sys.stderr)

    paths = EnginePaths.from_base(draft.overlay.state_path)
    # The runtime chooses each delivery's account from this policy (#2146); this host no longer
    # picks one up front.
    dispatcher = WorkerDispatcher(
        receipts_dir=paths.dispatch_ack_dir,
        runtime_socket=Path(args.runtime_socket),
        candidates=tuple(draft.overlay.accounts),
        usage_targets=dict(draft.overlay.usage_targets),
        note=note_account,
    )
    # The delivery's all-or-none generation fence, and the card's record of where it worked.
    # Never an owner: every path is a Work claim, which only the runtime releases (#2195).
    def register(directive: dict, worktrees: list[str]) -> bool:
        # Validate every path before opening the transaction: one unusable path must refuse
        # the whole delivery rather than durably register the rest of it.
        for path in worktrees:
            _repo_root, present = engine_github.registered_worktree(path)
            if not present:
                return False
        with StateStore(paths).transaction() as state:
            return engine_reconcile.register_generation_worktrees(
                state,
                str(directive.get("repo") or ""),
                int(directive.get("number") or 0),
                str(directive.get("generation") or ""),
                worktrees,
            )

    def legacy_worktrees(directive: dict) -> list[str] | None:
        """What the card has registered, for the runtime's adoption rule; None when unread."""
        try:
            rec = (StateStore(paths).read().get("issues") or {}).get(
                f"{directive.get('repo') or ''}#{int(directive.get('number') or 0)}"
            )
        except (StateError, TypeError, ValueError):
            return None
        worktrees = rec.get("worktrees") if isinstance(rec, dict) else []
        return [str(item) for item in worktrees or [] if isinstance(item, str)]

    def register_command(directive: dict) -> str:
        parts = [
            str(engine),
            "register-worktree",
            "--state",
            str(paths.state),
            "--generation",
            str(directive.get("generation") or ""),
        ]
        # The worktree becomes the Work's claim, never the card's to delete (#2195).
        if directive.get("work_id"):
            parts += [
                "--work-id",
                str(directive["work_id"]),
                "--runtime-socket",
                str(args.runtime_socket),
            ]
        parts += [str(directive.get("repo") or ""), str(int(directive.get("number") or 0))]
        return shlex.join(parts)

    def change_warning_for(directive: dict, workspace) -> str:
        # This is useful context, not an admission gate. A damaged state file or disappearing
        # worktree must not turn a safe at-most-once delivery into a refused or retried one.
        try:
            return recent_change_warning(directive, workspace)
        except (OSError, StateError, TypeError, ValueError):
            return ""

    artifact_repo, artifact_checkout = engine_config.artifact_evidence(
        draft.manifest, draft.overlay
    )
    result = act_on(
        directives,
        dispatcher=dispatcher,
        artifact_repo=artifact_repo,
        artifact_checkout=artifact_checkout,
        # Resolved from the Manifest before any brief is written, so a repository whose guide
        # bans closing keywords never receives an instruction demanding one (#1734).
        closing_keywords=engine_config.closing_keyword_policies(draft.manifest),
        worker_may_deploy=draft.manifest.worker_may_deploy,
        worker_may_merge=draft.manifest.worker_may_merge,
        checkouts={
            repo: Path(path)
            for repo, path in engine_config.worker_checkouts(
                draft.manifest, draft.overlay
            ).items()
        },
        borrowed=engine_config.borrowed_workspaces(draft.manifest),
        code_checkouts=engine_config.code_checkouts(draft.manifest, draft.overlay),
        register=register,
        register_command=register_command,
        work_workspaces=WorkWorkspaces(
            runtime_socket=args.runtime_socket, legacy_worktrees=legacy_worktrees
        ),
        control_command=shlex.join([
            str(engine),
            "worker-control",
            "--manifest", str(manifest),
            "--overlay", str(overlay),
            "--binding", str(binding),
            "--agentdeck-url", args.base_url,
            "--runtime-socket", args.runtime_socket,
        ]),
        # Each dispatch gets its own executable copy of that invocation, so the brief carries a
        # path instead of restating the whole thing on every command line (#1732).
        control_root=card_control_dir(paths.dispatch_ack_dir.parent),
        change_warning_for=change_warning_for,
    )
    for line in result.log:
        print(line)
    for line in result.skipped:
        print(line, file=sys.stderr)
    distribution: dict[str, int] = {}
    for selected in dispatcher.placed:
        distribution[selected] = distribution.get(selected, 0) + 1
    used = ", ".join(f"{name} x{count}" for name, count in distribution.items())
    print(f"{result.summary()} on {used or '(no account named)'}")
    # A skipped or uncertain delivery is not this process failing: the engine holds the lease
    # and decides on its next wake. Only an engine that refused outright is a failed wake.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
