"""Native builds use only the reviewed browser archives and AppImage tools."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.request
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

from tools import build_desktop
from tools import native_downloads as downloads

PLATFORM = "linux-x86_64"
SHELL = "chromium_headless_shell-1234"


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def entry(data: bytes, **fields) -> dict:
    return {**fields, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A manifest, the upstream bytes it names, and a fresh browsers path."""
    shell = zip_bytes(
        {
            "chrome-headless-shell-linux64/chrome-headless-shell": b"verified shell",
            "chrome-headless-shell-linux64/libGLESv2.so": b"verified library",
        }
    )
    ffmpeg = zip_bytes({"ffmpeg-linux": b"verified ffmpeg"})
    tools = {"AppRun-x86_64": b"apprun", "runtime-x86_64": b"type2 runtime"}
    upstream = {
        "https://cdn.example/builds/cft/1/linux64/chrome-headless-shell-linux64.zip": shell,
        "https://cdn.example/builds/ffmpeg/1011/ffmpeg-linux.zip": ffmpeg,
        **{f"https://tools.example/{name}": data for name, data in tools.items()},
    }
    manifest = {
        "schema_version": 1,
        "playwright": {"version": "1.62.0", "browsers": {}},
        "platforms": {
            PLATFORM: {
                "browser": [
                    entry(
                        shell,
                        directory=SHELL,
                        path="builds/cft/1/linux64/chrome-headless-shell-linux64.zip",
                        url="https://cdn.example/builds/cft/1/linux64/chrome-headless-shell-linux64.zip",
                    ),
                    entry(
                        ffmpeg,
                        directory="ffmpeg-1011",
                        path="builds/ffmpeg/1011/ffmpeg-linux.zip",
                        url="https://cdn.example/builds/ffmpeg/1011/ffmpeg-linux.zip",
                    ),
                ],
                "executable": f"{SHELL}/chrome-headless-shell-linux64/chrome-headless-shell",
                "appimage_tools": [
                    entry(data, file=name, url=f"https://tools.example/{name}")
                    for name, data in tools.items()
                ],
            }
        },
    }
    manifest_path = tmp_path / "native-downloads.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(downloads, "MANIFEST", manifest_path)
    monkeypatch.setattr(downloads, "host_platform", lambda *_: PLATFORM)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    monkeypatch.setattr(downloads, "tauri_tools_directory", lambda: tmp_path / "cache/tauri")
    fetched = []

    def download(url, destination):
        fetched.append(url)
        destination.write_bytes(upstream[url])

    return Namespace(
        path=tmp_path,
        manifest=manifest,
        manifest_path=manifest_path,
        upstream=upstream,
        download=download,
        fetched=fetched,
        receipt=tmp_path / "build/native-downloads.json",
    )


def playwright_like(extra: dict[str, bytes] | None = None, request_also=()):
    """Stand in for `playwright install`: fetch from the host, extract, mark complete."""
    calls = []

    def install(host, browsers):
        calls.append(host)
        assert re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", host)
        for path in request_also:
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{host}/{path}", timeout=10)
        for path, directory in (
            ("builds/cft/1/linux64/chrome-headless-shell-linux64.zip", SHELL),
            ("builds/ffmpeg/1011/ffmpeg-linux.zip", "ffmpeg-1011"),
        ):
            with urllib.request.urlopen(f"{host}/{path}", timeout=10) as response:
                data = response.read()
            target = browsers / directory
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                archive.extractall(target)
            (target / "INSTALLATION_COMPLETE").write_text("")
            (target / "DEPENDENCIES_VALIDATED").write_text("")
        (browsers / ".links").mkdir()
        for name, data in (extra or {}).items():
            (browsers / name).parent.mkdir(parents=True, exist_ok=True)
            (browsers / name).write_bytes(data)

    install.calls = calls
    return install


def prepare(world, installer=None):
    return downloads.prepare(
        world.receipt,
        world.manifest_path,
        download=world.download,
        installer=installer or playwright_like(),
        check_installed_playwright=lambda *_: None,
    )


