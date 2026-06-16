import time
start_ts = time.time()

from common.log_utils import init_root_logger
from agent.plugin import GlobalPluginManager
init_root_logger("multirag_server")

import logging
import os
import signal
import sys
import traceback
import threading
import uuid
import faulthandler

from api.apps import app
from api.db.runtime_config import RuntimeConfig
from api.db.services.document_service import DocumentService
from common import settings
from common.file_utils import get_project_base_directory

from api.db.db_models import init_database_tables as init_web_db, upgrade_database_tables as upgrade_database, SessionLocal, db_connection, engine
from api.db.init_data import init_web_data, init_superuser
from common.versions import get_multirag_version
import uvicorn
# from common.config_utils import show_configs
from common.mcp_tool_call_conn import shutdown_all_mcp_sessions
from core.utils.redis_conn import RedisDistributedLock

print("Start MultiRAG server...")

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
            # ⚠️ 关键修复：先关闭数据库连接，再等待
            # 这样连接可以立即归还到连接池，避免占用期间阻塞其他请求
            if db:
                try:
                    db.close()
                except Exception:
                    pass
            try:
                redis_lock.release()
            except Exception:
                pass
            # 等待下一次循环（此时连接已归还）
            stop_event.wait(6)

def signal_handler(sig, frame):
    logging.info("Received interrupt signal, shutting down...")
    shutdown_all_mcp_sessions()
    stop_event.set()
    stop_event.wait(1)  # 最多等待1秒，stop_event已设置则立即返回
    sys.exit(0)

if __name__ == '__main__':
    faulthandler.enable()
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
#                         ║║║ │ │ │   │  │ ├┬┘ ├─┤ ║ ╦    【——v0.9.9——】
#                         ╩ ╩ └─┘ ┴─┘ ┴  ┴ ┴└─ ┴ ┴ ╚═╝
# ============================================================================
#                 """)
    logging.info(r"""
============================================================================   
                __  ___      ____  _ ____  ___   ______
               /  |/  /_  __/ / /_(_) __ \/   | / ____/
              / /|_/ / / / / / __/ / /_/ / /| |/ / __   v0.9.9
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
    parser.add_argument('--init-superuser', default=False, help="init superuser", action='store_true')
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
    # 建表前检测是否为全新环境（无任何表），用于后续迁移策略判断
    from sqlalchemy import inspect as sa_inspect
    _inspector = sa_inspect(engine)
    _is_fresh_install = len(_inspector.get_table_names(schema="usr_ai")) == 0
    init_web_db()                              # 创建数据库表结构
    upgrade_database(_is_fresh_install)        # 执行数据库迁移
    
    # 初始化超级用户（如果指定了 --init-superuser 参数）
    # 必须在数据库表创建后、init_web_data 之前执行
    if args.init_superuser:
        logging.info("Initializing superuser...")
        with db_connection() as db:
            init_superuser(db)
        logging.info("Superuser initialization completed")
    
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
        logging.info(f"MultiRAG server is ready after {time.time() - start_ts}s initialization.")
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
        stop_event.wait(1)  # 最多等待1秒，stop_event已设置则立即返回
        os.kill(os.getpid(), signal.SIGKILL)
