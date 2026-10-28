"""``agentdeck-project join|update``: run a poller project from its repository's file (#3755).

A project several people's decks watch keeps its settings in one of its own repositories, at
``.agentdeck/poller.toml``, in the same format as any project file. Each deck reads it from
there instead of being sent a copy:

* ``join <owner/repo>`` reads the file as the deck's own App, writes this machine's half (the
  overlay: checkouts and the agent account) when there is none yet, and turns the project on
  with the usual ``activate --apply``. Checkouts already cloned under the folder it is given
  are found by their ``origin``; missing ones are cloned there.
* ``update <instance>`` reads the file again and applies it when it changed.

Where the file came from is kept beside the instance in ``source.json``, which the deploy's
own manifest step reads to leave such an instance alone, and the Pollers page reads to say
when the file has moved on since it was applied. Nothing applies a change by itself: the
activation is a person's approval (#2157).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from . import github_identity, poller_host, setup_init
from .poller_engine import config as engine_config
from .poller_engine import github_authority
from .project_source import Source, read_source, write_source

SHARED_PATH = ".agentdeck/poller.toml"
_REPOSITORY = re.compile(r"^([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)$")


class JoinError(Exception):
    """A step that cannot go on; the message says what to do."""


# --- reading the shared file --------------------------------------------------------------------


def fetch(repository: str, path: str, opener=None) -> tuple[str, str]:
    """``(text, blob sha)`` of ``path`` on the repository's default branch, read as its App."""
    match = _REPOSITORY.match(repository)
    if not match:
        raise JoinError(f"{repository!r} is not owner/repo")
    try:
        token = github_authority.token(github_authority.authority_for(match.group(1)))
    except github_authority.Refusal as refusal:
        raise JoinError(f"this deck cannot read {repository}: {refusal}") from refusal
    status, body = setup_init._call(
        "GET", f"/repos/{repository}/contents/{path}", auth=f"token {token}", opener=opener
    )
    if status == 404:
        raise JoinError(
            f"{repository} has no {path}, or this deck's App is not installed on it — add the "
            "repository to the App's installation, or commit the project file there"
        )
    if status != 200 or not isinstance(body, dict) or body.get("encoding") != "base64":
        said = body.get("message") if isinstance(body, dict) else ""
        raise JoinError(f"GitHub did not return {repository}/{path} (HTTP {status}: {said})")
    return base64.b64decode(body.get("content") or "").decode(), str(body.get("sha") or "")


def check(text: str, repository: str) -> engine_config.Manifest:
    """The parsed project file, or the reason it cannot be run."""
    try:
        raw = tomllib.loads(text)
    except ValueError as error:
        raise JoinError(f"{repository}/{SHARED_PATH} is not valid TOML: {error}") from error
    report = engine_config.Report()
    manifest = engine_config.parse_manifest(raw, report)
    if not report.ok:
        problems = "; ".join(f"{p.location}: {p.what}" for p in report.problems)
        raise JoinError(f"{repository}/{SHARED_PATH} has problems: {problems}")
    return manifest


# --- this machine's half ----------------------------------------------------------------------


def _origin(folder: Path) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(folder), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _names(url: str, repository: str) -> bool:
    """Whether a remote URL is the repository's, over https or ssh, with or without ``.git``."""
    tail = re.sub(r"\.git/?$", "", url.strip().rstrip("/")).casefold()
    return re.search(rf"github\.com[:/]{re.escape(repository.casefold())}$", tail) is not None


def find_checkout(bases: list[Path], repository: str) -> Path | None:
    """The clone of ``repository`` at one of ``bases`` or one folder below it, by its origin."""
    for base in bases:
        children = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
        for folder in [base, *children] if base.is_dir() else []:
            if (folder / ".git").exists() and _names(_origin(folder), repository):
                return folder
    return None


def checkouts(
    manifest: engine_config.Manifest,
    owner: str,
    base: Path,
    have: dict[str, str] | None = None,
    search: tuple[Path, ...] = (),
) -> dict[str, str]:
    """A checkout for each repository a Work runs in and ``have`` lacks, found under ``base``
    (or ``search``) or cloned into ``base``."""
    found: dict[str, str] = {}
    for repo in manifest.repos:
        if repo.workspace or not repo.holds_code or repo.id in (have or {}):
            continue
        # An id may already name its owner (``otherorg/service``), as the deck reads it too.
        repository = repo.id if "/" in repo.id else f"{owner}/{repo.id}"
        folder = find_checkout([base, *search], repository)
        if folder is None:
            folder = base / repo.id.rsplit("/", 1)[-1]
            environment = dict(os.environ)
            environment["GIT_TERMINAL_PROMPT"] = "0"
            github_identity.configure_github_git(environment)
            setup_init._say(f"… cloning {repository} to {folder}")
            base.mkdir(parents=True, exist_ok=True)  # the default ~/<instance> may not exist yet
            done = subprocess.run(
                ["git", "clone", "--quiet", f"https://github.com/{repository}.git", str(folder)],
                env=environment,
                capture_output=True,
                text=True,
            )
            if done.returncode != 0:
                raise JoinError(f"could not clone {repository}: {done.stderr.strip()}")
        found[repo.id] = str(folder)
    return found


