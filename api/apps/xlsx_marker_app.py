"""
XLSX 模板标记与填充接口

无状态接口，适用于集成到其他系统，由外部系统管理文档存储。
这些接口不存储任何数据。与 docx_marker 三接口同构。

接口列表：
- POST /parse - 解析 XLSX 文件，返回工作簿结构
- POST /recognize - 自动识别待填项
- POST /fill - 填充数据并返回文档
"""

import base64
import json
import logging
import os
import shutil
import uuid
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from api.service.xlsx_marker_service import (
    auto_recognize_placeholders,
    fill_workbook,
    parse_xlsx,
)
from api.utils.api_utils import get_json_result

router = APIRouter()
logger = logging.getLogger(__name__)

# 临时文件存放目录
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "temp", "xlsx_marker")
# 日志文件存放目录（用于观察），与 docx_marker 同机制
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "temp", "xlsx_marker_logs")
# 最大保留的请求数量
LOG_MAX_REQUESTS = 10


def ensure_dir(path: str):
    if not os.path.exists(path):
        os.makedirs(path)


def cleanup_old_logs():
    """清理旧的日志目录，只保留最近 LOG_MAX_REQUESTS 次请求的数据"""
    if not os.path.exists(LOG_DIR):
        return
    subdirs = []
    for name in os.listdir(LOG_DIR):
        path = os.path.join(LOG_DIR, name)
        if os.path.isdir(path):
            subdirs.append((path, os.path.getctime(path)))
    if len(subdirs) <= LOG_MAX_REQUESTS:
        return
    subdirs.sort(key=lambda x: x[1])
    for dir_path, _ in subdirs[:-LOG_MAX_REQUESTS]:
        try:
            shutil.rmtree(dir_path)
            logger.info(f"[cleanup] 已删除旧日志目录: {os.path.basename(dir_path)}")
        except Exception as e:
            logger.warning(f"[cleanup] 删除目录失败 {dir_path}: {e}")


