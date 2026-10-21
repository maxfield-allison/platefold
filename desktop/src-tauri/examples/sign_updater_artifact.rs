//! Sign one updater artifact for release, bound to its version, target and file.
//!
//! The protected release jobs run this after building an updater artifact under
//! its final name. The artifact key comes from TAURI_SIGNING_PRIVATE_KEY (base64
//! of a Minisign secret-key document, as Tauri's signer takes it) and its
//! password from TAURI_SIGNING_PRIVATE_KEY_PASSWORD. It writes FILE.sig in
//! Tauri's format, base64 of the signature document, whose trusted comment names
//! the release (src/updates/binding.rs). The app installs the file only when that
//! comment equals the version, target and file its update feed offers.
//!
//! Tauri's own signer writes a timestamp and file name there and cannot be told
//! otherwise, so its signature is replaced. Nothing here prints key material.
#[path = "../src/updates/binding.rs"]
mod binding;

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use minisign::{sign, PublicKey, SecretKey, SecretKeyBox};
use serde::Deserialize;
use std::{fs, io::Write, path::Path};

const MAX_ARTIFACT: u64 = 512 * 1024 * 1024;
const USAGE: &str = "Usage: sign_updater_artifact --version VERSION --platform PLATFORM FILE";
const KEY_ERROR: &str =
    "The artifact signing key or its password is invalid; no signature was written";

#[derive(Deserialize)]
struct Policy {
    artifact_public_keys: Vec<String>,
}

/// Decode Tauri's key format. An unencrypted key is accepted only with an
/// empty password; the password always comes from the environment, so the
/// Minisign crate never prompts.
fn secret_key(encoded: &str, password: &str) -> Result<SecretKey, String> {
    let document = BASE64
        .decode(encoded.trim())
        .ok()
        .and_then(|bytes| String::from_utf8(bytes).ok())
        .ok_or(KEY_ERROR)?;
    let boxed = SecretKeyBox::from_string(&document).map_err(|_| KEY_ERROR)?;
    match SecretKey::from_box(boxed.clone(), Some(password.to_owned())) {
        Ok(key) => Ok(key),
        Err(_) if password.is_empty() => {
            SecretKey::from_unencrypted_box(boxed).map_err(|_| KEY_ERROR.into())
        }
        Err(_) => Err(KEY_ERROR.into()),
    }
}

/// Sign an artifact's bytes; return the Tauri-format signature and its comment.
fn sign_artifact(
    data: impl std::io::Read,
    key: &SecretKey,
    version: &str,
    platform: &str,
    file: &str,
) -> Result<(String, String), String> {
    let comment = binding::comment(version, platform, file)?;
    let public = PublicKey::from_secret_key(key).map_err(|_| KEY_ERROR)?;
    // With the public key, sign() also checks the global signature it made.
    let document = sign(
        Some(&public),
        key,
        data,
        Some(&comment),
        Some("signature from PlateFold release artifact key"),
    )
    .map_err(|_| "Signing the updater artifact failed; no signature was written")?
    .to_string();
    let decoded = minisign_verify::Signature::decode(&document)
        .map_err(|_| "The new signature could not be read back")?;
    binding::check(decoded.trusted_comment(), version, platform, file)?;
    Ok((BASE64.encode(document), comment))
}

