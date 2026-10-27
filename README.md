# agentdeck

Self-hosted, mobile-first dashboard for monitoring and steering coding-agent
CLI sessions. It reads local sessions from
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) and Codex CLI
through a provider abstraction over session sources.

## Status & scope

What works today:

- **Usage bars** for per-account 5-hour / 7-day limits plus live host CPU and
  memory, with stale-greying when account-limit polling backs off.
- **Live/idle session list** across any number of Claude Code and Codex config
  dirs ("accounts"), with titles, last prompts, and Claude deep-links where
  available. Codex liveness is inferred from recent rollout writes because the
  CLI has no process-to-session registry.
- **Transcript viewer** (`/sessions/{key}`): per-event role/tool/model
  rendering, token totals, todos, live tail for running sessions, and
  "load earlier" pagination.
- **Codex chat controls** (opt-in): start persisted chats, queue follow-up
  messages on completed non-interactive sessions, and watch replies arrive in
  the existing transcript view — see the safety limits below.
- **Deckhand orchestration assistant** (opt-in): deterministic runtime signals
  and session-owned reports turn the current deck into concise waiting/finished
  handoffs, with optional push notifications. Sessions also name themselves;
  AgentDeck launches no background model jobs.

Roadmap: interactive streaming chat and more agent CLI providers (Gemini).

## Security model

