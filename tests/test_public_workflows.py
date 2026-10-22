"""The public GitHub workflows: permissions, credentials and mirror behaviour."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def workflow(name: str) -> dict:
    # BaseLoader keeps "on" a string key instead of YAML 1.1's boolean.
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda path: path.name)
def test_workflows_default_to_read_only_and_never_persist_the_token(path):
    pipeline = workflow(path.name)
    assert pipeline["permissions"] == {"contents": "read"}
    for job in pipeline["jobs"].values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout@"):
                if path.name in {"ci.yml", "codeql.yml"}:
                    continue
                assert step["with"]["persist-credentials"] == "false"


def test_only_the_pages_deploy_job_can_write_pages():
    jobs = workflow("pages.yml")["jobs"]
    assert "permissions" not in jobs["build"]
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert jobs["deploy"]["environment"]["name"] == "github-pages"


def test_the_documentation_site_neither_needs_nor_serves_the_update_feeds():
    # The signed feeds live in their own repository and Pages site, so this
    # repository's contents, and its docs deploy, play no part in updates.
    pipeline = workflow("pages.yml")
    assert "updates/**" not in pipeline["on"]["push"]["paths"]
    for step in pipeline["jobs"]["build"]["steps"]:
        assert "stable.json" not in step.get("run", "") and "updates" not in step.get("run", "")
    assert not (ROOT / "updates").exists()
    policy = json.loads((ROOT / "packaging/update-policy.json").read_text(encoding="utf-8"))
    site = next(
        line.split(":", 1)[1].strip()
        for line in (ROOT / "mkdocs.yml").read_text(encoding="utf-8").splitlines()
        if line.startswith("site_url:")
    )
    for channel in ("stable", "prerelease"):
        url = policy[channel]
        assert url == f"https://maxfield-allison.github.io/platefold-updates/{channel}.json"
        assert not url.startswith(site)


def test_pypi_identity_is_isolated_from_project_code():
    jobs = workflow("publish-pypi.yml")["jobs"]
    verify, publish = jobs["verify"], jobs["publish"]
    assert verify["permissions"] == {"contents": "read"}
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["environment"]["name"] == "pypi"
    assert publish["needs"] == "verify"
    # The job holding the OIDC token checks out nothing and runs no project code.
    publish_uses = [step.get("uses", "") for step in publish["steps"]]
    assert not any(uses.startswith("actions/checkout@") for uses in publish_uses)
    assert any(uses.startswith("pypa/gh-action-pypi-publish@") for uses in publish_uses)
    text = (WORKFLOWS / "publish-pypi.yml").read_text(encoding="utf-8")
    assert "password:" not in text and "PYPI_API_TOKEN" not in text
    assert "tools/assemble_release.py verify" in text
    assert (ROOT / "tools/assemble_release.py").is_file()
    assert (ROOT / "packaging/release-evidence.pub").is_file()


def test_gitlab_sync_keeps_the_token_out_of_urls_and_names_no_host():
    pipeline = workflow("sync-to-gitlab.yml")
    text = (WORKFLOWS / "sync-to-gitlab.yml").read_text(encoding="utf-8")
    assert pipeline["jobs"]["sync"]["environment"] == "gitlab-mirror"
    assert "@${GITLAB_HOST}" not in text and "oauth2:${GITLAB_TOKEN}@" not in text
    assert "--force" not in text
    assert "probablyfine" not in text
    assert "GIT_CONFIG_VALUE_0" in text and "::add-mask::" in text


STRICT = "PLATEFOLD_REQUIRE_TEST_PREREQUISITES"


def step_using(job: dict, action: str) -> dict:
    return next(step for step in job["steps"] if step.get("uses", "").startswith(action + "@"))


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda path: path.name)
def test_every_action_is_pinned_to_a_full_commit(path):
    for job in workflow(path.name)["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def test_browser_and_native_jobs_receive_the_built_frontend():
    jobs = workflow("ci.yml")["jobs"]
    frontend = jobs["frontend"]
    build = next(s for s in frontend["steps"] if s.get("name") == "Build the desktop frontend")
    assert build["working-directory"] == "desktop"
    assert build["run"].splitlines()[:3] == [
        "npm ci",
        "npm audit --audit-level=high",
        "npm run build",
    ]
    upload = step_using(frontend, "actions/upload-artifact")["with"]
    # upload-artifact roots several paths at their common ancestor, desktop/.
    assert upload["path"].split() == [
        "desktop/dist/",
        "desktop/node_modules/axe-core/axe.min.js",
    ]
    assert upload["if-no-files-found"] == "error"
    for name in ("browser", "desktop"):
        job = jobs[name]
        assert job["needs"] == "frontend"
        download = step_using(job, "actions/download-artifact")
        assert download["with"] == {"name": upload["name"], "path": "desktop"}
        first_command = next(i for i, step in enumerate(job["steps"]) if "run" in step)
        assert job["steps"].index(download) < first_command
    # Neither job builds its own copy: the tested frontend is the uploaded one.
    assert "npm" not in str(jobs["browser"]) + str(jobs["desktop"])


def test_browser_tests_run_in_the_pinned_image_with_chromiums_sandbox():
    job = workflow("ci.yml")["jobs"]["browser"]
    command = job["steps"][-1]["run"].split()
    assert command[:3] == ["docker", "run", "--rm"]
    image = next(word for word in command if word.startswith("mcr.microsoft.com/playwright/"))
    assert re.fullmatch(r"\S+:v1\.62\.0-noble@sha256:[0-9a-f]{64}", image)
    # The image's published profile: Docker's default denies unshare.
    assert "seccomp=packaging/container/seccomp.json" in command
    assert (ROOT / "packaging/container/seccomp.json").is_file()
    # A full Chromium build exhausts Docker's default 64 MB /dev/shm mid-render.
    assert "--shm-size=1g" in command
    text = " ".join(command)
    for weakening in ("--privileged", "unconfined", "--no-sandbox", "--cap-add"):
        assert weakening not in text
    assert "runuser -u pwuser -- .venv/bin/python -m pytest -x -m integration" in text
    lift = next(s for s in job["steps"] if s.get("name") == "Let Chromium create its sandbox")
    assert "kernel.apparmor_restrict_unprivileged_userns=0" in lift["run"]
    assert job["steps"].index(lift) < len(job["steps"]) - 1


def test_ci_jobs_fail_rather_than_skip_when_a_prerequisite_is_missing():
    jobs = workflow("ci.yml")["jobs"]
    assert jobs["browser"]["env"][STRICT] == "1"
    # docker run passes the variable through from the job's environment.
    assert f"--env {STRICT} " in jobs["browser"]["steps"][-1]["run"]
    test = next(s for s in jobs["python"]["steps"] if s.get("name") == "Test")
    assert test["env"][STRICT] == "1"
    install = next(s for s in jobs["python"]["steps"] if s.get("name") == "Install Xvfb")
    assert "xvfb" in install["run"].split()


def test_missing_prerequisite_skips_locally_and_fails_under_ci(missing_prerequisite, monkeypatch):
    monkeypatch.delenv(STRICT, raising=False)
    with pytest.raises(pytest.skip.Exception, match="Build desktop/dist"):
        missing_prerequisite("Build desktop/dist first")
    monkeypatch.setenv(STRICT, "1")
    with pytest.raises(pytest.fail.Exception, match=f"Build desktop/dist.*{STRICT}=1"):
        missing_prerequisite("Build desktop/dist first")


@pytest.mark.parametrize(
    "module,test",
    [
        ("test_desktop_ui.py", "test_catalog_messages_are_never_treated_as_html"),
        ("test_ui_accessibility.py", "test_axe_finds_no_violations"),
    ],
)
def test_frontend_suites_fail_in_ci_without_a_built_frontend(tmp_path, module, test):
    """Run the real fixtures from a tree with no desktop/dist, as a bare checkout has."""
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("conftest.py", "test_desktop_ui.py", module):
        shutil.copyfile(ROOT / "tests" / name, tests / name)
    # The collection hook skips every browser test without the packaged Mermaid.
    (tmp_path / "platefold").symlink_to(ROOT / "platefold", target_is_directory=True)
    assert not (tmp_path / "desktop").exists()
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "-rs",
        "--rootdir",
        str(tmp_path),
        "-k",
        test,
        str(tests / module),
    ]
    env = {key: value for key, value in os.environ.items() if key != STRICT}

    local = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    ci = subprocess.run(
        command, cwd=tmp_path, env={**env, STRICT: "1"}, capture_output=True, text=True
    )

    assert local.returncode == 0, local.stdout[-2000:]
    assert "Build desktop/dist" in local.stdout and " skipped" in local.stdout
    assert ci.returncode == 1, ci.stdout[-2000:]
    assert f"Build desktop/dist with npm ci and npm run build first ({STRICT}=1" in ci.stdout
    assert " error" in ci.stdout and " passed" not in ci.stdout


@pytest.fixture
def sync(tmp_path, working_git):
    """Run the workflow's own script against a local bare repository."""
    if shutil.which("bash") is None:
        pytest.skip("requires bash")
    script = workflow("sync-to-gitlab.yml")["jobs"]["sync"]["steps"][1]["run"]
    https = 'remote="https://${GITLAB_HOST}/${GITLAB_PROJECT_PATH}.git"'
    assert https in script
    runner = tmp_path / "sync.sh"
    runner.write_text(script.replace(https, 'remote="${SIMULATED_REMOTE}"'), encoding="utf-8")
    mirror = tmp_path / "mirror.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    identity = [
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
    ]

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(source), *identity, *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def run(ref: str, **overrides: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "GITLAB_TOKEN": "placeholder-not-a-token",
            "GITLAB_HOST": "gitlab.example.invalid",
            "GITLAB_PROJECT_PATH": "group/project",
            "SIMULATED_REMOTE": str(mirror),
            "GITHUB_REF": ref,
            "GITHUB_REF_NAME": ref.rsplit("/", 1)[-1],
            **overrides,
        }
        return subprocess.run(
            ["bash", str(runner)], cwd=source, env=env, capture_output=True, text=True
        )

    def mirrored(ref: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(mirror), "rev-parse", "--verify", "-q", ref],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    return git, run, mirrored, source


