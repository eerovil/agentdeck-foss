"""Account-selection contract shared by delegation and head-review launches."""

from dataclasses import dataclass
from itertools import permutations
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agentdeck.web import deps


@dataclass(frozen=True)
class _Account:
    key: str
    provider_id: str
    available: bool = True


class _Provider:
    def __init__(self, *, preferred=False):
        self.machine_delegation_default = preferred
        self.probed = []

    def can_start_session(self, account):
        self.probed.append(account)
        return account.available


@pytest.fixture
def select(monkeypatch):
    """Use synthetic accounts/providers; never launch or query an agent."""

    def run(accounts, providers, account_key=None):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(accounts=accounts)))
        monkeypatch.setattr(deps, "PROVIDERS", providers)
        return deps.delegation_account(request, account_key)

    return run


@pytest.mark.parametrize("preferred", [False, True])
def test_only_available_account_is_selected(select, preferred):
    account = _Account("provider:main", "provider")
    assert select([account], {"provider": _Provider(preferred=preferred)}) is account


@pytest.mark.parametrize("preferred_first", [False, True])
def test_one_preferred_account_wins_independently_of_configuration_order(select, preferred_first):
    preferred = _Account("preferred:main", "preferred")
    other = _Account("other:main", "other")
    accounts = [preferred, other] if preferred_first else [other, preferred]
    providers = {"preferred": _Provider(preferred=True), "other": _Provider()}
    assert select(accounts, providers) is preferred


def test_explicit_account_overrides_the_provider_default(select):
    preferred = _Account("preferred:main", "preferred")
    selected = _Account("other:main", "other")
    providers = {"preferred": _Provider(preferred=True), "other": _Provider()}
    assert select([preferred, selected], providers, selected.key) is selected


@pytest.mark.parametrize("account_key", ["missing:main", "other:main", "", "main", "Provider:main"])
def test_explicit_unknown_or_unavailable_account_never_falls_back(select, account_key):
    available = _Account("provider:main", "provider")
    unavailable = _Account("other:main", "other", available=False)
    providers = {"provider": _Provider(preferred=True), "other": _Provider()}
    with pytest.raises(HTTPException) as error:
        select([available, unavailable], providers, account_key)
    assert error.value.status_code == 404
    assert error.value.detail == "unknown delegation account"


@pytest.mark.parametrize("accounts", [[], [_Account("provider:main", "provider", False)]])
def test_no_available_account_reports_none(select, accounts):
    with pytest.raises(HTTPException) as error:
        select(accounts, {"provider": _Provider(preferred=True)})
    assert error.value.status_code == 422
    assert error.value.detail == "account_key is required; available accounts: none"


def test_unavailable_preferred_account_does_not_hide_the_only_available_account(select):
    unavailable = _Account("preferred:main", "preferred", available=False)
    available = _Account("other:main", "other")
    providers = {"preferred": _Provider(preferred=True), "other": _Provider()}
    assert select([unavailable, available], providers) is available


@pytest.mark.parametrize("preferred", [False, True])
def test_multiple_accounts_on_one_provider_are_not_an_implicit_account_choice(select, preferred):
    accounts = [_Account("provider:one", "provider"), _Account("provider:two", "provider")]
    providers = {"provider": _Provider(preferred=preferred)}
    with pytest.raises(HTTPException) as error:
        select(accounts, providers)
    assert error.value.status_code == 422
    assert error.value.detail == (
        "account_key is required; available accounts: provider:one, provider:two"
    )
    assert select(accounts, providers, "provider:two") is accounts[1]


@pytest.mark.parametrize("preferred_count", [0, 2, 3])
def test_ambiguous_defaults_report_only_available_accounts_in_configuration_order(
    select, preferred_count
):
    accounts = [
        _Account("one:main", "one"),
        _Account("disabled:main", "disabled", available=False),
        _Account("two:main", "two"),
        _Account("three:main", "three"),
    ]
    providers = {
        name: _Provider(preferred=index < preferred_count)
        for index, name in enumerate(("one", "two", "three"))
    }
    providers["disabled"] = _Provider(preferred=True)
    with pytest.raises(HTTPException) as error:
        select(accounts, providers)
    assert error.value.status_code == 422
    assert error.value.detail == (
        "account_key is required; available accounts: one:main, two:main, three:main"
    )


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_availability_is_evaluated_per_account_not_cached_per_provider(select, order):
    accounts = [
        _Account("preferred:offline", "preferred", available=False),
        _Account("preferred:online", "preferred"),
        _Account("other:main", "other"),
    ]
    configured = [accounts[index] for index in order]
    providers = {"preferred": _Provider(preferred=True), "other": _Provider()}
    assert select(configured, providers) is accounts[1]
    assert providers["preferred"].probed == [a for a in configured if a.provider_id == "preferred"]
    assert providers["other"].probed == [accounts[2]]
    assert configured == [accounts[index] for index in order]


def test_account_names_with_shared_prefixes_are_matched_exactly(select):
    accounts = [_Account("provider:main", "provider"), _Account("provider:main-alt", "provider")]
    providers = {"provider": _Provider()}
    assert select(accounts, providers, "provider:main") is accounts[0]
    assert select(accounts, providers, "provider:main-alt") is accounts[1]


def test_selection_rechecks_availability_on_each_request(select):
    first = _Account("provider:one", "provider")
    second = _Account("provider:two", "provider")
    provider = _Provider()
    assert select([first], {"provider": provider}) is first
    assert select([second], {"provider": provider}) is second
    assert provider.probed == [first, second]


def test_policy_is_owned_by_request_dependencies():
    assert deps.delegation_account.__module__ == deps.__name__
