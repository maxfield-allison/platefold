"""Row spans that the tagged PDF can hold, written into the table markup.

Chromium tags a table cell with the row span its markup gives, but the tagged
table does not always hold those rows. A row with no cells of its own has
nothing to tag and is left out, although a cell above that spans over it
still counts it. Layout also ends a span at the end of its row group
(``thead``, ``tbody``, ``tfoot``), and a row span of 0 reaches exactly there,
but the tag keeps the number as written. Either way the cell's tag claims
rows that are not its own, and the tagged rows disagree on how many columns
the table has (PDF/UA-1 rules 7.2-42 and 7.2-43).

``normalize`` rewrites the markup before the document is printed, in one
pass or in parts, so that the spans written are the spans laid out. A span
is clamped to its row group. A row with no cells that a span covers is
hidden with ``display: none`` and marked ``data-pf-covered``, and every span
over it counts one row fewer. Layout leaves a hidden row out of the table
grid, so the table lays out and draws as before; the row stays in the
markup, so the stripes ``nth-child`` selects do not move, and the part
planner (``htmlplan``) counts spans in laid-out rows by the mark. One thing
does change: where such a cell breaks across pages, Chromium used to draw
the next row over the cell's last lines, and no longer does.

A table no reader generated, such as raw HTML in Markdown, can also have
rows that hold fewer columns than others, which the tagged table cannot hold
either (7.2-42). Its short rows are filled out with empty cells that draw
nothing, as the DOCX reader fills a short Word row (see ``table_grid``).

Otherwise only the start tags of cells and rows change. Tables are read the
way the HTML parser builds them, with the end tags it implies. Two kinds of table
keep spans their tags cannot hold, and ``normalize`` warns of each: one with
a covered row that has attributes of its own, since hiding it could drop an
authored height or a link target, and one whose SVG or MathML content holds
table tags. Cells that overlap because the markup places one where a cell
above already spans, which HTML calls a table model error, cannot be tagged
consistently and are left as written.
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from . import htmlplan
from .htmlplan import ATTRIBUTE, authored_spans, cell_spans, without_attributes
from .limits import ResourceLimitError
from .table_grid import GRID_PAD, SHORT_ROW, Grid

_ROWSPAN = re.compile(r"rowspan", re.I)
_SPAN = re.compile(r"span", re.I)
_TABLE_START = re.compile(r"<table(?=[\s/>])", re.I)
_GENERATED_TABLE = '<table class="pf-data-table"'
_UNBOUNDED = 1 << 62
# Tokens as the HTML tokenizer splits them. Every alternative matches from its
# first characters on, to the end of the text if need be, so the search is
# linear. The last group is empty for a tag the text ends inside.
_TOKEN = re.compile(
    # A comment, including the empty forms <!--> and <!--->.
    r"<!--(?:-?>|.*?(?:--!?>|\Z))"
    # A DOCTYPE, processing instruction or other bogus comment.
    r"|<[!?][^>]*+>?|</(?![A-Za-z])[^>]*+>?"
    # A start or end tag. An unquoted attribute value may hold quotes.
    r"|<(/?)([A-Za-z][^\s/>]*+)((?:[\s/]*+[^\s/>][^\s/>=]*+"
    r"(?:\s*+=\s*+(?:\"[^\"]*+(?:\"|\Z)|'[^']*+(?:'|\Z)|[^\s>]*+))?+)*+)[\s/]*+(>?)",
    re.S,
)
# Elements whose content the parser reads as text, not markup.
_RAW_TEXT = frozenset(
    {"script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes", "noscript"}
)
_FOREIGN = frozenset({"svg", "math"})
# Start tags that end SVG or MathML content and continue as HTML.
_BREAKOUT_NAMES = (
    "b big blockquote body br center code dd div dl dt em embed h1 h2 h3 h4 h5 h6 head hr i img "
    "li listing menu meta nobr ol p pre ruby s small span strong strike sub sup table tt u ul var"
)
_BREAKOUT = frozenset(_BREAKOUT_NAMES.split())
_GROUPS = frozenset({"thead", "tbody", "tfoot"})
_SECTIONS = frozenset({"caption", "col", "colgroup", *_GROUPS})
# Start tags that end an open cell or caption and continue in the table.
_ENDS_CELL = frozenset({*_SECTIONS, "td", "th", "tr"})
_TABLE_TAGS = frozenset({"table", *_ENDS_CELL})
# Tags that can change how tables are read; others are skipped outside SVG and MathML.
_WATCHED = frozenset({*_TABLE_TAGS, *_RAW_TEXT, *_FOREIGN, "template", "plaintext"})
# The attributes a hidden row gets. Only rows without attributes of their own are hidden.
HIDDEN_ROW = f' {htmlplan.HIDDEN_ROW} aria-hidden="true" style="display:none"'
# The warning code for a table whose tags may still misplace its cells.
WARNING = "TABLE_SPAN_TAGS"


@dataclass(slots=True)
class _Row:
    name_end: int  # offset just past "<tr", or -1 for a row the parser implies
    authored: bool = False  # its start tag has attributes: an id, a style, a class
    cells: int = 0
    # Cells whose row span is not 1: (attribute text offset, attribute text, span as written).
    spans: list[tuple[int, str, int]] = field(default_factory=list)
    # Cells with a colspan or rowspan attribute: cell index to (colspan, rowspan) as laid out.
    sizes: dict[int, tuple[int, int]] = field(default_factory=dict)
    attributes: str = ""  # the start tag's attribute text
    end: int = -1  # where the parser ends the row: the token that ends it, or -1 if none does


@dataclass(slots=True)
class _Table:
    groups: list[list[_Row]] = field(default_factory=list)
    group: str | None = None  # the open row group's element name
    row: _Row | None = None
    cell: str | None = None  # "td" or "th" while a cell is open
    caption: bool = False
    unsure: bool = False  # SVG or MathML content holds table tags
    name: str = ""  # "Table 2", or "A table inside Table 2" for a nested one


class _Reader:
    """Tables as the HTML parser builds them, following its table insertion modes."""

    def __init__(self) -> None:
        self.tables: list[_Table] = []
        self.open: list[_Table] = []
        self.top = 0  # top-level tables so far, as a reader counts them
        self.foreign = 0  # depth of open SVG and MathML elements

    def _mark_unsure(self) -> None:
        # Marking runs down from the innermost table, so the first table
        # already marked means every one below it is too.
        for table in reversed(self.open):
            if table.unsure:
                break
            table.unsure = True

    def tag(self, end: bool, name: str, match: re.Match) -> None:
        closed = match.group(0).endswith("/>")
        if self.foreign:
            if name in _FOREIGN:
                self.foreign += -1 if end else 0 if closed else 1
                return
            if end or name not in _BREAKOUT:
                if name in _TABLE_TAGS:
                    self._mark_unsure()
                return
            self.foreign = 0
        elif not end and name in _FOREIGN:
            self.foreign = 0 if closed else 1
            return
        if end:
            self._end(name, match.start())
        else:
            self._start(name, match)

    def _start(self, name: str, match: re.Match) -> None:
        if name == "table":
            self._open_table(match.start())
            return
        if not self.open or name not in _TABLE_TAGS:
            return
        table = self.open[-1]
        if table.cell or table.caption:
            if name not in _ENDS_CELL:
                return
            table.cell, table.caption = None, False
        if name in _SECTIONS:
            _end_row(table, match.start())
            table.row = table.group = None
            if name in _GROUPS:
                table.group = name
                table.groups.append([])
            table.caption = name == "caption"
        else:
            self._row_part(table, name, match)

    def _open_table(self, at: int) -> None:
        # Outside a cell or caption, a table start ends the open table.
        while self.open and not (self.open[-1].cell or self.open[-1].caption):
            _end_row(self.open.pop(), at)
        if not self.open:
            self.top += 1
        name = f"{'A table inside ' if self.open else ''}Table {self.top}"
        self.open.append(_Table(name=name))
        self.tables.append(self.open[-1])

    @staticmethod
    def _row_part(table: _Table, name: str, match: re.Match) -> None:
        if table.group is None:
            table.group = "tbody"
            table.groups.append([])
        row = table.row
        if name == "tr" or row is None:
            _end_row(table, match.start())
            if name == "tr":
                attributes = match.group(3)
                authored = bool(attributes.strip(" \t\n\f\r/"))
                row = _Row(match.start(2) + 2, authored=authored, attributes=attributes)
            else:
                row = _Row(-1)
            table.row = row
            table.groups[-1].append(row)
        if name != "tr":
            attributes = match.group(3)
            if _SPAN.search(attributes):
                rowspan = authored_spans(attributes)[1]
                if rowspan != 1:
                    row.spans.append((match.start(3), attributes, rowspan))
                row.sizes[row.cells] = cell_spans(attributes)
            row.cells += 1
            table.cell = name

    def _end(self, name: str, at: int) -> None:
        if not self.open:
            return
        table = self.open[-1]
        if not self._ends_content(table, name):
            return
        if name == "table":
            _end_row(self.open.pop(), at)
        elif self._open_as(table, name):
            _end_row(table, at)
            table.row = None
            if name in _GROUPS:
                table.group = None

    def _ends_content(self, table: _Table, name: str) -> bool:
        """Close the open cell or caption as this end tag does; whether it goes on to the table."""
        if table.cell:
            if name in ("td", "th"):
                if name == table.cell:
                    table.cell = None
                return False
            if name != "table" and not self._open_as(table, name):
                return False
            table.cell = None
        elif table.caption:
            if name not in ("caption", "table"):
                return False
            table.caption = False
            return name == "table"
        return True

    @staticmethod
    def _open_as(table: _Table, name: str) -> bool:
        if name == "tr":
            return table.row is not None
        return name in _GROUPS and table.group == name


def _end_row(table: _Table, at: int) -> None:
    """The open row of ``table`` ends at offset ``at``."""
    if table.row is not None and table.row.end < 0:
        table.row.end = at


def _tables(html: str, cancel_check: Callable[[], None]) -> list[_Table]:
    reader = _Reader()
    pos = 0
    count = 0
    while pos < len(html):
        for match in _TOKEN.finditer(html, pos):
            name = match.group(2)
            if name is None:
                continue  # a comment or bogus comment
            count += 1
            if not count % 4096:
                cancel_check()
            name = name.lower()
            if not (reader.foreign or name in _WATCHED):
                continue
            if not match.group(4):
                return reader.tables  # a tag the text ends inside: the parser drops it
            end = bool(match.group(1))
            reader.tag(end, name, match)
            if end or reader.foreign:
                continue
            if name == "plaintext":
                return reader.tables
            if name in _RAW_TEXT or name == "template":
                pos = _content_end(html, match.end(), name)
                break
        else:
            break
    return reader.tables


def _content_end(html: str, pos: int, name: str) -> int:
    """Where the text content of a raw-text element or template that starts at ``pos`` ends."""
    if name != "template":
        close = re.compile(rf"</{name}(?=[\s/>])", re.I).search(html, pos)
        return len(html) if close is None else close.start()
    depth = 1  # a template's content is inert, never laid out
    for match in _TEMPLATE.finditer(html, pos):
        depth += -1 if match.group(1) else 1
        if depth == 0:
            return match.start()
    return len(html)


_TEMPLATE = re.compile(r"<(/?)template(?=[\s/>])", re.I)


def _group_edits(rows: list[_Row]) -> tuple[list[tuple[int, int, str]], bool]:
    """Replacements (start, end, text) that give one row group its laid-out spans.

    Also whether a covered row without cells was kept because it has
    attributes: hiding it could drop an authored height or a link target.
    """
    # The rows each span reaches: at most to the group's end, where a span of 0 reaches.
    reach = [
        [
            len(rows) - index if span == 0 else min(span, len(rows) - index)
            for _, _, span in row.spans
        ]
        for index, row in enumerate(rows)
    ]
    hidden = []
    kept = False
    covered_to = -1
    for index, row in enumerate(rows):
        covered = not row.cells and index <= covered_to
        hidden.append(covered and not row.authored)
        kept |= covered and row.authored
        for rows_spanned in reach[index]:
            covered_to = max(covered_to, index + rows_spanned - 1)
    hidden_before = [0]
    for flag in hidden:
        hidden_before.append(hidden_before[-1] + flag)
    edits: list[tuple[int, int, str]] = []
    for index, row in enumerate(rows):
        if hidden[index]:
            edits.append((row.name_end, row.name_end, HIDDEN_ROW))
        for (at, attributes, span), rows_spanned in zip(row.spans, reach[index], strict=True):
            wanted = rows_spanned - (hidden_before[index + rows_spanned] - hidden_before[index])
            if wanted != span:
                others = without_attributes(attributes, "rowspan")
                text = (f' rowspan="{wanted}"' if wanted != 1 else "") + others
                edits.append((at, at + len(attributes), text))
    return edits, kept


def _short_rows(table: _Table, budget: int) -> list[tuple[int, int, str]]:
    """Edits that fill the table's short rows out to its widest with empty cells.

    The cells and the row's class are those the DOCX reader writes for a short
    Word row (see ``table_grid``), so the row draws as it did. A table with a
    row the markup never ends is left alone: there is no place to put them.
    Raises ResourceLimitError when the cells would take the document past
    ``budget`` more cells.
    """
    grid = Grid(table.name, None)
    rows = []
    for group in table.groups:
        grid.group()
        for row in group:
            if row.cells and row.end < 0:
                return []
            grid.row(len(rows))
            rows.append(row)
            for index in range(row.cells):
                grid.cell(*row.sizes.get(index, (1, 1)))
            grid.end_row(row.end)
    pads = grid.padding(budget)
    if pads is None:
        raise ResourceLimitError(
            f"{table.name} needs more empty cells to fill its short rows than the document's "
            "cell limit allows; split the document or its tables"
        )
    edits = []
    for index, end, spans in pads:
        # The no-break space that fill_empty_cells gives every empty cell, which
        # has already run: Chromium leaves an empty cell out of the tagged table.
        cells = "".join(
            f'<td class="{GRID_PAD}"' + (f' colspan="{span}"' if span > 1 else "") + ">&#160;</td>"
            for span in spans
        )
        edits.append((end, end, cells))
        row = rows[index]
        if row.name_end >= 0:
            edits.append((row.name_end, row.name_end + len(row.attributes), _short(row.attributes)))
    return edits


def _short(attributes: str) -> str:
    """A row's attribute text with SHORT_ROW added to its classes."""
    existing = next(
        (
            next((value for value in match.groups()[1:] if value is not None), "")
            for match in ATTRIBUTE.finditer(attributes)
            if match.group(1).lower() == "class"
        ),
        None,
    )
    classes = SHORT_ROW if existing is None else f"{SHORT_ROW} {html_lib.unescape(existing)}"
    kept = without_attributes(attributes, "class").rstrip(" \t\n\f\r/")
    return f' class="{html_lib.escape(classes, quote=True)}"{kept}'


