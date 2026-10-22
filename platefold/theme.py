"""Theme value objects and portable system font stacks."""

from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass, fields, replace
from pathlib import Path
from tempfile import NamedTemporaryFile

from .contrast import contrast_failures, describe_failure
from .installed_fonts import discover_installed_fonts, valid_family
from .limits import MAX_CONFIG_BYTES, read_limited_text
from .paths import user_theme_dir
from .validation import validate

BUILTIN_DIR = Path(__file__).parent / "themes"


class ThemeContrastWarning(UserWarning):
    """A user or project theme has a pairing below WCAG 2.1 AA.

    It is their document and their call, so this is a warning rather than an
    error; the desktop shows the same pairings live while a palette is edited.
    """


# Metric-compatible Linux stand-ins, appended to every stack so that a CI
# container without the Microsoft fonts still lays out at roughly the same
# widths. Carlito matches Calibri; Liberation Sans matches Arial.
_SANS_FALLBACK = "Carlito, 'Liberation Sans', Arial, Sora, sans-serif"
_SERIF_FALLBACK = "'Liberation Serif', 'Times New Roman', Sora, serif"
_MONO_FALLBACK = "'DejaVu Sans Mono', 'Liberation Mono', 'JetBrains Mono', monospace"


@dataclass(frozen=True)
class FontSet:
    """The three typefaces a document uses."""

    name: str
    label: str
    body: str
    head: str
    mono: str


FONT_SETS: dict[str, FontSet] = {
    "sora": FontSet(
        name="sora",
        label="Sora + JetBrains Mono (bundled Latin fonts)",
        body=f"Sora, {_SANS_FALLBACK}",
        head=f"Sora, {_SANS_FALLBACK}",
        mono=f"'JetBrains Mono', {_MONO_FALLBACK}",
    ),
    "calibri": FontSet(
        name="calibri",
        label="Calibri + Segoe UI (Office default)",
        body=f"Calibri, {_SANS_FALLBACK}",
        head=f"'Segoe UI Semibold', 'Segoe UI', Calibri, {_SANS_FALLBACK}",
        mono=f"Consolas, {_MONO_FALLBACK}",
    ),
    "segoe": FontSet(
        name="segoe",
        label="Segoe UI (Windows system font)",
        body=f"'Segoe UI', {_SANS_FALLBACK}",
        head=f"'Segoe UI Semibold', 'Segoe UI', {_SANS_FALLBACK}",
        mono=f"Consolas, {_MONO_FALLBACK}",
    ),
    "cambria": FontSet(
        name="cambria",
        label="Cambria (serif body)",
        body=f"Cambria, Georgia, {_SERIF_FALLBACK}",
        head=f"'Segoe UI Semibold', 'Segoe UI', Calibri, {_SANS_FALLBACK}",
        mono=f"Consolas, {_MONO_FALLBACK}",
    ),
    "georgia": FontSet(
        name="georgia",
        label="Georgia (serif body, warmer)",
        body=f"Georgia, Cambria, {_SERIF_FALLBACK}",
        head=f"'Segoe UI Semibold', 'Segoe UI', {_SANS_FALLBACK}",
        mono=f"Consolas, {_MONO_FALLBACK}",
    ),
    "arial": FontSet(
        name="arial",
        label="Arial (system fonts)",
        body=f"Arial, {_SANS_FALLBACK}",
        head=f"'Arial Bold', Arial, {_SANS_FALLBACK}",
        mono=f"'Courier New', {_MONO_FALLBACK}",
    ),
    # ------------------------------------------------------------------
    # Platform-native stacks.
    #
    # These lead with faces that are NOT on Windows, which the module note
    # above warns about — and the warning still holds: exported on a machine
    # without them, the stack falls through and the document repaginates.
    # That is a property of font substitution, not a bug to fix here. Pair a
    # platform font set with the matching theme when the export happens on
    # that platform. System font substitutions can change pagination.
    # ------------------------------------------------------------------
    "apple": FontSet(
        name="apple",
        label="SF Pro (macOS system fonts)",
        body=f"-apple-system, 'SF Pro Text', 'Helvetica Neue', Helvetica, {_SANS_FALLBACK}",
        head=f"'SF Pro Display', -apple-system, 'Helvetica Neue', {_SANS_FALLBACK}",
        mono=f"'SF Mono', Menlo, Monaco, Consolas, {_MONO_FALLBACK}",
    ),
    "gnome": FontSet(
        name="gnome",
        label="Cantarell / Ubuntu (Linux desktop fonts)",
        body=f"Cantarell, Ubuntu, 'DejaVu Sans', {_SANS_FALLBACK}",
        head=f"Cantarell, 'Ubuntu Medium', Ubuntu, 'DejaVu Sans', {_SANS_FALLBACK}",
        mono=f"'Ubuntu Mono', 'Source Code Pro', {_MONO_FALLBACK}",
    ),
}

