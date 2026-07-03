"""add columns

Revision ID: ed48e0b671e8
Revises: 419b906f1434
Create Date: 2025-09-23 16:43:16.900736

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Inspector, Text

# revision identifiers, used by Alembic.
revision: str = "ed48e0b671e8"
down_revision: str | None = "419b906f1434"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 获取数据库类型
    dialect_name = bind.dialect.name

    # 检查 t_ai_dialogs 表是否已有 meta_data_filter 列
    cols = [col["name"] for col in insp.get_columns("t_ai_dialogs", schema="usr_ai")]

    if "meta_data_filter" not in cols:
        # 根据数据库类型选择合适的 JSON 类型
        if dialect_name == "postgresql":
            json_type = sa.dialects.postgresql.JSONB
        elif dialect_name == "mysql":
            json_type = sa.JSON
        elif dialect_name == "sqlite":
            # SQLite 没有原生 JSON 类型，使用 TEXT
            json_type = Text
        elif dialect_name == "oracle":
            # Oracle 12c+ 支持 JSON 检查但存储为 CLOB
            json_type = sa.CLOB
        elif dialect_name == "mssql":
            # SQL Server 2016+ 支持 JSON
            json_type = sa.Text
        else:
            # 默认使用 SQLAlchemy 的 JSON 类型
            json_type = sa.JSON

        with op.batch_alter_table("t_ai_dialogs", schema="usr_ai") as batch_op:
            # 为不同数据库提供合适的默认值文本
            if dialect_name == "postgresql":
                default_clause = sa.text("'{}'::jsonb")
            else:
                default_clause = sa.text("'{}'")

            batch_op.add_column(sa.Column("meta_data_filter", json_type, nullable=True, server_default=default_clause))


def downgrade() -> None:
    # 回滚时删除 meta_data_filter 列
    with op.batch_alter_table("t_ai_dialogs", schema="usr_ai") as batch_op:
        batch_op.drop_column("meta_data_filter")
