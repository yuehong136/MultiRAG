"""Dataset scope validation must retain partial parser configuration semantics."""

from typing import Any

import pytest
from pydantic import ValidationError

from api.apps.restful_apis.dataset_api import CreateDatasetRequest, UpdateDatasetRequest
from api.utils.validation_utils import RaptorConfig


@pytest.mark.parametrize("scope", ["file", "dataset"])
def test_create_dataset_retains_raptor_scope_and_extensions(scope: str) -> None:
    request = CreateDatasetRequest(
        name="scope",
        parser_config={"raptor": {"scope": scope, "ext": {"future": {"enabled": False}}}, "ext": {"future_parser": 7}},
    )
    config = request.model_dump()["parser_config"]
    assert config["raptor"]["scope"] == scope
    assert config["raptor"]["ext"] == {"future": {"enabled": False}}
    assert config["ext"] == {"future_parser": 7}


def test_create_scope_default_is_file() -> None:
    assert RaptorConfig().scope == "file"


@pytest.mark.parametrize("scope", ["file", "dataset"])
def test_dataset_scope_patch_stays_partial(scope: str) -> None:
    config = {"raptor": {"scope": scope, "future": False, "ext": {"custom": 7}}, "metadata": [{"key": "author"}]}
    request = UpdateDatasetRequest(parser_config=config)
    assert request.model_dump(exclude_unset=True) == {"parser_config": config}


def test_dataset_patch_without_scope_does_not_inject_file_default() -> None:
    config = {"raptor": {"use_raptor": False, "ext": {"legacy": True}}, "parent_child": {}}
    assert UpdateDatasetRequest(parser_config=config).model_dump(exclude_unset=True) == {"parser_config": config}
    assert UpdateDatasetRequest(parser_config={}).model_dump(exclude_unset=True) == {"parser_config": {}}
    assert UpdateDatasetRequest(description="rename").model_dump(exclude_unset=True) == {"description": "rename"}


@pytest.mark.parametrize("scope", ["all", "Dataset", "", None, 0, [], {}])
@pytest.mark.parametrize("request_type", [CreateDatasetRequest, UpdateDatasetRequest])
def test_invalid_dataset_scope_is_rejected(scope: Any, request_type: type[CreateDatasetRequest] | type[UpdateDatasetRequest]) -> None:
    kwargs: dict[str, Any] = {"parser_config": {"raptor": {"scope": scope}}}
    if request_type is CreateDatasetRequest:
        kwargs["name"] = "scope"
    with pytest.raises(ValidationError):
        request_type(**kwargs)


def test_pipeline_create_keeps_scope_and_parser_dependency_contract() -> None:
    request = CreateDatasetRequest(name="pipeline", parse_type=2, pipeline_id="a" * 32, parser_config={"raptor": {"scope": "dataset"}})
    assert request.model_dump()["parser_config"]["raptor"]["scope"] == "dataset"
    assert request.chunk_method is None
    with pytest.raises(ValidationError):
        CreateDatasetRequest(name="pipeline", parse_type=2, pipeline_id="a" * 32, chunk_method="naive")


@pytest.mark.parametrize("request_type", [CreateDatasetRequest, UpdateDatasetRequest])
def test_scope_validation_covers_legacy_ext_without_dropping_it(request_type: type[CreateDatasetRequest] | type[UpdateDatasetRequest]) -> None:
    ext = {"parser_config": {"raptor": {"scope": "dataset", "future": False}}, "pipeline_id": "a" * 32}
    kwargs = {"name": "scope"} if request_type is CreateDatasetRequest else {}
    assert request_type(**kwargs, ext=ext).model_dump(exclude_unset=True)["ext"] == ext
    with pytest.raises(ValidationError):
        request_type(**kwargs, ext={"parser_config": {"raptor": {"scope": "invalid"}}})
