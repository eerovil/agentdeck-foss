from __future__ import annotations

import pytest

from agentdeck.gh_shim.classify import classify, is_terminal, ownership_label_to_add
from agentdeck.poller_ops import LabelTarget

ENV = {"GH_TOKEN": "t"}


def read(argv: list[str], **kwargs):
    return classify(argv, remote="git@github.com:o/r.git", environment=ENV, **kwargs)


def test_json_field_order_does_not_split_the_cache_key():
    first = read(["issue", "view", "814", "--json", "title,body"])
    second = read(["issue", "view", "814", "--json", "body,title"])

    assert first.kind == "read"
    assert first.key == second.key


def test_equals_and_space_spellings_agree():
    assert read(["pr", "list", "--state=open"]).key == read(
        ["pr", "list", "--state", "open"]
    ).key


def test_different_arguments_are_different_questions():
    assert read(["issue", "view", "814"]).key != read(["issue", "view", "815"]).key
    assert read(["pr", "list"]).key != read(["pr", "list", "--state", "closed"]).key


def test_the_same_read_in_another_repository_is_another_key():
    here = classify(["issue", "view", "1"], remote="git@github.com:o/a.git", environment=ENV)
    there = classify(["issue", "view", "1"], remote="git@github.com:o/b.git", environment=ENV)

    assert here.key != there.key


def test_an_explicit_repo_flag_overrides_where_the_worker_stands():
    here = classify(
        ["issue", "view", "1", "-R", "o/x"], remote="git@github.com:o/a.git", environment=ENV
    )
    there = classify(
        ["issue", "view", "1", "-R", "o/x"], remote="git@github.com:o/b.git", environment=ENV
    )

    assert here.key == there.key




def test_writes_pass_through_and_name_what_they_invalidate():
    for argv in (
        ["pr", "create", "--title", "x"],
        ["issue", "comment", "814", "--body", "x"],
        ["pr", "merge", "12"],
        ["issue", "edit", "814", "--add-label", "agent"],
    ):
        decision = read(argv)
        assert decision.kind == "write", argv
        assert decision.invalidates.endswith(f"{argv[0]}:")


def test_gh_api_is_a_read_only_when_it_cannot_mutate():
    assert read(["api", "repos/o/r/issues/1"]).kind == "read"
    assert read(["api", "repos/o/r/issues", "-X", "POST"]).kind == "write"
    assert read(["api", "repos/o/r/issues", "-f", "title=x"]).kind == "write"


def test_graphql_is_judged_by_its_query_not_by_its_method():
    """GraphQL always travels by POST, so the method test alone would wave the
    poller's own expensive path through the floor entirely ungated."""
    assert read(["api", "graphql", "-f", "query=query{viewer{login}}"]).kind == "read"
    assert read(["api", "graphql", "-f", "query=mutation{addComment{id}}"]).kind == "write"
    assert read(["api", "graphql", "-f", "query=@from-a-file.graphql"]).kind == "write"
    assert read(["api", "graphql", "--input", "-"]).kind == "write"


def test_watch_flags_are_refused_rather_than_cached():
    refused = read(["pr", "checks", "--watch"])

    assert refused.kind == "refuse"
    assert "poll" in refused.reason


def test_an_unknown_subcommand_passes_through_instead_of_being_guessed():
    assert read(["some-extension", "do-a-thing"]).kind == "write"


def test_terminality_is_only_claimed_when_gh_said_so():
    assert is_terminal("issue", "view", {"state": "CLOSED"}) is True
    assert is_terminal("pr", "view", {"state": "MERGED"}) is True
    assert is_terminal("pr", "view", {"state": "OPEN"}) is False
    assert is_terminal("pr", "list", {"state": "CLOSED"}) is False
    assert is_terminal("issue", "view", None) is False


TARGETS = (
    LabelTarget("eerovil/agentdeck", ("agent", "claude")),
    LabelTarget("acmecorp/bazaar", ("claude",)),
)


@pytest.mark.parametrize(
    "repository",
    [
        "eerovil/agentdeck",
        "EeroVil/AgentDeck",
        "https://github.com/eerovil/agentdeck",
        "https://github.com/eerovil/agentdeck/",
        "https://github.com/eerovil/agentdeck.git",
        "git@github.com:eerovil/agentdeck.git",
        "ssh://git@github.com/eerovil/agentdeck.git",
    ],
)
def test_every_spelling_of_a_managed_repository_gains_its_label(repository):
    """A bare `gh issue create` in a checkout names its repository by the remote URL."""
    assert ownership_label_to_add(["issue", "create", "-t", "x"], repository, TARGETS) == "agent"


@pytest.mark.parametrize(
    "labels",
    [
        ["--label", "claude"],
        ["--label=claude"],
        ["-lclaude"],
        ["-l", "Agent"],
        ["--label", "bug, Agent"],
        ["--label", "bug", "--label", "agent"],
    ],
)
def test_a_call_that_already_hands_the_issue_over_is_left_alone(labels):
    argv = ["issue", "create", "-t", "x", *labels]
    assert ownership_label_to_add(argv, "eerovil/agentdeck", TARGETS) is None


def test_other_labels_still_gain_the_ownership_label():
    for labels in (
        ["--label", "spec-only"], ["--label", ""], ["-l", "bug"], ["--label", "wayfinder:task"]
    ):
        argv = ["issue", "create", *labels]
        assert ownership_label_to_add(argv, "eerovil/agentdeck", TARGETS) == "agent"


def test_each_project_gains_its_own_canonical_label():
    assert ownership_label_to_add(["issue", "create"], "acmecorp/bazaar", TARGETS) == "claude"


@pytest.mark.parametrize(
    "repository", ["eerovil/agent-evidence", "someone/agentdeck", "", "not a repo"]
)
def test_a_repository_no_poller_manages_is_left_alone(repository):
    assert ownership_label_to_add(["issue", "create"], repository, TARGETS) is None


def test_a_repository_two_manifests_declare_honours_either_ones_aliases():
    targets = (
        LabelTarget("o/r", ("agent",)),
        LabelTarget("o/r", ("claude",)),
    )

    assert ownership_label_to_add(["issue", "create", "-l", "claude"], "o/r", targets) is None
    assert ownership_label_to_add(["issue", "create"], "o/r", targets) == "agent"
