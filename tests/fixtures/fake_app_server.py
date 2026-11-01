"""Credential-free native-protocol fixture. This is simulated provider evidence."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path


def send(value):
    print(json.dumps(value), flush=True)


def data(prompt):
    match = re.search(r"<factory_data>\n(.*?)\n</factory_data>", prompt, re.S)
    return json.loads(match[1]) if match else {}


def changed(cwd):
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "diff", "--name-only", "-z", "HEAD"],
        cwd=cwd,
        capture_output=True,
        check=True,
    ).stdout
    result += subprocess.run(
        ["git", "-c", "safe.directory=*", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=cwd,
        capture_output=True,
        check=True,
    ).stdout
    return sorted(
        {p.decode() for p in result.split(b"\0") if p and not p.startswith(b".factory-rust.")}
    )


cwd = Path.cwd()
thread = ""
model = "factory-test-model"
effort = "low"
for line in sys.stdin:
    message = json.loads(line)
    method, params = message.get("method"), message.get("params", {})
    request_id = message.get("id")
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "factory-simulated/0.154.0 (Linux)"}})
    elif method == "model/list":
        send(
            {
                "id": request_id,
                "result": {
                    "data": [
                        {
                            "id": model,
                            "model": model,
                            "displayName": "Factory simulation",
                            "isDefault": True,
                            "defaultReasoningEffort": effort,
                            "supportedReasoningEfforts": [{"reasoningEffort": effort}],
                        }
                    ],
                    "nextCursor": None,
                },
            }
        )
    elif method in {"thread/start", "thread/resume"}:
        cwd = Path(params["cwd"])
        model = params["model"]
        effort = params["config"]["model_reasoning_effort"]
        home_id = Path(os.environ["CODEX_HOME"]) / "fixture-thread"
        if not home_id.exists():
            home_id.write_text("test-" + uuid.uuid4().hex)
        thread = params.get("threadId") or home_id.read_text()
        send(
            {
                "id": request_id,
                "result": {
                    "thread": {"id": thread},
                    "model": model,
                    "reasoningEffort": effort,
                    "approvalPolicy": params["approvalPolicy"],
                },
            }
        )
    elif method == "turn/start":
        operation = params["clientUserMessageId"]
        turn = "turn-" + hashlib.sha256(operation.encode()).hexdigest()[:20]
        send({"id": request_id, "result": {"turn": {"id": turn}}})
        send({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": turn}}})
        prompt = params["input"][0]["text"]
        context = data(prompt)
        original = context.get("original_request", "")
        if "read-only Grill with Docs" in prompt:
            output = {
                "version": 1,
                "status": "questions",
                "summary": "Choose a recipient",
                "questions": [
                    {
                        "id": "recipient",
                        "question": "Who should be greeted?",
                        "options": ["world"],
                        "recommendation": "world",
                        "context": "A public greeting",
                    }
                ],
            }
            if context.get("rounds"):
                output = {
                    "version": 1,
                    "status": "ready",
                    "summary": "Greeting planned",
                    "decisions": [
                        {
                            "id": "recipient",
                            "title": "Recipient",
                            "decision": "world",
                            "rationale": "The user chose world",
                        }
                    ],
                    "plan": {
                        "goal": "Greet world",
                        "scope": ["greeting.txt"],
                        "requirements": ["Hello, world!"],
                        "completion_checks": [
                            {"id": "greeting", "description": "The greeting is Hello, world!"}
                        ],
                    },
                }
        elif "read-only Planner" in prompt:
            if "[tamper]" in original:
                try:
                    (cwd / "greeting.txt").write_text("forbidden")
                except OSError:
                    pass
                else:
                    raise AssertionError("read-only specialist can write workspace")
            output = {
                "version": 2,
                "status": "ready",
                "summary": "Update greeting",
                "acceptance_criteria": [
                    {"id": "greeting", "description": "The greeting is Hello, world!"}
                ],
                "implementation_steps": ["Update greeting.txt"],
                "test_strategy": "Check the exact greeting",
            }
            if "[questions]" in original and not context.get("saved_answers"):
                output.update(
                    status="needs_input",
                    questions=[
                        {
                            "id": "recipient",
                            "question": "Who should be greeted?",
                            "options": ["world"],
                        }
                    ],
                )
        elif "Builder" in prompt:
            if "[native-input]" in original:
                send(
                    {
                        "id": "native-question",
                        "method": "item/tool/requestUserInput",
                        "params": {
                            "threadId": thread,
                            "turnId": turn,
                            "itemId": "recipient-item",
                            "questions": [
                                {
                                    "id": "recipient",
                                    "question": "Who should be greeted?",
                                    "options": [{"label": "world"}],
                                }
                            ],
                        },
                    }
                )
                answer = json.loads(sys.stdin.readline())
                assert answer["id"] == "native-question" and answer["result"]["answers"][
                    "recipient"
                ]["answers"] == ["world"]
            # Long enough to kill/recreate the controller while the exact coding
            # turn is in flight. The broker must survive and keep this one turn.
            time.sleep(3)
            if "[tamper]" in original:
                for protected in [
                    cwd / ".git/config",
                    Path(context["evidence_directory"]) / "config.json",
                ]:
                    try:
                        protected.write_text("forbidden")
                    except OSError:
                        pass
                    else:
                        raise AssertionError("specialist can write protected state")
                assert not Path("/var/run/docker.sock").exists()
            target = cwd / "greeting.txt"
            target.write_text(
                "BROKEN\n"
                if "[repair]" in original and "You are the Builder." in prompt
                else "Hello, world!\n"
            )
            output = {
                "status": "completed",
                "summary": "Updated greeting",
                "changed_files": changed(cwd),
            }
        else:
            bundle = context["bundle"]
            criteria = [
                {
                    "criterion_id": c["id"],
                    "status": "satisfied",
                    "evidence": "Current source and check evidence verify this criterion",
                    "evidence_refs": [bundle["checks"]],
                }
                for c in bundle["context"]["plan"]["acceptance_criteria"]
            ]
            focused = next(
                (
                    r
                    for r in ("Request", "Code", "Security", "Documentation")
                    if f"{r} Reviewer" in prompt
                ),
                None,
            )
            if focused:
                output = {
                    "version": 1,
                    "reviewer": focused.lower(),
                    "content_version": bundle["content_version"],
                    "summary": "Focused check passed",
                    "findings": [],
                    "criteria": criteria if focused == "Request" else [],
                }
            else:
                output = {
                    "version": 2,
                    "content_version": bundle["content_version"],
                    "approved": True,
                    "summary": "Verified",
                    "findings": [],
                    "criteria": criteria,
                    "resolutions": [],
                }
                if "read-only Judge" in prompt:
                    output["assessments"] = []
        text = json.dumps(output)
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread,
                    "turnId": turn,
                    "item": {
                        "id": "final",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": text,
                    },
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread,
                    "turn": {
                        "id": turn,
                        "status": "completed",
                        "items": [{"type": "agentMessage", "phase": "final_answer", "text": text}],
                    },
                },
            }
        )
