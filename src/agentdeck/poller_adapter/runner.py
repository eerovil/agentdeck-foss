"""One wake, end to end: run the engine, then act on what it decided (#180 slice 4).

The engine is deliberately incapable of delivery. It reads the board, decides, records its
intent durably, and prints one directive per line on stdout. Everything after that — making a
workspace, spawning a worker, writing the receipt — is host work, and it lives here so the
engine stays a pure decision process that can be run read-only against production.

The order is the whole design:

1. **Ask the project's adapter to prepare its workspaces.** The adapter may return a shared
   cwd with no owned worktree, the default single worktree, or several repositories.
2. **Register every owned worktree before delivery, all of them or none.** Registration is
   generation-bound and durable, so even a refused or uncertain delivery cannot leak a
   prepared checkout. A refusal means no worker is coming, so what this wake created is given
   back on the spot and anything that survives is named in the skip line: a prepared worktree
   is either the engine's to clean or gone, never a directory nobody mentions again.
3. **Deliver, then write the receipt** — and only for a positively accepted delivery. An
   uncertain delivery writes nothing and is never retried here; the engine awaits it and
   retries under a fresh generation, which disables the ambiguous predecessor. Retrying here
   is the one reliable way to get two workers on one card.
4. **Report the initial worktrees in the receipt as a compatibility copy.** Older hosts still
   learn them on acknowledgement; current hosts have already persisted them in engine state.

A directive whose workspace cannot be prepared is skipped, not half-delivered: the engine sees
no acknowledgement and decides for itself on the next wake.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .. import github_identity
from ..git_context import github_repository

# The engine owns the rule for what a destructive worktree path may be. The runner has to undo a
# preparation the engine never accepted, so it validates by that same rule rather than inventing
# a second, weaker one.
from ..poller_engine import reviews
from ..poller_engine.config import CLOSING_KEYWORDS_PROHIBITED, CLOSING_KEYWORDS_REQUIRED
from ..poller_engine.cycle import FAILING_TESTS_GUIDE, WORK_RESOLUTION_ASK
from ..poller_engine.dispatch import WORKSPACE_REFUSAL
from ..poller_engine.github import (
    RELEASE_STEP_MARKER,
    public_session_url,
    registered_worktree,
)
from ..providers.instructions import USE_CASE_WALKTHROUGH_LINE, USE_CASE_WALKTHROUGH_NOT_APPLICABLE
from .control import write_build_brief, write_card_control
from .work_workspaces import ARTIFACT, IMPLEMENTATION, ClaimedWorkspace, WorkWorkspaces
from .worker import (
    REOPEN_ASK,
    SPEC_APPROVED_ASK,
    SPEC_ONLY_QUESTION_RULE,
    USE_CASE_REVIEW_RULE,
    WORK_TITLE_ASK,
    DeliveryOutcome,
    WorkerDispatcher,
    _compact_poller_brief,
    push_rules,
    spec_approval_rule,
)

MAX_WORKTREES_PER_CARD = 8
MAX_CHANGED_FILES_PER_CARD = 20
MAX_PATH_LIST_CHARS = 1_500
MAX_RECENT_HISTORY_COMMITS = 5
MAX_RECENT_HISTORY_PATHS = 20
ISSUE_REF_RE = re.compile(
    r"(?im)^\s*(?:refs?|fix(?:e[sd])?|close[sd]?)\s+"
    r"(?P<ref>(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+#\d+|#\d+)\b"
)


@dataclass(frozen=True)
class CheckoutFreshness:
    """What a checkout's own working tree holds, once this wake has tried to refresh it.

    A worker's worktree is made from a freshly fetched base, so the code *in it* is current.
    The checkout it hangs off is a separate working tree that nothing updates, and a worker
    that reads a file there by absolute path — a sibling repository, the parent directory,
    anything outside its own worktree — reads whatever that tree happens to be parked on
    (#1853). That is silent: the file exists, it just says something that stopped being true
    weeks ago.
    """

    path: Path
    base: str
    branch: str = ""
    behind: int = 0
    # Commits this tree has that `origin/<base>` does not. Being ahead makes a read here just as
    # untrue as being behind: the file says what somebody wrote locally and never pushed, which
    # is not what the base branch holds. Measuring only one direction certified exactly that as
    # current.
    ahead: int = 0
    dirty: int = 0
    readable: bool = True
    # The fetch itself succeeded. When it did not — this host cannot reach one project's remote
    # over SSH at all — every count below is measured against a remote ref that is itself old,
    # so the checkout may be further behind than it looks and cannot be called current.
    fetched: bool = True

    @property
    def current(self) -> bool:
        """Reading code here gives the same answers as reading the base branch."""
        return (
            self.readable
            and self.fetched
            and self.branch == self.base
            and not self.behind
            and not self.ahead
            and not self.dirty
        )

    def describe(self) -> str:
        """Why it is not current, in the terms a worker can check itself."""
        if not self.readable:
            return "could not be read, so treat it as stale"
        reasons = []
        if self.branch and self.branch != self.base:
            reasons.append(f"on branch `{self.branch}`, not `{self.base}`")
        if self.behind:
            reasons.append(f"{self.behind} commit(s) behind `origin/{self.base}`")
        if self.ahead:
            reasons.append(
                f"{self.ahead} unpushed commit(s) `origin/{self.base}` does not have"
            )
        if self.dirty:
            reasons.append(f"{self.dirty} uncommitted file(s)")
        if not self.fetched:
            reasons.append(
                f"`origin/{self.base}` could not be fetched, so it may be further behind than "
                "this says"
            )
        return ", ".join(reasons) or "not current"


@dataclass
class Workspace:
    """Where a card's worker runs, and the branch it works on.

    ``created`` says this wake made the directory rather than reusing one that was already
    there. Only a freshly created worktree may be removed again when the delivery is
    abandoned: a reused one holds work from an earlier lease, and deleting that to tidy up
    a refused registration would destroy the very thing the card exists to produce.
    """

    path: Path
    branch: str
    created: bool = False
    base_branch: str = "master"
    # The state of the checkout this worktree hangs off, when the wake looked (#1853).
    checkout_state: CheckoutFreshness | None = None
    # The Work owns it (#2149): an abandoned delivery never removes it, whoever made it.
    work_claimed: bool = False


def _claimed(answer: ClaimedWorkspace) -> Workspace:
    assert answer.path is not None
    return Workspace(
        path=answer.path,
        branch=answer.branch,
        created=answer.created,
        base_branch=answer.base_ref,
        work_claimed=True,
    )


@dataclass(frozen=True)
class PreparedWorkspaces:
    """The host context a project adapter prepared for one delivery.

    ``cwd`` is where the worker starts; ``workspaces`` are only the disposable host
    worktrees this card owns.  Those are deliberately separate: an issue-only card may
    start in a shared project directory while owning no worktree at all.
    """

    cwd: Path
    workspaces: tuple[Workspace, ...] = ()
    owner: str | None = None
    # The code repository whose checkout this card borrowed, when the card's own repository
    # holds no code (#1087). Empty for the ordinary case. This is the worker's starting
    # repository, but another repository may own the implementation the card asks for (#1209).
    borrowed_from: str = ""

    @property
    def branch(self) -> str:
        return self.workspaces[0].branch if self.workspaces else ""

    @property
    def base(self) -> str:
        """The branch the work is based on, which the worker is told never to push to.

        Named rather than assumed for the same reason the worktree's base is (#1166): telling a
        worker on a `main` repository never to push to `master` forbids nothing it could do and
        leaves the branch that matters unmentioned.
        """
        return self.workspaces[0].base_branch if self.workspaces else "master"

    @property
    def paths(self) -> list[str]:
        return [str(item.path) for item in self.workspaces]

    @property
    def checkout_states(self) -> list[CheckoutFreshness]:
        """What this wake learned about the checkouts these worktrees hang off (#1853)."""
        seen: dict[Path, CheckoutFreshness] = {}
        for item in self.workspaces:
            if item.checkout_state is not None:
                seen.setdefault(item.checkout_state.path, item.checkout_state)
        return list(seen.values())


class WorkspaceAdapter(Protocol):
    """Project-owned workspace preparation behind the host runner's stable seam."""

    def prepare(self, directive: dict) -> PreparedWorkspaces | None:
        """Prepare zero or more owned worktrees and choose the worker's cwd."""


@dataclass
class WakeResult:
    directives: int = 0
    delivered: int = 0
    refused: int = 0
    uncertain: int = 0
    skipped: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.directives} directive(s): {self.delivered} delivered, "
            f"{self.refused} refused, {self.uncertain} uncertain, {len(self.skipped)} skipped"
        )


def _run(
    command: list[str],
    cwd: Path | None = None,
    timeout: float = 300.0,
    env: dict[str, str] | None = None,
):
    return subprocess.run(
        command, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        timeout=timeout, check=False, env=env,
    )


def changed_files(worktree: Path, *, base_branch: str = "master", runner=_run) -> set[str] | None:
    """Committed and local paths changed by one in-flight worktree, or ``None`` if unreadable."""
    local_commands = (
        ["git", "-C", str(worktree), "diff", "--name-only", "-z", "HEAD", "--"],
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
    )
    paths: set[str] = set()
    try:
        for command in local_commands:
            done = runner(command, timeout=10.0)
            if done.returncode != 0:
                return None
            paths.update(path for path in (done.stdout or "").split("\0") if path)
        committed = runner(
            ["git", "-C", str(worktree), "diff", "--name-only", "-z",
             f"origin/{base_branch}...HEAD", "--"],
            timeout=10.0,
        )
        if committed.returncode == 0:
            paths.update(path for path in (committed.stdout or "").split("\0") if path)
    except (OSError, subprocess.SubprocessError):
        return None
    return paths


def _safe_path(path: str) -> str:
    return json.dumps(path, ensure_ascii=True)


def _listed_paths(paths: set[str]) -> str:
    ordered = sorted(paths)
    shown: list[str] = []
    for path in ordered[:MAX_CHANGED_FILES_PER_CARD]:
        rendered = _safe_path(path)
        if shown and len(", ".join([*shown, rendered])) > MAX_PATH_LIST_CHARS:
            break
        shown.append(rendered)
    omitted = len(ordered) - len(shown)
    text = ", ".join(shown)
    return f"{text}, and {omitted} more" if omitted else text


def _qualified_issue_ref(raw: str, *, owner: str | None, fallback_repo: str) -> str:
    if raw.startswith("#"):
        return f"{fallback_repo}{raw}"
    repo, marker, number = raw.rpartition("#")
    if not marker:
        return ""
    if "/" not in repo and owner:
        repo = f"{owner}/{repo}"
    return f"{repo}#{number}"


