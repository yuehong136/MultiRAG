"""A zero history window classifies only the current query."""

import json
from typing import Any

import pytest

from agent.canvas import Canvas
from agent.component.categorize import Categorize, CategorizeParam
from tests.support.canvas_path_reset import paused_dsl


@pytest.mark.parametrize("window", [0, 1, 13])
def test_categorize_accepts_nonnegative_history_window(window: int) -> None:
    param = CategorizeParam()
    param.category_description = {"Other": {"to": ["Message:answer"]}}
    param.message_history_window_size = window
    param.check()


@pytest.mark.parametrize("window", [-1, 0.0, "0", None, False, True])
def test_categorize_rejects_invalid_history_window(window: Any) -> None:
    param = CategorizeParam()
    param.message_history_window_size = window
    with pytest.raises(ValueError, match="non-negative integer"):
        param.check()


async def test_zero_window_omits_saved_history_and_uses_current_query(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.component import categorize

    dsl = paused_dsl()
    dsl["history"] = [["user", "private old question"], ["assistant", {"content": "private old answer"}]]
    dsl["globals"] = {"sys.history": ["private saved history"], "sys.query": "current question"}
    canvas = Canvas(json.dumps(dsl), tenant_id="unit-owner")
    prompts: list[str] = []

    class Model:
        async def async_chat(self, system: str, history: list[dict[str, str]], config: dict[str, Any]) -> str:
            prompts.append(history[0]["content"])
            return "Other"

    async def build(*args: Any, **kwargs: Any) -> Model:
        return Model()

    monkeypatch.setattr(categorize, "build_named_bundle_async", build)
    monkeypatch.setattr(Canvas, "is_canceled", lambda self: False)
    param = CategorizeParam()
    param.message_history_window_size = 0
    param.category_description = {"Other": {"to": ["Message:answer"]}}
    component = Categorize(canvas, "Categorize:test", param)
    try:
        assert canvas.get_history(0) == []
        assert canvas.get_history(1) == [{"role": "user", "content": "private old question"}, {"role": "assistant", "content": "private old answer"}]
        await component._invoke_async()
        assert len(prompts) == 1 and "current question" in prompts[0]
        assert "private" not in prompts[0]
        assert canvas.history == dsl["history"] and canvas.globals["sys.history"] == ["private saved history"]
        assert component.output("category_name") == "Other"
    finally:
        canvas._thread_pool.shutdown(wait=True)
