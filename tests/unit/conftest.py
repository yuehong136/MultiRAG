"""tests/unit 共享 fixtures（对齐 ragflow 用 conftest + fixture 的写法）。

被测对象是 service 层纯编排函数，DB 访问全部通过 monkeypatch 打桩，
因此这里的 `db` 只是一个未绑定引擎的 Session（仅满足 beartype 的类型校验）。
"""

import types
from typing import NoReturn

import pytest
from sqlalchemy.orm import Session


class _StubResource:
    """行为中性的资源假件：任何业务属性访问都显式失败。

    抛 NotImplementedError（而非 AttributeError）——hasattr 探测拦不住，
    "偷偷用了真资源"的测试必定现形，且报错信息直接给出修复指引。
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return f"<unit-test stub resource {self._name!r}>"

    def __getattr__(self, attr: str) -> NoReturn:
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)  # 不劫持 dunder 协议探测（copy/pickle/inspect）
        raise NotImplementedError(f"单元测试不得使用真实资源 {self._name}.{attr}：请在测试内显式打桩（monkeypatch / resources_state_guard 换桩 / app.dependency_overrides）。")


def pytest_configure(config: pytest.Config) -> None:
    """收集任何测试模块之前，向 common.resources 注册表预置轻量假件。

    十几个单测文件在 collection 阶段就 import api.apps，其模块级守卫
    （``if settings.SECRET_KEY is None: settings.init_settings()``）看到预置的
    secret_key 即跳过真实初始化——全套件因此零真实连接（Redis/Milvus/
    PostgreSQL/MinIO），封闭性由 CI unit job（无服务）永久守护。
    需要特定资源行为的测试配合 ``resources_state_guard`` 换自己的桩。
    注意必须用本钩子而非 session fixture：fixture 晚于 collection 期 import。
    """
    from common import resources

    if not resources.is_initialized():
        resources._state.update(
            secret_key="unit-test-secret-key",  # str：喂 SessionMiddleware / LoginManager
            doc_store=_StubResource("doc_store"),
            msg_store=_StubResource("msg_store"),
            storage=_StubResource("storage"),
            retriever=_StubResource("retriever"),
            kg_retriever=_StubResource("kg_retriever"),
        )


@pytest.fixture(autouse=True)
def _defrost_settings_facade():
    """清扫 settings 惰性名的"冻结遮蔽"。

    common.settings 是 PEP 562 惰性 facade。测试里 monkeypatch.setattr(settings, "X", ...)
    在 undo 时会把补丁时刻取到的值写回模块 dict，永久遮蔽同名惰性属性，
    污染后续测试（尤其 DOC_ENGINE 等随环境变量动态取值的名字）。
    本 fixture 在每个测试结束后删除所有遮蔽惰性名的模块属性，恢复惰性行为。
    （autouse 无参 fixture 先于测试请求的 monkeypatch 建立，因此清扫在
    monkeypatch undo 之后执行。）
    """
    yield
    from common import settings

    for name in list(vars(settings)):
        if name in settings._LAZY:
            delattr(settings, name)


@pytest.fixture
def resources_state_guard():
    """保存/恢复 common.resources 注册表。

    测试若调用 resources.reset_resources()（或 init），必须用本 fixture——
    套件中 api.apps 的导入会初始化会话级真实资源，清空后不恢复会饿死
    后续依赖已初始化资源的测试。
    """
    from common import resources

    saved = dict(resources._state)
    yield
    resources._state.clear()
    resources._state.update(saved)


@pytest.fixture(autouse=True)
def _restore_dependency_overrides():
    """自动回滚契约测试对 app.dependency_overrides 的 per-test 追加覆盖。

    只在 api.apps 已被导入时快照/恢复（不主动触发 46s 的应用导入），
    保证测试内 ``client.app.dependency_overrides[dep] = ...`` 不泄漏到后续测试。
    """
    import sys

    api_apps = sys.modules.get("api.apps")
    if api_apps is None:
        yield
        return
    saved = dict(api_apps.app.dependency_overrides)
    yield
    api_apps.app.dependency_overrides.clear()
    api_apps.app.dependency_overrides.update(saved)


@pytest.fixture(scope="session")
def client_user():
    """契约测试的默认登录用户（已激活、非超管）。"""
    return types.SimpleNamespace(
        id="user-unit",
        email="unit@test.local",
        nickname="Unit Tester",
        avatar=None,
        is_active=True,
        is_superuser=False,
    )


@pytest.fixture(scope="session")
def client(client_user):
    """真实 ``api.apps.app`` 上的 TestClient——路由契约测试的统一入口。

    - session 级缓存：api.apps 冷导入约 46s/559 路由，整个会话只付一次；
      导入惰性化在 fixture 体内，只跑纯逻辑测试文件时零成本；
    - 基线 dependency_overrides：``get_db``/``get_async_db`` → 未绑定
      Session/AsyncSession（不连库）、登录 ``manager`` → ``client_user``、
      ``async_current_user`` → 同字段 Principal、
      ``current_tenant_id``/``async_current_tenant_id`` → "tenant-unit"；
      api/apps/deps.py 的资源依赖无需覆盖——注册表里已是预置 stub，
      未显式打桩就使用会以 NotImplementedError 现形（个别测试按需
      ``client.app.dependency_overrides[deps.get_storage] = ...`` 覆盖）；
    - 不进 lifespan 上下文（不 ``with``）：避开进度轮询线程与 workflow
      状态管理器的启动，路由栈不依赖它们；
    - per-test 追加覆盖由 ``_restore_dependency_overrides`` 自动回滚。
    """
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import AsyncSession

    import api.apps as api_apps
    from api.db.db_models import get_async_db, get_db
    from api.utils import api_utils

    app = api_apps.app
    baseline = {
        get_db: lambda: Session(),
        get_async_db: lambda: AsyncSession(),
        api_apps.manager: lambda: client_user,
        api_utils.current_tenant_id: lambda: "tenant-unit",
        api_utils.async_current_user: lambda: api_utils.Principal(id=client_user.id, email=client_user.email, nickname=client_user.nickname),
        api_utils.async_current_tenant_id: lambda: "tenant-unit",
    }
    app.dependency_overrides.update(baseline)
    yield TestClient(app)
    for dep in baseline:
        app.dependency_overrides.pop(dep, None)


@pytest.fixture
def route_dependency_calls():
    """展开真实 app 某路由的完整 Depends 树，返回全部依赖 callable（纯异步换轨测试用）。

    ``Depends(manager)`` 的 LoginManager 实例同样以 ``dep.call`` 形态出现在树里，
    可直接用 ``manager not in calls`` 断言同步鉴权轨已清除。
    """
    from fastapi.routing import APIRoute

    def _calls(app, method: str, path: str) -> set:
        for route in app.routes:
            if isinstance(route, APIRoute) and route.path == path and method in route.methods:
                calls = set()
                stack = [route.dependant]
                while stack:
                    dep = stack.pop()
                    if dep.call is not None:
                        calls.add(dep.call)
                    stack.extend(dep.dependencies)
                return calls
        raise AssertionError(f"route not found: {method} {path}")

    return _calls


@pytest.fixture
def db():
    """未绑定引擎的 SQLAlchemy Session：满足 beartype 的 `db: Session` 校验，不会真正连库。"""
    return Session()


@pytest.fixture
def async_db():
    """未绑定引擎的 AsyncSession：满足 beartype 的 `db: AsyncSession` 校验，不会真正连库。"""
    from sqlalchemy.ext.asyncio import AsyncSession

    return AsyncSession()


@pytest.fixture
def fake_kb():
    """知识库对象工厂，按需覆盖字段；`to_dict` 可单独指定。"""

    def _make(**kw):
        defaults = {
            "id": "kb1",
            "name": "ds",
            "parser_config": {},
            "parser_id": "naive",
            "embd_id": "bge@builtin",
            "chunk_num": 0,
            "pagerank": 0,
            "tenant_id": "t1",
            "pipeline_id": None,
            "graphrag_task_id": None,
            "raptor_task_id": None,
        }
        to_dict = kw.pop("to_dict", None)
        defaults.update(kw)
        if to_dict is None:
            to_dict = {"id": defaults["id"], "name": defaults["name"]}
        obj = types.SimpleNamespace(**defaults)
        obj.to_dict = lambda _d=to_dict: dict(_d)
        return obj

    return _make
