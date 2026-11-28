"""The published label on a Work's issue (#4794): the rule, the intents and the router."""

from __future__ import annotations

import fcntl
import json
import os

import pytest

from agentdeck import work_published, work_rounds
from agentdeck.github_webhook_log import WebhookLog
from agentdeck.models import PullRequestRef
from agentdeck.poller_ops import _manifest
from agentdeck.webhook_tailer import WebhookTailer
from agentdeck.work_published import PASS, PUBLISHED, Ports, PublishedLabels
from agentdeck.work_router import (
    PENDING_MAX_AGE_S,
    PendingActions,
    WorkRouterService,
    pending_path,
)

ISSUE = "acme/issues#5"
REPO = "acme/issues"


class GitHub:
    """Fake reads and writes, recording every write."""

    def __init__(self) -> None:
        self.labels = {REPO: "julkaistu"}
        self.registered: dict[str, list[str]] = {ISSUE: ["acme/app#1", "acme/app#2"]}
        self.states = {"acme/app#1": "MERGED", "acme/app#2": "MERGED"}
        self.issue: dict[tuple[str, int], list[str] | None] = {(REPO, 5): []}
        self.label_exists = True
        self.writes: list[tuple] = []
        self.reads: list[str] = []
        self.fail_edit = False
        self.open: list[tuple[int, list[str]]] | None = []
        self.during = None  # run inside the first pull read: a trigger landing mid-sync

    def ports(self) -> Ports:
        async def pull_state(pull):
            self.reads.append(pull)
            if self.during is not None:
                during, self.during = self.during, None
                during()
            return self.states.get(pull, "")

        async def issue_labels(repository, number):
            names = self.issue.get((repository, number))
            return None if names is None else list(names)

        async def edit_label(repository, number, label, add):
            self.writes.append(("add" if add else "remove", repository, number, label))
            if self.fail_edit or (add and not self.label_exists):
                return False
            names = self.issue.setdefault((repository, number), []) or []
            if add:
                names.append(label)
            else:
                names[:] = [name for name in names if name.casefold() != label.casefold()]
            self.issue[(repository, number)] = names
            return True

        async def create_label(repository, label):
            self.writes.append(("create", repository, label))
            self.label_exists = True
            return True

        async def open_issues(repository):
            return self.open

        return Ports(
            labels=lambda: dict(self.labels),
            registered=lambda issue: list(self.registered.get(issue, [])),
            registered_issues=lambda repository: {
                int(issue.partition("#")[2])
                for issue in self.registered
                if issue.startswith(f"{repository}#")
            },
            pull_state=pull_state,
            issue_labels=issue_labels,
            edit_label=edit_label,
            create_label=create_label,
            open_issues=open_issues,
        )


@pytest.fixture
def gh():
    return GitHub()


@pytest.fixture
def setup(tmp_path, gh):
    pending = PendingActions(tmp_path / "pending.json")
    clock = [1000.0]
    labels = PublishedLabels(gh.ports(), tmp_path / "pending.json", lambda: clock[0])
    return pending, labels, clock


async def run(pending, labels, issue=ISSUE):
    key = work_published.key_of(issue)
    work_published.request_into(pending, [f"github_issue:{issue}"], 1000.0)
    await labels.sync(pending, key, dict(pending.entries[key]))
    return pending.entries.get(key)


async def test_every_merged_pull_request_puts_the_label_on(setup, gh):
    pending, labels, _ = setup
    assert await run(pending, labels) is None  # settled: the intent is gone
    assert gh.writes == [("add", REPO, 5, "julkaistu")]
    # Already right: the next sync reads and writes nothing.
    gh.writes.clear()
    assert await run(pending, labels) is None
    assert gh.writes == []


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
async def test_one_open_or_closed_without_merging_takes_it_off(setup, gh, state):
    pending, labels, _ = setup
    gh.issue[(REPO, 5)] = ["Julkaistu"]  # GitHub's labels are not case-sensitive
    gh.states["acme/app#1"] = state
    assert await run(pending, labels) is None
    assert gh.writes == [("remove", REPO, 5, "julkaistu")]
    assert gh.reads == ["acme/app#1"]  # one surely unmerged settles it


async def test_no_registered_pull_request_is_not_published(setup, gh):
    pending, labels, _ = setup
    gh.registered[ISSUE] = []
    gh.issue[(REPO, 5)] = ["julkaistu"]
    assert await run(pending, labels) is None
    assert gh.writes == [("remove", REPO, 5, "julkaistu")]


