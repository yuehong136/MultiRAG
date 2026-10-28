from typing import Any

import pytest
from PIL import Image

from deepdoc.parser import docling_parser


class _Response:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


def test_remote_docling_prefers_chunked_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_remote_docling_falls_back_to_standard_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.parametrize(
    "payload",
    [
        {"document": {"md_content": "# Parsed\n\nbody"}},
        {"document": {"md_content": " ", "text_content": "# Parsed\n\nbody"}},
        {"documents": [{"text_content": "# Parsed\n\nbody"}]},
        {"results": [{"document": {"md_content": "# Parsed\n\nbody"}}]},
        {"results": [{"result": {"text_content": "# Parsed\n\nbody"}}]},
        {"document": {"json_content": {"md_content": "# Parsed\n\nbody"}}},
    ],
)
def test_chunk_request_accepts_standard_response_without_second_request(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, payload: Any) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: int) -> _Response:
        calls.append((url, json))
        return _Response(200, payload)

    monkeypatch.setattr(docling_parser.requests, "post", fake_post)
    parser = docling_parser.DoclingParser(docling_server_url="http://docling")

    with caplog.at_level("INFO", logger="DoclingParser"):
        sections, tables = parser._parse_pdf_remote("sample.pdf", binary=b"pdf")

    assert sections == [("# Parsed\n\nbody", "")]
    assert tables == []
    assert len(calls) == 1
    assert calls[0][1]["options"]["do_chunking"] is True
    assert "Successfully used native chunking" not in caplog.text


@pytest.mark.parametrize("container", ["list", "results", "chunks"])
def test_native_chunk_containers_preserve_nonempty_chunks(monkeypatch: pytest.MonkeyPatch, container: str) -> None:
    chunks: list[Any] = [None, {"text": "alpha"}, {"text": " "}, {"chunk": {"text": "beta"}}, {"text": 42}]
    payload: Any = chunks if container == "list" else {container: chunks, "document": {"md_content": "unused fallback"}}
    calls: list[str] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: int) -> _Response:
        calls.append(url)
        return _Response(200, payload)

    monkeypatch.setattr(docling_parser.requests, "post", fake_post)
    parser = docling_parser.DoclingParser(docling_server_url="http://docling")
    sections, tables = parser._parse_pdf_remote("sample.pdf", binary=b"pdf")

    assert sections == [("alpha", ""), ("beta", "")]
    assert tables == []
    assert calls == ["http://docling/v1/convert/source"]


@pytest.mark.parametrize("container", ["results", "chunks"])
@pytest.mark.parametrize(
    "entries",
    [
        [],
        [None, 42, "bad entry", {}, {"text": " "}, {"text": 42}, {"chunk": None}, {"chunk": {"text": False}}],
        "bad container",
        {"text": "not a chunk list"},
    ],
)
def test_empty_or_invalid_native_entries_use_standard_body(monkeypatch: pytest.MonkeyPatch, container: str, entries: Any) -> None:
    calls: list[str] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: int) -> _Response:
        calls.append(url)
        return _Response(200, {container: entries, "document": {"text_content": "recovered text"}})

    monkeypatch.setattr(docling_parser.requests, "post", fake_post)
    parser = docling_parser.DoclingParser(docling_server_url="http://docling")
    sections, tables = parser._parse_pdf_remote("sample.pdf", binary=b"pdf")

    assert sections == [("recovered text", "")]
    assert tables == []
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "invalid payload",
        42,
        [],
        {},
        {"results": []},
        {"results": [{"text": " "}, None, {"chunk": {"text": 42}}]},
        {"chunks": [{"text": False}]},
        {"document": {}},
        {"document": {"md_content": " ", "text_content": None}},
    ],
)
def test_remote_response_without_usable_body_fails(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    calls: list[str] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: int) -> _Response:
        calls.append(url)
        return _Response(200, payload)

    monkeypatch.setattr(docling_parser.requests, "post", fake_post)
    parser = docling_parser.DoclingParser(docling_server_url="http://docling")

    with pytest.raises(RuntimeError, match=r"\[Docling\]"):
        parser._parse_pdf_remote("sample.pdf", binary=b"pdf")

    assert len(calls) == 1


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize(
    ("parse_method", "expected"),
    [
        ("raw", [("body", "")]),
        ("manual", [("body", "text", "")]),
        ("pipeline", [("body", "text", "")]),
        ("paper", [("body", "text")]),
    ],
)
def test_remote_sections_keep_consumer_tuple_shape(monkeypatch: pytest.MonkeyPatch, native: bool, parse_method: str, expected: list[tuple[str, ...]]) -> None:
    payload = {"results": [{"text": "body"}]} if native else {"document": {"md_content": "body"}}

    def fake_post(url: str, *, json: dict[str, Any], timeout: int) -> _Response:
        return _Response(200, payload)

    monkeypatch.setattr(docling_parser.requests, "post", fake_post)
    parser = docling_parser.DoclingParser(docling_server_url="http://docling")
    sections, tables = parser._parse_pdf_remote("sample.pdf", binary=b"pdf", parse_method=parse_method)

    assert sections == expected
    assert tables == []
    assert parser.page_images == []


def test_standard_response_keeps_json_fallback_for_each_document(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, *, json: dict[str, Any], timeout: int) -> _Response:
        return _Response(200, {"documents": [{"md_content": "first body"}, {"json_content": {"md_content": "second body"}}]})

    monkeypatch.setattr(docling_parser.requests, "post", fake_post)
    parser = docling_parser.DoclingParser(docling_server_url="http://docling")

    assert parser._parse_pdf_remote("sample.pdf", binary=b"pdf") == ([("first body", ""), ("second body", "")], [])


@pytest.mark.parametrize("need_position", [True, False])
def test_docling_crop_without_page_images_returns_no_image(need_position: bool) -> None:
    parser = docling_parser.DoclingParser()

    assert parser.crop("@@1\t1\t10\t20\t30##", need_position=need_position) == ((None, None) if need_position else None)


@pytest.mark.parametrize("need_position", [True, False])
@pytest.mark.parametrize("tag", ["not a position", "@@0\t1\t10\t20\t30##", "@@2\t1\t10\t20\t30##", "@@1-2\t1\t10\t20\t30##"])
def test_docling_crop_skips_positions_outside_rendered_pages(tag: str, need_position: bool) -> None:
    parser = docling_parser.DoclingParser()
    with Image.new("RGB", (100, 100)) as page:
        parser.page_images = [page]

        assert parser.crop(tag, need_position=need_position) == ((None, None) if need_position else None)
