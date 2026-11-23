"""Add managed memory revision and publication state without changing messages."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "f0a2c4e6b8d0"
down_revision = "c9e1f3a5b7d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from api.db.memory_revision_models import MEMORY_REVISION_MODELS

    bind = op.get_bind()
    spec = importlib.util.spec_from_file_location("memory_revision_constraints", Path(__file__).with_name("c5e7f9a1b3d5_add_skill_assets.py"))
    assert spec and spec.loader
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    for model in MEMORY_REVISION_MODELS:
        table = model.__table__
        inspector = sa.inspect(bind)
        if inspector.has_table(table.name, schema="usr_ai"):
            columns = {column["name"]: column for column in inspector.get_columns(table.name, schema="usr_ai")}
            if set(columns) != set(table.columns.keys()):
                raise RuntimeError("Existing memory revision table has incompatible columns")
            for column in table.columns:
                actual = columns[column.name]
                if actual["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or actual["nullable"] != column.nullable:
                    raise RuntimeError("Existing memory revision table has incompatible column types")
        else:
            table.create(bind)
        validator.validate_constraints(bind, table)


def downgrade() -> None:
    from api.db.memory_revision_models import MEMORY_REVISION_MODELS

    bind = op.get_bind()
    for model in MEMORY_REVISION_MODELS:
        if bind.scalar(sa.select(sa.exists().select_from(model.__table__))):
            raise RuntimeError("Cannot discard persisted memory revision or tombstone state")
    for model in reversed(MEMORY_REVISION_MODELS):
        op.drop_table(model.__tablename__, schema="usr_ai")
