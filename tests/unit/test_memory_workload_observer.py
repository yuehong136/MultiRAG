"""Usage observations preserve unknowns and never fabricate provider output."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from api.db.services.llm_service import LLMBundle
from tests.support.memory_workload_models import ProviderObserver
from tests.support.resource_journal import ResourceJournal


@pytest.mark.parametrize(
    ("gross", "cached", "expected_input", "expected_cache"),
    [(10, 0, 10, 0), (10, 4, 6, 4), (10, None, None, None), (10, 20, None, None), (10, -1, None, None), (10, True, None, None), (10, False, None, None), (0, 0, 0, 0)],
)
def test_chat_cache_usage_is_not_guessed_or_reasoning_double_counted(gross: int, cached: int | None, expected_input: int | None, expected_cache: int | None) -> None:
    observer = ProviderObserver(ResourceJournal("observer-unit"))
    row: dict[str, Any] = {}
    observer._chat_usage(
        row,
        SimpleNamespace(
            model="gpt-6.1-terra",
            usage={"prompt_tokens": gross, "completion_tokens": 5, "total_tokens": gross + 5, "prompt_tokens_details": {"cached_tokens": cached}, "completion_tokens_details": {"reasoning_tokens": 2}},
        ),
    )
    assert row["usage"]["input_tokens"] == expected_input
    assert row["usage"]["cache_read_input_tokens"] == expected_cache
    assert row["provider_usage_raw"]["prompt_tokens_details"]["cached_tokens"] == cached
    assert row["usage"]["output_tokens"] == 5
    assert row["usage"]["cache_creation_input_tokens"] is None
    assert row["reasoning_tokens"] == 2 and row["reasoning_tokens_included_in_output"] is True
    assert row["provider_total_tokens"] == gross + 5


@pytest.mark.parametrize("failed", [False, True])
def test_openai_embedding_boundary_preserves_response_and_failed_attempt(failed: bool) -> None:
    calls: list[dict[str, Any]] = []
    response = SimpleNamespace(usage={"total_tokens": 9, "prompt_tokens": 9}, model="text-embedding-3-small", data=[object()])

    def sdk(**options: Any) -> Any:
        calls.append(options)
        if failed:
            raise TimeoutError("synthetic provider boundary")
        return response

    client = SimpleNamespace(max_retries=2, embeddings=SimpleNamespace(create=sdk))
    observer = ProviderObserver(ResourceJournal("observer-unit"))
    observer.observe_openai_embedding(cast(LLMBundle, SimpleNamespace(mdl=SimpleNamespace(client=client))), "text-embedding-3-small@OpenAI", "config-fingerprint")
    if failed:
        with pytest.raises(TimeoutError):
            client.embeddings.create(model="text-embedding-3-small", input=["synthetic"])
    else:
        assert client.embeddings.create(model="text-embedding-3-small", input=["synthetic"]) is response
    assert client.max_retries == 0 and len(calls) == 1 and len(observer.attempts) == 1
    row = observer.attempts[0]
    assert row["request_model"] == "text-embedding-3-small"
    assert row["state"] == ("failed" if failed else "completed")
    assert row["provider_total_tokens"] == (None if failed else 9)
    assert row["usage"] == ({} if failed else {"input_tokens": 9, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