def _issue_requester(ref: str, *, runner=_run) -> str | None:
    repo, marker, number = ref.rpartition("#")
    if not marker or "/" not in repo:
        return None
    try:
        # As the repository owner's non-human authority, never the host login (#2289).
        environment = github_identity.environment_for(repo)
    except github_identity.Refusal:
        return None
    try:
        done = runner(
            ["gh", "issue", "view", number, "--repo", repo, "--json", "author", "--jq",
             ".author.login"],
            timeout=10.0,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    requester = (done.stdout or "").strip() if done.returncode == 0 else ""
    return requester or None


def recent_change_warning(
    directive: dict,
    workspace: Workspace | PreparedWorkspaces,
    *,
    base_branch: str = "master",
    runner=_run,
    validator=registered_worktree,
) -> str:
    """Name recent completed-issue history touching an already changed worker checkout.

    A fresh checkout has no intended diff, so exact reversal detection remains a worker task.
    Reused work gets bounded file-level candidates before delivery; unreadable Git or GitHub
    metadata is advisory and never blocks the worker.
    """
    prepared = _prepared(workspace, None)
    raw_ref = str(directive.get("ref") or "")
    fallback_repo = raw_ref.rpartition("#")[0]
    own_ref = _qualified_issue_ref(raw_ref, owner=prepared.owner, fallback_repo=fallback_repo)
    current_requesters = {
        str(message.get("author") or "").strip()
        for message in directive.get("messages") or []
        if str(message.get("author") or "").strip()
    }
    candidates: list[tuple[str, str, str, str | None, set[str]]] = []
    seen: set[tuple[str, str]] = set()
    current_requester_checked = bool(current_requesters)
    for item in prepared.workspaces[:MAX_WORKTREES_PER_CARD]:
        checkout, present = validator(str(item.path))
        if checkout is None or not present:
            continue
        item_base = item.base_branch or base_branch
        paths = changed_files(item.path, base_branch=item_base, runner=runner)
        if not paths:
            continue
        bounded_paths = sorted(paths)[:MAX_RECENT_HISTORY_PATHS]
        try:
            done = runner([
                "git", "-C", str(item.path), "log", "--since=30.days",
                "--format=%H%x00%s%x00%B%x00%x1e", f"origin/{item_base}", "--",
                *bounded_paths,
            ], timeout=10.0)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode != 0:
            continue
        for record in (done.stdout or "").split("\x1e"):
            fields = record.strip("\n\x00").split("\x00", 2)
            if len(fields) != 3:
                continue
            sha, subject, body = fields
            match = ISSUE_REF_RE.search(body)
            if not match:
                continue
            issue_ref = _qualified_issue_ref(
                match.group("ref"), owner=prepared.owner, fallback_repo=fallback_repo
            )
            if not issue_ref or issue_ref == own_ref or (sha, issue_ref) in seen:
                continue
            seen.add((sha, issue_ref))
            requester = _issue_requester(issue_ref, runner=runner)
            if not current_requester_checked and own_ref:
                current_requester_checked = True
                current_requester = _issue_requester(own_ref, runner=runner)
                if current_requester:
                    current_requesters.add(current_requester)
            if requester and requester in current_requesters:
                continue
            candidates.append((sha, subject, issue_ref, requester, set(bounded_paths)))
            if len(candidates) >= MAX_RECENT_HISTORY_COMMITS:
                break
        if len(candidates) >= MAX_RECENT_HISTORY_COMMITS:
            break
    if not candidates:
        return ""

    lines = [
        "## Recent issue-linked changes in this branch's paths",
        "",
        "This reused checkout already changes paths touched by recent completed-issue commits. "
        "These are file-level candidates, not proof of a reversal; inspect the exact hunks before "
        "continuing:",
        "",
    ]
    for sha, subject, issue_ref, requester, paths in candidates:
        requester_text = _safe_path(requester) if requester else "requester unavailable"
        lines.append(
            f"- `{sha[:12]}` {_safe_path(subject)} — `{issue_ref}`, {requester_text}; "
            f"changed paths searched: {_listed_paths(paths)}"
        )
    lines.extend([
        "",
        "If an exact changed hunk reverses that issue's deliberate behavior for a different "
        "requester, report it through worker control and hand the decision back to a human.",
    ])
    return "\n".join(lines)


def default_branch(checkout: Path, *, fallback: str = "master", runner=_run) -> str:
    """The checkout's own default branch, from `origin/HEAD`, or ``fallback``.

    Asked rather than assumed (#1166). Every project this host ran was on `master`, so the
    constant was invisibly right until a project on `main` arrived: the worktree was created
    from `origin/master`, which does not exist there, `prepare` returned None, and the card
    reached Blocked reporting only that the adapter could not prepare a delivery — a state
    indistinguishable from being blocked on real work.

    `origin/HEAD` is a local ref written at clone time, so this costs no network and cannot
    hang a wake. A checkout can have none: a remote added by hand, or a clone of a repository
    that was still empty, which records no `origin/HEAD` even after its first push (#2778).
    Such a checkout keeps the fallback when `origin/<fallback>` exists, and otherwise takes
    `origin/main` when that exists — a new repository's first branch is `main`, and assuming
    `master` there left a new project unable to prepare any card.
    """
    done = runner(
        ["git", "-C", str(checkout), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
    )
    name = (done.stdout or "").strip() if done.returncode == 0 else ""
    prefix = "origin/"
    if name.startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix):]
    for candidate in (fallback, "main"):
        found = runner(
            ["git", "-C", str(checkout), "rev-parse", "--verify", "--quiet",
             f"refs/remotes/origin/{candidate}"]
        )
        if found.returncode == 0:
            return candidate
    return fallback


@dataclass(frozen=True)
class _TreeReading:
    """One read of a working tree: which branch it is on and how it differs from the base."""

    branch: str
    dirty: int
    behind: int
    ahead: int


def _read_tree(checkout: Path, base: str, runner) -> _TreeReading | None:
    """Everything the report needs, or None if any part of the checkout cannot be read.

    None rather than a partial answer on purpose: a half-read tree that defaults the missing
    halves to zero is indistinguishable from a checkout sitting exactly on its base.
    """
    head = runner(["git", "-C", str(checkout), "rev-parse", "--abbrev-ref", "HEAD"], timeout=30.0)
    if head.returncode != 0:
        return None
    # `status --porcelain` prints one line per file, so the count is the line count. Untracked
    # files are left out deliberately: a stray scratch file changes no read.
    status = runner(
        ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"], timeout=30.0
    )
    if status.returncode != 0:
        return None
    # Both directions in one command, because one of them alone is a lie: a tree sitting on
    # unpushed local commits is zero behind and still does not hold what the base branch holds.
    counts = runner(
        ["git", "-C", str(checkout), "rev-list", "--left-right", "--count",
         f"origin/{base}...HEAD"],
        timeout=30.0,
    )
    if counts.returncode != 0:
        return None
    parts = (counts.stdout or "").split()
    if len(parts) != 2:
        return None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return _TreeReading(
        branch=(head.stdout or "").strip(),
        dirty=len([line for line in (status.stdout or "").splitlines() if line.strip()]),
        behind=behind,
        ahead=ahead,
    )


def refresh_checkout(
    checkout: Path, *, base: str = "", fallback: str = "master", runner=_run
) -> CheckoutFreshness:
    """Refresh a checkout's view of its base branch and report what its files actually hold.

    Two things go wrong without this (#1853). A reused worktree never refetched its base, so
    `origin/<base>` — which the changed-paths warning and every `git diff` against the base
    read — could be weeks old. And a worker reading a file outside its worktree reads whatever
    branch a person left in that checkout: on this host that was an old feature branch with
    fifteen uncommitted files, and a worker reported a function as missing that had existed on
    master for a fortnight.

    Only the first of those is this function's to fix by acting. Fetching touches nothing but
    remote-tracking refs. **Updating the working tree is deliberately not attempted at all**, and
    that is the considered answer to a real defect rather than a missing feature: a checkout is
    shared with whoever else is using the machine, `git merge --ff-only <commit>` pins what is
    merged but never which branch receives it, and no amount of re-reading closes that window —
    a person switching branches between the last check and the write gets *their* branch
    fast-forwarded. Since exclusive ownership of a shared checkout cannot be established, the
    honest move is to leave it alone and say precisely how it differs from its base, which is
    what a worker needed in the first place. Nothing here writes to a working tree, ever: no
    merge, no checkout, no reset, no stash.
    """
    # Asked rather than assumed, for the same reason the worktree's base is (#1166): a project's
    # repositories need not all agree on one default branch.
    resolved = base or default_branch(checkout, fallback=fallback, runner=runner)
    fetched = runner(
        ["git", "-C", str(checkout), "fetch", "--quiet", "origin", resolved], timeout=120.0,
        # GitHub is reached only through the owner rule, never the host login (#2289).
        env=github_identity.github_git_environment(),
    )
    # A failed fetch is not an unreadable checkout. One project's remote is unreachable over SSH
    # from here, and throwing away the facts git *can* still answer would replace them with
    # "could not be read".
    reached = fetched.returncode == 0
    # Read after the fetch, so the counts are measured against the ref as it now stands. A read
    # that fails leaves the state unknown, and unknown is reported as unreadable rather than
    # filled in from anything older: a guess that reads as current is the one outcome that puts
    # a worker back where this issue started.
    reading = _read_tree(checkout, resolved, runner)
    if reading is None:
        return CheckoutFreshness(path=checkout, base=resolved, readable=False, fetched=reached)
    return CheckoutFreshness(
        path=checkout,
        base=resolved,
        branch=reading.branch,
        behind=reading.behind,
        ahead=reading.ahead,
        dirty=reading.dirty,
        fetched=reached,
    )


@dataclass
class CheckoutRefresh:
    """One refresh per checkout per wake, shared by every directive that touches it.

    A wake can carry several cards into the same repository, and refreshing once per card
    would fetch the same remote three times to learn the same answer.
    """

    base_branch: str = "master"
    runner: Callable[..., Any] = _run
    refresh: Callable[..., CheckoutFreshness] = refresh_checkout
    _seen: dict[Path, CheckoutFreshness] = field(default_factory=dict, repr=False)

    def __call__(self, checkout: Path, *, base: str = "") -> CheckoutFreshness:
        """``base`` empty means the checkout is asked what its own default branch is."""
        key = Path(checkout)
        if key not in self._seen:
            try:
                self._seen[key] = self.refresh(
                    key, base=base, fallback=self.base_branch, runner=self.runner
                )
            except (OSError, subprocess.SubprocessError):
                self._seen[key] = CheckoutFreshness(
                    path=key, base=base or self.base_branch, readable=False
                )
        return self._seen[key]


def stale_checkout_brief(
    states: Iterable[CheckoutFreshness], *, workspaces: Iterable[Workspace] = ()
) -> str:
    """Name every checkout on this host whose files no longer match its base branch.

    What this may *not* do is certify the worker's own worktree in passing. Refreshing a
    tracking ref does not move a branch: a worktree reused from an earlier turn stays at the
    commit that turn left it on, however fresh `origin/<base>` now is, and a failed fetch means
    even the ref is a guess. So the section says what was actually established and hands the
    worker the one command that settles the rest for its own branch.
    """
    stale = [state for state in states if not state.current]
    reused = [item for item in workspaces if not item.created]
    unfetched = sorted({state.base for state in stale if not state.fetched})
    if not stale and not reused:
        return ""
    listed = "\n".join(f"- `{state.path}` — {state.describe()}" for state in stale)
    own = ""
    if reused:
        paths = ", ".join(f"`{item.path}`" for item in reused)
        base = reused[0].base_branch
        own = (
            f"Your own workspace is not automatically current: {paths} was kept from earlier "
            "work on this card rather than made this wake, so it sits wherever that work left "
            "it, however fresh the base ref now is. Nothing here moved it — your commits are "
            "yours. Check it yourself with `git -C <worktree> rev-list --left-right --count "
            f"origin/{base}...HEAD`, and bring it onto the newer base deliberately if you need "
            "to."
        )
    if unfetched:
        refs = ", ".join(f"`origin/{name}`" for name in unfetched)
        own += (
            f" This wake also could not fetch {refs}, so even the base ref you are comparing "
            "against may be old."
        )
    paragraphs = ["## Code may be older than it looks"]
    if stale:
        paragraphs.append(
            "These checkouts on this host do not hold their base branch, and a file you read "
            "there by absolute path will tell you things that stopped being true of it:"
        )
        paragraphs.append(listed)
        paragraphs.append(
            "Nothing updated them, and nothing will: a checkout is shared with whoever else is "
            "using this machine, so the poller never moves one. So read code in your own "
            "worktree. When you need to look at another repository, make a worktree of it from "
            "its freshly fetched default branch and read that — and register the worktree "
            "first, exactly as above. Never state what code does from a read in one of the "
            "directories above."
        )
    if own:
        paragraphs.append(own.strip())
    return "\n\n".join(paragraphs) + "\n\n"


def owner_of(checkout: Path, *, runner=_run) -> str | None:
    """The GitHub owner of a checkout, from its origin remote, or None.

    The engine names a card ``repo#number``, which is enough for the board but leaves the
    reference ambiguous everywhere else: a worker's `gh` calls, and the deck's attempt to
    resolve the issue's real title for the session card (#310). The host knows the answer —
    it is in the checkout the worker is about to work in — so it says it.
    """
    done = runner(["git", "-C", str(checkout), "remote", "get-url", "origin"])
    if done.returncode != 0:
        return None
    slug = github_repository(done.stdout or "")
    return slug.split("/")[0] if slug else None


