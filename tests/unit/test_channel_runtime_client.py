"""Unit tests for the independent managed Channel HTTP clients."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

import httpx
import pytest

from api.channel_runtime.schemas import RuntimeCredential
from api.channels.agent_bridge import AgentExecutionError, AgentReply
from api.channels.runtime_client import ChannelRuntimeClient, ChannelRuntimeClientError, MultiRAGBindingExecutionClient


def _all_mapping_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return {str(key) for key in value} | set().union(*(_all_mapping_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_all_mapping_keys(item) for item in value), set())
    return set()


@pytest.mark.asyncio
async def test_runtime_client_lists_desired_state_without_exposing_token(caplog: pytest.LogCaptureFixture) -> None:
    token = "runtime-token-that-must-never-appear"
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"items": [{"binding_id": "binding-1", "provider": "feishu", "generation": 3}]},
        )

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ChannelRuntimeClient(
            base_url="http://multirag.local/",
            api_token=token,
            runner_id="runner-1",
            client=http_client,
        )
        desired = await client.list_desired()

        assert token not in repr(client)

    assert [item.model_dump() for item in desired] == [{"binding_id": "binding-1", "provider": "feishu", "generation": 3}]
    assert len(captured) == 1
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/api/v1/internal/channel-runtimes/desired"
    assert captured[0].headers["Authorization"] == f"Bearer {token}"
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_runtime_report_has_a_narrow_body_and_drops_unsafe_error_code() -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ChannelRuntimeClient(
            base_url="http://multirag.local",
            api_token="runtime-token",
            runner_id="runner-1",
            client=http_client,
        )
        await client.report(
            binding_id="binding/with space",
            generation=7,
            state="error",
            error_code="unsafe error with credential=secret",
        )

    assert captured == [
        {
            "observed_generation": 7,
            "state": "error",
            "runner_id": "runner-1",
            "connected_at": None,
            "last_error_code": None,
        }
    ]
    assert not {
        "tenant_id",
        "target_id",
        "target_type",
        "revision_id",
        "target_revision_id",
        "session_id",
    } & _all_mapping_keys(captured[0])


@pytest.mark.asyncio
async def test_binding_execution_request_cannot_override_trusted_context(caplog: pytest.LogCaptureFixture) -> None:
    api_token = "execution-token-that-must-never-appear"
    question = "question-that-must-never-be-logged"
    captured: dict[str, object] = {}
    sse = 'data:{"event":"message_delta","content":"answer","session_id":"session-server"}\n\ndata:{"event":"message_completed","session_id":"session-server"}\n\ndata:[DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["raw_path"] = request.url.raw_path.decode("ascii")
        captured["authorization"] = request.headers.get("Authorization")
        captured["binding_generation"] = request.headers.get("X-Channel-Binding-Generation")
        captured["idempotency_key"] = request.headers.get("Idempotency-Key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MultiRAGBindingExecutionClient(
            base_url="http://multirag.local",
            binding_id="binding/one",
            binding_generation=7,
            api_token=api_token,
            client=http_client,
        )
        reply = await client.ask(
            question=question,
            event_id="event-1",
            conversation_key="opaque-conversation-key",
            provider="feishu",
            subject="ou-user",
            conversation="oc-chat",
        )

        assert api_token not in repr(client)

    assert reply == AgentReply(content="answer", session_id="session-server")
    assert captured == {
        "method": "POST",
        "path": "/api/v1/internal/channel-bindings/binding/one/executions",
        "raw_path": "/api/v1/internal/channel-bindings/binding%2Fone/executions",
        "authorization": f"Bearer {api_token}",
        "binding_generation": "7",
        "idempotency_key": "event-1",
        "body": {
            "event_id": "event-1",
            "conversation_key": "opaque-conversation-key",
            "message": {"type": "text", "content": question},
            "actor": {
                "provider": "feishu",
                "subject": "ou-user",
                "conversation": "oc-chat",
            },
        },
    }
    assert not {
        "tenant_id",
        "target_id",
        "target_type",
        "revision_id",
        "target_revision_id",
        "session_id",
        "release",
        "permissions",
    } & _all_mapping_keys(captured["body"])
    assert api_token not in caplog.text
    assert question not in caplog.text


@pytest.mark.asyncio
async def test_binding_execution_failure_exposes_only_classified_code(caplog: pytest.LogCaptureFixture) -> None:
    token = "execution-secret-token"
    response_secret = "upstream-body-secret"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=response_secret)

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MultiRAGBindingExecutionClient(
            base_url="http://multirag.local",
            binding_id="binding-1",
            binding_generation=1,
            api_token=token,
            client=http_client,
        )
        with pytest.raises(AgentExecutionError) as captured:
            await client.ask(
                question="private-question",
                event_id="event-1",
                # 与本文件其他用例统一：短横线加数字的写法（如 conversation-1）香农熵 3.52，
                # 越过 gitleaks generic-api-key 的 3.5 阈值，CI 泄密扫描会把测试假值当密钥拦下。
                conversation_key="opaque-conversation-key",
                provider="feishu",
                subject="ou-user",
                conversation="oc-chat",
            )

    assert captured.value.code == "CHANNEL_EXECUTION_HTTP_502"
    assert str(captured.value) == "CHANNEL_EXECUTION_HTTP_502"
    assert token not in repr(captured.value)
    assert response_secret not in repr(captured.value)
    assert token not in caplog.text
    assert response_secret not in caplog.text


@pytest.mark.asyncio
async def test_runtime_client_http_failure_does_not_include_response_or_token() -> None:
    token = "runtime-secret-token"
    response_secret = "private-control-response"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=response_secret)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ChannelRuntimeClient(
            base_url="http://multirag.local",
            api_token=token,
            runner_id="runner-1",
            client=http_client,
        )
        with pytest.raises(ChannelRuntimeClientError) as captured:
            await client.list_desired()

    assert captured.value.code == "RUNTIME_API_HTTP_503"
    assert token not in repr(captured.value)
    assert response_secret not in repr(captured.value)


def test_runtime_credential_tolerates_both_contract_halves() -> None:
    """The tolerate step of CHN-P4 → CHN-P8.

    A worker running this build has to parse what today's API sends (the legacy
    pair only) and what tomorrow's will (a generic ``fields`` map), because the
    two are deployed separately — the supervisor is a long-lived process that an
    API deploy does not restart. See CHN-ADR-06.
    """

    legacy = RuntimeCredential.model_validate({"app_id": "cli_aaaa", "app_secret": "aaaa-aaaa"})
    assert legacy.value("app_id", legacy=legacy.app_id) == "cli_aaaa"
    assert legacy.value("app_secret", legacy=legacy.app_secret) == "aaaa-aaaa"

    both = RuntimeCredential.model_validate(
        {
            "app_id": "cli_aaaa",
            "app_secret": "aaaa-aaaa",
            "fields": {"app_id": "cli_bbbb", "app_secret": "bbbb-bbbb"},
        }
    )
    # The generic map wins once it is populated; the legacy pair is only a
    # fallback for the window where the API has not caught up.
    assert both.value("app_id", legacy=both.app_id) == "cli_bbbb"
    assert both.value("app_secret", legacy=both.app_secret) == "bbbb-bbbb"

    # An unknown key is not an error here: enable-time checks report a missing
    # credential, this only reads one.
    assert both.value("client_id") == ""
