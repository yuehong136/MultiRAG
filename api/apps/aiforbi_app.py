from fastapi import APIRouter, Depends, Body
from typing import Dict

from api.apps import manager
from api.db.database import get_db
from api.service.aiforbi_service.aiforbi_service import AIForBIService
from sqlalchemy.orm import Session

# AI生成图表
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
    llm_name: str


class ChartTypeReqBody(BaseModel):
    user_question: str
    sql_result: Dict[str, Any]
    llm_name: str


class DynamicChartOptionFunctionReqBody(BaseModel):
    user_question: str
    sql_result: Dict[str, Any]
    chart_type: str
    llm_name: str


class StaticChartOptionReqBody(BaseModel):
    raw_data: str
    llm_name: str


@router.post("/nl2sql", summary="自然语言转SQL", response_description="成功生成SQL查询语句")
async def nl2sql(body: NL2SQLReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    将自然语言转化为SQL查询语句的接口。

    概要：通过解析用户的问题，并结合表结构信息，生成对应的SQL查询语句。
    响应描述：返回生成的SQL查询语句。

    参数：
    - body (NL2SQLReqBody): 包含用户问题、表结构和模型名称的请求体。

    返回：
    - ResponseSchema: 返回包含生成的SQL查询语句的JSON结果。

    功能：
    1. 调用AI服务将自然语言问题转换为SQL查询。
    2. 返回生成的SQL查询结果。

    注意：
    - 用户必须提供完整的问题描述和表结构信息以确保SQL语句的生成。
    """
    sql = await AIForBIService.nl2sql(nl2sql_req_body=body, db=db, user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(status="success", data=sql)


@router.post("/chart-type", summary="获取推荐图表类型", response_description="成功获取推荐图表类型列表")
async def chart_type(body: ChartTypeReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取推荐的图表类型的接口。

    概要：根据SQL查询结果和用户问题，推荐适合的数据可视化图表类型。
    响应描述：返回推荐的图表类型列表。

    参数：
    - body (ChartTypeReqBody): 包含用户问题、SQL结果和模型名称的请求体。

    返回：
    - ResponseSchema: 返回包含推荐图表类型的JSON结果。

    功能：
    1. 调用AI服务获取推荐的图表类型。
    2. 返回图表类型的列表，以便前端用户进行选择。

    注意：
    - 用户需要提供SQL结果作为数据源以获取推荐的图表类型。
    """
    chart_type_list = await AIForBIService.chart_type(chart_type_req_body=body, db=db, user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(status="success", data=chart_type_list)


@router.post("/dynamic-chart-option-function", summary="动态生成图表配置", response_description="成功生成动态图表配置函数")
async def dynamic_chart_option_function(body: DynamicChartOptionFunctionReqBody = Body(...),
                                        db: Session = Depends(get_db), user=Depends(manager)):
    """
    动态生成图表配置的接口。

    概要：根据用户问题和SQL查询结果，动态生成图表配置函数，以便进行数据可视化。
    响应描述：返回生成的图表配置函数。

    参数：
    - body (DynamicChartOptionFunctionReqBody): 包含用户问题、SQL结果、图表类型和模型名称的请求体。

    返回：
    - ResponseSchema: 返回包含图表配置函数的JSON结果。

    功能：
    1. 调用AI服务生成适合的数据可视化配置。
    2. 返回配置函数，用于在前端呈现动态图表。

    注意：
    - 该接口根据图表类型生成适合的图表配置函数。
    """
    func = await AIForBIService.dynamic_chart_option_function(dynamic_chart_option_function_req_body=body, db=db,
                                                              user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(status="success", data=func)


@router.post("/static-chart-option", summary="静态生成图表配置", response_description="成功生成静态图表配置")
async def static_chart_option(body: StaticChartOptionReqBody = Body(...),
                                        db: Session = Depends(get_db), user=Depends(manager)):
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
    func = await AIForBIService.static_chart_option(static_chart_option_req_body=body, db=db,
                                                    user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(status="success", data=func)
