"""Semantic DOCX ingestion, source-theme, and OOXML security boundaries."""

from __future__ import annotations

import json
import random
import re
import struct
import tracemalloc
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import mammoth
import pytest
from PIL import Image
from pypdf import PdfReader

from platefold import htmlplan, table_spans
from platefold.cli import build_settings, parse_args
from platefold.config import Settings
from platefold.document import Document, effective_settings
from platefold.htmlplan import cell_spans
from platefold.ingest import ingest
from platefold.ingest.docx import GRID_PAD, SHORT_ROW, _sanitize
from platefold.limits import ResourceLimitError, ResourceLimits
from platefold.ooxml import InvalidOOXMLError, OOXMLPackage
from platefold.service import ExportRequest, _save_requested_theme, export, prepare_export
from platefold.theme import get_theme


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (12, 8), "#214e75").save(output, "PNG")
    return output.getvalue()


THEME = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Proof">
 <a:themeElements>
  <a:clrScheme name="Proof">
   <a:dk1><a:srgbClr val="112233"/></a:dk1>
   <a:lt1><a:sysClr val="window" lastClr="FAF7F0"/></a:lt1>
   <a:dk2><a:srgbClr val="2D4059"/></a:dk2>
   <a:lt2><a:srgbClr val="E6DFD4"/></a:lt2>
   <a:accent1><a:srgbClr val="2E6F95"/></a:accent1>
   <a:accent2><a:srgbClr val="D97732"/></a:accent2>
   <a:accent3><a:srgbClr val="668844"/></a:accent3>
   <a:accent4><a:srgbClr val="775588"/></a:accent4>
   <a:accent5><a:srgbClr val="338888"/></a:accent5>
   <a:accent6><a:srgbClr val="AA8844"/></a:accent6>
   <a:hlink><a:srgbClr val="0055AA"/></a:hlink>
   <a:folHlink><a:srgbClr val="7755AA"/></a:folHlink>
  </a:clrScheme>
  <a:fontScheme name="Proof">
   <a:majorFont><a:latin typeface="Aptos Display"/></a:majorFont>
   <a:minorFont><a:latin typeface="Aptos"/></a:minorFont>
  </a:fontScheme>
  <a:fmtScheme name="Proof"/>
 </a:themeElements>
</a:theme>"""


STYLES = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>
 <w:style w:type="paragraph" w:styleId="CustomHeading"><w:name w:val="Field Section"/><w:basedOn w:val="Heading1"/></w:style>
 <w:style w:type="paragraph" w:styleId="CodeBlock"><w:name w:val="Code Block"/></w:style>
</w:styles>"""


DOCUMENT_BODY = """
 <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Repeated title</w:t></w:r></w:p>
 <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Repeated title</w:t></w:r></w:p>
 <w:p><w:pPr><w:pStyle w:val="CustomHeading"/></w:pPr><w:r><w:t>Custom section</w:t></w:r></w:p>
 <w:p><w:r><w:t>Semantic body text.</w:t></w:r></w:p>
 <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Column</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
 <w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Proof" descr="Embedded proof"/><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdImage1"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
 <w:p><w:pPr><w:pStyle w:val="CodeBlock"/></w:pPr><w:r><w:t xml:space="preserve">```mermaid
flowchart LR
 A --&gt; B
```</w:t></w:r></w:p>
"""


