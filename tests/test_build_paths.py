"""Compiled desktop binaries must not carry the build machine's checkout path."""

import pytest

from tools import build_desktop


def test_rust_and_c_source_paths_are_mapped_to_a_neutral_name(tmp_path, monkeypatch):
    monkeypatch.setattr(build_desktop.sys, "platform", "linux")
    environment = build_desktop.path_mapping_environment(
        {"RUSTFLAGS": "-C debuginfo=1", "CFLAGS": "-O2"}, root=tmp_path
    )
    checkout = str(tmp_path.resolve())

    assert "RUSTFLAGS" not in environment
    assert environment["CARGO_ENCODED_RUSTFLAGS"].split("\x1f") == [
        "-C",
        "debuginfo=1",
        f"--remap-path-prefix={checkout}=platefold",
    ]
    assert environment["CFLAGS"] == f"-O2 -ffile-prefix-map={checkout}=platefold"
    assert environment["CXXFLAGS"] == f"-ffile-prefix-map={checkout}=platefold"


def test_msvc_trims_both_long_and_short_checkout_forms(tmp_path, monkeypatch):
    monkeypatch.setattr(build_desktop.sys, "platform", "win32")
    monkeypatch.setattr(build_desktop, "short_path", lambda _path: r"C:\BUILDS~1\PLATEF~1")
    environment = build_desktop.path_mapping_environment({}, root=tmp_path)
    checkout = str(tmp_path.resolve())

    assert environment["CFLAGS"] == f"/d1trimfile:{checkout}\\ /d1trimfile:C:\\BUILDS~1\\PLATEF~1\\"
    assert (
        "--remap-path-prefix=C:\\BUILDS~1\\PLATEF~1=platefold"
        in (environment["CARGO_ENCODED_RUSTFLAGS"])
    )


def test_existing_encoded_rust_flags_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(build_desktop.sys, "platform", "linux")
    environment = build_desktop.path_mapping_environment(
        {"CARGO_ENCODED_RUSTFLAGS": "-Cstrip=symbols"}, root=tmp_path
    )
    assert environment["CARGO_ENCODED_RUSTFLAGS"].startswith("-Cstrip=symbols\x1f")


@pytest.mark.parametrize(
    "embedded",
    [
        b"/builds/token/0/group/project/desktop/src/main.rs",
        r"C:\builds\token\0\group\project\build\.cargo\registry\x.rs".encode("utf-16-le"),
        b"C:\\BUILDS~1\\TOKEN~1\\0\\GROUP~1\\PROJEC~1\\build\\aws-lc\\mem.c",
        b"/elsewhere/Group/Project/src/lib.rs",
    ],
)
def test_a_binary_naming_the_checkout_or_project_is_refused(tmp_path, embedded):
    binary = tmp_path / "platefold-desktop"
    binary.write_bytes(b"\0header\0" + embedded + b"\0")
    prefixes = ["/builds/token/0/group/project", "C:\\BUILDS~1\\TOKEN~1\\0\\GROUP~1\\PROJEC~1"]
    with pytest.raises(SystemExit, match="checkout path"):
        build_desktop.check_binary_paths([binary], prefixes, "group/project")


def test_a_mapped_binary_passes(tmp_path):
    binary = tmp_path / "platefold-desktop"
    binary.write_bytes(b"\0platefold/desktop/src/main.rs\0cargo/registry/src/x.rs\0")
    build_desktop.check_binary_paths([binary], ["/builds/token/0/group/project"], "group/project")
