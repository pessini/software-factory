from __future__ import annotations

import asyncio

from software_factory.bb import BBClientProtocol
from software_factory.bb.client import RecoveryNeeded
from software_factory.persistence import RunStore
from software_factory.processes import CleanupUncertain, group_alive, stop_group


async def stop_owned_work(store: RunStore, run_id: str, bb: BBClientProtocol) -> None:
    children = set((await store.list_children(run_id)).values())
    operations = await store.operations(run_id)
    uncertain = False
    errors: list[str] = []
    for operation in operations:
        if operation["kind"] == "check" or operation["state"] not in {
            "dispatching",
            "dispatched",
        }:
            continue
        try:
            thread_id = operation["thread_id"] or await asyncio.wait_for(
                bb.find_child(str(operation["role"]), f"{run_id}:{operation['role']}"),
                5,
            )
        except Exception as exc:
            errors.append(str(exc))
            continue
        if thread_id is None:
            uncertain = True
        else:
            children.add(thread_id)
            await store.update_operation(operation["operation_id"], thread_id=thread_id)
    for thread_id in children:
        try:
            await asyncio.wait_for(bb.stop(thread_id), 5)
            inspection = await asyncio.wait_for(bb.inspect_thread(thread_id), 5)
            if not inspection.stopped:
                raise CleanupUncertain(f"cannot confirm child exit: {thread_id}")
            for operation in await store.operations(run_id):
                if operation["thread_id"] == thread_id and operation["state"] != "completed":
                    await store.update_operation(operation["operation_id"], state="stopped")
        except Exception as exc:
            errors.append(str(exc))
    if not errors and not uncertain:
        await store.clear_active_child(run_id)
    for process in await store.processes(run_id):
        try:
            await stop_group(int(process["process_group"]), str(process["identity"]))
            await store.clear_process(str(process["process_id"]))
        except Exception as exc:
            errors.append(str(exc))
    record = await store.get_run(run_id)
    if record and record["active_process_group"]:
        if await group_alive(int(record["active_process_group"])):
            errors.append("old quality process group still exists")
        else:
            await store.set_process_group(run_id, None)
    if errors or uncertain:
        raise RecoveryNeeded(
            "; ".join(errors) or "child dispatch is uncertain; stop cannot be confirmed"
        )
    for operation in operations:
        if operation["kind"] == "check" and operation["state"] != "completed":
            await store.update_operation(operation["operation_id"], state="stopped")
