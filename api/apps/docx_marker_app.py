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
import shutil
import logging
from io import BytesIO
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from fastapi.responses import StreamingResponse
import base64

from api.utils.api_utils import get_json_result
from api.service.docx_marker_service import (
    parse_docx,
    auto_recognize_placeholders,
    fill_document,
    fill_document_with_tables,
    generate_debug_report,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# 临时文件存放目录
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "temp", "docx_marker")
# 日志文件存放目录（用于观察）
LOG_DIR = "/Users/naimehao/PycharmProjects/multrag/temp/docx_marker"
# 最大保留的请求数量
LOG_MAX_REQUESTS = 10


def ensure_temp_dir():
    """确保临时目录存在"""
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)


def ensure_log_dir():
    """确保日志目录存在"""
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)


def cleanup_old_logs():
    """
    清理旧的日志目录，只保留最近 LOG_MAX_REQUESTS 次请求的数据
    按目录创建时间排序，删除最旧的
    """
    if not os.path.exists(LOG_DIR):
        return

    # 获取所有子目录
    subdirs = []
    for name in os.listdir(LOG_DIR):
        path = os.path.join(LOG_DIR, name)
        if os.path.isdir(path):
            # 获取目录创建时间
            ctime = os.path.getctime(path)
            subdirs.append((path, ctime))

    # 如果目录数量未超过限制，不需要清理
    if len(subdirs) <= LOG_MAX_REQUESTS:
        return

    # 按创建时间排序（最旧的在前）
    subdirs.sort(key=lambda x: x[1])

    # 删除最旧的目录，保留最近 LOG_MAX_REQUESTS 个
    dirs_to_delete = subdirs[:-LOG_MAX_REQUESTS]
    for dir_path, _ in dirs_to_delete:
        try:
            shutil.rmtree(dir_path)
            logger.info(f"[cleanup] 已删除旧日志目录: {os.path.basename(dir_path)}")
        except Exception as e:
            logger.warning(f"[cleanup] 删除目录失败 {dir_path}: {e}")


def save_fill_log(request_id: str, stage: str, file_content: bytes, json_data: dict = None, filename: str = None):
    """
    保存 fill 接口处理日志

    Args:
        request_id: 请求唯一标识符
        stage: 阶段标识，如 "input", "output"
        file_content: 文件内容（bytes）
        json_data: JSON 数据（可选）
        filename: 原始文件名（可选）
    """
    ensure_log_dir()

    # 在保存新日志前清理旧日志
    if stage == "input":
        cleanup_old_logs()

    # 创建请求专属目录
    request_dir = os.path.join(LOG_DIR, request_id)
    if not os.path.exists(request_dir):
        os.makedirs(request_dir)

    # 保存文件
    file_suffix = f"_{filename}" if filename else ""
    docx_path = os.path.join(request_dir, f"{stage}{file_suffix}.docx")
    with open(docx_path, "wb") as f:
        f.write(file_content)

    # 保存 JSON 数据（如果有）
    if json_data is not None:
        json_path = os.path.join(request_dir, f"{stage}_data.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

    return request_dir


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

    # 生成请求唯一标识符
    request_id = f"fill_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    logger.info(f"[fill] 请求ID: {request_id}, 文件名: {file.filename}")

    ensure_temp_dir()
    temp_id = str(uuid.uuid4())
    source_path = os.path.join(TEMP_DIR, f"temp_{temp_id}.docx")
    output_path = os.path.join(TEMP_DIR, f"temp_{temp_id}_filled.docx")

    try:
        content = await file.read()

        # 保存输入文件和 JSON 数据到日志目录
        save_fill_log(request_id, "input", content, {
            "placeholders": placeholders_data,
            "fields": fields_data
        }, file.filename)
        logger.info(f"[fill] 输入文件已保存到日志目录")

        with open(source_path, "wb") as f:
            f.write(content)

        # 构建数据字典，支持表格类型
        data_dict = {}
        for field in fields_data:
            field_id = field.get('id')
            if not field_id:
                continue
            if 'rows' in field and field['rows'] is not None:
                # 表格类型：包含 rows 数据
                data_dict[field_id] = {"rows": field['rows'], "value": field.get('value', '')}
            else:
                # 普通类型：只有 value
                data_dict[field_id] = field.get('value', '')

        # 检查是否有表格类型的 placeholder
        has_table_placeholder = any(
            p.get('type') in ('table', 'dynamic_table') for p in placeholders_data
        )

        # 填充文档
        if has_table_placeholder:
            fill_document_with_tables(source_path, placeholders_data, data_dict, output_path)
        else:
            fill_document(source_path, placeholders_data, data_dict, output_path)

        # 读取填充后的文件并编码为 Base64
        with open(output_path, "rb") as f:
            output_content = f.read()

        # 保存输出文件到日志目录
        save_fill_log(request_id, "output", output_content, filename=f"filled_{file.filename}")
        logger.info(f"[fill] 输出文件已保存到日志目录")
        logger.info(f"[fill] 请求ID: {request_id} 处理完成")

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

        # 构建数据字典，支持表格类型
        data_dict = {}
        for field in fields_data:
            field_id = field.get('id')
            if not field_id:
                continue
            if 'rows' in field and field['rows'] is not None:
                # 表格类型：包含 rows 数据
                data_dict[field_id] = {"rows": field['rows'], "value": field.get('value', '')}
            else:
                # 普通类型：只有 value
                data_dict[field_id] = field.get('value', '')

        # 检查是否有表格类型的 placeholder
        has_table_placeholder = any(
            p.get('type') in ('table', 'dynamic_table') for p in placeholders_data
        )

        # 填充文档
        if has_table_placeholder:
            fill_document_with_tables(source_path, placeholders_data, data_dict, output_path)
        else:
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
