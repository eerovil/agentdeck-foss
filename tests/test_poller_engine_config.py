"""Slice 1 of #177: the Manifest/Overlay model, collect-all validation, and offline inspect."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentdeck.poller_engine import config
from agentdeck.poller_engine.config import (
    CONTRACT,
    DEFAULT_CORE_RESERVE,
    DEFAULT_GRAPHQL_RESERVE,
    DEFAULT_TERMINAL_RECHECK_S,
    Role,
)

_MANIFEST = f"""
contract = {CONTRACT}

[board]
org = "Northwind"
project_number = 2

[board.statuses]
in_progress = "In Progress"

[policy]
merge_method = "merge"
worker_cap = 4

[[repos]]
id = "store"
holds_issues = true
holds_code = true
verification = "agentdeck_job"
auto_deploys = true

[[repos]]
id = "issues"
holds_issues = true
workspace = "store"

[authorization]
work = ["eero"]

[capabilities]
release = true

[extensions.sentry]
org = "scandinavian-trailhead"
"""

_OVERLAY = f"""
contract = {CONTRACT}

[paths]
state = "/var/home/me/trailhead/.claude-kanban/state.json"
log = "/tmp/poller.log"
env_files = ["/var/home/me/trailhead/.env"]

[checkouts]
store = "/var/home/me/trailhead/web"

[worker]
account = "claude_code:main"

[credentials]
github_token_file = "/var/home/me/.config/poller/token"
"""


def _write(tmp_path, manifest=_MANIFEST, overlay=_OVERLAY):
    manifest_path = tmp_path / "poller.toml"
    overlay_path = tmp_path / "overlay.toml"
    manifest_path.write_text(manifest)
    overlay_path.write_text(overlay)
    return manifest_path, overlay_path


def _inspect(tmp_path, **kwargs):
    manifest_path, overlay_path = _write(tmp_path, **kwargs)
    return config.inspect(manifest_path, overlay_path, extension_validators={"sentry": _accept})


def _accept(block, problems):
    return None


def _locations(draft):
    return [problem.location for problem in draft.report.problems]


# --- the happy path ---

def test_a_good_pair_of_files_reports_nothing(tmp_path):
    draft = _inspect(tmp_path)
    assert draft.ok, draft.report.render()
    assert draft.report.problems == []


def test_the_manifest_names_its_world_semantically(tmp_path):
    manifest = _inspect(tmp_path).manifest
    assert manifest.board.org == "Northwind"
    assert manifest.board.project_number == 2
    # A role the board spells differently, and one it spells canonically.
    assert manifest.board.display_name(Role.IN_PROGRESS) == "In Progress"
    assert manifest.board.display_name(Role.IN_REVIEW) == "In review"


def test_one_repo_entry_states_every_role_it_plays(tmp_path):
    manifest = _inspect(tmp_path).manifest
    assert manifest.issue_repos() == ("store", "issues")
    assert manifest.repo("issues").workspace == "store"
    assert manifest.code_repos() == ("store",)
    assert manifest.auto_deploy_repos() == frozenset({"store"})
    assert manifest.repo("issues").holds_code is False


def test_the_typed_slots_are_carried_without_interpretation(tmp_path):
    manifest = _inspect(tmp_path).manifest
    # #108 gives these a shape and a place; #110/#113/#112 give them meaning.
    assert manifest.authorization == {"work": ["eero"]}
    assert manifest.capabilities == {"release": True}
    assert manifest.extensions["sentry"] == {"org": "scandinavian-trailhead"}


def test_a_project_names_the_template_titles_it_wants_repaired(tmp_path):
    """#338: a fact about this project's issue templates, so the Manifest states it."""
    manifest = _inspect(tmp_path).manifest
    assert manifest.placeholder_titles == ()  # naming none repairs none

    named = _MANIFEST.replace(
        "worker_cap = 4", 'worker_cap = 4\nplaceholder_titles = ["Requested changes"]'
    )
    draft = _inspect(tmp_path, manifest=named)
    assert draft.ok, draft.report.render()
    assert draft.manifest.placeholder_titles == ("Requested changes",)


def test_live_query_budget_policy_is_configurable_with_conservative_defaults(tmp_path):
    defaulted = _inspect(tmp_path).manifest
    assert defaulted.terminal_recheck_s == DEFAULT_TERMINAL_RECHECK_S == 900
    assert defaulted.graphql_reserve == DEFAULT_GRAPHQL_RESERVE == 200
    assert defaulted.core_reserve == DEFAULT_CORE_RESERVE == 500

    configured = _MANIFEST.replace(
        "worker_cap = 4",
        "worker_cap = 4\nterminal_recheck_s = 300\ngraphql_reserve = 500\ncore_reserve = 750",
    )
    manifest = _inspect(tmp_path, manifest=configured).manifest
    assert manifest.terminal_recheck_s == 300
    assert manifest.graphql_reserve == 500
    assert manifest.core_reserve == 750


def test_invalid_live_query_budget_policy_is_reported_and_defaulted(tmp_path):
    broken = _MANIFEST.replace(
        "worker_cap = 4",
        'worker_cap = 4\nterminal_recheck_s = 0\ngraphql_reserve = "many"\ncore_reserve = -1',
    )
    draft = _inspect(tmp_path, manifest=broken)
    assert "manifest.policy.terminal_recheck_s" in _locations(draft)
    assert "manifest.policy.graphql_reserve" in _locations(draft)
    assert "manifest.policy.core_reserve" in _locations(draft)
    assert draft.manifest.terminal_recheck_s == DEFAULT_TERMINAL_RECHECK_S
    assert draft.manifest.graphql_reserve == DEFAULT_GRAPHQL_RESERVE
    assert draft.manifest.core_reserve == DEFAULT_CORE_RESERVE


def test_worker_deploys_default_to_staging_and_a_project_may_widen_or_close_them(tmp_path):
    """#2656: staging deploys a requester asks for are allowed unless the project says not."""
    assert _inspect(tmp_path).manifest.worker_may_deploy == "staging"

    for level in ("none", "staging", "production"):
        named = _MANIFEST.replace(
            "worker_cap = 4", f'worker_cap = 4\nworker_may_deploy = "{level}"'
        )
        draft = _inspect(tmp_path, manifest=named)
        assert draft.ok, draft.report.render()
        assert draft.manifest.worker_may_deploy == level


@pytest.mark.parametrize("value", ['"Production"', '"prod"', "true", '""'])
def test_an_unknown_deploy_level_is_refused(tmp_path, value):
    broken = _MANIFEST.replace("worker_cap = 4", f"worker_cap = 4\nworker_may_deploy = {value}")
    draft = _inspect(tmp_path, manifest=broken)
    assert "manifest.policy.worker_may_deploy" in _locations(draft)
    assert not draft.ok


def test_worker_merges_default_to_on_request_and_a_project_may_close_them(tmp_path):
    """#2723: a merge a requester asks for is allowed unless the project says not."""
    assert _inspect(tmp_path).manifest.worker_may_merge == "on_request"

    for level in ("on_request", "never"):
        named = _MANIFEST.replace("worker_cap = 4", f'worker_cap = 4\nworker_may_merge = "{level}"')
        draft = _inspect(tmp_path, manifest=named)
        assert draft.ok, draft.report.render()
        assert draft.manifest.worker_may_merge == level


@pytest.mark.parametrize("value", ['"always"', '"On_request"', "true", '""'])
def test_an_unknown_merge_level_is_refused(tmp_path, value):
    broken = _MANIFEST.replace("worker_cap = 4", f"worker_cap = 4\nworker_may_merge = {value}")
    draft = _inspect(tmp_path, manifest=broken)
    assert "manifest.policy.worker_may_merge" in _locations(draft)
    assert not draft.ok


def test_proposal_ceilings_are_configurable_and_absent_by_default(tmp_path):
    """#371: an extension bounds what it asks for; the engine owns what it honors."""
    defaulted = _inspect(tmp_path).manifest
    # No ceiling of their own: proposals stay bounded by worker_cap like any other card.
    assert defaulted.proposal_daily_cap is None
    assert defaulted.proposal_worker_cap is None

    configured = _MANIFEST.replace(
        "worker_cap = 4", "worker_cap = 4\n\n[policy.proposals]\ndaily_cap = 15\nworker_cap = 3"
    )
    draft = _inspect(tmp_path, manifest=configured)
    assert draft.ok, draft.report.render()
    assert draft.manifest.proposal_daily_cap == 15
    assert draft.manifest.proposal_worker_cap == 3
    # The project's own worker_cap is untouched by the proposal-scoped one.
    assert draft.manifest.worker_cap == 4


