"""Find where a rendered document can be printed in separate passes.

A pass boundary is exact in two places. Between two top-level elements where
the stylesheet forces a page break, nothing on either side can move. Inside a
table, between rows, the next pass can start again from a row that began a
page, because a table's continuation page holds nothing but that table (see
``pdfjoin.restart_point``). This module indexes the document's HTML to find
those places; it never changes the document.

The HTML comes from Plate|Fold's own renderers, which close every element.
Where the markup cannot be followed (an unclosed element, a table inside a
table, a table footer, rows joined by a cell spanning them) it is not split.
"""

from __future__ import annotations

import bisect
import html as html_lib
import itertools
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from typing import NamedTuple

_TAG = re.compile(
    r"<!--.*?-->|<(/?)([A-Za-z][A-Za-z0-9-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>",
    re.S,
)
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param"}
    | {"source", "track", "wbr"}
)
_RAW_TEXT = frozenset({"script", "style", "textarea", "title"})
_CLASS = re.compile(r"""\bclass\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
ATTRIBUTE = re.compile(r"""([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]*)))?""")
_HTML_INTEGER = re.compile(r"[ \t\n\f\r]*(?:\+?([0-9]+)|-0+(?![0-9]))")
# The browser's limits on a cell's spans. A rowspan of 0 reaches the end of its
# row group, as the largest one does: the browser clamps both there.
MAX_COLSPAN = 1000
MAX_ROWSPAN = 65534
# The attribute table_spans marks a hidden row with: not laid out, so no span counts it.
HIDDEN_ROW = "data-pf-covered"

# Classes whose elements the stylesheet starts or ends with a forced page break.
BREAK_BEFORE = frozenset({"table-plate", "plate", "tile-sheet"})
BREAK_AFTER = frozenset({"table-plate", "plate", "tile-sheet", "toc"})


class Unsplittable(ValueError):
    """The markup could not be followed well enough to split it safely."""


def cell_spans(attributes: str) -> tuple[int, int]:
    """A cell's colspan and rowspan, read as the browser reads them.

    The first of repeated attributes counts, values are decoded, and a
    number may have leading space, a plus sign and trailing text; a rowspan
    of 0 spans every following row of its group.
    """
    colspan, rowspan = authored_spans(attributes)
    return colspan, rowspan or MAX_ROWSPAN


def authored_spans(attributes: str) -> tuple[int, int]:
    """A cell's colspan and rowspan as the browser parses them, a rowspan of 0 kept as 0.

    These are the values Chromium writes into the cell's tag.
    """
    values: dict[str, str] = {}
    for match in ATTRIBUTE.finditer(attributes):
        name = match.group(1).lower()
        if name in ("colspan", "rowspan") and name not in values:
            value = next((group for group in match.groups()[1:] if group is not None), "")
            values[name] = html_lib.unescape(value)
    return (
        _span(values.get("colspan"), 1, MAX_COLSPAN),
        _span(values.get("rowspan"), 0, MAX_ROWSPAN),
    )


def _is_hidden_row(attributes: str) -> bool:
    return HIDDEN_ROW in attributes.lower() and any(
        match.group(1).lower() == HIDDEN_ROW for match in ATTRIBUTE.finditer(attributes)
    )


def without_attributes(attributes: str, *names: str) -> str:
    """An opening tag's attribute text without the named attributes."""
    kept = []
    at = 0
    for match in ATTRIBUTE.finditer(attributes):
        if match.group(1).lower() in names:
            kept.append(attributes[at : match.start()].rstrip())
            at = match.end()
    return "".join(kept) + attributes[at:]


def _span(value: str | None, least: int, most: int) -> int:
    match = _HTML_INTEGER.match(value) if value is not None else None
    if match is None:
        return 1
    return max(least, min(int(match.group(1) or 0), most))


def _classes(attributes: str) -> frozenset[str]:
    match = _CLASS.search(attributes)
    if match is None:
        return frozenset()
    value = next(group for group in match.groups() if group is not None)
    return frozenset(html_lib.unescape(value).split())


class Cover(NamedTuple):
    """A cell from an earlier row that spans down into a row."""

    column: int
    colspan: int
    start: int  # offset of its "<td" or "<th"
    open_end: int  # offset just past its opening tag


