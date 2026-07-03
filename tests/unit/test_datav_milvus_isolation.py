import importlib.util
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@contextmanager
def patched_modules(module_map: dict[str, types.ModuleType]):
    """Temporarily inject stub modules for isolated imports."""
    original = {name: sys.modules.get(name) for name in module_map}
    try:
        sys.modules.update(module_map)
        yield
    finally:
        for name, prev in original.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


def load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class FakeConnections:
    def __init__(self):
        self.connected_aliases: set[str] = set()
        self.connect_calls: list[dict] = []
        self.disconnect_calls: list[str] = []

    def connect(self, **kwargs):
        alias = kwargs.get("alias")
        if not alias:
            raise ValueError("alias is required")
        self.connected_aliases.add(alias)
        self.connect_calls.append(kwargs)

    def disconnect(self, alias: str):
        self.connected_aliases.discard(alias)
        self.disconnect_calls.append(alias)


def test_milvus_connection_pool_isolation_and_cache():
    fake_connections = FakeConnections()

    utility_module = types.ModuleType("pymilvus.orm.utility")

    def get_server_version(using: str):
        if using not in fake_connections.connected_aliases:
            raise RuntimeError(f"alias not connected: {using}")
        return "2.5.11"

    utility_module.get_server_version = get_server_version
    utility_module.get_server_type = lambda using: "milvus"

    connections_module = types.ModuleType("pymilvus.orm.connections")
    connections_module.connections = fake_connections

    pymilvus_orm_module = types.ModuleType("pymilvus.orm")
    pymilvus_orm_module.utility = utility_module

    pymilvus_module = types.ModuleType("pymilvus")
    pymilvus_module.orm = pymilvus_orm_module

    common_settings_module = types.ModuleType("common.settings")
    common_settings_module.MILVUS = {
        "hosts": "http://fake-milvus:19530",
        "username": "root",
        "password": "Milvus",
        "db_name": "default",
        "token": "",
        "timeout": None,
        "kwargs": {},
    }
    common_settings_module.get_base_config = lambda key, default=None: default

    common_decorator_module = types.ModuleType("common.decorator")
    common_decorator_module.singleton = lambda cls, *args, **kwargs: cls
    common_module = types.ModuleType("common")
    common_module.settings = common_settings_module

    with patched_modules(
        {
            "pymilvus": pymilvus_module,
            "pymilvus.orm": pymilvus_orm_module,
            "pymilvus.orm.utility": utility_module,
            "pymilvus.orm.connections": connections_module,
            "common.settings": common_settings_module,
            "common.decorator": common_decorator_module,
            "common": common_module,
        }
    ):
        module = load_module(
            "test_milvus_conn_pool_module",
            ROOT_DIR / "common" / "doc_store" / "milvus_conn_pool.py",
        )

    pool = module.MILVUS_CONN
    default_alias = pool.get_conn()
    assert default_alias
    assert default_alias in fake_connections.connected_aliases

    connect_calls_before_db = len(fake_connections.connect_calls)
    db_a_alias_1 = pool.get_conn_for_db("db_a")
    db_a_alias_2 = pool.get_conn_for_db("db_a")
    db_b_alias = pool.get_conn_for_db("db_b")

    assert db_a_alias_1 == db_a_alias_2
    assert db_a_alias_1 != db_b_alias
    assert pool.get_conn() == default_alias
    assert len(fake_connections.connect_calls) == connect_calls_before_db + 2


def _build_datav_app_module():
    api_apps_module = types.ModuleType("api.apps")
    api_apps_module.manager = lambda: None

    api_db_models_module = types.ModuleType("api.db.db_models")
    api_db_models_module.get_db = lambda: None

    api_utils_api_utils_module = types.ModuleType("api.utils.api_utils")
    api_utils_api_utils_module.get_data_error_result = lambda retmsg="": {"retcode": 1, "retmsg": retmsg}
    api_utils_api_utils_module.get_json_result = lambda data=None: {"retcode": 0, "data": data}
    api_utils_api_utils_module.server_error_response = lambda e: {"retcode": 500, "retmsg": str(e)}

    common_settings_module = types.ModuleType("common.settings")
    common_settings_module.EMBEDDING_MDL = "default-embed"
    common_module = types.ModuleType("common")
    common_module.settings = common_settings_module

    core_llm_module = types.ModuleType("core.llm")
    core_llm_module.EmbeddingModel = object

    with patched_modules(
        {
            "api.apps": api_apps_module,
            "api.db.db_models": api_db_models_module,
            "api.utils.api_utils": api_utils_api_utils_module,
            "common.settings": common_settings_module,
            "common": common_module,
            "core.llm": core_llm_module,
        }
    ):
        return load_module(
            "test_datav_app_module",
            ROOT_DIR / "api" / "apps" / "datav_app.py",
        )


