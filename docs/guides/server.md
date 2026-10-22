# Export in a browser

The optional Linux server runs the same PDF engine behind an authenticated job API. Open its address in a browser to use the shared theme and export settings, upload DOCX, XLSX, Markdown, CSV, TSV or a ZIP bundle, and download the finished PDFs. DOC, ODT and RTF also work when the operator enables legacy conversion and supplies a compatible converter. The server is intended for one person or a trusted team self-hosting a single instance. The [container image](container.md) packages the same server.

## Start a local development server

From a source checkout with the development tools installed, build the shared interface:

```console
npm ci --prefix desktop
npm run build --prefix desktop
```

Install the `server` extra alongside Plate|Fold. Supply an operator-owned, mode-0600 JSON credential file mapping principal names to distinct random tokens of 32-512 printable characters. Generate each token with a cryptographic random generator, such as `openssl rand -base64 32`. The server refuses a token that is repetitive, sequential or drawn from a small alphabet, such as a placeholder or a repeated word; that check catches mistakes but cannot prove a token is random. Use your secret manager to provision the file; do not put credentials in command arguments or URLs. Retaining a principal name when rotating its token preserves ownership of its retained jobs.

```console
platefold serve --data-dir ./job-data --tokens-file /run/secrets/platefold-api.json
```

The job directory holds other people's documents, so the server insists on owning it privately. It creates a missing directory at mode 0700 itself. **It refuses to start on an existing directory that the group or anyone else can reach**, which is what a pre-created directory or a mounted volume usually is:

```text
ValueError: Job storage must be an owned, private, unlinked absolute directory
```

`chmod 700` the directory and give it to the user the server runs as. The same applies to the credential file: if another user owns it, or its group or anyone else has any access to it, the server exits with `Invalid server configuration: Credential file must be owned by this user and private (0600)`. Configuration errors describe the problem without printing credential values. A file the server can't read or parse, such as invalid JSON, gives only the general `Invalid server configuration; check credentials, origins, mounts and limits.`

Open `http://127.0.0.1:8787`. The login page and its self-hosted assets load without a credential. All API operations, including capabilities and downloads, require one. Chromium must be installed and discoverable on the server; the UI reports renderer discovery separately from a successful export.

The capabilities response also reports whether Chromium's sandbox was actually on. `pdf_engine.sandbox` is `enabled` or `disabled` as observed in the renderer processes of the most recent export, and `unverified` before any export has shown it; `sandbox_disabled` carries the same answer as `false`, `true` or `null`. Plate|Fold never turns the sandbox off, so `disabled` means something else added a flag such as `--no-sandbox`, for example a distribution's browser wrapper script. Fix that before rendering documents you don't trust. The answer comes from each renderer's command line and its kernel status: a sandbox-disabling flag counts at once, even in a short export, and a renderer without both sandbox layers counts once it has run for a second.

## Put it on a network

For a network listener, configure a numeric `--host` and an exact HTTPS `--origin` matching the browser-facing address. Terminate TLS at your reverse proxy and preserve the configured Host. Wildcard origins and forwarded-header trust are unsupported. Run a single Linux process against its own private job directory. The server takes an exclusive store lock and rejects a second instance.

Every request must carry a Host that matches a configured origin, and a browser's Origin header must match one exactly. A web page that points its own domain name at your server's address (DNS rebinding) is therefore refused, because the browser sends that page's name as the Host.

A plain `http://` origin is accepted only for a loopback address such as `127.0.0.1`, `[::1]` or `localhost`. Anywhere else it would send bearer credentials and documents across the network unencrypted, so the server refuses to start. On a network you trust with that traffic, `--allow-http-origin` overrides the check.

### Slow and abusive clients

The server gives each connection 10 seconds to send a complete request head, closes idle keep-alive connections after 5 seconds, rejects request heads over 16 KiB and serves at most 64 connections at once. Request bodies and downloads have their own deadlines. These limits stop a slow client from holding a connection indefinitely, but not a client that keeps opening new ones: 64 connections trickling headers can still turn other requests away with 503 until they time out.

When the server is reachable by anyone you don't trust, put a buffering reverse proxy in front that limits each client, and don't let clients reach the server's port directly. This nginx example terminates TLS, holds slow clients itself and forwards each request only once it has arrived in full:

```nginx
limit_conn_zone $binary_remote_addr zone=platefold_clients:10m;

server {
    listen 443 ssl;
    server_name platefold.example.org;
    ssl_certificate     /etc/ssl/platefold/fullchain.pem;
    ssl_certificate_key /etc/ssl/platefold/privkey.pem;

    # Slow clients wait here, not in the server's 64 connections.
    client_header_timeout 10s;
    client_body_timeout   30s;
    send_timeout          60s;
    limit_conn platefold_clients 16;

    # At least the server's upload_bytes (16 MiB by default).
    client_max_body_size 17m;
    proxy_request_buffering on;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        # Must match the configured --origin, here https://platefold.example.org.
        proxy_set_header Host $host;
    }
}
```

