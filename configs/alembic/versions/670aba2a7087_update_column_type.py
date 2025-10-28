"""update column type

Revision ID: 670aba2a7087
Revises: ed48e0b671e8
Create Date: 2025-10-20 11:06:41.926684

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Inspector, String, Text


# revision identifiers, used by Alembic.
revision: str = '670aba2a7087'
down_revision: Union[str, None] = 'ed48e0b671e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ===== 按需修改：表名与 schema =====
TABLE_NAME = "t_ai_canvas_templates"
SCHEMA = "usr_ai"
# =====================================

def _json_type_for(dialect_name: str):
    """选择合适的 JSON 类型（示例代码风格保留多方言处理）"""
    if dialect_name == 'postgresql':
        return sa.dialects.postgresql.JSONB
    elif dialect_name == 'mysql':
        return sa.JSON
    elif dialect_name == 'sqlite':
        # sqlite 无原生 JSON 类型，通常以 TEXT 存储
        return sa.Text
    elif dialect_name == 'oracle':
        return sa.CLOB
    elif dialect_name == 'mssql':
        # SQL Server 以 NVARCHAR/TEXT 形式存 JSON 字符串
        return sa.Text
    return sa.JSON


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    dialect_name = bind.dialect.name
    json_type = _json_type_for(dialect_name)

    # 读取现有列
    cols = [c["name"] for c in insp.get_columns(TABLE_NAME, schema=SCHEMA)]

    # 按示例风格，用 batch_alter_table 操作
    with op.batch_alter_table(TABLE_NAME, schema=SCHEMA) as batch:
        # 如果列存在，则删除后用 JSON 类型重建（无数据时最简单稳妥）
        if "title" in cols:
            batch.drop_column("title")
        batch.add_column(
            sa.Column(
                "title",
                json_type,
                nullable=True,
                comment="Canvas title (JSON)"
            )
        )

        if "description" in cols:
            batch.drop_column("description")
        batch.add_column(
            sa.Column(
                "description",
                json_type,
                nullable=True,
                comment="Canvas description (JSON)"
            )
        )

    # 说明：默认值建议仍在 ORM 层使用 default=lambda: {}（而非 DB 层默认）


def downgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    # 还原为原先的 String(255)/Text
    with op.batch_alter_table(TABLE_NAME, schema=SCHEMA) as batch:
        # 先删 JSON 列再重建旧类型
        batch.drop_column("title")
        batch.add_column(sa.Column("title", String(length=255), nullable=True, comment="Canvas title"))

        batch.drop_column("description")
        batch.add_column(sa.Column("description", Text(), nullable=True, comment="Canvas description"))
