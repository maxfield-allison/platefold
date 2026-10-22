// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT

use std::{
    collections::HashMap,
    ffi::OsString,
    path::{Path, PathBuf},
    str::FromStr,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

#[cfg(any(windows, all(target_os = "linux", feature = "zip")))]
use std::ffi::OsStr;
#[cfg(any(target_os = "macos", feature = "zip"))]
use std::io::Cursor;

#[cfg(any(
    target_os = "macos",
    target_os = "linux",
    target_os = "dragonfly",
    target_os = "freebsd",
    target_os = "netbsd",
    target_os = "openbsd"
))]
mod replace;

mod gate;
pub use gate::CommitGate;

mod cleanup;
pub use cleanup::CleanupReport;

use base64::Engine;
use futures_util::StreamExt;
use http::{header::ACCEPT, HeaderName};
use minisign_verify::{PublicKey, Signature};
use percent_encoding::{AsciiSet, CONTROLS};
use reqwest::{
    header::{HeaderMap, HeaderValue},
    ClientBuilder, StatusCode,
};
use semver::Version;
use serde::{de::Error as DeError, Deserialize, Deserializer, Serialize};
use tauri::{
    utils::{
        config::BundleType,
        platform::{bundle_type, current_exe},
    },
    AppHandle, Resource, Runtime,
};
use time::OffsetDateTime;
use url::Url;

use crate::{
    error::{Error, Result},
    Config,
};

const UPDATER_USER_AGENT: &str = concat!(env!("CARGO_PKG_NAME"), "/", env!("CARGO_PKG_VERSION"),);

#[derive(Copy, Clone)]
pub enum Installer {
    AppImage,
    Deb,
    Rpm,

    App,

    Msi,
    Nsis,
}

impl Installer {
    fn name(self) -> &'static str {
        match self {
            Self::AppImage => "appimage",
            Self::Deb => "deb",
            Self::Rpm => "rpm",
            Self::App => "app",
            Self::Msi => "msi",
            Self::Nsis => "nsis",
        }
    }
}

#[derive(Debug, Deserialize, Serialize, Clone)]
pub struct ReleaseManifestPlatform {
    /// Download URL for the platform
    pub url: Url,
    /// Signature for the platform
    pub signature: String,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[serde(untagged)]
pub enum RemoteReleaseInner {
    Dynamic(ReleaseManifestPlatform),
    Static {
        platforms: HashMap<String, ReleaseManifestPlatform>,
    },
}

/// Information about a release returned by the remote update server.
///
/// This type can have one of two shapes: Server Format (Dynamic Format) and Static Format.
#[derive(Debug, Clone)]
pub struct RemoteRelease {
    /// Version to install.
    pub version: Version,
    /// Release notes.
    pub notes: Option<String>,
    /// Release date.
    pub pub_date: Option<OffsetDateTime>,
    /// Release data.
    pub data: RemoteReleaseInner,
}

impl RemoteRelease {
    /// The release's download URL for the given target.
    pub fn download_url(&self, target: &str) -> Result<&Url> {
        match self.data {
            RemoteReleaseInner::Dynamic(ref platform) => Ok(&platform.url),
            RemoteReleaseInner::Static { ref platforms } => platforms
                .get(target)
                .map_or(Err(Error::TargetNotFound(target.to_string())), |p| {
                    Ok(&p.url)
                }),
        }
    }

    /// The release's signature for the given target.
    pub fn signature(&self, target: &str) -> Result<&String> {
        match self.data {
            RemoteReleaseInner::Dynamic(ref platform) => Ok(&platform.signature),
            RemoteReleaseInner::Static { ref platforms } => platforms
                .get(target)
                .map_or(Err(Error::TargetNotFound(target.to_string())), |platform| {
                    Ok(&platform.signature)
                }),
        }
    }
}

pub type OnBeforeExit = Arc<dyn Fn() + Send + Sync + 'static>;
pub type OnBeforeRequest = Arc<dyn Fn(ClientBuilder) -> ClientBuilder + Send + Sync + 'static>;
pub type VersionComparator = Arc<dyn Fn(Version, RemoteRelease) -> bool + Send + Sync>;

// TODO: Move more fields to this in v3 if we can mark those fields non `pub`
/// Updater context shared between [`UpdaterBuilder`], [`Updater`] and [`Update`]
#[derive(Clone)]
struct UpdaterContext {
    config: Config,
    configure_client: Option<OnBeforeRequest>,
    /// App name, used for creating named tempfiles
    #[cfg(windows)]
    app_name: String,
    #[cfg(windows)]
    installer_args: Vec<OsString>,
    #[cfg(windows)]
    current_exe_args: Vec<OsString>,
    #[cfg(windows)]
    on_before_exit: Option<OnBeforeExit>,
    #[cfg(windows)]
    restart_after_install: bool,
    /// Set by the application when the installer must no longer relaunch it.
    #[cfg(windows)]
    restart_cancelled: Option<Arc<AtomicBool>>,
    /// Shared with the application, which can abandon the installation
    /// before it replaces anything.
    commit_gate: Arc<CommitGate>,
    /// Shared with the application, which reads the folders an installation
    /// could not delete.
    cleanup_report: Arc<CleanupReport>,
}

pub struct UpdaterBuilder {
    current_version: Version,
    pub(crate) version_comparator: Option<VersionComparator>,
    executable_path: Option<PathBuf>,
    target: Option<String>,
    endpoints: Option<Vec<Url>>,
    headers: HeaderMap,
    timeout: Option<Duration>,
    proxy: Option<Url>,
    no_proxy: bool,
    context: UpdaterContext,
}

impl UpdaterBuilder {
    pub(crate) fn new<R: Runtime>(app: &AppHandle<R>, config: crate::Config) -> Self {
        Self {
            context: UpdaterContext {
                #[cfg(windows)]
                installer_args: config
                    .windows
                    .as_ref()
                    .map(|w| w.installer_args.clone())
                    .unwrap_or_default(),
                config,
                configure_client: None,
                #[cfg(windows)]
                app_name: app.package_info().name.clone(),
                #[cfg(windows)]
                current_exe_args: Vec::new(),
                #[cfg(windows)]
                on_before_exit: None,
                #[cfg(windows)]
                restart_after_install: true,
                #[cfg(windows)]
                restart_cancelled: None,
                commit_gate: Arc::new(CommitGate::new()),
                cleanup_report: Arc::new(CleanupReport::new()),
            },
            current_version: app.package_info().version.clone(),
            version_comparator: None,
            executable_path: None,
            target: None,
            endpoints: None,
            headers: Default::default(),
            timeout: None,
            proxy: None,
            no_proxy: false,
        }
    }

    pub fn version_comparator<F: Fn(Version, RemoteRelease) -> bool + Send + Sync + 'static>(
        mut self,
        f: F,
    ) -> Self {
        self.version_comparator = Some(Arc::new(f));
        self
    }

    pub fn target(mut self, target: impl Into<String>) -> Self {
        self.target.replace(target.into());
        self
    }

    pub fn endpoints(mut self, endpoints: Vec<Url>) -> Result<Self> {
        crate::config::validate_endpoints(
            &endpoints,
            self.context.config.dangerous_insecure_transport_protocol,
        )?;

        self.endpoints.replace(endpoints);
        Ok(self)
    }

    pub fn executable_path<P: AsRef<Path>>(mut self, p: P) -> Self {
        self.executable_path.replace(p.as_ref().into());
        self
    }

    pub fn header<K, V>(mut self, key: K, value: V) -> Result<Self>
    where
        HeaderName: TryFrom<K>,
        <HeaderName as TryFrom<K>>::Error: Into<http::Error>,
        HeaderValue: TryFrom<V>,
        <HeaderValue as TryFrom<V>>::Error: Into<http::Error>,
    {
        let key: std::result::Result<HeaderName, http::Error> = key.try_into().map_err(Into::into);
        let value: std::result::Result<HeaderValue, http::Error> =
            value.try_into().map_err(Into::into);
        self.headers.insert(key?, value?);

        Ok(self)
    }

    pub fn headers(mut self, headers: HeaderMap) -> Self {
        self.headers = headers;
        self
    }

