"""First-page images of real exports, and a single latest-request preview queue."""

from __future__ import annotations

import base64
import io
import threading
from pathlib import Path

import pypdfium2 as pdfium

from platefold.service import ExportRequest, export

# PDFium calls must not overlap, even when callers use different documents.
_pdfium_lock = threading.Lock()


def first_page(request: ExportRequest) -> dict:
    """Render the first selected document without changing its resource root.

    The caller owns an isolated output/cache directory and removes it afterwards.
    Printing through the export path preserves first-page layout without generating
    the rest of the PDF. The image is bounded to 1200 pixels on the longest edge.
    """
    result = export(request, max_documents=1, first_page_only=True)
    doc = result.report["documents"][0]
    path = (Path(result.output_dir) / doc["file"]).with_suffix(".pdf")
    with _pdfium_lock, pdfium.PdfDocument(path) as pdf:
        page = pdf[0]
        width, height = page.get_size()
        bitmap = page.render(scale=min(1.5, 1200 / max(width, height)))
        try:
            image = bitmap.to_pil()
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            size = image.size
            text = page.get_textpage()
            try:
                excerpt = text.get_text_bounded()[:12000]
            finally:
                text.close()
        finally:
            bitmap.close()
        return {
            "file": doc["file"],
            # A preview deliberately renders only page 1. Reporting a total
            # here would require generating the entire PDF first.
            "pages": None,
            "width": size[0],
            "height": size[1],
            "image": base64.b64encode(stream.getvalue()).decode("ascii"),
            "text": excerpt,
            "settings": result.report["settings"],
            "warnings": doc["warnings"]
            + [f"Missing image: {name}" for name in doc["missing_images"]]
            + [f"Diagram failed: {name}" for name in doc["diagram_errors"]],
        }


class PreviewQueue:
    """Keep one active render and at most one pending replacement.

    Old completions (including errors) are discarded. Export can clear pending
    previews without waiting for the active document to finish rendering.
    """

    def __init__(self, render, emit, error_payload=None):
        self.render = render
        self.emit = emit
        self.error_payload = error_payload or (lambda exc, _message: {"message": str(exc)})
        self.condition = threading.Condition()
        self.pending = None
        self.revision = 0
        self.stopped = False
        self.thread = threading.Thread(target=self._run, name="platefold-preview")
        self.thread.start()

    def submit(self, message):
        with self.condition:
            self.revision += 1
            self.pending = (self.revision, message)
            self.condition.notify()

    def clear(self):
        with self.condition:
            self.revision += 1
            self.pending = None

    def close(self):
        with self.condition:
            self.stopped = True
            self.revision += 1
            self.pending = None
            self.condition.notify()
        self.thread.join()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or self.pending is not None)
                if self.stopped:
                    return
                pending = self.pending
                self.pending = None
                if pending is None:  # defensive against a spurious or future alternate wake-up
                    continue
                revision, message = pending
            try:
                payload = {"preview": self.render(message)}
                kind = "preview"
            except Exception as exc:
                payload = self.error_payload(exc, message)
                kind = "preview_error"
            with self.condition:
                if revision == self.revision and not self.stopped:
                    self.emit(kind, message["id"], **payload)
