"""Dataset payloads accept the same numeric identities as model defaults."""

from typing import Any

import pytest
from pydantic import ValidationError

from api.apps.restful_apis.dataset_api import CreateDatasetRequest, UpdateDatasetRequest


@pytest.mark.parametrize("reference", ["1", "3042692478845876", "9223372036854775807", "embedding@Ollama"])
@pytest.mark.parametrize("update", [False, True])
def test_embedding_reference_preserves_identity(reference: str, update: bool) -> None:
    model = UpdateDatasetRequest if update else CreateDatasetRequest
    request = model.model_validate({"embedding_model": reference, **({} if update else {"name": "dataset"})})
    assert request.embedding_model == reference


@pytest.mark.parametrize("reference", ["0", "-1", "+1", "01", "1.0", "1e3", "NaN", "Infinity", "١", "9223372036854775808", "", "missing-provider", 1, True, [], {}])
@pytest.mark.parametrize("update", [False, True])
def test_invalid_numeric_embedding_reference_fails_at_boundary(reference: Any, update: bool) -> None:
    model = UpdateDatasetRequest if update else CreateDatasetRequest
    with pytest.raises(ValidationError):
        model.model_validate({"embedding_model": reference, **({} if update else {"name": "dataset"})})
