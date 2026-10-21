use super::{
    binding, decode_artifact_signature, https_url, release_page, Channel, MAX_ARTIFACT, MAX_FEED,
};
use minisign_verify::{PublicKey, Signature};
use semver::Version;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Envelope {
    // Exact UTF-8 bytes, not reserialised JSON. Every release claim is signed.
    payload: String,
    // This signature authenticates payload. The per-platform signatures below
    // are the Tauri updater signatures over the artifact bytes.
    signature: String,
    version: String,
    notes: String,
    platforms: BTreeMap<String, InstallerArtifact>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct InstallerArtifact {
    url: String,
    signature: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    schema_version: u32,
    version: String,
    channel: Channel,
    published: bool,
    issued_at: u64,
    expires_at: u64,
    notes: String,
    artifacts: BTreeMap<String, Artifact>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Artifact {
    pub(super) url: String,
    pub(super) sha256: String,
    pub(super) size: u64,
    // Tauri's .sig file content: base64 of a Minisign signature document. Its
    // trusted comment names the release version, target and file (binding.rs).
    pub(super) signature: String,
    // A raw Minisign public-key line pinned by artifact_public_keys.
    pub(super) public_key: String,
}
#[derive(Clone, Debug, Serialize)]
pub struct Release {
    pub version: String,
    pub notes: String,
    // None when a package manager installed this copy and the release lists no
    // artifact for its target: the user downloads the next package instead.
    pub artifact: Option<Artifact>,
    pub release_page: String,
    #[serde(skip)]
    pub(super) target: String,
}

/// Whether a package manager (.deb or .rpm) installed this copy.
///
/// Such a copy never replaces its own files, so it only needs to learn that
/// a signed newer release exists; the release need not carry its package.
pub(super) fn package_managed(target: &str) -> bool {
    target.ends_with("-deb") || target.ends_with("-rpm")
}

/// The file name an artifact URL downloads, which its signature must name.
pub(super) fn artifact_file(url: &str) -> Result<String, String> {
    https_url(url)?
        .path_segments()
        .and_then(|mut segments| segments.next_back())
        .filter(|name| !name.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| "Invalid release artifact details".into())
}

/// What the compiled update policy trusts.
pub(super) struct Trust<'a> {
    pub(super) metadata_keys: &'a [String],
    pub(super) artifact_keys: &'a [String],
    /// Every artifact URL is this prefix followed by `v<version>/<file>`, the
    /// project's own release download, so the metadata key cannot send a client
    /// to another host for the signed bytes.
    pub(super) artifact_url_prefix: &'a str,
}

pub(super) fn verify(
    bytes: &[u8],
    trust: &Trust,
    channel: Channel,
    target: &str,
    current: &str,
    now: u64,
) -> Result<Option<Release>, String> {
    if bytes.len() as u64 > MAX_FEED {
        return Err("The update response is too large".into());
    }
    let envelope: Envelope =
        serde_json::from_slice(bytes).map_err(|_| "Invalid signed update response")?;
    let signature =
        Signature::decode(&envelope.signature).map_err(|_| "Invalid update metadata signature")?;
    if trust.metadata_keys.is_empty()
        || trust.metadata_keys.len() > 4
        || !trust.metadata_keys.iter().any(|key| {
            PublicKey::from_base64(key).is_ok_and(|key| {
                key.verify(envelope.payload.as_bytes(), &signature, false)
                    .is_ok()
            })
        })
    {
        return Err("Update metadata signature verification failed".into());
    }
    let mut manifest: Manifest =
        serde_json::from_str(&envelope.payload).map_err(|_| "Invalid release metadata")?;
    if envelope.version != manifest.version || envelope.notes != manifest.notes {
        return Err("The updater feed does not match the signed release metadata".into());
    }
    if manifest.schema_version != 1 || !manifest.published || manifest.channel != channel {
        return Err("This release is not published on the selected channel".into());
    }
    if manifest.issued_at > now.saturating_add(300)
        || manifest.expires_at <= now
        || manifest.expires_at <= manifest.issued_at
        || manifest.expires_at - manifest.issued_at > 7 * 86400
    {
        return Err(
            "Release metadata has expired or has an invalid date; check your system clock".into(),
        );
    }
    let version = Version::parse(&manifest.version).map_err(|_| "Invalid release version")?;
    let current = Version::parse(current).map_err(|_| "Invalid installed version")?;
    if !version.build.is_empty() || (channel == Channel::Stable && !version.pre.is_empty()) {
        return Err("This release version is not allowed on the selected channel".into());
    }
    if version < current {
        return Err("An older release was rejected".into());
    }
    if envelope.platforms.len() != manifest.artifacts.len() || manifest.notes.len() > 16 * 1024 {
        return Err("The updater feed does not match the signed release metadata".into());
    }
    for (artifact_target, artifact) in &manifest.artifacts {
        let installer = envelope
            .platforms
            .get(artifact_target)
            .ok_or("The updater feed does not match the signed release metadata")?;
        https_url(&artifact.url)?;
        if artifact.size == 0
            || artifact.size > MAX_ARTIFACT
            || artifact.sha256.len() != 64
            || !artifact
                .sha256
                .bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
            || !trust.artifact_keys.contains(&artifact.public_key)
            || PublicKey::from_base64(&artifact.public_key).is_err()
            || installer.url != artifact.url
            || installer.signature != artifact.signature
        {
            return Err("Invalid release artifact details".into());
        }
        // The version compared above comes from the metadata key. The artifact
        // key's trusted comment must name the same version, target and file, so
        // the metadata cannot relabel another signed release. The comment is
        // authenticated with the bytes before installation (verify_artifact).
        let file = artifact_file(&artifact.url)?;
        if artifact.url != format!("{}v{}/{file}", trust.artifact_url_prefix, manifest.version) {
            return Err("The release artifact is not at the project's release download".into());
        }
        let signature = decode_artifact_signature(&artifact.signature)
            .map_err(|_| "Invalid release artifact details")?;
        binding::check(
            signature.trusted_comment(),
            &manifest.version,
            artifact_target,
            &file,
        )?;
    }
    if version == current {
        return Ok(None);
    }
    let artifact = manifest.artifacts.remove(target);
    if artifact.is_none() && !package_managed(target) {
        return Err("No release for this platform, architecture and package type".into());
    }
    Ok(Some(Release {
        release_page: release_page(&manifest.version),
        version: manifest.version,
        notes: manifest.notes,
        artifact,
        target: target.to_owned(),
    }))
}
