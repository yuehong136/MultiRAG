from typing import Any

import numpy as np
import pytest
import requests

from core.llm import RerankModel


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("host:8080", "http://host:8080/rerank"),
        ("http://host:8080/", "http://host:8080/rerank"),
        ("https://host/prefix/", "https://host/prefix/rerank"),
        ("https://host/prefix/rerank/", "https://host/prefix/rerank"),
        ("https://host/prefix/rerank?tenant=a", "https://host/prefix/rerank?tenant=a"),
    ],
)
def test_registered_huggingface_url_and_batch_alignment(monkeypatch: pytest.MonkeyPatch, url: str, expected: str) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def post(endpoint: str, **kwargs: Any) -> requests.Response:
        calls.append((endpoint, kwargs))
        response = requests.Response()
        response.status_code = 200
        import json

        response._content = json.dumps([{"index": i, "score": float(t)} for i, t in reversed(list(enumerate(kwargs["json"]["texts"])))]).encode()
        return response

    monkeypatch.setattr(requests, "post", post)
    texts = [str(i) for i in range(17)]
    provider = RerankModel["HuggingFace"]("x", base_url=url)
    scores, tokens = provider.similarity("query", texts)
    assert [endpoint for endpoint, _ in calls] == [expected] * 3
    assert [len(kwargs["json"]["texts"]) for _, kwargs in calls] == [8, 8, 1]
    np.testing.assert_array_equal(scores, np.arange(17) / 16)
    assert tokens > 0
    assert texts == [str(i) for i in range(17)]
