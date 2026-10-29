"""The deck starts a project's Work itself (#2895 step 5b, ADR 0008 D2).

The router turns the ownership label landing on an issue no chat owns into one start, and
``start_work_agent`` starts one Claude chat with the ``work`` role at most once per Work.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_work_router import comment, deliver

from agentdeck import work_claims, work_signals, work_start
from agentdeck.inject import InjectResult
from agentdeck.poller_ops import work_accounts, work_start_labels
from agentdeck.webhook_tailer import WebhookTailer
from agentdeck.work import WorkRef
from agentdeck.work_router import HANDED_OVER, WorkRouterService
from agentdeck.work_start import start_action_id, start_work_agent

ISSUE = "eerovil/agentdeck"
#: The real one: every test here but its own sees the ``announced`` fake (#3109).
ANNOUNCE_START = work_signals.announce_start


def labelled(
    root,
    delivery: str,
    *,
    label="agent",
    number=7,
    state="open",
    on_pull=False,
    labels=(),
    action="labeled",
    assignee="",
):
    issue = {
        "number": number,
        "state": state,
        "labels": [{"name": name} for name in labels],
        "assignee": {"login": assignee} if assignee else None,
    }
    if on_pull:
        issue["pull_request"] = {"url": "x"}
    deliver(
        root,
        event="issues",
        delivery=delivery,
        action=action,
        issue=issue,
        label={"name": label},
        sender={"login": "eerovil", "type": "User"},
    )


def coworkers_deck(repository: str, assignee: str) -> bool:
    """The rule on a deck whose person is ``eerovil`` and that leaves unassigned issues."""
    return work_claims.mine(assignee, person="eerovil", takes_unassigned=False)


async def route(root, *, owners=None, switched=True, labels=None, **kwargs):
    service, started = router(root, owners=owners, switched=switched, labels=labels, **kwargs)
    tailer = WebhookTailer(root, consumer="work-router")
    await service.apply(list(tailer.deliveries()))
    tailer.commit()
    return started


def router(root, *, owners=None, switched=True, labels=None, **kwargs):
    started: list[tuple[str, int]] = []

    async def start_work(repository: str, number: int, **kwargs) -> str:
        started.append((repository, number))
        if labels is not None:
            labels.append(kwargs["labels"])
        return "started"

    async def start_review(pull, work):  # pragma: no cover - no review in these cases
        return None

    service = WorkRouterService(
        root,
        switched=lambda repository: switched,
        owner_of_issue=lambda repository, number: (owners or {}).get((repository, number)),
        owner_of_pull=lambda repository, number: None,
        schedule=kwargs.pop("schedule", lambda message: message),
        start_review=start_review,
        own_logins=frozenset(),
        start_work=start_work,
        start_labels=lambda repository: ("Agent",) if repository == ISSUE else (),
        **kwargs,
    )
    return service, started


@pytest.fixture
def root(tmp_path):
    return tmp_path / "github-webhooks"


async def test_the_ownership_label_on_an_unowned_issue_starts_its_work(root):
    """#3134: with the issue's labels, so a spec-only issue's agent writes the spec."""
    labelled(root, "d1", labels=("agent", "spec-only"))
    seen: list = []
    assert await route(root, labels=seen) == [(ISSUE, 7)]
    assert seen == [["agent", "spec-only"]]


@pytest.mark.parametrize(
    "kwargs",
    [{"label": "bug"}, {"state": "closed"}, {"on_pull": True}],
    ids=["another label", "closed issue", "pull request"],
)
async def test_only_the_ownership_label_on_an_open_issue_starts_anything(root, kwargs):
    labelled(root, "d1", **kwargs)
    assert await route(root) == []


@pytest.mark.parametrize(
    ("assignee", "person", "takes_unassigned", "mine"),
    [
        ("coworker", "", False, True),  # no person named: as before #3731
        ("", "eerovil", True, True),
        ("", "eerovil", False, False),
        ("EeroVil", "eerovil", False, True),
        ("coworker", "eerovil", True, False),
    ],
)
def test_whose_issue_it_is(assignee, person, takes_unassigned, mine):
    assert work_claims.mine(assignee, person=person, takes_unassigned=takes_unassigned) is mine


@pytest.mark.parametrize(
    ("assignee", "action", "started"),
    [
        ("Eerovil", "labeled", True),
        ("", "labeled", False),
        ("coworker", "labeled", False),
        ("eerovil", "assigned", True),
        ("coworker", "assigned", False),
        ("", "unassigned", False),
    ],
)
async def test_only_an_issue_assigned_to_this_decks_person_starts_here(
    root, assignee, action, started
):
    """#3731: two decks on one repository; the issue's first assignee says which one works it.
    An assignment to this deck's person starts an issue that already carries the label."""
    labelled(root, "d1", labels=("agent",), action=action, assignee=assignee)
    assert await route(root, mine=coworkers_deck) == ([(ISSUE, 7)] if started else [])


async def test_an_assignment_waits_while_the_deck_files_in_its_repository(root):
    """#3731 review: a New work filed with its assignee sends an assignment delivery, which
    must wait for the filing like the label's (#3590), not start a second agent."""
    service, started = router(root, mine=coworkers_deck)
    labelled(root, "d1", labels=("agent",), action="assigned", assignee="eerovil")
    tailer = WebhookTailer(root, consumer="work-router")

    with service.filing(ISSUE):
        await service.apply(list(tailer.deliveries()))

    assert started == []
    assert service.pending.entries[f"start:github_issue:{ISSUE}#7"]["assignee"] == "eerovil"


async def test_a_retried_start_rechecks_whose_issue_it_is(root):
    """#3731: a start kept for its retry is not made once the issue went to another deck."""
    service, started = router(root, mine=coworkers_deck)
    key = f"start:github_issue:{ISSUE}#7"
    fields = {"kind": "start", "repository": ISSUE, "number": 7, "labels": ["agent"]}
    service.pending.put(key, {**fields, "assignee": "coworker"}, 0.0)
    await service._retry_start(key, service.pending.entries[key])
    assert started == []
    assert key not in service.pending.entries
    service.pending.put(key, {**fields, "assignee": "eerovil"}, 0.0)
    await service._retry_start(key, service.pending.entries[key])
    assert started == [(ISSUE, 7)]


async def test_a_reassigned_work_stops_its_chat_once_and_hears_when_it_comes_back(root):
    """#3731: the deck that loses an issue interrupts its agent and says who has it; a
    replayed or second assignment to someone else does neither again, and a person's
    ``/claude`` meant for the deck that took it over reaches no chat here."""
    sent: list = []
    interrupted: list = []

    async def interrupt(chat_id):
        interrupted.append(chat_id)
        return True

    async def authorized(repository, login, association):
        return True

    ports = {
        "owners": {(ISSUE, 7): ("chat-1", "eerovil/agentdeck#7")},
        "mine": coworkers_deck,
        "interrupt": interrupt,
        "schedule": sent.append,
        "authorized": authorized,
    }
    labelled(root, "d1", labels=("agent",), action="assigned", assignee="coworker")
    labelled(root, "d2", labels=("agent",), action="assigned", assignee="someone")
    comment(root, "d3", comment_id=101)
    assert await route(root, **ports) == []
    assert interrupted == ["chat-1"]
    assert [message.chat_id for message in sent] == ["chat-1"]
    assert "reassigned to @coworker" in sent[0].text

    labelled(root, "d4", labels=("agent",), action="assigned", assignee="eerovil")
    comment(root, "d5", comment_id=102)
    await route(root, **ports)
    assert interrupted == ["chat-1"]
    assert len(sent) == 3 and "yours again" in sent[1].text
    assert "please also fix X" in sent[2].text


