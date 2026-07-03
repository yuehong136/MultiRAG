"""tests/unit 共享 fixtures（对齐 ragflow 用 conftest + fixture 的写法）。

被测对象是 service 层纯编排函数，DB 访问全部通过 monkeypatch 打桩，
因此这里的 `db` 只是一个未绑定引擎的 Session（仅满足 beartype 的类型校验）。
"""

import types

import pytest
from sqlalchemy.orm import Session


@pytest.fixture
def db():
    """未绑定引擎的 SQLAlchemy Session：满足 beartype 的 `db: Session` 校验，不会真正连库。"""
    return Session()


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
