"""Optional authenticated HTTP adapter for a single Linux job store."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import stat
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from platefold.browser import find_bundled, find_system
from platefold.contrast import RULES, contrast_failures
from platefold.paths import resource_root
from platefold.server_jobs import JobStore
from platefold.server_models import TERMINAL, APIError, JobLimits, JobRequest, Mount
from platefold.server_sources import InvalidSource, parts_for
from platefold.server_web import UI_CSP, asset, web_assets
from platefold.theme import COLOR_TOKENS, available_font_sets, discover_themes

JSON_BYTES = 16 * 1024


class ConfigurationError(ValueError):
    """An operator configuration problem whose fixed message is safe to print.

    Messages never include credential values, file contents or paths.
    """


def error_response(status: int, code: str, message: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    if status == 429:
        headers["Retry-After"] = "60"
    return JSONResponse(
        {"schema_version": 1, "error": {"code": code, "message": message}},
        status_code=status,
        headers=headers,
    )


# Token strength floor. A credential is only as strong as its generator, and no
# check on one string can prove randomness; these reject the mistakes that do
# show up in configuration: placeholders, repeated words, keyboard or alphabet
# runs and small alphabets. Thresholds were calibrated so that tokens from
# common generators (hex, base64/url-safe, UUID4, random printable) pass.
TOKEN_MIN_ESTIMATED_BITS = 76.0
TOKEN_MIN_DISTINCT_TRIGRAMS = 0.75
TOKEN_MAX_SEQUENTIAL_STEPS = 0.6


def weak_token(token: str) -> bool:
    """True when a token is too short, repetitive, sequential or small-alphabet."""
    length = len(token)
    if length < 32:
        return True
    # Zero-order estimate: sum of -log2(frequency) over the characters.
    bits = -sum(count * math.log2(count / length) for count in Counter(token).values())
    trigrams = [token[i : i + 3] for i in range(length - 2)]
    distinct = len(set(trigrams)) / len(trigrams)
    steps = sum(abs(ord(a) - ord(b)) == 1 for a, b in pairwise(token)) / (length - 1)
    return (
        bits < TOKEN_MIN_ESTIMATED_BITS
        or distinct < TOKEN_MIN_DISTINCT_TRIGRAMS
        or steps > TOKEN_MAX_SEQUENTIAL_STEPS
    )


def token_hashes(tokens: dict[str, str]) -> dict[bytes, str]:
    if not isinstance(tokens, dict) or not 1 <= len(tokens) <= 64:
        raise ConfigurationError("Configure between one and 64 API principals")
    result = {}
    for owner, token in tokens.items():
        if (
            not isinstance(owner, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", owner)
            or not isinstance(token, str)
            or not re.fullmatch(r"[\x21-\x7e]{32,512}", token)
            or weak_token(token)
        ):
            raise ConfigurationError(
                "API principals need lowercase names and random tokens of 32-512 printable "
                "characters; a token is too short, repetitive, sequential or small-alphabet"
            )
        digest = hashlib.sha256(token.encode()).digest()
        if digest in result:
            raise ConfigurationError("Each principal must use a distinct API credential")
        result[digest] = owner
    return result


def loopback_host(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def canonical_origins(origins: tuple[str, ...], *, allow_http: bool = False) -> tuple[str, ...]:
    """Validate exact browser origins and return them as browsers serialize them.

    Browsers send lowercase hosts and omit a scheme's default port in both
    Origin and Host, so a configured ``https://PF.example:443`` is compared as
    ``https://pf.example``. A plain-http origin is accepted only for a loopback
    host unless ``allow_http`` is set: anywhere else it sends bearer
    credentials and documents across the network unencrypted.
    """
    if not origins or len(origins) > 16:
        raise ConfigurationError("Configure one to 16 exact browser origins")
    result = []
    for origin in origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "*" in origin
            or origin.lower() != f"{parsed.scheme}://{parsed.netloc}".lower()
        ):
            raise ConfigurationError(
                "Origins must be exact http(s) origins without paths or wildcards"
            )
        host = parsed.hostname
        if parsed.scheme == "http" and not allow_http and not loopback_host(host):
            raise ConfigurationError(
                "Plain-http origins are accepted only on loopback addresses; "
                "use HTTPS or --allow-http-origin"
            )
        port = parsed.port  # Raises ValueError for an out-of-range port.
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
            authority += f":{port}"
        result.append(f"{parsed.scheme}://{authority}")
    return tuple(dict.fromkeys(result))


def origin_hosts(origins: tuple[str, ...]) -> set[str]:
    """Host header values for canonical origins."""
    return {origin.split("://", 1)[1] for origin in origins}


class SecurityBoundary:
    """Check Host, Origin and bearer ownership before the router reads any body."""

    def __init__(
        self, app, tokens: dict[bytes, str], origins: tuple[str, ...], public_assets: frozenset[str]
    ):
        self.app, self.tokens, self.origins = app, tokens, origins
        self.hosts = origin_hosts(origins)
        self.tokens_list = list(tokens.items())
        self.public_assets = public_assets

    def check(self, request: Request) -> Response | None:
        headers = request.headers
        if len(headers.getlist("host")) != 1 or headers["host"].lower() not in self.hosts:
            return error_response(400, "invalid_host", "Host is not configured")
        origins = headers.getlist("origin")
        if len(origins) > 1 or (origins and origins[0] not in self.origins):
            return error_response(403, "origin_denied", "Origin is not configured")
        if "cookie" in headers:
            return error_response(400, "cookies_unsupported", "Use bearer authentication")
        if request.method == "OPTIONS":
            method = headers.get("access-control-request-method")
            requested = {
                v.strip().lower()
                for v in headers.get("access-control-request-headers", "").split(",")
                if v.strip()
            }
            if (
                not origins
                or method not in {"GET", "POST", "PUT"}
                or not requested <= {"authorization", "content-type"}
            ):
                return error_response(403, "preflight_denied", "Preflight is not permitted")
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Methods": "GET, POST, PUT",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type",
                    "Access-Control-Max-Age": "600",
                },
            )
        if request.url.path in {"/health/live", "/health/ready"} and request.method == "GET":
            return None
        if request.method in {"GET", "HEAD"} and request.url.path in self.public_assets:
            return None
        authorization = headers.getlist("authorization")
        value = authorization[0] if len(authorization) == 1 else ""
        if not value.startswith("Bearer ") or len(value) > 519:
            return error_response(401, "unauthorized", "A bearer credential is required")
        owner = self.owner_for(value[7:])
        if owner is None:
            return error_response(401, "unauthorized", "A bearer credential is required")
        request.scope["owner"] = owner
        return None

    def owner_for(self, credential: str) -> str | None:
        """Compare against every configured digest, without stopping at a match."""
        digest = hashlib.sha256(credential.encode()).digest()
        owner = None
        for key, candidate in self.tokens_list:
            if hmac.compare_digest(key, digest):
                owner = candidate
        return owner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            # No WebSocket route exists; refuse other scopes before routing.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return None
        request = Request(scope, receive)
        started = False

        async def secure_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"content-security-policy",
                            (
                                UI_CSP
                                if request.url.path in self.public_assets
                                else "default-src 'none'; sandbox"
                            ).encode(),
                        ),
                    ]
                )
                origin = request.headers.get("origin")
                if origin in self.origins:
                    headers.extend(
                        [(b"access-control-allow-origin", origin.encode()), (b"vary", b"Origin")]
                    )
                message["headers"] = headers
            await send(message)

        denied = self.check(request)
        if denied is not None:
            return await denied(scope, receive, secure_send)
        try:
            return await self.app(scope, receive, secure_send)
        except Exception:
            if started:
                raise
            response = error_response(500, "internal_error", "Request could not be completed")
            return await response(scope, receive, secure_send)


def no_query(request: Request, allowed: set[str] | None = None) -> None:
    keys = list(request.query_params.keys())
    if set(keys) - (allowed or set()) or any(
        len(request.query_params.getlist(k)) != 1 for k in keys
    ):
        raise APIError(400, "invalid_query", "Unexpected or repeated query parameter")


async def read_json(request: Request) -> dict:
    if request.headers.get("content-type") != "application/json":
        raise APIError(415, "unsupported_media_type", "Expected application/json")
    raw = bytearray()
    async with asyncio.timeout(10):
        async for chunk in request.stream():
            if len(raw) + len(chunk) > JSON_BYTES:
                raise APIError(413, "body_too_large", "JSON body exceeds 16 KiB")
            raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise APIError(422, "invalid_request", "Expected a valid JSON job request") from None
    if not isinstance(value, dict):
        raise APIError(422, "invalid_request", "Expected a JSON object")
    return value


def current(request: Request):
    no_query(request)
    store = request.app.state.jobs
    return store, store.get(request.scope["owner"], request.path_params["job_id"])


LEGACY_FORMATS = frozenset({"doc", "odt", "rtf"})


async def create(request: Request) -> Response:
    no_query(request)
    try:
        data = JobRequest.model_validate(await read_json(request))
    except ValidationError:
        # Pydantic errors include submitted values; never reflect credentials or content.
        raise APIError(422, "invalid_request", "Job request does not match the v1 schema") from None
    store = request.app.state.jobs
    if (
        data.source.kind == "upload"
        and data.source.format in LEGACY_FORMATS
        and not store.limits.legacy_conversion
    ):
        raise APIError(
            422, "legacy_conversion_disabled", "This server does not convert DOC, ODT or RTF"
        )
    if not store.browser:
        raise APIError(503, "renderer_unavailable", "PDF renderer is unavailable")
    job = store.admit(request.scope["owner"], data)
    return JSONResponse(
        job.public(store.limits), status_code=201, headers={"Location": f"/api/v1/jobs/{job.id}"}
    )


async def upload(request: Request) -> Response:
    store, job = current(request)
    if job.state != "awaiting_upload" or job.id in store.uploads:
        raise APIError(409, "invalid_state", "This job is not awaiting an upload")
    expected = {
        "markdown": "text/markdown",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "doc": "application/msword",
        "odt": "application/vnd.oasis.opendocument.text",
        "rtf": "application/rtf",
        "csv": "text/csv",
        "tsv": "text/tab-separated-values",
        "zip": "application/zip",
    }[job.request["source"]["format"]]
    if request.headers.get("content-type", "").split(";")[0] != expected:
        raise APIError(415, "unsupported_media_type", f"Expected {expected}")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise APIError(415, "unsupported_encoding", "Compressed HTTP bodies are not accepted")
    store.uploads.add(job.id)
    try:
        size = await stream_upload(request, store, job)
        if not size:
            raise APIError(422, "empty_upload", "Upload is empty")
        if job.state in TERMINAL:
            raise APIError(409, "invalid_state", "Job was cancelled")
        store.enqueue(job)
    except BaseException:
        if job.state not in TERMINAL:
            store.transition(job, "failed", "upload_incomplete")
        store.clean_work(job)
        raise
    finally:
        store.uploads.discard(job.id)
    return JSONResponse(job.public(store.limits), status_code=202)


async def stream_upload(request: Request, store, job) -> int:
    size = 0
    async with asyncio.timeout(store.limits.upload_seconds):
        with (store.path(job) / "work" / "upload").open("xb") as stream:
            async for chunk in request.stream():
                if job.state in TERMINAL:
                    raise APIError(409, "invalid_state", "Job was cancelled")
                size += len(chunk)
                if size > store.limits.upload_bytes:
                    raise APIError(413, "upload_too_large", "Upload exceeds its byte limit")
                stream.write(chunk)
    return size


async def status(request: Request) -> Response:
    store, job = current(request)
    return JSONResponse(job.public(store.limits))


async def cancel(request: Request) -> Response:
    store, job = current(request)
    store.cancel(job)
    return JSONResponse(
        job.public(store.limits), status_code=202 if job.state == "cancelling" else 200
    )


async def events(request: Request) -> Response:
    no_query(request, {"after"})
    store = request.app.state.jobs
    job = store.get(request.scope["owner"], request.path_params["job_id"])
    raw = request.query_params.get("after", "0")
    if not re.fullmatch(r"[0-9]{1,10}", raw) or int(raw) > job.sequence:
        raise APIError(422, "invalid_cursor", "Expected an existing nonnegative event cursor")
    after = int(raw)
    return JSONResponse(
        {
            "schema_version": 1,
            "id": job.id,
            "state": job.state,
            "events": [e for e in job.events if e["sequence"] > after],
            "next_after": job.sequence,
            "reset": bool(job.events and after < job.events[0]["sequence"] - 1),
        }
    )


async def results(request: Request) -> Response:
    _store, job = current(request)
    if job.state not in {"completed", "completed_with_warnings"}:
        raise APIError(409, "results_unavailable", "Job has no completed results")
    return JSONResponse(
        {
            "schema_version": 1,
            "id": job.id,
            "results": [
                r | {"url": f"/api/v1/jobs/{job.id}/results/{r['id']}"} for r in job.results
            ],
        }
    )


async def download(request: Request) -> Response:
    store, job = current(request)
    record = next((r for r in job.results if r["id"] == request.path_params["result_id"]), None)
    if job.state not in {"completed", "completed_with_warnings"} or not record:
        raise APIError(404, "result_not_found", "Result not found")
    if store.downloads.get(job.id, 0) >= 4:
        raise APIError(429, "download_capacity", "This job already has four active downloads")
    path = (store.path(job) / "results").joinpath(*parts_for(record["name"]))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != record["bytes"]:
            os.close(fd)
            raise OSError("Invalid result")
    except OSError:
        raise APIError(404, "result_not_found", "Result not found") from None

    # Download IDs never become path components. Use a fixed header filename;
    # the original relative label is separately available in the JSON manifest.
    extension = "pdf" if record["media_type"] == "application/pdf" else "json"
    return FileDownload(
        fd,
        store,
        job.id,
        media_type=record["media_type"],
        headers={
            "Content-Length": str(info.st_size),
            "Content-Disposition": f'attachment; filename="platefold-{record["id"]}.{extension}"',
        },
    )


class FileDownload(StreamingResponse):
    """Own the file even if a consumer disconnects before reading its first byte."""

    def __init__(self, fd, store, job_id, **kwargs):
        self.fd, self.store, self.job_id = fd, store, job_id
        store.downloads[job_id] = store.downloads.get(job_id, 0) + 1
        super().__init__(self.chunks(), **kwargs)

    async def chunks(self):
        while chunk := os.read(self.fd, 64 * 1024):
            yield chunk
            await asyncio.sleep(0)

    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(self.store.limits.download_seconds):
                await super().__call__(scope, receive, send)
        finally:
            os.close(self.fd)
            self.store.downloads[self.job_id] -= 1
            if not self.store.downloads[self.job_id]:
                del self.store.downloads[self.job_id]


async def health(request: Request) -> Response:
    store = request.app.state.jobs
    ready = bool(store.browser and store.storage_healthy and not store.closing)
    live = request.url.path.endswith("/live")
    return JSONResponse(
        {"status": "ok" if live or ready else "not_ready"},
        status_code=200 if live or ready else 503,
    )


def static_capabilities() -> dict:
    """The parts of /capabilities that only change when the server restarts.

    Theme discovery, contrast checks, the JSON schema and installed-font
    discovery (an fc-list subprocess) take about a second together, so they run
    once in a worker thread instead of on the event loop for every request.
    """
    fonts = Path(resource_root()) / "fonts"
    bristol = discover_themes(user=False)["bristol"]
    return {
        "schema_version": 1,
        "replicas": 1,
        "platform": "linux",
        "contrast_rules": [asdict(rule) for rule in RULES],
        "gui_engine": "client browser; separate from PDF Chromium",
        "fonts": {
            "bundled_latin_available": any(fonts.glob("*.woff2")),
            "system_fallback": True,
        },
        "job_schema": JobRequest.model_json_schema(),
        "themes": [
            {
                "name": "source",
                "label": "Source document theme",
                "mode": "light",
                "description": (
                    "Use each available DOCX or XLSX palette and requested typefaces for "
                    "semantic re-layout, including after legacy conversion; other inputs "
                    "use the Bristol fallback."
                ),
                "builtin": False,
                "kind": "source",
                "contrast_failures": [],
                "colors": {k: getattr(bristol, k) for k in sorted(COLOR_TOKENS)},
            },
            *[
                {
                    "name": t.name,
                    "label": t.label,
                    "mode": t.mode,
                    "description": t.description,
                    "builtin": True,
                    "kind": "builtin",
                    "contrast_failures": contrast_failures(
                        {k: getattr(t, k) for k in COLOR_TOKENS}
                    ),
                    "colors": {k: getattr(t, k) for k in sorted(COLOR_TOKENS)},
                }
                for t in discover_themes(user=False).values()
            ],
        ],
        "font_sets": [asdict(f) for f in available_font_sets()],
    }


async def capabilities(request: Request) -> Response:
    no_query(request)
    state = request.app.state
    store = state.jobs
    catalog = getattr(state, "capability_catalog", None)
    if catalog is None:
        # Two first requests may both build it; the result is identical.
        catalog = await run_in_threadpool(static_capabilities)
        state.capability_catalog = catalog
    verified = any(j.state in {"completed", "completed_with_warnings"} for j in store.jobs.values())
    return JSONResponse(
        {
            **catalog,
            "storage_ready": store.storage_healthy,
            "browser_adapter": bool(state.web_assets),
            "pdf_engine": {
                "family": "Chromium",
                "discovered": bool(store.browser),
                "last_export_verified": verified,
                # Observed from the most recent export's renderer processes,
                # not assumed from launch flags. Null until an export shows it.
                "sandbox": store.sandbox or "unverified",
                "sandbox_disabled": None if store.sandbox is None else store.sandbox == "disabled",
            },
            "limits": asdict(store.limits),
            "mounts": [
                name
                for name, mount in store.mounts.items()
                if request.scope["owner"] in mount.owners
            ],
        }
    )


async def handle_error(request: Request, exc: Exception) -> Response:
    if isinstance(exc, APIError):
        return error_response(exc.status, exc.code, exc.message)
    if isinstance(exc, InvalidSource):
        return error_response(422, "invalid_source", "Source path is invalid")
    if isinstance(exc, HTTPException):
        return error_response(exc.status_code, "http_error", "Request is not supported")
    if isinstance(exc, TimeoutError):
        return error_response(408, "request_timeout", "Request exceeded its deadline")
    if isinstance(exc, ClientDisconnect):
        return error_response(400, "client_disconnected", "Request was interrupted")
    return error_response(500, "internal_error", "Request could not be completed")


def discover_browser() -> str:
    return find_bundled() or next(
        (p for kind in ("chromium", "chrome", "edge", "brave") if (p := find_system(kind))), ""
    )


def create_app(
    root: Path,
    tokens: dict[str, str],
    origins: tuple[str, ...],
    *,
    limits: JobLimits | None = None,
    mounts: dict[str, Mount] | None = None,
    browser: str | None = None,
    web_root: Path | None = None,
    allow_http_origins: bool = False,
):
    hashes = token_hashes(tokens)
    origins = canonical_origins(origins, allow_http=allow_http_origins)
    store = JobStore(
        root,
        limits or JobLimits(),
        discover_browser() if browser is None else browser,
        mounts or {},
    )

    @asynccontextmanager
    async def lifespan(app):
        await store.start()
        try:
            yield
        finally:
            await store.close()

    assets = web_assets(web_root)
    prefix = "/api/v1/jobs/{job_id}"
    app = Starlette(
        routes=[
            Route("/", asset),
            Route("/assets/{path:path}", asset),
            Route("/health/live", health),
            Route("/health/ready", health),
            Route("/api/v1/capabilities", capabilities),
            Route("/api/v1/jobs", create, methods=["POST"]),
            Route(prefix, status),
            Route(prefix + "/source", upload, methods=["PUT"]),
            Route(prefix + "/cancel", cancel, methods=["POST"]),
            Route(prefix + "/events", events),
            Route(prefix + "/results", results),
            Route(prefix + "/results/{result_id}", download),
        ],
        lifespan=lifespan,
        exception_handlers={
            APIError: handle_error,
            InvalidSource: handle_error,
            HTTPException: handle_error,
            TimeoutError: handle_error,
            ClientDisconnect: handle_error,
            Exception: handle_error,
        },
    )
    app.state.jobs = store
    app.state.web_assets = assets
    app.add_middleware(
        SecurityBoundary, tokens=hashes, origins=origins, public_assets=frozenset(assets)
    )
    return app


def read_operator_file(path: Path, *, secret: bool = False) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise ConfigurationError("Configuration must be a regular file no larger than 64 KiB")
        if secret and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ConfigurationError(
                "Credential file must be owned by this user and private (0600)"
            )
        value = json.loads(stream.read(64 * 1024 + 1))
        if not isinstance(value, dict):
            raise ConfigurationError("Configuration must be a JSON object")
        return value


def main(argv=None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Plate|Fold HTTP job API (Linux)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument(
        "--tokens-file",
        type=Path,
        help="0600 JSON owner-to-token map; otherwise PLATEFOLD_API_TOKENS",
    )
    parser.add_argument("--origin", action="append", default=[])
    parser.add_argument(
        "--allow-http-origin",
        action="store_true",
        help="Accept plain-http origins off loopback; credentials then cross the network unencrypted",
    )
    parser.add_argument("--mounts-file", type=Path, help="JSON mount name to path/owners object")
    parser.add_argument("--limits-file", type=Path, help="Operator-owned JSON limits overrides")
    args = parser.parse_args(argv)
    try:
        address = ipaddress.ip_address(args.host)
        if not 1 <= args.port <= 65535:
            raise ConfigurationError("Port must be between 1 and 65535")
        if not address.is_loopback and not args.origin:
            raise ConfigurationError("Non-loopback binding requires explicit --origin")
        authority = f"[{args.host}]" if address.version == 6 else args.host
        origins = tuple(args.origin or [f"http://{authority}:{args.port}"])
        tokens = (
            read_operator_file(args.tokens_file, secret=True)
            if args.tokens_file
            else json.loads(os.environ.get("PLATEFOLD_API_TOKENS", "{}"))
        )
        mounts = load_mounts(args.mounts_file, tokens)
        limits = (
            JobLimits(**read_operator_file(args.limits_file)) if args.limits_file else JobLimits()
        )
        app = create_app(
            args.data_dir.absolute(),
            tokens,
            origins,
            limits=limits,
            mounts=mounts,
            allow_http_origins=args.allow_http_origin,
        )
    except ConfigurationError as exc:
        parser.exit(2, f"Invalid server configuration: {exc}\n")
    except (ValueError, TypeError, OSError):
        parser.exit(
            2, "Invalid server configuration; check credentials, origins, mounts and limits.\n"
        )
    from platefold.server_http import HEAD_BYTES, DeadlineH11Protocol

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        # Pin the parser: "auto" silently switches to httptools when it is
        # installed, which ignores the head size limit and the head deadline.
        http=DeadlineH11Protocol,
        ws="none",
        lifespan="on",
        access_log=False,
        proxy_headers=False,
        server_header=False,
        limit_concurrency=64,
        timeout_keep_alive=5,
        # DeadlineH11Protocol limits complete heads to HEAD_BYTES; this bounds
        # anything else h11 buffers while incomplete, such as a chunk-size line.
        h11_max_incomplete_event_size=HEAD_BYTES,
    )
    return 0


def load_mounts(path: Path | None, tokens: dict) -> dict[str, Mount]:
    if path is None:
        return {}
    mounts = {}
    for name, value in read_operator_file(path).items():
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", name)
            or not isinstance(value, dict)
            or set(value) != {"path", "owners"}
            or not isinstance(value["path"], str)
            or not Path(value["path"]).is_absolute()
            or not isinstance(value["owners"], list)
            or not value["owners"]
            or any(not isinstance(o, str) or o not in tokens for o in value["owners"])
        ):
            raise ConfigurationError("Invalid mount configuration")
        mounts[name] = Mount(Path(value["path"]), frozenset(value["owners"]))
    return mounts
