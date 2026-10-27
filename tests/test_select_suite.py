"""Guard the test selector, the import graph it reads, and the marker it subtracts.

`scripts/select_suite.py` is allowed to tell a session it may ship without
running the whole suite, so the ways it can be wrong are the ways coverage goes
missing silently:

* an edge the graph fails to see -- an import spelled in a way it does not
  read, a module named in a string, a script run by path -- so a test that
  could have caught the change is never selected;
* a boundary claimed without proof. Every place the selector runs less than the
  full suite is backed by a test here: the graph's rules on a small synthetic
  tree whose answer is known, and the claimed boundaries of *this* repository
  asserted against the real graph, so a new import that crosses one shows up
  as a failure rather than as a quietly wider selection nobody reads;
* a gap -- a directory nobody wrote a rule for, or a module no test imports.
  Those are meant to be safe by construction (they escalate to the full
  suite), and the tests below prove the escalation rather than trusting it;
* a marker that has drifted off the tests it names, which turns the one
  subtraction the path rules make into a subtraction of nothing, or of the
  wrong thing.

The marker guards are the counterpart of `tests/test_browser_tier.py`: that
file proves `browser` still tracks the Playwright tests, this one proves
`serviceworker` still tracks the tests that let a real worker claim a page.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "select_suite.py"
_TESTS_DIR = Path(__file__).parent

# What makes a test a service-worker test: it waits for the deck's own worker to
# take control of the page, or it opens a context built for counting claims.
# Merely passing `service_workers="allow"` is not enough -- the HTML-preview
# sandbox test allows one to prove the CSP refuses it, and its subject is the
# preview route, not sw.js.
_CLAIM_CALLS = ("navigator.serviceWorker.controller", "_worker_claim_context")


@cache
def selector():
    """The script, loaded as a module. It is a script, so it has no import path."""
    spec = importlib.util.spec_from_file_location("select_suite", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed: the script's dataclasses carry string
    # annotations, and `@dataclass` resolves those through `sys.modules`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@cache
def repo_graph():
    """This checkout's graph, built once: it reads every Python file here."""
    return selector().ReachGraph(REPO)


def _select(*paths):
    return selector().select(paths, graph=repo_graph())


# --------------------------------------------------------------------------
# the path rules, for everything that is not a Python module or test file


def test_a_documentation_change_needs_no_pytest_run():
    # No graph is handed in, and none is needed: documentation is answered by the
    # path rules alone, so judging it never pays for reading every Python file.
    chosen = selector().select(
        ["docs/agents/domain.md", ".claude/codebase/secretary.md", "README.md"]
    )
    assert chosen.pytest_args == []
    assert chosen.command() == ""


def test_a_host_script_change_stops_at_the_fast_tier():
    # No graph handed in: neither path is Python, so the path rules answer alone and
    # the test never pays for reading every Python file (#3066).
    chosen = selector().select(["scripts/kube_pytest.sh", "poller/manifests/agentdeck.toml"])
    assert chosen.pytest_args == ["-q", "-m", "not browser"]


@pytest.mark.parametrize(
    "path",
    [
        "src/agentdeck/web/static/sw.js",
        "src/agentdeck/web/templates/base.html",
        "src/agentdeck/web/static/session_bottom_follow.js",
        "tests/conftest.py",
        "pyproject.toml",
        "uv.lock",
        # Half of what a claim test asserts is the *control* that must survive
        # the reload, and those controls live in ordinary pages. The model
        # picker `test_a_service_worker_claim_waits_for_a_picked_model` drives
        # (`#continue-chat-model`) is declared in `session.html`, so a
        # template-only edit can break a claim test with sw.js untouched, and no
        # import says which page renders which template.
        "src/agentdeck/web/templates/session.html",
        "src/agentdeck/web/templates/dashboard.html",
        "src/agentdeck/web/templates/partials/composer_controls.html",
    ],
)
def test_the_service_worker_group_is_never_dropped_by_a_change_that_can_reach_it(path):
    assert "serviceworker" in _select(path).reaches, f"{path} was allowed to skip the claim tests"


def test_the_model_picker_a_claim_test_drives_is_declared_where_the_rule_assumes():
    """The template rule above is only right while the control really lives in
    a page. Asserted against the real files rather than described in a comment:
    if the picker moves to a static script, `session.html` stops being the thing
    that has to run the claim tests and the rule needs rewriting.
    """
    session_html = (REPO / "src/agentdeck/web/templates/session.html").read_text()
    assert 'id="continue-chat-model"' in session_html
    claim_test = (_TESTS_DIR / "test_web.py").read_text()
    assert 'page.locator("#continue-chat-model")' in claim_test