def test_prepare_installs_only_verified_bytes_and_records_them(world):
    record = prepare(world)
    browsers = world.path / "browsers"
    executable = browsers / SHELL / "chrome-headless-shell-linux64/chrome-headless-shell"
    assert executable.read_bytes() == b"verified shell"
    assert (
        record["browser"]["executable"]["sha256"] == hashlib.sha256(b"verified shell").hexdigest()
    )
    assert [item["sha256"] for item in record["browser"]["archives"]] == [
        item["sha256"] for item in world.manifest["platforms"][PLATFORM]["browser"]
    ]
    assert json.loads(world.receipt.read_text()) == record
    # The AppImage tools replace whatever Tauri's cache held, executable.
    tools = world.path / "cache/tauri"
    assert (tools / "runtime-x86_64").read_bytes() == b"type2 runtime"
    if os.name != "nt":
        assert (tools / "AppRun-x86_64").stat().st_mode & 0o111
    assert record["appimage_tools"]["runtime_file"] == str(tools / "runtime-x86_64")
    assert not list(world.receipt.parent.glob("native-downloads-*"))


def test_a_changed_upstream_archive_installs_nothing(world):
    url = world.manifest["platforms"][PLATFORM]["browser"][0]["url"]
    world.upstream[url] = zip_bytes(
        {"chrome-headless-shell-linux64/chrome-headless-shell": b"evil"}
    )
    installer = playwright_like()
    with pytest.raises(downloads.DownloadError, match="is not the reviewed download"):
        prepare(world, installer)
    assert installer.calls == []
    assert not (world.path / "browsers").exists()
    assert not world.receipt.exists()


def test_rewriting_a_checked_archive_changes_nothing_installed(world):
    """A process rewrites the downloaded archives while the installer runs.

    Nothing that was checked is left on disk to change, and an archive put
    back where the download was is never served: the installer gets the bytes
    that were checked, and the receipt names them.
    """
    substitute = zip_bytes(
        {
            "chrome-headless-shell-linux64/chrome-headless-shell": b"substituted shell",
            "chrome-headless-shell-linux64/libGLESv2.so": b"verified library",
        }
    )
    install = playwright_like()

    def rewrite_then_install(host, browsers):
        (work,) = world.receipt.parent.glob("native-downloads-*")
        assert list(work.iterdir()) == []
        for index in range(2):
            (work / f"browser-{index}.zip").write_bytes(substitute)
        install(host, browsers)

    record = prepare(world, rewrite_then_install)
    executable = (
        world.path / "browsers" / SHELL / "chrome-headless-shell-linux64/chrome-headless-shell"
    )
    assert executable.read_bytes() == b"verified shell"
    assert (
        record["browser"]["executable"]["sha256"] == hashlib.sha256(b"verified shell").hexdigest()
    )


def test_an_installed_member_with_other_bytes_is_refused(world):
    install = playwright_like()

    def install_then_substitute(host, browsers):
        install(host, browsers)
        shell = browsers / SHELL / "chrome-headless-shell-linux64/chrome-headless-shell"
        shell.write_bytes(b"substituted shell")

    with pytest.raises(downloads.DownloadError, match="differs from its verified archive"):
        prepare(world, install_then_substitute)
    assert not world.receipt.exists()


def test_a_changed_appimage_runtime_is_refused(world):
    world.upstream["https://tools.example/runtime-x86_64"] = b"other runtime"
    with pytest.raises(downloads.DownloadError, match="runtime-x86_64 is not the reviewed"):
        prepare(world)
    assert not (world.path / "cache/tauri/runtime-x86_64").exists()


def test_a_stale_tool_in_the_cache_is_replaced(world):
    tools = world.path / "cache/tauri"
    tools.mkdir(parents=True)
    (tools / "runtime-x86_64").write_bytes(b"left by an earlier build")
    prepare(world)
    assert (tools / "runtime-x86_64").read_bytes() == b"type2 runtime"


def test_prepare_folds_in_a_rechecked_rust_receipt(world, monkeypatch):
    rust_receipt = world.path / "rust-toolchain.json"
    rust_record = {"kind": "verified-test-toolchain"}
    rust_receipt.write_text(json.dumps(rust_record))
    seen = []
    monkeypatch.setattr(
        downloads.rust_toolchain,
        "check_receipt",
        lambda record, platform_key: seen.append((record, platform_key)),
    )
    record = downloads.prepare(
        world.receipt,
        world.manifest_path,
        rust_receipt=rust_receipt,
        download=world.download,
        installer=playwright_like(),
        check_installed_playwright=lambda *_: None,
    )
    assert record["rust_toolchain"] == rust_record
    assert seen == [(rust_record, PLATFORM)]


