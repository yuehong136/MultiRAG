"""FastAPI 资源依赖（配置重构 Phase 4，方案见 internal/config_bootstrap_refactor_plan.md）。

新代码规范（AGENTS.md「配置与资源」）：
- 路由层通过 ``Depends`` 注入资源，不直接引用 ``settings.docStoreConn`` /
  ``settings.STORAGE_IMPL`` 等全局——测试用 ``app.dependency_overrides``
  即可替换，无需 monkeypatch 模块属性；
- **紧跟 ragflow 上游的文件例外**：保持 ``settings.X`` 风格以保证上游 diff
  可照抄（internal/ragflow_settings_porting_map.md 第四节）。

示例：
    from api.apps.deps import get_storage

    @router.get("/download/{file_id}")
    def download(file_id: str, storage: Any = Depends(get_storage)):
        blob = storage.get(bucket, name)
"""

from typing import Any

from common import resources


def get_doc_store() -> Any:
    """向量/全文检索存储连接（settings.docStoreConn 的注入版）。"""
    return resources.doc_store()


def get_storage() -> Any:
    """对象存储实现（settings.STORAGE_IMPL 的注入版）。"""
    return resources.storage()


def get_retriever() -> Any:
    """RAG 检索器（settings.retriever 的注入版）。"""
    return resources.retriever()


def get_kg_retriever() -> Any:
    """知识图谱检索器（settings.kg_retriever 的注入版）。"""
    return resources.kg_retriever()
