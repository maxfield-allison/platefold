"""Pure markdown-handling helpers: fences, metadata, headings."""

from __future__ import annotations

import base64
import math
import re

import pytest
from PIL import Image

from platefold.config import Settings, make_settings
from platefold.document import Document, marker_for
from platefold.images import ImageLoader
from platefold.ingest.markdown import extract_mermaid, heading_text, split_metadata
from platefold.layout import plan_figure
from platefold.limits import ResourceLimitError, ResourceLimits
from platefold.mermaid import MermaidRenderer
from platefold.placeholders import Placeholders

# A fixed key keeps the expected placeholder spelling readable in assertions.
MARKS = Placeholders("test")


def lines_of(text: str) -> list[str]:
    """Markdown reads better as a block of text than as a list of strings."""
    return text.split("\n")


# ---------------------------------------------------------------------------
# Mermaid fence extraction
# ---------------------------------------------------------------------------


def test_a_mermaid_fence_becomes_a_placeholder():
    body, figs = extract_mermaid(
        "intro\n\n```mermaid\nflowchart LR\n a-->b\n```\n\noutro\n", placeholders=MARKS
    )
    assert len(figs) == 1
    assert figs[0]["code"].strip() == "flowchart LR\n a-->b"
    assert MARKS.figure(0) in body
    assert "flowchart" not in body
    assert "intro" in body and "outro" in body


def test_windows_line_endings_do_not_turn_mermaid_into_literal_code():
    text = (
        "# Windows notes\r\n\r\n"
        "```mermaid\r\nflowchart LR\r\n a-->b\r\n```\r\n\r\n"
        "````markdown\r\n```mermaid\r\nthis-->stays_literal\r\n```\r\n````\r\n"
    )
    body, figs = extract_mermaid(text, placeholders=MARKS)

    assert [fig["code"] for fig in figs] == ["flowchart LR\n a-->b\n"]
    assert MARKS.figure(0) in body
    assert "this-->stays_literal" in body


def test_multiple_fences_are_numbered_in_source_order():
    text = "```mermaid\nA\n```\ntext\n```mermaid\nB\n```\n"
    body, figs = extract_mermaid(text, placeholders=MARKS)
    assert [f["code"].strip() for f in figs] == ["A", "B"]
    assert body.index(MARKS.figure(0)) < body.index(MARKS.figure(1))


def test_each_figure_records_the_nearest_preceding_heading():
    text = "# First\n\n```mermaid\nA\n```\n\n## Second\n\n```mermaid\nB\n```\n"
    _, figs = extract_mermaid(text, placeholders=MARKS)
    assert [f["head"] for f in figs] == ["First", "Second"]


def test_a_mermaid_fence_nested_in_a_longer_fence_is_left_alone():
    """Documentation *about* mermaid must not be rendered as a diagram."""
    text = "````markdown\n```mermaid\nflowchart TB\n x-->y\n```\n````\n"
    body, figs = extract_mermaid(text, placeholders=MARKS)
    assert figs == []
    assert "flowchart TB" in body
    assert "FIGURE" not in body


def test_a_python_fence_containing_backticks_is_preserved():
    text = "```python\nprint('```')\n```\n\n```mermaid\nA\n```\n"
    body, figs = extract_mermaid(text, placeholders=MARKS)
    assert len(figs) == 1
    assert "print" in body


def test_an_indented_mermaid_fence_is_still_extracted():
    body, figs = extract_mermaid(
        "  ```mermaid\n  flowchart LR\n  a-->b\n  ```\n", placeholders=MARKS
    )
    assert len(figs) == 1
    assert MARKS.figure(0) in body


def test_a_fence_with_no_info_string_is_not_a_diagram():
    _, figs = extract_mermaid("```\nflowchart LR\n```\n", placeholders=MARKS)
    assert figs == []


def test_an_unclosed_mermaid_fence_still_terminates():
    body, figs = extract_mermaid("```mermaid\nflowchart LR\n a-->b\n", placeholders=MARKS)
    assert len(figs) == 1
    assert MARKS.figure(0) in body


def test_document_without_diagrams_is_returned_unchanged():
    text = "# Title\n\nJust prose.\n"
    body, figs = extract_mermaid(text, placeholders=MARKS)
    assert figs == []
    assert body == text


# ---------------------------------------------------------------------------
# Metadata block
# ---------------------------------------------------------------------------


