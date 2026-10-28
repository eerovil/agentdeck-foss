"""#183: the Reference Poller Project's committed configuration, and the canary scenario.

The live half of #183 — a real board, a real activation, a real worker — is an operator action.
What is checkable here is that the example a fresh operator copies is complete and honest, and
that the acceptance scenario covers what #115 said it must.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agentdeck.poller_engine import config
from agentdeck.poller_engine.config import CONTRACT, Role

REPO = Path(__file__).resolve().parent.parent
REFERENCE = REPO / "poller" / "reference"
MANIFEST = REFERENCE / "poller.toml"
OVERLAY_TEMPLATE = REFERENCE / "overlay.example.toml"


def _raw(path: Path) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _filled(tmp_path, *, project_number=7) -> tuple[Path, Path]:
    """The committed example with its one placeholder filled, and a usable Overlay beside it."""
    manifest = tmp_path / "poller.toml"
    manifest.write_text(
        MANIFEST.read_text().replace("project_number = 0", f"project_number = {project_number}")
    )
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        OVERLAY_TEMPLATE.read_text().replace("~/", f"{tmp_path}/")
    )
    return manifest, overlay


# --- the committed example ---

def test_the_reference_manifest_is_on_the_engines_contract():
    assert _raw(MANIFEST)["contract"] == CONTRACT
    assert _raw(OVERLAY_TEMPLATE)["contract"] == CONTRACT


def _values(node) -> list[str]:
    """Every string value in the parsed file: what is configured, not what is explained."""
    if isinstance(node, dict):
        return [value for child in node.values() for value in _values(child)]
    if isinstance(node, list):
        return [value for child in node for value in _values(child)]
    return [node] if isinstance(node, str) else []


def test_the_reference_manifest_holds_no_secrets_paths_or_node_ids():
    # Checked over the configured values rather than the file text: a comment that mentions a
    # token is documentation, while a token in a value would be the mistake this split prevents.
    for value in _values(_raw(MANIFEST)):
        assert not value.startswith(("PVT_", "PVTSSF_")), value
        assert "/home/" not in value and not value.startswith("~/"), value
        assert "ghp_" not in value and "github_pat" not in value, value


def test_the_board_is_named_semantically_and_by_role():
    board = _raw(MANIFEST)["board"]
    assert board["org"] == "eerovil"
    roles = set(board["statuses"])
    # Every role the engine requires is named; Backlog is optional and absent here.
    for role in config.REQUIRED_ROLES:
        assert role.value.lower().replace(" ", "_") in roles, role
    assert "backlog" not in roles


def test_the_board_number_is_an_obvious_placeholder_that_fails_closed(tmp_path):
    assert _raw(MANIFEST)["board"]["project_number"] == 0
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(OVERLAY_TEMPLATE.read_text().replace("~/", f"{tmp_path}/"))
    draft = config.inspect(MANIFEST, overlay)
    # Refusing is the point: a poller must never point at a board nobody chose.
    assert not draft.ok
    assert "manifest.board.project_number" in [p.location for p in draft.report.problems]


def test_the_reference_project_validates_offline_once_its_board_is_known(tmp_path):
    manifest, overlay = _filled(tmp_path)
    draft = config.inspect(
        manifest,
        overlay,
        extension_validators={
            name: (lambda block, problems: None)
            for name in ("screenshots",)
        },
    )
    assert draft.ok, draft.report.render()


def test_the_repo_states_every_role_it_plays(tmp_path):
    manifest, overlay = _filled(tmp_path)
    parsed = config.inspect(manifest, overlay, extension_validators={
        name: (lambda block, problems: None)
        for name in ("screenshots",)
    }).manifest
    repo = parsed.repo("agentdeck")
    assert repo.holds_issues and repo.holds_code and repo.auto_deploys
    assert parsed.auto_deploy_repos() == frozenset({"agentdeck"})


def test_the_full_preset_is_enabled_because_that_is_what_the_example_demonstrates():
    capabilities = _raw(MANIFEST)["capabilities"]
    for capability in (
        "work_control",
        "guarded_release",
        "merge_review",
        "deployment_observation",
        "artifacts",
    ):
        assert capabilities[capability] is True, capability


def test_the_enabled_extensions_are_the_ones_this_project_can_actually_run():
    extensions = _raw(MANIFEST)["extensions"]
    assert set(extensions) == {"screenshots"}
    # sentry-autofix needs an organization and token this project does not have, and the file
    # says so rather than shipping a block that could never validate.
    assert "sentry-autofix" in MANIFEST.read_text()


def test_the_authorization_slot_shows_the_shape():
    authorization = _raw(MANIFEST)["authorization"]
    assert authorization["work"]["role"] == "triage"
    assert authorization["release"]["role"] == "maintain"


def test_the_reference_project_names_no_bot_and_says_why():
    """`bot` denies that account every tier, including approving an activation. This project's
    engine speaks with its single operator's own credential, so naming them would leave nobody
    able to approve, and naming a fictitious account would make the rule look enforced while the
    engine still posts as a human. Unset is the honest state — but only if the file says so,
    because a silently absent bot is indistinguishable from an oversight."""
    authorization = _raw(MANIFEST)["authorization"]
    assert "bot" not in authorization
    text = MANIFEST.read_text()
    assert "bot" in text, "the slot must still be documented even though it is unset"
    assert "account of its own" in text, "the file must say what would make setting it real"


def test_an_operator_of_this_project_can_actually_approve_its_activation():
    """The gate has to be passable by someone. With no bot named, the repository's own Release
    role decides — which is the whole point of stage four."""
    from agentdeck.poller_engine import authorization as auth

    policy = auth.parse_policy(_raw(MANIFEST)["authorization"])
    assert auth.holds(policy, auth.Tier.RELEASE, login="eerovil", permission="admin")
    # A drive-by with no role still cannot approve.
    assert not auth.holds(policy, auth.Tier.RELEASE, login="stranger", permission="read")


def test_the_overlay_template_asks_only_for_secrets_and_paths():
    overlay = _raw(OVERLAY_TEMPLATE)
    assert set(overlay) <= {"contract", "paths", "checkouts", "worker", "credentials", "extensions"}
    assert not overlay.get("extensions")
    # No GitHub credential at all: the engine speaks as its owner's authority (#2289), and no
    # token is ever inline in a config.
    assert not any("token" in key for key in overlay.get("credentials", {}))


def test_the_readme_states_both_prerequisites_this_host_lacks():
    text = (REFERENCE / "README.md").read_text()
    assert "read:project" in text
    assert "project_number" in text
    # And that the first write only happens after the one human gate.
    assert "single human gate" in text


# --- the board-creation helper (#183) ---

@pytest.fixture(autouse=True)
def _board_credential_is_stated(monkeypatch):
    """The board helper refuses to run on the host's own login (#2289)."""
    monkeypatch.setenv("GH_TOKEN", "test-board-token")