    pub fn clear_headers(mut self) -> Self {
        self.headers.clear();
        self
    }

    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }

    pub fn proxy(mut self, proxy: Url) -> Self {
        self.proxy.replace(proxy);
        self
    }

    /// Clear all proxies. See [`reqwest::ClientBuilder::no_proxy`](https://docs.rs/reqwest/latest/reqwest/struct.ClientBuilder.html#method.no_proxy).
    pub fn no_proxy(mut self) -> Self {
        self.no_proxy = true;
        self
    }

    pub fn pubkey<S: Into<String>>(mut self, pubkey: S) -> Self {
        self.context.config.pubkey = pubkey.into();
        self
    }

    /// Adds an argument to pass to the Windows installer.
    ///
    /// Note: this applies to both WiX and NSIS installers
    #[cfg_attr(not(windows), allow(unused))]
    pub fn installer_arg<S>(mut self, arg: S) -> Self
    where
        S: Into<OsString>,
    {
        #[cfg(windows)]
        {
            self.context.installer_args.push(arg.into());
        }
        self
    }

    /// Adds multiple arguments to pass to the Windows installer.
    ///
    /// Note: this applies to both WiX and NSIS installers
    #[cfg_attr(not(windows), allow(unused))]
    pub fn installer_args<I, S>(mut self, args: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<OsString>,
    {
        #[cfg(windows)]
        {
            self.context
                .installer_args
                .extend(args.into_iter().map(Into::into));
        }
        self
    }

    /// Removes all the additional arguments to pass to the Windows installer.
    ///
    /// Note: this only removes the additional arguments added through
    /// [`Self::installer_arg`], [`crate::Builder::installer_arg`]
    /// and the `plugins > updater > windows > installerArgs` config,
    /// not the ones managed by us (e.g. `/UPDATER` flag passed to the NSIS installer)
    #[cfg_attr(not(windows), allow(unused))]
    pub fn clear_installer_args(mut self) -> Self {
        #[cfg(windows)]
        {
            self.context.installer_args.clear();
        }
        self
    }

    /// Function to run before we run the installer and exit the app through `std::process::exit(0)` on Windows
    #[cfg_attr(not(windows), allow(unused))]
    pub fn on_before_exit<F: Fn() + Send + Sync + 'static>(mut self, f: F) -> Self {
        #[cfg(windows)]
        {
            self.context.on_before_exit.replace(Arc::new(f));
        }
        self
    }

    /// If the Windows installer should restart the app after installed, default is `true`
    #[cfg_attr(not(windows), allow(unused))]
    pub fn restart_after_install(mut self, restart_after_install: bool) -> Self {
        #[cfg(windows)]
        {
            self.context.restart_after_install = restart_after_install;
        }
        self
    }

    /// Allows you to modify the `reqwest` client builder before the HTTP request is sent.
    ///
    /// Note that `reqwest` crate may be updated in minor releases of tauri-plugin-updater.
    /// Therefore it's recommended to pin the plugin to at least a minor version when you're using `configure_client`.
    pub fn configure_client<F: Fn(ClientBuilder) -> ClientBuilder + Send + Sync + 'static>(
        mut self,
        f: F,
    ) -> Self {
        self.context.configure_client.replace(Arc::new(f));
        self
    }

    pub fn build(self) -> Result<Updater> {
        let endpoints = self
            .endpoints
            .unwrap_or_else(|| self.context.config.endpoints.clone());

        if endpoints.is_empty() {
            return Err(Error::EmptyEndpoints);
        };

        let arch = updater_arch().ok_or(Error::UnsupportedArch)?;

        let executable_path = self.executable_path.clone().unwrap_or(current_exe()?);

        // Get the extract_path from the provided executable_path
        let extract_path = if cfg!(target_os = "linux") {
            executable_path
        } else {
            extract_path_from_executable(&executable_path)?
        };

        Ok(Updater {
            current_version: self.current_version,
            version_comparator: self.version_comparator,
            timeout: self.timeout,
            proxy: self.proxy,
            no_proxy: self.no_proxy,
            endpoints,
            arch,
            target: self.target,
            headers: self.headers,
            extract_path,
            context: self.context.clone(),
        })
    }
}

#[cfg(windows)]
impl UpdaterBuilder {
    pub(crate) fn current_exe_args<I, S>(mut self, args: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<OsString>,
    {
        self.context
            .current_exe_args
            .extend(args.into_iter().map(Into::into));
        self
    }
}

pub struct Updater {
    current_version: Version,
    version_comparator: Option<VersionComparator>,
    timeout: Option<Duration>,
    proxy: Option<Url>,
    no_proxy: bool,
    endpoints: Vec<Url>,
    arch: &'static str,
    // The `{{target}}` variable we replace in the endpoint and serach for in the JSON,
    // this is either the user provided target or the current operating system by default
    target: Option<String>,
    headers: HeaderMap,
    extract_path: PathBuf,
    context: UpdaterContext,
}

impl Updater {
    pub async fn check(&self) -> Result<Option<Update>> {
        // we want JSON only
        let mut headers = self.headers.clone();
        if !headers.contains_key(ACCEPT) {
            headers.insert(ACCEPT, HeaderValue::from_static("application/json"));
        }

        // Set SSL certs for linux if they aren't available.
        #[cfg(target_os = "linux")]
        {
            if std::env::var_os("SSL_CERT_FILE").is_none() {
                std::env::set_var("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt");
            }
            if std::env::var_os("SSL_CERT_DIR").is_none() {
                std::env::set_var("SSL_CERT_DIR", "/etc/ssl/certs");
            }
        }
        let target = if let Some(target) = &self.target {
            target
        } else {
            updater_os().ok_or(Error::UnsupportedOs)?
        };

        let mut remote_release: Option<RemoteRelease> = None;
        let mut raw_json: Option<serde_json::Value> = None;
        let mut last_error: Option<Error> = None;
        for url in &self.endpoints {
            // replace {{current_version}}, {{target}}, {{arch}} and {{bundle_type}} in the provided URL
            // this is useful if we need to query example
            // https://releases.myapp.com/update/{{target}}/{{arch}}/{{current_version}}
            // will be translated into ->
            // https://releases.myapp.com/update/darwin/aarch64/1.0.0
            // The main objective is if the update URL is defined via the Cargo.toml
            // the URL will be generated dynamically
            let version = self.current_version.to_string();
            let version = version.as_bytes();
            const CONTROLS_ADD: &AsciiSet = &CONTROLS.add(b'+');
            let encoded_version = percent_encoding::percent_encode(version, CONTROLS_ADD);
            let encoded_version = encoded_version.to_string();
            let installer = installer_for_bundle_type(bundle_type())
                .map(|i| i.name())
                .unwrap_or("unknown");

            let url: Url = url
                .to_string()
                // url::Url automatically url-encodes the path components
                .replace("%7B%7Bcurrent_version%7D%7D", &encoded_version)
                .replace("%7B%7Btarget%7D%7D", target)
                .replace("%7B%7Barch%7D%7D", self.arch)
                .replace("%7B%7Bbundle_type%7D%7D", installer)
                // but not query parameters
                .replace("{{current_version}}", &encoded_version)
                .replace("{{target}}", target)
                .replace("{{arch}}", self.arch)
                .replace("{{bundle_type}}", installer)
                .parse()?;

            log::debug!("checking for updates {url}");

            #[cfg(feature = "rustls-tls")]
            if rustls::crypto::CryptoProvider::get_default().is_none() {
                // This can only fail if there is already a default provider which we checked for already.
                let _ = rustls::crypto::ring::default_provider().install_default();
            }

            let mut request = ClientBuilder::new().user_agent(UPDATER_USER_AGENT);
            if let Some(timeout) = self.timeout {
                request = request.timeout(timeout);
            }
            if self.no_proxy {
                log::debug!("disabling proxy");
                request = request.no_proxy();
            } else if let Some(ref proxy) = self.proxy {
                log::debug!("using proxy {proxy}");
                let proxy = reqwest::Proxy::all(proxy.as_str())?;
                request = request.proxy(proxy);
            }

            if let Some(ref configure_client) = self.context.configure_client {
                request = configure_client(request);
            }

            let response = request
                .build()?
                .get(url)
                .headers(headers.clone())
                .send()
                .await;

            match response {
                Ok(res) => {
                    if res.status().is_success() {
                        // no updates found!
                        if StatusCode::NO_CONTENT == res.status() {
                            log::debug!("update endpoint returned 204 No Content");
                            return Ok(None);
                        };

                        let update_response: serde_json::Value = res.json().await?;
                        log::debug!("update response: {update_response:?}");
                        raw_json = Some(update_response.clone());
                        match serde_json::from_value::<RemoteRelease>(update_response)
                            .map_err(Into::into)
                        {
                            Ok(release) => {
                                log::debug!("parsed release response {release:?}");
                                last_error = None;
                                remote_release = Some(release);
                                // we found a release, break the loop
                                break;
                            }
                            Err(err) => {
                                log::error!("failed to deserialize update response: {err}");
                                last_error = Some(err)
                            }
                        }
                    } else {
                        log::error!(
                            "update endpoint did not respond with a successful status code"
                        );
                    }
                }
                Err(err) => {
                    log::error!("failed to check for updates: {err}");
                    last_error = Some(err.into())
                }
            }
        }

        // Last error is cleaned on success.
        // Shouldn't be triggered if we had a successfull call
        if let Some(error) = last_error {
            return Err(error);
        }

        // Extracted remote metadata
        let release = remote_release.ok_or(Error::ReleaseNotFound)?;

        self.update_from_release(release, raw_json.ok_or(Error::ReleaseNotFound)?)
    }