@dataclass
class Row:
    start: int
    end: int
    cells: int = 0  # grid columns the row fills, spans included
    spans_down: int = 0  # further rows that a rowspan in this row covers
    covered: tuple[Cover, ...] = ()  # cells above spanning into it, by column
    hidden: bool = False  # a covered row without cells, left out of layout (table_spans)


def place_cells(covered: tuple[Cover, ...], spans: Iterable[int]) -> list[int]:
    """The grid column of each cell of a row, given its colspans and the cells above.

    Each cell takes the next column no cell from above occupies, as the
    browser's table layout places it.
    """
    columns = []
    column = 0
    for colspan in spans:
        column = free_column(covered, column)
        columns.append(column)
        column += colspan
    return columns


def free_column(covered: tuple[Cover, ...], column: int) -> int:
    """The first column from ``column`` on that no cell from above holds."""
    for cover in covered:
        if cover.column <= column < cover.column + cover.colspan:
            column = cover.column + cover.colspan
    return column


def free_span(covered: tuple[Cover, ...], column: int, colspan: int) -> int:
    """How many of the ``colspan`` columns from a free ``column`` on no cell from above holds.

    A cell whose colspan runs into a cell from above overlaps it, which HTML
    calls a table model error; this is the colspan that stops short of it.
    """
    for cover in covered:
        if column < cover.column < column + colspan:
            colspan = cover.column - column
    return colspan


@dataclass
class Table:
    """One table and what is needed to print part of it on its own."""

    start: int  # offset of "<table"
    open_end: int  # offset just past the opening tag
    end: int = -1  # offset just past "</table>"
    wrappers: list[str] = field(default_factory=list)  # opening tags, outermost first
    wrapper_names: list[str] = field(default_factory=list)
    h1_before: list[int] = field(default_factory=list)  # per level, body level first
    caption: tuple[int, int] | None = None
    thead: tuple[int, int] | None = None
    header_rows: int = 0  # header rows with cells: those the tagged PDF holds
    tbody: tuple[int, int] | None = None  # the explicit tbody's inner span
    rows: list[Row] = field(default_factory=list)  # body rows only
    simple: bool = True
    spanning: bool = False  # a body cell spans rows or columns

    @property
    def columns(self) -> int:
        return max((row.cells for row in self.rows), default=0)

    @property
    def cells(self) -> int:
        return sum(row.cells for row in self.rows)

    # The rows are complete once the table is scanned; these are read after.
    @cached_property
    def _cells_before(self) -> list[int]:
        return list(itertools.accumulate((row.cells for row in self.rows), initial=0))

    def cells_between(self, first: int, last: int) -> int:
        """Cells in body rows ``first`` to ``last``, the last excluded."""
        return self._cells_before[last] - self._cells_before[first]

    def rows_within(self, first: int, cells: int) -> int:
        """The furthest ``last`` whose rows ``first`` to ``last`` hold at most ``cells`` cells."""
        target = self._cells_before[first] + cells
        return max(first, bisect.bisect_right(self._cells_before, target) - 1)

    @cached_property
    def _allowed(self) -> list[bool]:
        # Spans count the rows laid out; a hidden row is none of them, nor a start.
        allowed = [False] * len(self.rows)
        covering = 0  # laid-out rows the spans so far still reach
        for index, row in enumerate(self.rows):
            if row.hidden:
                continue
            allowed[index] = not covering
            covering = max(covering - 1, row.spans_down)
        return allowed

    @cached_property
    def _row_starts(self) -> list[int]:
        return [row.start for row in self.rows]

    def row_at(self, offset: int) -> int:
        """The body row holding the markup at ``offset``."""
        return bisect.bisect_right(self._row_starts, offset) - 1

    def restart_allowed(self) -> list[bool]:
        """Rows a pass may start from: no row-spanning cell reaches into them."""
        return list(self._allowed)

    @cached_property
    def restarts(self) -> list[int]:
        """The rows a pass may start from, in order."""
        return [index for index, allowed in enumerate(self._allowed) if allowed]


@dataclass
class Element:
    """A top-level element of the body, with the text before it."""

    start: int
    end: int
    break_before: bool
    break_after: bool
    h1_before: int  # body-level h1 elements before this one
    tables: list[Table] = field(default_factory=list)
    cells: int = 0  # every table cell inside, split or not


