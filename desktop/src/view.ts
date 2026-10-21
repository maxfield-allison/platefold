import { mountUpdates } from "./updates";
import { mountWebControls } from "./web-controls";
import type {
  Catalog,
  ContrastRule,
  Theme,
  Transport,
  WorkerEvent,
  ExportResult,
} from "./transport";
import { paperOptions, suggestedPaper } from "./paper";
import { advancedControls, advancedMarkup } from "./advanced";
import {
  html,
  interfaceLocale,
  localizeLiteral,
  localizeTree,
  number as formatNumber,
  plural,
  t,
  tOr,
} from "./i18n";

// Token editor groups, in the order the design lays them out. Tokens the
// worker adds later land in a trailing group rather than disappearing.
const GROUPS: [string, string[]][] = [
  [
    t("theme.group.structure"),
    [
      "page",
      "ink",
      "ink_strong",
      "ink_soft",
      "title",
      "heading",
      "link",
      "accent",
      "accent_soft",
      "rule",
      "surface",
      "surface_alt",
    ],
  ],
  [
    t("theme.group.diagrams"),
    [
      "diagram_fill",
      "diagram_stroke",
      "diagram_text",
      "diagram_alt_fill",
      "diagram_note_fill",
      "diagram_note_stroke",
    ],
  ],
];
const DIAGRAM_WARNING = t("theme.diagram_warning");
const DARK_ADVISORY = t("theme.dark_advisory");

// WCAG 2.1 relative luminance and contrast ratio, mirrored from
// platefold/contrast.py so the readout updates on every keystroke without a
// round trip to the worker. The worker ships the pairing table.
export function luminance(hex: string): number {
  const channel = (i: number) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}
export function contrastRatio(a: string, b: string): number {
  const [low, high] = [luminance(a), luminance(b)].sort((x, y) => x - y);
  return (high + 0.05) / (low + 0.05);
}
interface Verdict {
  rule: ContrastRule;
  ratio: number;
  alternate?: number;
  passes: boolean | null;
}
function evaluate(
  rules: ContrastRule[],
  colors: Record<string, string>,
): Verdict[] {
  const colour = (token: string) =>
    token.startsWith("#") ? token : colors[token];
  const verdicts: Verdict[] = [];
  for (const rule of rules) {
    const fg = colour(rule.fg),
      bg = colour(rule.bg);
    if (!fg || !bg) continue;
    const ratio = contrastRatio(fg, bg);
    let alternate: number | undefined;
    if (rule.alternate) {
      const altFg = colour(rule.alternate[0]),
        altBg = colour(rule.alternate[1]);
      if (altFg && altBg) alternate = contrastRatio(altFg, altBg);
    }
    const effective =
      alternate === undefined ? ratio : Math.max(ratio, alternate);
    verdicts.push({
      rule,
      ratio,
      alternate,
      passes: rule.kind === "decorative" ? null : effective >= rule.minimum,
    });
  }
  return verdicts;
}
const describe = (v: Verdict) =>
  t("contrast.on", {
    foreground: v.rule.fg.startsWith("#")
      ? t("contrast.warning_callout")
      : v.rule.fg,
    background: v.rule.bg,
    ratio: formatNumber(v.ratio, { maximumFractionDigits: 2 }),
  }) +
  (v.rule.kind === "decorative"
    ? ` · ${t("contrast.decorative_detail")} · ${v.rule.label}`
    : ` · ${t("contrast.needs", { minimum: formatNumber(v.rule.minimum) })} · ${v.rule.label}` +
      ` · ${v.passes ? t("contrast.meets") : t("contrast.below")}`);

