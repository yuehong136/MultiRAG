"""add columns

Revision ID: 358db7ccaaa9
Revises: 7117497a9ae0
Create Date: 2025-05-21 11:48:48.491041

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = '358db7ccaaa9'
down_revision: str | None = '7117497a9ae0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查 t_ai_llms 表是否已有 is_tools 列
    cols = [col["name"] for col in insp.get_columns("t_ai_llms", schema="usr_ai")]
    if "is_tools" not in cols:
        with op.batch_alter_table("t_ai_llms", schema="usr_ai") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "is_tools",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text('false'),
                    doc="support tools (True/False)"
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("t_ai_llms", schema="usr_ai") as batch_op:
        batch_op.drop_column("is_tools")
