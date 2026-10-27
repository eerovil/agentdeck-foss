#!/usr/bin/env bash
# The offline software check a deploy runs on an incoming commit before selecting it (#2153).
#
#   scripts/check_poller_software.sh <tree>
#
# <tree> is an unpacked copy of the candidate commit. Every Poller test in it — engine unit,
# config, conformance, adapter, host, ops, and the tests that parse every checked-in Poller
# Manifest — runs against that copy's own sources. The set is the same `test_poller_*` group the
# deploy's trigger watches, so no Poller code can land past a failing test of its own. Nothing
# here reads GitHub or runs a real poller cycle: whether software may deploy is a question about
# the software.
set -euo pipefail

tree="${1:?usage: check_poller_software.sh <tree>}"
python="${AGENTDECK_CHECK_PYTHON:-python3}"
cd "$tree"
shopt -s nullglob
tests=(tests/test_poller_*.py)
if [[ ${#tests[@]} -eq 0 ]]; then
  echo "no poller tests found in $tree" >&2
  exit 1
fi
# The candidate's sources first on the path, ahead of the installed checkout. A deploy is
# not a suite submission, so it does not queue behind the host's heavy-command slot.
AGENTDECK_HEAVY_SLOT_HELD=1 PYTHONPATH="$tree/src${PYTHONPATH:+:$PYTHONPATH}" \
  exec "$python" -m pytest -q -p no:cacheprovider "${tests[@]}"
