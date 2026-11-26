"""Replace published gateway models with the gateway catalog.

Every registered model is in the catalog now; ``t_ai_gateway_model_settings``
keeps what an operator changed per model, ``t_ai_gateway_settings`` the
tenant default, and keys may allow whole connections. Existing data keeps
its behaviour: a published name becomes the model's alias, an inactive
publication a closed model, and for tenants that already used the gateway
every model that was never published starts closed. Key scopes move from
published-model ids to registered model ids.

Revision ID: f3b5d7e9a1c2
Revises: d1f3a5c7e9b2
"""

import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import op
from alembic.util import load_python_file
from sqlalchemy.dialects.postgresql import JSONB, insert

revision: str = "f3b5d7e9a1c2"
down_revision: str | None = "d1f3a5c7e9b2"
branch_labels: str | None = None
depends_on: str | None = None
SCHEMA = "usr_ai"
_LIMITS_CHECK = "(rpm_limit IS NULL OR rpm_limit > 0) AND (tpm_limit IS NULL OR tpm_limit > 0) AND (max_parallel_requests IS NULL OR max_parallel_requests > 0)"
_KEYS = "t_ai_gateway_keys"


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True, index=True),
        sa.Column("update_date", sa.DateTime(), nullable=True, index=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True, index=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True, index=True),
    ]


def _tables(*, model_first: bool = False) -> tuple[sa.Table, sa.Table]:
    metadata = sa.MetaData(schema=SCHEMA)
    model_settings = sa.Table(
        "t_ai_gateway_model_settings",
        metadata,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False, index=True),
        sa.Column("model_id", sa.String(32) if model_first else sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("alias", sa.String(128), nullable=True),
        sa.Column("preferred", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.BigInteger(), nullable=True),
        sa.Column("max_parallel_requests", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("updated_by", sa.String(32), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "model_id", name="uq_gateway_model_settings_tenant_model"),
        sa.CheckConstraint(_LIMITS_CHECK, name="ck_gateway_model_settings_limits"),
        sa.Index("uq_gateway_model_settings_tenant_alias", "tenant_id", "alias", unique=True, postgresql_where=sa.text("alias IS NOT NULL")),
    )
    settings = sa.Table(
        "t_ai_gateway_settings",
        metadata,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("new_models_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", sa.String(32), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", name="uq_gateway_settings_tenant"),
    )
    return model_settings, settings


def _published() -> sa.Table:
    """The published-model table of revision d1f3a5c7e9b2, read here and dropped."""
    return sa.Table(
        "t_ai_gateway_models",
        sa.MetaData(schema=SCHEMA),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("model_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.BigInteger(), nullable=True),
        sa.Column("max_parallel_requests", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("updated_by", sa.String(32), nullable=True),
        sa.Column("create_date", sa.DateTime(), nullable=True, index=True),
        sa.Column("update_date", sa.DateTime(), nullable=True, index=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True, index=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True, index=True),
        sa.UniqueConstraint("tenant_id", "name", name="uq_gateway_models_tenant_name"),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_gateway_models_status"),
        sa.CheckConstraint("model_type IN ('chat', 'embedding', 'rerank')", name="ck_gateway_models_type"),
        sa.CheckConstraint(_LIMITS_CHECK, name="ck_gateway_models_limits"),
    )


def _keys() -> sa.Table:
    return sa.Table(
        _KEYS,
        sa.MetaData(schema=SCHEMA),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32)),
        sa.Column("allowed_model_ids", JSONB()),
        sa.Column("allowed_instance_ids", JSONB()),
    )


def _registered() -> tuple[sa.Table, sa.Table]:
    metadata = sa.MetaData(schema=SCHEMA)
    llms = sa.Table("t_ai_tenant_llms", metadata, sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("tenant_id", sa.String(32)))
    models = sa.Table("t_ai_tenant_models", metadata, sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("model_name", sa.String(128)))
    return llms, models


def _validate(bind: sa.Connection, table: sa.Table) -> None:
    """Accept a table created model-first only when it matches this revision."""
    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns(table.name, schema=SCHEMA)}
    # A table created from a newer model already has the columns later revisions add.
    if not set(table.columns.keys()) <= set(columns):
        raise RuntimeError(f"Incompatible model gateway columns: {table.name}")
    for column in table.columns:
        actual = columns[column.name]
        if actual["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or actual["nullable"] != column.nullable:
            raise RuntimeError(f"Incompatible model gateway column: {table.name}.{column.name}")
    if inspector.get_pk_constraint(table.name, schema=SCHEMA)["constrained_columns"] != ["id"]:
        raise RuntimeError(f"Incompatible model gateway primary key: {table.name}")
    actual_unique = {tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table.name, schema=SCHEMA)}
    actual_checks = {item["name"] for item in inspector.get_check_constraints(table.name, schema=SCHEMA)}
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint) and tuple(column.name for column in constraint.columns) not in actual_unique:
            raise RuntimeError(f"Missing model gateway unique constraint: {table.name}")
        if isinstance(constraint, sa.CheckConstraint) and constraint.name not in actual_checks:
            raise RuntimeError(f"Missing model gateway check constraint: {table.name}.{constraint.name}")
    actual_indexes = {(tuple(index["column_names"]), bool(index["unique"])) for index in inspector.get_indexes(table.name, schema=SCHEMA)}
    if any((tuple(column.name for column in index.columns), bool(index.unique)) not in actual_indexes for index in table.indexes):
        raise RuntimeError(f"Missing model gateway indexes: {table.name}")


