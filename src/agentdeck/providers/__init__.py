"""Provider registry. The web layer resolves providers only through this."""

from dataclasses import dataclass

from ..models import Account, Capability, PendingInteraction, Session
from .base import SessionProvider
from .claude_code.provider import ClaudeCodeProvider
from .codex.provider import CodexProvider

PROVIDERS: dict[str, SessionProvider] = {
    ClaudeCodeProvider.provider_id: ClaudeCodeProvider(),
    CodexProvider.provider_id: CodexProvider(),
}

# Provider ids this deck once had and dropped. A config that still names one is
# loaded without that account rather than refused, so an old config cannot stop
# the deck from starting after the deploy that removed it (#2600).
RETIRED_PROVIDERS = frozenset({"cursor"})


def pending_interaction_for(
    session: Session, accounts: list[Account]
) -> PendingInteraction | None:
    """A session's actionable interaction, for callers that hold a Session but
    not its account/provider. The provider applies the INTERACT gate internally,
    so this is only the account lookup; a session whose account is gone (or whose
    provider has no live interaction) resolves to None."""
    account = next((a for a in accounts if a.key == session.account_key), None)
    if account is None:
        return None
    return PROVIDERS[account.provider_id].pending_interaction(account, session)


@dataclass(frozen=True)
class DeliveryTarget:
    """The three things a deck-authored write into a chat needs, or nothing.

    The account, provider and session always travel together, and every caller
    that resolves them also has to decide what a half-resolved chat means. That
    answer is the same everywhere — a session whose account is gone, whose
    provider is unknown, or which cannot be injected is simply not a target — so
    it lives here rather than being re-derived per service.
    """

    account: Account
    provider: SessionProvider
    session: Session


def delivery_target_for(session: Session, accounts: list[Account]) -> DeliveryTarget | None:
    """Resolve a chat the deck may write an ordinary user turn into.

    ``Capability.INJECT`` is checked here rather than by the caller because a
    chat that cannot be injected is not a target at all; the injector's own
    queue-tolerance is a separate, deliberate decision the boss-facing routes
    make and these background sweeps do not.
    """
    if Capability.INJECT not in session.capabilities:
        return None
    account = next((item for item in accounts if item.key == session.account_key), None)
    if account is None:
        return None
    provider = PROVIDERS.get(account.provider_id)
    if provider is None:
        return None
    return DeliveryTarget(account, provider, session)


__all__ = [
    "PROVIDERS",
    "RETIRED_PROVIDERS",
    "DeliveryTarget",
    "SessionProvider",
    "delivery_target_for",
    "pending_interaction_for",
]
