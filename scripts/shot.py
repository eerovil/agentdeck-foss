#!/usr/bin/env python3
"""Drive a headless browser against a running agentdeck and screenshot it.

Lets an agent (or you) *see* the UI, not just the HTML — for verifying layout,
markdown rendering, marquee, expand, etc.

Examples:
    uv run python scripts/shot.py /                       # dashboard, mobile viewport
    uv run python scripts/shot.py /sessions/<key> --full  # full-page detail
    uv run python scripts/shot.py / --click '.expand-btn' # tap the first expand button
    uv run python scripts/shot.py / --desktop --out /tmp/dash.png

Base URL defaults to $AGENTDECK_URL or http://127.0.0.1:8756.
Screenshots go to /tmp/agentdeck-shot.png unless --out is given.
The exit code is non-zero if any console error was logged (helps catch JS bugs).
"""

from __future__ import annotations

import argparse
import os
import sys

from playwright.sync_api import sync_playwright

DEFAULT_BASE = os.environ.get("AGENTDECK_URL", "http://127.0.0.1:8756")
# iPhone-ish portrait — this is a mobile-first app, so that's the default lens.
MOBILE = {"width": 390, "height": 844}
DESKTOP = {"width": 1100, "height": 900}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="URL or path (joined onto the base URL)")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--out", default="/tmp/agentdeck-shot.png")
    ap.add_argument("--desktop", action="store_true", help="desktop viewport instead of mobile")
    ap.add_argument("--full", action="store_true", help="full-page (scrolled) screenshot")
    ap.add_argument("--wait-ms", type=int, default=1200, help="settle time after load")
    ap.add_argument("--wait-selector", help="wait for this selector before shooting")
    ap.add_argument("--click", action="append", default=[], help="selector(s) to click first")
    args = ap.parse_args()

    url = (
        args.path
        if args.path.startswith("http")
        else args.base.rstrip("/") + "/" + args.path.lstrip("/")
    )
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=DESKTOP if args.desktop else MOBILE)
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        # NB: the page holds a long-lived SSE stream, so the network never goes
        # idle — wait on DOM/load, then settle with --wait-ms / --wait-selector.
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        if args.wait_selector:
            page.wait_for_selector(args.wait_selector, timeout=8000)
        for sel in args.click:
            page.click(sel, timeout=8000)
            page.wait_for_timeout(400)
        page.wait_for_timeout(args.wait_ms)
        page.screenshot(path=args.out, full_page=args.full)
        browser.close()

    print(f"→ {args.out}  ({url})")
    if errors:
        print("console errors:", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
