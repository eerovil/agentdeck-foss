---
name: review-fix-pr
description: End-to-end ship workflow — review the current branch's changes (or a PR#) under the rate-my-pr evidence contract, apply fixes for the findings, then open or update the pull request. Use when the user wants to "review, fix and PR", "ship this branch", or an AgentDeck poller review brief names this skill. Provider-agnostic skill; this is the Codex flavour.
metadata:
  short-description: Review the diff, fix the findings, open/update the PR
---

# review-fix-pr (Codex flavour)

Follow `PROCEDURE.md` in this skill folder. It defines the arguments, the four
phases (scope → review → fix → PR), and what the final report contains.

The review phase runs the installed sibling `rate-my-pr` procedure
(`../rate-my-pr/REVIEW.md`; in the tracked source, `../../rate-my-pr/REVIEW.md`).
Realize its `SPAWN_SUBREVIEWER(prompt, tier)` exactly as the sibling
`../rate-my-pr/SKILL.md` (Codex flavour) defines it — AgentDeck-owned child Codex
chats over the delegation bridge, with the `/healthz` preflight and the
tier-to-model env overrides. The model pins and the bridge mechanics live in that
adapter, not here; if they drift, fix them there once. If the bridge preflight
fails, run the review lanes inline yourself under the same evidence rules rather
than forking unowned `codex exec` turns.

Unlike `rate-my-pr`, this skill **does** edit: the fix phase applies changes in
this chat. The review lanes themselves stay read-only. Commit-trailer and
PR-body conventions come from the repository's root guide.