`$host` carries no port. If browsers reach the proxy on a non-default port, send `$host:$server_port` and include that port in `--origin`. The limits and timeouts are starting points; size `limit_conn` to the number of people who share one address. On Kubernetes, apply the equivalent header and body timeouts, connection limit and body size in your ingress controller, and use the NetworkPolicy in the [container guide](container.md#kubernetes-and-updates) so that only the controller reaches the pod.

## Choose, export, download

1. Enter the API credential supplied by your administrator and select **Connect / recover job**. The field is cleared immediately; the credential stays only in page memory.
2. Choose one `.docx`, `.xlsx`, `.md`, `.csv` or `.tsv` file, or a `.zip` containing supported documents and local images. A single upload is named `document` with its source extension on the server. Use a ZIP to preserve filenames, directories and image references. `.doc`, `.odt` and `.rtf` require the server operator to enable `legacy_conversion` and install a supported converter; the official Plate|Fold image does not include one.
3. Alternatively, choose a server mount visible to your credential and enter a path relative to that mount. The server copies the selected source into the job. Arbitrary host paths are unavailable.
4. Select paper, a built-in theme or **source document**, fonts, contents and bookmarks. Source imports each usable DOCX or XLSX palette and requested fonts, including after legacy conversion; conversion can change those details, and formats without a usable theme use the server's Bristol fallback. API clients may also supply `xlsx_sheets` as exact names or one-based indexes, with the same `#2` and `name:` rules as `--sheets`; the browser interface exports all visible worksheets. Palette edits apply to the export. The browser does not save or delete server theme files.
5. Select **Export PDFs**. Progress reports queued, rendering and cancelling states. **Cancel export** asks the server to stop the job and clean up its worker processes.
6. Open **View results** and download each PDF or the JSON report. Downloads use authenticated requests. Ordinary links cannot carry the credential.

The preview pane is a **style sample**, not your document or its pagination. Inspect the downloaded PDF for the actual result. The report includes document page/figure counts and warning counts, plus the full text of font warnings and each document's `font_fallback` details, which name only fonts and the document's own characters. Missing figures also remain visible in the PDF. Internal paths and renderer logs are excluded.

## Reloads, failures and limits

Reloading or disconnecting clears the credential. The tab remembers only the last opaque job ID in session storage. Reconnect to recover that job's progress or completed results. An unfinished upload cannot resume: select its file again and start a new export. An uncertain network response is never automatically retried as a new job.

A server restart marks unfinished jobs interrupted. Completed results survive ordinary restarts until their retention period expires. Expired jobs and jobs belonging to another principal both appear unavailable. Disconnecting the UI does not cancel a running export; use the cancel button before disconnecting when that is your intention.

Default limits are 16 MiB per upload and source file, 64 MiB expanded source or OOXML package, 128 source or OOXML entries, 32 documents, 100,000 table cells per document (`table_cells`), 128 sheet declarations per workbook, two simultaneous exports, 180 seconds per export and one hour of result retention. The export time limit (`export_seconds` in the `--limits-file` JSON) counts from the moment the job starts running, so it includes worker start-up but not time spent waiting in the queue. When it runs out, the server stops every process the export started and the job fails with `deadline_exceeded`, usually within a second. CSV byte limits inherit that operator-owned per-file ceiling; clients cannot raise them. The connection panel shows the server's actual upload and retention settings. ZIP/OOXML traversal and unsafe external relationships, configuration files and nested archives are rejected. XLSX hyperlinks, external workbook references, linked images and OLE objects are never followed; stable report warnings and visible boxes replace content where needed. Invalid or oversized uploads produce a typed failure; fix the bundle and create a new job. Failed, cancelled and interrupted jobs expose no partial PDFs.

A large table prints in parts, each its own browser pass, and the parts are joined into one PDF with the pages a single pass would print. The size of each part follows `memory_bytes`: at the default 1 GiB, a part holds about 11,000 table cells. This reduces peak rendering memory; it does not guarantee every document fits. The guardian samples process memory and stops an export that exceeds the limit, so use a container memory limit as well for a hard deployment boundary. Raising `table_cells` allows more work; keep `export_seconds` and `cpu_seconds` in step with it.

Each unfinished job reserves `scratch_bytes` plus 1 MiB of `store_bytes`; at the defaults the server holds three. One principal may hold every reservation but one, so another principal can always start a job. A server sized for a single reservation serves one job at a time for everyone. Jobs count toward `owner_jobs` while unfinished or while they retain results; cancelled and failed jobs do not.

DOC, ODT and RTF are refused by default. Converting them runs an installed LibreOffice or Pandoc, and LibreOffice is not a filesystem or network sandbox; on a server, the people uploading files are remote. An operator who runs the server inside an administered sandbox can set `"legacy_conversion": true` in the `--limits-file` JSON. The official container image includes no converter, so there the setting changes nothing unless you add one. Clients see `legacy_conversion` in the capabilities response. A direct DOC, ODT or RTF upload is refused with `legacy_conversion_disabled`; such files inside a ZIP upload or on a mount are accepted with the job and each fails during the export with `converter_disabled`.

Download sizes are checked against the manifest. On HTTPS and loopback secure contexts, SHA-256 is also checked using the browser's Web Crypto API. The downloadable report and API result manifest support independent inspection. Keep downloaded PDFs before their server retention expires.

## Building a server wheel

The CI package job includes the built UI. To produce the same package locally, build the frontend first, then stage it before building the wheel:

```console
python tools/bundle_web.py
python -m build
```

A source-only wheel without staged assets can still run the API, but does not advertise a browser interface. The CLI and desktop keep their own local file access and process transport.
