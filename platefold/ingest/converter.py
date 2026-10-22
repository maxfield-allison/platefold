"""Convert legacy office documents through an operator-installed converter.

The converter is only a bridge to DOCX.  Plate|Fold's bounded DOCX ingester
remains the semantic and archive-security boundary for the generated file.
"""

from __future__ import annotations

import contextlib
import os
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..cancellation import CancelledError
from ..config import Settings
from ..limits import MIB, ResourceLimitError
from . import Ingested, locate
from .docx import ingest as ingest_docx

CONVERTER_OUTPUT_BYTES = 1024 * 1024
PANDOC_PROBE_DOCX_BYTES = 8 * MIB
CONVERTER_ERROR_DETAIL_CHARS = 4000
PROBE_SECONDS = 10.0
POLL_SECONDS = 0.05
TEMPORARY_OVERHEAD_BYTES = 64 * MIB

_LIBREOFFICE_FILTERS = {
    ".doc": "MS Word 97",
    ".odt": "writer8",
    ".rtf": "Rich Text Format",
}
_PANDOC_INPUTS = {".odt": "odt", ".rtf": "rtf"}


class ExternalConverterError(ValueError):
    """A stable converter error code plus English fallback message fields."""

    def __init__(self, code: str, message: str, **fields: str | int | float) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


@dataclass(frozen=True)
class ConverterRoute:
    name: str
    executable: str
    version: str
    input_format: str


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    output: str


class _OutputReader(threading.Thread):
    def __init__(self, stream, limit: int) -> None:
        super().__init__(name="platefold-converter-output", daemon=True)
        self.stream = stream
        self.limit = limit
        self.output = bytearray()
        self.total = 0
        self.too_large = threading.Event()

    def run(self) -> None:
        try:
            while chunk := self.stream.read(64 * 1024):
                self.total += len(chunk)
                remaining = self.limit - len(self.output)
                if remaining > 0:
                    self.output.extend(chunk[:remaining])
                if self.total > self.limit:
                    self.too_large.set()
        finally:
            with contextlib.suppress(Exception):
                self.stream.close()


class _ProcessTree:
    """Own a subprocess tree on POSIX and Windows.

    POSIX uses a fresh session/process group. Windows assigns the process to
    a kill-on-close Job Object and fails closed if assignment is unavailable.
    """

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self._windows_job = self._assign_windows_job() if os.name == "nt" else None
        if os.name == "nt" and self._windows_job is None:  # pragma: no cover - native Windows
            self.process.kill()
            self.process.wait()
            if self.process.stdout is not None:
                self.process.stdout.close()
            raise ExternalConverterError(
                "converter_containment_unavailable",
                "Windows could not place the external converter in an owned Job Object",
            )

    def _assign_windows_job(self):  # pragma: no cover - exercised by native Windows CI
        import ctypes
        from ctypes import wintypes

        kernel32 = _windows_kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class BASIC_LIMITS(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMITS),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        limits = EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            job,
            wintypes.HANDLE(self.process._handle),  # type: ignore[attr-defined]
        )
        if assigned:
            return job
        kernel32.CloseHandle(job)
        return None

    def terminate(self) -> None:
        if os.name == "nt":  # pragma: no cover - exercised by native Windows CI
            if self._windows_job is not None:
                _windows_kernel32().TerminateJobObject(self._windows_job, 1)
            else:
                self.process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=1)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=0.5)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=1)

    def close(self) -> None:
        if self._windows_job is not None:  # pragma: no cover - native Windows CI
            _windows_kernel32().CloseHandle(self._windows_job)
            self._windows_job = None


