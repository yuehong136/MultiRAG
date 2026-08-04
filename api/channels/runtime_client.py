"""HTTP clients used by the independent Channel supervisor and workers."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from api.channel_runtime.schemas import DesiredRuntime, DesiredRuntimeList, RuntimeBindingConfig, RuntimeState
from api.channels.agent_bridge import AgentExecutionError, AgentReply, _strip_reasoning, _truncate_answer

_SAFE_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")


class ChannelRuntimeClientError(RuntimeError):
    """A classified private-control API failure without response details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ChannelRuntimeClient:
    """Control client shared by the supervisor and one managed worker."""

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        runner_id: str,
        binding_id: str | None = None,
        binding_generation: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if (binding_id is None) != (binding_generation is None):
            raise ValueError("binding_id and binding_generation must be configured together")
        if binding_generation is not None and binding_generation < 1:
            raise ValueError("channel binding generation must be positive")
        self._base_url = base_url.rstrip("/")
        self._runner_id = runner_id
        self._headers = {"Authorization": f"Bearer {api_token}"}
        self._binding_id = binding_id
        self._binding_generation = binding_generation
        if binding_generation is not None:
            self._headers["X-Channel-Binding-Generation"] = str(binding_generation)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5, read=30, write=5, pool=5),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_desired(self) -> list[DesiredRuntime]:
        response = await self._request(
            "GET",
            f"{self._base_url}/api/v1/internal/channel-runtimes/desired",
        )
        try:
            return DesiredRuntimeList.model_validate(response.json()).items
        except (ValueError, TypeError) as exc:
            raise ChannelRuntimeClientError("RUNTIME_DESIRED_INVALID") from exc

    async def fetch_binding(self, binding_id: str) -> RuntimeBindingConfig:
        if self._binding_id is not None and binding_id != self._binding_id:
            raise ChannelRuntimeClientError("RUNTIME_BINDING_SCOPE_MISMATCH")
        encoded = quote(binding_id, safe="")
        response = await self._request(
            "GET",
            f"{self._base_url}/api/v1/internal/channel-bindings/{encoded}/runtime-config",
        )
        try:
            return RuntimeBindingConfig.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise ChannelRuntimeClientError("RUNTIME_CONFIG_INVALID") from exc

    async def report(
        self,
        *,
        binding_id: str,
        generation: int,
        state: RuntimeState,
        connected_at: datetime | None = None,
        error_code: str | None = None,
    ) -> None:
        if self._binding_id is not None and (binding_id != self._binding_id or generation != self._binding_generation):
            raise ChannelRuntimeClientError("RUNTIME_BINDING_SCOPE_MISMATCH")
        encoded = quote(binding_id, safe="")
        payload = {
            "observed_generation": generation,
            "state": state,
            "runner_id": self._runner_id,
            "connected_at": (connected_at or datetime.now(UTC)).isoformat() if state == "connected" else None,
            "last_error_code": error_code if error_code and _SAFE_ERROR_CODE.fullmatch(error_code) else None,
        }
        await self._request(
            "PUT",
            f"{self._base_url}/api/v1/internal/channel-bindings/{encoded}/runtime-status",
            json=payload,
            expected_status=status_code_no_content(),
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, object] | None = None,
        expected_status: int = 200,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                url,
                headers=self._headers,
                json=json,
            )
        except httpx.TimeoutException as exc:
            raise ChannelRuntimeClientError("RUNTIME_API_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise ChannelRuntimeClientError("RUNTIME_API_TRANSPORT") from exc
        if response.status_code != expected_status:
            raise ChannelRuntimeClientError(f"RUNTIME_API_HTTP_{response.status_code}")
        return response


def status_code_no_content() -> int:
    """Keep the expected status explicit without importing a web framework."""

    return 204


class MultiRAGBindingExecutionClient:
    """Execute one trusted binding through MultiRAG's private SSE boundary."""

    def __init__(
        self,
        *,
        base_url: str,
        binding_id: str,
        binding_generation: int,
        api_token: str,
        max_answer_chars: int = 4000,
        total_timeout_seconds: float = 120,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if binding_generation < 1:
            raise ValueError("channel binding generation must be positive")
        self._base_url = base_url.rstrip("/")
        self._binding_id = binding_id
        self._max_answer_chars = max_answer_chars
        self._total_timeout_seconds = total_timeout_seconds
        self._owns_client = client is None
        encoded = quote(binding_id, safe="")
        self._execution_endpoint = f"{self._base_url}/api/v1/internal/channel-bindings/{encoded}/executions"
        self._conversation_endpoint = f"{self._base_url}/api/v1/internal/channel-bindings/{encoded}/conversations"
        self._headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "X-Channel-Binding-Generation": str(binding_generation),
        }
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5, read=total_timeout_seconds, write=5, pool=5),
            follow_redirects=False,
            trust_env=False,
        )

    async def preflight(self) -> None:
        """Check API reachability without executing a target or sending a token to ping."""

        try:
            response = await self._client.get(f"{self._base_url}/api/v1/system/ping")
        except httpx.HTTPError as exc:
            raise AgentExecutionError("CHANNEL_PREFLIGHT_TRANSPORT") from exc
        if response.status_code != httpx.codes.OK or response.text.strip().strip('"') != "pong":
            raise AgentExecutionError("CHANNEL_PREFLIGHT_FAILED")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def ask(
        self,
        *,
        question: str,
        event_id: str,
        conversation_key: str,
        provider: str,
        subject: str,
        conversation: str,
    ) -> AgentReply:
        body = {
            "event_id": event_id,
            "conversation_key": conversation_key,
            "message": {"type": "text", "content": question},
            "actor": {
                "provider": provider,
                "subject": subject,
                "conversation": conversation,
            },
        }
        headers = {**self._headers, "Idempotency-Key": event_id}
        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                async with self._client.stream(
                    "POST",
                    self._execution_endpoint,
                    headers=headers,
                    json=body,
                ) as response:
                    if response.status_code != httpx.codes.OK:
                        raise AgentExecutionError(f"CHANNEL_EXECUTION_HTTP_{response.status_code}")
                    return await self._consume_sse(response)
        except AgentExecutionError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise AgentExecutionError("CHANNEL_EXECUTION_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise AgentExecutionError("CHANNEL_EXECUTION_TRANSPORT") from exc

    async def reset(self, *, conversation_key: str) -> None:
        encoded = quote(conversation_key, safe="")
        try:
            response = await self._client.delete(
                f"{self._conversation_endpoint}/{encoded}",
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            raise AgentExecutionError("CHANNEL_RESET_TRANSPORT") from exc
        if response.status_code != httpx.codes.NO_CONTENT:
            raise AgentExecutionError(f"CHANNEL_RESET_HTTP_{response.status_code}")

    async def _consume_sse(self, response: httpx.Response) -> AgentReply:
        chunks: list[str] = []
        session_id = ""
        saw_completed = False
        saw_done = False
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload_text = line[5:].strip()
            if payload_text == "[DONE]":
                saw_done = True
                break
            if not payload_text:
                continue
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError as exc:
                raise AgentExecutionError("CHANNEL_EXECUTION_INVALID_SSE") from exc
            if not isinstance(payload, dict):
                raise AgentExecutionError("CHANNEL_EXECUTION_INVALID_SSE")
            event = payload.get("event")
            raw_session_id = payload.get("session_id")
            if isinstance(raw_session_id, str) and raw_session_id:
                session_id = raw_session_id
            if event == "execution_failed":
                code = payload.get("error_code")
                safe_code = code if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code) else "FAILED"
                raise AgentExecutionError(f"CHANNEL_EXECUTION_{safe_code}")
            if event == "message_delta":
                content = payload.get("content")
                if isinstance(content, str):
                    chunks.append(content)
            elif event == "message_completed":
                saw_completed = True

        if not saw_completed or not saw_done or not session_id:
            raise AgentExecutionError("CHANNEL_EXECUTION_INCOMPLETE")
        content = _strip_reasoning("".join(chunks))
        if not content:
            raise AgentExecutionError("CHANNEL_EXECUTION_EMPTY")
        return AgentReply(
            content=_truncate_answer(content, self._max_answer_chars),
            session_id=session_id,
        )
