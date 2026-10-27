# review-fix-pr — shared procedure

Take the current branch's work from "code written" to "pull request open": review
the change, apply fixes for what the review found, then push and open (or update)
the PR. Provider-neutral; each flavour's `SKILL.md` says only how the review
phase's sub-reviewers are spawned. This is the skill the poller's `/merge review`
dispatch names, so its review phase carries the same evidence contract as
`rate-my-pr` — a reviewed card the poller releases on its own verdict has the
least human backstop of any change that ships (#787).

Arguments (any combination, all optional):

- An **effort level** (`low` / `medium` / `high` / `max`) — passed to the review.
  Default `high`.
- A **PR number** — review and update that pull request instead of the working
  diff.
- A **PR title** in quotes — used verbatim if a new PR is opened.

## 0. Scope

Establish exactly what is being shipped before reviewing:

```bash
git status --short                       # uncommitted work
git branch --show-current                # current branch
git log --oneline origin/HEAD..HEAD      # commits ahead of base
gh pr view --json url,state,headRefOid 2>/dev/null   # existing PR, if any
```

The change set is committed-ahead-of-base plus uncommitted working-tree changes.
Nothing to review (no diff against base, clean tree) means nothing to ship: stop
and say so.

### What the card asked for

Write down the **ask** before reading the diff: one or two plain sentences
naming what the originating card wanted, and nothing else. A change that quietly
grew past its card is what this exists to catch (#1104), and a scope judgement
formed after reading the diff drifts towards whatever the diff happened to do.

- Find the card. A poller branch is named `agent/issue-<n>`, and a PR body
  normally links its issue; read it with
  `gh issue view <n> --json title,body,comments`.
- Take the ask from the card's own words, not from the branch's commits. The
  diff is evidence of what was built, never of what was asked for.
- No card at all: the ask is what the invoker asked for in as many words. If
  nothing records one, write `Ask: not recorded` and skip the beyond-the-card
  list in step 3 rather than inventing a scope to measure against.

Carry the ask into the review (step 1) and into step 3's report.

**Sync onto the base now, before the review, not after.** `git fetch` and merge
the current base into the branch first; never rewrite or force-push commits already
on the remote, including a merge the poller added (#2675). The review pins one sha and
`REVIEW.md` Step 4.5 invalidates it if the tip moves, so a base update done after the
lanes have run throws the whole pass away and buys a second one. Doing it here
costs nothing (#1002).

### What earlier passes already decided

This branch may have been through here before. Every pass writes a Review note
(step 3) recording what it fixed and what it deliberately skipped and why, so
read that note now, before the lanes run:

- PR mode, or a branch with an open PR: `gh pr view <n> --json body,comments`.
- Poller cards may put the note in the terminal issue comment instead, so read
  the issue thread too: `gh issue view <n> --json comments`.

From it, take two lists — the findings an earlier pass **fixed**, and the ones it
**deliberately skipped**, each with the reason it recorded. Call them the prior
record and carry it into step 1. There is nothing to carry on a first pass; say
so and move on.

The prior record goes to **you**, not into any lane's brief. A lane told what was
already dismissed stops looking there, and the whole value of a fresh pass is
that it does not know what the last one talked itself out of. Step 1 uses the
record after the lanes report, never before.

## 1. Review

Run the **installed sibling `rate-my-pr` skill's procedure** — its `REVIEW.md`,
at the chosen effort, in PR mode when a PR number was given. From this skill's
installed folder that file is `../rate-my-pr/REVIEW.md`; in the tracked source it
is `../../rate-my-pr/REVIEW.md`. Realize `SPAWN_SUBREVIEWER(prompt, tier)`
exactly as this provider's `rate-my-pr` flavour defines it. Hand it the ask you
recorded in step 0 — its *Scope & blast radius* dimension measures the diff
against that ask, and without it the lane measures the diff against itself.

The procedure's *Evidence rules* are binding here, not advisory: a finding ships
only with its cited line quoted verbatim from a tool result, an unreliable lane
returns `LANE UNRELIABLE` rather than reasoning past garbled output, and the
reviewed sha is recorded up front and re-checked before the findings are acted
on (#708). Do not re-implement or paraphrase the review inline when the sibling
procedure is available; if it genuinely is not installed, review the diff
directly under those same evidence rules.

Collect the findings as a numbered list: file:line, one-line description,
severity (bug / cleanup), confidence.

### Reconcile against the prior record

Now — and only now, with the lanes' findings in hand — bring in the prior record
from step 0. Match each finding against it by what the finding is about, not by
file and line: a pass that names the same missing lock in a different function
is raising the same thing, and one that has drifted to a neighbouring line is
not raising a new one.

- **Matches something an earlier pass deliberately skipped** — report it once as
  `still open — skipped in an earlier pass: <the recorded reason>`, and leave it
  skipped unless this pass has something the earlier one did not: new evidence,
  or a fix since made that invalidates the reason. Do not re-argue it from
  scratch. If you do act on it, say what changed.
- **Matches something an earlier pass fixed** — check the current code before
  acting. A lane reading a stale tree, or reading the fix and judging it
  insufficient, both look like this. Quote the line as it stands now; if the fix
  is there and holds, drop the finding and note that it was already fixed.
- **Matches nothing in the record** — it is new. Treat it normally.

This is bookkeeping, not a gate: nothing here suppresses a finding a lane can
evidence. It exists because the sub-threshold layer is where reruns churn — the
same missing locks, races and debug-logged payloads come back in slightly
different clothes every pass, get re-argued at length, and end up skipped again
for the reason already on file (#1148).

**The full review runs once per turn.** Not once per commit — once. Every sha you
push after this one is a fix for what this pass already found, and re-reviewing it
buys another round of the same lanes reading mostly the same diff. Step 2.5 is the
only check the fixes get, and it is deliberately small (#1002).

## 2. Fix

Present the findings, then apply fixes without waiting for confirmation.

- Fix the high-confidence bugs and the clear cleanups. Skip anything uncertain
  rather than guessing, and say which findings were skipped and why.
- Keep each fix minimal and in the style of the surrounding code.
- Run the repository's fast check for the touched area (its root guide names the
  validation tiers). Report failures with their output — never paper over them.
- A gated *Over-engineering* item is a finding like any other. Remove what a
  `Simplify:` item names, along the simpler path it gives. Never act on a
  `Design question:` item: it asks the requester whether to drop something they
  asked for. Record it in the Review note, do not call the head clean, and stop:
  report `needs_more_info` with the question quoted, so the requester answers it.
- No actionable findings: continue to step 3 with the change as-is.

**Work that went beyond the card is not a finding to fix.** Do not delete or
unwind it here. Whether unasked-for work is welcome is the merging human's call,
and ripping it out mid-turn is a bigger unreviewed change than leaving it. Name
it in step 3 instead.

## 2.5. Re-check the fixes — one lane

A fix can be wrong, so the fixes do get checked. What they do not get is the
review again.

Skip this entirely when step 2 changed nothing. Otherwise spawn **exactly one**
lane, whatever the effort level, with the same *Evidence rules*. Which lane
depends on what the fixes did — see *When a fix adds a new entry point* below;
by default it is `SPAWN_SUBREVIEWER(prompt, "cheap")` with this brief:

> Here is a diff of fixes applied in response to a code review that has already
> run: `git diff <sha reviewed in step 1>..HEAD`. Review ONLY these fix lines,
> and only for whether the fix itself is broken — wrong condition, wrong
> variable, missing case the old code handled, a fix that does not actually
> address the finding it names. Do not review anything the fixes did not touch,
> do not re-litigate the original change, and do not raise design or scope
> opinions: those were decided in the pass that produced these fixes. Return
> nothing if the fixes are sound.

That lane's findings are the last ones acted on this turn. Fix what it finds and
go to step 3 — do not run this step a second time on the fix-of-the-fix. If it
returns something you cannot fix confidently in one edit, stop and say so: on a
poller review card that is a `changes-needed` verdict, not a reason for another
review.

### When a fix adds a new entry point

The cheap lane above suits the fix it was written for: a tightened condition, a
corrected variable, a missing case restored. It does not suit a fix that adds
**new code the rest of the system will call on its own**, because "are these
lines a broken fix" is the wrong question to ask of it. The right question is
the one the review asks of any new code, and nothing else in the turn asks it.

So when a step-2 fix **adds a new entry point**, its lane is
`SPAWN_SUBREVIEWER(prompt, "strong")` instead, briefed as below. A new entry
point means the fix introduced something the codebase now enters on its own:

- a management command, script, or CLI subcommand,
- a cron/scheduled job or timer,
- a signal handler, receiver, hook, webhook route, or event subscriber,
- a new caller of an existing export, write, payment, or delete path.

Editing the body of one that already exists is not a new entry point. Adding
one — even a small one, even one whose whole purpose is to close the finding —
is. Its brief:

> A code review has already run on this branch. In response, these fixes were
> applied: `git diff <sha reviewed in step 1>..HEAD`. One of them adds a new
> entry point: <name it, with its file>. Review that new entry point as new
> code, not as a fix — what selects the rows or events it acts on, what it does
> to each, what state it assumes, what happens when it runs twice, runs late, or
> races the path it was added beside, and what it does to a record that is not
> in the state it was written for. Review the remaining fix lines only for
> whether the fix itself is broken. Do not re-litigate the original change and
> do not raise design or scope opinions: those were decided in the pass that
> produced these fixes. Return nothing if it is sound.

This is still **one** lane and still one full review this turn — the cap in
*Notes* is unchanged, and so is the poller's review brief that repeats it. Only the
tier and the question widen, and only for this case.

The case is not hypothetical. On a shop's pull request a first pass fixed a missed
export by adding a cron-driven rescue command that selected on `status="new"` —
which is also where an unpaid order sits, so it would have posted abandoned
card, PayPal and bank-transfer orders as if they were paid. Nothing in that turn
caught it; the next full pass did, and reported it as "a genuine money bug in
the previous pass's fix". A whole review is an expensive way to read one new
command (#1148).

## 3. PR

Never commit or push to the base branch (`master`/`main`). On the base branch,
create a topic branch first (short kebab-case name derived from the work).
When pushing to an existing PR, fetch its current state first — a kanban review
is only valid for the commit it read. If the base moved again while you were
fixing, merge it in and say so in the report; do not re-run the review over it. That
rule is about not acting on a *stale finding*, and every finding you acted on
this turn came from the sha you pinned in step 1.

```bash
git add -A                                # only files this work touched
git commit -m "<concise imperative summary>"
git push -u origin HEAD
gh pr create --base <base> --title "<title>" --body "<body>"   # or push to the existing PR
```

If a PR already exists for the branch, push to it and report its URL instead of
opening a duplicate. By default the PR body carries a short **Summary** and a
**Review** note: which findings were fixed, which were deliberately skipped,
and the reviewed sha. If a poller brief assigns work evidence to the issue thread,
put that Review note in the terminal issue comment instead.

### Source issue closure

A normal issue implementation PR completes its source issue unless the card or
invoker explicitly says the PR is partial or non-closing. Its body must use a
GitHub-supported closing keyword: `Closes #<issue>` when the issue and PR are in
the same repository, or `Closes <owner>/<repo>#<issue>` when they are not. A PR
title, `(#123)`, a bare issue reference, or prose such as `Follow-up on #123`
does not close an issue.

Before creating or rewriting the body, read its current value. Except for the
explicit non-closing case below, preserve an existing closing reference to the
source issue verbatim. Add one when a completion PR lacks it; never weaken or
remove it while adding a Review note, merging the base in, fixing findings, or updating the
summary.

A partial implementation opts out only when the card or invoker says so
explicitly, for example `PR intent: non-closing — <reason>`. In that case do not
add a closing keyword, and remove any existing closing keyword for the source
issue; keep the non-closing reason and an ordinary issue link in the PR body. Do
not infer non-closing intent from `follow-up` or `partial` in a branch, commit,
or PR title. With no card and no recorded ask, preserve existing references but
do not invent completion intent.

A non-closing PR must also carry the line `<!-- agentdeck:release=no -->` in its
body. The prose reason is for people; this marker is what stops a poller card
from picking the PR as its release target and trying to merge it. Say it in prose
*and* in the marker — a card that starts merging a follow-up nobody approved is
the failure this prevents, and reworded prose is not something the engine can
read. Only the marker and draft status hold a PR back, so remove the marker if a
follow-up later becomes the thing that should ship.

A **step** PR is different: it should merge, and the card goes on after it. Use one
only when the rest of the card needs this part merged or live first, or the card or
invoker says to split. Its body carries `PR intent: step — <reason>`, the marker
`<!-- agentdeck:release=step -->`, and `Part of <owner>/<repo>#<issue>` instead of a
closing keyword. The poller reviews, verifies and merges it like any other PR, then
hands the card back for the next part; the card's last PR is a normal completion PR.
Never write both markers on one PR.

The Review note is also what a **later pass reads in step 0**, so write it to be
read by one. Give every skipped finding the reason it was skipped, in enough
words to be acted on rather than re-derived — `skipped: pricing-mixin refactor,
won't refactor money code mid-review` tells the next pass what to do; `skipped
(low confidence)` sends it round the same loop. Keep the sub-threshold notes in
the list too: those are the ones that come back. When you pushed to an existing
PR, add this pass's note rather than overwriting the previous one — the record is
the whole history of what has been decided on this branch, not just the last
pass's half of it.
Keep the PR body to the concise fields the brief names. Follow the
repository's own commit-trailer and PR-body conventions (its root guide) over
anything here.

### Beyond the card

The report and the PR body (or the terminal issue comment, when the brief puts
work evidence there) carry one **Beyond the card** line: a short list of
anything in the change the card did not ask for — an extra refactor, a
behaviour change made while passing through, an option nobody requested — each
with the file it lives in and whether it could have shipped separately. Write
`Beyond the card: nothing` when the change stayed inside the ask, and omit the
line entirely when the ask was not recorded.

Take the list from the review's *Scope & blast radius* dimension, which is always
there: the architecture pass runs at every effort level. If that dimension came
back unusable, compare the ask against the diff yourself rather than omitting the
line. Report the line whatever the dimension rated: a
`sound` rating means the extra work looked defensible, not that it was asked
for. Disclosing a deliberate behaviour change in the PR body is not the same as
having been asked for it, so a disclosed one still belongs on this line.

Report the PR URL, the fixed/skipped findings, the Beyond the card line, and the
fast-check result.

## Notes

- Do not run the full CI yourself; a targeted check on the changed area is
  enough. Do not merge anything — whoever invoked this decides what ships.
- One turn spawns the step-1 lanes plus at most one step-2.5 lane. Wanting to be
  sure is not a reason for a second pass: #994 ran four rounds and 14 reviewers
  on a one-line label for $43. Being unsure is a `changes-needed` verdict (#1002).
- The repository's root guide (`AGENTS.md`, else `CLAUDE.md`) wins over this
  procedure wherever they disagree.
