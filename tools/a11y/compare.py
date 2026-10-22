"""Compare retained captures and test document cloning outside production code."""

import argparse
import hashlib
import json
from pathlib import Path

import pypdfium2 as pdfium

if __package__:
    from .inspect_pdf import inspect
else:
    from inspect_pdf import inspect
from pypdf import PdfReader, PdfWriter


def raster_hashes(path):
    with pdfium.PdfDocument(path) as document:
        hashes = []
        for page in document:
            bitmap = page.render(scale=1)
            hashes.append(hashlib.sha256(bitmap.to_pil().tobytes()).hexdigest())
            bitmap.close()
            page.close()
        return hashes


def compare(root):
    result = {}
    for case in sorted(p for p in root.iterdir() if (p / "on-capture").is_dir()):
        raw = sorted((case / "on-capture").glob("print-*.pdf"))[-1]
        raw_off = sorted((case / "off-capture").glob("print-*.pdf"))[-1]
        final = next((case / "on-capture/final").glob("*.pdf"))
        final_off = next((case / "off-capture/final").glob("*.pdf"))
        no_bookmarks = next((case / "on-no-bookmarks/final").glob("*.pdf"))
        diagnostic = case / "diagnostic-clone.pdf"
        with PdfReader(raw) as reader, PdfWriter() as writer:
            writer.clone_document_from_reader(reader)
            writer.add_outline_item("Qualification diagnostic", 0)
            writer.add_metadata({"/Title": "Qualification diagnostic"})
            writer.write(diagnostic)
        before, after, clone, bypass = map(inspect, (raw, final, diagnostic, no_bookmarks))
        diagnostic.with_suffix(".inspection.json").write_text(json.dumps(clone, indent=2) + "\n")
        result[case.name] = {
            "pages": after["pages"],
            "raw_bytes_off": raw_off.stat().st_size,
            "raw_bytes_on": raw.stat().st_size,
            "raw_ratio": raw.stat().st_size / raw_off.stat().st_size,
            "final_bytes_off": final_off.stat().st_size,
            "final_bytes_on": final.stat().st_size,
            "raw_roles": before["roles"],
            "raw_alternatives": before["alternatives"],
            "final_structure": after["structure_tree"],
            "raw_language": before["language"],
            "final_language": after["language"],
            "raw_final_text_equal": before["text"] == after["text"],
            "raw_final_annotations_equal": before["annotations"] == after["annotations"],
            "off_on_pixels_equal": raster_hashes(final_off) == raster_hashes(final),
            "raw_final_pixels_equal": raster_hashes(raw) == raster_hashes(final),
            "clone_pixels_equal": raster_hashes(raw) == raster_hashes(diagnostic),
            "clone_nodes_equal": before["nodes"] == clone["nodes"],
            "clone_language_equal": before["language"] == clone["language"],
            "clone_parent_tree": clone["parent_tree"],
            "bypass_has_structure": bypass["structure_tree"],
            "bypass_nodes_equal": before["nodes"] == bypass["nodes"],
        }
    (root / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    compare(parser.parse_args().root)
