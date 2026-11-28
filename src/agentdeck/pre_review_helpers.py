"""``agentdeck pre-review-helpers``: three Haiku helpers check a change before its first review
(#4437). A fourth, an Opus whole review (#4531), is switched off (``WHOLE_REVIEW``, #4533).

Right after a pull request's first push, beside its first full test run, the coding agent runs
this command from its own checkout (#4567), so the finds join that first fix round; the
checklist on ready asks for it again only when the coder changed more than the finds asked.
It starts three ``claude -p --model claude-haiku-5-5`` helpers at once, each with one of the
lists #4428 measured (every path and input; the rest of the repository; libraries and other
services), plus the issue and its use cases. Switched on, the Opus pass (``claude-opus-5-5``)
goes beside them: it reviews the whole change by the reviewer's own rules and gives each
suspicion a confidence, and only those at ``CONFIDENCE_FLOOR`` or above are kept (#4512's
variant c). It prints what they found. The coder fixes each real find and posts one
``Helper checks:`` comment on the issue; ``pre-review-done`` refuses the first review without
it (``review_paperwork``). On the benchmark this caught 5.8 of 7
known bugs per pull request with the review, against 3.0 for the review alone (#4428, #4434).

Each helper:

- reads its own throwaway clone of the pull request's pushed head under
  ``/var/tmp/agentdeck-helpers/``, with the branch ``base`` at the pull request's merge base,
  worked out in the coder's checkout, and sees no other clone, home folder or shared
  temporary folder;
- runs with read-only tools in Claude Code's sandbox with no network (``--settings``), no
  user settings, hooks or CLAUDE.md (``--setting-sources ""``), no MCP servers, no saved
  transcript, only an allowlist of environment variables, and a ``gh`` that refuses;
- is bounded by its own ``timeout -k 10 600``, as the Opus pass is, and gets SIGTERM when
  this command dies (``PR_SET_PDEATHSIG``), so a killed caller leaves nothing spending the
  account.

One run per pull request and head: a second one finds the first's lock and refuses, naming
the file the first writes its report to, which a later run for that head prints instead of
spending again (#4531 design review). The command is queued as an ``agentdeck job`` (up to
about 22 minutes, #4662), so its caller may be gone when it ends; the report file is how it
is read then.

The run spends a Claude account: a Claude chat's own, else (a Codex chat, or an own account at
its limit) the one the deck shows with most room. It holds one ``helpers`` host slot. The Opus
pass, when on, needs the reviewer's rule files from the review-chat checkout; without its
``SKILL.md`` that pass alone does not run. Every way it cannot run prints
``Helper checks: did not run (<why>)``, which the coder posts as it is: a failed run never
holds the review back.

Each helper's tokens, cost, wall time and exit are appended as one JSON line to a file in the
deck's data folder, ``~/.local/share/agentdeck/pre-review-helpers/usage.jsonl`` by default
(``AGENTDECK_HELPER_USAGE_LOG`` overrides it). It outlives the run's folder, so the helpers'
cost per pull request can be weighed afterwards (#4510). A run that does not start writes a
``did_not_run`` line per helper.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import fcntl
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .github_identity import github_git_environment

MODEL = "claude-haiku-5-5"
#: Whether the Opus whole review runs. Off by the owner's choice (#4533): on 56 pull requests
#: it caught 13 % of the late findings, under the 30 % it had to. Review point, a week after it
#: shipped: if late findings per pull request rise, switch it on; else remove its code.
WHOLE_REVIEW = False
#: The whole-review pass's model (#4531, assumed): Opus, as #4512 measured it.
WHOLE_MODEL = "claude-opus-5-5"
#: One helper's limit, enforced by its own ``timeout`` parent, not by this process. 600, as
#: the whole review's (#4662): at 300 a third of the Haiku helpers timed out (270 of 795 from
#: 21 to 25 Nov 2026), and the finished ones' 90th percentile was 270-286 s. Review point:
#: after 300 more Haiku runs, over 10 % still timing out means the prompts or the model, not a
#: longer limit.
HELPER_TIMEOUT_S = 600
#: The whole-review pass's own limit (#4531, assumed): #4512's median pass took 2.7 minutes.
WHOLE_TIMEOUT_S = 600
#: The whole review keeps a suspicion at this confidence or above (#4531, assumed: fitted on
#: #4512's data, about 45 % of late findings at 0.8 false ones per pull request).
CONFIDENCE_FLOOR = 25
#: How long a run waits for a ``helpers`` host slot before it does not run: one run's 600
#: seconds and its clones, so a third run waits for a slot to free rather than not running
#: (#4531).
SLOT_WAIT_S = 660.0
ROOT = Path("/var/tmp/agentdeck-helpers")
#: A leftover run directory (its command was SIGKILLed) older than this is removed.
STALE_S = 24 * 3600.0
API_HOST = ("api.anthropic.com", 443)
#: The most issue text a helper's prompt carries.
MAX_ISSUE_CHARS = 20_000
#: Where each helper's usage line is appended (#4510); ``AGENTDECK_HELPER_USAGE_LOG`` overrides it.
#: Under the deck's data folder, ``$XDG_DATA_HOME/agentdeck`` (``~/.local/share`` by default).
USAGE_LOG = "agentdeck/pre-review-helpers/usage.jsonl"
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

# #4428's three lists (h1), as measured. Helper 2's "Every way in" is #4481's: on replay it
# caught the other-route bugs the rest of list 2 missed (#4482). Helper 3 is h1's, without
# h2's required defaults table: Haiku caught the default bug 5 of 6 times without it and 3 of
# 6 with it (#4434).
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
value.

**Every way in.** For each action the change guards, gates or adds (start, send, approve,
refuse, record), list every other way in to that action: other routes, commands, buttons,
retries, timers, and older or newer versions of the client or server on either side. List who
else can send that command or reach that route: the agent being checked, another chat, a
person, an older process. For each, say whether it goes through the new check and what it
gets; report each one that skips the check or gets a different answer.""",
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

#: The whole-review pass's number, beside ``PASSES``' three.
WHOLE = 4
#: Each helper's name, for the report and the usage log.
NAMES = {**{part: name for part, (name, _text) in PASSES.items()}, WHOLE: "whole review"}


def active_parts() -> tuple[int, ...]:
    """The helpers a run starts and logs."""
    return (*PASSES, WHOLE) if WHOLE_REVIEW else tuple(PASSES)


