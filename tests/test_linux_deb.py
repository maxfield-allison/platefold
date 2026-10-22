"""The Debian package: layout, metadata, AppArmor attachment and reproducibility."""

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tools import build_deb, check_linux_deb

EPOCH = 1790000000
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="tests Debian packaging")
requires_dpkg = pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="needs dpkg-deb")


def synthetic_build(root):
    runtime = root / "runtime"
    browser = runtime / "_internal/platefold/browser/chrome-headless-shell"
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b"synthetic browser")
    browser.chmod(0o775)
    (runtime / "_internal/platefold/browser/icudtl.dat").write_bytes(b"data")
    (runtime / "_internal/libexample.so.1").write_bytes(b"library")
    (runtime / "_internal/libexample.so.1").chmod(0o755)
    (runtime / "_internal/libexample.so").symlink_to("libexample.so.1")
    (runtime / "platefold").write_bytes(b"synthetic CLI")
    (runtime / "platefold").chmod(0o700)
    program = root / "platefold-desktop"
    program.write_bytes(b"ELF\0__TAURI_BUNDLE_TYPE_VAR_UNK\0program")
    return program, runtime


def members(package):
    listing = subprocess.run(
        ["dpkg-deb", "--fsys-tarfile", str(package)], check=True, capture_output=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(listing)) as archive:
        return {member.name.removeprefix("./"): member for member in archive.getmembers()}


def test_prerelease_versions_sort_before_their_release():
    assert build_deb.debian_version("1.0.0") == "1.0.0"
    assert build_deb.debian_version("1.0.1-dev.2") == "1.0.1~dev.2"
    assert build_deb.package_name("1.0.0") == "platefold_1.0.0_amd64.deb"
    with pytest.raises(SystemExit):
        build_deb.debian_version("1.0")


def test_the_program_is_marked_as_a_package_manager_installation(tmp_path):
    for marker in (b"UNK", b"APP"):
        program = tmp_path / "program"
        program.write_bytes(b"x__TAURI_BUNDLE_TYPE_VAR_" + marker + b"y")
        assert build_deb.deb_program(program) == b"x__TAURI_BUNDLE_TYPE_VAR_DEBy"
    for contents in (b"no marker", b"__TAURI_BUNDLE_TYPE_VAR_UNK__TAURI_BUNDLE_TYPE_VAR_UNK"):
        program.write_bytes(contents)
        with pytest.raises(SystemExit, match="one Tauri bundle marker"):
            build_deb.deb_program(program)


def test_apparmor_profile_grants_userns_to_exactly_the_bundled_chromium():
    text = (build_deb.PACKAGING / f"{build_deb.PROFILE}.apparmor").read_text()
    assert build_deb.profile_attachment(text) == f"/{build_deb.BROWSER.as_posix()}"
    assert "\n  userns,\n" in text
    assert "*" not in build_deb.profile_attachment(text)
    # A distinct name, so an administrator's /opt AppImage profile is never replaced.
    assert build_deb.PROFILE != "platefold"
    assert build_deb.PROFILE in (build_deb.PACKAGING / "postinst").read_text()
    assert build_deb.PROFILE in (build_deb.PACKAGING / "postrm").read_text()


def test_maintainer_scripts_are_valid_posix_shell():
    for script in ("postinst", "postrm"):
        path = build_deb.PACKAGING / script
        subprocess.run(["sh", "-n", str(path)], check=True)
        assert path.read_text().startswith("#!/bin/sh\n")
        assert "set -e" in path.read_text()
    postrm = (build_deb.PACKAGING / "postrm").read_text()
    assert "remove|purge)" in postrm and 'rm -f "$target"' in postrm