def test_sync_fast_forwards_main_and_refuses_divergence(sync):
    git, run, mirrored, source = sync
    git("commit", "-q", "--allow-empty", "-m", "one")
    assert run("refs/heads/main").returncode == 0
    git("commit", "-q", "--allow-empty", "-m", "two")
    assert run("refs/heads/main").returncode == 0
    assert mirrored("refs/heads/public/main") == git("rev-parse", "HEAD")

    git("reset", "-q", "--hard", "HEAD~1")
    git("commit", "-q", "--allow-empty", "-m", "rewritten")
    result = run("refs/heads/main")
    assert result.returncode == 1 and "Refusing to rewrite" in result.stdout
    assert mirrored("refs/heads/public/main") != git("rev-parse", "HEAD")
    # The header lived only in the step's environment.
    assert "extraheader" not in (source / ".git/config").read_text()


def test_sync_mirrors_a_tag_once_and_never_moves_it(sync):
    git, run, mirrored, _source = sync
    git("commit", "-q", "--allow-empty", "-m", "one")
    git("tag", "-a", "v1.0.0", "-m", "v1.0.0")
    assert run("refs/tags/v1.0.0").returncode == 0
    assert mirrored("refs/tags/public/v1.0.0^{commit}") == git("rev-parse", "HEAD")
    assert mirrored("refs/tags/v1.0.0") == ""
    again = run("refs/tags/v1.0.0")
    assert again.returncode == 0 and "already mirrored" in again.stdout

    git("tag", "-d", "v1.0.0")
    git("commit", "-q", "--allow-empty", "-m", "two")
    git("tag", "v1.0.0")
    moved = run("refs/tags/v1.0.0")
    assert moved.returncode == 1 and "immutable" in moved.stdout


