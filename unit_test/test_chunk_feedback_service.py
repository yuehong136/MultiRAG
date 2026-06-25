from types import SimpleNamespace
from unittest.mock import MagicMock

from api.db.services import chunk_feedback_service as service
from common.constants import PAGERANK_FLD
from core.utils import milvus_conn as milvus_module


def _wrapped_singleton_class(factory):
    for cell in factory.__closure__ or ():
        if isinstance(cell.cell_contents, type):
            return cell.cell_contents
    raise AssertionError("singleton wrapper class not found")


def test_apply_feedback_returns_disabled_when_flag_off(monkeypatch):
    monkeypatch.setattr(service, "CHUNK_FEEDBACK_ENABLED", False)
    update = MagicMock(return_value=True)
    monkeypatch.setattr(service.ChunkFeedbackService, "update_chunk_weight", update)

    result = service.ChunkFeedbackService.apply_feedback(
        "tenant-1",
        {"chunks": [{"id": "chunk-1", "dataset_id": "kb-1"}]},
        True,
    )

    assert result == {"success_count": 0, "fail_count": 0, "chunk_ids": [], "disabled": True}
    update.assert_not_called()


def test_feedback_rows_accept_local_and_restful_chunk_shapes():
    rows = service.ChunkFeedbackService._feedback_rows_from_reference(
        {
            "chunks": [
                {"chunk_id": "chunk-1", "kb_id": "kb-1"},
                {"id": "chunk-2", "dataset_id": "kb-2"},
                {"id": "missing-kb"},
            ]
        }
    )

    assert [(chunk_id, kb_id) for chunk_id, kb_id, _chunk in rows] == [
        ("chunk-1", "kb-1"),
        ("chunk-2", "kb-2"),
    ]


def test_relevance_weighting_sends_budget_to_highest_signal(monkeypatch):
    monkeypatch.setattr(service, "CHUNK_FEEDBACK_ENABLED", True)
    monkeypatch.setattr(service, "CHUNK_FEEDBACK_WEIGHTING", "relevance")
    update = MagicMock(return_value=True)
    monkeypatch.setattr(service.ChunkFeedbackService, "update_chunk_weight", update)

    result = service.ChunkFeedbackService.apply_feedback(
        "tenant-1",
        {
            "chunks": [
                {"id": "high", "dataset_id": "kb-1", "similarity": 0.9},
                {"id": "low", "dataset_id": "kb-1", "similarity": 0.1},
            ]
        },
        True,
    )

    assert result["success_count"] == 1
    update.assert_called_once_with("tenant-1", "high", "kb-1", 1, row_id=None)


def test_uniform_weighting_updates_every_referenced_chunk(monkeypatch):
    monkeypatch.setattr(service, "CHUNK_FEEDBACK_ENABLED", True)
    monkeypatch.setattr(service, "CHUNK_FEEDBACK_WEIGHTING", "uniform")
    update = MagicMock(return_value=True)
    monkeypatch.setattr(service.ChunkFeedbackService, "update_chunk_weight", update)

    service.ChunkFeedbackService.apply_feedback(
        "tenant-1",
        {"chunks": [{"id": "c1", "dataset_id": "kb-1"}, {"id": "c2", "dataset_id": "kb-1"}]},
        False,
    )

    update.assert_any_call("tenant-1", "c1", "kb-1", -1, row_id=None)
    update.assert_any_call("tenant-1", "c2", "kb-1", -1, row_id=None)


def test_apply_feedback_forwards_row_id(monkeypatch):
    monkeypatch.setattr(service, "CHUNK_FEEDBACK_ENABLED", True)
    monkeypatch.setattr(service, "CHUNK_FEEDBACK_WEIGHTING", "relevance")
    update = MagicMock(return_value=True)
    monkeypatch.setattr(service.ChunkFeedbackService, "update_chunk_weight", update)

    service.ChunkFeedbackService.apply_feedback(
        "tenant-1",
        {"chunks": [{"id": "c1", "dataset_id": "kb-1", "row_id": "42"}]},
        True,
    )

    update.assert_called_once_with("tenant-1", "c1", "kb-1", 1, row_id=42)