def test_an_unclaimed_path_escalates_to_the_whole_suite():
    """A new top-level directory must cost time, never coverage."""
    chosen = _select("a_directory_nobody_has_written_a_rule_for/thing.py")
    assert "serviceworker" in chosen.reaches
    assert chosen.reasons[0][1] == selector().UNCLAIMED


def test_the_widest_changed_path_wins():
    """Areas are a union, so one sw.js in a hundred docs still runs everything."""
    assert "serviceworker" in _select("README.md", "src/agentdeck/web/static/sw.js").reaches


# --------------------------------------------------------------------------
# the import graph, on a tree small enough to know the right answer


_BASE_TREE = {
    "src/agentdeck/__init__.py": "",
    "src/agentdeck/core.py": "X = 1\n",
    "src/agentdeck/work.py": "from .core import X\n",
    "src/agentdeck/web.py": "from agentdeck import work\n",
    "src/agentdeck/lonely.py": "Y = 2\n",
    "src/agentdeck/pkg/__init__.py": "from .heavy import H\n",
    "src/agentdeck/pkg/heavy.py": "H = 1\n",
    "src/agentdeck/pkg/leaf.py": "L = 1\n",
    "tests/conftest.py": "from agentdeck.pkg import leaf\n",
    "tests/test_work.py": "from agentdeck.work import X\n\ndef test_x(): pass\n",
    "tests/test_web.py": "import agentdeck.web\n\ndef test_x(): pass\n",
    "tests/test_heavy.py": "from agentdeck.pkg.heavy import H\n\ndef test_x(): pass\n",
    "tests/test_other.py": "def test_x(): pass\n",
}


def _as_path(name: str) -> str:
    """`tests__test_x` -> `tests/test_x.py`; `scripts__go_sh` -> `scripts/go.sh`."""
    path = name.replace("__", "/")
    for suffix in ("py", "sh"):
        if path.endswith(f"_{suffix}"):
            return f"{path[: -len(suffix) - 1]}.{suffix}"
    return f"{path}.py"


def _graph(tmp_path, paths=None, **extra):
    """A synthetic checkout: the base tree plus `paths` (real paths) and `extra`
    (named as `_as_path` reads)."""
    files = dict(_BASE_TREE)
    files.update(paths or {})
    files.update({_as_path(name): text for name, text in extra.items()})
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return selector().ReachGraph(tmp_path)


def _files(graph, *paths):
    return set(selector().select(paths, graph=graph).files)


def test_a_module_selects_every_test_file_that_reaches_it_and_no_other(tmp_path):
    """Directly, through a relative import, and through `from pkg import mod`."""
    graph = _graph(tmp_path)
    assert _files(graph, "src/agentdeck/core.py") == {"tests/test_work.py", "tests/test_web.py"}
    assert _files(graph, "src/agentdeck/web.py") == {"tests/test_web.py"}


def test_the_selected_files_run_whole_and_say_how_they_were_reached(tmp_path):
    chosen = selector().select(["src/agentdeck/core.py"], graph=_graph(tmp_path))
    assert chosen.pytest_args == ["-q", "tests/test_web.py", "tests/test_work.py"]
    printed = selector().report(chosen)
    assert (
        "tests/test_web.py -> src/agentdeck/web.py -> src/agentdeck/work.py "
        "-> src/agentdeck/core.py"
    ) in printed
    assert "2 test files" in chosen.reasons[0][1]


def test_what_conftest_runs_for_every_test_runs_the_whole_suite(tmp_path):
    chosen = selector().select(["src/agentdeck/pkg/leaf.py"], graph=_graph(tmp_path))
    assert "serviceworker" in chosen.reaches
    assert "tests/conftest.py runs it for every test" in chosen.reasons[0][1]


def test_a_parent_package_init_is_not_an_edge_to_everything_it_imports(tmp_path):
    """conftest imports `pkg.leaf`, which executes `pkg/__init__`, which imports
    `heavy`. Counting that would make `heavy` reach every test through import
    side effects alone; it reaches the test that imports it by name."""
    graph = _graph(tmp_path)
    assert _files(graph, "src/agentdeck/pkg/heavy.py") == {"tests/test_heavy.py"}


def test_a_package_initializer_conftest_runs_for_every_test_runs_everything(tmp_path):
    """`from agentdeck.pkg import leaf` executes `pkg/__init__.py` before `leaf`,
    so conftest runs that initializer for every test even though it names only
    the submodule."""
    chosen = selector().select(["src/agentdeck/pkg/__init__.py"], graph=_graph(tmp_path))
    assert "serviceworker" in chosen.reaches
    assert "tests/conftest.py runs this package initializer" in chosen.reasons[0][1]