@pytest.mark.parametrize(
    ("login", "handed_over", "warned"),
    [
        ("their-deck[bot]", False, True),
        ("our-deck[bot]", False, False),
        ("their-deck[bot]", True, False),  # this deck handed the issue over on purpose
    ],
)
async def test_another_decks_start_comment_warns_the_works_chat(root, login, handed_over, warned):
    """#3731: another deck says it started an agent on an issue this deck works."""
    sent: list = []
    deliver(
        root,
        event="issue_comment",
        delivery="d1",
        action="created",
        sender={"login": login, "type": "Bot"},
        issue={"number": 7},
        comment={
            "id": 5,
            "user": {"login": login, "type": "Bot"},
            "body": work_signals.start_comment(action_id="work-start-x"),
        },
    )
    service, _ = router(
        root,
        owners={(ISSUE, 7): ("chat-1", "eerovil/agentdeck#7")},
        schedule=sent.append,
    )
    service._own_logins = frozenset({"our-deck[bot]"})
    if handed_over:
        service.handed_over.add(f"{HANDED_OVER}github_issue:eerovil/agentdeck#7")
    tailer = WebhookTailer(root, consumer="work-router")
    await service.apply(list(tailer.deliveries()))
    assert [f"@{login}" in message.text for message in sent] == ([True] if warned else [])


async def test_an_issue_a_chat_already_owns_is_not_started_again(root):
    labelled(root, "d1")
    assert await route(root, owners={(ISSUE, 7): ("chat-1", "")}) == []


async def test_a_repository_no_project_declares_starts_nothing(root):
    labelled(root, "d1")
    assert await route(root, switched=False) == []


async def test_an_issue_the_deck_filed_starts_its_work_without_the_webhook(root):
    """GitHub held the new issue's delivery back for minutes (#3371)."""
    owners: dict = {}
    seen: list = []
    service, started = router(root, owners=owners, labels=seen)
    assert service.note_filed("Eerovil/AgentDeck", 7, ["agent", "spec-only"]) is True
    await asyncio.gather(*service._checks)
    assert started == [(ISSUE, 7)]
    assert seen == [["agent", "spec-only"]]

    # GitHub's own delivery, once it comes, finds the chat the start made.
    owners[(ISSUE, 7)] = ("chat-1", "")
    labelled(root, "d1")
    tailer = WebhookTailer(root, consumer="work-router")
    await service.apply(list(tailer.deliveries()))
    assert started == [(ISSUE, 7)]

    assert service.note_filed(ISSUE, 8, ["bug"]) is False
    assert service.note_filed("eerovil/other", 8, ["agent"]) is False
    unswitched, _ = router(root, switched=False)
    assert unswitched.note_filed(ISSUE, 8, ["agent"]) is False
    assert unswitched.starts_on(ISSUE, ["agent"]) == ""


async def test_a_label_delivery_waits_while_the_deck_files_in_its_repository(root):
    """#3590: the deck's early agent may be this very issue's; the router retries later."""
    service, started = router(root)
    assert service.starts_on(ISSUE, ["spec-only", "AGENT"]) == "AGENT"
    labelled(root, "d1")
    tailer = WebhookTailer(root, consumer="work-router")

    with service.filing("Eerovil/AgentDeck"):
        await service.apply(list(tailer.deliveries()))

    assert started == []
    assert f"start:github_issue:{ISSUE}#7" in service.pending.entries
    assert service._filing == {}


# --- the start itself ---


class FakeDb:
    def __init__(self) -> None:
        self.actions: dict[str, tuple[str, str]] = {}
        self.settled: dict[str, tuple[str, str]] = {}
        self.side_chats: dict[str, str] = {}

    def planned_resets(self):
        return {}

    def reserve_work_action(self, action_id, work_id, action):
        if action_id in self.actions:
            return False
        self.actions[action_id] = (work_id, action)
        return True

    def settle_work_action(self, action_id, outcome, result=""):
        self.settled[action_id] = (outcome, result)

    def work_action(self, action_id):
        return self.actions.get(action_id)

    def release_failed_work_action(self, action_id):
        if self.settled.get(action_id, ("",))[0] == "failed":
            self.actions.pop(action_id, None)
            self.settled.pop(action_id, None)

    def record_work_side_chat(self, action_id, work_id, pulls):
        self.side_chats[action_id] = work_id
        return True

    def delete_work_side_chat(self, action_id):
        self.side_chats.pop(action_id, None)

    def claim_start_spare(self, action_id, token, worker_key):
        self.spare_claims = {**getattr(self, "spare_claims", {}), action_id: (token, worker_key)}
        return token, worker_key

    def forget_start_spare_claim(self, action_id):
        getattr(self, "spare_claims", {}).pop(action_id, None)

    def load_manual_new_chat_cwd(self):
        return ""


class FakeInjector:
    def __init__(self, result: InjectResult, answer: InjectResult | None = None) -> None:
        self.result = result
        # What the background spawn answered; ``None`` is unknown (still running, raised).
        self.answer = answer if answer is not None or not result.accepted else result
        self.calls: list[dict] = []
        self.waited: list[str] = []

    async def wait_for_new_start(self, account_key, timeout):
        self.waited.append(account_key)

    async def start_new(self, account, provider, cwd, message, **kwargs):
        self.calls.append({"account": account.key, "cwd": cwd, "message": message, **kwargs})
        return self.result

    def new_start_answer(self, account_key):
        async def answer():
            return self.answer

        return answer()


ACCEPTED = InjectResult(True)


def claimed(request: httpx.Request) -> httpx.Response:
    """The runtime's claim service, making the worktree it was asked for (#3274)."""
    body = json.loads(request.content)
    if request.url.path == work_start.RELEASE_PATH:
        return httpx.Response(200, json={"released": [], "unclaimed": []})
    claim = {"path": body["path"], "branch": body["branch"], "base_ref": body["base_ref"]}
    return httpx.Response(200, json={"outcome": "created", "claim": claim, "detail": ""})


def fake_runtime(handler=claimed) -> tuple[httpx.AsyncClient, list[dict]]:
    asked: list[dict] = []

    def record(request: httpx.Request) -> httpx.Response:
        asked.append({"route": request.url.path, **json.loads(request.content)})
        return handler(request)

    transport = httpx.MockTransport(record)
    return httpx.AsyncClient(transport=transport, base_url="http://agentdeck-runtime"), asked


