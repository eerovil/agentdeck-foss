import subprocess

import pytest

from agentdeck import kanban


def test_parse_ref_basic():
    ref = kanban.parse_ref("Run the kanban-worker skill for Northwind/store#2728.")
    assert ref is not None
    assert (ref.owner, ref.repo, ref.number, ref.mode) == (
        "Northwind",
        "store",
        2728,
        None,
    )
    assert ref.key == "Northwind/store#2728"
    assert ref.short == "store#2728"


def test_parse_ref_bazaar_variant():
    ref = kanban.parse_ref("Run the kanban-worker-bazaar skill for acmecorp/bazaar#244.")
    assert ref is not None
    assert (ref.owner, ref.repo, ref.number) == ("acmecorp", "bazaar", 244)


def test_parse_ref_captures_mode():
    ref = kanban.parse_ref(
        "Run the kanban-worker skill for Northwind/issues#56 in REVIEW mode: "
        "run the review-fix-pr skill ..."
    )
    assert ref is not None and ref.number == 56 and ref.mode == "review"

    ref = kanban.parse_ref(
        "Run the kanban-worker skill for Northwind/store#2716 in MERGE-FIX mode: ..."
    )
    assert ref is not None and ref.mode == "merge-fix"

    ref = kanban.parse_ref(
        "Run the kanban-worker skill for Northwind/issues#53, "
        "RESUMING from the existing worktree ..."
    )
    assert ref is not None and ref.mode == "resume"


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("REVIEW", "review"),
        ("MERGE-FIX", "merge-fix"),
        ("MERGE-ARM", "merge-arm"),
        ("RESUMING", "resume"),
        ("REVIEWER then MERGE-ARM", "merge-arm"),
        ("XREVIEW and RESUMING", "resume"),
        (("REVIEWER " * 4_000) + "MERGE-ARM", "merge-arm"),
        ("éREVIEW and REVIEWé", None),
        ("_REVIEW and REVIEW_", None),
        ("1REVIEW and REVIEW1", None),
        ("RESUMING before REVIEW", "resume"),
    ],
)
def test_parse_ref_mode_keeps_regex_boundaries_and_first_match(suffix, expected):
    ref = kanban.parse_ref(
        "You are working eerovil/agentdeck#1329 for the AgentDeck poller. " + suffix
    )
    assert ref is not None and ref.mode == expected


def test_parse_ref_finds_a_mode_late_in_a_long_dispatch_prompt():
    prompt = (
        "You are working eerovil/agentdeck#1329 for the AgentDeck poller.\n"
        + "method detail\n" * 4_000
        + "MERGE-FIX"
    )
    assert kanban.parse_ref(prompt).mode == "merge-fix"


def test_parse_ref_ignores_non_kanban():
    assert kanban.parse_ref("Look at agentdeck project") is None
    assert kanban.parse_ref("") is None
    assert kanban.parse_ref(None) is None


def test_parse_ref_memoizes_repeated_prompts():
    kanban.parse_ref.cache_clear()
    prompt = "You are working eerovil/agentdeck#1315 for the AgentDeck poller."
    assert kanban.parse_ref(prompt) == kanban.parse_ref(prompt)
    assert kanban.parse_ref.cache_info().hits == 1


def test_issue_url():
    ref = kanban.parse_ref("Run the kanban-worker skill for Northwind/store#2728.")
    assert kanban.issue_url(ref) == "https://github.com/Northwind/store/issues/2728"
    bazaar = kanban.parse_ref("Run the kanban-worker-bazaar skill for acmecorp/bazaar#244.")
    assert kanban.issue_url(bazaar) == "https://github.com/acmecorp/bazaar/issues/244"


