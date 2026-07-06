"""门禁：禁止 `async def` 路由 + 同步 Session（`Depends(get_db)`）组合。

背景（internal/engineering_modernization_plan.md §2）：FastAPI 中普通 `def` handler
进线程池，同步 DB 调用安全；`async def` handler 跑在事件循环上，同步 Session 查询
会阻塞整个进程的并发。上游已全面 async 化但 ORM 仍同步，移植时照抄会持续引入
该问题，故设本门禁。

用法：
    uv run python scripts/check_async_sync_db.py                   # 门禁模式：与基线比对，新增违规/基线未清理即退出码 1
    uv run python scripts/check_async_sync_db.py --report          # 报告模式：输出 A/B 分类清单
    uv run python scripts/check_async_sync_db.py --write-baseline  # 重写基线（仅限首建或确认收缩后使用）

分类（--report）：
    A 类 = 函数体无直接 await/async for/async with，且嵌套 async 函数不引用 Session 参数
           —— 直接改普通 def 即可；
    B 类 = 有真实异步用法，或嵌套 async 生成器（流式响应）内引用了同步 Session
           —— 保留 async，DB 操作需外移/run_in_threadpool（终态迁 AsyncSession，见方案 §11）。

基线（燃尽，只减不增）：scripts/async_sync_db_baseline.txt 记录存量违规（file::func）。
    - 新增违规（不在基线内）→ 门禁失败；
    - 基线条目已修复但未从基线删除 → 同样失败（强制收缩，防止基线腐化）。

豁免：在 `async def` 行或其紧邻上一行加注释 `# async-db-ok: <原因>`
（仅限已按 §2 分类 B 处理完 DB 阻塞的 handler）。
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SCAN_ROOT = Path("api/apps")
BASELINE_PATH = Path("scripts/async_sync_db_baseline.txt")
EXEMPT_MARK = "async-db-ok:"


@dataclass(frozen=True)
class Violation:
    file: Path
    line: int
    func: str
    reason: str  # "A(无await→改def)" / "B(有await)" / "B(嵌套async用db)"

    @property
    def key(self) -> str:
        return f"{self.file}::{self.func}"

    @property
    def is_class_a(self) -> bool:
        return self.reason.startswith("A")


def _call_target_name(node: ast.expr) -> str | None:
    """取 Name/Attribute 的末端名字（`get_db` / `deps.get_db` 都返回 'get_db'）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_get_db_depends(default: ast.expr | None) -> bool:
    """默认值是否为 `Depends(get_db)`（含 `Depends(deps.get_db)` 形式）。"""
    if not isinstance(default, ast.Call):
        return False
    if _call_target_name(default.func) != "Depends":
        return False
    return any(_call_target_name(arg) == "get_db" for arg in default.args)


def _sync_session_arg_names(fn: ast.AsyncFunctionDef) -> list[str]:
    """返回默认值为 Depends(get_db) 的参数名（通常是 'db'）。"""
    names: list[str] = []
    positional = [*fn.args.posonlyargs, *fn.args.args]
    defaults = fn.args.defaults
    for arg, default in zip(positional[len(positional) - len(defaults) :], defaults, strict=True):
        if _is_get_db_depends(default):
            names.append(arg.arg)
    for kw_arg, kw_default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True):
        if kw_default is not None and _is_get_db_depends(kw_default):
            names.append(kw_arg.arg)
    return names


class _AsyncUsageFinder(ast.NodeVisitor):
    """查函数体内是否有直接的 await / async for / async with（不进入嵌套函数）。"""

    def __init__(self) -> None:
        self.found = False

    def visit_Await(self, node: ast.Await) -> None:
        self.found = True

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.found = True
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.found = True
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        pass  # 嵌套函数是独立作用域，不计入

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        pass


def _has_async_usage(fn: ast.AsyncFunctionDef) -> bool:
    finder = _AsyncUsageFinder()
    for stmt in fn.body:
        finder.visit(stmt)
        if finder.found:
            return True
    return False


