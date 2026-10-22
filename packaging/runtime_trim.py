"""Leave out of the frozen desktop runtime what it can never use.

Two trims, each chosen because it cannot change what the runtime does:

* Babel ships CLDR data for more than a thousand locales, about 30 MB.
  PlateFold formats interface numbers and plural categories only in the
  locales it has an interface catalog for, and falls back to English for
  anything else. The runtime keeps those locales and the files they inherit
  from. The set is recorded by making the same Babel calls the interface
  makes, not by re-implementing Babel's inheritance rules.
* Some Linux wheels ship shared libraries with their full symbol tables.
  The dynamic loader never reads that table, so the runtime carries a copy
  stripped the way distributions strip their own libraries. Chromium and
  every library that is already stripped are left byte-for-byte as they are.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path, PurePath

# Enough values to reach every plural category a catalog locale can select.
PLURAL_SAMPLES = (0, 1, 2, 3, 5, 11, 21, 100, 1.5)

SHT_SYMTAB = 2


def required_babel_locale_files() -> set[str]:
    """Return the locale-data file names Babel reads for every shipped catalog."""
    import babel.localedata as localedata

    from platefold import i18n

    loaded: set[str] = set()
    original = localedata.resolve_locale_filename

    def recording(name):
        path = original(name)
        if Path(path).is_file():
            loaded.add(Path(path).name)
        return path

    with localedata._cache_lock:
        cached = dict(localedata._cache)
        localedata._cache.clear()
        localedata.resolve_locale_filename = recording
        try:
            for catalog in i18n._catalogs().values():
                translator = i18n.Translator.from_catalog(catalog)
                for count in PLURAL_SAMPLES:
                    i18n._babel_locale(translator.locale).plural_form(count)
                    translator.number(count)
                translator.number(1234567.891)
            i18n._babel_locale(i18n.DEFAULT_LOCALE).plural_form(1)
        finally:
            localedata.resolve_locale_filename = original
            localedata._cache.clear()
            localedata._cache.update(cached)
    if "root.dat" not in loaded or f"{i18n.DEFAULT_LOCALE}.dat" not in loaded:
        raise SystemExit(f"Babel locale data probe found an incomplete set: {sorted(loaded)}")
    return loaded


def keep_data_entry(destination: str, required: set[str]) -> bool:
    """Keep every bundled file except Babel locale data no catalog reads."""
    parts = PurePath(destination.replace("\\", "/")).parts
    if len(parts) == 3 and parts[:2] == ("babel", "locale-data"):
        return parts[2] in required
    return True


def has_symbol_table(path: Path) -> bool:
    """Whether a 64-bit little-endian ELF file still carries a static symbol table."""
    with Path(path).open("rb") as handle:
        header = handle.read(64)
        if len(header) < 64 or header[:4] != b"\x7fELF" or header[4:6] != b"\x02\x01":
            return False
        offset = int.from_bytes(header[0x28:0x30], "little")
        entry_size = int.from_bytes(header[0x3A:0x3C], "little")
        count = int.from_bytes(header[0x3C:0x3E], "little")
        if not offset or entry_size < 8:
            return False
        handle.seek(offset)
        table = handle.read(entry_size * count)
    return any(
        int.from_bytes(table[index + 4 : index + 8], "little") == SHT_SYMTAB
        for index in range(0, len(table) - entry_size + 1, entry_size)
    )


def stripped_binaries(binaries, workpath, keep_sources=()):
    """Point unstripped ELF libraries at stripped copies under workpath.

    ``binaries`` is PyInstaller's (destination, source, typecode) list. Sources
    in ``keep_sources`` stay untouched whatever their state.
    """
    strip = shutil.which("strip")
    if not strip:
        raise SystemExit("strip is required to build the Linux desktop worker")
    kept = {Path(source).resolve() for source in keep_sources}
    target_root = Path(workpath) / "stripped"
    result = []
    for destination, source, typecode in binaries:
        path = Path(source)
        if path.resolve() in kept or not path.is_file() or not has_symbol_table(path):
            result.append((destination, source, typecode))
            continue
        target = target_root / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        subprocess.run([strip, "--strip-unneeded", str(target)], check=True)
        result.append((destination, str(target), typecode))
    return result