def test_prepare_refuses_an_unverified_rust_receipt(world, monkeypatch):
    rust_receipt = world.path / "rust-toolchain.json"
    rust_receipt.write_text("{}")

    def refuse(*_args, **_kwargs):
        raise downloads.rust_toolchain.ToolchainError("unreviewed Rust")

    monkeypatch.setattr(downloads.rust_toolchain, "check_receipt", refuse)
    with pytest.raises(downloads.DownloadError, match="unreviewed Rust"):
        downloads.prepare(
            world.receipt,
            world.manifest_path,
            rust_receipt=rust_receipt,
            download=world.download,
            installer=playwright_like(),
            check_installed_playwright=lambda *_: None,
        )
    assert not world.receipt.exists()


@pytest.mark.parametrize("existing", ["chromium_headless_shell-1200/chrome", ".links/x"])
def test_the_browsers_path_must_start_empty(world, existing):
    path = world.path / "browsers" / existing
    path.parent.mkdir(parents=True)
    path.write_bytes(b"unverified")
    with pytest.raises(downloads.DownloadError, match="is not empty"):
        prepare(world)


def test_the_browsers_path_must_be_explicit(world, monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    with pytest.raises(downloads.DownloadError, match="PLAYWRIGHT_BROWSERS_PATH"):
        prepare(world)


def test_a_request_for_anything_but_the_verified_archives_is_refused(world, tmp_path):
    installer = playwright_like(request_also=["builds/winldd/1007/winldd-win64.zip"])
    with pytest.raises(downloads.DownloadError, match="requested"):
        downloads.prepare(
            tmp_path / "second.json",
            world.manifest_path,
            download=world.download,
            installer=installer,
            check_installed_playwright=lambda *_: None,
        )


def test_an_installed_file_that_is_not_an_archive_member_is_refused(world):
    extra = {f"{SHELL}/chrome-headless-shell-linux64/injected.so": b"not from the archive"}
    with pytest.raises(downloads.DownloadError, match="differs from its verified archive"):
        prepare(world, playwright_like(extra))


def test_an_unexpected_browser_directory_is_refused(world):
    with pytest.raises(downloads.DownloadError, match="Unexpected browser directories"):
        prepare(world, playwright_like({"chromium-1234/chrome": b"full chromium"}))


def test_the_receipt_is_never_overwritten(world):
    world.receipt.parent.mkdir(parents=True)
    world.receipt.write_text("{}")
    with pytest.raises(downloads.DownloadError, match="already exists"):
        prepare(world)


def installed_browsers(version="1.62.0", **overrides):
    browsers = {
        "chromium-headless-shell": {"revision": "1234", "browserVersion": "151.0.7922.34"},
        "ffmpeg": {"revision": "1011"},
        "winldd": {"revision": "1007"},
    }
    for name, fields in overrides.items():
        browsers[name.replace("_", "-")].update(fields)
    return version, [{"name": name, **fields} for name, fields in browsers.items()]


@pytest.mark.parametrize(
    "version,overrides,message",
    [
        ("1.63.0", {}, "Playwright 1.63.0 is installed"),
        ("1.62.0", {"chromium_headless_shell": {"revision": "1235"}}, "revision 1235"),
        ("1.62.0", {"chromium_headless_shell": {"browserVersion": "152.0"}}, "browserVersion"),
        ("1.62.0", {"winldd": {"revision": "1008"}}, "winldd revision 1008"),
        ("1.62.0", {"ffmpeg": {"revisionOverrides": {"mac14": "1"}}}, "overrides"),
    ],
)
def test_the_installed_playwright_must_want_the_reviewed_revisions(
    tmp_path, version, overrides, message
):
    version, browsers = installed_browsers(version, **overrides)
    browsers_json = tmp_path / "browsers.json"
    browsers_json.write_text(json.dumps({"browsers": browsers}))
    manifest = downloads.load_manifest()
    with pytest.raises(downloads.DownloadError, match=message):
        downloads.check_playwright(
            manifest, "windows-x86_64", installed=lambda: (version, browsers_json)
        )


def test_the_committed_playwright_pins_match_their_download_paths(tmp_path):
    version, browsers = installed_browsers()
    browsers_json = tmp_path / "browsers.json"
    browsers_json.write_text(json.dumps({"browsers": browsers}))
    manifest = downloads.load_manifest()
    for platform in manifest["platforms"]:
        downloads.check_playwright(manifest, platform, installed=lambda: (version, browsers_json))
    pinned = manifest["playwright"]["browsers"]
    for platform, entries in manifest["platforms"].items():
        browser = entries["browser"]
        assert (
            browser[0]["directory"]
            == f"chromium_headless_shell-{pinned['chromium-headless-shell']['revision']}"
        )
        assert f"/cft/{pinned['chromium-headless-shell']['browserVersion']}/" in browser[0]["path"]
        assert f"/ffmpeg/{pinned['ffmpeg']['revision']}/" in browser[1]["path"]
        assert entries["executable"].startswith(browser[0]["directory"] + "/")
        assert any("winldd" in item["path"] for item in browser) is platform.startswith("windows")
        for item in browser + entries.get("appimage_tools", []):
            assert re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) and item["size"] > 0
            assert item["url"].startswith("https://")
            if "path" in item:
                assert item["url"] == "https://cdn.playwright.dev/" + item["path"]
    tools = [item["file"] for item in manifest["platforms"]["linux-x86_64"]["appimage_tools"]]
    assert tools == [
        "AppRun-x86_64",
        "linuxdeploy-x86_64.AppImage",
        "linuxdeploy-plugin-gtk.sh",
        "linuxdeploy-plugin-gstreamer.sh",
        "linuxdeploy-plugin-appimage.AppImage",
        "runtime-x86_64",
    ]
    # Mutable branch names would let the plugin scripts change under the pin.
    assert not any(
        "/master/" in item["url"]
        for item in manifest["platforms"]["linux-x86_64"]["appimage_tools"]
    )


