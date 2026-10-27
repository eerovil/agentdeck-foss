"""Which refused sends may wait for a restarting runtime (#2847).

Only a chat the deck already knows is its own, whose runtime cannot be reached right
now, is held; every other refusal stays a refusal.
"""

from types import SimpleNamespace

from agentdeck.providers.base import SessionProvider
from agentdeck.providers.claude_code.provider import ClaudeCodeProvider
from agentdeck.providers.codex.provider import CodexProvider

ACCOUNT = SimpleNamespace(key="test:alt")
SESSION = SimpleNamespace(session_id="sid")


class _Owner:
    def __init__(self, owned, available=True):
        self._owned = owned
        self.available = available

    def owns(self, session_id):
        return session_id in self._owned


def test_claude_holds_only_for_its_own_chat_while_the_runtime_is_down():
    provider = ClaudeCodeProvider()
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False  # never reached it

    provider._workers[ACCOUNT.key] = _Owner({"sid"}, available=False)
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is True

    provider._workers[ACCOUNT.key] = _Owner({"sid"}, available=True)
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False

    provider._workers[ACCOUNT.key] = _Owner({"other"}, available=False)
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False


def test_codex_holds_only_for_its_own_thread_while_refresh_fails():
    provider = CodexProvider()
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False

    provider._clients[ACCOUNT.key] = _Owner({"sid"})
    provider._refresh_ok[ACCOUNT.key] = False
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is True

    provider._refresh_ok[ACCOUNT.key] = True
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False

    provider._clients[ACCOUNT.key] = _Owner({"other"})
    provider._refresh_ok[ACCOUNT.key] = False
    assert provider.runtime_unreachable(ACCOUNT, SESSION) is False


def test_a_provider_without_a_runtime_never_holds():
    assert SessionProvider.runtime_unreachable(object(), ACCOUNT, SESSION) is False
