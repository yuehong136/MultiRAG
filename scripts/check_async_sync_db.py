"""门禁：禁止 `async def` 路由 + 同步 Session（`Depends(get_db)`）组合。

背景（internal/engineering_modernization_plan.md §2）：FastAPI 中普通 `def` handler
进线程池，同步 DB 调用安全；`async def` handler 跑在事件循环上，同步 Session 查询
会阻塞整个进程的并发。上游 ragflow 已迁 Quart（全 async def）+ 同步 peewee，
移植时照抄会持续引入该问题，故设本门禁。

用法：
    uv run python scripts/check_async_sync_db.py             # 门禁模式：有违规即退出码 1
    uv run python scripts/check_async_sync_db.py --report    # 报告模式：输出 A/B 分类清单

分类（--report）：
    A 类 = 函数体内无任何 await / async for / async with —— 直接改普通 def 即可；
    B 类 = 有真实异步用法（SSE 流式、LLM 异步调用等）—— 保留 async，DB 操作需外移
           或 run_in_threadpool 包裹（终态迁 AsyncSession，见方案 §11）。

豁免：在 `async def` 行或其紧邻上一行加注释 `# async-db-ok: <原因>`
（仅限已按 §2 分类 B 处理完 DB 阻塞的 handler）。
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

SCAN_ROOT = Path("api/apps")
EXEMPT_MARK = "async-db-ok:"


@dataclass(frozen=True)
class Violation:
    file: Path
    line: int
    func: str
    has_async_usage: bool  # True → B 类；False → A 类


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


def _takes_sync_session(fn: ast.AsyncFunctionDef) -> bool:
    all_defaults: list[ast.expr | None] = [*fn.args.defaults, *fn.args.kw_defaults]
    return any(_is_get_db_depends(d) for d in all_defaults)


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
        if not _takes_sync_session(node):
            continue
        if _is_exempt(node, source_lines):
            continue
        violations.append(Violation(file=path, line=node.lineno, func=node.name, has_async_usage=_has_async_usage(node)))
    return violations


def collect() -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(SCAN_ROOT.rglob("*.py")):
        violations.extend(scan_file(path))
    return violations


def print_report(violations: list[Violation]) -> None:
    class_a = [v for v in violations if not v.has_async_usage]
    class_b = [v for v in violations if v.has_async_usage]
    print(f"async def + Depends(get_db) 共 {len(violations)} 处（A 类 {len(class_a)} / B 类 {len(class_b)}）\n")
    print(f"== A 类（无 await，直接改普通 def）: {len(class_a)} 处 ==")
    for v in class_a:
        print(f"  {v.file}:{v.line}  {v.func}")
    print(f"\n== B 类（有真实异步用法，DB 需外移/threadpool/AsyncSession）: {len(class_b)} 处 ==")
    for v in class_b:
        print(f"  {v.file}:{v.line}  {v.func}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", action="store_true", help="输出 A/B 分类报告（不设退出码）")
    args = parser.parse_args()

    violations = collect()
    if args.report:
        print_report(violations)
        return 0

    if violations:
        print(f"发现 {len(violations)} 处 async def 路由使用同步 Session（事件循环阻塞风险）：", file=sys.stderr)
        for v in violations:
            kind = "B(有await)" if v.has_async_usage else "A(无await→改def)"
            print(f"  {v.file}:{v.line}  {v.func}  [{kind}]", file=sys.stderr)
        print("\n修法见 internal/engineering_modernization_plan.md §2；确已处理 DB 阻塞的加 `# async-db-ok: <原因>` 豁免。", file=sys.stderr)
        return 1

    print("check_async_sync_db: OK（api/apps 无 async def + 同步 Session 组合）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
