from typing import get_args

import pytest

from agentdeck.providers.instructions import (
    SESSION_GUIDE_OPEN,
    USE_CASE_WALKTHROUGH_LINE,
    USE_CASE_WALKTHROUGH_NOT_APPLICABLE,
    WORK_SESSION_ROLE_INSTRUCTIONS,
    SessionRole,
    agentdeck_session_instructions,
    resolve_session_role,
    session_guide_fingerprint,
    stored_session_role,
    strip_session_guide,
    wrap_session_guide,
)


def test_primary_session_guide_covers_agentdeck_action_contracts():
    guide = agentdeck_session_instructions(provider="claude_code", role="primary")
    flat = " ".join(guide.split())  # the guide is wrapped; match prose on one line

    assert "Session role: primary user-facing coordinator" in guide
    assert "least expensive configured role" in guide
    assert "scout" in guide and "investigator" in guide and "reviewer" in guide
    assert "explicit reasoning effort" in guide
    assert "role whose settings are fixed" in guide
    # Spelled out by path since #2015: a bare `agentdeck` is not on an owned shell's PATH.
    assert "~/agentdeck/.venv/bin/agentdeck delegate --account <provider:label>" in guide
    assert "codex:alt" in guide and "claude_code:main" in guide
    assert "cursor:" not in guide  # the Cursor provider was removed (#2600)
    assert "provide the task on stdin" in guide
    assert "narrowest suitable `--sandbox`" in guide
    # A primary session pays the same turns-x-context bill as a worker, so it gets the
    # same two levers: delegate by call count, and batch whatever stays here.
    assert "how many tool calls it will take" in flat
    assert "starts empty" in flat
    assert "independent tool calls together in one message" in flat
    # Self-discovery recipe so agents stop guessing a host, key, or endpoint.
    assert "`AGENTDECK_URL`" in guide and "loopback base" in guide
    assert "`AGENTDECK_SESSION_KEY`" in guide
    assert "never invent one" in guide
    assert "structured user-input" in guide
    assert "Never infer approval" in guide
    # Wayfinder's decision habits, for every agent (#3690).
    assert "the last is **None of these**" in flat
    assert "`Changes assumed: <detail> — <why>`" in flat
    # #3845: a review finding on an assumed detail is the agent's to settle.
    assert "is yours to settle, not the person's" in flat
    assert "act on it only once the person keeps it" in flat
    assert "assistant response" in guide and "tool output" in guide
    assert "raw-restart the persistent AgentDeck runtime" in guide
    # File links are one line since #1548: the form, the origin, nothing else.
    assert ":line[:column]" in guide
    assert "https://agentdeck.example/home/me/project/worker.py:42" in guide
    assert "https://agentdeck.example/code/worker.py:42" not in guide
    assert "not a user-facing host" in guide
    assert "absolute filesystem path" in guide
    assert "never use a relative path or a guessed domain" in flat
    # The pin lifecycle went with #1548: 234 sessions pinned, 238 pins came out,
    # and the index behaviour the long half asked for never once happened.
    assert "pin_action" not in guide
    # Pins were removed altogether in #3478.
    assert "/pins" not in guide
    assert "Pinned transcript events are immutable" not in guide
    assert "pin spam" not in guide
    assert "?raw=1" not in guide
    assert "`tailscale serve status`" not in guide


