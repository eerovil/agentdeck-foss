"""Keep shared behavior out of adapters after architecture cleanups."""

import ast
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "agentdeck"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.add(f"{'.' * node.level}{node.module or ''}")
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
    return imports


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _defined_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_injection_domain_does_not_depend_on_the_web_layer():
    imports = _imports(SOURCE / "inject.py")
    assert not any(
        name == ".web"
        or name.startswith(".web.")
        or name == "agentdeck.web"
        or name.startswith("agentdeck.web.")
        for name in imports
    )


def test_browser_actions_do_not_depend_on_the_api_route_module():
    imports = _imports(SOURCE / "web" / "routes_actions.py")
    assert ".routes_api" not in imports
    assert "agentdeck.web.routes_api" not in imports


def test_shared_attachment_cleanup_has_one_domain_owner():
    assert "cleanup_uploaded_files" in _defined_functions(SOURCE / "attachments.py")
    assert "cleanup_uploaded_files" not in _defined_functions(SOURCE / "web" / "uploads.py")


def test_machine_account_selection_has_one_shared_request_owner():
    assert "delegation_account" in _defined_functions(SOURCE / "web" / "deps.py")
    assert "delegation_account" not in _defined_functions(SOURCE / "web" / "routes_api.py")
    assert "delegation_account" not in _defined_functions(SOURCE / "web" / "routes_actions.py")


def test_web_and_runtime_open_the_store_one_way():
    # One opener, so both processes run the same ordered migrations (#2601).
    assert "open_db" in _imported_names(SOURCE / "app.py")
    assert "open_db" in _imported_names(SOURCE / "runtime.py")
    assert not (SOURCE / "migrating_db.py").exists()
    assert not (SOURCE / "state_persistence.py").exists()


def test_release_policy_stays_independent_of_cycle_and_io_ports():
    imports = _imports(SOURCE / "poller_engine" / "release.py")
    assert ".cycle" not in imports
    assert ".ports" not in imports
    assert "agentdeck.poller_engine.cycle" not in imports
    assert "agentdeck.poller_engine.ports" not in imports
