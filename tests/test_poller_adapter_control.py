"""This dispatch's control command lives on disk, not in the brief (#1732, #1772)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentdeck.poller_adapter.control import (
    KEPT_PER_CARD,
    VERBS,
    write_build_brief,
    write_card_control,
)
from agentdeck.poller_adapter.runner import Workspace, compose_message

_DIRECTIVE = {
    "ref": "store#5",
    "repo": "store",
    "number": 5,
    "action": "new",
    "generation": "gen1",
    "dispatch_id": "d1",
    "messages": [{"payload": "fix the login bug"}],
}
_BASE = "/repo/scripts/poller_engine.py worker-control --manifest m --overlay o --binding b"


def test_the_wrapper_runs_the_engine_with_this_card_and_generation_already_in_it(tmp_path):
    log = tmp_path / "called"
    engine = tmp_path / "engine.sh"
    engine.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n', encoding="utf-8")
    engine.chmod(0o755)

    command = write_card_control(tmp_path / "card-control", _DIRECTIVE, str(engine))

    assert command
    subprocess.run([command, "finish", "--body-file", "/tmp/body.md"], check=True)
    assert log.read_text(encoding="utf-8").split("\n")[:6] == [
        "finish",
        "store",
        "5",
        "gen1",
        "--body-file",
        "/tmp/body.md",
    ]


def test_a_wrapper_with_no_verb_says_what_it_takes_instead_of_acting(tmp_path):
    command = write_card_control(tmp_path / "card-control", _DIRECTIVE, "/bin/false")

    done = subprocess.run([command], capture_output=True, text=True, check=False)

    assert done.returncode == 2
    # #2155: only the lifecycle verbs are offered, plus the one non-lifecycle offer a live
    # worker makes before it verifies (#2350), the take-back of its own `resolved` (#2522) and
    # a spec worker's take-over of the build (#2716); the retired ones no longer exist at all.
    assert "<finish|review|ready-for-review|reopen|start-implementation>" in done.stderr
    for verb in ("enabled", "comment", "links", "checkpoint"):
        assert verb not in done.stderr


def test_each_generation_gets_its_own_wrapper_so_a_stale_worker_keeps_its_own(tmp_path):
    """Sharing one wrapper per card would hand a superseded worker the live generation."""
    root = tmp_path / "card-control"

    first = write_card_control(root, _DIRECTIVE, _BASE)
    second = write_card_control(root, {**_DIRECTIVE, "generation": "gen2"}, _BASE)

    assert first != second
    assert Path(first).exists()
    assert "gen1" in Path(first).read_text(encoding="utf-8")
    assert "gen2" in Path(second).read_text(encoding="utf-8")


def test_a_card_keeps_its_recent_wrappers_and_drops_the_rest(tmp_path):
    root = tmp_path / "card-control"
    written = []
    for index in range(KEPT_PER_CARD + 3):
        path = Path(write_card_control(root, {**_DIRECTIVE, "generation": f"g{index}"}, _BASE))
        # Same-second writes would otherwise sort arbitrarily by mtime.
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
        written.append(path)

    assert written[-1].exists()
    assert len(list(root.iterdir())) == KEPT_PER_CARD
    assert not written[0].exists()


def test_an_unwritable_root_falls_back_to_the_long_form_rather_than_costing_a_dispatch(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")

    assert write_card_control(blocked / "card-control", _DIRECTIVE, _BASE) == ""


def test_a_directive_without_a_generation_gets_no_wrapper(tmp_path):
    assert write_card_control(tmp_path, {**_DIRECTIVE, "generation": ""}, _BASE) == ""


def test_the_brief_carries_the_card_control_command_instead_of_the_engine_invocation(tmp_path):
    command = write_card_control(tmp_path / "card-control", _DIRECTIVE, _BASE)

    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        control_command=_BASE,
        card_control=command,
    )

    assert f"    {command} finish in_review --body-file <path>\n" in message
    assert f"    {command} review pass --body-file <path>\n" in message
    for retired in ("enabled", "comment", "links"):
        assert f"{command} {retired}" not in message
    # The whole point: the engine invocation is not restated on any command line.
    assert _BASE not in message


@pytest.mark.parametrize(
    "directive",
    [
        _DIRECTIVE,
        {**_DIRECTIVE, "spec_only": True},
        {**_DIRECTIVE, "action": "merge-review", "messages": []},
        {**_DIRECTIVE, "continuation": True},
        {**_DIRECTIVE, "ready_for_review": True},
    ],
    ids=["work", "spec-only", "merge-review", "continuation", "ready-for-review"],
)
def test_every_command_a_brief_names_is_advertised_and_supported(directive):
    """#2155: the brief, the wrapper's verb list and the engine's parser agree.

    The same checkout builds all three since #2153, so this is a build invariant rather than a
    runtime probe: a verb the brief tells a worker to run must be one the wrapper advertises and
    the engine accepts, and the brief names no other.
    """
    import re

    from agentdeck.poller_engine.__main__ import build_parser

    parser = build_parser()
    worker = next(
        action.choices["worker-control"]
        for action in parser._subparsers._group_actions  # noqa: SLF001 -- argparse has no API
        if "worker-control" in (action.choices or {})
    )
    accepted = set(
        next(
            action.choices
            for action in worker._subparsers._group_actions  # noqa: SLF001
        )
    )

    message = compose_message(
        directive, Workspace(path=Path("/w/issue-5"), branch="b"), control_command=_BASE
    )
    named = set(re.findall(re.escape(_BASE) + r" (\S+) store 5 gen1", message))

    assert named, message
    assert named <= set(VERBS) <= accepted
    assert set(VERBS) == {
        "finish", "review", "ready-for-review", "reopen", "start-implementation",
    }


def test_the_brief_still_spells_the_command_out_when_no_wrapper_was_written():
    message = compose_message(
        _DIRECTIVE,
        Workspace(path=Path("/w/issue-5"), branch="b"),
        control_command=_BASE,
    )

    assert f"{_BASE} finish store 5 gen1 in_review --body-file" in message
    assert f"{_BASE} enabled" not in message


def test_the_wrapper_quotes_the_card_and_generation_it_bakes_in(tmp_path):
    """Host state, but nothing downstream keeps shell syntax out of a repo id or generation."""
    log = tmp_path / "called"
    engine = tmp_path / "engine.sh"
    engine.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n', encoding="utf-8")
    engine.chmod(0o755)
    hostile = {**_DIRECTIVE, "repo": "store; touch /tmp/pwned", "generation": "g 1"}

    command = write_card_control(tmp_path / "card-control", hostile, str(engine))

    subprocess.run([command, "enabled"], check=True)
    assert log.read_text(encoding="utf-8").split("\n")[:4] == [
        "enabled",
        "store; touch /tmp/pwned",
        "5",
        "g 1",
    ]


def _engine_that(script: str, tmp_path: Path) -> Path:
    engine = tmp_path / "engine.sh"
    engine.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    engine.chmod(0o755)
    return engine


@pytest.mark.parametrize("code", [1, 75, 78])
def test_every_refusal_is_final_and_costs_no_wait(tmp_path, code):
    """Nothing flips the engine under a worker any more (#2153), so no exit is worth a re-run.

    A stale generation is a stop, and a configuration refusal is the instance's to report.
    """
    counter = tmp_path / "attempts"
    counter.write_text("", encoding="utf-8")
    engine = _engine_that(f'printf "x" >> {counter}\nexit {code}\n', tmp_path)

    command = write_card_control(tmp_path / "card-control", _DIRECTIVE, str(engine))
    done = subprocess.run([command, "finish"], capture_output=True, text=True, check=False)

    assert done.returncode == code
    assert counter.read_text(encoding="utf-8") == "x"


def test_the_wrapper_forwards_reopen_and_its_body_file(tmp_path):
    log = tmp_path / "called"
    engine = tmp_path / "engine.sh"
    engine.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n', encoding="utf-8")
    engine.chmod(0o755)

    command = write_card_control(tmp_path / "card-control", _DIRECTIVE, str(engine))
    subprocess.run([command, "reopen", "--body-file", "/tmp/why.md"], check=True)

    assert log.read_text(encoding="utf-8").split("\n")[:6] == [
        "reopen", "store", "5", "gen1", "--body-file", "/tmp/why.md",
    ]


def test_every_work_brief_tells_a_worker_it_can_take_back_its_resolved(tmp_path):
    """A worker asked to carry on after `resolved` reopened the issue by hand, and the poller
    released the card anyway (#2522). Both brief shapes say what to run instead."""
    from agentdeck.poller_adapter.runner import Workspace, compose_message
    from agentdeck.poller_adapter.worker import REOPEN_ASK, _compact_poller_brief

    directive = {
        "ref": "agentdeck#2522",
        "repo": "agentdeck",
        "number": 2522,
        "generation": "g1",
        "messages": [{"payload": "Implement the requested change."}],
    }
    workspace = Workspace(path=tmp_path / "issue-2522", branch="agent/issue-2522")
    message = compose_message(directive, workspace)
    compact = _compact_poller_brief(
        directive, message, cwd=str(workspace.path), worktrees=[str(workspace.path)]
    )

    assert REOPEN_ASK in message
    assert compact.count(REOPEN_ASK) == 1
    flat = " ".join(REOPEN_ASK.split())
    assert "`reopen --body-file <path>`" in flat
    # Any finish, and a merged or closed card too: never a second question (#2845).
    assert "24 hours" not in flat
    assert "If you finished and" in flat
    assert "Do not ask the person a second time." in flat


def test_a_build_brief_lives_and_is_pruned_with_its_wrapper(tmp_path):
    """A spec worker's build brief (#2716) shares its wrapper's generation and its fate, and
    never costs a wrapper its place among the kept ones."""
    root = tmp_path / "card-control"
    written = []
    for index in range(KEPT_PER_CARD + 2):
        path = Path(write_card_control(root, {**_DIRECTIVE, "generation": f"g{index}"}, _BASE))
        brief = Path(write_build_brief(str(path), "build it"))
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
        os.utime(brief, (1_000_000 + index, 1_000_000 + index))
        written.append((path, brief))
    write_card_control(root, {**_DIRECTIVE, "generation": "g-last"}, _BASE)

    assert written[0][1].name == f"{written[0][0].name}.build.md"
    assert not written[0][0].exists() and not written[0][1].exists()
    kept = [path for path, _brief in written if path.exists()]
    assert len(kept) == KEPT_PER_CARD - 1
    assert all(Path(f"{path}.build.md").exists() for path in kept)
    assert write_build_brief("", "x") == ""
    assert write_build_brief(str(tmp_path / "missing" / "w"), "x") == ""


def test_the_wrapper_forwards_start_implementation(tmp_path):
    log = tmp_path / "called"
    engine = tmp_path / "engine.sh"
    engine.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n', encoding="utf-8")
    engine.chmod(0o755)

    command = write_card_control(tmp_path / "card-control", _DIRECTIVE, str(engine))
    subprocess.run([command, "start-implementation", "--body-file", "/tmp/ok.md"], check=True)

    assert log.read_text(encoding="utf-8").split("\n")[:6] == [
        "start-implementation", "store", "5", "gen1", "--body-file", "/tmp/ok.md",
    ]
