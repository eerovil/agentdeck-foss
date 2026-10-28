"""A deck-started Claude or Codex reviewer for each new ready head of a Work (#3640).

The Work router asks one function to start the review of a pull request's new ready head
(``start_review``). ``[review] reviewer`` picks who does it: ``chatgpt`` is the ChatGPT review
chat, unchanged, and ``claude`` or ``codex`` is this module. It ships in both editions, so the
public copy, which has no ChatGPT review chats, can switch a reviewer on, and the private deck
can pick one too (owner, 2026-10-28: one system for both).

One review is:

* **A throwaway checkout of the head** under the repository's own ``.worktrees/``, made from
  the checkout of the Work's chat (owner's choice: the agent keeps editing its own checkout
  while the review runs). It is removed once its reviewer has stopped.
* **A deck-owned chat in it**, started the way the New chat box starts one, so it runs in the
  persistent runtime. It runs the bundled ``rate-my-pr`` skill once, read-only.
* **The verdict and findings through ``agentdeck deck-request``**, as the stand-in reviewer's
  do: ``/reviewed pass <head> <digest>`` or ``/reviewed changes-needed <head>`` reaches the
  Work's release, and ``/claude Review of <pull> at <head>`` findings reach the Work's chat.
  Nothing new decides a merge; the release checks head and digest as for any reviewer.

The router keeps its own bookkeeping (one review per head, asks again, no-verdict retries).
At most ``max_reviewers`` run at once host-wide: a start over the cap raises ``ReviewBusy``,
which the router keeps pending and asks again after its back-off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .review_refs import ReviewBusy, ReviewError, _read_json

log = logging.getLogger(__name__)

CHATGPT = "chatgpt"
CLAUDE = "claude"
CODEX = "codex"
OFF = "off"
#: The provider each agent reviewer runs on, and its name in the brief.
PROVIDERS = {CLAUDE: "claude_code", CODEX: "codex"}
NAMES = {CLAUDE: "Claude", CODEX: "Codex"}
#: Claude reviews on Opus, as the stand-in reviewer does; Codex on its account's default.
CLAUDE_MODEL = "opus"
#: A start whose session the deck cannot see yet still holds its place this long.
SPAWN_GRACE_S = 5 * 60.0
#: A start this young holds its place whatever its session says: a chat just spawned can read
#: as idle for a moment before its first turn, and its checkout must not be removed under it.
START_FLOOR_S = 60.0
#: A reviewer still running after this is taken as stuck: its place and checkout are freed.
MAX_AGE_S = 3 * 3600.0
#: How many chats' directories a start asks git about, the Work chat's first.
MAX_CANDIDATES = 40
DECK_REQUEST = "~/agentdeck/.venv/bin/agentdeck deck-request"
GIT_TIMEOUT_S = 300.0

# How every reviewer's brief opens, and so how the chat list knows a reviewer's chat to leave
# out (#3101). The stand-in reviewer's briefs open the same way.
_BRIEF_OPENING = re.compile(r"You are AgentDeck's \w+ reviewer\.")
_PULL_RE = re.compile(r"^(?P<repository>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(?P<number>\d+)$")


# Codex here runs at most three sub-reviewers at once (`[agents] max_threads = 3` in its
# config), and a `high` review asks for nine lanes: spawns were refused, lanes replaced or
# cut short, and passes ended inline (#3602). Claude has no such limit. Every Codex reviewer's
# brief carries it, the stand-in reviewer's too.
CODEX_LANES = (
    "- Codex on this host runs at most three sub-reviewers at once (`[agents] max_threads = "
    "3` in its config). Start the review's lanes in waves of up to three, and start the next "
    "lane only when one has returned. If a spawn is still refused, run that lane inline and "
    "say in the closing note that it ran inline.\n"
)


def is_reviewer_prompt(prompt: str | None) -> bool:
    """Whether a chat's first message is a reviewer's brief."""
    return bool(prompt) and _BRIEF_OPENING.match(prompt.lstrip()) is not None


def effective(reviewer: str, *, review_chats: bool) -> str:
    """The reviewer that actually runs: ``chatgpt`` needs review chats, else it is ``off``."""
    if reviewer == CHATGPT and not review_chats:
        return OFF
    return reviewer if reviewer in (CHATGPT, CLAUDE, CODEX) else OFF


@dataclass(frozen=True)
class Started:
    """What a start answers the router with: the heads it asked about (``work_router.covers``)."""

    heads: tuple[tuple[str, str], ...]
    summary: str


def brief(
    *, name: str, pull: str, issue: str, head: str, base: str, digest: str, note: str = ""
) -> str:
    """The first message of one reviewer's chat."""
    again = (
        f"\nThis head was reviewed before; the Work's agent asked for another review: {note}\n"
        if note
        else ""
    )
    lanes = CODEX_LANES if name == NAMES[CODEX] else ""
    return f"""You are AgentDeck's {name} reviewer. The deck started you to review pull request \
{pull} at commit `{head}` for the Work {issue}. This directory is a throwaway checkout of that \
commit, made for you; the deck removes it when you stop.
{again}
- First check that `git rev-parse HEAD` is `{head}`. If it is not, stop and say so.
- Run the installed `rate-my-pr` skill exactly once on this commit against its base: \
`git diff origin/{base}...{head}`. Follow the repository's root guide (`AGENTS.md`, or \
`CLAUDE.md` where there is none). This review is read-only: do not edit, commit or push, do \
not merge or deploy, and do not comment on GitHub.
{lanes}- Then report through the deck, which hands it to the Work's agent and to its release. \
Write each message to a file under `/var/tmp` and send it with
    `{DECK_REQUEST} {issue} --body-file <file>`
  with a 10-minute timeout, never in the background. The deck's answer is the delivery. If the \
command fails or its answer is unclear, run the same command once more: a repeat is the same \
request.
  - If anything must change before this merges, send the findings first, as one message whose \
first line is exactly `/claude Review of {pull} at {head}` and whose next lines are the \
findings, each with its file, line and why. Then send the verdict \
`/reviewed changes-needed {head} {pull}`.
  - If nothing must change, send only the verdict `/reviewed pass {head} {digest} {pull}`.
  Send exactly one verdict.
- Finish by saying in a few sentences what you found and why you gave that verdict.
"""


