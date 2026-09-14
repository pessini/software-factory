"""Saved, authorized PR delivery. Verification remains a separate run outcome."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from software_factory.artifacts import ArtifactIntegrityError
from software_factory.config import DeliveryPolicy
from software_factory.delivery_commands import DeliveryCommandError, DeliveryCommands
from software_factory.delivery_git import VerifiedCommit
from software_factory.github_delivery import GitHubProtocol
from software_factory.runtime import FactoryCancelled, FactoryRuntime
from software_factory.state import FactoryState
from software_factory.worktree_policy import WorktreePolicyError


class DeliveryRecoveryNeeded(RuntimeError):
    pass


class Delivery:
    def __init__(self, runtime: FactoryRuntime, github: GitHubProtocol) -> None:
        self.runtime, self.github = runtime, github
        self.commands = DeliveryCommands(runtime.workspace, runtime.store, runtime.run_id)

    def policy(self, authorization: DeliveryPolicy | None = None) -> DeliveryPolicy:
        artifacts = self.runtime.artifacts
        path = "delivery/policy.json"
        if authorization is not None:
            authorization = self._linked_issue(authorization)
        if artifacts.path(path).exists():
            policy = DeliveryPolicy.model_validate_json(artifacts.read_text(path))
            if authorization is not None and authorization != policy:
                if not policy.review_fixes and authorization == policy.model_copy(
                    update={"review_fixes": True}
                ):
                    artifacts.write_json(path, authorization)
                    return authorization
                raise ValueError(
                    "delivery already has a saved policy; cannot change its destination"
                )
            return policy
        policy = authorization or self._linked_issue(self.runtime.config.delivery)
        if policy.mode == "pull_request":
            artifacts.write_json(path, policy)
        return policy

    def _linked_issue(self, policy: DeliveryPolicy) -> DeliveryPolicy:
        if policy.mode == "pull_request" and policy.issue is None:
            prefix = f"https://github.com/{policy.repository}/issues/"
            links = self.runtime.evidence.read("request.json")["linked_issues"]
            numbers = {int(link.removeprefix(prefix)) for link in links if link.startswith(prefix)}
            if len(numbers) == 1:
                return policy.model_copy(update={"issue": numbers.pop()})
        return policy

    async def operation(
        self,
        key: str,
        request: dict[str, Any],
        reconcile: Callable[[], Awaitable[dict[str, Any] | None]],
        execute: Callable[[], Awaitable[None]],
        *,
        repeat_if_absent: bool = False,
    ) -> dict[str, Any]:
        runtime = self.runtime
        op = await runtime.store.prepare_operation(
            runtime.run_id,
            f"delivery:{key}",
            1,
            "remote",
            json.dumps(request, sort_keys=True),
            repeat_rule="reconcile",
        )
        # Even completed operations must still agree with the remote state.
        found = await reconcile()
        if found is None:
            if op["state"] != "prepared" and not repeat_if_absent:
                raise DeliveryRecoveryNeeded(
                    f"uncertain {key}: no confirmed result; refusing a duplicate mutation"
                )
            await runtime.check_cancelled()
            await runtime.store.update_operation(
                op["operation_id"], state="dispatching", dispatch_count=op["dispatch_count"] + 1
            )
            await execute()
            found = await reconcile()
            if found is None:
                raise DeliveryRecoveryNeeded(f"{key} returned without a confirmed result")
        await runtime.store.update_operation(
            op["operation_id"], state="completed", result_json=json.dumps(found)
        )
        return found

    async def deliver(
        self, state: FactoryState, authorization: DeliveryPolicy | None = None
    ) -> FactoryState:
        if state["status"] != "ready_for_review":
            raise ValueError("delivery requires a verified ready_for_review run")
        policy = self.policy(authorization)
        if policy.mode == "manual":
            return await self.runtime.report({**state, "delivery": {"status": "manual"}})
        delivery = {
            **state.get("delivery", {}),
            "status": "preparing",
            "repository": policy.repository,
        }
        try:
            delivery.update(await self._deliver(state, policy))
        except FactoryCancelled as exc:
            delivery.update(status="cancelled", error=str(exc))
        except (ArtifactIntegrityError, WorktreePolicyError) as exc:
            state = {
                **state,
                "review_approved": False,
                "test_passed": False,
                "evidence_status": "stale",
            }
            delivery.update(status="blocked", error=str(exc))
        except ValueError as exc:
            delivery.update(status="blocked", error=str(exc))
        except (DeliveryRecoveryNeeded, DeliveryCommandError, TimeoutError, OSError) as exc:
            delivery.update(status="recovery_needed", error=str(exc))
        record = await self.runtime.store.get_run(self.runtime.run_id)
        progress = json.loads(record["result_json"] or "{}").get("delivery", {}) if record else {}
        delivery = {**progress, **delivery}
        return await self.runtime.report({**state, "delivery": delivery})

    async def _deliver(self, state: FactoryState, policy: DeliveryPolicy) -> dict[str, Any]:
        runtime = self.runtime
        await runtime.check_cancelled()
        if state.get("evidence_status") == "stale":
            raise ArtifactIntegrityError(
                "approval was invalidated; verify the current changes in a new run"
            )
        assert policy.repository and policy.title
        repository = policy.repository
        if state.get("delivery", {}).get("verified_run_id"):
            # Review fixes have their own evidence. Never reinstall the original commit.
            saved_delivery = state["delivery"]
            latest = runtime.evidence.read("delivery/latest-commit.json")
            await VerifiedCommit(runtime, self.commands).install(latest)
            pr = await self.github.pull(repository, saved_delivery["pr_number"])
            return {**saved_delivery, **await self.track(policy, pr, saved_delivery["commit"])}
        prepared = runtime.artifacts.path("delivery/commit.json").exists()
        if not prepared:
            handoff = await runtime.verify_handoff(state)
            if not handoff["bundle"]["changed_files"]:
                return {"status": "no_change", "error": None}
            base = await self.github.branch_head(repository, policy.base_branch)
            if base != handoff["project"]["starting_commit"]:
                raise ValueError(
                    "delivery must start at the selected remote base commit; verify the updated base in a new run"
                )
        slug = (
            re.sub(r"[^a-z0-9]+", "-", policy.title.casefold()).strip("-")[:48] or "implementation"
        )
        branch = f"{policy.work_type}/{policy.issue or runtime.run_id}-{slug}-{runtime.run_id}"
        commit = VerifiedCommit(runtime, self.commands)
        saved = await commit.prepare(dict(state), policy.title, branch)
        await commit.install(saved)
        sha = saved["commit"]
        branch = saved["branch"]
        await runtime.report(
            {
                **state,
                "delivery": {
                    "status": "publishing",
                    "repository": repository,
                    "branch": branch,
                    "commit": sha,
                    "content_version": state["content_version"],
                },
            }
        )

        async def pushed() -> dict[str, Any] | None:
            head = await self.github.branch_head(repository, branch)
            if head == sha:
                return {"commit": sha, "branch": branch}
            if head is not None:
                raise ValueError("remote delivery branch changed; refusing to overwrite it")
            return None

        async def push_verified() -> None:
            await commit.require_installed(saved)
            await self.github.push(repository, branch, sha, None)

        await self.operation(
            "push",
            {"repository": repository, "branch": branch, "commit": sha},
            pushed,
            push_verified,
            repeat_if_absent=True,
        )
        marker = f"<!-- factory-delivery:{runtime.run_id} -->"
        body = self.pull_body(state, policy, sha, marker)

        async def existing_pull() -> dict[str, Any] | None:
            pr = await self.github.find_pull(repository, branch)
            if pr is not None:
                self.validate_pull(pr, branch, policy.base_branch, sha)
                if marker not in pr["body"]:
                    raise ValueError("existing PR does not belong to this delivery")
            return pr

        async def create_verified_pull() -> None:
            await commit.require_installed(saved)
            await self.github.create_pull(
                repository, branch, policy.base_branch, policy.title or "", body
            )

        pr = await self.operation(
            "pull",
            {
                "repository": repository,
                "branch": branch,
                "base": policy.base_branch,
                "title": policy.title,
                "body": body,
            },
            existing_pull,
            create_verified_pull,
        )
        await runtime.report(
            {
                **state,
                "delivery": {
                    "status": "ci_pending",
                    "repository": repository,
                    "branch": branch,
                    "commit": sha,
                    "pr_number": pr["number"],
                    "pr_url": pr["url"],
                    "content_version": state["content_version"],
                },
            }
        )
        return await self.track(policy, pr, sha)

    @staticmethod
    def validate_pull(pr: dict[str, Any], branch: str, base: str, commit: str) -> None:
        if (pr["branch"], pr["base"], pr["head"], pr["draft"], pr["state"]) != (
            branch,
            base,
            commit,
            False,
            "open",
        ):
            raise ValueError("PR state or head differs from the verified delivery")

    async def track(self, policy: DeliveryPolicy, pr: dict[str, Any], sha: str) -> dict[str, Any]:
        assert policy.repository
        checks = await self.github.checks(policy.repository, sha)
        if checks["commit"] != sha:
            raise ValueError("CI result belongs to a different commit")
        latest = await self.github.pull(policy.repository, pr["number"])
        self.validate_pull(latest, pr["branch"], policy.base_branch, sha)
        status = {
            "passed": "delivered",
            "failed": "ci_failed",
            "pending": "ci_pending",
            "unavailable": "ci_unavailable",
        }.get(checks["status"])
        if status is None:
            raise ValueError("unknown CI result")
        return {
            "status": status,
            "repository": policy.repository,
            "branch": pr["branch"],
            "commit": sha,
            "pr_number": pr["number"],
            "pr_url": pr["url"],
            "ci": checks,
            "error": None,
        }

    @staticmethod
    def pull_body(state: FactoryState, policy: DeliveryPolicy, sha: str, marker: str) -> str:
        handoff = state["handoff"]
        checks = "\n".join(
            f"- {item['name']}: exit {item['exit_code']}" for item in handoff["checks"]["commands"]
        )
        criteria = "\n".join(
            f"- {item['criterion_id']}: {item['status']} — {item['evidence']}"
            for item in handoff["criteria"]
        )
        close = (
            f"\n\nCloses issue #{policy.issue}.\n\nCloses #{policy.issue}" if policy.issue else ""
        )
        manual = "\n".join(f"- {item}" for item in handoff["manual_work"]) or "None reported."
        return f"{policy.title}\n\n{handoff['change']}\n\n## Verification\n\nCommit: `{sha}`\nContent version: `{handoff['content_version']}`\n\n{checks}\n\n{criteria}\n\n## Remaining manual work\n\n{manual}{close}\n\n{marker}"
