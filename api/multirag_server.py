# coding=utf-8
"""
@project: multirag
@Author：龙
@file： multirag_server.py
@date：2024/7/30 18:00
@desc:
"""
import logging
from api.utils.log_utils import initRootLogger
initRootLogger("multirag_server")
for module in ["pdfminer"]:
    module_logger = logging.getLogger(module)
    module_logger.setLevel(logging.WARNING)
for module in ["sqlalchemy"]:
    module_logger = logging.getLogger(module)
    module_logger.handlers.clear()
    module_logger.propagate = True
import os
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

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
from core.settings import print_multirag_settings


def update_progress():
    """
    定期更新文档服务进度
    """
    while True:
        time.sleep(6)
        db = None
        try:
            db = SessionLocal()  # 创建数据库会话
            DocumentService.update_progress(db)  # 更新文档服务进度
        except Exception:
            logging.exception("update_progress exception")
        finally:
            if db:
                db.close()


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
    print_multirag_settings()

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

    # 启动进度更新线程
    thread = ThreadPoolExecutor(max_workers=1)
    thread.submit(update_progress)

    # 使用 uvicorn 启动 FastAPI 应用
    try:
        logging.info("MultiRAG HTTP server start...")
        uvicorn_logger = logging.getLogger("uvicorn.access")  # 获取uvicorn的访问日志记录器
        uvicorn.run("api.multirag_server:app", host=settings.HOST_IP, port=settings.HOST_PORT, log_level="info",
                    reload=RuntimeConfig.DEBUG)  # 启动 uvicorn 服务器
    except Exception:
        traceback.print_exc()
        os.kill(os.getpid(), signal.SIGKILL)  # 在异常情况下终止进程
