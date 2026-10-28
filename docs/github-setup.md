# Connecting AgentDeck to GitHub

AgentDeck reads issues and pull requests, starts a Work for each issue you label, and posts
back as it goes. It never uses your personal `gh` login for that: every repository owner is
mapped to an identity of its own, and an owner with none is refused. This page sets that up.

`agentdeck-init` does steps 1 to 3 for one repository: it creates the
App through GitHub's App-manifest flow, writes the file in step 2 and a board-less poller
project. Use this page for an organization board, more repositories, or webhooks.

## 1. Create a GitHub App for your deck

Create your own App: GitHub → Settings → Developer settings → GitHub Apps → New GitHub App.
Your App's key stays with you, its rate limit is yours, and its webhooks come to you.

- **Homepage URL:** anything, for example your fork's address.
- **Webhook:** off for now; step 4 turns it on if you want it.
- **Repository permissions:** Contents, Issues and Pull requests: read and write. Actions,
  Checks, Commit statuses and Metadata: read only. Leave **Workflows** at No access: with it an
  agent could change a CI file on its own branch and run code with the repository's secrets.
- **Organization permissions** (only if you will use an organization Project board):
  Projects, read and write.
- **Where can this App be installed:** only on this account.

Create it. Note the App ID and the slug (the last part of the App's public URL), and
generate a private key. Save the key as `~/.config/agentdeck/github-app.pem`, readable
only by you (`chmod 600`).

Install the App (its page → Install App) on your personal account, your organization, or
both. Choose **Only select repositories** and pick the ones the deck should work on, never
"All repositories": `agentdeck-init` refuses an installation that takes them all. Open each
installation's settings page; the number at the end of its URL is the installation ID.

On an organization, also protect the branch that deploys: a branch ruleset with **Restrict
updates**, with the people who merge by hand (and your own deck's App, if it should merge) on its
bypass list. An App can merge any pull request it can push to, and the ruleset is what stops it.

## 2. Tell the deck which owner uses which identity

`~/.config/agentdeck/github-authority.toml`:

```toml
[owners]
your-login = "app:your-login"
your-org = "app:your-org"

[app]
app_id = 123456
slug = "your-app-slug"
private_key_file = "~/.config/agentdeck/github-app.pem"
installations = { your-login = 11111111, your-org = 22222222 }
```

Check it with `agentdeck github-account show` and `agentdeck github-account prove`. The
proof reads each installation's repositories and fails loudly on anything it cannot reach.
An owner that is not listed is refused, with this file named in the refusal.

An App works on repositories under a personal account as well as an organization's. The one
thing it cannot reach is a **Project board owned by a personal account**. A poller for
personal repositories runs without a board: Works start from the issues you label.
Organization boards work.

## 3. Start a poller for a project

A poller watches one project's repositories and starts Works. Copy the example project in
`poller/reference/` and follow its README:

```sh
python3 scripts/install_poller_host.py --instance my-project
cp poller/reference/overlay.example.toml \
   ~/.local/share/agentdeck/poller/instances/my-project/overlay.toml
$EDITOR ~/.local/share/agentdeck/poller/instances/my-project/overlay.toml
scripts/poller_engine.py inspect --manifest poller/reference/poller.toml \
  --overlay ~/.local/share/agentdeck/poller/instances/my-project/overlay.toml
```

The example deliberately refuses to activate until you set `board.project_number` to your
organization board's number (the number in its URL). For repositories under a personal
account, which an App cannot give a board, leave the `[board]` table out and move its `org`
(your login) and its label settings to the top level: that absence is how a manifest says it
has no board, and the poller then keeps each card's status in a `status:*` label on its issue.

With that alone, the poller notices new issues, labels and reviews on its timer: a few
minutes after they happen.

## 4. Optional: webhooks, for changes within seconds

GitHub can tell the deck about each change at once, but it needs an address on the
internet, and the deck itself must never be on the internet (it has no login).
`scripts/webhook_gateway.py` is the one piece that is: it checks GitHub's signature on each
delivery and forwards only those to the deck over your private network.

Pick a long random secret and give it to both halves as `AGENTDECK_WEBHOOK_SECRET` (in the
deck's systemd unit, and in the gateway's environment). Then run the gateway where GitHub
can reach it. You do not need a server for this:

- **Tailscale Funnel** (if you already reach the deck over Tailscale): run the gateway on
  the deck's own machine and publish only its port.

  ```sh
  AGENTDECK_WEBHOOK_SECRET=... python3 scripts/webhook_gateway.py \
      --port 8090 --target http://127.0.0.1:8756 &
  tailscale funnel --bg --https=8443 http://127.0.0.1:8090
  ```

  Funnel can publish ports 443, 8443 and 10000 only.
- **Cloudflare Tunnel**: the same idea with a Cloudflare account and a domain of yours.
- **A small server** you already have: run the gateway there with `--target` set to the
  deck's address on your private network.

In the App's settings, turn the webhook on, set its URL to the public address followed by
`/github/webhook` (for example `https://your-machine.your-tailnet.ts.net:8443/github/webhook`)
and the same secret, and subscribe to: Issues, Issue comment, Pull request, Pull request
review, Push, Check run, Check suite, Status and Deployment status.

If the gateway is down, nothing breaks: the pollers still find every change on their timer.
