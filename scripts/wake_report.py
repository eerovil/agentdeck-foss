#!/usr/bin/env python3
"""Compare the wake word detector with Whisper's check on real phrases (#3476).

Reads the wake log voice mode writes while it waits for "Hey Jarvis"
(``[speech] wake_log_dir``): one ``detector`` line per waiting phrase the detector
checked, one ``whisper`` line for each phrase Whisper transcribed, joined by the
phrase id, and a ``waiting`` line where each spell of waiting starts and stops. In
``[speech] wake = "shadow"`` every waiting phrase should have both checks.

It prints how often each check woke, where they disagree (with Whisper's words for
each), shadow phrases missing a check, the time each took, and whether the detector
meets the rule for switching to ``wake = "detector"``: it woke on at least 90 % of
the phrases Whisper heard the wake word in, and woke on its own, with no
Jarvis-like word in Whisper's text, at most once per 8 hours of waiting. Every
shadow phrase must have both checks: while any is missing one (a failed detector
call, or a Whisper transcription that failed after the detector answered), the
report lists it and withholds the switch verdict.

    scripts/wake_report.py [--log PATH] [--since 2026-10-25]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_LOG = "~/.local/share/agentdeck/speech/wake/wake-log.jsonl"
CAUGHT_SHARE = 0.9
OWN_WAKES_PER_HOUR = 1 / 8
JARVIS_LIKE = re.compile(r"jar|jav|arvi|travis", re.IGNORECASE)


def read(path: Path, since: float) -> tuple[dict[str, dict], dict[str, dict]]:
    """From ``since`` on: phrase id -> {"detector": line, "whisper": line}, and spell
    id -> {"start": line, "stop": line}."""
    phrases: dict[str, dict] = {}
    spells: dict[str, dict] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict) or line.get("at", 0) < since:
            continue
        if line.get("check") == "waiting" and line.get("spell"):
            spells.setdefault(line["spell"], {})[line.get("state", "")] = line
        elif line.get("id"):
            phrases.setdefault(line["id"], {})[line.get("check", "")] = line
    return phrases, spells


def waiting_hours(spells: dict[str, dict]) -> tuple[float, int]:
    """Hours between each spell's start and stop, and how many spells have no stop
    (a page that died while waiting); those count no time, so the allowance for the
    detector's own wakes errs small."""
    closed = [s for s in spells.values() if "start" in s and "stop" in s]
    open_spells = sum(1 for s in spells.values() if "start" in s and "stop" not in s)
    seconds = sum(max(0.0, s["stop"]["at"] - s["start"]["at"]) for s in closed)
    return seconds / 3600, open_spells


def mean(values: list[float]) -> str:
    return f"{sum(values) / len(values):.0f} ms" if values else "-"


def _listed(found: list[dict]) -> list[str]:
    return [
        f"  {p['detector']['id']}  score {p['detector'].get('score', 0):.3f}  "
        f"{p['whisper'].get('text')!r}"
        for p in found
    ]


def report(phrases: dict[str, dict], spells: dict[str, dict]) -> str:
    # One rule (owner's decision on #3476): every shadow phrase needs both checks,
    # and the switch verdict waits until none is missing one.
    shadow = [
        p for p in phrases.values() if any(line.get("mode") == "shadow" for line in p.values())
    ]
    both = [p for p in shadow if "detector" in p and "whisper" in p]
    incomplete = [p for p in shadow if not ("detector" in p and "whisper" in p)]
    hours, open_spells = waiting_hours(spells)
    whisper_hits = [p for p in both if p["whisper"].get("matched")]
    detector_hits = [p for p in both if p["detector"].get("woken")]
    agreed = [p for p in whisper_hits if p["detector"].get("woken")]
    only_whisper = [p for p in whisper_hits if not p["detector"].get("woken")]
    only_detector = [p for p in detector_hits if not p["whisper"].get("matched")]
    own = [p for p in only_detector if not JARVIS_LIKE.search(p["whisper"].get("text") or "")]
    lines = [
        f"Waiting: {hours:.1f} h over {len(spells)} spells"
        + (f" ({open_spells} never closed, counted as no time)" if open_spells else ""),
        f"Shadow phrases with both checks: {len(both)}",
        f"Shadow phrases missing a check ({len(incomplete)}):",
        *(
            f"  {next(iter(p.values())).get('id')}  no "
            + ("detector check" if "detector" not in p else "Whisper transcription")
            for p in incomplete
        ),
        f"Whisper heard the wake word: {len(whisper_hits)}",
        f"Detector woke: {len(detector_hits)}",
        f"Both: {len(agreed)}",
        "",
        f"Only Whisper ({len(only_whisper)}) - detector misses:",
        *_listed(only_whisper),
        f"Only the detector ({len(only_detector)}):",
        *_listed(only_detector),
        "",
        "Time taken: the detector's check "
        + mean([p["detector"]["ms"] for p in both if isinstance(p["detector"].get("ms"), int)])
        + ", Whisper from the phrase's end to its words "
        + mean([p["whisper"]["ms"] for p in both if isinstance(p["whisper"].get("ms"), int)]),
        "",
    ]
    if incomplete:
        lines.append(
            f"Switch verdict withheld: {len(incomplete)} shadow phrases are missing a check, "
            "so the comparison is incomplete."
        )
        return "\n".join(lines)
    caught = len(agreed) / len(whisper_hits) if whisper_hits else 0.0
    allowed = OWN_WAKES_PER_HOUR * hours
    lines.append(
        f"Caught {caught:.0%} of Whisper's wakes (needs {CAUGHT_SHARE:.0%}): "
        + ("ok" if whisper_hits and caught >= CAUGHT_SHARE else "not yet")
    )
    lines.append(
        f"Woke on its own {len(own)} times (allowed {allowed:.1f} in {hours:.1f} h): "
        + ("ok" if len(own) <= allowed else "too many")
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--since", default="", help="a date or ISO time")
    args = parser.parse_args(argv)
    path = Path(args.log).expanduser()
    if not path.exists():
        print(f"no wake log at {path}", file=sys.stderr)
        return 1
    since = datetime.fromisoformat(args.since).timestamp() if args.since else 0.0
    print(report(*read(path, since)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
