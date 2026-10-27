# Changelog

## v0.3.0 (unreleased)
- **Delegated chats nest under the chat that dispatched them.** `agentdeck
  delegate` takes a new `--parent-session-token`, which the deck resolves to the
  parent's session key — the only self-identity a Codex chat has, since Codex
  threads share one process and cannot carry per-thread environment. Failing
  that, `--parent-session` now also defaults from `$AGENTDECK_SESSION_KEY`, and a
  recorded parent may be a full session key as well as a raw provider id; only
  the raw form resolved before, so a chat that named itself the one way it could
  still recorded a parent that matched nothing. When no parent can be named, or a
  token is bound to no session, the CLI says so on stderr, the server logs it,
  and `POST /api/delegations` echoes the recorded `parent_session` — previously
  both channels failed silently and one 145-way fan-out became 145 Deckhand
  handoffs the operator never dispatched.
- **Deckhand can run on a Claude account.** `[assistant] account_key` now accepts
  a `claude_code` account as well as a `codex` one, and attention triage plus
  Semantic Titles run on whichever CLI that account belongs to — Claude jobs use
  `claude -p --output-format json --json-schema` with no tools, no session
  persistence, and the account's customizations disabled, the same read-only
  shape as the existing `codex exec` job. The model is configured per provider
  (`model` for Codex, new `claude_model` for Claude, empty = account default), so
  switching accounts cannot leave one CLI asking for the other's model. An unset
  `account_key` still resolves to the first Codex account.
- Added a **Codex CLI provider**. Local
  `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` chats now share the session
  cards, transcript detail view, live tail, model metadata, and token/context
  counters used by Claude Code. LIVE/IDLE is inferred from recent rollout
  writes because Codex exposes no process registry. Account usage bars are read
  through `codex app-server`: windows are matched by duration, including plans
  that currently return only a weekly limit.
- Added opt-in injection for completed non-interactive Codex `exec` sessions.
  The provider checks the durable `task_complete` turn boundary at discovery
  and again immediately before `codex exec resume`; active/incomplete and TUI
  sessions fail closed and remain read-only. Prompts travel over stdin,
  agentdeck serializes follow-ups in a visible
  FIFO queue, Enter submits (Shift+Enter adds a newline), and the existing
  transcript SSE renders replies. The dashboard can also start new persisted
  Codex `exec` chats in a selected working directory. `[inject]` defaults to
  disabled.
- Added a machine-oriented delegation API and `agentdeck delegate` client.
  Local callers such as Claude Code can now submit prompts over stdin to an
  AgentDeck-owned persistent Codex app-server thread, wait for a structured
  completion, and receive the final assistant message. Delegated runs expose
  live steering, Stop, questions, and approval requests in AgentDeck. The
  bridge permits only read-only or workspace-write sandboxes and always routes
  approval-requiring actions through AgentDeck.
- AgentDeck-owned Codex chats now support `/compact` from the normal composer.
  Compaction uses Codex's native app-server operation, waits behind active work,
  and completes before later FIFO messages are sent.
- **Post-`/compact` cards no longer misreport.** A completed compaction (and
  other slash-command echoes) is bookkeeping, not a live turn, so cards used to
  read the *pre*-compact tail: a stale huge context size, a false "working"
  badge, and an old prompt/reply. Compaction is now a turn boundary — the
  context counter hides until the next real turn reveals the new (smaller) size,
  the open-turn probe reads idle, and the stale prompt/reply are dropped
  (`ai-title` still labels the card). Command/compaction lines are also filtered
  from the transcript view.
- Session cards show a **context-size** counter (`47k ctx`) in the meta row —
  how full the context window is now, taken from the input side of the latest
  usage block (input + cache-read + cache-create), not the cumulative token
  total. Read cheaply from the transcript tail (mtime-cached) and refreshed on
  the liveness sweep, so it ticks up live as a session works. Colour-coded like
  the usage bars — green under 500k, amber past the halfway mark of the 1M-token
  window, red in the near-full (800k+) auto-compaction zone.
- Cards recognise the **AskUserQuestion** (multiple-choice) tool: its prompt —
  which lives in the tool input, not a text block — is surfaced as the card's
  waiting-on-your-answer question, and an unanswered one reads as *waiting*
  rather than "Using tools". The transcript detail view renders the prompt too
  (as its own amber question block) instead of dropping the line as tool noise.
- Sessions show a pulsing **thinking** indicator (dot + label) in both the list
  and the detail header when the agent is actively streaming — a LIVE session
  whose transcript was written in the last ~25s — vs. live-but-waiting. Refreshed
  on the ~10s liveness sweep. On the session detail page the header's thinking
  state is driven directly off the live tail (server-pushed `status` events), so
  it reacts within ~1.5s instead of waiting for the sweep.
- Session cards now show a real title from the transcript's `ai-title` line
  (falling back to the first user prompt), plus a latest-prompt line — instead
  of a bare session id. Titles are mtime-cached so idle transcripts aren't
  re-parsed each scan.
- Transcript viewer collapses long tool-result output into a `<details>`
  accordion (one-line peek until expanded).


## v0.2.0 (unreleased)

- Session detail page (`/sessions/{key}`): transcript viewer with per-event
  role/tool/model rendering, subagent events inlined, and "load earlier"
  pagination for long transcripts.
- Live tail: per-session SSE stream (`/events/sessions/{key}`) that appends new
  transcript events from a byte cursor (idle sessions cost ~nothing) and pushes
  a status fragment when a session flips LIVE↔IDLE.
- Token totals (input/output/cache) summed from transcript `usage` blocks;
  last model + todos (`tasks/<sessionId>/`) shown on the detail page.
- Optional SQLite history (`[history] enabled`): usage snapshots + a
  sessions-seen ledger; a unicode sparkline of recent 5h usage in the header.

## v0.1.0 (unreleased)

- Read-only dashboard: limit bars (5h / 7d OAuth usage) + live/idle session list
  for any number of Claude Code config dirs ("accounts").
- Session discovery from `sessions/<pid>.json` registry files with `/proc`
  starttime pid-reuse guard; titles/last prompts from `history.jsonl`.
- OAuth usage poller with jitter, 429/5xx exponential backoff, 401 credential
  re-read, and an atomically-written shared usage cache under
  `$XDG_RUNTIME_DIR/agentdeck` for other tools to consume.
- HTMX + SSE web UI (`/`, `/healthz`, `/partials/*`, `/events`), mobile-first,
  sticky limit-bar header, stale-greying.
- Zero write paths: no message injection, no transcript parsing yet.
