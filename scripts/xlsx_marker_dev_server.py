"""
xlsx_marker 前端联调用最小服务

只挂载 /v1/xlsx_marker 三个无状态接口（parse/recognize/fill），不依赖
DB/ES 等基础设施，供 docx-marker 组件独立应用模式本地联调使用。

启动（multrag 根目录）：
    .venv/bin/python scripts/xlsx_marker_dev_server.py  # 监听 127.0.0.1:8123
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from fastapi import FastAPI

from api.apps.xlsx_marker_app import router

app = FastAPI(title="xlsx_marker dev server")
app.include_router(router, prefix="/v1/xlsx_marker", tags=["xlsx_marker"])


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8123)
