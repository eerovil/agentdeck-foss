"""The deck starts a switched project's Work itself (#2895 step 5b, ADR 0008 D2).

The Poller makes no card (step 5a, and since #3732 it has no cards at all), so
the ownership label landing on an issue no chat owns is the Work's start: the router calls
``start_work_agent``, which starts one Claude chat with the ``work`` role in a checkout of the
issue's repository. It is a Work side chat underneath (#2388): saved under its action id
before the spawn, which is also its session token, so the chat joins the Work with no Run once
it exists and its verification jobs prove the Work's head.

At most once per Work: the action id is derived from the Work id and reserved before the
spawn, so a replayed delivery, a second label or a web restart mid-start never starts a second
agent. Only a start that may have launched stays spent. One refused before anything ran (the
account busy or full, another start of it in progress) is settled ``failed`` and freed by the
next try, which the router's back-off, the hand-over pass and the 15-minute backstop make
(#2971). The backstop's start waits a little for the account's start in progress; the
router's never does, so no other delivery waits behind it, and a start refused because one is
in progress is retried on the router's back-off. ``start_new`` only queues the spawn, so a
start counts as made once the runtime accepted the new chat: the row stays unsettled (spent)
until it answers, and a clean refusal with no session (``at_capacity``) is settled ``failed``
in the background and handed back to the caller's ``on_refused``: the router puts it on its
back-off, so a full account delays a Work by minutes, not until the backstop (#3006). A start
settled ``done`` says so on the issue (``work_signals``, #3109): 🚀 and a link to the Work. An
issue carrying its project's spec-only label starts an agent told to write the spec and not
build it (#3134); the label coming off later is the approval the router passes on. The
account is chosen among the Claude accounts that may start a chat by the rule the runtime
places a Poller's new card with (``choose_account``, #3304): the one with the most budget left
per hour leads, and one at 100% of a window, or with its hard-quota circuit open, is passed
over. When every one would refuse, the one whose block clears first still takes it.

A Work filed from the deck's New work form does not wait for GitHub (#3590): its agent starts
the moment the form is pressed (``start_before_issue``), in a Work spare the form kept booted
(``start_spares``) or, without one, a fresh ``.worktrees/work-spare-<hex>`` worktree, told only
the request. Once GitHub answers with the number, ``bind_before_issue`` makes it the Work's one
start, links the chat to the Work, has the claim service adopt that worktree onto
``agent/issue-<n>`` (with an ``issue-<n>`` symlink beside it) and sends the agent the issue and
the rest of its brief. A filing GitHub refused stops the agent (``stop_before_issue``).

The agent starts in the Work's own worktree, never the repository's shared main checkout
(#3274): the runtime's claim service (``work_workspace_claims``, #2149) makes, or hands back,
``.worktrees/issue-<n>`` on ``agent/issue-<n>`` from the default branch, and Finish gives it
back (``release_work_worktrees``). Anything that stops the claim falls back to the main
checkout, saying why in the first message, so a Work never stalls on it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import time
import tomllib
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import IssueRef
from .work import WorkRef, work_id_for
from .work_side_chat import first_message, local_repository_checkout
from .work_signals import SPEC_MARKER
from .work_workspace import WorkspaceProbe
from .work_workspace_claims import SPARE_PREFIX, GitHost

log = logging.getLogger(__name__)

#: What the Work's agent is first told; the role (``work``) says how to do it.
FIRST_TEXT = "This chat is the Work for this issue. Read the issue, then do what it asks."

#: What the Work's agent is told about merging, by its project's ``auto_release`` (#3092). The
#: role's guide is the same for every project, so without this an agent on a project that
#: waits for a person's Merge press told its owner the deck would merge it.
MERGES_ITSELF = (
    "This project merges on its own: the deck merges your pull request once a review passed "
    "and the tests are green on the same head."
)
WAITS_FOR_MERGE = (
    "This project does not merge on its own: once a review passed and the tests are green on "
    "the same head, your pull request waits for a person to press Merge on the Work page. "
    "When it gets there, say in your final reply that it is ready for them to merge."
)

#: What the Work's agent is told about the repositories its project proves another way
#: (#3147): the role's guide has every head proven by a verification job, which in a
#: ``github_checks`` repository runs a suite its own CI already runs on that head.
CI_TESTS_TEXT = (
    "In {repos}, the pull request's own GitHub CI on its head is the test result: do not run "
    "a verification job there. Register a wake-on for that commit's checks instead, and fix "
    "what they fail."
)
REVIEW_ONLY_TEXT = "In {repos}, the review alone proves a pull request: there is no test run."

#: Where the Work's agent commits its screenshots (#3098): the project's evidence repository,
#: in the folder the Poller's workers used (``<repo>/issue-<number>``), so one evidence
#: repository can hold every project's images.
EVIDENCE_TEXT = "Commit this Work's screenshots to `{evidence}` under `{folder}/`."

#: What the agent of an issue carrying its project's spec-only label is told (#3134): the
#: Poller's spec brief, without its card-control commands, which a Work's agent does not have.
SPEC_ONLY_TEXT = (
    "This issue carries the `{label}` label, so it is a spec only: write a concrete "
    "implementation specification as an issue comment ending with the line `{marker}`, so "
    "the deck shows it waiting on the person's approval, and do not edit repository files "
    "other than docs and plans under `docs/` in your own worktree, left uncommitted (draw "
    "the plan's diagram there, put it on your live page and link it in the spec), "
    "create or switch a branch, commit, push, or open a pull request. Building starts only "
    "once a person approves the spec; never remove the label without their explicit "
    "approval. If they approve in this chat and ask you to build it, remove the label "
    "(`gh issue edit {number} --repo {repository} --remove-label {label}`) and build it "
    "here; if they remove it on GitHub, the deck tells this chat. {questions}"
)

#: How long a start waits for the account's start in progress before trying anyway.
START_SPACING_S = 30.0

#: What the Work's agent is told about its folder (#3274): its own worktree, or why not.
WORKTREE_TEXT = (
    "You are in this Work's own worktree `{path}` on `{branch}`; never edit the main "
    "checkout `{main}`."
)
NO_WORKTREE_TEXT = (
    "The deck could not make this Work's worktree ({why}), so you are in the shared main "
    "checkout `{main}`: make `{path}` on `{branch}` yourself before editing anything."
)

#: What an agent started before its issue exists is first told (#3590), and then, once GitHub
#: answers, what tells it the issue; the brief after it is a deck start's own.
BEFORE_ISSUE_TEXT = (
    "This chat is the Work for an issue the deck is filing right now in {repository}; the "
    "request, as filed, is below. Within seconds the deck sends you the issue's number, your "
    "branch and the rest of your brief. Until that message arrives only read: do not comment, "
    "commit, push or open a pull request."
)
ISSUE_FILED_TEXT = "Your issue is filed: {url}. Read it, then do what it asks."
#: What it is told when GitHub refused the issue, or may not have filed it.
NOT_FILED_TEXT = "The deck could not file this Work's issue ({why}). Stop here and do nothing more."
MAYBE_FILED_TEXT = (
    "The deck does not know whether this Work's issue was filed ({why}). Do nothing more until "
    "the deck or a person tells you its number."
)
#: A spare worktree no start took and nothing changed in this long is swept away.
SPARE_SWEEP_AGE_S = 3600.0

#: The runtime's claim routes (``work_workspace_claims``); a test pins the spelling.
ENSURE_PATH = "/work-workspaces/ensure"
RELEASE_PATH = "/work-workspaces/release"
#: A claim fetches the base and adds a worktree, and the router waits for the start, so
#: past this the start goes on in the main checkout rather than hold every delivery.
ENSURE_TIMEOUT_S = 30.0


def start_action_id(work_id: str) -> str:
    """The one action id, and session token, a Work's deck start may ever use."""
    return "work-start-" + hashlib.sha256(work_id.encode()).hexdigest()[:20]