def model_of(part: int) -> str:
    return WHOLE_MODEL if part == WHOLE else MODEL


def timeout_of(part: int) -> int:
    return WHOLE_TIMEOUT_S if part == WHOLE else HELPER_TIMEOUT_S


#: The reply is only the findings (#4720): on #4711's replay that about kept the catch (3.50 against
#: 3.67 known bugs alone, 5.50 against 5.17 with the reviewer) at a third less cost, where
#: `--effort low` dropped bugs. Review point: after 100 live pull requests, if the helpers'
#: own fixed finds per pull request fall by more than a quarter from 0.36-0.6 (#4703), go
#: back to "Write the list out, item by item, before deciding".
PROMPT = """You are a helper agent. The coding agent that wrote a pull request started you,
with other helpers, before it marks the pull request ready for review. It will fix what
you find, so look for real bugs in its change, not style.

The pull request: "{title}"

The checkout in your working directory, {cwd}, is the pull request's head. Its base is the
branch `base`: read the change with `git diff base HEAD` and `git log base..HEAD`, and read
any file in the checkout. Stay inside this checkout, plus installed library sources (a
virtualenv or site-packages) if you need them. Do not read other checkouts of this repository
on this host, do not use `gh`, GitHub or the web, and do not edit any file.

Your one job is this list. Work through it item by item to the end, but do the checking in
your tool calls and your own thinking, not in your reply: do not restate the diff, the code or
the list, and write nothing about the items that are fine. Your reply is only the findings
below. Do not read or report on diagrams (`docs/diagrams/`): one that is missing or no
longer matches the code is never a finding (#4721).

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

#: #4512's variant c, as a pre-review helper rather than a replay (#4531).
WHOLE_PROMPT = """You are a helper agent standing in for AgentDeck's pull-request reviewer, the
ChatGPT skill `github-repo-reviewer`, before the pull request's first review. The coding agent
that wrote it started you, beside three other helpers, and will fix what you find. Review the
change the way that reviewer's own rules say, and report your findings. Do not post anything.

