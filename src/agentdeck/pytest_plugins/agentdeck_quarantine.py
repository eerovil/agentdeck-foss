"""Leave out exactly the quarantined tests a verification job names (#3055).

The job queue loads this with ``-p agentdeck_quarantine`` and lists the test ids in
``AGENTDECK_QUARANTINED_TESTS`` (a JSON list). pytest's own ``--deselect`` matches a prefix, so
``test_x`` would take ``test_x_slow`` with it; this matches the whole id and nothing else.

Standalone on purpose: it is put on a run's ``PYTHONPATH`` (or copied into a pod) for any
checkout, which may not have ``agentdeck`` installed at all.
"""

from __future__ import annotations

import json
import os

ENV = "AGENTDECK_QUARANTINED_TESTS"


def _quarantined() -> frozenset[str]:
    try:
        listed = json.loads(os.environ.get(ENV) or "[]")
    except ValueError:
        return frozenset()
    if not isinstance(listed, list):
        return frozenset()
    return frozenset(str(test) for test in listed if str(test))


def pytest_collection_modifyitems(config, items) -> None:
    quarantined = _quarantined()
    if not quarantined:
        return
    kept = [item for item in items if item.nodeid not in quarantined]
    left_out = [item for item in items if item.nodeid in quarantined]
    if left_out:
        config.hook.pytest_deselected(items=left_out)
        items[:] = kept
