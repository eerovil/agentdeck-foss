"""The capability cache may only ever reuse an approval that already happened (#1738)."""

from __future__ import annotations

import json

import pytest

from agentdeck.approval_cache import (
    ALLOW,
    CACHEABLE_CLASSES,
    DENY,
    LOCAL_WRITE,
    READ,
    REMOTE_MUTATION,
    SENSITIVE,
    ApprovalCache,
    CapabilityLedger,
    WorkerScope,
    capability_key,
    file_change_key,
    policy_fingerprint,
    resolve_scope,
)


@pytest.fixture
def worktree(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/master\n")
    (root / "src").mkdir()
    (root / "AGENTS.md").write_text("do not push to master\n")
    return root


@pytest.fixture
def scope(worktree):
    return WorkerScope(
        worker="thread-1",
        repository=str(worktree / ".git"),
        worktree=str(worktree),
        policy_fingerprint=policy_fingerprint(worktree),
    )


def key_for(command, scope, cwd=None):
    return capability_key(command, cwd=cwd or scope.worktree, scope=scope)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    "command, action_class",
    [
        ("git status", READ),
        ("git log --oneline -5", READ),
        ("git commit -m 'fix'", LOCAL_WRITE),
        ("git add src", LOCAL_WRITE),
        ("git fetch origin", LOCAL_WRITE),
        ("ls src", READ),
        ("mkdir src/new", LOCAL_WRITE),
    ],
)
def test_low_risk_shapes_are_cacheable(command, action_class, scope):
    key = key_for(command, scope)
    assert key is not None, command
    assert key.action_class == action_class
    assert action_class in CACHEABLE_CLASSES


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD",
        "git push",
        "git remote add other https://example.invalid/x.git",
        "gh pr merge 12 --squash",
        "gh pr create --title x --body y",
        "gh issue comment 4 --body hi",
    ],
)
def test_remote_mutations_are_never_cacheable(command, scope):
    assert key_for(command, scope) is None


@pytest.mark.parametrize(
    "command",
    [
        # Destructive second-level forms whose first-level subcommand is an
        # ordinary local write. Classification that stopped at the first
        # subcommand would have made every one of these cacheable.
        "git stash drop",
        "git stash clear",
        "git stash pop",
        "git worktree remove ../other",
        "git worktree prune",
        "git branch -d feature",
        "git branch --delete feature",
        "git tag -d v1",
        "git reflog expire --all",
        "git remote remove origin",
        "rm -r src",
        "sudo systemctl restart agentdeck",
        "git reset --hard HEAD~1",
        "git push --force origin HEAD",
        "git branch -D feature",
        "chmod 777 src",
        "ssh host 'do thing'",
        "curl https://example.invalid",
    ],
)
def test_destructive_and_sensitive_commands_are_never_cacheable(command, scope):
    assert key_for(command, scope) is None


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'x' && git push",
        "git status | head",
        "bash -c 'git commit'",
        "python3 -c 'import os'",
        "make release",
        "npm install",
        "git frobnicate",
        "unknownprogram --do-it",
        "git commit -m",
        "",
        "   ",
        "git commit -m 'unterminated",
    ],
)
def test_uncertain_normalization_fails_closed(command, scope):
    assert key_for(command, scope) is None


def test_a_path_outside_the_worktree_is_not_cacheable(scope, tmp_path):
    assert key_for("ls ../elsewhere", scope) is None
    assert key_for(f"ls {tmp_path}/other", scope) is None
    assert key_for("git add ../../etc/passwd", scope) is None


def test_a_path_inside_the_worktree_is_recorded_relative(scope, worktree):
    key = key_for("git add src", scope)
    assert key is not None
    assert key.path_scope == ("src",)
    assert key_for(f"git add {worktree}/src", scope) == key


def test_unexpanded_expansions_fail_closed(scope):
    assert key_for("ls $HOME/thing", scope) is None
    assert key_for("ls ~/thing", scope) is None


# --- key narrowness -------------------------------------------------------


def test_a_commit_message_is_inert_but_a_flag_is_not(scope):
    first = key_for("git commit -m 'fix the thing'", scope)
    second = key_for("git commit -m 'fix another thing'", scope)
    amend = key_for("git commit --amend -m 'fix the thing'", scope)
    assert first is not None and first == second
    assert amend != first