def test_a_package_initializer_is_reached_by_whatever_imports_into_its_package(tmp_path):
    graph = _graph(
        tmp_path,
        {
            "src/agentdeck/other/__init__.py": "VALUE = 1\n",
            "src/agentdeck/other/mod.py": "M = 1\n",
        },
        tests__test_sub="from agentdeck.other.mod import M\n",
        tests__test_patch='TARGET = "agentdeck.other.mod.M"\n',
    )
    assert _files(graph, "src/agentdeck/other/__init__.py") == {
        "tests/test_sub.py",
        "tests/test_patch.py",
    }


def test_any_python_change_also_runs_the_ruff_guard_and_says_why(tmp_path):
    """The one test named on purpose (#3584): nothing imports a lint check."""
    graph = _graph(tmp_path, tests__test_ruff_clean="def test_x(): pass\n")
    chosen = selector().select(["src/agentdeck/core.py"], graph=graph)
    assert chosen.files == {"tests/test_web.py", "tests/test_work.py", "tests/test_ruff_clean.py"}
    assert "keeps master `ruff check` clean (#3584)" in selector().report(chosen)
    # No Python change, no guard; and a whole-suite tier already runs it, unchanged.
    docs = selector().select(["docs/agents/domain.md"], graph=graph)
    assert docs.files == frozenset() and len(docs.reasons) == 1
    whole = selector().select(["src/agentdeck/lonely.py"], graph=graph)
    assert whole.files == frozenset() and "#3584" not in selector().report(whole)


def test_the_report_shows_every_changed_path_that_selected_a_test(tmp_path):
    """`test_web.py` reaches both `web.py` and `core.py`; the report must show
    why each of them selected it, not just the first chain it met."""
    chosen = selector().select(
        ["src/agentdeck/core.py", "src/agentdeck/web.py"], graph=_graph(tmp_path)
    )
    printed = selector().report(chosen)
    assert "  src/agentdeck/core.py:" in printed
    assert "  src/agentdeck/web.py:" in printed
    assert (
        "tests/test_web.py -> src/agentdeck/web.py -> src/agentdeck/work.py "
        "-> src/agentdeck/core.py"
    ) in printed
    assert "    tests/test_web.py -> src/agentdeck/web.py\n" in printed + "\n"


def test_a_module_no_test_reaches_runs_the_whole_suite(tmp_path):
    """Nothing can be shown to reach it -- which is not the same as nothing can."""
    chosen = selector().select(["src/agentdeck/lonely.py"], graph=_graph(tmp_path))
    assert "serviceworker" in chosen.reaches
    assert "no test file imports or names it" in chosen.reasons[0][1]


def test_a_module_named_in_a_patch_target_or_a_command_line_is_reached(tmp_path):
    graph = _graph(
        tmp_path,
        tests__test_patch_it=(
            'def test_x(monkeypatch):\n    monkeypatch.setattr("agentdeck.lonely.Y", 3)\n'
        ),
        tests__test_run_it='import sys\nCMD = [sys.executable, "-m", "agentdeck.core"]\n',
    )
    assert _files(graph, "src/agentdeck/lonely.py") == {"tests/test_patch_it.py"}
    assert "tests/test_run_it.py" in _files(graph, "src/agentdeck/core.py")


def test_a_module_name_built_at_run_time_reaches_the_whole_package(tmp_path):
    graph = _graph(
        tmp_path,
        src__agentdeck__loader=(
            "import importlib\n\ndef load(name):\n"
            '    return importlib.import_module(f"agentdeck.pkg.{name}")\n'
        ),
        tests__test_loader="import agentdeck.loader\n",
    )
    assert _files(graph, "src/agentdeck/pkg/heavy.py") == {
        "tests/test_heavy.py",
        "tests/test_loader.py",
    }


def test_a_script_a_test_runs_by_path_carries_the_chain_through(tmp_path):
    graph = _graph(
        tmp_path,
        scripts__tool_py="from agentdeck.lonely import Y\n",
        tests__test_tool='from pathlib import Path\nSCRIPT = Path("scripts") / "tool.py"\n',
    )
    assert _files(graph, "src/agentdeck/lonely.py") == {"tests/test_tool.py"}


def test_a_shell_script_that_runs_a_module_carries_the_chain_through(tmp_path):
    graph = _graph(
        tmp_path,
        scripts__go_sh='#!/bin/sh\nexec python -m agentdeck.lonely "$@"\n',
        tests__test_go='SCRIPT = "scripts/go.sh"\n',
    )
    assert _files(graph, "src/agentdeck/lonely.py") == {"tests/test_go.py"}


