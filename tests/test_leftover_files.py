"""The harness frees files that finished tests left open (#4649)."""

from __future__ import annotations

import gc
import os

from conftest import LeftoverFiles


class _Holder:
    """An open file in a reference cycle, as a finished test's app is."""

    open_files = 0

    def __init__(self) -> None:
        type(self).open_files += 1
        self.itself = self
        self.file = os.fdopen(os.open(os.devnull, os.O_RDONLY))

    def __del__(self) -> None:
        self.file.close()
        type(self).open_files -= 1


def test_files_left_in_cycles_are_closed_once_enough_pile_up(monkeypatch):
    leftover = LeftoverFiles()
    assert leftover.count() > 0 or not os.path.exists("/proc/self/fd")
    # Only its own files: a thread an earlier test left may open or close one meanwhile.
    monkeypatch.setattr(leftover, "count", lambda: _Holder.open_files)
    gc.freeze()  # only what this test makes is walked
    gc.disable()
    try:
        for _ in range(leftover.STEP - 1):
            _Holder()
        assert not leftover.collect_if_many()
        assert _Holder.open_files == leftover.STEP - 1
        _Holder()
        assert leftover.collect_if_many()
        assert _Holder.open_files == 0
        assert not leftover.collect_if_many()
        # Files really in use raise the bar only for as long as they stay open.
        in_use = [_Holder() for _ in range(leftover.STEP)]
        assert leftover.collect_if_many()
        assert not leftover.collect_if_many()
        del in_use
        gc.collect()
        assert not leftover.collect_if_many()
        for _ in range(leftover.STEP):
            _Holder()
        assert leftover.collect_if_many()
        assert _Holder.open_files == 0
    finally:
        gc.enable()
        gc.unfreeze()
