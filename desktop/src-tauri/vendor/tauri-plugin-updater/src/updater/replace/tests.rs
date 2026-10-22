// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT

//! Deterministic failure injection for each step of the replacement.
//!
//! These tests use real directories so that the state left on disk is checked,
//! not only the returned value. They cover the atomic AppImage rename and the
//! macOS exchange, including its refusal on a volume that cannot exchange.
//!
//! The exchange itself is `renamex_np(RENAME_SWAP)`, which exists only on
//! macOS. Here it is emulated by three renames inside one injected step, so
//! these tests check the sequence around it and what each outcome leaves on
//! disk, not the system call. On Linux, the real `StdFs::swap` reports
//! `Unsupported`, which exercises the refusal.

use super::*;
use std::{cell::RefCell, collections::BTreeMap, fs, path::PathBuf};

/// Real filesystem operations that fail on chosen calls, counted from zero.
struct Faulty {
    fail_on: Vec<usize>,
    /// Removals that delete one file and then fail, as a recursive delete
    /// that stops partway does.
    partial_on: Vec<usize>,
    log: RefCell<Vec<String>>,
    swap_supported: bool,
}

impl Faulty {
    fn new(fail_on: &[usize]) -> Self {
        Self {
            fail_on: fail_on.to_vec(),
            partial_on: Vec::new(),
            log: RefCell::new(Vec::new()),
            swap_supported: true,
        }
    }

    /// A filesystem whose removals at `partial_on` stop partway.
    fn partial(partial_on: &[usize]) -> Self {
        Self {
            partial_on: partial_on.to_vec(),
            ..Self::new(&[])
        }
    }

    /// A filesystem whose volume cannot exchange two names.
    fn without_swap() -> Self {
        Self {
            swap_supported: false,
            ..Self::new(&[])
        }
    }

    fn step(&self, entry: String) -> io::Result<()> {
        let mut log = self.log.borrow_mut();
        let index = log.len();
        log.push(entry);
        if self.fail_on.contains(&index) {
            Err(io::Error::other(format!(
                "injected failure at step {index}"
            )))
        } else {
            Ok(())
        }
    }

    fn ops(&self) -> Vec<String> {
        self.log.borrow().clone()
    }
}

impl ReplaceFs for Faulty {
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        self.step(format!("rename {} -> {}", label(from), label(to)))?;
        StdFs.rename(from, to)
    }

    fn remove_all(&self, path: &Path) -> io::Result<()> {
        let index = self.log.borrow().len();
        self.step(format!("remove_all {}", label(path)))?;
        if self.partial_on.contains(&index) {
            let first = snapshot(path).into_keys().next().expect("a file to delete");
            fs::remove_file(path.join(first))?;
            return Err(io::Error::other(format!(
                "injected partial removal at step {index}"
            )));
        }
        StdFs.remove_all(path)
    }

    fn swap(&self, a: &Path, b: &Path) -> io::Result<()> {
        self.step(format!("swap {} <-> {}", label(a), label(b)))?;
        if !self.swap_supported {
            return Err(io::Error::new(io::ErrorKind::Unsupported, "no exchange"));
        }
        // Stands in for one renamex_np call; Watched checks only between steps.
        let spare = a.with_extension("swap-spare");
        StdFs.rename(a, &spare)?;
        StdFs.rename(b, a)?;
        StdFs.rename(&spare, b)
    }
}

/// Fails the test if `dest` is missing after any filesystem step.
struct Watched<'a> {
    fs: &'a Faulty,
    dest: &'a Path,
}

impl Watched<'_> {
    fn check<T>(&self, result: io::Result<T>, step: &str) -> io::Result<T> {
        assert!(
            exists(self.dest),
            "{} is missing after {step}",
            self.dest.display()
        );
        result
    }
}

impl ReplaceFs for Watched<'_> {
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        self.check(self.fs.rename(from, to), "a rename")
    }

    fn remove_all(&self, path: &Path) -> io::Result<()> {
        self.check(self.fs.remove_all(path), "a removal")
    }

    fn swap(&self, a: &Path, b: &Path) -> io::Result<()> {
        self.check(self.fs.swap(a, b), "an exchange")
    }
}

fn label(path: &Path) -> String {
    let parts: Vec<_> = path
        .components()
        .rev()
        .take(2)
        .map(|part| part.as_os_str().to_string_lossy().into_owned())
        .collect();
    parts.into_iter().rev().collect::<Vec<_>>().join("/")
}

