"""The engine's command line: the host operations a Poller Instance is driven through (#178).

The engine runs under the checkout's venv, from the same deployed AgentDeck checkout as the
host adapter (#2153, #2597) — ``scripts/poller_engine.py`` is its launcher — so this is its
whole entry point. It exposes the host operations:

* ``inspect`` — the offline configuration check (#177 stage 1). No network, no secrets, so
  it is safe to run against every checked-in Manifest before a deploy.
* ``activate`` — dry-run, or apply: resolve the board once, provision, publish the binding (#2157).
* ``cycle`` — one wake (#176).
* ``propose`` — stage one operator-supplied reference through an extension for the next wake.
* ``contract-test`` — check an extension program against the hook protocol (#179).
* ``reap-worktrees`` — list, and only on request release, the host worktrees no Work
  claims. Operator-driven and dry-run by default; no wake ever calls it.

``cycle`` composes the pieces: the config layer loads what was activated and proves it has not
drifted, and the GitHub adapter supplies the reads and performs the effects. The engine holds no
cards and starts no workers: the deck runs every Work (#3732).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from . import authorization as auth
from . import config, extensions, github, github_authority, reaper

_ADAPTER_MISSING = (
    "this engine has no host adapter installed, so {op} cannot run.\n"
    "The adapter implements EnginePort (real GitHub reads, worker delivery, and the\n"
    "effects the engine declares) and lands with the activation state machine (#180)."
)


def allowed_hooks(manifest) -> dict:
    """Each enabled extension's hook allow-list, which is its Manifest ``hooks`` list (#2158)."""
    return {
        name: frozenset(extensions.Hook(hook) for hook in hooks)
        for name, hooks in manifest.extension_hooks.items()
    }


def _enabled_extensions(manifest, overlay, *, wake=False) -> tuple[list, list]:
    """The extension programs this Manifest enables, and everything wrong with finding them.

    Offline and at activation, an extension that has no hook allow-list, cannot be found, will
    not say what it implements, or lacks an allowed hook is a problem rather than a silent
    omission: a project that activates believing something is being watched, when nothing is,
    is worse than one that refuses to activate. A ``wake`` loads the same extension refused
    instead — reported degraded, invoked at no hook — because one broken extension must not
    stop an otherwise healthy poller, and a refused release gate still holds its release.
    """
    roots = [Path(path).expanduser() for path in (overlay.checkouts or {}).values()]
    shared = [Path(overlay.extensions_dir).expanduser()] if overlay.extensions_dir else []
    return extensions.discover(
        # The merged settings (#386): what an extension is *given* includes the host's half, so a
        # credential the Overlay supplies is present by the time the program runs.
        config.effective_extensions(manifest, overlay),
        roots,
        allowed=allowed_hooks(manifest),
        extra_dirs=shared,
        interpreter=sys.executable,
        wake=wake,
    )


def _extension_table(draft) -> tuple[dict, list]:
    """The config validators for a draft's enabled extensions, for the offline check."""
    found, problems = _enabled_extensions(draft.manifest, draft.overlay)
    return (
        {ext.name: extensions.config_validator(ext) for ext in found},
        problems,
    )


def _cmd_inspect(args: argparse.Namespace) -> int:
    first = config.inspect(args.manifest, args.overlay)
    validators, discovery_problems = _extension_table(first)
    draft = config.inspect(args.manifest, args.overlay, extension_validators=validators)
    draft.report.extend(discovery_problems)
    if draft.ok:
        print(f"ok: contract {draft.manifest.contract}, {len(draft.manifest.repos)} repositories")
        return 0
    # Collect-all: every problem at once, so one run tells the whole story (#177).
    print(draft.report.render(), file=sys.stderr)
    print(f"\n{len(draft.report)} problem(s); nothing will activate until they are fixed",
          file=sys.stderr)
    return 1


def _cmd_contract_test(args: argparse.Namespace) -> int:
    """Run an extension's declared hooks against the fixture context (#112's harness)."""
    try:
        hooks = frozenset(extensions.Hook(name) for name in args.hook)
    except ValueError as exc:
        print(f"unknown hook: {exc}", file=sys.stderr)
        return 2
    extension = extensions.Extension(
        name=args.name, program=tuple(args.program), hooks=hooks, config={}
    )
    problems = extensions.contract_test(extension)
    if not problems:
        print(f"ok: {args.name} answers every declared hook as the engine expects")
        return 0
    for problem in problems:
        print(str(problem), file=sys.stderr)
    return 1


