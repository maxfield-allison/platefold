"""Build the standalone distribution with the downloaded Chromium engine."""

import os
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "packaging/platefold.spec",
            "--noconfirm",
            "--distpath",
            "dist-bundled",
            "--workpath",
            "build-bundled",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PLATEFOLD_BUNDLE_BROWSER": "1"},
        check=True,
    )
