"""Standard sheets and a best-effort local paper suggestion; no location lookup."""

from __future__ import annotations

import locale
import re

# Millimetres, portrait. ANSI sizes use exact inch-to-mm conversion.
PAPER_SIZES = {
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
    "tabloid": (279.4, 431.8),
    "a5": (148.0, 210.0),
    "a4": (210.0, 297.0),
    "a3": (297.0, 420.0),
    "a2": (420.0, 594.0),
    "a1": (594.0, 841.0),
    "a0": (841.0, 1189.0),
    "ansi-c": (431.8, 558.8),
    "ansi-d": (558.8, 863.6),
    "ansi-e": (863.6, 1117.6),
}

# Unicode CLDR supplementalData.xml / measurementData, checked 2026-09-13.
# https://github.com/unicode-org/cldr/blob/main/common/supplemental/supplementalData.xml
LETTER_REGIONS = frozenset(
    ["BZ", "CA", "CL", "CO", "CR", "GT", "MX", "NI", "PA", "PH", "PR", "SV", "US", "VE"]
)

# Windows C-runtime locale strings can contain English country names instead
# of ISO region codes, for example English_United States.1252.
WINDOWS_LETTER_REGIONS = frozenset(
    {
        "belize",
        "canada",
        "chile",
        "colombia",
        "costa rica",
        "guatemala",
        "mexico",
        "nicaragua",
        "panama",
        "philippines",
        "puerto rico",
        "el salvador",
        "united states",
        "venezuela",
    }
)


def local_paper(locale_hint: str | None = None) -> str:
    """Use an explicit locale region; unknown or regionless locales fall back to A4.

    None reads the desktop/CLI process locale. HTTP callers supply their own
    locale (or an empty string), so the server's location never picks their paper.
    Language alone is deliberately not treated as a country.
    """
    if locale_hint is None:
        try:
            locale_hint = locale.getlocale()[0] or ""
        except (ValueError, TypeError):
            locale_hint = ""
    tag = re.split(r"[.@]", locale_hint)[0]
    if tag.partition("_")[2].casefold() in WINDOWS_LETTER_REGIONS:
        return "letter"
    tag = tag.replace("_", "-")
    for part in tag.split("-")[1:]:
        if len(part) == 1:  # BCP 47 extension, not a region
            break
        if len(part) == 2 and part.isalpha():
            return "letter" if part.upper() in LETTER_REGIONS else "a4"
    return "a4"


def margins(mode: str, side: float, bottom: float) -> tuple[float, float, float, float]:
    """Top, right, bottom, left in CSS order."""
    if mode == "none":
        return (0.0, 0.0, 0.0, 0.0)
    if mode == "bottom-right":
        return (0.0, side, bottom, 0.0)
    return (side, side, bottom, side)


def margin_css(values: tuple[float, float, float, float]) -> str:
    return " ".join(f"{value:g}mm" for value in values)
