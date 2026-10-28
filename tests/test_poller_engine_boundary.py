"""Enforce the Poller Engine's import boundary (#2597).

The engine runs from the checkout under its venv (#2153), so it may import the deck's shared
modules and third-party packages. It must never import ``agentdeck.web`` or ``agentdeck.app``,
directly or through another module: the app may import the engine, never the reverse. Loading
it must not load ``agentdeck.providers`` either, because every engine command pays the
engine's import time and that package alone costs over half a second.
"""

from __future__ import annotations

import dis
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import CodeType

import agentdeck.poller_engine as engine_pkg

_PACKAGE_DIR = Path(engine_pkg.__file__).parent
_FORBIDDEN = ("agentdeck.web", "agentdeck.app")
_TOO_HEAVY = ("agentdeck.providers",)
# The last part of each forbidden module: an import of one names it in ``co_names``.
_LEAVES = frozenset(root.rsplit(".", 1)[-1] for root in _FORBIDDEN)


def _engine_sources() -> list[Path]:
    return sorted(_PACKAGE_DIR.rglob("*.py"))


def _within(module: str, roots: tuple[str, ...]) -> bool:
    return any(module == root or module.startswith(root + ".") for root in roots)


def _absolute(module: str, level: int, source: Path) -> str:
    """The module an engine import names, with a relative one resolved."""
    if not level:
        return module
    package = ["agentdeck", *source.relative_to(_PACKAGE_DIR.parent).parent.parts]
    base = package[: len(package) - (level - 1)]
    return ".".join([*base, module] if module else base)


def _compiled(source: Path) -> CodeType:
    """The module's code as Python runs it, from its bytecode cache while that is current.

    Every import statement, at any depth, is an ``IMPORT_NAME`` in it, and reading the cache
    costs a fraction of parsing the engine's 1.4 MB of source again.
    """
    return SourceFileLoader(source.stem, str(source)).get_code(source.stem)


def _imports(code: CodeType):
    """``(module, level, fromlist)`` of every import in ``code`` and the code nested in it."""
    # An imported name, and each name taken from it, is in ``co_names``: a code object
    # naming no forbidden leaf cannot import one, so only the rest is disassembled.
    if any(part in _LEAVES for name in code.co_names for part in name.split(".")):
        instructions = list(dis.get_instructions(code))
        for index, instruction in enumerate(instructions):
            if instruction.opname == "IMPORT_NAME":
                level, fromlist = (load.argval for load in instructions[index - 2 : index])
                yield instruction.argval, level, fromlist or ()
    for const in code.co_consts:
        if isinstance(const, CodeType):
            yield from _imports(const)


def _violations(code: CodeType, source: Path) -> list[str]:
    found = []
    for module, level, fromlist in _imports(code):
        if level or fromlist:
            module = _absolute(module, level, source)
            names = [module, *(f"{module}.{name}" for name in fromlist)]
        else:
            names = [module]
        found += [f"{source.name}: {name}" for name in names if _within(name, _FORBIDDEN)]
    return found


def test_there_are_engine_sources_to_check():
    assert _engine_sources(), "no poller_engine sources found"


def test_no_engine_module_names_the_web_or_the_app():
    violations = [
        found for source in _engine_sources() for found in _violations(_compiled(source), source)
    ]
    assert not violations, "engine imports the app side:\n" + "\n".join(violations)


def test_the_source_check_catches_each_spelling():
    source = _PACKAGE_DIR / "cycle.py"
    for code in (
        "import agentdeck.web.render",
        "from agentdeck.app import create_app",
        "from agentdeck import web",
        "from .. import app",
        "from ..web.render import x",
        "def later():\n    import agentdeck.web",
    ):
        assert _violations(compile(code, str(source), "exec"), source), code
    for code in ("from ..models import IssueRef", "from . import leases", "import agentdeck.work"):
        assert not _violations(compile(code, str(source), "exec"), source), code


def test_loading_the_engine_loads_neither_the_app_nor_the_providers():
    modules = sorted(
        f"agentdeck.poller_engine.{path.stem}"
        for path in _engine_sources()
        if path.stem != "__init__"
    )
    probe = (
        "import importlib, json, sys\n"
        f"for name in {modules!r}: importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )
    loaded = json.loads(done.stdout)
    assert not [name for name in loaded if _within(name, _FORBIDDEN + _TOO_HEAVY)]


def test_the_suite_turns_off_the_engines_own_fsync_switch():
    # conftest spells the name out so pytest starts without importing the engine (#3052).
    from agentdeck.poller_engine.state import FSYNC_ENV

    conftest = (Path(__file__).parent / "conftest.py").read_text()
    assert f'os.environ.setdefault("{FSYNC_ENV}", "0")' in conftest
