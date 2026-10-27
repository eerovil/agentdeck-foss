"""Cards that may not start yet because something they declared has not closed (#640).

One rule, deliberately narrow: **a card that has never been dispatched is not started
while a blocker it declares is still open.** Everything else about it is untouched — it
keeps the ownership label somebody put there, keeps its queued instructions, keeps its
board column — so the moment the blocker closes the ordinary dispatch path picks it up
with nothing to undo.

Why the gate sits at *start* and not everywhere:

* A card already under way must never be stranded. Cold reloads, delivery retries,
  follow-up instructions and the guarded release all run on a card that has started, and a
  dependency appearing — or a blocker being reopened — mid-flight must not orphan a live
  worker or an approved merge. "Blocked issues cannot be started" is the whole claim.
* Starting is also the only moment a hold is *legible*: it is where the engine would
  otherwise take the label, publish *In progress* and spawn, so it is where a one-time
  comment can explain the wait to whoever is watching the board.

**Not proven closed counts as blocking.** The refs that reach here are the ones the port
could not show as closed — an open blocker, an unreadable one, a typo naming an issue that
does not exist. Failing closed is the safe direction: the cost is a card that waits and
says exactly which reference is holding it, against a card that starts work its author
said was not ready. A wrong reference is visible on the card and fixable by editing it out.

The other half of the same idea is a card that *had* started and then stopped because
something else has to land first (#1230). A worker reports that through ``finish blocked
--blocked-by <ref>``, and the refs it names are kept here too — as a separate hold, because
they mean something different. The start gate answers "may this begin?"; this one answers
"is the thing that stopped it out of the way yet?", and when it is, the card is handed back
to the ordinary dispatch path as a resume.

A worker naming nothing used to end the card's life all the same, because that hold was the
only thing the resume path could read — and a card held from the start by a GitHub native
dependency, the mechanism this project's own docs recommend, never leaves one. So the resume
falls back to the card's live dependencies (#1588): all of them closed, and the card is picked
back up whichever way it came to rest there. A card that declares no dependency at all still
rests in Blocked until a person says otherwise.

Pure text and record bookkeeping, stdlib only. *Which* refs block is a fact about the card
that the port derives (``CardSnapshot.blockers``), exactly as ownership and standing
approval are: GitHub's native issue dependencies and this project's written convention are
two ways of saying the same thing, and the engine reasons about neither format.
"""

from __future__ import annotations

import re

# What a written declaration looks like. The phrase has to *open* the line (bullets, block
# quotes and bold wrappers allowed) so ordinary prose that happens to contain "blocked by"
# is not a declaration, and the line must carry at least one issue reference to mean
# anything at all — "Blocked by the missing upload API" names no card and holds nothing.
_DECLARATION = re.compile(
    r"^[\s>*+-]*(?:\*\*|__)?\s*(?:blocked[\s-]?by|depends\s+on)(?:\*\*|__)?\s*:?",
    re.IGNORECASE,
)

# ``#12``, ``repo#12`` and ``owner/repo#12`` — the three shapes GitHub itself accepts.
_REFERENCE = re.compile(r"(?:(?P<owner>[\w.-]+)/)?(?P<repo>[\w.-]+)?#(?P<number>\d+)\b")

# A pasted link is the same declaration typed the long way. Pull requests count: a card
# waiting for one is the ordinary case, and GitHub serves both under the same number space.
_URL = re.compile(
    r"github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:issues|pull)/(?P<number>\d+)",
    re.IGNORECASE,
)

# An issue closed and a pull request merged are both out of the way; so is a PR closed
# unmerged, which is somebody deciding it will not happen.
RESOLVED_STATES = frozenset({"CLOSED", "MERGED"})