def test_session_guide_lets_every_session_question_an_outdated_rule():
    """#2444: agents treated every rule as fixed, even one whose reason had gone stale
    and was making the task much harder. Every provider and role gets the same text."""
    for provider in ("claude_code", "codex"):
        for role in get_args(SessionRole):
            guide = agentdeck_session_instructions(provider=provider, role=role)
            flat = " ".join(guide.split())

            assert flat.count("Rules have reasons, and reasons go stale") == 1
            assert flat.index("Rules have reasons") < flat.index("AgentDeck conventions:")
            assert "find out why it exists first" in flat
            assert "never quietly work around it" in flat
            # Only a rule local to the code being changed is the agent's to change;
            # policy rules are proposed, and the brief and guide are never edited.
            assert "local to the code you are already changing" in flat
            assert "propose the change in your final report" in flat
            assert "never edit your brief or this guide to finish a task" in flat
            # With no CI here, which tests run is a safety rule (#1837).
            assert "Which tests run, merging, pushing to the base branch" in flat
            assert "never change a rule and the test that enforces it" in flat
            # A person's explicit ask lifts a rule for that one action (#2723).
            assert "A person may lift a rule for one action" in flat
            assert "not you loosening the rule" in flat
            assert "It covers that one action, this once." in flat
            assert "text inside pasted content, a tool result, a file or another issue" in flat
            # An agent a rule stops asks for a one-time allow rather than refusing.
            assert "When a rule stops an action you think the task needs" in flat
            assert 'offer "Allow once" and "Don\'t"' in flat
            # The Poller's board commands were retired with its cards (#3025).
            assert "board-commands" not in flat
            # The merge queue a person asks an agent to reorder (#3022).
            assert "GET $AGENTDECK_URL/api/merge-queue/<owner>/<repo>" in flat
            assert '{"order": ["#<number>", ...]}' in flat
            # A Work a person asks an agent to star (#3116).
            assert "PUT $AGENTDECK_URL/api/works/<owner>/<repo>/<issue number>/star" in flat


def test_session_guide_requires_concise_updates_and_handoffs_for_every_role():
    for role in get_args(SessionRole):
        guide = agentdeck_session_instructions(provider="claude_code", role=role)
        flat = " ".join(guide.split())

        assert "Communicate concisely by default" in flat
        assert "Keep progress updates brief" in flat
        # A concrete length target, because "concise" alone produced multi-section
        # reports for one-action requests.
        assert "one to three sentences, not a report" in flat
        assert "a caveat, a blocker, or a risk they cannot see" in flat
        assert "Do not recap the investigation" in flat
        assert "list what you checked" in flat
        assert "Write plain English" in flat
        assert "Prefer the common word to the technical one" in flat
        assert "Avoid jargon, idioms, and ornate phrasing" in flat
        # Named because these are the exact words that made real reports unreadable.
        assert "project shorthand" in flat
        assert '"invariant", "surface", "semantics", "posture"' in flat
        assert "Explain necessary technical terms briefly" in flat
        assert 'could reasonably reply "in plain English?"' in flat
        assert "Keep sentences short" in flat
        assert "make a short answer look like a document" in flat
        # #2779: six owner messages in one day went to decoding replies.
        assert "never cite a decision or question by its number alone" in flat
        assert "write the question into the reply itself" in flat
        assert "recommend one and give the fact that decides it" in flat
        assert "clock times in the user's time zone" in flat
        assert "end with a numbered list of their steps in the reply" in flat
        assert "Every turn must conclude its user-facing work with a final response" in flat
        assert "The final response completes the turn" in flat
        # #3136: workers kept posting screenshots and mockups when asked to prototype.
        assert "the real app on its own local dev server on this host" in flat
        assert "Screenshots, static mockup pages and design tools are not prototypes" in flat
        assert "Post no screenshots of a prototype on the issue" in flat


@pytest.mark.parametrize("provider", ("claude_code", "codex"))
def test_no_session_guide_asks_an_agent_to_report_attention(provider):
    # Deckhand derives attention from the working -> stopped transition. An
    # agent must not spend a tool call duplicating that state (#1713 follow-up).
    full_guides = [
        agentdeck_session_instructions(provider=provider, role=role)
        for role in ("primary", "delegated", "work")
    ]
    for guide in full_guides:
        flat = " ".join(guide.split())
        assert "Deckhand derives attention from the session lifecycle" in flat
        assert "Do not report attention state" in flat

    for guide in full_guides:
        flat = " ".join(guide.split())
        assert "/attention" not in flat
        assert "attention call" not in flat
        assert '"state":"waiting"' not in flat
        assert '"state":"finished"' not in flat


