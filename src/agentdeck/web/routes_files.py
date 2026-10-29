"""Open host files from absolute-path links rendered in chat transcripts.

An ``.html``/``.htm`` link renders: whatever form the link took, the file is
shown as a sandboxed preview rather than as escaped source. ``?raw=1`` or a
trailing ``:line`` asks for the source instead, the same way ``.md`` links do.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import mimetypes
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from .deps import get_templates, require_access, resolve_session
from .uploads import is_private_upload_path

router = APIRouter(dependencies=[Depends(require_access)])

_LINE_SUFFIX = re.compile(r"^(?P<path>.+):(?P<line>[1-9]\d*)(?::[1-9]\d*)?$")
_MAX_RENDERED_TEXT_BYTES = 5 * 1024 * 1024
_PREVIEW_ENTRY_SUFFIXES = {".htm", ".html"}
_PREVIEW_CSP = "; ".join(
    (
        "sandbox allow-scripts",
        "default-src 'none'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "media-src 'self' data: blob:",
        "connect-src 'none'",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "frame-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'self'",
    )
)
_PREVIEW_WRAPPER_CSP = "; ".join(
    (
        "default-src 'none'",
        "style-src 'unsafe-inline'",
        "frame-src 'self'",
        "object-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)
_PREVIEW_MEDIA_TYPES = {
    ".avif": "image/avif",
    ".css": "text/css",
    ".gif": "image/gif",
    ".htm": "text/html",
    ".html": "text/html",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript",
    ".json": "application/json",
    ".mjs": "text/javascript",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".otf": "font/otf",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".txt": "text/plain",
    ".wasm": "application/wasm",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
_TEXT_MEDIA_TYPES = {
    "application/javascript",
    "application/json",
    "application/ld+json",
    "application/sql",
    "application/toml",
    "application/xhtml+xml",
    "application/xml",
    "image/svg+xml",
}


def _candidate(local_path: str) -> Path:
    """Map the URL catch-all back to the absolute host path it represents."""
    return Path("/") / local_path


def _resolve_file(local_path: str) -> tuple[Path, int | None]:
    """Prefer an exact filename, then interpret a final ``:line[:column]``."""
    exact = _candidate(local_path)
    if exact.is_file():
        return exact, None

    match = _LINE_SUFFIX.fullmatch(local_path)
    if match:
        without_line = _candidate(match.group("path"))
        if without_line.is_file():
            return without_line, int(match.group("line"))

    raise HTTPException(status_code=404, detail="local file not found")


def _is_text_file(path: Path, media_type: str | None) -> bool:
    if media_type and (media_type.startswith("text/") or media_type in _TEXT_MEDIA_TYPES):
        return True
    return path.suffix.lower() in {
        ".cfg",
        ".conf",
        ".env",
        ".ini",
        ".jsonl",
        ".log",
        ".md",
        ".markdown",
        ".py",
        ".rst",
        ".sh",
        ".toml",
        ".yaml",
        ".yml",
    }


def _preview_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="preview file not found")


def _is_preview_part(part: str) -> bool:
    """One safe path component inside a preview bundle: no escape, no dotfile."""
    return bool(part) and not part.startswith(".")


def _preview_parts(relative_path: str) -> tuple[str, ...]:
    if "\\" in relative_path:
        raise _preview_not_found()
    path = Path(relative_path)
    parts = tuple(part for part in path.parts if part != ".")
    if path.is_absolute() or not parts or not all(map(_is_preview_part, parts)):
        raise _preview_not_found()
    return parts


def _open_directory(path: Path) -> int:
    """Open an absolute directory path without following any symlink component."""
    if not path.is_absolute():
        raise _preview_not_found()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError:
        os.close(fd)
        raise


def _directory_identity(directory: Path | None) -> tuple[str, int, int]:
    """Identify a preview root by path plus device/inode, so replacing the
    directory a capability was minted against invalidates that capability."""
    if directory is None:
        raise _preview_not_found()
    directory_fd: int | None = None
    try:
        directory_fd = _open_directory(directory)
        directory_stat = os.fstat(directory_fd)
        return str(directory), directory_stat.st_dev, directory_stat.st_ino
    except OSError as exc:
        raise _preview_not_found() from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _open_preview_file(
    root: Path | None,
    parts: tuple[str, ...],
    *,
    expected_root: tuple[str, int, int] | None = None,
) -> tuple[int, os.stat_result, tuple[str, int, int]]:
    """Descriptor-anchored open that rejects symlinks and non-regular files."""
    if root is None:
        raise _preview_not_found()
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = _open_directory(root)
        directory_stat = os.fstat(directory_fd)
        actual_root = str(root), directory_stat.st_dev, directory_stat.st_ino
        if expected_root is not None and actual_root != expected_root:
            raise OSError("preview root changed")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("preview target is not a regular file")
        return file_fd, file_stat, actual_root
    except (OSError, RuntimeError, ValueError) as exc:
        if file_fd is not None:
            os.close(file_fd)
        raise _preview_not_found() from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _token_payload(root_identity: tuple[str, int, int]) -> bytes:
    root_path, device, inode = root_identity
    return b"\0".join(
        (root_path.encode(), str(device).encode(), str(inode).encode())
    )


def _preview_token(request: Request, root_identity: tuple[str, int, int]) -> str:
    """Mint an unguessable capability for one preview bundle directory.

    Assets are served cross-origin-readable to the sandboxed opaque origin, so
    the URL itself is the only thing keeping an unrelated page from reading host
    files through it.
    """
    encoded_root = (
        base64.urlsafe_b64encode(root_identity[0].encode()).rstrip(b"=").decode()
    )
    signature = hmac.new(
        request.app.state.preview_secret,
        _token_payload(root_identity),
        hashlib.sha256,
    ).digest()[:18]
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{encoded_root}.{encoded_signature}"


def _preview_root(request: Request, token: str) -> tuple[Path, tuple[str, int, int]]:
    try:
        encoded_root, encoded_signature = token.split(".", 1)
        root = Path(
            base64.urlsafe_b64decode(
                encoded_root + "=" * (-len(encoded_root) % 4)
            ).decode()
        )
        supplied = base64.urlsafe_b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4)
        )
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise _preview_not_found() from exc
    if not root.is_absolute():
        raise _preview_not_found()
    root_identity = _directory_identity(root)
    expected = hmac.new(
        request.app.state.preview_secret,
        _token_payload(root_identity),
        hashlib.sha256,
    ).digest()[:18]
    if not hmac.compare_digest(supplied, expected):
        raise _preview_not_found()
    return root, root_identity


def _fd_chunks(fd: int) -> Iterator[bytes]:
    try:
        while chunk := os.read(fd, 64 * 1024):
            yield chunk
    finally:
        os.close(fd)


def _preview_entry(path: Path) -> Path | None:
    """The resolved file an HTML link should render, or ``None`` for source view."""
    if path.suffix.lower() not in _PREVIEW_ENTRY_SUFFIXES:
        return None
    try:
        entry = path.resolve(strict=True)
    except OSError:
        return None
    if entry.parent == entry or not _is_preview_part(entry.name):
        return None
    return entry


def _render_preview(request: Request, entry: Path) -> HTMLResponse:
    """Render the trusted shell that frames one HTML entry point."""
    root_identity = _directory_identity(entry.parent)
    file_fd, _file_stat, _actual_root = _open_preview_file(
        entry.parent, (entry.name,), expected_root=root_identity
    )
    os.close(file_fd)
    preview_src = request.url_for(
        "preview_asset",
        token=_preview_token(request, root_identity),
        asset_path=entry.name,
    )
    response = get_templates(request).TemplateResponse(
        request,
        "preview.html",
        {
            "entry_path": str(entry),
            "preview_src": str(preview_src),
            "source_href": f"{quote(str(entry))}?raw=1",
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Security-Policy"] = _PREVIEW_WRAPPER_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/sessions/{session_key}/preview/{relative_path:path}")
async def session_preview(
    request: Request, session_key: str, relative_path: str
) -> RedirectResponse:
    """Send a cwd-relative preview link to the file's own preview URL.

    Sessions are told to link the absolute path, so this is the compatibility
    path for relative links already sitting in transcripts. Resolving one stays
    contained to the session's working directory; the rendering itself is the
    shared absolute-path one.
    """
    _account, session, _provider = resolve_session(request, session_key)
    if session.cwd is None:
        raise _preview_not_found()
    parts = _preview_parts(relative_path)
    if Path(parts[-1]).suffix.lower() not in _PREVIEW_ENTRY_SUFFIXES:
        raise _preview_not_found()
    file_fd, _file_stat, _root_identity = _open_preview_file(session.cwd, parts)
    os.close(file_fd)
    return RedirectResponse(quote(str(session.cwd.joinpath(*parts))), status_code=307)


@router.get("/file-previews/{token}/{asset_path:path}", name="preview_asset")
async def preview_asset(
    request: Request, token: str, asset_path: str
) -> StreamingResponse:
    """Serve one allowlisted file from the selected preview bundle directory."""
    root, root_identity = _preview_root(request, token)
    asset_parts = _preview_parts(asset_path)
    media_type = _PREVIEW_MEDIA_TYPES.get(Path(asset_parts[-1]).suffix.lower())
    if media_type is None:
        raise _preview_not_found()
    file_fd, file_stat, _actual_root = _open_preview_file(
        root, asset_parts, expected_root=root_identity
    )
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Length": str(file_stat.st_size),
        "X-Content-Type-Options": "nosniff",
        "Access-Control-Allow-Origin": "null",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Content-Security-Policy": _PREVIEW_CSP,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        ),
    }
    return StreamingResponse(_fd_chunks(file_fd), media_type=media_type, headers=headers)


_MERMAID_BLOCK = re.compile(r"^```mermaid[ \t]*\n(.*?)\n```", re.S | re.M)
_BOX_NOTE = re.compile(r"^\s*[-*+]\s+\*\*(.+?)\*\*\s*[:\u2014-]\s*(.+)$", re.M)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# The named colours a diagram may give a box (`class a,b amber`); diagram_frame.js has
# the same hues.
_DIAGRAM_COLOURS = {"blue": 214, "violet": 262, "teal": 174, "amber": 36, "rose": 340, "green": 142}


def _diagram_parts(content: str) -> dict | None:
    """A diagram file's title, summary, first Mermaid block and box notes (#3822)."""
    content = content.replace("\r\n", "\n")
    block = _MERMAID_BLOCK.search(content)
    if block is None:
        return None
    head = content[: block.start()]
    title = next((ln[2:].strip() for ln in head.splitlines() if ln.startswith("# ")), "")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", head) if p.strip()]
    summary = next((p for p in paragraphs if not p.startswith("#")), "")
    notes, legend = {}, []
    for m in _BOX_NOTE.finditer(content):
        text = _MD_LINK.sub(r"\1", m.group(2)).replace("`", "").replace("*", "").strip()
        name = m.group(1).strip().lower()
        # A list item named after one of the diagram colours is the legend.
        if name in _DIAGRAM_COLOURS:
            legend.append({"colour": name, "hue": _DIAGRAM_COLOURS[name], "text": text})
        else:
            notes[re.sub(r"\s+", "", m.group(1)).lower()] = text
    return {
        "title": title,
        "summary": summary,
        "source": block.group(1),
        "notes": notes,
        "legend": legend,
    }


