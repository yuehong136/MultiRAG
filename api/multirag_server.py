# coding=utf-8
"""
@project: multirag
@Author：龙
@file： multirag_server.py
@date：2024/7/30 18:00
@desc:
"""
import logging
import os
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from api.apps import app
from api.db.database import SessionLocal
from api.db.runtime_config import RuntimeConfig
from api.db.services.document_service import DocumentService
from api.settings import (
    HOST, HTTP_PORT, access_logger, database_logger, stat_logger,
)
from api import utils

from api.db.db_models import init_database_tables as init_web_db
from api.db.init_data import init_web_data
from api.versions import get_versions
import uvicorn


def update_progress():
    """
    定期更新文档服务进度
    """
    while True:
        time.sleep(1)
        db = None
        try:
            db = SessionLocal()  # 创建数据库会话
            DocumentService.update_progress(db)  # 更新文档服务进度
        except Exception as e:
            stat_logger.error("update_progress exception:" + str(e))  # 记录异常
        finally:
            if db:
                db.close()

if __name__ == '__main__':
    # 打印启动信息
#     print(r"""
#     __  ___      ____  _    ____
#    /  |/  /_  __/ / /_(_)  / __ \____ _____ _
#   / /|_/ / / / / / __/ /  / /_/ / __ `/ __ `/
#  / /  / / /_/ / / /_/ /  / _, _/ /_/ / /_/ /
# /_/  /_/\__,_/_/\__/_/  /_/ |_|\__,_/\__, /
#                                     /____/
#
#     """, flush=True)
    print(r"""
    __  ___            __   __     _             ____                   
   /  |/  /  __  __   / /  / /_   (_)           / __ \   ____ _   ____ _
  / /|_/ /  / / / /  / /  / __/  / /  ______   / /_/ /  / __ `/  / __ `/
 / /  / /  / /_/ /  / /  / /_   / /  /_____/  / _, _/  / /_/ /  / /_/ / 
/_/  /_/   \__,_/  /_/   \__/  /_/           /_/ |_|   \__,_/   \__, /  
                                                               /____/   

        """, flush=True)
    stat_logger.info(
        f'project base: {utils.file_utils.get_project_base_directory()}'
    )

    # 初始化数据库
    # init_web_db()
    # init_web_data()

    # 初始化运行时配置
    import argparse

    # 解析命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', default=False, help="multi rag version", action='store_true')
    parser.add_argument('--debug', default=False, help="debug mode", action='store_true')
    args = parser.parse_args()
    if args.version:
        print(get_versions())
        sys.exit(0)

    RuntimeConfig.DEBUG = args.debug  # 设置调试模式
    if RuntimeConfig.DEBUG:
        stat_logger.info("run on debug mode")

    RuntimeConfig.init_env()  # 初始化环境变量
    RuntimeConfig.init_config(JOB_SERVER_HOST=HOST, HTTP_PORT=HTTP_PORT)  # 初始化配置

    sqlalchemy_logger = logging.getLogger('sqlalchemy')  # 获取SQLAlchemy日志记录器
    sqlalchemy_logger.propagate = False
    sqlalchemy_logger.addHandler(database_logger.handlers[0])  # 添加数据库日志处理程序
    sqlalchemy_logger.setLevel(database_logger.level)  # 设置日志级别

    # 启动进度更新线程
    thr = ThreadPoolExecutor(max_workers=1)
    thr.submit(update_progress)

    # 使用 uvicorn 启动 FastAPI 应用
    try:
        stat_logger.info("Multi RAG http server start...")
        uvicorn_logger = logging.getLogger("uvicorn.access")  # 获取uvicorn的访问日志记录器
        for h in access_logger.handlers:
            uvicorn_logger.addHandler(h)  # 将access_logger的处理程序添加到uvicorn的访问日志记录器中
        uvicorn_logger.setLevel(access_logger.level)  # 设置日志级别
        uvicorn.run(app, host=HOST, port=HTTP_PORT, log_level="info", reload=RuntimeConfig.DEBUG)  # 启动 uvicorn 服务器
    except Exception:
        traceback.print_exc()
        os.kill(os.getpid(), signal.SIGKILL)  # 在异常情况下终止进程
