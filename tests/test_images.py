"""Image loading, downsampling and caching."""

from __future__ import annotations

import base64
import os
import random
import struct
from io import BytesIO

import pytest
from PIL import ExifTags, Image, ImageChops

from platefold.images import CSS_PX_PER_INCH, ImageLoader, target_pixel_width
from platefold.limits import ResourceLimitError, ResourceLimits


def make_noise_png(path, w: int = 1200, h: int = 800) -> None:
    """Write an incompressible image.

    Genuinely random pixels matter here: a structured test pattern compresses
    so well that resampling it *increases* the PNG size, and the loader then
    correctly declines to downsample — which would make these tests assert the
    opposite of what they mean to.
    """
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(path, format="PNG")


@pytest.fixture
def big_png(tmp_path):
    path = tmp_path / "big.png"
    make_noise_png(path)
    return path


@pytest.fixture
def small_png(tmp_path):
    path = tmp_path / "small.png"
    Image.new("RGB", (120, 90), (200, 40, 40)).save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# DPI arithmetic
# ---------------------------------------------------------------------------


def test_target_width_is_placed_inches_times_dpi():
    # 96 CSS px is one inch; at 300 DPI that needs 300 pixels.
    assert target_pixel_width(CSS_PX_PER_INCH, 300) == 300
    assert target_pixel_width(CSS_PX_PER_INCH * 2, 300) == 600
    assert target_pixel_width(CSS_PX_PER_INCH, 150) == 150


def test_target_width_never_reaches_zero():
    assert target_pixel_width(0.0, 300) >= 1


# ---------------------------------------------------------------------------
# Downsampling
# ---------------------------------------------------------------------------


def test_oversized_image_is_resampled_and_gets_smaller(big_png):
    loader = ImageLoader(dpi=300)
    # 200 CSS px placed => 200/96 inch => 625px at 300 DPI, well under the
    # fixture's 1200px, so downsampling definitely applies.
    loaded = loader.load(str(big_png), placed_px=200.0)
    assert loaded.was_downsampled
    assert loaded.encoded_w == target_pixel_width(200.0, 300)
    assert loaded.encoded_bytes < loaded.original_bytes


def test_aspect_ratio_is_preserved_when_resampling(big_png):
    loaded = ImageLoader(dpi=300).load(str(big_png), placed_px=200.0)
    original = loaded.natural_w / loaded.natural_h
    resampled = loaded.encoded_w / loaded.encoded_h
    assert resampled == pytest.approx(original, rel=0.01)


def test_image_already_below_print_resolution_is_left_untouched(small_png):
    loaded = ImageLoader(dpi=300).load(str(small_png), placed_px=400.0)
    assert not loaded.was_downsampled
    assert loaded.encoded_bytes == loaded.original_bytes
    assert loaded.encoded_w == 120


def test_lower_dpi_produces_a_smaller_encoding(big_png):
    hi = ImageLoader(dpi=300).load(str(big_png), placed_px=300.0)
    lo = ImageLoader(dpi=96).load(str(big_png), placed_px=300.0)
    assert lo.encoded_w < hi.encoded_w
    assert lo.encoded_bytes < hi.encoded_bytes


def test_downsampling_never_loses_the_image_on_error(tmp_path):
    """A file Pillow can't parse still gets embedded rather than dropped."""
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not really a png")
    loaded = ImageLoader().load(str(broken), placed_px=300.0)
    assert loaded.data_uri.startswith("data:image/png;base64,")
    assert loaded.encoded_bytes == loaded.original_bytes


def test_svg_is_passed_through_untouched(tmp_path):
    svg = tmp_path / "vector.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>')
    loaded = ImageLoader().load(str(svg), placed_px=300.0)
    assert loaded.data_uri.startswith("data:image/svg+xml;base64,")
    assert loaded.encoded_bytes == loaded.original_bytes


