"""Resource bounds for hostile DOCX and XLSX packages.

Every hostile package here is generated at test time, so no large binary is
committed. The generators build packages that stay under the byte, entry and
compression-ratio checks, which is what makes them dangerous: the cost comes
from what the bytes turn into after parsing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import zipfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from platefold.config import Settings
from platefold.ingest import ingest
from platefold.limits import ResourceLimitError, ResourceLimits
from platefold.ooxml import InvalidOOXMLError, OOXMLPackage

_SHEET = (
    'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _relationships(*items: tuple[str, str, str]) -> str:
    body = "".join(
        f'<Relationship Id="{rid}" Type="{_OFFICE_RELS}/{kind}" Target="{target}"/>'
        for rid, kind, target in items
    )
    return f'<Relationships xmlns="{_RELS}">{body}</Relationships>'


def _png(size: tuple[int, int] = (12, 8)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "#214e75").save(output, "PNG")
    return output.getvalue()


def element_flood_docx(path: Path, body_bytes: int, element: bytes = b"<a/>") -> Path:
    """A DOCX whose body is one tiny element repeated, compressing about 70:1."""
    head = (
        b'<?xml version="1.0"?><w:document '
        b'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    )
    body = element * (body_bytes // len(element))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels", _relationships(("rId1", "officeDocument", "word/document.xml"))
        )
        # Vary a byte every few hundred elements so the part stays under the
        # compression-ratio check, as a crafted file would.
        stride = 240 * len(element)
        with archive.open("word/document.xml", "w") as member:
            member.write(head)
            for offset in range(0, len(body), stride):
                member.write(body[offset : offset + stride])
                member.write(b'<b c="%x"/>' % offset)
            member.write(b"</w:body></w:document>")
    return path


def image_docx(path: Path, references: int, image: bytes) -> Path:
    """A DOCX that places one embedded image part many times."""
    drawing = (
        '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="p" descr="Photo"/>'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdImage"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>"
        "</w:r></w:p>"
    )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        f'xmlns:r="{_OFFICE_RELS}" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f"<w:body>{drawing * references}<w:sectPr/></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels", _relationships(("rId1", "officeDocument", "word/document.xml"))
        )
        archive.writestr("word/document.xml", document)
        archive.writestr(
            "word/_rels/document.xml.rels",
            _relationships(("rIdImage", "image", "media/photo.png")),
        )
        archive.writestr("word/media/photo.png", image)
    return path


def workbook(
    path: Path,
    sheets: list[str],
    *,
    shared_strings: str | None = None,
    extra: dict[str, str | bytes] | None = None,
) -> Path:
    """A minimal XLSX with exactly the worksheet XML given."""
    workbook_rels = [
        (f"rId{index}", "worksheet", f"worksheets/sheet{index}.xml")
        for index in range(1, len(sheets) + 1)
    ]
    if shared_strings is not None:
        workbook_rels.append(("rIdStrings", "sharedStrings", "sharedStrings.xml"))
    declarations = "".join(
        f'<sheet name="Sheet {index}" sheetId="{index}" r:id="rId{index}"/>'
        for index in range(1, len(sheets) + 1)
    )
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>'
        ),
        "_rels/.rels": _relationships(("rId1", "officeDocument", "xl/workbook.xml")),
        "xl/workbook.xml": f"<workbook {_SHEET}><sheets>{declarations}</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": _relationships(*workbook_rels),
    }
    for index, sheet in enumerate(sheets, start=1):
        parts[f"xl/worksheets/sheet{index}.xml"] = sheet
    if shared_strings is not None:
        parts["xl/sharedStrings.xml"] = shared_strings
    parts.update(extra or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return path


def sheet(cells: str, tail: str = "") -> str:
    return f"<worksheet {_SHEET}><sheetData>{cells}</sheetData>{tail}</worksheet>"


def corner_sheet(rows: int, column: str) -> str:
    """Two cells whose used range spans rows x columns of empty table cells."""
    return sheet(
        f'<row r="1"><c r="A1"><v>1</v></c></row>'
        f'<row r="{rows}"><c r="{column}{rows}"><v>2</v></c></row>'
    )


def image_workbook(path: Path, anchors: int, images: dict[str, bytes]) -> Path:
    """One sheet whose drawing places each image part `anchors` times."""
    anchor = (
        "<xdr:oneCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>"
        "<xdr:row>0</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        '<xdr:ext cx="1" cy="1"/><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="p" '
        'descr="Photo"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill>'
        '<a:blip r:embed="{rid}"/></xdr:blipFill><xdr:spPr/></xdr:pic><xdr:clientData/>'
        "</xdr:oneCellAnchor>"
    )
    rids = [f"rIdImage{index}" for index in range(len(images))]
    drawing = (
        '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/'
        'spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'xmlns:r="{_OFFICE_RELS}">'
        + "".join(anchor.format(rid=rid) for rid in rids) * anchors
        + "</xdr:wsDr>"
    )
    extra: dict[str, str | bytes] = {
        "xl/worksheets/_rels/sheet1.xml.rels": _relationships(
            ("rIdDrawing", "drawing", "../drawings/drawing1.xml")
        ),
        "xl/drawings/drawing1.xml": drawing,
        "xl/drawings/_rels/drawing1.xml.rels": _relationships(
            *[(rid, "image", f"../media/{name}") for rid, name in zip(rids, images, strict=True)]
        ),
    }
    extra.update({f"xl/media/{name}": raw for name, raw in images.items()})
    return workbook(
        path,
        [sheet('<row r="1"><c r="A1"><v>1</v></c></row>', '<drawing r:id="rIdDrawing"/>')],
        extra=extra,
    )


def _settings(**limits) -> Settings:
    return Settings(limits=replace(ResourceLimits(), **limits))


# ------------------------------------------------------------ XML node budget


def test_element_flood_stops_at_the_node_budget(tmp_path):
    path = element_flood_docx(tmp_path / "flood.docx", 80_000)
    with pytest.raises(ResourceLimitError, match=r"5000-node XML limit.*word/document\.xml"):
        ingest(str(path), _settings(max_docx_xml_nodes=5_000))
    # The same package is an ordinary document under a budget that covers it.
    assert ingest(str(path), _settings(max_docx_xml_nodes=50_000)).body_html == ""


def test_attributes_count_toward_the_node_budget(tmp_path):
    parts = {"x.xml": "<root>" + '<a b="" c="" d=""/>' * 100 + "</root>"}
    path = tmp_path / "attributes.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    # 101 elements alone fit; their 300 attributes do not.
    with (
        pytest.raises(ResourceLimitError, match="node XML limit"),
        OOXMLPackage(path, ResourceLimits(), max_xml_nodes=300),
    ):
        pass
    with OOXMLPackage(path, ResourceLimits(), max_xml_nodes=401) as package:
        assert package.xml("x.xml") is not None


def test_the_node_budget_spans_every_part_of_a_package(tmp_path):
    path = tmp_path / "parts.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(4):
            archive.writestr(f"part{index}.xml", "<r>" + "<a/>" * 99 + "</r>")
    with (
        pytest.raises(ResourceLimitError, match=r"part3\.xml"),
        OOXMLPackage(path, ResourceLimits(), max_xml_nodes=350),
    ):
        pass


def test_each_format_uses_its_own_node_budget(tmp_path):
    rows = "".join(f'<row r="{row}"><c r="A{row}"><v>{row}</v></c></row>' for row in range(1, 200))
    path = workbook(tmp_path / "budget.xlsx", [sheet(rows)])
    # 199 rows of row, cell, value and two reference attributes.
    with pytest.raises(ResourceLimitError, match="node XML limit"):
        ingest(str(path), _settings(max_xlsx_xml_nodes=500))
    assert "<table" in ingest(str(path), _settings(max_docx_xml_nodes=1)).body_html


def test_shared_string_flood_stops_at_the_xlsx_budget(tmp_path):
    count = 50_000
    strings = (
        f'<sst {_SHEET} count="{count}" uniqueCount="{count}">' + "<si><t/></si>" * count + "</sst>"
    )
    path = workbook(
        tmp_path / "strings.xlsx",
        [sheet('<row r="1"><c r="A1" t="s"><v>0</v></c></row>')],
        shared_strings=strings,
    )
    with pytest.raises(ResourceLimitError, match=r"xl/sharedStrings\.xml"):
        ingest(str(path), _settings(max_xlsx_xml_nodes=10_000))


def test_entities_and_nesting_are_still_refused_while_streaming(tmp_path, monkeypatch):
    path = tmp_path / "entity.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "x.xml",
            '<!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]><x>&b;</x>',
        )
    with pytest.raises(InvalidOOXMLError, match="Unsafe"), OOXMLPackage(path, ResourceLimits()):
        pass
    # Depth is checked at each start tag, before the rest of the part is built.
    monkeypatch.setattr("platefold.ooxml.MAX_XML_DEPTH", 4)
    deep = tmp_path / "deep.docx"
    with zipfile.ZipFile(deep, "w") as archive:
        archive.writestr("x.xml", "<a>" * 5 + "</a>" * 5 + "<unclosed>")
    with pytest.raises(ResourceLimitError, match="nesting"), OOXMLPackage(deep, ResourceLimits()):
        pass


# ----------------------------------------------------------- workbook cells


def test_sheet_count_cannot_multiply_the_used_range_limit(tmp_path):
    # Each sheet is two cells, but its used range is 50,000 x 5 empty cells.
    # Eight of them are twice the workbook budget.
    path = workbook(tmp_path / "corners.xlsx", [corner_sheet(50_000, "E")] * 8)
    with pytest.raises(ResourceLimitError, match="1000000-cell combined used-range limit"):
        ingest(str(path), Settings())
    # Selecting sheets that fit keeps the workbook usable.
    selected = replace(Settings(), xlsx_sheets=["Sheet 1"])
    assert ingest(str(path), selected).body_html.count("<tr") > 1000


# ------------------------------------------------------------------ images


def test_xlsx_image_placed_many_times_is_carried_once(tmp_path):
    photo = _png((64, 48))
    path = image_workbook(tmp_path / "anchors.xlsx", 200, {"photo.png": photo})
    result = ingest(str(path), Settings())
    assert list(result.assets.values()) == [photo]
    assert result.body_html.count(next(iter(result.assets))) == 200


def test_distinct_xlsx_images_obey_the_embedded_asset_limit(tmp_path):
    images = {f"photo{index}.png": _png((64 + index, 48)) for index in range(3)}
    path = image_workbook(tmp_path / "images.xlsx", 1, images)
    held = sum(len(raw) for raw in images.values())
    assert len(ingest(str(path), _settings(max_embedded_bytes=held)).assets) == 3
    with pytest.raises(ResourceLimitError, match="embedded asset limit"):
        ingest(str(path), _settings(max_embedded_bytes=held - 1))


def test_docx_image_placed_many_times_is_carried_once(tmp_path):
    photo = _png((64, 48))
    result = ingest(str(image_docx(tmp_path / "images.docx", 50, photo)), Settings())
    assert list(result.assets.values()) == [photo]
    assert result.body_html.count(next(iter(result.assets))) == 50
    with pytest.raises(ResourceLimitError, match="embedded asset limit"):
        ingest(
            str(image_docx(tmp_path / "small.docx", 1, photo)),
            _settings(max_embedded_bytes=len(photo) - 1),
        )


# ---------------------------------------------------------------- peak RSS


_PEAK_RSS = textwrap.dedent(
    """
    import json, sys
    from platefold.config import Settings
    from platefold.ingest import ingest
    from platefold.limits import ResourceLimitError

    def peak_mib():
        if sys.platform == "win32":
            import psutil
            return psutil.Process().memory_info().peak_wset / 2**20
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS and KiB on Linux.
        return peak / 2**20 if sys.platform == "darwin" else peak / 1024

    before = peak_mib()
    try:
        ingest(sys.argv[1], Settings())
        outcome = "accepted"
    except ResourceLimitError as exc:
        outcome = str(exc)
    print(json.dumps({"outcome": outcome, "growth_mib": peak_mib() - before}))
    """
)


def test_hostile_docx_is_refused_within_a_bounded_peak_rss(tmp_path):
    if sys.platform == "win32":
        pytest.importorskip("psutil", reason="Windows peak memory is read through psutil")
    # 16 MiB of <a/> is about four million elements in a 250 KB file. Before
    # the node budget, DOCX ingestion of it peaked above 3 GiB.
    path = element_flood_docx(tmp_path / "flood.docx", 16 * 1024 * 1024)
    assert path.stat().st_size < 512 * 1024
    completed = subprocess.run(
        [sys.executable, "-c", _PEAK_RSS, str(path)],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": "0"},
    )
    result = json.loads(completed.stdout)
    assert "node XML limit" in result["outcome"]
    assert result["growth_mib"] < 256, result
