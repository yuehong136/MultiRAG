"""The worker provider seam: lazy lookup, fail-closed names, and plan shape."""

from __future__ import annotations

import pytest

from api.channel_providers import provider_names
from api.channel_runtime.schemas import RuntimeCredential
from api.channels.feishu.channel import FeishuChannel
from api.channels.provider import (
    ManagedChannelPlan,
    UnsupportedChannelProvider,
    WorkerProvider,
    WorkerTuning,
    supported_provider_names,
    worker_provider,
)
from common.app_config import AppConfig


def test_supported_provider_names_is_stable_and_complete() -> None:
    """Sorted, deduplicated, and the same list the control plane offers.

    Pinned the literal ``("feishu",)`` until CHN-P10 added a second provider,
    which is the assertion failing that told us the runner and the control
    plane were in fact reading one registry. Comparing the two derived lists
    keeps that guarantee without re-pinning a name that will change again.
    """

    names = supported_provider_names()
    assert names == tuple(sorted(set(names)))
    assert names == provider_names()
    assert "feishu" in names


def test_worker_provider_resolves_a_registered_descriptor() -> None:
    provider = worker_provider("feishu")

    assert provider.name == "feishu"
    # The runner only ever depends on the structural contract.
    assert isinstance(provider, WorkerProvider)


@pytest.mark.parametrize("name", ["telegram", "FEISHU", "", "api.channels.feishu.provider"])
def test_worker_provider_fails_closed_for_unregistered_names(name: str) -> None:
    # A stray provider name must not import an arbitrary module.
    with pytest.raises(UnsupportedChannelProvider, match="CHANNEL_NOT_SUPPORTED"):
        worker_provider(name)


def test_tuning_maps_the_provider_section_and_normalizes_the_timeout() -> None:
    app_config = AppConfig.model_validate({})
    section = app_config.channels.feishu

    tuning = worker_provider("feishu").tuning(app_config)

    assert isinstance(tuning, WorkerTuning)
    assert tuning.queue_size == section.queue_size
    assert tuning.worker_concurrency == section.worker_concurrency
    assert tuning.leader_renew_seconds == section.leader_renew_seconds
    assert tuning.max_question_chars == section.max_question_chars
    # The HTTP boundary takes seconds as a float; the provider converts once.
    assert isinstance(tuning.total_timeout_seconds, float)
    assert tuning.total_timeout_seconds == float(section.total_timeout_seconds)


def test_build_managed_keeps_the_raw_account_id_for_state_isolation() -> None:
    credential = RuntimeCredential(app_id="cli_unit", app_secret="unit-secret")

    plan = worker_provider("feishu").build_managed(
        credential=credential,
        public_config={"domain": "lark", "allowed_open_ids": ["ou_a", "ou_b"]},
    )

    assert isinstance(plan, ManagedChannelPlan)
    assert isinstance(plan.channel, FeishuChannel)
    # The state store namespaces dedupe/session/lease keys by this value, which is
    # what keeps two accounts of one provider from sharing a leader lease.
    assert plan.account_id == "cli_unit"
    assert plan.allowed_sender_ids == frozenset({"ou_a", "ou_b"})
    # The transport itself only ever exposes a hashed account id.
    assert plan.channel.account_id != "cli_unit"
    assert "unit-secret" not in repr(plan)


@pytest.mark.parametrize(
    "public_config",
    [
        {},
        {"domain": "international"},
        {"domain": "feishu", "allowed_open_ids": "ou_a"},
        {"domain": "feishu", "allowed_open_ids": [" "]},
    ],
)
def test_build_managed_rejects_untrustworthy_public_config(public_config: dict[str, object]) -> None:
    credential = RuntimeCredential(app_id="cli_unit", app_secret="unit-secret")

    with pytest.raises(Exception, match="CHANNEL_RUNTIME_CONFIG_INVALID"):
        worker_provider("feishu").build_managed(credential=credential, public_config=public_config)
