"""Verify a Linux AppImage receipt and exercise its bundled CLI/export runtime."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def exactly_one(paths, description):
    paths = list(paths)
    if len(paths) != 1:
        names = ", ".join(str(path) for path in paths) or "none"
        raise SystemExit(f"Expected one {description}, found: {names}")
    return paths[0]


def run_checked(command, *, timeout=180, env=None):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise SystemExit(
            f"Command failed ({result.returncode}): {' '.join(map(str, command))}\n{details}"
        )
    return result


def exercise_native_startup(command, *, hold_seconds, env=None):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=hold_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        return {
            "native_startup_exercised": True,
            "native_startup_hold_seconds": hold_seconds,
        }
    details = (stderr or stdout).strip()
    raise SystemExit(
        f"Native desktop exited before the {hold_seconds:g}-second startup hold "
        f"(status {process.returncode}):\n{details}"
    )


def verify_receipt(artifact, expected_commit=None):
    folder = artifact.parent
    receipt = json.loads((folder / "BUILD-RECEIPT.json").read_text(encoding="utf-8"))
    if receipt["schema_version"] != 1 or receipt["platform"] != "linux":
        raise SystemExit("Build receipt is not a Linux schema-v1 receipt")
    if expected_commit and receipt["source_commit"] != expected_commit:
        raise SystemExit("Build receipt source commit does not match the requested commit")
    if expected_commit and receipt.get("source_dirty") is not False:
        raise SystemExit("Build receipt does not describe a clean source tree")
    item = exactly_one(
        (item for item in receipt["artifacts"] if item["file"] == artifact.name),
        "receipt entry for the AppImage",
    )
    if item["size"] != artifact.stat().st_size or item["sha256"] != digest(artifact):
        raise SystemExit("AppImage bytes do not match BUILD-RECEIPT.json")
    checksum_lines = (folder / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    expected_line = f"{item['sha256']}  {artifact.name}"
    if expected_line not in checksum_lines:
        raise SystemExit("AppImage checksum is missing from SHA256SUMS")
    return receipt


def prepared_fixture(fixture, temporary, use_crlf):
    if not use_crlf:
        return fixture
    copied = temporary / fixture.name
    text = fixture.read_text(encoding="utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    copied.write_bytes(normalized.replace("\n", "\r\n").encode())
    return copied


SANDBOX_REFUSAL = "browser_sandbox_unavailable"


def sandbox_refusal(result):
    """Return the bundled CLI's clear sandbox refusal, or None for any other result.

    On hosts whose policy denies Chromium's user-namespace sandbox (Ubuntu 23.10+
    without a profile for this path), the supported outcome is this coded
    failure. Chromium's own "--no-sandbox" advice must not reach the user.
    """
    error = result.get("error") or {}
    if result.get("status") != "failed" or error.get("code") != SANDBOX_REFUSAL:
        return None
    message = error.get("message", "")
    if "--no-sandbox" in message or "LINUX-INSTALL.md" not in message:
        raise SystemExit(f"Sandbox refusal message is not the supported guidance: {message}")
    return error


def run_export(command, *, accept_refusal=False):
    """Run the bundled CLI export; return its result and any accepted sandbox refusal."""
    if not accept_refusal:
        return json.loads(run_checked(command).stdout), None
    attempted = subprocess.run(command, capture_output=True, text=True, timeout=180)
    try:
        result = json.loads(attempted.stdout)
    except json.JSONDecodeError:
        result = {}
    if refused := sandbox_refusal(result):
        return result, refused
    if attempted.returncode:
        details = (attempted.stderr or attempted.stdout).strip()
        raise SystemExit(f"Command failed ({attempted.returncode}): {details}")
    return result, None


def verify_export(result, pdf, *, expected_figures=None, expected_text=None):
    if result["schema_version"] != 1 or result["status"] not in {
        "completed",
        "completed_with_warnings",
    }:
        raise SystemExit("Bundled CLI returned an invalid export result")
    documents = result["report"]["documents"]
    if len(documents) != 1:
        raise SystemExit(f"Bundled CLI reported {len(documents)} documents, expected one")
    if expected_figures is not None and documents[0]["figures"] != expected_figures:
        raise SystemExit(
            f"Bundled CLI reported {documents[0]['figures']} figures, expected {expected_figures}"
        )
    if not pdf.is_file() or pdf.stat().st_size < 10_000:
        raise SystemExit("Bundled CLI did not produce a substantive PDF")
    if expected_text:
        from pypdf import PdfReader

        pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
        if expected_text not in pdf_text:
            raise SystemExit(f"Bundled PDF is missing expected text: {expected_text}")
    return documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--expected-commit", default=os.environ.get("CI_COMMIT_SHA"))
    parser.add_argument(
        "--no-export",
        action="store_true",
        help="verify package metadata, binaries and the CLI without launching Chromium",
    )
    parser.add_argument(
        "--native-startup-seconds",
        type=float,
        help="launch the AppImage and require it to remain alive for this many seconds",
    )
    parser.add_argument(
        "--crlf-fixture",
        action="store_true",
        help="copy the Markdown fixture with Windows line endings before export",
    )
    parser.add_argument("--expect-figures", type=int)
    parser.add_argument("--expect-text")
    parser.add_argument(
        "--accept-sandbox-refusal",
        action="store_true",
        help="accept the CLI's clear sandbox refusal as the result when this host denies the "
        "bundled Chromium its sandbox; any other failure still fails",
    )
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    fixture = args.fixture.resolve()
    receipt = verify_receipt(artifact, args.expected_commit)
    startup = {"native_startup_exercised": False}
    if args.native_startup_seconds is not None:
        if args.native_startup_seconds <= 0:
            parser.error("--native-startup-seconds must be positive")
        startup = exercise_native_startup(
            [artifact],
            hold_seconds=args.native_startup_seconds,
            env={**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1"},
        )

    with tempfile.TemporaryDirectory(prefix="platefold-appimage-") as temporary:
        temporary = Path(temporary)
        subprocess.run([artifact, "--appimage-extract"], cwd=temporary, check=True)
        extracted = temporary / "squashfs-root"
        runtime = exactly_one(extracted.rglob("runtime/platefold"), "bundled CLI")
        browser = runtime.parent / receipt["bundled_browser"]["file"]
        if not browser.is_file():
            raise SystemExit("Bundled browser named by BUILD-RECEIPT.json is missing")
        browser_env = {**os.environ, "LD_LIBRARY_PATH": str(runtime.parent / "_internal")}
        dependencies = run_checked(["ldd", browser], env=browser_env)
        if "not found" in dependencies.stdout:
            raise SystemExit(f"Bundled browser has unresolved libraries:\n{dependencies.stdout}")
        browser_version = run_checked([browser, "--version"], env=browser_env).stdout.strip()
        version = run_checked([runtime, "--version"]).stdout.strip()
        catalog = run_checked([runtime, "--list-themes", "--json"])
        catalog_result = json.loads(catalog.stdout)
        if catalog_result["schema_version"] != 1 or catalog_result["status"] != "completed":
            raise SystemExit("Bundled CLI returned an invalid theme catalog")
        if args.no_export:
            print(
                json.dumps(
                    {
                        "artifact": artifact.name,
                        "artifact_sha256": digest(artifact),
                        "browser_sha256": digest(browser),
                        "browser_version": browser_version,
                        "export_exercised": False,
                        "source_commit": receipt["source_commit"],
                        "version_output": version,
                        **startup,
                    },
                    sort_keys=True,
                )
            )
            return
        export_fixture = prepared_fixture(fixture, temporary, args.crlf_fixture)
        output = temporary / "proof"
        command = [runtime, export_fixture, "-o", output, "--json"]
        result, refused = run_export(command, accept_refusal=args.accept_sandbox_refusal)
        if refused:
            print(
                json.dumps(
                    {
                        "artifact": artifact.name,
                        "artifact_sha256": digest(artifact),
                        "browser_version": browser_version,
                        "source_commit": receipt["source_commit"],
                        "export_exercised": True,
                        "export_status": "refused",
                        "error_code": refused["code"],
                        **startup,
                    },
                    sort_keys=True,
                )
            )
            return
        pdf = output / f"{export_fixture.stem}.pdf"
        documents = verify_export(
            result,
            pdf,
            expected_figures=args.expect_figures,
            expected_text=args.expect_text,
        )
        print(
            json.dumps(
                {
                    "artifact": artifact.name,
                    "artifact_sha256": digest(artifact),
                    "browser_version": browser_version,
                    "source_commit": receipt["source_commit"],
                    "version_output": version,
                    "pdf": pdf.name,
                    "pdf_sha256": digest(pdf),
                    "documents": len(documents),
                    "figures": documents[0]["figures"],
                    "export_status": result["status"],
                    "engine": result["report"].get("engine"),
                    "warnings": [
                        w for doc in result["report"]["documents"] for w in doc["warnings"]
                    ],
                    "export_exercised": True,
                    **startup,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