def test_image_bytes_are_bounded_before_reading(tmp_path):
    image = tmp_path / "large.png"
    image.write_bytes(b"x" * 11)
    loader = ImageLoader(limits=ResourceLimits(max_image_bytes=10))

    with pytest.raises(ResourceLimitError, match="10-byte limit"):
        loader.load(str(image), placed_px=100)

    with pytest.raises(ResourceLimitError, match="10-byte limit"):
        loader.natural_size(str(image))


def test_raster_pixel_count_is_bounded(tmp_path):
    image = tmp_path / "pixels.png"
    Image.new("RGB", (11, 10)).save(image)
    loader = ImageLoader(limits=ResourceLimits(max_image_pixels=100))

    with pytest.raises(ResourceLimitError, match="110 pixels"):
        loader.natural_size(str(image))


def test_animated_image_frame_count_is_bounded(tmp_path):
    image = tmp_path / "animated.gif"
    frames = [Image.new("RGB", (2, 2), color) for color in ("red", "green", "blue")]
    frames[0].save(image, save_all=True, append_images=frames[1:], format="GIF")
    loader = ImageLoader(limits=ResourceLimits(max_gif_frames=2))

    with pytest.raises(ResourceLimitError, match="3 frames"):
        loader.natural_size(str(image))


# ---------------------------------------------------------------------------
# Caching — repeated images avoid re-encoding without retaining a whole corpus.
# ---------------------------------------------------------------------------


def test_repeated_load_at_the_same_size_is_served_from_cache(big_png):
    loader = ImageLoader(dpi=300)
    first = loader.load(str(big_png), placed_px=200.0)
    second = loader.load(str(big_png), placed_px=200.0)
    assert first is second, "repeated placements must not re-encode the image"


def test_image_cache_does_not_retain_an_item_larger_than_its_budget(small_png):
    loader = ImageLoader(max_cache_bytes=1)
    first = loader.load(str(small_png), placed_px=400.0)
    second = loader.load(str(small_png), placed_px=400.0)

    assert first is not second
    assert loader.cached_bytes == 0


def test_image_cache_evicts_least_recently_used_items(tmp_path):
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    Image.new("RGB", (120, 90), (200, 40, 40)).save(first_path)
    Image.new("RGB", (120, 90), (40, 40, 200)).save(second_path)
    sample = ImageLoader().load(str(first_path), placed_px=400.0)
    loader = ImageLoader(max_cache_bytes=len(sample.data_uri) + 16)

    first = loader.load(str(first_path), placed_px=400.0)
    loader.load(str(second_path), placed_px=400.0)
    reloaded = loader.load(str(first_path), placed_px=400.0)

    assert first is not reloaded
    assert loader.cached_bytes <= loader.max_cache_bytes


def test_editing_the_file_invalidates_the_cache(big_png):
    loader = ImageLoader(dpi=300)
    first = loader.load(str(big_png), placed_px=200.0)
    Image.new("RGB", (1200, 800), (10, 200, 10)).save(big_png, format="PNG")
    os.utime(big_png, (0, 0))  # force a different mtime stamp
    second = loader.load(str(big_png), placed_px=200.0)
    assert first is not second
    assert first.data_uri != second.data_uri


def test_natural_size_does_not_hold_the_file_open(big_png):
    """Windows locks open files; a leaked handle makes the file undeletable.

    The original code called `Image.open(path).size` without closing, which
    left a handle open for the life of the process.
    """
    loader = ImageLoader()
    assert loader.natural_size(str(big_png)) == (1200, 800)
    os.remove(big_png)  # raises PermissionError on Windows if a handle leaked


def test_load_does_not_hold_the_file_open(big_png):
    loader = ImageLoader(dpi=300)
    loader.load(str(big_png), placed_px=200.0)
    os.remove(big_png)


def test_natural_size_of_an_unreadable_file_is_zero(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"nope")
    assert ImageLoader().natural_size(str(broken)) == (0, 0)


# ---------------------------------------------------------------------------
# Browser formats: Chromium must be able to draw what is embedded.
# ---------------------------------------------------------------------------