    /// Build an update from metadata already fetched and authenticated by the caller.
    /// This performs no network request. Artifact verification still runs at download.
    pub fn check_from_json(&self, raw_json: serde_json::Value) -> Result<Option<Update>> {
        let release = serde_json::from_value::<RemoteRelease>(raw_json.clone())?;
        self.update_from_release(release, raw_json)
    }

    fn update_from_release(
        &self,
        release: RemoteRelease,
        raw_json: serde_json::Value,
    ) -> Result<Option<Update>> {
        let target = if let Some(target) = &self.target {
            target
        } else {
            updater_os().ok_or(Error::UnsupportedOs)?
        };
        let should_update = match self.version_comparator.as_ref() {
            Some(comparator) => comparator(self.current_version.clone(), release.clone()),
            None => release.version > self.current_version,
        };

        let installer = installer_for_bundle_type(bundle_type());
        let (download_url, signature) = self.get_urls(&release, &installer)?;

        let update = if should_update {
            Some(Update {
                current_version: self.current_version.to_string(),
                target: target.to_owned(),
                extract_path: self.extract_path.clone(),
                version: release.version.to_string(),
                date: release.pub_date,
                download_url: download_url.clone(),
                signature: signature.to_owned(),
                body: release.notes,
                raw_json,
                timeout: self.timeout,
                proxy: self.proxy.clone(),
                no_proxy: self.no_proxy,
                headers: self.headers.clone(),
                context: self.context.clone(),
            })
        } else {
            None
        };

        Ok(update)
    }

    fn get_urls<'a>(
        &self,
        release: &'a RemoteRelease,
        installer: &Option<Installer>,
    ) -> Result<(&'a Url, &'a String)> {
        // Use the user provided target
        if let Some(target) = &self.target {
            return Ok((release.download_url(target)?, release.signature(target)?));
        }

        // Or else we search for [`{os}-{arch}-{installer}`, `{os}-{arch}`] in order
        let os = updater_os().ok_or(Error::UnsupportedOs)?;
        let arch = self.arch;
        let mut targets = Vec::new();
        if let Some(installer) = installer {
            let installer = installer.name();
            targets.push(format!("{os}-{arch}-{installer}"));
        }
        targets.push(format!("{os}-{arch}"));

        for target in &targets {
            log::debug!("Searching for updater target '{target}' in release data");
            if let (Ok(download_url), Ok(signature)) =
                (release.download_url(target), release.signature(target))
            {
                return Ok((download_url, signature));
            };
        }

        Err(Error::TargetsNotFound(targets))
    }
}

#[derive(Clone)]
pub struct Update {
    /// Update description
    pub body: Option<String>,
    /// Version used to check for update
    pub current_version: String,
    /// Version announced
    pub version: String,
    /// Update publish date
    pub date: Option<OffsetDateTime>,
    /// The `{{target}}` variable we replace in the endpoint and search for in the JSON,
    /// this is either the user provided target or the current operating system by default
    pub target: String,
    /// Download URL announced
    pub download_url: Url,
    /// Signature announced
    pub signature: String,
    /// The raw version of server's JSON response. Useful if the response contains additional fields that the updater doesn't handle.
    pub raw_json: serde_json::Value,
    /// Request timeout
    pub timeout: Option<Duration>,
    /// Request proxy
    pub proxy: Option<Url>,
    /// Disable system proxy
    pub no_proxy: bool,
    /// Request headers
    pub headers: HeaderMap,
    /// Extract path
    #[allow(unused)]
    extract_path: PathBuf,
    context: UpdaterContext,
}

impl Resource for Update {}

impl Update {
    /// Downloads the updater package, verifies it then return it as bytes.
    ///
    /// Use [`Update::install`] to install it
    pub async fn download<C: FnMut(usize, Option<u64>), D: FnOnce()>(
        &self,
        on_chunk: C,
        on_download_finish: D,
    ) -> Result<Vec<u8>> {
        self.download_bounded(usize::MAX as u64, on_chunk, on_download_finish)
            .await
    }

    /// Download with a caller-supplied byte ceiling, checked before extending the buffer.
    pub async fn download_bounded<C: FnMut(usize, Option<u64>), D: FnOnce()>(
        &self,
        max_bytes: u64,
        mut on_chunk: C,
        on_download_finish: D,
    ) -> Result<Vec<u8>> {
        // set our headers
        let mut headers = self.headers.clone();
        if !headers.contains_key(ACCEPT) {
            headers.insert(ACCEPT, HeaderValue::from_static("application/octet-stream"));
        }

        let mut request = ClientBuilder::new().user_agent(UPDATER_USER_AGENT);
        if let Some(timeout) = self.timeout {
            request = request.timeout(timeout);
        }
        if self.no_proxy {
            request = request.no_proxy();
        } else if let Some(ref proxy) = self.proxy {
            let proxy = reqwest::Proxy::all(proxy.as_str())?;
            request = request.proxy(proxy);
        }
        if let Some(ref configure_client) = self.context.configure_client {
            request = configure_client(request);
        }
        let response = request
            .build()?
            .get(self.download_url.clone())
            .headers(headers)
            .send()
            .await?;

        if !response.status().is_success() {
            return Err(Error::Network(format!(
                "Download request failed with status: {}",
                response.status()
            )));
        }

        let content_length: Option<u64> = response
            .headers()
            .get("Content-Length")
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse().ok());

        if content_length.is_some_and(|size| size > max_bytes) {
            return Err(Error::Network(
                "Update artifact exceeds its size limit".into(),
            ));
        }
        let mut buffer = Vec::new();

