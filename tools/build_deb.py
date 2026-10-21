"""Build the Debian package from the desktop build's exact program and runtime.

The .deb carries the same compiled desktop program and the same frozen
runtime (Python worker, CLI and Chromium) that the AppImage bundles, so the
two Linux downloads share one Chromium payload. It installs under /usr, with
the program in /usr/bin and the runtime in /usr/lib/PlateFold, where Tauri
looks for resources beside a /usr/bin program.

The package is reproducible: every file is owned by root, has normalized
permissions and the SOURCE_DATE_EPOCH timestamp, and dpkg-deb archives the
tree in sorted order. Building twice from the same inputs gives the same
bytes.

    python tools/build_deb.py --program desktop/src-tauri/target/release/platefold-desktop \\
        --runtime desktop/src-tauri/target/release/runtime --version 1.0.0 --output-dir dist/deb
"""

import argparse
import datetime
import email.utils
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "platefold"
ARCHITECTURE = "amd64"
PACKAGING = ROOT / "packaging/linux/deb"
# Tauri resolves resources at <program dir>/../lib/<productName>.
RESOURCE_DIR = Path("usr/lib/PlateFold")
BROWSER = RESOURCE_DIR / "runtime/_internal/platefold/browser/chrome-headless-shell"
PROFILE = "platefold-bundled-chromium"
MAINTAINER = "Maxfield Allison <platefold@probablyfine.dev>"
HOMEPAGE = "https://github.com/maxfield-allison/platefold"
# The frozen runtime is built on Debian 12 and needs its glibc (2.36). The
# desktop program links these libraries directly; the t64 names are the
# Debian 13 and Ubuntu 24.04 packages, which also provide the older names.
DEPENDS = (
    "libc6 (>= 2.36)",
    "libcairo-gobject2",
    "libcairo2",
    "libdbus-1-3",
    "libgcc-s1",
    "libgdk-pixbuf-2.0-0",
    "libglib2.0-0t64 | libglib2.0-0",
    "libgtk-3-0t64 | libgtk-3-0",
    "libjavascriptcoregtk-4.1-0",
    "libpango-1.0-0",
    "libsoup-3.0-0",
    "libwebkit2gtk-4.1-0",
)
# /usr/bin/gio opens an exported PDF in the desktop's default viewer.
RECOMMENDS = ("libglib2.0-bin",)
DESCRIPTION = """\
Documents to PDF with every figure on a page that fits
 Plate|Fold turns Markdown, DOCX, XLSX, CSV and TSV documents into
 print-quality PDFs. DOC, ODT and RTF require a supported installed
 converter, which Plate|Fold does not bundle. Each wide diagram or table
 goes on a page it fits:
 inline, on a landscape plate, or on a larger sheet sized to it.
 .
 This package contains the desktop app and the platefold command, with a
 bundled Chromium that renders the PDFs. On Ubuntu 23.10 and later it
 installs an AppArmor profile that lets that Chromium keep its sandbox."""
ICONS = {
    "32x32": "32x32.png",
    "64x64": "64x64.png",
    "128x128": "128x128.png",
    "256x256": "128x128@2x.png",
    "512x512": "icon.png",
}
MARKER = re.compile(rb"__TAURI_BUNDLE_TYPE_VAR_(?:UNK|APP)")
DEB_MARKER = b"__TAURI_BUNDLE_TYPE_VAR_DEB"


