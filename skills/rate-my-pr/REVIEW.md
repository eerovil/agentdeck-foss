# rate-my-pr — shared review procedure (provider-agnostic)

This file is the single source of truth for **what** the review does. It is
provider-neutral: it never says *how* to spawn a sub-reviewer, nor which model.
The `SKILL.md` that loaded you (Claude or Codex flavour) defines the one
primitive this procedure calls — `SPAWN_SUBREVIEWER(prompt, tier) -> findings` —
and maps each **tier** to a concrete model. `tier` is `cheap` (fast scan) or
`strong` (deep reasoning). Read that primitive's definition in your `SKILL.md`
first, then follow the steps below verbatim.

`rate-my-pr` is a **self-contained combined review**. It produces one report
with two parts:

1. **Correctness** — a bug/regression scan of the change (always runs).
2. **Architecture** — *"was this change a good idea in the first place?"* (also
   always runs).

Architecture used to run only at `high`/`max`, and switching it off is what let a
reviewed pull request accrete. A review that may only report correctness can only
ever return the next edge case, and on the third round that edge case is inside
the patch the second round caused: `eerovil/agentdeck#2005` took twelve rounds and
eight fix commits to answer one question in eight places, every finding correct at
95–99% confidence, finishing at +1223/−8. "These four patches should be one rule"
was never a finding any round was allowed to make. Effort still decides how wide
the correctness fan-out is; it no longer decides whether anybody asks if the change
was a good idea.

It never edits code and never merges. It only reports.

---

## Step 0 — Parse invocation

Arguments (all optional, any order): `[effort] [PR#]`

- **effort** ∈ `low | medium | high | max`. Default `high`. `ultra` is an
  alias for `max` (this skill is local; there is no separate cloud engine).
  Effort scales the **correctness** fan-out only. Both passes run at every level.
  - `low` / `medium` → a narrow correctness scan, few high-confidence findings.
  - `high` / `max` → a wider correctness scan; broader coverage, may surface
    uncertain findings clearly labelled as such.
- **PR#** — a GitHub PR number (or `#123`). If given, review that PR. If
  omitted, review the **working diff** of the current branch.

## Step 1 — Acquire the diff and context

- **PR mode** (`PR#` given):
  - `gh pr view <n> --json title,body,baseRefName,headRefName,headRefOid,state,mergedAt,files,url`
  - `gh pr diff <n>`
- **Working-diff mode** (default):
  - base = merge-base with the repo's default branch
    (`git merge-base HEAD origin/HEAD` or the known default branch).
  - `git diff <base>...HEAD` **plus** uncommitted changes (`git diff` and
    `git diff --staged`). Review the union.
  - Read the branch's commit subjects for intent (`git log <base>..HEAD --oneline`).
