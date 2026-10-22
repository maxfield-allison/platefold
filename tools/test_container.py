"""Qualify a local Docker image with offline CLI, browser interface and server exports.

Only creates/removes uniquely named disposable containers and volumes. No registry
writes or host configuration changes. Evidence is synthetic and contains no tokens.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

UI_CHECK = Path(__file__).with_name("container_ui_check.py")
# The address the server itself answers on; --origin below names the same one.
SERVER_ORIGIN = "http://127.0.0.1:8787"


def docker(*args, input=None):
    if args[0] == "run":
        args = ("run", "--pull=never", *args[1:])
    result = subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True, timeout=180
    )
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[-3000:]}")
    return result.stdout.strip()


def runtime(profile):
    return [
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--security-opt=seccomp={profile}",
        "--memory=3g",
        "--cpus=2",
        "--pids-limit=256",
        "--shm-size=256m",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=512m,mode=1777",
    ]


def request(origin, path, token=None, body=None, method=None, content_type=None):
    headers = {"Host": "127.0.0.1:8787"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(origin + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read()


def wait_ready(origin):
    for _ in range(100):
        try:
            request(origin, "/health/ready")
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise RuntimeError("Container server did not become ready")


def browser_interface(server, output):
    """Load the served interface in the image's own Chromium, inside the server container.

    The HTML check in server_export only proves the page is served. A bundle
    that throws while loading still serves it, so this loads it in a browser
    under the same confinement and fails on any page error or a blank page.
    """
    result = docker("exec", "-i", server, "python", "-", SERVER_ORIGIN, input=UI_CHECK.read_text())
    (output / "browser-interface.json").write_text(result + "\n")


def server_export(origin, token, output):
    assert b'<div id="app">' in request(origin, "/")
    try:
        request(origin, "/api/v1/capabilities")
        raise AssertionError("Unauthenticated API access succeeded")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    caps = json.loads(request(origin, "/api/v1/capabilities", token))
    assert caps["browser_adapter"] and caps["pdf_engine"]["discovered"]
    job = json.loads(
        request(
            origin,
            "/api/v1/jobs",
            token,
            json.dumps({"source": {"kind": "upload", "format": "markdown"}}).encode(),
            "POST",
            "application/json",
        )
    )
    path = "/api/v1/jobs/" + job["id"]
    request(
        origin,
        path + "/source",
        token,
        (
            "# Container export\n\nLanguage: en\n\n"
            "Hello, café, Ελληνικά, 日本語, 中文, 한국어.\n\n"
            "| Item | Result |\n| --- | --- |\n| Rendering | Verified |\n\n"
            "```mermaid\nflowchart LR\n accDescr: Container produces a PDF.\n"
            " Container --> PDF\n```\n"
        ).encode(),
        "PUT",
        "text/markdown",
    )
    for _ in range(300):
        job = json.loads(request(origin, path, token))
        if job["state"] in {"completed", "completed_with_warnings", "failed", "interrupted"}:
            break
        time.sleep(0.2)
    # Sora is Latin-only, so the Greek, CJK and Hangul text must be reported
    # as fallback, and the image's own fonts must supply every glyph.
    assert job["state"] == "completed_with_warnings", job
    manifest = json.loads(request(origin, path + "/results", token))
    for artifact in manifest["results"]:
        data = request(origin, artifact["url"], token)
        assert len(data) == artifact["bytes"]
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
        (output / artifact["name"]).write_bytes(data)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    document = json.loads((output / "report.json").read_text())["documents"][0]
    fallback = document["font_fallback"]
    assert fallback["status"] == "complete", fallback
    assert fallback["fallbacks"], fallback
    assert all(entry["fallback"] for entry in fallback["fallbacks"]), fallback
    scripts = {script for entry in fallback["fallbacks"] for script in entry["scripts"]}
    assert {"Greek", "Han", "Hangul"} <= scripts, fallback
    assert document["warnings"] == len(document["font_warnings"]), document
    assert all(w.startswith("FONT_FALLBACK:") for w in document["font_warnings"]), document
    return path, job["state"]


def cancel_export(origin, token):
    job = json.loads(
        request(
            origin,
            "/api/v1/jobs",
            token,
            json.dumps({"source": {"kind": "upload", "format": "markdown"}}).encode(),
            "POST",
            "application/json",
        )
    )
    path = "/api/v1/jobs/" + job["id"]
    request(
        origin,
        path + "/source",
        token,
        b"# Cancellation\n\n" + b"A paragraph for the disposable cancellation check.\n\n" * 20000,
        "PUT",
        "text/markdown",
    )
    for _ in range(100):
        state = json.loads(request(origin, path, token))["state"]
        if state == "running":
            break
        time.sleep(0.1)
    assert state == "running", state
    request(origin, path + "/cancel", token, b"", "POST")
    for _ in range(100):
        state = json.loads(request(origin, path, token))["state"]
        if state == "cancelled":
            return
        time.sleep(0.1)
    raise AssertionError("Cancellation did not finish")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--seccomp", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    profile = (
        args.seccomp or Path(__file__).resolve().parents[1] / "packaging/container/seccomp.json"
    )
    prefix = "platefold-test-" + secrets.token_hex(6)
    volume, server = prefix + "-data", prefix + "-server"
    created = False
    docker("volume", "create", volume)
    try:
        sandbox = docker(
            "run",
            "--rm",
            "--network=none",
            *runtime(profile),
            "--entrypoint=python",
            args.image,
            "/opt/platefold/sandbox_check.py",
        )
        (args.output / "sandbox.json").write_text(sandbox + "\n")
        token = secrets.token_urlsafe(32)
        # stdin only, no credential in arguments or logs. Image's /data ownership
        # initializes the disposable volume as UID 10001; no root helper needed.
        setup = (
            "import sys,os,pathlib; os.umask(0o077); "
            "pathlib.Path('/data/api.json').write_text(sys.stdin.read()); "
            "pathlib.Path('/data/input.md').write_text('# Offline container\\n\\nLanguage: en\\n\\n"
            "```mermaid\\nflowchart LR\\n accDescr: Offline source becomes a PDF.\\n Offline --> PDF\\n```\\n')"
        )
        docker(
            "run",
            "--rm",
            "-i",
            "--network=none",
            *runtime(profile),
            "-v",
            volume + ":/data",
            "--entrypoint=python",
            args.image,
            "-c",
            setup,
            input=json.dumps({"smoke": token}),
        )
        cli = docker(
            "run",
            "--rm",
            "--network=none",
            *runtime(profile),
            "-v",
            volume + ":/data",
            args.image,
            "/data/input.md",
            "-o",
            "/data/output",
            "--font",
            "sora",
            "--json",
        )
        (args.output / "cli.json").write_text(cli + "\n")
        cli_result = json.loads(cli)
        assert cli_result["status"] == "completed", cli_result
        docker(
            "run",
            "-d",
            "--name",
            server,
            *runtime(profile),
            "-p",
            os.environ.get("PLATEFOLD_TEST_BIND", "127.0.0.1") + "::8787",
            "-v",
            volume + ":/data",
            args.image,
            "serve",
            "--host",
            "0.0.0.0",
            "--data-dir",
            "/data/jobs",
            "--tokens-file",
            "/data/api.json",
            "--origin",
            SERVER_ORIGIN,
            "--limits-file",
            "/opt/platefold/limits.json",
        )
        created = True
        port = docker("port", server, "8787/tcp").rsplit(":", 1)[1]
        origin = "http://" + os.environ.get("PLATEFOLD_TEST_HOST", "127.0.0.1") + ":" + port
        wait_ready(origin)
        browser_interface(server, args.output)
        path, state = server_export(origin, token, args.output)
        # Inspect the actual PDF with its bundled parser. The CI driver only has
        # Python's standard library; no extra host dependency is needed here.
        # Chromium maps some shared CJK glyphs to compatibility radicals in
        # ToUnicode. NFKC compares their equivalent text while still rejecting
        # the null/missing glyphs produced when the fallback font is absent.
        docker(
            "exec",
            "-i",
            server,
            "python",
            "-c",
            "import base64, io, sys, unicodedata; from pypdf import PdfReader; "
            "pdf = PdfReader(io.BytesIO(base64.b64decode(sys.stdin.read()))); "
            "root = pdf.trailer['/Root']; "
            "assert root.get('/MarkInfo', {}).get('/Marked'); "
            "assert '/StructTreeRoot' in root; "
            "text = '\\n'.join(page.extract_text() for page in pdf.pages); "
            "text = unicodedata.normalize('NFKC', text); "
            "expected = ['café', 'Ελληνικά', '日本語', '中文', '한국어', 'Rendering']; "
            "missing = [value for value in expected if value not in text]; "
            "assert not missing, ('Missing rendered text', missing)",
            input=base64.b64encode((args.output / "document.pdf").read_bytes()).decode(),
        )
        cancel_export(origin, token)
        pending = json.loads(
            request(
                origin,
                "/api/v1/jobs",
                token,
                json.dumps({"source": {"kind": "upload", "format": "markdown"}}).encode(),
                "POST",
                "application/json",
            )
        )
        docker("restart", "--time=30", server)
        port = docker("port", server, "8787/tcp").rsplit(":", 1)[1]
        origin = "http://" + os.environ.get("PLATEFOLD_TEST_HOST", "127.0.0.1") + ":" + port
        wait_ready(origin)
        assert json.loads(request(origin, path, token))["state"] == state
        assert (
            json.loads(request(origin, "/api/v1/jobs/" + pending["id"], token))["state"]
            == "interrupted"
        )
        docker(
            "exec",
            server,
            "python",
            "-c",
            'import psutil; assert not any("chrome" in p.name().lower() for p in psutil.process_iter())',
        )
        # Validate built-in health command and actual container restrictions.
        docker("exec", server, "python", "/opt/platefold/healthcheck.py")
        inspection = json.loads(docker("inspect", server))[0]
        host = inspection["HostConfig"]
        assert host["ReadonlyRootfs"] and not host["Privileged"]
        assert inspection["Config"]["User"] == "10001:10001"
        assert host["CapDrop"] == ["ALL"] and host["PidsLimit"] == 256
        docker("cp", server + ":/data/output", str(args.output / "cli-output"))
        receipt = {
            "image": json.loads(docker("image", "inspect", args.image))[0]["Id"],
            "docker_version": docker("version", "--format", "{{.Server.Version}}"),
            "uid": 10001,
            "read_only_root": True,
            "privileged": False,
            "cap_drop": host["CapDrop"],
            "security_options": [value.split("=", 1)[0] for value in host["SecurityOpt"]],
            "apparmor_profile": inspection["AppArmorProfile"],
            "offline_cli": "passed",
            "browser_interface_starts": "passed",
            "authenticated_web_export": "passed",
            "tagged_pdf_multilingual_text": "passed",
            "completed_result_after_restart": "passed",
            "unfinished_upload_after_restart": "interrupted",
            "cancel_render_and_reap_browser": "passed",
            "seccomp_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
        }
        (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))
    finally:
        if created:
            (args.output / "server.log").write_text(docker("logs", server))
            docker("rm", "-f", server)
        docker("volume", "rm", volume)


if __name__ == "__main__":
    main()