The reviewer's rules (read every one in full before reading the diff, as its SKILL.md says):
{rules}
The pull request under review ({repo}#{number}):
- The pull request's title and body, then the issue and its comments: {inputs}/context.md
- The diff of its head against its base ({base} .. {head}): {inputs}/diff.patch ({stat})
- The whole repository at its head: your working directory, {cwd} (read any file there; its
  AGENTS.md or CLAUDE.md is the repository guide). `git diff base HEAD` and `git log base..HEAD`
  work there too.

How this run differs from the reviewer's own:
- You have no sub-reviewers. Run each SPAWN_SUBREVIEWER pass yourself, in turn, as
  baseline-review.md and REVIEW.md describe for a runner that cannot spawn them (one
  correctness judgment, so its gate is the single-judgment one).
- Skip everything about handled state, labels, agent cards, dashboards, merging, posting,
  closing a chat, CI/check runs, verification jobs and screenshots: none of that exists here.
  Review the code, the tests, the docs in the diff, the use-case walkthrough, and the
  architecture, as the rules require.
- Do not read or report on diagrams (`docs/diagrams/`), whatever the rules say of them: one
  that is missing or no longer matches the code is never a finding (#4721).
- Read files with your file tools and `git`; "connector reads" in the rules mean those. Stay
  inside your working directory and the files named above; do not use `gh`, GitHub or the
  web, and do not edit any file.
- One rule changes for this run: drop every confidence gate. List every suspicion you have
  about the change, however unsure, each with your honest confidence 0-100 (low is fine). Keep
  quoting the evidence line where you can, but do not drop a suspicion for lacking it. Set
  gate to "none".
{missing}
Finish with the findings comment the reviewer would post (the findings list in REVIEW.md's
format, then the six-line Architecture: block), and after it, as the very last thing in your
reply, one fenced ```json block:
{{"findings": [{{"kind": "bug|edge case|cosmetic|design", "file": "path", "line": 123, \
"text": "one-paragraph finding as posted", "confidence": 0-100, "gate": "conf|none"}}], \
"verdict": "clean|findings"}}
List in it every finding you would post, and only those. Include architecture findings
(Simplify:/Design finding:/Design question:) with kind "design".
"""
#: Where the reviewer's rules live in the review-chat repository, and the ref its reviewer
#: reads them at (``review_fallback.brief``): never ``origin/HEAD``, which a clone may lack.
REVIEWER_DIR = "skills/github-repo-reviewer"
REVIEWER_REF = "refs/remotes/origin/main"
#: The shared review contract, from the deck's own repository at the ref the reviewer reads.
REVIEW_CONTRACT = "skills/rate-my-pr/REVIEW.md"
DECK_REF = "refs/remotes/origin/master"
#: The most a context file the whole review reads may hold.
MAX_CONTEXT_CHARS = 400_000

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
    #: ``ok``, ``timeout``, ``failed`` or ``did_not_run``, for the usage log (#4510).
    exit: str = "ok"
    #: What the CLI reported it spent, even for a helper that failed (``usage_of``).
    usage: dict = field(default_factory=dict)
    wall_s: float = 0.0
    #: The whole review's suspicions under ``CONFIDENCE_FLOOR``, left out of ``findings``.
    dropped: int = 0


@dataclass
class Run:
    """What one invocation started, so a signal or an error can stop and remove it."""

    directory: Path
    processes: list[subprocess.Popen] = field(default_factory=list)
    #: The helper each of ``processes`` runs, in the same order.
    parts: list[int] = field(default_factory=list)
    slot_fd: int | None = None
    transcripts: list[Path] = field(default_factory=list)
    #: Each finished helper's exit code and seconds to its own exit, by index in
    #: ``processes``, filled as ``wait_all`` sees them, so a run cut short still logs each
    #: finished helper's own wall time (#4511 review).
    ended: dict[int, tuple[int, float]] = field(default_factory=dict)


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
    """The Work's issue: ``given``, else the first ``Refs #n`` (or ``Closes``…) of the body.

    A bare number is the body's reference with that number, as a Work's issue may be in another
    repository than its pull request (#4641); else the pull request's own repository's. A body
    naming that number in two repositories is refused; a pull request's URL is no issue."""
    from .review_paperwork import _REFERENCE_RE

    references = [
        (
            (
                found.group("url_repo") or found.group("repo") or pull.repository,
                int(found.group("url_n") or found.group("n")),
            ),
            found.group(0).lower(),
        )
        for found in _REFERENCE_RE.finditer(pull.body)
    ]
    if given:
        match = _PULL_RE.match(given.strip())
        if match is None:
            raise NotRun(f"--issue must be `owner/repo#n` or a number, not {given!r}")
        number = int(match.group("n"))
        if match.group("repo"):
            return (match.group("repo"), number)
        named = {
            repository.lower(): repository
            for (repository, n), found in references
            if n == number and "/pull/" not in found
        }
        if len(named) > 1:
            raise NotRun(
                f"--issue {number} is ambiguous, the body names it in"
                f" {' and '.join(named.values())}: pass it as `owner/repo#n`"
            )
        return (next(iter(named.values()), pull.repository), number)
    return references[0][0] if references else None


def read_issue(repository: str, number: int, cwd: str) -> dict:
    return _gh_json(
        ["gh", "issue", "view", str(number), "--repo", repository, "--json", "title,body,comments"],
        cwd,
    )


def issue_text(repository: str, number: int, cwd: str, data: dict | None = None) -> str:
    """The issue's title and body, and any ``Use-case walkthrough:`` comment, for the prompt."""
    data = read_issue(repository, number, cwd) if data is None else data
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


def whole_context(repository: str, number: int, data: dict, pull: Pull) -> str:
    """The whole review's ``context.md`` (#4531): the pull request's title and body as read at
    this run, which is this head's (a later body cost #4512 five false findings), then the
    issue with every comment. The body comes first, so a cut at the end takes only comments."""
    text = f"# Pull request {pull.repository}#{pull.number}: {pull.title}\n\n{pull.body}\n\n"
    text += f"# Issue {repository}#{number}: {data.get('title') or ''}\n\n"
    text += f"{data.get('body') or ''}\n"
    text += "\n## Comments\n"
    for comment in data.get("comments") or ():
        if not isinstance(comment, dict):
            continue
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else ""
        text += f"\n--- {login or 'someone'} at {comment.get('createdAt') or '?'}\n"
        text += f"{comment.get('body') or ''}\n"
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + "\n[… the rest is cut]\n"
    return text


def _show(checkout: str | Path, ref: str, path: str) -> str:
    """``path`` at ``ref`` in ``checkout``, read without fetching: the checkout may be shared
    with a reviewer reading it at that moment (#4531 design review)."""
    return _git(checkout, "show", f"{ref}:{path}", strip=False)


_WORKFLOW_RE = re.compile(
    r"(?m)^\s+workflow_source:\s*\n\s+repository:\s*(?P<repo>\S+)\s*\n\s+path:\s*(?P<path>\S+)"
)


def reviewer_rules(
    repository: str, checkout: str, chat_repository: str, deck_root: Path
) -> tuple[dict[str, str], list[str]]:
    """The reviewer's rule files the whole review reads, by file name, and a warning per one
    that could not be read (#4531). Raises ``NotRun`` without the reviewer's ``SKILL.md``: the
    pass would then review by nobody's rules."""
    if not checkout:
        raise NotRun("no review-chat checkout (`[chat_board] review_fallback_checkout`)")
    where = Path(checkout).expanduser()
    try:
        skill = _show(where, REVIEWER_REF, f"{REVIEWER_DIR}/SKILL.md")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun(f"the reviewer's SKILL.md is unreadable in {checkout}: {exc}") from exc
    rules = {"reviewer-SKILL.md": skill}
    warnings: list[str] = []

    def read(name: str, root: Path, ref: str, path: str) -> str:
        try:
            rules[name] = _show(root, ref, path)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"the whole review lacks {path} ({exc})")
        return rules.get(name, "")

    read(
        "reviewer-review-policy.md",
        where,
        REVIEWER_REF,
        f"{REVIEWER_DIR}/references/review-policy.md",
    )
    read(
        "reviewer-baseline-review.md",
        where,
        REVIEWER_REF,
        f"{REVIEWER_DIR}/references/baseline-review.md",
    )
    wanted = repository.replace("/", "--").lower() + ".yaml"
    profile_name = "_default.yaml"
    with contextlib.suppress(RuntimeError, OSError, subprocess.TimeoutExpired):
        listed = _git(where, "ls-tree", "--name-only", REVIEWER_REF, f"{REVIEWER_DIR}/profiles/")
        for path in listed.splitlines():
            if path.rsplit("/", 1)[-1].lower() == wanted:
                profile_name = path.rsplit("/", 1)[-1]
    profile = read(
        f"reviewer-{profile_name} (this repository's profile)"
        if profile_name != "_default.yaml"
        else "reviewer-_default.yaml (the default profile; this repository has none of its own)",
        where,
        REVIEWER_REF,
        f"{REVIEWER_DIR}/profiles/{profile_name}",
    )
    source = _WORKFLOW_RE.search(profile)
    if not source and re.search(r"(?m)^\s*workflow_source:", profile):
        warnings.append(
            f"the whole review lacks the workflow source {profile_name} names: its"
            " `workflow_source` is not laid out as `repository:` then `path:` lines"
        )
    if source:
        if source.group("repo").lower() == chat_repository.lower():
            name = source.group("path").replace("/", "-").removeprefix("skills-")
            read(
                f"{name} (the profile's review.workflow_source)",
                where,
                REVIEWER_REF,
                source.group("path"),
            )
        else:
            warnings.append(
                f"the whole review lacks the profile's workflow source"
                f" {source.group('repo')}:{source.group('path')}"
                " (not in the review-chat repository)"
            )
    read(
        "rate-my-pr-REVIEW.md (the shared review contract, skills/rate-my-pr/REVIEW.md)",
        deck_root,
        DECK_REF,
        REVIEW_CONTRACT,
    )
    return rules, warnings


def deck_root() -> Path:
    """The checkout the running ``agentdeck`` package lives in (``~/agentdeck`` live)."""
    return Path(__file__).resolve().parents[2]


def review_chat_settings() -> tuple[str, str]:
    """The review-chat checkout and repository from the deck's config, or empty strings."""
    from .config import load_config

    try:
        board = load_config().chat_board
    except Exception:  # noqa: BLE001 - checked by ``account_roots`` already; empty is "none"
        return "", ""
    return board.review_fallback_checkout, board.repository


def write_whole_inputs(
    repo: Path, rules: dict[str, str], context: str, base: str, head: str
) -> tuple[Path, str]:
    """The whole review's inputs, inside its own clone's ``.git`` (readable to its sandbox, out
    of the work tree), and the diff's short stat."""
    inputs = Path(repo) / ".git" / "helper-review"
    (inputs / "rules").mkdir(parents=True, exist_ok=True)
    for name, text in rules.items():
        (inputs / "rules" / name.split(" ", 1)[0]).write_text(text, encoding="utf-8")
    (inputs / "context.md").write_text(context, encoding="utf-8")
    (inputs / "diff.patch").write_text(
        _git(repo, "diff", "-M", base, head, strip=False), encoding="utf-8"
    )
    return inputs, _git(repo, "diff", "--shortstat", base, head)


def whole_prompt(
    pull: Pull,
    repo: Path,
    inputs: Path,
    rules: dict[str, str],
    base: str,
    head: str,
    stat: str,
    warnings: list[str],
) -> str:
    listed = "".join(
        f"- {inputs / 'rules' / name.split(' ', 1)[0]}"
        + (f" {name.split(' ', 1)[1]}" if " " in name else "")
        + "\n"
        for name in rules
    )
    missing = "".join(f"- Missing here: {warning}.\n" for warning in warnings)
    return WHOLE_PROMPT.format(
        rules=listed,
        repo=pull.repository,
        number=pull.number,
        inputs=inputs,
        base=base,
        head=head,
        stat=stat or "no change",
        cwd=repo,
        missing=missing,
    )


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


#: What a run prints instead of checking a pull request whose review was asked for (#4726).
REVIEWED = (
    "A review of {ref} was already asked for, so the helper checks do not run again (#4726):"
    " the review reads this head. Post no `Helper checks:` comment for it. Pass --again only"
    " when a person asked for another helper run."
)


def review_asked(url: str, ref: str) -> bool:
    """Whether the deck says a review of ``ref`` was asked for yet, at any head (#4726): the
    helpers run only before a pull request's first review, since a review round later reads
    the fix itself (#4708: re-runs on later heads cost 5.6 units in six days). An unreadable
    answer is ``False``: the helpers then run as before."""
    import httpx

    try:
        answer = httpx.get(
            url.rstrip("/") + "/api/review-asked",
            params={"pull": ref},
            timeout=10.0,
            trust_env=False,
        )
        answer.raise_for_status()
        return answer.json().get("asked") is True
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, AttributeError):
        return False


