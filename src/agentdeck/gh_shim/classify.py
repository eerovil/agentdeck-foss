"""Decide what one ``gh`` invocation is, and what identifies its answer.

Three questions, kept together because they share one table: is this argv a read
worth caching, a write that passes through, or a poll that should be refused
outright; and if it is a read, which other invocations mean the same question.
A write passes through untouched with one exception: ``gh issue create`` in a
repository a poller manages gains that project's ownership label (#2505).

The allowlist is deliberately closed.  An unrecognized subcommand passes through
uncached rather than being guessed at, because a wrong cache hit is a wrong
answer while an uncached read is only an expensive one.  `gh`'s subcommand set
is versioned outside this repo and grows without telling us.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
from dataclasses import dataclass

from .. import github_identity
from ..poller_engine import github_authority
from ..poller_engine.rate_limits import credential_identity as _credential_identity

# Read commands worth serving from cache, as (group, verb) pairs.  Everything
# here answers a question about state that already exists on GitHub; nothing
# here changes it.  Taken from the same ground the `ghx` project's allowlist
# covers (#814), minus the verbs this deck's workers never run.
READ_COMMANDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("issue", "view"),
        ("issue", "list"),
        ("issue", "status"),
        ("pr", "view"),
        ("pr", "list"),
        ("pr", "status"),
        ("pr", "checks"),
        ("pr", "diff"),
        ("repo", "view"),
        ("run", "list"),
        ("run", "view"),
        ("workflow", "list"),
        ("workflow", "view"),
        ("release", "list"),
        ("release", "view"),
        ("label", "list"),
        ("search", "issues"),
        ("search", "prs"),
        ("search", "repos"),
        ("search", "commits"),
        ("search", "code"),
    }
)

# Flags that turn a read into a wait.  These are polls by construction — the
# whole failure mode this shim exists to stop — so they are refused rather than
# cached, which would only make the loop cheap enough to keep.
WATCH_FLAGS: frozenset[str] = frozenset({"--watch", "--watch-interval"})

# Subcommands that *are* the wait, with no flag to spot it by.  These have to be
# named as their own case: they are not in READ_COMMANDS, so without this they
# would fall through to the passthrough branch and run as an unbounded,
# uncached poll — the exact loop this shim exists to stop.
WATCH_COMMANDS: frozenset[tuple[str, str]] = frozenset(
    {("run", "watch"), ("pr", "checks-watch")}
)

# Flags that only change how the answer is printed, not which answer is
# fetched.  They still take part in the key: `gh` applies them server-side or
# inside its own formatter, and this shim caches `gh`'s stdout rather than a
# parsed document, so two spellings genuinely produce two different stdouts.
# Filtering them out locally instead would need a jq implementation here; that
# is a later optimization, not a correctness requirement (#814).
_FORMAT_FLAGS: frozenset[str] = frozenset({"--jq", "-q", "--template", "-t"})

# Flags whose value is a comma-separated set, where order carries no meaning.
_SET_VALUED_FLAGS: frozenset[str] = frozenset({"--json", "--label", "-l"})

# How long a locally resolved repository remote may be trusted.  It is read
# from git config, costs no API call, and cannot change under a worker mid-task.
_REMOTE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Invocation:
    """What the shim decided about one argv.

    ``kind`` is ``read`` (serve from cache), ``write`` (pass through, then
    invalidate) or ``refuse`` (a watch loop).  ``key`` is set only for a read.
    ``invalidates`` names the key prefix a write makes stale.
    """

    kind: str
    key: str = ""
    invalidates: str = ""
    reason: str = ""
    # The subcommand this argv resolved to.  Carried rather than re-derived by
    # the caller: a second, simpler parser disagrees with this one the moment a
    # flag takes a value (`gh --repo o/x pr view` reads as group "o/x"), and
    # the two answers then key and interpret the same call differently.
    group: str = ""
    verb: str = ""


def _split_flags(args: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Separate positionals from flags, normalizing ``--flag=value`` spellings.

    A flag's value is only consumed when it does not itself look like a flag,
    so a boolean flag followed by a positional keeps them apart.
    """
    positionals: list[str] = []
    flags: list[tuple[str, str]] = []
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            positionals.append(token)
            index += 1
            continue
        if "=" in token:
            name, _, value = token.partition("=")
            flags.append((name, value))
            index += 1
            continue
        following = args[index + 1] if index + 1 < len(args) else ""
        if following and not following.startswith("-"):
            flags.append((token, following))
            index += 2
        else:
            flags.append((token, ""))
            index += 1
    return positionals, flags


