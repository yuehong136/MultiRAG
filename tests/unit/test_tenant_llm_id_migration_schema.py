"""tenant_llm id 迁移（284487da1d89）的 schema 判定逻辑单测。

迁移脚本用 `_is_varchar_id` / `_is_integer_id` / `BUSINESS_KEY` 区分三种情形：
旧版 varchar id + 业务键 PK（执行迁移）、已迁移的整型 id PK（幂等跳过）、
其他意外 schema（抛错终止）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "configs" / "alembic" / "versions" / "284487da1d89_migrate_tenant_llm_id_to_bigint_pk.py"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("tenant_llm_id_migration", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tenant_llm_id_migration_detects_legacy_varchar_id() -> None:
    migration = _load_migration_module()

    assert migration._is_varchar_id({"type": sa.String(length=32)})
    assert not migration._is_varchar_id({"type": sa.BigInteger()})
    assert not migration._is_varchar_id(None)


def test_tenant_llm_id_migration_detects_migrated_integer_id() -> None:
    migration = _load_migration_module()

    assert migration._is_integer_id({"type": sa.BigInteger()})
    assert migration._is_integer_id({"type": sa.Integer()})
    assert not migration._is_integer_id({"type": sa.String(length=32)})
    assert not migration._is_integer_id(None)


def test_tenant_llm_id_migration_business_key_is_legacy_pk() -> None:
    migration = _load_migration_module()

    assert migration.BUSINESS_KEY == ("tenant_id", "llm_factory", "llm_name")
