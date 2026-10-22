"""Package resources, portable discovery, and user-file behavior."""

import importlib.util
import json
import re
import shutil
import tomllib
import warnings
from pathlib import Path

import pytest
import yaml

from platefold import __version__, browser, cdp, paths
from platefold.cli import build_settings, collect, find_settings_file, parse_args
from platefold.config import load_settings_file, make_settings, parse_settings_file
from platefold.mermaid import find_lib
from platefold.theme import COLOR_TOKENS, THEMES, discover_themes, get_theme
from tools import build_desktop


def write_theme(folder, name, **data):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    path.write_text(json.dumps({"name": name, **data}), encoding="utf-8")
    return path


def test_package_contains_renderer_and_license():
    resource = Path(find_lib(paths.resource_root()))
    assert resource.stat().st_size > 100_000
    assert resource.with_name("mermaid.LICENSE").is_file()


def test_package_contains_english_interface_fallback():
    path = Path(paths.resource_root()) / "locales" / "en.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    assert catalog["locale"] == "en"
    assert catalog["translation_method"] == "source"
    assert catalog["messages"]["app.tagline"] == "A little more room for the whole idea."


def test_source_distribution_manifest_contains_its_documented_build_inputs():
    manifest = (Path(__file__).parents[1] / "MANIFEST.in").read_text(encoding="utf-8")

    for name in (
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "requirements-build.txt",
        "requirements-runtime.txt",
        "setup.bat",
        "uv.lock",
    ):
        assert name in manifest
    assert "recursive-include packaging *.json *.py *.spec *.txt *.nsh *.plist" in manifest
    assert "include packaging/release-evidence.pub packaging/cosign.pub" in manifest


