"""Renderer evidence for requested fonts and real substitutions.

The probes use representative document text, not an assertion about every glyph
in a PDF. CSS platform-font evidence distinguishes rendered families from the
requested CSS stack; FontFace(local()).load checks local face availability
without document.fonts.check's successful generic fallback.
"""

from __future__ import annotations

import contextlib
import uuid

from .cdp import CDPError

_PROBES = r"""async ({id, stacks, exclude, texts}) => {
    const container = document.createElement('div');
    container.id = id;
    container.style.cssText = 'position:fixed;left:-20000px;top:0;white-space:pre;pointer-events:none';
    container.setAttribute('aria-hidden', 'true');
    document.body.appendChild(container);
    const selectors = {body:'p,td,li,blockquote', head:'h1,h2,h3,h4,h5,h6', mono:'pre,code'};
    const result = [];
    for (const [role, stack] of Object.entries(stacks)) {
        const node = texts ? (texts[role] ? {textContent: texts[role]} : null)
            : Array.from(document.querySelectorAll(selectors[role]))
                .find(node => (node.textContent || '').trim() && !(exclude && node.closest(exclude)));
        if (!node) continue;
        const requested = stack.split(',')[0].trim().replace(/^['"]|['"]$/g, '');
        const generic = ['serif','sans-serif','monospace','system-ui','-apple-system','cursive','fantasy'].includes(requested);
        let available = generic;
        // Bundled @font-face families are not local() fonts.
        for (const face of document.fonts) {
            if (face.family.replace(/^['"]|['"]$/g, '').toLowerCase() === requested.toLowerCase()
                && face.status === 'loaded') available = true;
        }
        if (!available) {
            const escaped = requested.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
            try { await new FontFace('PF-local-check', `local("${escaped}")`).load(); available = true; }
            catch (_) { available = false; }
        }
        const sample = document.createElement('span');
        sample.dataset.role = role;
        sample.style.fontFamily = stack;
        sample.style.fontSize = '16px';
        sample.style.fontWeight = role === 'head' ? '700' : '400';
        sample.textContent = node.textContent.slice(0, 256);
        container.appendChild(sample);
        result.push({role, requested, available, generic});
    }
    await document.fonts.ready;
    return result;
}"""


_REPRESENTATIVE = r"""({exclude}) => {
    const selectors = {body:'p,td,li,blockquote', head:'h1,h2,h3,h4,h5,h6', mono:'pre,code'};
    const texts = {};
    for (const [role, selector] of Object.entries(selectors)) {
        const node = Array.from(document.querySelectorAll(selector))
            .find(node => (node.textContent || '').trim() && !(exclude && node.closest(exclude)));
        if (node) texts[role] = node.textContent;
    }
    return texts;
}"""


def representative_texts(page, exclude: str = "") -> dict[str, str]:
    """The first text of each role in the loaded document, as the probe picks it."""
    texts = page.evaluate(_REPRESENTATIVE, {"exclude": exclude})
    return texts if isinstance(texts, dict) else {}


def font_resolution(
    page,
    settings,
    *,
    roles: tuple[str, ...] = ("body", "head", "mono"),
    exclude: str = "",
    texts: dict[str, str] | None = None,
) -> list[dict]:
    """Probe rendered families without changing the printed document.

    ``roles`` limits the probe to some text roles, and ``exclude`` is a CSS
    selector for content another print pass already covers. ``texts`` gives
    the representative text per role instead of reading it from the page.
    """
    probe_id = "pf-font-" + uuid.uuid4().hex
    all_stacks = {
        "body": settings.body_font,
        "head": settings.head_font,
        "mono": settings.mono_font,
    }
    stacks = {role: stack for role, stack in all_stacks.items() if role in roles}
    try:
        probes = page.evaluate(
            _PROBES, {"id": probe_id, "stacks": stacks, "exclude": exclude, "texts": texts}
        )
        if not isinstance(probes, list):
            return []
        page._send("DOM.enable")
        page._send("CSS.enable")
        root = page._send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        resolutions = []
        for probe in probes:
            node = page._send(
                "DOM.querySelector",
                {
                    "nodeId": root,
                    "selector": f"#{probe_id} [data-role={probe['role']}]",
                },
            )["nodeId"]
            fonts = page._send("CSS.getPlatformFontsForNode", {"nodeId": node}).get("fonts", [])
            used = sorted({f["familyName"] for f in fonts if f.get("glyphCount", 0) > 0})
            status = (
                "unverified"
                if not used
                else "generic"
                if probe["generic"]
                else "available"
                if probe["available"]
                or probe["requested"].casefold() in {name.casefold() for name in used}
                else "substituted"
            )
            resolutions.append(
                {
                    "role": probe["role"],
                    "requested": probe["requested"],
                    "used": used,
                    "status": status,
                    "scope": "representative-text",
                }
            )
        return resolutions
    finally:
        # Cancellation exceptions must propagate; cleanup cannot mask them.
        with contextlib.suppress(Exception):
            page.evaluate("id => document.getElementById(id)?.remove()", probe_id)
        for domain in ("CSS", "DOM"):
            with contextlib.suppress(Exception):
                page._send(f"{domain}.disable")


_UNVERIFIED = (
    "FONT_RESOLUTION_UNVERIFIED: The renderer could not verify which fonts were used. "
    "Check the PDF appearance before sharing it."
)


def report_font_resolution(page, settings, info: dict) -> None:
    if "font_resolution" in info:
        return  # contents-page measuring/final passes have the same font request
    try:
        resolutions = font_resolution(page, settings)
    except (CDPError, KeyError, AttributeError, TypeError):
        record_font_resolution(info, None)
        return
    record_font_resolution(info, resolutions)


def record_font_resolution(info: dict, resolutions: list[dict] | None) -> None:
    """Store probe results and their warnings; None means the probe failed."""
    if resolutions is None:
        info["font_resolution"] = []
        info.setdefault("warnings", []).append(_UNVERIFIED)
        return
    info["font_resolution"] = resolutions
    for resolution in info["font_resolution"]:
        if resolution["status"] == "substituted":
            warning = (
                f"FONT_SUBSTITUTED: {resolution['requested']} is unavailable to the renderer for "
                f"{resolution['role']} text; using {', '.join(resolution['used'])}. "
                "Install the requested font, or choose a font set whose fonts are installed. "
                "Substitution can change page layout."
            )
        elif resolution["status"] == "unverified":
            warning = (
                f"FONT_RESOLUTION_UNVERIFIED: The renderer could not identify the font used for "
                f"{resolution['role']} text. Check the PDF appearance before sharing it."
            )
        else:
            continue
        if warning not in info.setdefault("warnings", []):
            info["warnings"].append(warning)