        let mut stream = response.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            if (buffer.len() as u64).saturating_add(chunk.len() as u64) > max_bytes {
                return Err(Error::Network(
                    "Update artifact exceeds its size limit".into(),
                ));
            }
            if buffer.capacity() - buffer.len() < chunk.len() {
                let required = buffer.len().saturating_add(chunk.len());
                let capacity = (buffer.capacity() as u64)
                    .saturating_mul(2)
                    .max(required as u64)
                    .min(max_bytes) as usize;
                buffer
                    .try_reserve_exact(capacity - buffer.len())
                    .map_err(|_| {
                        Error::Network("Not enough memory for the update artifact".into())
                    })?;
            }
            on_chunk(chunk.len(), content_length);
            buffer.extend(chunk);
        }
        on_download_finish();

        verify_signature(&buffer, &self.signature, &self.context.config.pubkey)?;

        Ok(buffer)
    }

    /// Installs the updater package downloaded by [`Update::download`]
    ///
    /// ## Platform-specific:
    ///
    /// - **Windows:** This function exits the app after launching the updater installer successfully
    /// - **macOS / Linux:** You need to relaunch the app to run the newly install version
    pub fn install(&self, bytes: impl AsRef<[u8]>) -> Result<()> {
        self.install_inner(bytes.as_ref())
    }

    /// Downloads and installs the updater package
    ///
    /// ## Platform-specific:
    ///
    /// - **Windows:** This function exits the app after launching the updater installer successfully
    /// - **macOS / Linux:** You need to relaunch the app to run the newly install version
    pub async fn download_and_install<C: FnMut(usize, Option<u64>), D: FnOnce()>(
        &self,
        on_chunk: C,
        on_download_finish: D,
    ) -> Result<()> {
        let bytes = self.download(on_chunk, on_download_finish).await?;
        self.install(bytes)
    }

    #[cfg(mobile)]
    fn install_inner(&self, _bytes: &[u8]) -> Result<()> {
        Ok(())
    }

    /// Whether the Windows installer should restart the app after installed, default is `true`
    #[cfg_attr(not(windows), allow(unused))]
    pub fn restart_after_install(mut self, restart_after_install: bool) -> Self {
        #[cfg(windows)]
        {
            self.context.restart_after_install = restart_after_install;
        }
        self
    }

    /// Withdraws a requested Windows restart once `cancelled` is set.
    ///
    /// The flag is read when the installer is launched, so a request to close
    /// the app that arrives during extraction stops the installer from
    /// relaunching it.
    #[cfg_attr(not(windows), allow(unused))]
    pub fn restart_cancelled_by(mut self, cancelled: Arc<AtomicBool>) -> Self {
        #[cfg(windows)]
        {
            self.context.restart_cancelled = Some(cancelled);
        }
        self
    }

    /// Uses `gate` to decide whether this installation may still replace the
    /// installed app. See [`CommitGate`].
    pub fn commit_gate(mut self, gate: Arc<CommitGate>) -> Self {
        self.context.commit_gate = gate;
        self
    }

    /// Records in `report` staging folders this installation could not delete,
    /// except after a refused exchange, which reports its folder in the error.
    /// See [`CleanupReport`].
    pub fn cleanup_report(mut self, report: Arc<CleanupReport>) -> Self {
        self.context.cleanup_report = report;
        self
    }
}

/// Whether the Windows installer relaunches the app: requested, and not
/// withdrawn since.
#[cfg_attr(not(windows), allow(dead_code))]
fn relaunch_after_install(requested: bool, cancelled: Option<&AtomicBool>) -> bool {
    requested && !cancelled.is_some_and(|flag| flag.load(Ordering::SeqCst))
}

#[cfg(windows)]
enum WindowsUpdaterType {
    Nsis {
        path: PathBuf,
        #[allow(unused)]
        temp: Option<tempfile::TempPath>,
    },
    Msi {
        path: PathBuf,
        #[allow(unused)]
        temp: Option<tempfile::TempPath>,
    },
}

#[cfg(windows)]
impl WindowsUpdaterType {
    fn nsis(path: PathBuf, temp: Option<tempfile::TempPath>) -> Self {
        Self::Nsis { path, temp }
    }

    fn msi(path: PathBuf, temp: Option<tempfile::TempPath>) -> Self {
        Self::Msi {
            path: path.wrap_in_quotes(),
            temp,
        }
    }
}

#[cfg(windows)]
impl Config {
    fn install_mode(&self) -> crate::config::WindowsUpdateInstallMode {
        self.windows
            .as_ref()
            .map(|w| w.install_mode.clone())
            .unwrap_or_default()
    }
}

/// Windows
#[cfg(windows)]
impl Update {
    /// ### Expected structure:
    /// ├── [AppName]_[version]_x64.msi              # Application MSI
    /// ├── [AppName]_[version]_x64-setup.exe        # NSIS installer
    /// ├── [AppName]_[version]_x64.msi.zip          # ZIP generated by tauri-bundler
    /// │   └──[AppName]_[version]_x64.msi           # Application MSI
    /// ├── [AppName]_[version]_x64-setup.exe.zip          # ZIP generated by tauri-bundler
    /// │   └──[AppName]_[version]_x64-setup.exe           # NSIS installer
    /// └── ...
    fn install_inner(&self, bytes: &[u8]) -> Result<()> {
        use windows_sys::{
            w,
            Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOW},
        };

        let updater_type = self.extract(bytes)?;

        if let Some(on_before_exit) = self.context.on_before_exit.as_ref() {
            log::debug!("running on_before_exit hook");
            on_before_exit();
        }

        // From here the installer takes over; an abandoned update stops before it.
        let _committing = self.context.commit_gate.enter()?;

        let file = match &updater_type {
            WindowsUpdaterType::Nsis { path, .. } => path.as_os_str().to_os_string(),
            WindowsUpdaterType::Msi { .. } => std::env::var("SYSTEMROOT").as_ref().map_or_else(
                |_| OsString::from("msiexec.exe"),
                |p| OsString::from(format!("{p}\\System32\\msiexec.exe")),
            ),
        };
        let parameters = self.updater_parameters(&updater_type);

        log::debug!("Executing updater {file:?} with parameters: {parameters:?}");

        let file = encode_wide(file);
        let parameters = encode_wide(parameters);

        let result = unsafe {
            ShellExecuteW(
                std::ptr::null_mut(),
                w!("open"),
                file.as_ptr(),
                parameters.as_ptr(),
                std::ptr::null(),
                SW_SHOW,
            )
        };
        if result as isize <= 32 {
            return Err(crate::Error::Io(std::io::Error::last_os_error()));
        }

        std::process::exit(0);
    }

    fn updater_parameters(&self, updater_type: &WindowsUpdaterType) -> OsString {
        let install_mode = self.context.config.install_mode();
        let current_args = &self.context.current_exe_args[1..];
        let restart = relaunch_after_install(
            self.context.restart_after_install,
            self.context.restart_cancelled.as_deref(),
        );

        match updater_type {
            WindowsUpdaterType::Nsis { .. } => {
                let mut installer_args: Vec<&OsStr> = Vec::new();
                installer_args.extend(install_mode.nsis_args().iter().map(OsStr::new));
                installer_args.push(OsStr::new("/UPDATE"));

                let nsis_current_exe_arg;
                if restart {
                    nsis_current_exe_arg = current_args
                        .iter()
                        .map(escape_nsis_current_exe_arg)
                        .collect::<Vec<_>>();

                    installer_args.extend(
                        install_mode
                            .nsis_restart_after_install_args()
                            .iter()
                            .map(OsStr::new),
                    );
                    installer_args.push(OsStr::new("/ARGS"));
                    installer_args.extend(nsis_current_exe_arg.iter().map(OsStr::new));
                }

                installer_args.extend(self.installer_args());

                installer_args.join(OsStr::new(" "))
            }
            WindowsUpdaterType::Msi { path, .. } => {
                let mut installer_args: Vec<&OsStr> = vec![OsStr::new("/i"), path.as_os_str()];
                installer_args.extend(install_mode.msiexec_args().iter().map(OsStr::new));
                installer_args.push(OsStr::new("/promptrestart"));
                installer_args.extend(self.installer_args());

                let msi_current_exe_arg;
                if restart {
                    msi_current_exe_arg = format!(
                        "LAUNCHAPPARGS=\"{}\"",
                        current_args
                            .iter()
                            .map(escape_msi_property_arg)
                            .collect::<Vec<_>>()
                            .join(" ")
                    );

                    installer_args.extend(
                        install_mode
                            .msi_restart_after_install_args()
                            .iter()
                            .map(OsStr::new),
                    );
                    installer_args.push(OsStr::new(&msi_current_exe_arg));
                }

                installer_args.join(OsStr::new(" "))
            }
        }
    }

    fn installer_args(
        &self,
    ) -> std::iter::Map<std::slice::Iter<'_, OsString>, fn(&OsString) -> &OsStr> {
        self.context.installer_args.iter().map(OsStr::new)
    }

    fn extract(&self, bytes: &[u8]) -> Result<WindowsUpdaterType> {
        #[cfg(feature = "zip")]
        if infer::archive::is_zip(bytes) {
            return self.extract_zip(bytes);
        }

        self.extract_exe(bytes)
    }

    fn make_temp_dir(&self) -> Result<PathBuf> {
        Ok(tempfile::Builder::new()
            .prefix(&format!(
                "{}-{}-updater-",
                self.context.app_name, self.version
            ))
            .tempdir()?
            .keep())
    }

    #[cfg(feature = "zip")]
    fn extract_zip(&self, bytes: &[u8]) -> Result<WindowsUpdaterType> {
        let temp_dir = self.make_temp_dir()?;

        let archive = Cursor::new(bytes);
        let mut extractor = zip::ZipArchive::new(archive)?;
        extractor.extract(&temp_dir)?;

        let paths = std::fs::read_dir(&temp_dir)?;
        for path in paths {
            let path = path?.path();
            let ext = path.extension();
            if ext == Some(OsStr::new("exe")) {
                return Ok(WindowsUpdaterType::nsis(path, None));
            } else if ext == Some(OsStr::new("msi")) {
                return Ok(WindowsUpdaterType::msi(path, None));
            }
        }

        Err(crate::Error::BinaryNotFoundInArchive)
    }

    fn extract_exe(&self, bytes: &[u8]) -> Result<WindowsUpdaterType> {
        if infer::app::is_exe(bytes) {
            let (path, temp) = self.write_to_temp(bytes, ".exe")?;
            Ok(WindowsUpdaterType::nsis(path, temp))
        } else if infer::archive::is_msi(bytes) {
            let (path, temp) = self.write_to_temp(bytes, ".msi")?;
            Ok(WindowsUpdaterType::msi(path, temp))
        } else {
            Err(crate::Error::InvalidUpdaterFormat)
        }
    }

    fn write_to_temp(
        &self,
        bytes: &[u8],
        ext: &str,
    ) -> Result<(PathBuf, Option<tempfile::TempPath>)> {
        use std::io::Write;

        let temp_dir = self.make_temp_dir()?;
        let mut temp_file = tempfile::Builder::new()
            .prefix(&format!(
                "{}-{}-installer",
                self.context.app_name, self.version
            ))
            .suffix(ext)
            .rand_bytes(0)
            .tempfile_in(temp_dir)?;
        temp_file.write_all(bytes)?;

        let temp = temp_file.into_temp_path();
        Ok((temp.to_path_buf(), Some(temp)))
    }
}

