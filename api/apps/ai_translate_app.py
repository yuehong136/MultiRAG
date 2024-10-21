from typing import List

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


@router.post("/ai-translate", summary="翻译中文文本", response_description="成功返回英文翻译结果")
async def ai_translate(body: AITranslateReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    AI翻译接口

    此接口接收中文文本和指定的语言模型名称，返回对应的英文翻译。

    参数:
    - body: AITranslateReqBody
      - zh_text: 待翻译的中文文本
      - llm_name: 使用的语言模型名称
    - db: 数据库会话，由FastAPI依赖注入
    - user: 当前用户，由FastAPI依赖注入

    返回:
    - ResponseSchema对象，包含:
      - status: 操作状态（成功/失败）
      - data: 包含原中文文本和翻译后的英文文本
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


@router.post("/ai-batch-translate", summary="批量翻译中文文本", response_description="成功返回英文翻译结果列表")
async def ai_translate(body: AIBatchTranslateReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    AI批量翻译接口

    此接口接收多个中文文本和指定的语言模型名称，返回对应的英文翻译列表。

    参数:
    - body: AIBatchTranslateReqBody
      - zh_text_list: 待翻译的中文文本列表
      - llm_name: 使用的语言模型名称
    - db: 数据库会话，由FastAPI依赖注入
    - user: 当前用户，由FastAPI依赖注入

    返回:
    - ResponseSchema对象，包含:
      - status: 操作状态（成功/失败）
      - data: 包含原中文文本列表和翻译后的英文文本列表
    """
    translate_list = await AITranslateService.ai_batch_translate(ai_batch_translate_req_body=body, db=db,
                                                                 user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(status="success", data={
        "zh_text_list": body.zh_text_list,
        "en_text_list": translate_list
    })
