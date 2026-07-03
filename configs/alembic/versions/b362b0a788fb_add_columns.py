"""add columns

Revision ID: b362b0a788fb
Revises: 39957f66d1e6
Create Date: 2025-09-23 10:38:29.950368

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = 'b362b0a788fb'
down_revision: str | None = '39957f66d1e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    cols = [col["name"] for col in insp.get_columns("t_ai_ask_data_history", schema="usr_ai")]

    with op.batch_alter_table("t_ai_ask_data_history", schema="usr_ai") as batch_op:
        if "user_question" not in cols:
            batch_op.add_column(
                sa.Column(
                    "user_question",
                    sa.Text(),
                    nullable=False,
                    server_default=sa.text("''"),
                    doc="用户问题"
                )
            )

        # 添加 round_id 列
        if "round_id" not in cols:
            batch_op.add_column(
                sa.Column(
                    "round_id",
                    sa.String(length=32),
                    nullable=False,
                    index=True,
                    server_default=sa.text("''"),
                    doc="用于标识对话轮次的唯一标识符"
                )
            )

        if "processed_semantic_layer" not in cols:
            batch_op.add_column(
                sa.Column(
                    "processed_semantic_layer",
                    sa.Text(),
                    nullable=True,
                    doc="该问题构建的语义层"
                )
            )

        if "sql_info" not in cols:
            batch_op.add_column(
                sa.Column(
                    "sql_info",
                    sa.Text(),
                    nullable=True,
                    doc="生成的SQL及执行SQL的结果还有其他信息"
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    cols = [col["name"] for col in insp.get_columns("t_ai_ask_data_history", schema="usr_ai")]

    with op.batch_alter_table("t_ai_ask_data_history", schema="usr_ai") as batch_op:
        # 按相反顺序删除列（最后添加的先删除）
        if "sql_info" in cols:
            batch_op.drop_column("sql_info")

        if "processed_semantic_layer" in cols:
            batch_op.drop_column("processed_semantic_layer")

        if "round_id" in cols:
            batch_op.drop_column("round_id")

        if "user_question" in cols:
            batch_op.drop_column("user_question")
