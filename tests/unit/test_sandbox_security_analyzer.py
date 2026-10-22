"""Exercise the standalone executor's real analyzer without its top-level import collisions."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

EXECUTOR_ROOT = Path(__file__).resolve().parents[2] / "agent" / "sandbox" / "executor_manager"
ANALYZE_SCRIPT = """
import json
import logging
import sys

from core.logger import logger
from models.enums import SupportLanguage
from services.security import analyze_code_security

warnings = []

class Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        warnings.append(record.getMessage())

logger.addHandler(Capture(level=logging.WARNING))
request = json.load(sys.stdin)
safe, issues = analyze_code_security(request['code'], SupportLanguage(request['language']))
print(json.dumps({'safe': safe, 'issues': issues, 'warnings': warnings}))
"""


def _analyze(code: str, language: str) -> dict[str, Any]:
    # The executor is a separate app with its own core/models/services packages.
    # Keep those real imports in a child interpreter, isolated from backend imports.
    result = subprocess.run(
        [sys.executable, "-c", ANALYZE_SCRIPT],
        input=json.dumps({"code": code, "language": language}),
        cwd=EXECUTOR_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("module", ["child_process", "fs", "worker_threads"])
@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_javascript_require_quotes_are_rejected(module: str, quote: str) -> None:
    result = _analyze(f"\nconst mod = require({quote}{module}{quote}); async function main() {{ return 'ok'; }}", "nodejs")

    assert result["safe"] is False
    assert [f"Require: {module}", 2] in result["issues"]


@pytest.mark.parametrize("statement", ["import builtins", "import builtins as b", "from builtins import len as size"])
def test_python_builtins_import_forms_are_rejected(statement: str) -> None:
    result = _analyze(f"{statement}\ndef main():\n    return 2\n", "python")

    assert result["safe"] is False
    assert any("builtins" in issue and line == 1 for issue, line in result["issues"])


@pytest.mark.parametrize("call", ["eval", "exec", "open"])
def test_python_attribute_call_is_rejected_and_logged_without_import(call: str) -> None:
    result = _analyze(f"def main(obj):\n    return obj.{call}('harmless')\n", "python")

    assert result["safe"] is False
    assert [f"Call: {call}", 2] in result["issues"]
    assert f"[SafeCheck] Attribute-style dangerous call detected: {call} (line 2)" in result["warnings"]


@pytest.mark.parametrize(
    ("language", "code"),
    [
        ("python", "import json\ndef main(value):\n    return json.dumps({'answer': len(value)})\n"),
        ("nodejs", "async function main(args) { const label = `hello ${args.name}`; return { label }; }"),
    ],
)
def test_safe_code_with_normal_imports_attributes_and_templates_passes(language: str, code: str) -> None:
    result = _analyze(code, language)

    assert result == {"safe": True, "issues": [], "warnings": []}
