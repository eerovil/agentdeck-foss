"""``agentdeck-project join|update``: a project run from its own repository's file (#3755)."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import tomllib

from agentdeck import gh_cli, project_join, project_source, setup_init
from agentdeck.poller_engine import github_authority

SHARED = """contract = 1
org = "acme"
ownership_label = ["agent", "claude"]

[decks]
main = "eerovil"

[[repos]]
id = "store"
holds_issues = true
holds_code = true

[[repos]]
id = "issues"
holds_issues = true
workspace = "store"
"""


class _Answer:
    def __init__(self, status: int, body: dict) -> None:
        self.status, self._body = status, json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _github(sha: str, text: str = SHARED):
    content = base64.b64encode(text.encode()).decode()
    seen = []

    def opener(request, _timeout):
        seen.append(request.full_url)
        return _Answer(200, {"encoding": "base64", "content": content, "sha": sha})

    return opener, seen


def _deck(tmp_path, monkeypatch, person="coworker"):
    instance = tmp_path / "instances" / "acme"
    instance.mkdir(parents=True)
    activations, timers = [], []
    monkeypatch.setattr(project_join, "_start_timer", timers.append)
    monkeypatch.setattr(github_authority, "authority_for", lambda owner: owner)
    monkeypatch.setattr(github_authority, "token", lambda authority: "t")
    monkeypatch.setattr(github_authority, "deck_person", lambda: person)
    monkeypatch.setattr(setup_init, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(project_join, "_instance", lambda instance_id: instance)
    monkeypatch.setattr(project_join, "_account", lambda: "claude_code:main")
    monkeypatch.setattr(
        project_join,
        "_activate",
        lambda instance_id, manifest, where, login, **_: activations.append(
            (instance_id, manifest.read_text(), login)
        ),
    )
    return instance, activations, timers


def _clone(folder, repository):
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    subprocess.run(
        ["git", "-C", str(folder), "remote", "add", "origin", f"git@github.com:{repository}.git"],
        check=True,
    )


def test_join_finds_the_clone_writes_this_machines_half_and_activates(
    tmp_path, monkeypatch, capsys
):
    instance, activations, timers = _deck(tmp_path, monkeypatch)
    clone = tmp_path / "work" / "shop"  # the folder name need not be the repository's
    _clone(clone, "Acme/store")
    opener, seen = _github("abc123")

    project_join.join("acme/issues", instance_id="acme", base=tmp_path / "work", opener=opener)

    assert seen == ["https://api.github.com/repos/acme/issues/contents/.agentdeck/poller.toml"]
    overlay = (instance / "overlay.toml").read_text()
    assert f'"store" = "{clone}"' in overlay and '"issues"' not in overlay  # borrowed: no clone
    assert 'account = "claude_code:main"' in overlay
    assert activations == [("acme", SHARED, "coworker")]
    assert project_source.read_source(instance) == project_source.Source(
        "acme/issues", ".agentdeck/poller.toml", "abc123"
    )
    assert "eerovil's deck is the main one" in capsys.readouterr().out
    assert timers == ["acme"]  # its wakes are switched on

    # Another project of the same owner may not take this instance over (#3756 review).
    try:
        project_join.join("acme/other", instance_id="acme", base=tmp_path, opener=opener)
    except project_join.JoinError as error:
        assert "already runs acme/issues/.agentdeck/poller.toml" in str(error)
        assert "--instance" in str(error)
    else:
        raise AssertionError("a second project took the instance over")
    assert len(activations) == 1


def test_an_existing_overlay_keeps_its_settings_and_gains_new_checkouts(tmp_path, monkeypatch):
    """Joining over an instance, and an update whose file adds a code repository (#3756)."""
    instance, activations, _timers = _deck(tmp_path, monkeypatch)
    work = tmp_path / "work"
    _clone(work / "shop", "acme/store")
    overlay = instance / "overlay.toml"
    overlay.write_text('contract = 1\n[checkouts]\nold = "/x"\n[worker]\naccount = "mine"\n')

    project_join.join("acme/issues", instance_id="acme", base=work, opener=_github("a")[0])
    text = overlay.read_text()
    assert f'[checkouts]\n"store" = "{work / "shop"}"\nold = "/x"' in text
    assert 'account = "mine"' in text

    _clone(work / "gallery.v2", "acme/gallery.v2")
    monkeypatch.setattr(project_join.poller_host, "instance_dir", lambda root, name: instance)
    overlay.write_text(overlay.read_text().replace('old = "/x"\n', ""))
    grown = SHARED + '\n[[repos]]\nid = "gallery.v2"\nholds_code = true\n'
    assert project_join.update("acme", opener=_github("b", grown)[0]) is True
    # Found beside the others, and a dotted id stays one key rather than a table.
    checkouts = tomllib.loads(overlay.read_text())["checkouts"]
    assert checkouts["gallery.v2"] == str(work / "gallery.v2")
    assert len(activations) == 2


def test_a_main_deck_missing_an_extension_setting_is_told_what_to_add(tmp_path, monkeypatch):
    monkeypatch.setattr(
        project_join.subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 78, "", "token_file: missing"),
    )
    try:
        project_join._activate("acme", tmp_path / "m", tmp_path, "eerovil", extensions=True)
    except project_join.JoinError as error:
        said = str(error)
    assert "token_file: missing" in said
    assert f"[extensions.<name>] in {tmp_path / 'overlay.toml'}" in said