def debian_version(version):
    """1.0.0 stays 1.0.0; a prerelease such as 1.0.1-dev.2 sorts first as 1.0.1~dev.2."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?", version):
        raise SystemExit(f"Not a release version: {version}")
    return version.replace("-", "~", 1)


def package_name(version):
    return f"{PACKAGE}_{debian_version(version)}_{ARCHITECTURE}.deb"


def deb_program(program):
    """Tauri reads its bundle marker to report how it was installed.

    Stamp the package-manager marker so the updater treats this copy as a
    .deb and never tries to replace its own files.
    """
    contents = Path(program).read_bytes()
    markers = MARKER.findall(contents)
    if len(markers) != 1:
        raise SystemExit("Expected one Tauri bundle marker in the desktop program")
    return MARKER.sub(DEB_MARKER, contents, count=1)


def source_date_epoch(value=None):
    value = value if value is not None else os.environ.get("SOURCE_DATE_EPOCH")
    if value is None:
        value = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    if not str(value).isdigit():
        raise SystemExit(f"SOURCE_DATE_EPOCH must be whole seconds: {value!r}")
    return int(value)


def profile_attachment(text):
    match = re.search(rf"^profile {PROFILE} (\S+) flags=\(unconfined\) {{$", text, re.MULTILINE)
    if not match:
        raise SystemExit("The AppArmor profile does not name its attachment path")
    return match.group(1)


def gzip_bytes(data):
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=buffer, mtime=0) as out:
        out.write(data)
    return buffer.getvalue()


def changelog(version, epoch):
    date = email.utils.format_datetime(datetime.datetime.fromtimestamp(epoch, datetime.UTC))
    return (
        f"{PACKAGE} ({debian_version(version)}) stable; urgency=medium\n\n"
        f"  * Plate|Fold {version}. The release notes are in the project's\n"
        "    CHANGELOG.md.\n\n"
        f" -- {MAINTAINER}  {date}\n"
    )


def launcher():
    return f'#!/bin/sh\nexec /{RESOURCE_DIR}/runtime/platefold "$@"\n'


def write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(mode)


def stage(tree, program, runtime, version, epoch):
    """Lay out the installed files under tree; return their relative paths."""
    runtime = Path(runtime)
    if not (runtime / "platefold").is_file():
        raise SystemExit(f"No frozen CLI at {runtime / 'platefold'}")
    shutil.copytree(runtime, tree / RESOURCE_DIR / "runtime", symlinks=True)
    if not (tree / BROWSER).is_file():
        raise SystemExit(f"The runtime has no bundled Chromium at /{BROWSER}")
    profile = (PACKAGING / f"{PROFILE}.apparmor").read_text(encoding="utf-8")
    if profile_attachment(profile) != f"/{BROWSER}":
        raise SystemExit("The AppArmor profile does not name the bundled Chromium")
    write(tree / "usr/bin/platefold-desktop", deb_program(program), 0o755)
    write(tree / "usr/bin/platefold", launcher(), 0o755)
    write(tree / "usr/share/platefold/apparmor" / PROFILE, profile, 0o644)
    write(
        tree / "usr/share/applications/PlateFold.desktop",
        (PACKAGING / "PlateFold.desktop").read_bytes(),
        0o644,
    )
    icons = ROOT / "desktop/src-tauri/icons"
    for size, name in ICONS.items():
        target = tree / f"usr/share/icons/hicolor/{size}/apps/platefold-desktop.png"
        target.parent.mkdir(parents=True)
        shutil.copyfile(icons / name, target)
    scalable = tree / "usr/share/icons/hicolor/scalable/apps/platefold-desktop.svg"
    scalable.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "desktop/app.svg", scalable)
    doc = tree / f"usr/share/doc/{PACKAGE}"
    write(doc / "copyright", (PACKAGING / "copyright").read_bytes(), 0o644)
    write(doc / "changelog.gz", gzip_bytes(changelog(version, epoch).encode()), 0o644)
    write(
        tree / f"usr/share/lintian/overrides/{PACKAGE}",
        (PACKAGING / "lintian-overrides").read_bytes(),
        0o644,
    )
    for page in ("platefold.1", "platefold-desktop.1"):
        write(
            tree / "usr/share/man/man1" / f"{page}.gz",
            gzip_bytes((PACKAGING / page).read_bytes()),
            0o644,
        )


SHARED_LIBRARY = re.compile(r"\.so(\.[0-9]+)*$")


def normalize(tree, epoch):
    """Root-independent permissions and one timestamp, so the archive is reproducible.

    Programs keep their execute bit. Shared libraries are loaded, not run, and
    Debian installs them without one.
    """
    paths = sorted(tree.rglob("*"), reverse=True)
    for path in paths:
        if path.is_symlink():
            os.utime(path, (epoch, epoch), follow_symlinks=False)
            continue
        program = path.stat().st_mode & 0o111 and not SHARED_LIBRARY.search(path.name)
        path.chmod(0o755 if path.is_dir() or program else 0o644)
        os.utime(path, (epoch, epoch))
    tree.chmod(0o755)
    os.utime(tree, (epoch, epoch))


def installed_size(tree):
    """dpkg's measure: KiB rounded up per file, one unit per directory and link."""
    total = 0
    for path in tree.rglob("*"):
        if path.relative_to(tree).parts[0] == "DEBIAN":
            continue
        if path.is_symlink() or path.is_dir():
            total += 1
        else:
            total += -(-path.stat().st_size // 1024)
    return total


def md5sums(tree):
    lines = []
    for path in sorted(tree.rglob("*")):
        relative = path.relative_to(tree)
        if relative.parts[0] == "DEBIAN" or path.is_symlink() or not path.is_file():
            continue
        digest = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
        lines.append(f"{digest}  {relative.as_posix()}\n")
    return "".join(lines)


def control(version, size):
    return (
        f"Package: {PACKAGE}\n"
        f"Version: {debian_version(version)}\n"
        f"Architecture: {ARCHITECTURE}\n"
        f"Maintainer: {MAINTAINER}\n"
        f"Installed-Size: {size}\n"
        f"Depends: {', '.join(DEPENDS)}\n"
        f"Recommends: {', '.join(RECOMMENDS)}\n"
        "Section: text\n"
        "Priority: optional\n"
        f"Homepage: {HOMEPAGE}\n"
        f"Description: {DESCRIPTION}\n"
    )


def build(program, runtime, version, output_dir, epoch=None, workdir=None, threads=4):
    """Write the .deb into output_dir and return its path."""
    epoch = source_date_epoch(epoch)
    if shutil.which("dpkg-deb") is None:
        raise SystemExit("dpkg-deb is required to build the Debian package")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / package_name(version)
    if target.exists():
        raise SystemExit(f"Preserving existing package {target}")
    with tempfile.TemporaryDirectory(prefix="platefold-deb-", dir=workdir) as directory:
        tree = Path(directory) / "root"
        tree.mkdir()
        stage(tree, program, runtime, version, epoch)
        meta = tree / "DEBIAN"
        meta.mkdir()
        for script in ("postinst", "postrm"):
            write(meta / script, (PACKAGING / script).read_bytes(), 0o755)
        write(meta / "md5sums", md5sums(tree), 0o644)
        write(meta / "control", control(version, installed_size(tree)), 0o644)
        normalize(tree, epoch)
        environment = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}
        subprocess.run(
            [
                "dpkg-deb",
                "--root-owner-group",
                "-Zxz",
                "-z6",
                f"--threads-max={threads}",
                "--build",
                str(tree),
                str(target),
            ],
            check=True,
            env=environment,
            stdout=subprocess.DEVNULL,
        )
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", type=Path, required=True, help="built platefold-desktop")
    parser.add_argument("--runtime", type=Path, required=True, help="frozen runtime directory")
    parser.add_argument("--version", help="desktop version, for example 1.0.0")
    parser.add_argument(
        "--receipt",
        type=Path,
        help="take the version and SOURCE_DATE_EPOCH from this desktop BUILD-RECEIPT.json",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--compare", type=Path, help="require the new package to equal this one, byte for byte"
    )
    args = parser.parse_args()
    version, epoch = args.version, None
    if args.receipt:
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        version = version or receipt["version"]
        epoch = receipt["debian_package"]["source_date_epoch"]
    if not version:
        parser.error("--version or --receipt is required")
    package = build(args.program, args.runtime, version, args.output_dir, epoch=epoch)
    if args.compare:
        same = package.read_bytes() == args.compare.read_bytes()
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        if not same:
            raise SystemExit(f"Rebuilt {package.name} ({digest}) differs from {args.compare}")
        print(f"Reproducible: the rebuilt {package.name} matches, sha256 {digest}")
        return
    print(package)


if __name__ == "__main__":
    main()
