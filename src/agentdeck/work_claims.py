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

from collections.abc import Callable, Mapping, Sequence


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


async def run_gh_write(
    argv: list[str], repository: str, label: str, *, timeout: float = 30.0
) -> tuple[str, str]:
    """One ``gh`` write of a Work action: ``("done", "")``, or its outcome and why."""
    from . import gh_cli

    try:
        code, _stdout, stderr = await gh_cli.run_once(argv, timeout=timeout, repo=repository)
    except gh_cli.GitHubRefusal as exc:
        # Refused before anything ran, so pressing again after fixing it is safe.
        return "failed", str(exc)
    except (OSError, TimeoutError) as exc:
        return "uncertain", f"{label} did not complete ({exc}); it may have landed"
    if code != 0:
        message = stderr.decode("utf-8", "replace").strip() or f"gh exited {code}"
        return "uncertain", f"{label} did not report success ({message}); it may have landed"
    return "done", ""


async def start_issue(
    gh: str,
    repository: str,
    number: int,
    start_label: str,
    claims: Callable[[str, str], bool] | None,
    *,
    label: str = "Start agent",
    timeout: float = 30.0,
) -> tuple[str, str]:
    """Start an agent on an issue: its ownership label, assigned first where needed.

    The Start button (#3558) and a wayfinder ticket whose last blocker closed (#3764) both
    write this. Where another deck takes unassigned issues, it would start this one (#3731), so
    the issue is assigned first and the label's delivery already shows whose it is. Answers
    ``("done", what happened)`` or the failed write's outcome and why.
    """
    issue = f"{repository}#{number}"
    done = f"Started {issue}: it has the {start_label} label, so an agent starts on it"
    assignee = own_assignee(repository, claims)
    if assignee:
        outcome, text = await run_gh_write(
            [gh, "issue", "edit", str(number), "--repo", repository, "--add-assignee", assignee],
            repository,
            label,
            timeout=timeout,
        )
        if outcome != "done":
            return outcome, text
        done += f"; it is assigned to {assignee}"
    outcome, text = await run_gh_write(
        [gh, "issue", "edit", str(number), "--repo", repository, "--add-label", start_label],
        repository,
        label,
        timeout=timeout,
    )
    return (outcome, done) if outcome == "done" else (outcome, text)


async def take_over(
    gh: str,
    repository: str,
    number: int,
    *,
    assignees: Sequence[str],
    person: str,
    start_label: str,
    started: bool,
    timeout: float = 30.0,
) -> tuple[str, str]:
    """Take another deck's issue to this deck: Take over, or Start here (#3834).

    One edit makes ``person`` the only assignee (every other one is removed: only the first
    assignee counts, and one left in any order could leave it with another deck) and adds
    ``start_label`` to an issue no agent works on. The hand-over that follows is the
    router's (``work_router``, #3731): the other deck stops its agent, this one starts. A
    started Work also gets one comment, so the other person sees why their agent stopped; a
    failed comment is said, not undone. Answers ``("done", what happened)`` or the edit's
    outcome and why.
    """
    issue = f"{repository}#{number}"
    current = next(iter(assignees), "")
    others = [login for login in dict.fromkeys(assignees) if login and login != person]
    argv = [gh, "issue", "edit", str(number), "--repo", repository]
    if others:
        argv += ["--remove-assignee", ",".join(others)]
    argv += ["--add-assignee", person]
    if not started:
        argv += ["--add-label", start_label]
    label = "Take over" if started else "Start here"
    outcome, text = await run_gh_write(argv, repository, label, timeout=timeout)
    if outcome != "done":
        return outcome, text
    if not started:
        return "done", (
            f"Started {issue} here: it is assigned to {person} and has the {start_label} "
            "label, so an agent starts on it"
        )
    done = f"Took over {issue}: it is assigned to {person}, so "
    done += (
        f"@{current}'s deck stops its agent and "
        if current
        else "the other deck stops its agent and "
    )
    done += "this deck starts one on the same branch"
    whom = f"@{current}'s deck" if current else "another deck"
    comment, why = await run_gh_write(
        [
            gh,
            "issue",
            "comment",
            str(number),
            "--repo",
            repository,
            "--body",
            f"@{person} took this over from {whom}.",
        ],
        repository,
        "The take-over comment",
        timeout=timeout,
    )
    if comment != "done":
        done += f". {why}"
    return "done", done
