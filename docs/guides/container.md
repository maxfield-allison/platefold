# Run Plate|Fold in a container

The Linux image, for amd64 and arm64, includes the CLI, authenticated browser server, Chromium, Mermaid and fonts. Noto system fallback fonts include Chinese, Japanese and Korean coverage; fonts installed only on your workstation are not available to the server. Exports need no runtime downloads.

Releases from 1.0.0 publish a multi-architecture image as `ghcr.io/maxfield-allison/platefold` and `probablyfinedev/platefold` on Docker Hub, tagged `vX.Y.Z` and `latest`. Pin a version tag, or better its digest, for deployment. You can also build the image from the release source, as below.

## Build and check the image

From the repository root:

```console
docker buildx build --pull --platform linux/amd64 --tag platefold:candidate .
python3 tools/test_container.py platefold:candidate ./container-evidence
```

On an arm64 host, build with `--platform linux/arm64` instead. Build on a host of the target architecture: the test runs the image, so an emulated build would test the emulator rather than the image.

The test creates disposable containers and a named volume, checks Chromium's own sandbox status, exports Markdown and Mermaid with networking disabled, then tests an authenticated server export and recovery after restart. It keeps synthetic PDFs, reports and a receipt, and removes its containers and volume. It requires Python 3 and Docker on the test host; it does not install either.

The base images are pinned by digest. Python runtime dependencies use hashes exported from `uv.lock`; npm uses `package-lock.json`. Debian packages come from a dated snapshot. The browser download is selected by a pinned Playwright version and every payload file is checked against the manifest for its architecture, `packaging/container/browser-amd64.sha256` or `browser-arm64.sha256`. Playwright uses Chrome for Testing on amd64 and its own Chromium build on arm64; both are Chromium 151. Playwright, Node and the frontend compiler are absent from the final runtime. These controls pin inputs; byte-identical rebuilds have not been established.

