---
name: rate-my-pr
description: Combined review of the current diff (or a PR#) — a correctness bug scan plus an architect pass that judges whether the change was a good idea at all (right problem/approach/place, scope, UX, over-engineering) and proposes a concrete alternative when it wasn't. Use for "rate my PR", "was this change a good idea", "architect review my diff". Provider-agnostic skill; this is the Codex flavour.
metadata:
  short-description: Correctness + architect review of the current diff/PR
---

# rate-my-pr (Codex flavour)

Review the current change for correctness **and** for whether it was a good idea
in the first place. Read-only: never edits code, never merges, never posts unless
the user explicitly asks.

## The one provider primitive

The shared procedure calls `SPAWN_SUBREVIEWER(prompt, tier) -> findings`. Codex
has no native subagent tool, so realize it with **AgentDeck-owned child Codex
chats** over the delegation bridge — the same bridge the `use-codex` skill uses.
This keeps every sub-reviewer visible and steerable in AgentDeck.

Preflight once: `curl --fail --silent http://127.0.0.1:8756/healthz`. If it
fails, tell the user AgentDeck is unavailable and stop (do not fork raw
`codex exec` — those turns are unowned).

**Tier → model** (this is the per-lane pinning — edit here if model slugs drift;
override at runtime with the env vars shown):

- `cheap`  → `${RATE_MY_PR_CODEX_CHEAP_MODEL:-gpt-5.6-luna}` (correctness lane).
- `strong` → `${RATE_MY_PR_CODEX_STRONG_MODEL:-gpt-5.6-sol}` (architecture lane).

`SPAWN_SUBREVIEWER(prompt, tier)` =

```bash
uv run --directory ~/agentdeck agentdeck delegate \
  --sandbox workspace-write \
  --parent-session-token <this chat's AgentDeck session token> \
  --model <tier's model> \
  --cwd <repo-under-review> <<'EOF'
<the complete sub-reviewer brief>
EOF
```

- `--parent-session-token` is not optional. It is the token from your own AgentDeck
  conventions block, and it is the only way a Codex chat can name itself as the
  parent. Without it each sub-reviewer becomes its own top-level AgentDeck session
  with its own handoff for the operator to answer.
- Fan out a group in parallel by starting several delegate commands in the
  **background** and polling their output; each prints only its final answer to
  stdout (lifecycle on stderr). That final answer **is** that lane's findings.
- Delegations run `workspace-write`, but this skill is read-only — the brief must
  instruct each sub-reviewer to only read and report, never edit.
- If a delegation reports `waiting` (a question/approval), relay it to the user;
  it is awaiting action in AgentDeck. Keep polling the same command afterward.

## Procedure

Read `REVIEW.md` in this skill folder and follow it verbatim. It defines the
arguments (`[effort] [PR#]`), what effort scales (the correctness fan-out only —
the architecture pass always runs), the correctness and architecture lanes, the
verdict, and the exact output format.

Invocation examples:
- `rate-my-pr` — correctness and architecture review of the working diff, high effort.
- `rate-my-pr high` — a wider correctness scan on the working diff.
- `rate-my-pr max 2646` — full review of PR #2646.
