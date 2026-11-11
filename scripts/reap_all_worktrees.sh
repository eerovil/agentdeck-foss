#!/usr/bin/env bash
# Release every poller project's worktrees that `reap-worktrees` proves finished (#3394).
#
# Worktrees across the poller projects held about 53 GB on 2026-10-22 with the disk 97%
# full, and the reaper only ran when a person ran it. Its own guards decide what goes:
# a worktree is removed only when no Work claims it, it has been quiet for 7+ days, it
# holds no uncommitted or untracked files and everything on it is merged. An instance
# whose runtime claims cannot be read is refused by the reaper itself. A worktree no Work
# claims that holds uncommitted files or unmerged commits, untouched for 30 days and with no
# process in it, has that work saved to the hard disk and is then removed (#4267). One instance
# failing does not stop the rest; the exit status says whether any did.
set -u

instances="${AGENTDECK_POLLER_INSTANCES:-$HOME/.local/share/agentdeck/poller/instances}"
python="${AGENTDECK_PYTHON:-$HOME/agentdeck/.venv/bin/python}"
engine="${AGENTDECK_POLLER_ENGINE:-$HOME/agentdeck/scripts/poller_engine.py}"
janitor="${AGENTDECK_HOST_JANITOR:-$HOME/agentdeck/scripts/host-janitor.py}"
leftovers="${AGENTDECK_WORKTREE_LEFTOVERS:-/var/mnt/hdd/home/worktree-leftovers}"

status=0
# Claimed worktrees are the deck's to give back, so closed Works' go through it (#4148).
if ! "$python" "$janitor" --closed-works; then
    echo "releasing closed Works' worktrees failed" >&2
    status=1
fi
for dir in "$instances"/*/; do
    dir="${dir%/}"
    [[ -f "$dir/poller.toml" && -f "$dir/overlay.toml" ]] || continue
    echo "== ${dir##*/}"
    if ! "$python" "$engine" reap-worktrees \
        --manifest "$dir/poller.toml" --overlay "$dir/overlay.toml" --apply --size \
        --save-after-days 30 --leftovers "$leftovers"; then
        echo "reap-worktrees failed for ${dir##*/}" >&2
        status=1
    fi
done
exit "$status"
