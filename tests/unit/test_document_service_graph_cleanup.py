from api.db.services.document_service import DocumentService


def test_normalize_graph_source_ids_supports_nested_backend_mapping() -> None:
    graph_source = {"graph_chunk": {"source_id": ["doc-1", "doc-2"]}}

    assert DocumentService._normalize_graph_source_ids(graph_source) == ["doc-1", "doc-2"]


def test_normalize_graph_source_ids_supports_list_shaped_backend_values() -> None:
    graph_source = {"graph_chunk": ["doc-1", "doc-2"]}

    assert DocumentService._normalize_graph_source_ids(graph_source) == ["doc-1", "doc-2"]


def test_normalize_graph_source_ids_supports_newline_delimited_strings() -> None:
    graph_source = {"graph_chunk": {"source_id": "doc-1\ndoc-2"}}

    assert DocumentService._normalize_graph_source_ids(graph_source) == ["doc-1", "doc-2"]
