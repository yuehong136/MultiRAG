"""Track generated table metadata independently of user metadata.

Revision ID: e7a9b1c3d5f0
Revises: d6f8a0b2c4e6
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "e7a9b1c3d5f0"
down_revision: str | None = "d6f8a0b2c4e6"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"]: column for column in sa.inspect(bind).get_columns("t_ai_document_metadata", schema="usr_ai")}
    if "table_metadata_state" not in columns:
        op.add_column("t_ai_document_metadata", sa.Column("table_metadata_state", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")), schema="usr_ai")
    elif not isinstance(columns["table_metadata_state"]["type"], JSONB) or columns["table_metadata_state"]["nullable"]:
        raise RuntimeError("Existing table metadata ownership state has an incompatible schema.")


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE usr_ai.t_ai_document_metadata IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM usr_ai.t_ai_document_metadata WHERE table_metadata_state <> '{}'::jsonb)")):
        raise RuntimeError("Cannot discard active table metadata ownership state.")
    op.drop_column("t_ai_document_metadata", "table_metadata_state", schema="usr_ai")
