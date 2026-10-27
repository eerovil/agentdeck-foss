"""One Work's facts: its source, its outputs, their heads and states, and their review (#2229).

Every Work surface — the front page, the Work page and whatever comes after them — reads a
Work through ``WorkFacts``, built once per pass by ``work_facts`` from local reads only: the
Poller card as the engine holds it, the issue's cached record, the cached pull request rows,
the cached open pull request list and the pull requests the Work's chats registered. No
GitHub call is made here, and nothing is stored: it is a read of facts the deck already has,
not a lifecycle of its own.

Which pull requests are the Work's outputs is decided here and nowhere else. The Poller card
is the explicit answer. A card that names none — or a Work with no card — takes the discovery
the deck can make without asking GitHub, in a fixed order; each output records how it was
found (``OutputFact.found_by``) so a reader can tell an explicit output from an inferred one.

Review is exact-head evidence: each open output carries the instance reviewer's verdict for
its current head (``poller_nudge.ExternalReviews.judge``), read once here rather than again by
each page.

Each output also carries its size and target branch from the same cached row (#2236):
``gh pr view`` stores the per-file ``files`` list beside the changed-file count, so
``diffstat`` sums a Work's outputs without a GitHub call. The size counts source files only
(#2331): tests, docs and non-code files (``worktree_diff.classify_path``) are left out, the
way the chat's diff pane groups them away. The same list's ``changeType`` splits that size
into lines in new files and lines in edited ones (#3617): a large diff of new files is usually
fine to review, so the front page's icon is graded by the edited lines alone.

An output whose auto-merge waits on a required check that never reported carries those
checks' names (#2275), from the row ``merge_checks`` caches for armed pull requests only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .issue_context import issue_number_from_branch
from .merge_checks import armed, head_committed_at, merge_answer, never_ran
from .work import WorkRef
from .worktree_diff import KIND_SOURCE, classify_path

# How an output was found (``OutputFact.found_by``), strongest first.
FOUND_BY_CARD = "poller_card"  # the worker's ``comment_links`` link or the engine's ``review_prs``
FOUND_BY_CLOSING = "closing_reference"  # GitHub's ``closingIssuesReferences``, any repository
# LEGACY(epic:work): an ``issue-<n>`` head branch in the issue's own repository. Only a card
# whose worker reported no link, from before every dispatch recorded its pull request, needs it.
FOUND_BY_BRANCH = "branch"
# LEGACY(epic:work): an open or merged pull request a Work's chat registered (#2228, #2286).
# It exists because a card may name no pull request — the engine drops a finished card, and a
# worker whose pull request only says "Part of" never links one — so the chat's registration is
# the only link the deck holds; a Run output fact would replace it.
FOUND_BY_CHAT = "chat_registration"

MERGED = "merged"
CLOSED = "closed"
_SETTLED = frozenset({MERGED, CLOSED})


def pull_identity(url: str) -> tuple[str, int] | None:
    """``(owner/repo, number)`` of a pull request URL, or None for anything else."""
    parts = url.rstrip("/").split("/")
    if len(parts) >= 4 and parts[-2] == "pull" and parts[-1].isdigit():
        return f"{parts[-4]}/{parts[-3]}", int(parts[-1])
    return None


def pull_state(record: Mapping[str, Any] | None) -> str:
    """``open``, ``merged`` or ``closed`` from a cached pull request row, or "" unknown.

    The row is ``git_context``'s (``gh pr view``: ``OPEN``/``MERGED``/``CLOSED``) or the
    webhook write-through's (``open``/``closed`` plus ``mergedAt``); both spellings count.
    """
    if not isinstance(record, Mapping):
        return ""
    state = str(record.get("state") or "").lower()
    if record.get("mergedAt") or state == MERGED:
        return MERGED
    return state if state in {"open", CLOSED} else ""


def card_pull_requests(card: Any) -> tuple[str, ...]:
    """Every pull request the Poller card names: the worker's link, then ``review_prs``, then
    the ones the engine recorded as merged (#2332)."""
    found = tuple(getattr(card, "pull_requests", ()) or ())
    first = str(getattr(card, "pull_request_url", "") or "")
    if first and first.lower() not in {url.lower() for url in found}:
        found = (first, *found)
    known = {url.lower() for url in found}
    extra = [url for url in engine_merged(card) if url.lower() not in known]
    return (*found, *dict.fromkeys(extra))


def engine_merged(card: Any) -> tuple[str, ...]:
    """The pull requests the engine itself recorded as merged for this card (#2332)."""
    return tuple(getattr(card, "merged", ()) or ())


@dataclass(frozen=True)
class OutputFact:
    """One pull request the Work produced, with its current head, state and review.

    ``state`` and ``head`` are the cached row's ("" when the deck never resolved it: unknown,
    never a guess). ``review`` is the instance reviewer's verdict for that head, with its
    ``review_detail`` (``ExternalReviews.judge``); "" when no reviewer is in play or the output
    is merged or closed. ``review_clean`` says the reviewer recorded a clean review of exactly
    that head (``ExternalReviews.clean_at``, #2254), and ``draft`` is the cached row's.

    ``review`` is ``held`` where the reviewer awaits a chat that waits on a person (#2442):
    ``review_detail`` still names the chat, and ``review_held`` the labels it carries.
    """

    url: str
    repository: str
    number: int
    found_by: str
    state: str = ""
    head: str = ""
    review: str = ""
    review_detail: str = ""
    base: str = ""  # the branch it merges into, "" unknown
    # Its source files' size, from the cached row's ``files``; None when the row does not
    # carry a complete per-file list (#2236, #2331).
    additions: int | None = None
    deletions: int | None = None
    files: int | None = None
    # The lines and count of those source files GitHub marks ``ADDED``; None when the row
    # does not say for every one (#3617).
    new_lines: int | None = None
    new_files: int | None = None
    review_clean: bool = False
    draft: bool = False
    # GitHub auto-merge is on for it, per the cached row (#2275).
    auto_merge: bool = False
    # Required checks its auto-merge has waited on too long with no report (#2275), sorted.
    never_ran: tuple[str, ...] = ()
    # When the reviewer sent exactly this head back for changes (#2332), None when it did not.
    review_changes_at: datetime | None = None
    # GitHub's merge answer for exactly this head (``merge_checks``, #2332): ``mergeable``
    # (``MERGEABLE``/``CONFLICTING``/``UNKNOWN``) and ``merge_state`` (``CLEAN``, ``DIRTY``,
    # ``BEHIND``…), "" when the deck holds none for this head.
    mergeable: str = ""
    merge_state: str = ""
    # The cached row's per-file list is as complete as a re-read could make it — every file,
    # or cut at gh's limit — so a backfill has nothing more to fetch for it (#2331).
    file_list: bool = False
    # The chat board's labels on the awaited review chat that wait on a person (#2442).
    review_held: tuple[str, ...] = ()
    # Its repository has no tests, so a clean review of its head is its verification
    # (``verification = "review_only"``, #2729, #2832).
    review_only: bool = False
    # When its head commit was made, from the ``merge_checks`` row for exactly that head
    # (#3199); None when the deck holds no such row.
    head_at: datetime | None = None

    @property
    def conflicting(self) -> bool:
        """GitHub says this head conflicts with its base: nothing can merge it as it is."""
        return self.mergeable == "CONFLICTING" or self.merge_state == "DIRTY"

    @property
    def label(self) -> str:
        return f"{self.repository}#{self.number}" if self.repository else self.url

    @property
    def settled(self) -> bool:
        return self.state in _SETTLED

    @property
    def is_open(self) -> bool:
        """Known to be open; an unresolved state ("") is not (#3187)."""
        return self.state == "open"


@dataclass(frozen=True)
class DiffStat:
    """``+additions/−deletions · N files`` over a Work's counted outputs' source files.

    ``new_lines``/``new_files`` are the part of it in files the pull requests add, None when
    any counted output's row does not say (#3617).
    """

    additions: int
    deletions: int
    files: int
    new_lines: int | None = None
    new_files: int | None = None

    @property
    def split(self) -> bool:
        """Whether the new/edited split is known for a change that touches source."""
        return self.files > 0 and self.new_lines is not None and self.new_files is not None

    @property
    def edited_lines(self) -> int:
        return self.additions + self.deletions - (self.new_lines or 0)

    @property
    def edited_files(self) -> int:
        return self.files - (self.new_files or 0)

    @property
    def mostly_new(self) -> bool:
        """At least three quarters of the changed lines are in new files: GitHub's [+].

        A change of no lines (a rename alone) adds nothing new, so it is [•]."""
        total = self.additions + self.deletions
        return total > 0 and 4 * (self.new_lines or 0) >= 3 * total

    @property
    def grade(self) -> str:
        """``good``, ``ok`` or ``bad`` to review, by the lines in edited files only (#3617).

        The owner set 200 and 600: at 50 and 200 half the live Works were red."""
        edited = self.edited_lines
        return "good" if edited <= 200 else "ok" if edited <= 600 else "bad"

    @property
    def split_label(self) -> str:
        def lines(count: int) -> str:
            return f"{count} {'line' if count == 1 else 'lines'}"

        def files(count: int, kind: str) -> str:
            return f"{count} {kind} {'file' if count == 1 else 'files'}"

        return (
            f"Diff: {lines(self.new_lines or 0)} in {files(self.new_files or 0, 'new')}, "
            f"{lines(self.edited_lines)} in {files(self.edited_files, 'edited')}"
        )


def _counted(outputs: Sequence[OutputFact]) -> list[OutputFact]:
    """The outputs a Work's size is about: every one except those closed without merging."""
    return [output for output in outputs if output.state != CLOSED]


def diffstat(outputs: Sequence[OutputFact]) -> DiffStat | None:
    """The size of the Work's counted outputs, summed; None when any of them is unknown.

    A partial sum would be a wrong number, so one output whose row lacks the fields hides
    the whole line until the bounded backfill re-reads it. No output, no line (#2236).
    """
    counted = _counted(outputs)
    if not counted:
        return None
    sizes = [
        (output.additions, output.deletions, output.files, output.new_lines, output.new_files)
        for output in counted
    ]
    if any(value is None for size in sizes for value in size[:3]):
        return None
    if any(value is None for size in sizes for value in size[3:]):
        # The size is known but not its split: a partial split would be a wrong one.
        sizes = [size[:3] for size in sizes]
    return DiffStat(*(sum(column) for column in zip(*sizes, strict=True)))


DEFAULT_TARGET = "master"


def target_branches(outputs: Sequence[OutputFact]) -> tuple[str, ...]:
    """The branches the counted outputs merge into, when one is not ``master``; else ()."""
    bases = dict.fromkeys(output.base for output in _counted(outputs) if output.base)
    return tuple(base for base in bases if base != DEFAULT_TARGET)


def _count(value: Any) -> int | None:
    """A non-negative count from a cached row, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


# ``gh pr view`` lists at most this many files (#2331).
GH_FILE_LIMIT = 100


def _complete_file_list(row: Mapping[str, Any]) -> bool:
    """Whether a re-read could not improve the row's file list: it lists every changed file,
    or ``gh`` cut it at its limit. A list that disagrees with ``changedFiles`` otherwise is
    stale and is read again (#2331)."""
    files = row.get("files")
    if not isinstance(files, list):
        return False
    return len(files) == _count(row.get("changedFiles")) or len(files) >= GH_FILE_LIMIT


def source_size(row: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """``(additions, deletions, files)`` over a cached row's source files only (#2331).

    None unless ``files`` lists every changed file: ``gh pr view`` stops at 100, and a sum
    over part of the list would be a wrong number, so a longer pull request shows no size.
    """
    files = row.get("files")
    if not isinstance(files, list) or len(files) != _count(row.get("changedFiles")):
        return None
    additions = deletions = count = 0
    for item in files:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            return None
        added, removed = _count(item.get("additions")), _count(item.get("deletions"))
        if added is None or removed is None:
            return None
        if classify_path(item["path"]) == KIND_SOURCE:
            additions, deletions, count = additions + added, deletions + removed, count + 1
    return additions, deletions, count


def new_source_size(row: Mapping[str, Any]) -> tuple[int, int] | None:
    """``(lines, files)`` of the row's source files GitHub marks ``ADDED`` (#3617).

    None when ``source_size`` has no size, or any source file has no ``changeType``: a row
    stored before it was kept is not read again for this, the next re-read fills it in.
    """
    if source_size(row) is None:
        return None
    lines = count = 0
    for item in row["files"]:
        if classify_path(item["path"]) != KIND_SOURCE:
            continue
        kind = item.get("changeType")
        if not isinstance(kind, str):
            return None
        if kind == "ADDED":
            lines, count = lines + item["additions"] + item["deletions"], count + 1
    return lines, count


@dataclass(frozen=True)
class Registration:
    """A pull request one of the Work's chats registered, and whether that chat is in this
    Work alone: a chat working on several Works cannot say which of them it was for."""

    url: str
    exclusive: bool


@dataclass(frozen=True)
class WorkFacts:
    """What every Work surface reads about one Work, gathered once per pass."""

    ref: WorkRef | None = None
    card: Any = None  # the Poller card exactly as the engine holds it, or None
    record: Mapping[str, Any] | None = None  # the issue's cached record
    outputs: tuple[OutputFact, ...] = ()
    finished_at: datetime | None = None
    # The issue closed and no person has finished the Work since (#2510, ``awaiting_finish``).
    awaiting_finish: bool = False
    # It closed on a merge into a repository that deploys on merge, under ``DEPLOY_GRACE`` ago
    # (``deploy_due``, #2760): the deploy comes next, so it does not ask for a Finish yet.
    deploy_due: bool = False
    # A person paused the Work on this deck rather than with its Poller's label (#2559).
    deck_paused: bool = False
    # A person starred the Work (#3116): it goes ahead of unstarred Works in every line.
    starred: bool = False
    # No agent works on it (``work_overview.unstarted_issues``, #3558): the ownership label
    # the Start agent action adds. "" for every other Work.
    start_label: str = ""
    # The branch staging copy that runs the head of every open pull request, so the change
    # can be tried there already (``staging_deploys.staging_copy``, #3180); None otherwise.
    staging: Any = None


def _stamp(value: Any) -> datetime | None:
    """An ISO timestamp as an aware datetime, or None for anything unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def work_finished_at(card: Any, issue: Mapping[str, Any] | None) -> datetime | None:
    """When the Work finished, or None while it has not or the time is unknown (#2219).

    GitHub's own ``closed_at`` for the Work's issue, from the cached issue record. Never
    ``updatedAt``: a comment on an issue closed long ago moves that, and must not make the
    Work newly finished. A record cached before it carried ``closedAt`` has no finish time
    until it is next refreshed. A Poller card still on the board and not done means the
    Work is open unless the issue closed after the engine last looked at the card: the
    engine moves it on its next pass, and the Work is finished now (#2226).
    """
    if not issue or issue.get("state") != "closed" or issue.get("is_pr"):
        return None
    when = _stamp(issue.get("closedAt"))
    if when is None:
        return None
    if card is not None and not getattr(card, "done", False):
        checked = _stamp(getattr(card, "last_live_check", ""))
        if checked is None or when <= checked:
            return None  # the engine saw the close, or never said when it looked
    return when


# How far a deck-side Finish may trail GitHub's own close time and still count for that close:
# Close issue records the Finish right after GitHub answered, on this host's clock (#2510).
FINISH_SLACK = timedelta(minutes=1)


def awaiting_finish(
    finished_at: datetime | None,
    signed_off_at: datetime | None,
    cutoff: datetime | None,
) -> bool:
    """Whether a Work whose issue closed still waits for a person's Finish (#2510).

    The issue may close by itself — a merge, the poller's ``resolved`` — but the Work is over
    only when a person says so. ``finished_at`` is the issue's close (``work_finished_at``);
    ``signed_off_at`` is the last Finish pressed on this deck; ``cutoff`` is when this deck
    started asking. A close before the cutoff, or with no cutoff on record, counts as signed
    off, so nothing that finished before this existed comes back. A Finish counts only for the
    close it followed: an issue reopened and closed again waits for a new one.
    """
    if finished_at is None or cutoff is None or finished_at <= cutoff:
        return False
    return signed_off_at is None or signed_off_at < finished_at - FINISH_SLACK


# How long a closed issue whose Work merged into a repository that deploys on merge waits for
# the engine's deploy watch before it asks a person to finish the Work (#2760). The watch starts
# on the engine's next wake, a minute or so after the merge closed the issue.
DEPLOY_GRACE = timedelta(minutes=5)


def deploy_due(
    card: Any,
    outputs: Sequence[OutputFact],
    finished_at: datetime | None,
    now: datetime,
) -> bool:
    """The issue closed under ``DEPLOY_GRACE`` ago, and the Work merged into one of its Poller
    card's repositories that deploys on merge: a deploy comes next, not a person (#2760)."""
    if card is None or finished_at is None:
        return False
    if not timedelta(0) <= now - finished_at < DEPLOY_GRACE:
        return False
    deploys = {name.casefold() for name in getattr(card, "deploy_repos", ()) or ()}
    return any(
        output.state == MERGED and output.repository.casefold() in deploys for output in outputs
    )


def _issue_key(ref: WorkRef) -> str:
    return f"{ref.repository}#{ref.number}".lower()


def _registered_output(ref: WorkRef, item: Registration, row: Mapping[str, Any] | None) -> bool:
    """Whether a pull request one of the Work's chats registered is one of its outputs.

    Only a merged or an open one: a pull request closed without merging is not an output, and
    one the deck never resolved has no state or branch to judge. One still open after its
    issue closed stays an output (#3187): another chat may close the issue while this Work's
    pull request still waits for its review or Merge, which #2228 hid. A branch naming an issue
    speaks for that number only, and for another repository's issue only when the chat that
    registered it is in this Work alone. A branch naming no issue — the review-chat repository's
    ``agentdeck-2272-reviewed-verdict`` — leaves the registration as the only link, which
    holds only for a chat in this Work alone (#2286).
    """
    where = pull_identity(item.url)
    if where is None or pull_state(row) not in {"open", MERGED}:
        return False
    number = issue_number_from_branch(str((row or {}).get("headRefName") or ""))
    if number is None:
        return item.exclusive
    return number == ref.number and (item.exclusive or where[0].lower() == ref.repository.lower())


def _discover(
    ref: WorkRef | None,
    card: Any,
    open_pulls: Sequence[Mapping[str, Any]],
    registered: Sequence[Registration],
    rows: Mapping[str, Mapping[str, Any]],
) -> list[tuple[str, str]]:
    """``(url, found_by)`` for every output, by the one precedence every surface shares.

    The card's own pull requests, when it names any, are the whole answer. Otherwise the
    cached open pull request list says which open ones close the issue, and the Work's
    chats' registrations add theirs. Both speak together (#2286): the open list is re-read
    on its own clock, so a pull request opened a minute ago is only a registration yet, and
    a Work with pull requests in two repositories would otherwise lose the ones the list
    does not join to its issue.
    """
    explicit = card_pull_requests(card)
    if explicit:
        return [(url, FOUND_BY_CARD) for url in explicit]
    if ref is None:
        return []
    key = _issue_key(ref)
    found: list[tuple[str, str]] = []
    for pull in open_pulls:
        url = str(pull.get("url") or "")
        if not url:
            continue
        closes = {str(item).lower() for item in pull.get("closes") or ()}
        if key in closes:
            found.append((url, FOUND_BY_CLOSING))
        elif not closes:
            # GitHub named the issues it closes, and this is not one: its branch says nothing.
            found.append((url, FOUND_BY_BRANCH))
    for item in registered:
        if _registered_output(ref, item, rows.get(item.url.lower())):
            found.append((item.url, FOUND_BY_CHAT))
    return found


def discovery_urls(
    ref: WorkRef | None,
    card: Any,
    open_pulls: Sequence[Mapping[str, Any]] = (),
    registered: Sequence[Registration] = (),
) -> tuple[str, ...]:
    """Every pull request whose cached row ``work_facts`` may read for this Work."""
    urls = [*card_pull_requests(card), *(str(pull.get("url") or "") for pull in open_pulls)]
    urls += [item.url for item in registered]
    return tuple(dict.fromkeys(url for url in urls if url))


def _changes_at(reviewer: Any, where: tuple[str, int], head: str) -> datetime | None:
    """When the reviewer sent this head back (#2332); the epoch of 0 an unknown time."""
    reader = getattr(reviewer, "changes_at", None)
    at = reader(*where, head) if reader is not None else None
    if at is None:
        return None
    return datetime.fromtimestamp(at, UTC) if at > 0 else datetime.fromtimestamp(0, UTC)


def work_facts(
    ref: WorkRef | None,
    *,
    card: Any = None,
    record: Mapping[str, Any] | None = None,
    rows: Mapping[str, Mapping[str, Any]] | None = None,
    open_pulls: Sequence[Mapping[str, Any]] = (),
    registered: Sequence[Registration] = (),
    checks: Mapping[str, Mapping[str, Any]] | None = None,
    chat_holds: Mapping[str, Sequence[str]] | None = None,
) -> WorkFacts:
    """One Work's facts. Pure: every input is a local read the caller made once per pass.

    ``rows`` is the cached row of each pull request by lowercased URL; ``open_pulls`` the
    cached open pull requests joined to this issue (``IssueBacklog.pulls_for_work``);
    ``registered`` the pull requests the Work's chats registered; and ``checks`` the cached
    ``merge_checks`` row of each armed or mergeable-candidate pull request, by lowercased URL.
    ``chat_holds`` are the labels each open chat waits on a person with, by casefolded
    ``owner/repo#number`` (#2442).
    """
    rows = rows or {}
    chat_holds = chat_holds or {}
    checks = checks or {}
    reviewer = getattr(card, "reviewer", None)
    finished_at = work_finished_at(card, record)
    merged_by_engine = {url.lower() for url in engine_merged(card)}
    review_only = {str(name).casefold() for name in getattr(card, "review_only_repos", ()) or ()}
    seen: set[str] = set()
    outputs = []
    discovered = _discover(ref, card, open_pulls, registered, rows)
    for url, found_by in discovered:
        if url.lower() in seen:
            continue
        seen.add(url.lower())
        row = rows.get(url.lower()) or {}
        where = pull_identity(url)
        state = pull_state(row)
        if url.lower() in merged_by_engine:
            # The engine merged it itself: a row still saying open, or no row, is stale.
            state = MERGED
        head = str(row.get("headRefOid") or "")
        review, detail, clean, changes_at = "", "", False, None
        held: tuple[str, ...] = ()
        if reviewer is not None and where is not None and state not in _SETTLED:
            review, detail = reviewer.judge(*where, head=head)
            if review == "awaiting" and detail:
                held = tuple(chat_holds.get(detail.casefold(), ()))
                review = "held" if held else review
            clean = reviewer.clean_at(*where, head)
            changes_at = _changes_at(reviewer, where, head)
        mergeable, merge_state = (
            merge_answer(checks.get(url.lower()), head) if state not in _SETTLED else ("", "")
        )
        repository, number = where if where is not None else ("", 0)
        additions, deletions, files = source_size(row) or (None, None, None)
        new_lines, new_files = new_source_size(row) or (None, None)
        outputs.append(
            OutputFact(
                url,
                repository,
                number,
                found_by,
                state,
                head,
                review,
                detail,
                base=str(row.get("baseRefName") or ""),
                additions=additions,
                deletions=deletions,
                files=files,
                new_lines=new_lines,
                new_files=new_files,
                review_clean=clean,
                draft=bool(row.get("isDraft")),
                auto_merge=state not in _SETTLED and armed(row),
                never_ran=(
                    never_ran(row, checks.get(url.lower())) if state not in _SETTLED else ()
                ),
                review_changes_at=changes_at,
                mergeable=mergeable,
                merge_state=merge_state,
                file_list=_complete_file_list(row),
                review_held=held,
                review_only=bool(repository) and repository.casefold() in review_only,
                head_at=(
                    head_committed_at(checks.get(url.lower()), head)
                    if state not in _SETTLED
                    else None
                ),
            )
        )
    return WorkFacts(
        ref=ref,
        card=card,
        record=record,
        outputs=tuple(outputs),
        finished_at=finished_at,
    )
