import logging
from enum import Enum
from typing import Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.service.nl2sql_service.event.event_handlers import create_sse_response
from api.service.nl2sql_service.event.event_utils import send_event
from api.service.nl2sql_service.nl2sql_service import NL2SQLService, get_nl2sql_service

router = APIRouter()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: str | None = None
    data: Any | None = None


class QueryRewriteRequest(BaseModel):
    """查询重写请求的基础模型"""

    query_text: str = Field(
        ...,
        title="查询文本",
        description="需要重写的原始自然语言查询文本",
    )
    llm_name: str = Field(
        "gpt-4",
        title="LLM模型名称",
        description="用于重写查询的LLM模型名称",
    )
    max_variations: int | None = Field(
        5,
        title="最大变体数量",
        description="生成的查询变体的最大数量",
    )
    preserve_keywords: bool | None = Field(
        True,
        title="保留关键词",
        description="是否在重写过程中保留关键词",
    )

    class Config:
        json_schema_extra = {"example": {"query_text": "显示上个季度的销售数据", "llm_name": "gpt-4", "max_variations": 3, "preserve_keywords": True}}


class QueryRewriteResponse(BaseModel):
    """查询重写响应模型"""

    original_query: str = Field(
        ...,
        title="原始查询",
        description="提交进行重写的原始查询文本",
    )
    rewritten_queries: list[str] = Field(
        ...,
        title="重写后的查询列表",
        description="LLM生成的重写查询变体列表",
    )


@router.post("/rewrite-query", response_model=ResponseSchema, summary="重写自然语言查询为多个变体")
async def rewrite_natural_language_query(
    body: QueryRewriteRequest = Body(
        ..., title="查询重写请求", description="需要重写的自然语言查询信息", examples=[{"query_text": "显示上个季度的销售数据", "llm_name": "gpt-4", "max_variations": 3, "preserve_keywords": True}]
    ),
    db: Session = Depends(get_db),
    user=Depends(manager),
    service: NL2SQLService = Depends(get_nl2sql_service),
):
    """将自然语言查询重写为多个语义相同但表达不同的变体"""
    try:
        # 调用服务重写查询
        rewritten_queries = await service.rewrite_query(query_text=body.query_text, llm_name=body.llm_name)

        # 如果max_variations参数有效，限制返回的变体数量
        if body.max_variations and len(rewritten_queries) > body.max_variations:
            rewritten_queries = rewritten_queries[: body.max_variations]

        # 构建响应数据
        response_data = QueryRewriteResponse(original_query=body.query_text, rewritten_queries=rewritten_queries)

        return ResponseSchema(status=StatusEnum.SUCCESS, message="查询重写成功", data=response_data)
    except FileNotFoundError as e:
        return ResponseSchema(status=StatusEnum.ERROR, message=f"查询重写失败：提示词模板文件未找到 - {e!s}")
    except Exception as e:
        return ResponseSchema(status=StatusEnum.ERROR, message=f"查询重写失败：{e!s}")


class NL2SQLRequest(BaseModel):
    """自然语言转SQL请求的基础模型"""

    query_text: str = Field(
        ...,
        title="查询文本",
        description="需要转换为SQL的原始自然语言查询文本",
    )
    llm_name: str = Field(
        "gpt-4",
        title="LLM模型名称",
        description="用于将自然语言转换为SQL的LLM模型名称",
    )
    dataset_id_list: list[str] = Field(
        ...,
        title="数据集ID列表",
        description="用于查询的数据集ID列表",
    )

    model_config = ConfigDict(json_schema_extra={"example": {"query_text": "查询销售额最高的前10个产品", "llm_name": "gpt-4", "dataset_id_list": ["dataset1", "dataset2"]}})


class NL2SQLResponse(BaseModel):
    """自然语言转SQL响应模型"""

    original_query: str = Field(
        ...,
        title="原始查询",
        description="提交进行SQL转换的原始查询文本",
    )
    sql: str = Field(
        ...,
        title="生成的SQL查询",
        description="从自然语言转换生成的SQL查询语句",
    )
    semantic_layer_struct: dict = Field(
        ...,
        title="语义层结构",
        description="自然语言转SQL生成的语义层结构信息",
    )


