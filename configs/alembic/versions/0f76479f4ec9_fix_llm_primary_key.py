"""fix_llm_primary_key

修复 t_ai_llm_factories 和 t_ai_llms 表的重复数据问题，
并将 t_ai_llms 表的主键从 llm_name 改为 (fid, llm_name) 复合主键。

Revision ID: 0f76479f4ec9
Revises: 315b233f4811
Create Date: 2025-12-04 10:37:46.891276

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = '0f76479f4ec9'
down_revision: str | None = '315b233f4811'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 表配置
SCHEMA = "usr_ai"
TABLE_FACTORIES = "t_ai_llm_factories"
TABLE_LLMS = "t_ai_llms"


def _get_dialect_and_inspector():
    """获取数据库方言和检查器"""
    bind = op.get_bind()
    return bind.dialect.name, Inspector.from_engine(bind)


def _table_exists(insp: Inspector, table_name: str) -> bool:
    """检查表是否存在"""
    return table_name in insp.get_table_names(schema=SCHEMA)


def _get_pk_columns(insp: Inspector, table_name: str) -> set:
    """获取表的主键列"""
    pk_constraint = insp.get_pk_constraint(table_name, schema=SCHEMA)
    return set(pk_constraint.get("constrained_columns", []))


def _clean_duplicate_factories(dialect_name: str) -> None:
    """清理 t_ai_llm_factories 表的重复数据"""
    if dialect_name == "postgresql":
        op.execute(sa.text("""
            DELETE FROM usr_ai.t_ai_llm_factories
            WHERE ctid NOT IN (
                SELECT MIN(ctid) FROM usr_ai.t_ai_llm_factories GROUP BY name
            )
        """))
    elif dialect_name == "mysql":
        op.execute(sa.text("""
            DELETE t1 FROM usr_ai.t_ai_llm_factories t1
            INNER JOIN usr_ai.t_ai_llm_factories t2
            WHERE t1.name = t2.name AND t1.create_time > t2.create_time
        """))


def _clean_duplicate_llms(dialect_name: str) -> None:
    """清理 t_ai_llms 表的重复数据"""
    if dialect_name == "postgresql":
        op.execute(sa.text("""
            DELETE FROM usr_ai.t_ai_llms
            WHERE ctid NOT IN (
                SELECT MIN(ctid) FROM usr_ai.t_ai_llms GROUP BY fid, llm_name
            )
        """))
    elif dialect_name == "mysql":
        op.execute(sa.text("""
            DELETE t1 FROM usr_ai.t_ai_llms t1
            INNER JOIN usr_ai.t_ai_llms t2
            WHERE t1.fid = t2.fid AND t1.llm_name = t2.llm_name
            AND t1.create_time > t2.create_time
        """))


def upgrade() -> None:
    """Upgrade schema."""
    dialect_name, insp = _get_dialect_and_inspector()

    # ===== 1. 清理 t_ai_llm_factories 表的重复数据 =====
    if _table_exists(insp, TABLE_FACTORIES):
        _clean_duplicate_factories(dialect_name)

    # ===== 2. 清理 t_ai_llms 表的重复数据 =====
    if _table_exists(insp, TABLE_LLMS):
        _clean_duplicate_llms(dialect_name)

        # ===== 3. 修改 t_ai_llms 表的主键为 (fid, llm_name) =====
        current_pk = _get_pk_columns(insp, TABLE_LLMS)
        target_pk = {"fid", "llm_name"}

        if current_pk != target_pk:
            # 使用 Alembic 的 op 函数修改主键
            op.drop_constraint(
                constraint_name="t_ai_llms_pkey",
                table_name=TABLE_LLMS,
                schema=SCHEMA,
                type_="primary"
            )
            op.create_primary_key(
                constraint_name="t_ai_llms_pkey",
                table_name=TABLE_LLMS,
                columns=["fid", "llm_name"],
                schema=SCHEMA
            )


def downgrade() -> None:
    """Downgrade schema."""
    dialect_name, insp = _get_dialect_and_inspector()

    # ===== 回滚 t_ai_llms 表的主键为 llm_name =====
    if _table_exists(insp, TABLE_LLMS):
        current_pk = _get_pk_columns(insp, TABLE_LLMS)

        if current_pk == {"fid", "llm_name"}:
            op.drop_constraint(
                constraint_name="t_ai_llms_pkey",
                table_name=TABLE_LLMS,
                schema=SCHEMA,
                type_="primary"
            )
            op.create_primary_key(
                constraint_name="t_ai_llms_pkey",
                table_name=TABLE_LLMS,
                columns=["llm_name"],
                schema=SCHEMA
            )
