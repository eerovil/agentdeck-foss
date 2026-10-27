"""Every documented AgentDeck CLI command must name the binary by path (#2015).

A bare `agentdeck` is not on PATH in an owned worker shell, so a worker that follows
the prose literally burns a failed command and a hunt for the binary before it gets
anywhere.  Six independent sessions did exactly that.  These tests pin the three places
that hand a worker a command to run.
"""

import re
from pathlib import Path

from public_copy import shipped

from agentdeck.poller_adapter.runner import VERIFICATION_EVIDENCE_BRIEF
from agentdeck.providers.instructions import agentdeck_session_instructions

ROOT = Path(__file__).resolve().parents[1]
VENV_CLI = "~/agentdeck/.venv/bin/agentdeck"


def test_the_verification_brief_spells_out_the_job_submit_binary():
    """#2154 replaced the verify-phase packet with one brief section; the rule carries over."""
    brief = VERIFICATION_EVIDENCE_BRIEF

    assert f"{VENV_CLI} job submit --verification " in brief
    # The bare form is what sent workers hunting; it must not survive anywhere in the brief.
    assert "`agentdeck job submit" not in brief
    assert "\nagentdeck job submit" not in brief
    assert " agentdeck job submit" not in brief


def test_the_primary_session_guide_spells_out_the_delegate_binary():
    guide = agentdeck_session_instructions(provider="claude_code", role="primary")

    assert f"{VENV_CLI} delegate --account <provider:label>" in guide
    assert "the installed `agentdeck delegate" not in guide


@shipped("AGENTS.md")
def test_the_root_guide_spells_out_the_job_queue_commands_and_says_why():
    guide = (ROOT / "AGENTS.md").read_text()
    flat = " ".join(guide.split())  # the guide is hard-wrapped; match prose on one line

    assert f"{VENV_CLI} job submit --cwd <dir> -- pytest -q" in flat
    assert f"{VENV_CLI} job list|status|logs|cancel" in flat
    assert f"{VENV_CLI} job list`" in flat
    # Stating the rule once is what stops the next command added here from regressing.
    assert "must not assume a bare `agentdeck` is on PATH" in flat
    assert "`agentdeck job submit" not in flat
    assert "`agentdeck job list" not in flat


def _cli_subcommands() -> set[str]:
    """Every verb `agentdeck` dispatches, read out of the CLI itself.

    Taking the list from the code rather than restating it here is what makes the guard
    below cover a subcommand nobody has written yet.
    """
    main = (ROOT / "src" / "agentdeck" / "__main__.py").read_text()
    found = set(re.findall(r'sys\.argv\[1\] == "([a-z][a-z-]*)"', main))
    assert {"job", "delegate", "nudge-on-restart"} <= found, found
    return found


# The one span allowed to stay bare names the `job` subsystem in the component map
# ("before changing `agentdeck job`"); it tells nobody to run anything. Anything longer
# is a command with arguments, and `agentdeck <other-verb>` on its own is the shape that
# fooled the first pass, so both still fail the guard.
SUBSYSTEM_REFERENCES = {"agentdeck job"}


def _bare_invocations(text: str) -> list[str]:
    """Backticked `agentdeck <verb> …` spans in `text` that do not name the binary by path.

    A path-spelled command starts `~/agentdeck/...`, so the leading backtick is not
    followed by `agentdeck` and never matches here.
    """
    subcommands = _cli_subcommands()
    spans = re.findall(r"`(agentdeck +[^`]*)`", text)
    return [
        span
        for span in spans
        if len(span.split()) > 1
        and span.split()[1] in subcommands
        and span not in SUBSYSTEM_REFERENCES
    ]


@shipped("AGENTS.md")
def test_the_root_guide_never_hands_a_worker_a_bare_cli_command():
    """The rule, not just the three sites #2015 started from.

    `nudge-on-restart` survived the first pass because it reads like a label — it takes
    no arguments — and only `__main__.py` shows it is a real command.  So the guard asks
    the CLI what its verbs are instead of trusting how the prose looks.
    """
    offenders = _bare_invocations((ROOT / "AGENTS.md").read_text())

    assert not offenders, f"spell these by path: {offenders}"


def test_the_bare_guard_would_catch_a_newly_added_command():
    """The guard is worth nothing if its pattern silently matches nothing."""
    assert _bare_invocations(
        "Register with `agentdeck nudge-on-restart` before ending the turn."
    ) == ["agentdeck nudge-on-restart"]
    assert _bare_invocations("`agentdeck job submit --cwd . -- pytest -q`") == [
        "agentdeck job submit --cwd . -- pytest -q"
    ]
    # A path-spelled command and a bare non-verb are both fine.
    assert _bare_invocations(f"`{VENV_CLI} job submit --cwd . -- pytest -q`") == []
    assert _bare_invocations("`agentdeck maybe-someday --help`") == []
