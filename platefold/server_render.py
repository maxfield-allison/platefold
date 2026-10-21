"""One disposable export process. Client options never select local configuration."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import resource
import signal
import sys
from pathlib import Path


def contain(parent_pid: int, limits) -> None:
    import psutil

    from platefold.server_guardian import prctl, terminate_descendants

    def guardian_lost(*_):
        terminate_descendants(psutil.Process())
        os.killpg(os.getpgrp(), signal.SIGKILL)

    # If the guardian itself dies, kill this process group, including Chromium.
    prctl(36, 1)  # Also reap detached descendants if the guardian itself dies.
    signal.signal(signal.SIGUSR1, guardian_lost)
    prctl(1, signal.SIGUSR1)  # PR_SET_PDEATHSIG
    if os.getppid() != parent_pid:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    for key, value in (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_CPU, limits.cpu_seconds),
        (resource.RLIMIT_FSIZE, limits.output_bytes),
        (resource.RLIMIT_NOFILE, 512),
    ):
        _, hard = resource.getrlimit(key)
        value = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(key, (value, value))


def emit(**event) -> None:
    print(json.dumps(event), flush=True)


def prepare_source(root: Path, config: dict, limits) -> Path:
    from platefold.server_sources import SourceBudget, extract_zip, snapshot_mount

    destination = root / "source"
    destination.mkdir()
    source = config["request"]["source"]
    if source["kind"] == "mount":
        snapshot_mount(Path(config["mount_path"]), source["path"], destination, limits)
    elif source["format"] == "zip":
        extract_zip(root / "upload", destination, limits)
    else:
        extension = {
            "markdown": "md",
            "doc": "doc",
            "docx": "docx",
            "odt": "odt",
            "rtf": "rtf",
            "xlsx": "xlsx",
            "csv": "csv",
            "tsv": "tsv",
        }[source["format"]]
        name = f"document.{extension}"
        budget = SourceBudget(limits)
        budget.entry(name)
        with (root / "upload").open("rb") as stream:
            budget.copy(stream, destination / name)
        budget.finish()
    return destination


FONT_WARNINGS = (
    "FONT_FALLBACK:",
    "FONT_FALLBACK_INCOMPLETE:",
    "FONT_FALLBACK_UNVERIFIED:",
    "FONT_GLYPHS_MISSING:",
)


def public_report(result) -> dict:
    """Select report fields; renderer diagnostics can contain private scratch paths."""
    return {
        "schema_version": 1,
        "status": result.status,
        "engine": result.report["engine"],
        "settings": {
            key: result.report["settings"][key]
            for key in (
                "theme",
                "theme_mode",
                "font_set",
                "page_width_mm",
                "page_height_mm",
                "output_mode",
                "margin_mode",
                "page_margins_mm",
                "image_dpi",
                "csv_header",
                "document_language",
                "xlsx_sheets",
                "exact_margins",
                "inline_fit",
                "plate_fit",
                "max_custom_width_mm",
                "caption_px",
                "tile_overlap_mm",
                "body_size_pt",
                "mermaid_font_px",
                "mermaid_wrap_px",
            )
        },
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "documents": [
            {
                "file": doc["file"],
                "pages": doc["pages"],
                "figures": doc["figures"],
                "tiled_figures": doc.get("tiled_figures", []),
                "missing_images": len(doc["missing_images"]),
                "diagram_errors": len(doc["diagram_errors"]),
                "warnings": len(doc["warnings"]),
                # Font warnings name only faces and the document's own
                # characters, never paths, so they are published in full.
                "font_warnings": [w for w in doc["warnings"] if w.startswith(FONT_WARNINGS)],
                "font_fallback": doc.get("font_fallback"),
                "conversion": doc.get("conversion"),
                "theme": doc["theme"],
                "font_set": doc["font_set"],
            }
            for doc in result.report["documents"]
        ],
    }


def publish_results(root: Path, result, limits) -> list[dict]:
    from platefold.limits import ResourceLimitError

    output = root / "output"
    (output / "_export-report.json").unlink(missing_ok=True)
    report = output / "report.json"
    report.write_text(json.dumps(public_report(result)), encoding="utf-8")
    records, size = [], 0
    for path in sorted(output.rglob("*")):
        if not path.is_file():
            continue
        if path.is_symlink() or path.suffix not in {".pdf", ".json"}:
            raise ValueError("Unexpected export artifact")
        size += path.stat().st_size
        if size > limits.output_bytes or len(records) >= limits.documents + 1:
            raise ResourceLimitError("Result exceeds output quota")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        records.append(
            {
                "id": str(len(records)),
                "name": path.relative_to(output).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest,
                "media_type": "application/pdf" if path.suffix == ".pdf" else "application/json",
            }
        )
    return records


def run(root: Path, config: dict, limits) -> None:
    from platefold.server_models import ExportOptions
    from platefold.service import ExportRequest, export

    source = prepare_source(root, config, limits)
    result = export(
        ExportRequest(
            str(source),
            str(root / "output"),
            ExportOptions.model_validate(config["request"]["settings"]).settings(limits),
            config["browser"],
            str(root / "cache"),
        ),
        on_progress=lambda done, total, relative, record: emit(
            event="progress", done=done, total=total, file=relative
        ),
    )
    records = publish_results(root, result, limits)
    emit(event="result", status=result.status, results=records)


def main() -> int:
    from platefold.ingest.converter import ExternalConverterError
    from platefold.limits import ResourceLimitError
    from platefold.server_models import JobLimits
    from platefold.server_sources import InvalidSource

    root = Path(sys.argv[1])
    config = json.loads((root / "request.json").read_text())
    limits = JobLimits(**config["limits"])
    contain(int(sys.argv[2]), limits)
    try:
        run(root, config, limits)
    except InvalidSource:
        emit(event="error", code="invalid_source")
        return 1
    except ResourceLimitError:
        emit(event="error", code="resource_limit")
        return 1
    except ExternalConverterError as exc:
        # Stable code only: never return converter output, source names or
        # private temporary paths from the isolated server process.
        emit(event="error", code=exc.code)
        return 1
    except OSError as exc:
        code = "resource_limit" if exc.errno in {errno.EFBIG, errno.ENOSPC} else "export_failed"
        emit(event="error", code=code)
        return 1
    except Exception:
        # Do not return source snippets, server paths, subprocess logs or environment.
        emit(event="error", code="export_failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
