"""What counts as a heavy command, in one place for every enforcement half.

There are two enforcement points for the queue of #1287 and they must agree.
``scripts/heavy_command_hook.py`` sees a Claude Code ``Bash`` call as a single
command string before the shell runs it; ``agentdeck.heavy_shim`` sees a Codex
worker's already-split argv, because a Codex worker has no hook and is caught
by a shadowing executable on its PATH instead.  Two notions of "heavy" would
mean a suite refused under one provider and started under the other, so both
sides reduce what they have to a command string and ask this module.

Deliberately stdlib-only and free of package imports: the hook runs under the
system interpreter with no AgentDeck on its path and loads this file directly.
"""

from __future__ import annotations

import re
import shlex
import socket

# Commands worth queueing: whole suites and builds. Matched only at the start of
# a command segment, and with leading `VAR=value` assignments skipped, so
# `rg pytest src/` and `cat tests/test_jobs.py` are not suites — a false positive
# here stops someone reading their own code.
_HEAVY_BODY = r"""\s*(?:\w+=\S+\s+)*(?:
        (?:(?:uv|poetry|pipenv)\ run\ )?
        (?:(?:[\w./-]*/)?python(?:\d+(?:\.\d+)*)?\ -m\ )?
        (?:[\w./-]*/)?pytest\b
      | (?:[\w./-]*/)?pytest\.sh(?=\s|$)
      | (?:(?:[\w./-]*/)?python(?:\d+(?:\.\d+)*)?\ )?(?:[\w./-]*/)?manage\.py\ test(?=\s|$)
      | (?:npm|pnpm|yarn|bun)\ (?:run\ )?test\b
      | npx\ playwright\ test
      | (?:npx\ )?(?:vitest|jest)\b
      | cargo\ test\b
      | go\ test\b
      | tox\b
      | make\ (?:test|check)\b
    )"""
HEAVY = re.compile(rf"(?:^|[;&|]\s*){_HEAVY_BODY}", re.VERBOSE)
_HEAVY_SEGMENT = re.compile(rf"^{_HEAVY_BODY}", re.VERBOSE)

# Already going through the queue, or explicitly carrying a nonzero escape
# hatch on the same command segment. This must be segment-aware: prose such as
# `echo AGENTDECK_HEAVY_SLOT_HELD; pytest -q` does not exempt the later suite,
# and the documented value `0` means the hatch is off.
_EXEMPT_BODY = (
    r"\s*(?:\w+=\S+\s+)*(?:"
    r"agentdeck\s+job\b|"
    r"AGENTDECK_HEAVY_SLOT_HELD=(?!0(?:\s|$))\S+(?:\s|$)"
    r")"
)
EXEMPT = re.compile(rf"(?:^|[;&|]\s*){_EXEMPT_BODY}")
_EXEMPT_SEGMENT = re.compile(rf"^{_EXEMPT_BODY}")

# The programs a command in HEAVY can begin with. The shim installs one
# shadowing launcher per name, so this list is derived from that pattern rather
# than invented beside it: a program missing here is a suite nothing catches.
SHIMMED_PROGRAMS = (
    "pytest",
    "uv",
    "poetry",
    "pipenv",
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "npx",
    "vitest",
    "jest",
    "cargo",
    "go",
    "tox",
    "make",
    # Not heavy themselves, but each can start a suite the patterns above only
    # see once the wrapper is taken off (#2666), so a Codex worker meets them too.
    "podman",
    "docker",
    "timeout",
)

# Programs that only start another command, peeled off before matching (#2666):
# `timeout 1200 podman exec web bash -lc "./pytest.sh …"` is a suite, and it
# ran unqueued because the segment began with `timeout`.
_CONTAINER_TOOLS = frozenset({"podman", "docker"})
_COMPOSE_TOOLS = frozenset({"docker-compose", "podman-compose"})
_CONTAINER_VERBS = frozenset({"exec", "run"})
# Container options that take their value as the next word. Anything else that
# starts with `-` is a switch, or carries its value after `=`.
_CONTAINER_VALUE_FLAGS = frozenset(
    {
        "-v",
        "--volume",
        "-w",
        "--workdir",
        "-e",
        "--env",
        "--env-file",
        "--network",
        "--net",
        "--name",
        "-u",
        "--user",
        "-p",
        "--publish",
        "--entrypoint",
        "--mount",
        "--pod",
        "-l",
        "--label",
        "-m",
        "--memory",
        "--cpus",
        "--tmpfs",
        "--device",
        "--userns",
        "--security-opt",
        "--cap-add",
        "--cap-drop",
        "--add-host",
        "--hostname",
        "-h",
        "--ipc",
        "--pid",
        "--platform",
        "--pull",
        "--restart",
        "--shm-size",
        "--ulimit",
        "--volumes-from",
        "--detach-keys",
        "-f",
        "--file",
        "--project-name",
        "--profile",
        "--env-from",
        "--log-driver",
        "--log-opt",
    }
)
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
# How deep a `bash -c` inside a container inside a `bash -c` is followed.
_MAX_UNWRAP_DEPTH = 3