def _windows_kernel32():  # pragma: no cover - exercised by native Windows CI
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    cancel_check: Callable[[], None],
    output_limit: int = CONVERTER_OUTPUT_BYTES,
    watched_output: Path | None = None,
    watched_output_limit: int | None = None,
    watched_root: Path | None = None,
    temporary_byte_limit: int | None = None,
    temporary_entry_limit: int | None = None,
) -> _ProcessResult:
    """Run argv without a shell, bounding time, captured output and descendants."""
    if timeout <= 0:
        cancel_check()
        raise ExternalConverterError(
            "converter_timeout",
            "External conversion exceeded the export time limit",
            timeout_seconds=0,
        )
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
    except OSError as exc:
        raise ExternalConverterError(
            "converter_unavailable",
            f"External converter could not be started: {Path(argv[0]).name}",
            converter=Path(argv[0]).name,
        ) from exc
    assert process.stdout is not None
    tree = _ProcessTree(process)
    reader = _OutputReader(process.stdout, output_limit)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            cancel_check()
            _enforce_resource_bounds(
                reader,
                output_limit=output_limit,
                watched_output=watched_output,
                watched_output_limit=watched_output_limit,
                watched_root=watched_root,
                temporary_byte_limit=temporary_byte_limit,
                temporary_entry_limit=temporary_entry_limit,
            )
            if time.monotonic() >= deadline:
                raise ExternalConverterError(
                    "converter_timeout",
                    f"External converter exceeded the {timeout:g}-second limit",
                    timeout_seconds=timeout,
                )
            time.sleep(POLL_SECONDS)
        reader.join(timeout=0.5)
        if reader.is_alive():
            tree.terminate()
            reader.join(timeout=1)
            raise ExternalConverterError(
                "converter_descendant_lingered",
                "External converter left a descendant process holding its output open",
            )
        # Check once more after process exit. A converter can create output
        # between the last poll and exit, and that final burst remains bounded.
        _enforce_resource_bounds(
            reader,
            output_limit=output_limit,
            watched_output=watched_output,
            watched_output_limit=watched_output_limit,
            watched_root=watched_root,
            temporary_byte_limit=temporary_byte_limit,
            temporary_entry_limit=temporary_entry_limit,
        )
        return _ProcessResult(
            process.returncode,
            bytes(reader.output).decode("utf-8", errors="replace"),
        )
    finally:
        # A converter may daemonize and let its immediate process return.  The
        # isolated profile is disposable, so no descendant is allowed to live
        # beyond this call even after a successful conversion.
        tree.terminate()
        reader.join(timeout=1)
        tree.close()


def _enforce_resource_bounds(
    reader: _OutputReader,
    *,
    output_limit: int,
    watched_output: Path | None,
    watched_output_limit: int | None,
    watched_root: Path | None,
    temporary_byte_limit: int | None,
    temporary_entry_limit: int | None,
) -> None:
    if reader.too_large.is_set():
        raise ExternalConverterError(
            "converter_output_too_large",
            f"External converter output exceeded the {output_limit}-byte limit",
            limit=output_limit,
        )
    if watched_output is not None and watched_output.exists():
        info = watched_output.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ExternalConverterError(
                "converter_output_escaped",
                "External converter produced a linked or escaping DOCX output",
            )
        if not stat.S_ISREG(info.st_mode):
            raise ExternalConverterError(
                "converter_output_invalid",
                "External converter produced a special DOCX output",
            )
        if watched_output_limit is not None and info.st_size > watched_output_limit:
            raise ExternalConverterError(
                "converter_output_too_large",
                f"Converted DOCX exceeded the {watched_output_limit}-byte limit",
                limit=watched_output_limit,
            )
    if watched_root is None:
        return
    _temporary_usage(
        watched_root,
        entry_limit=temporary_entry_limit,
        byte_limit=temporary_byte_limit,
    )


def _temporary_usage(
    root: Path,
    *,
    entry_limit: int | None = None,
    byte_limit: int | None = None,
) -> tuple[int, int]:
    """Bound temporary entries and bytes without following converter-created links."""
    entries = 0
    size = 0
    for directory, folders, files in os.walk(root, followlinks=False):
        entries += len(folders) + len(files)
        if entry_limit is not None and entries > entry_limit:
            raise ResourceLimitError(
                f"External converter temporary files exceeded the {entry_limit}-entry limit"
            )
        for name in [*folders, *files]:
            try:
                size += Path(directory, name).lstat().st_size
            except FileNotFoundError:
                # The converter may atomically replace its own temporary file
                # between the directory listing and lstat.
                continue
            if byte_limit is not None and size > byte_limit:
                raise ResourceLimitError(
                    f"External converter temporary files exceeded the {byte_limit}-byte limit"
                )
    return entries, size


def _private_environment(work: Path) -> dict[str, str]:
    """Build a small environment whose writable user paths stay in the workspace."""
    keep = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TZ"}
    }
    home = work / "home"
    config = work / "config"
    cache = work / "cache"
    data = work / "data"
    temporary = work / "tmp"
    for directory in (home, config, cache, data, temporary):
        directory.mkdir(mode=0o700)
    keep.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(config),
            "LOCALAPPDATA": str(data),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
            "XDG_DATA_HOME": str(data),
            "TMPDIR": str(temporary),
            "TEMP": str(temporary),
            "TMP": str(temporary),
        }
    )
    return keep


