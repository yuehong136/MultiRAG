"""Deployment order and cooldown of the model gateway router, without a network."""

import random
from collections import Counter
from dataclasses import dataclass

import pytest

from api.model_gateway.router import Router, is_retryable


@dataclass(frozen=True)
class Deployment:
    model_id: str
    weight: int = 1
    order: int | None = None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _router(clock: Clock | None = None, **settings: float) -> Router:
    return Router(clock=clock or Clock(), rng=random.Random(7), **settings)


def _ids(plan: list[Deployment]) -> list[str]:
    return [item.model_id for item in plan]


def test_a_single_deployment_is_the_whole_plan():
    assert _ids(_router().plan([Deployment("a")])) == ["a"]


def test_the_lowest_order_is_tried_first_and_unordered_deployments_last():
    router = _router()
    deployments = [Deployment("unordered"), Deployment("second", order=2), Deployment("first", order=1)]

    for _ in range(20):
        assert _ids(router.plan(deployments)) == ["first", "second", "unordered"]


def test_weight_divides_the_first_pick_within_one_order():
    router = _router()
    deployments = [Deployment("heavy", weight=9), Deployment("light", weight=1)]

    first = Counter(router.plan(deployments)[0].model_id for _ in range(2000))

    assert 1700 < first["heavy"] < 1900
    assert first["heavy"] + first["light"] == 2000


def test_every_deployment_appears_once_in_a_plan():
    router = _router()
    deployments = [Deployment(name, weight=weight) for name, weight in (("a", 1), ("b", 5), ("c", 3), ("d", 1))]

    for _ in range(50):
        assert sorted(_ids(router.plan(deployments))) == ["a", "b", "c", "d"]


@pytest.mark.parametrize(("status", "retryable"), [(400, False), (422, False), (401, True), (403, True), (404, True), (408, True), (409, True), (429, True), (500, True), (503, True)])
def test_which_upstream_statuses_let_another_deployment_be_tried(status, retryable):
    assert is_retryable(status) is retryable


def test_a_rate_limited_deployment_rests_for_the_cooldown_and_comes_back():
    clock = Clock()
    router = _router(clock)
    deployments = [Deployment("a", order=1), Deployment("b", order=2)]

    assert router.failed("a", 429, deployments=2) is True
    assert router.cooling("a")
    # A resting deployment goes last, even when its order is lower.
    assert _ids(router.plan(deployments)) == ["b", "a"]

    clock.now += 5.1
    assert not router.cooling("a")
    assert _ids(router.plan(deployments)) == ["a", "b"]


def test_a_single_deployment_is_not_rested_by_rate_limits_or_a_few_failures():
    router = _router()

    assert router.failed("a", 429, deployments=1) is False
    for _ in range(20):
        assert router.failed("a", 500, deployments=1) is False
    assert not router.cooling("a")


def test_rejected_credentials_rest_a_deployment_at_once_even_alone():
    router = _router()

    assert router.failed("a", 401, deployments=1) is True
    assert router.failed("b", 404, deployments=3) is True


def test_server_errors_rest_a_deployment_once_most_recent_calls_failed():
    router = _router()
    for _ in range(4):
        router.succeeded("a")

    # Four successes and up to four failures: not yet more than half.
    for _ in range(4):
        assert router.failed("a", 500, deployments=2) is False
    assert router.failed("a", 500, deployments=2) is True

    # Too few calls for the percentage to mean anything.
    assert [router.failed("b", 503, deployments=2) for _ in range(4)] == [False] * 4
    assert router.failed("b", 503, deployments=2) is True


def test_failures_of_an_earlier_minute_are_forgotten():
    clock = Clock()
    router = _router(clock)
    for _ in range(4):
        router.failed("a", 500, deployments=2)

    clock.now += 60
    assert router.failed("a", 500, deployments=2) is False


def test_other_client_errors_never_rest_a_deployment():
    router = _router()

    for _ in range(10):
        assert router.failed("a", 400, deployments=2) is False


def test_all_deployments_resting_are_still_tried():
    router = _router()
    deployments = [Deployment("a"), Deployment("b")]
    router.failed("a", 429, deployments=2)
    router.failed("b", 429, deployments=2)

    assert sorted(_ids(router.plan(deployments))) == ["a", "b"]


def test_cooldown_can_be_switched_off():
    router = _router(cooldown_seconds=0.0)

    assert router.failed("a", 429, deployments=2) is False
    assert not router.cooling("a")