def save_fill_log(request_id: str, stage: str, file_content: bytes, json_data: dict | None = None, filename: str | None = None):
    """保存 fill 接口处理日志（输入/输出文件 + JSON 数据）"""
    ensure_dir(LOG_DIR)
    if stage == "input":
        cleanup_old_logs()
    request_dir = os.path.join(LOG_DIR, request_id)
    ensure_dir(request_dir)

    file_suffix = f"_{filename}" if filename else ""
    file_path = os.path.join(request_dir, f"{stage}{file_suffix}.xlsx")
    with open(file_path, "wb") as f:
        f.write(file_content)

    if json_data is not None:
        json_path = os.path.join(request_dir, f"{stage}_data.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
    return request_dir


def _check_extension(filename: str):
    if not filename.endswith(".xlsx"):
        detail = "只支持 .xlsx 文件（.xls 老格式请先另存为 .xlsx）" if filename.endswith(".xls") else "只支持 .xlsx 文件"
        raise HTTPException(status_code=400, detail=detail)


@router.post("/parse", summary="解析 XLSX 文件", response_description="返回解析后的工作簿结构")
async def stateless_parse(file: UploadFile = File(...)):
    """
    ### POST `/v1/xlsx_marker/parse` 无状态解析接口

    解析 XLSX 文件，返回工作簿结构（ParsedWorkbook：多 sheet 网格，
    含合并单元格、样式、下拉校验选项、公式标识），不存储任何数据。

    响应 data: `{"workbook": {...}, "filename": "template.xlsx"}`
    """
    _check_extension(file.filename)

    ensure_dir(TEMP_DIR)
    temp_path = os.path.join(TEMP_DIR, f"temp_{uuid.uuid4()}.xlsx")
    try:
        content = await file.read()
        with open(temp_path, "wb") as f:
            f.write(content)

        parsed = parse_xlsx(temp_path, file.filename)
        return get_json_result(retmsg="Workbook parsed successfully.", data={"workbook": parsed.model_dump(), "filename": file.filename})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[parse] 解析失败: {e!s}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"解析失败: {e!s}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.post("/recognize", summary="自动识别待填项", response_description="返回识别到的待填项列表")
async def stateless_recognize(file: UploadFile = File(...)):
    """
    ### POST `/v1/xlsx_marker/recognize` 无状态自动识别接口

    启发式识别待填单元格（右填充/下填充 + 填充色收敛）与表格区域
    （连续多列表头 + 下方成片空行 -> dynamic_table，区域内 cell 候选剔除），
    识别不到的区域由前端人工标记兜底。

    响应 data: `{"placeholders": [...], "total": N}`
    """
    _check_extension(file.filename)

    ensure_dir(TEMP_DIR)
    temp_path = os.path.join(TEMP_DIR, f"temp_{uuid.uuid4()}.xlsx")
    try:
        content = await file.read()
        with open(temp_path, "wb") as f:
            f.write(content)

        workbook = parse_xlsx(temp_path, file.filename)
        recognized = auto_recognize_placeholders(workbook)
        return get_json_result(retmsg="Placeholders recognized successfully.", data={"placeholders": [p.model_dump() for p in recognized], "total": len(recognized)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[recognize] 自动识别失败: {e!s}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"自动识别失败: {e!s}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.post("/fill", summary="填充数据并返回文档", response_description="返回填充后的文档（Base64 编码）")
async def stateless_fill(file: UploadFile = File(...), placeholders: str = Form(...), fields: str = Form(...)):
    """
    ### POST `/v1/xlsx_marker/fill` 无状态填充接口

    XML 直改填充：只修改目标工作表 XML，形状/图片/图表等元素全部原样保留。
    - cell/summary：按 path 写入单元格
    - table：固定区域填充，超出预留行截断
    - dynamic_table：数据行超出预留区时自动插行（合并区/校验区/绘图锚点联动平移）
    写后自校验（重开文件 + 逐格读回比对），失败即报错，绝不产出可疑文件。

    请求 (multipart/form-data)：file + placeholders(JSON) + fields(JSON)，
    结构与 docx_marker/fill 同构。

    响应 data: `{"file": "<base64>", "filename": "filled_template.xlsx"}`
    """
    _check_extension(file.filename)

    try:
        placeholders_data = json.loads(placeholders)
        fields_data = json.loads(fields)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败: {e!s}")

    if not isinstance(placeholders_data, list):
        raise HTTPException(status_code=400, detail="placeholders 必须是数组")
    if not isinstance(fields_data, list):
        raise HTTPException(status_code=400, detail="fields 必须是数组")
    if not placeholders_data:
        raise HTTPException(status_code=400, detail="没有标记任何待填项")

    request_id = f"fill_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    logger.info(f"[fill] 请求ID: {request_id}, 文件名: {file.filename}, 待填项: {len(placeholders_data)}, 字段: {len(fields_data)}")

    ensure_dir(TEMP_DIR)
    temp_id = str(uuid.uuid4())
    source_path = os.path.join(TEMP_DIR, f"temp_{temp_id}.xlsx")
    output_path = os.path.join(TEMP_DIR, f"temp_{temp_id}_filled.xlsx")

    try:
        content = await file.read()
        with open(source_path, "wb") as f:
            f.write(content)
        save_fill_log(request_id, "input", content, {"placeholders": placeholders_data, "fields": fields_data}, file.filename)

        fill_workbook(source_path, placeholders_data, fields_data, output_path)

        with open(output_path, "rb") as f:
            filled_content = f.read()
        save_fill_log(request_id, "output", filled_content, filename=file.filename)

        return get_json_result(
            retmsg="Workbook filled successfully.",
            data={"file": base64.b64encode(filled_content).decode(), "filename": f"filled_{file.filename}"},
        )
    except (ValueError, RuntimeError) as e:
        logger.error(f"[fill] 填充失败 请求ID: {request_id}: {e!s}")
        raise HTTPException(status_code=400, detail=f"填充失败: {e!s}")
    except Exception as e:
        logger.error(f"[fill] 填充失败 请求ID: {request_id}: {e!s}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"填充失败: {e!s}")
    finally:
        for path in (source_path, output_path):
            if os.path.exists(path):
                os.remove(path)
