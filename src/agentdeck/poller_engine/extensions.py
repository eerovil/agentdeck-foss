"""Poller Extensions: out-of-process hooks that may only ask (#179, designed in #112).

An extension is a separate program, not an import. The engine hands it a read-only snapshot
on stdin at one of a few named hooks and reads a structured answer on stdout. That boundary
is what makes the engine's invariants safe from a buggy extension *structurally* rather than
by discipline. (It once also kept the engine a stdlib-only zipapp, #116; that reason is gone
since #2153 and #2597.)

Three properties hold by construction here:

* **Read-only in.** The snapshot is serialized and copied on the way out, so nothing an
  extension does to its own copy can reach engine state, queues, leases or locks.
* **Request-only out.** An answer never *is* an action; it is a request. The engine parses
  requests into typed values and then runs each one through the same ownership,
  authorization, queue, lease and release rules as any other input — so an extension can ask
  for work but can never bypass a lease, authorize an unauthorized command, or skip a release
  gate. An unrecognized request kind is refused rather than guessed at.
* **Never crash the cycle.** A crash, timeout, non-zero exit, unparseable answer, or wrong
  shape means: skip this extension, mark it health-degraded, carry on. This is the same
  posture as the rest of the engine — skip and count what you cannot understand rather than
  failing a whole scan over it.

Every call also carries a live-vs-shadow mode, and in shadow mode requests are *reported and
never honored*, so #109's dry-run poll is safe without each extension having to remember to
behave.

What a project approves is **policy**, never one build of the program (#2158). Its Manifest
lists the hooks each enabled extension may be called at (``hooks = [...]`` in the extension's
block), and that list — covered by the Manifest fingerprint like every other setting — is the
whole of an extension's authority. The program's own declaration only confirms it still
implements what was allowed: a hook it newly declares is ignored until the Manifest allows it
and the project is applied, and an allowed hook it no longer implements refuses the extension.
The program's bytes are not approved at all, so fixing a shared extension deploys like any
other AgentDeck code instead of quarantining it in every project that enables it.

An extension that is missing, will not say what it implements, or no longer implements an
allowed hook is **refused** at a wake rather than refusing the wake: it is loaded so the wake
knows what the project is missing, and then invoked at no hook at all — `invoke` refuses it at
the one place every hook call passes through. The instance reports it degraded, and a refused
release gate contributes no deploy state, so its release waits rather than finishing on no
evidence. That is runtime health, not an approval state.

The trust model is a trusted-operator one (#112): this contains bugs, crashes and timeouts.
It does not sandbox a malicious extension, which remains a real program on the host — V1
assumes the operator wrote or reviewed their own.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .config import Problem
from .github_authority import TOKEN_VARS

DEFAULT_TIMEOUT_S = 20
# The most an observe hook may ask the engine to remember for it, serialized. It rides in the
# engine's state file, so a runaway extension must not be able to grow that without bound.
MAX_MEMORY_BYTES = 64 * 1024


# The op an extension answers with the hooks it implements. Asking is how the engine learns what a
# program does without the Manifest having to restate it — and without the engine guessing by
# calling hooks that may not exist.
DECLARE_OP = "hooks"

# Where a project keeps its extensions, relative to a checkout (#112's conventional folder).
EXTENSION_DIR = ("poller", "extensions")


class Hook(Enum):
    """The fixed, small set of hooks an extension may implement (#112)."""

    VALIDATE_CONFIG = "validate-config"  # check its own settings into the shared report (#108)
    PROVISION = "provision"              # idempotent, add-only setup at activation (#109)
    HEALTH = "health"                    # report its own health for the record (#107)
    OBSERVE = "observe"                  # observe external state and propose work
    PROPOSE = "propose"                  # turn one operator-supplied ref into proposal requests
    RELEASE_GATE = "release-gate"        # report deploy/CI status to the release machine
    COMMAND = "command"                  # declare and handle an extension command (#110)


class Mode(Enum):
    LIVE = "live"      # requests may be honored
    SHADOW = "shadow"  # requests are reported only — the dry-run poll (#109)


class RequestKind(Enum):
    """What an extension may ask for. Anything else is refused."""

    QUEUE_WORK = "queue-work"          # propose a work item on a card that already exists
    # Propose work with no card to put it on: the engine opens one, then treats it as ordinary
    # work (#393). Separate from QUEUE_WORK because opening a card is an external side effect
    # that must happen exactly once, while queueing onto an existing one is idempotent.
    OPEN_WORK = "open-work"
    RELEASE_STATUS = "release-status"  # report ok / bad / pending for a watched deploy
    COMMENT = "comment"                # ask for a comment on a card
    NOTE = "note"                      # record something in the health report


@dataclass(frozen=True)
class Request:
    """One thing an extension asked for. The engine decides whether it happens."""

    kind: RequestKind
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Extension:
    """One enabled extension: what to run, and the hooks this project allows it to be called at.

    ``hooks`` is the Manifest's allow-list, never more (#2158). ``ignored_hooks`` are hooks the
    program declares that the Manifest does not allow — shown, never called. ``source`` is the
    file that resolved, for diagnostics. ``refused`` is why this program may not run at all —
    empty when it may — and every hook call refuses it.
    """

    name: str
    program: tuple[str, ...]
    hooks: frozenset[Hook] = frozenset()
    config: dict = field(default_factory=dict)
    source: str = ""
    ignored_hooks: frozenset[Hook] = frozenset()
    refused: str = ""

    def implements(self, hook: Hook) -> bool:
        return hook in self.hooks


@dataclass(frozen=True)
class DeclaredCommand:
    """A command an extension exposes, and the authorization tier it requires (#110)."""

    word: str
    tier: str = "work"
    help: str = ""


@dataclass(frozen=True)
class Response:
    """An extension's answer, already reduced to what the engine will consider.

    ``ok`` false means the extension was skipped: ``degraded`` says why, and ``requests`` is
    empty, because a broken extension's output is never partially trusted.
    """

    ok: bool
    requests: tuple[Request, ...] = ()
    problems: tuple[Problem, ...] = ()
    health: dict = field(default_factory=dict)
    commands: tuple[DeclaredCommand, ...] = ()
    degraded: str = ""
    # What an observe hook asked the engine to hand back to it next wake (#2691), or None
    # when it asked nothing. The engine keeps it in its own state, so an extension needs no
    # store of its own, and a dry run never keeps it.
    memory: dict | None = None


# The runner is injected so the protocol can be tested against real programs and fakes alike.
# ``argv`` and the payload go in; exit code, stdout and stderr come back.
Runner = Callable[[list[str], str, float], tuple[int, str, str]]


def run_program(argv: list[str], payload: str, timeout: float) -> tuple[int, str, str]:
    """Run an extension program with its payload on stdin (the default runner).

    Without any inherited GitHub credential: an extension's only GitHub identity is
    the ``github_token_file`` the engine hands it (#2289).
    """
    environment = {
        name: value for name, value in os.environ.items() if name not in TOKEN_VARS
    }
    try:
        done = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g}s"
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout or "", done.stderr or ""