def account_roots() -> dict[str, Path]:
    from .config import load_config

    try:
        return {account.key: Path(account.root) for account in load_config().build_accounts()}
    except Exception as exc:  # noqa: BLE001 - an unreadable config is a reason not to run
        raise NotRun(f"the deck's config is unreadable: {exc}") from exc


# ------------------------------------------------------------------------------- git


def _git(cwd: str | Path, *args: str, env: dict[str, str] | None = None, strip: bool = True) -> str:
    """Git's output, its bytes that are not UTF-8 replaced: a Latin-1 file in a diff must not
    end the run with a traceback (#4532 helper check)."""
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip()[:200] or f"exit {done.returncode}")
    return done.stdout.strip() if strip else done.stdout


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
        if _HEAD_FILE_RE.fullmatch(child.name):
            with contextlib.suppress(OSError):
                if now - child.stat().st_mtime > STALE_S:
                    _remove_head_file(child)
            continue
        if not _RUN_NAME_RE.fullmatch(child.name):
            continue  # only this command's own run directories
        with contextlib.suppress(OSError):
            if now - child.stat().st_mtime > STALE_S:
                shutil.rmtree(child, ignore_errors=True)


# ------------------------------------------------------------------ one run per head (#4531)

_HEAD_FILE_RE = re.compile(r".+-\d+-[0-9a-f]{12}\.(?:lock|report)(?:\.\d+\.part)?")


def head_files(pull: Pull, root: Path | None = None) -> tuple[Path, Path]:
    """The lock and the report file of the run for this pull request's head."""
    root = ROOT if root is None else root
    key = f"{pull.repository.replace('/', '--')}-{pull.number}-{pull.head_oid[:12].lower()}"
    return root / f"{key}.lock", root / f"{key}.report"