@router.post("/nl-to-sql", response_model=ResponseSchema, summary="将自然语言查询转换为SQL")
async def convert_nl_to_sql(
    body: NL2SQLRequest = Body(
        ...,
        title="自然语言转SQL请求",
        description="需要转换为SQL的自然语言查询信息",
        examples=[{"query_text": "查询销售额最高的前10个产品", "llm_name": "gpt-4", "dataset_id_list": ["dataset1", "dataset2"]}],
    ),
    db: Session = Depends(get_db),
    user=Depends(manager),
    service: NL2SQLService = Depends(get_nl2sql_service),
):
    """将自然语言查询转换为对应的SQL查询语句"""
    try:
        # 调用服务将自然语言转换为SQL
        sql, semantic_layer_struct = await service.nl2sql(query_text=body.query_text, llm_name=body.llm_name, dataset_id_list=body.dataset_id_list)

        # 构建响应数据
        response_data = NL2SQLResponse(original_query=body.query_text, sql=sql, semantic_layer_struct=semantic_layer_struct)

        return ResponseSchema(status=StatusEnum.SUCCESS, message="自然语言转SQL成功", data=response_data)
    except FileNotFoundError as e:
        return ResponseSchema(status=StatusEnum.ERROR, message=f"自然语言转SQL失败：提示词模板文件未找到 - {e!s}")
    except Exception as e:
        logger.exception("发生异常")
        return ResponseSchema(status=StatusEnum.ERROR, message=f"自然语言转SQL失败：{e!s}")


# Add this code to nl2sql_app.py after the existing routes


class SQLTemplatingRequest(BaseModel):
    """SQL模板化请求的基础模型"""

    original_question: str = Field(
        ...,
        title="原始问题",
        description="用户提出的原始自然语言问题",
    )
    llm_name: str = Field(
        "gpt-4",
        title="LLM模型名称",
        description="用于SQL模板化的LLM模型名称",
    )
    sql: str = Field(
        ...,
        title="SQL查询",
        description="需要进行模板化的SQL查询语句",
    )
    semantic_layer: dict[str, Any] = Field(
        ...,
        title="语义层结构",
        description="包含数据集、维度、指标等语义层信息的结构",
    )


@router.post("/sql-templating", summary="将SQL查询进行模板化处理")
async def sql_templating(
    body: SQLTemplatingRequest = Body(..., title="SQL模板化请求", description="需要进行模板化的SQL查询信息"),
    db: Session = Depends(get_db),
    user=Depends(manager),
    service: NL2SQLService = Depends(get_nl2sql_service),
):
    """将SQL查询进行模板化处理，识别可参数化的内容"""
    try:
        # 调用服务进行SQL模板化
        sql_template_data = await service.sql_templating(original_question=body.original_question, llm_name=body.llm_name, sql=body.sql, semantic_layer=body.semantic_layer)

        # 构建响应数据
        response_data = {}
        response_data["original_question"] = body.original_question
        response_data["templated_sql"] = sql_template_data["sql_template"]
        response_data["parameters"] = sql_template_data["parameters"]

        return ResponseSchema(status=StatusEnum.SUCCESS, message="SQL模板化处理成功", data=response_data)
    except FileNotFoundError as e:
        return ResponseSchema(status=StatusEnum.ERROR, message=f"SQL模板化处理失败：提示词模板文件未找到 - {e!s}")
    except Exception as e:
        logger.exception("发生异常")
        return ResponseSchema(status=StatusEnum.ERROR, message=f"SQL模板化处理失败：{e!s}")


# Add this code to nl2sql_app.py after the existing routes


class FillSQLTemplateRequest(BaseModel):
    """SQL模板填充请求的基础模型"""

    templated_sql: str = Field(
        ...,
        title="SQL模板",
        description="需要填充的SQL模板字符串",
    )
    parameter_definitions: list[dict[str, Any]] = Field(
        ...,
        title="参数定义列表",
        description="SQL模板中参数的定义列表",
    )
    user_selected_values: dict[str, Any] = Field(
        ...,
        title="用户选择的值",
        description="用户为SQL模板参数选择的值映射",
    )


