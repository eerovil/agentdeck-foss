# Tips for running AgentDeck on your own machine

AgentDeck runs on one Linux machine under your user's systemd. The author's deck grew a
few helpers around it that fit only that machine: its disks, its graphics card, its
Kubernetes clusters and its private network. They are not part of this copy. Each tip
below says what the helper did and why it helped, so you can build your own if you want
one.

## Reach the deck from your phone

AgentDeck has no login, so never expose it to the internet. Bind it to `127.0.0.1` and put
it behind a private network you trust. With [Tailscale](https://tailscale.com/):

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8756
```

The deck is then at `https://<machine>.<tailnet>.ts.net` from any device on your tailnet,
with a real certificate, which the installable web app and push notifications need.

## Deploy each merge by itself

The author's deck deploys every merge to `master` within two minutes: a systemd timer
(`OnBootSec=90`, `OnUnitInactiveSec=120`) runs a script that fetches, checks the checkout is
clean and on `master`, fast-forwards, and restarts `agentdeck.service`. It restarts the
persistent runtime (`agentdeck-codex.service`) only when the change touches it and no
chat is mid-turn, because a runtime restart interrupts Codex turns. Write it to refuse,
and log why, when the checkout is dirty or on another branch.

## Clean up after the agents

Coding agents leave things behind. A timer every six hours that removes them keeps a small
machine healthy:

- MCP servers left running after their `codex app-server` exited;
- container images nothing has used for weeks (`podman image prune`);
- scratch files in `/tmp` and `/var/tmp` nothing has touched for days;
- `uv cache prune` for packages no environment uses;
- worktrees under `.worktrees/` whose branch has merged.

Never let it touch a running agent's processes or a worktree with uncommitted changes.

## Run the test suite somewhere else

A full `pytest -q` run takes all four cores of a small machine for a few minutes. The
job queue (`agentdeck job submit -- pytest -q`) runs one suite at a time on this machine
by default. The author's deck also streams the working copy into a throwaway pod on a
Kubernetes cluster and runs the suite there: list the tracked and untracked files
(`git ls-files --cached --others --exclude-standard`), `tar` them into the pod, install the
dependencies from a prebuilt image, and run `pytest`. Any spare machine with `k3s` works.

## A staging copy of the deck

To try a change against real data before merging, run a second deck from its own
worktree, with its own database, cache and socket folder, on another port. Copy the live
database into it when you want production's state. It shares your agent accounts, so a
chat started there spends real usage.

## Voice mode

Voice mode sends speech to a separate service for speech-to-text and text-to-speech. The
author's ran [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and
[Piper](https://github.com/rhasspy/piper) on an old graphics card, started on demand by a
systemd socket. Point the voice settings at your own service, or leave voice mode off.

## Ostree systems (Fedora Silverblue and its relatives)

On an rpm-ostree system `/home` is a link to `/var/home`, so one folder has two
spellings. AgentDeck compares resolved paths, so either works, but use one spelling in
your config and systemd units so logs and links match.

## Check for new agent releases

A weekly timer that compares `claude --version` and `codex --version` with the newest
releases, and files an issue with the release notes when either moved, keeps you ahead of
changes to the transcript formats AgentDeck reads.
