"""The host adapter: the engine's port, implemented against real GitHub and a real worker (#180).

The engine emits a directive; this package delivers it. Delivery lives here rather than in the
engine artifact because AgentDeck's worker client is async and dependency-laden, while the
artifact is stdlib-only (#116/#176). GitHub reads and effects ship inside the engine instead —
a wake that cannot read GitHub cannot run at all.

The engine has already made every decision by the time a directive arrives here, and it has
already recorded the send intent durably. All this package does is deliver, and then write the
receipt the engine reads next wake — which is what makes delivery at-most-once rather than
best-effort.
"""

from .worker import DeliveryOutcome, WorkerDispatcher

__all__ = ["DeliveryOutcome", "WorkerDispatcher"]
