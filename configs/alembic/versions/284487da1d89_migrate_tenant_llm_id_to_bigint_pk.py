"""migrate tenant_llm id to bigint pk

Revision ID: 284487da1d89
Revises: aae5b5f0d916
Create Date: 2026-03-23 16:03:55.917868

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = "284487da1d89"
down_revision: str | None = "aae5b5f0d916"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
TABLE = "t_ai_tenant_llms"
BUSINESS_KEY = ("tenant_id", "llm_factory", "llm_name")
BUSINESS_KEY_INDEX = "idx_tenant_llm_unique"


def _get_bind_and_inspector():
    bind = op.get_bind()
    return bind, bind.dialect.name, Inspector.from_engine(bind)


def _table_exists(insp: Inspector) -> bool:
    return TABLE in insp.get_table_names(schema=SCHEMA)


def _get_columns(insp: Inspector) -> dict[str, dict]:
    return {column["name"]: column for column in insp.get_columns(TABLE, schema=SCHEMA)}


def _get_pk(insp: Inspector) -> tuple[str | None, tuple[str, ...]]:
    pk = insp.get_pk_constraint(TABLE, schema=SCHEMA) or {}
    return pk.get("name"), tuple(pk.get("constrained_columns") or ())


def _has_business_key_index(insp: Inspector) -> bool:
    indexes = insp.get_indexes(TABLE, schema=SCHEMA)
    return any(index.get("name") == BUSINESS_KEY_INDEX for index in indexes)


def _drop_pk_if_present(pk_name: str | None) -> None:
    if pk_name:
        op.drop_constraint(pk_name, TABLE, schema=SCHEMA, type_="primary")


def _create_business_key_index_if_missing(insp: Inspector) -> None:
    if not _has_business_key_index(insp):
        op.create_index(
            BUSINESS_KEY_INDEX,
            TABLE,
            list(BUSINESS_KEY),
            schema=SCHEMA,
            unique=True,
        )


def _drop_business_key_index_if_present(insp: Inspector) -> None:
    if _has_business_key_index(insp):
        op.drop_index(BUSINESS_KEY_INDEX, table_name=TABLE, schema=SCHEMA)


def _is_varchar_id(column: dict | None) -> bool:
    if not column:
        return False
    return "char" in str(column.get("type", "")).lower()


def _is_integer_id(column: dict | None) -> bool:
    if not column:
        return False
    type_name = str(column.get("type", "")).lower()
    return "bigint" in type_name or "integer" in type_name or "int" in type_name


def upgrade() -> None:
    """Migrate the legacy tenant_llm table to bigint id primary key."""
    bind, dialect_name, insp = _get_bind_and_inspector()
    if not _table_exists(insp):
        return

    columns = _get_columns(insp)
    pk_name, pk_columns = _get_pk(insp)
    id_column = columns.get("id")

    # Local environments that were already migrated by the Go service should not fail here.
    # Treat the target schema as a no-op and only ensure the business-key unique index exists.
    if pk_columns == ("id",) and _is_integer_id(id_column):
        _create_business_key_index_if_missing(insp)
        return

    if pk_columns != BUSINESS_KEY or not _is_varchar_id(id_column):
        raise RuntimeError(
            f"Unexpected {TABLE} schema before migration: pk={pk_columns}, id_type={id_column.get('type') if id_column else None}. This migration expects the legacy Python-only schema."
        )

    _drop_pk_if_present(pk_name)
    op.drop_column(TABLE, "id", schema=SCHEMA)

    if dialect_name == "postgresql":
        op.execute(
            sa.text(f"""
            ALTER TABLE {SCHEMA}.{TABLE}
            ADD COLUMN id BIGSERIAL PRIMARY KEY
        """)
        )
    elif dialect_name == "mysql":
        op.execute(
            sa.text(f"""
            ALTER TABLE {SCHEMA}.{TABLE}
            ADD COLUMN id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY FIRST
        """)
        )
    else:
        raise RuntimeError(f"Unsupported dialect for migration: {dialect_name}")

    _create_business_key_index_if_missing(Inspector.from_engine(bind))


def downgrade() -> None:
    """Restore the legacy composite primary-key layout."""
    bind, dialect_name, insp = _get_bind_and_inspector()
    if not _table_exists(insp):
        return

    columns = _get_columns(insp)
    pk_name, pk_columns = _get_pk(insp)
    id_column = columns.get("id")

    if pk_columns != ("id",) or not _is_integer_id(id_column):
        raise RuntimeError(f"Unexpected {TABLE} schema before downgrade: pk={pk_columns}, id_type={id_column.get('type') if id_column else None}.")

    _drop_business_key_index_if_present(insp)
    _drop_pk_if_present(pk_name)
    op.drop_column(TABLE, "id", schema=SCHEMA)

    op.add_column(TABLE, sa.Column("id", sa.String(length=128), nullable=True), schema=SCHEMA)

    if dialect_name == "postgresql":
        op.execute(
            sa.text(f"""
            UPDATE {SCHEMA}.{TABLE}
            SET id = md5(random()::text || clock_timestamp()::text || tenant_id || llm_factory || coalesce(llm_name, ''))
            WHERE id IS NULL
        """)
        )
    elif dialect_name == "mysql":
        op.execute(
            sa.text(f"""
            UPDATE {SCHEMA}.{TABLE}
            SET id = REPLACE(UUID(), '-', '')
            WHERE id IS NULL
        """)
        )
    else:
        raise RuntimeError(f"Unsupported dialect for migration: {dialect_name}")

    with op.batch_alter_table(TABLE, schema=SCHEMA) as batch_op:
        batch_op.alter_column("id", existing_type=sa.String(length=128), nullable=False)

    op.create_primary_key(f"{TABLE}_pkey", TABLE, list(BUSINESS_KEY), schema=SCHEMA)
    op.create_index(f"ix_{SCHEMA}_{TABLE}_id", TABLE, ["id"], schema=SCHEMA, unique=False)