def _remaining(deadline: float, cancel_check: Callable[[], None]) -> float:
    cancel_check()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ExternalConverterError(
            "converter_timeout",
            "External conversion exceeded the export time limit",
            timeout_seconds=0,
        )
    return remaining


def _probe(
    argv: list[str],
    *,
    work: Path,
    env: dict[str, str],
    deadline: float,
    cancel_check: Callable[[], None],
) -> _ProcessResult:
    return _run_bounded(
        argv,
        cwd=work,
        env=env,
        timeout=min(PROBE_SECONDS, _remaining(deadline, cancel_check)),
        cancel_check=cancel_check,
    )


def _version_line(output: str, fallback: str) -> str:
    return next((line.strip()[:240] for line in output.splitlines() if line.strip()), fallback)


def _detect_routes(
    suffix: str,
    *,
    work: Path,
    env: dict[str, str],
    deadline: float,
    cancel_check: Callable[[], None],
) -> tuple[list[ConverterRoute], list[str]]:
    """Qualify the first working LibreOffice and Pandoc among the candidates.

    `locate` lists ``PATH`` first, then the standard install locations.
    """
    routes: list[ConverterRoute] = []
    failures: list[str] = []

    def fail(message: str) -> None:
        if message not in failures:
            failures.append(message)

    for libreoffice in locate.libreoffice(env.get("PATH")):
        try:
            result = _probe(
                [libreoffice, "--version"],
                work=work,
                env=env,
                deadline=deadline,
                cancel_check=cancel_check,
            )
        except ExternalConverterError as exc:
            fail(f"LibreOffice version probe failed: {exc}")
            continue
        if result.returncode == 0:
            routes.append(
                ConverterRoute(
                    "libreoffice",
                    libreoffice,
                    _version_line(result.output, "LibreOffice (version not reported)"),
                    suffix.removeprefix("."),
                )
            )
            break
        fail("LibreOffice version probe failed")

    if suffix in _PANDOC_INPUTS:
        for index, pandoc in enumerate(locate.pandoc(env.get("PATH"))):
            try:
                route = _qualify_pandoc(
                    pandoc,
                    suffix,
                    probe_dir=work / f"pandoc-docx-probe-{index}",
                    env=env,
                    deadline=deadline,
                    cancel_check=cancel_check,
                )
            except ExternalConverterError as exc:
                fail(f"Pandoc capability probe failed: {exc}")
                continue
            if isinstance(route, ConverterRoute):
                routes.append(route)
                break
            fail(route)
    return routes, failures


def _qualify_pandoc(
    pandoc: str,
    suffix: str,
    *,
    probe_dir: Path,
    env: dict[str, str],
    deadline: float,
    cancel_check: Callable[[], None],
) -> ConverterRoute | str:
    """A route for a Pandoc that proves sandboxed DOCX writing, or why not."""
    work = probe_dir.parent

    def probe(*arguments: str) -> _ProcessResult:
        return _probe(
            [pandoc, *arguments], work=work, env=env, deadline=deadline, cancel_check=cancel_check
        )

    version = probe("--version")
    inputs = probe("--list-input-formats")
    outputs = probe("--list-output-formats")
    help_result = probe("--help")
    usable = (
        version.returncode == inputs.returncode == outputs.returncode == help_result.returncode == 0
        and _PANDOC_INPUTS[suffix] in set(inputs.output.split())
        and "docx" in set(outputs.output.split())
        and "--sandbox" in help_result.output
    )
    if not usable:
        return f"Pandoc does not advertise sandboxed {suffix} to DOCX conversion"
    probe_dir.mkdir(mode=0o700)
    probe_source = probe_dir / "probe.rtf"
    probe_output = probe_dir / "probe.docx"
    probe_source.write_text(r"{\rtf1\ansi PlateFold capability probe}", encoding="ascii")
    writer = _run_bounded(
        [
            pandoc,
            "--sandbox",
            "--from=rtf",
            "--to=docx",
            f"--output={probe_output}",
            str(probe_source),
        ],
        cwd=probe_dir,
        env=env,
        timeout=min(PROBE_SECONDS, _remaining(deadline, cancel_check)),
        cancel_check=cancel_check,
        watched_output=probe_output,
        watched_output_limit=PANDOC_PROBE_DOCX_BYTES,
    )
    if (
        writer.returncode == 0
        and probe_output.is_file()
        and not probe_output.is_symlink()
        and probe_output.read_bytes()[:4] == b"PK\x03\x04"
    ):
        return ConverterRoute(
            "pandoc",
            pandoc,
            _version_line(version.output, "pandoc (version not reported)"),
            _PANDOC_INPUTS[suffix],
        )
    detail = _scrub_output(writer.output, work) or "invalid DOCX output"
    return f"Pandoc sandboxed DOCX self-test failed: {detail}"


