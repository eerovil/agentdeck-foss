"""``agentdeck pre-review-helpers``: three Haiku helpers check a change before its first review
(#4437).

Before a pull request's first review the coding agent runs this command from its own checkout.
It starts three ``claude -p --model claude-haiku-5-5`` helpers at once, each with one of the
lists #4428 measured (every path and input; the rest of the repository; libraries and other
services), plus the issue and its use cases, and prints what they found. The coder fixes each
real find and posts one ``Helper checks:`` comment on the issue; ``pre-review-done`` refuses
the first review without it (``review_paperwork``). On the benchmark this caught 5.8 of 7
known bugs per pull request with the review, against 3.0 for the review alone (#4428, #4434).

Each helper:

- reads its own throwaway clone of the pull request's pushed head under
  ``/var/tmp/agentdeck-helpers/``, with the branch ``base`` at the pull request's merge base,
  worked out in the coder's checkout, and sees no other clone, home folder or shared
  temporary folder;
- runs with read-only tools in Claude Code's sandbox with no network (``--settings``), no
  user settings, hooks or CLAUDE.md (``--setting-sources ""``), no MCP servers, no saved
  transcript, only an allowlist of environment variables, and a ``gh`` that refuses;
- is bounded by its own ``timeout -k 10 300``, and gets SIGTERM when this command dies
  (``PR_SET_PDEATHSIG``), so a killed caller leaves nothing spending the account.

The run spends a Claude account: a Claude chat's own, else (a Codex chat, or an own account at
its limit) the one the deck shows with most room. It holds one ``helpers`` host slot. Every
way it cannot run prints ``Helper checks: did not run (<why>)``, which the coder posts as it
is: a failed run never holds the review back.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .github_identity import github_git_environment

MODEL = "claude-haiku-5-5"
#: One helper's limit, enforced by its own ``timeout`` parent, not by this process.
HELPER_TIMEOUT_S = 300
#: How long a run waits for a ``helpers`` host slot before it does not run.
SLOT_WAIT_S = 90.0
ROOT = Path("/var/tmp/agentdeck-helpers")
#: A leftover run directory (its command was SIGKILLed) older than this is removed.
STALE_S = 24 * 3600.0
API_HOST = ("api.anthropic.com", 443)
#: The most issue text a helper's prompt carries.
MAX_ISSUE_CHARS = 20_000
_GH_TIMEOUT_S = 30
_GIT_TIMEOUT_S = 60
#: The only variables a helper gets from this process (#4437 review): the model runs Bash, so
#: nothing else (a cloud key, a token, a deck socket) may reach it.
_KEPT_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TZ",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
)

#: Folders every process on the host writes to, hidden from the helpers but their own clone.
SHARED_TMP = ("/var/tmp", "/tmp")

#: The helpers' sandbox: Bash runs in bubblewrap with no network and writes only in its clone.
SANDBOX_SETTINGS = {
    # Bash in the sandbox and the Read tool alike: no home folder, which holds the account
    # credentials and every other checkout (#4437 helper check).
    "permissions": {"deny": ["Read(~/**)"]},
    "sandbox": {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "network": {"allowedDomains": ["none.invalid"]},
    },
}

# #4428's three lists (h1), as measured. Helper 3 is h1's, without h2's required defaults
# table: Haiku caught the default bug 5 of 6 times without it and 3 of 6 with it (#4434).
PASSES = {
    1: (
        "every path and input",
        """**Every path and input.** For each new or changed guard, branch, threshold,
comparison or early return, list every state and input that reaches it and say what the code
does for each: empty, missing, one item, the shortest and the longest, exactly at the limit, a
third value where two are expected (a third language, a third mode), a device or process idle,
off its usual place or not yet started, a feature or setting turned off, an earlier step
skipped. Check each number against the real unit and size on the other side (samples per
second, milliseconds, chunk length, how many choices a score is shared between). Report every
state whose result is wrong.""",
    ),
    2: (
        "the rest of the repository",
        """**The rest of the repository.** For each new entry point or path, find the old path in
