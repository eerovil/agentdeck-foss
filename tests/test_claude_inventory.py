"""The Claude account's per-scan transcript inventory (#1088).

Every 15 seconds each account's whole ``projects`` tree is listed and ranked by
mtime. On a real account that is tens of thousands of directory entries, so what
this does *per file* is the scan's dominant cost.
"""

from __future__ import annotations

import os
from pathlib import Path

from agentdeck.providers.claude_code import provider as provider_mod


def _write(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    os.utime(path, (mtime, mtime))
    return path


def test_the_inventory_ranks_by_mtime_and_stats_each_transcript_once(
    tmp_path, monkeypatch
):
    """The ranking used to re-``stat`` every file the listing had just stat'ed.

    That doubled the syscalls of the most expensive thing the scan does, for a
    number it already had in hand. The ranking and the newest-wins pick must
    still agree, and each file may be looked at once.
    """
    projects = tmp_path / "projects"
    old = _write(projects / "a" / "old.jsonl", 100)
    dupe_old = _write(projects / "a" / "dupe.jsonl", 200)
    dupe_new = _write(projects / "b" / "dupe.jsonl", 300)
    new = _write(projects / "a" / "new.jsonl", 400)

    stats: list[str] = []
    real_stat = Path.stat

    def counting_stat(self, *args, **kwargs):
        if self.suffix == ".jsonl":
            stats.append(str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting_stat)
    transcripts, ranked, _subagents = provider_mod._scan_inventory(tmp_path)

    assert ranked == ["new", "dupe", "old"]
    # The newest file wins for an id that appears under two project slugs.
    assert transcripts["dupe"] == dupe_new
    assert transcripts["new"] == new
    assert transcripts["old"] == old
    assert sorted(stats) == sorted(str(p) for p in (old, dupe_old, dupe_new, new))


def test_a_transcript_that_vanishes_mid_listing_is_skipped_not_fatal(
    tmp_path, monkeypatch
):
    """Claude prunes and rewrites these files while the scan is walking them.

    The old code stat'ed inside a comparison that would have raised straight out
    of the scan; reading the mtime up front makes the race explicit.
    """
    projects = tmp_path / "projects"
    kept = _write(projects / "p" / "kept.jsonl", 100)
    _write(projects / "p" / "gone.jsonl", 200)

    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "gone.jsonl":
            raise OSError("vanished")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    transcripts, ranked, _subagents = provider_mod._scan_inventory(tmp_path)

    assert ranked == ["kept"]
    assert transcripts == {"kept": kept}
