"""A Work says when its change is on its branch staging copy and ready to test (#3180).

Deliveries go through `WebhookLog` and `WebhookTailer`, the same appender and reader the
live path uses, so the consumer is asserted over real stream bytes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentdeck.github_cache import GitHubMetadataCache
from agentdeck.github_webhook_log import Repository, WebhookLog
from agentdeck.staging_deploys import (
    StagingCopy,
    StagingDeployService,
    StagingSite,
    staging_copy,
)
from agentdeck.web.work_view import _staging
from agentdeck.webhook_tailer import WebhookTailer
from agentdeck.work_facts import OutputFact, WorkFacts

STORE = Repository(owner="Northwind", name="store")
ABACUS = Repository(owner="Northwind", name="abacus")
HEAD, NEWER, ABACUS_HEAD = "a" * 40, "b" * 40, "c" * 40
ENV = "staging-issue-674-w-ebe57b46"
SITE = StagingSite("staging-", (("Store", "https://{slug}.dev.shop.example/"),))
SITES = {"northwind/store": SITE, "northwind/abacus": SITE}

counter = iter(range(1_000_000))


def deploy_body(
    status_id: int, state: str, *, sha: str = HEAD, env: str = ENV, repository=STORE
) -> bytes:
    return json.dumps(
        {
            "action": "created",
            "deployment_status": {"id": status_id, "state": state, "environment": env},
            "deployment": {"id": 1, "sha": sha, "environment": env},
            "repository": {
                "full_name": repository.slug,
                "name": repository.name,
                "owner": {"login": repository.owner},
            },
        }
    ).encode()


@pytest.fixture
def root(tmp_path):
    return tmp_path / "github-webhooks"


@pytest.fixture
def service(root, tmp_path) -> StagingDeployService:
    return StagingDeployService(root, cache=GitHubMetadataCache(tmp_path / "cache.sqlite3"))


def deliver(root, *bodies: bytes, event: str = "deployment_status") -> None:
    for body in bodies:
        WebhookLog(root).append(
            body=body,
            headers={"X-GitHub-Delivery": f"d-{next(counter)}", "X-GitHub-Event": event},
        )


def apply_stream(root, service) -> bool:
    tailer = WebhookTailer(root, consumer="test-apply")
    changed = service.apply_batch(tailer.read())
    tailer.commit()
    return changed


def output(repository=STORE, head=HEAD, state="open", number=9) -> OutputFact:
    url = f"https://github.com/{repository.slug}/pull/{number}"
    return OutputFact(url, repository.slug, number, "registered", state=state, head=head)


def ready(service, *outputs) -> StagingCopy | None:
    heads = {(item.repository, item.head) for item in outputs}
    return staging_copy(outputs, SITES, service.lookup(heads))


def test_the_newest_status_of_the_exact_head_decides(root, service):
    # In progress, then success; a replayed in-progress arriving last cannot undo it.
    deliver(root, deploy_body(10, "in_progress"), deploy_body(11, "success"))
    deliver(root, deploy_body(10, "in_progress"))
    deliver(root, deploy_body(99, "success"), event="status")
    assert apply_stream(root, service)

    assert ready(service, output()) == StagingCopy(
        "issue-674-w-ebe57b46", (("Store", "https://issue-674-w-ebe57b46.dev.shop.example/"),)
    )
    # A newer push is not on staging until its own deploy succeeds.
    assert ready(service, output(head=NEWER)) is None
    deliver(root, deploy_body(12, "in_progress", sha=NEWER))
    apply_stream(root, service)
    assert ready(service, output(head=NEWER)) is None
    deliver(root, deploy_body(13, "success", sha=NEWER))
    apply_stream(root, service)
    assert ready(service, output(head=NEWER)) is not None
    # Nothing new, nothing to rebuild.
    deliver(root, deploy_body(13, "success", sha=NEWER))
    assert not apply_stream(root, service)


def test_every_open_repository_must_run_its_head_in_one_copy(root, service):
    store, abacus = output(), output(ABACUS, ABACUS_HEAD, number=4)
    deliver(root, deploy_body(11, "success"))
    apply_stream(root, service)
    assert ready(service, store, abacus) is None

    deliver(root, deploy_body(20, "success", sha=ABACUS_HEAD, repository=ABACUS, env="staging-x"))
    apply_stream(root, service)
    assert ready(service, store, abacus) is None  # a different copy

    deliver(root, deploy_body(21, "success", sha=ABACUS_HEAD, repository=ABACUS))
    apply_stream(root, service)
    assert ready(service, store, abacus) is not None
    # A merged output, one in a repository with no copy, and a production deploy do not count.
    merged = output(ABACUS, NEWER, state="merged", number=5)
    other = output(Repository(owner="Northwind", name="soi"), NEWER, number=6)
    assert ready(service, store, merged, other) is not None
    assert ready(service, merged, other) is None
    deliver(root, deploy_body(30, "success", sha=NEWER, env="prod"))
    apply_stream(root, service)
    assert ready(service, output(head=NEWER)) is None
    assert staging_copy([output(head="")], SITES, {}) is None


def test_the_work_facts_carry_the_copy_from_the_manifest_policy(root, service):
    deliver(root, deploy_body(11, "success"))
    apply_stream(root, service)
    policies = {
        "northwind/store": SimpleNamespace(
            staging=SITE, staging_repos=frozenset({"northwind/store"})
        ),
    }
    app = SimpleNamespace(
        state=SimpleNamespace(
            staging_deploys=service,
            work_release=SimpleNamespace(
                ports=SimpleNamespace(
                    policy=lambda repo: policies.get(repo, SimpleNamespace(staging=None))
                )
            ),
        )
    )
    facts = {"w": WorkFacts(outputs=(output(),)), "other": WorkFacts(outputs=(output(head=NEWER),))}

    found = _staging(app, dict(facts))

    assert found["w"].staging.slug == "issue-674-w-ebe57b46"
    assert found["other"] == facts["other"]
    # A repository the manifest's [staging] does not list deploys into no copy.
    policies["northwind/store"] = SimpleNamespace(staging=SITE, staging_repos=())
    assert _staging(app, dict(facts))["w"].staging is None
