"""MultiRAG deletion policy, kept separate from upstream model lifecycle writes.

This collector only reads the caller's transaction. Deletion, commit, rollback
and HTTP error mapping remain with model_provider_service and provider_api.
See docs/model-provider-lifecycle.md for the explicit upstream differences.
"""

import json
from typing import Any, TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Dialog, Knowledgebase, Memory, Search, Tenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider, UserCanvas, UserCanvasVersion


class ModelReference(TypedDict):
    resource_type: str
    resource_id: str | None
    resource_name: str | None
    model_names: list[str]
    is_deleted: bool


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


async def collect_blocking_model_references(db: AsyncSession, rows: list[tuple[TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider]]) -> list[ModelReference]:
    """Protect live consumers, including the snapshot serving published runs.

    Deleted resources and retained execution/version history are not ownership
    of a model configuration. Their stored content is never rewritten here.
    """
    if not rows:
        return []
    from api.db.db_models import Document, PythonSkillCoreConfig, SkillSearchConfig

    identifiers = {str(row.id) for row, *_ in rows}
    owners = {row.tenant_id for row, *_ in rows}
    aliases: dict[int | None, set[str]] = {
        row.id: {
            name
            for name in (
                row.llm_name,
                model.model_name,
                f"{model.model_name}@{provider.provider_name}",
                f"{model.model_name}@{instance.instance_name}@{provider.provider_name}",
                f"{model.model_name}@{provider.provider_name}@{provider.provider_name}",
            )
            if name
        }
        for row, model, instance, provider in rows
    }
    names = set().union(*aliases.values())
    references: list[ModelReference] = []
    datasets: dict[str, Knowledgebase] = {}
    canvases: dict[str, UserCanvas] = {}
    published_times: dict[str, int | None] = {}
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
    ):
        query = select(entity)
        if entity is UserCanvasVersion:
            query = query.where(UserCanvasVersion.release.is_(True))
        for item in (await db.scalars(query)).all():
            data = item.to_dict()
            item_owner = data.get("tenant_id", data.get("user_id"))
            if isinstance(item, Knowledgebase):
                datasets[item.id] = item
            if isinstance(item, UserCanvas):
                canvases[item.id] = item
            if getattr(item, "status", None) == "0":
                continue
            if isinstance(item, Document):
                dataset = datasets.get(item.kb_id)
                if dataset is None or dataset.status == "0":
                    continue
                item_owner = dataset.tenant_id
            if isinstance(item, UserCanvasVersion):
                canvas = canvases.get(item.user_canvas_id)
                if canvas is None:
                    continue
                if item.user_canvas_id not in published_times:
                    published_times[item.user_canvas_id] = await db.scalar(
                        select(func.max(UserCanvasVersion.create_time)).where(UserCanvasVersion.user_canvas_id == item.user_canvas_id, UserCanvasVersion.release.is_(True))
                    )
                # Tied or missing timestamps cannot prove which snapshot the
                # published runner selects, so retain protection for each one.
                if item.create_time is not None and published_times[item.user_canvas_id] != item.create_time:
                    continue
                item_owner = canvas.user_id
            if _references(data, identifiers, names if item_owner in owners else set()):
                # Return only a whitelist of display metadata. Numeric model IDs
                # may be shared across owners; their consumers must stay private.
                visible = item_owner in owners
                model_names = sorted({model.model_name for row, model, *_ in rows if _references(data, {str(row.id)}, aliases[row.id] if item_owner in owners else set())})
                references.append(
                    {
                        "resource_type": entity.__name__ if visible else "SharedResource",
                        "resource_id": str(item.id) if visible else None,
                        "resource_name": (data.get("name") or data.get("title")) if visible else None,
                        "model_names": model_names,
                        "is_deleted": visible and data.get("status") == "0",
                    }
                )
    references.sort(key=lambda item: (item["resource_type"], item["resource_name"] or "", item["resource_id"] or ""))
    return references
