"""Add immutable tenant-owned skill assets and durable operations.

Revision ID: c5e7f9a1b3d5
Revises: b0d2e4f6a8c0
"""

import uuid
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "c5e7f9a1b3d5"
down_revision = "b0d2e4f6a8c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from api.db.db_models import Skill, SkillIndexGeneration, SkillOperation, SkillSearchConfig, SkillSpace, SkillVersion, SkillVersionFile, ensure_skill_deferred_constraints

    bind = op.get_bind()
    for model in (SkillSpace, Skill, SkillVersion, SkillVersionFile, SkillSearchConfig, SkillIndexGeneration, SkillOperation):
        table = model.__table__
        inspector = sa.inspect(bind)
        if inspector.has_table(table.name, schema="usr_ai"):
            columns = {item["name"]: item for item in inspector.get_columns(table.name, schema="usr_ai")}
            if set(columns) != set(table.columns.keys()):
                raise RuntimeError("Existing skill table has incompatible columns")
            for column in table.columns:
                actual = columns[column.name]
                actual_type = actual["type"].compile(dialect=bind.dialect).replace("DOUBLE PRECISION", "FLOAT")
                expected_type = column.type.compile(dialect=bind.dialect).replace("DOUBLE PRECISION", "FLOAT")
                if actual_type != expected_type or actual["nullable"] != column.nullable:
                    raise RuntimeError("Existing skill table has incompatible column types")
        else:
            table.create(bind)
    ensure_skill_deferred_constraints(bind)
    for model in (SkillSpace, Skill, SkillVersion, SkillVersionFile, SkillSearchConfig, SkillIndexGeneration, SkillOperation):
        validate_constraints(bind, model.__table__)


def validate_constraints(bind: Any, table: sa.Table) -> None:
    """Reject partially bootstrapped or drifted tables, including changed CHECK text."""
    inspector = sa.inspect(bind)
    schema = "usr_ai"
    unique = inspector.get_unique_constraints(table.name, schema=schema)
    foreign = {item["name"]: item for item in inspector.get_foreign_keys(table.name, schema=schema)}
    indexes = {item["name"]: item for item in inspector.get_indexes(table.name, schema=schema)}
    if inspector.get_pk_constraint(table.name, schema=schema)["constrained_columns"] != list(table.primary_key.columns.keys()):
        raise RuntimeError("Existing skill table has incompatible primary key")
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            if not any(item["column_names"] == list(constraint.columns.keys()) and (constraint.name is None or item["name"] == constraint.name) for item in unique):
                raise RuntimeError("Existing skill table has incompatible unique constraint")
        elif isinstance(constraint, sa.ForeignKeyConstraint):
            actual = foreign.get(constraint.name)
            expected_columns = [element.column.name for element in constraint.elements]
            referred = next(iter(constraint.elements)).column.table
            if (
                actual is None
                or actual["constrained_columns"] != list(constraint.columns.keys())
                or actual["referred_columns"] != expected_columns
                or actual["referred_table"] != referred.name
                or actual["referred_schema"] != referred.schema
            ):
                raise RuntimeError("Existing skill table has incompatible foreign key")
            options = actual.get("options", {})
            if (
                bool(options.get("deferrable")) != bool(constraint.deferrable)
                or options.get("initially") != constraint.initially
                or options.get("ondelete") != constraint.ondelete
                or options.get("onupdate") != constraint.onupdate
            ):
                raise RuntimeError("Existing skill table has incompatible foreign key options")
    for index in table.indexes:
        actual = indexes.get(index.name)
        predicate = str(index.dialect_options["postgresql"].get("where") if index.dialect_options["postgresql"].get("where") is not None else "")
        reflected = str(actual.get("dialect_options", {}).get("postgresql_where", "")) if actual else ""

        def normalize(text: str) -> str:
            return "".join(text.replace("(", "").replace(")", "").split())

        if actual is None or actual["column_names"] != list(index.columns.keys()) or bool(actual["unique"]) != bool(index.unique) or normalize(reflected) != normalize(predicate):
            raise RuntimeError("Existing skill table has incompatible index")
    # Let PostgreSQL normalize CHECK expressions on an empty temporary table.
    # This preserves casts/operators instead of attempting unsafe text rewrites.
    probe = "skill_constraint_probe_" + uuid.uuid4().hex
    bind.execute(sa.text(f'CREATE TEMP TABLE "{probe}" (LIKE usr_ai."{table.name}") ON COMMIT DROP'))
    try:
        for constraint in table.constraints:
            if isinstance(constraint, sa.CheckConstraint):
                expression = str(constraint.sqltext.compile(dialect=bind.dialect, compile_kwargs={"literal_binds": True}))
                bind.execute(sa.text(f'ALTER TABLE "{probe}" ADD CONSTRAINT "{constraint.name}" CHECK ({expression})'))
        expected_checks = {item["name"]: item["sqltext"] for item in sa.inspect(bind).get_check_constraints(probe)}
        actual_checks = {item["name"]: item["sqltext"] for item in inspector.get_check_constraints(table.name, schema=schema)}
        if any(actual_checks.get(name) != expression for name, expression in expected_checks.items()):
            raise RuntimeError("Existing skill table has incompatible check constraint")
    finally:
        bind.execute(sa.text(f'DROP TABLE "{probe}"'))
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = CAST(:table_name AS regclass) AND NOT convalidated)"), {"table_name": f"usr_ai.{table.name}"}):
        raise RuntimeError("Existing skill table has unvalidated constraints")


def downgrade() -> None:
    bind = op.get_bind()
    tables = ("operations", "index_generations", "search_configs", "version_files", "versions", "skills", "spaces")
    names = ["t_ai_skills" if name == "skills" else "t_ai_skill_" + name for name in tables]
    for name in names:
        if bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM usr_ai.{name})")):
            raise RuntimeError("Cannot discard persisted skill assets or operations.")
    op.drop_constraint("fk_skill_space_generation", "t_ai_skill_spaces", schema="usr_ai", type_="foreignkey")
    op.drop_constraint("fk_skill_active_version", "t_ai_skills", schema="usr_ai", type_="foreignkey")
    for name in names:
        op.drop_table(name, schema="usr_ai")
