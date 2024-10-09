import json
import os

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Request, Body, Form
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
import os
from pathlib import Path

from api.apps import manager
from api.db.database import get_db
from api.service.aiforbi_service.aiforbi_service import AIForBIService
from workflow.WorkflowParser import WorkflowParser
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


@router.post("/nl2sql")
async def nl2sql(body: NL2SQLReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    sql = await AIForBIService.nl2sql(nl2sql_req_body=body, db=db, user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(status="success", data=sql)


@router.post("/chart-type")
async def chart_type(body: ChartTypeReqBody = Body(...), db: Session = Depends(get_db), user=Depends(manager)):
    chart_type_list = await AIForBIService.chart_type(chart_type_req_body=body, db=db, user_id=user.id,
                                                      llm_name=body.llm_name)
    return ResponseSchema(status="success", data=chart_type_list)


@router.post("/dynamic-chart-option-function")
async def dynamic_chart_option_function(body: DynamicChartOptionFunctionReqBody = Body(...),
                                        db: Session = Depends(get_db), user=Depends(manager)):
    func = await AIForBIService.dynamic_chart_option_function(dynamic_chart_option_function_req_body=body, db=db,
                                                              user_id=user.id, llm_name=body.llm_name)
    return ResponseSchema(status="success", data=func)
