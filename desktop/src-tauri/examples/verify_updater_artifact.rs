//! Verify packaged updater bytes against the same policy compiled into the app.
//!
//! An artifact must also carry the release binding the app requires: its
//! signature's trusted comment names the version, update target and file. The
//! policy type is the app's own (src/updates/policy.rs), so a field added to the
//! policy reaches this check too.
#[path = "../src/updates/binding.rs"]
mod binding;
#[path = "../src/updates/policy.rs"]
mod policy;

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use minisign_verify::{PublicKey, Signature};
use policy::Policy;
use sha2::{Digest, Sha256};
use std::{fs, path::Path};

fn https_without_credentials(value: &str) -> bool {
    reqwest::Url::parse(value).is_ok_and(|url| {
        value.len() <= 2048
            && url.scheme() == "https"
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.fragment().is_none()
    })
}

fn validate_policy(policy: &Policy) -> Result<(), String> {
    for keys in [&policy.public_keys, &policy.artifact_public_keys] {
        if keys.is_empty() || keys.len() > 4 {
            return Err("Updater metadata and artifact trust keys must be configured".into());
        }
        for key in keys {
            PublicKey::from_base64(key).map_err(|_| "Invalid updater public key")?;
        }
    }
    if policy
        .public_keys
        .iter()
        .any(|key| policy.artifact_public_keys.contains(key))
    {
        return Err("Updater metadata and artifacts require separate trust keys".into());
    }
    if policy.stable.is_none() && policy.prerelease.is_none() {
        return Err("An updater channel must be configured".into());
    }
    for endpoint in [&policy.stable, &policy.prerelease].into_iter().flatten() {
        if !https_without_credentials(endpoint) {
            return Err(
                "Updater channel URLs require HTTPS without credentials or fragments".into(),
            );
        }
    }
    // The app downloads an artifact only from <prefix>v<version>/<file>.
    if !policy.artifact_url_prefix.ends_with('/')
        || !https_without_credentials(&policy.artifact_url_prefix)
    {
        return Err("The updater download prefix must be an HTTPS URL ending in /".into());
    }
    Ok(())
}

/// What an artifact's signature must name: (version, update target, file).
type Binding<'a> = (&'a str, &'a str, &'a str);

/// Verify with the key for the purpose; return the key and the trusted comment.
fn verify<'a>(
    policy: &'a Policy,
    bytes: &[u8],
    encoded_signature: &str,
    binding: Option<Binding>,
) -> Result<(&'a str, String), String> {
    let metadata = binding.is_none();
    validate_policy(policy)?;
    let decoded = BASE64
        .decode(encoded_signature.trim())
        .map_err(|_| "Invalid updater signature encoding")?;
    let text = std::str::from_utf8(&decoded).map_err(|_| "Invalid updater signature document")?;
    let signature = Signature::decode(text).map_err(|_| "Invalid updater signature")?;
    let keys = if metadata {
        &policy.public_keys
    } else {
        &policy.artifact_public_keys
    };
    let key = keys
        .iter()
        .find_map(|key| {
            PublicKey::from_base64(key)
                .ok()
                // As the app does: prehashed signatures only. Verification
                // covers the bytes and, through the global signature, the
                // trusted comment.
                .filter(|public| public.verify(bytes, &signature, false).is_ok())
                .map(|_| key.as_str())
        })
        .ok_or("Updater signature does not match the compiled policy for the selected purpose")?;
    if let Some((version, platform, file)) = binding {
        binding::check(signature.trusted_comment(), version, platform, file)?;
    }
    Ok((key, signature.trusted_comment().to_owned()))
}

const USAGE: &str =
    "Usage: verify_updater_artifact (--metadata | --binding VERSION PLATFORM) FILE TAURI_SIG";

