# Security Policy

## Release status

Security fixes are provided for the current `1.x` release line. Development builds and prereleases are unsupported. This policy does not promise a response or fix deadline.

## Report a vulnerability privately

Please do not open a public issue for a suspected vulnerability.

Email [security@probablyfine.dev](mailto:security@probablyfine.dev). Do not include exploit details, private documents, or secrets in that first message.

Include as much of the following as you safely can:

- The affected version, commit, and package type
- The operating system and architecture
- A description of the impact and the conditions required to reproduce it
- A minimal proof of concept using synthetic content
- Any workaround you have found

Plate|Fold turns local DOC/DOCX/ODT/RTF, XLSX, Markdown, CSV/TSV tables, images, configuration, and browser-rendered content into PDFs. Those inputs and outputs can contain sensitive information. Remove names, paths, document contents, PDF metadata, tokens, and other private data before sharing a reproduction or an export report.

## Generated link policy

Generated PDFs preserve `http:`, `https:`, and `mailto:` links plus navigation to headings inside the same document. Other targets (including `file:`, `data:`, script-like URLs, relative paths, and operating-system custom protocols) remain visible as text but are not clickable in the PDF. The export report warns when it neutralizes one of these targets.

## Hostile and unusually large documents

Plate|Fold treats OOXML/DOCX/XLSX, Markdown, CSV/TSV tables, referenced images, inline data images, Mermaid source, rendered SVG, and the final PDF as untrusted rendering input. Generous default ceilings bound each document's source bytes, individual image bytes and pixels, animated-image frames, CSV/TSV rows, columns, cells and field lengths, XLSX used-range cells per sheet and across the selected sheets, sheet declarations, XML elements and attributes per DOCX or XLSX package, aggregate embedded assets, Mermaid source and SVG bytes, final PDF bytes, and total export time.

Before semantic conversion, every DOCX or XLSX ZIP central directory is bounded by entry count, member name length, supported compression method, per-entry size, aggregate expanded size and compression ratio. Each member is decompressed through counted reads that check the actual bytes, not the sizes the archive declares. Every XML and relationship part is streamed through `defusedxml` as it decompresses, with DTDs/entities refused and nesting depth and the package's combined count of XML elements and attributes checked at each start tag, so a small file packed with millions of tiny elements stops at the budget instead of being built in memory first. DOCX has the lower default budget because its converter holds several copies of each element. An image part placed from many drawings or anchors is carried once, and distinct images count toward the embedded-asset limit while the document is still being read. DOCX external relationships remain refused. XLSX permits only recognized link/image/OLE relationship kinds so it can replace them safely; targets are never fetched, carried into the PDF or included in reports. Mammoth's generated HTML is allowlist-sanitized. Third-party parser failures are returned as invalid-OOXML errors rather than internal traces. Plate|Fold never downloads fonts or loads font files embedded in source documents. DOCX-carried SVG images use the same defused XML boundary as inline and referenced SVGs; DTDs, entities, malformed XML and unsupported embedded image media types are refused before Chromium.

### Operator-installed legacy-document converters

DOC, ODT and RTF are passed through a compatible converter already installed on the machine, then through the same bounded DOCX reader described above. Plate|Fold does not ship LibreOffice, Pandoc or converter data files in its Python, desktop, AppImage or container packages. Plate|Fold looks on `PATH` first, then in each converter's standard install locations (the Windows program folders and the registry entries LibreOffice's installer writes, `%LOCALAPPDATA%\Pandoc`, `/Applications` and `~/Applications`, and Homebrew and MacPorts on macOS), so an executable in any of those places is trusted as the operator's converter. The export report records the selected converter and version. Conversion is a semantic bridge, not page reproduction: styles, layout, active content and source-theme details can be lost or changed.

Plate|Fold copies the input into a private temporary directory and gives the converter private home, configuration, cache, profile, temporary and output paths. It uses an explicit argument list without a shell, drains only a bounded amount of diagnostic output, limits observed temporary entries and bytes, stays inside the export deadline, rejects missing, extra, linked, special, escaping or oversized output, and kills the owned process tree on cancellation, timeout and completion. The directory is removed after DOCX ingestion.

