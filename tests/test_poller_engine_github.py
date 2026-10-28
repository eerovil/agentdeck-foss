"""The GitHub adapter (#180), parsed against payloads recorded from real `gh` output.

Every fixture in tests/fixtures/github/ was captured from live `gh` against this repository, so
these tests check the shapes GitHub actually returns rather than shapes I imagined. The parts
that cannot be recorded (a Project board this login cannot read) follow the reference engine's
production query and are exercised through its documented response shape.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentdeck.poller_engine import github as github_module
from agentdeck.poller_engine import rate_limits
from agentdeck.poller_engine.config import Reachability
from agentdeck.poller_engine.github import (
    BOT_MARKER,
    CHATGPT_REVIEW_MARKER,
    TERMINAL_MARKER,
    Gh,
    GitHubAdapter,
)
from agentdeck.poller_engine.ports import (
    CommentBody,
    Effect,
    EffectKind,
    WorktreeCleanup,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github"


def recorded(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeGh(Gh):
    """A `gh` that answers from a script instead of the network, recording what was asked."""

    def __init__(self, answers=None, default=(0, "null", "")):
        super().__init__()
        self.answers = list(answers or [])
        self.default = default
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args):
        self.calls.append(args)
        for match, answer in self.answers:
            if all(part in " ".join(args) for part in match):
                return answer
        return self.default

    def asked(self, *parts: str) -> bool:
        return any(all(part in " ".join(call) for part in parts) for call in self.calls)

    def current_rate_limits(self, *, max_age: float | None = None):
        self.rate_limit_max_age = max_age
        value = self.json("api", "rate_limit")
        resources = value.get("resources") if isinstance(value, dict) else None
        readings = {}
        for name in ("core", "graphql"):
            resource = resources.get(name) if isinstance(resources, dict) else None
            remaining = resource.get("remaining") if isinstance(resource, dict) else None
            if isinstance(remaining, int) and not isinstance(remaining, bool):
                readings[name] = rate_limits.Reading(
                    resource=name,
                    remaining=remaining,
                    source=rate_limits.ESTIMATE_SOURCE,
                )
        return readings


def adapter(api=None, host=None, **kwargs) -> GitHubAdapter:
    defaults = {
        "org": "eerovil",
        "issue_repos": ("agentdeck",),
        "project_number": 2,
    }
    defaults.update(kwargs)
    return GitHubAdapter(api=api or FakeGh(), host=host or FakeGh(), **defaults)


def ok(payload) -> tuple[int, str, str]:
    return (0, json.dumps(payload), "")


def test_gh_runner_records_real_headers_without_forwarding_api_trace(
    tmp_path, monkeypatch
):
    cache = tmp_path / "github.sqlite3"
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(cache))
    trace = """ordinary warning
* Request at now
> Authorization: token secret
< HTTP/2.0 200 OK
< X-RateLimit-Limit: 5000
< X-RateLimit-Remaining: 41
< X-RateLimit-Resource: core
* Request took 1ms
"""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, '{"id":20142828}', trace
        ),
    )
    gh = Gh(token="one-token")

    code, _out, err = gh.run("api", "user")

    assert code == 0
    assert err == "ordinary warning\n"
    credential = rate_limits.credential_identity(gh._environment())
    reading = rate_limits.RateLimitStore(cache).read(credential)["core"]
    assert reading.remaining == 41
    assert reading.source == rate_limits.HEADER_SOURCE


def test_gh_runner_remembers_what_github_answered_a_failed_call(tmp_path, monkeypatch):
    """The status behind "unexpected end of JSON input", and whether it was a hiccup (#2714)."""
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))
    answers = iter(
        [
            (1, "* Request at now\n> POST /graphql HTTP/1.1\n< HTTP/2.0 502 Bad Gateway\n"
                "< X-Accepted-Github-Permissions: issues=write\n* Request took 1ms\n"
                "unexpected end of JSON input\n"),
            (0, ""),
        ]
    )

    def completed(argv, **_kwargs):
        code, trace = next(answers)
        return subprocess.CompletedProcess(argv, code, "{}", trace)

    monkeypatch.setattr(subprocess, "run", completed)
    gh = Gh(token="one-token", authority="app:o")

    code, _out, err = gh.run("issue", "comment", "5", "-R", "o/r", "--body", "x")

    assert code == 1
    assert "lacks a permission" not in err
    assert "POST /graphql -> HTTP 502, empty body" in err
    assert gh.last_failure == "github answered: POST /graphql -> HTTP 502, empty body"
    assert gh.last_failure_transient is True
    assert gh.run("api", "user")[0] == 0
    assert (gh.last_failure, gh.last_failure_transient) == ("", False)


