"""Unit tests for the supported-image-format single source of truth."""

from __future__ import annotations

import io

import pytest

from agentdeck.images import (
    IMAGE_FORMATS,
    MAX_IMAGE_DIMENSION,
    SUPPORTED_IMAGE_MEDIA_TYPES,
    UnsupportedImage,
    media_type_for_suffix,
    read_supported_image,
    shrink_to_limit,
    sniff_suffix,
    suffix_for_media_type,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
_GIF87 = b"GIF87a" + b"\x00" * 8
_GIF89 = b"GIF89a" + b"\x00" * 8
_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4


def test_supported_media_types_are_the_four_formats():
    assert SUPPORTED_IMAGE_MEDIA_TYPES == frozenset(
        {"image/png", "image/jpeg", "image/webp", "image/gif"}
    )
    assert {fmt.media_type for fmt in IMAGE_FORMATS} == SUPPORTED_IMAGE_MEDIA_TYPES


@pytest.mark.parametrize(
    ("media_type", "suffix"),
    [
        ("image/png", ".png"),
        ("image/jpeg", ".jpg"),
        ("image/gif", ".gif"),
        ("image/webp", ".webp"),
    ],
)
def test_media_type_and_suffix_round_trip(media_type, suffix):
    assert suffix_for_media_type(media_type) == suffix
    assert media_type_for_suffix(suffix) == media_type


def test_jpeg_alias_and_case_insensitive_suffix():
    # ".jpeg" is an accepted alias even though uploads store ".jpg", and suffix
    # lookup is case-insensitive.
    assert media_type_for_suffix(".jpeg") == "image/jpeg"
    assert media_type_for_suffix(".JPG") == "image/jpeg"
    assert media_type_for_suffix(".PNG") == "image/png"


def test_unsupported_suffix_and_media_type_return_none():
    assert media_type_for_suffix(".bmp") is None
    assert media_type_for_suffix("") is None
    assert suffix_for_media_type("image/bmp") is None
    assert suffix_for_media_type("text/plain") is None


@pytest.mark.parametrize(
    ("data", "suffix"),
    [
        (_PNG, ".png"),
        (_JPEG, ".jpg"),
        (_GIF87, ".gif"),
        (_GIF89, ".gif"),
        (_WEBP, ".webp"),
    ],
)
def test_sniff_recognizes_each_format_signature(data, suffix):
    assert sniff_suffix(data) == suffix


def test_sniff_rejects_non_images_and_partial_webp():
    assert sniff_suffix(b"not an image at all") is None
    assert sniff_suffix(b"") is None
    # RIFF container that is not WEBP must not be accepted.
    assert sniff_suffix(b"RIFF\x00\x00\x00\x00AVI ....") is None
    # RIFF header too short to carry the WEBP tag.
    assert sniff_suffix(b"RIFF\x00\x00") is None


# --- outbound size cap (#2038) ---------------------------------------------
#
# One image whose longest side clears 2000px permanently kills an image-heavy
# Claude chat: the API names that block, the block never leaves the history, and
# every later turn 400s. The deck shrinks an attachment on the way out so a
# pasted full-resolution screenshot cannot do that.

_ENCODER_BY_MEDIA_TYPE = {fmt.media_type: fmt.encoder for fmt in IMAGE_FORMATS}


def _encode(media_type: str, size: tuple[int, int], mode: str = "RGB") -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    Image.new(mode, size, "white").save(buffer, format=_ENCODER_BY_MEDIA_TYPE[media_type])
    return buffer.getvalue()


def _dimensions(data: bytes) -> tuple[int, int]:
    Image = pytest.importorskip("PIL.Image")
    with Image.open(io.BytesIO(data)) as opened:
        return opened.size


def test_an_image_within_the_limit_is_forwarded_byte_identical():
    # Re-encoding a small image would cost quality for nothing, so the shrink
    # must be a no-op rather than a round-trip.
    original = _encode("image/png", (MAX_IMAGE_DIMENSION, 40))

    assert shrink_to_limit("image/png", original) is original


@pytest.mark.parametrize("media_type", ["image/png", "image/jpeg", "image/gif", "image/webp"])
def test_an_oversized_image_is_shrunk_in_every_supported_format(media_type):
    # A small limit keeps the encode cheap: the format handling is what varies here,
    # and the tests below hold the real default against full-size images.
    limit = 64
    shrunk = shrink_to_limit(media_type, _encode(media_type, (4 * limit, 2 * limit)), limit)

    width, height = _dimensions(shrunk)
    assert max(width, height) == limit
    # Aspect ratio survives, and the format does too: a caller has already told
    # its provider this media type, so re-encoding to another would lie.
    assert (width, height) == (limit, limit // 2)
    assert sniff_suffix(shrunk) == suffix_for_media_type(media_type)


def test_the_cap_applies_to_the_longer_side_whichever_it_is():
    # A tall screenshot is the common case on a phone, and it breaks a chat just
    # as thoroughly as a wide one.
    shrunk = shrink_to_limit("image/png", _encode("image/png", (500, 3000)))

    assert _dimensions(shrunk) == (261, MAX_IMAGE_DIMENSION)


def test_a_palette_image_survives_the_shrink():
    # A GIF or an indexed PNG has no pixels to interpolate between until it is
    # converted, and resizing one without that raises instead of shrinking.
    shrunk = shrink_to_limit("image/png", _encode("image/png", (3000, 3000), mode="P"))

    assert _dimensions(shrunk) == (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION)


def test_undecodable_bytes_are_forwarded_unchanged_rather_than_refused():
    # The shrink is a repair, not a new admission rule. Bytes it cannot read are
    # exactly as (un)usable as they were before this existed, so forwarding them
    # keeps the deck's behaviour instead of inventing a refusal for a file that
    # may well have worked.
    stub = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

    assert shrink_to_limit("image/png", stub) == stub


def test_an_unknown_media_type_is_left_alone():
    assert shrink_to_limit("image/bmp", b"BM stuff") == b"BM stuff"


def _animated_gif(size: tuple[int, int]) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    frames = []
    for index in range(3):
        frame = Image.new("RGB", size, "white")
        # A block that moves between frames, so a flattened result is obvious.
        block = Image.new("RGB", (size[0] // 4, size[1] // 4), "black")
        frame.paste(block, (index * size[0] // 4, 0))
        frames.append(frame.convert("P", palette=Image.ADAPTIVE))
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[100, 200, 300],
        loop=3,
    )
    return buffer.getvalue()


def _animated_webp(size: tuple[int, int]) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    frames = [Image.new("RGB", size, colour) for colour in ("red", "green", "blue")]
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=[100, 200, 300],
        loop=3,
    )
    return buffer.getvalue()


def test_an_oversized_animation_is_capped_with_every_frame_intact():
    # Flattening an animation to its first frame would be a silent loss, but
    # forwarding it whole would leave exactly the poison-size image this cap
    # exists to stop. Scale every frame instead, and keep what makes it play.
    ImageSequence = pytest.importorskip("PIL.ImageSequence")
    Image = pytest.importorskip("PIL.Image")

    # A small limit keeps three frames cheap to encode; the cap's real default is held
    # against full-size images above.
    shrunk = shrink_to_limit("image/gif", _animated_gif((300, 100)), 150)

    with Image.open(io.BytesIO(shrunk)) as opened:
        assert max(opened.size) <= 150
        assert opened.size == (150, 50)
        assert opened.n_frames == 3
        assert opened.info.get("loop") == 3
        durations = [frame.info.get("duration") for frame in ImageSequence.Iterator(opened)]
    assert durations == [100, 200, 300]


def test_an_oversized_animated_webp_is_capped_and_keeps_its_frames():
    # Variable frame timing has to survive too. Seeking a WebP only selects the
    # frame; its duration appears in `info` once the frame is loaded, so reading
    # too early makes WebP look like it carries no timing and quietly drops it.
    ImageSequence = pytest.importorskip("PIL.ImageSequence")
    Image = pytest.importorskip("PIL.Image")

    shrunk = shrink_to_limit("image/webp", _animated_webp((3000, 1000)))

    with Image.open(io.BytesIO(shrunk)) as opened:
        assert max(opened.size) <= MAX_IMAGE_DIMENSION
        assert opened.size == (MAX_IMAGE_DIMENSION, 523)
        assert opened.n_frames == 3
        assert opened.info.get("loop") == 3
        durations = []
        for frame in ImageSequence.Iterator(opened):
            frame.load()
            durations.append(frame.info.get("duration"))
    assert durations == [100, 200, 300]


def test_an_animation_within_the_limit_is_left_byte_identical():
    original = _animated_gif((800, 400))

    assert shrink_to_limit("image/gif", original) is original


def _exif_rotated_jpeg(size: tuple[int, int]) -> bytes:
    """A landscape JPEG whose EXIF says "display me rotated 90° clockwise"."""
    Image = pytest.importorskip("PIL.Image")
    source = Image.new("RGB", size, "white")
    # A marker in the stored top-left corner, which orientation 6 puts at the
    # top-right of what a reader actually sees.
    source.paste(Image.new("RGB", (size[0] // 5, size[1] // 5), "black"), (0, 0))
    exif = source.getexif()
    exif[274] = 6
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def test_an_exif_rotated_photo_keeps_its_orientation_through_the_shrink():
    # Re-encoding drops the EXIF tag, so the rotation has to be baked into the
    # pixels first or a portrait phone photo arrives sideways. The cap is also
    # measured after the rotation, since it swaps the two sides.
    Image = pytest.importorskip("PIL.Image")

    shrunk = shrink_to_limit("image/jpeg", _exif_rotated_jpeg((3000, 1000)))

    with Image.open(io.BytesIO(shrunk)) as opened:
        width, height = opened.size
        # Stored landscape, displayed portrait: the shrink honours what a reader
        # sees, not the raw pixel matrix.
        assert (width, height) == (523, MAX_IMAGE_DIMENSION)
        # The tag is gone, so nothing downstream rotates it a second time.
        assert opened.getexif().get(274) is None
        pixels = opened.convert("RGB").load()
        corners = {
            "top left": pixels[5, 5],
            "top right": pixels[width - 6, 5],
            "bottom left": pixels[5, height - 6],
            "bottom right": pixels[width - 6, height - 6],
        }
    dark = [corner for corner, value in corners.items() if sum(value) < 200]
    assert dark == ["top right"]


def test_read_supported_image_shrinks_what_it_admits(tmp_path):
    # The shrink has to live behind the shared admission point, so every
    # provider that forwards an upload gets it without its own copy.
    path = tmp_path / "screenshot.png"
    path.write_bytes(_encode("image/png", (3000, 1000)))

    media_type, data = read_supported_image(path)

    assert media_type == "image/png"
    assert _dimensions(data) == (MAX_IMAGE_DIMENSION, 523)


def test_read_supported_image_still_refuses_an_unsupported_type(tmp_path):
    path = tmp_path / "diagram.bmp"
    path.write_bytes(b"BM stuff")

    with pytest.raises(UnsupportedImage):
        read_supported_image(path)
