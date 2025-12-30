# coding=utf-8
"""
@project: multirag
@Author：龙
@file： multirag_server.py
@date：2024/7/30 18:00
@desc:
"""
from common.log_utils import init_root_logger
from plugin import GlobalPluginManager
init_root_logger("multirag_server")
# init_root_logger("multirag_server")
# for module in ["pdfminer"]:
#     module_logger = logging.getLogger(module)
#     module_logger.setLevel(logging.WARNING)
# for module in ["sqlalchemy"]:
#     module_logger = logging.getLogger(module)
#     module_logger.handlers.clear()
#     module_logger.propagate = True

import logging
import os
import signal
import sys
import time
import traceback
import threading
import uuid

from api.apps import app
from api.db.runtime_config import RuntimeConfig
from api.db.services.document_service import DocumentService
from common import settings
from common.file_utils import get_project_base_directory

from api.db.db_models import init_database_tables as init_web_db, upgrade_database_tables as upgrade_database, SessionLocal
from api.db.init_data import init_web_data
from common.versions import get_multirag_version
import uvicorn
# from common.config_utils import show_configs
from common.mcp_tool_call_conn import shutdown_all_mcp_sessions
from core.utils.redis_conn import RedisDistributedLock

stop_event = threading.Event()

MultiRAG_DEBUGPY_LISTEN = int(os.environ.get('MultiRAG_DEBUGPY_LISTEN', "0"))

def update_progress():
    """
    定期更新文档服务进度
    """
    lock_value = str(uuid.uuid4())
    redis_lock = RedisDistributedLock("update_progress", lock_value=lock_value, timeout=60)
    logging.info(f"update_progress lock_value: {lock_value}")
    while not stop_event.is_set():
        db = None
        try:
            if redis_lock.acquire():
                db = SessionLocal()  # 创建数据库会话
                DocumentService.update_progress(db)  # 更新文档服务进度
                redis_lock.release()
        except Exception:
            logging.exception("update_progress exception")
        finally:
            try:
                redis_lock.release()
            except Exception:
                logging.exception("update_progress exception")
            stop_event.wait(6)
            if db:
                db.close()

def signal_handler(sig, frame):
    logging.info("Received interrupt signal, shutting down...")
    shutdown_all_mcp_sessions()
    stop_event.set()
    time.sleep(1)
    sys.exit(0)

if __name__ == '__main__':
    # ============================================================================
    # 启动脚本 - 负责进程级别的初始化和服务器启动
    # 
    # 职责划分：
    # - 此文件：进程启动时的一次性操作（数据库迁移、参数解析等）
    # - api/apps/__init__.py lifespan：应用运行时的初始化和资源管理
    # ============================================================================
    
