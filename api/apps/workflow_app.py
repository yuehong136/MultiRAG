import json
import os

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Request, Body, Form
from typing import List, Optional, Dict
from pydantic import BaseModel

from workflow.WorkflowParser import WorkflowParser

router = APIRouter()

@router.post("/run")
async def run(
        workflow_json_str: str = Form(..., description="工作流请求参数"),
        input_data_str: Optional[str] = Form(None, description="入参"),
        file: Optional[UploadFile] = File(None, description="可选的上传文件")
):
    """
    运行工作流接口

    - **workflow_json**: 包含工作流数据的JSON字符串
    - **dict_data**: 包含字典数据的JSON字符串
    - **file**: 可选的上传文件

    返回:
    - **json_data**: 解析后的工作流JSON数据
    - **dict_data**: 解析后的字典数据
    - **file_info**: 如果上传了文件,则包含文件信息
    """
    # 解析JSON字符串
    try:
        json.loads(workflow_json_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="workflow_json_str is not a valid JSON string")

    # 解析Dict数据
    input_data = {}
    if input_data_str is not None:
        dict_data = json.loads(input_data_str)

    if file is not None:
        input_data['FILE'] = file

    workflow_engine = WorkflowParser.parse(workflow_json_str=workflow_json_str)
    result = await workflow_engine.execute(input_data=input_data)
    node_list = result['node_list']
    context = result['context']
    last_node_id = list(node_list)[-1].node_id
    last_node = context.get(last_node_id).output_data
    return last_node
