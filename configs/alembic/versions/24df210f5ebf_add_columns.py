"""add columns

Revision ID: 24df210f5ebf
Revises: efce62e1c3ac
Create Date: 2025-10-29 22:15:27.058927

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


# revision identifiers, used by Alembic.
revision: str = '24df210f5ebf'
down_revision: Union[str, None] = 'efce62e1c3ac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    # ===== 1. Add columns to t_ai_knowledgebases =====
    kb_cols = [col["name"] for col in insp.get_columns("t_ai_knowledgebases", schema="usr_ai")]

    with op.batch_alter_table("t_ai_knowledgebases", schema="usr_ai") as batch_op:
        if "pipeline_id" not in kb_cols:
            batch_op.add_column(
                sa.Column(
                    "pipeline_id",
                    sa.String(length=32),
                    nullable=True,
                    index=True,
                    doc="Pipeline ID"
                )
            )

        if "graphrag_task_id" not in kb_cols:
            batch_op.add_column(
                sa.Column(
                    "graphrag_task_id",
                    sa.String(length=32),
                    nullable=True,
                    index=True,
                    doc="Graph RAG task ID"
                )
            )

        if "graphrag_task_finish_at" not in kb_cols:
            batch_op.add_column(
                sa.Column(
                    "graphrag_task_finish_at",
                    sa.DateTime(),
                    nullable=True,
                    index=True
                )
            )

        if "raptor_task_id" not in kb_cols:
            batch_op.add_column(
                sa.Column(
                    "raptor_task_id",
                    sa.String(length=32),
                    nullable=True,
                    index=True,
                    doc="RAPTOR task ID"
                )
            )

        if "raptor_task_finish_at" not in kb_cols:
            batch_op.add_column(
                sa.Column(
                    "raptor_task_finish_at",
                    sa.DateTime(),
                    nullable=True,
                    index=True
                )
            )

        if "mindmap_task_id" not in kb_cols:
            batch_op.add_column(
                sa.Column(
                    "mindmap_task_id",
                    sa.String(length=32),
                    nullable=True,
                    index=True,
                    doc="Mindmap task ID"
                )
            )

        if "mindmap_task_finish_at" not in kb_cols:
            batch_op.add_column(
                sa.Column(
                    "mindmap_task_finish_at",
                    sa.DateTime(),
                    nullable=True,
                    index=True
                )
            )

    # ===== 2. Add columns to t_ai_documents =====
    doc_cols = [col["name"] for col in insp.get_columns("t_ai_documents", schema="usr_ai")]

    if "pipeline_id" not in doc_cols:
        with op.batch_alter_table("t_ai_documents", schema="usr_ai") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "pipeline_id",
                    sa.String(length=32),
                    nullable=True,
                    index=True,
                    doc="Pipeline ID"
                )
            )


def downgrade() -> None:
    """Downgrade schema."""
    # Remove columns from t_ai_documents
    with op.batch_alter_table("t_ai_documents", schema="usr_ai") as batch_op:
        batch_op.drop_column("pipeline_id")

    # Remove columns from t_ai_knowledgebases
    with op.batch_alter_table("t_ai_knowledgebases", schema="usr_ai") as batch_op:
        batch_op.drop_column("mindmap_task_finish_at")
        batch_op.drop_column("mindmap_task_id")
        batch_op.drop_column("raptor_task_finish_at")
        batch_op.drop_column("raptor_task_id")
        batch_op.drop_column("graphrag_task_finish_at")
        batch_op.drop_column("graphrag_task_id")
        batch_op.drop_column("pipeline_id")
