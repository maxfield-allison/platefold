"""Rust toolchains come only from the reviewed image or component archives."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from argparse import Namespace

import pytest

from tools import rust_toolchain


def item(url: str, data: bytes, package: str | None = None) -> dict:
    record = {
        "url": url,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    if package:
        record.update(package=package, path=url.removeprefix("https://static.rust-lang.org/"))
    return record


@pytest.fixture
def world(tmp_path, monkeypatch):
    version = "1.89.0"
    target = "x86_64-pc-windows-msvc"
    component_bytes = {
        "cargo": b"reviewed cargo archive",
        "rust-std": b"reviewed standard library archive",
        "rustc": b"reviewed compiler archive",
    }
    components = []
    channel_entries = []
    upstream = {}
    for package, data in component_bytes.items():
        url = f"https://static.rust-lang.org/dist/2025-08-07/{package}-{version}-{target}.tar.xz"
        component = item(url, data, package)
        components.append(component)
        upstream[url] = data
        channel_entries.append(
            f"[pkg.{package}.target.{target}]\n"
            f"available = true\n"
            f'xz_url = "{url}"\n'
            f'xz_hash = "{component["sha256"]}"\n'
        )
    channel = (
        'manifest-version = "2"\n'
        'date = "2025-08-07"\n'
        "[pkg.rust]\n"
        'version = "1.89.0 (29483883e 2025-08-04)"\n' + "\n".join(channel_entries)
    ).encode()
    channel_url = f"https://static.rust-lang.org/dist/channel-rust-{version}.toml"
    upstream[channel_url] = channel
    manifest = {
        "schema_version": 1,
        "version": version,
        "ci_image_digest": "sha256:" + "a" * 64,
        "channel_manifest": item(channel_url, channel),
        "platforms": {
            "windows-x86_64": {"target": target, "components": components},
        },
    }
    manifest_path = tmp_path / "rust.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setenv("RUSTUP_HOME", str(tmp_path / "rustup"))

    def download(url, destination):
        destination.write_bytes(upstream[url])

    versions = {
        "rustc": f"rustc {version} (29483883e 2025-08-04)\nhost: {target}",
        "cargo": f"cargo {version} (c24e10642 2025-06-23)",
    }
    return Namespace(
        manifest=manifest,
        manifest_path=manifest_path,
        upstream=upstream,
        channel_url=channel_url,
        download=download,
        versions=versions,
        receipt=tmp_path / "build/rust-toolchain.json",
    )


def install(world, installer):
    return rust_toolchain.install(
        world.receipt,
        world.manifest_path,
        platform_key="windows-x86_64",
        download=world.download,
        installer=installer,
        version_check=lambda *_: world.versions,
    )


def test_install_exposes_only_verified_channel_and_components(world):
    def installer(host, version):
        channel_name = f"dist/channel-rust-{version}.toml"
        checksum = urllib.request.urlopen(f"{host}/{channel_name}.sha256").read().decode()
        channel = urllib.request.urlopen(f"{host}/{channel_name}").read()
        assert checksum.split()[0] == hashlib.sha256(channel).hexdigest()
        assert host.encode() in channel
        assert b"https://static.rust-lang.org" not in channel
        for component in world.manifest["platforms"]["windows-x86_64"]["components"]:
            assert (
                urllib.request.urlopen(f"{host}/{component['path']}").read()
                == world.upstream[component["url"]]
            )

    record = install(world, installer)
    assert json.loads(world.receipt.read_text()) == record
    assert record["kind"] == "verified-archives"
    assert [entry["package"] for entry in record["components"]] == [
        "cargo",
        "rust-std",
        "rustc",
    ]


def test_rewriting_a_checked_component_changes_nothing_served(world):
    """A process rewrites the downloaded components before rustup requests them."""

    def rewrite_then_fetch(host, version):
        (work,) = world.receipt.parent.glob("rust-toolchain-*")
        assert list(work.iterdir()) == []
        components = world.manifest["platforms"]["windows-x86_64"]["components"]
        (work / "channel.toml").write_bytes(b"substituted channel")
        for index, _ in enumerate(components):
            (work / f"component-{index}.tar.xz").write_bytes(b"substituted component")
        channel = urllib.request.urlopen(f"{host}/dist/channel-rust-{version}.toml").read()
        assert b"substituted" not in channel
        for component in components:
            served = urllib.request.urlopen(f"{host}/{component['path']}").read()
            assert served == world.upstream[component["url"]]
        urllib.request.urlopen(f"{host}/dist/channel-rust-{version}.toml.sha256").read()

    install(world, rewrite_then_fetch)


def test_install_refuses_any_unreviewed_rustup_request(world):
    def installer(host, version):
        channel_name = f"dist/channel-rust-{version}.toml"
        urllib.request.urlopen(f"{host}/{channel_name}.sha256").read()
        urllib.request.urlopen(f"{host}/{channel_name}").read()
        for component in world.manifest["platforms"]["windows-x86_64"]["components"]:
            urllib.request.urlopen(f"{host}/{component['path']}").read()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{host}/dist/unreviewed.tar.xz")

    with pytest.raises(rust_toolchain.ToolchainError, match="not only the reviewed"):
        install(world, installer)
    assert not world.receipt.exists()


def test_changed_channel_manifest_runs_nothing(world):
    world.upstream[world.channel_url] += b"\n# replaced upstream\n"
    called = False

    def installer(*_):
        nonlocal called
        called = True

    with pytest.raises(rust_toolchain.ToolchainError, match="not the reviewed Rust input"):
        install(world, installer)
    assert not called
    assert not world.receipt.exists()


def test_channel_must_bind_every_pinned_component(world):
    channel = world.upstream[world.channel_url].replace(
        world.manifest["platforms"]["windows-x86_64"]["components"][0]["sha256"].encode(),
        b"0" * 64,
    )
    world.upstream[world.channel_url] = channel
    world.manifest["channel_manifest"] = item(world.channel_url, channel)
    world.manifest_path.write_text(json.dumps(world.manifest))
    with pytest.raises(rust_toolchain.ToolchainError, match="no longer binds cargo"):
        install(world, lambda *_: None)


def test_existing_toolchain_is_not_reused(world):
    toolchain = world.receipt.parents[1] / "rustup/toolchains/old"
    toolchain.mkdir(parents=True)
    with pytest.raises(rust_toolchain.ToolchainError, match="is not empty"):
        install(world, lambda *_: None)


def test_baked_toolchain_requires_the_reviewed_image_digest(world, monkeypatch):
    monkeypatch.setattr(rust_toolchain, "host_platform", lambda: "linux-x86_64")
    receipt = world.receipt
    versions = {
        "rustc": "rustc 1.89.0 (29483883e 2025-08-04)\nhost: x86_64-unknown-linux-gnu",
        "cargo": "cargo 1.89.0 (c24e10642 2025-06-23)",
    }
    with pytest.raises(rust_toolchain.ToolchainError, match="not the reviewed"):
        rust_toolchain.baked(
            receipt,
            "registry.example/tauri:1",
            world.manifest_path,
            version_check=lambda *_: versions,
        )
    record = rust_toolchain.baked(
        receipt,
        "registry.example/tauri:1@" + world.manifest["ci_image_digest"],
        world.manifest_path,
        version_check=lambda *_: versions,
    )
    assert record["image_digest"] == world.manifest["ci_image_digest"]


def test_receipt_is_rechecked_against_the_active_toolchain(world):
    record = {
        "schema_version": 1,
        "kind": "verified-archives",
        "platform": "windows-x86_64",
        "version": world.manifest["version"],
        "channel_manifest_sha256": world.manifest["channel_manifest"]["sha256"],
        "components": [
            {key: item[key] for key in ("package", "url", "sha256", "size")}
            for item in world.manifest["platforms"]["windows-x86_64"]["components"]
        ],
        **world.versions,
    }
    rust_toolchain.check_receipt(
        record,
        world.manifest_path,
        platform_key="windows-x86_64",
        version_check=lambda *_: world.versions,
    )
    with pytest.raises(rust_toolchain.ToolchainError, match="differs"):
        rust_toolchain.check_receipt(
            record,
            world.manifest_path,
            platform_key="windows-x86_64",
            version_check=lambda *_: {**world.versions, "cargo": "cargo 1.90.0"},
        )
