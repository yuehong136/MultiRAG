# coding=utf-8
"""
DOCX 模板标记与填充接口

无状态接口，适用于集成到其他系统，由外部系统管理文档存储。
这些接口不存储任何数据。

接口列表：
- POST /parse - 解析 DOCX 文件，返回文档结构
- POST /recognize - 自动识别待填项
- POST /fill - 填充数据并返回文档
"""

import json
import uuid
import os
import logging
from io import BytesIO

from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from fastapi.responses import StreamingResponse
import base64

from api.utils.api_utils import get_json_result
from api.service.docx_marker_service import (
    parse_docx,
    auto_recognize_placeholders,
    fill_document,
    generate_debug_report,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# 临时文件存放目录
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "temp", "docx_marker")


def ensure_temp_dir():
    """确保临时目录存在"""
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)


@router.post("/parse", summary="解析 DOCX 文件",
             response_description="返回解析后的文档结构")
async def stateless_parse(
        file: UploadFile = File(...)
):
    """
    ### POST `/v1/docx_marker/parse` 无状态解析接口

    **功能描述**:
    解析 DOCX 文件，返回文档结构（ParsedDocument），不存储任何数据。

    ---

    ### 请求体 (Request Body)

    | 字段   | 类型         | 必填 | 描述                                    |
    |--------|--------------|------|-----------------------------------------|
    | `file` | `UploadFile` | 是   | 用户上传的 Word 文档，格式必须为 `.docx` |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)

    ```json
    {
        "retcode": 0,
        "retmsg": "Document parsed successfully.",
        "data": {
            "document": { ... },
            "filename": "template.docx"
        }
    }
    ```

    ---

    ### 适用场景

    外部系统自行管理文档存储，只需要解析能力。
    """
    if not file.filename.endswith('.docx'):
        raise HTTPException(status_code=400, detail="只支持 .docx 文件")

    ensure_temp_dir()
    temp_id = str(uuid.uuid4())
    temp_path = os.path.join(TEMP_DIR, f"temp_{temp_id}.docx")

    try:
        content = await file.read()
        with open(temp_path, "wb") as f:
            f.write(content)

        # 解析文档
        parsed = parse_docx(temp_path, file.filename)

        return get_json_result(
            retmsg="Document parsed successfully.",
            data={
                "document": parsed.model_dump(),
                "filename": file.filename
            }
        )
    except Exception as e:
        logger.error(f"[parse] 解析失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"解析失败: {str(e)}")
    finally:
        # 清理临时文件
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.post("/recognize", summary="自动识别待填项",
             response_description="返回识别到的待填项列表")
async def stateless_recognize(
        file: UploadFile = File(...)
):
    """
    ### POST `/v1/docx_marker/recognize` 无状态自动识别接口

    **功能描述**:
    识别文档中的待填充位置，返回待填项列表，不存储任何数据。

    ---

    ### 请求体 (Request Body)

    | 字段   | 类型         | 必填 | 描述                                    |
    |--------|--------------|------|-----------------------------------------|
    | `file` | `UploadFile` | 是   | 用户上传的 Word 文档，格式必须为 `.docx` |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)

    ```json
    {
        "retcode": 0,
        "retmsg": "Placeholders recognized successfully.",
        "data": {
            "placeholders": [
                {
                    "path": "body[1]/row[0]/cell[1]",
                    "label": "姓名",
                    "field_key": "$1",
                    "custom_fields": {}
                }
            ],
            "total": 10
        }
    }
    ```

    ---

    ### 识别规则

    1. **表格空单元格**：右填充、下填充
    2. **表格非空单元格**：包含冒号且独占整行
    3. **段落下划线**：标签 + 冒号 + 下划线区域

    ---

    ### 适用场景

    外部系统自行管理标记数据，只需要识别能力。
    """
    if not file.filename.endswith('.docx'):
        raise HTTPException(status_code=400, detail="只支持 .docx 文件")

    ensure_temp_dir()
    temp_id = str(uuid.uuid4())
    temp_path = os.path.join(TEMP_DIR, f"temp_{temp_id}.docx")

    try:
        content = await file.read()
        with open(temp_path, "wb") as f:
            f.write(content)

        # 自动识别
        recognized = auto_recognize_placeholders(temp_path)

        return get_json_result(
            retmsg="Placeholders recognized successfully.",
            data={
                "placeholders": [p.model_dump() for p in recognized],
                "total": len(recognized)
            }
        )
    except Exception as e:
        logger.error(f"[recognize] 自动识别失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"自动识别失败: {str(e)}")
    finally:
        # 清理临时文件
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.post("/fill", summary="填充数据并返回文档",
             response_description="返回填充后的文档（Base64 编码）")
async def stateless_fill(
        file: UploadFile = File(...),
        placeholders: str = Form(...),
        fields: str = Form(...)
):
    """
    ### POST `/v1/docx_marker/fill` 无状态填充接口

    **功能描述**:
    填充数据并返回文档，不存储任何数据。

    ---

    ### 请求参数 (multipart/form-data)

    | 字段           | 类型         | 必填 | 描述                                      |
    |----------------|--------------|------|-------------------------------------------|
    | `file`         | `UploadFile` | 是   | 用户上传的 Word 文档                      |
    | `placeholders` | `string`     | 是   | JSON 字符串，待填项列表                   |
    | `fields`       | `string`     | 是   | JSON 字符串，填充值列表                   |

    #### placeholders 格式示例

    ```json
    [
        {
            "path": "body[1]/row[0]/cell[1]",
            "label": "姓名",
            "field_key": "$1"
        }
    ]
    ```

    #### fields 格式示例

    ```json
    [
        {"id": "$1", "value": "张三"},
        {"id": "$2", "value": "25"}
    ]
    ```

    ---

    ### 响应 (Response)

    #### 成功响应 (200)

    ```json
    {
        "retcode": 0,
        "retmsg": "Document filled successfully.",
        "data": {
            "file": "base64-encoded-filled-file-content",
            "filename": "filled_template.docx"
        }
    }
    ```

    ---

    ### 适用场景

    外部系统自行管理文档和标记数据，只需要填充能力。
    """
    if not file.filename.endswith('.docx'):
        raise HTTPException(status_code=400, detail="只支持 .docx 文件")

    # 解析 JSON 参数
    try:
        placeholders_data = json.loads(placeholders)
        fields_data = json.loads(fields)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败: {str(e)}")

    # 验证数据格式
    if not isinstance(placeholders_data, list):
        raise HTTPException(status_code=400, detail="placeholders 必须是数组")
    if not isinstance(fields_data, list):
        raise HTTPException(status_code=400, detail="fields 必须是数组")

    if not placeholders_data:
        raise HTTPException(status_code=400, detail="没有标记任何待填项")

    ensure_temp_dir()
    temp_id = str(uuid.uuid4())
    source_path = os.path.join(TEMP_DIR, f"temp_{temp_id}.docx")
    output_path = os.path.join(TEMP_DIR, f"temp_{temp_id}_filled.docx")

    try:
        content = await file.read()
        with open(source_path, "wb") as f:
            f.write(content)

        # 将数组格式转换为字典格式
        data_dict = {field['id']: field['value'] for field in fields_data}

        # 填充文档
        fill_document(source_path, placeholders_data, data_dict, output_path)

        # 读取填充后的文件并编码为 Base64
        with open(output_path, "rb") as f:
            output_content = f.read()

        base64_encoded_file = base64.b64encode(output_content).decode("utf-8")

        return get_json_result(
            retmsg="Document filled successfully.",
            data={
                "file": base64_encoded_file,
                "filename": f"filled_{file.filename}"
            }
        )
    except Exception as e:
        logger.error(f"[fill] 填充失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"填充失败: {str(e)}")
    finally:
        # 清理临时文件
        if os.path.exists(source_path):
            os.remove(source_path)
        if os.path.exists(output_path):
            os.remove(output_path)


@router.post("/fill_download", summary="填充数据并下载文档",
             response_description="返回填充后的文档文件流")
async def stateless_fill_download(
        file: UploadFile = File(...),
        placeholders: str = Form(...),
        fields: str = Form(...)
):
    """
    ### POST `/v1/docx_marker/fill_download` 无状态填充并下载接口

    **功能描述**:
    填充数据并直接返回文档文件流（非 Base64），适合直接下载。

    ---

    ### 请求参数

    与 `/fill` 接口相同。

    ---

    ### 响应

    直接返回 `.docx` 文件流，可直接下载保存。
    """
    if not file.filename.endswith('.docx'):
        raise HTTPException(status_code=400, detail="只支持 .docx 文件")

    # 解析 JSON 参数
    try:
        placeholders_data = json.loads(placeholders)
        fields_data = json.loads(fields)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败: {str(e)}")

    # 验证数据格式
    if not isinstance(placeholders_data, list):
        raise HTTPException(status_code=400, detail="placeholders 必须是数组")
    if not isinstance(fields_data, list):
        raise HTTPException(status_code=400, detail="fields 必须是数组")

    if not placeholders_data:
        raise HTTPException(status_code=400, detail="没有标记任何待填项")

    ensure_temp_dir()
    temp_id = str(uuid.uuid4())
    source_path = os.path.join(TEMP_DIR, f"temp_{temp_id}.docx")
    output_path = os.path.join(TEMP_DIR, f"temp_{temp_id}_filled.docx")

    try:
        content = await file.read()
        with open(source_path, "wb") as f:
            f.write(content)

        # 将数组格式转换为字典格式
        data_dict = {field['id']: field['value'] for field in fields_data}

        # 填充文档
        fill_document(source_path, placeholders_data, data_dict, output_path)

        # 读取填充后的文件
        with open(output_path, "rb") as f:
            output_content = f.read()

        # 清理临时文件
        if os.path.exists(source_path):
            os.remove(source_path)
        if os.path.exists(output_path):
            os.remove(output_path)

        # 返回文件流
        return StreamingResponse(
            BytesIO(output_content),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f"attachment; filename=filled_{file.filename}"
            }
        )
    except Exception as e:
        logger.error(f"[fill_download] 填充失败: {str(e)}", exc_info=True)
        # 清理临时文件
        if os.path.exists(source_path):
            os.remove(source_path)
        if os.path.exists(output_path):
            os.remove(output_path)
        raise HTTPException(status_code=500, detail=f"填充失败: {str(e)}")


@router.post("/debug", summary="获取文档调试信息",
             response_description="返回文档结构和识别结果")
async def stateless_debug(
        file: UploadFile = File(...)
):
    """
    ### POST `/v1/docx_marker/debug` 调试接口

    **功能描述**:
    获取文档的调试信息，包括文档结构和自动识别结果。

    ---

    ### 响应 (Response)

    ```json
    {
        "retcode": 0,
        "retmsg": "Debug report generated successfully.",
        "data": {
            "recognized_count": 10,
            "recognized_placeholders": [...],
            "report": "..."
        }
    }
    ```
    """
    if not file.filename.endswith('.docx'):
        raise HTTPException(status_code=400, detail="只支持 .docx 文件")

    ensure_temp_dir()
    temp_id = str(uuid.uuid4())
    temp_path = os.path.join(TEMP_DIR, f"temp_{temp_id}.docx")

    try:
        content = await file.read()
        with open(temp_path, "wb") as f:
            f.write(content)

        # 自动识别
        recognized = auto_recognize_placeholders(temp_path)
        recognized_dicts = [p.model_dump() for p in recognized]

        # 生成调试报告
        report = generate_debug_report(temp_path, recognized_dicts)

        return get_json_result(
            retmsg="Debug report generated successfully.",
            data={
                "filename": file.filename,
                "recognized_count": len(recognized),
                "recognized_placeholders": recognized_dicts,
                "report": report
            }
        )
    except Exception as e:
        logger.error(f"[debug] 生成调试信息失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"生成调试信息失败: {str(e)}")
    finally:
        # 清理临时文件
        if os.path.exists(temp_path):
            os.remove(temp_path)
