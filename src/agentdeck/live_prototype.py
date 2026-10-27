"""Live prototyping during a meeting (#3569).

One person talks to the agent; the room watches the prototype through a shared screen.
The agent keeps the real app running on a dev server and tells the deck its link, its
versions and each change it makes. The deck's meeting page (``/live/<session key>``)
shows the prototype in a frame, reloads it after each change, lists the changes with a
"Go back" for each, switches between versions, and sends suggestions to the chat.

The record is one JSON file per chat in ``LIVE_DIR``, keyed by the chat's identity
rather than its leg, so a chat that continues into a new leg keeps its meeting, and a
deck restart mid-meeting loses nothing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

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
Live prototyping: one person (the boss) talks to you during a meeting; the room watches
the deck's meeting page through a shared screen. Keep pace with the room.

This is a prototype, so speed comes first. Build only what the person sees and does:
the screens, the clicks and the flow of each use case, with made-up data in the page.
Build no backend (models, migrations, APIs, real integrations) unless it makes a use
case easier to check, and then the least that does. The real build comes later, from
the spec.

The prototype opens with no login: anyone with the link comes straight in as a made-up
user (a dev-only auto-login in the prototype's worktree or in a proxy in front of it).
Never make the room log in.

Do not take screenshots or drive a browser to check a change; the room sees it on the
shared screen and says what is wrong. Check only that the page still loads (one request).

1. Start. Make a worktree of <repo> for the prototype (never edit the live checkout) and
   start the real app on its own dev server on this host, with data that makes it look
   used, and set up the no-login access. Serve it over the tailnet:
   `tailscale serve --bg --https=<port> http://127.0.0.1:<port>`. Open the link yourself
   and check it loads with no login.
2. Register it: `PUT $AGENTDECK_URL/api/sessions/<session-key>/live-prototype` with
   `{"url": "<https link>"}`. The answer carries `meeting_url`; send that link in your
   reply. It is the page the boss shares.
3. Each change: commit it in the prototype's worktree (one commit per change), make sure
   the dev server serves it, then record it with
   `POST $AGENTDECK_URL/api/sessions/<session-key>/live-prototype/steps` and
   `{"summary": "<what changed, a few words>", "commit": "<sha>"}`. The meeting page
   reloads the prototype by itself when a step is recorded. Answer briefly in the chat.
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
   room can walk through it on the page: `POST .../live-prototype/use-cases` with
   `{"title": "<who does what>", "steps": ["<step>", ...], "version": "<name>",
   "url": "<link where it starts>"}` (`version` and `url` are optional). Change one
   with `PUT .../live-prototype/use-cases/<id>` (same body), drop one with `DELETE`.
   People on the page add and edit them too, so read the list with
   `GET .../live-prototype` before you change one. Keep them true to the prototype:
   a change that alters a use case's steps updates that use case.
8. "Write the spec from what we picked: version <name> (<link>)": file one issue in
   <repo> (it gets the `agent` label) for that version, with what the kept steps
   changed, the use cases, and the prototype's branch, so a Work builds the real
   pull request. Reply with its link. The room picks a version with the page's switch.
   Leave the dev server running until the boss says the meeting is over; then stop it
   by PID and `tailscale serve --https=<port> off`.
"""


def _path(chat_key: str) -> Path:
    return LIVE_DIR.expanduser() / f"{quote(chat_key, safe='')}.json"


def meeting_url(session_key: str) -> str:
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


def add_step(chat_key: str, summary: object, commit: object = "") -> dict:
    """Record one change; numbering starts at 1 and never reuses a number."""
    record = read(chat_key)
    if record is None or not record.get("url"):
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
    """Store the version the room picked (None for the main link).

    It is meeting state, not one browser's: a reload shows it again, and the spec
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

    The agent and the people on the meeting page both write them, one at a time, so
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
            raise ValueError(f"a meeting has at most {MAX_USE_CASES} use cases")
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
    """The event-bus topic a meeting page listens on."""
    return f"live_prototype:{chat_key}"
