"""Literal authored assets enter validation before the browser sees the HTML."""

from __future__ import annotations

import html

import pytest

from platefold.html_resources import preflight
from platefold.limits import ResourceLimitError

IMAGE = "data:image/png;base64,AAAA"
OTHER_IMAGE = "data:image/png;base64,BBBB"
FONT = "data:font/woff2;base64,AAAA"


def inspect_resources(body):
    images, reservations = [], []
    assert (
        preflight(body, images.append, lambda size, label: reservations.append((size, label)))
        is None
    )
    return images, reservations


@pytest.mark.parametrize(
    "css",
    [
        f"background: url({IMAGE})",
        f'background: url("{IMAGE}")',
        f"background: url('{IMAGE}')",
        r'background: u\72l("d\61ta:image/png;base64,AAAA")',
        f'background: /* comment */ URL("{IMAGE}" /* modifier comment */)',
        f'background: image-set("{IMAGE}" 1x)',
        f'background: -webkit-image-set("{IMAGE}" 1x)',
        f'background: image-set(url("{IMAGE}") type("image/png") 1x)',
        f'background: cross-fade(50%, url("{IMAGE}"), red)',
        f'--background: url("{IMAGE}"); background: var(--background)',
        f'--background: "{IMAGE}"; background: image-set(var(--background) 1x)',
        r'--background: "d\61ta:image/png;base64,AAAA"; background: image-set(var(--background))',
    ],
)
def test_css_tokens_validate_literal_images(css):
    images, reservations = inspect_resources(f'<p style="{html.escape(css, quote=True)}">Text</p>')
    assert images == [IMAGE]
    assert [size for size, _ in reservations] == [len(IMAGE)]


def test_nested_stylesheet_rules_are_inspected():
    body = f'<style>@media print {{ @supports (display: grid) {{ p {{ background: url("{IMAGE}") }} }} }}</style>'
    images, reservations = inspect_resources(body)
    assert images == [IMAGE]
    assert len(reservations) == 1


def test_data_fonts_count_without_running_the_image_decoder():
    body = f'<style>@font-face {{font-family: Test; src: url("{FONT}")}} p {{background:url({IMAGE})}}</style>'
    images, reservations = inspect_resources(body)
    assert images == [IMAGE]
    assert sorted(size for size, _ in reservations) == sorted([len(FONT), len(IMAGE)])


def test_normal_styling_text_and_remote_resources_are_not_rewritten_or_validated():
    body = '<style>p { color: red; content: "url(data:example)"; background: url(https://example.invalid/i) }</style><p style="font-weight:bold">Text</p>'
    original = body
    assert inspect_resources(body) == ([], [])
    assert body == original


@pytest.mark.parametrize(
    "body",
    [
        f'<svg><image href="{IMAGE}"/></svg>',
        f"<SVG><IMAGE XLINK:HREF='{IMAGE}'/></SVG>",
        f'<svg><feImage href="{IMAGE}"/></svg>',
        f'<input type="image" src="{IMAGE}">',
        f'<video poster="{IMAGE}"></video>',
        f'<table background="{IMAGE}"><tr><td>Text</td></tr></table>',
        f'<svg><rect fill="url({IMAGE})"/></svg>',
        f'<s:svg xmlns:s="http://www.w3.org/2000/svg"><s:image xmlns:x="http://www.w3.org/1999/xlink" x:href="{IMAGE}"/></s:svg>',
        f'<image src="{IMAGE}">',
    ],
)
def test_alternate_literal_image_routes(body):
    images, reservations = inspect_resources(body)
    assert images == [IMAGE]
    assert [size for size, _ in reservations] == [len(IMAGE)]


@pytest.mark.parametrize(
    "tag,attribute", [("img", "srcset"), ("source", "srcset"), ("link", "imagesrcset")]
)
def test_srcset_preserves_data_commas_and_counts_each_candidate(tag, attribute):
    body = f'<{tag} {attribute}="{IMAGE} 1x, {OTHER_IMAGE} 2x">'
    images, reservations = inspect_resources(body)
    assert images == [IMAGE, OTHER_IMAGE]
    assert len(reservations) == 2


