//! Daemon-owned trusted brokers retain provider stdio when the HTTP container dies.
//! Coding workers never receive these private mounts or Docker authority.
use super::*;

const ROLE: &str = "software-factory.role";

impl DockerWorker {
    pub(super) async fn inspect_daemon_broker(
        &self,
        receipt: &WorkerReceipt,
    ) -> Result<Option<Value>> {
        let broker = self
            .docker
            .broker
            .as_ref()
            .ok_or_else(|| Error::Config("daemon broker is not configured".into()))?;
        let mut identity = receipt.clone();
        identity.container_name = format!("{}-broker", self.container_name(&receipt.operation_id));
        identity.container_id = receipt.broker_container_id.clone();
        identity.image_id = Some(broker.image.clone());
        let inspected = self.inspect(&identity).await?;
        if inspected
            .as_ref()
            .is_some_and(|value| value["Config"]["Labels"][ROLE] != "broker")
        {
            return Err(Error::Worker("saved daemon broker role differs".into()));
        }
        Ok(inspected)
    }

    pub(super) async fn stop_daemon_broker(&self, receipt: &WorkerReceipt) -> Result<()> {
        if self.docker.broker.is_none() || receipt.broker_container_id.is_none() {
            return Ok(());
        }
        if let Some(current) = self.inspect_daemon_broker(receipt).await? {
            if current["State"]["Running"] == true {
                let _ = self
                    .output(&[
                        "container".into(),
                        "kill".into(),
                        "--signal=KILL".into(),
                        current["Id"]
                            .as_str()
                            .ok_or_else(|| Error::Worker("daemon broker ID missing".into()))?
                            .into(),
                    ])
                    .await;
            }
            if self
                .inspect_daemon_broker(receipt)
                .await?
                .is_some_and(|state| {
                    state["State"]["Running"] != false || state["State"]["Pid"] != 0
                })
            {
                return Err(Error::Worker("daemon broker cleanup is unconfirmed".into()));
            }
        }
        Ok(())
    }

    pub(super) async fn launch_daemon_broker(&self, mut receipt: WorkerReceipt) -> Result<()> {
        let broker = self
            .docker
            .broker
            .as_ref()
            .ok_or_else(|| Error::Config("daemon broker is not configured".into()))?;
        let current = self.inspect_daemon_broker(&receipt).await?;
        let id = if let Some(current) = &current {
            current["Id"]
                .as_str()
                .ok_or_else(|| Error::Worker("daemon broker has no Docker ID".into()))?
                .to_owned()
        } else {
            if receipt.broker_container_id.is_some() {
                return Err(Error::Worker(
                    "saved daemon broker disappeared; reconcile its operation receipt".into(),
                ));
            }
            let args = vec![
                "container".into(),
                "create".into(),
                "--rm".into(),
                "--init".into(),
                "--name".into(),
                format!("{}-broker", self.container_name(&receipt.operation_id)),
                "--label".into(),
                format!("{OPERATION}={}", receipt.operation_id),
                "--label".into(),
                format!("{OWNER}={}", self.owner),
                "--label".into(),
                format!(
                    "{WORKSPACE}={}",
                    digest(self.config.workspace().as_os_str().as_encoded_bytes())
                ),
                "--label".into(),
                format!("{ROLE}=broker"),
                "--read-only".into(),
                "--cap-drop=ALL".into(),
                "--security-opt=no-new-privileges".into(),
                "--network=none".into(),
                "--pids-limit=128".into(),
                "--memory=512m".into(),
                "--cpus=1".into(),
                "--user".into(),
                format!("{}:{}", self.docker.uid, self.docker.gid),
                "--group-add".into(),
                broker.socket_gid.to_string(),
                "--tmpfs".into(),
                "/tmp:rw,nosuid,nodev,size=128m,mode=1777".into(),
                "--env".into(),
                "HOME=/tmp".into(),
                "--mount".into(),
                "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock".into(),
                "--mount".into(),
                self.mount(
                    &self.docker.daemon_data_dir,
                    &self.config.data_dir().to_string_lossy(),
                    false,
                    self.docker.data_volume.as_deref(),
                    None,
                )?,
                "--mount".into(),
                self.mount(
                    &self.docker.daemon_workspace,
                    &self.config.workspace().to_string_lossy(),
                    false,
                    self.docker.workspace_volume.as_deref(),
                    None,
                )?,
                "--entrypoint".into(),
                "factory-runtime".into(),
                broker.image.clone(),
                self.config.data_dir().to_string_lossy().into_owned(),
                self.config.workspace().to_string_lossy().into_owned(),
                "worker-broker".into(),
                receipt.operation_id.clone(),
            ];
            let id = self.output(&args).await?.trim().to_owned();
            if id.len() != 64 || !id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
                return Err(Error::Worker(
                    "Docker create returned an invalid broker ID".into(),
                ));
            }
            id
        };
        if current
            .as_ref()
            .is_some_and(|state| state["State"]["Running"] == true)
        {
            return Ok(());
        }
        receipt.broker_container_id = Some(id.clone());
        self.save(&receipt)?;
        // Starting this exact broker does not submit another provider turn. Its
        // cross-container file lock and saved creation intent fence dispatch.
        self.output(&["container".into(), "start".into(), id])
            .await?;
        Ok(())
    }
}