def snapshot(context: dict) -> dict:
    """The read-only view an extension receives: a JSON round-trip, so it shares nothing.

    Anything that will not serialize is dropped rather than smuggled across as a live object,
    because a live object would be a hole in the boundary.
    """
    return json.loads(json.dumps(context, default=str))


def call_payload(extension: Extension, hook: Hook, *, context: dict, mode: Mode) -> dict:
    """Exactly what an extension is given: its own settings, the mode, and a snapshot."""
    return {
        "hook": hook.value,
        "mode": mode.value,
        "extension": extension.name,
        "config": snapshot(extension.config),
        "context": snapshot(context),
    }


def invoke(
    extension: Extension,
    hook: Hook,
    *,
    context: dict | None = None,
    mode: Mode = Mode.LIVE,
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Response:
    """Call one hook and reduce its answer, never raising and never trusting it blindly."""
    if extension.refused:
        # Checked before anything else, including whether the hook is implemented: this is the
        # one place every hook call passes through, so refusing here is what keeps a program the
        # wake could not load from running at a call site someone forgets to guard.
        return _degraded(extension, hook, extension.refused)
    if not extension.implements(hook):
        return Response(ok=True, degraded="")
    payload = json.dumps(call_payload(extension, hook, context=context or {}, mode=mode))
    run = runner or run_program
    try:
        code, out, err = run([*extension.program, hook.value], payload, timeout)
    except Exception as exc:  # a runner that itself explodes must not take the cycle with it
        return _degraded(extension, hook, f"runner failed: {exc!r}")
    if code != 0:
        return _degraded(extension, hook, _first_line(err) or f"exited {code}")
    try:
        answer = json.loads(out or "{}")
    except ValueError:
        return _degraded(extension, hook, "answered with something that is not JSON")
    if not isinstance(answer, dict):
        return _degraded(extension, hook, "answered with something that is not an object")
    return _parse(extension, hook, answer)


def _degraded(extension: Extension, hook: Hook, why: str) -> Response:
    return Response(
        ok=False,
        degraded=f"{extension.name}.{hook.value}: {why}",
        health={"state": "degraded", "reason": why},
    )


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0][:200] if (text or "").strip() else ""


