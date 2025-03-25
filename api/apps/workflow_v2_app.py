import asyncio
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Request
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
import json

from workflow_v2.workflow_state_manager import workflow_state_manager
from workflow_v2.component.component_manager import ComponentManager
from workflow_v2.workflow import run_workflow, WorkflowNode
from sqlalchemy.orm import Session
from api.db.database import get_db
from api.apps import manager
from workflow_v2.workflow_exceptions import NodeExecutionError, WorkflowValidationError
from workflow_v2.workflow_logging_config import WorkflowContextLogger, WorkflowLogger

router = APIRouter()


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: str | None = None
    data: Any | None = None


@router.post("/run")
async def run(
        schema_data: str = Form(..., alias="schema"),  # JSON string
        start_input_values: str = Form(...),  # JSON string
        workflow_id: str = Form(...),
        files: list[UploadFile] = File(None),  # Optional files
        bucket_name: str = Form(None),  # bucket_name在任何情况下都会传入，所以下面处理文件时，先判断files是否为空，如果为空，处理bucket_name
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    try:
        # Parse JSON strings back to dictionaries
        workflow_data = json.loads(schema_data)
        input_values = json.loads(start_input_values)

        # 将文件与输入值关联
        if files and len(files) > 0 and files[0].size > 0:
            nodes = workflow_data.get("nodes")
            for node in nodes:
                if node.get("id") == "100001":
                    node_data = node.get("data")
                    node_outputs = node_data.get("outputs")
                    for output in node_outputs:
                        if output.get("assistType", "") == 1:
                            input_values[output.get("name")] = files[0]
                            break
                else:
                    continue
        else:
            nodes = workflow_data.get("nodes")
            for node in nodes:
                if node.get("id") == "100001":
                    node_data = node.get("data")
                    node_outputs = node_data.get("outputs")
                    for output in node_outputs:
                        if output.get("type") == "minio":
                            input_values[output.get("name")] = bucket_name + "+" + input_values[output.get("name")]
                            break
                else:
                    continue

        # 运行工作流，传递额外的参数
        result = await run_workflow(
            workflow_data,
            start_input_values=input_values,
            workflow_id=workflow_id,
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
        files: list[UploadFile] = File(None),  # Optional files
        bucket_name: str = Form(None),  # bucket_name在任何情况下都会传入，所以下面处理文件时，先判断files是否为空，如果为空，处理bucket_name
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    try:
        node_data = json.loads(node_data_str)
        input_values = json.loads(input_str)
        # 因为是单次执行，所以值是用户直接写在参数里面的，所以要将这些字面量提取出来
        literal_params = extract_literal_parameters(node_data)
        merged_input_values = merge_with_input_values(literal_params, input_values)
        batch_input_type = extract_batch_input_types(node_data)
        batch_values = convert_input_values(batch_str, batch_input_type) if batch_str else None

        # 将文件与输入值关联
        if files and len(files) > 0 and files[0].size > 0:
            input_values[list(input_values)[0]] = files[0]
        else:
            for node_input in node_data['data']['inputs']['inputParameters']:
                if node_input['input']['type'] == 'minio':
                    batch_values[node_input['name']] = bucket_name + "+" + batch_values[node_input['name']]
                    break

        component = (ComponentManager(logger=WorkflowContextLogger("", WorkflowLogger().logger))
                     .create_component(node_data))
        component.user = user
        component.db = db

        workflow_node = WorkflowNode(node_id=node_data['id'], node_data=node_data)
        component.workflow_node = workflow_node

        start_time = datetime.now().timestamp()

        execute_result = await component.execute_alone(merged_input_values, batch_values)

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


@router.get("/workflow/{workflow_id}/events")
async def workflow_events(workflow_id: str, request: Request):
    """
    SSE端点，用于接收工作流状态更新

    Args:
        workflow_id: 工作流ID
        request: FastAPI请求对象

    Returns:
        EventSourceResponse: SSE事件流
    """

    async def event_generator():
        # 订阅工作流状态更新
        queue = await workflow_state_manager.subscribe(workflow_id)

        try:
            # 保持连接直到客户端断开或收到结束信号
            while True:
                if await request.is_disconnected():
                    break

                # 等待状态更新，带有超时
                try:
                    state_update = await asyncio.wait_for(queue.get(), timeout=60)

                    # 如果接收到None，表示工作流结束
                    if state_update is None:
                        # 发送一个结束事件
                        yield {
                            "event": "workflow_end",
                            "data": json.dumps({"workflow_id": workflow_id, "status": "completed"})
                        }
                        break

                    # 发送普通状态更新
                    yield {
                        "event": "workflow_update",
                        "data": json.dumps(state_update)
                    }

                except asyncio.TimeoutError:
                    # 发送保活消息
                    yield {
                        "event": "ping",
                        "data": json.dumps({"timestamp": workflow_state_manager._current_timestamp()})
                    }
        finally:
            # 确保取消订阅
            await workflow_state_manager.unsubscribe(workflow_id, queue)

    return EventSourceResponse(event_generator())


def extract_literal_parameters(node_data):
    """
    Extract key-value pairs from inputParameters where the value type is 'literal'

    Args:
        node_data (dict): The node data containing inputParameters

    Returns:
        dict: A dictionary of parameter name to literal content
    """
    literal_params = {}

    if 'data' in node_data and 'inputs' in node_data['data'] and 'inputParameters' in node_data['data']['inputs']:
        input_parameters = node_data['data']['inputs']['inputParameters']

        for param in input_parameters:
            if (param.get('input') and
                    param.get('input').get('value') and
                    param.get('input').get('value').get('type') == 'literal'):

                name = param.get('name')
                content = param.get('input').get('value').get('content')

                if name and content is not None:
                    literal_params[name] = content

    return literal_params


def merge_with_input_values(literal_params, input_values):
    """
    Merge literal parameters with input values, with input_values taking precedence

    Args:
        literal_params (dict): The extracted literal parameters
        input_values (dict): The input values

    Returns:
        dict: Merged dictionary with input_values taking precedence
    """
    merged = literal_params.copy()

    # Update with input_values (input_values takes precedence)
    if input_values:
        merged.update(input_values)

    return merged


def convert_string_to_typed_value(value_str, type_info):
    """
    将字符串值根据类型信息转换为正确的类型
    """
    # 去除Array<>外的包装
    base_type = type_info.replace('Array<', '').replace('>', '')

    try:
        # 先解析字符串为Python列表
        value_list = json.loads(value_str)

        # 根据基本类型进行转换
        if base_type == 'Number':
            return [float(x) if isinstance(x, str) else x for x in value_list]
        elif base_type == 'String':
            return [str(x) for x in value_list]
        elif base_type == 'Boolean':
            return [bool(x) if isinstance(x, str) else x for x in value_list]
        else:
            return value_list  # 对于其他类型（如Object），保持原样
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        return None
    except Exception as e:
        print(f"Error converting type: {e}")
        return None


def convert_input_values(input_str, type_dict):
    """
    转换输入字符串中的所有值为对应的类型
    """
    try:
        # 解析输入字符串为字典
        input_dict = json.loads(input_str)
        result = {}

        # 遍历每个键值对进行转换
        for key, value in input_dict.items():
            if key in type_dict:
                result[key] = convert_string_to_typed_value(value, type_dict[key])
            else:
                result[key] = value  # 如果没有对应的类型信息，保持原样

        return result
    except json.JSONDecodeError as e:
        print(f"Error parsing input string: {e}")
        return None
    except Exception as e:
        print(f"Error processing input: {e}")
        return None


def extract_batch_input_types(json_data):
    # 用于转换类型的映射字典
    type_mapping = {
        'integer': 'Number',
        'string': 'String',
        'boolean': 'Boolean',
        'float': 'Number'
    }

    def parse_schema_type(schema):
        # 对于对象类型，获取其第一个字段的类型
        if schema.get('type') == 'object':
            first_field = schema.get('schema', [])[0]
            if first_field.get('type') == 'list':
                return parse_schema_type(first_field.get('schema', {}))
        # 返回基本类型
        return type_mapping.get(schema.get('type'), schema.get('type').capitalize())

    result = {}

    try:
        input_lists = json_data['data']['inputs']['batch']['inputLists']

        for item in input_lists:
            variable_name = item['name']
            input_data = item['input']

            # 直接获取schema中的类型，然后包装成Array
            schema_type = parse_schema_type(input_data.get('schema', {}))
            result[variable_name] = f'Array<{schema_type}>'

    except KeyError as e:
        print(f"Error: Could not find key {e} in JSON data")
    except Exception as e:
        print(f"Error: {str(e)}")

    return result