@dataclass
class SingleCheckoutWorkspaceAdapter:
    """The original one-checkout/one-worktree policy, kept as the default adapter.

    ``borrowed`` maps an issue-only repository to the code repository whose checkout its cards
    are worked in — the Manifest's ``workspace`` declaration, resolved (#1087). Such a card's
    worktree and default branch carry its repository id, so issue N in the tracker and issue N
    in the code repository can never be handed the same directory or branch.
    """

    checkouts: dict[str, Path]
    # The fallback when a checkout cannot say what its default branch is; the checkout itself
    # is asked first (#1166). Still settable, so a caller with a reason can pin one.
    base_branch: str = "master"
    borrowed: Mapping[str, str] = field(default_factory=dict)
    read_default_branch: Callable[..., str] = default_branch
    # Fetch the base and, when the checkout is clean and on it, fast-forward it (#1853). Left
    # unset the wake does neither, which is what every adapter did before.
    refresh_checkout_state: Callable[..., CheckoutFreshness] | None = None
    # One answer per checkout per wake. It is a local ref read, but a wake can carry several
    # directives into the same repository and the answer cannot change under them.
    _bases: dict[Path, str] = field(default_factory=dict, repr=False)
    # The runtime's Work workspace service (#2149), the one owner of every card worktree.
    # Unset, no delivery can be prepared.
    work_workspaces: WorkWorkspaces | None = None
    # Why the last `prepare` returned None, for the wake's skip line (#2778).
    refusal: str = ""

    def prepare(self, directive: dict) -> PreparedWorkspaces | None:
        self.refusal = ""
        repo = str(directive.get("repo") or "")
        checkout = self.checkouts.get(repo)
        if checkout is None:
            return None
        number = int(directive.get("number") or 0)
        owns_checkout = repo not in self.borrowed
        suffix = "" if owns_checkout else f"-{repo}"
        branch = str(directive.get("branch") or f"agent/issue-{number}{suffix}")
        if checkout not in self._bases:
            self._bases[checkout] = self.read_default_branch(
                checkout, fallback=self.base_branch
            )
        # Before the worktree, not after: a created one is branched off `origin/<base>`, and a
        # reused one never refetched it at all, so this is the only moment the base ref both
        # paths read is made current (#1853).
        state = (
            self.refresh_checkout_state(checkout, base=self._bases[checkout])
            if self.refresh_checkout_state
            else None
        )
        if self.work_workspaces is None:
            self.refusal = "no Work workspace service on this host"
            return None
        answer = self.work_workspaces.ensure(
            directive,
            purpose=IMPLEMENTATION,
            checkout=checkout,
            name=f"issue-{number}{suffix}",
            branch=branch,
            base=self._bases[checkout],
        )
        if not answer.usable:
            # Said, not swallowed: the wake's skip line is the only place a person sees why a
            # card was not handed out, and "could not prepare" alone sent one looking at
            # accounts while the base branch was wrong (#2778).
            self.refusal = (
                f"base `origin/{self._bases[checkout]}`: {answer.detail or 'no detail given'}"
            )
            return None
        workspace = _claimed(answer)
        if state is not None and workspace.checkout_state is None:
            workspace.checkout_state = state
        return PreparedWorkspaces(
            cwd=workspace.path,
            workspaces=(workspace,),
            owner=owner_of(checkout),
            borrowed_from="" if owns_checkout else self.borrowed[repo],
        )


def qualified_ref(ref: str, owner: str | None) -> str:
    """``owner/repo#n`` when the owner is known, else the engine's own ``repo#n``."""
    if not owner or "/" in ref or "#" not in ref:
        return ref
    return f"{owner}/{ref}"


def _non_closing_reference_brief(ref: str, *, qualified: bool) -> str:
    """The same lifecycle tie for a repository whose own guide bans closing keywords (#1734).

    Nothing is given up. The release path finds a pull request from a fully qualified reference
    in its description with no keyword involved, and the engine closes the finished card itself
    rather than letting GitHub do it, so the card's lifecycle completes either way. What changes
    is only which sentence AgentDeck writes — and this one cannot contradict the repository.
    """
    reference = ref if qualified else "<owner>/<repo>#<issue>"
    return (
        "## Source issue reference\n\n"
        "This repository's own guide prohibits GitHub closing keywords in a pull request "
        "description, so do not write `Closes`, `Fixes`, `Resolves` or any of their variants "
        "for this card, and do not add one to a description that has none. Follow the "
        "repository's own convention for referencing an issue; where it does not specify one, "
        "an ordinary issue link is enough.\n\n"
        "Treat a normal implementation pull request as completing this card unless the card or "
        "the requester explicitly says the pull request is partial or non-closing. A completion "
        f"pull request must reference this card in full — `{reference}`, or the issue's URL — "
        "somewhere in its description. That fully qualified form is what ties the pull request "
        "to this card for review and release; a bare `#number` on its own does not, and neither "
        "does the title or prose such as `Follow-up on #123`. The card is closed by the poller "
        "when the release finishes, not by the pull request.\n\n"
        "Before creating or rewriting a pull request description, read its current body. "
        "Preserve an existing reference to this card verbatim; if this is a completion pull "
        f"request and `{reference}` is absent, add it. "
        "Review, fix, base-update, and follow-up work "
        "must not weaken or remove it.\n\n"
        "A partial implementation says so in prose — an explicit requester instruction such as "
        "`PR intent: non-closing — <reason>` is recorded in the description with the reason "
        "kept. Keep the qualified reference in that case too, so the card can still see the "
        "work. Do not infer non-closing intent merely from words such as `follow-up` or "
        "`partial` in a branch, commit, or pull request title.\n\n"
        + step_pull_request_brief(reference)
    )


def step_pull_request_brief(reference: str) -> str:
    """How a worker merges one part of a card and keeps working on the rest (#2730).

    The marker is what the release path reads; the `PR intent` line is the same decision said
    where the reviewer reads it. Without a closing keyword GitHub leaves the issue open, and the
    poller hands the card back once the step's release lands instead of finishing it.
    """
    return (
        "### Step pull requests\n\n"
        "Some cards cannot ship as one pull request: the next part needs the first one merged, "
        "or live, before it can be built or checked. Split the work only then, or when the "
        "issue, a person or an approved spec says to — never just to make a pull request "
        "smaller. A step pull request carries two lines in its description: "
        "`PR intent: step — <why the rest needs this merged first>` and the marker "
        f"`{RELEASE_STEP_MARKER}`. It refers to this card as `Part of {reference}` and carries "
        "no closing keyword for it. Finish `in_review` as usual: the step is reviewed, "
        "verified and released like any other pull request, and once it has merged (and its "
        "deploy is green, where this project watches one) the poller hands the card back to "
        "you on a fresh branch for the next part. The last pull request of the card is a "
        "normal completion pull request.\n\n"
    )


def source_issue_closure_brief(
    ref: str, *, closing_keywords: str = CLOSING_KEYWORDS_REQUIRED
) -> str:
    """Keep implementation PRs tied to the lifecycle they are meant to finish (#1261).

    ``closing_keywords`` is the policy declared by the repository the pull request lands in,
    resolved from the Manifest before dispatch (#1734). Where a repository's own guide bans
    closing keywords, demanding one here would hand the worker two trusted instructions it
    cannot both obey — and the demand buys AgentDeck nothing, because the release path links
    a pull request to its card from a fully qualified reference with no keyword at all.
    """
    issue_repo, separator, number = ref.rpartition("#")
    if closing_keywords == CLOSING_KEYWORDS_PROHIBITED:
        return _non_closing_reference_brief(ref, qualified=bool(separator))
    same_repo_closer = f"Closes #{number}" if separator else "Closes #<issue>"
    cross_repo_closer = (
        f"Closes {ref}" if separator and "/" in issue_repo
        else f"Closes <owner>/{ref}" if separator
        else "Closes <owner>/<repo>#<issue>"
    )
    return (
        "## Source issue closure\n\n"
        "Treat a normal implementation pull request as completing this card unless the card or "
        "the requester explicitly says the pull request is partial or non-closing. A completion "
        "pull request must contain a GitHub-supported closing keyword in its description: use "
        f"`{same_repo_closer}` when the pull request is in `{issue_repo}`, or "
        f"`{cross_repo_closer}` when it is in another repository. Its title, `(#123)`, a bare "
        "issue reference, or prose such as `Follow-up on #123` does not close the issue.\n\n"
        "Before creating or rewriting a pull request description, read its current body. Except "
        "for the explicit non-closing case below, preserve an existing closing reference to this "
        "card verbatim; if this is a completion pull request and that reference is absent, add "
        "it. Review, fix, base-update, and follow-up work must not weaken or remove it.\n\n"
        "A partial implementation can opt out only through an explicit requester instruction "
        "such as `PR intent: non-closing — <reason>`. In that case, do not add a closing keyword "
        "and remove any existing closing keyword for this source issue; keep the non-closing "
        "reason and an ordinary issue link in the pull request description. "
        "Do not infer non-closing intent merely from words such as `follow-up` or `partial` in a "
        "branch, commit, or pull request title.\n\n"
        + step_pull_request_brief(cross_repo_closer.removeprefix("Closes "))
    )


def _issue_view_command(ref: str, number: object) -> str:
    """A ``gh`` read pinned to the card's repository when the ref is qualified."""
    repo, separator, ref_number = ref.rpartition("#")
    raw_number = number or (ref_number if separator else ref)
    repo_option = f" --repo {shlex.quote(repo)}" if separator and "/" in repo else ""
    return (
        f"gh issue view {shlex.quote(str(raw_number))}{repo_option} "
        "--json title,body,comments,author"
    )


def _prepared(workspace: Workspace | PreparedWorkspaces, owner: str | None) -> PreparedWorkspaces:
    if isinstance(workspace, PreparedWorkspaces):
        return workspace
    return PreparedWorkspaces(cwd=workspace.path, workspaces=(workspace,), owner=owner)


def _release_set_brief(refs: list[str]) -> str:
    """Name every pull request a reviewer's ``pass`` would release (#1338).

    A card's release target is the whole linked-PR set, which the poller merges together. Told
    only the issue, a reviewer reviews the one pull request on its own branch and never learns
    the others exist: one tracker issue had three, in three repositories, and the
    review covered one. The set can also span repositories the worktree does not hold, so the
    brief has to say that reading them is part of the turn rather than out of reach.
    """
    if len(refs) < 2:
        return ""
    listed = "\n".join(f"  - {ref}" for ref in refs)
    return (
        f"**This card has {len(refs)} open pull requests, and your verdict covers all of "
        f"them.** The poller releases them together, so a `pass` ships every one:\n\n"
        f"{listed}\n\n"
        f"Review each of them, giving the skill that pull request's number so it reviews that "
        f"pull request rather than whatever your worktree happens to be on. One of them may live "
        f"in a repository your worktree does not contain; check it out or read it with `gh` "
        f"rather than skipping it. If you cannot review one of them, that is `changes-needed` — "
        f"say which one and why, and nothing is merged.\n"
    )


def merge_review_brief(ref: str, *, pull_requests: list[str] | None = None) -> str:
    """What a reviewer-only worker is asked to do (#963, #1338).

    An explicit ``/merge review`` has already consumed its command into the engine's release
    record and has no ordinary work of its own, so the action itself selects this brief.

    Every reviewer must be told to push fixes to the pull request that already exists rather
    than opening another, to run the review once rather than re-reviewing its own fixes
    (#1002), and to let the poller decide whether the reviewed release may merge.
    """
    opening = (
        "A person requested `/merge review`: review the existing pull request, then let "
        "the poller release it only if the reviewed content passes unchanged."
    )
    refs = list(pull_requests or [])
    release_set = _release_set_brief(refs)
    if release_set:
        release_set = f"\n\n{release_set}"
    target = (
        f"each of the {len(refs)} pull requests listed above"
        if release_set
        else (f"{refs[0]}" if refs else f"{ref}'s open pull request")
    )
    return (
        f"{opening}{release_set}\n\n"
        f"- Run the `review-fix-pr` skill **once** against {target}: review the "
        f"changes, then apply fixes for what the review found. The skill's own re-check of those "
        f"fixes is the last review this turn gets — do not start the review over on the fixed "
        f"commit, however sure you want to be.\n"
        f"- Push the fixes to the branch of the pull request they belong to. Do not open a second "
        f"pull request, and never push to the base branch.\n"
        f"- Do not merge anything yourself: the poller merges once you report `pass`, after its "
        f"own checks and deploy gate.\n\n"
        f"Before treating uncertainty as `changes-needed`, settle it with evidence available to "
        f"the worker. When a report says behavior used to work, or correctness depends on what "
        f"the production data actually looks like, use the project's available read-only "
        f"production query tools to inspect that data. Do not infer the runtime cause from the "
        f"shape of the code alone.\n\n"
        f"{USE_CASE_REVIEW_RULE}\n\n"
        f"Report `pass` only if you would be comfortable with the result reaching production "
        f"unread. Report `changes-needed` for anything you could not fix, or anything that needs "
        f"a human decision — the card then goes back to In review and nothing is merged. Wanting "
        f"another review pass to settle a doubt is itself `changes-needed`."
    )


