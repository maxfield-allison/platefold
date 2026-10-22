// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT

//! Replacing an installed application without leaving its path empty.
//!
//! The installed path changes in one step, so an exit at any moment leaves
//! either the previous or the new copy there. A Linux AppImage is a single
//! file and is replaced by one rename onto it ([`replace_file`]). A macOS app
//! bundle is a directory; it is exchanged with the staged bundle in one call,
//! after which the previous copy is deleted from the staged path
//! ([`replace_bundle`]). A volume that cannot exchange two names is refused:
//! nothing is moved, and the app is updated by hand. The filesystem
//! operations are injected so that a failure at each step can be tested
//! deterministically.
//!
//! Every replacement passes through a [`CommitGate`]. The application can
//! abandon an installation that has not reached its replacement step, and it
//! can tell when that step is running and must be allowed to finish.

// The exchange serves the macOS bundle. Elsewhere only the single-file rename
// runs, and the tests exercise the rest.
#![cfg_attr(not(target_os = "macos"), allow(dead_code))]

use std::{ffi::OsStr, io, path::Path};

use super::{cleanup::CleanupReport, gate::CommitGate};

/// The filesystem operations the replacement uses.
pub(crate) trait ReplaceFs {
    /// Atomically renames `from` to `to` on one filesystem.
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()>;
    /// Removes a file or a directory tree.
    fn remove_all(&self, path: &Path) -> io::Result<()>;
    /// Atomically exchanges two existing paths on one filesystem. Fails with
    /// [`io::ErrorKind::Unsupported`], changing nothing, where the filesystem
    /// cannot.
    fn swap(&self, a: &Path, b: &Path) -> io::Result<()>;
}

/// The real filesystem.
pub(crate) struct StdFs;

impl ReplaceFs for StdFs {
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        std::fs::rename(from, to)
    }

    fn remove_all(&self, path: &Path) -> io::Result<()> {
        if std::fs::symlink_metadata(path)?.is_dir() {
            std::fs::remove_dir_all(path)
        } else {
            std::fs::remove_file(path)
        }
    }

    #[cfg(target_os = "macos")]
    fn swap(&self, a: &Path, b: &Path) -> io::Result<()> {
        macos::swap(a, b)
    }

    #[cfg(not(target_os = "macos"))]
    fn swap(&self, _a: &Path, _b: &Path) -> io::Result<()> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "exchanging two paths is supported only on macOS",
        ))
    }
}

#[cfg(target_os = "macos")]
mod macos {
    use std::{
        ffi::{c_char, c_int, c_uint, CString},
        io,
        os::unix::ffi::OsStrExt,
        path::Path,
    };

    // From <stdio.h>, available since macOS 10.12. Declared here rather than
    // through the libc crate to keep this patch's dependency set unchanged.
    const RENAME_SWAP: c_uint = 0x0000_0002;
    // <sys/errno.h>: what renamex_np reports on a volume that cannot swap.
    const ENOTSUP: i32 = 45;
    const EINVAL: i32 = 22;

    extern "C" {
        fn renamex_np(from: *const c_char, to: *const c_char, flags: c_uint) -> c_int;
    }

    pub(super) fn swap(a: &Path, b: &Path) -> io::Result<()> {
        let a = CString::new(a.as_os_str().as_bytes())?;
        let b = CString::new(b.as_os_str().as_bytes())?;
        // SAFETY: both arguments are NUL-terminated strings that outlive the call.
        if unsafe { renamex_np(a.as_ptr(), b.as_ptr(), RENAME_SWAP) } == 0 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        match error.raw_os_error() {
            Some(ENOTSUP) | Some(EINVAL) => Err(io::Error::new(io::ErrorKind::Unsupported, error)),
            _ => Err(error),
        }
    }
}

/// A completed replacement.
#[derive(Debug)]
pub(crate) enum Replaced {
    /// The new copy is installed and the previous copy was deleted.
    Clean,
    /// The new copy is installed, but the previous copy could not be deleted
    /// from the staged path.
    BackupLeft,
}

/// A replacement that did not install the new copy.
#[derive(Debug)]
pub(crate) enum ReplaceFailure {
    /// The replacement did not change the installed copy.
    Unchanged(io::Error),
    /// The volume cannot exchange the two bundles, so nothing was moved.
    SwapUnsupported,
}

