"""结构化日志与脱敏测试（方案 §8-3，脱敏为硬验收）。

实证形态：真实 logging 管道（logger → Filter → Formatter → handler 输出），
构造含密钥的日志调用，断言输出已掩码；§1.4 曾在 logs/ 实测 330 处密钥泄露，
本套测试是该问题的防回归门禁。
"""

import io
import json
import logging

import pytest

from common.log_ctx import ContextInjectFilter, bind_log_context, clear_log_context, get_log_context
from common.log_utils import JsonFormatter, SecretMaskingFilter, sanitize_message


@pytest.fixture
def capture_logger():
    """独立 logger + 内存 handler，完整复刻 init_root_logger 的管道结构。"""
    logger = logging.getLogger("test.sanitize")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextInjectFilter())
    handler.addFilter(SecretMaskingFilter())
    logger.addHandler(handler)
    yield logger, stream
    logger.handlers.clear()
    clear_log_context()


class TestSecretMasking:
    """硬验收：含密钥的日志调用，输出必须已掩码。"""

    @pytest.mark.parametrize(
        ("message", "secret"),
        [
            # 假密钥用低熵值：既保持真实形态供正则测试，又不触发 gitleaks 熵检测
            ('api_key="sk-fakefakefakefake0001"', "sk-fakefakefakefake0001"),
            ("password=Datav@12$%deep", "Datav@12$%deep"),
            ('{"token": "eyJhbGciOiJIUzI1NiJ9.payload.sig"}', "eyJhbGciOiJIUzI1NiJ9.payload.sig"),
            ("Authorization: Bearer ya29.a0AfH6SMBxxxxxxxx", "ya29.a0AfH6SMBxxxxxxxx"),
            ("secret_key: 'multirag-super-secret-value'", "multirag-super-secret-value"),
            ("ZHIPU apikey=7ae32940deadbeefcafebabe.IDijUv", "7ae32940deadbeefcafebabe.IDijUv"),
            ("bare key sk-855ec5ff1234567890ab in text", "sk-855ec5ff1234567890ab"),
        ],
    )
    def test_secret_shapes_are_masked_end_to_end(self, capture_logger, message, secret):
        logger, stream = capture_logger

        logger.info("request conf: %s", message)

        output = stream.getvalue()
        assert secret not in output, f"密钥原文泄露到日志: {output}"
        assert "***" in output

    def test_lazy_format_args_are_also_masked(self, capture_logger):
        logger, stream = capture_logger

        logger.info("connecting with password=%s for tenant %s", "Q*PJLQ32fcg!", "t-001")

        output = stream.getvalue()
        assert "Q*PJLQ32fcg!" not in output
        assert "t-001" in output  # 非敏感参数不受影响

    def test_plain_message_untouched(self):
        message = "task 42 finished in 3.2s, 15 chunks"

        assert sanitize_message(message) == message

    def test_mask_keeps_short_prefix_for_correlation(self):
        masked = sanitize_message("api_key=abcdefghijklmn")

        assert "abcd***" in masked
        assert "abcdefghijklmn" not in masked


class TestJsonFormatter:
    def test_output_is_parseable_json_with_fixed_fields(self, capture_logger):
        logger, stream = capture_logger

        logger.warning("something %s", "happened")

        record = json.loads(stream.getvalue())
        assert record["level"] == "WARNING"
        assert record["logger"] == "test.sanitize"
        assert record["message"] == "something happened"
        assert {"ts", "process"} <= record.keys()

    def test_context_fields_injected(self, capture_logger):
        logger, stream = capture_logger
        bind_log_context(request_id="req-123", tenant_id="tenant-9", doc_id="doc-7")

        logger.info("processing")

        record = json.loads(stream.getvalue())
        assert record["request_id"] == "req-123"
        assert record["tenant_id"] == "tenant-9"
        assert record["doc_id"] == "doc-7"

    def test_absent_context_fields_omitted(self, capture_logger):
        logger, stream = capture_logger

        logger.info("no context")

        record = json.loads(stream.getvalue())
        assert "request_id" not in record
        assert "tenant_id" not in record

    def test_exception_info_serialized(self, capture_logger):
        logger, stream = capture_logger

        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")

        record = json.loads(stream.getvalue())
        assert "ValueError: boom" in record["exc_info"]


class TestLogContext:
    def test_bind_partial_keeps_existing(self):
        bind_log_context(request_id="r1")
        bind_log_context(tenant_id="t1")

        assert get_log_context() == {"request_id": "r1", "tenant_id": "t1"}

        clear_log_context()
        assert get_log_context() == {}


class TestRequestIdMiddleware:
    def test_response_carries_generated_request_id(self, client):
        resp = client.get("/api/v1/system/ping")

        assert resp.status_code == 200
        assert resp.headers.get("X-Request-ID")

    def test_inbound_request_id_is_propagated(self, client):
        resp = client.get("/api/v1/system/ping", headers={"X-Request-ID": "trace-me-42"})

        assert resp.headers.get("X-Request-ID") == "trace-me-42"
