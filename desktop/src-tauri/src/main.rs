#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::{json, Value};
use std::{
    io::{self, BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::Mutex,
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager, State};
use tauri_plugin_dialog::DialogExt;

#[cfg(windows)]
mod job;
mod process;
mod updates;

struct Worker {
    child: Child,
    input: ChildStdin,
    process_tree: process::ProcessTree,
}

impl Worker {
    fn shutdown(mut self, timeout: Duration) -> bool {
        let requested = self
            .input
            .write_all(b"{\"schema_version\":1,\"id\":\"shutdown\",\"command\":\"shutdown\"}\n")
            .and_then(|_| self.input.flush())
            .is_ok();
        if !requested {
            return false;
        }
        let deadline = Instant::now() + timeout;
        loop {
            match self.child.try_wait() {
                Ok(Some(_)) => return true,
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(10));
                }
                _ => return false,
            }
        }
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        self.process_tree.terminate(&mut self.child);
    }
}

#[derive(Default)]
struct Desktop {
    worker: Option<Worker>,
    source: Option<PathBuf>,
    output: Option<PathBuf>,
    // One slot per report document, in report order; see result_paths.
    results: Vec<Option<PathBuf>>,
    busy: bool,
    updating: bool,
    closing: bool,
}

// Resolve through the OS known-folder API (including redirected Downloads),
// not a translated display name or an assumed home/Downloads path.
fn initial_output(
    current: &mut Option<PathBuf>,
    resolved: Result<PathBuf, String>,
) -> Result<PathBuf, String> {
    if let Some(path) = current {
        return Ok(path.clone());
    }
    let unavailable = |reason: String| {
        format!(
        "Your Downloads folder is unavailable: {reason}. Choose an output folder before exporting."
    )
    };
    let path = resolved.map_err(unavailable)?;
    if !path.is_absolute() {
        return Err(unavailable(
            "the operating system did not return an absolute path".into(),
        ));
    }
    std::fs::create_dir_all(&path).map_err(|error| unavailable(error.to_string()))?;
    if !path.is_dir() {
        return Err(unavailable("the resolved path is not a folder".into()));
    }
    *current = Some(path.clone());
    Ok(path)
}

type Shared = Mutex<Desktop>;
const EXIT_WORKER_GRACE: Duration = Duration::from_secs(1);

fn allow_worker_command(state: &Desktop, command: &str) -> Result<(), String> {
    if state.busy && command != "cancel" {
        return Err("An export is already running".into());
    }
    if state.updating && command == "export" {
        return Err("Wait for the update operation to finish or cancel it".into());
    }
    Ok(())
}

#[tauri::command]
fn connect_worker(app: tauri::AppHandle, state: State<'_, Shared>) -> Result<(), String> {
    let mut state = state.lock().map_err(|e| e.to_string())?;
    if state.worker.is_some() {
        return Ok(());
    }
    let default_output = initial_output(
        &mut state.output,
        app.path().download_dir().map_err(|error| error.to_string()),
    );
    let executable = app
        .path()
        .resource_dir()
        .map_err(|e| e.to_string())?
        .join("runtime")
        .join(if cfg!(windows) {
            "platefold.exe"
        } else {
            "platefold"
        });
    let mut command = Command::new(executable);
    command
        .arg("--desktop-worker")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    process::configure(&mut command);
    let mut child = command
        .spawn()
        .map_err(|e| format!("Could not start the export worker: {e}"))?;
    let process_tree = match process::ProcessTree::attach(&child) {
        Ok(tree) => tree,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("Could not track the export worker's processes for cleanup: {error}"));
        }
    };
    let input = child.stdin.take().ok_or("Worker stdin unavailable")?;
    let output = child.stdout.take().ok_or("Worker stdout unavailable")?;
    let pid = child.id();
    state.worker = Some(Worker {
        child,
        input,
        process_tree,
    });
    std::thread::spawn(move || {
        let mut reader = BufReader::new(output);
        loop {
            let mut line = String::new();
            let read = std::io::Read::take(&mut reader, 16 * 1024 * 1024).read_line(&mut line);
            if !matches!(read, Ok(n) if n > 0) || !line.ends_with('\n') {
                break;
            }
            let Ok(mut event) = serde_json::from_str::<Value>(&line) else {
                break;
            };
            if event["event"] == "ready" {
                match &default_output {
                    Ok(path) => event["output_path"] = json!(path),
                    Err(message) => event["output_error"] = json!(message),
                }
            }
            if matches!(event["event"].as_str(), Some("result" | "error")) {
                if let Ok(mut state) = app.state::<Shared>().lock() {
                    if event["event"] == "result" || event["id"] == "export" {
                        state.busy = false;
                    }
                    if event["event"] == "result" {
                        state.results.clear();
                        if let (Some(root), Some(documents)) = (
                            event["result"]["output_dir"].as_str(),
                            event["result"]["report"]["documents"].as_array(),
                        ) {
                            state.results = result_paths(Path::new(root), documents);
                        }
                    }
                }
            }
            let _ = app.emit("worker-event", event);
        }
        if let Ok(mut state) = app.state::<Shared>().lock() {
            if state
                .worker
                .as_ref()
                .is_some_and(|worker| worker.child.id() == pid)
            {
                state.worker.take();
                state.busy = false;
            }
        }
        let _ = app.emit(
            "worker-exit",
            "The export worker stopped. Reopen the app to reconnect.",
        );
    });
    Ok(())
}