/// An installed copy and a staged new copy beside it.
struct Fixture {
    _root: tempfile::TempDir,
    dest: PathBuf,
    staged: PathBuf,
}

impl Fixture {
    fn new(bundle: bool) -> Self {
        let root = tempfile::tempdir().unwrap();
        let parent = root.path().join("Applications");
        fs::create_dir_all(&parent).unwrap();
        let name = if bundle { "App.app" } else { "App.AppImage" };
        let dest = parent.join(name);
        let staging = parent.join(format!(".{name}.update-test"));
        fs::create_dir(&staging).unwrap();
        let staged = staging.join(name);
        write_copy(&dest, bundle, "old");
        write_copy(&staged, bundle, "new");
        Self {
            _root: root,
            dest,
            staged,
        }
    }

    /// The staging folder that holds the staged copy.
    fn staging(&self) -> &Path {
        self.staged.parent().unwrap()
    }
}

fn write_copy(path: &Path, bundle: bool, version: &str) {
    if bundle {
        let contents = path.join("Contents/MacOS");
        fs::create_dir_all(&contents).unwrap();
        fs::write(contents.join("app"), version).unwrap();
        fs::write(path.join("Contents/Info.plist"), version).unwrap();
    } else {
        fs::write(path, version).unwrap();
    }
}

fn version_at(path: &Path, bundle: bool) -> Option<String> {
    let file = if bundle {
        path.join("Contents/MacOS/app")
    } else {
        path.to_path_buf()
    };
    fs::read_to_string(file).ok()
}

fn exists(path: &Path) -> bool {
    path.symlink_metadata().is_ok()
}

/// Every file under `root` with its bytes, to compare a tree before and after.
fn snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    let mut files = BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(path) = pending.pop() {
        if path.is_dir() {
            pending.extend(
                fs::read_dir(&path)
                    .unwrap()
                    .map(|entry| entry.unwrap().path()),
            );
        } else {
            let relative = path.strip_prefix(root).unwrap().to_path_buf();
            files.insert(relative, fs::read(&path).unwrap());
        }
    }
    files
}

const INSTALL: &str = "rename .App.update-test/App -> Applications/App";
const EXCHANGE: &str = "swap .App.update-test/App <-> Applications/App";

/// Replaces the extension-specific names in the operation log so that one
/// expected sequence covers both the bundle and the AppImage.
fn normalise(ops: Vec<String>, fixture: &Fixture) -> Vec<String> {
    let name = fixture
        .dest
        .file_name()
        .unwrap()
        .to_string_lossy()
        .into_owned();
    ops.into_iter()
        .map(|op| {
            op.replace(&format!(".{name}.update-test"), ".App.update-test")
                .replace(&name, "App")
        })
        .collect()
}

#[test]
fn an_appimage_is_replaced_by_one_rename_and_its_path_is_never_empty() {
    let fixture = Fixture::new(false);
    let faulty = Faulty::new(&[]);
    let fs = Watched {
        fs: &faulty,
        dest: &fixture.dest,
    };
    let outcome = replace_file(&fs, &CommitGate::new(), &fixture.staged, &fixture.dest).unwrap();
    assert!(matches!(outcome, Replaced::Clean));
    assert_eq!(version_at(&fixture.dest, false).as_deref(), Some("new"));
    assert!(!exists(&fixture.staged));
    // One rename onto the installed name: there is no step that moves it away.
    assert_eq!(normalise(faulty.ops(), &fixture), [INSTALL]);
}

#[test]
fn a_failed_appimage_rename_leaves_both_files() {
    let fixture = Fixture::new(false);
    let fs = Faulty::new(&[0]);
    let failure =
        replace_file(&fs, &CommitGate::new(), &fixture.staged, &fixture.dest).unwrap_err();
    assert!(
        matches!(failure, ReplaceFailure::Unchanged(_)),
        "{failure:?}"
    );
    assert_eq!(version_at(&fixture.dest, false).as_deref(), Some("old"));
    assert_eq!(version_at(&fixture.staged, false).as_deref(), Some("new"));
}

