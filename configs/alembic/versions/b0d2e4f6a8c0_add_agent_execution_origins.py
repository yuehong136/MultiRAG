"""Add immutable Web Agent session execution provenance.

Revision ID: b0d2e4f6a8c0
Revises: a9c810f1d2e3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b0d2e4f6a8c0"
down_revision: str | None = "a9c810f1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from api.db.db_models import AgentExecutionOrigin

    # Bootstrap may have already created the exact model table.
    table = AgentExecutionOrigin.__table__
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(table.name, schema=table.schema):
        columns = {column["name"]: column for column in inspector.get_columns(table.name, schema=table.schema)}
        if set(columns) != set(table.columns.keys()) or inspector.get_pk_constraint(table.name, schema=table.schema)["constrained_columns"] != ["id"]:
            raise RuntimeError("Existing Agent execution origin table has incompatible columns.")
        for column in table.columns:
            actual = columns[column.name]
            if actual["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or actual["nullable"] != column.nullable:
                raise RuntimeError("Existing Agent execution origin table has incompatible column types.")
        foreign_keys = inspector.get_foreign_keys(table.name, schema=table.schema)
        if not any(
            key["constrained_columns"] == ["id"]
            and key["referred_schema"] == "usr_ai"
            and key["referred_table"] == "t_ai_api4conversations"
            and key["referred_columns"] == ["id"]
            and key.get("options", {}).get("ondelete") == "CASCADE"
            for key in foreign_keys
        ):
            raise RuntimeError("Existing Agent execution origin table has incompatible session binding.")
        checks = {check["name"] for check in inspector.get_check_constraints(table.name, schema=table.schema)}
        if not {"ck_agent_execution_origin_mode", "ck_agent_execution_origin_revision"} <= checks:
            raise RuntimeError("Existing Agent execution origin table has incompatible constraints.")
        return
    table.create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE usr_ai.t_ai_agent_execution_origins IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM usr_ai.t_ai_agent_execution_origins)")):
        raise RuntimeError("Cannot discard Agent session execution provenance.")
    op.drop_table("t_ai_agent_execution_origins", schema="usr_ai")