def test_title_and_metadata_are_lifted_out_of_the_body():
    lines = lines_of("# My Doc\n\nDocument: Thing\nVersion: 1.2.0\nOwner: Someone\n\n---\n\nBody\n")
    title, meta, rest = split_metadata(lines)
    assert title == "My Doc"
    assert meta == [("Document", "Thing"), ("Version", "1.2.0"), ("Owner", "Someone")]
    assert "Body" in "\n".join(rest)
    assert "Version:" not in "\n".join(rest)


def test_a_single_key_value_line_is_prose_not_metadata():
    """One colon line under a title is far more likely to be a sentence."""
    lines = lines_of("# My Doc\n\nNote: this is just a sentence.\n\nBody\n")
    title, meta, rest = split_metadata(lines)
    assert (title, meta) == (None, None)
    assert rest == lines


def test_document_with_no_title_keeps_its_lines():
    lines = lines_of("Just text\nMore text\n")
    title, meta, rest = split_metadata(lines)
    assert (title, meta) == (None, None)
    assert rest == lines


def test_leading_blank_lines_do_not_hide_the_title():
    lines = lines_of("\n\n# Title\n\nAuthor: one\nVersion: two\n\nBody\n")
    title, meta, _ = split_metadata(lines)
    assert title == "Title"
    assert meta == [("Author", "one"), ("Version", "two")]


def test_very_short_keys_are_not_treated_as_metadata():
    """`A: one` is far more likely to be prose than a metadata field."""
    lines = lines_of("# Title\n\nA: one\nB: two\n\nBody\n")
    title, meta, _ = split_metadata(lines)
    assert (title, meta) == (None, None)


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("# Simple", "Simple"),
        ("### Deeply nested", "Deeply nested"),
        ("## With attrs {#custom-id}", "With attrs"),
        ("#NoSpace", "NoSpace"),
        ("#   Extra   spaces   ", "Extra   spaces"),
    ],
)
def test_heading_text_strips_markup(raw, expected):
    assert heading_text(raw) == expected


def test_markers_are_unique_per_anchor_and_stable_across_runs():
    a, b = marker_for("configuration"), marker_for("configuration-1")
    assert a != b
    assert marker_for("configuration") == a


def test_markers_are_alphanumeric_so_pdf_extraction_preserves_them():
    assert marker_for("some-heading").isalnum()


# ---------------------------------------------------------------------------
# Resource envelopes
# ---------------------------------------------------------------------------


class _NoDiagramRenderer:
    def render(self, _code):
        raise AssertionError("diagram rendering was not expected")


def test_markdown_bytes_are_bounded_before_parsing(tmp_path):
    path = tmp_path / "large.md"
    path.write_text("# heading\n" + "x" * 20, encoding="utf-8")
    settings = Settings(limits=ResourceLimits(max_markdown_bytes=10))
    document = Document(str(path), str(tmp_path), settings, _NoDiagramRenderer())

    with pytest.raises(ResourceLimitError, match="Markdown file exceeds"):
        document.render()


def test_inline_data_images_count_toward_the_document_limit(tmp_path):
    path = tmp_path / "inline.md"
    path.write_text("![large](data:image/png;base64,abcdefghij)", encoding="utf-8")
    limits = ResourceLimits(max_embedded_bytes=5)
    settings = Settings(limits=limits)
    document = Document(
        str(path),
        str(tmp_path),
        settings,
        _NoDiagramRenderer(),
        ImageLoader(limits=limits),
    )

    with pytest.raises(ResourceLimitError, match="Embedded assets exceed"):
        document.render()


@pytest.mark.parametrize(
    "syntax", ["![photo]({})", "<img src='{}'>", '<IMG SRC="{}">', "<img src={}>"]
)
@pytest.mark.parametrize(
    "limit", ["max_image_bytes", "max_image_pixels", "max_gif_frames", "max_embedded_bytes"]
)
def test_inline_images_obey_limits_in_markdown_and_html(tmp_path, syntax, limit):
    image = tmp_path / "photo.gif"
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "green", "blue")]
    frames[0].save(image, save_all=True, append_images=frames[1:])
    uri = "data:image/gif;base64," + base64.b64encode(image.read_bytes()).decode()
    path = tmp_path / "notes.md"
    path.write_text(syntax.format(uri))
    settings = Settings(limits=ResourceLimits(**{limit: 2}))
    with pytest.raises(ResourceLimitError):
        Document(str(path), str(tmp_path), settings, _NoDiagramRenderer()).render()
    # Same source is still accepted under the normal envelope.
    _, info = Document(str(path), str(tmp_path), Settings(), _NoDiagramRenderer()).render()
    assert info["embedded_bytes"] == len(uri)


