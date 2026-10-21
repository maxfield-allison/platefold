"""Serve only the built, self-hosted application assets; never job-store files."""

from pathlib import Path

from starlette.responses import FileResponse

UI_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def web_assets(directory: Path | None = None) -> dict[str, Path]:
    if directory is None:
        package = Path(__file__).resolve().parent
        directory = package / "web"
        if not (directory / "index.html").is_file():
            directory = package.parent / "desktop" / "dist"
    if not (directory / "index.html").is_file():
        return {}
    root = directory.resolve()
    result = {}
    candidates = [directory / "index.html", *(directory / "assets").glob("*")]
    for path in candidates:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("Web assets must be regular files within the built site")
        name = path.relative_to(directory).as_posix()
        if name != "index.html" and path.suffix not in {
            ".js",
            ".css",
            ".woff",
            ".woff2",
            ".svg",
            ".png",
        }:
            raise ValueError("Unexpected web asset type")
        result["/" if name == "index.html" else "/" + name] = path
    return result


async def asset(request):
    from platefold.server import no_query
    from platefold.server_models import APIError

    no_query(request)
    path = request.app.state.web_assets.get(request.url.path)
    if path is None:
        raise APIError(404, "asset_not_found", "Asset is unavailable")
    return FileResponse(path)
