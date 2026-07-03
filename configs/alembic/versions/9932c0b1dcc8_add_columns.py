"""add columns

Revision ID: 9932c0b1dcc8
Revises: 6d37ed11b4a3
Create Date: 2025-12-26 18:03:19.100988

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Inspector

# revision identifiers, used by Alembic.
revision: str = "9932c0b1dcc8"
down_revision: str | None = "6d37ed11b4a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查 t_ai_connector2kb 表是否已有 auto_parse 列
    cols = [col["name"] for col in insp.get_columns("t_ai_connector2kb", schema="usr_ai")]
    if "auto_parse" not in cols:
        with op.batch_alter_table("t_ai_connector2kb", schema="usr_ai") as batch_op:
            # 添加 auto_parse 列，默认值为 "1"（启用自动解析）
            batch_op.add_column(sa.Column("auto_parse", sa.String(1), nullable=False, server_default="1"))


def downgrade() -> None:
    # 回滚时删除 auto_parse 列
    with op.batch_alter_table("t_ai_connector2kb", schema="usr_ai") as batch_op:
        batch_op.drop_column("auto_parse")
