import type {
  Catalog,
  DocumentResult,
  ExportResult,
  Transport,
  WorkerEvent,
} from "./transport";
import { localizeLiteral, number, t, tOr } from "./i18n";
export interface WebCapabilities extends Catalog {
  schema_version: number;
  browser_adapter: boolean;
  mounts: string[];
  limits: {
    upload_bytes: number;
    output_bytes: number;
    retention_seconds: number;
  };
  pdf_engine: { discovered: boolean };
  storage_ready: boolean;
}
interface Job {
  id: string;
  state: string;
  error?: { code: string };
}
interface Artifact {
  id: string;
  name: string;
  bytes: number;
  sha256: string;
  media_type: string;
}
interface PublicReport {
  engine: string;
  documents: {
    file: string;
    pages: number;
    figures: number;
    missing_images: number;
    diagram_errors: number;
    warnings: number;
    font_warnings?: string[];
    font_fallback?: DocumentResult["font_fallback"];
    conversion?: {
      name: string;
      version: string;
      input_format: string;
      output_format: string;
      sandbox: string;
    } | null;
  }[];
}
const LAST_JOB = "platefold.last-job";
const terminal = new Set([
  "completed",
  "completed_with_warnings",
  "failed",
  "cancelled",
  "interrupted",
]);
class RequestError extends Error {
  constructor(
    public code: string,
    message: string,
  ) {
    super(message);
  }
}
function remembered(): string {
  try {
    return sessionStorage.getItem(LAST_JOB) ?? "";
  } catch {
    return "";
  }
}
function remember(id: string) {
  try {
    sessionStorage.setItem(LAST_JOB, id);
  } catch {
    /* Storage is optional. */
  }
}