def test_update_chunk_weight_clamps_min_and_max(monkeypatch):
    class FakeStore:
        def __init__(self, current):
            self.current = current
            self.updated = None
            self.adjust_chunk_pagerank_fea = None

        def get(self, *_args):
            return {PAGERANK_FLD: self.current}

        def update(self, _condition, new_value, *_args):
            self.updated = new_value[PAGERANK_FLD]
            return True

    monkeypatch.setattr(service.ChunkFeedbackService, "_index_candidates", lambda *_args: ["idx"])
    monkeypatch.setattr(service.settings, "DOC_ENGINE", "milvus")

    high = FakeStore(100)
    monkeypatch.setattr(service.settings, "docStoreConn", high)
    assert service.ChunkFeedbackService.update_chunk_weight("tenant-1", "c1", "kb-1", 5)
    assert high.updated == service.MAX_PAGERANK_WEIGHT

    low = FakeStore(0)
    monkeypatch.setattr(service.settings, "docStoreConn", low)
    assert service.ChunkFeedbackService.update_chunk_weight("tenant-1", "c1", "kb-1", -5)
    assert low.updated == service.MIN_PAGERANK_WEIGHT


def test_update_chunk_weight_uses_adjust_method_with_row_id(monkeypatch):
    adjust = MagicMock(return_value=True)
    store = SimpleNamespace(adjust_chunk_pagerank_fea=adjust)
    monkeypatch.setattr(service.settings, "docStoreConn", store)
    monkeypatch.setattr(service.ChunkFeedbackService, "_index_candidates", lambda *_args: ["idx"])

    assert service.ChunkFeedbackService.update_chunk_weight("tenant-1", "c1", "kb-1", 1, row_id=9)
    adjust.assert_called_once_with(
        "c1",
        "idx",
        "kb-1",
        1,
        service.MIN_PAGERANK_WEIGHT,
        service.MAX_PAGERANK_WEIGHT,
        row_id=9,
    )


def test_milvus_adjust_uses_primary_key_kb_filter_and_upserts_clamped_weight():
    cls = _wrapped_singleton_class(milvus_module.MilvusConnection)
    conn = cls.__new__(cls)
    conn.logger = MagicMock()

    class FakeMilvusClient:
        def __init__(self):
            self.query_filter = None
            self.delete_filter = None
            self.inserted = None

        def has_collection(self, collection_name):
            assert collection_name == "idx"
            return True

        def describe_collection(self, _collection_name):
            return {
                "fields": [
                    {"name": "pk", "is_primary": True, "type": milvus_module.DataType.VARCHAR},
                    {"name": "id", "type": milvus_module.DataType.VARCHAR},
                    {"name": "kb_id", "type": milvus_module.DataType.VARCHAR},
                    {"name": PAGERANK_FLD, "type": milvus_module.DataType.INT64},
                ]
            }

        def query(self, _collection_name, filter_expr, output_fields):
            assert output_fields == ["*"]
            self.query_filter = filter_expr
            return [{"pk": "c1", "id": "c1", "kb_id": "kb-1", PAGERANK_FLD: 99}]

        def delete(self, _collection_name, expression):
            self.delete_filter = expression

        def insert_rows(self, _collection_name, rows):
            self.inserted = rows

    fake = FakeMilvusClient()
    conn._get_connection = lambda: fake

    assert conn.adjust_chunk_pagerank_fea("c1", "idx", "kb-1", 5, 0, 100)
    assert fake.query_filter == "pk == 'c1' && kb_id == 'kb-1'"
    assert fake.delete_filter == fake.query_filter
    assert fake.inserted[0][PAGERANK_FLD] == 100
