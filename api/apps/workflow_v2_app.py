from datetime import datetime
from enum import Enum
from typing import Dict, Any, List

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
import json

from workflow_v2.component.component_manager import ComponentManager
from workflow_v2.workflow import run_workflow, WorkflowNode
from sqlalchemy.orm import Session
from api.db.database import get_db
from api.apps import manager
from workflow_v2.workflow_exceptions import NodeExecutionError, WorkflowValidationError
from workflow_v2.workflow_logging_config import WorkflowContextLogger, WorkflowLogger

router = APIRouter()


class DataInput(BaseModel):
    schema: Dict[str, Any]
    start_input_values: Dict[str, Any]


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: str | None = None
    data: Any | None = None


@router.post("/run")
async def run(
        schema: str = Form(...),  # JSON string
        start_input_values: str = Form(...),  # JSON string
        files: List[UploadFile] = File(None),  # Optional files
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    try:
        # Parse JSON strings back to dictionaries
        workflow_data = json.loads(schema)
        input_values = json.loads(start_input_values)

        # 处理上传的文件
        file_data = {}
        if files:
            for file in files:
                # 读取文件内容
                content = await file.read()
                # 将文件内容存储在字典中，以文件名为键
                file_data[file.filename] = content
                # 确保文件指针回到开始位置，以便后续可能的读取
                await file.seek(0)

        # 将文件数据添加到输入值中
        if file_data:
            input_values["uploaded_files"] = file_data

        # 运行工作流，传递额外的参数
        result = await run_workflow(
            workflow_data,
            start_input_values=input_values,
            db=db,
            user=user
        )
        return result

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON format: {str(e)}"
        )
    except NodeExecutionError as e:
        raise e
    except WorkflowValidationError as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error processing request: {str(e)}"
        )


@router.post("/component-run")
async def component_run(
        node_data_str: str = Form(...),  # JSON string
        input_str: str = Form(...),  # JSON string
        batch_str: str = Form(None),  # JSON string
        files: List[UploadFile] = File(None),  # Optional files
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    try:
        node_data = json.loads(node_data_str)
        input_values = json.loads(input_str)
        batch_values = json.loads(batch_str) if batch_str else None

        component = (ComponentManager(logger=WorkflowContextLogger("", WorkflowLogger().logger))
                     .create_component(node_data))
        component.user = user
        component.db = db

        workflow_node = WorkflowNode(node_id=node_data['id'], node_data=node_data)
        component.workflow_node = workflow_node

        start_time = datetime.now().timestamp()

        execute_result = await component.execute_alone(input_values, batch_values)

        end_time = datetime.now().timestamp()

        return_obj = {
            "nodes_io": {
                node_data['id']: {
                    "input": component.inputs,
                    "output": execute_result
                }
            },
            "node_execution_times": {
                node_data['id']: round(end_time - start_time, 3)
            }
        }

        return return_obj

    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON format: {str(e)}"
        )
    except NodeExecutionError as e:
        raise e
    except WorkflowValidationError as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error processing request: {str(e)}"
        )
