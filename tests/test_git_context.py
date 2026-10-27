from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from agentdeck.git_context import (
    GitContext,
    GitContextResolver,
    PullRequestContext,
    github_repository,
)
from agentdeck.github_identity import credential_helper_command
from agentdeck.models import IssueRef, PullRequestRef, Session, SessionStatus


@pytest.fixture(autouse=True)
def _isolated_github_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))


def _session(
    tmp_path,
    *,
    initial_prompt=None,
    last_text=None,
    issue_url=None,
    pull_requests=(),
    issues=(),
    last_activity=None,
):
    return Session(
        key="codex:test:thread-1",
        account_key="codex:test",
        session_id="thread-1",
        status=SessionStatus.IDLE,
        cwd=tmp_path,
        title="Ship the feature",
        initial_prompt=initial_prompt,
        last_text=last_text,
        issue_url=issue_url,
        issues=issues,
        pull_requests=pull_requests,
        last_activity=last_activity,
        show_when_idle=True,
    )


def test_github_repository_parses_https_and_ssh():
    assert github_repository("https://github.com/eerovil/agentdeck.git") == "eerovil/agentdeck"
    assert github_repository("git@github.com:acmecorp/bazaar.git") == "acmecorp/bazaar"
    assert github_repository("https://example.com/eerovil/agentdeck.git") is None


