#!/usr/bin/env python3
"""screenshots: declare `/shot` and turn it into worker-side work (#179, a #113 reference).

Taking a screenshot needs a browser, a running stack, and the project's own routes — none of
which belong in a poll cycle. So this extension does the small part the engine needs (declare
the command, decide what was asked for) and hands the actual work to the worker, where it runs
inside the session that already holds the card's lease. Per #112 that lease *is* the boundary
for worker-side procedures, so no second boundary is invented here.

`/shot` with no arguments falls back to the project's configured routes; `/shot /checkout /account`
asks for exactly those. Either way the result is an ordinary queued instruction, so it obeys
ownership, the worker cap, and the stop protocol like anything else.

Hooks: validate-config, command, health. Stdlib only.
"""

from __future__ import annotations

import json
import sys

WORD = "/shot"


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
                "what": "is empty, so a bare /shot would ask for nothing",
                "fix": "list at least one route",
            }
        ]
    return []


def _requested_routes(command: str, config: dict) -> list[str]:
    """The routes a `/shot` asked for: its arguments, or the project's defaults."""
    arguments = [part for part in command.split()[1:] if part.startswith("/")]
    if arguments:
        return arguments
    return [str(route) for route in config.get("default_routes") or ["/"]]


HOOKS = ['command', 'health', 'validate-config']


def main(argv: list[str]) -> int:
    hook = argv[1] if len(argv) > 1 else ""
    payload = _read_payload()
    config = payload.get("config") or {}
    context = payload.get("context") or {}

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
    if hook == "command":
        if context.get("declare"):
            return _answer(
                commands=[
                    {
                        "word": WORD,
                        "tier": "work",
                        "help": "screenshot one or more routes: /shot /checkout /account",
                    }
                ]
            )
        command = str(context.get("command") or WORD)
        routes = _requested_routes(command, config)
        return _answer(
            requests=[
                {
                    "kind": "queue-work",
                    "payload": {
                        # The routes are part of the identity: asking for a different page is
                        # different work, but asking twice for the same page is not.
                        "key": "shot:" + ",".join(routes),
                        "instruction": (
                            "Take screenshots of "
                            + ", ".join(routes)
                            + " using this project's screenshot procedure, and attach them."
                        ),
                    },
                },
                {
                    "kind": "comment",
                    "payload": {
                        "body": "Queued screenshots of " + ", ".join(f"`{r}`" for r in routes) + "."
                    },
                },
            ]
        )
    print(f"screenshots does not implement {hook!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
