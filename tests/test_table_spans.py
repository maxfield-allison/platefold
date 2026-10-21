"""Table markup is rewritten so that its row spans are the ones laid out.

Chromium tags a cell with the row span its markup gives, but leaves rows with
no cells of their own out of the tagged table and does not end a span with
its row group. ``table_spans.normalize`` clamps each span to its group and
hides each row without cells that a span covers, counting it out of the
spans over it. The browser tests in test_pdf_ua_fixtures and
test_large_tables check the tagged PDFs and that the pages do not change.
"""

import time

import pytest

from platefold import chunked, htmlplan
from platefold.limits import ResourceLimitError
from platefold.table_spans import HIDDEN_ROW, WARNING, normalize

HEAD = "<thead><tr><th>A</th><th>B</th></tr></thead>"
HID = f"<tr{HIDDEN_ROW}>"


@pytest.mark.parametrize(
    "html,expected",
    [
        # A cell over two rows with no cells of their own.
        (
            f"<table>{HEAD}<tbody><tr><td rowspan=3 colspan=2>x</td></tr><tr></tr><tr></tr>"
            "<tr><td>a</td><td>b</td></tr></tbody></table>",
            f"<table>{HEAD}<tbody><tr><td colspan=2>x</td></tr>{HID}</tr>{HID}</tr>"
            "<tr><td>a</td><td>b</td></tr></tbody></table>",
        ),
        # A row without cells that a span covers only in part; the next row keeps its span.
        (
            f"<table>{HEAD}<tbody><tr><td rowspan='3'>x</td><td>y</td></tr><tr></tr>"
            '<tr><td class="c">b</td></tr></tbody></table>',
            f'<table>{HEAD}<tbody><tr><td rowspan="2">x</td><td>y</td></tr>{HID}</tr>'
            '<tr><td class="c">b</td></tr></tbody></table>',
        ),
        # Spans past the end of a body and of a header, and a span of 0.
        (
            "<table><thead><tr><th rowspan=5>A</th><th>B</th></tr><tr><th>C</th></tr></thead>"
            "<tbody><tr><td>a</td><td rowspan=3 class=q>b</td></tr><tr><td>c</td></tr></tbody>"
            "<tbody><tr><td ROWSPAN=0>d</td><td>e</td></tr><tr><td>f</td></tr></tbody></table>",
            '<table><thead><tr><th rowspan="2">A</th><th>B</th></tr><tr><th>C</th></tr></thead>'
            '<tbody><tr><td>a</td><td rowspan="2" class=q>b</td></tr><tr><td>c</td></tr></tbody>'
            '<tbody><tr><td rowspan="2">d</td><td>e</td></tr><tr><td>f</td></tr></tbody></table>',
        ),
        # Footers, repeated header and footer groups: each ends its own spans.
        (
            "<table><tfoot><tr><td rowspan=2>f</td><td>g</td></tr></tfoot>"
            "<thead><tr><th rowspan=2>h</th><th>i</th></tr><tr><th>j</th></tr></thead>"
            "<thead><tr><th rowspan=4>k</th><th>l</th></tr></thead>"
            "<tfoot><tr><td rowspan=2>m</td><td>n</td></tr><tr></tr></tfoot></table>",
            "<table><tfoot><tr><td>f</td><td>g</td></tr></tfoot>"
            "<thead><tr><th rowspan=2>h</th><th>i</th></tr><tr><th>j</th></tr></thead>"
            "<thead><tr><th>k</th><th>l</th></tr></thead>"
            f"<tfoot><tr><td>m</td><td>n</td></tr>{HID}</tr></tfoot></table>",
        ),
        # End tags the parser implies, and the body it adds.
        (
            "<table><tr><th>X<th>Y<tr><td rowspan=3>x<td>y<tr><tr><td>z</table>",
            f'<table><tr><th>X<th>Y<tr><td rowspan="2">x<td>y{HID}<tr><td>z</table>',
        ),
        # A row group start ends the open cell, row and group; </th> does not end a td.
        (
            f"<table>{HEAD}<tr><td rowspan=4>a</th><td>b<tbody><tr><td>c<td>d</table>",
            f"<table>{HEAD}<tr><td>a</th><td>b<tbody><tr><td>c<td>d</table>",
        ),
        # A quote in an unquoted value, and a tag name in an attribute value.
        (
            "<table><tr><td title=it's data-x='<td>' rowspan=2>q</td></tr><tr></tr></table>",
            f"<table><tr><td title=it's data-x='<td>'>q</td></tr>{HID}</tr></table>",
        ),
    ],
)
def test_spans_are_written_as_laid_out(html, expected):
    assert normalize(f"<p>Before</p>{html}<p>After</p>") == f"<p>Before</p>{expected}<p>After</p>"


