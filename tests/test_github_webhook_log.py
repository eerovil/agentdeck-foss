"""The raw per-repository webhook streams (#1601).

These tests hold the storage contract: one file per repository, the original bytes
recoverable from every record, any event type stored the same way, an unfilable delivery
refused rather than mixed in, and concurrent appends that cannot interleave.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentdeck.github_webhook_log import (
    MAX_BODY_BYTES,
    Repository,
    RepositoryUnknown,
    WebhookLog,
    raw_body,
    repository_of,
    verify_signature,
)
from agentdeck.web.routes_webhook import router as webhook_router

SECRET = "shhh-this-is-the-webhook-secret"


def body_for(full_name: str, **extra) -> bytes:
    owner, _, name = full_name.partition("/")
    payload = {
        "action": "opened",
        "repository": {"full_name": full_name, "name": name, "owner": {"login": owner}},
    }
    payload.update(extra)
    return json.dumps(payload).encode()


@pytest.fixture
def stream(tmp_path) -> WebhookLog:
    return WebhookLog(tmp_path / "github-webhooks")


@pytest.fixture
def client(stream, monkeypatch) -> TestClient:
    monkeypatch.delenv("AGENTDECK_WEBHOOK_SECRET", raising=False)
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.github_webhook_log = stream
    return TestClient(app)


def post(client: TestClient, body: bytes, *, event: str = "issues", delivery: str = "d-1"):
    return client.post(
        "/api/github/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
        },
    )


# --- identifying the repository -------------------------------------------------------


def test_repository_comes_from_full_name():
    assert repository_of(body_for("eerovil/agentdeck")) == Repository("eerovil", "agentdeck")


def test_repository_falls_back_to_owner_and_name():
    body = json.dumps({"repository": {"name": "agentdeck", "owner": {"login": "eerovil"}}}).encode()
    assert repository_of(body) == Repository("eerovil", "agentdeck")


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json at all",
        b"[1, 2, 3]",
        json.dumps({"action": "created"}).encode(),
        json.dumps({"repository": None}).encode(),
        json.dumps({"repository": {"full_name": ""}}).encode(),
        json.dumps({"repository": {"full_name": "../../etc/passwd"}}).encode(),
        json.dumps({"repository": {"name": "..", "owner": {"login": "eerovil"}}}).encode(),
        json.dumps({"repository": {"name": "a/b", "owner": {"login": "eerovil"}}}).encode(),
    ],
)
def test_unfilable_deliveries_are_refused(body):
    with pytest.raises(RepositoryUnknown):
        repository_of(body)


def test_a_path_traversing_name_never_escapes_the_root(stream):
    body = json.dumps({"repository": {"full_name": "../../etc/passwd"}}).encode()
    with pytest.raises(RepositoryUnknown):
        stream.append(body=body, headers={})
    assert not stream.root.exists()


# --- the storage contract -------------------------------------------------------------


def test_each_repository_gets_its_own_file(stream):
    stream.append(body=body_for("owner/repo-a"), headers={"X-GitHub-Delivery": "a"})
    stream.append(body=body_for("owner/repo-b"), headers={"X-GitHub-Delivery": "b"})

    assert len(stream.read(Repository("owner", "repo-a"))) == 1
    assert len(stream.read(Repository("owner", "repo-b"))) == 1
    assert (stream.root / "owner" / "repo-a.jsonl").exists()
    assert (stream.root / "owner" / "repo-b.jsonl").exists()


def test_same_owner_different_repo_never_share_a_file(stream):
    stream.append(body=body_for("owner/repo-a"), headers={})
    stored = stream.read(Repository("owner", "repo-b"))
    assert stored == []


def test_exact_bytes_survive_including_newlines_and_non_utf8_escapes(stream):
    # A payload with embedded newlines and a lone tab is exactly what naive line framing
    # loses. Base64 means the record can never be split by the payload's own content.
    body = b'{"repository": {"full_name": "owner/repo"}, "note": "line one\\nline two\\ttab"}\n'
    stream.append(body=body, headers={})
    records = stream.read(Repository("owner", "repo"))
    assert len(records) == 1
    assert raw_body(records[0]) == body
    assert base64.b64decode(records[0]["raw_body_base64"]) == body


def test_a_stored_line_is_one_line_however_the_payload_is_formatted(stream):
    body = json.dumps(
        {"repository": {"full_name": "owner/repo"}, "body": "a\nb\nc"}, indent=4
    ).encode()
    stream.append(body=body, headers={})
    raw = (stream.root / "owner" / "repo.jsonl").read_text(encoding="utf-8")
    assert raw.count("\n") == 1
    assert raw_body(json.loads(raw)) == body


@pytest.mark.parametrize(
    "event", ["issues", "pull_request", "ping", "workflow_run", "some_event_from_2029", ""]
)
def test_any_event_type_lands_in_the_same_repository_stream(stream, event):
    stream.append(body=body_for("owner/repo"), headers={"X-GitHub-Event": event})
    records = stream.read(Repository("owner", "repo"))
    assert [r["event"] for r in records] == [event]


def test_events_of_every_type_accumulate_in_one_stream(stream):
    for event in ("issues", "pull_request", "an_event_nobody_has_written_yet"):
        stream.append(body=body_for("owner/repo"), headers={"X-GitHub-Event": event})
    records = stream.read(Repository("owner", "repo"))
    assert [r["event"] for r in records] == [
        "issues",
        "pull_request",
        "an_event_nobody_has_written_yet",
    ]


def test_the_envelope_keeps_transport_metadata_but_not_the_signature(stream):
    stream.append(
        body=body_for("owner/repo"),
        headers={
            "X-GitHub-Delivery": "delivery-77",
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": "sha256=deadbeef",
            "User-Agent": "GitHub-Hookshot/abc",
        },
        received_at="2026-09-06T12:00:00+00:00",
    )
    record = stream.read(Repository("owner", "repo"))[0]
    assert record["received_at"] == "2026-09-06T12:00:00+00:00"
    assert record["delivery_id"] == "delivery-77"
    assert record["repository"] == "owner/repo"
    assert record["headers"]["User-Agent"] == "GitHub-Hookshot/abc"
    assert "X-Hub-Signature-256" not in record["headers"]
    assert "deadbeef" not in json.dumps(record)


def test_no_event_specific_field_is_lifted_out_of_the_payload(stream):
    stream.append(body=body_for("owner/repo", issue={"number": 1601, "state": "open"}), headers={})
    record = stream.read(Repository("owner", "repo"))[0]
    # Only framing keys. An "issue_number" here would be stage two leaking into stage one.
    assert set(record) == {
        "received_at",
        "delivery_id",
        "event",
        "repository",
        "headers",
        "body_sha256",
        "raw_body_base64",
    }


def test_stream_files_are_not_world_readable(stream):
    stream.append(body=body_for("owner/repo"), headers={})
    path = stream.root / "owner" / "repo.jsonl"
    assert os.stat(path).st_mode & 0o077 == 0
    assert os.stat(path.parent).st_mode & 0o077 == 0


def test_a_half_written_line_does_not_hide_the_rest(stream):
    stream.append(body=body_for("owner/repo"), headers={"X-GitHub-Delivery": "first"})
    path = stream.root / "owner" / "repo.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"truncated": tru\n')
    stream.append(body=body_for("owner/repo"), headers={"X-GitHub-Delivery": "second"})
    assert [r["delivery_id"] for r in stream.read(Repository("owner", "repo"))] == [
        "first",
        "second",
    ]


# --- concurrency ----------------------------------------------------------------------


def test_concurrent_appends_never_interleave(stream):
    # Bodies far larger than PIPE_BUF, so O_APPEND alone would not save us. Every line
    # must parse and every delivery must appear exactly once.
    deliveries = [f"delivery-{i}" for i in range(24)]
    filler = "x" * 20_000
    barrier = threading.Barrier(len(deliveries))

    def append(delivery: str) -> None:
        body = body_for("owner/repo", filler=filler, delivery=delivery)
        barrier.wait()
        stream.append(body=body, headers={"X-GitHub-Delivery": delivery})

    threads = [threading.Thread(target=append, args=(d,)) for d in deliveries]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    raw = (stream.root / "owner" / "repo.jsonl").read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == len(deliveries)
    parsed = [json.loads(line) for line in lines]  # every line whole
    assert sorted(r["delivery_id"] for r in parsed) == sorted(deliveries)
    for record in parsed:
        assert json.loads(raw_body(record))["delivery"] == record["delivery_id"]


def test_two_repositories_written_concurrently_stay_separate(stream):
    barrier = threading.Barrier(2)

    def append(repo: str) -> None:
        barrier.wait()
        for i in range(20):
            stream.append(body=body_for(repo), headers={"X-GitHub-Delivery": f"{repo}-{i}"})

    threads = [
        threading.Thread(target=append, args=("owner/repo-a",)),
        threading.Thread(target=append, args=("owner/repo-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for repo in ("repo-a", "repo-b"):
        records = stream.read(Repository("owner", repo))
        assert len(records) == 20
        assert {r["repository"] for r in records} == {f"owner/{repo}"}


# --- signature verification -----------------------------------------------------------


def test_signature_verification_matches_githubs_scheme():
    import hashlib
    import hmac

    body = body_for("owner/repo")
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(SECRET, body, good)
    assert not verify_signature(SECRET, body + b" ", good)
    assert not verify_signature(SECRET, body, "sha1=whatever")
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature("", body, good)


# --- the route ------------------------------------------------------------------------


def test_route_stores_one_record_per_delivery(client, stream):
    response = post(client, body_for("owner/repo-a"), delivery="d-a")
    assert response.status_code == 200
    assert response.json()["repository"] == "owner/repo-a"
    assert response.json()["status"] == "stored"
    assert len(stream.read(Repository("owner", "repo-a"))) == 1


def test_route_separates_repositories(client, stream):
    post(client, body_for("owner/repo-a"), delivery="d-a")
    post(client, body_for("owner/repo-b"), delivery="d-b")
    assert len(stream.read(Repository("owner", "repo-a"))) == 1
    assert len(stream.read(Repository("owner", "repo-b"))) == 1


def test_route_stores_an_event_type_it_has_never_heard_of(client, stream):
    post(client, body_for("owner/repo"), event="a_future_github_event", delivery="d-x")
    records = stream.read(Repository("owner", "repo"))
    assert [r["event"] for r in records] == ["a_future_github_event"]


def test_route_preserves_exact_bytes(client, stream):
    body = b'{"repository": {"full_name": "owner/repo"}, "text": "one\\ntwo"}   '
    post(client, body)
    assert raw_body(stream.read(Repository("owner", "repo"))[0]) == body


def test_route_refuses_a_delivery_without_a_repository(client, stream):
    response = post(client, json.dumps({"zen": "keep it logically awesome"}).encode())
    assert response.status_code == 400
    assert not stream.root.exists()


def test_route_survives_malformed_input(client, stream):
    for body in (b"", b"<html>nope</html>", b"\xff\xfe\x00"):
        assert post(client, body).status_code == 400
    # Still serving afterwards.
    assert post(client, body_for("owner/repo")).status_code == 200


def test_route_refuses_an_oversized_body_by_declared_length(client, stream):
    response = client.post(
        "/api/github/webhook",
        content=b"{}",
        headers={"Content-Length": str(MAX_BODY_BYTES + 1), "X-GitHub-Event": "issues"},
    )
    assert response.status_code == 413


def test_route_refuses_an_oversized_body_it_actually_receives(client, stream):
    body = body_for("owner/repo", filler="x" * (MAX_BODY_BYTES + 100))
    assert post(client, body).status_code == 413
    assert not stream.root.exists()


def test_route_keeps_a_delivery_larger_than_a_mebibyte(client, stream):
    # GitHub delivers payloads well past a mebibyte — a big push or a release with long
    # release notes. Refusing one would drop a real event from the stream, so the bound
    # is GitHub's own 25 MB and not a guess about what the deck subscribes to.
    body = body_for("owner/repo", filler="x" * (2 * 1024 * 1024))
    assert len(body) > 1_048_576
    assert post(client, body).status_code == 200
    records = stream.read(Repository("owner", "repo"))
    assert len(records) == 1
    assert raw_body(records[0]) == body


def test_a_short_write_never_leaves_a_truncated_record(stream, monkeypatch):
    # os.write may return fewer bytes than it was given, and these records run to
    # megabytes. If the append trusted one call, the file would hold half a line and the
    # body would no longer decode — while the delivery had been reported as stored.
    real_write = os.write

    def dribble(fd, data):
        return real_write(fd, data[:1])

    monkeypatch.setattr("agentdeck.github_webhook_log.os.write", dribble)
    body = body_for("owner/repo", filler="payload that must survive one byte at a time")
    stream.append(body=body, headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "d"})

    text = stream.path_for(Repository("owner", "repo")).read_text()
    assert text.count("\n") == 1
    records = stream.read(Repository("owner", "repo"))
    assert len(records) == 1
    assert raw_body(records[0]) == body


def test_route_verifies_the_signature_when_a_secret_is_set(client, stream, monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setenv("AGENTDECK_WEBHOOK_SECRET", SECRET)
    body = body_for("owner/repo")

    bad = client.post(
        "/api/github/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=nope"},
    )
    assert bad.status_code == 401
    assert not stream.root.exists()

    good = client.post(
        "/api/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": "sha256="
            + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        },
    )
    assert good.status_code == 200
    assert len(stream.read(Repository("owner", "repo"))) == 1


def test_route_reports_when_no_log_is_configured(stream):
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.github_webhook_log = None
    response = TestClient(app).post("/api/github/webhook", content=body_for("owner/repo"))
    assert response.status_code == 503


def test_receiving_a_webhook_wakes_nothing(client, stream, monkeypatch):
    # The card's hard boundary: storing a delivery must not reach the poller. If any of
    # these ever gains a caller from this path, this test is the thing that says so.
    import agentdeck.github_webhook_log as module

    for forbidden in ("subprocess", "httpx", "requests"):
        assert forbidden not in dir(module)
    response = post(client, body_for("owner/repo"))
    assert response.status_code == 200
    assert set(response.json()) == {"status", "delivery", "event", "repository", "bytes", "path"}


# --- rotation (#1622) -----------------------------------------------------------------


@pytest.fixture
def rotating(tmp_path) -> WebhookLog:
    # Thresholds small enough that a handful of ordinary deliveries cross them.
    return WebhookLog(tmp_path / "github-webhooks", rotate_bytes=4_000, keep_rotations=3)


def fill(log: WebhookLog, repo: str, count: int, *, filler: int = 1_000) -> None:
    for i in range(count):
        log.append(
            body=body_for(repo, filler="x" * filler, delivery=f"d-{i}"),
            headers={"X-GitHub-Delivery": f"d-{i}"},
        )


def archived_records(path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as archive:
        return [json.loads(line) for line in archive if line.strip()]


def test_a_stream_below_the_threshold_never_rotates(rotating):
    fill(rotating, "owner/repo", 1)
    assert rotating.rotations(Repository("owner", "repo")) == []


def test_a_full_stream_is_archived_and_the_live_file_starts_again(rotating):
    repo = Repository("owner", "repo")
    fill(rotating, "owner/repo", 3)  # enough to cross the threshold exactly once

    live = rotating.path_for(repo)
    assert live.stat().st_size < rotating.rotate_bytes
    assert rotating.rotations(repo) == [rotating.rotation_path(repo, 1)]


def test_rotation_loses_no_delivery(tmp_path):
    # Room for every rotation these deliveries produce, so anything missing is a bug
    # rather than the retention limit doing its job.
    log = WebhookLog(tmp_path / "github-webhooks", rotate_bytes=4_000, keep_rotations=50)
    repo = Repository("owner", "repo")
    fill(log, "owner/repo", 30)

    stored = [r["delivery_id"] for r in log.read(repo)]
    for archive in log.rotations(repo):
        stored += [r["delivery_id"] for r in archived_records(archive)]
    assert sorted(stored) == sorted(f"d-{i}" for i in range(30))


def test_an_archived_delivery_keeps_its_exact_bytes(rotating):
    repo = Repository("owner", "repo")
    fill(rotating, "owner/repo", 30)

    archived = archived_records(rotating.rotation_path(repo, 1))
    assert archived
    for record in archived:
        assert json.loads(raw_body(record))["delivery"] == record["delivery_id"]


def test_only_the_configured_number_of_archives_is_kept(rotating):
    repo = Repository("owner", "repo")
    fill(rotating, "owner/repo", 200)

    kept = rotating.rotations(repo)
    assert len(kept) == rotating.keep_rotations
    assert not rotating.rotation_path(repo, rotating.keep_rotations + 1).exists()


def test_the_newest_archive_is_the_most_recent_one(rotating):
    repo = Repository("owner", "repo")
    fill(rotating, "owner/repo", 200)

    newest = [r["delivery_id"] for r in archived_records(rotating.rotation_path(repo, 1))]
    oldest = [r["delivery_id"] for r in archived_records(rotating.rotation_path(repo, 3))]
    order = lambda ids: max(int(i.removeprefix("d-")) for i in ids)  # noqa: E731
    assert order(newest) > order(oldest)


def test_an_archive_is_not_world_readable(rotating):
    repo = Repository("owner", "repo")
    fill(rotating, "owner/repo", 30)
    for archive in rotating.rotations(repo):
        assert archive.stat().st_mode & 0o077 == 0


def test_rotation_leaves_no_staging_file_behind(rotating):
    repo = Repository("owner", "repo")
    fill(rotating, "owner/repo", 30)
    leftovers = list(rotating.path_for(repo).parent.glob("*.rotating.gz"))
    assert leftovers == []


def test_each_repository_rotates_on_its_own(rotating):
    fill(rotating, "owner/busy", 30)
    fill(rotating, "owner/quiet", 1)

    assert rotating.rotations(Repository("owner", "busy"))
    assert rotating.rotations(Repository("owner", "quiet")) == []


def test_rotation_can_be_turned_off(tmp_path):
    log = WebhookLog(tmp_path / "github-webhooks", rotate_bytes=0)
    repo = Repository("owner", "repo")
    fill(log, "owner/repo", 40)

    assert log.rotations(repo) == []
    assert len(log.read(repo)) == 40


def test_keeping_no_rotations_discards_the_full_stream(tmp_path):
    log = WebhookLog(tmp_path / "github-webhooks", rotate_bytes=4_000, keep_rotations=0)
    repo = Repository("owner", "repo")
    fill(log, "owner/repo", 30)

    assert log.rotations(repo) == []
    assert list(log.path_for(repo).parent.glob("*.gz")) == []
    # The live stream is still bounded, which is the point of the setting.
    assert log.path_for(repo).stat().st_size < log.rotate_bytes


def test_a_delivery_waiting_on_the_lock_lands_in_the_new_stream(tmp_path):
    # The race rotation has to survive: while one append is archiving and emptying the
    # file, others are blocked on the same lock. Every one of them must end up in the
    # live stream or an archive, never in a file that was rotated away underneath it.
    # Retention is generous here so the only thing under test is the race.
    rotating = WebhookLog(tmp_path / "github-webhooks", rotate_bytes=4_000, keep_rotations=50)
    repo = Repository("owner", "repo")
    deliveries = [f"d-{i}" for i in range(40)]
    barrier = threading.Barrier(len(deliveries))

    def append(delivery: str) -> None:
        body = body_for("owner/repo", filler="x" * 1_000, delivery=delivery)
        barrier.wait()
        rotating.append(body=body, headers={"X-GitHub-Delivery": delivery})

    threads = [threading.Thread(target=append, args=(d,)) for d in deliveries]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stored = [r["delivery_id"] for r in rotating.read(repo)]
    for archive in rotating.rotations(repo):
        stored += [r["delivery_id"] for r in archived_records(archive)]
    assert sorted(stored) == sorted(deliveries)


def test_the_route_rotates_too(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTDECK_WEBHOOK_SECRET", raising=False)
    log = WebhookLog(tmp_path / "github-webhooks", rotate_bytes=4_000, keep_rotations=2)
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.github_webhook_log = log
    client = TestClient(app)

    for i in range(20):
        response = post(client, body_for("owner/repo", filler="x" * 1_000), delivery=f"d-{i}")
        assert response.status_code == 200

    repo = Repository("owner", "repo")
    assert log.rotations(repo)
    assert log.path_for(repo).stat().st_size < log.rotate_bytes