def make_docx(
    path: Path,
    *,
    body: str = DOCUMENT_BODY,
    theme: str | None = THEME,
    relationships: str | None = None,
    image_name: str = "proof.png",
    image_content_type: str = "image/png",
    image_bytes: bytes | None = None,
    extra: dict[str, bytes | str] | None = None,
) -> Path:
    rels = (
        relationships
        or """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rIdImage1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/%s"/>
 <Relationship Id="rIdTheme" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>
</Relationships>"""
        % image_name
    )
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body>{body}<w:sectPr/></w:body></w:document>"""
    parts: dict[str, bytes | str] = {
        "[Content_Types].xml": """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="%s" ContentType="%s"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>"""
        % (Path(image_name).suffix.lstrip("."), image_content_type),
        "_rels/.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>""",
        "word/document.xml": document,
        "word/_rels/document.xml.rels": rels,
        "word/styles.xml": STYLES,
        "docProps/core.xml": """<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>DOCX Proof</dc:title><dc:creator>PlateFold Test</dc:creator><cp:version>7.2</cp:version></cp:coreProperties>""",
        f"word/media/{image_name}": image_bytes if image_bytes is not None else _png(),
    }
    if theme is not None:
        parts["word/theme/theme1.xml"] = theme
    parts.update(extra or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return path


def test_semantic_ingestion_theme_metadata_assets_and_duplicate_slugs(tmp_path):
    result = ingest(str(make_docx(tmp_path / "proof.docx")), Settings())

    assert result.title == "DOCX Proof"
    assert ("Author", "PlateFold Test") in result.metadata
    assert ("Version", "7.2") in result.metadata
    assert [anchor for _, _, anchor in result.outline] == [
        "repeated-title",
        "repeated-title_1",
        "custom-section",
    ]
    assert 'class="pf-data-table"' in result.body_html
    assert "data-natural-width=" in result.body_html
    assert result.assets and next(iter(result.assets.values())).startswith(b"\x89PNG")
    assert next(iter(result.assets)) in result.body_html
    assert result.figures == [{"code": "flowchart LR\n A --> B\n", "head": "Custom section"}]
    assert result.source_theme is not None
    assert result.source_theme.page == "#faf7f0"
    assert result.source_theme.accent == "#2e6f95"
    assert result.source_theme.fonts is not None
    assert result.source_theme.fonts.body.startswith("'Aptos'")
    assert not any(code == "SOURCE_FONT_FALLBACK" for code, _ in result.warnings)
    assert result.source_theme.source != "builtin"


def test_embedded_asset_resolves_before_filesystem(tmp_path):
    path = make_docx(tmp_path / "proof.docx")
    result = ingest(str(path), Settings())
    key = next(iter(result.assets))
    (tmp_path / key).write_bytes(b"not an image")

    class Renderer:
        def render(self, _code):
            return {"error": "not exercised"}

    _, info = Document(str(path), str(tmp_path), Settings(), Renderer(), source=result).render()
    assert ("OK", key) in info["images"]


def test_embedded_svg_uses_svg_validation_and_refuses_entities(tmp_path):
    class Renderer:
        def render(self, _code):
            return {"error": "not exercised"}

    safe = b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10"><rect width="20" height="10"/></svg>'
    path = make_docx(
        tmp_path / "safe.docx",
        image_name="proof.svg",
        image_content_type="image/svg+xml",
        image_bytes=safe,
    )
    source = ingest(str(path), Settings())
    key = next(iter(source.assets))
    assert key.endswith(".svg")
    body, _ = Document(str(path), str(tmp_path), Settings(), Renderer(), source=source).render()
    assert "data:image/svg+xml;base64," in body

    hostile = b'<!DOCTYPE svg [<!ENTITY x "expanded">]><svg xmlns="http://www.w3.org/2000/svg" width="20" height="10"><text>&x;</text></svg>'
    hostile_path = make_docx(
        tmp_path / "hostile.docx",
        image_name="proof.svg",
        image_content_type="image/svg+xml",
        image_bytes=hostile,
    )
    hostile_source = ingest(str(hostile_path), Settings())
    with pytest.raises(ValueError, match="declarations, entities"):
        Document(
            str(hostile_path),
            str(tmp_path),
            Settings(),
            Renderer(),
            source=hostile_source,
        ).render()


def test_unsupported_embedded_image_type_is_refused(tmp_path):
    path = make_docx(
        tmp_path / "unsupported.docx",
        image_name="proof.wmf",
        image_content_type="image/x-wmf",
        image_bytes=b"not a supported image",
    )
    with pytest.raises(InvalidOOXMLError, match="Unsupported DOCX embedded image type"):
        ingest(str(path), Settings())


def test_source_theme_fallback_and_named_theme_precedence(tmp_path):
    path = make_docx(tmp_path / "plain.docx", theme=None)
    result = ingest(str(path), Settings())
    base = Settings(theme=get_theme("mono"), source_theme=True)
    resolved, warnings = effective_settings(base, result)
    assert resolved.theme.name == "mono"
    assert any(code == "SOURCE_THEME_FALLBACK" for code, _ in warnings)

    config = tmp_path / "platefold.yml"
    config.write_text("theme:\n  base: mono\n", encoding="utf-8")
    source_settings, _ = build_settings(
        parse_args([str(path), "--theme", "source", "--config", str(config)]), str(tmp_path)
    )
    assert source_settings.source_theme is True
    assert source_settings.theme.name == "mono"
    named_settings, _ = build_settings(
        parse_args([str(path), "--theme", "forest", "--config", str(config)]), str(tmp_path)
    )
    assert named_settings.source_theme is False
    assert named_settings.theme.name == "forest"


def test_incomplete_theme_falls_back_and_malformed_theme_is_refused(tmp_path):
    incomplete = """<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:themeElements><a:clrScheme name="short"><a:dk1><a:srgbClr val="000000"/></a:dk1></a:clrScheme></a:themeElements></a:theme>"""
    result = ingest(str(make_docx(tmp_path / "incomplete.docx", theme=incomplete)), Settings())
    assert result.source_theme is None
    assert any(code == "SOURCE_THEME_INCOMPLETE" for code, _ in result.warnings)
    with pytest.raises(InvalidOOXMLError, match="malformed OOXML part"):
        ingest(str(make_docx(tmp_path / "malformed.docx", theme="<a:theme>")), Settings())


def test_save_source_theme_is_atomic_reusable_and_collision_safe(tmp_path, monkeypatch):
    source = make_docx(tmp_path / "proof.docx")
    user = tmp_path / "themes"
    monkeypatch.setattr("platefold.theme.user_theme_dir", lambda: user)
    settings = Settings(source_theme=True)
    request = ExportRequest(str(source), str(tmp_path / "out"), settings, save_theme="office-proof")
    saved = _save_requested_theme(request, prepare_export(request, None))
    assert saved and Path(saved["path"]).is_file()
    payload = json.loads(Path(saved["path"]).read_text())
    assert payload["colors"]["accent"] == "#2e6f95"
    assert payload["fonts"]["body"].startswith("'Aptos'")
    reused = get_theme("office-proof")
    assert reused.accent == "#2e6f95" and reused.fonts is not None
    with pytest.raises(ValueError, match="already exists"):
        _save_requested_theme(request, prepare_export(request, None))
    reserved = ExportRequest(str(source), str(tmp_path / "out"), settings, save_theme="source")
    with pytest.raises(ValueError, match="source theme names are reserved"):
        _save_requested_theme(reserved, prepare_export(reserved, None))
    assert not list(user.glob("*.tmp"))
    (user / "linked.json").symlink_to(tmp_path / "outside.json")
    linked = ExportRequest(str(source), str(tmp_path / "out"), settings, save_theme="linked")
    with pytest.raises(ValueError, match="already exists"):
        _save_requested_theme(linked, prepare_export(linked, None))
    assert not (tmp_path / "outside.json").exists()


def test_save_source_theme_requires_single_docx(tmp_path):
    folder = tmp_path / "mixed"
    folder.mkdir()
    make_docx(folder / "proof.docx")
    (folder / "notes.md").write_text("# Notes")
    request = ExportRequest(
        str(folder), str(tmp_path / "out"), Settings(source_theme=True), save_theme="x"
    )
    with pytest.raises(ValueError, match="one DOCX or XLSX"):
        _save_requested_theme(request, prepare_export(request, None))


def _package(path: Path, parts: dict[str, bytes | str], limits: ResourceLimits | None = None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return OOXMLPackage(path, limits or ResourceLimits())


def test_ooxml_refuses_traversal_entities_external_relationships_and_duplicates(tmp_path):
    cases = [
        ({"../document.xml": "<root/>"}, "escapes"),
        ({"document.xml": '<!DOCTYPE x [<!ENTITY e "boom">]><x>&e;</x>'}, "Unsafe"),
        (
            {
                "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="x" Type="http://example/link" Target="https://example.invalid" TargetMode="External"/></Relationships>'
            },
            "external relationship",
        ),
    ]
    for index, (parts, message) in enumerate(cases):
        with (
            pytest.raises(InvalidOOXMLError, match=message),
            _package(tmp_path / f"bad-{index}.docx", parts),
        ):
            pass
    with zipfile.ZipFile(tmp_path / "duplicate.docx", "w") as archive:
        archive.writestr("word/document.xml", "<x/>")
        archive.writestr("WORD/DOCUMENT.XML", "<x/>")
    with (
        pytest.raises(InvalidOOXMLError, match="duplicate"),
        OOXMLPackage(tmp_path / "duplicate.docx", ResourceLimits()),
    ):
        pass


def test_ooxml_refuses_count_total_per_entry_ratio_and_depth(tmp_path, monkeypatch):
    with (
        pytest.raises(ResourceLimitError, match="entry limit"),
        _package(
            tmp_path / "count.docx",
            {"a.xml": "<x/>", "b.xml": "<x/>"},
            ResourceLimits(max_archive_entries=1),
        ),
    ):
        pass
    with (
        pytest.raises(ResourceLimitError, match="expanded limit"),
        _package(
            tmp_path / "total.docx",
            {"x.bin": b"a" * 300, "y.bin": b"b" * 300},
            ResourceLimits(max_archive_expanded_bytes=500),
        ),
    ):
        pass
    monkeypatch.setattr("platefold.ooxml.MAX_ARCHIVE_ENTRY_BYTES", 16)
    with (
        pytest.raises(ResourceLimitError, match="per-entry"),
        _package(tmp_path / "entry.docx", {"x.bin": b"not-compressible-17"}),
    ):
        pass
    monkeypatch.setattr("platefold.ooxml.MAX_ARCHIVE_ENTRY_BYTES", 64 * 1024 * 1024)
    with (
        pytest.raises(ResourceLimitError, match="compression-ratio"),
        _package(tmp_path / "ratio.docx", {"x.bin": b"0" * 100_000}),
    ):
        pass
    monkeypatch.setattr("platefold.ooxml.MAX_XML_DEPTH", 3)
    with (
        pytest.raises(ResourceLimitError, match="nesting"),
        _package(tmp_path / "deep.docx", {"x.xml": "<a><b><c><d/></c></b></a>"}),
    ):
        pass


def _understate_entry_count(path: Path, claimed: int) -> None:
    """Rewrite the ZIP footer so it claims ``claimed`` entries; the records stay."""
    data = bytearray(path.read_bytes())
    footer = data.rfind(b"PK\x05\x06")
    data[footer + 8 : footer + 12] = claimed.to_bytes(2, "little") * 2
    path.write_bytes(bytes(data))


def _forbid_zipfile(monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("zipfile.ZipFile must not read an unchecked directory")

    monkeypatch.setattr("platefold.ooxml.zipfile.ZipFile", refuse)


def test_ooxml_counts_directory_records_before_zipfile_allocates_them(tmp_path, monkeypatch):
    path = tmp_path / "understated.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(5):
            archive.writestr(f"part-{index}.xml", "<x/>")
    _understate_entry_count(path, 1)
    _forbid_zipfile(monkeypatch)

    with (
        pytest.raises(ResourceLimitError, match="2-entry limit"),
        OOXMLPackage(path, ResourceLimits(max_archive_entries=2)),
    ):
        pass


def test_ooxml_refuses_a_footer_that_understates_records_within_the_limit(tmp_path, monkeypatch):
    path = tmp_path / "understated.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(3):
            archive.writestr(f"part-{index}.xml", "<x/>")
    _understate_entry_count(path, 1)
    _forbid_zipfile(monkeypatch)

    with (
        pytest.raises(InvalidOOXMLError, match="entry count is inconsistent"),
        OOXMLPackage(path, ResourceLimits()),
    ):
        pass


def test_ooxml_refuses_a_zip64_locator_hidden_in_an_ordinary_directory(tmp_path, monkeypatch):
    # One entry whose 76-byte comment ends the directory with a ZIP64 end record
    # and locator. The ordinary footer and walk see one entry; zipfile would
    # follow the locator to a directory of 5000.
    zip64_record = struct.Struct("<4sQ2H2L4Q")
    locator = struct.Struct("<4sLQL")
    placeholder = zip64_record.pack(b"PK\x06\x06", 44, 45, 45, 0, 0, 5000, 5000, 0, 0)
    path = tmp_path / "zip64.docx"
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("word/document.xml")
        info.comment = placeholder + locator.pack(b"PK\x06\x07", 0, 0, 1)
        archive.writestr(info, "<x/>")
    data = bytearray(path.read_bytes())
    record_at = data.index(b"PK\x06\x06")
    locator_at = data.index(b"PK\x06\x07")
    data[locator_at : locator_at + locator.size] = locator.pack(b"PK\x06\x07", 0, record_at, 1)
    path.write_bytes(bytes(data))
    assert locator_at + locator.size == data.rindex(b"PK\x05\x06")
    _forbid_zipfile(monkeypatch)

    with (
        pytest.raises(InvalidOOXMLError, match="ZIP64"),
        OOXMLPackage(path, ResourceLimits(max_archive_entries=2)),
    ):
        pass


def test_malformed_mammoth_structure_is_normalized(tmp_path, monkeypatch):
    body = '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r></w:p>'
    monkeypatch.setattr(
        "platefold.ingest.docx.mammoth.convert_to_html",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(IndexError("unmatched field")),
    )
    with pytest.raises(InvalidOOXMLError, match="Invalid DOCX structure"):
        ingest(str(make_docx(tmp_path / "field.docx", body=body)), Settings())


def test_mammoth_output_is_sanitized(tmp_path, monkeypatch):
    path = make_docx(tmp_path / "proof.docx")
    monkeypatch.setattr(
        "platefold.ingest.docx.mammoth.convert_to_html",
        lambda *_args, **_kwargs: SimpleNamespace(
            value='<h1>Safe</h1><script>alert(1)</script><p><a href="javascript:alert(2)">link</a></p>',
            messages=[],
        ),
    )
    result = ingest(str(path), Settings())
    assert "Safe" in result.body_html and "link" in result.body_html
    assert "script" not in result.body_html and "javascript:" not in result.body_html
    assert {code for code, _ in result.warnings} >= {"DOCX_HTML_REMOVED", "DOCX_LINK_REMOVED"}


def _pdf_shape(path: Path) -> tuple[list[str], list[tuple[float, float]]]:
    reader = PdfReader(path)
    return (
        [(page.extract_text() or "").strip() for page in reader.pages],
        [(float(page.mediabox.width), float(page.mediabox.height)) for page in reader.pages],
    )


@pytest.mark.integration
def test_real_pdf_source_precedence_mixed_fallback_and_markdown_csv_parity(tmp_path):
    source = tmp_path / "mixed"
    source.mkdir()
    make_docx(source / "proof.docx")
    (source / "notes.md").write_text("# Markdown parity\n\nUnchanged prose.\n", encoding="utf-8")
    (source / "table.csv").write_text("Name,Value\nAlpha,1\nBeta,2\n", encoding="utf-8")

    fallback = get_theme("mono")
    source_result = export(
        ExportRequest(
            str(source),
            str(tmp_path / "source-out"),
            Settings(theme=fallback, source_theme=True, toc_min_headings=1),
            diagram_cache=str(tmp_path / "source-cache"),
        )
    )
    records = {item["file"]: item for item in source_result.report["documents"]}
    assert records["proof.docx"]["theme"] == "source"
    assert records["proof.docx"]["font_set"] == "source"
    for name in ("notes.md", "table.csv"):
        assert records[name]["theme"] == "mono"
        assert any("SOURCE_THEME_FALLBACK" in warning for warning in records[name]["warnings"])
    docx_reader = PdfReader(tmp_path / "source-out" / "proof.pdf")
    docx_text = "\n".join(page.extract_text() or "" for page in docx_reader.pages)
    assert "DOCX Proof" in docx_text and "Semantic body text" in docx_text
    assert len(docx_reader.outline) >= 3

    named_result = export(
        ExportRequest(
            str(source),
            str(tmp_path / "named-out"),
            Settings(theme=fallback, toc_min_headings=1),
            diagram_cache=str(tmp_path / "named-cache"),
        )
    )
    assert {item["theme"] for item in named_result.report["documents"]} == {"mono"}
    for stem in ("notes", "table"):
        assert _pdf_shape(tmp_path / "source-out" / f"{stem}.pdf") == _pdf_shape(
            tmp_path / "named-out" / f"{stem}.pdf"
        )


def _merge_cell(text: str, span: int = 1, merge: str = "") -> str:
    """A table cell spanning ``span`` grid columns: merge "r" starts a vertical merge, "c" continues one."""
    properties = f'<w:gridSpan w:val="{span}"/>' if span > 1 else ""
    properties += {"": "", "r": '<w:vMerge w:val="restart"/>', "c": "<w:vMerge/>"}[merge]
    return f"<w:tc><w:tcPr>{properties}</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


def merge_table(*rows: list, header: int = 0) -> str:
    """A table whose cells are each a text or (text, span, merge); the first ``header`` rows repeat."""
    marks = ["<w:trPr><w:tblHeader/></w:trPr>"] * header + [""] * (len(rows) - header)
    return "<w:tbl>%s</w:tbl>" % "".join(
        f"<w:tr>{mark}"
        + "".join(
            _merge_cell(cell) if isinstance(cell, str) else _merge_cell(*cell) for cell in row
        )
        + "</w:tr>"
        for mark, row in zip(marks, rows, strict=True)
    )


def _table_rows(body: str) -> list[list[list[tuple[int, int]]]]:
    """Each converted table's rows, and each row's cells as (rowspan, colspan)."""
    return [
        [
            [cell_spans(attributes)[::-1] for attributes in re.findall(r"<t[dh]\b([^>]*)>", row)]
            for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.S)
        ]
        for table in re.findall(r"<table\b.*?</table>", body, re.S)
    ]


def _tables(body: str) -> list[str]:
    """Each table's markup, a nested one first and replaced in its holder by "[table]"."""
    tables = []
    innermost = re.compile(r"<table\b(?:(?!<table\b).)*?</table>", re.S)
    while match := innermost.search(body):
        tables.append(match.group(0))
        body = body[: match.start()] + "[table]" + body[match.end() :]
    return tables


def _grid_problems(body: str) -> list[tuple[int, list[str]]]:
    """Each table's problems as a tagged grid, with its rows as the browser lays them out.

    Row spans are normalised as they are before printing; a row the
    normaliser hides, or one with no cells, is not tagged.
    """
    from test_pdf_ua_fixtures import grid_problems  # it imports this module

    problems = []
    for n, table in enumerate(_tables(table_spans.normalize(body))):
        rows = [
            [cell_spans(attributes)[::-1] for attributes in re.findall(r"<t[dh]\b([^>]*)>", row)]
            for opening, row in re.findall(r"<tr\b([^>]*)>(.*?)</tr>", table, re.S)
            if htmlplan.HIDDEN_ROW not in opening
        ]
        found = grid_problems([row for row in rows if row])
        if found:
            problems.append((n, found))
    return problems


def _overlaps(rows: list[list[tuple[int, int]]]) -> list[tuple[int, int]]:
    """The grid places two cells hold, with each cell at the next column no cell above holds."""
    held: set[tuple[int, int]] = set()
    found = []
    for r, cells in enumerate(rows):
        column = 0
        for down, across in cells:
            while (r, column) in held:
                column += 1
            places = {(r + i, column + j) for i in range(down) for j in range(across)}
            found += sorted(places & held)
            held |= places
            column += across
    return found


def _irregular_table(rng: random.Random) -> str:
    """A small table whose rows need not fill one grid, drawn as the issue's fuzz drew them."""
    rows = []
    for r in range(rng.randint(2, 5)):
        rows.append(
            [
                (f"r{r}", rng.choice([1, 1, 2, 3]), rng.choice(["", "r", "c", "c"]))
                for _ in range(rng.randint(1, 4))
            ]
        )
    return merge_table(*rows)


def _regular_table(rng: random.Random) -> str:
    """A table of rectangular merges on one grid, every continuation as wide as its origin."""
    height, width = rng.randint(2, 6), rng.randint(2, 5)
    owner: dict[tuple[int, int], tuple[int, int, int]] = {}
    for _ in range(rng.randint(1, 4)):
        r, c = rng.randrange(height), rng.randrange(width)
        down, across = rng.randint(1, height - r), rng.randint(1, width - c)
        places = [(r + i, c + j) for i in range(down) for j in range(across)]
        if not any(place in owner for place in places):
            owner.update(dict.fromkeys(places, (r, down, across)))
    rows = []
    for r in range(height):
        row, c = [], 0
        while c < width:
            top, down, across = owner.get((r, c), (r, 1, 1))
            row.append((f"r{r}", across, "" if down == 1 else "r" if r == top else "c"))
            c += across
        rows.append(row)
    return merge_table(*rows)


@pytest.mark.parametrize(("seed", "irregular"), [(1, 26), (2, 27), (3, 39)])
def test_irregular_word_tables_convert_without_overlapping_cells(tmp_path, seed, irregular):
    # Mammoth extends a vertical merge by grid index alone, so an irregular
    # Word grid used to convert to cells drawn over each other (seeds 1-3 of
    # the fuzz that found it: 26, 27 and 39 of 300 tables).
    rng = random.Random(seed)
    tables = [_irregular_table(rng) for _ in range(300)]
    path = make_docx(tmp_path / "irregular.docx", body="<w:p/>".join(tables))
    result = ingest(str(path), Settings())
    converted = _table_rows(result.body_html)
    assert len(converted) == 300
    assert [(n, _overlaps(rows)) for n, rows in enumerate(converted) if _overlaps(rows)] == []
    # Short rows are filled out too: every table is one valid tagged grid.
    assert _grid_problems(result.body_html) == []
    warnings = [detail for code, detail in result.warnings if code == "DOCX_TABLE_OVERLAP"]
    assert len(warnings) == irregular

    # Only colspans change, and empty cells are added: every cell and its
    # text stay, rowspans as Mammoth wrote them.
    with path.open("rb") as handle:
        written = mammoth.convert_to_html(handle).value
    cell = re.compile(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", re.S)
    before = cell.findall(written)
    after = [
        (attributes, text)
        for attributes, text in cell.findall(result.body_html)
        if GRID_PAD not in attributes
    ]
    assert [text for _, text in before] == [text for _, text in after]
    narrowed = 0
    for (was, _), (now, _) in zip(before, after, strict=True):
        (old_across, old_down), (across, down) = cell_spans(was), cell_spans(now)
        assert down == old_down and across <= old_across
        narrowed += across < old_across
    assert narrowed >= irregular


def test_regular_word_tables_are_left_as_converted(tmp_path):
    rng = random.Random(7)
    path = make_docx(
        tmp_path / "regular.docx",
        body="<w:p/>".join(_regular_table(rng) for _ in range(300)),
    )
    result = ingest(str(path), Settings())
    assert not any(code == "DOCX_TABLE_OVERLAP" for code, _ in result.warnings)
    with path.open("rb") as handle:
        written = mammoth.convert_to_html(handle).value
    spans = re.compile(r"<t[dh]\b[^>]*>")
    assert spans.findall(result.body_html) == spans.findall(written)


@pytest.mark.parametrize(
    ("rows", "converted", "texts"),
    [
        pytest.param(
            # The continuation matches the first row's cell by grid index, over
            # a second row that spans that column: the span runs into the merge.
            (["a", "b"], [("C", 2)], ["d", ("", 1, "c")]),
            [[(1, 1), (2, 1)], [(1, 1)], [(1, 1), (1, 1)]],
            ["a", "b", "C", "d"],
            id="colspan-into-rowspan",
        ),
        pytest.param(
            # The continuation is wider than its origin, so the next cell moves
            # left, under a merge that a later span runs into.
            (["a"], [("", 2, "c"), "c"], [("D", 2), ("", 1, "c")]),
            [[(2, 1), (1, 1)], [(2, 1)], [(1, 1)]],
            ["a", "c", "D"],
            id="continuation-wider",
        ),
        pytest.param(
            # The continuation is narrower than its origin, whose full width
            # then spans down into the merge beside it.
            (["a", "b"], [("C", 2)], [("", 1, "c"), ("", 1, "c")]),
            [[(1, 1), (2, 1)], [(2, 1)], []],
            ["a", "b", "C"],
            id="continuation-narrower",
        ),
    ],
)
def test_each_overlap_is_narrowed_to_the_free_columns(tmp_path, rows, converted, texts):
    regular = merge_table(["x", "y"], ["z", "w"])
    heading = '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Results</w:t></w:r></w:p>'
    path = make_docx(tmp_path / "case.docx", body=regular + heading + merge_table(*rows))
    result = ingest(str(path), Settings())
    tables = _table_rows(result.body_html)
    assert tables == [[[(1, 1), (1, 1)], [(1, 1), (1, 1)]], converted]
    assert _overlaps(converted) == []
    assert _grid_problems(result.body_html) == []
    assert re.findall(r"<td\b[^>]*><p>(\w+)</p>", result.body_html) == ["x", "y", "z", "w", *texts]
    assert result.warnings == [
        (
            "DOCX_TABLE_OVERLAP",
            'Table 2 (under "Results") has merged cells that overlap in the Word file\'s grid; '
            "1 cell(s) were narrowed to the columns left free, keeping their text. "
            "Check the table's merged cells in Word.",
        )
    ]


def test_a_span_ends_with_its_row_group(tmp_path):
    # The header cell's merge continues in the body by grid index, but the
    # browser ends it with the header, so the body's wide cell keeps its span.
    table = merge_table(["H1", "H2"], [("wide", 2)], ["x", ("", 1, "c")], header=1)
    result = ingest(str(make_docx(tmp_path / "head.docx", body=table)), Settings())
    assert "<thead>" in result.body_html
    assert _table_rows(result.body_html) == [[[(1, 1), (2, 1)], [(1, 2)], [(1, 1), (1, 1)]]]
    assert not any(code == "DOCX_TABLE_OVERLAP" for code, _ in result.warnings)


def test_short_rows_are_filled_out_to_the_grid_with_cells_that_draw_nothing(tmp_path):
    # Word draws a row with fewer cells short and leaves the rest blank. The
    # browser does too, but the tagged table then has rows of different widths
    # (PDF/UA-1 7.2-42), so each run of missing columns gets one empty cell.
    tables = [
        merge_table(["Name", "Value", "Note"], ["alpha", "1"], ["beta"], header=1),
        merge_table(["a", "b"], ["c", "d", "e", "f"]),
        # A merge beside the gap: the pad goes after it, where the row is free.
        merge_table(["a", "b", "c", "d"], [("e", 1, "r"), "f"], [("", 1, "c"), "g", "h", "i"]),
        # A row whose only cell spans down, and a row a merge covers entirely.
        merge_table([("a", 1, "r"), ("b", 1, "r")], [("", 1, "c"), ("", 1, "c")], ["c", "d", "e"]),
        # A short header row: its empty cell is a data cell, not a header.
        merge_table(["H1", "H2"], ["a", "b", "c"], header=1),
    ]
    result = ingest(str(make_docx(tmp_path / "short.docx", body="<w:p/>".join(tables))), Settings())
    # Each row's added cells, as their colspans, and where each row ends.
    pad = re.compile(rf'<td class="{GRID_PAD}"(?: colspan="(\d+)")?></td>')
    rows = [
        re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.S)
        for table in re.findall(r"<table\b.*?</table>", result.body_html, re.S)
    ]
    assert [[[int(span or 1) for span in pad.findall(row)] for row in table] for table in rows] == [
        [[], [1], [2]],
        [[2], []],
        [[], [2], []],
        [[1], [], []],
        [[1], []],
    ]
    assert (
        f'<thead><tr><th><p>H1</p></th><th><p>H2</p></th><td class="{GRID_PAD}"></td></tr></thead>'
        in (result.body_html.replace(f'<tr class="{SHORT_ROW}">', "<tr>"))
    )
    # Only the rows they fill are marked, so the stylesheet stripes their own cells.
    marked = re.findall(r"<tr( class=\"[^\"]*\")?>", result.body_html)
    assert [bool(mark) for mark in marked] == [
        False,
        True,
        True,
        True,
        False,
        False,
        True,
        False,
        True,
        False,
        False,
        True,
        False,
    ]
    assert set(marked) == {"", f' class="{SHORT_ROW}"'}
    # The added cells close their row, after every cell of its own.
    assert all(
        pad.sub("", row) == row[: pad.search(row).start()]
        for table in rows
        for row in table
        if pad.search(row)
    )
    assert _grid_problems(result.body_html) == []
    assert not result.warnings


def test_filling_short_rows_keeps_to_the_browser_span_and_the_cell_limit(tmp_path):
    # A run of free columns wider than a cell can span takes several cells.
    wide = merge_table([("a", 1000), ("b", 1000)], ["c"])
    result = ingest(str(make_docx(tmp_path / "wide.docx", body=wide)), Settings())
    assert f'<td><p>c</p></td><td class="{GRID_PAD}" colspan="1000"></td>' in result.body_html
    assert f'<td class="{GRID_PAD}" colspan="999"></td></tr>' in result.body_html
    assert _grid_problems(result.body_html) == []

    # The added cells count toward the document's cell limit, checked before they are written.
    short = merge_table(["a", "b", "c"], ["d"], ["e"])
    path = make_docx(tmp_path / "short.docx", body=short)
    settings = Settings(limits=ResourceLimits(max_document_cells=6))
    with pytest.raises(ResourceLimitError, match="more than 6 cells"):
        ingest(str(path), settings)
    assert ingest(str(path), Settings(limits=ResourceLimits(max_document_cells=7))).body_html


def test_filling_short_rows_is_counted_against_the_limit_before_any_cell_is_made():
    # One row of 5,000 cells each 1,000 columns wide over 5,000 one-cell rows
    # asks for 25 million empty cells. They are counted, not built, before the
    # limit refuses them: a few megabytes, where building them took gigabytes.
    fragment = (
        "<table><tr>"
        + '<td colspan="1000">w</td>' * 5000
        + "</tr>"
        + "<tr><td>s</td></tr>" * 5000
        + "</table>"
    )
    tracemalloc.start()
    try:
        with pytest.raises(ResourceLimitError, match="more than 1,000,000 cells"):
            _sanitize(fragment, max_cells=1_000_000)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 32 * 1024 * 1024, peak


def test_overlap_warnings_number_tables_as_a_reader_counts_them(tmp_path):
    # A nested table is named after the table it sits in, and does not move
    # the numbers of the tables after it.
    irregular = merge_table(["a", "b"], [("C", 2)], ["d", ("", 1, "c")])
    holder = (
        "<w:tbl><w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>outer</w:t></w:r></w:p></w:tc>"
        f"<w:tc><w:tcPr/>{irregular}<w:p/></w:tc></w:tr></w:tbl>"
    )
    body = "<w:p/>".join([merge_table(["x", "y"]), holder, irregular])
    result = ingest(str(make_docx(tmp_path / "nested.docx", body=body)), Settings())
    assert [detail.split(" has ")[0] for code, detail in result.warnings] == [
        "A table inside Table 2",
        "Table 3",
    ]


def indented_row(*cells, before=0, after=0, header=False, deleted=False, width_before=0):
    """A table row whose cells are texts or (text, span, merge), with Word's row indents."""
    properties = "<w:tblHeader/>" if header else ""
    properties += "<w:del/>" if deleted else ""
    properties += f'<w:gridBefore w:val="{before}"/>' if before else ""
    properties += f'<w:gridAfter w:val="{after}"/>' if after else ""
    properties += f'<w:wBefore w:w="{width_before}" w:type="dxa"/>' if width_before else ""
    return (
        f"<w:tr><w:trPr>{properties}</w:trPr>"
        + "".join(_merge_cell(c) if isinstance(c, str) else _merge_cell(*c) for c in cells)
        + "</w:tr>"
    )


def _placed(body: str) -> list[list[list[tuple[int, int, int, str]]]]:
    """Each table's rows as the browser places them: (column, colspan, rowspan, text) per cell.

    The text of an empty cell that fills a row is "-". A nested table is listed
    before the table holding it, where it reads as the text "[table]".
    """
    tables = []
    for table in _tables(body):
        held, rows = set(), []
        for r, row in enumerate(re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.S)):
            cells, column = [], 0
            for attributes, text in re.findall(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", row, re.S):
                while (r, column) in held:
                    column += 1
                across, down = cell_spans(attributes)
                held |= {(r + i, column + j) for i in range(down) for j in range(across)}
                label = "-" if GRID_PAD in attributes else re.sub(r"<[^>]+>", "", text)
                cells.append((column, across, down, label))
                column += across
            rows.append(cells)
        tables.append(rows)
    return tables


_ROWS = {
    # Indented rows: their cells start after the empty columns.
    "grid-before": (
        indented_row("a", "b", "c")
        + indented_row("d", "e", before=1)
        + indented_row("f", before=2),
        [
            [[(0, 1, 1, "a"), (1, 1, 1, "b"), (2, 1, 1, "c")]],
            [[(0, 1, 1, "-"), (1, 1, 1, "d"), (2, 1, 1, "e")]],
            [[(0, 2, 1, "-"), (2, 1, 1, "f")]],
        ],
    ),
    # Rows that stop early on purpose, before the table's last columns.
    "grid-after": (
        indented_row("a", "b", "c")
        + indented_row("d", after=2)
        + indented_row("e", before=1, after=1),
        [
            [[(0, 1, 1, "a"), (1, 1, 1, "b"), (2, 1, 1, "c")]],
            [[(0, 1, 1, "d"), (1, 2, 1, "-")]],
            [[(0, 1, 1, "-"), (1, 1, 1, "e"), (2, 1, 1, "-")]],
        ],
    ),
    # A merge continued in an indented row joins the cell above it, not the
    # first column's: Mammoth counts the stand-in's columns too.
    "grid-before-with-merge": (
        indented_row("a", ("B", 1, "r"), "c") + indented_row(("", 1, "c"), "y", before=1),
        [
            [[(0, 1, 1, "a"), (1, 1, 2, "B"), (2, 1, 1, "c")]],
            [[(0, 1, 1, "-"), (2, 1, 1, "y")]],
        ],
    ),
    "grid-before-with-span": (
        indented_row("a", "b", "c", "d") + indented_row(("E", 2), "f", before=1),
        [
            [[(0, 1, 1, "a"), (1, 1, 1, "b"), (2, 1, 1, "c"), (3, 1, 1, "d")]],
            [[(0, 1, 1, "-"), (1, 2, 1, "E"), (3, 1, 1, "f")]],
        ],
    ),
    # A deleted row is left out with its indent, a width alone is no indent,
    # and the indented row after them is still placed by its own.
    "deleted-and-width-only": (
        indented_row("a", "b")
        + indented_row("gone", before=1, deleted=True)
        + indented_row("c", "d", width_before=1440)
        + indented_row("e", before=1),
        [
            [[(0, 1, 1, "a"), (1, 1, 1, "b")]],
            [[(0, 1, 1, "c"), (1, 1, 1, "d")]],
            [[(0, 1, 1, "-"), (1, 1, 1, "e")]],
        ],
    ),
}


@pytest.mark.parametrize("case", list(_ROWS))
def test_indented_word_rows_keep_their_grid_columns(tmp_path, case):
    rows, placed = _ROWS[case]
    result = ingest(
        str(make_docx(tmp_path / "rows.docx", body=f"<w:tbl>{rows}</w:tbl>")), Settings()
    )
    assert [row for table in _placed(result.body_html) for row in table] == [
        row for rows in placed for row in rows
    ]
    assert _grid_problems(result.body_html) == []
    assert not result.warnings


def test_indented_rows_in_a_header_and_a_nested_table(tmp_path):
    inner = "<w:tbl>" + indented_row("n1", "n2") + indented_row("n3", before=1) + "</w:tbl>"
    outer = (
        "<w:tbl>"
        + indented_row("H2", "H3", before=1, header=True)
        + indented_row("a", "b", "c")
        + f"<w:tr><w:tc><w:tcPr/><w:p/></w:tc><w:tc><w:tcPr/>{inner}<w:p/></w:tc>"
        + "<w:tc><w:tcPr/><w:p><w:r><w:t>z</w:t></w:r></w:p></w:tc></w:tr>"
        + "</w:tbl>"
    )
    result = ingest(str(make_docx(tmp_path / "nested.docx", body=outer)), Settings())
    # The header row's stand-in is a data cell before its two header cells.
    assert f'<thead><tr class="{SHORT_ROW}"><td class="{GRID_PAD}"></td><th><p>H2</p></th>' in (
        result.body_html
    )
    inner, outer = _placed(result.body_html)
    assert inner == [[(0, 1, 1, "n1"), (1, 1, 1, "n2")], [(0, 1, 1, "-"), (1, 1, 1, "n3")]]
    assert outer == [
        [(0, 1, 1, "-"), (1, 1, 1, "H2"), (2, 1, 1, "H3")],
        [(0, 1, 1, "a"), (1, 1, 1, "b"), (2, 1, 1, "c")],
        [(0, 1, 1, ""), (1, 1, 1, "[table]"), (2, 1, 1, "z")],
    ]
    assert _grid_problems(result.body_html) == []
    assert "pf" not in re.sub(r'class="pf-[a-z-]+"', "", result.body_html)  # no stand-in text left


def _stray_key(body: str) -> list[str]:
    """Any stand-in key left in the output: "pf" and 16 hex digits."""
    return re.findall(r"pf[0-9a-f]{16}", body)


def test_a_row_outside_any_table_never_shows_the_stand_in_key(tmp_path):
    # Mammoth writes a w:tr straight in the body as a row of its own. Only
    # rows of a table get stand-ins, and no key text survives in any case.
    stray = "<w:p><w:r><w:t>before</w:t></w:r></w:p>" + indented_row("x", before=1)
    result = ingest(str(make_docx(tmp_path / "stray.docx", body=stray)), Settings())
    assert _stray_key(result.body_html) == []
    assert "<td><p>x</p></td>" in result.body_html
    # Whatever Mammoth does with a stand-in, its key is taken out of the output.
    body, _, _ = _sanitize("<p>pf0123456789abcdef</p>", stand_in="pf0123456789abcdef")
    assert body == "<p></p>"


@pytest.mark.parametrize("value", ["²", "\u0661", "-1", "1e3", "", "99999"])
def test_a_row_indent_that_is_not_a_count_is_ignored(tmp_path, value):
    # Word writes a plain count. Anything else is ignored, as it was before
    # indents were read, instead of refusing the document.
    row = f'<w:tr><w:trPr><w:gridBefore w:val="{value}"/></w:trPr>{_merge_cell("c")}</w:tr>'
    body = "<w:tbl>" + indented_row("a", "b") + row + "</w:tbl>"
    result = ingest(str(make_docx(tmp_path / "value.docx", body=body)), Settings())
    assert _placed(result.body_html) == [
        [[(0, 1, 1, "a"), (1, 1, 1, "b")], [(0, 1, 1, "c"), (1, 1, 1, "-")]]
    ]


def test_a_merged_cell_holding_only_an_indented_table_keeps_its_span(tmp_path):
    # The nested table's one row is all indent. Its stand-in is the nested
    # table's cell; the cell holding that table is not a stand-in, and keeps
    # the rows its merge spans.
    nested = '<w:tbl><w:tr><w:trPr><w:gridBefore w:val="2"/></w:trPr></w:tr></w:tbl>'
    holder = f'<w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>{nested}<w:p/></w:tc>'
    body = (
        "<w:tbl>"
        + f"<w:tr>{_merge_cell('a')}{holder}{_merge_cell('c')}</w:tr>"
        + indented_row("d", ("", 1, "c"), "f")
        + indented_row("g", "h", "i")
        + "</w:tbl>"
    )
    result = ingest(str(make_docx(tmp_path / "holder.docx", body=body)), Settings())
    nested_rows, outer = _placed(result.body_html)
    assert nested_rows == [[(0, 2, 1, "-")]]
    assert outer[0][1][:3] == (1, 1, 2)
    assert _grid_problems(result.body_html) == []
    assert _stray_key(result.body_html) == []


def _rename_part(path: Path, old: str, new: str) -> Path:
    """Move one part of a package and every reference to it, as other producers name parts."""
    with zipfile.ZipFile(path) as source:
        members = {info.filename: source.read(info) for info in source.infolist()}
    old_folder, _, old_name = old.rpartition("/")
    new_folder, _, new_name = new.rpartition("/")
    renamed = {}
    for name, data in members.items():
        if name == old:
            name = new
        elif name == f"{old_folder}/_rels/{old_name}.rels":
            name = f"{new_folder}/_rels/{new_name}.rels"
        if name.endswith(".rels") or name == "[Content_Types].xml":
            data = data.replace(old.encode(), new.encode())
            data = data.replace(f'Target="{old_name}"'.encode(), f'Target="{new_name}"'.encode())
        renamed[name] = data
    with zipfile.ZipFile(path, "w") as target:
        for name, data in renamed.items():
            target.writestr(name, data)
    return path


def _with_stray_main_document(path: Path) -> Path:
    """Add an unreferenced word/document.xml beside a renamed main document."""
    stray = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Stray part</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("word/document.xml", stray)
    return path


INDENTED_TABLE = (
    "<w:tbl>" + indented_row("a", "b", "c") + indented_row("indented", before=2) + "</w:tbl>"
)


@pytest.mark.parametrize("stray", [False, True], ids=["renamed", "renamed-beside-a-stray-part"])
def test_a_main_document_named_by_the_package_relationship_converts_fully(tmp_path, stray):
    # The package's relationships name the main document; word/document.xml is
    # only the usual name. An unreferenced part with that name is not the document.
    path = _rename_part(
        make_docx(tmp_path / "renamed.docx", body=INDENTED_TABLE),
        "word/document.xml",
        "word/document2.xml",
    )
    if stray:
        _with_stray_main_document(path)
    result = ingest(str(path), Settings())
    assert [row for table in _placed(result.body_html) for row in table] == [
        [(0, 1, 1, "a"), (1, 1, 1, "b"), (2, 1, 1, "c")],
        [(0, 2, 1, "-"), (2, 1, 1, "indented")],
    ]
    assert "Stray part" not in result.body_html
    assert _stray_key(result.body_html) == []


def test_a_package_without_its_main_document_is_refused(tmp_path):
    path = make_docx(tmp_path / "missing.docx", body=INDENTED_TABLE)
    with zipfile.ZipFile(path) as source:
        members = {i.filename: source.read(i) for i in source.infolist()}
    del members["word/document.xml"]
    with zipfile.ZipFile(path, "w") as target:
        for name, data in members.items():
            target.writestr(name, data)
    with pytest.raises(InvalidOOXMLError, match="no main document part"):
        ingest(str(path), Settings())


@pytest.mark.parametrize(
    "renames",
    [
        (),
        (("word/document.xml", "word/document2.xml"),),
        (("word/styles.xml", "word/styles2.xml"),),
        (("word/document.xml", "content/main.xml"),),
    ],
    ids=["usual-names", "renamed-main", "renamed-styles", "main-in-another-folder"],
)
def test_docx_parts_agree_with_mammoths_own_resolution(tmp_path, renames):
    from mammoth import zips
    from mammoth.docx import _find_part_paths  # what the conversion itself reads

    from platefold.ingest.docx_semantics import docx_parts

    path = make_docx(tmp_path / "parts.docx")
    for old, new in renames:
        _rename_part(path, old, new)
    with open(path, "rb") as handle, zips.open_zip(handle, "r") as archive:
        expected = _find_part_paths(archive)
    with OOXMLPackage(path, ResourceLimits()) as package:
        found = docx_parts(package)
    assert (found.main, found.styles, found.footnotes, found.endnotes, found.comments) == (
        expected.main_document,
        expected.styles,
        expected.footnotes,
        expected.endnotes,
        expected.comments,
    )
