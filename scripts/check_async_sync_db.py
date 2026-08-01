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

def-handler 盲区检查（§11 Phase 2 起）：普通 `def` handler + `Depends(get_db)` 本身合法
（跑线程池），但其**嵌套 async 生成器**（典型：StreamingResponse 的流式体）引用同步
Session 时，流式阶段仍跑在事件循环上——同样计入违规（reason=B(def嵌套async用db)），
走同一基线燃尽机制。

依赖树检查（§11 Phase 2 起，硬失败、无基线）：
    handler 挂 `Depends(get_async_db)` 时，其 Depends 链上不得出现同步 Session 依赖
    ——包括直接 `Depends(get_db)`、签名里（直接或传递）带 Depends(get_db) 的依赖函数
    （token_required / current_tenant_id 等，从 api/utils/api_utils.py 自动推导），
    以及 `Depends(manager)`（LoginManager 实例，user_loader/兜底内部自开 SessionLocal）。
    同一请求业务走 AsyncSession、鉴权走同步 Session 即"双轨请求"，违反 AGENTS.md
    「同一请求禁混两种 session」；异步变体见 api_utils 的 async_token_required 等。

纳管反转检查（§11 Phase 2.5/3 棘轮，硬失败、无基线）：
    `MANAGED_PURE_ASYNC` 清单中的文件已完成纯异步收口，检查逻辑对其反转——从
    「禁止 async def + 同步 Session」变为「禁止任何同步 Session 形态」：任何函数
    （含普通 def）挂同步 Session 依赖（get_db / manager / 同步鉴权污点集），或
    引用自开同步会话（SessionLocal / db_connection），一律违规。每收口一个文件，
    在同一提交把它加进清单，只增不减；run_sync 回调收到的同步 facade 以参数形式
    出现、不引用上述名字，天然不受影响。
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SCAN_ROOT = Path("api/apps")
BASELINE_PATH = Path("scripts/async_sync_db_baseline.txt")
EXEMPT_MARK = "async-db-ok:"

# 依赖树污点源：同步鉴权依赖所在文件（签名含 Depends(get_db) 的函数自动入污点集）
DEP_SCAN_FILES = (Path("api/utils/api_utils.py"),)
# 非函数形态的同步依赖：LoginManager 实例，user_loader/兜底内部自开 SessionLocal
SYNC_DEP_EXTRA = frozenset({"manager"})

# 纳管清单（棘轮反转，只增不减）：已完成纯异步收口的文件，禁止任何同步 Session 形态。
# 每收口一个文件，在同一提交把它加进来。文件被合并/删除时条目随之迁移
# （config_api.py 的端点已并入 system_api.py，后者在管）。
MANAGED_PURE_ASYNC: tuple[Path, ...] = (
    Path("api/apps/restful_apis/chat_api.py"),
    Path("api/apps/restful_apis/connector_api.py"),
    Path("api/apps/restful_apis/dataset_api.py"),
    Path("api/apps/restful_apis/document_api.py"),
    Path("api/apps/restful_apis/file_api.py"),
    Path("api/apps/restful_apis/memory_api.py"),
    Path("api/apps/restful_apis/search_api.py"),
    Path("api/apps/restful_apis/system_api.py"),
    Path("api/apps/restful_apis/tenant_api.py"),
)
# 纳管文件内出现即违规的自开同步会话名（Name 或 Attribute 末端名）
SYNC_SESSION_OPEN_NAMES = frozenset({"SessionLocal", "db_connection"})


@dataclass(frozen=True)
class DualTrackViolation:
    """AsyncSession 路由的依赖树中出现同步 Session 依赖（双轨请求）。"""

    file: Path
    line: int
    func: str
    dep: str


@dataclass(frozen=True)
class ManagedSyncViolation:
    """纳管（纯异步）文件中出现同步 Session 形态（棘轮反转）。"""

    file: Path
    line: int
    detail: str


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


