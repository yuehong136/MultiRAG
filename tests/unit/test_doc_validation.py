from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from api.db import FileType
from api.utils.validation_utils import (
    UpdateDocumentReq,
    validate_chunk_method,
    validate_document_name,
    validate_immutable_fields,
)
from common.constants import RetCode


def test_update_document_req_validates_parser_config_bounds():
    with pytest.raises(ValidationError) as exc_info:
        UpdateDocumentReq(parser_config={"chunk_token_num": 0})

    assert "chunk_token_num" in str(exc_info.value)


def test_update_document_req_dumps_partial_parser_config_only():
    req = UpdateDocumentReq(parser_config={"chunk_token_num": 256})

    assert req.model_dump(exclude_unset=True) == {"parser_config": {"chunk_token_num": 256}}


def test_update_document_req_rejects_invalid_chunk_method_and_enabled():
    with pytest.raises(ValidationError):
        UpdateDocumentReq(chunk_method="unknown")

    with pytest.raises(ValidationError):
        UpdateDocumentReq(enabled=2)


def test_validate_immutable_fields_checks_explicit_zero_values():
    doc = SimpleNamespace(chunk_num=3, token_num=100, progress=0.5)
    error_msg, error_code = validate_immutable_fields(UpdateDocumentReq(chunk_count=0), doc)

    assert error_msg == "Can't change `chunk_count`."
    assert error_code == RetCode.DATA_ERROR


def test_validate_immutable_fields_allows_matching_values():
    doc = SimpleNamespace(chunk_num=0, token_num=100, progress=0.5)
    error_msg, error_code = validate_immutable_fields(UpdateDocumentReq(chunk_count=0, token_count=100, progress=0.5), doc)

    assert error_msg is None
    assert error_code is None


def test_validate_document_name_checks_extension_and_duplicates():
    doc = SimpleNamespace(name="old.pdf")
    error_msg, error_code = validate_document_name("new.docx", doc, [])

    assert error_msg == "The extension of file can't be changed"
    assert error_code == RetCode.ARGUMENT_ERROR

    error_msg, error_code = validate_document_name("new.pdf", doc, [SimpleNamespace(name="new.pdf")])
    assert error_msg == "Duplicated document name in the same dataset."
    assert error_code == RetCode.DATA_ERROR


def test_validate_chunk_method_rejects_visual_and_presentation_files():
    visual_doc = SimpleNamespace(type=FileType.VISUAL, name="image.jpg")
    error_msg, error_code = validate_chunk_method(visual_doc, "naive")

    assert error_msg == "Not supported yet!"
    assert error_code == RetCode.DATA_ERROR

    ppt_doc = SimpleNamespace(type=FileType.PDF, name="slides.pptx")
    error_msg, error_code = validate_chunk_method(ppt_doc, "naive")

    assert error_msg == "Not supported yet!"
    assert error_code == RetCode.DATA_ERROR
