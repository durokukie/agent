"""HTTP adapter와 runtime composition root의 import 방향을 검증한다."""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path


class HttpImportArchitectureTests(unittest.TestCase):
    """HTTP가 runtime seam을 건너 concrete 구현을 알지 못하게 한다."""

    def test_http_imports_only_the_runtime_application_interface(self) -> None:
        spec = importlib.util.find_spec("agent_system.http")
        assert spec is not None and spec.submodule_search_locations is not None
        package = Path(next(iter(spec.submodule_search_locations)))
        agent_system_imports: set[str] = set()
        forbidden_roots: set[str] = set()
        for source_path in package.glob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    forbidden_roots.update(
                        alias.name.split(".", maxsplit=1)[0] for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    assert node.module is not None
                    forbidden_roots.add(node.module.split(".", maxsplit=1)[0])
                    if node.module.startswith("agent_system"):
                        agent_system_imports.add(node.module)

        self.assertEqual(agent_system_imports, {"agent_system.runtime"})
        self.assertTrue(
            forbidden_roots.isdisjoint(
                {
                    "langchain_upstage",
                    "langgraph",
                    "rich",
                    "sqlalchemy",
                    "sqlite3",
                }
            )
        )

    def test_runtime_does_not_import_http_or_rich(self) -> None:
        spec = importlib.util.find_spec("agent_system.runtime")
        assert spec is not None and spec.submodule_search_locations is not None
        package = Path(next(iter(spec.submodule_search_locations)))
        imported: set[str] = set()
        for source_path in package.glob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    assert node.module is not None
                    imported.add(node.module)

        self.assertNotIn("agent_system.http", imported)
        self.assertNotIn("rich", imported)


if __name__ == "__main__":
    unittest.main()
