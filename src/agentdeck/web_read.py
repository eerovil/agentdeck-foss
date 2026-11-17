"""Read a web page's text for an agent, through the site's own API where it has one (#4290).

Claude's own fetch cannot open Reddit, a plain request from this host gets a 403 from it, and
Reddit ends its RSS feeds on 2026-11-13. So a page an agent was pointed at (by Codex's web
search, usually) is read here the way each site, or an open archive of it, still serves it to
a script: Reddit through the free Arctic Shift archive API, and anything else with one plain
GET.

One rule covers every reader: a read that knows it missed part of the discussion (a page the
site would not serve, comments the archive has not listed, the size cap, or the whole read's
:data:`TIMEOUT_S` running out) says so in :attr:`ReadResult.partial`, so the agent knows the
text is not the whole page. Nothing retries: a rate-limited site is reported
as such, because a retry only lengthens the block.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx

ReadStatus = Literal["ok", "blocked", "rate_limited", "error"]

MAX_CHARS = 200_000
# The limit on one whole read, however many requests it makes.
TIMEOUT_S = 20.0
# A browser's User-Agent: some sites answer a library's default one with a block page.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/157.0 Safari/537.36"
)
# What a script names itself to an API that asks scripts to.
SCRIPT_USER_AGENT = "agentdeck-web-read/1.0 (personal research tool)"

REDDIT_ARCHIVE = "https://arctic-shift.photon-reddit.com/api"
# The archive's own cap on one comment tree is far above any thread an agent quotes. Its tree
# also collapses past 4 siblings or 4 levels by default, so both are opened as wide.
REDDIT_TREE_LIMIT = 10_000
# The archive is a copy taken when a post or comment appears and refreshed once about 36 hours
# later, so nothing it holds proves a live thread has no later comments: every thread read
# from it is partial, and a quote missing from it is "not checked", never NOT FOUND.
REDDIT_SNAPSHOT = "an archive copy, so comments added since it was taken may be missing"
# Redirect hops one read follows, as a browser would stop a loop.
MAX_REDIRECTS = 10
# Decoded bytes kept from one response: a huge or decompression-inflated page stops here.
MAX_BYTES = 20_000_000
# What Cloudflare's and Reddit's bot checks put in an otherwise successful-looking page.
_CHALLENGE_MARKERS = (
    "<title>just a moment...</title>",
    "cf-chl-",
    "prove your humanity",
    "<title>blocked</title>",
)


@dataclass(frozen=True)
class ReadResult:
    url: str
    status: ReadStatus
    source: str
    text: str = ""
    detail: str = ""
    # What ``text`` is missing, when the reader knows it is not the whole discussion. A quote
    # missing from a partial read is unchecked, never "not found".
    partial: str = ""


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    _BLOCK = {
        "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
        "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "header", "footer",
    }  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._parts).splitlines())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup degrades to a crude strip
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())
    return parser.text()


def _is_reddit(host: str) -> bool:
    """reddit.com, every subdomain of it (old., new., np., m., sh., …) and redd.it."""
    return host in {"reddit.com", "redd.it"} or host.endswith(".reddit.com")


def reddit_thread_id(host: str, path: str) -> str | None:
    """The post id a Reddit link names, in any of its shapes, or ``None``.

    ``/comments/<id>`` under a subreddit, a user or nothing (comment permalinks included),
    ``/gallery/<id>``, and ``redd.it/<id>`` all name one thread. A ``/r/<sub>/s/<token>`` share
    link names none until Reddit's redirect is followed.
    """
    if host == "redd.it":
        match = re.match(r"^/([a-z0-9]+)/?$", path, re.IGNORECASE)
    else:
        match = re.search(r"/comments/([a-z0-9]+)", path, re.IGNORECASE) or re.match(
            r"^/gallery/([a-z0-9]+)", path, re.IGNORECASE
        )
    return match.group(1).lower() if match else None


def plan(url: str) -> tuple[str, str]:
    """``(source, fetch_url)`` for ``url``: which reader handles it and what it fetches first."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path or "/"
    if _is_reddit(host):
        thread = reddit_thread_id(host, path)
        if thread:
            return "reddit-archive", f"{REDDIT_ARCHIVE}/posts/ids?ids={thread}"
        if re.match(r"^/r/[A-Za-z0-9_]+/s/[A-Za-z0-9]+/?$", path):
            return "reddit-share", url.strip()
        subreddit = re.match(r"^/r/([A-Za-z0-9_]+)/?(?:\.rss|\.json)?$", path)
        if subreddit:
            return (
                "reddit-archive",
                f"{REDDIT_ARCHIVE}/posts/search?subreddit={subreddit.group(1)}&limit=100&sort=desc",
            )
    return "page", url.strip()