def _nested_async_uses_session(fn: ast.AsyncFunctionDef, session_names: list[str]) -> bool:
    """嵌套 async 函数（典型：SSE 流式生成器）是否引用了同步 Session 参数。

    这类 handler 即使外层无 await，改 def 也只是把预处理挪出事件循环，
    流式阶段的同步 DB 仍会阻塞——按 B 类对待，留在基线里等深修。
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.AsyncFunctionDef) and node is not fn:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id in session_names:
                    return True
    return False


def _is_exempt(fn: ast.AsyncFunctionDef, source_lines: list[str]) -> bool:
    def_line_idx = fn.lineno - 1  # `async def` 所在行（不含装饰器）
    candidates = source_lines[max(0, def_line_idx - 1) : def_line_idx + 1]
    return any(EXEMPT_MARK in line for line in candidates)


def scan_file(path: Path) -> list[Violation]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # 门禁不该被语法错误文件卡死，交给 ruff 报
        print(f"[warn] 跳过无法解析的文件 {path}: {exc}", file=sys.stderr)
        return []

    source_lines = source.splitlines()
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        session_names = _sync_session_arg_names(node)
        if not session_names:
            continue
        if _is_exempt(node, source_lines):
            continue
        if _has_async_usage(node):
            reason = "B(有await)"
        elif _nested_async_uses_session(node, session_names):
            reason = "B(嵌套async用db)"
        else:
            reason = "A(无await→改def)"
        violations.append(Violation(file=path, line=node.lineno, func=node.name, reason=reason))
    return violations


def collect() -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(SCAN_ROOT.rglob("*.py")):
        violations.extend(scan_file(path))
    return violations


def load_baseline() -> Counter[str]:
    if not BASELINE_PATH.exists():
        return Counter()
    lines = [line.strip() for line in BASELINE_PATH.read_text(encoding="utf-8").splitlines()]
    return Counter(line for line in lines if line and not line.startswith("#"))


def write_baseline(violations: list[Violation]) -> None:
    header = (
        "# async def 路由 × 同步 Session 存量基线（燃尽清单，只减不增）\n"
        "# 由 scripts/check_async_sync_db.py --write-baseline 生成；\n"
        "# 修复一处后请手动删除对应行（门禁会强制：已修复未删行 = 失败）。\n"
        "# 修法见 internal/engineering_modernization_plan.md §2 / §11。\n"
    )
    body = "\n".join(sorted(v.key for v in violations))
    BASELINE_PATH.write_text(f"{header}{body}\n", encoding="utf-8")


def print_report(violations: list[Violation]) -> None:
    class_a = [v for v in violations if v.is_class_a]
    class_b = [v for v in violations if not v.is_class_a]
    print(f"async def + Depends(get_db) 共 {len(violations)} 处（A 类 {len(class_a)} / B 类 {len(class_b)}）\n")
    print(f"== A 类（无 await，直接改普通 def）: {len(class_a)} 处 ==")
    for v in class_a:
        print(f"  {v.file}:{v.line}  {v.func}")
    print(f"\n== B 类（DB 需外移/threadpool/AsyncSession）: {len(class_b)} 处 ==")
    for v in class_b:
        print(f"  {v.file}:{v.line}  {v.func}  [{v.reason}]")


def run_gate(violations: list[Violation]) -> int:
    baseline = load_baseline()
    current = Counter(v.key for v in violations)
    new_keys = current - baseline
    fixed_keys = baseline - current

    ok = True
    if new_keys:
        ok = False
        by_key = {v.key: v for v in violations}
        print(f"新增 {sum(new_keys.values())} 处 async def 路由使用同步 Session（事件循环阻塞风险）：", file=sys.stderr)
        for key in sorted(new_keys):
            v = by_key[key]
            print(f"  {v.file}:{v.line}  {v.func}  [{v.reason}]", file=sys.stderr)
        print("修法见 internal/engineering_modernization_plan.md §2；确已处理 DB 阻塞的加 `# async-db-ok: <原因>` 豁免。", file=sys.stderr)
    if fixed_keys:
        ok = False
        print(f"基线中 {sum(fixed_keys.values())} 条已修复，请从 {BASELINE_PATH} 删除（燃尽只减不增）：", file=sys.stderr)
        for key in sorted(fixed_keys):
            print(f"  {key}", file=sys.stderr)

    if ok:
        print(f"check_async_sync_db: OK（新增 0；基线存量 {sum(current.values())} 处待燃尽）")
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", action="store_true", help="输出 A/B 分类报告（不设退出码）")
    parser.add_argument("--write-baseline", action="store_true", help="用当前扫描结果重写基线文件")
    args = parser.parse_args()

    violations = collect()
    if args.report:
        print_report(violations)
        return 0
    if args.write_baseline:
        write_baseline(violations)
        print(f"基线已写入 {BASELINE_PATH}（{len(violations)} 条）")
        return 0
    return run_gate(violations)


if __name__ == "__main__":
    sys.exit(main())
