import logging
import os
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from starlette.middleware.base import BaseHTTPMiddleware

from routes import admin_router
from auth import init_default_admin
from api.utils.log_utils import init_root_logger
from common.constants import SERVICE_CONF
from common.config_utils import show_configs
from api import settings
from config import load_configurations, SERVICE_CONFIGS
from api.common.exceptions import setup_exception_handlers


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """限制请求体大小的中间件"""
    def __init__(self, app, max_content_length: int):
        super().__init__(app)
        self.max_content_length = max_content_length

    async def dispatch(self, request: Request, call_next):
        # 检查 Content-Length header
        content_length = request.headers.get("content-length")
        if content_length:
            content_length = int(content_length)
            if content_length > self.max_content_length:
                raise HTTPException(
                    status_code=413,
                    detail=f"Request body too large. Maximum size: {self.max_content_length} bytes"
                )
        
        response = await call_next(request)
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期管理"""
    # 启动时执行
    init_root_logger("admin_service")
    logging.info(r"""
        __  ___      __  _ ____  ___   ______   ___       __          _     
       /  |/  /_  __/ /_(_) __ \/   | / ____/  /   | ____/ /___ ___  (_)___ 
      / /|_/ / / / / __/ / /_/ / /| |/ / __   / /| |/ __  / __ `__ \/ / __ \
     / /  / / /_/ / /_/ / _, _/ ___ / /_/ /  / ___ / /_/ / / / / / / / / / /
    /_/  /_/\__,_/\__/_/_/ |_/_/  |_\____/  /_/  |_\__,_/_/ /_/ /_/_/_/ /_/ 
    """)

    # settings 已在 auth.py 模块导入时初始化，这里确保初始化完成
    if settings.SECRET_KEY is None:
        settings.init_settings()
    
    # 加载服务配置
    SERVICE_CONFIGS.configs = load_configurations(SERVICE_CONF)

    show_configs()

    # 初始化默认管理员账号
    from api.db.db_models import SessionLocal
    db = SessionLocal()
    try:
        init_default_admin(db)
    except Exception as e:
        logging.error(f"Failed to init default admin: {e}")
    finally:
        db.close()
    
    logging.info("MultiRAG Admin service started...")
    
    yield
    
    # 关闭时执行
    logging.info("MultiRAG Admin service shutting down...")


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例"""
    app = FastAPI(
        title="RAGFlow Admin API",
        description="RAGFlow 管理后台 API",
        version="1.0.0",
        lifespan=lifespan
    )
    
    # 配置最大请求体大小（默认 1GB）
    max_content_length = int(os.environ.get("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))
    # 添加请求体大小限制中间件
    app.add_middleware(RequestSizeLimitMiddleware, max_content_length=max_content_length)
    
    # 配置 CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # 注册路由
    app.include_router(admin_router, prefix="/api/v1/admin")
    
    # 设置异常处理器
    setup_exception_handlers(app)
    
    return app


app = create_app()


if __name__ == '__main__':
    uvicorn.run(
        "admin_server:app",
        host="0.0.0.0",
        port=8130,
        reload=False,
        log_level="info",
        limit_max_requests=10000,  # 最大请求数（进程重启前的请求数）
        timeout_keep_alive=5,       # Keep-Alive 超时
        # 请求体大小限制已通过 RequestSizeLimitMiddleware 实现
    )
