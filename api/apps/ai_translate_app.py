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
    """
    将中文文本翻译成英文的接口。

    概要：使用AI翻译服务将中文文本翻译为英文文本。
    响应描述：返回包含翻译结果的英文文本。

    参数：
    - body (AITranslateReqBody): 包含中文文本和模型名称的请求体。

    返回：
    - ResponseSchema: 返回包含原始中文文本和翻译后的英文文本的JSON结果。

    功能：
    1. 使用指定的语言模型将中文文本翻译成英文。
    2. 返回翻译后的英文文本，供前端用户查看。

    注意：
    - 提供的中文文本应准确无误，以便获得正确的翻译结果。
    """
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
    """
    批量将中文文本翻译成英文的接口。

    概要：使用AI翻译服务批量将多个中文文本翻译为英文文本。
    响应描述：返回包含批量翻译结果的英文文本列表。

    参数：
    - body (AIBatchTranslateReqBody): 包含中文文本列表和模型名称的请求体。

    返回：
    - ResponseSchema: 返回包含原始中文文本列表和翻译后的英文文本列表的JSON结果。

    功能：
    1. 使用指定的语言模型批量将中文文本列表翻译成英文。
    2. 返回所有翻译后的英文文本，以供进一步处理或查看。

    注意：
    - 输入的中文文本列表不应为空，以确保翻译服务能够正常处理。
    """
    translate_list = await AITranslateService.ai_batch_translate(ai_batch_translate_req_body=body, db=db,
                                                                 user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(status="success", data={
        "zh_text_list": body.zh_text_list,
        "en_text_list": translate_list
    })
