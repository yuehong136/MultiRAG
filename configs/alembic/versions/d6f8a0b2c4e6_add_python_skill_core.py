"""Add Python-private directory Skills core without changing existing assets.

Revision ID: d6f8a0b2c4e6
Revises: c5e7f9a1b3d5
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "d6f8a0b2c4e6"
down_revision = "c5e7f9a1b3d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from api.db.db_models import PythonSkillCoreConfig, PythonSkillCoreSpace

    bind = op.get_bind()
    spec = importlib.util.spec_from_file_location("skill_asset_constraints", Path(__file__).with_name("c5e7f9a1b3d5_add_skill_assets.py"))
    assert spec and spec.loader
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)
    for model in (PythonSkillCoreSpace, PythonSkillCoreConfig):
        table = model.__table__
        inspector = sa.inspect(bind)
        if inspector.has_table(table.name, schema="usr_ai"):
            columns = {item["name"]: item for item in inspector.get_columns(table.name, schema="usr_ai")}
            if set(columns) != set(table.columns.keys()):
                raise RuntimeError("Existing Python core table has incompatible columns")
            for column in table.columns:
                actual = columns[column.name]
                if actual["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or actual["nullable"] != column.nullable:
                    raise RuntimeError("Existing Python core table has incompatible column types")
        else:
            table.create(bind)
        previous.validate_constraints(bind, table)


def downgrade() -> None:
    for table in ("t_ai_python_skill_search_configs", "t_ai_python_skill_spaces"):
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM usr_ai.{table})")):
            raise RuntimeError("Cannot discard persisted Python core Skills data")
    op.drop_table("t_ai_python_skill_search_configs", schema="usr_ai")
    op.drop_table("t_ai_python_skill_spaces", schema="usr_ai")
