"""Preview the delivered PDF, and keep rapid edits from building a render backlog."""

import base64
import io
import threading
from dataclasses import replace

import pytest
from PIL import Image

from platefold.config import make_settings
from platefold.preview import PreviewQueue, first_page
from platefold.service import export
from platefold.style import footer_template
from platefold.worker import request_from


def test_preview_queue_discards_superseded_work_and_errors():
    started, release, delivered = threading.Event(), threading.Event(), threading.Event()
    rendered, events = [], []

    def render(message):
        rendered.append(message["id"])
        if message["id"] == "old":
            started.set()
            assert release.wait(5)
            raise ValueError("Stale error")
        return message["id"]

    def emit(*args, **kwargs):
        events.append((args, kwargs))
        delivered.set()

    queue = PreviewQueue(render, emit)
    try:
        queue.submit({"id": "old"})
        assert started.wait(5)
        queue.submit({"id": "intermediate"})
        queue.submit({"id": "latest"})
        release.set()
        assert delivered.wait(5)
    finally:
        release.set()
        queue.close()
    assert rendered == ["old", "latest"]
    assert events == [(("preview", "latest"), {"preview": "latest"})]


def test_clearing_preview_queue_discards_active_and_pending_results():
    started, release = threading.Event(), threading.Event()
    rendered, events = [], []

    def render(message):
        rendered.append(message["id"])
        started.set()
        assert release.wait(5)

    queue = PreviewQueue(render, lambda *args, **kwargs: events.append(args))
    try:
        queue.submit({"id": "active"})
        assert started.wait(5)
        queue.submit({"id": "pending"})
        queue.clear()
    finally:
        release.set()
        queue.close()
    assert rendered == ["active"]
    assert events == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "theme,font,preset,toc",
    [("bristol", "sora", "letter", False), ("carbon", "georgia", "a4", True)],
)
def test_first_page_matches_export_fonts_palette_paper_and_contents(
    tmp_path, theme, font, preset, toc
):
    import pypdfium2 as pdfium

    source = tmp_path / "source.md"
    source.write_text(
        "# A real source document\n\nSource-specific body text.\n\n"
        + "\n\n".join(f"## Section {i}\n\n" + "Body text. " * 60 for i in range(6)),
        encoding="utf-8",
    )
    request = request_from(
        {
            "input_path": str(source),
            "output_dir": str(tmp_path / "preview"),
            "settings": {
                "theme": theme,
                "font_set": font,
                "preset": preset,
                "toc": toc,
                "bookmarks": toc,
                "colors": {"accent": "#123456"},
            },
        }
    )
    preview = first_page(request)
    assert "A real source document" in preview["text"]
    assert preview["file"] == "source.md" and preview["pages"] is None
    assert preview["settings"]["font_set"] == font
    image = Image.open(io.BytesIO(base64.b64decode(preview["image"])))
    assert max(image.size) <= 1200
    delivered = export(replace(request, output_dir=str(tmp_path / "export")))
    assert preview["settings"] == delivered.report["settings"]
    with pdfium.PdfDocument(tmp_path / "export/source.pdf") as pdf:
        page = pdf[0]
        bitmap = page.render(scale=min(1.5, 1200 / max(page.get_size())))
        try:
            delivered_page = bitmap.to_pil()
            # Preview deliberately omits the unknown full-document page total.
            content = (0, 0, image.width, image.height - 40)
            assert image.crop(content).tobytes() == delivered_page.crop(content).tobytes()
        finally:
            bitmap.close()
        assert len(pdf) > 1


def test_preview_footer_does_not_claim_a_full_document_page_total():
    footer = footer_template(make_settings(), include_total=False)

    assert 'class="pageNumber"' in footer
    assert 'class="totalPages"' not in footer
