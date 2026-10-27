"""Skips for tests that cannot run in the public copy (#3623).

``scripts/export_public.py`` builds the public copy of this repository: it leaves this host's
own scripts and some guides out, and puts a public stand-in (``public/stubs/``) in place of
each private module. A test marked with one of these runs here and skips there.

Kept out of ``conftest.py`` on purpose: the test selector reads every name conftest mentions
as something each test uses, and these name modules only the marked tests need.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def shipped(*paths: str) -> pytest.MarkDecorator:
    """Skip a test that reads a file the public copy leaves out (#3623).

    The public copy (``scripts/export_public.py``) drops this host's own scripts and the
    guides that name private things. A test that reads one of them still runs here, and
    skips there rather than failing on a file that was never meant to ship.
    """
    root = Path(__file__).resolve().parents[1]
    missing = [path for path in paths if not (root / path).exists()]
    return pytest.mark.skipif(bool(missing), reason=f"not in the public copy: {', '.join(missing)}")


def private_feature(module) -> pytest.MarkDecorator:
    """Skip a test of a feature the public copy replaces with a stand-in (#3623).

    ``public/stubs/`` holds the public stand-in for each private module (ChatGPT's review
    chats, the Mac tool); a stand-in sets ``PUBLIC_STUB``. Pass the module itself, imported
    by the test file, so the test selector sees which tests depend on it: a test of the real
    behaviour runs here and skips in the public copy.
    """
    reason = f"{module.__name__} is a stand-in in the public copy"
    return pytest.mark.skipif(getattr(module, "PUBLIC_STUB", False), reason=reason)
