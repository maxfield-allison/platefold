"""Planning a large document's parts, and the markup each part prints."""

import re
from dataclasses import replace

import pytest

from platefold import chunked, htmlplan, pdfjoin
from platefold.chunked import Cut, PartPlan, Start
from platefold.config import Settings
from platefold.limits import ResourceLimitError, ResourceLimits
from platefold.pdf import parts_layout

HEAD = (
    "<!doctype html><html><head><style>@page { size: 215.9mm 279.4mm; margin: 15mm 15mm 18mm 15mm; }"
    ".table-plate { break-before: page; break-after: page; }</style></head><body>"
)
TAIL = "</body></html>"


def rows(count: int, columns: int = 2, start: int = 0) -> str:
    return "".join(
        "<tr>" + "".join(f"<td>r{i}c{c}</td>" for c in range(columns)) + "</tr>"
        for i in range(start, start + count)
    )


def table(count: int, *, caption: str = "", header: bool = True, columns: int = 2) -> str:
    thead = (
        "<thead><tr>" + "".join(f"<th>H{c}</th>" for c in range(columns)) + "</tr></thead>"
        if header
        else ""
    )
    cap = f"<caption>{caption}</caption>" if caption else ""
    return f'<table class="pf-data-table">{cap}{thead}<tbody>{rows(count, columns)}</tbody></table>'


def document(body: str) -> str:
    return HEAD + body + TAIL


