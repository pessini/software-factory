//! Authenticated remote control. No execution state or worker lifetime lives in clients.
use crate::{Error, Result};
use serde_json::{Value, json};
use std::{path::PathBuf, time::Duration};

#[derive(Clone)]
pub struct Client {
    http: reqwest::Client,
    pub origin: String,
    token: String,
    pub workspace: PathBuf,
}
impl Client {
    pub fn environment(workspace: PathBuf) -> Result<Self> {
        let origin =
            std::env::var("FACTORY_CONTROL_URL").unwrap_or_else(|_| "http://127.0.0.1:4317".into());
        let url = reqwest::Url::parse(&origin).map_err(|_| {
            Error::Config("FACTORY_CONTROL_URL must be an HTTP(S) service origin".into())
        })?;
        if !["http", "https"].contains(&url.scheme())
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || url.path() != "/"
        {
            return Err(Error::Config(
                "FACTORY_CONTROL_URL must be an HTTP(S) service origin".into(),
            ));
        }
        if url.scheme() == "http"
            && !matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"))
        {
            return Err(Error::Config(
                "non-loopback Factory connections require HTTPS".into(),
            ));
        }
        let token = std::env::var("FACTORY_HTTP_TOKEN").unwrap_or_default();
        if token.is_empty() {
            return Err(Error::Config("FACTORY_HTTP_TOKEN is required. Start the Rust service with software-factory web --storage /absolute/private/data; configure the same token in the service and client. FACTORY_CONTROL_URL defaults to http://127.0.0.1:4317; legacy recovery uses software-factory-legacy separately".into()));
        }
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| Error::Config("cannot initialize HTTPS client".into()))?;
        Ok(Self {
            http,
            origin: origin.trim_end_matches('/').into(),
            token,
            workspace: workspace.canonicalize()?,
        })
    }
    async fn response(&self, request: reqwest::RequestBuilder) -> Result<Value> {
        let response = request.bearer_auth(&self.token).send().await
            .map_err(|_| Error::Config("Factory service unavailable; start software-factory web or check FACTORY_CONTROL_URL and FACTORY_HTTP_TOKEN".into()))?;
        let status = response.status();
        let value: Value = response
            .json()
            .await
            .map_err(|_| Error::Config(format!("invalid Factory service response ({status})")))?;
        if !status.is_success() {
            return Err(Error::Config(
                value["error"]
                    .as_str()
                    .unwrap_or("Factory service rejected the request")
                    .into(),
            ));
        }
        Ok(value)
    }
    pub async fn verify(&self) -> Result<()> {
        let value = self
            .response(self.http.get(format!("{}/api/sessions", self.origin)))
            .await?;
        if value["workspace"].as_str() != self.workspace.to_str() {
            return Err(Error::Conflict(
                "Factory service belongs to a different workspace".into(),
            ));
        }
        Ok(())
    }
    pub async fn call(&self, operation: &str, value: Value) -> Result<Value> {
        if !operation
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b == b'_')
        {
            return Err(Error::Config("invalid control operation".into()));
        }
        if value["run_id"]
            .as_str()
            .is_some_and(|id| id.starts_with("fct_") || id.ends_with(".sqlite") || id.contains('/'))
        {
            return Err(Error::Incompatible("LangGraph checkpoints are not Rust state; use the separate software-factory-legacy recovery environment".into()));
        }
        self.verify().await?;
        self.response(
            self.http
                .post(format!("{}/api/control/{operation}", self.origin))
                .json(&value),
        )
        .await
    }
    pub async fn settings(&self, session: &str, request: &Value) -> Result<Value> {
        let mut saved = self.call("settings", json!({"session_id":session})).await?;
        let config: crate::workflow::WorkflowConfig =
            serde_json::from_value(saved["configuration"].clone())?;
        if request["action"] == "change" {
            let mut selection: crate::workflow::config::Selection =
                serde_json::from_value(saved["selection"].clone())?;
            let id = request["config_id"]
                .as_str()
                .ok_or_else(|| Error::Config("config_id is required".into()))?;
            let value = request["value"]
                .as_str()
                .ok_or_else(|| Error::Config("value is required".into()))?;
            match id {
                "preset" => selection.preset = (value != "inherit").then(|| value.into()),
                "shared" => {
                    selection.shared = if value == "inherit" {
                        Default::default()
                    } else {
                        serde_json::from_str(value)?
                    }
                }
                step if config.active_steps().contains(&step) => {
                    selection.steps.insert(
                        step.into(),
                        if value == "inherit" {
                            Default::default()
                        } else {
                            serde_json::from_str(value)?
                        },
                    );
                }
                _ => return Err(Error::Config("unknown setting".into())),
            }
            saved = self
                .call(
                    "settings",
                    json!({"session_id":session,"selection":selection}),
                )
                .await?;
        }
        let catalog: Vec<crate::workflow::config::ModelOption> =
            serde_json::from_value(self.call("models", json!({})).await?)?;
        let selection: crate::workflow::config::Selection =
            serde_json::from_value(saved["selection"].clone())?;
        let mut options = vec![
            json!({"id":"preset","name":"Preset","type":"select","description":null,"currentValue":selection.preset.as_deref().unwrap_or("inherit"),"options":std::iter::once(json!({"value":"inherit","name":"Project default"})).chain(config.presets.keys().map(|name|json!({"value":name,"name":name}))).collect::<Vec<_>>()}),
        ];
        for name in std::iter::once("shared").chain(config.active_steps()) {
            let mut choices = vec![json!({"value":"inherit","name":"Use inherited settings"})];
            for model in &catalog {
                let permissions = if name == "shared" {
                    config
                        .active_steps()
                        .iter()
                        .map(|s| config.agent(s).permission_mode.as_str())
                        .collect::<Vec<_>>()
                } else {
                    vec![config.agent(name).permission_mode.as_str()]
                };
                if !permissions
                    .iter()
                    .all(|p| model.permission_modes.iter().any(|m| m == p))
                {
                    continue;
                }
                for effort in &model.efforts {
                    choices.push(json!({"value":serde_json::to_string(&json!({"provider":model.provider,"model":model.model,"reasoning_level":effort}))?,"name":format!("{} / {} / {effort}",model.provider,model.name)}));
                }
            }
            let choice = if name == "shared" {
                selection.shared.clone()
            } else {
                selection.steps.get(name).cloned().unwrap_or_default()
            };
            options.push(json!({"id":name,"name":name.replace('_'," "),"type":"select","description":format!("Resolved: {}",saved["resolved"]["effective"][name]),"currentValue":if choice == Default::default() { "inherit".into() } else { serde_json::to_string(&serde_json::to_value(&choice)?)? },"options":choices}));
        }
        Ok(json!({"sessionId":session,"configOptions":options}))
    }
}

pub fn unique_id() -> String {
    use std::io::Read;
    let mut bytes = [0u8; 16];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .expect("OS randomness unavailable");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
