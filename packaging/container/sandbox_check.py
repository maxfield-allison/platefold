"""Fail unless the bundled renderer reports namespace and seccomp sandboxes."""

import json
import os
import time
from pathlib import Path

from platefold.browser import launch


def sandbox_status(page):
    # Page.navigate acknowledges navigation before the new DOM is available.
    # An absent body is a loading state, not evidence about sandbox support.
    text = ""
    for _ in range(50):
        text = page.evaluate("document.body?.innerText || ''")
        if "Seccomp-BPF sandbox" in text:
            break
        time.sleep(0.1)
    assert "Layer 1 Sandbox\tNamespace" in text, text
    for label in ("PID namespaces", "Network namespaces", "Seccomp-BPF sandbox"):
        assert label + "\tYes" in text, text
    return text


def main():
    assert os.geteuid() != 0, "Sandbox qualification must run as a non-root user"
    status = Path("/proc/self/status").read_text()
    assert "NoNewPrivs:\t1" in status
    assert "CapEff:\t0000000000000000" in status
    browser, engine = launch("bundled")
    try:
        page = browser.new_page()
        page._send("Page.navigate", {"url": "chrome://sandbox"})
        text = sandbox_status(page)
        print(json.dumps({"engine": engine, "uid": os.geteuid(), "sandbox": text}, indent=2))
    finally:
        browser.close()


if __name__ == "__main__":
    main()
