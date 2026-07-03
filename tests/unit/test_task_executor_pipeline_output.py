from core.svr.task_executor import _normalize_pipeline_output_chunks


def test_normalize_pipeline_output_chunks_treats_empty_payloads_as_zero_chunks():
    for output in [
        {"embedding_token_consumption": 7, "chunks": []},
        {"embedding_token_consumption": 7, "json": []},
        {"embedding_token_consumption": 7, "markdown": ""},
        {"embedding_token_consumption": 7, "text": ""},
        {"embedding_token_consumption": 7, "html": ""},
        {"embedding_token_consumption": 7},
    ]:
        chunks, token_count = _normalize_pipeline_output_chunks(output)

        assert chunks == []
        assert token_count == 7


def test_normalize_pipeline_output_chunks_uses_field_presence_and_deepcopies():
    output = {
        "embedding_token_consumption": 3,
        "chunks": [{"text": "alpha"}],
        "json": [{"text": "fallback"}],
    }

    chunks, token_count = _normalize_pipeline_output_chunks(output)
    chunks[0]["text"] = "changed"

    assert token_count == 3
    assert chunks == [{"text": "changed"}]
    assert output["chunks"] == [{"text": "alpha"}]


def test_normalize_pipeline_output_chunks_wraps_scalar_text_outputs():
    assert _normalize_pipeline_output_chunks({"text": "alpha"}) == ([{"text": ["alpha"]}], 0)
    assert _normalize_pipeline_output_chunks({"markdown": "alpha"}) == ([{"text": ["alpha"]}], 0)
    assert _normalize_pipeline_output_chunks({"html": "alpha"}) == ([{"text": ["alpha"]}], 0)
