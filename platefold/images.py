"""Load, downsample and embed referenced images.

Two jobs beyond "read the file":

**Downsampling.** Images are embedded as base64 inside the HTML, so whatever
pixels go in end up in the PDF. A 4000px screenshot placed 180mm wide carries
roughly eight times the pixels print can use; resampling it to the target DPI
at its *placed* size cuts file size substantially without touching layout,
because the CSS width is unchanged.

**Browser formats.** Chromium draws the embedded data URI, so an image must
arrive in a format it decodes. TIFF, BMP, ICO and any other raster Pillow
reads but Chromium does not are always re-encoded as PNG, even when that is
larger. That conversion is required; downsampling stays an optional saving.

**Orientation.** Chromium applies an image's EXIF orientation when it draws
a JPEG, PNG or AVIF, but not a WebP. Measurement and re-encoding use the same
rule, so a photo is planned, resampled and printed in its displayed geometry
whichever path it takes.

**Caching.** The exporter renders each document twice — once to measure where
headings land, once with real page numbers — and without a cache every image
is re-read, re-resampled and re-encoded on both passes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import os
import re
import urllib.parse
import warnings
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from PIL import ExifTags, Image, ImageOps

from .limits import ResourceLimitError, ResourceLimits

# CSS pixels per inch, as Chromium lays out.
CSS_PX_PER_INCH = 96.0

# Keep cross-document reuse without retaining an entire image-heavy corpus.
# One current document can still exceed this; the bound covers completed
# documents whose encoded data would otherwise remain reachable until exit.
DEFAULT_IMAGE_CACHE_BYTES = 64 * 1024 * 1024

MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}

# Rasters Chromium decodes, delivered as they are, by Pillow format. Anything
# else Pillow can read is converted to PNG before embedding. BMP and ICO are
# decodable, but Chromium and Pillow differ on some BMP variants and on which
# ICO frame to draw; PNG is lossless and nearly always smaller, so they are
# converted too.
BROWSER_RASTERS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",  # a camera JPEG with extra frames; browsers draw the first
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "AVIF": "image/avif",
}

# Every raster Chromium can decode. Only an inline image nested where it cannot
# be re-encoded, such as inside an SVG, is ever delivered as BMP or ICO.
BROWSER_DECODABLE = {*BROWSER_RASTERS, "BMP", "DIB", "ICO"}

# Pillow format to re-encode a downsampled browser raster as.
_RESAMPLED_FORMAT = {"JPEG": "JPEG", "MPO": "JPEG", "WEBP": "WEBP"}
_ENCODED_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_SAVE_MODES = {
    "PNG": {"1", "L", "LA", "I", "I;16", "P", "RGB", "RGBA"},
    "JPEG": {"L", "RGB"},
    "WEBP": {"RGB", "RGBA"},
}

# Chromium draws a WebP's stored pixels and ignores its EXIF orientation.
_IGNORES_ORIENTATION = {"WEBP"}

# EXIF orientation -> the transpose that shows the stored pixels upright.
_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


@dataclass(frozen=True)
class _Raster:
    """What an opened raster needs for planning: its displayed size and format."""

    size: tuple[int, int]
    format: str | None = None
    orientation: int = 1


def _orientation(image: Image.Image) -> int:
    """The EXIF orientation Chromium would apply when drawing this image.

    Chromium reads a PNG's eXIf chunk only when it precedes the image data,
    which is also the only place Pillow reads it without decoding every pixel.
    """
    if image.format in _IGNORES_ORIENTATION:
        return 1
    if image.format == "PNG" and "exif" not in image.info:
        return 1
    try:
        value = image.getexif().get(ExifTags.Base.Orientation, 1)
    except Exception:  # malformed metadata never costs the image
        return 1
    return value if value in _TRANSPOSE else 1


def _stored_size(image: Image.Image) -> tuple[int, int]:
    """Pixel dimensions as stored. Recent Pillow reports a TIFF's displayed size."""
    if image.format == "TIFF":
        tags = getattr(image, "tag_v2", {})
        width, length = tags.get(256), tags.get(257)
        if isinstance(width, int) and isinstance(length, int):
            return width, length
    return image.size