def test_real_quota_path_shares_users_and_keeps_headers_authoritative(
    tmp_path, monkeypatch
):
    cache = tmp_path / "github.sqlite3"
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(cache))
    calls = []

    def completed(argv, **_kwargs):
        calls.append(argv[1:])
        remaining = 41 if argv[1:] == ["api", "user"] else 40
        trace = (
            "* Request at now\n"
            "< HTTP/2.0 200 OK\n"
            "< X-RateLimit-Limit: 5000\n"
            f"< X-RateLimit-Remaining: {remaining}\n"
            "< X-RateLimit-Resource: core\n"
            "* Request took 1ms\n"
        )
        stdout = (
            '{"id":20142828}'
            if argv[1:] == ["api", "user"]
            else '{"resources":{"core":{"remaining":4999},'
            '"graphql":{"remaining":700}}}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout, trace)

    monkeypatch.setattr(subprocess, "run", completed)

    first = Gh(token="token-a").current_rate_limits()
    second = Gh(token="token-b").current_rate_limits()

    assert first["core"].remaining == 40
    assert first["core"].source == rate_limits.HEADER_SOURCE
    assert first["graphql"].source == rate_limits.ESTIMATE_SOURCE
    assert second["graphql"].remaining == 700
    assert calls.count(["api", "rate_limit"]) == 1

    # An App installation token cannot read `GET /user`; its probe is its own repository
    # list, which answers with the same core headers (#3711).
    calls.clear()
    app = Gh(token="token-c", authority="app:o").current_rate_limits()
    assert ["api", "user"] not in calls
    assert ["api", "installation/repositories?per_page=1"] in calls
    assert app["core"].remaining == 40


# --- authorization input ---

def test_a_real_permission_payload_yields_its_level():
    api = FakeGh([(("collaborators",), ok(recorded("permission")))])
    assert adapter(api=api).permission("agentdeck", "eerovil") == "admin"


def test_an_unreadable_permission_is_unknown_not_a_denial():
    api = FakeGh(default=(1, "", "HTTP 403"))
    # The engine turns None into "uncertain" and retries; an adapter must never answer "no" here.
    assert adapter(api=api).permission("agentdeck", "eerovil") is None


def test_an_anonymous_login_is_not_looked_up():
    api = FakeGh()
    assert adapter(api=api).permission("agentdeck", "") is None
    assert api.calls == []


# --- intake ---


# --- the live view of a card ---


def test_one_rate_limit_probe_guards_both_live_query_budgets():
    api = FakeGh(
        [
            (
                ("rate_limit",),
                ok(
                    {
                        "resources": {
                            "core": {"remaining": 499},
                            "graphql": {"remaining": 5000},
                        }
                    }
                ),
            )
        ]
    )
    guarded = adapter(api=api, core_reserve=500, graphql_reserve=200)

    assert guarded.live_queries_halt() == (
        "core-budget: untrusted /rate_limit cold-start estimate"
    )
    assert guarded.live_queries_halt() == (
        "core-budget: untrusted /rate_limit cold-start estimate"
    )
    assert [call for call in api.calls if "rate_limit" in call] == [("api", "rate_limit")]


def test_healthy_rate_limit_buckets_permit_live_queries():
    api = FakeGh(
        [
            (
                ("rate_limit",),
                ok(
                    {
                        "resources": {
                            "core": {"remaining": 5000},
                            "graphql": {"remaining": 5000},
                        }
                    }
                ),
            )
        ]
    )

    assert adapter(api=api).live_queries_halt() is None


def test_core_budget_at_the_configured_floor_permits_live_queries():
    api = FakeGh(
        [
            (
                ("rate_limit",),
                ok(
                    {
                        "resources": {
                            "core": {"remaining": 500},
                            "graphql": {"remaining": 5000},
                        }
                    }
                ),
            )
        ]
    )

    assert adapter(api=api, core_reserve=500).live_queries_halt() is None


def test_low_graphql_budget_has_its_own_halt_reason():
    api = FakeGh(
        [
            (
                ("rate_limit",),
                ok(
                    {
                        "resources": {
                            "core": {"remaining": 5000},
                            "graphql": {"remaining": 200},
                        }
                    }
                ),
            )
        ]
    )

    assert adapter(api=api).live_queries_halt() == (
        "graphql-budget: untrusted /rate_limit cold-start estimate"
    )


def test_failed_rate_limit_probe_never_blocks_the_live_wake():
    api = FakeGh(default=(1, "", "temporary GitHub failure"))

    assert adapter(api=api).live_queries_halt() is None


# --- dependencies: what is still holding a card (#640) ---


def dependency(number, state="OPEN", repo="agentdeck", owner="eerovil"):
    return {"number": number, "state": state,
            "repository": {"name": repo, "owner": {"login": owner}}}


# --- the whole declared list, closed entries included (#1588) ---
#
# `blockers` alone cannot tell a card whose dependency has landed from a card nobody ever
# declared a dependency for: both report an empty tuple. GitHub's answer holds the difference
# already, so keeping it costs no query and is what lets a blocked card resume.


# --- ownership provenance ---


# --- linked pull requests ---


# One `gh pr diff` of a two-line change, and the same change after the branch was rebased onto
# a master that had edited the same file: lines were added above the hunk, the context either
# side of it was reworded, and the blob OIDs moved. Not one line the pull request contributes
# is different (#2122).
_DIFF_BEFORE_REBASE = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,7 +10,8 @@ def handler(request):
     ctx = build(request)
     if not ctx:
         return None
-    return render(ctx)
+    log(ctx)
+    return render(ctx, strict=True)


 def build(request):
"""

_DIFF_AFTER_REBASE = """diff --git a/src/app.py b/src/app.py
index 3333333..4444444 100644
--- a/src/app.py
+++ b/src/app.py
@@ -31,7 +31,8 @@ def handler(request):
     ctx = build(request, trace=True)
     if not ctx:
         return None
-    return render(ctx)
+    log(ctx)
+    return render(ctx, strict=True)


 def build(request, trace=False):
"""


def _digest(diff: str) -> str | None:
    return github_module.review_diff_digest(diff)


def test_a_review_diff_is_hashed_without_storing_its_contents():
    diff = "diff --git a/a.py b/a.py\n-old\n+new\n"
    digest = _digest(diff)
    assert digest and diff not in digest
    assert digest == _digest(diff)


def test_a_rebase_that_changes_no_contributed_line_keeps_the_review_digest():
    # The raw bytes moved, which is exactly what used to cost the card a second review.
    assert _DIFF_BEFORE_REBASE != _DIFF_AFTER_REBASE
    assert _digest(_DIFF_BEFORE_REBASE) == _digest(_DIFF_AFTER_REBASE)


def test_a_push_that_changes_a_contributed_line_moves_the_review_digest():
    changed = _DIFF_AFTER_REBASE.replace("render(ctx, strict=True)", "render(ctx, strict=False)")
    assert _digest(changed) != _digest(_DIFF_BEFORE_REBASE)


def test_a_rebase_that_moves_a_deletion_to_another_file_moves_the_review_digest():
    # The paths are in the digest, so the same contributed lines under a different file are
    # not the same contribution.
    moved = _DIFF_AFTER_REBASE.replace("src/app.py", "src/other.py")
    assert _digest(moved) != _digest(_DIFF_BEFORE_REBASE)


def test_an_added_line_is_not_split_on_anything_but_a_newline():
    # `str.splitlines` breaks on a form feed too, and the tail — " bar" — then begins with a
    # space and reads as a context line, so contributed text would drop out of the digest.
    plain = "diff --git a/a.py b/a.py\n+foo\n"
    with_feed = "diff --git a/a.py b/a.py\n+foo\x0c bar\n"
    assert _digest(plain) != _digest(with_feed)


_BINARY = (
    "diff --git a/logo.png b/logo.png\n"
    "index 1111111..2222222 100644\n"
    "Binary files a/logo.png and b/logo.png differ\n"
)


def test_two_different_binary_replacements_are_not_the_same_contribution():
    # Git says nothing about binary content except the blob OIDs, so dropping `index` wholesale
    # would make every replacement of the same image normalize identically.
    other = _BINARY.replace("..2222222", "..3333333")
    assert _digest(_BINARY) != _digest(other)


def test_a_rebased_binary_keeps_its_digest_when_only_the_pre_image_moved():
    # The base replaced the file too; this pull request's own post-image is unchanged.
    rebased = _BINARY.replace("index 1111111..", "index 9999999..")
    assert _digest(rebased) == _digest(_BINARY)


def test_a_binary_section_without_a_readable_blob_is_unknown_not_equal():
    # Unknown defers the release; a content-independent digest would have shipped it.
    no_index = (
        "diff --git a/logo.png b/logo.png\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    assert _digest(no_index) is None
    assert _digest(_BINARY.replace("1111111..2222222", "1111111..zzz")) is None


def test_a_rename_keeps_its_digest_when_only_the_similarity_percentage_moved():
    # `similarity index` is computed from the pre- and post-images, so a base edit to a renamed
    # file moves the number while the rename and the contributed lines stay exactly put.
    before = (
        "diff --git a/old.py b/new.py\nsimilarity index 88%\nrename from old.py\n"
        "rename to new.py\nindex 1111..2222 100644\n--- a/old.py\n+++ b/new.py\n"
        "@@ -3,4 +3,5 @@ def f():\n ctx\n-a\n+b\n ctx\n"
    )
    after = (
        "diff --git a/old.py b/new.py\nsimilarity index 61%\nrename from old.py\n"
        "rename to new.py\nindex 9999..2222 100644\n--- a/old.py\n+++ b/new.py\n"
        "@@ -40,4 +41,5 @@ def f():\n other\n-a\n+b\n other\n"
    )
    assert _digest(before) == _digest(after)


def test_a_rename_to_a_different_path_is_a_different_contribution():
    before = (
        "diff --git a/old.py b/new.py\nsimilarity index 88%\nrename from old.py\n"
        "rename to new.py\nindex 1111..2222 100644\n"
    )
    assert _digest(before) != _digest(before.replace("new.py", "other.py"))


def test_a_binary_patch_body_is_not_hashed_once_the_post_image_is_known():
    # The encoded body is a delta against the pre-image, so a rebase moves it even though the
    # post-image — the identity this code deliberately chose for binary — is unchanged.
    literal = (
        "diff --git a/a.bin b/a.bin\nindex 1111..2222 100644\nGIT binary patch\n"
        "literal 12\nzcmZQzU|?WmWMXDv0Ba*z\n\n"
    )
    delta = (
        "diff --git a/a.bin b/a.bin\nindex 8888..2222 100644\nGIT binary patch\n"
        "delta 7\nocmZQzXXXXXyyyy0Bb9z\ndelta 3\nxABC\n"
    )
    assert _digest(literal) == _digest(delta)
    assert _digest(literal) != _digest(delta.replace("..2222", "..3333"))


def test_the_file_after_a_binary_patch_is_still_read():
    """The skip ends at the next `diff --git`, not at the end of the diff."""
    both = (
        "diff --git a/a.bin b/a.bin\nindex 1111..2222 100644\nGIT binary patch\n"
        "literal 12\nzcmZQzU|?WmWMXDv0Ba*z\n\n"
        "diff --git a/b.py b/b.py\nindex 4444..5555 100644\n@@ -1 +1 @@\n-x\n+y\n"
    )
    changed = both.replace("+y", "+z")
    assert _digest(both) != _digest(changed)


def test_a_binary_file_never_borrows_the_blob_of_the_file_before_it():
    # Two sections in one diff, the second binary with no `index` of its own. Carrying the
    # first file's OID forward would digest the image as that file's content.
    two = (
        "diff --git a/a.py b/a.py\nindex 1111..2222 100644\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/b.png b/b.png\nBinary files a/b.png and b/b.png differ\n"
    )
    assert _digest(two) is None


def test_a_deleted_binary_is_a_statement_about_content_not_a_missing_one():
    deleted = (
        "diff --git a/logo.png b/logo.png\ndeleted file mode 100644\n"
        "index 2222222..0000000\nBinary files a/logo.png and /dev/null differ\n"
    )
    assert _digest(deleted) is not None
    assert _digest(deleted) != _digest(_BINARY)


def test_two_hunks_are_not_the_same_contribution_as_one():
    one = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,3 +1,3 @@\n ctx\n-foo\n+bar\n ctx\n"
    )
    two = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n ctx\n-foo\n ctx\n@@ -40,2 +40,2 @@\n ctx\n+bar\n ctx\n"
    )
    assert _digest(one) != _digest(two)


# --- pull requests the newest-100 listing can no longer reach (#1467) ---


# --- pull requests whose author held them back, and targets a person named (#1445) ---


# --- pull-request state ---


# --- arming auto-merge ---


# --- effects ---


def test_a_comment_carries_the_bot_marker_so_it_can_never_self_trigger():
    api = FakeGh()
    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Queued."))
    body = next(call for call in api.calls if "comment" in call)
    assert "<!-- claude-bot -->" in " ".join(body)


def _posted_body(api) -> str:
    call = next(call for call in api.calls if "comment" in call)
    return call[call.index("--body") + 1]


def test_every_comment_carries_the_projects_control_guide():
    """#373: the guide is appended once, here, so 'every bot comment' can actually be true."""
    guide = "---\n🤖 **Ohjaa Claudea**\n- **Pysäytä työ:** `/claude stop`"
    api = FakeGh()
    adapter(api=api, comment_guide=guide).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, "Work is waiting.")
    )
    body = _posted_body(api)
    assert body.startswith("Work is waiting.")
    assert guide in body
    navigation = "[Issue #5](https://github.com/eerovil/agentdeck/issues/5)"
    assert navigation in body
    assert body.index(guide) < body.index(navigation)
    # The marker stays last, so endswith remains the cheap "we wrote this" test.
    assert body.endswith(BOT_MARKER)
    # The engine holds no prose of its own: nothing it did not render appears.
    assert "/shot" not in body and "/review" not in body


