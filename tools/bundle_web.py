#!/usr/bin/env python3
"""Stage the Vite build in the Python package before building a server wheel."""

import shutil
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "desktop" / "dist"
    target = root / "platefold" / "web"
    if not (source / "index.html").is_file():
        raise SystemExit("Build desktop/dist with npm ci and npm run build first")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    print("Staged shared browser interface in platefold/web")


if __name__ == "__main__":
    main()
