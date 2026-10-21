"""Print a document too large for one browser pass in several passes.

Chromium lays out a whole document before it prints any page, so its memory
grows with the document: about 30 KB per table cell. A document with more
table cells than one pass may hold is printed in parts instead, each a
complete document of its own, and ``pdfjoin`` joins them.

Every part prints exactly the pages one pass would have printed:

* A part ends where the stylesheet forces a page break, or inside a table.
  After a table cut, the part's last page is thrown away and the next part
  starts again from the row that began that page. The next part prints the
  preceding rows needed to preserve row-spanning cells, then forces a page
  break, so the kept pages are a real continuation that Chromium lays out as
  one pass would: the header row repeats and shared borders sit where they
  did. The pages holding the repeated rows are thrown away as well. Hidden
  elements that take no space keep the stripe parity and the first-heading
  margin rule matching.
* Every part of a split table carries, as collapsed rows that take no space
  and are not drawn or tagged, the rows holding each column's widest cells.
  Chromium sizes table columns from the widest content in each column, so
  every part gets the columns the whole table would have.
* Page numbers and running headers come from one extra pass over empty pages
  of the same sizes, drawn over the joined pages, because each pass numbers
  its pages from one.
"""

from __future__ import annotations

import bisect
import hashlib
import html as html_lib
import itertools
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import htmlplan, pdfjoin
from .htmlplan import Element, Layout, Table
from .limits import ResourceLimitError

# The fewest rows worth cutting a table after; fewer rarely reach a second page.
MIN_CUT_ROWS = 32
# The most rows a continuation prints before its first; see repeated_from.
MAX_REPEATED_ROWS = MIN_CUT_ROWS
# Widest cells per column, by each measure, carried as sizing rows.
SIZING_CELLS_PER_COLUMN = 4
# Content that cannot be split (no forced page break, no table to cut, rows
# joined by cells spanning rows) prints in one pass as before, up to this
# many times the per-pass budget. No pass lays out more cells than that.
UNSPLITTABLE_FACTOR = 4