def fake_app(tmp_path, *, result=ACCEPTED, answer=None, provider_id="claude_code", runtime=claimed):
    account = SimpleNamespace(key=f"{provider_id}:main", provider_id=provider_id)
    db = FakeDb()
    injector = FakeInjector(result, answer)
    config = SimpleNamespace(
        inject=SimpleNamespace(enabled=True, new_chat_default_cwd=""),
        poller=SimpleNamespace(root=str(tmp_path / "poller")),
        usage=SimpleNamespace(shared_cache_dir=str(tmp_path / "usage")),
    )
    state = SimpleNamespace(
        config=config,
        accounts=[account],
        control_db=db,
        db=db,
        injector=injector,
        app_state=SimpleNamespace(all_sessions=lambda: []),
    )
    if runtime is not None:
        state.runtime_http, state.runtime_asked = fake_runtime(runtime)
    return SimpleNamespace(state=state), db, injector


@pytest.fixture(autouse=True)
def announced(monkeypatch):
    """What each start told its issue (#3109); never a real ``gh`` call here."""
    calls: list[tuple] = []

    async def announce_start(repository, number, *, action_id, work_url=""):
        calls.append((repository, number, action_id, work_url))

    monkeypatch.setattr(work_signals, "announce_start", announce_start)
    monkeypatch.setattr("agentdeck.public_origin.public_origin", lambda: "https://deck.example")
    return calls


@pytest.fixture
def startable(monkeypatch, tmp_path):
    checkout = tmp_path / "agentdeck"
    monkeypatch.setattr(work_start, "local_repository_checkout", lambda ref, found: str(checkout))
    monkeypatch.setattr(work_start, "default_branch", lambda main: "master")
    _claude_may_start(monkeypatch)
    return checkout


def _claude_may_start(monkeypatch) -> None:
    # Patched on the provider `_account` actually reads, not on its class: in the full suite
    # the registry's instance need not be the class this module imports, and patching the
    # class then left every start here reading "no account" (#2895 review).
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["claude_code"], "can_start_session", lambda account: True)


async def test_a_work_starts_one_claude_chat_with_the_work_role(tmp_path, startable, announced):
    app, db, injector = fake_app(tmp_path)

    assert await start_work_agent(app, ISSUE, 7) == "started"

    (call,) = injector.calls
    action_id = start_action_id("github_issue:eerovil/agentdeck#7")
    assert call["role"] == "work"
    assert call["origin"] == "work"
    assert call["client_action_id"] == call["session_token"] == action_id
    # #3274: in the Work's own worktree, which the runtime claimed, never the main checkout.
    worktree = startable / ".worktrees" / "issue-7"
    assert call["cwd"] == worktree
    assert app.state.runtime_asked == [
        {
            "route": work_start.ENSURE_PATH,
            "work_id": "github_issue:eerovil/agentdeck#7",
            "repository": ISSUE,
            "purpose": "implementation",
            "checkout": str(startable),
            "path": str(worktree),
            "branch": "agent/issue-7",
            "base_ref": "master",
            "legacy_paths": [],
        }
    ]
    assert call["message"].startswith("Context: Work for eerovil/agentdeck#7.")
    # No manifest says how this project merges, so the role's guide covers both cases.
    assert call["message"].endswith(
        work_start.FIRST_TEXT
        + " "
        + work_start.WORKTREE_TEXT.format(path=worktree, branch="agent/issue-7", main=startable)
    )
    # A Work side chat underneath: it joins the Work once it exists.
    assert db.side_chats == {action_id: "github_issue:eerovil/agentdeck#7"}
    await _settled()
    assert db.settled[action_id][0] == "done"
    # #3109: the issue shows the agent is on it, with a link to the Work page.
    assert announced == [
        (ISSUE, 7, action_id, "https://deck.example/works/github_issue:eerovil%2Fagentdeck%237")
    ]


def _answer(status: int, body: dict):
    return lambda request: httpx.Response(status, json=body)


@pytest.mark.parametrize(
    ("runtime", "branch", "why"),
    [
        (_answer(422, {"detail": "already claimed"}), "master", "already claimed"),
        (_answer(200, {"outcome": "failed", "detail": "fetch failed"}), "master", "fetch failed"),
        (None, "master", "the runtime is not reachable"),
        (claimed, "", "default branch could not be read"),
    ],
    ids=["refused", "failed", "no runtime", "no default branch"],
)
async def test_a_work_with_no_worktree_starts_in_the_main_checkout_saying_why(
    tmp_path, startable, monkeypatch, runtime, branch, why
):
    """#3274: a claim that cannot be made never stalls the Work; its agent is told."""
    monkeypatch.setattr(work_start, "default_branch", lambda main: branch)
    app, _db, injector = fake_app(tmp_path, runtime=runtime)

    assert await start_work_agent(app, ISSUE, 7) == "started"

    (call,) = injector.calls
    assert call["cwd"] == startable
    assert why in call["message"]
    assert call["message"].endswith(
        f"make `{startable / '.worktrees' / 'issue-7'}` on `agent/issue-7` yourself before"
        " editing anything."
    )
    await _settled()


async def test_an_earlier_worktree_the_claim_will_not_take_is_still_where_it_starts(
    tmp_path, startable, monkeypatch
):
    """#3274: one holding unpushed work is the Work's, unclaimed, so Finish never removes it."""
    monkeypatch.setattr(work_start, "_existing_worktree", lambda path, repository: True)
    legacy = _answer(200, {"outcome": "legacy", "detail": "it has uncommitted changes"})
    app, _db, injector = fake_app(tmp_path, runtime=legacy)

    assert await start_work_agent(app, ISSUE, 7) == "started"

    assert injector.calls[0]["cwd"] == startable / ".worktrees" / "issue-7"
    assert "never edit the main checkout" in injector.calls[0]["message"]
    await _settled()


def test_the_main_checkout_ignores_its_worktrees_locally_once(tmp_path):
    """#3274: ``~/choir-tracks-cf`` showed ``?? .worktrees/`` once Works made them there."""
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    (info / "exclude").write_text("# git ls-files --others --exclude-from=.git/info/exclude")

    work_start.exclude_worktrees(str(tmp_path))
    work_start.exclude_worktrees(str(tmp_path))

    assert (info / "exclude").read_text().splitlines()[1:] == [".worktrees/"]
    work_start.exclude_worktrees(str(tmp_path / "not-a-checkout"))  # nothing to write, no error


async def test_a_finished_work_asks_the_runtime_to_release_its_worktrees(tmp_path):
    app, _db, _injector = fake_app(tmp_path)
    await work_start.release_work_worktrees(app, "github_issue:eerovil/agentdeck#7")
    assert app.state.runtime_asked == [
        {"route": work_start.RELEASE_PATH, "work_id": "github_issue:eerovil/agentdeck#7"}
    ]

    def down(request):
        raise httpx.ConnectError("no socket")

    app, _db, _injector = fake_app(tmp_path, runtime=down)
    await work_start.release_work_worktrees(app, "github_issue:eerovil/agentdeck#7")  # no raise


async def test_a_second_start_of_the_same_work_starts_nothing(tmp_path, startable):
    app, _, injector = fake_app(tmp_path)

    await start_work_agent(app, ISSUE, 7)
    assert await start_work_agent(app, ISSUE, 7) == "already started"
    assert len(injector.calls) == 1


