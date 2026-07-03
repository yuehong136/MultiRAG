"""行为钉板：init_settings() 的 DOC_ENGINE → 后端实现映射。

配置重构 Phase 3 会把这段 if/elif 迁入 common/resources.py（分支结构镜像保留，
便于对照 ragflow 上游 diff），映射关系必须逐条保持：

  doc store:  elasticsearch→ESConnection  milvus→MilvusConnection
              infinity→InfinityConnection opensearch→OSConnection
              oceanbase/seekdb→OBConnection vastbase→VastBaseConnection
              其他→抛异常
  msg store:  elasticsearch/milvus/infinity→memory 同名实现
              oceanbase/seekdb→memory OBConnection
              vastbase→**不设置**（保持原值，现状如此）
"""

import pytest

import core.graphrag.search
import core.utils.es_conn
import core.utils.infinity_conn
import core.utils.milvus_conn
import core.utils.ob_conn
import core.utils.opensearch_conn
import core.utils.vastbase_conn
import memory.utils.es_conn
import memory.utils.infinity_conn
import memory.utils.milvus_conn
import memory.utils.ob_conn
from common import settings


class _Fake:
    """占位后端：记录自己的角色名，构造时零副作用。"""

    def __init__(self, role):
        self.role = role

    def __call__(self):  # 以类身份被调用时返回自身实例
        return self


@pytest.fixture
def settings_state_guard():
    """init_settings 会改写大量模块全局，测试后完整还原。"""
    before = dict(vars(settings))
    yield
    current = vars(settings)
    for key in set(current) - set(before):
        delattr(settings, key)
    for key, value in before.items():
        setattr(settings, key, value)


@pytest.fixture
def mocked_backends(monkeypatch, settings_state_guard):
    """把全部有连接副作用的构造点替换为占位对象。"""
    fakes = {}

    def stub(module, attr, role):
        fake = _Fake(role)
        monkeypatch.setattr(module, attr, fake)
        fakes[role] = fake

    stub(core.utils.es_conn, "ESConnection", "doc:es")
    stub(core.utils.milvus_conn, "MilvusConnection", "doc:milvus")
    stub(core.utils.infinity_conn, "InfinityConnection", "doc:infinity")
    stub(core.utils.opensearch_conn, "OSConnection", "doc:opensearch")
    stub(core.utils.ob_conn, "OBConnection", "doc:ob")
    stub(core.utils.vastbase_conn, "VastBaseConnection", "doc:vastbase")
    stub(memory.utils.es_conn, "ESConnection", "msg:es")
    stub(memory.utils.milvus_conn, "MilvusConnection", "msg:milvus")
    stub(memory.utils.infinity_conn, "InfinityConnection", "msg:infinity")
    stub(memory.utils.ob_conn, "OBConnection", "msg:ob")

    # 资源侧其余构造点
    monkeypatch.setattr(settings.StorageFactory, "create", classmethod(lambda cls, storage: _Fake(f"storage:{storage}")))
    monkeypatch.setattr(settings.search, "Dealer", lambda conn: _Fake("retriever"))
    monkeypatch.setattr(core.graphrag.search, "KGSearch", lambda conn: _Fake("kg_retriever"))
    monkeypatch.setattr(settings.REDIS_CONN, "get_or_create_secret_key", lambda key, generated: "pinned-secret", raising=False)

    return fakes


DOC_ENGINE_EXPECTATIONS = [
    ("elasticsearch", "doc:es", "msg:es"),
    ("milvus", "doc:milvus", "msg:milvus"),
    ("infinity", "doc:infinity", "msg:infinity"),
    ("opensearch", "doc:opensearch", None),  # msgStore 无 opensearch 分支：保持原值
    ("oceanbase", "doc:ob", "msg:ob"),
    ("seekdb", "doc:ob", "msg:ob"),
    ("vastbase", "doc:vastbase", None),  # msgStore 无 vastbase 分支：保持原值
]


@pytest.mark.parametrize("engine,doc_role,msg_role", DOC_ENGINE_EXPECTATIONS)
def test_doc_engine_backend_mapping(engine, doc_role, msg_role, mocked_backends, monkeypatch):
    monkeypatch.setenv("DOC_ENGINE", engine)
    sentinel = object()
    settings.msgStoreConn = sentinel  # 用哨兵验证"无分支则不改写"

    settings.init_settings()

    assert settings.docStoreConn.role == doc_role
    if msg_role is None:
        assert settings.msgStoreConn is sentinel
    else:
        assert settings.msgStoreConn.role == msg_role
    # retriever/kg_retriever 基于选中的 doc store 构建
    assert settings.retriever.role == "retriever"
    assert settings.kg_retriever.role == "kg_retriever"
    # SECRET_KEY 来自 Redis get_or_create（yaml 中的 secret_key 不生效）
    assert settings.SECRET_KEY == "pinned-secret"


def test_unknown_doc_engine_raises(mocked_backends, monkeypatch):
    monkeypatch.setenv("DOC_ENGINE", "no-such-engine")

    with pytest.raises(Exception, match="Not supported doc engine"):
        settings.init_settings()


def test_storage_factory_mapping_table():
    """存储类型 → 实现类映射表（纯数据，直接钉住键集合）。"""
    from common.constants import Storage

    assert set(settings.StorageFactory.storage_mapping) == {
        Storage.MINIO,
        Storage.AZURE_SPN,
        Storage.AZURE_SAS,
        Storage.AWS_S3,
        Storage.OSS,
        Storage.OPENDAL,
        Storage.GCS,
    }
