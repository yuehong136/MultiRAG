"""Request-level web-search opt-in behavior for dialog retrieval pipelines."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import core.advanced_rag.tree_structured_query_decomposition_retrieval as deep_research_module
from api.db.services import dialog_service
from api.db.services.llm_service import LLMBundle


class _GenerationReached(RuntimeError):
    """Stop a fully stubbed chat pipeline once answer generation is reached."""


class _ReasonerConstructed(RuntimeError):
    """Stop a chat pipeline after recording the DeepResearcher contract."""


class _DeepResearchBundle(LLMBundle):
    """Nominal LLMBundle instance required by the core package's beartype hook."""

    def __init__(self) -> None:
        self.db = None


class _PipelineBundle(LLMBundle):
    """Chat-model probe shared by the synchronous and asynchronous pipelines."""

    def __init__(self) -> None:
        self.db = None
        self.max_length = 1024

    def chat(
        self,
        system: str,
        history: list[dict[str, Any]],
        gen_conf: dict[str, Any],
        **kwargs: Any,
    ) -> str:
        raise _GenerationReached

    async def async_chat(
        self,
        system: str,
        history: list[dict[str, Any]],
        gen_conf: dict[str, Any],
        **kwargs: Any,
    ) -> str:
        raise _GenerationReached


@dataclass
class _PipelineProbe:
    dialog: SimpleNamespace
    messages: list[dict[str, str]]
    tavily_calls: list[tuple[str, str]]


