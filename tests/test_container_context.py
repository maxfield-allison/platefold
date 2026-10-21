"""The container build context holds tracked build inputs and nothing else.

.dockerignore denies everything and re-admits files by name. A directory
exception would also admit the ignored build output under it (installed
node_modules, a stale dist, the Rust target tree), send it to the builder and,
when the Dockerfile copied the whole desktop tree after npm ci, lay it over
the freshly installed packages.

The fast tests evaluate the rules with the same matching BuildKit uses. The
integration test builds with Docker from a tree seeded with ignored sentinel
files and proves they reach neither the context nor the web bundle.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "PLATEFOLD-CONTEXT-SENTINEL"

# Ignored or untracked paths a working tree accumulates. None may reach a build.
IGNORED = [
    "desktop/node_modules/vite/bin/vite.js",
    "desktop/node_modules/.package-lock.json",
    "desktop/node_modules/axe-core/axe.min.js",
    "desktop/dist/index.html",
    "desktop/dist/assets/index.js",
    "desktop/src-tauri/target/release/platefold-desktop",
    "desktop/src-tauri/runtime/platefold-worker",
    "desktop/src-tauri/gen/schemas/desktop-schema.json",
    "desktop/src/node_modules/stray.ts",
    "desktop/src/dist/stray.css",
    "desktop/src/.main.ts.swp",
    "desktop/.vite/deps/stray.js",
    "desktop/tsconfig.tsbuildinfo",
    "platefold/web/index.html",
    "platefold/web/assets/index.js",
    "platefold/__pycache__/cli.cpython-313.pyc",
    "platefold/ingest/__pycache__/docx.cpython-313.pyc",
    "platefold/stray.pyc",
    "packaging/container/.cache/stray",
    "node_modules/.package-lock.json",
    ".venv/bin/python",
    "dist/platefold-1.0.0-py3-none-any.whl",
    "build/lib/platefold/cli.py",
    "container-release/amd64/image.json",
    ".docker-auth/config.json",
    "platefold.yml",
]

# Every file the web stage may see under desktop/, besides desktop/src/.
DESKTOP_INPUTS = {
    "desktop/THIRD_PARTY.md",
    "desktop/index.html",
    "desktop/package-lock.json",
    "desktop/package.json",
    "desktop/tsconfig.json",
    "desktop/vite.config.ts",
}


# --- BuildKit's .dockerignore matching (moby/patternmatcher) ------------------


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Translate one pattern exactly as patternmatcher does; no character classes here."""
    regex, i = "^", 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*" and pattern[i + 1 : i + 2] == "*":
            i += 1
            if pattern[i + 1 : i + 2] == "/":
                i += 1
            regex += ".*" if i + 1 == len(pattern) else "(.*/)?"
        elif char == "*":
            regex += "[^/]*"
        elif char == "?":
            regex += "[^/]"
        elif char in "[\\":
            raise ValueError(f"extend compile_pattern before using {char!r}: {pattern}")
        else:
            regex += re.escape(char)
        i += 1
    return re.compile(regex + "$")


Rules = tuple[tuple[bool, re.Pattern[str]], ...]


def parse_rules(text: str) -> Rules:
    """(re-admits, pattern) pairs in file order, normalised like ignorefile.ReadAll."""
    parsed = []
    for raw in text.splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        line = raw.strip()
        admits = line.startswith("!")
        pattern = posixpath.normpath(line[1:].strip() if admits else line).lstrip("/")
        parsed.append((admits, compile_pattern(pattern)))
    return tuple(parsed)


@cache
def rules() -> Rules:
    return parse_rules((ROOT / ".dockerignore").read_text(encoding="utf-8"))


def admitted(path: str, ruleset: Rules | None = None) -> bool:
    """Whether BuildKit sends this file: the last rule matching it or a parent decides."""
    parts = path.split("/")
    candidates = [path, *("/".join(parts[:n]) for n in range(1, len(parts)))]
    excluded = False
    for admits, pattern in ruleset or rules():
        if admits != excluded:
            continue
        if any(pattern.match(candidate) for candidate in candidates):
            excluded = not admits
    return not excluded


def tracked_files() -> list[str]:
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("requires a git checkout")
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.decode()
    return [name for name in listed.split("\0") if name and (ROOT / name).is_file()]


