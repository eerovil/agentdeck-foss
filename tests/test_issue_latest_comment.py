"""Deck issue links open at the issue's newest comment (#2424).

Deliveries go through `WebhookLog` and `WebhookTailer`, the same appender and reader the
live path uses, so the consumer is asserted over real stream bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates

from agentdeck.github_cache import GitHubMetadataCache
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.issue_latest_comment import (
    CONSUMER,
    LatestCommentService,
    cache_key,
    issue_href,
)
from agentdeck.web import render
from agentdeck.webhook_tailer import WebhookTailer

AGENTDECK = Repository(owner="eerovil", name="agentdeck")
ISSUE = "https://github.com/eerovil/agentdeck/issues/3"


def comment_body(
    comment_id: int,
    *,
    number: int = 3,
    action: str = "created",
    pull_request: bool = False,
    repository: Repository = AGENTDECK,
) -> bytes:
    issue: dict = {"number": number, "title": "Links", "state": "open", "comments": 1}
    if pull_request:
        issue["pull_request"] = {"url": "https://api.github.com/x"}
    return json.dumps(
        {
            "action": action,
            "issue": issue,
            "comment": {
                "id": comment_id,
                "html_url": f"https://github.com/{repository.slug}/issues/{number}"
                f"#issuecomment-{comment_id}",
            },
            "repository": {
                "full_name": repository.slug,
                "name": repository.name,
                "owner": {"login": repository.owner},
            },
        }
    ).encode()


@pytest.fixture
def root(tmp_path):
    return tmp_path / "github-webhooks"


@pytest.fixture
def cache(tmp_path) -> GitHubMetadataCache:
    return GitHubMetadataCache(tmp_path / "github-cache.sqlite3")


@pytest.fixture
def service(root, cache) -> LatestCommentService:
    return LatestCommentService(root, cache=cache)


counter = iter(range(1_000_000))


def deliver(root, body: bytes, *, event: str = "issue_comment") -> None:
    WebhookLog(root).append(
        body=body,
        headers={"X-GitHub-Delivery": f"d-{next(counter)}", "X-GitHub-Event": event},
    )


def apply_stream(root, service: LatestCommentService) -> None:
    tailer = WebhookTailer(root, consumer="test-apply")
    service.apply_batch(tailer.read())
    tailer.commit()


def test_a_new_comment_anchors_the_issue_link(root, cache, service):
    deliver(root, comment_body(501))
    apply_stream(root, service)

    assert issue_href(ISSUE, cache) == f"{ISSUE}#issuecomment-501"


def test_an_older_comment_arriving_later_never_moves_the_anchor_back(root, cache, service):
    # Same-second comments and replays arrive in any order; the larger id is the newer one.
    deliver(root, comment_body(502))
    deliver(root, comment_body(501))
    apply_stream(root, service)

    assert issue_href(ISSUE, cache) == f"{ISSUE}#issuecomment-502"


def test_a_pull_request_comment_stores_nothing(root, cache, service):
    deliver(root, comment_body(501, pull_request=True))
    apply_stream(root, service)

    assert cache.peek(cache_key("eerovil/agentdeck#3")).value is None
    assert issue_href(ISSUE, cache) == ISSUE


def test_deleting_the_newest_comment_clears_the_anchor_for_good(root, cache, service):
    deliver(root, comment_body(501))
    deliver(root, comment_body(501, action="deleted"))
    # A replay of the deleted comment's own creation cannot bring it back.
    deliver(root, comment_body(501))
    apply_stream(root, service)

    assert issue_href(ISSUE, cache) == ISSUE

    deliver(root, comment_body(503))
    apply_stream(root, service)
    assert issue_href(ISSUE, cache) == f"{ISSUE}#issuecomment-503"


def test_deleting_another_comment_or_editing_keeps_the_anchor(root, cache, service):
    deliver(root, comment_body(502))
    deliver(root, comment_body(501, action="deleted"))
    deliver(root, comment_body(502, action="edited"))
    deliver(root, comment_body(900), event="issues")
    apply_stream(root, service)

    assert issue_href(ISSUE, cache) == f"{ISSUE}#issuecomment-502"


def test_repositories_and_case_do_not_mix(root, cache, service):
    other = Repository(owner="eerovil", name="menu-app")
    deliver(root, comment_body(501))
    deliver(root, comment_body(777, repository=other))
    apply_stream(root, service)

    assert issue_href("https://github.com/EeroVil/AgentDeck/issues/3", cache).endswith(
        "#issuecomment-501"
    )
    assert issue_href("https://github.com/eerovil/menu-app/issues/3", cache).endswith(
        "#issuecomment-777"
    )
    assert issue_href("https://github.com/eerovil/agentdeck/issues/33", cache).endswith("/33")


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "/sessions/claude_code:main:abc",
        "https://github.com/eerovil/agentdeck/pull/3",
        "https://github.com/eerovil/agentdeck/issues/3#new_comment_field",
        "https://example.com/eerovil/agentdeck/issues/3",
    ],
)
def test_anything_but_a_plain_issue_url_passes_through(root, cache, service, url):
    deliver(root, comment_body(501))
    apply_stream(root, service)

    assert issue_href(url, cache) == url


def test_a_new_consumer_replays_the_streams_already_on_disk(root, cache, service):
    # Written before the service ever ran: its own consumer name starts at offset 0.
    deliver(root, comment_body(501))
    tailer = WebhookTailer(root, consumer=CONSUMER)
    service.apply_batch(tailer.read())

    assert issue_href(ISSUE, cache) == f"{ISSUE}#issuecomment-501"


def test_templates_anchor_issue_links_and_leave_the_reply_box_link(root, service):
    # The filter reads the deck's default cache, which conftest points at a temp file.
    live = LatestCommentService(root, cache=GitHubMetadataCache())
    deliver(root, comment_body(501))
    apply_stream(root, live)

    templates = Jinja2Templates(directory=str(Path(render.__file__).parent / "templates"))
    render.register_filters(templates)
    page = templates.env.from_string(
        '<a href="{{ a|issue_href }}"></a><a href="{{ b|issue_href }}"></a>'
    ).render(a=ISSUE, b=f"{ISSUE}#new_comment_field")

    assert f'href="{ISSUE}#issuecomment-501"' in page
    assert f'href="{ISSUE}#new_comment_field"' in page
