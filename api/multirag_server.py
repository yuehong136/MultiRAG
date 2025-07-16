# coding=utf-8
"""
@project: multirag
@Author：龙
@file： multirag_server.py
@date：2024/7/30 18:00
@desc:
"""
from api.utils.log_utils import init_root_logger
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
# from api.db.database import SessionLocal
from api.db.runtime_config import RuntimeConfig
from api.db.services.document_service import DocumentService
from api import settings
from api import utils, validation

from api.db.db_models import init_database_tables as init_web_db, upgrade_database_tables as upgrade_database, SessionLocal
from api.db.init_data import init_web_data
from api.versions import get_multirag_version
import uvicorn
from api.utils import show_configs
from core.settings import print_rag_settings
from core.utils.mcp_tool_call_conn import shutdown_all_mcp_sessions
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
            stop_event.wait(6)
        except Exception:
            logging.exception("update_progress exception")
        finally:
            redis_lock.release()
            if db:
                db.close()

def signal_handler(sig, frame):
    logging.info("Received interrupt signal, shutting down...")
    shutdown_all_mcp_sessions()
    stop_event.set()
    time.sleep(1)
    sys.exit(0)

if __name__ == '__main__':
#     logging.info(r"""
# ┌───────────────────────────  Project Starting ──────────────────────────────┐
# │     __  ___            __   __     _             ____     ___       ______ │
# │    /  |/  /  __  __   / /  / /_   (_)           / __ \   /   |     / ____/ │
# │   / /|_/ /  / / / /  / /  / __/  / /  ______   / /_/ /  / /| |    / / __   │
# │  / /  / /  / /_/ /  / /  / /_   / /  /_____/  / _, _/  / ___ |   / /_/ /   │
# │ /_/  /_/   \__,_/  /_/   \__/  /_/           /_/ |_|  /_/  |_|   \____/    │
# │                                                                            │
# └─────────────────────────────── API Showing ────────────────────────────────┘
#             """)
    logging.info(r"""
============================================================================
     __  ___            __   __     _             ____     ___       ______ 
    /  |/  /  __  __   / /  / /_   (_)           / __ \   /   |     / ____/ 
   / /|_/ /  / / / /  / /  / __/  / /  ______   / /_/ /  / /| |    / / __   
  / /  / /  / /_/ /  / /  / /_   / /  /_____/  / _, _/  / ___ |   / /_/ /   
 /_/  /_/   \__,_/  /_/   \__/  /_/           /_/ |_|  /_/  |_|   \____/    
 
                        ╔╦╗ ┬ ┬ ┬  ┌┬┐ ┬ ┬─┐ ┌─┐ ╔═╗
                        ║║║ │ │ │   │  │ ├┬┘ ├─┤ ║ ╦    【——v0.6.0 preview——】  
                        ╩ ╩ └─┘ ┴─┘ ┴  ┴ ┴└─ ┴ ┴ ╚═╝
============================================================================
                """)

    logging.info(
        f'MultiRAG version: {get_multirag_version()}'
    )
    logging.info(
        f'project base: {utils.file_utils.get_project_base_directory()}'
    )
    show_configs()
    settings.init_settings()
    print_rag_settings()

    if MultiRAG_DEBUGPY_LISTEN > 0:
        logging.info(f"debugpy listen on {MultiRAG_DEBUGPY_LISTEN}")
        import debugpy

        debugpy.listen(("0.0.0.0", MultiRAG_DEBUGPY_LISTEN))

    # 初始化数据库
    init_web_db()
    upgrade_database()
    init_web_data()

    # 初始化运行时配置
    import argparse

    # 解析命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', default=False, help="MultiRAG version", action='store_true')
    parser.add_argument('--debug', default=False, help="debug mode", action='store_true')
    args = parser.parse_args()
    if args.version:
        print(get_multirag_version())
        sys.exit(0)

    RuntimeConfig.DEBUG = args.debug  # 设置调试模式
    if RuntimeConfig.DEBUG:
        logging.info("run on debug mode")

    RuntimeConfig.init_env()  # 初始化环境变量
    RuntimeConfig.init_config(JOB_SERVER_HOST=settings.HOST_IP, HTTP_PORT=settings.HOST_PORT)  # 初始化配置

    GlobalPluginManager.load_plugins()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 进度更新线程现在通过 FastAPI lifespan 事件启动
    # 参见 api/apps/__init__.py 中的 lifespan 函数

    # 使用 uvicorn 启动 FastAPI 应用
    try:
        logging.info("MultiRAG HTTP server start...")
        uvicorn_logger = logging.getLogger("uvicorn.access")  # 获取uvicorn的访问日志记录器
        uvicorn.run("api.multirag_server:app", host=settings.HOST_IP, port=settings.HOST_PORT, log_level="info",
                    reload=RuntimeConfig.DEBUG)  # 启动 uvicorn 服务器
    except Exception:
        traceback.print_exc()
        stop_event.set()
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGKILL)  # 在异常情况下终止进程
