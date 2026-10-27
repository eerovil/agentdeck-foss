"""Durable deck-level identity above concrete provider session legs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

#: Sorts before every real activity stamp, so a leg that has never been seen
#: working loses to one that has.
_NEVER = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ChatIdentity:
    """One chat and its ordered provider/account session legs, oldest first."""

    chat_id: str
    legs: tuple[str, ...]

    @property
    def newest_session_key(self) -> str:
        """The leg appended last — chain history, not necessarily the live one.

        This answers "where did this chat grow last", which is what the linear
        append rules need. It is *not* the answer to "which leg is working";
        that is ``working_leg`` below, and confusing the two is #1501.
        """
        return self.legs[-1]


@dataclass(frozen=True)
class LegStanding:
    """One leg of a chat, as the deck sees it when asking which leg is working."""

    key: str
    live: bool
    activity: datetime | None = None
    failed: bool = False


def working_leg(standings: Sequence[LegStanding]) -> LegStanding | None:
    """Which leg of a chat is actually running, given the chain oldest first.

    Succession is not permanent. A chat leaves one account for another and can
    be handed straight back to the first — the poller picks an account by
    headroom and by affinity, not by chain position, and the process it left
    behind is never killed — so the leg appended last means "the leg added
    last", never "the leg that is working" (#1501). Liveness answers the real
    question unless the leg's last turn failed: that process may survive, but
    the conversation is no longer working there. Where two viable legs still
    look live the newest activity decides, because one conversation cannot be
    running in two places at once.

    Two cases still answer "the end of the chain", and both matter. A leg the
    chat has only just moved to has no activity to compare — often it has not
    even been scanned yet — so it is taken at its word as the successor, which
    is what keeps a durable link trusted before the scan that installs it. And
    with nothing live at all, the end of the chain is the rule every caller
    followed before this, and the right answer for a chat at rest.

    Liveness cannot decide this on its own, because it does not mean the same
    thing twice. A deck-owned Claude worker is live for as long as its process
    is up, hours after its last turn; a Codex thread is live only while a turn
    is actually running. Filtering to the live legs therefore deleted the leg a
    chat had just moved *to* the moment its first turn finished, and handed the
    chat straight back to the leg it had left — which the deck had already told
    to stop working (#1914). So the end of the chain always stands as a
    candidate alongside the live legs, and activity, which every provider
    records the same way, decides between them.
    """
    if not standings:
        return None
    end = standings[-1]
    if end.activity is None and not end.failed:
        return end
    # A provider process may survive a failed turn indefinitely. It remains
    # reachable, but it is not where the conversation is working and must not
    # reclaim the chat from a later leg that has already finished (#1713).
    last = len(standings) - 1
    candidates = [
        (offset, leg)
        for offset, leg in enumerate(standings)
        if not leg.failed and (leg.live or offset == last)
    ]
    if not candidates:
        return end
    return max(candidates, key=lambda pair: (pair[1].activity or _NEVER, pair[0]))[1]


class ChatLinkConflict(ValueError):
    """A requested successor would fork or merge a linear chat."""


def published_chat_reference(url: object) -> tuple[str, str] | None:
    """What a published deck URL names: ``("chat", id)`` or ``("session", key)``.

    The poller, worker briefs and issue comments all carry a chat URL that some
    earlier turn published, in whichever spelling was canonical when it was
    written. Both spellings still address the same conversation, so a follow-up
    that wants to know which chat it is continuing reads either one. Anything
    else — a GitHub link, a bare word, a transcript URL for a path this does not
    recognise — is ``None`` rather than a guess (#1267).
    """
    text = str(url or "").strip()
    if not text:
        return None
    parts = [part for part in urlsplit(text).path.split("/") if part]
    for segment, kind in (("chats", "chat"), ("sessions", "session")):
        if segment not in parts:
            continue
        position = parts.index(segment)
        if position + 1 < len(parts) and (value := unquote(parts[position + 1]).strip()):
            return (kind, value)
    return None