def test_a_project_with_no_guide_posts_exactly_what_it_did_before():
    """Parity-ledger name is stable; #957 adds only the shared navigation row."""
    api = FakeGh()
    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Queued."))
    issue = "[Issue #5](https://github.com/eerovil/agentdeck/issues/5)"
    assert _posted_body(api) == f"Queued.\n\n{issue}\n\n{BOT_MARKER}"


def test_comment_navigation_adds_known_session_and_pull_request_before_the_issue():
    api = FakeGh()
    comment = CommentBody(
        "Ready.",
        session_url="https://deck.example/sessions/codex:test:sid",
        pull_request_url="https://github.com/example/agentdeck/pull/959",
    )
    adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, comment)
    )

    navigation = (
        "[AgentDeck](https://deck.example/sessions/codex:test:sid) · "
        "[PR #959](https://github.com/example/agentdeck/pull/959) · "
        "[Issue #5](https://github.com/eerovil/agentdeck/issues/5)"
    )
    assert navigation in _posted_body(api)


def test_comment_navigation_resolves_an_engine_discovered_pull_request_ref():
    api = FakeGh()
    comment = CommentBody(
        "Ready.",
        pull_request_url="https://github.com/eerovil/agentdeck/pull/12",
        pull_request_ref="agentdeck#959",
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, comment))

    assert "[PR #959](https://github.com/eerovil/agentdeck/pull/959)" in _posted_body(api)
    assert "/pull/12" not in _posted_body(api)


def test_malformed_comment_links_are_omitted_without_losing_the_issue():
    api = FakeGh()
    comment = CommentBody(
        "Ready.",
        session_url="https://deck.example/sessions/private\\",
        pull_request_url="https://example.com/example/agentdeck/pull/959",
    )
    adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, comment)
    )
    body = _posted_body(api)
    assert "AgentDeck" not in body and "PR #959" not in body
    assert "[Issue #5](https://github.com/eerovil/agentdeck/issues/5)" in body


# --- activation discovery, against a payload recorded from a real board ---

def test_a_real_boards_payload_yields_its_ids_and_columns():
    """The shape here was recorded from a live board (ids redacted), not invented."""
    api = FakeGh([(("projectV2",), ok(recorded("board_discovery")))])
    board = adapter(api=api).board("example", 2)
    assert board is not None
    assert board.project_id == "PVT_kwDOEXAMPLE"
    assert board.status_field_id == "PVTSSF_lADOEXAMPLE"
    # Every role the engine moves a card through is present, and so is a column it does not know
    # about — which it must simply ignore rather than object to.
    for role in ("Todo", "In progress", "In review", "Done", "Needs More Info", "Blocked"):
        assert role in board.status_options, role
    assert "Bot-issues" in board.status_options


def test_a_user_owned_board_is_found_under_user_when_organization_is_empty():
    payload = recorded("board_discovery")
    user_shaped = {"data": {"user": payload["data"]["organization"]}}
    api = FakeGh(
        [
            (("organization(login:$org)",), ok({"data": {"organization": None}})),
            (("user(login:$org)",), ok(user_shaped)),
        ]
    )
    # A personal board answers under `user`, so discovery has to ask both ways.
    assert adapter(api=api).board("eerovil", 1) is not None


def test_a_board_nobody_can_read_is_unknown_rather_than_empty():
    api = FakeGh(default=(1, "", "INSUFFICIENT_SCOPES"))
    assert adapter(api=api).board("example", 2) is None


def test_repo_reachability_distinguishes_absent_from_unverifiable():
    absent = FakeGh(default=(1, "", "gh: Not Found (HTTP 404)"))
    assert adapter(api=absent).repo_check("example", "nope").reach is Reachability.ABSENT
    blocked = FakeGh(default=(1, "", "HTTP 502 bad gateway"))
    # Unverifiable must not read as absent: activation fails closed instead of dropping a repo.
    assert adapter(api=blocked).repo_check("example", "store").reach is Reachability.UNAVAILABLE
    fine = FakeGh(default=(0, '"store"', ""))
    assert adapter(api=fine).repo_check("example", "store").reach is Reachability.REACHABLE


def test_a_rate_limited_repo_read_is_not_reported_as_a_credential_failure():
    """#707: both arrive as HTTP 403, and only one of them is anyone's job to fix."""
    limited = FakeGh(
        default=(1, "", "gh: API rate limit exceeded for user ID 42. (HTTP 403)")
    )
    throttled = adapter(api=limited).repo_check("example", "store")
    assert throttled.reach is Reachability.UNAVAILABLE
    assert "API rate limit exceeded" in throttled.detail

    refused = FakeGh(default=(1, "", "gh: Resource not accessible by integration (HTTP 403)"))
    denied = adapter(api=refused).repo_check("example", "store")
    assert denied.reach is Reachability.DENIED
    assert "Resource not accessible by integration" in denied.detail


def test_a_repo_read_that_never_reached_github_reads_as_unavailable():
    for blob in (
        "gh api timed out",
        'Get "https://api.github.com/repos/example/store": dial tcp: lookup api.github.com: '
        "no such host",
        "error connecting to api.github.com",
    ):
        api = FakeGh(default=(1, "", blob))
        check = adapter(api=api).repo_check("example", "store")
        assert check.reach is Reachability.UNAVAILABLE, blob
        assert check.detail


def test_an_unrecognized_repo_failure_does_not_accuse_the_credentials():
    """The default has to be "unanswered": that is the half the reader cannot act on."""
    api = FakeGh(default=(1, "", "gh: something nobody has seen before"))
    check = adapter(api=api).repo_check("example", "store")
    assert check.reach is Reachability.UNAVAILABLE
    assert check.detail == "something nobody has seen before"


def test_the_adapter_still_carries_every_lookup_name_activation_discovers_with():
    """Activation's Discovery is duck-typed, so a renamed lookup breaks at nobody's compile."""
    from agentdeck.poller_engine.config import Discovery

    assert isinstance(adapter(), Discovery)


