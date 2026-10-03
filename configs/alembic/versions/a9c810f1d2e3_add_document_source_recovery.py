"""Add durable source recovery authority.

Revision ID: a9c810f1d2e3
Revises: e1f3a5c7b9d0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c810f1d2e3"
down_revision: str | None = "e1f3a5c7b9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from api.db.db_models import SourceRecoveryRecord

    table = SourceRecoveryRecord.__table__
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(table.name, schema=table.schema):
        columns = {column["name"]: column for column in inspector.get_columns(table.name, schema=table.schema)}
        primary_key = inspector.get_pk_constraint(table.name, schema=table.schema)["constrained_columns"]
        unique = [frozenset(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table.name, schema=table.schema)]
        indexes = inspector.get_indexes(table.name, schema=table.schema)
        pair = frozenset({"document_id", "task_id"})
        if set(columns) != set(table.columns.keys()) or primary_key != ["id"]:
            raise RuntimeError("Existing source recovery table has incompatible columns.")
        # A complete plain pair index enforces the same rows as a constraint.
        # Partial indexes and larger keys cannot guarantee every exact pair.
        if pair not in unique and not any(index["unique"] and frozenset(index["column_names"]) == pair and index.get("dialect_options", {}).get("postgresql_where") is None for index in indexes):
            raise RuntimeError("Existing source recovery table has incompatible uniqueness.")
        unique.extend(frozenset(index["column_names"]) for index in indexes if index["unique"])
        # Names and column order do not change uniqueness. A superset of an
        # existing model key is redundant; any other unique key may reject
        # rows the model permits, including another Task for the same Doc.
        if any("id" not in key and not pair <= key for key in unique):
            raise RuntimeError("Existing source recovery table has incompatible uniqueness.")
        for column in table.columns:
            actual = columns[column.name]
            if actual["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or actual["nullable"] != column.nullable:
                raise RuntimeError("Existing source recovery table has incompatible column types.")
        return
    table.create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE usr_ai.t_document_source_recovery IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM usr_ai.t_document_source_recovery)")):
        raise RuntimeError("Cannot downgrade while source recovery material exists.")
    op.drop_table("t_document_source_recovery", schema="usr_ai")