#[test]
fn a_bundle_is_exchanged_in_one_step_and_its_path_is_never_empty() {
    let fixture = Fixture::new(true);
    let faulty = Faulty::new(&[]);
    let fs = Watched {
        fs: &faulty,
        dest: &fixture.dest,
    };
    let outcome = replace_bundle(&fs, &CommitGate::new(), &fixture.staged, &fixture.dest).unwrap();
    assert!(matches!(outcome, Replaced::Clean));
    assert_eq!(version_at(&fixture.dest, true).as_deref(), Some("new"));
    // The previous copy came out at the staged path and was deleted from there.
    assert!(!exists(&fixture.staged));
    assert_eq!(
        normalise(faulty.ops(), &fixture),
        [EXCHANGE, "remove_all .App.update-test/App"]
    );
}

#[test]
fn a_volume_that_cannot_exchange_is_refused_and_the_installed_bundle_is_untouched() {
    let fixture = Fixture::new(true);
    let before = snapshot(&fixture.dest);
    let fs = Faulty::without_swap();
    let failure =
        replace_bundle(&fs, &CommitGate::new(), &fixture.staged, &fixture.dest).unwrap_err();
    assert!(
        matches!(failure, ReplaceFailure::SwapUnsupported),
        "{failure:?}"
    );
    assert_eq!(snapshot(&fixture.dest), before, "byte for byte");
    // Only the refused exchange was attempted: nothing was moved aside.
    assert_eq!(normalise(fs.ops(), &fixture), [EXCHANGE]);
    let error = crate::Error::from(failure);
    assert!(matches!(
        error,
        crate::Error::ReplacementUnsupported { staging_left: None }
    ));
    assert!(error.to_string().contains("not changed"), "{error}");
}

const CLOSE: &str = "remove_all Applications/.App.update-test";

/// Exchanges the bundles as the macOS installer does, then closes the staging folder.
fn exchange_and_close(
    fs: &Faulty,
    fixture: &Fixture,
    report: &CleanupReport,
) -> crate::Result<Replaced> {
    let attempt = replace_bundle(fs, &CommitGate::new(), &fixture.staged, &fixture.dest)
        .map_err(crate::Error::from);
    close_staging(fs, fixture.staging(), attempt, report)
}

/// Renames the AppImage as the Linux installer does, then closes the staging folder.
fn rename_and_close(
    fs: &Faulty,
    fixture: &Fixture,
    report: &CleanupReport,
) -> crate::Result<Replaced> {
    let attempt = replace_file(fs, &CommitGate::new(), &fixture.staged, &fixture.dest)
        .map_err(crate::Error::from);
    close_staging(fs, fixture.staging(), attempt, report)
}

#[test]
fn a_refused_bundle_deletes_its_staging_folder() {
    let fixture = Fixture::new(true);
    let before = snapshot(&fixture.dest);
    let fs = Faulty::without_swap();
    let report = CleanupReport::new();
    let error = exchange_and_close(&fs, &fixture, &report).unwrap_err();
    assert!(
        matches!(
            error,
            crate::Error::ReplacementUnsupported { staging_left: None }
        ),
        "{error:?}"
    );
    assert_eq!(snapshot(&fixture.dest), before, "byte for byte");
    assert!(!exists(fixture.staging()));
    assert_eq!(normalise(fs.ops(), &fixture), [EXCHANGE, CLOSE]);
    assert!(report.take().is_empty());
}

#[test]
fn a_refused_bundle_whose_staging_folder_cannot_be_deleted_names_that_folder() {
    let fixture = Fixture::new(true);
    let staging = fixture.staging().to_path_buf();
    let before = snapshot(&fixture.dest);
    let fs = Faulty {
        swap_supported: false,
        ..Faulty::new(&[1])
    };
    let report = CleanupReport::new();
    let error = exchange_and_close(&fs, &fixture, &report).unwrap_err();
    let crate::Error::ReplacementUnsupported {
        staging_left: Some(left),
    } = &error
    else {
        panic!("{error:?}");
    };
    assert_eq!(left, &staging);
    assert!(
        error.to_string().contains(&staging.display().to_string()),
        "{error}"
    );
    assert_eq!(snapshot(&fixture.dest), before, "byte for byte");
    // The new copy is still in the folder the refusal names.
    assert_eq!(version_at(&fixture.staged, true).as_deref(), Some("new"));
    assert_eq!(normalise(fs.ops(), &fixture), [EXCHANGE, CLOSE]);
    // The refusal carries the folder, so it is not reported a second time.
    assert!(report.take().is_empty());
}