def _board_script():
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import create_poller_board

    return create_poller_board


def test_the_board_helper_asks_for_every_status_role_the_engine_moves_through():
    module = _board_script()
    wanted = {name.casefold() for name in module.REQUIRED_OPTIONS}
    for role in config.REQUIRED_ROLES:
        assert role.value.casefold() in wanted, role
    # Backlog is optional, because a board without an intake column still works.
    assert Role.BACKLOG.value in module.OPTIONAL_OPTIONS
    assert Role.BACKLOG.value.casefold() not in wanted


def test_the_helper_adds_only_what_is_missing_and_never_renames_a_column():
    module = _board_script()
    field = {
        "id": "F_1",
        "options": [
            {"name": "Todo", "color": "BLUE"},
            {"name": "In Progress", "color": "YELLOW"},  # the board's own spelling
            {"name": "Done", "color": "GREEN"},
            {"name": "Icebox", "color": "GRAY"},          # something the operator added
        ],
    }
    options, additions = module.ensure_options(
        field, wanted=module.REQUIRED_OPTIONS, keep_optional=False
    )
    names = [option["name"] for option in options]
    # Existing columns survive exactly as they are — the Manifest maps a spelling onto a role, so
    # "In Progress" must not be rewritten to "In progress", and a column the engine does not know
    # about must not be dropped.
    assert names[:4] == ["Todo", "In Progress", "Done", "Icebox"]
    assert set(additions) == {"In review", "Needs More Info", "Blocked"}