def test_a_docstring_that_names_a_file_is_not_a_use_of_it(tmp_path):
    graph = _graph(
        tmp_path,
        tests__test_prose=(
            '"""About agentdeck.lonely and scripts/tool.py."""\n\ndef test_x(): pass\n'
        ),
    )
    chosen = selector().select(["src/agentdeck/lonely.py"], graph=graph)
    assert "serviceworker" in chosen.reaches, "the docstring was counted as an import"


def test_a_package_module_named_only_by_its_bare_file_name_is_not_reached(tmp_path):
    """`state.py` in a message is not a reason to run everything that imports
    state; a package module is reached through its imports. A path with a
    directory in it still counts, because that is how a test reads a source."""
    graph = _graph(
        tmp_path,
        tests__test_bare='MESSAGE = "see lonely.py"\n',
        tests__test_path='SOURCE = "src/agentdeck/lonely.py"\n',
        tests__test_absolute='SOURCE = "/home/someone/checkout/src/agentdeck/lonely.py"\n',
    )
    assert _files(graph, "src/agentdeck/lonely.py") == {
        "tests/test_path.py",
        "tests/test_absolute.py",
    }


def test_a_test_that_globs_for_test_files_is_reached_by_every_test_file(tmp_path):
    graph = _graph(
        tmp_path,
        tests__test_hygiene='from pathlib import Path\nFOUND = Path(".").glob("test_*.py")\n',
    )
    assert _files(graph, "tests/test_other.py") == {"tests/test_other.py", "tests/test_hygiene.py"}


def test_a_changed_test_helper_selects_the_tests_that_import_it(tmp_path):
    graph = _graph(
        tmp_path,
        tests__helper_py="def h(): pass\n",
        tests__test_uses="from helper import h\n\ndef test_x(): h()\n",
        tests__test_uses_too="from tests.helper import h\n",
    )
    assert _files(graph, "tests/helper.py") == {"tests/test_uses.py", "tests/test_uses_too.py"}


def test_a_changed_test_file_runs_itself_and_whatever_imports_it(tmp_path):
    graph = _graph(tmp_path, tests__test_reuses="from test_work import test_x\n")
    assert _files(graph, "tests/test_work.py") == {"tests/test_work.py", "tests/test_reuses.py"}


def test_a_deleted_module_still_selects_the_files_that_name_it(tmp_path):
    """A caller left behind by a deletion is exactly the test that has to run."""
    graph = _graph(
        tmp_path,
        tests__test_stale="from agentdeck.gone import g\n",
        tests__test_stale_too="from agentdeck import gone_too\n",
    )
    assert _files(graph, "src/agentdeck/gone.py") == {"tests/test_stale.py"}
    assert _files(graph, "src/agentdeck/gone_too.py") == {"tests/test_stale_too.py"}


def test_a_deletion_nothing_names_runs_everything_even_beside_other_changes(tmp_path):
    """Nothing left naming a deleted file is not proof nothing depended on it,
    and an unrelated module changed in the same diff does not make it safer."""
    graph = _graph(tmp_path)
    alone = selector().select(["src/agentdeck/gone.py"], graph=graph)
    assert "serviceworker" in alone.reaches
    with_another = selector().select(["src/agentdeck/gone.py", "src/agentdeck/web.py"], graph=graph)
    assert "serviceworker" in with_another.reaches
    assert not with_another.files
    assert (
        "nothing left in the tree imports or names it"
        in dict(with_another.reasons)["src/agentdeck/gone.py"]
    )


def test_a_file_that_will_not_parse_runs_everything(tmp_path):
    """Mid-edit syntax is not evidence of what a file imports, and an unread
    file anywhere could be the one that reaches the change."""
    graph = _graph(tmp_path, tests__test_broken="def test_x(:\n")
    chosen = selector().select(["src/agentdeck/web.py"], graph=graph)
    assert "serviceworker" in chosen.reaches
    assert "tests/test_broken.py did not parse" in chosen.reasons[0][1]


def test_what_conftest_imports_for_browser_tests_reaches_browser_tests_only(
    tmp_path, monkeypatch
):
    """conftest's Playwright `goto` wrapper imports a helper from a test module.
    That import runs only inside a browser test, so it widens the browser test
    files and nothing else."""
    graph = _graph(
        tmp_path,
        tests__conftest_py=(
            "from agentdeck.pkg import leaf\n\n"
            "def _front_page_with_chats(url):\n"
            "    from test_big import helper\n"
            "    return helper(url)\n"
        ),
        tests__test_big="import agentdeck.lonely\n\ndef helper(url): return url\n",
        tests__test_ui="import pytest\n\n@pytest.mark.browser\ndef test_x(): pass\n",
    )
    del graph
    # The real conftest names no such function any more (#2603), so the exemption
    # this proves is switched on here rather than read from the script.
    module = selector()
    monkeypatch.setattr(
        module,
        "CONFTEST_BROWSER_ONLY",
        {"_front_page_with_chats": "runs inside a browser test"},
    )
    graph = module.ReachGraph(tmp_path)
    assert set(module.select(["src/agentdeck/lonely.py"], graph=graph).files) == {
        "tests/test_big.py",
        "tests/test_ui.py",
    }