def test_materially_different_arguments_are_a_miss(scope):
    assert key_for("git add src", scope) != key_for("git add .", scope)
    assert key_for("git log -5", scope) != key_for("git log -50", scope)
    assert key_for("git fetch origin", scope) != key_for("git fetch upstream", scope)


def test_a_key_cannot_be_shared_across_worker_repository_or_worktree(scope):
    base = key_for("git commit -m x", scope)
    for field in ("worker", "repository", "worktree"):
        other = WorkerScope(**{**scope.__dict__, field: "different"})
        assert key_for("git commit -m x", other) != base


def test_policy_fingerprint_is_part_of_the_key(scope, worktree):
    before = key_for("git commit -m x", scope)
    (worktree / "AGENTS.md").write_text("now you may push to master\n")
    after_scope = WorkerScope(
        **{**scope.__dict__, "policy_fingerprint": policy_fingerprint(worktree)}
    )
    assert key_for("git commit -m x", after_scope) != before


def test_adding_a_policy_file_changes_the_fingerprint(worktree):
    before = policy_fingerprint(worktree)
    (worktree / "CLAUDE.md").write_text("@AGENTS.md\n")
    assert policy_fingerprint(worktree) != before


# --- the in-memory store --------------------------------------------------


def test_an_approval_is_reused_for_the_identical_capability(scope):
    cache = ApprovalCache()
    key = key_for("git commit -m 'one'", scope)
    assert cache.lookup(key) is None
    cache.record(key, outcome=ALLOW, approval_id="approval-1")
    hit = cache.lookup(key_for("git commit -m 'two'", scope))
    assert hit is not None
    assert hit.origin_approval_id == "approval-1"
    assert "approval-1" in hit.audit_line()
    assert key.describe() in hit.audit_line()


def test_a_grant_does_not_widen_to_a_neighbouring_command(scope):
    cache = ApprovalCache()
    cache.record(key_for("git add src", scope), outcome=ALLOW, approval_id="approval-1")
    assert cache.lookup(key_for("git add .", scope)) is None
    assert cache.lookup(key_for("git commit -m x", scope)) is None
    assert cache.lookup(key_for("git push origin HEAD", scope)) is None


def test_a_local_write_grant_cannot_cover_a_remote_mutation(scope):
    cache = ApprovalCache()
    cache.record(key_for("git add src", scope), outcome=ALLOW, approval_id="approval-1")
    # There is no key at all for the push, so there is nothing to match against.
    assert key_for("git push origin HEAD", scope) is None
    assert cache.lookup(key_for("git push origin HEAD", scope)) is None


def test_a_prior_denial_is_never_reusable_as_an_approval(scope):
    cache = ApprovalCache()
    key = key_for("git commit -m x", scope)
    cache.record(key, outcome=DENY, approval_id="approval-deny")
    cache.record(key, outcome=ALLOW, approval_id="approval-allow")
    assert cache.lookup(key) is None


def test_a_denial_revokes_an_existing_grant(scope):
    cache = ApprovalCache()
    key = key_for("git commit -m x", scope)
    cache.record(key, outcome=ALLOW, approval_id="approval-1")
    cache.record(key, outcome=DENY, approval_id="approval-2")
    assert cache.lookup(key) is None


def test_a_policy_change_invalidates_the_grants_under_the_old_policy(scope, worktree):
    cache = ApprovalCache()
    old = key_for("git commit -m x", scope)
    cache.record(old, outcome=ALLOW, approval_id="approval-1")
    changed = WorkerScope(**{**scope.__dict__, "policy_fingerprint": "different"})
    new = key_for("git commit -m x", changed)
    assert cache.lookup(new) is None
    cache.record(new, outcome=ALLOW, approval_id="approval-2")
    assert cache.lookup(old) is None


def test_a_grant_expires(scope):
    clock = _Clock()
    cache = ApprovalCache(clock=clock, ttl_s=60)
    key = key_for("git commit -m x", scope)
    cache.record(key, outcome=ALLOW, approval_id="approval-1")
    clock.now += 61
    assert cache.lookup(key) is None


def test_forgetting_a_worker_drops_its_grants(scope):
    cache = ApprovalCache()
    key = key_for("git commit -m x", scope)
    cache.record(key, outcome=ALLOW, approval_id="approval-1")
    cache.forget_worker(scope.worker)
    assert cache.lookup(key) is None


