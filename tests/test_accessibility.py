"""Final-file structural regressions; these are not screen-reader acceptance."""

import hashlib
import os
import re
import shutil
from itertools import pairwise
from pathlib import Path

import pytest
from pypdf import PdfReader
from test_docx_ingest import DOCUMENT_BODY, STYLES, make_docx

from platefold.accessibility import valid_language
from platefold.cli import build_settings, parse_args
from platefold.config import Settings, make_settings, parse_settings_file
from platefold.document import Document
from platefold.ingest import ingest
from platefold.service import ExportRequest, export
from tools.a11y.compare import raster_hashes
from tools.a11y.corpus import create
from tools.a11y.inspect_pdf import inspect


@pytest.mark.parametrize("value", [None, True, 1, [], "en_US", 'en" onclick="x', "e", "-en", "en-"])
def test_language_rejects_non_tags(value):
    assert not valid_language(value)
    with pytest.raises(ValueError, match="document_language"):
        make_settings(document_language=value if value is not None else True)


def test_language_file_cli_and_source_are_independent_of_paper_locale(tmp_path):
    source = tmp_path / "source.md"
    source.write_text(
        '# Language specimen\n\nLanguage: fr-CA\n\nBonjour. <span lang="en">Hello.</span>'
    )
    settings, _ = build_settings(
        parse_args([str(source), "--locale", "de-DE", "--document-language", "fr-CA"]),
        str(tmp_path),
    )
    assert settings.document_language == "fr-CA"
    _, overrides = parse_settings_file("behaviour:\n  document_language: fr-CA\n", "yaml")
    assert overrides["document_language"] == "fr-CA"
    body, info = Document(str(source), str(tmp_path), settings, None).render()
    assert '<html lang="fr-CA">' in body
    assert '<span lang="en">Hello.</span>' in body
    assert info["language"] == "fr-CA"


def _docx(path):
    styles = STYLES.replace(
        "<w:style ",
        '<w:docDefaults><w:rPrDefault><w:rPr><w:lang w:val="en-GB"/></w:rPr></w:rPrDefault></w:docDefaults><w:style ',
        1,
    )
    body = (
        DOCUMENT_BODY
        + '<w:p><w:r><w:rPr><w:lang w:val="fr-CA"/></w:rPr><w:t>Bonjour.</w:t></w:r></w:p>'
    )
    # Authored repeating headers and merged cells must remain semantics, not guesses.
    body += '<w:tbl><w:tr><w:trPr><w:tblHeader/></w:trPr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Group</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>First</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Second</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    return make_docx(path, body=body, extra={"word/styles.xml": styles})


def test_docx_preserves_authored_language_headers_and_source_bytes(tmp_path):
    source = _docx(tmp_path / "language.docx")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = ingest(str(source), Settings())
    assert result.language == "en-GB"
    assert '<span lang="fr-CA">Bonjour.</span>' in result.body_html
    assert '<th colspan="2">' in result.body_html
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "case,mode",
    [
        ("prose", "online"),
        ("semantics", "online"),
        ("semantics", "print"),
        ("tiled", "tiled"),
        ("wide-csv", "online"),
        ("wide-csv", "print"),
        ("wide-csv", "tiled"),
        ("long-toc", "online"),
        ("docx", "online"),
        ("docx", "print"),
        ("docx", "tiled"),
    ],
)
def test_delivered_structure_survives_real_export_and_bookmarks(tmp_path, monkeypatch, case, mode):
    from platefold import service

    cases = create(tmp_path / "sources")
    source = _docx(tmp_path / "sources" / "language.docx") if case == "docx" else cases[case][0]
    settings = make_settings(output_mode=mode, document_language="en-GB")
    original = service.print_pdf
    captures = []

    def capture(page, html, destination, *args, **options):
        original(page, html, destination, *args, **options)
        copy = tmp_path / f"print-{len(captures)}.pdf"
        copy.write_bytes(Path(destination).read_bytes())
        captures.append(copy)

    monkeypatch.setattr(service, "print_pdf", capture)
    result = export(
        ExportRequest(
            str(source),
            str(tmp_path / "final"),
            settings=settings,
            diagram_cache=str(tmp_path / "cache"),
            browser="bundled",
        )
    )
    assert result.status in {"completed", "completed_with_warnings"}
    final = next((tmp_path / "final").rglob("*.pdf"))
    before, after = inspect(captures[-1]), inspect(final)
    assert after["structure_tree"] and after["parent_tree"]
    assert after["language"] == "en-GB"
    assert after["parent_tree_errors"] == []
    # The only structural change is an LBody around list-item content.
    assert _without_list_bodies(after["nodes"]) == _without_list_bodies(before["nodes"])
    assert after["page_struct_parents"] == before["page_struct_parents"]
    assert after["annotations"] == before["annotations"]
    assert raster_hashes(final) == raster_hashes(captures[-1])
    assert not any(node.get("invalid_page_reference") for node in after["nodes"])
    assert not re.search(r"Zq[0-9a-f]{8}", "".join(after["text"]))
    if case in {"prose", "tiled"}:
        sequence = _tagged_text(final, after["nodes"])
        if case == "prose":
            assert (
                sequence.index("ALPHA begins")
                < sequence.index("BRAVO follows")
                < sequence.index("CHARLIE ends")
            )
        else:
            assert sequence.index("Twelve step process") < sequence.index("DELTA follows")
            assert "review record" not in sequence
            assert "Assemble left to right" not in sequence
    if case == "semantics":
        assert after["alternatives"] == [
            "A navy rectangle centered on a white background",
            "Approval flow Start proceeds to Review, then to Finish.",
            "Diagram: author description missing",
        ]
        assert after["roles"]["/TH"] == 3
        assert sum("/Headers" in str(n.get("/A")) for n in after["nodes"]) == 6
    if case == "tiled":
        assert after["pages"] > 2
        assert after["roles"]["/Figure"] == 1
        assert after["alternatives"] == [
            "Twelve step process Steps proceed in numerical order from step 1 to step 12."
        ]
    if case == "docx":
        assert any(n.get("/Lang") == "fr-CA" for n in after["nodes"])
        assert any(
            "/ColSpan" in str(n.get("/A")) and "2" in str(n.get("/A")) for n in after["nodes"]
        )
    with PdfReader(final) as reader:
        assert reader.outline
        _assert_accessibility_repairs(reader)
    _keep_evidence(final, f"{case}-{mode}")


