"""add columns

Revision ID: 2c6a1888784e
Revises: 428d4fdf6064
Create Date: 2025-05-07 16:08:24.746288

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Inspector

# revision identifiers, used by Alembic.
revision: str = '2c6a1888784e'
down_revision: str | None = '428d4fdf6064'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查 t_ai_tenant_llms 表是否已有 max_tokens 列
    cols = [col["name"] for col in insp.get_columns("t_ai_tenant_llms", schema="usr_ai")]
    if "max_tokens" not in cols:
        with op.batch_alter_table("t_ai_tenant_llms", schema="usr_ai") as batch_op:
            # 添加列并给已有数据一个默认值
            batch_op.add_column(
                sa.Column(
                    "max_tokens",
                    sa.Integer(),
                    nullable=False,
                    server_default="8192"
                )
            )


def downgrade() -> None:
    # 回滚时删除 task_type 列
    with op.batch_alter_table("t_ai_tenant_llms", schema="usr_ai") as batch_op:
        batch_op.drop_column("max_tokens")
