"""add columns

Revision ID: 7117497a9ae0
Revises: 2c6a1888784e
Create Date: 2025-05-20 14:17:20.435224

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Inspector, Text

# revision identifiers, used by Alembic.
revision: str = "7117497a9ae0"
down_revision: str | None = "2c6a1888784e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 获取数据库类型
    dialect_name = bind.dialect.name

    # 检查 t_ai_dialogs 表是否已有 search_mode 列
    cols = [col["name"] for col in insp.get_columns("t_ai_dialogs", schema="usr_ai")]
    if "search_mode" not in cols:
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

        # 添加列
        with op.batch_alter_table("t_ai_dialogs", schema="usr_ai") as batch_op:
            batch_op.add_column(sa.Column("search_mode", json_type, nullable=True, comment="search mode configuration: hybrid, sparse, dense, or fusion"))

    # 注意：不再使用特定数据库的 SQL 更新语句
    # 设置默认值的工作可以在应用层完成


def downgrade() -> None:
    # 回滚时删除 search_mode 列
    with op.batch_alter_table("t_ai_dialogs", schema="usr_ai") as batch_op:
        batch_op.drop_column("search_mode")