def continuation_brief(ref: str, previous_session_url: str = "") -> str:
    """What a worker taking over from one that ended without reporting is told (#835).

    The engine restarts such a card instead of parking it, which means this worker is not
    starting the task — it is inheriting a turn that stopped somewhere unknown, with the same
    worktree, branch and possibly an open pull request still sitting there. Told nothing, its
    most likely first act on work that was actually finished is to do it a second time and
    open a second pull request for it.

    So the brief says the one thing the engine knows (the previous turn ended with no report)
    and asks for the one thing it cannot do itself (look). It deliberately does *not* say the
    work is probably done: silence is not success, and a worker that assumes otherwise reports
    a finish nobody performed.

    What it must not do is send the successor to ingest the whole predecessor transcript
    (#1485).  ``/transcript.json`` is unbounded by construction -- its own route exists to
    "expose the complete normalized transcript" and takes no limit -- so an instruction to read
    it costs the new turn the entire old one before it has done anything.  A 24-hour audit
    found at least fifteen sessions that began exactly that way, some inheriting predecessors
    that had contributed nothing at all, such as an immediate quota stop.  The bounded sources
    say more for a fraction of the context: the card's own issue thread is where every plan,
    decision and terminal report was published, and the branch, worktree and pull request are
    observable directly.  The transcript stays available as a *targeted* fallback, for a
    specific question the bounded sources leave open, which is the shape this card fixed for
    phase deliveries and which a reportless revive needs for the same reason.

    #1228's case is named explicitly rather than left to the general permission, because it is
    the one this brief already existed to serve: a choice the user made in the old conversation
    is part of the current ask, and a successor that re-interviews them for it has failed just
    as surely as one that ingested the whole transcript.  Bounding the default must not quietly
    reintroduce the question #1228 removed.
    """
    transcript = ""
    if session_url := public_session_url(previous_session_url):
        transcript_url = f"{session_url.rstrip('/')}/transcript.json"
        transcript = (
            "- The previous worker's conversation is at "
            f"`{transcript_url}`. Do not read it to find out what happened: it is the whole "
            "old turn, and this card's issue thread already carries every plan, decision and "
            "report that worker published, in a fraction of the context. Consult the "
            "transcript only for a specific question the issue thread and the repository "
            "leave unanswered, and read only the part that answers it. A choice the user "
            "made in that conversation is exactly such a question: it is part of this card's "
            "current ask, so look it up there rather than asking the user to decide it "
            "again.\n"
        )
    return (
        f"**You are continuing this card, not starting it.** A previous worker on {ref} ended "
        f"without posting its terminal report, so the poller has no statement of what the work "
        f"reached — only that the turn is over. Its worktree, branch and any pull request it "
        f"opened are still here and are yours.\n\n"
        f"{transcript}"
        f"- Look at what is already there first: this card's issue thread, including any plan "
        f"and progress comments the previous worker posted, then the branch's commits against "
        f"the base, uncommitted changes in the worktree, and whether a pull request for this "
        f"card exists. Those are bounded and they are what the previous turn actually "
        f"published.\n"
        f"- If a requested repository change is missing, finish it rather than redoing what is "
        f"done, and push to the existing pull request's branch instead of opening a second one. "
        f"If the existing pull request only archives investigation results, do not update it; "
        f"put the findings and the pull request link in the terminal issue comment instead.\n"
        f"- Do not treat the leftovers as proof the work succeeded, and do not treat them as "
        f"proof it failed. Validate the state you find, then take it the rest of the way.\n\n"
        f"Report through the interface below whatever you conclude — including `blocked` or "
        f"`needs_more_info` if what you find cannot be finished or judged. The report is the "
        f"only thing that ends this card."
    )


def merge_review_freshness_brief() -> str:
    """Keep an up-to-date check from turning a clean review into a false failure (#948)."""
    return (
        "## Branch freshness during review\n\n"
        "The poller refreshed known-behind pull-request branches before starting this review. "
        "A failing branch-up-to-date check is not `changes-needed` by itself. Do not merge or "
        "rebase the base branch merely to clear that check during the review. If a refresh "
        "conflicted, resolve that conflict as part of this worker turn. The release gate still "
        "updates a branch that falls behind later; when the effective release content did not "
        "change, that base sync does not invalidate a clean pass. The gate independently verifies "
        "the reviewed content."
    )


_VISUAL_EVIDENCE_ASK = (
    "## Visual evidence\n\n"
    "If your change has a user-visible surface, capture screenshots using the repository's "
    "screenshot procedure and include the relevant screenshots in the same terminal issue "
    "comment. Treat them as product evidence: exercise the actual interaction or data path "
    "and show the changed feature clearly in a realistic used state. Assert that the intended "
    "state is visible before capture. If the change has no visual surface, write `No visual "
    "change` in the terminal issue comment instead. Do not duplicate visual evidence in the "
    "pull request description.\n\n"
    # The images were folded once so the prose stayed reachable (#2116). The person reading these
    # threads mostly wants the images, and with the prose now short and folded (#2498) they are
    # shown open instead, at a fixed small width so they do not fill the page. Width, not height:
    # GitHub shrinks an image wider than the screen, and a fixed height would then squash it.
    "Show the screenshots open, right after the outcome, not inside a collapsed block, and keep "
    "each one small: write it as an HTML image with a fixed width, "
    "`<img src=\"<embed URL>\" width=\"400\" alt=\"<what it shows>\">`, all of them on one "
    "line so they sit side by side on a wide screen. Clicking one opens it full size. Show at "
    "most 8; put any more in one collapsed `<details>` block summarised "
    "`More screenshots (N)`.\n\n"
    # The second ask is not politeness: the poller hides its own superseded comments, so a
    # terminal comment that points back at an earlier one points at something collapsed.
    "Embed the images in the terminal comment every time, even when an earlier comment on "
    "this card already showed them. Never write that screenshots were posted previously: the "
    "last comment on a card has to stand on its own, because it is the one a person opens the "
    "card to read and the earlier ones are hidden as outdated once it lands. Re-embedding "
    "costs nothing — the images are already pushed, so it is the same link again.\n\n"
)

_VISUAL_EVIDENCE_EMBED = (
    "A screenshot the reviewer cannot see is not evidence. Embed each committed image "
    "through the repository host, pinned to a commit — "
    "`https://github.com/<owner>/<repo>/blob/<commit-sha>/<path>?raw=1`. A "
    "`raw.githubusercontent.com` link carries none of the reviewer's session and shows as a "
    "broken image in a private repository, and a branch ref dies when the branch is deleted "
    "after merge.\n\n"
)


# The order of a terminal issue comment (#2498). The person reading the card wants the outcome
# and the images; a 19,600-character report with both buried in it is what this replaces. The
# deck reads a terminal comment's first line as its headline, so the outcome leads. It is the
# whole card's outcome, never just this turn's: the newest report is the one comment left open
# on the issue, and one that said only "fixed the review finding" left a person no overview of
# the work (#2689).
COMMENT_LAYOUT_BRIEF = (
    "## Comment layout\n\n"
    "The person who reads your terminal issue comment mostly wants the outcome and the "
    "screenshots, so keep everything else out of their way. Write it in this order:\n\n"
    "1. The outcome of the whole card, in at most three plain sentences (about 60 words): "
    "what was wrong and what the pull request as a whole changes, the same gist you give as "
    "the first sentence of the Work resolution's `problem` and `solution`. Write it for "
    "someone who has read nothing else on the issue: the newest report is the one left open, "
    "so it must stand alone. A turn that only fixed review findings or merged the base in "
    "still opens with that whole-card outcome, and lists this turn's fixes under a "
    "`Review fixes` line inside Details. A `Blocked surface` line, when this run needs one, "
    "comes before it and is one sentence.\n"
    "2. The screenshots, open, as Visual evidence describes.\n"
    # The walkthrough's summary line is a result the reader weighs, like the screenshots (#2919).
    "3. The one `Use-case walkthrough:` line, open, when a Use-case walkthrough section asks "
    "for it; its numbered steps go in the Details block.\n"
    "4. Only what the reader has to act on: open questions, numbered decisions they can "
    "overrule, and manual steps such as a cutover or a feature flag to switch. Leave this out "
    "when there is nothing.\n"
    "5. Everything else inside one collapsed "
    "`<details><summary>Details — review surface, verification, notes</summary>` block: the "
    "review packet below, verification, review fixes, forgiven tests, caveats, and anything "
    "else a reviewer may want. Leave a blank line after the summary so the Markdown inside "
    "renders.\n\n"
    "Any other information goes inside that block too; when unsure, fold it.\n\n"
)


# Tests prove the code does what its author meant, not that a person gets what they came for
# (#2919). The owner's example: a "Create campaign" button saves the campaign and says so, but the
# list never shows it. Every test passes and the feature still fails, so the worker walks the use
# case to the person's goal, past the confirmation. A trace through the code is enough; running it
# is welcome, production never. Spec-only and review-only turns write no code and skip it.
USE_CASE_WALKTHROUGH_BRIEF = (
    "## Use-case walkthrough\n\n"
    "If your change adds or changes a use case — a page, control, command, API route, poller "
    "action, notification or integration that a person or another program uses — walk through "
    "it before you report the card ready. Walk through it as the person who uses it, not as "
    "the code. Start from what they want to get done: who does what, and where. Follow each "
    "step through the code you wrote, naming the file or function, and do not stop at the "
    "confirmation: carry on to what they do next with the result, and end only when their "
    "goal is met. Include what a test leaves out: what is already on screen, existing and "
    "older data, a second user or repository, a reload or a restart.\n\n"
    "Example: a \"Create campaign\" button that saves the campaign and says \"Campaign "
    "created\", but the campaign list does not show it, passes every test and still fails the "
    "person — that is a gap.\n\n"
    "For each step, say what the person expects to see or do next and whether the code gives "
    "them that. A trace through the code is enough; say on a step when you actually ran it, "
    "and never use production for this. Fix a gap before you report, or list it as a known "
    "gap. Tests, fixtures and mocks are not a substitute for the walkthrough.\n\n"
    "Put the numbered walkthrough, usually three to eight steps, in the collapsed Details "
    f"block, and one open line right after the screenshots: {USE_CASE_WALKTHROUGH_LINE}. If "
    "the change has no use case — a refactor, docs or tests only — write "
    f"{USE_CASE_WALKTHROUGH_NOT_APPLICABLE} there instead.\n\n"
)


# How a Run proves its work (#2154). A verification job is evidence the engine attaches to this
# Run on its own; there is nothing to checkpoint and nothing to wait for. Only an explicit one
# counts, so an exploratory or partial run can never be mistaken for the release suite.
_VERIFICATION_JOB = (
    "## Verification evidence\n\n"
    "When you run the suite that proves this work, submit it as this card's verification "
    "job:\n\n"
    "    ~/agentdeck/.venv/bin/agentdeck job submit --verification "
    "--session-token <this chat's AgentDeck session token> --cwd <worktree> -- <command>\n\n"
    "The result attaches to this card's Run by itself. It counts as the green run the release "
    "needs only when it succeeds on a clean, committed tree, and only for that exact commit: "
    "commit and push first. Other jobs are fine for exploring and never count.\n\n"
    "The selected suite is the last gate before release, not the ticket into review. Open or "
    "update the pull request and report `in_review` on focused tests. Run the final suite "
    "once, on the head a clean review passed, as this project's release policy requires. If a "
    "failure needs a code change, fix it with focused tests and send the changed head back to "
    "review before running the suite again.\n\n"
)

