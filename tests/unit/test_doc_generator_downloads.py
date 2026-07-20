import json
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.component.docs_generator import DocGenerator, DocGeneratorParam, sanitize_filename
from agent.component.message import Message, MessageParam


def test_doc_generator_param_matches_download_contract():
    param = DocGeneratorParam()

    assert param.output_format == "pdf"
    assert param.outputs == {"download": {"value": "", "type": "string"}}


def test_sanitize_filename_removes_paths_and_enforces_extension():
    assert sanitize_filename("../../quarterly/report.exe", "pdf") == "quarterly report.pdf"
    assert sanitize_filename("", "docx") == "file.docx"


def test_message_extracts_single_and_multiple_downloads():
    message = object.__new__(Message)
    first = {
        "doc_id": "doc-1",
        "filename": "one.pdf",
        "mime_type": "application/pdf",
        "size": 10,
    }
    second = {
        "doc_id": "doc-2",
        "filename": "two.docx",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    assert message._extract_downloads(json.dumps(first)) == [first]
    assert message._extract_downloads([first, second]) == [first, second]
    assert message._extract_downloads("ordinary message") == []


def test_message_removes_download_metadata_from_text_and_accumulates_it():
    message = object.__new__(Message)
    downloads = []
    download = {
        "doc_id": "doc-1",
        "filename": "report.pdf",
        "mime_type": "application/pdf",
    }

    assert message._stringify_message_value(download, downloads=downloads) == ""
    assert downloads == [download]
    assert message._stringify_message_value({"status": "ok"}) == '{"status": "ok"}'


def test_message_param_exposes_downloads_output():
    assert MessageParam().outputs == {
        "content": {"type": "str"},
        "downloads": {"type": "list"},
    }


def test_doc_generator_uses_runtime_content_when_param_content_is_empty():
    generator = object.__new__(DocGenerator)
    generator._param = SimpleNamespace(content="")
    generator._canvas = MagicMock()

    assert generator._resolve_content({"content": "runtime content"}) == "runtime content"
    generator._canvas.get_variable_value.assert_not_called()


def test_doc_generator_resolves_mixed_stream_and_structured_variables():
    generator = object.__new__(DocGenerator)
    generator._param = SimpleNamespace(
        content="Answer={begin@answer}; metadata={begin@metadata}; missing={begin@missing}",
    )
    values = {
        "begin@answer": partial(lambda: iter(["hel", "lo"])),
        "begin@metadata": {"status": "ok"},
        "begin@missing": None,
    }
    generator._canvas = MagicMock()
    generator._canvas.get_variable_value.side_effect = values.get

    assert generator._resolve_content({}) == 'Answer=hello; metadata={"status": "ok"}; missing='
