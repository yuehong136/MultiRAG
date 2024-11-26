from enum import Enum

from fastapi import APIRouter, Depends
from fastapi import HTTPException
from pydantic import BaseModel

from typing import Dict, Any

from workflow_v2.workflow import run_workflow

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
async def run(data: DataInput):
    workflow_data = data.schema
    start_input_values = data.start_input_values

    try:
        result = await run_workflow(workflow_data, start_input_values=start_input_values)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error processing schema: {str(e)}"
        )