@requires_dpkg
def test_package_layout_metadata_and_permissions(tmp_path):
    program, runtime = synthetic_build(tmp_path)
    package = build_deb.build(program, runtime, "1.0.0", tmp_path / "out", epoch=EPOCH)
    assert package.name == "platefold_1.0.0_amd64.deb"

    def field(name):
        return subprocess.run(
            ["dpkg-deb", "--field", str(package), name], check=True, capture_output=True, text=True
        ).stdout.strip()

    assert field("Package") == "platefold"
    assert field("Version") == "1.0.0"
    assert field("Architecture") == "amd64"
    assert field("Depends").startswith("libc6 (>= 2.36), ")
    assert "libgtk-3-0t64 | libgtk-3-0" in field("Depends")
    assert "libwebkit2gtk-4.1-0" in field("Depends")
    assert field("Recommends") == "libglib2.0-bin"

    files = members(package)
    for name, member in files.items():
        assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "root", "root")
        assert member.mtime == EPOCH, name
        if member.isdir():
            assert member.mode == 0o755, name
        elif member.isfile():
            assert member.mode in (0o644, 0o755), name
    assert files["usr/bin/platefold-desktop"].mode == 0o755
    assert files["usr/lib/PlateFold/runtime/platefold"].mode == 0o755
    assert files[build_deb.BROWSER.as_posix()].mode == 0o755
    assert files["usr/lib/PlateFold/runtime/_internal/libexample.so"].issym()
    # Shared libraries are loaded, not run: no execute bit.
    assert files["usr/lib/PlateFold/runtime/_internal/libexample.so.1"].mode == 0o644
    assert files["usr/share/platefold/apparmor/platefold-bundled-chromium"].mode == 0o644
    assert "etc/apparmor.d/platefold-bundled-chromium" not in files
    for name in (
        "usr/bin/platefold",
        "usr/share/applications/PlateFold.desktop",
        "usr/share/icons/hicolor/512x512/apps/platefold-desktop.png",
        "usr/share/icons/hicolor/scalable/apps/platefold-desktop.svg",
        "usr/share/doc/platefold/copyright",
        "usr/share/doc/platefold/changelog.gz",
        "usr/share/man/man1/platefold.1.gz",
        "usr/share/man/man1/platefold-desktop.1.gz",
        "usr/share/lintian/overrides/platefold",
    ):
        assert name in files

    extracted = tmp_path / "extracted"
    subprocess.run(["dpkg-deb", "--raw-extract", str(package), str(extracted)], check=True)
    assert (extracted / "usr/bin/platefold-desktop").read_bytes().count(b"_VAR_DEB") == 1
    assert (extracted / "usr/bin/platefold").read_text() == (
        '#!/bin/sh\nexec /usr/lib/PlateFold/runtime/platefold "$@"\n'
    )
    md5sums = (extracted / "DEBIAN/md5sums").read_text()
    assert "  usr/bin/platefold-desktop\n" in md5sums
    assert "libexample.so\n" not in md5sums  # symlinks carry no checksum
    changelog = subprocess.run(
        ["gzip", "-dc", str(extracted / "usr/share/doc/platefold/changelog.gz")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert changelog.startswith("platefold (1.0.0) stable; urgency=medium\n")
    assert changelog.rstrip().endswith("+0000")
    for script in ("postinst", "postrm"):
        assert (extracted / "DEBIAN" / script).stat().st_mode & 0o777 == 0o755


@requires_dpkg
def test_the_same_inputs_build_the_same_bytes(tmp_path):
    program, runtime = synthetic_build(tmp_path)
    first = build_deb.build(program, runtime, "1.0.0", tmp_path / "a", epoch=EPOCH, threads=1)
    (runtime / "platefold").touch()  # a newer file time must not change the package
    second = build_deb.build(program, runtime, "1.0.0", tmp_path / "b", epoch=EPOCH, threads=4)
    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )
    third = build_deb.build(program, runtime, "1.0.0", tmp_path / "c", epoch=EPOCH + 1)
    assert third.read_bytes() != first.read_bytes()


@requires_dpkg
def test_the_build_refuses_a_runtime_without_the_profiled_chromium(tmp_path):
    program, runtime = synthetic_build(tmp_path)
    shutil.rmtree(runtime / "_internal/platefold/browser")
    with pytest.raises(SystemExit, match="no bundled Chromium"):
        build_deb.build(program, runtime, "1.0.0", tmp_path / "out", epoch=EPOCH)
    with pytest.raises(SystemExit, match="whole seconds"):
        build_deb.source_date_epoch("2026-09-25")


@requires_dpkg
def test_the_install_check_binds_the_package_to_its_receipt(tmp_path):
    program, runtime = synthetic_build(tmp_path)
    package = build_deb.build(program, runtime, "1.0.0", tmp_path, epoch=EPOCH)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    receipt = {
        "source_commit": "1" * 40,
        "source_dirty": False,
        "version": "1.0.0",
        "artifacts": [{"file": package.name, "sha256": digest, "size": package.stat().st_size}],
        "debian_package": {"file": package.name, "package_version": "1.0.0"},
    }
    (tmp_path / "BUILD-RECEIPT.json").write_text(json.dumps(receipt))
    (tmp_path / "SHA256SUMS").write_text(f"{digest}  {package.name}\n")
    assert check_linux_deb.check_receipt(package, "1" * 40)["version"] == "1.0.0"
    with pytest.raises(SystemExit, match="source commit"):
        check_linux_deb.check_receipt(package, "2" * 40)
    (tmp_path / "SHA256SUMS").write_text("")
    with pytest.raises(SystemExit, match="SHA256SUMS"):
        check_linux_deb.check_receipt(package, "1" * 40)


def test_purge_scan_reports_only_platefold_paths(tmp_path, monkeypatch):
    (tmp_path / "etc/apparmor.d").mkdir(parents=True)
    (tmp_path / "etc/apparmor.d/platefold-bundled-chromium").write_text("left")
    (tmp_path / "etc/other").write_text("unrelated")
    monkeypatch.setattr(check_linux_deb, "SCAN_ROOTS", (str(tmp_path / "etc"),))
    assert check_linux_deb.leftovers() == [
        str(tmp_path / "etc/apparmor.d/platefold-bundled-chromium")
    ]


def test_copyright_is_machine_readable_and_names_every_bundled_part():
    text = (build_deb.PACKAGING / "copyright").read_text()
    assert text.startswith(
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
    )
    for part in (
        "LICENSE.headless_shell",
        "mermaid.LICENSE",
        "Sora-OFL.txt",
        "JetBrains-Mono-OFL.txt",
        "PSF-2.0",
        "\nLicense: MIT\n",
    ):
        assert part in text
    mit = Path(build_deb.ROOT / "LICENSE").read_text()
    assert "Copyright (c) 2026 Maxfield Allison" in mit
    assert "Copyright: 2026 Maxfield Allison" in text