def test_command_output_is_off_until_a_project_asks_for_it(tmp_path):
    """#2154: absent configuration means off, so a project that never asked keeps its workers'
    command output exactly as the provider shows it."""
    defaulted = _inspect(tmp_path).manifest.command_output
    assert not defaulted.enabled
    assert defaulted.wire(card="agentdeck#1", generation="g1")["mode"] == "off"

    configured = _MANIFEST + """
[command_output]
enabled = true
visible_tool_result_chars = 4000
slice_max_bytes = 4096
slice_call_limit = 3
no_archive_commands = ["vault read", "secret-tool"]
"""
    draft = _inspect(tmp_path, manifest=configured)
    assert draft.ok, draft.report.render()
    policy = draft.manifest.command_output
    assert policy.enabled
    assert policy.no_archive_commands == ("vault read", "secret-tool")
    # Everything not named keeps its default rather than being reset alongside.
    assert policy.retention_days == 30
    # The runtime's hooks still read the original wire shape, so an engine change never needs
    # a runtime restart to keep bounding output.
    assert policy.wire(card="agentdeck#1", generation="g1") == {
        "mode": "enforce",
        "visible_tool_result_chars": 4000,
        "artifact_retention_days": 30,
        "artifact_slice_max_bytes": 4096,
        "artifact_slice_call_limit": 3,
        "no_archive_commands": ["vault read", "secret-tool"],
        "card": "agentdeck#1",
        "generation": "g1",
    }


def test_invalid_command_output_settings_are_reported_and_defaulted(tmp_path):
    broken = _MANIFEST + """
[command_output]
enabled = "yes"
visible_tool_result_chars = 0
slice_max_bytes = 99999999
retention = 5
"""
    draft = _inspect(tmp_path, manifest=broken)
    locations = _locations(draft)

    assert "manifest.command_output.enabled" in locations
    assert "manifest.command_output.visible_tool_result_chars" in locations
    assert "manifest.command_output.slice_max_bytes" in locations
    # A misspelled key is reported rather than dropped: a project that thinks it bounded its
    # workers' output and did not is the case where carrying on quietly is wrong.
    assert "manifest.command_output.retention" in locations
    policy = draft.manifest.command_output
    assert not policy.enabled
    assert policy.visible_tool_result_chars == 8_000
    assert policy.slice_max_bytes == 64 * 1024


def test_a_command_output_block_that_is_not_a_table_is_reported(tmp_path):
    not_a_table = _MANIFEST.replace(
        f"contract = {CONTRACT}", f"contract = {CONTRACT}\ncommand_output = 3", 1
    )
    draft = _inspect(tmp_path, manifest=not_a_table)

    assert "manifest.command_output" in _locations(draft)
    assert not draft.manifest.command_output.enabled


def test_a_retired_poller_phases_table_is_refused(tmp_path):
    """#2154 retired it and #2196 dropped the reader. A leftover table is reported rather than
    ignored, because ignoring it would switch bounded command output off without a word."""
    draft = _inspect(tmp_path, manifest=_MANIFEST + '\n[poller_phases]\nmode = "enforce"\n')
    assert "manifest.poller_phases" in _locations(draft)
    assert not draft.manifest.command_output.enabled


def test_invalid_proposal_ceilings_are_reported_and_left_unset(tmp_path):
    broken = _MANIFEST.replace(
        "worker_cap = 4",
        'worker_cap = 4\n\n[policy.proposals]\ndaily_cap = 0\nworker_cap = "three"',
    )
    draft = _inspect(tmp_path, manifest=broken)
    assert "manifest.policy.proposals.daily_cap" in _locations(draft)
    assert "manifest.policy.proposals.worker_cap" in _locations(draft)
    # Unset rather than guessed: a ceiling nobody wrote is not one the engine invents.
    assert draft.manifest.proposal_daily_cap is None
    assert draft.manifest.proposal_worker_cap is None

    not_a_table = _MANIFEST.replace("worker_cap = 4", "worker_cap = 4\nproposals = 15")
    draft = _inspect(tmp_path, manifest=not_a_table)
    assert "manifest.policy.proposals" in _locations(draft)


_GUIDE_BLOCK = """
[comments.guide]
heading = "🤖 **Ohjaa Claudea uudella kommentilla**"
work = "**Vastaa tai pyydä muutoksia:** `/claude ...`"
stop = "**Pysäytä työ:** `/claude stop`"
merge = "**Hyväksy ja julkaise:** `/merge`"
merge_review = "**Tarkista ennen julkaisua:** `/merge review`"
"""


def _with_guide(*, capabilities="work_control = true\nguarded_release = true\nmerge_review = true",
                guide=_GUIDE_BLOCK) -> str:
    """The fixture manifest with its capability block replaced and a guide appended."""
    return _MANIFEST.replace("release = true", f"release = true\n{capabilities}") + guide


def test_a_project_writes_its_own_comment_guide_in_its_own_language(tmp_path):
    """#373: the wording and its language are facts about the project, not engine constants."""
    assert _inspect(tmp_path).manifest.comment_guide == ""  # naming none appends nothing

    draft = _inspect(tmp_path, manifest=_with_guide())
    assert draft.ok, draft.report.render()
    guide = draft.manifest.comment_guide
    # Folded under its heading (#2498), which GitHub shows without rendering its Markdown.
    assert guide.startswith(
        "---\n<details><summary>🤖 <b>Ohjaa Claudea uudella kommentilla</b></summary>\n\n- "
    )
    assert guide.endswith("\n\n</details>")
    assert "- **Pysäytä työ:** `/claude stop`" in guide
    assert "- **Tarkista ennen julkaisua:** `/merge review`" in guide
    # Rendered in a canonical order, not the order the table happened to be written in.
    assert guide.index("/claude stop") < guide.index("/merge")
    # Only what the project wrote: no control it left out is invented.
    assert "/shot" not in guide


def test_a_guide_without_a_heading_still_folds(tmp_path):
    draft = _inspect(tmp_path, manifest=_with_guide(
        guide='\n[comments.guide]\nstop = "**Pysäytä työ:** `/claude stop`"\n'))
    assert draft.ok, draft.report.render()
    assert draft.manifest.comment_guide == (
        "---\n<details><summary>Commands</summary>\n\n"
        "- **Pysäytä työ:** `/claude stop`\n\n</details>"
    )


def test_a_guide_line_for_a_disabled_capability_is_reported_not_dropped(tmp_path):
    """Silently omitting it would show an operator's own wording on a board that never has it."""
    draft = _inspect(tmp_path, manifest=_with_guide(
        capabilities="work_control = true\nguarded_release = true"))
    assert "manifest.comments.guide.merge_review" in _locations(draft)
    # Reported *and* withheld — the guide never advertises a control the project disabled.
    assert "/merge review" not in draft.manifest.comment_guide
    assert "/claude stop" in draft.manifest.comment_guide


def test_a_guide_naming_an_unknown_control_is_refused(tmp_path):
    draft = _inspect(tmp_path, manifest=_with_guide(
        guide=_GUIDE_BLOCK + 'deploy = "**Deploy:** `/deploy`"\n'))
    assert "manifest.comments.guide.deploy" in _locations(draft)


def test_a_malformed_guide_is_refused_rather_than_half_read(tmp_path):
    draft = _inspect(tmp_path, manifest=_with_guide(
        guide='\n[comments]\nguide = "just a string"\n'))
    assert "manifest.comments.guide" in _locations(draft)
    assert draft.manifest.comment_guide == ""

    blank = _inspect(tmp_path, manifest=_with_guide(
        guide=_GUIDE_BLOCK.replace('stop = "**Pysäytä työ:** `/claude stop`"', 'stop = "  "')))
    assert "manifest.comments.guide.stop" in _locations(blank)


def test_a_guide_with_only_a_heading_renders_nothing(tmp_path):
    draft = _inspect(tmp_path, manifest=_with_guide(
        capabilities="work_control = true", guide='\n[comments.guide]\nheading = "**Ohjaa**"\n'))
    # A heading over an empty list is a separator and a promise with nothing behind it.
    assert draft.manifest.comment_guide == ""


def test_a_malformed_placeholder_list_is_refused_rather_than_half_read(tmp_path):
    broken = _MANIFEST.replace("worker_cap = 4", 'worker_cap = 4\nplaceholder_titles = "one"')
    draft = _inspect(tmp_path, manifest=broken)
    assert "manifest.policy.placeholder_titles" in _locations(draft)
    assert draft.manifest.placeholder_titles == ()

    mixed = _MANIFEST.replace("worker_cap = 4", 'worker_cap = 4\nplaceholder_titles = ["ok", 7]')
    draft = _inspect(tmp_path, manifest=mixed)
    assert "manifest.policy.placeholder_titles" in _locations(draft)
    # Not "ok" alone: a list nobody checked is a worse answer than a complaint.
    assert draft.manifest.placeholder_titles == ()


def test_the_overlay_holds_only_secrets_and_paths(tmp_path):
    overlay = _inspect(tmp_path).overlay
    assert overlay.state_path.endswith("state.json")
    assert overlay.checkouts == {"store": "/var/home/me/trailhead/web"}
    assert overlay.account == "claude_code:main"
    assert overlay.credentials["github_token_file"].endswith("token")
    assert overlay.env_files == ("/var/home/me/trailhead/.env",)
    # A host that names one account still has a one-entry fallback order.
    assert overlay.accounts == ("claude_code:main",)
    # Naming no shared extensions folder is the default: only the checkouts are searched.
    assert overlay.extensions_dir == ""


def test_the_host_may_name_one_shared_extensions_folder(tmp_path):
    """#377: machine path, so the Overlay states it — not every project vendoring a copy."""
    _LOG = 'log = "/tmp/poller.log"'
    named = _OVERLAY.replace(_LOG, f'{_LOG}\nextensions = "~/agentdeck/poller/extensions"')
    draft = _inspect(tmp_path, overlay=named)
    assert draft.ok, draft.report.render()
    assert draft.overlay.extensions_dir.endswith("/agentdeck/poller/extensions")
    assert "~" not in draft.overlay.extensions_dir  # expanded on the host that named it

    broken = _OVERLAY.replace(_LOG, f"{_LOG}\nextensions = 7")
    assert "overlay.paths.extensions" in _locations(_inspect(tmp_path, overlay=broken))


