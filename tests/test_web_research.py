"""`agentdeck read` and `agentdeck research` (#4290), against the shapes each site really sends."""

import json

import httpx
import pytest

from agentdeck import __main__ as cli
from agentdeck import web_read
from agentdeck.web_read import (
    EXIT_CODES,
    REDDIT_SNAPSHOT,
    html_to_text,
    plan,
    read_url,
)
from agentdeck.web_research import build_prompt, research_account

THREAD = "https://www.reddit.com/r/ClaudeCode/comments/1xdqey0/claude_code_sessions_dashboard/"
SHARE = "https://www.reddit.com/r/ClaudeCode/s/AbC123"

POST_ID = "1xdqey0"
ARCHIVE = "https://arctic-shift.photon-reddit.com/api"
ARCHIVE_POST = f"{ARCHIVE}/posts/ids?ids={POST_ID}"

# Trimmed from the Arctic Shift archive's answers for THREAD, keeping the fields read: the post
# from /posts/ids, then /comments/tree with a "more" stub where the archive cut a branch
# (Reddit's own listing shape; bodies are Markdown with &, < and > escaped).
REDDIT_POST = {
    "data": [
        {
            "id": POST_ID,
            "title": "claude code sessions dashboard",
            "author": "Amazing_Ad9369",
            "selftext": "it brings all your claude code sessions into one place, keeps them"
            " and allows you to search",
            "num_comments": 3,
            "created_utc": 1_700_000_000,
            "_meta": {"retrieved_2nd_on": 1_700_200_000},
        }
    ]
}


def _comment(author, body, replies=()):
    listing = {"kind": "Listing", "data": {"children": list(replies)}} if replies else ""
    return {"kind": "t1", "data": {"author": author, "body": body, "replies": listing}}


REDDIT_TREE = {
    "data": [
        _comment(
            "HowzoTV",
            "The nerf check with limit health and trends is the part that caught my eye.",
            [_comment("Amazing_Ad9369", "thanks &amp; more to come")],
        ),
        _comment("someone", "Terminal from your phone is a great add."),
    ]
}


def _reddit_handler(tree=REDDIT_TREE, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        if request.url.path == "/api/posts/ids":
            return httpx.Response(200, json=REDDIT_POST)
        assert request.url.params["link_id"] == POST_ID
        return httpx.Response(200, json=tree)

    return handler


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (THREAD, ("reddit-archive", ARCHIVE_POST)),
        (THREAD.replace("www.", "old.") + "abc123/", ("reddit-archive", ARCHIVE_POST)),
        ("https://reddit.com/comments/1XDQEY0", ("reddit-archive", ARCHIVE_POST)),
        ("https://redd.it/1xdqey0", ("reddit-archive", ARCHIVE_POST)),
        ("https://www.reddit.com/gallery/1xdqey0", ("reddit-archive", ARCHIVE_POST)),
        ("https://reddit.com/user/x/comments/1xdqey0/t/", ("reddit-archive", ARCHIVE_POST)),
        (
            "https://www.reddit.com/r/ClaudeCode/s/AbC123",
            ("reddit-share", "https://www.reddit.com/r/ClaudeCode/s/AbC123"),
        ),
        (
            "https://reddit.com/r/selfhosted/",
            ("reddit-archive", f"{ARCHIVE}/posts/search?subreddit=selfhosted&limit=100&sort=desc"),
        ),
        ("https://m.reddit.com/r/x/comments/1xdqey0/t/", ("reddit-archive", ARCHIVE_POST)),
        ("https://sh.reddit.com/r/x/comments/1xdqey0/", ("reddit-archive", ARCHIVE_POST)),
        (
            "https://m.reddit.com/r/ClaudeCode/s/AbC123",
            ("reddit-share", "https://m.reddit.com/r/ClaudeCode/s/AbC123"),
        ),
        (
            "https://notreddit.com/comments/1xdqey0/",
            ("page", "https://notreddit.com/comments/1xdqey0/"),
        ),
        ("https://www.reddit.com/user/x/", ("page", "https://www.reddit.com/user/x/")),
        (
            "https://news.ycombinator.com/item?id=8863",
            ("page", "https://news.ycombinator.com/item?id=8863"),
        ),
        ("https://news.ycombinator.com/news", ("page", "https://news.ycombinator.com/news")),
        (
            "https://stackoverflow.com/questions/11227809/why-is-it",
            ("page", "https://stackoverflow.com/questions/11227809/why-is-it"),
        ),
        ("https://medium.com/", ("page", "https://medium.com/")),
    ],
)
def test_each_site_is_read_through_its_own_feed_or_api(url, expected):
    assert plan(url) == expected


