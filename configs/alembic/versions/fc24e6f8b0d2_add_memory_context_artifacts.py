"""Add independently recoverable context artifacts and ordered RAW roots."""

import importlib.util
from pathlib import Path
from typing import cast

import sqlalchemy as sa
from alembic import op

revision = "fc24e6f8b0d2"
down_revision = "fb13d5e7a9c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from api.db.memory_compaction_models import MEMORY_COMPACTION_MODELS

    bind = op.get_bind()
    spec = importlib.util.spec_from_file_location("memory_compaction_constraints", Path(__file__).with_name("c5e7f9a1b3d5_add_skill_assets.py"))
    assert spec and spec.loader
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    for model in MEMORY_COMPACTION_MODELS:
        table = cast(sa.Table, model.__table__)
        inspector = sa.inspect(bind)
        if not inspector.has_table(table.name, schema="usr_ai"):
            table.create(bind)
        else:
            columns = {column["name"]: column for column in inspector.get_columns(table.name, schema="usr_ai")}
            if set(columns) != set(table.columns.keys()) or any(
                columns[column.name]["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or columns[column.name]["nullable"] != column.nullable
                for column in table.columns
            ):
                raise RuntimeError("Existing memory compaction table is incompatible")
        validator.validate_constraints(bind, table)


def downgrade() -> None:
    from api.db.memory_compaction_models import MEMORY_COMPACTION_MODELS

    bind = op.get_bind()
    if any(bind.scalar(sa.select(sa.exists().select_from(model.__table__))) for model in MEMORY_COMPACTION_MODELS):
        raise RuntimeError("Cannot discard persistent context artifact audit records")
    for model in reversed(MEMORY_COMPACTION_MODELS):
        op.drop_table(model.__tablename__, schema="usr_ai")