def test_datav_execute_vector_search_routes_to_database_alias():
    class FakePool:
        def get_conn_for_db(self, db_name: str | None):
            normalized = (db_name or "").strip()
            return "default_alias" if not normalized else f"alias_{normalized}"

    class FakeConn:
        def __init__(self):
            self.calls = []

        def search_by_milvus(self, **kwargs):
            self.calls.append(kwargs)
            return [{"id": "doc-1", "distance": 0.9}]

    module = _build_datav_app_module()
    milvus_pool_module = types.ModuleType("common.doc_store.milvus_conn_pool")
    milvus_pool_module.MILVUS_CONN = FakePool()
    conn = FakeConn()
    search_cfg = module.SearchConfig()

    original_pool_module = sys.modules.get("common.doc_store.milvus_conn_pool")
    sys.modules["common.doc_store.milvus_conn_pool"] = milvus_pool_module
    try:
        module.execute_vector_search(
            conn=conn,
            db_type=module.SupportedVectorDBType.MILVUS,
            collection_name="c1",
            query_vector=[0.1, 0.2],
            vector_field="q_2_vec",
            output_fields=["content"],
            filter_expr="",
            search_config=search_cfg,
            database="db_a",
        )
        module.execute_vector_search(
            conn=conn,
            db_type=module.SupportedVectorDBType.MILVUS,
            collection_name="c1",
            query_vector=[0.1, 0.2],
            vector_field="q_2_vec",
            output_fields=["content"],
            filter_expr="",
            search_config=search_cfg,
            database="db_b",
        )
        module.execute_vector_search(
            conn=conn,
            db_type=module.SupportedVectorDBType.MILVUS,
            collection_name="c1",
            query_vector=[0.1, 0.2],
            vector_field="q_2_vec",
            output_fields=["content"],
            filter_expr="",
            search_config=search_cfg,
            database=None,
        )
    finally:
        if original_pool_module is None:
            sys.modules.pop("common.doc_store.milvus_conn_pool", None)
        else:
            sys.modules["common.doc_store.milvus_conn_pool"] = original_pool_module

    assert conn.calls[0]["using"] == "alias_db_a"
    assert conn.calls[1]["using"] == "alias_db_b"
    assert conn.calls[2]["using"] == "default_alias"


def test_datav_vector_search_returns_business_error_on_database_connection_failure():
    class FailingPool:
        def get_conn_for_db(self, db_name: str | None):
            raise RuntimeError("db unavailable")

    class FakeEmbeddingModel:
        def encode_queries(self, _: str):
            return [0.1, 0.2], 2

    class FakeConn:
        def search_by_milvus(self, **kwargs):  # pragma: no cover
            return []

    module = _build_datav_app_module()
    milvus_pool_module = types.ModuleType("common.doc_store.milvus_conn_pool")
    milvus_pool_module.MILVUS_CONN = FailingPool()
    module.get_embedding_model = lambda db, tenant_id, config: FakeEmbeddingModel()
    module.VectorDBFactory.get_connection = staticmethod(lambda db_type, database=None: FakeConn())

    request = module.VectorSearchRequest(
        db_type=module.SupportedVectorDBType.MILVUS,
        collection_name="c1",
        query_text="hello",
        vector_field="q_2_vec",
        output_fields=["content"],
        database="db_x",
    )
    original_pool_module = sys.modules.get("common.doc_store.milvus_conn_pool")
    sys.modules["common.doc_store.milvus_conn_pool"] = milvus_pool_module
    try:
        result = module.vector_search(request=request, db=None, user=SimpleNamespace(id="user_1"))
    finally:
        if original_pool_module is None:
            sys.modules.pop("common.doc_store.milvus_conn_pool", None)
        else:
            sys.modules["common.doc_store.milvus_conn_pool"] = original_pool_module

    assert result["retcode"] != 0
    assert "db_x" in result["retmsg"]
