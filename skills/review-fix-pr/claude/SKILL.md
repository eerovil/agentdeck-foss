---
name: review-fix-pr
description: Review this branch's changes (or a PR#) under the rate-my-pr evidence contract, apply fixes for the findings, then open or update the pull request. Use for "/review-fix-pr", "ship this branch", or when an AgentDeck poller review brief names this skill. Claude flavour.
---

# review-fix-pr (Claude flavour)

Follow `PROCEDURE.md` in this skill folder. It defines the arguments, the four
phases (scope → review → fix → PR), and what the final report contains.

The review phase runs the installed sibling `rate-my-pr` procedure
(`../rate-my-pr/REVIEW.md`; in the tracked source, `../../rate-my-pr/REVIEW.md`).
Realize its `SPAWN_SUBREVIEWER(prompt, tier)` exactly as the sibling
`../rate-my-pr/SKILL.md` (Claude flavour) defines it — the Agent tool, `cheap` →
Sonnet/`Explore`, `strong` → Opus/`Plan` — including its evidence rules and
reviewed-sha handling. The model pins live in that adapter, not here; if they
drift, fix them there once.

Unlike `rate-my-pr`, this skill **does** edit: the fix phase applies changes in
the main thread with Edit/Write. The review lanes themselves stay read-only.
Commit-trailer and PR-body conventions come from the repository's root guide.
