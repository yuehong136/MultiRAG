"""Add bounded Agent tags with an empty default for existing canvases."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a3c5e7b9d0"
down_revision: str | None = "e7a9b1c3d5f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
TABLE = "t_ai_user_canvases"
INDEX = "ix_usr_ai_t_ai_user_canvases_tags"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "tags" not in {column["name"] for column in inspector.get_columns(TABLE, schema=SCHEMA)}:
        op.add_column(TABLE, sa.Column("tags", sa.String(512), nullable=False, server_default=sa.text("''")), schema=SCHEMA)
    if INDEX not in {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(TABLE, schema=SCHEMA)}:
        op.create_index(INDEX, TABLE, ["tags"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE, schema=SCHEMA)
    op.drop_column(TABLE, "tags", schema=SCHEMA)
