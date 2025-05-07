from enum import Enum
from typing import List, Any

from fastapi import APIRouter, Depends, Body
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.db.db_models import get_db
from api.apps import manager
from api.service.nl2sql_service.nl2sql_service import NL2SQLService, get_nl2sql_service
from api.service.nl2sql_service.query_intent_analyzer import QueryIntentType

router = APIRouter()


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
        schema_extra = {
            "example": {
                "query_text": "显示上个季度的销售数据",
                "llm_name": "gpt-4",
                "max_variations": 3,
                "preserve_keywords": True
            }
        }


class QueryRewriteResponse(BaseModel):
    """查询重写响应模型"""
    original_query: str = Field(
        ...,
        title="原始查询",
        description="提交进行重写的原始查询文本",
    )
    rewritten_queries: List[str] = Field(
        ...,
        title="重写后的查询列表",
        description="LLM生成的重写查询变体列表",
    )


@router.post("/rewrite-query", response_model=ResponseSchema, summary="重写自然语言查询为多个变体")
async def rewrite_natural_language_query(
        body: QueryRewriteRequest = Body(
            ...,
            title="查询重写请求",
            description="需要重写的自然语言查询信息",
            example={
                "query_text": "显示上个季度的销售数据",
                "llm_name": "gpt-4",
                "max_variations": 3,
                "preserve_keywords": True
            }
        ),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: NL2SQLService = Depends(get_nl2sql_service)
):
    """将自然语言查询重写为多个语义相同但表达不同的变体"""
    try:
        # 调用服务重写查询
        rewritten_queries = await service.rewrite_query(
            query_text=body.query_text,
            llm_name=body.llm_name
        )

        # 如果max_variations参数有效，限制返回的变体数量
        if body.max_variations and len(rewritten_queries) > body.max_variations:
            rewritten_queries = rewritten_queries[:body.max_variations]

        # 构建响应数据
        response_data = QueryRewriteResponse(
            original_query=body.query_text,
            rewritten_queries=rewritten_queries
        )

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="查询重写成功",
            data=response_data
        )
    except FileNotFoundError as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"查询重写失败：提示词模板文件未找到 - {str(e)}"
        )
    except Exception as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"查询重写失败：{str(e)}"
        )


class QueryIntentRequest(BaseModel):
    """查询意图分析请求的基础模型"""
    query_text: str = Field(
        ...,
        title="查询文本",
        description="需要分析意图的原始自然语言查询文本",
    )
    llm_name: str = Field(
        "gpt-4",
        title="LLM模型名称",
        description="用于分析意图的LLM模型名称",
    )

    class Config:
        schema_extra = {
            "example": {
                "query_text": "查询销售额最高的前10个产品",
                "llm_name": "gpt-4"
            }
        }


class QueryIntentResponse(BaseModel):
    """查询意图分析响应模型"""
    original_query: str = Field(
        ...,
        title="原始查询",
        description="提交进行意图分析的原始查询文本",
    )
    intents: List[str] = Field(
        ...,
        title="查询意图列表",
        description="LLM识别的查询意图类型列表",
    )
    primary_intent: str = Field(
        ...,
        title="主要意图",
        description="识别出的主要查询意图",
    )


@router.post("/analyze-intent", response_model=ResponseSchema, summary="分析自然语言查询的意图")
async def analyze_query_intent(
        body: QueryIntentRequest = Body(
            ...,
            title="查询意图分析请求",
            description="需要分析意图的自然语言查询信息",
            example={
                "query_text": "查询销售额最高的前10个产品",
                "llm_name": "gpt-4"
            }
        ),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: NL2SQLService = Depends(get_nl2sql_service)
):
    """分析自然语言查询的意图类型"""
    try:
        # 调用服务分析查询意图
        intent_types = await service.analyze_query_intent(
            query_text=body.query_text,
            llm_name=body.llm_name
        )

        # 将枚举类型转换为字符串列表
        intent_strings = [intent.value for intent in intent_types]

        # 确定主要意图（这里简单地取第一个意图作为主要意图）
        primary_intent = intent_strings[0] if intent_strings else QueryIntentType.AMBIGUOUS.value

        # 构建响应数据
        response_data = QueryIntentResponse(
            original_query=body.query_text,
            intents=intent_strings,
            primary_intent=primary_intent
        )

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="查询意图分析成功",
            data=response_data
        )
    except FileNotFoundError as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"查询意图分析失败：提示词模板文件未找到 - {str(e)}"
        )
    except Exception as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"查询意图分析失败：{str(e)}"
        )


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
    dataset_id_list: List[str] = Field(
        ...,
        title="数据集ID列表",
        description="用于查询的数据集ID列表",
    )

    class Config:
        schema_extra = {
            "example": {
                "query_text": "查询销售额最高的前10个产品",
                "llm_name": "gpt-4",
                "dataset_id_list": ["dataset1", "dataset2"]
            }
        }


class NL2SQLResponse(BaseModel):
    """自然语言转SQL响应模型"""
    original_query: str = Field(
        ...,
        title="原始查询",
        description="提交进行SQL转换的原始查询文本",
    )
    sql_query: str = Field(
        ...,
        title="生成的SQL查询",
        description="从自然语言转换生成的SQL查询语句",
    )


@router.post("/nl-to-sql", response_model=ResponseSchema, summary="将自然语言查询转换为SQL")
async def convert_nl_to_sql(
        body: NL2SQLRequest = Body(
            ...,
            title="自然语言转SQL请求",
            description="需要转换为SQL的自然语言查询信息",
            example={
                "query_text": "查询销售额最高的前10个产品",
                "llm_name": "gpt-4",
                "dataset_id_list": ["dataset1", "dataset2"]
            }
        ),
        db: Session = Depends(get_db),
        user=Depends(manager),
        service: NL2SQLService = Depends(get_nl2sql_service)
):
    """将自然语言查询转换为对应的SQL查询语句"""
    try:
        # 调用服务将自然语言转换为SQL
        sql_query = await service.nl2sql(
            query_text=body.query_text,
            llm_name=body.llm_name,
            dataset_id_list=body.dataset_id_list
        )

        # 构建响应数据
        response_data = NL2SQLResponse(
            original_query=body.query_text,
            sql_query=sql_query
        )

        return ResponseSchema(
            status=StatusEnum.SUCCESS,
            message="自然语言转SQL成功",
            data=response_data
        )
    except FileNotFoundError as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"自然语言转SQL失败：提示词模板文件未找到 - {str(e)}"
        )
    except Exception as e:
        return ResponseSchema(
            status=StatusEnum.ERROR,
            message=f"自然语言转SQL失败：{str(e)}"
        )
