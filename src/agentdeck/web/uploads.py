"""Validated private uploads for agent turns."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from starlette.datastructures import FormData, UploadFile

from ..attachments import cleanup_uploaded_files
from ..config import InjectConfig
from ..images import sniff_suffix, suffix_for_media_type

GENERIC_UPLOAD_ROOT = Path(tempfile.gettempdir()) / f"agentdeck-file-uploads-{os.getuid()}"
GENERIC_UPLOAD_TTL_S = 24 * 60 * 60
GENERIC_UPLOAD_SWEEP_INTERVAL_S = 60 * 60
MAX_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


class UploadError(ValueError):
    """A turn attachment failed validation."""


@dataclass(frozen=True)
class UploadedFile:
    """A generic upload's opaque storage path and safe display name."""

    path: Path
    name: str


@dataclass(frozen=True)
class SavedUploads:
    """One form's native images and path-addressed files."""

    images: tuple[Path, ...] = ()
    files: tuple[UploadedFile, ...] = ()

    @property
    def paths(self) -> tuple[Path, ...]:
        return self.images + tuple(upload.path for upload in self.files)


def _safe_file_name(name: str) -> str:
    """A readable basename with traversal and control characters removed."""
    basename = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = "".join(
        char if char.isprintable() and char not in "\x00/" else "_"
        for char in basename
    )
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "attachment"
    # Leave room for the numeric prefix on filesystems with a 255-byte component cap.
    encoded = cleaned.encode("utf-8")[:200]
    while True:
        try:
            cleaned = encoded.decode("utf-8")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return cleaned


def sweep_abandoned_uploads(now: float | None = None) -> None:
    """Remove generic upload leases left behind by a dead web process."""
    try:
        children = tuple(GENERIC_UPLOAD_ROOT.iterdir())
    except OSError:
        return
    cutoff = (time.time() if now is None else now) - GENERIC_UPLOAD_TTL_S
    for child in children:
        try:
            if child.is_symlink() or not child.is_dir() or child.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(child)
        except OSError:
            continue


async def sweep_abandoned_uploads_periodically() -> None:
    """Apply the hard upload TTL even when no later upload or restart occurs."""
    while True:
        await asyncio.sleep(GENERIC_UPLOAD_SWEEP_INTERVAL_S)
        await asyncio.to_thread(sweep_abandoned_uploads)


def _same_file_digest(path: Path, digest: str) -> bool:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest() == digest


def _save_generic_file(
    source: BinaryIO, action_id: str, index: int, max_bytes: int
) -> tuple[Path, int]:
    """Stream one retry-stable, opaque, private generic attachment to disk.

    A file may be a hundred megabytes (#3374), so it is copied in chunks rather than
    read whole; it gets its final, content-addressed name only once it is complete.
    """
    GENERIC_UPLOAD_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(GENERIC_UPLOAD_ROOT, 0o700)
    lease = GENERIC_UPLOAD_ROOT / hashlib.sha256(action_id.encode()).hexdigest()
    lease.mkdir(mode=0o700, exist_ok=True)
    fd, partial_name = tempfile.mkstemp(dir=lease, prefix=".partial-")
    partial = Path(partial_name)
    try:
        hasher = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as handle:
            while chunk := source.read(_COPY_CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadError("file is too large")
                hasher.update(chunk)
                handle.write(chunk)
        digest = hasher.hexdigest()
        path = lease / f"{index + 1}-{digest[:24]}.upload"
        try:
            os.link(partial, path)
        except FileExistsError:
            if path.is_symlink() or not _same_file_digest(path, digest):
                raise UploadError("attachment retry does not match its earlier upload") from None
        return path, size
    except BaseException:
        partial.unlink(missing_ok=True)
        try:
            lease.rmdir()
        except OSError:
            pass
        raise
    finally:
        partial.unlink(missing_ok=True)


def is_private_upload_path(path: Path) -> bool:
    """Whether a host-file request targets AgentDeck's private generic spool."""
    try:
        return path.resolve().is_relative_to(GENERIC_UPLOAD_ROOT.resolve())
    except OSError:
        return False


async def save_uploaded_files(
    form: FormData, config: InjectConfig, *, action_id: str
) -> SavedUploads:
    """Validate and save native images plus arbitrary path-addressed files."""
    file_parts = [
        (name, item)
        for name, item in form.multi_items()
        if isinstance(item, UploadFile)
    ]
    if any(name != "images" for name, _upload in file_parts):
        for _name, upload in file_parts:
            await upload.close()
        raise UploadError("unexpected file field")
    uploads = [upload for _name, upload in file_parts if upload.filename]
    for _name, upload in file_parts:
        if not upload.filename:
            await upload.close()
    if len(uploads) > config.max_images:
        for upload in uploads:
            await upload.close()
        raise UploadError("too many files")

    images: list[Path] = []
    files: list[UploadedFile] = []
    total = 0
    image_total = 0
    directory: Path | None = None
    try:
        for index, upload in enumerate(uploads):
            expected = suffix_for_media_type(upload.content_type or "")
            if expected is None:
                try:
                    sweep_abandoned_uploads()
                    path, size = await asyncio.to_thread(
                        _save_generic_file,
                        upload.file,
                        action_id,
                        index,
                        config.max_file_bytes,
                    )
                finally:
                    await upload.close()
                files.append(
                    UploadedFile(path, _safe_file_name(upload.filename or "attachment"))
                )
                total += size
                if total > config.max_file_total_bytes:
                    raise UploadError("files are too large in total")
                continue
            try:
                data = await upload.read(config.max_image_bytes + 1)
            finally:
                await upload.close()
            if len(data) > config.max_image_bytes:
                raise UploadError("file is too large")
            total += len(data)
            image_total += len(data)
            if image_total > config.max_image_total_bytes or total > config.max_file_total_bytes:
                raise UploadError("files are too large in total")
            if directory is None:
                directory = Path(tempfile.mkdtemp(prefix="agentdeck-images-"))
            extension = sniff_suffix(data)
            if extension is None or extension != expected:
                raise UploadError("uploaded file is not the declared image type")
            path = directory / f"{secrets.token_hex(16)}{extension}"
            images.append(path)
            path.write_bytes(data)
    except BaseException:
        cleanup_uploaded_files([*images, *(upload.path for upload in files)])
        if directory is not None:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return SavedUploads(tuple(images), tuple(files))