class FillSQLTemplateResponse(BaseModel):
    """SQL模板填充响应模型"""

    templated_sql: str = Field(
        ...,
        title="SQL模板",
        description="原始SQL模板字符串",
    )
    filled_sql: str = Field(
        ...,
        title="填充后的SQL",
        description="参数值填充后的SQL查询语句",
    )


@router.post("/fill-sql-template", response_model=ResponseSchema, summary="填充SQL模板参数")
async def fill_sql_template(
    body: FillSQLTemplateRequest = Body(..., title="SQL模板填充请求", description="需要填充参数的SQL模板信息"),
    db: Session = Depends(get_db),
    user=Depends(manager),
    service: NL2SQLService = Depends(get_nl2sql_service),
):
    """根据用户选择的参数值填充SQL模板"""
    try:
        # 调用服务填充SQL模板
        filled_sql = await service.fill_sql_template(templated_sql=body.templated_sql, parameter_definitions=body.parameter_definitions, user_selected_values=body.user_selected_values)

        # 构建响应数据
        response_data = FillSQLTemplateResponse(templated_sql=body.templated_sql, filled_sql=filled_sql)

        return ResponseSchema(status=StatusEnum.SUCCESS, message="SQL模板填充成功", data=response_data)
    except KeyError as e:
        return ResponseSchema(status=StatusEnum.ERROR, message=f"SQL模板填充失败：缺少必要的参数 - {e!s}")
    except Exception as e:
        logger.exception("发生异常")
        return ResponseSchema(status=StatusEnum.ERROR, message=f"SQL模板填充失败：{e!s}")


# Add this code to nl2sql_app.py after the existing routes


class GenerateEChartsRequest(BaseModel):
    """ECharts生成请求的基础模型"""

    user_question: str = Field(
        ...,
        title="用户问题",
        description="用户提出的原始问题，用于理解需要生成的图表类型",
    )
    sql: str = Field(
        ...,
        title="SQL查询",
        description="执行的SQL查询语句，图表将基于该查询的结果生成",
    )
    column_and_type: list = Field(
        ...,
        title="列名与类型",
        description="SQL查询结果的列名及其数据类型描述",
    )
    sample_data: list = Field(
        ...,
        title="样本数据",
        description="SQL查询结果的样本数据，用于生成图表",
    )
    llm_name: str = Field(
        "gpt-4",
        title="LLM模型名称",
        description="用于生成ECharts配置的LLM模型名称",
    )


class GenerateEChartsResponse(BaseModel):
    """ECharts生成响应模型"""

    user_question: str = Field(
        ...,
        title="用户问题",
        description="生成图表对应的原始用户问题",
    )
    echarts_code: str = Field(
        ...,
        title="ECharts代码",
        description="生成的ECharts配置JavaScript代码",
    )


@router.post("/generate-echarts", response_model=ResponseSchema, summary="生成ECharts配置代码")
async def generate_echarts(
    body: GenerateEChartsRequest = Body(..., title="ECharts生成请求", description="生成ECharts配置的请求信息"),
    db: Session = Depends(get_db),
    user=Depends(manager),
    service: NL2SQLService = Depends(get_nl2sql_service),
):
    """根据用户问题、SQL查询和查询结果生成ECharts配置代码"""
    try:
        # 调用服务生成ECharts配置
        echarts_code = await service.generate_echarts(user_question=body.user_question, sql=body.sql, column_and_type=body.column_and_type, sample_data=body.sample_data, llm_name=body.llm_name)

        # 构建响应数据
        response_data = GenerateEChartsResponse(user_question=body.user_question, echarts_code=echarts_code)

        return ResponseSchema(status=StatusEnum.SUCCESS, message="ECharts配置生成成功", data=response_data)
    except FileNotFoundError as e:
        return ResponseSchema(status=StatusEnum.ERROR, message=f"ECharts配置生成失败：提示词模板文件未找到 - {e!s}")
    except Exception as e:
        logger.exception("发生异常")
        return ResponseSchema(status=StatusEnum.ERROR, message=f"ECharts配置生成失败：{e!s}")