@router.get("/{local_path:path}", response_class=Response)
async def local_file(
    request: Request, local_path: str, raw: bool = False, bare: bool = False
) -> Response:
    """Serve any readable host file; render HTML, and other text without executing it."""
    # This catch-all is registered last and claims every unmatched path, so a
    # mistyped or imagined API route lands here and comes back "local file not
    # found" — which reads as "that session/file is gone" rather than "there is
    # no such endpoint", and has sent more than one agent chasing a data bug that
    # was really a wrong URL. Nothing under /api/ is ever a host file, so say so.
    if local_path == "api" or local_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="no such API route")
    path, line_number = _resolve_file(local_path)
    if is_private_upload_path(path):
        raise HTTPException(status_code=404, detail="local file not found")

    if not raw and line_number is None:
        entry = _preview_entry(path)
        if entry is not None:
            return _render_preview(request, entry)

    media_type, _ = mimetypes.guess_type(path.name)

    try:
        stat = path.stat()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="local file is not readable") from exc
    except OSError as exc:
        raise HTTPException(status_code=404, detail="local file not found") from exc

    if _is_text_file(path, media_type) and stat.st_size <= _MAX_RENDERED_TEXT_BYTES:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="local file is not readable") from exc
        except OSError as exc:
            raise HTTPException(status_code=404, detail="local file not found") from exc

        markdown = (
            path.suffix.lower() in {".md", ".markdown"}
            and not raw
            and (line_number is None or line_number == 1)
        )
        diagram = _diagram_parts(content) if markdown and bare else None
        if diagram is not None:
            response = get_templates(request).TemplateResponse(
                request, "diagram_canvas.html", {"path": path, **diagram}
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        response = get_templates(request).TemplateResponse(
            request,
            "local_file.html",
            {
                "path": path,
                "content": content,
                "lines": content.splitlines(),
                "line_number": line_number,
                "markdown": markdown,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="inline",
        stat_result=stat,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
