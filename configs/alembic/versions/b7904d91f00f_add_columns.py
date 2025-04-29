"""add columns

Revision ID: b7904d91f00f
Revises: a08220dd999f
Create Date: 2025-04-22 09:17:14.501004

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Inspector

# revision identifiers, used by Alembic.
revision: str = 'b7904d91f00f'
down_revision: Union[str, None] = 'a08220dd999f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查 t_ai_tasks 表是否已有 priority 列
    cols = [col["name"] for col in insp.get_columns("t_ai_tasks", schema="usr_ai")]
    if "priority" not in cols:
        with op.batch_alter_table("t_ai_tasks", schema="usr_ai") as batch_op:
            # 添加 priority 列，并设置初始默认值 0
            batch_op.add_column(
                sa.Column(
                    "priority",
                    sa.Integer(),
                    nullable=False,
                    server_default="0"
                )
            )
            # 可选：移除 server_default，让默认值仅在添加时生效
            batch_op.alter_column("priority", server_default=None)


def downgrade() -> None:
    # 回滚时删除 priority 列
    with op.batch_alter_table("t_ai_tasks", schema="usr_ai") as batch_op:
        batch_op.drop_column("priority")
