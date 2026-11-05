use factory_runtime::{
    Config, CrashPoint, Error, FakeWorker, Faults, Result, Runtime, StartRequest,
};
use std::sync::Arc;

struct ProcessFaults;
impl Faults for ProcessFaults {
    fn hit(&self, point: CrashPoint) {
        if std::env::var_os("FACTORY_CRASH_OPERATION_CONTAINS").is_none() {
            self.crash(point);
        }
    }
    fn hit_operation(&self, point: CrashPoint, operation: &str) {
        if std::env::var("FACTORY_CRASH_OPERATION_CONTAINS")
            .ok()
            .is_none_or(|filter| operation.contains(&filter))
        {
            self.crash(point);
        }
    }
}
impl ProcessFaults {
    fn crash(&self, point: CrashPoint) {
        let name = match point {
            CrashPoint::BeforeDispatch => "before_dispatch",
            CrashPoint::AfterDispatch => "after_dispatch",
            CrashPoint::AfterResult => "after_result",
            CrashPoint::AfterQueueFinish => "after_queue_finish",
            CrashPoint::AfterHandoffIntent => "after_handoff_intent",
        };
        if std::env::var("FACTORY_SLICE_CRASH_AT").as_deref() == Ok(name) {
            // Acceptance-only abrupt exit: no Rust destructors or shutdown hooks.
            std::process::exit(86);
        }
    }
}

#[tokio::main]
async fn main() {
    if let Err(error) = command(std::env::args().skip(1).collect()).await {
        eprintln!("{}", serde_json::json!({"error": error.to_string()}));
        std::process::exit(1);
    }
}

async fn command(args: Vec<String>) -> Result<()> {
    if args.len() < 3 {
        return Err(usage());
    }
    let config = Config::new(&args[0], &args[1])?;
    if let ("worker-broker", [operation]) = (args[2].as_str(), &args[3..]) {
        let docker = serde_json::from_slice(&std::fs::read(
            config.data_dir().join("docker-backend.json"),
        )?)?;
        return factory_runtime::workflow::docker::DockerWorker::open(config, docker)?
            .broker(operation)
            .await;
    }
    if let ("configure-docker", [path]) = (args[2].as_str(), &args[3..]) {
        let docker = serde_json::from_slice(&std::fs::read(path)?)?;
        Runtime::open_docker(config, docker).await?;
        println!("{}", serde_json::json!({"configured":"docker"}));
        return Ok(());
    }
    let worker: Arc<dyn factory_runtime::Worker> =
        if config.data_dir().join("docker-backend.json").exists() {
            Arc::new(factory_runtime::workflow::docker::DockerWorker::open(
                config.clone(),
                serde_json::from_slice(&std::fs::read(
                    config.data_dir().join("docker-backend.json"),
                )?)?,
            )?)
        } else {
            Arc::new(FakeWorker::open(&config.data_dir().join("worker-records")).await?)
        };
    let runtime = Runtime::open_with_worker(config, worker, Arc::new(ProcessFaults)).await?;
    let value = match (args[2].as_str(), &args[3..]) {
        ("start", [request]) => serde_json::to_value(
            runtime
                .start(serde_json::from_str::<StartRequest>(request)?)
                .await?,
        )?,
        ("implement", [request, path]) => {
            let mut input: StartRequest = serde_json::from_str(request)?;
            if input.implementation.is_some() {
                return Err(usage());
            }
            input.implementation = Some(factory_runtime::workflow::ImplementationInput {
                config: factory_runtime::workflow::WorkflowConfig::from_yaml(
                    &std::fs::read_to_string(path)?,
                )?,
                required_criteria: vec![],
                approved_plan: None,
            });
            serde_json::to_value(runtime.start(input).await?)?
        }
        ("answer-questions", [id, interaction, answers]) => serde_json::to_value(
            runtime
                .answer_questions(id, interaction, serde_json::from_str(answers)?)
                .await?,
        )?,
        ("deliver", [id]) => serde_json::to_value(runtime.deliver(id, None).await?)?,
        ("deliver", [id, path]) => serde_json::to_value(
            runtime
                .deliver(id, Some(serde_json::from_slice(&std::fs::read(path)?)?))
                .await?,
        )?,
        ("fix-reviews", [id, threads @ ..]) if !threads.is_empty() => {
            serde_json::to_value(runtime.fix_reviews(id, threads).await?)?
        }
        ("cancel-delivery", [id]) => serde_json::to_value(runtime.cancel_delivery(id).await?)?,
        ("status", [id]) => serde_json::to_value(runtime.status(id).await?)?,
        ("resume", [id]) => serde_json::to_value(runtime.resume(id).await?)?,
        ("cancel", [id]) => serde_json::to_value(runtime.cancel(id).await?)?,
        ("answer", [id, interaction, answer]) => {
            let approved = match answer.as_str() {
                "true" => true,
                "false" => false,
                _ => return Err(usage()),
            };
            serde_json::to_value(runtime.answer(id, interaction, approved).await?)?
        }
        ("list", []) => serde_json::to_value(runtime.list().await?)?,
        ("import-planning", [path]) => {
            let control = factory_runtime::control::Control::open(runtime).await?;
            control
                .import_planning(serde_json::from_slice(&std::fs::read(path)?)?)
                .await?
        }
        ("control", [operation, payload]) => {
            let control = factory_runtime::control::Control::open(runtime).await?;
            control
                .call(operation, serde_json::from_str(payload)?)
                .await?
        }
        ("serve", []) | ("web", []) => {
            let control = factory_runtime::control::Control::open(runtime).await?;
            let token = match std::env::var("FACTORY_HTTP_TOKEN") {
                Ok(token) if token.len() >= 24 => token,
                Ok(_) => {
                    return Err(Error::Config(
                        "FACTORY_HTTP_TOKEN must contain at least 24 characters".into(),
                    ));
                }
                Err(_) => {
                    use std::io::Read;
                    let mut random = [0u8; 32];
                    std::fs::File::open("/dev/urandom")?.read_exact(&mut random)?;
                    random.iter().map(|byte| format!("{byte:02x}")).collect()
                }
            };
            return factory_runtime::http::serve(
                control,
                &std::env::var("FACTORY_HTTP_BIND").unwrap_or_else(|_| "127.0.0.1:4317".into()),
                &token,
            )
            .await;
        }
        _ => return Err(usage()),
    };
    println!("{}", serde_json::to_string(&value)?);
    Ok(())
}

fn usage() -> Error {
    Error::Config("usage: factory-runtime <absolute-data-dir> <absolute-workspace> configure-docker <backend-json-file> | implement <StartRequest-json> <config-yaml> | start <StartRequest-json> | status/resume/cancel <run-id> | answer <run-id> <interaction-id> true/false | answer-questions <run-id> <interaction-id> <answers-json> | deliver <run-id> [policy-json-file] | fix-reviews <run-id> <thread-id>... | cancel-delivery <run-id> | list | control <operation> <json> | serve".into())
}