# Who starts that final suite, in the words the Work page's Verify step uses (#2360), so the
# worker can say whether it runs the suite now or a later turn does.
_ENGINE_VERIFIES = (
    "Do not submit the final suite before a review passes. Once a review passes the head you "
    "pushed, AgentDeck sends this chat one more turn asking for exactly that run: run it then. "
    "A repository whose CI is its verification needs no job, because AgentDeck reads the CI on "
    "that head itself. Until a green run covers the head, the Work page shows Verify as next "
    "and the card is not ready to merge.\n\n"
)
_WORKER_VERIFIES = (
    "Nothing in this project starts the final suite for you. If a review has already passed "
    "your pushed head while you hold this turn, submit the verification job now. Otherwise "
    "report `in_review`: the Work page then shows Verify as not run until a person asks for "
    "it, and the card is not ready to merge until a green run covers its head.\n\n"
)


def verification_evidence_brief(*, verify_turn: bool = False) -> str:
    """The verification section for a project that reviews before it verifies (#2253).

    ``verify_turn`` is the directive's word that this project's engine asks for the final
    suite itself once a review passes (#2360); without it nothing does, and the brief says so.
    """
    return (
        _VERIFICATION_JOB
        + (_ENGINE_VERIFIES if verify_turn else _WORKER_VERIFIES)
        + FAILING_TESTS_GUIDE
        + WORK_RESOLUTION_ASK
    )


VERIFICATION_EVIDENCE_BRIEF = verification_evidence_brief()


def ready_for_review_brief(offer_command: str) -> str:
    """The verification section for a project that reviews beside verification (#2350).

    The worker keeps its turn: it offers the head, verifies it while the outside reviewer reads
    it, and only then reports. A review that does not pass arrives in this same chat, and the
    poller cancels the verification of the rejected head itself, so the worker's part is to not
    start one again until it has pushed and offered a fix.
    """
    return (
        "## Verification evidence\n\n"
        "This project reviews a head while its worker verifies it. When the implementation is "
        "committed, pushed and its pull request is open (not a draft), offer that head for "
        "review from your worktree and keep your turn:\n\n"
        f"    {offer_command} --head $(git rev-parse HEAD)\n\n"
        "Right after a push GitHub can still show the pull request's previous head; the offer "
        "waits a few seconds for it. If it still answers `unavailable`, submit the "
        "verification job below and then run the offer once more, before your turn ends; if "
        "it still fails, say so in your report.\n\n"
        "Then submit the suite that proves this work as this card's verification job, on that "
        "same head:\n\n"
        "    ~/agentdeck/.venv/bin/agentdeck job submit --verification "
        "--session-token <this chat's AgentDeck session token> --cwd <worktree> -- <command>\n\n"
        "The result attaches to this card's Run by itself. It counts as the green run the "
        "release needs only when it succeeds on a clean, committed tree, and only for that "
        "exact commit. Other jobs are fine for exploring and never count. You may end the turn "
        "while it runs; its completion wakes this chat.\n\n"
        "If the review does not pass, its findings arrive in this chat, and the poller cancels "
        "the verification of the rejected head. Do not verify a rejected head: fix the "
        "findings with focused tests, push, offer the new head again, and only then submit the "
        "verification job for it. The offer refuses a head the review already rejected.\n\n"
        "When the suite is green, report `in_review`, whether or not the review has answered "
        "yet: the poller releases once both are green on the same head. If it fails and the "
        "code needs a change, fix it, push, offer the new head, and verify that.\n\n"
        + FAILING_TESTS_GUIDE
        + WORK_RESOLUTION_ASK
    )


# What every verification job has to run at the least (#2364): a job that runs no test proves
# nothing, whatever its exit code.
VERIFICATION_SCOPE_BRIEF = (
    "Whatever command you submit as the verification job must run at least one real test "
    "of the repository you changed. A job that runs no test, or only a linter or a build, "
    "proves nothing and is not a verification.\n\n"
)


def visual_evidence_brief(repo: str, workspace: Workspace | None, path: str) -> str:
    """What the worker is told about screenshots, given where this project keeps them (#1791).

    With no evidence repository configured this is the text every project has always had:
    commit the images beside the code and embed them from there. A project that named one gets
    the same ask with a different destination — the images never enter the repository the card
    ships from, and the embed points at the evidence repository's own commit. The embed form is
    the part that must survive either way: on a private repository it is the only link a
    reviewer's browser is actually served.

    ``workspace`` is this card's *own* worktree of the evidence repository, and the brief may
    only be written when there is one. Naming the shared clone instead would put every
    concurrent card in one working tree and one index: one worker's `git add` would capture
    another's screenshots, and a `pull --rebase` would refuse outright on the other's
    uncommitted files. Per-card directories stop paths from colliding; they do nothing about
    working-tree state.
    """
    if not repo or workspace is None:
        return _VISUAL_EVIDENCE_ASK + _VISUAL_EVIDENCE_EMBED
    # Every path in a rendered command is shell-quoted, like the rest of the runner's command
    # lines: an Overlay checkout is a host path an operator wrote, and one containing a space
    # would otherwise be handed to `git` as two arguments by a worker who pasted it.
    tree = shlex.quote(str(workspace.path))
    inside = shlex.quote(path)
    base = workspace.base_branch
    return (
        _VISUAL_EVIDENCE_ASK
        + f"Screenshots do not belong in this project's code. Commit them to `{repo}` instead, "
        f"in the worktree this wake prepared for this card:\n\n    {workspace.path}\n\n"
        f"That worktree is yours alone — no other card writes in it — and it is on branch "
        f"`{workspace.branch}`. Put this card's images under `{path}/` inside it, then commit "
        f"and push them to the evidence repository's `{base}` branch:\n\n"
        f"    git -C {tree} add {inside}\n"
        f"    git -C {tree} commit -m \"evidence: <card>\"\n"
        f"    git -C {tree} push origin {shlex.quote(f'HEAD:{base}')}\n\n"
        "If that push is rejected because another card pushed first, rebase and push again:\n\n"
        f"    git -C {tree} pull --rebase origin {shlex.quote(base)}\n\n"
        "Your worktree holds only your own commits and only your own uncommitted files, so "
        "that rebase is never a conflict to resolve. Never commit screenshots in your code "
        "worktree, and never write into another card's evidence worktree.\n\n"
        "A screenshot the reviewer cannot see is not evidence. Embed each pushed image through "
        "the repository host, pinned to the commit you just pushed — "
        f"`https://github.com/{repo}/blob/<commit-sha>/<path>?raw=1`. A "
        "`raw.githubusercontent.com` link carries none of the reviewer's session and shows as "
        "a broken image in a private repository, whichever ref it names.\n\n"
    )


def _evidence_workspace(
    directive: dict,
    checkout: Path,
    artifact_repo: str,
    repo: str,
    work_workspaces: WorkWorkspaces | None,
) -> Workspace | None:
    """The card's evidence worktree: the Work's artifact claim (#2149), or None.

    The evidence repository is written by every card at once, which is exactly what a shared
    checkout cannot survive, so each Work gets its own worktree, index and branch of it (#1791).
    The branch is per card so two worktrees never claim one ref, and the push still lands on
    the default branch, so a merged card's embed keeps resolving after the branch is gone.

    Returning None is not a failure to report: the caller falls back to the brief every project
    had before, which commits the images beside the code.
    """
    if work_workspaces is None:
        return None
    number = int(directive.get("number") or 0)
    answer = work_workspaces.ensure(
        directive,
        purpose=ARTIFACT,
        checkout=checkout,
        name=f"issue-{number}-{repo}-evidence",
        branch=f"evidence/{repo}-{number}",
        base=default_branch(checkout),
        repository=artifact_repo,
    )
    return _claimed(answer) if answer.usable else None


