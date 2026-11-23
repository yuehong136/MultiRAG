"""MultiRAG deletion policy, kept separate from upstream model lifecycle writes.

This collector only reads the caller's transaction. Deletion, commit, rollback
and HTTP error mapping remain with model_provider_service and provider_api.
See docs/model-provider-lifecycle.md for the explicit upstream differences.
"""

import json
from typing import Any, TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Dialog, Knowledgebase, Memory, Search, Tenant, UserCanvas, UserCanvasVersion
from api.db.services.provider_model_resolution import ModelRecord


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


def _references(value: Any, identifiers: set[str], key: str = "") -> bool:
    if isinstance(value, dict):
        return any(_references(item, identifiers, "llm_id" if key == "search_config" and field == "chat_id" else field) for field, item in value.items())
    if isinstance(value, list):
        return any(_references(item, identifiers, key) for item in value)
    if key in {"dsl", "snapshot_dsl"} and isinstance(value, str):
        try:
            return _references(json.loads(value), identifiers)
        except (TypeError, ValueError):
            return False
    return key in _REFERENCE_KEYS and isinstance(value, str) and value in identifiers


async def collect_blocking_model_references(db: AsyncSession, rows: list[ModelRecord]) -> list[ModelReference]:
    """Protect live consumers, including the snapshot serving published runs.

    Deleted resources and retained execution/version history are not ownership
    of a model configuration. Their stored content is never rewritten here.
    """
    if not rows:
        return []
    from api.db.db_models import Document, PythonSkillCoreConfig, SkillSearchConfig

    identifiers = {model.id for model, _, _ in rows}
    owners = {provider.tenant_id for _, _, provider in rows}
    references: list[ModelReference] = []
    datasets: dict[str, Knowledgebase] = {}
    canvases: dict[str, UserCanvas] = {}
    published_times: dict[str, int | None] = {}
    # Numeric IDs are global identities, including explicitly shared consumers.
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
            if _references(data, identifiers):
                # Return only a whitelist of display metadata. Model IDs
                # may be shared across owners; their consumers must stay private.
                visible = item_owner in owners
                model_names = sorted({model.model_name for model, _, _ in rows if _references(data, {model.id})})
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