# --- the build's own check ------------------------------------------------------------


def test_check_receipt_accepts_the_verified_browser_and_names_the_runtime(world):
    record = prepare(world)
    executable = Path(record["browser"]["browsers_path"]) / record["browser"]["executable"]["file"]
    environment = downloads.check_receipt(record, str(executable), world.manifest_path)
    assert environment == {"LDAI_RUNTIME_FILE": record["appimage_tools"]["runtime_file"]}


@pytest.mark.parametrize(
    "tamper,message",
    [
        (lambda w, r: r.update(platform="windows-x86_64"), "another platform"),
        (lambda w, r: w.manifest_path.write_text(w.manifest_path.read_text() + " "), "predates"),
        (
            lambda w, r: (
                w.path / "browsers" / SHELL / "chrome-headless-shell-linux64/libGLESv2.so"
            ).write_bytes(b"x"),
            "changed after it was installed",
        ),
        (
            lambda w, r: (w.path / "cache/tauri/runtime-x86_64").write_bytes(b"x"),
            "runtime-x86_64 changed",
        ),
    ],
)
def test_check_receipt_refuses_anything_changed_since_it_was_verified(world, tamper, message):
    record = prepare(world)
    executable = Path(record["browser"]["browsers_path"]) / record["browser"]["executable"]["file"]
    tamper(world, record)
    with pytest.raises(downloads.DownloadError, match=message):
        downloads.check_receipt(record, str(executable), world.manifest_path)


def test_check_receipt_refuses_a_different_browser(world, tmp_path):
    record = prepare(world)
    other = tmp_path / "chrome-headless-shell"
    other.write_bytes(b"verified shell")
    with pytest.raises(downloads.DownloadError, match="not the verified browser"):
        downloads.check_receipt(record, str(other), world.manifest_path)
    with pytest.raises(downloads.DownloadError, match="not the verified browser"):
        downloads.check_receipt(record, None, world.manifest_path)


def test_build_desktop_records_the_verified_downloads(world, monkeypatch):
    record = prepare(world)
    record["rust_toolchain"] = {"kind": "verified-test-toolchain"}
    world.receipt.write_text(json.dumps(record))
    monkeypatch.setattr(
        downloads, "check_receipt", lambda receipt, browser: {"LDAI_RUNTIME_FILE": "/r"}
    )
    monkeypatch.delenv("LDAI_RUNTIME_FILE", raising=False)
    evidence = build_desktop.check_verified_downloads(world.receipt)
    assert evidence["verified_downloads"]["browser_archives"] == record["browser"]["archives"]
    assert evidence["verified_downloads"]["appimage_tools"] == record["appimage_tools"]["files"]
    assert evidence["verified_downloads"]["rust_toolchain"] == record["rust_toolchain"]
    assert "browsers_path" not in json.dumps(evidence)
    assert build_desktop.os.environ["LDAI_RUNTIME_FILE"] == "/r"
    assert build_desktop.check_verified_downloads(None) == {"verified_downloads": None}


def test_build_desktop_stops_on_an_unverified_browser(world, monkeypatch):
    record = prepare(world)
    record["rust_toolchain"] = {"kind": "verified-test-toolchain"}
    world.receipt.write_text(json.dumps(record))

    def refuse(receipt, browser):
        raise downloads.DownloadError("The build would bundle x, not the verified browser")

    monkeypatch.setattr(downloads, "check_receipt", refuse)
    with pytest.raises(SystemExit, match="not the verified browser"):
        build_desktop.check_verified_downloads(world.receipt)