def test_srcset_without_descriptors_preserves_the_payload_comma():
    images, _ = inspect_resources(f'<img srcset="{IMAGE}, {OTHER_IMAGE}">')
    assert images == [IMAGE, OTHER_IMAGE]


def test_image_set_string_and_url_candidates_are_each_counted_once():
    images, reservations = inspect_resources(
        f'<style>p {{ background: image-set("{IMAGE}" 1x, url({OTHER_IMAGE}) 2x) }}</style>'
    )
    assert sorted(images) == [IMAGE, OTHER_IMAGE]
    assert len(reservations) == 2


def test_ordinary_img_src_is_owned_by_the_image_embedder():
    assert inspect_resources(f'<IMG SRC="{IMAGE}">') == ([], [])
    images, _ = inspect_resources(f'<img src="{IMAGE}" srcset="{OTHER_IMAGE} 2x">')
    assert images == [OTHER_IMAGE]


def test_html_entities_and_url_whitespace_are_normalized_before_validation():
    images, _ = inspect_resources('<svg><image href="da&#x09;ta:image/png;base64,AAAA"/></svg>')
    assert images == [IMAGE]


def test_first_duplicate_attribute_wins_like_the_browser():
    images, _ = inspect_resources(f'<svg><image href="{IMAGE}" href="{OTHER_IMAGE}"/></svg>')
    assert images == [IMAGE]


def test_reservation_failure_stops_before_image_decoding():
    def reserve(size, label):
        raise ResourceLimitError("synthetic aggregate ceiling")

    def validate(uri):
        raise AssertionError("decoding must not begin after the aggregate ceiling fails")

    with pytest.raises(ResourceLimitError, match="aggregate ceiling"):
        preflight(f"<style>p {{background: url({IMAGE})}}</style>", validate, reserve)


def test_image_validation_failure_propagates():
    def validate(uri):
        raise ResourceLimitError("synthetic pixel ceiling")

    with pytest.raises(ResourceLimitError, match="pixel ceiling"):
        preflight(f'<svg><image href="{IMAGE}"/></svg>', validate, lambda size, label: None)


def test_unclosed_style_block_is_inspected_at_end_of_document():
    images, _ = inspect_resources(f"<style>p {{background: url({IMAGE})}}")
    assert images == [IMAGE]


def test_script_text_does_not_become_an_authored_image():
    assert inspect_resources(f"<script>const text = '<img srcset=\"{IMAGE}\">';</script>") == (
        [],
        [],
    )


def test_svg_foreign_object_img_src_can_be_included_for_nested_image_validation():
    images, reservations = [], []
    body = f'<svg><foreignObject><h:img src="{IMAGE}"/></foreignObject></svg>'
    preflight(
        body, images.append, lambda size, label: reservations.append(size), include_img_src=True
    )
    assert images == [IMAGE]
    assert reservations == [len(IMAGE)]


@pytest.mark.parametrize("tag,attribute", [("set", "to"), ("animate", "from"), ("animate", "by")])
def test_svg_animation_literal_resource_attributes_are_checked(tag, attribute):
    images, reservations = inspect_resources(
        f'<svg><image><{tag} attributeName="xlink:href" {attribute}="{IMAGE}"/></image></svg>'
    )
    assert images == [IMAGE]
    assert len(reservations) == 1


def test_svg_animation_values_with_data_are_explicitly_rejected():
    with pytest.raises(ValueError, match="animation values"):
        inspect_resources(
            f'<svg><animate attributeName="href" values="{IMAGE};{OTHER_IMAGE}"/></svg>'
        )


def test_nonresource_svg_animation_is_unchanged():
    assert inspect_resources('<svg><animate attributeName="opacity" values="0;1"/></svg>') == (
        [],
        [],
    )


def test_svg_style_cdata_is_inspected():
    images, _ = inspect_resources(
        f"<svg><style><![CDATA[p {{ background:url({IMAGE}) }}]]></style></svg>"
    )
    assert images == [IMAGE]