class FakeSpares:
    """The Work spare a New work form kept booted (#3590)."""

    def __init__(self, folder: str) -> None:
        self.folder = folder
        self.events: list[str] = []

    def claim_work(self, repository, *, account_key):
        self.events.append(f"claim {repository} {account_key}")
        return "spare-token", "chat-spare", self.folder

    def replenish_after(self, name, answer):
        self.events.append(f"replenish {name}")

    def release(self, name):
        self.events.append(f"release {name}")


@pytest.fixture
def worker(monkeypatch):
    """What the deck told, and interrupted, the early start's worker (#3590)."""
    from agentdeck.providers import PROVIDERS

    told: list[tuple] = []

    async def message_worker(account, key, message, *, delivery_id):
        told.append(("message", key, message, delivery_id))
        return InjectResult(True)

    async def interrupt_worker(account, key):
        told.append(("interrupt", key))
        return InjectResult(True)

    monkeypatch.setattr(PROVIDERS["claude_code"], "message_worker", message_worker)
    monkeypatch.setattr(PROVIDERS["claude_code"], "interrupt_worker", interrupt_worker)
    return told


async def test_an_early_start_takes_the_work_spare_and_becomes_the_work_once_filed(
    tmp_path, startable, worker, announced, caplog
):
    """#3590: the agent starts before the issue exists and is told its issue a second later."""
    app, db, injector = fake_app(tmp_path)
    spare = str(startable / ".worktrees" / "work-spare-1a2b3c4d")
    app.state.start_spares = FakeSpares(spare)

    early = await work_start.start_before_issue(
        app, ISSUE, request="Chart labels overlap\n\nOn the phone.\n", press="press-1"
    )

    (call,) = injector.calls
    assert call["cwd"] == Path(spare) and call["role"] == "work" and call["origin"] == "work"
    assert (call["session_token"], call["worker_key"]) == ("spare-token", "chat-spare")
    assert call["client_action_id"] == work_start.early_action_id("press-1")
    assert call["message"].startswith("Context: new Work in eerovil/agentdeck.")
    assert call["message"].endswith("Chart labels overlap\n\nOn the phone.")
    assert "Until that message arrives only read" in call["message"]
    assert app.state.start_spares.events == [
        "claim eerovil/agentdeck claude_code:main",
        "replenish work",
    ]
    assert early.warm and db.actions == {}  # no Work yet: nothing is spent

    work_id = "github_issue:eerovil/agentdeck#7"
    url = "https://github.com/eerovil/agentdeck/issues/7"
    # The press names the repository as typed; the router's start is lower-cased (#3629).
    bound = await work_start.bind_before_issue(
        app, early, WorkRef("EeroVil/AgentDeck", 7), url=url, labels=["agent"]
    )

    assert bound == "started"
    action_id = start_action_id(work_id)
    assert db.actions == {action_id: (work_id, "new_chat")}
    # The chat is the Work's under its own token, which the chat link reads back.
    assert db.side_chats == {"spare-token": work_id}
    assert db.spare_claims == {action_id: ("spare-token", "chat-spare")}
    (ensure,) = app.state.runtime_asked
    assert (ensure["path"], ensure["branch"], ensure["spare"]) == (spare, "agent/issue-7", True)
    assert ensure["link"] == str(startable / ".worktrees" / "issue-7")
    ((kind, key, message, delivery_id),) = worker
    assert (kind, key) == ("message", "chat-spare")
    assert delivery_id == work_start.early_action_id("press-1") + ":issue"
    opening = f"Context: Work for eerovil/agentdeck#7.\n\nYour issue is filed: {url}."
    assert message.startswith(opening)
    assert f"own worktree `{spare}`" in message
    caplog.set_level("INFO", logger="agentdeck.work_start")
    await _settled()
    assert db.settled[action_id][0] == "done"
    assert [entry[2] for entry in announced] == ["spare-token"]
    # D16: one timing line per start, every phase since the press, the session last.
    (timing,) = [r.getMessage() for r in caplog.records if "work start timing" in r.getMessage()]
    assert timing.startswith(f"work start timing: {work_id} press=press-1 warm worktree=")
    assert [part.split("=")[0] for part in timing.split()[-5:]] == [
        "worktree",
        "queued",
        "filed",
        "bound",
        "session",
    ]

    # Bound twice (a replay, or a router start that won), the early agent is stopped.
    assert await work_start.bind_before_issue(app, early, WorkRef(ISSUE, 7), url=url) == (
        "already started"
    )
    assert ("interrupt", "chat-spare") in worker


async def test_a_label_or_backstop_start_takes_a_matching_work_spare(
    tmp_path, startable, announced
):
    """#3590 D21: a Work labelled on GitHub starts warm too when a form kept a spare."""
    app, db, injector = fake_app(tmp_path)
    spare = str(startable / ".worktrees" / "work-spare-1a2b3c4d")
    app.state.start_spares = FakeSpares(spare)

    assert await start_work_agent(app, ISSUE, 7) == "started"

    (call,) = injector.calls
    action_id = start_action_id("github_issue:eerovil/agentdeck#7")
    # Still the Work's one start under its own id, in the spare, under the spare's token.
    assert call["client_action_id"] == action_id and call["cwd"] == Path(spare)
    assert (call["session_token"], call["worker_key"]) == ("spare-token", "chat-spare")
    (ensure,) = app.state.runtime_asked
    assert (ensure["path"], ensure["spare"], ensure["branch"]) == (spare, True, "agent/issue-7")
    assert db.side_chats == {"spare-token": "github_issue:eerovil/agentdeck#7"}
    assert db.spare_claims == {action_id: ("spare-token", "chat-spare")}
    assert app.state.start_spares.events == [
        "claim eerovil/agentdeck claude_code:main",
        "replenish work",
    ]
    await _settled()
    assert db.settled[action_id][0] == "done"


async def test_an_early_start_with_no_spare_makes_its_own_worktree(
    tmp_path, startable, monkeypatch
):
    made: list[str] = []
    removed: list[str] = []
    monkeypatch.setattr(
        work_start, "make_spare_worktree", lambda main: made.append(main) or f"{main}/spare"
    )
    monkeypatch.setattr(work_start, "remove_spare_worktree", removed.append)
    app, _db, injector = fake_app(tmp_path)

    early = await work_start.start_before_issue(app, ISSUE, request="Body", press="press-1")

    assert made == [str(startable)] and not early.warm
    assert injector.calls[0]["cwd"] == Path(f"{startable}/spare")
    assert injector.calls[0]["worker_key"].startswith("chat-")
    # A start the injector refuses gives its worktree back and starts nothing.
    refused, _db, _injector = fake_app(tmp_path, result=InjectResult(False, "busy"))
    assert await work_start.start_before_issue(refused, ISSUE, request="B", press="p2") is None
    assert removed == [f"{startable}/spare"]


