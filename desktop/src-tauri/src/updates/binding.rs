//! The release an updater artifact's signature names.
//!
//! The update feed is signed by the metadata key, which the daily renewal job
//! holds. If the feed alone said which version a file is, that job could offer
//! a genuine, older signed installer as a newer version. So the release job
//! signs every updater artifact with a Minisign trusted comment that names its
//! version, update target and file name. Minisign's global signature covers the
//! trusted comment, so only the artifact key can write it, and the app installs
//! a file only when the comment equals the release the feed offers.
//!
//! This file is shared, through `#[path]`, with the signer and verifier
//! examples, so the release tools and the app use one definition.

/// The scheme and format version that start every bound trusted comment.
pub const SCHEME: &str = "platefold-artifact v1";

pub const UNBOUND: &str = "The update's installer signature does not name its version, platform and file, so PlateFold will not install it";
pub const MISMATCH: &str = "The update's installer is signed for a different version, platform or file than the update offers, so PlateFold will not install it";
const INVALID: &str = "Invalid release artifact details";

fn token(value: &str, punctuation: &[u8]) -> bool {
    (1..=128).contains(&value.len())
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || punctuation.contains(&byte))
}

/// The trusted comment for one artifact. Every part is one space-free token,
/// so no two different releases can produce the same comment.
pub fn comment(version: &str, platform: &str, file: &str) -> Result<String, &'static str> {
    if !token(version, b".-") || !token(platform, b"_-") || !token(file, b"_.-") {
        return Err(INVALID);
    }
    Ok(format!(
        "{SCHEME} version={version} platform={platform} file={file}"
    ))
}

/// Require a signature's trusted comment to name exactly this release.
///
/// The comment is authentic only once the signature has been verified with the
/// pinned artifact key over the artifact's bytes; callers check it again then.
pub fn check(trusted: &str, version: &str, platform: &str, file: &str) -> Result<(), &'static str> {
    let expected = comment(version, platform, file)?;
    if !trusted
        .strip_prefix(SCHEME)
        .is_some_and(|rest| rest.starts_with(' '))
    {
        return Err(UNBOUND);
    }
    if trusted != expected {
        return Err(MISMATCH);
    }
    Ok(())
}