def test_reddit_thread_reads_post_and_every_comment_from_the_archive():
    seen = []
    with _client(_reddit_handler(seen=seen)) as client:
        result = read_url(THREAD, client=client)
    assert result.status == "ok" and result.source == "reddit-archive"
    assert result.partial == REDDIT_SNAPSHOT
    assert "agentdeck-web-read" in seen[0].headers["user-agent"]
    assert "keeps them and allows you to search" in result.text
    assert "The nerf check with limit health and trends" in result.text
    assert "thanks & more to come" in result.text  # a reply, unescaped
    assert "Terminal from your phone" in result.text


def test_comments_the_archive_has_not_listed_make_a_partial_read():
    more = {"kind": "more", "data": {"count": 4, "children": ["a"]}}
    with _client(_reddit_handler({"data": [*REDDIT_TREE["data"][:1], more]})) as client:
        cut_branch = read_url(THREAD, client=client)
    assert cut_branch.partial == f"{REDDIT_SNAPSHOT}; 4 comments the archive did not list"
    with _client(lambda request: httpx.Response(200, json={"data": []})) as client:
        assert read_url(THREAD, client=client).status == "error"


def test_the_whole_tree_is_asked_for_and_read_however_wide_or_deep():
    seen = []
    deep = _comment("d5", "fifth level")
    for level in (4, 3, 2, 1):
        deep = _comment(f"d{level}", f"level {level}", [deep])
    siblings = [_comment(f"s{n}", f"sibling {n}") for n in range(5)]
    with _client(_reddit_handler({"data": [*siblings, deep]}, seen=seen)) as client:
        result = read_url(THREAD, client=client)
    tree_params = seen[1].url.params
    assert tree_params["start_breadth"] == tree_params["start_depth"] == "10000"
    assert "sibling 4" in result.text and "fifth level" in result.text


def test_a_reddit_share_link_is_followed_to_its_thread_once():
    seen = []
    archive = _reddit_handler(seen=seen)

    def handler(request):
        if request.url.host == "www.reddit.com":
            seen.append(request)
            if request.url.path == "/r/ClaudeCode/s/AbC123":
                return httpx.Response(301, headers={"location": THREAD + "?share_id=x"})
            if request.url.path == "/r/ClaudeCode/s/Away1":
                return httpx.Response(
                    301, headers={"location": "https://evil.example/comments/x1/"}
                )
            return httpx.Response(307, headers={"location": "https://www.reddit.com/r/ClaudeCode/"})
        return archive(request)

    with _client(handler) as client:
        result = read_url(SHARE, client=client)
        dead = read_url("https://www.reddit.com/r/ClaudeCode/s/Gone99", client=client)
    assert result.status == "ok" and result.source == "reddit-archive"
    assert "Terminal from your phone" in result.text
    assert "agentdeck-web-read" in seen[0].headers["user-agent"]
    assert dead.status == "error" and "did not lead to a Reddit thread" in dead.detail
    with _client(handler) as client:
        away = read_url("https://www.reddit.com/r/ClaudeCode/s/Away1", client=client)
    assert away.status == "error" and "did not lead to a Reddit thread" in away.detail


def test_a_page_past_the_byte_budget_is_cut_while_it_streams(monkeypatch):
    monkeypatch.setattr(web_read, "MAX_BYTES", 1000)
    sent = []

    def flood():
        for _ in range(100):
            sent.append(1)
            yield b"<p>" + b"x" * 96 + b"</p>"

    with _client(lambda request: httpx.Response(200, content=flood())) as client:
        result = read_url("https://example.com/huge", client=client)
    assert result.status == "ok" and result.partial == "cut at 1000 bytes"
    assert len(result.text) <= 1000 and len(sent) < 20


