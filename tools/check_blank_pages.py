#!/usr/bin/env python3
"""Regression check: flag pages that contain nothing but the running header.

Blank pages are the classic failure mode when tweaking the plate CSS - a
figure that overflows its page pushes an empty one ahead of it. Run this after
any change to style.py or layout.py.

    python tools/check_blank_pages.py "C:\\Docs\\PDF Export"

The detection itself lives in `platefold.blankcheck` so the test suite can use it;
this is just the command-line front end.
"""

import sys

from platefold.blankcheck import blank_pages, scan

__all__ = ["blank_pages", "scan"]


def main(root: str) -> int:
    found = scan(root)
    for rel, empties in found.items():
        print("  BLANK %s  %s" % (empties, rel))
    print("documents with blank pages: %d" % len(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "PDF Export"))
