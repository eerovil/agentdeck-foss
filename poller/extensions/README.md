# Poller Extensions (the conventional folder)

An extension is a **separate program**, not an import. The engine runs it at a named hook with
a JSON payload on stdin and reads a JSON object on stdout:

```
$ ./screenshots.py health <<< '{"hook":"health","mode":"live","config":{"default_routes":["/"]},"context":{}}'
{"health": {"state": "ok", "routes": 1}}
```

The engine only ever receives **requests** and decides each one itself, so an extension can ask
for work but never act on its own authority (#112). The one request a wake honours is
`open-work`, which asks for an *issue to exist*: the extension supplies a repo, a stable key and a
title, and the engine decides whether to open anything — exactly once, because a duplicate issue
is not undoable (#393). The issue is labelled at creation, so the deck starts its Work. A crash, timeout or malformed answer
skips that extension and marks it health-degraded — it never fails the cycle.

An `open-work` payload may also say `"spec_only": true`. The engine then opens the issue with the
project's `spec_only_label` beside the ownership label, in the same write, and the deck starts its
Work as a specification: it writes a plan and nothing is built until a person approves it
(#2874). A project that names no `spec_only_label` refuses such a request rather than opening it
as an ordinary build.

An `observe` hook's `context` carries `settled`, the proposal keys this extension has already had
decided — opened or staged — namespaced to the asking extension; its `memory` (below); and
`quiet`, whether this wake asked GitHub nothing, in which case an extension reading GitHub should
not either. The card-era `cards` list and `works_router` flag are gone (#3741). An extension that
bounds how much it proposes per wake **must** subtract `settled` before spending that bound: its
own source usually keeps reporting work that already has an issue, and re-asking spends every slot
on proposals the engine drops as already-seen, so nothing new is ever proposed (#1709).

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
scripts/poller_engine.py contract-test --name screenshots --hook validate-config --hook health \
    -- ./poller/extensions/screenshots.py
```

## The shipped extensions

`sentry_autofix.py` and `screenshots.py` are what is left of #113's references. `deploy_watch.py`,
`drone_deploy_watch.py` and `chatgpt_nudge.py`, and the `/shot` command of `screenshots.py`, were
retired in #3741: they gated a card's release, queued work on a card, or reviewed a card's pull
request, and the engine holds no cards since #3732.

| Extension | Hooks | What it does |
| --- | --- | --- |
| `sentry_autofix.py` | validate-config, observe, health | Proposes work for unresolved Sentry incidents. Known infrastructure signatures are denied and short same-repository bursts with similar titles become one issue. A proposal has a stable identity, so it is deduplicated and subject to every normal rule. |
| `screenshots.py` | validate-config, health | Checks the project's `default_routes`, the pages worth shooting, and reports how many there are. |


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
silent hole. The wake carries on, the extension answers no hook, and its instance reads
*degraded* on the deck.

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
