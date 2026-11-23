"""Move registrations and persisted SQL references to independent string model IDs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import op
from alembic.util import load_python_file
from sqlalchemy.engine import Connection

revision = "f7a9c2e4b6d8"
down_revision = "172b4d6f8a0c"
branch_labels = None
depends_on = None

_MIGRATIONS = str(Path(__file__).parents[1])
REFERENCES = load_python_file(_MIGRATIONS, "model_identity_references.py")
SCHEMA = REFERENCES.SCHEMA
LEGACY = "t_ai_tenant_llms"
MODELS = "t_ai_tenant_models"
PROVIDERS = "t_ai_tenant_model_providers"
INSTANCES = "t_ai_tenant_model_instances"
GATEWAY_KEYS = "t_ai_gateway_keys"
GATEWAY_MODELS = {
    "t_ai_gateway_model_settings": "model_id",
    "t_ai_gateway_request_logs": "upstream_model_id",
}


def _runtime_credential(config: dict[str, Any]) -> str:
    """Reproduce the prepared credential in the pre-cutover embedding hash."""
    try:
        payload = json.loads(config["api_key"])
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        return config["api_key"]
    payload.pop("is_tools", None)
    raw_key = payload.get("api_key", "")
    return raw_key if set(payload) <= {"api_key"} and isinstance(raw_key, str) else json.dumps(payload, ensure_ascii=False)


def _embedding_fingerprints(row: sa.RowMapping, identifier: str) -> tuple[str, str] | None:
    if "embedding" not in row["model_types"]:
        return None
    # Regional transport and provider-specific credential normalization remain
    # shared with the runtime. The REFERENCES.fingerprint fields and ID conversion are
    # frozen here. A mismatching historic digest never gets marked current.
    from api.db.provider_catalog import normalize_model_transport

    credential = row["instance_api_key"]
    try:
        payload = json.loads(credential)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and "is_tools" in payload:
        payload.pop("is_tools")
        credential = json.dumps(payload, ensure_ascii=False)
    config = {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "llm_factory": row["provider_name"],
        "llm_name": row["model_name"],
        "mdl_type": "embedding",
        "api_key": credential,
        "api_base": row["base_url"],
        "region": row["region"],
        "max_tokens": row["max_tokens"],
        "instance_id": row["instance_id"],
    }
    try:
        config = normalize_model_transport(config)
    except ValueError:
        # A registration with invalid transport settings can be migrated without
        # becoming callable or certifying any historical embedding binding.
        return None
    config["api_key"] = _runtime_credential(config)
    previous = {field: config.get(field) for field in REFERENCES.MODEL_FIELDS}
    return REFERENCES.fingerprint(previous), REFERENCES.fingerprint({**previous, "id": identifier})


def _collect_models(bind: Connection) -> tuple[REFERENCES.References, list[dict[str, Any]]]:
    metadata = sa.MetaData(schema=SCHEMA)
    legacy, models, providers, instances = (sa.Table(name, metadata, autoload_with=bind) for name in (LEGACY, MODELS, PROVIDERS, INSTANCES))
    timestamps = [legacy.c[key] for key in ("create_time", "create_date") if key in legacy.c and key in models.c]
    rows = bind.execute(
        sa.select(
            legacy.c.id,
            legacy.c.tenant_id,
            legacy.c.llm_name,
            legacy.c.llm_factory,
            legacy.c.max_tokens,
            legacy.c.used_tokens,
            legacy.c.status,
            models.c.instance_id,
            models.c.model_name,
            models.c.model_types,
            models.c.is_tools,
            providers.c.id.label("provider_id"),
            providers.c.tenant_id.label("provider_tenant"),
            providers.c.provider_name,
            instances.c.instance_name,
            instances.c.api_key.label("instance_api_key"),
            instances.c.base_url,
            instances.c.region,
            *timestamps,
        )
        .select_from(legacy.join(models, legacy.c.id == models.c.id).join(instances, models.c.instance_id == instances.c.id).join(providers, instances.c.provider_id == providers.c.id))
        .order_by(legacy.c.id)
    ).mappings()
    old_models: list[REFERENCES.OldModel] = []
    new_rows: list[dict[str, Any]] = []
    embedding_fingerprints: dict[int, tuple[str, str]] = {}
    for registration_order, row in enumerate(rows, 1):
        if row["tenant_id"] != row["provider_tenant"] or row["llm_factory"] != row["provider_name"]:
            raise RuntimeError("Cannot migrate a model linked to a different tenant or provider")
        capabilities = tuple(row["model_types"])
        if not capabilities or not set(capabilities) <= REFERENCES.TYPE_BITS.keys() or row["status"] not in {"0", "1"}:
            raise RuntimeError("Cannot migrate a model with invalid capabilities or status")
        identifier = REFERENCES.model_id(row["id"])
        old_models.append(REFERENCES.OldModel(row["id"], row["tenant_id"], row["provider_name"], row["llm_name"], row["model_name"], row["instance_name"], capabilities))
        if hashes := _embedding_fingerprints(row, identifier):
            embedding_fingerprints[row["id"]] = hashes
        new_rows.append(
            {
                "previous_id": str(row["id"]),
                "id": identifier,
                "registration_order": registration_order,
                "provider_id": row["provider_id"],
                "model_type": sum(REFERENCES.TYPE_BITS[capability] for capability in set(capabilities)),
                "status": "active" if row["status"] == "1" else "inactive",
                "extra": {"max_tokens": row["max_tokens"], "is_tools": row["is_tools"]},
                "used_tokens": row["used_tokens"],
                **{column.name: row[column.name] for column in timestamps},
            }
        )
    if len(new_rows) != bind.scalar(sa.select(sa.func.count()).select_from(legacy)):
        raise RuntimeError("Model cutover would lose at least one legacy registration")
    return REFERENCES.References(old_models, embedding_fingerprints), new_rows


def _install_model_schema(bind: Connection, rows: list[dict[str, Any]]) -> None:
    foreign_keys = sa.inspect(bind).get_foreign_keys(MODELS, schema=SCHEMA)
    for key in foreign_keys:
        if key["referred_table"] == LEGACY and key["constrained_columns"] == ["id"]:
            op.drop_constraint(key["name"], MODELS, type_="foreignkey", schema=SCHEMA)
    op.alter_column(MODELS, "id", schema=SCHEMA, existing_type=sa.BigInteger(), type_=sa.String(32), postgresql_using="id::text")
    columns = (
        sa.Column("registration_order", sa.BigInteger()),
        sa.Column("provider_id", sa.String(32)),
        sa.Column("model_type", sa.Integer()),
        sa.Column("status", sa.String(8), server_default="active"),
        sa.Column("extra", sa.JSON()),
        sa.Column("used_tokens", sa.Integer(), server_default="0"),
    )
    for column in columns:
        op.add_column(MODELS, column, schema=SCHEMA)
    model = sa.Table(MODELS, sa.MetaData(), schema=SCHEMA, autoload_with=bind)
    if rows:
        bind.execute(
            model.update().where(model.c.id == sa.bindparam("previous_id")).values({field: sa.bindparam(field) for field in rows[0] if field != "previous_id"}),
            rows,
        )
    for column in columns:
        op.alter_column(MODELS, column.name, schema=SCHEMA, nullable=False)
    # The gateway assigned shared names to the lowest legacy ID. Keep that
    # registration order independent of opaque IDs for existing and new rows.
    bind.exec_driver_sql(f"ALTER TABLE {SCHEMA}.{MODELS} ALTER COLUMN registration_order ADD GENERATED ALWAYS AS IDENTITY (START WITH {len(rows) + 1})")
    op.create_foreign_key("fk_tenant_model_provider", MODELS, PROVIDERS, ["provider_id"], ["id"], source_schema=SCHEMA, referent_schema=SCHEMA, ondelete="RESTRICT")
    op.create_index("ix_usr_ai_t_ai_tenant_models_provider_id", MODELS, ["provider_id"], schema=SCHEMA)
    op.create_check_constraint("ck_tenant_model_status", MODELS, "status IN ('active', 'inactive')", schema=SCHEMA)
    op.drop_column(MODELS, "model_types", schema=SCHEMA)
    op.drop_column(MODELS, "is_tools", schema=SCHEMA)


def _gateway_reference_tables(bind: Connection) -> set[str]:
    inspector = sa.inspect(bind)
    return {name for name in (GATEWAY_KEYS, *GATEWAY_MODELS) if inspector.has_table(name, schema=SCHEMA)}


def _migrate_gateway_references(bind: Connection, names: set[str]) -> None:
    if not names:
        return
    columns = {table: column for table, column in GATEWAY_MODELS.items() if table in names}
    queries = [f"SELECT DISTINCT {column} FROM {SCHEMA}.{table} WHERE {column} IS NOT NULL" for table, column in columns.items()]
    if GATEWAY_KEYS in names:
        queries.append(f"SELECT DISTINCT jsonb_array_elements_text(allowed_model_ids)::bigint FROM {SCHEMA}.{GATEWAY_KEYS}")
    previous_ids = bind.exec_driver_sql(" UNION ".join(queries)).scalars().all()
    if None in previous_ids:
        raise RuntimeError("Gateway key scope contains an empty model ID")
    bind.exec_driver_sql("CREATE TEMP TABLE model_identity_gateway_ids (old_id varchar(20) PRIMARY KEY, new_id varchar(32) NOT NULL) ON COMMIT DROP")
    if previous_ids:
        bind.execute(
            sa.text("INSERT INTO model_identity_gateway_ids (old_id, new_id) VALUES (:old_id, :new_id)"),
            [{"old_id": str(old_id), "new_id": REFERENCES.model_id(old_id)} for old_id in previous_ids],
        )
    # Settings, expired grants and logs can refer to registrations that were
    # deleted before the cutover. Apply the same mapping without dropping them.
    for table, column in columns.items():
        op.alter_column(table, column, schema=SCHEMA, existing_type=sa.BigInteger(), type_=sa.String(32), postgresql_using=f"{column}::text")
        bind.exec_driver_sql(f"UPDATE {SCHEMA}.{table} SET {column} = ids.new_id FROM model_identity_gateway_ids AS ids WHERE {column} = ids.old_id")
    if GATEWAY_KEYS in names:
        bind.exec_driver_sql(f"""
            UPDATE {SCHEMA}.{GATEWAY_KEYS} AS key
            SET allowed_model_ids = (
                SELECT jsonb_agg(ids.new_id ORDER BY allowed.position)
                FROM jsonb_array_elements_text(key.allowed_model_ids) WITH ORDINALITY AS allowed(old_id, position)
                JOIN model_identity_gateway_ids AS ids ON ids.old_id = allowed.old_id::bigint::text
            )
            WHERE jsonb_array_length(key.allowed_model_ids) > 0
            """)


def _require_drained(bind: Connection, tables: dict[str, sa.Table]) -> None:
    checks = {
        "t_ai_memory_compaction_artifacts": ("running", "persistence_unknown"),
        "t_ai_memory_publication_outbox": ("pending", "staged", "sync_pending", "persistence_unknown"),
        "t_ai_memory_maintenance_executions": ("queued", "claimed", "running", "canceling", "needs_reconciliation"),
        "t_ai_memory_writer_admissions": ("active", "unknown"),
    }
    for name, states in checks.items():
        table = tables.get(name)
        if table is not None and "state" in table.c:
            pending = sa.or_(table.c.state.in_(states), *(table.c[key].is_not(None) for key in ("active_token", "lease_token") if key in table.c))
            if bind.scalar(sa.select(sa.exists().where(pending))):
                raise RuntimeError(f"Drain unfinished operations in {name} before migrating model identities")


def _require_linked_opaque_names(bind: Connection) -> None:
    """An unlinked placeholder has lost the provider model name."""
    metadata = sa.MetaData(schema=SCHEMA)
    legacy = sa.Table(LEGACY, metadata, autoload_with=bind)
    models = sa.Table(MODELS, metadata, autoload_with=bind)
    missing = sa.select(legacy.c.id).where(legacy.c.llm_name.op("~")("^model:[0-9a-f]{32}$"), ~sa.exists(sa.select(models.c.id).where(models.c.id == legacy.c.id)))
    if bind.scalar(sa.select(missing.exists())):
        raise RuntimeError("Repair unlinked model placeholders before the identity migration")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("The model identity cutover requires PostgreSQL")
    if REFERENCES.has_model_first_schema(bind):
        return
    tables = REFERENCES.reference_tables(bind)
    quote = bind.dialect.identifier_preparer.quote
    gateway_tables = _gateway_reference_tables(bind)
    write_tables = sorted({LEGACY, MODELS, PROVIDERS, INSTANCES, *tables, *gateway_tables})
    qualified = ", ".join(f"{SCHEMA}.{quote(name)}" for name in write_tables)
    bind.exec_driver_sql(f"LOCK TABLE {qualified} IN ACCESS EXCLUSIVE MODE")
    _require_drained(bind, tables)
    _require_linked_opaque_names(bind)
    # Resolve legacy rows imported after the original provider-instance revision
    # while their full, authoritative credential and endpoint data still exists.
    load_python_file(_MIGRATIONS, "legacy_model_backfill.py").backfill(bind)
    references, rows = _collect_models(bind)
    REFERENCES.migrate_references(bind, tables, references)
    _install_model_schema(bind, rows)
    _migrate_gateway_references(bind, gateway_tables)
    op.drop_table(LEGACY, schema=SCHEMA)


def downgrade() -> None:
    raise RuntimeError("Model identity cutover is irreversible; restore the pre-cutover database snapshot")