def test_build_desktop_requires_a_verified_rust_toolchain(world):
    prepare(world)
    with pytest.raises(SystemExit, match="lacks a verified Rust toolchain"):
        build_desktop.check_verified_downloads(world.receipt)


@pytest.mark.parametrize(
    "flags",
    [["--updater-artifacts"], ["--gitlab-windows-signing", "--updater-artifacts"]],
)
def test_signed_and_updater_builds_require_verified_downloads(monkeypatch, flags):
    monkeypatch.setattr(build_desktop.sys, "platform", "win32")
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "throwaway")
    monkeypatch.setattr(build_desktop.sys, "argv", ["build_desktop.py", *flags])
    parser_errors = []

    class Parser:
        def error(self, message):
            parser_errors.append(message)
            raise SystemExit(2)

    args = Namespace(
        no_installer=False,
        updater_artifacts="--updater-artifacts" in flags,
        gitlab_windows_signing="--gitlab-windows-signing" in flags,
        attended_windows_signing=False,
        signing_preflight_only=False,
        verified_downloads=None,
    )
    with pytest.raises(SystemExit):
        build_desktop.validate_build_args(Parser(), args, build_desktop.PLATFORMS["win32"])
    assert parser_errors == ["Signed and updater builds require --verified-downloads"]
    args.verified_downloads = Path("build/native-downloads.json")
    build_desktop.validate_build_args(Parser(), args, build_desktop.PLATFORMS["win32"])


# --- the WebView2 installer the offline setup embeds ----------------------------------


def webview2_cache(tmp_path, count=1):
    tools = tmp_path / "tauri"
    paths = []
    for index in range(count):
        path = tools / f"x64/{index:04x}-id/MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"MZ WebView2 %d" % index)
        paths.append(path)
    return tools, paths


def test_a_microsoft_signed_webview2_installer_is_recorded(tmp_path):
    tools, (path,) = webview2_cache(tmp_path)
    seen = []

    def signer(target):
        seen.append(target)
        return {"status": "Valid", "signer": "Microsoft Corporation"}

    records = build_desktop.verify_webview2_installers(tools=tools, signer=signer)
    assert seen == [path]
    assert records == [
        {
            "file": f"0000-id/{path.name}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
            "signer": "Microsoft Corporation",
        }
    ]


@pytest.mark.parametrize(
    "signature",
    [
        {"status": "NotSigned", "signer": ""},
        {"status": "HashMismatch", "signer": "Microsoft Corporation"},
        {"status": "Valid", "signer": "Someone Else"},
        {},
    ],
)
def test_an_unsigned_or_foreign_webview2_installer_stops_the_build(tmp_path, signature):
    tools, _paths = webview2_cache(tmp_path, count=2)
    responses = iter([{"status": "Valid", "signer": "Microsoft Corporation"}, signature])
    with pytest.raises(SystemExit, match="not validly signed by Microsoft Corporation"):
        build_desktop.verify_webview2_installers(tools=tools, signer=lambda _: next(responses))


def test_the_offline_setup_must_have_had_a_webview2_installer_to_check(tmp_path):
    with pytest.raises(SystemExit, match="without a WebView2 installer"):
        build_desktop.verify_webview2_installers(tools=tmp_path / "tauri", signer=dict)
    assert (
        build_desktop.verify_webview2_installers(
            require=False, tools=tmp_path / "tauri", signer=dict
        )
        == []
    )


def test_the_webview2_cache_is_in_tauri_tools_directory(tmp_path, monkeypatch):
    tools, paths = webview2_cache(tmp_path)
    monkeypatch.setattr(downloads, "tauri_tools_directory", lambda: tools)
    assert build_desktop.webview2_offline_installers() == paths


def test_tauri_tools_live_in_the_cargo_target_directory_for_this_project(tmp_path):
    # tauri.conf.json sets bundle.useLocalToolsDir, so the bundler reads and
    # downloads its tools under <cargo target>/.tauri, not the user cache.
    project = downloads.TAURI_PROJECT
    target = tmp_path / "target"
    assert downloads.tauri_tools_directory(project, lambda _p: target) == target / ".tauri"
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    (elsewhere / "tauri.conf.json").write_text(json.dumps({"bundle": {}}))
    assert downloads.tauri_tools_directory(elsewhere, lambda _p: target) != target / ".tauri"
