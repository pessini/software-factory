"""Run the engine and worker ACP agent together, replacing only BB and the provider."""

import asyncio
import re

import pytest
from acp.schema import TextContentBlock

from software_factory.bb import BBContext
from software_factory.bb.client import BBExecution, BBThreadState
from software_factory.bb.worker_acp import WorkerAgent
from software_factory.engine import FactoryEngine
from software_factory.worker_records import WorkerMessage
from tests.conftest import FakeBB, init_repository, render_review
from tests.integration.test_factory_engine import build_output, context, plan_output, review_output


@pytest.mark.parametrize("action", ["answer", "cancel", "near_timeout"])
async def test_worker_question_uses_the_same_turn_after_controller_restart(
    tmp_path, factory_yaml, monkeypatch, action
):
    init_repository(
        tmp_path,
        factory_yaml.replace("provider: fake", "provider: codex").replace(
            "limits:\n",
            "limits:\n  step_timeout_seconds: 1\n" if action == "near_timeout" else "limits:\n",
        ),
        "raise SystemExit(0)\n",
    )
    ctx = context(tmp_path)
    responses = []
    providers = []

    class Provider:
        def __init__(self, container_id, operation_id, *args, **kwargs):
            self.step = operation_id.split(":")[1]
            self.notifications = asyncio.Queue()
            self.closed = False
            self.finished = asyncio.Event()
            providers.append(self)

        async def start(self, *, before_create):
            before_create()

        async def close(self):
            self.closed = True
            self.finished.set()

        async def complete(self):
            output = {"plan": plan_output(), "build": build_output(), "review": review_output()}[
                self.step
            ]
            output = render_review(output, self.prompt)
            await self.notifications.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": self.step,
                        "turn": {
                            "id": self.step + "-turn",
                            "status": "completed",
                            "items": [{"type": "agentMessage", "text": output}],
                        },
                    },
                }
            )

        async def request(self, method, params):
            if method in {"thread/start", "thread/resume"}:
                return {"thread": {"id": self.step}, "model": "model-a", "reasoningEffort": "high"}
            assert method == "turn/start"
            self.prompt = params["input"][0]["text"]
            if self.step == "build":
                if action == "near_timeout":
                    await asyncio.sleep(0.8)
                await self.notifications.put(
                    {
                        "id": 17,
                        "method": "item/tool/requestUserInput",
                        "params": {
                            "threadId": self.step,
                            "turnId": self.step + "-turn",
                            "itemId": "format-question",
                            "questions": [
                                {
                                    "id": "format",
                                    "question": "Which format?",
                                    "options": [{"label": "Text", "description": "Plain text"}],
                                }
                            ],
                        },
                    }
                )
            else:
                await self.complete()
            return {"turn": {"id": self.step + "-turn"}}

        async def respond(self, request_id, result):
            responses.append((request_id, result))
            (tmp_path / "answer.txt").write_text("good")
            await self.complete()

        async def reject_request(self, request_id):
            raise AssertionError(f"unexpected request rejection: {request_id}")

    class WorkerBB(FakeBB):
        def __init__(self):
            super().__init__(tmp_path, {})
            self.workers = {}
            self.operations = {}
            self.tasks = []
            self.child_tasks = {}

        async def model_catalog(self):
            return [
                item.model_copy(update={"provider": "codex"})
                for item in await super().model_catalog()
            ]

        async def start_worker(self, role, prompt, agent, spawn_key):
            self.spawn_counts[role] += 1
            thread_id = "child-" + role
            operation_id = re.search(r"Factory operation: (\S+)", prompt)[1]
            self.remote_threads[role, spawn_key] = thread_id
            self.thread_roles[thread_id] = role
            worker = WorkerAgent(
                BBContext(
                    thread_id, ctx.project_id, ctx.environment_id, ctx.thread_storage / thread_id
                ),
                Provider,
            )
            await worker.new_session(str(tmp_path))
            self.workers[thread_id] = worker
            self.operations[thread_id] = operation_id
            message = WorkerMessage(
                parent_thread_id=ctx.parent_thread_id, operation_id=operation_id
            )
            self.tasks.append(
                asyncio.create_task(
                    worker.prompt(
                        thread_id, [TextContentBlock(type="text", text=message.model_dump_json())]
                    )
                )
            )
            self.child_tasks[thread_id] = self.tasks[-1]
            return thread_id

        async def reconcile_operation(self, thread_id, operation_id):
            while True:
                receipt = self.workers[thread_id].records.read(operation_id)
                if receipt and receipt.execution_id:
                    return await self.execution(thread_id, receipt.execution_id)
                await asyncio.sleep(0.01)

        async def execution(self, thread_id, execution_id):
            receipt = self.workers[thread_id].records.read(self.operations[thread_id])
            assert receipt.execution_id == execution_id
            return BBExecution(
                execution_id,
                receipt.status,
                receipt.output,
                receipt.reported,
                receipt.interaction_id,
            )

        async def inspect_worker(self, thread_id):
            receipt = self.workers[thread_id].records.read(self.operations[thread_id])
            return BBThreadState(
                receipt.status if receipt else "starting", bool(receipt and receipt.stopped)
            )

        async def wait(self, thread_id):
            step = self.operations[thread_id].split(":")[1]
            await next(provider for provider in providers if provider.step == step).finished.wait()
            while not (await self.inspect_worker(thread_id)).stopped:
                await asyncio.wait({self.child_tasks[thread_id]}, timeout=0.01)
                if self.child_tasks[thread_id].done():
                    await self.child_tasks[thread_id]

        async def stop(self, thread_id):
            await self.workers[thread_id].cancel(thread_id)
            await self.wait(thread_id)

    bb = WorkerBB()

    async def read_bb(path):
        child = path.removeprefix("threads/")
        role = bb.thread_roles[child]
        run_id = bb.operations[child].split(":")[0]
        return {
            "parentThreadId": ctx.parent_thread_id,
            "projectId": ctx.project_id,
            "environmentId": ctx.environment_id,
            "title": f"{role.title()} · Software Factory · {run_id}:{role}",
        }

    async def thread_storage(parent):
        assert parent == ctx.parent_thread_id
        return ctx.thread_storage

    monkeypatch.setattr("software_factory.bb.worker_acp.read_bb", read_bb)
    monkeypatch.setattr("software_factory.bb.worker_acp.thread_storage", thread_storage)
    engine = FactoryEngine(workspace=tmp_path, context=ctx, execution=bb)
    try:
        async with asyncio.timeout(15):
            waiting = await engine.run("Write the answer", "fct_worker_question")
            assert waiting["status"] == "waiting_for_input", waiting.get("error")
            question = waiting["interactions"][0]
            assert question["source"] == "child"
            assert not (tmp_path / "answer.txt").exists()
            restarted = FactoryEngine(workspace=tmp_path, context=ctx, execution=bb)
            assert (await restarted.inspect(waiting["run_id"]))["interactions"] == waiting[
                "interactions"
            ]
            if action == "answer":
                result = await restarted.answer(
                    waiting["run_id"], question["interaction_id"], {"format": "Text"}
                )
                assert result["status"] == "ready_for_review", result.get("error")
                await restarted.answer(
                    waiting["run_id"], question["interaction_id"], {"format": "Text"}
                )
                assert responses == [(17, {"answers": {"format": {"answers": ["Text"]}}})]
                assert bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}
            else:
                if action == "near_timeout":
                    await asyncio.sleep(0.35)
                    assert not next(
                        provider for provider in providers if provider.step == "build"
                    ).closed
                result = await restarted.cancel_external(waiting["run_id"])
                assert result["status"] == "cancelled"
                assert not responses and not (tmp_path / "answer.txt").exists()
            await asyncio.gather(*bb.tasks)
            assert all(provider.closed for provider in providers)
    finally:
        for task in bb.tasks:
            task.cancel()
        await asyncio.gather(*bb.tasks, return_exceptions=True)