# The bundled set, so an export starts from the same faces on every machine and
# every surface: the desktop, the HTTP API, the CLI and the Python service all
# default to it. The Office and platform sets stay one `--font` away.
DEFAULT_FONT_SET = "sora"


@dataclass(frozen=True)
class Theme:
    """A colour palette for one exported document.

    Kept deliberately small. Every colour in the stylesheet resolves to one of
    these tokens, so a new theme is a dozen hex values rather than a fork of
    the stylesheet.
    """

    name: str
    label: str

    # --- structural ---------------------------------------------------
    page: str  # the paper itself — body background
    accent: str  # h1 rule, document header rule, code block spine
    accent_soft: str  # secondary borders drawn from the accent hue
    ink: str  # body text
    ink_strong: str  # bold text, table headers
    title: str  # document title and h1
    heading: str  # h2/h3/h4
    ink_soft: str  # captions, metadata labels, running header/footer
    rule: str  # hairlines and table cell borders
    surface: str  # table header fill, inline code chip
    surface_alt: str  # zebra striping, code block background
    link: str

    # --- diagrams (mermaid themeVariables) ----------------------------
    diagram_fill: str
    diagram_stroke: str
    diagram_text: str
    diagram_alt_fill: str
    diagram_note_fill: str
    diagram_note_stroke: str

    mode: str = "light"
    description: str = ""
    source: str = "builtin"
    fonts: FontSet | None = None

    def merged(self, **overrides: str) -> Theme:
        """Return a copy with the given tokens replaced.

        Unknown keys raise, so a typo in a settings file fails loudly rather
        than silently leaving the colour at its default.
        """
        known = COLOR_TOKENS
        unknown = set(overrides) - known
        if unknown:
            raise ValueError(
                "unknown theme colour(s): %s. Valid tokens: %s"
                % (", ".join(sorted(unknown)), ", ".join(sorted(known - {"name", "label"})))
            )
        for key, value in overrides.items():
            if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                raise ValueError(f"{key}: expected a #rrggbb colour")
        return replace(self, **overrides)


DEFAULT_THEME = "default"

COLOR_TOKENS = {f.name for f in fields(Theme)} - {
    "name",
    "label",
    "mode",
    "description",
    "source",
    "fonts",
}


def colours_of(theme: Theme) -> dict[str, str]:
    return {key: getattr(theme, key) for key in sorted(COLOR_TOKENS)}


def theme_payload(theme: Theme, name: str, description: str) -> dict:
    """Return a complete reusable user-theme document."""
    payload = {
        "name": name,
        "label": name,
        "mode": theme.mode,
        "description": description,
        "colors": colours_of(theme),
    }
    if theme.fonts:
        payload["fonts"] = {key: getattr(theme.fonts, key) for key in ("body", "head", "mono")}
    validate(payload, "theme")
    return payload