def test_an_uncacheable_class_is_never_stored(scope):
    cache = ApprovalCache()
    forced = key_for("git commit -m x", scope)
    remote = type(forced)(**{**forced.__dict__, "action_class": REMOTE_MUTATION})
    sensitive = type(forced)(**{**forced.__dict__, "action_class": SENSITIVE})
    cache.record(remote, outcome=ALLOW, approval_id="a")
    cache.record(sensitive, outcome=ALLOW, approval_id="b")
    assert cache.lookup(remote) is None
    assert cache.lookup(sensitive) is None


# --- file-change capabilities ---------------------------------------------


def test_a_file_change_root_inside_the_worktree_is_cacheable(scope, worktree):
    key = file_change_key(worktree / "src", scope=scope)
    assert key is not None
    assert key.action_class == LOCAL_WRITE
    assert key.path_scope == ("src",)


def test_a_file_change_root_outside_the_worktree_is_not(scope, tmp_path):
    assert file_change_key(tmp_path / "elsewhere", scope=scope) is None
    assert file_change_key(None, scope=scope) is None


# --- scope resolution -----------------------------------------------------


def test_scope_resolution_finds_the_worktree_and_repository(worktree):
    resolved = resolve_scope("thread-1", worktree / "src")
    assert resolved is not None
    assert resolved.worktree == str(worktree)
    assert resolved.repository == str(worktree / ".git")
    assert resolved.policy_fingerprint == policy_fingerprint(worktree)


def test_two_worktrees_of_one_repository_share_its_identity(tmp_path):
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "feature").mkdir(parents=True)
    (main / ".git" / "HEAD").write_text("ref: refs/heads/master\n")
    (main / "AGENTS.md").write_text("policy\n")
    linked = tmp_path / "feature"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'feature'}\n")
    assert resolve_scope("t", linked).repository == resolve_scope("t", main).repository
    assert resolve_scope("t", linked).worktree != resolve_scope("t", main).worktree


def test_an_empty_git_directory_is_not_a_repository(tmp_path):
    stray = tmp_path / "not-a-repo"
    (stray / ".git").mkdir(parents=True)
    resolved = resolve_scope("thread-1", stray)
    assert resolved is None or resolved.worktree != str(stray)


def test_a_request_with_no_worker_identity_fails_closed(tmp_path):
    assert resolve_scope("", tmp_path) is None


# --- the durable ledger ---------------------------------------------------


def test_a_deferred_request_becomes_a_grant_only_once_the_tool_runs(tmp_path, scope):
    ledger = CapabilityLedger(tmp_path / "cache.json")
    key = key_for("git commit -m 'one'", scope)
    assert ledger.consult(key, request_id="call-1") is None
    # Nothing is reusable yet: the guardian has not decided.
    assert ledger.consult(key, request_id="call-2") is None
    ledger.observe_execution("call-2")
    hit = ledger.consult(key, request_id="call-3")
    assert hit is not None
    assert hit.origin_approval_id == "call-2"


def test_a_denied_request_never_becomes_a_grant(tmp_path, scope):
    ledger = CapabilityLedger(tmp_path / "cache.json")
    key = key_for("git commit -m 'one'", scope)
    ledger.consult(key, request_id="call-1")
    # A denial means the tool never runs, so observe_execution never fires.
    assert ledger.consult(key, request_id="call-2") is None


def test_an_execution_we_did_not_defer_grants_nothing(tmp_path, scope):
    ledger = CapabilityLedger(tmp_path / "cache.json")
    assert ledger.observe_execution("never-seen") is None
    key = key_for("git commit -m x", scope)
    assert ledger.consult(key, request_id="call-1") is None


def test_a_stale_deferral_cannot_be_claimed(tmp_path, scope):
    clock = _Clock()
    ledger = CapabilityLedger(tmp_path / "cache.json", clock=clock, pending_ttl_s=60)
    key = key_for("git commit -m x", scope)
    ledger.consult(key, request_id="call-1")
    clock.now += 61
    assert ledger.observe_execution("call-1") is None