def spanned(count: int, spans: dict[tuple[int, int], int], columns: int = 2) -> str:
    """A table whose cell at (row, column) spans ``spans[(row, column)]`` rows."""
    covered: dict[int, int] = {}  # column -> the first row its last span leaves free
    body = []
    for r in range(count):
        cells = []
        for c in range(columns):
            if covered.get(c, 0) > r:
                continue
            span = spans.get((r, c), 1)
            covered[c] = r + span
            rowspan = f' rowspan="{span}"' if span > 1 else ""
            cells.append(f"<td{rowspan}>r{r}c{c}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    head = "<thead><tr>" + "".join(f"<th>H{c}</th>" for c in range(columns)) + "</tr></thead>"
    return f'<table class="pf-data-table">{head}<tbody>{"".join(body)}</tbody></table>'


def chained(count: int, columns: int = 3) -> dict[tuple[int, int], int]:
    """Spans of four rows, alternating between two columns and overlapping by two."""
    return {(r, (r // 2) % 2): 4 for r in range(0, count, 2)}


# ------------------------------------------------------------------ indexing


def test_top_level_elements_and_their_tables_are_indexed():
    html = document(
        "<h1 id='a'>A</h1><p>text</p>"
        f'<section class="data-table table-plate"><h1>Sheet</h1>{table(100, caption="S")}</section>'
        "<h1>B</h1>"
    )
    layout = htmlplan.scan(html)

    assert [html[e.start : e.start + 4] for e in layout.elements] == [
        "<h1 ",
        "<p>t",
        "<sec",
        "<h1>",
    ]
    section = layout.elements[2]
    assert section.break_before and section.break_after
    assert section.h1_before == 1
    (found,) = section.tables
    assert found.header_rows == 1 and len(found.rows) == 100 and found.columns == 2
    assert found.caption is not None and found.tbody is not None
    assert found.wrapper_names == ["section"]
    # One h1 at body level before the table, one inside its section.
    assert found.h1_before == [1, 1]
    assert section.cells == 200


def test_rows_joined_by_a_row_span_cannot_start_a_part():
    body = "<tr><td rowspan='3'>x</td><td>1</td></tr><tr><td>2</td></tr><tr><td>3</td></tr><tr><td>a</td><td>4</td></tr>"
    layout = htmlplan.scan(document(f"<table><tbody>{body}</tbody></table>"))
    (found,) = layout.elements[0].tables
    assert found.restart_allowed() == [True, False, False, True]


@pytest.mark.parametrize(
    "markup",
    [
        "<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>",
        "<table><tfoot><tr><td>f</td></tr></tfoot><tbody><tr><td>x</td></tr></tbody></table>",
        "<table style='width:10px'><tr><td>x</td></tr></table>",
        "<table><colgroup><col></colgroup><tr><td>x</td></tr></table>",
    ],
)
def test_tables_the_parts_cannot_reproduce_are_never_cut(markup):
    layout = htmlplan.scan(document(markup))
    assert layout.elements[0].tables == []
    assert layout.elements[0].cells > 0


def test_markup_that_cannot_be_followed_is_reported():
    with pytest.raises(htmlplan.Unsplittable):
        htmlplan.scan(document("<div><p>unclosed</div>"))


# ------------------------------------------------------------------ planning


def test_a_table_over_the_budget_is_cut_between_rows():
    layout = htmlplan.scan(document("<p>intro</p>" + table(1000)))
    plan = chunked.plan_part(layout, Start(0), budget=400)

    assert plan.cut is not None
    assert plan.cut.element == 1
    assert plan.cut.row_end == 200  # 400 cells, two per row; the paragraph holds none


def test_small_content_ends_a_part_only_at_a_forced_page_break():
    plates = "".join(
        f'<section class="data-table table-plate">{table(40)}</section>' for _ in range(6)
    )
    layout = htmlplan.scan(document("<p>a</p>" * 3 + plates))
    plan = chunked.plan_part(layout, Start(0), budget=100)

    # The paragraphs cannot end a part; the second plate is the first break
    # after the budget runs out.
    assert plan.cut is None
    assert plan.end_element == 5


def test_a_long_row_span_makes_its_table_one_unsplittable_run():
    # One cell spans all 300 rows: 301 cells that no part can divide.
    layout = htmlplan.scan(document(spanned(300, {(0, 0): 300})))
    element = layout.elements[0]
    assert element.tables and chunked._cuttable(element) is None

    with pytest.raises(ResourceLimitError, match="cells spanning rows"):
        chunked.check_unsplittable(layout, budget=50)
    chunked.check_unsplittable(layout, budget=100)
    assert chunked.plan_part(layout, Start(0), budget=100).cut is None


def test_chained_overlapping_spans_join_every_row_they_reach():
    # Rows 0-201 are joined link by link; the rows after them are ordinary.
    layout = htmlplan.scan(document(spanned(300, chained(200), columns=3)))
    tbl = layout.elements[0].tables[0]
    assert chunked._starts(tbl)[0] == 202
    # The first part holds the joined rows and the row after them.
    assert 240 < tbl.cells_between(0, 203) <= 400

    with pytest.raises(ResourceLimitError, match="cells spanning rows"):
        chunked.check_unsplittable(layout, budget=60)
    chunked.check_unsplittable(layout, budget=100)
    plan = chunked.plan_part(layout, Start(0), budget=100)
    assert plan.cut is not None and plan.cut.row_end > 202
    assert chunked.part_cells(layout, plan, {}) <= 400


def test_a_table_is_cut_at_the_first_row_its_spanning_cells_leave_free():
    layout = htmlplan.scan(document(spanned(300, {(0, 0): 100})))
    tbl = layout.elements[0].tables[0]
    chunked.check_unsplittable(layout, budget=100)

    plan = chunked.plan_part(layout, Start(0), budget=100)

    # The first 100 rows stay together; the cut leaves room for a page after them.
    assert plan.cut is not None and plan.cut.row_end == 100 + chunked.MIN_CUT_ROWS
    assert chunked.part_cells(layout, plan, {}) <= 4 * 100
    # A part may start inside the table only where no cell spans into the row.
    assert chunked._next_start(tbl, 0) == 100


def test_ordinary_merged_cells_are_planned_as_before():
    plain = htmlplan.scan(document(table(1000)))
    merged = htmlplan.scan(document(spanned(1000, {(r, 0): 2 for r in range(0, 1000, 10)})))
    for layout in (plain, merged):
        chunked.check_unsplittable(layout, budget=400)
        plan = chunked.plan_part(layout, Start(0), budget=400)
        assert plan.cut is not None and plan.cut.row_end == 200


def test_unsplittable_content_over_four_budgets_is_refused_before_printing():
    many = "".join(table(10) for _ in range(30))  # 30 small tables, none worth cutting
    layout = htmlplan.scan(document(many))
    with pytest.raises(ResourceLimitError, match="cannot be printed in parts"):
        chunked.check_unsplittable(layout, budget=100)
    chunked.check_unsplittable(layout, budget=1000)


# -------------------------------------------------------------------- markup


def test_a_continuation_restarts_after_a_forced_break_with_the_row_before():
    html = document(
        f'<section class="data-table table-plate"><h1>S</h1>{table(300, caption="Cap")}</section>'
    )
    layout = htmlplan.scan(html)
    tbl = layout.elements[0].tables[0]
    plan = PartPlan(Start(0, tbl, 101), 1)

    text, stand_ins = chunked.part_html(layout, plan, {id(tbl): [5]})

    assert stand_ins == frozenset()
    body = text[len(layout.head) :]
    # Wrappers reopen with a hidden h1 for :first-of-type; caption and the
    # visible heading stay with the first part.
    assert body.startswith('<section class="data-table table-plate"><h1 aria-hidden="true"')
    assert "Cap" not in body
    assert "<thead>" in body
    # The row before prints on a page the joiner drops, then a forced break.
    assert "<tr data-pf-repeated><td>r100c0" in body
    assert '<tr data-pf-restart style="break-before:page;"><td>r101c0' in body
    # Row 100 is the 101st body row: an odd count before it, so no spacer.
    assert "data-pf-spacer" not in body
    # The sizing row is hidden and carries no height.
    assert (
        '<tr data-pf-sizer aria-hidden="true" style="visibility:hidden"><td style="padding-top:0'
        in body
    )
    assert body.count("<tr") == 1 + 300 - 100 + 1


def test_the_stripe_parity_is_kept_with_a_hidden_row():
    layout = htmlplan.scan(document(table(300)))
    tbl = layout.elements[0].tables[0]
    text, _ = chunked.part_html(layout, PartPlan(Start(0, tbl, 102), 1), {})
    assert (
        '<tbody><tr data-pf-spacer aria-hidden="true" style="display:none"></tr><tr data-pf-repeated>'
        in text
    )


def test_a_cut_part_closes_the_table_and_its_wrappers():
    html = document(f'<div class="wide-table">{table(300)}</div><p>after</p>')
    layout = htmlplan.scan(html)
    tbl = layout.elements[0].tables[0]
    plan = PartPlan(Start(0), 0, Cut(0, tbl, 120))
    text, _ = chunked.part_html(layout, plan, {id(tbl): []})

    assert text.endswith("</tbody></table></div>" + TAIL)
    assert "r119c1" in text and "r120c0" not in text and "<p>after" not in text


def test_links_to_targets_in_other_parts_get_stand_ins():
    html = document(
        f'<p><a href="#later">jump</a> <a href="#nowhere">x</a></p>{table(300)}<h2 id="later">L</h2>'
    )
    layout = htmlplan.scan(html)
    tbl = layout.elements[1].tables[0]
    text, stand_ins = chunked.part_html(layout, PartPlan(Start(0), 1, Cut(1, tbl, 100)), {})

    # A missing target that exists elsewhere gets a stand-in; one that
    # exists nowhere does not, so the link drops as it would in one pass.
    assert stand_ins == frozenset({"later"})
    assert '<a id="later" aria-hidden="true"' in text


def test_the_running_header_pass_has_one_empty_page_per_joined_page():
    rules = chunked.page_rules(document(""))
    html = chunked.overlay_html(
        [(612.0, 792.0), (612.0, 792.0)], rules, HEAD, [("Sora", "Title Page 0123456789 of")]
    )
    assert html.count('<div class="pf-running-0"></div>') == 2
    assert "background: none" in html
    assert "Title Page 0123456789 of" in html


def _settings(**limits) -> Settings:
    return Settings(limits=replace(ResourceLimits(), **limits))


def test_a_document_within_one_pass_prints_as_before():
    assert parts_layout(document(table(100)), _settings(max_cells_per_pass=500)) is None


def test_a_document_over_one_pass_is_indexed_for_parts():
    layout = parts_layout(document(table(300)), _settings(max_cells_per_pass=500))
    assert layout is not None and chunked.table_cells(layout) == 600


def test_a_document_over_the_cell_limit_is_refused_before_printing():
    with pytest.raises(ResourceLimitError, match="more than the 500-cell limit"):
        parts_layout(document(table(300)), _settings(max_document_cells=500))


def test_a_server_sizes_each_pass_to_its_memory_limit():
    from platefold.server_models import ExportOptions, JobLimits

    small = ExportOptions().settings(JobLimits()).limits
    large = ExportOptions().settings(replace(JobLimits(), memory_bytes=4 * 1024**3)).limits

    # 1 GiB leaves room for about 11,000 cells per pass after the Python
    # process; the operator's table_cells bounds the document.
    assert 10_000 < small.max_cells_per_pass < 12_000
    assert large.max_cells_per_pass > 4 * small.max_cells_per_pass
    assert small.max_document_cells == small.max_workbook_cells == JobLimits().table_cells


# ------------------------------------------------------------------ printing


class _FakeBrowser:
    """Prints nothing; records the table cells each attempted pass lays out."""

    def __init__(self):
        self.passes: list[int] = []
        self.printer = chunked.Printer(
            print_part=self.print_part,
            printed=lambda row: None,
            load=lambda html: None,
            evaluate=lambda script, arg: [
                [[column, 10.0, 1.0] for column, _lines in row] for row in arg["rows"]
            ],
        )

    def print_part(self, html: str, path: str) -> None:
        self.passes.append(chunked.table_cells(htmlplan.scan(html)))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(html)


def _clean_pages(monkeypatch, every_allowed_row: bool) -> None:
    """Pretend every row a part may start from begins a page, or none does."""

    def restart_point(path, header_rows, allowed):
        rows = [index for index, ok in enumerate(allowed) if ok and index > 0]
        return (rows[-1], 1) if rows and every_allowed_row else None

    monkeypatch.setattr(pdfjoin, "restart_point", restart_point)
    monkeypatch.setattr(pdfjoin, "continuation_page", lambda path, header_rows, repeated: 1)


# Four cells of 150 rows each: every run between free rows fits in one pass
# of 240 cells, but no two do.
_BLOCKS = {(row, 0): 150 for row in range(0, 600, 150)}


def test_a_grown_cut_never_passes_the_unsplittable_limit(monkeypatch):
    _clean_pages(monkeypatch, every_allowed_row=False)
    layout = htmlplan.scan(document(spanned(600, _BLOCKS)))
    chunked.check_unsplittable(layout, budget=60)
    browser = _FakeBrowser()
    cache = chunked.PartCache()
    try:
        with pytest.raises(ResourceLimitError, match="leave no page to start a part on"):
            chunked.print_parts(layout, 60, browser.printer, cache)
    finally:
        cache.close()
    # The cut grew, but never past 240 cells, and stopped before a larger pass.
    assert len(browser.passes) >= 2
    assert max(browser.passes) <= 240


def test_parts_of_a_table_with_spanning_cells_stay_within_the_limit(monkeypatch):
    _clean_pages(monkeypatch, every_allowed_row=True)
    layout = htmlplan.scan(document(spanned(600, _BLOCKS)))
    browser = _FakeBrowser()
    cache = chunked.PartCache()
    try:
        parts = chunked.print_parts(layout, 60, browser.printer, cache)
    finally:
        cache.close()
    assert len(parts) == 4
    assert max(browser.passes) <= 240


# ----------------------------------------------------- cells spanning rows and columns


@pytest.mark.parametrize(
    ("attributes", "spans"),
    [
        (' rowspan="3"', (1, 3)),
        (" ROWSPAN=2 colspan='0'", (1, 2)),
        (' rowspan=" +4px"', (1, 4)),
        (' rowspan="&#51;"', (1, 3)),
        (' rowspan="0"', (1, htmlplan.MAX_ROWSPAN)),
        (' rowspan="-1"', (1, 1)),
        (' rowspan="2" rowspan="5"', (1, 2)),
        (' data-rowspan="5" title="rowspan=4"', (1, 1)),
        (" colspan=5000", (htmlplan.MAX_COLSPAN, 1)),
    ],
)
def test_cell_spans_are_read_as_the_browser_reads_them(attributes, spans):
    assert htmlplan.cell_spans(attributes) == spans


def _body_table(body: str, head: str = "") -> htmlplan.Table:
    layout = htmlplan.scan(document(f"<table>{head}<tbody>{body}</tbody></table>"))
    (found,) = layout.elements[0].tables
    return found


def test_each_row_knows_the_cells_spanning_into_it():
    found = _body_table(
        "<tr><td rowspan=3 colspan=2>a</td><td>b</td></tr>"
        "<tr><td>c</td></tr>"
        "<tr><td rowspan=2>d</td></tr>"
        "<tr><td>e</td><td>f</td></tr>"
    )
    covered = [[(cover.column, cover.colspan) for cover in row.covered] for row in found.rows]
    assert covered == [[], [(0, 2)], [(0, 2)], [(2, 1)]]
    assert found.spanning and not _body_table(rows(3)).spanning


def test_a_header_spanning_rows_leaves_the_table_splittable():
    head = "<thead><tr><th rowspan=2>A</th><th rowspan=2>B</th></tr><tr></tr></thead>"
    layout = htmlplan.scan(document(f"<table>{head}<tbody>{rows(100)}</tbody></table>"))
    (found,) = layout.elements[0].tables
    # The header row without cells has no tag in the PDF, so is not counted.
    assert found.header_rows == 1
    assert chunked._cuttable(layout.elements[0]) is found


def test_a_copy_of_a_row_under_a_span_keeps_its_columns():
    html = document(
        "<table><tbody>"
        "<tr><td rowspan='2' class='k'>a</td><td colspan=2>b</td><td rowspan=2>z</td></tr>"
        "<tr><td>c</td><td>d</td></tr>"
        "</tbody></table>"
    )
    found = htmlplan.scan(html).elements[0].tables[0]

    # The spanning cells lose their rowspan; the cells under them get empty
    # stand-ins with their attributes, so each cell keeps its column.
    assert chunked.standalone_row(found, html, 0) == (
        "<tr><td class='k'>a</td><td colspan=2>b</td><td>z</td></tr>"
    )
    assert chunked.standalone_row(found, html, 1) == (
        "<tr><td class='k'></td><td>c</td><td>d</td><td></td></tr>"
    )


def test_a_row_whose_cells_overlap_a_span_is_not_copied():
    # Row 1's first cell reaches into the column the span from above holds,
    # which pushes its second cell past it; a copy could not do that.
    html = document(
        "<table><tbody>"
        "<tr><td>a</td><td>b</td><td rowspan=2 colspan=2>x</td></tr>"
        "<tr><td colspan=3>c</td><td>d</td></tr>"
        "</tbody></table>"
    )
    found = htmlplan.scan(html).elements[0].tables[0]
    assert chunked.standalone_row(found, html, 1) is None


# Every even row's first cell spans the odd row after it.
_PAIRS = {(row, 0): 2 for row in range(0, 300, 2)}


def test_a_continuation_prints_again_from_where_the_cells_above_begin():
    html = document(spanned(300, _PAIRS))
    layout = htmlplan.scan(html)
    tbl = layout.elements[0].tables[0]
    assert chunked.repeated_from(tbl, 102) == 100
    plan = PartPlan(Start(0, tbl, 102), 1)

    text, _ = chunked.part_html(layout, plan, {id(tbl): []})

    # Rows 100 and 101 print as in the table, the span included, so row 102
    # meets the same cells as in one pass.
    assert (
        '<tbody><tr data-pf-repeated><td rowspan="2">r100c0</td><td>r100c1</td></tr>'
        "<tr data-pf-repeated><td>r101c1</td></tr>"
        '<tr data-pf-restart style="break-before:page;"><td rowspan="2">r102c0</td>' in text
    )
    assert chunked.part_cells(layout, plan, {id(tbl): []}) == chunked.table_cells(
        htmlplan.scan(text)
    )


def test_a_long_run_of_spans_is_not_printed_again():
    # Chained spans join rows 0-101: row 101 prints alone, a stand-in for the
    # cell spanning into it.
    html = document(spanned(300, chained(100), columns=3))
    layout = htmlplan.scan(html)
    tbl = layout.elements[0].tables[0]
    assert chunked.repeated_from(tbl, 102) == 101
    text, _ = chunked.part_html(layout, PartPlan(Start(0, tbl, 102), 1), {id(tbl): []})
    assert "<tr data-pf-repeated><td>r101c0</td><td></td><td>r101c2</td></tr>" in text


def test_hidden_rows_of_a_spanning_table_keep_the_table_order():
    html = document(spanned(300, _PAIRS))
    layout = htmlplan.scan(html)
    tbl = layout.elements[0].tables[0]
    plan = PartPlan(Start(0, tbl, 102), 0, Cut(0, tbl, 200))
    sizing = {id(tbl): [5, 7, 150, 250]}

    text, _ = chunked.part_html(layout, plan, sizing)

    body = text[text.index("<tbody") : text.rindex("</tbody>") + 8]
    groups = re.findall(r"<tbody[^>]*>(.*?)</tbody>", body)
    assert [re.findall(r"r(\d+)c1", group)[:1] for group in groups] == [["0"], ["100"], ["250"]]
    before, printed, after = groups
    # Row 0 first, keeping its top border; rows 5 and 7 after it; row 150,
    # printed anyway, not copied.
    assert before.count("<tr data-pf-sizer") == 3
    assert 'style="padding-top:0;padding-bottom:0;border-bottom-width:0;"' in before
    assert "r150c1" in printed and "r150c1" not in before + after
    # Copies stand alone: no spans, a stand-in under row 4's span.
    assert "rowspan" not in before + after
    assert 'style="visibility:hidden"><td style="padding-top:0;' in before
    assert chunked.part_cells(layout, plan, sizing) == chunked.table_cells(htmlplan.scan(text))


def test_an_ordinary_table_keeps_its_hidden_rows_after_its_own():
    layout = htmlplan.scan(document(table(300)))
    tbl = layout.elements[0].tables[0]
    text, _ = chunked.part_html(layout, PartPlan(Start(0, tbl, 101), 1), {id(tbl): [5]})
    assert text.count("<tbody") == 1 and "data-pf-sizer aria-hidden" in text


def test_cells_spanning_columns_are_keyed_by_the_columns_they_span():
    html = document(
        "<table><tbody>"
        "<tr><td rowspan=2>a</td><td colspan=2>plain text</td></tr>"
        "<tr><td colspan=2 style='text-align:right'>b<br>c</td></tr>"
        "<tr><td colspan=2><b>bold</b></td><td>d</td></tr>"
        "<tr><td>e</td><td class='x' colspan=2>f</td></tr>"
        "</tbody></table>"
    )
    found = htmlplan.scan(html).elements[0].tables[0]
    assert chunked.cell_lines(found, html) == [
        [(0, ["a"], False), ("1+2", ["plain text"], True)],
        [("1+2", ["b", "c"], True)],
        [("0+2", ["bold"], False), (2, ["d"], False)],
        [(0, ["e"], False), ("1+2", ["f"], False)],
    ]


def test_wide_cells_are_carried_unless_earlier_ones_cover_them():
    widths = [[("0+2", 100.0, 10.0)], [("0+2", 90.0, 10.0)], [("0+2", 120.0, 9.0)]]
    widths += [[("0+2", 110.0, 12.0), (1, 500.0, 50.0)], [("1+3", 5.0, 5.0)]]
    exact = [[True]] * 3 + [[True, True], [True]]
    # Row 1 is no wider than row 0 by either measure; the rest widen a column.
    assert chunked.wide_records(widths, exact) == {0, 2, 3, 4}
    # Measured without its markup, row 1 might print wider than row 0.
    assert chunked.wide_records(widths, [[True], [False], *exact[2:]]) == {0, 1, 2, 3, 4}
    growing = [[("0+2", float(width), 1.0)] for width in range(100)]
    assert len(chunked.wide_records(growing, [[True]] * 100)) == chunked.MAX_WIDE_RECORDS


def test_a_part_holding_the_rest_of_a_table_keeps_what_follows_it():
    html = document(
        f"{table(300)}<p>after</p><p>more</p>"
        f'<section class="data-table table-plate">{table(40)}</section>'
    )
    layout = htmlplan.scan(html)
    tbl = layout.elements[0].tables[0]
    plan = PartPlan(Start(0), 0, Cut(0, tbl, 250))

    grown = chunked._grown(layout, plan, {id(tbl): []}, 10**6)

    # The paragraphs print after the table in the same part, as in one pass;
    # the part ends at the plate's forced page break.
    assert grown.cut is None and grown.end_element == 3


def test_rows_without_cells_are_left_out_of_the_restart_search(monkeypatch):
    # A cell spanning both columns and three rows every ten rows leaves two
    # rows with no cells, which the tagged PDF does not hold.
    blocks = "".join(
        "<tr><td rowspan=3 colspan=2>block</td></tr><tr></tr><tr></tr>" + rows(7, start=row)
        for row in range(0, 600, 10)
    )
    layout = htmlplan.scan(document(f"<table><tbody>{blocks}</tbody></table>"))
    tbl = layout.elements[0].tables[0]
    searched: list[int] = []
    continued: list[tuple[int, int]] = []

    def restart_point(path, header_rows, allowed):
        searched.append(len(allowed))
        index = max(i for i, ok in enumerate(allowed) if ok and i > 0)
        return index, 1

    def continuation_page(path, header_rows, repeated):
        continued.append((header_rows, repeated))
        return 1

    plans: list[PartPlan] = []
    original = chunked.part_html

    def recording(layout, plan, sizing):
        plans.append(plan)
        return original(layout, plan, sizing)

    monkeypatch.setattr(pdfjoin, "restart_point", restart_point)
    monkeypatch.setattr(pdfjoin, "continuation_page", continuation_page)
    monkeypatch.setattr(chunked, "part_html", recording)
    browser = _FakeBrowser()
    cache = chunked.PartCache()
    try:
        chunked.print_parts(layout, 100, browser.printer, cache)
    finally:
        cache.close()

    # Each search covers exactly the printed rows that have cells.
    cut = [plan for plan in plans if plan.cut is not None]
    assert len(cut) > 3
    for count, plan in zip(searched, cut, strict=True):
        assert plan.cut is not None
        first = chunked.repeated_from(tbl, plan.start.row) if plan.start.table else 0
        assert count == sum(1 for row in tbl.rows[first : plan.cut.row_end] if row.cells)
    # Every part starts at a row with cells, and knows how many printed rows before it have them.
    starts = [plan.start.row for plan in plans if plan.start.table is not None]
    allowed = tbl.restart_allowed()
    assert all(tbl.rows[row].cells and allowed[row] for row in starts)
    for (_header, repeated), row in zip(continued, starts, strict=True):
        before = tbl.rows[chunked.repeated_from(tbl, row) : row]
        assert repeated == sum(1 for printed in before if printed.cells)