/// The PDF for each report document, in report order.
///
/// The interface opens a result by its row in the report, so every document
/// keeps its slot here. A PDF that is missing or resolves outside the output
/// folder is `None`, and opening it fails without shifting the documents after it.
fn result_paths(root: &Path, documents: &[Value]) -> Vec<Option<PathBuf>> {
    let canonical_root = root.canonicalize().ok();
    documents
        .iter()
        .map(|document| {
            let canonical_root = canonical_root.as_ref()?;
            let file = document["file"].as_str()?;
            let path = root.join(file).with_extension("pdf").canonicalize().ok()?;
            path.starts_with(canonical_root).then_some(path)
        })
        .collect()
}

#[tauri::command]
fn send_worker(mut message: Value, state: State<'_, Shared>) -> Result<(), String> {
    let mut state = state.lock().map_err(|e| e.to_string())?;
    let command = message["command"].as_str().unwrap_or("").to_owned();
    if !matches!(
        command.as_str(),
        "catalog" | "export" | "cancel" | "save_copy" | "overwrite" | "delete" | "preview"
    ) {
        return Err("Unsupported desktop operation".into());
    }
    if message["schema_version"] != 1 || !message["id"].is_string() {
        return Err("Invalid desktop request".into());
    }
    allow_worker_command(&state, &command)?;
    message["input_path"] = json!(state.source);
    if command == "export" {
        if state.source.is_none() || state.output.is_none() {
            return Err("Choose source documents and an output folder first".into());
        }
        message["id"] = json!("export");
        message["output_dir"] = json!(state.output);
    }
    let line = serde_json::to_string(&message).map_err(|e| e.to_string())? + "\n";
    if line.len() > 65536 {
        return Err("Desktop request is too large".into());
    }
    state
        .worker
        .as_mut()
        .ok_or("Export worker is not connected")?
        .input
        .write_all(line.as_bytes())
        .map_err(|e| e.to_string())?;
    if command == "export" {
        state.busy = true;
        state.results.clear();
    }
    Ok(())
}

