"""add columns

Revision ID: e2694b092c03
Revises: 358db7ccaaa9
Create Date: 2025-06-26 09:46:02.647897

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = 'e2694b092c03'
down_revision: Union[str, None] = '358db7ccaaa9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查 t_ai_conversations 表是否已有 user_id 列
    cols = [col["name"] for col in insp.get_columns("t_ai_conversations", schema="usr_ai")]
    if "user_id" not in cols:
        with op.batch_alter_table("t_ai_conversations", schema="usr_ai") as batch_op:
            # 添加列并给已有数据一个默认值
            batch_op.add_column(
                sa.Column(
                    "user_id",
                    sa.String(length=32),
                    nullable=True,
                )
            )


def downgrade() -> None:
    # 回滚时删除 user_id 列
    with op.batch_alter_table("t_ai_conversations", schema="usr_ai") as batch_op:
        batch_op.drop_column("user_id")
