import logging
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
import json
import pandas as pd
import numpy as np
import io
from pathlib import Path
from core.components.file_operations import upload_file
from core.components.data_processing import process_data
from core.components.sql_operations import execute_sql
from core.components.nl2sql import input_nl_query, semantic_parsing, db_schema_understanding, generate_sql

# 设置日志记录
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("workflow_api")

app = FastAPI()

DEFAULT_WORKFLOW_PATH = Path(r"E:\Project\python\study\RAG\workflow.json")
UPLOADED_WORKFLOW_PATH = Path(r"E:\Project\python\study\RAG\workflow.json")

def read_workflow(use_uploaded: bool = False):
    path = UPLOADED_WORKFLOW_PATH if use_uploaded and UPLOADED_WORKFLOW_PATH.exists() else DEFAULT_WORKFLOW_PATH
    try:
        with path.open("r", encoding="utf-8") as f:
            logger.info(f"读取工作流配置文件: {path}")
            return json.load(f)
    except FileNotFoundError:
        logger.error("工作流文件未找到")
        raise HTTPException(status_code=404, detail="Workflow file not found")

def convert_to_serializable(data):
    if isinstance(data, pd.DataFrame):
        return data.astype(str).to_dict(orient="records")  # 转换为字符串以确保兼容性
    elif isinstance(data, np.generic):
        return data.item()
    elif isinstance(data, dict):
        return {k: convert_to_serializable(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [convert_to_serializable(i) for i in data]
    return data

@app.get("/")
def read_root():
    logger.info("根路径被访问")
    return {"message": "欢迎使用工作流API"}

@app.post("/upload-workflow")
async def upload_workflow(file: UploadFile = File(...)):
    try:
        content = await file.read()
        with UPLOADED_WORKFLOW_PATH.open("w", encoding="utf-8") as f:
            f.write(content.decode('utf-8'))
        logger.info(f"上传并保存工作流文件: {UPLOADED_WORKFLOW_PATH}")
        return {"message": "工作流上传成功"}
    except Exception as e:
        logger.error(f"上传工作流文件时出错: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")

@app.post("/execute")
async def execute_workflow(file: UploadFile = File(None), use_uploaded: bool = Depends(lambda: UPLOADED_WORKFLOW_PATH.exists())):
    try:
        logger.info("执行工作流")
        workflow = read_workflow(use_uploaded=use_uploaded)
        result = None

        for step in workflow:
            action = step["action"]
            logger.info(f"执行步骤: {step}")

            match action:
                case "上传文件":
                    if file is not None:
                        result = await handle_upload_file(file)
                    else:
                        raise HTTPException(status_code=400, detail="未提供文件进行上传")
                case "数据处理":
                    if result is not None:
                        result = process_data(result, step["params"]["method"])
                    else:
                        raise HTTPException(status_code=400, detail="没有数据可处理")
                case "显示结果":
                    if result is not None:
                        result = display_results(result)
                    else:
                        raise HTTPException(status_code=400, detail="没有数据可显示")
                case "输入自然语言查询":
                    result = input_nl_query(step["params"]["query"])
                case "语义解析":
                    result = semantic_parsing(result)
                case "数据库模式理解":
                    result = db_schema_understanding()
                case "生成 SQL":
                    result = generate_sql(result, db_schema_understanding())
                case "执行 SQL":
                    if result is not None:
                        result = execute_sql(result)
                    else:
                        raise HTTPException(status_code=400, detail="没有要执行的SQL查询")

        serializable_result = convert_to_serializable(result)
        logger.info(f"工作流执行结果: {serializable_result}")
        return {"result": serializable_result}

    except Exception as e:
        logger.error(f"执行工作流时出错: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")

async def handle_upload_file(file: UploadFile):
    logger.info("处理上传的文件")
    content = await file.read()
    try:
        data = pd.read_csv(io.StringIO(content.decode('utf-8')))
        logger.info(f"文件内容: {data.head()}")
        return data
    except pd.errors.ParserError as e:
        logger.error(f"解析CSV文件时出错: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"解析CSV文件时出错: {e}")

def display_results(data):
    logger.info("显示结果")
    return convert_to_serializable(data)