agentdeck has **no authentication**. It is designed to be bound to a
[Tailscale](https://tailscale.com/) (or other private overlay) address and
nothing else:

- Default bind is `127.0.0.1` — an unconfigured install exposes nothing.
- Set `server.bind` to your Tailscale IP to reach it from your phone.
- **Do not bind `0.0.0.0` on an untrusted LAN.** From v0.3 the inject routes
  are effectively remote code execution for anyone who can reach the port.
- All routers pass through a single `require_access` dependency (a no-op
  today) so real auth can be added later without touching routes.
- Credentials (`.credentials.json` OAuth tokens) are read fresh per poll,
  held only in local variables, and never logged or served.

Threat model in one line: anyone who can reach the port is you.

## Install

Requires Python >= 3.12 and [uv](https://docs.astral.sh/uv/). Clone it to `~/agentdeck`:
the systemd units and some instructions the deck gives its agents expect that path. The
public copy is [eerovil/agentdeck-foss](https://github.com/eerovil/agentdeck-foss).

```sh
git clone https://github.com/eerovil/agentdeck-foss.git ~/agentdeck
cd ~/agentdeck
uv sync
mkdir -p ~/.config/agentdeck
cp config.example.toml ~/.config/agentdeck/config.toml
$EDITOR ~/.config/agentdeck/config.toml   # set bind address + accounts
uv run agentdeck
```

Run as a systemd user service (survives logout with lingering enabled):

```sh
mkdir -p ~/.config/systemd/user
ln -s ~/agentdeck/systemd/agentdeck.service ~/agentdeck/systemd/agentdeck.socket \
  ~/agentdeck/systemd/agentdeck-codex.service \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agentdeck.socket agentdeck-codex agentdeck
loginctl enable-linger "$USER"   # keep it running when logged out
```


## Configuration

Config lives at `~/.config/agentdeck/config.toml` (override with the
`AGENTDECK_CONFIG` env var). See [config.example.toml](config.example.toml)
for the annotated reference. Highlights:

- `[server]` — bind address and port (default `127.0.0.1:8756`). This change
  keeps those values for direct `uv run agentdeck` launches; the bundled
  systemd service instead uses `agentdeck.socket`, whose `ListenStream` must be
  changed by a socket-unit drop-in when a different bind or port is required.
- `[polling]` — account usage polling (default 300 s, server-side rate limited),
  session scan/liveness intervals, and host CPU/memory refresh (default 3 s).
- `[usage] shared_cache_dir` — agentdeck publishes each usage snapshot
  atomically to `$XDG_RUNTIME_DIR/agentdeck/usage-<provider>-<label>.json` (with a
  validated label-only compatibility alias; fallback
  `~/.cache/agentdeck`) so other tools can read limits without their own
  polling.
- `[[accounts]]` — one block per agent config dir; for Claude Code, one per
  `CLAUDE_CONFIG_DIR` (e.g. `~/.claude` and `~/.claude2`), and for Codex the
  normal `CODEX_HOME` (usually `~/.codex`).
- `[assistant]` — enables deterministic Deckhand attention tracking. Sessions
  report `waiting` or `finished` through the local API; completed turns that do
  not report safely fall back to `finished`.

The config file contains **no secrets** — credentials always come from the
provider's own store.

### Error reporting

Nothing on the host watches `journalctl`, so by default a new failure on a
running deck is noticed only when somebody looks. Setting `AGENTDECK_SENTRY_DSN`
gives the web supervisor, its worker, and the persistent runtime one shared
destination for unhandled failures, and the dashboard page forwards its own
uncaught errors and rejected promises to the server so they are reported too.

    Environment=AGENTDECK_SENTRY_DSN=https://<key>@<host>/<project>
    Environment=AGENTDECK_SENTRY_ENVIRONMENT=production

The DSN is an environment variable rather than a config key because it is a
write credential, and `config.toml` holds no secrets. Leave it unset and nothing
changes: reporting stays off and failures go to the journal as before. Prompts,
transcripts, request bodies, and local variables are never exported — reports
carry the exception, its stack, and the process it came from, scrubbed for
credential-shaped strings on the way out. Browser failures are written to the
server log whether or not a DSN is set.

## How it works

agentdeck reads undocumented, unversioned internals of the Claude Code CLI
(session registry files, `history.jsonl`, the OAuth usage endpoint) and Codex
CLI (date-partitioned rollout JSONL). These can change without notice in any
CLI release; every parser is written to skip-and-count unknown shapes rather
than crash. Details of the Claude surfaces will be documented in
`docs/claude-code-internals.md` (v0.4).

## Multi-account

Each `[[accounts]]` entry is an independent provider config directory. Usage
data renders for both providers; Codex delegates authentication and rolling
limit reads to `codex app-server`. Sessions are grouped by account. Labels must
be unique and slug-safe (they appear in URLs and cache filenames).

## Message injection & safety interlocks

From v0.3 you can start a persisted Codex chat from the dashboard or send
follow-ups to a completed, non-interactive Codex `exec` session from its detail
page. AgentDeck-created chats run through one persistent Codex app-server per
account, enabling active-turn steering, Stop, structured questions, approvals,
and a FIFO follow-up queue. Enter submits and Shift+Enter inserts a newline.
Existing external `exec` rollouts retain the conservative completed-turn
fallback described below. In AgentDeck-owned chats, send `/compact` as the
entire message to summarize the conversation and free context space; it waits
behind any active turn and preserves the follow-up queue order.

The bundled systemd deployment keeps Codex controls in a separate
`agentdeck-codex.service`. The dashboard talks to it over a Unix socket inside
a mode-0700 `$XDG_RUNTIME_DIR/agentdeck` directory; restarting `agentdeck.service` for a
frontend deploy therefore does not stop active Codex turns or lose pending
approval state. Restart the Codex service itself only after its active turns
have drained.

Local tools can delegate work through the same owned runtime. The prompt is
read from stdin and only the final Codex message is written to stdout:

```sh
uv run --directory ~/agentdeck agentdeck delegate \
  --sandbox workspace-write --cwd /path/to/repo <<'EOF'
Review the current change, fix any issues, and run the tests.
EOF
```

The client polls `POST /api/delegations` / `GET /api/delegations/{id}` and
prints lifecycle and interaction notices to stderr. When Codex needs input, the
question or approval remains answerable in AgentDeck. The delegation bridge
accepts only `read-only` and `workspace-write`; approval-requiring actions use
the interactive AgentDeck review path.

Name the delegating chat so the child nests under it instead of becoming its own
top-level session with its own Deckhand handoff — a fan-out that omits this turns
one dispatched wave into one card per child. An owned Claude turn is covered by
`$AGENTDECK_SESSION_KEY` (then `$CLAUDE_CODE_SESSION_ID`) automatically. A Codex
chat has no per-thread environment, so pass the AgentDeck session token it was
given: `--parent-session-token <token>`, which the deck resolves against its own
binding. `--parent-session <key-or-id>` overrides both when a wrapper delegates on
another chat's behalf. When nothing names a parent, or the token resolves to no
session, the client says so on stderr rather than losing the link silently.

Standalone Codex TUI sessions remain read-only because Codex exposes no
cross-process ownership registry. A safety spike confirmed that a second
resume process can write into a rollout while its original TUI process is still
active. They remain available for monitoring in agentdeck, without an external
open button.

Interlocks, re-checked at spawn time on every attempt:

- **Completed `exec` turns only.** The rollout's last native boundary must be
  `task_complete`; `task_started`, aborted, TUI, and unknown session kinds fail
  closed. This is rechecked immediately before Codex starts.
- **cwd must exist** — a session whose worktree was deleted is refused.
- **One owned turn per session** — agentdeck serializes its own submissions in
  a FIFO queue; only one Codex child writes the rollout at a time. Owned process
  groups are reaped on timeout or shutdown.
- **Messages stay out of argv** — the prompt is delivered on stdin.
- **Kill-switch**: `[inject] enabled = false` hides the form and 403s the route.
  The bundled configuration ships with injection **off** — opt in when ready.

To enable on the live deploy: set `[inject] enabled = true` in
`~/.config/agentdeck/config.toml` and `systemctl --user restart agentdeck`.

## Contributing

Clean-room note: no code, templates, or CSS may be copied from AGPL-licensed
projects (e.g. `claudecodeui`). Test fixtures must be **synthetic only** —
never copy real transcripts, registry files, or credentials into the tree.
A quick hygiene check before committing:

```sh
git grep -I "sk-ant" -- ':!README.md'
```

should return nothing (the string `oauth` appears legitimately in the usage
poller; actual tokens must not).

## Public edition

The public copy at [eerovil/agentdeck-foss](https://github.com/eerovil/agentdeck-foss) is
exported from the author's own repository, one commit per sync. It leaves out the author's
poller projects and notes, the features that drive ChatGPT's website (so a Work merges when
you press Merge), the Mac command-approval tool, the remote test pods, and the helpers that
fit only the author's machine. [docs/self-hosting-tips.md](docs/self-hosting-tips.md)
describes those helpers so you can build your own, and
[docs/github-setup.md](docs/github-setup.md) connects your deck to GitHub. Report problems
and send changes on the public repository; they are carried over by hand.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