def _instance_gh(manifest) -> github.Gh:
    """The one `gh` path this instance speaks through: its owner's authority (#2289).

    The credential is fetched per call, so an App installation token that expires during a
    wake is minted again. An owner with no authority gives a `Gh` whose every call refuses
    with the reason; `_authority` is what refuses the wake itself up front.
    """
    owner = manifest.board.org

    def token_source() -> str:
        return github_authority.token(github_authority.authority_for(owner))

    try:
        name = github_authority.authority_for(owner).name
    except github_authority.Refusal:
        name = ""
    return github.Gh(authority=name, token_source=token_source)


def _authority(manifest) -> github_authority.Authority:
    """This instance's authority, with a credential that can actually be produced.

    Raises ``github_authority.Refusal``: an empty or unreadable credential stops the wake
    here, with its reason, rather than letting any call reach GitHub as somebody else.
    """
    authority = github_authority.authority_for(manifest.board.org)
    github_authority.token(authority)
    return authority


def _policy(manifest, authority: github_authority.Authority) -> auth.Policy:
    """The controller policy, with the engine's own account set to the authority it speaks as.

    The bot must be the identity the engine's comments are signed with, or the engine could
    authorize itself. A Manifest that names a different bot is refused rather than trusted;
    one that names none gets the authority's login.
    """
    policy = auth.parse_policy(manifest.authorization)
    login = authority.login.lower()
    if policy.bot and policy.bot != login:
        raise github_authority.Refusal(
            f"the Manifest names bot {policy.bot!r}, but this instance speaks as "
            f"{authority.login}; set [authorization] bot = \"{authority.login}\""
        )
    return replace(policy, bot=login)


def _hand_token_to_extensions(installed: list, loaded, authority) -> list | None:
    """Give every extension the instance's own credential, and nothing wider (#2289).

    One 0600 file beside the instance's state, rewritten each wake so an installation token in
    it is always fresh. ``github_token_file`` names it, and ``github_token_files`` maps the
    instance's case-folded owner to the same file. No human token is ever exposed to an
    extension.

    Until #3741 the map also held the owner of the review-chat repository, for the
    ``chatgpt-nudge`` extension that read its chats; that extension is retired, so no instance
    holds another owner's credential any more.
    """
    state = Path(loaded.overlay.state_path)
    path = state.with_name("github-token")
    try:
        _write_credential(path, github_authority.token(authority))
    except (OSError, github_authority.Refusal) as exc:
        # Refused rather than degraded: an extension run without its handed credential
        # would read GitHub as whatever it could find instead.
        print(f"deferred: the extensions' GitHub credential was not written: {exc}",
              file=sys.stderr)
        return None
    files = {authority.owner: str(path)}
    # A file for another owner (the review-chat owner's, before #3741) is removed, so no copy
    # of a credential outlives its grant.
    for stale in state.parent.glob("github-token-*"):
        stale.unlink(missing_ok=True)
    return [
        replace(
            ext,
            config={**ext.config, "github_token_file": str(path), "github_token_files": files},
        )
        if isinstance(ext.config, dict)
        else ext
        for ext in installed
    ]


def _write_credential(path: Path, credential: str) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".github-token-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(credential + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _adapter(loaded: config.PollerConfig) -> github.GitHubAdapter:
    """The GitHub adapter for this project, speaking only as the instance's authority."""
    gh = _instance_gh(loaded.manifest)
    return github.GitHubAdapter(
        org=loaded.manifest.board.org,
        issue_repos=loaded.manifest.issue_repos(),
        project_number=loaded.manifest.board.project_number,
        ownership_label=loaded.manifest.board.ownership_label,
        comment_guide=loaded.manifest.comment_guide,
        api=gh,
        host=gh,
        graphql_reserve=loaded.manifest.graphql_reserve,
        core_reserve=loaded.manifest.core_reserve,
    )


