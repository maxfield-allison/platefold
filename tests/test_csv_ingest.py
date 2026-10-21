"""CSV/TSV ingestion, resource bounds and table-page routing."""

from __future__ import annotations

import csv
import os
import re
from dataclasses import replace

import pytest
from pypdf import PdfReader

from platefold.cli import build_settings, parse_args
from platefold.config import Settings, make_settings, parse_settings_file
from platefold.document import Document
from platefold.ingest import SUPPORTED_SUFFIXES, ingest
from platefold.limits import ResourceLimitError, ResourceLimits
from platefold.service import collect, plan_outputs
from platefold.style import build_css


class NoDiagrams:
    def render(self, _source):
        raise AssertionError("CSV ingestion must not invoke Mermaid")


def write(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_registry_accepts_csv_and_tsv():
    assert {".md", ".csv", ".tsv"} <= SUPPORTED_SUFFIXES


def test_cli_and_settings_file_control_header_handling(tmp_path):
    args = parse_args(["data.csv", "--no-header"])
    settings, config_path = build_settings(args, str(tmp_path))
    assert settings.csv_header is False and config_path is None
    _, overrides = parse_settings_file("behaviour:\n  csv_header: false\n")
    assert make_settings(**overrides).csv_header is False


def test_service_collects_mixed_formats_and_names_output_collisions(tmp_path):
    for name in ("notes.md", "data.CSV", "raw.tsv", "ignored.txt"):
        write(tmp_path, name, "value")
    files, root = collect(str(tmp_path))
    assert [os.path.basename(path) for path in files] == ["data.CSV", "notes.md", "raw.tsv"]
    with pytest.raises(ValueError, match="Source files map to the same output PDF"):
        plan_outputs(
            [str(tmp_path / "data.CSV"), str(tmp_path / "data.csv")],
            root,
            str(tmp_path / "out"),
        )


def test_quotes_multiline_unicode_and_html_are_preserved_safely(tmp_path):
    path = write(
        tmp_path,
        "Field notes ü.csv",
        'Name,Note,Place\n"Allison, Max","first line\nsecond ""quoted"" line",東京\n'
        "Ana,<script>alert(1)</script>,Málaga\n",
    )
    result = ingest(str(path), Settings())
    assert result.title is None
    assert result.outline == [(1, "Field notes ü", "table")]
    assert (
        '<th scope="col">Name</th><th scope="col">Note</th><th scope="col">Place</th>'
        in result.body_html
    )
    assert "Allison, Max" in result.body_html
    assert "first line<br>second &quot;quoted&quot; line" in result.body_html
    assert "東京" in result.body_html and "Málaga" in result.body_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in result.body_html
    assert "<script>" not in result.body_html


@pytest.mark.parametrize(
    "name,content,expected",
    [
        ("data.tsv", "A\tB\n1\t2\n", '<th scope="col">A</th><th scope="col">B</th>'),
        ("data.csv", "A;B\n1;2\n", '<th scope="col">A</th><th scope="col">B</th>'),
        ("data.csv", "A|B\n1|2\n", '<th scope="col">A</th><th scope="col">B</th>'),
    ],
)
def test_common_dialects_are_detected(tmp_path, name, content, expected):
    assert expected in ingest(str(write(tmp_path, name, content)), Settings()).body_html


def test_utf8_bom_is_not_part_of_first_header(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbfName,Value\nOne,1\n")
    body = ingest(str(path), Settings()).body_html
    assert "\ufeff" not in body
    assert '<th scope="col">Name</th>' in body


def test_no_header_keeps_first_row_in_tbody(tmp_path):
    path = write(tmp_path, "raw.csv", "1,2\n3,4\n")
    body = ingest(str(path), Settings(csv_header=False)).body_html
    assert "<thead>" not in body
    assert "<tbody><tr><td>1</td><td>2</td></tr>" in body


def _one_column(position: str, separator: str) -> tuple[str, list[str]]:
    """A one-column file with a quoted empty record at ``position``."""
    values = ["first", "middle", "last"]
    index = {"first": 0, "middle": 1, "last": len(values)}[position]
    records = [*values[:index], '""', *values[index:]]
    expected = [*values[:index], "", *values[index:]]
    # Blank physical lines, including CRLF ones, are still skipped.
    return "Value" + separator + separator.join(records) + separator + "\r\n\n", expected


def _body_cells(body_html: str) -> list[str]:
    tbody = body_html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    return re.findall(r"<tr><td>(.*?)</td></tr>", tbody)


@pytest.mark.parametrize("suffix", [".csv", ".tsv"])
@pytest.mark.parametrize("position", ["first", "middle", "last"])
@pytest.mark.parametrize("header", [True, False])
def test_one_column_quoted_empty_records_are_kept(tmp_path, suffix, position, header):
    content, expected = _one_column(position, "\n")
    path = write(tmp_path, f"values{suffix}", content)

    body = ingest(str(path), Settings(csv_header=header)).body_html

    if header:
        assert '<thead><tr><th scope="col">Value</th></tr></thead>' in body
    else:
        assert "<thead>" not in body
        expected = ["Value", *expected]
    assert _body_cells(body) == expected
    assert body.count("<tr>") == len(expected) + header


@pytest.mark.parametrize("suffix", [".csv", ".tsv"])
def test_a_quoted_empty_header_is_a_column_not_a_blank_line(tmp_path, suffix):
    path = write(tmp_path, f"values{suffix}", '""\r\none\r\n\r\ntwo\r\n')
    body = ingest(str(path), Settings()).body_html
    assert '<thead><tr><th scope="col"></th></tr></thead>' in body
    assert _body_cells(body) == ["one", "two"]
    body = ingest(str(path), Settings(csv_header=False)).body_html
    assert _body_cells(body) == ["", "one", "two"]


def test_only_blank_lines_are_still_an_empty_table(tmp_path):
    with pytest.raises(ValueError, match="contains no rows"):
        ingest(str(write(tmp_path, "blank.csv", "\r\n\n\n")), Settings())


@pytest.mark.parametrize(
    "content,message",
    [
        ('A,B\n"unterminated,1\n', "Malformed delimited table"),
        ("A,B\n1\n", "row 2 has 1 fields; expected 2"),
        ("\n\n", "contains no rows"),
        ("A,B\n1,\x00\n", "unsupported control"),
    ],
)
def test_malformed_inputs_fail_with_bounded_errors(tmp_path, content, message):
    path = write(tmp_path, "bad.csv", content)
    with pytest.raises(ValueError, match=message):
        ingest(str(path), Settings())


def test_non_utf8_input_is_rejected(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_bytes(b"Name\n\xff\n")
    with pytest.raises(ValueError, match="must be UTF-8"):
        ingest(str(path), Settings())


@pytest.mark.parametrize(
    "field,value,content,message",
    [
        ("max_csv_bytes", 4, "A,B\n1,2\n", "4-byte limit"),
        ("max_csv_rows", 1, "A,B\n1,2\n", "1-row limit"),
        ("max_csv_columns", 1, "A,B\n", "1-column limit"),
        ("max_csv_cells", 3, "A,B\n1,2\n", "3-cell limit"),
        ("max_csv_field_characters", 3, "Name\nvalue\n", "3-character limit"),
    ],
)
def test_resource_limits(field, value, content, message, tmp_path):
    limits = replace(ResourceLimits(), **{field: value})
    with pytest.raises(ResourceLimitError, match=message):
        ingest(str(write(tmp_path, "large.csv", content)), Settings(limits=limits))


def test_multiline_records_and_blank_lines_use_logical_row_budget(tmp_path):
    path = write(tmp_path, "logical.csv", '\n\nA,B\n"line 1\nline 2",x\n')
    limits = replace(ResourceLimits(), max_csv_rows=2)
    result = ingest(str(path), Settings(limits=limits))
    assert "line 1<br>line 2" in result.body_html


def test_stdlib_field_limit_is_restored(tmp_path):
    previous = csv.field_size_limit()
    limits = replace(ResourceLimits(), max_csv_field_characters=64)
    ingest(str(write(tmp_path, "small.csv", "A\nvalue\n")), Settings(limits=limits))
    assert csv.field_size_limit() == previous


def test_huge_configured_field_limit_is_clamped_to_platform_capacity(tmp_path):
    limits = replace(ResourceLimits(), max_csv_field_characters=10**100)
    result = ingest(str(write(tmp_path, "small.csv", "A\nvalue\n")), Settings(limits=limits))
    assert "value" in result.body_html


def render(tmp_path, name: str, content: str, settings: Settings | None = None):
    path = write(tmp_path, name, content)
    return Document(str(path), str(tmp_path), settings or Settings(), NoDiagrams()).render()


def document_body(page: str) -> str:
    return page.split("</head><body>", 1)[1]


def test_three_column_table_stays_inline_without_legacy_wrapper(tmp_path):
    page, info = render(tmp_path, "narrow.csv", "A,B,C\n1,2,3\n")
    body = document_body(page)
    assert 'class="data-table inline-table"' in body
    assert "table-plate" not in body and "wide-table" not in body
    assert body.count('class="pf-data-table"') == 1
    assert body.count(">narrow</h1>") == 1
    assert info["title"] == "narrow"


def test_forty_column_table_uses_one_custom_plate_wrapper(tmp_path):
    headings = ",".join(f"Column {index}" for index in range(40))
    values = ",".join(str(index) for index in range(40))
    page, _ = render(tmp_path, "wide.csv", f"{headings}\n{values}\n")
    body = document_body(page)
    assert body.count("table-plate") == 1
    assert "wide-table" not in body
    assert "@page table-widecsv" in page


def test_long_narrow_table_is_width_driven_and_can_paginate(tmp_path):
    rows = "\n".join(f"{index},{index + 1},{index + 2}" for index in range(1000))
    page, _ = render(tmp_path, "long.csv", "A,B,C\n" + rows)
    body = document_body(page)
    assert "inline-table" in body and "table-plate" not in body
    css = build_css({}, Settings())
    assert "thead { display: table-header-group; }" in " ".join(css.split())
    assert ".table-plate { break-before: page" in " ".join(css.split())
    assert ".table-plate { break-inside" not in " ".join(css.split())


def test_markdown_wide_table_keeps_legacy_heuristic(tmp_path):
    markdown = "| " + " | ".join(f"Heading {i}" for i in range(8)) + " |\n"
    markdown += "| " + " | ".join("---" for _ in range(8)) + " |\n"
    markdown += "| " + " | ".join("value" for _ in range(8)) + " |\n"
    page, _ = render(tmp_path, "table.md", markdown)
    body = document_body(page)
    assert 'class="wide-table"' in body
    assert "pf-data-table" not in body and "table-plate" not in body


@pytest.mark.integration
def test_long_csv_repeats_its_header_on_real_pdf_pages(tmp_path, run_export):
    rows = "\n".join(f"row {index},value {index},note {index}" for index in range(250))
    source = write(tmp_path, "long.csv", "Name,Value,Note\n" + rows + "\n")

    code, output = run_export(source, "--no-toc", "--no-bookmarks")

    assert code == 0
    pages = PdfReader(output / "long.pdf").pages
    assert len(pages) >= 2
    for page in pages:
        text = page.extract_text() or ""
        assert "Name" in text and "Value" in text and "Note" in text


def _baselines(pdf) -> dict[str, float]:
    """The vertical position of each single-word line on the first page."""
    positions: dict[str, float] = {}

    def visit(text, _cm, tm, _font, _size):
        if text.strip():
            positions.setdefault(text.strip(), tm[5])

    PdfReader(pdf).pages[0].extract_text(visitor_text=visit)
    return positions


@pytest.mark.integration
@pytest.mark.parametrize("suffix", [".csv", ".tsv"])
@pytest.mark.parametrize("header", [True, False])
def test_exported_pdf_keeps_every_quoted_empty_record(tmp_path, run_export, suffix, header):
    """Count the printed rows from where the surrounding records land.

    The empty records carry no text, and Chromium leaves rows without content
    out of the PDF's structure tree, so the count comes from layout: each
    printed empty record moves the words after it down by one empty row.
    """
    flags = ["--no-toc", "--no-bookmarks"] + ([] if header else ["--no-header"])
    plain = write(tmp_path, f"plain{suffix}", "Value\nalpha\nbravo\ncharlie\n")
    spaced = write(tmp_path, f"spaced{suffix}", 'Value\n""\nalpha\n""\nbravo\n""\ncharlie\n""\n')

    code, plain_out = run_export(plain, *flags)
    assert code == 0
    code, spaced_out = run_export(spaced, *flags)
    assert code == 0

    # Chromium's text matrix grows down the page. An empty record prints as a
    # shorter row than one with text, so the height of one is measured from
    # the single empty record between Value and alpha.
    before = _baselines(plain_out / "plain.pdf")
    after = _baselines(spaced_out / "spaced.pdf")
    empty_row = (after["alpha"] - after["Value"]) - (before["alpha"] - before["Value"])
    assert empty_row > 0
    # Two more between alpha and charlie. The trailing empty record has no
    # text below it to measure; the ingest tests cover it.
    added = (after["charlie"] - after["alpha"]) - (before["charlie"] - before["alpha"])
    assert added == pytest.approx(2 * empty_row, abs=0.5)
