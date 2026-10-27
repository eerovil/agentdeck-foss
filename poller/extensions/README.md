# Poller Extensions (the conventional folder)

An extension is a **separate program**, not an import. The engine runs it at a named hook with
a JSON payload on stdin and reads a JSON object on stdout:

```
$ ./deploy_watch.py release-gate <<< '{"hook":"release-gate","mode":"live","config":{…},"context":{…}}'
{"requests":[{"kind":"release-status","payload":{"repo":"agentdeck","status":"ok"}}]}
```

The engine only ever receives **requests** and runs each one through the same ownership,
authorization, queue, lease and release rules as any other input, so an extension can ask for
work but can never bypass a lease or skip a gate (#112). That includes `open-work`, which asks for
a *card to exist*: the extension supplies a repo, a stable key and a title, and the engine decides
whether to open anything — exactly once, because a duplicate issue is not undoable (#393). A crash, timeout or malformed answer
skips that extension and marks it health-degraded — it never fails the cycle.

An `open-work` payload may also say `"spec_only": true`. The engine then opens the issue with the
project's `spec_only_label` beside the ownership label, in the same write, and the card's first
worker gets the spec-only brief: it writes a plan and nothing is built until a person approves it
(#2874). A project that names no `spec_only_label` refuses such a request rather than opening it
as an ordinary build.

An `observe` hook's `context` carries two things: `cards`, this project's cards with their status
and queue depth, and `settled`, the proposal keys this extension has already had decided — opened,
in the control ledger, or staged. Both are namespaced to the asking extension, and neither exposes
a lease or a dispatch. An extension that bounds how much it proposes per wake **must** subtract
`settled` before spending that bound: its own source usually keeps reporting work that already has
a card, and re-asking spends every slot on proposals the engine drops as already-seen, so nothing
new is ever proposed (#1709).

An `observe` answer may also carry `memory`: one JSON object (at most 64 KiB) that the engine
keeps in its own state and hands back as `context.memory` on the next wake, and to `propose`.
It is kept only from a live wake whose hook answered, so a dry run leaves no trace and a skipped
extension keeps what it had. This is how an extension remembers something between wakes without
a store of its own — `alert_autofix` keeps each firing group's start there, so a group whose
first alert resolves while another still fires keeps its one card (#2691).

## Where the engine looks for one

Two places, in order:

1. **Each of the project's own code checkouts**, at `poller/extensions/<name>.py` — the
   conventional folder, for a program a project deliberately vendors or overrides.
2. **The shared folder the host named**, `overlay.paths.extensions`, holding `<name>.py`
   directly.

Checkouts win, so naming a shared folder can never change what an existing installation
resolves. Without (2) an extension is only findable under a project's own checkout, which works
for a project whose checkout happens to *be* AgentDeck and forces every other project to vendor a
copy of shared code (#377). Wherever it resolves from, a program runs only at the hooks its
project's Manifest allows — a shared program gets no more authority than a vendored one (#2158).

Check a program against the protocol before enabling it:

```
scripts/poller_engine.py contract-test --name deploy-watch --hook release-gate --hook health \
    -- ./poller/extensions/deploy_watch.py
```

## The shipped extensions

`deploy_watch.py`, `sentry_autofix.py` and `screenshots.py` are #113's references.

| Extension | Hooks | What it does |
| --- | --- | --- |
| `deploy_watch.py` | validate-config, release-gate, health | After a merge, reports whether this host's deployed commit contains it (or, unconfigured, whether the service restarted) and the service answers healthy, so the guarded release only finishes on an observed deploy. |
| `sentry_autofix.py` | validate-config, observe, health | Proposes work for unresolved Sentry incidents. Known infrastructure signatures are denied and short same-repository bursts with similar titles become one card. A proposal has a stable identity, so it is deduplicated and subject to every normal rule. |
| `screenshots.py` | validate-config, command, health | Declares `/shot` and turns it into queued work. Taking the screenshots is a **worker-side** procedure (#112): it runs inside the worker session, behind the lease the worker already holds, so it needs no separate boundary. |


## Policy is applied; code is deployed

Changing extension policy/config/hook allow-list requires project apply. Changing implementation
code is a normal software deployment (#2158).

Each enabled extension's Manifest block lists the hooks this project allows it to be called at:

```toml
[extensions.sentry-autofix]
hooks = ["validate-config", "observe", "propose", "health"]
host = "https://us.sentry.io"
```

That list is the extension's whole authority in this project, and the Manifest fingerprint covers
it like any other setting. The engine lifts it out of the block before the program sees its
settings, and the Overlay may not set it — a host can supply a credential, not widen what an
extension may do. At load:

- the program is asked which hooks it implements, only to confirm it still implements every
  allowed one;
- a hook it declares beyond the list is **ignored** — shown as `ignored_hooks` in its health
  record and in the activation dry run, never called — until the Manifest allows it and the
  project is applied;
- an allowed hook it no longer implements is a clear error: activation refuses, and a wake loads
  the extension **refused**.

Nothing records the program's bytes. Fixing a shared extension reaches every project that enables
it on the next wake, and a project's vendored copy is software in the same way; which file
resolved is shown as `source` in the health record. What an extension can *ask for* is still
bounded by the engine's fixed request kinds and core authorization, whatever build is deployed.

An extension that is missing, will not say what it implements, or lacks an allowed hook is not a
silent hole. The wake carries on, the extension answers no hook, its instance reads *degraded* on
the deck, and a refused release gate reports no deploy state at all, so the releases it gates keep
waiting rather than finishing on evidence the gate never gave.

An instance applied before #2158 has a Manifest with no `hooks` lists; until its next apply, the
hook sets recorded in its binding stand in for them, and the code hash beside them is ignored.

## Settings

Each extension validates its own `[extensions.<name>]` block through its `validate-config`
hook, and its complaints land in the loader's one collect-all report (#177). The core loader
knows nothing about any of these settings, which is what lets extensions change without
touching it.

Settings come from **two** files and are merged per key before an extension sees them (#386):

- the **Manifest** enables the extension and states its portable settings — what it watches,
  which repos, how many proposals;
- the **Overlay** supplies what cannot be portable, which in practice means credential paths.

The Overlay may override a value the Manifest set, but it may **not** enable an extension the
Manifest never listed: enabling is the project's decision, and the Manifest is where a reader
looks to see what runs. A host block for an unlisted extension is refused by the offline check.

This is why the manifests under `poller/manifests/` contain no `~/` paths at all. It also means a
token path can be corrected without changing the Manifest's hash — so rotating a credential is not
a re-activation, which it would be if the path lived in the fingerprinted Manifest.

The Manifest half, then:

```toml
[extensions.deploy-watch]
service = "agentdeck.service"
health_url = "http://127.0.0.1:8756/healthz"
grace_seconds = 900

[extensions.sentry-autofix]
host = "https://us.sentry.io"
org = "example-org"
max_proposals = 3
query = "is:unresolved"          # the default search, for projects that don't override it
burst_window_minutes = 15         # similar firstSeen timestamps form one incident
title_similarity = 0.8            # normalized-title similarity from 0 to 1
deny_signatures = [               # case-insensitive title fragments never proposed as work
    "Server shutdown in progress",
]

# One entry per Sentry project, with everything it does stated inline (#371).
[[extensions.sentry-autofix.projects]]
id = "ledger"
repo = "ledger"

[[extensions.sentry-autofix.projects]]
id = "web"
repo = "store"
query = "is:unresolved !level:warning"   # this project's own search, sent to Sentry

[[extensions.sentry-autofix.projects]]
id = "ledger-javascript"
repo = "ledger"
suppressed = true                # never fetched, never proposed; still routable

[extensions.screenshots]
default_routes = ["/"]
```

and the host's half, in that instance's `overlay.toml`, naming only what the Manifest cannot:

```toml
[extensions.sentry-autofix]
token_file = "~/.config/agentdeck/example-sentry-token"
```

### The one that acts outside GitHub

`chatgpt_nudge` is the only shipped extension whose effect is not a request. There is no request
kind for "press a button", the click is not queued work, and its request list is empty every wake
— what it does instead is start a program the operator named in their own overlay, which drives a
browser against this host's ChatGPT login.

That makes two things load-bearing. The hook is killed at 20 seconds and a browser run takes
longer, so the click is launched as its own transient user service and the hook returns at once:
a new process session alone would still be killed with the poller's one-shot service control
group. A nudge still running is not a hook still running. And because nothing upstream bounds it,
`min_interval_minutes` is the whole budget — a branch pushed five times in a minute is one review,
not five.

All of that is the **blind sweep click**, which is the path a project takes when it names a `task`
and no `review_program`. A project that names `review_program` does something else entirely, and
since #2069 it neither presses nor waits: it runs `agentdeck chat-review --no-start` over every
pull request that went ready this wake, which writes one issue per pull request in the chat
repository and starts nothing. Pressing was then the deck's own `chat_auto_nudge` (removed in #2864: `/api/chat-review` now always starts the worker), which held one
fleet-wide ceiling (six starts an hour, six workers in flight) rather than a per-project clock.

The reason for the split is that opening a chat and starting a worker on it cost different
things. A budget written for the expensive half had been applied to both, so a second pull
request going ready inside the window got no chat either — and then had nothing for a worker to
be started onto when the budget came round. `min_interval_minutes` therefore no longer applies to
a scoped review at all, and none of the shipped manifests sets it. What stops a branch pushed
five times becoming five reviews is unchanged and was never the clock: it is the head SHA and the
card's round count, and one open review chat per card.

A launch that carries several pull requests gets one receipt with a row per target, because they
do not share a fate: a run can open two chats and be told the third reference is malformed, and
settling one verdict over all three would either lose two reviews or claim one that never
happened. Each head is settled against its own row.

Detached also means the extension cannot watch the run, so the run answers for itself. Each nudge
is handed a one-off **receipt** path in `CHATGPT_NUDGE_RECEIPT` — a file beside the extension's
own state file — and the changed head SHAs wait in `pending` rather than in `seen` until a later
wake reads a receipt saying the button was pressed. `chatgpt-nudge-run.sh` writes
`{"ok": …, "status": …}` there from the click's exit status, so an expired login, a control that
moved, a task that is not there, a browser that would not start and a lock it waited out all come
back as `ok: false`. So does a click the page accepted without starting anything: `Run now` opens
the task's conversation, so `chatgpt_run_now.py` exits zero only once that navigation has
happened, and clicked-but-never-started is an exit status of its own. A failed run, and a run
that never reports back within twenty minutes, both leave their SHAs unrecorded, so that pull
request is nudged again instead of being counted as reviewed. Three consecutive missing receipts
for one head stop further nudges for that head and make the extension unhealthy; a new head resets
the count. This bounds failures that no shell can report, such as SIGKILL or a dead host. While a
launched run is still unanswered the extension holds off rather than starting a second browser on
the same profile lock. An operator's own `nudge_program` that ignores the variable therefore stops
after the same bounded retries.

The login is established once, by hand, with `scripts/chatgpt_run_now.py --login`. The poller never
authenticates and never sees a password; it only reuses a browser profile that is already logged
in. A click does not touch the task's own schedule — the once-an-hour cap is on the timer, not on
the task (#1816) — so `min_interval_minutes` is a budget a clicking project sets for itself rather
than one ChatGPT enforces. A reviewing project sets none.

### Which deploy gate

Both answer `release-gate`, and a project enables exactly one. `deploy_watch` observes a service on
*this* host — right when the thing being deployed is the host's own service, as it is for AgentDeck.
`drone_deploy_watch` observes a CI promotion — right when the deploy happens somewhere the host
cannot see, as it does for a project deployed to a Kubernetes cluster by CI.

Enabling neither while `capabilities.deployment_observation` is on is the one combination to avoid:
the release then has no source of deploy truth, and waits forever rather than finishing.

### Suppressing noise

A project's `query` is an ordinary Sentry search, so the filtering happens at Sentry and the
noise never crosses the wire. `suppressed = true` goes further: that project is not requested at
all, because asking for issues only to discard them is the expensive way to say no. A suppressed
project keeps its `repo`, so routing stays defined and `validate-config` can still check it — and
it is *named* in the health record rather than merely absent, since a project silently producing
nothing otherwise looks identical to one nobody configured.

`project_to_repo` is gone. It was a routing map shadowed by parallel maps of queries and
exclusions — the shape the Manifest already rejected for repositories, where "what happens to
this project?" becomes a cross-reference exercise and the halves drift apart. The old key is
named and refused by `validate-config` rather than quietly accepted alongside the new one.

`deny_signatures` filters known non-code failures after the fetch and before proposals are formed.
Each entry is a case-insensitive title fragment. This is intentionally separate from a Sentry
`query`: an operator can state a small auditable list even when the upstream search language cannot
reliably express an exception message. Health reports how many fetched issues were denied.

The remaining issues are grouped when they route to the same code repository, their normalized
titles meet `title_similarity` (default `0.8`), and their `firstSeen` timestamps are within
`burst_window_minutes` (default `15`). Quoted details and numbers are normalized before comparison,
so one database outage split into several driver codes still has one shape. Missing or invalid
`firstSeen` values never group: uncertain data keeps separate cards instead of collapsing unrelated
work. The oldest issue, then its short id, selects the group's stable representative; the proposal
records every grouped short id and health reports the collapsed count.

### Ceilings are the engine's, not the extension's

An extension bounds what it *asks* for — `max_proposals` per wake. What is *honored* belongs to
the project, in `[policy.proposals]`:

```toml
[policy.proposals]
daily_cap = 15   # proposals honored per UTC day; omit for no ceiling
worker_cap = 3   # concurrent workers on proposal-only cards; omit to use policy.worker_cap
```

These live outside the extension block on purpose. The core loader knows nothing about what is
inside `[extensions.<name>]`, and more importantly a cap is a gate: an extension enforcing its
own budget is trusting the thing being bounded (#112). The daily tally is durable, so a
redeploy does not refill it, and a batch a human contributed to is never throttled as proposed
work — that batch is the person's request.
