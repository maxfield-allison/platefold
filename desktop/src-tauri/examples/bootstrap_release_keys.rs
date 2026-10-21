//! One-time Unix custody-host bootstrap. Never prints keys or passwords.
#[cfg(unix)]
mod bootstrap {
    use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
    use minisign::{sign, KeyPair, SecretKey, SecretKeyBox};
    use minisign_verify::{PublicKey, Signature};
    use serde_json::{json, Value};
    use std::{
        fs::{self, OpenOptions},
        io::{Cursor, Write},
        os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
        path::{Path, PathBuf},
    };

    const ROLES: [&str; 3] = ["artifact", "metadata", "provenance"];
    const PROOF: &[u8] = b"PlateFold release-key custody restore proof v1\n";

    fn custody_directory(path: &Path) -> Result<PathBuf, String> {
        let metadata = fs::symlink_metadata(path).map_err(|_| "Destination must already exist")?;
        if !metadata.is_dir() || metadata.permissions().mode() & 0o777 != 0o700 {
            return Err("Destination must be a real directory with permissions 0700".into());
        }
        // SAFETY: geteuid has no parameters or memory-safety preconditions.
        if metadata.uid() != unsafe { libc::geteuid() } {
            return Err("Destination must belong to the current user".into());
        }
        let path = path
            .canonicalize()
            .map_err(|_| "Cannot resolve destination")?;
        if path.ancestors().any(|parent| {
            let git = parent.join(".git");
            git.is_file()
                || (git.is_dir()
                    && git.join("HEAD").is_file()
                    && (git.join("config").is_file()
                        || git.join("objects").is_dir()
                        || git.join("commondir").is_file()))
        }) {
            return Err("Destination must be outside Git repositories and worktrees".into());
        }
        Ok(path)
    }

    fn destination(path: &Path) -> Result<PathBuf, String> {
        let path = custody_directory(path)?;
        for role in ROLES {
            for suffix in ["key", "pub"] {
                if fs::symlink_metadata(path.join(format!("{role}.{suffix}"))).is_ok() {
                    return Err(
                        "Destination already contains a key output; refusing overwrite".into(),
                    );
                }
            }
        }
        if fs::symlink_metadata(path.join("PUBLIC-KEYS.json")).is_ok() {
            return Err("Destination already contains a receipt; refusing overwrite".into());
        }
        Ok(path)
    }

    fn passwords(values: &[String; 3]) -> Result<(), String> {
        if values
            .iter()
            .any(|value| value.len() < 24 || value.len() > 1024)
        {
            return Err("Each password must contain 24 to 1024 bytes".into());
        }
        if values[0] == values[1] || values[0] == values[2] || values[1] == values[2] {
            return Err("Each key requires an independent password".into());
        }
        Ok(())
    }