def test_the_host_supplies_an_extensions_credential_without_restating_its_settings(tmp_path):
    """#386: an extension's schema is portable; the credential in it cannot be."""
    overlay = _OVERLAY + '\n[extensions.sentry]\ntoken_file = "~/.config/sentry-token"\n'
    draft = _inspect(tmp_path, overlay=overlay)
    assert draft.ok, draft.report.render()

    merged = config.effective_extensions(draft.manifest, draft.overlay)
    # The Manifest's own setting survives, and the host's is merged in beside it.
    assert merged["sentry"]["org"] == "scandinavian-trailhead"
    assert merged["sentry"]["token_file"].endswith("/.config/sentry-token")
    # The Manifest itself is untouched, so it stays portable and its hash stays stable.
    assert "token_file" not in draft.manifest.extensions["sentry"]


def test_the_host_may_override_a_setting_but_not_enable_an_extension(tmp_path):
    overriding = _OVERLAY + '\n[extensions.sentry]\norg = "a-different-org"\n'
    draft = _inspect(tmp_path, overlay=overriding)
    assert draft.ok, draft.report.render()
    # Per key, host wins: the machine's answer is the operative one where it has an opinion.
    assert config.effective_extensions(draft.manifest, draft.overlay)["sentry"]["org"] == (
        "a-different-org"
    )

    # Enabling is the project's decision — a host block for an extension the Manifest never
    # enabled would switch something on that nobody reading the Manifest can see.
    unknown = _OVERLAY + '\n[extensions.made-up]\nservice = "x.service"\n'
    draft = _inspect(tmp_path, overlay=unknown)
    assert "overlay.extensions.made-up" in _locations(draft)
    assert "made-up" not in config.effective_extensions(draft.manifest, draft.overlay)

    # A retired extension's leftover host block (#3741) switches nothing on, so it is ignored:
    # the Manifest that dropped it still applies over an Overlay not yet cleaned.
    leftover = _OVERLAY + '\n[extensions.chatgpt-nudge]\nstate_file = "~/s.json"\n'
    draft = _inspect(tmp_path, overlay=leftover)
    assert draft.ok, draft.report.render()
    assert "chatgpt-nudge" not in config.effective_extensions(draft.manifest, draft.overlay)


def test_an_extension_validates_the_settings_it_will_actually_run_with(tmp_path):
    """A required credential the host supplies must not read as missing to the extension."""
    seen: dict = {}

    def capture(block, problems):
        seen.update(block)

    manifest_path, overlay_path = _write(
        tmp_path,
        overlay=_OVERLAY + '\n[extensions.sentry]\ntoken_file = "~/tok"\n',
    )
    config.inspect(manifest_path, overlay_path, extension_validators={"sentry": capture})
    assert seen.get("org") == "scandinavian-trailhead"
    assert "token_file" in seen


def test_a_malformed_overlay_extensions_block_is_refused(tmp_path):
    # Before any table header, or TOML reads it as a key of the last table instead.
    not_a_table = _OVERLAY.replace("[paths]", "extensions = 7\n\n[paths]", 1)
    draft = _inspect(tmp_path, overlay=not_a_table)
    assert "overlay.extensions" in _locations(draft)
    assert draft.overlay.extensions == {}

    draft = _inspect(tmp_path, overlay=_OVERLAY + '\n[extensions]\nsentry = "not a table"\n')
    assert "overlay.extensions.sentry" in _locations(draft)


# --- the hook allow-list is Manifest policy (#2158) ---

_WITH_HOOKS = _MANIFEST.replace(
    '[extensions.sentry]\n', '[extensions.sentry]\nhooks = ["observe", "health"]\n', 1
)


def test_the_hook_allow_list_is_lifted_out_of_the_extensions_settings(tmp_path):
    """The engine enforces it; the extension never sees it as one of its own settings."""
    draft = _inspect(tmp_path, manifest=_WITH_HOOKS)
    assert draft.ok, draft.report.render()
    assert draft.manifest.extension_hooks == {"sentry": ("health", "observe")}
    assert config.effective_extensions(draft.manifest, draft.overlay)["sentry"] == {
        "org": "scandinavian-trailhead"
    }
    # A block stating none is simply absent: the wake decides what that means, not the parser.
    assert _inspect(tmp_path).manifest.extension_hooks == {}


@pytest.mark.parametrize(
    "value, wrong",
    [
        ('["observe", "teleport"]', "'teleport', which is not a hook"),
        ('["Health"]', "'Health', which is not a hook"),
        ("[]", "allows no hooks"),
        ('"observe"', "not a list of hook names"),
        ("[1]", "not a list of hook names"),
    ],
)
def test_a_hook_list_the_engine_cannot_read_is_a_config_problem(tmp_path, value, wrong):
    manifest = _MANIFEST.replace(
        "[extensions.sentry]\n", f"[extensions.sentry]\nhooks = {value}\n", 1
    )
    draft = _inspect(tmp_path, manifest=manifest)
    location = "manifest.extensions.sentry.hooks"
    problems = [p for p in draft.report.problems if p.location == location]
    assert problems and wrong in problems[0].what
    assert "sentry" not in draft.manifest.extension_hooks


def test_a_host_cannot_widen_an_extensions_hooks(tmp_path):
    overlay = _OVERLAY + '\n[extensions.sentry]\nhooks = ["observe", "propose"]\n'
    draft = _inspect(tmp_path, manifest=_WITH_HOOKS, overlay=overlay)
    assert "overlay.extensions.sentry.hooks" in _locations(draft)


def test_changing_the_hook_list_is_a_policy_change_that_needs_an_apply(tmp_path):
    """The Manifest fingerprint covers the list, so widening it cannot take effect silently."""
    manifest_path, overlay_path, binding_path = _activate(tmp_path, manifest=_WITH_HOOKS)
    assert config.load(manifest_path, overlay_path, binding_path)
    manifest_path.write_text(_WITH_HOOKS.replace('"observe", "health"', '"observe", "propose"'))
    with pytest.raises(config.DriftError) as refused:
        config.load(manifest_path, overlay_path, binding_path)
    assert "manifest changed" in refused.value.report.render()


def test_the_fallback_order_always_starts_with_the_primary_account(tmp_path):
    """The order is what the engine falls back through when an account fills up. The primary
    leads it whether or not the host repeated it, so naming a fallback can never drop the
    account the host just chose."""
    overlay = _inspect(
        tmp_path,
        overlay=_OVERLAY.replace(
            'account = "claude_code:main"',
            'account = "claude_code:main"\n'
            'fallback_accounts = ["claude_code:alt", "claude_code:main"]\n'
            'usage_cache = "/run/user/1000/agentdeck"',
        ),
    ).overlay
    assert overlay.accounts == ("claude_code:main", "claude_code:alt")  # deduped, order kept
    assert overlay.usage_cache == "/run/user/1000/agentdeck"


def test_a_deck_may_leave_unassigned_issues_to_another(tmp_path):
    """#3731: ``[claims] unassigned = false`` needs no manifest switch since #3732, because no
    deck's Poller runs cards any more; only a value that is not a boolean is refused."""
    claims = _OVERLAY + "\n[claims]\nunassigned = false\n"
    assert _inspect(tmp_path).overlay.claims_unassigned is True
    accepted = _inspect(tmp_path, overlay=claims)
    assert accepted.overlay.claims_unassigned is False
    assert "overlay.claims.unassigned" not in _locations(accepted)
    wrong = _inspect(tmp_path, overlay=claims.replace("false", '"no"'))
    assert "overlay.claims.unassigned" in _locations(wrong)


def test_each_account_can_name_its_own_usage_ceiling(tmp_path):
    overlay = _inspect(
        tmp_path,
        overlay=_OVERLAY.replace(
            'account = "claude_code:main"',
            'account = "claude_code:main"\n'
            'fallback_accounts = ["codex:codex", "claude_code:alt"]\n'
            'usage_ceilings = { "claude_code:main" = 50, "codex:codex" = 90, '
            '"claude_code:alt" = 90 }',
        ),
    ).overlay

    assert overlay.accounts == (
        "claude_code:main",
        "codex:codex",
        "claude_code:alt",
    )
    assert overlay.usage_ceilings == {
        "claude_code:main": 50.0,
        "codex:codex": 90.0,
        "claude_code:alt": 90.0,
    }


def test_usage_ceilings_must_name_offered_accounts_and_valid_percentages(tmp_path):
    draft = _inspect(
        tmp_path,
        overlay=_OVERLAY.replace(
            'account = "claude_code:main"',
            'account = "claude_code:main"\n'
            'usage_ceilings = { "codex:absent" = 101 }',
        ),
    )

    assert not draft.ok
    assert any("usage_ceilings" in problem.location for problem in draft.report.problems)


