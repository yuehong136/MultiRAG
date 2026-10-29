"""Idempotent legacy model linking; no application initialization or credentials in logs."""

import hashlib
import json
from collections import defaultdict
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

SCHEMA = "usr_ai"


def credential_identity(key: str) -> str:
    """Remove only model-level tools encoding, retaining all credential semantics."""
    try:
        payload = json.loads(key)
    except (ValueError, TypeError):
        return key
    if not isinstance(payload, dict):
        return key
    if "is_tools" in payload and type(payload["is_tools"]) is not bool:
        return key
    payload.pop("is_tools", None)
    if set(payload) == {"api_key"} and isinstance(payload["api_key"], str):
        return payload["api_key"]
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def backfill(bind: Connection, tenant_id_filter: str | None = None) -> None:
    """Idempotently link old rows, retaining distinct credential/base pairs."""
    metadata = sa.MetaData(schema=SCHEMA)
    legacy = sa.Table("t_ai_tenant_llms", metadata, autoload_with=bind)
    providers = sa.Table("t_ai_tenant_model_providers", metadata, autoload_with=bind)
    instances = sa.Table("t_ai_tenant_model_instances", metadata, autoload_with=bind)
    models = sa.Table("t_ai_tenant_models", metadata, autoload_with=bind)
    catalog = sa.Table("t_ai_llms", metadata, autoload_with=bind)
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    query = sa.select(legacy).where(~sa.exists(sa.select(models.c.id).where(models.c.id == legacy.c.id))).order_by(legacy.c.id)
    if tenant_id_filter is not None:
        query = query.where(legacy.c.tenant_id == tenant_id_filter)
    for row in bind.execute(query).mappings():
        groups[(row["tenant_id"], row["llm_factory"])].append(row)
    for (tenant_id, factory), rows in groups.items():
        provider_id = bind.scalar(sa.select(providers.c.id).where(providers.c.tenant_id == tenant_id, providers.c.provider_name == factory))
        if provider_id is None:
            provider_id = uuid5(NAMESPACE_URL, f"multirag:provider:{tenant_id}:{factory}").hex
            bind.execute(providers.insert().values(id=provider_id, tenant_id=tenant_id, provider_name=factory, status="active"))
        credential_groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for row in rows:
            credential_groups[(credential_identity(row["api_key"] or ""), row["api_base"] or "")].append(row)
        # New imports can share an existing instance. Never relink migrated models
        # or merge existing instances, which may already carry active references.
        existing: dict[tuple[str, str], Any] = {}
        existing_ids: set[str] = set()
        for stored_instance in bind.execute(sa.select(instances).where(instances.c.provider_id == provider_id).order_by(instances.c.id)).mappings():
            existing_ids.add(stored_instance["id"])
            if not stored_instance["region"]:
                existing.setdefault((credential_identity(stored_instance["api_key"] or ""), stored_instance["base_url"] or ""), stored_instance)
        for (identity, base_url), credential_rows in credential_groups.items():
            key = credential_rows[0]["api_key"] or ""
            fingerprint = hashlib.sha256(json.dumps([identity, base_url], ensure_ascii=False).encode()).hexdigest()
            instance_id = uuid5(NAMESPACE_URL, f"multirag:instance:{provider_id}:{fingerprint}").hex
            instance = existing.get((identity, base_url))
            if instance is not None:
                instance_id = instance["id"]
            if instance is None:
                if instance_id in existing_ids:
                    # A previously migrated instance can have been edited since.
                    # Preserve its identity and create one for this new import.
                    instance_id = uuid5(NAMESPACE_URL, f"multirag:instance:{provider_id}:{fingerprint}:legacy:{credential_rows[0]['id']}").hex
                instance_name = "default" if len(credential_groups) == 1 else f"legacy-{credential_rows[0]['id']}"
                collision = bind.scalar(sa.select(instances.c.id).where(instances.c.provider_id == provider_id, instances.c.instance_name == instance_name))
                if collision:
                    instance_name = f"legacy-{credential_rows[0]['id']}"
                bind.execute(instances.insert().values(id=instance_id, provider_id=provider_id, instance_name=instance_name, api_key=key, base_url=base_url, region="", status="active"))
            for row in credential_rows:
                raw_name = row["llm_name"] or ""
                suffix = {"LocalAI": "___LocalAI", "HuggingFace": "___HuggingFace", "OpenAI-API-Compatible": "___OpenAI-API", "VLLM": "___VLLM"}.get(factory, "")
                if suffix and raw_name.endswith(suffix):
                    raw_name = raw_name[: -len(suffix)]
                capability = row["mdl_type"]
                if not raw_name or capability not in {"chat", "embedding", "rerank", "image2text", "speech2text", "tts", "ocr"}:
                    raise RuntimeError("Legacy model has an invalid name or capability; migration requires explicit repair")
                item = bind.execute(sa.select(catalog).where(catalog.c.fid == factory, catalog.c.llm_name == raw_name)).mappings().first()
                capabilities = [capability]
                if item and "IMAGE2TEXT" in {tag.strip().upper() for tag in (item["tags"] or "").split(",")} and "image2text" not in capabilities:
                    capabilities.append("image2text")
                tools = bool(item["is_tools"]) if item else False
                try:
                    envelope = json.loads(row["api_key"] or "")
                except (ValueError, TypeError):
                    envelope = None
                if isinstance(envelope, dict) and isinstance(envelope.get("is_tools"), bool):
                    tools = envelope["is_tools"]
                bind.execute(models.insert().values(id=row["id"], instance_id=instance_id, model_name=raw_name, model_types=capabilities, is_tools=tools))


def backfill_tenant_defaults(db: Session) -> int:
    """Fill only missing default identities in the caller's transaction.

    Keep stored aliases and existing numeric choices unchanged. No completion
    marker is used: newly imported legacy defaults must remain eligible later.
    """
    from api.db.db_models import Tenant
    from api.db.model_contract import DEFAULT_MODEL_FIELDS
    from api.db.services.provider_model_resolution import find_model_row

    missing = sa.or_(*(sa.and_(getattr(Tenant, f"tenant_{field}").is_(None), getattr(Tenant, field).is_not(None), getattr(Tenant, field) != "") for field in DEFAULT_MODEL_FIELDS.values()))
    last_id = ""
    updated = 0
    while True:
        rows = list(db.scalars(sa.select(Tenant).where(missing, Tenant.id > last_id).order_by(Tenant.id).limit(500)))
        if not rows:
            return updated
        for tenant in rows:
            for capability, field in DEFAULT_MODEL_FIELDS.items():
                target = f"tenant_{field}"
                reference = getattr(tenant, field)
                if getattr(tenant, target) is not None or not reference:
                    continue
                model = find_model_row(db, tenant.id, reference, capability)
                if model is not None:
                    result = db.execute(sa.update(Tenant).where(Tenant.id == tenant.id, getattr(Tenant, target).is_(None), getattr(Tenant, field) == reference).values({target: model.id}))
                    updated += result.rowcount
            last_id = tenant.id
        db.flush()
