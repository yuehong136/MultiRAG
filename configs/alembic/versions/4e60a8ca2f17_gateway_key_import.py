"""Index imported caller credentials and reserve their source identity across rotations."""

import sqlalchemy as sa
from alembic import op

revision: str = "4e60a8ca2f17"
down_revision: str | None = "3d5f7b9c1e2a"
branch_labels: str | None = None
depends_on: str | None = None
SCHEMA = "usr_ai"
TABLE = "t_ai_gateway_keys"
SPELLINGS = "t_ai_gateway_imported_credentials"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {col["name"] for col in inspector.get_columns(TABLE, schema=SCHEMA)}
    for name, kind in (("import_source", sa.String(128)), ("import_id", sa.String(128)), ("import_digest", sa.CHAR(64)), ("import_spec_hash", sa.CHAR(64))):
        if name not in columns:
            op.add_column(TABLE, sa.Column(name, kind, nullable=True), schema=SCHEMA)
    indexes = {idx["name"] for idx in inspector.get_indexes(TABLE, schema=SCHEMA)}
    if "uq_gateway_keys_import_source" not in indexes:
        op.create_index("uq_gateway_keys_import_source", TABLE, ["import_source", "import_id"], unique=True, schema=SCHEMA, postgresql_where=sa.text("import_source IS NOT NULL"))
    if not inspector.has_table(SPELLINGS, schema=SCHEMA):
        op.create_table(
            SPELLINGS,
            sa.Column("digest", sa.CHAR(64), primary_key=True),
            sa.Column("key_id", sa.String(32), sa.ForeignKey(f"{SCHEMA}.{TABLE}.id", ondelete="CASCADE"), nullable=False),
            sa.Column("allow_raw_authorization", sa.Boolean(), nullable=False, server_default=sa.false()),
            schema=SCHEMA,
        )


def downgrade() -> None:
    # The old gateway understands only native credentials. Never silently strand a live imported key.
    if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.{TABLE} WHERE import_digest IS NOT NULL)")):
        raise RuntimeError("Gateway key import history exists. Restore the paired pre-import backup to roll back.")
    op.drop_table(SPELLINGS, schema=SCHEMA)
    for column in ("import_source", "import_id", "import_digest", "import_spec_hash"):
        op.drop_column(TABLE, column, schema=SCHEMA)