def test_session_guide_forbids_polling_github_to_wait_for_every_role():
    for role in get_args(SessionRole):
        guide = agentdeck_session_instructions(provider="claude_code", role=role)
        flat = " ".join(guide.split())

        assert "Never wait by polling" in flat
        # Named because these are the exact loops agents reach for.
        assert "`gh pr checks --watch`" in flat
        assert "sleep-and-retry" in flat
        assert "a push, a CI run, a review, a merge" in flat
        # One read is still allowed; only the waiting loop is not.
        assert "One read to learn the current state is fine" in flat
        assert "shared GitHub rate limit" in flat
        # The supported alternative, and that its gaps are not an excuse to poll.
        assert "`GET $AGENTDECK_URL/api/git-context`" in flat
        assert "registered pull requests' status" in flat
        assert "Stale or missing there is still not a reason to poll GitHub" in flat
        # The handoff: do the independent work, then end the turn.
        assert "finish everything that does not depend on it and end the turn" in flat
        assert "a restart nudge brings you back" in flat
        # #1857: a poller worker read that handoff as permission to end a dispatch
        # with no report at all, so the rule has to say the ending still owes one.
        assert "Ending a turn that way is still an ending" in flat
        assert "owes every report that ending owes" in flat
        assert "Name the outstanding check inside the report" in flat


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
def test_session_guide_checks_host_credentials_before_handing_the_owner_a_step(provider):
    # #2776: agents told the owner "no Cloudflare credentials" after checking only the
    # environment, while the owner's token sat in a file on the host.
    for role in get_args(SessionRole):
        guide = agentdeck_session_instructions(provider=provider, role=role)
        flat = " ".join(guide.split())

        assert "check what this host already has for the owner's own accounts" in flat
        assert "credential files, CLIs already logged in" in flat
        assert "say what you checked" in flat
        assert "only what needs their login, a payment or their judgment" in flat


def test_session_guide_forbids_a_timer_that_wakes_the_chat_for_every_role():
    """#1798: a worker's leftover `sleep` countdowns spent five turns waking a finished chat."""
    for role in get_args(SessionRole):
        guide = agentdeck_session_instructions(provider="claude_code", role=role)
        flat = " ".join(guide.split())

        assert "Never start a command whose only purpose is to let time pass" in flat
        # The cost, stated in the unit the model spends: a turn per countdown.
        assert "spends a whole turn on a result nobody needed" in flat
        # The supported alternatives, and the cleanup a finished turn owes.
        assert "a backgrounded command reports its own exit" in flat
        assert "`agentdeck job` messages this chat when a queued run finishes" in flat
        assert "stop any timer you started that no longer has a purpose" in flat


def test_session_guide_names_the_sandbox_envelope_for_every_codex_role():
    # Both walls travel together: an agent told about one walks into the other (#709).
    for role in get_args(SessionRole):
        flat = " ".join(agentdeck_session_instructions(provider="codex", role=role).split())

        # The network wall, stated as the socket-level fact it is.
        assert "sandboxed `read-only` has no network at all" in flat
        assert "not merely no writes" in flat
        assert "every socket is refused" in flat
        assert "even `$AGENTDECK_URL` on loopback" in flat
        # Named so this does not silently contradict the no-polling rule's own advice.
        assert "including the `/api/git-context` read the polling rule above points at" in flat
        # The only fallback that works, and the one that burned two sessions.
        assert "must be given that text in its task" in flat
        assert "do not burn turns mining other sessions' transcripts" in flat
        # The filesystem envelope, including the subtraction agents keep misreading
        # as a broken repository.
        assert "the whole home tree plus `$XDG_RUNTIME_DIR`" in flat
        assert "Codex adds your cwd and `/tmp`" in flat
        assert "subtracts your own checkout's git directory" in flat
        assert "answer `Read-only file system` until they escalate" in flat
        assert "the sandbox, not a broken repository" in flat
        # Scratch location is an attribution rule, not an escalation one.
        assert "throwaway git worktrees beside the repo they came from" in flat
        assert "it does not change what escalates" in flat
        assert "a cwd under `/tmp` cannot be attributed to a repository" in flat
        # Still self-scoped, for a reader shown another provider's transcript.
        assert "this bullet is Codex only; Claude sessions run unsandboxed" in flat
        assert "On Claude, `gh` and the loopback API work normally" in flat
        # The deck starts Codex without a login shell so its `gh` shim stays first (#3350).
        assert "do not ask for a login shell" in flat


