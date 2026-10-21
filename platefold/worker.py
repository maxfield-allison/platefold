"""Versioned NDJSON adapter for the packaged desktop shell.

One worker accepts one export at a time. stdin remains available for cancellation;
stdout carries events only. Closing stdin requests cancellation and shutdown.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import warnings
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from platefold.cdp import CDPError, SandboxUnavailableError, TimeoutError_
from platefold.config import (
    LegacySettingsError,
    load_settings_file,
    make_settings,
    settings_files,
)
from platefold.contrast import RULES, contrast_failures
from platefold.i18n import translator
from platefold.ingest import UnsupportedFormatError, ingest, source_theme_for_path
from platefold.ingest.converter import ExternalConverterError
from platefold.limits import ResourceLimitError
from platefold.ooxml import InvalidOOXMLError
from platefold.paths import cache_dir, user_theme_dir
from platefold.preview import PreviewQueue, first_page
from platefold.service import ExportRequest, export
from platefold.theme import (
    Theme,
    ThemeContrastWarning,
    _read_theme,
    available_font_sets,
    colours_of,
    discover_themes,
    get_theme,
    is_builtin,
    save_user_theme,
)
from platefold.validation import validate

MAX_MESSAGE = 65536
# Match the native host's line reader, including the terminal newline.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


# Checked in order: a specific failure class wins over a broader one after it.
_RECOVERY_BY_TYPE: tuple[tuple[type[BaseException] | tuple[type[BaseException], ...], str], ...] = (
    (ExternalConverterError, "help.converter"),
    (LegacySettingsError, "help.legacy_settings"),
    (UnsupportedFormatError, "help.unsupported_format"),
    (InvalidOOXMLError, "help.invalid_ooxml"),
    (ResourceLimitError, "help.resource_limit"),
    (SandboxUnavailableError, "help.browser_sandbox"),
    ((CDPError, TimeoutError_), "help.browser"),
)


def recovery_id(exc: Exception) -> str:
    """Return stable, user-actionable recovery guidance for a worker failure."""
    for kinds, help_id in _RECOVERY_BY_TYPE:
        if isinstance(exc, kinds):
            return help_id
    detail = str(exc).casefold()
    if isinstance(exc, FileNotFoundError) or "not found" in detail or "no longer exists" in detail:
        return "help.source_missing"
    if isinstance(exc, PermissionError) or any(
        phrase in detail for phrase in ("permission denied", "read-only", "access is denied")
    ):
        return "help.permission"
    if isinstance(exc, ValueError):
        return "help.invalid_input"
    return "help.retry"


def response_line(kind: str, request_id: str | None = None, **payload) -> str:
    """Frame one response without sending a partial line to the native host."""
    event = {"schema_version": 1, "event": kind, "id": request_id, **payload}
    line = json.dumps(event) + "\n"
    if len(line.encode("utf-8")) <= MAX_RESPONSE_BYTES:
        return line
    if kind == "progress" and "record" in event:
        # The interface uses counts/file here; complete diagnostics belong in
        # the final report. Preserve the path and make the omission explicit.
        del event["record"]
        event["diagnostics_omitted"] = "Progress diagnostics are too large; see the export report"
        line = json.dumps(event) + "\n"
        if len(line.encode("utf-8")) <= MAX_RESPONSE_BYTES:
            return line
    message = "Desktop response is too large to display. Reduce the source or theme catalog."
    if kind == "result" and isinstance(payload.get("result"), dict):
        result = payload["result"]
        message = (
            "Export finished, but its result is too large for the desktop to display. "
            f"Full report: {result.get('report_path', '')}. "
            f"Completed PDFs remain in: {result.get('output_dir', '')}."
        )
    event = {
        "schema_version": 1,
        "event": "preview_error" if kind == "preview" else "error",
        "id": request_id,
        "code": "response_too_large",
        "message": message,
        "help_id": "help.resource_limit",
        "help": "Reduce the document, split it into smaller files, or remove unusually large images or diagrams before retrying.",
    }
    line = json.dumps(event) + "\n"
    if len(line.encode("utf-8")) > MAX_RESPONSE_BYTES:
        # Even pathological metadata must leave the pipe usable. This fallback
        # does not publish any shortened document paths or native open indexes.
        event["message"] = (
            "Desktop response is too large; inspect the export folder or reduce input."
        )
        line = json.dumps(event) + "\n"
    return line


def source_root(source: str | None) -> Path | None:
    if not source:
        return None
    path = Path(source).resolve()
    if not path.exists():
        raise ValueError("The selected source no longer exists")
    return path.parent if path.is_file() else path


def theme_kind(theme: Theme) -> str:
    """Where a theme file lives decides what the desktop may do to it.

    Built-ins are never written to. Only files in the user theme directory can
    be overwritten or deleted from the app; a project theme beside the documents
    is shown but left alone, because the app never writes into a source tree.
    """
    if is_builtin(theme):
        return "builtin"
    # Resolve the folder, not the file: a linked file inside the user folder is
    # still the user's, and overwrite/delete decide separately what a link allows.
    try:
        if Path(theme.source).parent.resolve() == user_theme_dir().resolve():
            return "user"
    except OSError:
        pass
    return "project"


def source_theme_candidates(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.lower() in {".docx", ".xlsx"} else []
    if source.is_dir():
        return sorted(
            path for path in source.rglob("*") if path.suffix.lower() in {".docx", ".xlsx"}
        )
    return []


def catalog(source: str | None = None) -> dict:
    with warnings.catch_warnings(record=True) as notices:
        warnings.simplefilter("always")
        themes = discover_themes(source_root(source))
        fallback = themes.get("bristol") or next(iter(themes.values()))
        source_choice = replace(
            fallback,
            name="source",
            label="Source document theme",
            source="source",
            description=(
                "Use each available DOCX or XLSX palette and requested typefaces for semantic "
                "re-layout, including after legacy conversion; other inputs use the configured "
                "fallback."
            ),
        )
        if source:
            selected = Path(source).resolve()
            candidates = source_theme_candidates(selected)
            if len(candidates) == 1:
                imported, imported_warnings = source_theme_for_path(candidates[0], make_settings())
                if imported is not None:
                    source_choice = imported
                for code, detail in imported_warnings:
                    warnings.warn(f"{code}: {detail}", stacklevel=2)
    return {
        "themes": [
            {
                "name": t.name,
                "label": t.label,
                "mode": t.mode,
                "description": t.description,
                "builtin": is_builtin(t),
                "kind": "source" if t.name == "source" else theme_kind(t),
                "path": t.source,
                "file": Path(t.source).name,
                "colors": colours_of(t),
                "contrast_failures": contrast_failures(colours_of(t)),
            }
            for t in [source_choice, *themes.values()]
        ],
        "font_sets": [asdict(f) for f in available_font_sets()],
        "contrast_rules": [asdict(rule) for rule in RULES],
        "warnings": [
            str(notice.message)
            for notice in notices
            if not issubclass(notice.category, ThemeContrastWarning)
        ],
    }


def request_from(message: dict) -> ExportRequest:  # noqa: C901
    for field in ("input_path", "output_dir"):
        if not isinstance(message.get(field), str) or not message[field].strip():
            raise ValueError(f"{field} is required")
    root = source_root(message["input_path"])
    if root is None:  # request validation above guarantees this; keep the boundary explicit.
        raise ValueError("input_path is required")
    preset, settings = "auto", {}
    configs = settings_files(root)
    if len(configs) > 1:
        raise ValueError("Multiple settings files found beside the source; keep one")
    if configs:
        file_preset, settings = load_settings_file(str(configs[0]))
        preset = file_preset or preset
    options = message.get("settings", {})
    if not isinstance(options, dict):
        raise ValueError("settings must be an object")
    unknown = options.keys() - {
        "preset",
        "output_mode",
        "margin_mode",
        "orientation",
        "locale_hint",
        "document_language",
        "theme",
        "colors",
        "font_set",
        "image_dpi",
        "page_width",
        "page_height",
        "margin_edges",
        "inline_fit",
        "plate_fit",
        "max_custom_width",
        "caption_px",
        "tile_overlap",
        "body_size_pt",
        "mermaid_font_px",
        "mermaid_wrap_px",
        "toc",
        "bookmarks",
        "csv_header",
        "xlsx_sheets",
    }
    if unknown:
        raise ValueError(f"Unknown desktop setting: {', '.join(sorted(unknown))}")
    preset = options.get("preset", preset)
    configured_theme = settings.get("theme", get_theme("bristol", root))
    settings.update(
        {k: v for k, v in options.items() if k not in {"preset", "colors", "theme", "font_set"}}
    )
    choice = options.get("theme", configured_theme)
    colors = options.get("colors", {})
    if not isinstance(colors, dict):
        raise ValueError("colors must be an object")
    if choice == "source":
        settings["theme"] = configured_theme
        settings["source_theme"] = True
        settings["source_theme_colors"] = colors
        settings["source_fonts"] = "font_set" not in options
        if "font_set" in options:
            settings["font_set"] = options["font_set"]
    else:
        selected = get_theme(choice, root) if isinstance(choice, str) else choice
        settings["theme"] = selected.merged(**colors)
        if "font_set" in options:
            settings["font_set"] = options["font_set"]
    browser = message.get("browser", "auto")
    if not isinstance(browser, str) or browser not in {"auto", "system"}:
        raise ValueError("Choose the bundled renderer or a compatible installed browser")
    return ExportRequest(
        message["input_path"],
        message["output_dir"],
        make_settings(preset, **settings),
        browser=browser,
    )


def save_copy(message: dict) -> dict:
    name = message.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", name):
        raise ValueError(
            "Use a name starting with a letter, with lowercase letters, digits or hyphens"
        )
    root = source_root(message.get("input_path"))
    if message.get("theme") == "source":
        source = Path(message.get("input_path", "")).resolve()
        candidates = source_theme_candidates(source)
        if len(candidates) != 1:
            raise ValueError("Saving a source theme requires one DOCX or XLSX input")
        imported = ingest(str(candidates[0]), make_settings()).source_theme
        if imported is None:
            raise ValueError("The selected OOXML document has no complete source theme to save")
        theme = imported.merged(**edited_colors(message))
        kind = candidates[0].suffix[1:].upper()
        description = f"Source palette and requested fonts imported from {kind} by Plate|Fold."
    else:
        theme = get_theme(message["theme"], root).merged(**edited_colors(message))
        description = "User palette saved from Plate|Fold."
    path = save_user_theme(theme, name, description)
    return {"name": name, "path": str(path), "catalog": catalog(message.get("input_path"))}


def edited_colors(message: dict) -> dict:
    colors = message.get("colors", {})
    if not isinstance(colors, dict):
        raise ValueError("colors must be an object")
    return colors


def user_theme(message: dict) -> tuple[Theme, Path]:
    """Resolve a theme the desktop may write to, or explain why it may not."""
    name = message.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("A theme name is required")
    themes = discover_themes(source_root(message.get("input_path")))
    if name not in themes:
        raise ValueError(f"Theme {name!r} is no longer available. Choose another theme")
    theme = themes[name]
    kind = theme_kind(theme)
    if kind == "builtin":
        raise ValueError("Built-in themes are read-only. Save as a copy to keep your edits")
    if kind != "user":
        raise ValueError(
            "Project themes beside your documents are read-only here. "
            "Save as a copy to keep your edits in your user themes"
        )
    return theme, Path(theme.source)


def overwrite_theme(message: dict) -> dict:
    """Store the edited palette in the selected user theme's own file.

    The complete palette is written, so a sparse theme that inherited a base
    stops following later changes to that base; the desktop says so before the
    user overwrites. Label, description, mode and fonts are preserved.
    """
    theme, path = user_theme(message)
    if path.is_symlink():
        raise ValueError(f"{path} is a link. Edit the file it points to instead")
    merged = theme.merged(**edited_colors(message))
    data = _read_theme(path)
    data.pop("base", None)
    data["colors"] = colours_of(merged)
    validate(data, "theme")
    # Replace atomically so a crash mid-write cannot leave a half theme behind.
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp", delete=False
    ) as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        staged = Path(handle.name)
    try:
        os.replace(staged, path)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    return {
        "name": theme.name,
        "path": str(path),
        "catalog": catalog(message.get("input_path")),
    }


def delete_theme(message: dict) -> dict:
    """Remove a user theme file; documents already exported keep their appearance."""
    theme, path = user_theme(message)
    try:
        path.unlink()
    except FileNotFoundError:
        raise ValueError(f"{path} was already removed") from None
    return {
        "name": theme.name,
        "path": str(path),
        "catalog": catalog(message.get("input_path")),
    }


class DesktopWorker:
    """Own the desktop protocol state and dispatch one validated command at a time."""

    def __init__(self) -> None:
        self.output_lock = threading.Lock()
        self.busy = threading.Event()
        self.cancelled = threading.Event()
        self.job: threading.Thread | None = None
        self.previews = PreviewQueue(self.render_preview, self.emit, self.error_payload)

    @staticmethod
    def error_payload(exc: Exception, message: dict | None) -> dict:
        requested = message.get("ui_locale") if isinstance(message, dict) else None
        messages = translator(requested)
        message_id, localized = messages.exception(exc)
        help_id = recovery_id(exc)
        payload = {"message": localized, "help_id": help_id, "help": messages.text(help_id)}
        if message_id:
            payload["message_id"] = message_id
        if code := getattr(exc, "code", None):
            payload["code"] = code
        if fields := getattr(exc, "fields", None):
            payload["fields"] = fields
        return payload

    def emit(self, kind: str, request_id: str | None = None, **payload) -> None:
        with self.output_lock:
            print(response_line(kind, request_id, **payload), end="", flush=True)

    def render_export(self, message: dict) -> None:
        request_id = message["id"]
        try:
            # Each job owns its cache, including across separate app instances.
            Path(cache_dir()).mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix="desktop-", dir=cache_dir()) as cache:
                result = export(
                    replace(request_from(message), diagram_cache=cache),
                    on_progress=lambda done, total, rel, record: self.emit(
                        "progress", request_id, done=done, total=total, file=rel, record=record
                    ),
                    on_activity=(
                        (lambda phase: self.emit("activity", request_id, phase=phase))
                        if message.get("activity") is True
                        else None
                    ),
                    should_cancel=self.cancelled.is_set,
                )
        except Exception as exc:
            self.busy.clear()
            self.emit("error", request_id, **self.error_payload(exc, message))
            return
        self.busy.clear()
        self.emit("result", request_id, result=result.as_dict())

    def render_preview(self, message: dict) -> dict:
        # Previews never write into the selected destination or source tree.
        with TemporaryDirectory(prefix="platefold-preview-") as directory:
            request = request_from(message | {"output_dir": directory})
            return first_page(replace(request, diagram_cache=str(Path(directory) / "cache")))

    @staticmethod
    def parse_message(line: str) -> dict:
        message = json.loads(line)
        if not isinstance(message, dict) or message.get("schema_version") != 1:
            raise ValueError("Expected worker schema_version 1")
        request_id = message.get("id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("A request id of 1 to 128 characters is required")
        return message

    def dispatch(self, message: dict) -> bool:
        """Handle one command; return false when the worker should stop."""
        request_id = message["id"]
        command = message.get("command")
        if command == "shutdown":
            return False
        if command == "cancel":
            self.cancelled.set()
            self.emit("cancelling", request_id)
            return True
        if self.busy.is_set():
            raise ValueError("An export is already running")
        if command == "catalog":
            self.emit("catalog", request_id, catalog=catalog(message.get("input_path")))
        elif command == "save_copy":
            self.emit("saved", request_id, **save_copy(message))
        elif command == "overwrite":
            self.emit("overwritten", request_id, **overwrite_theme(message))
        elif command == "delete":
            self.emit("deleted", request_id, **delete_theme(message))
        elif command == "preview":
            self.previews.submit(message)
        elif command == "export":
            self.previews.clear()
            self.cancelled.clear()
            self.busy.set()
            self.job = threading.Thread(
                target=self.render_export, args=(message,), name="platefold-export"
            )
            self.job.start()
        else:
            raise ValueError("Unknown worker command")
        return True

    def close(self) -> None:
        self.cancelled.set()
        if self.job:
            self.job.join()
        self.previews.close()

    def run(self) -> int:
        self.emit("ready")
        try:
            while line := sys.stdin.readline(MAX_MESSAGE + 1):
                if len(line) > MAX_MESSAGE:
                    self.emit("error", message="Worker message exceeds 64 KiB")
                    return 64
                request_id = None
                message = None
                try:
                    message = self.parse_message(line)
                    request_id = message["id"]
                    if not self.dispatch(message):
                        break
                except Exception as exc:
                    self.emit("error", request_id, **self.error_payload(exc, message))
        finally:
            self.close()
        return 0


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    if isinstance(sys.stdin, io.TextIOWrapper):
        sys.stdin.reconfigure(encoding="utf-8")
    return DesktopWorker().run()


if __name__ == "__main__":
    raise SystemExit(main())
