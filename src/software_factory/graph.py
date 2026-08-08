from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from software_factory.runtime import FactoryRuntime
from software_factory.state import FactoryState
from software_factory.workflow_nodes import WorkflowNodes


def after_ok(state: FactoryState) -> Literal["next", "failed"]:
    return "failed" if state.get("error") else "next"


def after_tests(state: FactoryState) -> Literal["review", "repair", "failed"]:
    if state.get("test_passed"):
        return "review"
    if state.get("status") in {"cancelled", "blocked"}:
        return "failed"
    if state.get("test_repair_count", 0) < state.get("max_test_repairs", 0):
        return "repair"
    return "failed"


def after_review(state: FactoryState) -> Literal["finalize", "revise", "failed"]:
    if state.get("review_approved"):
        return "finalize"
    if state.get("status") in {"cancelled", "blocked"}:
        return "failed"
    if state.get("review_revision_count", 0) < state.get("max_review_revisions", 0):
        return "revise"
    return "failed"


def build_graph(runtime: FactoryRuntime, checkpointer: Any = None) -> Any:
    nodes = WorkflowNodes(runtime)
    graph = StateGraph(FactoryState)
    graph.add_node("bootstrap", nodes.bootstrap)
    graph.add_node("plan", nodes.plan)
    graph.add_node("build", nodes.build)
    graph.add_node("test", nodes.test)
    graph.add_node("repair", nodes.repair)
    graph.add_node("review", nodes.review)
    graph.add_node("revise", nodes.revise)
    graph.add_node("finalize", nodes.finalize)
    graph.add_node("failed", nodes.failed)
    graph.add_edge(START, "bootstrap")
    graph.add_conditional_edges("bootstrap", after_ok, {"next": "plan", "failed": "failed"})
    graph.add_conditional_edges("plan", after_ok, {"next": "build", "failed": "failed"})
    graph.add_conditional_edges("build", after_ok, {"next": "test", "failed": "failed"})
    graph.add_conditional_edges("test", after_tests)
    graph.add_conditional_edges("repair", after_ok, {"next": "test", "failed": "failed"})
    graph.add_conditional_edges("review", after_review)
    graph.add_conditional_edges("revise", after_ok, {"next": "test", "failed": "failed"})
    graph.add_edge("finalize", END)
    graph.add_edge("failed", END)
    return graph.compile(checkpointer=checkpointer)


MERMAID = """flowchart TD
    START --> bootstrap --> plan --> build --> test
    test -->|pass| review
    test -->|fail, budget remains| repair --> test
    test -->|fail, exhausted| failed
    review -->|approve| finalize --> END
    review -->|reject, budget remains| revise --> test
    review -->|reject, exhausted| failed
"""
