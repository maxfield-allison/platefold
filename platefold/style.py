"""Print stylesheet.

Chromium honours `@page` named pages, which is what makes mixed page sizes in a
single PDF possible. Everything here is tuned for print, not screen: physical
units, widow/orphan control, and repeating table headers.

Every colour resolves to a token on `Settings.theme`, so a new palette is a
few hex values in a theme JSON file (or a settings file's `theme` section)
rather than a fork of this file.
"""

from __future__ import annotations

from .config import Settings
from .contrast import CALLOUT_BG, CALLOUT_TEXT
from .fonts import font_css
from .layout import IMAGE_FRAME_PX
from .paper import margin_css
from .tiles import sheet_size

# Semantic colours for the "this went wrong" boxes. Deliberately *not*
# themed: a warning that blends into the page defeats the point of failing
# loudly, so these stay amber whatever the document palette is. The dashed
# border is drawn in the text colour so the box stands out from a light page
# by its border and from a dark page by its fill (`contrast.RULES`).
WARN_BG = CALLOUT_BG
WARN_TEXT = CALLOUT_TEXT
WARN_BORDER = WARN_TEXT


def build_css(custom_pages: dict, s: Settings) -> str:
    t = s.theme
    plate_rules = []
    for spec in s.plates:
        plate_rules.append(
            "@page %s { size: %smm %smm; margin: %s; }\n.%s { page: %s; }"
            % (
                spec.name,
                spec.width,
                spec.height,
                margin_css(spec.page_margins),
                spec.name,
                spec.name,
            )
        )
    for name, (w, h, m) in custom_pages.items():
        plate_rules.append(
            "@page %s { size: %smm %smm; margin: %s; }\n.%s { page: %s; }"
            % (name, w, h, margin_css(s.margins_for(m, m)), name, name)
        )
    plates = "\n".join(plate_rules)
    sheet_w, sheet_h = sheet_size(s)

    return f"""
{font_css(s.body_font + s.head_font + s.mono_font)}
@page {{ size: {s.page_width}mm {s.page_height}mm;
         margin: {margin_css(s.page_margins)};
         background: {t.page}; }}
@page print-sheet {{ size: {s.page_width}mm {s.page_height}mm; margin: {margin_css(s.page_margins)}; }}
.print-sheet {{ page: print-sheet; }}
/* Tile sheets draw their own margins so marks can sit outside the tile window. */
@page tile-sheet {{ size: {s.page_width}mm {s.page_height}mm; margin: 0; }}
.tile-sheet {{ page: tile-sheet; }}
{plates}

* {{ box-sizing: border-box; }}
html {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ margin: 0; font-family: {s.body_font}; font-size: {s.body_size_pt}pt;
        line-height: 1.46; color: {t.ink}; background: {t.page}; hyphens: none; }}

/* `position: relative` adds no offset and moves nothing, but makes the
   heading the containing block for its .pgmark child (see below). */
h1, h2, h3, h4, h5 {{ font-family: {s.head_font}; color: {t.ink_strong}; line-height: 1.22;
  margin: 0 0 .35em; break-after: avoid; page-break-after: avoid; position: relative; }}
h1 {{ font-size: 17pt; font-weight: 700; margin-top: 1.5em; padding-bottom: .18em;
     color: {t.title}; border-bottom: 1.6pt solid {t.accent}; letter-spacing: -.2px; }}
h2 {{ font-size: 13.2pt; font-weight: 700; margin-top: 1.35em; color: {t.heading}; }}
h3 {{ font-size: 11.4pt; font-weight: 700; margin-top: 1.1em; color: {t.heading}; }}
h4 {{ font-size: 10.4pt; font-weight: 700; margin-top: 1em; color: {t.ink_soft}; }}
h1:first-of-type {{ margin-top: 0; }}
p {{ margin: 0 0 .62em; orphans: 3; widows: 3; }}
ul, ol {{ margin: 0 0 .7em; padding-left: 1.35em; }}
li {{ margin-bottom: .22em; }}
li > ul, li > ol {{ margin-top: .22em; margin-bottom: .1em; }}
a {{ color: {t.link}; text-decoration: none; }}
hr {{ border: 0; border-top: .6pt solid {t.rule}; margin: 1.15em 0; }}
strong {{ color: {t.ink_strong}; }}
blockquote {{ margin: .7em 0; padding: .45em .9em; border-left: 2.5pt solid {t.accent_soft};
  background: {t.surface_alt}; color: {t.ink_soft}; }}
blockquote p:last-child {{ margin-bottom: 0; }}

/* Zero-layout page markers. Absolutely positioned inside a heading, so they
   are out of flow and cannot shift pagination, and transparent so they are
   invisible - but still present in the extracted text layer, which is how
   pdf.py maps headings onto page numbers. Only emitted on the measuring
   pass; the delivered PDF never contains them. */
.pgmark {{ position: absolute; top: 0; left: 0; color: transparent;
  font-size: 6pt; line-height: 1; white-space: nowrap; }}

/* --- document header ------------------------------------------------- */
.doc-head {{ border-bottom: 2.4pt solid {t.accent}; padding-bottom: .8em; margin-bottom: 1.4em; }}
.doc-head .eyebrow {{ font-size: 7.6pt; letter-spacing: 1.3px; text-transform: uppercase;
  color: {t.ink_soft}; font-family: {s.head_font}; margin-bottom: .35em; }}
.doc-head h1 {{ font-size: 21pt; border: 0; margin: 0 0 .55em; padding: 0; }}
.meta {{ display: grid; grid-template-columns: max-content 1fr; gap: .17em 1.1em;
  font-size: 8.9pt; color: {t.ink}; }}
.meta dt {{ font-weight: 700; color: {t.ink_soft}; text-transform: uppercase;
  letter-spacing: .4px; font-size: 7.6pt; padding-top: .16em; font-family: {s.head_font}; }}
.meta dd {{ margin: 0; }}

/* --- contents -------------------------------------------------------- */
.toc {{ break-after: page; page-break-after: always; }}
.toc h1, .toc h2 {{ margin-top: .2em; font-size: 12pt; border-bottom: .8pt solid {t.rule};
  padding-bottom: .25em; color: {t.heading}; letter-spacing: normal; }}
.toc ol {{ list-style: none; padding: 0; margin: .5em 0 0; }}
.toc li {{ margin: 0; }}
.toc a {{ display: flex; align-items: baseline; gap: .4em; color: {t.ink};
  padding: .17em 0; font-size: 9.6pt; }}
.toc .t {{ flex: 0 1 auto; }}
.toc .dots {{ flex: 1 1 auto; border-bottom: .7pt dotted {t.accent_soft}; margin-bottom: .18em; }}
/* Fixed width: the number column must not change size between the measuring
   pass and the final pass, or pagination shifts and the numbers are wrong. */
.toc .pg {{ flex: 0 0 auto; color: {t.ink_soft}; font-variant-numeric: tabular-nums;
  min-width: 1.6em; text-align: right; font-size: 9pt; }}
.toc .lvl2 a {{ padding-left: 1.15em; font-size: 9.1pt; color: {t.ink_soft}; }}
.toc .lvl3 {{ display: none; }}

/* --- tables ---------------------------------------------------------- */
table {{ border-collapse: collapse; width: 100%; margin: .55em 0 1em;
  font-size: 8.6pt; line-height: 1.32; }}
thead {{ display: table-header-group; }}       /* repeat header on each page */
tr {{ break-inside: avoid; page-break-inside: avoid; }}
th {{ background: {t.surface}; color: {t.ink_strong}; font-weight: 700; text-align: left;
  border-bottom: 1.1pt solid {t.accent_soft}; }}
th, td {{ padding: .34em .5em; border: .5pt solid {t.rule}; vertical-align: top;
  overflow-wrap: anywhere; }}
/* Short labels in two-column tables need a readable minimum. Without this,
   overflow-wrap:anywhere lets a long value squeeze labels down to letters.
   Keep emergency wrapping for URLs/identifiers and leave wide tables alone.
   The print step marks these tables once after loading. A :has() selector
   here made Chromium restyle every cell of a table each time the parser
   added a row, so a 50,000-cell sheet took minutes to load and print. */
table.pf-two-column :is(th, td) {{ min-width: 8em; }}
tbody tr:nth-child(even) {{ background: {t.surface_alt}; }}
/* A short Word row is filled out to its table's grid with empty cells that
   draw nothing, as Word leaves the rest of the row blank. An empty cell has no
   border of its own, and the row's stripe goes on the row's own cells, where
   the browser paints a row's background, so whatever lies behind the table
   shows through the empty cells as it did beyond the short row. */
td.pf-grid-pad {{ border: none; }}
tbody tr.pf-short-row:nth-child(even) {{ background: none; }}
tbody tr.pf-short-row:nth-child(even) > td:not(.pf-grid-pad) {{ background: {t.surface_alt}; }}
table code {{ font-size: 7.9pt; background: none; padding: 0; border: 0; }}
.wide-table table {{ font-size: 7.7pt; }}
.wide-table th, .wide-table td {{ padding: .28em .38em; }}
.data-table {{ margin: 0; }}
/* A table plate selects a wider named page but remains ordinary table flow.
   In particular, do not inherit figure.plate's break-inside avoidance: long
   tables must continue across pages so the browser can repeat thead. */
.table-plate {{ break-before: page; page-break-before: always;
  break-after: page; page-break-after: always; }}
.table-plate table {{ font-size: 7.7pt; }}
.table-plate th, .table-plate td {{ padding: .28em .38em; }}

/* --- code ------------------------------------------------------------ */
code {{ font-family: {s.mono_font}; font-size: 8.5pt; background: {t.surface};
  border: .4pt solid {t.rule}; border-radius: 2px; padding: .04em .28em; }}
pre {{ margin: .55em 0 1em; background: {t.surface_alt}; border: .5pt solid {t.rule};
  border-left: 2.4pt solid {t.accent}; border-radius: 3px; padding: .55em .7em;
  break-inside: avoid; page-break-inside: avoid; }}
pre code {{ display: block; background: none; border: 0; padding: 0; font-size: 8.2pt;
  line-height: 1.4; white-space: pre-wrap; overflow-wrap: anywhere; }}
pre.long {{ break-inside: auto; page-break-inside: auto; }}
.codehilite .k, .codehilite .kd, .codehilite .kt {{ color: {t.accent}; font-weight: 600; }}
.codehilite .nb, .codehilite .nf {{ color: {t.heading}; }}
.codehilite .s, .codehilite .s1, .codehilite .s2 {{ color: {t.link}; }}
.codehilite .c, .codehilite .c1, .codehilite .cm {{ color: {t.ink_soft}; font-style: italic; }}
.codehilite .o, .codehilite .p {{ color: {t.ink}; }}
.codehilite .m, .codehilite .mi, .codehilite .mf {{ color: {t.heading}; }}

/* --- figures --------------------------------------------------------- */
figure.fig {{ margin: 1em 0 1.2em; text-align: center;
  break-inside: avoid; page-break-inside: avoid; }}
figure.fig svg {{ max-width: 100%; height: auto; }}
figcaption {{ margin-top: .5em; font-size: 8.3pt; color: {t.ink_soft}; text-align: center;
  font-family: {s.head_font}; }}
/* A plate owns its page. Do NOT add height:100% here - it resolves against
   the body box, not the page box, and pushes a blank page ahead of each plate. */
figure.plate {{ break-before: page; page-break-before: always;
  break-after: page; page-break-after: always;
  break-inside: avoid; page-break-inside: avoid;
  display: block; text-align: center; margin: 0 auto; }}
figure.plate img, figure.plate svg {{ display: block; margin: 0 auto; }}
figure.tile-set {{ margin: 0; }}
.tile-set > figcaption {{ break-after: auto; margin: .5em 0; text-align: left; }}
.tile-sheet {{ break-before: page; break-after: page; break-inside: avoid; margin: 0;
  position: relative; overflow: hidden; width: {sheet_w}px; height: {sheet_h}px; }}
.tile-window {{ overflow: hidden; position: absolute; }}
.tile-art {{ position: absolute; }}
.tile-marks {{ position: absolute; left: 0; top: 0; }}
.tile-label {{ position: absolute; margin: 0; display: flex; align-items: center;
  justify-content: space-between; gap: 1.5em; white-space: nowrap; line-height: 1;
  font-family: {s.head_font}; font-size: 7pt; color: {t.ink_soft}; }}
.tile-name {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; }}
.tile-where {{ flex: none; }}
.tile-art > :is(svg, img) {{ max-width: none !important; margin: 0; border: 0;
  border-radius: 0; display: block; }}
/* The hairline frame sits outside the picture (content-box), so an image placed
   at a given scale prints at exactly that scale. The planner reserves the
   frame's width on every side, so the framed figure still fits its page. An
   outline would not take room, and a page clips it where the figure meets the
   edge of the printable area. The width is the 1 px Chromium draws for any
   thinner border. */
img {{ box-sizing: content-box; max-width: calc(100% - {2 * IMAGE_FRAME_PX:g}px);
  height: auto; display: block; margin: .8em auto;
  border: {IMAGE_FRAME_PX:g}px solid {t.rule}; border-radius: 2px; }}
.fig-missing, .img-missing {{ display: block; border: .8pt dashed {WARN_BORDER};
  background: {WARN_BG}; color: {WARN_TEXT}; padding: .6em .8em; font-size: 8.8pt;
  border-radius: 3px; margin: .6em 0; }}
.img-missing code {{ background: none; border: 0; color: {WARN_TEXT}; }}
"""


