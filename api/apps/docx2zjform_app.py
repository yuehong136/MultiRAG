import io
import logging
import traceback
from enum import Enum
from typing import Any

from docx import Document
from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

# from api.db.database import get_db
from api.apps import manager
from api.db.db_models import get_db
from api.service.docx2zjform_service.docx2zjform_service import Docx2ZJFormService

router = APIRouter()


class StatusEnum(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ResponseSchema(BaseModel):
    status: StatusEnum = StatusEnum.SUCCESS
    message: str | None = None
    data: Any | None = None


@router.post("/convert", summary="转换Word文档到竹简表单", response_description="成功解析文档内容", response_model=ResponseSchema)
async def convert(file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    Word文档转换接口

    此接口接收一个.docx文件，解析其内容并返回竹简表单数据。

    参数:
    - file: UploadFile，上传的.docx文件
    - db: 数据库会话，由FastAPI依赖注入
    - user: 当前用户，由FastAPI依赖注入

    返回:
    - ResponseSchema对象，包含:
      - status: 操作状态（成功/失败）
      - message: 状态信息
      - data: 竹简表单数据
    """
    try:
        # 验证文件类型
        if not file.filename.endswith(".docx"):
            logging.warning(f"上传了不支持的文件类型: {file.filename}")
            return ResponseSchema(status=StatusEnum.ERROR, message="仅支持.docx格式文件")

        # 读取上传的文件内容
        content = await file.read()
        doc = Document(io.BytesIO(content))

        # 解析文档内容
        zj_json_str = await Docx2ZJFormService.convert(doc, db, user.id)

        logging.info(f"成功解析文档: {file.filename}")
        return ResponseSchema(status=StatusEnum.SUCCESS, message="文档解析成功", data=zj_json_str)

    except Exception as e:
        # 使用logger.error记录完整的异常信息和堆栈跟踪
        logging.error(f"文档处理失败: {e!s}")
        logging.error(traceback.format_exc())

        return ResponseSchema(status=StatusEnum.ERROR, message=f"文档处理失败: {e!s}")