@pytest.mark.parametrize(
    "html",
    [
        "<table><tr><td>a</td></tr></table>",
        f"<table>{HEAD}<tbody><tr><td rowspan=2>a</td><td>b</td></tr>"
        "<tr><td>c</td></tr></tbody></table>",
        # A row with no cells and no span over it is left as it is.
        f"<table>{HEAD}<tbody><tr><td rowspan=1>a</td><td>b</td></tr><tr></tr></tbody></table>",
        # Text that only looks like a table.
        "<script>'<table><tr><td rowspan=4>'</script><!-- <table><tr><td rowspan=9> -->"
        "<template><table><tr><td rowspan=3>x</td></tr><tr></tr></table></template>"
        "<textarea><table><tr><td rowspan=5>y</textarea>",
        # A table whose markup SVG content holds table tags is left alone.
        "<table><tr><td rowspan=2><svg><tr></tr></svg></td></tr><tr></tr></table>",
    ],
)
def test_tables_laid_out_as_written_are_left_alone(html):
    assert normalize(html) == html


def test_comment_forms_the_parser_ends_early_do_not_hide_tables():
    html = "<!--><table><tr><td rowspan=2>a</td></tr><tr></tr></table><!--->"
    assert normalize(html) == f"<!--><table><tr><td>a</td></tr>{HID}</tr></table><!--->"


def test_svg_title_and_style_hold_markup_not_text():
    html = "<svg><title>t</title><style>s</style></svg><table><tr><td rowspan=2>a</td></tr><tr></tr></table>"
    assert normalize(html).endswith(f"<table><tr><td>a</td></tr>{HID}</tr></table>")


def test_nested_tables_are_normalized_each_on_their_own():
    inner = "<table><tr><td rowspan=2>in</td></tr><tr></tr></table>"
    html = f"<table><tr><td>{inner}</td><td rowspan=3>outer</td></tr><tr></tr></table>"
    assert normalize(html) == (
        f"<table><tr><td><table><tr><td>in</td></tr>{HID}</tr></table></td>"
        f"<td>outer</td></tr>{HID}</tr></table>"
    )


@pytest.mark.parametrize(
    "row",
    ['<tr style="height:30pt">', '<tr id="target">', "<tr class=note>"],
)
def test_a_covered_row_with_attributes_is_kept_and_reported(row):
    # Hiding it could drop an authored height or a link target; the other row is hidden.
    html = f"<table><tr><td rowspan=3>a</td><td>b</td></tr>{row}</tr><tr></tr></table>"
    warnings = []
    out = normalize(html, warn=lambda code, detail: warnings.append((code, detail)))
    assert out == f'<table><tr><td rowspan="2">a</td><td>b</td></tr>{row}</tr>{HID}</tr></table>'
    assert [code for code, _ in warnings] == [WARNING]
    assert warnings[0][1].startswith("Table 1 ")


def test_a_table_left_alone_for_its_svg_is_reported():
    warnings = []
    html = "<table><tr><td>x</td></tr></table><table><tr><td rowspan=2><svg><tr></svg></td></tr><tr></tr></table>"
    assert normalize(html, warn=lambda code, detail: warnings.append((code, detail))) == html
    assert [(code, detail.split(" holds")[0]) for code, detail in warnings] == [
        (WARNING, "Table 2")
    ]


def test_tables_whose_spans_are_written_as_laid_out_are_not_reported():
    warnings = []
    html = f"<table>{HEAD}<tbody><tr><td rowspan=3>a</td><td>b</td></tr><tr></tr><tr><td>c</td></tr></tbody></table>"
    normalize(html, warn=lambda code, detail: warnings.append((code, detail)))
    assert warnings == []


def test_the_part_planner_counts_spans_in_laid_out_rows():
    # A cell over three rows, the middle one without cells: the third is still under it.
    rows = "<tr><td rowspan=3>a</td><td>b</td><td>c</td></tr><tr></tr><tr><td>d</td><td>e</td></tr>"
    html = normalize(f"<body><table><tbody>{rows * 3}</tbody></table></body>")
    table = htmlplan.scan(html).elements[0].tables[0]
    assert [row.hidden for row in table.rows[:3]] == [False, True, False]
    assert table.restart_allowed()[:4] == [True, False, False, True]
    assert [cover.column for cover in table.rows[2].covered] == [0]
    copy = chunked.standalone_row(table, html, 2)
    assert copy is not None and copy.index("<td>d</td>") > copy.index("<td")


def test_normalizing_is_linear_on_large_and_hostile_input():
    started = time.perf_counter()
    normalize("rowspan" + "<table><tr><td>" * 20_000 + "<svg>" + "<td>" * 20_000)
    assert time.perf_counter() - started < 2
    started = time.perf_counter()  # many SVG blocks, each meeting every open table
    normalize("rowspan" + "<table><tr><td>" * 20_000 + "<svg><td></svg>" * 20_000)
    assert time.perf_counter() - started < 2
    rows = "".join(
        f'<tr><td rowspan="2">{i}</td>' + "<td>x</td>" * 19 + "</tr><tr></tr>"
        for i in range(10_000)
    )
    started = time.perf_counter()
    normalize(f"<table>{rows}</table>" + "<td a b c d e f g h " * 20_000)
    assert time.perf_counter() - started < 5


