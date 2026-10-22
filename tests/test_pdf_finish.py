"""The streaming finisher marks, edits and copies exactly what pypdf would."""

import pytest
from pypdf import PdfWriter
from pypdf.generic import ContentStream, DecodedStreamObject

from platefold.pdf_finish import mark_page
from platefold.pdf_semantics import mark_artifacts
from platefold.pdfobjects import (
    Name,
    PdfFile,
    PdfShapeError,
    Raw,
    Ref,
    parse,
    serialize,
    text_string,
)

# Shapes Chromium writes: a page background outside any marked content,
# marked cells, clipping ended by n, text, and a sequence crossing q/Q.
CHROMIUM_LIKE = b"""q
3.125 0 0 3.125 0 0 cm
1 1 1 RG 1 1 1 rg
/G3 gs
/Document <</MCID 0 >>BDC
0 0 816 1056 re
f
Q
q
EMC
0 0 703 931 re
f
Q
q
178.125 178.125 2195.7 2910.2 re
W* n
/TR <</MCID 5 >>BDC
1 176 55 24 re
f
EMC
BT
/F6 9.33 Tf
1 0 0 -1 706.35938 1034 Tm
<0033> Tj
6.22 0 Td [<0044> -12 <004A>] TJ
ET
/Span <</MCID 6 /Lang /en >>BDC
10 10 m 20 20 l S
EMC
/Im1 Do
Q
"""


def _operations(data: bytes) -> list:
    stream = DecodedStreamObject()
    stream.set_data(data)
    return ContentStream(stream, None).operations


@pytest.mark.parametrize(
    "content",
    [
        CHROMIUM_LIKE,
        b"0 0 m 1 1 l h W n 0 0 5 5 re f*",
        b"/P <</MCID 0>>BDC 0 0 1 1 re f EMC 2 2 3 3 re B",
        b"/Artifact BMC 0 0 1 1 re f EMC <0001> Tj",
        b"q 1 0 0 1 0 0 cm Q",
    ],
)
def test_fast_marking_matches_the_operation_list_marking(content):
    marked, wrapped = mark_page(content)
    expected, expected_wrapped = mark_artifacts(_operations(content))

    assert wrapped == expected_wrapped
    assert _operations(marked) == expected


def test_a_page_with_literal_strings_is_marked_through_pypdf():
    content = b"/Span <</ActualText (a (b) c)>>BDC <0001> Tj EMC (Tj f) Tj"
    marked, wrapped = mark_page(content)

    assert wrapped == 1
    assert _operations(marked) == mark_artifacts(_operations(content))[0]


def test_a_page_with_nothing_unmarked_is_left_byte_for_byte():
    content = b"/P <</MCID 0 >>BDC 0 0 1 1 re f EMC"
    assert mark_page(content) == (content, 0)


def test_objects_round_trip_through_the_lexer():
    head = b"<</Type /StructElem /S /TD /P 12 0 R /K [5 <</Type /MCR /Pg 7 0 R /MCID 3>>] /A [<</O /Table /Headers [(node\\(1\\))]>>] /W -1.5 /B true>>"
    value = parse(head)

    assert value[b"/P"] == Ref(12)
    assert value[b"/K"][0] == 5
    assert value[b"/K"][1][b"/Pg"] == Ref(7)
    assert value[b"/A"][0][b"/Headers"] == [Raw(b"(node\\(1\\))")]
    assert value[b"/W"] == Raw(b"-1.5")
    assert parse(serialize(value)) == value


def test_text_strings_use_utf16_outside_printable_ascii():
    assert text_string("Plain (x)") == Raw(b"(Plain \\(x\\))")
    assert text_string("Nord ü") == Raw(b"<FEFF004E006F00720064002000FC>")


def test_the_reader_indexes_a_classic_cross_reference_file(tmp_path):
    path = tmp_path / "three.pdf"
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=72, height=72)
    with path.open("wb") as fh:
        writer.write(fh)

    pdf = PdfFile(path)

    assert len(pdf.pages()) == 3
    assert pdf.media_box(pdf.pages()[0]) == (72.0, 72.0)
    assert pdf.root[b"/Type"] == Name(b"/Catalog")
    first = pdf.pages()[0]
    assert pdf.span(first).startswith(b"%d 0 obj" % first)


def test_the_reader_refuses_a_file_without_a_classic_table(tmp_path):
    path = tmp_path / "moved.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as fh:
        writer.write(fh)
    # Point startxref at something that is not a table, as a stream would be.
    head = path.read_bytes().rsplit(b"startxref", 1)[0]
    path.write_bytes(head + b"startxref\n9\n%%EOF\n")

    with pytest.raises(PdfShapeError):
        PdfFile(path)
