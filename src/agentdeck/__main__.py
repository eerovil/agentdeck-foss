"""``python -m agentdeck`` / the ``agentdeck`` console script."""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin

import httpx
import uvicorn
from uvicorn.main import STARTUP_FAILURE

from . import shutdown
from .app import create_app
from .command_artifact_client import ArtifactClientError, request_artifact
from .command_artifacts import ACCOUNT_KEY_ENV
from .config import DEFAULT_DELEGATION_TIMEOUT_S, config_path, load_config
from .error_reporting import initialize_error_reporting
from .job_client import (
    JobClientError,
    cancel_job,
    get_job,
    list_jobs,
    records_other_workspaces,
    submit_job,
)
from .jobs import DESKTOP as DESKTOP_LANE
from .jobs import KUBE as KUBE_LANE
from .jobs import LANES as JOB_LANES
from .jobs import LOCAL as LOCAL_LANE
from .jobs import (
    canonical_lane,
    default_lane,
    kube_image_notice,
    lane_label,
    lane_settings,
    quiet_output_hint,
)
from .local_bin import ensure_local_bin_on_path
from .providers.codex.runtime_client import runtime_socket_path
from .restart_nudge import RestartNudgeRegistry
from .session_secrets import SecretRuntimeError, claim_runtime_secret
from .test_timings import read_timings, summarize, timings_path_for_log


