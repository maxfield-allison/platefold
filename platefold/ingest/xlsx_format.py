"""Render XLSX cell values through their Excel number-format codes.

Excel stores a number and a format code separately, and the code decides what
the reader sees: `000000` turns 123 into 000123, `# ?/?` turns 0.25 into 1/4
and `0.0,," million"` turns 1250000 into 1.3 million. Showing the stored
number instead, or rounding it under a misread code, prints a different value
from the one the author saw.

This module parses the parts of the grammar that decide the visible value:
up to four `;`-separated sections (positive, negative, zero, text), simple
`[condition]` sections, digit placeholders `0 # ?`, thousands separators,
scaling commas, percent, scientific notation, fractions, quoted and escaped
literals, `@`, `General`, and currency or locale brackets such as `[$€-407]`.
Colour brackets, `_x` spacing and `*x` fill only affect presentation.

It never guesses. For grammar it does not model, such as date and time
directives, elapsed-time brackets or engineering notation, the formatter
returns ``None`` so that the caller can show the stored value unrounded and
report the format as unsupported.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from fractions import Fraction
from functools import lru_cache

# Excel refuses codes longer than 255 characters. The bound also keeps the
# decimal arithmetic below inside a fixed precision.
_MAX_CODE = 255
_PRECISION = Context(prec=1200, rounding=ROUND_HALF_UP, Emax=999_999, Emin=-999_999)
_LEXEME = re.compile(
    r'"(?P<quoted>[^"]*)"'
    r"|\\(?P<escaped>.)"
    r"|_(?P<space>.)"
    r"|\*(?P<fill>.)"
    r"|(?P<bracket>\[[^\]]*\])"
    r"|(?P<general>[Gg][Ee][Nn][Ee][Rr][Aa][Ll])"
    r"|(?P<exp>[Ee][+-])"
    r"|(?P<char>.)",
    re.S,
)
_CONDITION = re.compile(r"\[(<=|>=|<>|<|>|=)\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\]")
_CURRENCY = re.compile(r"\[\$([^\-\]]*)(?:-([0-9A-Fa-f]{1,8}))?\]")
_COLOUR = re.compile(
    r"\[(?:Black|Blue|Cyan|Green|Magenta|Red|White|Yellow|Color\s*\d{1,2})\]", re.I
)
# Locale identifiers whose low word selects a system date or time layout.
_SYSTEM_LAYOUTS = frozenset({"F800", "F400"})
_CHARACTERS = {
    "0": "ph",
    "#": "ph",
    "?": "ph",
    ".": "dot",
    ",": "comma",
    "%": "percent",
    "/": "slash",
    "@": "at",
}
_PAD = {"0": "0", "?": " ", "#": ""}
_OPERATORS: dict[str, Callable[[Decimal, Decimal], bool]] = {
    "<": lambda value, limit: value < limit,
    "<=": lambda value, limit: value <= limit,
    ">": lambda value, limit: value > limit,
    ">=": lambda value, limit: value >= limit,
    "=": lambda value, limit: value == limit,
    "<>": lambda value, limit: value != limit,
}


class Unsupported(Exception):
    """The code uses Excel format grammar this module does not render."""


@dataclass(frozen=True)
class _Token:
    kind: str  # lit, ph, digit, dot, comma, percent, slash, exp, at, general
    text: str = ""


Tokens = tuple[_Token, ...]


@dataclass(frozen=True)
class _Section:
    tokens: Tokens
    condition: tuple[str, Decimal] | None

    @property
    def has_text(self) -> bool:
        return any(token.kind == "at" for token in self.tokens)


class _SectionBuilder:
    def __init__(self) -> None:
        self.tokens: list[_Token] = []
        self.condition: tuple[str, Decimal] | None = None

    def bracket(self, text: str) -> None:
        if _COLOUR.fullmatch(text):
            return
        if condition := _CONDITION.fullmatch(text):
            if self.condition is not None:
                raise Unsupported("more than one condition in a section")
            self.condition = condition.group(1), Decimal(condition.group(2))
            return
        currency = _CURRENCY.fullmatch(text)
        if currency is None:
            raise Unsupported(f"bracket {text}")
        symbol, locale = currency.groups()
        if locale and locale.upper()[-4:] in _SYSTEM_LAYOUTS:
            raise Unsupported("system date or time layout")
        if symbol:
            self.tokens.append(_Token("lit", symbol))

    def char(self, char: str) -> None:
        if char in '"[\\_*':
            raise Unsupported("unterminated literal, escape or bracket")
        if kind := _CHARACTERS.get(char):
            self.tokens.append(_Token(kind, char))
        elif char in "123456789":
            self.tokens.append(_Token("digit", char))
        elif char.isascii() and char.isalpha():
            # Unquoted letters are date, time, era or locale directives.
            raise Unsupported(f"directive {char}")
        else:
            self.tokens.append(_Token("lit", char))

    def add(self, match: re.Match[str]) -> None:
        kind, text = match.lastgroup, match.group(match.lastgroup or 0)
        if kind in {"quoted", "escaped", "space"}:
            self.tokens.append(_Token("lit", " " if kind == "space" else text))
        elif kind == "fill":
            # A fill repeats its character across the column. One space keeps
            # the separation a space fill gives; other fills add no value.
            if text == " ":
                self.tokens.append(_Token("lit", " "))
        elif kind == "bracket":
            self.bracket(text)
        elif kind == "general":
            self.tokens.append(_Token("general"))
        elif kind == "exp":
            self.tokens.append(_Token("exp", text[1]))
        else:
            self.char(text)

    def build(self) -> _Section:
        return _Section(tuple(self.tokens), self.condition)


def _raw_sections(code: str) -> list[list[re.Match[str]]]:
    """Split a code into sections on unquoted, unescaped semicolons."""
    if len(code) > _MAX_CODE:
        raise Unsupported("format code is longer than Excel allows")
    sections: list[list[re.Match[str]]] = [[]]
    for match in _LEXEME.finditer(code):
        if match.lastgroup == "char" and match.group() == ";":
            sections.append([])
        else:
            sections[-1].append(match)
    if len(sections) > 4:
        raise Unsupported("more than four sections")
    return sections


def _is_text_section(sections: list[list[re.Match[str]]]) -> bool:
    """Excel applies the fourth section, or a last section holding `@`, to text."""
    last = sections[-1]
    return len(sections) == 4 or any(
        match.lastgroup == "char" and match.group() == "@" for match in last
    )


def _build(matches: list[re.Match[str]]) -> _Section:
    builder = _SectionBuilder()
    for match in matches:
        builder.add(match)
    return builder.build()


@lru_cache(maxsize=512)
def _numeric_sections(code: str) -> tuple[_Section, ...]:
    """Parse the sections Excel applies to numbers; a text section is skipped."""
    raw = _raw_sections(code)
    if _is_text_section(raw):
        raw.pop()
    sections = tuple(_build(matches) for matches in raw)
    if any(section.condition for section in sections[2:]):
        raise Unsupported("condition outside the first two sections")
    if any(section.has_text for section in sections):
        raise Unsupported("text placeholder in a numeric section")
    return sections


@lru_cache(maxsize=512)
def _text_section(code: str) -> _Section | None:
    """Parse only the text section; numeric sections never affect a text cell."""
    raw = _raw_sections(code)
    return _build(raw[-1]) if _is_text_section(raw) else None


def _choose(code: str, value: Decimal) -> tuple[_Section, bool]:
    """Return the section Excel applies and whether it shows a minus sign."""
    sections = list(_numeric_sections(code))
    if not sections:
        # A code with only a text section shows numbers as General.
        return _Section((_Token("general"),), None), value < 0
    first = sections[0].condition
    second = sections[1].condition if len(sections) > 1 else None
    if first is None and second is None:
        if value < 0 and len(sections) > 1:
            return sections[1], False
        if value == 0 and len(sections) > 2:
            return sections[2], False
        return sections[0], value < 0
    if value < 0:
        # Excel's sign handling inside conditional sections is not modelled.
        raise Unsupported("negative value under a conditional format")
    for section, condition in zip(sections, (first, second), strict=False):
        if condition is not None and _OPERATORS[condition[0]](value, condition[1]):
            return section, False
    if len(sections) > 2 and first is not None and second is not None:
        return sections[2], False
    fallback = sections[1] if len(sections) > 1 else sections[0]
    if fallback.condition is not None:
        raise Unsupported("no section matches the value")
    return fallback, False


def _decimal(value: int | float) -> Decimal:
    if isinstance(value, int):
        return Decimal(value)
    number = Decimal(repr(value))
    # Excel keeps fifteen significant digits of a stored number.
    if number and len(number.as_tuple().digits) > 15:
        number = number.quantize(Decimal(1).scaleb(number.adjusted() - 14))
    return number


def general(value: int | float) -> str:
    """Show a stored number without rounding it, as Excel's General does."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def _general_decimal(value: Decimal) -> str:
    if value == value.to_integral_value() and value < Decimal("1e15"):
        return str(int(value))
    return str(value.normalize())


