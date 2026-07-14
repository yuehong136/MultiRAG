"""check_async_sync_db 纳管反转检查（§11.6 Phase 3 棘轮）契约。

纳管文件 = 已完成纯异步收口：任何函数（含普通 def）挂同步 Session 依赖
（get_db / manager / 同步鉴权污点集），或引用自开同步会话
（SessionLocal / db_connection），都必须被硬失败拦截；run_sync 回调的
`s: Session` 形态与 AsyncSession 轨不受影响。
"""

from pathlib import Path

from scripts.check_async_sync_db import MANAGED_PURE_ASYNC, collect_managed_sync

TAINTED = frozenset({"get_db", "manager", "current_tenant_id"})


def _check(tmp_path: Path, source: str):
    managed_file = tmp_path / "managed_api.py"
    managed_file.write_text(source, encoding="utf-8")
    return collect_managed_sync([managed_file], TAINTED)


def test_flags_plain_def_route_with_sync_session(tmp_path):
    violations = _check(tmp_path, "def route(db: Session = Depends(get_db)):\n    return db\n")

    assert [v.detail for v in violations] == ["route 挂 Depends(get_db)"]


def test_flags_manager_and_tainted_auth_dep(tmp_path):
    source = "async def a(user=Depends(manager)):\n    return user\n\n\ndef b(tenant_id: str = Depends(current_tenant_id)):\n    return tenant_id\n"

    violations = _check(tmp_path, source)

    assert {v.detail for v in violations} == {"a 挂 Depends(manager)", "b 挂 Depends(current_tenant_id)"}


def test_flags_self_opened_sync_session_names(tmp_path):
    source = "def helper():\n    with db_connection() as s:\n        return s\n\n\ndef mk():\n    return db_models.SessionLocal()\n"

    violations = _check(tmp_path, source)

    assert {v.detail for v in violations} == {"引用自开同步会话 db_connection", "引用自开同步会话 SessionLocal"}


def test_pure_async_shape_with_run_sync_callback_passes(tmp_path):
    source = (
        "async def route(db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)):\n"
        "    def _cb(s: Session) -> bool:\n"
        "        return bool(SomeService.query(s))\n"
        "    return await db.run_sync(_cb)\n"
    )

    assert _check(tmp_path, source) == []


def test_unparsable_managed_file_is_a_violation(tmp_path):
    violations = _check(tmp_path, "def broken(:\n")

    assert len(violations) == 1
    assert "纳管文件不可解析" in violations[0].detail


def test_enrolled_files_exist_on_disk():
    assert MANAGED_PURE_ASYNC  # 清单只增不减，永不为空
    for path in MANAGED_PURE_ASYNC:
        assert path.exists(), f"纳管清单指向不存在的文件: {path}"