def test_a_repo_failure_detail_is_one_short_line():
    api = FakeGh(default=(1, "", "gh: " + "x" * 400 + "\nsecond line\n"))
    detail = adapter(api=api).repo_check("example", "store").detail
    assert len(detail) == 120
    assert "\n" not in detail


# --- the port contract, and the budget question it asks (#183) ---

def test_a_blockers_state_is_read_for_an_issue_and_a_pull_request_alike():
    """#1230: the thing a worker is blocked on is usually a PR, but never has to be, so this
    goes through the issues endpoint — which serves both — rather than `pr view`."""
    api = FakeGh(
        [
            (("repos/eerovil/agentdeck/issues/122",),
             ok({"state": "closed", "pull_request": {"merged_at": "2026-08-27T10:00:00Z"}})),
            (("repos/eerovil/agentdeck/issues/120",), ok({"state": "closed"})),
            (("repos/eerovil/agentdeck/issues/9",), ok({"state": "open"})),
            # A pull request somebody abandoned: closed, never merged, and out of the way.
            (("repos/other/thing/issues/4",),
             ok({"state": "closed", "pull_request": {"merged_at": None}})),
        ]
    )
    # The host login, like every other cross-repository card read (`pr_states`, the marker
    # listing): a blocker can live in a repository the project-scoped token cannot see.
    port = adapter(host=api)

    states = port.ref_states(
        ["agentdeck#122", "agentdeck#120", "agentdeck#9", "other/thing#4"]
    )

    assert states == {
        "agentdeck#122": "MERGED",
        "agentdeck#120": "CLOSED",
        "agentdeck#9": "OPEN",
        "other/thing#4": "CLOSED",
    }
    # A cross-organization ref already carries its owner; prefixing this project's would
    # invent a path that does not exist.
    assert api.asked("repos/other/thing/issues/4")


def test_a_blocker_that_could_not_be_read_is_absent_rather_than_called_open():
    """The engine fails closed on a missing entry; reporting `OPEN` would be a claim the
    adapter has no grounds for, and would read as a deliberate answer in the cycle log."""
    api = FakeGh([(("issues/122",), ok({"state": "open"}))], default=(1, "", "not found"))
    port = adapter(host=api)

    assert port.ref_states(["agentdeck#122", "agentdeck#404"]) == {"agentdeck#122": "OPEN"}


def test_the_adapter_implements_every_method_the_engine_calls():
    """`dispatch_ok` was in EnginePort and only ever on the test fakes, so the first real wake
    died on AttributeError with every unit test green. The protocol is the contract; check the
    adapter against it rather than against the set of calls some test happened to make."""
    from agentdeck.poller_engine.ports import EnginePort

    required = [
        name
        for name, value in vars(EnginePort).items()
        if callable(value) and not name.startswith("_")
    ]
    assert required, "EnginePort has no methods — the check would pass vacuously"
    missing = [name for name in required if not hasattr(GitHubAdapter, name)]
    assert missing == []


def _usage(
    tmp_path, label, *, provider="claude_code", five=0.0, seven=0.0, age_s=0.0
):
    from datetime import UTC, datetime, timedelta

    fetched = datetime.now(UTC) - timedelta(seconds=age_s)
    (tmp_path / f"usage-{label}.json").write_text(
        json.dumps(
            {
                "account": f"{provider}:{label}",
                "fetched_at": fetched.isoformat(),
                "five_hour_pct": five,
                "seven_day_pct": seven,
            }
        )
    )