async def test_an_early_start_the_runtime_refused_is_not_told_and_starts_again(
    tmp_path, startable, worker
):
    """Its spawn refused (``at_capacity``): no message, and the Work starts the usual way."""
    app, db, _injector = fake_app(tmp_path, answer=InjectResult(False, "at_capacity"))
    app.state.start_spares = FakeSpares(str(startable / ".worktrees" / "work-spare-1"))
    early = await work_start.start_before_issue(app, ISSUE, request="Body", press="press-1")
    again: list[str] = []

    await work_start.bind_before_issue(
        app, early, WorkRef(ISSUE, 7), url="u", on_refused=lambda: again.append("start")
    )
    await _settled()

    assert worker == [] and again == ["start"]
    assert db.settled[start_action_id("github_issue:eerovil/agentdeck#7")][0] == "failed"
    # Nothing names the refused chat any more: the usual start's chat is the Work's.
    assert db.spare_claims == {} and db.side_chats == {}


async def test_an_early_start_its_issue_could_not_be_told_to_is_stopped_and_started_again(
    tmp_path, startable, worker, monkeypatch, announced
):
    """Told to wait for that message, it would idle while the Work reads as started."""
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(work_start, "TELL_RETRY_S", (0.0, 0.0))
    tries: list[str] = []

    async def refuse(account, key, message, *, delivery_id):
        tries.append(delivery_id)
        return InjectResult(False, "runtime unreachable")

    monkeypatch.setattr(PROVIDERS["claude_code"], "message_worker", refuse)
    app, db, _injector = fake_app(tmp_path)
    app.state.start_spares = FakeSpares(str(startable / ".worktrees" / "work-spare-1"))
    early = await work_start.start_before_issue(app, ISSUE, request="Body", press="press-1")
    again: list[str] = []

    bound = await work_start.bind_before_issue(
        app, early, WorkRef(ISSUE, 7), url="u", on_refused=lambda: again.append("start")
    )

    assert bound == "not told" and again == ["start"]
    # Three tries of one message, never three messages.
    assert tries == [work_start.early_action_id("press-1") + ":issue"] * 3
    assert ("interrupt", "chat-spare") in worker
    assert db.settled[start_action_id("github_issue:eerovil/agentdeck#7")][0] == "failed"
    assert db.spare_claims == {} and db.side_chats == {} and announced == []


@pytest.mark.parametrize("maybe", [False, True], ids=["refused", "uncertain"])
async def test_an_early_start_whose_issue_was_not_filed_is_stopped_or_told_to_wait(
    tmp_path, startable, worker, maybe
):
    app, _db, _injector = fake_app(tmp_path)
    app.state.start_spares = FakeSpares(str(startable / ".worktrees" / "work-spare-1"))
    early = await work_start.start_before_issue(app, ISSUE, request="Body", press="press-1")

    await work_start.stop_before_issue(app, early, "HTTP 422.", maybe=maybe)

    kinds = [entry[0] for entry in worker]
    assert kinds == (["message"] if maybe else ["interrupt", "message"])

    text = worker[-1][2]
    assert ("does not know whether" in text) is maybe and "(HTTP 422)" in text


async def test_an_early_agent_neither_told_nor_stopped_never_gets_a_second_beside_it(
    tmp_path, startable, worker, monkeypatch
):
    """#3598 review: a refused interrupt must not free the start for another agent."""
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(work_start, "TELL_RETRY_S", (0.0, 0.0))

    async def refuse_message(account, key, message, *, delivery_id):
        return InjectResult(False, "runtime unreachable")

    async def refuse_interrupt(account, key):
        return InjectResult(False, "runtime unreachable")

    monkeypatch.setattr(PROVIDERS["claude_code"], "message_worker", refuse_message)
    monkeypatch.setattr(PROVIDERS["claude_code"], "interrupt_worker", refuse_interrupt)
    app, db, _injector = fake_app(tmp_path)
    app.state.start_spares = FakeSpares(str(startable / ".worktrees" / "work-spare-1"))
    early = await work_start.start_before_issue(app, ISSUE, request="Body", press="press-1")
    again: list[str] = []

    bound = await work_start.bind_before_issue(
        app, early, WorkRef(ISSUE, 7), url="u", on_refused=lambda: again.append("start")
    )

    assert bound == "not told, not stopped" and again == []
    action_id = start_action_id("github_issue:eerovil/agentdeck#7")
    assert db.settled[action_id][0] == "done"  # still spent: no usual start after it
    # Its chat stays the Work's, for a person to find on the Work page.
    assert db.side_chats == {"spare-token": "github_issue:eerovil/agentdeck#7"}


async def test_an_early_start_that_will_not_stop_is_tried_again_and_said_so(
    tmp_path, startable, worker, monkeypatch
):
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(work_start, "TELL_RETRY_S", (0.0, 0.0))
    tries: list[str] = []

    async def refuse(account, key):
        tries.append(key)
        return InjectResult(False, "runtime unreachable")

    monkeypatch.setattr(PROVIDERS["claude_code"], "interrupt_worker", refuse)
    app, _db, _injector = fake_app(tmp_path)
    app.state.start_spares = FakeSpares(str(startable / ".worktrees" / "work-spare-1"))
    early = await work_start.start_before_issue(app, ISSUE, request="Body", press="press-1")

    assert await work_start.stop_before_issue(app, early, "HTTP 422") is False
    assert tries == ["chat-spare"] * 3


async def test_a_refused_start_is_unlinked_and_the_next_try_starts_it(tmp_path, startable):
    """#2971: a refusal launched nothing, so it does not spend the Work's one start."""
    busy = InjectResult(False, "a new session is already starting for this account")
    app, db, injector = fake_app(tmp_path, result=busy)
    action_id = start_action_id("github_issue:eerovil/agentdeck#7")

    assert await start_work_agent(app, ISSUE, 7) == f"refused: {busy.reason}"
    assert db.side_chats == {}
    assert db.settled[action_id][0] == "failed"

    injector.result = ACCEPTED
    assert await start_work_agent(app, ISSUE, 7) == "started"
    assert await start_work_agent(app, ISSUE, 7) == "already started"
    assert len(injector.calls) == 2
    # The router's start never waits for the account's start in progress (#2971 review).
    assert injector.waited == []


async def test_only_an_unspent_waiting_start_waits_for_the_one_in_progress(tmp_path, startable):
    """The backstop's start waits; a spent Work answers at once, with no wait."""
    app, db, injector = fake_app(tmp_path)

    assert await start_work_agent(app, ISSUE, 7, wait=True) == "started"
    assert await start_work_agent(app, ISSUE, 7, wait=True) == "already started"
    assert injector.waited == ["claude_code:main"]


async def test_a_start_that_may_have_launched_stays_spent(tmp_path, startable):
    app, db, injector = fake_app(tmp_path)

    async def boom(*args, **kwargs):
        injector.calls.append(kwargs)
        raise RuntimeError("runtime went away mid-start")

    injector.start_new = boom
    assert await start_work_agent(app, ISSUE, 7) == "uncertain"
    injector.start_new = FakeInjector(ACCEPTED).start_new
    assert await start_work_agent(app, ISSUE, 7) == "already started"
    assert len(injector.calls) == 1


async def _settled() -> None:
    await asyncio.gather(*list(work_start._SETTLING))


