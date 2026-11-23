"""Add durable writer admission and minimal memory deletion authorization."""

import importlib.util
from pathlib import Path
from typing import cast

import sqlalchemy as sa
from alembic import op

revision = "f2b4d6e8a0c2"
down_revision = "f0a2c4e6b8d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from api.db.memory_writer_models import MEMORY_WRITER_MODELS

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns("t_ai_memory_managed_states", schema="usr_ai")}
    for name, length in (("delete_permissions", 16), ("delete_operation_id", 128)):
        if name not in columns:
            op.add_column("t_ai_memory_managed_states", sa.Column(name, sa.String(length), nullable=True), schema="usr_ai")
        elif columns[name]["type"].compile(dialect=bind.dialect) != sa.String(length).compile(dialect=bind.dialect) or not columns[name]["nullable"]:
            raise RuntimeError("Existing memory deletion authorization column is incompatible")
    spec = importlib.util.spec_from_file_location("memory_writer_constraints", Path(__file__).with_name("c5e7f9a1b3d5_add_skill_assets.py"))
    assert spec and spec.loader
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    for model in MEMORY_WRITER_MODELS:
        table = cast(sa.Table, model.__table__)
        inspector = sa.inspect(bind)
        if not inspector.has_table(table.name, schema="usr_ai"):
            table.create(bind)
        else:
            actual = {column["name"]: column for column in inspector.get_columns(table.name, schema="usr_ai")}
            if set(actual) != set(table.columns.keys()):
                raise RuntimeError("Existing memory writer table has incompatible columns")
            for column in table.columns:
                stored = actual[column.name]
                if stored["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or stored["nullable"] != column.nullable:
                    raise RuntimeError("Existing memory writer table has incompatible column types")
        validator.validate_constraints(bind, table)


def downgrade() -> None:
    from api.db.memory_writer_models import MEMORY_WRITER_MODELS

    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM usr_ai.t_ai_memory_managed_states WHERE delete_operation_id IS NOT NULL)")):
        raise RuntimeError("Cannot discard memory deletion authorization tombstones")
    for model in MEMORY_WRITER_MODELS:
        if bind.scalar(sa.select(sa.exists().select_from(model.__table__))):
            raise RuntimeError("Cannot discard persisted memory writer ownership")
    for reversed_model in reversed(MEMORY_WRITER_MODELS):
        op.drop_table(reversed_model.__tablename__, schema="usr_ai")
    op.drop_column("t_ai_memory_managed_states", "delete_operation_id", schema="usr_ai")
    op.drop_column("t_ai_memory_managed_states", "delete_permissions", schema="usr_ai")
