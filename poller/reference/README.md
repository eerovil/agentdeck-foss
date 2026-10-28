# The AgentDeck Reference Poller Project

> **Cards are gone (#3732).** The poller holds no cards and runs no workers: the deck runs every
> Work (ADR 0008). A wake is the project's housekeeping — extensions and the issues they propose,
> the deck's bug reports, scheduled tasks and health. The card-era sections of this page (work
> starting, shipping, workspaces and `worker-control`) went in #3741.

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

Label an issue with the ownership label (`agent` or `claude`, as `ownership_label` lists) and the
deck starts its Work; comment `/claude <what you want done>` on it to steer that Work. The poller
plays no part in either: see the Work-first dashboard in
[web UI live updates](../../.claude/codebase/web-ui-live-updates.md). What the wake still opens —
an extension's proposal, a scheduled task's run, a bug report — it opens already labelled, so the
deck starts those Works the same way.

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
- `dispatch-acks/`, `dispatch-deferrals/` — the card era's delivery receipts. Nothing writes them
  since #3732 and activation no longer creates them (#3741).
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
# the engine only — it logs what it did to stderr
scripts/poller_engine.py cycle \
  --manifest poller/reference/poller.toml \
  --overlay ~/.local/share/agentdeck/poller/instances/agentdeck/overlay.toml \
  --binding ~/.local/share/agentdeck/poller/instances/agentdeck/binding.json

# the full wake, exactly what the systemd timer runs
python3 scripts/poller_wake.py --instance agentdeck
```

The second adds what only the host knows: the webhook-quiet hint (#1631).

- Each wake appends the engine's own log to `instances/<id>/cycle.log`. That log is for diagnosis only: every wake also writes a structured
  health record to durable state, and that record — not the log — is the observable surface (#107).
- The timer is deliberately not persistent, so a wake missed while the host was down is never
  replayed. Recovery is the next ordinary wake's job.

## Why the Reference Project runs the full preset

It exists to demonstrate every capability, so work control, guarded release, merge review,
deployment observation and artifacts are all on (#113); the deck reads them for its Works.
`screenshots` is the one extension it enables. `sentry-autofix` is left out: it needs a Sentry
organization and token this project does not have. The extension is shipped and contract-tested
regardless — enabling it is a per-project choice.
