import pytest

from core.llm.cv import GptV4, QWenCV


@pytest.mark.anyio
async def test_qwen_async_chat_without_video_delegates_to_gptv4(monkeypatch):
    async def fake_async_chat(self, system, history, gen_conf, images=None, **kwargs):
        return "ok", 7

    monkeypatch.setattr(GptV4, "async_chat", fake_async_chat)

    model = object.__new__(QWenCV)

    answer, tokens = await QWenCV.async_chat(
        model,
        "system prompt",
        [{"role": "user", "content": "describe this image"}],
        {"temperature": 0.1},
        images=[b"\x89PNG\r\n\x1a\n"],
    )

    assert answer == "ok"
    assert tokens == 7


def test_qwen_resolve_video_prompt_prefers_latest_user_text():
    model = object.__new__(QWenCV)

    prompt = model._resolve_video_prompt(
        "system prompt",
        [
            {"role": "assistant", "content": "previous"},
            {"role": "user", "content": [{"type": "text", "text": "last user question"}]},
        ],
    )

    assert prompt == "last user question"
