#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use tauri::{Emitter, Manager, State};

#[derive(Clone, Serialize, Deserialize)]
struct Settings {
    kit: String,
    python: String,
}

struct Library {
    settings: Mutex<Settings>,
    job: Mutex<Option<PathBuf>>,
    storage: PathBuf,
    settings_file: PathBuf,
}

fn command(settings: &Settings, storage: &PathBuf, request: &Value) -> Command {
    let mut cmd = Command::new(&settings.python);
    cmd.arg("-u")
        .arg(PathBuf::from(&settings.kit).join("tools/library.py"))
        .arg("--storage")
        .arg(storage)
        .arg("--request")
        .arg(request.to_string())
        .current_dir(&settings.kit)
        .env_remove("RECOMP_BUILD_ROOT");
    let mut paths = vec![PathBuf::from(&settings.python)
        .parent()
        .unwrap_or(std::path::Path::new("."))
        .to_path_buf()];
    if let Some(path) = std::env::var_os("PATH") {
        paths.extend(std::env::split_paths(&path));
    }
    if let Some(home) = std::env::var_os("HOME") {
        paths.push(PathBuf::from(home).join(".cargo/bin"));
    }
    #[cfg(target_os = "macos")]
    paths.extend([
        PathBuf::from("/opt/homebrew/bin"),
        PathBuf::from("/usr/local/bin"),
    ]);
    if let Ok(path) = std::env::join_paths(paths) {
        cmd.env("PATH", path);
    }
    cmd
}

#[tauri::command]
fn settings(state: State<Library>) -> Settings {
    state.settings.lock().unwrap().clone()
}