def _copy_source(
    source: Path,
    destination: Path,
    limit: int,
    cancel_check: Callable[[], None],
) -> None:
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ExternalConverterError(
            "converter_input_unreadable", f"Could not read converter input: {source.name}"
        ) from exc
    if size > limit:
        raise ResourceLimitError(f"Converter input exceeds the {limit}-byte archive limit")
    copied = 0
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        while chunk := incoming.read(64 * 1024):
            cancel_check()
            copied += len(chunk)
            if copied > limit:
                raise ResourceLimitError(f"Converter input exceeds the {limit}-byte archive limit")
            outgoing.write(chunk)
    destination.chmod(stat.S_IRUSR)


def _scrub_output(output: str, work: Path) -> str:
    text = output.replace(str(work), "<temporary>")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())[
        :CONVERTER_ERROR_DETAIL_CHARS
    ]


def _converter_messages(output: str, work: Path) -> list[tuple[str, str]]:
    messages = []
    for line in _scrub_output(output, work).splitlines():
        if any(word in line.casefold() for word in ("warning", "error", "failed", "failure")):
            messages.append(("EXTERNAL_CONVERTER_MESSAGE", line))
    return messages[:20]


def _argv(route: ConverterRoute, source: Path, destination: Path, profile: Path) -> list[str]:
    if route.name == "libreoffice":
        output_filter = "docx:Office Open XML Text"
        return [
            route.executable,
            "--headless",
            "--safe-mode",
            "--nologo",
            "--nodefault",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            f"--infilter={_LIBREOFFICE_FILTERS[source.suffix.lower()]}",
            "--convert-to",
            output_filter,
            "--outdir",
            str(destination.parent),
            str(source),
        ]
    return [
        route.executable,
        "--sandbox",
        f"--from={route.input_format}",
        "--to=docx",
        f"--output={destination}",
        str(source),
    ]


def _validate_output(output_dir: Path, expected: Path, limit: int) -> None:
    with os.scandir(output_dir) as iterator:
        entries = [Path(entry.path) for _, entry in zip(range(2), iterator, strict=False)]
    if entries == [expected] and expected.is_symlink():
        raise ExternalConverterError(
            "converter_output_escaped",
            "External converter produced a linked or escaping DOCX output",
        )
    if entries == [expected] and not expected.is_file():
        raise ExternalConverterError(
            "converter_output_invalid",
            "External converter produced a special DOCX output",
        )
    if entries != [expected]:
        raise ExternalConverterError(
            "converter_output_invalid",
            "External converter did not produce exactly one confined DOCX output",
        )
    if not expected.resolve().is_relative_to(output_dir.resolve()) or expected.stat().st_nlink != 1:
        raise ExternalConverterError(
            "converter_output_escaped",
            "External converter produced a linked or escaping DOCX output",
        )
    if expected.stat().st_size > limit:
        raise ExternalConverterError(
            "converter_output_too_large",
            f"Converted DOCX exceeded the {limit}-byte limit",
            limit=limit,
        )