@pytest.mark.parametrize("syntax", ["<img src='{}'>", '<IMG SRC="{}">', "<img src={}>"])
def test_html_images_are_embedded_or_reported_for_all_attribute_forms(tmp_path, syntax):
    Image.new("RGB", (20, 20), "red").save(tmp_path / "photo.png")
    path = tmp_path / "notes.md"
    path.write_text(
        "before\rline\n\n" + syntax.format("photo.png") + "\n\n" + syntax.format("missing.png")
    )
    body, info = Document(str(path), str(tmp_path), Settings(), _NoDiagramRenderer()).render()
    assert 'src="data:image/png;base64,' in body
    assert [(code, detail) for code, detail in info["images"] if code in {"OK", "MISSING"}] == [
        ("OK", "photo.png"),
        ("MISSING", "missing.png"),
    ]
    assert "Image not found" in body


def test_quoted_html_attributes_and_code_samples_remain_intact(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text(
        'Before\n\n`<img src="missing.png">`\n\n<img alt="one > two &amp; three" src="missing.png">'
    )
    body, info = Document(str(path), str(tmp_path), Settings(), _NoDiagramRenderer()).render()
    assert [(code, detail) for code, detail in info["images"] if code == "MISSING"] == [
        ("MISSING", "missing.png")
    ]
    assert "one &gt; two &amp; three" in body
    assert '&lt;img src="missing.png"&gt;' in body


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("entity", [False, True])
def test_svg_nested_data_images_obey_the_raster_limit(tmp_path, inline, entity):
    image = tmp_path / "photo.png"
    Image.new("RGB", (20, 20), "red").save(image)
    data = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode()
    declaration = f'<!DOCTYPE svg [<!ENTITY photo "{data}">]>' if entity else ""
    svg = declaration + (
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        f'<image href="{"&photo;" if entity else data}" width="20" height="20"/></svg>'
    )
    (tmp_path / "photo.svg").write_text(svg)
    source = (
        "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
        if inline
        else "photo.svg"
    )
    path = tmp_path / "notes.md"
    path.write_text(f"![nested]({source})")
    limits = ResourceLimits(max_image_pixels=100)
    if entity:
        with pytest.raises(ValueError, match="declarations, entities"):
            Document(
                str(path), str(tmp_path), Settings(limits=limits), _NoDiagramRenderer()
            ).render()
        return
    with pytest.raises(ResourceLimitError, match="400 pixels"):
        Document(str(path), str(tmp_path), Settings(limits=limits), _NoDiagramRenderer()).render()
    _, info = Document(str(path), str(tmp_path), Settings(), _NoDiagramRenderer()).render()
    assert info["embedded_bytes"] > len(data)


@pytest.mark.parametrize(
    "markup",
    [
        "<div style=\"background: url('{uri}')\">Styled</div>",
        '<style>.sample {{background: url("{uri}")}}</style><p class="sample">Styled</p>',
        '<img srcset="{uri} 1x">',
        '<svg><image href="{uri}"/></svg>',
        "<p style=\"--picture: '{uri}';background: image-set(var(--picture) 1x)\">Styled</p>",
    ],
)
def test_authored_resource_paths_reach_image_validation(tmp_path, markup):
    image = tmp_path / "photo.png"
    Image.new("RGB", (20, 20), "red").save(image)
    uri = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode()
    path = tmp_path / "notes.md"
    path.write_text(markup.format(uri=uri))
    limits = ResourceLimits(max_image_pixels=100)
    with pytest.raises(ResourceLimitError, match="400 pixels"):
        Document(str(path), str(tmp_path), Settings(limits=limits), _NoDiagramRenderer()).render()
    _, info = Document(str(path), str(tmp_path), Settings(), _NoDiagramRenderer()).render()
    assert info["embedded_bytes"] >= len(uri)


def test_mermaid_source_is_bounded_before_browser_work():
    renderer = object.__new__(MermaidRenderer)
    renderer.settings = Settings(limits=ResourceLimits(max_mermaid_source_bytes=3))
    renderer.cancel_check = lambda: None

    with pytest.raises(ResourceLimitError, match="Mermaid source exceeds"):
        renderer.render("flowchart")


def test_cached_mermaid_svg_is_read_with_a_bound(tmp_path):
    svg = tmp_path / "diagram.svg"
    svg.write_text("<svg></svg>", encoding="utf-8")
    renderer = object.__new__(MermaidRenderer)
    renderer.settings = Settings(limits=ResourceLimits(max_mermaid_svg_bytes=4))
    renderer.cache_dir = str(tmp_path)

    with pytest.raises(ResourceLimitError, match="Rendered Mermaid SVG exceeds"):
        renderer.svg_text({"svg_path": svg.name})


def test_cached_mermaid_svg_cannot_escape_the_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (tmp_path / "private.svg").write_text("<svg>private</svg>", encoding="utf-8")
    renderer = object.__new__(MermaidRenderer)
    renderer.settings = Settings()
    renderer.cache_dir = str(cache)

    with pytest.raises(ResourceLimitError, match="outside the cache"):
        renderer.svg_text({"svg_path": "../private.svg"})


def test_tiled_figures_report_their_final_figure_numbers(tmp_path):
    from dataclasses import replace

    from platefold.config import make_settings

    def svg(name: str, width: int, height: int) -> None:
        (tmp_path / name).write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"/>'
        )

    svg("small.svg", 100, 100)
    svg("wide.svg", 1500, 300)
    svg("tall.svg", 300, 2000)
    source = tmp_path / "doc.md"
    source.write_text(
        "# Doc\n\n![Small](small.svg)\n\n![Wide](wide.svg)\n\n![Small](small.svg)\n\n"
        "![Tall](tall.svg)\n"
    )
    settings = make_settings("letter", output_mode="tiled")
    html, info = Document(str(source), str(tmp_path), settings, renderer=None).render_html()
    assert info["figures"] == 4
    assert info["tiled_figures"] == [
        {"figure": 2, "columns": 3, "rows": 1, "sheets": 3, "scale": 1.0},
        {"figure": 4, "columns": 1, "rows": 3, "sheets": 3, "scale": 1.0},
    ]
    # Every sheet repeats its own figure's number; no placeholder survives.
    assert html.count('<span class="tile-name">Figure 2 &middot; Wide</span>') == 3
    assert html.count('<span class="tile-name">Figure 4 &middot; Tall</span>') == 3
    assert "@@" not in html
    # Other output modes report no tiles.
    online = replace(settings, output_mode="online")
    _, info = Document(str(source), str(tmp_path), online, renderer=None).render_html()
    assert info["tiled_figures"] == []