def _slots(tokens: Tokens, start: int, stop: int) -> list[int]:
    return [index for index in range(start, stop) if tokens[index].kind == "ph"]


def _integer_digits(placeholders: list[str], digits: str) -> list[str]:
    """Distribute integer digits right to left; the leftmost takes the rest."""
    result = [""] * len(placeholders)
    remaining = digits
    for position in range(len(placeholders) - 1, 0, -1):
        result[position] = remaining[-1] if remaining else _PAD[placeholders[position]]
        remaining = remaining[:-1]
    if placeholders:
        result[0] = remaining or _PAD[placeholders[0]]
    return result


def _fraction_digits(placeholders: list[str], digits: str) -> list[str]:
    """Show decimals; trailing zeros under `#` vanish and under `?` become spaces."""
    result = list(digits)
    for position in range(len(result) - 1, -1, -1):
        if result[position] != "0" or placeholders[position] == "0":
            break
        result[position] = _PAD[placeholders[position]]
    return result


def _group(text: str) -> str:
    digits = [index for index, char in enumerate(text) if char.isdigit()]
    chars = list(text)
    for count, index in enumerate(reversed(digits)):
        if count and count % 3 == 0:
            chars[index] += ","
    return "".join(chars)


def _emit(tokens: Tokens, replacements: dict[int, str]) -> str:
    """Join a section, substituting rendered digits and dropping directives."""
    output: list[str] = []
    for index, token in enumerate(tokens):
        if index in replacements:
            output.append(replacements[index])
        elif token.kind in {"lit", "digit", "dot", "percent", "comma"}:
            output.append(token.text)
        elif token.kind != "ph":
            raise Unsupported(f"{token.kind} is not valid here")
    return "".join(output)


