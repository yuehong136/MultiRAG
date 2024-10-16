import json
from fastapi import APIRouter, Depends, status, UploadFile, File, Form
from typing import Optional
from fastapi import HTTPException
from fastapi.responses import FileResponse
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
    运行指定工作流的接口。

    概要：通过解析和执行工作流请求参数来运行工作流。
    响应描述：成功执行后，返回工作流执行的最终节点输出数据。

    参数：
    - **workflow_json_str** (str): 包含工作流数据的 JSON 字符串。
    - **input_data_str** (str, 可选): 包含字典数据的 JSON 字符串。
    - **file** (UploadFile, 可选): 可上传的文件，作为工作流的附加输入数据。

    返回：
    - **dict**: 返回工作流执行结果的 JSON 对象，包含最终节点输出数据。

    功能：
    1. 解析工作流请求参数，确保为有效的 JSON 字符串。
    2. 可选解析输入数据参数，如果存在的话。
    3. 将上传的文件数据与其他输入参数一起传递给工作流执行引擎。
    4. 执行工作流，返回最终节点的输出结果。

    异常处理：
    - 如果工作流或输入数据不是有效的 JSON 字符串，将抛出 HTTP 400 错误。
    - 如果上传文件不为空，则将其添加到输入参数中。

    注意：
    - 工作流请求参数必须是有效的 JSON 字符串。
    """
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
    """
    测试运行的接口，用于检查服务是否正常工作。

    概要：用于测试接口的运行状态。
    响应描述：成功运行返回简单的结果对象，表明接口可用。

    返回：
    - **dict**: 返回包含 "result" 键的 JSON 对象，表明测试成功。

    注意：
    - 此接口不涉及复杂操作，仅用于测试用途。
    """
    print('okk')
    return {"result": "success"}


@router.get("/download/{filename}")
async def download_file(filename: str):
    """
    下载指定文件的接口。

    概要：根据文件名从指定路径下载文件。
    响应描述：成功找到并下载文件时，返回文件响应对象。

    参数：
    - **filename** (str): 要下载的文件名（不包括扩展名）。

    返回：
    - **FileResponse**: 返回指定文件的下载响应，包含文件内容。

    异常处理：
    - 如果文件未找到，将返回 HTTP 404 错误。

    注意：
    - 文件名应对应存在的文件，以确保成功下载。
    """
    file_path = Path("/Users/naimehao/PycharmProjects/multrag/workflow/temp") / (filename + ".xlsx")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=file_path,
        filename=filename + ".xlsx",
        media_type='application/octet-stream'
    )
