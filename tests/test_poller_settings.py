"""#901: editing a Poller Instance's worker accounts without losing its Overlay."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentdeck import poller_settings

_NOW = datetime(2026, 8, 9, 10, 30, tzinfo=UTC)

OVERLAY = """\
# The host Overlay. Machine-local: credentials, accounts and paths only.

contract = 1

[paths]
state = "~/.local/share/agentdeck/poller/instances/reference/state.json"

[checkouts]
reference = "~/reference"

[worker]
# Switched main -> alt on 2026-08-07 because main's 5-hour window hit 100%.
account = "claude_code:main"

# Where the work goes when that account fills up, in order.
# codex:codex removed 2026-08-07: it is a silent black hole.
fallback_accounts = ["claude_code:alt"]

usage_ceilings = { "claude_code:main" = 90, "claude_code:alt" = 50 }
usage_cache = "/run/user/1000/agentdeck"

[credentials]
github_token_file = "~/.config/agentdeck/poller-token"
"""


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "instances" / "reference"
    path.mkdir(parents=True)
    (path / "overlay.toml").write_text(OVERLAY)
    return tmp_path


def _text(root):
    return (root / "instances" / "reference" / "overlay.toml").read_text()


def test_it_reads_the_order_the_engine_will_build(root):
    view = poller_settings.read_worker_accounts(root, "reference")
    assert view.editable
    assert view.account == "claude_code:main"
    assert view.fallbacks == ("claude_code:alt",)
    assert view.order == ("claude_code:main", "claude_code:alt")


def test_a_host_with_no_overlay_is_not_editable(tmp_path):
    view = poller_settings.read_worker_accounts(tmp_path, "missing")
    assert not view.editable
    assert "no overlay.toml" in view.note


def test_unreadable_toml_is_a_fact_not_a_crash(tmp_path):
    path = tmp_path / "instances" / "broken"
    path.mkdir(parents=True)
    (path / "overlay.toml").write_text("[worker\naccount =")
    view = poller_settings.read_worker_accounts(tmp_path, "broken")
    assert not view.editable
    assert "could not be read" in view.note


def test_switching_the_primary_swaps_only_the_two_keys(root):
    after = poller_settings.set_worker_accounts(
        root, "reference", account="claude_code:alt", fallbacks=["claude_code:main"], now=_NOW
    )
    assert after.order == ("claude_code:alt", "claude_code:main")
    text = _text(root)
    assert 'account = "claude_code:alt"' in text
    assert 'fallback_accounts = ["claude_code:main"]' in text
    # Everything the operator wrote by hand is still there, comments included.
    assert "Switched main -> alt on 2026-08-07" in text
    assert "codex:codex removed 2026-08-07" in text
    assert 'usage_cache = "/run/user/1000/agentdeck"' in text
    assert "github_token_file" in text
    assert '"claude_code:main" = 90' in text


def test_the_change_is_stamped_once_however_often_it_is_made(root):
    poller_settings.set_worker_accounts(root, "reference", account="claude_code:alt", now=_NOW)
    poller_settings.set_worker_accounts(root, "reference", account="claude_code:main", now=_NOW)
    text = _text(root)
    assert text.count(poller_settings.MARKER) == 1
    assert "2026-08-09 10:30Z by operator" in text


def test_an_account_leaving_the_order_takes_its_ceiling_with_it(root):
    """The engine refuses an Overlay whose ceiling names an account the order does not offer."""
    poller_settings.set_worker_accounts(root, "reference", account="claude_code:main", now=_NOW)
    text = _text(root)
    assert "claude_code:alt" not in text
    assert '"claude_code:main" = 90' in text


def test_an_account_leaving_the_order_takes_its_spend_target_with_it(root):
    """#1965 added a second per-account table. Leaving it behind would make the engine refuse
    the Overlay, and the operator's account switch would roll back for no visible reason."""
    path = root / "instances" / "reference" / "overlay.toml"
    path.write_text(
        OVERLAY.replace(
            'usage_cache =', 'usage_targets = { "claude_code:alt" = 99 }\nusage_cache ='
        )
    )

    poller_settings.set_worker_accounts(root, "reference", account="claude_code:main", now=_NOW)

    text = _text(root)
    assert "claude_code:alt" not in text
    assert "usage_targets = {}" in text


def test_the_primary_always_leads_and_is_never_duplicated(root):
    after = poller_settings.set_worker_accounts(
        root,
        "reference",
        account="claude_code:alt",
        fallbacks=["claude_code:alt", "claude_code:main"],
        now=_NOW,
    )
    assert after.order == ("claude_code:alt", "claude_code:main")
    assert _text(root).count('fallback_accounts = ["claude_code:main"]') == 1


def test_dropping_every_fallback_leaves_an_empty_list(root):
    after = poller_settings.set_worker_accounts(
        root, "reference", account="claude_code:main", fallbacks=[], now=_NOW
    )
    assert after.fallbacks == ()
    assert "fallback_accounts = []" in _text(root)


def test_a_missing_worker_table_gains_one(tmp_path):
    path = tmp_path / "instances" / "bare"
    path.mkdir(parents=True)
    (path / "overlay.toml").write_text('contract = 1\n\n[paths]\nstate = "/tmp/s.json"\n')
    after = poller_settings.set_worker_accounts(
        tmp_path, "bare", account="claude_code:main", now=_NOW
    )
    assert after.account == "claude_code:main"
    assert "[worker]" in (path / "overlay.toml").read_text()


def test_a_multi_line_fallback_list_is_replaced_whole(tmp_path):
    path = tmp_path / "instances" / "wrapped"
    path.mkdir(parents=True)
    (path / "overlay.toml").write_text(
        'contract = 1\n\n[paths]\nstate = "/tmp/s.json"\n\n[worker]\n'
        'account = "claude_code:main"\n'
        'fallback_accounts = [\n  "claude_code:alt",\n  "claude_code:third",\n]\n'
        'usage_cache = "/run/user/1000/agentdeck"\n'
    )
    poller_settings.set_worker_accounts(
        tmp_path, "wrapped", account="claude_code:main", fallbacks=["claude_code:third"], now=_NOW
    )
    text = (path / "overlay.toml").read_text()
    assert 'fallback_accounts = ["claude_code:third"]' in text
    assert "claude_code:alt" not in text
    assert 'usage_cache = "/run/user/1000/agentdeck"' in text


def test_an_edit_that_would_not_load_is_rolled_back(root, monkeypatch):
    """A broken Overlay fails every wake, so a refused button is the better outcome."""
    monkeypatch.setattr(poller_settings, "_render_string", lambda value: f'"{value}')
    with pytest.raises(poller_settings.SettingsError) as raised:
        poller_settings.set_worker_accounts(
            root, "reference", account="claude_code:alt", now=_NOW
        )
    assert "rolled back" in str(raised.value)
    assert _text(root) == OVERLAY


def test_an_empty_primary_is_refused(root):
    with pytest.raises(poller_settings.SettingsError):
        poller_settings.set_worker_accounts(root, "reference", account="  ", now=_NOW)
    assert _text(root) == OVERLAY


def test_a_missing_overlay_is_refused_rather_than_created(tmp_path):
    with pytest.raises(poller_settings.SettingsError):
        poller_settings.set_worker_accounts(
            tmp_path, "nowhere", account="claude_code:main", now=_NOW
        )
    assert not (tmp_path / "instances" / "nowhere").exists()
