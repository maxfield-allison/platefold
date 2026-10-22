"""Real DOM readiness regression, separate from container sandbox acceptance."""

import runpy
from pathlib import Path

import pytest

from platefold.browser import launch
from platefold.cdp import CDPError


@pytest.mark.integration
@pytest.mark.parametrize("seccomp", ["Yes", "No"])
def test_sandbox_probe_waits_for_body_and_still_requires_protection(seccomp):
    probe = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "packaging/container/sandbox_check.py")
    )["sandbox_status"]
    browser, _ = launch("bundled")
    try:
        page = browser.new_page()
        page.set_content(
            "<pre>Layer 1 Sandbox\tNamespace\nPID namespaces\tYes\n"
            f"Network namespaces\tYes\nSeccomp-BPF sandbox\t{seccomp}</pre>"
        )
        page.evaluate("window.savedBody = document.body; document.body.remove()")
        # Reproduce the failed CI expression before allowing the body to appear.
        with pytest.raises(CDPError, match="innerText"):
            page.evaluate("document.body.innerText")
        page.evaluate("setTimeout(() => document.documentElement.append(window.savedBody), 150)")
        if seccomp == "Yes":
            assert "Seccomp-BPF sandbox\tYes" in probe(page)
        else:
            with pytest.raises(AssertionError, match="Seccomp-BPF sandbox"):
                probe(page)
    finally:
        browser.close()