#     logging.info(r"""
# ============================================================================
#      __  ___            __   __     _             ____     ___       ______
#     /  |/  /  __  __   / /  / /_   (_)           / __ \   /   |     / ____/
#    / /|_/ /  / / / /  / /  / __/  / /  ______   / /_/ /  / /| |    / / __
#   / /  / /  / /_/ /  / /  / /_   / /  /_____/  / _, _/  / ___ |   / /_/ /
#  /_/  /_/   \__,_/  /_/   \__/  /_/           /_/ |_|  /_/  |_|   \____/
#
#                         ╔╦╗ ┬ ┬ ┬  ┌┬┐ ┬ ┬─┐ ┌─┐ ╔═╗
#                         ║║║ │ │ │   │  │ ├┬┘ ├─┤ ║ ╦    【——v0.9.7——】
#                         ╩ ╩ └─┘ ┴─┘ ┴  ┴ ┴└─ ┴ ┴ ╚═╝
# ============================================================================
#                 """)
    logging.info(r"""
============================================================================   
                __  ___      ____  _ ____  ___   ______
               /  |/  /_  __/ / /_(_) __ \/   | / ____/
              / /|_/ / / / / / __/ / /_/ / /| |/ / __   v0.9.7
             / /  / / /_/ / / /_/ / _, _/ ___ / /_/ /
            /_/  /_/\__,_/_/\__/_/_/ |_/_/  |_\____/
============================================================================
                """)

    # ============ 版本和环境信息 ============
    logging.info(f'MultiRAG version: {get_multirag_version()}')
    logging.info(f'project base: {get_project_base_directory()}')

    # ============ 命令行参数解析 ============
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', default=False, help="MultiRAG version", action='store_true')
    parser.add_argument('--debug', default=False, help="debug mode", action='store_true')
    args = parser.parse_args()
    
    if args.version:
        print(get_multirag_version())
        sys.exit(0)

    # ============ 运行时配置（需要在数据库初始化前设置）============
    RuntimeConfig.DEBUG = args.debug
    if RuntimeConfig.DEBUG:
        logging.info("Running in DEBUG mode")

    # ============ 开发调试配置 ============
    if MultiRAG_DEBUGPY_LISTEN > 0:
        logging.info(f"Debugpy listening on port {MultiRAG_DEBUGPY_LISTEN}")
        import debugpy
        debugpy.listen(("0.0.0.0", MultiRAG_DEBUGPY_LISTEN))
        logging.info("Waiting for debugger to attach...")

    # ============ 数据库初始化（一次性操作，必须在服务启动前完成）============
    # 这些操作必须在进程启动时执行，不适合放在 lifespan 中
    logging.info("Initializing database schema...")
    init_web_db()        # 创建数据库表结构
    upgrade_database()   # 执行数据库迁移
    init_web_data()      # 初始化默认数据
    logging.info("Database initialization completed")

    # ============ 获取启动参数 ============
    # 注意：settings.init_settings() 已在 api/apps/__init__.py 模块级别执行
    # 因此可以直接使用 settings.HOST_IP 和 settings.HOST_PORT
    # 
    # 热重载说明：
    # - uvicorn --reload 会在代码变更时重新导入模块
    # - 模块重新导入 → settings.init_settings() 重新执行
    # - 因此自动支持配置热重载，无需额外处理
    
    # ============ 运行时环境配置 ============
    RuntimeConfig.init_env()
    RuntimeConfig.init_config(
        JOB_SERVER_HOST=settings.HOST_IP,
        HTTP_PORT=settings.HOST_PORT
    )

    # ============ 插件系统加载 ============
    logging.info("Loading plugins...")
    GlobalPluginManager.load_plugins()

    # ============ 信号处理注册 ============
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ============ 启动 FastAPI 应用服务器 ============
    # 注意：以下功能在 api/apps/__init__.py 中执行：
    # 
    # 【模块级别（导入时）】：
    # • settings.init_settings() - 全局配置初始化（只执行一次）
    # • LoginManager 初始化 - 需要 SECRET_KEY
    # • SQLAdmin 初始化 - 需要数据库配置
    # • 路由注册
    # 
    # 【lifespan 函数（应用启动时）】：
    # • settings.print_rag_settings() - 打印 RAG 配置
    # • show_configs() - 显示配置信息
    # • update_progress thread - 进度更新后台线程
    # • SMTP mail server - 邮件服务初始化
    # • workflow_state_manager - 工作流状态管理
    # 
    # 【热重载说明】：
    # • uvicorn --reload 会在代码变更时重新导入模块
    # • 模块重新导入 → settings.init_settings() 自动重新执行
    # • 无需在 lifespan 中重复初始化
    
    try:
        logging.info(f"Starting MultiRAG HTTP server on {settings.HOST_IP}:{settings.HOST_PORT}...")
        uvicorn.run(
            "api.multirag_server:app",
            host=settings.HOST_IP,
            port=settings.HOST_PORT,
            log_level="info",
            reload=RuntimeConfig.DEBUG,
        )
    except Exception:
        logging.exception("Failed to start MultiRAG server")
        traceback.print_exc()
        stop_event.set()
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGKILL)
