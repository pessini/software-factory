//! Newline-delimited JSON-RPC stdio; stdout contains protocol frames only.
use crate::{Error, Result, client::Client};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt};

struct Tool {
    name: &'static str,
    description: &'static str,
    fields: &'static [(&'static str, &'static str, bool)],
}
const TOOLS: &[Tool] = &[
    Tool {
        name: "start",
        description: "Save a work request; reuse request_id only for identical input.",
        fields: &[
            ("request", "string", true),
            ("request_id", "string", true),
            ("selection", "object", false),
        ],
    },
    Tool {
        name: "status",
        description: "Read saved status, questions, reports and missed events without starting work.",
        fields: &[
            ("run_id", "string", true),
            ("after_event", "integer", false),
        ],
    },
    Tool {
        name: "answer",
        description: "Save answers to the exact pending interaction.",
        fields: &[
            ("run_id", "string", true),
            ("interaction_id", "string", true),
            ("answers", "object", true),
        ],
    },
    Tool {
        name: "resume",
        description: "Request reconciliation under saved limits and authorization.",
        fields: &[("run_id", "string", true)],
    },
    Tool {
        name: "cancel",
        description: "Save a stop request; poll status until worker exit is confirmed.",
        fields: &[("run_id", "string", true)],
    },
    Tool {
        name: "models",
        description: "Discover supported worker models and effort choices.",
        fields: &[],
    },
    Tool {
        name: "settings",
        description: "Read saved settings or change selections before starting work.",
        fields: &[("run_id", "string", false), ("selection", "object", false)],
    },
    Tool {
        name: "plan_start",
        description: "Start a Grill planning interview.",
        fields: &[("request", "string", true), ("request_id", "string", true)],
    },
    Tool {
        name: "plan_list",
        description: "List planning sessions in this workspace.",
        fields: &[],
    },
    Tool {
        name: "plan_status",
        description: "Read the saved plan revision, questions, hash and handoff.",
        fields: &[
            ("session_id", "string", true),
            ("after_event", "integer", false),
        ],
    },
    Tool {
        name: "plan_answer",
        description: "Submit answers to an exact saved planning round.",
        fields: &[
            ("session_id", "string", true),
            ("interaction_id", "string", true),
            ("answers", "object", true),
        ],
    },
    Tool {
        name: "plan_revise",
        description: "Create a new revision without changing earlier snapshots.",
        fields: &[("session_id", "string", true), ("feedback", "string", true)],
    },
    Tool {
        name: "plan_approve",
        description: "Only after explicit user approval, implement this exact plan hash.",
        fields: &[
            ("session_id", "string", true),
            ("plan_hash", "string", true),
        ],
    },
    Tool {
        name: "plan_cancel",
        description: "Stop the current planning revision.",
        fields: &[("session_id", "string", true)],
    },
];
fn schema(tool: &Tool) -> Value {
    let mut properties = serde_json::Map::new();
    for (key, kind, required) in tool.fields {
        let mut field = if !required && matches!(*key, "selection" | "run_id") {
            json!({"type":[kind,"null"]})
        } else {
            json!({"type":kind})
        };
        if *key == "answers" {
            field["additionalProperties"] = json!({"type":"string"});
        }
        if *kind == "integer" {
            field["minimum"] = json!(0);
        }
        properties.insert((*key).into(), field);
    }
    json!({"name":format!("factory_{}",tool.name),"description":tool.description,"inputSchema":{"type":"object","properties":properties,"required":tool.fields.iter().filter(|f|f.2).map(|f|f.0).collect::<Vec<_>>(),"additionalProperties":false}})
}
async fn mcp(client: &Client, session: &str, method: &str, params: &Value) -> Result<Value> {
    match method {
        "initialize" => {
            let requested = params["protocolVersion"].as_str().unwrap_or("");
            let version =
                if ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"].contains(&requested) {
                    requested
                } else {
                    "2025-03-26"
                };
            Ok(
                json!({"protocolVersion":version,"capabilities":{"tools":{}},"serverInfo":{"name":"Software Factory","version":env!("CARGO_PKG_VERSION")},"instructions":"Use saved Rust Factory identities. Disconnecting does not stop work; only explicit cancel does."}),
            )
        }
        "ping" => Ok(json!({})),
        "tools/list" => Ok(json!({"tools":TOOLS.iter().map(schema).collect::<Vec<_>>()})),
        "tools/call" => {
            let name = params["name"].as_str().unwrap_or("");
            let tool = TOOLS
                .iter()
                .find(|t| name == format!("factory_{}", t.name))
                .ok_or_else(|| Error::Config("unknown tool".into()))?;
            let mut args = params.get("arguments").cloned().unwrap_or(json!({}));
            let object = args
                .as_object()
                .ok_or_else(|| Error::Config("arguments must be an object".into()))?;
            for key in object.keys() {
                if !tool.fields.iter().any(|f| f.0 == key) {
                    return Err(Error::Config(format!("unexpected argument: {key}")));
                }
            }
            for (key, kind, required) in tool.fields {
                match object.get(*key) {
                    None if *required => return Err(Error::Config(format!("{key} is required"))),
                    Some(v)
                        if v.is_null() && !required && matches!(*key, "selection" | "run_id") => {}
                    Some(v)
                        if !match *kind {
                            "string" => v.is_string(),
                            "object" => v.is_object(),
                            "integer" => v.as_u64().is_some(),
                            _ => false,
                        } =>
                    {
                        return Err(Error::Config(format!("invalid {key}")));
                    }
                    _ => (),
                }
            }
            if tool.name == "plan_start" {
                args["client_session_id"] = json!(session);
            }
            if !tool.name.starts_with("plan_") {
                args["session_id"] = json!(session);
            }
            match client.call(tool.name, args).await {
                Ok(mut result) => {
                    if let Some(id) = result["session_id"]
                        .as_str()
                        .map(String::from)
                        .filter(|_| tool.name.starts_with("plan_"))
                    {
                        let encoded = percent_encoding::utf8_percent_encode(
                            &id,
                            percent_encoding::NON_ALPHANUMERIC,
                        );
                        result["web_url"] = json!(format!("{}/planning/{encoded}", client.origin));
                    }
                    Ok(
                        json!({"content":[{"type":"text","text":serde_json::to_string_pretty(&result)?}],"isError":false}),
                    )
                }
                Err(error) => {
                    Ok(json!({"content":[{"type":"text","text":error.to_string()}],"isError":true}))
                }
            }
        }
        _ => Err(Error::Config("method not found".into())),
    }
}

