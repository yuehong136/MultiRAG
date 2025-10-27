from typing import Generic, TypeVar
from pydantic import BaseModel, Field, ConfigDict

T = TypeVar('T')


class APIResponse(BaseModel, Generic[T]):
    """统一的 API 响应模型"""
    code: int = Field(default=0, description="响应代码，0 表示成功")
    message: str = Field(default="Success", description="响应消息")
    data: T | None = Field(default=None, description="响应数据")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": 0,
                "message": "Success",
                "data": None
            }
        }
    )


def success_response(data=None, message: str = "Success", code: int = 0) -> APIResponse:
    """创建成功响应"""
    return APIResponse(code=code, message=message, data=data)


def error_response(message: str = "Error", code: int = -1, data=None) -> APIResponse:
    """创建错误响应"""
    return APIResponse(code=code, message=message, data=data)
