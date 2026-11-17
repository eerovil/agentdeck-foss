#!/usr/bin/env bash
# Keep every deck process on this host from opening a desktop password box (#4340).
#
# The KDE session puts SSH_ASKPASS and SUDO_ASKPASS (/usr/bin/ksshaskpass) into the
# systemd user manager, so every unit and every `systemd-run` it starts inherits
# them. Git runs SSH_ASKPASS before it looks at GIT_TERMINAL_PROMPT, so a background
# git call that needs a password opened a box nobody saw and hung until its timeout
# (#4337: a design review held its lock for 300 s). This script holds one rule for
# the whole host, and is safe to run every tick:
#
#   1. removes the askpass variables from the user manager, which hands them to
#      transient units and to anything started before a unit's own unset applies;
#   2. sets git's global credential.interactive=false and an empty core.askPass,
#      which stops git from falling back to SSH_ASKPASS whatever the environment;
#   3. writes a Plasma env script that unsets them at login, so a reboot does not
#      bring them back (Plasma sources the user's scripts after the system's), and
#      a ~/.bashrc.d script that unsets them in every bash shell, login shells
#      (`bash -lc`, the deck's command launcher) included: /etc/profile.d's
#      kde-openssh-askpass.sh exports SSH_ASKPASS again in each, and git's config
#      does not stop a direct ssh call from running it;
#   4. installs a drop-in that unsets them for every agentdeck and host-janitor
#      service, whatever its unit file says: a staging checkout on an older commit,
#      or a rendered poller unit from before, gets the same policy as live;
#   5. reads every process of every active agentdeck and host-janitor unit, scopes
#      and jobs that are still starting included. A long-running service whose
#      processes still carry one restarts, so no old environment outlives the fix.
#      Only the owner of live work may cut it, so: a runtime (agentdeck*-codex)
#      restarts only while its /activity says no chat or job would be cut; a unit
#      a deck process started itself (a Codex app-server backend) is replaced by
#      that process once its threads are idle; and nothing else is interrupted: a
#      oneshot job still running (nightly tests, an upgrade, a poller cycle, this
#      deploy), a chat's or job's scope, and the unit this script runs inside keep
#      their processes until they end. Git cannot prompt there either (step 2);
#   6. warns about each of those until it ends, and about an open ksshaskpass box,
#      once when the set changes, and says when the host is clean.
#
# It prints one line per change or new warning and nothing when the host is already
# right, so deploy-live.sh can log its output on every tick without noise. It never
# fails: a host it cannot fix is reported, not a deploy refused.
#
# Overrides (tests): AGENTDECK_DEPLOY_STATE, AGENTDECK_PROC, AGENTDECK_CGROUP_ROOT,
# AGENTDECK_SELF_CGROUP, XDG_CONFIG_HOME.
set -uo pipefail

STATE_DIR="${AGENTDECK_DEPLOY_STATE:-${XDG_DATA_HOME:-$HOME/.local/share}/agentdeck/deploy}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
PROC="${AGENTDECK_PROC:-/proc}"
CGROUP_ROOT="${AGENTDECK_CGROUP_ROOT:-/sys/fs/cgroup}"
SELF_CGROUP="${AGENTDECK_SELF_CGROUP:-/proc/self/cgroup}"
VARS=(SSH_ASKPASS SUDO_ASKPASS GIT_ASKPASS)
PLASMA_ENV="$CONFIG_HOME/plasma-workspace/env/zz-agentdeck-no-askpass.sh"
SHELL_ENV="$HOME/.bashrc.d/zz-agentdeck-no-askpass.sh"
UNIT_DIR="$CONFIG_HOME/systemd/user"
# `agentdeck-.service.d` applies to every service named agentdeck-*, templates and
# staging included; the bare `agentdeck.service` needs its own.
DROPIN_DIRS=(agentdeck.service.d agentdeck-.service.d host-janitor-.service.d)
DROPIN_NAME=zz-no-askpass.conf
WARNINGS_FILE="$STATE_DIR/askpass_warnings"

