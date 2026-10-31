//! Host commands run in a controller-owned process group. Dropping a cancelled
//! command kills the group, including credential-helper descendants.
use crate::{Error, Result};
use std::{
    process::{Output, Stdio},
    time::Duration,
};
use tokio::{io::AsyncWriteExt, process::Command};
struct ProcessGroup(u32);
impl Drop for ProcessGroup {
    fn drop(&mut self) {
        // The ID comes only from our newly spawned private group, never a PID
        // supplied by an agent, a receipt from another namespace, or a user.
        unsafe {
            libc::kill(-(self.0 as i32), libc::SIGKILL);
        }
    }
}
pub(crate) async fn output(
    mut command: Command,
    input: Option<&[u8]>,
    timeout: Duration,
) -> Result<Output> {
    command
        .process_group(0)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = command.spawn()?;
    let _group = ProcessGroup(
        child
            .id()
            .ok_or_else(|| Error::Worker("owned command has no process identity".into()))?,
    );
    let mut stdin = child.stdin.take().unwrap();
    if let Some(input) = input {
        stdin.write_all(input).await?;
    }
    drop(stdin);
    tokio::time::timeout(timeout, child.wait_with_output())
        .await
        .map_err(|_| {
            Error::Worker("controlled command timed out; reconcile its saved operation".into())
        })?
        .map_err(Error::from)
}