#[tauri::command]
async fn choose_path(kind: String, app: tauri::AppHandle) -> Result<Option<String>, String> {
    if app
        .state::<Shared>()
        .lock()
        .map_err(|e| e.to_string())?
        .busy
    {
        return Err("Wait for the export to finish".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        let window = app
            .get_webview_window("main")
            .ok_or("Main window unavailable")?;
        let picker = app.dialog().file().set_parent(&window);
        let selected = match kind.as_str() {
            "file" => picker
                .add_filter(
                    "Documents",
                    &["doc", "docx", "odt", "rtf", "xlsx", "md", "csv", "tsv"],
                )
                .blocking_pick_file(),
            "folder" | "output" => picker.blocking_pick_folder(),
            _ => return Err("Unknown file selection".to_owned()),
        };
        let Some(file) = selected else {
            return Ok(None);
        };
        let path = file.into_path().map_err(|e| e.to_string())?;
        let path = dunce::canonicalize(path).map_err(|e| e.to_string())?;
        let shared = app.state::<Shared>();
        let mut state = shared.lock().map_err(|e| e.to_string())?;
        if state.busy {
            return Err("An export started while the picker was open".into());
        }
        if kind == "output" {
            state.output = Some(path.clone());
        } else {
            state.source = Some(path.clone());
        }
        Ok(Some(path.to_string_lossy().into_owned()))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn set_path(kind: String, path: String, state: State<'_, Shared>) -> Result<String, String> {
    let path = dunce::canonicalize(path.trim()).map_err(|e| format!("Path unavailable: {e}"))?;
    let mut state = state.lock().map_err(|e| e.to_string())?;
    if state.busy {
        return Err("Wait for the export to finish".into());
    }
    match kind.as_str() {
        "source"
            if path.is_dir()
                || (path.is_file()
                    && path.extension().is_some_and(|e| {
                        ["doc", "docx", "odt", "rtf", "xlsx", "md", "csv", "tsv"]
                            .iter()
                            .any(|suffix| e.eq_ignore_ascii_case(suffix))
                    })) =>
        {
            state.source = Some(path.clone())
        }
        "output" if path.is_dir() => state.output = Some(path.clone()),
        _ => {
            return Err(
                "Choose a DOC, DOCX, ODT, RTF, XLSX, Markdown, CSV, or TSV source file/folder or an existing output folder"
                    .into(),
            )
        }
    }
    Ok(path.to_string_lossy().into_owned())
}

#[tauri::command]
async fn open_result(index: usize, state: State<'_, Shared>) -> Result<(), String> {
    let path = {
        let state = state.lock().map_err(|e| e.to_string())?;
        state
            .results
            .get(index)
            .cloned()
            .flatten()
            .ok_or("No completed PDF at that index")?
    };
    // Hand off outside the UI event loop. Linux uses one GIO dispatch instead
    // of the open crate's xdg-open path, which coincided with one action opening
    // both Evince and Firefox despite one MIME default during qualification.
    tauri::async_runtime::spawn_blocking(move || launch_result(path))
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e.to_string())
}

#[cfg(target_os = "linux")]
fn result_opener(path: &std::path::Path) -> Command {
    // AppImage's launcher prepends its private libraries and GIO modules to
    // the desktop process environment. A system GIO launched with those
    // variables can find the host's PDF handler and still fail to start it.
    // Start the one external handoff from a small desktop-session allowlist.
    let mut command = Command::new("/usr/bin/gio");
    command.env_clear().env(
        "PATH",
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    );
    for name in [
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command.arg("open").arg(path);
    command
}

fn launch_result(path: PathBuf) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    {
        let mut command = result_opener(&path);
        let status = command.status()?;
        if status.success() {
            Ok(())
        } else {
            Err(io::Error::other(format!(
                "gio could not open the PDF (status {status})"
            )))
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        open::that_detached(path)
    }
}

fn fitted_window_size(width: u32, height: u32, scale: f64) -> (f64, f64) {
    let scale = if scale.is_finite() && scale > 0.0 {
        scale
    } else {
        1.0
    };
    let available_width = (f64::from(width) / scale - 32.0).max(640.0);
    let available_height = (f64::from(height) / scale - 64.0).max(420.0);
    (
        1280.0_f64.min(available_width),
        700.0_f64.min(available_height),
    )
}

fn fit_window_to_monitor(window: &tauri::WebviewWindow) -> tauri::Result<()> {
    let Some(monitor) = window.current_monitor()? else {
        return Ok(());
    };
    let size = monitor.size();
    let (width, height) = fitted_window_size(size.width, size.height, monitor.scale_factor());
    window.set_size(tauri::LogicalSize::new(width, height))
}

fn main() {
    tauri::Builder::default()
        .manage(Shared::default())
        .setup(|app| {
            app.manage(updates::UpdateState::new(app.path().app_config_dir()?));
            if let Some(window) = app.get_webview_window("main") {
                fit_window_to_monitor(&window)?;
            }
            Ok(())
        })
        .plugin(tauri_plugin_dialog::init())
        // No updater commands are granted to JavaScript. PlateFold's commands
        // bind signed discovery to the Tauri adapter before it can install.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            connect_worker,
            send_worker,
            choose_path,
            set_path,
            open_result,
            updates::update_status,
            updates::update_preferences,
            updates::check_updates,
            updates::download_update,
            updates::cancel_update,
            updates::install_update,
            updates::restart_update
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                // Defer this close until the installation result or the close watcher
                // releases it; an active replacement or installer launch must finish.
                if updates::defer_exit_while_installing(window.app_handle()) {
                    return;
                }
                updates::cancel_for_exit(window.state::<updates::UpdateState>().inner());
                let worker = if let Ok(mut state) = window.state::<Shared>().lock() {
                    if state.closing {
                        return;
                    }
                    state.closing = true;
                    state.busy = false;
                    state.worker.take()
                } else {
                    None
                };
                let window = window.clone();
                std::thread::spawn(move || {
                    if let Some(worker) = worker {
                        worker.shutdown(EXIT_WORKER_GRACE);
                    }
                    let _ = window.destroy();
                });
            }
        })
        .build(tauri::generate_context!())
        .expect("Could not start PlateFold")
        .run(|app, event| {
            // Any other exit, such as Quit on macOS, waits for an installation too.
            if let tauri::RunEvent::ExitRequested { api, .. } = event {
                if updates::defer_exit_while_installing(app) {
                    api.prevent_exit();
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_missing_pdf_keeps_its_slot_so_later_results_open_their_own_file() {
        let base = tempfile::tempdir().unwrap();
        let root = base.path().join("PDF Export");
        std::fs::create_dir(&root).unwrap();
        std::fs::write(root.join("b.pdf"), b"%PDF").unwrap();
        std::fs::write(base.path().join("outside.pdf"), b"%PDF").unwrap();
        let documents = [json!({"file": "a.md"}), json!({"file": "b.md"})];
        let paths = result_paths(&root, &documents);
        assert_eq!(paths.len(), 2);
        assert_eq!(paths[0], None);
        assert_eq!(paths[1], Some(root.join("b.pdf").canonicalize().unwrap()));
        // A document without a file, or one outside the folder, also keeps its slot.
        let escaping = [
            json!({}),
            json!({"file": "../outside.md"}),
            json!({"file": "b.md"}),
        ];
        let paths = result_paths(&root, &escaping);
        assert_eq!(paths.len(), 3);
        assert!(paths[0].is_none() && paths[1].is_none() && paths[2].is_some());
    }

    #[test]
    fn downloads_default_preserves_explicit_destination() {
        let selected = PathBuf::from("selected-output");
        let mut output = Some(selected.clone());
        assert_eq!(
            initial_output(&mut output, Err("Downloads offline".into())).unwrap(),
            selected
        );
        assert_eq!(output, Some(selected));
    }

    #[test]
    fn downloads_resolution_failure_requires_selection() {
        let mut output = None;
        let error =
            initial_output(&mut output, Err("Known folder not configured".into())).unwrap_err();
        assert!(error.contains("Choose an output folder"));
        assert!(output.is_none());
        assert!(initial_output(&mut output, Ok(PathBuf::from("relative-downloads"))).is_err());
    }

    #[test]
    fn downloads_default_can_create_the_resolved_folder() {
        let path = std::env::temp_dir().join(format!(
            "platefold-downloads-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let mut output = None;
        assert_eq!(initial_output(&mut output, Ok(path.clone())).unwrap(), path);
        assert!(path.is_dir());
        assert_eq!(output, Some(path.clone()));
        std::fs::remove_dir(path).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_pdf_handoff_uses_one_gio_dispatch() {
        use std::ffi::OsStr;

        let command = result_opener(std::path::Path::new("/tmp/example.pdf"));
        assert_eq!(command.get_program(), "/usr/bin/gio");
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            [OsStr::new("open"), OsStr::new("/tmp/example.pdf")]
        );
        let environment = command
            .get_envs()
            .map(|(name, value)| (name.to_owned(), value.map(OsStr::to_owned)))
            .collect::<std::collections::HashMap<_, _>>();
        assert_eq!(
            environment.get(OsStr::new("PATH")),
            Some(&Some(
                OsStr::new("/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
                    .to_owned()
            ))
        );
        for name in [
            "APPDIR",
            "APPIMAGE",
            "GIO_EXTRA_MODULES",
            "GTK_PATH",
            "LD_LIBRARY_PATH",
            "PYTHONHOME",
            "PYTHONPATH",
            "QT_PLUGIN_PATH",
            "XDG_CONFIG_DIRS",
            "XDG_DATA_DIRS",
        ] {
            assert!(!environment.contains_key(OsStr::new(name)));
        }
    }

    #[test]
    fn high_density_monitor_gets_a_visible_responsive_window() {
        let (width, height) = fitted_window_size(1280, 800, 1.5);
        assert!((800.0..=840.0).contains(&width));
        assert!((450.0..=480.0).contains(&height));
    }

    #[cfg(unix)]
    #[test]
    fn worker_shutdown_requests_cooperative_exit_before_termination() {
        let mut command = Command::new("sh");
        command
            .arg("-c")
            .arg("read -r request; test -n \"$request\"")
            .stdin(Stdio::piped())
            .stdout(Stdio::null());
        process::configure(&mut command);
        let mut child = command.spawn().expect("spawn worker fixture");
        let process_tree = process::ProcessTree::attach(&child).expect("attach worker group");
        let input = child.stdin.take().expect("worker stdin");
        let worker = Worker {
            child,
            input,
            process_tree,
        };

        assert!(worker.shutdown(Duration::from_secs(1)));
    }

    #[cfg(unix)]
    #[test]
    fn worker_shutdown_force_terminates_the_process_tree_after_timeout() {
        let mut command = Command::new("sh");
        command
            .arg("-c")
            .arg("trap '' TERM; sleep 60 & printf '%s\\n' $!; wait")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped());
        process::configure(&mut command);
        let mut child = command.spawn().expect("spawn worker fixture");
        let process_tree = process::ProcessTree::attach(&child).expect("attach worker group");
        let mut output = BufReader::new(child.stdout.take().expect("worker stdout"));
        let mut line = String::new();
        output.read_line(&mut line).expect("read descendant PID");
        let descendant: libc::pid_t = line.trim().parse().expect("numeric descendant PID");
        let input = child.stdin.take().expect("worker stdin");
        let worker = Worker {
            child,
            input,
            process_tree,
        };

        assert!(!worker.shutdown(Duration::from_millis(50)));
        assert_eq!(unsafe { libc::kill(descendant, 0) }, -1);
        assert_eq!(io::Error::last_os_error().raw_os_error(), Some(libc::ESRCH));
    }

    #[test]
    fn packaged_updater_configuration_initializes() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let updater: tauri_plugin_updater::Config =
            serde_json::from_value(config["plugins"]["updater"].clone()).unwrap();

        assert!(updater.endpoints.is_empty());
        assert!(updater.pubkey.is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn generated_updater_build_key_passes_tauri_cli_parser() {
        use base64::{engine::general_purpose::STANDARD, Engine as _};
        // Exercise the actual Python overlay, then the same Minisign parser
        // used by Tauri CLI pub_key(). No private key or signing is involved.
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let output = Command::new("python3")
            .args([
                "-c",
                "from tools.build_desktop import updater_build_config; p = updater_build_config(); print(p.read_text()); p.unlink()",
            ])
            .current_dir(&root)
            .output()
            .expect("Python 3.11+ is required for the packaging contract test");
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let config: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        let encoded = config["plugins"]["updater"]["pubkey"].as_str().unwrap();
        let document = String::from_utf8(STANDARD.decode(encoded).unwrap()).unwrap();
        let key = minisign::PublicKeyBox::from_string(&document)
            .unwrap()
            .into_public_key()
            .expect("generated public key must pass Tauri CLI's decoder");
        let policy: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(root.join("packaging/update-policy.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            key.to_base64(),
            policy["artifact_public_keys"][0].as_str().unwrap()
        );
        assert!(minisign::PublicKeyBox::from_string("")
            .unwrap()
            .into_public_key()
            .is_err());
    }

    #[test]
    fn native_export_gate_excludes_update_operations() {
        let state = Desktop {
            updating: true,
            ..Desktop::default()
        };
        assert!(allow_worker_command(&state, "export")
            .unwrap_err()
            .contains("update operation"));
        assert!(allow_worker_command(&state, "catalog").is_ok());
    }
}