def dockerfile_sources(tracked: set[str]) -> list[str]:
    """Tracked files the Dockerfile reads from the build context."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    sources = []
    for line in text.splitlines():
        if line.startswith("COPY ") and "--from=" not in line:
            sources += line.split()[1:-1]
        sources += re.findall(r"--mount=type=bind,source=([^,\s]+)", line)
    files = []
    for source in sources:
        for arch in ("amd64", "arm64"):
            matches = sorted(ROOT.glob(source.replace("${TARGETARCH}", arch).rstrip("/")))
            assert matches, f"Dockerfile source {source} names nothing"
            for match in matches:
                found = match.rglob("*") if match.is_dir() else [match]
                files += [p.relative_to(ROOT).as_posix() for p in found]
    return sorted(set(files) & tracked)


# --- Fast checks of the rules --------------------------------------------------


@pytest.mark.parametrize("path", IGNORED)
def test_ignored_build_output_never_enters_the_context(path):
    assert not admitted(path)


# The desktop and packaging rules as reviewed: each directory exception
# re-admitted everything beneath it, ignored build output included.
REVIEWED_RULES = """
*
!desktop/
!desktop/package.json
!desktop/src/
!desktop/src/**
!packaging/
!packaging/container/
!packaging/container/seccomp.json
"""


def test_directory_exceptions_admit_ignored_output_as_reviewed():
    reviewed = parse_rules(REVIEWED_RULES)
    leaked = [path for path in IGNORED if admitted(path, reviewed)]
    assert "desktop/node_modules/vite/bin/vite.js" in leaked
    assert "desktop/src-tauri/target/release/platefold-desktop" in leaked
    assert "packaging/container/.cache/stray" in leaked
    assert admitted("desktop/package.json", reviewed)
    assert not admitted("README.md", reviewed)


def test_desktop_contributes_only_the_web_build_inputs():
    tracked = tracked_files()
    web_sources = {name for name in tracked if name.startswith("desktop/src/")}
    assert web_sources, "desktop/src has no tracked files"
    from_desktop = {name for name in tracked if name.startswith("desktop/") and admitted(name)}
    # A new tracked source file must be admitted, or the image would build without it.
    assert from_desktop == DESKTOP_INPUTS | web_sources


def test_the_context_is_exactly_the_dockerfile_inputs():
    tracked = set(tracked_files())
    sources = dockerfile_sources(tracked)
    assert {"desktop/src/main.ts", "platefold/locales/en.json", "LICENSE"} <= set(sources)
    assert [name for name in sources if not admitted(name)] == []
    context = {name for name in tracked if admitted(name)}
    # The builder reads the Dockerfile itself from the context.
    assert sorted(context - set(sources)) == ["Dockerfile"]


# --- The real build -------------------------------------------------------------

# Stages appended to the real Dockerfile. context exports what BuildKit
# received; bundle exports the web build after proving no sentinel reached the
# web stage's tree, including the installed packages.
PROBE_STAGES = f"""
FROM scratch AS context
COPY . /

FROM web AS web-probe
RUN if grep -rl {SENTINEL} /build; then exit 1; fi

FROM scratch AS bundle
COPY --from=web-probe /build/desktop/dist/ /
"""


def docker_build(tree: Path, target: str, out: Path) -> None:
    dockerfile = (tree / "Dockerfile").read_text(encoding="utf-8") + PROBE_STAGES
    build = ["docker", "buildx", "build", "--target", target, "--output", f"type=local,dest={out}"]
    result = subprocess.run(
        [*build, "-f", "-", "."],
        input=dockerfile,
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    # BuildKit ends a failure with Go stack traces; the failing step names itself earlier.
    evidence = [line for line in result.stderr.splitlines() if "ERROR" in line or SENTINEL in line]
    assert result.returncode == 0, "\n".join(evidence[-40:]) or result.stderr[-4000:]


@pytest.mark.integration
def test_docker_build_excludes_ignored_host_files_from_context_and_bundle(tmp_path):
    try:
        subprocess.run(["docker", "buildx", "version"], capture_output=True, check=True)
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("requires Docker with buildx")
    tree = tmp_path / "tree"
    for name in tracked_files():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, tree / name)
    for name in IGNORED:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(f"{SENTINEL} {name}\n", encoding="utf-8")

    docker_build(tree, "context", tmp_path / "context")
    docker_build(tree, "bundle", tmp_path / "bundle")

    received = sorted(
        p.relative_to(tmp_path / "context").as_posix()
        for p in (tmp_path / "context").rglob("*")
        if p.is_file()
    )
    # BuildKit's own matching agrees with the fast checks above, file for file.
    assert received == sorted(name for name in tracked_files() if admitted(name))
    bundle = [p for p in (tmp_path / "bundle").rglob("*") if p.is_file()]
    assert (tmp_path / "bundle/index.html") in bundle
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in bundle)
    assert SENTINEL not in text
    # The interface catalogs come from platefold/locales; without them the
    # bundle builds and then stops on load.
    assert "translation_method" in text
