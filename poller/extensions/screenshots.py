#!/usr/bin/env python3
"""screenshots: check a project's screenshot routes and report them (#179, a #113 reference).

It used to declare `/shot` and turn it into queued work for the card's worker. The engine holds
no cards since #3732, so there was nothing for the request to queue on, and the command went in
#3741. What is left validates `default_routes` — the pages worth shooting, which a project still
names — and reports how many there are.

Hooks: validate-config, health. Stdlib only.
"""

from __future__ import annotations

import json
import sys


def _read_payload() -> dict:
    try:
        value = json.load(sys.stdin)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _answer(**parts) -> int:
    print(json.dumps(parts))
    return 0


def _validate(config: dict) -> list[dict]:
    routes = config.get("default_routes", [])
    if not isinstance(routes, list) or any(not isinstance(route, str) for route in routes):
        return [
            {
                "location": "manifest.extensions.screenshots.default_routes",
                "what": "must be a list of route paths",
                "fix": 'set default_routes = ["/"] or list the routes worth shooting',
            }
        ]
    if not routes:
        return [
            {
                "location": "manifest.extensions.screenshots.default_routes",
                "what": "is empty, so it names no page worth shooting",
                "fix": "list at least one route",
            }
        ]
    return []


HOOKS = ['health', 'validate-config']


def main(argv: list[str]) -> int:
    hook = argv[1] if len(argv) > 1 else ""
    payload = _read_payload()
    config = payload.get("config") or {}

    if hook == "hooks":
        # The engine asks rather than assuming: calling a hook that is not implemented would make
        # "unimplemented" indistinguishable from "broken".
        return _answer(hooks=HOOKS)
    if hook == "validate-config":
        return _answer(problems=_validate(config))
    if hook == "health":
        return _answer(
            health={"state": "ok", "routes": len(config.get("default_routes") or [])}
        )
    print(f"screenshots does not implement {hook!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