def _parse(extension: Extension, hook: Hook, answer: dict) -> Response:
    """Reduce a well-formed answer; a malformed part degrades the whole call, not half of it."""
    problems: list[Problem] = []
    for raw in answer.get("problems") or []:
        if not isinstance(raw, dict):
            return _degraded(extension, hook, "a problem was not an object")
        problems.append(
            Problem(
                location=str(raw.get("location") or f"manifest.extensions.{extension.name}"),
                what=str(raw.get("what") or "reported a problem without saying what"),
                fix=str(raw.get("fix") or "check this extension's settings"),
            )
        )

    requests: list[Request] = []
    for raw in answer.get("requests") or []:
        if not isinstance(raw, dict):
            return _degraded(extension, hook, "a request was not an object")
        try:
            kind = RequestKind(str(raw.get("kind")))
        except ValueError:
            # Refuse rather than guess: an unknown kind may mean anything at all.
            return _degraded(
                extension, hook, f"asked for {raw.get('kind')!r}, which is not a request kind"
            )
        payload = raw.get("payload")
        requests.append(Request(kind=kind, payload=payload if isinstance(payload, dict) else {}))

    commands: list[DeclaredCommand] = []
    for raw in answer.get("commands") or []:
        if not isinstance(raw, dict):
            return _degraded(extension, hook, "a declared command was not an object")
        word = str(raw.get("word") or "").strip()
        if not word.startswith("/") or " " in word:
            return _degraded(
                extension, hook, f"declared {word!r}, which is not a single /command word"
            )
        commands.append(
            DeclaredCommand(
                word=word.lower(),
                tier=str(raw.get("tier") or "work"),
                help=str(raw.get("help") or ""),
            )
        )

    memory = answer.get("memory")
    if memory is not None:
        if hook is not Hook.OBSERVE or not isinstance(memory, dict):
            return _degraded(extension, hook, "answered memory that is not an observe object")
        if len(json.dumps(memory)) > MAX_MEMORY_BYTES:
            return _degraded(
                extension, hook, f"answered more than {MAX_MEMORY_BYTES} bytes of memory"
            )

    health = answer.get("health")
    return Response(
        ok=True,
        requests=tuple(requests),
        problems=tuple(problems),
        health=health if isinstance(health, dict) else {},
        commands=tuple(commands),
        memory=memory,
    )


def honored(response: Response, mode: Mode) -> tuple[Request, ...]:
    """The requests the engine may act on: none at all in shadow mode.

    A shadow poll still collects what *would* have happened, so an operator can read it, but
    nothing an extension asked for is honored — which is what makes the dry run safe without
    every extension having to implement its own dry-run behavior.
    """
    if mode is Mode.SHADOW or not response.ok:
        return ()
    return response.requests


# --- the hook allow-list: what a project approved an extension to do (#2158) ---


