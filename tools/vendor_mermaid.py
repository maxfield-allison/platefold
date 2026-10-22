"""Refresh the renderer and license inventory from a lockfile npm installation.

Run npm ci --ignore-scripts first. No npm code runs during export or wheel builds.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    vendor = ROOT / "platefold/vendor"
    vendor.mkdir(exist_ok=True)
    mermaid = ROOT / "node_modules/mermaid"
    blob = (mermaid / "dist/mermaid.min.js").read_bytes()
    (vendor / "mermaid.min.js").write_bytes(blob)
    (vendor / "mermaid.LICENSE").write_bytes((mermaid / "LICENSE").read_bytes())
    notices = []
    packages = []
    for relative, info in sorted(lock["packages"].items()):
        if not relative or info.get("dev"):
            continue
        folder = ROOT / relative
        metadata = json.loads((folder / "package.json").read_text(encoding="utf-8"))
        packages.append(
            {
                "name": metadata["name"],
                "version": metadata["version"],
                "license": metadata.get("license"),
                "integrity": info.get("integrity"),
            }
        )
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.name.lower().startswith(
                ("license", "licence", "copying", "notice")
            ):
                notices.append(
                    f"{metadata['name']} {metadata['version']} / {path.name}\n\n{path.read_text(encoding='utf-8', errors='replace')}"
                )
    (vendor / "THIRD_PARTY_LICENSES.txt").write_text("\n\n".join(notices), encoding="utf-8")
    manifest = {
        "mermaid_version": lock["packages"]["node_modules/mermaid"]["version"],
        "sha256": hashlib.sha256(blob).hexdigest(),
        "npm_dependency_inventory": packages,
    }
    (vendor / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"Vendored Mermaid {manifest['mermaid_version']}; license inventory covers {len(packages)} npm packages."
    )


if __name__ == "__main__":
    main()
