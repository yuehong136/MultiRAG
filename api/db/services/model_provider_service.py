"""Tenant model lifecycle. Callers own the AsyncSession transaction."""

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Dialog, Knowledgebase, Memory, Search, Tenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider, UserCanvas, UserCanvasVersion
from api.db.model_contract import DEFAULT_MODEL_FIELDS, ConnectionCandidate, InstanceInput, InstancePatch, ModelRegistration
from api.db.provider_catalog import provider_order, provider_transport, public_factories, regional_provider
from common import settings


class ModelConflict(ValueError):
    """A conflicting identity or reference prevents this operation."""


def catalog(provider_name: str, region: str = "") -> list[ModelRegistration]:
    target = regional_provider(provider_name, region)
    factory = next((item for item in settings.FACTORY_LLM_INFOS if item["name"] == target), None)
    if factory is None:
        raise LookupError("Provider not found")
    result = []
    for item in factory.get("llm", []):
        capabilities = [item.get("mdl_type", item.get("model_type"))]
        if "IMAGE2TEXT" in {tag.strip().upper() for tag in item.get("tags", "").split(",")} and "image2text" not in capabilities:
            capabilities.append("image2text")
        result.append(ModelRegistration(model_name=item["llm_name"], model_types=capabilities, max_tokens=item.get("max_tokens", 8192), is_tools=item.get("is_tools", False)))
    return sorted(result, key=lambda model: model.model_name)


def instance_projection(instance: TenantModelInstance, provider: TenantModelProvider) -> dict[str, Any]:
    return {
        "id": instance.id,
        "provider_id": provider.id,
        "provider_name": provider.provider_name,
        "instance_name": instance.instance_name,
        "has_api_key": bool(instance.api_key),
        "base_url": instance.base_url,
        "region": provider_transport(provider.provider_name, instance.api_key, instance.base_url, instance.region, stored=True)[2],
        "status": instance.status,
    }


def model_projection(row: TenantLLM, model: TenantModel, instance: TenantModelInstance, provider: TenantModelProvider) -> dict[str, Any]:
    active = row.status == "1" and instance.status == "active" and provider.status == "active"
    return {
        "id": row.id,
        "name": model.model_name,
        "provider_id": provider.id,
        "provider_name": provider.provider_name,
        "instance_id": instance.id,
        "instance_name": instance.instance_name,
        "model_types": list(model.model_types),
        "status": "active" if row.status == "1" else "inactive",
        "effective_status": "active" if active else "inactive",
        "max_tokens": row.max_tokens,
        "is_tools": model.is_tools,
        "reference": str(row.id),
    }


async def lock_owner(db: AsyncSession, tenant_id: str) -> Tenant:
    owner = await db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if owner is None:
        raise LookupError("Tenant not found")
    return owner


async def provider_for_owner(db: AsyncSession, tenant_id: str, name: str) -> TenantModelProvider:
    provider = await db.scalar(select(TenantModelProvider).where(TenantModelProvider.tenant_id == tenant_id, TenantModelProvider.provider_name == name))
    if provider is None:
        raise LookupError("Provider not found")
    return provider


async def instance_for_owner(db: AsyncSession, tenant_id: str, provider_name: str, instance_name: str) -> tuple[TenantModelProvider, TenantModelInstance]:
    provider = await provider_for_owner(db, tenant_id, provider_name)
    instance = await db.scalar(select(TenantModelInstance).where(TenantModelInstance.provider_id == provider.id, TenantModelInstance.instance_name == instance_name))
    if instance is None:
        raise LookupError("Instance not found")
    return provider, instance


async def add_provider(db: AsyncSession, tenant_id: str, name: str) -> dict[str, Any]:
    await lock_owner(db, tenant_id)
    if name not in {item["name"] for item in public_factories()}:
        raise LookupError("Provider not found")
    catalog(name)
    if await db.scalar(select(TenantModelProvider.id).where(TenantModelProvider.tenant_id == tenant_id, TenantModelProvider.provider_name == name)):
        raise ModelConflict("Provider already exists")
    provider = TenantModelProvider(id=uuid4().hex, tenant_id=tenant_id, provider_name=name, status="active")
    db.add(provider)
    await db.flush()
    return {"id": provider.id, "name": name, "status": provider.status}