def test_a_comment_tree_past_the_byte_budget_keeps_the_post_as_a_partial_read(monkeypatch):
    monkeypatch.setattr(web_read, "MAX_BYTES", 2000)
    big = {"data": [_comment("a", "y" * 5000)]}
    with _client(_reddit_handler(tree=big)) as client:
        result = read_url(THREAD, client=client)
    assert result.status == "ok"
    assert REDDIT_POST["data"][0]["title"] in result.text
    assert "comments: the answer was cut or not valid JSON" in result.partial


def test_redirect_hops_share_the_whole_reads_deadline():
    now = [0.0]
    hops = []

    def handler(request):
        hops.append(now[0])
        now[0] += 8.0  # each hop takes eight seconds to answer
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"/{n + 1}"})

    with _client(handler) as client:
        slow = read_url("https://example.com/", client=client, clock=lambda: now[0])
    assert slow.status == "error" and "took over 20 s" in slow.detail
    assert hops == [0.0, 8.0, 16.0]  # no hop starts once the 20 s are used
    with _client(lambda request: httpx.Response(302, headers={"location": "/"})) as client:
        loop = read_url("https://example.com/", client=client)
    assert loop.status == "error" and "redirects" in loop.detail


def test_a_redirect_is_followed_to_the_page():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://example.com/new"})
        return httpx.Response(200, text="<p>moved here</p>", headers={"content-type": "text/html"})

    with _client(handler) as client:
        result = read_url("https://example.com/old", client=client)
    assert result.status == "ok" and result.text == "moved here"


def test_a_failed_comment_request_keeps_the_post_as_a_partial_read():
    archive = _reddit_handler()

    def handler(request):
        if request.url.path == "/api/comments/tree":
            raise httpx.ReadTimeout("slow", request=request)
        return archive(request)

    with _client(handler) as client:
        result = read_url(THREAD, client=client)
    assert result.status == "ok"
    assert REDDIT_POST["data"][0]["title"] in result.text
    assert "comments: ReadTimeout" in result.partial


def test_a_site_dripping_its_answer_is_cut_at_the_whole_reads_deadline():
    now = [0.0]

    def drip():
        for _ in range(20):
            now[0] += 5.0  # each chunk arrives just inside httpx's own timeout
            yield b"<p>more</p>"

    with _client(lambda request: httpx.Response(200, content=drip())) as client:
        result = read_url("https://example.com/slow", client=client, clock=lambda: now[0])
    assert result.status == "error" and "over 20 s" in result.detail
    assert now[0] <= 25.0


def test_a_subreddit_reads_its_newest_posts_as_a_partial_read():
    with _client(lambda request: httpx.Response(200, json=REDDIT_POST)) as client:
        result = read_url("https://www.reddit.com/r/ClaudeCode/", client=client)
    assert "claude code sessions dashboard" in result.text
    assert result.partial == "only the subreddit's newest 100 posts"


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx.Response(429, text="Too Many Requests"), "rate_limited"),
        (
            httpx.Response(422, json={"data": None, "error": "Timeout. Maybe slow down a bit"}),
            "rate_limited",
        ),
        (httpx.Response(403, text="<title>Blocked</title>"), "blocked"),
        (
            httpx.Response(503, text="<html><title>Just a moment...</title>cf-chl-x</html>"),
            "blocked",
        ),
        (
            httpx.Response(
                200,
                text="<html><title>Reddit - Prove your humanity</title></html>",
                headers={"content-type": "text/html"},
            ),
            "blocked",
        ),
        (httpx.Response(500, text="oops"), "error"),
    ],
)
def test_a_refusing_site_is_reported_not_retried(response, status):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    with _client(handler) as client:
        result = read_url("https://example.com/page", client=client)
    assert result.status == status
    assert len(calls) == 1
    assert EXIT_CODES[status] == {"rate_limited": 3, "blocked": 2, "error": 1}[status]


def test_a_broken_feed_or_network_is_an_error_not_a_crash():
    def handler(request):
        raise httpx.ConnectError("down")

    with _client(handler) as client:
        assert read_url("https://example.com", client=client).status == "error"
    with _client(lambda request: httpx.Response(200, text="<not json")) as client:
        assert read_url(THREAD, client=client).status == "error"
    malformed = read_url("https://[bad")
    assert malformed.status == "error" and "malformed" in malformed.detail


def test_read_command_reports_a_malformed_url_as_an_error(capsys):
    assert cli._read(["https://[bad"]) == EXIT_CODES["error"]
    assert "the URL is malformed" in capsys.readouterr().err