async def test_resolves_merged_state_of_a_registered_pull_request(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"
    calls = []

    async def fake_run(*args):
        calls.append(args)
        if "rev-parse" in args:
            return (0, "cccccccccccccccccccccccccccccccccccccccc\n")
        if "status" in args:
            return (0, "## feature/deckhand...origin/feature/deckhand\n M src/app.py\n")
        if "remote" in args:
            return (0, "git@github.com:eerovil/agentdeck.git\n")
        if args[1:3] == ("pr", "view"):
            return (
                0,
                json.dumps(
                    {
                        "number": 91,
                        "title": "Add PR context",
                        "url": "https://github.com/eerovil/agentdeck/pull/91",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-07-15T07:00:00Z",
                        "headRefName": "feature/deckhand",
                        "baseRefName": "master",
                        "headRefOid": "d" * 40,
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path, pull_requests=(PullRequestRef("eerovil/agentdeck", 91),)
    )
    contexts = await resolver.resolve([session])

    context = contexts["codex:test:thread-1"]
    assert context.repository == "eerovil/agentdeck"
    assert context.branch == "feature/deckhand"
    assert context.dirty is True
    assert context.pull_requests[0].status == "merged"
    assert context.pull_requests[0].number == 91
    # The head commit rides the query that was already being made (#2088), and survives the
    # cache round trip, which is where the chip reads it from.
    assert context.pull_requests[0].head_sha == "d" * 40
    assert context.pull_requests[0].short_head_sha == "ddddddd"
    rehydrated = PullRequestContext.from_json(context.pull_requests[0].as_json())
    assert rehydrated is not None and rehydrated.head_sha == "d" * 40
    # Registration is the identity; the live state (merged) still comes from GitHub.
    assert any(call[1:3] == ("pr", "view") for call in calls)


async def test_chat_that_registered_nothing_has_no_pull_requests(tmp_path, monkeypatch):
    # The shared checkout sits on a feature branch that does have a PR. It
    # belongs to whichever chat registered it, not to this one.
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"
    github_calls = 0

    async def fake_run(*args):
        nonlocal github_calls
        if "rev-parse" in args:
            return (0, "cccccccccccccccccccccccccccccccccccccccc\n")
        if "status" in args:
            return (0, "## feature/other-chat...origin/feature/other-chat\n")
        if "remote" in args:
            return (0, "git@github.com:acmecorp/bazaar.git\n")
        github_calls += 1
        raise AssertionError("an unregistered chat must not ask GitHub for PRs")

    monkeypatch.setattr(resolver, "_run", fake_run)
    context = (await resolver.resolve([_session(tmp_path)]))["codex:test:thread-1"]

    assert context.branch == "feature/other-chat"
    assert context.pull_requests == ()
    assert context.pulls_complete is True
    assert github_calls == 0


async def test_explicit_issue_relationship_names_repository_without_a_checkout(
    tmp_path, monkeypatch
):
    resolver = GitContextResolver()
    resolver._git = None
    resolver._gh = None
    session = _session(
        tmp_path / "missing",
        issues=(IssueRef("eerovil/agentdeck", 961),),
    )

    context = (await resolver.resolve([session]))[session.key]

    assert context.repository == "eerovil/agentdeck"


async def test_transcript_pr_mentions_are_never_attributed_to_the_chat(
    tmp_path, monkeypatch
):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if "rev-parse" in args:
            return (0, "cccccccccccccccccccccccccccccccccccccccc\n")
        if "status" in args:
            return (0, "## master...origin/master\n")
        if "remote" in args:
            return (0, "https://github.com/eerovil/agentdeck.git\n")
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path,
        initial_prompt="Review PR #92 and acmecorp/bazaar#7 for me.",
        last_text="Opened https://github.com/eerovil/agentdeck/pull/93 earlier today.",
        issue_url="https://github.com/eerovil/agentdeck/issues/230",
    )

    context = (await resolver.resolve([session]))[session.key]

    assert context.pull_requests == ()


async def test_registered_pull_request_is_resolved_in_its_own_repository(
    tmp_path, monkeypatch
):
    # A worker whose checkout is a superproject while its PR lives in a nested
    # app repo: the registration carries the repository, so nothing is guessed.
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if "rev-parse" in args:
            return (0, "cccccccccccccccccccccccccccccccccccccccc\n")
        if "status" in args:
            return (0, "## master...origin/master\n")
        if "remote" in args:
            return (0, "https://github.com/Northwind/docker.git\n")
        if args[1:3] == ("pr", "view"):
            assert args[5] == "Northwind/abacus"
            return (
                0,
                json.dumps(
                    {
                        "number": 1657,
                        "title": "Ready for review",
                        "url": "https://github.com/Northwind/abacus/pull/1657",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergedAt": None,
                        "headRefName": "fix/deckhand",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path,
        issue_url="https://github.com/Northwind/abacus/issues/151",
        pull_requests=(PullRequestRef("Northwind/abacus", 1657),),
    )

    context = (await resolver.resolve([session]))[session.key]

    assert [pull.number for pull in context.pull_requests] == [1657]
    assert context.pulls_complete is True


async def test_unresolvable_registration_marks_the_context_incomplete(
    tmp_path, monkeypatch
):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if "rev-parse" in args:
            return (0, "cccccccccccccccccccccccccccccccccccccccc\n")
        if "status" in args:
            return (0, "## feature...origin/feature\n")
        if "remote" in args:
            return (0, "https://github.com/acme/checkout.git\n")
        if args[1:3] == ("pr", "view"):
            if args[5] == "other/repo":
                return (1, "")  # e.g. GitHub API rate limiting
            return (
                0,
                json.dumps(
                    {
                        "number": 151,
                        "title": "Checkout PR",
                        "url": "https://github.com/acme/checkout/pull/151",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergedAt": None,
                        "headRefName": "feature",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path,
        pull_requests=(
            PullRequestRef("acme/checkout", 151),
            PullRequestRef("other/repo", 151),
        ),
    )

    context = (await resolver.resolve([session]))[session.key]

    assert [pull.repository for pull in context.pull_requests] == ["acme/checkout"]
    # Every registration is an explicit claim, so a missing one is incomplete
    # context rather than proof that the chat has only one PR.
    assert context.pulls_complete is False


async def test_registered_pr_outranks_unrelated_shared_checkout_branch(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if "rev-parse" in args:
            return (0, "cccccccccccccccccccccccccccccccccccccccc\n")
        if "status" in args:
            return (0, "## newer-work...origin/newer-work\n M search.py\n")
        if "remote" in args:
            return (0, "git@github.com:acmecorp/bazaar.git\n")
        if args[1:3] == ("pr", "view"):
            return (
                0,
                json.dumps(
                    {
                        "number": 252,
                        "title": "Older completed work",
                        "url": "https://github.com/acmecorp/bazaar/pull/252",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-07-14T13:17:40Z",
                        "headRefName": "older-work",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path, pull_requests=(PullRequestRef("acmecorp/bazaar", 252),)
    )
    context = (await resolver.resolve([session]))[session.key]

    assert [pull.number for pull in context.pull_requests] == [252]
    assert context.pull_requests[0].status == "merged"
    # The checkout has moved on to another chat's branch, so local branch, dirty
    # state and the commit sitting there are not this chat's to show (#1127).
    assert context.branch is None
    assert context.dirty is False
    assert context.head_sha is None


async def test_resolves_multiple_registered_prs_for_one_chat(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if "rev-parse" in args:
            return (0, "cccccccccccccccccccccccccccccccccccccccc\n")
        if "status" in args:
            return (0, "## shared...origin/shared\n")
        if "remote" in args:
            return (0, "git@github.com:acmecorp/bazaar.git\n")
        if args[1:3] == ("pr", "view"):
            number = int(args[3])
            return (
                0,
                json.dumps(
                    {
                        "number": number,
                        "title": f"PR {number}",
                        "url": f"https://github.com/acmecorp/bazaar/pull/{number}",
                        "state": "MERGED" if number == 247 else "OPEN",
                        "isDraft": False,
                        "mergedAt": "2026-07-14T13:17:40Z" if number == 247 else None,
                        "headRefName": f"work-{number}",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path,
        pull_requests=(
            PullRequestRef("acmecorp/bazaar", 247),
            PullRequestRef("acmecorp/bazaar", 250),
        ),
    )
    context = (await resolver.resolve([session]))[session.key]

    assert [(pull.number, pull.status) for pull in context.pull_requests] == [
        (250, "open"),
        (247, "merged"),
    ]


async def test_resolves_merged_pr_after_worktree_was_removed(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if args[0] == "git":
            return (128, "")
        if args[1:3] == ("pr", "view"):
            assert args[3] == "250"
            assert args[5] == "acmecorp/bazaar"
            return (
                0,
                json.dumps(
                    {
                        "number": 250,
                        "title": "Prevent category filter panel popping",
                        "url": "https://github.com/acmecorp/bazaar/pull/250",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-07-14T13:17:40Z",
                        "headRefName": "claude/issue-246-tag-filter-last",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path / "removed-worktree",
        issue_url="https://github.com/acmecorp/bazaar/issues/246",
        pull_requests=(PullRequestRef("acmecorp/bazaar", 250),),
    )
    contexts = await resolver.resolve([session])

    context = contexts[session.key]
    assert context.repository == "acmecorp/bazaar"
    assert context.branch is None
    assert context.pull_requests[0].status == "merged"


async def test_a_failed_gh_read_is_not_retried_as_another_identity(monkeypatch):
    """No second attempt without the credential: that was the host login (#2289)."""
    resolver = GitContextResolver()
    resolver._gh = "gh"
    monkeypatch.setenv("GH_TOKEN", "stale-token")
    calls = []

    def spawn(argv, environment, timeout, *, stdin=None, keep_stderr=False):
        calls.append(environment)
        return subprocess.CompletedProcess(list(argv), 1, b"", b"")

    monkeypatch.setattr("agentdeck.gh_cli._spawn", spawn)
    code, _output = await resolver._run("gh", "pr", "list", "--repo", "eerovil/agentdeck")

    assert code == 1
    assert len(calls) == 1
    assert calls[0]["GH_TOKEN"] == "test-agent-token"


async def test_a_gh_read_naming_no_owner_is_refused_before_it_runs(monkeypatch):
    resolver = GitContextResolver()
    resolver._gh = "gh"
    calls = []

    def spawn(argv, environment, timeout, *, stdin=None, keep_stderr=False):
        calls.append(environment)
        return subprocess.CompletedProcess(list(argv), 0, b"[]", b"")

    monkeypatch.setattr("agentdeck.gh_cli._spawn", spawn)
    assert await resolver._run("gh", "pr", "list") == (1, "")
    assert calls == []


async def test_git_runs_once_with_no_token_and_the_agentdeck_helper(monkeypatch):
    """Only gh reads GH_TOKEN; git gets AgentDeck's credential helper instead (#2289)."""
    resolver = GitContextResolver()
    resolver._gh = "gh"
    resolver._git = "git"
    monkeypatch.setenv("GH_TOKEN", "stale-token")
    calls = []

    def spawn(argv, environment, timeout, *, stdin=None, keep_stderr=False):
        calls.append(environment)
        return subprocess.CompletedProcess(list(argv), 1, b"", b"")

    monkeypatch.setattr("agentdeck.gh_cli._spawn", spawn)
    assert await resolver._run("git", "status") == (1, "")
    assert len(calls) == 1
    assert "GH_TOKEN" not in calls[0]
    assert credential_helper_command() in calls[0].values()


async def test_explicit_pr_failure_retains_cached_context_and_retries(tmp_path, monkeypatch):
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"
    resolver._metadata.OPEN_TTL_S = 0
    resolver._metadata.FAILURE_BACKOFF_S = 0
    github_calls = 0

    async def fake_run(*args):
        nonlocal github_calls
        if "rev-parse" in args:
            return (0, "cccccccccccccccccccccccccccccccccccccccc\n")
        if "status" in args:
            return (0, "## master...origin/master\n")
        if "remote" in args:
            return (0, "https://github.com/eerovil/agentdeck.git\n")
        if args[1:3] == ("pr", "view"):
            github_calls += 1
            if github_calls == 2:
                return (1, "")  # e.g. GitHub API rate limiting
            return (
                0,
                json.dumps(
                    {
                        "number": 151,
                        "title": "Keep done sessions dismissed",
                        "url": "https://github.com/eerovil/agentdeck/pull/151",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergedAt": None,
                        "headRefName": "fix/deckhand",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    session = _session(
        tmp_path, pull_requests=(PullRequestRef("eerovil/agentdeck", 151),)
    )

    first = (await resolver.resolve([session]))[session.key]
    unavailable = (await resolver.resolve([session]))[session.key]
    recovered = (await resolver.resolve([session]))[session.key]

    assert [pull.number for pull in first.pull_requests] == [151]
    assert unavailable.pull_requests == first.pull_requests
    assert unavailable.pulls_complete is False
    assert recovered.pull_requests == first.pull_requests
    assert recovered.pulls_complete is True
    assert github_calls == 3  # the failure was not negative-cached


def _open_pr_resolver(monkeypatch, counter):
    """A resolver whose only GitHub answer is one perpetually open PR.

    ``OPEN_TTL_S = 0`` makes every resolve a candidate for a live read, so what
    the tests below measure is the dormancy decision alone rather than a TTL.
    """
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"
    resolver._metadata.OPEN_TTL_S = 0

    async def fake_run(*args):
        if "rev-parse" in args:
            return (0, "cccccccccccccccccccccccccccccccccccccccc\n")
        if "status" in args:
            return (0, "## master...origin/master\n")
        if "remote" in args:
            return (0, "https://github.com/eerovil/agentdeck.git\n")
        if args[1:3] == ("pr", "view"):
            counter.append(args)
            return (
                0,
                json.dumps(
                    {
                        "number": 151,
                        "title": "Keep done sessions dismissed",
                        "url": "https://github.com/eerovil/agentdeck/pull/151",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergedAt": None,
                        "headRefName": "fix/deckhand",
                        "baseRefName": "master",
                    }
                ),
            )
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    return resolver


def _dormant_stamp(resolver):
    return datetime.now(UTC) - timedelta(seconds=resolver.DORMANT_AFTER_S + 60)


async def test_a_dormant_chat_reads_its_pull_request_once_and_then_holds_it(
    tmp_path, monkeypatch
):
    """The triage rotation reaches every resting chat, so an unconditional
    refresh bills the shared GitHub budget for chats that cannot move (#1394).

    The first read still happens — a card must never be permanently unknown —
    and every later pass reuses it.
    """
    calls = []
    resolver = _open_pr_resolver(monkeypatch, calls)
    session = _session(
        tmp_path,
        pull_requests=(PullRequestRef("eerovil/agentdeck", 151),),
        last_activity=_dormant_stamp(resolver),
    )

    first = (await resolver.resolve([session]))[session.key]
    again = (await resolver.resolve([session]))[session.key]

    assert len(calls) == 1
    assert [pull.number for pull in first.pull_requests] == [151]
    assert again.pull_requests == first.pull_requests
    assert again.pulls_complete is True


async def test_a_recently_active_chat_still_refreshes_its_pull_request(
    tmp_path, monkeypatch
):
    """Dormancy is measured, not assumed: today's work stays live."""
    calls = []
    resolver = _open_pr_resolver(monkeypatch, calls)
    session = _session(
        tmp_path,
        pull_requests=(PullRequestRef("eerovil/agentdeck", 151),),
        last_activity=datetime.now(UTC) - timedelta(minutes=5),
    )

    await resolver.resolve([session])
    await resolver.resolve([session])

    assert len(calls) == 2


async def test_a_manual_refresh_still_re_reads_a_dormant_chat(tmp_path, monkeypatch):
    calls = []
    resolver = _open_pr_resolver(monkeypatch, calls)
    session = _session(
        tmp_path,
        pull_requests=(PullRequestRef("eerovil/agentdeck", 151),),
        last_activity=_dormant_stamp(resolver),
    )

    await resolver.resolve([session])
    await resolver.resolve([session], force=True)

    assert len(calls) == 2


def _pull(status: str, *, draft: bool = False):
    from agentdeck.git_context import PullRequestContext

    return PullRequestContext(
        repository="o/r",
        number=1,
        title="t",
        url="https://github.com/o/r/pull/1",
        status=status,
        draft=draft,
    )


@pytest.mark.parametrize(
    ("status", "is_open", "is_merged", "is_terminal"),
    [
        ("open", True, False, False),
        ("merged", False, True, True),
        ("closed", False, False, True),
        # An unexpected status is neither open nor terminal, so it is never
        # silently cached as done or suppressed as resolved.
        ("locked", False, False, False),
    ],
)
def test_pull_request_status_predicates(status, is_open, is_merged, is_terminal):
    pull = _pull(status)
    assert pull.is_open is is_open
    assert pull.is_merged is is_merged
    assert pull.is_terminal is is_terminal


def test_terminal_is_not_merely_the_complement_of_open():
    # closed and merged are both terminal but only one is merged; a draft is
    # still open (draft-ness is orthogonal to status).
    assert _pull("closed").is_terminal and not _pull("closed").is_merged
    assert _pull("open", draft=True).is_open


def test_pull_is_reviewable_and_display_status():
    assert _pull("open").is_reviewable is True
    assert _pull("open", draft=True).is_reviewable is False  # a draft is not reviewable
    assert _pull("merged").is_reviewable is False
    # display_status is the chip LABEL, not a boolean: an open draft reads "draft".
    assert _pull("open").display_status == "open"
    assert _pull("open", draft=True).display_status == "draft"
    assert _pull("merged").display_status == "merged"
    assert _pull("closed").display_status == "closed"


def test_reviewable_pull_returns_first_match_or_none():
    from agentdeck.git_context import GitContext

    ctx = GitContext("r", "b", False, (_pull("merged"), _pull("open"), _pull("open", draft=True)))
    assert ctx.reviewable_pull is ctx.pull_requests[1]  # first reviewable in order
    assert GitContext("r", "b", False, (_pull("merged"), _pull("closed"))).reviewable_pull is None
    assert GitContext("r", "b", False, ()).reviewable_pull is None


def test_is_shipped_requires_prs_and_all_terminal():
    from agentdeck.git_context import GitContext

    assert GitContext("r", "b", False, (_pull("merged"), _pull("closed"))).is_shipped is True
    assert GitContext("r", "b", False, (_pull("merged"), _pull("open"))).is_shipped is False
    assert GitContext("r", "b", False, ()).is_shipped is False  # empty is NOT shipped


def test_has_merged_pr_only_counts_actual_merges():
    from agentdeck.git_context import GitContext

    assert GitContext("r", "b", False, (_pull("merged"), _pull("open"))).has_merged_pr is True
    assert GitContext("r", "b", False, (_pull("closed"), _pull("open"))).has_merged_pr is False
    assert GitContext("r", "b", False, ()).has_merged_pr is False


def test_context_json_round_trip_and_rejects_partial_payloads():
    from agentdeck.git_context import GitContext

    context = GitContext("r", "b", True, (_pull("merged"), _pull("open", draft=True)))
    assert GitContext.from_json(context.as_json()) == context

    # A payload whose PRs cannot all be decoded would look authoritative while
    # having silently lost one, so the whole context is refused.
    broken = context.as_json()
    broken["pull_requests"][0].pop("number")
    assert GitContext.from_json(broken) is None
    assert GitContext.from_json({"repository": "r"}) is None
    assert GitContext.from_json("nonsense") is None


# --- GitContexts: "an incomplete lookup is not absence", decided once --------


def _context(*statuses: str, complete: bool = True):
    from agentdeck.git_context import GitContext

    return GitContext("o/r", "b", False, tuple(_pull(s) for s in statuses), complete)


def _store(**contexts):
    from agentdeck.git_context import GitContexts

    store = GitContexts()
    store.update(contexts)
    return store


def test_an_incomplete_lookup_never_replaces_a_complete_context():
    # A gh outage resolves to pulls_complete=False. Adopting it would read
    # "the merged PR is gone" and resurrect the attention that PR retired.
    shipped = _context("merged")
    store = _store(a=shipped)

    assert store.update({"a": _context(complete=False)}) is False
    assert store.get("a") == shipped
    assert store.is_shipped("a") is True
    assert store.has_merged_pr("a") is True


def test_a_complete_lookup_replaces_an_incomplete_one():
    store = _store(a=_context(complete=False))
    merged = _context("merged")

    assert store.update({"a": merged}) is True
    assert store.get("a") == merged


def test_an_incomplete_lookup_is_accepted_for_a_key_with_no_entry():
    # A first lookup that failed is not a downgrade: partial knowledge beats none.
    unknown = _context(complete=False)
    store = _store()

    assert store.update({"a": unknown}) is True
    assert store.get("a") == unknown
    # ...and it stays honest about what it does not know.
    assert store.is_shipped("a") is False
    assert store.has_merged_pr("a") is False
    assert store.is_shipped("absent") is False
    assert store.has_merged_pr("absent") is False


def test_an_unchanged_context_reports_no_change():
    shipped = _context("merged")
    store = _store(a=shipped)

    assert store.update({"a": shipped}) is False
    assert store.update({"a": _context("open")}) is True


def test_an_incomplete_context_is_never_checkpointed_or_restored():
    from agentdeck.git_context import GitContexts

    store = _store(a=_context("merged"), b=_context(complete=False))
    payload = store.as_json()
    assert set(payload) == {"a"}

    restored = GitContexts()
    restored.restore(payload)
    assert restored.get("a") == store.get("a")

    # An incomplete payload written by an older process is refused on the way in
    # too, so the rule holds in both directions.
    restored.restore({"c": _context(complete=False).as_json()})
    restored.restore({"d": "nonsense"})
    restored.restore("nonsense")
    assert set(restored.view) == {"a"}


def test_retain_drops_ineligible_keys_and_keeps_the_rest():
    store = _store(a=_context("merged"), b=_context("open"))

    store.retain(["a", "never-seen"])

    assert set(store.view) == {"a"}
    assert store.shipped_keys() == ("a",)


def test_the_view_is_read_only():
    store = _store(a=_context("merged"))

    with pytest.raises(TypeError):
        store.view["a"] = _context("open")  # type: ignore[index]


async def test_resolves_the_checkout_s_current_commit(tmp_path, monkeypatch):
    # The commit a review verdict would be about (#1127), read from the same
    # checkout as branch and dirty state.
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"

    async def fake_run(*args):
        if "rev-parse" in args:
            return (0, f"{'a' * 40}\n")
        if "status" in args:
            return (0, "## agent/issue-1127...origin/agent/issue-1127\n")
        if "remote" in args:
            return (0, "git@github.com:eerovil/agentdeck.git\n")
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    context = (await resolver.resolve([_session(tmp_path)]))["codex:test:thread-1"]

    assert context.head_sha == "a" * 40
    assert context.short_head_sha == "aaaaaaa"
    assert context.as_json()["head_sha"] == "a" * 40
    assert GitContext.from_json(context.as_json()) == context


async def test_a_commitless_or_unreadable_checkout_has_no_commit(tmp_path, monkeypatch):
    # `git rev-parse HEAD` fails in a repository with no commits yet, and anything
    # that is not a commit id is not a commit: neither may become a chip's subject.
    resolver = GitContextResolver()
    resolver._git = "git"
    resolver._gh = "gh"
    answers = [(128, "fatal: ambiguous argument 'HEAD'\n"), (0, "not-a-sha\n")]

    async def fake_run(*args):
        if "rev-parse" in args:
            return answers[0]
        if "status" in args:
            return (0, "## master...origin/master\n")
        if "remote" in args:
            return (0, "git@github.com:eerovil/agentdeck.git\n")
        raise AssertionError(args)

    monkeypatch.setattr(resolver, "_run", fake_run)
    for answer in list(answers):
        answers[0] = answer
        context = (await resolver.resolve([_session(tmp_path)]))["codex:test:thread-1"]
        assert context.head_sha is None
        assert context.short_head_sha is None
        assert context.repository == "eerovil/agentdeck"


def test_reconcile_cached_pulls_sees_what_the_webhook_wrote(tmp_path):
    """A merge recorded for free by the webhook streams must be visible here.

    Before #1697 nothing re-read this row once a session's context existed, so a
    pill kept showing the state of its first lookup. The check is a local peek:
    no GitHub call, no subprocess.
    """
    from agentdeck.git_context import PullRequestContext
    from agentdeck.github_cache import GitHubMetadata

    resolver = GitContextResolver()
    key = "pr-ref:v1:eerovil/agentdeck:4321"
    url = "https://github.com/eerovil/agentdeck/pull/4321"
    pull = PullRequestContext(
        repository="eerovil/agentdeck",
        number=4321,
        title="Ship the thing",
        url=url,
        status="open",
    )
    context = GitContext("eerovil/agentdeck", "agent/issue-4321", False, (pull,))
    row = {
        "number": 4321,
        "title": "Ship the thing",
        "url": url,
        "state": "OPEN",
        "isDraft": False,
        "mergedAt": None,
        "headRefName": None,
        "baseRefName": None,
    }

    # No row at all is not evidence that anything moved; the ordinary TTL covers it.
    assert resolver.reconcile_cached_pulls(context) is None

    resolver._metadata.offer(key, GitHubMetadata(row, terminal=False))
    assert resolver.reconcile_cached_pulls(context) is None

    resolver._metadata.offer(
        key,
        GitHubMetadata(
            dict(row, state="CLOSED", mergedAt="2026-09-08T06:21:50Z"), terminal=True
        ),
    )
    reconciled = resolver.reconcile_cached_pulls(context)
    assert reconciled is not None
    assert reconciled.pull_requests[0].status == "merged"
    # Only the pull requests are reconciled: no webhook can speak for the checkout.
    assert reconciled.branch == "agent/issue-4321"
    assert reconciled.repository == "eerovil/agentdeck"


def test_reconcile_cached_pulls_many_reads_the_cache_once_and_agrees(tmp_path, monkeypatch):
    """#2841: Deckhand reconciles every held context each pass, now in one cache read, and
    each context comes back exactly as reconciling it alone would give it."""
    from agentdeck.git_context import PullRequestContext
    from agentdeck.github_cache import GitHubMetadata

    resolver = GitContextResolver()

    def pull(number: int) -> PullRequestContext:
        return PullRequestContext(
            repository="eerovil/agentdeck",
            number=number,
            title="Ship it",
            url=f"https://github.com/eerovil/agentdeck/pull/{number}",
            status="open",
        )

    def row(number: int, **changes) -> dict:
        return {
            "number": number,
            "title": "Ship it",
            "url": f"https://github.com/eerovil/agentdeck/pull/{number}",
            "state": "OPEN",
            "isDraft": False,
            "mergedAt": None,
            "headRefName": None,
            "baseRefName": None,
            **changes,
        }

    contexts = {
        "merged": GitContext("eerovil/agentdeck", "a", False, (pull(1), pull(2))),
        "same": GitContext("eerovil/agentdeck", "b", False, (pull(3),)),
        "uncached": GitContext("eerovil/agentdeck", "c", False, (pull(4),)),
        "none": GitContext("eerovil/agentdeck", "d", False, ()),
    }
    resolver._metadata.offer(
        "pr-ref:v1:eerovil/agentdeck:2",
        GitHubMetadata(row(2, state="CLOSED", mergedAt="2026-09-08T06:21:50Z"), terminal=True),
    )
    resolver._metadata.offer(
        "pr-ref:v1:eerovil/agentdeck:3", GitHubMetadata(row(3), terminal=False)
    )
    alone = {
        name: moved
        for name, context in contexts.items()
        if (moved := resolver.reconcile_cached_pulls(context)) is not None
    }

    def refuse(_key):
        raise AssertionError("one read for all the contexts, not one per pull request")

    monkeypatch.setattr(resolver._metadata, "peek", refuse)
    together = resolver.reconcile_cached_pulls_many(contexts)

    assert together == alone
    assert set(together) == {"merged"}
    assert [p.status for p in together["merged"].pull_requests] == ["open", "merged"]
    assert resolver.reconcile_cached_pulls_many({}) == {}