def test_every_hit_is_recorded_with_its_originating_approval(tmp_path, scope):
    ledger = CapabilityLedger(tmp_path / "cache.json")
    key = key_for("git commit -m 'one'", scope)
    ledger.consult(key, request_id="call-1")
    ledger.observe_execution("call-1")
    ledger.consult(key, request_id="call-2")
    audit = ledger.audit()
    assert len(audit) == 1
    assert audit[0]["origin_approval_id"] == "call-1"
    assert audit[0]["request_id"] == "call-2"
    assert audit[0]["capability"] == key.describe()
    assert audit[0]["key"]["shape"] == list(key.shape)


def test_a_policy_change_invalidates_the_ledger_grants(tmp_path, scope, worktree):
    ledger = CapabilityLedger(tmp_path / "cache.json")
    old = key_for("git commit -m x", scope)
    ledger.consult(old, request_id="call-1")
    ledger.observe_execution("call-1")
    assert ledger.consult(old, request_id="call-2") is not None

    changed = WorkerScope(**{**scope.__dict__, "policy_fingerprint": "changed"})
    new = key_for("git commit -m x", changed)
    assert ledger.consult(new, request_id="call-3") is None
    ledger.observe_execution("call-3")
    assert ledger.consult(old, request_id="call-4") is None


def test_a_corrupt_ledger_degrades_to_a_miss(tmp_path, scope):
    path = tmp_path / "cache.json"
    path.write_text("{not json")
    ledger = CapabilityLedger(path)
    key = key_for("git commit -m x", scope)
    assert ledger.consult(key, request_id="call-1") is None
    assert json.loads(path.read_text())["version"] == 1


def test_the_ledger_ignores_an_unclassifiable_request(tmp_path, scope):
    ledger = CapabilityLedger(tmp_path / "cache.json")
    assert ledger.consult(None, request_id="call-1") is None
    assert ledger.observe_execution("call-1") is None


def test_a_serialized_key_round_trips_every_field(tmp_path, scope):
    """Every key field must survive the ledger, or a grant silently never matches."""
    from dataclasses import fields

    from agentdeck.approval_cache import _key_from_json, _key_to_json

    key = key_for("git add src", scope)
    assert set(_key_to_json(key)) == {field.name for field in fields(key)}
    assert _key_from_json(_key_to_json(key)) == key


# --- corpus: the reduction this exists for, and the denials it must keep ---

# One worker's realistic escalation sequence, in the shape the 24-hour audit
# found: a long tail of repeated local git work, and a small number of remote or
# policy-sensitive actions that must keep reaching the guardian every time.
CORPUS = (
    [("git status", "src"), ("git add src", "src"), ("git commit -m 'step one'", "src")] * 12
    + [("git fetch origin", "."), ("git log --oneline -5", ".")] * 8
    + [("git diff", "src")] * 10
    # The three that must never be answered from cache:
    + [
        ("gh pr create --title x --body 'ignores the repo policy'", "."),
        ("git push origin HEAD", "."),
        ("rm -r src", "."),
    ]
)


def test_the_corpus_collapses_guardian_work_while_keeping_every_denial(tmp_path, worktree, scope):
    ledger = CapabilityLedger(tmp_path / "cache.json")
    reached_guardian = []
    for index, (command, subdir) in enumerate(CORPUS):
        request_id = f"call-{index}"
        key = capability_key(command, cwd=worktree / subdir, scope=scope)
        if ledger.consult(key, request_id=request_id) is not None:
            continue
        reached_guardian.append(command)
        # The guardian allows the low-risk ones; the three sensitive ones it
        # denies, so their tools never run and nothing is ever granted.
        if key is not None:
            ledger.observe_execution(request_id)

    assert len(CORPUS) == 65
    # One adjudication per distinct capability, not one per request: 65 escalations
    # become 9 guardian runs, a 86% reduction, with nothing sensitive among the
    # ones that were skipped.
    assert len(reached_guardian) == 9
    # Every sensitive command reached the guardian, every time it was asked.
    assert reached_guardian.count("git push origin HEAD") == 1
    assert "rm -r src" in reached_guardian
    assert any(command.startswith("gh pr create") for command in reached_guardian)