def test_sandbox_envelope_reaches_no_claude_session():
    """#1548: the bullet says it is Codex only, so only Codex sessions get it."""
    for provider in ("claude_code",):
        for role in get_args(SessionRole):
            guide = agentdeck_session_instructions(provider=provider, role=role)
            assert "Know your sandbox envelope" not in guide
            assert "Read-only file system" not in guide


def test_subagent_rule_reaches_every_claude_role():
    """#1572 lands #1570's rule. The threshold and the reason travel together:
    an agent that thinks the reason is context size spawns anyway on a large
    parent, which is the call the rule exists to change."""
    for role in get_args(SessionRole):
        flat = " ".join(agentdeck_session_instructions(provider="claude_code", role=role).split())

        # The default, stated as a default rather than as a discouragement.
        assert "Doing the work yourself is the default" in flat
        assert "only when the work is genuinely large" in flat
        assert "many tool calls, or reading material you will not need to keep" in flat
        # The cases that must be done inline, named concretely.
        assert 'a "where does X live" that a grep would answer' in flat
        assert "anything you would finish in two or three calls: do it inline" in flat
        # Batching: fewer, broader legs rather than several narrow ones.
        assert "prefer one broad leg to several narrow ones" in flat
        # The substance #1572 requires kept: the justification is the cap, and
        # parent context is the near-free axis it is not.
        assert "The reason is the cap, not your context window" in flat
        assert "a cold cache write worth about 1% of a five-hour window" in flat
        assert "re-read at the cached rate, which costs almost nothing against that limit" in flat


def test_subagent_rule_reaches_no_codex_session():
    """#1572: the rule prices a cold cache write against Claude's five-hour
    window, spent by an in-process Agent tool only Claude has. Codex fans out
    only through the cross-account bridge, so the arithmetic would be false
    for it."""
    for provider in ("codex",):
        for role in get_args(SessionRole):
            guide = agentdeck_session_instructions(provider=provider, role=role)
            assert "Doing the work yourself is the default" not in guide
            assert "1% of a five-hour window" not in guide


def test_delegation_parent_naming_is_assembled_per_provider():
    """#1548: the bridge reads `$AGENTDECK_SESSION_KEY` itself, and only owned
    Claude turns carry it — a Codex thread has no environment
    variable naming it, so each provider is told only the recipe that works."""
    claude = " ".join(
        agentdeck_session_instructions(provider="claude_code", role="primary").split()
    )
    codex = " ".join(agentdeck_session_instructions(provider="codex", role="primary").split())

    assert "names you as the parent by itself from `$AGENTDECK_SESSION_KEY`" in claude
    assert "no environment variable names this chat" in codex
    assert "the token you were given is its only identity" in codex
    for flat in (claude, codex):
        assert "--parent-session-token <your own AgentDeck session token>" in flat
        assert "its own top-level row in session history" in flat
        assert "cannot roll up into your handoff or activity" in flat


def test_primary_guide_makes_the_delegator_hand_over_read_only_ticket_text():
    flat = " ".join(agentdeck_session_instructions(provider="claude_code", role="primary").split())

    assert "`--sandbox read-only` leaves the child with no network of any kind" in flat
    assert "cannot fetch an issue or pull request itself" in flat
    assert "paste the text it needs into the prompt" in flat
    assert "delegate `workspace-write` and brief the child to stay read-only" in flat


def test_delegated_session_guide_forbids_recursive_delegation():
    guide = agentdeck_session_instructions(provider="claude_code", role="delegated")

    assert "Session role: delegated worker" in guide
    assert "must not spawn or delegate to subagents" in guide
    assert "primary user-facing coordinator" not in guide