async def test_an_unread_pull_request_takes_it_off_and_is_tried_again(setup, gh):
    pending, labels, clock = setup
    gh.issue[(REPO, 5)] = ["julkaistu"]
    del gh.states["acme/app#2"]
    entry = await run(pending, labels)
    assert gh.writes == [("remove", REPO, 5, "julkaistu")]
    assert entry is not None and entry["next_at"] > clock[0]  # on the back-off


async def test_an_unread_issue_is_never_labelled(setup, gh):
    pending, labels, _ = setup
    gh.issue[(REPO, 5)] = None
    assert await run(pending, labels) is not None
    assert gh.writes == []  # all merged, but unread labels: nothing added
    gh.states["acme/app#1"] = "OPEN"
    assert await run(pending, labels) is not None
    assert gh.writes == [("remove", REPO, 5, "julkaistu")]  # off on a guess, never on


async def test_unread_registrations_take_it_off(setup, gh):
    pending, labels, _ = setup
    gh.issue[(REPO, 5)] = ["julkaistu"]

    def broken(issue):
        raise RuntimeError("database is locked")

    labels.ports = labels.ports.__class__(**{**labels.ports.__dict__, "registered": broken})
    assert await run(pending, labels) is not None
    assert gh.writes == [("remove", REPO, 5, "julkaistu")]


async def test_a_missing_label_is_created_once_and_added(setup, gh):
    pending, labels, _ = setup
    gh.label_exists = False
    assert await run(pending, labels) is None
    assert gh.writes == [
        ("add", REPO, 5, "julkaistu"),
        ("create", REPO, "julkaistu"),
        ("add", REPO, 5, "julkaistu"),
    ]


async def test_a_failed_write_stays_pending_past_a_day(setup, gh):
    pending, labels, _ = setup
    gh.fail_edit = True
    entry = await run(pending, labels)
    assert entry is not None and entry["kind"] == PUBLISHED


async def test_a_repository_without_the_setting_is_left_alone(setup, gh):
    pending, labels, _ = setup
    gh.labels = {}
    assert await run(pending, labels) is None
    assert gh.writes == [] and gh.reads == []


