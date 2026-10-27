#!/usr/bin/env python3
"""Install the Poller host layout and its templated systemd units (#178).

Creates each named project's working directory, then installs the templated timer and
oneshot service with this host's poller root substituted in. Idempotent,
and deliberately narrow: it writes only inside the root and unit directories it is given, so
it can never disturb a poller installation that already exists on the same host.

    python3 scripts/install_poller_host.py --instance agentdeck
    python3 scripts/install_poller_host.py --root /srv/poller --unit-dir ~/.config/systemd/user

Installing does not enable anything. Enabling a project's timer is an activation decision
(#109/#180), so it stays a deliberate manual step:

    systemctl --user daemon-reload
    systemctl --user enable --now agentdeck-poller@agentdeck.timer
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agentdeck.poller_host import (  # noqa: E402
    DEFAULT_ROOT,
    DEFAULT_UNIT_DIR,
    install,
    timer_unit,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT, help="the host's poller root directory")
    parser.add_argument("--unit-dir", default=DEFAULT_UNIT_DIR, help="user systemd unit directory")
    parser.add_argument("--instance", action="append", default=[], help="a project id to prepare")
    args = parser.parse_args(argv)

    written = install(args.root, args.unit_dir, instances=args.instance)
    for path in written:
        print(f"installed {path}")
    print(f"layout ready under {Path(args.root).expanduser()}")
    if args.instance:
        print("\nnext, once each project has a manifest, overlay and binding:")
        print("  systemctl --user daemon-reload")
        for instance_id in args.instance:
            print(f"  systemctl --user enable --now {timer_unit(instance_id)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
