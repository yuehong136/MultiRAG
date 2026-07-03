from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db

# from api.db.database import get_db
from api.service.aiforbi_service.aiforbi_service import AIForBIService

# AI生成图表
router = APIRouter()

from enum import Enum
from typing import Any

from pydantic import BaseModel


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: str | None = None
    data: Any | None = None


class NL2SQLReqBody(BaseModel):
    user_question: str
    table_structure: str
    llm_name: str


class ChartTypeReqBody(BaseModel):
    user_question: str
    sql_result: dict[str, Any]
    llm_name: str


class DynamicChartOptionFunctionReqBody(BaseModel):
    user_question: str
    sql_result: dict[str, Any]
    chart_type: str
    llm_name: str


class StaticChartOptionReqBody(BaseModel):
    raw_data: str
    llm_name: str


@router.post("/nl2sql", summary="自然语言转SQL", response_description="成功生成SQL查询语句")
async def nl2sql(body: NL2SQLReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    自然语言转SQL接口

    此接口接收用户的自然语言问题、数据表结构和指定的语言模型名称，返回相应的SQL查询语句。

    参数:
    - body: NL2SQLReqBody
      - user_question: 用户的自然语言问题
      - table_structure: 数据表结构
      - llm_name: 使用的语言模型名称
    - db: 数据库会话，由FastAPI依赖注入
    - user: 当前用户，由FastAPI依赖注入

    返回:
    - ResponseSchema对象，包含:
      - status: 操作状态（成功/失败）
      - data: 生成的SQL查询语句
    """
    sql = await AIForBIService.nl2sql(nl2sql_req_body=body, db=db, user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(data=sql)


@router.post("/chart-type", summary="获取推荐图表类型", response_description="成功获取推荐图表类型列表")
async def chart_type(body: ChartTypeReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    图表类型推荐接口

    此接口基于用户问题和SQL查询结果，推荐适合的图表类型。

    参数:
    - body: ChartTypeReqBody
      - user_question: 用户的问题
      - sql_result: SQL查询的结果
      - llm_name: 使用的语言模型名称
    - db: 数据库会话，由FastAPI依赖注入
    - user: 当前用户，由FastAPI依赖注入

    返回:
    - ResponseSchema对象，包含:
      - status: 操作状态（成功/失败）
      - data: 推荐的图表类型列表
    """
    chart_type_list = await AIForBIService.chart_type(chart_type_req_body=body, db=db, user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(data=chart_type_list)


@router.post("/dynamic-chart-option-function", summary="动态生成图表配置", response_description="成功生成动态图表配置函数")
async def dynamic_chart_option_function(body: DynamicChartOptionFunctionReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    动态图表选项生成接口

    此接口根据用户问题、SQL查询结果和选定的图表类型，生成用于创建图表的动态选项函数。

    参数:
    - body: DynamicChartOptionFunctionReqBody
      - user_question: 用户的问题
      - sql_result: SQL查询的结果
      - chart_type: 选定的图表类型
      - llm_name: 使用的语言模型名称
    - db: 数据库会话，由FastAPI依赖注入
    - user: 当前用户，由FastAPI依赖注入

    返回:
    - ResponseSchema对象，包含:
      - status: 操作状态（成功/失败）
      - data: 生成的动态图表选项函数
    """
    func = await AIForBIService.dynamic_chart_option_function(dynamic_chart_option_function_req_body=body, db=db, user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(data=func)


@router.post("/static-chart-option", summary="静态生成图表配置", response_description="成功生成静态图表配置")
async def static_chart_option(body: StaticChartOptionReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    静态生成图表配置的接口。

    概要：根据原始数据生成静态图表的配置，适合固定的数据结构。
    响应描述：返回生成的静态图表配置。

    参数：
    - body (StaticChartOptionReqBody): 包含原始数据和模型名称的请求体。

    返回：
    - ResponseSchema: 返回包含静态图表配置的JSON结果。

    功能：
    1. 调用AI服务生成静态图表配置。
    2. 返回静态图表配置，用于前端呈现固定图表。

    注意：
    - 静态图表配置适用于结构已确定的数据源。
    """
    func = await AIForBIService.static_chart_option(static_chart_option_req_body=body, db=db, user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(data=func)
