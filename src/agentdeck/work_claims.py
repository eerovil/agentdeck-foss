"""Which deck works an issue when two decks watch one repository (#3731).

Each deck names the person it works for (``[deck] person`` in the accounts file). An issue is
this deck's when its assignee is that person, or when nobody is assigned and this deck takes
unassigned issues in that project (``[claims] unassigned``, on by default). Only the issue's
first assignee counts — GitHub's single ``assignee`` field — so both decks, reading the same
issue, always reach opposite answers. With no person named, the assignee is ignored and every
issue is this deck's, as before.

Reassigning an issue hands it over: the deck it now belongs to starts on the event, and the
one that loses it stops its agent (``work_router``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


def assignee_of(issue: object) -> str:
    """The issue's first assignee's login, lowercased, or ``""`` (a webhook or REST issue)."""
    if not isinstance(issue, Mapping):
        return ""
    user = issue.get("assignee")
    if isinstance(user, Mapping):
        login = user.get("login")
    else:
        login = user  # a cached record keeps the bare login
    return login.strip().lower() if isinstance(login, str) else ""


def mine(assignee: str, *, person: str, takes_unassigned: bool) -> bool:
    """Whether this deck works an issue assigned to ``assignee`` ("" for nobody)."""
    person = person.strip().casefold()
    assignee = assignee.strip().casefold()
    if not person:
        return True
    if not assignee:
        return takes_unassigned
    return assignee == person


def own_assignee(repository: str, claims: Callable[[str, str], bool] | None) -> str:
    """Who to assign an issue this deck starts or files itself to, or ``""`` for nobody.

    Where this deck leaves unassigned issues to another deck, its Start agent and its New
    work assign the issue to this deck's person, or the other deck would start it.
    ``claims`` is ``mine`` bound to this deck's settings (``app.state.deck_claims``).
    """
    from .poller_engine.github_authority import deck_person

    person = deck_person()
    if not person or claims is None or claims(repository, ""):
        return ""
    return person