def _normalize(name: str, value: str) -> str:
    """Render one flag so that equivalent spellings render identically."""
    if name in _SET_VALUED_FLAGS and value:
        members = sorted({part.strip() for part in value.split(",") if part.strip()})
        return f"{name}={','.join(members)}"
    return f"{name}={value}" if value else name


def repo_identity(cwd: str | None = None, remote: str | None = None) -> str:
    """Which repository a bare ``gh issue view 5`` is actually asking about.

    Resolved from git's own config rather than `gh repo view`, because asking
    GitHub which repository we are in would spend the quota this shim exists to
    protect.  An unresolvable remote falls back to the directory itself: two
    checkouts then key separately, which costs a duplicate fetch but never
    serves one repository's answer for another's.
    """
    if remote is not None:
        return remote
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=_REMOTE_TIMEOUT_S,
            check=False,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd or ""
    resolved = (done.stdout or "").strip()
    return resolved or (cwd or "")


def repo_flag(argv: list[str]) -> str:
    """The ``--repo owner/name`` this invocation names, or ``""``.

    Split out from the cache key because the credential has to be chosen before
    anything else happens (#2105), and answering this costs no subprocess —
    which is the whole point of asking it before falling back to the remote.
    """
    _positionals, flags = _split_flags(argv)
    for name, value in flags:
        if name in ("-R", "--repo") and value:
            return value
    return ""


def target_repo(argv: list[str], *, remote: str | None = None) -> str:
    """Which ``owner/name`` this invocation is about, or ``""`` when unclear.

    ``--repo``, a ``repos/<o>/<n>`` path, a ``github.com`` URL, or else the
    checkout's remote. A positional merely shaped like ``owner/name`` — a branch
    such as ``agent/issue-2289`` — is not a repository (#2289).
    """
    explicit = repo_flag(argv)
    if explicit:
        return explicit if github_identity.owner_of(explicit) else ""
    positionals, _flags = _split_flags(argv)
    for part in positionals:
        segments = [segment for segment in part.split("?", 1)[0].split("/") if segment]
        if len(segments) >= 3 and segments[0] == "repos":
            return f"{segments[1]}/{segments[2]}"
        if "github.com" in part and github_identity.owner_of(part):
            return part
    resolved = repo_identity(remote=remote) if remote is not None else ""
    return resolved if github_identity.owner_of(resolved) else ""


def target_owner(argv: list[str], *, remote: str | None = None) -> str:
    """Which owner's authority this invocation needs, or ``""`` to refuse it (#2289).

    ``--repo`` wins, then what the positionals name (a URL, ``repos/<o>/…``,
    ``orgs/<o>``, ``users/<o>``), then the checkout's own remote.
    """
    explicit = repo_flag(argv)
    if explicit:
        return github_identity.owner_of(explicit)
    positionals, _flags = _split_flags(argv)
    named = github_authority.owner_in_positionals(positionals)
    if named:
        return named
    resolved = repo_identity(remote=remote) if remote is not None else ""
    return github_identity.owner_of(resolved)


def label_values(argv: list[str]) -> list[str]:
    """Every label an invocation names, case-folded and stripped, empties dropped.

    ``--label X``, ``--label=X``, ``-l X`` and ``-lX``, repeated or comma-separated, as
    ``gh`` itself accepts them. ``_split_flags`` keeps ``-lX`` whole as a flag name, so
    that spelling is read off the name.
    """
    _positionals, flags = _split_flags(argv)
    values: list[str] = []
    for name, value in flags:
        if name in ("--label", "-l"):
            raw = value
        elif name.startswith("-l") and not name.startswith("--"):
            raw = name[2:]
        else:
            continue
        values.extend(part.strip().casefold() for part in raw.split(",") if part.strip())
    return values


def ownership_label_to_add(
    argv: list[str], repository: str, targets: object
) -> str | None:
    """The ownership label a ``gh issue create`` should gain, or ``None`` (#2505).

    ``targets`` is ``poller_ops.issue_label_targets``' answer. Nothing is added for a
    repository no poller manages, or when the call already names one of that project's
    ownership labels (aliases included). A repository two
    manifests declare honours either's labels and gains the first one's canonical label.
    """
    wanted = github_authority.repository_of(repository)
    if not wanted:
        return None
    matches = [
        target
        for target in (targets or ())
        if github_authority.repository_of(getattr(target, "repository", "")) == wanted
        and getattr(target, "ownership_labels", ())
    ]
    if not matches:
        return None
    owned = {label.strip().casefold() for target in matches for label in target.ownership_labels}
    for value in label_values(argv):
        if value in owned:
            return None
    return matches[0].ownership_labels[0]