- **Record the ask** — what the originating card asked for, in one or two
  sentences taken from the card's own words. In PR mode read the issue the PR
  body links (`gh issue view <n> --json title,body,comments`); otherwise use the
  branch's issue (`agent/issue-<n>`) or, failing that, the request the invoker
  made. Do this **before** reading the diff: an ask reconstructed afterwards
  drifts towards whatever the diff turned out to do, which is precisely the
  thing *Scope & blast radius* has to measure against (#1104). If nothing
  records one, note `Ask: not recorded` and say so in the report.
- **Record the size context** — one line from the per-file line counts the file
  list already carries (`files` above; `git diff --numstat <base>...HEAD` in
  working-diff mode): additions/deletions for **source**, **tests**, **docs** and
  **generated**, the number of files, and the three largest source additions with
  new files marked. Tests are `tests/**` and `test_*` files; docs are `*.md`,
  `docs/**` and `.claude/**`; generated is lock files and whatever the repository
  marks as generated; everything else is source. It is context for the
  *Over-engineering* dimension and decides whether its deep check runs. **Line
  counts are never the finding.**
- Locate governing context: the root `CLAUDE.md`/`AGENTS.md`, and any
  `CLAUDE.md`/`AGENTS.md` in the directories the diff touches. Note the file
  paths; sub-reviewers read them.

If the diff is empty, say so and stop.

### Record what you are reviewing

A review is about one exact revision, and the branch under it keeps moving —
authors push follow-up `fix:` commits in response to review, and PRs merge while
a lane is still reading. So pin the revision now:

- **Reviewed sha** — PR mode: `headRefOid` from the `gh pr view` above.
  Working-diff mode: `git rev-parse HEAD`, and note whether the worktree is dirty
  (`git status --porcelain`), since uncommitted changes belong to no sha.
- **State** — in PR mode, if `state` is not `OPEN` (or `mergedAt` is set), the
  change has already landed. Review it if asked, but say so in the report's first
  line: these findings are history, not a merge decision.

Carry the reviewed sha into every sub-reviewer brief and print it in the report
header. Step 4.5 re-checks it before anything ships.

Run `git diff --stat <base>...<sha>` once yourself, and hand every lane the exact
command that shows the diff: `git -C <checkout> diff <base>...<sha>`. Never
diff a merge-commit head against `<sha>^` or read it with plain `git show`: both
show the base's changes, not the branch's. Tell each lane to read a big diff one
file or slice at a time after `--stat`, since a whole diff in one read is cut off.

## Step 2 — Correctness pass (always)

Spawn correctness sub-reviewers with `SPAWN_SUBREVIEWER(prompt, "cheap")` — the
scan is broad and shallow, so it uses the cheap tier. Scale the count to
effort: `low`→1, `medium`→2, `high`→3, `max`→4. Number them `correctness 1..N` and
keep that name — it is how a lane is identified if it comes back unusable. Each
gets the diff, the reviewed sha, the CLAUDE.md/AGENTS.md paths, the three quoted
rules from *Evidence rules* below verbatim, and this brief:

> Review ONLY the changed lines for correctness bugs and regressions. Focus on
> real bugs a senior engineer would block a merge on. Ignore nitpicks, style,
> and anything a linter/typechecker/CI would catch. Ignore pre-existing issues
> on unmodified lines. When you confirm a finding inside a function, do not
> move on: re-read that whole function and check the same region for sibling
> bugs of the standard classes — case folding and normalization, None/empty
> handling, multi-repository or multi-tenant identity, boundaries and
> off-by-one — and report each sibling as its own finding. For each finding
> return: file:line, one-line description,
> why it's a real bug, a confidence 0–100, and the **quoted evidence** the
> evidence rules in this brief require. Return nothing if the change is clean.
> You are reviewing sha <reviewed sha>; cite nothing you have not read at that
> revision. Trace the flag-off or default path through the change too, not only
> the path it was written for.

Gate by evidence first: a finding without its quoted evidence is dropped
whatever its confidence (see *Evidence rules*). Then keep a finding when
**either** its confidence is ≥ 70 **or** two or more correctness sub-reviewers
reported it independently — the same defect in the same region, not merely the
same file. Agreement is the stronger signal: benchmarked on two cards whose
full ground truth was known (`#2095`'s two bugs, `#2079`'s three findings),
every real finding was reported by at least two of three sub-reviewers while
self-rating confidence 38–70, so a ≥70-only gate dropped nearly all of them —
and the one-off extras the agreement rule excludes were exactly the findings
no second reviewer could reproduce. At `low` effort there is one sub-reviewer
and the confidence gate stands alone. Deduplicate across sub-reviewers by
file+line after gating, and label each kept finding with the gate that passed
it (`conf` or `2-of-N`). Record any lane that returned
`LANE UNRELIABLE` by its name — the report header lists them.

## Step 3 — Architecture pass (always runs)

This is the point of the skill: judge whether the change **should exist at all**
and whether it was built the **right way**. Spawn **one sub-reviewer per
dimension** below with `SPAWN_SUBREVIEWER(prompt, "strong")` — design judgement
needs the strong tier. They run independently, and each is named by its dimension.
Give each the diff, the reviewed sha, the ask and branch/PR intent from Step 1, the
CLAUDE.md/AGENTS.md paths, the three quoted rules from *Evidence rules* below
verbatim, and its dimension's question. Each sub-reviewer must read enough of the
surrounding codebase to judge, not just the diff, within a budget of about 12
reads: rate once the seam and the main risk are found. Tell the *Use cases* lane
to trace the flag-off or default path as well as the new one.

A runner that cannot spawn sub-reviewers runs each dimension as a separate pass of
its own, and runs *Over-engineering* after correctness, never blended into a
general scan. Every dimension still has to reach a rating: Step 5's verdict block
has one line per dimension, and a review missing any line **did not finish and is
never clean**.

Dimensions:

1. **Right problem** — Is this solving a real, worthwhile problem? Or is it a
   non-issue, premature optimization, or scope invented by the author?
2. **Right approach** — Is the chosen approach sound, or is there a materially
   simpler/cleaner one? If a better approach exists, it must be sketched
   concretely.
3. **Right place** — Is the change in the correct layer/repo/module? (E.g. in a
   superproject-with-nested-repos, does dev-env wiring belong in the
   orchestration repo vs an app repo; does shared logic sit in the shared layer
   vs duplicated per-consumer.) Name the correct location if wrong.
4. **Scope & blast radius** — Did the change stay inside **the ask** from Step
   1? List everything in the diff the card did not ask for — an extra refactor,
   a behaviour change made while passing through, an option nobody requested —
   naming the file each lives in and whether it could have shipped separately.
   Return that list even when you rate the dimension `sound`; it is reported
   either way. A deliberate behaviour change is not excused by being disclosed
   in the PR body — disclosure is not authorisation — and neither is unasked-for
   work that happens to be good. Rate `questionable` when material unasked-for
   work rides along with the ask, and `reconsider` when the unasked-for work is
   the bulk of the diff or changes behaviour the card never mentioned. When the
   ask was not recorded, judge against the branch/PR's own stated intent and say
   the ask was missing. Then the rest of the blast radius: unnecessary coupling,
   migration/deploy/rollback risk, and whether it fits existing patterns and
   conventions.
5. **Use cases, UX & ease of use** — Does it actually serve the end user's real
   use case? Is it discoverable and easy to use/operate, or does it add friction,
   footguns, or surprising behavior?
6. **Over-engineering** — Is every mechanism the change adds needed by what was
   asked, and is the result as simple as it can be — nothing it replaced left
   standing, nothing done twice, nothing rebuilt that already existed? Narrower
   than *Right approach* (*is the shape sound* vs. *is it heavier than it needs
   to be*). Give this lane the size context from Step 1. It has two parts: the
   reuse check, which always runs, and the deep check, which runs on a trigger.

   **Reuse check — always runs.** Read past the diff: the code the change
   replaces, and the helpers that already answer the questions it asks. Look for:
   - **Superseded code kept alive** — an old mechanism the change replaces but
     does not remove: an endpoint, model or table, page or control, setting, or
     code path that the new code now does instead. Grep for its remaining callers
     and quote them. It is superseded when nothing outside itself and its own
     tests still reaches it, or when every remaining caller could move to the new
     code.
   - **Two ways of doing one thing** — two code paths in the changed area that
     answer the same question, or new logic that repeats an existing helper.
   - **New code where existing code would do** — a new function, component or
     module where an existing one could be called or extended. Quote the
     existing one.

   Each item quotes both sides with `file:line` — the code that is redundant and
   the code that makes it so — and says what removing or merging saves: roughly
   how many lines, and what a maintainer no longer has to keep in step. Old code
   the card asked to keep, or that a migration window named in the card or the
   pull request still needs, is not superseded. This check reads code only. When
   only live data can show the old path is unused — a table that may hold rows,
   a setting someone may have set — classify it `Design question:` and name the
   exact read that settles it ("is that table empty in production?"); do not
   guess the answer either way.

   The case this was written for went about twelve rounds without anyone
   asking. A shop's pull request kept an old image-upload path and its linking
   code beside the newer code that replaced it, over a table production showed
   empty, and matched placeholders in two ways where one rule would do —
   several hundred lines between them (#3176).

   **Deep check triggers** — starting values, to be tuned from the replay corpus in
   `eval/over_engineering.toml`; they decide only whether the deep check runs,
   never the rating:
   - source additions ≥ 400;
   - a new source file ≥ 200 lines;
   - two or more new kinds of stored state (a state/JSON file, table, cache, or a
     history with its own retention);
   - a new parser or general-purpose normaliser;
   - four or more top-level modules changed for one focused behaviour;
   - a clearly new parser, store or framework, even below the line counts.

   With no trigger, skip the deep check, report `not triggered`, and rate on the
   reuse check alone. Otherwise:

   1. **Inventory** every new mechanism, with `file:line`: stored state, a parser,
      a state machine, a scheduler or follow-up protocol, a new module, data
      carried across layers, a retention policy.
   2. **Load-bearing check** — for each, name the requirement or safety rule it
      serves, then follow its output to the code that *decides* something and
      quote where that code reads it. It is **not load-bearing** when its output
      reaches only notes, logs or display text, or when it only rebuilds
      information the system already had when it created it. "The spec mentions
      it" is not enough: a later decision on the card can take away the reason it
      was asked for.
   3. **Simpler path** — for each one that is not load-bearing, sketch the
      replacement built on state the system already has, and quote that state.
   4. **Classify** each one that is not load-bearing, and each reuse item:
      - `Simplify:` — machinery, or code left standing or written twice, that
        nothing the requester asked for depends on. Handed to the implementer to
        remove or merge.
      - `Design question:` — removing it would also remove something the requester
        explicitly asked for that users or operators can see or rely on. Quote that
        request. Its fix is "do not remove; ask the requester", never a deletion.

   Rate `reconsider` when the parts that are not load-bearing are a whole new
   module or at least a quarter of the source additions, and `questionable` when
   they are at least a tenth. Below a tenth — a field written and never read, a
   flag carried in state that an argument could carry — rate `sound` and list
   them as sub-threshold notes: they are cleanups, not a change built heavier
   than its problem, and a large change that is otherwise all load-bearing is
   what this dimension must let through (`#2358` replayed at +710 with 55 such
   lines). Confidence is in the trace, not the feel: "I quoted every
   place this output is read and none of them decides anything" can honestly be
   90; "this feels heavy" stays below 70. Return the trigger (or `not triggered`),
   the number of mechanisms traced and how many are not load-bearing.

   A reuse item rates the dimension at least `questionable` when it would remove
   a whole superseded mechanism, or when the reuse items together would remove or
   merge 20 or more source lines; `reconsider` when they come to a quarter of the
   source additions or more. Smaller ones — one duplicated expression, a helper
   that could be one line shorter — are sub-threshold notes. The same confidence
   rule holds: "I quoted every caller of the old path and the new code covers
   each" can be 90; "this looks like it duplicates something" stays below 70.
   Return the number of reuse items.

   The case this was written for: `eerovil/agentdeck#2386` at `6981decf` was
   reviewed clean at source +1240 with a new 544-line `flaky.py`. Its 72-hour
   known-flaky list only annotated a note, and its pytest command parser only
   rebuilt which tests a job ran — something the queue knew when it created the
   job.

Each dimension sub-reviewer returns:
- a **per-dimension rating**: `sound | questionable | reconsider`,
- a **confidence 0–100** in that rating (how sure it is the concern is real and
  material — not how bad it would be). Do not inflate; a hunch is low confidence,
- 1–3 sentence justification grounded in specific files/lines, with the
  **quoted evidence** *Evidence rules* requires for every concrete claim about
  what the code does, and
- when its rating is `questionable`/`reconsider`, a **concrete alternative**
  (which repo/layer/module, and the rough shape of the better change).

A `questionable`/`reconsider` rating whose justification names a symbol, line, or
behavior it cannot quote is downgraded to `sound` with a note that the evidence
was missing. The lane is told to apply this to itself, and Step 4 applies it again
on what comes back — a lane that broke the rule is exactly the lane that will not
have noticed. Design opinions about the diff as a whole ("this layer does not
belong here") need no quote; claims about what a specific line does always do.

## Step 4 — Synthesize the verdict

First **gate the dimension ratings by evidence**: read each flagged dimension's
justification and check that every concrete claim about a specific line carries
its quote. One that does not becomes `sound` here, whatever the lane rated it,
and is listed as an evidence-less flag. Do this before the confidence gate — an
unquoted claim at confidence 95 is exactly the failure these rules exist for.

Then **gate the surviving ratings by confidence**, mirroring the correctness
pass's ≥ 70 rule (agreement cannot substitute here: each dimension has one
sub-reviewer, so there is no second report to agree with). A
`questionable`/`reconsider` rating only counts toward the
verdict at **confidence ≥ 70**. A flagged dimension below 70 does not tip the
verdict; carry it in the report as a **note worth addressing** (labelled with its
confidence), not as a blocker. This exists so one low-confidence lane cannot sink
an otherwise sound change — the reviewers are each prompted to find fault, so
some will; the verdict must reflect *material, confident* concerns only.

Then combine the **gated** ratings into ONE overall **architecture verdict**.
`Sound` is the default the evidence must overcome — do not downgrade without a
concrete, confident reason:

- **Reconsider** — either **one** gated `reconsider` dimension, **or** **two or
  more** gated `questionable`/`reconsider` dimensions together. The change as
  shaped (wrong problem, wrong approach, wrong place, or materially
  over-engineered) should not ship without rework.
- **Questionable** — exactly **one** gated `questionable` dimension and no gated
  `reconsider`. Worth addressing but not a blocker.
- **Sound** — no gated `questionable`/`reconsider` dimensions. Sub-threshold
  flags may still be listed as notes; they do not change this verdict.

**A gated *Over-engineering* flag is always a finding to act on**, whether it is
`questionable` or `reconsider`: it goes into the handoff like a correctness
finding — each item starting `Simplify:` or `Design question:`, with the simpler
path as its fix and, for a reuse item, both quoted sides and what it saves —
and the review is not clean. "Not a blocker" above never
applies to it; a simplification the review could name and still pass is how
`#2386` shipped its extra machinery. A `Design question:` is not deleted by
whoever acts on the review; it goes to the requester.

The *Scope & blast radius* lane's beyond-the-ask list is **not** subject to
either gate. Report it whenever it is non-empty, including when the dimension
rated `sound` or fell below the confidence gate: `sound` means the extra work
looked defensible, not that anyone asked for it, and whether to accept it is the
reader's decision. Naming it is the review's job (#1104).

When earlier review rounds on this pull request already returned two rounds of narrow
findings on one area, do not report a third single edge case there. Name the shared design
question they point at as a `Design question:` (for example, which companions a live window
keeps across scans), so the author fixes the rule once.

When the verdict is `Questionable` or `Reconsider`, the report **must** include a
concrete **Proposed alternative** (not just "this is wrong"): what to build
instead, where it belongs, and roughly how — synthesized from the dimension
sub-reviewers' alternatives.

## Step 4.5 — Re-check the reviewed sha before reporting

Lanes take minutes; branches move underneath them. Immediately before printing,
read the tip again — `gh pr view <n> --json headRefOid,state,mergedAt` in PR mode
(after `git fetch` if you are reading the branch locally), `git rev-parse HEAD`
in working-diff mode — and compare it with the reviewed sha from Step 1.

Working-diff mode also re-checks the uncommitted half: `git status --porcelain`
and `git diff` again. Edits made while the lanes ran belong to no sha, so they
never show up as an advanced tip, and a fix the author just typed is exactly the
already-fixed finding this step exists to catch. Treat a changed working tree the
same as an advanced tip below, with "working tree" standing in for the new sha.

If nothing moved — same tip, same working tree — report normally.

If the tip advanced, the working tree changed, or the PR merged/closed while the
lanes ran:

- Re-diff the interval: `git diff <reviewed sha>..<new tip> -- <files cited by
  findings>` (`gh pr diff` again if the branch is not local).
- **Drop** any finding whose cited lines were changed by that interval and whose
  bug the change removes — those are already-fixed findings, and reporting them
  sends the reader after a bug that no longer exists.
- **Label** every surviving finding that touches a changed file
  `may be stale — re-verify at <new tip>`.
- Name both revisions in the report header — the reviewed sha and what it is now
  — never just one.

Never silently re-point a finding at the new tip: it was verified against the
reviewed sha, and that is what the report claims.

## Step 5 — Output

Print one Markdown report. Do not edit files. Do not post anywhere unless the
user explicitly asked. Use this shape:

```
## rate-my-pr — <PR #n | working diff of <branch>>  (effort: <level>)
Reviewed <reviewed sha (short)><, now <new sha | working tree changed> — findings re-checked>
<"Already merged/closed — this is history, not a merge decision." when applicable>
<"Lanes excluded for unreliable tool output: <lane names>." when applicable>
<"Ask: not recorded — scope judged against the branch's own stated intent." when applicable>

### Correctness (<N> issues)
1. <file:line> — <description> (<confidence>)
   > <the cited source line, quoted verbatim>
   <why it's a real bug>
...
(or: "No correctness issues found.")

### Architecture verdict: <Sound | Questionable | Reconsider>
<2–4 sentence overall judgement of whether this change was a good idea.>

Architecture:
- Right problem: <sound|questionable|reconsider> (<conf>)< — reason when not sound>
- Right approach: <rating> (<conf>)< — reason>
- Right place: <rating> (<conf>)< — reason>
- Scope & blast radius: <rating> (<conf>)< — reason>
- Use cases / UX: <rating> (<conf>)< — reason>
- Over-engineering: <rating> (<conf>) — <not triggered (source +A−D) | triggered (<which>); <k> mechanisms traced, <j> not load-bearing>; <r> reuse items

Size: <the size context line from Step 1>

<For every flagged dimension (gated or not), one evidence line under the table:
"<Dimension> — <file:line>: `<the quoted source line>`". A flag with no such line
was downgraded in Step 4; say so instead: "<Dimension> — downgraded to sound, no
quoted evidence.">

<When the Scope lane listed anything the card did not ask for, one line whatever
that dimension rated: "Beyond the ask: <item> (<file>, could/could not have
shipped separately); …". Write "Beyond the ask: nothing" when it listed nothing,
and omit the line when the ask was not recorded — say "Ask: not recorded" in the
header instead.>

<When any flagged dimension fell below the ≥ 70 gate, add one line:
"Sub-threshold notes (did not affect the verdict): <dimension> (<conf>) — <one-liner>.">

### Proposed alternative        ← only when verdict ≠ Sound
<what to build instead, where it belongs, rough shape.>
```

(The six-line `Architecture:` block is the review's record that every dimension
ran. It is written in this order and this form on every path that follows this
procedure, including one that posts only a short verdict: a review missing any of
the six lines did not finish, and is never reported clean. The Architecture sections are never omitted. They ran at every effort level
since the accretion fix; a report without them is a report that did not finish.)

## Evidence rules

A confident finding about code that does not exist is worse than no review at
all: someone spends an hour hunting a bug that was never there. Both rules below
come from one observed failure — a lane blocked a PR over a symbol
(`_store_loaded`) that has never appeared anywhere in the repo, after every
command in that session returned host noise instead of its payload. It reasoned
past the noise and wrote down what the code plausibly said.

**The quoted block below is the brief.** Paste these three rules into every
sub-reviewer's prompt verbatim; the surrounding text is for you, the orchestrator,
and is not part of the brief.

> **Quote the line, or drop the finding.** Every finding — and every claim about
> what a specific line does — must include the cited source line(s) copied
> **verbatim from a tool result you actually received** (file read, grep hit, or
> the diff itself), with `file:line`. Copy it; do not retype it from memory and do
> not reconstruct what you expect it to say. If you cannot produce that quote,
> the finding does not ship, no matter how confident it feels. Never name a
> symbol, function, field, or flag you have not seen in tool output — if you
> believe one exists, grep for it first and quote the hit.
>
> **Fail loudly on corrupted tool output.** If a command returns garbled text,
> content unrelated to what you ran, an empty payload where one was expected, or
> output that does not match what you asked for, do not reason past it and do not
> infer what it probably said. Retry once. If it is still bad, return exactly
> `LANE UNRELIABLE: <what you ran, what came back>` and no findings. A lane that
> reports nothing is cheap; a lane that invents findings from noise is expensive.
> The Codex sandbox prints `Loading sysroot` and `dconf-CRITICAL` on stderr for
> every command; that is not corruption. Judge stdout, and when `rg` is missing
> retry once with `grep`, before you call a lane unreliable.
>
> **Return the whole result in your final response.** Only the spawned agent's
> final response is returned to the orchestrator; intermediate output is not
> visible there. Repeat every finding and all supporting evidence in that final
> response so it is complete and self-contained. Never return only "reported
> above", "as discussed above", or another reference to earlier output.

Synthesis enforces the first two evidence rules, and that is your job, not the
lane's: a lane returning
`LANE UNRELIABLE` contributes no findings and no rating, and is named in the
report header so the reader knows the coverage was short. Unquoted findings are
dropped in Step 2 and downgrade a dimension in Steps 3 and 4.

## Notes for every sub-reviewer

- Do not check build/lint/typecheck signal or run the app; assume CI covers it.
- Cite specific files and lines, with the quote the evidence rules require.
  Evidence over assertion, always.
- Distinguish "verified real" from "suspected"; never inflate confidence.
- You are reviewing one revision, named in your brief. Read the code at that
  revision rather than assuming the branch still looks the way the diff does.
