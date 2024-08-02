# coding=utf-8
"""
@project: multirag
@Author：龙
@file： dialog_app.py
@date：2024/7/17 16:00
@desc: 对话管理接口
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional

from api.apps import manager
from api.db.services.dialog_service import DialogService
from api.db import StatusEnum
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService
from api.utils.api_utils import server_error_response, get_data_error_result, validate_request
from api.utils import get_uuid
from api.utils.api_utils import get_json_result
from api.db.database import get_db

router = APIRouter()

class DialogRequest(BaseModel):
    dialog_id: Optional[str] = None
    """对话的唯一标识符，如果为空则表示创建新对话。"""

    name: Optional[str] = "New Dialog"
    """对话的名称，默认值为 'New Dialog'。"""

    description: Optional[str] = "A helpful Dialog"
    """对话的描述，默认值为 'A helpful Dialog'。"""

    icon: Optional[str] = ""
    """对话的图标URL，默认值为空字符串。"""

    top_n: Optional[int] = 6
    """从知识库中返回的最大条目数，默认值为 6。"""

    top_k: Optional[int] = 1024
    """从知识库中检索的最大条目数，默认值为 1024。"""

    rerank_id: Optional[str] = ""
    """重新排序的ID，默认值为空字符串。"""

    similarity_threshold: Optional[float] = 0.1
    """相似度阈值，默认值为 0.1。"""

    vector_similarity_weight: Optional[float] = 0.3
    """向量相似度权重，默认值为 0.3。"""

    llm_id: Optional[str] = ""
    """大语言模型的ID，默认值为空字符串。"""

    llm_setting: Optional[dict] = {}
    """大语言模型的配置，默认值为空字典。"""

    prompt_config: Optional[dict] = {
        "system": """你是一个智能助手，请总结知识库的内容来回答问题，请列举知识库中的数据详细回答。当所有知识库内容都与问题无关时，你的回答必须包括“知识库中未找到您要的答案！”这句话。回答需要考虑聊天历史。
以下是知识库：
{knowledge}
以上是知识库。""",
        "prologue": "您好，我是您的助手小樱，长得可爱又善良，can I help you?",
        "parameters": [
            {"key": "knowledge", "optional": False}
        ],
        "empty_response": "Sorry! 知识库中未找到相关内容！"
    }
    """提示配置，包含系统提示、开场白、参数和空响应消息。"""

    kb_ids: Optional[List[str]] = []
    """知识库的ID列表，默认值为空列表。"""

class RemoveDialogRequest(BaseModel):
    dialog_ids: List[str]
    """要删除的对话ID列表。"""

def get_kb_names(kb_ids, db: Session):
    ids, nms = [], []
    for kid in kb_ids:
        kb = KnowledgebaseService.get_by_id(db, kid)
        if not kb or kb.status != StatusEnum.VALID.value:
            continue
        ids.append(kid)
        nms.append(kb.name)
    return ids, nms

@router.post('/set', summary="设置对话", response_description="成功设置对话")
async def set_dialog(request: DialogRequest, db: Session = Depends(get_db), req: Request = None, user=Depends(manager)):
    """
    设置对话

    该接口用于创建或更新对话信息。

    参数:
    - request: DialogRequest对象，包含对话的配置信息
        - dialog_id: 对话的唯一标识符，如果为空则表示创建新对话
        - name: 对话的名称，默认值为 'New Dialog'
        - description: 对话的描述，默认值为 'A helpful Dialog'
        - icon: 对话的图标URL，默认值为空字符串
        - top_n: 从知识库中返回的最大条目数，默认值为 6
        - top_k: 从知识库中检索的最大条目数，默认值为 1024
        - rerank_id: 重新排序的ID，默认值为空字符串
        - similarity_threshold: 相似度阈值，默认值为 0.1
        - vector_similarity_weight: 向量相似度权重，默认值为 0.3
        - llm_id: 大语言模型的ID，默认值为空字符串
        - llm_setting: 大语言模型的配置，默认值为空字典
        - prompt_config: 提示配置，包含系统提示、开场白、参数和空响应消息
            - system: 系统提示模板
            - prologue: 开场白
            - parameters: 参数列表，每个参数包含键值和是否可选
            - empty_response: 空响应消息
        - kb_ids: 知识库的ID列表，默认值为空列表
    - db: 数据库会话对象
    - req: 请求对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含对话信息的JSON结果
    - 失败时返回错误信息
    """
    try:
        prompt_config = request.prompt_config or {
            "system": """你是一个智能助手，请总结知识库的内容来回答问题，请列举知识库中的数据详细回答。当所有知识库内容都与问题无关时，你的回答必须包括“知识库中未找到您要的答案！”这句话。回答需要考虑聊天历史。
