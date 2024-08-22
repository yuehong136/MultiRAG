import json
import os

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Request, Body, Form
from typing import List, Optional, Dict
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
import os
from pathlib import Path

from api.apps import manager
from api.db.database import get_db
from workflow.WorkflowParser import WorkflowParser
from sqlalchemy.orm import Session

router = APIRouter()


@router.post("/run")
async def run(
        workflow_json_str: str = Form(..., description="工作流请求参数"),
        input_data_str: Optional[str] = Form(None, description="入参"),
        file: Optional[UploadFile] = File(None, description="可选的上传文件"),
        db: Session = Depends(get_db),
        user=Depends(manager)
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

    print(input_data_str)
    # 解析Dict数据
    input_data = {}
    if input_data_str is not None:
        try:
            input_data = json.loads(input_data_str)
        except json.JSONDecodeError:
            print(f"input_data_str = {input_data_str}")
            print(f"{input_data_str} is not a valid JSON string")
            input_data = {"input_data": input_data_str}
            pass
            # raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
            #                     detail="input_data_str is not a valid JSON string")

    if file is not None:
        input_data['FILE'] = file

    workflow_engine = WorkflowParser.parse(workflow_json_str=workflow_json_str)
    result = await workflow_engine.execute(input_data=input_data, db=db, user=user)
    node_list = result['node_list']
    context = result['context']
    last_node_id = list(node_list)[-1].node_id
    last_node = context.get(str(last_node_id)).output_data
    return last_node


@router.post("/run-test")
async def run_test(user=Depends(manager)):
    print('okk')
    return {"result": "success"}


@router.get("/download/{filename}")
async def download_file(filename: str):
    file_path = Path("/Users/naimehao/PycharmProjects/multrag/workflow/temp") / (filename + ".xlsx")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=file_path,
        filename=filename + ".xlsx",
        media_type='application/octet-stream'
    )