def save_user_theme(theme: Theme, name: str, description: str) -> Path:
    """Atomically create a complete user theme without replacing any path."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", name):
        raise ValueError(
            "Use a name starting with a letter, with lowercase letters, digits or hyphens"
        )
    if name == "source" or name in THEMES:
        raise ValueError("Built-in and source theme names are reserved. Choose a new name")
    directory = user_theme_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    payload = theme_payload(theme, name, description)
    staged: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{name}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            staged = Path(handle.name)
        # A hard link publishes the already-complete inode and fails if a file
        # or symlink with the requested name appeared concurrently.
        os.link(staged, path, follow_symlinks=False)
    except FileExistsError:
        raise ValueError("That theme name already exists. Choose a new name for the copy") from None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return path


def is_builtin(theme: Theme) -> bool:
    return theme.source == "builtin" or Path(theme.source).parent == BUILTIN_DIR


def _inside(path: Path, folder: str | Path) -> Path | None:
    """``path`` relative to ``folder`` when it lies inside it, following links."""
    try:
        return Path(os.path.realpath(path)).relative_to(os.path.realpath(folder))
    except (OSError, ValueError):
        return None


def theme_provenance(
    theme: Theme,
    source_root: str | Path | None = None,
    document: str | Path | None = None,
) -> str:
    """Where a theme came from, in terms that are safe to share.

    `Theme.source` holds the file a theme was loaded from, which the desktop
    needs to edit user themes. An export report travels with the PDFs, so it
    gets this instead: `builtin:<name>`, `user:<name>`, `project:<path>`
    relative to the exported folder, `document:<path>` for a palette imported
    from the exported document itself, or `file:<file name>` for an explicit
    theme file elsewhere. None of them discloses a home, user name or
    temporary location.
    """
    if is_builtin(theme):
        return f"builtin:{theme.name}"
    if theme.name == "source":
        relative = (
            _inside(Path(document), source_root)
            if document is not None and source_root is not None
            else None
        )
        if relative is not None:
            return f"document:{relative.as_posix()}"
        return "document:" + Path(document).name if document is not None else "document"
    path = Path(theme.source)
    if _inside(path.parent, user_theme_dir()) == Path("."):
        return f"user:{theme.name}"
    relative = _inside(path, source_root) if source_root is not None else None
    if relative is not None:
        return f"project:{relative.as_posix()}"
    return f"file:{path.name}"


def warn_on_low_contrast(theme: Theme, origin: str | None = None) -> list[dict]:
    """Report pairings below AA for a theme that is not one of ours.

    Built-ins are held to the same rules in the test-suite, so they never warn
    here. Returns the failures so callers can surface them without re-checking.
    """
    failures = contrast_failures(colours_of(theme))
    if failures:
        where = origin or theme.source
        warnings.warn(
            ThemeContrastWarning(
                f"theme {theme.name!r} ({where}) has {len(failures)} colour pairing(s) below "
                "WCAG 2.1 AA: " + "; ".join(describe_failure(f) for f in failures)
            ),
            stacklevel=3,
        )
    return failures


def _read_theme(path: Path) -> dict:
    try:
        data = json.loads(
            read_limited_text(path, MAX_CONFIG_BYTES, label="Theme file", encoding="utf-8-sig")
        )
        validate(data, "theme")
        return data
    except (ValueError, OSError) as exc:
        raise ValueError(f"{path}: {exc}") from None


def _theme_paths(root: str | Path | None, explicit: Path | None, user: bool) -> list[Path]:
    folders = [Path(__file__).parent / "themes"]
    if user:
        folders.append(user_theme_dir())
    if root is not None:
        folders.append(Path(root) / "themes")
    paths = [path for folder in folders for path in sorted(folder.glob("*.json"))]
    if explicit is not None:
        paths.append(explicit)
    # A file reached twice, typically a project theme also passed by path, is one
    # definition rather than an override of itself. It loads once, in its last
    # (strongest) position, so an explicit file still wins over its own layer.
    seen: set[object] = set()
    unique: list[Path] = []
    for path in reversed(paths):
        identity = _file_identity(path)
        if identity not in seen:
            seen.add(identity)
            unique.append(path)
    return unique[::-1]


def _file_identity(path: Path) -> object:
    """Identify the file behind a path, whatever spelling, link or letter case reached it."""
    try:
        status = path.stat()
    except OSError:
        status = None
    if status is not None and status.st_ino:
        return (status.st_dev, status.st_ino)
    # Some Windows and network file systems report no file ID; fall back to the path.
    return os.path.normcase(os.path.realpath(path))


def _theme_definitions(paths: list[Path]) -> dict[str, list[tuple[Path, dict]]]:
    definitions: dict[str, list[tuple[Path, dict]]] = {}
    for path in paths:
        data = _read_theme(path)
        name = data["name"]
        if name in definitions:
            warnings.warn(
                f"theme {name!r}: {path} overrides {definitions[name][-1][0]}", stacklevel=2
            )
        definitions.setdefault(name, []).append((path, data))
    return definitions


def _build_theme(name: str, path: Path, data: dict, base: Theme | None) -> Theme:
    colors = data.get("colors", {})
    if base is not None:
        theme = base.merged(**colors)
    else:
        missing = COLOR_TOKENS - colors.keys()
        if missing:
            raise ValueError(f"{path}: missing theme colour(s): {', '.join(sorted(missing))}")
        theme = Theme(name=name, label=data.get("label", name), **colors)
    fonts = data.get("fonts")
    return replace(
        theme,
        name=name,
        label=data.get("label", name),
        mode=data.get("mode", theme.mode),
        description=data.get("description", ""),
        source=str(path),
        fonts=FontSet(name=name, label=data.get("label", name), **fonts) if fonts else theme.fonts,
    )


def _resolve_theme(
    definitions: dict[str, list[tuple[Path, dict]]],
    resolved: dict[tuple[str, int], Theme],
    name: str,
    index: int | None = None,
    chain: tuple[tuple[str, int], ...] = (),
) -> Theme:
    if name not in definitions:
        raise ValueError(f"unknown theme {name!r}. Available: {', '.join(sorted(definitions))}")
    index = len(definitions[name]) - 1 if index is None else index
    key = (name, index)
    if key in chain:
        raise ValueError(f"cyclic theme base: {' -> '.join(item[0] for item in (*chain, key))}")
    if key in resolved:
        return resolved[key]
    if index < 0:
        raise ValueError(f"theme {name!r} has no earlier definition to inherit")

    path, data = definitions[name][index]
    base_name = data.get("base")
    base = None
    if base_name:
        base_index = index - 1 if base_name == name else None
        base = _resolve_theme(definitions, resolved, base_name, base_index, (*chain, key))
    theme = _build_theme(name, path, data, base)
    resolved[key] = theme
    return theme


def discover_themes(
    root: str | Path | None = None, explicit: Path | None = None, *, user: bool = True
) -> dict[str, Theme]:
    """Load built-ins, user themes, project themes, then an explicit file.

    A sparse override can use its own name as its base to extend the previous
    layer. Other base references use the winning definition, with cycle checks.
    """
    definitions = _theme_definitions(_theme_paths(root, explicit, user))
    resolved: dict[tuple[str, int], Theme] = {}
    return {name: _resolve_theme(definitions, resolved, name) for name in definitions}


def get_theme(name: str, root: str | Path | None = None) -> Theme:
    path = Path(name).expanduser()
    is_path = path.suffix.lower() == ".json" or "/" in name or "\\" in name
    if is_path:
        if not path.is_absolute() and root is not None:
            path = Path(root) / path
        data = _read_theme(path)
        theme = discover_themes(root, path)[data["name"]]
    else:
        themes = discover_themes(root)
        if name not in themes:
            raise ValueError(f"unknown theme {name!r}. Available: {', '.join(sorted(themes))}")
        theme = themes[name]
    if not is_builtin(theme):
        warn_on_low_contrast(theme)
    return theme


def installed_font_set(family: str) -> FontSet:
    if not valid_family(family):
        raise ValueError("Invalid installed font family")
    return FontSet(
        name=f"system:{family}",
        label=family,
        body=f"'{family}', Sora, {_SANS_FALLBACK}",
        head=f"'{family}', Sora, {_SANS_FALLBACK}",
        mono=f"'JetBrains Mono', {_MONO_FALLBACK}",
    )


def available_font_sets() -> list[FontSet]:
    return [
        *FONT_SETS.values(),
        *(installed_font_set(family) for family in discover_installed_fonts().families),
    ]


def get_font_set(name: str) -> FontSet:
    if name.startswith("system:"):
        # Keep a saved request after removal; the renderer checks substitution.
        return installed_font_set(name.removeprefix("system:"))
    try:
        return FONT_SETS[name]
    except KeyError:
        raise ValueError(
            "unknown font set %r. Available: %s" % (name, ", ".join(sorted(FONT_SETS)))
        ) from None


# The GUI's initial choices; CLI lookups discover user and project files afresh.
THEMES = discover_themes(user=False)
