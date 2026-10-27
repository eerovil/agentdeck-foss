#!/usr/bin/env python3
"""Pick the tests a change can actually reach, and say why (#1837, #2165).

`AGENTS.md` used to ask for one full `pytest -q` before shipping, full stop.
That is the right default when nobody can say which tests a change can reach --
but it makes every change pay for the whole suite, including the thousands of
tests it plainly cannot break. A change to the poller engine has no way of
reaching the provider parsers, and a change to one Work module has no way of
reaching the deploy scripts' tests.

This script answers the question "which tests can this change reach?" from the
paths it touched, and prints both the command to run and the reason each path
selected what it did. Three properties matter more than speed:

* **Conservative by construction.** A path nothing can account for escalates the
  whole run to the full suite. Adding a directory, or a module no test imports,
  costs time, never coverage.
* **Auditable.** Every answer comes with its reason, and every test file a
  Python change selects comes with the chain of imports that selected it, so a
  reviewer can disagree with a *link* rather than with an opaque verdict.
* **Derived, not listed.** It never names individual tests. A Python module
  under `src/agentdeck/`, or a Python file directly under `tests/`, selects
  every test file that imports it -- directly or through any chain of other
  modules, test helpers or scripts -- or names it in a string (a
  `monkeypatch.setattr("agentdeck.x.y", ...)` target, a `-m agentdeck.x`
  command line, a script's file name). Nothing about the grouping is written
  down by hand, so it cannot drift from the code: an import added tomorrow
  widens tomorrow's selection. Everything else -- templates, static assets,
  host scripts, manifests, config -- is still judged by the path rules in
  `RULES`, which subtract whole marked groups exactly as before.

What a Python edge does *not* count is everything a parent package's
`__init__.py` imports. The initializer itself is reached -- a changed one selects
every file that imports into its package, and the full suite when conftest is
one of them -- but its own imports are not followed from there. Importing
`agentdeck.poller_engine.state` runs the engine's `__init__`, which imports
every engine module, and `tests/conftest.py` imports exactly that -- so
following it would make the whole engine reach every test in the suite through
import side effects alone. The price of leaving it
out is import-time behaviour: a module whose *import* breaks is still caught,
because every changed module must be imported by at least one selected test
file (otherwise the run escalates), but a module that mutates global state when
merely imported would not be. Nothing here does that, and a module that starts
to should be imported by name where it matters.

Usage:

    scripts/select_suite.py                      # against origin/master, fetched first
    scripts/select_suite.py --base 13975b20b366  # against an exact commit
    scripts/select_suite.py --paths src/agentdeck/state.py
    scripts/select_suite.py --format command     # the whole command, safe pytest and all
    scripts/select_suite.py --format args        # just the arguments, if you run pytest yourself

The base has to be one it can trust: a remote-tracking ref, which it fetches
before comparing, or an exact commit SHA. A bare local name like `master` or
`HEAD~1` is refused with the full suite -- in a long-lived worktree those say
nothing about where the branch actually left the base.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import subprocess
import sys
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent

# The three marked groups a path rule can reach. "python" is everything the fast
# tier already runs; the other two are the groups a path rule may subtract.
PYTHON = "python"
BROWSER = "browser"
SERVICE_WORKER = "serviceworker"

NOTHING: frozenset[str] = frozenset()
PYTHON_ONLY = frozenset({PYTHON})
THROUGH_THE_BROWSER = frozenset({PYTHON, BROWSER})
EVERYTHING = frozenset({PYTHON, BROWSER, SERVICE_WORKER})


@dataclass(frozen=True)
class Rule:
    pattern: str
    reaches: frozenset[str]
    because: str


# Ordered: the first rule whose pattern matches a path claims it, so the
# specific entries come before the directory they live in. `*` crosses `/`,
# which is why `src/agentdeck/web/*` covers the whole subtree. Python modules
# under `src/agentdeck/` and Python files directly under `tests/` never get this
# far -- `claim` answers those from the import graph -- except `conftest.py`.
RULES: tuple[Rule, ...] = (
    Rule(
        "pyproject.toml",
        EVERYTHING,
        "pytest's own configuration, so it changes how every test runs",
    ),
    Rule("uv.lock", EVERYTHING, "the dependency set every test imports"),
    Rule(
        "public/*",
        EVERYTHING,
        "a public stand-in for a private module (#3623); only a whole public-copy run "
        "shows what it breaks there, so a change here keeps the full suite",
    ),
    Rule(
        "tests/conftest.py",
        EVERYTHING,
        "the fixtures, the browser harness and the slot caps every test shares",
    ),
    Rule(
        "src/agentdeck/web/static/sw.js",
        EVERYTHING,
        "the service worker itself",
    ),
    Rule(
        "src/agentdeck/web/templates/base.html",
        EVERYTHING,
        "the shell sw.js precaches and every claim test loads",
    ),
    Rule(
        "src/agentdeck/web/static/*",
        EVERYTHING,
        "a shell asset sw.js precaches and a claimed page re-runs",
    ),
    # What is left of the package here is its non-Python files: templates and
    # anything else loaded by path. A claim test's subject is half the worker
    # and half *the control that must survive the reload* -- the model picker in
    # `session.html`, the composer draft, the deck filter -- so a template can
    # break a claim test without touching sw.js, and no import says which page
    # renders which template. So these stay on the full suite.
    Rule(
        "src/agentdeck/*",
        EVERYTHING,
        "a page or asset the app loads by path, so it can break any rendered page, "
        "a claim test included",
    ),
    # Fixtures and data under tests/ are read by path, not imported, so the
    # import graph cannot say who reads them.
    Rule("tests/*", THROUGH_THE_BROWSER, "test data read by path rather than imported"),
    Rule(
        "scripts/*",
        PYTHON_ONLY,
        "a host-side script; nothing in it is rendered to a page",
    ),
    Rule(
        "poller/*",
        PYTHON_ONLY,
        "poller manifests, extensions and reference assets, all asserted without a browser",
    ),
    Rule("skills/*", PYTHON_ONLY, "a bundled skill asset, asserted without a browser"),
    Rule("systemd/*", PYTHON_ONLY, "unit files, asserted without a browser"),
    Rule("src/*", THROUGH_THE_BROWSER, "shipped code outside the agentdeck package"),
    Rule("docs/*", NOTHING, "documentation"),
    Rule(".claude/*", NOTHING, "documentation"),
    Rule(".github/*", NOTHING, "repository metadata, not imported by anything"),
    Rule("*.md", NOTHING, "documentation"),
    Rule("LICENSE", NOTHING, "documentation"),
    Rule("NOTICE", NOTHING, "documentation"),
    Rule(".gitignore", NOTHING, "repository metadata, not imported by anything"),
    Rule("config.example.toml", PYTHON_ONLY, "an example config the suite parses"),
    Rule("config.staging.example.toml", PYTHON_ONLY, "an example config the suite parses"),
    Rule("reports/*", NOTHING, "recorded output, not imported by anything"),
    Rule("screenshots/*", NOTHING, "recorded output, not imported by anything"),
)

UNCLAIMED = "no rule claims this path, so the whole suite runs"

# ----------------------------------------------------------------------------
# The import graph

#: Where the graph looks. `src` and `tests` are what it selects for; the rest
#: are there so a chain can pass *through* them -- a test that runs a script by
#: path reaches whatever that script imports.
GRAPH_ROOTS = ("src", "tests", "scripts", "poller", "skills", "systemd")

#: Non-Python files read for the modules and files they name. A shell script
#: that runs `python -m agentdeck.x` reaches `agentdeck.x`.
TEXT_SUFFIXES = frozenset({".sh", ".service", ".timer", ".socket", ".toml"})

CONFTEST = "tests/conftest.py"

#: `conftest.py` functions whose imports only ever run inside a browser test.
#: Their imports reach the browser-marked test files rather than the whole
#: suite. `tests/test_select_suite.py` proves each one is only called from the
#: Playwright `goto` wrapper, so the exemption cannot quietly outlive its reason.
#: Empty since #2603 removed the `goto` wrapper that rewrote addresses to `?chats=1`:
#: conftest now imports no test module, so there is nothing to narrow.
CONFTEST_BROWSER_ONLY: dict[str, str] = {}

#: A dotted `agentdeck.x.y` name anywhere in a string: a patch target, a
#: `python -m` argument, a logger name. Noise here only ever adds edges.
_DOTTED = re.compile(r"\bagentdeck(?:\.[A-Za-z_]\w*)+")
#: A dotted name that stops at a dot: the fixed half of a name built at run
#: time (`f"agentdeck.providers.{name}"`), or a prefix a test filters modules
#: by. It reaches every module under that package.
_DOTTED_PREFIX = re.compile(r"\bagentdeck(?:\.[A-Za-z_]\w*)*\.(?![A-Za-z_])")
#: A file name or path ending in `.py` or `.sh`: a script run by path.
_PATHISH = re.compile(r"[\w./-]*[\w-]\.(?:py|sh)\b")
#: The console script run by path, `.venv/bin/agentdeck`. Counted only in a
#: test or a host script: in the package it is the text of an instruction to an
#: agent, not something a test executes. A bare `"agentdeck"` is never counted:
#: it is far more often a repository or poller instance name.
_BINARY = re.compile(r"/bin/agentdeck\b")


def _module_names(path: str) -> list[str]:
    """The names an `import` can reach `path` by."""
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return []
    if pure.parts[0] == "src":
        parts = list(pure.with_suffix("").parts[1:])
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return [".".join(parts)] if parts else []
    # Tests, scripts and extensions import their neighbours by bare name after
    # putting the directory on `sys.path`; tests are also imported as `tests.x`.
    names = [pure.stem]
    if pure.parts[0] == "tests" and len(pure.parts) == 2:
        names.append(f"tests.{pure.stem}")
    return names


def _virtual_paths(name: str) -> set[str]:
    """Where a module that no longer exists would have lived.

    Kept as edges so a deleted module still finds the files that name it.
    """
    parts = name.split(".")
    if parts[0] == "agentdeck":
        base = "src/" + "/".join(parts)
        return {f"{base}.py", f"{base}/__init__.py"}
    if len(parts) == 1:
        return {f"tests/{name}.py"}
    return set()


def is_test_file(path: str) -> bool:
    """A file pytest collects: `test_*.py` directly under `tests/`."""
    pure = PurePosixPath(path)
    return pure.parent == PurePosixPath("tests") and fnmatch.fnmatch(pure.name, "test_*.py")


def is_graph_path(path: str) -> bool:
    """A changed path the import graph, rather than a rule, answers for."""
    if path == CONFTEST:
        return False
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return False
    return path.startswith("src/agentdeck/") or pure.parent == PurePosixPath("tests")


def _walk_skipping(tree: ast.AST, skip: frozenset[str]):
    """`ast.walk`, minus the bodies of the top-level functions named in `skip`."""
    queue = deque([tree])
    while queue:
        node = queue.popleft()
        yield node
        for child in ast.iter_child_nodes(node):
            if (
                node is tree
                and isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                and child.name in skip
            ):
                continue
            queue.append(child)


class ReachGraph:
    """Which files each file imports or names, over one checkout.

    Built from the files on disk, not from git, so it also works in the `kube`
    lane's pod, which has no `.git`.
    """

    def __init__(self, root: Path = REPO):
        self.root = root
        self.files = self._files()
        self.unparsed: list[str] = []
        self.markers: dict[str, set[str]] = {}
        self._modules: dict[str, set[str]] = {}
        self._by_name: dict[str, set[str]] = {}
        for path in self.files:
            for name in _module_names(path):
                self._modules.setdefault(name, set()).add(path)
            self._by_name.setdefault(PurePosixPath(path).name, set()).add(path)
        self._python = sorted(p for p in self.files if p.endswith(".py"))
        self._console_script = frozenset(self._modules.get("agentdeck.__main__", ()))
        self.edges: dict[str, set[str]] = {}
        #: Package `__init__.py` -> the files that import something inside that
        #: package, and therefore run it. Kept apart from `edges` on purpose:
        #: the initializer is on every such import path, but what *it* imports
        #: is not (see the module docstring).
        self.init_users: dict[str, set[str]] = {}
        browser_only: set[str] = set()
        for path in self.files:
            if path == CONFTEST:
                self.edges[path], browser_only = self._conftest_references()
                self._note_initializers(path, self.edges[path] | browser_only)
            else:
                self.edges[path] = self._references(path)
                self._note_initializers(path, self.edges[path])
        # What a browser test gets from conftest on top of what every test gets.
        for path, marks in self.markers.items():
            if BROWSER in marks or SERVICE_WORKER in marks:
                self.edges[path] |= browser_only
        self.reverse: dict[str, set[str]] = {}
        for source, targets in self.edges.items():
            for target in targets:
                self.reverse.setdefault(target, set()).add(source)
        self.test_files = frozenset(p for p in self.files if is_test_file(p))
        self.universal = self._forward_chains(CONFTEST) if CONFTEST in self.files else {}

    # -- reading ---------------------------------------------------------------

    def _files(self) -> list[str]:
        found = []
        for top in GRAPH_ROOTS:
            base = self.root / top
            if not base.is_dir():
                continue
            for path in base.rglob("*"):
                relative = path.relative_to(self.root)
                if any(part == "__pycache__" or part.startswith(".") for part in relative.parts):
                    continue
                if path.suffix == ".py" or path.suffix in TEXT_SUFFIXES:
                    if path.is_file():
                        found.append(relative.as_posix())
        return sorted(found)

    def _note_initializers(self, source: str, targets) -> None:
        """Every package `__init__.py` that importing `targets` executes."""
        for target in targets:
            if not target.startswith("src/"):
                continue
            parent = PurePosixPath(target).parent
            while parent.parts and parent != PurePosixPath("src"):
                initializer = (parent / "__init__.py").as_posix()
                if initializer != target and initializer != source:
                    self.init_users.setdefault(initializer, set()).add(source)
                parent = parent.parent

    def _read(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8", errors="replace")

    def _parse(self, path: str) -> ast.AST | None:
        try:
            tree = ast.parse(self._read(path))
        except (OSError, SyntaxError, ValueError):
            self.unparsed.append(path)
            return None
        return tree

    def _references(self, path: str) -> set[str]:
        if not path.endswith(".py"):
            found = self._text_references(path)
        else:
            tree = self._parse(path)
            if tree is None:
                return set()
            attributes: set[str] = set()
            found = self._python_references(path, ast.walk(tree), attributes)
            if is_test_file(path):
                # Every marker name the file could apply, however it spells it:
                # `from pytest import mark` with `@mark.browser` contains no
                # literal `pytest.mark.browser`, and neither do `import pytest as
                # pt`, `pytestmark = [...]` or `pytest.param(marks=...)`. A
                # same-named attribute that is not a marker only runs more tests.
                self.markers[path] = attributes & {BROWSER, SERVICE_WORKER}
        if not path.startswith("tests/"):
            # Nothing outside tests/ runs a test module; a script that names
            # `test_jobs.py` is describing it. (conftest's own reach into
            # `test_web.py` is handled separately, for browser tests only.)
            found = {target for target in found if not target.startswith("tests/")}
        if not path.startswith(("tests/", "scripts/", "systemd/")):
            found -= self._console_script
        return found

    def _conftest_references(self) -> tuple[set[str], set[str]]:
        tree = self._parse(CONFTEST)
        if tree is None:
            return set(), set()
        everyone = self._python_references(
            CONFTEST, _walk_skipping(tree, frozenset(CONFTEST_BROWSER_ONLY))
        )
        browser = set()
        for node in tree.body:
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in CONFTEST_BROWSER_ONLY
            ):
                browser |= self._python_references(CONFTEST, ast.walk(node))
        return everyone, browser - everyone

    def _python_references(self, path: str, nodes, attributes: set[str] | None = None):
        """What the parsed file imports or names, in one breadth-first pass.

        `nodes` must yield a parent before its children (both `ast.walk` and
        `_walk_skipping` do), which is what lets a docstring be recognised
        before its string is read. Attribute names are collected on the way
        into `attributes`, for the marker check.
        """
        names = _module_names(path)
        own = names[0] if names else ""
        package = own if path.endswith("/__init__.py") else own.rpartition(".")[0]
        found: set[str] = set()
        # A docstring (or any bare string statement) is prose about a file, not
        # a use of it: `tests/test_browser_tier.py` named in conftest's comments
        # would otherwise make every test reach every file that test globs.
        prose: set[int] = set()
        for node in nodes:
            if isinstance(node, ast.Attribute):
                if attributes is not None:
                    attributes.add(node.attr)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                prose.add(id(node.value))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    found |= self._module_targets(alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    anchor = package.split(".") if package else []
                    anchor = anchor[: len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                    base = ".".join([*anchor, *([node.module] if node.module else [])])
                for alias in node.names:
                    submodule = f"{base}.{alias.name}" if base else alias.name
                    if alias.name != "*" and submodule in self._modules:
                        found |= self._modules[submodule]
                        continue
                    if alias.name != "*" and base.startswith("agentdeck"):
                        found |= _virtual_paths(submodule)  # a submodule since deleted
                    if base:
                        found |= self._module_targets(base)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in prose
            ):
                found |= self._string_targets(node.value)
        found.discard(path)
        return found

    def _text_references(self, path: str) -> set[str]:
        try:
            text = self._read(path)
        except OSError:
            return set()
        found = self._string_targets(text)
        found.discard(path)
        return found

    # -- resolving -------------------------------------------------------------

    def _module_targets(self, name: str) -> set[str]:
        """The file(s) `import name` executes the code of.

        The longest prefix that is a real module. When the full name is not one
        -- a deleted module, or an attribute in a patch target -- the place it
        would have lived is kept too, so a deletion still finds its callers.
        """
        parts = name.split(".")
        found: set[str] = set()
        if name not in self._modules:
            found |= _virtual_paths(name)
        for end in range(len(parts), 0, -1):
            candidate = ".".join(parts[:end])
            if candidate in self._modules:
                return found | self._modules[candidate]
        return found

    def _string_targets(self, text: str) -> set[str]:
        found: set[str] = set()
        if "agentdeck" in text:
            for match in _DOTTED.finditer(text):
                found |= self._module_targets(match.group())
            for match in _DOTTED_PREFIX.finditer(text):
                prefix = match.group()
                found |= {
                    path
                    for name, paths in self._modules.items()
                    if name.startswith(prefix) or name == prefix[:-1]
                    for path in paths
                }
            if _BINARY.search(text):
                found |= self._console_script
        if ".py" in text or ".sh" in text:
            for match in _PATHISH.finditer(text):
                found |= self._path_targets(match.group())
        stripped = text.strip()
        if "*" in stripped and stripped.endswith(".py") and "\n" not in stripped:
            # A test that globs for Python files reaches every file it could find.
            pattern = PurePosixPath(stripped).name
            found |= {p for p in self._python if fnmatch.fnmatch(PurePosixPath(p).name, pattern)}
        return found

    def _path_targets(self, token: str) -> set[str]:
        while token.startswith(("./", "../")):
            token = token.split("/", 1)[1]
        if "/" in token:
            # Relative to the checkout, a tail of it, or an absolute path into it.
            return {
                p
                for p in self.files
                if p == token or p.endswith("/" + token) or token.endswith("/" + p)
            }
        # A bare file name is how a script or test file is found by path. A
        # package module is reached through its imports instead, and `state.py`
        # in a docstring is not a reason to run everything that imports state.
        return {p for p in self._by_name.get(token, ()) if not p.startswith("src/agentdeck/")}

    # -- answering -------------------------------------------------------------

    def _forward_chains(self, start: str) -> dict[str, list[str]]:
        """Everything `start` reaches, each with the chain from `start` to it."""
        via: dict[str, str | None] = {start: None}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for target in sorted(self.edges.get(node, ())):
                if target not in via:
                    via[target] = node
                    queue.append(target)
        return {node: self._chain(via, node)[::-1] for node in via}

    def reaching_tests(self, path: str, runners=()) -> dict[str, list[str]]:
        """Every test file that reaches `path`, with the chain from it to `path`.

        `runners` are files that reach `path` without an edge to it: the files
        that execute a package initializer by importing a module under it.
        """
        via: dict[str, str | None] = {path: None}
        for runner in sorted(runners):
            via.setdefault(runner, path)
        queue = deque(via)
        while queue:
            node = queue.popleft()
            for source in sorted(self.reverse.get(node, ())):
                if source not in via:
                    via[source] = node
                    queue.append(source)
        return {node: self._chain(via, node) for node in via if node in self.test_files}

    @staticmethod
    def _chain(via: dict[str, str | None], node: str) -> list[str]:
        chain = [node]
        while via[chain[-1]] is not None:
            chain.append(via[chain[-1]])
        return chain

    def tiers_of(self, files) -> frozenset[str]:
        """The marked groups present in these test files."""
        tiers = {PYTHON}
        for path in files:
            marks = self.markers.get(path, set())
            if SERVICE_WORKER in marks:
                tiers |= {BROWSER, SERVICE_WORKER}
            elif BROWSER in marks:
                tiers.add(BROWSER)
        return frozenset(tiers)


# ----------------------------------------------------------------------------
# Reporting groups. Only for the reader: nothing is selected or dropped by
# these names, so a file in the wrong group costs a confusing line, not a test.

GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("selector", ("test_select_suite.py", "test_browser_tier.py", "test_suite_hygiene.py")),
    ("work", ("test_work_*",)),
    (
        "core state",
        (
            "test_state*",
            "test_models.py",
            "test_collector.py",
            "test_events*",
            "test_db*",
            "test_config*",
            "test_architecture_boundaries.py",
        ),
    ),
    (
        "chat",
        ("test_chat_*", "test_chatgpt_*", "test_inject*", "test_scheduled_*", "test_context_*"),
    ),
    ("deckhand", ("test_assistant*", "test_triage.py", "test_titles.py", "test_evidence*")),
    ("poller", ("test_poller_*",)),
    (
        "providers",
        ("test_claude_*", "test_codex_*", "test_cursor_*", "test_transcript*", "test_provider*"),
    ),
    ("runtime", ("test_runtime*", "test_*worker*", "test_*delivery*", "test_revival*")),
    ("web", ("test_web*", "test_*_web.py", "test_usage*", "test_sse_*", "test_render*")),
    (
        "host tooling",
        (
            "test_deploy*",
            "test_jobs*",
            "test_kube*",
            "test_heavy_*",
            "test_staging_*",
            "test_host_*",
            "test_slot_*",
            "test_gh_*",
            "test_install*",
        ),
    ),
)


def group_of(path: str) -> str:
    name = PurePosixPath(path).name
    for label, patterns in GROUPS:
        if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            return label
    return "other"


def _group_summary(files) -> str:
    counts = Counter(group_of(path) for path in files)
    return ", ".join(f"{label} {count}" for label, count in counts.most_common())


# ----------------------------------------------------------------------------
# Claims and selections


@dataclass(frozen=True)
class Claim:
    """What one changed path reaches: marked groups of the whole suite, or whole
    test files, and the reason to print for it."""

    reaches: frozenset[str]
    because: str
    files: frozenset[str] = frozenset()
    chains: tuple[tuple[str, ...], ...] = ()


def _rule_claim(path: str) -> Claim:
    for rule in RULES:
        if fnmatch.fnmatch(path, rule.pattern):
            return Claim(rule.reaches, rule.because)
    return Claim(EVERYTHING, UNCLAIMED)


def _graph_claim(path: str, graph: ReachGraph) -> Claim:
    if graph.unparsed:
        shown = ", ".join(graph.unparsed[:3])
        return Claim(
            EVERYTHING,
            f"the import graph is incomplete ({shown} did not parse), so nothing is deselected",
        )
    if path in graph.universal:
        chain = " -> ".join(graph.universal[path])
        return Claim(EVERYTHING, f"tests/conftest.py runs it for every test ({chain})")
    # A package initializer runs whenever anything inside its package is
    # imported, so the files importing into the package reach it too.
    runners = graph.init_users.get(path, set())
    for runner in sorted(runners):
        if runner in graph.universal:
            chain = " -> ".join([*graph.universal[runner], path])
            return Claim(
                EVERYTHING,
                f"tests/conftest.py runs this package initializer for every test ({chain})",
            )
    reached = graph.reaching_tests(path, runners)
    if not reached:
        if (graph.root / path).exists():
            return Claim(
                EVERYTHING,
                "no test file imports or names it, so nothing can be shown to reach it",
            )
        # Nothing left names it, so nothing can be shown to have depended on
        # it -- which is not the same as nothing having depended on it. That
        # doubt is the whole change's, not this path's, so it runs everything
        # whatever the other paths selected.
        return Claim(
            EVERYTHING,
            "deleted, and nothing left in the tree imports or names it, so nothing "
            "can be shown to have depended on it",
        )
    files = frozenset(reached)
    return Claim(
        NOTHING,
        f"imported or named by {len(files)} test files ({_group_summary(files)})",
        files,
        tuple(tuple(reached[f]) for f in sorted(files)),
    )


def claim(path: str, graph: ReachGraph | None = None) -> Claim:
    """What `path` can reach, and the reason to print for it."""
    if is_graph_path(path):
        return _graph_claim(path, graph if graph is not None else ReachGraph())
    return _rule_claim(path)


def worktree_pytest(root: Path | None = None) -> str:
    """A pytest that imports the checkout it is run from.

    `<checkout>/.venv/bin/pytest` when that exists, because a worktree's own
    venv has an editable install pointing at its own `src`. Otherwise `uv run
    pytest`, which creates that venv on the spot. What this must never return is
    a bare `pytest`: the repo-root venv is pinned to master's `src`, so the root
    executable run from a worktree silently tests the wrong code.
    """
    root = root or REPO
    local = root / ".venv" / "bin" / "pytest"
    return str(local) if local.is_file() else "uv run pytest"


@dataclass(frozen=True)
class Selection:
    """What to run: marked groups of the whole suite (`reaches`), or else a set
    of whole test files (`files`). Never both -- a whole-suite tier already
    includes every file."""

    reaches: frozenset[str]
    reasons: tuple[tuple[str, str], ...]
    files: frozenset[str] = frozenset()
    total_files: int = 0
    chains: tuple[tuple[str, ...], ...] = ()

    @property
    def pytest_args(self) -> list[str]:
        # Browser tests (service-worker ones included) left the per-change gate and run
        # nightly on master instead (#3046, #3054), so every whole-suite tier is the same run.
        if self.reaches:
            return ["-q", "-m", f"not {BROWSER}"]
        if self.files:
            return ["-q", *sorted(self.files)]
        return []

    @property
    def arguments(self) -> str:
        """Just the arguments, for a caller that supplies its own runner."""
        return " ".join(a if " " not in a else f'"{a}"' for a in self.pytest_args)

    def command(self, runner: str | None = None) -> str:
        """The whole command, including a pytest that imports *this* checkout.

        Never a bare `pytest`. The repo-root venv's editable install points at
        master's `src`, so the root `pytest` run from a worktree tests code the
        branch does not contain and reports it green -- and `AGENTS.md` tells
        the reader to run exactly what this prints, which would have blessed
        precisely that mistake.
        """
        if not self.pytest_args:
            return ""
        return f"{runner or worktree_pytest()} {self.arguments}".strip()

    @property
    def headline(self) -> str:
        if self.files and not self.reaches:
            of = f" of {self.total_files}" if self.total_files else ""
            return (
                f"Run {len(self.files)}{of} test files, whole: nothing else imports or "
                "names what this change touched."
            )
        if not self.reaches:
            return "No pytest run is required: this change is documentation only."
        if BROWSER in self.reaches or SERVICE_WORKER in self.reaches:
            return (
                "Run the full suite without browser tests: this change can reach the "
                "rendered pages, but browser tests run nightly on master, not per change (#3054)."
            )
        return "Run the fast tier: this change cannot reach anything that needs a browser."


# The one test this script names on purpose, against "Derived, not listed" above: no
# import reaches a lint check, and with no CI here nothing else runs `ruff check` before a
# merge -- lint drift on master had already broken three chats' checks (#3584). Any `*.py`
# change selects it; a whole-suite tier already runs it, so that tier is left as it is.
RUFF_GUARD = "tests/test_ruff_clean.py"
RUFF_GUARD_BECAUSE = f"a Python file changed: {RUFF_GUARD} keeps master `ruff check` clean (#3584)"


def select(paths, graph: ReachGraph | None = None) -> Selection:
    paths = sorted(dict.fromkeys(paths))
    if graph is None and any(is_graph_path(p) for p in paths):
        graph = ReachGraph()
    reaches: set[str] = set()
    files: set[str] = set()
    chains: list[tuple[str, ...]] = []
    reasons: list[tuple[str, str]] = []
    for path in paths:
        answer = claim(path, graph)
        reaches |= answer.reaches
        files |= answer.files
        chains.extend(answer.chains)
        reasons.append((path, answer.because))
    total = len(graph.test_files) if graph is not None else 0
    if files and graph is not None and graph.test_files <= files:
        reaches |= EVERYTHING  # every file, whole, is simply the full suite
    if reaches and files:
        # A whole-suite tier already runs these files; make sure it runs their
        # browser tests too, not only the marked groups the rules asked for.
        reaches |= graph.tiers_of(files) if graph is not None else EVERYTHING
        files = set()
        chains = []
    if (
        not reaches
        and graph is not None
        and RUFF_GUARD in graph.test_files
        and any(path.endswith(".py") for path in paths)
    ):
        files.add(RUFF_GUARD)
        reasons.append(("<any *.py change>", RUFF_GUARD_BECAUSE))
    return Selection(
        frozenset(reaches),
        tuple(reasons),
        frozenset(files),
        total,
        tuple(sorted(set(chains))),
    )


class StaleBase(Exception):
    """The base could not be proven current, so nothing may be subtracted.

    A remote-tracking ref in a long-lived worktree is a snapshot of whenever
    somebody last fetched. Diffing against a stale `origin/master` yields a
    changed-path set that is not the pull request's diff -- usually too *large*,
    which is harmless here, but a base ahead of the real branch point drops
    paths, and a dropped path is a test group silently subtracted. So the base
    is either refreshed, or given as an exact commit, or this is raised and the
    caller runs everything.
    """


def refreshed_base(base: str, run=subprocess.run) -> tuple[str, str]:
    """`base` made current, plus the sentence that says how it was trusted.

    Two trustworthy forms. `<remote>/<branch>` is fetched first, so it is as
    current as the remote was a moment ago. An exact commit needs no fetch --
    the caller has already decided which commit the comparison is against, and
    the poller does exactly that.
    """
    remote, _, branch = base.partition("/")
    if branch and "/" not in branch:
        fetch = run(["git", "fetch", "--quiet", remote, branch], capture_output=True, text=True)
        if fetch.returncode != 0:
            raise StaleBase(
                f"could not fetch {base} ({(fetch.stderr or '').strip() or 'git fetch failed'}), "
                "so it may be stale and no group may be dropped"
            )
        return base, f"{base}, fetched just now"
    resolved = run(
        ["git", "rev-parse", "--verify", f"{base}^{{commit}}"], capture_output=True, text=True
    )
    revision = resolved.stdout.strip()
    if resolved.returncode != 0 or not revision:
        raise StaleBase(f"{base} does not name a commit in this checkout")
    if not revision.startswith(base.lower()):
        raise StaleBase(
            f"{base} is a name, not an exact commit, so it cannot be shown to be current; "
            "pass a remote-tracking ref like origin/master, or the base commit's SHA"
        )
    return revision, f"{revision[:12]}, an exact commit given by the caller"


def changed_paths(base: str, run=subprocess.run) -> list[str]:
    """Every path this branch changed against `base`, committed or not.

    Uncommitted work counts: the run this script is choosing happens before the
    commit, so leaving it out would deselect tests for a file that is about to
    be part of the change. Renames count twice: `--no-renames` lists the old
    path as deleted, so the tests that still import it by its old name are
    selected -- a rename would otherwise report only the new name.
    """
    merge_base = run(["git", "merge-base", base, "HEAD"], capture_output=True, text=True)
    against = merge_base.stdout.strip() if merge_base.returncode == 0 else base
    seen: list[str] = []
    for args in (
        ["diff", "--name-only", "--no-renames", against],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        result = run(["git", *args], capture_output=True, text=True, check=True)
        seen.extend(line for line in result.stdout.splitlines() if line)
    return list(dict.fromkeys(seen))


def everything_because(reason: str) -> Selection:
    """The full suite, carrying why nothing could be subtracted."""
    return Selection(EVERYTHING, ((f"<base: {reason}>", "the base is not trustworthy"),))


def selection_for_branch(base: str, run=subprocess.run, graph: ReachGraph | None = None):
    """What this branch needs, refusing to subtract anything on a doubtful base."""
    try:
        against, how = refreshed_base(base, run=run)
    except StaleBase as refusal:
        return everything_because(str(refusal))
    selection = select(changed_paths(against, run=run), graph=graph)
    return Selection(
        selection.reaches,
        ((f"<base: {how}>", "the base this was judged against"),) + selection.reasons,
        selection.files,
        selection.total_files,
        selection.chains,
    )


def report(selection: Selection) -> str:
    lines = [selection.headline, ""]
    command = selection.command()
    if command:
        lines += [f"    {command}", ""]
    lines.append("Why:")
    for path, because in selection.reasons:
        lines.append(f"  {path} -- {because}")
    if not selection.reasons:
        lines.append("  nothing changed against the base")
    if selection.files:
        lines += ["", "For each changed path, every test file it selected and the chain that did:"]
        # One line per (changed path, test file): a test reached by two changed
        # modules shows both chains, so each path's selection stands on its own.
        by_path: dict[str, list[tuple[str, ...]]] = {}
        for chain in selection.chains:
            by_path.setdefault(chain[-1], []).append(chain)
        for changed in sorted(by_path):
            lines.append(f"  {changed}:")
            for chain in sorted(set(by_path[changed])):
                lines.append(f"    {' -> '.join(chain)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        default="origin/master",
        help=(
            "branch point to diff against: a remote-tracking ref (fetched first) or an "
            "exact commit SHA. A base that cannot be shown to be current runs the full suite."
        ),
    )
    parser.add_argument("--paths", nargs="*", help="judge these paths instead of asking git")
    parser.add_argument(
        "--format",
        choices=("report", "command", "args"),
        default="report",
        help=(
            "'command' prints the whole command with a pytest that imports this checkout; "
            "'args' prints only the arguments, for a caller with its own runner. Both are "
            "empty when no run is required."
        ),
    )
    args = parser.parse_args(argv)

    if args.paths is not None:
        selection = select(args.paths)
    else:
        selection = selection_for_branch(args.base)
    if args.format == "command":
        print(selection.command())
    elif args.format == "args":
        print(selection.arguments)
    else:
        print(report(selection))
    return 0


if __name__ == "__main__":
    sys.exit(main())
