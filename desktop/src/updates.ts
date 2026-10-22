import { html, localizeLiteral, number, t } from "./i18n";

export interface UpdatePreferences {
  enabled: boolean;
  channel: "stable" | "prerelease";
}
export interface UpdateStatus {
  preferences: UpdatePreferences;
  version: string;
  configured: boolean;
  installable: boolean;
  distribution: string;
  phase:
    | "idle"
    | "available"
    | "downloading"
    | "verified"
    | "installing"
    | "installed";
  pending_version: string | null;
  guidance: string;
  warning: string | null;
}
// The status after a successful installation, and whether native code restarts
// PlateFold now.
export interface InstallResult extends UpdateStatus {
  restarting: boolean;
}
export interface UpdateRelease {
  version: string;
  notes: string;
  // Null when a package manager installed this copy: the user downloads the
  // next package from the release page instead.
  artifact: {
    url: string;
    sha256: string;
    size: number;
    signature: string;
  } | null;
  release_page: string;
}
export interface VerifiedDownload {
  version: string;
  size: number;
  sha256: string;
}
export interface UpdateTransport {
  status(): Promise<UpdateStatus>;
  preferences(preferences: UpdatePreferences): Promise<UpdateStatus>;
  check(): Promise<UpdateRelease | null>;
  download(): Promise<VerifiedDownload>;
  cancel(): Promise<void>;
  install(restart: boolean): Promise<InstallResult>;
  restart(): Promise<void>;
  // Calls listener whenever a close or quit waits for an installation.
  onClosePending(listener: () => void): Promise<void>;
}

