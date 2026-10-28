"""Cohere catalog updates respect the two active loaders and driver inventory."""

import json
from pathlib import Path

from api.db.services.model_provider_service import catalog
from common.app_config import get_factory_llm_infos

ROOT = Path(__file__).resolve().parents[2]
NEW_CHAT = {
    "command-a-plus-05-2026": 131072,
    "command-a-03-2025": 262144,
    "command-r7b-12-2024": 131072,
    "command-a-translate-08-2025": 8192,
    "command-a-reasoning-08-2025": 262144,
    "command-a-vision-07-2025": 131072,
    "command-r-plus-08-2024": 131072,
    "command-r-08-2024": 131072,
}
LEGACY = {
    "command-r-plus",
    "command-r",
    "command",
    "command-nightly",
    "command-light",
    "command-light-nightly",
    "embed-english-v3.0",
    "embed-english-light-v3.0",
    "embed-multilingual-v3.0",
    "embed-multilingual-light-v3.0",
    "embed-english-v2.0",
    "embed-english-light-v2.0",
    "embed-multilingual-v2.0",
    "rerank-english-v3.0",
    "rerank-multilingual-v3.0",
    "rerank-english-v2.0",
    "rerank-multilingual-v2.0",
}


def test_python_active_catalog_retains_old_models_and_only_supported_capabilities() -> None:
    factory = next(item for item in get_factory_llm_infos() if item["name"] == "Cohere")
    models = {model.model_name: model for model in catalog("Cohere")}
    assert len(models) == len(factory["llm"]) == 29
    assert LEGACY <= models.keys()
    assert all("mdl_type" in row and "model_type" not in row for row in factory["llm"])
    for name, limit in NEW_CHAT.items():
        assert models[name].max_tokens == limit and models[name].model_types == ["chat"]
        assert models[name].is_tools is (name not in {"command-a-translate-08-2025", "command-a-vision-07-2025"})
    assert models["embed-v4.0"].max_tokens == 131072 and models["embed-v4.0"].model_types == ["embedding"]
    for name in ("rerank-v4.0-pro", "rerank-v4.0-fast", "rerank-v3.5"):
        assert models[name].model_types == ["rerank"]
        assert models[name].max_tokens == (4096 if name == "rerank-v3.5" else 32768)
    assert "SPEECH2TEXT" not in factory["tags"]
    assert not any("speech2text" in model.model_types for model in models.values())


def test_go_catalog_keeps_its_identity_and_available_asr_contract() -> None:
    native = json.loads((ROOT / "configs/models/cohere.json").read_text())
    models = {row["name"]: row for row in native["models"]}
    assert native["name"] == "CoHere" and native["class"] == "cohere"
    assert native["url_suffix"]["asr"] == "audio/transcriptions"
    assert len(models) == len(native["models"]) == 19
    for name, limit in NEW_CHAT.items():
        assert models[name] == {"name": name, "max_tokens": limit, "model_types": ["chat"]}
    assert models["embed-v4.0"]["max_tokens"] == 131072
    assert models["rerank-v4.0-pro"]["max_tokens"] == 32768
    assert models["cohere-transcribe-03-2026"] == {"name": "cohere-transcribe-03-2026", "max_tokens": 8192, "model_types": ["speech2text"]}
