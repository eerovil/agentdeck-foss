"""The deck's own public address, so a session can link a file (#1838).

`/{local_path:path}` already serves any absolute path on this host, and the
session guide already tells every chat to link files that way. What no session
had was the host to put in front of the path: `owned_session_base_url` is
loopback on purpose, and the public address existed only as `request.base_url`
inside a live web request. A poller-started worker has no browser and no request,
so it could only guess a domain or drop the link.

Two sources, cheapest first:

* `AGENTDECK_PUBLIC_URL` in the web or runtime environment, for a deployment
  that would rather state its address than have one observed.
* Otherwise the last non-loopback address the web process was actually reached
  on, recorded to a small file so the runtime process — which starts sessions
  and never sees a browser request — can read it. Nothing has to be configured
  for this to work.

An observed address is only as trustworthy as the `Host` header that carried it,
which anyone who can reach the deck can set. That is the same population that
can already drive every unauthenticated agent-control route on it, so the
exposure is not new; a deployment that wants the address fixed sets the
environment variable and the observed one is never consulted.
"""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_ORIGIN_FILE = "~/.local/share/agentdeck/public-origin.json"
PUBLIC_URL_ENV = "AGENTDECK_PUBLIC_URL"
# Where the observed address is kept. Redirected in tests, which serve requests
# as `http://testserver` and must not teach this host's real deck that address.
ORIGIN_FILE_ENV = "AGENTDECK_PUBLIC_ORIGIN_FILE"

# Writing the same address on every request would be one disk write per page
# load for a value that changes approximately never, so the last write is
# remembered per process and only a genuine change touches the file.
_recorded: str | None = None


def origin_file(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(ORIGIN_FILE_ENV) or DEFAULT_ORIGIN_FILE).expanduser()


def forget_recorded_origin() -> None:
    """Drop the per-process memo of the last address written."""
    global _recorded
    _recorded = None


def normalize_origin(value: object) -> str:
    """A `scheme://host[:port]` a user-facing link can carry, or nothing.

    Loopback is rejected rather than returned: a link to `127.0.0.1` opens the
    reader's own machine, which is the failure this module exists to stop.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname.rstrip(".").lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if loopback:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def record_public_origin(value: object, *, path: str | Path | None = None) -> str:
    """Remember the address a real request reached this deck on. Returns it."""
    global _recorded
    origin = normalize_origin(value)
    if not origin or origin == _recorded:
        return origin
    target = origin_file(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"origin": origin}), encoding="utf-8")
    except OSError:
        # A deck that cannot write its own state directory still serves pages;
        # sessions fall back to the guide's "no known domain" wording.
        return origin
    _recorded = origin
    return origin


def recorded_public_origin(*, path: str | Path | None = None) -> str:
    try:
        data = json.loads(origin_file(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return normalize_origin(data.get("origin") if isinstance(data, dict) else "")


def public_origin(*, path: str | Path | None = None) -> str:
    """This deck's public address for user-facing links, or nothing."""
    configured = normalize_origin(os.environ.get(PUBLIC_URL_ENV))
    return configured or recorded_public_origin(path=path)


def public_url(local_path: str, *, path: str | Path | None = None) -> str:
    """A full link to one absolute host path, or nothing when the deck has no
    known public address."""
    origin = public_origin(path=path)
    if not origin or not local_path:
        return ""
    return f"{origin}/{local_path.lstrip('/')}"
