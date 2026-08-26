// The desktop shell.
//
// Deliberately thin. It starts the Python backend, waits for it to announce a
// port, and points a webview at it. Every decision that could be got wrong --
// which address to bind, where the database lives, whether the login screen
// appears -- is in `scopemaker/desktop.py`, where it can be tested. Rust here
// is a process supervisor and a window, nothing more.
//
// The three things this file has to get right:
//
//   1. Read the port from the child rather than agree one in advance. A fixed
//      port collides with whatever else the user runs; a guessed one races.
//   2. Kill the backend when the window closes. Otherwise closing the app
//      leaves a server holding the user's database, and the next launch fails
//      to bind.
//   3. Fail loudly. A backend that dies during startup must produce a message,
//      not a blank white webview that looks like a hung application.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, WindowEvent};

/// How long to wait for the backend to announce its port. Generous: first run
/// creates the database and seeds 236 clauses.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);

/// Holds the backend so it can be killed on exit.
struct Backend(Mutex<Option<Child>>);

/// Tie the child's lifetime to this process at the OS level.
///
/// The window-close handler covers the normal path, and covers nothing else. A
/// crash, or a kill from Task Manager, runs no Rust at all -- and the backend
/// survives, holding the user's SQLite file open. Each such orphan accumulates.
///
/// A Job Object with `KILL_ON_JOB_CLOSE` makes Windows do the cleanup: when the
/// last handle to the job closes, which happens when this process dies for any
/// reason, every process in the job is terminated.
///
/// Deliberately best-effort. If the job cannot be created the app should still
/// run -- an orphaned backend is worse than nothing, but a desktop app that
/// refuses to open is worse still.
#[cfg(windows)]
fn tie_to_process_lifetime(child: &Child) {
    use std::os::windows::io::AsRawHandle;

    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            eprintln!("Could not create a job object; the backend may outlive this window.");
            return;
        }

        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

        let ok = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const std::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if ok == 0 {
            eprintln!("Could not configure the job object; the backend may outlive this window.");
            return;
        }

        if AssignProcessToJobObject(job, child.as_raw_handle() as _) == 0 {
            eprintln!("Could not assign the backend to a job object.");
        }
        // The job handle is deliberately leaked: it must stay open for the
        // lifetime of this process, and closing it early would kill the child.
        std::mem::forget(job);
    }
}

#[cfg(not(windows))]
fn tie_to_process_lifetime(_child: &Child) {}

/// Start the Python backend and block until it prints its port.
///
/// Returns the port, or an error describing what the backend said before it
/// gave up -- which is the difference between a useful dialog and a blank
/// window.
fn start_backend() -> Result<(Child, u16), String> {
    // In a bundled build this is the sidecar next to the executable. In
    // development it runs the module with an interpreter that has ScopeMaker
    // installed -- which is rarely the bare `python` on PATH. On the machine
    // this was written, that resolved to an unrelated Python 3.10 with none of
    // the dependencies, and the failure was a ModuleNotFoundError with no
    // obvious connection to the shell. SCOPEMAKER_PYTHON names the right one.
    let (program, args): (String, Vec<String>) = if cfg!(debug_assertions) {
        let interpreter =
            std::env::var("SCOPEMAKER_PYTHON").unwrap_or_else(|_| "python".into());
        (
            interpreter,
            vec!["-m".into(), "scopemaker.desktop".into()],
        )
    } else {
        ("scopemaker-backend".into(), vec![])
    };

    let mut child = Command::new(&program)
        .args(&args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Could not start the ScopeMaker backend ({program}): {e}"))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "The backend produced no output.".to_string())?;

    let started = Instant::now();
    let reader = BufReader::new(stdout);

    for line in reader.lines() {
        let line = line.map_err(|e| format!("Could not read from the backend: {e}"))?;

        if let Some(rest) = line.strip_prefix("SCOPEMAKER_PORT=") {
            let port: u16 = rest
                .trim()
                .parse()
                .map_err(|_| format!("The backend announced an unusable port: {rest:?}"))?;
            return Ok((child, port));
        }

        if started.elapsed() > STARTUP_TIMEOUT {
            let _ = child.kill();
            return Err("The backend did not start in time.".into());
        }
    }

    // stdout closed without a port line: the process died on startup. Its
    // stderr is the only thing that explains why, so put it in the message.
    let mut detail = String::new();
    if let Some(stderr) = child.stderr.take() {
        for line in BufReader::new(stderr).lines().map_while(Result::ok).take(20) {
            detail.push_str(&line);
            detail.push('\n');
        }
    }
    let _ = child.wait();
    Err(format!(
        "The ScopeMaker backend stopped during startup.\n\n{}",
        if detail.is_empty() {
            "It produced no error output.".into()
        } else {
            detail
        }
    ))
}

fn main() {
    let (child, port) = match start_backend() {
        Ok(pair) => pair,
        Err(message) => {
            // No window yet, so stderr plus a dialog is the only channel that
            // reaches a user. The dialog is modal and waits for a human, which
            // would hang the Phase 4 smoke test -- so CI sets this to skip it
            // and read stderr instead.
            eprintln!("{message}");
            if std::env::var_os("SCOPEMAKER_NO_DIALOG").is_none() {
                rfd_message(&message);
            }
            std::process::exit(1);
        }
    };

    // Before anything else can go wrong: make the OS responsible for the child.
    tie_to_process_lifetime(&child);

    let url = format!("http://127.0.0.1:{port}");

    tauri::Builder::default()
        .manage(Backend(Mutex::new(Some(child))))
        .setup(move |app| {
            let window = tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::External(url.parse().expect("a valid loopback url")),
            )
            .title("ScopeMaker")
            .inner_size(1280.0, 860.0)
            .min_inner_size(900.0, 600.0)
            .build()?;

            let handle = app.handle().clone();
            window.on_window_event(move |event| {
                if matches!(event, WindowEvent::Destroyed) {
                    stop_backend(&handle);
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to start the ScopeMaker window");
}

/// Kill the backend. Called when the window closes and again on exit, because
/// leaving it running holds the database open and the next launch cannot bind.
fn stop_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Backend>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

/// A message box for the case where startup failed before there was a window.
/// Kept trivial rather than pulling in a dialog crate for one string.
#[cfg(target_os = "windows")]
fn rfd_message(text: &str) {
    use std::os::windows::ffi::OsStrExt;
    let wide: Vec<u16> = std::ffi::OsStr::new(text)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let title: Vec<u16> = std::ffi::OsStr::new("ScopeMaker")
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(hwnd: isize, text: *const u16, caption: *const u16, utype: u32) -> i32;
    }
    unsafe {
        MessageBoxW(0, wide.as_ptr(), title.as_ptr(), 0x10);
    }
}

#[cfg(not(target_os = "windows"))]
fn rfd_message(_text: &str) {}
