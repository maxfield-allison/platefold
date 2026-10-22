import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export interface ContrastRule {
  fg: string; // token name, or a literal #rrggbb for the fixed callout colours
  bg: string;
  minimum: number; // 0 for decorative pairings, which are reported but not scored
  kind: "text" | "large-text" | "graphic" | "callout" | "decorative";
  label: string;
  alternate?: [string, string] | null;
}
export interface ContrastFailure extends ContrastRule {
  ratio: number;
  alternate_ratio?: number | null;
  passes: boolean | null;
}
export interface Theme {
  name: string;
  label: string;
  description: string;
  mode: "light" | "dark";
  builtin: boolean;
  // Where the file lives decides what the app may do to it. Only "user"
  // themes can be overwritten or deleted; built-ins and project themes beside
  // the documents are read-only here.
  kind?: "builtin" | "user" | "project" | "source";
  path?: string;
  file?: string;
  colors: Record<string, string>;
  contrast_failures?: ContrastFailure[];
}
export interface Catalog {
  themes: Theme[];
  font_sets: {
    name: string;
    label: string;
    body: string;
    head: string;
    mono: string;
  }[];
  contrast_rules?: ContrastRule[];
  warnings?: string[];
}
export interface FontResolution {
  role: "body" | "head" | "mono";
  requested: string;
  used: string[];
  status: "available" | "substituted" | "unverified" | "generic";
  scope: "representative-text";
}
export interface FontFallback {
  font: string;
  fallback: string | null;
  characters: number;
  occurrences: number;
  scripts: string[];
  sample: string[];
}
export interface DocumentResult {
  font_resolution?: FontResolution[];
  font_fallback?: {
    status: "complete" | "partial" | "unverified";
    characters_checked: number;
    fallbacks: FontFallback[];
  };
  file: string;
  pages: number;
  figures: number;
  missing_images: string[];
  diagram_errors: string[];
  warnings: string[];
  conversion?: {
    name: string;
    version: string;
    input_format: string;
    output_format: string;
    sandbox: string;
  } | null;
  theme?: string;
  font_set?: string;
}
export interface ExportResult {
  elapsed_seconds?: number;
  status: string;
  output_dir: string;
  total_documents: number;
  report: { engine: string; documents: DocumentResult[] };
}
export interface WorkerEvent {
  output_path?: string;
  output_error?: string;
  schema_version: number;
  event: string;
  id?: string;
  message?: string;
  message_id?: string;
  help?: string;
  help_id?: string;
  connected?: boolean;
  active?: boolean;
  code?: string;
  catalog?: Catalog;
  name?: string;
  path?: string;
  result?: ExportResult;
  phase?: string;
  done?: number;
  total?: number;
  file?: string;
  preview?: {
    file: string;
    pages: number | null;
    width: number;
    height: number;
    image: string;
    text: string;
    warnings: string[];
  };
}
export interface Transport {
  readonly updates?: import("./updates").UpdateTransport;
  readonly web?: import("./http").HttpTransport;
  connect(
    onEvent: (event: WorkerEvent) => void,
    onError: (message: string) => void,
  ): Promise<void>;
  choose(kind: "file" | "folder" | "output"): Promise<string | null>;
  usePath(kind: "source" | "output", path: string): Promise<string>;
  send(command: string, fields?: Record<string, unknown>): Promise<void>;
  open(index: number): Promise<void>;
}

export const desktop: Transport = {
  updates: {
    status: () => invoke("update_status"),
    preferences: (preferences) => invoke("update_preferences", { preferences }),
    check: () => invoke("check_updates"),
    download: () => invoke("download_update"),
    cancel: () => invoke("cancel_update"),
    install: (restart) => invoke("install_update", { restart }),
    restart: () => invoke("restart_update"),
    onClosePending: async (listener) => {
      await listen("update-close-pending", () => listener());
    },
  },
  async connect(onEvent, onError) {
    await listen<WorkerEvent>("worker-event", (e) => onEvent(e.payload));
    await listen<string>("worker-exit", (e) => onError(e.payload));
    await invoke("connect_worker");
  },
  choose: (kind) => invoke("choose_path", { kind }),
  usePath: (kind, path) => invoke("set_path", { kind, path }),
  send: (command, fields = {}) =>
    invoke("send_worker", {
      message: { schema_version: 1, id: command, command, ...fields },
    }),
  open: (index) => invoke("open_result", { index }),
};