# --- worktrees the host reported, and only those (#183) ---


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def test_worktree_cleanup_validates_every_path_before_force_removal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "--initial-branch=master", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "tracked").write_text("one")
    _git("add", "tracked", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    valid = repo / ".worktrees" / "issue-5"
    _git("worktree", "add", "-b", "agent/issue-5", str(valid), cwd=repo)
    unsafe = repo / "ordinary-directory"
    unsafe.mkdir()

    result = adapter().remove_worktrees([str(valid), str(unsafe)])

    assert result == WorktreeCleanup((str(unsafe),))
    assert not valid.exists()
    assert unsafe.is_dir(), "an unregistered path must never reach git worktree remove --force"


def _checkout_with_worktree(tmp_path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "--initial-branch=master", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "tracked").write_text("one")
    _git("add", "tracked", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    tree = repo / ".worktrees" / "issue-5"
    _git("worktree", "add", "-b", "agent/issue-5", str(tree), cwd=repo)
    return repo, tree


def test_a_worktree_still_holding_uncommitted_work_is_kept_not_force_removed(tmp_path):
    """#1718: `git worktree remove --force` deleted a live session's unfinished patch.

    Retained is deliberately not `failed`. Reporting it pending would make the card
    unforgettable and retry the same refusal on every wake (#1189); the release is allowed
    to finish, and the directory is handed to the operator sweep instead.
    """
    _repo, tree = _checkout_with_worktree(tmp_path)
    (tree / "tracked").write_text("edited but never committed")
    (tree / "scratch.py").write_text("progress")

    result = adapter().remove_worktrees([str(tree)])

    assert result == WorktreeCleanup((), ((str(tree), "2 uncommitted file(s)"),))
    assert result.landed, "a deliberate keep is not a failed effect"
    assert (tree / "scratch.py").exists(), "the worker's unfinished work is still there"


def test_a_clean_worktree_is_still_released_by_an_ordinary_removal(tmp_path):
    """The guard is about work, not about worktrees: nothing to lose, nothing to keep."""
    _repo, tree = _checkout_with_worktree(tmp_path)

    assert adapter().remove_worktrees([str(tree)]) == WorktreeCleanup()
    assert not tree.exists()


def test_work_written_after_the_precheck_is_still_not_deleted(tmp_path, monkeypatch):
    """The precheck cannot close the window; the removal itself has to refuse.

    A `git status` read and a `git worktree remove --force` are two processes, so a session
    writing between them loses exactly the work the read was meant to protect — the same
    class of loss #1718 is about. The stubbed precheck below *is* that race: it reports the
    clean checkout it saw, while the directory on disk is already dirty. Git's non-forced
    removal declines it as one operation, and that refusal is a keep, not a failure.
    """
    _repo, tree = _checkout_with_worktree(tmp_path)
    (tree / "tracked").write_text("written a moment after the check returned clean")
    (tree / "scratch.py").write_text("progress")
    monkeypatch.setattr(github_module, "_holds_work", _clean_then_real())

    result = adapter().remove_worktrees([str(tree)])

    assert result.failed == (), "a refusal that saved work is not a cleanup failure"
    assert [path for path, _ in result.retained] == [str(tree)]
    assert result.retained[0][1] == "2 uncommitted file(s)"
    assert (tree / "scratch.py").exists(), "the work written mid-removal is still there"
    assert tree.is_dir()


def _clean_then_real():
    """A precheck that reports clean once, then answers honestly — i.e. loses the race."""
    real = github_module._holds_work
    calls = {"n": 0}

    def probe(target, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""
        return real(target, **kwargs)

    return probe


def test_a_clean_worktree_git_refuses_for_its_submodules_is_kept_not_failed(tmp_path):
    """Git declines a worktree that ever had a submodule without --force; a retry never helps."""
    repo, tree = _checkout_with_worktree(tmp_path)
    (repo / ".git" / "worktrees" / "issue-5" / "modules").mkdir()

    result = adapter().remove_worktrees([str(tree)])

    assert result.failed == ()
    assert [path for path, _ in result.retained] == [str(tree)]
    assert "submodules" in result.retained[0][1]
    assert tree.is_dir()


def test_a_worktree_whose_state_cannot_be_read_is_kept_rather_than_deleted(tmp_path,
                                                                           monkeypatch):
    """An unknown is never permission — the same rule the reaper already follows."""
    _repo, tree = _checkout_with_worktree(tmp_path)
    monkeypatch.setattr(github_module, "_dirty_count", lambda target, **kwargs: None)

    result = adapter().remove_worktrees([str(tree)])

    assert result == WorktreeCleanup((), ((str(tree), "its state could not be read"),))
    assert tree.is_dir()


def test_an_already_removed_valid_worktree_path_is_idempotently_clean(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "--initial-branch=master", cwd=repo)
    absent = repo / ".worktrees" / "issue-5"

    assert adapter().remove_worktrees([str(absent)]) == WorktreeCleanup()


def test_a_nested_worktree_whose_whole_checkout_vanished_is_clean_not_pending(tmp_path):
    """#1189: the outer checkout goes first and takes the inner path's repository with it.

    Nothing is left to remove and no repository can be attributed, so reporting this as
    pending made the card unforgettable: every wake retried it, every wake's report carried
    an error, and no activation canary could ever come back clean.
    """
    outer = tmp_path / "trailhead" / ".worktrees" / "issue-207-issues"
    nested = outer / "web" / ".worktrees" / "issue-207"

    assert not nested.exists(), "the whole checkout is gone, not just the worktree"
    assert adapter().remove_worktrees([str(nested)]) == WorktreeCleanup()


def test_something_still_on_the_host_that_no_checkout_claims_stays_pending(tmp_path):
    """The other half of the same rule: absent is clean, present-but-unattributable is not.

    A dangling symlink counts as present. Something is there, nothing owns it, and writing
    it off as released would hide a real leftover behind the fix above.
    """
    orphan = tmp_path / "nowhere" / ".worktrees" / "issue-5"
    orphan.mkdir(parents=True)
    dangling = tmp_path / "elsewhere" / ".worktrees" / "issue-6"
    dangling.parent.mkdir(parents=True)
    dangling.symlink_to(tmp_path / "gone")

    result = adapter().remove_worktrees([str(orphan), str(dangling)])

    assert result == WorktreeCleanup((str(orphan), str(dangling)))
    assert dangling.is_symlink(), "a dangling symlink is a leftover, not a clean path"


def test_a_relative_cleanup_path_is_refused_rather_than_written_off(tmp_path, monkeypatch):
    """Absent *relative to some cwd* says nothing about the host, so it stays pending."""
    monkeypatch.chdir(tmp_path)

    assert adapter().remove_worktrees(["repo/.worktrees/issue-5"]) == WorktreeCleanup(
        ("repo/.worktrees/issue-5",)
    )


# --- the ownership label the Manifest named, not the engine's default (#108) ---


# --- per-project labels: the engine never learns their names, only the facts ---


# --- finding an issue this engine already opened (#393) ---

_MARK = "<!-- poller-opened: ext:sentry-autofix:WEB-7 -->"
_SINCE = "2026-07-27T12:00:00+00:00"


def test_a_marked_issue_is_found_by_listing_not_by_search():
    api = FakeGh(answers=[(("issue", "list"), ok([
        {"number": 9, "body": "unrelated", "createdAt": "2026-07-27T12:30:00Z"},
        {"number": 8, "body": f"blah\n{_MARK}", "createdAt": "2026-07-27T12:10:00Z"},
    ]))])
    assert adapter(api=api).find_marked_issue("agentdeck", _MARK, since=_SINCE) == (8, True)
    # A search would be wrong here: GitHub's index lags creation, and this is asked precisely
    # about an issue created moments ago.
    assert not api.asked("--search")


def test_absence_is_only_conclusive_once_the_listing_reaches_past_the_intent():
    """The hole this closes: a bounded page on a busy repository can miss the issue entirely,
    and calling that absence is exactly how the duplicate gets created."""
    older_than_intent = ok([
        {"number": 9, "body": "unrelated", "createdAt": "2026-07-27T12:30:00Z"},
        {"number": 7, "body": "also unrelated", "createdAt": "2026-07-27T11:00:00Z"},
    ])
    found, conclusive = adapter(
        api=FakeGh(answers=[(("issue", "list"), older_than_intent)])
    ).find_marked_issue("agentdeck", _MARK, since=_SINCE)
    assert (found, conclusive) == (None, True), "it saw past the intent, so absence is real"

    # Every issue on the page is newer than the intent, so the window is not covered.
    all_newer = ok([
        {"number": 9, "body": "unrelated", "createdAt": "2026-07-27T12:30:00Z"},
        {"number": 8, "body": "unrelated", "createdAt": "2026-07-27T12:20:00Z"},
    ])
    found, conclusive = adapter(
        api=FakeGh(answers=[(("issue", "list"), all_newer)])
    ).find_marked_issue("agentdeck", _MARK, since=_SINCE)
    assert (found, conclusive) == (None, False), "a page that proves nothing must say so"


def test_an_unreadable_listing_is_never_absence():
    api = FakeGh(default=(1, "", "boom"))
    assert adapter(api=api).find_marked_issue("agentdeck", _MARK, since=_SINCE) == (None, False)


def test_an_empty_repository_is_conclusively_without_the_issue():
    api = FakeGh(answers=[(("issue", "list"), ok([]))])
    assert adapter(api=api).find_marked_issue("agentdeck", _MARK, since=_SINCE) == (None, True)


def test_opening_an_issue_reads_its_number_off_the_url_it_prints():
    api = FakeGh(answers=[
        (("issue", "create"), (0, "https://github.com/eerovil/agentdeck/issues/412\n", "")),
    ])
    subject = adapter(api=api)
    assert subject.open_issue("agentdeck", "boom", "body") == 412
    # Owned in the same write, so the card is never briefly unlabelled.
    assert api.asked("--label", subject.ownership_label)


def test_an_extra_label_goes_on_in_the_same_write_as_ownership():
    api = FakeGh(answers=[
        (("issue", "create"), (0, "https://github.com/eerovil/agentdeck/issues/9\n", "")),
    ])
    subject = adapter(api=api)
    assert subject.open_issue("agentdeck", "t", "b", labels=("spec-only",)) == 9
    (call,) = [call for call in api.calls if call[:2] == ("issue", "create")]
    labels = [call[i + 1] for i, arg in enumerate(call) if arg == "--label"]
    assert labels == [subject.ownership_label, "spec-only"]


def test_a_notice_printed_beside_the_url_is_not_read_as_a_number():
    api = FakeGh(answers=[(("issue", "create"), (
        0, "Creating issue in eerovil/agentdeck\nhttps://github.com/eerovil/agentdeck/issues/7\n",
        ""))])
    assert adapter(api=api).open_issue("agentdeck", "boom", "body") == 7


def test_a_refused_creation_answers_none_rather_than_guessing():
    assert adapter(api=FakeGh(default=(1, "", "denied"))).open_issue("a", "t", "b") is None


# --- window readings for pacing (follow-up to #1137) ---


def _paced_usage(tmp_path, label, *, provider="claude_code", five=0.0, five_in_h=1.0):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    (tmp_path / f"usage-{provider}-{label}.json").write_text(
        json.dumps(
            {
                "account": f"{provider}:{label}",
                "fetched_at": now.isoformat(),
                "five_hour_pct": five,
                "five_hour_resets_at": (now + timedelta(hours=five_in_h)).isoformat(),
                "seven_day_pct": 10.0,
                "seven_day_resets_at": None,
                "month_pct": None,
                "month_resets_at": None,
            }
        )
    )


def test_window_readings_report_only_windows_with_both_a_meter_and_a_boundary(tmp_path):
    """Pacing needs the boundary as well as the number. A window carrying one without the
    other is left out rather than paired with a guessed value."""
    from agentdeck.poller_engine.github import account_window_readings

    _paced_usage(tmp_path, "main", five=45.0, five_in_h=4.25)
    readings = account_window_readings(tmp_path, "claude_code:main")

    assert [(window, pct) for window, pct, _ in readings] == [("five_hour", 45.0)]
    assert readings[0][2].tzinfo is not None


def test_window_readings_are_empty_without_a_usable_snapshot(tmp_path):
    from agentdeck.poller_engine.github import account_window_readings

    assert account_window_readings(tmp_path, "claude_code:nobody") == ()
    assert account_window_readings(None, "claude_code:main") == ()
    # The older cache format has no reset boundaries at all, so it reports no windows here
    # while `account_usage_pct` still reads its percentages.
    _usage(tmp_path, "main", five=80.0)
    assert account_window_readings(tmp_path, "claude_code:main") == ()


# --- budget about to expire unspent (#1965) ---


def _weekly(tmp_path, label, *, provider="claude_code", pct, resets_in_h, month=None):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    payload = {
        "account": f"{provider}:{label}",
        "fetched_at": now.isoformat(),
        "seven_day_pct": pct,
        "seven_day_resets_at": (now + timedelta(hours=resets_in_h)).isoformat(),
    }
    if month is not None:
        month_pct, month_in_h = month
        payload["month_pct"] = month_pct
        payload["month_resets_at"] = (now + timedelta(hours=month_in_h)).isoformat()
    (tmp_path / f"usage-{provider}-{label}.json").write_text(json.dumps(payload))


def test_a_window_about_to_reset_part_spent_reports_what_it_will_waste(tmp_path):
    """The operator's own example: alt resets in 12 hours and sits at 85%."""
    from agentdeck.poller_engine.github import account_expiring_headroom

    _weekly(tmp_path, "alt", pct=85.0, resets_in_h=12.0)

    assert account_expiring_headroom(tmp_path, "claude_code:alt") == 10.0
    assert account_expiring_headroom(tmp_path, "claude_code:alt", target_pct=99.0) == 14.0


def test_a_window_with_days_left_to_run_wastes_nothing_yet(tmp_path):
    """Being behind is only a loss once the remainder cannot plausibly be spent."""
    from agentdeck.poller_engine.github import account_expiring_headroom

    _weekly(tmp_path, "alt", pct=20.0, resets_in_h=120.0)

    assert account_expiring_headroom(tmp_path, "claude_code:alt") == 0.0


def test_an_account_at_its_target_and_an_unknown_one_both_waste_nothing(tmp_path):
    """Ignorance must not promote an account over one with a real reading."""
    from agentdeck.poller_engine.github import account_expiring_headroom

    _weekly(tmp_path, "main", pct=97.0, resets_in_h=3.0)

    assert account_expiring_headroom(tmp_path, "claude_code:main") == 0.0
    assert account_expiring_headroom(tmp_path, "claude_code:nobody") == 0.0
    assert account_expiring_headroom(None, "claude_code:main") == 0.0


def test_a_target_of_zero_opts_the_account_out(tmp_path):
    from agentdeck.poller_engine.github import account_expiring_headroom

    _weekly(tmp_path, "alt", pct=40.0, resets_in_h=2.0)

    assert account_expiring_headroom(tmp_path, "claude_code:alt", target_pct=0.0) == 0.0


def test_the_five_hour_rate_limit_is_not_a_budget_that_expires(tmp_path):
    """A five-hour window refills on its own, so leaving it part spent costs nothing and
    chasing it would flip accounts every few hours for no gain."""
    from agentdeck.poller_engine.github import account_expiring_headroom

    _paced_usage(tmp_path, "alt", five=20.0, five_in_h=0.25)

    assert account_expiring_headroom(tmp_path, "claude_code:alt") == 0.0


def test_the_worst_expiring_window_is_the_one_that_counts(tmp_path):
    """Two budget windows can both be closing; the account wastes the larger of the two."""
    from agentdeck.poller_engine.github import account_expiring_headroom

    _weekly(tmp_path, "alt", pct=90.0, resets_in_h=6.0, month=(60.0, 100.0))

    assert account_expiring_headroom(tmp_path, "claude_code:alt") == 35.0


# --- budget left per hour (#2757) ---


def test_budget_pace_is_points_left_per_hour_to_the_reset(tmp_path):
    from agentdeck.poller_engine.github import account_budget_pace

    _weekly(tmp_path, "alt", pct=11.0, resets_in_h=84.0)

    assert account_budget_pace(tmp_path, "claude_code:alt") == pytest.approx(1.0, rel=1e-3)
    assert account_budget_pace(tmp_path, "claude_code:alt", target_pct=0.0) == 0.0


def test_budget_pace_is_zero_past_the_target_and_none_when_unknown(tmp_path):
    from agentdeck.poller_engine.github import account_budget_pace

    _weekly(tmp_path, "main", pct=97.0, resets_in_h=30.0)

    assert account_budget_pace(tmp_path, "claude_code:main") == 0.0
    assert account_budget_pace(tmp_path, "claude_code:nobody") is None
    assert account_budget_pace(None, "claude_code:main") is None


def test_budget_pace_ignores_the_five_hour_window_and_a_past_reset(tmp_path):
    from agentdeck.poller_engine.github import account_budget_pace

    _paced_usage(tmp_path, "alt", five=20.0, five_in_h=0.25)
    assert account_budget_pace(tmp_path, "claude_code:alt") is None

    _weekly(tmp_path, "main", pct=10.0, resets_in_h=-1.0)
    assert account_budget_pace(tmp_path, "claude_code:main") is None


def test_budget_pace_takes_the_faster_of_two_budget_windows(tmp_path):
    from agentdeck.poller_engine.github import account_budget_pace

    _weekly(tmp_path, "alt", pct=55.0, resets_in_h=100.0, month=(35.0, 20.0))

    assert account_budget_pace(tmp_path, "claude_code:alt") == pytest.approx(3.0, rel=1e-3)


# --- when a refusal clears (#1726) ---


def test_a_refusal_clears_only_once_every_blocked_window_has_reset(tmp_path):
    """The reason names the first blocked window; the boundary is the last one to reset,
    because the account cannot run a turn until all of them have."""
    from datetime import UTC, datetime, timedelta

    from agentdeck.poller_engine.github import account_refusal_detail

    now = datetime.now(UTC)
    soon, later = now + timedelta(hours=2), now + timedelta(days=3)
    (tmp_path / "usage-claude_code-main.json").write_text(
        json.dumps(
            {
                "account": "claude_code:main",
                "fetched_at": now.isoformat(),
                "five_hour_pct": 100.0,
                "five_hour_resets_at": soon.isoformat(),
                "seven_day_pct": 100.0,
                "seven_day_resets_at": later.isoformat(),
            }
        )
    )

    reason, clears_at = account_refusal_detail(tmp_path, "claude_code:main")

    assert reason.startswith("five_hour window at 100%")
    assert clears_at == later


def test_an_account_that_is_not_refusing_reports_no_boundary(tmp_path):
    from agentdeck.poller_engine.github import account_refusal_detail

    _paced_usage(tmp_path, "alt", five=31.0)

    assert account_refusal_detail(tmp_path, "claude_code:alt") == ("", None)


def test_the_reserve_gate_reuses_a_reading_too_old_to_report_rather_than_buying_one(
    tmp_path, monkeypatch
):
    """#2082: a quiet wake must not spend a core call to find out whether it may spend core calls.

    `live_queries_halt` reaches here on every wake including the webhook-quiet ones, and those
    make no other GitHub call — so at the 30s reporting freshness the store was always empty by
    the time the gate looked, and the gate bought its own answer. Seven instances, ~670 wakes an
    hour. A reading minutes old still answers "is there room?" correctly, and errs towards
    refusing rather than towards overspending, because `remaining` only rises at the reset.

    That last sentence is only true while every spender records what it spends, and on a *full*
    wake one need not: an extension may read GitHub over its own HTTP client from a separate
    process and keep none of the response headers (`chatgpt-nudge` did, until #3741). So the
    tolerance is offered per wake, and the default — a full wake — still buys a fresh reading.
    """
    cache = tmp_path / "github.sqlite3"
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(cache))
    calls = []

    def completed(argv, **_kwargs):
        calls.append(argv[1:])
        trace = (
            "* Request at now\n"
            "< HTTP/2.0 200 OK\n"
            "< X-RateLimit-Limit: 5000\n"
            "< X-RateLimit-Remaining: 4200\n"
            "< X-RateLimit-Resource: core\n"
            "* Request took 1ms\n"
        )
        stdout = (
            '{"id":20142828}'
            if argv[1:] == ["api", "user"]
            else '{"resources":{"core":{"remaining":4200},"graphql":{"remaining":700}}}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout, trace)

    monkeypatch.setattr(subprocess, "run", completed)

    first = Gh(token="token-a").current_rate_limits(max_age=rate_limits.GATE_MAX_AGE_S)
    assert first["core"].remaining == 4200
    probes = calls.count(["api", "user"])

    # Well past the reporting freshness, well inside the gate's tolerance.
    base = time.time()
    monkeypatch.setattr(
        rate_limits,
        "time",
        SimpleNamespace(time=lambda: base + rate_limits.READING_MAX_AGE_S * 4),
    )

    later = Gh(token="token-a").current_rate_limits(max_age=rate_limits.GATE_MAX_AGE_S)

    assert later["core"].remaining == 4200
    assert calls.count(["api", "user"]) == probes, "the gate bought a reading it already had"

    # A full wake asks at the reporting freshness, because the nudge extension may have spent
    # core quota since without recording a header anywhere.
    assert Gh(token="token-a").current_rate_limits()["core"].remaining == 4200
    assert calls.count(["api", "user"]) > probes, "a full wake trusted an unaccountable reading"


def test_only_a_webhook_quiet_wake_is_offered_the_stale_reserve_reading(tmp_path, monkeypatch):
    """#2082 review: the stale-reading proof does not cover the nudge extension's raw calls.

    `chatgpt_nudge_core._get` (retired in #3741) spent the same core budget through `urllib` and
    threw the `x-ratelimit-*` response headers away, as any extension may, so between two full
    wakes inside `GATE_MAX_AGE_S` the real bucket can fall below the reserve while the gate still
    reads the older, higher number. A webhook-quiet wake is the one an extension is told to ask
    GitHub nothing on, so the tolerance is handed out per wake, not globally.
    """
    cache = tmp_path / "github.sqlite3"
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(cache))
    api = FakeGh([(("rate_limit",), ok('{"resources":{"core":{"remaining":4200},'
                                       '"graphql":{"remaining":700}}}'))])

    assert adapter(api=api).live_queries_halt(stale_reading_ok=True) is None
    assert api.rate_limit_max_age == rate_limits.GATE_MAX_AGE_S

    assert adapter(api=api).live_queries_halt() is None
    assert api.rate_limit_max_age == rate_limits.READING_MAX_AGE_S


def _thread(*comments, remaining: int = 4000) -> tuple[int, str, str]:
    """One GraphQL answer shaped like the issue-comment page the retirement pass reads."""
    return ok(
        {
            "data": {
                "rateLimit": {
                    "remaining": remaining,
                    "resetAt": "2026-09-22T09:00:00Z",
                    "cost": 1,
                },
                "repository": {"issue": {"comments": {"nodes": list(comments)}}},
            }
        }
    )


def _bot(node_id: str, text: str = "Report.", minimized: bool = False) -> dict:
    return {"id": node_id, "isMinimized": minimized, "body": f"{text}\n\n{BOT_MARKER}"}


def _minimized_ids(api: FakeGh) -> list[str]:
    """Every comment id this run actually asked GitHub to hide."""
    mutations = [
        " ".join(call) for call in api.calls if "minimizeComment" in " ".join(call)
    ]
    return [
        part.split('"')[1]
        for call in mutations
        for part in call.split("subjectId:")[1:]
    ]


def test_a_new_poller_comment_hides_the_cards_older_poller_comments():
    """#2116: the thread grew by re-posting and nothing was ever marked superseded.

    agentdeck#2108 was 77 KB, 64 KB of it four near-identical review packets, one per
    re-dispatch. Only the newest stays open; the rest collapse to a grey row.
    """
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}, "r1": {}}})),
            (("graphql",), _thread(_bot("C_one"), _bot("C_two"), _bot("C_new"))),
        ]
    )

    assert adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, "Report.")
    ) is True

    # The two older ones, and never the newest — that is the comment a reader opens the card
    # to find.
    assert _minimized_ids(api) == ["C_one", "C_two"]


