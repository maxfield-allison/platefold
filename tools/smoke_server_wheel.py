"""Exercise an installed server wheel outside its source checkout with a real PDF."""

import argparse
import hashlib
import json
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def wait_ready(request):
    deadline = time.monotonic() + 15
    while True:
        try:
            request("/health/ready")
            break
        except (urllib.error.URLError, OSError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Installed server did not become ready") from None
            time.sleep(0.1)


def exercise(origin, credential, output):
    def request(path, data=None, *, method=None, content_type=None):
        headers = {"Authorization": "Bearer " + credential}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(origin + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.read()

    wait_ready(request)
    assert b'<div id="app">' in request("/")
    caps = json.loads(request("/api/v1/capabilities"))
    assert caps["browser_adapter"] and caps["pdf_engine"]["discovered"]
    created = json.loads(
        request(
            "/api/v1/jobs",
            json.dumps({"source": {"kind": "upload", "format": "markdown"}}).encode(),
            method="POST",
            content_type="application/json",
        )
    )
    path = f"/api/v1/jobs/{created['id']}"
    request(
        path + "/source",
        b"# Installed browser server\n\nLanguage: en\n\n```mermaid\nflowchart LR\n accDescr: Source becomes a PDF.\n Source --> PDF\n```\n",
        method="PUT",
        content_type="text/markdown",
    )
    deadline = time.monotonic() + 60
    while True:
        job = json.loads(request(path))
        if job["state"] in {
            "completed",
            "completed_with_warnings",
            "failed",
            "interrupted",
            "cancelled",
        }:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Installed export did not finish")
        time.sleep(0.2)
    assert job["state"] == "completed", job
    manifest = json.loads(request(path + "/results"))
    output.mkdir(parents=True, exist_ok=True)
    for artifact in manifest["results"]:
        content = request(artifact["url"])
        assert len(content) == artifact["bytes"]
        assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
        if artifact["media_type"] == "application/pdf":
            assert content.startswith(b"%PDF-")
        (output / Path(artifact["name"]).name).write_bytes(content)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("Installed wheel served the shared UI and exported a verified PDF and report")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "python", type=Path, help="Python in the environment containing the installed server wheel"
    )
    parser.add_argument("output", type=Path, help="Directory for synthetic PDF evidence")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="platefold-installed-server-") as temporary:
        root = Path(temporary)
        credential = secrets.token_urlsafe(32)
        token_file = root / "credentials.json"
        token_file.touch(mode=0o600)
        token_file.write_text(json.dumps({"smoke": credential}))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            [
                str(args.python.absolute()),
                "-I",
                "-m",
                "platefold",
                "serve",
                "--data-dir",
                str(root / "jobs"),
                "--tokens-file",
                str(token_file),
                "--port",
                str(port),
            ],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            exercise(f"http://127.0.0.1:{port}", credential, args.output)
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