def _keep_evidence(pdf: Path, name: str) -> None:
    """Copy a delivered PDF where CI validates it against PDF/UA-1 with veraPDF."""
    directory = os.environ.get("PLATEFOLD_PDF_EVIDENCE_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf, Path(directory) / f"{name}.pdf")


def _without_list_bodies(nodes: list[dict]) -> list[dict]:
    """Drop LBody elements and lift their descendants one level."""
    result, lifted = [], []
    for node in nodes:
        while lifted and node["depth"] <= lifted[-1]:
            lifted.pop()
        if node.get("/S") == "/LBody":
            lifted.append(node["depth"])
            continue
        result.append({**node, "depth": node["depth"] - len(lifted)})
    return result


def _assert_accessibility_repairs(reader: PdfReader) -> None:
    """The checks behind PDF/UA-1 rules 7.1, 7.2, 7.4.2 and 7.18 that failed before."""
    from pypdf.generic import ContentStream

    from platefold.pdf_semantics import mark_artifacts

    for page in reader.pages:
        if "/StructParents" in page:
            content = ContentStream(page.get_contents(), reader)
            assert mark_artifacts(content.operations)[1] == 0, "unmarked painting remains"
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            if annotation.get("/Subtype") == "/Link":
                assert str(annotation.get("/Contents", "")).strip()
    metadata = reader.root_object["/Metadata"].get_object().get_data().decode("utf-8")
    assert "<dc:title>" in metadata and "pdfuaid" not in metadata
    levels = []
    pending = [reader.root_object["/StructTreeRoot"]]
    while pending:
        node = pending.pop(0).get_object()
        kids = node.get("/K", [])
        kids = kids if isinstance(kids, list) else [kids]
        if node.get("/S") == "/LI":
            assert {str(k.get_object().get("/S")) for k in kids} <= {"/Lbl", "/LBody"}
        if str(node.get("/S", "")) in {f"/H{n}" for n in range(1, 7)}:
            levels.append(int(str(node["/S"])[2:]))
        pending[:0] = [
            k for k in kids if hasattr(k, "get_object") and hasattr(k.get_object(), "get")
        ]
    assert levels and levels[0] == 1
    assert all(b <= a + 1 for a, b in pairwise(levels))


@pytest.mark.integration
def test_language_unknown_is_reported_without_guessing(tmp_path):
    source = tmp_path / "unknown.md"
    source.write_text("# Example\n\nText without declared language.")
    result = export(
        ExportRequest(
            str(source),
            str(tmp_path / "final"),
            settings=Settings(bookmarks=False),
            diagram_cache=str(tmp_path / "cache"),
            browser="bundled",
        )
    )
    final = next((tmp_path / "final").glob("*.pdf"))
    assert inspect(final)["language"] == "und"
    # Without bookmarks the final rewrite still applies the structure repairs.
    with PdfReader(final) as reader:
        assert "/Metadata" in reader.root_object
    _keep_evidence(final, "no-bookmarks")
    assert "Document language is not set" in str(result.report)
    assert "Set the document language to en" in str(result.report)
    assert "Export settings" not in str(result.report)


def test_cli_names_its_own_controls_once_per_document():
    """Engine warnings are surface-neutral; the CLI adds its flags, not GUI controls."""
    import io

    from platefold.accessibility import document_language
    from platefold.cli import progress_reporter
    from platefold.i18n import translator

    (_code, neutral), *_ = document_language("", None)[1]
    assert "Export settings" not in neutral
    warnings = [f"DOCUMENT_LANGUAGE: {neutral}"] + [
        f"FONT_SUBSTITUTED: Calibri is unavailable to the renderer for {role} text"
        for role in ("body", "head", "mono")
    ]
    record = {"pages": 1, "figures": 0, "missing_images": [], "diagram_errors": []}
    record["warnings"] = warnings
    stream = io.StringIO()
    progress_reporter(False, stream, messages=translator("en"))(1, 1, "a.md", record)
    printed = stream.getvalue()
    assert "--document-language en" in printed
    assert printed.count("--font sora") == 1
    assert "Export settings" not in printed and "restart" not in printed
    quiet = io.StringIO()
    progress_reporter(False, quiet, messages=translator("en"), hints=False)(1, 1, "a.md", record)
    assert "--document-language" not in quiet.getvalue()
    assert all(warning in quiet.getvalue() for warning in warnings)


def test_docx_decorative_image_is_explicit_and_not_an_invented_description(tmp_path):
    body = DOCUMENT_BODY.replace(
        '<wp:docPr id="1" name="Proof" descr="Embedded proof"/>',
        '<wp:docPr id="1" name="Proof" descr="Embedded proof"><a:extLst><a:ext uri="decorative"><d:decorative xmlns:d="http://schemas.microsoft.com/office/drawing/2017/decorative" val="1"/></a:ext></a:extLst></wp:docPr>',
    )
    source = make_docx(tmp_path / "decorative.docx", body=body)
    result = ingest(str(source), Settings())
    assert 'alt=""' in result.body_html
    assert "__platefold_explicitly_decorative__" not in result.body_html
    assert "Embedded proof" not in result.body_html


@pytest.mark.parametrize(
    "tag",
    [
        "en",
        "en-GB",
        "fr-CA",
        "zh-Hant-TW",
        "es-419",
        "und",
        "x-author",
        "en-u-ca-gregory",
        "i-klingon",
    ],
)
def test_language_accepts_valid_syntax(tag):
    assert valid_language(tag)


def _tagged_text(path, nodes):
    from collections import defaultdict

    import pdfplumber

    text = defaultdict(str)
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            for char in page.chars:
                if char.get("mcid") is not None:
                    text[(number, char["mcid"])] += char["text"]
    return " ".join(
        n.get("/Alt", "") or text[(n["page"], n["mcid"])]
        for n in nodes
        if "/Alt" in n or "mcid" in n
    )


@pytest.mark.integration
def test_authored_inline_svg_alternative_and_language_reach_final_pdf(tmp_path):
    source = tmp_path / "inline.md"
    source.write_text("""# Inline semantics

Language: en

ALPHA <span lang="fr-CA">Bonjour.</span> BRAVO.

<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"><title>Test rectangle</title><desc>A blue rectangle fills the view.</desc><rect width="120" height="80" fill="blue"/></svg>

CHARLIE.
""")
    export(
        ExportRequest(
            str(source),
            str(tmp_path / "final"),
            diagram_cache=str(tmp_path / "cache"),
            browser="bundled",
        )
    )
    final = tmp_path / "final" / "inline.pdf"
    structure = inspect(final)
    assert structure["language"] == "en"
    assert structure["alternatives"] == ["Test rectangle A blue rectangle fills the view."]
    assert any(n.get("/Lang") == "fr-CA" for n in structure["nodes"])
    assert not structure["parent_tree_errors"]


def test_only_unmarked_painting_becomes_an_artifact():
    from pypdf.generic import DictionaryObject, NameObject, NumberObject

    from platefold.pdf_semantics import mark_artifacts

    tagged = [NameObject("/P"), DictionaryObject({NameObject("/MCID"): NumberObject(0)})]
    operations = [
        ([], b"q"),
        ([0, 0, 10, 10], b"re"),
        ([], b"f"),  # background: painted outside any sequence
        ([0, 0, 5, 5], b"re"),
        ([], b"W"),
        ([], b"n"),  # clipping path: paints nothing
        (tagged, b"BDC"),
        ([], b"BT"),
        (["x"], b"Tj"),  # real, tagged text
        ([], b"ET"),
        ([], b"EMC"),
        ([], b"BT"),
        (["y"], b"Tj"),  # untagged text inside a text object
        ([], b"ET"),
        ([NameObject("/Im1")], b"Do"),
        ([], b"Q"),
    ]
    result, wrapped = mark_artifacts(operations)
    operators = [operator for _, operator in result]
    assert wrapped == 3
    assert operators == [
        b"q",
        *(b"BMC", b"re", b"f", b"EMC"),
        b"re",
        b"W",
        b"n",
        b"BDC",
        b"BT",
        b"Tj",
        b"ET",
        b"EMC",
        b"BT",
        *(b"BMC", b"Tj", b"EMC"),
        b"ET",
        *(b"BMC", b"Do", b"EMC"),
        b"Q",
    ]
    assert mark_artifacts(result)[1] == 0