def test_container_requirements_cover_every_direct_runtime_dependency():
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = project["dependencies"] + project["optional-dependencies"]["server"]
    required = {
        re.match(r"[A-Za-z0-9_.-]+", dependency).group().lower().replace("_", "-")
        for dependency in dependencies
    }
    exported = {
        match.group(1).lower().replace("_", "-")
        for line in (root / "packaging/container/requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if (match := re.match(r"([A-Za-z0-9_.-]+)==", line))
    }

    assert required <= exported


def test_container_retains_the_cobble_license_omitted_from_its_wheel():
    root = Path(__file__).parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    license_name = "packaging/licenses/cobble-BSD-2-Clause.txt"

    assert license_name in dockerfile
    assert (root / license_name).is_file()


def chromium_credits_module():
    path = Path(__file__).parents[1] / "packaging/container/chromium_credits.py"
    spec = importlib.util.spec_from_file_location("chromium_credits", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_container_keeps_chromium_credits_as_a_file():
    root = Path(__file__).parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    target = "/usr/share/doc/platefold/chromium-credits.html.gz"

    assert "packaging/container/chromium_credits.py" in dockerfile
    assert target in dockerfile
    assert target in (root / "packaging/container/NOTICE.md").read_text(encoding="utf-8")


def test_chromium_credits_refuses_a_page_without_the_pdf_components():
    credits = chromium_credits_module()
    entry = '<span class="title">{}</span>'
    complete = "".join(entry.format(name) for name in credits.REQUIRED)
    complete += entry.format("filler") * credits.MINIMUM_ENTRIES

    assert credits.check(complete) == credits.MINIMUM_ENTRIES + len(credits.REQUIRED)
    with pytest.raises(SystemExit, match="PDFium"):
        credits.check(complete.replace(entry.format("PDFium"), ""))
    with pytest.raises(SystemExit, match="entries"):
        credits.check(complete[: len(complete) // 10])


def test_desktop_default_window_fits_a_1280_by_800_work_area():
    config_path = Path(__file__).parents[1] / "desktop/src-tauri/tauri.conf.json"
    window = json.loads(config_path.read_text(encoding="utf-8"))["app"]["windows"][0]
    assert window["width"] <= 1280
    assert window["height"] <= 700
    assert window["minHeight"] <= window["height"]


def test_desktop_window_zooms_with_only_the_zoom_permission_added():
    """Ctrl+plus/minus zoom the interface; the reflow tests cover the result.

    On macOS and Linux Tauri implements the hotkeys with a script that calls
    set_webview_zoom, so that one command is allowed and nothing broader.
    """
    root = Path(__file__).parents[1] / "desktop/src-tauri"
    window = json.loads((root / "tauri.conf.json").read_text(encoding="utf-8"))["app"]["windows"][0]
    assert window["zoomHotkeysEnabled"] is True
    capability = json.loads((root / "capabilities/main.json").read_text(encoding="utf-8"))
    assert capability["windows"] == ["main"]
    assert capability["permissions"] == [
        "core:event:allow-listen",
        "core:event:allow-unlisten",
        "core:webview:allow-set-webview-zoom",
    ]


def test_desktop_bundler_keeps_tools_inside_the_project():
    config_path = Path(__file__).parents[1] / "desktop/src-tauri/tauri.conf.json"
    bundle = json.loads(config_path.read_text(encoding="utf-8"))["bundle"]

    # A Windows service-account runner can have a restricted AppData directory,
    # so Tauri documents a project-local cache for this exact case.
    assert bundle["useLocalToolsDir"] is True


def test_native_desktop_artifact_contract_covers_each_supported_host():
    assert build_desktop.PLATFORMS == {
        "win32": {"name": "windows", "bundles": ["nsis"], "executable": "platefold.exe"},
        "linux": {"name": "linux", "bundles": ["appimage"], "executable": "platefold"},
        "darwin": {"name": "macos", "bundles": ["app", "dmg"], "executable": "platefold"},
    }


def test_native_desktop_bundle_declares_cross_platform_icons():
    root = Path(__file__).parents[1]
    config = json.loads((root / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    icons = config["bundle"]["icon"]
    assert {Path(icon).suffix for icon in icons} == {".png", ".icns", ".ico"}
    assert all((root / "desktop/src-tauri" / icon).is_file() for icon in icons)


def test_updater_artifact_generation_is_an_explicit_signed_build_mode():
    root = Path(__file__).parents[1]
    base = json.loads((root / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    updater = json.loads((root / "packaging/tauri-updater.conf.json").read_text(encoding="utf-8"))
    assert base["bundle"]["createUpdaterArtifacts"] is False
    assert updater["bundle"] == {"createUpdaterArtifacts": True}


def test_windows_build_summary_removes_private_context(monkeypatch, tmp_path):
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\qa")
    output = (
        f"error: could not compile {tmp_path / 'desktop'}\n"
        "C:\\Users\\qa\\.cargo\\registry\\source.rs\n"
        "download https://example.invalid/archive?token=value\n"
        "note: keep this compiler detail\n"
    )

    summary = build_desktop.sanitized_build_tail(output)

    assert str(tmp_path) not in summary
    assert "C:\\Users\\qa" not in summary
    assert "example.invalid" not in summary
    assert "token=value" not in summary
    assert "note: keep this compiler detail" in summary


def test_public_pull_request_ci_exercises_each_runtime_layer():
    root = Path(__file__).parents[1]
    path = root / ".github/workflows/ci.yml"
    text = path.read_text(encoding="utf-8")
    pipeline = yaml.load(text, Loader=yaml.BaseLoader)

    assert set(pipeline["on"]) == {"pull_request", "push", "workflow_dispatch"}
    assert set(pipeline["jobs"]) == {"python", "frontend", "browser", "desktop"}
    assert pipeline["jobs"]["python"]["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.13",
    ]
    assert "uv sync --locked --all-extras" in text
    assert "@sha256:" in pipeline["jobs"]["browser"]["steps"][-1]["run"]
    assert "runuser -u pwuser" in text
    for command in ("npm run build", "cargo test --locked", "cargo clippy --locked"):
        assert command in text


def test_public_workflows_pin_actions_to_commit_shas():
    workflows = Path(__file__).parents[1] / ".github/workflows"
    text = "\n".join(path.read_text(encoding="utf-8") for path in workflows.glob("*.yml"))

    assert not re.search(r"uses:\s+\S+@v\d", text)


def test_release_version_metadata_is_consistent():
    desktop_version = build_desktop.validated_desktop_version()

    assert desktop_version == __version__.replace(".dev", "-dev.")


@pytest.mark.parametrize(
    "machine,expected",
    [("AMD64", "x86_64"), ("x86_64", "x86_64"), ("aarch64", "arm64"), ("arm64", "arm64")],
)
def test_native_desktop_architecture_names_are_stable(machine, expected):
    assert build_desktop.normalized_arch(machine) == expected


def test_windows_keeps_the_existing_x64_download_name():
    assert build_desktop.artifact_arch("windows", "x86_64") == "x64"
    assert build_desktop.artifact_arch("linux", "x86_64") == "x86_64"
    assert build_desktop.artifact_arch("macos", "arm64") == "arm64"


def test_ci_source_state_uses_exact_pipeline_commit(monkeypatch):
    monkeypatch.setenv("CI_COMMIT_SHA", "a" * 40)
    assert build_desktop.source_state() == ("a" * 40, False)


def test_native_build_sets_the_environment_ci_flag(monkeypatch, tmp_path):
    observed = {}

    def record_run(command, **kwargs):
        observed.update(command=command, **kwargs)

    monkeypatch.setenv("CI", "false")
    monkeypatch.setattr(build_desktop.subprocess, "run", record_run)

    build_desktop.run_native_build(["tauri", "build", "--ci"], tmp_path)

    assert observed["command"] == ["tauri", "build", "--ci"]
    assert observed["cwd"] == tmp_path
    assert observed["check"] is True
    assert observed["env"]["CI"] == "true"


def test_bundled_browser_recognizes_playwright_names(tmp_path):
    browser = tmp_path / "_internal/platefold/browser/chrome-headless-shell"
    browser.parent.mkdir(parents=True)
    browser.touch()
    assert build_desktop.bundled_browser(tmp_path) == browser


def test_folder_collection_accepts_uppercase_extensions(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "Notes.MD").write_text("# Notes")
    (tmp_path / "ignore.txt").write_text("ignore")
    files, root = collect(str(tmp_path))
    assert files == [str(tmp_path / "nested" / "Notes.MD")]
    assert root == str(tmp_path)


def test_folder_collection_rejects_escaping_file_links_and_ignores_directory_links(
    tmp_path, symlinks
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("outside")
    source = tmp_path / "source"
    source.mkdir()
    (source / "ok.md").write_text("inside")
    (source / "loop").symlink_to(source, target_is_directory=True)
    (source / "other").symlink_to(outside, target_is_directory=True)
    assert collect(str(source))[0] == [str(source / "ok.md")]
    (source / "escape.md").symlink_to(outside / "secret.md")
    with pytest.raises(SystemExit, match="outside the source folder"):
        collect(str(source))


def test_sparse_user_theme_inherits_and_changes_cache_signature(tmp_path):
    write_theme(tmp_path / "themes", "custom", base="bond", colors={"diagram_fill": "#112233"})
    custom = get_theme("custom", tmp_path)
    base = get_theme("bond")
    assert custom.ink == base.ink
    assert custom.diagram_fill == "#112233"
    assert (
        make_settings(theme=custom).diagram_signature()
        != make_settings(theme=base).diagram_signature()
    )


def test_later_theme_layer_wins_and_reports_collision(tmp_path):
    write_theme(paths.user_theme_dir(), "bond", base="bond", colors={"ink": "#123456"})
    write_theme(tmp_path / "themes", "bond", base="bond", colors={"accent": "#654321"})
    with pytest.warns(UserWarning, match="overrides"):
        theme = get_theme("bond", tmp_path)
    assert theme.ink == "#123456"
    assert theme.accent == "#654321"
    assert Path(theme.source) == tmp_path / "themes/bond.json"


def override_warnings(call):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = call()
    return result, [str(w.message) for w in caught if "overrides" in str(w.message)]


@pytest.mark.parametrize(
    "spelling",
    ["themes/blue-notes.json", "./themes/blue-notes.json", "themes/../themes/blue-notes.json"],
)
def test_project_theme_passed_by_path_is_not_an_override_of_itself(tmp_path, spelling):
    project = write_theme(tmp_path / "themes", "blue-notes", base="bond", colors={"ink": "#123456"})
    for name in (spelling, str(project), "blue-notes"):
        theme, collisions = override_warnings(lambda name=name: get_theme(name, tmp_path))
        assert collisions == [], name
        assert theme.ink == "#123456"
        assert Path(theme.source).samefile(project)


def test_project_theme_by_path_on_the_command_line_does_not_warn(tmp_path):
    (tmp_path / "doc.md").write_text("# Doc\n", encoding="utf-8")
    write_theme(tmp_path / "themes", "blue-notes", base="bond", colors={"ink": "#123456"})
    args = parse_args([str(tmp_path / "doc.md"), "--theme", "themes/blue-notes.json"])
    (settings, _), collisions = override_warnings(lambda: build_settings(args, str(tmp_path)))
    assert collisions == []
    assert settings.theme.ink == "#123456"


def test_user_theme_passed_by_path_or_name_does_not_warn(tmp_path):
    user = write_theme(paths.user_theme_dir(), "mine", base="mono", colors={"ink": "#123456"})
    for name in (str(user), "mine"):
        theme, collisions = override_warnings(lambda name=name: get_theme(name, tmp_path))
        assert collisions == [], name
        assert Path(theme.source) == user


def test_builtin_theme_passed_by_path_does_not_warn(tmp_path):
    builtin = Path(THEMES["bond"].source)
    theme, collisions = override_warnings(lambda: get_theme(str(builtin), tmp_path))
    assert collisions == []
    assert theme.ink == THEMES["bond"].ink


def test_link_to_a_project_theme_is_the_same_file(tmp_path):
    project = write_theme(tmp_path / "themes", "blue-notes", base="bond", colors={"ink": "#123456"})
    link = tmp_path / "elsewhere.json"
    try:
        link.symlink_to(project)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    theme, collisions = override_warnings(lambda: get_theme(str(link), tmp_path))
    assert collisions == []
    assert theme.ink == "#123456"


def test_different_file_passed_by_path_still_reports_its_override(tmp_path):
    project = write_theme(tmp_path / "themes", "blue-notes", base="bond", colors={"ink": "#123456"})
    other = write_theme(tmp_path / "drafts", "blue-notes", base="bond", colors={"ink": "#654321"})
    theme, collisions = override_warnings(lambda: get_theme("drafts/blue-notes.json", tmp_path))
    assert collisions == [f"theme 'blue-notes': {other} overrides {project}"]
    assert theme.ink == "#654321"


def test_user_theme_passed_by_path_overrides_the_project_layer(tmp_path):
    user = write_theme(paths.user_theme_dir(), "bond", base="bond", colors={"ink": "#123456"})
    project = write_theme(tmp_path / "themes", "bond", base="bond", colors={"accent": "#654321"})
    theme, collisions = override_warnings(lambda: get_theme(str(user), tmp_path))
    # The explicit file keeps the strongest position, so the project layer is what it overrides.
    assert collisions == [
        f"theme 'bond': {project} overrides {Path(THEMES['bond'].source)}",
        f"theme 'bond': {user} overrides {project}",
    ]
    assert Path(theme.source) == user
    assert theme.ink == "#123456"
    assert theme.accent == "#654321"


def test_project_file_passed_by_path_wins_over_a_sibling_with_the_same_name(tmp_path):
    first = write_theme(tmp_path / "themes", "a", base="bond", colors={"ink": "#123456"})
    first.write_text(json.dumps({"name": "shared", "base": "bond", "colors": {"ink": "#123456"}}))
    second = write_theme(tmp_path / "themes", "b", base="bond", colors={"ink": "#654321"})
    second.write_text(json.dumps({"name": "shared", "base": "bond", "colors": {"ink": "#654321"}}))
    theme, collisions = override_warnings(lambda: get_theme("themes/a.json", tmp_path))
    assert collisions == [f"theme 'shared': {first} overrides {second}"]
    assert theme.ink == "#123456"
    by_name, collisions = override_warnings(lambda: get_theme("shared", tmp_path))
    assert collisions == [f"theme 'shared': {second} overrides {first}"]
    assert by_name.ink == "#654321"


def test_sparse_project_theme_passed_by_path_extends_the_user_layer_once(tmp_path):
    user = write_theme(paths.user_theme_dir(), "bond", base="bond", colors={"ink": "#123456"})
    project = write_theme(tmp_path / "themes", "bond", base="bond", colors={"accent": "#654321"})
    theme, collisions = override_warnings(lambda: get_theme("themes/bond.json", tmp_path))
    assert collisions == [
        f"theme 'bond': {user} overrides {Path(THEMES['bond'].source)}",
        f"theme 'bond': {project} overrides {user}",
    ]
    assert (theme.ink, theme.accent) == ("#123456", "#654321")


def test_explicit_theme_path_supports_spaces_and_relative_bases(tmp_path):
    path = write_theme(tmp_path / "with spaces", "mine", base="mono", colors={"ink": "#123456"})
    assert get_theme(str(path)).ink == "#123456"


@pytest.mark.parametrize(
    "data,offender",
    [
        ({"base": "bond", "colors": {"acccent": "#123456"}}, "acccent"),
        ({"base": "bond", "colors": {"ink": "red"}}, "colors.ink"),
        ({"base": "bond", "mode": "print"}, "mode"),
        ({"colors": {"ink": "#123456"}}, "missing theme colour"),
        ({"base": "absent"}, "unknown theme"),
        (
            {
                "base": "bond",
                "fonts": {"body": "url(https://example.org)", "head": "serif", "mono": "monospace"},
            },
            "fonts.body",
        ),
    ],
)
def test_invalid_user_theme_names_the_problem(tmp_path, data, offender):
    path = write_theme(tmp_path, "bad", **data)
    with pytest.raises(ValueError, match=offender):
        get_theme(str(path))


def test_cyclic_theme_bases_fail_with_a_useful_error(tmp_path):
    write_theme(tmp_path / "themes", "first", base="second")
    write_theme(tmp_path / "themes", "second", base="first")
    with pytest.raises(ValueError, match="cyclic theme base"):
        discover_themes(tmp_path)


def test_schema_link_never_controls_runtime_validation(tmp_path):
    path = write_theme(
        tmp_path, "mine", base="mono", **{"$schema": "https://example.invalid/remote.json"}
    )
    assert get_theme(str(path)).ink == THEMES["mono"].ink


@pytest.mark.parametrize(
    "format,text",
    [
        ("json", '{"page":{"preset":"a4"},"behaviour":{"image_dpi":150}}'),
        ("yaml", "page:\n  preset: a4\nbehaviour:\n  image_dpi: 150"),
    ],
)
def test_settings_formats_have_the_same_semantics(format, text):
    preset, overrides = parse_settings_file(text, format)
    settings = make_settings(preset, **overrides)
    assert settings.page_width == 210
    assert settings.image_dpi == 150


@pytest.mark.parametrize(
    "text,offender",
    [
        ('{"behaviour":{"toc":"false"}}', "behaviour.toc"),
        ('{"behaviour":{"image_dpi":-1}}', "image_dpi"),
        ('{"layout":{"inline_fit":1.1}}', "inline_fit"),
        ('{"page":false}', "page"),
        ('{"theme":[]}', "theme"),
        ("[]", "settings"),
    ],
)
def test_settings_reject_invalid_types_and_ranges(text, offender):
    with pytest.raises(ValueError, match=offender):
        parse_settings_file(text, "json")


@pytest.mark.parametrize(
    "overrides",
    [
        {"image_dpi": 0},
        {"margin": 200},
        {"page_width": float("nan")},
        {"caption_px": 2000},
        {"plate_fit": 0},
        {"toc": "false"},
    ],
)
def test_cli_and_python_overrides_cannot_bypass_validation(overrides):
    with pytest.raises(ValueError):
        make_settings(**overrides)


def test_multiple_settings_files_require_explicit_selection(tmp_path):
    (tmp_path / "platefold.json").write_text("{}")
    (tmp_path / "platefold.yml").write_text("")
    with pytest.raises(SystemExit, match="Multiple settings files"):
        find_settings_file(None, str(tmp_path))
    chosen = str(tmp_path / "platefold.yml")
    assert find_settings_file(chosen, str(tmp_path)) == chosen


def test_theme_paths_in_settings_are_relative_to_settings_file(tmp_path):
    write_theme(tmp_path / "palettes", "mine", base="mono", colors={"ink": "#123456"})
    config = tmp_path / "platefold.json"
    config.write_text('{"theme":{"base":"palettes/mine.json"}}')
    _, overrides = load_settings_file(str(config))
    assert overrides["theme"].ink == "#123456"


def test_cli_overrides_file_and_preserves_unspecified_values(tmp_path):
    (tmp_path / "platefold.json").write_text(
        '{"page":{"preset":"a4"},"behaviour":{"image_dpi":150}}'
    )
    settings, _ = build_settings(parse_args([str(tmp_path), "--dpi", "72"]), str(tmp_path))
    assert settings.page_width == 210
    assert settings.image_dpi == 72


@pytest.mark.parametrize(
    "platform,expected",
    [("linux", ".config"), ("darwin", "Library/Application Support"), ("win32", "AppData/Roaming")],
)
def test_default_config_directory_follows_platform(tmp_path, monkeypatch, platform, expected):
    # Invoke the real implementation, independently of the autouse isolation fixture.
    import importlib

    import platefold.paths as module

    original = module.config_dir
    importlib.reload(module)
    try:
        monkeypatch.setattr(module.sys, "platform", platform)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert Path(module.config_dir()) == tmp_path / expected / "platefold"
    finally:
        module.config_dir = original


def test_linux_browser_discovery_uses_path(monkeypatch):
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.setattr(
        browser.shutil, "which", lambda name: "/usr/bin/chromium" if name == "chromium" else None
    )
    assert browser.find_system("chromium") == "/usr/bin/chromium"
    assert browser.find_system("edge") is None


def test_snap_browser_profile_uses_snap_visible_home(tmp_path, monkeypatch):
    monkeypatch.setattr(cdp.Path, "home", classmethod(lambda cls: tmp_path))
    profile = Path(cdp._make_user_data_dir("/snap/bin/chromium"))
    try:
        assert profile.parent == tmp_path / "snap/chromium/common"
        assert profile.name.startswith("platefold-profile-")
    finally:
        shutil.rmtree(profile)


def test_regular_browser_profile_uses_system_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(cdp.tempfile, "tempdir", str(tmp_path))
    profile = Path(cdp._make_user_data_dir("/usr/bin/chromium"))
    try:
        assert profile.parent == tmp_path
    finally:
        shutil.rmtree(profile)


def test_active_browser_wait_checks_cancellation_between_short_polls():
    class WaitingSocket:
        def send(self, _message):
            pass

        def recv(self):
            raise cdp.websocket.WebSocketTimeoutException()

    connection = cdp.Connection.__new__(cdp.Connection)
    connection._ws = WaitingSocket()
    connection._next_id = 0
    checks = iter((False, False, True))
    connection._should_cancel = lambda: next(checks)

    with pytest.raises(cdp.CancelledError, match="cancelled"):
        connection.send("Page.printToPDF")
    assert cdp.CANCEL_POLL_INTERVAL <= 0.1


def test_every_public_palette_is_complete():
    assert len(THEMES) == 10
    assert {"bristol", "bond", "newsprint", "graphite", "midnight", "carbon"} <= THEMES.keys()
    for theme in THEMES.values():
        assert all(getattr(theme, token) for token in COLOR_TOKENS)


def test_desktop_build_receipt_binds_the_patched_updater_source():
    inputs = build_desktop.build_inputs()
    for name in (
        "desktop/src-tauri/Cargo.toml",
        "desktop/src-tauri/vendor/tauri-plugin-updater/src/updater.rs",
        "desktop/src-tauri/vendor/tauri-plugin-updater/UPSTREAM.json",
        "desktop/src-tauri/vendor/tauri-plugin-updater/LICENSE_MIT",
    ):
        assert len(inputs[name]) == 64
