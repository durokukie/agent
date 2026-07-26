"""Orchestration의 허용된 import 방향을 검증한다."""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path


class OrchestrationImportTests(unittest.TestCase):
    """Supervisor가 provider·구현·전송·저장 세부사항과 격리된다."""

    def test_orchestration_does_not_import_forbidden_adapters(self) -> None:
        spec = importlib.util.find_spec("agent_system.orchestration")
        assert spec is not None and spec.submodule_search_locations is not None
        package = Path(next(iter(spec.submodule_search_locations)))
        imported_roots: set[str] = set()
        for source_path in package.glob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_roots.update(
                        alias.name.split(".", maxsplit=1)[0] for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    assert node.module is not None
                    imported_roots.add(node.module.split(".", maxsplit=1)[0])

        self.assertTrue(
            imported_roots.isdisjoint(
                {
                    "fastapi",
                    "langchain_upstage",
                    "rich",
                    "sqlalchemy",
                    "sqlite3",
                }
            )
        )
