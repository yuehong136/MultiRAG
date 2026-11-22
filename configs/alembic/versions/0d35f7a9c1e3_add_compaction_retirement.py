"""Retire context bodies while retaining generation identities and request digests."""

import importlib.util
from pathlib import Path
from typing import cast

import sqlalchemy as sa
from alembic import op

revision = "0d35f7a9c1e3"
down_revision = "fc24e6f8b0d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from api.db.memory_compaction_models import MemoryCompactionArtifact

    bind = op.get_bind()
    table = cast(sa.Table, MemoryCompactionArtifact.__table__)
    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns(table.name, schema="usr_ai")}
    for name in ("retired_at", "retirement"):
        desired = table.c[name]
        if name not in columns:
            op.add_column(table.name, sa.Column(name, desired.type, nullable=True), schema="usr_ai")
        elif columns[name]["type"].compile(dialect=bind.dialect) != desired.type.compile(dialect=bind.dialect) or not columns[name]["nullable"]:
            raise RuntimeError("Existing compaction retirement column is incompatible")
    checks = {check["name"] for check in inspector.get_check_constraints(table.name, schema="usr_ai")}
    constraint_name = "ck_memory_compaction_retirement"
    constraint = next(check for check in table.constraints if isinstance(check, sa.CheckConstraint) and check.name == constraint_name)
    if constraint_name not in checks:
        op.create_check_constraint(constraint_name, table.name, str(constraint.sqltext), schema="usr_ai")
    indexes = {index["name"] for index in inspector.get_indexes(table.name, schema="usr_ai")}
    if "ix_memory_compaction_retention" not in indexes:
        op.create_index("ix_memory_compaction_retention", table.name, ["memory_id", "tenant_id", "retired_at", "state"], schema="usr_ai")
    spec = importlib.util.spec_from_file_location("compaction_retention_constraints", Path(__file__).with_name("c5e7f9a1b3d5_add_skill_assets.py"))
    assert spec and spec.loader
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    validator.validate_constraints(bind, table)


def downgrade() -> None:
    from api.db.memory_compaction_models import MemoryCompactionArtifact

    bind = op.get_bind()
    table = cast(sa.Table, MemoryCompactionArtifact.__table__)
    if bind.scalar(sa.select(sa.exists().where(sa.or_(table.c.retired_at.is_not(None), table.c.retirement.is_not(None))))):
        raise RuntimeError("Cannot discard compaction retirement tombstones")
    op.drop_constraint("ck_memory_compaction_retirement", table.name, type_="check", schema="usr_ai")
    op.drop_index("ix_memory_compaction_retention", table_name=table.name, schema="usr_ai")
    op.drop_column(table.name, "retirement", schema="usr_ai")
    op.drop_column(table.name, "retired_at", schema="usr_ai")