def _authored_tables(html: str) -> bool:
    """Whether the markup holds a table no reader generated.

    Readers write regular tables with the data-table class, and the DOCX
    reader fills its short rows itself, so a document of those alone is not
    read again: reading a million-cell sheet's markup takes seconds.
    """
    return len(_TABLE_START.findall(html)) > html.count(_GENERATED_TABLE)


def normalize(
    html: str,
    cancel_check: Callable[[], None] = lambda: None,
    *,
    warn: Callable[[str, str], None] | None = None,
    max_cells: int | None = None,
) -> str:
    """The markup with every table's row spans written as the browser lays them out.

    Tables no reader generated also have their short rows filled out to the
    table's widest row (see ``_short_rows``), within ``max_cells`` cells for
    the whole document. ``warn(code, detail)`` hears of each table whose tags
    may still misplace its cells.
    """
    pad = _authored_tables(html)
    if not (pad or _ROWSPAN.search(html)):
        return html
    tables = _tables(html, cancel_check)
    budget = (
        max_cells - sum(row.cells for table in tables for rows in table.groups for row in rows)
        if max_cells is not None
        else None
    )
    edits = []
    for table in tables:
        changes, kept = [], False
        for rows in table.groups:
            group, group_kept = _group_edits(rows)
            changes += group
            kept |= group_kept
        if table.unsure:
            if changes and warn is not None:
                warn(
                    WARNING,
                    f"{table.name} holds SVG or MathML with table tags, so its merged "
                    "cells were left as written; the PDF's table tags may place its cells in "
                    "the wrong columns.",
                )
            continue
        edits += changes
        if pad:
            padding = _short_rows(table, budget if budget is not None else _UNBOUNDED)
            budget = None if budget is None else budget - sum(e[2].count("<td") for e in padding)
            edits += padding
        if kept and warn is not None:
            warn(
                WARNING,
                f"{table.name} has a merged cell over a row that has no cells but has "
                "attributes, such as an id or a style. The row is kept so the page and its links "
                "do not change, but the PDF's table tags leave it out, so they may place the "
                "table's cells in the wrong columns.",
            )
    if not edits:
        return html
    edits.sort()
    pieces = []
    at = 0
    for start, end, text in edits:
        pieces += [html[at:start], text]
        at = end
    pieces.append(html[at:])
    return "".join(pieces)
