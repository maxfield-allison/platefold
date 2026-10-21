"""WCAG 2.1 contrast arithmetic and the colour pairings a theme is held to.

The pairings below are the ones the stylesheet and the Mermaid configuration
actually draw, named by the token that paints the foreground and the token
that paints its ground. Text pairings need 4.5:1, large text and informative
graphics 3.0:1 (WCAG 1.4.3 and 1.4.11). Pairings declared decorative are
reported with their ratio but never scored, so an exemption stays visible
instead of quietly counting as a pass.

This module has no imports from the rest of the package so that theme loading,
the desktop catalog and the test-suite can all share one rule table.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

TEXT_MINIMUM = 4.5
GRAPHIC_MINIMUM = 3.0

# The "this went wrong" callout keeps its own colours whatever the palette, so a
# missing image or failed diagram stays visible. They live here, beside the rule
# that checks them against every page colour.
CALLOUT_BG = "#fdf8e8"
CALLOUT_TEXT = "#6b5615"


@dataclass(frozen=True)
class ContrastRule:
    fg: str  # token name, or a literal "#rrggbb" for fixed colours
    bg: str  # token name, or a literal "#rrggbb"
    minimum: float  # 0 for decorative pairings, which are reported but not scored
    kind: str  # text | large-text | graphic | callout | decorative
    label: str  # where the pairing is drawn, in plain language
    # A callout is delineated by whichever of its border or its fill stands out
    # from the page; the alternate pairing may carry the minimum instead.
    alternate: tuple[str, str] | None = None


RULES: tuple[ContrastRule, ...] = (
    ContrastRule("ink", "page", TEXT_MINIMUM, "text", "Body text on the page"),
    ContrastRule("ink_strong", "page", TEXT_MINIMUM, "text", "Bold text on the page"),
    ContrastRule(
        "ink_soft",
        "page",
        TEXT_MINIMUM,
        "text",
        "Captions, metadata, running header and h4 headings on the page",
    ),
    ContrastRule("heading", "page", TEXT_MINIMUM, "text", "Section headings on the page"),
    ContrastRule("link", "page", TEXT_MINIMUM, "text", "Links on the page"),
    ContrastRule(
        "title", "page", GRAPHIC_MINIMUM, "large-text", "Document title and h1 on the page"
    ),
    ContrastRule("ink_strong", "surface", TEXT_MINIMUM, "text", "Table header text on its fill"),
    ContrastRule("ink", "surface", TEXT_MINIMUM, "text", "Inline code on its chip"),
    ContrastRule(
        "ink", "surface_alt", TEXT_MINIMUM, "text", "Code blocks and zebra rows on their fill"
    ),
    ContrastRule(
        "accent", "surface_alt", TEXT_MINIMUM, "text", "Code keywords on the code block fill"
    ),
    ContrastRule(
        "link", "surface_alt", TEXT_MINIMUM, "text", "Code strings on the code block fill"
    ),
    ContrastRule(
        "ink_soft",
        "surface_alt",
        TEXT_MINIMUM,
        "text",
        "Blockquotes and code comments on their fill",
    ),
    ContrastRule(
        "diagram_text", "diagram_fill", TEXT_MINIMUM, "text", "Diagram labels on node fill"
    ),
    ContrastRule(
        "diagram_text",
        "diagram_alt_fill",
        TEXT_MINIMUM,
        "text",
        "Diagram labels on cluster and secondary fill",
    ),
    ContrastRule(
        "diagram_text", "diagram_note_fill", TEXT_MINIMUM, "text", "Note text on note fill"
    ),
    ContrastRule("diagram_text", "page", TEXT_MINIMUM, "text", "Edge labels on the page"),
    ContrastRule(
        "accent",
        "page",
        GRAPHIC_MINIMUM,
        "graphic",
        "Title underline, header rule and code spine on the page",
    ),
    ContrastRule(
        "diagram_stroke",
        "page",
        GRAPHIC_MINIMUM,
        "graphic",
        "Diagram node borders and edges on the page",
    ),
    ContrastRule(
        CALLOUT_TEXT,
        "page",
        GRAPHIC_MINIMUM,
        "callout",
        "Warning callout border or fill against the page",
        alternate=(CALLOUT_BG, "page"),
    ),
    # Declared decorative: each sits beside a fill or text that carries the same
    # information, so the hairline itself is not required to read the document.
    ContrastRule(
        "accent_soft",
        "page",
        0,
        "decorative",
        "Blockquote bar, table header underline and contents leader dots",
    ),
    ContrastRule("rule", "page", 0, "decorative", "Table hairlines and container borders"),
    ContrastRule(
        "diagram_note_stroke", "page", 0, "decorative", "Note borders beside the note fill"
    ),
)

# Tokens that only ever paint a ground. They have no verdict of their own; the
# text drawn on them is what gets scored.
FILL_TOKENS = frozenset(
    {"page", "surface", "surface_alt", "diagram_fill", "diagram_alt_fill", "diagram_note_fill"}
)


def relative_luminance(colour: str) -> float:
    """WCAG 2.1 relative luminance of a #rrggbb colour."""
    if not HEX.match(colour):
        raise ValueError(f"{colour!r} is not a #rrggbb colour")
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(a: str, b: str) -> float:
    """Contrast ratio between two colours, from 1.0 to 21.0."""
    low, high = sorted((relative_luminance(a), relative_luminance(b)))
    return (high + 0.05) / (low + 0.05)


def _colour(colors: Mapping[str, str], token: str) -> str:
    return token if token.startswith("#") else colors[token]


def contrast_report(colors: Mapping[str, str]) -> list[dict]:
    """Evaluate every rule against one palette.

    Each entry carries the pairing, the exact ratio, the minimum it is held to and
    a verdict: True, False, or None for decorative pairings that are reported only.
    """
    report = []
    for rule in RULES:
        ratio = contrast_ratio(_colour(colors, rule.fg), _colour(colors, rule.bg))
        alternate = None
        if rule.alternate:
            alternate = contrast_ratio(
                _colour(colors, rule.alternate[0]), _colour(colors, rule.alternate[1])
            )
        effective = max(ratio, alternate) if alternate is not None else ratio
        passes = None if rule.kind == "decorative" else effective >= rule.minimum
        report.append(
            {
                **asdict(rule),
                "ratio": round(ratio, 2),
                "alternate_ratio": None if alternate is None else round(alternate, 2),
                "passes": passes,
            }
        )
    return report


def contrast_failures(colors: Mapping[str, str]) -> list[dict]:
    return [entry for entry in contrast_report(colors) if entry["passes"] is False]


def describe_failure(entry: dict) -> str:
    fg = entry["fg"] if not entry["fg"].startswith("#") else "the warning callout"
    return (
        f"{fg} on {entry['bg']} is {entry['ratio']:.2f}:1; "
        f"{entry['label'].lower()} needs {entry['minimum']:g}:1"
    )
