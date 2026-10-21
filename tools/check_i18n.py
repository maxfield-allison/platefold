#!/usr/bin/env python3
"""Validate shipped interface catalogs and stable message references."""

from __future__ import annotations

import ast
import json
import re
import string
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOGS = ROOT / "platefold" / "locales"
PSEUDO_LOCALES = {"en-xa", "ar-xb"}
TS_REFERENCE = re.compile(r"\b(?:t|tOr|plural)\(\s*[\"']([a-z][a-z0-9_.-]+)[\"']")


def placeholders(template: str) -> Counter[str]:
    return Counter(
        name
        for _literal, name, _format, _conversion in string.Formatter().parse(template)
        if name is not None
    )


def forms(entry: object) -> list[str]:
    if isinstance(entry, str):
        return [entry]
    if (
        isinstance(entry, dict)
        and entry
        and all(isinstance(value, str) for value in entry.values())
    ):
        return list(entry.values())
    raise ValueError("messages must be strings or nonempty plural-form objects")


def python_references(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        name = (
            function.attr
            if isinstance(function, ast.Attribute)
            else function.id
            if isinstance(function, ast.Name)
            else ""
        )
        first = node.args[0]
        if (
            name in {"text", "plural"}
            and isinstance(first, ast.Constant)
            and isinstance(first.value, str)
        ):
            result.add(first.value)
    return result


def source_references() -> set[str]:
    references = set()
    for path in (ROOT / "platefold").rglob("*.py"):
        references.update(python_references(path))
    for path in (ROOT / "desktop" / "src").glob("*.ts"):
        references.update(TS_REFERENCE.findall(path.read_text(encoding="utf-8")))
    return references


def main() -> int:  # noqa: C901 - one pass reports all catalog contract failures together.
    problems: list[str] = []
    catalogs = []
    for path in sorted(CATALOGS.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{path.relative_to(ROOT)}: invalid UTF-8 JSON: {exc}")
            continue
        catalogs.append((path, data))
    english = next((data for _path, data in catalogs if data.get("locale") == "en"), None)
    if english is None:
        problems.append("platefold/locales/en.json: English fallback catalog is required")
    else:
        if english.get("translation_method") != "source":
            problems.append("platefold/locales/en.json: translation_method must be source")
        messages = english.get("messages")
        if not isinstance(messages, dict):
            problems.append("platefold/locales/en.json: messages must be an object")
        else:
            missing = source_references() - messages.keys()
            if missing:
                problems.append(
                    "English catalog is missing referenced IDs: " + ", ".join(sorted(missing))
                )
            for message_id, entry in messages.items():
                try:
                    variants = forms(entry)
                except ValueError as exc:
                    problems.append(f"en:{message_id}: {exc}")
                    continue
                expected = placeholders(variants[0])
                if any(placeholders(variant) != expected for variant in variants[1:]):
                    problems.append(f"en:{message_id}: plural forms use different placeholders")
            english_ids = set(messages)
            for path, catalog in catalogs:
                locale = str(catalog.get("locale", "")).replace("_", "-")
                label = path.relative_to(ROOT)
                if catalog.get("schema_version") != 1:
                    problems.append(f"{label}: schema_version must be 1")
                if catalog.get("direction") not in {"ltr", "rtl"}:
                    problems.append(f"{label}: direction must be ltr or rtl")
                if locale.casefold() in PSEUDO_LOCALES or locale.casefold().startswith("qps-"):
                    problems.append(f"{label}: pseudolocales are test data and must not ship")
                translated = catalog.get("messages")
                if not isinstance(translated, dict):
                    continue
                if locale != "en":
                    if catalog.get("translation_method") != "human":
                        problems.append(
                            f"{label}: non-English catalogs must declare human translation"
                        )
                    if not catalog.get("reviewed_by") or not catalog.get("reviewed_at"):
                        problems.append(f"{label}: human reviewer and review date are required")
                    missing = english_ids - translated.keys()
                    extra = translated.keys() - english_ids
                    if missing:
                        problems.append(f"{label}: missing IDs: {', '.join(sorted(missing))}")
                    if extra:
                        problems.append(f"{label}: unknown IDs: {', '.join(sorted(extra))}")
                    for message_id in english_ids & translated.keys():
                        try:
                            source_forms = forms(messages[message_id])
                            target_forms = forms(translated[message_id])
                        except ValueError as exc:
                            problems.append(f"{label}:{message_id}: {exc}")
                            continue
                        expected = placeholders(source_forms[0])
                        if any(placeholders(value) != expected for value in target_forms):
                            problems.append(
                                f"{label}:{message_id}: placeholders differ from English"
                            )
    if problems:
        print("\n".join(f"i18n: {problem}" for problem in problems), file=sys.stderr)
        return 1
    print(f"i18n: {len(catalogs)} shipped catalog(s), {len(english['messages'])} stable IDs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