def test_only_the_pollers_own_comments_are_ever_hidden():
    """A person's instruction and an external reviewer's verdict stay visible however old.

    The discriminator is the marker the engine appends to its own comments and to nothing
    else, so this holds without the adapter knowing which accounts are agents.
    """
    human = {"id": "C_human", "isMinimized": False, "body": "/claude please redo the guard"}
    reviewer = {"id": "C_review", "isMinimized": False, "body": "Looks good to me."}
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (("graphql",), _thread(_bot("C_old"), human, reviewer, _bot("C_new"))),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert _minimized_ids(api) == ["C_old"]


def _review(node_id: str, marker: bool = True, minimized: bool = False) -> dict:
    body = "/claude\nReview findings for PR #9 at abc123:\n\n1. a.py:1 — wrong → fix it"
    if marker:
        body += "\n\n" + CHATGPT_REVIEW_MARKER
    return {"id": node_id, "isMinimized": minimized, "body": body}


def test_older_chatgpt_reviews_are_hidden_and_the_newest_stays_open():
    """#2167: every review round re-posts a findings list, and only the newest still applies.

    Reviews posted before the reviewer carried a marker are recognised by their heading, and
    the newest review stays open beside the newest poller report even when it is older than
    that report, because the worker may not have answered it yet.
    """
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}, "r1": {}, "r2": {}, "r3": {}}})),
            (
                ("graphql",),
                _thread(
                    _review("R_legacy", marker=False),
                    {"id": "R_oneline", "isMinimized": False,
                     "body": "/claude Review findings for PR #9 at abc123:\n\n1. a.py:1"},
                    _bot("C_old"),
                    _review("R_mid"),
                    _review("R_new"),
                    _bot("C_new"),
                ),
            ),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert sorted(_minimized_ids(api)) == ["C_old", "R_legacy", "R_mid", "R_oneline"]


