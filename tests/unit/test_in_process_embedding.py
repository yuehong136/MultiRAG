"""Model identity, cache ownership and retired-runtime boundaries."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import FlagEmbedding
import numpy as np
import pytest
from sqlalchemy.orm import Session

from api.db.services.tenant_llm_service import TenantLLMService
from api.utils import api_utils
from core.llm import EmbeddingModel, embedding


@pytest.fixture
def loader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(embedding.settings, "LIGHTEN", 0)
    monkeypatch.setattr(embedding, "get_home_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(embedding.DefaultEmbedding, "_model", None)
    monkeypatch.setattr(embedding.DefaultEmbedding, "_model_key", None)
    monkeypatch.setattr(embedding.DefaultEmbedding, "_model_name", "")

    def load(path: str, **kwargs: Any) -> SimpleNamespace:
        calls.append({"path": path, **kwargs})
        return SimpleNamespace(path=path)

    monkeypatch.setattr(FlagEmbedding, "FlagModel", load)
    return calls


def local_model(directory: Path) -> str:
    directory.mkdir(parents=True)
    (directory / "config.json").write_text("{}")
    return str(directory)


def test_local_models_are_bound_by_identity_and_reused(loader: list[dict[str, Any]], tmp_path: Path) -> None:
    first_path, second_path = local_model(tmp_path / "first"), local_model(tmp_path / "second")
    first = embedding.DefaultEmbedding(None, "BAAI/first", model_path=first_path)
    same = embedding.DefaultEmbedding(None, "BAAI/first", model_path=first_path)
    second = embedding.DefaultEmbedding(None, "BAAI/second", model_path=second_path)
    assert first._model is same._model
    assert first._model.path == first_path
    assert second._model.path == second_path
    assert first._model is not second._model
    assert len(loader) == 2


def test_concurrent_loads_keep_instance_ownership(loader: list[dict[str, Any]], tmp_path: Path) -> None:
    paths = [local_model(tmp_path / str(i)) for i in range(4)]

    def load(index: int) -> embedding.DefaultEmbedding:
        return embedding.DefaultEmbedding(None, f"BAAI/{index}", model_path=paths[index])

    with ThreadPoolExecutor(max_workers=4) as executor:
        models = list(executor.map(load, range(4)))
    assert [model._model.path for model in models] == paths


def test_download_uses_requested_model_and_offline_policy(loader: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    downloaded = local_model(tmp_path / "snapshot")
    requests: list[dict[str, Any]] = []

    def download(**kwargs: Any) -> str:
        requests.append(kwargs)
        return downloaded

    monkeypatch.setattr(embedding, "snapshot_download", download)
    model = embedding.DefaultEmbedding(None, "BAAI/bge-small-en-v1.5", local_files_only=True, query_instruction="")
    assert requests == [{"repo_id": "BAAI/bge-small-en-v1.5", "cache_dir": None, "local_files_only": True}]
    assert model._model_name == "BAAI/bge-small-en-v1.5"
    assert loader[0]["query_instruction_for_retrieval"] == ""


def test_failed_load_never_substitutes_model(loader: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = local_model(tmp_path / "broken")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise ValueError("invalid model weights")

    monkeypatch.setattr(FlagEmbedding, "FlagModel", fail)
    monkeypatch.setattr(embedding, "snapshot_download", lambda **kwargs: pytest.fail("must not substitute another model"))
    with pytest.raises(ValueError, match="invalid model weights"):
        embedding.DefaultEmbedding(None, "BAAI/broken", model_path=path)
    assert embedding.DefaultEmbedding._model is None


def test_explicit_missing_path_fails_without_download(loader: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(embedding, "snapshot_download", lambda **kwargs: pytest.fail("explicit local path must stay offline"))
    with pytest.raises(FileNotFoundError, match="incomplete"):
        embedding.DefaultEmbedding(None, "BAAI/missing", model_path=str(tmp_path / "absent"))


def test_lighten_fails_clearly(loader: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedding.settings, "LIGHTEN", 1)
    with pytest.raises(RuntimeError, match="LIGHTEN"):
        embedding.DefaultEmbedding(None, "BAAI/model")
    assert not loader


def test_query_returns_one_full_vector() -> None:
    model = embedding.DefaultEmbedding.__new__(embedding.DefaultEmbedding)
    vector = np.array([[0.25, 0.5, 0.75]], dtype=np.float32)
    model._model = SimpleNamespace(encode_queries=lambda texts, **kwargs: vector)
    result, tokens = model.encode_queries("hello")
    np.testing.assert_array_equal(result, vector[0])
    assert result.shape == (3,)
    assert tokens > 0


@pytest.mark.parametrize("allowlist", [None, ["FastEmbed", "BAAI"]])
def test_retired_factory_is_not_offered(monkeypatch: pytest.MonkeyPatch, db: Session, allowlist: list[str] | None) -> None:
    monkeypatch.setattr(api_utils.settings, "ALLOWED_LLM_FACTORIES", allowlist)
    monkeypatch.setattr(api_utils.LLMFactoriesService, "get_all", lambda *args, **kwargs: [SimpleNamespace(name="FastEmbed"), SimpleNamespace(name="BAAI")])
    assert [factory.name for factory in api_utils.get_allowed_llm_factories(db)] == ["BAAI"]
    assert "BAAI" in EmbeddingModel
    assert "FastEmbed" not in EmbeddingModel


def test_retired_tenant_model_requires_explicit_migration(db: Session) -> None:
    with pytest.raises(ValueError, match="rebuild its vector index"):
        TenantLLMService.model_instance(db, "tenant", {"llm_factory": "FastEmbed", "mdl_type": "embedding"})
