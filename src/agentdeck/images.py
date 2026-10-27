"""Single source of truth for the image formats AgentDeck supports.

Every supported-set membership test, suffix<->media-type conversion, and
content sniff derives from ONE table, so the accepted formats cannot drift
apart across the upload, image-serving, inject-tracking, transcript-rendering,
and Claude-worker paths that each used to keep their own copy.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageFormat:
    """One supported raster image format."""

    media_type: str
    suffix: str  # the canonical suffix an accepted upload is stored under
    matches: Callable[[bytes], bool]  # true when bytes carry this format's signature
    encoder: str  # the Pillow format name this re-encodes a shrunk image under
    holds_alpha: bool  # whether the encoder can carry a transparency channel


def _is_webp(data: bytes) -> bool:
    # WEBP is a RIFF container tagged "WEBP" at byte 8, not a plain prefix.
    return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"


IMAGE_FORMATS: tuple[ImageFormat, ...] = (
    ImageFormat(
        "image/png",
        ".png",
        lambda d: d.startswith(b"\x89PNG\r\n\x1a\n"),
        encoder="PNG",
        holds_alpha=True,
    ),
    ImageFormat(
        "image/jpeg",
        ".jpg",
        lambda d: d.startswith(b"\xff\xd8\xff"),
        encoder="JPEG",
        holds_alpha=False,
    ),
    ImageFormat(
        "image/gif",
        ".gif",
        lambda d: d.startswith((b"GIF87a", b"GIF89a")),
        encoder="GIF",
        holds_alpha=True,
    ),
    ImageFormat(
        "image/webp",
        ".webp",
        _is_webp,
        encoder="WEBP",
        holds_alpha=True,
    ),
)

#: The longest side an outbound image may have once this deck is done with it.
#:
#: Anthropic applies a stricter per-image rule once one request carries many
#: images: any side over 2000px is rejected outright. The block it names stays
#: in the conversation forever, so from that point on *every* turn 400s with
#: "an image in the conversation could not be processed and was removed" and the
#: chat is dead no matter what is sent next (#2038). 1568 is the size Anthropic
#: itself scales a large image down to before the model ever sees it, so
#: capping here costs nothing a model could have read anyway, and it leaves
#: plenty of headroom under the 2000px cliff.
MAX_IMAGE_DIMENSION = 1568

#: The media types AgentDeck accepts anywhere it renders or forwards an image.
SUPPORTED_IMAGE_MEDIA_TYPES: frozenset[str] = frozenset(
    fmt.media_type for fmt in IMAGE_FORMATS
)

_FORMAT_BY_MEDIA_TYPE = {fmt.media_type: fmt for fmt in IMAGE_FORMATS}
# Every accepted upload is stored under a format's canonical suffix, but ".jpeg"
# is a common alias a caller may hand us, so it resolves to image/jpeg too.
_MEDIA_TYPE_BY_SUFFIX = {fmt.suffix: fmt.media_type for fmt in IMAGE_FORMATS}
_MEDIA_TYPE_BY_SUFFIX[".jpeg"] = "image/jpeg"


def media_type_for_suffix(suffix: str) -> str | None:
    """The media type for a file suffix, or None if it is not a supported image."""
    return _MEDIA_TYPE_BY_SUFFIX.get(suffix.lower())


def suffix_for_media_type(media_type: str) -> str | None:
    """The canonical suffix for a media type, or None if it is not supported."""
    fmt = _FORMAT_BY_MEDIA_TYPE.get(media_type)
    return fmt.suffix if fmt is not None else None


def sniff_suffix(data: bytes) -> str | None:
    """Canonical suffix for the format whose signature ``data`` starts with.

    Returns None when the bytes match no supported format, so callers can reject
    a file whose declared type does not match its actual contents.
    """
    for fmt in IMAGE_FORMATS:
        if fmt.matches(data):
            return fmt.suffix
    return None


class UnsupportedImage(ValueError):
    """An outbound attachment AgentDeck will not forward to an agent."""


def _save_animation(opened, size, fmt, buffer, Image, ImageSequence) -> None:
    """Rewrite every frame of an animation at ``size``, keeping what makes it play.

    Pillow's sequence iterator hands back each frame already composited against
    the ones before it, so writing full frames with disposal 2 ("restore to
    background") reproduces the animation without inheriting the source's
    per-frame disposal bookkeeping.

    Per-frame duration has to be read *after* the frame is loaded. Seeking a
    WebP only selects the logical frame; ``load()`` is what fills ``info`` with
    that frame's timing, so sampling ``info`` first yields None for frame 0 and
    the previous frame's value thereafter — enough to look like WebP carries no
    timing at all. Read it after the load and the original timing survives.
    """
    frames = []
    durations = []
    for source in ImageSequence.Iterator(opened):
        source.load()
        durations.append(source.info.get("duration"))
        frames.append(source.convert("RGBA").resize(size, Image.LANCZOS))
    options = {
        "save_all": True,
        "append_images": frames[1:],
        "loop": opened.info.get("loop", 0),
        "disposal": 2,
    }
    if all(duration is not None for duration in durations):
        options["duration"] = durations
    frames[0].save(buffer, format=fmt.encoder, **options)


def shrink_to_limit(media_type: str, data: bytes, limit: int = MAX_IMAGE_DIMENSION) -> bytes:
    """``data``, scaled down so neither side exceeds ``limit``.

    Best-effort *repair*, never a new refusal: an image already within the limit
    comes back byte-identical, and anything this cannot decode or re-encode comes
    back unchanged too. Passing a doomed image through is what the deck does
    today, so a host with no Pillow or a file Pillow dislikes is no worse off
    than before — while the common case, a full-resolution screenshot someone
    pasted, stops being able to kill a chat.

    An animation is scaled frame by frame rather than flattened to its first
    frame, so nothing silently loses the rest of a GIF on the way out.

    Blocking and CPU-bound on the resize path: call it off the event loop.
    """
    fmt = _FORMAT_BY_MEDIA_TYPE.get(media_type)
    if fmt is None:
        return data
    try:
        from PIL import Image, ImageOps, ImageSequence
    except ImportError:  # pragma: no cover - a host that skipped a dependency sync
        log.warning("Pillow is unavailable; forwarding %s at full size", media_type)
        return data
    try:
        with Image.open(io.BytesIO(data)) as opened:
            if max(opened.size) <= limit:
                return data
            # A phone photo usually stores its rotation in EXIF rather than in
            # the pixels. Re-encoding drops that tag, so bake the rotation in
            # first or a portrait shot arrives sideways — and measure after the
            # transform, since it can swap the two sides. An animation carries no
            # EXIF orientation, so it skips this.
            animated = getattr(opened, "n_frames", 1) > 1
            frame = opened if animated else ImageOps.exif_transpose(opened)
            width, height = frame.size
            longest = max(width, height)
            scale = limit / longest
            size = (max(1, round(width * scale)), max(1, round(height * scale)))
            buffer = io.BytesIO()
            if animated:
                _save_animation(opened, size, fmt, buffer, Image, ImageSequence)
            else:
                # A palette image has no usable pixels to interpolate between,
                # and a JPEG cannot carry the alpha an RGBA source would hand it.
                if frame.mode in {"P", "PA"}:
                    frame = frame.convert("RGBA" if fmt.holds_alpha else "RGB")
                if not fmt.holds_alpha and frame.mode not in {"RGB", "L", "CMYK"}:
                    frame = frame.convert("RGB")
                frame.resize(size, Image.LANCZOS).save(buffer, format=fmt.encoder)
    except Exception:  # noqa: BLE001 - any decode/encode failure keeps today's bytes
        log.warning("could not shrink a %s attachment; forwarding it", media_type)
        return data
    log.info(
        "shrank a %s attachment from %dx%d to %dx%d",
        media_type,
        width,
        height,
        size[0],
        size[1],
    )
    return buffer.getvalue()


def read_supported_image(path_value: str | Path) -> tuple[str, bytes]:
    """One outbound attachment as ``(media_type, raw bytes)``.

    The single read-and-admit step for a provider that forwards an upload's
    bytes (Claude, which wraps them in Anthropic content blocks), so the *format*
    and *size* decisions live here with the table they derive from rather than in
    the worker. Blocking I/O: call it off the event loop.

    Raises ``UnsupportedImage`` for a type this deck does not accept or a file it
    cannot read, so a caller can refuse the whole delivery before writing
    anything.
    """
    path = Path(path_value)
    media_type = media_type_for_suffix(path.suffix)
    if media_type is None:
        raise UnsupportedImage(f"unsupported image type: {path.name}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise UnsupportedImage(f"could not read image {path.name}: {exc}") from exc
    return media_type, shrink_to_limit(media_type, data)
