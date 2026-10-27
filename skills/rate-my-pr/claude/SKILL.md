---
name: rate-my-pr
description: Review the current diff (or a PR#) for correctness bugs, plus an architect pass on whether the change was a good idea at all — right problem, approach and place — proposing an alternative when it was not. Use for "/rate-my-pr" or "rate my PR". Claude flavour.
---

# rate-my-pr (Claude flavour)

Review the current change for correctness **and** for whether it was a good idea
in the first place. Read-only: never edits code, never merges, never posts unless
the user explicitly asks.

## The one provider primitive

The shared procedure calls `SPAWN_SUBREVIEWER(prompt, tier) -> findings`. Under
Claude, realize it with the **Agent tool**:

- Spawn each sub-reviewer as its own agent, in parallel where the procedure runs
  a group concurrently (multiple `Agent` calls in one message).
- **Tier → model + agent type** (this is the per-lane pinning — edit here if
  model names drift):
  - `cheap`  → `model: "sonnet"`, `subagent_type: "Explore"` (correctness lane:
    read-only, locates and reads code).
  - `strong` → `model: "opus"`, `subagent_type: "Plan"` (architecture
    dimensions: design/trade-off reasoning).
- Pass the whole sub-reviewer brief as the agent `prompt`, and set `model` to the
  tier's model on every `Agent` call — do not rely on inherited defaults. The
  agent's returned text **is** its findings — feed it straight into synthesis.

Do not do the lanes yourself in the main thread when you can fan out; the value
is independent perspectives.

## Procedure

Read `REVIEW.md` in this skill folder and follow it verbatim. It defines the
arguments (`[effort] [PR#]`), what effort scales (the correctness fan-out only —
the architecture pass always runs), the correctness and architecture lanes, the
verdict, and the exact output format.

Invocation examples:
- `/rate-my-pr` — correctness and architecture review of the working diff, high effort.
- `/rate-my-pr high` — a wider correctness scan on the working diff.
- `/rate-my-pr max 2646` — full review of PR #2646.
