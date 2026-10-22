"""Local schema validation; schema links in user files are editor hints only."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


@lru_cache
def validator(name: str) -> Draft202012Validator:
    path = Path(__file__).parent / "schemas" / f"{name}.schema.json"
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def validate(data: Any, schema: str) -> None:
    error = next(validator(schema).iter_errors(data), None)
    if error:
        key = ".".join(str(part) for part in error.absolute_path) or schema
        raise ValueError(f"{key}: {error.message}")