def compose_message(
    directive: dict,
    workspace: Workspace | PreparedWorkspaces,
    *,
    owner: str | None = None,
    control_command: str = "poller-engine worker-control",
    card_control: str = "",
    register_command: str = "",
    account: str = "",
    change_warning: str = "",
    code_checkouts: Mapping[str, str] | None = None,
    # What this wake found in the project's other checkouts, which a worker can read from but
    # no card owns (#1853).
    sibling_states: Iterable[CheckoutFreshness] | None = None,
    artifact_repo: str = "",
    artifact_workspace: Workspace | None = None,
    # Each code repository's declared pull request closing-keyword policy, resolved from the
    # Manifest before this brief is written (#1734). Absent means the default, which is the
    # instruction every project got before the policy existed.
    closing_keywords: Mapping[str, str] | None = None,
) -> str:
    """What the worker is actually told: its workspace, its card, and the instructions.

    The engine's ``messages`` are the human instructions in the order they were given. They are
    passed through verbatim — the poller decides *whether* work happens, never what the work is.
    """
    instructions = []
    for message in directive.get("messages") or []:
        payload = str(message.get("payload") or "").strip()
        if not payload:
            continue
        author = str(message.get("author") or "").strip()
        requester = f"\n  Requester: {_safe_path(author)}" if author else ""
        instructions.append(f"{payload}{requester}")
    prepared = _prepared(workspace, owner)
    ref = qualified_ref(str(directive.get("ref") or ""), prepared.owner or owner)
    # A reviewer-only dispatch has no work of its own.
    spec_only = bool(directive.get("spec_only"))
    review_only = str(directive.get("action") or "") == "merge-review"
    # A card started by its ownership label alone carries no instruction, and that is a real
    # start rather than a mistake (#311) — it is how the old poller was driven. The issue itself
    # is the brief in that case, so say so instead of handing the worker "(none)" and hoping.
    # Reading it beats inlining it: comments and images are part of the ask and a body is not.
    # `--json` rather than the human rendering: `gh issue view <n> --comments` has returned
    # exit 0 with zero bytes on this host, twice in a row, for an issue that plainly exists —
    # and a worker reading that as "the issue is empty" works the wrong card (#787). JSON output
    # is never legitimately empty, so emptiness becomes a detectable failure. The brief still has
    # to say so, because the exit code alone gives the worker no signal.
    body = "\n\n".join(f"- {text}" for text in instructions) or (
        f"Nothing beyond the issue itself — this card was started by its label rather than by an "
        f"instruction. Read {ref} in full, including its comments, and do what it asks:\n\n"
        f"    {_issue_view_command(ref, directive.get('number'))}\n\n"
        f"Empty output from that command is a failed read, not an empty issue — retry it once. "
        f"A repository that reads as missing (404) is a missing grant for this host's "
        f"non-human GitHub identity: report it as a blocker, and never retry as another "
        f"identity."
    )
    if review_only:
        # Owner-qualified like the card ref, because a release set can span repositories and a
        # bare `repo#n` is not something the worker can hand to `gh` (#1338).
        release_refs = [
            qualified_ref(str(pr), prepared.owner or owner)
            for pr in (directive.get("pull_requests") or [])
            if str(pr).strip()
        ]
        body = merge_review_brief(ref, pull_requests=release_refs)
    if str(directive.get("action") or "") == "merge-review":
        body = f"{body}\n\n{merge_review_freshness_brief()}"
    if spec_only:
        body = (
            f"{body}\n\n## Specification-only card\n\n"
            "This card carries the poller's `spec-only` label. Write a concrete implementation "
            "specification as an issue comment, but do not edit any repository file, create or "
            "switch a branch, commit, push, or open a pull request. "
            "After the specification is posted, hand the card to a person with `finish "
            "needs_more_info`. If a person has explicitly asked, on the issue or in this chat, "
            "for the card to be closed instead (already done, a duplicate, no longer wanted), "
            "end with `finish resolved`, which closes the issue; never choose `resolved` on "
            f"your own judgement. "
            f"{spec_approval_rule(str(directive.get('build_brief') or ''))}\n\n"
            f"{SPEC_ONLY_QUESTION_RULE}"
        )
    elif directive.get("spec_approved"):
        body = f"{body}\n\n{SPEC_APPROVED_ASK}"
    if directive.get("continuation"):
        previous_session_url = str(directive.get("previous_session_url") or "")
        body = f"{body}\n\n{continuation_brief(ref, previous_session_url)}"
    issue_requester_command = _issue_view_command(ref, directive.get("number"))
    generation = directive.get("generation") or ""

    # One command per control operation, not the whole engine invocation restated on every line
    # (#1732). When the host wrote this dispatch's wrapper, the card and the generation are
    # already inside it; without one — a caller with no host state directory — the long form is
    # still exactly what it always was.
    def control(verb: str) -> str:
        if card_control:
            return f"{card_control} {verb}"
        return (
            f"{control_command} {verb} {directive.get('repo')} "
            f"{directive.get('number')} {generation}"
        )

    # What the wrapper is gets explained where the worker will actually read it, in
    # `worker.py::_compact_poller_brief` — that rebuilds this section from the command lines
    # alone, so a paragraph composed here would be dropped before delivery (#1772).

    if spec_only:
        completion_text = (
            "After posting the specification as an ordinary issue comment with `gh`, write "
            "the short handoff summary to a file and run:\n\n"
            f"    {control('finish')} needs_more_info --body-file <path>\n\n"
            "Only when a person explicitly asked for the card to be closed, run this instead; "
            "it closes the issue:\n\n"
            f"    {control('finish')} resolved --body-file <path>\n\n"
            "Do not report `in_review`; worker-control rejects it while the `spec-only` label "
            "remains on the card.\n\n"
        )
        final_report_text = (
            "The terminal handoff is separate from the specification comment and must be "
            "posted last.\n"
        )
    else:
        completion_text = (
            "The poller cannot see that you finished without a terminal operation. After all "
            "other requested work, write the short human summary to a file and run exactly "
            "one of:\n\n"
            f"    {control('finish')} in_review --body-file <path>\n"
            f"    {control('finish')} needs_more_info --body-file <path>\n"
            f"    {control('finish')} blocked --body-file <path>\n"
            f"    {control('finish')} resolved --body-file <path>\n\n"
            "Use `needs_more_info` only when a human decision is required, and `blocked` "
            "only when something outside your workspaces prevents completion. Use `resolved` "
            "only when the work was finished entirely in issues or comments and there is "
            "nothing to merge — an answered question, a decision recorded on an issue. It "
            "moves the card straight to Done. If you wrote any code at all, the ending is "
            "`in_review`.\n\n"
            "There is a fifth ending, `human_review_pending`, and it is not yours to choose. "
            "Report it only when the card or whoever asked for the work says in so many words "
            "that it has to wait for a person to look at it. Never pick it because you would "
            "like a second opinion, because you are unsure, or because the work feels risky — "
            "those are `needs_more_info`. Where this project has no such status it parks in "
            "Needs More Info anyway.\n\n"
            "When what blocks you is another issue or pull request landing, name it and the "
            "poller picks the card back up on its own once it does — no `/claude` comment, "
            "no re-labelling:\n\n"
            f"    {control('finish')} blocked --body-file <path> "
            "--blocked-by owner/repo#122\n\n"
            "`--blocked-by` is repeatable and takes `#12`, `repo#12`, "
            "`owner/repo#12` or a GitHub URL. Name every reference that has to land, because "
            "the card only resumes once all of them are closed or merged. Use it only for a "
            "dependency GitHub can settle by itself; a blocker that needs a person to decide "
            "something is `needs_more_info`, not a reference that will never close. If "
            "a worker-control operation reports a `disabled:` or `stale:` reason, make no "
            "further change and stop without another one; there is nothing to acknowledge, "
            "because the AgentDeck runtime ends a stopped turn itself. An `unavailable:` "
            "reason is not a stop at all — wait and retry.\n\n"
            "For a merge-review dispatch, report the verdict through the same interface:\n\n"
            f"    {control('review')} pass --body-file <path>\n"
            f"    {control('review')} changes-needed --body-file <path>\n\n"
        )
        final_report_text = (
            "This report is required even when the instruction above says to post a single "
            "comment or to stop when the work is done. It is separate from the work's own "
            "comments and does not count as one of them: post it last, after everything else "
            "you were asked to do.\n"
        )
    owned = [str(item.path) for item in prepared.workspaces]
    if len(owned) == 1:
        workspace_text = (
            f"Your worktree — your only workspace, already your cwd: {prepared.cwd}\n"
            f"Your branch: {prepared.branch}\n\n"
            "Work only inside this worktree."
        )
        if prepared.borrowed_from:
            # The card's repository holds no code, so the worker starts in a declared code
            # workspace. That workspace is not necessarily the implementation owner: a tracker
            # may start in a docker superproject, for example, while a product fix belongs to
            # one of its services. Investigation-only PRs are forbidden globally below (#1209).
            workspace_text += (
                f" This card's repository holds no code of its own: the worktree is in "
                f"{prepared.borrowed_from}'s checkout. If the card requires a repository change, "
                "open its pull request in the repository that owns the requested implementation; "
                "that may be "
                f"{prepared.borrowed_from} or another repository you inspect. If the "
                "implementation "
                f"belongs elsewhere, leave this starting branch unchanged; do not create a "
                f"companion pull request in {prepared.borrowed_from}. "
                f"Reference the card as {ref} in whichever pull request carries the "
                "implementation, "
                "rather than as a bare issue number."
            )
    else:
        listed = "\n".join(f"- {path}" for path in owned) or "- none yet"
        workspace_text = (
            f"Your starting cwd: {prepared.cwd}\n"
            f"Worktrees currently owned by this card:\n{listed}\n\n"
            "Work only inside the starting cwd and worktrees owned by this card."
        )
    # A project whose code spans more than one repository (#1578). The worktree is in one of
    # them; nothing else in the brief would ever say the others exist, and a card whose
    # deliverable belongs in a fork that holds no issues of its own cannot be moved to a
    # tracker there. Naming them — and the reference form a release can actually see — is what
    # keeps that half of the work from being promised and then quietly left behind.
    own_code_repo = prepared.borrowed_from or str(directive.get("repo") or "")
    # Resolved once, here, rather than left for the worker to work out from each repository's
    # guide mid-turn (#1734). The card's own repository decides the closure section below; a
    # sibling that answers differently is named where the brief already sends work into it,
    # because that is the only place the worker learns the other repository exists at all.
    policies = dict(closing_keywords or {})
    closing_policy = policies.get(own_code_repo, CLOSING_KEYWORDS_REQUIRED)
    siblings = {
        repo: path
        for repo, path in sorted((code_checkouts or {}).items())
        if repo and path and repo != own_code_repo
    }
    sibling_text = ""
    if siblings:
        # The card's own workspace is the Work's (#2149), so only a worktree the worker makes by
        # hand in another repository still needs registering, which claims it for the Work
        # (#2155, #2195). Its own
        # paragraph, and nothing after it on the command's line: prose that runs on from a
        # command block is prose a worker pastes into a shell.
        registration_text = (
            "If you need a worktree in one of them, create it first with `git worktree add`, "
            "then register it right away, before editing anything in it, so it becomes this "
            "Work's and is cleaned up when the Work ends. One worktree "
            "per repository: a second one is refused and named. Registration is accepted only "
            "while this "
            f"generation is active:\n\n    {register_command} /absolute/path/to/worktree\n\n"
            if register_command
            else ""
        )
        listed = "\n".join(
            f"- `{repo}` — {path}"
            + (
                " (this repository prohibits closing keywords in a pull request description)"
                if policies.get(repo) == CLOSING_KEYWORDS_PROHIBITED
                else ""
            )
            for repo, path in siblings.items()
        )
        sibling_text = (
            "## Other code this project owns\n\n"
            "This project's code does not all live in your worktree's repository. It also "
            f"holds:\n\n{listed}\n\n"
            "If this card's change belongs in one of those, make it there rather than forcing "
            "it into your worktree: work in that checkout and open the pull request in that "
            "repository.\n\n"
            f"{registration_text}"
            "A pull request outside your card's own repository is released only when its "
            f"description references the card in full — `{ref}`, or the issue's URL. A bare "
            "`#number` is deliberately not trusted there, because the release path will not "
            "let a passing mention in an unrelated repository nominate itself for a merge. A "
            "companion pull request carrying only a bare reference is invisible to the merge "
            "path and to this card's review, and its work is left behind when the card "
            "closes.\n\n"
            "Never describe a companion pull request you have not actually opened. If part of "
            "the deliverable is not done, say so in the terminal comment and do not report the "
            "card as complete.\n\n"
            # #2718: a card shipping two pull requests sat reviewed and never released, because
            # its one verification job credited only its own repository's head, and a third
            # pull request in a repository the board never reads was silently not on the card.
            "Only pull requests in your worktree's repository and the repositories listed "
            "above are part of this card. A pull request in any other repository is never "
            "reviewed, verified or released with it, whatever its description says; if the "
            "work needs one, say so in your terminal comment.\n\n"
            "When this card ships pull requests in more than one repository, each one's head "
            "needs its own green verification job. A verification job credits only the head of "
            "the repository its `--cwd` is in, so submit one job per pull request, with "
            "`--cwd` in that pull request's checkout; a job that only `cd`s into another "
            "checkout still proves just its own.\n\n"
        )
    # The card's own checkout and every sibling this wake looked at, deduplicated by path: a
    # borrowed workspace makes the two lists overlap.
    freshness: dict[Path, CheckoutFreshness] = {
        state.path: state for state in prepared.checkout_states
    }
    for state in sibling_states or ():
        freshness.setdefault(state.path, state)
    stale_text = stale_checkout_brief(freshness.values(), workspaces=prepared.workspaces)
    if account.startswith("codex:"):
        starting_guide_text = (
            "If the actual contents of the root guide for your starting repository are present "
            "in your context, follow them and do not read the guide again. If they are absent, "
            "read the root guide now — its AGENTS.md, or its CLAUDE.md where there is no "
            "AGENTS.md — and follow it."
        )
    else:
        starting_guide_text = (
            "Read the root guide for your starting repository — its AGENTS.md, or its CLAUDE.md "
            "where there is no AGENTS.md — and follow it."
        )
    change_warning_text = f"{change_warning.strip()}\n\n" if change_warning.strip() else ""
    # Name the card's own directory rather than a template: one evidence repository can hold
    # several projects' screenshots, and two cards numbered alike in different repositories must
    # not write to one place.
    card_number = str(directive.get("number") or "").strip()
    evidence_path = (
        f"{str(directive.get('repo') or '').strip()}/issue-{card_number}"
        if card_number and str(directive.get("repo") or "").strip()
        else "issue-<number>"
    )
    visual_text = visual_evidence_brief(artifact_repo, artifact_workspace, evidence_path)
    verification_text = "" if review_only or spec_only else (
        ready_for_review_brief(control("ready-for-review"))
        if directive.get("ready_for_review")
        else verification_evidence_brief(verify_turn=bool(directive.get("verify_turn")))
    )
    if verification_text:
        verification_text += VERIFICATION_SCOPE_BRIEF
    walkthrough_text = "" if review_only or spec_only else USE_CASE_WALKTHROUGH_BRIEF
    return (
        f"You are working {ref} for the AgentDeck poller.\n\n"
        # Naming the *root guide of the repository in hand* is the whole point (#685). Codex has
        # normally injects that guide into the worker context, so telling every Codex worker to
        # read it adds a third copy to every later model call (#1004). The dispatch cannot verify
        # that provider-owned injection, however, so a missing guide must retain an explicit
        # fallback (#1237). Claude does not load AGENTS.md itself, so other providers keep the
        # explicit read. Any worker may still touch another
        # repository whose guide was not injected, and cross-project skills may name a control
        # document that exists only in their source fork.
        f"{workspace_text} {starting_guide_text} If you touch another repository, read that "
        "repository's root guide — its AGENTS.md, or its CLAUDE.md where there is no AGENTS.md — "
        "and follow it. Each root guide is that repository's design of record. Read only the "
        "deeper documents it routes to for the paths you actually inspect or change, so a "
        "design-of-record or control-plane path named by another project's skill does not apply "
        "here: follow the root guide rather than reading for that path or guessing a local "
        "substitute. If the card requires a repository change, open a pull request when that "
        "change is done and validated. "
        f"{push_rules(directive, prepared.base)}"
        "\n\n"
        f"{change_warning_text}"
        f"{sibling_text}"
        f"{stale_text}"
        "## Investigation results\n\n"
        "Never open or update a pull request whose sole purpose is to record an investigation, "
        "diagnosis, research, findings, or evidence. Put those results in the terminal issue "
        "comment, even when the card asks you to investigate or document the findings. If no "
        "requested repository change is needed, end the turn without a pull request. A card that "
        "explicitly requests a change to repository documentation still gets a normal pull "
        "request for that documentation change; the pull request changes the product's "
        "documentation, it does not archive the investigation.\n\n"
        f"{source_issue_closure_brief(ref, closing_keywords=closing_policy)}"
        "## Documentation in unmerged changes\n\n"
        "When documentation changed in this pull request describes behavior introduced by the "
        "same unmerged changes, write it as what this pull request introduces or proposes, not "
        "as behavior the repository already has. Reserve present-tense claims about how the "
        "repository works for behavior that already exists on the pull request's base branch. "
        "In particular, do not add a `LIVE` tag for behavior this pull request introduces.\n\n"
        # Codex unified-exec polling is a model turn: each empty check resends the whole accumulated
        # context. The provider owns that protocol, but the shared brief can keep a worker from
        # reflexively spending one turn every 30 seconds while a long suite is still running.
        "## Long-running commands\n\n"
        "Do not repeatedly poll a running command at short intervals. If a command is still "
        "running, continue independent work before checking it, and take the completion signal "
        "the tool already gives you rather than checking again yourself: a backgrounded command "
        "reports its own exit, and a queued host job messages this chat when it finishes. Run the "
        "command itself in the foreground or through the job queue; do not detach it only to "
        "replace one polling loop with another.\n\n"
        # A worker that cannot wait in the foreground reaches for a detached `sleep` instead, and
        # every countdown that fires costs a full turn on a result nobody needed (#1798).
        "Never start a command whose only purpose is to let time pass. A detached `sleep`, a "
        "countdown, or any wait started so the turn can end and be woken later is not free: "
        "finishing it wakes this chat and spends a whole turn, and several left running wake it "
        "several times over. When nothing is left but waiting, end the turn and name what you are "
        "waiting for. Before you end a turn, stop any timer you already started that no longer "
        "has a purpose, so it cannot wake the chat after the work is done.\n\n"
        "## Unfamiliar external APIs\n\n"
        "Treat route discovery against an unfamiliar external service as read-only. Probe only "
        "with `GET`, `HEAD`, or `OPTIONS`; never send `POST`, `PUT`, `PATCH`, or `DELETE` "
        "speculatively to learn whether a route exists. Read the vendor documentation or ask "
        "instead. If a write must be tested, use a sandbox account or get explicit human "
        "approval before using credentials available in the checkout.\n\n"
        "## Shared pull request branches\n\n"
        "Before pushing to an existing pull request, fetch its current remote branch and confirm "
        "that the fetched remote head is an ancestor of your local `HEAD`, for example with "
        "`git merge-base --is-ancestor FETCH_HEAD HEAD`. If it is not, the remote contains work "
        "you do not have: merge that remote head into your branch (`git merge FETCH_HEAD`) "
        "and resolve the result before pushing. Bring a base branch in the same way, by "
        "merging it. Never rewrite or force-push commits already on the remote, including a "
        "merge commit the poller added to bring the branch up to date (#2675).\n\n"
        "## Recent cross-issue reversals\n\n"
        "Use the current issue and conversation as the authoritative source for this card's "
        "requester:\n\n"
        f"    {issue_requester_command}\n\n"
        "Before finalizing an implementation or review, inspect the last 30 days of history for "
        "the exact behavior and lines being removed, replaced, or reintroduced. Start with "
        "`git log --since=30.days -- <paths>`, then use `git blame` or `git log -L` for affected "
        "lines and `git log -S` for text or behavior being brought back. A commit author is not "
        "necessarily the requester: follow the commit or pull request to its issue and compare "
        "that issue's requester with this card's requester. If this brief does not name the "
        "current requester, read the current issue's author and conversation before deciding. "
        "An unreadable history or unresolved issue link is not itself a conflict: note the gap "
        "and continue unless the available evidence shows competing deliberate requests.\n\n"
        "If the change would undo deliberate work requested by someone else for another recent "
        "issue, do not silently reverse it. Name the commit, prior issue, and requesters in an "
        "issue comment, then report `needs_more_info` unless the issue conversation "
        "already records an explicit decision reconciling the two requests.\n\n"
        f"What was asked:\n\n{body}\n\n"
        # Human asks are passed through verbatim, so an ask that is just a skill's name arrives
        # as one short line under thousands of words of poller contract — and reads as a
        # description of the job rather than a command. A worker asked for `review-fix-pr` hand
        # -rolled a review from the instructions below and never invoked the installed skill
        # (#1110). Say it generically: the worker knows its own skill list, so the poller never
        # has to match names or track one provider's skill layout.
        "If the ask names something you have as a skill, invoke that skill and follow it — a "
        "bare name is a request to run it, not a description of the work to improvise. The "
        "poller's instructions here cover reporting and workspace rules; they never replace the "
        "procedure the skill defines. If you cannot find the named skill, say so in your "
        "terminal report and do the work as described instead. The poller dispatch owns the issue "
        "workflow: do not invoke a generic repository or manual issue-management skill merely "
        "because the repository and issue match its description. Treat that match as a trigger "
        "only when the issue or request explicitly names the skill.\n\n"
        # The deck correlates Work, Run, chat and pull request itself (#2147), so the session is
        # not registered with the poller and not repeated in every issue comment (#2155). The
        # pull request description still names it, for a reader who starts from the pull request.
        "## AgentDeck session link\n\n"
        "Resolve your own session as described in the AgentDeck conventions, then derive its "
        "public AgentDeck session URL using the public-origin rules in those same conventions. "
        "When you open or update a pull request, include that URL in the pull request "
        "description if it is absent there. Never publish the loopback `AGENTDECK_URL`; if no "
        "public origin can be resolved, say so there instead of emitting a broken link. There is "
        "nothing to register with the poller and no link row to add to issue comments.\n\n"
        f"{COMMENT_LAYOUT_BRIEF}"
        "## Issue review packet\n\n"
        "Make the terminal worker-control comment on the issue carry the code review surface. "
        "When a pull request exists, compute it from the final diff against that pull request's "
        "actual base branch after all fixes are committed. When no pull request exists because no "
        "repository change was needed, report a zero diff and say that the investigation produced "
        "no repository change. Assemble it in the body file passed to `finish` or `review`. "
        "Start the collapsed `Details` block from Comment layout with a "
        "one-line `Review surface` that separates source, tests, docs, and generated files, "
        "shows additions and deletions for each bucket, and names the one to three source files "
        "a reviewer should read first. Source means everything not classified as tests, docs, "
        "or generated output by that repository's conventions.\n\n"
        # A blocked run with no diff used to render as `Review surface — source +0/−0 … No
        # source files to review`, which reads like a review that found nothing rather than
        # work that never started (#1230).
        "A run you are finishing as `blocked` is not a review, so do not dress it as one. When "
        "you stopped before making any repository change, lead the comment with a one-sentence "
        "`Blocked surface` instead: say that implementation never started, name what is "
        "blocking it, and say what has to happen before the work can go on. Do not print a "
        "`Review surface` line, a zero-diff bucket breakdown, or `No source code changed` for "
        "that run — there is no diff because nothing was attempted, which is a different fact "
        "from a diff that turned out empty. A blocked run that *did* change the repository "
        "before it stopped still carries the normal review packet for what it changed, under "
        "the `Blocked surface` line.\n\n"
        "In the `Details` block, under `Review surface`, put one `Beyond the card` line naming "
        "anything in the change the card did "
        "not ask for: an extra refactor, a behaviour change made while passing through, an "
        "option nobody requested. Give each one the file it lives in and say whether it could "
        "have shipped separately, and write `Beyond the card: nothing` when the change stayed "
        "inside the ask. Disclosing a deliberate behaviour change elsewhere in the comment is "
        "not the same as having been asked for it, so a disclosed one still belongs on this "
        "line. Do not unwind that work to make the line empty — whether unasked-for work is "
        "welcome is the reader's call, and this line is how they get to make it.\n\n"
        # The packet is the reason poller threads became unreadable: 59–83% of four measured
        # threads was review packets, re-posted whole on every dispatch (#2116). One wrapper
        # rather than one block per file is what makes a ten-file packet cost a reader one
        # line instead of a screenful of collapsed headers, and the halved budget reflects
        # that the pull request's own Files changed tab is where a diff is really reviewed.
        "Then inline source-file diffs verbatim in review-priority order, all of them inside "
        "one outer collapsed `<details>` block summarised `Full diff — N files, M lines`, nested "
        "at the end of the `Details` block, so that the whole packet costs a reader one line "
        "until they ask for it. A file is "
        "eligible only "
        "when it has at most 150 diff lines, and the complete inline packet must stay within both "
        "250 diff lines and 25,000 characters. Put each included file in its own collapsed "
        "`<details>` block, nested inside that wrapper, with the path and `+A−B` in the summary "
        "and a fenced `diff` body. Do "
        "not inline tests, docs, or generated files. Add every source file not inlined to one "
        "trailing `Not inlined — review these in the Files changed tab` list with its path, "
        "`+A−B`, diff-line count, and whether it exceeded the per-file or packet limit. If there "
        "are no source files, say `No source code changed.` Preserve the terminal comment's short "
        "human summary, visual evidence, verification, and review notes. Build it through the "
        "terminal body file so diffs are copied exactly rather than retyped. Keep the pull request "
        "description concise: preserve its issue reference, summary, verification, and AgentDeck "
        "session link, but do not duplicate the review packet there.\n\n"
        f"{verification_text}"
        f"{visual_text}"
        f"{walkthrough_text}"
        "## Worker control\n\n"
        # The lease fences what a stale Run could race its successor on, and nothing else
        # (#2152). Ordinary GitHub collaboration rides the credentials the worker already has;
        # proxying it through the lease is how a superseded worker came to believe it could not
        # even say what it had learned.
        "The Poller lease this dispatch holds controls Poller lifecycle, not your GitHub "
        "access. Lifecycle operations go only through the Poller Instance interface below, "
        "which validates this dispatch generation atomically: the terminal `finish` and a "
        "review verdict. There is no separate lease check to run first — each lifecycle "
        "operation proves the generation itself when it runs. Never write raw engine markers or "
        "edit status directly — neither a board column nor a `status:*` label.\n\n"
        "Ordinary GitHub collaboration does not need the lease. Read issues and pull requests, "
        "post an ordinary issue comment, or open a follow-up issue for a separate problem you "
        "found, with normal `gh` and whatever permissions your credentials already have. Link "
        "the source issue or pull request in plain words where it helps; no hidden marker is "
        "needed, and a follow-up issue does not become part of this card. Never start a comment "
        "or issue you write with a board command such as `/claude` or `/merge`: posted under "
        "the host's account it is read as a real command. No plan comment is required: post "
        "progress on the issue only when the card asks for it or it genuinely helps a reader, "
        "because the deck already shows this Run as it happens.\n\n"
        # A worker that reads every refusal as terminal throws away a finished turn the moment
        # the shared GitHub query reserve defers one card read, which on this host is routine
        # (#1651). The prefix is the whole instruction: only `disabled:` and `stale:` mean stop.
        "Every worker-control refusal starts with one of three words, and they mean different "
        "things. `unavailable:` means the card could not be read this second — your lease is "
        "**not** stale, nothing was written, and the right response is to wait a short while "
        "and run the same operation again, as many times as it takes. Never abandon a turn or "
        "throw away finished work on an `unavailable:` reason. `stale:` means another "
        "generation now holds the card, and `disabled:` means this generation may no longer "
        "act; both are final, so stop and follow what the reason says. Stop means you no "
        "longer own this card's execution: make no further code or workspace change, push "
        "nothing that could race whoever holds the card now, and run no further lifecycle "
        "operation. It does not revoke your GitHub credentials — you may still post one "
        "ordinary comment saying what your turn reached, or open a follow-up issue, with "
        "normal `gh`.\n\n"
        # A parked worker answered in its own chat keeps this lease (#2348).
        "If this card rests in Needs More Info and a person answers in this chat, the deck "
        "posts the answer on the issue and moves the card back to In progress for you, under "
        "this same generation: carry on, and end with the same terminal command as usual. "
        "A turn that only answers in the chat ends with one too — `finish needs_more_info` if "
        "you still need an answer; a turn that ends without one reads as a crash.\n\n"
        # A worker whose `resolved` closed the card can take it back when asked (#2522).
        f"{REOPEN_ASK}\n\n"
        f"{WORK_TITLE_ASK}\n\n"
        f"{completion_text}"
        # The one dispatch whose terminal operation is *not* `finish`. Without saying so plainly,
        # a worker that has just been handed a three-way `finish` menu reports `in_review` out of
        # habit, and the poller has to spend a whole second turn discovering nothing was reviewed.
        + (
            "**For this dispatch the verdict above is the terminal operation, not `finish`.** "
            "Report `pass` or `changes-needed` once the work and its review pass are both done; "
            "that is what the poller waits for. Use `finish needs_more_info` or `finish blocked` "
            "only if you cannot produce a verdict at all, and do not report `finish "
            "in_review`.\n\n"
            if review_only and not spec_only
            else ""
        )
        + "The generation is bound to this dispatch: do not change it, and do not report work "
        "you did not finish.\n\n"
        # The one real card that wedged did so because its instruction said "post one comment,
        # then stop", which read as forbidding this report (#279). The instruction is the
        # operator's and is passed through verbatim, so the conflict is resolved here instead.
        f"{final_report_text}"
    )