@pytest.mark.parametrize(
    ("spelling", "source", "marks"),
    [
        ("pytest.mark", "import pytest\n@pytest.mark.browser\ndef test_x(): ...\n", {"browser"}),
        (
            "from pytest import mark",
            "from pytest import mark\n@mark.browser\ndef test_x(): ...\n",
            {"browser"},
        ),
        (
            "from pytest import mark, on a claim test",
            "from pytest import mark\n@mark.browser\n@mark.serviceworker\ndef test_x(): ...\n",
            {"browser", "serviceworker"},
        ),
        (
            "import pytest as pt",
            "import pytest as pt\n@pt.mark.browser\ndef test_x(): ...\n",
            {"browser"},
        ),
        (
            "module-level pytestmark",
            "import pytest\npytestmark = pytest.mark.browser\n",
            {"browser"},
        ),
    ],
)
def test_a_marker_is_recognised_however_the_file_spells_it(tmp_path, spelling, source, marks):
    """The marks decide which of a file's tests a whole-suite tier keeps and
    which files get conftest's browser-only reach. Searching for the literal
    `pytest.mark.browser` missed every spelling but the first; read off the
    parsed tree instead."""
    graph = _graph(tmp_path, tests__test_new=source)
    assert graph.markers["tests/test_new.py"] == marks, spelling


def test_a_rule_tier_keeps_the_browser_tests_of_the_files_the_graph_chose(tmp_path):
    """A host script asks for the fast tier; a module whose only test is a
    browser test must not lose that test to the fast tier's `not browser`."""
    graph = _graph(
        tmp_path,
        tests__test_ui=(
            "import pytest\nimport agentdeck.lonely\n\n@pytest.mark.browser\ndef test_x(): pass\n"
        ),
    )
    chosen = selector().select(["scripts/tool.sh", "src/agentdeck/lonely.py"], graph=graph)
    assert chosen.reaches == {"python", "browser"}


def test_selecting_every_test_file_is_just_the_whole_suite(tmp_path):
    graph = _graph(
        tmp_path,
        tests__test_other="import agentdeck.core\n",
        tests__test_heavy="from agentdeck.pkg.heavy import H\nimport agentdeck.core\n",
    )
    assert "serviceworker" in selector().select(["src/agentdeck/core.py"], graph=graph).reaches


# --------------------------------------------------------------------------
# the boundaries this repository's graph claims, asserted against the graph


def test_every_python_file_in_this_checkout_is_read():
    """One unread file would disable the graph, so the whole suite runs every
    time. Loud here instead of quietly slow."""
    assert repo_graph().unparsed == []


def test_the_graph_sees_the_edges_it_is_trusted_with():
    """If the graph silently stopped reading imports, every selection would
    shrink to nothing and every run would escalate -- or worse, it would keep
    a stale shape. Pinned on edges that have to exist."""
    work = _select("src/agentdeck/work.py").files
    assert "tests/test_work_projection.py" in work
    assert "tests/test_state.py" in work, "state.py imports work.py"
    engine = _select("src/agentdeck/poller_engine/cycle.py").files
    assert "tests/test_poller_engine_cycle.py" in engine


def test_a_poller_engine_change_does_not_run_the_web_or_provider_suites():
    """The engine sits behind an import boundary (`tests/test_poller_engine_boundary.py`);
    nothing on the web or provider side can import its cycle."""
    engine = _select("src/agentdeck/poller_engine/cycle.py")
    assert engine.files, engine.reasons
    for unrelated in (
        "tests/test_web.py",
        "tests/test_claude_worker.py",
        "tests/test_codex_app_server.py",
        "tests/test_transcripts.py",
        "tests/test_chats_page.py",
    ):
        assert unrelated not in engine.files, f"{unrelated} became reachable from the engine"


def test_a_web_change_still_pulls_the_service_worker_tests_it_can_reach():
    """The claim tests build the real app, so a module on the page path selects
    the files that hold them -- and those files run whole, claims included."""
    graph = repo_graph()
    holders = {path for path, marks in graph.markers.items() if "serviceworker" in marks}
    assert holders, "no file holds a service-worker test any more"
    for module in (
        "src/agentdeck/web/render.py",
        "src/agentdeck/web/routes_pages.py",
        "src/agentdeck/state.py",
    ):
        chosen = _select(module)
        assert "serviceworker" in chosen.reaches or holders <= chosen.files, module


