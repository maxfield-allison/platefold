"""Interface localization without coupling UI language to document language."""

from __future__ import annotations

import json
import locale as system_locale
import os
import re
import string
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from importlib import resources
from typing import Any

from babel import Locale
from babel.core import UnknownLocaleError
from babel.numbers import format_decimal

DEFAULT_LOCALE = "en"
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*$")


def _catalogs() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    root = resources.files("platefold.locales")
    for path in root.iterdir():
        if path.name.endswith(".json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1 or not isinstance(data.get("messages"), dict):
                raise ValueError(f"Invalid interface catalog: {path.name}")
            found[str(data["locale"])] = data
    return found


@cache
def _english_literals() -> dict[str, str]:
    messages = _catalogs()[DEFAULT_LOCALE]["messages"]
    return {value: key for key, value in messages.items() if isinstance(value, str)}


def requested_locale(explicit: str | None = None) -> str:
    """Return the requested interface locale, before supported-catalog fallback."""
    if explicit:
        return explicit
    if value := os.environ.get("PLATEFOLD_UI_LOCALE"):
        return value
    value = system_locale.getlocale()[0]
    return value or DEFAULT_LOCALE


def resolve_locale(requested: str | None, available: set[str] | None = None) -> str:
    """Match a BCP-47-like request to a shipped catalog, ending at English."""
    choices = available if available is not None else set(_catalogs())
    raw = (requested or DEFAULT_LOCALE).strip()
    if not _LOCALE_RE.fullmatch(raw):
        return DEFAULT_LOCALE
    normalized = raw.replace("_", "-")
    by_casefold = {choice.casefold(): choice for choice in choices}
    for candidate in (normalized, normalized.split("-", 1)[0], DEFAULT_LOCALE):
        if match := by_casefold.get(candidate.casefold()):
            return match
    return DEFAULT_LOCALE


def _babel_locale(locale: str) -> Locale:
    try:
        return Locale.parse(locale, sep="-")
    except (UnknownLocaleError, ValueError):
        return Locale(DEFAULT_LOCALE)


def _placeholders(template: str) -> set[str]:
    return {
        name
        for _literal, name, _spec, _conversion in string.Formatter().parse(template)
        if name is not None
    }


@dataclass(frozen=True)
class Translator:
    locale: str
    direction: str
    messages: dict[str, str | dict[str, str]]

    @classmethod
    def create(cls, requested: str | None = None) -> Translator:
        catalogs = _catalogs()
        locale = resolve_locale(requested_locale(requested), set(catalogs))
        catalog = catalogs[locale]
        return cls(locale, str(catalog["direction"]), dict(catalog["messages"]))

    @classmethod
    def from_catalog(cls, catalog: dict[str, Any]) -> Translator:
        """Construct a translator for validation and mechanical test catalogs."""
        return cls(str(catalog["locale"]), str(catalog["direction"]), dict(catalog["messages"]))

    def text(self, message_id: str, /, **values: object) -> str:
        entry = self.messages.get(message_id)
        if not isinstance(entry, str):
            fallback = self.messages.get("i18n.unknown_message", "Missing interface message: {id}")
            if not isinstance(fallback, str):
                fallback = "Missing interface message: {id}"
            return fallback.format(id=message_id)
        return self._format(entry, values)

    def plural(self, message_id: str, count: int | float | Decimal, /, **values: object) -> str:
        entry = self.messages.get(message_id)
        if not isinstance(entry, dict):
            return self.text(message_id, count=count, **values)
        category = str(_babel_locale(self.locale).plural_form(count))
        template = entry.get(category) or entry.get("other")
        if not isinstance(template, str):
            return self.text("i18n.unknown_message", id=message_id)
        return self._format(template, {"count": self.number(count), **values})

    def number(self, value: int | float | Decimal, format: str | None = None) -> str:
        return format_decimal(value, format=format, locale=_babel_locale(self.locale))

    def literal(self, english: str) -> tuple[str | None, str]:
        """Localize an exact adapter fallback and retain its stable ID when known."""
        message_id = _english_literals().get(english)
        return (message_id, self.text(message_id)) if message_id else (None, english)

    def exception(self, error: Exception) -> tuple[str | None, str]:
        """Localize a stable coded failure, then fall back to an exact literal."""
        code = getattr(error, "code", None)
        message_id = f"error.{code}" if isinstance(code, str) else None
        if message_id and isinstance(self.messages.get(message_id), str):
            return message_id, self.text(message_id)
        return self.literal(str(error))

    @staticmethod
    def _format(template: str, values: dict[str, object]) -> str:
        expected = _placeholders(template)
        missing = expected - values.keys()
        if missing:
            raise ValueError(f"Missing interface placeholders: {', '.join(sorted(missing))}")
        return template.format_map({name: str(values[name]) for name in expected})


def translator(requested: str | None = None) -> Translator:
    return Translator.create(requested)
