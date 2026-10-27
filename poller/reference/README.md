# The AgentDeck Reference Poller Project

This directory is the canonical example a fresh operator copies (#183). It contains the two files
a Poller Project is made of, and nothing else — the engine, the launcher, the installer, the
systemd units and the extension programs all live in AgentDeck and are never copied into a
project. (Extensions only became true of that sentence in #377: before it, they were findable
only under a project's own checkout, so every project but this one had to vendor a copy. Point
`overlay.paths.extensions` at this repo's `poller/extensions` folder.)

| File | What it is |
| --- | --- |
| `poller.toml` | The **Poller Manifest**: portable, committed, readable. Names the board and repos semantically, states the rules, and carries one block per enabled extension. No secrets, no machine paths, no GitHub node ids. |
| `overlay.example.toml` | A template for the **Host Overlay**: credentials, the agent account, and paths. The real file is machine-local and never committed. |

## Bringing it up

```sh
# 1. install the host layout and the templated units (enables nothing)
python3 scripts/install_poller_host.py --instance agentdeck

# 2. fill in the overlay (the engine is this checkout's scripts/poller_engine.py; nothing to build)
cp poller/reference/overlay.example.toml \
   ~/.local/share/agentdeck/poller/instances/agentdeck/overlay.toml
$EDITOR ~/.local/share/agentdeck/poller/instances/agentdeck/overlay.toml

# 3. check it offline — no network, no credentials, every problem at once
scripts/poller_engine.py inspect \
  --manifest poller/reference/poller.toml \
  --overlay ~/.local/share/agentdeck/poller/instances/agentdeck/overlay.toml

# 4. optional dry run: discovery, the plan, and a shadow cycle — writes nothing
scripts/poller_engine.py activate --instance agentdeck \
  --manifest poller/reference/poller.toml \
  --overlay ~/.../overlay.toml --binding ~/.../binding.json

# 5. a Release Controller applies the current config — this is the approval
scripts/poller_engine.py activate --instance agentdeck \
  --manifest … --overlay … --binding … \
  --apply <github-login>

# 6. only then does anything wake on its own
systemctl --user daemon-reload
systemctl --user enable --now agentdeck-poller@agentdeck.timer
```

Steps 1–4 are read-only with respect to GitHub. Step 5 is the single human gate (#2157): it
checks the login's Release authority, re-resolves the config as it is at that moment, adds any
missing labels and directories, and only then installs `--manifest` as the instance's
`poller.toml` together with `binding.json`, as one pair. Nothing carries over from
the dry run, so it can never be stale; if a provisioning step fails nothing is published and
running step 5 again simply retries it.

## How work starts

Two ways, and neither needs anything else done by hand — the engine adds the board card itself.
It does not touch the issue's labels: the ownership label (`agent` or `claude`, as
`ownership_label` lists) is something you put on, never something
the poller writes back, so an issue wearing one is always an issue somebody handed over.

**Label the issue.** Each wake lists open issues carrying an ownership label, seeds any it does
not know, and the ownership ledger decides whether a label is a fresh start or part of the world
being adopted. A card started this way carries no instruction, so the issue *is* the brief: the
worker is told to read it in full, comments included.

**Or comment an instruction on the issue or its linked pull request:**

    /claude <what you want done>

Use this when the work is narrower than the issue, or when you want to steer a card that is
already running. Pull-request conversation commands route to the PR's one native closing issue;
a PR that closes zero or several project issues remains its own command target only on a repository
that also holds poller issues, rather than steering all of them or inventing a code-only card. An
instruction always wins over the issue body.

Two rules worth knowing. The label is authorized by *whoever applied it* — a drive-by issue
cannot summon a worker unless someone holding Work authority labels it. And a rebuilt state
adopts the standing world rather than re-running it: on an uninitialized ownership ledger nothing
counts as a start, so losing `state.json` does not restart every labelled issue on the board.

The configured token ceilings hold automatic extension proposals and recovery spawns. A label or
command accepted from someone with Work authority is their explicit decision to spend the tokens,
so that exact dispatch (including its bounded retries) may start over budget. Worker capacity and
all other admission rules still apply.

An ownership label may also be an ordered list. Any listed label hands the card over; the first is
the canonical label used when the engine writes ownership; provisioning creates every configured
label. This lets a project add the fleet-standard `claude` label without abandoning cards that
still carry an older name.

**Or label it `spec-only`.** The label named by `board.spec_only_label` starts a worker that may
write only an implementation specification as an issue comment. It then moves the card to Needs
More Info. A person approves by removing the label and starting the card again; worker-control
blocks pull-request registration and implementation outcomes while the label remains.

## How work ships

An ordinary card stops at **In review** and waits for a person:

    /merge review     approve after a review worker passes it
    /merge            release finished work now

Both need Release authority, because passing a review auto-progresses into the real merge.

## An engine change needs no re-activation

The engine deploys with AgentDeck (#2153), and an activation approves this project's
configuration, not one engine build: its fingerprint covers the contract, the Manifest and the
resolved binding only. A binding activated while the engine still shipped as its own artifact is
migrated on its next wake, keeping the old record beside the new one so a rollback still loads
it. A changed Manifest or binding still reads as drift and needs step 5 again.

## What lives in the instance directory

Everything a running instance owns is under `~/.local/share/agentdeck/poller/instances/<id>/`,
created by step 1 and never shared with another instance:

- `poller.toml` — this instance's copy of the Manifest, the one a wake actually reads.
- `overlay.toml` — the machine-local Host Overlay: credentials, the agent account, and paths.
- `binding.json` — the GitHub node ids resolved at activation; engine-written, never
  hand-edited. It records nothing about extension code: what an extension may do is its Manifest
  `hooks` list (#2158).
- `state.json` — the durable engine state, with its last-valid `.bak` and its transaction `.lock`
  beside it.
- `activation.json` — the audit record of the last apply: who applied it, when, and which config
  and binding fingerprints. Audit only; the wake reads `binding.json`.
- `poller.toml`, `binding.json`, `activation.json` — once applied, links through `current` into
  `generations/<id>/`. An apply writes a whole new generation and switches `current` in one
  rename (#2157). Change the Manifest by applying it, not by editing the link's target.
- `dispatch-acks/` — one receipt per delivery, keyed by dispatch id, so an uncertain send can be
  awaited instead of re-sent.
- `turn-watch/`, `turn-ends/` — gone (#2200). How a Run ended, and whether a stop was carried
  out, is read from the Runtime's `GET /executions/outcome`; nothing reads these directories, and
  an old instance's can be deleted.
- `canary-evidence.json` — written by `scripts/poller_canary.py` and the acceptance artifact for
  #115: it records what each capability was judged on, and a step with no positive evidence is
  recorded as `unknown` rather than as a pass.

## Two prerequisites this host does not have yet

1. **A Project board, and its number in `poller.toml`.** `project_number = 0` is a deliberate
   placeholder: the offline check refuses it rather than letting a poller point at a board nobody
   chose. Creating the board and completing its Status column is one command:

   ```sh
   python3 scripts/create_poller_board.py --owner eerovil --title "AgentDeck Poller"
   ```

   It is idempotent — an existing board is reused, existing columns are preserved exactly as they
   are spelled, and only genuinely missing ones are added — and it prints the number to paste in.
2. **A credential that can *create* a board.** Reading one is already covered: the PAT in this
   repository's `.env` reads Projects fine, and the discovery query has been verified against a
   real board with it. Creating a user-owned board is the gap — GitHub allows `createProjectV2`
   only for a **classic** PAT with the `project` scope, so both PATs on this host are refused
   (`Resource not accessible by personal access token`). Any one of these closes it:

   - `gh auth refresh -s project,read:project` on the host login, then run the script; or
   - a classic PAT with `project`, exported as `GH_TOKEN` for the one command; or
   - click **New project** at `github.com/eerovil?tab=projects` and let the script fill in the
     columns afterwards (it only adds what is missing).

Until a board exists, steps 1–3 work and step 4 stops at discovery with a problem naming the
board — which is the fail-closed behavior working, not a bug.

## Running one wake by hand

Two commands, and the difference between them is whether anything happens:

```sh
# the engine only — it prints the directives it would act on, one JSON object per line
scripts/poller_engine.py cycle \
  --manifest poller/reference/poller.toml \
  --overlay ~/.local/share/agentdeck/poller/instances/agentdeck/overlay.toml \
  --binding ~/.local/share/agentdeck/poller/instances/agentdeck/binding.json

# the full wake, exactly what the systemd timer runs
python3 scripts/poller_wake.py --instance agentdeck
```

The first answers "what would this wake do?" without a worker appearing: the directives go to
stdout instead of to the dispatcher. The second runs the same engine and then acts on what it
decided — preparing workspaces and delivering.

### Workspace ownership

Workspace preparation is a project-adapter seam in `poller_adapter.runner`. This change proposes
that the standard wake map each code repository's own `[checkouts]` entry to one
`.worktrees/issue-N` worktree. An issue-only repo may instead declare `workspace = "code-repo"` in
its Manifest entry; its worktree and default branch gain the issue repo id as a suffix, keeping
equal issue numbers distinct inside the borrowed checkout. Its brief also names the borrowed
repository, because that is where the pull request lands and a bare `#n` in that repository's PR
body would name a different card entirely. Undeclared checkout aliases fail the offline
inspection, as does an issue repository with neither code nor a workspace — the state that used
to leave every one of its cards adopted and then skipped forever. Upgrading an existing instance
therefore means declaring `workspace` before the new engine reaches it, or `inspect` refuses the
cutover. A project launcher with different topology may still supply an adapter that
chooses a shared starting cwd and zero disposable worktrees, or prepares several worktrees across
repositories.

Every prepared worktree is registered to the card's active generation before delivery and copied
into the dispatch receipt for compatibility. The card's own workspace belongs to its Work
(#2149), so this pull request proposes that the brief carries the exact `register-worktree` command
only where a worker may make a worktree by hand: in another code repository the project owns
(#2155). That command validates the path,
refuses a stale generation, writes engine state under the instance lock immediately, and makes
the worktree the Work's claim through the runtime (#2195). Nothing card-level deletes a worktree:
a finished card hands its paths to the Work's release, which removes only a clean worktree Git
still registers under a checkout's `.worktrees`.

- Each wake appends the engine's own log and then the dispatcher's summary to
  `instances/<id>/cycle.log`. That log is for diagnosis only: every wake also writes a structured
  health record to durable state, and that record — not the log — is the observable surface (#107).
- `log-derived` in a canary step's detail means that run had to fall back to the log: the capability
  did happen, but the card that carried it has since been pruned, so a `cycle.log` line is the only
  surviving proof rather than a durable record. Still a pass — just one read from the convenience
  channel instead of from the observable surface.
- The timer is deliberately not persistent, so a wake missed while the host was down is never
  replayed: a backlog dispatched at once would act on a stale world and stampede the worker cap.
  Recovery is the next ordinary wake's job.

## How a worker talks back

The dispatcher gives every worker the exact `scripts/poller_engine.py worker-control` command for its
Poller Instance, including the activated Manifest, Overlay and binding paths. This pull request
proposes that the brief names only the two lifecycle operations, `finish` and `review` (#2155):
each is one a stale Run could corrupt Poller state with, and each proves its generation atomically
when it runs, so no separate lease check comes first. There is no stop acknowledgement: a stopped
generation is refused and told to stop, and the persistent Runtime ends that Run's process itself
(#2159).

- `enabled` remains as an unadvertised diagnostic. It checks the durable generation and the live
  open/owned card while holding the instance transaction lock, through the same bounded snapshot
  batch a wake uses; an unreadable or deferred card is disabled, never guessed safe. It is not a
  fence: the answer can be stale the moment it returns.
- `comment` and `links` are retired (#2155) and kept only for a worker dispatched before that,
  until #2150. The generation fences Poller lifecycle — `finish` and `review` — and nothing
  else (#2152). An ordinary comment or a follow-up issue goes
  out with normal `gh` under whatever permissions the worker's credentials already carry, and a
  worker refused as `stale:` or `disabled:` stops code, workspace and lifecycle changes but may
  still post a documentary comment saying what it learned.
- `finish` renders the terminal marker first, then rechecks the live card before moving the board.
  If the card was closed or released in that window, the marker remains as evidence but the stale
  worker gets no status write.
- `review` accepts only the active merge-review generation and records `pass` or
  `changes-needed` for the ordinary release cycle to verify and consume.

Every operation uses the same `StateStore.transaction()` discipline as a wake. The API takes an
`EnginePort`, so it is provider-neutral and can be tested without starting a worker process; the
CLI supplies the activated GitHub adapter in production.

## Why the Reference Project runs the full preset

It exists to demonstrate every capability, so work control, guarded release, merge review,
deployment observation and artifacts are all on (#113). `sentry-autofix` is the one extension left
out: it needs a Sentry organization and token this project does not have. The extension is shipped
and contract-tested regardless — enabling it is a per-project choice.

## What deploy-watch watches here

AgentDeck's own web deploy hot-reloads its workers and then records the commit it deployed, so
`deploy-watch` reports a merge as deployed only once that recorded commit contains the merge and
`/healthz` answers healthy. The Overlay names the record and the checkout to ask
(`deployed_commit_file`, `repo_path`); without them the gate falls back to `agentdeck.service`
having restarted *after* the release started waiting, which a hot reload never does (#2308).
Anything less stays `pending`, so a release never finishes on a deploy nobody observed.
