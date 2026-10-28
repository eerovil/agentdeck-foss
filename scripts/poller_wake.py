#!/usr/bin/env python3
"""One Poller Instance wake: run the engine with the host's webhook-quiet hint (#180).

This is what a Poller Instance's systemd unit runs. It is deliberately thin: the engine makes
every decision and applies every effect itself. Since #3732 the engine holds no cards and
starts no workers (the deck runs every Work), so there is nothing left here to deliver.

    python3 scripts/poller_wake.py --instance agentdeck

The engine itself remains directly runnable (`scripts/poller_engine.py cycle …`) for
diagnosis.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agentdeck.local_bin import ensure_local_bin_on_path  # noqa: E402
from agentdeck.poller_engine import config as engine_config  # noqa: E402
from agentdeck.poller_engine import cycle as engine_cycle  # noqa: E402
from agentdeck.providers.runtime_socket_client import runtime_socket_path  # noqa: E402

DEFAULT_ROOT = Path.home() / ".local/share/agentdeck/poller"
DEFAULT_BASE_URL = "http://127.0.0.1:8756"

# The engine this wake runs: the launcher in this same checkout (#2153). The engine deploys
# with AgentDeck as ordinary software.
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


def main(argv: list[str] | None = None) -> int:
    ensure_local_bin_on_path()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="the host's poller root")
    parser.add_argument("--instance", required=True, help="the instance id")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="the deck's API")
    # The engine's reads of the persistent runtime go to its socket, not the web (#2144).
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
            # point that canary at the live runtime, so the pair must be named together.
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
