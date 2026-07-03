"""add columns

Revision ID: 6d37ed11b4a3
Revises: 0f76479f4ec9
Create Date: 2025-12-19 14:02:48.576299

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = '6d37ed11b4a3'
down_revision: str | None = '0f76479f4ec9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查 t_ai_tenant_llms 表是否已有 status 列
    cols = [col["name"] for col in insp.get_columns("t_ai_tenant_llms", schema="usr_ai")]

    if "status" not in cols:
        with op.batch_alter_table("t_ai_tenant_llms", schema="usr_ai") as batch_op:
            # 添加 status 列并给已有数据一个默认值
            batch_op.add_column(
                sa.Column(
                    "status",
                    sa.String(length=1),
                    nullable=False,
                    index=True,
                    server_default="1",
                    doc="is it validate(0: wasted, 1: validate)"
                )
            )


def downgrade() -> None:
    # 回滚时删除 status 列
    with op.batch_alter_table("t_ai_tenant_llms", schema="usr_ai") as batch_op:
        batch_op.drop_column("status")