def _challenge(body: str) -> bool:
    head = body[:20_000].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def _refusal(url: str, source: str, response: httpx.Response) -> ReadResult | None:
    code = response.status_code
    # The Reddit archive answers a burst with 422 "Timeout. Maybe slow down a bit".
    if code == 429 or (code == 422 and "slow down" in response.text[:500].lower()):
        return ReadResult(
            url, "rate_limited", source, detail=f"HTTP {code}: the site rate-limited this host"
        )
    if code in (401, 403) or (code == 503 and _challenge(response.text)):
        return ReadResult(url, "blocked", source, detail=f"HTTP {code}: the site refused this host")
    if code >= 400:
        return ReadResult(url, "error", source, detail=f"HTTP {code}")
    return None


class _Read:
    """One read's requests, all inside one deadline, and the parts it could not get."""

    def __init__(
        self,
        client: httpx.Client,
        agent: str,
        clock: Callable[[], float],
    ) -> None:
        self.client = client
        self.agent = agent
        self.clock = clock
        self.deadline = clock() + TIMEOUT_S
        self.missing: list[str] = []

    def get(
        self, url: str, params: dict | None = None, *, follow_redirects: bool = True
    ) -> httpx.Response | None:
        """The response, or ``None`` once the read's time is used up.

        One deadline covers the whole read. Redirects are followed here one hop at a time,
        each hop given only the time left, because httpx's own redirect following and timeout
        would give every hop and every chunk its own full wait. The body is streamed and the
        deadline checked after every chunk. A body past :data:`MAX_BYTES` is cut there and the
        cut recorded as missing.
        """
        for _ in range(MAX_REDIRECTS + 1):
            response = self._hop(url, params)
            if response is None or not (follow_redirects and response.has_redirect_location):
                return response
            url, params = urljoin(str(response.request.url), response.headers["location"]), None
        raise httpx.TooManyRedirects(f"over {MAX_REDIRECTS} redirects", request=response.request)

    def _hop(self, url: str, params: dict | None) -> httpx.Response | None:
        left = self.deadline - self.clock()
        if left <= 0:
            return None
        with self.client.stream(
            "GET",
            url,
            params=params,
            headers={"User-Agent": self.agent},
            timeout=min(TIMEOUT_S, left),
            follow_redirects=False,
        ) as response:
            if self.clock() > self.deadline:
                return None
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                chunks.append(chunk[: MAX_BYTES - size])
                size += len(chunk)
                if size > MAX_BYTES:
                    self.missing.append(f"cut at {MAX_BYTES} bytes")
                    break
                if self.clock() > self.deadline:
                    return None
            # The chunks are already decoded, so the copy must not decode them again.
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
            }
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=b"".join(chunks),
                request=response.request,
            )

    def fetched(self, what: str, response: httpx.Response | None) -> dict | None:
        """``response``'s JSON, or ``None`` with ``what`` recorded as missing.

        A body cut at :data:`MAX_BYTES` is not whole JSON, so it is missing too rather than an
        error that would lose what the read already has.
        """
        if response is None:
            self.missing.append(f"{what}: out of time after {TIMEOUT_S:.0f} s")
            return None
        if response.status_code != 200:
            self.missing.append(f"{what}: HTTP {response.status_code}")
            return None
        try:
            return response.json()
        except ValueError:
            self.missing.append(f"{what}: the answer was cut or not valid JSON")
            return None


