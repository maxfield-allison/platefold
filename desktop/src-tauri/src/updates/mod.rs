//! Signed discovery plus an explicit, distribution-aware Tauri installation adapter.
mod binding;
mod manifest;
mod policy;
#[cfg(test)]
mod tests;

use policy::Policy;

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use futures_util::future::{AbortHandle, Abortable};
use minisign_verify::{PublicKey, Signature};
use reqwest::{blocking::Client, redirect::Policy as RedirectPolicy, Url};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::{Emitter, Manager, State};
use tauri_plugin_updater::{CleanupReport, CommitGate, Update, UpdaterExt};

const MAX_FEED: u64 = 64 * 1024;
const MAX_ARTIFACT: u64 = 512 * 1024 * 1024;
/// Time from a deferred close before trying to abandon further replacement.
/// A replacement or Windows installer launch already inside the gate must
/// finish before the close can proceed.
const DEFERRED_CLOSE_LIMIT: Duration = Duration::from_secs(60);
const DEFERRED_CLOSE_POLL: Duration = Duration::from_millis(250);
// Every release, and the packages it offers, is published on its tag's page.
const RELEASES: &str = "https://github.com/maxfield-allison/platefold/releases/tag/";

fn release_page(version: &str) -> String {
    format!("{RELEASES}v{version}")
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Channel {
    #[default]
    Stable,
    Prerelease,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Preferences {
    enabled: bool,
    channel: Channel,
}

impl Policy {
    fn endpoint(&self, channel: Channel) -> Result<&str, String> {
        let endpoint = match channel {
            Channel::Stable => &self.stable,
            Channel::Prerelease => &self.prerelease,
        }
        .as_deref()
        .ok_or("Update checks are not configured for this build")?;
        validate_keys(&self.public_keys, "metadata")?;
        validate_keys(&self.artifact_public_keys, "artifact")?;
        https_url(endpoint)?;
        if !self.artifact_url_prefix.ends_with('/') || https_url(&self.artifact_url_prefix).is_err()
        {
            return Err("The update download location is invalid".into());
        }
        Ok(endpoint)
    }
}

fn validate_keys(keys: &[String], kind: &str) -> Result<(), String> {
    if keys.is_empty() || keys.len() > 4 {
        return Err(format!("The update {kind} trust keys are not configured"));
    }
    for key in keys {
        PublicKey::from_base64(key)
            .map_err(|_| format!("The update {kind} trust key is invalid"))?;
    }
    Ok(())
}

fn https_url(value: &str) -> Result<Url, String> {
    let url = Url::parse(value).map_err(|_| "Invalid update URL")?;
    if value.len() > 2048
        || url.scheme() != "https"
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err("Updates require HTTPS URLs without credentials or fragments".into());
    }
    Ok(url)
}

// GitHub serves `github.com/<owner>/<repo>/releases/download/<tag>/<file>` as a
// 302 to its asset host, so an artifact download that refuses every redirect can
// never succeed against the published release. The installer is verified after
// download by exact size, SHA-256 and signature, so a redirect cannot change what
// is installed; this policy only bounds where the client may go. Feed metadata
// is still fetched with redirects refused.
const ARTIFACT_REDIRECT_HOSTS: &[&str] = &[
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
];
const MAX_ARTIFACT_REDIRECTS: usize = 2;

fn artifact_redirect_allowed(url: &Url) -> bool {
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none()
        && url
            .host_str()
            .is_some_and(|host| ARTIFACT_REDIRECT_HOSTS.contains(&host))
}

fn artifact_redirect_policy() -> RedirectPolicy {
    RedirectPolicy::custom(|attempt| {
        if attempt.previous().len() > MAX_ARTIFACT_REDIRECTS {
            attempt.error("too many artifact redirects")
        } else if artifact_redirect_allowed(attempt.url()) {
            attempt.follow()
        } else {
            attempt.stop()
        }
    })
}

fn read_bounded(reader: impl Read, limit: u64) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    reader
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "The update response was interrupted")?;
    if bytes.len() as u64 > limit {
        return Err("The update response is too large".into());
    }
    Ok(bytes)
}

// The caller supplies only the compiled policy's endpoint. No URL IPC argument.
fn fetch(endpoint: &str) -> Result<Vec<u8>, String> {
    let url = https_url(endpoint)?;
    fetch_with_client(Client::builder().https_only(true), url)
}

fn fetch_with_client(
    builder: reqwest::blocking::ClientBuilder,
    url: Url,
) -> Result<Vec<u8>, String> {
    let response = builder
        .redirect(RedirectPolicy::none())
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|_| "Could not initialise update checks")?
        .get(url)
        .header(reqwest::header::ACCEPT, "application/json")
        .send()
        .map_err(|_| "Could not reach the update service; try again later")?;
    if response.status() != reqwest::StatusCode::OK {
        return Err("The update service did not return a release; try again later".into());
    }
    if response
        .content_length()
        .is_some_and(|size| size > MAX_FEED)
    {
        return Err("The update response is too large".into());
    }
    read_bounded(response, MAX_FEED)
}

