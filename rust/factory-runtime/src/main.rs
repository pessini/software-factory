use chrono::Utc;
use factory_runtime::{
    Config, CrashPoint, Error, FakeWorker, Faults, Result, Runtime, StartRequest, Status,
};
use std::sync::Arc;

struct ProcessFaults;
impl Faults for ProcessFaults {
    fn hit(&self, point: CrashPoint) {
        let name = match point {
            CrashPoint::BeforeDispatch => "before_dispatch",
            CrashPoint::AfterDispatch => "after_dispatch",
            CrashPoint::AfterResult => "after_result",
            CrashPoint::AfterQueueFinish => "after_queue_finish",
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
    let worker = Arc::new(FakeWorker::open(&config.data_dir().join("worker-records")).await?);
    let runtime = Runtime::open_with_worker(config, worker, Arc::new(ProcessFaults)).await?;
    let value = match (args[2].as_str(), &args[3..]) {
        ("start", [request]) => serde_json::to_value(
            runtime
                .start(serde_json::from_str::<StartRequest>(request)?)
                .await?,
        )?,
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
        ("serve", []) => {
            println!(
                "{}",
                serde_json::json!({"ready": true, "schema_version": 1})
            );
            loop {
                for run in runtime.list().await? {
                    if run.status.is_terminal()
                        || (run.status == Status::RecoveryNeeded
                            && !run.cancel_requested
                            && Utc::now() < run.deadline_at)
                    {
                        continue;
                    }
                    let waiting = run.status == Status::WaitingForInput
                        && run
                            .interaction
                            .as_ref()
                            .is_some_and(|q| q.approved.is_none());
                    if !waiting || run.cancel_requested || Utc::now() >= run.deadline_at {
                        runtime.resume(&run.run_id).await?;
                    }
                }
                tokio::select! {
                    result = tokio::signal::ctrl_c() => { result?; return Ok(()); }
                    _ = tokio::time::sleep(std::time::Duration::from_millis(200)) => (),
                }
            }
        }
        _ => return Err(usage()),
    };
    println!("{}", serde_json::to_string(&value)?);
    Ok(())
}

fn usage() -> Error {
    Error::Config("usage: factory-runtime <absolute-data-dir> <absolute-workspace> start <StartRequest-json> | status/resume/cancel <run-id> | answer <run-id> <interaction-id> true/false | list | serve".into())
}