def _neighbour(tokens: Tokens, index: int, step: int) -> _Token | None:
    index += step
    while 0 <= index < len(tokens) and tokens[index].kind == "comma":
        index += step
    return tokens[index] if 0 <= index < len(tokens) else None


def _commas(tokens: Tokens, dot: int) -> tuple[set[int], set[int]]:
    """Split commas after a placeholder into thousands separators and scaling."""
    grouping: set[int] = set()
    scaling: set[int] = set()
    for index, token in enumerate(tokens):
        before = _neighbour(tokens, index, -1)
        if token.kind != "comma" or before is None or before.kind != "ph":
            continue  # not a comma, or a literal one
        after = _neighbour(tokens, index, 1)
        if after is not None and after.kind == "dot":
            # Implementations disagree on a scaling comma before the decimal point.
            raise Unsupported("scaling comma before the decimal point")
        if after is None or after.kind != "ph":
            scaling.add(index)
        elif index > dot:
            raise Unsupported("comma between decimal placeholders")
        else:
            grouping.add(index)
    return grouping, scaling


def _render_decimal(tokens: Tokens, number: Decimal) -> str:
    dots = [index for index, token in enumerate(tokens) if token.kind == "dot"]
    if len(dots) > 1:
        raise Unsupported("more than one decimal point")
    dot = dots[0] if dots else len(tokens)
    grouping, scaling = _commas(tokens, dot)
    percents = sum(token.kind == "percent" for token in tokens)
    if percents > 1:
        # Whether each sign multiplies by a hundred again is not settled.
        raise Unsupported("more than one percent sign")
    number *= Decimal(100) ** percents
    number /= Decimal(1000) ** len(scaling)
    integer_slots = _slots(tokens, 0, dot)
    fraction_slots = _slots(tokens, dot + 1, len(tokens))
    rounded = number.quantize(Decimal(1).scaleb(-len(fraction_slots)))
    whole, _, decimals = format(rounded, "f").partition(".")
    whole = whole.lstrip("0")
    integer_text = _integer_digits([tokens[i].text for i in integer_slots], whole)
    if grouping:
        span = range(integer_slots[0], integer_slots[-1] + 1)
        if any(tokens[i].kind not in {"ph", "comma"} for i in span):
            raise Unsupported("thousands separator between literal text")
        integer_text = [_group("".join(integer_text))] + [""] * (len(integer_slots) - 1)
    replacements = dict.fromkeys(grouping | scaling, "")
    replacements.update(zip(integer_slots, integer_text, strict=True))
    fraction_text = _fraction_digits([tokens[i].text for i in fraction_slots], decimals)
    replacements.update(zip(fraction_slots, fraction_text, strict=True))
    if dots and not integer_slots:
        replacements[dot] = whole + "."
    return _emit(tokens, replacements)


