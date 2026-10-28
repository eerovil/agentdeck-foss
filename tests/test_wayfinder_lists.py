"""The deck keeps a wayfinder map's lists from its tickets' answers (#3695)."""

from __future__ import annotations

import gzip
import json

from agentdeck import wayfinder_lists as lists
from agentdeck.github_webhook_log import Repository, build_record

URL = "https://github.com/eerovil/agentdeck/issues/"

BODY = """Make wayfinder great, in the person's words.

## Destination

Wayfinder is great to use.

## Notes

- Domain: the deck. Not a list the deck keeps.

## Decisions so far

- [Old ticket](https://github.com/eerovil/agentdeck/issues/10) — a gist a person wrote by hand,
  wrapped onto a second line

## Not yet specified

- Charted fog nobody recorded
- Fog the answer makes sharp

## Ideas

- Not doing it at all (owner)

## Out of scope
"""


def answer(number, body, title=None):
    return lists.parse(
        body,
        issue=("eerovil/agentdeck", number),
        title=title or f"Ticket {number}",
        url=f"{URL}{number}",
    )


def test_an_answer_moves_the_lists_and_leaves_the_rest_byte_for_byte():
    records = [
        answer(
            11,
            "**Answer:** Keep it small. The rest of the answer is not on the map.\n\n"
            "Graduated: Fog the answer makes sharp\n"
            "Fog: New fog\n"
            "Idea: Ask the phone (a past map)\n"
            f"Revises: [Old ticket]({URL}10) — its assumed limit\n",
        ),
        answer(
            12, f"**Answer:** Past the destination.\n\nOut of scope: [Ticket 12]({URL}12) — why"
        ),
        lists.parse(
            "**Ideas sorted:**\n\nOut of scope: Not doing it at all — not picked\n",
            issue=("eerovil/agentdeck", 1),
            title="Map",
            url=f"{URL}1",
        ),
    ]
    rebuilt = lists.rebuild(BODY, records)
    head, _, _ = BODY.partition("## Decisions so far")
    assert rebuilt.startswith(head)  # the person's words, Destination and Notes stay
    assert lists._lists(rebuilt.split("\n"))[lists.DECISIONS].items == [
        f"[Old ticket]({URL}10) — a gist a person wrote by hand, wrapped onto a second line"
        f" (revised by [Ticket 11]({URL}11))",
        f"[Ticket 11]({URL}11) — Keep it small",
    ]
    assert "\n- Charted fog nobody recorded\n- New fog\n" in rebuilt
    assert "Fog the answer makes sharp" not in rebuilt
    assert "\n## Ideas\n\n- Ask the phone (a past map)\n" in rebuilt
    assert rebuilt.endswith(
        f"## Out of scope\n\n- [Ticket 12]({URL}12) — why\n- Not doing it at all — not picked\n"
    )
    # Applied again, the same records change nothing: no write.
    assert lists.rebuild(rebuilt, records) == rebuilt


def test_nothing_new_leaves_the_body_alone_even_where_its_shape_differs():
    """A list whose items did not change keeps its own bullets and spacing."""
    body = BODY.replace("- Charted fog", "* Charted fog")
    assert lists.rebuild(body, []) == body
    record = answer(10, "**Answer:** A new gist that must not replace the hand-written one.")
    assert lists.rebuild(body, [record]) == body
    # A body with no such heading gets it, at its end.
    rebuilt = lists.rebuild("## Destination\n\nX\n", [record])
    assert rebuilt.endswith(f"## Decisions so far\n\n- [Ticket 10]({URL}10) — {record.gist}\n")


def test_the_gist_is_the_answers_first_sentence():
    assert lists.gist(" No — keep it. Because `x.y` said so.") == "No — keep it"
    assert lists.gist("Use v1.2 here\nand there.\n\nSecond paragraph.") == "Use v1.2 here and there"
    long = lists.gist("word " * 100)
    assert long.endswith("…") and len(long) <= lists.MAX_GIST
    assert lists.record_kind("**Answer:** x") == lists.ANSWER
    assert lists.record_kind("> **Answer:** quoted") == ""


def test_scan_reads_links_and_each_records_newest_copy(tmp_path):
    root = tmp_path / "github-webhooks"
    # Filed under the repository's own case, as ``WebhookLog`` files it.
    repo = Repository(owner="EeroVil", name="AgentDeck")

    lines = []

    def put(event, **payload):
        body = json.dumps({"repository": {"full_name": repo.slug}, **payload}).encode()
        headers = {"X-GitHub-Delivery": f"d{len(lines)}", "X-GitHub-Event": event}
        lines.append(json.dumps(build_record(body=body, headers=headers, repository=repo)) + "\n")

    def comment(action, cid, body, stamp, number=11):
        put(
            "issue_comment",
            action=action,
            issue={"number": number, "title": "T"},
            comment={"id": cid, "body": body, "updated_at": stamp, "created_at": stamp},
        )

    put(
        "sub_issues", action="sub_issue_added", parent_issue={"number": 1}, sub_issue={"number": 11}
    )
    put(
        "sub_issues", action="sub_issue_added", parent_issue={"number": 1}, sub_issue={"number": 12}
    )
    put(
        "sub_issues",
        action="sub_issue_removed",
        parent_issue={"number": 1},
        sub_issue={"number": 12},
    )
    comment("created", 5, "**Answer:** first", "2026-10-27T10:00:00Z")
    comment("edited", 5, "**Answer:** second", "2026-10-27T10:05:00Z")
    comment("edited", 5, "**Answer:** late copy", "2026-10-27T10:01:00Z")
    comment("created", 6, "**Answer:** gone", "2026-10-27T10:00:00Z")
    comment("deleted", 6, "**Answer:** gone", "2026-10-27T10:00:00Z")
    comment("created", 7, "just talk", "2026-10-27T10:00:00Z")
    # An archived stream is read first, so the live file's newer copy wins.
    live = root / "EeroVil" / "AgentDeck.jsonl"
    live.parent.mkdir(parents=True)
    live.write_text("".join(lines))
    with gzip.open(live.with_name("AgentDeck.jsonl.1.gz"), "wt") as handle:
        handle.write(lines[3])
    facts = lists.scan(root, "EeroVil/AgentDeck")
    assert facts.parents == {
        ("eerovil/agentdeck", 11): ("eerovil/agentdeck", 1),
        ("eerovil/agentdeck", 12): None,
    }
    assert facts.comments[5]["body"] == "**Answer:** second"
    assert facts.comments[6] is None and 7 not in facts.comments
