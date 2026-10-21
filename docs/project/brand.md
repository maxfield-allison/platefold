# Brand guide

How Plate|Fold looks, and the rules that keep it that way. The artwork and stylesheet described here live in the repository under `docs/assets/brand/` and `docs/stylesheets/`, and a test checks the colours in them.

## The name

**Plate|Fold** in prose, titles and artwork: a capital P, a capital F, and a vertical bar with no space on either side. **`platefold`** everywhere a bar can't go: the command, the Python package, file names and project paths.

The name is two printing terms. A **plate** is a page given over to one figure. A **fold-out** is a page larger than the book it's bound into.

The wordmark is never split across lines, hyphenated or set in two colours.

## The idea everything comes from

Plate|Fold measures each figure and walks a ladder, stopping at the first page that holds it near full size:

text column → Letter landscape → A3 landscape → custom page

The mark and the documentation diagrams both draw that ladder.

## Taglines

| Use | Text |
| --- | --- |
| Canonical | Documents to PDF with every figure on a page that fits |
| Long | Documents to PDF with every figure on a page that fits. Vector Mermaid, embedded images, and oversized figures on their own correctly sized pages. |
| Positioning | The exporter that measures before it prints. |
| Pills | vector Mermaid · per-figure pages · offline exports |

Taglines and product copy don't use dashes. Every claim in a tagline or pill has to be true of the current build. Release badges, platform badges and accessibility badges don't appear on anything until the claim behind them is tested.

## The mark

<div class="grid" markdown>

![The Plate|Fold mark in bright inks](../assets/brand/mark-bright.svg){ width="96" .pf-on-navy }

![The Plate|Fold mark in deep inks](../assets/brand/mark-deep.svg){ width="96" .pf-on-paper }

![The Plate|Fold tile](../assets/brand/tile.svg){ width="96" }

</div>

**Two leaves on one crease.** The tall amber leaf is the body page and the wide ember leaf is the plate.

- Drawn on a 64 px grid with a 5.5 px stroke, round caps and round joins.
- **Bright** inks, `amber-400` and `ember-400`, on navy grounds. **Deep** inks, `amber-700` and `ember-700`, on paper.
- A one-colour **mono** version uses `currentColor` and takes the colour of the text around it.
- **Minimum size 20 px.** Below that the stroked crease closes up, so use the **tile**: a navy rounded square with solid leaves. The tile is also the application icon and the favicon.
- **Clear space** on every side equals the mark's full height. Nothing sits inside it.
- The gap between mark and wordmark in a lockup is one third of the mark's height.

## Lockups

![Horizontal lockup in bright inks](../assets/brand/lockup-h-bright.svg){ width="320" .pf-on-navy }
![Horizontal lockup in deep inks](../assets/brand/lockup-h-deep.svg){ width="320" .pf-on-paper }

Horizontal, stacked and horizontal-with-tagline lockups each come in bright and deep versions. The wordmark is set in Sora 600 with −0.015 em tracking and converted to outlines, so the files don't depend on an installed font.

## Palette

**Eight tokens, and nothing outside them.**

| Token | Hex | Job |
| --- | --- | --- |
| <span class="pf-swatch" style="background:#0A1422"></span>`navy-950` | `#0A1422` | Deepest ground: dark site background, banners |
| <span class="pf-swatch" style="background:#0F1D31"></span>`navy-900` | `#0F1D31` | Ground: tile, code blocks on dark, text on paper |
| <span class="pf-swatch" style="background:#FAF7F2"></span>`paper-50` | `#FAF7F2` | Paper: light site background, text on navy |
| <span class="pf-swatch" style="background:#F2B457"></span>`amber-400` | `#F2B457` | Bright amber, dark grounds only |
| <span class="pf-swatch" style="background:#EF7B55"></span>`ember-400` | `#EF7B55` | Bright ember, dark grounds only |
| <span class="pf-swatch" style="background:#A9700E"></span>`amber-700` | `#A9700E` | Deep amber for graphics and large text on paper |
| <span class="pf-swatch" style="background:#B4482A"></span>`ember-700` | `#B4482A` | Deep ember for text and links on paper |
| <span class="pf-swatch" style="background:#845407"></span>`amber-800` | `#845407` | Amber body text on paper |