def _scientific_parts(tokens: Tokens) -> tuple[int, int | None, list[int], list[int]]:
    exp_at = next(index for index, token in enumerate(tokens) if token.kind == "exp")
    if any(token.kind in {"comma", "percent", "slash", "general"} for token in tokens):
        raise Unsupported("scientific notation with separators, scaling or fractions")
    if sum(token.kind == "exp" for token in tokens) > 1:
        raise Unsupported("more than one exponent")
    dots = [index for index, token in enumerate(tokens) if token.kind == "dot"]
    if len(dots) > 1 or (dots and dots[0] > exp_at):
        raise Unsupported("misplaced decimal point in scientific notation")
    dot = dots[0] if dots else None
    integer_slots = _slots(tokens, 0, exp_at if dot is None else dot)
    exponent_slots = _slots(tokens, exp_at + 1, len(tokens))
    if len(integer_slots) != 1 or not exponent_slots:
        raise Unsupported("engineering or multi-digit scientific notation")
    return exp_at, dot, integer_slots, exponent_slots


def _render_scientific(tokens: Tokens, number: Decimal) -> str:
    exp_at, dot, integer_slots, exponent_slots = _scientific_parts(tokens)
    fraction_slots = [] if dot is None else _slots(tokens, dot + 1, exp_at)
    quantum = Decimal(1).scaleb(-len(fraction_slots))
    exponent = number.adjusted() if number else 0
    mantissa = number.scaleb(-exponent).quantize(quantum)
    if mantissa >= 10:
        exponent += 1
        mantissa = number.scaleb(-exponent).quantize(quantum)
    whole, _, decimals = format(mantissa, "f").partition(".")
    integer_text = _integer_digits([tokens[integer_slots[0]].text], whole.lstrip("0"))
    replacements = dict(zip(integer_slots, integer_text, strict=True))
    fraction_text = _fraction_digits([tokens[i].text for i in fraction_slots], decimals)
    replacements.update(zip(fraction_slots, fraction_text, strict=True))
    minimum = sum(tokens[i].text == "0" for i in exponent_slots)
    sign = "-" if exponent < 0 else ("+" if tokens[exp_at].text == "+" else "")
    replacements[exp_at] = "E" + sign + str(abs(exponent)).rjust(minimum, "0")
    return _emit(tokens, replacements)


@dataclass(frozen=True)
class _FractionLayout:
    integer_slots: list[int]
    numerator: range
    denominator: range
    fixed: int | None


def _in_denominator(token: _Token, *, fixed: bool) -> bool:
    if fixed:
        return token.kind == "digit" or token.text == "0"
    return token.kind == "ph"


def _fraction_layout(tokens: Tokens, slash: int) -> _FractionLayout:
    if any(token.kind in {"dot", "comma", "percent", "general"} for token in tokens):
        raise Unsupported("fraction with decimals, separators or scaling")
    start = slash
    while start > 0 and tokens[start - 1].kind == "ph":
        start -= 1
    fixed = tokens[slash + 1].kind == "digit"
    end = slash + 1
    while end < len(tokens) and _in_denominator(tokens[end], fixed=fixed):
        end += 1
    integer_slots = _slots(tokens, 0, start)
    if integer_slots and integer_slots != list(range(integer_slots[0], integer_slots[-1] + 1)):
        raise Unsupported("whole-number placeholders split by literal text")
    if any(token.kind not in {"lit", "digit"} for token in tokens[end:]):
        raise Unsupported("directives after a fraction")
    denominator = range(slash + 1, end)
    value = int("".join(tokens[i].text for i in denominator)) if fixed else None
    return _FractionLayout(integer_slots, range(start, slash), denominator, value)


