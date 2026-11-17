"""Naming and exposure rules of the model gateway catalog, without a database."""

import pytest

from api.db.db_models import GatewayModelSetting
from api.model_gateway.catalog import RegisteredModel
from api.model_gateway.directory import CALLABLE, CLOSED, UNAVAILABLE, NameSource, assign_names, build_directory
from api.model_gateway.limits import Limits
from api.model_gateway.store import Caller


def _source(model_id: int, provider: str, instance: str, name: str, **extra: object) -> NameSource:
    return NameSource(model_id=model_id, provider=provider, instance_name=instance, model_name=name, **extra)  # type: ignore[arg-type]


def _registered(
    model_id: int, name: str = "qwen-plus", *, provider: str = "Tongyi-Qianwen", instance: str = "default", types: tuple[str, ...] = ("chat",), available: bool = True, relayable: bool = True
) -> RegisteredModel:
    return RegisteredModel(
        model_id=model_id,
        model_name=name,
        provider=provider,
        instance_id=f"{provider}-{instance}",
        instance_name=instance,
        model_types=types,
        available=available,
        relayable=relayable,
        created=1700000000000,
    )


def _setting(model_id: int, **values: object) -> GatewayModelSetting:
    return GatewayModelSetting(id=f"s{model_id}", tenant_id="t", model_id=model_id, created_by="u", **{"enabled": True, "preferred": False, **values})


def test_a_unique_model_is_called_by_its_own_name():
    names = assign_names([_source(1, "Tongyi-Qianwen", "default", "qwen-plus")])

    assert names[1] == ("qwen-plus", "Tongyi-Qianwen/qwen-plus", "Tongyi-Qianwen/default/qwen-plus")


def test_the_earliest_registered_model_keeps_a_shared_name():
    names = assign_names(
        [
            _source(3, "Tongyi-Qianwen", "intl", "qwen-plus"),
            _source(2, "OpenAI-API-Compatible", "intranet", "qwen-plus"),
            _source(1, "Tongyi-Qianwen", "default", "qwen-plus"),
        ]
    )

    assert names[1] == ("qwen-plus", "Tongyi-Qianwen/qwen-plus", "Tongyi-Qianwen/default/qwen-plus")
    assert names[2] == ("OpenAI-API-Compatible/qwen-plus", "OpenAI-API-Compatible/intranet/qwen-plus")
    # Same provider and name: only the connection-qualified name is left.
    assert names[3] == ("Tongyi-Qianwen/intl/qwen-plus",)


def test_a_later_model_never_takes_a_name_from_an_existing_one():
    before = assign_names([_source(1, "VLLM", "gpu-a", "Qwen3-32B")])
    after = assign_names([_source(1, "VLLM", "gpu-a", "Qwen3-32B"), _source(9, "VLLM", "gpu-b", "Qwen3-32B")])

    assert after[1] == before[1]
    assert after[9] == ("VLLM/gpu-b/Qwen3-32B",)


def test_the_preferred_model_owns_the_shared_names():
    names = assign_names([_source(1, "Tongyi-Qianwen", "default", "qwen-plus"), _source(2, "OpenAI-API-Compatible", "intranet", "qwen-plus", preferred=True)])

    assert names[2][0] == "qwen-plus"
    assert names[1] == ("Tongyi-Qianwen/qwen-plus", "Tongyi-Qianwen/default/qwen-plus")


def test_an_alias_is_the_call_name_and_the_other_names_still_work():
    names = assign_names([_source(4, "VLLM", "gpu-a", "BAAI/bge-m3", alias="embed-default")])

    assert names[4] == ("embed-default", "BAAI/bge-m3", "VLLM/BAAI/bge-m3", "VLLM/gpu-a/BAAI/bge-m3")


def test_qualified_names_outrank_a_bare_name_that_looks_like_them():
    # A self-hosted model literally named like another provider's qualified name.
    names = assign_names([_source(1, "VLLM", "gpu-a", "Tongyi-Qianwen/qwen-plus"), _source(2, "Tongyi-Qianwen", "default", "qwen-plus")])

    assert "Tongyi-Qianwen/qwen-plus" in names[2]
    assert "Tongyi-Qianwen/qwen-plus" not in names[1]


def test_untouched_models_follow_the_owner_default():
    opened = build_directory([_registered(1)], {}, new_models_enabled=True)
    closed = build_directory([_registered(1)], {}, new_models_enabled=False)

    assert (opened.models[0].state, opened.models[0].enabled_by_default) == (CALLABLE, True)
    assert (closed.models[0].state, closed.models[0].enabled_by_default) == (CLOSED, True)


def test_a_switched_model_keeps_its_own_state_and_limits():
    directory = build_directory([_registered(1), _registered(2, "qwen-flash")], {1: _setting(1, enabled=False), 2: _setting(2, rpm_limit=600)}, new_models_enabled=True)

    first, second = directory.models
    assert (first.state, first.enabled_by_default) == (CLOSED, False)
    assert (second.state, second.limits) == (CALLABLE, Limits(rpm=600))


@pytest.mark.parametrize(
    ("model", "reason"),
    [
        (_registered(1, relayable=False, available=False), "provider_not_supported"),
        (_registered(1, types=("rerank",), available=False), "endpoint_not_supported"),
        (_registered(1, available=False), "model_disabled"),
    ],
)
def test_models_the_gateway_cannot_serve_say_why_whatever_their_switch(model, reason):
    entry = build_directory([model], {1: _setting(1, enabled=True)}, new_models_enabled=True).models[0]

    assert (entry.state, entry.reason) == (UNAVAILABLE, reason)


def test_servable_types_keep_only_capabilities_with_an_endpoint():
    entry = build_directory([_registered(1, types=("chat", "image2text", "embedding"))], {}, new_models_enabled=True).models[0]

    assert entry.servable_types == ("chat", "embedding")


def test_lookup_answers_every_name_and_marks_shared_names():
    directory = build_directory([_registered(1), _registered(2, provider="OpenAI-API-Compatible", instance="intranet")], {}, new_models_enabled=True)

    assert directory.lookup("qwen-plus").model_id == 1  # type: ignore[union-attr]
    assert directory.lookup("OpenAI-API-Compatible/qwen-plus").model_id == 2  # type: ignore[union-attr]
    assert directory.lookup("gpt-4o") is None
    assert all(model.shares_name for model in directory.models)


def test_a_key_may_use_listed_models_whole_connections_or_everything():
    narrow = Caller(key_id="k", tenant_id="t", key_name="n", allow_all_models=False, allowed_model_ids=frozenset({1}), allowed_instance_ids=frozenset({"gpu-a"}))
    everything = Caller(key_id="k", tenant_id="t", key_name="n", allow_all_models=True)
    nothing = Caller(key_id="k", tenant_id="t", key_name="n", allow_all_models=False)

    assert narrow.may_use(1, "elsewhere") and narrow.may_use(5, "gpu-a")
    assert not narrow.may_use(5, "elsewhere")
    assert everything.may_use(5, "elsewhere")
    assert not nothing.may_use(1, "gpu-a")
