---
name: rate-my-pr
description: Combined review of the current diff (or a PR#) — a correctness bug scan plus an architect pass that judges whether the change was a good idea at all (right problem/approach/place, scope, UX, over-engineering) and proposes a concrete alternative when it wasn't. Use for "/rate-my-pr", "rate my PR", "was this change a good idea", "architect review my diff". Provider-agnostic skill; this is the Cursor flavour.
---

# rate-my-pr (Cursor flavour)

Review the current change for correctness **and** for whether it was a good idea
in the first place. Read-only: never edits code, never merges, never posts unless
the user explicitly asks.

## The one provider primitive

The shared procedure calls `SPAWN_SUBREVIEWER(prompt, tier) -> findings`. Under
Cursor, realize it with the **Task tool** — Cursor's own delegation tool, the one
its subagent picker writes out as `Use the Task tool with subagent_type="<name>"`.

- One `Task` call per sub-reviewer: `subagent_type: "generalPurpose"`, a short
  `description`, and the whole brief as `prompt`. Issue a group's calls together
  to run that group in parallel.
- **Tier → model** (this is the per-lane pinning — edit here if slugs drift):
  - `cheap`  → `composer-2.5-fast` (correctness lane).
  - `strong` → `claude-opus-5-thinking-high` (architecture dimensions).
- `model` is an optional `Task` argument. Omit it and the sub-reviewer inherits
  this chat's model, which silently collapses the tiers — so set it on every call.
- The Task tool accepts a **short allowlist** of subagent models that is not the
  same list as `cursor-agent --list-models`. Probed live 2026-08-07 on
  `cursor-agent 2026.08.04`: `claude-fable-5-thinking-high`,
  `claude-opus-5-thinking-high`, `claude-sonnet-5-thinking-high`,
  `composer-2.5-fast`, `cursor-grok-4.5-high-fast`, `gpt-5.6-sol-medium`,
  `gpt-5.6-terra-medium`. An unlisted slug fails the call with
  `Invalid model selection` **and prints the current allowlist**, which is how to
  re-derive this table when it drifts.
- `subagent_type` is a closed enum too — `generalPurpose`, `cursor-guide`,
  `bugbot`, `security-review`, `best-of-n-runner`, plus any custom subagent
  defined in `.cursor/agents/` or `~/.cursor/agents/`. Both tiers use
  `generalPurpose`. Claude's `Explore` and `general-purpose` name nothing here,
  and the lowercase `explore` is rejected outright.
- A Cursor subagent **can write files**, and this skill must not. There is no
  read-only subagent type to lean on and the shared procedure does not say it, so
  the enforcement is yours: prefix every brief you send with this line, verbatim,
  before the text `REVIEW.md` gives you.

  > You are a read-only reviewer. Do not create, edit, delete, or move any file,
  > do not run any command that writes, commits, pushes, or merges, and do not
  > post anywhere. Read and report only.
- A completed `Task` call returns the child's final reply. That text **is** that
  lane's findings — feed it straight into synthesis.

Do not do the lanes yourself in the main thread when you can fan out; the value
is independent perspectives.

## Verify every file you read here

Cursor's `read` tool has returned empty or partial content while reporting
`status: "success"`, on files as small as 4.5 KB (#767). `REVIEW.md` is ~16 KB.
After reading it, check what you got against `wc -c` / `wc -l` on the same path;
if it is empty, short, or missing its final section, re-read it through the shell
in chunks (`sed -n '1,120p' REVIEW.md`, then `'121,240p'`, …). Never review from a
procedure you did not fully receive.

## Procedure

Read `REVIEW.md` in this skill folder and follow it verbatim. It defines the
arguments (`[effort] [PR#]`), what effort scales (the correctness fan-out only —
the architecture pass always runs), the correctness and architecture lanes, the
verdict, and the exact output format.

Invocation examples:
- `/rate-my-pr` — correctness and architecture review of the working diff, high effort.
- `/rate-my-pr high` — a wider correctness scan on the working diff.
- `/rate-my-pr max 2646` — full review of PR #2646.