#[test]
fn a_refused_bundle_whose_staging_folder_is_partly_deleted_still_names_it() {
    let fixture = Fixture::new(true);
    let staging = fixture.staging().to_path_buf();
    let before = snapshot(&fixture.dest);
    let staged_files = snapshot(&staging).len();
    let fs = Faulty {
        swap_supported: false,
        ..Faulty::partial(&[1])
    };
    let error = exchange_and_close(&fs, &fixture, &CleanupReport::new()).unwrap_err();
    assert!(
        matches!(
            &error,
            crate::Error::ReplacementUnsupported { staging_left: Some(left) } if left == &staging
        ),
        "{error:?}"
    );
    assert_eq!(snapshot(&fixture.dest), before, "byte for byte");
    assert_eq!(
        snapshot(&staging).len(),
        staged_files - 1,
        "one file was deleted"
    );
}

#[test]
fn a_staging_folder_that_cannot_be_deleted_keeps_the_result_and_is_reported() {
    let fixture = Fixture::new(true);
    let staging = fixture.staging();
    let report = CleanupReport::new();
    let failed = close_staging(
        &Faulty::new(&[0]),
        staging,
        Err(crate::Error::Io(io::Error::other("extract"))),
        &report,
    );
    assert!(matches!(failed, Err(crate::Error::Io(_))), "{failed:?}");
    assert_eq!(report.take(), [staging]);
    let installed = close_staging(&Faulty::new(&[0]), staging, Ok(Replaced::Clean), &report);
    assert!(matches!(installed, Ok(Replaced::Clean)), "{installed:?}");
    assert_eq!(report.take(), [staging]);
}

#[test]
fn closing_the_staging_folder_deletes_a_previous_bundle_left_in_it() {
    let fixture = Fixture::new(true);
    // The exchange succeeds, but deleting the previous copy from the staged path fails once.
    let fs = Faulty::new(&[1]);
    let report = CleanupReport::new();
    let outcome = exchange_and_close(&fs, &fixture, &report).unwrap();
    assert!(matches!(outcome, Replaced::Clean), "{outcome:?}");
    assert_eq!(version_at(&fixture.dest, true).as_deref(), Some("new"));
    assert!(!exists(fixture.staging()));
    assert!(report.take().is_empty());
}

#[test]
fn a_previous_bundle_that_survives_both_deletions_is_reported_by_its_folder() {
    let fixture = Fixture::new(true);
    // Deleting the previous copy fails, and so does deleting the folder around it.
    let fs = Faulty::new(&[1, 2]);
    let report = CleanupReport::new();
    let outcome = exchange_and_close(&fs, &fixture, &report).unwrap();
    assert!(matches!(outcome, Replaced::BackupLeft), "{outcome:?}");
    assert_eq!(version_at(&fixture.dest, true).as_deref(), Some("new"));
    assert_eq!(version_at(&fixture.staged, true).as_deref(), Some("old"));
    assert_eq!(report.take(), [fixture.staging()]);
}

#[test]
fn a_partly_deleted_staging_folder_after_an_installed_bundle_is_reported() {
    let fixture = Fixture::new(true);
    let fs = Faulty::partial(&[1, 2]);
    let report = CleanupReport::new();
    let outcome = exchange_and_close(&fs, &fixture, &report).unwrap();
    assert!(matches!(outcome, Replaced::BackupLeft), "{outcome:?}");
    assert_eq!(version_at(&fixture.dest, true).as_deref(), Some("new"));
    assert!(exists(fixture.staging()));
    assert_eq!(report.take(), [fixture.staging()]);
}

#[test]
fn an_installed_appimage_deletes_its_staging_folder() {
    let fixture = Fixture::new(false);
    let fs = Faulty::new(&[]);
    let report = CleanupReport::new();
    let outcome = rename_and_close(&fs, &fixture, &report).unwrap();
    assert!(matches!(outcome, Replaced::Clean), "{outcome:?}");
    assert_eq!(version_at(&fixture.dest, false).as_deref(), Some("new"));
    assert!(!exists(fixture.staging()));
    assert_eq!(normalise(fs.ops(), &fixture), [INSTALL, CLOSE]);
    assert!(report.take().is_empty());
}