def _describe(image: Image.Image) -> _Raster:
    width, height = image.size
    orientation = _orientation(image)
    # A quarter turn, possibly mirrored, swaps the axes, unless Pillow did.
    if orientation >= 5 and image.size == _stored_size(image):
        width, height = height, width
    return _Raster((width, height), image.format, orientation)


def _upright(image: Image.Image, orientation: int) -> Image.Image:
    """The loaded image as displayed under ``orientation``."""
    if image.format == "TIFF":
        # Recent Pillow turns a TIFF upright as it loads and drops the tag;
        # this applies the tag only where an older Pillow left it in place.
        return ImageOps.exif_transpose(image)
    if orientation in _TRANSPOSE:
        return image.transpose(_TRANSPOSE[orientation])
    return image


def _safe_svg_root(raw: bytes):
    try:
        return DefusedET.fromstring(
            raw,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DefusedXmlException, DefusedET.ParseError, ValueError) as exc:
        raise ValueError("SVG declarations, entities, or malformed XML are not supported") from exc


@dataclass(frozen=True)
class LoadedImage:
    """An image ready to drop into the document."""

    data_uri: str
    natural_w: int
    natural_h: int
    encoded_w: int
    encoded_h: int
    original_bytes: int
    encoded_bytes: int

    @property
    def was_downsampled(self) -> bool:
        return self.encoded_w < self.natural_w


def target_pixel_width(placed_px: float, dpi: int) -> int:
    """Pixels needed to hit `dpi` for an image placed `placed_px` CSS px wide."""
    return max(1, round(placed_px / CSS_PX_PER_INCH * dpi))