fn run() -> Result<(), String> {
    let arguments: Vec<String> = std::env::args_os()
        .skip(1)
        .map(|value| value.into_string().map_err(|_| USAGE.to_string()))
        .collect::<Result<_, _>>()?;
    let [flag_version, version, flag_platform, platform, path] = arguments.as_slice() else {
        return Err(USAGE.into());
    };
    if flag_version != "--version" || flag_platform != "--platform" {
        return Err(USAGE.into());
    }
    let path = Path::new(path);
    let metadata = fs::symlink_metadata(path).map_err(|_| "Missing updater artifact")?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > MAX_ARTIFACT {
        return Err(
            "The updater artifact must be an ordinary file within the app's size limit".into(),
        );
    }
    let file = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or("Invalid artifact name")?;
    let key = secret_key(
        &std::env::var("TAURI_SIGNING_PRIVATE_KEY").map_err(|_| KEY_ERROR)?,
        &std::env::var("TAURI_SIGNING_PRIVATE_KEY_PASSWORD").unwrap_or_default(),
    )?;
    let policy: Policy =
        serde_json::from_str(include_str!("../../../packaging/update-policy.json"))
            .map_err(|_| "Invalid compiled updater policy")?;
    let public = PublicKey::from_secret_key(&key).map_err(|_| KEY_ERROR)?;
    if !policy.artifact_public_keys.contains(&public.to_base64()) {
        return Err(
            "The signing key is not an artifact key in the compiled update policy; use the artifact key, not the metadata key"
                .into(),
        );
    }
    let artifact = fs::File::open(path).map_err(|_| "Cannot read updater artifact")?;
    let (signature, comment) = sign_artifact(artifact, &key, version, platform, file)?;
    let folder = path.parent().ok_or("Invalid artifact path")?;
    let mut output =
        tempfile::NamedTempFile::new_in(folder).map_err(|_| "Cannot write the signature")?;
    output
        .write_all(signature.as_bytes())
        .and_then(|_| output.as_file().sync_all())
        .map_err(|_| "Cannot write the signature")?;
    let target = folder.join(format!("{file}.sig"));
    output
        .persist(&target)
        .map_err(|_| "Cannot write the signature")?;
    println!(
        "{}",
        serde_json::json!({"file": file, "signature": format!("{file}.sig"), "trusted_comment": comment})
    );
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
    use minisign::KeyPair;
    use minisign_verify::{PublicKey as VerifyKey, Signature};
    use std::io::Cursor;

    const FILE: &str = "PlateFold_1.0.0_x64-online-setup.exe";

    fn tauri_key(keys: &KeyPair) -> String {
        BASE64.encode(keys.sk.to_box(None).unwrap().to_string())
    }

    fn verify(keys: &KeyPair, bytes: &[u8], encoded: &str) -> Result<String, String> {
        let document = String::from_utf8(BASE64.decode(encoded).unwrap()).unwrap();
        let signature = Signature::decode(&document).unwrap();
        VerifyKey::from_base64(&keys.pk.to_base64())
            .unwrap()
            .verify(bytes, &signature, false)
            .map_err(|_| "signature".to_string())?;
        Ok(signature.trusted_comment().to_owned())
    }

    #[test]
    fn signs_prehashed_with_the_release_binding_as_trusted_comment() {
        let keys = KeyPair::generate_unencrypted_keypair().unwrap();
        let key = secret_key(&tauri_key(&keys), "").unwrap();
        let bytes = b"synthetic installer";
        let (signature, comment) = sign_artifact(
            Cursor::new(bytes),
            &key,
            "1.0.0",
            "windows-x86_64-nsis",
            FILE,
        )
        .unwrap();
        assert_eq!(
            comment,
            "platefold-artifact v1 version=1.0.0 platform=windows-x86_64-nsis file=PlateFold_1.0.0_x64-online-setup.exe"
        );
        assert_eq!(verify(&keys, bytes, &signature).unwrap(), comment);
        assert!(verify(&keys, b"other installer", &signature).is_err());
        assert!(
            binding::check(&comment, "1.0.0", "windows-x86_64-nsis", FILE).is_ok()
                && binding::check(&comment, "1.0.1", "windows-x86_64-nsis", FILE).is_err()
        );
    }

    #[test]
    fn an_encrypted_key_needs_its_password_and_bad_parts_are_refused() {
        let keys =
            KeyPair::generate_encrypted_keypair(Some("disposable test password".into())).unwrap();
        let encoded = tauri_key(&keys);
        assert_eq!(secret_key(&encoded, "wrong").err().unwrap(), KEY_ERROR);
        assert_eq!(secret_key(&encoded, "").err().unwrap(), KEY_ERROR);
        assert_eq!(secret_key("not a key", "").err().unwrap(), KEY_ERROR);
        let key = secret_key(&encoded, "disposable test password").unwrap();
        let (signature, _) = sign_artifact(
            Cursor::new(b"bytes"),
            &key,
            "1.0.0",
            "linux-x86_64-appimage",
            "a.AppImage",
        )
        .unwrap();
        assert!(verify(&keys, b"bytes", &signature).is_ok());
        for (version, platform, file) in [
            ("1.0.0 extra", "linux-x86_64-appimage", "a.AppImage"),
            ("1.0.0", "linux x86_64", "a.AppImage"),
            ("1.0.0", "linux-x86_64-appimage", "a b.AppImage"),
        ] {
            assert!(sign_artifact(Cursor::new(b"bytes"), &key, version, platform, file).is_err());
        }
    }
}
