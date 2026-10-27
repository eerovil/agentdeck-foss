"""Shared attachment cleanup preserves filesystem and upload lifetimes."""

import asyncio
import importlib.util
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import FormData, Headers, UploadFile

from agentdeck import attachments, inject
from agentdeck.config import InjectConfig
from agentdeck.models import InjectResult
from agentdeck.web import uploads


def test_web_and_injector_share_the_attachment_cleanup_function():
    assert uploads.cleanup_uploaded_files is attachments.cleanup_uploaded_files
    assert inject.cleanup_uploaded_files is attachments.cleanup_uploaded_files


def test_injector_loads_without_the_web_upload_adapter(monkeypatch):
    # Execute the full application module with the web adapter unavailable.
    # The module alias avoids replacing the injector used by other tests.
    spec = importlib.util.spec_from_file_location("agentdeck._inject_without_web", inject.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    monkeypatch.setitem(sys.modules, "agentdeck.web.uploads", None)
    spec.loader.exec_module(module)
    assert module.cleanup_uploaded_files is attachments.cleanup_uploaded_files


@pytest.mark.parametrize("collection", [list, tuple])
def test_cleanup_is_idempotent_and_only_removes_empty_parent_directories(tmp_path, collection):
    empty_after = tmp_path / "first-upload"
    shared = tmp_path / "shared-upload"
    empty_after.mkdir()
    shared.mkdir()
    first, second, untouched = empty_after / "one", shared / "two", shared / "other"
    for path in (first, second, untouched):
        path.write_text("attachment")
    paths = collection((first, first, empty_after / "missing", second))
    uploads.cleanup_uploaded_files(paths)
    uploads.cleanup_uploaded_files(paths)
    uploads.cleanup_uploaded_files(collection())
    assert not first.exists() and not second.exists()
    assert not empty_after.exists()
    assert untouched.read_text() == "attachment"


def test_cleanup_unlinks_symlink_without_deleting_target(tmp_path):
    target = tmp_path / "original"
    target.write_text("keep me")
    directory = tmp_path / "upload"
    directory.mkdir()
    link = directory / "link"
    link.symlink_to(target)
    uploads.cleanup_uploaded_files([link])
    assert not directory.exists()
    assert target.read_text() == "keep me"


def test_unlink_failure_is_not_silently_ignored(tmp_path, monkeypatch):
    path = tmp_path / "denied"
    path.write_text("attachment")
    original = Path.unlink

    def unlink(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic unlink refusal")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(PermissionError, match="synthetic unlink refusal"):
        uploads.cleanup_uploaded_files([path])
    assert path.exists()


def _upload(name, data, content_type):
    return UploadFile(BytesIO(data), filename=name, headers=Headers({"content-type": content_type}))


@pytest.mark.parametrize("images", [False, True])
async def test_failed_upload_cleans_files_already_saved(tmp_path, monkeypatch, images):
    spool = tmp_path / "spool"
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    monkeypatch.setattr(uploads, "GENERIC_UPLOAD_ROOT", spool)
    monkeypatch.setattr(uploads.tempfile, "mkdtemp", lambda **kwargs: str(image_dir))
    parts = (
        [_upload("one.png", b"\x89PNG\r\n\x1a\n", "image/png"),
         _upload("bad.png", b"not png", "image/png")]
        if images else
        [_upload("one.txt", b"small", "text/plain"),
         _upload("large.txt", b"x" * 20, "text/plain")]
    )
    config = SimpleNamespace(
        max_images=2,
        max_image_bytes=10,
        max_image_total_bytes=40,
        max_file_bytes=10,
        max_file_total_bytes=40,
    )
    form = FormData([("images", part) for part in parts])
    try:
        with pytest.raises(uploads.UploadError):
            await uploads.save_uploaded_files(form, config, action_id="upload-cleanup")
        assert not list(tmp_path.rglob("*.upload"))
        assert not list(tmp_path.rglob("*.png"))
        if images:
            assert not image_dir.exists()
        else:
            assert list(spool.iterdir()) == []
    finally:
        await form.close()


@pytest.mark.parametrize("accepted", [False, True])
async def test_real_injector_preserves_generic_file_cleanup_timing(tmp_path, accepted):
    directory = tmp_path / "private-upload"
    directory.mkdir()
    path = directory / "one.upload"
    path.write_text("attachment")
    turn_finished = asyncio.Event()

    async def cursor(*args):
        return 0, 0

    async def steer(*args, **kwargs):
        return InjectResult(accepted)

    async def wait_for_session(*args, **kwargs):
        await turn_finished.wait()
        return InjectResult(True)

    provider = SimpleNamespace(
        transcript_cursor=cursor, steer=steer, wait_for_session=wait_for_session
    )
    config = InjectConfig(enabled=True, timeout_s=1, max_message_chars=10000)
    service = inject.InjectionService(config)
    session = SimpleNamespace(key="codex:test:cleanup", session_id="cleanup")
    try:
        receipt = await service.deliver_now(object(), session, provider, "read", files=[path])
        assert receipt.result.accepted is accepted
        assert path.exists() is accepted
        if accepted:
            turn_finished.set()
            await asyncio.wait_for(asyncio.gather(*tuple(service._cleanup_tasks)), timeout=1)
        assert not directory.exists()
    finally:
        turn_finished.set()
        await service.stop()