@dataclass
class Layout:
    html: str
    body_start: int  # offset just past "<body...>"
    body_end: int  # offset of "</body>"
    elements: list[Element]

    @property
    def head(self) -> str:
        return self.html[: self.body_start]

    @property
    def tail(self) -> str:
        return self.html[self.body_end :]


@dataclass
class _Open:
    name: str
    text: str
    classes: frozenset[str]
    h1_children: int = 0
    last_chain: tuple[frozenset[str], ...] = ()


@dataclass
class _TableState:
    table: Table
    depth: int  # stack length when the table opened
    row: Row | None = None
    in_thead: bool = False
    # Body cells spanning rows, as [column, colspan, rows still to cover, Cover];
    # the column the next cell of the current row may take.
    spanning: list[list] = field(default_factory=list)
    column: int = 0
    thead_start: int = 0
    tbody_start: int = 0


class _Scanner:
    def __init__(self, html: str):
        self.html = html
        body = re.search(r"<body\b[^>]*>", html)
        self.body_end = html.rfind("</body>")
        if body is None or self.body_end < 0:
            raise Unsplittable("document has no body")
        self.body_start = body.end()
        self.stack: list[_Open] = []
        self.tables: list[_TableState] = []
        self.done_tables: list[Table] = []
        self.elements: list[Element] = []
        self.body_h1 = 0
        self.element_start = self.body_start
        self.first_chain: list[frozenset[str]] = []
        self.element_h1 = 0

    def run(self) -> Layout:
        pos = self.body_start
        while True:
            match = _TAG.search(self.html, pos, self.body_end)
            if match is None:
                break
            pos = match.end()
            if match.group(0).startswith("<!--"):
                continue
            if match.group(1):
                self._close(match)
            else:
                pos = self._open(match)
        if self.stack:
            raise Unsplittable(f"unclosed <{self.stack[-1].name}>")
        if self.elements:
            self.elements[-1].end = self.body_end
        return Layout(self.html, self.body_start, self.body_end, self.elements)

    # -- opening tags ------------------------------------------------------
    def _open(self, match: re.Match) -> int:
        name, attributes = match.group(2).lower(), match.group(3)
        classes = _classes(attributes)
        pos = match.end()
        if not self.stack and not self.first_chain:
            self.element_h1 = self.body_h1
        if len(self.first_chain) == len(self.stack):
            self.first_chain.append(classes)
        if name == "h1":
            if self.stack:
                self.stack[-1].h1_children += 1
            else:
                self.body_h1 += 1
        self._table_open(name, match, attributes)
        void = name in _VOID or attributes.rstrip().endswith("/")
        if name in _RAW_TEXT and not void:
            close = self.html.find(f"</{name}", pos, self.body_end)
            if close < 0:
                raise Unsplittable(f"unclosed <{name}>")
            pos = self.html.index(">", close) + 1
            void = True
        if void:
            if not self.stack:
                self._finish_element(pos, (classes,))
            return pos
        self.stack.append(_Open(name, match.group(0), classes))
        return pos

    def _table_open(self, name: str, match: re.Match, attributes: str) -> None:
        if name == "table":
            self._start_table(match, attributes)
            return
        if not self.tables:
            return
        state = self.tables[-1]
        level = len(self.stack) - state.depth  # 1: a child of <table>
        table = state.table
        if name in ("tfoot", "colgroup", "col") and level <= 2:
            table.simple = False  # a repeating footer or authored widths
        elif level == 1 and name in ("caption", "thead", "tbody"):
            self._table_section(state, name, match)
        elif name == "tr" and (
            level == 1 or (level == 2 and self.stack[-1].name in ("thead", "tbody"))
        ):
            state.row = Row(match.start(), -1, hidden=_is_hidden_row(attributes))
            state.column = 0
            if state.spanning and not state.in_thead and not state.row.hidden:
                state.row.covered = tuple(entry[3] for entry in state.spanning)
        elif name in ("td", "th") and state.row is not None and self.stack[-1].name == "tr":
            self._cell_open(state, state.row, match, attributes)

    def _cell_open(self, state: _TableState, row: Row, match: re.Match, attributes: str) -> None:
        if row.hidden:
            # Only table_spans hides rows, and only rows without cells: an
            # authored marker on a row with cells leaves it laid out.
            row.hidden = False
            if state.spanning and not state.in_thead:
                row.covered = tuple(entry[3] for entry in state.spanning)
        colspan, rowspan = cell_spans(attributes)
        row.cells += colspan
        row.spans_down = max(row.spans_down, rowspan - 1)
        if not state.in_thead:
            state.table.spanning |= colspan > 1 or rowspan > 1
            column = free_column(row.covered, state.column)
            if rowspan > 1:
                cover = Cover(column, colspan, match.start(), match.end())
                state.spanning.append([column, colspan, rowspan, cover])
            state.column = column + colspan

    def _start_table(self, match: re.Match, attributes: str) -> None:
        table = Table(
            start=match.start(),
            open_end=match.end(),
            wrappers=[entry.text for entry in self.stack],
            wrapper_names=[entry.name for entry in self.stack],
            h1_before=[self.body_h1] + [entry.h1_children for entry in self.stack],
        )
        # A nested table, or one with authored inline style, is printed whole.
        if self.tables or re.search(r"\bstyle\s*=", attributes, re.I):
            table.simple = False
        for state in self.tables:
            state.table.simple = False
        self.tables.append(_TableState(table, len(self.stack)))

    @staticmethod
    def _table_section(state: _TableState, name: str, match: re.Match) -> None:
        table = state.table
        if name == "caption":
            table.caption = (match.start(), -1)
        elif name == "thead":
            state.in_thead = True
            state.thead_start = match.start()
        else:
            if table.tbody is not None:
                table.simple = False  # several bodies
            state.tbody_start = match.end()

    # -- closing tags ------------------------------------------------------
    def _close(self, match: re.Match) -> None:
        name = match.group(2).lower()
        index = len(self.stack) - 1
        while index >= 0 and self.stack[index].name != name:
            index -= 1
        if index != len(self.stack) - 1:
            raise Unsplittable(f"</{name}> does not close the innermost element")
        if self.tables:
            self._table_close(name, match)
        node = self.stack.pop()
        chain = (node.classes, *node.last_chain)
        if self.stack:
            self.stack[-1].last_chain = chain
        else:
            self._finish_element(match.end(), chain)

    def _table_close(self, name: str, match: re.Match) -> None:
        state = self.tables[-1]
        level = len(self.stack) - state.depth - 1  # 1: closing a child of <table>
        table = state.table
        if name == "table" and level == 0:
            table.end = match.end()
            self.tables.pop()
            self.done_tables.append(table)
            return
        if name == "caption" and level == 1 and table.caption is not None:
            table.caption = (table.caption[0], match.end())
        elif name == "thead" and level == 1:
            state.in_thead = False
            table.thead = (state.thead_start, match.end())
        elif name == "tbody" and level == 1:
            table.tbody = (state.tbody_start, match.start())
        elif name == "tr" and state.row is not None and self.stack[-1].name == "tr":
            row = state.row
            row.end = match.end()
            state.row = None
            if not state.in_thead and not row.hidden:
                # The spans that reach past this row, ordered by column.
                for entry in state.spanning:
                    entry[2] -= 1
                state.spanning = sorted(
                    (entry for entry in state.spanning if entry[2]), key=lambda entry: entry[0]
                )
            if state.in_thead:
                # Every part prints the header whole, and a cell spanning rows
                # in it ends with it, so it is no reason to print the table whole.
                table.header_rows += 1 if row.cells else 0
            else:
                table.rows.append(row)

    def _finish_element(self, end: int, last_chain: tuple[frozenset[str], ...]) -> None:
        tables = self.done_tables
        self.done_tables = []
        element = Element(
            start=self.element_start,
            end=end,
            break_before=any(classes & BREAK_BEFORE for classes in self.first_chain),
            break_after=any(classes & BREAK_AFTER for classes in last_chain),
            h1_before=self.element_h1,
            tables=[table for table in tables if table.simple and table.rows],
            cells=sum(table.cells for table in tables),
        )
        self.elements.append(element)
        self.element_start = end
        self.first_chain = []


def scan(html: str) -> Layout:
    """Index a document's top-level elements and tables."""
    return _Scanner(html).run()