def test_each_account_can_name_how_full_its_window_should_end_up(tmp_path):
    """#1965. Accounts left out keep the engine default, so an empty table is the norm."""
    overlay = _inspect(
        tmp_path,
        overlay=_OVERLAY.replace(
            'account = "claude_code:main"',
            'account = "claude_code:main"\n'
            'fallback_accounts = ["claude_code:alt"]\n'
            'usage_targets = { "claude_code:alt" = 99 }',
        ),
    ).overlay

    assert overlay.usage_targets == {"claude_code:alt": 99.0}
    assert _inspect(tmp_path).overlay.usage_targets == {}


def test_usage_targets_must_name_offered_accounts_and_valid_percentages(tmp_path):
    draft = _inspect(
        tmp_path,
        overlay=_OVERLAY.replace(
            'account = "claude_code:main"',
            'account = "claude_code:main"\nusage_targets = { "codex:absent" = 140 }',
        ),
    )

    assert not draft.ok
    assert any("usage_targets" in problem.location for problem in draft.report.problems)


def test_a_fallback_list_that_is_not_account_keys_is_a_problem(tmp_path):
    draft = _inspect(
        tmp_path,
        overlay=_OVERLAY.replace(
            'account = "claude_code:main"',
            'account = "claude_code:main"\nfallback_accounts = "claude_code:alt"',
        ),
    )
    assert not draft.ok
    assert any("fallback_accounts" in problem.location for problem in draft.report.problems)


# --- missing and unreadable files ---

def test_a_missing_file_is_a_problem_not_an_exception(tmp_path):
    draft = config.inspect(tmp_path / "nope.toml", tmp_path / "also-nope.toml")
    assert not draft.ok
    assert _locations(draft) == ["manifest", "overlay"]
    assert all("file not found" in problem.what for problem in draft.report.problems)


def test_invalid_toml_is_reported_with_its_syntax_error(tmp_path):
    draft = _inspect(tmp_path, manifest="contract = = 1")
    assert not draft.ok
    assert "manifest" in _locations(draft)
    assert any("not valid TOML" in problem.what for problem in draft.report.problems)


# --- the contract rule ---

def test_an_older_contract_refuses_and_points_at_the_migration(tmp_path):
    draft = _inspect(tmp_path, manifest=_MANIFEST.replace(
        f"contract = {CONTRACT}", f"contract = {CONTRACT - 1}", 1
    ))
    problem = next(p for p in draft.report.problems if p.location == "manifest.contract")
    assert "older than" in problem.what
    assert "Builder migration" in problem.fix


def test_a_newer_contract_is_also_refused_never_interpreted(tmp_path):
    draft = _inspect(tmp_path, manifest=_MANIFEST.replace(
        f"contract = {CONTRACT}", f"contract = {CONTRACT + 1}", 1
    ))
    problem = next(p for p in draft.report.problems if p.location == "manifest.contract")
    assert "newer than" in problem.what


def test_a_missing_contract_is_reported(tmp_path):
    draft = _inspect(tmp_path, manifest=_MANIFEST.replace(f"contract = {CONTRACT}\n", "", 1))
    assert "manifest.contract" in _locations(draft)


def test_the_two_files_must_share_one_contract(tmp_path):
    draft = _inspect(tmp_path, overlay=_OVERLAY.replace(
        f"contract = {CONTRACT}", f"contract = {CONTRACT + 1}", 1
    ))
    locations = _locations(draft)
    assert "overlay.contract" in locations
    assert any("does not match the manifest" in p.what for p in draft.report.problems)


# --- collect-all, not first-failure ---

def test_every_problem_is_reported_in_one_pass(tmp_path):
    broken = """
contract = 999

[board]
project_number = 0

[policy]
merge_method = "fast-forward"
worker_cap = 0

[[repos]]
id = "store"

[[repos]]
id = "store"
holds_issues = true
"""
    draft = _inspect(tmp_path, manifest=broken)
    locations = _locations(draft)
    # One run surfaces the contract, the board, the policy and both repo mistakes together.
    assert "manifest.contract" in locations
    assert "manifest.board.org" in locations
    assert "manifest.board.project_number" in locations
    assert "manifest.policy.merge_method" in locations
    assert "manifest.policy.worker_cap" in locations
    assert "manifest.repos[0]" in locations
    assert "manifest.repos[1].id" in locations
    assert len(draft.report) >= 7


def test_every_problem_carries_a_location_and_a_fix(tmp_path):
    draft = _inspect(tmp_path, manifest=_MANIFEST.replace('id = "store"', 'id = ""', 1))
    assert not draft.ok
    for problem in draft.report.problems:
        assert problem.location and problem.what and problem.fix
        assert str(problem).startswith(problem.location)


def test_the_report_renders_as_one_message(tmp_path):
    draft = config.inspect(tmp_path / "nope.toml", tmp_path / "nope2.toml")
    rendered = draft.report.render()
    assert rendered.count("\n") == 1
    assert "manifest" in rendered and "overlay" in rendered


# --- board and repo shapes ---

def test_an_unknown_status_role_is_refused(tmp_path):
    draft = _inspect(tmp_path, manifest=_MANIFEST.replace(
        'in_progress = "In Progress"', 'shipping = "Shipping"', 1
    ))
    assert "manifest.board.statuses.shipping" in _locations(draft)


def test_a_repo_with_no_role_is_pointless_and_says_so(tmp_path):
    draft = _inspect(tmp_path, manifest=_MANIFEST.replace(
        '[[repos]]\nid = "issues"\nholds_issues = true', '[[repos]]\nid = "issues"', 1
    ))
    assert any("no role" in problem.what for problem in draft.report.problems)


def test_auto_deploy_without_code_is_refused(tmp_path):
    draft = _inspect(tmp_path, manifest=_MANIFEST.replace(
        '[[repos]]\nid = "issues"\nholds_issues = true',
        '[[repos]]\nid = "issues"\nholds_issues = true\nauto_deploys = true',
        1,
    ))
    assert "manifest.repos[1].auto_deploys" in _locations(draft)


def test_a_missing_repo_list_is_reported(tmp_path):
    bare = f"contract = {CONTRACT}\n[board]\norg = 'x'\nproject_number = 1\n"
    draft = _inspect(tmp_path, manifest=bare)
    assert "manifest.repos" in _locations(draft)


# --- cross-referencing the two files ---

def test_a_code_repo_without_a_checkout_is_caught(tmp_path):
    draft = _inspect(tmp_path, overlay=_OVERLAY.replace(
        'store = "/var/home/me/trailhead/web"', "", 1
    ))
    problem = next(p for p in draft.report.problems if p.location == "overlay.checkouts.store")
    assert "holds code but has no checkout path" in problem.what
    assert "store" in problem.fix


def test_an_issue_only_repo_uses_its_declared_code_workspace(tmp_path):
    draft = _inspect(tmp_path)
    assert "overlay.checkouts.issues" not in _locations(draft)
    assert config.worker_checkouts(draft.manifest, draft.overlay) == {
        "store": "/var/home/me/trailhead/web",
        "issues": "/var/home/me/trailhead/web",
    }
    assert config.borrowed_workspaces(draft.manifest) == {"issues": "store"}


def test_an_issue_only_repo_without_a_workspace_is_caught(tmp_path):
    draft = _inspect(tmp_path, manifest=_MANIFEST.replace('workspace = "store"', "", 1))
    problem = next(
        p for p in draft.report.problems
        if p.location == "manifest.repos.issues.workspace"
    )
    assert "holds issues but neither code nor a workspace" in problem.what


def test_a_workspace_must_name_a_code_repo(tmp_path):
    draft = _inspect(
        tmp_path,
        manifest=_MANIFEST.replace('workspace = "store"', 'workspace = "ghost"', 1),
    )
    assert "manifest.repos.issues.workspace" in _locations(draft)

    draft = _inspect(
        tmp_path,
        manifest=_MANIFEST.replace('workspace = "store"', 'workspace = "issues"', 1),
    )
    problem = next(
        p for p in draft.report.problems
        if p.location == "manifest.repos.issues.workspace"
    )
    assert "does not hold code" in problem.what


def test_a_code_repo_cannot_borrow_another_workspace(tmp_path):
    manifest = _MANIFEST.replace("holds_code = true", 'holds_code = true\nworkspace = "issues"', 1)
    draft = _inspect(tmp_path, manifest=manifest)
    assert "manifest.repos.store.workspace" in _locations(draft)


def test_an_issue_only_repo_cannot_smuggle_a_checkout_into_the_overlay(tmp_path):
    overlay = _OVERLAY.replace(
        'store = "/var/home/me/trailhead/web"',
        'store = "/var/home/me/trailhead/web"\nissues = "/var/home/me/trailhead/web"',
        1,
    )
    draft = _inspect(tmp_path, overlay=overlay)
    problem = next(p for p in draft.report.problems if p.location == "overlay.checkouts.issues")
    assert "holds neither code nor artifacts" in problem.what


def test_two_code_repos_cannot_claim_the_same_checkout(tmp_path):
    manifest = _MANIFEST.replace(
        '[[repos]]\nid = "issues"\nholds_issues = true\nworkspace = "store"',
        '[[repos]]\nid = "issues"\nholds_issues = true\nholds_code = true',
        1,
    )
    overlay = _OVERLAY.replace(
        'store = "/var/home/me/trailhead/web"',
        'store = "/var/home/me/trailhead/web"\nissues = "/var/home/me/trailhead/web"',
        1,
    )
    draft = _inspect(tmp_path, manifest=manifest, overlay=overlay)
    problem = next(p for p in draft.report.problems if p.location == "overlay.checkouts.issues")
    assert "same directory as 'store'" in problem.what