export function mount(root: HTMLElement, transport: Transport) {
  root.innerHTML = `
    <header class="masthead"><h1 class="brand"><span class="mark" aria-hidden="true"><i></i><i></i></span><span aria-hidden="true">plate<span class="fold">|</span>fold</span><span class="sr-only">PlateFold</span></h1>
      <span class="tagline">A little more room for the whole idea.</span>
      <button id="appearance" class="text-button" aria-pressed="false">Dark interface</button><span class="build-label">DESKTOP</span></header>
    <main>
      <section class="source-bar" aria-label="Documents and destination">
        <div class="source-choice"><div class="source-heading"><span class="eyebrow">SOURCE</span><div class="button-row"><button id="choose-file">Choose file</button><button id="choose-folder">Choose folder</button></div></div><p id="source-path" class="path">Choose a DOC, DOCX, ODT, RTF, XLSX, Markdown, CSV, or TSV file, or a folder of documents.</p><details class="path-details"><summary>Enter source path</summary><div class="path-entry"><input id="source-entry" aria-label="Source path" placeholder="Paste a file or folder path"><button id="use-source">Use path</button></div></details></div>
        <div class="destination"><div class="source-heading"><span class="eyebrow">DESTINATION</span><button id="choose-output">Choose output folder <span aria-hidden="true">↗</span></button></div><p id="output-path" class="path">PDFs keep your source folder structure.</p><details class="path-details"><summary>Enter output path</summary><div class="path-entry"><input id="output-entry" aria-label="Output folder path" placeholder="Paste an existing folder path"><button id="use-output">Use path</button></div></details></div>
      </section>
      <div class="workspace-tabs" role="tablist" aria-label="Workspace view"><button id="settings-tab" role="tab" aria-selected="true" aria-controls="settings-pane">Settings</button><button id="preview-tab" role="tab" aria-selected="false" aria-controls="preview-pane" tabindex="-1">Preview</button></div>
      <section class="workspace" data-view="settings" aria-label="Theme and export settings">
        <section id="preview-pane" class="preview-pane" aria-label="Theme preview"><div class="preview-toolbar"><div class="preview-heading"><span id="preview-label" class="eyebrow">STYLE SAMPLE</span><span id="paper-label">LETTER</span></div>
          <div class="preview-state"><span id="preview-spinner" aria-hidden="true" hidden></span><p id="preview-status" aria-live="polite">Choose a document to preview its first page.</p><button id="preview-warning-toggle" class="warning-toggle" aria-expanded="false" aria-controls="preview-warnings" hidden></button><button id="refresh-preview" class="text-button" disabled>Refresh preview</button></div>
          </div><div id="preview-scroll" class="pane-scroll" tabindex="0" role="region" aria-label="Preview page"><div id="preview-warnings" class="warning-details" hidden></div><figure id="document-preview" hidden><img id="preview-image" hidden><figcaption id="preview-caption"></figcaption></figure>
          <article id="paper" class="paper"><div class="running-head"><span>FIELD NOTES</span><span>PLATE 01</span></div>
            <h2>A place for the whole idea.</h2><div class="accent-rule"></div><p class="sample-intro">Some things need a wider page.</p>
            <h3>1. Follow the details</h3><p>A good document keeps the text readable and gives its diagrams enough room. Body pages and larger plates can share the same PDF.</p>
            <table><thead><tr><th>On the page</th><th>In the export</th></tr></thead><tbody><tr><td>Clear headings</td><td>PDF bookmarks</td></tr><tr><td>Mermaid diagrams</td><td>Vector figures</td></tr><tr><td>Referenced images</td><td>Embedded locally</td></tr></tbody></table>
            <pre><code>platefold ./notes -o ./pdfs</code></pre><div class="sample-diagram" aria-label="Documents to layout to PDF"><span>Documents</span><i aria-hidden="true">→</i><span>Layout</span><i aria-hidden="true">→</i><span>PDF</span></div>
            <p class="caption">Figure 1 — Room to follow the connections.</p><footer><span>PLATE|FOLD</span><span>01</span></footer>
          </article><p id="preview-note" class="preview-note">Style sample only. Choose a document for a rendered first page. PDF bookmarks change navigation, not page appearance.</p></div></section>
        <aside id="settings-pane" class="settings-pane pane-scroll" tabindex="0" aria-label="Export settings"><div class="pane-heading"><span class="eyebrow">EXPORT SETTINGS</span><button id="choose-theme" aria-haspopup="dialog"><span id="theme-name">Loading themes…</span><span aria-hidden="true"> &#9662;</span></button><p id="theme-description" class="pane-note"></p><p id="theme-kind" class="theme-kind"></p><p id="mode-advisory" class="advisory" role="note" hidden>${html(DARK_ADVISORY)}</p></div>
          <fieldset id="settings"><legend class="sr-only">Paper, fonts and palette</legend>
            <label class="setting">Output mode<select id="output-mode" aria-label="Output mode"><option value="online">Online · flexible page sizes</option><option value="print">Print · keep diagrams whole</option><option value="tiled">Tiled print · full-size diagrams</option></select></label>
            <p id="layout-note" class="pane-note"></p>
            <label class="setting">Paper<select id="preset" aria-label="Paper">${paperOptions.map(([value, label]) => `<option value="${value}">${html(label)}</option>`).join("")}</select></label>
            <label class="setting">Font set<select id="font" aria-label="Font set"></select></label><button id="refresh-fonts" class="text-button" type="button">Refresh fonts</button><p id="font-note" class="pane-note"></p>
            <label class="setting">Orientation<select id="orientation" aria-label="Orientation"><option value="portrait">Portrait</option><option value="landscape">Landscape</option></select></label>
            <label class="setting">Page margins<select id="margins" aria-label="Page margins"><option value="full">Full margin</option><option value="none">No margin</option><option value="bottom-right">Bottom and right only</option></select></label>
            <p id="margin-note" class="pane-note"></p>
            <div class="checks"><label><input id="toc" type="checkbox" checked> Contents page</label><label><input id="bookmarks" type="checkbox" checked> PDF bookmarks</label><label><input id="csv-header" type="checkbox" checked> First CSV/TSV row is a header</label></div>
            <label class="setting">Document language<input id="document-language" type="text" maxlength="255" placeholder="From source" aria-describedby="document-language-help"></label>
            <p id="document-language-help" class="pane-note">Use a language tag such as en or en-GB. Leave empty to use source metadata. This controls PDF language, independently of the interface.</p>
            ${advancedMarkup}
            ${transport.web ? "" : `<details id="renderer-options"><summary>${html(t("renderer.options"))}</summary><label class="setting">${html(t("renderer.label"))}<select id="renderer" aria-label="${html(t("renderer.label"))}"><option value="auto">${html(t("renderer.bundled"))}</option><option value="system">${html(t("renderer.system"))}</option></select></label><p class="pane-note">${html(t("renderer.note"))}</p></details>`}
            <details id="palette-editor"><summary>Customise palette</summary><div class="token-heading"><span class="eyebrow">PALETTE</span><button id="revert" class="text-button" type="button">Revert</button></div><div id="tokens"></div><p id="contrast-summary" class="contrast-summary" aria-live="polite"></p><p class="pane-note">Ratios follow WCAG 2.1: 4.5:1 for text, 3:1 for large text and informative graphics. Table hairlines, secondary borders and note borders are decorative and reported without a verdict.</p></details>
            <div class="theme-actions"><button id="save-copy" type="button">Save as a copy…</button><button id="overwrite" type="button">Overwrite</button><button id="delete-theme" type="button" aria-haspopup="dialog">Delete</button></div><p id="theme-note" class="pane-note"></p><p id="theme-error" role="alert" hidden></p>
          </fieldset></aside>
      </section>
      <section class="job-panel" aria-label="Export progress and actions"><div class="job-summary"><div><span class="eyebrow">EXPORT</span><p id="status" role="status" aria-live="polite">Connecting to the export worker…</p><p id="export-timing" class="pane-note" hidden></p><progress id="progress" max="1" value="0" hidden aria-label="Documents exported"></progress></div><div class="button-row"><button id="view-results" aria-haspopup="dialog" hidden>View results</button><button id="cancel" hidden>Cancel export</button><button id="export" class="primary" disabled>Export PDFs <span aria-hidden="true">↗</span></button></div></div></section>
    </main>
    <dialog id="theme-dialog" aria-label="Choose a theme"><div class="dialog-heading"><h2>Choose a theme</h2><div class="button-row"><button id="new-from-current" class="text-button" type="button">New from current…</button><button id="dismiss-themes" aria-label="Close theme picker">Close</button></div></div><div class="dialog-scroll" tabindex="0">        <div class="theme-picker">
          <div class="filters" aria-label="Filter themes"><button data-filter="all" aria-pressed="true">All</button><button data-filter="light" aria-pressed="false">Light</button><button data-filter="dark" aria-pressed="false">Dark</button></div>
          <div id="themes" aria-label="Available themes"></div><p class="pane-note">Document colours are separate from the app’s appearance. Dark themes suit screen reading; light themes suit printing.</p></div>
</div></dialog>
    <dialog id="results-dialog" aria-label="Export results"><div class="dialog-heading"><h2>Export results</h2><button id="dismiss-results">Close results</button></div><div class="dialog-scroll" tabindex="0" role="region" aria-label="Result details"><p id="error" role="alert" hidden></p><div id="results"></div></div></dialog>
    <dialog id="save-dialog" aria-label="Save a theme copy"><form id="copy-form"><span class="eyebrow">YOUR PALETTE</span><h2>Keep a copy.</h2><label class="setting">Theme name<input id="copy-name" required pattern="[a-z][a-z0-9-]{0,47}" maxlength="48" placeholder="my-field-notes" autocomplete="off"></label><p class="pane-note">Lowercase letters, digits and hyphens. Existing themes stay unchanged.</p><p id="copy-error" role="alert"></p><div class="button-row"><button id="dismiss-copy" type="button">Cancel</button><button id="confirm-copy" class="primary">Save copy</button></div></form></dialog>
    <dialog id="delete-dialog" aria-labelledby="delete-title"><span class="eyebrow">DELETE THEME</span><h2 id="delete-title">Delete this theme?</h2><p id="delete-summary" class="pane-note"></p><p id="delete-error" role="alert"></p><div class="button-row"><button id="dismiss-delete" type="button">Cancel</button><button id="confirm-delete" class="danger" type="button">Delete</button></div></dialog>`;

  localizeTree(root);

  const el = <T extends HTMLElement = HTMLElement>(id: string) =>
    root.querySelector<T>(`#${id}`)!;
  const pathMarker = "\uFFF0";
  const deletePath = document.createElement("code");
  deletePath.id = "delete-path";
  const [beforePath, afterPath] = t("theme.delete_summary", {
    path: pathMarker,
  }).split(pathMarker);
  el("delete-summary").replaceChildren(beforePath, deletePath, afterPath);
  let catalog: Catalog = { themes: [], font_sets: [] };
  let selected: Theme | undefined;
  let colors: Record<string, string> = {};
  let source = "",
    output = "",
    filter = "all",
    busy = false,
    connected = false;
  let previewRevision = 0;
  let previewTimer: ReturnType<typeof setTimeout> | undefined;
  let previewDirty = false;
  let summaryKey = "";
  let updates: ReturnType<typeof mountUpdates> | undefined;
  const advanced = advancedControls(
    root,
    () => {
      paperControls();
      controls();
      schedulePreview();
    },
    !!transport.web,
  );
  const pills = new Map<string, HTMLElement>();
  const hexEntries = new Map<string, HTMLInputElement>();
  const pickers = new Map<string, HTMLInputElement>();

  const kindOf = (theme: Theme) =>
    theme.kind ?? (theme.builtin ? "builtin" : "user");
  const dirty = () =>
    !!selected &&
    Object.keys(colors).some((key) => colors[key] !== selected!.colors[key]);
  const darkPage = () =>
    selected?.mode === "dark" ||
    (!!colors.page && luminance(colors.page) < 0.2);

  function error(message: unknown) {
    el("error").textContent = localizeLiteral(message);
    el("error").hidden = false;
    el("view-results").hidden = false;
    const dialog = el<HTMLDialogElement>("results-dialog");
    if (!dialog.open) dialog.showModal();
    el("error").scrollIntoView({ block: "nearest" });
  }
  function status(message: string) {
    el("status").textContent = message;
    el("status").title = message;
  }
  let cancelling = false;
  function controls() {
    const restoreExportFocus = document.activeElement === el("cancel") && !busy;
    updates?.setBusy(busy);
    el<HTMLButtonElement>("export").disabled =
      busy ||
      !!updates?.blocksExport() ||
      !connected ||
      !source ||
      !output ||
      !selected ||
      !advanced.valid();
    el<HTMLFieldSetElement>("settings").disabled = busy || !selected;
    for (const id of [
      "choose-file",
      "choose-folder",
      "choose-output",
      "choose-theme",
      "use-source",
      "use-output",
    ])
      el<HTMLButtonElement>(id).disabled = busy || !connected;
    for (const id of ["source-entry", "output-entry"])
      el<HTMLInputElement>(id).disabled = busy || !connected;
    for (const button of root.querySelectorAll<HTMLButtonElement>(
      "[data-theme], [data-filter]",
    ))
      button.disabled = busy;
    el<HTMLButtonElement>("refresh-preview").disabled =
      busy || !connected || !source;
    el("cancel").hidden = !busy;
    el<HTMLButtonElement>("cancel").disabled = false;
    el("cancel").setAttribute("aria-disabled", String(cancelling));
    if (restoreExportFocus) {
      (el("view-results").hidden ? el("export") : el("view-results")).focus();
    }
    if (transport.web) {
      el<HTMLFieldSetElement>("web-sources").disabled = busy || !connected;
    }
    const edited = dirty();
    const kind = selected ? kindOf(selected) : undefined;
    el<HTMLButtonElement>("save-copy").disabled =
      busy || !connected || !selected;
    el<HTMLButtonElement>("new-from-current").disabled =
      busy || !connected || !selected;
    el<HTMLButtonElement>("overwrite").disabled =
      busy || !connected || kind !== "user" || !edited;
    el<HTMLButtonElement>("delete-theme").disabled =
      busy || !connected || kind !== "user";
    el<HTMLButtonElement>("revert").disabled = !edited;
    el<HTMLSelectElement>("font").disabled = busy || !selected;
    el<HTMLButtonElement>("refresh-fonts").disabled = busy || !connected;
    el("mode-advisory").hidden = !selected || !darkPage();
  }
  updates = transport.updates
    ? mountUpdates(root, transport.updates, controls, status)
    : undefined;
  async function send(command: string, fields: Record<string, unknown> = {}) {
    try {
      await transport.send(command, { ui_locale: interfaceLocale, ...fields });
    } catch (e) {
      if (command === "export") {
        busy = false;
        stopExportTime();
        el("progress").hidden = true;
        if (previewDirty) schedulePreview();
      }
      error(e);
      controls();
    }
  }
  function preview() {
    for (const [key, value] of Object.entries(colors))
      el("paper").style.setProperty(`--${key.replaceAll("_", "-")}`, value);
  }
  function rendererChoice(): string {
    return transport.web ? "auto" : el<HTMLSelectElement>("renderer").value;
  }
  function settings() {
    const colorOverrides = Object.fromEntries(
      Object.entries(colors).filter(
        ([key, value]) => value !== selected!.colors[key],
      ),
    );
    const result: Record<string, unknown> = {
      ...advanced.read(),
      theme: selected!.name,
      colors: colorOverrides,
      preset: el<HTMLSelectElement>("preset").value,
      output_mode: el<HTMLSelectElement>("output-mode").value,
      margin_mode: el<HTMLSelectElement>("margins").value,
      orientation: el<HTMLSelectElement>("orientation").value,
      locale_hint: navigator.language,
      document_language: el<HTMLInputElement>("document-language").value.trim(),
      toc: el<HTMLInputElement>("toc").checked,
      bookmarks: el<HTMLInputElement>("bookmarks").checked,
      csv_header: el<HTMLInputElement>("csv-header").checked,
    };
    const font = el<HTMLSelectElement>("font").value;
    if (font && font !== "__source__") result.font_set = font;
    return result;
  }
  function previewState(message: string, generating = false) {
    el("preview-status").textContent = message;
    el("preview-warning-toggle").hidden = true;
    el("preview-warning-toggle").setAttribute("aria-label", t("warning.show"));
    el("preview-warning-toggle").title = t("warning.show");
    el("preview-warning-toggle").setAttribute("aria-expanded", "false");
    el("preview-warnings").hidden = true;
    el("preview-spinner").hidden = !generating;
    el("document-preview").setAttribute("aria-busy", String(generating));
  }
  function schedulePreview() {
    preview();
    const font = catalog.font_sets.find(
      (f) => f.name === el<HTMLSelectElement>("font").value,
    );
    if (font) {
      for (const key of ["body", "head", "mono"] as const)
        el("paper").style.setProperty(`--font-${key}`, font[key]);
    }
    if (transport.web) return;
    clearTimeout(previewTimer);
    const revision = ++previewRevision;
    if (!source || !selected || !connected) return;
    if (!advanced.valid()) {
      previewState(t("status.preview_check_layout"));
      return;
    }
    previewDirty = true;
    el("paper").hidden = true;
    el("document-preview").hidden = false;
    el("document-preview").dataset.stale = "true";
    el("preview-label").textContent = t("preview.document");
    el("preview-note").textContent = t("preview.first_page_note");
    previewState(
      el("preview-image").hidden
        ? t("preview.generating")
        : t("preview.generating_stale"),
      true,
    );
    if (busy) {
      previewState(t("preview.after_export"));
      return;
    }
    previewTimer = setTimeout(async () => {
      try {
        await transport.send("preview", {
          id: `preview-${revision}`,
          browser: rendererChoice(),
          settings: settings(),
        });
      } catch (e) {
        if (revision !== previewRevision) return;
        previewState(t("status.preview_unavailable", { message: String(e) }));
      }
    }, 500);
  }
  // Recompute every pairing after any edit: a change to `page` moves the
  // verdict of every token drawn on it.
  function renderContrast() {
    const verdicts = evaluate(catalog.contrast_rules ?? [], colors);
    for (const [token, pill] of pills) {
      const involved = verdicts.filter(
        (v) => v.rule.fg === token || v.rule.bg === token,
      );
      const scored = involved.filter((v) => v.passes !== null);
      const detail = involved.map(describe).join("\n");
      pill.title = detail;
      delete pill.dataset.verdict;
      if (!involved.length) {
        pill.textContent = "";
        pill.removeAttribute("aria-label");
        continue;
      }
      if (!scored.length) {
        pill.textContent = t("contrast.decorative");
        pill.setAttribute(
          "aria-label",
          t("contrast.token_label", { token, detail }),
        );
        continue;
      }
      const failing = scored.filter((v) => !v.passes);
      const worst = (failing.length ? failing : scored).reduce((a, b) =>
        a.ratio / a.rule.minimum <= b.ratio / b.rule.minimum ? a : b,
      );
      pill.dataset.verdict = failing.length ? "fail" : "pass";
      pill.textContent = failing.length
        ? t("contrast.below_short", {
            ratio: formatNumber(worst.ratio, { maximumFractionDigits: 2 }),
          })
        : t("contrast.pass_short", {
            ratio: formatNumber(worst.ratio, { maximumFractionDigits: 2 }),
          });
      pill.setAttribute(
        "aria-label",
        failing.length
          ? t("contrast.aria_failed", {
              token,
              failed: formatNumber(failing.length),
              total: formatNumber(scored.length),
              detail,
            })
          : plural("contrast.aria_passed", scored.length, { token, detail }),
      );
    }
    const scored = verdicts.filter((v) => v.passes !== null);
    const failing = scored.filter((v) => !v.passes);
    const key = failing.map((v) => `${v.rule.fg}/${v.rule.bg}`).join(",");
    const summary = el("contrast-summary");
    summary.dataset.state = failing.length ? "fail" : "pass";
    if (key === summaryKey && summary.textContent) return;
    summaryKey = key;
    summary.textContent = !scored.length
      ? ""
      : failing.length
        ? t("contrast.summary_failed", {
            failed: formatNumber(failing.length),
            total: formatNumber(scored.length),
            details: failing
              .map(
                (v) =>
                  `${t("contrast.on", {
                    foreground: v.rule.fg.startsWith("#")
                      ? t("contrast.warning_callout")
                      : v.rule.fg,
                    background: v.rule.bg,
                    ratio: formatNumber(v.ratio, { maximumFractionDigits: 2 }),
                  })} (${t("contrast.needs", { minimum: formatNumber(v.rule.minimum) })}, ${v.rule.label.toLocaleLowerCase(interfaceLocale)})`,
              )
              .join("; "),
          })
        : plural("contrast.summary_passed", scored.length);
  }
  function changed() {
    renderContrast();
    controls();
    schedulePreview();
  }
  function tokenRow(key: string) {
    const label = key.replaceAll("_", " ");
    const row = document.createElement("div");
    row.className = "token";
    row.dataset.token = key;
    const name = document.createElement("span");
    name.className = "token-name";
    name.textContent = label;
    const input = document.createElement("input");
    input.type = "color";
    input.value = colors[key];
    input.setAttribute("aria-label", t("contrast.colour_label", { label }));
    const hex = document.createElement("input");
    hex.type = "text";
    hex.className = "hex-entry";
    hex.value = colors[key];
    hex.maxLength = 7;
    hex.spellcheck = false;
    hex.autocomplete = "off";
    hex.setAttribute("aria-label", t("contrast.hex_label", { label }));
    const pill = document.createElement("span");
    pill.className = "contrast";
    const apply = (value: string) => {
      colors[key] = value;
      input.value = value;
      hex.value = value;
      changed();
    };
    input.oninput = () => apply(input.value);
    hex.oninput = () => {
      const match = /^#?([0-9a-f]{6})$/i.exec(hex.value.trim());
      if (!match) {
        hex.setAttribute("aria-invalid", "true");
        return;
      }
      hex.removeAttribute("aria-invalid");
      apply(`#${match[1].toLowerCase()}`);
    };
    hex.onchange = () => {
      if (hex.getAttribute("aria-invalid")) {
        hex.value = colors[key];
        hex.removeAttribute("aria-invalid");
      }
    };
    row.append(name, input, hex, pill);
    pills.set(key, pill);
    hexEntries.set(key, hex);
    pickers.set(key, input);
    return row;
  }
  let regularFont = "sora";
  function select(theme: Theme) {
    const wasSource = selected && kindOf(selected) === "source";
    const font = el<HTMLSelectElement>("font");
    if (kindOf(theme) === "source" && !wasSource) {
      regularFont = font.value || "sora";
      font.value = "__source__";
    } else if (kindOf(theme) !== "source" && font.value === "__source__") {
      font.value = regularFont;
    }
    const sourceOption = font.querySelector<HTMLOptionElement>(
      'option[value="__source__"]',
    );
    if (sourceOption) {
      sourceOption.disabled = kindOf(theme) !== "source";
      sourceOption.hidden = sourceOption.disabled;
    }
    selected = theme;
    colors = { ...theme.colors };
    const kind = kindOf(theme);
    el("theme-name").textContent = theme.name;
    el("theme-name").dir = "auto";
    el("choose-theme").setAttribute(
      "aria-label",
      t("theme.choose_named", { name: theme.name }),
    );
    el("theme-description").textContent = tOr(
      `theme.${theme.name}.description`,
      theme.description,
    );
    el("theme-kind").textContent =
      kind === "source"
        ? t("theme.kind.source")
        : kind === "builtin"
          ? t("theme.kind.builtin")
          : kind === "user"
            ? t("theme.kind.user", { path: theme.file ?? theme.name })
            : t("theme.kind.project");
    el("theme-note").textContent =
      kind === "source"
        ? t("theme.note.source")
        : kind === "builtin"
          ? t("theme.note.builtin")
          : kind === "user"
            ? t("theme.note.user", { path: theme.file ?? theme.name })
            : t("theme.note.project");
    if (transport.web && kind !== "source") {
      el("theme-kind").textContent = t("theme.kind.web");
      el("theme-note").textContent = t("web.theme_note");
    }
    el("theme-error").hidden = true;
    const tokens = el("tokens");
    tokens.replaceChildren();
    pills.clear();
    hexEntries.clear();
    pickers.clear();
    const placed = new Set<string>();
    const groups: [string, string[]][] = [
      ...GROUPS.map(
        ([title, keys]) =>
          [title, keys.filter((key) => key in colors)] as [string, string[]],
      ),
      [
        t("theme.group.other"),
        Object.keys(colors).filter(
          (key) => !GROUPS.some(([, keys]) => keys.includes(key)),
        ),
      ],
    ];
    for (const [title, keys] of groups) {
      if (!keys.length) continue;
      const heading = document.createElement("p");
      heading.className = "eyebrow";
      heading.textContent = title;
      tokens.append(heading);
      for (const key of keys) {
        tokens.append(tokenRow(key));
        placed.add(key);
      }
      if (title === t("theme.group.diagrams")) {
        const warning = document.createElement("p");
        warning.className = "diagram-warning";
        warning.setAttribute("role", "note");
        warning.textContent = DIAGRAM_WARNING;
        tokens.append(warning);
      }
    }
    summaryKey = "";
    renderContrast();
    schedulePreview();
    themes();
    controls();
  }
  function themes() {
    const list = el("themes");
    list.replaceChildren();
    for (const group of [true, false]) {
      const matches = catalog.themes.filter(
        (t) =>
          (kindOf(t) === "builtin") === group &&
          (filter === "all" || t.mode === filter),
      );
      if (!matches.length) continue;
      const heading = document.createElement("p");
      heading.className = "eyebrow";
      heading.textContent = group
        ? t("theme.group.builtin")
        : t("theme.group.other_sources");
      list.append(heading);
      for (const theme of matches) {
        const button = document.createElement("button");
        button.dataset.theme = theme.name;
        button.setAttribute("aria-label", theme.name);
        button.setAttribute(
          "aria-pressed",
          String(theme.name === selected?.name),
        );
        button.title = tOr(
          `theme.${theme.name}.description`,
          theme.description,
        );
        const swatch = document.createElement("span");
        swatch.className = "swatch";
        swatch.style.background = `linear-gradient(135deg, ${theme.colors.page} 0 45%, ${theme.colors.accent} 45% 72%, ${theme.colors.ink} 72%)`;
        swatch.setAttribute("aria-hidden", "true");
        const name = document.createElement("span");
        name.textContent = theme.name;
        button.append(swatch, name);
        if (theme.mode === "dark") {
          const tag = document.createElement("span");
          tag.className = "tag";
          tag.textContent = t("theme.tag.dark");
          button.append(tag);
        }
        if (theme.contrast_failures?.length) {
          const tag = document.createElement("span");
          tag.className = "tag warn";
          tag.textContent = t("theme.tag.contrast", {
            count: formatNumber(theme.contrast_failures.length),
          });
          button.append(tag);
        }
        button.onclick = () => {
          select(theme);
          el<HTMLDialogElement>("theme-dialog").close();
        };
        list.append(button);
      }
    }
  }
  function loadCatalog(data: Catalog, name?: string) {
    catalog = data;
    if (data.warnings?.length) error(data.warnings.join("\n"));
    const font = el<HTMLSelectElement>("font");
    const previous = font.value || "sora";
    font.replaceChildren();
    const sourceOption = document.createElement("option");
    sourceOption.value = "__source__";
    sourceOption.textContent = t("font.source");
    font.append(sourceOption);
    for (const installed of [false, true]) {
      const group = document.createElement("optgroup");
      group.label = t(
        installed
          ? transport.web
            ? "font.server_installed"
            : "font.installed"
          : "font.presets",
      );
      for (const item of data.font_sets.filter(
        (item) => item.name.startsWith("system:") === installed,
      )) {
        const option = document.createElement("option");
        option.value = item.name;
        const label = installed
          ? item.label
          : tOr(`font.${item.name}.label`, item.label);
        option.textContent = installed
          ? label
          : label.replace(/\s*\(.*\)$/, "");
        option.title = label;
        group.append(option);
      }
      if (group.children.length) font.append(group);
    }
    if (
      previous.startsWith("system:") &&
      !data.font_sets.some((item) => item.name === previous)
    ) {
      const missing = document.createElement("option");
      missing.value = previous;
      missing.textContent = t("font.unavailable", {
        family: previous.slice(7),
      });
      font.append(missing);
    }
    font.value = previous;
    if (!font.value) font.value = "sora";
    el("font-note").textContent = t(
      transport.web ? "font.server_note" : "font.local_note",
    );
    const theme =
      data.themes.find(
        (t) => t.name === (name || selected?.name || "bristol"),
      ) ||
      data.themes.find((t) => kindOf(t) === "builtin") ||
      data.themes[0];
    if (theme) select(theme);
  }
  const warningIcon =
    '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3 2 21h20L12 3Z"/><path d="M12 9v5m0 3v1"/></svg>';
  function warningParagraphs(warnings: string[]): HTMLParagraphElement[] {
    return warnings.map((warning) => {
      const p = document.createElement("p");
      p.className = "warning";
      p.textContent = warning.startsWith("DOCUMENT_LANGUAGE:")
        ? t("a11y.document_language_missing")
        : localizeLiteral(warning);
      return p;
    });
  }
  const warningToggle = el("preview-warning-toggle");
  warningToggle.innerHTML = warningIcon;
  warningToggle.setAttribute("aria-label", t("warning.show"));
  warningToggle.title = t("warning.show");
  warningToggle.onclick = () => {
    const panel = el("preview-warnings");
    panel.hidden = !panel.hidden;
    warningToggle.setAttribute("aria-expanded", String(!panel.hidden));
    warningToggle.setAttribute(
      "aria-label",
      t(panel.hidden ? "warning.show" : "warning.hide"),
    );
    if (!panel.hidden) panel.scrollIntoView({ block: "nearest" });
  };
  let exportStarted = 0;
  let exportTimer: ReturnType<typeof setInterval> | undefined;
  let exportKey = "";
  let estimatedSeconds: number | undefined;
  let activeFile = "";
  // Session-only history: no document paths or content are saved to disk.
  const exportDurations = new Map<string, number>();
  function duration(seconds: number): string {
    const rounded = Math.max(0, Math.ceil(seconds));
    return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, "0")}`;
  }
  function updateExportTime() {
    const elapsed = (performance.now() - exportStarted) / 1000;
    const expectation =
      estimatedSeconds === undefined
        ? t("export.estimate_unknown")
        : elapsed >= estimatedSeconds
          ? t("export.longer")
          : t("export.remaining", {
              time: duration(estimatedSeconds - elapsed),
            });
    el("export-timing").textContent =
      `${t("export.elapsed", { time: duration(elapsed) })} · ${expectation}`;
  }
  function stopExportTime(result?: ExportResult) {
    clearInterval(exportTimer);
    exportTimer = undefined;
    if (result && result.status !== "cancelled" && exportStarted) {
      const seconds =
        result.elapsed_seconds ?? (performance.now() - exportStarted) / 1000;
      exportDurations.set(exportKey, seconds);
      el("export-timing").textContent = t("export.elapsed", {
        time: duration(seconds),
      });
    } else {
      el("export-timing").hidden = true;
    }
    exportStarted = 0;
  }
  function results(result: ExportResult) {
    const container = el("results");
    container.replaceChildren();
    const docs = result.report.documents;
    const pages = docs.reduce((n, doc) => n + doc.pages, 0);
    const state =
      result.status === "cancelled"
        ? t("status.cancelled")
        : result.status === "completed_with_warnings"
          ? t("status.finished_warnings")
          : t("result.state.complete");
    status(
      plural("result.status_summary", result.total_documents, {
        state,
        done: formatNumber(docs.length),
        total: formatNumber(result.total_documents),
        pages: formatNumber(pages),
      }),
    );
    const engine = document.createElement("p");
    engine.className = "pane-note";
    engine.textContent = t("result.rendered_with", {
      engine: result.report.engine,
    });
    container.append(engine);
    docs.forEach((doc, index) => {
      const row = document.createElement("div");
      row.className = "result";
      const text = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = doc.file;
      name.dir = "auto";
      const detail = document.createElement("span");
      detail.textContent = t("result.document_detail", {
        pages: plural("result.pages", doc.pages),
        figures: plural("result.figures", doc.figures),
      });
      text.append(name, detail);
      if (doc.font_resolution?.length) {
        const fonts = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = t("font.used");
        fonts.append(summary);
        for (const resolution of doc.font_resolution) {
          const line = document.createElement("p");
          line.className = "pane-note";
          line.textContent = t("font.resolution", {
            role: t(`font.role.${resolution.role}`),
            requested: resolution.requested,
            used: resolution.used.join(", ") || t("font.unverified"),
          });
          fonts.append(line);
        }
        const scope = document.createElement("p");
        scope.className = "pane-note";
        scope.textContent = t("font.resolution_scope");
        fonts.append(scope);
        text.append(fonts);
      }
      const warnings = [
        ...doc.warnings,
        ...doc.missing_images.map((x) =>
          t("result.missing_image", { file: x }),
        ),
        ...doc.diagram_errors.map((x) =>
          t("result.diagram_failed", { detail: x }),
        ),
      ];
      if (warnings.length) {
        const disclosure = document.createElement("details");
        disclosure.className = "warning-disclosure";
        const summary = document.createElement("summary");
        summary.className = "warning-toggle";
        summary.innerHTML = warningIcon;
        summary.setAttribute("aria-label", t("warning.show"));
        summary.title = t("warning.show");
        disclosure.append(summary, ...warningParagraphs(warnings));
        text.append(disclosure);
      }
      const open = document.createElement("button");
      const action = transport.web
        ? t("result.download_pdf")
        : t("result.open_pdf");
      open.textContent = action;
      open.setAttribute(
        "aria-label",
        t("result.open_for", { action, file: doc.file }),
      );
      open.onclick = () => transport.open(index).catch(error);
      row.append(text, open);
      container.append(row);
    });
    if (transport.web) {
      const report = document.createElement("button");
      report.textContent = t("result.download_report");
      report.onclick = () => transport.web!.downloadReport().catch(error);
      container.append(report);
    }
    el("progress").hidden = true;
    el("view-results").hidden = false;
    el("view-results").textContent = plural(
      "action.view_results_count",
      docs.length,
    );
    el<HTMLDialogElement>("results-dialog").querySelector(
      ".dialog-scroll",
    )!.scrollTop = 0;
  }
  let lastProgressAnnouncement = 0;
  function event(event: WorkerEvent) {
    if (event.event === "ready" && !transport.web && !output) {
      if (event.output_path) {
        output = event.output_path;
        el("output-path").textContent = output;
        el("output-path").title = output;
        el("output-path").dir = "auto";
        el<HTMLInputElement>("output-entry").value = output;
        controls();
      } else if (event.output_error) error(event.output_error);
    }
    const eventMessage = () =>
      event.message_id
        ? tOr(event.message_id, event.message ?? "")
        : localizeLiteral(event.message ?? "");
    const eventHelp = () =>
      event.help_id
        ? tOr(event.help_id, event.help ?? "")
        : localizeLiteral(event.help ?? t("help.retry"));
    const actionableEventMessage = () =>
      t("error.with_help", { message: eventMessage(), help: eventHelp() });
    if (event.schema_version !== 1) {
      error(t("error.worker_version"));
      return;
    }
    if (event.event === "connection") {
      connected = !!event.connected;
      if (connected) {
        el("error").hidden = true;
        previewState(t("preview.sample_only"));
      }
      if (!connected) {
        busy = false;
        stopExportTime();
        source = "";
        el<HTMLInputElement>("upload-file").value = "";
        el<HTMLSelectElement>("server-mount").replaceChildren();
        el("mount-controls").hidden = true;
        el("source-path").textContent = t("http.source_type");
        el("results").replaceChildren();
        el("view-results").hidden = true;
        el("progress").hidden = true;
      }
    }
    if (event.event === "job_state") {
      busy = !!event.active;
      status(event.message ? eventMessage() : t("status.export_progress"));
    }
    if (event.event === "preview" || event.event === "preview_error") {
      if (event.id !== `preview-${previewRevision}`) return;
      if (event.event === "preview_error") {
        previewState(
          t("status.preview_unavailable", {
            message: actionableEventMessage(),
          }),
        );
      } else if (event.preview) {
        const result = event.preview;
        const image = el<HTMLImageElement>("preview-image");
        image.src = `data:image/png;base64,${result.image}`;
        image.alt = t("preview.image_alt_visual", { file: result.file });
        image.width = result.width;
        image.height = result.height;
        image.hidden = false;
        el("document-preview").dataset.stale = "false";
        el("preview-caption").textContent = result.pages
          ? t("preview.caption_page", {
              file: result.file,
              pages: formatNumber(result.pages),
            })
          : t("preview.caption_first_page", { file: result.file });
        el("preview-caption").dir = "auto";
        previewDirty = false;
        previewState(t("status.preview_ready"));
        const warningPanel = el("preview-warnings");
        warningPanel.replaceChildren(...warningParagraphs(result.warnings));
        el("preview-warning-toggle").hidden = !result.warnings.length;
      }
      return;
    }
    if (event.event === "catalog" && event.catalog) {
      loadCatalog(event.catalog);
      status(transport.web ? t("status.ready_web") : t("status.ready_desktop"));
    }
    if (event.event === "saved" && event.catalog) {
      loadCatalog(event.catalog, event.name);
      el<HTMLDialogElement>("save-dialog").close();
      status(
        t("theme.saved", {
          name: event.name ?? "",
          path: event.path ? t("theme.path_suffix", { path: event.path }) : "",
        }),
      );
    }
    if (event.event === "overwritten" && event.catalog) {
      loadCatalog(event.catalog, event.name);
      status(
        t("theme.overwritten", {
          name: event.name ?? "",
          path: event.path ? t("theme.path_suffix", { path: event.path }) : "",
        }),
      );
    }
    if (event.event === "deleted" && event.catalog) {
      el<HTMLDialogElement>("delete-dialog").close();
      selected = undefined;
      loadCatalog(event.catalog);
      status(
        t("theme.deleted", {
          name: event.name ?? "",
          path: event.path ? t("theme.path_suffix", { path: event.path }) : "",
        }),
      );
      el("choose-theme").focus();
    }
    if (event.event === "progress" && !cancelling) {
      const progress = el<HTMLProgressElement>("progress");
      progress.hidden = false;
      progress.max = event.total || 1;
      if ((event.total || 1) === 1 && !event.done)
        progress.removeAttribute("value");
      else progress.value = event.done || 0;
      activeFile = event.file ?? "";
      if (
        exportStarted &&
        event.done &&
        event.total &&
        event.done < event.total
      ) {
        estimatedSeconds =
          (((performance.now() - exportStarted) / 1000) * event.total) /
          event.done;
      }
      const now = performance.now();
      if (
        lastProgressAnnouncement === 0 ||
        now - lastProgressAnnouncement >= 1000 ||
        event.done === event.total
      ) {
        status(
          t("status.exporting_file", {
            file: event.file ?? "",
            done: formatNumber(event.done ?? 0),
            total: formatNumber(event.total ?? 0),
          }),
        );
        lastProgressAnnouncement = now;
      }
    }
    if (event.event === "activity" && busy && !cancelling) {
      const phase = event.phase ?? "other";
      const stage = tOr(`export.phase.${phase}`, t("export.phase.other"));
      status(
        activeFile ? t("export.stage", { stage, file: activeFile }) : stage,
      );
    }
    if (event.event === "result" && event.result) {
      stopExportTime(event.result);
      busy = false;
      results(event.result);
      if (previewDirty) schedulePreview();
    }
    if (event.event === "error") {
      if (event.id === "export") {
        busy = false;
        stopExportTime();
        el("progress").hidden = true;
        status(
          event.code === "response_too_large"
            ? t("status.report_large")
            : t("status.export_stopped"),
        );
        if (previewDirty) schedulePreview();
      }
      if (event.id === "save_copy")
        el("copy-error").textContent = event.message
          ? actionableEventMessage()
          : t("theme.save_failed");
      else if (event.id === "overwrite") {
        el("theme-error").textContent = event.message
          ? actionableEventMessage()
          : t("theme.overwrite_failed");
        el("theme-error").hidden = false;
      } else if (event.id === "delete")
        el("delete-error").textContent = event.message
          ? actionableEventMessage()
          : t("theme.delete_failed");
      else error(actionableEventMessage());
    }
    controls();
  }
  for (const kind of ["file", "folder", "output"] as const) {
    el(`choose-${kind}`).onclick = async () => {
      try {
        const path = await transport.choose(kind);
        if (!path) return;
        if (kind === "output") output = path;
        else source = path;
        const label = el(kind === "output" ? "output-path" : "source-path");
        label.textContent = path;
        label.title = path;
        label.dir = "auto";
        el<HTMLInputElement>(
          kind === "output" ? "output-entry" : "source-entry",
        ).value = path;
        el("error").hidden = true;
        if (kind !== "output") {
          schedulePreview();
          await send("catalog");
        }
        controls();
      } catch (e) {
        error(e);
      } finally {
        if (!root.querySelector("dialog[open]")) el(`choose-${kind}`).focus();
      }
    };
  }
  for (const kind of ["source", "output"] as const) {
    const apply = async () => {
      try {
        const path = await transport.usePath(
          kind,
          el<HTMLInputElement>(`${kind}-entry`).value,
        );
        if (kind === "source") source = path;
        else output = path;
        el(`${kind}-path`).textContent = path;
        el(`${kind}-path`).title = path;
        el(`${kind}-path`).dir = "auto";
        el<HTMLInputElement>(`${kind}-entry`).value = path;
        el("error").hidden = true;
        if (kind === "source") {
          schedulePreview();
          await send("catalog");
        }
        controls();
      } catch (e) {
        error(e);
      }
    };
    el(`use-${kind}`).onclick = apply;
    el<HTMLInputElement>(`${kind}-entry`).onkeydown = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        void apply();
      }
    };
  }
  for (const button of root.querySelectorAll<HTMLButtonElement>(
    "[data-filter]",
  ))
    button.onclick = () => {
      filter = button.dataset.filter!;
      for (const b of root.querySelectorAll("[data-filter]"))
        b.setAttribute("aria-pressed", String(b === button));
      themes();
    };
  el("choose-theme").onclick = () => {
    el<HTMLDialogElement>("theme-dialog").showModal();
    root
      .querySelector<HTMLElement>('[data-theme][aria-pressed="true"]')
      ?.focus();
  };
  el("dismiss-themes").onclick = () =>
    el<HTMLDialogElement>("theme-dialog").close();
  el("view-results").onclick = () =>
    el<HTMLDialogElement>("results-dialog").showModal();
  el("dismiss-results").onclick = () =>
    el<HTMLDialogElement>("results-dialog").close();
  for (const view of ["settings", "preview"]) {
    const tab = el(`${view}-tab`);
    tab.onclick = () => {
      root.querySelector<HTMLElement>(".workspace")!.dataset.view = view;
      for (const name of ["settings", "preview"]) {
        el(`${name}-tab`).setAttribute("aria-selected", String(name === view));
        el(`${name}-tab`).tabIndex = name === view ? 0 : -1;
      }
    };
    tab.onkeydown = (event) => {
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const next =
          event.key === "Home"
            ? "settings"
            : event.key === "End"
              ? "preview"
              : view === "settings"
                ? "preview"
                : "settings";
        el(`${next}-tab`).click();
        el(`${next}-tab`).focus();
      }
    };
  }
  el("revert").onclick = () => {
    if (!selected) return;
    colors = { ...selected.colors };
    for (const [key, value] of Object.entries(colors)) {
      pickers.get(key)!.value = value;
      const hex = hexEntries.get(key)!;
      hex.value = value;
      hex.removeAttribute("aria-invalid");
    }
    changed();
  };
  function paperControls() {
    advanced.update();
    const mode = el<HTMLSelectElement>("output-mode").value;
    const paper = el<HTMLSelectElement>("preset").value;
    const suggested =
      mode === "online" ? "letter" : suggestedPaper(navigator.language);
    el<HTMLSelectElement>("preset").options[0].text = t(
      "paper.auto_suggested",
      {
        paper: suggested.toLocaleUpperCase(interfaceLocale),
      },
    );
    el("paper-label").textContent = (
      advanced.customPaper()
        ? t("paper.custom_sheet")
        : paper === "auto"
          ? suggested
          : paper
    ).toLocaleUpperCase(interfaceLocale);
    el("layout-note").textContent =
      mode === "online"
        ? t("layout.online")
        : mode === "print"
          ? t("layout.print")
          : t("layout.tiled");
    const margins = el<HTMLSelectElement>("margins").value;
    el("margin-note").textContent = advanced.read().margin_edges
      ? t("margin.exact")
      : margins === "full"
        ? t("margin.full")
        : margins === "none"
          ? t("margin.none")
          : t("margin.bottom_right");
  }
  for (const id of ["preset", "output-mode", "margins", "orientation"]) {
    el<HTMLSelectElement>(id).onchange = () => {
      paperControls();
      controls();
      schedulePreview();
    };
  }
  paperControls();
  for (const id of [
    "font",
    "toc",
    "bookmarks",
    "csv-header",
    "document-language",
  ])
    el<HTMLInputElement>(id).onchange = schedulePreview;
  el("refresh-preview").onclick = schedulePreview;
  el("refresh-fonts").onclick = async () => {
    el<HTMLButtonElement>("refresh-fonts").disabled = true;
    await send("catalog");
    controls();
  };
  if (!transport.web)
    el<HTMLSelectElement>("renderer").onchange = schedulePreview;
  el("appearance").onclick = () => {
    const dark = root.dataset.appearance !== "dark";
    root.dataset.appearance = dark ? "dark" : "light";
    el("appearance").setAttribute("aria-pressed", String(dark));
    el("appearance").textContent = dark
      ? t("app.interface.light")
      : t("app.interface.dark");
  };
  el("export").onclick = async () => {
    if (!advanced.valid()) return;
    busy = true;
    cancelling = false;
    clearTimeout(previewTimer);
    ++previewRevision;
    if (previewDirty) previewState(t("preview.after_export"));
    el("error").hidden = true;
    el("results").replaceChildren();
    el("view-results").hidden = true;
    el("view-results").textContent = t("action.view_results");
    status(t("status.starting_export"));
    lastProgressAnnouncement = 0;
    activeFile = "";
    exportStarted = performance.now();
    exportKey = JSON.stringify([source, rendererChoice(), settings()]);
    estimatedSeconds = exportDurations.get(exportKey);
    el("export-timing").hidden = false;
    const progress = el<HTMLProgressElement>("progress");
    progress.hidden = false;
    progress.removeAttribute("value");
    updateExportTime();
    clearInterval(exportTimer);
    exportTimer = setInterval(updateExportTime, 1000);
    controls();
    el("cancel").focus();
    await send("export", {
      activity: true,
      browser: rendererChoice(),
      settings: settings(),
    });
  };
  el("cancel").onclick = async () => {
    if (cancelling) return;
    cancelling = true;
    status(t("status.cancelling"));
    el("cancel").setAttribute("aria-disabled", "true");
    await send("cancel");
  };
  const openSaveDialog = () => {
    el("copy-error").textContent = "";
    el<HTMLDialogElement>("save-dialog").showModal();
    el("copy-name").focus();
  };
  el("save-copy").onclick = openSaveDialog;
  el("new-from-current").onclick = () => {
    el<HTMLDialogElement>("theme-dialog").close();
    openSaveDialog();
  };
  el("dismiss-copy").onclick = () =>
    el<HTMLDialogElement>("save-dialog").close();
  el<HTMLFormElement>("copy-form").onsubmit = async (e) => {
    e.preventDefault();
    const savedColors =
      kindOf(selected!) === "source"
        ? Object.fromEntries(
            Object.entries(colors).filter(
              ([key, value]) => value !== selected!.colors[key],
            ),
          )
        : colors;
    await send("save_copy", {
      name: el<HTMLInputElement>("copy-name").value,
      theme: selected!.name,
      colors: savedColors,
    });
  };
  el("overwrite").onclick = async () => {
    el("theme-error").hidden = true;
    await send("overwrite", { name: selected!.name, colors });
  };
  el("delete-theme").onclick = () => {
    if (!selected) return;
    el("delete-title").textContent = t("theme.delete_named", {
      name: selected.name,
    });
    el("delete-path").textContent =
      selected.path ?? selected.file ?? selected.name;
    el("delete-path").dir = "auto";
    el("delete-error").textContent = "";
    el<HTMLDialogElement>("delete-dialog").showModal();
    // Cancel is the safe default; Delete is one Tab away.
    el("dismiss-delete").focus();
  };
  el("dismiss-delete").onclick = () =>
    el<HTMLDialogElement>("delete-dialog").close();
  el("confirm-delete").onclick = async () => {
    el("delete-error").textContent = "";
    await send("delete", { name: selected!.name });
  };
  if (transport.web) {
    output = "browser-downloads";
    mountWebControls(
      root,
      transport.web,
      (name) => {
        source = name;
        el("source-path").textContent = name;
        el("source-path").dir = "auto";
        el("error").hidden = true;
        controls();
      },
      error,
    );
    status(t("status.connect_server"));
  }
  controls();
  transport
    .connect(event, (message) => {
      connected = false;
      busy = false;
      stopExportTime();
      el("progress").hidden = true;
      clearTimeout(previewTimer);
      ++previewRevision;
      if (!transport.web) previewState(message);
      status(message);
      error(message);
      controls();
    })
    .then(async () => {
      if (!transport.web) {
        connected = true;
        await send("catalog");
      }
      controls();
    })
    .catch((e) => {
      status(
        transport.web
          ? t("status.server_unavailable")
          : t("status.worker_unavailable"),
      );
      error(e);
    });
}