def _abandon(prepared: PreparedWorkspaces) -> str:
    """Say plainly what an abandoned preparation leaves behind.

    A Work's claimed worktree is kept for its next Run, since a Run ending releases nothing
    (#2149). This wake deletes nothing (#2195): anything else an adapter handed back is named
    in the skip line rather than left as a directory nobody mentions again.
    """
    stranded = [
        str(workspace.path) for workspace in prepared.workspaces if not workspace.work_claimed
    ]
    if not stranded:
        return ""
    return f" (left on disk, unregistered this wake: {', '.join(stranded)})"


def _sibling_states(
    code_checkouts: Mapping[str, str] | None, refresher: CheckoutRefresh | None
) -> list[CheckoutFreshness]:
    """Refresh the project's other code checkouts, which no card owns but every worker can read.

    The failure this card came from was exactly here: a card worked in one repository read a
    second repository's checkout directly, and that tree was twenty commits behind (#1853).
    Each is refreshed under its own default branch, because a project's repositories need not
    agree on one.
    """
    if refresher is None:
        return []
    states = []
    for path in sorted({str(value) for value in (code_checkouts or {}).values() if value}):
        candidate = Path(path)
        if candidate.is_dir():
            states.append(refresher(candidate))
    return states


def act_on(
    directives: list[dict],
    *,
    dispatcher: WorkerDispatcher,
    checkouts: dict[str, Path] | None = None,
    borrowed: Mapping[str, str] | None = None,
    # Every code repository this project holds, not only the ones cards are worked in (#1578).
    code_checkouts: Mapping[str, str] | None = None,
    # Where this project keeps screenshot evidence, if it named a repository for it (#1791).
    artifact_repo: str = "",
    artifact_checkout: str = "",
    # Each code repository's pull request closing-keyword policy, from the Manifest (#1734).
    closing_keywords: Mapping[str, str] | None = None,
    base_branch: str = "master",
    workspace_adapter: WorkspaceAdapter | None = None,
    register: Callable[[dict, list[str]], bool] | None = None,
    register_command: Callable[[dict], str] | None = None,
    # The runtime's Work workspace service (#2149); unset, no delivery can be prepared.
    work_workspaces: WorkWorkspaces | None = None,
    control_command: str = "poller-engine worker-control",
    # Where this wake writes each dispatch's own control wrapper (#1732). Without it the brief
    # falls back to spelling the whole engine invocation out on every command line.
    control_root: Path | str | None = None,
    change_warning_for: Callable[[dict, PreparedWorkspaces], str] | None = None,
    # Fetch each checkout's base, fast-forward the ones it is safe to, and tell the worker
    # about the ones it is not (#1853). Off in tests that must not touch a real remote.
    refresh_checkouts: bool = True,
    # The Manifest's ``policy.worker_may_deploy`` (#2656), stamped on every work directive so
    # both the composed brief and the compacted one the runtime delivers say the same thing.
    # The default forbids every deploy, so a caller that names no level widens nothing.
    worker_may_deploy: str = "none",
    # The Manifest's ``policy.worker_may_merge`` (#2723), stamped the same way. The default
    # forbids every merge, so a caller that names no level widens nothing.
    worker_may_merge: str = "never",
) -> WakeResult:
    """Turn each directive into a worker, in the order the engine produced them.

    Where each one runs is the runtime's decision, made from the Overlay's account policy the
    dispatcher carries (#2146); nothing here names an account.
    """
    default_adapter = workspace_adapter is None
    # One per wake, so a repository carrying three cards is fetched once (#1853).
    refresher = CheckoutRefresh(base_branch=base_branch) if refresh_checkouts else None
    adapter = workspace_adapter or SingleCheckoutWorkspaceAdapter(
        checkouts=dict(checkouts or {}),
        base_branch=base_branch,
        borrowed=dict(borrowed or {}),
        refresh_checkout_state=refresher,
        work_workspaces=work_workspaces,
    )
    result = WakeResult(directives=len(directives))
    for directive in directives:
        ref = str(directive.get("ref") or "")
        repo = str(directive.get("repo") or "")
        if directive.get("steer"):
            # A steer goes to the account holding its own Run, even after that turn ended: the
            # runtime chooses it from its frozen route and deduplicates the stable delivery id
            # there, so an accepted-but-unacknowledged instruction is recognized rather than
            # re-sent under a new one (#1259, #2146). A Run nothing holds is refused there.
            delivery = dispatcher.deliver(directive, cwd=None)
            if delivery.outcome is DeliveryOutcome.DELIVERED:
                result.delivered += 1
            elif delivery.outcome in {
                DeliveryOutcome.REFUSED,
                DeliveryOutcome.SAFE_RETRY,
            }:
                result.refused += 1
            else:
                result.uncertain += 1
            result.log.append(f"{ref}: {delivery.detail}")
            continue
        directive = {
            **directive,
            "worker_may_deploy": worker_may_deploy,
            "worker_may_merge": worker_may_merge,
        }
        prepared = adapter.prepare(directive)
        if prepared is None:
            if default_adapter and repo not in (checkouts or {}):
                why = f"no checkout for {repo!r} on this host"
                result.skipped.append(f"{ref}: {why}")
            else:
                # A project adapter need not say why; the default one does (#2778).
                why = str(getattr(adapter, "refusal", "") or "")
                result.skipped.append(
                    f"{ref}: workspace adapter could not prepare the delivery"
                    + (f" — {why}" if why else "")
                )
            # Nothing was sent, so say so: silence reads to the engine as an attempt that
            # may have started a worker, and its park then says nobody answered (#2816).
            try:
                dispatcher.refuse(directive, f"{WORKSPACE_REFUSAL}: {why or 'no detail given'}")
            except OSError as exc:  # unwritten, it is only the old silence; other cards go on
                result.log.append(f"{ref}: the refusal could not be recorded: {exc}")
            continue
        paths = prepared.paths
        if not prepared.cwd.is_dir():
            result.skipped.append(
                f"{ref}: workspace adapter returned a missing cwd"
                f"{_abandon(prepared)}"
            )
            continue

        # This card's own worktree of the evidence repository, the Work's artifact claim,
        # prepared before registration so it is fenced and recorded with the rest (#1791). A
        # card that cannot get one is told to commit its screenshots beside the code, exactly
        # as before — never to share the
        # evidence checkout with whatever else is running, which is one index and one working
        # tree for every concurrent card at once.
        evidence: Workspace | None = None
        if artifact_repo and artifact_checkout:
            evidence = _evidence_workspace(
                directive, Path(artifact_checkout), artifact_repo, repo, work_workspaces
            )
            if evidence is None:
                result.log.append(
                    f"{ref}: no evidence worktree in {artifact_checkout!r}; screenshots stay "
                    "with the code this time"
                )
            else:
                paths = [*paths, str(evidence.path)]

        # All of this card's worktrees are registered in one accepted act, or none of them
        # are. A stale generation must never be delivered, and a half-registered set would
        # leave the card's record of where it worked incomplete.
        if register and not register(directive, paths):
            stranded = _abandon(prepared)
            result.skipped.append(
                f"{ref}: could not register prepared worktrees{stranded}"
            )
            continue

        change_warning = change_warning_for(directive, prepared) if change_warning_for else ""
        card_control = (
            write_card_control(control_root, directive, control_command) if control_root else ""
        )

        compose_options = dict(
            control_command=control_command,
            card_control=card_control,
            change_warning=change_warning,
            code_checkouts=code_checkouts,
            # Refreshed here rather than up front, and memoized, so a wake that delivers
            # nothing touches no remote at all.
            sibling_states=_sibling_states(code_checkouts, refresher),
            artifact_repo=artifact_repo,
            artifact_workspace=evidence,
            closing_keywords=closing_keywords,
        )

        if directive.get("spec_only") and card_control:
            # The brief this spec worker follows if the person approves in its chat and asks it
            # to build (#2716): the same Run, so the same wrapper, workspaces and generation.
            build = {
                key: value
                for key, value in directive.items()
                if key not in {"spec_only", "build_flags"}
            }
            build.update(directive.get("build_flags") or {})
            build["spec_approved"] = True
            build_message = compose_message(
                build,
                prepared,
                register_command=register_command(build) if register_command else "",
                **compose_options,
            )
            build_brief = write_build_brief(
                card_control,
                _compact_poller_brief(
                    build, build_message, cwd=str(prepared.cwd), worktrees=paths
                ),
            )
            if build_brief:
                directive = {**directive, "build_brief": build_brief}
        delivery = dispatcher.deliver(
            directive,
            cwd=str(prepared.cwd),
            message=compose_message(
                directive,
                prepared,
                register_command=register_command(directive) if register_command else "",
                **compose_options,
            ),
            worktrees=paths,
        )
        if delivery.outcome is DeliveryOutcome.DELIVERED:
            result.delivered += 1
        elif delivery.outcome in {
            DeliveryOutcome.REFUSED,
            DeliveryOutcome.SAFE_RETRY,
        }:
            result.refused += 1
        else:
            result.uncertain += 1
        # `refused (over_budget)` rather than a bare `refused` (#482): without the
        # reason a refusal that will retry elsewhere is indistinguishable from one
        # that will not, which is how #470 and #482 sat `Blocked` unnoticed.
        result.log.append(f"{ref}: {delivery.detail}")
    return result


