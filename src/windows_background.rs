//! Hold a Windows process handle while checking identity and terminating it.
//! PID reuse must never turn one environment's stop into another one's stop.
use crate::brand::BRAND;
use anyhow::{Context, Result, bail};
use std::ffi::c_void;

#[repr(C)]
#[derive(Default)]
struct FileTime {
    low: u32,
    high: u32,
}

type Handle = *mut c_void;
#[link(name = "kernel32")]
unsafe extern "system" {
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> Handle;
    fn CloseHandle(handle: Handle) -> i32;
    fn GetProcessTimes(
        handle: Handle,
        created: *mut FileTime,
        exited: *mut FileTime,
        kernel: *mut FileTime,
        user: *mut FileTime,
    ) -> i32;
    fn WaitForSingleObject(handle: Handle, milliseconds: u32) -> u32;
    fn TerminateProcess(handle: Handle, exit_code: u32) -> i32;
    fn GetExitCodeProcess(handle: Handle, exit_code: *mut u32) -> i32;
}

const PROCESS_TERMINATE: u32 = 0x0001;
const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
const SYNCHRONIZE: u32 = 0x0010_0000;
const ERROR_INVALID_PARAMETER: i32 = 87;
const STILL_ACTIVE: u32 = 259;

pub(crate) struct Process {
    handle: Handle,
    pub pid: u32,
    pub created: u64,
    /// Opened with query rights only because Windows refused the full
    /// rights: the process may be inspected but not waited on or stopped.
    restricted: bool,
}

impl Process {
    /// Opens `pid` for an identity check. `Ok(None)` means no process the
    /// caller could own: the PID is gone, or it now belongs to a process the
    /// current user may not even query (a recorded PID reused by a protected
    /// or another user's process). When only the requested rights are
    /// refused, the process comes back restricted so callers can still
    /// compare its creation time against the recorded one.
    pub fn open(pid: u32, terminate: bool) -> Result<Option<Self>> {
        if pid <= 1 {
            bail!("invalid background process ID");
        }
        let rights = PROCESS_QUERY_LIMITED_INFORMATION
            | SYNCHRONIZE
            | if terminate { PROCESS_TERMINATE } else { 0 };
        let mut restricted = false;
        let mut handle = unsafe { OpenProcess(rights, 0, pid) };
        if handle.is_null() {
            let error = std::io::Error::last_os_error();
            match error.raw_os_error() {
                Some(ERROR_INVALID_PARAMETER) => return Ok(None),
                Some(ERROR_ACCESS_DENIED) => {
                    handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
                    if handle.is_null() {
                        let limited = std::io::Error::last_os_error();
                        if matches!(
                            limited.raw_os_error(),
                            Some(ERROR_INVALID_PARAMETER | ERROR_ACCESS_DENIED)
                        ) {
                            return Ok(None);
                        }
                        return Err(limited).with_context(inspect_failed);
                    }
                    restricted = true;
                }
                _ => return Err(error).with_context(inspect_failed),
            }
        }
        let mut process = Self {
            handle,
            pid,
            created: 0,
            restricted,
        };
        let (mut created, mut exited, mut kernel, mut user) = (
            FileTime::default(),
            FileTime::default(),
            FileTime::default(),
            FileTime::default(),
        );
        if unsafe { GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user) }
            == 0
        {
            return Err(std::io::Error::last_os_error()).with_context(|| {
                format!(
                    "cannot read {brand_product} process creation time",
                    brand_product = BRAND.product_name
                )
            });
        }
        process.created = (u64::from(created.high) << 32) | u64::from(created.low);
        Ok(Some(process))
    }

    pub fn running(&self) -> bool {
        if self.restricted {
            // No SYNCHRONIZE right: ask for the exit code instead.
            let mut code = 0;
            return unsafe { GetExitCodeProcess(self.handle, &mut code) } != 0
                && code == STILL_ACTIVE;
        }
        unsafe { WaitForSingleObject(self.handle, 0) == WAIT_TIMEOUT }
    }

    pub fn stop(&self) -> Result<()> {
        if self.restricted {
            if !self.running() {
                return Ok(());
            }
            bail!(
                "selected {brand_product} process (PID {pid}) is running with higher privileges; \
                 stop it from an administrator terminal or restart Windows, then retry",
                brand_product = BRAND.product_name,
                pid = self.pid
            );
        }
        match self.wait(0) {
            Wait::Exited => return Ok(()),
            Wait::TimedOut => {}
            Wait::Failed(error) => return Err(error).with_context(wait_failed),
        }
        // Read the error at once: the waits below overwrite the thread's
        // last error.
        let terminate_error = if unsafe { TerminateProcess(self.handle, 0) } == 0 {
            Some(std::io::Error::last_os_error())
        } else {
            None
        };
        finish_stop(terminate_error, |milliseconds| self.wait(milliseconds))
    }

    /// Waits on this verified handle (never a PID reopened later).
    fn wait(&self, milliseconds: u32) -> Wait {
        match unsafe { WaitForSingleObject(self.handle, milliseconds) } {
            WAIT_OBJECT_0 => Wait::Exited,
            WAIT_TIMEOUT => Wait::TimedOut,
            WAIT_FAILED => Wait::Failed(std::io::Error::last_os_error()),
            other => Wait::Failed(std::io::Error::other(format!(
                "unexpected wait result {other:#x}"
            ))),
        }
    }
}

