# Setting up AgentDeck: a guide for the coding agent doing it

You are a coding agent (Claude Code, Codex or similar) and a person asked you to set up
AgentDeck on this machine. Follow these steps in order. The person only needs to answer one
question and press a few buttons on GitHub; you do everything else.

When you are done, the person has a dashboard at `http://127.0.0.1:8756/` and a private GitHub
repository for tasks. On the dashboard they press **New work**, type a task, and an agent works
on it and opens a pull request.

## 1. Check the machine

- macOS, or Linux with systemd (`systemctl --user status` works). On macOS the services run
  as launchd agents while the person is logged in, so the deck runs while the Mac is awake;
  the agents' memory caps, the host CPU bars and keeping chats alive across a runtime
  restart are Linux-only. On Windows, tell the person and stop.
- `git`, Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/) (uv can fetch the
  Python). Install uv with the person's agreement if it is missing; on macOS `brew install uv`
  or the installer on uv's page. On macOS, `git` comes with the Xcode command line tools.
- Claude Code or the Codex CLI logged in. The deck's agents use that account and its usage
  limits. The setup command checks Claude's login the way the deck's agents will see it (on
  macOS that differs from a folder merely existing) and asks the person to log in if needed.

## 2. Ask the person one thing

Ask for their **GitHub login**. Also ask whether the first project should be a new private
repository `<login>/agent-tasks` (the default) or a repository they already have
(`owner/name`), which you then pass as `--repo`.

## 3. Install

```sh
git clone https://github.com/eerovil/agentdeck-foss.git ~/agentdeck
cd ~/agentdeck && uv sync
```

It must be `~/agentdeck`: the services and the instructions the deck gives its agents expect it.

## 4. Run the setup command, as often as it asks

```sh
~/agentdeck/.venv/bin/agentdeck-init --login <login> [--repo owner/name]
```

It never prompts. Each run does every step it can, keeps what is done, and ends with one of:

| Exit | Last lines | What you do |
| --- | --- | --- |
| 0 | `DONE: …` | Tell the person what the line says, with its `OPEN:` link. Setup is finished. |
| 3 | `PERSON: …`, any `OPEN: …`, then `THEN: …` | Tell the person exactly what `PERSON:` asks, wait until they say it is done, then run the `THEN:` command. |
| 1 | `FAILED: …` | Fix what it names (or tell the person if it needs them), then run the same command again. |

**Pass on every `OPEN:` line as a link of its own**, one per line, written as a Markdown link
(`[Create the App](http://127.0.0.1:…/)`) so it can be clicked; never bury it inside a
sentence. Pass a `TUNNEL:` line as a command in a code block.

**Run it in the background and read its output as it goes.** When it creates the deck's
GitHub App it prints a `PERSON:` line with a local link and then *waits* (up to 15 minutes)
for the person to press **Create GitHub App** on GitHub, so you must pass that line and its
`OPEN:` link on while it is still running. If the person's browser is on another machine, they
first run the `TUNNEL:` command there.

If the wait ends first, or GitHub leaves the browser on a page that does not load, the run
exits 3 asking for the address that page shows; pass it with `--app-code '<address>'` as the
`THEN:` line says.

Other person steps it may stop at: logging Claude Code in for the deck's agents, creating the
repository on GitHub (when `gh` is not logged in here; an empty repository gets its README from
the setup itself) and installing the App on the account with that repository, choosing
**Only select repositories**. It also stops if the App could change CI files or was given every
repository, and says what the person should change.

## 5. Hand over

Tell the person, in a few lines:

- the address of the dashboard, and that it has **no login**: anyone who can reach the port
  can control the agents, so it listens only on `127.0.0.1`. To open it from a phone, see
  `docs/self-hosting-tips.md` (Tailscale) rather than changing the address to `0.0.0.0`;
- to press **New work**, pick the repository and write a small task, for example "Add a
  hello-world script that prints Hello, world";
- that the agent opens a pull request, and they press **Merge** on the Work page when they
  are happy with it.

## Do not

- Do not edit the files the command wrote to make a step pass; run the command again, or
  follow `docs/github-setup.md` by hand if the person wants something it does not cover
  (an organization board, more repositories, webhooks).
- Do not give the deck the person's own GitHub token. It speaks only as its own GitHub App.
- Do not change the deck's Claude agents from `permission_mode = "auto"`, and never to
  `bypassPermissions`. In auto mode Claude Code's classifier reviews risky tool calls, and a
  call it blocks waits on the Work page for the person to allow or keep blocked.
