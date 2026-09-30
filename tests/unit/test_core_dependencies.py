"""Prevent application adapters from becoming core execution dependencies again."""

import ast
from pathlib import Path


def test_core_has_no_application_dependencies():
    root = Path(__file__).resolve().parents[2] / "src"
    pending = [
        "software_factory." + name
        for name in ("engine", "control", "worker", "worker_manager", "jobs")
    ]
    visited = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        assert not module.startswith(("software_factory.bb", "acp")), module
        path = root / (module.replace(".", "/") + ".py")
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            imports = (
                [node.module]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for name in imports:
                if name and name.startswith(("software_factory.", "acp")):
                    pending.append(name)