# A cell: its name, attributes and content.
_CELL = re.compile(r"""<(t[dh])\b((?:[^>"']|"[^"]*"|'[^']*')*)>(.*?)</\1\s*>""", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_PAGE_RULE = re.compile(
    r"@page(?:\s+([\w-]+))?\s*\{\s*size:\s*([\d.]+)mm\s+([\d.]+)mm;\s*margin:\s*"
    r"([\d.]+)mm\s+([\d.]+)mm\s+([\d.]+)mm\s+([\d.]+)mm;"
)
_PT_PER_MM = 72 / 25.4


@dataclass(frozen=True)
class PageRule:
    name: str | None
    width_mm: float
    height_mm: float
    margins: tuple[float, float, float, float]  # top, right, bottom, left

    @property
    def css(self) -> str:
        margins = " ".join("%gmm" % margin for margin in self.margins)
        return "size: %gmm %gmm; margin: %s;" % (self.width_mm, self.height_mm, margins)


def page_rules(html: str) -> list[PageRule]:
    """The document's @page rules, the default page first."""
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    rules = []
    for match in _PAGE_RULE.finditer(style.group(1) if style else ""):
        name, width, height, top, right, bottom, left = match.groups()
        margins = (float(top), float(right), float(bottom), float(left))
        rules.append(PageRule(name, float(width), float(height), margins))
    rules.sort(key=lambda rule: rule.name is not None)
    if not rules or rules[0].name is not None:
        raise htmlplan.Unsplittable("the stylesheet has no default page size")
    return rules


def table_cells(layout: Layout) -> int:
    return sum(element.cells for element in layout.elements)


def check_unsplittable(layout: Layout, budget: int) -> None:
    """Refuse, before printing, content too large for a pass that cannot be split.

    Between two forced page breaks, content is split only inside a table a
    part may be cut in, and only at a row a part may start from (see
    ``_starts``). Whatever lies between two such places prints in one pass.
    """
    limit = budget * UNSPLITTABLE_FACTOR
    run = 0
    for index, element in enumerate(layout.elements):
        if index and _boundary(layout.elements, index):
            run = 0
        table = _cuttable(element)
        if table is None:
            run += element.cells
            _check_run(run, limit, spans=_joined(element))
            continue
        # A part cut inside the table prints the row the next part starts
        # from, to find its page, and a part starting inside the table prints
        # rows before its first too (see repeated_from).
        starts = _starts(table)
        run += element.cells - table.cells + table.cells_between(0, starts[0] + 1)
        _check_run(run, limit, spans=starts[0] > 1)
        for begin, end in itertools.pairwise(starts):
            printed = repeated_from(table, begin)
            _check_run(table.cells_between(printed, end + 1), limit, spans=True)
        last = len(table.rows) - MIN_CUT_ROWS - 1  # the last row a part may start from
        run = table.cells_between(repeated_from(table, starts[-1]), len(table.rows))
        _check_run(run, limit, spans=starts[-1] < last)


def _check_run(cells: int, limit: int, *, spans: bool = False) -> None:
    if cells <= limit:
        return
    if spans:
        raise ResourceLimitError(
            f"The document has {cells:,} table cells that cannot be printed in parts, more than "
            f"the {limit:,} one pass may hold, because cells spanning rows join a table's rows; "
            f"split the table or its merged cells"
        )
    raise ResourceLimitError(
        f"The document has {cells:,} table cells that cannot be printed in parts, more "
        f"than the {limit:,} one pass may hold; split the document or its tables"
    )


def _cuttable(element: Element) -> Table | None:
    """The table a part may be cut inside: the element's only simple table.

    It must be long enough to be worth cutting and have a row a part could
    start from; a table whose rows a spanning cell joins throughout is not.
    """
    if len(element.tables) != 1 or len(element.tables[0].rows) < 2 * MIN_CUT_ROWS:
        return None
    table = element.tables[0]
    return table if _starts(table) else None


def _joined(element: Element) -> bool:
    """Whether the element's table would be cut but for cells spanning its rows."""
    tables = element.tables
    return len(tables) == 1 and len(tables[0].rows) >= 2 * MIN_CUT_ROWS and not _starts(tables[0])


def _starts(table: Table) -> list[int]:
    """The rows a part may start from inside ``table``, in order.

    No cell spans into such a row from above, and it is not the first row,
    nor so near the end that fewer than MIN_CUT_ROWS rows follow a cut.
    """
    restarts = table.restarts
    low = bisect.bisect_right(restarts, 0)
    high = bisect.bisect_left(restarts, len(table.rows) - MIN_CUT_ROWS)
    return restarts[low:high]


def _next_start(table: Table, after: int) -> int | None:
    """The first row after ``after`` that a part may start from."""
    starts = _starts(table)
    position = bisect.bisect_right(starts, after)
    return starts[position] if position < len(starts) else None


# ------------------------------------------------------------------ planning


@dataclass
class Start:
    """Where a part begins: an element, or a row inside a table of it."""

    element: int
    table: Table | None = None
    row: int = 0


@dataclass
class Cut:
    element: int
    table: Table
    row_end: int  # rows[start_row:row_end] are printed


@dataclass
class PartPlan:
    start: Start
    end_element: int  # elements[start.element : end_element] are printed (whole)
    cut: Cut | None = None


def plan_part(layout: Layout, start: Start, budget: int) -> PartPlan:
    """Choose where the part beginning at ``start`` ends."""
    limit = budget * UNSPLITTABLE_FACTOR
    elements = layout.elements
    cost = 0
    index = start.element
    while index < len(elements):
        element = elements[index]
        if (
            index > start.element
            and _boundary(elements, index)
            and (cost >= budget or cost + _ahead(elements, index) > limit)
        ):
            return PartPlan(start, index)
        # Only an element's single table is cut inside; the part after the cut
        # reopens that table's wrappers and nothing else.
        table = _cuttable(element)
        if table is not None:
            first = start.row if start.table is table else 0
            remaining = table.cells_between(first, len(table.rows))
            row_end = _plan_cut(table, first, budget - cost, limit - cost)
            if cost + remaining > budget and row_end is not None:
                return PartPlan(start, index, Cut(index, table, row_end))
            cost += remaining
        cost += element.cells - (table.cells if table is not None else 0)
        index += 1
    return PartPlan(start, len(elements))


def _plan_cut(table: Table, first: int, room: int, limit: int) -> int | None:
    """Where to cut a table printed from row ``first``, or None when it cannot be cut.

    The cut leaves room for about ``room`` cells, and always for a row a
    later part may start from; it never passes ``limit`` cells.
    """
    following = _next_start(table, first)
    if following is None:
        return None
    row_end = first + max(MIN_CUT_ROWS, room // max(1, table.columns))
    if row_end <= following:
        # Rows joined by a spanning cell: take them all and a page more.
        row_end = following + MIN_CUT_ROWS
    printed = repeated_from(table, first)
    row_end = min(row_end, len(table.rows) - MIN_CUT_ROWS, table.rows_within(printed, limit))
    if row_end <= max(following, first + MIN_CUT_ROWS // 2):
        return None
    return row_end


def _ahead(elements: list[Element], index: int) -> int:
    """Cells from ``index`` to the next place a part may end: a forced break or a table cut."""
    cells = 0
    for position in range(index, len(elements)):
        if position > index and _boundary(elements, position):
            break
        element = elements[position]
        table = _cuttable(element)
        if table is not None:
            start = _starts(table)[0]
            return cells + element.cells - table.cells + table.cells_between(0, start + 1)
        cells += element.cells
    return cells


def _boundary(elements: list[Element], index: int) -> bool:
    return elements[index].break_before or elements[index - 1].break_after


# ------------------------------------------------------------------- markup


_HIDDEN_H1 = '<h1 aria-hidden="true" style="display:none"></h1>'
_SPACER_ROW = '<tr data-pf-spacer aria-hidden="true" style="display:none"></tr>'
# A sizing row is a copy of a row with a column's widest cell. Its cells keep
# their horizontal padding, so the column sizes as in the whole table, but
# their content sits in a box of no height and the row is hidden: it takes no
# height, is not drawn and is not exposed to assistive technology.
_SIZING_ROW = '<tr data-pf-sizer aria-hidden="true" style="visibility:hidden"'
_SIZING_BODY = '<tbody data-pf-sizer aria-hidden="true">'
_SIZING_CELL = "padding-top:0;padding-bottom:0;border-top-width:0;border-bottom-width:0;"
_SIZING_FIRST = "padding-top:0;padding-bottom:0;border-bottom-width:0;"
_SIZING_BOX = '<div style="height:0;overflow:hidden">'
_CELL_OPEN = re.compile(r"""<(t[dh])\b((?:[^>"']|"[^"]*"|'[^']*')*)>""", re.I)
_CELL_CLOSE = re.compile(r"</(t[dh])\s*>", re.I)
_ROW_ATTRIBUTE = re.compile(r"""\s(?:id|href|name)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""")


def _open_table(table: Table, html: str, continued: bool) -> str:
    tag = html[table.start : table.open_end]
    if continued:
        tag = tag[:-1] + " data-pf-continued>"
    parts = [tag]
    if table.caption is not None and not continued:
        parts.append(html[table.caption[0] : table.caption[1]])
    if table.thead is not None:
        parts.append(html[table.thead[0] : table.thead[1]])
    return "".join(parts)


def _sizing_rows(table: Table, html: str, rows: list[int], cell_style: str = "") -> str:
    """Hidden copies of rows, without IDs or link targets."""
    cell_style = cell_style or _SIZING_CELL

    def cell(match: re.Match) -> str:
        name, attributes = match.group(1), match.group(2)
        style = re.search(r"""\bstyle\s*=\s*(["'])""", attributes)
        if style:
            at = style.end()
            attributes = attributes[:at] + cell_style + attributes[at:]
        else:
            attributes += f' style="{cell_style}"'
        return f"<{name}{attributes}>{_SIZING_BOX}"

    copies = []
    for index in rows:
        text = _ROW_ATTRIBUTE.sub("", standalone_row(table, html, index) or "")
        text = _CELL_OPEN.sub(cell, text)
        text = _CELL_CLOSE.sub(r"</div></\1>", text)
        copies.append(_SIZING_ROW + text[len("<tr") :])
    return "".join(copies)


_BREAK_BEFORE = "break-before:page;"
_TR_OPEN = re.compile(r"<tr\b([^>]*)>")


def standalone_row(table: Table, html: str, index: int) -> str | None:
    """Body row ``index`` as markup that lays out alone as it does in its table.

    A cell spanning rows loses its rowspan, and a cell spanning into the row
    from above is stood in for by an empty cell with its attributes and
    colspan. Every cell keeps its grid column, and its neighbours the borders
    they meet in the table. None when cells overlap, which a copy cannot
    reproduce.
    """
    row = table.rows[index]
    if not row.covered and not row.spans_down:
        return html[row.start : row.end]
    cells = list(_CELL.finditer(html, row.start, row.end))
    spans = [htmlplan.cell_spans(cell.group(2))[0] for cell in cells]
    entries = [
        (column, 1, colspan, _cell_markup(cell.group(1), cell.group(2), cell.group(3)))
        for column, colspan, cell in zip(
            htmlplan.place_cells(row.covered, spans), spans, cells, strict=True
        )
    ]
    for cover in row.covered:
        opening = _CELL_OPEN.match(html, cover.start, cover.open_end)
        assert opening is not None
        entries.append(
            (cover.column, 0, cover.colspan, _cell_markup(opening.group(1), opening.group(2), ""))
        )
    entries.sort(key=lambda entry: entry[:2])
    markup = []
    column = 0
    for at, real, colspan, text in entries:
        if at != column:
            if real:
                return None
            continue  # a cell from above that this row's own cells overlap
        markup.append(text)
        column += colspan
    opening = _TR_OPEN.match(html, row.start)
    assert opening is not None
    return opening.group(0) + "".join(markup) + "</tr>"


def _cell_markup(name: str, attributes: str, content: str) -> str:
    name = name.lower()
    return f"<{name}{htmlplan.without_attributes(attributes, 'rowspan')}>{content}</{name}>"


def _with_attributes(row_html: str, attribute: str, style: str = "") -> str:
    """A row's markup with an extra attribute and inline style declarations."""
    match = _TR_OPEN.match(row_html)
    if match is None:
        raise htmlplan.Unsplittable("a table row does not start with <tr>")
    attributes = match.group(1)
    if style:
        found = re.search(r"""\bstyle\s*=\s*(["'])""", attributes)
        if found:
            attributes = attributes[: found.end()] + style + attributes[found.end() :]
        else:
            attributes += f' style="{style}"'
    return f"<tr {attribute}{attributes}>" + row_html[match.end() :]


def repeated_from(table: Table, first: int) -> int:
    """The first row a part starting at row ``first`` of ``table`` prints.

    A continuation prints the row before its first one, on a page the joiner
    leaves out. Where cells above span into that row, it prints from the row
    they begin in, so the kept rows meet the same cells, in the same order,
    as in the whole table: the browser resolves the borders two cells share
    by that order, and draws their corners by it. Past MAX_REPEATED_ROWS the
    row before is printed alone, with stand-ins for the cells above.
    """
    if first == 0:
        return 0
    before = first - 1
    while before and not table.rows[before].cells:
        before -= 1  # a row without cells is not laid out; the row before it meets the first
    if not table.rows[before].covered:
        return before
    restarts = table.restarts
    free = restarts[bisect.bisect_right(restarts, before) - 1]
    return free if before - free < MAX_REPEATED_ROWS else before


def _rows(table: Table, html: str, first: int, last: int, sizing: list[int] | None) -> str:
    """Body rows first to last; a continuation starts with rows before ``first``."""
    rows = table.rows[first:last]
    pieces = []
    printed_from = repeated_from(table, first)
    for index in range(printed_from, first):
        # Printed on a page the joiner leaves out; the forced break after
        # them makes the kept pages a real continuation.
        before = table.rows[index]
        alone = index == printed_from and before.covered  # past MAX_REPEATED_ROWS
        copy = standalone_row(table, html, index) if alone else None
        pieces.append(_with_attributes(copy or html[before.start : before.end], "data-pf-repeated"))
    if first:
        opening = html[rows[0].start : rows[0].end] if rows else ""
        pieces.append(_with_attributes(opening, "data-pf-restart", _BREAK_BEFORE))
        rows = rows[1:]
    if rows:
        pieces.append(html[rows[0].start : rows[-1].end])
    if sizing and not table.spanning:
        pieces.append(_sizing_rows(table, html, sizing))
    # A hidden row keeps the stripe parity: nth-child counts it, layout ignores it.
    spacer = _SPACER_ROW if printed_from % 2 else ""
    body = "<tbody>" + spacer + "".join(pieces) + "</tbody>"
    if not (sizing and table.spanning):
        return body
    # Cells spanning columns share their width out in the order they come in
    # the table, so hidden copies of earlier rows come before the printed rows
    # and of later ones after, each in a row group of its own: its rows leave
    # the stripes alone, and cells spanning past the last printed row end
    # there, as in the whole table. Row 0 comes first with its top border, so
    # the header's lower border is the one the whole table draws.
    earlier, later = _hidden_rows(table, sizing, first, last)
    if earlier:
        head = _sizing_rows(table, html, earlier[:1], _SIZING_FIRST)
        body = _SIZING_BODY + head + _sizing_rows(table, html, earlier[1:]) + "</tbody>" + body
    if later:
        body += _SIZING_BODY + _sizing_rows(table, html, later) + "</tbody>"
    return body


def _hidden_rows(
    table: Table, sizing: list[int], first: int, last: int
) -> tuple[list[int], list[int]]:
    """The rows a part of a table with spanning cells copies before and after its own.

    A continuation also copies row 0 first, and the rows where cells
    spanning columns and rows into its first printed row begin.
    """
    later = [index for index in sizing if index >= last]
    printed_from = repeated_from(table, first)
    if printed_from == 0:
        return [], later
    wanted = {0, *sizing, *_origins(table, [printed_from])}
    return sorted(index for index in wanted if index < printed_from), later


def _origins(table: Table, rows: list[int]) -> set[int]:
    """The rows where cells spanning columns and rows into ``rows`` begin, and theirs.

    A copy stands in for such a cell with an empty cell spanning the same
    columns. Where no cell of its own begins in any of them, that stand-in
    could widen them, unless the cell it stands in for comes before it.
    """
    found: set[int] = set()
    pending = list(rows)
    while pending:
        for cover in table.rows[pending.pop()].covered:
            origin = table.row_at(cover.start)
            if cover.colspan > 1 and origin not in found:
                found.add(origin)
                pending.append(origin)
    return found


def _close_wrappers(table: Table) -> str:
    return "".join(f"</{name}>" for name in reversed(table.wrapper_names))


_HREF = re.compile(r"""\bhref\s*=\s*["']#([^"']+)["']""")
_ID = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""")
# A link keeps its annotation only when its target is in the same document.
# A target printed by another part gets an empty stand-in here, whose named
# destination the joiner replaces with the real one.
_STAND_IN = (
    '<a id="%s" aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden"></a>'
)


def part_html(
    layout: Layout, plan: PartPlan, sizing: dict[int, list[int]]
) -> tuple[str, frozenset[str]]:
    """One part as a complete document, and the link targets it only stands in for."""
    body = _part_body(layout, plan, sizing)
    part = body[len(layout.head) : len(body) - len(layout.tail)]
    wanted = {html_lib.unescape(m) for m in _HREF.findall(part)}
    missing = wanted - {html_lib.unescape(m) for m in _ID.findall(part)}
    missing &= layout_ids(layout)
    if not missing:
        return body, frozenset()
    stand_ins = "".join(_STAND_IN % html_lib.escape(name, quote=True) for name in sorted(missing))
    return body[: len(body) - len(layout.tail)] + stand_ins + layout.tail, frozenset(missing)


def layout_ids(layout: Layout) -> frozenset[str]:
    cached = getattr(layout, "_ids", None)
    if cached is None:
        body = layout.html[layout.body_start : layout.body_end]
        cached = frozenset(html_lib.unescape(m) for m in _ID.findall(body))
        layout._ids = cached  # type: ignore[attr-defined]
    return cached


def _part_body(layout: Layout, plan: PartPlan, sizing: dict[int, list[int]]) -> str:
    html = layout.html
    elements = layout.elements
    out = [layout.head]
    start = plan.start
    index = start.element
    element = elements[index]
    cut = plan.cut
    if start.table is not None:
        table = start.table
        if table.h1_before[0]:
            out.append(_HIDDEN_H1)
        for level, tag in enumerate(table.wrappers):
            out.append(tag)
            if table.h1_before[level + 1]:
                out.append(_HIDDEN_H1)
        out.append(_open_table(table, html, continued=True))
        last = cut.row_end if cut is not None and cut.table is table else len(table.rows)
        out.append(_rows(table, html, start.row, last, sizing.get(id(table))) + "</table>")
        if cut is not None and cut.table is table:
            out.append(_close_wrappers(table) + layout.tail)
            return "".join(out)
        out.append(html[table.end : element.end])
        index += 1
    elif index > 0 and element.h1_before:
        out.append(_HIDDEN_H1)
    stop = cut.element if cut is not None else plan.end_element
    if index < stop:
        out.append(html[elements[index].start : elements[stop - 1].end])
        index = stop
    if cut is not None:
        element = elements[cut.element]
        table = cut.table
        begin = element.start
        out.append(html[begin : table.start])
        out.append(_open_table(table, html, continued=False))
        out.append(_rows(table, html, 0, cut.row_end, sizing.get(id(table))) + "</table>")
        out.append(_close_wrappers(table))
    body = "".join(out)
    # The last element's span runs to </body>; keep the tail exactly once.
    if not body.endswith(layout.tail):
        body += layout.tail
    return body


# ------------------------------------------------------------------- sizing


def cell_lines(table: Table, html: str) -> list[list[tuple[int | str, list[str], bool]]]:
    """Each body row's cells as (key, text lines, measured exactly).

    A cell one column wide is keyed by its grid column. A wider cell is keyed
    ``"column+colspan"``: the browser shares its width out among the columns
    it spans, after the narrow cells, so the widest cells over each run of
    columns are carried too. A wider cell's text is measured exactly when it
    is plain text in the table's own cell font (see ``_plain``).
    """
    rows = []
    for row in table.rows:
        matches = list(_CELL.finditer(html, row.start, row.end))
        spans = [htmlplan.cell_spans(match.group(2))[0] for match in matches]
        plain_row = None
        cells: list[tuple[int | str, list[str], bool]] = []
        for column, width, match in zip(
            htmlplan.place_cells(row.covered, spans), spans, matches, strict=True
        ):
            text = html_lib.unescape(_TAGS.sub("", re.sub(r"<br\s*/?>", "\n", match.group(3))))
            if width == 1:
                cells.append((column, text.split("\n"), False))
                continue
            if plain_row is None:
                opening = _TR_OPEN.match(html, row.start)
                plain_row = opening is not None and _plain(opening.group(1))
            exact = plain_row and match.group(1) == "td" and _plain(match.group(2))
            exact = exact and _TAGS.search(re.sub(r"<br\s*/?>", "", match.group(3))) is None
            cells.append((f"{column}+{width}", text.split("\n"), exact))
        rows.append(cells)
    return rows


_FONT_NEUTRAL_STYLE = re.compile(r"\s*(?:(?:text-align|vertical-align)\s*:\s*[\w-]+\s*(?:;\s*|$))*")


def _plain(attributes: str) -> bool:
    """Whether an element's attributes leave its font as the stylesheet sets it."""
    for match in htmlplan.ATTRIBUTE.finditer(attributes):
        name = match.group(1).lower()
        value = next((group for group in match.groups()[1:] if group is not None), "")
        if name == "class" or (name == "style" and not _FONT_NEUTRAL_STYLE.fullmatch(value)):
            return False
    return True


def widest_rows(widths: Sequence[Sequence[tuple[int | str, float]]], per_column: int) -> set[int]:
    """Rows holding each column's (or each run of columns') ``per_column`` widest cells."""
    best: dict[int | str, list[tuple[float, int]]] = {}
    for index, cells in enumerate(widths):
        for column, width in cells:
            best.setdefault(column, []).append((width, index))
    chosen: set[int] = set()
    for entries in best.values():
        entries.sort(reverse=True)
        chosen.update(index for _width, index in entries[:per_column])
    return chosen


def _measuring_html(layout: Layout, table: Table, rows: list[int]) -> str:
    """The table's header and a few rows, laid out to read its cells' font.

    With cells spanning, an unstyled cell comes first, so the font read is
    the one plain cells print in.
    """
    html = layout.html
    body = (
        "".join(table.wrappers)
        + _open_table(table, html, continued=False)
        + "<tbody>"
        + ("<tr><td>x</td></tr>" if table.spanning else "")
        + "".join(html[table.rows[i].start : table.rows[i].end] for i in rows)
        + "</tbody></table>"
        + _close_wrappers(table)
    )
    return layout.head + body + layout.tail


# Each cell's widest line (its max-content width) and widest character (its
# min-content width, since cells wrap anywhere), measured with the browser's
# own text shaping in the font body cells use.
MEASURE_TEXT = """({rows}) => {
    const cell = document.querySelector('tbody td');
    const context = document.createElement('canvas').getContext('2d');
    context.font = getComputedStyle(cell || document.body).font;
    const segmenter = new Intl.Segmenter(undefined, {granularity: 'grapheme'});
    const width = text => context.measureText(text).width;
    return rows.map(cells => cells.map(([column, lines]) => [
        column,
        Math.max(0, ...lines.map(width)),
        Math.max(0, ...lines.flatMap(line => [...segmenter.segment(line)].map(s => width(s.segment)))),
    ]));
}"""

# Cells measured per batch sent to the browser.
_TEXT_BATCH = 2_000


def sizing_rows(layout: Layout, table: Table, printer: Printer) -> list[int]:
    """The rows whose cells give each column its width in the whole table.

    Chromium sizes automatic table columns from each column's widest content
    and widest unbreakable piece, then shares out what cells spanning
    columns need beyond that. Every cell's text is measured in the table's
    own font; cells with markup are measured by their text, so the rows
    widest by character count are included too. A row with or under a cell
    spanning rows is copied so that it stands alone (``standalone_row``);
    the rare row whose cells overlap cannot be, and is left out.
    """
    html = layout.html
    cells = cell_lines(table, html)
    lines = [[(key, text) for key, text, _exact in row] for row in cells]
    usable = [
        index
        for index, row in enumerate(table.rows)
        if not (row.covered or row.spans_down) or standalone_row(table, html, index) is not None
    ]
    by_characters = widest_rows(
        [[(c, max(map(len, text), default=0)) for c, text in lines[i]] for i in usable],
        SIZING_CELLS_PER_COLUMN,
    )
    chosen = {usable[i] for i in by_characters}
    printer.load(_measuring_html(layout, table, sorted(chosen)))
    measured: list[list] = []  # (key, widest line, widest character) per cell
    batch: list = []
    for index in usable:
        batch.append(lines[index])
        if sum(len(row) for row in batch) >= _TEXT_BATCH:
            measured += printer.evaluate(MEASURE_TEXT, {"rows": batch})
            batch = []
    if batch:
        measured += printer.evaluate(MEASURE_TEXT, {"rows": batch})
    for position in (1, 2):
        widths = [[(cell[0], cell[position]) for cell in row] for row in measured]
        chosen |= {usable[i] for i in widest_rows(widths, SIZING_CELLS_PER_COLUMN)}
    exact = [[flag for _key, _text, flag in cells[index]] for index in usable]
    chosen |= {usable[i] for i in wide_records(measured, exact)}
    usable_rows = set(usable)
    chosen |= {index for index in _origins(table, sorted(chosen)) if index in usable_rows}
    return sorted(chosen)


# Cells spanning columns carried per run of columns; see wide_records.
MAX_WIDE_RECORDS = 16
# How far text measured without its markup may be from the width it prints at.
_MARKUP_FACTOR, _MARKUP_MARGIN = 1.5, 2.0


def wide_records(measured: list[list], exact: list[list[bool]]) -> set[int]:
    """Rows with a cell spanning columns that no earlier cell over the same columns covers.

    The browser widens columns for such cells one at a time, in table order,
    and rounds each share, so the widths depend on every cell that widened
    them, not just the widest. A cell no wider, in both its narrowest and
    widest measure, than earlier cells over the same columns changes nothing
    and is left out. Text measured without its markup counts as possibly
    narrower when earlier and wider when later. The rest are carried, up to
    MAX_WIDE_RECORDS per run of columns.
    """
    best: dict[str, list[float]] = {}  # widest, narrowest, cells carried
    chosen: set[int] = set()
    for index, (row, flags) in enumerate(zip(measured, exact, strict=True)):
        for (key, widest, narrowest), sure in zip(row, flags, strict=True):
            if not isinstance(key, str):
                continue
            seen = best.setdefault(key, [-1.0, -1.0, 0])
            covered = seen[0] >= _bound(widest, sure, 1) and seen[1] >= _bound(narrowest, sure, 1)
            if not covered and seen[2] < MAX_WIDE_RECORDS:
                chosen.add(index)
                seen[2] += 1
            seen[0] = max(seen[0], _bound(widest, sure, -1))
            seen[1] = max(seen[1], _bound(narrowest, sure, -1))
    return chosen


def _bound(width: float, exact: bool, side: int) -> float:
    """The most (``side`` 1) or least (-1) a measured width may print at."""
    if exact:
        return width
    if side > 0:
        return width * _MARKUP_FACTOR + _MARKUP_MARGIN
    return width / _MARKUP_FACTOR - _MARKUP_MARGIN


# ------------------------------------------------------------------ printing


class PartCache:
    """Printed parts kept between a document's measuring and final passes.

    The two passes differ only in the contents page, so every part after the
    first is printed once.
    """

    def __init__(self):
        self.directory = tempfile.mkdtemp(prefix="platefold-parts-")
        self.files: dict[str, str] = {}

    def path_for(self, html: str) -> tuple[str, bool]:
        """Where a part's PDF goes, and whether it was already printed."""
        key = hashlib.sha256(html.encode("utf-8")).hexdigest()
        path = self.files.get(key)
        if path is not None and os.path.exists(path):
            return path, True
        path = os.path.join(self.directory, f"{len(self.files):05d}.pdf")
        self.files[key] = path
        return path, False

    def temporary(self, name: str) -> str:
        return os.path.join(self.directory, name)

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


@dataclass
class Printer:
    """What printing needs from the browser, supplied by the caller."""

    print_part: Callable[[str, str], None]  # (html, pdf path)
    printed: Callable[[int | None], None]  # a part is final; the argument is its cut row
    load: Callable[[str], None]  # load a document without printing it
    evaluate: Callable[[str, Any], Any]  # run a script in the loaded document


def print_parts(
    layout: Layout, budget: int, printer: Printer, cache: PartCache
) -> list[pdfjoin.Part]:
    """Print every part and return them ready to join."""
    sizing: dict[int, list[int]] = {}
    parts: list[pdfjoin.Part] = []
    start = Start(0)
    while True:
        plan = plan_part(layout, start, budget)
        part, following = _print_one(
            layout, plan, printer, cache, sizing, budget * UNSPLITTABLE_FACTOR
        )
        parts.append(part)
        if following is None:
            return parts
        start = following


def _print_one(layout, plan, printer, cache, sizing, limit):
    """Print the part ``plan`` describes, growing its cut until a page begins cleanly.

    No attempt lays out more than ``limit`` table cells: a part that would
    stops with an error before it is loaded.
    """
    start = plan.start
    while True:
        cut = plan.cut
        if cut is not None and id(cut.table) not in sizing:
            sizing[id(cut.table)] = sizing_rows(layout, cut.table, printer)
        cells = part_cells(layout, plan, sizing)
        if cells > limit:
            raise ResourceLimitError(
                f"A part of the document would lay out {cells:,} table cells, more than the "
                f"{limit:,} one pass may hold; split the document or its tables"
            )
        text, stand_ins = part_html(layout, plan, sizing)
        path, cached = cache.path_for(text)
        if not cached:
            printer.print_part(text, path)
        part = pdfjoin.Part(
            path,
            continues=start.table is not None,
            header_rows=start.table.header_rows if start.table is not None else 0,
            stand_ins=stand_ins,
        )
        if start.table is not None:
            rows = start.table.rows[repeated_from(start.table, start.row) : start.row]
            part.first = pdfjoin.continuation_page(
                path, start.table.header_rows, sum(1 for row in rows if row.cells)
            )
        if cut is None:
            printer.printed(None)
            following = Start(plan.end_element) if plan.end_element < len(layout.elements) else None
            return part, following
        # A continuation part prints rows before its first one as well.
        first = repeated_from(cut.table, start.row) if start.table is cut.table else 0
        # A row without cells is left out of the tagged PDF.
        tagged = [index for index in range(first, cut.row_end) if cut.table.rows[index].cells]
        allowed = cut.table.restart_allowed()
        restart = pdfjoin.restart_point(
            path, cut.table.header_rows, [allowed[index] for index in tagged]
        )
        if restart is not None and tagged[restart[0]] > (
            start.row if start.table is cut.table else 0
        ):
            row, part.keep = tagged[restart[0]] - first, restart[1]
            printer.printed(row)
            return part, Start(cut.element, cut.table, first + row)
        plan = _grown(layout, plan, sizing, limit)


def _grown(layout: Layout, plan: PartPlan, sizing: dict[int, list[int]], limit: int) -> PartPlan:
    """The next attempt after no later page of ``plan`` began cleanly: cut later, or not at all.

    The cut moves on by at least MIN_CUT_ROWS rows and at most as far as
    ``limit`` cells allow; where that is no further, the table cannot be
    printed in parts.
    """
    cut = plan.cut
    assert cut is not None
    table = cut.table
    first = repeated_from(table, plan.start.row) if plan.start.table is table else 0
    grown = cut.row_end + max(MIN_CUT_ROWS, cut.row_end - first)
    if grown >= len(table.rows) - MIN_CUT_ROWS:
        # The rest of the table, and what follows it up to where a part may end.
        end = cut.element + 1
        while end < len(layout.elements) and not _boundary(layout.elements, end):
            end += 1
        whole = PartPlan(plan.start, end)
        if part_cells(layout, whole, sizing) <= limit:
            return whole
        grown = len(table.rows) - MIN_CUT_ROWS
    # The rows the pass has room for besides everything else it prints.
    room = limit - part_cells(layout, plan, sizing) + table.cells_between(first, cut.row_end)
    grown = min(grown, table.rows_within(first, room))
    if grown <= cut.row_end:
        raise ResourceLimitError(
            f"A table cannot be printed in parts: cells spanning rows leave no page to start a "
            f"part on within the {limit:,} table cells one pass may hold; split the table or its "
            f"merged cells"
        )
    return PartPlan(plan.start, plan.end_element, Cut(cut.element, table, grown))


def part_cells(layout: Layout, plan: PartPlan, sizing: dict[int, list[int]]) -> int:
    """The table cells a part lays out, its hidden sizing rows included; never fewer."""
    elements = layout.elements
    start, cut = plan.start, plan.cut
    cells = 0
    index = start.element
    if start.table is not None:
        table = start.table
        last = cut.row_end if cut is not None and cut.table is table else len(table.rows)
        printed_from = repeated_from(table, start.row)
        cells += table.cells_between(printed_from, last)
        cells += _sizing_cells(table, sizing, start.row, last)
        cells += _stand_in_cells(table.rows[printed_from]) if start.row else 0
        if cut is not None and cut.table is table:
            return cells
        cells += elements[index].cells - table.cells  # what follows the table in its element
        index += 1
    stop = cut.element if cut is not None else plan.end_element
    cells += sum(element.cells for element in elements[index:stop])
    if cut is not None:
        table = cut.table
        cells += elements[cut.element].cells - table.cells + table.cells_between(0, cut.row_end)
        cells += _sizing_cells(table, sizing, 0, cut.row_end)
    return cells


def _sizing_cells(table: Table, sizing: dict[int, list[int]], first: int = 0, last: int = 0) -> int:
    """The cells of the hidden rows a part of ``table`` printing rows first to last holds."""
    rows = table.rows
    copied = sizing.get(id(table), [])
    if table.spanning and copied:
        earlier, later = _hidden_rows(table, copied, first, last)
        copied = earlier + later
    return sum(rows[index].cells + _stand_in_cells(rows[index]) for index in copied)


def _stand_in_cells(row: htmlplan.Row) -> int:
    """The empty cells a copy of ``row`` holds for the cells spanning into it."""
    return sum(cover.colspan for cover in row.covered)


def overlay_html(
    sizes: list[tuple[float, float]],
    rules: list[PageRule],
    head: str,
    running_text: list[tuple[str, str]],
) -> str:
    """Empty pages of the given sizes, for printing running headers and footers.

    A page's size picks its @page rule, and with it the margins where Chromium
    draws the header and footer. The document's own styles come along so the
    header and footer set in its fonts, and hidden samples of their text make
    the browser load those fonts before printing.
    """
    names: dict[PageRule, str] = {}
    pages = []
    for width_pt, height_pt in sizes:
        rule = min(
            rules,
            key=lambda r: (
                abs(r.width_mm * _PT_PER_MM - width_pt) + abs(r.height_mm * _PT_PER_MM - height_pt)
            ),
        )
        name = names.setdefault(rule, "pf-running-%d" % len(names))
        pages.append(f'<div class="{name}"></div>')
    # The body parts paint the page; the overlay must draw nothing but text.
    css = "".join(
        "@page %s { %s background: none; } .%s { page: %s; break-before: page; height: 1px; }"
        % (name, rule.css, name, name)
        for rule, name in names.items()
    )
    css += "html, body { background: none !important; }"
    style = re.search(r"<style>.*?</style>", head, re.S)
    samples = "".join(
        '<div aria-hidden="true" style="position:absolute;visibility:hidden;'
        'font-family:%s;font-size:7pt">%s</div>'
        % (html_lib.escape(font, quote=True), html_lib.escape(text))
        for font, text in running_text
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        + (style.group(0) if style else "")
        + "<style>body{margin:0}"
        + css
        + "</style></head><body>"
        + samples
        + "".join(pages)
        + "</body></html>"
    )