def _report(node_id: str, status: str = "in_review", minimized: bool = False) -> dict:
    marker = f"{TERMINAL_MARKER}gen-{node_id} status={status} -->"
    return {
        "id": node_id,
        "isMinimized": minimized,
        "body": f"What was done.\n\n{marker}\n\n{BOT_MARKER}",
    }


def test_the_newest_worker_report_stays_open_beside_later_notices():
    """#2689: on agentdeck#2687 Releasing, Merged and Done hid the report saying what was done."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}, "r1": {}, "r2": {}}})),
            (
                ("graphql",),
                _thread(
                    _report("W_old"),
                    _bot("N_a"),
                    _report("W_new"),
                    _bot("N_b"),
                    _bot("N_c"),
                ),
            ),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Done."))

    assert sorted(_minimized_ids(api)) == ["N_a", "N_b", "W_old"]


def test_a_resumed_marker_is_a_notice_not_a_report():
    """A relayed answer or a reopen note says the work restarted, not what was done."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (("graphql",), _thread(_report("W"), _report("R", status="resumed"), _bot("N"))),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Note."))

    assert _minimized_ids(api) == ["R"]


def test_only_the_newest_of_several_worker_reports_stays_open():
    """Older reports still fold, so a thread never stacks old review packets again (#2116)."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}, "r1": {}}})),
            (
                ("graphql",),
                _thread(
                    _report("W_one", status="blocked"),
                    _report("W_two", status="needs_more_info"),
                    _report("W_three", minimized=True),
                    {"id": "W_bare", "isMinimized": False,
                     "body": f"{TERMINAL_MARKER}gen-bare -->\n\n{BOT_MARKER}"},
                    _report("W_new"),
                ),
            ),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    # A marker with no status still reads as a report ("acknowledged"), so it folds as one.
    assert sorted(_minimized_ids(api)) == ["W_bare", "W_one", "W_two"]


def test_a_quoted_terminal_marker_without_the_bot_marker_is_never_touched():
    """Only the poller's own comments are reports; a person quoting one stays as written."""
    quote = {
        "id": "H_quote",
        "isMinimized": False,
        "body": f"look at this: {TERMINAL_MARKER}gen status=in_review -->",
    }
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {}})),
            (("graphql",), _thread(quote, _report("W"), {"id": "H_empty", "body": None})),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert _minimized_ids(api) == []


