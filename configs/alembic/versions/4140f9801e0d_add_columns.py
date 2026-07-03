"""add columns

Revision ID: 4140f9801e0d
Revises: 9932c0b1dcc8
Create Date: 2025-12-29 14:55:34.791938

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Inspector

# revision identifiers, used by Alembic.
revision: str = '4140f9801e0d'
down_revision: str | None = '9932c0b1dcc8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查 t_ai_llm_factories 表是否已有 rank 列
    cols = [col["name"] for col in insp.get_columns("t_ai_llm_factories", schema="usr_ai")]
    if "rank" not in cols:
        with op.batch_alter_table("t_ai_llm_factories", schema="usr_ai") as batch_op:
            # 添加 rank 列，用于 LLM 厂商排序，默认值为 0
            batch_op.add_column(
                sa.Column(
                    "rank",
                    sa.Integer(),
                    nullable=False,
                    server_default="0"
                )
            )


def downgrade() -> None:
    """Downgrade schema."""
    # 回滚时删除 rank 列
    with op.batch_alter_table("t_ai_llm_factories", schema="usr_ai") as batch_op:
        batch_op.drop_column("rank")