def _template_fonts(s: Settings) -> str:
    """Bundled faces for the running header and footer.

    Chromium renders these templates in their own document, which does not
    inherit the page's @font-face rules, so without this a bundled face falls
    back to an installed one. Braces are doubled for the str.format call that
    fills the template.
    """
    rules = font_css(s.head_font)
    return f"<style>{rules}</style>".replace("{", "{{").replace("}", "}}") if rules else ""


def header_template(s: Settings) -> str:
    return _template_fonts(s) + (
        f'<div style="font-family:{_plain(s.head_font)};font-size:7pt;'
        f"color:{s.theme.ink_soft};width:100%;padding:0 15mm;display:flex;"
        'justify-content:space-between;"><span>{title}</span><span>{version}</span></div>'
    )


def footer_template(s: Settings, *, include_total: bool = True) -> str:
    page = '<span class="pageNumber"></span>'
    if include_total:
        page += ' of <span class="totalPages"></span>'
    return _template_fonts(s) + (
        f'<div style="font-family:{_plain(s.head_font)};font-size:7pt;'
        f"color:{s.theme.ink_soft};width:100%;padding:0 15mm;display:flex;"
        f'justify-content:space-between;"><span>{{sub}}</span><span>Page {page}</span></div>'
    )


def _plain(font_stack: str) -> str:
    """Chromium's header/footer templates sit in an attribute; drop the quotes."""
    return font_stack.replace('"', "").replace("'", "")
