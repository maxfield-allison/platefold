"""Check literal authored data resources without rewriting HTML or CSS.

Call before inserting generated Mermaid SVG or package font CSS. Ordinary
``img src`` belongs to the image embedder; this pass covers the additional
literal image routes and counts authored data fonts. This is not a sanitizer,
a CSS cascade evaluator, or a recursive SVG-image resource validator.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from typing import Any

import tinycss2

from .limits import ResourceLimitError

_SPACE = " \t\n\r\f"
_URL_TRIM = "".join(chr(value) for value in range(33))
_SVG_CSS_ATTRIBUTES = {
    "fill",
    "stroke",
    "filter",
    "clip-path",
    "mask",
    "cursor",
    "marker",
    "marker-start",
    "marker-mid",
    "marker-end",
}


def _srcset_urls(value: str) -> Iterator[str]:
    """Read candidate URLs without splitting the comma inside a data URL.

    URLs end at ASCII whitespace, with trailing commas separating candidates.
    Descriptors end at an unparenthesized comma. We inspect all candidates,
    including those whose density/width descriptor a browser may ignore.
    """
    position = 0
    while position < len(value):
        while position < len(value) and value[position] in _SPACE + ",":
            position += 1
        start = position
        while position < len(value) and value[position] not in _SPACE:
            position += 1
        url = value[start:position]
        if not url:
            break
        yield url.rstrip(",")
        if not url.endswith(","):
            position = _skip_descriptors(value, position)


def _skip_descriptors(value: str, position: int) -> int:
    depth = 0
    while position < len(value):
        character = value[position]
        position += 1
        if character == "(":
            depth += 1
        elif character == ")" and depth:
            depth -= 1
        elif character == "," and not depth:
            break
    return position


def _quoted_url(arguments: list[Any]) -> str:
    significant = [item for item in arguments if item.type not in {"whitespace", "comment"}]
    return significant[0].value if significant and significant[0].type == "string" else ""


class _Resources(HTMLParser):
    def __init__(
        self,
        validate_image: Callable[[str], None],
        reserve: Callable[[int, str], None],
        include_img_src: bool = False,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.validate_image = validate_image
        self.reserve = reserve
        self.include_img_src = include_img_src
        self.style_parts: list[str] | None = None

    def _resource(self, value: str, label: str, *, image: bool = True) -> None:
        # URL parsing removes ASCII tabs/newlines even inside the scheme.
        value = value.strip(_URL_TRIM).translate({9: None, 10: None, 13: None})
        if not value.lower().startswith("data:"):
            return
        self.reserve(len(value.encode("utf-8")), label)
        if image:
            self.validate_image(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.rsplit(":", 1)[-1]
        # As in the browser and the ordinary image rewriter, first duplicate wins.
        attributes: dict[str, str] = {}
        for name, value in attrs:
            attributes.setdefault(name, value or "")
        if tag == "style":
            self.style_parts = []
        if style := attributes.get("style"):
            self._css(style)
        self._image_attributes(tag, attributes)
        if tag in {"set", "animate"}:
            self._animation(attributes)
        for name in _SVG_CSS_ATTRIBUTES & attributes.keys():
            self._values(tinycss2.parse_component_value_list(attributes[name], skip_comments=True))

    def _image_attributes(self, tag: str, attributes: dict[str, str]) -> None:
        for name in ("srcset", "imagesrcset"):
            for value in _srcset_urls(attributes.get(name, "")):
                self._resource(value, f"HTML {name} data image")
        names: tuple[str, ...] = ()
        if tag in {"image", "feimage"}:
            # XML SVG may bind the XLink namespace to any prefix. The obsolete
            # HTML <image src> spelling is parsed as <img> by browsers as well.
            names = tuple(name for name in attributes if name.rsplit(":", 1)[-1] == "href")
            if tag == "image":
                names += ("src",)
        elif (tag == "img" and self.include_img_src) or (
            tag == "input" and attributes.get("type", "").lower() == "image"
        ):
            names = ("src",)
        elif tag == "video":
            names = ("poster",)
        elif tag in {"body", "table", "tr", "td", "th"}:
            names = ("background",)
        for name in names:
            self._resource(attributes.get(name, ""), f"HTML {tag} {name} data image")

    def _animation(self, attributes: dict[str, str]) -> None:
        target = attributes.get("attributename", "").rsplit(":", 1)[-1].lower()
        if target not in {"href", "src"}:
            return
        for name in ("to", "from", "by"):
            self._resource(attributes.get(name, ""), f"SVG animated {target} data image")
        values = attributes.get("values", "").translate({9: None, 10: None, 13: None})
        if "data:" in values.lower():
            # SMIL uses semicolons between values; data URI metadata also uses
            # them. Do not guess where a candidate resource begins or ends.
            raise ValueError("SVG resource animation values containing data URLs are unsupported")

    def handle_data(self, data: str) -> None:
        if self.style_parts is not None:
            self.style_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.rsplit(":", 1)[-1] == "style":
            self._finish_style()

    def _finish_style(self) -> None:
        if self.style_parts is not None:
            source = "".join(self.style_parts).strip()
            if source.startswith("<![CDATA[") and source.endswith("]]>"):
                source = source[9:-3]
            self._css(source)
            self.style_parts = None

    def _css(self, source: str) -> None:
        pending = [(tinycss2.parse_blocks_contents(source, skip_comments=True), False)]
        while pending:
            nodes, font_face = pending.pop()
            for node in nodes:
                if node.type == "declaration":
                    # A quoted data URI in a custom property can later become
                    # an image-set() candidate through var(). Check the literal
                    # at its declaration without attempting CSS cascade/evaluation.
                    self._values(
                        node.value,
                        font=font_face and node.lower_name == "src",
                        strings=node.lower_name.startswith("--"),
                    )
                elif node.type == "at-rule":
                    imported = node.lower_at_keyword == "import"
                    self._values(node.prelude, font=imported, strings=imported)
                    if node.content is not None:
                        pending.append(
                            (
                                tinycss2.parse_blocks_contents(node.content),
                                node.lower_at_keyword == "font-face",
                            )
                        )
                elif node.type == "qualified-rule":
                    pending.append((tinycss2.parse_blocks_contents(node.content), False))

    def _values(self, tokens: list[Any], *, font: bool = False, strings: bool = False) -> None:
        label = "CSS data resource" if font else "CSS data image"
        pending = [(tokens, strings)]
        while pending:
            values, string_urls = pending.pop()
            for token in values:
                if token.type == "url" or (string_urls and token.type == "string"):
                    self._resource(token.value, label, image=not font)
                elif token.type == "function" and token.lower_name == "url":
                    self._resource(_quoted_url(token.arguments), label, image=not font)
                elif token.type == "function":
                    # Strings in type(), local(), and ordinary content are
                    # not URLs. image-set() and image() explicitly admit them.
                    pending.append(
                        (
                            token.arguments,
                            token.lower_name
                            in {
                                "image-set",
                                "-webkit-image-set",
                                "image",
                            },
                        )
                    )
                elif hasattr(token, "content"):
                    pending.append((token.content, False))


def preflight(
    body: str,
    validate_image: Callable[[str], None],
    reserve: Callable[[int, str], None],
    *,
    include_img_src: bool = False,
) -> None:
    """Validate/count literal authored resources, leaving all source unchanged.

    The caller bounds the document bytes and owns callback validation. Counts
    are conservative per authored occurrence, including unselected srcset or
    CSS branches. Quoted data values in custom properties are conservatively
    validated as images at declaration, even if not consumed. Set include_img_src
    when checking a normalized SVG whose HTML images will not pass through the
    document embedder. XML entity expansion, computed CSS, and recursive image
    traversal belong to the caller; this function does not parse XML or decode assets.
    External resource access remains governed by the renderer's CSP/network policy.
    """
    parser = _Resources(validate_image, reserve, include_img_src)
    try:
        parser.feed(body)
        parser.close()
        parser._finish_style()
    except RecursionError:
        raise ResourceLimitError("Authored CSS nesting exceeds parsing capacity") from None
