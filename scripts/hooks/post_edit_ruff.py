"""Claude Code PostToolUse 钩子：AI 每次编辑 .py 文件后即时 lint + 类型检查。

从 stdin 读取钩子 JSON，提取被编辑的文件路径：
- 非 .py 文件或在 ruff 排除目录内 → 直接放行（exit 0）
- 对该单个文件跑 ruff format + ruff check --fix（毫秒级）
- 自动修复后仍有残留诊断 → 输出到 stderr 并 exit 2
  （exit 2 会把错误回灌给模型，促使其当场修复）
- 文件在 mypy 纳管范围内（pyproject [tool.mypy] files/exclude，动态读取零漂移）
  → 追加 dmypy 增量类型检查（daemon 首跑建缓存较慢，之后秒级）；
  类型错误同样 exit 2 回灌。daemon 损坏时 dmypy stop 后重试一次，
  仍失败则放行（fail-open，钩子不因基建故障阻塞编辑）。
- 纳管范围外的文件静默跳过类型检查。

仅作用于单个文件，绝不触发全库扫描。
"""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# 与 pyproject [tool.ruff] extend-exclude 保持一致
EXCLUDED_PARTS = {"server", "internal", "temp", "tmp", ".venv", "node_modules"}

UV = ["uv", "run", "--no-sync"]


def in_mypy_scope(rel: Path) -> bool:
    """编辑的文件是否在 pyproject [tool.mypy] 的纳管范围内（files 命中且不被 exclude）。"""
    try:
        mypy_cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")).get("tool", {}).get("mypy", {})
    except (OSError, tomllib.TOMLDecodeError):
        return False
    posix = rel.as_posix()
    roots = mypy_cfg.get("files", [])
    if not any(posix == root or posix.startswith(root.rstrip("/") + "/") for root in roots):
        return False
    return not any(re.search(pattern, posix) for pattern in mypy_cfg.get("exclude", []))


def run_dmypy(path: Path) -> subprocess.CompletedProcess[str] | None:
    """dmypy run 单文件；daemon 损坏（退出码 ≥2）时 stop 后重试一次，超时/仍损坏返回 None。

    dmypy 不支持 pyproject 里的 follow_imports=silent，故命令行覆盖为 normal；
    由此连带报告的 import 图内其他文件的错误在调用方过滤掉，
    对被编辑文件本身的诊断与 CI（silent）完全一致。
    """
    cmd = [*UV, "dmypy", "run", "--", "--follow-imports=normal", str(path)]
    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=50)
        if result.returncode >= 2:  # 0=通过 1=类型错误 ≥2=daemon 异常
            subprocess.run([*UV, "dmypy", "stop"], cwd=REPO_ROOT, capture_output=True, timeout=15)
            result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=50)
        return result if result.returncode < 2 else None
    except subprocess.TimeoutExpired:
        return None


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

    subprocess.run([*UV, "ruff", "format", "--quiet", str(path)], cwd=REPO_ROOT, capture_output=True)
    subprocess.run([*UV, "ruff", "check", "--fix", "--quiet", str(path)], cwd=REPO_ROOT, capture_output=True)

    # 自动修复后复查，残留问题回灌给模型
    result = subprocess.run([*UV, "ruff", "check", "--output-format", "concise", str(path)], cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ruff 在自动修复后仍有残留问题，请修复：\n{result.stdout}", file=sys.stderr)
        return 2

    if in_mypy_scope(rel):
        dmypy_result = run_dmypy(path)
        if dmypy_result is not None and dmypy_result.returncode == 1:
            # 只回灌被编辑文件自身的诊断（follow-imports=normal 连带的他文件错误滤除）
            own_lines = [line for line in dmypy_result.stdout.splitlines() if line.startswith(rel.as_posix())]
            if own_lines:
                print("dmypy 类型检查未通过，请修复：\n" + "\n".join(own_lines), file=sys.stderr)
                return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