def _cmd_cycle(args: argparse.Namespace) -> int:
    """One wake: load what was activated, run the cycle, and log what it did."""
    from .cycle import WEBHOOK_QUIET_ENV, run_cycle
    from .state import EnginePaths, StateStore

    try:
        loaded = config.load(args.manifest, args.overlay, args.binding)
    except config.DriftError as exc:
        # Fail closed: what is on disk is not what was approved (#109). Record the refusal as
        # a health fact before returning, or the instance simply goes quiet — and a quiet
        # instance reads as stale rather than as refused, which is the exact confusion #107's
        # health record exists to prevent.
        print(exc.report.render(), file=sys.stderr)
        _record_drift(args, exc)
        return 75  # EX_TEMPFAIL: re-activation is the fix, not a retry
    except config.ConfigError as exc:
        # The same for configuration this software cannot load at all (#2153): a deploy whose
        # engine no longer accepts one project's files stops that instance, and its health
        # says why, while every other project keeps running. An instance never activated has
        # no health to report yet, and is left exactly as it was.
        print(exc.report.render(), file=sys.stderr)
        if Path(args.binding).exists():
            _record_drift(args, exc)
        return 78

    # Each extension runs at the hooks its Manifest allows, whatever build of it is deployed
    # (#2158). One that cannot run is loaded refused — reported, invoked at no hook — rather
    # than refusing the wake.
    installed, problems = _enabled_extensions(loaded.manifest, loaded.overlay, wake=True)
    if problems:
        # Running without an extension the Manifest enables would silently stop watching
        # something the project believes is watched.
        for problem in problems:
            print(str(problem), file=sys.stderr)
        return 78

    # The instance's GitHub authority, before anything reaches GitHub (#2289). No authority,
    # no credential, or a Manifest bot that is not that authority stops the wake, and the
    # health record says why; nothing falls back to another identity.
    try:
        authority = _authority(loaded.manifest)
        policy = _policy(loaded.manifest, authority)
    except github_authority.TransientRefusal as refusal:
        # GitHub could not mint the token just now: the next wake tries again.
        print(f"deferred: github: {refusal}", file=sys.stderr)
        return 75  # EX_TEMPFAIL
    except github_authority.Refusal as refusal:
        print(f"refused: github: {refusal}", file=sys.stderr)
        _record_drift(args, f"GitHub authority refused: {refusal}")
        return 77  # EX_NOPERM
    installed = _hand_token_to_extensions(installed, loaded, authority)
    if installed is None:
        return 75  # EX_TEMPFAIL: the credential could not be handed over this wake

    paths = EnginePaths.from_base(loaded.overlay.state_path)
    store = StateStore(paths)
    adapter = _adapter(loaded)
    outcome = run_cycle(
        store,
        adapter,
        now=datetime.now(UTC),
        issue_repos=frozenset(loaded.manifest.issue_repos()),
        enabled_extensions=installed,
        policy=policy,
        proposal_daily_cap=loaded.manifest.proposal_daily_cap,
        webhook_skip_s=loaded.manifest.webhook_skip_s,
        spec_only_label=loaded.manifest.board.spec_only_label,
        # The host's quiet hint arrives as an environment variable (#1631).
        webhook_quiet=os.environ.get(WEBHOOK_QUIET_ENV) == "1",
    )
    for line in outcome.log:
        print(line, file=sys.stderr)
    return 0 if not outcome.halted else 75


def _cmd_propose(args: argparse.Namespace) -> int:
    """Resolve one reference through an extension allowed `propose`, and stage its requests."""
    from .cycle import extension_memory, stage_proposals, valid_proposal_request
    from .state import EnginePaths, StateStore

    try:
        loaded = config.load(
            args.manifest,
            args.overlay,
            args.binding,
        )
    except config.DriftError as exc:
        print(exc.report.render(), file=sys.stderr)
        return 75
    except config.ConfigError as exc:
        print(exc.report.render(), file=sys.stderr)
        return 78

    installed, problems = _enabled_extensions(loaded.manifest, loaded.overlay, wake=True)
    if problems:
        for problem in problems:
            print(str(problem), file=sys.stderr)
        return 78
    extension = next((item for item in installed if item.name == args.extension), None)
    if extension is None:
        print(f"extension {args.extension!r} is not enabled", file=sys.stderr)
        return 64
    if not extension.implements(extensions.Hook.PROPOSE):
        print(f"extension {args.extension!r} does not accept proposals", file=sys.stderr)
        return 64

    response = extensions.invoke(
        extension,
        extensions.Hook.PROPOSE,
        context={
            "reference": args.reference,
            # So a reference resolves to the key the observer gave it (#2691).
            "memory": extension_memory(
                StateStore(EnginePaths.from_base(loaded.overlay.state_path)).read(),
                extension.name,
            ),
        },
        mode=extensions.Mode.LIVE,
    )
    if not response.ok:
        print(response.degraded, file=sys.stderr)
        return 75
    if response.problems:
        for problem in response.problems:
            print(f"{problem.location}: {problem.what}; {problem.fix}", file=sys.stderr)
        return 65
    if any(r.kind is extensions.RequestKind.QUEUE_WORK for r in response.requests):
        # It asked for work on a card; the engine holds none since #3732.
        print(
            f"extension {args.extension!r} asked to queue work on a card, and the deck runs "
            "every Work; only an issue to open (open-work) can be proposed",
            file=sys.stderr,
        )
        return 65
    requests = tuple(
        request for request in response.requests
        if request.kind is extensions.RequestKind.OPEN_WORK
    )
    if not requests:
        print(f"extension {args.extension!r} proposed no work", file=sys.stderr)
        return 65
    if any(not valid_proposal_request(request) for request in requests):
        print(
            f"extension {args.extension!r} returned a malformed proposal request",
            file=sys.stderr,
        )
        return 65

    allowed = frozenset(loaded.manifest.issue_repos())
    refused = [
        str(request.payload.get("repo") or "")
        for request in requests
        if str(request.payload.get("repo") or "") not in allowed
    ]
    if refused:
        print(f"refused issue repository: {refused[0]!r}", file=sys.stderr)
        return 65

    store = StateStore(EnginePaths.from_base(loaded.overlay.state_path))
    with store.transaction() as state:
        added = stage_proposals(state, extension.name, requests)
    if not added:
        print(f"already proposed: {args.reference}")
        return 0
    print(f"queued {args.reference} through {extension.name} for the next wake")
    return 0


