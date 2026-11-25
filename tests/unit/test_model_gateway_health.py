"""Background health checks against a controlled upstream and an in-memory store."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
import pytest

from api.model_gateway.catalog import UpstreamTarget
from api.model_gateway.health import HEALTHY, RATE_LIMITED, UNHEALTHY, HealthChecker, HealthResult, HealthTarget, classify
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


async def test_a_model_that_refuses_max_tokens_is_probed_again_with_max_completion_tokens():
    def newer_openai(request: httpx.Request) -> httpx.Response:
        if "max_tokens" in json.loads(request.content):
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens'"}})
        return httpx.Response(200, json={"ok": True})

    store, seen = await _round(newer_openai, [_target("a", "chat.test")])

    assert [sorted(json.loads(request.content)) for request in seen] == [["max_tokens", "messages", "model"], ["max_completion_tokens", "messages", "model"]]
    assert [(result.status, result.status_code, result.error_message) for result in store.recorded] == [(HEALTHY, 200, None)]


async def test_a_probe_the_upstream_rejects_as_malformed_does_not_make_the_model_unhealthy():
    store, seen = await _round(_by_host({"chat.test": 400, "embed.test": 422}), [_target("a", "chat.test"), _target("b", "embed.test", "embedding")])

    # The upstream is up and accepted the credential; only the probe did not fit.
    assert {result.model_id: (result.status, result.status_code) for result in store.recorded} == {"a": (HEALTHY, 400), "b": (HEALTHY, 422)}
    # Chat is tried both ways; an embedding probe has nothing to vary.
    assert len(seen) == 3


# --- why a model is not healthy, and when it is not probed --------------------


class Minutes:
    """A clock the test moves; the checker and the router read the same one."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Rounds:
    """One checker over several rounds, with a script of answers per host."""

    def __init__(self, targets: list[HealthTarget], answers: dict[str, object], interval: float = 300.0) -> None:
        self.clock, self.answers, self.interval = Minutes(), answers, interval
        self.router = Router(clock=self.clock)
        self.store, self.seen = Store(targets), []
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.checker = HealthChecker(self._client, self.store, self.router, interval_seconds=interval, monotonic=self.clock)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request.url.host)
        answer = self.answers[request.url.host]
        return answer if isinstance(answer, httpx.Response) else httpx.Response(answer, json={"error": {"message": "no"}} if answer >= 400 else {"ok": True})

    async def next(self) -> dict[str, HealthResult]:
        self.seen.clear()
        results = await self.checker.run_once()
        self.clock.now += self.interval
        return {result.model_id: result for result in results}


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (200, "", (HEALTHY, None)),
        (400, "max_tokens is too small", (HEALTHY, None)),
        (429, "You have exceeded your current request limit", (RATE_LIMITED, "rate_limit")),
        # Providers report an empty balance as 429 or 403 as often as 402.
        (429, '{"error":{"code":"insufficient_quota","message":"You exceeded your current quota"}}', (UNHEALTHY, "quota")),
        (403, "Your credit balance is too low to access the API", (UNHEALTHY, "quota")),
        (402, "", (UNHEALTHY, "quota")),
        (401, "Incorrect API key provided", (UNHEALTHY, "credential")),
        (403, "forbidden", (UNHEALTHY, "credential")),
        (404, "Model not exist.", (UNHEALTHY, "model_gone")),
        (408, "", (UNHEALTHY, "timeout")),
        (500, "", (UNHEALTHY, "transient")),
        (503, "overloaded", (UNHEALTHY, "transient")),
    ],
)
def test_an_upstream_answer_is_classified_by_what_it_says_about_the_model(status_code, body, expected):
    assert classify(status_code, body) == expected


async def test_results_say_why_a_model_is_not_healthy():
    async def slow(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slow.test":
            await asyncio.sleep(1)
        return _by_host({"key.test": 401, "gone.test": httpx.ConnectError("refused"), "slow.test": 200})(request)

    store, _ = await _round(slow, [_target("a", "key.test"), _target("b", "gone.test"), _target("c", "slow.test")], timeout_seconds=0.05)

    assert {result.model_id: (result.status, result.failure_class, result.source) for result in store.recorded} == {
        "a": (UNHEALTHY, "credential", "probe"),
        "b": (UNHEALTHY, "unreachable", "probe"),
        "c": (UNHEALTHY, "timeout", "probe"),
    }


async def test_a_rate_limited_model_keeps_its_place_in_routing():
    rounds = Rounds([_target("a", "first.test"), _target("b", "second.test")], {"first.test": 429, "second.test": 200})

    results = await rounds.next()

    assert (results["a"].status, results["a"].failure_class, results["a"].status_code) == (RATE_LIMITED, "rate_limit", 429)
    # Being rate limited passes by itself; only a real failure sends a deployment to the back.
    assert [item.model_id for item in rounds.router.plan([Deployment("a", order=1), Deployment("b", order=2)])] == ["a", "b"]


async def test_a_rate_limited_model_is_left_alone_for_a_round_and_its_result_stands():
    rounds = Rounds([_target("a", "limited.test")], {"limited.test": 429})
    first = await rounds.next()

    second = await rounds.next()

    # No probe was sent, and the page keeps saying what the last probe found.
    assert rounds.seen == [] and second["a"] == first["a"]
    rounds.answers["limited.test"] = 200
    third = await rounds.next()
    assert rounds.seen == ["limited.test"] and third["a"].status == HEALTHY


async def test_the_upstreams_retry_after_is_honoured_up_to_an_hour():
    asked = httpx.Response(429, headers={"retry-after": "1500"}, json={"error": {"message": "slow down"}})
    rounds = Rounds([_target("a", "limited.test")], {"limited.test": asked})
    await rounds.next()

    probed = []
    for _ in range(6):
        await rounds.next()
        probed.append(bool(rounds.seen))

    # 1500 seconds is five rounds of 300; the probe after that is sent again.
    assert probed == [False, False, False, False, True, False]


async def test_a_model_that_served_real_calls_is_not_probed():
    rounds = Rounds([_target("a", "busy.test"), _target("b", "idle.test")], {"busy.test": 200, "idle.test": 200})
    rounds.router.succeeded("a")

    results = await rounds.next()

    assert rounds.seen == ["idle.test"]
    assert (results["a"].status, results["a"].source, results["b"].source) == (HEALTHY, "traffic", "probe")
    # A round later the call is too old to vouch for the model.
    await rounds.next()
    assert sorted(rounds.seen) == ["busy.test", "idle.test"]


async def test_a_reasoning_model_gets_enough_tokens_to_answer():
    def reasoning(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens'"}})
        return httpx.Response(200 if body["max_completion_tokens"] >= 16 else 400, json={"ok": True})

    store, _ = await _round(reasoning, [_target("a", "chat.test")])

    assert [(result.status, result.status_code) for result in store.recorded] == [(HEALTHY, 200)]
