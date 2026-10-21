# Plate|Fold

Plate|Fold turns Markdown, Word (DOCX), Excel (XLSX), CSV and TSV files into tagged, print-quality PDFs. Mermaid diagrams stay vector graphics. A diagram or table too wide for a normal page gets a page it fits on: first a landscape plate, then a larger sheet, then a page cut to its shape. The rest of the document stays on Letter or A4.

This image runs the command-line exporter or the authenticated browser server. It includes Chromium, Mermaid and the fonts it needs, and makes no network requests while exporting.

[Source and documentation](https://github.com/maxfield-allison/platefold) · [Container guide](https://github.com/maxfield-allison/platefold/blob/main/docs/guides/container.md) · [Releases](https://github.com/maxfield-allison/platefold/releases)

## Tags

- `latest`: the newest stable release
- `vX.Y.Z`: one release. Release tags are intended to stay fixed; pin the image digest when you need an immutable reference.

Every tag is a multi-architecture image for `linux/amd64` and `linux/arm64`. The same images are on GitHub Container Registry as `ghcr.io/maxfield-allison/platefold`.

Before a release is published, both architectures pass the same tests against the exact image you pull: an offline command-line export, an authenticated server export, Chromium's own sandbox check, restart recovery and cancellation. Each image also passes a scan that fails on any fixed high or critical vulnerability. The arm64 test host runs without AppArmor. If your arm64 host uses AppArmor, check the sandbox yourself with the command below before relying on it.

## Before you run it

Chromium sandboxes each page it renders. Inside a container that needs a few system calls Docker's default seccomp profile blocks, so Plate|Fold ships its own profile: `packaging/container/seccomp.json` in the source repository. Download it from the release tag that matches your image. The profile adds the calls Chromium needs to create its sandbox. The container still runs with no capabilities, as UID 10001, with a read-only filesystem; the profile is not a complete security guarantee.

Don't work around a startup failure with `--privileged`, `--cap-add SYS_ADMIN`, an unconfined profile or Chromium's `--no-sandbox`. Those remove the protection the sandbox exists for.

Check the sandbox on your host:

```console
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --security-opt seccomp=seccomp.json \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 \
  --entrypoint python probablyfinedev/platefold:latest /opt/platefold/sandbox_check.py
```

## Export from the command line

Give the container an input directory and an output directory that UID 10001 can write to:

```console
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --security-opt seccomp=seccomp.json \
  --memory 3g --cpus 2 --pids-limit 256 --shm-size 256m \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 \
  --mount type=bind,src=/absolute/input,dst=/input,readonly \
  --mount type=bind,src=/absolute/output,dst=/output \
  probablyfinedev/platefold:latest /input -o /output --json
```

The PDFs and an export report land in the output directory. Add `--help` in place of the paths to see every option.

## Run the browser server

The server exports documents that people upload through a web page, behind a token. It is meant for one person or a trusted team on one server; run a single replica against its own private data directory. The source repository has a Compose file (`packaging/container/compose.yaml`) and a Kubernetes example with the resource limits, probes and security settings already filled in. The container guide linked above walks through both, including how to provide the token file without putting it in an environment variable.

Kubernetes nodes need the OCI form of the seccomp profile: `seccomp-kubernetes.json` for amd64 nodes and `seccomp-kubernetes-arm64.json` for arm64 nodes.

## What else is in the image

Each image carries an SPDX software bill of materials and SLSA build provenance, attached to the image index. You can read them with `docker buildx imagetools inspect probablyfinedev/platefold:<tag> --format '{{json .SBOM}}'` (or `.Provenance`). They are not signatures. Each image is signed separately with the PlateFold release Cosign key, `packaging/cosign.pub` in the source repository. With that key saved as `cosign.pub`, check an image with `cosign verify --key cosign.pub probablyfinedev/platefold:<tag>`. Do not run the image unless verification succeeds. A failure can mean the image is not the signed release, or that verification could not complete because the registry, signature data or tool was unavailable. See the container guide linked above for verification details.

Updating means pulling a new tag and recreating the container. Nothing inside the image updates itself.

## License

Plate|Fold is MIT licensed. Chromium, Mermaid, the fonts and the other bundled components keep their own licenses; the notices are in `/usr/share/doc/platefold` inside the image.
