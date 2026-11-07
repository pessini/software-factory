//! Simulated App Server protocol, with real native subprocess/cgroup supervision.
//! This fixture deliberately makes no model or enforcement claims.
use serde_json::{Value, json};
use std::io::{self, BufRead, Write};
fn send(value: Value) { println!("{value}"); io::stdout().flush().unwrap(); }
fn completed(text: &str, turn: &str) {
    send(json!({"method":"turn/completed","params":{"threadId":"thread-fixture","turn":{"id":turn,"status":"completed","items":[{"type":"agentMessage","phase":"final_answer","text":text}]}}}));
}
fn main() {
    if std::env::args().any(|arg|arg=="--version") { println!("codex-cli 0.154.0"); return; }
    let mut pending = false;
    let mut turn = String::new();
    for line in io::stdin().lock().lines() {
        let value: Value=serde_json::from_str(&line.unwrap()).unwrap();
        let id=value["id"].clone();
        match value["method"].as_str() {
            Some("initialize") => send(json!({"id":id,"result":{"userAgent":"fixture/0.154.0 linux"}})),
            Some("command/exec") => send(json!({"id":id,"result":{"exitCode":0,"stdout":"","stderr":""}})),
            Some("model/list") => send(json!({"id":id,"result":{"data":[{"model":"fixture-model","displayName":"Fixture","supportedReasoningEfforts":[{"reasoningEffort":"low"}],"defaultReasoningEffort":"low","isDefault":true}],"nextCursor":null}})),
            Some("thread/start"|"thread/resume") => send(json!({"id":id,"result":{"model":value["params"]["model"],"reasoningEffort":value["params"]["config"]["model_reasoning_effort"],"approvalPolicy":value["params"]["approvalPolicy"],"activePermissionProfile":{"id":"factory"},"sandbox":{"type":"readOnly","networkAccess":false},"cwd":value["params"]["cwd"],"thread":{"id":"thread-fixture"}}})),
            Some("turn/start") => {
                let prompt=value["params"]["input"][0]["text"].as_str().unwrap();
                let count_path="/factory/provider/fixture-turn-count";
                let count=std::fs::read_to_string(count_path).ok().and_then(|n|n.parse::<u32>().ok()).unwrap_or(0)+1;
                std::fs::write(count_path,count.to_string()).unwrap();
                turn=format!("turn-{count}");
                if prompt=="question-before-start-response" {
                    send(json!({"id":9001,"method":"item/tool/requestUserInput","params":{"threadId":"thread-fixture","turnId":turn,"itemId":"recipient","questions":[{"id":"recipient","question":"Who?","options":[{"label":"world"}]}]}}));
                    pending=true;
                }
                send(json!({"id":id,"result":{"turn":{"id":turn}}}));
                if !pending {
                    let output=if prompt.contains("You are the read-only Planner") {
                        std::thread::sleep(std::time::Duration::from_secs(3));
                        json!({"version":2,"status":"ready","summary":"Keep source unchanged","acceptance_criteria":[{"id":"unchanged","description":"Source remains original"}],"implementation_steps":["Verify source"],"test_strategy":"Run the reviewed check"})
                    } else if prompt.contains("You are the Builder") {
                        json!({"status":"completed","summary":"Source already satisfies request","changed_files":[]})
                    } else {
                        let data=prompt.split("<factory_data>\n").nth(1).unwrap().split("\n</factory_data>").next().unwrap();
                        let context:Value=serde_json::from_str(data).unwrap();
                        let bundle=&context["bundle"];
                        json!({"version":2,"content_version":bundle["content_version"],"approved":true,"summary":"Verified unchanged source","findings":[],"criteria":[{"criterion_id":"unchanged","status":"satisfied","evidence":"Reviewed check verifies original source","evidence_refs":[bundle["checks"].clone()]}],"resolutions":[]})
                    };
                    completed(&output.to_string(),&turn);
                }
            }
            None if id==9001 => {
                assert!(value.get("error").is_none(),"Factory discarded the early question");
                assert_eq!(value["result"]["answers"]["recipient"]["answers"][0],"world");
                pending=false; completed("fixture completed",&turn);
            }
            Some("initialized") => {},
            other => panic!("unexpected fixture RPC {other:?}"),
        }
    }
}