def _param_depends_targets(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """签名默认值里全部 `Depends(X)` 的 X 名字（含位置参数与 kw-only 参数）。"""
    targets: list[str] = []
    kw_defaults = [d for d in fn.args.kw_defaults if d is not None]
    for default in [*fn.args.defaults, *kw_defaults]:
        if not isinstance(default, ast.Call) or _call_target_name(default.func) != "Depends":
            continue
        for arg in default.args:
            name = _call_target_name(arg)
            if name:
                targets.append(name)
    return targets


def collect_sync_dep_names() -> frozenset[str]:
    """依赖树污点集：签名里（直接或传递）带 Depends(get_db) 的依赖函数名 + manager。"""
    dep_targets: dict[str, set[str]] = {}
    for path in DEP_SCAN_FILES:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            print(f"[warn] 依赖树污点源不可解析 {path}: {exc}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                dep_targets[node.name] = set(_param_depends_targets(node))
    tainted = {"get_db", *SYNC_DEP_EXTRA}
    changed = True
    while changed:
        changed = False
        for name, targets in dep_targets.items():
            if name not in tainted and targets & tainted:
                tainted.add(name)
                changed = True
    return frozenset(tainted)


def collect_dual_track(tainted: frozenset[str]) -> list[DualTrackViolation]:
    """扫 SCAN_ROOT：挂 Depends(get_async_db) 的函数，其 Depends 链上出现污点依赖即违规。"""
    violations: list[DualTrackViolation] = []
    for path in sorted(SCAN_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # 语法错误由主扫描报 warn，这里静默跳过
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            targets = _param_depends_targets(node)
            if "get_async_db" not in targets:
                continue
            for dep in targets:
                if dep in tainted:
                    violations.append(DualTrackViolation(file=path, line=node.lineno, func=node.name, dep=dep))
    return violations


def collect_managed_sync(files: Iterable[Path], tainted: frozenset[str]) -> list[ManagedSyncViolation]:
    """纳管文件反转检查：任何函数挂同步 Session 依赖、或引用自开同步会话名即违规。

    run_sync 回调收到的同步 facade 以参数形式出现（`s: Session`），不引用
    SessionLocal/db_connection，也不挂 Depends——天然不受本检查影响。
    """
    violations: list[ManagedSyncViolation] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            violations.append(ManagedSyncViolation(file=path, line=0, detail=f"纳管文件不可解析: {exc}"))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for dep in _param_depends_targets(node):
                    if dep in tainted:
                        violations.append(ManagedSyncViolation(file=path, line=node.lineno, detail=f"{node.name} 挂 Depends({dep})"))
            elif isinstance(node, ast.Name | ast.Attribute):
                name = _call_target_name(node)
                if name in SYNC_SESSION_OPEN_NAMES:
                    violations.append(ManagedSyncViolation(file=path, line=node.lineno, detail=f"引用自开同步会话 {name}"))
    return violations


def _sync_session_arg_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
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


def _nested_async_uses_session(fn: ast.FunctionDef | ast.AsyncFunctionDef, session_names: list[str]) -> bool:
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


def _is_exempt(fn: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str]) -> bool:
    """豁免标记可出现在 `async def` 的上一行，或签名区间内任意一行。

    签名区间 = def 行到函数体首语句前——ruff format 折行长签名时会把
    行尾注释挪到 `):` 收尾行，因此不能只看 def 行本身。
    """
    start_idx = max(0, fn.lineno - 2)  # def 行的上一行（0 起）
    end_idx = (fn.body[0].lineno - 1) if fn.body else fn.lineno
    return any(EXEMPT_MARK in line for line in source_lines[start_idx:end_idx])


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
        if isinstance(node, ast.AsyncFunctionDef):
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
        elif isinstance(node, ast.FunctionDef):
            # def handler 本身进线程池没问题；但嵌套 async 生成器引用同步 Session 时，
            # 流式阶段仍在事件循环上（Phase 2 实例：conversation_app.ask_about 旧形态）
            session_names = _sync_session_arg_names(node)
            if not session_names:
                continue
            if _is_exempt(node, source_lines):
                continue
            if _nested_async_uses_session(node, session_names):
                violations.append(Violation(file=path, line=node.lineno, func=node.name, reason="B(def嵌套async用db)"))
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


def print_report(violations: list[Violation], dual_track: list[DualTrackViolation], managed: list[ManagedSyncViolation]) -> None:
    class_a = [v for v in violations if v.is_class_a]
    class_b = [v for v in violations if not v.is_class_a]
    print(f"async def + Depends(get_db) 共 {len(violations)} 处（A 类 {len(class_a)} / B 类 {len(class_b)}）\n")
    print(f"== A 类（无 await，直接改普通 def）: {len(class_a)} 处 ==")
    for v in class_a:
        print(f"  {v.file}:{v.line}  {v.func}")
    print(f"\n== B 类（DB 需外移/threadpool/AsyncSession）: {len(class_b)} 处 ==")
    for v in class_b:
        print(f"  {v.file}:{v.line}  {v.func}  [{v.reason}]")
    print(f"\n== 双轨（get_async_db 路由依赖树含同步 Session 依赖，硬失败无基线）: {len(dual_track)} 处 ==")
    for d in dual_track:
        print(f"  {d.file}:{d.line}  {d.func}  [Depends({d.dep})]")
    print(f"\n== 纳管反转（纯异步文件出现同步 Session，硬失败无基线；纳管 {len(MANAGED_PURE_ASYNC)} 文件）: {len(managed)} 处 ==")
    for m in managed:
        print(f"  {m.file}:{m.line}  {m.detail}")


def run_gate(violations: list[Violation], dual_track: list[DualTrackViolation], managed: list[ManagedSyncViolation]) -> int:
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

    if dual_track:
        ok = False
        print(f"双轨违规 {len(dual_track)} 处：get_async_db 路由的依赖树中出现同步 Session 依赖（无基线，必须当场修）：", file=sys.stderr)
        for d in dual_track:
            print(f"  {d.file}:{d.line}  {d.func}  [Depends({d.dep})]", file=sys.stderr)
        print("改用 api/utils/api_utils.py 的 async_token_required / async_beta_token_required / async_current_tenant_id / async_current_user。", file=sys.stderr)

    if managed:
        ok = False
        print(f"纳管违规 {len(managed)} 处：纯异步文件出现同步 Session 形态（棘轮反转，无基线，必须当场修）：", file=sys.stderr)
        for m in managed:
            print(f"  {m.file}:{m.line}  {m.detail}", file=sys.stderr)
        print("纳管清单见 MANAGED_PURE_ASYNC；这些文件的 DB 一律走 AsyncSession（迁移期 run_sync 桥接规范见 AGENTS.md）。", file=sys.stderr)

    if ok:
        print(f"check_async_sync_db: OK（新增 0；双轨 0；纳管 {len(MANAGED_PURE_ASYNC)} 文件净；基线存量 {sum(current.values())} 处待燃尽）")
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", action="store_true", help="输出 A/B 分类报告（不设退出码）")
    parser.add_argument("--write-baseline", action="store_true", help="用当前扫描结果重写基线文件")
    args = parser.parse_args()

    violations = collect()
    tainted = collect_sync_dep_names()
    dual_track = collect_dual_track(tainted)
    managed = collect_managed_sync(MANAGED_PURE_ASYNC, tainted)
    if args.report:
        print_report(violations, dual_track, managed)
        return 0
    if args.write_baseline:
        write_baseline(violations)
        print(f"基线已写入 {BASELINE_PATH}（{len(violations)} 条）")
        return 0
    return run_gate(violations, dual_track, managed)


if __name__ == "__main__":
    sys.exit(main())