impl From<ReplaceFailure> for crate::Error {
    fn from(failure: ReplaceFailure) -> Self {
        match failure {
            ReplaceFailure::Unchanged(error) => crate::Error::Io(error),
            ReplaceFailure::SwapUnsupported => {
                crate::Error::ReplacementUnsupported { staging_left: None }
            }
        }
    }
}

/// Splits the installed path into its parent directory and file name.
pub(crate) fn parent_and_name(dest: &Path) -> io::Result<(&Path, &OsStr)> {
    match (dest.parent(), dest.file_name()) {
        (Some(parent), Some(name)) if !parent.as_os_str().is_empty() => Ok((parent, name)),
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("cannot replace {}: it has no parent folder", dest.display()),
        )),
    }
}

/// The prefix of the directory the new copy is staged in.
pub(crate) fn staging_prefix(name: &OsStr) -> String {
    format!(".{}.update-", name.to_string_lossy())
}

/// Replaces the file at `dest` with `staged` in one rename.
///
/// `staged` must be a file on the same filesystem as `dest`. The rename
/// replaces the name in one step, so `dest` names the previous or the new file
/// at every moment. A failure leaves both files as they were.
// The AppImage path; a macOS bundle is a directory and uses swap_replace.
#[cfg_attr(target_os = "macos", allow(dead_code))]
pub(crate) fn replace_file(
    fs: &impl ReplaceFs,
    gate: &CommitGate,
    staged: &Path,
    dest: &Path,
) -> Result<Replaced, ReplaceFailure> {
    let _committing = gate.enter().map_err(ReplaceFailure::Unchanged)?;
    fs.rename(staged, dest).map_err(ReplaceFailure::Unchanged)?;
    Ok(Replaced::Clean)
}

/// Exchanges the staged directory with the installed one in one step, then
/// deletes the previous copy, which the exchange left at `staged`.
///
/// Returns `Ok(None)` without changing anything where the filesystem cannot
/// exchange the two. Any other failure of the exchange also leaves both
/// copies where they were.
pub(crate) fn swap_replace(
    fs: &impl ReplaceFs,
    gate: &CommitGate,
    staged: &Path,
    dest: &Path,
) -> Result<Option<Replaced>, ReplaceFailure> {
    {
        let _committing = gate.enter().map_err(ReplaceFailure::Unchanged)?;
        match fs.swap(staged, dest) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::Unsupported => return Ok(None),
            Err(error) => return Err(ReplaceFailure::Unchanged(error)),
        }
    }
    // The new copy is installed; deleting the previous one is cleanup.
    match fs.remove_all(staged) {
        Ok(()) => Ok(Some(Replaced::Clean)),
        Err(_) => Ok(Some(Replaced::BackupLeft)),
    }
}

/// Replaces the installed bundle at `dest` with `staged` by exchanging them.
///
/// Refuses with [`ReplaceFailure::SwapUnsupported`], having moved nothing,
/// where the volume cannot exchange two names. Moving the installed bundle
/// aside instead would leave its path empty until the new one is moved in,
/// and a forced quit in between would leave no app to launch.
pub(crate) fn replace_bundle(
    fs: &impl ReplaceFs,
    gate: &CommitGate,
    staged: &Path,
    dest: &Path,
) -> Result<Replaced, ReplaceFailure> {
    swap_replace(fs, gate, staged, dest)?.ok_or(ReplaceFailure::SwapUnsupported)
}

/// Attempts to delete the staging folder after an installation attempt.
///
/// Dropping a `tempfile::TempDir` discards deletion errors. If cleanup fails
/// after a refused exchange, the refusal names the unused staging folder so
/// that the user can delete it. Cleanup may already have deleted some contents.
/// Successful cleanup changes a successful installation result to
/// `Replaced::Clean`. If cleanup fails after any other result, that result is
/// returned unchanged and `report` records the folder for the application to
/// show. A previous copy the exchange could not delete is inside the folder,
/// so the folder covers it.
pub(crate) fn close_staging(
    fs: &impl ReplaceFs,
    staging: &Path,
    attempt: crate::Result<Replaced>,
    report: &CleanupReport,
) -> crate::Result<Replaced> {
    if fs.remove_all(staging).is_ok() {
        return attempt.map(|_| Replaced::Clean);
    }
    if let Err(crate::Error::ReplacementUnsupported { .. }) = attempt {
        return Err(crate::Error::ReplacementUnsupported {
            staging_left: Some(staging.to_path_buf()),
        });
    }
    report.record(staging.to_path_buf());
    attempt
}

#[cfg(all(test, unix))]
mod tests;
