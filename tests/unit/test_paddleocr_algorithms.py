"""PaddleOCR service contracts: algorithm selection, payloads and parsed sections."""

import base64
import json
from typing import Any

import pytest
from beartype.roar import BeartypeCallHintParamViolation

import deepdoc.parser.paddleocr_parser as paddle_module
from core.llm.ocr_model import PaddleOCROcrModel
from deepdoc.parser.paddleocr_parser import SUPPORTED_PADDLEOCR_ALGORITHMS, AlgorithmType, PaddleOCRConfig, PaddleOCRParser


@pytest.mark.parametrize("algorithm", SUPPORTED_PADDLEOCR_ALGORITHMS)
def test_configuration_accepts_exact_algorithms(algorithm: AlgorithmType) -> None:
    config = PaddleOCRConfig.from_dict({"algorithm": algorithm})
    assert config.algorithm == algorithm
    assert config.algorithm_config["use_doc_unwarping"] is False


@pytest.mark.parametrize("algorithm", ["PaddleOCR", "OCR", "", "PaddleOCR-VL-1.6"])
def test_configuration_rejects_unknown_or_partial_algorithms(algorithm: Any) -> None:
    with pytest.raises(ValueError, match="Unsupported algorithm"):
        PaddleOCRConfig.from_dict({"algorithm": algorithm})


@pytest.mark.parametrize(
    "config",
    [
        {"algorithm": None},
        {"algorithm": "PP-OCRv5", "algorithm_config": {"max_new_tokens": 100}},
        {"algorithm": "PaddleOCR-VL", "algorithm_config": {"use_table_recognition": True}},
        {"algorithm_config": []},
        {"algorithm_config": {"use_doc_unwarping": "false"}},
        {"algorithm_config": {"max_new_tokens": "100"}},
        {"algorithm_config": {"temperature": True}},
        {"request_timeout": 0},
        {"request_timeout": True},
        {"request_timeout": "10"},
        {"additional_params": []},
        {"visualize": "false"},
    ],
)
def test_configuration_rejects_invalid_fields_and_types(config: dict[str, Any]) -> None:
    # Runtime annotations can reject malformed types before dataclass validation.
    with pytest.raises((ValueError, BeartypeCallHintParamViolation)):
        PaddleOCRConfig.from_dict(config)


@pytest.mark.parametrize("algorithm", SUPPORTED_PADDLEOCR_ALGORITHMS)
def test_default_payload_uses_only_algorithm_service_parameters(algorithm: AlgorithmType) -> None:
    parser = PaddleOCRParser(algorithm=algorithm)
    payload = parser._build_payload(b"pdf", 0, PaddleOCRConfig(algorithm=algorithm))
    expected = {"file": base64.b64encode(b"pdf").decode(), "fileType": 0, "visualize": False, "useDocOrientationClassify": False, "useDocUnwarping": False}
    if algorithm != "PP-OCRv5":
        expected.update(prettifyMarkdown=True, showFormulaNumber=True, formatBlockContent=True)
    if algorithm in {"PaddleOCR-VL", "PaddleOCR-VL-1.5"}:
        expected.update(mergeLayoutBlocks=False, restructurePages=False)
    assert payload == expected


def test_ocr_and_structure_parameter_mapping_preserves_false_and_zero() -> None:
    parser = PaddleOCRParser()
    config = PaddleOCRConfig(algorithm="PP-StructureV3", algorithm_config={"use_table_recognition": False, "text_rec_score_thresh": 0.0, "use_e2e_wired_table_rec_model": True})
    payload = parser._build_payload(b"pdf", 0, config)
    assert payload["useTableRecognition"] is False
    assert payload["textRecScoreThresh"] == 0.0
    assert payload["useE2eWiredTableRecModel"] is True
    assert "maxNewTokens" not in payload


def _result(algorithm: AlgorithmType) -> dict[str, Any]:
    if algorithm == "PP-OCRv5":
        return {
            "ocrResults": [
                {"prunedResult": {"rec_texts": [" First ", " "], "rec_boxes": [[40, 80, 10, 20], [0, 0, 0, 0]]}},
                {"prunedResult": {"rec_texts": ["Second"], "rec_polys": [[[20, 40], [80, 42], [78, 60], [18, 58]]]}},
            ]
        }
    return {
        "layoutParsingResults": [
            {"prunedResult": {"parsing_res_list": [{"block_content": " First ", "block_label": "text", "block_bbox": [40, 80, 10, 20]}]}},
            {"prunedResult": {"parsing_res_list": [{"block_content": "Second", "block_label": "table", "block_bbox": [18, 40, 80, 60]}]}},
        ]
    }


