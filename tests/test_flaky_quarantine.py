"""The test quarantine files its fix issue and lifts itself when that issue closes (#3055)."""

from __future__ import annotations

import json

from agentdeck import gh_cli
from agentdeck.flaky_quarantine import MARKER, Quarantine

REPO = "eerovil/agentdeck"
FLAKE = "tests/test_x.py::test_flaky[a b]"


class FakeGitHub:
    """`gh issue create/view/list` as the sweep calls them, recording every call."""

    def __init__(
        self,
        monkeypatch,
        *,
        create=(0, "https://github.com/eerovil/agentdeck/issues/91\n", ""),
        state="OPEN",
        listing=None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.created: list[dict] = []
        self.create = create
        self.state = state
        self.listing = listing

        async def run_once(argv, *, timeout, stdin=None, repo=None, owner=None):
            self.calls.append(list(argv))
            self.created.append({"argv": list(argv), "body": (stdin or b"").decode()})
            code, out, err = self.create
            return code, out.encode(), err.encode()

        async def run(argv, *, timeout, repo=None, owner=None):
            self.calls.append(list(argv))
            if argv[1:3] == ["issue", "view"]:
                return 0, json.dumps({"state": self.state}).encode()
            if self.listing is None:
                return 1, b""
            return 0, json.dumps(self.listing).encode()

        monkeypatch.setattr(gh_cli, "run_once", run_once)
        monkeypatch.setattr(gh_cli, "run", run)


def _quarantine(tmp_path) -> Quarantine:
    return Quarantine(
        tmp_path / "quarantine.json",
        labels=lambda repository: "agent" if repository == REPO else "",
        clock=lambda: 1_800_000_000.0,
    )


def _forgiven(test=FLAKE) -> list[dict]:
    return [{"test": test, "kind": "flaky", "commit": "abc123def4567", "job_id": "r1"}]


async def test_a_forgiven_test_gets_one_labelled_fix_issue_and_stays_out_until_it_closes(
    tmp_path, monkeypatch
):
    github = FakeGitHub(monkeypatch)
    quarantine = _quarantine(tmp_path)

    assert quarantine.add("EeroVil/AgentDeck", _forgiven(), verification="v1") == [FLAKE]
    assert quarantine.add(REPO, _forgiven(), verification="v2") == [], "only the first time"
    assert quarantine.tests(REPO) == (FLAKE,)
    # A collection error names a whole file: never quarantined, or the file leaves the gate.
    assert quarantine.add(REPO, _forgiven("tests/test_x.py"), verification="v3") == []
    assert quarantine.tests("other/repo") == ()

    await quarantine.sweep()
    [created] = github.created
    argv = created["argv"]
    assert argv[argv.index("--label") + 1] == "agent"
    assert argv[argv.index("--title") + 1] == f"Flaky test: {FLAKE}"
    assert MARKER.format(repository=REPO, test=FLAKE) in created["body"]
    assert "quarantined" in created["body"] and "v1" in created["body"]
    # Durable: a restarted runtime reads the same quarantine and its issue.
    reread = _quarantine(tmp_path)
    assert [r["issue"] for r in reread.records()] == [91]

    await reread.sweep()  # still open: nothing filed again, still left out
    assert len(github.created) == 1 and reread.tests(REPO) == (FLAKE,)

    github.state = "CLOSED"
    await reread.sweep()
    assert reread.tests(REPO) == () and _quarantine(tmp_path).records() == []


async def test_a_repository_no_manifest_declares_is_never_quarantined(tmp_path, monkeypatch):
    github = FakeGitHub(monkeypatch)
    quarantine = _quarantine(tmp_path)

    assert quarantine.add("someone/else", _forgiven(), verification="v1") == []
    assert quarantine.add("", _forgiven(), verification="v1") == []
    await quarantine.sweep()
    assert github.calls == [] and quarantine.records() == []


async def test_an_unanswered_filing_looks_for_its_issue_before_opening_another(
    tmp_path, monkeypatch
):
    github = FakeGitHub(monkeypatch, create=(1, "", "connection reset"))
    quarantine = _quarantine(tmp_path)
    quarantine.add(REPO, _forgiven(), verification="v1")

    await quarantine.sweep()
    assert len(github.created) == 1
    # The listing cannot be read: unknown is not absent, so no second create.
    await quarantine.sweep()
    assert len(github.created) == 1

    # The listing shows the issue the unanswered create made: adopted, not filed again.
    github.listing = [
        {
            "number": 92,
            "body": "x " + MARKER.format(repository=REPO, test=FLAKE),
            "createdAt": "2027-01-15T08:00:00Z",
        },
    ]
    await quarantine.sweep()
    assert len(github.created) == 1
    assert [r["issue"] for r in quarantine.records()] == [92]


async def test_an_intent_with_no_issue_after_it_is_filed_again(tmp_path, monkeypatch):
    github = FakeGitHub(monkeypatch, create=(1, "", "connection reset"))
    quarantine = _quarantine(tmp_path)
    quarantine.add(REPO, _forgiven(), verification="v1")
    await quarantine.sweep()

    # The page reaches back past the intent and holds no marked issue: really absent.
    github.listing = [{"number": 5, "body": "older", "createdAt": "2020-01-01T00:00:00Z"}]
    github.create = (0, "https://github.com/eerovil/agentdeck/issues/93\n", "")
    await quarantine.sweep()
    assert len(github.created) == 2
    assert [r["issue"] for r in quarantine.records()] == [93]


def test_only_a_verifications_pytest_leaves_quarantined_tests_out(tmp_path):
    from agentdeck.jobs import VERIFICATION, Job, JobQueue

    quarantine = _quarantine(tmp_path)
    quarantine.add(REPO, _forgiven(), verification="v1")
    queue = JobQueue(state_dir=tmp_path / "jobs", quarantine=quarantine)

    def job(command, **extra):
        return Job(
            id="j", command=command, cwd=str(tmp_path), log_path="", submitted_at=0.0, **extra
        )

    verifying = {"purpose": VERIFICATION, "work_id": "w"}
    for command in (
        ["pytest", "-q"],
        ["uv", "run", "pytest"],
        ["/x/.venv/bin/pytest"],
        ["python", "-m", "pytest", "tests"],
    ):
        assert queue._quarantined(job(command, **verifying), REPO) == (FLAKE,), command
    assert queue._quarantined(job(["pytest", "-q"], **verifying), "other/repo") == ()
    assert queue._quarantined(job(["make", "test"], **verifying), REPO) == ()
    # A pod runs the checkout's own runner: one that cannot load the plugin claims nothing.
    from agentdeck.jobs import AGENTDECK_RUNNER, KUBE, QUARANTINE_PLUGIN_FILE_ENV

    runner = tmp_path / AGENTDECK_RUNNER
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text("#!/bin/bash\nexec pytest\n")
    assert queue._quarantined(job(["pytest"], lane=KUBE, **verifying), REPO) == ()
    runner.write_text(f"#!/bin/bash\n# {QUARANTINE_PLUGIN_FILE_ENV}\n")
    assert queue._quarantined(job(["pytest"], lane=KUBE, **verifying), REPO) == (FLAKE,)
    # A rerun of failing tests is not a verification: it must still run the flaky test.
    assert queue._quarantined(job(["pytest", FLAKE], follows="v1", follow_kind="rerun"), REPO) == ()
    assert (
        JobQueue(state_dir=tmp_path / "q2")._quarantined(job(["pytest"], **verifying), REPO) == ()
    )

    # #3584: the Work whose issue is the test's fix issue runs it; any other Work leaves it
    # out, as does the same number in another repository or a Work that names no issue.
    quarantine._records[next(iter(quarantine._records))]["issue"] = 91
    for work, expected in (
        ("github_issue:EeroVil/AgentDeck#91", ()),
        ("github_issue:eerovil/agentdeck#92", (FLAKE,)),
        ("github_issue:other/repo#91", (FLAKE,)),
        ("", (FLAKE,)),
    ):
        own = job(["pytest"], purpose=VERIFICATION, run_id="r", owner_work_id=work)
        assert queue._quarantined(own, REPO) == expected, work
    assert quarantine.tests(REPO, keep_for_issue=91) == ()
    assert quarantine.tests(REPO, keep_for_issue=None) == (FLAKE,)


def test_the_plugin_leaves_out_exactly_the_quarantined_test_not_its_namesakes(monkeypatch):
    """pytest's `--deselect` is a prefix match; the plugin is not (review of #3075)."""
    import importlib.util
    from types import SimpleNamespace

    from agentdeck.jobs import QUARANTINE_ENV, QUARANTINE_PLUGIN, QUARANTINE_PLUGIN_DIR

    spec = importlib.util.spec_from_file_location(
        QUARANTINE_PLUGIN, QUARANTINE_PLUGIN_DIR / f"{QUARANTINE_PLUGIN}.py"
    )
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    ids = ["t.py::test_x", "t.py::test_x_slow", "t.py::test_y[1]", "t.py::test_y[2]"]
    items = [SimpleNamespace(nodeid=nodeid) for nodeid in ids]
    deselected = []
    config = SimpleNamespace(
        hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items))
    )
    monkeypatch.setenv(QUARANTINE_ENV, '["t.py::test_x", "t.py::test_y[1]"]')

    plugin.pytest_collection_modifyitems(config, items)

    assert [item.nodeid for item in items] == ["t.py::test_x_slow", "t.py::test_y[2]"]
    assert [item.nodeid for item in deselected] == ["t.py::test_x", "t.py::test_y[1]"]
    monkeypatch.setenv(QUARANTINE_ENV, "not json")
    plugin.pytest_collection_modifyitems(config, items)
    assert len(items) == 2
