"""有状态资源的显式生命周期（配置重构 Phase 3，方案见 internal/config_bootstrap_refactor_plan.md）。

管理进程级资源：SECRET_KEY（Redis 取/建）、doc store / msg store 连接、
对象存储实现、检索器。与 common/app_config（纯数据）相对。

- 入口点通过 :func:`common.bootstrap.ensure_initialized` 初始化（幂等）；
- **仅 import 当前选中后端的模块**——旧 settings.py 头部无条件导入全部
  6 种向量库 + 4 种 memory 后端 + 7 种存储实现（import 一次 8 秒）在此终结；
- 未初始化就访问核心资源 → 抛 :class:`ResourcesNotInitialized`（含操作指引），
  取代旧的"默默返回 None、等下游炸"的失败模式；
- 引擎分支的 if/elif 结构刻意镜像 ragflow 上游 init_settings，
  上游给某引擎打补丁时 diff 可视觉对齐（internal/ragflow_settings_porting_map.md 第三节）。
"""

import importlib
import logging
import os
import secrets
import threading
from typing import Any

from common.constants import Storage

_lock = threading.RLock()
_state: dict[str, Any] = {}


class ResourcesNotInitialized(RuntimeError):
    """资源未初始化即被访问。入口点需先调用 common.bootstrap.ensure_initialized()。"""


def _doc_engine() -> str:
    return os.environ.get("DOC_ENGINE", "milvus").strip().lower()


def _storage_type() -> str:
    return os.getenv("STORAGE_IMPL", "MINIO")


class StorageFactory:
    """存储实现工厂（懒加载：值为 'module:attr'，create 时才 import）。"""

    storage_mapping: dict[Storage, str] = {
        Storage.MINIO: "core.utils.minio_conn:MultiRAGMinio",
        Storage.AZURE_SPN: "core.utils.azure_spn_conn:MultiRAGAzureSpnBlob",
        Storage.AZURE_SAS: "core.utils.azure_sas_conn:MultiRAGAzureSasBlob",
        Storage.AWS_S3: "core.utils.s3_conn:MultiRAGS3",
        Storage.OSS: "core.utils.oss_conn:MultiRAGOSS",
        Storage.OPENDAL: "core.utils.opendal_conn:OpenDALStorage",
        Storage.GCS: "core.utils.gcs_conn:MultiRAGGCS",
    }

    @classmethod
    def create(cls, storage: Storage):
        module_name, _, attr = cls.storage_mapping[storage].partition(":")
        module = importlib.import_module(module_name)
        return getattr(module, attr)()


def _create_secret_key() -> str:
    """镜像旧 settings._get_or_create_secret_key：Redis get_or_create，自动生成并告警。"""
    from core.utils.redis_conn import REDIS_CONN

    generated_key = secrets.token_hex(32)
    secret_key = REDIS_CONN.get_or_create_secret_key("multirag:system:secret_key", generated_key)
    logging.warning("SECURITY WARNING: Using auto-generated SECRET_KEY.")
    return secret_key


def _create_doc_store(engine: str) -> Any:
    # 分支结构镜像 ragflow 上游 init_settings —— 勿改写为映射表
    if engine == "elasticsearch":
        import core.utils.es_conn

        return core.utils.es_conn.ESConnection()
    elif engine == "milvus":
        import core.utils.milvus_conn

        return core.utils.milvus_conn.MilvusConnection()
    elif engine == "infinity":
        import core.utils.infinity_conn

        return core.utils.infinity_conn.InfinityConnection()
    elif engine == "opensearch":
        import core.utils.opensearch_conn

        return core.utils.opensearch_conn.OSConnection()
    elif engine == "oceanbase":
        import core.utils.ob_conn

        return core.utils.ob_conn.OBConnection()
    elif engine == "seekdb":
        import core.utils.ob_conn

        return core.utils.ob_conn.OBConnection()
    elif engine == "vastbase":
        import core.utils.vastbase_conn

        return core.utils.vastbase_conn.VastBaseConnection()
    raise Exception(f"Not supported doc engine: {engine}")


def _create_msg_store(engine: str) -> Any:
    # use the same engine for message store；opensearch/vastbase 无 msg 分支（历史行为）→ None
    if engine == "elasticsearch":
        import memory.utils.es_conn

        return memory.utils.es_conn.ESConnection()
    elif engine == "milvus":
        import memory.utils.milvus_conn

        return memory.utils.milvus_conn.MilvusConnection()
    elif engine == "infinity":
        import memory.utils.infinity_conn

        return memory.utils.infinity_conn.InfinityConnection()
    elif engine in ["oceanbase", "seekdb"]:
        import memory.utils.ob_conn

        return memory.utils.ob_conn.OBConnection()
    return None


def _create_storage() -> Any:
    storage_impl = StorageFactory.create(Storage[_storage_type()])

    crypto_enabled = os.environ.get("MultiRAG_CRYPTO_ENABLED", "false").lower() == "true"
    if crypto_enabled:
        try:
            from core.utils.encrypted_storage import create_encrypted_storage

            algorithm = os.environ.get("MultiRAG_CRYPTO_ALGORITHM", "aes-256-cbc")
            crypto_key = os.environ.get("MultiRAG_CRYPTO_KEY")
            return create_encrypted_storage(storage_impl, algorithm=algorithm, key=crypto_key, encryption_enabled=crypto_enabled)
        except Exception as e:
            logging.error(f"Failed to initialize encrypted storage: {e}")
    return storage_impl


def init_resources(force: bool = False) -> None:
    """创建全部进程级资源。幂等；force=True 强制重建（旧 init_settings 语义）。"""
    with _lock:
        if _state and not force:
            return

        secret_key = _create_secret_key()

        engine = _doc_engine()
        doc_store = _create_doc_store(engine)
        msg_store = _create_msg_store(engine)
        storage = _create_storage()

        from core.graphrag import search as kg_search
        from core.nlp import search

        retriever = search.Dealer(doc_store)
        kg_retriever = kg_search.KGSearch(doc_store)

        _state.update(
            secret_key=secret_key,
            doc_store=doc_store,
            msg_store=msg_store,
            storage=storage,
            retriever=retriever,
            kg_retriever=kg_retriever,
        )
        os.environ["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"


def reset_resources() -> None:
    """清空资源注册表（仅测试使用）。"""
    with _lock:
        _state.clear()


def is_initialized() -> bool:
    return bool(_state)


def _require(name: str) -> Any:
    try:
        return _state[name]
    except KeyError:
        raise ResourcesNotInitialized(f"资源 {name!r} 尚未初始化：入口点需先调用 common.bootstrap.ensure_initialized()（等价旧 settings.init_settings()；说明见 AGENTS.md）") from None


def doc_store() -> Any:
    return _require("doc_store")


def msg_store() -> Any:
    """注意：opensearch/vastbase 引擎下合法为 None（历史行为），因此不做未初始化 fail-fast。"""
    return _state.get("msg_store")


def storage() -> Any:
    return _require("storage")


def retriever() -> Any:
    return _require("retriever")


def kg_retriever() -> Any:
    return _require("kg_retriever")


def secret_key() -> Any:
    """未初始化返回 None——api/apps 的模块级守卫（if SECRET_KEY is None: init）依赖此语义。"""
    return _state.get("secret_key")