# 1. The user manager.
manager_env="$(systemctl --user show-environment 2>/dev/null || true)"
present=()
for var in "${VARS[@]}"; do
  grep -q "^$var=" <<<"$manager_env" && present+=("$var")
done
if ((${#present[@]})); then
  if systemctl --user unset-environment "${present[@]}"; then
    echo "removed ${present[*]} from the systemd user manager"
  else
    echo "could not remove ${present[*]} from the systemd user manager"
  fi
fi

if ! grep -qx 'SSH_ASKPASS_REQUIRE=never' <<<"$manager_env"; then
  # With DISPLAY set, ssh runs its built-in askpass even with SSH_ASKPASS unset.
  if systemctl --user set-environment SSH_ASKPASS_REQUIRE=never; then
    echo "set SSH_ASKPASS_REQUIRE=never in the systemd user manager"
  else
    echo "could not set SSH_ASKPASS_REQUIRE=never in the systemd user manager"
  fi
fi

# 2. Git's global config. An empty core.askPass is what stops the SSH_ASKPASS
# fallback (checked on git 2.56). Any other value is a helper that can prompt, and
# nothing on this host may, so it is replaced too.
if [[ "$(git config --global --get credential.interactive 2>/dev/null)" != "false" ]]; then
  git config --global credential.interactive false \
    && echo "set git config --global credential.interactive false"
fi
if ! askpass="$(git config --global --get-all core.askPass 2>/dev/null)" || [[ -n "$askpass" ]]; then
  if git config --global --replace-all core.askPass ""; then
    echo "set git config --global core.askPass to empty${askpass:+ (was '$askpass')}"
  else
    echo "could not set git config --global core.askPass to empty"
  fi
fi

# 3. The next login.
plasma_body='# Written by AgentDeck scripts/host_no_askpass.sh (#4340): this host runs deck
# processes only, and a background git or ssh call must never open a password box.
# Plasma sources this after /etc/xdg/plasma-workspace/env/ksshaskpass.sh, so the
# session, and the systemd user manager it fills, starts without them. With DISPLAY
# set, ssh runs its built-in askpass even then, unless told never to.
unset SSH_ASKPASS SUDO_ASKPASS GIT_ASKPASS
export SSH_ASKPASS_REQUIRE=never'
if [[ "$(cat "$PLASMA_ENV" 2>/dev/null)" != "$plasma_body" ]]; then
  if mkdir -p "$(dirname "$PLASMA_ENV")" && printf '%s\n' "$plasma_body" >"$PLASMA_ENV"; then
    echo "wrote $PLASMA_ENV"
  else
    echo "could not write $PLASMA_ENV"
  fi
fi

# Every bash shell. /etc/profile runs /etc/profile.d before ~/.bash_profile, and
# Fedora's ~/.bashrc (which ~/.bash_profile sources) runs ~/.bashrc.d last, so
# this unset comes after the export; /etc/bashrc does the same for a shell that
# is not a login shell.
shell_body='# Written by AgentDeck scripts/host_no_askpass.sh (#4340): /etc/profile.d exports
# SSH_ASKPASS in every login shell, so a deck command run with `bash -lc` would
# get it back and a background ssh or git call could open a password box.
unset SSH_ASKPASS SUDO_ASKPASS GIT_ASKPASS
export SSH_ASKPASS_REQUIRE=never'
if [[ "$(cat "$SHELL_ENV" 2>/dev/null)" != "$shell_body" ]]; then
  if mkdir -p "$(dirname "$SHELL_ENV")" && printf '%s\n' "$shell_body" >"$SHELL_ENV"; then
    echo "wrote $SHELL_ENV"
  else
    echo "could not write $SHELL_ENV"
  fi
fi

# 4. One policy for every deck service, loaded before anything is restarted.
dropin_body='# Written by AgentDeck scripts/host_no_askpass.sh (#4340): no deck process may
# open a desktop password box, whatever commit its unit file comes from.
[Service]
UnsetEnvironment=SSH_ASKPASS SUDO_ASKPASS GIT_ASKPASS
Environment=GIT_TERMINAL_PROMPT=0 SSH_ASKPASS_REQUIRE=never'
written=()
for dir in "${DROPIN_DIRS[@]}"; do
  file="$UNIT_DIR/$dir/$DROPIN_NAME"
  [[ "$(cat "$file" 2>/dev/null)" == "$dropin_body" ]] && continue
  if mkdir -p "$UNIT_DIR/$dir" && printf '%s\n' "$dropin_body" >"$file"; then
    written+=("$file")
  else
    echo "could not write $file"
  fi
done
if ((${#written[@]})); then
  if systemctl --user daemon-reload; then
    echo "installed ${written[*]}"
  else
    echo "installed ${written[*]}, but systemctl --user daemon-reload failed"
  fi
fi

# 5. Every process of every active deck unit. A unit's processes are its whole
# control group, so a oneshot job's children and a scope's chat are read too; the
# main PID is the fallback when the group cannot be read.
pids_of() {
  local group pids=""
  group="$(systemctl --user show "$1" --property ControlGroup --value 2>/dev/null || true)"
  if [[ -n "$group" && -d "$CGROUP_ROOT$group" ]]; then
    pids="$(find "$CGROUP_ROOT$group" -name cgroup.procs -exec cat {} + 2>/dev/null)"
  fi
  if [[ -z "$pids" ]]; then
    pids="$(systemctl --user show "$1" --property MainPID --value 2>/dev/null || true)"
  fi
  grep -E '^[1-9][0-9]*$' <<<"$pids"
}

# What in a unit's processes can still open a password box: an askpass variable
# (git and sudo run it), or no SSH_ASKPASS_REQUIRE=never (with DISPLAY set, ssh
# runs its built-in /usr/libexec/openssh/ssh-askpass even with SSH_ASKPASS unset).
# A process whose environ cannot be read (gone, or a kernel thread) is skipped.
askpass_in() {
  local pid env names="" bare=""
  while read -r pid; do
    env="$(tr '\0' '\n' 2>/dev/null <"$PROC/$pid/environ")" || continue
    [[ -n "$env" ]] || continue
    names+="$(grep -oE '^(SSH|SUDO|GIT)_ASKPASS=' <<<"$env" | tr -d '=')"$'\n'
    grep -qx 'SSH_ASKPASS_REQUIRE=never' <<<"$env" || bare=1
  done < <(pids_of "$1")
  names="$(grep -v '^$' <<<"$names" | sort -u | tr '\n' ' ')"
  names="${names% }"
  if [[ -n "$bare" ]]; then
    names="${names:+$names, }no SSH_ASKPASS_REQUIRE=never"
  fi
  printf '%s' "$names"
}

# Whether a runtime can restart now: only a clean answer that nothing would be cut.
# A runtime that does not answer is not taken as idle, as in deploy-live.sh.
runtime_idle() {
  local socket body
  socket="$(systemctl --user show "$1" --property Environment --value 2>/dev/null \
    | tr ' ' '\n' | sed -n 's/^AGENTDECK_CODEX_SOCKET=//p' | tail -n 1)"
  socket="${socket:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/agentdeck/codex-runtime.sock}"
  body="$(curl -fsS --max-time 6 --unix-socket "$socket" http://localhost/activity 2>/dev/null)" \
    || return 1
  python3 -I -S - "$body" <<'PY' 2>/dev/null
import json, sys
try:
    payload = json.loads(sys.argv[1])
except ValueError:
    raise SystemExit(1)
if not isinstance(payload, dict) or not isinstance(payload.get("active"), bool):
    raise SystemExit(1)
cut = payload.get("cut_by_restart")
busy = cut > 0 if isinstance(cut, int) and not isinstance(cut, bool) else payload["active"]
raise SystemExit(1 if busy else 0)
PY
}

warnings=()
scopes=()
while read -r unit _ _ sub _; do
  [[ "$unit" == *.service || "$unit" == *.scope ]] || continue
  found="$(askpass_in "$unit")"
  [[ -n "$found" ]] || continue
  if [[ "$unit" == *.scope ]]; then
    # A chat's or job's processes: never cut, and many, so one line for all.
    scopes+=("$unit")
    continue
  fi
  if [[ "$(systemctl --user show "$unit" --property Transient --value 2>/dev/null)" == yes ]]; then
    # Started by a deck process with systemd-run (a Codex app-server backend):
    # only that owner knows whether a turn runs in it, and it replaces the unit
    # itself once idle, so the guard never restarts one.
    warnings+=("$unit still has $found: started by a deck process, which replaces it once idle")
    continue
  fi
  if grep -qF "/$unit" "$SELF_CGROUP" 2>/dev/null; then
    warnings+=("$unit still has $found: it runs this check, and ends on its own")
    continue
  fi
  if [[ "$sub" != running ]]; then
    # A oneshot job still starting (sub-state start), or one stopping: restarting
    # it would cut the job, and its next run starts clean.
    warnings+=("$unit still has $found: a job started before the fix, left to end on its own")
    continue
  fi
  if ! systemctl --user show "$unit" --property UnsetEnvironment --value 2>/dev/null \
      | grep -qw SSH_ASKPASS \
    || ! systemctl --user show "$unit" --property Environment --value 2>/dev/null \
      | grep -qw 'SSH_ASKPASS_REQUIRE=never'; then
    # Restarting would bring the variable straight back.
    warnings+=("$unit still has $found: systemd has not loaded the unset for it")
    continue
  fi
  if [[ "$unit" == *codex.service ]] && ! runtime_idle "$unit"; then
    warnings+=("$unit still has $found: restarts once no chat or job would be cut")
    continue
  fi
  if ! systemctl --user restart "$unit"; then
    warnings+=("$unit still has $found: its restart failed")
    continue
  fi
  echo "restarted $unit, which had $found"
  found="$(askpass_in "$unit")"
  [[ -z "$found" ]] || warnings+=("$unit still has $found after a restart")
done < <(systemctl --user list-units 'agentdeck*' 'host-janitor-*' --type=service,scope \
  --state=active,activating,deactivating,reloading --no-legend --plain 2>/dev/null)
if ((${#scopes[@]})); then
  warnings+=("${#scopes[@]} chat or job scope(s) started before the fix can still open a password box, left to end on their own")
fi

# What is still wrong. Said once when it changes, not every tick.
if ! grep -qs 'bashrc\.d' "$HOME/.bashrc"; then
  warnings+=("~/.bashrc does not read ~/.bashrc.d, so a login shell keeps SSH_ASKPASS from /etc/profile.d")
fi
if boxes="$(pgrep -x 'ksshaskpass|ssh-askpass' 2>/dev/null)"; then
  warnings+=("a password box is open (pid $(tr '\n' ' ' <<<"$boxes" | sed 's/ $//')): something still prompts")
fi
current="$(printf '%s\n' "${warnings[@]+"${warnings[@]}"}")"
previous="$(cat "$WARNINGS_FILE" 2>/dev/null || true)"
if [[ "$current" != "$previous" ]]; then
  if [[ -n "$current" ]]; then
    printf 'warning: %s\n' "${warnings[@]}"
  else
    echo "no deck process can open a password box, and none is open"
  fi
  mkdir -p "$STATE_DIR" 2>/dev/null && printf '%s' "$current" >"$WARNINGS_FILE"
fi
exit 0