def test_status_label():
    assert kanban.status_label(None) is None
    assert kanban.status_label({}) is None
    assert kanban.status_label({"state": "open", "is_pr": False}) == ("open", "open")
    assert kanban.status_label({"state": "open", "is_pr": True}) == ("open", "open")
    # PRs: merged vs closed-unmerged.
    assert kanban.status_label({"state": "closed", "is_pr": True, "merged": True}) == (
        "merged", "merged")
    assert kanban.status_label({"state": "closed", "is_pr": True, "merged": False}) == (
        "closed", "closed")
    # Issues: completed (done) vs not-planned (dropped).
    assert kanban.status_label(
        {"state": "closed", "is_pr": False, "state_reason": "completed"}) == ("closed", "done")
    assert kanban.status_label(
        {"state": "closed", "is_pr": False, "state_reason": "not_planned"}) == ("closed", "dropped")


def test_format_title():
    ref = kanban.parse_ref("Run the kanban-worker skill for Northwind/store#2728.")
    assert kanban.format_title(ref, "Fix intro text duplication") == (
        "store#2728 · Fix intro text duplication"
    )
    # Unresolved title falls back to the bare reference.
    assert kanban.format_title(ref, None) == "store#2728"


def test_format_title_with_mode():
    ref = kanban.parse_ref(
        "Run the kanban-worker skill for Northwind/issues#56 in REVIEW mode: ..."
    )
    assert kanban.format_title(ref, "Improve chatbot") == "issues#56 · Improve chatbot (review)"


def test_cache_persists_and_dedupes(tmp_path, monkeypatch):
    path = tmp_path / "kanban_titles.json"
    cache = kanban.KanbanTitleCache(path=path)
    cache._gh = "/usr/bin/gh"  # pretend gh exists

    calls: list[str] = []

    async def fake_fetch(ref):
        calls.append(ref.key)
        return (ref, {"title": f"Title for {ref.short}", "state": "open", "is_pr": False})

    monkeypatch.setattr(cache, "_fetch", fake_fetch)

    ref = kanban.parse_ref("Run the kanban-worker skill for Northwind/store#2728.")
    dup_mode = kanban.parse_ref(
        "Run the kanban-worker skill for Northwind/store#2728 in REVIEW mode: ..."
    )

    import asyncio

    # Two refs to the same issue → one fetch; title + state land in the cache/disk.
    changed = asyncio.run(cache.resolve_missing([ref, dup_mode], now=1000.0))
    assert changed is True
    assert calls == ["Northwind/store#2728"]
    assert cache.get(ref) == "Title for store#2728"
    assert cache.get_status(ref) == ("open", "open")

    # A fresh instance reads the persisted record without re-fetching.
    reloaded = kanban.KanbanTitleCache(path=path)
    reloaded._gh = "/usr/bin/gh"
    assert reloaded.get(ref) == "Title for store#2728"
    assert reloaded.get_status(ref) == ("open", "open")
    # Open state re-polls after OPEN_TTL_S, not before.
    assert asyncio.run(reloaded.resolve_missing([ref], now=1000.0 + 60)) is False


def test_a_read_keeps_the_parent_a_sub_issue_delivery_set(tmp_path, monkeypatch):
    """#3654: GitHub's issue read never carries ``parent_issue_url``, so a refresh keeps the
    link the ``sub_issues`` delivery wrote, and a read that names one still wins."""
    import asyncio

    from agentdeck.github_cache import GitHubMetadata

    cache = kanban.KanbanTitleCache(path=tmp_path / "k.json")
    cache._gh = "/usr/bin/gh"
    ref = kanban.parse_ref("Run the kanban-worker skill for Northwind/store#3.")
    parent = "https://api.github.com/repos/Northwind/store/issues/9"
    cache._metadata.offer(
        cache._cache_key(ref), GitHubMetadata({"title": "T", "state": "open", "parent": parent})
    )
    read = {"title": "T", "state": "open"}

    async def fake_fetch(ref):
        return (ref, dict(read))

    monkeypatch.setattr(cache, "_fetch", fake_fetch)
    assert asyncio.run(cache.refresh([ref], now=1.0, limit=1)) == 1
    assert cache._record(ref)["parent"] == parent
    read["parent"] = None
    asyncio.run(cache.refresh([ref], now=2.0, limit=1))
    assert cache._record(ref)["parent"] is None