def test_html_to_text_drops_scripts_and_keeps_paragraphs():
    text = html_to_text(
        "<html><head><title>t</title></head><body><script>var x=1</script>"
        "<p>One&nbsp;two</p><p>Three</p></body></html>"
    )
    assert text == "One two\n\nThree"


def test_research_runs_on_the_least_used_fresh_codex_account():
    usage = [
        {"account_key": "claude_code:main", "five_hour_pct": 0.0},
        {"account_key": "codex:a", "five_hour_pct": 70.0},
        {"account_key": "codex:b", "five_hour_pct": 10.0, "seven_day_pct": 40.0},
        {"account_key": "codex:c", "five_hour_pct": 1.0, "stale": True},
        {"account_key": "codex:d", "five_hour_pct": None},
        {"account_key": "codex:e", "five_hour_pct": 0.0, "seven_day_pct": 100.0},
    ]
    assert research_account(usage) == "codex:b"  # e has its week used up; b's fullest is 40
    usage[2]["seven_day_pct"] = 80.0
    assert research_account(usage) == "codex:a"
    assert research_account(usage[3:]) == "codex:c"
    assert research_account(usage[:1]) is None
    assert research_account([usage[5]]) is None


ANSWER = """People like the stuck-session detection.

Sources:
- https://www.reddit.com/r/ClaudeCode/comments/1xdqey0/ "caught my eye"
"""


def test_research_command_delegates_read_only_to_codex_and_prints_its_answer(monkeypatch, capsys):
    posted = []
    old_deck = False

    def handler(request):
        if request.url.path == "/api/usage":
            return httpx.Response(
                200, json={"usage": [{"account_key": "codex:codex", "five_hour_pct": 2.0}]}
            )
        if request.method == "POST":
            posted.append((request.url.path, json.loads(request.content)))
            if old_deck:
                # A web process that predates #4381 has no such route.
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(202, json={"id": "d1", "status_url": "/api/delegations/d1"})
        return httpx.Response(200, json={"state": "complete", "final_message": ANSWER})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(cli.httpx, "Client", lambda *a, **k: real_client(transport=transport))
    monkeypatch.setattr(
        cli.httpx, "get", lambda url, **k: real_client(transport=transport).get(url)
    )
    monkeypatch.setenv("AGENTDECK_SESSION_TOKEN", "tok")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("What do people say?"))

    assert cli._research(["--url", "http://deck.test"]) == 0
    path, body = posted[0]
    # Always web-only, on its own route (#4381); the deck picks the folder, sandbox
    # and approvals, so none of them is sent.
    assert path == "/api/delegations/web-only"
    assert body["account_key"] == "codex:codex"
    assert body["parent_session_token"] == "tok"
    assert not {"cwd", "sandbox", "approval_policy"} & body.keys()
    assert body["message"] == build_prompt("What do people say?")
    assert capsys.readouterr().out == ANSWER  # as Codex wrote it: nothing checked or marked

    # `--web-only` is accepted, so a script can refuse an agentdeck too old to lock.
    posted.clear()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("What do people say?"))
    # The caller's folder is never read, so a deleted one does not stop it.
    monkeypatch.setattr(cli.os, "getcwd", lambda: (_ for _ in ()).throw(FileNotFoundError()))
    assert cli._research(["--web-only", "--url", "http://deck.test"]) == 0
    assert [path for path, _ in posted] == ["/api/delegations/web-only"]

    # A deck too old to lock the chat refuses the route; nothing else is tried.
    old_deck = True
    posted.clear()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("What do people say?"))
    assert cli._research(["--url", "http://deck.test"]) == 1
    assert [path for path, _ in posted] == ["/api/delegations/web-only"]


def test_research_refuses_an_empty_question(monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("  \n"))
    with pytest.raises(SystemExit) as exc:
        cli._research([])
    assert exc.value.code == 2


def test_research_refuses_an_account_that_is_not_codex(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("what do people say?"))
    monkeypatch.setattr(cli, "_delegate", lambda argv: pytest.fail("delegated"))
    with pytest.raises(SystemExit) as exc:
        cli._research(["--account", "claude_code:main"])
    assert exc.value.code == 2 and "must be a Codex account" in capsys.readouterr().err
