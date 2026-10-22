"""The native build jobs install hash-complete, per-platform requirement files."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tools import native_requirements as requirements

ROOT = Path(__file__).parents[1]
HASH = "a" * 64


def copy_pins(tmp_path: Path) -> tuple[Path, Path]:
    for name in ("requirements-build.txt", "requirements-runtime.txt"):
        shutil.copy(ROOT / name, tmp_path / name)
    output = tmp_path / "native-requirements"
    shutil.copytree(requirements.OUTPUT, output)
    return tmp_path / "requirements-build.txt", output


def test_committed_platform_files_are_hash_complete_and_match_the_pins():
    counts = requirements.check()
    assert set(counts) == {"linux-x86_64", "windows-x86_64", "macos-arm64"}
    pins = requirements.pins()
    for platform in counts:
        text = (requirements.OUTPUT / f"{platform}.txt").read_text(encoding="utf-8")
        _digest, found = requirements.parse(text)
        for name, version in pins.items():
            assert found[name][0] == version
        assert all(hashes for _version, hashes in found.values())
    # The project installs without build isolation, so its build backend is pinned.
    assert "setuptools" in pins


def test_the_browser_pin_and_the_native_download_manifest_agree():
    from tools import native_downloads

    manifest = native_downloads.load_manifest()
    assert requirements.pins()["playwright"] == manifest["playwright"]["version"]


def test_editing_a_pin_fails_until_the_files_are_locked_again(tmp_path):
    pins, output = copy_pins(tmp_path)
    requirements.check(output, pins)
    pins.write_text(pins.read_text().replace("PyInstaller==6.22.2", "PyInstaller==6.22.3"))
    with pytest.raises(requirements.LockError, match="generated from other pins"):
        requirements.check(output, pins)


def test_a_removed_pin_also_fails(tmp_path):
    pins, output = copy_pins(tmp_path)
    pins.write_text(pins.read_text().replace("pyee==13.0.1\n", ""))
    with pytest.raises(requirements.LockError, match="generated from other pins"):
        requirements.check(output, pins)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda text: text.replace("--hash=sha256:", "--hash=md5:", 1), "Only --hash=sha256"),
        (lambda text: text + "requests==2.0.0\n", "has no hash"),
        (lambda text: text + "requests>=2 \\\n    --hash=sha256:" + HASH + "\n", "exact"),
        (lambda text: text + "altgraph==0.17.5 \\\n    --hash=sha256:" + HASH + "\n", "twice"),
        (lambda text: text.replace("--hash", "--no-binary=:all: --hash", 1), "Only --hash"),
    ],
)
def test_a_platform_file_must_stay_exact_and_hashed(tmp_path, change, message):
    pins, output = copy_pins(tmp_path)
    path = output / "linux-x86_64.txt"
    path.write_text(change(path.read_text()))
    with pytest.raises(requirements.LockError, match=message):
        requirements.check(output, pins)


def test_a_platform_file_with_another_version_of_a_pin_is_refused(tmp_path):
    pins, output = copy_pins(tmp_path)
    path = output / "macos-arm64.txt"
    path.write_text(path.read_text().replace("playwright==1.62.0", "playwright==1.62.1"))
    with pytest.raises(requirements.LockError, match=r"playwright==1\.62\.1, the pin is 1\.62\.0"):
        requirements.check(output, pins)


def test_platforms_must_agree_on_shared_versions(tmp_path):
    pins, output = copy_pins(tmp_path)
    path = output / "windows-x86_64.txt"
    path.write_text(path.read_text().replace("typing-extensions==", "typing-extensions==0.", 1))
    with pytest.raises(requirements.LockError, match="differs between platforms"):
        requirements.check(output, pins)


@pytest.mark.parametrize(
    "filename,platform,expected",
    [
        ("pyee-13.0.1-py3-none-any.whl", "windows-x86_64", True),
        ("greenlet-3.5.5-cp313-cp313-win_amd64.whl", "windows-x86_64", True),
        ("greenlet-3.5.5-cp313-cp313-win32.whl", "windows-x86_64", False),
        ("greenlet-3.5.5-cp313-cp313t-win_amd64.whl", "windows-x86_64", False),
        ("greenlet-3.5.5-cp310-cp310-win_amd64.whl", "windows-x86_64", False),
        (
            "pillow-12.3.0-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
            "linux-x86_64",
            True,
        ),
        ("pillow-12.3.0-cp311-cp311-musllinux_1_2_x86_64.whl", "linux-x86_64", False),
        ("pillow-12.3.0-cp311-cp311-manylinux_2_28_aarch64.whl", "linux-x86_64", False),
        ("rpds_py-1-cp314-cp314-macosx_11_0_arm64.whl", "macos-arm64", True),
        ("rpds_py-1-cp312-cp312-macosx_10_12_x86_64.whl", "macos-arm64", False),
        ("cryptography-1-cp38-abi3-macosx_10_9_universal2.whl", "macos-arm64", True),
        ("pypdfium2-5-py3-none-macosx_11_0_arm64.whl", "macos-arm64", True),
        ("playwright-1.62.0-py3-none-manylinux1_x86_64.whl", "linux-x86_64", True),
        ("playwright-1.62.0-py3-none-win_amd64.whl", "linux-x86_64", False),
    ],
)
def test_only_wheels_the_platform_can_install_are_hashed(filename, platform, expected):
    assert requirements.compatible(filename, platform) is expected


def test_lock_writes_files_that_check_accepts_without_the_network(tmp_path):
    pins, output = copy_pins(tmp_path)
    shutil.rmtree(output)
    base = requirements.pins(pins)

    def resolver(platform, _path):
        extra = {"pywin32-ctypes": "0.2.3"} if platform.startswith("windows") else {}
        return {**base, "typing-extensions": "4.16.0", **extra}

    def fetch(name, version):
        return [
            {"filename": f"{name.replace('-', '_')}-{version}-py3-none-any.whl", "sha256": HASH},
            {"filename": f"{name}-{version}-cp313-cp313-win_amd64.whl", "sha256": "b" * 64},
        ]

    requirements.lock(output, pins, fetch=fetch, resolver=resolver)
    windows = (output / "windows-x86_64.txt").read_text()
    linux = (output / "linux-x86_64.txt").read_text()
    assert "pywin32-ctypes==0.2.3" in windows and "pywin32-ctypes" not in linux
    assert "b" * 64 in windows and "b" * 64 not in linux


def test_lock_refuses_a_package_without_a_wheel_for_the_platform(tmp_path):
    pins, output = copy_pins(tmp_path)
    base = requirements.pins(pins)

    def fetch(name, version):
        return [{"filename": f"{name}-{version}-cp313-cp313-win_amd64.whl", "sha256": HASH}]

    with pytest.raises(requirements.LockError, match="no linux-x86_64 wheel"):
        requirements.lock(output, pins, fetch=fetch, resolver=lambda *_: base)


def test_lock_refuses_a_resolution_that_moves_a_pin(tmp_path):
    pins, output = copy_pins(tmp_path)
    moved = {**requirements.pins(pins), "playwright": "1.63.0"}
    with pytest.raises(requirements.LockError, match=r"playwright==1\.63\.0"):
        requirements.lock(output, pins, fetch=lambda *_: [], resolver=lambda *_: moved)
