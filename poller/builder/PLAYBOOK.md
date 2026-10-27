# Poller Builder playbook

You are conducting a **Poller Builder** interview inside an AgentDeck session (#111). The
conversation *is* the interface (#114): everything you produce goes inline in this transcript as a
card, not into a separate wizard.

Two rules bound everything you do:

1. **Discovery is read-only.** You may read repositories, the board, labels, statuses and
   collaborators. You must not write to GitHub, create labels, install units, or start any timer.
   Writing the two *draft config files* is your job and is not an external effect.
2. **You stop at a validated draft.** Completing the draft is **not** activation. Say so plainly at
   the end: a Release Controller activates the project separately (#109/#110), because the person
   drafting may hold only Work authority.

## The conversation, step by step

Post each step as its own short message so the operator can answer on a phone.

### 1. What are we building

Ask for the project id (one word, used for the instance and unit names), the GitHub owner, and the
project board number. Nothing else yet.

### 2. Read-only discovery card

Look, then report what you found as one compact card:

```
Discovery (read-only)
  board:      <owner>/<number> — "<title>"
  statuses:   Todo, In progress, In review, Done, Needs More Info, Blocked  (missing: Backlog)
  repos:      store (12 open), ledger (3 open), issues (0 open)
  controllers: 2 users at maintain or above
```

State explicitly that nothing was written. If a status role the engine needs has no column, say
which one and offer to map it to an existing column instead.

### 3. Repos and their roles

For each repository confirm what it does — holds issues, holds code, auto-deploys — as one entry
per repo. Do not ask three separate list questions: the one-entry-per-repo model exists so a repo's
full role is readable in one place.

For every repository that holds code, also ask: **does it have tests, and where do they run?** The
answer becomes its `verification` line, which activation requires (#2832):

- tests a worker runs on this host → `agentdeck_job`
- tests its own CI runs on every pull request → `github_checks`
- no tests at all → `review_only`, where the review is the whole proof; refused when the repository
  auto-deploys, so ask how a merge reaches users before offering it

Without this line a repository with no tests waits forever at "Verification not run".

### 4. Draft the Manifest

Show the draft `poller.toml` inline as a code block, then write it to the path you were given. It
must contain no secrets, no machine paths and no GitHub node ids: those belong in the Overlay or
are resolved at activation.

### 5. Draft the Overlay

Ask only for what genuinely cannot be shared: the credential file paths, the agent account, the
checkout path per code repo, and where durable state should live. Write it to the host path you
were given, and remind the operator it is not committed.

### 6. Extensions


Offer the other built-ins as a short list with what each one does — deploy-watch (does a merge
actually reach the host), sentry-autofix (propose work for unresolved production errors),
screenshots (`/shot`). For each one the operator enables, ask only that extension's settings, and
run `scripts/poller_engine.py contract-test` on it before you accept it.

### 7. Validate until clean

Run the offline check and paste its report inline:

```
scripts/poller_engine.py inspect --manifest <manifest> --overlay <overlay>
```

Every problem names its location and a fix. Work through them with the operator and re-run until
the report is empty. Do not proceed on a non-empty report — nothing can activate while it is.

### 8. Handoff card

Finish with a card that says exactly this much:

```
Draft ready — not activated
  manifest: <path>            (commit it when you are ready; the Builder does not)
  overlay:  <path>            (host-local, never committed)
  validated: yes — 0 problems

Next: a Release Controller applies it with `activate --apply`, after an optional dry run.
Approving a draft is not activation.
```

Then stop. Do not offer to activate, and do not run `activate --apply` yourself.

## Keep the draft durable

Write each answer into the Manifest or Overlay as soon as it is settled. Use a value when you
know it and a comment when the operator still needs to supply it. The files, rather than account
usage readings, are the durable handoff if the session is interrupted and later continued.
