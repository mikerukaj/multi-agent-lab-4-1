"""SC-110: the Coder and QA agents never refer to each other's code."""

import ast
import inspect
import unittest
from pathlib import Path

from orchestration import team

ROOT = Path(__file__).resolve().parents[1]


def imported_modules(path: Path) -> set[str]:
    """Every module a file imports, by full dotted name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules |= {f"{node.module}.{alias.name}" for alias in node.names}
    return modules


class AgentIsolationTests(unittest.TestCase):
    def test_coder_never_imports_qa_and_qa_never_imports_coder(self) -> None:
        coder = imported_modules(ROOT / "agents" / "coder_agent.py")
        qa = imported_modules(ROOT / "agents" / "qa_agent.py")
        self.assertFalse({m for m in coder if m.startswith("agents.qa_agent")}, coder)
        self.assertFalse({m for m in qa if m.startswith("agents.coder_agent")}, qa)

    def test_endpoints_import_no_agent_module(self) -> None:
        endpoints = imported_modules(ROOT / "messaging" / "endpoints.py")
        self.assertFalse({m for m in endpoints if m.startswith("agents")}, endpoints)

    def test_loop_nodes_take_a2a_clients_not_agents(self) -> None:
        for node in (team.qa_node, team.fix_node, team.finish_node):
            with self.subTest(node=node.__name__):
                parameters = set(inspect.signature(node).parameters)
                self.assertIn("clients", parameters)
                self.assertFalse(parameters & {"qa", "coder"})


if __name__ == "__main__":
    unittest.main()
