"""End-to-end exports against a real headless Chromium.

Nothing here is mocked. Assertions inspect printed PDFs and rendered diagram
cache entries to catch pagination, blank pages, mixed page sizes, text-layer
errors, and stale diagrams.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pypdf import PdfReader

from platefold.document import marker_for

pytestmark = pytest.mark.integration

# Points per millimetre, for interpreting a PDF MediaBox.
PT_PER_MM = 72.0 / 25.4


def page_sizes(pdf: Path) -> list[tuple[float, float]]:
    """Rounded (width, height) in mm for each page."""
    reader = PdfReader(str(pdf))
    return [
        (round(float(p.mediabox.width) / PT_PER_MM), round(float(p.mediabox.height) / PT_PER_MM))
        for p in reader.pages
    ]


def all_text(pdf: Path) -> str:
    reader = PdfReader(str(pdf))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


def outline_pages(pdf: Path) -> list[tuple[str, int]]:
    """Flatten the PDF outline into (title, 1-based page number)."""
    reader = PdfReader(str(pdf))
    found: list[tuple[str, int]] = []

    def walk(items):
        for item in items:
            if isinstance(item, list):
                walk(item)
            else:
                found.append((item.title, reader.get_destination_page_number(item) + 1))

    walk(reader.outline)
    return found


# ---------------------------------------------------------------------------
# The core promise: a wide diagram gets a bigger page, not a smaller rendering.
# ---------------------------------------------------------------------------


def test_a_wide_diagram_lands_on_a_larger_page_than_the_body(fixture_doc, run_export):
    src = fixture_doc("wide_diagram.md")
    code, out = run_export(src)
    assert code == 0

    pdf = out / "wide_diagram.pdf"
    sizes = page_sizes(pdf)
    assert len(set(sizes)) > 1, "expected mixed page sizes; got %r" % sizes

    body = (216, 279)  # US Letter portrait, rounded to mm
    assert body in sizes
    oversized = [s for s in sizes if s[0] > body[0]]
    assert oversized, "the wide diagram did not get a wider page"


def test_the_system_browser_paginates_identically_to_the_bundled_one(fixture_doc, run_export):
    """The slim build renders with the installed Edge instead of a bundled
    Chromium. Both are Blink, so the placement ladder must land on the same
    page sizes — if they diverge, the slim build is not a safe default.
    """
    from platefold.browser import find_bundled, find_system

    if not find_system("edge"):
        pytest.skip("Microsoft Edge is not installed")
    if not find_bundled():
        pytest.skip("this is a slim build; no bundled engine to compare against")

    src = fixture_doc("wide_diagram.md")
    code, bundled = run_export(src, "--browser", "bundled")
    assert code == 0
    try:
        code, edge = run_export(src, "--browser", "edge")
    except SystemExit as exc:
        # Edge refuses to start headless under some accounts, such as a
        # Windows service account; the export reports that and stops.
        if "No installed browser passed the rendering check" in str(exc):
            pytest.skip(f"Microsoft Edge cannot render headless here: {exc}")
        raise
    assert code == 0

    assert page_sizes(bundled / "wide_diagram.pdf") == page_sizes(edge / "wide_diagram.pdf")


def test_uniform_preset_keeps_every_page_the_same_size(fixture_doc, run_export):
    src = fixture_doc("wide_diagram.md")
    code, out = run_export(src, "--preset", "uniform-letter")
    assert code == 0
    assert len(set(page_sizes(out / "wide_diagram.pdf"))) == 1


def test_a4_preset_produces_a4_body_pages(fixture_doc, run_export):
    src = fixture_doc("wide_diagram.md")
    code, out = run_export(src, "--preset", "a4")
    assert code == 0
    assert (210, 297) in page_sizes(out / "wide_diagram.pdf")


@pytest.mark.parametrize(
    "paper,dimensions", [("letter", (216, 279)), ("a4", (210, 297)), ("a0", (841, 1189))]
)
def test_print_uses_only_the_chosen_sheet(fixture_doc, run_export, paper, dimensions):
    src = fixture_doc("wide_diagram.md")
    code, out = run_export(src, "--output-mode", "print", "--preset", paper)
    assert code == 0
    assert set(page_sizes(out / "wide_diagram.pdf")) == {dimensions}
    assert "Figure 1" in all_text(out / "wide_diagram.pdf")


@pytest.mark.parametrize("margins", ["full", "none", "bottom-right", "custom"])
def test_tiles_keep_vector_scale_and_join_without_lost_content(tmp_path, run_export, margins):
    import pypdfium2 as pdfium

    from platefold.config import make_settings
    from platefold.layout import plan_figure

    # A synthetic coloured grid lets us check the source coordinate of every
    # tile after Chromium lays it out and prints it, including both seams.
    source = tmp_path / "grid.md"
    squares = "".join(
        f'<rect x="{x}" y="{y}" width="100" height="100" fill="{("#ff0000" if (x + y) // 100 % 2 else "#0000ff")}"/>'
        for y in range(0, 1400, 100)
        for x in range(0, 1200, 100)
    )
    (tmp_path / "grid.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1400" viewBox="0 0 1200 1400">'
        + squares
        + "</svg>"
    )
    source.write_text("# Assembly\n\n![Grid](grid.svg)\n\n# After the grid\n\nEnd of document.\n")
    extra = (
        ["--margins", margins]
        if margins != "custom"
        else [
            "--page-width",
            "230.5",
            "--page-height",
            "340.25",
            "--orientation",
            "landscape",
            "--margin-edges",
            "3",
            "17",
            "21",
            "7",
        ]
    )
    code, out = run_export(source, "--output-mode", "tiled", "--preset", "letter", *extra)
    assert code == 0
    path = out / "grid.pdf"
    reader = PdfReader(path)
    assert set(page_sizes(path)) == ({(340, 230)} if margins == "custom" else {(216, 279)})
    settings = make_settings(
        "letter",
        output_mode="tiled",
        **(
            {
                "page_width": 230.5,
                "page_height": 340.25,
                "orientation": "landscape",
                "margin_edges": [3, 17, 21, 7],
            }
            if margins == "custom"
            else {"margin_mode": margins}
        ),
    )
    plan = plan_figure(1200, 1400, settings)
    sheets = plan["columns"] * plan["rows"]
    assert len(reader.pages) == 2 + sheets
    # The caption, then one label on every sheet.
    assert all_text(path).count("Figure 1") == 1 + sheets
    assert f"Tile {sheets} of {sheets}" in reader.pages[sheets].extract_text()
    assert "End of document." in reader.pages[-1].extract_text()
    assert outline_pages(path)[-1][1] == len(reader.pages)
    left, top = plan["left"], plan["top"]
    step_x, step_y = plan["width"] - plan["overlap"], plan["height"] - plan["overlap"]
    with pdfium.PdfDocument(path) as pdf:
        for row in range(plan["rows"]):
            for col in range(plan["columns"]):
                index = 1 + row * plan["columns"] + col
                assert not list(reader.pages[index].images), "diagram was rasterized"
                page = pdf[index]
                bitmap = page.render(scale=96 / 72)
                image = bitmap.to_pil()
                try:
                    for dx, dy in [(35, 35), (235, 235), (435, 135)]:
                        x, y = col * step_x + dx, row * step_y + dy
                        expected = (
                            tuple(bytes.fromhex(settings.theme.page.lstrip("#")))
                            if x >= 1200 or y >= 1400
                            else ((255, 0, 0) if (x // 100 + y // 100) % 2 else (0, 0, 255))
                        )
                        actual = image.getpixel((left + dx, top + dy))[:3]
                        assert actual == expected, (margins, row, col, x, y, actual, expected)
                finally:
                    image.close()
                    bitmap.close()
                    page.close()


# ---------------------------------------------------------------------------
# Blank pages — the classic regression when plate CSS changes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["wide_diagram.md", "duplicate_headings.md"])
def test_export_contains_no_blank_pages(fixture, fixture_doc, run_export):
    from platefold.blankcheck import blank_pages

    src = fixture_doc(fixture)
    code, out = run_export(src)
    assert code == 0
    pdf = out / fixture.replace(".md", ".pdf")
    assert blank_pages(str(pdf)) == []


def test_no_blank_pages_across_every_theme(fixture_doc, run_export):
    """Themes change borders and fills, which is exactly what shifts layout."""
    from platefold.blankcheck import blank_pages

    for theme in (
        "default",
        "mono",
        "bristol",
        "bond",
        "newsprint",
        "graphite",
        "midnight",
        "carbon",
    ):
        src = fixture_doc("wide_diagram.md")
        code, out = run_export(src, "--theme", theme)
        assert code == 0
        assert blank_pages(str(out / "wide_diagram.pdf")) == [], f"theme {theme}"


# ---------------------------------------------------------------------------
# Heading -> page mapping via invisible markers.
# ---------------------------------------------------------------------------


def test_repeated_headings_map_to_their_own_pages(fixture_doc, run_export):
    """Text matching collapsed these onto the first occurrence."""
    src = fixture_doc("duplicate_headings.md")
    code, out = run_export(src)
    assert code == 0

    entries = outline_pages(out / "duplicate_headings.pdf")
    config_pages = [page for title, page in entries if title.strip() == "Configuration"]
    assert len(config_pages) == 3, f"expected three Configuration entries, got {entries}"
    assert len(set(config_pages)) == 3, f"headings collapsed onto the same page: {config_pages}"
    assert config_pages == sorted(config_pages)


def test_outline_covers_every_heading_in_document_order(fixture_doc, run_export):
    src = fixture_doc("duplicate_headings.md")
    code, out = run_export(src)
    assert code == 0
    entries = outline_pages(out / "duplicate_headings.pdf")
    titles = [t.strip() for t, _ in entries]
    assert titles[0] == "Alpha section"
    assert "Delta section" in titles
    pages = [p for _, p in entries]
    assert pages == sorted(pages), "outline is out of document order"


def test_skipped_heading_level_keeps_its_current_section(tmp_path, run_export):
    source = tmp_path / "sections.md"
    source.write_text(
        "# A\n\nFirst section.\n\n## A1\n\nFirst child.\n\n"
        "# B\n\nSecond section.\n\n### B1\n\nSkipped heading level.\n",
        encoding="utf-8",
    )
    code, out = run_export(source, "--no-toc")
    assert code == 0
    with PdfReader(out / "sections.pdf") as reader:
        outline = reader.outline
        assert [outline[0].title, outline[2].title] == ["A", "B"]
        assert [item.title for item in outline[1]] == ["A1"]
        assert [item.title for item in outline[3]] == ["B1"]


def test_delivered_pdf_contains_no_measuring_markers(fixture_doc, run_export):
    """Markers exist only on the throwaway measuring pass."""
    src = fixture_doc("duplicate_headings.md")
    code, out = run_export(src)
    assert code == 0

    text = all_text(out / "duplicate_headings.pdf")
    assert marker_for("configuration") not in text
    assert not re.search(r"\bZq[0-9a-f]{8}\b", text), "a page marker leaked into the output"


def test_bookmarks_can_be_disabled(fixture_doc, run_export):
    src = fixture_doc("duplicate_headings.md")
    code, out = run_export(src, "--no-bookmarks")
    assert code == 0
    assert outline_pages(out / "duplicate_headings.pdf") == []


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def test_figures_are_numbered_in_reading_order(image_doc, run_export):
    """An image before a diagram must still be Figure 1."""
    doc, _ = image_doc
    code, out = run_export(doc)
    assert code == 0

    text = all_text(out / "ordering.pdf")
    assert "Figure 1" in text and "Figure 2" in text
    # The image's caption carries the first heading, the diagram's the second.
    fig1 = text.index("Figure 1")
    fig2 = text.index("Figure 2")
    assert fig1 < fig2
    assert "An image comes first" in text[: fig2 + 200]


def test_a_fence_documenting_mermaid_is_not_rendered_as_a_diagram(fixture_doc, run_export):
    src = fixture_doc("wide_diagram.md")
    code, out = run_export(src)
    assert code == 0

    pdf = out / "wide_diagram.pdf"
    text = all_text(pdf)
    # The nested example survives as literal text...
    assert "should_not_render" in text
    # ...and only the two real diagrams were numbered.
    assert "Figure 2" in text
    assert "Figure 3" not in text


def test_windows_markdown_line_endings_render_supported_mermaid(tmp_path, run_export):
    src = tmp_path / "windows.md"
    fixture = (Path(__file__).parent / "fixtures" / "wide_diagram.md").read_text(encoding="utf-8")
    src.write_bytes(fixture.replace("\n", "\r\n").encode())

    code, out = run_export(src)

    assert code == 0
    report = json.loads((out / "_export-report.json").read_text(encoding="utf-8"))
    assert report["documents"][0]["figures"] == 2
    text = all_text(out / "windows.pdf")
    assert "should_not_render" in text
    assert "Figure 2" in text and "Figure 3" not in text


def test_missing_images_are_reported_loudly_not_silently_skipped(tmp_path, run_export):
    doc = tmp_path / "src" / "broken.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Broken\n\n![alt text](does-not-exist.png)\n", encoding="utf-8")

    code, out = run_export(doc)
    assert code == 0
    text = all_text(out / "broken.pdf")
    assert "Image not found" in text
    assert "does-not-exist.png" in text

    report = (out / "_export-report.json").read_text(encoding="utf-8")
    assert "does-not-exist.png" in report


# ---------------------------------------------------------------------------
# Document properties and images
# ---------------------------------------------------------------------------


def test_pdf_document_properties_come_from_the_metadata_block(fixture_doc, run_export):
    src = fixture_doc("wide_diagram.md")
    code, out = run_export(src)
    assert code == 0

    meta = PdfReader(str(out / "wide_diagram.pdf")).metadata
    assert meta.title == "Wide Diagram Fixture"
    assert meta.author == "Test Suite"
    assert "2.1.0" in (meta.get("/Keywords") or "")


def test_an_image_placed_near_full_size_keeps_its_pixels(image_doc, run_export):
    """Downsampling must not fire when the image is already under-resolution.

    The placement ladder promotes a large image onto a bigger page rather than
    shrinking it, so a 1600px image ends up spanning most of an A3 plate —
    which at 300 DPI wants ~4600px. Resampling it there would destroy detail
    to save nothing.
    """
    doc, _image_path = image_doc
    code, out = run_export(doc, "--dpi", "300")
    assert code == 0

    report = json.loads((out / "_export-report.json").read_text(encoding="utf-8"))
    assert report["documents"][0]["image_bytes_saved"] == 0
    assert report["engine"], "the report must record which engine rendered it"


def test_a_low_dpi_export_downsamples_the_image(image_doc, run_export):
    """At 72 DPI the same image is comfortably over-resolution and shrinks."""
    doc, image_path = image_doc
    code, out = run_export(doc, "--dpi", "72")
    assert code == 0

    report = json.loads((out / "_export-report.json").read_text(encoding="utf-8"))
    assert report["documents"][0]["image_bytes_saved"] > 0

    pdf_size = (out / "ordering.pdf").stat().st_size
    source_size = image_path.stat().st_size
    assert pdf_size < source_size, (
        f"PDF ({pdf_size}) should be smaller than the raw source image ({source_size})"
    )


def test_lower_dpi_produces_a_smaller_pdf(image_doc, run_export):
    doc, _ = image_doc
    code, hi = run_export(doc, "--dpi", "300")
    assert code == 0
    hi_size = (hi / "ordering.pdf").stat().st_size

    code, lo = run_export(doc, "--dpi", "72")
    assert code == 0
    lo_size = (lo / "ordering.pdf").stat().st_size

    assert lo_size < hi_size


# ---------------------------------------------------------------------------
# Themes and settings files
# ---------------------------------------------------------------------------


def test_theme_choice_reaches_the_rendered_pdf(fixture_doc, run_export):
    """Two themes must not produce byte-identical output."""
    src = fixture_doc("wide_diagram.md")
    _, out_a = run_export(src, "--theme", "default")
    a = (out_a / "wide_diagram.pdf").read_bytes()
    _, out_b = run_export(src, "--theme", "mono")
    b = (out_b / "wide_diagram.pdf").read_bytes()
    assert a != b


@pytest.mark.parametrize("theme", ["carbon", "graphite", "midnight", "newsprint"])
def test_page_background_covers_margins_on_body_and_plate_pages(theme, fixture_doc, run_export):
    import pypdfium2 as pdfium

    from platefold.theme import get_theme

    src = fixture_doc("wide_diagram.md")
    code, out = run_export(src, "--theme", theme)
    assert code == 0
    pdf = out / "wide_diagram.pdf"
    assert len(set(page_sizes(pdf))) > 1
    assert outline_pages(pdf), "Background painting must preserve PDF navigation"
    colour = get_theme(theme).page
    expected = tuple(int(colour[i : i + 2], 16) for i in (1, 3, 5))
    with pdfium.PdfDocument(pdf) as document:
        for page in document:
            bitmap = page.render(scale=0.5)
            image = bitmap.to_pil()
            width, height = image.size
            for x, y in [(2, 2), (width - 3, 2), (2, height - 3), (width - 3, height - 3)]:
                actual = image.getpixel((x, y))[:3]
                assert all(abs(a - b) <= 1 for a, b in zip(actual, expected, strict=True))
            bitmap.close()
            page.close()


def test_settings_file_is_picked_up_from_beside_the_documents(fixture_doc, run_export):
    src = fixture_doc("wide_diagram.md")
    (src.parent / "platefold.yml").write_text(
        "page:\n  preset: a4\ntheme:\n  base: forest\n", encoding="utf-8"
    )
    code, out = run_export(src.parent)
    assert code == 0
    assert (210, 297) in page_sizes(out / "wide_diagram.pdf")


def test_a_broken_settings_file_fails_loudly(fixture_doc, run_export):
    src = fixture_doc("wide_diagram.md")
    (src.parent / "platefold.yml").write_text("layout:\n  inline_fitt: 0.9\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        run_export(src.parent)
    assert "inline_fitt" in str(exc.value)


# ---------------------------------------------------------------------------
# Diagram cache
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index", ["[]", "null", "42", '"invalid"'])
def test_non_object_cache_index_is_rebuilt(tmp_path, fixture_doc, run_export, index):
    source = fixture_doc("wide_diagram.md")
    cache = tmp_path / "diagram-cache"
    cache.mkdir()
    (cache / "index.json").write_text(index)
    code, output = run_export(source, "--clear-cache")
    assert code == 0
    assert page_sizes(output / source.with_suffix(".pdf").name)
    rebuilt = json.loads((cache / "index.json").read_text())
    assert isinstance(rebuilt, dict) and rebuilt


def test_changing_the_font_does_not_serve_a_stale_cached_diagram(fixture_doc, run_export, tmp_path):
    """Regression: the cache used to be keyed on diagram source alone."""
    src = fixture_doc("wide_diagram.md")
    cache = tmp_path / "diagram-cache"
    code, out_a = run_export(src, "--font", "calibri")
    assert code == 0
    first = json.loads((cache / "index.json").read_text(encoding="utf-8"))
    assert first

    code, out_b = run_export(src, "--font", "arial")
    assert code == 0
    second = json.loads((cache / "index.json").read_text(encoding="utf-8"))
    fresh = second.keys() - first.keys()
    assert len(fresh) == len(first), "Changing fonts must render a new SVG for each diagram"
    for key in fresh:
        svg = (cache / second[key]["svg_path"]).read_text(encoding="utf-8")
        assert "Arial" in svg
        assert "Calibri" not in svg

    # Linux may resolve both requested fonts to the same installed fallback,
    # producing identical PDF bytes even though cache invalidation is correct.
    assert page_sizes(out_a / "wide_diagram.pdf")
    assert page_sizes(out_b / "wide_diagram.pdf")


def test_a_second_export_reuses_the_cache_and_matches_the_first(fixture_doc, run_export):
    src = fixture_doc("wide_diagram.md")
    _, first = run_export(src)
    _, second = run_export(src)
    assert page_sizes(first / "wide_diagram.pdf") == page_sizes(second / "wide_diagram.pdf")


# ---------------------------------------------------------------------------
# Folder handling
# ---------------------------------------------------------------------------


def test_folder_export_mirrors_the_input_tree(tmp_path, run_export):
    root = tmp_path / "src"
    (root / "nested").mkdir(parents=True)
    (root / "top.md").write_text("# Top\n\nText.\n", encoding="utf-8")
    (root / "nested" / "inner.md").write_text("# Inner\n\nText.\n", encoding="utf-8")

    code, out = run_export(root)
    assert code == 0
    assert (out / "top.pdf").is_file()
    assert (out / "nested" / "inner.pdf").is_file()


def test_cancelling_stops_the_run_and_reports_partial_output(tmp_path):
    from platefold.cli import main

    root = tmp_path / "src"
    root.mkdir(parents=True)
    for i in range(4):
        (root / f"doc{i}.md").write_text(f"# Doc {i}\n\nText.\n", encoding="utf-8")
    out = tmp_path / "out"

    seen: list[str] = []

    def should_cancel() -> bool:
        return len(seen) >= 2

    def on_progress(done, total, rel, summary):
        if summary is not None:
            seen.append(rel)

    code = main([str(root), "-o", str(out), "--quiet"], on_progress, should_cancel)
    assert code == 2
    assert len(seen) == 2
    assert len(list(out.glob("*.pdf"))) == 2


@pytest.mark.parametrize("font", ["sora", "georgia"])
def test_metadata_labels_remain_words_and_long_values_stay_on_page(tmp_path, run_export, font):
    from contextlib import closing

    from pypdfium2 import PdfDocument

    source = tmp_path / "wrapping.md"
    labels = ["Document ID", "Provenance", "Verified by", "Last verified"]
    source.write_text(
        "# Document control\n\n| Field | Value |\n| --- | --- |\n"
        + "\n".join(f"| {label} | {'Detailed reference information ' * 10} |" for label in labels)
        + "\n| Identifier | "
        + "a" * 160
        + " |\n",
        encoding="utf-8",
    )
    code, out = run_export(source, "--no-toc", "--font", font)
    assert code == 0
    with (
        PdfDocument(out / "wrapping.pdf") as pdf,
        closing(pdf[0]) as page,
        closing(page.get_textpage()) as text,
    ):
        content = text.get_text_range()
        for word in ("Document", "Provenance", "Verified", "verified"):
            assert word in content, content
        for index in range(text.count_chars()):
            left, _bottom, right, _top = text.get_charbox(index)
            assert 0 <= left <= right <= page.get_width()


def test_readme_quickstart_sample_exports_without_warnings(tmp_path, capsys):
    """`platefold SAMPLE.md -o output` is the first command a new user runs.

    The project's own sample must model a clean export with the defaults: a
    declared language, an authored diagram description and the bundled fonts.
    """
    from platefold.cli import main

    sample = Path(__file__).resolve().parents[1] / "SAMPLE.md"
    assert main([str(sample), "-o", str(tmp_path / "output"), "--json", "--quiet"]) == 0
    result = json.loads(capsys.readouterr().out)
    documents = result["report"]["documents"]
    assert [document["warnings"] for document in documents] == [[]]
    assert result["status"] == "completed"
