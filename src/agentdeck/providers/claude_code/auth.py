"""Claude account authentication: is this login alive, and how do you fix it (#298).

When an account's OAuth expired, the only sign was a *delegation that reached
``complete``* whose final message read ``Failed to authenticate: OAuth session
expired and could not be refreshed``. Nothing in the deck said the account was
broken, and recovery meant leaving AgentDeck for a terminal: ``claude auth login``,
copy the URL out of the output, paste the code back, then test the account by hand.

Two capabilities live here, both per-account (``CLAUDE_CONFIG_DIR`` is what makes
an account an account):

* **Is it alive** — ``claude auth status --json`` answers authoritatively, without
  spending tokens, and independently of whatever a session's text happens to say.
  That answer is what the deck shows, so a broken login is visible before anyone
  delegates into it.
* **Fixing it** — ``claude auth login --claudeai`` prints an authorization URL and
  then waits on stdin for the code the browser hands back. Both halves are
  reachable from the deck: the URL is shown, the code is posted back, and the flow
  finishes by re-reading the status rather than by trusting the exit code.

The exact CLI behaviour was observed rather than assumed (claude 2.1.220):

    $ CLAUDE_CONFIG_DIR=… claude auth login --claudeai
    Opening browser to sign in…
    If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?…
    Paste code here if prompted >

Nothing here logs, stores or renders the pasted code, the tokens it becomes, or the
contents of ``.credentials.json``. The authorize URL is shown deliberately: it is a
PKCE request the user has to open, and it grants nothing on its own. The status
JSON's email/organization *are* shown — that is how you know which account you are
looking at, and neither is a credential.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATUS_TIMEOUT_S = 15.0
# Long enough for the CLI to reach its OAuth endpoint and print the URL; short
# enough that a hung login does not leave the operator staring at a spinner.
URL_TIMEOUT_S = 30.0
CODE_TIMEOUT_S = 60.0

# What a broken login looks like from the outside: the CLI's own wording, seen on a
# delegation that otherwise reported success. Matched case-insensitively on the text
# a session ended with, so a *report* about authentication ("the auth flow failed
# for the user") does not masquerade as one.
_FAILURE_PATTERNS = (
    re.compile(r"failed to authenticate", re.I),
    re.compile(r"oauth (?:session|token) (?:has )?expired", re.I),
    re.compile(r"(?:could not|couldn't) be refreshed", re.I),
    re.compile(r"please run .{0,20}claude (?:auth )?login", re.I),
    re.compile(r"invalid api key", re.I),
)

_URL_RE = re.compile(r"https://\S*/oauth/\S+")


def looks_like_auth_failure(text: str | None) -> bool:
    """Whether a session's final text is an authentication failure rather than an answer.

    Two of these together are ordinary prose; on their own they are the CLI's error
    strings. The check is deliberately anchored to the *end* of a session — a long
    transcript quoting an error is not the same as a turn that produced one — and the
    caller decides what "final" means.
    """
    if not text:
        return False
    candidate = text.strip()
    if not candidate or len(candidate) > 2000:
        # A real failure message is short. A wall of text that happens to contain the
        # words is a session talking *about* authentication, which is not a failure.
        return False
    return any(pattern.search(candidate) for pattern in _FAILURE_PATTERNS)


@dataclass(frozen=True)
class AuthStatus:
    """What ``claude auth status`` said about one account, and when.

    ``logged_in is None`` means the question could not be answered — no ``claude`` on
    PATH, a timeout, unreadable output. That is not the same as a broken login and is
    never rendered as one.
    """

    logged_in: bool | None
    email: str | None = None
    organization: str | None = None
    method: str | None = None
    subscription: str | None = None
    detail: str = ""
    checked_at: datetime | None = None

    @property
    def known(self) -> bool:
        return self.logged_in is not None

    @property
    def broken(self) -> bool:
        return self.logged_in is False


def _run(command: list[str], *, env: dict[str, str], timeout: float):
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False, env=env
    )


def _account_env(config_dir: Path) -> dict[str, str]:
    """The environment that makes the CLI act on *this* account and nothing else."""
    return {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        # The deck may be reached from a phone; a browser opened on the host helps
        # nobody and can block. The URL is what the operator needs, so make the
        # CLI's "open a browser" step a no-op and print it instead.
        "BROWSER": "true",
    }


def read_status(
    config_dir: Path, *, binary: str | None = None, runner=_run, now=None
) -> AuthStatus:
    """Ask the CLI whether this account is signed in. Never raises."""
    checked_at = (now or (lambda: datetime.now(UTC)))()
    claude = binary or shutil.which("claude")
    if claude is None:
        return AuthStatus(None, detail="claude is not on PATH", checked_at=checked_at)
    try:
        done = runner(
            [claude, "auth", "status", "--json"],
            env=_account_env(config_dir),
            timeout=STATUS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return AuthStatus(None, detail=f"could not run claude: {exc}", checked_at=checked_at)
    try:
        payload = json.loads(done.stdout or "")
    except ValueError:
        # A non-zero exit with unparseable output is an unknown, not a denial: the
        # difference between "signed out" and "we could not tell" matters to whoever
        # is deciding whether to log in again.
        return AuthStatus(
            None,
            detail=(done.stderr or done.stdout or "no answer from claude auth status").strip()[
                :200
            ],
            checked_at=checked_at,
        )
    if not isinstance(payload, dict):
        return AuthStatus(None, detail="unexpected auth status payload", checked_at=checked_at)
    logged_in = payload.get("loggedIn")
    return AuthStatus(
        logged_in=bool(logged_in) if isinstance(logged_in, bool) else None,
        email=_text(payload.get("email")),
        organization=_text(payload.get("orgName")),
        method=_text(payload.get("authMethod")),
        subscription=_text(payload.get("subscriptionType")),
        checked_at=checked_at,
    )


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


class LoginError(RuntimeError):
    """A login attempt that cannot continue, with a reason worth showing."""


@dataclass
class LoginFlow:
    """One in-progress ``claude auth login`` for one account.

    The CLI prints the authorization URL and then blocks on stdin, so the flow has
    two visible steps with a human in between. It is kept as a live process rather
    than restarted per step: the code the browser returns is only valid for the
    PKCE challenge *that* process created.
    """

    account_key: str
    config_dir: Path
    binary: str | None = None
    state: str = "starting"  # starting | awaiting_code | verifying | done | failed
    url: str = ""
    detail: str = ""
    status: AuthStatus | None = None
    started_at: datetime | None = None
    _process: subprocess.Popen | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.state in {"starting", "awaiting_code", "verifying"}

    def start(self, *, spawn=None, now=None) -> None:
        """Launch the CLI and read as far as the authorization URL."""
        self.started_at = (now or (lambda: datetime.now(UTC)))()
        claude = self.binary or shutil.which("claude")
        if claude is None:
            self._fail("claude is not on PATH, so this account cannot be signed in here")
            return
        launch = spawn or _spawn
        try:
            self._process = launch(
                [claude, "auth", "login", "--claudeai"], self.config_dir
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._fail(f"could not start claude auth login: {exc}")
            return
        url = self._read_url()
        if not url:
            self.cancel()
            self._fail("claude auth login did not print an authorization URL")
            return
        self.url = url
        self.state = "awaiting_code"
        self.detail = "Open the link, approve, then paste the code it gives you."

    def _read_url(self) -> str:
        """Read the CLI's opening output up to the authorize URL.

        Line-by-line rather than ``communicate()``: the process stays alive waiting
        for the code, so reading to EOF would deadlock the request.
        """
        process = self._process
        if process is None or process.stdout is None:
            return ""
        deadline = URL_TIMEOUT_S
        for _ in range(40):  # bounded lines, so junk output cannot spin forever
            line = process.stdout.readline()
            if not line:
                return ""
            match = _URL_RE.search(line)
            if match:
                return match.group(0)
            if deadline <= 0:
                return ""
        return ""

    def submit_code(self, code: str) -> None:
        """Hand the browser's code to the waiting CLI and confirm the result."""
        if self.state != "awaiting_code":
            raise LoginError(f"this login is {self.state}, not waiting for a code")
        code = (code or "").strip()
        if not code:
            raise LoginError("no code was given")
        process = self._process
        if process is None or process.stdin is None:
            self._fail("the login process is gone; start it again")
            return
        self.state = "verifying"
        try:
            process.stdin.write(code + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            self._fail(f"could not hand the code to claude: {exc}")
            return
        try:
            process.wait(timeout=CODE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.cancel()
            self._fail("claude auth login did not finish after the code was given")
            return
        # The exit code is not the evidence: what matters is whether the account is
        # signed in afterwards, which is the same question the deck asks anyway.
        self.status = read_status(self.config_dir, binary=self.binary)
        if self.status.logged_in:
            self.state = "done"
            self.detail = "Signed in."
        else:
            self.state = "failed"
            self.detail = self.status.detail or "the code was not accepted"

    def cancel(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.kill()
        except OSError:
            pass
        else:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.debug("claude auth login did not exit after kill")

    def _fail(self, detail: str) -> None:
        self.state = "failed"
        self.detail = detail

    def snapshot(self) -> dict:
        """What the UI may see. Deliberately no process handle and no code."""
        return {
            "account_key": self.account_key,
            "state": self.state,
            "url": self.url,
            "detail": self.detail,
            "logged_in": self.status.logged_in if self.status else None,
            "email": self.status.email if self.status else None,
        }


def _spawn(command: list[str], config_dir: Path) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        command,
        cwd=str(Path.home()),
        env=_account_env(config_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
