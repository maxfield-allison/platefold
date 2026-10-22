"""Prove the desktop runtime trims leave behaviour unchanged."""

import ctypes
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import babel
import pytest

from platefold import i18n
from tools import build_desktop

ROOT = Path(__file__).resolve().parents[1]


def load_runtime_trim():
    spec = importlib.util.spec_from_file_location(
        "runtime_trim", ROOT / "packaging/runtime_trim.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_trim = load_runtime_trim()

# Every Babel call the interface makes, for every shipped catalog.
FORMATTING_PROBE = """
import json
from platefold import i18n
out = {}
for catalog in i18n._catalogs().values():
    t = i18n.Translator.from_catalog(catalog)
    out[t.locale] = [
        [str(i18n._babel_locale(t.locale).plural_form(n)), t.number(n)]
        for n in (0, 1, 2, 3, 5, 11, 21, 100, 1.5, 1234567.891)
    ]
out["fallback"] = str(i18n._babel_locale("xx-unknown").plural_form(2))
import babel
out["babel"] = babel.__file__
print(json.dumps(out, sort_keys=True))
"""


def test_every_shipped_catalog_keeps_its_babel_locale_chain():
    required = runtime_trim.required_babel_locale_files()
    assert {"root.dat", f"{i18n.DEFAULT_LOCALE}.dat"} <= required
    for catalog in i18n._catalogs().values():
        assert f"{catalog['locale'].replace('-', '_')}.dat" in required
    # The trim is only worth carrying while it removes most of Babel's data.
    assert len(required) < 20


def test_babel_filter_drops_only_unused_locale_data():
    required = {"root.dat", "en.dat"}
    keep = runtime_trim.keep_data_entry
    assert keep("babel/locale-data/en.dat", required)
    assert keep("babel\\locale-data\\root.dat", required)
    assert not keep("babel/locale-data/de.dat", required)
    assert keep("babel/global.dat", required)
    assert keep("platefold/locales/en.json", required)
    assert keep("babel-2.18.0.dist-info/licenses/LICENSE", required)


def test_pruned_babel_formats_exactly_like_the_full_data(tmp_path):
    required = runtime_trim.required_babel_locale_files()
    package = Path(babel.__file__).parent
    pruned = tmp_path / "babel"
    shutil.copytree(package, pruned, ignore=shutil.ignore_patterns("__pycache__"))
    for data in (pruned / "locale-data").iterdir():
        if data.name not in required:
            data.unlink()
    assert len(list((pruned / "locale-data").iterdir())) == len(required)

    workdir = tmp_path / "work"
    workdir.mkdir()

    def probe(path_prefix):
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(path_prefix)}
        result = subprocess.run(
            [sys.executable, "-c", FORMATTING_PROBE],
            cwd=workdir,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    full = probe([str(ROOT)])
    trimmed = probe([str(tmp_path), str(ROOT)])
    assert Path(trimmed.pop("babel")).is_relative_to(tmp_path)
    assert not Path(full.pop("babel")).is_relative_to(tmp_path)
    assert trimmed == full


def unstripped_extension():
    for module in ("rpds", "yaml", "PIL"):
        spec = importlib.util.find_spec(module)
        if spec is None or not spec.submodule_search_locations:
            continue
        for folder in spec.submodule_search_locations:
            for path in sorted(Path(folder).glob("*.so")):
                if runtime_trim.has_symbol_table(path):
                    return path
    return None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="ELF stripping is Linux-only")
def test_strip_removes_symbols_but_keeps_the_library_loadable(tmp_path):
    source = unstripped_extension()
    if source is None or not shutil.which("strip"):
        pytest.skip("no unstripped extension module or strip tool in this environment")
    kept = tmp_path / "kept.so"
    shutil.copy2(source, kept)
    kept_bytes = kept.read_bytes()
    binaries = [
        ("pkg/" + source.name, str(source), "EXTENSION"),
        ("kept.so", str(kept), "BINARY"),
    ]
    result = runtime_trim.stripped_binaries(binaries, tmp_path / "work", keep_sources=[kept])

    stripped = Path(result[0][1])
    assert result[0][0] == "pkg/" + source.name and result[0][2] == "EXTENSION"
    assert stripped.is_relative_to(tmp_path / "work")
    assert not runtime_trim.has_symbol_table(stripped)
    assert stripped.stat().st_size < source.stat().st_size
    assert runtime_trim.has_symbol_table(source)
    assert result[1] == binaries[1] and kept.read_bytes() == kept_bytes
    init = "PyInit_" + source.name.split(".", 1)[0]
    assert hasattr(ctypes.CDLL(str(stripped)), init)


def test_non_elf_and_stripped_inputs_are_passed_through(tmp_path):
    text = tmp_path / "data.bin"
    text.write_bytes(b"not an ELF file" * 10)
    assert not runtime_trim.has_symbol_table(text)
    binaries = [("data.bin", str(text), "BINARY")]
    if shutil.which("strip"):
        assert runtime_trim.stripped_binaries(binaries, tmp_path / "work") == binaries


def test_worker_build_records_and_uses_the_trim_module():
    spec = (ROOT / "packaging/desktop-worker.spec").read_text(encoding="utf-8")
    assert "required_babel_locale_files()" in spec
    assert "keep_sources=[browser]" in spec
    assert "packaging/runtime_trim.py" in build_desktop.build_inputs()