class WholeProcessRequest(BaseModel):
    """全流程处理请求的基础模型"""

    user_question: str = Field(
        ...,
        title="用户问题",
        description="用户提出的自然语言问题",
    )
    request_id: str = Field(
        ...,
        title="请求ID",
        description="请求ID",
    )
    dataset_id_list: list[str] = Field(
        [],
        title="数据集ID列表",
        description="数据集ID列表",
    )
    llm_name: str = Field(
        "gpt-4",
        title="LLM模型名称",
        description="用于将自然语言转换为SQL的LLM模型名称",
    )


@router.post("/whole-process", response_model=ResponseSchema, summary="执行从自然语言到查询结果的全流程处理")
async def execute_whole_process(
    body: WholeProcessRequest = Body(..., title="全流程处理请求", description="需要执行全流程处理的自然语言问题"),
    db: Session = Depends(get_db),
    user=Depends(manager),
    service: NL2SQLService = Depends(get_nl2sql_service),
):
    """执行从自然语言到查询结果的全流程处理，包括自然语言转SQL、执行查询等步骤"""
    logging.info(f"whole-process请求体：{body}")

    try:
        await service.whole_process(user_question=body.user_question, request_id=body.request_id, dataset_id_list=body.dataset_id_list, llm_name=body.llm_name)

        # 构建响应数据
        response_data = {}

        await send_event(body.request_id, {"message": "全流程处理完成"}, "completed")

        return ResponseSchema(status=StatusEnum.SUCCESS, message="处理成功", data=response_data)
    except FileNotFoundError as e:
        return ResponseSchema(status=StatusEnum.ERROR, message=f"全流程处理失败：提示词模板文件未找到 - {e!s}")
    except Exception as e:
        logger.exception("发生异常")
        await send_event(body.request_id, {"message": "处理失败"}, "error")
        return ResponseSchema(status=StatusEnum.ERROR, message=f"全流程处理失败：{e!s}")


from fastapi import Depends, Request


@router.get("/events/{event_id}")
def subscribe_to_event(request: Request, event_id: str):
    """
    订阅指定事件ID的SSE端点

    Args:
        request: FastAPI请求对象
        event_id: 事件ID

    Returns:
        StreamingResponse: SSE流响应
    """
    return create_sse_response(request, event_id)


import json

import numpy as np


class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


class ReQueryRequest(BaseModel):
    """SQL重新查询请求的基础模型"""

    templated_sql: str = Field(
        ...,
        title="SQL模板",
        description="需要填充的SQL模板字符串",
    )
    parameter_definitions: list[dict[str, Any]] = Field(
        ...,
        title="参数定义列表",
        description="SQL模板中参数的定义列表",
    )
    user_selected_values: dict[str, Any] = Field(
        ...,
        title="用户选择的值",
        description="用户为SQL模板参数选择的值映射",
    )


@router.post("/re-query", response_model=ResponseSchema, summary="使用填充后的SQL模板重新查询")
async def re_query(
    body: ReQueryRequest = Body(..., title="SQL重新查询请求", description="使用填充后的SQL模板重新查询的请求信息"),
    db: Session = Depends(get_db),
    user=Depends(manager),
    service: NL2SQLService = Depends(get_nl2sql_service),
):
    """根据用户选择的参数值填充SQL模板并执行查询"""
    logger.info(f"收到查询请求：{body}")
    try:
        # 调用服务填充SQL模板并执行查询
        result = await service.re_query(templated_sql=body.templated_sql, parameter_definitions=body.parameter_definitions, user_selected_values=body.user_selected_values)

        # 处理结果中的NumPy类型
        if "sql_result" in result and "data" in result["sql_result"]:
            # 将NumPy类型转换为Python原生类型
            for item in result["sql_result"]["data"]:
                for key, value in item.items():
                    if isinstance(value, np.integer):
                        item[key] = int(value)
                    elif isinstance(value, np.floating):
                        item[key] = float(value)
                    elif isinstance(value, np.ndarray):
                        item[key] = value.tolist()

        return ResponseSchema(status=StatusEnum.SUCCESS, message="SQL重新查询成功", data=result)
    except KeyError as e:
        return ResponseSchema(status=StatusEnum.ERROR, message=f"SQL重新查询失败：缺少必要的参数 - {e!s}")
    except Exception as e:
        logger.exception("发生异常")
        return ResponseSchema(status=StatusEnum.ERROR, message=f"SQL重新查询失败：{e!s}")