class _FixedDiagramRenderer:
    """Every Mermaid fence renders to one fractional-size diagram."""

    def __init__(self, width: float, height: float):
        self.rec = {
            "svg_path": "fixed.svg",
            "viewBox": f"0 0 {width} {height}",
            "w": width,
            "h": height,
        }

    def render(self, _code):
        return dict(self.rec)

    def svg_text(self, _rec):
        return (
            '<svg id="m1" xmlns="http://www.w3.org/2000/svg" width="1" height="1" '
            'viewBox="0 0 1 1"><title>Flow</title><desc>A flow</desc><rect/></svg>'
        )


@pytest.mark.parametrize(
    "mode,width,height",
    [("print", 800.3, 300.7), ("online", 500.3, 200.7), ("tiled", 3000.5, 3000.5)],
)
def test_diagram_height_is_whole_pixels_so_its_width_prints_as_planned(
    tmp_path, mode, width, height
):
    """Chromium paints an SVG box on whole pixels. A height rounded down would
    make the aspect-ratio fit shrink the width, so it is rounded up; tiles keep
    their exact natural size, because their sheets are cut from it."""
    path = tmp_path / "flow.md"
    path.write_text("```mermaid\nflowchart LR\n  a --> b\n```\n", encoding="utf-8")
    settings = make_settings("letter", output_mode=mode)
    html, _ = Document(
        str(path), str(tmp_path), settings, _FixedDiagramRenderer(width, height)
    ).render()

    plan = plan_figure(width, height, settings)
    sizes = re.findall(r'<svg\b[^>]*? width="([^"]+)" height="([^"]+)" id="fig', html)
    assert sizes
    expected_width = f"{width * plan['scale']:.4f}"
    if plan["mode"] == "tiled":
        expected_height = f"{height:.4f}"
    else:
        expected_height = str(math.ceil(height * plan["scale"] - 1e-6))
    assert set(sizes) == {(expected_width, expected_height)}