def work_accounts(app: Any, repository: str = "") -> list[Any] | None:
    """The Claude accounts this repository's Work may run on, in the project's order.

    The project's own choice: its overlay's ``[worker] account`` and fallbacks, in order,
    keeping only Claude accounts that may start a chat now. A project that names accounts
    runs only on them, as its Poller would. A readable overlay that names none takes every
    Claude account the new-chat form would offer. An unreadable overlay fails closed
    (``None``). Only Claude: a Codex start cannot take the Work role yet (#2895). Work start
    and the usage move (#3362) both choose from this list.
    """
    from .poller_ops import work_accounts
    from .providers import PROVIDERS

    inject = getattr(app.state.config, "inject", None)
    if inject is None or not inject.enabled:
        return None
    claude = [account for account in app.state.accounts if account.provider_id == "claude_code"]
    preferred: tuple[str, ...] = ()
    poller = getattr(app.state.config, "poller", None)
    if repository and poller is not None:
        try:
            choices = work_accounts(poller.root)
        except Exception:  # noqa: BLE001 - no configured answer must fail closed
            log.warning("work start: could not read the account choice", exc_info=True)
            return None
        configured = choices.get(repository.lower(), ())
        if configured is None:
            log.warning("work start: the account choice for %s is unreadable", repository)
            return None
        preferred = configured
    if preferred:
        by_key = {account.key: account for account in claude}
        claude = [by_key[key] for key in preferred if key in by_key]
    provider = PROVIDERS["claude_code"]
    return [account for account in claude if provider.can_start_session(account)]


def start_account(app: Any, repository: str = "") -> Any:
    """The Claude account to start this repository's Work on, or ``None``.

    Of the project's own accounts (``work_accounts``), usage balances the choice as it does
    a Poller's new card (#3304).
    """
    from .delivery_scheduling import choose_account
    from .providers.usage import shared_cache_dir

    accounts = work_accounts(app, repository)
    if not accounts:
        return None
    startable = {account.key: account for account in accounts}
    usage = getattr(app.state.config, "usage", None)
    notes: list[str] = []
    chosen = choose_account(
        list(startable),
        shared_cache_dir(usage.shared_cache_dir) if usage is not None else None,
        {},
        notes,
        cold=True,
        planned_resets=app.state.db.planned_resets(),
    )
    for note in notes:
        log.info("work start: %s", note)
    return startable.get(chosen)


def _merge_text(app: Any, repository: str) -> str:
    """Whether this repository's project merges on its own, and which of its repositories
    its CI or its review proves, for the first message; empty when its manifest cannot say,
    so the role's guide is left to cover both cases."""
    from .poller_ops import release_policies

    poller = getattr(app.state.config, "poller", None)
    if poller is None:
        return ""
    try:
        policy = release_policies(poller.root).get(repository.lower())
    except Exception:  # noqa: BLE001 - a missing line must never stop the start
        log.warning("work start: could not read the merge policy", exc_info=True)
        return ""
    if policy is None:
        return ""
    texts = [MERGES_ITSELF if getattr(policy, "auto_release", False) else WAITS_FOR_MERGE]
    for repos, template in (
        (getattr(policy, "github_checks", ()), CI_TESTS_TEXT),
        (getattr(policy, "review_only", ()), REVIEW_ONLY_TEXT),
    ):
        if repos:
            texts.append(template.format(repos=", ".join(f"`{repo}`" for repo in sorted(repos))))
    return " ".join(texts)