/// Linux (AppImage, Deb, RPM)
#[cfg(any(
    target_os = "linux",
    target_os = "dragonfly",
    target_os = "freebsd",
    target_os = "netbsd",
    target_os = "openbsd"
))]
impl Update {
    /// ### Expected structure:
    /// ├── [AppName]_[version]_amd64.AppImage.tar.gz    # GZ generated by tauri-bundler
    /// │   └──[AppName]_[version]_amd64.AppImage        # Application AppImage
    /// ├── [AppName]_[version]_amd64.deb                # Debian package
    /// ├── [AppName]_[version]_amd64.rpm                # RPM package
    /// └── ...
    ///
    fn install_inner(&self, bytes: &[u8]) -> Result<()> {
        match installer_for_bundle_type(bundle_type()) {
            Some(Installer::Deb) => self.install_deb(bytes),
            Some(Installer::Rpm) => self.install_rpm(bytes),
            _ => self.install_appimage(bytes),
        }
    }

    fn install_appimage(&self, bytes: &[u8]) -> Result<()> {
        let dest = &self.extract_path;
        let (parent, name) = replace::parent_and_name(dest)?;
        let installed = dest.metadata()?;

        // Stage the new AppImage beside the installed file. Replacing it needs write access to that
        // folder anyway, and it makes the replacement a rename on one filesystem.
        let staging = tempfile::Builder::new()
            .prefix(&replace::staging_prefix(name))
            .tempdir_in(parent)?;
        let staged = staging.path().join(name);
        let attempt = self.stage_and_replace_appimage(bytes, &staged, dest, &installed);
        // Delete the staging folder here rather than on drop, which discards deletion errors.
        // A folder that remains is recorded for the application; the result stands.
        replace::close_staging(
            &replace::StdFs,
            &staging.keep(),
            attempt,
            &self.context.cleanup_report,
        )?;
        Ok(())
    }

    /// Stages the new AppImage at `staged` and renames it onto `dest`.
    fn stage_and_replace_appimage(
        &self,
        bytes: &[u8],
        staged: &Path,
        dest: &Path,
        installed: &std::fs::Metadata,
    ) -> Result<replace::Replaced> {
        use std::os::unix::fs::MetadataExt;

        let staging = staged.parent().ok_or(Error::TempDirNotFound)?;
        if staging.metadata()?.dev() != installed.dev() {
            return Err(Error::TempDirNotOnSameMountPoint);
        }
        Self::stage_appimage(bytes, staged, installed.permissions())?;

        // One rename onto the installed name: it names the previous or the new AppImage at
        // every moment, so an exit at any point still leaves an AppImage to launch.
        replace::replace_file(&replace::StdFs, &self.context.commit_gate, staged, dest)
            .map_err(Error::from)
    }

    /// Writes the new AppImage to `staged`, leaving the installed one untouched.
    fn stage_appimage(
        bytes: &[u8],
        staged: &Path,
        permissions: std::fs::Permissions,
    ) -> Result<()> {
        use std::io::Write;

        #[cfg(feature = "zip")]
        if infer::archive::is_gz(bytes) {
            log::debug!("extracting AppImage");
            let archive = Cursor::new(bytes);
            let decoder = flate2::read::GzDecoder::new(archive);
            let mut archive = tar::Archive::new(decoder);
            for mut entry in archive.entries()?.flatten() {
                if let Ok(path) = entry.path() {
                    if path.extension() == Some(OsStr::new("AppImage")) {
                        entry.unpack(staged)?;
                        std::fs::File::open(staged)?.sync_all()?;
                        return Ok(());
                    }
                }
            }
            return Err(Error::BinaryNotFoundInArchive);
        }

        log::debug!("rewriting AppImage");
        let mut file = std::fs::File::create(staged)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        std::fs::set_permissions(staged, permissions)?;
        Ok(())
    }

    fn install_deb(&self, bytes: &[u8]) -> Result<()> {
        // First verify the bytes are actually a .deb package
        if !infer::archive::is_deb(bytes) {
            log::warn!("update is not a valid deb package");
            return Err(Error::InvalidUpdaterFormat);
        }

        self.try_tmp_locations(bytes, "dpkg", "-i", "deb")
    }

    fn install_rpm(&self, bytes: &[u8]) -> Result<()> {
        // First verify the bytes are actually a .rpm package
        if !infer::archive::is_rpm(bytes) {
            return Err(Error::InvalidUpdaterFormat);
        }
        self.try_tmp_locations(bytes, "rpm", "-U", "rpm")
    }

    fn try_tmp_locations(
        &self,
        bytes: &[u8],
        install_cmd: &str,
        install_arg: &str,
        package_extension: &str,
    ) -> Result<()> {
        // Try different temp directories
        let tmp_dir_locations = vec![
            Box::new(|| Some(std::env::temp_dir())) as Box<dyn FnOnce() -> Option<PathBuf>>,
            Box::new(dirs::cache_dir),
            Box::new(|| Some(self.extract_path.parent().unwrap().to_path_buf())),
        ];

        // Try writing to multiple temp locations until one succeeds
        for tmp_dir_location in tmp_dir_locations {
            if let Some(path) = tmp_dir_location() {
                let prefix = format!("tauri_{package_extension}_update");
                if let Ok(tmp_dir) = tempfile::Builder::new().prefix(&prefix).tempdir_in(path) {
                    let pkg_path = tmp_dir.path().join(format!("package.{package_extension}"));

                    // Try writing the .deb / .rpm file
                    if std::fs::write(&pkg_path, bytes).is_ok() {
                        // If write succeeds, proceed with installation
                        return self.try_install_with_privileges(
                            &pkg_path,
                            install_cmd,
                            install_arg,
                        );
                    }
                    // If write fails, continue to next temp location
                }
            }
        }

        // If we get here, all temp locations failed
        Err(Error::TempDirNotFound)
    }