def test_negative_cache_retries_after_ttl(tmp_path, monkeypatch):
    cache = kanban.KanbanTitleCache(path=tmp_path / "k.json")
    cache._gh = "/usr/bin/gh"

    result = {"rec": None}

    async def fake_fetch(ref):
        return (ref, result["rec"])

    monkeypatch.setattr(cache, "_fetch", fake_fetch)
    ref = kanban.parse_ref("Run the kanban-worker skill for Northwind/issues#1.")

    import asyncio

    # Miss is cached; not retried before NEG_TTL_S.
    assert asyncio.run(cache.resolve_missing([ref], now=1.0)) is True
    assert cache.get(ref) is None
    assert asyncio.run(
        cache.resolve_missing([ref], now=cache.FAILURE_BACKOFF_S)
    ) is False

    # After the transient-failure backoff it retries, and now succeeds.
    result["rec"] = {"title": "Recovered title", "state": "closed", "state_reason": "completed"}
    recovered_at = 1.0 + cache.FAILURE_BACKOFF_S + 1
    assert asyncio.run(cache.resolve_missing([ref], now=recovered_at)) is True
    assert cache.get(ref) == "Recovered title"
    assert cache.get_status(ref) == ("closed", "done")
    # Terminal state uses the long TTL: past OPEN_TTL_S but under TERMINAL_TTL_S
    # it must NOT re-poll (an open issue would have).
    later = recovered_at + cache.OPEN_TTL_S + 60
    assert cache.OPEN_TTL_S < (later - recovered_at) < cache.TERMINAL_TTL_S
    assert asyncio.run(cache.resolve_missing([ref], now=later)) is False


def test_resolve_noop_without_gh(tmp_path):
    cache = kanban.KanbanTitleCache(path=tmp_path / "k.json")
    cache._gh = None
    ref = kanban.parse_ref("Run the kanban-worker skill for Northwind/store#9.")

    import asyncio

    assert asyncio.run(cache.resolve_missing([ref], now=0.0)) is False
    assert cache.get(ref) is None


# --- every dispatch shape a worker is actually given (#310) ---

def test_parse_ref_reads_the_pollers_own_dispatch_line():
    ref = kanban.parse_ref(
        "You are working eerovil/agentdeck#301 for the AgentDeck poller.\n\n"
        "Your worktree — your only workspace, already your cwd: /home/me/agentdeck/..."
    )
    assert ref is not None
    assert (ref.owner, ref.repo, ref.number) == ("eerovil", "agentdeck", 301)
    assert ref.resolvable
    assert kanban.issue_url(ref) == "https://github.com/eerovil/agentdeck/issues/301"


def test_parse_ref_accepts_the_engines_ownerless_card_reference():
    """The engine names a card `repo#number`. Unqualified it cannot be resolved or
    linked, but `agentdeck#301` is still a title, and the boilerplate is not."""
    ref = kanban.parse_ref("You are working agentdeck#301 for the AgentDeck poller.")
    assert ref is not None
    assert (ref.owner, ref.repo, ref.number) == (None, "agentdeck", 301)
    assert not ref.resolvable
    assert kanban.issue_url(ref) is None
    assert kanban.format_title(ref, None) == "agentdeck#301"


def test_parse_ref_reads_a_codex_workers_assignment_block():
    ref = kanban.parse_ref(
        "You are an autonomous **AgentDeck kanban worker** — a local Codex session. "
        "Implement exactly one\nGitHub issue, open a pull request, then stop. Nothing else.\n\n"
        "## Assignment\n"
        "- Repo: eerovil/agentdeck\n"
        "- Issue: #290 — Requested changes\n"
        "- Your worktree (your ONLY workspace, already your cwd): /home/me/agentdeck/..."
    )
    assert ref is not None
    assert (ref.owner, ref.repo, ref.number) == ("eerovil", "agentdeck", 290)


