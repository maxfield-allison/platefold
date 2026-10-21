"""Shared fixtures.

Integration tests drive a real headless Chromium and assert against real PDFs.
That is deliberate: mocking the browser would test the mock, and every
interesting failure in this project (pagination, blank pages, page sizes)
only exists once something is actually printed.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest
from PIL import Image

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_application_data(tmp_path, monkeypatch):
    """Tests must neither read user themes nor mutate the user's diagram cache."""
    from platefold import cli, paths

    monkeypatch.setattr(cli, "cache_dir", lambda: str(tmp_path / "diagram-cache"))
    monkeypatch.setattr(paths, "config_dir", lambda: str(tmp_path / "config"))


@pytest.fixture
def streaming_finish(monkeypatch):
    """Fail if a PDF is finished through pypdf: Chromium's output must stream."""
    from platefold import pdf as pdf_module

    fallbacks: list[str] = []
    monkeypatch.setattr(
        pdf_module, "_finish_with_pypdf", lambda path, *a, **k: fallbacks.append(path)
    )
    yield fallbacks
    assert fallbacks == [], "finished through pypdf instead of streaming"


def pytest_collection_modifyitems(config, items):
    """Skip integration tests when the browser/mermaid toolchain isn't present."""
    mermaid = PROJECT_ROOT / "platefold" / "vendor" / "mermaid.min.js"
    if mermaid.is_file():
        return
    skip = pytest.mark.skip(reason="packaged mermaid resource missing")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def working_git():
    """Skip a test that builds real repositories when git cannot run.

    Checking PATH is not enough: macOS has a git shim that only offers to
    install the developer tools.
    """
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("requires a working git")


@pytest.fixture
def symlinks(tmp_path_factory):
    """Skip a test that creates symbolic links where this account cannot.

    Windows allows it to administrators and in Developer Mode only.
    """
    probe = tmp_path_factory.mktemp("symlink-probe")
    try:
        (probe / "link").symlink_to(probe / "target")
    except OSError:
        pytest.skip("this account cannot create symbolic links")


@pytest.fixture
def fixture_doc(tmp_path):
    """Copy one fixture markdown file into an isolated working directory."""

    def _copy(name: str) -> Path:
        src = FIXTURES / name
        dst = tmp_path / "src" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return dst

    return _copy


@pytest.fixture
def image_doc(tmp_path):
    """A document containing an image *before* a diagram, plus the image.

    The image is generated rather than committed: it needs to be genuinely
    oversized to exercise downsampling, and a multi-megabyte binary has no
    business in the repository.
    """
    root = tmp_path / "src"
    root.mkdir(parents=True, exist_ok=True)

    # Random pixels: a structured test pattern compresses so well that
    # resampling would *grow* the file and the loader would rightly decline.
    width, height = 1600, 1000
    image_path = root / "diagram-photo.png"
    Image.frombytes("RGB", (width, height), os.urandom(width * height * 3)).save(
        image_path, format="PNG"
    )

    doc = root / "ordering.md"
    doc.write_text(
        "# Ordering Fixture\n"
        "\n"
        "Document: Figure numbering\n"
        "Version: 1.0.0\n"
        "Owner: Test Suite\n"
        "\n"
        "---\n"
        "\n"
        "# 1. An image comes first\n"
        "\n"
        "![A wide raster image](diagram-photo.png)\n"
        "\n"
        "# 2. A diagram comes second\n"
        "\n"
        "```mermaid\n"
        "flowchart LR\n"
        '    a["Alpha"] --> b["Beta"]\n'
        "```\n",
        encoding="utf-8",
    )
    return doc, image_path


@pytest.fixture
def run_export(tmp_path):
    """Run a real export and return (exit_code, output_dir).

    Each call gets its own output directory so a test can export the same
    document twice — with different flags — and compare the results.
    """
    from platefold.cli import main

    counter = itertools.count(1)

    def _run(source: Path, *flags: str) -> tuple[int, Path]:
        out = tmp_path / f"out{next(counter)}"
        argv = [str(source), "-o", str(out), "--quiet", *flags]
        code = main(argv)
        return code, out

    return _run


# CI sets this to 1 in every job that supplies a test prerequisite (the built
# frontend, axe-core, Xvfb). There a missing prerequisite is a broken job, so
# it fails; a developer's run without it still skips.
REQUIRE_PREREQUISITES = "PLATEFOLD_REQUIRE_TEST_PREREQUISITES"


@pytest.fixture(scope="session")
def missing_prerequisite():
    """Return a function that skips, or under REQUIRE_PREREQUISITES=1 fails, the test."""

    def _missing(message: str) -> NoReturn:
        if os.environ.get(REQUIRE_PREREQUISITES) == "1":
            pytest.fail(f"{message} ({REQUIRE_PREREQUISITES}=1 requires it)", pytrace=False)
        pytest.skip(message)

    return _missing
