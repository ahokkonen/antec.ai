"""Граница заменяемости (AGENTS.md, правило 4; ADR-0010, п. 1): SDK только в adapters/."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "antec_platform"
VENDOR = ("pydantic_ai", "pydantic_graph", "openai", "httpx2", "litellm", "mcp_types")


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_vendor_sdks_only_in_adapters() -> None:
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if "adapters" in path.relative_to(PACKAGE).parts:
            continue
        bad = _imports(path) & set(VENDOR)
        if bad:
            offenders.append(f"{path.relative_to(PACKAGE)}: {sorted(bad)}")
    assert offenders == []


def test_agent_contract_does_not_import_adapters() -> None:
    """Контракт агентов не зависит от реализации: agents.py не импортирует adapters/."""
    tree = ast.parse((PACKAGE / "agents.py").read_text(encoding="utf-8"))
    modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    assert not [m for m in modules if m.startswith("antec_platform.adapters")]
