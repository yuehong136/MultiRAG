import asyncio
from types import SimpleNamespace

import numpy as np

from core.flow.parser.pdf_chunk_metadata import PDF_POSITIONS_KEY
from core.flow.tokenizer import tokenizer as tokenizer_module
from core.flow.tokenizer.tokenizer import Tokenizer, TokenizerParam


def _make_tokenizer():
    param = TokenizerParam()
    param.search_method = ["full_text"]
    tokenizer = Tokenizer.__new__(Tokenizer)
    tokenizer._param = param
    tokenizer._canvas = SimpleNamespace(_kb_id=None, _tenant_id="tenant-1")
    tokenizer.callback = lambda *args, **kwargs: None
    tokenizer._id = "tokenizer-1"
    return tokenizer


def _outputs(tokenizer):
    return {key: value["value"] for key, value in tokenizer._param.outputs.items()}


def test_tokenizer_writes_chunk_order_and_finalizes_pdf_fields():
    tokenizer = _make_tokenizer()

    asyncio.run(
        tokenizer._invoke(
            name="sample.pdf",
            chunks=[
                {"text": "first", PDF_POSITIONS_KEY: [[0, 1, 2, 3, 4]]},
                {"text": "second"},
            ],
        )
    )

    chunks = _outputs(tokenizer)["chunks"]

    assert [chunk["chunk_order_int"] for chunk in chunks] == [0, 1]
    assert chunks[0]["position_int"] == [(1, 1, 2, 3, 4)]
    assert chunks[0]["page_num_int"] == [1]
    assert chunks[0]["top_int"] == [3]
    assert PDF_POSITIONS_KEY not in chunks[0]


def test_tokenizer_accepts_empty_chunk_outputs():
    tokenizer = _make_tokenizer()

    asyncio.run(tokenizer._invoke(name="empty.pdf", output_format="chunks", chunks=[]))

    assert _outputs(tokenizer)["chunks"] == []


def test_tokenizer_accepts_empty_json_outputs():
    tokenizer = _make_tokenizer()

    asyncio.run(tokenizer._invoke(name="empty.json", output_format="json", json=[]))

    assert _outputs(tokenizer)["chunks"] == []


def test_tokenizer_embedding_skips_blank_text_chunks(monkeypatch):
    calls = []

    class DummyDb:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyEmbeddingBundle:
        max_length = 128

        def __init__(self, *args, **kwargs):
            pass

        def encode(self, texts):
            calls.append(list(texts))
            return np.array([[float(index + 1), float(len(text))] for index, text in enumerate(texts)]), len(texts)

    monkeypatch.setattr(tokenizer_module, "db_connection", lambda: DummyDb())
    monkeypatch.setattr(tokenizer_module, "get_tenant_default_model_by_type", lambda *args, **kwargs: {})
    monkeypatch.setattr(tokenizer_module, "LLMBundle", DummyEmbeddingBundle)

    tokenizer = _make_tokenizer()
    tokenizer._param.search_method = ["embedding"]
    chunks = [
        {"text": "   "},
        {"text": "<table></table>"},
        {"text": " alpha "},
    ]

    result, token_count = asyncio.run(tokenizer._embedding("sample.pdf", chunks))

    assert result is chunks
    assert token_count == 2
    assert calls == [["sample.pdf"], ["alpha"]]
    assert not any(key.startswith("q_") for key in chunks[0])
    assert not any(key.startswith("q_") for key in chunks[1])
    assert "q_2_vec" in chunks[2]