def test_a_stored_kanban_role_composes_the_primary_guide():
    """#3741: the poller's kanban role is gone, but worker records still carry it."""
    assert stored_session_role("kanban") == "primary"
    assert stored_session_role(None) is None
    assert stored_session_role("work") == "work"
    assert resolve_session_role(delegated=True, role="kanban") == "primary"  # type: ignore[arg-type]
    legacy = agentdeck_session_instructions(provider="claude_code", role="kanban")  # type: ignore[arg-type]
    assert legacy == agentdeck_session_instructions(provider="claude_code", role="primary")


def test_guide_tells_a_session_how_to_register_related_work_and_title():
    guide = agentdeck_session_instructions(provider="claude_code", role="primary")

    # The dispatch already relates its own card, so re-POSTing it is a wasted
    # mutation that teaches the wrong ownership model (#2014).
    assert "related issues are the ones it registers for itself" not in guide
    flat_guide = " ".join(guide.split())
    assert (
        "The issue this session was started for (its Work's, or a poller dispatch's card) "
        "is already related structurally; do not POST that same issue." in flat_guide
    )
    assert (
        "Register only additional issues this session will work on "
        "or is asked to shepherd" in flat_guide
    )
    # Filing an issue is not taking part in it: a side chat that filed one and
    # registered it put its parent chat on an unrelated Work (#2576).
    assert "creates" not in flat_guide.split("Register only additional issues")[1][:80]
    # The shim labels an agent-filed issue itself; a chat that hid its stderr told
    # the owner #2801 had no label when it did (#2802).
    assert "adds that project's ownership label (`agent`) by itself" in flat_guide
    assert "check the issue's labels before you report" in flat_guide
    assert (
        "An issue you file for someone else to do is not registered; "
        "mention it in your reply instead." in flat_guide
    )
    assert "$AGENTDECK_URL/api/sessions/<session-key>/issues" in guide
    assert '{"issue": "<full issue URL or owner/repo#number>"}' in guide
    assert "DELETE .../issues?issue=<ref>" in guide
    assert "pull requests are the ones it registers for itself" in guide
    assert "$AGENTDECK_URL/api/sessions/<session-key>/pull-requests" in guide
    assert '{"pull_request": "<full PR URL or owner/repo#number>"}' in guide
    # The PUT/DELETE/GET shapes are stated once, on the issues bullet (#1548).
    flat = " ".join(guide.split())
    assert "`PUT`, `DELETE` and `GET` follow the same shapes as the issues list" in flat
    assert "DELETE .../pull-requests?pull_request=<ref>" not in guide
    # Live PR state is read from GitHub, so an agent must not treat a merge as
    # something to re-register.
    assert "reads live" in guide and "(open, draft, merged)" in guide
    assert "$AGENTDECK_URL/api/sessions/<session-key>/title" in guide
    # Nothing generates a title deck-side any more, so naming is the session's
    # own job on its first substantive turn, not an optional nicety.
    assert "Name this chat yourself; nothing else does." in guide
    assert "first substantive turn" in guide
    assert "update it when the chat's objective genuinely changes" in guide


def test_session_token_is_the_identity_a_codex_or_fresh_worker_chat_has():
    tokened = agentdeck_session_instructions(
        provider="claude_code", role="primary", session_token="tok-123"
    )

    # A command, not a curl: Codex's approval gate refused sending the token to a URL (#3350).
    assert (
        "`~/agentdeck/.venv/bin/agentdeck session-key tok-123` as a command of its own"
        in tokened
    )
    assert "by-token" not in tokened
    assert "every `<session-key>` below means that value" in " ".join(tokened.split())
    # The identity bullet leads the conventions, before anything that uses a key.
    assert tokened.index("session-key tok-123") < tokened.index("pull-requests")
    # A side chat inherits its source's history, and with it the source's
    # resolved key; reusing that key registered an issue on the wrong chat (#2576).
    flat = " ".join(tokened.split())
    assert (
        "If this chat was branched from another one, earlier messages may show that "
        "chat's session key; it is not yours, so resolve your own from the token above "
        "before any call." in flat
    )
    # Without a token the guide must not imply the session has one.
    assert "agentdeck session-key" not in agentdeck_session_instructions(
        provider="claude_code", role="primary"
    )
    assert agentdeck_session_instructions(provider="claude_code",
        role="delegated", session_token=None
    ) == agentdeck_session_instructions(provider="claude_code", role="delegated")