The two navies are shared with [dnsweaver](https://github.com/maxfield-allison/dnsweaver), which is what makes the two projects read as related. Plate|Fold turns dnsweaver's cyan and teal into amber and ember and keeps the structure the same.

### Why there are eight

Amber needs three steps. `amber-400` is for dark grounds. `amber-700` reaches 3.92:1 on paper, enough for graphics and large text but short of the 4.5:1 body text needs, and `amber-800` reaches 6.04:1 and carries text. A ninth token would need a job these eight don't do.

### Contrast, measured

WCAG 2.1 contrast ratios. 4.5:1 is the bar for body text, 3:1 for large text and graphics.

| Ink | On `paper-50` | On `navy-900` | On `navy-950` |
| --- | --- | --- | --- |
| `paper-50` | | 15.84 | 17.30 |
| `navy-900` | 15.84 | | |
| `amber-400` | 1.72 | 9.22 | 10.07 |
| `ember-400` | 2.57 | 6.15 | 6.72 |
| `amber-700` | 3.92 | 4.04 | 4.41 |
| `ember-700` | 5.04 | 3.14 | 3.43 |
| `amber-800` | 6.04 | 2.62 | 2.86 |

These ratios are for the brand palette on the site and artwork. They say nothing about the accessibility of the application or of exported PDFs.

### The palette is a test

`docs/stylesheets/brand.css` defines the eight tokens as custom properties. `tests/test_brand_assets.py` reads them, then reads every SVG under `docs/assets/brand/`, every stylesheet and the brand asset generator, and fails on any colour that isn't one of the eight. It names the file, the line and the colour.

A few things are allowed without being tokens, and the test lists them explicitly: `none`, `currentColor`, `transparent`, and a translucent version of a token such as `rgba(15, 29, 49, 0.1)`, which is the same ink at a lighter weight.

The rule covers the brand artwork and this site's own stylesheet, including code highlighting. Zensical's note, warning and tip boxes keep their default colours because those colours carry meaning.

Document themes are separate. A theme for exported PDFs has eighteen colour tokens of its own, because controlling body text, headings, tables, code and six diagram colours independently takes that many. The eight-token rule covers the brand only.

## Typography

| Role | Face | Notes |
| --- | --- | --- |
| Headings | Sora 600 | Tracking −0.015 em |
| Subheads, buttons | Sora 500 | |
| Body | Sora 400 | Line height 1.6 |
| Code, labels | JetBrains Mono 400 | |

Both faces are under the SIL Open Font License 1.1, and both are served from this site rather than a font service. The licence texts sit beside the font files.

## Print-shop motifs

Four devices borrowed from print production, each limited to one use.

**Crop marks.** Hairline corner ticks in `ember-400` on dark or `ember-700` on paper, set a few pixels in from the corner and never touching the edge. They frame a page, a hero image or the mark in a banner. They mean "this gets cut here", so they never go around a button, a card or body text.

**Fold lines.** A dashed line, three on and three off, only on a fold-out page. The mark's solid crease is the same idea drawn once. Never use a fold line as a divider.

**Plate numerals.** Plates in printed books are numbered in Roman capitals: Plate I, Plate II. In artwork, set them in JetBrains Mono small capitals, tracked +0.1 em. Exported PDFs don't number plates separately yet, so don't show this device as a feature of the output.

**Paper stocks.** New themes are named after a paper stock or a material, never a desktop environment or a vendor. Where themes are chosen, present them as a swatch book, the way a printer shows paper.

## Assets

Every file below is generated by `tools/build_brand_assets.py` from the palette and the mark geometry. Edit the generator, not the files, then run it and commit the result. `python tools/build_brand_assets.py --check` fails if the committed files don't match.

| File | Use |
| --- | --- |
| `mark-bright.svg`, `mark-deep.svg`, `mark-mono.svg` | The mark on dark grounds, on paper, and in one colour |
| `tile.svg`, `favicon.svg`, `favicon.ico` | Application icon and site favicon |
| `apple-touch-icon.png`, `icon-192.png`, `icon-512.png` | Home-screen and app icons |
| `lockup-h-*.svg`, `lockup-stack-*.svg`, `lockup-tagline-*.svg` | Mark and wordmark together, bright and deep |
| `hero-banner.svg`, `hero-banner.png` | Hero image for the README and announcements, 1280 × 400 |
| `og-image.png` | Link previews, 1200 × 630 |
| `social-banner.png` | Social profile banner, 1280 × 640 |
| `ladder-light.svg`, `ladder-dark.svg` | The page ladder, drawn to scale |

Sources for the raster-only images are in `docs/assets/brand/source/`.