    fn try_install_with_privileges(
        &self,
        pkg_path: &Path,
        install_cmd: &str,
        install_arg: &str,
    ) -> Result<()> {
        // 1. First try using pkexec (graphical sudo prompt)
        if let Ok(status) = std::process::Command::new("pkexec")
            .arg(install_cmd)
            .arg(install_arg)
            .arg(pkg_path)
            .status()
        {
            if status.success() {
                log::debug!("installed {pkg_path:?} with pkexec");
                return Ok(());
            }
        }

        // 2. Try zenity or kdialog for a graphical sudo experience
        if let Ok(password) = self.get_password_graphically() {
            if self.install_with_sudo(pkg_path, &password, install_cmd, install_arg)? {
                log::debug!("installed {pkg_path:?} with GUI sudo");
                return Ok(());
            }
        }

        // 3. Final fallback: terminal sudo
        let status = std::process::Command::new("sudo")
            .arg(install_cmd)
            .arg(install_arg)
            .arg(pkg_path)
            .status()?;

        if status.success() {
            log::debug!("installed {pkg_path:?} with sudo");
            Ok(())
        } else {
            Err(Error::PackageInstallFailed)
        }
    }

    fn get_password_graphically(&self) -> Result<String> {
        // Try zenity first
        let zenity_result = std::process::Command::new("zenity")
            .args([
                "--password",
                "--title=Authentication Required",
                "--text=Enter your password to install the update:",
            ])
            .output();

        if let Ok(output) = zenity_result {
            if output.status.success() {
                return Ok(String::from_utf8_lossy(&output.stdout).trim().to_string());
            }
        }

        // Fall back to kdialog if zenity fails or isn't available
        let kdialog_result = std::process::Command::new("kdialog")
            .args(["--password", "Enter your password to install the update:"])
            .output();

        if let Ok(output) = kdialog_result {
            if output.status.success() {
                return Ok(String::from_utf8_lossy(&output.stdout).trim().to_string());
            }
        }

        Err(Error::AuthenticationFailed)
    }

    fn install_with_sudo(
        &self,
        pkg_path: &Path,
        password: &str,
        install_cmd: &str,
        install_arg: &str,
    ) -> Result<bool> {
        use std::io::Write;
        use std::process::{Command, Stdio};

        let mut child = Command::new("sudo")
            .arg("-S") // read password from stdin
            .arg(install_cmd)
            .arg(install_arg)
            .arg(pkg_path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;

        if let Some(mut stdin) = child.stdin.take() {
            // Write password to stdin
            writeln!(stdin, "{password}")?;
        }

        let status = child.wait()?;
        Ok(status.success())
    }
}

/// MacOS
#[cfg(target_os = "macos")]
impl Update {
    /// ### Expected structure:
    /// ├── [AppName]_[version]_x64.app.tar.gz       # GZ generated by tauri-bundler
    /// │   └──[AppName].app                         # Main application
    /// │      └── Contents                          # Application contents...
    /// │          └── ...
    /// └── ...
    fn install_inner(&self, bytes: &[u8]) -> Result<()> {
        let dest = &self.extract_path;
        let (parent, name) = replace::parent_and_name(dest)?;

        // Stage beside the installed app so that every move is a rename on one volume. There is
        // no administrator-assisted path: moving a copy this account can still change into a
        // folder it can't would hand the administrator unverified bytes. An app in a folder this
        // account can't change stops here, before anything moves, and is updated by hand.
        let staging = tempfile::Builder::new()
            .prefix(&replace::staging_prefix(name))
            .tempdir_in(parent)?;
        let staged = staging.path().join(name);
        // Exchange the two bundles in one step, so the installed path always holds a complete
        // app. A volume that cannot do that is refused.
        let attempt = Self::extract_app(bytes, &staged).and_then(|()| {
            replace::replace_bundle(&replace::StdFs, &self.context.commit_gate, &staged, dest)
                .map_err(Error::from)
        });
        // Delete the staging folder here rather than on drop, which discards deletion errors.
        // If cleanup fails after a refusal, report the unused staging folder's path.
        replace::close_staging(
            &replace::StdFs,
            &staging.keep(),
            attempt,
            &self.context.cleanup_report,
        )?;
        Self::touch(dest);
        Ok(())
    }

    /// Extracts the app bundle in the archive to `staged`.
    fn extract_app(bytes: &[u8], staged: &Path) -> Result<()> {
        use flate2::read::GzDecoder;
        use std::path::Component;

        let mut archive = tar::Archive::new(GzDecoder::new(Cursor::new(bytes)));
        for entry in archive.entries()? {
            let mut entry = entry?;
            // Drop the bundle's own folder name; the bundle is staged under the installed name.
            let relative: PathBuf = entry.path()?.iter().skip(1).collect();
            if relative
                .components()
                .any(|part| !matches!(part, Component::Normal(_)))
            {
                return Err(Error::InvalidUpdaterFormat);
            }
            let path = staged.join(&relative);
            if let Some(folder) = path.parent() {
                std::fs::create_dir_all(folder)?;
            }
            entry.unpack(&path)?;
        }
        if !staged.is_dir() {
            return Err(Error::BinaryNotFoundInArchive);
        }
        Ok(())
    }

    fn touch(dest: &Path) {
        let _ = std::process::Command::new("touch").arg(dest).status();
    }
}

/// Gets the base target string used by the updater. If bundle type is available it
/// will be added to this string when selecting the download URL and signature.
/// `tauri::utils::platform::bundle_type` method is used to obtain current bundle type.
pub fn target() -> Option<String> {
    if let (Some(target), Some(arch)) = (updater_os(), updater_arch()) {
        Some(format!("{target}-{arch}"))
    } else {
        None
    }
}

fn updater_os() -> Option<&'static str> {
    if cfg!(target_os = "linux") {
        Some("linux")
    } else if cfg!(target_os = "macos") {
        // TODO shouldn't this be macos instead?
        Some("darwin")
    } else if cfg!(target_os = "windows") {
        Some("windows")
    } else {
        None
    }
}

fn updater_arch() -> Option<&'static str> {
    if cfg!(target_arch = "x86") {
        Some("i686")
    } else if cfg!(target_arch = "x86_64") {
        Some("x86_64")
    } else if cfg!(target_arch = "arm") {
        Some("armv7")
    } else if cfg!(target_arch = "aarch64") {
        Some("aarch64")
    } else if cfg!(target_arch = "riscv64") {
        Some("riscv64")
    } else {
        None
    }
}

pub fn extract_path_from_executable(executable_path: &Path) -> Result<PathBuf> {
    // Return the path of the current executable by default
    // Example C:\Program Files\My App\
    let extract_path = executable_path
        .parent()
        .map(PathBuf::from)
        .ok_or(Error::FailedToDetermineExtractPath)?;

    // MacOS example binary is in /Applications/TestApp.app/Contents/MacOS/myApp
    // We need to get /Applications/<app>.app
    // TODO(lemarier): Need a better way here
    // Maybe we could search for <*.app> to get the right path
    #[cfg(target_os = "macos")]
    if extract_path
        .display()
        .to_string()
        .contains("Contents/MacOS")
    {
        return extract_path
            .parent()
            .map(PathBuf::from)
            .ok_or(Error::FailedToDetermineExtractPath)?
            .parent()
            .map(PathBuf::from)
            .ok_or(Error::FailedToDetermineExtractPath);
    }

    Ok(extract_path)
}

impl<'de> Deserialize<'de> for RemoteRelease {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        #[derive(Deserialize)]
        struct InnerRemoteRelease {
            #[serde(alias = "name", deserialize_with = "parse_version")]
            version: Version,
            notes: Option<String>,
            pub_date: Option<String>,
            platforms: Option<HashMap<String, ReleaseManifestPlatform>>,
            // dynamic platform response
            url: Option<Url>,
            signature: Option<String>,
        }

        let release = InnerRemoteRelease::deserialize(deserializer)?;