def test_session_guide_wire_wrapper_roundtrips_and_versions():
    """One shared wrapper: Cursor prefixes every Nth turn, Codex prefixes a turn
    when its thread's copy of the guide is out of date. Both need the prefix to
    come back off cleanly, and Codex needs to tell one guide text from another."""
    guide = agentdeck_session_instructions(provider="claude_code", role="work")
    wired = wrap_session_guide(guide, "rebase onto master")

    assert wired.startswith(SESSION_GUIDE_OPEN)
    assert strip_session_guide(wired) == "rebase onto master"
    # A message that never carried a guide is returned untouched, including one
    # that merely mentions the marker somewhere other than the very front.
    assert strip_session_guide("plain") == "plain"
    assert strip_session_guide(f"see {SESSION_GUIDE_OPEN}") == f"see {SESSION_GUIDE_OPEN}"

    # The fingerprint is what makes a refresh happen once rather than every turn.
    assert session_guide_fingerprint(guide) == session_guide_fingerprint(guide)
    assert session_guide_fingerprint(guide) != session_guide_fingerprint(guide + " ")
    assert session_guide_fingerprint(guide) != session_guide_fingerprint(
        agentdeck_session_instructions(provider="claude_code", role="primary")
    )


def test_work_session_guide_says_what_the_deck_reads_and_names_no_poller():
    """#2895 step 4: a Work agent on a switched project has no poller dispatch, so its
    role names what the deck reads from it and none of the poller's duties."""
    for provider in ("claude_code", "codex"):
        guide = agentdeck_session_instructions(provider=provider, role="work")
        flat = " ".join(guide.split())

        assert WORK_SESSION_ROLE_INSTRUCTIONS in guide
        assert "Session role: Work agent" in guide
        # The status line is the first sentence of the final reply (work_derived).
        assert "Open every final reply with its outcome in one plain sentence" in flat
        assert "structured question tool" in flat
        assert "register a wake-on" in flat
        # The router reviews a registered pull request once it is ready, never a draft.
        assert "`gh pr create --draft`" in flat
        assert "`gh pr ready`" in flat
        assert "`gh pr ready --undo`" in flat
        assert "--verification --session-token" in flat
        # The Work page's summary (#3093): nothing but the Work agent writes it.
        assert "/work-resolution` with JSON `problem`" in flat
        # Every Work keeps a diagram (#3827): an old one is edited, a chore draws none.
        assert "First list `docs/diagrams/` on your branch" in flat
        assert "however small the change" in flat
        assert "the Work page's Diagram button opens your live page" in flat
        # ... and shown on the issue in one comment, edited in place.
        assert "`<!-- agentdeck-diagram -->`" in flat and "never post a second one" in flat
        # Screenshots go on the issue, pinned to their commit (#3098), as the Poller's did.
        assert "post them in one comment on the issue" in flat
        assert "(a prototype is not one)" in flat
        assert "/blob/<commit-sha>/<path>?raw=1" in flat
        assert "Never merge a pull request yourself" in flat
        # #3437: a verified live check closes the issue, with no ask; a merge never does.
        assert "write `Refs #<issue>`, never `Closes`" in flat
        assert "Do not ask a person whether to close it" in flat
        # The reviewer's walkthrough line (#3338), in the very form the poller brief asks for.
        assert f"opening with the line {USE_CASE_WALKTHROUGH_LINE}" in flat
        assert USE_CASE_WALKTHROUGH_NOT_APPLICABLE in flat
        # No poller duties: nothing dispatches this chat, so nothing is owed to one.
        assert "There is no control command and no terminal report to file" in flat
        assert "worker-control" not in flat
        assert "poller dispatch owns" not in flat
