"""Dataset payloads accept the same opaque identities as model defaults."""

from typing import Any

import pytest
from pydantic import ValidationError

from api.apps.restful_apis.dataset_api import CreateDatasetRequest, UpdateDatasetRequest


@pytest.mark.parametrize("reference", ["ab" * 16, "090c" * 8, "9" * 32])
@pytest.mark.parametrize("update", [False, True])
def test_embedding_reference_preserves_identity(reference: str, update: bool) -> None:
    model = UpdateDatasetRequest if update else CreateDatasetRequest
    request = model.model_validate({"embedding_model": reference, **({} if update else {"name": "dataset"})})
    assert request.embedding_model == reference


@pytest.mark.parametrize("reference", ["1", "AB" * 16, "model:" + "a" * 32, "a" * 31, "a" * 33, "embedding@Ollama", "", " " + "a" * 32, 1, True, [], {}])
@pytest.mark.parametrize("update", [False, True])
def test_invalid_embedding_reference_fails_at_boundary(reference: Any, update: bool) -> None:
    model = UpdateDatasetRequest if update else CreateDatasetRequest
    with pytest.raises(ValidationError):
        model.model_validate({"embedding_model": reference, **({} if update else {"name": "dataset"})})
