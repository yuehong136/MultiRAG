"""RESTful Langfuse credential endpoints mounted under ``/api/v1``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.apps import langfuse_app as legacy
from api.apps import manager
from api.db.db_models import get_db

router = APIRouter()


@router.post("/langfuse/api-key", summary="设置 Langfuse API 密钥")
@router.put("/langfuse/api-key", summary="更新 Langfuse API 密钥")
def set_api_key(
    request: legacy.LangfuseKeysRequest,
    db: Session = Depends(get_db),
    user: Any = Depends(manager),
) -> Any:
    return legacy.set_api_key(request=request, db=db, user=user)


@router.get("/langfuse/api-key", summary="获取 Langfuse API 密钥")
def get_api_key(
    db: Session = Depends(get_db),
    user: Any = Depends(manager),
) -> Any:
    return legacy.get_api_key(db=db, user=user)


@router.delete("/langfuse/api-key", summary="删除 Langfuse API 密钥")
def delete_api_key(
    db: Session = Depends(get_db),
    user: Any = Depends(manager),
) -> Any:
    return legacy.delete_api_key(db=db, user=user)
