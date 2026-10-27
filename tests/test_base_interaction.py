"""The SessionProvider base owns the interaction admission policy: the INTERACT
gate for reads, and the gate + id-match + dict-normalization for answers. A
provider supplies only the read (`_actionable_interaction`) and the runtime
hand-off (`_answer_actionable`)."""

from __future__ import annotations

import pytest

from agentdeck.models import (
    Account,
    Capability,
    InjectResult,
    PendingInteraction,
    Session,
    SessionStatus,
)
from agentdeck.providers.base import SessionProvider

_ACCOUNT = Account(key="stub:main", provider_id="stub", label="main", root=None)
_INTERACTION = PendingInteraction(
    id="int-1", kind="question", thread_id="t", turn_id=None, title="Pick one"
)


class _StubProvider(SessionProvider):
    provider_id = "stub"

    def __init__(self, interaction: PendingInteraction | None) -> None:
        self._interaction = interaction
        self.reads: list[str] = []
        self.answered: list[tuple[str, dict]] = []

    async def scan_sessions(self, account):
        return []

    def watch_paths(self, account):
        return []

    async def read_transcript(self, account, session, after_seq=0):
        return []

    async def fetch_usage(self, account):
        return None

    def _actionable_interaction(self, account, session_id):
        self.reads.append(session_id)
        return self._interaction

    async def _answer_actionable(self, account, session, interaction, *, answers, decision):
        self.answered.append((interaction.id, answers))
        return InjectResult(True)


def _session(*, interact: bool) -> Session:
    caps = {Capability.TRANSCRIPT}
    if interact:
        caps.add(Capability.INTERACT)
    return Session(
        key="stub:main:s",
        account_key="stub:main",
        session_id="s",
        status=SessionStatus.LIVE,
        capabilities=frozenset(caps),
    )


def test_pending_interaction_gate_skips_the_provider_read_without_interact():
    provider = _StubProvider(_INTERACTION)
    assert provider.pending_interaction(_ACCOUNT, _session(interact=False)) is None
    assert provider.reads == []  # the gate short-circuits before the read


def test_pending_interaction_reads_when_interact_is_granted():
    provider = _StubProvider(_INTERACTION)
    assert provider.pending_interaction(_ACCOUNT, _session(interact=True)) is _INTERACTION
    assert provider.reads == ["s"]


async def test_answer_rejected_without_interact_capability():
    provider = _StubProvider(_INTERACTION)
    result = await provider.answer_interaction(
        _ACCOUNT, _session(interact=False), "int-1", answers={}, decision=None
    )
    assert result == InjectResult(False, "interaction is unavailable")
    assert provider.answered == []


async def test_answer_rejected_on_id_mismatch():
    provider = _StubProvider(_INTERACTION)
    result = await provider.answer_interaction(
        _ACCOUNT, _session(interact=True), "stale-id", answers={}, decision=None
    )
    assert result == InjectResult(False, "interaction is no longer pending")
    assert provider.answered == []


async def test_answer_delegates_with_dict_normalized_answers():
    provider = _StubProvider(_INTERACTION)
    # Pass a non-dict Mapping to prove the base normalizes to a plain dict before
    # the provider hook (the Claude worker path JSON-serializes it).
    from types import MappingProxyType

    result = await provider.answer_interaction(
        _ACCOUNT,
        _session(interact=True),
        "int-1",
        answers=MappingProxyType({"q": ["a"]}),
        decision=None,
    )
    assert result == InjectResult(True)
    assert provider.answered == [("int-1", {"q": ["a"]})]
    assert type(provider.answered[0][1]) is dict


async def test_missing_actionable_interaction_is_rejected():
    provider = _StubProvider(None)  # nothing pending
    result = await provider.answer_interaction(
        _ACCOUNT, _session(interact=True), "int-1", answers={}, decision=None
    )
    assert result == InjectResult(False, "interaction is no longer pending")
    assert provider.answered == []


def test_base_defaults_have_no_interaction():
    # A provider that overrides neither hook composes: no read, no answer.
    class _Bare(_StubProvider):
        _actionable_interaction = SessionProvider._actionable_interaction
        _answer_actionable = SessionProvider._answer_actionable

    bare = _Bare(_INTERACTION)
    assert bare.pending_interaction(_ACCOUNT, _session(interact=True)) is None


@pytest.mark.parametrize("interact", [False, True])
def test_gate_is_the_only_capability_check_pending_reads_see(interact):
    provider = _StubProvider(_INTERACTION)
    result = provider.pending_interaction(_ACCOUNT, _session(interact=interact))
    assert (result is _INTERACTION) == interact