# ``start(kind, cwd, text, token) -> (accepted, why)``: launch one reviewer chat.
Starter = Callable[[str, Path, str, str], Awaitable[tuple[bool, str]]]
# ``alive(token)``: True while it runs, False once it stopped, None while the deck cannot tell.
Alive = Callable[[str], "bool | None"]
# ``checkout_of(pull)``: directories on this host that may be checkouts of the pull request's
# repository, likeliest first (the Work chat's own). ``_checkout`` takes the first whose
# ``origin`` is that repository.
CheckoutOf = Callable[[str], "list[Path]"]
# ``digest(repository, number, head) -> (digest, why)``: ``review_refs.target_digest``.
Digest = Callable[[str, int, str], Awaitable[tuple[str, str]]]
# ``read_pull(repository, number)``: the pull request's state, draft flag, head and base.
ReadPull = Callable[[str, int], Awaitable["dict | None"]]


async def read_pull(repository: str, number: int) -> dict | None:
    return await _read_json(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repository,
            "--json",
            "number,state,isDraft,headRefOid,baseRefName",
        ],
        f"{repository}#{number}",
    )


async def git(*args: str, cwd: Path) -> tuple[int, str]:
    """Run git; ``(exit code, output)``. Never raises for a failing command."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timed out"
    return proc.returncode or 0, out.decode(errors="replace").strip()


Git = Callable[..., Awaitable[tuple[int, str]]]


class AgentReviewer:
    """Starts one Claude or Codex review per asked-for head, at most ``max_reviewers`` at once."""

    def __init__(
        self,
        *,
        kind: str,
        max_reviewers: int,
        ledger: Path,
        start: Starter,
        alive: Alive,
        checkout_of: CheckoutOf,
        digest: Digest,
        read_pull: ReadPull = read_pull,
        git: Git = git,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if kind not in PROVIDERS:
            raise ValueError(f"no agent reviewer {kind!r}")
        self.kind = kind
        self._max = max_reviewers
        self._ledger = ledger
        self._start = start
        self._alive = alive
        self._checkout_of = checkout_of
        self._digest = digest
        self._read_pull = read_pull
        self._git = git
        self._clock = clock
        self._lock = asyncio.Lock()

    # --- the ledger: one line per reviewer started, until its checkout is removed ---

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self._ledger.read_text())
        except (OSError, ValueError):
            return []
        return [line for line in data if isinstance(line, dict)] if isinstance(data, list) else []

    def _write(self, lines: list[dict]) -> None:
        self._ledger.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._ledger.with_suffix(".tmp")
        temporary.write_text(json.dumps(lines, indent=1))
        temporary.replace(self._ledger)

    def _running(self, line: dict, now: float) -> bool:
        age = now - float(line.get("started") or 0)
        if age > MAX_AGE_S:
            return False
        if age < START_FLOOR_S:
            return True
        alive = self._alive(str(line.get("token") or ""))
        if alive is None:
            return age < SPAWN_GRACE_S
        return alive

    async def sweep(self) -> list[dict]:
        """Drop every reviewer that stopped, removing its checkout; the ones still running."""
        now = self._clock()
        kept, done = [], []
        for line in self._read():
            (kept if self._running(line, now) else done).append(line)
        for line in done:
            await self._remove(line)
        if done:
            self._write(kept)
        return kept

    async def _remove(self, line: dict) -> None:
        path, root = line.get("path"), line.get("root")
        if not path or not root:
            return
        code, out = await self._git("worktree", "remove", "--force", path, cwd=Path(root))
        if code and Path(path).exists():
            log.warning("agent review: could not remove %s: %s", path, out)

    # --- one start ---

    async def start(self, pull: str, work: str, *, note: str = "", again: bool = False) -> Started:
        """Start the review of ``pull``'s ready head for ``work``; the router's ``start_review``.

        Raises ``ReviewBusy`` when every place is taken and ``ReviewError`` when nothing was
        started and asking again later may work; both leave the head pending in the router.
        """
        from .work import WorkRef

        target = _PULL_RE.match(pull)
        ref = WorkRef.from_work_id(work)
        if target is None or ref is None:
            raise ReviewError(f"cannot review {pull} for {work!r}")
        repository, number = target.group("repository"), int(target.group("number"))
        issue = f"{ref.repository}#{ref.number}"
        async with self._lock:
            running = await self.sweep()
            facts = await self._read_pull(repository, number) or {}
            head = str(facts.get("headRefOid") or "").lower()
            base = str(facts.get("baseRefName") or "")
            if str(facts.get("state") or "").upper() != "OPEN" or facts.get("isDraft") or not head:
                raise ReviewError(f"{pull} has no ready head to review")
            if not again and any(
                line.get("pull") == pull.lower() and line.get("head") == head for line in running
            ):
                return Started(((pull, head),), f"a reviewer already reviews {pull} at {head[:7]}")
            if len(running) >= self._max:
                raise ReviewBusy(f"{len(running)} agent reviews already run (the cap)")
            digest, why = await self._digest(repository, number, head)
            if not digest:
                raise ReviewError(f"the diff of {pull} at {head[:7]} could not be read ({why})")
            token = uuid.uuid4().hex
            path, root = await self._checkout(pull, repository, number, head, base, token)
            self._write([*self._read(), _line(token, pull, head, path, root, self._clock())])
            name = NAMES[self.kind]
            text = brief(
                name=name, pull=pull, issue=issue, head=head, base=base, digest=digest, note=note
            )
            try:
                accepted, refusal = await self._start(self.kind, path, text, token)
            except Exception as exc:  # noqa: BLE001 - nothing started: give the place back
                accepted, refusal = False, str(exc) or type(exc).__name__
            if not accepted:
                await self._remove({"path": str(path), "root": str(root)})
                self._write([line for line in self._read() if line.get("token") != token])
                raise ReviewError(f"the {name} reviewer did not start: {refusal}")
        log.info("agent review: a %s reviewer started on %s at %s", name, pull, head[:12])
        return Started(((pull, head),), f"a {name} reviewer started on {pull} at {head[:7]}")

    async def _root_of(self, repository: str, pull: str = "") -> Path:
        """The main checkout of ``repository``: the first candidate whose ``origin`` is it.

        A Work's pull request can live in another repository than its chat's checkout, so a
        candidate is never taken on trust: fetching ``pull/<n>/head`` from the wrong origin
        would check out some other pull request, or nothing.
        """
        seen: list[str] = []
        for candidate in self._checkout_of(pull or repository):
            code, url = await self._git("remote", "get-url", "origin", cwd=candidate)
            if code:
                continue
            named = origin_repository(url)
            if named != repository.lower():
                seen.append(f"{candidate} is {named or url}")
                continue
            code, common = await self._git(
                "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=candidate
            )
            if not code:
                return Path(common).parent
        why = f" ({'; '.join(seen)})" if seen else ""
        raise ReviewError(f"no checkout of {repository} on this host to review from{why}")

    async def _checkout(
        self, pull: str, repository: str, number: int, head: str, base: str, token: str
    ) -> tuple[Path, Path]:
        """A detached checkout of ``head`` under the main checkout's ``.worktrees/``."""
        root = await self._root_of(repository, pull)
        path = root / ".worktrees" / f"review-{number}-{head[:12]}-{token[:6]}"
        refs = [f"pull/{number}/head"] + ([base] if base else [])
        code, out = await self._git("fetch", "-q", "origin", *refs, cwd=root)
        if code:
            raise ReviewError(f"could not fetch {pull}: {out}")
        if base:
            # ``origin/<base>`` is what the brief diffs against; a plain fetch of a named ref
            # updates it only where the remote's refspec covers it, so set it outright.
            await self._git(
                "fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}", cwd=root
            )
        code, out = await self._git("worktree", "add", "--detach", str(path), head, cwd=root)
        if code:
            raise ReviewError(f"could not check out {pull} at {head[:7]}: {out}")
        return path, root


