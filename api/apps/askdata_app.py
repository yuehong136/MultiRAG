import logging
from enum import Enum
from typing import List, Any

from fastapi import APIRouter, Depends, Body
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.db.db_models import get_db
from api.apps import manager
from api.service.askdata_service.askdata_service import AskdataService, get_askdata_service
from api.service.askdata_service.pg_query_formatter import execute_sql_and_format_result
from api.service.askdata_service.sql_parser import SQLParser

from api.service.nl2sql_service.event.event_handlers import create_sse_response
from api.service.nl2sql_service.event.event_utils import send_event

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


class NLQToInitialSQLRequest(BaseModel):
    """自然语言转初始SQL请求的基础模型"""
    user_query: str = Field(
        ...,
        title="查询文本",
        description="用户提出的自然语言查询文本",
    )
    dataset_id_list: List[str] = Field(
        [],
        title="数据集ID列表",
        description="数据集ID列表",
    )
    llm_name: str = Field(
        "gpt-4",
        title="LLM模型名称",
        description="用于将自然语言转换为SQL的LLM模型名称",
    )


@router.post("/nlq-to-initial-sql", response_model=ResponseSchema, summary="将自然语言查询转换为初始SQL")
async def nlq_to_initial_sql(
        body: NLQToInitialSQLRequest = Body(
            ...,
            title="自然语言转SQL请求",
            description="需要转换为SQL的自然语言查询"
        ),
        db: Session = Depends(get_db), user=Depends(manager),
        service: AskdataService = Depends(get_askdata_service)
):
    """
    将自然语言查询转换为初始SQL语句

    该接口会：
    1. 生成语义层数据
    2. 使用LLM将自然语言查询转换为SQL
    3. 验证生成的SQL语句

    返回生成的SQL语句或错误信息
    """
    logging.info(f"nlq-to-initial-sql请求体：{body}")

    try:
        # 生成语义层
        semantic_layer = await service.generate_semantic_layer(
            user_query=body.user_query,
            dataset_id_list=body.dataset_id_list
        )

        # 转换为SQL
        sql, used_models = await service.nlq_to_initial_sql(
            user_query=body.user_query,
            llm_name=body.llm_name,
            semantic_layer=semantic_layer
        )

        table_config = await service.generate_table_config(sql)

        # 执行查询
        result = execute_sql_and_format_result(sql=sql, db_config={})

        if sql:
            return ResponseSchema(
                status=StatusEnum.SUCCESS,
                message="生成初始SQL成功",
                data={"sql": sql, "result": result}
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