fn distribution(bundle: Option<tauri::utils::config::BundleType>) -> &'static str {
    use tauri::utils::config::BundleType;
    match bundle {
        Some(BundleType::Nsis) => "nsis",
        Some(BundleType::Msi) => "msi",
        Some(BundleType::AppImage) => "appimage",
        Some(BundleType::App) => "app",
        Some(BundleType::Deb) => "deb",
        Some(BundleType::Rpm) => "rpm",
        _ => "portable",
    }
}

fn installable(distribution: &str) -> bool {
    matches!(distribution, "nsis" | "msi" | "appimage" | "app")
}

const DEB_GUIDANCE: &str = "This copy was installed from a .deb package, so it can't replace itself. To update, download the new .deb from the release page and install it with your package manager, for example sudo apt install ./platefold_1.0.1_amd64.deb. Your settings and themes are kept.";

fn guidance(distribution: &str) -> &'static str {
    match distribution {
        "deb" => DEB_GUIDANCE,
        "rpm" => "Use your Linux package manager to update PlateFold.",
        "portable" => "For portable copies, verify the release checksum and signature before replacing the app. Close PlateFold first and keep the previous copy for recovery.",
        _ => "PlateFold can download, verify and install a signed update only after you approve each step.",
    }
}

const APPIMAGE_NOT_REPLACEABLE: &str = "This AppImage is in a folder your account can't change, such as the administrator-managed /opt/platefold installation. PlateFold can't replace it there. Download the new AppImage and repeat the administrator install steps in LINUX-INSTALL.md.";

const APP_NOT_REPLACEABLE: &str = "This copy of PlateFold is in a folder your account can't change, for example because another account installed it in Applications. PlateFold can't replace it there. Download the new version from the release page and drag it to Applications, or ask the person who installed it.";

/// Whether this copy can replace itself, and what to tell the user.
///
/// An AppImage or a macOS app updates by renaming itself, which needs write
/// access to what it renames. The documented Ubuntu installation keeps
/// /opt/platefold root-owned so its AppArmor exception can't be claimed by
/// other programs, and a macOS app another account installed belongs to that
/// account; in both an administrator replaces it instead. PlateFold never asks
/// for an administrator password to do it, because the copy it would hand over
/// sits where this account can still change it. Deciding before discovery
/// offers a download saves a large transfer that could never be installed.
fn install_route(distribution: &str, replaceable: bool) -> (bool, &'static str) {
    match distribution {
        "appimage" if !replaceable => (false, APPIMAGE_NOT_REPLACEABLE),
        "app" if !replaceable => (false, APP_NOT_REPLACEABLE),
        _ => (installable(distribution), guidance(distribution)),
    }
}

/// What an update renames for this executable: the AppImage's folder, or the
/// app bundle and the folder holding it.
fn renamed_paths(distribution: &str, executable: &Path) -> Vec<PathBuf> {
    match distribution {
        "appimage" => executable.parent().into_iter().map(Path::to_path_buf).collect(),
        // <folder>/PlateFold.app/Contents/MacOS/<executable>
        "app" => executable
            .ancestors()
            .nth(3)
            .filter(|bundle| bundle.extension().is_some_and(|ext| ext == "app"))
            .into_iter()
            .flat_map(|bundle| [Some(bundle), bundle.parent()])
            .flatten()
            .map(Path::to_path_buf)
            .collect(),
        _ => Vec::new(),
    }
}

#[cfg(unix)]
fn writable(path: &Path) -> bool {
    use std::os::unix::ffi::OsStrExt;
    std::ffi::CString::new(path.as_os_str().as_bytes())
        // SAFETY: access() only reads the NUL-terminated path it is given.
        .is_ok_and(|path| unsafe { libc::access(path.as_ptr(), libc::W_OK) } == 0)
}

#[cfg(not(unix))]
fn writable(_path: &Path) -> bool {
    true
}