def _checkout_line(repo: str, path: str) -> str:
    """One ``[checkouts]`` entry, key and path quoted: a bare ``api.v2`` would be a table."""
    return f"{json.dumps(repo)} = {json.dumps(path)}"


def overlay_text(instance: Path, checkouts: dict[str, str], account: str) -> str:
    lines = "\n".join(_checkout_line(repo, path) for repo, path in checkouts.items())
    return f"""# Written by `agentdeck-project join`. This machine's half of a shared project:
# poller/reference/overlay.example.toml explains every key.
contract = 1

[paths]
state = "{instance / "state.json"}"
log = "{instance / "cycle.log"}"
extensions = "{setup_init.REPO / "poller" / "extensions"}"

[checkouts]
{lines}

[worker]
account = "{account}"

[credentials]
"""


def _overlay_checkouts(overlay: Path) -> dict[str, str]:
    try:
        table = tomllib.loads(overlay.read_text()).get("checkouts")
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in table.items()} if isinstance(table, dict) else {}


def _add_checkouts(
    overlay: Path, manifest: engine_config.Manifest, owner: str, base: Path | None
) -> None:
    """Give an existing overlay a checkout for each code repository the shared file added.

    Everything else in it (the account, credentials, extension settings) is this machine's
    own and stays as it is. ``base`` defaults to the folder the present checkouts share.
    """
    if not overlay.exists():
        return  # activation names the missing overlay itself
    have = _overlay_checkouts(overlay)
    present = [Path(path).expanduser() for path in have.values()]
    parents = [path.parent for path in present]
    if base is None:
        # Beside most of the checkouts already there: ~/shop for ~/shop/{web,api}.
        base = max(set(parents), key=parents.count) if parents else Path.home()
    search = tuple(dict.fromkeys([*present, *parents]))
    added = checkouts(manifest, owner, base.expanduser(), have, search)
    if not added:
        return
    lines = "".join(_checkout_line(repo, folder) + "\n" for repo, folder in added.items())
    text = overlay.read_text()
    if re.search(r"(?m)^\[checkouts\]\s*$", text):
        text = re.sub(r"(?m)^\[checkouts\]\s*\n", lambda m: m.group(0) + lines, text, count=1)
    else:
        text = text.rstrip("\n") + "\n\n[checkouts]\n" + lines
    overlay.write_text(text)
    setup_init._say(f"✓ added checkouts for {', '.join(added)} to {overlay}.")


def _start_timer(instance_id: str) -> None:
    """Switch the project's own wakes on, as ``agentdeck-init`` does; the deck's services are
    already running wherever a deck joins a project."""
    root = Path(poller_host.DEFAULT_ROOT).expanduser()
    if sys.platform == "darwin":
        from . import launchd

        home = Path.home()
        failures = launchd.install(
            [launchd.poller_job(setup_init.REPO, home, root, instance_id)], home
        )
        if failures:
            raise JoinError("; ".join(failures))
        return
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", poller_host.timer_unit(instance_id)],
    ):
        ran = subprocess.run(command, capture_output=True, text=True)
        if ran.returncode != 0:
            raise JoinError(f"`{' '.join(command)}` failed: {ran.stderr.strip()}")


def _account() -> str:
    from .config import config_path, load_config

    accounts = load_config(config_path()).accounts
    if not accounts:
        raise JoinError("the deck's config lists no [[accounts]]; run agentdeck-init first")
    return f"{accounts[0].provider}:{accounts[0].label}"


def _activate(
    instance_id: str, manifest: Path, instance: Path, login: str, *, extensions: bool = False
) -> None:
    applied = subprocess.run(
        [
            sys.executable,
            str(setup_init.REPO / "scripts" / "poller_engine.py"),
            "activate",
            "--instance",
            instance_id,
            "--manifest",
            str(manifest),
            "--overlay",
            str(instance / "overlay.toml"),
            "--binding",
            str(instance / "binding.json"),
            "--apply",
            login,
        ],
        text=True,
        capture_output=True,
    )
    if applied.returncode != 0:
        said = applied.stderr.strip() or applied.stdout.strip()
        if extensions:
            # Activation checks every setting before it publishes anything, so nothing is on.
            said += (
                "\nThis is the main deck, so it runs the project's extensions: add each one's "
                f"machine settings (tokens, kubeconfigs) under [extensions.<name>] in "
                f"{instance / 'overlay.toml'} — see poller/reference/overlay.example.toml — "
                "and run this again."
            )
        raise JoinError(f"turning the project on failed:\n{said}")