const WAIT_OBJECT_0: u32 = 0;
const WAIT_TIMEOUT: u32 = 258;
const WAIT_FAILED: u32 = 0xFFFF_FFFF;
const ERROR_ACCESS_DENIED: i32 = 5;
const EXIT_WAIT_MILLISECONDS: u32 = 10_000;

#[derive(Debug)]
enum Wait {
    Exited,
    TimedOut,
    Failed(std::io::Error),
}

/// Outcome of a stop after `TerminateProcess`. Termination counts only when
/// the process is seen to exit. Access denied usually means the process is
/// already exiting, so it gets the same bounded wait; if the process is
/// still there afterwards the original termination error is returned.
/// Other termination errors succeed only if the process has already exited.
fn finish_stop(
    terminate_error: Option<std::io::Error>,
    wait: impl FnOnce(u32) -> Wait,
) -> Result<()> {
    let milliseconds = match &terminate_error {
        None => EXIT_WAIT_MILLISECONDS,
        Some(error) if error.raw_os_error() == Some(ERROR_ACCESS_DENIED) => EXIT_WAIT_MILLISECONDS,
        Some(_) => 0,
    };
    match (wait(milliseconds), terminate_error) {
        (Wait::Exited, _) => Ok(()),
        (Wait::Failed(error), _) => Err(error).with_context(wait_failed),
        (Wait::TimedOut, Some(error)) => Err(error).with_context(|| {
            format!(
                "cannot terminate selected {brand_product} process",
                brand_product = BRAND.product_name
            )
        }),
        (Wait::TimedOut, None) => bail!(
            "selected {brand_product} process did not exit after termination",
            brand_product = BRAND.product_name
        ),
    }
}

fn inspect_failed() -> String {
    format!(
        "cannot inspect {brand_product} process identity",
        brand_product = BRAND.product_name
    )
}

fn wait_failed() -> String {
    format!(
        "cannot wait for selected {brand_product} process to exit",
        brand_product = BRAND.product_name
    )
}

