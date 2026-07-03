"""add columns

Revision ID: 39957f66d1e6
Revises: e2694b092c03
Create Date: 2025-07-17 09:32:58.430554

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = '39957f66d1e6'
down_revision: str | None = 'e2694b092c03'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查 t_ai_documents 表是否已有 user_id 列
    cols = [col["name"] for col in insp.get_columns("t_ai_documents", schema="usr_ai")]
    if "suffix" not in cols:
        with op.batch_alter_table("t_ai_documents", schema="usr_ai") as batch_op:
            # 添加列并给已有数据一个默认值
            batch_op.add_column(
                sa.Column(
                    "suffix",
                    sa.String(length=32),
                    nullable=False,
                    index=True,
                    server_default="",
                    doc="The real file extension suffix"
                )
            )


def downgrade() -> None:
    # 回滚时删除 suffix 列
    with op.batch_alter_table("t_ai_documents", schema="usr_ai") as batch_op:
        batch_op.drop_column("suffix")
