"""The shim itself: one shadowed program, queued or passed straight through.

One rule governs every path out of here, and it is the one #1287 says must not
break: **fail open**.  A worker that cannot run its tests because the deck's
runtime is down is a worse failure than the pile-up this exists to prevent, so
anything unexpected — no runtime, an odd argv, a program this shim cannot even
find — ends in the real program running exactly as it would have.

Only a command that is recognisably a whole suite, with the runtime up and no
escape hatch set, is refused; and a refusal always says what to run instead.
"""

from __future__ import annotations

import os
import sys

from .classify import command_string, is_heavy, queue_advice, runtime_is_up
from .install import EXIT_NOT_FOUND, EXIT_REFUSED, RECURSION_GUARD, real_program

SLOT_HELD_ENV = "AGENTDECK_HEAVY_SLOT_HELD"


def _bypassed() -> bool:
    """Whether something has already accounted for this command's slot.

    ``AGENTDECK_HEAVY_SLOT_HELD`` is the documented escape hatch and is also
    what ``jobs.py`` sets on a queued job's child, so a suite the queue itself
    started is never refused by the shim it inherited.
    """
    if os.environ.get(SLOT_HELD_ENV, "").strip() not in ("", "0"):
        return True
    return os.environ.get(RECURSION_GUARD, "").strip() not in ("", "0")


def _passthrough(program: str, arguments: list[str]) -> int:
    """Become the real program. Returns only if it could not be run at all."""
    target = real_program(program)
    if target is None:
        print(f"agentdeck heavy-command shim: {program}: not found", file=sys.stderr)
        return EXIT_NOT_FOUND
    environment = dict(os.environ)
    environment[RECURSION_GUARD] = "1"
    try:
        os.execve(target, [program, *arguments], environment)
    except OSError as exc:  # pragma: no cover - exec of a resolved binary
        print(f"agentdeck heavy-command shim: {program}: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    return EXIT_NOT_FOUND


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return EXIT_NOT_FOUND
    program, arguments = args[0], args[1:]
    if _bypassed():
        return _passthrough(program, arguments)
    command = command_string(program, arguments)
    if not is_heavy(command):
        return _passthrough(program, arguments)
    if not runtime_is_up():
        # The queue is not there to take it. Refusing now would leave every
        # project on this host unable to test at all.
        return _passthrough(program, arguments)
    cwd = os.getcwd()
    print(queue_advice(command, cwd), file=sys.stderr)
    return EXIT_REFUSED