def test_a_complete_board_needs_no_changes():
    module = _board_script()
    field = {
        "id": "F_1",
        "options": [{"name": name, "color": "GRAY"} for name in module.REQUIRED_OPTIONS],
    }
    _options, additions = module.ensure_options(
        field, wanted=module.REQUIRED_OPTIONS, keep_optional=False
    )
    assert additions == []


def test_a_dry_run_reports_what_it_would_do_without_touching_anything(capsys):
    module = _board_script()
    code = module.main(["--owner", "nobody-at-all", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "would create a board" in out


class _FakeBoard:
    """A board behind the four `gh` calls the helper makes, recording every write.

    The helper's whole job is to leave a used board better than it found it, so the fake models
    the one behaviour that makes that hard: replacing the Status option set clears every card's
    Status, exactly as `updateProjectV2Field` does on real GitHub.
    """

    def __init__(self, options, assigned):
        self.options = [dict(option) for option in options]
        self.assigned = dict(assigned)
        self.created = []
        self.option_writes = 0

    def __call__(self, *args, stdin=None):
        if args[:2] == ("project", "view"):
            return 0, __import__("json").dumps(
                {"number": int(args[2]), "url": f"https://example/{args[2]}"}
            ), ""
        if args[:2] == ("project", "create"):
            self.created.append(args)
            return 1, "", "should not create"
        if args[:2] == ("project", "field-list"):
            # Real `gh` returns each option's id alongside its name; restoring a card needs it.
            options = [
                {"id": f"o{i}", **option} for i, option in enumerate(self.options)
            ]
            return 0, __import__("json").dumps(
                {"fields": [{"name": "Status", "id": "F_1", "options": options}]}
            ), ""
        if args[:2] == ("api", "graphql"):
            return self._graphql(args, stdin)
        raise AssertionError(f"unexpected gh call: {args}")

    def _graphql(self, args, stdin):
        import json as _json

        body = _json.loads(stdin) if stdin else {"query": args[3].split("=", 1)[1]}
        query = body.get("query", "")
        if "items(first:100)" in query:  # read_assignments
            return 0, _json.dumps({"data": {"user": {"projectV2": {
                "id": "P_1",
                "field": {"id": "F_1", "options": [
                    {"id": f"o{i}", "name": option["name"]}
                    for i, option in enumerate(self.options)
                ]},
                "items": {"nodes": [
                    {"id": item, "fieldValueByName": {"name": name}}
                    for item, name in self.assigned.items()
                ]},
            }}}}), ""
        if "updateProjectV2Field" in query:  # the destructive replacement
            self.option_writes += 1
            self.options = [dict(option) for option in body["variables"]["o"]]
            self.assigned = dict.fromkeys(self.assigned, None)  # GitHub clears every card
            return 0, "{}", ""
        if "updateProjectV2ItemFieldValue" in query:  # putting a card back
            fields = dict(
                arg.split("=", 1) for arg in args if isinstance(arg, str) and "=" in arg
            )
            index = int(fields["o"].removeprefix("o"))
            self.assigned[fields["i"]] = self.options[index]["name"]
            return 0, "{}", ""
        raise AssertionError(f"unexpected graphql: {query[:60]}")


def test_completing_a_board_by_number_never_creates_a_second_one(
    monkeypatch, capsys, tmp_path
):
    """A board is identified by the number the Manifest stores. Matching on title instead means
    renaming the board in the UI silently makes a duplicate on the next run."""
    module = _board_script()
    board = _FakeBoard(
        [{"name": "Todo"}, {"name": "In Progress"}, {"name": "Done"}],
        {"card-1": "Todo"},
    )
    monkeypatch.setattr(module, "_gh", board)
    monkeypatch.chdir(tmp_path)

    # The title deliberately does not match the board's real name.
    code = module.main(["--owner", "eerovil", "--project-number", "1"])

    assert code == 0
    assert board.created == []
    assert "completing board #1" in capsys.readouterr().out


def test_adding_a_column_puts_every_card_back_where_it_was(monkeypatch, capsys, tmp_path):
    """Replacing the option set clears every card's Status, so the helper must save the
    assignments first and restore them — otherwise adding a column silently empties the board."""
    module = _board_script()
    board = _FakeBoard(
        [{"name": "Todo"}, {"name": "In Progress"}, {"name": "Done"}],
        {"card-1": "Todo", "card-2": "In Progress"},
    )
    monkeypatch.setattr(module, "_gh", board)
    monkeypatch.chdir(tmp_path)  # the saved-status record lands here, not in the repo

    code = module.main(["--owner", "eerovil", "--project-number", "1"])
    out = capsys.readouterr().out

    assert code == 0
    assert board.option_writes == 1
    assert "added: In review, Needs More Info, Blocked" in out
    # Every card is back on the column it was on, by name — not merely "not crashed".
    assert board.assigned == {"card-1": "Todo", "card-2": "In Progress"}
    assert "restored 2 card status(es)" in out


def test_an_unreadable_board_is_never_written_to(monkeypatch, capsys):
    """If the current assignments cannot be read, writing would destroy data that could not be
    put back. Refuse instead — the fail-closed rule the whole engine is built on."""
    module = _board_script()
    board = _FakeBoard([{"name": "Todo"}], {"card-1": "Todo"})

    def unreadable(*args, stdin=None):
        if args[:2] == ("api", "graphql") and stdin and "items(first:100)" in stdin:
            return 1, "", "boom"
        return board(*args, stdin=stdin)

    monkeypatch.setattr(module, "_gh", unreadable)

    code = module.main(["--owner", "eerovil", "--project-number", "1"])

    assert code == 1
    assert board.option_writes == 0  # nothing was written
    assert "refusing rather than writing blind" in capsys.readouterr().err


def test_the_reference_project_names_the_label_it_owns_cards_with():
    """This project owns cards with `agent` or `claude`, preserving the former as canonical.

    The value matters less than the fact that the file states it: a copy-me example that relied
    on engine defaults would teach the next project that these labels are not theirs to choose.
    """
    board = _raw(MANIFEST)["board"]
    assert board["ownership_label"] == ["agent", "claude"]
    assert board["spec_only_label"] == "spec-only"
    text = MANIFEST.read_text()
    # And that extending a live project's ownership is additive, with stable write behavior.
    assert "aliases without abandoning old cards" in text
    assert "first entry remains canonical" in text


def test_the_reference_project_names_the_template_title_it_repairs(tmp_path):
    """#338: this project's issue template pre-fills `Requested changes`, so the Manifest
    says so and the engine can put a real title on a card filed without one."""
    manifest, overlay = _filled(tmp_path)
    loaded = config.inspect(manifest, overlay, extension_validators={
        name: (lambda block, problems: None)
        for name in ("screenshots",)
    })
    assert loaded.ok, loaded.report.render()
    assert loaded.manifest.placeholder_titles == ("Requested changes",)

    template = REPO / ".github" / "ISSUE_TEMPLATE" / "issue-for-agent-to-fix.md"
    # The value is only right while it is what the template actually writes.
    assert "title: Requested changes" in template.read_text()


def test_the_board_helper_refuses_without_an_explicit_credential(monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)

    assert _board_script().main(["--owner", "acmecorp", "--dry-run"]) == 2
    assert "never used" in capsys.readouterr().err