def test_a_missing_clone_goes_into_a_fresh_checkouts_folder(tmp_path, monkeypatch):
    base = tmp_path / "fresh" / "acme"  # the default ~/<instance>, not made yet
    clones = []

    def run(argv, **kwargs):
        clones.append(argv)
        assert base.is_dir()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(project_join.subprocess, "run", run)
    shared = SHARED + '\n[[repos]]\nid = "other/api"\nholds_code = true\n'
    manifest = project_join.check(shared, "acme/issues")

    assert project_join.checkouts(manifest, "acme", base) == {
        "store": str(base / "store"),
        "other/api": str(base / "api"),  # an id that names its owner keeps it
    }
    assert [argv[3:] for argv in clones] == [
        ["https://github.com/acme/store.git", str(base / "store")],
        ["https://github.com/other/api.git", str(base / "api")],
    ]


def test_join_refuses_a_deck_that_names_no_person(tmp_path, monkeypatch):
    _instance, activations, _timers = _deck(tmp_path, monkeypatch, person="")
    opener, _seen = _github("abc123")
    try:
        project_join.join("acme/issues", instance_id="acme", base=tmp_path, opener=opener)
    except project_join.JoinError as error:
        assert "[deck] person" in str(error)
    else:
        raise AssertionError("joined with no person")
    assert activations == []


def test_update_applies_only_a_moved_file_and_the_page_says_when_it_moved(tmp_path, monkeypatch):
    instance, activations, _timers = _deck(tmp_path, monkeypatch)
    monkeypatch.setattr(project_join.poller_host, "instance_dir", lambda root, name: instance)
    project_source.write_source(
        instance, project_source.Source("acme/issues", ".agentdeck/poller.toml", "old")
    )

    async def gh(argv, *, timeout, repo=None, owner=None):
        assert argv[:3] == ["gh", "api", "repos/acme/issues/contents/.agentdeck/poller.toml"]
        return 0, b"new\n"

    monkeypatch.setattr(gh_cli, "run", gh)
    changes = asyncio.run(project_source.changed_sources(tmp_path))
    assert changes == {"acme": "acme/issues"}

    assert project_join.update("acme", opener=_github("old")[0]) is False
    assert activations == []
    assert project_join.update("acme", opener=_github("new")[0]) is True
    assert [login for *_rest, login in activations] == ["coworker"]
    assert project_source.read_source(instance).sha == "new"
    assert asyncio.run(project_source.changed_sources(tmp_path)) == {}