        let pub_date = if let Some(date) = release.pub_date {
            Some(
                OffsetDateTime::parse(&date, &time::format_description::well_known::Rfc3339)
                    .map_err(|e| DeError::custom(format!("invalid value for `pub_date`: {e}")))?,
            )
        } else {
            None
        };

        Ok(RemoteRelease {
            version: release.version,
            notes: release.notes,
            pub_date,
            data: if let Some(platforms) = release.platforms {
                RemoteReleaseInner::Static { platforms }
            } else {
                RemoteReleaseInner::Dynamic(ReleaseManifestPlatform {
                    url: release.url.ok_or_else(|| {
                        DeError::custom("the `url` field was not set on the updater response")
                    })?,
                    signature: release.signature.ok_or_else(|| {
                        DeError::custom("the `signature` field was not set on the updater response")
                    })?,
                })
            },
        })
    }
}

fn installer_for_bundle_type(bundle: Option<BundleType>) -> Option<Installer> {
    match bundle? {
        BundleType::Deb => Some(Installer::Deb),
        BundleType::Rpm => Some(Installer::Rpm),
        BundleType::AppImage => Some(Installer::AppImage),
        BundleType::Msi => Some(Installer::Msi),
        BundleType::Nsis => Some(Installer::Nsis),
        BundleType::App => Some(Installer::App), // App is also returned for Dmg type
        _ => None,
    }
}

fn parse_version<'de, D>(deserializer: D) -> std::result::Result<Version, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let str = String::deserialize(deserializer)?;

    Version::from_str(str.trim_start_matches('v')).map_err(serde::de::Error::custom)
}

// Validate signature
fn verify_signature(data: &[u8], release_signature: &str, pub_key: &str) -> Result<()> {
    // we need to convert the pub key
    let pub_key_decoded = base64_to_string(pub_key)?;
    let public_key = PublicKey::decode(&pub_key_decoded)?;
    let signature_base64_decoded = base64_to_string(release_signature)?;
    let signature = Signature::decode(&signature_base64_decoded)?;

    // Validate signature or bail out
    public_key.verify(data, &signature, true)?;
    Ok(())
}

fn base64_to_string(base64_string: &str) -> Result<String> {
    let decoded_string = &base64::engine::general_purpose::STANDARD.decode(base64_string)?;
    let result = std::str::from_utf8(decoded_string)
        .map_err(|_| Error::SignatureUtf8(base64_string.into()))?
        .to_string();
    Ok(result)
}

#[cfg(windows)]
fn encode_wide(string: impl AsRef<OsStr>) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;

    string
        .as_ref()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

#[cfg(windows)]
trait PathExt {
    fn wrap_in_quotes(&self) -> Self;
}

#[cfg(windows)]
impl PathExt for PathBuf {
    fn wrap_in_quotes(&self) -> Self {
        let mut msi_path = OsString::from("\"");
        msi_path.push(self.as_os_str());
        msi_path.push("\"");
        PathBuf::from(msi_path)
    }
}

// adapted from https://github.com/rust-lang/rust/blob/1c047506f94cd2d05228eb992b0a6bbed1942349/library/std/src/sys/args/windows.rs#L174
#[cfg(windows)]
fn escape_nsis_current_exe_arg(arg: impl AsRef<OsStr>) -> OsString {
    use std::os::windows::ffi::{OsStrExt, OsStringExt};

    let arg = arg.as_ref();
    let mut cmd: Vec<u16> = Vec::new();

    // compared to std we additionally escape `/` so that nsis won't interpret them as a beginning of an nsis argument.
    let quote = arg
        .as_encoded_bytes()
        .iter()
        .any(|c| *c == b' ' || *c == b'\t' || *c == b'/')
        || arg.is_empty();
    let escape = true;
    if quote {
        cmd.push('"' as u16);
    }
    let mut backslashes: usize = 0;
    for x in arg.encode_wide() {
        if escape {
            if x == '\\' as u16 {
                backslashes += 1;
            } else {
                if x == '"' as u16 {
                    // Add n+1 backslashes to total 2n+1 before internal '"'.
                    cmd.extend((0..=backslashes).map(|_| '\\' as u16));
                }
                backslashes = 0;
            }
        }
        cmd.push(x);
    }
    if quote {
        // Add n backslashes to total 2n before ending '"'.
        cmd.extend((0..backslashes).map(|_| '\\' as u16));
        cmd.push('"' as u16);
    }
    OsString::from_wide(&cmd)
}

