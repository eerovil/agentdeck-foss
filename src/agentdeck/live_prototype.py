"""Live prototyping (#3569): one person, usually on a phone, talks to the agent.

They watch the prototype on the deck's live prototype page, and say what to change
(#3811: it is usually just the owner, not a meeting watching a shared screen).
The agent keeps the real app running on a dev server and tells the deck its link, its
versions and each change it makes. The live prototype page (``/live/<session key>``)
shows the prototype in a frame, reloads it after each change, lists the changes with a
"Go back" for each, switches between versions, and sends suggestions to the chat.

A diagram (#3827) has a slot of its own beside the prototype's link, so a chat that
prototypes and draws keeps both, and a Work's page opens the diagram from the same record.

The record is one JSON file per chat in ``LIVE_DIR``, keyed by the chat's identity
rather than its leg, so a chat that continues into a new leg keeps its prototype, and a
deck restart midway loses nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

LIVE_DIR = Path("~/.local/share/agentdeck/live-prototypes")
MAX_STEPS = 200
MAX_VERSIONS = 8
MAX_SUMMARY_CHARS = 300
MAX_NAME_CHARS = 60
MAX_URL_CHARS = 2000
MAX_USE_CASES = 30
MAX_USE_CASE_STEPS = 20

# The session guide carries only the trigger; the procedure is read on demand, so
# chats that never prototype live do not pay for it.
GUIDE = """\
Live prototyping: one person (usually the boss, on a phone) talks to you and watches
the deck's live prototype page while you change the prototype. Keep pace with them.

This is a prototype, so speed comes first. Build only what the person sees and does:
the screens, the clicks and the flow of each use case, with made-up data in the page.
Build no backend (models, migrations, APIs, real integrations) unless it makes a use
case easier to check, and then the least that does. The real build comes later, from
the spec.

The prototype opens with no login: anyone with the link comes straight in as a made-up
user (a dev-only auto-login in the prototype's worktree or in a proxy in front of it).
Never make the person log in.

Do not take screenshots or drive a browser to check a change; the person sees it on
the page and says what is wrong. Check only that the page still loads (one request).

1. Start. Make a worktree of <repo> for the prototype (never edit the live checkout) and
   start the real app on its own dev server on this host, with data that makes it look
   used, and set up the no-login access. Serve it over the tailnet:
   `tailscale serve --bg --https=<port> http://127.0.0.1:<port>`. Open the link yourself
   and check it loads with no login.
2. Register it: `PUT $AGENTDECK_URL/api/sessions/<session-key>/live-prototype` with
   `{"url": "<https link>"}`. The answer carries `page_url`; send that link in your
   reply. It is the live prototype page the person watches.
3. Each change: commit it in the prototype's worktree (one commit per change), make sure
   the dev server serves it, then record it with
   `POST $AGENTDECK_URL/api/sessions/<session-key>/live-prototype/steps` and
   `{"summary": "<what changed, a few words>", "commit": "<sha>"}`. The live prototype
   page reloads the prototype by itself when a step is recorded. Answer briefly in the chat.
4. Several suggestions waiting: when you start a change, fold every suggestion that has
   arrived since your last one into it, rather than one change each.
   The deck delivers each one as its own message; merging them is yours to do.
5. Competing ideas: build each as a version reachable at its own link (a query
   parameter is enough), and register them all:
   `PUT .../live-prototype` with `{"url": "<link>", "versions": [{"name": "A",
   "url": "<link A>"}, {"name": "B", "url": "<link B>"}]}`. The page shows a switch.
6. "Go back to step N": restore that step's commit as a new commit (never rewrite
   history) and record it as a new step that says which step it went back to.
7. Use cases: list each thing a person will do with the feature as a use case, so the
   person can walk through it on the page: `POST .../live-prototype/use-cases` with
   `{"title": "<who does what>", "steps": ["<step>", ...], "version": "<name>",
   "url": "<link where it starts>"}` (`version` and `url` are optional). Change one
   with `PUT .../live-prototype/use-cases/<id>` (same body), drop one with `DELETE`.
   People on the page add and edit them too, so read the list with
   `GET .../live-prototype` before you change one. Keep them true to the prototype:
   a change that alters a use case's steps updates that use case.
