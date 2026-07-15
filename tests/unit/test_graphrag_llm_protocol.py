import asyncio
import collections

import core.graphrag.general.extractor as extractor_module
from core.graphrag.general.mind_map_extractor import MindMapExtractor


class FakeLLM:
    llm_name = "fake-llm"
    max_length = 4096

    async def async_chat(self, system, history: list[dict[str, str]], gen_conf=None, **kwargs):
        return "{}"


class TupleLLM:
    llm_name = "tuple-llm"
    max_length = 4096

    async def async_chat(self, system, history: list[dict[str, str]], gen_conf=None, **kwargs):
        return "{}", 0


def test_mind_map_extractor_accepts_protocol_based_llm():
    extractor = MindMapExtractor(FakeLLM())

    assert extractor._llm.llm_name == "fake-llm"
    assert extractor._llm.max_length == 4096


async def test_mind_map_extractor_accepts_tuple_chat_response(monkeypatch):
    extractor = MindMapExtractor(TupleLLM())
    monkeypatch.setattr(extractor_module, "get_llm_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(extractor_module, "set_llm_cache", lambda *args, **kwargs: None)

    assert await extractor._async_chat("system", [{"role": "user", "content": "Output:"}], {}) == "{}"


def test_mind_map_extractor_todict_supports_list_leaves():
    extractor = MindMapExtractor(FakeLLM())
    layer = collections.OrderedDict(
        {
            "顶层": collections.OrderedDict(
                {
                    "部分A": [
                        "点1",
                        "点2",
                    ]
                }
            )
        }
    )

    assert extractor._todict(layer) == {"顶层": {"部分A": ["点1", "点2"]}}


def test_mind_map_extractor_be_children_supports_list_leaves():
    extractor = MindMapExtractor(FakeLLM())

    assert extractor._be_children(["点1", "点2"], {"顶层"}) == [
        {"id": "点1", "children": []},
        {"id": "点2", "children": []},
    ]


def test_mind_map_extractor_process_document_returns_none(monkeypatch):
    extractor = MindMapExtractor(FakeLLM())
    out_res = []

    async def fake_async_chat(*args, **kwargs):
        return "# 顶层\n## 部分A\n- 点1\n- 点2"

    monkeypatch.setattr(extractor, "_async_chat", fake_async_chat)

    result = asyncio.run(extractor._process_document("课堂纪要", {}, out_res))

    assert result is None
    assert out_res == [{"顶层": {"部分A": ["点1", "点2"]}}]