def test_a_sensitive_command_reaches_the_guardian_on_every_repetition(tmp_path, worktree, scope):
    ledger = CapabilityLedger(tmp_path / "cache.json")
    asked = 0
    for index in range(20):
        key = capability_key("git push origin HEAD", cwd=worktree, scope=scope)
        if ledger.consult(key, request_id=f"call-{index}") is None:
            asked += 1
            ledger.observe_execution(f"call-{index}")
    assert asked == 20


# --- the hook entry point -------------------------------------------------


def _run_hook(payload, ledger_path, monkeypatch=None):
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    from agentdeck.approval_cache import LEDGER_PATH_ENV

    script = _Path(__file__).resolve().parent.parent / "scripts" / "approval_cache_hook.py"
    import os as _os

    environment = dict(_os.environ, **{LEDGER_PATH_ENV: str(ledger_path)})
    completed = subprocess.run(
        [_sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _request(command, worktree, tool_use_id):
    return {
        "hook_event_name": "PermissionRequest",
        "session_id": "thread-1",
        "turn_id": "turn-1",
        "tool_use_id": tool_use_id,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(worktree),
        "model": "gpt-5",
        "permission_mode": "default",
        "transcript_path": None,
    }


def test_the_hook_defers_a_first_request_and_allows_the_repeat(tmp_path, worktree):
    ledger_path = tmp_path / "cache.json"
    assert _run_hook(_request("git commit -m 'one'", worktree, "call-1"), ledger_path) == ""
    _run_hook(
        {"hook_event_name": "PostToolUse", "tool_use_id": "call-1", "tool_name": "Bash"},
        ledger_path,
    )
    output = _run_hook(_request("git commit -m 'two'", worktree, "call-2"), ledger_path)
    decision = json.loads(output)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PermissionRequest"
    assert decision["decision"]["behavior"] == "allow"
    assert "call-1" in decision["decision"]["message"]


def test_the_hook_never_allows_a_remote_mutation(tmp_path, worktree):
    ledger_path = tmp_path / "cache.json"
    for index in range(3):
        assert (
            _run_hook(_request("git push origin HEAD", worktree, f"call-{index}"), ledger_path)
            == ""
        )
        _run_hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_use_id": f"call-{index}",
                "tool_name": "Bash",
            },
            ledger_path,
        )


def test_the_hook_stays_silent_without_a_configured_ledger(tmp_path, worktree):
    import os as _os
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    from agentdeck.approval_cache import LEDGER_PATH_ENV

    script = _Path(__file__).resolve().parent.parent / "scripts" / "approval_cache_hook.py"
    environment = {name: value for name, value in _os.environ.items() if name != LEDGER_PATH_ENV}
    completed = subprocess.run(
        [_sys.executable, str(script)],
        input=json.dumps(_request("git commit -m x", worktree, "call-1")),
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == ""


def test_the_hook_survives_junk_input(tmp_path):
    assert _run_hook({"hook_event_name": "PermissionRequest"}, tmp_path / "cache.json") == ""
    assert _run_hook([], tmp_path / "cache.json") == ""


def test_a_symlink_out_of_the_worktree_is_not_cacheable(scope, worktree, tmp_path):
    """A path that spells out as in-worktree can still write outside it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "escape").symlink_to(outside)

    # The write follows the link, so none of these may be keyed as in-worktree.
    assert key_for("touch escape/thing", scope) is None
    assert key_for("mkdir escape/sub", scope) is None
    assert key_for("git add escape", scope) is None
    assert key_for("ls escape", scope) is None


def test_a_symlink_inside_the_worktree_is_still_cacheable(scope, worktree):
    """Only an escaping link fails closed; an internal one is the same capability."""
    (worktree / "inside").symlink_to(worktree / "src")
    assert key_for("ls inside", scope) is not None


def test_a_worktree_reached_through_a_symlink_still_matches_itself(tmp_path):
    """A symlinked worktree root must not make every path look like an escape."""
    real = tmp_path / "real"
    (real / ".git").mkdir(parents=True)
    (real / ".git" / "HEAD").write_text("ref: refs/heads/master\n")
    (real / "src").mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    linked_scope = WorkerScope(
        worker="thread-1",
        repository=str(link / ".git"),
        worktree=str(link),
        policy_fingerprint=policy_fingerprint(link),
    )
    key = capability_key("git add src", cwd=link, scope=linked_scope)
    assert key is not None
    assert key.path_scope == ("src",)
