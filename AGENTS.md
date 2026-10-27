# AgentDeck guide for agents and contributors

AgentDeck is a self-hosted, mobile-first FastAPI dashboard for watching and steering Claude
Code and Codex CLI sessions on one machine. It reads each provider's transcripts into one
session model, renders a server-driven HTMX/SSE UI, and keeps the agent processes it starts
alive across web restarts. `CLAUDE.md` imports this file, so Claude and Codex read the same
guide.

**The dashboard has no login.** Bind it to `127.0.0.1` or a private network you trust: anyone
who can reach it can run commands through the agents it controls.

## Where things are

| Area | Paths |
| --- | --- |
| Web process, state, events, database | `src/agentdeck/{app,collector,state,events,db}.py` |
| Providers and the session model | `src/agentdeck/providers/`, `src/agentdeck/models.py` |
| The persistent runtime that owns agent processes | `src/agentdeck/runtime.py`, the provider worker and app-server clients |
| Pages, fragments and the live stream | `src/agentdeck/web/`, its `templates/` and `static/` |
| Works (an issue, its chats and pull requests) | `src/agentdeck/work_*.py`, `src/agentdeck/web/work_view.py` |
| The poller that starts Works from GitHub issues | `src/agentdeck/poller_engine/`, `poller/reference/` (the example project), `poller/extensions/` |
| The queue for heavy commands such as a whole test suite | `src/agentdeck/jobs.py`, `agentdeck job` |
| GitHub identities | `src/agentdeck/github_identity.py`, `src/agentdeck/poller_engine/github_authority.py`, `docs/github-setup.md` |

Setting it up: `README.md`, then `docs/github-setup.md` to connect GitHub and
`docs/self-hosting-tips.md` for the machine around it.

## Rules that keep it working

- Treat the real provider files as the truth. Claude Code's and Codex's transcript formats are
  undocumented and change between versions: a parser skips and counts a shape it does not
  know, and never crashes a scan.
- Decide what the UI offers from `Session.capabilities`, not from which provider a session is.
- Keep the web process restartable without interrupting agents: a web deploy must not stop a
  running turn, a queued message or a pending question.
- A message sent to an agent is delivered at most once. Never retry an uncertain write in a way
  that could send it twice.
- A background update (an SSE swap, a poll, the service worker) must keep what the person was
  doing: the text they were typing, what they selected, their scroll position.
- On a phone every line of height counts: a new control does not make an element taller unless
  it starts collapsed.

## Tests

```sh
uv sync
uv run pytest -q -m "not browser"     # the whole suite without the browser tests
uv run pytest -q tests/test_jobs.py   # one file while you work
```

Browser tests (`-m browser`) need Playwright's Chromium (`uv run playwright install chromium`).
A whole suite takes every core for a few minutes, so queue it rather than run it beside
another: `~/agentdeck/.venv/bin/agentdeck job submit --cwd <dir> -- pytest -q` answers at once
with a job id, `~/agentdeck/.venv/bin/agentdeck job list|status|logs|cancel` read the queue, and
`~/agentdeck/.venv/bin/agentdeck job list` shows what runs now. Spell the CLI by its path every
time: an agent's shell must not assume a bare `agentdeck` is on PATH, and an unknown subcommand
starts the web server. Test data is made up: never copy a real transcript, token or account
file into `tests/`.

## This repository

This is the public copy of the author's own repository, exported from it one commit at a time
("Sync from private …"). Features that fit only the author's setup are left out or replaced by
a small stand-in that reports them off. Issues and pull requests are welcome here; a change is
applied to the private repository by hand, with its author kept, and arrives in the next sync.
