"""Dataset create/update boundary contracts without external services."""

from typing import Any

import pytest
from pydantic import ValidationError

from api.apps.restful_apis.dataset_api import CreateDatasetRequest, DeleteDatasetRequest, UpdateDatasetRequest
from tests.support.dataset_contract_cases import CREATE_INVALID_PARSER_CASES, CREATE_VALID_PARSER_CASES, UPDATE_INVALID_PARSER_CASES, UPDATE_VALID_PARSER_CASES


@pytest.mark.parametrize("avatar", [None, "", "data:image/png;base64,aGVsbG8=", "data:image/jpeg;base64,aGVsbG8="])
def test_empty_and_supported_avatar(avatar: str | None) -> None:
    assert CreateDatasetRequest(name="dataset", avatar=avatar).avatar == avatar
    assert UpdateDatasetRequest(avatar=avatar).model_dump(exclude_unset=True) == {"avatar": avatar}


@pytest.mark.parametrize("value", [False, 0, [], {}, " ", "bad", "data:image/svg+xml;base64,abc", "data:video/mp4;base64,abc", "a" * 65536])
@pytest.mark.parametrize("update", [False, True])
def test_invalid_avatar(value: Any, update: bool) -> None:
    with pytest.raises(ValidationError):
        (UpdateDatasetRequest if update else CreateDatasetRequest).model_validate({"avatar": value, **({} if update else {"name": "dataset"})})


def test_explicit_null_embedding_uses_default() -> None:
    assert CreateDatasetRequest(name="dataset", embedding_model=None).embedding_model is None
    assert UpdateDatasetRequest(embedding_model=None).model_dump(exclude_unset=True) == {"embedding_model": None}


@pytest.mark.parametrize("name,config", CREATE_VALID_PARSER_CASES + UPDATE_VALID_PARSER_CASES)
def test_parser_valid_matrix(name: str, config: dict[str, Any]) -> None:
    assert CreateDatasetRequest(name=name, parser_config=config).parser_config is not None
    assert UpdateDatasetRequest(parser_config=config).model_dump(exclude_unset=True) == {"parser_config": config}


@pytest.mark.parametrize("config,fragment", CREATE_INVALID_PARSER_CASES + UPDATE_INVALID_PARSER_CASES)
@pytest.mark.parametrize("update", [False, True])
def test_parser_invalid_matrix(config: dict[str, Any], fragment: str, update: bool) -> None:
    with pytest.raises(ValidationError):
        (UpdateDatasetRequest if update else CreateDatasetRequest).model_validate({"parser_config": config, **({} if update else {"name": "dataset"})})


@pytest.mark.parametrize(
    "payload",
    [
        {"name": None},
        {"name": " "},
        {"name": 0},
        {"permission": None},
        {"permission": "ME"},
        {"permission": []},
        {"chunk_method": None},
        {"chunk_method": "bad"},
        {"pagerank": None},
        {"pagerank": False},
        {"pagerank": 3.14},
        {"pagerank": -1},
        {"pagerank": 101},
        {"description": "a" * 65536},
        {"embedding_model": ""},
        {"embedding_model": "@Builtin"},
        {"id": "forged"},
    ],
)
def test_invalid_update_fields(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UpdateDatasetRequest.model_validate(payload)


@pytest.mark.parametrize("pagerank", [0, 50, 100])
def test_pagerank_and_omission(pagerank: int) -> None:
    assert UpdateDatasetRequest(pagerank=pagerank).model_dump(exclude_unset=True) == {"pagerank": pagerank}
    assert UpdateDatasetRequest().model_dump(exclude_unset=True) == {}


def test_partial_parser_extensions_survive() -> None:
    patch = {"raptor": {"max_token": 1024, "future_knob": "keep"}, "future_parser": {"value": 0}, "built_in_metadata": []}
    assert UpdateDatasetRequest(parser_config=patch).model_dump(exclude_unset=True) == {"parser_config": patch}
    with pytest.raises(ValidationError):
        UpdateDatasetRequest(ext={"parser_config": {"raptor": {"max_token": 0}}})


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("extension", [{"chunk_token_num": 0}, {"auto_keywords": -1}, {"built_in_metadata": [{"type": "string"}]}, {"raptor": {"max_token": 0}}])
def test_partial_parser_effective_extension_validation(extension: dict[str, Any], wrapped: bool) -> None:
    payload: dict[str, Any] = {"parser_config": {"ext": extension}}
    if wrapped:
        payload = {"ext": payload}
    with pytest.raises(ValidationError):
        UpdateDatasetRequest.model_validate(payload)


def test_partial_parser_extension_table_roles_are_normalized_without_losing_extensions() -> None:
    patch = {"ext": {"table_column_roles": {"title": "vectorize"}, "future": {"zero": 0}}}
    result = UpdateDatasetRequest(parser_config=patch).model_dump(exclude_unset=True)
    assert result == {"parser_config": {"ext": {"table_column_roles": {"title": "indexing"}, "future": {"zero": 0}}}}


def test_delete_rejects_duplicates_before_execution() -> None:
    with pytest.raises(ValidationError, match="Duplicate ids"):
        DeleteDatasetRequest(ids=["a", "a"])


@pytest.mark.parametrize("field", ["id", "tenant_id", "created_by", "status"])
@pytest.mark.parametrize("update", [False, True])
def test_immutable_identity_cannot_be_supplied_through_extension(field: str, update: bool) -> None:
    payload = {"ext": {field: "forged"}, **({} if update else {"name": "dataset"})}
    with pytest.raises(ValidationError):
        (UpdateDatasetRequest if update else CreateDatasetRequest).model_validate(payload)


@pytest.mark.parametrize("value", ["", " ", "missing-at", "@Builtin", "model@", " @Builtin", "model@ ", 0, []])
@pytest.mark.parametrize("update", [False, True])
def test_embedding_identifier_matrix(value: Any, update: bool) -> None:
    with pytest.raises(ValidationError):
        (UpdateDatasetRequest if update else CreateDatasetRequest).model_validate({"embedding_model": value, **({} if update else {"name": "dataset"})})
