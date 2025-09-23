"""add columns

Revision ID: 419b906f1434
Revises: 39957f66d1e6
Create Date: 2025-09-03 11:16:25.654563

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = '419b906f1434'
down_revision: Union[str, None] = 'b362b0a788fb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 检查 t_ai_api_tokens 表是否已有 name 和 description 列
    cols = [col["name"] for col in insp.get_columns("t_ai_api_tokens", schema="usr_ai")]

    if "name" not in cols:
        with op.batch_alter_table("t_ai_api_tokens", schema="usr_ai") as batch_op:
            # 添加 name 列并给已有数据一个默认值
            batch_op.add_column(
                sa.Column(
                    "name",
                    sa.String(length=20),
                    nullable=False,
                    index=True,
                    server_default="",
                    doc="Token名称"
                )
            )

    if "description" not in cols:
        with op.batch_alter_table("t_ai_api_tokens", schema="usr_ai") as batch_op:
            # 添加 description 列
            batch_op.add_column(
                sa.Column(
                    "description",
                    sa.Text(),
                    nullable=True,
                    doc="Token描述"
                )
            )


def downgrade() -> None:
    # 回滚时删除 name 和 description 列
    with op.batch_alter_table("t_ai_api_tokens", schema="usr_ai") as batch_op:
        batch_op.drop_column("name")
        batch_op.drop_column("description")
