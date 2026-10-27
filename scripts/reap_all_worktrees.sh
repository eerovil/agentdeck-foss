#!/usr/bin/env bash
# Release every poller project's worktrees that `reap-worktrees` proves finished (#3394).
#
# Worktrees across the poller projects held about 53 GB on 2026-10-22 with the disk 97%
# full, and the reaper only ran when a person ran it. Its own guards decide what goes:
# a worktree is removed only when no Work claims it, it has been quiet for 7+ days, it
# holds no uncommitted or untracked files and everything on it is merged. An instance
# whose runtime claims cannot be read is refused by the reaper itself. One instance
# failing does not stop the rest; the exit status says whether any did.
set -u

instances="${AGENTDECK_POLLER_INSTANCES:-$HOME/.local/share/agentdeck/poller/instances}"
python="${AGENTDECK_PYTHON:-$HOME/agentdeck/.venv/bin/python}"
engine="${AGENTDECK_POLLER_ENGINE:-$HOME/agentdeck/scripts/poller_engine.py}"

status=0
for dir in "$instances"/*/; do
    dir="${dir%/}"
    [[ -f "$dir/poller.toml" && -f "$dir/overlay.toml" ]] || continue
    echo "== ${dir##*/}"
    if ! "$python" "$engine" reap-worktrees \
        --manifest "$dir/poller.toml" --overlay "$dir/overlay.toml" --apply --size; then
        echo "reap-worktrees failed for ${dir##*/}" >&2
        status=1
    fi
done
exit "$status"