@pytest.mark.parametrize(
    "path",
    [
        "src/agentdeck/models.py",
        "src/agentdeck/poller_engine/state.py",
        "src/agentdeck/host_slots.py",
        # conftest imports `agentdeck.poller_engine.state`, which runs these.
        "src/agentdeck/poller_engine/__init__.py",
        "src/agentdeck/__init__.py",
    ],
)
def test_what_conftest_uses_for_every_test_is_named_and_runs_everything(path):
    chosen = _select(path)
    assert "serviceworker" in chosen.reaches
    assert "tests/conftest.py runs" in chosen.reasons[0][1]


#: Where a `CONFTEST_BROWSER_ONLY` helper may be used: inside the `goto`
#: wrapper `pytest_configure` installs on Playwright's `Page`, which only a
#: browser test ever calls.
_BROWSER_ONLY_SCOPE = ("pytest_configure", "goto")


def _uses_outside_the_goto_wrapper(source: str, name: str) -> tuple[int, list[str]]:
    """How often `name` is used in `source`, and where, outside the wrapper.

    Parent-aware: each use is placed by the chain of functions around it, so a
    direct call in `pytest_configure` itself, or a use at module level, is a
    use outside the browser path even though `goto` sits inside
    `pytest_configure` too.
    """
    uses = 0
    outside: list[str] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        nonlocal uses
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, (*scope, child.name))
                continue
            if isinstance(child, ast.Name) and child.id == name:
                uses += 1
                if scope[: len(_BROWSER_ONLY_SCOPE)] != _BROWSER_ONLY_SCOPE:
                    outside.append("/".join(scope) or "<module>")
            visit(child, scope)

    visit(ast.parse(source), ())
    return uses, outside