CI also generates SPDX inventory and SLSA build provenance using a pinned BuildKit SBOM generator. `tools/container_artifacts.py` checks the OCI descriptor hashes and extracts both attestations from a `docker save` archive. Neither attestation is a signature. Published images are signed separately with Cosign; see [Verify a published image](#verify-a-published-image). The image retains component license files and an installed OS package inventory. See `packaging/container/NOTICE.md` for the syscall profile's source and license.

Published images come from the project's private build pipeline, not from a tag. Once the public `vX.Y.Z` tag exists, the maintainer starts a protected pipeline on the build project's `main` with `PLATEFOLD_CONTAINER_RELEASE=vX.Y.Z`. Before building anything, that run checks that the public tag holds exactly the public snapshot of the commit it is building. It then builds each architecture on a native runner and pushes it to the CI registry as a candidate. The full container test runs against that exact digest under both seccomp profiles, and a vulnerability scan fails on any fixed high or critical finding. Only then are the two candidates joined into one multi-architecture index. The maintainer then starts the manual publish job, which repeats the public-tag check and copies that index to both public registries without rebuilding it. Before writing anything public, the job checks that `packaging/cosign.pub` holds the release public key and that the Cosign signing key in its environment belongs to that public key. It refuses to continue if either check fails. After the copies, it signs each registry's copy of the index by digest and verifies each signature against `packaging/cosign.pub`; only then does `latest` move. The publish job logs in to GHCR with `GHCR_USERNAME` and `GHCR_TOKEN` (a classic token limited to `write:packages`) and to Docker Hub with `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`. Those four and the signing key's `COSIGN_PRIVATE_KEY` and `COSIGN_PASSWORD` are protected variables scoped to the `platefold/publish-registry` environment, and the public tag lookup uses no GitHub credential. `tools/container_release.py` implements these steps; the evidence for each release is kept with its build pipeline.

## Export from the CLI

Prepare an input directory and an output directory writable by numeric UID 10001. Use absolute host paths. The image runs as UID/GID 10001 by default.

```console
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --security-opt seccomp=packaging/container/seccomp.json \
  --memory 3g --cpus 2 --pids-limit 256 --shm-size 256m \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 \
  --mount type=bind,src=/absolute/input,dst=/input,readonly \
  --mount type=bind,src=/absolute/output,dst=/output \
  platefold:candidate /input -o /output --json
```

Only scratch and output need writes for this command. Package files and the input mount stay read-only. Scratch includes the browser profile and diagram cache and is discarded with the container. Keep the exported PDFs and report in the output mount. A one-shot CLI container has no HTTP server, so its HTTP healthcheck is irrelevant; use the command's exit status and report.

## Run the browser server with Compose

`packaging/container/compose.yaml` provides the server command and its resource, filesystem and syscall settings. Configure these environment variables for Compose:

| Variable | Value |
| --- | --- |
| `PLATEFOLD_IMAGE` | The qualified image reference, pinned to `@sha256:…` |
| `PLATEFOLD_DATA` | Absolute private data directory, owned by UID/GID 10001 |
| `PLATEFOLD_TOKENS_FILE` | Absolute credential JSON file, UID 10001, mode 0600 |
| `PLATEFOLD_ORIGIN` | Browser-facing origin; default `http://127.0.0.1:8787` |
| `PLATEFOLD_HEALTH_HOST` | That origin's authority, including any port |

Provision the credential file through your secret manager as described in the [browser guide](server.md). Values do not belong in Compose, environment files, command arguments or URLs. The named variables above contain paths and addresses. The image does not change host file ownership for you.

```console
docker compose -f packaging/container/compose.yaml config --quiet
docker compose -f packaging/container/compose.yaml up -d
```

The example binds only the host's loopback interface. Open `http://127.0.0.1:8787`. For remote browser access, put your TLS proxy in front, set the exact HTTPS origin and healthcheck authority, and preserve Host. The [browser guide](server.md#slow-and-abusive-clients) has an example proxy configuration. Do not expose the unauthenticated proxy-to-server hop beyond its trusted network. Health probes are unauthenticated, check storage and renderer discovery, and obey the same Host allowlist. A ready probe is not proof of a completed render.

The supplied container limits allow one concurrent export and 2 GiB of summed worker-process RSS, inside a 3 GiB container memory limit. Summed RSS counts shared pages more than once: a small diagram with the full browser exceeded the API's default 1 GiB budget in qualification. The outer memory/CPU/PID limits remain necessary. The server's upload, scratch, deadline and retention limits still apply. Completed results persist in `/data/jobs`; scratch under `/tmp` does not. Use a bounded filesystem or volume quota for durable storage as well as retention.

## Chromium sandbox requirements

Use the supplied seccomp profile with `cap_drop: ALL` and `no-new-privileges`. It starts from Playwright's default-deny profile with namespace creation enabled, then permits `chroot` inside Chromium's user namespace. Without that last allowance, Chromium exits during zygote startup even though a user namespace can be created. The kernel still requires the appropriate capability inside that namespace; this does not give the container host capabilities.

Check the actual renderer under the same restrictions as an export:

```console
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --security-opt seccomp=packaging/container/seccomp.json \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 \
  --entrypoint python platefold:candidate /opt/platefold/sandbox_check.py
```

Qualification passed on Debian 13 / Docker 29.7.2 and a clean Ubuntu 24.04 guest / Docker 29.1.3 for amd64, and on a Raspberry Pi 5 running Debian 13 with the Raspberry Pi 16 KiB page kernel and Docker 29.7.2 for arm64. That arm64 host has AppArmor disabled, so the arm64 result says nothing about an AppArmor policy. Ubuntu retained `apparmor_restrict_unprivileged_userns=1` and Docker's `docker-default` AppArmor profile. No host-wide namespace exception was needed. Hosts with a different AppArmor policy must qualify that policy before advertising support. Do not solve startup failures with `--privileged`, `SYS_ADMIN`, unconfined seccomp/AppArmor, host PID/IPC namespaces or Chromium's `--no-sandbox`.

The upstream [Playwright container guidance](https://playwright.dev/docs/docker) explains the namespace allowance; [Docker's seccomp documentation](https://docs.docker.com/engine/security/seccomp/) describes how profiles filter syscalls.

## Kubernetes and updates

`packaging/container/kubernetes.yaml` is a single-replica Deployment, Service and PVC example. It uses `Recreate` so an update does not start two servers against the same store. Replace the image placeholder and HTTPS authority, provision the referenced Secret, and install `seccomp-kubernetes.json` in the kubelet seccomp directory on eligible amd64 nodes before scheduling; arm64 nodes use `seccomp-kubernetes-arm64.json` under the same installed name, with the example's `kubernetes.io/arch` selector changed to match. Pod policy must permit that specific `Localhost` profile. The Docker profile contains conditional fields that are absent from the OCI schema; the Kubernetes files resolve them for one architecture each with zero capabilities. Run `python3 tools/container_seccomp.py --check` after changing the source profile. The example explicitly requests RuntimeDefault AppArmor. On Ubuntu, an unconfined pod can be denied user namespaces even when the seccomp profile allows them. Qualify the runtime profile and use nodes with AppArmor support.

The non-root init container copies the projected credential into a private 0600 memory-backed file because Kubernetes Secret files are root-owned. Secret values are never printed. It also creates or restores the owned job directory to mode 0700 after kubelet volume permission handling. `fsGroupChangePolicy` limits recursive permission changes on subsequent mounts; the explicit ownership and no-symlink check keeps an unrelated directory from being adopted. Integrate your existing TLS ingress with the Service while preserving the configured Host.

The example's NetworkPolicy admits traffic to port 8787 only from your ingress controller's namespace and allows the pod no egress, because renders are offline. Set its namespace label before applying. Clients then meet the controller's header and body timeouts and per-client connection limits before they reach the server; configure those as the [browser guide](server.md#slow-and-abusive-clients) describes. A network plugin that enforces policy on kubelet probe traffic also needs a rule for the nodes.

The renderer needs no Internet access. Configure a workload PID limit through the kubelet/runtime policy. The manifest is an example, not a live deployment or a claim that every cluster's admission and storage policy has been tested.

Updates replace the whole image. Pull and verify a new immutable digest, preserve the job volume and credential principal names, then recreate the single server. Running jobs are interrupted; retained completed results survive ordinary restarts. Keep the prior image digest and a data backup before upgrading. Do not install packages or run a desktop updater inside the container.

Published images are signed with the PlateFold release Cosign key. Each also carries SPDX and SLSA attestations, and each release's pipeline keeps its test and scan evidence.

## Verify a published image

Each release image is signed with [Cosign](https://docs.sigstore.dev/cosign/system_config/installation/) using the PlateFold release key, `packaging/cosign.pub` in the source repository. The signature is recorded in the public Sigstore transparency log, so checking it needs only Cosign and the public key. Replace `vX.Y.Z` with the release you deploy:

```console
curl -fsSLO https://raw.githubusercontent.com/maxfield-allison/platefold/vX.Y.Z/packaging/cosign.pub
cosign verify --key cosign.pub ghcr.io/maxfield-allison/platefold:vX.Y.Z
```

For Docker Hub, verify `probablyfinedev/platefold:vX.Y.Z` the same way. Cosign lists the checks it made and exits with status 0 when the signature is valid. Do not run the image unless verification succeeds. A failure can mean the image is not the signed release, or that verification could not complete because the registry, signature data or tool was unavailable. Verifying by tag checks the digest the tag points to at that moment, so deploy that digest rather than the tag.
