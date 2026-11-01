"""C04.1 解码协议：真实编解码与资源边界；不做视觉质量判断。"""

import builtins
import io
from dataclasses import asdict

import pytest
from PIL import Image, ImageFile, PngImagePlugin

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.vision.adapters.pillow import PillowDecoder
from adam.vision.models import ImageError, ImageInfo, ImageLimits
from tests.image_samples import image_bytes


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
@pytest.mark.parametrize("orientation", [None, 1, 6, 8])
def test_complete_images_have_real_format_dimensions_and_original_exif(image_format, orientation):
    raw = image_bytes(image_format, orientation=orientation)
    info = PillowDecoder(ImageLimits()).decode(raw, cancel=CancellationToken())
    assert asdict(info) == {
        "format": image_format,
        "width": 3,
        "height": 2,
        "orientation": orientation,
    }
    assert (
        info.mime_type
        == {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image_format]
    )
    assert info.display_size == ((2, 3) if orientation in (6, 8) else (3, 2))


@pytest.mark.parametrize(
    "limits,expected",
    [
        (ImageLimits(max_pixels=11), "pixel_limit"),
        (ImageLimits(max_dimension=3), "dimension_limit"),
    ],
)
def test_header_limits_reject_before_any_pixel_load(monkeypatch, limits, expected):
    raw = image_bytes(size=(4, 3))
    monkeypatch.setattr(
        PngImagePlugin.PngImageFile, "load", lambda *_: pytest.fail("pixel load before bound")
    )
    with pytest.raises(ImageError) as raised:
        PillowDecoder(limits).decode(raw, cancel=CancellationToken())
    assert raised.value.kind == expected


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_missing_tail_is_not_an_acceptable_complete_image(image_format):
    raw = image_bytes(image_format)
    with pytest.raises(ImageError) as raised:
        PillowDecoder(ImageLimits()).decode(raw[:-12], cancel=CancellationToken())
    assert raised.value.kind in {"corrupt_image", "unsupported_or_corrupt"}


def test_bad_png_crc_and_invalid_orientation_are_not_saved():
    raw = bytearray(image_bytes())
    raw[-13] ^= 1
    with pytest.raises(ImageError):
        PillowDecoder(ImageLimits()).decode(bytes(raw), cancel=CancellationToken())
    with pytest.raises(ImageError, match="方向"):
        PillowDecoder(ImageLimits()).decode(
            image_bytes("JPEG", orientation=9), cancel=CancellationToken()
        )


@pytest.mark.parametrize(
    "raw",
    [b"", b"private non-image content", image_bytes("GIF")],
    ids=["empty", "non-image", "gif"],
)
def test_unsupported_or_corrupt_content_has_controlled_diagnostic(raw):
    with pytest.raises(ImageError) as raised:
        PillowDecoder(ImageLimits()).decode(raw, cancel=CancellationToken())
    assert raw.decode("ascii", errors="ignore") not in str(raised.value) or not raw
    assert raised.value.kind in {"corrupt_image", "unsupported_or_corrupt"}


@pytest.mark.parametrize("image_format", ["PNG", "WEBP"])
def test_animated_images_cannot_be_silently_reduced_to_first_frame(image_format):
    with pytest.raises(ImageError) as raised:
        PillowDecoder(ImageLimits()).decode(
            image_bytes(image_format, animated=True), cancel=CancellationToken()
        )
    assert raised.value.kind == "animated_image"


def test_byte_bound_and_cancel_are_checked_before_import_or_decode(monkeypatch):
    original = builtins.__import__

    def reject(name, *args, **kwargs):
        if name == "PIL":
            pytest.fail("rejected input imported SDK")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject)
    with pytest.raises(ImageError) as raised:
        PillowDecoder(ImageLimits(max_bytes=1)).decode(b"xx", cancel=CancellationToken())
    assert raised.value.kind == "byte_limit"
    cancel = CancellationToken()
    cancel.cancel()
    with pytest.raises(RequestInterrupted):
        PillowDecoder(ImageLimits()).decode(b"xx", cancel=cancel)


def test_missing_optional_sdk_is_explicit(monkeypatch):
    raw = image_bytes()
    original = builtins.__import__

    def reject(name, *args, **kwargs):
        if name == "PIL":
            raise ImportError("private dependency diagnostic")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject)
    with pytest.raises(ImageError) as raised:
        PillowDecoder(ImageLimits()).decode(raw, cancel=CancellationToken())
    assert raised.value.kind == "dependency_missing"
    assert "private" not in str(raised.value)


def test_decoder_does_not_change_global_safety_settings(monkeypatch):
    raw = image_bytes()
    maximum = Image.MAX_IMAGE_PIXELS
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    with pytest.raises(ImageError) as raised:
        PillowDecoder(ImageLimits()).decode(raw, cancel=CancellationToken())
    assert raised.value.kind == "unsafe_decoder_settings"
    assert ImageFile.LOAD_TRUNCATED_IMAGES is True and Image.MAX_IMAGE_PIXELS == maximum


def test_internal_streams_are_closed_on_success_and_corruption(monkeypatch):
    raw = image_bytes()
    original = io.BytesIO
    streams = []

    def opened(data):
        result = original(data)
        streams.append(result)
        return result

    monkeypatch.setattr("adam.vision.adapters.pillow.io.BytesIO", opened)
    PillowDecoder(ImageLimits()).decode(raw, cancel=CancellationToken())
    with pytest.raises(ImageError):
        PillowDecoder(ImageLimits()).decode(raw[:-12], cancel=CancellationToken())
    assert len(streams) >= 3 and all(stream.closed for stream in streams)


@pytest.mark.parametrize(
    "change",
    [
        {"max_bytes": True},
        {"max_bytes": 10485761},
        {"max_pixels": 0},
        {"max_pixels": 64000001},
        {"max_dimension": 16385},
        {"max_dimension": 1.5},
    ],
)
def test_image_limits_are_finite_and_do_not_treat_bool_as_int(change):
    with pytest.raises(ValueError):
        ImageLimits(**change)


@pytest.mark.parametrize("orientation,expected", [(2, (3, 2)), (5, (2, 3)), (7, (2, 3))])
def test_mirrored_exif_orientation_keeps_original_code(orientation, expected):
    info = ImageInfo("PNG", 3, 2, orientation)
    assert info.orientation == orientation and info.display_size == expected