# A hold that names no card: GitHub listed more dependencies than the port could read, so
# something may be blocking that nobody can name yet (#684). It travels as an ordinary ref so
# the record, the fingerprint and the gate need no special case; only the notice reads it.
# It carries no ``#``, which every real ref does, so it can never collide with one.
UNREAD_DEPENDENCIES = "unread dependencies"


def is_resolved(state: str | None) -> bool:
    """Does this GitHub state prove the blocker is out of the way? Unknown never does."""
    return str(state or "").strip().upper() in RESOLVED_STATES


def declared(body: str, *, org: str, repo: str, number: int) -> tuple[str, ...]:
    """The blockers a card's body declares in writing, as engine card refs, in order.

    ``org``/``repo``/``number`` are the card's own coordinates, because a bare ``#12`` means
    this repository and a bare ``repo#12`` means this owner. A ref inside the project's own
    org is normalized to the engine's ``repo#number`` key so it reads like every other card;
    one in another org keeps its owner, since nothing else could name it.

    A card that declares *itself* is ignored rather than deadlocked forever.
    """
    mine = card_ref(repo, number)
    found: list[str] = []
    for line in (body or "").splitlines():
        if not _DECLARATION.match(line):
            continue
        for match in _URL.finditer(line):
            _add(found, _ref(org, repo, match), skip=mine)
        # Links are consumed first, then removed, so their trailing number is not also read
        # as a bare ``#12`` in the same line.
        for match in _REFERENCE.finditer(_URL.sub(" ", line)):
            _add(found, _ref(org, repo, match), skip=mine)
    return tuple(found)


def parse_refs(
    values: tuple[str, ...] | list[str], *, org: str, repo: str, number: int
) -> tuple[str, ...]:
    """Worker-supplied blocker references as engine card refs, in order, deduplicated.

    ``declared`` reads prose and therefore insists on a declaration line; here somebody typed
    ``--blocked-by`` on purpose, so every reference in the value counts. Normalization is the
    same, including a card that names itself being dropped rather than deadlocking forever.
    """
    mine = card_ref(repo, number)
    found: list[str] = []
    for value in values or ():
        text = str(value or "")
        for match in _URL.finditer(text):
            _add(found, _ref(org, repo, match), skip=mine)
        for match in _REFERENCE.finditer(_URL.sub(" ", text)):
            _add(found, _ref(org, repo, match), skip=mine)
    return tuple(found)


def card_ref(repo: str, number: int) -> str:
    """The engine's key for one card, which is also how a blocker is named."""
    return f"{repo}#{int(number)}"


def _add(found: list[str], ref: str, *, skip: str) -> None:
    if ref and ref != skip and ref not in found:
        found.append(ref)


def _ref(org: str, repo: str, match: re.Match) -> str:
    """One parsed reference as an engine card ref, or the empty string when unusable."""
    try:
        which = int(match.group("number"))
    except (TypeError, ValueError):
        return ""
    if which <= 0:
        return ""
    where = (match.group("repo") or repo).strip()
    holder = (match.group("owner") or "").strip()
    if not where:
        return ""
    if holder and holder.casefold() != (org or "").casefold():
        return f"{holder}/{where}#{which}"
    return f"{where}#{which}"


# --- durable bookkeeping on the card record ---

def record(rec: dict, refs: tuple[str, ...] | list[str]) -> None:
    """Store what currently blocks this card, or forget the hold entirely when nothing does.

    Held refs live on the record rather than being re-derived at dispatch because a card
    that could not be read this wake still has to be governed by the last thing that *was*
    read: an unreadable snapshot is not proof that a blocker closed. The announcement
    fingerprint is dropped with the hold, so a card blocked again later is explained again.
    """
    kept = [str(ref) for ref in refs or () if str(ref)]
    if kept:
        rec["blocked_by"] = kept
        return
    rec.pop("blocked_by", None)
    rec.pop("blocked_notice", None)


def holding(rec: dict) -> tuple[str, ...]:
    """What must close before this card may start; empty once it has started at all."""
    if rec.get("started"):
        return ()
    return tuple(str(ref) for ref in rec.get("blocked_by") or () if str(ref))


