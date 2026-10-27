"""Quarantine a flaky test the first time the job queue forgives it (#3055).

The queue forgives a verification that failed only on a test a rerun of the same commit then
passed (``poller_engine.flaky``, #2382). Forgiving keeps that one Work moving, but the test
stays in the gate and fails the next Work's run the same way. So the first time a test is
forgiven as flaky, it is quarantined here:

- every later verification job for that repository leaves exactly that test out (the
  ``pytest_plugins/agentdeck_quarantine.py`` plugin), until
- its fix issue, filed here and labelled with the project's ownership label so a worker picks
  it up, is closed. Closing the issue is what brings the test back into the gate.

Only a verification is affected. A rerun of failing tests, and a ``pytest`` anyone runs by
hand to reproduce the flake, still run it.

Only repositories a poller manifest declares are quarantined: anywhere else there is no
label to hand the fix issue to a worker, and a test with no issue would stay out for good.

The runtime is the one writer: the queue adds records and ``sweep`` files issues and
releases closed ones. Creating an issue is not idempotent, so the intent is saved before the
write, and a sweep that finds an intent without a number looks for the marked issue before it
creates another.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import gh_cli

log = logging.getLogger(__name__)

FILENAME = "quarantine.json"
MARKER = "<!-- agentdeck-test-quarantine: {repository}::{test} -->"
# How often filings are retried and fix issues are checked for closing. A closed issue brings
# its test back within this long; each check is one read per quarantined test.
SWEEP_S = 600.0
GH_TIMEOUT_S = 30.0
# How many recent issues an interrupted filing scans for its marker.
MARKED_SCAN = 100

# `gh issue create` answers these before anything is created (as in ``new_work``).
_DEFINITE_REFUSAL = re.compile(
    r"could not add label|Could not resolve to a Repository|"
    r"does not have the correct permissions|HTTP 40[134]\b|HTTP 422\b",
    re.IGNORECASE,
)
_ISSUE_URL = re.compile(r"https://github\.com/[^/]+/[^/]+/issues/(\d+)")


def _key(repository: str, test: str) -> str:
    return f"{repository.lower()}::{test}"


def title(test: str) -> str:
    text = f"Flaky test: {test}"
    return text if len(text) <= 200 else text[:199] + "…"


def body(record: dict) -> str:
    test = str(record.get("test") or "")
    commit = str(record.get("commit") or "")[:12]
    return (
        f"The job queue forgave `{test}`: it failed verification job "
        f"{record.get('verification') or '?'} on commit `{commit}`, then passed on a rerun of "
        f"the same commit (job {record.get('job_id') or '?'}).\n\n"
        "It is now **quarantined**: verification runs in this repository leave it out until "
        "this issue closes, and closing this issue brings it back into the gate. So close it "
        "only with the fix (a pull request that says `Closes` this issue).\n\n"
        "Find why it fails and fix the test or the code under it. Do not skip, mark or delete "
        "the test to make it pass. Verification jobs leave it out while this issue is open, so "
        "prove the fix by running the test directly, several times.\n\n"
        + MARKER.format(repository=record.get("repository") or "", test=test)
    )


def _gh() -> str:
    return shutil.which("gh") or "gh"


class Quarantine:
    """The quarantined tests of every repository, kept in one JSON file."""

    def __init__(
        self,
        path: Path,
        *,
        labels: Callable[[str], str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        # The ownership label of a repository a poller manifest declares, or "" for any other.
        self._labels = labels
        self._clock = clock
        self._records: dict[str, dict[str, Any]] = self._read()
        self._wake = asyncio.Event()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.exception("quarantine: cannot read %s; starting empty", self._path)
            return {}
        tests = raw.get("tests") if isinstance(raw, dict) else None
        if not isinstance(tests, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in tests.items()
            if isinstance(value, dict) and value.get("repository") and value.get("test")
        }

    def _save(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps({"tests": self._records}, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
            return True
        except OSError:
            log.exception("quarantine: cannot write %s", self._path)
            return False

    def records(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self._records.values()]

    def tests(
        self, repository: str, *, keep_for_issue: int | None = None
    ) -> tuple[str, ...]:
        """The tests a verification in ``repository`` leaves out, in a stable order.

        ``keep_for_issue`` is the issue the verified Work is about: a test whose fix issue it
        is stays in, so the pull request meant to fix it is verified by running it (#3584).
        """
        wanted = str(repository or "").lower()
        if not wanted:
            return ()
        return tuple(
            sorted(
                str(record["test"])
                for record in self._records.values()
                if str(record.get("repository") or "").lower() == wanted
                and (keep_for_issue is None or record.get("issue") != keep_for_issue)
            )
        )

    def add(self, repository: str, tests: Iterable[dict], *, verification: str) -> list[str]:
        """Quarantine each forgiven test not already quarantined; return the new ones.

        ``tests`` are ``flaky.forgiveness`` items of kind ``flaky``. A repository no poller
        manifest declares quarantines nothing.
        """
        repository = str(repository or "").lower()
        label = self._labels(repository) if repository else ""
        if not label:
            return []
        added: list[str] = []
        for item in tests:
            test = str(item.get("test") or "")
            # Only an id that names one test: a collection error names a whole file, and
            # leaving that out would drop every test in it from the gate.
            if "::" not in test or _key(repository, test) in self._records:
                continue
            self._records[_key(repository, test)] = {
                "repository": repository,
                "test": test,
                "label": label,
                "since": datetime.fromtimestamp(self._clock(), UTC).isoformat(),
                "verification": verification,
                "commit": str(item.get("commit") or ""),
                "job_id": str(item.get("job_id") or ""),
                "issue": None,
                "opening_at": None,
            }
            added.append(test)
        if added:
            self._save()
            self._wake.set()
        return added

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("quarantine: sweep failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=SWEEP_S)

    async def sweep(self) -> None:
        """File every missing fix issue and release every test whose issue closed."""
        for key in list(self._records):
            record = self._records.get(key)
            if record is None:
                continue
            if isinstance(record.get("issue"), int):
                await self._release_if_closed(key, record)
            else:
                await self._file(key, record)

    async def _release_if_closed(self, key: str, record: dict) -> None:
        repository = str(record["repository"])
        try:
            code, out = await gh_cli.run(
                [
                    _gh(),
                    "issue",
                    "view",
                    str(record["issue"]),
                    "--repo",
                    repository,
                    "--json",
                    "state",
                ],
                timeout=GH_TIMEOUT_S,
                repo=repository,
            )
        except (OSError, TimeoutError) as exc:
            log.info("quarantine: cannot read %s#%s: %s", repository, record["issue"], exc)
            return
        if code != 0:
            return
        try:
            state = str(json.loads(out or b"{}").get("state") or "")
        except (ValueError, AttributeError):
            return
        if state.upper() == "CLOSED":
            self._records.pop(key, None)
            self._save()
            log.info(
                "quarantine: %s#%s closed; %s is back in the gate",
                repository,
                record["issue"],
                record["test"],
            )

    async def _file(self, key: str, record: dict) -> None:
        repository = str(record["repository"])
        marker = MARKER.format(repository=repository, test=record["test"])
        if record.get("opening_at"):
            found, conclusive = await self._find_marked(
                repository, marker, since=str(record["opening_at"])
            )
            if found is not None:
                record["issue"] = found
                record["opening_at"] = None
                self._save()
                return
            if not conclusive:
                return  # unknown is not absent: filing now could make a second issue
        record["opening_at"] = datetime.fromtimestamp(self._clock(), UTC).isoformat()
        if not self._save():
            return
        argv = [
            _gh(),
            "issue",
            "create",
            "--repo",
            repository,
            "--title",
            title(record["test"]),
            "--body-file",
            "-",
            "--label",
            str(record.get("label") or ""),
        ]
        try:
            code, stdout, stderr = await gh_cli.run_once(
                argv, timeout=GH_TIMEOUT_S, stdin=body(record).encode("utf-8"), repo=repository
            )
        except gh_cli.GitHubRefusal as exc:
            log.warning("quarantine: may not file for %s: %s", repository, exc)
            record["opening_at"] = None  # refused before anything ran
            self._save()
            return
        except (OSError, TimeoutError) as exc:
            log.warning("quarantine: filing for %s unanswered: %s", record["test"], exc)
            return
        message = stderr.decode("utf-8", "replace").strip()
        if code != 0:
            if _DEFINITE_REFUSAL.search(message):
                record["opening_at"] = None
                self._save()
            log.warning("quarantine: filing for %s failed: %s", record["test"], message)
            return
        found = _ISSUE_URL.search((stdout or b"").decode("utf-8", "replace"))
        if found is None:
            return  # created or not, the next sweep finds it by its marker
        record["issue"] = int(found.group(1))
        record["opening_at"] = None
        self._save()
        log.info("quarantine: filed %s#%s for %s", repository, record["issue"], record["test"])

    async def _find_marked(
        self, repository: str, marker: str, *, since: str
    ) -> tuple[int | None, bool]:
        """The issue carrying ``marker``, and whether its absence was established.

        A listing rather than a search, whose index lags creation; and absence only counts
        when the page reaches back past the intent (as ``poller_engine.github``).
        """
        try:
            code, out = await gh_cli.run(
                [
                    _gh(),
                    "issue",
                    "list",
                    "--repo",
                    repository,
                    "--state",
                    "all",
                    "--limit",
                    str(MARKED_SCAN),
                    "--json",
                    "number,body,createdAt",
                ],
                timeout=GH_TIMEOUT_S,
                repo=repository,
            )
        except (OSError, TimeoutError):
            return None, False
        if code != 0:
            return None, False
        try:
            issues = json.loads(out or b"[]")
        except ValueError:
            return None, False
        if not isinstance(issues, list):
            return None, False
        reached_back = not issues
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if marker in str(issue.get("body") or ""):
                with contextlib.suppress(TypeError, ValueError):
                    return int(issue.get("number")), True
            if str(issue.get("createdAt") or "") and _before(str(issue["createdAt"]), since):
                reached_back = True
        return None, reached_back


def _before(created: str, since: str) -> bool:
    try:
        first = datetime.fromisoformat(created.replace("Z", "+00:00"))
        second = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return False
    return first < second