async def add_model(db: AsyncSession, tenant_id: str, provider_name: str, instance_name: str, registration: ModelRegistration) -> dict[str, Any]:
    await lock_owner(db, tenant_id)
    provider, instance = await instance_for_owner(db, tenant_id, provider_name, instance_name)
    if await db.scalar(select(TenantModel.id).where(TenantModel.instance_id == instance.id, TenantModel.model_name == registration.model_name)):
        raise ModelConflict("Model already exists")
    row = TenantLLM(
        tenant_id=tenant_id,
        llm_factory=provider_name,
        llm_name="model:" + uuid4().hex,
        mdl_type=registration.model_types[0],
        api_key=instance.api_key,
        api_base=instance.base_url,
        max_tokens=registration.max_tokens,
        status="1",
    )
    db.add(row)
    await db.flush()
    model = TenantModel(id=row.id, instance_id=instance.id, model_name=registration.model_name, model_types=list(registration.model_types), is_tools=registration.is_tools)
    db.add(model)
    await db.flush()
    return model_projection(row, model, instance, provider)


def validate_key(provider_name: str, api_key: str) -> None:
    # Local servers and parser adapters may carry empty credentials. All other
    # structured provider envelopes are retained verbatim, without key echo.
    optional = {"Ollama", "VLLM", "LocalAI", "LM-Studio", "Xinference", "ModelScope", "Builtin", "MinerU", "PaddleOCR", "OpenDataLoader"}
    if not api_key.strip() and provider_name not in optional:
        raise ValueError("API key is required for this provider")
    if api_key and api_key != api_key.strip():
        raise ValueError("API key must not have surrounding whitespace")


def candidate_models(provider_name: str, request: ConnectionCandidate) -> list[ModelRegistration]:
    if provider_name not in {item["name"] for item in public_factories()}:
        raise LookupError("Provider not found")
    validate_key(provider_name, request.api_key)
    provider_transport(provider_name, request.api_key, request.base_url, request.region)
    models = catalog(provider_name, request.region)
    if request.model_ids is not None:
        available = {model.model_name for model in models}
        if not set(request.model_ids) <= available:
            raise ValueError("Selected model is not in the provider catalog")
        models = [model for model in models if model.model_name in request.model_ids]
    if request.custom_model is not None:
        if any(model.model_name == request.custom_model.model_name for model in models):
            raise ValueError("Custom model duplicates a selected catalog model")
        models.insert(0, request.custom_model)
    if not models:
        raise ValueError("Select a catalog model or supply a custom model for verification")
    return models


async def verify_candidate(db: AsyncSession, tenant_id: str, provider_name: str, request: ConnectionCandidate) -> dict[str, Any]:
    from api.db.services.provider_verification import verify_model

    if db.new or db.dirty or db.deleted:
        raise ValueError("Connection verification requires a clean transaction")
    models = candidate_models(provider_name, request)
    owner = await db.get(Tenant, tenant_id)
    if owner is None or owner.status != "1":
        raise LookupError("Tenant not found")
    provider = await db.scalar(select(TenantModelProvider).where(TenantModelProvider.tenant_id == tenant_id, TenantModelProvider.provider_name == provider_name))
    if provider is not None and provider.status != "active":
        raise ValueError("Provider is inactive")
    if provider is not None and isinstance(request, InstanceInput):
        if await db.scalar(select(TenantModelInstance.id).where(TenantModelInstance.provider_id == provider.id, TenantModelInstance.instance_name == request.instance_name)):
            raise ModelConflict("Instance already exists")
    # Read-only checks have taken no row locks. Release their transaction before
    # external I/O; creation rechecks owner/status/uniqueness under its write lock.
    await db.rollback()
    key, base, _region = provider_transport(provider_name, request.api_key, request.base_url, request.region)
    return await verify_model(provider_name, key, base, models[0])


async def create_instance(db: AsyncSession, tenant_id: str, provider_name: str, request: InstanceInput) -> dict[str, Any]:
    models = candidate_models(provider_name, request)
    await verify_candidate(db, tenant_id, provider_name, request)
    owner = await lock_owner(db, tenant_id)
    if owner.status != "1":
        raise LookupError("Tenant not found")
    provider = await db.scalar(select(TenantModelProvider).where(TenantModelProvider.tenant_id == tenant_id, TenantModelProvider.provider_name == provider_name))
    if provider is None:
        provider = TenantModelProvider(id=uuid4().hex, tenant_id=tenant_id, provider_name=provider_name, status="active")
        db.add(provider)
        await db.flush()
    if provider.status != "active":
        raise ValueError("Provider is inactive")
    if await db.scalar(select(TenantModelInstance.id).where(TenantModelInstance.provider_id == provider.id, TenantModelInstance.instance_name == request.instance_name)):
        raise ModelConflict("Instance already exists")
    instance = TenantModelInstance(
        id=uuid4().hex, provider_id=provider.id, instance_name=request.instance_name, api_key=request.api_key, base_url=request.base_url, region=request.region, status="active"
    )
    db.add(instance)
    await db.flush()
    for model in models:
        await add_model(db, tenant_id, provider_name, instance.instance_name, model)
    return instance_projection(instance, provider)