class ImageLoader:
    """Read and downsample images with a byte-bounded cross-document cache."""

    def __init__(
        self,
        dpi: int = 300,
        max_cache_bytes: int = DEFAULT_IMAGE_CACHE_BYTES,
        limits: ResourceLimits | None = None,
    ):
        if max_cache_bytes < 0:
            raise ValueError("max_cache_bytes must be nonnegative")
        self.dpi = dpi
        self.max_cache_bytes = max_cache_bytes
        self.limits = limits or ResourceLimits()
        self.limits.validate()
        self._rasters: dict[str, _Raster] = {}
        self._encoded: OrderedDict[tuple[str, int], LoadedImage] = OrderedDict()
        self._cached_bytes = 0

    @property
    def cached_bytes(self) -> int:
        return self._cached_bytes

    def _stamp(self, path: str) -> str:
        """Identity of a file on disk, so an edited image isn't served stale."""
        try:
            st = os.stat(path)
            return "%s:%d:%d" % (path, st.st_size, st.st_mtime_ns)
        except OSError:
            return path

    def _check_file_bytes(self, path: str) -> None:
        try:
            size = os.stat(path).st_size
        except OSError:
            return
        if size > self.limits.max_image_bytes:
            raise ResourceLimitError(
                f"Image exceeds the {self.limits.max_image_bytes}-byte limit: {path}"
            )

    def _data_uri_payload(self, uri: str) -> tuple[str, bytes]:
        """An inline image's declared MIME type and bytes, inside the image envelope."""
        header, separator, payload = uri.partition(",")
        if not separator:
            raise ValueError("Invalid inline data image: missing payload")
        mime = header[5:].split(";", 1)[0].lower()
        if mime not in MIME.values():
            raise ValueError("Unsupported inline data image format")
        limit = self.limits.max_image_bytes
        encoded = urllib.parse.unquote_to_bytes(payload)
        if header.lower().endswith(";base64"):
            encoded = b"".join(encoded.split())
            if len(encoded) > ((limit + 2) // 3) * 4:
                raise ResourceLimitError(f"Inline image exceeds the {limit}-byte limit")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except binascii.Error as exc:
                raise ValueError("Invalid inline data image encoding") from exc
        else:
            raw = encoded
        if len(raw) > limit:
            raise ResourceLimitError(f"Inline image exceeds the {limit}-byte limit")
        return mime, raw

    def validate_data_uri(
        self,
        uri: str,
        *,
        reserve: Callable[[int, str], None] | None = None,
        undecodable: Callable[[str], None] | None = None,
    ) -> None:
        """Apply the image envelope before Chromium decodes an inline asset.

        A raster in a format Chromium cannot draw, such as TIFF, is passed to
        ``undecodable`` by Pillow format name, including one nested in an SVG.
        Without that callback it is an error, since it would print as nothing.
        """
        mime, raw = self._data_uri_payload(uri)
        if mime == "image/svg+xml":
            from .html_resources import preflight

            normalized = DefusedET.tostring(_safe_svg_root(raw), encoding="unicode")
            preflight(
                normalized,
                lambda nested: self.validate_data_uri(
                    nested, reserve=reserve, undecodable=undecodable
                ),
                reserve or (lambda _amount, _source: None),
                include_img_src=True,
            )
            return
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                im = Image.open(BytesIO(raw))
            with im:
                self._validate_raster(im, "inline data image")
                image_format = im.format or "unknown"
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise ResourceLimitError("Inline image dimensions are unsafe to decode") from exc
        if image_format not in BROWSER_DECODABLE:
            if undecodable is None:
                raise ValueError(f"Inline {image_format} image cannot be displayed by the renderer")
            undecodable(image_format)

    def displayable_data_uri(self, uri: str) -> str:
        """Re-encode an inline raster as PNG unless Chromium draws it as delivered.

        Call after ``validate_data_uri``. SVG, browser rasters and bytes Pillow
        cannot read are returned unchanged; the render check reports the last.
        """
        mime, raw = self._data_uri_payload(uri)
        if mime == "image/svg+xml":
            return uri
        raster = self._probe(BytesIO(raw), "inline data image")
        if not raster.size[0] or raster.format in BROWSER_RASTERS:
            return uri
        data = self._resample(raw, raster.size, "PNG", raster.orientation)
        if data is None:
            return uri
        return "data:image/png;base64," + base64.b64encode(data).decode()

    def _probe(self, source: str | BytesIO, label: str) -> _Raster:
        """Displayed size, format and orientation; zero size when Pillow cannot read it."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image = Image.open(source)
            with image:
                self._validate_raster(image, label)
                return _describe(image)
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise ResourceLimitError(f"Image dimensions are unsafe to decode: {label}") from exc
        except ResourceLimitError:
            raise
        except Exception:
            return _Raster((0, 0))

    def _raster(self, path: str) -> _Raster:
        self._check_file_bytes(path)
        stamp = self._stamp(path)
        hit = self._rasters.get(stamp)
        if hit is not None:
            return hit
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        # The planner falls back to the text column width for an SVG.
        raster = _Raster((0, 0)) if ext == "svg" else self._probe(path, path)
        self._rasters[stamp] = raster
        return raster

    def _raster_bytes(self, name: str, raw: bytes) -> _Raster:
        if len(raw) > self.limits.max_image_bytes:
            raise ResourceLimitError(
                f"Image exceeds the {self.limits.max_image_bytes}-byte limit: {name}"
            )
        stamp = "memory:%s:%s" % (name, hashlib.sha256(raw).hexdigest())
        hit = self._rasters.get(stamp)
        if hit is not None:
            return hit
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        raster = _Raster((0, 0)) if ext == "svg" else self._probe(BytesIO(raw), name)
        self._rasters[stamp] = raster
        return raster

    def natural_size(self, path: str) -> tuple[int, int]:
        """Displayed pixel dimensions, used to plan placement before encoding."""
        return self._raster(path).size

    def natural_size_bytes(self, name: str, raw: bytes) -> tuple[int, int]:
        """Measure a source-carried image without writing it to disk."""
        return self._raster_bytes(name, raw).size

    def print_size(self, path: str) -> tuple[float, float]:
        """Measure SVG roots for fixed-paper exports; keep legacy sizing online."""
        if not path.lower().endswith(".svg"):
            return self.natural_size(path)
        self._check_file_bytes(path)
        with open(path, "rb") as handle:
            raw = handle.read(self.limits.max_image_bytes + 1)
        if len(raw) > self.limits.max_image_bytes:
            raise ResourceLimitError("SVG exceeds the image byte limit")
        return svg_dimensions(raw)

    def print_size_bytes(self, name: str, raw: bytes) -> tuple[float, float]:
        if name.lower().endswith(".svg"):
            return svg_dimensions(raw)
        return self.natural_size_bytes(name, raw)

    def load(self, path: str, placed_px: float) -> LoadedImage:
        """Return the image encoded for embedding at its placed size."""
        self._check_file_bytes(path)
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        target_w = target_pixel_width(placed_px, self.dpi)
        cache_key = (self._stamp(path), target_w)
        hit = self._encoded.get(cache_key)
        if hit is not None:
            self._encoded.move_to_end(cache_key)
            return hit

        with open(path, "rb") as fh:
            raw = fh.read(self.limits.max_image_bytes + 1)
        if len(raw) > self.limits.max_image_bytes:
            raise ResourceLimitError(
                f"Image exceeds the {self.limits.max_image_bytes}-byte limit: {path}"
            )
        result = self._loaded(ext, raw, self._raster(path), target_w)
        self._remember(cache_key, result)
        return result

    def load_bytes(self, name: str, raw: bytes, placed_px: float) -> LoadedImage:
        """Downsample and encode an image already carried by an input package."""
        if len(raw) > self.limits.max_image_bytes:
            raise ResourceLimitError(
                f"Image exceeds the {self.limits.max_image_bytes}-byte limit: {name}"
            )
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        target_w = target_pixel_width(placed_px, self.dpi)
        stamp = "memory:%s:%s" % (name, hashlib.sha256(raw).hexdigest())
        cache_key = (stamp, target_w)
        hit = self._encoded.get(cache_key)
        if hit is not None:
            self._encoded.move_to_end(cache_key)
            return hit
        result = self._loaded(ext, raw, self._raster_bytes(name, raw), target_w)
        self._remember(cache_key, result)
        return result

    def _loaded(self, ext: str, raw: bytes, raster: _Raster, target_w: int) -> LoadedImage:
        data, enc_w, enc_h, mime = self._encode(ext, raw, raster, target_w)
        nat_w, nat_h = raster.size
        return LoadedImage(
            data_uri="data:%s;base64,%s" % (mime, base64.b64encode(data).decode()),
            natural_w=nat_w,
            natural_h=nat_h,
            encoded_w=enc_w,
            encoded_h=enc_h,
            original_bytes=len(raw),
            encoded_bytes=len(data),
        )

    def _validate_raster(self, image: Image.Image, path: str) -> None:
        width, height = image.size
        pixels = width * height
        if pixels > self.limits.max_image_pixels:
            raise ResourceLimitError(
                f"Image has {pixels} pixels; limit is {self.limits.max_image_pixels}: {path}"
            )
        frames = int(getattr(image, "n_frames", 1))
        if frames > self.limits.max_gif_frames:
            raise ResourceLimitError(
                f"Image has {frames} frames; limit is {self.limits.max_gif_frames}: {path}"
            )

    def _remember(self, key: tuple[str, int], image: LoadedImage) -> None:
        cost = len(image.data_uri)
        if cost > self.max_cache_bytes:
            return
        while self._encoded and self._cached_bytes + cost > self.max_cache_bytes:
            _, evicted = self._encoded.popitem(last=False)
            self._cached_bytes -= len(evicted.data_uri)
        self._encoded[key] = image
        self._cached_bytes += cost

    def _encode(
        self, ext: str, raw: bytes, raster: _Raster, target_w: int
    ) -> tuple[bytes, int, int, str]:
        """Bytes, encoded size and MIME type to embed.

        Conversion to a browser format is required and happens at any size.
        Downsampling is optional: its result is kept only when it is smaller.
        """
        nat_w, nat_h = raster.size
        delivered = BROWSER_RASTERS.get(raster.format or "", MIME.get(ext, "image/png"))
        if ext == "svg" or not nat_w or not nat_h or raster.format == "GIF":
            # SVG is vector, and resampling a possibly animated GIF would
            # flatten it. Bytes Pillow cannot read are embedded as they are;
            # the render check reports them if Chromium cannot draw them.
            return raw, nat_w, nat_h, delivered
        required = raster.format not in BROWSER_RASTERS
        if nat_w <= target_w and not required:
            return raw, nat_w, nat_h, delivered  # already at or below print resolution

        width, height = nat_w, nat_h
        if nat_w > target_w:
            width, height = target_w, max(1, round(nat_h * target_w / nat_w))
        output_format = "PNG" if required else _RESAMPLED_FORMAT.get(raster.format or "", "PNG")
        data = self._resample(raw, (width, height), output_format, raster.orientation)
        if data is None or (not required and len(data) >= len(raw)):
            # Keep the original: Chromium applies its orientation tag as the
            # re-encoding would have, and the render check reports a failed
            # conversion's format if Chromium cannot draw it.
            return raw, nat_w, nat_h, delivered
        return data, width, height, _ENCODED_MIME[output_format]

    def _resample(
        self, raw: bytes, size: tuple[int, int], output_format: str, orientation: int
    ) -> bytes | None:
        """Encode the displayed image at ``size``, or None when encoding fails.

        ``size`` is in displayed geometry: the stored pixels are turned upright
        first and the output carries no orientation tag.
        """
        try:
            # The source bytes are already in memory for embedding/fallback.
            # Reopening the path would read every downsampled image twice.
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                im = Image.open(BytesIO(raw))
            with im:
                self._validate_raster(im, "embedded raster")
                im.load()
                picture = _upright(im, orientation)
                if picture.size != size:
                    picture = picture.resize(size, Image.Resampling.LANCZOS)
                if picture.mode not in _SAVE_MODES[output_format]:
                    alpha = "A" in picture.getbands() or "transparency" in picture.info
                    target = "RGBA" if alpha and output_format != "JPEG" else "RGB"
                    picture = picture.convert(target)
                buf = BytesIO()
                save_args: dict[str, bool | int | bytes] = {"optimize": True}
                profile = im.info.get("icc_profile")
                if profile and _colour_space(im.mode) == _colour_space(picture.mode):
                    save_args["icc_profile"] = profile
                if output_format == "JPEG":
                    save_args["quality"] = 88
                    save_args["progressive"] = True
                picture.save(buf, format=output_format, **save_args)
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise ResourceLimitError("Image dimensions are unsafe to decode") from exc
        except ResourceLimitError:
            raise
        except Exception:
            # Downsampling is an optimisation, never a reason to lose an
            # image; the caller falls back to the bytes it already has.
            return None
        return buf.getvalue()


def _colour_space(mode: str) -> str:
    """The colour space an ICC profile for this mode describes."""
    if mode.startswith(("RGB", "P")):
        return "RGB"
    if mode.startswith("CMYK"):
        return "CMYK"
    if mode in {"1", "L", "LA", "La", "F"} or mode.startswith("I"):
        return "L"
    return mode


def _svg_length(value: str) -> float:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(px|mm|cm|in|pt|pc)?\s*", value)
    if not match:
        return 0.0
    units = {None: 1, "px": 1, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96, "pt": 96 / 72, "pc": 16}
    result = float(match[1]) * units[match[2]]
    return result if math.isfinite(result) else 0.0


def svg_dimensions(raw: bytes) -> tuple[float, float]:
    """Resolve absolute SVG dimensions and viewBox aspect ratios in CSS pixels."""
    root = _safe_svg_root(raw)
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError("Expected an SVG root")
    width, height = (_svg_length(root.get(key, "")) for key in ("width", "height"))
    try:
        view = [float(v) for v in re.split(r"[\s,]+", root.get("viewBox", "").strip())]
    except ValueError:
        view = []
    if len(view) == 4 and all(math.isfinite(v) for v in view) and min(view[2:]) > 0:
        vw, vh = view[2:]
        if not width and not height:
            width, height = vw, vh
        elif not width:
            width = height * vw / vh
        elif not height:
            height = width * vh / vw
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise ValueError("SVG needs a positive width and height or viewBox for printing")
    return width, height