async def test_a_start_the_runtime_refuses_after_queueing_is_settled_failed(
    tmp_path, startable, announced
):
    """``start_new`` only queues the spawn; ``at_capacity`` arrives after it (#2984)."""
    app, db, injector = fake_app(tmp_path, answer=InjectResult(False, "at_capacity"))
    action_id = start_action_id("github_issue:eerovil/agentdeck#7")

    # The caller does not wait for the answer; the start is spent (unsettled) until it lands.
    assert await start_work_agent(app, ISSUE, 7) == "started"
    assert action_id not in db.settled
    await _settled()
    assert db.settled[action_id] == ("failed", "at_capacity")
    assert db.side_chats == {}
    assert announced == [], "a start that launched nothing says nothing on the issue"

    # Failed is not spent: the next try (the backstop's) starts it.
    injector.answer = ACCEPTED
    assert await start_work_agent(app, ISSUE, 7) == "started"
    await _settled()
    assert db.settled[action_id][0] == "done"
    assert len(injector.calls) == 2


async def test_a_refusal_after_queueing_is_handed_back_to_the_caller(tmp_path, startable):
    """#3006: both accounts were full; the Work waited 20 minutes for the backstop."""
    refused: list[str] = []
    app, db, injector = fake_app(tmp_path, answer=InjectResult(False, "at_capacity"))
    assert await start_work_agent(app, ISSUE, 7, on_refused=lambda: refused.append("x")) == (
        "started"
    )
    await _settled()
    assert refused == ["x"]

    injector.answer = ACCEPTED
    assert await start_work_agent(app, ISSUE, 7, on_refused=lambda: refused.append("y")) == (
        "started"
    )
    await _settled()
    assert refused == ["x"], "a start that ran is not handed back"


@pytest.mark.parametrize(
    "answer",
    [
        None,
        InjectResult(True, session_id="sid"),
        InjectResult(False, "first turn rejected", session_id="sid"),
    ],
    ids=["unknown", "accepted", "refused with a session"],
)
async def test_a_start_that_may_have_launched_stays_done(tmp_path, startable, answer):
    app, db, _ = fake_app(tmp_path)
    app.state.injector.answer = answer

    assert await start_work_agent(app, ISSUE, 7) == "started"
    await _settled()
    action_id = start_action_id("github_issue:eerovil/agentdeck#7")
    assert db.settled[action_id][0] == "done"
    assert action_id in db.side_chats


def test_only_a_failed_work_action_is_released(tmp_path):
    from agentdeck.db import Db

    db = Db(tmp_path / "control.sqlite3")
    for action_id, outcome in (("a", "failed"), ("b", "done"), ("c", None)):
        db.reserve_work_action(action_id, "github_issue:o/r#1", "new_chat")
        if outcome:
            db.settle_work_action(action_id, outcome)
        db.release_failed_work_action(action_id)
    assert db.work_action("a") is None
    assert db.work_action("b").outcome == "done"
    assert db.work_action("c").outcome is None
    assert db.reserve_work_action("a", "github_issue:o/r#1", "new_chat") is True


async def test_waiting_for_a_new_start_returns_when_it_ends_or_times_out():
    import asyncio

    from agentdeck.inject import InjectionService

    injector = InjectionService.__new__(InjectionService)
    injector._new_tasks = {}
    await injector.wait_for_new_start("claude_code:main", 5)  # nothing running

    gate = asyncio.Event()
    injector._new_tasks["claude_code:main"] = asyncio.create_task(gate.wait())
    await injector.wait_for_new_start("claude_code:main", 0.01)  # gives up, never cancels
    assert not injector._new_tasks["claude_code:main"].done()
    asyncio.get_running_loop().call_later(0.01, gate.set)
    await asyncio.wait_for(injector.wait_for_new_start("claude_code:main", 5), 1)
    assert injector._new_tasks["claude_code:main"].done()


async def route_refused(root, outcomes, *, clock):
    """A router whose starts answer ``outcomes`` in turn."""
    started: list[tuple[str, int]] = []
    refusals: list = []
    seen_labels: list = []
    answers = iter(outcomes)

    async def start_work(repository: str, number: int, on_refused=None, labels=()) -> str:
        started.append((repository, number))
        seen_labels.append(labels)
        refusals.append(on_refused)
        return next(answers)

    async def start_review(pull, work):  # pragma: no cover - no review in these cases
        return None

    service = WorkRouterService(
        root,
        switched=lambda repository: True,
        owner_of_issue=lambda repository, number: None,
        owner_of_pull=lambda repository, number: None,
        schedule=lambda message: message,
        start_review=start_review,
        own_logins=frozenset(),
        start_work=start_work,
        start_labels=lambda repository: ("Agent",),
        clock=clock,
    )
    service.refusals = refusals
    service.seen_labels = seen_labels
    return service, started


async def test_a_refused_label_start_is_retried_on_the_back_off(root):
    """#2971: two labels a second apart; the second start was refused and never retried.
    #3134: the retry keeps the issue's labels, so a spec-only issue stays a spec."""
    now = [1000.0]
    labelled(root, "d1", labels=("agent", "spec-only"))
    service, started = await route_refused(
        root,
        ["refused: a new session is already starting for this account", "started"],
        clock=lambda: now[0],
    )
    tailer = WebhookTailer(root, consumer="work-router")
    await service.apply(list(tailer.deliveries()))

    key = "start:github_issue:eerovil/agentdeck#7"
    assert key in service.pending
    await service.retry_pending()
    assert len(started) == 1, "not before its back-off"
    now[0] += 61
    await service.retry_pending()
    assert started == [(ISSUE, 7), (ISSUE, 7)]
    assert service.seen_labels[-1] == ["agent", "spec-only"]
    assert key not in service.pending


@pytest.mark.parametrize(
    "action, label", [("closed", None), ("unlabeled", "agent")], ids=["closed", "unlabelled"]
)
async def test_a_retry_is_dropped_when_the_issue_closes_or_loses_its_label(root, action, label):
    now = [1000.0]
    labelled(root, "d1")
    service, started = await route_refused(root, ["refused: at capacity"], clock=lambda: now[0])
    tailer = WebhookTailer(root, consumer="work-router")
    await service.apply(list(tailer.deliveries()))
    tailer.commit()
    assert "start:github_issue:eerovil/agentdeck#7" in service.pending

    deliver(
        root,
        event="issues",
        delivery="d2",
        action=action,
        issue={"number": 7, "state": "closed" if action == "closed" else "open"},
        **({"label": {"name": label}} if label else {}),
        sender={"login": "eerovil", "type": "User"},
    )
    await service.apply(list(tailer.deliveries()))
    assert "start:github_issue:eerovil/agentdeck#7" not in service.pending
    now[0] += 3600
    await service.retry_pending()
    assert len(started) == 1


async def test_a_start_the_runtime_refuses_later_is_retried_on_a_growing_back_off(root):
    """#3006: ``started`` then ``at_capacity`` waited for the 15-minute backstop."""
    now = [1000.0]
    labelled(root, "d1")
    service, started = await route_refused(root, ["started"] * 3, clock=lambda: now[0])
    tailer = WebhookTailer(root, consumer="work-router")
    await service.apply(list(tailer.deliveries()))
    key = "start:github_issue:eerovil/agentdeck#7"
    assert key not in service.pending

    service.refusals[-1]()  # the runtime answers at_capacity
    assert service.pending.entries[key]["next_at"] == 1060.0
    now[0] += 61
    await service.retry_pending()
    assert len(started) == 2
    assert key not in service.pending

    service.refusals[-1]()  # still full: the wait doubles rather than starting over
    assert service.pending.entries[key]["next_at"] == now[0] + 120.0