def announced(rec: dict, refs: tuple[str, ...] | list[str]) -> bool:
    """Has this exact hold already been explained on the card?"""
    return rec.get("blocked_notice") == fingerprint(refs)


def mark_announced(rec: dict, refs: tuple[str, ...] | list[str]) -> None:
    """Record that the hold was explained, so the next wake does not say it again."""
    rec["blocked_notice"] = fingerprint(refs)


def fingerprint(refs: tuple[str, ...] | list[str]) -> str:
    """A stable name for one hold: the same wait is announced once, a changed one again."""
    return ",".join(sorted(str(ref) for ref in refs or ()))


def notice(refs: tuple[str, ...] | list[str]) -> str:
    """The one comment a held card gets: what is holding it, and what will release it.

    A hold nobody can name is said in its own words rather than printed as a ref, because
    "waiting on `unread dependencies`" would read like a card that does not exist.
    """
    named = [str(ref) for ref in refs or () if str(ref) != UNREAD_DEPENDENCIES]
    parts = ["**Waiting on a dependency.**"]
    if named:
        listed = ", ".join(f"`{ref}`" for ref in named)
        parts.append(
            f"This card declares {listed} as blocking and it is not closed yet, so nothing "
            "has been started on it."
        )
    if UNREAD_DEPENDENCIES in {str(ref) for ref in refs or ()}:
        parts.append(
            "GitHub also lists more dependencies here than could be read this wake, so the "
            "card waits until they can be read rather than starting past one nobody saw."
            if named else
            "GitHub lists more dependencies here than could be read this wake, so nothing has "
            "been started on it rather than starting past a dependency nobody saw."
        )
    parts.append(
        "It starts on its own on the wake after the blockers close — nothing needs "
        "re-labelling. If a reference is wrong, edit it out of the issue body."
    )
    return " ".join(parts)


# --- the hold a blocked worker leaves behind (#1230) ---

def record_resume(rec: dict, refs: tuple[str, ...] | list[str]) -> None:
    """Keep what a blocked worker said has to land, or forget the hold when nothing does.

    Stored separately from ``blocked_by`` because the two holds are read at different
    moments and mean different things: one gates a start, this one arms a resume. Sharing a
    field would make a worker's report look like the issue body declared a dependency, and
    the start gate would then be re-derived away by the next reconcile that reads the body.
    """
    kept = [str(ref) for ref in refs or () if str(ref)]
    if kept:
        rec["blocked_until"] = kept
        return
    rec.pop("blocked_until", None)


def resuming(rec: dict) -> tuple[str, ...]:
    """What must close before this blocked card picks itself back up."""
    return tuple(str(ref) for ref in rec.get("blocked_until") or () if str(ref))


def unresolved(refs: tuple[str, ...] | list[str], states: dict[str, str]) -> tuple[str, ...]:
    """The refs still holding the card, given whatever states could be read this wake.

    A ref nobody could read is still holding it. That is the same fail-closed direction the
    start gate takes: the cost of being wrong is a card that waits and says which reference
    it is waiting on, against a card that resumes into a prerequisite that never landed.
    """
    return tuple(ref for ref in refs or () if not is_resolved((states or {}).get(ref)))


def resumed_notice(cleared: tuple[str, ...] | list[str], *, started: bool = True) -> str:
    """The one comment a card gets when the thing it was blocked on has landed.

    A card that never started says so plainly: "stopped because" would be a lie about a card
    that has not run yet, and the reader has to be able to tell the two apart.
    """
    listed = ", ".join(f"`{ref}`" for ref in cleared or ())
    began = (
        f"This card stopped because {listed} had to land first, and it now has. Work is being "
        "picked back up from the current base branch"
        if started else
        f"This card was waiting on {listed}, and it has landed. Work is starting on it now"
    )
    return (
        f"**The blocker has landed.** {began} — nothing needs re-labelling or a `/claude` "
        "comment. If it should have stayed put, remove the ownership label."
    )


