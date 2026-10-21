//! Own and terminate the export worker's complete process tree.

use std::{io, process::Child, process::Command};

#[cfg(unix)]
use std::{thread, time::Duration, time::Instant};

#[cfg(unix)]
const TERM_GRACE: Duration = Duration::from_millis(250);
#[cfg(unix)]
const KILL_GRACE: Duration = Duration::from_millis(750);

pub fn configure(command: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
}

pub struct ProcessTree {
    #[cfg(unix)]
    process_group: libc::pid_t,
    #[cfg(windows)]
    job: crate::job::Job,
}

impl ProcessTree {
    pub fn attach(child: &Child) -> io::Result<Self> {
        #[cfg(unix)]
        {
            let process_group = libc::pid_t::try_from(child.id()).map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "Worker process ID is too large")
            })?;
            // SAFETY: getpgid only reads kernel process metadata for this numeric PID.
            if unsafe { libc::getpgid(process_group) } != process_group {
                return Err(io::Error::other(
                    "Worker did not start in its owned process group",
                ));
            }
            Ok(Self { process_group })
        }
        #[cfg(windows)]
        {
            Ok(Self {
                job: crate::job::Job::attach(child)?,
            })
        }
    }

    pub fn terminate(&self, child: &mut Child) {
        #[cfg(unix)]
        if matches!(child.try_wait(), Ok(Some(_))) && !group_exists(self.process_group) {
            return;
        }
        #[cfg(windows)]
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        #[cfg(unix)]
        self.terminate_unix(child);
        #[cfg(windows)]
        {
            let _ = self.job.terminate();
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    #[cfg(unix)]
    fn terminate_unix(&self, child: &mut Child) {
        signal_group(self.process_group, libc::SIGTERM);
        if !wait_for_group(child, self.process_group, TERM_GRACE) {
            signal_group(self.process_group, libc::SIGKILL);
            wait_for_group(child, self.process_group, KILL_GRACE);
        }
        let _ = child.wait();
    }
}

#[cfg(unix)]
fn signal_group(process_group: libc::pid_t, signal: libc::c_int) {
    // SAFETY: a negative PID addresses the process group created for this worker.
    unsafe {
        libc::kill(-process_group, signal);
    }
}

#[cfg(unix)]
fn group_exists(process_group: libc::pid_t) -> bool {
    // SAFETY: signal 0 checks existence and does not alter the target process group.
    if unsafe { libc::kill(-process_group, 0) } == 0 {
        return true;
    }
    io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
}

#[cfg(unix)]
fn wait_for_group(child: &mut Child, process_group: libc::pid_t, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        let _ = child.try_wait();
        if !group_exists(process_group) {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;

    #[test]
    fn termination_escalates_and_removes_worker_descendants() {
        let mut command = Command::new("sh");
        command
            .arg("-c")
            .arg("trap '' TERM; sleep 60 & printf '%s\\n' $!; wait")
            .stdout(Stdio::piped());
        configure(&mut command);
        let mut child = command.spawn().expect("spawn worker fixture");
        let tree = ProcessTree::attach(&child).expect("attach worker process group");
        let mut output = BufReader::new(child.stdout.take().expect("worker stdout"));
        let mut line = String::new();
        output.read_line(&mut line).expect("read descendant PID");
        let descendant: libc::pid_t = line.trim().parse().expect("numeric descendant PID");
        assert!(process_exists(descendant));

        let started = Instant::now();
        tree.terminate(&mut child);

        assert!(started.elapsed() < Duration::from_millis(1500));
        assert!(child.try_wait().expect("worker status").is_some());
        assert!(!group_exists(tree.process_group));
        assert!(!process_exists(descendant));
    }

    fn process_exists(pid: libc::pid_t) -> bool {
        // SAFETY: signal 0 checks existence and does not alter the target process.
        if unsafe { libc::kill(pid, 0) } == 0 {
            return true;
        }
        io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
    }
}