    fn save(path: &Path, bytes: &[u8]) -> Result<(), String> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .map_err(|_| "Cannot create new custody file; existing files are preserved")?;
        file.write_all(bytes)
            .map_err(|_| "Cannot write custody file")?;
        file.sync_all()
            .map_err(|_| "Cannot synchronize custody file")?;
        Ok(())
    }

    fn read_custody_file(path: &Path) -> Result<Vec<u8>, String> {
        let metadata = fs::symlink_metadata(path).map_err(|_| "Missing custody file")?;
        // SAFETY: geteuid has no parameters or memory-safety preconditions.
        if !metadata.is_file()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.permissions().mode() & 0o777 != 0o600
            || metadata.len() > 8192
        {
            return Err(
                "Custody files must be regular owner-only 0600 files of bounded size".into(),
            );
        }
        fs::read(path).map_err(|_| "Cannot read custody file".into())
    }

    fn restore(path: &Path, password: &str) -> Result<SecretKey, String> {
        let encoded = read_custody_file(path)?;
        let document = BASE64
            .decode(encoded)
            .map_err(|_| "Invalid encrypted-key encoding")?;
        let document = String::from_utf8(document).map_err(|_| "Invalid encrypted-key document")?;
        let boxed = SecretKeyBox::from_string(&document).map_err(|_| "Invalid encrypted key")?;
        SecretKey::from_box(boxed, Some(password.to_owned()))
            .map_err(|_| "Encrypted-key restore failed".into())
    }

    fn create(path: &Path, values: &[String; 3]) -> Result<Value, String> {
        passwords(values)?;
        let directory = destination(path)?;
        for (role, password) in ROLES.iter().zip(values) {
            let pair = KeyPair::generate_encrypted_keypair(Some(password.clone()))
                .map_err(|_| "Encrypted-key generation failed")?;
            if !pair.sk.is_encrypted() {
                return Err("Refusing to persist an unencrypted private key".into());
            }
            let boxed = pair
                .sk
                .to_box(None)
                .map_err(|_| "Cannot serialize encrypted key")?;
            let public = pair
                .pk
                .to_box()
                .map_err(|_| "Cannot serialize public key")?;
            let key_path = directory.join(format!("{role}.key"));
            save(&key_path, BASE64.encode(boxed.to_bytes()).as_bytes())?;
            save(&directory.join(format!("{role}.pub")), &public.to_bytes())?;
        }
        let receipt = verify(&directory, values)?;
        let document =
            serde_json::to_vec_pretty(&receipt).map_err(|_| "Cannot serialize receipt")?;
        save(&directory.join("PUBLIC-KEYS.json"), &document)?;
        Ok(receipt)
    }

    fn verify(path: &Path, values: &[String; 3]) -> Result<Value, String> {
        passwords(values)?;
        let directory = custody_directory(path)?;
        let mut receipts = Vec::new();
        for (role, password) in ROLES.iter().zip(values) {
            let public_bytes = read_custody_file(&directory.join(format!("{role}.pub")))?;
            let public_document =
                String::from_utf8(public_bytes).map_err(|_| "Invalid public-key document")?;
            let public_box = minisign::PublicKeyBox::from_string(&public_document)
                .map_err(|_| "Invalid public-key document")?;
            let public =
                minisign::PublicKey::from_box(public_box).map_err(|_| "Invalid public key")?;
            let key_path = directory.join(format!("{role}.key"));
            let restored = restore(&key_path, password)?;
            let signature = sign(Some(&public), &restored, Cursor::new(PROOF), None, None)
                .map_err(|_| "Restored-key signing proof failed")?;
            let verifier = PublicKey::from_base64(&public.to_base64())
                .map_err(|_| "Public-key verification setup failed")?;
            let signature = Signature::decode(&signature.to_string())
                .map_err(|_| "Signature serialization proof failed")?;
            verifier
                .verify(PROOF, &signature, false)
                .map_err(|_| "Restored-key verification proof failed")?;
            if verifier
                .verify(b"modified custody proof", &signature, false)
                .is_ok()
            {
                return Err("Signature mutation rejection failed".into());
            }
            receipts.push(json!({
                "purpose": role, "public_key": public.to_base64(),
                "private_file": format!("{role}.key"), "public_file": format!("{role}.pub"),
                "encrypted": true, "restored_from_disk": true,
                "sign_verify_passed": true, "mutation_rejected": true,
            }));
        }
        for a in 0..receipts.len() {
            for b in a + 1..receipts.len() {
                if receipts[a]["public_key"] == receipts[b]["public_key"] {
                    return Err("Generated keys are not independent".into());
                }
            }
        }
        let receipt = json!({"schema_version": 1, "algorithm": "minisign-ed25519",
            "private_encoding": "base64-minisign-encrypted-secret-key-box",
            "synthetic_restore_proof": true, "keys": receipts});
        Ok(receipt)
    }

    pub fn run() -> Result<(), String> {
        let mut arguments: Vec<_> = std::env::args_os().skip(1).collect();
        let verify_only = arguments.first().is_some_and(|value| value == "--verify");
        if verify_only {
            arguments.remove(0);
        }
        if arguments.len() != 1 {
            return Err(
                "Usage: bootstrap_release_keys [--verify] EXISTING_SECURE_DIRECTORY".into(),
            );
        }
        let limits = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        // SAFETY: the pointer refers to a valid rlimit for this call.
        if unsafe { libc::setrlimit(libc::RLIMIT_CORE, &limits) } != 0 {
            return Err("Cannot disable core dumps before loading passwords".into());
        }
        let mut values = [String::new(), String::new(), String::new()];
        for (index, role) in ROLES.iter().enumerate() {
            let name = format!("PF_{}_KEY_PASSWORD", role.to_uppercase());
            values[index] = std::env::var(&name).map_err(|_| format!("Missing {name}"))?;
            std::env::remove_var(name);
        }
        let receipt = if verify_only {
            verify(Path::new(&arguments[0]), &values)?
        } else {
            create(Path::new(&arguments[0]), &values)?
        };
        println!("{receipt}");
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn encrypted_disk_restore_and_no_overwrite() {
            let folder = tempfile::tempdir().unwrap();
            fs::set_permissions(folder.path(), fs::Permissions::from_mode(0o700)).unwrap();
            let values =
                std::array::from_fn(|i| format!("synthetic-only-not-production-password-{i}"));
            let receipt = create(folder.path(), &values).unwrap();
            assert_eq!(receipt["keys"].as_array().unwrap().len(), 3);
            let snapshots: Vec<_> = fs::read_dir(folder.path())
                .unwrap()
                .map(|entry| {
                    let path = entry.unwrap().path();
                    let metadata = fs::metadata(&path).unwrap();
                    (
                        path.clone(),
                        fs::read(&path).unwrap(),
                        metadata.modified().unwrap(),
                    )
                })
                .collect();
            assert_eq!(verify(folder.path(), &values).unwrap(), receipt);
            for (path, bytes, modified) in snapshots {
                assert_eq!(fs::read(&path).unwrap(), bytes);
                assert_eq!(fs::metadata(path).unwrap().modified().unwrap(), modified);
            }
            let key = folder.path().join("artifact.key");
            assert_eq!(
                fs::metadata(&key).unwrap().permissions().mode() & 0o777,
                0o600
            );
            let mut wrong_passwords = values.clone();
            wrong_passwords[0] = "incorrect synthetic password".into();
            assert!(verify(folder.path(), &wrong_passwords).is_err());
            let before = fs::read(&key).unwrap();
            assert!(create(folder.path(), &values).is_err());
            assert_eq!(fs::read(&key).unwrap(), before);
            fs::write(&key, b"corrupted private key").unwrap();
            assert!(verify(folder.path(), &values).is_err());
            fs::write(&key, before).unwrap();
            let public = folder.path().join("artifact.pub");
            fs::copy(folder.path().join("metadata.pub"), &public).unwrap();
            assert!(verify(folder.path(), &values).is_err());
            assert!(
                passwords(&["too short".into(), values[1].clone(), values[2].clone()]).is_err()
            );
            assert!(passwords(&[values[0].clone(), values[0].clone(), values[2].clone()]).is_err());
        }

        #[test]
        fn rejects_repo_permissions_and_symlink_destinations() {
            let folder = tempfile::tempdir().unwrap();
            fs::set_permissions(folder.path(), fs::Permissions::from_mode(0o755)).unwrap();
            assert!(destination(folder.path()).is_err());
            fs::set_permissions(folder.path(), fs::Permissions::from_mode(0o700)).unwrap();
            let alias = folder.path().join("alias");
            std::os::unix::fs::symlink(folder.path(), &alias).unwrap();
            assert!(destination(&alias).is_err());
            let key = folder.path().join("artifact.key");
            std::os::unix::fs::symlink("missing-target", &key).unwrap();
            assert!(destination(folder.path()).is_err());
            fs::remove_file(key).unwrap();
            fs::write(folder.path().join(".git"), "gitdir: elsewhere").unwrap();
            assert!(destination(folder.path()).is_err());
        }

        #[test]
        fn permits_inert_git_placeholder_but_rejects_repository_metadata() {
            let folder = tempfile::tempdir().unwrap();
            fs::set_permissions(folder.path(), fs::Permissions::from_mode(0o700)).unwrap();
            let custody = folder.path().join("custody");
            fs::create_dir(&custody).unwrap();
            fs::set_permissions(&custody, fs::Permissions::from_mode(0o700)).unwrap();
            let git = folder.path().join(".git");
            fs::create_dir(&git).unwrap();
            assert!(destination(&custody).is_ok());
            fs::write(git.join("HEAD"), "ref: refs/heads/main\n").unwrap();
            fs::write(git.join("config"), "[core]\nrepositoryformatversion = 0\n").unwrap();
            assert!(destination(&custody).is_err());
            fs::remove_file(git.join("config")).unwrap();
            fs::create_dir(git.join("objects")).unwrap();
            assert!(destination(&custody).is_err());
        }
    }
}

fn main() {
    #[cfg(unix)]
    if let Err(error) = bootstrap::run() {
        eprintln!("{error}; any existing outputs remain preserved");
        std::process::exit(1);
    }
    #[cfg(not(unix))]
    {
        eprintln!("Key custody bootstrap requires a Unix host with owner-only file permissions");
        std::process::exit(1);
    }
}
