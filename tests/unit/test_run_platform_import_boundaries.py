"""Executable purity boundary for the RUN-F1a protocol core."""

import ast
from pathlib import Path

RUN_PLATFORM_ROOT = Path(__file__).resolve().parents[2] / "api" / "run_platform"
PROTOCOL_FILES = (
    RUN_PLATFORM_ROOT / "domain.py",
    RUN_PLATFORM_ROOT / "schemas.py",
    RUN_PLATFORM_ROOT / "events.py",
)
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "agent",
        "core",
        "fastapi",
        "redis",
        "sqlalchemy",
    }
)
FORBIDDEN_API_PREFIXES = (
    "api.apps",
    "api.db",
    "api.identity",
    "api.identity_adapters",
    "api.execution_control",
    "api.security",
    "api.channels",
    "api.channel_control",
    "api.channel_execution",
    "api.channel_runtime",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_run_platform_protocol_has_no_runtime_or_identity_dependencies() -> None:
    violations: list[str] = []
    for path in PROTOCOL_FILES:
        for module in _imports(path):
            root = module.split(".", maxsplit=1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS or module.startswith(FORBIDDEN_API_PREFIXES):
                violations.append(f"{path.name}: {module}")

    assert violations == []