8. "Write the spec from what we picked: version <name> (<link>)": file one issue in
   <repo> (it gets the `agent` label) for that version, with what the kept steps
   changed, the use cases, and the prototype's branch, so a Work builds the real
   pull request. Reply with its link. The person picks a version with the page's switch.
   Leave the dev server running until the person says they are done; then stop it
   by PID and `tailscale serve --https=<port> off`.

Diagrams (#3822): a diagram is a file in the repository it describes, drawn by the
deck and shown on the same live page. No dev server and no tailnet link: the deck
draws it.

1. Read the code or the plan first. Work in `.worktrees/diagram-<name>` on a branch
   `diagram/<name>` (inside a Work, on the Work's branch), never in the live checkout.
2. Write `docs/diagrams/<name>.md`: a `# Title`, two to four sentences on what it
   shows, one ```mermaid block, then a `## Colours` list and a `## Boxes` list.
   - Logic: `flowchart TD` with `{question?}` boxes and labelled `-- Yes -->` arrows.
     Parts of a system: `subgraph` groups; the deck gives each group its own colour.
   - Colour boxes by what they do with `class <ids> <colour>`, using only blue,
     violet, teal, amber, rose or green (never a hex `classDef`), and say what each
     colour means in `## Colours` as `- **amber**: <meaning>`.
   - Explain the boxes worth explaining in `## Boxes` as
     `- **<box text exactly as drawn>**: <what it does, with the file and function>`.
     The deck shows it when that box is tapped.
   - Label boxes in the language the person wrote in. Keep one diagram under about
     40 boxes; split a bigger one into an overview and parts.
3. Commit it, then put the deck's full-screen view of it on the live page:
   `PUT .../live-prototype` with
   `{"diagram": "<deck address>/<absolute path of the file>?bare=1"}`, the deck address
   being the one file links use. The diagram has its own place on the page, beside any
   prototype link, so `url` is left alone. Record each change as a step, as for a
   prototype, and send the `page_url` link in your reply. A suggestion that starts with
   `About the box "<text>" in the diagram:` was written with that box tapped: change
   that box, its arrows or its note.
4. Push the branch through the repository's normal flow when the person wants to
   share it; GitHub draws the same file.

Inside a Work, work on the Work's branch in its worktree and commit the diagram in the
Work's pull request; the Work page's Diagram button opens your live page on it.
"""

#: Where a repository keeps its diagrams (#3822); a Work's Diagram button (#3827) opens
#: only a file in it.
DIAGRAM_DIR = "docs/diagrams/"


def _path(chat_key: str) -> Path:
    return LIVE_DIR.expanduser() / f"{quote(chat_key, safe='')}.json"


def page_url(session_key: str) -> str:
    return f"/live/{quote(session_key, safe=':')}"


def read(chat_key: str) -> dict | None:
    try:
        record = json.loads(_path(chat_key).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _write(chat_key: str, record: dict) -> None:
    path = _path(chat_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record))
    os.replace(temp, path)


def clean_url(value: object) -> str:
    """An http(s) link, or ValueError: the page puts it in a frame's ``src``."""
    url = value.strip() if isinstance(value, str) else ""
    parts = urlsplit(url)
    if len(url) > MAX_URL_CHARS or parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("url must be an http or https link")
    return url


def _clean_text(value: object, limit: int, what: str) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"{what} must not be empty")
    return text[:limit]


def set_link(chat_key: str, url: object, versions: object = None) -> dict:
    """Store the prototype's link and its versions; the steps so far stay."""
    record = read(chat_key) or {"steps": []}
    record["url"] = clean_url(url)
    if versions is not None:
        if not isinstance(versions, list) or len(versions) > MAX_VERSIONS:
            raise ValueError(f"versions must be a list of at most {MAX_VERSIONS}")
        record["versions"] = [
            {
                "name": _clean_text(item.get("name"), MAX_NAME_CHARS, "version name"),
                "url": clean_url(item.get("url")),
            }
            for item in versions
            if isinstance(item, dict)
        ]
    record.setdefault("versions", [])
    if not any(item["name"] == record.get("picked") for item in record["versions"]):
        record["picked"] = None
    record["revision"] = record.get("revision", 0) + 1
    record["updated_at"] = time.time()
    _write(chat_key, record)
    return record


def set_diagram(chat_key: str, url: object, repository: str = "") -> dict:
    """Store the diagram's link (#3827), kept apart from the prototype's own link, with the
    repository its file is in: the Work's button links there once the worktree is gone."""
    record = read(chat_key) or {"steps": []}
    record["diagram"] = clean_url(url)
    record["diagram_repository"] = repository
    record.setdefault("versions", [])
    record["revision"] = record.get("revision", 0) + 1
    record["updated_at"] = time.time()
    _write(chat_key, record)
    return record


def diagram_file(url: object) -> str:
    """The absolute host path a diagram link shows, or "" when it is not a deck file view
    of a ``docs/diagrams/*.md`` file (#3827)."""
    if not isinstance(url, str):
        return ""
    path = unquote(urlsplit(url).path)
    marker = "/" + DIAGRAM_DIR
    if marker not in path or not path.endswith(".md") or "/../" in path:
        return ""
    return path


def diagram_repository(file: str) -> str:
    """``owner/repo`` of the checkout holding a diagram file, from its ``origin`` remote, or
    "" when it is not a GitHub checkout or git does not answer in time."""
    from .poller_engine.workspace import github_repository

    folder = Path(file).parent
    if not folder.is_dir():
        return ""
    try:
        done = subprocess.run(
            ["git", "-C", str(folder), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (github_repository(done.stdout) or "").lower() if done.returncode == 0 else ""


#: The deck's own checkout, which the deploy keeps on ``master``: a merged agentdeck
#: diagram is read from it once the worktree it was drawn in is gone (#3827).
DECK_CHECKOUT = Path(__file__).resolve().parents[2]


def diagram_title(path: Path) -> str:
    """A diagram file's ``# Title``, or "" when it has none or cannot be read."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _number, line in zip(range(40), handle, strict=False):
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return ""


def diagram_link(
    record: dict | None, session_key: str, repository: str = ""
) -> tuple[str, str, bool] | None:
    """``(title, href, opens on GitHub)`` for a chat's diagram, shared by the Work page's
    button and the chat's chip (#3827), or None when the chat has none.

    The live page while the file is there. Once the worktree is gone, an agentdeck diagram
    opens from the deck's own checkout and any other from GitHub, in the repository the
    file was drawn in (``repository`` only when that was not recorded): only the deck's
    checkout is kept current, so another one could be stale.
    """
    from .work_release import DECK_REPOSITORY

    file = diagram_file((record or {}).get("diagram"))
    if not file:
        return None
    path = Path(file)
    relative = DIAGRAM_DIR + file.split("/" + DIAGRAM_DIR, 1)[1]
    name = path.name.removesuffix(".md")
    if path.is_file():
        return diagram_title(path) or name, page_url(session_key) + "?view=diagram", False
    repository = (record or {}).get("diagram_repository") or repository.lower()
    merged = DECK_CHECKOUT / relative
    if repository == DECK_REPOSITORY and merged.is_file():
        return diagram_title(merged) or name, quote(str(merged)) + "?bare=1", False
    if not repository:
        return None
    return name, f"https://github.com/{repository}/blob/HEAD/{relative}", True


def add_step(chat_key: str, summary: object, commit: object = "") -> dict:
    """Record one change; numbering starts at 1 and never reuses a number."""
    record = read(chat_key)
    if record is None or not (record.get("url") or record.get("diagram")):
        raise LookupError("register the prototype's link first")
    commit = commit.strip()[:64] if isinstance(commit, str) else ""
    steps = record.setdefault("steps", [])
    number = steps[-1]["number"] + 1 if steps else 1
    steps.append(
        {
            "number": number,
            "summary": _clean_text(summary, MAX_SUMMARY_CHARS, "summary"),
            "commit": commit,
            "at": time.time(),
        }
    )
    del steps[:-MAX_STEPS]
    record["revision"] = record.get("revision", 0) + 1
    record["updated_at"] = time.time()
    _write(chat_key, record)
    return record


def pick(chat_key: str, name: object) -> dict:
    """Store the version the person picked (None for the main link).

    It is the prototype's state, not one browser's: a reload shows it again, and the spec
    request names it, so the agent files the issue for the version actually picked.
    """
    record = read(chat_key)
    if record is None or not record.get("url"):
        raise LookupError("register the prototype's link first")
    if name in (None, ""):
        record["picked"] = None
    elif any(item["name"] == name for item in record.get("versions", [])):
        record["picked"] = name
    else:
        raise ValueError("no version has that name")
    record["updated_at"] = time.time()
    _write(chat_key, record)
    return record


def save_use_case(chat_key: str, data: dict, use_case_id: str | None = None) -> dict:
    """Add a use case, or replace the one named ``use_case_id``.

    The agent and the people on the live prototype page both write them, one at a time, so
    neither overwrites the other's list. A use case change does not move
    ``revision``: that would reload the prototype in everyone's frame.
    """
    record = read(chat_key) or {"steps": []}
    use_cases = record.setdefault("use_cases", [])
    version = data.get("version") or None
    if version is not None and not any(
        item["name"] == version for item in record.get("versions", [])
    ):
        raise ValueError("no version has that name")
    steps = data.get("steps")
    if not isinstance(steps, list) or len(steps) > MAX_USE_CASE_STEPS:
        raise ValueError(f"steps must be a list of at most {MAX_USE_CASE_STEPS}")
    url = data.get("url") or None
    use_case = {
        "title": _clean_text(data.get("title"), MAX_SUMMARY_CHARS, "title"),
        "steps": [_clean_text(step, MAX_SUMMARY_CHARS, "step") for step in steps],
        "version": version,
        "url": clean_url(url) if url is not None else None,
    }
    if use_case_id is None:
        if len(use_cases) >= MAX_USE_CASES:
            raise ValueError(f"a live prototype has at most {MAX_USE_CASES} use cases")
        record["next_use_case"] = record.get("next_use_case", 0) + 1
        use_case["id"] = f"u{record['next_use_case']}"
        use_cases.append(use_case)
    else:
        index = _use_case_index(use_cases, use_case_id)
        use_cases[index] = {**use_case, "id": use_case_id}
    record["updated_at"] = time.time()
    _write(chat_key, record)
    return record


def delete_use_case(chat_key: str, use_case_id: str) -> dict:
    record = read(chat_key) or {}
    use_cases = record.get("use_cases", [])
    del use_cases[_use_case_index(use_cases, use_case_id)]
    record["updated_at"] = time.time()
    _write(chat_key, record)
    return record


def _use_case_index(use_cases: list[dict], use_case_id: str) -> int:
    for index, item in enumerate(use_cases):
        if item.get("id") == use_case_id:
            return index
    raise LookupError("no use case has that id")


def spec_request(record: dict) -> str:
    """The message "Write the spec" sends: it names the picked version and its link."""
    picked = next(
        (item for item in record.get("versions", []) if item["name"] == record.get("picked")),
        None,
    )
    shown = picked or {"name": "the main link", "url": record.get("url")}
    return f"Write the spec from what we picked: version {shown['name']} ({shown['url']})."


def topic(chat_key: str) -> str:
    """The event-bus topic a live prototype page listens on."""
    return f"live_prototype:{chat_key}"