def _record_drift(args: argparse.Namespace, exc) -> None:
    """Write a refusal to load this instance's configuration into its health record.

    The config cannot be trusted, so the state path is read from the Overlay alone — the one
    file the drift check does not fingerprint. The cycle's drift branch returns before
    it touches the port, which is why none is needed here.
    """
    from .cycle import run_cycle
    from .state import EnginePaths, StateStore

    draft = config.inspect(args.manifest, args.overlay)
    if not draft.overlay or not draft.overlay.state_path:
        return
    try:
        store = StateStore(EnginePaths.from_base(draft.overlay.state_path))
        # The first problem rides along, so the record says *what* no longer loads.
        problems = getattr(getattr(exc, "report", None), "problems", None) or []
        detail = f"{exc}: {problems[0]}" if problems else str(exc)
        run_cycle(store, None, now=datetime.now(UTC), drift=detail)
    except OSError as write_failure:  # an unwritable state dir is already its own symptom
        print(f"could not record the drift refusal: {write_failure}", file=sys.stderr)


def _format_size(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}G"


def _work_claimed_paths(socket_path: str) -> list[str] | None:
    """Every path an active Work workspace claim holds, or None when unreadable (#2149)."""
    payload = github._runtime_get(socket_path, github.WORK_WORKSPACE_CLAIMS_PATH)
    rows = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    return [str(row["path"]) for row in rows if isinstance(row, dict) and row.get("path")]


def _cmd_reap_worktrees(args: argparse.Namespace) -> int:
    """List the host worktrees no Work claims, and release them only when asked.

    The default is a report. `--apply` is the whole difference between this command and a
    deletion, and it is deliberately not the default: the engine has no way to tell a
    forgotten card's leftovers from a live worker's checkout except the conservative rules in
    `reaper`, so a person reads the list first.
    """
    draft = config.inspect(args.manifest, args.overlay)
    if not draft.overlay or not draft.overlay.state_path:
        print(draft.report.render(), file=sys.stderr)
        return 78  # EX_CONFIG: without an Overlay there is nothing to scan and nothing to trust

    overlay = draft.overlay
    # Work claims (#2149) are the one owner of a worktree (#2195): a Work's worktree outlives
    # any card record, and every card worktree is one. They are host-wide, so a checkout two
    # instances share is covered too. Unread, they are unknown rather than absent, so a report
    # says so and an apply refuses.
    # The engine never names the runtime's socket itself; `scripts/poller_engine.py` supplies it.
    claimed: list[str] = []
    work_claims = _work_claimed_paths(args.runtime_socket) if args.runtime_socket else None
    if work_claims is None:
        print(
            "Work workspace claims could not be read from the runtime; a worktree a Work "
            "owns may be listed as reapable",
            file=sys.stderr,
        )
        if args.apply:
            print("refused: --apply needs the runtime's Work claims", file=sys.stderr)
            return 75
    else:
        claimed.extend(work_claims)

    checkouts = sorted(set((overlay.checkouts or {}).values()))
    if not checkouts:
        print("no checkouts configured for this instance; nothing to scan")
        return 0

    verdicts = reaper.plan(
        reaper.scan(checkouts, now=datetime.now(UTC)),
        claimed,
        min_age_days=args.min_age_days,
    )
    reapable = [item for item in verdicts if item.reap]
    kept = [item for item in verdicts if not item.reap]

    print(
        f"scanned {len(verdicts)} worktrees under {len(checkouts)} checkouts: "
        f"{len(reapable)} reapable, {len(kept)} kept"
    )
    for item in sorted(kept, key=lambda held: (held.reason, held.path)):
        print(f"  keep  {item.reason:<12} {item.path}")
    total = 0
    for item in sorted(reapable, key=lambda ready: ready.path):
        size = reaper.directory_size(item.path) if args.size else None
        if size:
            total += size
        stamp = f" {_format_size(size):>7}" if args.size else ""
        print(f"  reap {stamp} {item.path}")
    if args.size and reapable:
        print(f"  reclaimable: {_format_size(total)}")

    if not args.apply:
        if reapable:
            print("dry run: nothing was removed. Re-run with --apply to release these.")
        return 0

    cleanup = github.GitHubAdapter.remove_worktrees([item.path for item in reapable])
    # A candidate the scan judged clean can have been written to since, and the removal
    # itself re-checks (#1718). Such a path is kept, not failed: nothing went wrong, the
    # directory simply stopped being reapable between the two reads.
    for path, reason in cleanup.retained:
        print(f"  keep  {'dirty-now':<12} {path} ({reason})")
    released = len(reapable) - len(cleanup.failed) - len(cleanup.retained)
    if cleanup.failed:
        for path in cleanup.failed:
            print(f"  failed {path}", file=sys.stderr)
        print(f"released {released}, {len(cleanup.failed)} failed")
        return 1
    print(f"released {released} worktrees")
    return 0