# --- the hold a card never recorded, read back off its live dependencies (#1588) ---

def cleared_dependencies(
    declared_refs: tuple[str, ...] | list[str], still_holding: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """Every dependency this card declares, but only once none of them is still holding it.

    This is the hold a card resting in *Blocked* never wrote down. Only a worker reporting
    ``finish blocked --blocked-by`` records one, so a card blocked any other way — moved to the
    column by hand, or held from the start by a GitHub native dependency — had nothing to clear
    and was never re-examined again (#1588). Its dependencies are a fact the wake already read,
    so they can stand in for the hold it never left.

    Two properties keep this from resuming cards nobody meant to resume:

    * **A card that declares nothing is never touched.** No dependency means the card is in
      *Blocked* for a reason this cannot see — somebody parked it — and the empty answer here
      leaves it exactly where it is.
    * **One unproven ref holds the whole card**, the same fail-closed direction the start gate
      takes. ``still_holding`` is what the port could not show as closed, and an unreadable
      dependency is in it, so a wake that could not read the card's dependencies resumes nothing.
    """
    kept = tuple(str(ref) for ref in declared_refs or () if str(ref))
    if not kept or tuple(str(ref) for ref in still_holding or () if str(ref)):
        return ()
    return kept


def resume_armed(rec: dict, refs: tuple[str, ...] | list[str]) -> bool:
    """Has this exact set of cleared dependencies already picked this card back up?

    Without the fence a card whose worker reports *Blocked* again for some reason it cannot
    name would be resumed on every wake forever, because the dependencies it declares stay
    closed. A different dependency later is a different fingerprint and arms again.
    """
    return rec.get("blocked_resumed") == fingerprint(refs)


def mark_resume_armed(rec: dict, refs: tuple[str, ...] | list[str]) -> None:
    """Record that these cleared dependencies have had their one resume."""
    rec["blocked_resumed"] = fingerprint(refs)


# --- the green verification that lands on a card resting in Blocked (#2306) ---

_VERIFIED = "blocked_verified"


def note_verified(rec: dict, *, head: str, job: str = "") -> None:
    """Remember that a green verification was credited to this card since its last start.

    Kept whatever the card's column, because the worker's Blocked report and the result can
    land in either order (review of #2307). Only the next dispatch start forgets it, and only a
    card resting in *Blocked* spends it. On the card record, not in memory, so a restart between
    the result and the next full read of the card still resumes it.
    """
    sha = str(head or "").strip()
    if sha:
        rec[_VERIFIED] = {"head": sha, "job": str(job or "")}


def verified_while_blocked(rec: dict) -> dict:
    """The green verification still waiting to pick this card back up, or ``{}``."""
    held = rec.get(_VERIFIED)
    if not isinstance(held, dict) or not str(held.get("head") or ""):
        return {}
    return held


def clear_verified(rec: dict) -> None:
    rec.pop(_VERIFIED, None)


def verified_notice(head: str, *, job: str = "", refs: tuple[str, ...] = ()) -> str:
    """The one comment a Blocked card gets when its verification finishes green (#2306)."""
    ran = f"verification job `{job}`" if job else "its verification job"
    where = (
        f"`{str(head)[:12]}`, the current head of {', '.join(f'`{ref}`' for ref in refs)}"
        if refs else f"`{str(head)[:12]}`"
    )
    return (
        f"**Verification finished green.** This card is resting in Blocked, and {ran} has "
        f"passed on {where}. Work is being picked back up so the "
        "current worker can finish the report; with nothing else holding it, that is "
        "`in_review`, and the release takes it from there. Nothing needs re-labelling or a "
        "`/claude` comment. If it should have stayed put, remove the ownership label."
    )