async def test_a_starred_works_start_is_retried_first(root):
    """#3116: when several refused starts fall due, the starred Work's is tried first, so it
    takes the worker that freed up."""
    service, _started = await route_refused(root, [], clock=lambda: 1000.0)
    for number in (7, 8, 9):
        service.pending.put(
            f"start:github_issue:eerovil/agentdeck#{number}",
            {"kind": "start", "repository": "eerovil/agentdeck", "number": number},
            900.0,
        )
    service._starred = lambda work_id: work_id == "github_issue:eerovil/agentdeck#9"
    tried = []

    async def record(key, entry):
        tried.append(entry["number"])

    service._retry_start = record
    await service.retry_pending()
    assert tried == [9, 7, 8]


async def test_a_late_refusal_does_not_revive_a_start_that_lost_its_label(root):
    labelled(root, "d1")
    service, started = await route_refused(root, ["started"], clock=lambda: 1000.0)
    tailer = WebhookTailer(root, consumer="work-router")
    await service.apply(list(tailer.deliveries()))
    tailer.commit()
    deliver(
        root,
        event="issues",
        delivery="d2",
        action="unlabeled",
        issue={"number": 7, "state": "open"},
        label={"name": "agent"},
        sender={"login": "eerovil", "type": "User"},
    )
    await service.apply(list(tailer.deliveries()))
    service.refusals[-1]()
    assert service.pending.entries == {}


async def test_an_uncertain_label_start_is_never_retried(root):
    labelled(root, "d1")
    service, started = await route_refused(root, ["uncertain"], clock=lambda: 1000.0)
    tailer = WebhookTailer(root, consumer="work-router")
    await service.apply(list(tailer.deliveries()))
    assert service.pending.entries == {}


async def test_no_claude_account_or_no_checkout_starts_nothing(tmp_path, monkeypatch):
    app, db, injector = fake_app(tmp_path, provider_id="codex")
    assert await start_work_agent(app, ISSUE, 7) == "no account"

    app, db, injector = fake_app(tmp_path)
    _claude_may_start(monkeypatch)
    monkeypatch.setattr(work_start, "local_repository_checkout", lambda ref, found: "")
    assert await start_work_agent(app, ISSUE, 7) == "no checkout"
    assert injector.calls == [] and db.actions == {}


def test_the_poller_overlays_checkouts_are_candidates(tmp_path):
    overlay = tmp_path / "poller" / "instances" / "notes" / "overlay.toml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text('[checkouts]\nnotes = "~/notes"\n')
    assert work_start._overlay_checkouts(tmp_path / "poller") == ["~/notes"]


def _manifest(root: Path, instance: str, body: str) -> None:
    path = root / "instances" / instance / "poller.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_start_labels_are_each_projects_ownership_labels(tmp_path):
    body = (
        'contract = 1\n[board]\norg = "eerovil"\nproject_number = 1\n'
        'ownership_label = ["agent", "Claude"]\n'
        '[[repos]]\nid = "notes"\nholds_issues = true\n'
    )
    _manifest(tmp_path, "notes", body)
    _manifest(tmp_path, "other", body.replace("notes", "other"))

    assert work_start_labels(tmp_path) == {
        "eerovil/notes": frozenset({"agent", "claude"}),
        "eerovil/other": frozenset({"agent", "claude"}),
    }


def _switched(root: Path, instance: str, worker: str) -> None:
    _manifest(
        root,
        instance,
        'contract = 1\n[board]\norg = "eerovil"\nproject_number = 1\n'
        f'ownership_label = ["agent"]\n[[repos]]\nid = "{instance}"\nholds_issues = true\n',
    )
    (root / "instances" / instance / "overlay.toml").write_text(worker)


def test_a_switched_projects_worker_accounts_come_from_its_overlay(tmp_path):
    _switched(
        tmp_path,
        "agentdeck",
        '[worker]\naccount = "claude_code:alt"\n'
        'fallback_accounts = ["claude_code:main", "codex:codex"]\n',
    )
    _switched(tmp_path, "notes", "[checkouts]\nnotes = '~/notes'\n")  # names no worker

    assert work_accounts(tmp_path) == {
        "eerovil/agentdeck": ("claude_code:alt", "claude_code:main", "codex:codex"),
        "eerovil/notes": (),
    }


def test_an_unreadable_worker_overlay_has_no_configured_answer(tmp_path):
    _switched(tmp_path, "agentdeck", "this is not toml = [")

    assert work_accounts(tmp_path) == {"eerovil/agentdeck": None}


def test_a_malformed_worker_choice_has_no_configured_answer(tmp_path):
    _switched(tmp_path, "agentdeck", 'worker = "not a table"\n')

    assert work_accounts(tmp_path) == {"eerovil/agentdeck": None}


def _accounts(app, *keys):
    app.state.accounts = [SimpleNamespace(key=key, provider_id=key.split(":")[0]) for key in keys]


async def test_the_work_starts_on_the_account_its_project_chose(tmp_path, monkeypatch):
    """#2895 batch 2: trailhead's Work runs on its work account, as its Poller's worker would."""
    _switched(
        tmp_path / "poller",
        "agentdeck",
        '[worker]\naccount = "claude_code:alt"\n'
        'fallback_accounts = ["codex:codex", "claude_code:main"]\n',
    )
    app, db, injector = fake_app(tmp_path)
    _accounts(app, "claude_code:main", "claude_code:alt", "claude_code:other")
    full = {"claude_code:alt"}
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: account.key not in full
    )
    assert work_start.start_account(app, ISSUE).key == "claude_code:main"  # alt full: next listed
    full.add("claude_code:main")
    assert work_start.start_account(app, ISSUE) is None  # never an account the project did not name
    assert work_start.start_account(app, "eerovil/unswitched").key == "claude_code:other"
    full.clear()
    assert work_start.start_account(app, ISSUE).key == "claude_code:alt"

    # #3304: alt's five-hour window at 100% would refuse the turn, so main takes it ...
    _usage(tmp_path, "claude_code:alt", five_hour=100, seven_day=82)
    _usage(tmp_path, "claude_code:main", five_hour=12, seven_day=62)
    assert work_start.start_account(app, ISSUE).key == "claude_code:main"
    # ... and usage balances: with room on both, the one with more weekly budget left leads.
    _usage(tmp_path, "claude_code:alt", five_hour=10, seven_day=30)
    assert work_start.start_account(app, ISSUE).key == "claude_code:alt"
    _usage(tmp_path, "claude_code:alt", five_hour=10, seven_day=90)
    assert work_start.start_account(app, ISSUE).key == "claude_code:main"


def _usage(tmp_path, account, *, five_hour, seven_day):
    now = datetime.now(UTC)
    folder = tmp_path / "usage"
    folder.mkdir(exist_ok=True)
    (folder / f"usage-{account.replace(':', '-')}.json").write_text(
        json.dumps(
            {
                "account": account,
                "fetched_at": now.isoformat(),
                "five_hour_pct": five_hour,
                "five_hour_resets_at": (now + timedelta(hours=1)).isoformat(),
                "seven_day_pct": seven_day,
                "seven_day_resets_at": (now + timedelta(days=3)).isoformat(),
            }
        )
    )


