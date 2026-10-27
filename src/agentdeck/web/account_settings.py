"""One contract for account-scoped defaults shown and applied by New chat."""

from __future__ import annotations

from fastapi import Request

from ..models import Account
from ..providers.base import NewSessionDefaults, SessionProvider
from .deps import get_db


def native_new_session_defaults(
    request: Request, account: Account, provider: SessionProvider
) -> NewSessionDefaults:
    model_override = request.app.state.config.native_model_override(
        account.provider_id, account.label
    )
    return provider.new_session_defaults(account, model_override=model_override)


def saved_new_session_defaults(
    request: Request, account: Account, provider: SessionProvider
) -> NewSessionDefaults:
    """Return only valid saved overrides; stale provider choices fail closed."""
    stored = get_db(request).load_account_new_chat_defaults(account.key)
    model = stored.get("model")
    effort = stored.get("effort")
    return NewSessionDefaults(
        model=model if model and provider.is_valid_model(model) else None,
        effort=effort if effort and provider.is_valid_effort(effort) else None,
    )


def effective_new_session_defaults(
    request: Request, account: Account, provider: SessionProvider
) -> NewSessionDefaults:
    native = native_new_session_defaults(request, account, provider)
    saved = saved_new_session_defaults(request, account, provider)
    return NewSessionDefaults(
        model=saved.model or native.model,
        effort=saved.effort or native.effort,
    )


def choice_label(value: str | None, choices) -> str:
    if not value:
        return "account"
    return next((choice.label for choice in choices if choice.value == value), value)


def account_settings_context(
    request: Request, account: Account, provider: SessionProvider
) -> dict[str, object]:
    native = native_new_session_defaults(request, account, provider)
    saved = saved_new_session_defaults(request, account, provider)
    return {
        "account": account,
        "provider": provider,
        "model_choices": provider.selectable_models,
        "effort_choices": provider.selectable_efforts,
        "native_defaults": native,
        "saved_defaults": saved,
        "effective_defaults": NewSessionDefaults(
            saved.model or native.model, saved.effort or native.effort
        ),
        "choice_label": choice_label,
    }