impl Drop for Process {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.handle);
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::cell::Cell;

    fn os_error(code: i32) -> std::io::Error {
        std::io::Error::from_raw_os_error(code)
    }

    fn os_code(error: &anyhow::Error) -> Option<i32> {
        error
            .chain()
            .find_map(|cause| cause.downcast_ref::<std::io::Error>())
            .and_then(std::io::Error::raw_os_error)
    }

    #[test]
    fn access_denied_counts_only_once_the_process_has_exited() {
        let waited = Cell::new(None);
        finish_stop(Some(os_error(ERROR_ACCESS_DENIED)), |ms| {
            waited.set(Some(ms));
            Wait::Exited
        })
        .unwrap();
        assert_eq!(waited.get(), Some(EXIT_WAIT_MILLISECONDS));

        // Still running after the bounded wait: the original error, kept.
        let error =
            finish_stop(Some(os_error(ERROR_ACCESS_DENIED)), |_| Wait::TimedOut).unwrap_err();
        assert_eq!(os_code(&error), Some(ERROR_ACCESS_DENIED));
        assert_eq!(error.to_string(), "cannot terminate selected KTEST process");
    }

    #[test]
    fn other_termination_errors_are_not_waited_out() {
        let waited = Cell::new(None);
        let error = finish_stop(Some(os_error(6)), |ms| {
            waited.set(Some(ms));
            Wait::TimedOut
        })
        .unwrap_err();
        assert_eq!(waited.get(), Some(0));
        assert_eq!(os_code(&error), Some(6));
        // Already gone: nothing left to stop.
        finish_stop(Some(os_error(6)), |_| Wait::Exited).unwrap();
    }

    #[test]
    fn a_failed_wait_is_reported_as_such() {
        for terminate_error in [None, Some(os_error(ERROR_ACCESS_DENIED))] {
            let error = finish_stop(terminate_error, |_| Wait::Failed(os_error(6))).unwrap_err();
            assert_eq!(os_code(&error), Some(6));
            assert_eq!(
                error.to_string(),
                "cannot wait for selected KTEST process to exit"
            );
        }
        let error = finish_stop(None, |_| Wait::TimedOut).unwrap_err();
        assert_eq!(
            error.to_string(),
            "selected KTEST process did not exit after termination"
        );
    }

    /// The real path on a child process: one that is running (no
    /// grandchildren), and one that exited before the stop. The window
    /// between the running check and TerminateProcess is covered by the
    /// decision tests above.
    #[test]
    fn stop_terminates_a_running_child_and_accepts_one_that_already_exited() {
        let mut running = std::process::Command::new("ping")
            .args(["-n", "30", "127.0.0.1"])
            .stdout(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let process = Process::open(running.id(), true).unwrap().unwrap();
        assert!(process.running());
        process.stop().unwrap();
        assert!(!process.running());
        running.wait().unwrap();

        let mut quick = std::process::Command::new("cmd")
            .args(["/D", "/C", "exit 0"])
            .spawn()
            .unwrap();
        let process = Process::open(quick.id(), true).unwrap().unwrap();
        quick.wait().unwrap();
        process.stop().unwrap();
        assert!(!process.running());
    }

    /// PID of a running csrss.exe: a protected process that even an elevated
    /// test runner may query but never terminate.
    pub(crate) fn protected_process_id() -> u32 {
        let output = std::process::Command::new("tasklist")
            .args(["/FI", "IMAGENAME eq csrss.exe", "/FO", "CSV", "/NH"])
            .output()
            .unwrap();
        let text = String::from_utf8_lossy(&output.stdout);
        text.lines()
            .find_map(|line| line.split(',').nth(1))
            .map(|pid| pid.trim_matches('"').parse().unwrap())
            .expect("csrss.exe is always running")
    }

    /// A recorded PID that Windows reused for a protected process: opening
    /// it to terminate is refused, so the process comes back restricted with
    /// its real creation time and is never stopped.
    #[test]
    fn a_process_that_refuses_termination_is_inspected_but_not_stopped() {
        let pid = protected_process_id();
        let process = Process::open(pid, true).unwrap().unwrap();
        assert!(process.restricted);
        assert_ne!(process.created, 0);
        assert!(process.running());
        let error = process.stop().unwrap_err().to_string();
        assert!(error.contains("higher privileges"), "{error}");
        assert!(error.contains(&format!("PID {pid}")), "{error}");
        assert!(process.running());
    }
}
