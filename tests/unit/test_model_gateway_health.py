"""Background health checks against a controlled upstream and an in-memory store."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from api.model_gateway.catalog import UpstreamTarget
from api.model_gateway.health import HEALTHY, UNHEALTHY, HealthChecker, HealthResult, HealthTarget
from api.model_gateway.router import Router


class Store:
    def __init__(self, targets: list[HealthTarget]) -> None:
        self.targets = targets
        self.recorded: list[HealthResult] = []

    async def health_targets(self) -> list[HealthTarget]:
        return self.targets

    async def record_health(self, results: Sequence[HealthResult]) -> None:
        self.recorded = list(results)


@dataclass(frozen=True)
class Deployment:
    model_id: str
    weight: int = 1
    order: int | None = None


def _target(model_id: str, host: str, model_type: str = "chat", api_key: str = "upstream-key") -> HealthTarget:
    return HealthTarget("tenant-1", model_id, model_type, UpstreamTarget(model_id, "VLLM", f"{model_id}-upstream", f"http://{host}/v1", api_key))


async def _round(handler, targets: list[HealthTarget], router: Router | None = None, **settings: float) -> tuple[Store, list[httpx.Request]]:
    store, seen = Store(targets), []

    async def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result = handler(request)
        return await result if asyncio.iscoroutine(result) else result

    async with httpx.AsyncClient(transport=httpx.MockTransport(recording)) as client:
        await HealthChecker(client, store, router or Router(), interval_seconds=300.0, **settings).run_once()
    return store, seen


def _by_host(answers: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers[request.url.host]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"error": {"message": "broken"}} if answer >= 400 else {"ok": True})

    return handler


async def test_a_chat_model_is_probed_with_one_token_and_an_embedding_model_with_one_input():
    store, seen = await _round(_by_host({"chat.test": 200, "embed.test": 200}), [_target("a", "chat.test"), _target("b", "embed.test", "embedding", api_key="")])

    by_path = {request.url.path: request for request in seen}
    assert json.loads(by_path["/v1/chat/completions"].content) == {"model": "a-upstream", "messages": [{"role": "user", "content": "test"}], "max_tokens": 1}
    assert json.loads(by_path["/v1/embeddings"].content) == {"model": "b-upstream", "input": "test"}
    assert by_path["/v1/chat/completions"].headers["authorization"] == "Bearer upstream-key"
    assert [(result.model_id, result.status, result.status_code, result.error_message) for result in store.recorded] == [("a", HEALTHY, 200, None), ("b", HEALTHY, 200, None)]


async def test_an_upstream_error_an_unreachable_host_and_a_slow_one_are_all_unhealthy():
    async def slow(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slow.test":
            await asyncio.sleep(1)
        return _by_host({"down.test": 500, "gone.test": httpx.ConnectError("refused"), "slow.test": 200})(request)

    store, _ = await _round(slow, [_target("a", "down.test"), _target("b", "gone.test"), _target("c", "slow.test")], timeout_seconds=0.05)

    results = {result.model_id: result for result in store.recorded}
    assert {model_id: result.status for model_id, result in results.items()} == {"a": UNHEALTHY, "b": UNHEALTHY, "c": UNHEALTHY}
    assert (results["a"].status_code, "broken" in (results["a"].error_message or "")) == (500, True)
    assert (results["b"].status_code, results["b"].error_message) == (None, "The model service could not be reached: ConnectError")
    assert (results["c"].status_code, results["c"].error_message) == (None, "The model service did not respond in time.")


async def test_unhealthy_deployments_are_tried_last_until_a_round_finds_them_healthy():
    router = Router()
    deployments = [Deployment("a", order=1), Deployment("b", order=2)]
    targets = [_target("a", "first.test"), _target("b", "second.test")]

    await _round(_by_host({"first.test": 503, "second.test": 200}), targets, router)
    assert [item.model_id for item in router.plan(deployments)] == ["b", "a"]

    await _round(_by_host({"first.test": 200, "second.test": 200}), targets, router)
    assert [item.model_id for item in router.plan(deployments)] == ["a", "b"]


async def test_a_name_whose_deployments_are_all_unhealthy_is_still_served():
    router = Router()
    router.set_unhealthy({"a", "b"})

    assert sorted(item.model_id for item in router.plan([Deployment("a"), Deployment("b")])) == ["a", "b"]


async def test_a_round_without_models_records_nothing():
    store, seen = await _round(_by_host({}), [])

    assert store.recorded == [] and seen == []
