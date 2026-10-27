---
name: review-fix-pr
description: End-to-end ship workflow — review the current branch's changes (or a PR#) under the rate-my-pr evidence contract, apply fixes for the findings, then open or update the pull request. Use when the user wants to "review, fix and PR", "ship this branch", invokes /review-fix-pr, or an AgentDeck poller review brief names this skill. Provider-agnostic skill; this is the Cursor flavour.
---

# review-fix-pr (Cursor flavour)

Follow `PROCEDURE.md` in this skill folder. It defines the arguments, the four
phases (scope → review → fix → PR), and what the final report contains.

The review phase runs the installed sibling `rate-my-pr` procedure
(`../rate-my-pr/REVIEW.md`; in the tracked source, `../../rate-my-pr/REVIEW.md`).
Realize its `SPAWN_SUBREVIEWER(prompt, tier)` exactly as the sibling
`../rate-my-pr/SKILL.md` (Cursor flavour) defines it — the Task tool with
`subagent_type: "generalPurpose"`, that adapter's model pins, its `wc -c` read
guard, and its "You are a read-only reviewer." brief prefix on every review
lane. Those pins and guards live in that adapter, not here; if they drift, fix
them there once.

Unlike `rate-my-pr`, this skill **does** edit — but only in the main thread,
during the fix phase. A Cursor subagent can write files, so the read-only brief
prefix on review-lane subagents is what keeps the review honest; never delegate
the fix phase to a subagent.

Commit-trailer and PR-body conventions come from the repository's root guide.
