// Omit empty controls so document settings and presets can supply their defaults.
import { html, t } from "./i18n";

const fields = [
  [
    "page_width",
    t("advanced.page_width"),
    0.1,
    "any",
    t("advanced.from_preset"),
    "all",
  ],
  [
    "page_height",
    t("advanced.page_height"),
    0.1,
    "any",
    t("advanced.from_preset"),
    "all",
  ],
  ["inline_fit", t("advanced.inline_fit"), 0.01, "any", "0.8", "whole"],
  ["plate_fit", t("advanced.plate_fit"), 0.01, "any", "0.85", "online"],
  [
    "max_custom_width",
    t("advanced.max_custom_width"),
    0,
    "any",
    t("advanced.custom_cap_placeholder"),
    "online",
  ],
  ["caption_px", t("advanced.caption_px"), 0, "any", "52", "all"],
  ["tile_overlap", t("advanced.tile_overlap"), 0, "any", "10", "tiled"],
  ["body_size_pt", t("advanced.body_size_pt"), 0.1, "any", "10.2", "all"],
  ["mermaid_font_px", t("advanced.mermaid_font_px"), 1, "1", "16", "all"],
  ["mermaid_wrap_px", t("advanced.mermaid_wrap_px"), 1, "1", "260", "all"],
  ["image_dpi", t("advanced.image_dpi"), 1, "1", "300", "all"],
] as const;
const edges = [
  ["top", t("edge.top")],
  ["right", t("edge.right")],
  ["bottom", t("edge.bottom")],
  ["left", t("edge.left")],
] as const;

export const advancedMarkup = `
  <details id="advanced-layout">
    <summary>${html(t("advanced.title"))}</summary>
    <p id="advanced-inheritance" class="pane-note">${html(t("advanced.inheritance"))}</p>
    <p class="pane-note">${html(t("advanced.sheet_note"))}</p>
    <div class="advanced-grid">${fields
      .map(
        ([key, label, min, step, placeholder]) => `
      <label class="setting">${html(label)}<input id="advanced-${key}" type="number" min="${min}" step="${step}" ${key.endsWith("_fit") ? 'max="1"' : key === "tile_overlap" ? 'max="50"' : ""} placeholder="${html(placeholder)}"></label>`,
      )
      .join("")}
    </div>
    <p class="pane-note">${html(t("advanced.threshold_note"))}</p>
    <label class="custom-margins"><input id="exact-margins" type="checkbox"> ${html(t("advanced.exact_margins"))}</label>
    <fieldset id="exact-margin-fields" disabled><legend class="sr-only">${html(t("advanced.exact_legend"))}</legend><div class="advanced-grid">${edges
      .map(
        ([key, label]) => `
      <label class="setting">${html(t("advanced.margin_label", { edge: label }))}<input id="margin-${key}" type="number" min="0" step="any" required></label>`,
      )
      .join("")}</div></fieldset>
    <p class="pane-note">${html(t("advanced.margin_note"))}</p>
    <button id="reset-layout" type="button">${html(t("advanced.reset"))}</button>
    <p id="advanced-status" class="pane-note" aria-live="polite"></p>
  </details>`;

export function advancedControls(
  root: HTMLElement,
  changed: () => void,
  remote = false,
) {
  const input = (id: string) => root.querySelector<HTMLInputElement>(`#${id}`)!;
  const inherited = remote
    ? t("advanced.inherited_remote")
    : t("advanced.inherited_local");
  if (remote) {
    const dpi = input("advanced-image_dpi");
    dpi.min = "36";
    dpi.max = "600";
    dpi.placeholder = "150";
    root.querySelector("#advanced-inheritance")!.textContent = t(
      "advanced.remote_inheritance",
    );
  }
  const exact = input("exact-margins");
  const marginFields = root.querySelector<HTMLFieldSetElement>(
    "#exact-margin-fields",
  )!;
  const values = () =>
    fields.filter(([key]) => input(`advanced-${key}`).value !== "");
  const read = (): Record<string, unknown> => {
    const result: Record<string, unknown> = {};
    for (const [key] of values()) {
      const field = input(`advanced-${key}`);
      if (!field.disabled) result[key] = field.valueAsNumber;
    }
    if (exact.checked)
      result.margin_edges = edges.map(
        ([key]) => input(`margin-${key}`).valueAsNumber,
      );
    return result;
  };
  const valid = () =>
    Array.from(
      root.querySelectorAll<HTMLInputElement>("#advanced-layout input"),
    ).every((field) => field.checkValidity());
  const update = () => {
    const mode = input("output-mode").value;
    for (const [key, , , , , modes] of fields)
      input(`advanced-${key}`).disabled =
        (modes === "online" && mode !== "online") ||
        (modes === "whole" && mode === "tiled") ||
        (modes === "tiled" && mode !== "tiled");
    marginFields.disabled = !exact.checked;
    root.querySelector("#advanced-status")!.textContent = !valid()
      ? t("advanced.invalid")
      : values().length || exact.checked
        ? t("advanced.overrides", { inherited })
        : t("advanced.using", { inherited });
  };
  exact.onchange = () => {
    if (exact.checked && !input("margin-top").value) {
      const mode = input("margins").value;
      const defaults =
        mode === "none"
          ? [0, 0, 0, 0]
          : mode === "bottom-right"
            ? [0, 15, 18, 0]
            : [15, 15, 18, 15];
      edges.forEach(([key], i) => {
        input(`margin-${key}`).value = String(defaults[i]);
      });
    }
    changed();
  };
  for (const field of root.querySelectorAll<HTMLInputElement>(
    '#advanced-layout input[type="number"]',
  ))
    field.oninput = changed;
  root.querySelector<HTMLButtonElement>("#reset-layout")!.onclick = () => {
    for (const [key] of fields) input(`advanced-${key}`).value = "";
    exact.checked = false;
    for (const [key] of edges) input(`margin-${key}`).value = "";
    changed();
  };
  return {
    read,
    valid,
    update,
    customPaper: () =>
      !!(
        input("advanced-page_width").value ||
        input("advanced-page_height").value
      ),
  };
}