_ORIGIN_RE = re.compile(r"github\.com[:/]+(?P<repository>[^/\s]+/[^/\s]+?)(?:\.git)?/?$")


def origin_repository(url: str) -> str:
    """``owner/repo`` (lower case) of a GitHub remote URL, or ``""`` for anything else."""
    found = _ORIGIN_RE.search((url or "").strip())
    return found.group("repository").lower() if found else ""


def _line(token: str, pull: str, head: str, path: Path, root: Path, now: float) -> dict:
    return {
        "token": token,
        "pull": pull.lower(),
        "head": head,
        "path": str(path),
        "root": str(root),
        "started": now,
    }


def deck_hooks(config, *, injector, state, accounts, providers) -> dict:
    """``start``, ``alive`` and ``checkout_of`` for ``AgentReviewer``, over the deck's own parts."""
    from .work_router import owner_of_pull

    def account_for(kind: str):
        provider_id = PROVIDERS[kind]
        chosen = config.review.account.strip()
        for account in accounts:
            if account.provider_id == provider_id and (not chosen or account.key == chosen):
                return account
        return None

    async def start(kind: str, cwd: Path, text: str, token: str) -> tuple[bool, str]:
        account = account_for(kind)
        if account is None:
            wanted = config.review.account.strip() or f"a {NAMES[kind]} account"
            return False, f"no {wanted} on this deck"
        provider = providers.get(account.provider_id)
        if provider is None:
            return False, f"no provider for {account.key}"
        result = await injector.start_new(
            account,
            provider,
            cwd,
            text,
            model=CLAUDE_MODEL if kind == CLAUDE else None,
            session_token=token,
        )
        return bool(result.accepted), str(result.reason or "")

    def alive(token: str) -> bool | None:
        key = state.session_key_for_token(token)
        if key:
            session = state.sessions.get(key)
            if session is None:
                return None
            # ``thinking`` is the reviewer's own turn only: its rate-my-pr lanes run as
            # sub-agents and child chats after that turn ends. The deck's display copy
            # counts that descendant work too, so the checkout is not removed under them.
            return bool(state.session_presentation().display(session).thinking)
        started = injector.started_chat(token)
        if started is not None and started.status.state == "failed":
            return False
        return None

    def checkout_of(pull: str) -> list[Path]:
        # The Work chat's own checkout first, then every other chat's: one of them may be a
        # checkout of the pull request's repository when the Work's chat is in another.
        repository, _, number = pull.rpartition("#")
        owner = owner_of_pull(state, repository, int(number)) if number.isdigit() else None
        chat = state.chat_by_id(owner[0]) if owner else None
        session = state.current_session_for_chat(chat) if chat is not None else None
        found: list[Path] = []
        for cwd in [getattr(session, "cwd", None)] + [
            getattr(other, "cwd", None) for other in list(state.sessions.values())
        ]:
            if cwd and Path(cwd) not in found and Path(cwd).is_dir():
                found.append(Path(cwd))
        return found[:MAX_CANDIDATES]

    return {"start": start, "alive": alive, "checkout_of": checkout_of}