def _instance(instance_id: str) -> Path:
    root = Path(poller_host.DEFAULT_ROOT).expanduser()
    if not poller_host.instance_dir(root, instance_id).exists() and sys.platform != "darwin":
        poller_host.install(root, poller_host.DEFAULT_UNIT_DIR, instances=[instance_id])
    instance = poller_host.instance_dir(root, instance_id)
    instance.mkdir(parents=True, exist_ok=True)
    return instance


def _keep(instance_id: str, text: str) -> Path:
    manifest = setup_init.PROJECTS.expanduser() / instance_id / "poller.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(text)
    return manifest


def join(
    repository: str, *, instance_id: str, base: Path, path: str = SHARED_PATH, opener=None
) -> None:
    text, sha = fetch(repository, path, opener)
    manifest = check(text, repository)
    person = github_authority.deck_person()
    if manifest.main_deck and not person:
        raise JoinError(
            f"{repository}'s project names {manifest.main_deck}'s deck as the main one, and this "
            'deck names no person: add [deck] person = "<your GitHub login>" to '
            "~/.config/agentdeck/github-authority.toml first"
        )
    owner = repository.split("/", 1)[0]
    instance = _instance(instance_id)
    joined = read_source(instance)
    if joined is not None and (joined.repository.lower(), joined.path) != (
        repository.lower(),
        path,
    ):
        raise JoinError(
            f"the instance {instance_id} already runs {joined.repository}/{joined.path}; "
            "join this project under another name with --instance <name>"
        )
    overlay = instance / "overlay.toml"
    if overlay.exists():
        setup_init._say(f"✓ {overlay} is already there; keeping its settings.")
        _add_checkouts(overlay, manifest, owner, base)
    else:
        found = checkouts(manifest, owner, base.expanduser())
        overlay.write_text(overlay_text(instance, found, _account()))
        setup_init._say(f"✓ wrote {overlay}.")
    if manifest.main_deck and person != manifest.main_deck:
        setup_init._say(
            f"✓ {manifest.main_deck}'s deck is the main one: this deck takes only issues "
            f"assigned to {person} and runs none of the project's extensions."
        )
    login = person or _activator(instance)
    if not login:
        raise JoinError("name the person who approves this project in [deck] person first")
    manifest_file = _keep(instance_id, text)
    _activate(instance_id, manifest_file, instance, login, extensions=bool(manifest.extensions))
    write_source(instance, Source(repository, path, sha))
    _start_timer(instance_id)
    setup_init._say(f"✓ {instance_id} now runs {repository}/{path} at {sha[:12]}, and wakes.")


def _activator(instance: Path) -> str:
    try:
        login = json.loads((instance / "activation.json").read_text()).get("activated_by")
    except (OSError, ValueError, AttributeError):
        return ""
    return login if isinstance(login, str) else ""


def update(instance_id: str, opener=None) -> bool:
    """Apply the shared file again if it moved; ``False`` when it had not."""
    instance = poller_host.instance_dir(Path(poller_host.DEFAULT_ROOT).expanduser(), instance_id)
    source = read_source(instance)
    if source is None:
        raise JoinError(f"{instance_id} was not joined from a repository; run project join first")
    text, sha = fetch(source.repository, source.path, opener)
    if sha == source.sha:
        setup_init._say(f"✓ {instance_id} already runs {source.repository} at {sha[:12]}.")
        return False
    manifest = check(text, source.repository)
    login = github_authority.deck_person() or _activator(instance)
    if not login:
        raise JoinError("name the person who approves this project in [deck] person first")
    _add_checkouts(instance / "overlay.toml", manifest, source.repository.split("/", 1)[0], None)
    _activate(
        instance_id, _keep(instance_id, text), instance, login, extensions=bool(manifest.extensions)
    )
    write_source(instance, Source(source.repository, source.path, sha))
    setup_init._say(f"✓ {instance_id} now runs {source.repository} at {sha[:12]}.")
    return True


# --- command line -------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentdeck-project")
    commands = parser.add_subparsers(dest="command", required=True)
    joining = commands.add_parser("join", help="run a project from its repository's file")
    joining.add_argument("repository", help="owner/repo holding .agentdeck/poller.toml")
    joining.add_argument("--instance", help="the poller instance name (default: the owner)")
    joining.add_argument(
        "--checkouts",
        help="the folder holding (or to hold) the project's clones (default: ~/<instance>)",
    )
    joining.add_argument("--path", default=SHARED_PATH, help=argparse.SUPPRESS)
    updating = commands.add_parser("update", help="apply the repository's file again")
    updating.add_argument("instance")
    args = parser.parse_args(argv)
    try:
        if args.command == "join":
            instance_id = (args.instance or args.repository.split("/", 1)[0]).lower()
            base = Path(args.checkouts or f"~/{instance_id}")
            join(args.repository, instance_id=instance_id, base=base, path=args.path)
        else:
            update(args.instance)
    except JoinError as error:
        print(f"agentdeck-project: {error}", file=sys.stderr)
        return 1
    return 0


def run() -> None:
    raise SystemExit(main(sys.argv[1:]))
