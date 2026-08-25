"""Unit tests for the independent managed Channel HTTP clients."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from api.channel_runtime.schemas import RuntimeBindingConfig, RuntimeCredential
from api.channels import runtime_client as runtime_client_module
from api.channels.agent_bridge import AgentExecutionError, AgentReply
from api.channels.core.base import (
    ChannelFormAction,
    IncomingIdentityAssertion,
    IncomingIdentityIdentifier,
)
from api.channels.core.reply import truncate_answer
from api.channels.execution_events import (
    ExecutionFailedEvent,
    InteractionRequiredEvent,
    MessageCompletedEvent,
    MessageDeltaEvent,
)
from api.channels.identity_events import (
    ChannelIdentityEvent,
    ChannelIdentityIdentifier,
    ChannelIdentitySubject,
)
from api.channels.interaction_models import ClaimedInteractionDelivery
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
async def test_runtime_fetches_sanitized_execution_capabilities_once() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "progressive_reply": True,
                "cancel_queued": True,
                "cancel_running": True,
                "regenerate": True,
                "retry": True,
                "feedback": False,
                "identity_event_receipt": True,
                "interaction_delivery": True,
                "future_additive_capability": True,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ChannelRuntimeClient(
            base_url="http://multirag.local",
            api_token="runtime-token",
            runner_id="runner-1",
            binding_id="binding/one",
            binding_generation=7,
            client=http_client,
        )
        capabilities = await client.fetch_execution_capabilities("binding/one")

    assert capabilities.regenerate is True
    assert capabilities.retry is True
    assert capabilities.feedback is False
    assert capabilities.identity_event_receipt is True
    assert capabilities.interaction_delivery is True
    assert len(captured) == 1
    assert captured[0].url.path == "/api/v1/internal/channel-bindings/binding/one/execution-capabilities"
    assert captured[0].headers["X-Channel-Binding-Generation"] == "7"


@pytest.mark.asyncio
async def test_runtime_defaults_missing_identity_event_receipt_capability_to_false() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"regenerate": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ChannelRuntimeClient(
            base_url="http://multirag.local",
            api_token="runtime-token",
            runner_id="runner-1",
            binding_id="binding-1",
            binding_generation=7,
            client=http_client,
        )
        capabilities = await client.fetch_execution_capabilities("binding-1")

    assert capabilities.regenerate is True
    assert capabilities.identity_event_receipt is False
    assert capabilities.interaction_delivery is False


@pytest.mark.parametrize(
    "payload",
    [
        {"regenerate": "yes"},
        {"identity_event_receipt": "true"},
        {"interaction_delivery": "true"},
    ],
)
@pytest.mark.asyncio
async def test_runtime_rejects_malformed_execution_capabilities(
    payload: dict[str, object],
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ChannelRuntimeClient(
            base_url="http://multirag.local",
            api_token="runtime-token",
            runner_id="runner-1",
            binding_id="binding-1",
            binding_generation=7,
            client=http_client,
        )
        with pytest.raises(ChannelRuntimeClientError) as captured:
            await client.fetch_execution_capabilities("binding-1")

    assert captured.value.code == "RUNTIME_CAPABILITIES_INVALID"


@pytest.mark.asyncio
async def test_runtime_submits_generation_scoped_identity_event_with_idempotency_key() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    event = ChannelIdentityEvent(
        event_type="contact.user.updated_v3",
        event_id="directory-event-1",
        event_at=datetime(2026, 8, 24, 6, 0, tzinfo=UTC),
        observed_app_id="app-observed",
        observed_tenant_key="tenant-observed",
        subject=ChannelIdentitySubject(
            identifiers=(
                ChannelIdentityIdentifier(kind="open_id", value="ou-observed"),
                ChannelIdentityIdentifier(kind="user_id", value="user-observed"),
            ),
            directory_status="inactive",
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ChannelRuntimeClient(
            base_url="http://multirag.local",
            api_token="binding-token",
            runner_id="runner-1",
            binding_id="binding/one",
            binding_generation=7,
            client=http_client,
        )
        await client.submit_identity_event(event)

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert request.url.raw_path == b"/api/v1/internal/channel-bindings/binding%2Fone/identity-events"
    assert request.headers["Authorization"] == "Bearer binding-token"
    assert request.headers["X-Channel-Binding-Generation"] == "7"
    assert request.headers["Idempotency-Key"] == "directory-event-1"
    payload = json.loads(request.content)
    assert payload == event.model_dump(mode="json")
    assert not {
        "tenant_id",
        "provider_account_id",
        "provider_account_key",
        "provider_account_revision",
        "binding_generation",
    } & _all_mapping_keys(payload)


@pytest.mark.asyncio
async def test_runtime_refuses_identity_event_without_binding_scope() -> None:
    event = ChannelIdentityEvent(
        event_type="contact.scope.updated_v3",
        event_id="scope-event-1",
        event_at=datetime(2026, 8, 24, 6, 0, tzinfo=UTC),
        observed_app_id="app-observed",
        observed_tenant_key="tenant-observed",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(204))) as http_client:
        client = ChannelRuntimeClient(
            base_url="http://multirag.local",
            api_token="control-token",
            runner_id="runner-1",
            client=http_client,
        )
        with pytest.raises(ChannelRuntimeClientError) as captured:
            await client.submit_identity_event(event)

    assert captured.value.code == "RUNTIME_BINDING_SCOPE_MISMATCH"


@pytest.mark.parametrize(
    "protected_header",
    ["Authorization", "x-channel-binding-generation"],
)
async def test_runtime_extra_headers_cannot_override_workload_authority(
    protected_header: str,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ChannelRuntimeClient(
            base_url="http://multirag.local",
            api_token="binding-token",
            runner_id="runner-1",
            binding_id="binding-1",
            binding_generation=7,
            client=http_client,
        )
        with pytest.raises(ChannelRuntimeClientError) as captured:
            await client._request(
                "POST",
                "http://multirag.local/private",
                extra_headers={protected_header: "attacker-value"},
                expected_status=204,
            )

    assert captured.value.code == "RUNTIME_HEADER_OVERRIDE"
    assert requests == []


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
async def test_legacy_binding_execution_payload_is_byte_for_byte_unchanged() -> None:
    captured: list[bytes] = []
    sse = 'data:{"event":"message_delta","content":"answer","session_id":"session-server"}\n\ndata:{"event":"message_completed","session_id":"session-server"}\n\ndata:[DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content)
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MultiRAGBindingExecutionClient(
            base_url="http://multirag.local",
            binding_id="binding-1",
            binding_generation=7,
            api_token="runtime-token",
            client=http_client,
        )
        events = [
            event
            async for event in client.stream(
                question="legacy question",
                event_id="legacy-event",
                conversation_key="legacy-conversation-key",
                provider="feishu",
                subject="ou-legacy",
                conversation="oc-legacy",
                identity=None,
            )
        ]

    assert events[-1] == MessageCompletedEvent(session_id="session-server")
    assert captured == [
        b'{"event_id":"legacy-event","conversation_key":"legacy-conversation-key",'
        b'"message":{"type":"text","content":"legacy question"},'
        b'"actor":{"provider":"feishu","subject":"ou-legacy","conversation":"oc-legacy"}}'
    ]


@pytest.mark.asyncio
async def test_binding_execution_maps_structured_identity_without_logging_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_values = (
        "tenant-sensitive",
        "open-sensitive",
        "user-sensitive",
        "union-sensitive",
    )
    identity = IncomingIdentityAssertion(
        provider="feishu",
        provider_tenant_key=sensitive_values[0],
        identifiers=(
            IncomingIdentityIdentifier(kind="open_id", value=sensitive_values[1]),
            IncomingIdentityIdentifier(kind="user_id", value=sensitive_values[2]),
            IncomingIdentityIdentifier(kind="union_id", value=sensitive_values[3]),
        ),
    )
    captured: list[dict[str, object]] = []
    sse = 'data:{"event":"message_delta","content":"answer","session_id":"session-server"}\n\ndata:{"event":"message_completed","session_id":"session-server"}\n\ndata:[DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MultiRAGBindingExecutionClient(
            base_url="http://multirag.local",
            binding_id="binding-1",
            binding_generation=7,
            api_token="runtime-token",
            client=http_client,
        )
        events = [
            event
            async for event in client.stream(
                question="identity question",
                event_id="identity-event",
                conversation_key="identity-conversation",
                provider="feishu",
                subject="legacy-subject",
                conversation="legacy-conversation",
                identity=identity,
            )
        ]

    assert events[-1] == MessageCompletedEvent(session_id="session-server")
    assert captured[0]["actor"] == {
        "provider": "feishu",
        "subject": "legacy-subject",
        "conversation": "legacy-conversation",
        "identity": {
            "provider": "feishu",
            "provider_tenant_key": "tenant-sensitive",
            "identifiers": [
                {"kind": "open_id", "value": "open-sensitive"},
                {"kind": "user_id", "value": "user-sensitive"},
                {"kind": "union_id", "value": "union-sensitive"},
            ],
        },
    }
    for sensitive in sensitive_values:
        assert sensitive not in caplog.text


@pytest.mark.asyncio
async def test_binding_execution_sends_regenerate_only_for_explicit_action() -> None:
    captured: list[dict[str, object]] = []
    sse = 'data:{"event":"message_delta","content":"answer","session_id":"session-server"}\n\ndata:{"event":"message_completed","session_id":"session-server"}\n\ndata:[DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MultiRAGBindingExecutionClient(
            base_url="http://multirag.local",
            binding_id="binding-1",
            binding_generation=7,
            api_token="runtime-token",
            client=http_client,
        )
        events = [
            event
            async for event in client.stream(
                question="same question",
                event_id="action:regenerate-event",
                conversation_key="conversation-key",
                provider="feishu",
                subject="ou-user",
                conversation="oc-chat",
                operation="regenerate",
            )
        ]
        retry_events = [
            event
            async for event in client.stream(
                question="failed question",
                event_id="action:retry-event",
                conversation_key="conversation-key",
                provider="feishu",
                subject="ou-user",
                conversation="oc-chat",
                operation="message",
            )
        ]

    assert events[-1] == MessageCompletedEvent(session_id="session-server")
    assert retry_events[-1] == MessageCompletedEvent(session_id="session-server")
    assert [body["operation"] for body in captured] == ["regenerate", "message"]


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


def test_runtime_credential_is_a_generic_map_with_no_provider_named_in_it() -> None:
    """What CHN-P4 → CHN-P8 → CHN-P11 was for.

    The credential model used to carry ``app_id``/``app_secret`` — Feishu's
    names, in a model every provider shares. Reaching for them was how the
    coupling would have grown back, so the three-step removal ended by making
    them unreachable rather than merely unused.
    """

    credential = RuntimeCredential.model_validate({"fields": {"app_id": "cli_aaaa", "app_secret": "aaaa-aaaa"}})
    assert credential.value("app_id") == "cli_aaaa"
    assert credential.value("app_secret") == "aaaa-aaaa"
    # A second provider's names are not special-cased anywhere; they are just
    # other keys in the same map.
    dingtalk = RuntimeCredential.model_validate({"fields": {"client_id": "ding_aaaa", "client_secret": "aaaa-aaaa"}})
    assert dingtalk.value("client_id") == "ding_aaaa"

    # Absent and blank collapse to the same falsy answer on purpose; each
    # provider raises its own classified error rather than distinguishing them.
    assert credential.value("client_id") == ""
    assert RuntimeCredential.model_validate({"fields": {"app_id": ""}}).value("app_id") == ""


def test_runtime_credential_refuses_the_deleted_legacy_pair() -> None:
    """``extra="forbid"`` is what makes the deletion real.

    An old API still emitting the legacy pair must fail the parse loudly rather
    than have it silently ignored — that rejection is precisely the signal the
    deployment order exists to avoid producing, and CHN-ADR-06's three steps
    are what earn the right to it.
    """

    with pytest.raises(ValidationError):
        RuntimeCredential.model_validate({"app_id": "cli_aaaa", "app_secret": "aaaa-aaaa", "fields": {}})


def _binding_config(policy: object | None = None) -> RuntimeBindingConfig:
    payload = {
        "binding_id": "binding-1",
        "provider": "feishu",
        "generation": 1,
        "public_config": {},
        "credential": {"fields": {"app_id": "cli_aaaa", "app_secret": "aaaa-aaaa"}},
    }
    if policy is not None:
        payload["policy"] = policy
    return RuntimeBindingConfig.model_validate(payload)


def test_runtime_binding_config_tolerates_a_policy_the_api_does_not_send_yet() -> None:
    """The tolerate step of CHN-O2 → CHN-O3.

    A worker running this build has to parse what today's API sends (no policy
    at all) and what tomorrow's will, because the two are deployed separately.
    See CHN-ADR-06.
    """

    # Today's payload. Absent policy must mean today's behaviour, not a new one.
    assert _binding_config().policy == {}
    assert _binding_config().private_chat_only is True

    assert _binding_config({"private_chat_only": False}).private_chat_only is False
    assert _binding_config({"private_chat_only": True}).private_chat_only is True

    # Unknown keys ride along: the policy column is free-form by design, and a
    # worker that rejected an unrecognised key would turn any future toggle
    # into a fleet-wide outage rather than an ignored field.
    forward = _binding_config({"private_chat_only": False, "locale": "zh-CN"})
    assert forward.private_chat_only is False
    assert forward.policy["locale"] == "zh-CN"


def test_a_malformed_policy_never_widens_where_a_bot_answers() -> None:
    # Fail safe in the direction that matters: the failure mode of guessing
    # wrong is a bot that starts answering in every group chat it sits in.
    for broken in ({"private_chat_only": "false"}, {"private_chat_only": None}, {"private_chat_only": 0}):
        assert _binding_config(broken).private_chat_only is True


_STREAM_ARGUMENTS = {
    "question": "private-question",
    "event_id": "event-1",
    "conversation_key": "opaque-conversation-key",
    "provider": "feishu",
    "subject": "ou-user",
    "conversation": "oc-chat",
    "identity": None,
}


async def _collect_execution_stream(
    client: MultiRAGBindingExecutionClient,
) -> list[MessageDeltaEvent | MessageCompletedEvent | ExecutionFailedEvent | InteractionRequiredEvent]:
    return [event async for event in client.stream(**_STREAM_ARGUMENTS)]


def _execution_client(
    http_client: httpx.AsyncClient,
    *,
    max_answer_chars: int = 4000,
    total_timeout_seconds: float = 120.0,
) -> MultiRAGBindingExecutionClient:
    return MultiRAGBindingExecutionClient(
        base_url="http://multirag.local",
        binding_id="binding-1",
        binding_generation=3,
        api_token="runtime-token",
        max_answer_chars=max_answer_chars,
        total_timeout_seconds=total_timeout_seconds,
        client=http_client,
    )


@pytest.mark.asyncio
async def test_execution_stream_yields_ordered_typed_events_and_ignores_additive_frames() -> None:
    sse = (
        ": keepalive\n\n"
        "event: ignored-by-data-parser\n"
        "data:\n\n"
        'data:{"event":"status_changed","status":"retrieving","card_id":"provider-private"}\n\n'
        'data:{"event":"message_delta","content":"Hello ","session_id":"session-server"}\n\n'
        'data:{"event":"message_delta","content":"world","session_id":"session-server"}\n\n'
        'data:{"event":"message_completed","session_id":"session-server"}\n\n'
        "data:[DONE]\n\n"
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        events = await _collect_execution_stream(_execution_client(http_client))

    assert events == [
        MessageDeltaEvent(content="Hello ", session_id="session-server"),
        MessageDeltaEvent(content="world", session_id="session-server"),
        MessageCompletedEvent(session_id="session-server"),
    ]
    assert not any(hasattr(event, field) for event in events for field in ("card_id", "message_id", "sequence"))


@pytest.mark.asyncio
async def test_execution_stream_sends_presentation_ref_and_parses_strict_interaction_terminal() -> None:
    captured: list[dict[str, object]] = []
    expires_at = datetime(2026, 8, 24, 12, tzinfo=UTC)
    sse = (
        "data:"
        + json.dumps(
            {
                "event": "interaction_required",
                "action_id": "interaction-1",
                "revision": 2,
                "expires_at": expires_at.isoformat(),
            }
        )
        + "\n\ndata:[DONE]\n\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            text=sse,
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = _execution_client(http_client)
        events = [
            event
            async for event in client.stream(
                **_STREAM_ARGUMENTS,
                presentation_ref="reply-message-1",
            )
        ]

    assert captured[0]["presentation_ref"] == "reply-message-1"
    assert events == [
        InteractionRequiredEvent(
            action_id="interaction-1",
            revision=2,
            expires_at=expires_at,
        )
    ]


@pytest.mark.parametrize(
    "interaction_payload",
    [
        {
            "event": "interaction_required",
            "action_id": "interaction-1",
            "revision": 2,
        },
        {
            "event": "interaction_required",
            "action_id": "interaction-1",
            "revision": True,
            "expires_at": "2026-08-24T12:00:00+00:00",
        },
        {
            "event": "interaction_required",
            "action_id": "interaction-1",
            "revision": 2,
            "expires_at": "2026-08-24T12:00:00",
        },
        {
            "event": "interaction_required",
            "action_id": "interaction-1",
            "revision": 2,
            "expires_at": "2026-08-24T12:00:00+00:00",
            "provider_payload": "must-not-cross",
        },
    ],
    ids=["missing-expiry", "boolean-revision", "naive-expiry", "extra-field"],
)
@pytest.mark.asyncio
async def test_execution_stream_rejects_malformed_interaction_terminal(
    interaction_payload: dict[str, object],
) -> None:
    sse = f"data:{json.dumps(interaction_payload)}\n\ndata:[DONE]\n\n"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=sse,
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(AgentExecutionError) as captured:
            await _collect_execution_stream(_execution_client(http_client))

    assert captured.value.code == "CHANNEL_EXECUTION_INVALID_SSE"


@pytest.mark.asyncio
async def test_execution_stream_uses_authoritative_terminal_snapshot() -> None:
    sse = (
        'data:{"event":"message_delta","content":"foo ","session_id":"session-server"}\n\n'
        'data:{"event":"message_delta","content":"bar baz","session_id":"session-server"}\n\n'
        'data:{"event":"message_completed","content":"foo bar ##0$$ baz","session_id":"session-server"}\n\n'
        "data:[DONE]\n\n"
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = _execution_client(http_client)
        events = await _collect_execution_stream(client)

    assert events == [
        MessageDeltaEvent(content="foo ", session_id="session-server"),
        MessageDeltaEvent(content="bar baz", session_id="session-server"),
        MessageCompletedEvent(
            session_id="session-server",
            content="foo bar ##0$$ baz",
        ),
    ]


@pytest.mark.asyncio
async def test_aggregated_reply_prefers_authoritative_terminal_snapshot() -> None:
    sse = (
        'data:{"event":"message_delta","content":"foo bar baz","session_id":"session-server"}\n\n'
        'data:{"event":"message_completed","content":"foo bar ##0$$ baz","session_id":"session-server"}\n\n'
        "data:[DONE]\n\n"
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        reply = await _execution_client(http_client).ask(
            question="question",
            event_id="event-1",
            conversation_key="test",
            provider="feishu",
            subject="ou-user",
            conversation="oc-chat",
        )

    assert reply == AgentReply(
        content="foo bar ##0$$ baz",
        session_id="session-server",
    )


@pytest.mark.parametrize(
    ("sse", "expected_code"),
    [
        ('data:{"event":"message_delta","content":"answer"}\n\ndata:[DONE]\n\n', "CHANNEL_EXECUTION_INCOMPLETE"),
        (
            'data:{"event":"message_delta","content":"answer","session_id":"session-server"}\n\ndata:{"event":"message_completed","session_id":"session-server"}\n\n',
            "CHANNEL_EXECUTION_INCOMPLETE",
        ),
        ("data:{not-json}\n\n", "CHANNEL_EXECUTION_INVALID_SSE"),
        ("data:[]\n\n", "CHANNEL_EXECUTION_INVALID_SSE"),
        (
            'data:{"event":"message_delta","content":42}\n\ndata:[DONE]\n\n',
            "CHANNEL_EXECUTION_INVALID_SSE",
        ),
        (
            'data:{"event":"message_delta","content":"answer","session_id":"session-server"}\n\ndata:{"event":"message_completed","content":42,"session_id":"session-server"}\n\ndata:[DONE]\n\n',
            "CHANNEL_EXECUTION_INVALID_SSE",
        ),
        (
            'data:{"event":"message_delta","content":"answer","session_id":"session-server"}\n\ndata:{"event":"message_completed","content":"<think>private</think>","session_id":"session-server"}\n\ndata:[DONE]\n\n',
            "CHANNEL_EXECUTION_EMPTY",
        ),
        ('data:{"event":"message_completed"}\n\ndata:[DONE]\n\n', "CHANNEL_EXECUTION_INCOMPLETE"),
    ],
    ids=[
        "missing-completed",
        "missing-done",
        "invalid-json",
        "non-object-json",
        "invalid-known-event",
        "invalid-completed-snapshot",
        "reasoning-only-completed-snapshot",
        "completed-without-session",
    ],
)
@pytest.mark.asyncio
async def test_execution_stream_rejects_incomplete_or_invalid_sse(sse: str, expected_code: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(AgentExecutionError) as captured:
            await _collect_execution_stream(_execution_client(http_client))

    assert captured.value.code == expected_code


@pytest.mark.parametrize(
    ("wire_code", "safe_code"),
    [
        ("TARGET_EXECUTION_FAILED", "TARGET_EXECUTION_FAILED"),
        ("unsafe error=secret", "FAILED"),
        (42, "FAILED"),
    ],
)
@pytest.mark.asyncio
async def test_execution_stream_exposes_only_safe_execution_failure_codes(
    wire_code: object,
    safe_code: str,
) -> None:
    payload = json.dumps({"event": "execution_failed", "error_code": wire_code})
    sse = f"data:{payload}\n\ndata:[DONE]\n\n"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        events = await _collect_execution_stream(_execution_client(http_client))

    assert events == [ExecutionFailedEvent(error_code=safe_code)]
    assert "secret" not in repr(events)


@pytest.mark.parametrize(
    ("exception_type", "expected_code"),
    [
        (httpx.ConnectError, "CHANNEL_EXECUTION_TRANSPORT"),
        (httpx.ReadTimeout, "CHANNEL_EXECUTION_TIMEOUT"),
    ],
)
@pytest.mark.asyncio
async def test_execution_stream_classifies_transport_and_timeout_failures(
    exception_type: type[httpx.HTTPError],
    expected_code: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise exception_type("private transport detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(AgentExecutionError) as captured:
            await _collect_execution_stream(_execution_client(http_client))

    assert captured.value.code == expected_code
    assert "private transport detail" not in str(captured.value)


@pytest.mark.asyncio
async def test_execution_stream_enforces_its_total_timeout() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, text="data:[DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(AgentExecutionError) as captured:
            await _collect_execution_stream(_execution_client(http_client, total_timeout_seconds=0.001))

    assert captured.value.code == "CHANNEL_EXECUTION_TIMEOUT"


def _claimed_form_payload() -> dict[str, object]:
    return {
        "delivery_id": "delivery-1",
        "delivery_token": "delivery-token-1234",
        "action_id": "interaction-1",
        "revision": 1,
        "kind": "form",
        "presentation_ref": "reply-message-1",
        "projection": {
            "message": "Please provide the missing value.",
            "fields": [
                {
                    "name": "field-1",
                    "kind": "text",
                    "label": "Name",
                    "required": True,
                    "options": [],
                    "min_length": 0,
                    "max_length": 100,
                    "minimum": None,
                    "maximum": None,
                }
            ],
        },
        "action_nonce": "action-nonce-1234",
        "expires_at": (datetime(2026, 8, 24, 12, tzinfo=UTC) + timedelta(minutes=5)).isoformat(),
        "safe_error_code": None,
    }


@pytest.mark.asyncio
async def test_interaction_delivery_claim_and_ack_are_generation_scoped_and_fenced() -> None:
    captured: list[tuple[str, str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append((request.method, request.url.path, payload))
        assert request.headers["X-Channel-Binding-Generation"] == "3"
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json=_claimed_form_payload())
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = _execution_client(http_client)
        delivery = await client.claim_interaction_delivery(
            owner="worker-owner",
            action_id="interaction-1",
            revision=1,
        )
        assert isinstance(delivery, ClaimedInteractionDelivery)
        await client.acknowledge_interaction_delivery(
            delivery=delivery,
            owner="worker-owner",
            success=True,
        )

    assert captured == [
        (
            "POST",
            "/api/v1/internal/channel-bindings/binding-1/interaction-deliveries/claim",
            {
                "owner": "worker-owner",
                "action_id": "interaction-1",
                "revision": 1,
            },
        ),
        (
            "POST",
            "/api/v1/internal/channel-bindings/binding-1/interaction-deliveries/delivery-1/ack",
            {
                "owner": "worker-owner",
                "delivery_token": "delivery-token-1234",
                "success": True,
            },
        ),
    ]


def _form_action() -> ChannelFormAction:
    return ChannelFormAction(
        action_id="interaction-1",
        nonce="action-nonce-1234",
        revision=1,
        action="accept",
        form_value={"field-1": "Ada", "field-2": ["option-1", "option-2"]},
        identity=IncomingIdentityAssertion(
            provider="feishu",
            provider_tenant_key="tenant-key",
            identifiers=(
                IncomingIdentityIdentifier(kind="open_id", value="ou-user"),
                IncomingIdentityIdentifier(kind="user_id", value="user-1"),
            ),
        ),
        chat_id="oc-chat",
        message_id="reply-message-1",
        event_id="callback-event-1",
    )


@pytest.mark.asyncio
async def test_interaction_callback_waits_for_durable_receipt_and_sends_bounded_identity() -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.url.path, json.loads(request.content)))
        return httpx.Response(202, json={"status": "accepted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        status = await _execution_client(http_client).receive_interaction_callback(
            _form_action(),
        )

    assert status == "accepted"
    assert captured == [
        (
            "/api/v1/internal/channel-bindings/binding-1/interactions/interaction-1/revisions/1/callbacks",
            {
                "event_id": "callback-event-1",
                "nonce": "action-nonce-1234",
                "action": "accept",
                "message_id": "reply-message-1",
                "actor": {
                    "provider": "feishu",
                    "subject": "ou-user",
                    "conversation": "oc-chat",
                    "identity": {
                        "provider": "feishu",
                        "provider_tenant_key": "tenant-key",
                        "identifiers": [
                            {"kind": "open_id", "value": "ou-user"},
                            {"kind": "user_id", "value": "user-1"},
                        ],
                    },
                },
                "form_value": {
                    "field-1": "Ada",
                    "field-2": ["option-1", "option-2"],
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_interaction_callback_has_a_deadline_for_durable_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_client_module,
        "_CALLBACK_RECEIPT_TIMEOUT_SECONDS",
        0.001,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(202, json={"status": "accepted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(AgentExecutionError) as captured:
            await _execution_client(http_client).receive_interaction_callback(
                _form_action(),
            )

    assert captured.value.code == "CHANNEL_INTERACTION_RECEIPT_TIMEOUT"


class _InterruptedSSEBody(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data:{"event":"message_delta","content":"partial"}\n\n'
        raise httpx.ReadError("private interrupted response detail")


@pytest.mark.asyncio
async def test_execution_stream_classifies_a_connection_interrupted_after_a_delta() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_InterruptedSSEBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(AgentExecutionError) as captured:
            await _collect_execution_stream(_execution_client(http_client))

    assert captured.value.code == "CHANNEL_EXECUTION_TRANSPORT"


@pytest.mark.asyncio
async def test_execution_stream_filters_reasoning_markers_across_delta_boundaries() -> None:
    sse = (
        'data:{"event":"message_delta","content":"visible<thi","session_id":"session-server"}\n\n'
        'data:{"event":"message_delta","content":"nk>private reasoning</th","session_id":"session-server"}\n\n'
        'data:{"event":"message_delta","content":"ink> answer","session_id":"session-server"}\n\n'
        'data:{"event":"message_completed","session_id":"session-server"}\n\n'
        "data:[DONE]\n\n"
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        events = await _collect_execution_stream(_execution_client(http_client))

    visible = "".join(event.content for event in events if isinstance(event, MessageDeltaEvent))
    assert visible == "visible answer"
    assert "private reasoning" not in visible
    assert "think" not in visible.lower()


@pytest.mark.asyncio
async def test_ask_aggregates_stream_deltas_with_the_established_limit() -> None:
    answer = "a" * 45 + "b" * 45
    sse = (
        f'data:{{"event":"message_delta","content":"{answer[:30]}","session_id":"session-server"}}\n\n'
        f'data:{{"event":"message_delta","content":"{answer[30:60]}","session_id":"session-server"}}\n\n'
        f'data:{{"event":"message_delta","content":"{answer[60:]}","session_id":"session-server"}}\n\n'
        'data:{"event":"message_completed","session_id":"session-server"}\n\n'
        "data:[DONE]\n\n"
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        reply = await _execution_client(http_client, max_answer_chars=60).ask(**_STREAM_ARGUMENTS)

    assert reply == AgentReply(
        content=truncate_answer(answer, 60),
        session_id="session-server",
    )


@pytest.mark.asyncio
async def test_ask_preserves_the_legacy_execution_failed_classification() -> None:
    sse = 'data:{"event":"execution_failed","error_code":"TARGET_EXECUTION_FAILED"}\n\ndata:[DONE]\n\n'

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(AgentExecutionError) as captured:
            await _execution_client(http_client).ask(**_STREAM_ARGUMENTS)

    assert captured.value.code == "CHANNEL_EXECUTION_TARGET_EXECUTION_FAILED"


@pytest.mark.asyncio
async def test_ask_is_only_a_thin_consumer_of_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden_handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("ask must not maintain a second HTTP or SSE path")

    captured: dict[str, object] = {}

    async def fake_stream(**kwargs: object) -> AsyncIterator[MessageDeltaEvent | MessageCompletedEvent]:
        captured.update(kwargs)
        yield MessageDeltaEvent(content="answer ", session_id="session-stream")
        yield MessageDeltaEvent(content="from stream", session_id="session-stream")
        yield MessageCompletedEvent(session_id="session-stream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_handler)) as http_client:
        client = _execution_client(http_client)
        monkeypatch.setattr(client, "stream", fake_stream)
        reply = await client.ask(**_STREAM_ARGUMENTS)

    assert captured == _STREAM_ARGUMENTS
    assert reply == AgentReply(content="answer from stream", session_id="session-stream")