fn current_route(distribution: &str) -> (bool, &'static str) {
    let replaceable = !matches!(distribution, "appimage" | "app")
        || tauri::utils::platform::current_exe().is_ok_and(|executable| {
            let paths = renamed_paths(distribution, &executable);
            !paths.is_empty() && paths.iter().all(|path| writable(path))
        });
    install_route(distribution, replaceable)
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
enum Phase {
    #[default]
    Idle,
    Available,
    Downloading,
    Verified,
    Installing,
    Installed,
}

struct Pending {
    release: manifest::Release,
    update: Update,
    bytes: Option<Vec<u8>>,
}

struct Store {
    preferences: Preferences,
    revision: u64,
    checking: bool,
    warning: Option<String>,
    phase: Phase,
    pending: Option<Pending>,
    abort: Option<AbortHandle>,
    /// A window close or app exit arrived while installing. The installation
    /// result handler or the deferred-close watcher resumes it.
    exit_requested: bool,
    /// Counts installation attempts, so a watcher started for one attempt
    /// never acts on a later one.
    attempt: u64,
    /// The current attempt's gate; abandoning it prevents any later replacement
    /// step from starting through it.
    gate: Option<Arc<CommitGate>>,
    /// The current attempt's Windows restart, withdrawn by a deferred close.
    restart_cancelled: Option<Arc<AtomicBool>>,
    /// The current attempt was abandoned, so a close no longer waits for it.
    close_released: bool,
}

pub struct UpdateState {
    file: PathBuf,
    recovery: PathBuf,
    inner: Mutex<Store>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Recovery {
    version: String,
    phase: Phase,
}

impl UpdateState {
    pub fn new(directory: PathBuf) -> Self {
        let file = directory.join("updates.json");
        let recovery = directory.join("update-recovery.json");
        let loaded = match fs::File::open(&file) {
            Ok(file) => read_bounded(file, 1024).and_then(|bytes| {
                serde_json::from_slice(&bytes).map_err(|_| "Invalid preferences".into())
            }),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                Ok(Preferences::default())
            }
            Err(_) => Err("Could not read preferences".into()),
        };
        let mut warning = loaded.as_ref().err().map(|_| "Update preferences could not be read. Checks are disabled; save your choice to recover.".into());
        if recovery.is_file() {
            // Windows exits when handing off to NSIS, before clear_mark can run.
            // Only the requested version starting after that handoff resolves it.
            let installed = fs::File::open(&recovery)
                .ok()
                .and_then(|file| read_bounded(file, 1024).ok())
                .and_then(|bytes| serde_json::from_slice::<Recovery>(&bytes).ok())
                .is_some_and(|record| {
                    record.phase == Phase::Installing && record.version == env!("CARGO_PKG_VERSION")
                });
            if !installed {
                warning = Some("A previous update operation was interrupted. No partial or unverified download was reused; check again to recover.".into());
            }
            let _ = fs::remove_file(&recovery);
        }
        Self {
            file,
            recovery,
            inner: Mutex::new(Store {
                preferences: loaded.unwrap_or_default(),
                revision: 0,
                checking: false,
                warning,
                phase: Phase::Idle,
                pending: None,
                abort: None,
                exit_requested: false,
                attempt: 0,
                gate: None,
                restart_cancelled: None,
                close_released: false,
            }),
        }
    }

    fn save_json<T: Serialize>(&self, file: &PathBuf, value: &T) -> Result<(), String> {
        let directory = file.parent().ok_or("Missing update settings directory")?;
        fs::create_dir_all(directory).map_err(|_| "Could not create update settings directory")?;
        let mut temporary = tempfile::NamedTempFile::new_in(directory)
            .map_err(|_| "Could not save update state")?;
        temporary
            .write_all(&serde_json::to_vec(value).map_err(|_| "Invalid update state")?)
            .and_then(|_| temporary.as_file().sync_all())
            .map_err(|_| "Could not save update state")?;
        temporary
            .persist(file)
            .map_err(|_| "Could not save update state")?;
        Ok(())
    }

    fn save(&self, preferences: Preferences) -> Result<(), String> {
        self.save_json(&self.file, &preferences)
            .map_err(|_| "Could not save update preferences".into())
    }

    fn mark(&self, version: &str, phase: Phase) -> Result<(), String> {
        self.save_json(
            &self.recovery,
            &Recovery {
                version: version.to_owned(),
                phase,
            },
        )
    }

    fn clear_mark(&self) {
        if let Err(error) = fs::remove_file(&self.recovery) {
            if error.kind() != std::io::ErrorKind::NotFound {
                // A stale marker is safe: the next launch discards state again.
            }
        }
    }
}

#[derive(Serialize)]
pub struct Status {
    preferences: Preferences,
    version: &'static str,
    configured: bool,
    installable: bool,
    distribution: &'static str,
    phase: Phase,
    pending_version: Option<String>,
    guidance: &'static str,
    warning: Option<String>,
}

/// The status after a successful installation, and whether PlateFold restarts now.
///
/// The UI says it is restarting only when `restarting` is true, never because the
/// user asked for a restart: a close during installation or a folder the
/// installation could not delete defers it. `app.restart()` does not return, so
/// when `restarting` is true the process restarts before this response is
/// delivered; every response the UI receives therefore states that no restart is
/// under way.
#[derive(Serialize)]
pub struct InstallResult {
    #[serde(flatten)]
    status: Status,
    restarting: bool,
}

fn status(state: &Store) -> Status {
    let distribution = distribution(tauri::utils::platform::bundle_type());
    let (installable, guidance) = current_route(distribution);
    Status {
        preferences: state.preferences,
        version: env!("CARGO_PKG_VERSION"),
        configured: Policy::bundled()
            .and_then(|policy| policy.endpoint(state.preferences.channel).map(|_| ()))
            .is_ok(),
        installable,
        distribution,
        phase: state.phase,
        pending_version: state
            .pending
            .as_ref()
            .map(|pending| pending.release.version.clone()),
        guidance,
        warning: state.warning.clone(),
    }
}

#[tauri::command]
pub fn update_status(state: State<'_, UpdateState>) -> Result<Status, String> {
    let store = state
        .inner
        .lock()
        .map_err(|_| "Update state is unavailable")?;
    Ok(status(&store))
}

#[tauri::command]
pub fn update_preferences(
    preferences: Preferences,
    state: State<'_, UpdateState>,
) -> Result<Status, String> {
    save_preferences(&state, preferences)
}

fn save_preferences(state: &UpdateState, preferences: Preferences) -> Result<Status, String> {
    let mut store = state
        .inner
        .lock()
        .map_err(|_| "Update state is unavailable")?;
    if matches!(store.phase, Phase::Downloading | Phase::Installing) {
        return Err("Wait for the current update operation to finish or cancel it".into());
    }
    state.save(preferences)?;
    store.preferences = preferences;
    store.revision += 1;
    store.pending = None;
    // An installed update still waits for its restart, and its warning still names any
    // folder it left; neither depends on the settings.
    if store.phase != Phase::Installed {
        store.warning = None;
        store.phase = Phase::Idle;
    }
    state.clear_mark();
    Ok(status(&store))
}

fn begin(store: &mut Store, policy: &Policy) -> Result<(u64, Channel, String), String> {
    if !store.preferences.enabled {
        return Err("Update checks are disabled".into());
    }
    if store.checking || matches!(store.phase, Phase::Downloading | Phase::Installing) {
        return Err("An update operation is already running".into());
    }
    let endpoint = policy.endpoint(store.preferences.channel)?.to_owned();
    store.checking = true;
    store.pending = None;
    store.phase = Phase::Idle;
    Ok((store.revision, store.preferences.channel, endpoint))
}

fn finish(store: &mut Store, revision: u64, busy: bool) -> Result<(), String> {
    store.checking = false;
    if store.revision != revision {
        return Err("Update settings changed; check again with the new settings".into());
    }
    if busy {
        return Err("An export is running. Check again when it finishes".into());
    }
    Ok(())
}

fn tauri_public_key(raw: &str) -> String {
    BASE64.encode(format!(
        "untrusted comment: PlateFold updater public key\n{raw}\n"
    ))
}

fn decode_artifact_signature(encoded: &str) -> Result<Signature, String> {
    let bytes = BASE64
        .decode(encoded)
        .map_err(|_| "Invalid Tauri artifact signature encoding")?;
    let text =
        std::str::from_utf8(&bytes).map_err(|_| "Invalid Tauri artifact signature encoding")?;
    Signature::decode(text).map_err(|_| "Invalid Tauri artifact signature".into())
}

fn verify_artifact(
    bytes: &[u8],
    version: &str,
    target: &str,
    artifact: &manifest::Artifact,
) -> Result<String, String> {
    if bytes.len() as u64 != artifact.size {
        return Err("Downloaded artifact size does not match the signed release".into());
    }
    let digest = format!("{:x}", Sha256::digest(bytes));
    if digest != artifact.sha256 {
        return Err("Downloaded artifact checksum does not match the signed release".into());
    }
    let key = PublicKey::from_base64(&artifact.public_key)
        .map_err(|_| "The artifact trust key is invalid")?;
    let signature = decode_artifact_signature(&artifact.signature)?;
    // Verifies the signature over the bytes and the global signature over the
    // trusted comment, so the comment checked next is the artifact key's own.
    key.verify(bytes, &signature, false)
        .map_err(|_| "Downloaded artifact signature verification failed")?;
    binding::check(
        signature.trusted_comment(),
        version,
        target,
        &manifest::artifact_file(&artifact.url)?,
    )?;
    Ok(digest)
}

fn prepare_tauri_update(
    app: &tauri::AppHandle,
    endpoint: &str,
    release: &manifest::Release,
) -> Result<Update, String> {
    let artifact = release
        .artifact
        .as_ref()
        .ok_or("This copy can't install updates itself")?;
    let endpoint = https_url(endpoint)?;
    let update = app
        .updater_builder()
        // PlateFold's signed target is exact, including bundle type. This also
        // keeps the documented `macos-*` name instead of Tauri's `darwin-*`.
        .target(release.target.clone())
        .endpoints(vec![endpoint])
        .map_err(|_| "The signed update endpoint is invalid")?
        .pubkey(tauri_public_key(&artifact.public_key))
        .timeout(Duration::from_secs(300))
        .restart_after_install(false)
        .configure_client(|client| {
            client
                .https_only(true)
                .redirect(artifact_redirect_policy())
                .connect_timeout(Duration::from_secs(5))
        })
        .build()
        .map_err(|_| "Could not initialise the Tauri updater")?
        .check_from_json(serde_json::json!({
            "version": release.version,
            "notes": release.notes,
            "platforms": { &release.target: {
                "url": artifact.url,
                "signature": artifact.signature,
            }},
        }))
        .map_err(|_| "The Tauri updater could not read the signed release")?
        .ok_or("The Tauri updater did not accept the signed release")?;
    if update.version != release.version
        || update.download_url.as_str() != artifact.url
        || update.signature != artifact.signature
    {
        return Err("The Tauri updater response does not match the signed release".into());
    }
    Ok(update)
}

#[tauri::command]
pub async fn check_updates(app: tauri::AppHandle) -> Result<Option<manifest::Release>, String> {
    let state = app.state::<UpdateState>();
    let desktop = app.state::<super::Shared>();
    if desktop
        .lock()
        .map_err(|_| "Export state is unavailable")?
        .busy
    {
        return Err("Wait for the export to finish before checking for updates".into());
    }
    let policy = Policy::bundled()?;
    let (revision, channel, endpoint) = {
        let mut store = state
            .inner
            .lock()
            .map_err(|_| "Update state is unavailable")?;
        begin(&mut store, &policy)?
    };
    let distribution = distribution(tauri::utils::platform::bundle_type());
    let target = format!(
        "{}-{}-{}",
        std::env::consts::OS,
        std::env::consts::ARCH,
        distribution
    );
    let metadata_keys = policy.public_keys.clone();
    let artifact_keys = policy.artifact_public_keys.clone();
    let artifact_url_prefix = policy.artifact_url_prefix.clone();
    let fetch_endpoint = endpoint.clone();
    let result: Result<(Option<manifest::Release>, Option<Update>), String> = async {
        let release = tauri::async_runtime::spawn_blocking(move || {
            let bytes = fetch(&fetch_endpoint)?;
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|_| "Check your system clock")?
                .as_secs();
            manifest::verify(
                &bytes,
                &manifest::Trust {
                    metadata_keys: &metadata_keys,
                    artifact_keys: &artifact_keys,
                    artifact_url_prefix: &artifact_url_prefix,
                },
                channel,
                &target,
                env!("CARGO_PKG_VERSION"),
                now,
            )
        })
        .await
        .map_err(|_| "The update check could not finish".to_string())??;
        let prepared = if let Some(release) = release.as_ref().filter(|_| current_route(distribution).0)
        {
            Some(prepare_tauri_update(&app, &endpoint, release)?)
        } else {
            None
        };
        Ok((release, prepared))
    }
    .await;
    let busy = desktop
        .lock()
        .map_err(|_| "Export state is unavailable")?
        .busy;
    let mut store = state
        .inner
        .lock()
        .map_err(|_| "Update state is unavailable")?;
    finish(&mut store, revision, busy)?;
    let (release, prepared) = result?;
    if let (Some(release), Some(update)) = (release.clone(), prepared) {
        store.pending = Some(Pending {
            release,
            update,
            bytes: None,
        });
        store.phase = Phase::Available;
    }
    Ok(release)
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DownloadProgress {
    downloaded: u64,
    total: u64,
}

#[derive(Serialize)]
pub struct VerifiedDownload {
    version: String,
    size: u64,
    sha256: String,
}

#[tauri::command]
pub async fn download_update(app: tauri::AppHandle) -> Result<VerifiedDownload, String> {
    let state = app.state::<UpdateState>();
    let desktop = app.state::<super::Shared>();
    let (release, artifact, update, abort_registration, abort_for_progress, size_violation) = {
        let mut desktop = desktop.lock().map_err(|_| "Export state is unavailable")?;
        if desktop.busy || desktop.updating {
            return Err("Wait for the export or update operation to finish".into());
        }
        let mut store = state
            .inner
            .lock()
            .map_err(|_| "Update state is unavailable")?;
        if store.phase != Phase::Available {
            return Err("Check for an update before downloading it".into());
        }
        let pending = store.pending.as_ref().ok_or("No pending update")?;
        let release = pending.release.clone();
        let artifact = release
            .artifact
            .clone()
            .ok_or("This copy can't install updates itself")?;
        let update = pending.update.clone();
        let (abort, registration) = AbortHandle::new_pair();
        let abort_for_progress = abort.clone();
        state.mark(&release.version, Phase::Downloading)?;
        store.abort = Some(abort);
        store.phase = Phase::Downloading;
        desktop.updating = true;
        (
            release,
            artifact,
            update,
            registration,
            abort_for_progress,
            Arc::new(AtomicBool::new(false)),
        )
    };
    let expected = artifact.size;
    let app_progress = app.clone();
    let violation = size_violation.clone();
    let mut downloaded = 0u64;
    let future = update.download_bounded(
        expected,
        move |chunk, total| {
            downloaded = downloaded.saturating_add(chunk as u64);
            if total.is_some_and(|total| total != expected) || downloaded > expected {
                violation.store(true, Ordering::SeqCst);
                abort_for_progress.abort();
            }
            let _ = app_progress.emit(
                "update-progress",
                DownloadProgress {
                    downloaded,
                    total: expected,
                },
            );
        },
        || {},
    );
    let result = Abortable::new(future, abort_registration).await;
    desktop
        .lock()
        .map_err(|_| "Export state is unavailable")?
        .updating = false;
    let mut store = state
        .inner
        .lock()
        .map_err(|_| "Update state is unavailable")?;
    store.abort = None;
    let bytes = match result {
        Err(_) if size_violation.load(Ordering::SeqCst) => {
            store.phase = Phase::Available;
            state.clear_mark();
            return Err("Downloaded artifact size does not match the signed release".into());
        }
        Err(_) => {
            store.phase = Phase::Available;
            state.clear_mark();
            return Err("Update download cancelled; no partial bytes were kept".into());
        }
        Ok(Err(_)) => {
            store.phase = Phase::Available;
            state.clear_mark();
            return Err(
                "Update download was interrupted or its Tauri signature was rejected".into(),
            );
        }
        Ok(Ok(_)) if size_violation.load(Ordering::SeqCst) => {
            store.phase = Phase::Available;
            state.clear_mark();
            return Err("Downloaded artifact size does not match the signed release".into());
        }
        Ok(Ok(bytes)) => bytes,
    };
    let digest = match verify_artifact(&bytes, &release.version, &release.target, &artifact) {
        Ok(digest) => digest,
        Err(error) => {
            store.phase = Phase::Available;
            state.clear_mark();
            return Err(error);
        }
    };
    let pending = store.pending.as_mut().ok_or("No pending update")?;
    if let Err(error) = state.mark(&release.version, Phase::Verified) {
        store.phase = Phase::Available;
        return Err(error);
    }
    pending.bytes = Some(bytes);
    store.phase = Phase::Verified;
    Ok(VerifiedDownload {
        version: release.version,
        size: artifact.size,
        sha256: digest,
    })
}

#[tauri::command]
pub fn cancel_update(state: State<'_, UpdateState>) -> Result<(), String> {
    let mut store = state
        .inner
        .lock()
        .map_err(|_| "Update state is unavailable")?;
    if store.phase == Phase::Installing {
        return Err("Installation has started and cannot be cancelled safely".into());
    }
    if let Some(abort) = store.abort.take() {
        abort.abort();
        return Ok(());
    }
    store.pending = None;
    store.phase = Phase::Idle;
    state.clear_mark();
    Ok(())
}

#[tauri::command]
pub async fn install_update(app: tauri::AppHandle, restart: bool) -> Result<InstallResult, String> {
    let state = app.state::<UpdateState>();
    let desktop = app.state::<super::Shared>();
    let (version, update, bytes, cleanup) = {
        let mut desktop = desktop.lock().map_err(|_| "Export state is unavailable")?;
        if desktop.busy || desktop.updating {
            return Err("Wait for the export or update operation to finish".into());
        }
        let mut store = state
            .inner
            .lock()
            .map_err(|_| "Update state is unavailable")?;
        if store.phase != Phase::Verified {
            return Err("Download and verify the update before installing it".into());
        }
        let pending = store.pending.as_mut().ok_or("No verified update")?;
        if pending.bytes.is_none() {
            return Err("No verified update bytes".into());
        }
        let version = pending.release.version.clone();
        let gate = Arc::new(CommitGate::new());
        let restart_cancelled = Arc::new(AtomicBool::new(false));
        let cleanup = Arc::new(CleanupReport::new());
        let update = pending
            .update
            .clone()
            .restart_after_install(restart)
            .restart_cancelled_by(restart_cancelled.clone())
            .commit_gate(gate.clone())
            .cleanup_report(cleanup.clone());
        state.mark(&version, Phase::Installing)?;
        let bytes = pending.bytes.take().ok_or("No verified update bytes")?;
        store.phase = Phase::Installing;
        store.attempt += 1;
        store.gate = Some(gate);
        store.restart_cancelled = Some(restart_cancelled);
        store.exit_requested = false;
        store.close_released = false;
        desktop.updating = true;
        (version, update, bytes, cleanup)
    };
    let result = tauri::async_runtime::spawn_blocking(move || {
        let result = update.install(&bytes);
        (result, bytes)
    })
    .await;
    // Folders the installation could not delete. They add to the warning and never change
    // the result.
    let leftovers = cleanup.take();
    // Keep exports excluded until a requested restart has committed to process exit.
    let (outcome, exit_requested) = {
        let mut desktop = desktop.lock().map_err(|_| "Export state is unavailable")?;
        let mut store = state
            .inner
            .lock()
            .map_err(|_| "Update state is unavailable")?;
        // Read under the same lock that ends Installing, so a close request is
        // either seen here or arrives after the phase has left Installing.
        let exit_requested = std::mem::take(&mut store.exit_requested);
        let outcome = match result {
            Ok((Ok(()), _)) => {
                store.phase = Phase::Installed;
                store.pending = None;
                store.warning = with_leftovers(None, &leftovers);
                state.clear_mark();
                let installed = InstallResult {
                    status: status(&store),
                    restarting: restarts_now(restart, exit_requested, &leftovers),
                };
                drop(store);
                if installed.restarting {
                    drop(desktop);
                    app.restart();
                }
                desktop.updating = false;
                Ok(installed)
            }
            Ok((Err(error), bytes)) => {
                desktop.updating = false;
                let failure = install_failure(&version, &error);
                if failure.retry {
                    if let Some(pending) = store.pending.as_mut() {
                        pending.bytes = Some(bytes);
                    }
                    store.phase = Phase::Verified;
                } else {
                    store.pending = None;
                    store.phase = Phase::Idle;
                }
                store.warning = with_leftovers(Some(failure.warning), &leftovers);
                state.clear_mark();
                Err(failure.message)
            }
            Err(_) => {
                desktop.updating = false;
                store.phase = Phase::Idle;
                store.pending = None;
                store.warning = Some("Installation was interrupted. Check the installed version and download again before retrying.".into());
                // Keep the marker: the native replacement outcome is unknown.
                Err(
                    "Installation was interrupted; check the installed version before retrying"
                        .into(),
                )
            }
        };
        (outcome, exit_requested)
    };
    if exit_requested {
        close_after_install(&app);
    }
    outcome
}

/// Finish a close or exit that arrived while installing, now that the
/// installation has a terminal result or was abandoned.
fn close_after_install(app: &tauri::AppHandle) {
    match app.get_webview_window("main") {
        // Runs the ordinary close handler, which no longer defers.
        Some(window) => {
            let _ = window.close();
        }
        None => app.exit(0),
    }
}

/// What a handled installation failure means for the user.
struct InstallFailure {
    /// Whether the verified download stays available for another attempt.
    retry: bool,
    warning: String,
    message: String,
}

fn install_failure(version: &str, error: &tauri_plugin_updater::Error) -> InstallFailure {
    use tauri_plugin_updater::Error;
    const RETRY: &str = "The updater could not install the verified package; retry or cancel";
    match error {
        // The replacement kept the previous copy but could not put it back. Retrying would not
        // restore it, so the user is told where it is instead.
        Error::ReplacementNotRestored { app, backup, .. } => InstallFailure {
            retry: false,
            warning: format!(
                "Installation of {version} failed, and PlateFold could not put the previous copy back. It is kept at {}. Move it back to {} to use PlateFold again.",
                backup.display(),
                app.display()
            ),
            message: format!(
                "The update could not be installed. The previous copy of PlateFold is kept at {}",
                backup.display()
            ),
        },
        // The volume cannot exchange the app bundles, so nothing was moved. A retry would be
        // refused the same way, so none is offered. If staging cleanup fails, the user is told
        // which unused update folder they can delete.
        Error::ReplacementUnsupported { staging_left } => {
            let leftover = staging_left
                .as_deref()
                .map_or_else(String::new, |folder| format!(" {}", leftover_sentence(folder)));
            InstallFailure {
                retry: false,
                warning: format!("Update {version} was not installed because this disk can't replace PlateFold safely. PlateFold is unchanged. Install the update by hand.{leftover}"),
                message: "The update was not installed; install it by hand".into(),
            }
        }
        Error::ReplacementRolledBack(_) => InstallFailure {
            retry: true,
            warning: format!("Installation of {version} failed, and the installed copy was put back unchanged. The verified download is still available to retry or cancel."),
            message: RETRY.into(),
        },
        _ => InstallFailure {
            retry: true,
            warning: format!("Installation of {version} failed. The verified download is still available to retry or cancel; the last working package was not declared replaced."),
            message: RETRY.into(),
        },
    }
}

/// Whether a successful installation restarts PlateFold at once.
///
/// A close during installation wins over a requested restart: the update is in
/// place and starts with the next launch. A folder the installation could not
/// delete also defers the restart, so that the warning naming it is shown; the
/// user restarts with the Restart button.
fn restarts_now(requested: bool, exit_requested: bool, leftovers: &[PathBuf]) -> bool {
    requested && !exit_requested && leftovers.is_empty()
}

/// Tells the user they can delete a folder an installation could not delete.
fn leftover_sentence(folder: &Path) -> String {
    format!(
        "PlateFold could not delete the unused update folder at {}. You can delete that folder.",
        folder.display()
    )
}

/// Adds the folders an installation could not delete to its warning, if any.
fn with_leftovers(warning: Option<String>, folders: &[PathBuf]) -> Option<String> {
    let sentences: Vec<String> = warning
        .into_iter()
        .chain(folders.iter().map(PathBuf::as_path).map(leftover_sentence))
        .collect();
    (!sentences.is_empty()).then(|| sentences.join(" "))
}

fn reserve_restart(desktop: &mut super::Desktop, phase: Phase) -> Result<(), String> {
    if desktop.busy || desktop.updating {
        return Err("Wait for the export or update operation to finish before restarting".into());
    }
    if phase != Phase::Installed {
        return Err("No installed update is waiting for restart".into());
    }
    desktop.updating = true;
    Ok(())
}

#[tauri::command]
pub fn restart_update(app: tauri::AppHandle) -> Result<(), String> {
    let desktop = app.state::<super::Shared>();
    let mut desktop = desktop.lock().map_err(|_| "Export state is unavailable")?;
    let state = app.state::<UpdateState>();
    let store = state
        .inner
        .lock()
        .map_err(|_| "Update state is unavailable")?;
    reserve_restart(&mut desktop, store.phase)?;
    drop(store);
    drop(desktop);
    app.restart();
}

/// Defer a close or app exit while an installation attempt is active.
///
/// AppImage replacement and a supported macOS bundle exchange change the
/// installed path in one step. A macOS volume that cannot exchange bundles is
/// refused without moving the installed app.
///
/// The installation result handler normally resumes the close. After
/// [`DEFERRED_CLOSE_LIMIT`] from the close request, the watcher abandons
/// further replacement and resumes the close once the gate is idle. If a
/// replacement or Windows installer launch is already running, the close
/// waits for that step. Abandoning the gate does not undo an installed update.
///
/// A close also withdraws a requested Windows restart if the updater has not
/// yet read the restart flag for launch. Returns whether the caller must keep
/// the window open and the process running.
pub fn defer_exit_while_installing(app: &tauri::AppHandle) -> bool {
    let Some(state) = app.try_state::<UpdateState>() else {
        return false;
    };
    let Ok(mut store) = state.inner.lock() else {
        // No installation can be tracked through a poisoned lock; let it exit.
        return false;
    };
    let first = !store.exit_requested;
    if !defer_exit(&mut store) {
        return false;
    }
    let attempt = store.attempt;
    drop(store);
    let _ = app.emit("update-close-pending", ());
    if first {
        let app = app.clone();
        std::thread::spawn(move || watch_deferred_close(app, attempt));
    }
    true
}

fn defer_exit(store: &mut Store) -> bool {
    if store.phase != Phase::Installing || store.close_released {
        return false;
    }
    store.exit_requested = true;
    if let Some(cancelled) = &store.restart_cancelled {
        cancelled.store(true, Ordering::SeqCst);
    }
    true
}

/// After the close-wait limit, resumes the close once the attempt's gate is idle.
fn watch_deferred_close(app: tauri::AppHandle, attempt: u64) {
    let deadline = Instant::now() + DEFERRED_CLOSE_LIMIT;
    loop {
        std::thread::sleep(DEFERRED_CLOSE_POLL);
        let state = app.state::<UpdateState>();
        let Ok(mut store) = state.inner.lock() else {
            return;
        };
        if !release_overdue_close(&mut store, attempt, Instant::now() >= deadline) {
            if store.phase != Phase::Installing || store.attempt != attempt {
                // Finished: install_update resumes the close itself.
                return;
            }
            continue;
        }
        drop(store);
        close_after_install(&app);
        return;
    }
}

/// Abandons further replacement by an overdue attempt once its gate is idle,
/// and releases the close it was holding. Returns whether the close may go ahead.
fn release_overdue_close(store: &mut Store, attempt: u64, overdue: bool) -> bool {
    if !overdue || store.phase != Phase::Installing || store.attempt != attempt {
        return false;
    }
    // Keep waiting while replacement or Windows installer launch holds the gate.
    if !store.gate.as_ref().is_none_or(|gate| gate.abandon()) {
        return false;
    }
    store.close_released = true;
    true
}

pub fn cancel_for_exit(state: &UpdateState) {
    if let Ok(mut store) = state.inner.lock() {
        if let Some(abort) = store.abort.take() {
            abort.abort();
        }
    }
}