def test_a_persons_claude_instruction_is_never_taken_for_a_review():
    """Only the findings heading or the reviewer's marker makes a comment a review."""
    nodes = [
        {"id": "H_one", "isMinimized": False, "body": "/claude please redo the guard"},
        {"id": "H_two", "isMinimized": False, "body": "/claude\nReview the findings for PR #9"},
        {"id": "H_three", "isMinimized": False, "body": "Review findings for PR #9 at abc"},
        {"id": "H_four", "isMinimized": False, "body": "/claude\nreview findings for PR #9"},
        {"id": "H_five", "isMinimized": False, "body": ""},
        {"id": "H_six", "isMinimized": False, "body": None},
        _review("R_only"),
        {"id": "B_quote", "isMinimized": False,
         "body": "/claude\nReview findings for PR #9\n\n" + BOT_MARKER},
    ]
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (("graphql",), _thread(*nodes, _bot("C_new"))),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    # The quoting comment is the poller's own and older than its newest, so it goes as one;
    # the lone review is the newest review and stays.
    assert _minimized_ids(api) == ["B_quote"]


def test_an_already_hidden_comment_is_not_hidden_again():
    """Every later comment re-runs this pass; it must not re-pay for work already done."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (
                ("graphql",),
                _thread(
                    _bot("C_done", minimized=True),
                    _bot("C_open"),
                    _bot("C_new"),
                ),
            ),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert _minimized_ids(api) == ["C_open"]


def test_a_comment_that_did_not_land_retires_nothing():
    """Nothing was superseded, because nothing replaced it."""
    api = FakeGh(default=(1, "", "no such issue"))

    assert adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, "Report.")
    ) is False
    assert not api.asked("minimizeComment")


def test_a_failed_retirement_never_unlands_the_comment():
    """The comment is the durable fact; tidying up after it is not allowed to deny it."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), (1, "", "Resource not accessible")),
            (("graphql",), _thread(_bot("C_old"), _bot("C_new"))),
        ]
    )

    assert adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, "Report.")
    ) is True


def test_an_unreadable_thread_hides_nothing():
    """An answer this pass cannot parse is unknown, never an empty thread to act on."""
    for answer in ((0, "not json", ""), ok({"data": {"repository": None}}), (1, "", "boom")):
        api = FakeGh(answers=[(("graphql",), answer)])

        assert adapter(api=api).apply(
            Effect(EffectKind.COMMENT, "agentdeck", 5, "Report.")
        ) is True
        assert not api.asked("minimizeComment")


def test_a_comment_id_that_is_not_a_node_id_is_left_alone():
    """Ids are pasted into a mutation document, so a surprising one is dropped, not sent.

    GitHub has never returned anything but an opaque node id here. The guard is for the day
    something else arrives, because the cost of being wrong is a document this engine wrote.
    """
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (
                ("graphql",),
                _thread(
                    _bot('C_bad",classifier:OUTDATED}){__typename}}#'),
                    _bot("C_fine"),
                    _bot("C_new"),
                ),
            ),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert _minimized_ids(api) == ["C_fine"]


def test_one_pass_hides_at_most_its_own_limit():
    """The cap bounds the document, not the work: the next comment hides the remainder."""
    crowd = [_bot(f"C_{index}") for index in range(github_module.RETIRE_COMMENT_LIMIT + 5)]
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {}})),
            (("graphql",), _thread(*crowd, _bot("C_new"))),
        ]
    )

    adapter(api=api).apply(Effect(EffectKind.COMMENT, "agentdeck", 5, "Report."))

    assert len(_minimized_ids(api)) == github_module.RETIRE_COMMENT_LIMIT


def test_retirement_reports_only_the_comments_github_confirmed():
    """A mutation can apply some aliases and fail others; the count follows the answer."""
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {"minimizedComment": {}}, "r1": None}})),
            (("graphql",), _thread(_bot("C_one"), _bot("C_two"), _bot("C_new"))),
        ]
    )

    assert adapter(api=api).retire_superseded_comments("agentdeck", 5) == 1


def test_a_spent_graphql_budget_retires_nothing():
    """Tidying is the first thing to give way when the shared reserve is low."""
    api = FakeGh(
        answers=[
            (
                ("rate_limit",),
                ok({"resources": {"graphql": {"remaining": 1}, "core": {"remaining": 5000}}}),
            ),
        ]
    )

    assert adapter(api=api).retire_superseded_comments("agentdeck", 5) == 0
    assert not api.asked("minimizeComment")


def test_both_retirement_documents_are_well_formed_graphql():
    """Both of these shipped broken once and no fake caught it (#2116).

    A `FakeGh` answers whatever it is handed, so an unbalanced brace or a field that does not
    exist on the type passes every behavioural test above and fails only against GitHub. Two
    things are checked here because both were really wrong: the read query was one closing
    brace short, and the mutation asked for `rateLimit`, which exists on GitHub's Query type
    and not on Mutation — so the whole document was a schema error that hid nothing.
    """
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (("graphql",), _thread(_bot("C_old"), _bot("C_new"))),
        ]
    )

    adapter(api=api).retire_superseded_comments("agentdeck", 5)

    documents = [
        part[len("query="):]
        for call in api.calls
        for part in call
        if part.startswith("query=")
    ]
    assert len(documents) == 2
    for document in documents:
        assert document.count("{") == document.count("}")
    read, mutation = documents
    assert read.startswith("query(")
    assert mutation.startswith("mutation{")
    assert "rateLimit" in read
    assert "rateLimit" not in mutation


def test_a_cards_first_poller_comment_hides_nothing():
    """The boundary the pass runs at every single time a card opens: one comment, no history."""
    api = FakeGh(answers=[(("graphql",), _thread(_bot("C_only")))])

    assert adapter(api=api).apply(
        Effect(EffectKind.COMMENT, "agentdeck", 5, "Plan.")
    ) is True
    assert not api.asked("minimizeComment")


def test_a_human_replying_last_does_not_cost_the_poller_its_visible_comment():
    """The newest *poller* comment is what stays open, not the newest comment on the thread.

    A person answering straight after a report is the common case, and hiding the report they
    are answering would be the exact unreadability this was meant to fix.
    """
    human = {"id": "C_reply", "isMinimized": False, "body": "/claude thanks, carry on"}
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (("graphql",), _thread(_bot("C_old"), _bot("C_report"), human)),
        ]
    )

    adapter(api=api).retire_superseded_comments("agentdeck", 5)

    assert _minimized_ids(api) == ["C_old"]


def test_a_retirement_charges_its_own_mutation_against_the_cached_reserve():
    """Two passes near the floor: the second must refuse, not spend through the reserve.

    A mutation cannot report `rateLimit` — the field is on GitHub's Query type only — so the
    cached remaining count refreshes on reads and never on hides. Left unaccounted, a pass that
    starts one point above the reserve leaves the cache claiming a point it already spent, and
    the next pass reads on the strength of it and crosses the floor.
    """
    api = FakeGh(
        answers=[
            (("minimizeComment",), ok({"data": {"r0": {}}})),
            (
                ("rate_limit",),
                ok({"resources": {"graphql": {"remaining": 102}, "core": {"remaining": 5000}}}),
            ),
            (("graphql",), _thread(_bot("C_old"), _bot("C_new"), remaining=101)),
        ]
    )
    subject = adapter(api=api, graphql_reserve=100)

    assert subject.retire_superseded_comments("agentdeck", 5) == 1
    assert subject._graphql_remaining == 100

    assert subject.retire_superseded_comments("agentdeck", 5) == 0
    assert _minimized_ids(api) == ["C_old"]


def test_the_poller_never_reads_through_the_worker_gh_shim(tmp_path, monkeypatch):
    """Worker-control runs in a worker's shell, shim first on PATH (#2698)."""
    shim = tmp_path / "gh-shim"
    monkeypatch.setenv("AGENTDECK_GH_SHIM_DIR", str(shim))
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}/usr/bin{os.pathsep}{shim}/")

    path = Gh(token="one-token")._environment()["PATH"]

    assert path.split(os.pathsep) == ["/usr/bin"]


@pytest.mark.parametrize(
    "variables",
    [{}, {"XDG_DATA_HOME": "/data"}, {"AGENTDECK_GH_SHIM_DIR": "~/shim"}],
)
def test_the_engine_finds_the_shim_where_the_shim_installs_itself(monkeypatch, variables):
    from agentdeck.gh_shim.install import shim_dir
    from agentdeck.poller_engine.github import _gh_shim_dir

    for name in ("AGENTDECK_GH_SHIM_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    assert _gh_shim_dir() == Path(shim_dir())


