"""The host half of the webhook-quiet wake skip (#1631, built by #1673).

Immediately before a timed poller wake, the wake script asks this module one question: do
the webhook streams *prove* there is nothing new for this instance to read? Two of the
skip's three conditions are host facts and answered here; the third — a full wake recent
enough that trusting an at-most-once delivery channel stays bounded — belongs to the
engine, which also owns the `webhook_skip_s` policy knob and may decline the hint.

The rules, all pointing the same direction:

* **Uncertainty is never quiet.** A missing or stale heartbeat, an unreadable stream, a
  repository with no stream file at all, a tailer error — every one of them answers "not
  quiet", and not-quiet means the ordinary full 30-second wake. The failure mode of this
  whole feature must be more polling, because the streams are at-most-once and a dropped
  delivery looks exactly like silence (`webhook_tailer`'s own rule 1).
* **The heartbeat is the watcher's liveness, not GitHub's.** `poller_wake.heartbeat_path`
  moves at least every `poller_wake.KEEPALIVE_S` while the web process's webhook watch runs.
  It proves the plumbing that would have recorded a delivery is alive; it proves nothing
  about whether GitHub sent one, which is why the engine's `webhook_skip_s` ceiling exists.
* **One consumer per instance.** Each instance reads through its own tailer name, so
  observing a delivery advances only that instance's offsets: the delivery forces exactly
  one full wake there, and every other instance judges its own streams itself.
* **A stream must exist to be quiet.** A repository nobody's webhook has ever delivered
  for has no stream file, and its silence proves nothing — a host whose webhook ingress
  was never configured must keep full polling rather than silently stretch to the ceiling.
  The rule stands, but it now names the repository it is waiting for (#1685), so an
  operator who knows the ingress covers a dormant repository can create the empty stream
  and an instance that can never skip says so instead of being found by hand.
* **A stream is matched without regard to case.** The streams are written under the
  owner and repository spelling GitHub sends; a manifest may spell either differently,
  and on a case-sensitive filesystem that lookup missed silently — the whole feature was
  unreachable for every repository with a capital letter in it (#1685). The requested
  slug is matched against the live streams case-insensitively, and everything downstream
  uses the on-disk spelling, so offsets stay keyed to one name.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from pathlib import Path

from .github_webhook_log import Repository
from .poller_wake import heartbeat_path
from .webhook_tailer import WebhookTailer

log = logging.getLogger(__name__)

#: Matches ``config.github_webhook.state_dir``'s default. The wake script runs outside the
#: web process and has no deck config to read, so the shared default is restated here.
DEFAULT_STREAMS_ROOT = "~/.local/share/agentdeck/github-webhooks"

#: How old the watcher heartbeat may be and still count as alive. The watch's keep-alive
#: moves it every 30 seconds (`poller_wake.KEEPALIVE_S`, #2865), so 120 is three missed
#: keep-alives — past a hiccup, and still only a few additional 30-second polls of latency
#: when a dead watcher goes unnoticed.
HEARTBEAT_STALE_S = 120.0

#: Each instance's tailer consumer name, so two instances never share offsets.
CONSUMER_PREFIX = "poller-skip-"


def reason_path(instance: str, streams_root: Path | str = DEFAULT_STREAMS_ROOT) -> Path:
    """Where this instance's last announced not-quiet reason is remembered.

    Beside the tailer's own consumer state, because it is the same kind of thing: a note
    one wake leaves for the next, which a deletion only makes the next wake repeat itself.
    """
    root = Path(streams_root).expanduser()
    return root.parent / "webhook-tailers" / f"{CONSUMER_PREFIX}{instance}.reason"


def reason_is_new(
    instance: str,
    reason: str | None,
    streams_root: Path | str = DEFAULT_STREAMS_ROOT,
) -> bool:
    """Is this a different answer from the one this instance last announced?

    The wake runs every 30 seconds and `cycle.log` already carries a line per wake, so an
    unchanging reason is said once rather than 2,880 times a day. Anything that goes wrong
    reading or writing the note answers "new": the failure mode of this is a repeated log
    line, never a silent instance — which is the failure this reason exists to end.
    """
    held = "" if reason is None else reason
    path = reason_path(instance, streams_root)
    try:
        if path.read_text(encoding="utf-8") == held:
            return False
    except OSError:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(held, encoding="utf-8")
    except OSError:
        log.warning("could not record the webhook-quiet reason for %s", instance)
    return True


def heartbeat_fresh(
    streams_root: Path | str = DEFAULT_STREAMS_ROOT,
    *,
    stale_s: float = HEARTBEAT_STALE_S,
    now: float | None = None,
) -> bool:
    """Is the poller-wake watcher provably alive? Missing or unreadable means no.

    A future mtime is also not fresh: it is evidence of clock trouble, and clock trouble
    is uncertainty, which polls.
    """
    try:
        mtime = heartbeat_path(streams_root).stat().st_mtime
    except OSError:
        return False
    moment = time.time() if now is None else now
    return 0 <= moment - mtime < stale_s


def _repositories(slugs: Iterable[str]) -> list[Repository]:
    found = []
    for slug in slugs:
        owner, _, name = str(slug).partition("/")
        if owner and name:
            found.append(Repository(owner=owner, name=name))
    return found


def _live_streams(tailer: WebhookTailer) -> dict[str, Repository]:
    """Every stream on this host, keyed by its lowercased slug for matching."""
    return {stream.slug.lower(): stream for stream in tailer.streams()}


def _resolve(
    tailer: WebhookTailer, repositories: Iterable[Repository]
) -> tuple[list[Repository], list[str]]:
    """Each requested repository as its on-disk stream, plus the slugs with no stream.

    Matching ignores case; what comes back is the spelling the stream is actually stored
    under, so the tailer's offsets are keyed to one name however the manifest spells it.
    """
    live = _live_streams(tailer)
    matched: list[Repository] = []
    missing: list[str] = []
    for repository in repositories:
        stream = live.get(repository.slug.lower())
        if stream is None:
            missing.append(repository.slug)
        else:
            matched.append(stream)
    return matched, missing


def _delivery_reason(
    instance: str,
    slugs: Iterable[str],
    streams_root: Path | str = DEFAULT_STREAMS_ROOT,
) -> str | None:
    """Why these streams do not prove silence, or None when they do.

    Reads through the instance's own consumer and commits, so an observed delivery forces
    exactly one full wake and the next quiet check starts past it. The batch is drained
    rather than abandoned at the first record — offsets advance only over what was read,
    and leaving the rest unread would answer "new deliveries" on every wake forever.

    A reason — deliberately — when a named repository has no live stream: silence from a
    stream that does not exist proves nothing (see the module docstring), and a reason
    here means a full wake. Any error reads the same way.
    """
    repositories = _repositories(slugs)
    if not repositories:
        return "no usable repository slugs in the manifest"
    try:
        tailer = WebhookTailer(streams_root, consumer=f"{CONSUMER_PREFIX}{instance}")
        matched, missing = _resolve(tailer, repositories)
        if missing:
            return (
                f"no webhook stream for {', '.join(sorted(missing))} — configure the hook, "
                "create the empty stream once you know the ingress covers it, or drop the "
                "repository from the manifest"
            )
        landed = False
        for _ in tailer.deliveries(matched):
            landed = True
        tailer.commit()
        return "unread webhook deliveries on this instance's streams" if landed else None
    except Exception as error:  # noqa: BLE001 - an unreadable stream polls, never skips
        log.warning(
            "could not read the webhook streams for %s; forcing a full wake",
            instance,
            exc_info=True,
        )
        return f"could not read the webhook streams ({error})"


def new_deliveries(
    instance: str,
    slugs: Iterable[str],
    streams_root: Path | str = DEFAULT_STREAMS_ROOT,
) -> bool:
    """Has anything landed on these repositories' streams since this instance last looked?

    The boolean half of `_delivery_reason`: everything that is not provable silence — a
    missing stream, an unreadable read, no usable slug — answers true, and true is a full
    wake.
    """
    return _delivery_reason(instance, slugs, streams_root) is not None


def not_quiet_reason(
    instance: str,
    slugs: Iterable[str],
    *,
    streams_root: Path | str = DEFAULT_STREAMS_ROOT,
    now: float | None = None,
) -> str | None:
    """Why this instance's host half is not quiet, or None when it is (#1685).

    The heartbeat is checked first, so a dead watcher leaves the instance's offsets
    untouched — the backlog that accumulates meanwhile is then read by the first check
    after the watcher returns, which correctly answers "not quiet" once.

    The reason exists so the wake can say it out loud. A skip nobody can have is
    otherwise indistinguishable from a busy host, which is how a repository spelled with
    a capital letter cost every such instance the whole feature unnoticed.
    """
    if not heartbeat_fresh(streams_root, now=now):
        return "the webhook watcher heartbeat is missing or stale"
    return _delivery_reason(instance, slugs, streams_root)


def quiet(
    instance: str,
    slugs: Iterable[str],
    *,
    streams_root: Path | str = DEFAULT_STREAMS_ROOT,
    now: float | None = None,
) -> bool:
    """The host's whole quiet fact: the watcher is alive AND nothing new landed here."""
    return not_quiet_reason(instance, slugs, streams_root=streams_root, now=now) is None
