import pytest

import core.graphrag.general.extractor as extractor_module
from common.exceptions import TaskCanceledException
from core.graphrag.general.extractor import Extractor
from core.graphrag.general.graph_extractor import GraphExtractor


class CountingLLM:
    llm_name = "counting-llm"
    max_length = 4096

    def __init__(self, outcomes: list):
        self.calls = 0
        self._outcomes = outcomes

    async def async_chat(self, system, history: list[dict[str, str]], gen_conf=None, **kwargs):
        self.calls += 1
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def test_async_chat_returns_cached_response_without_llm_call(monkeypatch):
    llm = CountingLLM(["should not be called"])
    extractor = Extractor(llm)
    monkeypatch.setattr(extractor_module, "get_llm_cache", lambda *args, **kwargs: "cached response")

    result = await extractor._async_chat("system", [{"role": "user", "content": "Output:"}], {})

    assert result == "cached response"
    assert llm.calls == 0


async def test_async_chat_timeout_is_not_retried(monkeypatch):
    llm = CountingLLM([TimeoutError()])
    extractor = Extractor(llm)
    monkeypatch.setattr(extractor_module, "get_llm_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(extractor_module, "set_llm_cache", lambda *args, **kwargs: None)

    with pytest.raises(TimeoutError):
        await extractor._async_chat("system", [{"role": "user", "content": "Output:"}], {})

    assert llm.calls == 1


async def test_async_chat_retries_transient_error_and_writes_cache(monkeypatch):
    llm = CountingLLM([RuntimeError("boom"), "recovered response"])
    extractor = Extractor(llm)
    cache_writes = []
    monkeypatch.setattr(extractor_module, "get_llm_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(extractor_module, "set_llm_cache", lambda *args, **kwargs: cache_writes.append(args))

    result = await extractor._async_chat("system", [{"role": "user", "content": "Output:"}], {})

    assert result == "recovered response"
    assert llm.calls == 2
    assert len(cache_writes) == 1


async def test_async_chat_raises_when_task_is_canceled(monkeypatch):
    llm = CountingLLM(["should not be called"])
    extractor = Extractor(llm)
    monkeypatch.setattr(extractor_module, "get_llm_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(extractor_module, "has_canceled", lambda task_id: True)

    with pytest.raises(TaskCanceledException):
        await extractor._async_chat("system", [{"role": "user", "content": "Output:"}], {}, task_id="task-1")

    assert llm.calls == 0


async def test_process_single_content_passes_task_id_to_gleaning_calls(monkeypatch):
    extractor = GraphExtractor(CountingLLM([]), entity_types=["person"])
    extractor.callback = None
    seen_task_ids = []
    responses = iter(["seed-response", "glean-response", "N"])

    async def fake_async_chat(system, history, gen_conf=None, task_id=""):
        seen_task_ids.append(task_id)
        return next(responses)

    monkeypatch.setattr(extractor, "_async_chat", fake_async_chat)

    out_results = []
    await extractor._process_single_content(("chunk-1", "alpha beta"), 0, 1, out_results, task_id="task-123")

    assert seen_task_ids == ["task-123", "task-123", "task-123"]
    assert len(out_results) == 1
