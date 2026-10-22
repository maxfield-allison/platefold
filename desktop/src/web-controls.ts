import type { HttpTransport } from "./http";
import { html, localizeLiteral, number, plural, t } from "./i18n";

export function mountWebControls(
  root: HTMLElement,
  web: HttpTransport,
  source: (name: string) => void,
  error: (message: unknown) => void,
) {
  root.dataset.transport = "web";
  const el = <T extends HTMLElement = HTMLElement>(id: string) =>
    root.querySelector<T>(`#${id}`)!;
  const section = document.createElement("section");
  section.className = "web-connection";
  section.setAttribute("aria-label", t("web.server_connection"));
  section.innerHTML = `<form id="connect-form"><label>${html(t("web.api_credential"))} <input id="api-credential" type="password" autocomplete="off" spellcheck="false" required maxlength="512"></label><button id="connect-server">${html(t("web.connect"))}</button><button id="disconnect-server" type="button" disabled>${html(t("web.disconnect"))}</button></form><p id="connection-note" role="status">${html(t("web.connection_intro"))}</p><p id="server-limits" class="pane-note"></p>`;
  root.querySelector("main")!.prepend(section);
  for (const id of [
    "choose-file",
    "choose-folder",
    "choose-output",
    "refresh-preview",
    "save-copy",
    "overwrite",
    "delete-theme",
    "new-from-current",
  ])
    el(id).hidden = true;
  for (const details of root.querySelectorAll<HTMLElement>(".path-details"))
    details.hidden = true;
  root.querySelector(".build-label")!.textContent = t("app.build.browser");
  el("source-path").textContent = t("web.source_prompt");
  el("output-path").textContent = t("web.output_help");
  el("preview-status").textContent = t("web.preview_status");
  el("preview-note").textContent = t("web.preview_note");
  el("theme-note").textContent = t("web.theme_note");
  const controls = document.createElement("fieldset");
  controls.id = "web-sources";
  controls.disabled = true;
  controls.innerHTML = `<legend class="sr-only">${html(t("web.sources"))}</legend><label class="web-upload">${html(t("web.document_zip"))} <input id="upload-file" type="file" accept=".doc,.docx,.odt,.rtf,.xlsx,.md,.csv,.tsv,.zip"></label><div id="mount-controls" hidden><label>${html(t("web.server_mount"))} <select id="server-mount"></select></label><label>${html(t("web.relative_path"))} <input id="mount-path" placeholder="${html(t("web.mount_placeholder"))}"></label><button id="use-mount" type="button">${html(t("web.use_mount"))}</button></div>`;
  root.querySelector(".source-choice")!.append(controls);
  el<HTMLInputElement>("upload-file").onchange = () => {
    const input = el<HTMLInputElement>("upload-file");
    try {
      if (input.files?.[0]) source(web.selectFile(input.files[0]));
    } catch (e) {
      input.value = "";
      error(e);
    }
  };
  el("use-mount").onclick = () => {
    try {
      source(
        web.selectMount(
          el<HTMLSelectElement>("server-mount").value,
          el<HTMLInputElement>("mount-path").value,
        ),
      );
      el<HTMLInputElement>("upload-file").value = "";
    } catch (e) {
      error(e);
    }
  };
  el<HTMLFormElement>("connect-form").onsubmit = async (event) => {
    event.preventDefault();
    const input = el<HTMLInputElement>("api-credential");
    const token = input.value;
    input.value = "";
    el<HTMLButtonElement>("connect-server").disabled = true;
    try {
      const caps = await web.authenticate(token);
      const select = el<HTMLSelectElement>("server-mount");
      select.replaceChildren();
      for (const mount of caps.mounts) {
        const option = document.createElement("option");
        option.value = mount;
        option.textContent = mount;
        select.append(option);
      }
      el("mount-controls").hidden = !caps.mounts.length;
      const minutes = Math.round(caps.limits.retention_seconds / 60);
      el("server-limits").textContent = plural("web.limit_summary", minutes, {
        mebibytes: number(Math.floor(caps.limits.upload_bytes / 1048576)),
        minutes: number(minutes),
        readiness:
          caps.pdf_engine.discovered && caps.storage_ready
            ? t("web.server_ready")
            : t("web.server_not_ready"),
      });
      el("connection-note").textContent = t("web.connected");
      el<HTMLButtonElement>("disconnect-server").disabled = false;
      await web.resume();
    } catch (e) {
      el("connection-note").textContent = localizeLiteral(e);
    } finally {
      el<HTMLButtonElement>("connect-server").disabled = false;
    }
  };
  el("disconnect-server").onclick = () => {
    web.disconnect();
    el<HTMLButtonElement>("disconnect-server").disabled = true;
    el("connection-note").textContent = t("web.disconnected_clear");
  };
}
