"""Server-owned analyst strategy and model configuration identity."""

from common.constants import LLMType
from memory.maintenance.provider import _SYSTEM
from memory.maintenance.snapshots import ANALYSIS_INPUT_CONTRACT
from memory.revisions.domain import request_hash
from memory.writes.providers import MemoryWriteProviders


async def analysis_identity(provider: MemoryWriteProviders, memory_id: str) -> dict[str, str]:
    """SQL-only prepared configuration; do not construct a model or end a txn."""
    settings, prepared = await provider._prepared(memory_id, LLMType.CHAT.value)
    return {
        "strategy_sha256": request_hash({"system": _SYSTEM, "input_contract": ANALYSIS_INPUT_CONTRACT, "output_contract": "reviewed_add_patch_noop"}),
        "model_reference": f"{prepared.model_config['llm_name']}@{prepared.model_config['llm_factory']}",
        "config_reference": provider._chat_fingerprint(settings, prepared),
    }
