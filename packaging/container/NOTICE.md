# Container inputs

The seccomp profile is derived from Microsoft Playwright v1.62.0, `utils/docker/seccomp_profile.json` (Apache-2.0). Source: <https://github.com/microsoft/playwright/blob/v1.62.0/utils/docker/seccomp_profile.json>. SHA-256: `cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`. The source hash above identifies the upstream file. PlateFold adds an unconditional `chroot` syscall allowance: Chromium needs it inside its new user namespace when the container drops ALL capabilities. Linux capability checks still apply; no host capability is granted. Without it Chromium exits during zygote startup.

The profile retains a default-deny syscall policy and allows Chromium to create its user namespaces. It is not Docker's unconfined profile. Review upstream changes before replacing it. The adjacent Apache-2.0 license applies to that profile.

Playwright downloads a different Chromium build per architecture: Chrome for Testing on amd64 and Playwright's own Chromium build on arm64. Both are open-source Chromium 151 with the same pinned Playwright release, and each payload is checked against its own manifest (`browser-amd64.sha256`, `browser-arm64.sha256`). Neither archive ships Chromium's license inventory as a file; the browser carries it at `chrome://credits`. The image build renders that page from each architecture's installed browser into `/usr/share/doc/platefold/chromium-credits.html.gz`, covering Chromium, PDFium and their bundled components.

The image retains Debian package copyright files under `/usr/share/doc`, Python wheel license metadata under `/opt/venv`, the Chromium credits above, the browser archives' own files (such as FFmpeg's LGPL text) under `/opt/browsers`, and PlateFold/Mermaid/font notices in the installed package. The frontend's `THIRD_PARTY.md` is also under `/usr/share/doc/platefold`. Generate an image SBOM from the actual built digest; the source dependency inventory is not an image SBOM.

The Debian `fonts-noto-core` and `fonts-noto-cjk` packages supply system fallback fonts, including Chinese, Japanese and Korean coverage. Their copyright and license notices remain in `/usr/share/doc/fonts-noto-core` and `/usr/share/doc/fonts-noto-cjk`. These are container fonts; the browser edition cannot use fonts installed only on the user's workstation.

DOCX support uses Mammoth 1.12.1 and Cobble (BSD) and defusedxml 0.7.1 (Python Software Foundation License). Cobble 0.1.4's wheel omits its declared BSD-2-Clause text, so `cobble-BSD-2-Clause.txt` supplies the retained upstream notice. Other installed wheel license metadata remains under `/opt/venv`; PlateFold does not bundle, download or redistribute source-document fonts.

XLSX support uses openpyxl 3.1.5 and et-xmlfile 2.0.0 (MIT). Their installed wheel license metadata is retained under `/opt/venv`; PlateFold does not evaluate formulas, fetch linked workbooks or execute embedded objects.

`seccomp-kubernetes.json` (Linux amd64) and `seccomp-kubernetes-arm64.json` (Linux arm64) specialize that policy for one architecture each with no container capabilities. `tools/container_seccomp.py --check` verifies both. It removes Docker-specific conditional fields and privileged-only rules before using the OCI schema expected by a Kubernetes Localhost profile.
