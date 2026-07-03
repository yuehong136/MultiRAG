"""add colums

Revision ID: 60e7c845e76c
Revises:
Create Date: 2025-04-15 09:49:04.105880

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Inspector

# revision identifiers, used by Alembic.
revision: str = '60e7c845e76c'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # 1) t_ai_knowledgebases 添加 pagerank 列
    cols_kb = [c["name"] for c in insp.get_columns("t_ai_knowledgebases", schema="usr_ai")]
    if "pagerank" not in cols_kb:
        with op.batch_alter_table("t_ai_knowledgebases", schema="usr_ai") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "pagerank",
                    sa.Integer(),
                    nullable=False,
                    server_default="0"
                )
            )

    # 2) t_ai_documents 添加 meta_fields 列
    cols_doc = [c["name"] for c in insp.get_columns("t_ai_documents", schema="usr_ai")]
    if "meta_fields" not in cols_doc:
        with op.batch_alter_table("t_ai_documents", schema="usr_ai") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "meta_fields",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{ }'::jsonb")
                )
            )


def downgrade() -> None:
    # 如果需要回滚，删除刚才加的列
    with op.batch_alter_table("t_ai_documents", schema="usr_ai") as batch_op:
        batch_op.drop_column("meta_fields")

    with op.batch_alter_table("t_ai_knowledgebases", schema="usr_ai") as batch_op:
        batch_op.drop_column("pagerank")