def activation_plan(manifest) -> dict:
    """What an apply of this Manifest touches on the board side, for the dry run to show."""
    return {
        "labels": list(manifest.board.accepted_ownership_labels),
        "repos": list(manifest.issue_repos()),
        "board": manifest.board.project_number,
        # A boardless project's `board` is zero, and zero on its own reads as "not read yet"
        # rather than as a decision (#1481).
        "status_backend": "labels" if manifest.board.boardless else "project",
    }


def _extension_plan(installed: list) -> dict:
    """What each extension would run as after this apply, for the dry run to show (#2158).

    The hooks are what the Manifest allows; ``ignored_hooks`` are ones the program declares that
    it does not.
    """
    plan = {}
    for extension in installed:
        entry = {
            "source": extension.source,
            "hooks": sorted(hook.value for hook in extension.hooks),
        }
        if extension.ignored_hooks:
            entry["ignored_hooks"] = sorted(hook.value for hook in extension.ignored_hooks)
        plan[extension.name] = entry
    return plan


def _cmd_activate(args: argparse.Namespace) -> int:
    """Apply this project's configuration (#2157): one validated, idempotent operation.

    Without ``--apply`` this is a dry run: it validates, resolves, and prints what an apply
    would provision and publish, plus a read-only shadow cycle as a diagnostic. It writes
    nothing and persists nothing, so nothing later has to prove it saw it.

    ``--apply LOGIN`` is the approval. It checks Release authority now, re-inspects and
    re-resolves the current config, provisions what is missing, and only when every step
    landed installs ``--manifest`` as the instance's ``poller.toml`` (beside ``--binding``,
    where the wake reads it) and publishes the binding resolved from it, as one pair. A
    failure before that leaves the previously installed pair selected and untouched.
    """
    from . import activation
    from .cycle import run_cycle
    from .extensions import Mode
    from .state import EnginePaths, StateStore

    now = datetime.now(UTC)
    # Locate the extension programs exactly as `inspect` does. Without this every enabled
    # extension is unvalidatable, so a project that inspects clean could never activate.
    first = config.inspect(args.manifest, args.overlay)
    installed, discovery_problems = _enabled_extensions(first.manifest, first.overlay)
    validators = {ext.name: extensions.config_validator(ext) for ext in installed}
    draft = config.inspect(args.manifest, args.overlay, extension_validators=validators)
    draft.report.extend(discovery_problems)
    if not draft.ok:
        print(draft.report.render(), file=sys.stderr)
        return 78

    try:
        authority = _authority(draft.manifest)
        policy = _policy(draft.manifest, authority)
    except github_authority.Refusal as refusal:
        print(f"refused: github: {refusal}", file=sys.stderr)
        return 77  # EX_NOPERM
    loaded, report = config.resolve(
        args.manifest, args.overlay, _adapter_for_draft(draft),
        extension_validators=validators,
    )
    if loaded is None or not report.ok:
        print(report.render(), file=sys.stderr)
        return 78

    paths = EnginePaths.from_base(loaded.overlay.state_path)
    adapter = _adapter(loaded)
    if args.apply is not None:
        # The one gate, taken after the read-only lookups and before anything is written.
        # Uncertainty is not approval: an unreadable permission has confirmed nothing.
        repos = loaded.manifest.issue_repos()
        permission = adapter.permission(repos[0], args.apply) if repos else None
        # A deck that is not a shared project's main one runs none of its extensions, takes only
        # its own person's issues and cannot change what the main deck merges, so turning it on
        # is Work, not Release (#3755). Merging there still checks Release, merge by merge.
        main = loaded.manifest.main_deck
        tier = auth.Tier.WORK if main and not config.is_main_deck(main) else auth.Tier.RELEASE
        if not auth.holds(policy, tier, login=args.apply, permission=permission):
            who = args.apply or "nobody"
            name = "Work" if tier is auth.Tier.WORK else "Release"
            print(f"refused: {who} does not hold {name} authority", file=sys.stderr)
            return 77  # EX_NOPERM: refused, not broken
    steps = _provision_steps(loaded, adapter, paths)
    installed_manifest = Path(args.binding).with_name("poller.toml")
    candidate = Path(args.manifest)
    installs_manifest = candidate.resolve() != installed_manifest.resolve()

    if args.apply is None:
        # A diagnostic, not a gate. `_ShadowPort` stops every *external* mutation, because
        # those all leave through one declared-effect chokepoint; durable state is written by
        # `store.transaction()`, so the cycle runs against a throwaway copy of it (#318).
        with _scratch_store(paths) as scratch:
            shadow = run_cycle(
                scratch,
                _ShadowPort(adapter),
                now=now,
                issue_repos=frozenset(loaded.manifest.issue_repos()),
                enabled_extensions=installed,
                policy=policy,
                proposal_daily_cap=loaded.manifest.proposal_daily_cap,
                spec_only_label=loaded.manifest.board.spec_only_label,
                mode=Mode.SHADOW,
            )
        shadow_summary = {
            "would_apply": [effect.kind.value for effect in shadow.effects],
        }
        print(json.dumps({
            "dry_run": {
                "instance": args.instance,
                "plan": activation_plan(loaded.manifest),
                "provision": [step.name for step in steps],
                "installs_manifest": str(installed_manifest) if installs_manifest else None,
                "extensions": _extension_plan(installed),
                "binding": loaded.binding.as_json(),
                "shadow": shadow_summary,
            },
        }, indent=2))
        return 0

    failed = activation.provision(steps)
    if failed:
        # Nothing is published, so the binding a wake reads is still the previous one.
        for step in failed:
            print(f"provisioning failed: {step.name} {step.detail}".rstrip(), file=sys.stderr)
        print("nothing was published; apply again once the step can complete", file=sys.stderr)
        return 75

    stamp = now.astimezone(UTC).isoformat()
    loaded = replace(
        loaded,
        binding=replace(
            loaded.binding, approval={"activated_by": args.apply, "activated_at": stamp}
        ),
    )
    record_path = Path(loaded.overlay.state_path).with_name("activation.json")
    record: dict = {}

    def audit_for(previous: bytes | None) -> bytes:
        # Called under the same lock the pair is replaced under, so `previous` is the record
        # of the pair this apply replaces, even with another apply racing it.
        try:
            prior = json.loads(previous) if previous else None
        except ValueError:
            prior = None
        record.update(activation.audit_record(
            instance=args.instance,
            contract=loaded.manifest.contract,
            fingerprint=dict(loaded.binding.fingerprint),
            activated_by=args.apply,
            now=now,
            previous=prior if isinstance(prior, dict) else None,
        ))
        return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()

    # The commit: the candidate Manifest installed where the wake reads it, the binding
    # resolved from it, and the audit that describes them, as one unit under the config lock.
    # `publish` re-reads and checks the candidate inside that lock, and puts every file back
    # if any of them cannot be replaced.
    try:
        config.publish(
            args.binding,
            loaded.binding,
            manifest_path=installed_manifest,
            candidate_path=candidate,
            audit_path=record_path,
            audit_for=audit_for,
        )
    except config.ConfigError as exc:
        print(exc.report.render(), file=sys.stderr)
        return 78
    except OSError as exc:
        print(f"nothing was published: {exc}", file=sys.stderr)
        return 74  # EX_IOERR

    # A postcondition, not a phase: lifting the old hold is idempotent, so a failure here asks
    # for the same apply again, which recommits the same files and tries once more.
    if not _release_stand_down(StateStore(paths)):
        print("published, but the old activation hold is still on; apply again", file=sys.stderr)
        return 74
    print(f"applied {args.instance}: binding {record['fingerprint'][:16]} by {args.apply}")
    return 0


