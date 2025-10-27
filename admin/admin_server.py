import logging
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from routes import admin_router
from api.utils.log_utils import init_root_logger
from api.constants import SERVICE_CONF
from config import load_configurations, SERVICE_CONFIGS
from exceptions import setup_exception_handlers


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
    
    SERVICE_CONFIGS.configs = load_configurations(SERVICE_CONF)
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
        reload=True,
        log_level="info"
    )