# A spec-only Work waits for its approval whatever the spec says, and a standing "grill me"
# instruction reads that wait as a call for questions. #2424's spec asked four, and three had
# answers the worker had already given itself (#2427). A question costs the person a round trip
# on a spec they read in full before anything is built, so choices go in a numbered list they can
# overrule instead. The one deliberate exception is disagreement (#3234): on #3228 the owner
# asked to be grilled on their own choices, and arguing against them caught five real problems,
# so a worker that doubts a choice now challenges it unasked, with no cap, while the spec is open.
SPEC_ONLY_QUESTION_RULE = (
    "The person reviewing this spec reads every decision in it before anything is built, so a "
    "question only adds a round trip. Decide everything you can, and list each choice as a "
    "numbered **Decisions** item (D1, D2, …) right after the summary, one line each with its "
    "reason; they can overrule any of them by number while approving. These are assumed, "
    "not chosen: mark only what the person picked or wrote `(chosen)`. Approving the spec "
    "does not make the rest chosen, so the build may change a wrong one and say so in its "
    "pull request (#3690). In a chat reply, never "
    "cite a decision or question by its number alone: restate it in a few words (#2779). Ask "
    "a question only for a preference only the person holds and nothing you can read reveals: "
    "extra cost (API calls, "
    "tokens, money), accepting a known gap or risk, or a conflict with another requester's "
    "recent work. Give your recommendation with each question. Scope beyond what the issue "
    "asked for is not a question: leave it out, or mention it in one line. Go wide before "
    "you narrow: up to three other ways your research turned up to meet the issue, not doing "
    "it included, each one line under **Ideas** with where it came from; none is built unless "
    "the person keeps it. "
    "The one other question is a challenge, asked without being asked to (#3234): while the "
    "spec is still open, grill the person on anything in it you think is not a good idea, "
    "whether a choice they made, one of yours they accepted, or an earlier choice of theirs "
    "the spec reverses. Do it only when you really disagree or doubt, never as a ritual. Ask "
    "each challenge as one question in your structured question tool (where you have none, "
    "as an open question in the spec), worded `You chose X. I think Y is better, because "
    "<fact>. Which do you want?`, with your recommended option first. Ask up to four a "
    "round, and keep asking rounds while you still disagree with something; never re-ask a "
    "choice the person has answered. Write each answer into the spec comment at once, "
    "marked as the owner's choice, before the next round. Keep your own copy of the spec and "
    "edit that copy, because re-reading the comment through `gh` can return a stale cached "
    "copy that overwrites your earlier edits. Once the spec is approved, build it rather "
    "than reopen its choices. Whenever your question tool asks whether the spec is ready, "
    "include the option `Grill me on the spec`; if it is chosen, challenge every choice you "
    "have any doubt about. "
    "Most specs have no questions; then write `No open questions — approve, or overrule a "
    "decision by number.` "
    "Keep the summary, the Decisions and the questions (or that line) open, and put the "
    "detailed spec — files, wording, tests, steps — inside one collapsed `Details` block. "
    "This brief takes precedence over any standing instruction to question the requester "
    "before starting."
)


def _spec_only_text(app: Any, repository: str, number: int, labels: Iterable[str]) -> str:
    """The spec-only brief when ``labels`` hold the project's spec-only label, else empty."""
    from .poller_ops import release_policies

    poller = getattr(app.state.config, "poller", None)
    names = {str(name).strip().casefold() for name in labels}
    if poller is None or not names:
        return ""
    try:
        policy = release_policies(poller.root).get(repository.lower())
    except Exception:  # noqa: BLE001 - logged; an unread manifest is no spec-only label
        log.warning("work start: could not read the spec-only label", exc_info=True)
        return ""
    label = str(getattr(policy, "spec_only_label", "") or "").strip()
    if not label or label.casefold() not in names:
        return ""
    return SPEC_ONLY_TEXT.format(
        label=label, number=number, repository=repository, questions=SPEC_ONLY_QUESTION_RULE,
        marker=SPEC_MARKER,
    )


def _evidence_text(app: Any, repository: str, number: int) -> str:
    """Where this Work's screenshots go, for the first message; empty when its project names
    no evidence repository, so the role's guide has them committed beside the code."""
    from .poller_ops import work_evidence_repositories

    poller = getattr(app.state.config, "poller", None)
    if poller is None:
        return ""
    try:
        evidence = work_evidence_repositories(poller.root).get(repository.lower())
    except Exception:  # noqa: BLE001 - a missing line must never stop the start
        log.warning("work start: could not read the evidence repository", exc_info=True)
        return ""
    if not evidence:
        return ""
    folder = f"{repository.split('/', 1)[-1].lower()}/issue-{number}"
    return EVIDENCE_TEXT.format(evidence=evidence, folder=folder)


#: A Work started with a pull request already open for it (#3834): one taken over from another
#: deck, whose agent's work is on that branch. The deck reviews and merges only pull requests
#: this deck's chats registered, so the agent registers it before anything else.
OPEN_PULL_TEXT = (
    "A pull request is already open for this issue: {pulls}, from an earlier agent, or from "
    "another deck's that this deck took over. Register it for this chat first, then read its "
    "branch, the pull request and the issue's comments before changing anything."
)


def _open_pull_text(app: Any, repository: str, number: int) -> str:
    """The open pull requests the deck's cached lists hold for this issue, for the brief."""
    from .issue_context import identity

    issues = getattr(app.state, "issues", None)
    try:
        backlog = issues.cached() if issues is not None else None
        pulls = backlog.pulls_for_work().get(identity(repository, number), ()) if backlog else ()
    except Exception:  # noqa: BLE001 - a missing line must never stop the start
        log.warning("work start: could not read the cached pull requests", exc_info=True)
        return ""
    urls = [str(pull.get("url")) for pull in pulls if pull.get("url")]
    return OPEN_PULL_TEXT.format(pulls=", ".join(urls)) if urls else ""