以下是知识库：
{knowledge}
以上是知识库。""",
            "prologue": "您好，我是您的助手小樱，长得可爱又善良，can I help you?",
            "parameters": [
                {"key": "knowledge", "optional": False}
            ],
            "empty_response": "Sorry! 知识库中未找到相关内容！"
        }

        for p in prompt_config["parameters"]:
            if p["optional"]:
                continue
            if prompt_config["system"].find("{%s}" % p["key"]) < 0:
                return get_data_error_result(
                    retmsg="Parameter '{}' is not used".format(p["key"]))

        tenant = TenantService.get_by_id(db, user.id)
        if not tenant:
            return get_data_error_result(retmsg="Tenant not found!")

        llm_id = request.llm_id or tenant.llm_id
        if not request.dialog_id:
            dia = {
                "id": get_uuid(),
                "tenant_id": user.id,
                "name": request.name,
                "kb_ids": request.kb_ids,
                "description": request.description,
                "llm_id": llm_id,
                "llm_setting": request.llm_setting,
                "prompt_config": prompt_config,
                "top_n": request.top_n,
                "top_k": request.top_k,
                "rerank_id": request.rerank_id,
                "similarity_threshold": request.similarity_threshold,
                "vector_similarity_weight": request.vector_similarity_weight,
                "icon": request.icon
            }
            if not DialogService.save(db, **dia):
                return get_data_error_result(retmsg="Fail to new a dialog!")
            dia = DialogService.get_by_id(db, dia["id"])
            if not dia:
                return get_data_error_result(retmsg="Fail to new a dialog!")
            dia = dia.to_dict()
            return get_json_result(data=dia)
        else:
            update_data = request.dict(exclude_unset=True)
            del update_data["dialog_id"]
            if "kb_names" in update_data:
                del update_data["kb_names"]
            if not DialogService.update_by_id(db, request.dialog_id, update_data):
                return get_data_error_result(retmsg="Dialog not found!")
            dia = DialogService.get_by_id(db, request.dialog_id)
            if not dia:
                return get_data_error_result(retmsg="Fail to update a dialog!")
            dia = dia.to_dict()
            dia["kb_ids"], dia["kb_names"] = get_kb_names(dia["kb_ids"], db)
            return get_json_result(data=dia)
    except Exception as e:
        return server_error_response(e)

@router.get('/get', summary="获取对话", response_description="成功获取对话")
async def get(dialog_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取对话

    该接口用于获取指定对话的信息。

    参数:
    - dialog_id: str 对话的唯一标识符
    - db: Session 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含对话信息的JSON结果
    - 失败时返回错误信息
    """
    try:
        dia = DialogService.get_by_id(db, dialog_id)
        if not dia:
            return get_data_error_result(retmsg="Dialog not found!")
        dia = dia.to_dict()
        dia["kb_ids"], dia["kb_names"] = get_kb_names(dia["kb_ids"], db)
        return get_json_result(data=dia)
    except Exception as e:
        return server_error_response(e)

@router.get('/list', summary="列出对话", response_description="成功列出对话")
async def list_dialogs(db: Session = Depends(get_db), user=Depends(manager)):
    """
    列出对话

    该接口用于列出当前用户的所有对话。

    参数:
    - db: Session 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回包含对话列表的JSON结果
    - 失败时返回错误信息
    """
    try:
        diags = DialogService.query(
            db,
            tenant_id=user.id,
            status=StatusEnum.VALID.value,
            reverse=True,
            order_by=DialogService.model.create_time)
        diags = [d.to_dict() for d in diags]
        for d in diags:
            d["kb_ids"], d["kb_names"] = get_kb_names(d["kb_ids"], db)
        return get_json_result(data=diags)
    except Exception as e:
        return server_error_response(e)

@router.post('/rm', summary="删除对话", response_description="成功删除对话")
async def rm(request: RemoveDialogRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    删除对话

    该接口用于删除指定的对话。

    参数:
    - request: RemoveDialogRequest对象，包含要删除的对话ID列表
        - dialog_ids: List[str] 要删除的对话ID列表
    - db: Session 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回成功删除的JSON结果
    - 失败时返回错误信息
    """
    try:
        DialogService.update_many_by_id(
            db, [{"id": id, "status": StatusEnum.INVALID.value} for id in request.dialog_ids])
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)