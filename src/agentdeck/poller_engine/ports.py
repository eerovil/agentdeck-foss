"""The engine's I/O boundary: one port in, declared effects out (#176 slice 9).

The engine is deterministic and stdlib-only, so it performs no I/O itself. Everything
it needs to know arrives through a single ``EnginePort`` the host implements, and
everything it wants to change leaves as a declared ``Effect`` applied through that
same port. Two consequences make the engine testable and safe:

* **One seam.** The app-side GitHub adapter and the tests' fake implement this one
  port. Nothing else has to be faked.
* **Writes are declarative and reported.** ``apply`` returns whether the change
  actually landed, so a failed comment becomes retryable durable state rather than an
  assumption the engine records as done.

The engine holds no cards since #3732, so what is left here is what a housekeeping wake
needs: the budget gate, a permission read, issue states, opening and finding issues, and
the scheduled-task sweep.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .schedules import TaskIssue


class EffectKind(Enum):
    COMMENT = "comment"  # post an engine comment (bot-marked)
    SET_BODY = "set-body"  # rewrite an engine-owned issue's body (#2517)
    PIN_ISSUE = "pin-issue"  # pin an engine-owned issue in its repository (#2517)


@dataclass(frozen=True)
class Effect:
    """One outward change the engine wants made, applied through the port."""

    kind: EffectKind
    repo: str
    number: int
    value: object = None


@dataclass(frozen=True)
class CommentBody:
    """Comment prose plus the navigation links the adapter renders under it."""

    body: str
    session_url: str = ""
    pull_request_url: str = ""
    pull_request_ref: str = ""

    def __str__(self) -> str:
        return self.body


@dataclass(frozen=True)
class WorktreeCleanup:
    """Per-path result for a destructive host cleanup effect.

    A boolean can only say that *something* failed.  Keeping the exact failed paths lets
    the engine forget successful removals while retrying the remainder.

    ``retained`` is the other outcome, and it is not a failure: each pair is a path the host
    deliberately did **not** remove, with the reason a person should read (#1718).  A worktree
    still holding uncommitted work is somebody's unfinished progress, and no release is worth
    deleting it; one no Work claims is not the card's to delete at all (#2195).  Keeping
    that separate from ``failed`` matters, because a path reported pending is retried on
    every wake for as long as the card exists (#1189) — while a retained one is given back
    to the operator reaper, which is where an unclaimed directory belongs.
    """

    failed: tuple[str, ...] = ()
    retained: tuple[tuple[str, str], ...] = ()

    @property
    def landed(self) -> bool:
        return not self.failed


@runtime_checkable
class EnginePort(Protocol):
    """Everything the engine cannot do for itself (see module docstring)."""

    def live_queries_halt(self, *, stale_reading_ok: bool = False) -> str | None:
        """Why GitHub forbids live reads, optionally followed by ``: <reading source>``.

        ``stale_reading_ok`` says this wake is webhook-quiet, so an older budget reading may
        answer rather than a fresh probe being bought for it (#2082).
        """

    def permission(self, repo: str, login: str) -> str | None:
        """This login's collaborator level on the repository, or None if it could not be read.

        The engine decides what a level *means* (``authorization.py``): the port only reports
        what GitHub said, because an authorization rule is an engine invariant and must not
        depend on an adapter getting it right. ``None`` is uncertainty, never a denial.
        """

    def ref_states(self, refs: list[str]) -> dict[str, str]:
        """GitHub's own state for each ``repo#number``, whether it is an issue or a PR (#1230).

        ``OPEN``, ``CLOSED`` or ``MERGED``. A ref that could not be read is simply **absent**
        from the answer rather than reported as open: the engine fails closed on ignorance
        here, and saying "open" would be a claim the adapter has no grounds for.
        """

    def apply(self, effect: Effect) -> bool:
        """Perform one outward change, answering whether it landed."""

    def find_marked_issue(self, repo: str, marker: str, *, since: str) -> tuple[int | None, bool]:
        """``(number, conclusive)`` for an issue carrying this marker, created at/after ``since``.

        Read by listing recent issues rather than through GitHub's search index, which lags: an
        issue created seconds ago can be reported absent, and "absent" is precisely what this is
        asked to establish before a second one is created.

        ``conclusive`` is separate from the number because "not found" and "could not establish
        that" must not collapse into one answer — they differ by exactly one duplicated issue. It
        is false both when the read failed and when the listing never reached back as far as
        ``since``: a busy repository can push the issue being looked for out of one page, and a
        page that is entirely newer than the intent proves nothing about whether it exists.
        """

    def open_issue(
        self, repo: str, title: str, body: str, *, labels: tuple[str, ...] = ()
    ) -> int | None:
        """Open an issue and return its number, or None when the write did not land.

        ``labels`` go on beside the ownership label in the same write, such as the spec-only
        label an ``open-work`` proposal asked for (#2874).

        The only port method that *creates* rather than mutates, and the only one whose failure
        cannot be distinguished from success by looking at its return value alone — which is why
        the caller writes a durable intent first and reconciles through ``find_marked_issue``.
        """

    def open_list_issue(self, repo: str, title: str, body: str) -> int | None:
        """Open an issue **without** the ownership label, or None when the write did not land.

        For an issue the engine keeps up to date itself, such as the scheduled-task list
        (#2517): labelling it would hand it to a worker. The same intent-then-look rule as
        ``open_issue`` applies to the caller.
        """

    def scheduled_tasks(self, tracked: tuple[str, ...] = ()) -> dict[str, TaskIssue] | None:
        """Every issue carrying the ``scheduled`` label in this project's issue repositories.

        Keyed ``repo#number``. Open tasks are all listed, and the most recently updated closed
        ones beside them. Every ``tracked`` task is also read directly, so a tracked task absent
        from the answer is one GitHub says is unlabelled or gone — never one a bounded page
        merely left out. None means the sweep could not be read whole, which must never be read
        as "there are no tasks" (#2517).
        """