#[cfg(windows)]
fn escape_msi_property_arg(arg: impl AsRef<OsStr>) -> String {
    let mut arg = arg.as_ref().to_string_lossy().to_string();

    // Otherwise this argument will get lost in ShellExecute
    if arg.is_empty() {
        return "\"\"\"\"".to_string();
    } else if !arg.contains(' ') && !arg.contains('"') {
        return arg;
    }

    if arg.contains('"') {
        arg = arg.replace('"', r#""""""#);
    }

    if arg.starts_with('-') {
        if let Some((a1, a2)) = arg.split_once('=') {
            format!("{a1}=\"\"{a2}\"\"")
        } else {
            format!("\"\"{arg}\"\"")
        }
    } else {
        format!("\"\"{arg}\"\"")
    }
}

#[cfg(test)]
mod tests {

    #[test]
    fn a_withdrawn_restart_stops_the_installer_relaunching_the_app() {
        use super::relaunch_after_install;
        use std::sync::atomic::AtomicBool;

        assert!(relaunch_after_install(true, None));
        assert!(!relaunch_after_install(false, None));
        let cancelled = AtomicBool::new(false);
        assert!(relaunch_after_install(true, Some(&cancelled)));
        cancelled.store(true, std::sync::atomic::Ordering::SeqCst);
        assert!(!relaunch_after_install(true, Some(&cancelled)));
        assert!(!relaunch_after_install(false, Some(&cancelled)));
    }

    #[test]
    #[cfg(windows)]
    fn it_wraps_correctly() {
        use super::PathExt;
        use std::path::PathBuf;

        assert_eq!(
            PathBuf::from("C:\\Users\\Some User\\AppData\\tauri-example.exe").wrap_in_quotes(),
            PathBuf::from("\"C:\\Users\\Some User\\AppData\\tauri-example.exe\"")
        )
    }

    #[test]
    #[cfg(windows)]
    fn it_escapes_correctly_for_msi() {
        use crate::updater::escape_msi_property_arg;

        // Explanation for quotes:
        // The output of escape_msi_property_args() will be used in `LAUNCHAPPARGS=\"{HERE}\"`. This is the first quote level.
        // To escape a quotation mark we use a second quotation mark, so "" is interpreted as " later.
        // This means that the escaped strings can't ever have a single quotation mark!
        // Now there are 3 major things to look out for to not break the msiexec call:
        //   1) Wrap spaces in quotation marks, otherwise it will be interpreted as the end of the msiexec argument.
        //   2) Escape escaping quotation marks, otherwise they will either end the msiexec argument or be ignored.
        //   3) Escape emtpy args in quotation marks, otherwise the argument will get lost.
        let cases = [
            "something",
            "--flag",
            "--empty=",
            "--arg=value",
            "some space",                     // This simulates `./my-app "some string"`.
            "--arg value", // -> This simulates `./my-app "--arg value"`. Same as above but it triggers the startsWith(`-`) logic.
            "--arg=unwrapped space", // `./my-app --arg="unwrapped space"`
            "--arg=\"wrapped\"", // `./my-app --args=""wrapped""`
            "--arg=\"wrapped space\"", // `./my-app --args=""wrapped space""`
            "--arg=midword\"wrapped space\"", // `./my-app --args=midword""wrapped""`
            "",            // `./my-app '""'`
        ];
        let cases_escaped = [
            "something",
            "--flag",
            "--empty=",
            "--arg=value",
            "\"\"some space\"\"",
            "\"\"--arg value\"\"",
            "--arg=\"\"unwrapped space\"\"",
            r#"--arg=""""""wrapped"""""""#,
            r#"--arg=""""""wrapped space"""""""#,
            r#"--arg=""midword""""wrapped space"""""""#,
            "\"\"\"\"",
        ];

        // Just to be sure we didn't mess that up
        assert_eq!(cases.len(), cases_escaped.len());

        for (orig, escaped) in cases.iter().zip(cases_escaped) {
            assert_eq!(escape_msi_property_arg(orig), escaped);
        }
    }

    #[test]
    #[cfg(windows)]
    fn it_escapes_correctly_for_nsis() {
        use crate::updater::escape_nsis_current_exe_arg;
        use std::ffi::OsStr;

        let cases = [
            "something",
            "--flag",
            "--empty=",
            "--arg=value",
            "some space",                     // This simulates `./my-app "some string"`.
            "--arg value", // -> This simulates `./my-app "--arg value"`. Same as above but it triggers the startsWith(`-`) logic.
            "--arg=unwrapped space", // `./my-app --arg="unwrapped space"`
            "--arg=\"wrapped\"", // `./my-app --args=""wrapped""`
            "--arg=\"wrapped space\"", // `./my-app --args=""wrapped space""`
            "--arg=midword\"wrapped space\"", // `./my-app --args=midword""wrapped""`
            "",            // `./my-app '""'`
        ];
        // Note: These may not be the results we actually want (monitor this!).
        // We only make sure the implementation doesn't unintentionally change.
        let cases_escaped = [
            "something",
            "--flag",
            "--empty=",
            "--arg=value",
            "\"some space\"",
            "\"--arg value\"",
            "\"--arg=unwrapped space\"",
            "--arg=\\\"wrapped\\\"",
            "\"--arg=\\\"wrapped space\\\"\"",
            "\"--arg=midword\\\"wrapped space\\\"\"",
            "\"\"",
        ];

        // Just to be sure we didn't mess that up
        assert_eq!(cases.len(), cases_escaped.len());

        for (orig, escaped) in cases.iter().zip(cases_escaped) {
            assert_eq!(escape_nsis_current_exe_arg(&OsStr::new(orig)), escaped);
        }
    }
}

#[cfg(all(test, target_os = "linux"))]
mod platefold_tests {
    use super::*;
    use std::{
        io::{Cursor, Read, Write},
        net::TcpListener,
        thread,
    };

    fn updater() -> Updater {
        Updater {
            current_version: Version::parse("1.0.0").unwrap(),
            version_comparator: None,
            timeout: Some(Duration::from_secs(2)),
            proxy: None,
            no_proxy: true,
            endpoints: vec![Url::parse("http://127.0.0.1:1/must-not-be-fetched").unwrap()],
            arch: "x86_64",
            target: Some("linux-x86_64-appimage".into()),
            headers: Default::default(),
            extract_path: "/tmp/never-installed-by-this-test".into(),
            context: UpdaterContext {
                config: Config::default(),
                configure_client: Some(Arc::new(|client| {
                    client.redirect(reqwest::redirect::Policy::none())
                })),
                commit_gate: Arc::new(CommitGate::new()),
                cleanup_report: Arc::new(CleanupReport::new()),
            },
        }
    }

    fn metadata(url: &str, signature: &str) -> serde_json::Value {
        serde_json::json!({"version":"1.1.0", "platforms": {
            "linux-x86_64-appimage": {"url":url, "signature":signature}
        }})
    }

    // PlateFold binds each artifact's release into the signature's trusted
    // comment and checks it itself; this check must refuse a rewritten comment.
    #[test]
    fn a_rewritten_trusted_comment_fails_signature_verification() {
        use base64::Engine as _;
        let engine = base64::engine::general_purpose::STANDARD;
        let keys = minisign::KeyPair::generate_unencrypted_keypair().unwrap();
        let bytes = b"synthetic updater artifact";
        let comment = "platefold-artifact v1 version=1.0.0 platform=linux-x86_64-appimage file=a";
        let document = minisign::sign(None, &keys.sk, Cursor::new(bytes), Some(comment), None)
            .unwrap()
            .to_string();
        let key = engine.encode(keys.pk.to_box().unwrap().to_string());
        assert!(verify_signature(bytes, &engine.encode(&document), &key).is_ok());
        let forged = document.replace("version=1.0.0", "version=1.1.0");
        assert_ne!(forged, document);
        assert!(verify_signature(bytes, &engine.encode(forged), &key).is_err());
    }

    #[test]
    fn authenticated_metadata_constructs_update_without_contacting_the_endpoint() {
        let updater = updater();
        let json = metadata("https://example.invalid/update", "signature");
        let update = updater.check_from_json(json.clone()).unwrap().unwrap();
        assert_eq!(update.raw_json, json);
        assert_eq!(update.version, "1.1.0");
        assert_eq!(update.timeout, Some(Duration::from_secs(2)));
        assert_eq!(update.signature, "signature");
        let mut older = json.clone();
        older["version"] = serde_json::json!("0.9.0");
        assert!(updater.check_from_json(older).unwrap().is_none());
        let mut wrong_target = json;
        wrong_target["platforms"] = serde_json::json!({});
        assert!(updater.check_from_json(wrong_target).is_err());
    }

    #[test]
    fn appimage_install_replaces_the_file_through_the_backup_and_leaves_nothing_behind() {
        use std::os::unix::fs::PermissionsExt;
        let folder = tempfile::tempdir().unwrap();
        let app = folder.path().join("PlateFold.AppImage");
        std::fs::write(&app, "old").unwrap();
        std::fs::set_permissions(&app, std::fs::Permissions::from_mode(0o750)).unwrap();
        let mut updater = updater();
        updater.extract_path = app.clone();
        let update = updater
            .check_from_json(metadata("https://example.invalid/update", "signature"))
            .unwrap()
            .unwrap();
        // A test build has no bundle marker, so Linux installs through the AppImage path.
        update.install(b"new").unwrap();
        assert_eq!(std::fs::read(&app).unwrap(), b"new");
        let mode = std::fs::metadata(&app).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o750);
        let left: Vec<_> = std::fs::read_dir(folder.path())
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect();
        assert_eq!(left, [OsString::from("PlateFold.AppImage")]);
    }

    fn download_response(
        raw: Vec<u8>,
        limit: u64,
        key: String,
        signature: &str,
    ) -> Result<Vec<u8>> {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/artifact", listener.local_addr().unwrap());
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let _ = socket.read(&mut [0; 4096]);
            let _ = socket.write_all(&raw);
        });
        let mut updater = updater();
        updater.context.config.pubkey = key;
        let update = updater
            .check_from_json(metadata(&url, signature))
            .unwrap()
            .unwrap();
        let result =
            tauri::async_runtime::block_on(update.download_bounded(limit, |_, _| {}, || {}));
        server.join().unwrap();
        result
    }

    #[test]
    fn bounded_download_preserves_signature_verification_and_refuses_redirects_and_oversize() {
        let keys = minisign::KeyPair::generate_unencrypted_keypair().unwrap();
        let bytes = b"synthetic updater artifact";
        let signature = base64::engine::general_purpose::STANDARD.encode(
            minisign::sign(None, &keys.sk, Cursor::new(bytes), None, None)
                .unwrap()
                .to_string(),
        );
        let key =
            base64::engine::general_purpose::STANDARD.encode(keys.pk.to_box().unwrap().to_string());
        let mut valid = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            bytes.len()
        )
        .into_bytes();
        valid.extend(bytes);
        assert_eq!(
            download_response(valid.clone(), bytes.len() as u64, key.clone(), &signature).unwrap(),
            bytes
        );
        assert!(
            download_response(valid, bytes.len() as u64, key.clone(), "wrong-signature").is_err()
        );
        for raw in [
            b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/never-follow\r\nContent-Length: 0\r\n\r\n".to_vec(),
            b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n".to_vec(),
            b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nresponse without length exceeds bound".to_vec(),
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n20\r\n01234567890123456789012345678901\r\n0\r\n\r\n".to_vec(),
            b"HTTP/1.1 200 OK\r\nContent-Length: 19\r\n\r\nshort".to_vec(),
        ] {
            let error = download_response(raw, 19, key.clone(), &signature).unwrap_err().to_string();
            assert!(error.contains("302") || error.contains("size limit") || error.contains("body"), "{error}");
        }
    }
}
