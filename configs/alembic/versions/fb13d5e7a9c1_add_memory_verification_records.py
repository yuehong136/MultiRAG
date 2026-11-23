"""Add independent version-bound human verification records and audit handles."""

import importlib.util
from pathlib import Path
from typing import cast

import sqlalchemy as sa
from alembic import op

revision = "fb13d5e7a9c1"
down_revision = "fa02c4e6b8d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from api.db.memory_verification_models import MEMORY_VERIFICATION_MODELS

    bind = op.get_bind()
    spec = importlib.util.spec_from_file_location("memory_verification_constraints", Path(__file__).with_name("c5e7f9a1b3d5_add_skill_assets.py"))
    assert spec and spec.loader
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    for model in MEMORY_VERIFICATION_MODELS:
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
                raise RuntimeError("Existing memory verification table is incompatible")
        validator.validate_constraints(bind, table)


def downgrade() -> None:
    from api.db.memory_verification_models import MEMORY_VERIFICATION_MODELS

    bind = op.get_bind()
    if any(bind.scalar(sa.select(sa.exists().select_from(model.__table__))) for model in MEMORY_VERIFICATION_MODELS):
        raise RuntimeError("Cannot discard persistent human verification records")
    for model in reversed(MEMORY_VERIFICATION_MODELS):
        op.drop_table(model.__tablename__, schema="usr_ai")
