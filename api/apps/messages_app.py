#
#  Copyright 2025 The MultiRAG Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.joint_services import memory_message_service
from api.db.services.memory_service import MemoryService
from api.utils.api_utils import get_json_result
from common.constants import RetCode
from common.time_utils import current_timestamp, timestamp_to_date
from memory.services.messages import MessageService

logger = logging.getLogger("multirag.messages_app")

router = APIRouter()

class AddMessageRequest(BaseModel):
    memory_id: str | list[str] = Field(..., description="Memory ID or list of Memory IDs")
    agent_id: str = Field(..., description="Agent ID")
    session_id: str = Field(..., description="Session ID")
    user_input: str = Field(..., description="User input message")
    agent_response: str = Field(..., description="Agent response message")
    user_id: str = Field(default="", description="Optional User ID")


class UpdateMessageRequest(BaseModel):
    status: bool = Field(..., description="Message status (True=active, False=inactive)")


class SearchMessageRequest(BaseModel):
    memory_id: list[str] = Field(..., description="List of Memory IDs")
    query: str = Field(..., description="Search query")
    similarity_threshold: float = Field(default=0.2, description="Minimum similarity threshold")
    keywords_similarity_weight: float = Field(default=0.7, description="Weight for keyword similarity")
    top_n: int = Field(default=5, description="Maximum number of results")
    agent_id: str = Field(default="", description="Optional Agent ID filter")
    session_id: str = Field(default="", description="Optional Session ID filter")


@router.post("", summary="添加消息到记忆")
async def add_message(
    request: AddMessageRequest,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    """Add messages to memory."""
    memory_ids = request.memory_id
    if isinstance(memory_ids, str):
        memory_ids = [memory_ids]

    res = []
    for memory_id in memory_ids:
        success, msg = await memory_message_service.save_to_memory(
            db,
            memory_id,
            {
                "user_id": request.user_id,
                "agent_id": request.agent_id,
                "session_id": request.session_id,
                "user_input": request.user_input,
                "agent_response": request.agent_response,
            },
        )
        res.append({"memory_id": memory_id, "success": success, "message": msg})

    if all([r["success"] for r in res]):
        return get_json_result(retmsg="Successfully added to memories.")

    return get_json_result(retcode=RetCode.SERVER_ERROR, retmsg="Some messages failed to add.", data=res)


@router.delete("/{memory_id}:{message_id}", summary="忘记（软删除）消息")
async def forget_message(
    memory_id: str,
    message_id: int,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    """Forget (soft delete) a message."""
    memory = MemoryService.get_by_memory_id(db, memory_id)
    if not memory:
        return get_json_result(retcode=RetCode.NOT_FOUND, retmsg=f"Memory '{memory_id}' not found.")

    forget_time = timestamp_to_date(current_timestamp())
    update_succeed = MessageService.update_message(
        {"memory_id": memory_id, "message_id": int(message_id)},
        {"forget_at": forget_time},
        memory.tenant_id,
        memory_id,
    )
    if update_succeed:
        return get_json_result(retmsg=update_succeed)
    else:
        return get_json_result(retcode=RetCode.SERVER_ERROR, retmsg=f"Failed to forget message '{message_id}' in memory '{memory_id}'.")


@router.put("/{memory_id}:{message_id}", summary="更新消息状态")
async def update_message(
    memory_id: str,
    message_id: int,
    request: UpdateMessageRequest,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    """Update message status."""
    from api.db.services.memory_service import MemoryService

    memory = MemoryService.get_by_memory_id(db, memory_id)
    if not memory:
        return get_json_result(retcode=RetCode.NOT_FOUND, retmsg=f"Memory '{memory_id}' not found.")

    update_succeed = MessageService.update_message(
        {"memory_id": memory_id, "message_id": int(message_id)},
        {"status": request.status},
        memory.tenant_id,
        memory_id,
    )
    if update_succeed:
        return get_json_result(retmsg=update_succeed)
    else:
        return get_json_result(retcode=RetCode.SERVER_ERROR, retmsg=f"Failed to set status for message '{message_id}' in memory '{memory_id}'.")


@router.get("/search", summary="搜索记忆中的消息")
async def search_message(
    memory_id: list[str] = Query(..., description="List of Memory IDs"),
    query: str = Query(..., description="Search query"),
    similarity_threshold: float = Query(default=0.2, description="Minimum similarity threshold"),
    keywords_similarity_weight: float = Query(default=0.7, description="Weight for keyword similarity"),
    top_n: int = Query(default=5, description="Maximum number of results"),
    agent_id: str = Query(default="", description="Optional Agent ID filter"),
    session_id: str = Query(default="", description="Optional Session ID filter"),
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    """Search messages in memory."""
    if len(memory_id) == 1 and ',' in memory_id[0]:
        memory_id = memory_id[0].split(',')
    filter_dict = {"memory_id": memory_id, "agent_id": agent_id, "session_id": session_id}
    params = {
        "query": query,
        "similarity_threshold": similarity_threshold,
        "keywords_similarity_weight": keywords_similarity_weight,
        "top_n": top_n,
    }
    res = memory_message_service.query_message(db, filter_dict, params)
    return get_json_result(retmsg=True, data=res)


@router.get("", summary="获取最近的消息")
async def get_messages(
    memory_id: list[str] = Query(..., description="List of Memory IDs"),
    agent_id: str = Query(default="", description="Agent ID"),
    session_id: str = Query(default="", description="Session ID"),
    limit: int = Query(default=10, description="Maximum number of messages"),
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    """Get recent messages from memory."""
    if len(memory_id) == 1 and ',' in memory_id[0]:
        memory_id = memory_id[0].split(',')
    from api.db.services.memory_service import MemoryService

    memory_list = MemoryService.get_by_ids(db, memory_id)
    uids = [memory.tenant_id for memory in memory_list]
    res = MessageService.get_recent_messages(uids, memory_id, agent_id, session_id, limit)
    return get_json_result(retmsg=True, data=res)


@router.get("/{memory_id}:{message_id}/content", summary="获取消息内容")
async def get_message_content(
    memory_id: str,
    message_id: int,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    """Get message content by ID."""
    from api.db.services.memory_service import MemoryService

    memory = MemoryService.get_by_memory_id(db, memory_id)
    if not memory:
        return get_json_result(retcode=RetCode.NOT_FOUND, retmsg=f"Memory '{memory_id}' not found.")

    res = MessageService.get_by_message_id(memory_id, message_id, memory.tenant_id)
    if res:
        return get_json_result(retmsg=True, data=res)
    else:
        return get_json_result(retcode=RetCode.NOT_FOUND, retmsg=f"Message '{message_id}' in memory '{memory_id}' not found.")