def _convert_with_route(
    route: ConverterRoute,
    source: Path,
    *,
    settings: Settings,
    work: Path,
    env: dict[str, str],
    deadline: float,
    limit: int,
    cancel_check: Callable[[], None],
) -> tuple[Ingested, list[tuple[str, str]]]:
    input_dir = work / "input"
    output_dir = work / "output"
    profile = work / "profile"
    for directory in (input_dir, output_dir, profile):
        directory.mkdir(mode=0o700)
    staged_source = input_dir / f"source{source.suffix.lower()}"
    converted = output_dir / "source.docx"
    _copy_source(source, staged_source, limit, cancel_check)
    result = _run_bounded(
        _argv(route, staged_source, converted, profile),
        cwd=work,
        env=env,
        timeout=_remaining(deadline, cancel_check),
        cancel_check=cancel_check,
        watched_output=converted,
        watched_output_limit=limit,
        watched_root=work,
        temporary_byte_limit=limit * 2 + TEMPORARY_OVERHEAD_BYTES,
        temporary_entry_limit=settings.limits.max_archive_entries * 2 + 128,
    )
    if result.returncode != 0:
        detail = _scrub_output(result.output, work) or "no diagnostic output"
        raise ExternalConverterError(
            "converter_failed",
            f"{route.name} could not convert {source.suffix.lower()} to DOCX: {detail}",
            converter=route.name,
            input_format=source.suffix.lower().removeprefix("."),
            returncode=result.returncode,
        )
    _validate_output(output_dir, converted, limit)
    cancel_check()
    try:
        ingested = ingest_docx(str(converted), settings=settings, cancel_check=cancel_check)
    except (ResourceLimitError, CancelledError):
        raise
    except Exception as exc:
        raise ExternalConverterError(
            "converter_output_invalid",
            f"{route.name} produced a DOCX that Plate|Fold could not validate",
            converter=route.name,
        ) from exc
    cancel_check()
    return ingested, _converter_messages(result.output, work)


def ingest(
    path: str,
    settings: Settings,
    cancel_check: Callable[[], None] | None = None,
) -> Ingested:
    """Convert DOC/ODT/RTF to DOCX and reuse the hardened DOCX ingester."""
    if not settings.external_converters:
        raise ExternalConverterError(
            "converter_disabled", "External document conversion is disabled here"
        )
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix not in _LIBREOFFICE_FILTERS:
        raise ExternalConverterError(
            "converter_format_unsupported", f"External conversion does not support {suffix}"
        )
    cancel_check = cancel_check or (lambda: None)
    deadline = time.monotonic() + settings.limits.max_export_seconds
    failures: list[str] = []
    route_errors: list[ExternalConverterError] = []
    with tempfile.TemporaryDirectory(prefix="platefold-converter-") as temporary:
        work = Path(temporary)
        work.chmod(0o700)
        env = _private_environment(work)
        routes, probe_failures = _detect_routes(
            suffix,
            work=work,
            env=env,
            deadline=deadline,
            cancel_check=cancel_check,
        )
        if not routes:
            detail = f" Probe details: {'; '.join(probe_failures)}." if probe_failures else ""
            raise ExternalConverterError(
                "converter_unavailable",
                f"{source.name}: Plate|Fold cannot read {suffix} on this machine. "
                "Install LibreOffice, or re-save the file as DOCX. Pandoc is eligible for "
                "ODT/RTF only when its sandboxed DOCX capability self-test passes."
                f"{detail}",
                input_format=suffix.removeprefix("."),
                converter_options="LibreOffice; sandbox-capable Pandoc (ODT/RTF only)",
            )

        for route in routes:
            route_work = work / route.name
            route_work.mkdir(mode=0o700)
            try:
                ingested, converter_messages = _convert_with_route(
                    route,
                    source,
                    settings=settings,
                    work=route_work,
                    env=_private_environment(route_work),
                    deadline=deadline,
                    limit=settings.limits.max_archive_expanded_bytes,
                    cancel_check=cancel_check,
                )
            except ExternalConverterError as exc:
                failures.append(f"{route.name}: {exc}")
                route_errors.append(exc)
                continue
            warning = (
                "EXTERNAL_CONVERSION",
                f"Converted {source.name} from {suffix} to DOCX with {route.version}. "
                "Styles, layout, active content and source-theme details may not survive "
                "conversion; Plate|Fold then laid out the DOCX again.",
            )
            fallback = (
                [("EXTERNAL_CONVERTER_FALLBACK", failure) for failure in failures]
                if failures
                else []
            )
            conversion = {
                "name": route.name,
                "version": route.version,
                "input_format": suffix.removeprefix("."),
                "output_format": "docx",
                "sandbox": "safe-mode/private-profile"
                if route.name == "libreoffice"
                else "pandoc --sandbox",
            }
            return replace(
                ingested,
                warnings=[*ingested.warnings, warning, *converter_messages, *fallback],
                conversion=conversion,
            )
    detail = "; ".join([*failures, *probe_failures]) or "no route succeeded"
    terminal = route_errors[-1] if route_errors else None
    raise ExternalConverterError(
        terminal.code if terminal else "converter_failed",
        f"No installed converter could turn {source.name} into a valid DOCX. {detail}",
        **(terminal.fields if terminal else {"input_format": suffix.removeprefix(".")}),
    )
