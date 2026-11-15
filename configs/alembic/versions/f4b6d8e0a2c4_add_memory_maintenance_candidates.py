"""Add isolated maintenance jobs, inputs, and human-reviewed candidates."""

import importlib.util
from pathlib import Path
from typing import cast

import sqlalchemy as sa
from alembic import op

revision = "f4b6d8e0a2c4"
down_revision = "f2b4d6e8a0c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from api.db.memory_maintenance_models import MEMORY_MAINTENANCE_MODELS

    bind = op.get_bind()
    spec = importlib.util.spec_from_file_location("memory_maintenance_constraints", Path(__file__).with_name("c5e7f9a1b3d5_add_skill_assets.py"))
    assert spec and spec.loader
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    for model in MEMORY_MAINTENANCE_MODELS:
        table = cast(sa.Table, model.__table__)
        inspector = sa.inspect(bind)
        if not inspector.has_table(table.name, schema="usr_ai"):
            table.create(bind)
        else:
            columns = {column["name"]: column for column in inspector.get_columns(table.name, schema="usr_ai")}
            if set(columns) != set(table.columns.keys()):
                raise RuntimeError("Existing memory maintenance table has incompatible columns")
            for column in table.columns:
                if columns[column.name]["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or columns[column.name]["nullable"] != column.nullable:
                    raise RuntimeError("Existing memory maintenance table has incompatible column types")
        validator.validate_constraints(bind, table)


def downgrade() -> None:
    from api.db.memory_maintenance_models import MEMORY_MAINTENANCE_MODELS

    bind = op.get_bind()
    for model in MEMORY_MAINTENANCE_MODELS:
        if bind.scalar(sa.select(sa.exists().select_from(model.__table__))):
            raise RuntimeError("Cannot discard persisted memory maintenance review history")
    for reversed_model in reversed(MEMORY_MAINTENANCE_MODELS):
        op.drop_table(reversed_model.__tablename__, schema="usr_ai")