fn run() -> Result<(), String> {
    let mut arguments: Vec<String> = std::env::args_os()
        .skip(1)
        .map(|value| value.into_string().map_err(|_| USAGE.to_string()))
        .collect::<Result<_, _>>()?;
    let (metadata, release) = match arguments.first().map(String::as_str) {
        Some("--metadata") if arguments.len() == 3 => {
            arguments.remove(0);
            (true, None)
        }
        Some("--binding") if arguments.len() == 5 => {
            let release: Vec<String> = arguments.drain(..3).skip(1).collect();
            (false, Some(release))
        }
        _ => return Err(USAGE.into()),
    };
    let policy = Policy::bundled().map_err(|_| "Invalid compiled updater policy")?;
    validate_policy(&policy)?;
    let artifact = Path::new(&arguments[0]);
    let signature_path = Path::new(&arguments[1]);
    let size = fs::metadata(artifact)
        .map_err(|_| "Missing updater artifact")?
        .len();
    if size == 0
        || size
            > if metadata {
                64 * 1024
            } else {
                512 * 1024 * 1024
            }
        || fs::metadata(signature_path)
            .map_err(|_| "Missing updater signature")?
            .len()
            > 8192
    {
        return Err("Updater artifact or signature exceeds accepted bounds".into());
    }
    let bytes = fs::read(artifact).map_err(|_| "Cannot read updater artifact")?;
    let signature =
        fs::read_to_string(signature_path).map_err(|_| "Cannot read updater signature")?;
    let file = artifact
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or("Invalid artifact name")?;
    let binding = release
        .as_ref()
        .map(|release| (release[0].as_str(), release[1].as_str(), file));
    let (public_key, trusted_comment) = verify(&policy, &bytes, &signature, binding)?;
    let mut receipt = serde_json::json!({
        "file": file,
        "sha256": format!("{:x}", Sha256::digest(&bytes)), "size": size,
        "public_key": public_key, "verified": true,
        "key_kind": if metadata { "metadata" } else { "artifact" },
        "trusted_comment": trusted_comment,
    });
    if let Some((version, platform, file)) = binding {
        receipt["binding"] =
            serde_json::json!({"version": version, "platform": platform, "file": file});
    }
    println!("{receipt}");
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use minisign::{sign, KeyPair};
    use std::io::Cursor;

    const RELEASE: Binding = (
        "1.0.0",
        "linux-x86_64-appimage",
        "PlateFold_1.0.0_linux-x86_64.AppImage",
    );

    fn signed(keys: &KeyPair, bytes: &[u8], comment: Option<&str>) -> String {
        BASE64.encode(
            sign(None, &keys.sk, Cursor::new(bytes), comment, None)
                .unwrap()
                .to_string(),
        )
    }

    fn bound(keys: &KeyPair, bytes: &[u8]) -> String {
        let (version, platform, file) = RELEASE;
        let comment = binding::comment(version, platform, file).unwrap();
        signed(keys, bytes, Some(&comment))
    }

    fn policy(metadata: &KeyPair, artifact: &str) -> Policy {
        Policy {
            public_keys: vec![metadata.pk.to_base64()],
            artifact_public_keys: vec![artifact.into()],
            stable: Some("https://example.invalid/stable.json".into()),
            prerelease: None,
            artifact_url_prefix: "https://example.invalid/releases/download/".into(),
        }
    }

    // The release jobs and the feed builder run this tool against the policy the
    // app ships. It once refused that policy after a field was added to the app's
    // copy of the type and not to this one; both now read src/updates/policy.rs.
    #[test]
    fn the_compiled_policy_passes_the_release_checks() {
        validate_policy(&Policy::bundled().unwrap()).unwrap();
    }

    #[test]
    fn the_download_prefix_must_be_https_and_end_in_a_slash() {
        let metadata = KeyPair::generate_unencrypted_keypair().unwrap();
        let artifact = KeyPair::generate_unencrypted_keypair().unwrap();
        let mut policy = policy(&metadata, &artifact.pk.to_base64());
        assert!(validate_policy(&policy).is_ok());
        for prefix in [
            "https://example.invalid/releases/download",
            "http://example.invalid/releases/download/",
            "https://user@example.invalid/releases/download/",
            "https://example.invalid/releases/download/#x/",
        ] {
            policy.artifact_url_prefix = prefix.into();
            assert!(validate_policy(&policy).is_err(), "{prefix}");
        }
    }

    #[test]
    fn verifies_exact_artifact_and_rejects_wrong_key_and_mutation() {
        let keys = KeyPair::generate_unencrypted_keypair().unwrap();
        let metadata = KeyPair::generate_unencrypted_keypair().unwrap();
        let bytes = b"synthetic installer test bytes";
        let encoded = bound(&keys, bytes);
        let mut policy = policy(&metadata, &keys.pk.to_base64());
        assert!(verify(&policy, bytes, &encoded, Some(RELEASE)).is_ok());
        assert!(verify(&policy, bytes, &encoded, None).is_err());
        let metadata_signature = signed(&metadata, bytes, None);
        assert!(verify(&policy, bytes, &metadata_signature, None).is_ok());
        assert!(verify(&policy, bytes, &metadata_signature, Some(RELEASE)).is_err());
        assert!(verify(&policy, b"mutated final installer", &encoded, Some(RELEASE)).is_err());
        policy.artifact_public_keys = vec![KeyPair::generate_unencrypted_keypair()
            .unwrap()
            .pk
            .to_base64()];
        assert!(verify(&policy, bytes, &encoded, Some(RELEASE)).is_err());
        policy.artifact_public_keys.clear();
        assert!(validate_policy(&policy).is_err());
        policy.artifact_public_keys = policy.public_keys.clone();
        assert!(validate_policy(&policy).is_err());
    }

    #[test]
    fn an_artifact_must_name_the_version_platform_and_file_it_is_checked_as() {
        let keys = KeyPair::generate_unencrypted_keypair().unwrap();
        let metadata = KeyPair::generate_unencrypted_keypair().unwrap();
        let policy = policy(&metadata, &keys.pk.to_base64());
        let bytes = b"synthetic installer test bytes";
        let encoded = bound(&keys, bytes);
        let (key, comment) = verify(&policy, bytes, &encoded, Some(RELEASE)).unwrap();
        assert_eq!(key, keys.pk.to_base64());
        assert_eq!(
            comment,
            "platefold-artifact v1 version=1.0.0 platform=linux-x86_64-appimage file=PlateFold_1.0.0_linux-x86_64.AppImage"
        );
        let (version, platform, file) = RELEASE;
        for release in [
            ("1.0.1", platform, file),
            (version, "linux-aarch64-appimage", file),
            (version, platform, "PlateFold_1.0.1_linux-x86_64.AppImage"),
        ] {
            assert_eq!(
                verify(&policy, bytes, &encoded, Some(release)).unwrap_err(),
                binding::MISMATCH
            );
        }
        for comment in [
            None,
            Some("timestamp:1700000000\tfile:PlateFold_1.0.0_linux-x86_64.AppImage"),
        ] {
            assert_eq!(
                verify(
                    &policy,
                    bytes,
                    &signed(&keys, bytes, comment),
                    Some(RELEASE)
                )
                .unwrap_err(),
                binding::UNBOUND
            );
        }
        // A comment rewritten after signing fails the global signature.
        let document = String::from_utf8(BASE64.decode(signed(
            &keys,
            bytes,
            Some("platefold-artifact v1 version=0.9.0 platform=linux-x86_64-appimage file=PlateFold_1.0.0_linux-x86_64.AppImage"),
        ))
        .unwrap())
        .unwrap()
        .replace("version=0.9.0", "version=1.0.0");
        assert!(
            verify(&policy, bytes, &BASE64.encode(document), Some(RELEASE))
                .unwrap_err()
                .contains("does not match the compiled policy")
        );
    }

    #[test]
    fn refuses_the_legacy_signature_mode_the_app_refuses() {
        // This is the public key from the published Minisign legacy-mode test vector.
        let artifact_key = "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3"; // gitleaks:allow
        let legacy_signature = "untrusted comment: signature from minisign secret key\n\
RWQf6LRCGA9i59SLOFxz6NxvASXDJeRtuZykwQepbDEGt87ig1BNpWaVWuNrm73YiIiJbq71Wi+dP9eKL8OC351vwIasSSbXxwA=\n\
trusted comment: timestamp:1555779966\tfile:test\n\
QtKMXWyYcwdpZAlPF7tE2ENJkRd1ujvKjlj1m9RtHTBnZPa5WKU5uWRs5GoP5M/VqE81QFuMKI5k/SfNQUaOAA==";
        let metadata = KeyPair::generate_unencrypted_keypair().unwrap();
        let policy = policy(&metadata, artifact_key);
        // The app verifies artifacts and metadata with legacy mode disallowed.
        let key = PublicKey::from_base64(artifact_key).unwrap();
        let signature = Signature::decode(legacy_signature).unwrap();
        assert!(key.verify(b"test", &signature, true).is_ok());
        assert!(key.verify(b"test", &signature, false).is_err());
        assert!(verify(
            &policy,
            b"test",
            &BASE64.encode(legacy_signature),
            Some(("1.0.0", "linux-x86_64-appimage", "test"))
        )
        .is_err());
    }
}
