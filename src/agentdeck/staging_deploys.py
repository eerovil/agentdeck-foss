"""Say when a Work's change is on its branch's staging copy and ready to test (#3180).

A consumer of the raw webhook streams. A project that deploys each branch to its own
staging copy records the deploy as a GitHub deployment whose environment names the copy
(for example a Drone pipeline's ``staging-<slug>``). Every ``deployment_status`` delivery
is kept as one
small row per commit in the shared GitHub metadata cache: each environment that commit was
deployed to, with the newest status seen there. No GitHub read, ever: a commit this has not
seen a deployment of is simply not on staging as far as the deck knows.

* **Its own consumer name.** A new name starts at offset 0, so the first start replays the
  live streams already on disk and a deploy that finished before this shipped still counts.
* **The status id is the version.** GitHub deployment status ids only grow, so the larger id
  wins whatever order deliveries arrive in; two statuses can share an ``updated_at`` second.
* **Only the head counts.** ``staging_copy`` reads the row of each open pull request's
  current head, so a newer push takes "ready to test" away until that commit is deployed.

Which environments are a staging copy, and the copy's addresses, come from the project's
manifest (``[staging]``, read by ``poller_ops.release_policies``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .github_cache import GitHubMetadata, GitHubMetadataCache
from .webhook_tailer import Delivery, WebhookTailer, WebhookWatcher

log = logging.getLogger(__name__)

CONSUMER = "staging-deploys"

# ``<owner/repo lowercased>@<sha lowercased>``.
_KEY = "staging-deploy:v1:{repository}@{sha}"


def cache_key(repository: str, sha: str) -> str:
    return _KEY.format(repository=repository.lower(), sha=sha.lower())


@dataclass(frozen=True)
class StagingSite:
    """A project's staging copies, from its manifest's ``[staging]`` table.

    ``prefix`` starts the GitHub deployment environment of every copy; the rest of the
    environment's name is the copy's slug. ``links`` are ``(label, url template)`` pairs whose
    ``{slug}`` is filled with it.
    """

    prefix: str
    links: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class StagingCopy:
    """The staging copy that runs the head of every open pull request of a Work."""

    slug: str
    links: tuple[tuple[str, str], ...] = ()


def _status_id(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class StagingDeployService:
    """Tail the streams and keep each commit's deployments. Web-process owned."""

    def __init__(
        self,
        streams_root: Path | str,
        *,
        cache: GitHubMetadataCache | None = None,
        enabled: bool = True,
        watcher: WebhookWatcher | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._cache = cache or GitHubMetadataCache()
        self._tailer = WebhookTailer(streams_root, consumer=CONSUMER)
        # The web process's one shared watch (#2865); None opens a private one.
        self._watcher = watcher
        # Says only "the Work cards should look again" when a stored state moved.
        self.on_change = on_change
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="staging-deploys")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            try:
                async for batch in self._tailer.watch(stop_event=self._stop, watcher=self._watcher):
                    if self.apply_batch(batch) and self.on_change is not None:
                        self.on_change()
                    self._tailer.commit()
                return  # the stop event ended the watch
            except asyncio.CancelledError:
                raise
            except Exception:
                # A missing "ready to test" is the whole cost of this failing, so keep going.
                log.exception("staging deploy pass failed; retrying")
                await asyncio.sleep(5)

    def apply_batch(self, batch: Iterable[Delivery]) -> bool:
        """Apply every delivery; True when any stored state changed."""
        changed = False
        for delivery in batch:
            changed = self.apply(delivery) or changed
        return changed

    def apply(self, delivery: Delivery) -> bool:
        """Apply one delivery. Idempotent, and indifferent to arrival order."""
        if delivery.event != "deployment_status":
            return False
        payload = delivery.payload
        status = payload.get("deployment_status")
        deployment = payload.get("deployment")
        if not isinstance(status, dict) or not isinstance(deployment, dict):
            return False
        sha = deployment.get("sha")
        environment = status.get("environment") or deployment.get("environment")
        state = status.get("state")
        status_id = _status_id(status.get("id"))
        if not (
            isinstance(sha, str)
            and sha
            and isinstance(environment, str)
            and environment
            and isinstance(state, str)
            and status_id is not None
        ):
            return False
        key = cache_key(delivery.stream, sha)
        held = self._cache.peek(key).value
        environments = dict(held.get("environments") or {}) if isinstance(held, dict) else {}
        current = environments.get(environment)
        if isinstance(current, dict) and (_status_id(current.get("id")) or 0) >= status_id:
            return False
        environments[environment] = {"id": status_id, "state": state}
        # Terminal: nothing ages this out, and no fetch exists to refresh it.
        self._cache.offer(key, GitHubMetadata({"environments": environments}, terminal=True))
        return True

    def lookup(self, heads: Iterable[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        """Each ``(repository, sha)``'s environments, by environment name, where known."""
        wanted = {cache_key(repository, sha): (repository, sha) for repository, sha in heads}
        if not wanted:
            return {}
        found = {}
        for key, value in self._cache.peek_values(wanted).items():
            environments = value.get("environments") if isinstance(value, dict) else None
            if isinstance(environments, dict):
                found[wanted[key]] = environments
        return found


def staging_copy(
    outputs: Sequence[Any],
    sites: Mapping[str, StagingSite],
    deployed: Mapping[tuple[str, str], Mapping[str, Any]],
) -> StagingCopy | None:
    """The copy running every open output's head, or None (#3180).

    ``sites`` is each repository's ``StagingSite`` by lowercased ``owner/repo``; ``deployed``
    is ``StagingDeployService.lookup``'s answer. Only open pull requests in a repository with
    a site take part, and each must have its exact head deployed with success to one copy
    they all share: a Work whose shop and ledger changes landed in different copies, or one
    of them not yet, is not ready to test.
    """
    common: set[str] | None = None
    site: StagingSite | None = None
    for output in outputs:
        if output.state != "open":
            continue
        found = sites.get(output.repository.lower())
        if found is None:
            continue
        environments = deployed.get((output.repository, output.head)) if output.head else None
        here = {
            name[len(found.prefix) :]
            for name, status in (environments or {}).items()
            if name.startswith(found.prefix)
            and len(name) > len(found.prefix)
            and isinstance(status, Mapping)
            and status.get("state") == "success"
        }
        common = here if common is None else common & here
        if not common:
            return None
        site = site or found
    if site is None or not common:
        return None
    # A head deployed to several copies leaves more than one in common; any of them runs
    # every head, so the choice only has to be stable.
    slug = min(common)
    return StagingCopy(
        slug, tuple((label, url.replace("{slug}", slug)) for label, url in site.links)
    )
