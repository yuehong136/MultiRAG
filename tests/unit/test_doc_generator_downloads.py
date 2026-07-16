import json

from agent.component.docs_generator import DocGeneratorParam, sanitize_filename
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
