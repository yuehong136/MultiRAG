"""Reference inventory and value conversion frozen for the model identity cutover."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, NamedTuple
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection

SCHEMA = "usr_ai"
TYPE_BITS = {"chat": 1, "embedding": 2, "speech2text": 4, "image2text": 8, "rerank": 16, "tts": 32, "ocr": 64}
DEFAULT_TYPES = {
    "llm_id": "chat",
    "embd_id": "embedding",
    "asr_id": "speech2text",
    "img2txt_id": "image2text",
    "rerank_id": "rerank",
    "tts_id": "tts",
    "ocr_id": "ocr",
}
SCALAR_REFERENCES = {
    "t_ai_tenants": dict.fromkeys(DEFAULT_TYPES),
    "t_ai_knowledgebases": {"embd_id": None},
    "t_ai_dialogs": {"llm_id": None, "rerank_id": None},
    "t_ai_memories": {"embd_id": None, "llm_id": None},
    "t_ai_skill_search_configs": {"embedding_model_id": "embedding", "rerank_model_id": "rerank"},
}
JSON_REFERENCES = {
    "t_ai_knowledgebases": ("parser_config",),
    "t_ai_documents": ("parser_config",),
    "t_ai_search": ("search_config",),
    "t_ai_user_canvases": ("dsl",),
    "t_ai_canvas_templates": ("dsl",),
    "t_ai_user_canvas_version": ("dsl",),
    "t_pipeline_operation_log": ("dsl",),
    "t_ai_pipeline_operation_logs": ("dsl",),
    "t_ai_api4conversations": ("dsl",),
    "t_ai_agent_execution_origins": ("snapshot_dsl",),
    "t_ai_python_skill_search_configs": ("settings", "index_data"),
    "t_ai_skill_index_generations": ("config",),
    "t_ai_memory_entry_revisions": ("message_snapshot",),
    "t_ai_memory_publication_outbox": ("payload", "receipt"),
    "t_ai_memory_mutation_operations": ("result",),
    "t_ai_memory_maintenance_jobs": ("entry_snapshots", "receipt"),
    "t_ai_memory_maintenance_inputs": ("snapshot",),
    "t_ai_memory_maintenance_candidates": ("receipt",),
    "t_ai_memory_maintenance_discoveries": ("profile", "receipt"),
    "t_ai_memory_maintenance_executions": ("receipt",),
    "t_ai_memory_writer_admissions": ("request_metadata", "receipt"),
    "t_ai_memory_compaction_artifacts": ("model_binding",),
    "t_ai_memory_verification_records": ("entry_binding",),
}
REFERENCE_KEYS = {
    **DEFAULT_TYPES,
    **{f"tenant_{key}": value for key, value in DEFAULT_TYPES.items()},
    "embedding_model_id": "embedding",
    "embedding_model": "embedding",
    "rerank_model_id": "rerank",
    "layout_recognize": None,
    "layout_recognizer": None,
    "parse_method": None,
    "mineru_llm_name": "ocr",
    "paddleocr_llm_name": "ocr",
    "opendataloader_llm_name": "ocr",
    "model_id": None,
    "llm_id": None,
    "tenant_llm_id": None,
    "search_chat_id": "chat",
}
BOUND_ID_KEYS = {key for key in REFERENCE_KEYS if key.startswith("tenant_")} | {"embedding_model_id", "rerank_model_id"}
DOSSIER_KEYS = {"tenant_model_id", "model_reference", "config_reference"}
MODEL_FIELDS = ("id", "tenant_id", "llm_factory", "llm_name", "mdl_type", "model_type", "api_key", "api_base", "region", "max_tokens", "instance_id")
HISTORICAL_JSON = {
    "t_pipeline_operation_log",
    "t_ai_pipeline_operation_logs",
    "t_ai_user_canvas_version",
    "t_ai_api4conversations",
    "t_ai_agent_execution_origins",
    "t_ai_skill_index_generations",
}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def model_id(old_id: int) -> str:
    return uuid5(NAMESPACE_URL, f"multirag:tenant-model-id:{old_id}").hex


class OldModel(NamedTuple):
    id: int
    tenant_id: str
    factory: str
    old_name: str | None
    name: str
    instance: str
    capabilities: tuple[str, ...]


class References:
    def __init__(self, models: list[OldModel], embedding_fingerprints: dict[int, tuple[str, str]]) -> None:
        self.models = {model.id: model for model in models}
        self.identifiers = {identifier: model_id(identifier) for identifier in self.models}
        self.embedding_fingerprints = embedding_fingerprints
        self.exact: defaultdict[tuple[str, str], set[int]] = defaultdict(set)
        self.named: defaultdict[tuple[str, str], set[int]] = defaultdict(set)
        for model in models:
            if model.old_name:
                self.exact[model.tenant_id, model.old_name].add(model.id)
                self.exact[model.tenant_id, f"{model.old_name}@{model.factory}"].add(model.id)
            for alias in (model.name, f"{model.name}@{model.factory}", f"{model.name}@{model.instance}@{model.factory}"):
                self.named[model.tenant_id, alias].add(model.id)

    def resolve(self, value: Any, tenant: str | None, capability: str | tuple[str, ...] | None, *, exact_id: bool = False, historical: bool = False) -> str | None:
        if type(value) not in {int, str} or not value:
            return None
        name = str(value)
        numeric = int(name) if re.fullmatch(r"[1-9][0-9]{0,18}", name) else None
        numeric_match = numeric is not None and numeric in self.models
        exact = self.exact.get((tenant or "", name), set()) if not exact_id else set()
        if numeric_match and (exact_id or self.models[numeric].tenant_id == tenant or not exact):
            candidates = {*exact, numeric}
        elif exact:
            candidates = exact
        elif not exact_id:
            candidates = self.named.get((tenant or "", name), set())
        else:
            candidates = set()
        if len(candidates) > 1:
            raise RuntimeError("Ambiguous persisted model reference; select an explicit model before migration")
        if not candidates:
            if historical and numeric is not None:
                return model_id(numeric)
            if exact_id and numeric is not None:
                raise RuntimeError("Orphaned persisted model ID; repair the active reference before migration")
            if name.startswith("model:") and not historical:
                raise RuntimeError("Unresolved opaque model reference; repair its registration before migration")
            return None
        identifier = next(iter(candidates))
        accepted = (capability,) if isinstance(capability, str) else capability
        if accepted and not any(kind in self.models[identifier].capabilities for kind in accepted) and not historical:
            raise RuntimeError("Persisted model reference does not match its configured capability")
        return self.identifiers[identifier]

    def _binding(self, value: dict[str, Any]) -> dict[str, Any]:
        identifier = value["tenant_model_id"]
        if type(identifier) not in {int, str} or not re.fullmatch(r"[1-9][0-9]{0,18}", str(identifier)):
            return value
        previous_id = int(identifier)
        # Deleted models can still appear in retained provenance. Give their IDs
        # the same deterministic form without creating a callable registration.
        updated = {**value, "tenant_model_id": model_id(previous_id)}
        fingerprints = self.embedding_fingerprints.get(previous_id)
        if "dimension" in value and fingerprints and value["config_reference"] == fingerprints[0]:
            updated["config_reference"] = fingerprints[1]
        return updated

    def rewrite_json(self, value: Any, tenant: str | None, key: str = "", *, historical: bool = False) -> Any:
        if isinstance(value, dict):
            if DOSSIER_KEYS <= value.keys():
                return self._binding(value)
            rewritten = {}
            for field, child in value.items():
                # A paired canonical reference takes precedence over its display alias.
                bound = value.get(f"tenant_{field}") if field in DEFAULT_TYPES else None
                if bound:
                    rewritten[field] = self.rewrite_json(bound, tenant, f"tenant_{field}", historical=historical)
                else:
                    rewritten[field] = self.rewrite_json(child, tenant, "search_chat_id" if key == "search_config" and field == "chat_id" else field, historical=historical)
            return rewritten
        if isinstance(value, list):
            return [self.rewrite_json(child, tenant, key, historical=historical) for child in value]
        if key in {"dsl", "snapshot_dsl"} and isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                return value
            rewritten = self.rewrite_json(decoded, tenant, key, historical=historical)
            return json.dumps(rewritten, ensure_ascii=False) if rewritten != decoded else value
        if key in REFERENCE_KEYS:
            lookup = self.resolve(value, tenant, REFERENCE_KEYS[key], exact_id=key in BOUND_ID_KEYS, historical=historical)
            if lookup is not None:
                return lookup
        return value


def reference_tables(bind: Connection) -> dict[str, sa.Table]:
    names = set(SCALAR_REFERENCES) | set(JSON_REFERENCES)
    metadata = sa.MetaData(schema=SCHEMA)
    inspector = sa.inspect(bind)
    return {name: sa.Table(name, metadata, autoload_with=bind) for name in sorted(names) if inspector.has_table(name, schema=SCHEMA)}


def _tenant_columns(table: sa.Table, tables: dict[str, sa.Table]) -> tuple[sa.FromClause, sa.ColumnElement[str | None]]:
    if table.name == "t_ai_tenants":
        return table, table.c.id
    if "tenant_id" in table.c:
        return table, table.c.tenant_id
    if table.name == "t_ai_documents" and "t_ai_knowledgebases" in tables:
        knowledgebase = tables["t_ai_knowledgebases"]
        return table.outerjoin(knowledgebase, table.c.kb_id == knowledgebase.c.id), knowledgebase.c.tenant_id
    if table.name == "t_ai_user_canvas_version" and "t_ai_user_canvases" in tables:
        canvas = tables["t_ai_user_canvases"]
        return table.outerjoin(canvas, table.c.user_canvas_id == canvas.c.id), canvas.c.user_id
    if table.name == "t_ai_api4conversations" and "t_ai_agent_execution_origins" in tables:
        origin = tables["t_ai_agent_execution_origins"]
        source = table.outerjoin(origin, table.c.id == origin.c.id)
        canvas = tables.get("t_ai_user_canvases")
        if canvas is not None:
            return source.outerjoin(canvas, table.c.dialog_id == canvas.c.id), sa.func.coalesce(origin.c.tenant_id, canvas.c.user_id, table.c.user_id)
        return source, sa.func.coalesce(origin.c.tenant_id, table.c.user_id)
    if "user_id" in table.c:
        return table, table.c.user_id
    return table, sa.literal(None)


def _template_dsl(value: Any, *, encoded: bool = True) -> Any:
    """Templates are global. Model identities are selected in the cloning tenant."""
    if isinstance(value, dict):
        return {field: "" if field in DEFAULT_TYPES else _template_dsl(child, encoded=False) for field, child in value.items() if field not in BOUND_ID_KEYS}
    if isinstance(value, list):
        return [_template_dsl(child, encoded=False) for child in value]
    if encoded and isinstance(value, str) and value.startswith("{"):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        rewritten = _template_dsl(decoded, encoded=False)
        return json.dumps(rewritten, ensure_ascii=False) if rewritten != decoded else value
    return value


def _update_values(table: sa.Table, row: sa.RowMapping, references: References) -> dict[str, Any]:
    tenant = row["_migration_tenant"]
    values: dict[str, Any] = {}
    for field, model_type in SCALAR_REFERENCES.get(table.name, {}).items():
        if model_type is not None:
            stored = row[field]
            if stored is not None:
                resolved = references.resolve(stored, tenant, model_type, exact_id=True)
                if resolved is None:
                    raise RuntimeError(f"Orphaned model ID in {table.name}.{field}")
                values[field] = resolved
            continue
        bound_field = f"tenant_{field}"
        stored = row[bound_field]
        # Dialogs choose chat or vision from the registered capability set.
        # Tenant and memory chat defaults invoke the chat capability directly.
        capability = ("chat", "image2text") if table.name == "t_ai_dialogs" and field == "llm_id" else DEFAULT_TYPES[field]
        resolved = references.resolve(stored, tenant, capability, exact_id=True) if stored is not None else references.resolve(row[field], tenant, capability)
        if stored is not None and resolved is None:
            raise RuntimeError(f"Orphaned model ID in {table.name}.{bound_field}")
        if resolved is not None:
            values.update({bound_field: resolved, field: resolved})
    for field in JSON_REFERENCES.get(table.name, ()):
        if field not in row:
            continue
        select_value = row[field]
        value = _template_dsl(select_value) if table.name == "t_ai_canvas_templates" else references.rewrite_json(select_value, tenant, field, historical=table.name in HISTORICAL_JSON)
        if value != select_value:
            if field == "snapshot_dsl":
                if row["snapshot_digest"] != fingerprint(select_value):
                    raise RuntimeError("Agent execution snapshot integrity check failed before model ID migration")
                values["snapshot_digest"] = fingerprint(value)
            values[field] = value
    return values


def migrate_references(bind: Connection, tables: dict[str, sa.Table], references: References) -> None:
    quote = bind.dialect.identifier_preparer.quote
    for name, fields in SCALAR_REFERENCES.items():
        if name not in tables:
            continue
        for field, kind in fields.items():
            bound = quote(field if kind is not None else f"tenant_{field}")
            bind.exec_driver_sql(f"ALTER TABLE {SCHEMA}.{quote(name)} ALTER COLUMN {bound} TYPE varchar(32) USING {bound}::text")
    tables = reference_tables(bind)
    for name, table in tables.items():
        source, tenant = _tenant_columns(table, tables)
        fields = set(JSON_REFERENCES.get(name, ()))
        for field, kind in SCALAR_REFERENCES.get(name, {}).items():
            fields.add(field)
            if kind is None:
                fields.add(f"tenant_{field}")
        if name == "t_ai_agent_execution_origins":
            fields.add("snapshot_digest")
        keys = tuple(table.primary_key.columns)
        columns = [table.c[name] for name in sorted(fields) if name in table.c]
        if not columns:
            continue
        stmt = sa.select(*keys, tenant.label("_migration_tenant"), *columns).select_from(source)
        with bind.execute(stmt.execution_options(yield_per=500)).mappings() as rows:
            for row in rows:
                values = _update_values(table, row, references)
                if values:
                    bind.execute(table.update().where(*(key == row[key.name] for key in keys)).values(values))


def has_model_first_schema(bind: Connection, *, schema: str = SCHEMA) -> bool:
    """Recognize an exact post-cutover model-first layout, never an incomplete upgrade."""
    inspector = sa.inspect(bind)
    if inspector.has_table("t_ai_tenant_llms", schema=schema):
        return False
    model = "t_ai_tenant_models"
    if not inspector.has_table(model, schema=schema):
        return False
    columns = {column["name"]: column for column in inspector.get_columns(model, schema=schema)}
    if (
        not {"id", "instance_id", "provider_id", "model_name", "model_type", "status", "extra", "used_tokens"} <= columns.keys()
        or {"model_types", "is_tools"} & columns.keys()
        or not isinstance(columns["id"]["type"], sa.String)
        or columns["id"]["type"].length != 32
        or inspector.get_pk_constraint(model, schema=schema)["constrained_columns"] != ["id"]
    ):
        return False
    links = {(tuple(key["constrained_columns"]), key["referred_table"]) for key in inspector.get_foreign_keys(model, schema=schema)}
    if not {(("provider_id",), "t_ai_tenant_model_providers"), (("instance_id",), "t_ai_tenant_model_instances")} <= links:
        return False
    for name, fields in SCALAR_REFERENCES.items():
        if not inspector.has_table(name, schema=schema):
            continue
        types = {column["name"]: column["type"] for column in inspector.get_columns(name, schema=schema)}
        for field, capability in fields.items():
            bound = field if capability is not None else f"tenant_{field}"
            if not isinstance(types.get(bound), sa.String) or types[bound].length != 32:
                return False
    return True