def decoded(uri: str) -> tuple[str, Image.Image]:
    """The MIME type a data URI declares, and the image its bytes hold."""
    header, payload = uri.split(",", 1)
    return header[5:].split(";", 1)[0], Image.open(BytesIO(base64.b64decode(payload)))


def encoded_bytes(fmt: str, image: Image.Image, **options) -> bytes:
    output = BytesIO()
    image.save(output, format=fmt, **options)
    return output.getvalue()


# Formats Pillow reads and Chromium does not draw as delivered.
CONVERTED = [("TIFF", "tiff"), ("TIFF", "tif"), ("BMP", "bmp"), ("PPM", "ppm"), ("TGA", "tga")]


@pytest.mark.parametrize(("fmt", "ext"), CONVERTED)
@pytest.mark.parametrize("placed", [400.0, 30.0], ids=["at-or-below-target", "downsampled"])
def test_formats_the_browser_cannot_draw_are_always_converted(tmp_path, fmt, ext, placed):
    raw = encoded_bytes(fmt, Image.new("RGB", (60, 40), (220, 20, 20)))
    path = tmp_path / f"figure.{ext}"
    path.write_bytes(raw)
    for loaded in (
        ImageLoader(dpi=96).load(str(path), placed),
        ImageLoader(dpi=96).load_bytes(path.name, raw, placed),
    ):
        mime, image = decoded(loaded.data_uri)
        assert (mime, image.format) == ("image/png", "PNG")
        width = 60 if placed == 400.0 else target_pixel_width(placed, 96)
        assert image.size == (loaded.encoded_w, loaded.encoded_h) == (width, width * 2 // 3)
        assert image.convert("RGB").getpixel((width // 2, width // 3)) == (220, 20, 20)


def test_icon_files_are_converted_to_the_frame_that_was_measured(tmp_path):
    path = tmp_path / "icon.ico"
    Image.new("RGB", (48, 48), (20, 20, 220)).save(path, sizes=[(16, 16), (48, 48)])
    loader = ImageLoader()
    loaded = loader.load(str(path), 400.0)
    mime, image = decoded(loaded.data_uri)
    assert (mime, image.format) == ("image/png", "PNG")
    assert image.size == loader.natural_size(str(path)) == (48, 48)


def test_content_decides_the_format_not_the_file_name(tmp_path):
    tiff_named_png = tmp_path / "scan.png"
    tiff_named_png.write_bytes(encoded_bytes("TIFF", Image.new("RGB", (30, 20), "red")))
    png_named_jpg = tmp_path / "chart.jpg"
    png_named_jpg.write_bytes(encoded_bytes("PNG", Image.new("RGB", (30, 20), "red")))
    loader = ImageLoader()

    converted = loader.load(str(tiff_named_png), 400.0)
    assert decoded(converted.data_uri)[1].format == "PNG"
    untouched = loader.load(str(png_named_jpg), 400.0)
    assert untouched.data_uri.startswith("data:image/png;base64,")
    assert untouched.encoded_bytes == untouched.original_bytes


def test_a_required_conversion_is_kept_even_when_it_is_larger(tmp_path):
    """JPEG-compressed TIFF noise is far smaller than any lossless PNG of it."""
    noise = Image.frombytes("RGB", (200, 150), os.urandom(200 * 150 * 3))
    path = tmp_path / "photo.tiff"
    noise.save(path, format="TIFF", compression="jpeg")
    for placed in (400.0, 190.0):  # at or below target, then downsampled at 96 DPI
        loaded = ImageLoader(dpi=96).load(str(path), placed)
        assert loaded.data_uri.startswith("data:image/png;base64,")
        assert loaded.encoded_bytes > loaded.original_bytes


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP"])
def test_browser_rasters_at_print_resolution_are_embedded_byte_for_byte(tmp_path, fmt):
    raw = encoded_bytes(fmt, Image.new("RGB", (30, 20), "red"))
    loaded = ImageLoader().load_bytes(f"figure.{fmt.lower()}", raw, 400.0)
    assert base64.b64decode(loaded.data_uri.split(",", 1)[1]) == raw


def test_an_inline_tiff_is_converted_and_a_browser_raster_is_left_alone():
    loader = ImageLoader()
    tiff = "data:image/tiff;base64," + base64.b64encode(
        encoded_bytes("TIFF", Image.new("RGB", (30, 20), "red"))
    ).decode("ascii")
    loader.validate_data_uri(tiff, undecodable=lambda _format: None)
    mime, image = decoded(loader.displayable_data_uri(tiff))
    assert (mime, image.format, image.size) == ("image/png", "PNG", (30, 20))

    png = "data:image/png;base64," + base64.b64encode(
        encoded_bytes("PNG", Image.new("RGB", (30, 20), "red"))
    ).decode("ascii")
    assert loader.displayable_data_uri(png) is png


def test_a_nested_raster_the_browser_cannot_draw_is_reported_by_format():
    tiff = base64.b64encode(encoded_bytes("TIFF", Image.new("RGB", (4, 4), "red"))).decode()
    bmp = base64.b64encode(encoded_bytes("BMP", Image.new("RGB", (4, 4), "red"))).decode()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
        f'<image href="data:image/tiff;base64,{tiff}" width="4" height="4"/>'
        f'<image href="data:image/bmp;base64,{bmp}" width="4" height="4"/></svg>'
    )
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    reported: list[str] = []
    ImageLoader().validate_data_uri(uri, undecodable=reported.append)
    assert reported == ["TIFF"]  # Chromium draws a BMP where it cannot be converted

    with pytest.raises(ValueError, match="TIFF image cannot be displayed"):
        ImageLoader().validate_data_uri(uri)


# ---------------------------------------------------------------------------
# EXIF orientation: measured, planned and re-encoded in displayed geometry.
# ---------------------------------------------------------------------------

QUADRANTS = {"tl": (230, 30, 30), "tr": (30, 200, 30), "bl": (30, 30, 230), "br": (230, 230, 30)}

# EXIF orientation -> the transpose that stores an upright picture under that tag.
STORED = {
    1: None,
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_90,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_270,
}


def upright_quadrants(width: int, height: int) -> Image.Image:
    """Four coloured quadrants with noise, so that downsampling saves bytes."""
    image = Image.new("RGB", (width, height))
    for key, (x, y) in {"tl": (0, 0), "tr": (1, 0), "bl": (0, 1), "br": (1, 1)}.items():
        box = (x * width // 2, y * height // 2, (x + 1) * width // 2, (y + 1) * height // 2)
        image.paste(QUADRANTS[key], box)
    rng = random.Random(width * height)
    noise = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    return ImageChops.add(image, noise.point(lambda v: v * 50 // 255), offset=-25)


def oriented_bytes(fmt: str, orientation: int, width: int = 160, height: int = 240) -> bytes:
    """A picture that displays upright at ``width`` x ``height`` under its EXIF tag."""
    image = upright_quadrants(width, height)
    if STORED[orientation] is not None:
        image = image.transpose(STORED[orientation])
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = orientation
    options = {"quality": 95} if fmt == "JPEG" else {}
    return encoded_bytes(fmt, image, exif=exif, **options)


def quadrant_colours(image: Image.Image) -> list[str]:
    """Which reference colour each quadrant of ``image`` shows, in reading order."""
    image = image.convert("RGB")
    width, height = image.size
    found = []
    for fx, fy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        box = (
            fx * width // 2 + width // 8,
            fy * height // 2 + height // 8,
            (fx + 1) * width // 2 - width // 8,
            (fy + 1) * height // 2 - height // 8,
        )
        mean = image.crop(box).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
        found.append(
            min(
                QUADRANTS,
                key=lambda k: sum((a - b) ** 2 for a, b in zip(QUADRANTS[k], mean, strict=True)),
            )
        )
    return found


UPRIGHT = ["tl", "tr", "bl", "br"]


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "TIFF"])
@pytest.mark.parametrize("orientation", range(1, 9))
def test_orientation_is_applied_before_measuring_and_resampling(fmt, orientation):
    raw = oriented_bytes(fmt, orientation)
    loader = ImageLoader(dpi=96)
    assert loader.natural_size_bytes("photo", raw) == (160, 240)

    resampled = loader.load_bytes("photo", raw, 80.0)
    _, image = decoded(resampled.data_uri)
    assert (resampled.encoded_w, resampled.encoded_h) == image.size == (80, 120)
    assert ExifTags.Base.Orientation not in image.getexif()
    assert quadrant_colours(image) == UPRIGHT

    # At print resolution the original bytes and their tag go to Chromium,
    # which applies the tag (TIFF is converted, and turned upright, instead).
    kept = ImageLoader(dpi=96).load_bytes("photo", raw, 400.0)
    assert (kept.natural_w, kept.natural_h) == (kept.encoded_w, kept.encoded_h) == (160, 240)
    if fmt == "TIFF":
        assert quadrant_colours(decoded(kept.data_uri)[1]) == UPRIGHT
    else:
        assert base64.b64decode(kept.data_uri.split(",", 1)[1]) == raw


def test_the_reported_photo_is_upright_at_its_planned_scale(tmp_path):
    """A 1200 x 800 JPEG tagged 6 displays 800 x 1200 and must stay portrait."""
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    Image.effect_noise((1200, 800), 60).convert("RGB").save(path, quality=96, exif=exif)
    loader = ImageLoader(dpi=96)
    assert loader.natural_size(str(path)) == (800, 1200)
    loaded = loader.load(str(path), 200.0)
    _, image = decoded(loaded.data_uri)
    assert image.size == (loaded.encoded_w, loaded.encoded_h) == (200, 300)
    assert ExifTags.Base.Orientation not in image.getexif()


@pytest.mark.parametrize("orientation", [3, 6])
def test_webp_orientation_is_ignored_as_chromium_ignores_it(orientation):
    raw = oriented_bytes("WEBP", orientation)
    stored = Image.open(BytesIO(raw)).size
    loader = ImageLoader(dpi=96)
    assert loader.natural_size_bytes("photo.webp", raw) == stored
    resampled = loader.load_bytes("photo.webp", raw, stored[0] / 2)
    _, image = decoded(resampled.data_uri)
    assert image.size == (stored[0] // 2, stored[1] // 2)
    upright = Image.open(BytesIO(raw)).convert("RGB").resize(image.size)
    assert quadrant_colours(image) == quadrant_colours(upright)


def test_a_png_orientation_after_the_image_data_is_ignored_as_chromium_ignores_it():
    raw = oriented_bytes("PNG", 6)
    chunks, position = [], 8
    while position < len(raw):
        (length,) = struct.unpack(">I", raw[position : position + 4])
        chunks.append(raw[position : position + 12 + length])
        position += 12 + length
    exif = [chunk for chunk in chunks if chunk[4:8] == b"eXIf"]
    rest = [chunk for chunk in chunks if chunk[4:8] != b"eXIf"]
    late = raw[:8] + b"".join(rest[:-1]) + b"".join(exif) + rest[-1]
    assert ImageLoader().natural_size_bytes("late.png", late) == (240, 160)


def test_resampling_keeps_the_colour_profile():
    """Chromium colour-manages a tagged photo; the downsampled copy must say the same."""
    from PIL import ImageCms

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    raw = encoded_bytes("JPEG", upright_quadrants(400, 300), icc_profile=profile)
    loaded = ImageLoader(dpi=96).load_bytes("photo.jpg", raw, 100.0)
    _, image = decoded(loaded.data_uri)
    assert loaded.was_downsampled and image.info.get("icc_profile") == profile
