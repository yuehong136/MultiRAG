from typing import Dict, Any, List

from fastapi import APIRouter, Body

from api.service.ai_translate_service.ai_translate_service import AITranslateService

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


class NL2SQLReqBody(BaseModel):
    user_question: str
    table_structure: str


class ChartTypeReqBody(BaseModel):
    user_question: str
    sql_result: Dict[str, Any]


class DynamicChartOptionFunctionReqBody(BaseModel):
    user_question: str
    sql_result: Dict[str, Any]
    chart_type: str


class AITranslateReqBody(BaseModel):
    zh_text: str


@router.post("/ai-translate")
async def ai_translate(body: AITranslateReqBody = Body(...)):
    translate = await AITranslateService.ai_translate(ai_translate_req_body=body)
    return ResponseSchema(status="success", data={
        "zh_text": body.zh_text,
        "en_text": translate
    })


class AIBatchTranslateReqBody(BaseModel):
    zh_text_list: List[str]


@router.post("/ai-batch-translate")
async def ai_translate(body: AIBatchTranslateReqBody = Body(...)):
    translate_list = await AITranslateService.ai_batch_translate(ai_batch_translate_req_body=body)
    return ResponseSchema(status="success", data={
        "zh_text_list": body.zh_text_list,
        "en_text_list": translate_list
    })
