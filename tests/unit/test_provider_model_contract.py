"""Strict public instance/model inputs; legal zero and explicit empty selection."""

import pytest
from pydantic import ValidationError

from api.db.model_contract import DefaultModelInput, InstanceInput, InstancePatch, ModelRegistration, parse_model_id


def test_zero_context_and_empty_catalog_selection_are_explicit() -> None:
    model = ModelRegistration(model_name="ocr", model_types=["ocr"], max_tokens=0)
    assert model.max_tokens == 0
    assert InstanceInput(instance_name="local", api_key="", region="", model_ids=[]).model_ids == []
    assert DefaultModelInput(model_type="ocr", model_id=None).model_id is None


@pytest.mark.parametrize("patch", [{}, {"api_key": None}, {"status": None}, {"region": None}, {"api_key": "secret", "clear_api_key": True}, {"base_url": "https://user:secret@host"}])
def test_instance_patch_rejects_ambiguous_or_secret_urls(patch: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InstancePatch(**patch)


@pytest.mark.parametrize("value", [False, 1.1, "1", -1, None])
def test_max_tokens_rejects_coercion(value: object) -> None:
    with pytest.raises(ValidationError):
        ModelRegistration(model_name="model", model_types=["chat"], max_tokens=value)


@pytest.mark.parametrize("types", [[], ["chat", "chat"], ["unknown"], ["CHAT"]])
def test_capability_contract_rejects_invalid_lists(types: list[str]) -> None:
    with pytest.raises(ValidationError):
        ModelRegistration(model_name="model", model_types=types, max_tokens=0)


@pytest.mark.parametrize("value", ["0", "-1", "+1", "1.0", "01", "9223372036854775808"])
def test_numeric_model_references_are_unambiguous(value: str) -> None:
    with pytest.raises(ValueError):
        parse_model_id(value)


def test_numeric_and_legacy_references() -> None:
    assert parse_model_id("12") == 12
    assert parse_model_id("org/model@instance@provider") is None
    assert InstancePatch(api_key="").api_key == ""


def test_remote_model_batch_keeps_explicit_selection_and_rejects_ambiguity() -> None:
    models = [{"model_name": name, "model_types": ["chat", "image2text"], "max_tokens": 0} for name in ("first", "second")]
    candidate = InstanceInput(instance_name="local", api_key="", model_ids=[], custom_models=models)
    assert candidate.model_ids == [] and [item.model_name for item in candidate.custom_models] == ["first", "second"]
    with pytest.raises(ValidationError):
        InstanceInput(instance_name="local", api_key="", model_ids=[], custom_models=[models[0], models[0]])
    with pytest.raises(ValidationError):
        InstanceInput(instance_name="local", api_key="", model_ids=[], custom_model=models[0], custom_models=[models[1]])
    with pytest.raises(ValidationError):
        InstanceInput(instance_name="local", api_key="", model_ids=[], custom_models=[{**models[0], "model_name": str(index)} for index in range(101)])