def command_string(program: str, arguments: list[str] | tuple[str, ...]) -> str:
    """Rebuild the command line a shim was invoked as.

    The shell has already split and expanded it, so this is not the text the
    agent typed — but it is the text the machine is about to run, which is the
    honest thing to judge and the thing HEAVY is written against.
    """
    return shlex.join([program, *arguments])


def is_heavy(command: str) -> bool:
    """Whether ``command`` is a whole suite that belongs on the queue."""
    return _is_heavy(command, 0)


def _is_heavy(command: str, depth: int) -> bool:
    for tokens_in_segment in _segments(command):
        segment = shlex.join(tokens_in_segment)
        if _EXEMPT_SEGMENT.search(segment):
            continue
        if _HEAVY_SEGMENT.search(segment):
            return True
        inner = _unwrap(tokens_in_segment)
        if not inner or depth >= _MAX_UNWRAP_DEPTH:
            continue
        # A `bash -c` script is a command line of its own; the rest of a wrapper is one segment.
        if _is_heavy(inner if isinstance(inner, str) else shlex.join(inner), depth + 1):
            return True
    return False


def _segments(command: str) -> list[list[str]]:
    """``command``'s words, split into the segments `;`, `&` and `|` separate."""
    if not command:
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        tokens = [command]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and not token.strip(";&|"):
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    return segments


def runs_in_container(command: str, depth: int = 0) -> bool:
    """Whether some segment of ``command`` starts its work in a container."""
    for tokens in _segments(command):
        while tokens and depth <= _MAX_UNWRAP_DEPTH:
            inner = _unwrap(tokens)
            if inner is None:
                break
            words = [word for word in tokens if not re.fullmatch(r"\w+=\S*", word)]
            program = words[0].rsplit("/", 1)[-1] if words else ""
            if program in _CONTAINER_TOOLS or program in _COMPOSE_TOOLS:
                return True
            if isinstance(inner, str):
                if runs_in_container(inner, depth + 1):
                    return True
                break
            tokens = inner
            depth += 1
    return False


def _unwrap(tokens: list[str]) -> list[str] | str | None:
    """The command a wrapper segment starts, or ``None`` when it is not a wrapper.

    Returns the remaining words for `timeout`/`nice`/`env`/`nohup` and for a
    container's `exec`/`run`, or the script string for `bash -c`. Peels one
    layer; the caller recurses. Anything it cannot read is ``None`` — not heavy,
    the same fail-open stance as the rest of this module.
    """
    words = list(tokens)
    while words and re.fullmatch(r"\w+=\S*", words[0]):
        words.pop(0)
    if not words:
        return None
    program = words[0].rsplit("/", 1)[-1]
    rest = words[1:]
    if program == "timeout":
        rest = _skip_options(rest, value_flags={"-s", "--signal", "-k", "--kill-after"})
        return rest[1:] if rest else None
    if program == "nice":
        rest = _skip_options(rest, value_flags={"-n", "--adjustment"})
        return rest or None
    if program == "nohup":
        return rest or None
    if program == "env":
        rest = _skip_options(rest, value_flags={"-u", "--unset", "-C", "--chdir"})
        while rest and re.fullmatch(r"\w+=\S*", rest[0]):
            rest.pop(0)
        return rest or None
    if program in _CONTAINER_TOOLS or program in _COMPOSE_TOOLS:
        if program in _CONTAINER_TOOLS:
            rest = _skip_options(rest, value_flags=_CONTAINER_VALUE_FLAGS)
            if rest and rest[0] == "compose":
                rest = _skip_options(rest[1:], value_flags=_CONTAINER_VALUE_FLAGS)
        if not rest or rest[0] not in _CONTAINER_VERBS:
            return None
        rest = _skip_options(rest[1:], value_flags=_CONTAINER_VALUE_FLAGS)
        # The container, image or compose service name.
        return rest[1:] or None
    if program in _SHELLS:
        for index, word in enumerate(rest):
            if not word.startswith("-"):
                return None
            if word.startswith("--"):
                continue
            if "c" in word[1:]:
                return rest[index + 1] if index + 1 < len(rest) else None
        return None
    return None


def _skip_options(words: list[str], *, value_flags) -> list[str]:
    """``words`` past the leading options, a value-taking one with its value."""
    index = 0
    while index < len(words) and words[index].startswith("-") and words[index] != "-":
        if words[index] == "--":
            return words[index + 1 :]
        if words[index] in value_flags:
            index += 1
        index += 1
    return words[index:]


