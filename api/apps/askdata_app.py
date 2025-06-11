import logging
import uuid
from enum import Enum
from http.client import HTTPException
from typing import List, Any, Dict, Optional

from fastapi import APIRouter, Depends, Body, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.db.db_models import get_db
from api.apps import manager
from api.service.askdata_service.askdata_service import AskdataService, get_askdata_service
from api.service.askdata_service.async_llm_service import AsyncLLMService
from api.service.askdata_service.event.event_handlers import create_sse_response
from api.service.askdata_service.pg_query_formatter import execute_sql_and_format_result

router = APIRouter()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: str | None = None
    data: Any | None = None


@router.get("/health", response_model=ResponseSchema, summary="健康检查接口")
async def health_check():
    """
    健康检查接口，无需任何输入参数

    返回服务状态信息
    """
    try:
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="服务运行正常",
            data={
                "service": "nl2sql-service",
                "status": "healthy",
                "timestamp": "2024-06-04T00:00:00Z"
            }
        )
    except Exception as e:
        logger.exception("健康检查发生异常")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"健康检查失败：{str(e)}"
        )


class GetSqlAndTableConfigReq(BaseModel):
    """自然语言转初始SQL请求的基础模型"""
    user_query: str = Field(..., title="查询文本", description="用户提出的自然语言查询文本")
    dataset_id_list: List[str] = Field([], title="数据集ID列表", description="数据集ID列表")
    llm_name: str = Field("gpt-4", title="LLM模型名称", description="用于将自然语言转换为SQL的LLM模型名称")
    conversation_id: str = Field(None, title="conversation_id", description="conversation_id")
    ask_id: str = Field(None, title="ask_id", description="用户的提问ID")
    semantic_layer: Dict[str, Any] = Field({}, title="语义层", description="语义层")


@router.post("/get-sql-and-table-config", response_model=ResponseSchema)
async def get_sql_and_table_config(
        body: GetSqlAndTableConfigReq = Body(
            ...,
            title="涵盖了SQL生成、表格配置、查询执行的全过程",
            description="涵盖了SQL生成、表格配置、查询执行的全过程"
        ),
        db: Session = Depends(get_db), user=Depends(manager),
        service: AskdataService = Depends(get_askdata_service)
):
    logging.info(f"get-sql-and-table-config请求体：{body}")

    try:
        sql, used_models = await service.nlq_to_initial_sql(
            user_query=body.user_query,
            llm_name=body.llm_name,
            semantic_layer=body.semantic_layer['processed_semantic_layer']
        )

        table_config = await service.generate_table_config(sql, body.dataset_id_list,
                                                           model_ids=body.semantic_layer['model_ids'],
                                                           used_models=used_models)

        # 执行查询
        result = execute_sql_and_format_result(sql=sql, db_config={})

        if sql:
            return ResponseSchema(
                status=StatusEnum.SUCCESS,
                message="生成初始SQL成功",
                data={"sql": sql, "result": result, "table_config": table_config}
            )
        else:
            return ResponseSchema(
                status=StatusEnum.ERROR,
                message="SQL生成失败或验证未通过"
            )

    except Exception as e:
        logger.exception("nlq-to-initial-sql发生异常")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"生成初始SQL失败：{str(e)}"
        )


from fastapi import Request
from fastapi.responses import StreamingResponse


@router.get("/events/{event_id}")
async def subscribe_to_event(request: Request, event_id: str):
    """
    订阅指定事件ID的SSE端点

    Args:
        request: FastAPI请求对象
        event_id: 事件ID

    Returns:
        StreamingResponse: SSE流响应
    """
    return create_sse_response(request, event_id)


class AnalyzeUserQueryRequest(BaseModel):
    conversation_id: str = Field(..., description="会话ID")
    ask_id: str = Field(..., description="用户的提问ID")
    user_query: str = Field(..., description="用户查询")
    llm_name: Optional[str] = Field(default=None, description="指定使用的模型名称")
    dataset_id_list: List[str] = Field([], title="数据集ID列表", description="数据集ID列表")
    semantic_layer: Dict[str, Any] = Field(..., title="语义层", description="语义层")


class AnalyzeUserQueryResponse(BaseModel):
    event_id: str = Field(..., description="事件ID，用于监听流式输出")
    subscribe_url: str = Field(..., description="SSE订阅地址")
    chat_status: str = Field(default="started", description="聊天状态")