@pytest.mark.parametrize(
    "ref,overrides,message",
    [
        ("refs/heads/feature", {}, "Only main and v* tags"),
        ("refs/heads/main", {"GITLAB_HOST": "host/with path"}, "bare hostname"),
        ("refs/heads/main", {"GITLAB_TOKEN": ""}, "must be set"),
    ],
)
def test_sync_refuses_other_refs_and_malformed_settings(sync, ref, overrides, message):
    git, run, mirrored, _source = sync
    git("commit", "-q", "--allow-empty", "-m", "one")
    result = run(ref, **overrides)
    assert result.returncode == 1 and message in result.stdout
    assert mirrored("refs/heads/public/main") == ""


def test_sync_never_touches_the_private_projects_own_main(sync):
    git, run, mirrored, source = sync
    git("commit", "-q", "--allow-empty", "-m", "private build home")
    private = git("rev-parse", "HEAD")
    assert run("refs/heads/main").returncode == 0
    # Stand in for the private project's own main, then publish a new change.
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "push",
            "-q",
            str(source.parent / "mirror.git"),
            f"{private}:refs/heads/main",
        ],
        check=True,
    )
    git("commit", "-q", "--allow-empty", "-m", "public change")
    assert run("refs/heads/main").returncode == 0
    assert mirrored("refs/heads/main") == private
    assert mirrored("refs/heads/public/main") == git("rev-parse", "HEAD")