def test_an_unreadable_overlay_never_falls_back_to_an_unnamed_account(tmp_path, monkeypatch):
    poller_root = tmp_path / "poller"
    _switched(poller_root, "agentdeck", "this is not toml = [")
    app, db, injector = fake_app(tmp_path)
    _accounts(app, "claude_code:main")
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["claude_code"], "can_start_session", lambda account: True)

    assert work_start.start_account(app, ISSUE) is None

    (poller_root / "instances" / "agentdeck" / "overlay.toml").write_text(
        '[worker]\naccount = "claude_code:main"\n'
    )
    assert work_start.start_account(app, ISSUE).key == "claude_code:main"


@pytest.mark.parametrize("auto_release", [True, False])
async def test_the_first_message_says_whether_the_project_merges_on_its_own(
    tmp_path, startable, auto_release
):
    """#3092: trailhead's agent told its owner the deck would merge what waits for a press.
    #3098: it is also told where its screenshots go, when its project has an evidence
    repository (here only the auto-releasing one does).
    #3134: an issue carrying the spec-only label is told to write the spec, not build it.
    #3147: it is told which repositories its CI or its review proves, so it runs no job there."""
    evidence = '[[repos]]\nid = "agent-evidence"\nholds_artifacts = true\n'
    proven = (
        '[[repos]]\nid = "store"\nholds_code = true\nverification = "github_checks"\n'
        '[[repos]]\nid = "docs"\nholds_code = true\nverification = "review_only"\n'
    )
    _manifest(
        tmp_path / "poller",
        "agentdeck",
        'contract = 1\n[board]\norg = "eerovil"\nproject_number = 1\n'
        'spec_only_label = "Spec-Only"\n'
        '[[repos]]\nid = "agentdeck"\nholds_issues = true\n'
        + (evidence if auto_release else proven)
        + f"[capabilities]\nauto_release = {str(auto_release).lower()}\n",
    )
    (tmp_path / "poller" / "instances" / "agentdeck" / "overlay.toml").write_text("")
    app, _, injector = fake_app(tmp_path)
    pull = "https://github.com/eerovil/agentdeck/pull/70"
    if not auto_release:
        # #3834: a pull request already open for it, as on a Work taken over from another deck.
        from types import SimpleNamespace

        from agentdeck.issue_context import IssueBacklog, RepositoryPulls

        rows = ({"number": 70, "url": pull, "head_branch": "agent/issue-7"},)
        backlog = IssueBacklog(pulls=(RepositoryPulls("eerovil/agentdeck", rows),))
        app.state.issues = SimpleNamespace(cached=lambda: backlog)

    labels = ("agent", "spec-only") if auto_release else ("agent",)
    assert await start_work_agent(app, "EeroVil/AgentDeck", 7, labels=labels) == "started"

    wanted = work_start.MERGES_ITSELF if auto_release else work_start.WAITS_FOR_MERGE
    if not auto_release:
        wanted += (
            " "
            + work_start.CI_TESTS_TEXT.format(repos="`eerovil/store`")
            + " "
            + work_start.REVIEW_ONLY_TEXT.format(repos="`eerovil/docs`")
        )
    if auto_release:
        wanted += (
            " Commit this Work's screenshots to `eerovil/agent-evidence`"
            " under `agentdeck/issue-7/`."
        )
    else:
        wanted += " " + work_start.OPEN_PULL_TEXT.format(pulls=pull)
    # #3274: last, where it works; claimed under the id Finish releases, whatever the case.
    assert app.state.runtime_asked[0]["work_id"] == "github_issue:eerovil/agentdeck#7"
    wanted += " " + work_start.WORKTREE_TEXT.format(
        path=startable / ".worktrees" / "issue-7", branch="agent/issue-7", main=startable
    )
    message = injector.calls[0]["message"]
    spec = "This issue carries the `Spec-Only` label, so it is a spec only"
    if auto_release:
        head, _, tail = message.partition(f"{work_start.FIRST_TEXT} {spec}")
        assert tail.endswith(wanted) and "--remove-label Spec-Only" in tail
        # A spec may draw its plan's diagram, uncommitted (#3827).
        assert "other than docs and plans under `docs/` in your own worktree" in tail
    else:
        assert spec not in message
        assert message.endswith(f"{work_start.FIRST_TEXT} {wanted}")
    await _settled()


def test_a_readable_overlay_with_no_preference_uses_an_available_account(tmp_path, monkeypatch):
    _switched(tmp_path / "poller", "agentdeck", "[checkouts]\nagentdeck = '~/agentdeck'\n")
    app, db, injector = fake_app(tmp_path)
    _accounts(app, "claude_code:main")
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["claude_code"], "can_start_session", lambda account: True)

    assert work_start.start_account(app, ISSUE).key == "claude_code:main"


async def test_the_start_sign_is_a_rocket_and_one_marked_comment(monkeypatch):
    """#3109: what ``announce_start`` asks GitHub for; a failure of either is only logged."""
    calls: list[tuple[list[str], bytes | None]] = []

    async def run_once(argv, *, timeout, stdin=None, repo=None, owner=None):
        calls.append((list(argv[1:]), stdin))
        return (1 if argv[1] == "api" else 0), b"", b"boom"

    monkeypatch.setattr(work_signals.shutil, "which", lambda name: "/bin/gh")
    monkeypatch.setattr(work_signals.gh_cli, "run_once", run_once)
    await ANNOUNCE_START(ISSUE, 7, action_id="work-start-x", work_url="https://d/w")
    await work_signals.acknowledge_comment(ISSUE, 55)

    react, post, seen = calls
    assert react[0][-3:] == [f"repos/{ISSUE}/issues/7/reactions", "-f", "content=rocket"]
    assert post[0][:2] == ["issue", "comment"] and b"[Work page](https://d/w)" in post[1]
    assert b"<!-- agentdeck-work-start id=work-start-x -->" in post[1]
    assert seen[0][-3:] == [f"repos/{ISSUE}/issues/comments/55/reactions", "-f", "content=eyes"]

    # #3188: a close's quote is the closer's own newest comment, whoever else spoke since.
    comments = [
        {"user": {"login": "Eerovil"}, "created_at": "2026-10-16T09:51:40Z", "body": "mine"},
        {"user": {"login": "other"}, "created_at": "2026-10-16T09:51:47Z", "body": "theirs"},
    ]

    async def read(argv, *, timeout, stdin=None, repo=None, owner=None):
        calls.append((list(argv[1:]), stdin))
        return 0, json.dumps(comments).encode(), b""

    monkeypatch.setattr(work_signals.gh_cli, "run_once", read)
    found = await work_signals.latest_comment(ISSUE, 7, "2026-10-16T09:50:48Z", "eerovil")
    assert found["body"] == "mine"
    assert calls[-1][0][-1].endswith("issues/7/comments?since=2026-10-16T09:50:48Z&per_page=100")
