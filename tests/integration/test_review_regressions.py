from pathlib import Path

from software_factory.bb.client import RecoveryNeeded
from software_factory.config import ModelChoice, Selection
from software_factory.engine import FactoryEngine
from tests.conftest import FakeBB, init_repository
from tests.integration.test_factory_engine import build_output, context, plan_output, write_answer


async def test_cancel_finds_repair_child_after_lost_creation_reply(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(
        tmp_path,
        factory_yaml,
        "from pathlib import Path\nassert not Path('answer.txt').exists() or Path('answer.txt').read_text() == 'good'\n",
    )

    class LostRepairReply(FakeBB):
        async def spawn_child(self, role, prompt, agent, spawn_key):
            if role == "builder" and self.spawn_counts[role] == 1:
                self.spawn_error_after_create = RecoveryNeeded("creation reply lost")
            return await super().spawn_child(role, prompt, agent, spawn_key)

    bb = LostRepairReply(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [
                {"output": build_output(), "action": write_answer("bad")},
                {"output": build_output(), "action": write_answer("good")},
            ],
        },
    )
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    result = await engine.run(
        "Write the answer",
        selection=Selection(steps={"repair": ModelChoice(model="model-b", reasoning_level="low")}),
    )
    assert result["status"] == "recovery_needed"
    cancelled = await engine.cancel_external(result["run_id"])
    assert cancelled["status"] == "cancelled"
    assert "thread-builder-2" in bb.stopped


async def test_builder_cannot_change_ignored_secret_alongside_application(
    tmp_path: Path, factory_yaml: str
) -> None:
    import pytest

    from software_factory.worktree_policy import WorktreePolicy, WorktreePolicyError

    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    (tmp_path / ".git/info/exclude").write_text(".env\n")
    (tmp_path / ".env").write_text("dummy secret")
    with pytest.raises(WorktreePolicyError, match=".env"):
        async with WorktreePolicy(tmp_path, []).protect_builder_changes():
            (tmp_path / "answer.txt").write_text("good")
            (tmp_path / ".env").write_text("changed dummy secret")
    assert (tmp_path / ".env").read_text() == "changed dummy secret"
    assert (tmp_path / "answer.txt").read_text() == "good"


async def test_preflight_failure_retains_ownership_if_stop_is_unconfirmed(
    tmp_path: Path, factory_yaml: str
) -> None:
    import pytest

    from software_factory.bb.client import BBError, BBThreadState
    from software_factory.ownership import OwnershipError

    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    run_id = "fct_preflight_cleanup"

    class UncertainPreflight(FakeBB):
        async def model_catalog(self):
            await engine.store.save_child(run_id, "planner", "uncertain-child")
            raise BBError("connection ended")

        async def inspect_thread(self, thread_id):
            return BBThreadState("unknown", False)

    bb = UncertainPreflight(tmp_path, {})
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    result = await engine.run("Write the answer", run_id)
    assert result["status"] == "recovery_needed"
    other = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=FakeBB(tmp_path, {}))
    with pytest.raises(OwnershipError, match="reserved for recovery"):
        await other.run("Other work", "fct_takeover")
