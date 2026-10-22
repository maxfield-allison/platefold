"""Local open fonts embedded in print and Mermaid HTML without network access."""

import base64
import hashlib
from functools import lru_cache
from pathlib import Path


@lru_cache
def font_css(stacks: str) -> str:
    faces = [
        ("Sora", 400, "sora-latin-400-normal.woff2"),
        ("Sora", 700, "sora-latin-700-normal.woff2"),
        ("JetBrains Mono", 400, "jetbrains-mono-latin-400-normal.woff2"),
    ]
    rules = []
    for family, weight, filename in faces:
        if family in stacks:
            data = base64.b64encode(
                (Path(__file__).parent / "fonts" / filename).read_bytes()
            ).decode()
            rules.append(
                f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:normal;src:url(data:font/woff2;base64,{data}) format('woff2')}}"
            )
    return "\n".join(rules)


def font_signature(stacks: str) -> str:
    return hashlib.sha256(font_css(stacks).encode()).hexdigest()