# The pause note the staged activation (#180) left on a first activation it rolled back. It is
# no longer written; an apply that succeeds still lifts one, so no instance stays held by it.
_STAND_DOWN = "activation rolled back"


def _release_stand_down(store) -> bool:
    """Lift only a hold the old activation placed; an operator's own pause is theirs to lift.

    False only when a hold of ours is, or may still be, in place.
    """
    from . import health
    from .state import StateError

    try:
        if not health.pause_note(store.read()).startswith(_STAND_DOWN):
            return True
        with store.transaction() as state:
            if health.pause_note(state).startswith(_STAND_DOWN):
                health.set_paused(state, False)
    except (OSError, StateError) as exc:
        print(f"could not release the old activation hold: {exc}", file=sys.stderr)
        return False
    return True


def _adapter_for_draft(draft) -> github.GitHubAdapter:
    """A read-only adapter for discovery, before any binding exists."""
    gh = _instance_gh(draft.manifest)
    return github.GitHubAdapter(
        org=draft.manifest.board.org,
        issue_repos=draft.manifest.issue_repos(),
        project_number=draft.manifest.board.project_number,
        ownership_label=draft.manifest.board.ownership_label,
        api=gh,
        host=gh,
    )


def _provision_steps(loaded, adapter, paths) -> list:
    """Add-only, idempotent setup: the project's labels, and this instance's directories."""
    from .activation import ProvisionStep

    def ensure_label(repo: str, name: str, description: str, step: str):
        def apply() -> bool:
            slug = f"{loaded.manifest.board.org}/{repo}"
            code, _out, err = adapter.api.run(
                "label", "create", name, "-R", slug, "--description", description,
            )
            # Already existing is success: provisioning only ever adds what is missing.
            return code == 0 or "already exists" in (err or "").lower()

        return ProvisionStep(f"{step}:{repo}", apply)

    def ensure_dirs() -> bool:
        for directory in (paths.state.parent,):
            try:
                Path(directory).mkdir(parents=True, exist_ok=True)
            except OSError:
                return False
        return True

    board = loaded.manifest.board
    steps = [
        ensure_label(
            repo,
            label,
            "The Poller Engine owns this card",
            "label" if label == board.ownership_label else f"ownership-alias-label:{label}",
        )
        for repo in loaded.manifest.issue_repos()
        for label in board.accepted_ownership_labels
    ]
    if board.boardless:
        # These labels are this project's board (#1481). Every role, not only the ones the
        # engine happens to write first: a status write to a label the repository does not
        # define fails, and a failed effect is retried every wake rather than dropped.
        steps += [
            ensure_label(
                repo,
                config.status_label(role),
                f"Poller card status: {role.value}",
                "status-label",
            )
            for repo in loaded.manifest.issue_repos()
            for role in config.Role
        ]
    if board.spec_only_label:
        steps += [
            ensure_label(
                repo,
                board.spec_only_label,
                "Write a specification only; wait for human approval before implementation",
                "spec-only-label",
            )
            for repo in loaded.manifest.issue_repos()
        ]
    if board.paused_label:
        steps += [
            ensure_label(
                repo,
                board.paused_label,
                "Paused: the Poller starts, restarts and merges nothing here until it is removed",
                "paused-label",
            )
            for repo in loaded.manifest.issue_repos()
        ]
    # Every board can hold scheduled tasks (#2517), so every issue repository gets the label a
    # worker or a person puts on one.
    from .schedules import LABEL as SCHEDULED_LABEL

    steps += [
        ensure_label(
            repo,
            SCHEDULED_LABEL,
            "A repeating task: files a new card on the schedule in its When: line",
            "scheduled-label",
        )
        for repo in loaded.manifest.issue_repos()
    ]
    return steps + [ProvisionStep("directories", ensure_dirs)]