@pytest.mark.parametrize("algorithm", SUPPORTED_PADDLEOCR_ALGORITHMS)
@pytest.mark.parametrize("method", ["raw", "manual", "pipeline", "paper"])
def test_sections_preserve_page_coordinates_and_caller_tuple_shapes(algorithm: AlgorithmType, method: str) -> None:
    parser = PaddleOCRParser(algorithm=algorithm)
    sections = parser._transfer_to_sections(_result(algorithm), algorithm, method)
    tags = ["@@1\t5.0\t20.0\t10.0\t40.0##", "@@2\t9.0\t40.0\t20.0\t30.0##"]
    labels = ["text", "text" if algorithm == "PP-OCRv5" else "table"]
    if method in {"manual", "pipeline"}:
        assert sections == [("First", labels[0], tags[0]), ("Second", labels[1], tags[1])]
    elif method == "paper":
        assert sections == [("First" + tags[0], labels[0]), ("Second" + tags[1], labels[1])]
    else:
        assert sections == [("First", tags[0]), ("Second", tags[1])]


@pytest.mark.parametrize("algorithm", SUPPORTED_PADDLEOCR_ALGORITHMS)
def test_wrong_result_shape_fails_instead_of_returning_success_with_no_text(algorithm: AlgorithmType) -> None:
    with pytest.raises(ValueError, match="response missing"):
        PaddleOCRParser()._transfer_to_sections({}, algorithm, "raw")


def test_ocr_without_coordinates_preserves_text_and_empty_pages_are_valid() -> None:
    parser = PaddleOCRParser(algorithm="PP-OCRv5")
    result = {"ocrResults": [{"prunedResult": {"rec_texts": []}}, {"prunedResult": {"rec_texts": ["Text"]}}]}
    assert parser._transfer_to_sections(result, "PP-OCRv5", "pipeline") == [("Text", "text", "@@2\t0.0\t0.0\t0.0\t0.0##")]


@pytest.mark.parametrize("algorithm", SUPPORTED_PADDLEOCR_ALGORITHMS)
@pytest.mark.parametrize("key_shape", ["flat", "nested", "dict", "env"])
def test_model_reads_existing_and_new_saved_configuration(algorithm: AlgorithmType, key_shape: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config = {"paddleocr_api_url": "http://ocr.local/service", "paddleocr_algorithm": algorithm, "paddleocr_access_token": "test-token"}
    key: str | dict = config if key_shape == "dict" else json.dumps({"api_key": config} if key_shape == "nested" else config)
    if key_shape == "env":
        key = json.dumps({"PADDLEOCR_API_URL": config["paddleocr_api_url"], "PADDLEOCR_ALGORITHM": algorithm, "PADDLEOCR_ACCESS_TOKEN": "test-token"})
    monkeypatch.setattr(paddle_module.RAGFlowPdfParser, "__init__", lambda _self: pytest.fail("remote OCR must not load local DeepDOC models"))
    model = PaddleOCROcrModel(key, "saved-model")
    assert model.algorithm == algorithm
    assert model.api_url == config["paddleocr_api_url"]
    assert model.access_token == "test-token"
    assert model.check_available() == (True, "")


@pytest.mark.parametrize("algorithm", SUPPORTED_PADDLEOCR_ALGORITHMS)
def test_parse_model_posts_selected_algorithm_and_honors_timeout(algorithm: AlgorithmType, monkeypatch: pytest.MonkeyPatch) -> None:
    model = PaddleOCROcrModel({"paddleocr_api_url": "http://ocr.local/service", "paddleocr_algorithm": algorithm, "paddleocr_access_token": "test-token"}, "model")
    monkeypatch.setattr(paddle_module, "extract_pdf_outlines", lambda _source: [])
    monkeypatch.setattr(model, "__images__", lambda *_args, **_kwargs: None)
    calls: list[dict[str, Any]] = []

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"errorCode": 0, "result": _result(algorithm)}

    def post(url: str, **kwargs: Any) -> Response:
        calls.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(paddle_module.requests, "post", post)
    sections, tables = model.parse_pdf("sample.pdf", binary=b"pdf", parse_method="pipeline", request_timeout=19)
    assert len(sections) == 2
    assert tables == []
    assert calls[0]["url"] == "http://ocr.local/service"
    assert calls[0]["timeout"] == 19
    assert calls[0]["headers"]["Authorization"] == "token test-token"
    assert base64.b64decode(calls[0]["json"]["file"]) == b"pdf"


@pytest.mark.parametrize("body", [[], None, {"errorCode": 500, "result": {}}, {"errorCode": False, "result": {}}, {"errorCode": 0, "result": []}])
def test_send_request_rejects_invalid_envelopes(body: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> Any:
            return body

    monkeypatch.setattr(paddle_module.requests, "post", lambda *_args, **_kwargs: Response())
    with pytest.raises(RuntimeError, match="invalid response format"):
        PaddleOCRParser()._send_request(b"pdf", PaddleOCRConfig(), None)


@pytest.mark.parametrize("timeout", [True, 1.5, 0, "invalid"])
def test_saved_model_rejects_invalid_timeout(timeout: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        PaddleOCROcrModel({"paddleocr_timeout": timeout}, "model")


@pytest.mark.parametrize("url", ["not-a-url", "http://", "ftp://server/ocr"])
def test_saved_model_rejects_invalid_service_url(url: str) -> None:
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        PaddleOCROcrModel({"paddleocr_api_url": url}, "model")
