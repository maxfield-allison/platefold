// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT

//! The point after which an installation changes the installed application.

use std::{
    io,
    sync::atomic::{AtomicU8, Ordering},
};

const GATE_OPEN: u8 = 0;
const GATE_COMMITTING: u8 = 1;
const GATE_ABANDONED: u8 = 2;

/// Decides whether an installation may still change the installed path.
///
/// An installation enters the gate for the steps that replace files at the
/// installed path, and leaves it when they end. An application that has to
/// stop waiting for a slow installation calls [`CommitGate::abandon`]: once it
/// succeeds, no replacement through this gate starts.
#[derive(Debug, Default)]
pub struct CommitGate {
    state: AtomicU8,
}

impl CommitGate {
    pub fn new() -> Self {
        Self::default()
    }

    /// Stops any replacement that has not started.
    ///
    /// Returns `false` while replacement or installer launch holds the gate.
    /// Returns `true` once no operation through this gate is running or can
    /// start. The caller may retry after a running operation finishes.
    pub fn abandon(&self) -> bool {
        match self.state.compare_exchange(
            GATE_OPEN,
            GATE_ABANDONED,
            Ordering::SeqCst,
            Ordering::SeqCst,
        ) {
            Ok(_) => true,
            Err(current) => current == GATE_ABANDONED,
        }
    }

    /// Marks the replacement step as running, or refuses if it was abandoned.
    pub(crate) fn enter(&self) -> io::Result<Committing<'_>> {
        match self.state.compare_exchange(
            GATE_OPEN,
            GATE_COMMITTING,
            Ordering::SeqCst,
            Ordering::SeqCst,
        ) {
            Ok(_) => Ok(Committing(self)),
            Err(_) => Err(io::Error::new(
                io::ErrorKind::Interrupted,
                "replacement is already running or the update has been abandoned",
            )),
        }
    }
}

/// The replacement step, running while this value lives.
pub(crate) struct Committing<'a>(&'a CommitGate);

impl Drop for Committing<'_> {
    fn drop(&mut self) {
        self.0.state.store(GATE_OPEN, Ordering::SeqCst);
    }
}