/** Same-origin HTTP only. Credentials never enter URLs or persistent storage. */
export class HttpTransport implements Transport {
  readonly web = this;
  private token = "";
  private emit: (event: WorkerEvent) => void = () => {};
  private disconnected: (message: string) => void = () => {};
  private generation = 0;
  private timer?: ReturnType<typeof setTimeout>;
  private upload?: AbortController;
  private job = "";
  private cursor = 0;
  private file?: File;
  private mount?: { kind: "mount"; mount: string; path: string };
  private caps?: WebCapabilities;
  private artifacts: Artifact[] = [];
  private pdfs: Artifact[] = [];
  async connect(
    onEvent: (event: WorkerEvent) => void,
    onError: (message: string) => void,
  ) {
    this.emit = onEvent;
    this.disconnected = onError;
  }
  private event(event: string, fields: Partial<WorkerEvent> = {}) {
    this.emit({ schema_version: 1, event, ...fields });
  }
  private path(suffix = "") {
    return `/api/v1/jobs/${this.job}${suffix}`;
  }
  private async request(
    path: string,
    init: RequestInit = {},
  ): Promise<Response> {
    if (!/^\/api\/v1\//.test(path)) throw new Error("Invalid API path");
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.token}`);
    let response: Response;
    try {
      response = await fetch(path, {
        ...init,
        headers,
        credentials: "omit",
        redirect: "error",
        cache: "no-store",
        signal: init.signal ?? AbortSignal.timeout(15000),
      });
    } catch {
      throw new RequestError("connection_lost", t("http.connection_lost"));
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new RequestError(
        body.error?.code ?? "request_failed",
        `${tOr(`server.error.${body.error?.code}`, body.error?.message ?? t("http.request_failed"))} (${number(response.status)}${response.status === 429 ? t("http.retry_after") : ""})`,
      );
    }
    return response;
  }
  async authenticate(token: string): Promise<WebCapabilities> {
    this.disconnect(false);
    if (!/^[\x21-\x7e]{32,512}$/.test(token))
      throw new Error(t("http.credential"));
    this.token = token;
    const generation = this.generation;
    try {
      this.caps = await (await this.request("/api/v1/capabilities")).json();
      if (generation !== this.generation)
        throw new Error(t("http.connection_cancelled"));
      if (this.caps?.schema_version !== 1 || !this.caps.browser_adapter)
        throw new Error(t("http.browser_unsupported"));
      this.event("catalog", { catalog: this.caps });
      this.event("connection", { connected: true });
      return this.caps;
    } catch (error) {
      this.token = "";
      throw error;
    }
  }
  disconnect(notify = true) {
    this.generation++;
    clearTimeout(this.timer);
    this.upload?.abort();
    this.token = "";
    this.file = undefined;
    this.mount = undefined;
    this.caps = undefined;
    this.artifacts = [];
    this.pdfs = [];
    this.event("connection", { connected: false });
    if (notify) this.disconnected(t("http.disconnected"));
  }
  selectFile(file: File): string {
    if (!/\.(doc|docx|odt|rtf|xlsx|md|csv|tsv|zip)$/i.test(file.name))
      throw new Error(t("http.source_type"));
    if (!this.caps || file.size > this.caps.limits.upload_bytes)
      throw new Error(t("http.upload_limit"));
    if (!file.size) throw new Error(t("http.nonempty_source"));
    this.file = file;
    this.mount = undefined;
    return file.name;
  }
  selectMount(mount: string, path: string): string {
    if (!this.caps?.mounts.includes(mount))
      throw new Error(t("http.mount_available"));
    if (
      path.startsWith("/") ||
      path.split("/").some((p) => p === "..") ||
      /[\\:\x00-\x1f]/.test(path)
    )
      throw new Error(t("http.mount_relative"));
    this.mount = { kind: "mount", mount, path };
    this.file = undefined;
    return `${mount}/${path}`;
  }
  async choose(): Promise<string | null> {
    throw new Error(t("http.native_paths"));
  }
  async usePath(): Promise<string> {
    throw new Error(t("http.native_paths"));
  }
  async send(command: string, fields: Record<string, unknown> = {}) {
    if (command === "catalog") {
      if (this.caps) this.event("catalog", { catalog: this.caps });
      return;
    }
    if (command === "export") return this.start(fields.settings);
    if (command === "cancel") return this.cancel();
    throw new Error(t("http.desktop_required"));
  }
  async resume() {
    const id = remembered();
    if (!/^[a-f0-9]{32}$/.test(id)) return;
    this.job = id;
    this.cursor = 0;
    this.event("job_state", { message: t("http.recovering"), active: true });
    await this.poll(this.generation);
  }
  private async start(settings: unknown) {
    if (!this.mount && !this.file) throw new Error(t("http.choose_source"));
    clearTimeout(this.timer);
    const generation = ++this.generation;
    this.artifacts = [];
    this.pdfs = [];
    this.cursor = 0;
    this.job = "";
    const lowerName = this.file?.name.toLowerCase() ?? "";
    const format = lowerName.endsWith(".zip")
      ? "zip"
      : lowerName.endsWith(".doc")
        ? "doc"
        : lowerName.endsWith(".docx")
          ? "docx"
          : lowerName.endsWith(".odt")
            ? "odt"
            : lowerName.endsWith(".rtf")
              ? "rtf"
              : lowerName.endsWith(".xlsx")
                ? "xlsx"
                : lowerName.endsWith(".csv")
                  ? "csv"
                  : lowerName.endsWith(".tsv")
                    ? "tsv"
                    : "markdown";
    const source = this.mount ?? { kind: "upload", format };
    const job: Job = await (
      await this.request("/api/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, settings }),
      })
    ).json();
    if (generation !== this.generation) return;
    this.job = job.id;
    remember(job.id);
    if (this.file) {
      this.event("job_state", { message: t("http.uploading"), active: true });
      this.upload = new AbortController();
      try {
        await this.request(this.path("/source"), {
          method: "PUT",
          headers: {
            "Content-Type": {
              zip: "application/zip",
              doc: "application/msword",
              docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              odt: "application/vnd.oasis.opendocument.text",
              rtf: "application/rtf",
              xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              csv: "text/csv",
              tsv: "text/tab-separated-values",
              markdown: "text/markdown",
            }[format],
          },
          body: this.file,
          signal: AbortSignal.any([
            this.upload.signal,
            AbortSignal.timeout(65000),
          ]),
        });
      } catch (error) {
        if (generation !== this.generation) return;
        this.event("job_state", {
          message: localizeLiteral(error),
          active: true,
        });
      } finally {
        this.upload = undefined;
      }
    }
    await this.poll(generation);
  }
  private async cancel() {
    if (!this.job) throw new Error(t("http.not_admitted"));
    // Let the server record cancellation before disconnecting an in-flight upload.
    await this.request(this.path("/cancel"), { method: "POST" });
    const generation = ++this.generation;
    this.upload?.abort();
    clearTimeout(this.timer);
    await this.poll(generation);
  }
  private async poll(generation: number) {
    if (generation !== this.generation) return;
    try {
      const job: Job = await (await this.request(this.path())).json();
      if (generation !== this.generation) return;
      if (terminal.has(job.state)) {
        if (job.state.startsWith("completed"))
          await this.complete(job, generation);
        else
          this.event("error", {
            id: "export",
            code: job.error?.code ?? job.state,
            message: t("http.export_terminal", {
              state: job.state,
              code: job.error?.code
                ? t("http.code_suffix", { code: job.error.code })
                : "",
            }),
          });
        return;
      }
      if (job.state === "awaiting_upload") {
        await this.request(this.path("/cancel"), { method: "POST" });
        this.event("error", {
          id: "export",
          message: t("http.upload_interrupted"),
        });
        return;
      }
      this.event("job_state", {
        message:
          job.state === "cancelling"
            ? t("status.cancelling")
            : job.state === "queued"
              ? t("http.queued")
              : t("http.rendering"),
        active: true,
      });
      const events = await (
        await this.request(this.path(`/events?after=${this.cursor}`))
      ).json();
      if (generation !== this.generation) return;
      this.cursor = events.next_after;
      for (const event of events.events)
        if (event.event === "progress")
          this.event("progress", {
            done: event.done,
            total: event.total,
            file: event.file,
          });
      this.timer = setTimeout(() => void this.poll(generation), 1000);
    } catch (error) {
      if (generation !== this.generation) return;
      if (error instanceof RequestError && error.code === "job_not_found") {
        remember("");
        this.event("error", {
          id: "export",
          message: t("http.job_unavailable"),
        });
      } else {
        this.disconnect(false);
        this.disconnected(localizeLiteral(error));
      }
    }
  }
  private async bytes(artifact: Artifact): Promise<Blob> {
    if (
      !/^\d+$/.test(artifact.id) ||
      artifact.bytes > (this.caps?.limits.output_bytes ?? 0)
    )
      throw new Error(t("http.result_manifest_invalid"));
    const response = await this.request(this.path(`/results/${artifact.id}`), {
      signal: AbortSignal.timeout(65000),
    });
    const blob = await response.blob();
    if (blob.size !== artifact.bytes)
      throw new Error(t("http.download_incomplete"));
    if (crypto.subtle) {
      const digest = await crypto.subtle.digest(
        "SHA-256",
        await blob.arrayBuffer(),
      );
      const hex = Array.from(new Uint8Array(digest), (x) =>
        x.toString(16).padStart(2, "0"),
      ).join("");
      if (hex !== artifact.sha256) throw new Error(t("http.checksum_mismatch"));
    }
    return blob;
  }
  private async complete(job: Job, generation: number) {
    const manifest = await (await this.request(this.path("/results"))).json();
    const artifacts: Artifact[] = manifest.results;
    const reportFile = artifacts.find((a) => a.name === "report.json");
    if (!reportFile) throw new Error(t("http.report_unavailable"));
    const report: PublicReport = JSON.parse(
      await (await this.bytes(reportFile)).text(),
    );
    if (generation !== this.generation) return;
    this.artifacts = artifacts;
    this.pdfs = report.documents.map((doc) => {
      const artifact = artifacts.find(
        (a) =>
          a.name ===
            doc.file.replace(
              /\.(doc|docx|odt|rtf|xlsx|md|csv|tsv)$/i,
              ".pdf",
            ) && a.media_type === "application/pdf",
      );
      if (!artifact) throw new Error(t("http.pdf_missing"));
      return artifact;
    });
    const result: ExportResult = {
      status: job.state,
      output_dir: "",
      total_documents: report.documents.length,
      report: {
        engine: report.engine,
        documents: report.documents.map((doc) => ({
          ...doc,
          missing_images: [],
          diagram_errors: [],
          warnings:
            doc.missing_images + doc.diagram_errors + doc.warnings
              ? [
                  t("http.report_counts", {
                    missing: number(doc.missing_images),
                    diagrams: number(doc.diagram_errors),
                    warnings: number(doc.warnings),
                  }),
                  ...(doc.font_warnings ?? []),
                ]
              : [],
        })),
      },
    };
    this.event("result", { result });
  }
  async open(index: number) {
    await this.save(this.pdfs[index]);
  }
  async downloadReport() {
    await this.save(this.artifacts.find((a) => a.name === "report.json"));
  }
  private async save(artifact?: Artifact) {
    if (!artifact) throw new Error(t("http.result_unavailable"));
    const generation = this.generation;
    const blob = await this.bytes(artifact);
    if (generation !== this.generation) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = artifact.name.split("/").pop()!;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