async def patch_instance(db: AsyncSession, tenant_id: str, provider_name: str, instance_name: str, request: InstancePatch) -> dict[str, Any]:
    await lock_owner(db, tenant_id)
    provider, instance = await instance_for_owner(db, tenant_id, provider_name, instance_name)
    provider_transport(
        provider_name,
        request.api_key if request.api_key else instance.api_key,
        request.base_url if request.base_url is not None else instance.base_url,
        request.region if request.region is not None else instance.region,
        stored=not bool(request.api_key),
    )
    if request.clear_api_key:
        validate_key(provider_name, "")
        instance.api_key = ""
    elif request.api_key:
        validate_key(provider_name, request.api_key)
        instance.api_key = request.api_key
    for field in ("base_url", "region", "status"):
        if field in request.model_fields_set:
            setattr(instance, field, getattr(request, field))
    # Keep the old row's credential snapshot useful for a reviewed rollback and
    # direct legacy readers. Runtime always applies the authoritative instance.
    ids = select(TenantModel.id).where(TenantModel.instance_id == instance.id)
    await db.execute(update(TenantLLM).where(TenantLLM.id.in_(ids), TenantLLM.tenant_id == tenant_id).values(api_key=instance.api_key, api_base=instance.base_url))
    await db.flush()
    return instance_projection(instance, provider)


async def models_for_owner(db: AsyncSession, tenant_id: str) -> list[tuple[TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider]]:
    query = (
        select(TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider)
        .join(TenantModel, TenantModel.id == TenantLLM.id)
        .join(TenantModelInstance, TenantModel.instance_id == TenantModelInstance.id)
        .join(TenantModelProvider, TenantModelInstance.provider_id == TenantModelProvider.id)
        .where(TenantLLM.tenant_id == tenant_id, TenantModelProvider.tenant_id == tenant_id)
        .order_by(TenantLLM.id)
    )
    rows = [tuple(row) for row in (await db.execute(query)).all()]
    return sorted(rows, key=lambda item: (*provider_order(item[3].provider_name), item[2].instance_name, item[1].model_name, item[0].id))


_REFERENCE_KEYS = {
    "model_id",
    "llm_id",
    "embd_id",
    "embedding_model",
    "rerank_id",
    "asr_id",
    "img2txt_id",
    "tts_id",
    "ocr_id",
    "parse_method",
    "layout_recognize",
    "layout_recognizer",
    "tenant_llm_id",
    "tenant_embd_id",
    "tenant_rerank_id",
    "tenant_img2txt_id",
    "tenant_asr_id",
    "tenant_tts_id",
    "tenant_ocr_id",
    "embedding_model_id",
    "rerank_model_id",
}


def _references(value: Any, identifiers: set[str], names: set[str], key: str = "") -> bool:
    if isinstance(value, dict):
        return any(_references(item, identifiers, names, "llm_id" if key == "search_config" and field == "chat_id" else field) for field, item in value.items())
    if isinstance(value, list):
        return any(_references(item, identifiers, names, key) for item in value)
    if key in {"dsl", "snapshot_dsl"} and isinstance(value, str):
        try:
            return _references(json.loads(value), identifiers, names)
        except (TypeError, ValueError):
            return False
    return key in _REFERENCE_KEYS and str(value) in identifiers | names


async def require_unreferenced(db: AsyncSession, rows: list[tuple[TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider]]) -> None:
    if not rows:
        return
    from api.db.db_models import API4Conversation, ChannelCanvasCandidate, Document, PipelineOperationLog, PythonSkillCoreConfig, SkillSearchConfig

    identifiers = {str(row.id) for row, *_ in rows}
    owners = {row.tenant_id for row, *_ in rows}
    names = {
        name
        for row, model, instance, provider in rows
        for name in (
            row.llm_name,
            model.model_name,
            f"{model.model_name}@{provider.provider_name}",
            f"{model.model_name}@{instance.instance_name}@{provider.provider_name}",
            f"{model.model_name}@{provider.provider_name}@{provider.provider_name}",
        )
        if name
    }
    # Numeric IDs are global identities, including explicitly shared consumers.
    # Name aliases are tenant-local and count only in actual model-ref fields.
    for entity in (
        Tenant,
        Knowledgebase,
        Document,
        Dialog,
        Memory,
        UserCanvas,
        UserCanvasVersion,
        Search,
        SkillSearchConfig,
        PythonSkillCoreConfig,
        API4Conversation,
        ChannelCanvasCandidate,
        PipelineOperationLog,
    ):
        for item in (await db.scalars(select(entity))).all():
            data = item.to_dict()
            item_owner = data.get("tenant_id", data.get("user_id"))
            if entity is UserCanvasVersion:
                item_owner = await db.scalar(select(UserCanvas.user_id).where(UserCanvas.id == item.user_canvas_id))
            if _references(data, identifiers, names if item_owner in owners else set()):
                raise ModelConflict("Model is referenced; update its consumers before deletion")