@contextmanager
def _scratch_store(paths):
    """A copy of this instance's durable state that the caller may ruin, and then discard.

    The shadow poll needs the real state to decide anything useful — what is in flight, what the
    ownership ledger has already seen — but a decision it records is a decision no later wake
    will make again. Running it against a copy keeps the reads real and throws the writes away.

    Copied rather than mocked, and file-for-file, so the store loads through exactly the path it
    normally does: a preview that read state differently from a wake would preview a different
    engine. Only the store's own files come along; the acknowledgement directories stay where
    they are, because the adapter reads those and the adapter is not what is being sandboxed.
    """
    from .state import EnginePaths, StateStore

    with tempfile.TemporaryDirectory(prefix="poller-shadow-") as scratch:
        base = Path(scratch) / paths.state.name
        copy = EnginePaths.from_base(base)
        for source, target in (
            (paths.state, copy.state),
            (paths.backup, copy.backup),
            (paths.controls, copy.controls),
            (paths.controls_backup, copy.controls_backup),
            (paths.ownership, copy.ownership),
        ):
            # A file that does not exist yet is not an error: an instance activating for the
            # first time has no state, and the shadow of an empty world is an empty shadow.
            if source.exists():
                shutil.copy2(source, target)
        yield StateStore(copy)


class _ShadowPort:
    """The adapter with its *external* writes turned into records.

    Every mutation the engine wants to make to GitHub leaves through one declared-effect
    chokepoint, so intercepting that one method covers all of them. It does **not** cover
    durable state, which never passes through here — see `_scratch_store` and #318.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.recorded: list = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def apply(self, effect) -> bool:
        self.recorded.append(effect)
        return True

    def cancel_job(self, job_id) -> bool:
        # The one engine write that is not a GitHub effect (#2350): a dry run must not stop
        # a real suite either.
        self.recorded.append(("cancel_job", job_id))
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="poller-engine", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    def with_config(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subcommands.add_parser(name, help=help_text)
        sub.add_argument("--manifest", required=True, help="path to the Poller Manifest (TOML)")
        sub.add_argument("--overlay", required=True, help="path to the Host Overlay (TOML)")
        return sub

    with_config("inspect", "offline configuration check").set_defaults(handler=_cmd_inspect)
    activate = with_config(
        "activate", "dry-run, or with --apply apply, this project's configuration (#2157)"
    )
    activate.add_argument("--binding", required=True, help="where to write the resolved binding")
    activate.add_argument("--instance", required=True, help="this project's instance id")
    activate.add_argument(
        "--apply", "--approve", dest="apply", metavar="LOGIN",
        help="apply the current config as this Release Controller; without it, a dry run",
    )
    activate.set_defaults(handler=_cmd_activate)
    cycle = with_config("cycle", "run one wake")
    cycle.add_argument("--binding", required=True, help="path to the frozen resolved binding")
    # Accepted and unused since the engine stopped asking the deck about workers (#3741):
    # `scripts/poller_wake.py` still passes both, and an unknown flag would refuse every wake.
    cycle.add_argument("--agentdeck-url", default="", help=argparse.SUPPRESS)
    cycle.add_argument("--runtime-socket", default="", help=argparse.SUPPRESS)
    cycle.set_defaults(handler=_cmd_cycle)
    propose = with_config("propose", "stage one extension proposal for the next wake")
    propose.add_argument("--binding", required=True, help="path to the frozen resolved binding")
    propose.add_argument("--extension", required=True, help="enabled extension to resolve the ref")
    propose.add_argument("reference", help="extension-specific reference to propose")
    propose.set_defaults(handler=_cmd_propose)

    reap = with_config(
        "reap-worktrees", "list host worktrees no Work claims (dry run by default)"
    )
    reap.add_argument(
        "--min-age-days",
        type=float,
        default=reaper.DEFAULT_MIN_AGE_DAYS,
        help="how quiet a worktree must have been before it is even considered",
    )
    reap.add_argument("--size", action="store_true", help="also report what each one holds")
    reap.add_argument(
        "--runtime-socket",
        default="",
        help="the runtime whose Work workspace claims must be respected (the launcher supplies"
        " the live one; without it --apply refuses)",
    )
    # `--dry-run` is what this command already does. It exists so the safe spelling can be
    # written out explicitly in a runbook, and so it cannot be confused for the opposite.
    mode = reap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (the default)")
    mode.add_argument("--apply", action="store_true", help="actually release the listed worktrees")
    reap.set_defaults(handler=_cmd_reap_worktrees)

    contract = subcommands.add_parser(
        "contract-test", help="check an extension program against the hook protocol"
    )
    contract.add_argument("--name", required=True, help="the extension's name")
    contract.add_argument(
        "--hook",
        action="append",
        required=True,
        help="a hook the extension implements (repeatable)",
    )
    contract.add_argument("program", nargs="+", help="the program to run, and its arguments")
    contract.set_defaults(handler=_cmd_contract_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    # No inherited GitHub credential survives into this process or anything it starts —
    # extensions included: the instance's own authority is the only one (#2289).
    for name in github_authority.TOKEN_VARS:
        os.environ.pop(name, None)
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
