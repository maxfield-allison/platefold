// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT

//! Folders an installation could not delete.

use std::{
    path::PathBuf,
    sync::{Mutex, PoisonError},
};

/// Collects the staging folders an installation could not delete.
///
/// Cleanup never changes the installation's own result: a successful
/// installation stays successful and a failed one keeps its error. The
/// application reads the folders afterwards with [`CleanupReport::take`] so
/// that it can tell the user which ones to delete.
#[derive(Debug, Default)]
pub struct CleanupReport {
    left: Mutex<Vec<PathBuf>>,
}

impl CleanupReport {
    pub fn new() -> Self {
        Self::default()
    }

    /// Removes and returns the folders recorded so far.
    pub fn take(&self) -> Vec<PathBuf> {
        std::mem::take(&mut *self.left.lock().unwrap_or_else(PoisonError::into_inner))
    }

    /// Records a folder that could not be deleted.
    // Only the staged replacements on macOS, Linux and the BSDs record anything; Windows hands
    // the installer to NSIS or MSI.
    #[cfg_attr(
        not(any(
            target_os = "macos",
            target_os = "linux",
            target_os = "dragonfly",
            target_os = "freebsd",
            target_os = "netbsd",
            target_os = "openbsd"
        )),
        allow(dead_code)
    )]
    pub(crate) fn record(&self, folder: PathBuf) {
        self.left
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(folder);
    }
}
