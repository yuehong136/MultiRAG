"""Official Job API transport, deadlines and JSONL result contracts."""

import json
from typing import Any

import pytest
import requests

import deepdoc.parser.paddleocr_parser as paddle_module
from deepdoc.parser.paddleocr_parser import SUPPORTED_PADDLEOCR_ALGORITHMS, AlgorithmType, PaddleOCRConfig, PaddleOCRParser


def _response(body: Any, status: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.encoding = "utf-8"
    return response


def _config(algorithm: AlgorithmType = "PaddleOCR-VL", timeout: int = 600) -> PaddleOCRConfig:
    return PaddleOCRConfig(api_url="https://gateway.example/paddle/api/v2/ocr/jobs?route=ocr", access_token="test-token", algorithm=algorithm, request_timeout=timeout)


@pytest.mark.parametrize("algorithm", SUPPORTED_PADDLEOCR_ALGORITHMS)
def test_job_upload_poll_and_jsonl_preserve_algorithm_pages_and_credentials(algorithm: AlgorithmType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    key = "ocrResults" if algorithm == "PP-OCRv5" else "layoutParsingResults"
    pages = (
        [{"prunedResult": {"rec_texts": ["First"]}}, {"prunedResult": {"rec_texts": ["Second"]}}]
        if algorithm == "PP-OCRv5"
        else [{"prunedResult": {"parsing_res_list": []}}, {"prunedResult": {"parsing_res_list": []}}]
    )
    result = _response(None)
    result._content = ("\n" + "\n\n".join(json.dumps({"errorCode": 0, "result": {key: [page]}}) for page in pages) + "\n").encode()
    responses = iter(
        [
            _response({"code": 0, "data": {"state": "pending"}}),
            _response({"code": 0, "data": {"state": "done", "resultUrl": {"jsonUrl": "https://storage.example/result.jsonl?signature=private"}}}),
            result,
        ]
    )

    def post(url: str, **kwargs: Any) -> requests.Response:
        calls.append({"url": url, **kwargs})
        return _response({"code": 0, "data": {"jobId": "job/one"}})

    def get(url: str, **kwargs: Any) -> requests.Response:
        calls.append({"url": url, **kwargs})
        return next(responses)

    monkeypatch.setattr(paddle_module.requests, "post", post)
    monkeypatch.setattr(paddle_module.requests, "get", get)
    monkeypatch.setattr(paddle_module.time, "sleep", lambda _seconds: None)
    result_data = PaddleOCRParser()._send_request(b"actual-pdf", _config(algorithm), None)
    assert result_data == {key: pages}
    assert calls[0]["files"] == {"file": ("document.pdf", b"actual-pdf", "application/pdf")}
    assert calls[0]["data"]["model"] == algorithm
    payload = json.loads(calls[0]["data"]["optionalPayload"])
    assert "file" not in payload and "fileType" not in payload
    assert payload["useDocOrientationClassify"] is False
    assert ("formatBlockContent" in payload) is (algorithm != "PP-OCRv5")
    assert calls[0]["headers"]["Authorization"] == "Bearer test-token"
    assert calls[1]["url"] == "https://gateway.example/paddle/api/v2/ocr/jobs/job%2Fone?route=ocr"
    assert calls[1]["headers"] == calls[0]["headers"]
    assert calls[-1]["url"].startswith("https://storage.example/")
    assert "headers" not in calls[-1]
    assert all(0 < call["timeout"] <= 600 for call in calls)


@pytest.mark.parametrize("token", [None, "", "  "])
def test_job_configuration_requires_token(token: str | None) -> None:
    with pytest.raises(ValueError, match="requires an access token"):
        PaddleOCRConfig(api_url="https://service.example/api/v2/ocr/jobs/", access_token=token)


@pytest.mark.parametrize("body", [None, [], {"code": False, "data": {}}, {"code": 0, "data": []}, {"code": 0}, {"code": 10010, "data": {}}])
def test_job_envelope_rejects_invalid_and_business_error_results(body: Any) -> None:
    with pytest.raises(RuntimeError):
        PaddleOCRParser._job_data(_response(body), "submit")


def test_job_http_error_retains_business_code_without_provider_body() -> None:
    with pytest.raises(RuntimeError, match=r"HTTP 400 \(API code 10010\)") as caught:
        PaddleOCRParser._job_data(_response({"code": 10010, "msg": "private-provider-body"}, 400), "submit")
    assert "private-provider-body" not in str(caught.value)


@pytest.mark.parametrize("body", [{"state": "failed"}, {"state": "unknown"}, {"state": []}, {}, {"state": "done"}, {"state": "done", "resultUrl": {"jsonUrl": "file:///private"}}])
def test_failed_or_malformed_jobs_fail_before_result_download(body: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paddle_module.requests, "post", lambda *_args, **_kwargs: _response({"code": 0, "data": {"jobId": "job-one"}}))
    calls: list[str] = []

    def get(url: str, **_kwargs: Any) -> requests.Response:
        calls.append(url)
        return _response({"code": 0, "data": body})

    monkeypatch.setattr(paddle_module.requests, "get", get)
    with pytest.raises(RuntimeError):
        PaddleOCRParser()._send_request(b"pdf", _config(), None)
    assert len(calls) == 1


@pytest.mark.parametrize("jsonl", ["", "\n\n", "not-json", "[]", '{"errorCode":false,"result":{}}', '{"errorCode":1,"result":{}}', '{"errorCode":0,"result":{"ocrResults":[]}}'])
def test_bad_result_jsonl_is_not_successful_empty_parse(jsonl: str) -> None:
    with pytest.raises(RuntimeError):
        PaddleOCRParser._merge_job_results(jsonl, "PaddleOCR-VL")


def test_timeout_covers_submission_polling_and_sleep_without_resubmission(monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = 0.0
    submits = 0
    polls = 0

    def post(*_args: Any, **_kwargs: Any) -> requests.Response:
        nonlocal submits
        submits += 1
        return _response({"code": 0, "data": {"jobId": "job-one"}})

    def get(*_args: Any, **_kwargs: Any) -> requests.Response:
        nonlocal polls
        polls += 1
        return _response({"code": 0, "data": {"state": "running"}})

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(paddle_module.requests, "post", post)
    monkeypatch.setattr(paddle_module.requests, "get", get)
    monkeypatch.setattr(paddle_module.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(paddle_module.time, "sleep", sleep)
    with pytest.raises(RuntimeError, match="timed out after 2s"):
        PaddleOCRParser()._send_request(b"pdf", _config(timeout=2), None)
    assert (submits, polls, elapsed) == (1, 1, 2.0)


def test_result_download_transport_error_hides_signed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paddle_module.requests, "post", lambda *_args, **_kwargs: _response({"code": 0, "data": {"jobId": "job-one"}}))

    def get(url: str, **_kwargs: Any) -> requests.Response:
        if "storage.example" in url:
            raise requests.Timeout("https://storage.example/?private-signature")
        return _response({"code": 0, "data": {"state": "done", "resultJsonUrl": "https://storage.example/result"}})

    monkeypatch.setattr(paddle_module.requests, "get", get)
    with pytest.raises(RuntimeError, match="result download request failed: Timeout") as caught:
        PaddleOCRParser()._send_request(b"pdf", _config(), None)
    assert "private-signature" not in str(caught.value)
    assert caught.value.__suppress_context__
