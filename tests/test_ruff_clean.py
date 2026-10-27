"""Keep master `ruff check` clean (#3584).

There is no CI here, so nothing else runs the linter before a merge, and lint drift on
master had already broken three chats' own checks. `scripts/select_suite.py` selects this
file for every change that touches a `*.py` path.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _ruff() -> list[str] | None:
    """The ruff of the interpreter running the tests, else the one on PATH."""
    if importlib.util.find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    found = shutil.which("ruff")
    return [found] if found else None


def test_master_passes_ruff_check():
    ruff = _ruff()
    if ruff is None:
        pytest.skip("ruff is installed neither in this environment nor on PATH")
    # The repo's own `[tool.ruff]` in pyproject.toml applies: it runs from the root.
    result = subprocess.run(
        [*ruff, "check", "src", "tests", "scripts"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "`ruff check src tests scripts` must pass; fix these (most have `--fix`):\n"
        + result.stdout
        + result.stderr
    )
