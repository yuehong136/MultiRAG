import api.db.services.doc_metadata_service as doc_metadata_service_module
from core.prompts import generator as generator_module


class _FakeDbConnection:
    def __call__(self):
        return self

    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


def test_kb_prompt_normalizes_list_kb_id_before_metadata_lookup(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_get_metadata_for_documents(cls, db, doc_ids, kb_id):
        captured["kb_id"] = kb_id
        return {}

    monkeypatch.setattr(generator_module, "db_connection", _FakeDbConnection())
    monkeypatch.setattr(
        doc_metadata_service_module.DocMetadataService,
        "get_metadata_for_documents",
        classmethod(fake_get_metadata_for_documents),
    )

    generator_module.kb_prompt(
        {
            "chunks": [
                {
                    "chunk_id": "kg-1",
                    "doc_id": "",
                    "docnm_kwd": "Related content in Knowledge Graph",
                    "kb_id": ["kb-1"],
                    "content_with_weight": "graph chunk",
                }
            ]
        },
        1024,
    )

    assert captured["kb_id"] == "kb-1"