def test_an_authored_covered_marker_does_not_hide_a_row_with_cells():
    def allowed(marker):
        html = (
            '<body><table><tr><td rowspan="2">a</td><td>b</td></tr>'
            f"<tr{marker}><td>c</td></tr><tr><td>d</td><td>e</td></tr></table></body>"
        )
        table = htmlplan.scan(html).elements[0].tables[0]
        return table.restart_allowed(), [row.hidden for row in table.rows]

    assert allowed(f" {htmlplan.HIDDEN_ROW}") == allowed("")
    assert allowed("") == ([True, False, True], [False, False, False])


def test_a_continuation_meets_the_last_row_with_cells_before_it():
    html = normalize(
        "<body><table><tbody>"
        + "".join(f"<tr><td>{i}</td><td>x</td></tr>" for i in range(40))
        + '<tr><td rowspan="3" colspan="2">block</td></tr><tr></tr><tr></tr>'
        + "".join(f"<tr><td>{i}</td><td>y</td></tr>" for i in range(40))
        + "</tbody></table></body>"
    )
    table = htmlplan.scan(html).elements[0].tables[0]
    assert [row.cells for row in table.rows[40:44]] == [2, 0, 0, 2]  # grid columns filled
    assert chunked.repeated_from(table, 43) == 40  # not a hidden row


def test_a_nested_table_is_reported_by_the_table_holding_it():
    # Tables are numbered as a reader counts them, at the top level.
    kept = "<table><tr><td rowspan=3>a</td><td>b</td></tr><tr id=t></tr><tr></tr></table>"
    html = f"<table><tr><td>{kept}</td></tr></table>{kept}"
    warnings = []
    normalize(html, warn=lambda code, detail: warnings.append((code, detail)))
    assert [detail.split(" has ")[0] for _, detail in warnings] == [
        "A table inside Table 1",
        "Table 2",
    ]


PAD = '<td class="pf-grid-pad">&#160;</td>'


@pytest.mark.parametrize(
    "html,expected",
    [
        # Rows shorter than the widest are filled out with cells that draw nothing.
        (
            "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td><td>3</td></tr>"
            "<tr><td>only</td></tr></table>",
            f'<table><tr class="pf-short-row"><th>A</th><th>B</th>{PAD}</tr>'
            "<tr><td>1</td><td>2</td><td>3</td></tr>"
            '<tr class="pf-short-row"><td>only</td><td class="pf-grid-pad" colspan="2">&#160;</td>'
            "</tr></table>",
        ),
        # End tags the parser implies; an authored class is kept beside the row's.
        (
            "<table><tr><th>A<th>B<th>C<tr><td>x<tr class='odd' id=r3><td>y<td>z</table>",
            '<table><tr><th>A<th>B<th>C<tr class="pf-short-row"><td>x'
            '<td class="pf-grid-pad" colspan="2">&#160;</td>'
            f'<tr class="pf-short-row odd" id=r3><td>y<td>z{PAD}</table>',
        ),
        # A cell spanning down from above splits the gap: one empty cell each side of it.
        (
            "<table><tr><td>a</td><td>b</td><td rowspan=2>c</td><td>d</td></tr>"
            "<tr><td>e</td></tr></table>",
            "<table><tr><td>a</td><td>b</td><td rowspan=2>c</td><td>d</td></tr>"
            f'<tr class="pf-short-row"><td>e</td>{PAD}{PAD}</tr></table>',
        ),
        # A nested table is filled out on its own, inside the cell holding it.
        (
            "<table><tr><td><table><tr><td>n1</td><td>n2</td></tr><tr><td>n3</td></tr></table>"
            "</td><td>o</td></tr><tr><td>p</td></tr></table>",
            "<table><tr><td><table><tr><td>n1</td><td>n2</td></tr>"
            f'<tr class="pf-short-row"><td>n3</td>{PAD}</tr></table></td><td>o</td></tr>'
            f'<tr class="pf-short-row"><td>p</td>{PAD}</tr></table>',
        ),
    ],
)
def test_short_rows_of_authored_tables_are_filled_out(html, expected):
    assert normalize(html) == expected


@pytest.mark.parametrize(
    "html",
    [
        # Readers write regular tables; theirs are not read again.
        '<table class="pf-data-table" data-natural-width="9"><tr><td>a</td><td>b</td></tr>'
        "<tr><td>c</td></tr></table>",
        # A row the markup never ends has nowhere to put the cells.
        "<table><tr><td>a</td><td>b</td></tr><tr><td>c",
    ],
)
def test_tables_without_a_place_to_fill_are_left_alone(html):
    assert normalize(html) == html


def test_filling_short_rows_stays_within_the_document_cell_limit():
    wide = "<tr>" + "<td colspan=1000>w</td>" * 3 + "</tr>"
    html = "<table>" + wide + "<tr><td>x</td></tr>" * 50 + "</table>"
    with pytest.raises(ResourceLimitError, match="cell limit"):
        normalize(html, max_cells=100)
    assert normalize(html, max_cells=1_000) != html