async def test_a_sync_held_elsewhere_waits_without_blocking(setup, gh):
    pending, labels, clock = setup
    fd = os.open(labels.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another process's sync
    try:
        key = work_published.key_of(ISSUE)
        work_published.request_into(pending, [ISSUE], clock[0])
        token = pending.entries[key]["requested"]
        await labels.sync(pending, key, dict(pending.entries[key]))
    finally:
        os.close(fd)
    assert gh.reads == [] and gh.writes == []
    entry = pending.entries[key]
    assert entry["requested"] == token  # the re-arm keeps it
    assert entry["next_at"] == clock[0] + work_published.LOCK_WAIT_S


async def test_a_trigger_during_a_sync_gets_a_sync_of_its_own(setup, gh):
    pending, labels, _ = setup
    key = work_published.key_of(ISSUE)
    # A new open pull request is registered while the first sync reads.
    gh.during = lambda: work_published.request_into(pending, [ISSUE], 1000.0)
    work_published.request_into(pending, [ISSUE], 1000.0)
    await labels.sync(pending, key, dict(pending.entries[key]))
    entry = pending.entries[key]
    assert entry["next_at"] == 1000.0  # still due, for the next sync
    gh.registered[ISSUE].append("acme/app#3")
    gh.states["acme/app#3"] = "OPEN"
    await labels.sync(pending, key, dict(entry))
    assert key not in pending.entries
    assert gh.writes == [("add", REPO, 5, "julkaistu"), ("remove", REPO, 5, "julkaistu")]


async def test_turning_it_on_passes_over_the_repository_once(setup, gh):
    pending, labels, _ = setup
    gh.registered = {ISSUE: ["acme/app#1"], "acme/issues#6": ["acme/app#4"]}
    gh.open = [(5, []), (6, []), (7, ["julkaistu"]), (8, ["bug"])]
    assert labels.check_settings(pending) == [REPO]
    assert labels.check_settings(pending) == []  # recorded
    key = f"{PASS}:{REPO}"
    await labels.run_pass(pending, key, dict(pending.entries[key]))
    assert sorted(pending.entries) == [
        "published:acme/issues#5",
        "published:acme/issues#6",
        "published:acme/issues#7",  # labelled with no Work: it loses the label
    ]
    gh.labels[REPO] = "released"
    assert labels.check_settings(pending) == [REPO]


async def test_an_unread_issue_list_keeps_the_pass(setup, gh):
    pending, labels, clock = setup
    gh.open = None
    labels.check_settings(pending)
    key = f"{PASS}:{REPO}"
    await labels.run_pass(pending, key, dict(pending.entries[key]))
    assert pending.entries[key]["next_at"] > clock[0]


def test_changed_works_names_both_sides_of_a_move():
    before = [PullRequestRef("acme/app", 1, work_id="github_issue:acme/issues#5")]
    after = [PullRequestRef("acme/app", 1, work_id="github_issue:acme/issues#6")]
    assert work_published.changed_works(before, after) == {
        "github_issue:acme/issues#5",
        "github_issue:acme/issues#6",
    }
    assert work_published.changed_works(before, before) == set()
    assert work_published.issue_of("github_issue:Acme/Issues#5") == "acme/issues#5"
    assert work_published.issue_of("not a work") == ""


def test_a_new_round_records_an_intent(tmp_path, monkeypatch):
    nudged = []
    work_published.configure(
        PendingActions(pending_path(tmp_path / "github-webhooks")),
        nudge=lambda: nudged.append(1),
    )
    try:

        class Db:
            def start_work_round(self, work_id, earlier, reason):
                return True

        assert work_rounds.start(Db(), "github_issue:acme/issues#5", (), "follow_up")
    finally:
        work_published.configure(None)
    pending = PendingActions(pending_path(tmp_path / "github-webhooks"))
    assert "published:acme/issues#5" in pending.entries and nudged == [1]


def test_registering_or_moving_a_pull_request_records_an_intent(tmp_path):
    from agentdeck.db import Db
    from agentdeck.state import AppState

    control = Db(tmp_path / "control.db")
    key = "codex:test:thread-1"
    work = "github_issue:acme/issues#5"
    pending_file = pending_path(tmp_path / "github-webhooks")
    work_published.configure(PendingActions(pending_file))
    try:
        control.ensure_session_chats((key,))
        state = AppState(db=control, budget_db=control)
        state.set_pull_requests(key, (PullRequestRef("acme/app", 1, work_id=work),))
        assert list(PendingActions(pending_file).entries) == ["published:acme/issues#5"]
        PendingActions(pending_file).discard("published:acme/issues#5")
        state.set_pull_requests(key, (PullRequestRef("acme/app", 1, work_id=work),))
        assert PendingActions(pending_file).entries == {}  # nothing changed
        state.set_pull_requests(key, ())  # dropped: the Work is synced again
        assert list(PendingActions(pending_file).entries) == ["published:acme/issues#5"]
    finally:
        work_published.configure(None)
        control.close()


def test_the_manifest_key_is_read_only_on_an_issue_repository(tmp_path):
    path = tmp_path / "poller.toml"
    path.write_text(
        '[board]\norg = "acme"\n'
        '[[repos]]\nid = "issues"\nholds_issues = true\npublished_label = " julkaistu "\n'
        '[[repos]]\nid = "app"\nholds_code = true\npublished_label = "julkaistu"\n'
    )
    assert _manifest(path).published_labels == (("issues", "julkaistu"),)


# --- the router ---------------------------------------------------------------------------


async def test_a_registered_pull_request_merging_is_synced_by_the_router(tmp_path, gh):
    root = tmp_path / "github-webhooks"
    payload = {
        "repository": {"full_name": "acme/app", "name": "app", "owner": {"login": "acme"}},
        "action": "closed",
        "sender": {"login": "someone", "type": "User"},
        "pull_request": {"number": 1, "merged": True, "state": "closed"},
    }
    WebhookLog(root).append(
        body=json.dumps(payload).encode(),
        headers={"X-GitHub-Delivery": "d1", "X-GitHub-Event": "pull_request"},
    )
    now = [1000.0]
    published = PublishedLabels(gh.ports(), pending_path(root), lambda: now[0])

    async def start_review(*args, **kwargs):
        return None

    service = WorkRouterService(
        root,
        switched=lambda repository: False,  # an undeclared repository: still noted
        owner_of_issue=lambda repository, number: None,
        owner_of_pull=lambda repository, number: None,  # a let-go chat: still synced
        schedule=lambda message: message,
        start_review=start_review,
        clock=lambda: now[0],
        published=published,
        works_of_pull=lambda repository, number: (
            ["github_issue:acme/issues#5"] if (repository, number) == ("acme/app", 1) else []
        ),
    )
    tailer = WebhookTailer(root, consumer="work-router")
    await service.apply(list(tailer.deliveries()))
    assert "published:acme/issues#5" in service.pending.entries
    # Older than a day is no reason to drop it.
    entry = service.pending.entries["published:acme/issues#5"]
    service.pending.put(
        "published:acme/issues#5", {"since": now[0] - PENDING_MAX_AGE_S - 1}, now[0], at=now[0]
    )
    del entry
    await service.retry_pending()
    # Handed to a task of its own, and moved past now so the deadline loop does not spin.
    assert service.pending.entries["published:acme/issues#5"]["next_at"] > now[0]
    await service._published_task
    assert gh.writes == [("add", REPO, 5, "julkaistu")]
    assert "published:acme/issues#5" not in service.pending.entries


async def test_a_slow_sync_holds_up_no_other_action_and_a_failure_backs_off(tmp_path, gh):
    import asyncio

    root = tmp_path / "github-webhooks"
    now = [1000.0]
    published = PublishedLabels(gh.ports(), pending_path(root), lambda: now[0])
    release = asyncio.Event()
    slow = gh.ports().pull_state

    async def pull_state(pull):
        await release.wait()
        return await slow(pull)

    published.ports = Ports(**{**published.ports.__dict__, "pull_state": pull_state})

    async def start_review(*args, **kwargs):
        return None

    service = WorkRouterService(
        root,
        switched=lambda repository: True,
        owner_of_issue=lambda repository, number: None,
        owner_of_pull=lambda repository, number: None,
        schedule=lambda message: message,
        start_review=start_review,
        clock=lambda: now[0],
        published=published,
    )
    work_published.request_into(service.pending, [ISSUE, "acme/issues#6"], now[0])
    service.pending.put("other:x", {"kind": "unknown-kind"}, now[0], at=now[0])
    await service.retry_pending()  # returns while the sync waits on GitHub
    assert "other:x" not in service.pending.entries
    # A second pass while it runs starts no second task and only looks again later.
    now[0] += work_published.LOCK_WAIT_S
    task = service._published_task
    await service.retry_pending()
    assert service._published_task is task
    gh.registered["acme/issues#6"] = ["acme/app#1"]

    def broken():
        raise OSError("disk full")

    calls = [0]
    labels = published.ports.labels

    def labels_once_then_broken():
        calls[0] += 1
        if calls[0] > 1:
            broken()
        return labels()

    published.ports = Ports(**{**published.ports.__dict__, "labels": labels_once_then_broken})
    release.set()
    await task
    assert "published:acme/issues#5" not in service.pending.entries
    entry = service.pending.entries["published:acme/issues#6"]  # failed: on the back-off
    assert entry["next_at"] > now[0]


async def test_a_nudge_from_a_worker_thread_reaches_the_loop(tmp_path):
    import asyncio

    nudged = asyncio.Event()
    work_published.configure(PendingActions(pending_path(tmp_path)), nudge=nudged.set)
    try:
        keys = await asyncio.to_thread(work_published.request, [ISSUE])
        await asyncio.wait_for(nudged.wait(), 1)
    finally:
        work_published.configure(None)
    assert keys == ["published:acme/issues#5"]


# --- the review's round: an unread read is never "none" (#4794 review) --------------------


def _db_with_pull(tmp_path):
    from agentdeck.db import Db

    db = Db(tmp_path / "control.db")
    db.ensure_session_chats(("codex:test:thread-1",))
    db.record_session_pull_request(
        "codex:test:thread-1", PullRequestRef("acme/app", 1, work_id=f"github_issue:{ISSUE}")
    )
    return db


async def test_an_unread_registrations_table_keeps_the_sync_and_the_pass(tmp_path, gh):
    db = _db_with_pull(tmp_path)
    try:
        registrations = work_published.Registrations(db)
        assert registrations.registered(ISSUE) == ("acme/app#1",)
        assert registrations.registered_issues(REPO) == {5}
        assert registrations.works_of_pull("acme/app", 1) == [f"github_issue:{ISSUE}"]
        db._conn.execute("DROP TABLE session_pull_requests")  # noqa: SLF001 - a real failure
        for read in (
            lambda: registrations.registered(ISSUE),
            lambda: registrations.registered_issues(REPO),
            lambda: registrations.works_of_pull("acme/app", 1),
        ):
            with pytest.raises(Exception, match="session_pull_requests"):
                read()
        pending = PendingActions(tmp_path / "pending.json")
        ports = Ports(
            **{
                **gh.ports().__dict__,
                "registered": registrations.registered,
                "registered_issues": registrations.registered_issues,
            }
        )
        labels = PublishedLabels(ports, tmp_path / "pending.json", lambda: 1000.0)
        gh.issue[(REPO, 5)] = ["julkaistu"]
        assert await run(pending, labels) is not None  # the sync stays pending
        assert gh.writes == [("remove", REPO, 5, "julkaistu")]  # off, never on
        gh.open = [(5, ["julkaistu"])]
        labels.check_settings(pending)
        key = f"{PASS}:{REPO}"
        await labels.run_pass(pending, key, dict(pending.entries[key]))
        assert key in pending.entries  # the backfill is tried again, not skipped
    finally:
        db.close()


async def test_an_unread_rounds_table_takes_the_label_off(tmp_path):
    db = _db_with_pull(tmp_path)
    try:
        db._conn.execute("DROP TABLE work_rounds")  # noqa: SLF001 - a real failure
        with pytest.raises(Exception, match="work_rounds"):
            work_published.Registrations(db).registered(ISSUE)
    finally:
        db.close()


def _router(root, now, published=None, works_of_pull=lambda repository, number: ()):
    async def start_review(*args, **kwargs):
        return None

    return WorkRouterService(
        root,
        switched=lambda repository: True,
        owner_of_issue=lambda repository, number: None,
        owner_of_pull=lambda repository, number: None,
        schedule=lambda message: message,
        start_review=start_review,
        clock=lambda: now[0],
        published=published,
        works_of_pull=works_of_pull,
    )


async def test_a_merge_not_recorded_is_not_acknowledged(tmp_path, gh):
    root = tmp_path / "github-webhooks"
    WebhookLog(root).append(
        body=json.dumps(
            {
                "repository": {"full_name": "acme/app", "name": "app", "owner": {"login": "acme"}},
                "action": "closed",
                "sender": {"login": "someone", "type": "User"},
                "pull_request": {"number": 1, "merged": True, "state": "closed"},
            }
        ).encode(),
        headers={"X-GitHub-Delivery": "d1", "X-GitHub-Event": "pull_request"},
    )
    now = [1000.0]
    published = PublishedLabels(gh.ports(), pending_path(root), lambda: now[0])
    unread = [True]

    def works_of_pull(repository, number):
        if unread[0]:
            raise RuntimeError("database is locked")
        return [f"github_issue:{ISSUE}"]

    service = _router(root, now, published, works_of_pull)
    tailer = WebhookTailer(root, consumer="work-router")
    with pytest.raises(RuntimeError):  # so ``_route`` commits no offset and reads it again
        await service.apply(list(tailer.deliveries()))
    assert service.pending.entries == {}
    unread[0] = False
    broken = service.pending.put

    def full_disk(*args, **kwargs):
        raise OSError("disk full")

    service.pending.put = full_disk
    with pytest.raises(OSError):
        await service.apply(list(tailer.deliveries()))
    service.pending.put = broken
    await service.apply(list(tailer.deliveries()))
    assert f"published:{ISSUE}" in service.pending.entries


async def test_a_web_configured_before_its_loop_nudges_from_a_thread_once_bound(tmp_path):
    import asyncio

    nudged = asyncio.Event()
    pending = PendingActions(pending_path(tmp_path))
    # ``create_app`` runs before ``asyncio.run``: no loop to capture yet.
    await asyncio.to_thread(work_published.configure, pending, nudged.set)
    try:
        work_published.bind_loop()  # the lifespan, once the loop runs
        await asyncio.to_thread(work_published.request, [ISSUE])  # a round in a worker thread
        await asyncio.wait_for(nudged.wait(), 1)
    finally:
        work_published.configure(None)


def test_an_unreadable_manifest_is_not_a_label_turned_off(tmp_path):
    from agentdeck.work_release import Policies

    path = tmp_path / "instances" / "acme" / "poller.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[board]\norg = "acme"\n'
        '[[repos]]\nid = "issues"\nholds_issues = true\npublished_label = "julkaistu"\n'
    )
    policies = Policies(tmp_path)
    assert policies.published_labels() == {REPO: "julkaistu"}
    path.write_text("[board\n")  # a half-written file
    with pytest.raises(OSError, match="unreadable"):
        policies.published_labels()
    path.write_text('[board]\norg = "acme"\n[[repos]]\nid = "issues"\nholds_issues = true\n')
    assert policies.published_labels() == {}  # really turned off
