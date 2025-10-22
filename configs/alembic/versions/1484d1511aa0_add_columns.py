"""add columns

Revision ID: 1484d1511aa0
Revises: 670aba2a7087
Create Date: 2025-10-22 10:33:44.301993

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = '1484d1511aa0'
down_revision: Union[str, None] = '670aba2a7087'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ===== 按需修改：schema 与表名 =====
SCHEMA = "usr_ai"
TBL_USER_CANVAS = "t_ai_user_canvases"
TBL_CANVAS_TEMPLATE = "t_ai_canvas_templates"
# ==================================

def _json_type_for(dialect_name: str):
    """选择合适的 JSON 类型（与上一版保持风格一致）"""
    if dialect_name == 'postgresql':
        return sa.dialects.postgresql.JSONB
    elif dialect_name == 'mysql':
        return sa.JSON
    elif dialect_name == 'sqlite':
        # sqlite 无原生 JSON，通常以 TEXT 存储
        return sa.Text
    elif dialect_name == 'oracle':
        return sa.CLOB
    elif dialect_name == 'mssql':
        # SQL Server 以 NVARCHAR/TEXT 形式存 JSON 字符串
        return sa.Text
    return sa.JSON


def _ensure_add_column(batch, cols_now, name, col):
    """仅当列不存在时添加，避免重复执行失败"""
    if name not in cols_now:
        batch.add_column(col)


def _has_index(insp: Inspector, table_name: str, index_name: str) -> bool:
    for ix in insp.get_indexes(table_name, schema=SCHEMA):
        if ix.get("name") == index_name:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # ===== t_ai_user_canvases：新增 avatar / permission / canvas_type / canvas_category / dsl =====
    user_cols = {c["name"] for c in insp.get_columns(TBL_USER_CANVAS, schema=SCHEMA)}
    with op.batch_alter_table(TBL_USER_CANVAS, schema=SCHEMA) as batch:
        _ensure_add_column(
            batch, user_cols, "canvas_category",
            sa.Column("canvas_category", sa.String(length=32), nullable=False, server_default="agent_canvas",
                     comment="Canvas category: agent_canvas|dataflow_canvas"),
        )

    # 创建索引（若不存在）
    # 命名与 SQLAlchemy 默认一致：ix_<schema>_<table>_<column>（在部分方言上 schema 可能不进名，这里显式命名）
    ix_cat = f"ix_{SCHEMA}_{TBL_USER_CANVAS}_canvas_category"

    if not _has_index(insp, TBL_USER_CANVAS, ix_cat):
        op.create_index(ix_cat, TBL_USER_CANVAS, ["canvas_category"], unique=False, schema=SCHEMA)

    # ===== t_ai_canvas_templates：新增 avatar / canvas_type / canvas_category / dsl =====
    tpl_cols = {c["name"] for c in insp.get_columns(TBL_CANVAS_TEMPLATE, schema=SCHEMA)}
    with op.batch_alter_table(TBL_CANVAS_TEMPLATE, schema=SCHEMA) as batch:
        _ensure_add_column(
            batch, tpl_cols, "canvas_category",
            sa.Column("canvas_category", sa.String(length=32), nullable=False, server_default="agent_canvas",
                     comment="Canvas category: agent_canvas|dataflow_canvas"),
        )

    # 索引
    ix_tpl_cat  = f"ix_{SCHEMA}_{TBL_CANVAS_TEMPLATE}_canvas_category"
    if not _has_index(insp, TBL_CANVAS_TEMPLATE, ix_tpl_cat):
        op.create_index(ix_tpl_cat, TBL_CANVAS_TEMPLATE, ["canvas_category"], unique=False, schema=SCHEMA)


def downgrade() -> None:
    with op.batch_alter_table(TBL_USER_CANVAS, schema=SCHEMA) as batch:
        batch.drop_column("canvas_category")
    with op.batch_alter_table(TBL_CANVAS_TEMPLATE, schema=SCHEMA) as batch:
        batch.drop_column("canvas_category")