def _fraction_value(layout: _FractionLayout, number: Decimal) -> tuple[int, int, int]:
    """Return the whole part, numerator and denominator Excel would show."""
    exact = Fraction(number)
    whole = int(exact) if layout.integer_slots else 0
    part = exact - whole
    if layout.fixed is not None:
        denominator = layout.fixed
        numerator = int((Decimal(part.numerator) * denominator / part.denominator).quantize(1))
    else:
        limited = part.limit_denominator(10 ** len(layout.denominator) - 1)
        numerator, denominator = limited.numerator, limited.denominator
    if layout.integer_slots and numerator == denominator:
        whole, numerator = whole + 1, 0
    return whole, numerator, denominator


def _render_fraction(tokens: Tokens, number: Decimal, slash: int) -> str:
    layout = _fraction_layout(tokens, slash)
    whole, numerator, denominator = _fraction_value(layout, number)
    replacements: dict[int, str] = {}
    if layout.integer_slots:
        zero = not numerator or any(tokens[i].text == "0" for i in layout.integer_slots)
        replacements.update(dict.fromkeys(layout.integer_slots, ""))
        replacements[layout.integer_slots[0]] = str(whole) if whole or zero else ""
        if not numerator:
            # A whole number shows no fraction, only the space it would take.
            first = layout.integer_slots[-1] + 1
            replacements.update(dict.fromkeys(range(first, layout.denominator.stop), ""))
            return _emit(tokens, replacements)
    marks = [tokens[i].text for i in layout.numerator]
    padding = marks[: max(0, len(marks) - len(str(numerator)))]
    replacements.update(dict.fromkeys(layout.numerator, ""))
    replacements[layout.numerator.start] = "".join(_PAD[item] for item in padding) + str(numerator)
    replacements[slash] = "/"
    replacements.update(dict.fromkeys(layout.denominator, ""))
    shown = str(denominator)
    if layout.fixed is None:
        # Excel aligns a variable denominator to the left and pads it with spaces.
        marks = [tokens[i].text for i in layout.denominator][len(shown) :]
        shown += "".join(" " if item == "?" else "" for item in marks)
    replacements[layout.denominator.start] = shown
    return _emit(tokens, replacements)


def _render_number(section: _Section, number: Decimal) -> str:
    tokens = section.tokens
    if any(token.kind == "general" for token in tokens):
        if any(token.kind not in {"general", "lit", "digit"} for token in tokens):
            raise Unsupported("General combined with placeholders")
        general_text = _general_decimal(number)
        return "".join(general_text if token.kind == "general" else token.text for token in tokens)
    if any(token.kind == "exp" for token in tokens):
        return _render_scientific(tokens, number)
    for index in range(1, len(tokens) - 1):
        if (
            tokens[index].kind == "slash"
            and tokens[index - 1].kind == "ph"
            and tokens[index + 1].kind in {"ph", "digit"}
        ):
            return _render_fraction(tokens, number, index)
    return _render_decimal(tokens, number)


def format_number(value: int | float, code: str) -> str | None:
    """Return Excel's display text for one number, or None if unsupported."""
    try:
        number = _decimal(value)
        section, signed = _choose(code, number)
        with localcontext(_PRECISION):
            rendered = _render_number(section, abs(number)).strip(" ")
    except (Unsupported, ArithmeticError, ValueError, IndexError):
        return None
    return f"-{rendered}" if signed and rendered else rendered


def format_text(text: str, code: str) -> str | None:
    """Apply a code's text section to a string cell, or None if unsupported."""
    try:
        section = _text_section(code)
    except (Unsupported, ArithmeticError, ValueError, IndexError):
        return None
    if section is None:
        # Without a text section Excel shows text as typed, whatever the code.
        return text
    output: list[str] = []
    for token in section.tokens:
        if token.kind == "at":
            output.append(text)
        elif token.kind in {"lit", "digit"}:
            output.append(token.text)
        else:
            return None
    return "".join(output).strip(" ")