// Mounted only by the native transport. Feed text is always plain text, never HTML.
export function mountUpdates(
  root: HTMLElement,
  transport: UpdateTransport,
  onActivity: () => void = () => {},
  announce: (text: string) => void = () => {},
) {
  const button = document.createElement("button");
  button.className = "text-button";
  button.textContent = t("updates.title");
  button.setAttribute("aria-haspopup", "dialog");
  root.querySelector(".build-label")!.before(button);
  const dialog = document.createElement("dialog");
  dialog.id = "updates-dialog";
  dialog.setAttribute("aria-labelledby", "updates-title");
  dialog.innerHTML = `<div class="dialog-heading"><h2 id="updates-title">${html(t("updates.title"))}</h2><button id="updates-close">${html(t("updates.close"))}</button></div>
    <div class="dialog-scroll" tabindex="0"><p id="updates-version"></p>
    <div class="checks"><label><input id="updates-enabled" type="checkbox"> ${html(t("updates.allow_checks"))}</label></div>
    <label class="setting">${html(t("updates.channel"))}<select id="updates-channel"><option value="stable">${html(t("updates.stable"))}</option><option value="prerelease">${html(t("updates.prerelease"))}</option></select></label>
    <p class="pane-note">${html(t("updates.policy"))}</p>
    <p id="updates-guidance" class="pane-note"></p>
    <button id="updates-check">${html(t("updates.check"))}</button>
    <p id="updates-message" role="status" aria-live="polite"></p>
    <section id="updates-release" hidden><h3 id="updates-release-version"></h3><p id="updates-notes"></p>
    <p class="pane-note" id="updates-signature-note">${html(t("updates.signature_note"))}</p>
    <label class="setting" id="updates-url-row">${html(t("updates.artifact_url"))}<input id="updates-url" readonly></label>
    <label class="setting" id="updates-sha256-row">${html(t("updates.expected_sha"))}<input id="updates-sha256" readonly></label>
    <label class="setting" id="updates-page-row" hidden>${html(t("updates.release_page"))}<input id="updates-page" readonly></label>
    <div class="button-row" id="updates-download-actions"><button id="updates-download">${html(t("updates.download"))}</button><button id="updates-cancel">${html(t("updates.cancel"))}</button></div>
    <div id="updates-install-actions" hidden><p class="pane-note" id="updates-verified"></p>
      <div class="checks"><label><input id="updates-install-approved" type="checkbox"> ${html(t("updates.approve_install"))}</label></div>
      <div class="checks"><label><input id="updates-restart-approved" type="checkbox"> ${html(t("updates.approve_restart"))}</label></div>
      <p class="pane-note">${html(t("updates.install_note"))}</p>
      <div class="button-row"><button id="updates-install">${html(t("updates.install"))}</button><button id="updates-cancel-verified">${html(t("updates.cancel"))}</button></div>
    </div>
    </section>
    <button id="updates-restart" hidden>${html(t("updates.restart_now"))}</button></div>`;
  root.append(dialog);
  const el = <T extends HTMLElement = HTMLElement>(id: string) =>
    dialog.querySelector<T>(`#updates-${id}`)!;
  const enabled = el<HTMLInputElement>("enabled");
  const channel = el<HTMLSelectElement>("channel");
  const check = el<HTMLButtonElement>("check");
  const download = el<HTMLButtonElement>("download");
  const cancelDownload = el<HTMLButtonElement>("cancel");
  const cancelVerified = el<HTMLButtonElement>("cancel-verified");
  const install = el<HTMLButtonElement>("install");
  const approveInstall = el<HTMLInputElement>("install-approved");
  const approveRestart = el<HTMLInputElement>("restart-approved");
  let status: UpdateStatus | undefined;
  let release: UpdateRelease | undefined;
  let exporting = false;
  let saving = false;
  let checking = false;
  let operation: "download" | "install" | undefined;
  let verified = false;
  let revision = 0;
  const message = (text: string) => (el("message").textContent = text);
  const blockingExport = () => operation !== undefined;
  function controls() {
    enabled.disabled = !status || saving || !!operation;
    channel.disabled =
      !status || saving || !!operation || !status.preferences.enabled;
    // A check resets native state, which would discard an installed update's restart.
    check.disabled =
      !status?.configured ||
      !status.preferences.enabled ||
      status.phase === "installed" ||
      exporting ||
      checking ||
      saving ||
      !!operation;
    download.disabled =
      !release?.artifact ||
      !status?.installable ||
      exporting ||
      !!operation ||
      verified;
    el<HTMLButtonElement>("restart").disabled =
      exporting || !!operation || saving;
    cancelDownload.disabled = operation === "install";
    cancelVerified.disabled = !!operation;
    approveInstall.disabled = exporting || !!operation || !verified;
    approveRestart.disabled = exporting || !!operation || !verified;
    install.disabled =
      exporting || !!operation || !verified || !approveInstall.checked;
    onActivity();
  }
  function render(next: UpdateStatus) {
    status = next;
    enabled.checked = next.preferences.enabled;
    channel.value = next.preferences.channel;
    el("version").textContent = t("updates.installed", {
      version: next.version,
      distribution: next.distribution,
    });
    el("guidance").textContent = next.guidance;
    el<HTMLButtonElement>("restart").hidden = next.phase !== "installed";
    if (next.phase === "installed") {
      verified = false;
      el("install-actions").hidden = true;
      el("download-actions").hidden = true;
    }
    message(statusMessage(next));
    controls();
  }
  // An installed update waits for its restart whatever the settings, and Check is
  // disabled meanwhile, so it keeps the restart-later message ahead of any warning.
  function statusMessage(next: UpdateStatus) {
    if (next.phase === "installed") {
      const installed = t("updates.installed_later");
      return next.warning ? `${installed} ${next.warning}` : installed;
    }
    return (
      next.warning ??
      (!next.preferences.enabled
        ? t("updates.disabled")
        : !next.configured
          ? t("updates.not_configured")
          : t("updates.ready"))
    );
  }
  function clearRelease() {
    release = undefined;
    verified = false;
    approveInstall.checked = false;
    approveRestart.checked = false;
    el("release").hidden = true;
    el("install-actions").hidden = true;
    el("download-actions").hidden = false;
  }
  button.onclick = async () => {
    dialog.showModal();
    if (status) return;
    controls();
    try {
      render(await transport.status());
    } catch {
      message(t("updates.load_failed"));
    }
  };
  el("close").onclick = () => dialog.close();
  async function save() {
    if (!status || saving || operation) return;
    revision++;
    clearRelease();
    saving = true;
    controls();
    try {
      render(
        await transport.preferences({
          enabled: enabled.checked,
          channel: channel.value as UpdatePreferences["channel"],
        }),
      );
    } catch {
      enabled.checked = status.preferences.enabled;
      channel.value = status.preferences.channel;
      message(t("updates.save_failed"));
    } finally {
      saving = false;
      controls();
    }
  }
  enabled.onchange = save;
  channel.onchange = save;
  check.onclick = async () => {
    if (check.disabled) return;
    const started = revision;
    checking = true;
    clearRelease();
    message(t("updates.checking"));
    controls();
    try {
      const available = await transport.check();
      if (started !== revision || exporting) return;
      if (available) {
        release = available;
        el("release-version").textContent = t("updates.available", {
          version: available.version,
        });
        el("notes").textContent = available.notes;
        const artifact = available.artifact;
        el<HTMLInputElement>("url").value = artifact?.url ?? "";
        el<HTMLInputElement>("sha256").value = artifact?.sha256 ?? "";
        el<HTMLInputElement>("page").value = available.release_page;
        for (const row of ["signature-note", "url-row", "sha256-row"])
          el(row).hidden = !artifact;
        el("page-row").hidden = !!artifact;
        el("release").hidden = false;
        el("download-actions").hidden = !status?.installable || !artifact;
        message(
          status?.installable && artifact
            ? t("updates.new_installable")
            : artifact
              ? t("updates.new_guidance")
              : t("updates.new_package"),
        );
      } else {
        message(t("updates.current"));
      }
    } catch (error) {
      if (started === revision && !exporting) message(localizeLiteral(error));
    } finally {
      checking = false;
      controls();
    }
  };
  download.onclick = async () => {
    if (download.disabled) return;
    operation = "download";
    message(t("updates.downloading"));
    controls();
    try {
      const result = await transport.download();
      verified = true;
      el("download-actions").hidden = true;
      el("install-actions").hidden = false;
      el("verified").textContent = t("updates.verified", {
        version: result.version,
        bytes: number(result.size),
        sha256: result.sha256,
      });
      message(t("updates.install_approval"));
    } catch (error) {
      message(localizeLiteral(error));
    } finally {
      operation = undefined;
      controls();
    }
  };
  async function cancel() {
    const wasDownloading = operation === "download";
    try {
      await transport.cancel();
      clearRelease();
      message(
        wasDownloading
          ? t("updates.download_cancelled")
          : t("updates.cancelled"),
      );
    } catch (error) {
      message(localizeLiteral(error));
    }
    controls();
  }
  cancelDownload.onclick = cancel;
  cancelVerified.onclick = cancel;
  approveInstall.onchange = controls;
  approveRestart.onchange = controls;
  // Native status decides what a failed installation leaves: whether the
  // verified download can be retried, and the warning that says how to recover.
  async function afterInstallFailure(failure: string) {
    let next: UpdateStatus;
    try {
      next = await transport.status();
    } catch {
      // Without native status, offer no retry the native side may refuse.
      clearRelease();
      message(failure);
      return;
    }
    if (next.phase !== "verified") clearRelease();
    render(next);
    if (!next.warning) message(failure);
  }
  install.onclick = async () => {
    if (install.disabled) return;
    operation = "install";
    message(t("updates.installing"));
    controls();
    try {
      const next = await transport.install(approveRestart.checked);
      render(next);
      // A native warning after success names a folder the installation could not delete.
      // Native code decides whether it restarts now: a close during installation or such
      // a folder defers a requested restart.
      const installed = next.restarting
        ? t("updates.installed_restarting")
        : t("updates.installed_later");
      message(next.warning ? `${installed} ${next.warning}` : installed);
    } catch (error) {
      await afterInstallFailure(localizeLiteral(error));
    } finally {
      operation = undefined;
      controls();
    }
  };
  el("restart").onclick = async () => {
    if (el<HTMLButtonElement>("restart").disabled) return;
    operation = "install";
    message(t("updates.restarting"));
    controls();
    try {
      await transport.restart();
    } catch (error) {
      message(localizeLiteral(error));
    } finally {
      operation = undefined;
      controls();
    }
  };
  // Show a deferred close in the dialog and the main window's live region.
  // Native code resumes the close after the result or the permitted timeout.
  void transport.onClosePending(() => {
    const text = t("updates.close_pending");
    message(text);
    announce(text);
  });
  controls();
  return {
    setBusy(busy: boolean) {
      if (exporting === busy) return;
      if (busy && !exporting) {
        revision++;
        if (!verified && status?.phase !== "installed") clearRelease();
        if (checking) message(t("updates.export_started"));
      }
      exporting = busy;
      controls();
    },
    blocksExport: blockingExport,
  };
}
