"""Cancel and the export deadline stop a document import part-way through.

Each reader is exercised with a counting cancel check. A first, uncancelled
import counts how often the reader consults the check and where from; later
imports raise at chosen points and must stop at that exact call, surfacing the
same exception rather than reporting a malformed document.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook
from test_docx_ingest import STYLES, make_docx

from platefold.cancellation import CancelledError, Throttle, checked
from platefold.config import Settings
from platefold.ingest import ingest
from platefold.limits import ResourceLimitError, ResourceLimits

_CANCELLATION_MODULE = str(Path(sys.modules[CancelledError.__module__].__file__ or ""))


class CountingCheck:
    """Record every call and its caller; raise `error` on call number `stop_at`."""

    def __init__(self, stop_at: int | None = None, error: type[Exception] = CancelledError):
        self.calls = 0
        self.stop_at = stop_at
        self.error = error
        self.callers: set[str] = set()

    def __call__(self) -> None:
        self.calls += 1
        frame = sys._getframe(1)
        # Report the reader that asked, not the throttling helper between them.
        while frame.f_back is not None and frame.f_code.co_filename == _CANCELLATION_MODULE:
            frame = frame.f_back
        self.callers.add(frame.f_code.co_qualname)
        if self.stop_at is not None and self.calls >= self.stop_at:
            raise self.error("stopped by the test")


def _markdown(tmp_path: Path) -> Path:
    section = (
        "## Section {n}\n\nSome *emphasis*, **strong** text and a [link](https://example.com).\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n- one\n- two\n\n"
    )
    path = tmp_path / "large.md"
    path.write_text(
        "# Title\n\n"
        + "".join(section.format(n=n) for n in range(400))
        + "```mermaid\nflowchart LR\n A --> B\n```\n",
        encoding="utf-8",
    )
    return path


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "large.csv"
    path.write_text(
        "name,value,note\n" + "".join(f"row {n},{n},text {n}\n" for n in range(5000)),
        encoding="utf-8",
    )
    return path


def _docx(tmp_path: Path) -> Path:
    paragraphs = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Part {n}</w:t></w:r></w:p>'
        + "<w:p><w:r><w:t>Semantic body text.</w:t></w:r></w:p>" * 8
        for n in range(300)
    )
    image = (
        '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Proof" descr="Proof"/>'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdImage1"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )
    assert "Heading1" in STYLES
    return make_docx(tmp_path / "large.docx", body=paragraphs + image)


def _xlsx(tmp_path: Path) -> Path:
    workbook = Workbook()
    first = workbook.active
    first.title = "First"
    second = workbook.create_sheet("Second")
    for sheet in (first, second):
        sheet.append(["Name", "Value", "Note"])
        for n in range(1500):
            sheet.append([f"row {n}", n, f"note {n}"])
    first.merge_cells("A2:B2")
    path = tmp_path / "large.xlsx"
    workbook.save(path)
    return path


# Where each reader must consult the check: every loop that can run long on a
# large or hostile input, including inside the third-party converters.
FORMATS = {
    "markdown": (
        _markdown,
        {
            "ingest",
            "extract_mermaid",
            "_BlockCheckpoint.test",
            "_InlineCheckpoint.handleMatch",
            "_StageCheckpoint.run",
            "_TextCheckpoint.run",
            # Heading ids, assigned in one call by the contents extension.
            "_Scope.unique",
        },
    ),
    "csv": (_csv, {"ingest", "_parse"}),
    "docx": (
        _docx,
        {
            # Package validation: members, and each chunk as it is decompressed
            # and parsed.
            "OOXMLPackage._validate",
            "OOXMLPackage._read_validated",
            # Semantic preparation and Mammoth's parse, tree build and walk.
            "Languages.annotate",
            "prepared",
            "_CheckedBuffer.write",
            "_CheckedReader.read",
            "_CheckedElement.__setattr__",
            "_CheckedChildren.__iter__",
            "_image_converter.<locals>.convert",
            "_checkpoint.<locals>.transform",
            # Clean-up of Mammoth's HTML.
            "_sanitize",
            "ingest",
        },
    ),
    "xlsx": (
        _xlsx,
        {
            "OOXMLPackage._validate",
            "OOXMLPackage._read_validated",
            "_shape",
            "_merged_ranges",
            "_formula_cells",
            "_table",
            "ingest",
        },
    ),
}


@pytest.fixture(scope="module", params=sorted(FORMATS))
def large_source(request, tmp_path_factory):
    build, expected_callers = FORMATS[request.param]
    path = build(tmp_path_factory.mktemp(request.param))
    baseline = ingest(str(path), Settings())
    counted = CountingCheck()
    assert ingest(str(path), Settings(), counted) == baseline
    return path, counted, expected_callers


def test_every_long_running_stage_consults_the_check(large_source):
    _path, counted, expected_callers = large_source
    assert expected_callers <= counted.callers, expected_callers - counted.callers
    # Enough checks that stopping at any of them is a mid-import cancellation.
    assert counted.calls >= 20


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 0.75, 1.0])
@pytest.mark.parametrize("error", [CancelledError, ResourceLimitError])
def test_import_stops_at_the_requested_check_with_the_same_exception(large_source, fraction, error):
    path, counted, _expected = large_source
    stop_at = max(1, round(counted.calls * fraction))
    check = CountingCheck(stop_at, error)

    with pytest.raises(error, match="stopped by the test"):
        ingest(str(path), Settings(), check)

    # No further work, and no retry that would call the check again.
    assert check.calls == stop_at


def test_throttle_and_checked_reach_the_check_at_the_stride():
    calls = []
    tick = Throttle(lambda: calls.append(1), 3)
    for _ in range(10):
        tick()
    assert len(calls) == 3

    calls.clear()
    assert list(checked(range(7), lambda: calls.append(1), 3)) == list(range(7))
    # Once before the first item, then every third item.
    assert len(calls) == 3


def _docx_with_paragraphs(tmp_path: Path, paragraphs: int) -> Path:
    # Varied text keeps the part well inside the compression-ratio limit.
    words = random.Random(0)
    return make_docx(
        tmp_path / f"paragraphs-{paragraphs}.docx",
        body="".join(
            f"<w:p><w:r><w:t>{words.getrandbits(64):016x}</w:t></w:r></w:p>"
            for _ in range(paragraphs)
        ),
    )


def test_cancel_lands_inside_a_part_during_the_budgeted_streaming_parse(tmp_path):
    # About 2 MiB of document.xml: many 64 KiB chunks, each parsed as it is
    # decompressed, while staying far inside the XML node budget.
    path = _docx_with_paragraphs(tmp_path, 60_000)
    chunks = []

    def check():
        frame = sys._getframe(1)
        if (
            frame.f_code.co_name == "_read_validated"
            and frame.f_locals["name"] == "word/document.xml"
        ):
            chunks.append(frame.f_locals["member_size"])
            if len(chunks) == 5:
                raise CancelledError("stopped by the test")

    with pytest.raises(CancelledError, match="stopped by the test"):
        ingest(str(path), Settings(), check)
    # Four chunks had been read and parsed; the rest of the part never was.
    assert chunks[-1] == 4 * 64 * 1024


def test_node_budget_still_refuses_a_flood_when_a_cancel_check_is_present(tmp_path):
    path = _docx_with_paragraphs(tmp_path, 2_000)
    limits = ResourceLimits(max_docx_xml_nodes=1_000)
    counted = CountingCheck()

    with pytest.raises(ResourceLimitError, match="node XML limit"):
        ingest(str(path), Settings(limits=limits), counted)
    assert counted.calls > 0
