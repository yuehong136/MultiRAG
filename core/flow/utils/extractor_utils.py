"""
Canvas-free Extractor facade.

This helper keeps the direct-call metadata extraction interface while delegating
prompt rendering and chunk iteration to ``core.flow.extractor.Extractor``.
"""

import logging
from copy import deepcopy

from core.flow.extractor.extractor import Extractor, ExtractorParam

logger = logging.getLogger(__name__)


class _ExtractorCanvas:
    def __init__(self, chunks: list[dict], doc_id: str | None = None):
        self._chunks = chunks
        self._doc_id = doc_id

    def get_variable_value(self, name: str):
        if name == "sys.query":
            return self._chunks
        return ""

    def get_component_name(self, cpn_id) -> str:
        return str(cpn_id)


def _normalize_chunks(chunks: list[dict]) -> list[dict]:
    normalized = []
    for chunk in chunks or []:
        item = deepcopy(chunk)
        text = item.get("text")
        if not isinstance(text, str):
            text = item.get("content_with_weight")
        item["text"] = text if isinstance(text, str) else ("" if text is None else str(text))
        normalized.append(item)
    return normalized


def _make_process(
    chunks: list[dict],
    field_name: str,
    prompt: str,
    llm_model,
    temperature: float,
    max_tokens: int,
    callback=None,
) -> Extractor:
    param = ExtractorParam()
    param.field_name = field_name
    param.llm_id = "__direct_llm__"
    param.sys_prompt = prompt
    param.prompts = [{"role": "user", "content": "{sys.query}"}]
    param.temperature = temperature
    param.max_tokens = max_tokens
    param.temperatureEnabled = True
    param.maxTokensEnabled = True

    process = Extractor.__new__(Extractor)
    process._param = param
    process._id = "extractor-utils"
    process._canvas = _ExtractorCanvas(chunks)
    process.chat_mdl = llm_model
    process.imgs = []

    def _callback(prog, msg=""):
        if callback:
            callback(prog, msg)

    process.callback = _callback
    return process


class FlowExtractor:
    """Canvas-free facade over the runtime Extractor component."""

    @staticmethod
    async def extract(
        chunks: list[dict],
        field_name: str,
        prompt: str,
        llm_model,
        temperature: float = 0.1,
        max_tokens: int = 512,
        callback=None,
    ) -> list[dict]:
        input_chunks = _normalize_chunks(chunks)
        if not input_chunks:
            return []

        process = _make_process(
            input_chunks,
            field_name,
            prompt,
            llm_model,
            temperature,
            max_tokens,
            callback,
        )

        await process._invoke()
        error = process.output("_ERROR")
        if error:
            raise ValueError(error)

        result = process.output("chunks") or []
        logger.info("Extractor: extracted %s for %d chunks", field_name, len(result))
        return result


async def extract_metadata(
    chunks: list[dict],
    field_name: str,
    prompt: str,
    llm_model,
    temperature: float = 0.1,
    max_tokens: int = 512,
    callback=None,
) -> list[dict]:
    return await FlowExtractor.extract(
        chunks,
        field_name,
        prompt,
        llm_model,
        temperature,
        max_tokens,
        callback,
    )
