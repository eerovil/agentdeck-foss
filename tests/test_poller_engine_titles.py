"""Recovering a real title for a card nobody named (#338 placeholders, #595 pastes)."""

from __future__ import annotations

from agentdeck.poller_engine.titles import MAX_TITLE_CHARS, repaired_title

PLACEHOLDERS = ("Requested changes",)

# The title of the issue that asked for this, exactly as it was filed: the request typed
# straight after the template's pre-filled title, with no body at all.
FILED = 'Requested changes Update github issue title automatically if its placeholder'


def test_what_was_typed_after_the_placeholder_becomes_the_title():
    assert repaired_title(FILED, "", PLACEHOLDERS) == (
        "Update github issue title automatically if its placeholder"
    )


def test_a_title_carrying_no_placeholder_is_left_alone():
    assert repaired_title("Fix the export encoding", "a body", PLACEHOLDERS) is None


def test_the_match_ignores_case_and_the_separator_someone_typed():
    assert repaired_title("requested changes: fix the export", "", PLACEHOLDERS) == (
        "fix the export"
    )
    assert repaired_title("Requested changes — fix the export", "", PLACEHOLDERS) == (
        "fix the export"
    )


def test_an_untouched_template_title_falls_back_to_the_body():
    body = "Fix the export encoding\n\nIt writes latin-1 where the importer wants utf-8."
    assert repaired_title("Requested changes", body, PLACEHOLDERS) == "Fix the export encoding"


def test_the_body_line_is_read_past_markdown_and_template_comments():
    body = "<!-- Describe the change\n     over two lines -->\n\n## Fix the export encoding\n"
    assert repaired_title("Requested changes", body, PLACEHOLDERS) == "Fix the export encoding"


def test_a_placeholder_with_nothing_to_recover_is_left_exactly_as_filed():
    """The placeholder is at least an honest "this was filed without a title"; a guess is not."""
    assert repaired_title("Requested changes", "", PLACEHOLDERS) is None
    boilerplate = "<!-- write the ask here -->\n\n---\n"
    assert repaired_title("Requested changes", boilerplate, PLACEHOLDERS) is None
    # A body that only repeats the placeholder recovers nothing either.
    assert repaired_title("Requested changes", "Requested changes", PLACEHOLDERS) is None


def test_a_project_that_names_no_placeholder_keeps_a_title_that_reads_as_one():
    assert repaired_title(FILED, "", ()) is None


# --- titles GitHub took from a paste (#595) ---
#
# Both of these are real, filed from a phone: agentdeck#592 dropped a screenshot in, and
# agentdeck#595 shared a session link. GitHub names the issue after the first line of what was
# pasted, so the card, the board and the deck all read the paste instead of the request.
PASTED_IMAGE = '<img width="1080" height="2414" alt="Image"…'
PASTED_IMAGE_BODY = (
    '<img width="1080" height="2414" alt="Image" '
    'src="https://github.com/user-attachments/assets/ea513aaf" />\n\n'
    "Seems the calibration will not work, there are no samples for 1 week"
)
PASTED_LINK = "https://myhost.tailnet0.ts.net/sessions/claude_code:main:869358b8-2d5f-4381…"
PASTED_LINK_BODY = f"{PASTED_LINK}\n\nSession failed to set a nice title"


def test_a_dropped_screenshot_gives_up_the_title_to_what_was_written_under_it():
    assert repaired_title(PASTED_IMAGE, PASTED_IMAGE_BODY, PLACEHOLDERS) == (
        "Seems the calibration will not work, there are no samples for 1 week"
    )


def test_a_shared_link_gives_up_the_title_to_what_was_written_under_it():
    assert repaired_title(PASTED_LINK, PASTED_LINK_BODY, PLACEHOLDERS) == (
        "Session failed to set a nice title"
    )


def test_a_paste_is_repaired_for_a_project_that_names_no_placeholder():
    """Recognising markup or a bare link needs nothing a project has to declare."""
    assert repaired_title(PASTED_LINK, PASTED_LINK_BODY, ()) == "Session failed to set a nice title"
    assert repaired_title(PASTED_IMAGE, PASTED_IMAGE_BODY) is not None


def test_a_markdown_image_is_the_same_failure_as_an_html_one():
    body = "![Screenshot](https://example.com/a.png)\n\nThe totals row is off by one"
    assert repaired_title("![Screenshot](https://example.com/a.png)", body) == (
        "The totals row is off by one"
    )


def test_a_link_inside_a_sentence_is_a_title_somebody_wrote():
    title = "https://example.com/build/9 is red again"
    assert repaired_title(title, "a body", PLACEHOLDERS) is None


def test_a_paste_with_nothing_written_under_it_is_left_exactly_as_filed():
    """Naming the card after the image's own URL would be no better than the tag."""
    assert repaired_title(PASTED_IMAGE, PASTED_IMAGE.replace("…", ' src="x.png" />'), ()) is None
    assert repaired_title(PASTED_LINK, PASTED_LINK, ()) is None


def test_repairing_a_paste_is_idempotent():
    once = repaired_title(PASTED_LINK, PASTED_LINK_BODY, PLACEHOLDERS)
    assert once is not None
    assert repaired_title(once, PASTED_LINK_BODY, PLACEHOLDERS) is None


def test_a_long_title_is_cut_at_a_word_boundary():
    body = "Make the poller retitle every card whose title is still the placeholder the "
    body += "issue template pre-filled, wherever it was filed from"
    repaired = repaired_title("Requested changes", body, PLACEHOLDERS)
    assert repaired is not None
    assert len(repaired) <= MAX_TITLE_CHARS
    assert repaired.endswith("…")
    assert repaired.startswith("Make the poller retitle every card")
    assert " …" not in repaired  # cut at the boundary, not left with a dangling space


def test_the_repair_is_idempotent_because_what_it_writes_is_never_a_placeholder():
    once = repaired_title(FILED, "", PLACEHOLDERS)
    assert once is not None
    assert repaired_title(once, "", PLACEHOLDERS) is None
