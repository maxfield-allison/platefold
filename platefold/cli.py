#!/usr/bin/env python3
"""Export supported text documents to print-quality PDFs.

    python -m platefold "C:\\Docs\\MyVault" -o "C:\\Docs\\PDFs"

Mermaid diagrams are rendered as vector SVG, referenced images are embedded,
and oversized diagrams are placed on their own correctly-sized pages.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable

from platefold import __version__
from platefold.config import (
    PRESETS,
    LegacySettingsError,
    load_settings_file,
    make_settings,
    settings_files,
)
from platefold.i18n import Translator, translator
from platefold.paths import cache_dir
from platefold.service import ExportRequest, ExportResult, export
from platefold.service import collect as collect_sources
from platefold.theme import FONT_SETS, discover_themes, get_theme


def _interface_locale(argv: list[str]) -> str | None:
    """Read only the interface-locale option before argparse creates its help."""
    for index, value in enumerate(argv):
        if value.startswith("--ui-locale="):
            return value.partition("=")[2]
        if value == "--ui-locale" and index + 1 < len(argv):
            return argv[index + 1]
    return None


def parse_args(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    messages = translator(_interface_locale(argv))
    # prog is fixed: from Python 3.14 argparse names the program after how
    # __main__ was found ("python -m platefold"), not after argv[0].
    p = argparse.ArgumentParser(prog="platefold", description=messages.text("cli.description"))
    p.add_argument("input", nargs="?", help=messages.text("cli.input_help"))
    p.add_argument("-o", "--output", default=None, help=messages.text("cli.output_help"))
    p.add_argument(
        "--preset",
        default=None,
        choices=["auto", *PRESETS],
        help=messages.text("cli.preset_help"),
    )
    p.add_argument(
        "--output-mode",
        choices=["online", "print", "tiled"],
        default=None,
        help=messages.text("cli.output_mode_help"),
    )
    p.add_argument("--margins", choices=["full", "none", "bottom-right"], default=None)
    p.add_argument("--orientation", choices=["portrait", "landscape"], default=None)
    p.add_argument("--locale", default=None, help=messages.text("cli.locale_help"))
    p.add_argument("--ui-locale", default=None, help=messages.text("cli.ui_locale_help"))
    _advanced_arguments(p, messages)
    p.add_argument(
        "--theme",
        default=None,
        help=messages.text("cli.theme_help"),
    )
    p.add_argument(
        "--save-theme",
        default=None,
        metavar="NAME",
        help=messages.text("cli.save_theme_help"),
    )
    p.add_argument("--version", action="version", version=f"Plate|Fold {__version__}")
    p.add_argument(
        "--font", default=None, choices=sorted(FONT_SETS), help=messages.text("cli.font_help")
    )
    p.add_argument("--dpi", type=int, default=None, help=messages.text("cli.dpi_help"))
    p.add_argument(
        "--config",
        default=None,
        help=messages.text("cli.config_help"),
    )
    p.add_argument(
        "--no-soft-breaks", action="store_true", help=messages.text("cli.soft_breaks_help")
    )
    p.add_argument(
        "--no-header",
        action="store_true",
        help=messages.text("cli.header_help"),
    )
    p.add_argument(
        "--document-language",
        help=messages.text("cli.document_language_help"),
    )
    p.add_argument(
        "--sheets",
        nargs="+",
        default=None,
        metavar="SHEET",
        help=messages.text("cli.sheets_help"),
    )
    p.add_argument("--no-bookmarks", action="store_true")
    p.add_argument("--no-toc", action="store_true")
    p.add_argument("--clear-cache", action="store_true", help=messages.text("cli.cache_help"))
    p.add_argument(
        "--browser",
        default="auto",
        help=messages.text("cli.browser_help"),
    )
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true", help=messages.text("cli.json_help"))
    p.add_argument("--timings", action="store_true", help=messages.text("cli.timings_help"))
    p.add_argument("--list-themes", action="store_true", help=messages.text("cli.themes_help"))
    result = p.parse_args(argv)
    result._translator = messages
    return result


def _advanced_arguments(parser: argparse.ArgumentParser, messages: Translator) -> None:
    group = parser.add_argument_group(messages.text("cli.advanced_group"))
    for flag, message_id in (
        ("page-width", "cli.page_width_help"),
        ("page-height", "cli.page_height_help"),
        ("inline-fit", "cli.inline_fit_help"),
        ("plate-fit", "cli.plate_fit_help"),
        ("max-custom-width", "cli.max_custom_width_help"),
        ("caption-px", "cli.caption_help"),
        ("tile-overlap", "cli.tile_overlap_help"),
        ("body-size-pt", "cli.body_size_help"),
    ):
        group.add_argument(f"--{flag}", type=float, default=None, help=messages.text(message_id))
    group.add_argument(
        "--margin-edges",
        type=float,
        nargs=4,
        default=None,
        metavar=("TOP", "RIGHT", "BOTTOM", "LEFT"),
        help=messages.text("cli.margin_edges_help"),
    )
    group.add_argument(
        "--mermaid-font-px", type=int, default=None, help=messages.text("cli.mermaid_font_help")
    )
    group.add_argument(
        "--mermaid-wrap-px", type=int, default=None, help=messages.text("cli.mermaid_wrap_help")
    )


def collect(target: str) -> tuple[list[str], str]:
    try:
        return collect_sources(target)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def find_settings_file(
    explicit: str | None, root: str, messages: Translator | None = None
) -> str | None:
    """Locate a settings file: the explicit path, then beside the documents."""
    messages = messages or translator()
    if explicit:
        if not os.path.isfile(explicit):
            sys.exit(messages.text("cli.settings_missing", path=explicit))
        return explicit
    try:
        found = [str(path) for path in settings_files(root)]
    except LegacySettingsError as exc:
        sys.exit(str(exc))
    if len(found) > 1:
        sys.exit(messages.text("cli.settings_multiple", paths=", ".join(found)))
    return found[0] if found else None


def build_settings(args, root: str):
    """Merge defaults, an optional settings file, and command-line flags."""
    preset, overrides = None, {}
    messages = getattr(args, "_translator", translator())
    path = find_settings_file(args.config, root, messages)
    if path:
        try:
            preset, overrides = load_settings_file(path)
        except ValueError as exc:
            sys.exit(str(exc))

    # Command line beats the file.
    if args.preset:
        preset = args.preset
    cli = {
        "output_mode": args.output_mode,
        "margin_mode": args.margins,
        "orientation": args.orientation,
        "locale_hint": args.locale,
        "document_language": args.document_language,
        "theme": None if args.theme == "source" else args.theme,
        "source_theme": args.theme == "source" or None,
        "source_fonts": False if args.theme == "source" and args.font else None,
        "font_set": args.font,
        "image_dpi": args.dpi,
        "soft_breaks": False if args.no_soft_breaks else None,
        "csv_header": False if args.no_header else None,
        "xlsx_sheets": args.sheets,
        "bookmarks": False if args.no_bookmarks else None,
        "toc": False if args.no_toc else None,
    }
    overrides.update({k: v for k, v in cli.items() if v is not None})
    for key in (
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
    ):
        if (value := getattr(args, key)) is not None:
            overrides[key] = value
    try:
        if isinstance(overrides.get("theme"), str):
            overrides["theme"] = get_theme(overrides["theme"], root)
        selected = overrides.get("theme")
        if selected and selected.fonts and not overrides.get("font_set"):
            overrides["fonts"] = selected.fonts
        return make_settings(preset or "auto", **overrides), path
    except ValueError as exc:
        sys.exit(str(exc))


def list_themes(args) -> int:
    root = os.path.abspath(args.input or ".")
    if os.path.isfile(root):
        root = os.path.dirname(root)
    themes = discover_themes(root)
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "command": "list_themes",
                    "status": "completed",
                    "exit_code": 0,
                    "themes": [
                        {
                            "name": name,
                            "label": theme.label,
                            "mode": theme.mode,
                            "source": theme.source,
                        }
                        for name, theme in sorted(themes.items())
                    ],
                    "font_sets": [
                        {"name": name, "label": fonts.label}
                        for name, fonts in sorted(FONT_SETS.items())
                    ],
                }
            )
        )
        return 0
    messages = args._translator
    print(messages.text("cli.themes"))
    for name, theme in sorted(themes.items()):
        print("  %-10s %s [%s]" % (name, theme.label, theme.source))
    print("\n" + messages.text("cli.font_sets"))
    for name, fonts in sorted(FONT_SETS.items()):
        print("  %-10s %s" % (name, fonts.label))
    return 0


def print_summary(result: ExportResult, quiet: bool, messages: Translator | None = None) -> None:
    messages = messages or translator()
    summary = result.report["documents"]
    if not quiet:
        print(messages.text("result.rendered_with", engine=result.report["engine"]))
    pages = sum(document["pages"] for document in summary)
    print(
        "\n"
        + messages.plural(
            "result.summary",
            len(summary),
            documents=messages.number(len(summary)),
            pages=messages.number(pages),
            seconds=messages.number(result.elapsed_seconds, "0.0"),
            output=result.output_dir,
        )
    )
    saved = sum(document["image_bytes_saved"] for document in summary)
    if saved > 512 * 1024:
        print(
            messages.text(
                "result.image_savings", megabytes=messages.number(saved / 1024 / 1024, "0.0")
            )
        )
    missing = sum(len(document["missing_images"]) for document in summary)
    errors = sum(len(document["diagram_errors"]) for document in summary)
    if missing:
        print(messages.plural("result.images_missing", missing))
    if errors:
        print(messages.plural("result.diagrams_failed", errors))
    if result.cancelled:
        print(
            messages.plural(
                "result.cancelled_partial",
                result.total_documents,
                written=messages.number(len(summary)),
                total=messages.number(result.total_documents),
            )
        )


def warning_hints(warnings: list[str], messages: Translator) -> list[str]:
    """Name the command-line control that clears each coded warning, once.

    Engine warnings are worded for every surface because they also reach the
    export report, JSON results and API clients. The desktop swaps in its own
    control names by warning code; this is the command line's equivalent.
    """
    codes = {warning.split(":", 1)[0] for warning in warnings}
    hints = []
    if "DOCUMENT_LANGUAGE" in codes:
        hints.append(messages.text("cli.hint_document_language"))
    if "FONT_SUBSTITUTED" in codes:
        hints.append(messages.text("cli.hint_font_substituted"))
    return hints


def progress_reporter(
    quiet, stream, callback=None, messages: Translator | None = None, hints: bool = True
):
    messages = messages or translator()

    def progress(done, total, relative, record):
        if callback:
            callback(done, total, relative, record)
        if not record or quiet:
            return
        flag = ""
        if record["missing_images"]:
            flag += messages.text(
                "cli.progress_missing", count=messages.number(len(record["missing_images"]))
            )
        if record["diagram_errors"]:
            flag += messages.text(
                "cli.progress_diagrams", count=messages.number(len(record["diagram_errors"]))
            )
        print(
            messages.text(
                "cli.progress",
                pages=messages.number(record["pages"]),
                figures=messages.number(record["figures"]),
                file=relative,
                flags=flag,
            ),
            file=stream,
        )
        for warning in record["warnings"]:
            print(warning, file=stream)
        for hint in warning_hints(record["warnings"], messages) if hints else ():
            print("  " + hint, file=stream)

    return progress


def _run(args, on_progress=None, should_cancel=None, hints: bool = True) -> int:
    messages = args._translator
    if args.list_themes:
        return list_themes(args)

    if not args.input:
        raise SystemExit(messages.text("cli.input_required"))
    target = os.path.abspath(args.input)
    if not os.path.exists(target):
        raise SystemExit(messages.text("cli.input_missing", path=target))
    files, root = collect(target)
    if not files:
        raise SystemExit(messages.text("cli.input_empty", path=target))
    settings, settings_path = build_settings(args, root)
    out_root = os.path.abspath(
        args.output or os.path.join(os.path.dirname(root.rstrip(os.sep)), "PDF Export")
    )
    stream = sys.stderr if args.json else sys.stdout
    # Warnings quote document characters and file names. A console or file
    # in a legacy code page must not turn one into an encoding error.
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    if settings_path and not args.quiet:
        print(messages.text("cli.settings_using", path=settings_path), file=stream)

    result = export(
        ExportRequest(
            target,
            out_root,
            settings,
            args.browser,
            cache_dir(),
            args.clear_cache,
            args.timings,
            args.save_theme,
        ),
        on_progress=progress_reporter(args.quiet, stream, on_progress, messages, hints),
        should_cancel=should_cancel,
    )
    if args.json:
        print(json.dumps(result.as_dict()))
    else:
        print_summary(result, args.quiet, messages)
    if result.timings.enabled:
        print(result.timings.report(), file=stream)
    return result.exit_code


def main(
    argv=None,
    on_progress: Callable[[int, int, str, dict | None], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    command_hints: bool = True,
) -> int:
    """CLI adapter. JSON mode reserves stdout for one versioned result object.

    `command_hints=False` omits the flag-naming recovery lines, for the legacy
    Tk interface, which reuses this adapter but has no command line to type.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    messages = translator(_interface_locale(argv))
    if argv and argv[0] == "serve":
        try:
            from platefold.server import main as serve_main
        except ImportError:
            print(messages.text("cli.server_dependencies"), file=sys.stderr)
            return 64
        return serve_main(argv[1:])
    if argv == ["--desktop-worker"]:
        from platefold.worker import main as worker_main

        return worker_main()
    options = argv[: argv.index("--")] if "--" in argv else argv
    machine_output = "--json" in options
    command = "list_themes" if "--list-themes" in options else "export"
    error_fields: dict = {}
    try:
        args = parse_args(argv)
        return _run(args, on_progress, should_cancel, command_hints)
    except SystemExit as exc:
        # argparse help/version intentionally retain their normal human output.
        if not machine_output or exc.code in (0, None):
            raise
        message = (
            str(exc.code) if isinstance(exc.code, str) else messages.text("cli.invalid_arguments")
        )
        code, category = 64, "invalid_input"
    except KeyboardInterrupt:
        if not machine_output:
            raise
        message, code, category = messages.text("cli.interrupted"), 130, "interrupted"
    except Exception as exc:
        # The process adapter translates renderer/library failures into the
        # machine contract. Direct service callers still receive the exception.
        _message_id, localized = messages.exception(exc)
        if not machine_output:
            raise SystemExit(localized) from exc
        message, code = localized, 1
        category = getattr(exc, "code", "export_failed")
        error_fields = getattr(exc, "fields", {})
    print(
        json.dumps(
            {
                "schema_version": 1,
                "command": command,
                "status": "cancelled" if code == 130 else "failed",
                "exit_code": code,
                "error": {
                    "code": category,
                    "message": message,
                    **({"fields": error_fields} if error_fields else {}),
                },
            }
        )
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