def _with_fork(manifest=_MANIFEST, overlay=_OVERLAY):
    """The shape #1578 needs: a second repository that holds code and no issues."""
    return (
        manifest + '\n[[repos]]\nid = "fork"\nholds_issues = false\nholds_code = true\n'
        'verification = "agentdeck_job"\n',
        overlay.replace(
            'store = "/var/home/me/trailhead/web"',
            'store = "/var/home/me/trailhead/web"\nfork = "/var/home/me/trailhead/fork"',
            1,
        ),
    )


def test_a_repo_may_hold_code_without_holding_issues(tmp_path):
    """#1578: an owned fork whose cards must live in another repository's tracker."""
    manifest, overlay = _with_fork()
    draft = _inspect(tmp_path, manifest=manifest, overlay=overlay)
    assert draft.ok, draft.report.render()
    assert draft.manifest.code_repos() == ("store", "fork")
    # It plays no other role: no card is ever filed there, and nothing deploys it.
    assert draft.manifest.issue_repos() == ("store", "issues")
    assert draft.manifest.auto_deploy_repos() == frozenset({"store"})


def test_a_code_only_repo_is_named_to_the_worker_but_never_worked_in(tmp_path):
    """#1578: the brief has to say the fork exists; no card may be dispatched into it."""
    manifest, overlay = _with_fork()
    draft = _inspect(tmp_path, manifest=manifest, overlay=overlay)
    assert config.code_checkouts(draft.manifest, draft.overlay) == {
        "store": "/var/home/me/trailhead/web",
        "fork": "/var/home/me/trailhead/fork",
    }
    assert config.worker_checkouts(draft.manifest, draft.overlay) == {
        "store": "/var/home/me/trailhead/web",
        "issues": "/var/home/me/trailhead/web",
    }


def test_a_code_only_repo_still_needs_a_checkout_on_this_host(tmp_path):
    manifest, _ = _with_fork()
    draft = _inspect(tmp_path, manifest=manifest)
    problem = next(p for p in draft.report.problems if p.location == "overlay.checkouts.fork")
    assert "holds code but has no checkout path" in problem.what


def _with_evidence_repo(manifest=_MANIFEST, overlay=_OVERLAY, entry=None):
    """The shape #1791 needs: a repository that exists only to hold screenshot evidence."""
    return (
        manifest + (entry or '\n[[repos]]\nid = "shots"\nholds_artifacts = true\n'),
        overlay.replace(
            'store = "/var/home/me/trailhead/web"',
            'store = "/var/home/me/trailhead/web"\nshots = "/var/home/me/trailhead/shots"',
            1,
        ),
    )


def test_a_repo_may_exist_only_to_hold_screenshot_evidence(tmp_path):
    """#1791: holding artifacts is a role of its own, so the entry is not roleless."""
    manifest, overlay = _with_evidence_repo()
    draft = _inspect(tmp_path, manifest=manifest, overlay=overlay)
    assert draft.ok, draft.report.render()
    assert draft.manifest.artifact_repo() == "shots"
    # It plays no other role: no card is filed there, no code is worked there, nothing deploys.
    assert draft.manifest.issue_repos() == ("store", "issues")
    assert draft.manifest.code_repos() == ("store",)
    assert config.code_checkouts(draft.manifest, draft.overlay) == {
        "store": "/var/home/me/trailhead/web",
    }


def test_the_evidence_repo_is_named_to_the_worker_with_its_owner_and_its_clone(tmp_path):
    """The brief needs both: a name to embed from, and a directory to push from."""
    manifest, overlay = _with_evidence_repo()
    draft = _inspect(tmp_path, manifest=manifest, overlay=overlay)
    assert config.artifact_evidence(draft.manifest, draft.overlay) == (
        "Northwind/shots",
        "/var/home/me/trailhead/shots",
    )


def test_a_project_that_named_no_evidence_repo_keeps_committing_screenshots_as_it_did(tmp_path):
    draft = _inspect(tmp_path)
    assert draft.ok, draft.report.render()
    assert draft.manifest.artifact_repo() == ""
    assert config.artifact_evidence(draft.manifest, draft.overlay) == ("", "")


def test_an_evidence_repo_this_host_has_not_cloned_goes_dormant_rather_than_refusing(tmp_path):
    """#1791: a Manifest names it; only this host's Overlay can say where it was cloned.

    Promotion publishes the committed Manifest and never the machine-local Overlay, so the merge
    that names an evidence repository necessarily lands before any host has the path. Refusing
    would take every one of that project's wakes down for the gap; dormant hands the worker the
    brief it always got and lets the path arrive whenever the clone does.
    """
    manifest, _ = _with_evidence_repo()
    draft = _inspect(tmp_path, manifest=manifest)
    assert draft.ok, draft.report.render()
    assert draft.manifest.artifact_repo() == "shots"
    # Both halves or neither: a name this host cannot push to is not somewhere to file evidence.
    assert config.artifact_evidence(draft.manifest, draft.overlay) == ("", "")


def test_an_evidence_repo_never_borrows_the_checkout_its_cards_are_worked_in(tmp_path):
    """A `workspace` says where this repo's cards are worked, never where its images go.

    Trailhead's shape exactly: `issues` holds the cards *and* the evidence, and borrows `store`'s
    checkout to work them in. Committing screenshots into that borrowed clone would push them
    straight back into the code repository this whole role exists to keep them out of, so an
    evidence repo with no clone of its own stays dormant even when it has a workspace.
    """
    manifest = _MANIFEST.replace(
        '[[repos]]\nid = "issues"\nholds_issues = true\nworkspace = "store"',
        '[[repos]]\nid = "issues"\nholds_issues = true\nholds_artifacts = true\n'
        'workspace = "store"',
        1,
    )
    draft = _inspect(tmp_path, manifest=manifest)
    assert draft.ok, draft.report.render()
    assert config.artifact_evidence(draft.manifest, draft.overlay) == ("", "")
    assert config.worker_checkouts(draft.manifest, draft.overlay)["issues"] == (
        "/var/home/me/trailhead/web"
    )


def test_an_issue_repo_may_also_hold_the_evidence(tmp_path):
    """#1791: Trailhead's issues-only repository is where its screenshots should go."""
    manifest = _MANIFEST.replace(
        '[[repos]]\nid = "issues"\nholds_issues = true\nworkspace = "store"',
        '[[repos]]\nid = "issues"\nholds_issues = true\nholds_artifacts = true\n'
        'workspace = "store"',
        1,
    )
    overlay = _OVERLAY.replace(
        'store = "/var/home/me/trailhead/web"',
        'store = "/var/home/me/trailhead/web"\nissues = "/var/home/me/trailhead/issues"',
        1,
    )
    draft = _inspect(tmp_path, manifest=manifest, overlay=overlay)
    assert draft.ok, draft.report.render()
    # Its own clone is where evidence is pushed; its cards are still worked in store's.
    assert config.artifact_evidence(draft.manifest, draft.overlay) == (
        "Northwind/issues",
        "/var/home/me/trailhead/issues",
    )
    assert config.worker_checkouts(draft.manifest, draft.overlay)["issues"] == (
        "/var/home/me/trailhead/web"
    )


def test_two_evidence_repos_are_refused_rather_than_one_being_picked(tmp_path):
    manifest, overlay = _with_evidence_repo(
        entry='\n[[repos]]\nid = "shots"\nholds_artifacts = true\n'
        '\n[[repos]]\nid = "more-shots"\nholds_artifacts = true\n'
    )
    overlay = overlay.replace(
        'shots = "/var/home/me/trailhead/shots"',
        'shots = "/var/home/me/trailhead/shots"\nmore-shots = "/var/home/me/trailhead/more"',
        1,
    )
    draft = _inspect(tmp_path, manifest=manifest, overlay=overlay)
    problem = next(p for p in draft.report.problems if p.location == "manifest.repos")
    assert "more than one repository holds artifacts" in problem.what


def test_a_checkout_for_an_unknown_repo_is_caught(tmp_path):
    draft = _inspect(tmp_path, overlay=_OVERLAY.replace(
        "[checkouts]", '[checkouts]\nghost = "/tmp/ghost"', 1
    ))
    assert "overlay.checkouts.ghost" in _locations(draft)


def test_a_missing_state_path_is_caught(tmp_path):
    draft = _inspect(tmp_path, overlay=_OVERLAY.replace(
        'state = "/var/home/me/trailhead/.claude-kanban/state.json"', "", 1
    ))
    assert "overlay.paths.state" in _locations(draft)


# --- the extension delegation seam ---

def test_each_extension_validates_its_own_block(tmp_path):
    seen = {}

    def sentry(block, problems):
        seen.update(block)
        problems.append(config.Problem("manifest.extensions.sentry.org", "unknown org", "fix it"))

    manifest_path, overlay_path = _write(tmp_path)
    draft = config.inspect(manifest_path, overlay_path, extension_validators={"sentry": sentry})
    assert seen == {"org": "scandinavian-trailhead"}
    # The extension's complaint lands in the same single report.
    assert "manifest.extensions.sentry.org" in _locations(draft)