the repository that does the same job, read it, and compare their side effects line by line —
repairs, cleanup, environment, tokens, locks, saved state — and report each one the new path
skips. For each value the new code reads, find where and when it is set, in any file, and
whether it is set on every path before this read, and to what. For each read of saved, cached,
stored or snapshot data, ask when the copy was made and whether the decision needs a fresh
value.""",
    ),
    3: (
        "libraries, settings and other services",
        """**Libraries, settings and other services.** For each library call or constructor the
change adds or changes, list the parameters it leaves at their default, and state each default
with the installed version's value: from the library's installed source if this host has it
(look in the project's virtualenv or the system's site-packages), otherwise from what you know
of that library and version, saying so. Compare each default with what the code, its comments
and the repository's docs assume (timings, sizes, counts, languages, retries). For each call to
another service or repository, read its docs or code when you can reach them on this host, and
check whether a read returns cached or stored data, its units, and its limits. Report each
mismatch, quoting the default or the doc line.""",
    ),
}

PROMPT = """You are a helper agent. The coding agent that wrote a pull request started you,
with two other helpers, before it marks the pull request ready for review. It will fix what
you find, so look for real bugs in its change, not style.

The pull request: "{title}"

The checkout in your working directory, {cwd}, is the pull request's head. Its base is the
branch `base`: read the change with `git diff base HEAD` and `git log base..HEAD`, and read
any file in the checkout. Stay inside this checkout, plus installed library sources (a
virtualenv or site-packages) if you need them. Do not read other checkouts of this repository
on this host, do not use `gh`, GitHub or the web, and do not edit any file.

Your one job is this list. Write the list out, item by item, before deciding: an item that is
not written out was not checked.

{pass_text}

Report any other bug you notice too, but spend your time on this list and work it through to
the end.

The issue the pull request is for, with its use cases, is below. Check the change against
what it asks, too.

<issue>
{issue}
</issue>