def take_head_lock(path: Path) -> tuple[int | None, str]:
    """An fd holding this head's lock, or ``None`` and the holder's pid. The kernel frees it
    when the holder dies, even by SIGKILL, so it never goes stale."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = os.read(fd, 32).decode(errors="replace").strip()
        os.close(fd)
        return None, holder or "?"
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd, ""


def _remove_head_file(path: Path) -> None:
    """Remove a day-old lock or report; a lock only while nobody holds it."""
    if path.suffix != ".lock":
        path.unlink(missing_ok=True)
        return
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path.unlink(missing_ok=True)
    except OSError:
        pass
    finally:
        os.close(fd)


def save_report(path: Path, text: str) -> None:
    """The report, beside the lock, for a caller that is gone by the time the run ends (#4531
    design review): written whole or not at all, and never stopping the run."""
    try:
        partial = path.with_name(path.name + f".{os.getpid()}.part")
        partial.write_text(text + "\n", encoding="utf-8")
        os.replace(partial, path)
    except OSError as exc:
        _say(f"(the report file {path} cannot be written: {exc})", stream=sys.stderr)


def _say(text: str, stream=None) -> None:
    """Print, and keep going when nobody reads any more (#4531 design review): the run can
    outlive the call that started it in the background, and the usage lines and the report
    file must still be written. A dead stream is pointed at ``/dev/null`` so Python's own flush
    at exit does not fail on it either."""
    stream = sys.stdout if stream is None else stream
    try:
        print(text, file=stream, flush=True)
    except (OSError, ValueError):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), stream.fileno())


# --------------------------------------------------------------------------- the helpers


def helper_command(binary: str = "claude", repo: Path | None = None, part: int = 1) -> list[str]:
    """One helper's argv, ``timeout`` first so its limit holds whatever happens to us."""
    return [
        "timeout",
        "-k",
        "10",
        str(timeout_of(part)),
        binary,
        "-p",
        "--model",
        model_of(part),
        # One ``result`` per turn: ``json`` prints the last one only, which lost the findings
        # when a background command's end started another turn (#4656).
        "--output-format",
        "stream-json",
        "--verbose",
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
    # A background command that ends after the findings starts one more turn (#4656).
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
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
        run.parts.append(part)
        tmp.mkdir(parents=True, exist_ok=True)
        (out / f"{part}.prompt").write_text(prompt, encoding="utf-8")
        with (out / f"{part}.prompt").open() as stdin, (out / f"{part}.json").open("w") as stdout:
            run.processes.append(
                subprocess.Popen(
                    helper_command(repo=repos[part], part=part),
                    stdin=stdin,
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                    cwd=repos[part],
                    env={**env, "TMPDIR": str(tmp)},
                    preexec_fn=_die_with_parent if sys.platform == "linux" else None,
                )
            )


#: At a line's start only: a helper's notes may name the heading before its list does.
_HEADING_RE = re.compile(r"(?m)^[ \t]*## Findings")


def findings_of(text: str) -> str:
    """The ``## Findings`` list of a helper's reply, or the whole reply when it has none. The
    first heading counts: a finding may quote another one, fenced, quoted or indented, and a
    cut there would lose the findings above it. At worst some notes show too (#4656)."""
    heading = _HEADING_RE.search(text)
    return (text[heading.end() :] if heading else text).strip() or "No findings."


_JSON_BLOCK_RE = re.compile(r"```json[ \t]*\r?\n(?P<body>.*?)\r?\n[ \t]*```", re.S)


def _findings_block(text: str) -> list[dict] | None:
    """The findings in a whole review's last ```json block, ``None`` when it cannot be read."""
    blocks = list(_JSON_BLOCK_RE.finditer(text))
    data = None
    if blocks:
        with contextlib.suppress(ValueError):
            # ``strict=False``: a raw newline inside a finding's text (#4532 helper check)
            data = json.loads(blocks[-1].group("body"), strict=False)
    found = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(found, list) or not all(isinstance(item, dict) for item in found):
        return None
    return found


def whole_findings(text: str, floor: int = CONFIDENCE_FLOOR) -> tuple[str, int]:
    """The whole review's findings at ``floor`` or above as a numbered list, and how many fell
    under it (#4531). They come from the reply's last ```json block. Only a finite number at
    the floor passes it; a finding with no such number is listed apart, under a note, never
    passed off as scored (#4532 review). A reply whose block cannot be read, or holds an
    entry that is not a finding object, gives its own text, as a Haiku helper's reply
    without a ``## Findings`` list does: nothing in it is lost in silence."""
    found = _findings_block(text)
    if found is None:
        reply = text.strip() or "No findings."
        return "(No readable findings block; the reply as it is:)\n" + reply, 0
    kept, unscored, dropped = [], [], 0
    for item in found:
        confidence = item.get("confidence")
        if isinstance(confidence, str):  # "40" counts as 40 (#4532 helper check)
            with contextlib.suppress(ValueError):
                confidence = float(confidence.strip())
        scored = (
            isinstance(confidence, int | float)
            and not isinstance(confidence, bool)
            and math.isfinite(confidence)
        )
        if scored and confidence < floor:
            dropped += 1
            continue
        where = str(item.get("file") or "")
        if where and isinstance(item.get("line"), int) and not isinstance(item.get("line"), bool):
            where += f":{item['line']}"
        kind = str(item.get("kind") or "finding")
        into = kept if scored else unscored
        label = f", confidence {confidence:g}" if scored else ""
        into.append(
            f"{len(into) + 1}. {where + ' ' if where else ''}[{kind}{label}]"
            f" {str(item.get('text') or '').strip()}"
        )
    lines = kept or ["No findings."]
    if unscored:
        lines += [
            f"\n(Without a readable confidence, so not checked against the floor of {floor};"
            " judge each as a suspicion:)",
            *unscored,
        ]
    return "\n".join(lines), dropped


def _events(raw: str) -> list[dict]:
    """The output's JSON lines. Split on newlines only: ``splitlines`` also cuts at U+2028,
    which JSON leaves unescaped inside a text."""
    events = []
    for line in raw.split("\n"):
        with contextlib.suppress(ValueError):
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
    return events


def _result_event(events: list[dict]) -> dict | None:
    """The CLI's last ``result``: its cost and ``modelUsage`` cover the whole run (checked on
    Claude Code 2.1.317), its ``result`` and ``duration_ms`` only the last turn."""
    results = [event for event in events if event.get("type") == "result"]
    return results[-1] if results else None


def _has_findings(part: int, text: str) -> bool:
    return _findings_block(text) is not None if part == WHOLE else bool(_HEADING_RE.search(text))


def replies_of(part: int, events: list[dict]) -> list[str]:
    """Each finished turn's reply, in order. A turn whose reply lacks the findings also gives
    the messages it wrote them in, before its last one (#4656)."""
    replies, messages = [], []
    for event in events:
        if event.get("type") == "result":
            reply = str(event.get("result") or "")
            if not event.get("is_error"):
                if not _has_findings(part, reply):
                    replies += [text for text in messages if _has_findings(part, text)]
                if reply:
                    replies.append(reply)
            messages = []
        elif event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else ():
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    messages.append(block["text"])
    return list(dict.fromkeys(replies))


def all_findings(part: int, replies: list[str]) -> tuple[str, int]:
    """Every reply in order: its findings, or the reply as it is, since a later message may
    correct a finding (#4656). The count under the floor is the last list's."""
    if not any(_has_findings(part, reply) for reply in replies):
        replies = ["\n\n".join(replies)]  # no findings in any: the whole text as it is
    shown: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for reply in replies:
        if len(replies) > 1 and not _has_findings(part, reply):
            shown.append(f"(Another message from this helper:)\n{reply.strip()}")
            continue
        text, dropped = whole_findings(reply) if part == WHOLE else (findings_of(reply), 0)
        if text not in seen:  # the same list given twice is shown once
            shown.append(f"(A later list from this helper:)\n{text}" if seen else text)
            seen.add(text)
    return "\n\n".join(shown), dropped


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


#: A usage log field and the API's name for it in a message's or a result's ``usage``.
_TOKEN_FIELDS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_tokens", "cache_read_input_tokens"),
    ("cache_write_tokens", "cache_creation_input_tokens"),
)


def usage_of(event: dict | None, model: str = MODEL) -> dict:
    """The tokens and cost one helper's ``result`` reports (#4510). ``modelUsage`` holds the
    whole run per model; the top-level ``usage`` is the fallback. Checked on Claude Code's
    ``--output-format json`` on 2026-11-19: ``modelUsage[<model>]`` carries ``inputTokens``,
    ``outputTokens``, ``cacheReadInputTokens``, ``cacheCreationInputTokens`` and ``costUSD``,
    and ``total_cost_usd`` the whole cost. ``cost_usd`` is ``None`` when the CLI reported none."""
    usage = {
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": None,
    }
    if not isinstance(event, dict):
        return usage
    per_model = event.get("modelUsage")
    per_model = {
        name: row
        for name, row in (per_model.items() if isinstance(per_model, dict) else ())
        if isinstance(row, dict)
    }
    if per_model:
        usage["model"] = ",".join(sorted(per_model))
        for key, field_ in (
            ("input_tokens", "inputTokens"),
            ("output_tokens", "outputTokens"),
            ("cache_read_tokens", "cacheReadInputTokens"),
            ("cache_write_tokens", "cacheCreationInputTokens"),
        ):
            usage[key] = sum(_count(row.get(field_)) for row in per_model.values())
    elif isinstance(event.get("usage"), dict):
        top = event["usage"]
        for key, field_ in _TOKEN_FIELDS:
            usage[key] = _count(top.get(field_))
    cost = event.get("total_cost_usd")
    if isinstance(cost, int | float) and not isinstance(cost, bool):
        usage["cost_usd"] = float(cost)
    else:  # no total: the per-model costs, when the CLI gave any (#4510 helper check)
        costs = [
            row["costUSD"]
            for row in per_model.values()
            if isinstance(row.get("costUSD"), int | float) and not isinstance(row["costUSD"], bool)
        ]
        usage["cost_usd"] = float(sum(costs)) if costs else None
    return usage


def usage_so_far(events: list[dict], model: str = MODEL) -> dict:
    """What a run spent as far as its output shows (#4728): its last ``result``'s usage, plus
    each message's own after it. A helper that timed out or was stopped prints no ``result``
    for its last turn, often for none, and was logged as having spent nothing.

    A message's ``usage`` is one model call's; the stream repeats a message once per content
    block, so each id counts once, at its largest reading. The output count is the stream's
    last reading, which may be short of the final one, and such a tail has no cost.
    """
    last = max((i for i, event in enumerate(events) if event.get("type") == "result"), default=-1)
    usage = usage_of(events[last] if last >= 0 else None, model)
    calls: dict[object, dict] = {}
    for index, event in enumerate(events[last + 1 :]):
        message = event.get("message") if event.get("type") == "assistant" else None
        spent = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(spent, dict):
            continue
        call = calls.setdefault(message.get("id") or index, {"model": message.get("model")})
        for key, field_ in _TOKEN_FIELDS:
            call[key] = max(call.get(key, 0), _count(spent.get(field_)))
    if not calls:
        return usage
    for key, _ in _TOKEN_FIELDS:
        usage[key] += sum(call[key] for call in calls.values())
    models = {call["model"] for call in calls.values() if isinstance(call["model"], str)}
    if last < 0 and models:
        usage["model"] = ",".join(sorted(models))
    return usage


def written_of(events: list[dict]) -> dict:
    """How much a run's output shows (#4754): the characters of its reply text and of its
    tool calls, and how many of those it made. Output tokens also pay for thinking, which the
    output does not show; beside these lengths the token report splits them into reply, tool
    calls and thinking. A subagent's messages count too: its tokens are in the run's usage.

    The stream gives each content block once; a block seen again under the same message id
    is counted once.
    """
    written = {"reply_chars": 0, "tool_chars": 0, "tool_calls": 0}
    seen: set[tuple] = set()
    for event in events:
        message = event.get("message") if event.get("type") == "assistant" else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            text, asked = block.get("text"), block.get("type") == "tool_use"
            if not asked and not isinstance(text, str):
                continue
            key = (message.get("id"), block.get("id") if asked else text)
            if message.get("id") and key in seen:
                continue
            seen.add(key)
            if asked:
                written["tool_chars"] += len(json.dumps(block.get("input") or {}))
                written["tool_calls"] += 1
            else:
                written["reply_chars"] += len(text)
    return written


def _spent(events: list[dict], model: str) -> dict:
    """A usage log line's own fields: what the run spent, and how much its output shows."""
    return {**usage_so_far(events, model), **written_of(events)}


def _read_events(path: Path) -> tuple[str, list[dict]]:
    raw = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return raw, _events(raw)


def read_outcome(part: int, path: Path, code: int, wall_s: float = 0.0) -> Outcome:
    raw, events = _read_events(path)
    event = _result_event(events)
    # A failed helper may still have spent tokens: its usage is kept whatever the exit (#4510),
    # and one that printed no ``result`` keeps what its messages say (#4728).
    spent = {"usage": _spent(events, model_of(part)), "wall_s": wall_s}
    if code in (124, 137):  # ``timeout``'s own exit, or its SIGKILL after ``-k``
        return Outcome(
            part,
            failed=f"timed out after {timeout_of(part) // 60} minutes",
            exit="timeout",
            **spent,
        )
    replies = replies_of(part, events)
    last_failed = event is not None and (event.get("is_error") or not event.get("result"))
    # A later turn that failed does not take an earlier turn's findings with it (#4656).
    kept = last_failed and any(_has_findings(part, reply) for reply in replies)
    # A result printed before a failure is not a finished helper (#4437 review).
    if code != 0 and not kept:
        last = (raw.strip().splitlines()[-1:] or [""])[0][:200]
        return Outcome(
            part, failed=f"exit {code}" + (f": {last}" if last else ""), exit="failed", **spent
        )
    if event is None or (last_failed and not kept):
        why = (raw.strip().splitlines()[-1:] or [f"exit {code}"])[0][:200]
        if event is not None and event.get("is_error"):
            why = str(event.get("result") or event.get("subtype") or why)[:200]
        return Outcome(part, failed=why, exit="failed", **spent)
    findings, dropped = all_findings(part, replies)
    if kept:
        error = event.get("subtype") if event.get("is_error") else ""
        why = str(event.get("result") or error or "no reply")[:200]
        findings += f"\n\n(This helper's last turn failed: {why})"
    turns = [turn for turn in events if turn.get("type") == "result"]
    return Outcome(
        part,
        findings=findings,
        dropped=dropped,
        minutes=sum(float(turn.get("duration_ms") or 0) for turn in turns) / 60000,
        cost_usd=float(spent["usage"].get("cost_usd") or 0),  # per-model costs without a total
        **spent,
    )


def note_ended(run: Run, started: float) -> None:
    """Record the exit of each helper that has ended since the last look."""
    for index, process in enumerate(run.processes):
        if index not in run.ended and (code := process.poll()) is not None:
            run.ended[index] = (code, time.monotonic() - started)


def wait_all(run: Run, started: float) -> list[tuple[int, float]]:
    """Each helper's exit code and its seconds from ``started`` to its own exit."""
    while True:
        note_ended(run, started)
        if len(run.ended) == len(run.processes):
            return [run.ended[index] for index in range(len(run.processes))]
        time.sleep(0.1)


def usage_log_path() -> Path:
    given = os.environ.get("AGENTDECK_HELPER_USAGE_LOG")
    if given:
        return Path(given).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser() / USAGE_LOG


def log_usage(
    pull: str, head: str, account: str, outcomes: Iterable[Outcome], why: str = ""
) -> None:
    """Append one JSON line per helper to the usage log (#4510). Each line is one ``write`` on
    an ``O_APPEND`` file, so two runs at once do not mix their lines. A log that cannot be
    written is said on stderr and never stops the report."""
    at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    lines = []
    for outcome in outcomes:
        usage = outcome.usage or usage_of(None, model_of(outcome.part))
        lines.append(
            json.dumps(
                {
                    "at": at,
                    "pull": pull,
                    "head": head,
                    "account": account,
                    "helper": outcome.part,
                    "name": NAMES[outcome.part],
                    **usage,
                    "wall_s": round(outcome.wall_s, 1),
                    "exit": outcome.exit,
                    "why": why or outcome.failed,
                },
                sort_keys=False,
            )
            + "\n"
        )
    path: Path | str = os.environ.get("AGENTDECK_HELPER_USAGE_LOG") or USAGE_LOG
    try:
        path = usage_log_path()  # no home folder raises RuntimeError (#4510 helper check)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            for line in lines:
                log.write(line)
                log.flush()
    except (OSError, RuntimeError) as exc:
        _say(f"(the helpers' usage log {path} cannot be written: {exc})", stream=sys.stderr)


def not_run(part: int, why: str) -> Outcome:
    return Outcome(part, failed=why, exit="did_not_run", usage={**usage_of(None), "model": ""})


def log_not_run(pull: str, why: str, head: str = "", account: str = "") -> None:
    """A ``did_not_run`` line for each helper of a run that never started them."""
    log_usage(pull, head, account, [not_run(part, why) for part in active_parts()])


def log_cut_short(run: Run, pull: str, head: str, account: str, started: float) -> None:
    """The run was killed or failed while its helpers ran (#4510 helper check): a helper that
    had finished gets its own line from its output, one still running an ``interrupted`` line
    with what its output shows so far (#4728; a message still on its way is not in it).
    Called before ``stop`` removes the output."""
    note_ended(run, started)  # one that ended since ``wait_all`` last looked
    outcomes = []
    for index, part in enumerate(run.parts[: len(run.processes)]):
        output = run.directory / "out" / f"{part}.json"
        if index not in run.ended:
            outcomes.append(
                Outcome(
                    part,
                    failed="the run was stopped",
                    exit="interrupted",
                    usage=_spent(_read_events(output)[1], model_of(part)),
                    wall_s=time.monotonic() - started,
                )
            )
            continue
        code, wall_s = run.ended[index]
        outcomes.append(read_outcome(part, output, code, wall_s))
    log_usage(pull, head, account, outcomes)


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
        title = NAMES[outcome.part]
        if outcome.part == WHOLE:
            title += f" by {WHOLE_MODEL}, confidence {CONFIDENCE_FLOOR} or above"
        lines.append("")
        if outcome.failed:
            lines.append(f"## Helper {outcome.part} ({title}): did not run ({outcome.failed})")
            continue
        lines.append(
            f"## Helper {outcome.part} ({title}), {outcome.minutes:.1f} min,"
            f" ${outcome.cost_usd:.2f}"
        )
        lines.append(outcome.findings)
        if outcome.dropped:
            lines.append(f"({outcome.dropped} more under confidence {CONFIDENCE_FLOOR} left out.)")
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


_HELD = {signal.SIGTERM, signal.SIGHUP}


@contextlib.contextmanager
def _signals_held():
    """SIGTERM and SIGHUP wait until the block ends, so writing the usage lines and noting
    them written happen together or not at all: a kill between the two would log them twice
    (#4511 review). The block swaps in a handler that only notes the signal, then puts the
    old handlers back and raises the first one noted, so ``_on_signal`` stops the command.

    Not ``pthread_sigmask``: that holds a signal for the calling thread only, and any other
    thread of the process (a pytest-xdist worker has some) took the kill and ran the handler
    in the main thread mid-block (#4520). Python handlers run in the main thread, whichever
    thread the kernel picked, so swapping them holds the signal for the whole process."""
    noted: list[int] = []

    def note(signum, _frame) -> None:
        noted.append(signum)

    old = {}
    try:
        # Inside the ``try``: a signal that stops the swap halfway still gets the swapped
        # handlers put back (#4522 helper check).
        for signum in _HELD:
            old[signum] = signal.signal(signum, note)
        yield
    finally:
        for signum, handler in old.items():
            signal.signal(signum, handler)
        if noted:
            signal.raise_signal(noted[0])


def run_helpers(args: argparse.Namespace) -> int:
    checkout = str(Path(args.cwd).expanduser().resolve())
    try:
        # First, so a Codex chat without escalation hears to escalate, not that gh failed.
        check_network()
        pull = read_pull(args.pull, checkout)
        found = issue_of(pull, args.issue or "")
    except NotRun as exc:
        _say(did_not_run(str(exc), spec_ref(args.pull, checkout)))
        log_not_run(spec_ref(args.pull, checkout) or args.pull, str(exc))
        return 0
    ref = f"{pull.repository}#{pull.number}"
    if found is None:
        why = "the pull request body names no issue: add `Refs #<issue>` or pass --issue"
        _say(did_not_run(why, ref))
        log_not_run(ref, why, pull.head_oid)
        return 0
    issue = f"{found[0]}#{found[1]}"
    # Before the lock and the saved report: a head checked before its review would otherwise
    # print that whole report again, which is the reading this skip saves (#4739 review).
    if not getattr(args, "again", False) and review_asked(args.url, ref):
        _say(REVIEWED.format(ref=ref))
        log_not_run(ref, "a review was already asked for", pull.head_oid)
        return 0
    lock_fd = None
    if pull.head_oid:
        # Before anything is spent: one run per pull request and head (#4531 design review).
        lock, saved = head_files(pull)
        try:
            lock_fd, holder = take_head_lock(lock)
        except OSError as exc:
            # Fail closed: without the lock a second paid run could go at once (#4532 review).
            why = f"this head's lock {lock} cannot be taken: {exc}"
            log_not_run(ref, why, pull.head_oid)
            _say(did_not_run(why, ref))
            return 0
        if lock_fd is None and holder:
            _say(
                f"A helper run for {ref} at {pull.head_oid[:12]} is already running (pid"
                f" {holder}): wait for it to end and post from its report. If its caller is"
                f" gone, run this again once it has ended: that prints the report it kept in"
                f" {saved}, or checks the head itself when that run checked nothing."
            )
            return 1
        if lock_fd is not None and saved.is_file() and not getattr(args, "again", False):
            try:
                earlier = saved.read_text(encoding="utf-8")
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(saved.stat().st_mtime))
            except OSError:
                earlier = ""
            if earlier:
                os.close(lock_fd)
                _say(
                    f"The helpers already checked {ref} at {pull.head_oid[:12]} ({when}); their"
                    f" report, from {saved}, follows. Post from it if you have not yet; pass"
                    " --again to check this head again.\n"
                )
                _say(earlier.rstrip("\n"))
                return 0
    try:
        return _run_locked(args, checkout, pull, ref, issue, found)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _run_locked(
    args: argparse.Namespace,
    checkout: str,
    pull: Pull,
    ref: str,
    issue: str,
    found: tuple[str, int],
) -> int:
    from .host_slots import helper_slot_paths, take_a_slot

    warnings: list[str] = []
    directory = ROOT / f"{pull.repository.replace('/', '--')}-{pull.number}-{uuid.uuid4().hex[:8]}"
    run = Run(directory)
    account = ""
    head = pull.head_oid
    started = time.monotonic()
    logged = False
    skipped: list[Outcome] = []
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _on_signal)
    try:
        try:
            data = read_issue(found[0], found[1], checkout)
            text = issue_text(found[0], found[1], checkout, data)
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
            parts = list(active_parts())
            rules: dict[str, str] = {}
            missing: list[str] = []
            if WHOLE in parts:
                try:
                    chat_checkout, chat_repository = review_chat_settings()
                    rules, missing = reviewer_rules(
                        pull.repository, chat_checkout, chat_repository, deck_root()
                    )
                    warnings += missing
                except NotRun as exc:
                    # The whole review alone does not run; the Haiku helpers go on (#4531).
                    parts.remove(WHOLE)
                    skipped.append(not_run(WHOLE, str(exc)))
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
                part: make_clone(checkout, directory, head, base, f"repo-{part}") for part in parts
            }
            if WHOLE in repos:
                inputs, stat = write_whole_inputs(
                    repos[WHOLE], rules, whole_context(found[0], found[1], data, pull), base, head
                )
            bin_dir = directory / "bin"
            fake_gh(bin_dir)
            env = helper_env(bin_dir, roots[account])
        except NotRun as exc:
            logged = True  # every helper's line, the skipped whole review's included
            log_not_run(ref, str(exc), pull.head_oid, account)
            _say(did_not_run(str(exc), ref))
            return 0
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            logged = True
            log_not_run(ref, f"the run cannot be prepared: {exc}", pull.head_oid, account)
            _say(did_not_run(f"the run cannot be prepared: {exc}", ref))
            return 0
        prompts = {
            part: PROMPT.format(title=pull.title, cwd=repos[part], pass_text=text_, issue=text)
            for part, (_title, text_) in PASSES.items()
        }
        if WHOLE in repos:
            prompts[WHOLE] = whole_prompt(
                pull, repos[WHOLE], inputs, rules, base, head, stat, missing
            )
        longest = max(timeout_of(part) for part in prompts) // 60
        _say(f"Starting {len(prompts)} helpers on {account}; this takes up to {longest} minutes.")
        run.transcripts = [transcript_dir(roots[account], repo) for repo in repos.values()]
        started = time.monotonic()
        try:
            start_helpers(run, repos, prompts, env)
        except OSError as exc:
            _say(did_not_run(f"the helpers cannot start: {exc}", ref))
            # One that did start before the failure is stopped by ``stop`` a moment after
            # it began, before it could spend anything, so every helper counts as not run.
            with _signals_held():
                log_not_run(ref, f"the helpers cannot start: {exc}", head, account)
                logged = True
            return 0
        ended = wait_all(run, started)
        outcomes = [
            read_outcome(part, directory / "out" / f"{part}.json", code, wall_s)
            for part, (code, wall_s) in zip(prompts, ended, strict=True)
        ] + skipped
        with _signals_held():
            log_usage(ref, head, account, outcomes)
            logged = True
        text = report(pull, issue, head, base, account, outcomes, warnings)
        if any(not outcome.failed for outcome in outcomes):
            # Before the lock is let go: a run refused on it then finds the report (#4531).
            save_report(head_files(pull)[1], text)
        _say(text)
        return 0
    finally:
        # Held here too: a second kill must not cut the cut-short lines in half, or skip
        # ``stop`` and leave helpers running (#4511 review).
        with _signals_held():
            if run.processes and not logged:
                # Killed (SIGTERM, SIGHUP) or failed while the helpers ran: log before
                # ``stop`` removes their output (#4510 helper check). A SIGKILL logs nothing.
                with contextlib.suppress(Exception):
                    log_cut_short(run, ref, head, account, started)
                    if skipped:
                        log_usage(ref, head, account, skipped)
            elif skipped and not logged:
                # Killed before any helper started, say in the slot wait (#4532 helper check).
                with contextlib.suppress(Exception):
                    log_usage(ref, head, account, skipped)
            stop(run)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agentdeck pre-review-helpers",
        description="Three Haiku helpers check a pull request's change before its first review,"
        " and print what they found (#4437). Takes up"
        " to about 22 minutes: queue it as an `agentdeck job`.",
    )
    parser.add_argument(
        "--again",
        action="store_true",
        help="check a head the helpers already checked, instead of printing their report",
    )
    parser.add_argument("--cwd", required=True, help="the pull request's checkout")
    parser.add_argument("--pull", required=True, help="owner/repo#n, or n in --cwd's repository")
    parser.add_argument(
        "--issue",
        default="",
        help="owner/repo#n, or n: the body's Refs with that number, else --pull's repository's;"
        " default: the body's first Refs",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("AGENTDECK_URL", "http://127.0.0.1:8756"),
        help="running AgentDeck base URL",
    )
    return run_helpers(parser.parse_args(argv))
