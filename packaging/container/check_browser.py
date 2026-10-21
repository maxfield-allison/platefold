"""Verify all browser payload files against the reviewed acquisition manifest."""

import hashlib
from pathlib import Path

root = Path("/opt/browsers")
expected = {}
for line in Path("/build/browser.sha256").read_text().splitlines():
    digest, name = line.split("  ", 1)
    expected[name] = digest
actual = {}
for path in sorted(root.rglob("*")):
    if ".links" in path.parts:
        continue
    if path.is_symlink():
        raise SystemExit("Unexpected browser payload symlink")
    if path.is_file():
        with path.open("rb") as stream:
            actual[str(path.relative_to(root))] = hashlib.file_digest(stream, "sha256").hexdigest()
if actual != expected:
    # Name what differs so a changed upstream archive can be reviewed; bounded
    # so an entirely different payload does not flood the build log.
    for label, names in (
        ("missing", sorted(expected.keys() - actual.keys())),
        ("unexpected", sorted(actual.keys() - expected.keys())),
        ("changed", sorted(n for n in expected.keys() & actual.keys() if expected[n] != actual[n])),
    ):
        for name in names[:20]:
            print(f"{label}: {name}")
        if len(names) > 20:
            print(f"{label}: ... {len(names) - 20} more")
    raise SystemExit("Browser payload differs from the reviewed acquisition manifest")
print(f"Verified {len(actual)} browser payload files")
