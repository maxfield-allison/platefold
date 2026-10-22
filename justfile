# Plate|Fold task runner. Setup needs Python 3.11+; Node is only used for vendor updates.
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]
python := if os() == "windows" { ".venv/Scripts/python.exe" } else { ".venv/bin/python" }

default:
    @just --list

setup:
    uv sync --locked --all-extras

run target *FLAGS:
    {{python}} -m platefold "{{target}}" {{FLAGS}}

gui:
    {{python}} -m platefold.gui

lint:
    {{python}} -m ruff check .
    {{python}} -m ruff check --preview --select PLR1702 platefold
    {{python}} -m ruff format --check .
    {{python}} -m pyright platefold

format:
    {{python}} -m ruff check --fix .
    {{python}} -m ruff format .

test:
    {{python}} -m pytest -m "not integration"

test-all:
    {{python}} -m pytest

smoke:
    {{python}} -m platefold SAMPLE.md -o _smoke
    {{python}} tools/check_blank_pages.py _smoke

check-blanks dir="_smoke":
    {{python}} tools/check_blank_pages.py "{{dir}}"

themes:
    {{python}} -m platefold --list-themes

build:
    {{python}} -m build

exe:
    uv pip install --python {{python}} -r requirements-build.txt -e .
    {{python}} -m PyInstaller packaging/platefold.spec --noconfirm

exe-bundled:
    {{python}} -m playwright install chromium
    uv pip install --python {{python}} -r requirements-build.txt -e .
    {{python}} tools/build_bundle.py

exe-check:
    dist/platefold/platefold tests/fixtures/wide_diagram.md -o _exe-smoke
    {{python}} tools/check_blank_pages.py _exe-smoke

bench count="25":
    {{python}} tools/make_corpus.py _bench {{count}}
    {{python}} -m platefold _bench -o _benchout --quiet --timings

vendor:
    npm ci --ignore-scripts --no-audit --no-fund
    {{python}} tools/vendor_mermaid.py
