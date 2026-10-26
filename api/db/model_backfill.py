"""Idempotent legacy model linking; no application initialization or credentials in logs."""

import hashlib
import json
from collections import defaultdict
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection

SCHEMA = "usr_ai"


def backfill(bind: Connection, tenant_id_filter: str | None = None) -> None:
    """Idempotently link old rows, retaining distinct credential/base pairs."""
    metadata = sa.MetaData(schema=SCHEMA)
    legacy = sa.Table("t_ai_tenant_llms", metadata, autoload_with=bind)
    providers = sa.Table("t_ai_tenant_model_providers", metadata, autoload_with=bind)
    instances = sa.Table("t_ai_tenant_model_instances", metadata, autoload_with=bind)
    models = sa.Table("t_ai_tenant_models", metadata, autoload_with=bind)
    catalog = sa.Table("t_ai_llms", metadata, autoload_with=bind)
    existing = set(bind.scalars(sa.select(models.c.id)))
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    query = sa.select(legacy).order_by(legacy.c.id)
    if tenant_id_filter is not None:
        query = query.where(legacy.c.tenant_id == tenant_id_filter)
    for row in bind.execute(query).mappings():
        if row["id"] not in existing:
            groups[(row["tenant_id"], row["llm_factory"])].append(row)
    for (tenant_id, factory), rows in groups.items():
        provider_id = bind.scalar(sa.select(providers.c.id).where(providers.c.tenant_id == tenant_id, providers.c.provider_name == factory))
        if provider_id is None:
            provider_id = uuid5(NAMESPACE_URL, f"multirag:provider:{tenant_id}:{factory}").hex
            bind.execute(providers.insert().values(id=provider_id, tenant_id=tenant_id, provider_name=factory, status="active"))
        credential_groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for row in rows:
            credential_groups[(row["api_key"] or "", row["api_base"] or "")].append(row)
        for (key, base_url), credential_rows in credential_groups.items():
            fingerprint = hashlib.sha256(json.dumps([key, base_url], ensure_ascii=False).encode()).hexdigest()
            instance_id = uuid5(NAMESPACE_URL, f"multirag:instance:{provider_id}:{fingerprint}").hex
            instance = bind.execute(sa.select(instances).where(instances.c.id == instance_id)).mappings().first()
            if instance is None:
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
                    envelope = json.loads(key)
                except (ValueError, TypeError):
                    envelope = None
                if isinstance(envelope, dict) and isinstance(envelope.get("is_tools"), bool):
                    tools = envelope["is_tools"]
                bind.execute(models.insert().values(id=row["id"], instance_id=instance_id, model_name=raw_name, model_types=capabilities, is_tools=tools))