@pytest.fixture
def dialog_pipeline_stubs(monkeypatch: pytest.MonkeyPatch) -> _PipelineProbe:
    """Keep both chat pipelines offline while leaving their web gates observable."""
    bundle = _PipelineBundle()
    kb = SimpleNamespace(name="dataset", tenant_id="tenant-unit")
    tavily_calls: list[tuple[str, str]] = []

    def fake_llm_type(_llm_id: str) -> str:
        return "chat"

    def fake_primary_model_config(_db: Session, _dialog: Any) -> dict[str, Any]:
        return {"llm_name": "chat-model", "max_tokens": 8192}

    def fake_langfuse_keys(
        cls: type[Any],
        _db: Session,
        tenant_id: str,
    ) -> None:
        return None

    def fake_get_models(
        _db: Session,
        _dialog: Any,
    ) -> tuple[list[SimpleNamespace], None, None, _PipelineBundle, None]:
        return ([kb] if _dialog.kb_ids else []), None, None, bundle, None

    def fake_field_map(
        cls: type[Any],
        _db: Session,
        _kb_ids: list[str],
    ) -> dict[str, Any]:
        return {}

    async def fake_retrieval(*args: Any, **kwargs: Any) -> dict[str, list[Any]]:
        return {"chunks": [], "doc_aggs": []}

    def fake_message_fit_in(
        messages: list[dict[str, Any]],
        _max_length: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        return 1, messages

    def fake_kb_prompt(
        _kbinfos: dict[str, Any],
        _max_tokens: int,
    ) -> list[str]:
        return []

    class TavilySpy:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def retrieve_chunks(self, question: str) -> dict[str, list[Any]]:
            tavily_calls.append((self.api_key, question))
            return {"chunks": [], "doc_aggs": []}

    monkeypatch.setattr(
        dialog_service.TenantLLMService,
        "llm_id2llm_type",
        staticmethod(fake_llm_type),
    )
    monkeypatch.setattr(
        dialog_service,
        "_resolve_dialog_primary_model_config",
        fake_primary_model_config,
    )
    monkeypatch.setattr(
        dialog_service.TenantLangfuseService,
        "filter_by_tenant",
        classmethod(fake_langfuse_keys),
    )
    monkeypatch.setattr(dialog_service, "get_models", fake_get_models)
    monkeypatch.setattr(
        dialog_service.KnowledgebaseService,
        "get_field_map",
        classmethod(fake_field_map),
    )
    monkeypatch.setattr(
        dialog_service.settings,
        "retriever",
        SimpleNamespace(retrieval=fake_retrieval),
    )
    monkeypatch.setattr(dialog_service, "message_fit_in", fake_message_fit_in)
    monkeypatch.setattr(dialog_service, "kb_prompt", fake_kb_prompt)
    monkeypatch.setattr(dialog_service, "Tavily", TavilySpy)

    dialog = SimpleNamespace(
        kb_ids=["kb-1"],
        llm_id="chat-model",
        tenant_id="tenant-unit",
        llm_setting={},
        prompt_config={
            "system": "Use {knowledge}",
            "parameters": [{"key": "knowledge", "optional": False}],
            "tavily_api_key": "tavily-key",
        },
        meta_data_filter=None,
        top_n=6,
        similarity_threshold=0.2,
        vector_similarity_weight=0.3,
        search_mode=None,
    )
    return _PipelineProbe(
        dialog=dialog,
        messages=[{"role": "user", "content": "question"}],
        tavily_calls=tavily_calls,
    )


def _patch_web_search_decision(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> list[tuple[dict[str, Any], object]]:
    calls: list[tuple[dict[str, Any], object]] = []

    def decide(prompt_config: dict[str, Any], internet: object = None) -> bool:
        calls.append((prompt_config, internet))
        return enabled

    monkeypatch.setattr(dialog_service, "_should_use_web_search", decide)
    return calls


@pytest.mark.parametrize(
    ("internet", "expected"),
    [
        pytest.param(None, False, id="none"),
        pytest.param(False, False, id="bool-false"),
        pytest.param(0, False, id="int-zero"),
        pytest.param(0.0, False, id="float-zero"),
        pytest.param("", False, id="empty-string"),
        pytest.param(" false ", False, id="string-false"),
        pytest.param("no", False, id="string-no"),
        pytest.param("OFF", False, id="string-off"),
        pytest.param("unknown", False, id="unknown-string"),
        pytest.param(2, False, id="other-number"),
        pytest.param(True, True, id="bool-true"),
        pytest.param(1, True, id="int-one"),
        pytest.param(1.0, True, id="float-one"),
        pytest.param(" true ", True, id="string-true"),
        pytest.param("1", True, id="string-one"),
        pytest.param("YES", True, id="string-yes"),
        pytest.param("on", True, id="string-on"),
    ],
)
def test_should_use_web_search_requires_explicit_opt_in(
    internet: object,
    expected: bool,
) -> None:
    assert (
        dialog_service._should_use_web_search(
            {"tavily_api_key": "tavily-key"},
            internet,
        )
        is expected
    )


def test_should_use_web_search_defaults_off_and_requires_key() -> None:
    assert dialog_service._should_use_web_search({"tavily_api_key": "tavily-key"}) is False
    assert dialog_service._should_use_web_search({}, True) is False
    assert dialog_service._should_use_web_search({"tavily_api_key": ""}, True) is False


@pytest.mark.parametrize(
    ("internet_enabled", "api_key", "expected_calls"),
    [
        pytest.param(False, "tavily-key", 0, id="disabled-with-key"),
        pytest.param(True, "tavily-key", 1, id="enabled-with-key"),
        pytest.param(True, "", 0, id="enabled-without-key"),
    ],
)
async def test_deep_researcher_web_retrieval_requires_capability(
    monkeypatch: pytest.MonkeyPatch,
    internet_enabled: bool,
    api_key: str,
    expected_calls: int,
) -> None:
    tavily_calls: list[tuple[str, str]] = []

    class TavilySpy:
        def __init__(self, key: str) -> None:
            self.api_key = key

        def retrieve_chunks(self, question: str) -> dict[str, list[dict[str, str]]]:
            tavily_calls.append((self.api_key, question))
            return {
                "chunks": [{"chunk_id": "web-chunk"}],
                "doc_aggs": [{"doc_id": "web-doc"}],
            }

    async def retrieve_from_kb(
        *,
        question: str,
    ) -> dict[str, list[dict[str, str]]]:
        assert question == "search query"
        return {
            "chunks": [{"chunk_id": "kb-chunk"}],
            "doc_aggs": [{"doc_id": "kb-doc"}],
        }

    monkeypatch.setattr(deep_research_module, "Tavily", TavilySpy)
    reasoner = deep_research_module.TreeStructuredQueryDecompositionRetrieval(
        _DeepResearchBundle(),
        {"tavily_api_key": api_key},
        partial(retrieve_from_kb),
        internet_enabled=internet_enabled,
    )

    result = await reasoner._retrieve_information("search query")

    assert len(tavily_calls) == expected_calls
    assert result["total"] == 0
    expected_chunk_ids = ["kb-chunk"]
    expected_doc_ids = ["kb-doc"]
    if expected_calls:
        expected_chunk_ids.append("web-chunk")
        expected_doc_ids.append("web-doc")
    assert [chunk["chunk_id"] for chunk in result["chunks"]] == expected_chunk_ids
    assert [doc["doc_id"] for doc in result["doc_aggs"]] == expected_doc_ids


async def test_deep_researcher_defaults_web_retrieval_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tavily_calls: list[str] = []

    class TavilySpy:
        def __init__(self, _api_key: str) -> None:
            pass

        def retrieve_chunks(self, question: str) -> dict[str, list[Any]]:
            tavily_calls.append(question)
            return {"chunks": [], "doc_aggs": []}

    monkeypatch.setattr(deep_research_module, "Tavily", TavilySpy)
    reasoner = deep_research_module.TreeStructuredQueryDecompositionRetrieval(
        _DeepResearchBundle(),
        {"tavily_api_key": "tavily-key"},
    )

    await reasoner._retrieve_information("search query")

    assert tavily_calls == []


async def test_deep_researcher_accumulates_retrieval_totals() -> None:
    reasoner = deep_research_module.TreeStructuredQueryDecompositionRetrieval(
        _DeepResearchBundle(),
        {},
    )
    chunk_info = {
        "total": 2,
        "chunks": [{"chunk_id": "existing"}],
        "doc_aggs": [{"doc_id": "existing-doc"}],
    }
    new_info = {
        "total": 3,
        "chunks": [{"chunk_id": "new"}],
        "doc_aggs": [{"doc_id": "new-doc"}],
    }

    await reasoner._async_update_chunk_info(chunk_info, new_info)

    assert chunk_info["total"] == 5
    assert [chunk["chunk_id"] for chunk in chunk_info["chunks"]] == ["existing", "new"]
    assert [doc["doc_id"] for doc in chunk_info["doc_aggs"]] == ["existing-doc", "new-doc"]


async def test_deep_researcher_always_emits_end_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoner = deep_research_module.TreeStructuredQueryDecompositionRetrieval(
        _DeepResearchBundle(),
        {},
    )
    events: list[str] = []

    async def fail_research(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("research failed")

    async def record_event(message: str) -> None:
        events.append(message)

    monkeypatch.setattr(reasoner, "_research", fail_research)

    with pytest.raises(RuntimeError, match="research failed"):
        await reasoner.research(
            {"total": 0, "chunks": [], "doc_aggs": []},
            "question",
            "query",
            callback=record_event,
        )

    assert events == ["<START_DEEP_RESEARCH>", "<END_DEEP_RESEARCH>"]


async def test_deep_research_event_bridge_emits_one_ordered_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoner = deep_research_module.TreeStructuredQueryDecompositionRetrieval(
        _DeepResearchBundle(),
        {},
    )

    async def fake_research(
        chunk_info: dict[str, Any],
        question: str,
        query: str,
        depth: int = 3,
        callback: Any = None,
    ) -> None:
        assert question == query == "question"
        assert callback is not None
        await callback("<START_DEEP_RESEARCH>")
        await callback("searching")
        await callback("<END_DEEP_RESEARCH>")

    monkeypatch.setattr(reasoner, "research", fake_research)

    events = [
        event
        async for event in dialog_service._deep_research_events(
            reasoner,
            {"total": 0, "chunks": [], "doc_aggs": []},
            "question",
        )
    ]

    assert events == [
        "<START_DEEP_RESEARCH><br/>",
        "searching<br/>",
        "<END_DEEP_RESEARCH><br/>",
    ]


async def test_deep_research_event_bridge_propagates_failure_after_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoner = deep_research_module.TreeStructuredQueryDecompositionRetrieval(
        _DeepResearchBundle(),
        {},
    )
    events: list[str] = []

    async def fake_research(
        chunk_info: dict[str, Any],
        question: str,
        query: str,
        depth: int = 3,
        callback: Any = None,
    ) -> None:
        assert question == query == "question"
        assert callback is not None
        await callback("<START_DEEP_RESEARCH>")
        await callback("<END_DEEP_RESEARCH>")
        raise RuntimeError("research failed")

    monkeypatch.setattr(reasoner, "research", fake_research)

    with pytest.raises(RuntimeError, match="research failed"):
        async for event in dialog_service._deep_research_events(
            reasoner,
            {"total": 0, "chunks": [], "doc_aggs": []},
            "question",
        ):
            events.append(event)

    assert events == [
        "<START_DEEP_RESEARCH><br/>",
        "<END_DEEP_RESEARCH><br/>",
    ]


async def test_deep_research_event_bridge_cancels_on_early_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoner = deep_research_module.TreeStructuredQueryDecompositionRetrieval(
        _DeepResearchBundle(),
        {},
    )
    cancelled = asyncio.Event()

    async def fake_research(
        chunk_info: dict[str, Any],
        question: str,
        query: str,
        depth: int = 3,
        callback: Any = None,
    ) -> None:
        assert question == query == "question"
        assert callback is not None
        await callback("<START_DEEP_RESEARCH>")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(reasoner, "research", fake_research)
    events = dialog_service._deep_research_events(
        reasoner,
        {"total": 0, "chunks": [], "doc_aggs": []},
        "question",
    )

    assert await anext(events) == "<START_DEEP_RESEARCH><br/>"
    await asyncio.wait_for(events.aclose(), timeout=0.5)

    assert cancelled.is_set()


def test_sync_event_bridge_cancels_on_early_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoner = deep_research_module.TreeStructuredQueryDecompositionRetrieval(
        _DeepResearchBundle(),
        {},
    )
    cancelled: list[bool] = []

    async def fake_research(
        chunk_info: dict[str, Any],
        question: str,
        query: str,
        depth: int = 3,
        callback: Any = None,
    ) -> None:
        assert question == query == "question"
        assert callback is not None
        await callback("<START_DEEP_RESEARCH>")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(reasoner, "research", fake_research)
    events = dialog_service.sync_async_generator(
        dialog_service._deep_research_events(
            reasoner,
            {"total": 0, "chunks": [], "doc_aggs": []},
            "question",
        )
    )

    assert next(events) == "<START_DEEP_RESEARCH><br/>"
    events.close()

    assert cancelled == [True]


def test_chat_without_retrieval_source_continues_to_generation(
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
    dialog_pipeline_stubs: _PipelineProbe,
) -> None:
    dialog_pipeline_stubs.dialog.kb_ids = []
    dialog_pipeline_stubs.dialog.prompt_config["empty_response"] = "no knowledge"
    dialog_pipeline_stubs.dialog.prompt_config["reasoning"] = True
    gate_calls = _patch_web_search_decision(monkeypatch, False)

    def fail_reasoner(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("DeepResearcher should not run without a retrieval source")

    monkeypatch.setattr(dialog_service, "DeepResearcher", fail_reasoner)

    with pytest.raises(_GenerationReached):
        list(
            dialog_service.chat(
                dialog_pipeline_stubs.dialog,
                dialog_pipeline_stubs.messages,
                db,
                stream=False,
            )
        )

    assert gate_calls == [(dialog_pipeline_stubs.dialog.prompt_config, None)]
    assert dialog_pipeline_stubs.tavily_calls == []


def test_sync_chat_reasoning_uses_shared_event_bridge(
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
    dialog_pipeline_stubs: _PipelineProbe,
) -> None:
    gate_calls = _patch_web_search_decision(monkeypatch, False)

    async def fake_research(
        self: Any,
        chunk_info: dict[str, Any],
        question: str,
        query: str,
        depth: int = 3,
        callback: Any = None,
    ) -> None:
        assert question == query == "question"
        assert callback is not None
        await callback("<START_DEEP_RESEARCH>")
        await callback("<END_DEEP_RESEARCH>")

    monkeypatch.setattr(
        deep_research_module.TreeStructuredQueryDecompositionRetrieval,
        "research",
        fake_research,
    )

    with pytest.raises(_GenerationReached):
        list(
            dialog_service.chat(
                dialog_pipeline_stubs.dialog,
                dialog_pipeline_stubs.messages,
                db,
                stream=False,
                reasoning=True,
            )
        )

    assert gate_calls == [(dialog_pipeline_stubs.dialog.prompt_config, None)]


def test_chat_separates_extracted_keywords_from_question(
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
    dialog_pipeline_stubs: _PipelineProbe,
) -> None:
    dialog_pipeline_stubs.dialog.prompt_config["keyword"] = True
    _patch_web_search_decision(monkeypatch, True)

    async def fake_keyword_extraction(*_args: Any, **_kwargs: Any) -> str:
        return "alpha,beta"

    monkeypatch.setattr(dialog_service, "keyword_extraction", fake_keyword_extraction)

    with pytest.raises(_GenerationReached):
        list(
            dialog_service.chat(
                dialog_pipeline_stubs.dialog,
                dialog_pipeline_stubs.messages,
                db,
                stream=False,
            )
        )

    assert dialog_pipeline_stubs.tavily_calls == [("tavily-key", "question,alpha,beta")]


async def test_async_chat_without_retrieval_source_continues_to_generation(
    monkeypatch: pytest.MonkeyPatch,
    async_db: AsyncSession,
    dialog_pipeline_stubs: _PipelineProbe,
) -> None:
    dialog_pipeline_stubs.dialog.kb_ids = []
    dialog_pipeline_stubs.dialog.prompt_config["empty_response"] = "no knowledge"
    dialog_pipeline_stubs.dialog.prompt_config["reasoning"] = True
    gate_calls = _patch_web_search_decision(monkeypatch, False)

    def fail_reasoner(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("DeepResearcher should not run without a retrieval source")

    monkeypatch.setattr(dialog_service, "DeepResearcher", fail_reasoner)

    with pytest.raises(_GenerationReached):
        async for _answer in dialog_service.async_chat(
            dialog_pipeline_stubs.dialog,
            dialog_pipeline_stubs.messages,
            async_db,
            stream=False,
        ):
            pass

    assert gate_calls == [(dialog_pipeline_stubs.dialog.prompt_config, None)]
    assert dialog_pipeline_stubs.tavily_calls == []


async def test_async_chat_separates_extracted_keywords_from_question(
    monkeypatch: pytest.MonkeyPatch,
    async_db: AsyncSession,
    dialog_pipeline_stubs: _PipelineProbe,
) -> None:
    dialog_pipeline_stubs.dialog.prompt_config["keyword"] = True
    _patch_web_search_decision(monkeypatch, True)

    async def fake_keyword_extraction(*_args: Any, **_kwargs: Any) -> str:
        return "alpha,beta"

    monkeypatch.setattr(dialog_service, "keyword_extraction", fake_keyword_extraction)

    with pytest.raises(_GenerationReached):
        async for _answer in dialog_service.async_chat(
            dialog_pipeline_stubs.dialog,
            dialog_pipeline_stubs.messages,
            async_db,
            stream=False,
        ):
            pass

    assert dialog_pipeline_stubs.tavily_calls == [("tavily-key", "question,alpha,beta")]


@pytest.mark.parametrize("enabled", [False, True])
def test_chat_normal_retrieval_uses_shared_web_search_decision(
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
    dialog_pipeline_stubs: _PipelineProbe,
    enabled: bool,
) -> None:
    gate_calls = _patch_web_search_decision(monkeypatch, enabled)

    with pytest.raises(_GenerationReached):
        list(
            dialog_service.chat(
                dialog_pipeline_stubs.dialog,
                dialog_pipeline_stubs.messages,
                db,
                stream=False,
                internet=enabled,
            )
        )

    assert gate_calls == [
        (dialog_pipeline_stubs.dialog.prompt_config, enabled),
    ]
    assert len(dialog_pipeline_stubs.tavily_calls) == int(enabled)


@pytest.mark.parametrize("enabled", [False, True])
async def test_async_chat_normal_retrieval_uses_shared_web_search_decision(
    monkeypatch: pytest.MonkeyPatch,
    async_db: AsyncSession,
    dialog_pipeline_stubs: _PipelineProbe,
    enabled: bool,
) -> None:
    gate_calls = _patch_web_search_decision(monkeypatch, enabled)

    with pytest.raises(_GenerationReached):
        async for _answer in dialog_service.async_chat(
            dialog_pipeline_stubs.dialog,
            dialog_pipeline_stubs.messages,
            async_db,
            stream=False,
            internet=enabled,
        ):
            pass

    assert gate_calls == [
        (dialog_pipeline_stubs.dialog.prompt_config, enabled),
    ]
    assert len(dialog_pipeline_stubs.tavily_calls) == int(enabled)


@pytest.mark.parametrize("enabled", [False, True])
def test_chat_reasoner_receives_explicit_web_search_decision(
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
    dialog_pipeline_stubs: _PipelineProbe,
    enabled: bool,
) -> None:
    captured: dict[str, Any] = {}
    gate_calls = _patch_web_search_decision(monkeypatch, enabled)

    def build_reasoner(
        _chat_mdl: Any,
        prompt_config: dict[str, Any],
        *args: Any,
        internet_enabled: bool,
        **kwargs: Any,
    ) -> NoReturn:
        captured["internet_enabled"] = internet_enabled
        captured["tavily_api_key"] = prompt_config.get("tavily_api_key")
        raise _ReasonerConstructed

    monkeypatch.setattr(dialog_service, "DeepResearcher", build_reasoner)

    with pytest.raises(_ReasonerConstructed):
        list(
            dialog_service.chat(
                dialog_pipeline_stubs.dialog,
                dialog_pipeline_stubs.messages,
                db,
                stream=False,
                internet=enabled,
                reasoning=True,
            )
        )

    assert gate_calls == [
        (dialog_pipeline_stubs.dialog.prompt_config, enabled),
    ]
    assert captured == {
        "internet_enabled": enabled,
        "tavily_api_key": "tavily-key",
    }


@pytest.mark.parametrize("enabled", [False, True])
async def test_async_chat_reasoner_receives_explicit_web_search_decision(
    monkeypatch: pytest.MonkeyPatch,
    async_db: AsyncSession,
    dialog_pipeline_stubs: _PipelineProbe,
    enabled: bool,
) -> None:
    captured: dict[str, Any] = {}
    gate_calls = _patch_web_search_decision(monkeypatch, enabled)

    def build_reasoner(
        _chat_mdl: Any,
        prompt_config: dict[str, Any],
        *args: Any,
        internet_enabled: bool,
        **kwargs: Any,
    ) -> NoReturn:
        captured["internet_enabled"] = internet_enabled
        captured["tavily_api_key"] = prompt_config.get("tavily_api_key")
        raise _ReasonerConstructed

    monkeypatch.setattr(dialog_service, "DeepResearcher", build_reasoner)

    with pytest.raises(_ReasonerConstructed):
        async for _answer in dialog_service.async_chat(
            dialog_pipeline_stubs.dialog,
            dialog_pipeline_stubs.messages,
            async_db,
            stream=False,
            internet=enabled,
            reasoning=True,
        ):
            pass

    assert gate_calls == [
        (dialog_pipeline_stubs.dialog.prompt_config, enabled),
    ]
    assert captured == {
        "internet_enabled": enabled,
        "tavily_api_key": "tavily-key",
    }