#[test]
fn an_installed_appimage_reports_a_staging_folder_it_cannot_delete() {
    let fixture = Fixture::new(false);
    let fs = Faulty::new(&[1]);
    let report = CleanupReport::new();
    let outcome = rename_and_close(&fs, &fixture, &report).unwrap();
    assert!(matches!(outcome, Replaced::Clean), "{outcome:?}");
    assert_eq!(version_at(&fixture.dest, false).as_deref(), Some("new"));
    assert_eq!(report.take(), [fixture.staging()]);
}

#[test]
fn a_failed_appimage_rename_keeps_its_error_and_reports_the_staging_folder() {
    let fixture = Fixture::new(false);
    let fs = Faulty::new(&[0, 1]);
    let report = CleanupReport::new();
    let error = rename_and_close(&fs, &fixture, &report).unwrap_err();
    assert!(matches!(error, crate::Error::Io(_)), "{error:?}");
    assert_eq!(version_at(&fixture.dest, false).as_deref(), Some("old"));
    assert_eq!(version_at(&fixture.staged, false).as_deref(), Some("new"));
    assert_eq!(report.take(), [fixture.staging()]);
}

#[cfg(not(target_os = "macos"))]
#[test]
fn the_real_exchange_is_unsupported_off_macos_so_the_bundle_is_refused() {
    let fixture = Fixture::new(true);
    let before = snapshot(&fixture.dest);
    let gate = CommitGate::new();
    let failure = replace_bundle(&StdFs, &gate, &fixture.staged, &fixture.dest).unwrap_err();
    assert!(matches!(failure, ReplaceFailure::SwapUnsupported));
    assert_eq!(snapshot(&fixture.dest), before);
    assert_eq!(version_at(&fixture.staged, true).as_deref(), Some("new"));
    // The gate is open again: a refusal leaves no replacement running.
    assert!(gate.abandon());
}

#[test]
fn a_failed_exchange_leaves_both_bundles() {
    let fixture = Fixture::new(true);
    let fs = Faulty::new(&[0]);
    let failure =
        replace_bundle(&fs, &CommitGate::new(), &fixture.staged, &fixture.dest).unwrap_err();
    assert!(
        matches!(failure, ReplaceFailure::Unchanged(_)),
        "{failure:?}"
    );
    assert_eq!(version_at(&fixture.dest, true).as_deref(), Some("old"));
    assert_eq!(version_at(&fixture.staged, true).as_deref(), Some("new"));
}

#[test]
fn a_previous_bundle_that_cannot_be_deleted_is_left_at_the_staged_path() {
    let fixture = Fixture::new(true);
    let fs = Faulty::new(&[1]);
    let outcome = replace_bundle(&fs, &CommitGate::new(), &fixture.staged, &fixture.dest).unwrap();
    assert!(matches!(outcome, Replaced::BackupLeft), "{outcome:?}");
    assert_eq!(version_at(&fixture.dest, true).as_deref(), Some("new"));
    assert_eq!(version_at(&fixture.staged, true).as_deref(), Some("old"));
}

#[test]
fn an_abandoned_gate_stops_every_replacement_before_it_touches_a_file() {
    let gate = CommitGate::new();
    assert!(gate.abandon());
    for bundle in [true, false] {
        let fixture = Fixture::new(bundle);
        let fs = Faulty::new(&[]);
        let failures = [
            replace_file(&fs, &gate, &fixture.staged, &fixture.dest).unwrap_err(),
            replace_bundle(&fs, &gate, &fixture.staged, &fixture.dest).unwrap_err(),
        ];
        for failure in failures {
            let ReplaceFailure::Unchanged(error) = failure else {
                panic!("{failure:?}");
            };
            assert_eq!(error.kind(), io::ErrorKind::Interrupted);
        }
        assert_eq!(version_at(&fixture.dest, bundle).as_deref(), Some("old"));
        assert!(fs.ops().is_empty());
    }
}

#[test]
fn a_gate_cannot_be_abandoned_while_a_replacement_is_running() {
    let gate = CommitGate::new();
    let committing = gate.enter().unwrap();
    assert!(!gate.abandon());
    assert!(gate.enter().is_err(), "one replacement at a time");
    drop(committing);
    assert!(gate.abandon());
    assert!(gate.abandon(), "abandoning twice is still abandoned");
    assert!(gate.enter().is_err());
}

#[test]
fn a_destination_without_a_parent_is_refused() {
    for dest in ["/", "App.app", ""] {
        assert!(parent_and_name(Path::new(dest)).is_err(), "{dest}");
    }
}