def _carry_over(bind: sa.Connection, model_settings: sa.Table) -> None:
    """Move published names, switches, limits and key scopes onto registered models."""
    published, keys = _published(), _keys()
    rows = bind.execute(sa.select(published).order_by(published.c.tenant_id, (published.c.status != "active"), published.c.create_time, published.c.id)).mappings().all()
    key_rows = bind.execute(sa.select(keys.c.id, keys.c.tenant_id, keys.c.allowed_model_ids)).mappings().all()
    tenants = {row["tenant_id"] for row in rows} | {row["tenant_id"] for row in key_rows}
    if not tenants:
        return
    llms, models = _registered()
    registered = bind.execute(sa.select(llms.c.id, llms.c.tenant_id, models.c.model_name).join(models, models.c.id == llms.c.id).where(llms.c.tenant_id.in_(tenants))).all()
    # One publication per model wins: an active one, then the earliest.
    chosen: dict[tuple[str, int], Any] = {}
    for row in rows:
        chosen.setdefault((row["tenant_id"], int(row["model_id"])), row)
    values = []
    for model_id, tenant_id, model_name in registered:
        row = chosen.get((tenant_id, int(model_id)))
        if row is None:
            # Never published, so no key could call it: it starts closed.
            values.append({"id": uuid.uuid4().hex, "tenant_id": tenant_id, "model_id": int(model_id), "enabled": False, "created_by": tenant_id})
            continue
        values.append(
            {
                "id": uuid.uuid4().hex,
                "tenant_id": tenant_id,
                "model_id": int(model_id),
                "enabled": row["status"] == "active",
                "alias": row["name"] if row["name"] != model_name else None,
                "description": row["description"],
                "rpm_limit": row["rpm_limit"],
                "tpm_limit": row["tpm_limit"],
                "max_parallel_requests": row["max_parallel_requests"],
                "created_by": row["created_by"],
                "updated_by": row["updated_by"],
                "create_time": row["create_time"],
                "update_time": row["update_time"],
                "create_date": row["create_date"],
                "update_date": row["update_date"],
            }
        )
    for value in values:
        bind.execute(insert(model_settings).values(**value).on_conflict_do_nothing(index_elements=["tenant_id", "model_id"]))
    published_model = {row["id"]: int(row["model_id"]) for row in rows}
    for key in key_rows:
        scope = [published_model[item] for item in key["allowed_model_ids"] or [] if isinstance(item, str) and item in published_model]
        bind.execute(keys.update().where(keys.c.id == key["id"]).values(allowed_model_ids=list(dict.fromkeys(scope))))


def upgrade() -> None:
    bind = op.get_bind()
    references = load_python_file(str(Path(__file__).parents[1]), "model_identity_references.py")
    model_first = references.has_model_first_schema(bind, schema=SCHEMA)
    model_settings, settings = _tables(model_first=model_first)
    for table in (model_settings, settings):
        if sa.inspect(bind).has_table(table.name, schema=SCHEMA):
            _validate(bind, table)
        else:
            table.create(bind)
    if "allowed_instance_ids" not in {column["name"] for column in sa.inspect(bind).get_columns(_KEYS, schema=SCHEMA)}:
        op.add_column(_KEYS, sa.Column("allowed_instance_ids", JSONB(), nullable=False, server_default="[]"), schema=SCHEMA)
    if sa.inspect(bind).has_table("t_ai_gateway_models", schema=SCHEMA):
        if model_first:
            raise RuntimeError("Legacy gateway publications coexist with independent model identities")
        _carry_over(bind, model_settings)
        _published().drop(bind)


def downgrade() -> None:
    bind = op.get_bind()
    model_settings, settings = _tables()
    keys = _keys()
    for table in (model_settings, settings, keys):
        if bind.scalar(sa.select(sa.exists().select_from(table))):
            raise RuntimeError("Cannot discard persisted model gateway data")
    settings.drop(bind)
    model_settings.drop(bind)
    op.drop_column(_KEYS, "allowed_instance_ids", schema=SCHEMA)
    _published().create(bind)
