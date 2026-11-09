use factory_runtime::{Error, Result, client::Client};
use serde_json::{Value, json};
use std::path::PathBuf;

#[tokio::main]
async fn main() {
    if let Err(error) = command(std::env::args().skip(1).collect()).await {
        eprintln!("{}", json!({"error":error.to_string()}));
        std::process::exit(1);
    }
}
async fn command(mut args: Vec<String>) -> Result<()> {
    if args.first().is_some_and(|s| s.starts_with('/')) {
        return factory_runtime::operator::command(args).await;
    }
    let workspace = take_option(&mut args, &["--workspace", "-w"])?
        .map(PathBuf::from)
        .unwrap_or(std::env::current_dir()?);
    let session = take_option(&mut args, &["--session"])?.unwrap_or_else(|| "default".into());
    let storage = take_option(&mut args, &["--storage"])?;
    let request_id = take_option(&mut args, &["--request-id"])?;
    let policy = take_option(&mut args, &["--policy"])?;
    let mut threads = vec![];
    while let Some(thread) = take_option(&mut args, &["--thread"])? {
        threads.push(thread);
    }
    let command = args.first().map(String::as_str).unwrap_or("--help");
    if matches!(command, "--help" | "-h" | "help") {
        println!(
            "Software Factory (Rust/ADK)\nUsage: software-factory COMMAND [ARGS] [-w WORKSPACE] [--session ID]\nCommands: web, serve, run, start, inspect, status, list, answer, resume, retry, cancel, models, settings, plan, plan-status, plan-answer, plan-revise, plan-approve, plan-resume, plan-cancel, plan-report, acp, mcp, settings-panel, doctor, configure-docker, configure-native, doctor-native, init, repo-check, graph, deliver, review-threads, fix-reviews, review-quality, review-feedback, convert-config\nService: software-factory web --storage /absolute/private/data -w /absolute/workspace\nClient: FACTORY_HTTP_TOKEN is required; FACTORY_CONTROL_URL defaults to http://127.0.0.1:4317"
        );
        return Ok(());
    }
    if matches!(command, "--version" | "-V") {
        println!("software-factory {} (Rust/ADK)", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if matches!(
        command,
        "web"
            | "serve"
            | "configure-docker"
            | "configure-native"
            | "doctor-native"
            | "import-planning"
    ) {
        let port = take_option(&mut args, &["--port"])?;
        let bind = port
            .map(|p| {
                p.parse::<u16>()
                    .ok()
                    .filter(|p| *p > 0)
                    .map(|p| format!("127.0.0.1:{p}"))
                    .ok_or_else(|| Error::Config("--port must be 1..65535".into()))
            })
            .transpose()?;
        let storage = storage.or_else(||std::env::var("FACTORY_DATA_DIR").ok()).ok_or_else(||Error::Config("service startup requires --storage /absolute/private/data or FACTORY_DATA_DIR, separate from the workspace".into()))?;
        let mut operator = vec![storage, workspace.canonicalize()?.display().to_string()];
        operator.extend(args);
        return factory_runtime::operator::command_at(operator, bind).await;
    }
    if storage.is_some() {
        return Err(Error::Config("--storage is a service option; clients use FACTORY_CONTROL_URL. LangGraph storage is unsupported; use a historical release for old records".into()));
    }
    if matches!(command, "init" | "repo-check" | "convert-config" | "graph") {
        return local(&workspace, command, &args[1..]);
    }
    let client = Client::environment(workspace)?;
    if matches!(command, "mcp" | "acp") {
        return factory_runtime::protocol::serve(client, &session, command == "acp").await;
    }
    if command == "settings-panel" {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let mut input = String::new();
        tokio::io::stdin()
            .take(65537)
            .read_to_string(&mut input)
            .await?;
        if input.len() > 65536 {
            return Err(Error::Config("settings request exceeds 64 KiB".into()));
        }
        let request: Value = serde_json::from_str(&input)?;
        let chats = factory_runtime::chat::Chats::open().await?;
        let id = if let Some(id) = request["session_id"].as_str() {
            chats.run(&client, id).await?;
            id.into()
        } else {
            chats
                .panel_session(&client, request["interface_reference"].as_str())
                .await?
        };
        let result = client.settings(&id, &request).await?;
        tokio::io::stdout()
            .write_all(format!("{result}\n").as_bytes())
            .await?;
        return Ok(());
    }
    if command == "doctor" {
        client.verify().await?;
        let config = factory_runtime::workflow::WorkflowConfig::from_yaml(
            &std::fs::read_to_string(client.workspace.join(".factory/factory.yaml"))?,
        )?;
        let models = client.call("models", json!({})).await?;
        println!(
            "{}",
            json!({"service":client.origin,"workspace":client.workspace,"configuration_valid":true,"workflow":config.workflow,"models":models,"runtime":"Rust/ADK"})
        );
        return Ok(());
    }
    let operation;
    let mut value = json!({"session_id":session});
    match (command, &args[1..]) {
        ("run" | "start", [request]) => {
            operation = "start";
            value["request"] = json!(request);
            value["request_id"] =
                json!(request_id.unwrap_or_else(factory_runtime::client::unique_id));
        }
        ("inspect" | "status" | "cancel" | "resume" | "retry", [id]) => {
            operation = match command {
                "inspect" => "status",
                "retry" => "resume",
                other => other,
            };
            value["run_id"] = json!(id);
        }
        ("plan", [request]) => {
            operation = "plan_start";
            value = json!({"request":request,"request_id":request_id.unwrap_or_else(factory_runtime::client::unique_id),"client_session_id":session});
        }
        ("plan-status", []) => {
            operation = "plan_list";
            value = json!({});
        }
        ("plan-status" | "plan-cancel" | "plan-resume" | "plan-report", [id]) => {
            operation = match command {
                "plan-status" => "plan_status",
                "plan-cancel" => "plan_cancel",
                "plan-resume" => "plan_resume",
                _ => "plan_report",
            };
            value = json!({"session_id":id});
        }
        ("plan-answer", [id, interaction, answers]) => {
            operation = "plan_answer";
            value = json!({"session_id":id,"interaction_id":interaction,"answers":serde_json::from_str::<Value>(answers)?});
        }
        ("plan-revise", [id, feedback]) => {
            operation = "plan_revise";
            value = json!({"session_id":id,"feedback":feedback});
        }
        ("plan-approve", [id, hash]) => {
            operation = "plan_approve";
            value = json!({"session_id":id,"plan_hash":hash});
        }
        ("models" | "list", []) => {
            operation = command;
        }
        ("settings", []) => {
            operation = "settings";
        }
        ("answer", [id, interaction, answers]) => {
            operation = "answer";
            value["run_id"] = json!(id);
            value["interaction_id"] = json!(interaction);
            value["answers"] = serde_json::from_str(answers)?;
        }
        ("deliver", [id]) => {
            operation = "deliver";
            value["run_id"] = json!(id);
            if let Some(path) = policy {
                value["policy"] = serde_json::from_slice(&std::fs::read(path)?)?;
            }
        }
        ("fix-reviews", [id]) => {
            operation = "fix_reviews";
            value["run_id"] = json!(id);
            value["threads"] = json!(threads);
        }
        ("review-threads" | "review-quality", [id]) => {
            operation = if command == "review-threads" {
                "review_threads"
            } else {
                "review_quality"
            };
            value["run_id"] = json!(id);
        }
        ("review-feedback", [id, attempt, reviewer, finding, verdict, reason]) => {
            operation = "review_feedback";
            value = json!({"run_id":id,"attempt":attempt.parse::<u64>().map_err(|_|Error::Config("attempt must be positive".into()))?,"reviewer":reviewer,"finding_id":finding,"verdict":verdict,"reason":reason});
        }
        (
            "export-planning" | "legacy-inspect" | "legacy-resume" | "legacy-cancel" | "worker-acp",
            _,
        ) => {
            return Err(Error::Incompatible(
                "this removed Python recovery operation requires a historical release".into(),
            ));
        }
        _ => {
            return Err(Error::Config(
                "invalid command or arguments; use software-factory --help".into(),
            ));
        }
    }
    let result: Value = client.call(operation, value).await?;
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}
fn take_option(args: &mut Vec<String>, names: &[&str]) -> Result<Option<String>> {
    let Some(index) = args.iter().position(|s| names.contains(&s.as_str())) else {
        return Ok(None);
    };
    args.remove(index);
    if index >= args.len() {
        return Err(Error::Config(format!("{} requires a value", names[0])));
    }
    Ok(Some(args.remove(index)))
}

fn local(workspace: &std::path::Path, command: &str, args: &[String]) -> Result<()> {
    use factory_runtime::workflow::WorkflowConfig;
    let workspace = workspace.canonicalize()?;
    let path = workspace.join(".factory/factory.yaml");
    match command {
        "init" => {
            use std::io::Write;
            let force = args == ["--force"];
            if !args.is_empty() && !force {
                return Err(Error::Config("init accepts --force only".into()));
            }
            if path.ancestors().any(|p| p.is_symlink()) {
                return Err(Error::Config(
                    "policy path cannot contain symbolic links".into(),
                ));
            }
            std::fs::create_dir_all(path.parent().unwrap())?;
            let mut options = std::fs::OpenOptions::new();
            options.write(true);
            if force {
                options.create(true).truncate(true);
            } else {
                options.create_new(true);
            }
            options
                .open(&path)?
                .write_all(include_bytes!("../assets/factory.yaml"))?;
            println!(
                "{}",
                json!({"created":path,"next":"Review quality.commands argv before starting work"})
            );
        }
        "graph" => {
            if !args.is_empty() && args != ["--format", "mermaid"] {
                return Err(Error::Config("graph supports --format mermaid".into()));
            }
            print!("{}", include_str!("../assets/workflow.mmd"));
        }
        _ => {
            if !args.is_empty() {
                return Err(Error::Config("unexpected arguments".into()));
            }
            let mut config = WorkflowConfig::from_yaml(&std::fs::read_to_string(path)?)?;
            if command == "convert-config" {
                config.version = 2;
                config.worker_connection = Some(serde_json::from_value(
                    json!({"runtime":"codex","model_connection":"native","codex_version":"0.154.0"}),
                )?);
                config.resolved_settings = None;
                config.validate()?;
                println!("{}", serde_json::to_string_pretty(&config)?);
            } else {
                println!("{}", json!({"valid":true,"configuration":config}));
            }
        }
    }
    Ok(())
}