def parse_hooks(value: object) -> tuple[frozenset[Hook] | None, str]:
    """A Manifest ``hooks`` list as hooks, or None and what is wrong with it.

    Exact names only: an allow-list is a grant of authority, so a spelling the engine does not
    know is refused rather than guessed at, and an empty list would enable an extension that
    may do nothing.
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None, "is not a list of hook names"
    if not value:
        return None, "allows no hooks, so the engine would never call it"
    hooks = set()
    for name in value:
        try:
            hooks.add(Hook(name))
        except ValueError:
            known = ", ".join(hook.value for hook in Hook)
            return None, f"names {name!r}, which is not a hook (known: {known})"
    return frozenset(hooks), ""


# --- the contract-test harness (#112) ---

FIXTURE_CONTEXT = {
    "instance": "reference",
    # A watched build, so a release-gate hook has something concrete to answer about: a gate
    # that reports nothing for a build the engine is waiting on would stall a release forever.
    "builds": {"store": {"sha": "0" * 40}},
    "since": "2026-07-25T12:00:00+00:00",
    "cards": [
        {
            "ref": "store#7",
            "status": "In review",
            "owned": True,
            "merged": ["store#9"],
            "repos": ["store"],
            # A card a worker has already handed back twice, so a hook keyed on rounds
            # rather than on a pull request head has a non-zero count to reason about.
            "reports": 2,
        }
    ],
    "board": {"org": "example", "project_number": 1},
    "budget": {"dispatch_ok": True},
}


def contract_test(
    extension: Extension,
    *,
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    context: dict | None = None,
) -> list[Problem]:
    """Exercise every hook an extension declares against a fixture context.

    Each hook is a separate program with a structured answer, so this needs no engine and no
    network: feed it the fixture, assert the answer is a shape the engine will accept. An
    extension that passes this cannot break the cycle at the protocol level; whether its
    *judgment* is right is what #109's shadow poll is for.
    """
    problems: list[Problem] = []
    if not extension.hooks:
        problems.append(
            Problem(
                location=f"extensions.{extension.name}",
                what="declares no hooks, so the engine would never call it",
                fix="declare the hooks it implements, or stop enabling it",
            )
        )
    for hook in sorted(extension.hooks, key=lambda item: item.value):
        response = invoke(
            extension,
            hook,
            context=context or FIXTURE_CONTEXT,
            mode=Mode.SHADOW,  # a contract test must never let an extension act
            runner=runner,
            timeout=timeout,
        )
        if not response.ok:
            problems.append(
                Problem(
                    location=f"extensions.{extension.name}.{hook.value}",
                    what=response.degraded,
                    fix="answer with a JSON object on stdout and exit 0",
                )
            )
            continue
        if hook is Hook.RELEASE_GATE:
            problems.extend(_release_gate_shape(extension, response))
    return problems


_RELEASE_STATES = ("ok", "bad", "pending")


def _release_gate_shape(extension: Extension, response: Response) -> list[Problem]:
    """A release gate must speak the engine's normalized states, not its vendor's."""
    problems: list[Problem] = []
    statuses = [
        request for request in response.requests if request.kind is RequestKind.RELEASE_STATUS
    ]
    if not statuses:
        problems.append(
            Problem(
                location=f"extensions.{extension.name}.release-gate",
                what="reported no release status for the fixture card",
                fix="return a release-status request with ok, bad or pending",
            )
        )
    for request in statuses:
        state = str(request.payload.get("status") or "")
        if state not in _RELEASE_STATES:
            problems.append(
                Problem(
                    location=f"extensions.{extension.name}.release-gate",
                    what=f"reported {state!r}, which is not a release state",
                    fix=f"use one of {', '.join(_RELEASE_STATES)}",
                )
            )
    return problems


# --- the seams the engine plugs these hooks into ---


def config_validator(
    extension: Extension, *, runner: Runner | None = None, timeout: float = DEFAULT_TIMEOUT_S
) -> Callable[[dict, list], None]:
    """An extension's ``validate-config`` hook, shaped as the validator #177's loader expects.

    The loader knows nothing about any extension's settings; it hands each block to its own
    extension and folds the complaints into the one collect-all report. An extension that
    cannot be run at all is itself a problem — silently accepting settings nobody checked
    would let a broken configuration activate.
    """

    def validate(block: dict, problems: list) -> None:
        if not extension.refused and not extension.implements(Hook.VALIDATE_CONFIG):
            # The allow-list binds this call too (#2158): checking settings runs the program, so a
            # project that did not allow validate-config gets a problem, never a quiet call.
            problems.append(
                Problem(
                    location=f"manifest.extensions.{extension.name}.hooks",
                    what="does not allow validate-config, so its settings cannot be checked",
                    fix="add \"validate-config\" to its hooks, or stop enabling it",
                )
            )
            return
        candidate = Extension(
            name=extension.name,
            program=extension.program,
            hooks=extension.hooks,
            config=block,
            source=extension.source,
            refused=extension.refused,  # a refused program checks nothing either
        )
        response = invoke(
            candidate, Hook.VALIDATE_CONFIG, mode=Mode.LIVE, runner=runner, timeout=timeout
        )
        if not response.ok:
            problems.append(
                Problem(
                    location=f"manifest.extensions.{extension.name}",
                    what=f"its settings could not be checked ({response.degraded})",
                    fix="make the extension answer validate-config, or stop enabling it",
                )
            )
            return
        problems.extend(response.problems)

    return validate


def validators_for(
    enabled: list[Extension], *, runner: Runner | None = None
) -> dict[str, Callable[[dict, list], None]]:
    """The validator table for every enabled extension that implements the hook."""
    return {
        extension.name: config_validator(extension, runner=runner)
        for extension in enabled
        if extension.implements(Hook.VALIDATE_CONFIG)
    }


def declared_commands(
    enabled: list[Extension], *, runner: Runner | None = None, mode: Mode = Mode.LIVE
) -> dict[str, tuple[Extension, DeclaredCommand]]:
    """Ask each command-hook extension what it exposes, keyed by the command word.

    Two extensions cannot claim the same word: the first one wins and the second is simply
    not registered, because guessing which of them a human meant is worse than ignoring one.
    """
    table: dict[str, tuple[Extension, DeclaredCommand]] = {}
    for extension in enabled:
        if not extension.implements(Hook.COMMAND):
            continue
        response = invoke(
            extension, Hook.COMMAND, context={"declare": True}, mode=mode, runner=runner
        )
        if not response.ok:
            continue
        for command in response.commands:
            table.setdefault(command.word, (extension, command))
    return table


def release_states(
    enabled: list[Extension],
    *,
    context: dict,
    mode: Mode = Mode.LIVE,
    runner: Runner | None = None,
) -> tuple[dict[str, dict], list[str]]:
    """Collect normalized deploy states from the release-gate hooks.

    Returns the states by repository and the names of extensions that were skipped. A skipped
    gate deliberately contributes *nothing* rather than a guess: an unknown deploy is left
    pending, which keeps the release waiting instead of finishing on missing evidence.

    Reading a reported state is not acting on it, so states are collected in shadow mode too —
    what the caller does with them is where live and shadow differ.
    """
    states: dict[str, dict] = {}
    degraded: list[str] = []
    for extension in enabled:
        if not extension.implements(Hook.RELEASE_GATE):
            continue
        response = invoke(
            extension, Hook.RELEASE_GATE, context=context, mode=mode, runner=runner
        )
        if not response.ok:
            degraded.append(extension.name)
            continue
        for request in response.requests:
            if request.kind is not RequestKind.RELEASE_STATUS:
                continue
            repo = str(request.payload.get("repo") or "")
            status = str(request.payload.get("status") or "pending")
            if repo and status in _RELEASE_STATES:
                states[repo] = {"status": status, "link": request.payload.get("link")}
    return states, degraded


# --- discovery: finding the programs a Manifest enables ---


def declared_hooks(
    program: tuple[str, ...], *, runner: Runner | None = None, timeout: float = DEFAULT_TIMEOUT_S
) -> frozenset[Hook] | None:
    """Ask a program which hooks it implements, or None when it cannot say.

    The engine asks rather than assuming, because calling a hook a program does not implement would
    make "unimplemented" indistinguishable from "broken" — and an extension that cannot say what it
    does is not something to guess about.
    """
    run = runner or run_program
    try:
        code, out, _err = run([*program, DECLARE_OP], "{}", timeout)
    except Exception:
        return None
    if code != 0:
        return None
    try:
        answer = json.loads(out or "{}")
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    names = answer.get("hooks")
    if not isinstance(names, list):
        return None
    hooks = set()
    for name in names:
        try:
            hooks.add(Hook(str(name)))
        except ValueError:
            return None  # a hook this engine does not know: refuse rather than partially load
    return frozenset(hooks)


def program_for(name: str, roots: list[Path], extra_dirs: Sequence[Path] = ()) -> Path | None:
    """The program implementing this extension.

    ``roots`` are project checkouts, searched at the conventional ``poller/extensions`` folder
    inside each. ``extra_dirs`` are folders holding extension programs directly, which is how a
    host points every instance at one shared set instead of vendoring copies per project (#377).

    Checkouts are searched **first**, so a project that has deliberately vendored its own version
    of a program keeps getting it, and adding a shared folder can never change what an existing
    installation resolves.
    """
    filename = f"{name.replace('-', '_')}.py"
    candidates = [Path(root).expanduser().joinpath(*EXTENSION_DIR, filename) for root in roots]
    candidates += [Path(folder).expanduser() / filename for folder in extra_dirs]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def discover(
    enabled: dict,
    roots: list[Path],
    *,
    allowed: Mapping[str, frozenset[Hook]] | None = None,
    extra_dirs: Sequence[Path] = (),
    runner: Runner | None = None,
    interpreter: str | None = None,
    wake: bool = False,
) -> tuple[list[Extension], list[Problem]]:
    """Load exactly the extensions the Manifest enables, at the hooks it allows them.

    Only listed names are loaded — a program sitting in the folder that nobody enabled is not run
    (#112). ``allowed`` is each extension's hook allow-list (#2158); an extension's hooks are
    that list and nothing else. The program is asked which hooks it implements only to confirm
    it still implements every allowed one: one it declares beyond the list is recorded as
    ignored, never called.

    What cannot be loaded — no allow-list, no program, a program that will not say what it
    implements, or one missing an allowed hook — is a problem for the offline check and for
    activation, because a project that activates believing an extension is watching something,
    when nothing is, is worse than one that refuses. At a ``wake`` the same extension is loaded
    **refused** instead: it carries its allowed hooks so a release gate is still known to be
    missing, answers nothing, and the rest of the wake carries on.

    ``extra_dirs`` are shared extension folders the host named, searched after the checkouts
    (#377). Which file resolved is recorded as ``source``; its bytes are not approved.
    """
    allowed = allowed or {}
    found: list[Extension] = []
    problems: list[Problem] = []
    for name, block in sorted(enabled.items()):
        settings = block if isinstance(block, dict) else {}
        permitted = allowed.get(name)
        path = program_for(name, roots, extra_dirs)
        program = (
            ((interpreter, str(path)) if interpreter else (str(path),)) if path else ()
        )

        declared: frozenset[Hook] | None = None
        why = _unloadable(name, permitted, path)
        if why is None:
            declared = declared_hooks(program, runner=runner)
            why = _unimplemented(path, permitted, declared)
        if why is not None:
            what, fix = why
            if not wake:
                problems.append(Problem(location=f"manifest.extensions.{name}", what=what, fix=fix))
                continue
            found.append(
                Extension(
                    name=name,
                    program=program,
                    hooks=permitted or frozenset(),
                    config=settings,
                    source=str(path or ""),
                    refused=what,
                )
            )
            continue
        found.append(
            Extension(
                name=name,
                program=program,
                hooks=permitted,
                config=settings,
                source=str(path),
                ignored_hooks=declared - permitted,
            )
        )
    return found, problems


def _unloadable(
    name: str, permitted: frozenset[Hook] | None, path: Path | None
) -> tuple[str, str] | None:
    """Why an extension cannot even be asked what it implements, as (what, fix), or None."""
    if permitted is None:
        return (
            "states no hooks, so nothing says what this project allows it to do",
            f"list them under [extensions.{name}] as hooks = [...], then apply",
        )
    if path is None:
        return (
            "no program for it was found in any checkout's poller/extensions "
            "folder, nor in a shared extensions folder this host named",
            f"add {name.replace('-', '_')}.py there, set overlay.paths.extensions "
            "to the folder holding it, or stop enabling it",
        )
    return None


def _unimplemented(
    path: Path, permitted: frozenset[Hook], declared: frozenset[Hook] | None
) -> tuple[str, str] | None:
    """Why a program cannot serve the hooks it was allowed, as (what, fix), or None."""
    if declared is None:
        return (
            f"{path} would not say which hooks it implements",
            f"make it answer `{DECLARE_OP}` with a JSON list of hook names",
        )
    missing = permitted - declared
    if missing:
        names = ", ".join(sorted(hook.value for hook in missing))
        return (
            f"{path} does not implement {names}, which the Manifest allows",
            "restore the hook in the program, or remove it from hooks and apply",
        )
    return None
