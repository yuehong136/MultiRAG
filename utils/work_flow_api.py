import inspect
import logging
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
import json
from pathlib import Path
from core.components import get_action_handlers, convert_to_serializable

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

        action_handlers = get_action_handlers()

        for step in workflow:
            action = step["action"]
            params = step.get("params", {})
            logger.info(f"执行步骤: {step}")
            logger.debug(f"传递的参数: {params}")

            if action in action_handlers:
                handler = action_handlers[action]
                logger.debug(f"调用处理函数: {handler.__name__}，参数: {params}")
                if inspect.iscoroutinefunction(handler):
                    result = await handler(result, file=file, **params)  # 异步调用
                else:
                    result = handler(result, **params)  # 同步调用
            else:
                raise HTTPException(status_code=400, detail=f"未知的操作: {action}")

        serializable_result = convert_to_serializable(result)
        logger.info(f"工作流执行结果: {serializable_result}")
        return {"result": serializable_result}

    except Exception as e:
        logger.error(f"执行工作流时出错: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")

# def get_action_handlers():
#     """
#     获取所有action的映射关系
#     """
#     import core.components.file_operations as file_ops
#     import core.components.data_processing as data_ops
#     import core.components.sql_operations as sql_ops
#     import core.components.nl2sql as nl2sql
#
#     handlers = {}
#
#     for module in [file_ops, data_ops, sql_ops, nl2sql]:
#         for name in dir(module):
#             obj = getattr(module, name)
#             if callable(obj) and obj.__doc__:
#                 handlers[obj.__doc__.strip()] = obj
#
#     # 单独处理文件上传操作
#     handlers["上传文件"] = handle_upload_file
#
#     return handlers