Pandoc is used only for ODT/RTF when runtime probes advertise that input, DOCX output and its built-in [`--sandbox`](https://pandoc.org/MANUAL.html#option--sandbox); the conversion command enables that sandbox. Pandoc documents that the sandbox constrains readers and writers, and that some builds cannot access their DOCX data files while sandboxed. In that case Plate|Fold reports the route as failed instead of retrying without it.

LibreOffice uses headless safe mode and a disposable user profile, but those controls are **not a filesystem or network sandbox**. The process still runs with the Plate|Fold user's operating-system permissions, and Plate|Fold does not audit LibreOffice's parsers or prove that a document cannot trigger a local read or network request. Use LibreOffice conversion only with trusted legacy files, or put the whole Plate|Fold process in a separately administered container or OS sandbox with the filesystem and network policy the workload requires. The hardened DOCX validation happens after conversion and cannot undo access that occurred inside the external converter. The HTTP server refuses DOC, ODT and RTF unless its operator sets `legacy_conversion` in the limits file, because its users are remote.

These ceilings can be lowered for a deployment in the `limits` section of a `platefold.yml` or `platefold.json` settings file; see `platefold.example.yml` for every setting and its default. Configuration and theme files have a fixed 1 MiB ceiling because they cannot safely raise the limit used to read themselves.

Image checks cover Markdown images, HTML image attributes, literal CSS data resources and images nested inside SVGs. Asset accounting is conservative: unselected responsive-image candidates and unused CSS declarations still count. SVG resource animations with ambiguous lists of data URLs are rejected. This preflight checks literal resources; it does not evaluate the browser's complete CSS cascade or establish a process-wide memory ceiling.

Exceeding a limit stops the current document with an explicit error. Any PDFs completed earlier in the batch remain published, while the current document's staged output and the prior export report remain untouched.

## What to expect

Reports are handled on a best-effort basis. The maintainer will confirm receipt when possible, ask for missing information, and coordinate next steps for confirmed vulnerabilities.

## Current security checks

Release checks include:

- A repository secrets scan with Gitleaks
- A dependency scan for high and critical vulnerabilities with Trivy
- A CycloneDX source and dependency SBOM retained beside Python build artifacts
- Browser integration tests run as an unprivileged user with Chromium's sandbox enabled
- Builds from pinned package-tooling inputs, with package checksums and a machine-readable build receipt

These checks reduce risk, but they are not a guarantee. The retained SBOM describes source dependencies; it is not a signed attestation for every native package. Platform signing and package verification are recorded separately for each release.

## Desktop update trust

In-app updates use two separate keys, both pinned in the application. The metadata key signs the update feed: which release is offered, its notes, and a validity of at most seven days, renewed daily by an automated job. The artifact key, held only by the protected release process, signs each installer. That signature also names the installer's version, platform and file name in its Minisign trusted comment, which the signature covers.

The app installs an update only when the feed's metadata signature verifies, the version is newer than the installed one, the installer is downloaded from the project's own GitHub release for that version, and the downloaded installer matches its signed size and SHA-256 and carries an artifact-key signature naming exactly the offered version, platform and file. The release-download location is compiled into the app, so the feed cannot point a download anywhere else. The release checks refuse to publish a feed that fails the same test.

Someone who obtains the metadata key can withhold updates, write the release notes shown with an offer, and choose which signed release newer than a user's installed version is offered, including one older than the latest. They cannot make the app install bytes the artifact key did not sign, relabel a signed build as another version, or send the download to another host. Closing the remaining choice of release needs an index of releases signed by the release key; that is planned for a later version, before choosing among older releases becomes possible.

The app never asks for an administrator password to update. A copy in a folder your account can't change, such as an app another account installed, shows manual update steps instead.

## Known advisories in bundled components

The bundled Mermaid renderer contains DOMPurify 3.4.0 and KaTeX 0.16.45, which are in the ranges of advisories GHSA-p98j-92pf-mc4p, GHSA-6688-9rhm-gjv2 and GHSA-238p-pmpm-9mq7. As Plate|Fold calls Mermaid, none of them is reachable: the DOMPurify advisories need its `IN_PLACE` mode on a live page element, which Mermaid does not use, and the KaTeX advisory needs an attacker to have already polluted JavaScript's object prototype. Diagrams also render under a content security policy that blocks scripts and network requests. The bundle will be replaced when Mermaid ships with patched versions.

## Coordinated disclosure

Please allow reasonable time to investigate and fix a vulnerability before publishing details. For a confirmed vulnerability, the maintainer will coordinate disclosure timing with the reporter and provide credit unless the reporter prefers to remain anonymous.