def _reddit_thread(read: _Read, post: dict) -> str:
    """The post and its whole comment tree from the archive."""
    blocks = [
        f"# {html.unescape(post.get('title') or '')} — u/{post.get('author') or ''}",
        html.unescape(post.get("selftext") or ""),
    ]
    try:
        tree_response = read.get(
            f"{REDDIT_ARCHIVE}/comments/tree",
            {
                "link_id": post.get("id"),
                "limit": REDDIT_TREE_LIMIT,
                "start_breadth": REDDIT_TREE_LIMIT,
                "start_depth": REDDIT_TREE_LIMIT,
            },
        )
    except httpx.HTTPError as exc:
        # The post is already read: a failed comment request leaves the read partial.
        read.missing.append(f"comments: {type(exc).__name__}: {exc}")
        return "\n\n".join(blocks)
    tree = read.fetched("comments", tree_response)
    if tree is None:
        return "\n\n".join(blocks)
    listed = unlisted = 0
    pending = list(tree.get("data") or [])
    while pending:
        node = pending.pop(0)
        data = node.get("data") or {}
        if node.get("kind") == "more":
            unlisted += int(data.get("count") or len(data.get("children") or []) or 1)
            continue
        listed += 1
        blocks.append(f"## comment by u/{data.get('author') or ''}")
        blocks.append(html.unescape(data.get("body") or ""))
        replies = data.get("replies")
        if isinstance(replies, dict):
            pending.extend((replies.get("data") or {}).get("children") or [])
    read.missing.append(REDDIT_SNAPSHOT)
    if unlisted:
        read.missing.append(f"{unlisted} comments the archive did not list")
    return "\n\n".join(block for block in blocks if block)


def _reddit_text(read: _Read, fetch_url: str, body: dict) -> str | None:
    posts = body.get("data") or []
    if "/posts/ids" in fetch_url:
        return _reddit_thread(read, posts[0]) if posts else None
    read.missing.append("only the subreddit's newest 100 posts")
    return "\n\n".join(
        f"## {html.unescape(post.get('title') or '')} — u/{post.get('author') or ''}\n"
        f"{html.unescape(post.get('selftext') or '')}".strip()
        for post in posts
    )


def _follow_share(read: _Read, url: str) -> str | ReadResult:
    """The thread a Reddit share link names, as ``/comments/<id>/``, or why it names none.

    Reddit answers this host's share links with a redirect to the thread, not a 403.
    """
    hop = read.get(url.strip(), follow_redirects=False)
    if hop is None:
        return ReadResult(
            url, "error", "reddit-share", detail=f"the site took over {TIMEOUT_S:.0f} s to answer"
        )
    target = urlsplit(urljoin(url.strip(), hop.headers.get("location", "")))
    host = (target.hostname or "").lower().removeprefix("www.")
    thread = reddit_thread_id(host, target.path) if _is_reddit(host) else None
    if not hop.is_redirect or thread is None:
        refused = _refusal(url, "reddit-share", hop)
        return refused or ReadResult(
            url, "error", "reddit-share", detail="the share link did not lead to a Reddit thread"
        )
    return f"https://www.reddit.com/comments/{thread}/"


def read_url(
    url: str,
    *,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ReadResult:
    """Read ``url``'s text once, through the reader that suits its site."""
    try:
        source, fetch_url = plan(url)
    except ValueError as exc:
        # urlsplit refuses some malformed URLs, such as an unclosed ``[`` host.
        return ReadResult(url, "error", "page", detail=f"the URL is malformed: {exc}")
    agent = SCRIPT_USER_AGENT if source.startswith("reddit") else BROWSER_USER_AGENT
    owned = client is None
    http = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)
    read = _Read(http, agent, clock)
    try:
        if source == "reddit-share":
            thread_url = _follow_share(read, url)
            if isinstance(thread_url, ReadResult):
                return thread_url
            source, fetch_url = plan(thread_url)
        response = read.get(fetch_url)
        if response is None:
            return ReadResult(
                url, "error", source, detail=f"the site took over {TIMEOUT_S:.0f} s to answer"
            )
        refused = _refusal(url, source, response)
        if refused is not None:
            return refused
        if source == "reddit-archive":
            text = _reddit_text(read, fetch_url, response.json())
            if text is None:
                return ReadResult(url, "error", source, detail="the archive has no such thread")
        else:
            if _challenge(response.text):
                return ReadResult(
                    url, "blocked", source, detail="the site answered with a bot check"
                )
            kind = response.headers.get("content-type", "")
            text = html_to_text(response.text) if "html" in kind or not kind else response.text
    except (httpx.HTTPError, ValueError, KeyError, IndexError, AttributeError) as exc:
        return ReadResult(url, "error", source, detail=f"{type(exc).__name__}: {exc}")
    finally:
        if owned:
            http.close()
    if len(text) > MAX_CHARS:
        read.missing.append(f"cut at {MAX_CHARS} characters")
    return ReadResult(url, "ok", source, text=text[:MAX_CHARS], partial="; ".join(read.missing))


EXIT_CODES: dict[ReadStatus, int] = {"ok": 0, "error": 1, "blocked": 2, "rate_limited": 3}