End your reply with a heading `## Findings` and a numbered list under it, one bug per item:
`path:line`, what goes wrong, for which state or input, and the evidence. Start an item with
`Suspicion:` when you are not sure it is a bug. Write `No findings.` if there are none.
Nothing after the list.
"""

_WALKTHROUGH_RE = re.compile(r"(?m)^[\s>*_#]*Use-case walkthrough:")
_PULL_RE = re.compile(r"^(?:(?P<repo>[\w.-]+/[\w.-]+)#)?(?P<n>\d+)$")
_URL_RE = re.compile(r"github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/\d+")


class NotRun(Exception):
    """The helpers cannot run; the message is the ``did not run`` reason."""


@dataclass
class Pull:
    repository: str
    number: int
    title: str
    body: str
    base_ref: str
    base_oid: str
    head_oid: str


@dataclass
class Outcome:
    part: int
    findings: str = ""
    failed: str = ""
    minutes: float = 0.0
    cost_usd: float = 0.0


@dataclass
class Run:
    """What one invocation started, so a signal or an error can stop and remove it."""

    directory: Path
    processes: list[subprocess.Popen] = field(default_factory=list)
    slot_fd: int | None = None
    transcripts: list[Path] = field(default_factory=list)


def did_not_run(why: str, pull: str = "") -> str:
    """The line to post when the helpers cannot run; it names the pull request, as each
    receipt must (``review_paperwork.helper_receipt``)."""
    return f"Helper checks: {pull + ' ' if pull else ''}did not run ({why})"


# ------------------------------------------------------------------------------ reading


def _gh_json(argv: list[str], cwd: str) -> dict:
    try:
        done = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=_GH_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun(f"`{' '.join(argv[:3])}` failed: {exc}") from exc
    if done.returncode != 0:
        why = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
        raise NotRun(f"`{' '.join(argv[:3])}` failed: {why[0][:200]}")
    try:
        value = json.loads(done.stdout or "null")
    except ValueError as exc:
        raise NotRun(f"`{' '.join(argv[:3])}` answered no JSON") from exc
    if not isinstance(value, dict):
        raise NotRun(f"`{' '.join(argv[:3])}` answered no object")
    return value


_ORIGIN_RE = re.compile(r"github\.com[:/](?P<repo>[\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def spec_ref(spec: str, cwd: str) -> str:
    """The pull request ``--pull`` names, as ``owner/repo#n``, without asking GitHub: what a
    ``did not run`` line names when the pull request itself cannot be read, so its receipt
    still satisfies ``pre-review-done`` (#4437 review). ``#n`` when the checkout's ``origin``
    is not on GitHub; empty when ``spec`` is not a pull request at all."""
    match = _PULL_RE.match(spec.strip())
    if match is None:
        return ""
    repository = match.group("repo")
    if not repository:
        with contextlib.suppress(RuntimeError, OSError, subprocess.TimeoutExpired):
            origin = _ORIGIN_RE.search(_git(cwd, "remote", "get-url", "origin"))
            repository = origin.group("repo") if origin else ""
    return f"{repository}#{match.group('n')}" if repository else f"#{match.group('n')}"


def read_pull(spec: str, cwd: str) -> Pull:
    """The pull request ``spec`` (``owner/repo#n`` or ``n``, read in ``cwd``'s repository)."""
    match = _PULL_RE.match(spec.strip())
    if match is None:
        raise NotRun(f"--pull must be `owner/repo#n` or a number, not {spec!r}")
    argv = ["gh", "pr", "view", match.group("n")]
    if match.group("repo"):
        argv += ["--repo", match.group("repo")]
    argv += ["--json", "title,body,baseRefName,baseRefOid,headRefOid,url"]
    data = _gh_json(argv, cwd)
    url = _URL_RE.search(str(data.get("url") or ""))
    repository = match.group("repo") or (url.group("repo") if url else "")
    if not repository:
        raise NotRun("the pull request's repository is unreadable")
    return Pull(
        repository=repository,
        number=int(match.group("n")),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        base_ref=str(data.get("baseRefName") or ""),
        base_oid=str(data.get("baseRefOid") or ""),
        head_oid=str(data.get("headRefOid") or ""),
    )


def issue_of(pull: Pull, given: str = "") -> tuple[str, int] | None:
    """The Work's issue: ``given``, else the first ``Refs #n`` (or ``Closes``…) of the body."""
    from .review_paperwork import _REFERENCE_RE

    if given:
        match = _PULL_RE.match(given.strip())
        if match is None:
            raise NotRun(f"--issue must be `owner/repo#n` or a number, not {given!r}")
        return (match.group("repo") or pull.repository, int(match.group("n")))
    for found in _REFERENCE_RE.finditer(pull.body):
        repository = found.group("url_repo") or found.group("repo") or pull.repository
        return (repository, int(found.group("url_n") or found.group("n")))
    return None


def issue_text(repository: str, number: int, cwd: str) -> str:
    """The issue's title and body, and any ``Use-case walkthrough:`` comment, for the prompt."""
    data = _gh_json(
        ["gh", "issue", "view", str(number), "--repo", repository, "--json", "title,body,comments"],
        cwd,
    )
    walkthroughs = []
    for comment in data.get("comments") or ():
        body = str(comment.get("body") or "") if isinstance(comment, dict) else ""
        if _WALKTHROUGH_RE.search(body):
            walkthroughs.append("A use-case walkthrough posted on the issue:\n" + body)
    # A long body is cut, not the use cases after it: the helpers must get those (#4437).
    tail = "\n\n".join(walkthroughs)
    if len(tail) > MAX_ISSUE_CHARS // 2:
        tail = tail[: MAX_ISSUE_CHARS // 2] + "\n[… the rest of the walkthroughs is cut]"
    text = f"{repository}#{number}: {data.get('title') or ''}\n\n{data.get('body') or ''}"
    room = MAX_ISSUE_CHARS - len(tail)
    if len(text) > room:
        text = text[:room] + "\n[… the rest of the issue body is cut]"
    return "\n\n".join(part for part in (text.strip(), tail) if part)


# ------------------------------------------------------------------------- before running


def check_network(connect=socket.create_connection) -> None:
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        raise NotRun("run it escalated: the Codex sandbox has no network")
    try:
        connect(API_HOST, 5).close()
    except OSError as exc:
        raise NotRun(f"no network to the Claude API: {exc}") from exc


def check_sandbox() -> None:
    """Claude's sandbox runs Bash in bubblewrap; without it a helper would lose Bash and give a
    quietly weaker result, so the run does not start (#4437 design review)."""
    if sys.platform != "linux":
        return
    for tool in ("bwrap", "socat"):
        if shutil.which(tool) is None:
            raise NotRun(f"Claude's sandbox cannot start here: {tool} is not installed")
    try:
        done = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "--dev", "/dev", "true"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun(f"Claude's sandbox cannot start here: {exc}") from exc
    if done.returncode != 0:
        why = (done.stderr or "").strip().splitlines()[-1:] or [f"exit {done.returncode}"]
        raise NotRun(f"Claude's sandbox cannot start here: {why[0][:200]}")


def own_claude_account(accounts: dict[str, Path]) -> str:
    """This chat's own Claude account key, or "" (a Codex chat, or a shell outside the deck)."""
    key = os.environ.get("AGENTDECK_ACCOUNT_KEY", "")
    if key.startswith("claude_code:"):
        return key
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if not configured:
        return ""
    here = Path(configured).expanduser().resolve()
    for name, root in accounts.items():
        if name.startswith("claude_code:") and Path(root).expanduser().resolve() == here:
            return name
    return ""


def choose_account(usage: list[dict], own: str) -> str:
    """The chat's own Claude account while it has room, else the one with most room."""
    from .web_research import roomiest_account

    rows = {str(row.get("account_key") or ""): row for row in usage if isinstance(row, dict)}
    if (
        own
        and own in rows
        and not any(
            (rows[own].get(window) or 0) >= 100 for window in ("five_hour_pct", "seven_day_pct")
        )
    ):
        return own
    chosen = roomiest_account(usage, "claude_code")
    if chosen is None:
        raise NotRun("no Claude account with room")
    return chosen


def read_usage(url: str) -> list[dict]:
    import httpx

    try:
        answer = httpx.get(url.rstrip("/") + "/api/usage", timeout=10.0, trust_env=False)
        answer.raise_for_status()
        rows = answer.json().get("usage") or []
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, AttributeError) as exc:
        raise NotRun(f"the deck's accounts are unreadable: {exc}") from exc
    return [row for row in rows if isinstance(row, dict)]


def account_roots() -> dict[str, Path]:
    from .config import load_config

    try:
        return {account.key: Path(account.root) for account in load_config().build_accounts()}
    except Exception as exc:  # noqa: BLE001 - an unreadable config is a reason not to run
        raise NotRun(f"the deck's config is unreadable: {exc}") from exc


# ------------------------------------------------------------------------------- git


def _git(cwd: str | Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip()[:200] or f"exit {done.returncode}")
    return done.stdout.strip()


def base_commit(checkout: str, pull: Pull, head: str = "HEAD") -> tuple[str, str]:
    """The merge base of ``head`` and the pull request's base, worked out in the
    checkout, and a warning or "": (a) the base branch fetched now; (b) ``baseRefOid`` when the
    checkout has it (a Codex sandbox refuses the fetch); (c) the checkout's own
    ``origin/<base>``, which may be older than the pull request's base."""
    tried: list[str] = []
    if pull.base_ref:
        try:
            _git(checkout, "fetch", "-q", "origin", pull.base_ref, env=github_git_environment())
            return _git(checkout, "merge-base", head, "FETCH_HEAD"), ""
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            tried.append(f"fetch: {exc}")
    if pull.base_oid:
        try:
            _git(checkout, "cat-file", "-e", f"{pull.base_oid}^{{commit}}")
            return _git(checkout, "merge-base", head, pull.base_oid), ""
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            tried.append(f"{pull.base_oid[:12]}: {exc}")
    if pull.base_ref:
        try:
            sha = _git(checkout, "merge-base", head, f"refs/remotes/origin/{pull.base_ref}")
            return sha, (
                f"the base branch could not be fetched, so the base is this checkout's"
                f" `origin/{pull.base_ref}`, which may be older than the pull request's"
            )
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            tried.append(f"origin/{pull.base_ref}: {exc}")
    raise NotRun("base branch unreadable: " + "; ".join(tried)[:300])


def pull_head(checkout: str, pull: Pull) -> str:
    """The commit the helpers read: the pull request's own head, never whatever the checkout
    has checked out, so their receipt is about this pull request (#4437 review)."""
    if not pull.head_oid:
        raise NotRun("the pull request's head is unreadable")
    try:
        _git(checkout, "cat-file", "-e", f"{pull.head_oid}^{{commit}}")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun(
            f"this checkout lacks the pull request's head {pull.head_oid[:12]}: push it from"
            " here, or fetch it"
        ) from exc
    return pull.head_oid


def make_clone(checkout: str, directory: Path, head: str, base: str, name: str = "repo") -> Path:
    """A throwaway clone of ``checkout`` at ``head`` with the branch ``base`` at ``base``. It
    copies the whole object store (``--no-hardlinks``: ``/var/tmp`` and the home folder can be
    different subvolumes, where a hard link fails), so both commits are there whatever its refs
    say; for this repository about 150 MB and a second, removed with the run. Not
    ``--shared``: borrowed objects would sit in the coder's checkout under the home folder,
    which the helpers cannot read, and ``git`` there then fails with "bad object HEAD" (#4437
    review, checked live)."""
    repo = directory / name
    _git(directory, "clone", "-q", "--no-hardlinks", "--no-checkout", checkout, str(repo))
    _git(repo, "checkout", "-q", "--detach", head)
    _git(repo, "branch", "-f", "base", base)
    # The sandbox drops empty masks (``.bashrc``, ``.gitconfig``, …) into the work tree; a
    # helper reading ``git status`` should not take them for the change (checked live).
    _git(repo, "config", "status.showUntrackedFiles", "no")
    with contextlib.suppress(RuntimeError):
        _git(repo, "remote", "remove", "origin")
    return repo


_RUN_NAME_RE = re.compile(r".+-\d+-[0-9a-f]{8}")


def sweep_stale(root: Path = ROOT, now: float | None = None) -> None:
    """Remove run directories a SIGKILLed command left behind, once a day old."""
    now = time.time() if now is None else now
    if not root.is_dir():
        return
    for child in root.iterdir():
        if not _RUN_NAME_RE.fullmatch(child.name):
            continue  # only this command's own run directories
        with contextlib.suppress(OSError):
            if now - child.stat().st_mtime > STALE_S:
                shutil.rmtree(child, ignore_errors=True)


# --------------------------------------------------------------------------- the helpers


def helper_command(binary: str = "claude", repo: Path | None = None) -> list[str]:
    """One helper's argv, ``timeout`` first so its limit holds whatever happens to us."""
    return [
        "timeout",
        "-k",
        "10",
        str(HELPER_TIMEOUT_S),
        binary,
        "-p",
        "--model",
        MODEL,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--settings",
        json.dumps(helper_settings(repo)),
        "--allowedTools",
        "Bash",
        "--disallowedTools",
        "Edit",
        "Write",
        "NotebookEdit",
        "WebFetch",
        "WebSearch",
        "Agent",
        "Task",
    ]


def helper_settings(repo: Path | None) -> dict:
    """``SANDBOX_SETTINGS``, with ``/var/tmp`` and ``/tmp`` hidden but for the helper's own
    clone, so Bash sees no other helper's or run's clone and no other chat's scratch files
    (#4437 review). Checked live: ``git`` and ``$TMPDIR`` still work, the home folder and a
    sibling run stay hidden. The clone stays writable: a read-only clone breaks the sandbox,
    which writes masks such as ``.gitconfig`` into the working folder."""
    settings = json.loads(json.dumps(SANDBOX_SETTINGS))
    if repo is not None:
        clone = Path(repo).resolve()
        # Only this clone of the shared temporary folders: no other run's clone, no other
        # chat's scratch files (#4437 review). ``allowRead`` wins over ``denyRead``.
        shared = [*SHARED_TMP, *([os.environ["TMPDIR"]] if os.environ.get("TMPDIR") else [])]
        settings["sandbox"]["filesystem"] = {
            "denyRead": shared,
            # The run's fake ``gh`` too (``bin`` beside the clones), or ``PATH`` would fall
            # through to the real one (#4437 helper check).
            "allowRead": [str(clone), str(clone.parent / "bin")],
        }
    return settings


def helper_tmp(repo: Path) -> Path:
    """The helper's own temporary folder, inside its clone's ``.git`` (out of the work tree,
    and writable where the clone is): the host's is shared with every other chat, so it is
    hidden from the helper (#4437 review). A folder beside the clone would need ``allowRead``,
    which mounts it read-only and stops the sandbox starting (checked live)."""
    return Path(repo) / ".git" / "helper-tmp"


def helper_env(bin_dir: Path, account_root: Path | None) -> dict[str, str]:
    from . import claude_env

    env = {k: v for k, v in os.environ.items() if k in _KEPT_ENV or k.startswith("LC_")}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    # ``--strict-mcp-config`` alone leaves the account's claude.ai connectors on (``locked``).
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    if account_root is not None:
        claude_env.point_at(env, account_root)
    return env


def fake_gh(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho 'gh is not available to a pre-review helper' >&2\nexit 1\n")
    gh.chmod(0o755)


def _die_with_parent() -> None:
    """In the child before exec: SIGTERM when the process that started it dies, even by
    SIGKILL (``PR_SET_PDEATHSIG``). ``timeout`` passes it on to its helper."""
    with contextlib.suppress(Exception):
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


def start_helpers(
    run: Run, repos: dict[int, Path], prompts: dict[int, str], env: dict[str, str]
) -> None:
    """Each helper in its own clone, with a sandbox that reads only that clone."""
    out = run.directory / "out"
    out.mkdir(exist_ok=True)
    for part, prompt in prompts.items():
        tmp = helper_tmp(repos[part])
        tmp.mkdir(parents=True, exist_ok=True)
        (out / f"{part}.prompt").write_text(prompt, encoding="utf-8")
        with (out / f"{part}.prompt").open() as stdin, (out / f"{part}.json").open("w") as stdout:
            run.processes.append(
                subprocess.Popen(
                    helper_command(repo=repos[part]),
                    stdin=stdin,
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                    cwd=repos[part],
                    env={**env, "TMPDIR": str(tmp)},
                    preexec_fn=_die_with_parent if sys.platform == "linux" else None,
                )
            )


def findings_of(text: str) -> str:
    """The ``## Findings`` list of a helper's reply, or the whole reply when it has none. The
    last heading counts: a helper's notes may name the heading before its list does."""
    head, sep, tail = text.rpartition("## Findings")
    head = text if not sep else head
    return (tail if sep else head).strip() or "No findings."


def read_outcome(part: int, path: Path, code: int) -> Outcome:
    raw = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    if code in (124, 137):  # ``timeout``'s own exit, or its SIGKILL after ``-k``
        return Outcome(part, failed=f"timed out after {HELPER_TIMEOUT_S // 60} minutes")
    if code != 0:  # a result printed before a failure is not a finished helper (#4437 review)
        last = (raw.strip().splitlines()[-1:] or [""])[0][:200]
        return Outcome(part, failed=f"exit {code}" + (f": {last}" if last else ""))
    event = None
    with contextlib.suppress(ValueError):
        whole = json.loads(raw)
        if isinstance(whole, dict) and whole.get("type") == "result":
            event = whole
    for line in reversed(raw.splitlines() if event is None else ()):
        with contextlib.suppress(ValueError):
            candidate = json.loads(line)
            if isinstance(candidate, dict) and candidate.get("type") == "result":
                event = candidate
                break
    if event is None or event.get("is_error") or not event.get("result"):
        why = (raw.strip().splitlines()[-1:] or [f"exit {code}"])[0][:200]
        if event is not None and event.get("is_error"):
            why = str(event.get("result") or event.get("subtype") or why)[:200]
        return Outcome(part, failed=why)
    return Outcome(
        part,
        findings=findings_of(str(event["result"])),
        minutes=float(event.get("duration_ms") or 0) / 60000,
        cost_usd=float(event.get("total_cost_usd") or 0),
    )


def stop(run: Run) -> None:
    for process in run.processes:
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
    for process in run.processes:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            process.wait(timeout=15)
    if run.slot_fd is not None:
        with contextlib.suppress(OSError):
            os.close(run.slot_fd)
        run.slot_fd = None
    shutil.rmtree(run.directory, ignore_errors=True)
    for transcripts in run.transcripts:
        shutil.rmtree(transcripts, ignore_errors=True)


def transcript_dir(account_root: Path, repo: Path) -> Path:
    """Where Claude keeps the clone's project folder in the account: even with
    ``--no-session-persistence`` it writes large tool results and an empty memory folder
    there, so the run removes it (#4437 live run)."""
    return account_root / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo))


# ------------------------------------------------------------------------------- report


def report(
    pull: Pull,
    issue: str,
    head: str,
    base: str,
    account: str,
    outcomes: list[Outcome],
    warnings: list[str],
) -> str:
    lines = [
        f"Helper checks of {pull.repository}#{pull.number} at {head[:12]}"
        f" (base {base[:12]}), on {account}:"
    ]
    lines += [f"Warning: {warning}" for warning in warnings]
    for outcome in outcomes:
        title = PASSES[outcome.part][0]
        lines.append("")
        if outcome.failed:
            lines.append(f"## Helper {outcome.part} ({title}): did not run ({outcome.failed})")
            continue
        lines.append(
            f"## Helper {outcome.part} ({title}), {outcome.minutes:.1f} min,"
            f" ${outcome.cost_usd:.2f}"
        )
        lines.append(outcome.findings)
    lines.append("")
    lines.append("---")
    ran = [outcome for outcome in outcomes if not outcome.failed]
    if not ran:
        why = "; ".join(f"helper {o.part}: {o.failed}" for o in outcomes)
        lines.append(f"No helper ran. Post this line on {issue} as it is:")
        lines.append(did_not_run(why, f"{pull.repository}#{pull.number}"))
        return "\n".join(lines)
    lines.append(
        f"Judge each find against the code. Fix the real ones and push; drop the rest with a"
        f" one-line reason. Then post one comment on {issue}, opening with the line"
        f" `Helper checks: {pull.repository}#{pull.number}`, that lists each find as `fixed`"
        " (with its commit) or `dropped: <why>`."
    )
    failed = [outcome for outcome in outcomes if outcome.failed]
    if failed:
        lines.append(
            "Name the helpers that did not run in it too: "
            + "; ".join(f"helper {o.part} did not run ({o.failed})" for o in failed)
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------------- main


def _on_signal(signum, _frame) -> None:
    raise SystemExit(128 + signum)


def run_helpers(args: argparse.Namespace) -> int:
    from .host_slots import helper_slot_paths, take_a_slot

    checkout = str(Path(args.cwd).expanduser().resolve())
    try:
        # First, so a Codex chat without escalation hears to escalate, not that gh failed.
        check_network()
    except NotRun as exc:
        print(did_not_run(str(exc), spec_ref(args.pull, checkout)))
        return 0
    try:
        pull = read_pull(args.pull, checkout)
        found = issue_of(pull, args.issue or "")
    except NotRun as exc:
        print(did_not_run(str(exc), spec_ref(args.pull, checkout)))
        return 0
    ref = f"{pull.repository}#{pull.number}"
    if found is None:
        print(
            did_not_run(
                "the pull request body names no issue: add `Refs #<issue>` or pass --issue", ref
            )
        )
        return 0
    issue = f"{found[0]}#{found[1]}"
    warnings: list[str] = []
    directory = ROOT / f"{pull.repository.replace('/', '--')}-{pull.number}-{uuid.uuid4().hex[:8]}"
    run = Run(directory)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _on_signal)
    try:
        try:
            text = issue_text(found[0], found[1], checkout)
            check_sandbox()
            roots = account_roots()
            account = choose_account(read_usage(args.url), own_claude_account(roots))
            if account not in roots:
                raise NotRun(f"the account {account} is not in this deck's config")
            head = pull_head(checkout, pull)
            if _git(checkout, "status", "--porcelain", "--untracked-files=no"):
                warnings.append(
                    "the checkout has uncommitted changes; the helpers read only the pull"
                    " request's pushed head"
                )
            base, warning = base_commit(checkout, pull, head)
            warnings += [warning] if warning else []
            ROOT.mkdir(parents=True, exist_ok=True)
            sweep_stale()
            run.slot_fd = take_a_slot(helper_slot_paths(), time.monotonic() + SLOT_WAIT_S)
            if run.slot_fd is None:
                raise NotRun(
                    f"every helper slot on this host stayed busy for {SLOT_WAIT_S:.0f} seconds"
                )
            directory.mkdir(parents=True)
            # One clone each, so no helper changes what another reads (#4437 review).
            repos = {
                part: make_clone(checkout, directory, head, base, f"repo-{part}") for part in PASSES
            }
        except NotRun as exc:
            print(did_not_run(str(exc), ref))
            return 0
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            print(did_not_run(f"the run cannot be prepared: {exc}", ref))
            return 0
        bin_dir = directory / "bin"
        fake_gh(bin_dir)
        env = helper_env(bin_dir, roots[account])
        prompts = {
            part: PROMPT.format(title=pull.title, cwd=repos[part], pass_text=text_, issue=text)
            for part, (_title, text_) in PASSES.items()
        }
        print(
            f"Starting three helpers on {account}; this takes up to {HELPER_TIMEOUT_S // 60}"
            " minutes.",
            flush=True,
        )
        run.transcripts = [transcript_dir(roots[account], repo) for repo in repos.values()]
        try:
            start_helpers(run, repos, prompts, env)
        except OSError as exc:
            print(did_not_run(f"the helpers cannot start: {exc}", ref))
            return 0
        codes = [process.wait() for process in run.processes]
        outcomes = [
            read_outcome(part, directory / "out" / f"{part}.json", code)
            for part, code in zip(prompts, codes, strict=True)
        ]
        print(report(pull, issue, head, base, account, outcomes, warnings))
        return 0
    finally:
        stop(run)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agentdeck pre-review-helpers",
        description="Three Haiku helpers check a pull request's change before its first review,"
        " and print what they found (#4437). Takes up to about 9 minutes.",
    )
    parser.add_argument("--cwd", required=True, help="the pull request's checkout")
    parser.add_argument("--pull", required=True, help="owner/repo#n, or n in --cwd's repository")
    parser.add_argument("--issue", default="", help="the Work's issue; default: the body's Refs")
    parser.add_argument(
        "--url",
        default=os.environ.get("AGENTDECK_URL", "http://127.0.0.1:8756"),
        help="running AgentDeck base URL",
    )
    return run_helpers(parser.parse_args(argv))
