"""run_graphrag_for_kb 空 doc_ids 分支（全库文档发现）回归测试。

回归点：该分支曾写成 ``with db_connection as db:``（漏调用括号）。
db_connection 是 @contextmanager 装饰的函数，函数对象本身没有 __enter__，
doc_ids 为空走到该分支时直接 TypeError，全库 GraphRAG 任务无法启动。
打桩 DocumentService.get_by_kb_id 验证分支能取到文档列表并进入后续流程；
chunk_list 打桩为空使函数在 total_chunks==0 处早退，钉住 doc_ids 已被填充。
DB / doc store 全部打桩（纯函数打桩形态，同 test_checkpoint_resume.py）。
"""

from contextlib import contextmanager
from types import SimpleNamespace

import core.graphrag.general.index as kg_index


@contextmanager
def _fake_db():
    yield object()


async def test_empty_doc_ids_fetches_doc_list_from_db(monkeypatch):
    monkeypatch.setattr(kg_index, "db_connection", _fake_db)

    seen = {}

    def fake_get_by_kb_id(cls, db, **kwargs):
        seen.update(kwargs)
        return [{"id": "doc-1"}, {"id": "doc-2"}], 2

    monkeypatch.setattr(kg_index.DocumentService, "get_by_kb_id", classmethod(fake_get_by_kb_id))
    monkeypatch.setattr(kg_index.settings, "retriever", SimpleNamespace(chunk_list=lambda *a, **k: []), raising=False)

    row = {"tenant_id": "t1", "kb_id": "kb1", "id": "task-1"}
    res = await kg_index.run_graphrag_for_kb(row, [], "English", {}, None, None, lambda msg=None, prog=None: None)

    assert seen["kb_id"] == "kb1"
    assert res["total_docs"] == 2  # 回归点：DB 取到的文档列表进入了 doc_ids
    assert res["failed_docs"] == ["doc-1", "doc-2"]
    assert res["total_chunks"] == 0