async def delete_models(db: AsyncSession, rows: list[tuple[TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider]]) -> None:
    await require_unreferenced(db, rows)
    identifiers = [row.id for row, *_ in rows]
    if identifiers:
        await db.execute(delete(TenantModel).where(TenantModel.id.in_(identifiers)))
        await db.execute(delete(TenantLLM).where(TenantLLM.id.in_(identifiers)))


async def delete_provider(db: AsyncSession, tenant_id: str, provider_name: str) -> None:
    await lock_owner(db, tenant_id)
    provider = await provider_for_owner(db, tenant_id, provider_name)
    rows = [row for row in await models_for_owner(db, tenant_id) if row[3].id == provider.id]
    await delete_models(db, rows)
    await db.execute(delete(TenantModelInstance).where(TenantModelInstance.provider_id == provider.id))
    await db.delete(provider)


async def delete_instance(db: AsyncSession, tenant_id: str, provider_name: str, instance_name: str) -> None:
    await lock_owner(db, tenant_id)
    _provider, instance = await instance_for_owner(db, tenant_id, provider_name, instance_name)
    rows = [row for row in await models_for_owner(db, tenant_id) if row[2].id == instance.id]
    await delete_models(db, rows)
    await db.delete(instance)


async def set_default(db: AsyncSession, tenant_id: str, model_type: str, model_id: int | None) -> None:
    owner = await lock_owner(db, tenant_id)
    field = DEFAULT_MODEL_FIELDS[model_type]
    if model_id is not None:
        rows = [row for row in await models_for_owner(db, tenant_id) if row[0].id == model_id]
        if not rows:
            raise LookupError("Model not found")
        projection = model_projection(*rows[0])
        if model_type not in projection["model_types"] or projection["effective_status"] != "active":
            raise ValueError("Model is not active for the requested capability")
    setattr(owner, field, str(model_id) if model_id is not None else "")
    setattr(owner, "tenant_" + field, model_id)
    await db.flush()


async def defaults_for_owner(db: AsyncSession, tenant_id: str) -> dict[str, Any]:
    owner = await db.get(Tenant, tenant_id)
    if owner is None:
        raise LookupError("Tenant not found")
    rows = {row[0].id: row for row in await models_for_owner(db, tenant_id)}
    models = []
    for capability, field in DEFAULT_MODEL_FIELDS.items():
        identifier = getattr(owner, "tenant_" + field)
        reference = getattr(owner, field) or ""
        row = rows.get(identifier)
        models.append(
            {
                "model_type": capability,
                "model_id": identifier,
                "reference": str(identifier) if row else reference,
                "model_provider": row[3].provider_name if row else "",
                "model_instance": row[2].instance_name if row else "",
                "model_name": row[1].model_name if row else reference,
            }
        )
    return {"models": models}


async def resolve_config(db: AsyncSession, tenant_id: str, model_id: int, model_type: str) -> dict[str, Any]:
    from api.db.services.tenant_llm_service import TenantLLMService

    matched = [row for row in await models_for_owner(db, tenant_id) if row[0].id == model_id]
    if matched:
        row, model, instance, _provider = matched[0]
        projection = model_projection(*matched[0])
        if projection["effective_status"] != "active" or model_type not in model.model_types:
            raise LookupError("Model is unavailable for the requested capability")
        config = row.to_dict()
        config.update(
            llm_name=model.model_name, api_key=instance.api_key, api_base=instance.base_url, mdl_type=model_type, model_types=list(model.model_types), is_tools=model.is_tools, region=instance.region
        )
        decoded = TenantLLMService.decode_model_config(config)
        decoded["is_tools"] = model.is_tools
        return decoded
    row = await db.scalar(select(TenantLLM).where(TenantLLM.id == model_id, TenantLLM.tenant_id == tenant_id, TenantLLM.mdl_type == model_type, TenantLLM.status == "1"))
    if row is None:
        raise LookupError("Model is unavailable for the requested capability")
    return TenantLLMService.decode_model_config(row.to_dict())
