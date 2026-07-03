"""add colums

Revision ID: a08220dd999f
Revises: 60e7c845e76c
Create Date: 2025-04-17 10:27:47.094225

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Inspector

# revision identifiers, used by Alembic.
revision: str = 'a08220dd999f'
down_revision: str | None = '60e7c845e76c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查 t_ai_tasks 表是否已有 task_type 列
    cols = [col["name"] for col in insp.get_columns("t_ai_tasks", schema="usr_ai")]
    if "task_type" not in cols:
        with op.batch_alter_table("t_ai_tasks", schema="usr_ai") as batch_op:
            # 添加列并给已有数据一个默认值
            batch_op.add_column(
                sa.Column(
                    "task_type",
                    sa.String(length=32),
                    nullable=False,
                    server_default=""
                )
            )


def downgrade() -> None:
    # 回滚时删除 task_type 列
    with op.batch_alter_table("t_ai_tasks", schema="usr_ai") as batch_op:
        batch_op.drop_column("task_type")