def main() -> None:
    ensure_local_bin_on_path()
    if len(sys.argv) > 1 and sys.argv[1] == "resolve-context-delivery":
        raise SystemExit(_resolve_context_delivery(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "secret-run":
        raise SystemExit(_secret_run(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "job":
        raise SystemExit(_job(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "session-key":
        raise SystemExit(_session_key(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "artifact":
        raise SystemExit(_artifact(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "delegate":
        _delegate(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "codex-runtime":
        _codex_runtime(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "restart-runtime":
        _restart_runtime(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("chat-review", "review-fallback"):
        from .review_refs import reviewer_available

        if not reviewer_available():
            # Refused rather than dropped: an unknown command starts the web server (#3623).
            print(f"agentdeck {sys.argv[1]}: this edition has no review chats", file=sys.stderr)
            raise SystemExit(2)
    if len(sys.argv) > 1 and sys.argv[1] == "chat-review":
        raise SystemExit(_chat_review(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "deck-request":
        raise SystemExit(_deck_request(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "review-fallback":
        raise SystemExit(_review_fallback(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "github-account":
        from .github_accounts import main as github_account

        raise SystemExit(github_account(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "gh":
        from .github_identity import gh_main

        raise SystemExit(gh_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "git-credential":
        from .git_credential import main as git_credential

        raise SystemExit(git_credential(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "nudge-on-restart":
        _nudge_on_restart(sys.argv[2:])
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Never emit bearer tokens even if something tries to log one.
    _install_redaction_filter()
    initialize_error_reporting("web")

    path = config_path()
    config = load_config(path)
    app = create_app(config)
    logging.getLogger(__name__).info(
        "agentdeck starting on %s:%d (%d account(s))",
        config.server.bind,
        config.server.port,
        len(config.accounts),
    )
    # Session SSE streams are intentionally long-lived and browsers reconnect
    # them automatically. Do not let those connections hold a frontend deploy
    # open until systemd's stop timeout; Codex work lives in the runtime service.
    #
    # A worker delivery is the opposite kind of connection and must not be paying
    # for that trade. Cut between its ledger fence and the runtime's answer it can
    # only be recorded `uncertain`, which at-most-once makes permanent, and the
    # instruction is lost (#1889). `shutdown` refuses a delivery that has not begun
    # and waits for one that has; the two seconds here stay as they are.
    server = _WebServer(
        uvicorn.Config(
            app,
            host=config.server.bind,
            port=config.server.port,
            log_level="info",
            timeout_graceful_shutdown=2,
        )
    )
    server.run()


class _WebServer(uvicorn.Server):
    """Tell the delivery path a stop has begun, the moment uvicorn learns of it.

    The signal arrives here and nowhere else: uvicorn owns the handler and knows nothing
    about the app it is serving, so the flag is process-wide rather than app state. It has
    to be set on the *signal* and not in the lifespan's teardown, because by the time the
    lifespan is unwinding the connections have already been cancelled — which is the very
    thing a delivery has to be refused ahead of (#1889).
    """

    def handle_exit(self, sig, frame) -> None:
        shutdown.signal_shutdown()
        super().handle_exit(sig, frame)


def _local_deck_url() -> str:
    """The deck on this host, read from its own config when nothing points us at one.

    The poller launches this through `systemd-run --user`, which hands the command the user
    manager's environment — and `AGENTDECK_URL` is not in it. A shell that has the variable
    is not the same thing as a service that does. Without this fallback, naming
    `review_program` in an instance's overlay makes every scoped review exit "AGENTDECK_URL
    is not set", and because that answer is a *refusal* rather than a failure the poller
    writes the head off and never asks again: reviews would go quiet with nothing obviously
    broken.

    An unreadable or absent config is not an error here — the caller says what to do about
    an empty answer.
    """
    try:
        config = load_config(config_path())
    except Exception:  # pragma: no cover - a broken config is the caller's refusal to report
        return ""
    host = config.server.bind or "127.0.0.1"
    # A deck bound to every interface is still reachable on loopback, and loopback is the only
    # address a command on this host can rely on. The family has to survive the swap though: a
    # listener on `::` may be IPv6-only (`IPV6_V6ONLY`), and dialling 127.0.0.1 at one of those
    # fails — as a refusal, which is the silent write-off this fallback exists to prevent.
    if host in {"0.0.0.0", "*"}:  # noqa: S104 - matching a config value, not binding one
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{config.server.port}"


def _chat_review(argv: list[str]) -> int:
    """Ask ChatGPT chats to review one issue each, and start a worker on them (#1820).

    Through the running deck rather than in this process, because the press it can end with
    shares marker, lock and receipt files with the board's own buttons — two writers
    would be two claims on one browser profile. The kanban poller calls this after a pull
    request is ready, so a refusal has to be a status a script can read, not a traceback.

    Several targets are one call (#2069), because the poller now asks about every pull request
    that went ready rather than the oldest one. They are opened one at a time and each answers
    for itself: one malformed target must not lose the chats the others opened, so the receipt
    carries a row per target and the exit status is only the summary of them.
    """
    parser = argparse.ArgumentParser(
        prog="agentdeck chat-review",
        description="Open a review chat for each issue, and nudge a worker onto them.",
    )
    parser.add_argument(
        "targets",
        nargs="+",
        metavar="target",
        help="the issues to review, each as owner/repo#123 or its GitHub URL",
    )
    parser.add_argument(
        "--no-start",
        dest="start",
        action="store_false",
        help=(
            "open the chats without starting a worker; the deck's own auto-nudge presses "
            "for a waiting chat within its hourly ceiling"
        ),
    )
    args = parser.parse_args(argv)

    def chat_opened(outcome: dict) -> dict | None:
        """The review chat this call opened or reused, as the poller needs to name it (#1876).

        The poller records a head as reviewed on evidence the review happened, and the chat is
        that evidence, so the receipt has to say which chat it was. The outcome carries the
        chat's number and its URL; the repository comes off the URL, because that is where the
        deck's own configured chat board is already spelled out.

        It also says which commit that chat is about (#2088). A chat already open for this
        target was opened about an earlier head and reviews that one, so the poller has to
        record *its* commit as reviewed rather than the newer one it asked about — otherwise
        a pushed commit is credited to a chat that never saw it.
        """
        number = outcome.get("issue_number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            return None
        url = str(outcome.get("issue_url") or "")
        rest = url.removeprefix("https://github.com/").strip("/").split("/")
        if len(rest) < 2 or not rest[0] or not rest[1]:
            return None
        row = {"repository": f"{rest[0]}/{rest[1]}", "number": number}
        head = str(outcome.get("head_sha") or "")
        if head:
            row["head"] = head
        return row

    def report(rows: list[dict]) -> None:
        """Answer the poller launch that started this, when one did (#1820).

        The kanban poller launches this detached, so an exit status reaches nobody. It records
        a head as reviewed only once a receipt says the review happened; without one, the same
        pull request is asked about on every wake until the extension gives up on it. The file
        is the same shape the click program writes, so the settle path reads both alike.

        `refused` is the part a click never had to say. A click that failed is worth trying
        again — the browser was busy, the page had not loaded. A review the deck *answered* by
        refusing is not: the target is malformed, or this deck has no chat board, and the next
        wake would put the same question to the same deck and get the same sentence back. So a
        refusal says so, and carries the reason, because "the poller learns why instead of
        retrying" is the thing this was for.

        `rows` is one entry per target, and `targets` in the receipt is how the poller settles
        each head on its own evidence (#2069). The top-level `ok`, `refused`, `target`, `chat`
        and `detail` stay exactly as a single-target call always wrote them: the click program
        writes only the first two, and a receipt left over from the release before this one is
        still read by the same settle path.
        """
        path = os.environ.get("CHATGPT_NUDGE_RECEIPT", "").strip()
        if not path:
            return
        first = rows[0] if rows else {"ok": False, "refused": False, "detail": "", "target": ""}
        try:
            receipt = Path(path)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            answer = {
                # Every target had to open for the launch as a whole to have succeeded, and a
                # launch is only *refused* when nothing about it is worth asking again.
                "ok": all(row["ok"] for row in rows) if rows else False,
                "refused": bool(rows) and all(
                    row["ok"] or row["refused"] for row in rows
                ) and not all(row["ok"] for row in rows),
                "at": time.time(),
                "target": first.get("target", ""),
                "detail": first.get("detail", ""),
                "targets": {
                    row["target"]: {
                        key: row[key]
                        for key in ("ok", "refused", "detail", "chat")
                        if row.get(key) is not None
                    }
                    for row in rows
                },
            }
            if first.get("chat") is not None:
                answer["chat"] = first["chat"]
            receipt.write_text(json.dumps(answer, sort_keys=True))
        except OSError:
            # Nothing to do about it here, and failing the review over its own receipt would
            # turn a review that happened into one the poller retries.
            pass

    def row(target: str, ok: bool, detail: str, *, refused: bool = False,
            chat: dict | None = None) -> dict:
        return {"target": target, "ok": ok, "refused": refused, "detail": detail, "chat": chat}

    base = os.environ.get("AGENTDECK_URL", "").strip() or _local_deck_url()
    if not base:
        # A deck that was never pointed at is a configuration fact, not a bad moment, and it
        # is the same fact for every target — so all of them are answered, and refused.
        detail = "AGENTDECK_URL is not set and this host has no deck config to read"
        report([row(target, False, detail, refused=True) for target in args.targets])
        parser.exit(1, "agentdeck chat-review: AGENTDECK_URL is not set\n")

    rows: list[dict] = []
    outcomes: list[dict] = []
    for target in args.targets:
        try:
            response = httpx.post(
                urljoin(base + "/", "api/chat-review"),
                json={"target": target, "start": args.start},
                timeout=60.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = exc.response.text
            try:
                detail = exc.response.json().get("detail", detail)
            except ValueError:
                pass
            # Only a 4xx is the deck saying no. `/api/chat-review` answers a refusal as 400
            # with the reason, and that is worth nobody's second wake. A 5xx is the deck having
            # a bad moment — mid-deploy, or a bug it may not hit twice — which is the same kind
            # of thing as never reaching it at all, so it keeps its retry and its detail.
            rows.append(row(target, False, str(detail), refused=400 <= status < 500))
            continue
        except httpx.HTTPError as exc:
            # Never reached the deck — a restart or a deploy. That is worth another wake.
            rows.append(row(target, False, str(exc)))
            continue
        outcome = response.json()
        outcome = outcome if isinstance(outcome, dict) else {}
        outcomes.append(outcome)
        rows.append(
            row(
                target,
                True,
                str(outcome.get("summary") or outcome.get("issue_url") or ""),
                chat=chat_opened(outcome),
            )
        )

    report(rows)
    print(json.dumps(outcomes[0] if len(outcomes) == 1 else outcomes, sort_keys=True))
    failed = [entry for entry in rows if not entry["ok"]]
    if failed:
        # Named rather than counted: a wake that opened two of three chats has to be able to
        # say which one it did not, and the log line is the only place a person sees it.
        parser.exit(
            1,
            "agentdeck chat-review: "
            + "; ".join(f"{entry['target']}: {entry['detail']}" for entry in failed)
            + "\n",
        )
    return 0



#: How long ``agentdeck deck-request`` keeps asking a deck that does not answer (#2974). Under
#: the ten minutes a Claude shell command may be given, so the reviewer's call outlasts a deck
#: restart without being cut off itself.
DECK_REQUEST_PATIENCE_S = 9 * 60.0
#: How long one try waits for the deck's answer (#2963).
DECK_REQUEST_TIMEOUT_S = 120.0


def _deck_request(argv: list[str]) -> int:
    """Hand the fallback reviewer's verdict, findings or comment to the deck (#2736, #3004).

    What the Claude fallback reviewer runs instead of posting ``/reviewed`` or ``/claude`` findings
    on GitHub, where nothing reads them from the deck's own account. The id is a hash of the card
    and the comment, so running the same thing twice is one request.
    """
    parser = argparse.ArgumentParser(
        prog="agentdeck deck-request",
        description=(
            "Send one /reviewed verdict or /claude findings to the deck, or any other reviewer "
            "comment for the deck to post on the card."
        ),
    )
    parser.add_argument("issue", help="the card, as owner/repo#123")
    parser.add_argument("--body-file", help="read the command from this file")
    parser.add_argument("command", nargs="*", help="the command, when no --body-file is given")
    args = parser.parse_args(argv)
    if args.body_file:
        try:
            command = Path(args.body_file).read_text()
        except OSError as exc:
            parser.exit(2, f"agentdeck deck-request: {exc}\n")
    else:
        command = " ".join(args.command)
    command = command.strip()
    if not command:
        parser.exit(2, "agentdeck deck-request: no command given\n")
    base = os.environ.get("AGENTDECK_URL", "").strip() or _local_deck_url()
    if not base:
        parser.exit(1, "agentdeck deck-request: AGENTDECK_URL is not set\n")
    import hashlib

    # Case-folded, as the deck finds the card: ``Eerovil/AgentDeck#5`` is the same card.
    issue = args.issue.strip().lower()
    request_id = hashlib.sha256(f"{issue}\n{command}".encode()).hexdigest()[:40]
    # A deck that does not answer is usually one mid-restart (#2974: a hung web reload ate a
    # reviewer's verdict in two 30-second tries), so keep asking for a while. A 503 is the
    # deck's own "uncertain", the restarting deck's "send it again" among them, so it is asked
    # again too. The id is the same each time, so a try the deck did receive is never a second
    # request: a recorded "uncertain" only repeats its answer. A 4xx refusal is final.
    deadline = time.monotonic() + DECK_REQUEST_PATIENCE_S
    pause = 5.0
    while True:
        response: httpx.Response | None = None
        try:
            response = httpx.post(
                urljoin(base + "/", "api/deck-request"),
                json={"issue": args.issue.strip(), "command": command, "id": request_id},
                # A verdict on a Work the deck releases does its GitHub reads before it
                # answers; 30 s cut off the slow ones (#2963).
                timeout=DECK_REQUEST_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            why = f"the deck did not answer ({exc})"
        else:
            why = "the deck could not say whether it was asked (503)"
        if response is not None and response.status_code != 503:
            break
        # A try is not started that could run past the patience: the whole call stays inside
        # the ten minutes the reviewer's shell gives it.
        if time.monotonic() + pause + DECK_REQUEST_TIMEOUT_S >= deadline:
            if response is None:
                parser.exit(1, f"agentdeck deck-request: {why}\n")
            break  # the last 503's own detail is the answer
        print(f"agentdeck deck-request: {why}; trying again", file=sys.stderr)
        time.sleep(pause)
        pause = min(pause * 2, 30.0)
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    if response.status_code >= 400:
        parser.exit(1, f"agentdeck deck-request: {detail}\n")
    print(detail)
    return 0


def _tell_deck_review_fallback_changed() -> None:
    """Wake the deck's review fallback after a switch write (#2865). Best effort.

    The deck no longer looks every 30 seconds, so a switch flipped here would otherwise
    wait for the next board change. A deck that cannot be reached reads the file on start.
    """
    base = os.environ.get("AGENTDECK_URL", "").strip() or _local_deck_url()
    if not base:
        return
    with contextlib.suppress(httpx.HTTPError):
        httpx.post(urljoin(base + "/", "api/review-fallback/changed"), timeout=5.0)


def _review_fallback(argv: list[str]) -> int:
    """Switch the fallback reviewers on until a time, off, or say how it stands (#2736)."""
    from . import review_fallback

    parser = argparse.ArgumentParser(
        prog="agentdeck review-fallback",
        description="Have Claude and Codex do every review instead of ChatGPT, until a set time.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    on = sub.add_parser("on", help="switch on until a time")
    on.add_argument(
        "--until",
        default="",
        help=f"6h, 90m, 18:30 or 2026-10-08T18:30 (default {review_fallback.DEFAULT_HOURS:g}h)",
    )
    only = sub.add_parser(
        "only", help="let one kind do every review until a time, then both again (#2906)"
    )
    only.add_argument("kind", choices=sorted(review_fallback.KINDS))
    only.add_argument("--until", required=True, help="47h, 18:30 or 2026-10-14T10:23")
    sub.add_parser("off", help="switch off now; reviews already running finish")
    sub.add_parser("status", help="say whether it is on, and until when")
    args = parser.parse_args(argv)
    chat_board = load_config(config_path()).chat_board
    path = review_fallback.switch_path(chat_board)
    who = review_fallback.names(chat_board)
    now = time.time()
    if args.action == "on":
        try:
            until = review_fallback.parse_until(args.until, now=now)
            switch = review_fallback.turn_on(path, until, by="cli", now=now)
        except review_fallback.SwitchError as exc:
            parser.exit(2, f"agentdeck review-fallback: {exc}\n")
        _tell_deck_review_fallback_changed()
        print(f"Reviews go to {who} instead of ChatGPT until {switch.until_iso()}.")
        return 0
    if args.action == "only":
        try:
            until = review_fallback.parse_until(args.until, now=now)
            switch = review_fallback.limit_to(path, args.kind, until, by="cli", now=now)
        except review_fallback.SwitchError as exc:
            parser.exit(2, f"agentdeck review-fallback: {exc}\n")
        _tell_deck_review_fallback_changed()
        print(
            f"Reviews go to {review_fallback.KINDS[args.kind]} only until "
            f"{switch.only_until_iso()}"
            + (
                f", then to {who} until {switch.until_iso()}."
                if switch.until > switch.only_until
                else ", then back to ChatGPT."
            )
        )
        return 0
    if args.action == "off":
        was = review_fallback.turn_off(path)
        _tell_deck_review_fallback_changed()
        print("Switched off; reviews go back to ChatGPT." if was else "It was already off.")
        return 0
    switch = review_fallback.active(path, now)
    if switch is None:
        print("Off: ChatGPT does the reviews.")
    else:
        print(f"On: reviews go to {who} until {switch.until_iso()}.")
        if switch.only_now(now):
            print(f"{switch.only_words(now)}.")
    return 0


def _resolve_context_delivery(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agentdeck resolve-context-delivery",
        description="Resolve one fenced context delivery without sending provider input.",
    )
    parser.add_argument("--provider", required=True)
    parser.add_argument("--account", required=True, help="configured account label")
    parser.add_argument("--worker", required=True, help="exact keyed-worker id")
    parser.add_argument("--delivery-id", required=True)
    parser.add_argument(
        "--choice", required=True, choices=("accepted", "not_written", "abandoned")
    )
    parser.add_argument("--rationale", required=True)
    args = parser.parse_args(argv)
    base = os.environ.get("AGENTDECK_URL", "").strip()
    if not base:
        parser.exit(1, "agentdeck resolve-context-delivery: AGENTDECK_URL is not set\n")
    url = urljoin(
        base + "/",
        f"api/workers/accounts/{args.provider}/{args.account}/context-delivery-resolution",
    )
    try:
        response = httpx.put(
            url,
            json={
                "key": args.worker,
                "delivery_id": args.delivery_id,
                "choice": args.choice,
                "rationale": args.rationale,
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        parser.exit(1, f"agentdeck resolve-context-delivery: {exc.response.text}\n")
    except httpx.HTTPError as exc:
        parser.exit(1, f"agentdeck resolve-context-delivery: {exc}\n")
    print(json.dumps(response.json(), sort_keys=True))
    return 0


def _secret_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agentdeck secret-run",
        description="Run one command with a temporary session secret.",
    )
    parser.add_argument("ticket", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    try:
        claimed = claim_runtime_secret(runtime_socket_path(), args.ticket)
    except SecretRuntimeError as exc:
        parser.exit(1, f"agentdeck secret-run: {exc}\n")
    environment = os.environ.copy()
    environment[claimed.name] = claimed.value
    try:
        completed = subprocess.run(  # noqa: S603 -- argv is the user's requested command
            command,
            env=environment,
            stdin=None,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        parser.exit(1, f"agentdeck secret-run: could not start command: {exc}\n")
    secret = claimed.value.encode()
    replacement = f"[redacted secret {claimed.name}]".encode()
    sys.stdout.buffer.write(completed.stdout.replace(secret, replacement))
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(completed.stderr.replace(secret, replacement))
    sys.stderr.buffer.flush()
    return completed.returncode


JOB_POLL_S = 2.0


def _artifact(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agentdeck artifact", description="Read a protected command-result artifact."
    )
    subcommands = parser.add_subparsers(dest="subcommand", required=True)
    command = subcommands.add_parser("slice")
    command.add_argument("artifact_id")
    command.add_argument("--cap", required=True, help=argparse.SUPPRESS)
    command.add_argument("--stream", choices=("stdout", "stderr"), required=True)
    command.add_argument("--offset", type=int, default=0)
    command.add_argument("--max-bytes", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        payload: dict[str, object] = {
            "slice_capability": args.cap,
            "stream": args.stream,
            "offset": args.offset,
            "max_bytes": args.max_bytes,
        }
        result = request_artifact(
            runtime_socket_path(), args.artifact_id, args.subcommand, payload
        )
    except ArtifactClientError as exc:
        parser.exit(1, f"agentdeck artifact: {exc}\n")
    sys.stdout.buffer.write(base64.b64decode(result["data_base64"], validate=True))
    return 0


# How the submission line names the lane a job actually took. The local lane
# says nothing, because "queued job ..." already means this host.
def _job_lane_description(lane: str) -> str:
    return "" if lane == LOCAL_LANE else f" in a pod on {lane_label(lane)}"


def _job(argv: list[str]) -> int:
    """``agentdeck job`` — the host-wide queue for heavy commands (#1287).

    Submitting returns immediately and the runtime tells the submitting chat when
    the job ends, so an agent can keep working instead of holding a ten-minute
    Bash call open waiting for a test suite.
    """
    parser = argparse.ArgumentParser(
        prog="agentdeck job",
        description="Queue a heavy command (a test suite, a build) behind the host-wide slots.",
    )
    subcommands = parser.add_subparsers(dest="subcommand", required=True)

    submit = subcommands.add_parser("submit", help="queue a command and return its job id")
    submit.add_argument("--cwd", default=None, help="working directory (default: this one)")
    submit.add_argument("--session", default=None, help="AgentDeck session key to tell when done")
    submit.add_argument(
        "--session-token", default=None, help="session token to resolve into a session key"
    )
    submit.add_argument("--label", default="", help="short name for the job in listings")
    submit.add_argument(
        "--verification",
        action="store_true",
        help=(
            "this is the run that proves a poller card's work: the job is attached to the "
            "chat's Run, and a green run on a clean, committed tree is that commit's release "
            "evidence. Needs --session-token"
        ),
    )
    submit.add_argument(
        "--work",
        default="",
        metavar="ISSUE",
        help=(
            "with --verification, for a chat that joined several Works: which one the run "
            "proves, as owner/repo#number or a Work id. It must be one of the chat's own"
        ),
    )
    submit.add_argument(
        "--workspace",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "with --verification: another checkout the command also tests, such as a second "
            "repository of the same Work; repeatable. Each is recorded next to --cwd as what "
            "the run verified, and the job runs on this host"
        ),
    )
    submit.add_argument(
        "--where",
        default=None,
        metavar="{" + ",".join(JOB_LANES) + "}",
        help=(
            "the remote lanes this host has are listed in ~/.config/agentdeck/"
            "job-lanes.toml; with none, everything runs here. By default the agentdeck "
            "checkout's pytest goes to a pod on the desktop lane, moving to kube when the "
            "desktop is busy, full or off; on a host with ten or more cores it runs here "
            "first instead, moving to desktop and then kube when this host is full. "
            "Another listed project's pytest goes to kube, and anything else runs here. "
            "local forces this host's heavy slots, at low priority, worth doing when the "
            "cluster lanes are backed up or down; mac runs pytest in a pod on the Mac "
            "lane, and only when named"
        ),
    )
    submit.add_argument(
        "--max-minutes",
        type=int,
        default=None,
        metavar="N",
        help=(
            "stop the job after N minutes of running (0: no limit). By default the limit is "
            "3x the slowest of the project's last 20 passed runs, at least 15 minutes, or "
            "60 minutes for a project with none yet"
        ),
    )
    submit.add_argument(
        "--wait",
        action="store_true",
        help="block, stream the output, and exit with the command's own status",
    )
    submit.add_argument("command", nargs=argparse.REMAINDER)

    subcommands.add_parser("list", help="show the queue")
    status = subcommands.add_parser("status", help="show one job")
    status.add_argument("job_id")
    logs = subcommands.add_parser("logs", help="print a job's output")
    logs.add_argument("job_id")
    cancel = subcommands.add_parser("cancel", help="cancel a queued or running job")
    cancel.add_argument("job_id")
    tests = subcommands.add_parser(
        "tests",
        help="a pytest job's tests running now and the slowest finished, with start/stop times",
    )
    tests.add_argument("job_id")
    tests.add_argument("--slowest", type=int, default=15, help="how many finished tests to list")

    args = parser.parse_args(argv)
    socket_path = runtime_socket_path()
    try:
        if args.subcommand == "submit":
            return _job_submit(parser, socket_path, args)
        if args.subcommand == "list":
            for job in list_jobs(socket_path):
                print(_job_line(job))
            return 0
        if args.subcommand == "status":
            print(_job_line(get_job(socket_path, args.job_id)))
            return 0
        if args.subcommand == "logs":
            print(Path(get_job(socket_path, args.job_id)["log_path"]).read_text(errors="replace"))
            return 0
        if args.subcommand == "cancel":
            print(_job_line(cancel_job(socket_path, args.job_id)))
            return 0
        if args.subcommand == "tests":
            log_path = get_job(socket_path, args.job_id)["log_path"]
            timings = read_timings(timings_path_for_log(log_path))
            print(summarize(timings, now=time.time(), slowest=max(0, args.slowest)))
            return 0
    except JobClientError as exc:
        parser.exit(1, f"agentdeck job: {exc}\n")
    except OSError as exc:
        parser.exit(1, f"agentdeck job: {exc}\n")
    return 0


def _job_submit(parser: argparse.ArgumentParser, socket_path: Path, args) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    # One lookup answers both which chat to tell and which Run that chat is in.
    binding = _resolve_session_token(args.session_token) if args.session_token else None
    session_key, unnamed_reason = _job_session_key(args, binding)
    if args.workspace and not args.verification:
        parser.error("--workspace names what a --verification job tests")
    if args.work and not args.verification:
        parser.error("--work names the Work a --verification job proves")
    if args.max_minutes is not None and args.max_minutes < 0:
        parser.error("--max-minutes is 0 (no limit) or more")
    work_id, run_id = _job_run(args, binding)
    if args.workspace and not records_other_workspaces(socket_path):
        # Queued anyway, the job would record only --cwd and still read as complete
        # one-repository evidence, crediting a tree whose other repositories nobody proved.
        raise JobClientError(
            "the AgentDeck runtime does not record other workspaces yet (it has not been "
            "restarted since --workspace shipped), so this verification was not queued"
        )
    # Absolute here: the runtime resolves a relative path against its own directory, so
    # `--cwd .` from a worktree used to test the deck's own checkout (#3338).
    cwd = os.path.abspath(os.path.expanduser(args.cwd)) if args.cwd else os.getcwd()
    # No --where means "wherever this belongs", which is a pod whenever one can
    # run it (#1368); an explicit lane is still obeyed, refusal and all. A job that
    # also tests another checkout runs here, because a pod only receives its own (#2148).
    if args.where:
        args.where = canonical_lane(args.where)
        if args.where not in JOB_LANES:
            raise JobClientError(
                f"unknown lane {args.where!r}; expected one of {', '.join(JOB_LANES)}"
            )
    lane = args.where or (LOCAL_LANE if args.workspace else default_lane(cwd, command))

    def submit(lane: str) -> dict:
        return submit_job(
            socket_path,
            command=command,
            cwd=cwd,
            lane=lane,
            lane_defaulted=not args.where,
            session_key=session_key,
            label=args.label,
            purpose="verification" if args.verification else "",
            work_id=work_id,
            run_id=run_id,
            workspaces=[os.path.abspath(path) for path in args.workspace],
            max_minutes=args.max_minutes,
        )

    try:
        job = submit(lane)
    except JobClientError as exc:
        # The runtime outlives deploys, so this CLI can know the desktop lane before
        # the runtime does (#2297) — or by its new name before the runtime does
        # (#3623). It refused the submission outright, so nothing was queued and
        # asking again by a name it does know cannot run anything twice.
        if lane != DESKTOP_LANE or "unknown lane" not in str(exc):
            raise
        older = [name for name, to in lane_settings().aliases.items() if to == DESKTOP_LANE]
        if older:
            lane = older[0]
        elif args.where:
            raise
        else:
            lane = KUBE_LANE
        job = submit(lane)
        lane = canonical_lane(lane)
    # Printed with the submission line, while whoever submitted it is still
    # reading: a dependency bump makes the dev-cluster lane take the slow path,
    # and it used to say so only in the log (#1590). Keyed off the lane this
    # submission actually took, not off --where, or the submissions that never
    # name a lane — now the common ones (#1618) — would be the silent ones.
    notice = kube_image_notice(cwd) if lane == KUBE_LANE else None
    if args.workspace and "workspaces" not in job:
        # The runtime was replaced by an older one between the check above and the submit.
        # The row it took would be one-repository evidence for a multi-repository run, so it
        # must not be allowed to finish.
        cancelled = cancel_job(socket_path, job["id"])
        raise JobClientError(
            f"job {job['id']} was queued by a runtime that dropped --workspace, and was "
            f"cancelled ({cancelled.get('state', 'unknown')}) so it cannot become evidence; "
            "resubmit once the runtime is current"
        )
    if args.verification and job.get("purpose") != "verification":
        # The runtime that took it predates verification jobs and dropped the purpose, so the
        # run is an ordinary job. Say so now rather than let a card wait on evidence that will
        # never be credited.
        sys.stdout.flush()
        print(
            f"job {job['id']} was queued as an ordinary job: the AgentDeck runtime does not "
            "record verification jobs yet, so this run will not count as release evidence.",
            file=sys.stderr,
        )
    if not args.wait:
        where = _job_lane_description(lane)
        print(f"queued job {job['id']}{where}: {' '.join(command)}")
        print(f"output: {job['log_path']}")
        if notice:
            print(notice)
        hint = quiet_output_hint(cwd, command)
        if hint:
            print(hint)
        if session_key:
            print(
                "You can do other work, or end your turn. AgentDeck will send this chat "
                "a message when the job finishes, and one with its progress every five "
                "minutes while it runs."
            )
        else:
            # Nothing will announce it, so say so rather than let the caller wait
            # for a message that is never coming — and do not send them into the
            # polling loop the queue exists to remove. Flush first: the warning
            # goes to stderr, and unflushed stdout would print it out of order.
            sys.stdout.flush()
            print(
                f"No chat could be identified, so nothing will announce this job: "
                f"{unnamed_reason}. Read {job['log_path']} once it is done instead "
                "of polling for it, or resubmit with --session <key> / "
                "--session-token <token> to have the finish announced.",
                file=sys.stderr,
            )
        return 0
    if notice:
        print(notice)
    return _job_wait(socket_path, job)


def _resolve_session_token(token: str) -> dict:
    """What the deck knows about the chat holding this session token."""
    base = os.environ.get("AGENTDECK_URL", "").strip()
    if not base:
        raise JobClientError("--session-token needs AGENTDECK_URL to resolve it")
    url = urljoin(base + "/", f"api/sessions/by-token/{token}")
    try:
        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise JobClientError(f"could not resolve the session token: {exc}") from None
    if not isinstance(payload, dict) or not payload.get("session_key"):
        raise JobClientError("that session token is bound to no session")
    return payload


def _session_key(argv: list[str]) -> int:
    """Print the session key a session token is bound to (#3350).

    The brief tells every owned chat to run this rather than curl the by-token route:
    Codex's approval gate refused the curl as sending a credential to an unknown place.
    """
    parser = argparse.ArgumentParser(
        prog="agentdeck session-key",
        description="Print this chat's session key from the session token in its brief.",
    )
    parser.add_argument("token", help="the AgentDeck session token")
    args = parser.parse_args(argv)
    try:
        payload = _resolve_session_token(args.token.strip())
    except JobClientError as exc:
        print(f"agentdeck session-key: {exc}", file=sys.stderr)
        return 1
    print(payload["session_key"])
    return 0


def _job_run(args, binding: dict | None) -> tuple[str, str]:
    """The Work and Run a job submitted from this chat belongs to (#2154, #2622).

    Read from the deck's own binding of the calling chat, never from anything the caller
    types: the chat a poller delivery landed in is bound to that delivery's Run (#2142), so
    the token is the one input and the Run is the deck's answer. Every job a Run's chat
    queues carries it, so the poller can tell a worker waiting on its suite from one that
    ended without reporting; only a ``--verification`` job is ever evidence, and only it
    refuses a chat that is in no Run.
    """
    if not args.verification:
        if binding is None:
            return "", ""
        return str(binding.get("work_id") or ""), str(binding.get("run_id") or "")
    if binding is None:
        raise JobClientError(
            "--verification needs --session-token: the job is attached to the Run of the chat "
            "that token names"
        )
    work_id = str(binding.get("work_id") or "")
    run_id = str(binding.get("run_id") or "")
    works = [str(work) for work in binding.get("works") or () if work]
    named = _named_work(getattr(args, "work", "") or "")
    if named:
        # Only one of the chat's own Works, as the deck reports them (#3392).
        if named not in works:
            raise JobClientError(
                f"--work {args.work} is not one of this chat's Works: "
                + (", ".join(works) or "it joined none")
            )
        # Another of the chat's Works than its Run's is proved outside that Run (#3392).
        return named, run_id if named == work_id else ""
    if len(works) > 1:
        # Even with a Run: it may be one the chat finished before taking on another (#3392).
        raise JobClientError(
            "this chat joined several Works; name the one this run proves with --work: "
            + ", ".join(works)
        )
    if not run_id and not work_id:
        # A chat in a Work with no Poller card proves that Work's head (#2895); a chat in
        # neither proves nothing.
        raise JobClientError(
            "this chat is not part of any poller Run or Work, so a verification job from it "
            "would prove nothing; submit it without --verification"
        )
    return work_id, run_id


def _named_work(value: str) -> str:
    """``--work`` as a ``work_id``: an issue reference or a Work id, "" for anything else."""
    from .models import IssueRef
    from .work import WorkRef

    text = value.strip()
    if not text:
        return ""
    ref = WorkRef.from_work_id(text)
    if ref is None:
        issue = IssueRef.parse(text)
        ref = WorkRef.from_issue(issue) if issue is not None else None
    if ref is None:
        raise JobClientError(f"--work must be owner/repo#number or a Work id; got {value!r}")
    return ref.work_id


def _job_session_key(args, binding: dict | None = None) -> tuple[str | None, str | None]:
    """Which chat to tell: explicit key, then token, then the ambient environment.

    Returns the key and, when there is none, why there is none. Unlike
    ``_session_key_argument`` an unidentified chat is not fatal here: a person at
    a terminal has no chat to announce to and still wants their job queued.

    A Codex chat can never be named by an environment variable — its threads
    share one app-server process — which is why the token exists at all.
    """
    if args.session:
        return args.session, None
    if binding is not None:
        return str(binding["session_key"]), None
    return _ambient_session_key()


def _ambient_session_key() -> tuple[str | None, str | None]:
    """Name the calling chat from its own environment, or say why it cannot be.

    ``AGENTDECK_SESSION_KEY`` is injected into owned Claude turns, but plenty of
    Claude chats run without it; a bare ``$CLAUDE_CODE_SESSION_ID`` still names
    the session once the account label in front of it is known. Both
    ``agentdeck job submit`` and the ``--session-key`` arguments read this one
    rule, because a chat either provider can identify must not depend on which
    subcommand asked.

    The label comes from whichever of three sources can answer, in order of how
    certain each is: the spawning account key, a lone configured account, or the
    account that actually holds a transcript for this id. The count of
    configured accounts used to decide it alone, which refused every owned
    worker on a two-account host even though its own environment named its
    account outright (#2092).
    """
    inherited = os.environ.get("AGENTDECK_SESSION_KEY", "").strip()
    if inherited:
        return inherited, None
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not session_id:
        return None, (
            "neither $AGENTDECK_SESSION_KEY nor $CLAUDE_CODE_SESSION_ID is set,"
            " so nothing in this environment names a chat"
        )
    # A fresh owned worker cannot be handed its session key at spawn — nobody
    # knows its id until Claude has picked one — but it is handed the account
    # that spawned it, and a session key is exactly that plus the id. Exact,
    # offline, and no guess. Another provider's account key means this Claude
    # chat is a guest in someone else's worker, so it does not count.
    account_key = os.environ.get(ACCOUNT_KEY_ENV, "").strip()
    if account_key.startswith("claude_code:") and account_key.count(":") == 1:
        return f"{account_key}:{session_id}", None
    try:
        accounts = [
            account
            for account in load_config(config_path()).build_accounts()
            if account.provider_id == "claude_code"
        ]
    except (OSError, ValueError) as exc:
        return None, f"the AgentDeck config could not be read to label the chat: {exc}"
    if not accounts:
        return None, (
            "$CLAUDE_CODE_SESSION_ID is set but no claude_code account is"
            " configured, so its account label cannot be derived"
        )
    if len(accounts) == 1:
        return f"claude_code:{accounts[0].label}:{session_id}", None
    from .providers.claude_code.provider import transcript_for_session

    owners = [
        account
        for account in accounts
        if transcript_for_session(account.root, session_id) is not None
    ]
    if len(owners) == 1:
        return f"claude_code:{owners[0].label}:{session_id}", None
    # Say which of the two it was: a chat no account holds is a stale or
    # mistyped id, while one several accounts hold is the genuine ambiguity
    # this refusal was written for, and they call for different fixes.
    if not owners:
        return None, (
            "$CLAUDE_CODE_SESSION_ID is set but no chat with that id exists under"
            f" any of the {len(accounts)} configured claude_code accounts, so its"
            " account label cannot be derived"
        )
    return None, (
        "$CLAUDE_CODE_SESSION_ID is set but chats with that id exist under"
        f" {len(owners)} claude_code accounts"
        f" ({', '.join(account.label for account in owners)}), so its account"
        " label is genuinely ambiguous"
    )


def _job_wait(socket_path: Path, job: dict) -> int:
    """Foreground mode, for a person at a terminal with no chat to be told."""
    log_path = Path(job["log_path"])
    seen = 0
    while True:
        current = get_job(socket_path, job["id"])
        try:
            text = log_path.read_text(errors="replace")
        except OSError:
            text = ""
        if len(text) > seen:
            sys.stdout.write(text[seen:])
            sys.stdout.flush()
            seen = len(text)
        if current["state"] in ("done", "failed", "cancelled", "interrupted"):
            code = current.get("exit_code")
            if current["state"] != "done" and not code:
                note = current.get("note", "")
                print(f"agentdeck job: {current['state']}: {note}", file=sys.stderr)
                return 1
            return int(code or 0)
        time.sleep(JOB_POLL_S)


def _job_line(job: dict) -> str:
    parts = [job["id"], job["state"]]
    if job.get("exit_code") is not None:
        parts.append(f"exit={job['exit_code']}")
    parts.append(job.get("label") or " ".join(job.get("command", [])))
    if job.get("purpose") == "verification":
        from .poller_engine.verification import describe, snapshots_tested

        # What a verification run tested, one repository@head each (#2148).
        tested = snapshots_tested(job)
        if tested:
            parts.append(f"[tested {describe(tested)}]")
    return "  ".join(parts)


def _codex_runtime(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="agentdeck codex-runtime",
        description="Run the persistent local Codex control service.",
    )
    parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _install_redaction_filter()
    initialize_error_reporting("codex-runtime")
    from .github_accounts import check_at_start

    check_at_start("codex-runtime")
    from .runtime import create_runtime_app

    socket = runtime_socket_path()
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.unlink(missing_ok=True)
    os.umask(0o077)
    config = load_config(config_path())
    logging.getLogger(__name__).info("Codex runtime listening on %s", socket)
    app = create_runtime_app(config)
    server = uvicorn.Server(uvicorn.Config(app, uds=str(socket), log_level="info"))
    handle_exit = server.handle_exit

    def stop(sig, frame) -> None:
        # A held change-feed request would otherwise keep the server waiting (#2865).
        app.state.change_feed.shutdown_soon()
        handle_exit(sig, frame)

    server.handle_exit = stop  # type: ignore[method-assign]
    server.run()
    if not server.started:
        # `Server.run` returns quietly when startup fails, so without this the unit
        # saw a clean exit and `Restart=on-failure` left the runtime dead (#3151).
        # Exit as `uvicorn.run` would.
        raise SystemExit(STARTUP_FAILURE)


def _session_key_argument(parser: argparse.ArgumentParser, given: str | None) -> str:
    """Resolve the calling chat's AgentDeck session key, or refuse.

    The environment rule lives in ``_ambient_session_key``; here an unidentified
    chat is fatal, because these subcommands have nothing to do without one (a
    Codex chat reads its own key with ``agentdeck session-key <token>``).
    """
    explicit = (given or "").strip()
    if explicit:
        return explicit
    key, reason = _ambient_session_key()
    if not key:
        parser.error(
            f"could not determine this session's key: {reason}. Run this from"
            " inside an owned agent turn or pass"
            " --session-key <provider:label:session_id>"
        )
    return key


def _nudge_registry() -> RestartNudgeRegistry:
    config = load_config(config_path())
    return RestartNudgeRegistry(
        config.restart_nudge.path, ttl_s=config.restart_nudge.ttl_s
    )


def _nudge_on_restart(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="agentdeck nudge-on-restart",
        description=(
            "Ask to be woken up after the next persistent-runtime restart."
            " A runtime restart (including the unattended deploy's) replaces every"
            " owned agent process, so register here to be nudged to continue once"
            " the fresh runtime is up. Registration is a set: registering twice"
            " is one nudge, and the registry is cleared by the restart it fires on."
        ),
    )
    parser.add_argument(
        "--session-key",
        help="AgentDeck session key to nudge (defaults to this session's)",
    )
    parser.add_argument(
        "--cancel",
        action="store_true",
        help="withdraw a pending request instead of registering one",
    )
    args = parser.parse_args(argv)

    session_key = _session_key_argument(parser, args.session_key)
    registry = _nudge_registry()
    if args.cancel:
        removed = registry.unregister(session_key)
        print(
            f"{session_key} will no longer be nudged."
            if removed
            else f"{session_key} was not registered for a nudge.",
            file=sys.stderr,
        )
        return
    registry.register(session_key)
    print(
        f"{session_key} will be nudged to continue after the next runtime restart.",
        file=sys.stderr,
    )


def _restart_runtime(argv: list[str]) -> None:
    import subprocess

    from .providers.claude_code.restart import (
        detect_runtime_unit,
        looks_like_runtime_unit,
        trigger_detached_restart,
    )

    parser = argparse.ArgumentParser(
        prog="agentdeck restart-runtime",
        description=(
            "Restart the persistent runtime and continue this session afterward."
            " Use this instead of `systemctl restart` from inside an agent turn:"
            " it detaches the restart so the unit's control-group teardown does not"
            " kill the caller. Nothing has to be registered first — the fresh"
            " runtime reads which chats the restart cut off and wakes them itself"
            " (#1547)."
        ),
    )
    parser.add_argument(
        "--service",
        default=os.environ.get("AGENTDECK_RUNTIME_UNIT"),
        help="systemd --user unit to restart (auto-detected from the cgroup if unset)",
    )
    args = parser.parse_args(argv)

    explicit_service = args.service is not None
    service = (args.service or detect_runtime_unit() or "").strip()
    if not service:
        parser.error("could not determine the runtime unit; pass --service")
    if not explicit_service and not looks_like_runtime_unit(service):
        parser.error(
            f"refusing to auto-restart {service!r}: it does not look like an"
            " agentdeck runtime unit; pass --service explicitly if intended"
        )

    print(
        f"Restarting {service}; chats it interrupts are woken by the fresh runtime.",
        file=sys.stderr,
    )
    try:
        trigger_detached_restart(service)
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"could not trigger restart: {exc}\n")


class _DelegationParent(NamedTuple):
    """How this delegation names the chat that started it."""

    field: str  # the /api/delegations request field to send it in
    value: str
    source: str  # what the value came from, for a warning the operator can act on


def _delegation_parent(args: argparse.Namespace) -> _DelegationParent | None:
    """Identify the delegating chat, or None when nothing here can name it.

    Precedence runs from most to least deliberate. A flag beats the
    environment, and a session token beats a session id read out of the
    environment: the deck minted the token and resolves it against its own
    binding, while an inherited id is only ever a guess about which chat this
    process belongs to.
    """
    if args.parent_session:
        return _DelegationParent(
            "parent_session_id", args.parent_session, "--parent-session"
        )
    if args.parent_session_token:
        return _DelegationParent(
            "parent_session_token",
            args.parent_session_token,
            "--parent-session-token",
        )
    # $AGENTDECK_SESSION_KEY is a full session key and $CLAUDE_CODE_SESSION_ID a
    # bare provider id; the deck resolves either form to the same parent.
    for env_var in ("AGENTDECK_SESSION_KEY", "CLAUDE_CODE_SESSION_ID"):
        inherited = os.environ.get(env_var, "").strip()
        if inherited:
            return _DelegationParent("parent_session_id", inherited, f"${env_var}")
    return None


def _delegate(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="agentdeck delegate",
        description="Delegate a stdin prompt to an AgentDeck-owned Codex chat.",
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="Codex working directory")
    parser.add_argument("--account", help="AgentDeck account key; auto-selected if unique")
    parser.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write"),
        default="workspace-write",
    )
    parser.add_argument("--model", help="Codex model override")
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high", "xhigh", "max", "ultra"),
        help="Codex reasoning effort",
    )
    parser.add_argument(
        "--approval-policy",
        choices=("untrusted", "on-failure", "on-request", "never"),
        help="Codex approval policy; use 'never' for a fully autonomous run"
        " (no per-command prompts, e.g. an unattended kanban worker)",
    )
    parser.add_argument(
        "--parent-session",
        help="session key or provider session id of the chat initiating this"
        " delegation; defaults to $AGENTDECK_SESSION_KEY, then"
        " $CLAUDE_CODE_SESSION_ID. Lets AgentDeck nest the delegated chat under"
        " its parent",
    )
    parser.add_argument(
        "--parent-session-token",
        help="AgentDeck session token of the chat initiating this delegation,"
        " resolved to its session key server-side. Use this from a Codex chat:"
        " Codex threads share one process, so no environment variable can name"
        " the calling thread, and the token it was given is its only identity",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("AGENTDECK_URL", "http://127.0.0.1:8756"),
        help="running AgentDeck base URL",
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--timeout",
        type=float,
        help="wall-clock wait budget in seconds (defaults to the server's delegation budget)",
    )
    args = parser.parse_args(argv)
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be positive")

    message = sys.stdin.read()
    if not message.strip():
        parser.error("a prompt must be supplied on stdin")
    try:
        cwd = str(Path(args.cwd).expanduser().resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        parser.error(f"invalid working directory: {exc}")

    base_url = args.url.rstrip("/") + "/"
    payload = {"cwd": cwd, "message": message, "sandbox": args.sandbox}
    if args.account:
        payload["account_key"] = args.account
    if args.model:
        payload["model"] = args.model
    if args.effort:
        payload["effort"] = args.effort
    if args.approval_policy:
        payload["approval_policy"] = args.approval_policy
    parent = _delegation_parent(args)
    if parent is None:
        print(
            "AgentDeck delegation: no parent session identified, so this chat"
            " will appear in AgentDeck as its own top-level session, but it will"
            " stay out of Deckhand and cannot roll up into your handoff. Pass"
            " --parent-session-token <this chat's AgentDeck session"
            " token>, or --parent-session <session key>, to nest it under the"
            " session that started it.",
            file=sys.stderr,
        )
    else:
        payload[parent.field] = parent.value

    post_timeout = httpx.Timeout(30.0, connect=10.0)
    poll_timeout = httpx.Timeout(60.0, connect=10.0)
    try:
        with httpx.Client() as client:
            response = client.post(
                urljoin(base_url, "api/delegations"),
                json=payload,
                timeout=post_timeout,
            )
            response.raise_for_status()
            started = response.json()
            # A server that predates the field says nothing about parentage, so
            # only an explicit null means the parent we named recorded nothing.
            if parent is not None and started.get("parent_session", "") is None:
                print(
                    "AgentDeck delegation: the deck could not resolve a parent"
                    f" session from {parent.source}, so this chat stays top-level"
                    " in AgentDeck and stays out of Deckhand.",
                    file=sys.stderr,
                )
            status_url = urljoin(base_url, started["status_url"].lstrip("/"))
            delegation_id = started["id"]
            # Older servers do not advertise their configured budget. Keep that
            # mixed-version window safe instead of abandoning a job we just started.
            wait_budget = args.timeout or float(
                started.get("timeout_s", DEFAULT_DELEGATION_TIMEOUT_S)
            )
            deadline = time.monotonic() + wait_budget
            print(f"AgentDeck delegation {delegation_id} started.", file=sys.stderr)
            previous = None
            last_session_url = None
            poll_failures = 0
            while True:
                if time.monotonic() >= deadline:
                    last_known = (
                        f" The server last reported it as {previous}."
                        if previous is not None
                        else ""
                    )
                    if last_session_url:
                        last_known += f" Its chat: {last_session_url}"
                    parser.exit(
                        1,
                        "AgentDeck stopped waiting after the delegation budget expired;"
                        f" the server-side job may still be running.{last_known}"
                        " Resume it instead of"
                        f" relaunching it.\nDelegation id: {delegation_id}\n"
                        f"Status URL: {status_url}\n",
                    )
                try:
                    response = client.get(status_url, timeout=poll_timeout)
                    if _unknown_delegation(response):
                        # The deck keeps delegations across its own restarts, so this
                        # is final: retrying only waits out the budget (#2582).
                        parser.exit(
                            1,
                            f"AgentDeck no longer knows delegation {delegation_id}"
                            " (the deck may have restarted before it kept delegations)."
                            " Its chat may still have finished: "
                            + (last_session_url or "no chat link was reported")
                            + ". Do not relaunch it before checking.\n",
                        )
                    response.raise_for_status()
                    status = response.json()
                    state = status["state"]
                    poll_failures = 0
                except (httpx.HTTPError, KeyError, ValueError):
                    poll_failures += 1
                    backoff = min(
                        max(0.1, args.poll_interval) * (2 ** min(poll_failures - 1, 5)),
                        30.0,
                        max(0.0, deadline - time.monotonic()),
                    )
                    if backoff:
                        time.sleep(backoff)
                    continue
                if status.get("session_url"):
                    last_session_url = urljoin(base_url, str(status["session_url"]).lstrip("/"))
                if state != previous:
                    previous = state
                    detail = f" ({status['session_url']})" if status.get("session_url") else ""
                    print(f"AgentDeck delegation: {state}{detail}", file=sys.stderr)
                    if state == "waiting" and status.get("interaction"):
                        print(
                            "Codex needs input in AgentDeck: "
                            + json.dumps(status["interaction"], ensure_ascii=False),
                            file=sys.stderr,
                        )
                if state == "complete":
                    final_message = status.get("final_message")
                    if final_message:
                        print(final_message)
                    return
                if state == "failed":
                    parser.exit(1, f"AgentDeck delegation failed: {status.get('reason')}\n")
                time.sleep(
                    min(
                        max(0.1, args.poll_interval),
                        max(0.0, deadline - time.monotonic()),
                    )
                )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        parser.exit(1, f"AgentDeck delegation request failed: {exc}\n")


def _unknown_delegation(response: httpx.Response) -> bool:
    """Whether the deck answered that it has no record of this delegation at all."""
    if response.status_code != 404:
        return False
    try:
        return response.json().get("detail") == "unknown delegation"
    except (ValueError, AttributeError):
        return False


def _install_redaction_filter() -> None:
    import re

    pattern = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+|sk-ant-[A-Za-z0-9._\-]+")

    class _Redact(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if isinstance(record.msg, str):
                record.msg = pattern.sub(r"\g<1>[redacted]", record.msg)
            return True

    logging.getLogger().addFilter(_Redact())


if __name__ == "__main__":
    main()
