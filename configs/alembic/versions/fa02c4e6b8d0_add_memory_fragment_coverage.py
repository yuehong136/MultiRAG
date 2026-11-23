"""Add opt-in metadata-only fragment coverage and nullable input provenance."""

import importlib.util
from pathlib import Path
from typing import cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "fa02c4e6b8d0"
down_revision = "f8c0e2a4b6d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from api.db.memory_incremental_models import MemoryMaintenanceCoverage, MemoryMaintenanceFragmentCoverage

    bind = op.get_bind()
    for table_name, column, expected in (
        ("t_ai_memory_maintenance_inputs", "fragment", sa.Column("fragment", JSONB(), nullable=True)),
        ("t_ai_memory_maintenance_coverage", "expected_fragments", sa.Column("expected_fragments", sa.BigInteger(), nullable=False, server_default="0")),
    ):
        columns = {item["name"]: item for item in sa.inspect(bind).get_columns(table_name, schema="usr_ai")}
        if column not in columns:
            op.add_column(table_name, expected, schema="usr_ai")
        elif columns[column]["type"].compile(dialect=bind.dialect) != expected.type.compile(dialect=bind.dialect) or columns[column]["nullable"] != expected.nullable:
            raise RuntimeError("Existing memory fragment column is incompatible")
    table = cast(sa.Table, MemoryMaintenanceFragmentCoverage.__table__)
    if not sa.inspect(bind).has_table(table.name, schema="usr_ai"):
        table.create(bind)
    else:
        columns = {item["name"]: item for item in sa.inspect(bind).get_columns(table.name, schema="usr_ai")}
        if set(columns) != set(table.columns.keys()) or any(
            columns[column.name]["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or columns[column.name]["nullable"] != column.nullable for column in table.columns
        ):
            raise RuntimeError("Existing memory fragment table is incompatible")
    spec = importlib.util.spec_from_file_location("memory_fragment_constraints", Path(__file__).with_name("c5e7f9a1b3d5_add_skill_assets.py"))
    assert spec and spec.loader
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    validator.validate_constraints(bind, table)
    coverage = cast(sa.Table, MemoryMaintenanceCoverage.__table__)
    checks = {item["name"] for item in sa.inspect(bind).get_check_constraints(coverage.name, schema="usr_ai")}
    if "ck_memory_coverage_fragments" not in checks:
        op.create_check_constraint("ck_memory_coverage_fragments", coverage.name, "expected_fragments >= 0 AND expected_fragments <= 64", schema="usr_ai")
    validator.validate_constraints(bind, coverage)


def downgrade() -> None:
    from api.db.memory_incremental_models import MemoryMaintenanceFragmentCoverage

    bind = op.get_bind()
    if bind.scalar(sa.select(sa.exists().select_from(MemoryMaintenanceFragmentCoverage.__table__))):
        raise RuntimeError("Cannot discard persistent memory fragment records")
    op.drop_table(MemoryMaintenanceFragmentCoverage.__tablename__, schema="usr_ai")
    op.drop_column("t_ai_memory_maintenance_coverage", "expected_fragments", schema="usr_ai")
    op.drop_column("t_ai_memory_maintenance_inputs", "fragment", schema="usr_ai")