pub async fn serve(client: Client, session: &str, acp: bool) -> Result<()> {
    let chats = if acp {
        Some(crate::chat::Chats::open().await?)
    } else {
        None
    };
    let mut input = tokio::io::BufReader::new(tokio::io::stdin());
    let mut output = tokio::io::stdout();
    loop {
        let mut line = String::new();
        let n = (&mut input).take(131073).read_line(&mut line).await?;
        if n == 0 {
            return Ok(());
        }
        if n > 131072 {
            return Err(Error::Config("protocol frame exceeds 128 KiB".into()));
        }
        let response = match serde_json::from_str::<Value>(&line) {
            Err(_) => {
                json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"invalid JSON"}})
            }
            Ok(request) => {
                let id = request.get("id").cloned();
                if request["jsonrpc"] != "2.0" || !request["method"].is_string() {
                    json!({"jsonrpc":"2.0","id":id,"error":{"code":-32600,"message":"invalid request"}})
                } else if let Some(id) = id {
                    let result = if let Some(chats) = &chats {
                        match chats
                            .call(
                                &client,
                                request["method"].as_str().unwrap(),
                                &request["params"],
                            )
                            .await
                        {
                            Ok((result, updates)) => {
                                for update in updates {
                                    output.write_all(format!("{update}\n").as_bytes()).await?;
                                }
                                Ok(result)
                            }
                            Err(error) => Err(error),
                        }
                    } else {
                        mcp(
                            &client,
                            session,
                            request["method"].as_str().unwrap(),
                            &request["params"],
                        )
                        .await
                    };
                    match result {
                        Ok(result) => json!({"jsonrpc":"2.0","id":id,"result":result}),
                        Err(error) => {
                            json!({"jsonrpc":"2.0","id":id,"error":{"code":if error.to_string().contains("method not found") {-32601} else {-32602},"message":error.to_string()}})
                        }
                    }
                } else {
                    continue;
                }
            }
        };
        output.write_all(format!("{response}\n").as_bytes()).await?;
        output.flush().await?;
    }
}