def credential_identity(environment: dict[str, str] | None = None) -> str:
    """A short digest of *whose* view of GitHub this invocation gets.

    Two credentials do not see the same issues — a worker can run on its own
    ``GH_CONFIG_DIR`` and the poller's PAT is repository-scoped — so a cached
    answer fetched under one must never be served to another.  Only a digest is
    kept: the key lands in a shared SQLite file that is not a secret store.
    """
    return _credential_identity(dict(os.environ) if environment is None else environment)


def classify(
    argv: list[str],
    *,
    cwd: str | None = None,
    remote: str | None = None,
    environment: dict[str, str] | None = None,
) -> Invocation:
    """Decide what ``argv`` (without the ``gh`` itself) is."""
    positionals, flags = _split_flags(argv)
    if not positionals:
        return Invocation("write", reason="no subcommand")

    group = positionals[0]
    verb = positionals[1] if len(positionals) > 1 else ""

    if (group, verb) in WATCH_COMMANDS:
        return Invocation(
            "refuse", reason=f"`gh {group} {verb}` is a poll, not a read", group=group, verb=verb
        )

    if group == "api" and verb == "graphql":
        cacheable = _graphql_is_a_query(flags)
    elif group == "api":
        # `gh api` is a read only for GET, which is its default until a method
        # or a field is named.  Anything that can mutate goes straight through.
        method = ""
        for name, value in flags:
            if name in ("-X", "--method"):
                method = value.upper()
            elif name in ("-f", "--field", "-F", "--raw-field", "--input"):
                method = method or "POST"
        if method not in ("", "GET"):
            return Invocation("write", reason=f"gh api {method}", group=group, verb=verb)
        cacheable = True
    else:
        cacheable = (group, verb) in READ_COMMANDS

    if not cacheable:
        return Invocation(
            "write",
            invalidates=_prefix(group, cwd=cwd, remote=remote, environment=environment),
            reason=f"{group} {verb}".strip(),
            group=group,
            verb=verb,
        )

    for name, _value in flags:
        if name in WATCH_FLAGS:
            return Invocation(
                "refuse", reason=f"{name} is a poll, not a read", group=group, verb=verb
            )

    rendered = sorted(_normalize(name, value) for name, value in flags)
    # `--repo` names the target explicitly; without it the answer depends on
    # where the worker happens to stand, so the resolved remote joins the key.
    scope = ""
    for name, value in flags:
        if name in ("-R", "--repo") and value:
            scope = value
    if not scope:
        scope = repo_identity(cwd=cwd, remote=remote)

    material = "\x00".join([scope, *positionals, *rendered])
    digest = hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:32]
    identity = credential_identity(environment)
    return Invocation(
        "read",
        key=f"gh-shim:v1:{identity}:{group}:{digest}",
        reason=" ".join(shlex.quote(part) for part in positionals[:2]),
        group=group,
        verb=verb,
    )


def _graphql_is_a_query(flags: list[tuple[str, str]]) -> bool:
    """Is this ``gh api graphql`` asking a question rather than changing something?

    GraphQL is the poller's expensive path and it travels by POST, so the
    method test above would wave every one of these through ungated — leaving
    the shim's whole GraphQL floor unable to gate any GraphQL at all.  Instead
    the query text itself decides.  Anything this cannot read outright — a
    mutation, a ``@file`` reference, a body on stdin — is treated as a write,
    because guessing wrong towards "read" would cache a mutation's answer.
    """
    for name, value in flags:
        if name in ("--input", "-X", "--method"):
            return False
        if name in ("-f", "--field", "-F", "--raw-field"):
            _, _, body = value.partition("=")
            if body.startswith("@"):
                return False
            if body.lstrip().lower().startswith("mutation"):
                return False
    return True


def _prefix(
    group: str,
    *,
    cwd: str | None,
    remote: str | None,
    environment: dict[str, str] | None,
) -> str:
    """The key prefix a write against ``group`` makes stale."""
    del cwd, remote  # Invalidation is per credential and group, not per repo.
    return f"gh-shim:v1:{credential_identity(environment)}:{group}:"


def is_terminal(group: str, verb: str, payload: object) -> bool:
    """Can this answer still change on GitHub?

    Only claimed where `gh` was asked for the state outright and said the thing
    is finished.  Everything else is treated as still open, because the cost of
    guessing wrong is an hour of serving a stale answer.
    """
    if (group, verb) not in (("issue", "view"), ("pr", "view")):
        return False
    if not isinstance(payload, dict):
        return False
    state = payload.get("state")
    return isinstance(state, str) and state.upper() in ("CLOSED", "MERGED")
