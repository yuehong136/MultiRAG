from typing import Dict, Any, List

from fastapi import APIRouter, Body, Depends

from api.apps import manager
from api.db.database import get_db
from api.service.ai_translate_service.ai_translate_service import AITranslateService
from sqlalchemy.orm import Session

router = APIRouter()

from enum import Enum

from pydantic import BaseModel
from typing import Any, Optional


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: Optional[str] = None
    data: Optional[Any] = None


class AITranslateReqBody(BaseModel):
    zh_text: str
    llm_name: str


@router.post("/ai-translate")
async def ai_translate(body: AITranslateReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    translate = await AITranslateService.ai_translate(ai_translate_req_body=body, db=db, user_id=user.id,
                                                      llm_name=body.llm_name)
    return ResponseSchema(status="success", data={
        "zh_text": body.zh_text,
        "en_text": translate
    })


class AIBatchTranslateReqBody(BaseModel):
    zh_text_list: List[str]
    llm_name: str


@router.post("/ai-batch-translate")
async def ai_translate(body: AIBatchTranslateReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    translate_list = await AITranslateService.ai_batch_translate(ai_batch_translate_req_body=body, db=db,
                                                                 user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(status="success", data={
        "zh_text_list": body.zh_text_list,
        "en_text_list": translate_list
    })
