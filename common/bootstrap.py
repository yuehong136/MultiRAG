"""进程初始化统一入口（配置重构 Phase 3）。

所有入口点（multirag_server、api.apps、task_executor、sync_data_source、
admin_server、graphrag 工具）的初始化都收敛到 :func:`ensure_initialized`：

1. 加载并校验类型化配置（common.app_config，fail-fast）；
2. OTel 自动埋点（common.observability，配置开关，默认关闭）——必须先于
   api.db.db_models / api.apps 的导入执行，全局 patch 才能覆盖 engine 与 app；
3. 创建有状态资源（common.resources：SECRET_KEY / doc store / storage / 检索器）。

幂等、线程安全。旧 ``settings.init_settings()`` 是本函数 force=True 的兼容别名
（上游移植的入口文件无需改动）。
"""

import threading

from common import observability, resources
from common.app_config import get_app_config

_lock = threading.Lock()


def ensure_initialized(force: bool = False) -> None:
    """加载配置 + OTel 埋点 + 初始化资源。可重复调用；force=True 强制重建资源。"""
    with _lock:
        get_app_config()  # 配置类型错误在此 fail-fast（AppConfigError 含字段路径）
        observability.init_otel()  # 进程唯一初始化点（自身幂等，默认关闭）
        resources.init_resources(force=force)
