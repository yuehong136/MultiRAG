from typing import Any

from deepdoc.parser import docling_parser


class _Response:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


def test_remote_docling_prefers_chunked_conversion(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: int) -> _Response:
        calls.append((url, json))
        assert timeout == 600
        return _Response(
            200,
            {
                "results": [
                    {"text": "first chunk"},
                    {"chunk": {"text": "second chunk"}},
                ]
            },
        )

    monkeypatch.setattr(docling_parser.requests, "post", fake_post)

    parser = docling_parser.DoclingParser(docling_server_url="http://docling")
    sections, tables = parser._parse_pdf_remote("sample.pdf", binary=b"pdf")

    assert sections == [("first chunk", ""), ("second chunk", "")]
    assert tables == []
    assert calls[0][0] == "http://docling/v1/convert/source"
    assert calls[0][1]["options"]["do_chunking"] is True
    assert calls[0][1]["options"]["chunking_options"] == {
        "max_tokens": 512,
        "overlap": 50,
        "tokenizer": "sentencepiece",
    }


def test_remote_docling_falls_back_to_standard_conversion(monkeypatch) -> None:
    responses = iter(
        [
            _Response(404, text="not found"),
            _Response(422, text="unsupported options"),
            _Response(200, {"document": {"md_content": "fallback text"}}),
        ]
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: int) -> _Response:
        calls.append((url, json))
        assert timeout == 600
        return next(responses)

    monkeypatch.setattr(docling_parser.requests, "post", fake_post)

    parser = docling_parser.DoclingParser(docling_server_url="http://docling")
    sections, tables = parser._parse_pdf_remote("sample.pdf", binary=b"pdf")

    assert sections == [("fallback text", "")]
    assert tables == []
    assert [url for url, _ in calls] == [
        "http://docling/v1/convert/source",
        "http://docling/v1alpha/convert/source",
        "http://docling/v1/convert/source",
    ]
    assert "do_chunking" not in calls[-1][1]["options"]
