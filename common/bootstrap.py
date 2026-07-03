"""进程初始化统一入口（配置重构 Phase 3）。

所有入口点（multirag_server、api.apps、task_executor、sync_data_source、
admin_server、graphrag 工具）的初始化都收敛到 :func:`ensure_initialized`：

1. 加载并校验类型化配置（common.app_config，fail-fast）；
2. 创建有状态资源（common.resources：SECRET_KEY / doc store / storage / 检索器）。

幂等、线程安全。旧 ``settings.init_settings()`` 是本函数 force=True 的兼容别名
（上游移植的入口文件无需改动）。
"""

import threading

from common import resources
from common.app_config import get_app_config

_lock = threading.Lock()


def ensure_initialized(force: bool = False) -> None:
    """加载配置 + 初始化资源。可重复调用；force=True 强制重建资源。"""
    with _lock:
        get_app_config()  # 配置类型错误在此 fail-fast（AppConfigError 含字段路径）
        resources.init_resources(force=force)