def read_directives(stdout: str) -> list[dict]:
    """One JSON object per line. A line that is not one is not a directive, and is ignored —
    the engine writes its log to stderr, but a stray line must never abort a whole wake."""
    directives = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("ref"):
            directives.append(value)
    return directives




def drain_review_verdicts(
    directory, *, base_url: str, curl: str = "curl", timeout: float = 10.0
) -> list[str]:
    """Hand the engine's consumed review verdicts to the deck's database (#1122).

    The engine decides what a verdict meant but cannot reach the database, so it leaves one
    record per reviewed commit on disk and this drains them. A record is removed only after
    the deck accepts it; the table is keyed by repository and commit, so re-sending one whose
    removal failed replaces the same row rather than adding another.

    Every failure is silent and retryable. A restarting web process, an unreachable deck, or
    a refused write leaves the record where it is for the next wake — losing this history is
    bad, but stopping a wake over it would be worse.
    """
    lines: list[str] = []
    for path, body in reviews.pending(directory):
        if not _post_review_verdict(body, base_url=base_url, curl=curl, timeout=timeout):
            continue
        reviews.clear(path)
        lines.append(
            f"recorded review verdict {body.get('verdict')} "
            f"({body.get('result')}) for {body.get('repo')}@{str(body.get('sha'))[:12]}"
        )
    return lines


def _post_review_verdict(
    body: dict, *, base_url: str, curl: str, timeout: float
) -> bool:
    """True only when the deck says it stored the record."""
    try:
        done = subprocess.run(
            [
                curl,
                "--fail-with-body",
                "--silent",
                "--show-error",
                "--max-time",
                str(int(timeout)),
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "--data-binary",
                json.dumps(body),
                f"{base_url.rstrip('/')}/api/poller/review-verdicts",
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode != 0:
        return False
    try:
        payload = json.loads(done.stdout or "")
    except ValueError:
        return False
    return bool(isinstance(payload, dict) and payload.get("recorded"))
