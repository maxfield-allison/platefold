"""Exercise filesystem boundaries and the browser's content policy."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import quote

import pytest
from PIL import Image

from platefold.browser import launch
from platefold.config import Settings, make_settings
from platefold.document import Document
from platefold.images import ImageLoader


@pytest.mark.parametrize(
    "reference",
    [
        "../outside.png",
        "%2e%2e/outside.png",
        "file:///outside.png",
        "//example.invalid/x.png",
        "https://example.invalid/x.png",
        "%5c%5cexample.invalid/x.png",
    ],
)
def test_images_cannot_escape_source_folder(tmp_path, reference):
    Image.new("RGB", (20, 20)).save(tmp_path / "outside.png")
    root = tmp_path / "source"
    root.mkdir()
    source = root / "doc.md"
    source.write_text(f"![outside]({reference})", encoding="utf-8")
    document = Document(str(source), str(root), Settings(), None, ImageLoader())
    rendered, info = document.render()
    assert [(code, detail) for code, detail in info["images"] if code == "MISSING"] == [
        ("MISSING", reference)
    ]
    assert "data:image" not in rendered


@pytest.mark.integration
def test_raw_html_cannot_execute_or_fetch_remote_resources(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    browser = None
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        (tmp_path / "nested.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
            f'<image href="{url}/svg-image"/><script>window.hostileRan=true</script>'
            f'<foreignObject><iframe xmlns="http://www.w3.org/1999/xhtml" src="{url}/svg-frame"/></foreignObject></svg>',
            encoding="utf-8",
        )
        source = tmp_path / "hostile.md"
        navigation = quote(
            '<script>window.hostileRan=true; fetch("' + url + '/navigation")</script>'
        )
        source.write_text(
            '<meta http-equiv="refresh" content="0;url=data:text/html,' + navigation + '">\n'
            '<script>window.hostileRan=true; fetch("' + url + '/fetch")</script>\n'
            '<iframe src="' + url + '/frame"></iframe>\n'
            '<style>body { background-image: url("' + url + '/css"); }</style>\n'
            '<img src="' + url + '/image" onerror="window.hostileRan=true">\n'
            '<object data="' + url + '/object"></object>\n'
            '<svg onload="window.hostileRan=true"><image href="' + url + '/inline-svg"/></svg>\n'
            '<style>@import url("' + url + '/import");</style>\n'
            "![Nested SVG](nested.svg)\n\n"
            "# Safe text\n",
            encoding="utf-8",
        )
        doc = Document(str(source), str(tmp_path), Settings(), None, ImageLoader())
        rendered, _ = doc.render()
        browser, _ = launch()
        page = browser.new_page()
        page.set_content(rendered)
        assert page.evaluate("location.href") == "about:blank"
        assert page.evaluate("typeof window.hostileRan") == "undefined"
        assert "Safe text" in page.evaluate("document.body.innerText")
        assert requests == []
    finally:
        if browser:
            browser.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.integration
def test_pdf_neutralizes_unsafe_link_schemes(tmp_path):
    """Delivered PDFs keep ordinary links without handing off unsafe protocols."""
    from pypdf import PdfReader

    from platefold.service import ExportRequest, export

    source = tmp_path / "links.md"
    source.write_text(
        """# Link policy

[Web](https://example.invalid/path)
[Email](mailto:security@example.invalid)
[Contents](#safe-heading)
[Local file](file:///etc/passwd)
[Script](javascript:alert(1))
[Embedded](data:text/html,unsafe)
<a href="custom-danger:payload">Custom protocol</a>

## Safe heading
""",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    result = export(
        ExportRequest(
            str(source),
            str(output),
            # Keep link-policy assertions independent of host-installed fonts.
            make_settings(font_set="sora", toc=False, bookmarks=False, document_language="en"),
        )
    )

    reader = PdfReader(output / "links.pdf")
    uris = []
    for page in reader.pages:
        for reference in page.get("/Annots", []):
            action = reference.get_object().get("/A")
            if action and action.get("/URI") is not None:
                uris.append(str(action["/URI"]))
    assert sorted(uris) == ["https://example.invalid/path", "mailto:security@example.invalid"]
    assert result.report["documents"][0]["warnings"] == [
        "Neutralized 4 link(s) with unsupported URI schemes"
    ]
    assert "file:///etc/passwd" not in json.dumps(result.report)


@pytest.mark.parametrize("name", ["notes.pdf", "_export-report.json"])
def test_export_rejects_hardlinked_outputs_before_any_write(tmp_path, name):
    import os

    from platefold.service import ExportRequest, export

    source = tmp_path / "notes.md"
    source.write_text("# Synthetic document", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside sentinel")
    os.link(outside, output / name)
    with pytest.raises(ValueError, match="linked file"):
        export(ExportRequest(str(source), str(output), Settings()))
    assert outside.read_bytes() == b"outside sentinel"
    assert sorted(p.name for p in output.iterdir()) == [name]


def test_export_rejects_redirected_output_directory(tmp_path):
    import os
    import subprocess

    from platefold.service import ExportRequest, export

    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "notes.md").write_text("# Synthetic document", encoding="utf-8")
    output, outside = tmp_path / "output", tmp_path / "outside"
    output.mkdir()
    outside.mkdir()
    link = output / "nested"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the destination"):
        export(ExportRequest(str(source), str(output), Settings()))
    assert list(outside.iterdir()) == []
