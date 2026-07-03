"""Claude Code PostToolUse 钩子：AI 每次编辑 .py 文件后即时 lint。

从 stdin 读取钩子 JSON，提取被编辑的文件路径：
- 非 .py 文件或在 ruff 排除目录内 → 直接放行（exit 0）
- 对该单个文件跑 ruff format + ruff check --fix（毫秒级）
- 自动修复后仍有残留诊断 → 输出到 stderr 并 exit 2
  （exit 2 会把错误回灌给模型，促使其当场修复）

仅作用于单个文件，绝不触发全库扫描。
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# 与 pyproject [tool.ruff] extend-exclude 保持一致
EXCLUDED_PARTS = {"server", "internal", "temp", "tmp", ".venv", "node_modules"}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    file_path = (payload.get("tool_input") or {}).get("file_path", "")
    if not file_path or not file_path.endswith(".py"):
        return 0

    path = Path(file_path)
    if not path.is_file():
        return 0
    try:
        rel = path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return 0  # 仓库外的文件不管
    if EXCLUDED_PARTS & set(rel.parts):
        return 0

    uv = ["uv", "run", "--no-sync"]
    subprocess.run([*uv, "ruff", "format", "--quiet", str(path)], cwd=REPO_ROOT, capture_output=True)
    subprocess.run([*uv, "ruff", "check", "--fix", "--quiet", str(path)], cwd=REPO_ROOT, capture_output=True)

    # 自动修复后复查，残留问题回灌给模型
    result = subprocess.run([*uv, "ruff", "check", "--output-format", "concise", str(path)], cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ruff 在自动修复后仍有残留问题，请修复：\n{result.stdout}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
