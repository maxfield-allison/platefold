"""One table's cells on the HTML table grid, and the empty cells that fill its short rows.

The DOCX reader places Mammoth's cells on the grid as it writes them, and
``table_spans`` places a document's other tables as the browser reads their
markup. Both fill a row with fewer columns than its table out with empty
cells, so that every tagged row has the same columns (PDF/UA-1 7.2-42).
"""

from __future__ import annotations

from .htmlplan import MAX_COLSPAN, Cover, free_column, free_span

# The classes of a short table row and of the empty cells that fill it out to
# the table's grid; the stylesheet stripes the row's own cells, not the row.
SHORT_ROW = "pf-short-row"
GRID_PAD = "pf-grid-pad"


def runs(columns: int) -> list[int]:
    """Colspans that together cover ``columns`` free columns, none wider than the browser allows."""
    return [min(MAX_COLSPAN, columns - at) for at in range(0, max(columns, 0), MAX_COLSPAN)]


class Grid:
    """One table's cells placed on the HTML table grid, narrowed where they would overlap.

    Markup can place a cell whose colspan runs into a cell spanning down from
    above, which HTML calls a table model error and the browser draws as two
    cells over each other. Mammoth writes such cells for a Word table whose
    rows do not fill one regular grid.

    Cells are placed as the browser places them, each at the next column no
    cell from above holds. A cell whose colspan runs into a cell from above
    keeps only the columns up to it, and its text. Its rowspan never needs
    narrowing: a cell placed earlier that reaches a later row either reaches
    this row too or lies to the left in it, so the columns this cell keeps are
    free in every row it spans.
    """

    def __init__(self, name: str, heading: str | None) -> None:
        self.name = name  # "Table 2", or "A table inside Table 2" for a nested one
        self.heading = heading  # the heading it follows, if any
        self.narrowed = 0
        self._spanning: list[tuple[int, Cover]] = []  # (last row each holds, cell)
        self._covered: tuple[Cover, ...] = ()  # cells from above in this row, by column
        self._row = -1
        self._start = -1  # output offset of the row's start tag, or -1 for an implied row
        self._column = 0
        self._held: list[tuple[int, int]] = []  # this row's (column, colspan), from above too
        self._cells = 0  # the row's own cells
        self._marked = False  # the row holds a stand-in for a Word row indent
        # Each row with cells of its own: (start offset, end offset, held, marked).
        self._rows: list[tuple[int, int, list[tuple[int, int]], bool]] = []

    def group(self) -> None:
        """A row group starts; the browser ends every span at its group's end."""
        self._spanning = []
        self._row = -1

    def row(self, start: int = -1) -> None:
        self._row += 1
        self._start = start
        self._column = 0
        self._spanning = [(last, cell) for last, cell in self._spanning if last >= self._row]
        self._covered = tuple(sorted((cell for _, cell in self._spanning), key=lambda c: c.column))
        self._held = [(cell.column, cell.colspan) for cell in self._covered]
        self._cells = 0
        self._marked = False

    def cell(self, colspan: int, rowspan: int) -> int:
        """Place the next cell of the row; the colspan it can keep."""
        if self._row < 0:
            self.row()  # a cell before any row: the parser implies one
        column = free_column(self._covered, self._column)
        kept = free_span(self._covered, column, colspan)
        self.narrowed += kept < colspan
        if rowspan > 1:
            # A Cover's markup offsets serve the part planner; they are not needed here.
            self._spanning.append((self._row + rowspan - 1, Cover(column, kept, -1, -1)))
        self._column = column + kept
        self._held.append((column, kept))
        self._cells += 1
        return kept

    def end_row(self, offset: int) -> None:
        """The row ends at ``offset`` in the output.

        A row with no cells of its own stays out of the grid: a span covers it
        and it is hidden (see table_spans), or the browser leaves it untagged.
        """
        if self._row >= 0 and self._cells:
            self._rows.append((self._start, offset, self._held, self._marked))

    def stand_in(self, narrowed: bool) -> None:
        """The cell just placed stands in for columns a Word row leaves empty."""
        self._marked = True
        self.narrowed -= narrowed  # an empty cell narrowed loses nothing

    def padding(self, budget: int) -> list[tuple[int, int, list[int]]] | None:
        """Empty cells that fill every row out to the widest, as colspans, with each row's offsets.

        Word draws a row with fewer cells than the table's grid short, and the
        browser leaves the missing places empty in the same way, but a tagged
        table must give every row the same columns (PDF/UA-1 7.2-42). Each run
        of free columns gets one empty cell, or more where it is wider than a
        cell can span. The cells draw no border, and the row's stripe goes on
        its own cells, so the row looks as it did whatever lies behind it.

        The cells are counted before any is made: None when there would be
        more than ``budget``. A few wide cells over many short rows would
        otherwise ask for rows times width of them.
        """
        width = max(
            (column + span for _, _, held, _ in self._rows for column, span in held), default=0
        )
        gaps = []
        count = 0
        for start, end, held, marked in self._rows:
            free = []
            at = 0
            for column, span in sorted(held):
                if column > at:
                    free.append(column - at)
                at = max(at, column + span)
            if width > at:
                free.append(width - at)
            count += sum(-(-columns // MAX_COLSPAN) for columns in free)
            if count > budget:
                return None
            if free or marked:
                gaps.append((start, end, free))
        return [
            (start, end, [s for columns in free for s in runs(columns)])
            for start, end, free in gaps
        ]
