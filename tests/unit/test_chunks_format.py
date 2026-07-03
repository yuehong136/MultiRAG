from core.prompts.generator import chunks_format


def test_chunks_format_keeps_knowledge_graph_content_with_weight() -> None:
    reference = {
        "chunks": [
            {
                "chunk_id": "kg-chunk-1",
                "content_with_weight": "---- Entities ----\nentity,data",
                "docnm_kwd": "Related content in Knowledge Graph",
                "kb_id": "kb-1",
                "positions": [],
            }
        ]
    }

    chunks = chunks_format(reference)

    assert chunks[0]["content"] == "---- Entities ----\nentity,data"
    assert chunks[0]["document_name"] == "Related content in Knowledge Graph"