async def test_a_worker_card_is_named_after_its_issue_despite_a_stale_token(
    tmp_path, monkeypatch
):
    """The web process may inherit a PAT that 404s on the repository. The card must
    still read the issue (#544) — as the owner's authority, never by retrying as the
    host login (#2289)."""
    monkeypatch.setenv("GH_TOKEN", "poller-token-without-access")
    cache = kanban.KanbanTitleCache(path=tmp_path / "k.json")
    cache._gh = "/usr/bin/gh"
    title = "When kanban worker starts working, set a title that says something"
    answers = [(0, b'{"title": "%s", "state": "open"}' % title.encode())]
    environments: list[dict[str, str] | None] = []

    def spawn(argv, environment, timeout, *, stdin=None, keep_stderr=False):
        environments.append(environment)
        code, stdout = answers.pop(0)
        return subprocess.CompletedProcess(list(argv), code, stdout, b"")

    monkeypatch.setattr("agentdeck.gh_cli._spawn", spawn)
    ref = kanban.parse_ref("You are working eerovil/agentdeck#544 for the AgentDeck poller.")

    assert await cache.resolve_missing([ref], now=1000.0) is True
    assert len(environments) == 1
    assert environments[0]["GH_TOKEN"] == "test-agent-token"
    assert kanban.format_title(ref, cache.get(ref)) == f"agentdeck#544 · {title}"


def test_an_ownerless_ref_is_never_asked_about(tmp_path):
    """`gh api repos/None/agentdeck/issues/301` is not a question worth asking."""
    cache = kanban.KanbanTitleCache(tmp_path / "cache.json")
    ref = kanban.parse_ref("You are working agentdeck#301 for the AgentDeck poller.")
    assert ref is not None

    import asyncio

    async def boom(_ref):  # any fetch attempt is the failure
        raise AssertionError("resolution was attempted for an ownerless ref")

    cache._fetch = boom  # type: ignore[method-assign]
    assert asyncio.run(cache.resolve_missing([ref], 0.0)) is False
    assert cache.get(ref) is None
    assert cache.get_status(ref) is None


# --- the one spelling the deck and a poller engine can be compared in (#552) ---

def test_a_session_issue_url_reads_back_as_the_card_a_poller_names():
    assert kanban.card_ref(
        "https://github.com/eerovil/agentdeck/issues/549"
    ) == "eerovil/agentdeck#549"
    # The issue form redirects for a PR number, but the PR form is a valid link too.
    assert kanban.card_ref(
        "https://github.com/EeroVil/AgentDeck/pull/551"
    ) == "eerovil/agentdeck#551"


def test_anything_that_is_not_a_github_card_url_names_no_card():
    for value in (None, "", "not a url", "https://example.com/eerovil/agentdeck/issues/1",
                  "https://github.com/eerovil/agentdeck", "https://github.com/e/a/issues/x"):
        assert kanban.card_ref(value) is None


def test_parse_ref_ignores_a_dispatch_quoted_mid_line():
    """#2960: /dream lists past chats by title, so its prompt quotes a worker's dispatch line
    ("title: You are working eerovil/agentdeck#677 …"). That chat never worked #677, and
    reading it as a worker made a Work of a merged pull request."""
    dream = (
        "DREAM-AGENT\nYou are reading past agent chats to find the agent's mistakes.\n"
        "- chat: 617a8bdd57a60163\n"
        "  title: You are working eerovil/agentdeck#677 for the AgentDeck poller. Your\n"
        "  notes: Run the kanban-worker skill for Northwind/store#1.\n"
        "  quoted: - Repo: eerovil/agentdeck\n- Issue: #290\n"
    )
    assert kanban.parse_ref(dream) is None
    # A dispatch that opens a later line, after leading whitespace, is still read.
    ref = kanban.parse_ref("Context first.\n  You are working eerovil/agentdeck#12 for the poller.")
    assert ref is not None and ref.number == 12