def test_the_core_loader_knows_nothing_about_extension_settings(tmp_path):
    # An extension block the core cannot interpret is fine as long as its extension can.
    manifest = _MANIFEST.replace(
        "[extensions.sentry]\norg = \"scandinavian-trailhead\"",
        "[extensions.sentry]\nwhatever = 42\nnested = { a = 1 }",
        1,
    )
    manifest_path, overlay_path = _write(tmp_path, manifest=manifest)
    draft = config.inspect(manifest_path, overlay_path, extension_validators={"sentry": _accept})
    assert draft.ok, draft.report.render()


def test_an_extension_block_with_no_installed_extension_is_refused(tmp_path):
    manifest_path, overlay_path = _write(tmp_path)
    draft = config.inspect(manifest_path, overlay_path, extension_validators={})
    problem = next(p for p in draft.report.problems if p.location == "manifest.extensions.sentry")
    assert "is installed" in problem.what


def test_an_extension_block_that_is_not_a_table_is_refused(tmp_path):
    # A top-level key has to precede every table, so this is written at the top.
    draft = _inspect(tmp_path, manifest="extensions = 5\n" + _MANIFEST.replace(
        '[extensions.sentry]\norg = "scandinavian-trailhead"', "", 1
    ))
    assert "manifest.extensions" in _locations(draft)


def test_a_single_extension_block_that_is_not_a_table_is_refused(tmp_path):
    draft = _inspect(tmp_path, manifest=_MANIFEST.replace(
        '[extensions.sentry]\norg = "scandinavian-trailhead"', "[extensions]\nsentry = 5", 1
    ))
    assert "manifest.extensions.sentry" in _locations(draft)


# --- stage 2: resolve at activation ---

_DEFAULT = object()


class FakeDiscovery:
    """A recording read-only Discovery: the board's ids and columns, and repo reachability."""

    def __init__(self, *, board=_DEFAULT, columns=None, repos=None):
        self._board = board if board is not _DEFAULT else config.BoardSnapshot(
            project_id="PVT_kwabc",
            status_field_id="PVTSSF_lakbc",
            status_options=dict(columns) if columns is not None else {
                "Backlog": "f75ad846",
                "Todo": "61e4505c",
                "In Progress": "47fc9ee4",
                "In review": "df73e18b",
                "Done": "98236657",
                "Needs More Info": "5923bc46",
                "Blocked": "7d32c486",
            },
        )
        self._repos = dict(repos or {})
        self.board_calls = 0
        self.repo_calls = []

    def board(self, org, project_number):
        self.board_calls += 1
        return self._board

    def repo_check(self, org, repo_id):
        self.repo_calls.append(repo_id)
        found = self._repos.get(repo_id, config.Reachability.REACHABLE)
        return found if isinstance(found, config.RepoCheck) else config.RepoCheck(found)


def _resolve(tmp_path, discovery=None, **kwargs):
    manifest_path, overlay_path = _write(tmp_path, **kwargs)
    return config.resolve(
        manifest_path,
        overlay_path,
        discovery or FakeDiscovery(),
        extension_validators={"sentry": _accept},
    )


def test_the_discovery_protocol_is_satisfied_by_the_fake():
    assert isinstance(FakeDiscovery(), config.Discovery)


def test_resolving_freezes_the_board_ids_and_a_fingerprint(tmp_path):
    resolved, report = _resolve(tmp_path)
    assert report.ok, report.render()
    assert resolved.binding.project_id == "PVT_kwabc"
    assert resolved.binding.status_field_id == "PVTSSF_lakbc"
    # The board's own spelling is matched case-insensitively onto the role.
    assert resolved.option_id(Role.IN_PROGRESS) == "47fc9ee4"
    assert resolved.option_id(Role.DONE) == "98236657"
    assert resolved.binding.fingerprint["digest"]
    assert resolved.checkout("store") == "/var/home/me/trailhead/web"


def test_a_broken_manifest_is_never_taken_to_github(tmp_path):
    discovery = FakeDiscovery()
    resolved, report = _resolve(tmp_path, discovery, manifest="contract = 0")
    assert resolved is None
    assert not report.ok
    assert discovery.board_calls == 0


def test_an_unreadable_board_fails_closed(tmp_path):
    resolved, report = _resolve(tmp_path, FakeDiscovery(board=None))
    assert resolved is None
    problem = next(p for p in report.problems if p.location == "board")
    assert "could not be read" in problem.what


def test_a_missing_column_for_a_required_role_is_refused(tmp_path):
    columns = {"Todo": "1", "In Progress": "2", "In review": "3", "Done": "4",
               "Needs More Info": "5"}
    resolved, report = _resolve(tmp_path, FakeDiscovery(columns=columns))
    assert resolved is None
    assert "board.statuses.Blocked" in [p.location for p in report.problems]


def test_a_board_without_a_backlog_column_still_activates(tmp_path):
    columns = {"Todo": "1", "In Progress": "2", "In review": "3", "Done": "4",
               "Needs More Info": "5", "Blocked": "6"}
    resolved, report = _resolve(tmp_path, FakeDiscovery(columns=columns))
    assert report.ok, report.render()
    assert resolved.option_id(Role.BACKLOG) == ""
    # Human review pending (#1465) is optional the same way, and the empty binding is what tells
    # a worker's finish to park in Needs More Info instead of writing an unbound option.
    assert resolved.option_id(Role.HUMAN_REVIEW_PENDING) == ""


def test_a_board_that_made_the_human_review_column_binds_it(tmp_path):
    columns = {"Todo": "1", "In Progress": "2", "In review": "3", "Done": "4",
               "Needs More Info": "5", "Blocked": "6", "human review pending": "7"}
    resolved, report = _resolve(tmp_path, FakeDiscovery(columns=columns))
    assert report.ok, report.render()
    assert resolved.option_id(Role.HUMAN_REVIEW_PENDING) == "7"


def test_an_unreachable_repo_is_refused(tmp_path):
    resolved, report = _resolve(
        tmp_path, FakeDiscovery(repos={"issues": config.Reachability.ABSENT})
    )
    assert resolved is None
    problem = next(p for p in report.problems if p.location == "manifest.repos.issues")
    assert "does not exist or is not visible" in problem.what


def test_a_repo_these_credentials_may_not_see_asks_for_a_grant(tmp_path):
    resolved, report = _resolve(
        tmp_path,
        FakeDiscovery(
            repos={
                "store": config.RepoCheck(
                    config.Reachability.DENIED, "Resource not accessible by integration"
                )
            }
        ),
    )
    assert resolved is None
    problem = next(p for p in report.problems if p.location == "manifest.repos.store")
    assert "could not be checked with these credentials" in problem.what
    assert "Resource not accessible by integration" in problem.what
    assert "grant access" in problem.fix


def test_a_repo_github_could_not_answer_for_says_so_instead_of_blaming_credentials(tmp_path):
    """#707: a rate-limited read used to send the reader hunting a permissions problem."""
    resolved, report = _resolve(
        tmp_path,
        FakeDiscovery(
            repos={
                "store": config.RepoCheck(
                    config.Reachability.UNAVAILABLE, "API rate limit exceeded for user ID 1"
                )
            }
        ),
    )
    assert resolved is None  # still fails closed; only the diagnosis changed
    problem = next(p for p in report.problems if p.location == "manifest.repos.store")
    assert "GitHub could not answer right now" in problem.what
    assert "API rate limit exceeded" in problem.what
    assert "credentials" not in problem.what
    assert "grant" not in problem.fix
    assert "transient" in problem.fix
    assert "read the detail" in problem.fix


def test_every_repo_is_checked_once(tmp_path):
    discovery = FakeDiscovery()
    _resolve(tmp_path, discovery)
    assert discovery.repo_calls == ["store", "issues"]


# --- the binding artifact ---

def test_a_binding_round_trips_through_its_json_file(tmp_path):
    resolved, _ = _resolve(tmp_path)
    binding_path = tmp_path / "binding.json"
    config.write_binding(binding_path, resolved.binding)
    again = config.read_binding(binding_path)
    assert again == resolved.binding
    # Engine-written, never hand-authored: it is plain JSON with no secrets in it.
    text = binding_path.read_text()
    assert "PVT_kwabc" in text and "token" not in text


def test_a_new_binding_records_no_extension_code(tmp_path):
    """Activation approves policy, not one build of an extension (#2158): a fresh binding
    carries no code hash for anything, so a later code fix has nothing to disagree with."""
    manifest_path, overlay_path = _write(tmp_path)
    resolved, report = config.resolve(
        manifest_path, overlay_path, FakeDiscovery(), extension_validators={"sentry": _accept},
    )
    assert report.ok, report.render()
    assert resolved.binding.as_json()["extension_pins"] == {}

    binding_path = tmp_path / "binding.json"
    config.write_binding(binding_path, resolved.binding)
    assert "code_hash" not in binding_path.read_text()