#[tauri::command]
fn save_settings(value: Settings, state: State<Library>) -> Result<(), String> {
    let busy = state.job.lock().unwrap();
    if busy.is_some() {
        return Err("Wait for the current installation to finish".into());
    }
    if !PathBuf::from(&value.kit).join("tools/library.py").is_file() {
        return Err("Choose a recomp-kit checkout containing tools/library.py".into());
    }
    if value.python.is_empty() {
        return Err("Select a Python interpreter".into());
    }
    let temporary = state.settings_file.with_extension("part");
    std::fs::write(
        &temporary,
        serde_json::to_vec(&value).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    std::fs::rename(temporary, &state.settings_file).map_err(|e| e.to_string())?;
    *state.settings.lock().unwrap() = value;
    Ok(())
}

#[tauri::command]
async fn pick(kind: String) -> Option<String> {
    let dialog = rfd::AsyncFileDialog::new();
    let chosen = match kind.as_str() {
        "folder" | "kit" => dialog.pick_folder().await,
        "source" => {
            dialog
                .add_filter("Game executable or ZIP", &["zip", "exe"])
                .pick_file()
                .await
        }
        "reference" => dialog.pick_file().await,
        _ => dialog.pick_file().await,
    };
    chosen.map(|file| file.path().to_string_lossy().into_owned())
}

#[tauri::command]
async fn query(request: Value, app: tauri::AppHandle) -> Result<Value, String> {
    if !matches!(request["action"].as_str(), Some("catalog" | "check" | "inspect" | "discover" | "preflight" | "adopt" | "job_log"
        | "assisted_start" | "assisted_status" | "assisted_path" | "assisted_cancel" | "assisted_recheck")) {
        return Err("Unsupported query".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<Library>();
        let settings = state.settings.lock().unwrap().clone();
        let output = command(&settings, &state.storage, &request)
            .output()
            .map_err(|e| format!("Cannot start Python: {e}"))?;
        for line in String::from_utf8_lossy(&output.stdout).lines() {
            if let Ok(event) = serde_json::from_str::<Value>(line) {
                if event["kind"] == "result" && output.status.success() {
                    return Ok(event["result"].clone());
                }
                if event["kind"] == "error" {
                    return Err(serde_json::to_string(&json!({
                        "message": event["message"].as_str().unwrap_or("Query failed"),
                        "diagnostic": event.get("diagnostic")
                    })).unwrap_or_else(|_| "Query failed".into()));
                }
            }
        }
        Err(format!(
            "Library query failed: {}",
            String::from_utf8_lossy(&output.stderr)
        ))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn open_assisted_folder(game: String, job_id: String, app: tauri::AppHandle) -> Result<(), String> {
    let result = query(json!({"action":"assisted_path", "game":game, "jobId":job_id}), app).await?;
    let folder = result["dropFolder"].as_str().ok_or("The saved job has no safe drop folder")?;
    let path = PathBuf::from(folder);
    if !path.is_dir() || path.is_symlink() {
        return Err("The saved drop folder is no longer available".into());
    }
    #[cfg(target_os = "macos")]
    let program = "open";
    #[cfg(target_os = "windows")]
    let program = "explorer";
    #[cfg(all(unix, not(target_os = "macos")))]
    let program = "xdg-open";
    Command::new(program).arg(path).spawn().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
async fn cancel_assisted(game: String, job_id: String, app: tauri::AppHandle) -> Result<Value, String> {
    let confirmed = rfd::AsyncMessageDialog::new()
        .set_level(rfd::MessageLevel::Warning)
        .set_title("Remove prepared files?")
        .set_description("This removes files copied into this job’s owned preparation folder. It does not change the selected source. This cannot be undone.")
        .set_buttons(rfd::MessageButtons::YesNo)
        .show()
        .await;
    if confirmed != rfd::MessageDialogResult::Yes {
        return Ok(json!({"confirmed": false}));
    }
    query(json!({"action":"assisted_cancel", "game":game, "jobId":job_id,
                 "confirmed":true}), app).await
}

#[tauri::command]
fn start_job(mut request: Value, app: tauri::AppHandle, state: State<Library>) -> Result<(), String> {
    if !matches!(request["action"].as_str(), Some("install" | "play")) {
        return Err("Unsupported job".into());
    }
    let mut active = state.job.lock().unwrap();
    if active.is_some() {
        return Err("Another job is already running".into());
    }
    let cancel_file = state.storage.join(format!(
        "cancel-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_nanos()
    ));
    request["cancelFile"] = json!(cancel_file);
    let settings = state.settings.lock().unwrap().clone();
    let mut cmd = command(&settings, &state.storage, &request);
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Cannot start Python: {e}"))?;
    *active = Some(cancel_file.clone());
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let game = request["game"].as_str().unwrap_or("").to_owned();
    std::thread::spawn(move || {
        let err_app = app.clone();
        let error_reader = std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                let _ = err_app.emit("library-job", json!({"kind": "log", "text": line}));
            }
        });
        let mut outcome = None;
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let event = serde_json::from_str::<Value>(&line)
                .unwrap_or_else(|_| json!({"kind": "log", "text": line}));
            if event["kind"] == "result" || event["kind"] == "error" {
                outcome = Some(event);
            } else {
                let _ = app.emit("library-job", event);
            }
        }
        let status = child.wait();
        let _ = error_reader.join();
        *app.state::<Library>().job.lock().unwrap() = None;
        let _ = std::fs::remove_file(cancel_file);
        let event = match status {
            Ok(status) if status.success() => outcome.unwrap_or_else(|| json!({"kind": "error", "message": "Job returned no result"})),
            _ => outcome.filter(|event| event["kind"] == "error")
                .unwrap_or_else(|| json!({"kind": "error", "message": "Job stopped or failed; see the build log"})),
        };
        let _ = app.emit(
            "library-job",
            json!({"kind": "finished", "game": game, "outcome": event}),
        );
    });
    Ok(())
}

fn stop(state: &Library) -> Result<(), String> {
    if let Some(path) = state.job.lock().unwrap().as_ref() {
        std::fs::write(path, b"cancel").map_err(|e| format!("Cannot cancel installation: {e}"))?;
    }
    Ok(())
}

#[tauri::command]
fn cancel_job(state: State<Library>) -> Result<(), String> {
    stop(&state)
}

#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    if !url.starts_with("https://") {
        return Err("Only HTTPS setup guides can be opened".into());
    }
    #[cfg(target_os = "macos")]
    let program = "open";
    #[cfg(target_os = "windows")]
    let program = "explorer";
    #[cfg(all(unix, not(target_os = "macos")))]
    let program = "xdg-open";
    Command::new(program)
        .arg(url)
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let legacy = app.path().app_data_dir()?;
            let data = app.path().data_dir()?.join("recomp-kit");
            std::fs::create_dir_all(&data)?;
            let settings_file = data.join("settings.json");
            let settings = std::fs::read(&settings_file)
                .or_else(|_| std::fs::read(legacy.join("settings.json")))
                .ok()
                .and_then(|bytes| serde_json::from_slice(&bytes).ok())
                .unwrap_or_else(|| Settings {
                    kit: option_env!("RECOMP_LIBRARY_KIT").unwrap_or("").into(),
                    python: option_env!("RECOMP_LIBRARY_PYTHON")
                        .unwrap_or("python3")
                        .into(),
                });
            if !settings_file.exists() {
                std::fs::write(&settings_file, serde_json::to_vec(&settings)?)?;
            }
            let storage = data.join("games");
            std::fs::create_dir_all(&storage)?;
            app.manage(Library {
                settings: Mutex::new(settings),
                job: Mutex::new(None),
                storage,
                settings_file,
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            settings,
            save_settings,
            pick,
            query,
            start_job,
            cancel_job,
            open_assisted_folder,
            cancel_assisted,
            open_url
        ])
        .build(tauri::generate_context!())
        .expect("Unable to create Recomp Library")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                if let Err(error) = stop(&app.state::<Library>()) {
                    eprintln!("{error}");
                }
            }
        });
}