async def analyze_user_query_background_task(
        event_id: str,
        request: AnalyzeUserQueryRequest,
        db: Session,
        user
) -> None:
    """
    处理流式聊天的后台任务

    Args:
        event_id: 事件ID
        request: 聊天请求
        db: 数据库会话
    """
    try:
        # 创建异步LLM服务
        llm_service = AsyncLLMService(db)

        # 转换消息格式
        history = [{"role": "user", "content": "写一个500字的笑话"}]

        gen_conf = {
            "temperature": 0.7,
            "max_tokens": 2000,
        }

        # 执行流式聊天
        await llm_service.chat_stream_async(
            event_id=event_id,
            tenant_id=user.id,
            history=history,
            gen_conf=gen_conf,
            llm_name=request.llm_name
        )

    except Exception as e:
        logger.exception(f"Background chat task failed for event_id {event_id}: {e}")
        # 发送错误事件
        try:
            from api.service.nl2sql_service.event.event_manager import event_manager
            await event_manager.publish(
                event_id=event_id,
                data={
                    "message": f"后台任务失败: {str(e)}",
                    "error": str(e),
                    "status": "task_error"
                },
                event_type="chat_error"
            )
        except Exception as publish_error:
            logger.error(f"Failed to send error event for {event_id}: {publish_error}")


@router.post("/analyze-user-query-streaming/{custom_event_id}", response_model=ResponseSchema,
             summary="使用自定义事件ID启动流式聊天")
async def analyze_user_query_streaming(
        custom_event_id: str,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        user=Depends(manager),
        body: AnalyzeUserQueryRequest = Body(
            ...,
            title="流式输出对于用户问题的分析",
            description="流式输出对于用户问题的分析"
        )
) -> ResponseSchema:
    """
    使用自定义事件ID启动流式聊天

    Args:
        custom_event_id: 自定义的事件ID
        body: 聊天请求参数
        background_tasks: FastAPI后台任务
        db: 数据库会话
        user: 当前用户

    Returns:
        ResponseSchema: 包含事件ID和监听地址的响应
    """
    logger.info(f"使用自定义事件ID {custom_event_id} 启动流式聊天：{body}")

    try:
        # 添加后台任务
        background_tasks.add_task(
            analyze_user_query_background_task,
            custom_event_id,
            body,
            db,
            user
        )

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="聊天请求已提交，请监听事件流获取实时结果",
            data=AnalyzeUserQueryResponse(
                event_id=custom_event_id,
                subscribe_url=f"/events/{custom_event_id}",
                chat_status="started"
            )
        )

    except Exception as e:
        logger.exception("使用自定义事件ID启动流式聊天失败")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"启动聊天失败：{str(e)}"
        )


class SemanticLayerRequest(BaseModel):
    conversation_id: str = Field(..., description="会话ID")
    ask_id: str = Field(..., description="用户的提问ID")
    user_query: str = Field(..., description="用户查询")
    dataset_id_list: List[str] = Field(
        [],
        title="数据集ID列表",
        description="数据集ID列表",
    )


@router.post("/get-semantic-layer-streaming/{custom_event_id}", response_model=ResponseSchema,
             summary="获得语义层信息")
async def get_semantic_layer_streaming(
        custom_event_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager),
        body: SemanticLayerRequest = Body(
            ...,
            title="获得语义层信息",
            description="获得语义层信息"
        ),
        service: AskdataService = Depends(get_askdata_service)
) -> ResponseSchema:
    logger.info(f"使用自定义事件ID {custom_event_id} 获得语义层信息，参数：{body}")

    try:
        processed_semantic_layer, model_ids = await service.generate_semantic_layer(user_query=body.user_query,
                                                                                    dataset_id_list=body.dataset_id_list,
                                                                                    conversation_id=body.conversation_id,
                                                                                    event_id=custom_event_id)

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="获得语义层信息成功",
            data={"processed_semantic_layer": processed_semantic_layer, "model_ids": model_ids}
        )

    except Exception as e:
        logger.exception("使用自定义事件ID启动流式聊天失败")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"启动聊天失败：{str(e)}"
        )

# --- 新增接口 ---

class AddHistoryRequest(BaseModel):
    conversation_id: str = Field(..., description="会话ID")
    ask_id: str = Field(..., description="询问ID")
    data: str = Field(..., description="历史记录内容, 可以是JSON字符串")


@router.post("/add-history", response_model=ResponseSchema, summary="新增历史记录")
async def add_history(
    body: AddHistoryRequest,
    service: AskdataService = Depends(get_askdata_service)
):
    """
    新增一条问数历史记录。
    """
    try:
        result = await service.add_ask_data_history(
            conversation_id=body.conversation_id,
            ask_id=body.ask_id,
            data=body.data
        )
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="历史记录添加成功",
            data={"history_id": result.id}
        )
    except Exception as e:
        logger.exception("新增历史记录失败")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"新增历史记录失败: {e}"
        )


@router.get("/get-history/{conversation_id}", response_model=ResponseSchema, summary="查询历史记录")
async def get_history(
    conversation_id: str,
    service: AskdataService = Depends(get_askdata_service)
):
    """
    根据对话ID查询相关的历史记录。
    """
    try:
        history_records = await service.get_ask_data_history(conversation_id)
        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="历史记录查询成功",
            data=history_records
        )
    except Exception as e:
        logger.exception(f"查询历史记录失败 (conversation_id: {conversation_id})")
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"查询历史记录失败: {e}"
        )