def test_a_missing_or_corrupt_binding_reads_as_none(tmp_path):
    assert config.read_binding(tmp_path / "nope.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert config.read_binding(broken) is None


# --- stage 3: the per-cycle load ---

def _activate(tmp_path, **kwargs):
    """Activate, and publish the binding the way an apply does."""
    manifest_path, overlay_path = _write(tmp_path, **kwargs)
    resolved, report = config.resolve(
        manifest_path, overlay_path, FakeDiscovery(), extension_validators={"sentry": _accept},
    )
    assert report.ok, report.render()
    binding_path = tmp_path / "binding.json"
    config.write_binding(binding_path, resolved.binding)
    return manifest_path, overlay_path, binding_path


def test_a_wake_loads_what_was_activated_without_re_discovering_anything(tmp_path):
    manifest_path, overlay_path, binding_path = _activate(tmp_path)
    loaded = config.load(manifest_path, overlay_path, binding_path)
    assert loaded.option_id(Role.IN_REVIEW) == "df73e18b"
    assert loaded.manifest.auto_deploy_repos() == frozenset({"store"})
    assert loaded.org == "Northwind"


def test_a_manifest_installed_before_verification_was_required_still_loads(
    tmp_path, monkeypatch
):
    """#2832 review: a deploy must not stop an instance whose installed Manifest predates the
    rule. Only activation asks for the line; a wake reads the old file as ``agentdeck_job``."""
    old = _MANIFEST.replace('verification = "agentdeck_job"\n', "", 1)
    with monkeypatch.context() as patch:
        patch.setattr(config, "_verification_named", lambda raw, report: None)  # older engine
        manifest_path, overlay_path, binding_path = _activate(tmp_path, manifest=old)

    loaded = config.load(manifest_path, overlay_path, binding_path)
    assert loaded.manifest.repo("store").verification == "agentdeck_job"
    assert not config.inspect(manifest_path, overlay_path).ok, "re-activation still asks"


def test_an_edited_manifest_fails_closed_as_drift(tmp_path):
    manifest_path, overlay_path, binding_path = _activate(tmp_path)
    manifest_path.write_text(_MANIFEST.replace("project_number = 2", "project_number = 3", 1))
    try:
        config.load(manifest_path, overlay_path, binding_path)
    except config.DriftError as exc:
        assert "manifest" in exc.report.render()
    else:
        raise AssertionError("an edited manifest must not load")


def test_a_tampered_binding_fails_closed_as_drift(tmp_path):
    manifest_path, overlay_path, binding_path = _activate(tmp_path)
    binding_path.write_text(
        binding_path.read_text().replace("47fc9ee4", "deadbeef")
    )
    try:
        config.load(manifest_path, overlay_path, binding_path)
    except config.DriftError as exc:
        assert "binding" in exc.report.render()
    else:
        raise AssertionError("a tampered binding must not load")


def test_a_binding_from_before_extensions_were_pinned_drifts_rather_than_crashes(tmp_path):
    """An instance activated by an older engine has no pins. It must ask for re-activation —
    the one remedy that mints them — not raise, and not run on an activation that pinned
    nothing (#269)."""
    manifest_path, overlay_path, binding_path = _activate(tmp_path)
    stored = json.loads(binding_path.read_text())
    old_derived = {
        key: stored[key] for key in ("project_id", "status_field_id", "status_option_ids")
    }
    stored.pop("extension_pins")
    stored["fingerprint"] = config.fingerprint_of(
        manifest_path, old_derived, contract=config.CONTRACT
    )
    binding_path.write_text(json.dumps(stored))

    try:
        config.load(manifest_path, overlay_path, binding_path)
    except config.DriftError as exc:
        assert "re-activate" in exc.report.render()
    else:
        raise AssertionError("a binding that pinned no extension code must not load")


def _restamp(binding_path, manifest_path, raw):
    """Write ``raw`` with a fingerprint that is honest about its own derived facts."""
    derived = {
        key: raw[key]
        for key in ("project_id", "status_field_id", "status_option_ids", "extension_pins")
    }
    raw["fingerprint"] = config.fingerprint_of(manifest_path, derived, contract=config.CONTRACT)
    binding_path.write_text(json.dumps(raw))


def test_a_binding_with_pre_2158_extension_pins_is_drift(tmp_path):
    """Pins stopped granting hooks in #2196, so a binding that still has them was never applied
    under explicit Manifest lists: it must be applied again, not run with its pins dropped."""
    manifest_path, overlay_path, binding_path = _activate(tmp_path)
    raw = json.loads(binding_path.read_text())
    raw["extension_pins"] = {"sentry": {"code_hash": "abc123", "hooks": ["observe", "health"]}}
    _restamp(binding_path, manifest_path, raw)

    with pytest.raises(config.DriftError) as refused:
        config.load(manifest_path, overlay_path, binding_path)
    assert "binding.extension_pins" in refused.value.report.render()


@pytest.mark.parametrize(
    "pins", [{"sentry": {"hooks": ["observe", "release-gate"]}}, ["observe"], "sentry", None]
)
def test_editing_extension_pins_without_restamping_is_drift(tmp_path, pins):
    """#2213 review: the pins grant nothing any more, but they are still a fingerprinted fact,
    so a hand edit — whatever shape it takes — must change the digest and fail closed."""
    manifest_path, overlay_path, binding_path = _activate(tmp_path)
    raw = json.loads(binding_path.read_text())
    raw["extension_pins"] = pins
    binding_path.write_text(json.dumps(raw))

    with pytest.raises(config.DriftError) as refused:
        config.load(manifest_path, overlay_path, binding_path)
    assert "binding.fingerprint" in refused.value.report.render()
    assert "binding changed" in refused.value.report.render()


def test_an_engine_code_change_alone_never_invalidates_a_binding(tmp_path):
    """Activation approves the project, not one engine build (#2153)."""
    manifest_path, overlay_path, binding_path = _activate(tmp_path)
    fingerprint = json.loads(binding_path.read_text())["fingerprint"]
    assert set(fingerprint) == {"contract", "manifest", "binding", "digest"}
    assert config.load(manifest_path, overlay_path, binding_path).binding.fingerprint == (
        fingerprint
    )


def test_a_pre_2153_engine_bound_binding_is_drift_and_left_as_it_was(tmp_path):
    """The engine-free migration is gone (#2196): an engine part is a changed fingerprint."""
    manifest_path, overlay_path, binding_path = _activate(tmp_path)
    raw = json.loads(binding_path.read_text())
    parts = {part: raw["fingerprint"][part] for part in ("contract", "manifest", "binding")}
    parts["engine"] = "0f1e2d3c4b5a"
    raw["fingerprint"] = {**parts, "digest": config._digest(parts)}
    binding_path.write_text(json.dumps(raw))
    before = binding_path.read_text()

    with pytest.raises(config.DriftError):
        config.load(manifest_path, overlay_path, binding_path)
    assert binding_path.read_text() == before
    assert "engine_free_fingerprint" not in before


def test_a_rotated_credential_does_not_require_re_activation(tmp_path):
    manifest_path, overlay_path, binding_path = _activate(tmp_path)
    overlay_path.write_text(
        _OVERLAY.replace("/var/home/me/.config/poller/token", "/var/home/me/.config/new-token")
    )
    loaded = config.load(manifest_path, overlay_path, binding_path)
    # The Overlay is host-local and deliberately not fingerprinted.
    assert loaded.overlay.credentials["github_token_file"].endswith("new-token")


def test_an_overlay_that_breaks_an_invariant_still_fails_closed(tmp_path):
    manifest_path, overlay_path, binding_path = _activate(tmp_path)
    overlay_path.write_text(_OVERLAY.replace('store = "/var/home/me/trailhead/web"', "", 1))
    try:
        config.load(manifest_path, overlay_path, binding_path)
    except config.ConfigError as exc:
        assert "overlay.checkouts.store" in exc.report.render()
    else:
        raise AssertionError("a code repo with no checkout must not load")


def test_a_missing_binding_says_to_activate(tmp_path):
    manifest_path, overlay_path = _write(tmp_path)
    try:
        config.load(manifest_path, overlay_path, tmp_path / "absent.json")
    except config.ConfigError as exc:
        assert "activate" in exc.report.render()
    else:
        raise AssertionError("loading without a binding must fail closed")


def test_unreadable_files_raise_with_their_report(tmp_path):
    try:
        config.load(tmp_path / "a.toml", tmp_path / "b.toml", tmp_path / "c.json")
    except config.ConfigError as exc:
        assert len(exc.report) == 2
    else:
        raise AssertionError("missing files must fail closed")


# --- host paths resolve once, here (#183) ---

def test_a_tilde_in_an_overlay_path_is_expanded(tmp_path):
    """The Overlay is hand-written, so `~` is the natural way to name a home-relative path.
    Taken literally it creates a directory called `~` under whatever the working directory
    happens to be — which is where a real activation put its state, its receipts and its
    activation record before this."""
    overlay = _OVERLAY.replace(
        'state = "/var/home/me/trailhead/.claude-kanban/state.json"',
        'state = "~/poller/state.json"',
    ).replace('log = "/tmp/poller.log"', 'log = "~/poller/cycle.log"').replace(
        'store = "/var/home/me/trailhead/web"', 'store = "~/web"'
    )
    parsed = _inspect(tmp_path, overlay=overlay).overlay
    home = str(Path.home())
    assert parsed.state_path == f"{home}/poller/state.json"
    assert parsed.log_path == f"{home}/poller/cycle.log"
    assert parsed.checkouts["store"] == f"{home}/web"
    assert "~" not in parsed.state_path


def test_a_relative_overlay_path_is_refused(tmp_path):
    """A relative path resolves against the working directory, so the same instance would use
    one state file when an operator runs a wake by hand and another when systemd runs it — two
    histories for one project, with nothing to show it happened."""
    draft = _inspect(
        tmp_path,
        overlay=_OVERLAY.replace(
            'state = "/var/home/me/trailhead/.claude-kanban/state.json"',
            'state = "state.json"',
        ),
    )
    assert not draft.ok
    problem = next(p for p in draft.report.problems if p.location == "overlay.paths.state")
    assert "depends on the working directory" in problem.what


# --- the ownership label is the project's choice, not the engine's (#108) ---

def _board(tmp_path, line: str = ""):
    """A minimal valid manifest/overlay pair, with an optional extra [board] line."""
    manifest = tmp_path / "poller.toml"
    manifest.write_text(
        "contract = 1\n\n"
        "[board]\norg = \"acme\"\nproject_number = 4\n" + (line + "\n" if line else "") +
        "\n[board.statuses]\ntodo = \"Todo\"\nin_progress = \"In progress\"\n"
        "in_review = \"In review\"\ndone = \"Done\"\n"
        "needs_more_info = \"Needs More Info\"\nblocked = \"Blocked\"\n\n"
        "[[repos]]\nid = \"app\"\nholds_issues = true\nholds_code = true\n"
        "verification = \"agentdeck_job\"\n"
    )
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        f"contract = 1\n\n[paths]\nstate = \"{tmp_path}/state.json\"\n"
        f"\n[checkouts]\napp = \"{tmp_path}\"\n"
        f"\n[credentials]\ngithub_token_file = \"{tmp_path}/token\"\n"
        f"\n[worker]\naccounts = [\"claude_code:main\"]\n"
    )
    (tmp_path / "token").write_text("t")
    return config.inspect(manifest, overlay)