def _overlay_checkouts(poller_root: str | Path) -> list[str]:
    """Every checkout a Poller overlay names (``[checkouts]``): where a project's code lives."""
    found: list[str] = []
    for path in sorted(Path(poller_root).expanduser().glob("instances/*/overlay.toml")):
        try:
            checkouts = tomllib.loads(path.read_text()).get("checkouts")
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if isinstance(checkouts, dict):
            found.extend(value for value in checkouts.values() if isinstance(value, str))
    return found


def default_branch(main: str) -> str:
    """The main checkout's ``origin/HEAD`` branch (``master``), or "" when it cannot be read."""
    try:
        done = subprocess.run(
            ["git", "-C", main, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    ref = done.stdout.strip() if done.returncode == 0 else ""
    return ref.removeprefix("origin/") if ref.startswith("origin/") else ""


def exclude_worktrees(main: str) -> None:
    """Keep ``.worktrees/`` out of the main checkout's status, locally (``info/exclude``)."""
    info = Path(main) / ".git" / "info"
    exclude = info / "exclude"
    try:
        if not (Path(main) / ".git").is_dir():
            return
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        if any(
            line.strip() in {".worktrees", ".worktrees/", "/.worktrees", "/.worktrees/"}
            for line in lines
        ):
            return
        info.mkdir(exist_ok=True)
        with exclude.open("a") as handle:
            if lines and lines[-1].strip():
                handle.write("\n")
            handle.write(".worktrees/\n")
    except OSError:
        log.warning("work start: could not exclude .worktrees in %s", main, exc_info=True)


def _existing_worktree(path: str, repository: str) -> bool:
    """Whether ``path`` is already a readable checkout of ``repository`` at its own top."""
    seen = WorkspaceProbe().observe(path)
    return seen.readable and seen.repository == repository.lower() and seen.toplevel == path


async def work_worktree(app: Any, ref: WorkRef, main: str) -> tuple[str, str]:
    """The folder the Work's agent starts in and the sentence that says so (#3274).

    The Work's claimed worktree; one already at its path that the claim would not take over
    (an earlier agent's, holding unpushed work), unclaimed, so Finish never removes it; else
    the main checkout, with the reason. Never raises.
    """
    path = str(Path(main) / ".worktrees" / f"issue-{ref.number}")
    branch = f"agent/issue-{ref.number}"
    worktree = WORKTREE_TEXT.format(path=path, branch=branch, main=main)

    def fallback(why: str) -> tuple[str, str]:
        log.warning("work start: %s starts in the main checkout: %s", ref.work_id, why)
        return main, NO_WORKTREE_TEXT.format(why=why, main=main, path=path, branch=branch)

    base = await asyncio.to_thread(default_branch, main)
    if not base:
        return fallback("the main checkout's default branch could not be read")
    await asyncio.to_thread(exclude_worktrees, main)
    client = getattr(app.state, "runtime_http", None)
    if client is None:
        return fallback("the runtime is not reachable")
    body = {
        # The canonical spelling, which Finish's release names, whatever case the caller used.
        "work_id": work_id_for(IssueRef(ref.repository, ref.number)),
        "repository": ref.repository.lower(),
        "purpose": "implementation",
        "checkout": main,
        "path": path,
        "branch": branch,
        "base_ref": base,
        # No Poller card holds a Work the deck starts, so it has no worktrees of its own.
        "legacy_paths": [],
    }
    try:
        response = await client.post(ENSURE_PATH, json=body, timeout=ENSURE_TIMEOUT_S)
        answer = response.json() if response.content else None
    except Exception as exc:  # noqa: BLE001 - any failure is the fallback, never a lost start
        return fallback(f"the runtime did not answer: {type(exc).__name__}")
    if response.status_code != 200 or not isinstance(answer, dict):
        detail = answer.get("detail") if isinstance(answer, dict) else ""
        return fallback(str(detail or f"the runtime answered {response.status_code}"))
    claim = answer.get("claim")
    if answer.get("outcome") in {"created", "reused", "adopted"} and isinstance(claim, dict):
        claimed = str(claim.get("path") or path)
        return claimed, WORKTREE_TEXT.format(
            path=claimed, branch=str(claim.get("branch") or branch), main=main
        )
    if answer.get("outcome") == "legacy" and await asyncio.to_thread(
        _existing_worktree, path, ref.repository
    ):
        return path, worktree
    return fallback(f"{answer.get('outcome') or 'no outcome'}: {answer.get('detail') or ''}")


async def release_work_worktrees(app: Any, work_id: str) -> None:
    """The Work finished: ask the runtime to give back the worktrees it claimed (#3274).

    Best effort. The runtime keeps a dirty, unpushed or busy one with its reason and retries
    it; this never fails the Finish that called it.
    """
    client = getattr(app.state, "runtime_http", None)
    if client is None:
        return
    try:
        response = await client.post(RELEASE_PATH, json={"work_id": work_id})
        answer = response.json() if response.content else None
    except Exception:  # noqa: BLE001 - the Finish stands; the claim stays to release later
        log.warning("work finish: could not release %s's worktrees", work_id, exc_info=True)
        return
    for item in answer.get("released", ()) if isinstance(answer, dict) else ():
        if isinstance(item, dict) and not item.get("released"):
            log.info("work finish: %s kept: %s", item.get("path"), item.get("pending"))


def checkout_candidates(app: Any) -> list[str]:
    """Folders on this host that may be a checkout: the Poller overlays' checkouts first, then
    the folders the side chat's start reads."""
    found = _overlay_checkouts(app.state.config.poller.root)
    found += [
        app.state.config.inject.new_chat_default_cwd or "",
        app.state.db.load_manual_new_chat_cwd() or "",
    ]
    found.extend(sorted({str(item.cwd) for item in app.state.app_state.all_sessions() if item.cwd}))
    return found


async def pull_request_clones(app: Any, pulls: Iterable[Any], covered: Iterable[str]) -> list[str]:
    """A local clone of each pull request's repository that ``covered`` does not name.

    When the issue is on one repository and the code in another, nothing being read is a
    checkout of the pull request's repository, so its branch's worktree is looked for from
    a clone of it (#3129). The Work page and a chat's diff pane both need this (#3295).
    """
    have = {repository.lower() for repository in covered if repository}
    missing = {
        pull.repository.lower()
        for pull in pulls
        if pull.repository and pull.head_branch and pull.repository.lower() not in have
    }
    if not missing:
        return []
    candidates = checkout_candidates(app)
    clones = []
    for repository in sorted(missing):
        clone = await asyncio.to_thread(local_repository_checkout, repository, candidates)
        if clone:
            clones.append(clone)
    return clones


async def start_work_agent(
    app: Any,
    repository: str,
    number: int,
    *,
    wait: bool = False,
    on_refused: Callable[[], object] | None = None,
    labels: Iterable[str] = (),
) -> str:
    """Start the Work's agent for ``repository#number`` once; say what happened.

    ``wait`` first waits up to ``START_SPACING_S`` for the account's start in progress, for a
    caller that may take its time (the backstop's pass), never the router's delivery loop.
    ``on_refused`` is called when a ``started`` start is later refused cleanly by the runtime
    (``at_capacity``), so the caller can try again sooner than the backstop. ``labels`` are
    the issue's labels as the caller read them, for the spec-only brief.
    """
    from . import work_actions
    from .providers import PROVIDERS

    ref = WorkRef(repository, number)
    work_id = ref.work_id
    action_id = start_action_id(work_id)
    db = app.state.control_db
    account = start_account(app, repository)
    if account is None:
        log.warning("work start: no Claude account may start a chat for %s", work_id)
        return "no account"
    folder = await asyncio.to_thread(local_repository_checkout, ref, checkout_candidates(app))
    if not folder:
        log.warning("work start: no checkout of %s on this host for %s", repository, work_id)
        return "no checkout"
    provider = PROVIDERS[account.provider_id]
    # A start refused before anything ran is not spent: free it, so this try can be the start.
    db.release_failed_work_action(action_id)
    if db.work_action(action_id) is not None:
        return "already started"  # spent: no wait, no second start
    if wait:
        await app.state.injector.wait_for_new_start(account.key, START_SPACING_S)
    reserved = db.reserve_work_action(action_id, work_id, work_actions.NEW_CHAT)
    if not reserved:
        # Spent (``False``) or unrecordable (``None``): either way, never a second start.
        return "already started" if reserved is False else "not recorded"
    # A Work spare an open New work form kept for this repository takes it warm (#3590).
    from .web.start_spares import WORK

    spares = getattr(app.state, "start_spares", None)
    taken = spares.claim_work(repository, account_key=account.key) if spares else None
    token = taken[0] if taken is not None else action_id
    if taken is not None:
        db.claim_start_spare(action_id, taken[0], taken[1])

    def undo_links() -> None:
        db.forget_start_spare_claim(action_id)
        db.delete_work_side_chat(token)
        if taken is not None:
            spares.release(WORK)

    if not db.record_work_side_chat(token, work_id, []):
        db.settle_work_action(action_id, "failed", "the deck could not save its Work link")
        undo_links()
        return "not linked"
    # After the reservation, so a replayed delivery never claims while a start is under way.
    timing = StartTiming(time.monotonic(), warm=taken is not None)
    if taken is not None:
        folder, where = await adopt_spare_worktree(app, ref, folder, taken[2])
    else:
        folder, where = await work_worktree(app, ref, folder)
    timing.mark("worktree")
    message = first_message(
        IssueRef(repository, number), (), _brief(app, ref, FIRST_TEXT, labels, where)
    )
    try:
        result = await app.state.injector.start_new(
            account,
            provider,
            Path(folder),
            message,
            client_action_id=action_id,
            session_token=token,
            origin="work",
            role="work",
            **({"worker_key": taken[1]} if taken is not None else {}),
        )
    except Exception:  # noqa: BLE001 - the spawn may have begun; never repeat it
        log.exception("work start: %s did not report its start", work_id)
        if taken is not None:
            spares.release(WORK)
        return "uncertain"
    if not result.accepted:
        _settle_refused(db, work_id, action_id, result.reason)
        undo_links()
        return f"refused: {result.reason or 'the start was refused'}"
    # The runtime's answer (``at_capacity`` ...) comes from the queued spawn; wait for it off
    # the caller, so the router's delivery loop never waits behind a start (#2984). One task
    # every waiter shares: the settle, and the next spare when this start took one.
    answer = asyncio.ensure_future(app.state.injector.new_start_answer(account.key))
    if taken is not None:
        spares.replenish_after(WORK, answer)

    def refused_later() -> None:
        db.forget_start_spare_claim(action_id)
        db.delete_work_side_chat(token)
        if on_refused is not None:
            on_refused()

    timing.mark("queued")
    _settle_in_background(
        db, work_id, action_id, folder, answer, refused_later, _announcer(ref, action_id), timing
    )
    log.info("work start: %s started in %s on %s", work_id, folder, account.key)
    return "started"


def _brief(app: Any, ref: WorkRef, opening: str, labels: Iterable[str], where: str) -> str:
    """A deck start's brief after its opening line: spec-only, merging, evidence, folder."""
    return " ".join(
        filter(
            None,
            (
                opening,
                _spec_only_text(app, ref.repository, ref.number, labels),
                _merge_text(app, ref.repository),
                _evidence_text(app, ref.repository, ref.number),
                _open_pull_text(app, ref.repository, ref.number),
                where,
            ),
        )
    )


def _settle_in_background(
    db: Any,
    work_id: str,
    action_id: str,
    folder: str,
    answer: Awaitable[Any],
    on_refused: Callable[[], object] | None,
    announce: Callable[[], Awaitable[None]],
    timing: StartTiming,
) -> None:
    task = asyncio.create_task(
        _settle_when_answered(
            db, work_id, action_id, folder, answer, on_refused, announce=announce, timing=timing
        )
    )
    _SETTLING.add(task)
    task.add_done_callback(_SETTLING.discard)


@dataclass
class StartTiming:
    """One start's phases, logged as one line once the runtime names its session (#3590 D16).

    Every mark is seconds since the press (an early start) or since the start began (a label
    or backstop start). ``session`` is when the runtime answered with the chat's session: its
    transcript exists from then, and the agent's first action follows in it.
    """

    began: float
    press: str = ""
    warm: bool = False
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str) -> None:
        self.marks[name] = time.monotonic() - self.began

    def line(self, work_id: str) -> str:
        phases = " ".join(f"{name}={value:.1f}s" for name, value in self.marks.items())
        press = f" press={self.press}" if self.press else ""
        return f"{work_id}{press} {'warm' if self.warm else 'cold'} {phases}"


# --- starting before the issue exists (#3590) -----------------------------------------------


@dataclass(frozen=True)
class EarlyStart:
    """An agent started for a New work press before its issue existed (#3590)."""

    account: Any
    token: str
    key: str
    folder: str
    main: str
    action_id: str
    answer: Awaitable[Any]
    warm: bool
    timing: StartTiming


def make_spare_worktree(main: str) -> str:
    """A new ``.worktrees/work-spare-<hex>`` of ``main``, detached at a fresh base; or "".

    Runs Git, so callers keep it off the event loop. Sweeps the checkout's old untaken spares
    first, so one a restart or a refused filing left behind does not stay forever.
    """
    base = default_branch(main)
    if not base:
        log.warning("work spare: the default branch of %s could not be read", main)
        return ""
    exclude_worktrees(main)
    sweep_spare_worktrees(main)
    path = str(Path(main) / ".worktrees" / f"{SPARE_PREFIX}{uuid.uuid4().hex[:8]}")
    error = GitHost().create_spare(main, path, base)
    if error:
        log.warning("work spare: could not make %s: %s", path, error)
        return ""
    return path


def remove_spare_worktree(path: str) -> None:
    """Remove a spare worktree no start took: only clean and still detached (#3590)."""
    host = GitHost()
    if not Path(path).exists():
        return
    if host.current_branch(path) or host.changed(path):
        log.info("work spare: %s is in use now; kept", path)
        return
    removed, why = host.remove(path)
    if not removed:
        log.warning("work spare: could not remove %s: %s", path, why)


def sweep_spare_worktrees(main: str, *, now: float | None = None) -> None:
    """Remove ``main``'s spare worktrees untouched for ``SPARE_SWEEP_AGE_S``, never one in use.

    An adopted spare is on its Work's branch, and one an agent changed is dirty: both stay.
    """
    now = time.time() if now is None else now
    try:
        spares = sorted((Path(main) / ".worktrees").glob(f"{SPARE_PREFIX}*"))
    except OSError:
        return
    for path in spares:
        try:
            old = now - path.stat().st_mtime > SPARE_SWEEP_AGE_S
        except OSError:
            continue
        if old and not path.is_symlink():
            remove_spare_worktree(str(path))


def early_action_id(press: str) -> str:
    """The client action id of a New work press's early start: one per press."""
    return "work-early-" + hashlib.sha256(press.encode()).hexdigest()[:20]


async def start_before_issue(
    app: Any, repository: str, *, request: str, press: str
) -> EarlyStart | None:
    """Start the Work's agent for a New work press now, before its issue exists (#3590).

    ``request`` is the issue's body as it will be filed. ``None`` when nothing started (no
    account, no checkout, the start refused): the Work then starts the usual way once the
    issue exists. Called once per reserved press, so it never starts twice.
    """
    from .providers import PROVIDERS
    from .web.start_spares import WORK

    timing = StartTiming(time.monotonic(), press=press)
    account = start_account(app, repository)
    if account is None:
        return None
    spares = getattr(app.state, "start_spares", None)
    taken = spares.claim_work(repository, account_key=account.key) if spares else None
    timing.warm = taken is not None
    main = await asyncio.to_thread(local_repository_checkout, repository, checkout_candidates(app))
    if taken is not None:
        token, key, folder = taken
    else:
        folder = await asyncio.to_thread(make_spare_worktree, main) if main else ""
        token, key = uuid.uuid4().hex, f"chat-{uuid.uuid4().hex[:12]}"
    timing.mark("worktree")
    if not main or not folder:
        if taken is not None:
            spares.release(WORK)
        return None
    action_id = early_action_id(press)
    opening = BEFORE_ISSUE_TEXT.format(repository=repository)
    message = f"Context: new Work in {repository}.\n\n{opening}\n\n{request.strip()}"
    try:
        result = await app.state.injector.start_new(
            account,
            PROVIDERS[account.provider_id],
            Path(folder),
            message,
            client_action_id=action_id,
            session_token=token,
            origin="work",
            role="work",
            worker_key=key,
        )
    except Exception:  # noqa: BLE001 - it may have begun: never start it again
        log.exception("work start: the early start in %s did not report", repository)
        if taken is not None:
            spares.release(WORK)
        return None
    if not result.accepted:
        log.info("work start: no early start in %s: %s", repository, result.reason)
        if taken is not None:
            spares.release(WORK)
        else:
            await asyncio.to_thread(remove_spare_worktree, folder)
        return None
    # One task every waiter shares: the next spare, the bind, and the settle after it.
    answer = asyncio.ensure_future(app.state.injector.new_start_answer(account.key))
    if taken is not None:
        spares.replenish_after(WORK, answer)
    timing.mark("queued")
    log.info("work start: early start in %s on %s", repository, account.key)
    return EarlyStart(
        account, token, key, folder, main, action_id, answer, taken is not None, timing
    )


async def bind_before_issue(
    app: Any,
    early: EarlyStart,
    ref: WorkRef,
    *,
    url: str,
    labels: Iterable[str] = (),
    on_refused: Callable[[], object] | None = None,
    reserved: bool = False,
    filed_at: float | None = None,
) -> str:
    """The issue exists: make the early start this Work's one start, and tell its agent.

    ``reserved``: the caller already reserved the Work's start for this press the moment
    GitHub answered, so it could stop holding the router's starts at once.

    Idempotent where it writes: the Work's start id is reserved first, so a second call, or a
    router start racing it, finds the start spent. ``on_refused`` is called when the runtime
    later refuses the spawn cleanly, or when its agent could not be told its issue, so the
    caller can start the Work the usual way; the early start's links are undone first, so
    nothing (the chat link above all) keeps naming a chat that is not the Work's.
    """
    from . import work_actions
    from .providers import PROVIDERS

    work_id = ref.work_id
    action_id = start_action_id(work_id)
    db = app.state.control_db
    provider = PROVIDERS[early.account.provider_id]
    # When GitHub answered (``time.monotonic()``), which may be before the early start
    # finished queueing; now, when the caller did not say.
    early.timing.marks["filed"] = (filed_at or time.monotonic()) - early.timing.began
    if not reserved:
        reserved = db.reserve_work_action(action_id, work_id, work_actions.NEW_CHAT)
    if not reserved:
        # Only an unrecordable press or a start this press did not make: never two agents.
        log.warning("work start: %s was already started; stopping the early start", work_id)
        await stop_before_issue(app, early, "this Work already has an agent")
        return "already started" if reserved is False else "not recorded"
    db.claim_start_spare(action_id, early.token, early.key)
    if not db.record_work_side_chat(early.token, work_id, []):
        log.warning("work start: %s's chat could not be linked to its Work", work_id)

    def start_again() -> None:
        db.forget_start_spare_claim(action_id)
        db.delete_work_side_chat(early.token)
        if on_refused is not None:
            on_refused()

    folder, where = await adopt_spare_worktree(app, ref, early.main, early.folder)
    # The worker must exist before it can be told anything: a cold spawn may still be under
    # way. A start the runtime refused gets no message; its settle starts the Work again.
    if await _launched(early):
        text = _brief(app, ref, ISSUE_FILED_TEXT.format(url=url), labels, where)
        message = first_message(IssueRef(ref.repository, ref.number), (), text)
        if not await _tell(provider, early, message, work_id):
            # Told to wait for this message, it would sit idle with the Work marked started:
            # stop it and start the Work the usual way, in the same worktree claim. Only a
            # stop that landed frees the start: never a second agent beside a running one.
            if not await _interrupt(provider, early):
                db.settle_work_action(
                    action_id, "done", "its agent was neither told its issue nor stopped"
                )
                log.error(
                    "work start: %s's early agent was neither told its issue nor stopped; "
                    "its chat is left to a person",
                    work_id,
                )
                return "not told, not stopped"
            db.settle_work_action(action_id, "failed", "its agent could not be told its issue")
            start_again()
            return "not told"
    early.timing.mark("bound")
    _settle_in_background(
        db,
        work_id,
        action_id,
        folder,
        early.answer,
        start_again,
        _announcer(ref, early.token),
        early.timing,
    )
    return "started"


#: Waits between tries of telling an early start its issue; the same delivery id each time.
TELL_RETRY_S = (1.0, 3.0)


async def _interrupt(provider: Any, early: EarlyStart) -> bool:
    """Interrupt the early start's worker, retrying; whether the runtime confirmed it."""
    for wait in (0.0, *TELL_RETRY_S):
        if wait:
            await asyncio.sleep(wait)
        answer = await provider.interrupt_worker(early.account, early.key)
        if answer.accepted:
            return True
        log.warning("work start: the early start was not interrupted: %s", answer.reason)
    return False


async def _tell(provider: Any, early: EarlyStart, message: str, work_id: str) -> bool:
    """Deliver the issue message, retrying the same delivery id, which is never a duplicate."""
    for wait in (0.0, *TELL_RETRY_S):
        if wait:
            await asyncio.sleep(wait)
        sent = await provider.message_worker(
            early.account, early.key, message, delivery_id=f"{early.action_id}:issue"
        )
        if sent.accepted:
            return True
        log.warning("work start: %s's agent was not told its issue: %s", work_id, sent.reason)
    return False


async def stop_before_issue(app: Any, early: EarlyStart, why: str, *, maybe: bool = False) -> bool:
    """Tell an early start its issue was not filed (``maybe``: may not have been), and stop it.

    A refused filing interrupts the agent and leaves it the reason; an uncertain one only
    tells it to wait, since the issue may exist and a person can give it the number.
    """
    from .providers import PROVIDERS

    if not await _launched(early):
        return True  # refused before anything ran: nothing to stop or tell
    provider = PROVIDERS[early.account.provider_id]
    stopped = True if maybe else await _interrupt(provider, early)
    text = (MAYBE_FILED_TEXT if maybe else NOT_FILED_TEXT).format(why=why.rstrip("."))
    sent = await provider.message_worker(
        early.account, early.key, text, delivery_id=f"{early.action_id}:not-filed"
    )
    if not sent.accepted:
        log.warning("work start: the early start was not told: %s", sent.reason)
    if not stopped:
        log.error("work start: an early start whose issue was not filed is still running")
    return stopped


#: How long a bind or a stop waits for the early start's spawn before messaging it anyway.
LAUNCH_WAIT_S = 60.0


async def _launched(early: EarlyStart) -> bool:
    """Wait for the early start's spawn; False only when the runtime refused it cleanly."""
    try:
        result = await asyncio.wait_for(asyncio.shield(early.answer), LAUNCH_WAIT_S)
    except Exception:  # noqa: BLE001 - unknown, or slow: it may have launched
        return True
    return not (result is not None and not result.accepted and not result.session_id)


async def adopt_spare_worktree(app: Any, ref: WorkRef, main: str, folder: str) -> tuple[str, str]:
    """Have the claim service adopt the spare the agent runs in; its folder and the sentence.

    On ``agent/issue-<n>`` where it stands, with ``.worktrees/issue-<n>`` linked to it. When
    the claim cannot be made, the agent stays where it is and is told to make the branch.
    """
    branch = f"agent/issue-{ref.number}"
    usual = str(Path(main) / ".worktrees" / f"issue-{ref.number}")

    def unclaimed(why: str) -> tuple[str, str]:
        log.warning("work start: %s keeps an unclaimed spare: %s", ref.work_id, why)
        return folder, (
            f"The deck could not claim your worktree ({why}): you are in `{folder}`, detached; "
            f"run `git switch -c {branch}` there before editing anything, and never edit the "
            f"main checkout `{main}`."
        )

    base = await asyncio.to_thread(default_branch, main)
    client = getattr(app.state, "runtime_http", None)
    if not base or client is None:
        return unclaimed("the base branch or the runtime could not be reached")
    body = {
        "work_id": work_id_for(IssueRef(ref.repository, ref.number)),
        "repository": ref.repository.lower(),
        "purpose": "implementation",
        "checkout": main,
        "path": folder,
        "branch": branch,
        "base_ref": base,
        "legacy_paths": [],
        "spare": True,
        "link": usual,
    }
    try:
        response = await client.post(ENSURE_PATH, json=body, timeout=ENSURE_TIMEOUT_S)
        answer = response.json() if response.content else None
    except Exception as exc:  # noqa: BLE001 - the agent keeps working either way
        return unclaimed(f"the runtime did not answer: {type(exc).__name__}")
    claim = answer.get("claim") if isinstance(answer, dict) else None
    if response.status_code != 200 or not isinstance(claim, dict):
        detail = answer.get("detail") if isinstance(answer, dict) else ""
        return unclaimed(str(detail or f"the runtime answered {response.status_code}"))
    claimed = str(claim.get("path") or folder)
    return claimed, (
        f"You are in this Work's own worktree `{claimed}` (also `{usual}`) on "
        f"`{claim.get('branch') or branch}`; never edit the main checkout `{main}`."
    )


#: Background settles in flight, held so none is collected before it ends.
_SETTLING: set[asyncio.Task] = set()


def _announcer(ref: WorkRef, action_id: str) -> Callable[[], Awaitable[None]]:
    """Tell the issue its Work's agent started: 🚀 and the Work page's public link."""
    from .public_origin import public_origin
    from .work_overview import work_href
    from .work_signals import announce_start

    def announce() -> Awaitable[None]:
        origin = public_origin()
        work_url = f"{origin}{work_href(ref.work_id)}" if origin else ""
        return announce_start(ref.repository, ref.number, action_id=action_id, work_url=work_url)

    return announce


def _settle_refused(db: Any, work_id: str, action_id: str, reason: str | None) -> None:
    reason = reason or "the start was refused"
    db.settle_work_action(action_id, "failed", reason)
    db.delete_work_side_chat(action_id)
    log.warning("work start: %s refused: %s", work_id, reason)


async def _settle_when_answered(
    db: Any,
    work_id: str,
    action_id: str,
    folder: str,
    answer: Awaitable[Any],
    on_refused: Callable[[], object] | None = None,
    *,
    announce: Callable[[], Awaitable[None]] | None = None,
    timing: StartTiming | None = None,
) -> None:
    """Settle the start once the runtime answered: ``failed`` only for a clean refusal (no
    session, so nothing launched); anything else, unknown included, may have launched."""
    try:
        result = await answer
    except Exception:  # noqa: BLE001 - unknown: it may have launched
        log.exception("work start: %s had no answer from the runtime", work_id)
        result = None
    if timing is not None:
        timing.mark("session")
        log.info("work start timing: %s", timing.line(work_id))
    if result is not None and not result.accepted and not result.session_id:
        _settle_refused(db, work_id, action_id, result.reason)
        if on_refused is not None:
            try:
                on_refused()
            except Exception:  # noqa: BLE001 - the backstop still retries it
                log.exception("work start: %s could not be put back for a retry", work_id)
        return
    db.settle_work_action(action_id, "done", f"Started the Work's agent in {folder}")
    if announce is not None:
        try:
            await announce()
        except Exception:  # noqa: BLE001 - the sign is best effort; the start stands
            log.warning("work start: could not tell %s's issue", work_id, exc_info=True)
