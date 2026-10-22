//! The update policy compiled into the app and into the release verifier.
//!
//! The app and `examples/verify_updater_artifact.rs` both read
//! `packaging/update-policy.json` through this one type, the same way they share
//! `binding.rs`. It refuses unknown fields, so a field added to the policy file
//! must be added here once, and both read it.
use serde::Deserialize;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Policy {
    // Metadata and artifacts deliberately have separate trust roots.
    pub(super) public_keys: Vec<String>,
    pub(super) artifact_public_keys: Vec<String>,
    pub(super) stable: Option<String>,
    pub(super) prerelease: Option<String>,
    // Where every release artifact is downloaded from; see manifest::Trust.
    pub(super) artifact_url_prefix: String,
}

impl Policy {
    pub(super) fn bundled() -> Result<Self, String> {
        serde_json::from_str(include_str!("../../../../packaging/update-policy.json"))
            .map_err(|_| "The update policy is invalid".into())
    }
}
