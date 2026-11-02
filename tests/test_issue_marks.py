"""Another deck's Work's spec and review verdict, read from the webhook log (#3855).

Deliveries go through `WebhookLog` and the service's own tailer, as on the live path.
"""

from __future__ import annotations

import json

from test_issue_latest_comment import AGENTDECK, deliver

from agentdeck.github_cache import GitHubMetadataCache
from agentdeck.issue_marks import IssueMarksService, classify, issue_marks
from agentdeck.webhook_tailer import WebhookTailer

HEAD = "e4d1e594f3cebd0e345fb3bb3929e14eff73d987"
# Real bodies, as #3834's and #3852's comments were posted.
FINDINGS = (
    f"/claude\nReview findings for PR #3837 at {HEAD}:\n\n1. `x.py:1` — a bug.\n\n"
    "<!-- chatgpt-review -->"
)
PASS = f"/reviewed pass {HEAD} {'0' * 64} agentdeck#3853"
SPEC = "## Spec: a thing\n\nNo open questions.\n\n<!-- agentdeck-spec -->"


def body(comment_id, text, *, number=3, action="created", pull_request=False) -> bytes:
    issue: dict = {"number": number, "title": "Marks", "state": "open"}
    if pull_request:
        issue["pull_request"] = {"url": "https://api.github.com/x"}
    return json.dumps(
        {
            "action": action,
            "issue": issue,
            "comment": {
                "id": comment_id,
                "body": text,
                "created_at": "2026-10-29T08:00:00Z",
                "html_url": f"https://github.com/eerovil/agentdeck/issues/{number}"
                f"#issuecomment-{comment_id}",
            },
            "repository": {
                "full_name": AGENTDECK.slug,
                "name": AGENTDECK.name,
                "owner": {"login": AGENTDECK.owner},
            },
        }
    ).encode()


def test_classify_reads_the_spec_the_findings_and_the_pass():
    assert classify(SPEC) == {"spec": ""}
    assert classify(FINDINGS) == {HEAD: "findings"}
    assert classify(PASS) == {HEAD: "passed"}
    assert classify(f"/reviewed changes-needed {HEAD[:7]}") == {HEAD[:7]: "findings"}
    # A person's own instruction, the poller's quote of a review, and plain talk are none.
    assert classify("/claude please fix the tests") == {}
    assert classify(FINDINGS + "\n<!-- claude-bot -->") == {}
    assert classify("Looks good to me") == {} and classify(None) == {}


def test_each_head_keeps_its_newest_verdict_whatever_the_order(tmp_path):
    root, cache = tmp_path / "hooks", GitHubMetadataCache(tmp_path / "cache.sqlite3")
    service = IssueMarksService(root, cache=cache)
    other = "b" * 40
    for delivery in (
        body(20, PASS),
        body(10, FINDINGS),
        body(30, SPEC),
        body(5, SPEC),
        body(25, f"/reviewed changes-needed {other}"),  # another pull request's head
        body(40, "/claude hi", pull_request=True),
    ):
        deliver(root, delivery)
    tailer = WebhookTailer(root, consumer="test-marks")
    service.apply_batch(tailer.read())
    tailer.commit()
    marks = issue_marks("eerovil/agentdeck#3", cache)
    # The older findings on HEAD lose to its pass; the other head keeps its own findings.
    assert (marks.verdict(HEAD).id, marks.verdict(HEAD).kind) == (20, "passed")
    assert (marks.verdict(other).id, marks.verdict(other).kind) == (25, "findings")
    assert marks.verdict("f" * 40) is None and marks.spec.id == 30
    assert marks.spec.url.endswith("#issuecomment-30") and marks.spec.at.startswith("2026")
    # Edited out of its kind, or deleted: the mark goes, and the older one is not brought
    # back; another issue is untouched.
    deliver(root, body(30, "No spec after all", action="edited"))
    deliver(root, body(20, PASS, action="deleted"))
    service.apply_batch(tailer.read())
    tailer.commit()
    marks = issue_marks("eerovil/agentdeck#3", cache)
    assert marks.spec is None and marks.verdict(HEAD) is None and marks.verdict(other).id == 25
    assert issue_marks("eerovil/agentdeck#4", cache).verdicts == ()


def test_a_running_service_says_look_again_only_when_a_work_may_read_differently(
    tmp_path, monkeypatch
):
    """#3855 review: an idle open front page hears of a new verdict without a reload."""
    import asyncio

    from agentdeck import webhook_tailer

    monkeypatch.setattr(webhook_tailer, "_WATCH_DEBOUNCE_MS", 20)
    monkeypatch.setattr(webhook_tailer, "_WATCH_STEP_MS", 10)
    root, cache = tmp_path / "hooks", GitHubMetadataCache(tmp_path / "cache.sqlite3")
    looks: list[int] = []
    service = IssueMarksService(root, cache=cache, on_change=lambda: looks.append(1))
    # A plain comment on an issue with no spec changes nothing anyone reads.
    assert service.apply_batch([]) is False

    async def run() -> None:
        service.start()
        deliver(root, body(10, "just talk"))
        await asyncio.sleep(0.15)
        assert looks == []
        deliver(root, body(11, PASS))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if looks:
                break
        await service.stop()

    asyncio.run(run())
    assert looks == [1] and issue_marks("eerovil/agentdeck#3", cache).verdict(HEAD).id == 11
    # Once a spec is held, any later comment may end its wait: that is a look too.
    tailer = WebhookTailer(tmp_path / "hooks2", consumer="t")
    deliver(tmp_path / "hooks2", body(20, SPEC))
    deliver(tmp_path / "hooks2", body(21, "a question about the spec"))
    first, second = tailer.read()
    assert service.apply(first) is True and service.apply(second) is True
    assert service.apply(first) is True  # a replay with a spec held still says look