def runtime_socket() -> str:
    """Return the runtime socket used by the host-wide job queue."""
    import os

    configured = os.environ.get("AGENTDECK_CODEX_SOCKET")
    if configured:
        return os.path.expanduser(configured)
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache")
    return os.path.join(base, "agentdeck", "codex-runtime.sock")


def runtime_is_up() -> bool:
    """Probe the queue listener, failing open on stale or inaccessible sockets."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(runtime_socket())
    except (OSError, TimeoutError):
        return False
    return True


def queue_advice(command: str, cwd: str) -> str:
    """The refusal text. Identical on both halves on purpose: a worker that
    meets this under Codex and under Claude Code is told to do the same thing.
    """
    advice = (
        "This host runs whole test suites through AgentDeck's queue, because several "
        "of them at once take the machine down (issue #1287).\n\n"
        f"Submit it instead:\n\n    agentdeck job submit --cwd {cwd} -- {command}\n\n"
        "That returns straight away with a job id. You can carry on with other work or "
        "end your turn — AgentDeck sends this chat a message when the job finishes, "
        "with the exit code and the output.\n\n"
    )
    lanes = _remote_lanes(cwd)
    if runs_in_container(command) and lanes:
        # The pod lane takes a bare pytest only, so a container command queued as
        # typed would run here on the one local slot (#2666).
        advice += (
            "This command runs its tests inside a local container, so the queue can only "
            "run it here, on the one local slot. From this checkout a bare pytest runs in "
            "a throwaway cluster pod that already has the container's dependencies and "
            "services, so leave the container out and submit:\n\n"
            f"    agentdeck job submit --cwd {cwd} -- pytest <the same test arguments>\n\n"
        )
    elif _is_pytest(command) and lanes:
        # Only pytest has somewhere else to go, only from a checkout a lane accepts, and
        # only to a lane this host has set up — advertising more earns a refusal (#3623).
        advice += (
            "That submission runs off this host by default, in a throwaway cluster pod "
            "(issue #1368). "
        )
        if "mac" in lanes:
            advice += "Add --where mac for a pod on the Mac lane when the others are busy, or "
        else:
            advice += "Add "
        advice += (
            "--where local to run it here instead — worth doing only when the clusters "
            "are backed up or down, because only one local job runs at a time and yours "
            "waits its turn.\n\n"
        )
    else:
        advice += "Only one local job runs at a time, so yours waits its turn.\n\n"
    return advice + (
        "If you truly need it inline (a single quick test, a debugger), prefix the "
        "command with AGENTDECK_HEAVY_SLOT_HELD=1 to bypass this."
    )


def _remote_lanes(cwd: str) -> list[str]:
    """The remote lanes this host has set up that take a pytest from ``cwd`` (#1383, #3623).

    The settings are read here rather than through ``jobs``: this module's source is
    copied verbatim into the generated worker guard, which runs with no AgentDeck
    package on its path. Keep it in step with ``jobs.lane_settings`` and
    ``jobs.kube_runners``; a stale copy costs a wrong sentence, not a wrong refusal,
    because ``jobs`` decides.

    Only lanes ``job-lanes.toml`` lists count, so a host without the file gets local
    advice. The agentdeck checkout ships ``scripts/kube_pytest.sh`` and may use every
    listed lane; another project is listed with its runner and its own ``lanes`` (kube
    if absent), and a runner that serves several nested checkouts lists them as
    ``suites`` — only those may go (#2666).
    """
    import os
    import tomllib

    path = os.path.expanduser(
        os.environ.get("AGENTDECK_JOB_LANES") or "~/.config/agentdeck/job-lanes.toml"
    )
    try:
        with open(path, "rb") as handle:
            settings = tomllib.load(handle)
    except (OSError, ValueError):
        settings = {}
    listed = [lane for lane in ("kube", "mac", "desktop") if lane in (settings.get("lanes") or {})]
    if not listed:
        return []
    projects = settings.get("projects") or {}
    runners = {
        str(table["runner"]): table
        for table in projects.values()
        if isinstance(table, dict) and isinstance(table.get("runner"), str)
    }
    start = os.path.realpath(cwd)
    directory = start
    while True:
        if os.path.isfile(os.path.join(directory, "scripts/kube_pytest.sh")):
            return listed
        for runner, table in runners.items():
            if os.path.isfile(os.path.join(directory, runner)):
                suites = table.get("suites") or {}
                nested = os.path.relpath(start, directory).split(os.sep)[0]
                if suites and nested not in suites:
                    return []
                allowed = table.get("lanes") or ["kube"]
                return [lane for lane in listed if lane in allowed]
        parent = os.path.dirname(directory)
        if parent == directory:
            return []
        directory = parent


def _is_pytest(command: str) -> bool:
    parts = command.split()
    if parts[:2] == ["uv", "run"]:
        parts = parts[2:]
    head = parts[0] if parts else ""
    return head == "pytest" or head.endswith("/pytest")
