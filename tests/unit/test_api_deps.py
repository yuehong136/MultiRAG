"""api/apps/deps.py 资源依赖测试（配置重构 Phase 4）。

演示并锁定标准模式：路由用 Depends 注入资源后，测试通过
``app.dependency_overrides`` 替换实现——无需 monkeypatch 模块属性。
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.apps import deps
from common import resources


class _FakeStorage:
    def get(self, bucket, name):
        return f"blob:{bucket}/{name}".encode()


def test_deps_delegate_to_resources_registry(resources_state_guard):
    resources.reset_resources()
    # 未初始化：核心资源依赖同样 fail-fast
    with pytest.raises(resources.ResourcesNotInitialized):
        deps.get_storage()
    with pytest.raises(resources.ResourcesNotInitialized):
        deps.get_doc_store()


def test_dependency_overrides_replace_storage_without_monkeypatch():
    """标准测试写法：dependency_overrides 注入假资源。"""
    app = FastAPI()

    @app.get("/probe/{name}")
    def probe(name: str, storage=Depends(deps.get_storage)):
        return {"blob": storage.get("bucket-1", name).decode()}

    app.dependency_overrides[deps.get_storage] = lambda: _FakeStorage()

    client = TestClient(app)
    resp = client.get("/probe/hello.txt")

    assert resp.status_code == 200
    assert resp.json() == {"blob": "blob:bucket-1/hello.txt"}
