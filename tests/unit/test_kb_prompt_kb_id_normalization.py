from typing import Any

import pytest

from core.prompts import generator as generator_module


def test_kb_prompt_does_not_query_ambiguous_graph_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> Any:
        raise AssertionError("Prompt rendering must not open a database connection")

    monkeypatch.setattr(generator_module, "db_connection", forbidden)
    result = generator_module.kb_prompt(
        {"chunks": [{"chunk_id": "kg-1", "doc_id": "", "docnm_kwd": "Related content in Knowledge Graph", "kb_id": ["kb-1"], "content_with_weight": "graph chunk"}]}, 1024
    )
    assert "graph chunk" in result[0]
