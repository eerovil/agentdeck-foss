#!/usr/bin/env python3
"""Report real worker `gh` calls recorded by the AgentDeck shim."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agentdeck.gh_shim.limits import CallAccounting  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=1.0)
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(
            os.environ.get(
                "AGENTDECK_GITHUB_CACHE",
                "~/.cache/agentdeck/github_metadata.sqlite3",
            )
        ).expanduser(),
    )
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours must be positive")
    rows = CallAccounting(args.cache).totals(since=time.time() - args.hours * 3600)
    if not rows:
        print("No worker gh calls recorded in this window.")
        return 0
    print("consumer\tkind\tcalls")
    for consumer, kind, calls in rows:
        print(f"{consumer}\t{kind}\t{calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