def test_a_project_that_never_names_a_label_keeps_the_old_behaviour(tmp_path):
    """Defaulted rather than required, so adding the field needs no contract bump."""
    draft = _board(tmp_path)
    assert draft.ok, draft.report.render()
    assert draft.manifest.board.ownership_label == config.DEFAULT_OWNERSHIP_LABEL == "claude"


def test_a_project_may_name_its_own_ownership_label(tmp_path):
    draft = _board(tmp_path, 'ownership_label = "agent"')
    assert draft.ok, draft.report.render()
    assert draft.manifest.board.ownership_label == "agent"


def test_a_project_may_accept_ownership_aliases_without_changing_its_canonical_label(tmp_path):
    draft = _board(tmp_path, 'ownership_label = ["agent", "claude"]')
    assert draft.ok, draft.report.render()
    assert draft.manifest.board.ownership_label == "agent"
    assert draft.manifest.board.accepted_ownership_labels == ("agent", "claude")


@pytest.mark.parametrize(
    "value",
    ('ownership_label = []', 'ownership_label = ["agent", 7]',
     'ownership_label = ["agent", "agent"]',
     'ownership_label = ["agent", "Agent"]'),
)
def test_invalid_ownership_alias_lists_are_refused(tmp_path, value):
    draft = _board(tmp_path, value)
    assert not draft.ok
    assert "manifest.board.ownership_label" in [p.location for p in draft.report.problems]


def test_an_empty_ownership_label_is_refused_rather_than_silently_defaulted(tmp_path):
    """A blank label would match every card's absent label; failing closed beats guessing."""
    draft = _board(tmp_path, 'ownership_label = "   "')
    assert not draft.ok
    assert "manifest.board.ownership_label" in [p.location for p in draft.report.problems]


def test_a_non_string_ownership_label_is_refused(tmp_path):
    draft = _board(tmp_path, "ownership_label = 7")
    assert not draft.ok
    assert "manifest.board.ownership_label" in [p.location for p in draft.report.problems]


# --- the retired automerge label (#2502) ---

def _label_board(tmp_path, label_line="", capabilities=("guarded_release", "merge_review")):
    """A minimal project whose board carries an extra label line."""
    manifest = tmp_path / "poller.toml"
    caps = "".join(f"{name} = true\n" for name in capabilities)
    manifest.write_text(
        'contract = 1\n\n[board]\norg = "acme"\nproject_number = 4\n'
        + (label_line + "\n" if label_line else "")
        + '\n[board.statuses]\ntodo = "Todo"\nin_progress = "In progress"\n'
        'in_review = "In review"\ndone = "Done"\n'
        'needs_more_info = "Needs More Info"\nblocked = "Blocked"\n\n'
        '[[repos]]\nid = "app"\nholds_issues = true\nholds_code = true\n'
        'verification = "agentdeck_job"\n\n'
        "[capabilities]\n" + caps
    )
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        f'contract = 1\n\n[paths]\nstate = "{tmp_path}/state.json"\n'
        f'\n[checkouts]\napp = "{tmp_path}"\n'
        f'\n[credentials]\ngithub_token_file = "{tmp_path}/token"\n'
        f'\n[worker]\naccounts = ["claude_code:main"]\n'
    )
    (tmp_path / "token").write_text("t")
    return config.inspect(manifest, overlay)


@pytest.mark.parametrize(
    "line",
    [
        'automerge_label = "automerge"',
        'automerge_label = "claude"',
        "automerge_label = 7",
    ],
)
def test_an_installed_manifest_naming_automerge_still_loads_and_names_nothing(tmp_path, line):
    """The key is ignored, not refused: refusing would halt the instance until re-applied."""
    draft = _label_board(tmp_path, line, capabilities=("guarded_release",))
    assert draft.ok, draft.report.render()
    assert not hasattr(draft.manifest.board, "automerge_label")


def test_a_project_may_name_a_distinct_spec_only_label(tmp_path):
    draft = _board(tmp_path, 'spec_only_label = "spec-only"')
    assert draft.ok, draft.report.render()
    assert draft.manifest.board.spec_only_label == "spec-only"


def test_spec_only_may_not_reuse_another_control_label(tmp_path):
    draft = _board(
        tmp_path,
        'ownership_label = ["agent", "claude"]\nspec_only_label = "CLAUDE"',
    )
    assert not draft.ok
    assert draft.manifest.board.spec_only_label == ""


# --- pull request closing-keyword policy (#1734) ---


def test_a_repo_can_declare_that_it_prohibits_closing_keywords(tmp_path):
    """#1734: two repos, two answers, resolved from the Manifest before any dispatch."""
    manifest = _MANIFEST.replace(
        '[[repos]]\nid = "store"\nholds_issues = true\nholds_code = true',
        '[[repos]]\nid = "store"\nholds_issues = true\nholds_code = true\n'
        'closing_keywords = "prohibited"',
        1,
    )
    draft = _inspect(tmp_path, manifest=manifest)

    assert draft.ok, draft.report.render()
    assert config.closing_keyword_policies(draft.manifest) == {"store": "prohibited"}
    assert draft.manifest.repo("store").prohibits_closing_keywords


def test_an_unstated_closing_keyword_policy_is_the_one_every_project_already_had(tmp_path):
    draft = _inspect(tmp_path)

    assert draft.ok, draft.report.render()
    assert config.closing_keyword_policies(draft.manifest) == {"store": "required"}
    assert not draft.manifest.repo("store").prohibits_closing_keywords
    # Issue-only repositories are absent: no pull request is ever opened in one.
    assert "issues" not in config.closing_keyword_policies(draft.manifest)


def test_an_unreadable_closing_keyword_policy_fails_closed(tmp_path):
    """#1734: a typo must not be the reason a worker is told to write a banned keyword."""
    manifest = _MANIFEST.replace(
        'id = "store"\nholds_issues = true',
        'id = "store"\nclosing_keywords = "forbidden"\nholds_issues = true',
        1,
    )
    draft = _inspect(tmp_path, manifest=manifest)

    assert not draft.ok
    assert "manifest.repos[0].closing_keywords" in _locations(draft)
    # And the value it fell back to is the safe one, not the demanding one.
    assert draft.manifest.repo("store").prohibits_closing_keywords


def test_an_issue_only_repo_cannot_declare_a_pull_request_body_policy(tmp_path):
    """#1734: it would read as applying and silently do nothing — the PR lands elsewhere."""
    manifest = _MANIFEST.replace(
        'id = "issues"\nholds_issues = true\nworkspace = "store"',
        'id = "issues"\nholds_issues = true\nworkspace = "store"\n'
        'closing_keywords = "prohibited"',
        1,
    )
    draft = _inspect(tmp_path, manifest=manifest)

    assert not draft.ok
    assert "manifest.repos[1].closing_keywords" in _locations(draft)


def test_a_project_may_name_a_paused_label(tmp_path):
    draft = _board(tmp_path, 'paused_label = " paused "')
    assert draft.ok, draft.report.render()
    assert draft.manifest.board.paused_label == "paused"
    assert _board(tmp_path, "").manifest.board.paused_label == ""


@pytest.mark.parametrize(
    "line",
    [
        'ownership_label = ["agent", "claude"]\npaused_label = "Agent"',
        'spec_only_label = "spec-only"\npaused_label = "SPEC-ONLY"',
        "paused_label = 3",
    ],
)
def test_the_paused_label_may_not_reuse_another_control_label(tmp_path, line):
    draft = _board(tmp_path, line)
    assert not draft.ok
    assert draft.manifest.board.paused_label == ""
