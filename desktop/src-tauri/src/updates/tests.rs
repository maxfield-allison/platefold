use super::*;
use minisign::{sign, KeyPair};
use serde_json::{json, Value};
use std::{io::Cursor, net::TcpListener, thread};

const TARGET: &str = "windows-x86_64-nsis";
const NOW: u64 = 2_000_000_000;
// Test artifacts download from here, followed by v<version>/<file>.
const DOWNLOAD: &str = "https://example.invalid/download/";

fn trust<'a>(metadata_keys: &'a [String], artifact_keys: &'a [String]) -> manifest::Trust<'a> {
    manifest::Trust {
        metadata_keys,
        artifact_keys,
        artifact_url_prefix: DOWNLOAD,
    }
}

// Fresh, ephemeral keys stay in memory. No production key or private key file.
struct Fixture {
    metadata_keys: KeyPair,
    artifact_keys: KeyPair,
    manifest: Value,
}
impl Fixture {
    fn new() -> Self {
        let metadata_keys = KeyPair::generate_unencrypted_keypair().unwrap();
        let artifact_keys = KeyPair::generate_unencrypted_keypair().unwrap();
        let public_key = artifact_keys.pk.to_base64();
        let manifest = json!({
            "schema_version": 1, "version": "0.3.0", "channel": "stable", "published": true,
            "issued_at": NOW - 10, "expires_at": NOW + 600, "notes": "A synthetic release.",
            "artifacts": {TARGET: {"url": format!("{DOWNLOAD}v0.3.0/release.exe"), "size": 19,
                "sha256": format!("{:x}", Sha256::digest(b"synthetic installer")),
                "signature": "", "public_key": public_key}}
        });
        let mut fixture = Self {
            metadata_keys,
            artifact_keys,
            manifest,
        };
        fixture.rebind();
        fixture
    }
    // The artifact key's signature over the installer bytes, with a trusted
    // comment; None is Minisign's default comment, as an unbound signature has.
    fn artifact_signature(&self, trusted_comment: Option<&str>) -> String {
        BASE64.encode(
            sign(
                None,
                &self.artifact_keys.sk,
                Cursor::new(b"synthetic installer"),
                trusted_comment,
                None,
            )
            .unwrap()
            .to_string(),
        )
    }
    // Sign every listed artifact as the release job does: bound to the
    // manifest's version, its target and its URL's file name, at that
    // version's release download.
    fn rebind(&mut self) {
        let version = self.manifest["version"].as_str().unwrap().to_owned();
        let targets: Vec<String> = self.manifest["artifacts"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        for target in targets {
            let url = self.manifest["artifacts"][&target]["url"]
                .as_str()
                .unwrap()
                .to_owned();
            let file = manifest::artifact_file(&url).unwrap();
            self.manifest["artifacts"][&target]["url"] = json!(format!("{DOWNLOAD}v{version}/{file}"));
            let comment = binding::comment(&version, &target, &file).unwrap();
            self.manifest["artifacts"][&target]["signature"] =
                json!(self.artifact_signature(Some(&comment)));
        }
    }
    fn envelope(&self) -> Vec<u8> {
        self.envelope_for(TARGET)
    }
    fn envelope_for(&self, target: &str) -> Vec<u8> {
        let payload = self.manifest.to_string();
        let signature = sign(
            None,
            &self.metadata_keys.sk,
            Cursor::new(payload.as_bytes()),
            None,
            None,
        )
        .unwrap()
        .to_string();
        let artifact = &self.manifest["artifacts"][target];
        serde_json::to_vec(&json!({
            "payload": payload,
            "signature": signature,
            "version": self.manifest["version"],
            "notes": self.manifest["notes"],
            "platforms": {target: {
                "url": artifact["url"],
                "signature": artifact["signature"]
            }}
        }))
        .unwrap()
    }
    fn verify(
        &self,
        channel: Channel,
        target: &str,
        current: &str,
    ) -> Result<Option<manifest::Release>, String> {
        manifest::verify(
            &self.envelope(),
            &trust(&[self.metadata_keys.pk.to_base64()], &[self.artifact_keys.pk.to_base64()]),
            channel,
            target,
            current,
            NOW,
        )
    }
    fn policy(&self) -> Policy {
        Policy {
            public_keys: vec![self.metadata_keys.pk.to_base64()],
            artifact_public_keys: vec![self.artifact_keys.pk.to_base64()],
            stable: Some("https://example.invalid/stable.json".into()),
            prerelease: None,
            artifact_url_prefix: DOWNLOAD.into(),
        }
    }
}
#[test]
fn signed_release_requires_exact_platform_and_rejects_rollback() {
    let fixture = Fixture::new();
    assert_eq!(
        fixture
            .verify(Channel::Stable, TARGET, "0.3.0-dev.6")
            .unwrap()
            .unwrap()
            .version,
        "0.3.0"
    );
    assert!(fixture
        .verify(Channel::Stable, TARGET, "0.3.0")
        .unwrap()
        .is_none());
    assert!(fixture
        .verify(Channel::Stable, TARGET, "0.3.1")
        .unwrap_err()
        .contains("older"));
    for target in [
        "linux-x86_64-appimage",
        "windows-aarch64-nsis",
        "windows-x86_64-portable",
    ] {
        assert!(fixture.verify(Channel::Stable, target, "0.2.0").is_err());
    }
}
#[test]
fn metadata_tampering_and_wrong_keys_fail_before_claims_are_used() {
    let fixture = Fixture::new();
    let mut envelope: Value = serde_json::from_slice(&fixture.envelope()).unwrap();
    envelope["payload"] = json!(envelope["payload"]
        .as_str()
        .unwrap()
        .replace("0.3.0", "9.9.9"));
    let key = fixture.metadata_keys.pk.to_base64();
    assert!(manifest::verify(
        &serde_json::to_vec(&envelope).unwrap(),
        &trust(&[key], &[fixture.artifact_keys.pk.to_base64()]),
        Channel::Stable,
        TARGET,
        "0.2.0",
        NOW
    )
    .unwrap_err()
    .contains("signature"));
    let wrong = Fixture::new().metadata_keys.pk.to_base64();
    assert!(manifest::verify(
        &fixture.envelope(),
        &trust(&[wrong], &[fixture.artifact_keys.pk.to_base64()]),
        Channel::Stable,
        TARGET,
        "0.2.0",
        NOW
    )
    .is_err());
}
#[test]
fn key_overlap_accepts_next_key_but_empty_or_removed_keys_fail() {
    let fixture = Fixture::new();
    let old = Fixture::new().metadata_keys.pk.to_base64();
    let old_artifact = Fixture::new().artifact_keys.pk.to_base64();
    assert!(manifest::verify(
        &fixture.envelope(),
        &trust(&[old, fixture.metadata_keys.pk.to_base64()], &[fixture.artifact_keys.pk.to_base64()]),
        Channel::Stable,
        TARGET,
        "0.2.0",
        NOW
    )
    .is_ok());
    assert!(manifest::verify(
        &fixture.envelope(),
        &trust(&[fixture.metadata_keys.pk.to_base64()], &[old_artifact, fixture.artifact_keys.pk.to_base64()]),
        Channel::Stable,
        TARGET,
        "0.2.0",
        NOW
    )
    .is_ok());
    assert!(manifest::verify(
        &fixture.envelope(),
        &trust(&[], &[fixture.artifact_keys.pk.to_base64()]),
        Channel::Stable,
        TARGET,
        "0.2.0",
        NOW
    )
    .is_err());
    assert!(manifest::verify(
        &fixture.envelope(),
        &trust(&[fixture.metadata_keys.pk.to_base64()], &[Fixture::new().artifact_keys.pk.to_base64()]),
        Channel::Stable,
        TARGET,
        "0.2.0",
        NOW
    )
    .is_err());
}
#[test]
fn stable_excludes_drafts_and_prereleases_even_with_valid_signature() {
    for (field, value) in [
        ("published", json!(false)),
        ("version", json!("0.4.0-rc.1")),
        ("channel", json!("prerelease")),
        ("version", json!("0.3.0+other")),
        ("schema_version", json!(2)),
    ] {
        let mut fixture = Fixture::new();
        fixture.manifest[field] = value;
        assert!(
            fixture.verify(Channel::Stable, TARGET, "0.2.0").is_err(),
            "{field}"
        );
    }
    let mut fixture = Fixture::new();
    fixture.manifest["channel"] = json!("prerelease");
    fixture.manifest["version"] = json!("0.4.0-rc.1");
    fixture.rebind();
    assert!(fixture.verify(Channel::Prerelease, TARGET, "0.3.0").is_ok());
}
#[test]
fn signed_but_expired_future_or_unbounded_metadata_fails() {
    for (field, value) in [
        ("expires_at", NOW),
        ("issued_at", NOW + 301),
        ("expires_at", NOW + 8 * 86400),
        ("expires_at", NOW - 11),
    ] {
        let mut fixture = Fixture::new();
        fixture.manifest[field] = json!(value);
        assert!(fixture.verify(Channel::Stable, TARGET, "0.2.0").is_err());
    }
}
#[test]
fn artifact_identity_and_pinned_key_are_required_before_download() {
    for (field, value) in [
        ("url", json!("http://example.invalid/app")),
        ("sha256", json!("short")),
        ("size", json!(0)),
        ("size", json!(MAX_ARTIFACT + 1)),
        ("signature", json!("forged")),
        ("public_key", json!("untrusted")),
    ] {
        let mut fixture = Fixture::new();
        fixture.manifest["artifacts"][TARGET][field] = value;
        assert!(fixture.verify(Channel::Stable, TARGET, "0.2.0").is_err());
    }
}

#[test]
fn tauri_feed_fields_must_match_the_signed_payload() {
    let fixture = Fixture::new();
    for path in ["version", "signature"] {
        let mut envelope: Value = serde_json::from_slice(&fixture.envelope()).unwrap();
        if path == "version" {
            envelope["version"] = json!("9.9.9");
        } else {
            envelope["platforms"][TARGET]["signature"] = json!("different");
        }
        assert!(manifest::verify(
            &serde_json::to_vec(&envelope).unwrap(),
            &trust(&[fixture.metadata_keys.pk.to_base64()], &[fixture.artifact_keys.pk.to_base64()]),
            Channel::Stable,
            TARGET,
            "0.2.0",
            NOW,
        )
        .is_err());
    }
}

#[test]
fn test_signed_artifact_bytes_are_verified_before_installation() {
    let fixture = Fixture::new();
    let tauri_key = BASE64
        .decode(tauri_public_key(&fixture.artifact_keys.pk.to_base64()))
        .unwrap();
    assert!(PublicKey::decode(std::str::from_utf8(&tauri_key).unwrap()).is_ok());
    let artifact = fixture
        .verify(Channel::Stable, TARGET, "0.2.0")
        .unwrap()
        .unwrap()
        .artifact
        .unwrap();
    assert_eq!(
        verify_artifact(b"synthetic installer", "0.3.0", TARGET, &artifact).unwrap(),
        format!("{:x}", Sha256::digest(b"synthetic installer"))
    );
    assert!(verify_artifact(b"tampered installer", "0.3.0", TARGET, &artifact)
        .unwrap_err()
        .contains("size"));
    let mut same_size = b"synthetic installer".to_vec();
    same_size[0] ^= 1;
    assert!(verify_artifact(&same_size, "0.3.0", TARGET, &artifact)
        .unwrap_err()
        .contains("checksum"));
    let mut resigned_claim = artifact.clone();
    resigned_claim.sha256 = format!("{:x}", Sha256::digest(&same_size));
    assert!(verify_artifact(&same_size, "0.3.0", TARGET, &resigned_claim)
        .unwrap_err()
        .contains("signature"));
}
// A metadata-key holder offering another signed release under this version:
// the artifact signature's trusted comment names the release it was signed for.
fn check_with_signature(fixture: &mut Fixture, signature: String) -> Result<(), String> {
    fixture.manifest["artifacts"][TARGET]["signature"] = json!(signature);
    fixture.verify(Channel::Stable, TARGET, "0.2.0").map(|_| ())
}

#[test]
fn the_artifact_signature_must_name_the_offered_version_platform_and_file() {
    let mut fixture = Fixture::new();
    let release = fixture
        .verify(Channel::Stable, TARGET, "0.2.0")
        .unwrap()
        .unwrap();
    let artifact = release.artifact.unwrap();
    assert!(verify_artifact(b"synthetic installer", "0.3.0", TARGET, &artifact).is_ok());

    // An older build, genuinely signed as 0.2.0, offered by the metadata as 0.3.0.
    for (comment, reason) in [
        ("platefold-artifact v1 version=0.2.0 platform=windows-x86_64-nsis file=release.exe", "version"),
        ("platefold-artifact v1 version=0.3.0 platform=linux-x86_64-appimage file=release.exe", "platform"),
        ("platefold-artifact v1 version=0.3.0 platform=windows-x86_64-nsis file=release-0.2.0.exe", "file"),
        ("platefold-artifact v1 version=0.3.0 platform=windows-x86_64-nsis file=release.exe extra", "suffix"),
    ] {
        let signature = fixture.artifact_signature(Some(comment));
        let error = check_with_signature(&mut fixture, signature.clone()).unwrap_err();
        assert_eq!(error, binding::MISMATCH, "{reason}");
        // The download path refuses the same signature after verifying it.
        let mut signed = artifact.clone();
        signed.signature = signature;
        assert_eq!(
            verify_artifact(b"synthetic installer", "0.3.0", TARGET, &signed).unwrap_err(),
            binding::MISMATCH,
            "{reason}"
        );
    }

    // Signatures without a binding: Minisign's and Tauri's default comments,
    // and another scheme or format version.
    for comment in [
        None,
        Some("timestamp:1700000000\tfile:release.exe"),
        Some("platefold-artifact v2 version=0.3.0 platform=windows-x86_64-nsis file=release.exe"),
        Some("platefold-artifactv1 version=0.3.0 platform=windows-x86_64-nsis file=release.exe"),
        Some(""),
    ] {
        let signature = fixture.artifact_signature(comment);
        assert_eq!(
            check_with_signature(&mut fixture, signature.clone()).unwrap_err(),
            binding::UNBOUND,
            "{comment:?}"
        );
        let mut signed = artifact.clone();
        signed.signature = signature;
        assert_eq!(
            verify_artifact(b"synthetic installer", "0.3.0", TARGET, &signed).unwrap_err(),
            binding::UNBOUND
        );
    }
}

#[test]
fn a_rewritten_trusted_comment_fails_the_global_signature_before_installation() {
    let mut fixture = Fixture::new();
    // The genuine signature for an older release, its comment rewritten to the
    // offered one. The text now matches, so discovery shows it, but the global
    // signature no longer covers the comment and both verifiers refuse the bytes.
    let old = fixture.artifact_signature(Some(
        "platefold-artifact v1 version=0.2.0 platform=windows-x86_64-nsis file=release.exe",
    ));
    let document = String::from_utf8(BASE64.decode(&old).unwrap()).unwrap();
    let mut lines: Vec<&str> = document.lines().collect();
    let forged_comment = format!(
        "trusted comment: {}",
        binding::comment("0.3.0", TARGET, "release.exe").unwrap()
    );
    lines[2] = &forged_comment;
    let forged = BASE64.encode(lines.join("\n") + "\n");
    check_with_signature(&mut fixture, forged.clone()).unwrap();
    let artifact = fixture
        .verify(Channel::Stable, TARGET, "0.2.0")
        .unwrap()
        .unwrap()
        .artifact
        .unwrap();
    assert!(verify_artifact(b"synthetic installer", "0.3.0", TARGET, &artifact)
        .unwrap_err()
        .contains("signature verification failed"));
    // Tauri's own check, which runs on the downloaded bytes first, agrees.
    let key = PublicKey::from_base64(&fixture.artifact_keys.pk.to_base64()).unwrap();
    let signature = decode_artifact_signature(&forged).unwrap();
    assert!(key.verify(b"synthetic installer", &signature, true).is_err());
    let genuine = decode_artifact_signature(&old).unwrap();
    assert!(key.verify(b"synthetic installer", &genuine, true).is_ok());
}

#[test]
fn every_listed_artifact_is_bound_even_when_another_target_is_installed() {
    let mut fixture = Fixture::new();
    let other = fixture.manifest["artifacts"][TARGET].clone();
    fixture.manifest["artifacts"]["linux-x86_64-appimage"] = other;
    fixture.manifest["artifacts"]["linux-x86_64-appimage"]["url"] =
        json!(format!("{DOWNLOAD}v0.3.0/release.AppImage"));
    let envelope = |fixture: &Fixture| {
        let payload = fixture.manifest.to_string();
        let signature = sign(
            None,
            &fixture.metadata_keys.sk,
            Cursor::new(payload.as_bytes()),
            None,
            None,
        )
        .unwrap()
        .to_string();
        let platforms: serde_json::Map<String, Value> = fixture.manifest["artifacts"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(target, item)| {
                (
                    target.clone(),
                    json!({"url": item["url"], "signature": item["signature"]}),
                )
            })
            .collect();
        serde_json::to_vec(&json!({
            "payload": payload, "signature": signature,
            "version": fixture.manifest["version"], "notes": fixture.manifest["notes"],
            "platforms": platforms,
        }))
        .unwrap()
    };
    let check = |fixture: &Fixture, target: &str| {
        manifest::verify(
            &envelope(fixture),
            &trust(&[fixture.metadata_keys.pk.to_base64()], &[fixture.artifact_keys.pk.to_base64()]),
            Channel::Stable,
            target,
            "0.2.0",
            NOW,
        )
    };
    // The AppImage entry still carries the Windows installer's signature.
    for target in [TARGET, "linux-x86_64-deb"] {
        assert_eq!(check(&fixture, target).unwrap_err(), binding::MISMATCH);
    }
    fixture.rebind();
    for target in [TARGET, "linux-x86_64-appimage", "linux-x86_64-deb"] {
        assert!(check(&fixture, target).is_ok(), "{target}");
    }
}

#[test]
fn binding_comments_are_single_tokens_and_refuse_ambiguous_parts() {
    assert_eq!(
        binding::comment("1.0.0-rc.1", "macos-aarch64-app", "PlateFold_1.0.0-rc.1_macos-arm64.app.tar.gz")
            .unwrap(),
        "platefold-artifact v1 version=1.0.0-rc.1 platform=macos-aarch64-app file=PlateFold_1.0.0-rc.1_macos-arm64.app.tar.gz"
    );
    for (version, platform, file) in [
        ("1.0.0 platform=x", TARGET, "release.exe"),
        ("1.0.0", "windows x86_64", "release.exe"),
        ("1.0.0", TARGET, "release%20.exe"),
        ("1.0.0", TARGET, ".hidden"),
        ("1.0.0", TARGET, ""),
        ("", TARGET, "release.exe"),
    ] {
        assert!(binding::comment(version, platform, file).is_err());
    }
    assert!(manifest::artifact_file("https://example.invalid/").is_err());
    assert_eq!(
        manifest::artifact_file("https://example.invalid/v1/a.exe?x=1").unwrap(),
        "a.exe"
    );
}

#[test]
fn invalid_or_oversized_envelopes_and_credential_urls_fail() {
    let fixture = Fixture::new();
    for bytes in [
        b"{}".to_vec(),
        fixture.envelope()[..20].to_vec(),
        vec![b'a'; MAX_FEED as usize + 1],
    ] {
        assert!(manifest::verify(
            &bytes,
            &trust(&[fixture.metadata_keys.pk.to_base64()], &[fixture.artifact_keys.pk.to_base64()]),
            Channel::Stable,
            TARGET,
            "0.2.0",
            NOW
        )
        .is_err());
    }
    for url in [
        "http://example.invalid/a",
        "file:///tmp/a",
        "https://user:password@example.invalid/a",
        "https://example.invalid/a#b",
    ] {
        assert!(https_url(url).is_err());
    }
}
#[test]
fn preferences_survive_reopen_without_touching_other_settings_and_recover_from_corruption() {
    let temp = tempfile::tempdir().unwrap();
    let unrelated = temp.path().join("theme.json");
    fs::write(&unrelated, b"original").unwrap();
    let state = UpdateState::new(temp.path().to_owned(), None);
    assert_eq!(
        state.inner.lock().unwrap().preferences,
        Preferences::default()
    );
    let preferences = Preferences {
        enabled: true,
        channel: Channel::Prerelease,
    };
    state.save(preferences).unwrap();
    assert_eq!(
        UpdateState::new(temp.path().to_owned(), None)
            .inner
            .lock()
            .unwrap()
            .preferences,
        preferences
    );
    state.save(Preferences::default()).unwrap();
    assert!(
        !UpdateState::new(temp.path().to_owned(), None)
            .inner
            .lock()
            .unwrap()
            .preferences
            .enabled
    );
    fs::write(&state.file, "invalid").unwrap();
    let recovered = UpdateState::new(temp.path().to_owned(), None);
    let recovered = recovered.inner.lock().unwrap();
    assert!(!recovered.preferences.enabled);
    assert!(recovered.warning.is_some());
    assert_eq!(fs::read(unrelated).unwrap(), b"original");
}

#[test]
fn interrupted_operation_marker_is_consumed_without_reusing_bytes() {
    let temp = tempfile::tempdir().unwrap();
    let state = UpdateState::new(temp.path().to_owned(), None);
    let preferences = Preferences {
        enabled: true,
        channel: Channel::Prerelease,
    };
    state.save(preferences).unwrap();
    state.mark("0.3.0", Phase::Downloading).unwrap();
    let recovered = UpdateState::new(temp.path().to_owned(), None);
    let store = recovered.inner.lock().unwrap();
    assert_eq!(store.preferences, preferences);
    assert_eq!(store.phase, Phase::Idle);
    assert!(store.pending.is_none());
    assert!(store.warning.as_deref().unwrap().contains("interrupted"));
    assert!(!recovered.recovery.exists());
}
#[test]
fn completed_native_handoff_clears_marker_and_preserves_preferences() {
    let temp = tempfile::tempdir().unwrap();
    let state = UpdateState::new(temp.path().to_owned(), None);
    let preferences = Preferences {
        enabled: true,
        channel: Channel::Prerelease,
    };
    state.save(preferences).unwrap();
    state
        .mark(env!("CARGO_PKG_VERSION"), Phase::Installing)
        .unwrap();
    let recovered = UpdateState::new(temp.path().to_owned(), None);
    let store = recovered.inner.lock().unwrap();
    assert_eq!(store.preferences, preferences);
    assert_eq!(store.phase, Phase::Idle);
    assert!(store.pending.is_none());
    assert!(store.warning.is_none());
    assert!(!recovered.recovery.exists());
}

#[test]
fn unresolved_or_invalid_handoff_still_warns() {
    for marker in [
        serde_json::to_vec(&Recovery {
            version: "999.0.0".into(),
            phase: Phase::Installing,
        })
        .unwrap(),
        serde_json::to_vec(&Recovery {
            version: env!("CARGO_PKG_VERSION").into(),
            phase: Phase::Downloading,
        })
        .unwrap(),
        b"invalid".to_vec(),
        vec![b' '; 1025],
    ] {
        let temp = tempfile::tempdir().unwrap();
        let state = UpdateState::new(temp.path().to_owned(), None);
        fs::write(&state.recovery, marker).unwrap();
        let recovered = UpdateState::new(temp.path().to_owned(), None);
        let store = recovered.inner.lock().unwrap();
        assert!(store.warning.as_deref().unwrap().contains("interrupted"));
        assert!(store.pending.is_none());
        assert!(!recovered.recovery.exists());
    }
}

#[test]
fn completed_handoff_does_not_hide_invalid_preferences() {
    let temp = tempfile::tempdir().unwrap();
    let state = UpdateState::new(temp.path().to_owned(), None);
    state
        .mark(env!("CARGO_PKG_VERSION"), Phase::Installing)
        .unwrap();
    fs::write(&state.file, "invalid").unwrap();
    let recovered = UpdateState::new(temp.path().to_owned(), None);
    let store = recovered.inner.lock().unwrap();
    assert!(!store.preferences.enabled);
    assert!(store.warning.as_deref().unwrap().contains("preferences"));
}

#[test]
fn disabled_unconfigured_concurrent_and_stale_checks_fail_closed() {
    let fixture = Fixture::new();
    let mut store = Store {
        appimage: None,
        preferences: Preferences::default(),
        revision: 0,
        checking: false,
        warning: None,
        phase: Phase::Idle,
        pending: None,
        abort: None,
        exit_requested: false,
        attempt: 0,
        gate: None,
        restart_cancelled: None,
        close_released: false,
    };
    assert!(begin(&mut store, &fixture.policy())
        .unwrap_err()
        .contains("disabled"));
    store.preferences.enabled = true;
    let unconfigured = Policy {
        public_keys: vec![],
        artifact_public_keys: vec![],
        stable: None,
        prerelease: None,
        artifact_url_prefix: DOWNLOAD.into(),
    };
    assert!(begin(&mut store, &unconfigured).is_err());
    let (revision, _, _) = begin(&mut store, &fixture.policy()).unwrap();
    assert!(begin(&mut store, &fixture.policy()).is_err());
    store.revision += 1;
    store.preferences.enabled = false;
    assert!(finish(&mut store, revision, false).is_err());
    assert!(!store.checking);
    assert!(finish(&mut store, revision + 1, true)
        .unwrap_err()
        .contains("export"));
}
#[test]
fn package_manager_and_portable_guidance_uses_bundle_marker() {
    use tauri::utils::config::BundleType;
    assert_eq!(distribution(None), "portable");
    assert_eq!(distribution(Some(BundleType::Nsis)), "nsis");
    assert!(guidance(distribution(Some(BundleType::Deb))).contains("new .deb"));
    assert!(guidance(distribution(Some(BundleType::Rpm))).contains("package manager"));
    assert!(guidance("portable").contains("signature"));
    assert!(guidance("app").contains("approve each step"));
    assert!(installable("appimage"));
    assert!(!installable("deb"));
    assert!(!guidance("appimage").contains("qualification"));
}

#[test]
fn an_appimage_in_a_folder_it_cannot_change_offers_guidance_not_a_download() {
    assert_eq!(install_route("appimage", true), (true, guidance("appimage")));
    let (offered, message) = install_route("appimage", false);
    assert!(!offered);
    assert!(message.contains("/opt/platefold") && message.contains("LINUX-INSTALL.md"));
    // A macOS app in a folder this account can't change is updated by hand,
    // never through an administrator prompt.
    assert_eq!(install_route("app", true), (true, guidance("app")));
    let (offered, message) = install_route("app", false);
    assert!(!offered);
    assert!(message.contains("drag it to Applications") && !message.contains("password"));
    // Windows installers replace the app themselves; other bundles keep their route.
    assert_eq!(install_route("nsis", false), (true, guidance("nsis")));
    assert_eq!(install_route("deb", false), (false, guidance("deb")));
}

#[test]
fn a_deb_install_is_detected_as_a_package_and_never_replaces_itself() {
    use tauri::utils::config::BundleType;
    let distribution = distribution(Some(BundleType::Deb));
    assert_eq!(distribution, "deb");
    assert!(!installable(distribution));
    // Neither a writable folder nor a read-only one offers a download.
    for writable in [true, false] {
        let (offered, message) = install_route(distribution, writable);
        assert!(!offered);
        assert!(message.contains("download the new .deb from the release page"));
        assert!(message.contains("sudo apt install ./platefold_"));
        assert!(!message.contains("approve each step") && !message.contains("AppImage"));
    }
    assert_ne!(guidance("deb"), guidance("rpm"));
}

#[test]
fn a_deb_install_learns_of_a_newer_signed_release_without_a_package_in_the_feed() {
    let fixture = Fixture::new();
    for target in ["linux-x86_64-deb", "linux-aarch64-deb", "linux-x86_64-rpm"] {
        let release = fixture
            .verify(Channel::Stable, target, "0.2.0")
            .unwrap()
            .unwrap();
        assert_eq!(release.version, "0.3.0");
        assert!(release.artifact.is_none());
        assert_eq!(
            release.release_page,
            "https://github.com/maxfield-allison/platefold/releases/tag/v0.3.0"
        );
        assert!(fixture
            .verify(Channel::Stable, target, "0.3.0")
            .unwrap()
            .is_none());
        assert!(fixture
            .verify(Channel::Stable, target, "0.3.1")
            .unwrap_err()
            .contains("older"));
    }
    // A self-updating bundle still needs its own signed artifact.
    assert!(fixture
        .verify(Channel::Stable, "linux-x86_64-appimage", "0.2.0")
        .unwrap_err()
        .contains("No release for this platform"));
    // The notice still rests on the signed metadata: a wrong key refuses it.
    let other = KeyPair::generate_unencrypted_keypair().unwrap();
    assert!(manifest::verify(
        &fixture.envelope(),
        &trust(&[other.pk.to_base64()], &[fixture.artifact_keys.pk.to_base64()]),
        Channel::Stable,
        "linux-x86_64-deb",
        "0.2.0",
        NOW,
    )
    .unwrap_err()
    .contains("signature"));
}

#[test]
fn a_deb_listed_in_the_feed_is_shown_with_its_signed_details() {
    let mut fixture = Fixture::new();
    let artifact = fixture.manifest["artifacts"][TARGET].clone();
    fixture.manifest["artifacts"] = json!({"linux-x86_64-deb": artifact.clone()});
    fixture.rebind();
    let mut envelope: Value =
        serde_json::from_slice(&fixture.envelope_for("linux-x86_64-deb")).unwrap();
    let release = manifest::verify(
        &serde_json::to_vec(&envelope).unwrap(),
        &trust(&[fixture.metadata_keys.pk.to_base64()], &[fixture.artifact_keys.pk.to_base64()]),
        Channel::Stable,
        "linux-x86_64-deb",
        "0.2.0",
        NOW,
    )
    .unwrap()
    .unwrap();
    assert_eq!(release.artifact.unwrap().url, artifact["url"]);
    envelope["platforms"] = json!({});
    assert!(manifest::verify(
        &serde_json::to_vec(&envelope).unwrap(),
        &trust(&[fixture.metadata_keys.pk.to_base64()], &[fixture.artifact_keys.pk.to_base64()]),
        Channel::Stable,
        "linux-x86_64-deb",
        "0.2.0",
        NOW,
    )
    .is_err());
}
// Local HTTP is available only to this test helper; production fetch enforces HTTPS.
fn response(raw: Vec<u8>) -> Result<Vec<u8>, String> {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = Url::parse(&format!("http://{}/feed", listener.local_addr().unwrap())).unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut request = [0; 4096];
        let _ = stream.read(&mut request);
        let _ = stream.write_all(&raw);
    });
    let result = fetch_with_client(Client::builder().no_proxy(), url);
    server.join().unwrap();
    result
}
#[test]
fn real_transport_verifies_signed_bytes_and_handles_interrupted_unavailable_redirect_and_oversize()
{
    let fixture = Fixture::new();
    let body = fixture.envelope();
    let mut raw = format!(
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .into_bytes();
    raw.extend(&body);
    let received = response(raw).unwrap();
    assert!(manifest::verify(
        &received,
        &trust(&[fixture.metadata_keys.pk.to_base64()], &[fixture.artifact_keys.pk.to_base64()]),
        Channel::Stable,
        TARGET,
        "0.2.0",
        NOW
    )
    .is_ok());
    assert!(response(
        b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/other\r\nContent-Length: 0\r\n\r\n"
            .to_vec()
    )
    .unwrap_err()
    .contains("did not return a release"));
    for raw in [
        b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n".to_vec(),
        b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort".to_vec(),
        b"HTTP/1.1 200 OK\r\nContent-Length: 65537\r\n\r\n".to_vec(),
        [
            b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n".as_slice(),
            &vec![b'a'; MAX_FEED as usize + 1],
        ]
        .concat(),
    ] {
        assert!(response(raw).is_err());
    }
}

#[test]
fn deferred_restart_excludes_exports_and_requires_an_installed_update() {
    let mut desktop = super::super::Desktop::default();
    for phase in [
        Phase::Idle,
        Phase::Available,
        Phase::Verified,
        Phase::Installing,
    ] {
        assert!(reserve_restart(&mut desktop, phase).is_err());
        assert!(!desktop.updating);
    }
    desktop.busy = true;
    assert!(reserve_restart(&mut desktop, Phase::Installed).is_err());
    assert!(!desktop.updating);
    desktop.busy = false;
    reserve_restart(&mut desktop, Phase::Installed).unwrap();
    assert!(super::super::allow_worker_command(&desktop, "export").is_err());
    assert!(reserve_restart(&mut desktop, Phase::Installed).is_err());
}

#[test]
fn withdrawing_consent_during_check_persists_and_discards_the_stale_result() {
    let temp = tempfile::tempdir().unwrap();
    let state = UpdateState::new(temp.path().to_owned(), None);
    save_preferences(
        &state,
        Preferences {
            enabled: true,
            channel: Channel::Stable,
        },
    )
    .unwrap();
    let (revision, _, _) =
        begin(&mut state.inner.lock().unwrap(), &Fixture::new().policy()).unwrap();
    save_preferences(&state, Preferences::default()).unwrap();
    let reopened = UpdateState::new(temp.path().to_owned(), None);
    assert!(!reopened.inner.lock().unwrap().preferences.enabled);
    let mut store = state.inner.lock().unwrap();
    assert!(store.checking); // Preserve exclusion until the in-flight fetch finishes.
    assert!(finish(&mut store, revision, false).is_err());
    assert!(!store.checking);
    assert!(store.pending.is_none());
    assert_eq!(store.phase, Phase::Idle);
}

#[test]
fn changing_update_settings_keeps_an_installed_update_waiting_for_its_restart() {
    let temp = tempfile::tempdir().unwrap();
    let state = UpdateState::new(temp.path().to_owned(), None);
    let warning = "PlateFold could not delete the unused update folder at /apps/.PlateFold.AppImage.update-a1B2c3. You can delete that folder.";
    {
        let mut store = state.inner.lock().unwrap();
        store.phase = Phase::Installed;
        store.warning = Some(warning.into());
    }

    let status = save_preferences(
        &state,
        Preferences {
            enabled: true,
            channel: Channel::Prerelease,
        },
    )
    .unwrap();

    assert_eq!(status.phase, Phase::Installed);
    assert_eq!(status.warning.as_deref(), Some(warning));
    let mut desktop = super::super::Desktop::default();
    assert!(
        reserve_restart(&mut desktop, state.inner.lock().unwrap().phase).is_ok(),
        "the Restart button still works"
    );
}

#[test]
fn an_install_result_keeps_the_status_fields_beside_the_native_restart_decision() {
    let temp = tempfile::tempdir().unwrap();
    let state = UpdateState::new(temp.path().to_owned(), None);
    let result = InstallResult {
        status: status(&state.inner.lock().unwrap()),
        restarting: false,
    };

    let json = serde_json::to_value(&result).unwrap();

    assert_eq!(json["restarting"], json!(false));
    assert!(json.get("phase").is_some(), "{json}");
    assert!(json.get("warning").is_some(), "{json}");
}

#[test]
fn artifact_redirects_are_limited_to_github_release_asset_hosts() {
    for allowed in [
        "https://release-assets.githubusercontent.com/github-production-release-asset/1/a?sig=x",
        "https://objects.githubusercontent.com/github-production-release-asset-2e65be/1/a",
    ] {
        assert!(
            artifact_redirect_allowed(&Url::parse(allowed).unwrap()),
            "{allowed}"
        );
    }
    for refused in [
        "http://release-assets.githubusercontent.com/a",
        "https://release-assets.githubusercontent.com:8443/a",
        "https://user:pw@release-assets.githubusercontent.com/a",
        "https://github.com/maxfield-allison/platefold/releases/download/v1/a",
        "https://evil.example/release-assets.githubusercontent.com/a",
        "https://release-assets.githubusercontent.com.evil.example/a",
    ] {
        assert!(
            !artifact_redirect_allowed(&Url::parse(refused).unwrap()),
            "{refused}"
        );
    }
}

#[test]
fn a_replacement_that_could_not_be_undone_names_the_kept_copy_and_offers_no_retry() {
    let app = PathBuf::from("/Applications/PlateFold.app");
    let backup = PathBuf::from("/Applications/PlateFold.app.previous-a1b2c3/PlateFold.app");
    let failure = install_failure(
        "1.0.1",
        &tauri_plugin_updater::Error::ReplacementNotRestored {
            app: app.clone(),
            backup: backup.clone(),
            install: std::io::Error::other("install"),
            restore: std::io::Error::other("restore"),
        },
    );
    assert!(!failure.retry);
    assert!(failure.warning.contains(&backup.display().to_string()));
    assert!(failure.warning.contains(&app.display().to_string()));
    assert!(failure.message.contains(&backup.display().to_string()));

    let rolled_back = install_failure(
        "1.0.1",
        &tauri_plugin_updater::Error::ReplacementRolledBack(std::io::Error::other("install")),
    );
    assert!(rolled_back.retry);
    assert!(rolled_back.warning.contains("put back unchanged"));

    let refused = install_failure(
        "1.0.1",
        &tauri_plugin_updater::Error::ReplacementUnsupported { staging_left: None },
    );
    assert!(!refused.retry, "a refused volume would refuse every retry");
    assert!(refused.warning.contains("PlateFold is unchanged"));
    assert!(refused.warning.contains("by hand"));
    assert!(!refused.warning.contains("could not delete"));
    assert!(refused.message.contains("by hand"));

    let staging = std::path::PathBuf::from("/Applications/.PlateFold.app.update-a1B2c3");
    let refused_left = install_failure(
        "1.0.1",
        &tauri_plugin_updater::Error::ReplacementUnsupported {
            staging_left: Some(staging.clone()),
        },
    );
    assert!(
        !refused_left.retry,
        "a staging folder left behind changes no retry decision"
    );
    assert!(refused_left.warning.contains("PlateFold is unchanged"));
    assert!(refused_left
        .warning
        .contains(&staging.display().to_string()));

    let other = install_failure(
        "1.0.1",
        &tauri_plugin_updater::Error::Io(std::io::Error::other("extract")),
    );
    assert!(other.retry);
    assert!(other.warning.contains("not declared replaced"));
}

#[test]
fn a_folder_left_by_a_successful_installation_defers_the_requested_restart() {
    let leftover = [PathBuf::from("/apps/.PlateFold.AppImage.update-a1B2c3")];
    assert!(restarts_now(true, false, &[]));
    assert!(
        !restarts_now(true, false, &leftover),
        "the warning naming the folder must be seen first"
    );
    assert!(
        !restarts_now(true, true, &[]),
        "a close wins over a restart"
    );
    assert!(!restarts_now(false, false, &[]));
}

#[test]
fn folders_an_installation_could_not_delete_are_added_to_its_warning() {
    let staging = PathBuf::from("/apps/.PlateFold.AppImage.update-a1B2c3");
    let sentence = "PlateFold could not delete the unused update folder at /apps/.PlateFold.AppImage.update-a1B2c3. You can delete that folder.";

    assert_eq!(with_leftovers(None, &[]), None);
    assert_eq!(
        with_leftovers(Some("Installation failed.".into()), &[]).as_deref(),
        Some("Installation failed.")
    );
    // A successful installation gains a warning only for the folder it left.
    assert_eq!(
        with_leftovers(None, std::slice::from_ref(&staging)).as_deref(),
        Some(sentence)
    );
    // A failed one keeps its own warning first.
    assert_eq!(
        with_leftovers(Some("Installation failed.".into()), &[staging]).unwrap(),
        format!("Installation failed. {sentence}")
    );
}

// The signed feeds live in a repository of their own, served by its own Pages
// site, so the credential that renews them cannot write the product's source.
#[test]
fn the_compiled_policy_reads_both_channels_from_the_feed_only_origin() {
    const FEED_ORIGIN: &str = "https://maxfield-allison.github.io/platefold-updates/";
    let policy = Policy::bundled().unwrap();
    for (channel, file) in [
        (Channel::Stable, "stable.json"),
        (Channel::Prerelease, "prerelease.json"),
    ] {
        let endpoint = policy.endpoint(channel).unwrap();
        assert_eq!(endpoint, format!("{FEED_ORIGIN}{file}"));
        let url = https_url(endpoint).unwrap();
        assert_eq!(url.host_str(), Some("maxfield-allison.github.io"));
        assert!(!url.path().starts_with("/platefold/"), "{endpoint}");
    }
}

#[test]
fn an_artifact_outside_its_release_download_is_refused() {
    // The metadata key signs these URLs, the artifact key signs only version,
    // target and file name: the location is held by the compiled policy.
    for url in [
        "https://mirror.example/download/v0.3.0/release.exe",
        "https://example.invalid/download/v0.2.0/release.exe",
        "https://example.invalid/download/v0.3.0/other/release.exe",
        "https://example.invalid/elsewhere/v0.3.0/release.exe",
    ] {
        let mut fixture = Fixture::new();
        let version = "0.3.0";
        let comment = binding::comment(version, TARGET, "release.exe").unwrap();
        fixture.manifest["artifacts"][TARGET]["url"] = json!(url);
        fixture.manifest["artifacts"][TARGET]["signature"] =
            json!(fixture.artifact_signature(Some(&comment)));
        let error = fixture
            .verify(Channel::Stable, TARGET, "0.2.0")
            .unwrap_err();
        assert!(error.contains("release download"), "{url}: {error}");
    }
}

#[test]
fn the_download_location_must_be_an_https_folder() {
    let fixture = Fixture::new();
    for prefix in ["http://example.invalid/download/", "https://example.invalid/download", ""] {
        let policy = Policy {
            artifact_url_prefix: prefix.into(),
            ..fixture.policy()
        };
        assert!(policy.endpoint(Channel::Stable).is_err(), "{prefix}");
    }
    assert!(fixture.policy().endpoint(Channel::Stable).is_ok());
}

#[test]
fn the_bundled_policy_downloads_from_the_project_releases() {
    let policy = Policy::bundled().unwrap();
    assert_eq!(
        policy.artifact_url_prefix,
        "https://github.com/maxfield-allison/platefold/releases/download/"
    );
}

#[test]
fn an_update_checks_write_access_to_everything_it_renames() {
    let app = Path::new("/Applications/PlateFold.app/Contents/MacOS/platefold-desktop");
    assert_eq!(
        renamed_paths("app", app),
        [
            PathBuf::from("/Applications/PlateFold.app"),
            PathBuf::from("/Applications")
        ]
    );
    // An executable outside a bundle has nothing the updater knows how to rename.
    assert!(renamed_paths("app", Path::new("/usr/local/bin/platefold-desktop")).is_empty());
    assert_eq!(
        renamed_paths("appimage", Path::new("/home/a/Apps/PlateFold.AppImage")),
        [PathBuf::from("/home/a/Apps")]
    );
    assert!(renamed_paths("nsis", app).is_empty());
}

#[cfg(unix)]
#[test]
fn appimage_route_uses_outer_file_and_preserves_admin_managed_refusal() {
    use std::os::unix::fs::PermissionsExt;
    let temp = tempfile::tempdir().unwrap();
    let mounted = temp.path().join("mount/usr/bin");
    let installed = temp.path().join("Applications");
    fs::create_dir_all(&mounted).unwrap();
    fs::create_dir(&installed).unwrap();
    let executable = mounted.join("platefold-desktop");
    let appimage = installed.join("PlateFold.AppImage");
    fs::write(&executable, b"mounted executable").unwrap();
    fs::write(&appimage, b"outer image").unwrap();
    fs::set_permissions(&mounted, fs::Permissions::from_mode(0o555)).unwrap();
    assert_eq!(
        route_for_paths("appimage", Some(&executable), Some(&appimage)),
        (true, guidance("appimage"))
    );
    // Even root must use the outer path: this mounted-executable location has
    // no parent directory, so falling back to it would refuse the update.
    let unavailable_mount = temp.path().join("unavailable/usr/bin/platefold-desktop");
    assert_eq!(
        route_for_paths("appimage", Some(&unavailable_mount), Some(&appimage)),
        (true, guidance("appimage"))
    );
    assert_eq!(
        route_for_paths("appimage", None, Some(&appimage)),
        (true, guidance("appimage"))
    );
    assert_eq!(
        route_for_paths("appimage", Some(&executable), None),
        (false, APPIMAGE_NOT_REPLACEABLE)
    );
    fs::set_permissions(&installed, fs::Permissions::from_mode(0o555)).unwrap();
    if unsafe { libc::geteuid() } != 0 {
        assert_eq!(
            route_for_paths("appimage", Some(&executable), Some(&appimage)),
            (false, APPIMAGE_NOT_REPLACEABLE)
        );
    }
    fs::set_permissions(&mounted, fs::Permissions::from_mode(0o755)).unwrap();
    fs::set_permissions(&installed, fs::Permissions::from_mode(0o755)).unwrap();
}

#[cfg(unix)]
#[test]
fn write_access_follows_the_folder_permissions() {
    use std::os::unix::fs::PermissionsExt;
    let folder = tempfile::tempdir().unwrap();
    assert!(writable(folder.path()));
    fs::set_permissions(folder.path(), fs::Permissions::from_mode(0o555)).unwrap();
    // root ignores the mode; the check is then meaningless, as it is for root.
    if unsafe { libc::geteuid() } != 0 {
        assert!(!writable(folder.path()));
    }
    fs::set_permissions(folder.path(), fs::Permissions::from_mode(0o755)).unwrap();
}

fn installing_store(attempt: u64) -> Store {
    Store {
        appimage: None,
        preferences: Preferences::default(),
        revision: 0,
        checking: false,
        warning: None,
        phase: Phase::Installing,
        pending: None,
        abort: None,
        exit_requested: false,
        attempt,
        gate: Some(Arc::new(CommitGate::new())),
        restart_cancelled: Some(Arc::new(AtomicBool::new(false))),
        close_released: false,
    }
}

#[test]
fn a_close_during_installation_waits_for_its_result_and_is_then_finished() {
    let mut store = installing_store(1);
    // Outside an installation a close goes ahead at once and records nothing.
    for phase in [
        Phase::Idle,
        Phase::Downloading,
        Phase::Verified,
        Phase::Installed,
    ] {
        store.phase = phase;
        assert!(!defer_exit(&mut store), "{phase:?}");
        assert!(!store.exit_requested);
    }
    store.phase = Phase::Installing;
    assert!(defer_exit(&mut store));
    // A second close while still installing is held the same way.
    assert!(defer_exit(&mut store));
    // install_update takes the request together with the terminal phase.
    assert!(std::mem::take(&mut store.exit_requested));
    store.phase = Phase::Installed;
    assert!(!defer_exit(&mut store));
}

#[test]
fn a_deferred_close_withdraws_the_windows_restart() {
    let mut store = installing_store(1);
    let cancelled = store.restart_cancelled.clone().unwrap();
    assert!(!cancelled.load(Ordering::SeqCst));
    assert!(defer_exit(&mut store));
    assert!(cancelled.load(Ordering::SeqCst));
}

#[test]
fn a_stalled_installation_stops_holding_the_close_after_the_limit() {
    let mut store = installing_store(3);
    assert!(defer_exit(&mut store));
    // Before the limit the close keeps waiting.
    assert!(!release_overdue_close(&mut store, 3, false));
    // A watcher left over from an earlier attempt never acts on this one.
    assert!(!release_overdue_close(&mut store, 2, true));
    // Past the limit, an attempt that is still staging is abandoned...
    assert!(release_overdue_close(&mut store, 3, true));
    assert!(store.close_released);
    // ...so it can no longer replace anything, and closes are no longer held.
    assert!(
        store.gate.as_ref().unwrap().abandon(),
        "the gate stays abandoned"
    );
    assert!(!defer_exit(&mut store));
    // Once an attempt has finished, an old watcher releases nothing.
    let mut finished = installing_store(4);
    finished.phase = Phase::Installed;
    assert!(!release_overdue_close(&mut finished, 4, true));
    assert!(!finished.close_released);
}
