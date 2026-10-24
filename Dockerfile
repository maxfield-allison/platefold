# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
ARG PYTHON_IMAGE=python:3.13-slim@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f
ARG NODE_IMAGE=node:22.23.4-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d
FROM ${NODE_IMAGE} AS web
WORKDIR /build/desktop
COPY desktop/package.json desktop/package-lock.json ./
RUN npm ci
# Copy the web build inputs by name after npm ci, never the whole desktop/
# tree, so nothing from the build host lands on the installed packages.
COPY desktop/index.html desktop/tsconfig.json desktop/vite.config.ts ./
COPY desktop/src/ src/
# src/i18n.ts bundles the interface catalogs from the Python package. Without
# them the build still succeeds, and the page stops on load.
COPY platefold/locales/*.json /build/platefold/locales/
RUN npm run build

FROM ${PYTHON_IMAGE} AS python-build
WORKDIR /build
COPY packaging/container/requirements.txt /build/requirements.txt
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.txt
COPY packaging/container/build.txt /build/build.txt
RUN pip install --no-cache-dir --require-hashes -r /build/build.txt
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
# Playwright downloads a different Chromium build per architecture: Chrome for
# Testing on amd64, Playwright's own Chromium build on arm64. Each has its own
# reviewed payload manifest.
ARG TARGETARCH
COPY packaging/container/browser-${TARGETARCH}.sha256 /build/browser.sha256
COPY packaging/container/check_browser.py /build/
# Host validation would check this build stage, not the runtime image, and
# whether it writes its DEPENDENCIES_VALIDATED marker differs by architecture,
# so it is skipped to keep the payload manifests exact.
RUN PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 python -m playwright install --no-shell chromium && \
    python /build/check_browser.py && \
    chmod -R a+rX /opt/browsers
COPY pyproject.toml README.md LICENSE ./
COPY platefold/ platefold/
COPY --from=web /build/desktop/dist/ platefold/web/
RUN python -m build --wheel --no-isolation && /opt/venv/bin/pip install --no-cache-dir --no-deps dist/*.whl

FROM ${PYTHON_IMAGE} AS runtime
ARG TARGETARCH
ARG CACHE_BUST=local
# Frozen Debian repository metadata keeps OS inputs repeatable. Advance deliberately
# with a fresh image scan; base image digest and Python/npm locks move separately.
ARG DEBIAN_SNAPSHOT=20261024T120000Z
RUN case "$TARGETARCH" in amd64|arm64) ;; *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1;; esac && \
    rm /etc/apt/sources.list.d/debian.sources && \
    printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/%s/ trixie main\ndeb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/%s/ trixie-security main\n' "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list && \
    apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends tini ca-certificates fontconfig fonts-dejavu-core fonts-liberation fonts-noto-core fonts-noto-cjk libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 platefold && useradd --uid 10001 --gid 10001 --home-dir /tmp/home --no-create-home platefold && \
    install -d -o 10001 -g 10001 -m 0700 /data /output
COPY --from=python-build /opt/venv /opt/venv
COPY --from=python-build /opt/browsers /opt/browsers
COPY packaging/container/healthcheck.py packaging/container/sandbox_check.py packaging/container/limits.json /opt/platefold/
COPY LICENSE desktop/THIRD_PARTY.md packaging/container/NOTICE.md packaging/container/LICENSE.seccomp packaging/container/seccomp.json packaging/licenses/cobble-BSD-2-Clause.txt /usr/share/doc/platefold/
COPY packaging/container/requirements.txt /usr/share/doc/platefold/requirements.txt
# Nothing installs packages at runtime, and pip's vendored copies of msgpack and
# setuptools fail the release vulnerability scan, so pip leaves the final image.
RUN /usr/local/bin/python3 -m pip uninstall --yes --quiet --root-user-action=ignore pip && \
    /opt/venv/bin/python -m pip uninstall --yes --quiet --root-user-action=ignore pip && \
    fc-cache -f && dpkg-query -W > /usr/share/doc/platefold/os-packages.txt
# Neither Playwright Chromium archive ships Chromium's license inventory as a
# file, so keep the installed browser's own chrome://credits beside the notices.
RUN --mount=type=bind,source=packaging/container/chromium_credits.py,target=/tmp/chromium_credits.py \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers HOME=/tmp/credits-home \
    /opt/venv/bin/python /tmp/chromium_credits.py /usr/share/doc/platefold/chromium-credits.html.gz && \
    rm -rf /tmp/credits-home
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers HOME=/tmp/home \
    XDG_CACHE_HOME=/tmp/cache XDG_CONFIG_HOME=/tmp/config
ARG SOURCE_REVISION=uncommitted
ARG SOURCE_VERSION=unversioned
LABEL org.opencontainers.image.title="PlateFold" \
      org.opencontainers.image.description="Offline document-to-PDF CLI and authenticated browser server" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version=$SOURCE_VERSION \
      org.opencontainers.image.revision=$SOURCE_REVISION
USER 10001:10001
WORKDIR /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python", "/opt/platefold/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "platefold"]
CMD ["--help"]