def test_the_browser_only_conftest_imports_really_are_browser_only():
    """`CONFTEST_BROWSER_ONLY` narrows what a conftest import reaches, so it
    must be true: each named function is only ever used inside the Playwright
    `goto` wrapper that `pytest_configure` installs."""
    source = (_TESTS_DIR / "conftest.py").read_text()
    tree = ast.parse(source)
    top_level = {n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    for name in selector().CONFTEST_BROWSER_ONLY:
        assert name in top_level, f"{name} is no longer a conftest function"
        uses, outside = _uses_outside_the_goto_wrapper(source, name)
        assert uses, f"{name} is never used"
        assert not outside, f"{name} is used outside the browser-only goto wrapper: {outside}"


@pytest.mark.parametrize(
    ("where", "source"),
    [
        (
            "pytest_configure",
            "def helper(url): ...\n"
            "def pytest_configure(config):\n"
            "    def goto(self, url):\n"
            "        return helper(url)\n"
            "    helper('/')\n",
        ),
        (
            "<module>",
            "def helper(url): ...\n"
            "def pytest_configure(config):\n"
            "    def goto(self, url):\n"
            "        return helper(url)\n"
            "ALIAS = helper\n",
        ),
        (
            "some_fixture",
            "def helper(url): ...\ndef some_fixture():\n    helper('/')\n",
        ),
    ],
)
def test_the_browser_only_guard_catches_a_use_outside_the_wrapper(where, source):
    """The guard above is only worth its green if it goes red here: a direct
    call during configuration or a module-level use would run the helper's
    imports for every test, while the graph still counts them as browser-only."""
    _, outside = _uses_outside_the_goto_wrapper(source, "helper")
    assert outside == [where]


def test_the_browser_only_guard_accepts_the_wrapper_itself():
    source = (
        "def helper(url): ...\n"
        "def pytest_configure(config):\n"
        "    def goto(self, url):\n"
        "        return helper(url)\n"
    )
    assert _uses_outside_the_goto_wrapper(source, "helper") == (1, [])


def test_pytest_collects_the_files_the_graph_calls_tests():
    """`is_test_file` assumes pytest's default `test_*.py` under `tests/`."""
    pytest_section = (REPO / "pyproject.toml").read_text().split("[tool.pytest.ini_options]")[1]
    pytest_section = pytest_section.split("\n[", 1)[0]
    assert "python_files" not in pytest_section
    assert 'testpaths = ["tests"]' in pytest_section
    assert not any(_TESTS_DIR.glob("*/test_*.py")), "a nested test file the graph never selects"


# --------------------------------------------------------------------------
# the rule table against the repository it is about


@cache
def _git_is_usable_here() -> bool:
    """Whether the suite is running inside a git checkout at all.

    The `kube` job lane copies the working tree into a pod without `.git`, so
    `git` there fails with "not a repository" rather than telling the truth
    about the branch. The two tests below are about this repository, so in a
    pod they have nothing to be about and skip; on any real checkout -- the
    host, a worktree, a clone -- they run.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=REPO, capture_output=True, text=True
    )
    return probe.returncode == 0


needs_a_checkout = pytest.mark.skipif(
    not _git_is_usable_here(), reason="no git checkout here (the pod lane copies the tree in)"
)


@needs_a_checkout
def test_every_tracked_top_level_path_has_a_rule():
    """A gap is safe but expensive, and nothing else would ever point one out.

    Judged on the real checkout rather than a fixed list, so a directory added
    next year shows up here instead of quietly turning every change into a full
    suite.
    """
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    top_level = sorted({line.split("/")[0] for line in tracked if line})
    # One real path per top-level entry, because the rules match paths, not names.
    probes = {}
    for entry in top_level:
        probes[entry] = next(
            line
            for line in tracked
            if (line == entry or line.startswith(f"{entry}/"))
            and not selector().is_graph_path(line)
        )
    unclaimed = [
        probe
        for probe in probes.values()
        if selector().claim(probe, repo_graph()).because == selector().UNCLAIMED
    ]
    assert not unclaimed, (
        "no rule in scripts/select_suite.py claims these paths, so every change "
        f"touching them silently costs a full suite: {unclaimed}"
    )


# --------------------------------------------------------------------------
# the pytest it tells you to run


def test_the_printed_command_never_blesses_a_bare_pytest():
    """The repo-root venv's editable install points at master's `src`, so the
    root `pytest` run from a worktree tests code the branch does not contain and
    calls it green. `AGENTS.md` says to run exactly what this prints, so a bare
    executable here would have blessed that exact mistake.
    """
    printed = _select("scripts/kube_pytest.sh").command()
    assert not printed.startswith("pytest"), printed
    assert printed.endswith('-q -m "not browser"')
    printed = _select("src/agentdeck/poller_engine/cycle.py").command()
    assert not printed.startswith("pytest"), printed
    assert " -q tests/" in printed


def test_the_runner_is_the_checkouts_own_pytest_when_it_has_one(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "pytest").write_text("#!/bin/sh\n")
    assert selector().worktree_pytest(tmp_path) == str(tmp_path / ".venv" / "bin" / "pytest")


def test_the_runner_falls_back_to_uv_which_creates_that_venv(tmp_path):
    assert selector().worktree_pytest(tmp_path) == "uv run pytest"


def test_the_tier_can_be_had_without_a_runner_at_all():
    """For a caller that already knows which interpreter it wants."""
    chosen = _select("scripts/kube_pytest.sh")
    assert chosen.arguments == '-q -m "not browser"'
    assert chosen.command("uv run pytest") == 'uv run pytest -q -m "not browser"'


def test_the_command_line_prints_a_command_on_its_own(monkeypatch, capsys):
    # The script's own entry point, in this process: a second Python reading the
    # whole checkout again would prove nothing the shared graph does not.
    graph = repo_graph()
    monkeypatch.setattr(selector(), "ReachGraph", lambda *_args: graph)
    assert (
        selector().main(
            ["--format", "args", "--paths", "src/agentdeck/poller_engine/cycle.py"]
        )
        == 0
    )
    printed = capsys.readouterr().out.split()
    assert printed[0] == "-q"
    assert "tests/test_poller_engine_cycle.py" in printed
    assert all(arg.startswith("tests/test_") for arg in printed[1:]), printed


# --------------------------------------------------------------------------
# the base it judges against


class _FakeGit:
    """Stands in for `git`, so a stale base can be staged without a network."""

    def __init__(self, *, fetch_fails=False, revision="a" * 40, resolves=True):
        self.fetch_fails = fetch_fails
        self.revision = revision
        self.resolves = resolves
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        verb = argv[1]
        if verb == "fetch":
            return _Finished(1 if self.fetch_fails else 0, "", "fatal: unable to access remote")
        if verb == "rev-parse":
            return _Finished(0 if self.resolves else 128, self.revision if self.resolves else "")
        if verb == "merge-base":
            return _Finished(0, self.revision)
        if verb == "diff":
            return _Finished(0, "src/agentdeck/poller_engine/cycle.py\n")
        return _Finished(0, "")  # ls-files --others

    @property
    def fetched(self) -> bool:
        return any(call[1] == "fetch" for call in self.calls)


class _Finished:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_a_remote_base_is_fetched_before_anything_is_subtracted():
    """`origin/master` in a long-lived worktree is whenever somebody last
    fetched, and a base ahead of the real branch point *drops* paths -- which is
    a test group subtracted for a file that did change."""
    git = _FakeGit()
    chosen = selector().selection_for_branch("origin/master", run=git, graph=repo_graph())
    assert git.fetched, "the remote-tracking base was used without refreshing it"
    assert "tests/test_poller_engine_cycle.py" in chosen.files
    assert "fetched just now" in chosen.reasons[0][0]


def test_a_base_that_cannot_be_refreshed_runs_everything():
    git = _FakeGit(fetch_fails=True)
    chosen = selector().selection_for_branch("origin/master", run=git)
    assert "serviceworker" in chosen.reaches, "a stale base was allowed to subtract a group"
    assert "could not fetch origin/master" in chosen.reasons[0][0]
    assert "unable to access remote" in chosen.reasons[0][0]


def test_an_exact_commit_needs_no_fetch():
    """The poller hands over the commit it compared; that is already exact."""
    sha = "b" * 40
    git = _FakeGit(revision=sha)
    chosen = selector().selection_for_branch(sha, run=git, graph=repo_graph())
    assert not git.fetched
    assert "tests/test_poller_engine_cycle.py" in chosen.files
    assert sha[:12] in chosen.reasons[0][0]


@pytest.mark.parametrize("base", ["master", "HEAD~1", "some-branch"])
def test_a_bare_local_name_is_refused_rather_than_trusted(base):
    """A local name says nothing about where the branch left the base."""
    git = _FakeGit()
    chosen = selector().selection_for_branch(base, run=git)
    assert "serviceworker" in chosen.reaches
    assert "not an exact commit" in chosen.reasons[0][0]


def test_a_base_that_names_nothing_runs_everything():
    git = _FakeGit(resolves=False)
    chosen = selector().selection_for_branch("deadbeef", run=git)
    assert "serviceworker" in chosen.reaches
    assert "does not name a commit" in chosen.reasons[0][0]


def test_a_rename_lists_the_old_name_too():
    """`git diff --name-only` reports a rename by its new name alone, which
    would leave every test still importing the old name unselected."""
    git = _FakeGit()
    selector().changed_paths("a" * 40, run=git)
    diff = next(call for call in git.calls if call[1] == "diff")
    assert "--no-renames" in diff


@needs_a_checkout
def test_the_selector_agrees_with_this_branch():
    """The script can read git as well as a list -- exercised, not assumed."""
    paths = selector().changed_paths("HEAD")
    assert isinstance(paths, list)
    assert selector().select(paths, graph=repo_graph()).headline


# --------------------------------------------------------------------------
# the marker the selector subtracts


@cache
def _marked_tests() -> list[tuple[str, str, set[str], str]]:
    """Every test function in tests/, with its markers and its own source."""
    found = []
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        lines = path.read_text().splitlines()
        for node in ast.parse("\n".join(lines)).body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            markers = set()
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute):
                    markers.add(target.attr)
            body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            found.append((path.name, node.name, markers, body))
    return found


def test_the_service_worker_group_is_not_empty():
    """A marker that matches nothing turns the subtraction into a no-op, and the
    suite would simply get slower again with nobody the wiser."""
    marked = [f"{f}::{n}" for f, n, markers, _ in _marked_tests() if "serviceworker" in markers]
    assert len(marked) >= 5, marked


def test_every_service_worker_test_is_also_a_browser_test():
    """`serviceworker` narrows `browser`; it does not stand beside it. If one
    escaped the wider marker, `-m 'not browser'` would start running it."""
    loose = [
        f"{f}::{n}"
        for f, n, markers, _ in _marked_tests()
        if "serviceworker" in markers and "browser" not in markers
    ]
    assert not loose, loose


def test_every_test_that_waits_for_a_real_claim_is_marked():
    unmarked = [
        f"{f}::{n}"
        for f, n, markers, body in _marked_tests()
        if any(call in body for call in _CLAIM_CALLS) and "serviceworker" not in markers
    ]
    assert not unmarked, (
        "these tests wait for the deck's own service worker to claim a page but "
        "are missing @pytest.mark.serviceworker, so a change to sw.js would not "
        f"run them: {unmarked}"
    )


def test_no_test_claims_the_service_worker_marker_without_driving_one():
    mismarked = [
        f"{f}::{n}"
        for f, n, markers, body in _marked_tests()
        if "serviceworker" in markers and not any(call in body for call in _CLAIM_CALLS)
    ]
    assert not mismarked, (
        "these tests carry @pytest.mark.serviceworker but never wait for a claim, "
        f"so they are skipped for no reason: {mismarked}"
    )


def test_the_marker_is_registered():
    """An unregistered marker is a warning, not an error, so `-m 'not
    serviceworker'` would silently deselect nothing at all."""
    assert "serviceworker:" in (REPO / "pyproject.toml").read_text()
