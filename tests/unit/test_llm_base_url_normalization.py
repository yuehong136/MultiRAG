"""base_url 规范化的钉板测试。

背景：历史写法 `urljoin(base_url, "v1")` 把 URL 末段当文件名替换，导致带版本路径的
厂商（智谱 `/api/paas/v4`）无论加不加尾斜杠都拼不出真实端点：

- `https://open.bigmodel.cn/api/paas/v4`  → `.../api/paas/v1`（v4 被顶替）
- `https://open.bigmodel.cn/api/paas/v4/` → `.../api/paas/v4/v1`（重复）

这里既钉 helper 的语义，也钉真实注册表里被激活的那几个类的最终请求地址。
"""

import pytest

from common.url_utils import append_path_segment, ensure_api_version, has_api_version, strip_trailing_segment
from core.llm import LITELLM_PROVIDER_PREFIX, ChatModel, CvModel, EmbeddingModel, RerankModel, SupportedLiteLLMProvider

ZHIPU_V4 = "https://open.bigmodel.cn/api/paas/v4"
DASHSCOPE_V1 = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ZHIPU_ANTHROPIC = "https://open.bigmodel.cn/api/anthropic"


class TestEnsureApiVersion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # 已带版本段：一律原样（这是修复的核心，旧写法会把 v4 改成 v1）
            (ZHIPU_V4, ZHIPU_V4),
            (f"{ZHIPU_V4}/", ZHIPU_V4),
            (DASHSCOPE_V1, DASHSCOPE_V1),
            (f"{DASHSCOPE_V1}/", DASHSCOPE_V1),
            ("https://api.openai.com/v1", "https://api.openai.com/v1"),
            ("https://ark.cn-beijing.volces.com/api/v3", "https://ark.cn-beijing.volces.com/api/v3"),
            ("https://generativelanguage.googleapis.com/v1beta", "https://generativelanguage.googleapis.com/v1beta"),
            # 未带版本段：补 /v1
            ("http://localhost:11434", "http://localhost:11434/v1"),
            ("http://localhost:11434/", "http://localhost:11434/v1"),
            # 关键回归：带路径前缀的自建部署，旧写法会把 /xinference 整段吃掉
            ("http://host:9997/xinference", "http://host:9997/xinference/v1"),
            ("https://api.longcat.chat/openai", "https://api.longcat.chat/openai/v1"),
            # 空值交给调用方处理，不在这里报错
            ("", ""),
            (None, ""),
            ("   ", ""),
        ],
    )
    def test_normalizes(self, raw: str | None, expected: str) -> None:
        assert ensure_api_version(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "https://generativelanguage.googleapis.com/v1beta2",
            "https://gw.internal/api/v2alpha1",
            "https://gw.internal/api/v1.5",
            "https://GW.internal/API/V1",
        ],
    )
    def test_less_common_version_shapes_are_recognized(self, raw: str) -> None:
        assert has_api_version(raw) is True
        assert ensure_api_version(raw) == raw

    @pytest.mark.parametrize("raw", ["https://gw.internal/v2ray", "https://gw.internal/vision"])
    def test_version_lookalikes_still_get_v1(self, raw: str) -> None:
        assert ensure_api_version(raw) == f"{raw}/v1"

    def test_custom_version(self) -> None:
        assert ensure_api_version("https://gw.internal/api", version="v2") == "https://gw.internal/api/v2"

    def test_version_lookalike_in_host_does_not_count(self) -> None:
        assert has_api_version("https://v1.example.com") is False
        assert ensure_api_version("https://v1.example.com") == "https://v1.example.com/v1"

    def test_query_string_is_preserved(self) -> None:
        assert ensure_api_version("https://gw.internal/api?tenant=a") == "https://gw.internal/api/v1?tenant=a"


class TestAppendPathSegment:
    @pytest.mark.parametrize(
        ("raw", "segment", "expected"),
        [
            (ZHIPU_V4, "rerank", f"{ZHIPU_V4}/rerank"),
            (f"{ZHIPU_V4}/", "rerank", f"{ZHIPU_V4}/rerank"),
            (f"{ZHIPU_V4}/rerank", "rerank", f"{ZHIPU_V4}/rerank"),
            # 旧写法 urljoin(base, "/rerank") 会丢掉整个 path，这里必须保留
            ("http://host:8080/localai", "rerank", "http://host:8080/localai/rerank"),
            ("http://host:9997/v1", "v1/rerank", "http://host:9997/v1/v1/rerank"),
            ("", "rerank", ""),
            (ZHIPU_V4, "", ZHIPU_V4),
        ],
    )
    def test_appends(self, raw: str, segment: str, expected: str) -> None:
        assert append_path_segment(raw, segment) == expected

    def test_query_string_does_not_defeat_the_already_appended_check(self) -> None:
        # 判定必须落在 path 上：对整串 endswith 会因为 query 而重复追加
        assert append_path_segment("http://host/v1/rerank?tenant=a", "rerank") == "http://host/v1/rerank?tenant=a"


class TestStripTrailingSegment:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://api.anthropic.com/v1", "https://api.anthropic.com"),
            ("https://api.anthropic.com/v1/", "https://api.anthropic.com"),
            ("https://api.anthropic.com", "https://api.anthropic.com"),
            (ZHIPU_ANTHROPIC, ZHIPU_ANTHROPIC),
            # 已经是完整 messages 端点时不能动（LiteLLM 靠这个后缀判断是否补全）
            ("https://gw.internal/anthropic/v1/messages", "https://gw.internal/anthropic/v1/messages"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_strips_only_bare_trailing_v1(self, raw: str | None, expected: str) -> None:
        assert strip_trailing_segment(raw, "v1") == expected


class TestActivatedOpenAICompatibleClasses:
    """注册表里真正被 TenantLLMService 使用的那几个类，最终请求地址必须落在 v4 上。"""

    @pytest.mark.parametrize("raw", [ZHIPU_V4, f"{ZHIPU_V4}/"])
    def test_chat_and_embedding_and_vision_agree_on_zhipu_v4(self, raw: str) -> None:
        expected = f"{ZHIPU_V4}/"

        chat = ChatModel["OpenAI-API-Compatible"](key="k", model_name="glm-4.6___OpenAI-API", base_url=raw)
        embed = EmbeddingModel["OpenAI-API-Compatible"](key="k", model_name="embedding-3___OpenAI-API", base_url=raw)
        vision = CvModel["OpenAI-API-Compatible"](key="k", model_name="glm-4.6v___OpenAI-API", base_url=raw)

        assert str(chat.client.base_url) == expected
        assert str(embed.client.base_url) == expected
        assert str(vision.client.base_url) == expected

    @pytest.mark.parametrize("raw", [ZHIPU_V4, f"{ZHIPU_V4}/"])
    def test_rerank_endpoint(self, raw: str) -> None:
        rerank = RerankModel["OpenAI-API-Compatible"](key="k", model_name="rerank___OpenAI-API", base_url=raw)
        assert rerank.base_url == f"{ZHIPU_V4}/rerank"

    def test_dashscope_trailing_slash_no_longer_doubles_v1(self) -> None:
        embed = EmbeddingModel["OpenAI-API-Compatible"](key="k", model_name="text-embedding-v4", base_url=f"{DASHSCOPE_V1}/")
        assert str(embed.client.base_url) == f"{DASHSCOPE_V1}/"

    def test_path_prefixed_self_hosted_deployment_keeps_its_prefix(self) -> None:
        embed = EmbeddingModel["Xinference"](key="k", model_name="bge-m3", base_url="http://host:9997/xinference")
        rerank = RerankModel["Xinference"](key="k", model_name="bge-reranker", base_url="http://host:9997/xinference")

        assert str(embed.client.base_url) == "http://host:9997/xinference/v1/"
        assert rerank.base_url == "http://host:9997/xinference/v1/rerank"

    @pytest.mark.parametrize(
        "raw",
        ["http://host:9997", "http://host:9997/", "http://host:9997/v1", "http://host:9997/rerank", "http://host:9997/v1/rerank"],
    )
    def test_rerank_converges_no_matter_how_much_of_the_endpoint_user_typed(self, raw: str) -> None:
        assert RerankModel["Xinference"](key="k", model_name="bge-reranker", base_url=raw).base_url == "http://host:9997/v1/rerank"

    @pytest.mark.parametrize("model_name", ["nvidia/nv-rerankqa-mistral-4b-v3", "nvidia/rerank-qa-mistral-4b", "nvidia/llama-3.2-nv-rerankqa-1b-v2"])
    def test_every_seeded_nvidia_rerank_model_gets_an_endpoint(self, model_name: str) -> None:
        # 目录里有三个 rerank 模型，原实现只给两个硬编码名赋 base_url，其余 AttributeError
        assert RerankModel["NVIDIA"](key="k", model_name=model_name).base_url.startswith("https://ai.api.nvidia.com/v1/retrieval/nvidia/")

    def test_bare_host_agrees_across_all_four_channels(self) -> None:
        raw = "http://vllm:8000"
        chat = ChatModel["OpenAI-API-Compatible"](key="k", model_name="qwen3", base_url=raw)
        embed = EmbeddingModel["OpenAI-API-Compatible"](key="k", model_name="bge-m3", base_url=raw)
        vision = CvModel["OpenAI-API-Compatible"](key="k", model_name="qwen3-vl", base_url=raw)

        assert str(chat.client.base_url) == "http://vllm:8000/v1/"
        assert str(chat.async_client.base_url) == "http://vllm:8000/v1/"
        assert str(embed.client.base_url) == "http://vllm:8000/v1/"
        assert str(vision.client.base_url) == "http://vllm:8000/v1/"

    @pytest.mark.parametrize("factory", ["LocalAI", "LM-Studio", "Xinference"])
    def test_sync_and_async_clients_never_disagree(self, factory: str) -> None:
        # LocalAIChat 曾先用未规范化的 base_url 调 super()，导致 async_client 缺 /v1，
        # 而添加模型的探测与生产流式都只走 async_client
        mdl = ChatModel[factory](key="k", model_name="m", base_url="http://host:8080")
        assert str(mdl.client.base_url) == str(mdl.async_client.base_url) == "http://host:8080/v1/"


class TestAnthropicCompatibleEndpoints:
    def test_provider_prefix_is_explicit(self) -> None:
        # 空前缀会让 LiteLLM 靠模型名反查 provider，非 claude-* 名字直接 BadRequestError
        assert LITELLM_PROVIDER_PREFIX[SupportedLiteLLMProvider.Anthropic] == "anthropic/"

    def test_third_party_model_name_gets_routed(self) -> None:
        mdl = ChatModel["Anthropic"](key="k", model_name="glm-4.6", base_url=ZHIPU_ANTHROPIC, provider="Anthropic")
        assert mdl.model_name == "anthropic/glm-4.6"
        assert mdl.base_url == ZHIPU_ANTHROPIC

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (ZHIPU_ANTHROPIC, ZHIPU_ANTHROPIC),
            ("https://dashscope.aliyuncs.com/apps/anthropic", "https://dashscope.aliyuncs.com/apps/anthropic"),
            # 用户沿 OpenAI 习惯填的 /v1 必须摘掉，否则 LiteLLM 拼出 /v1/v1/messages
            ("https://api.anthropic.com/v1", "https://api.anthropic.com"),
            ("", "https://api.anthropic.com"),
        ],
    )
    def test_base_url_normalized_for_native_protocol(self, raw: str, expected: str) -> None:
        mdl = ChatModel["Anthropic"](key="k", model_name="claude-sonnet-4-5-20250929", base_url=raw, provider="Anthropic")
        assert mdl.base_url == expected

    def test_vision_channel_strips_full_messages_endpoint(self) -> None:
        # 原生 SDK 会自己拼 /v1/messages，用户把整条端点填进来时不能二次拼接
        mdl = CvModel["Anthropic"]("k", "claude-sonnet-4-5-20250929", "Chinese", base_url=f"{ZHIPU_ANTHROPIC}/v1/messages")
        assert str(mdl.client.base_url).rstrip("/") == ZHIPU_ANTHROPIC

    def test_vision_channel_normalizes_raw_image_bytes(self) -> None:
        mdl = CvModel["Anthropic"]("k", "claude-sonnet-4-5-20250929", "Chinese")
        blocks = mdl._image_prompt("描述这张图", b"\x89PNG\r\n\x1a\nfake-bytes")

        image_block = blocks[1]
        assert image_block["type"] == "image"
        assert image_block["source"]["type"] == "base64"
        # 原始 bytes 必须先转成 base64 字符串，否则 SDK 序列化时 "bytes is not JSON serializable"
        assert isinstance(image_block["source"]["data"], str)
        assert "base64," not in image_block["source"]["data"]

    def test_vision_clean_conf_respects_caller_max_tokens(self) -> None:
        mdl = CvModel["Anthropic"]("k", "claude-opus-4-5-20251101", "Chinese")
        # 不能用类里那份陈旧的 4096/8192 猜测去夹调用方显式给的值
        assert mdl._clean_conf({"max_tokens": 32000})["max_tokens"] == 32000


class TestAnthropicVisionResponseParsing:
    """/v1/messages 的 content 是块数组，取正文不能假设第 0 块就是 text。

    开了思考的模型（kimi-k2.5、claude thinking，很多第三方兼容端点默认就开）
    第一块是 {"type": "thinking"}，直接取 ["text"] 会 KeyError('text')——
    表现是添加 image2text 模型时报一句莫名的 `'text'`。
    """

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ([{"type": "text", "text": "图中是一只猫。"}], "图中是一只猫。"),
            ([{"type": "thinking", "thinking": "嗯…"}, {"type": "text", "text": "图中是一只猫。"}], "图中是一只猫。"),
            ([{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}], "第一段\n第二段"),
            ([{"type": "tool_use", "id": "t1", "name": "x", "input": {}}, {"type": "text", "text": "结果"}], "结果"),
            ([{"type": "thinking", "thinking": "想了很久"}], ""),
            ([], ""),
        ],
    )
    def test_answer_text_skips_non_text_blocks(self, content: list[dict], expected: str) -> None:
        assert CvModel["Anthropic"]._answer_text({"content": content}) == expected

    def test_describe_reports_why_when_thinking_ate_the_budget(self) -> None:
        mdl = CvModel["Anthropic"]("k", "kimi-k2.5", "Chinese")

        class _ThinkingOnly:
            class messages:
                @staticmethod
                def create(**kwargs):
                    class _Resp:
                        @staticmethod
                        def to_dict():
                            return {"content": [{"type": "thinking", "thinking": "…"}], "stop_reason": "max_tokens", "usage": {"input_tokens": 1, "output_tokens": 2}}

                    return _Resp()

        mdl.client = _ThinkingOnly()
        with pytest.raises(ValueError, match="stop_reason=max_tokens"):
            mdl._describe([{"role": "user", "content": "hi"}])

    def test_describe_returns_text_after_thinking_block(self) -> None:
        mdl = CvModel["Anthropic"]("k", "kimi-k2.5", "Chinese")

        class _Thinking:
            class messages:
                @staticmethod
                def create(**kwargs):
                    class _Resp:
                        @staticmethod
                        def to_dict():
                            return {
                                "content": [{"type": "thinking", "thinking": "嗯…"}, {"type": "text", "text": "图中是一只猫。"}],
                                "stop_reason": "end_turn",
                                "usage": {"input_tokens": 20, "output_tokens": 30},
                            }

                    return _Resp()

        mdl.client = _Thinking()
        assert mdl._describe([{"role": "user", "content": "hi"}]) == ("图中是一只猫。", 50)

    def test_api_base_reaches_litellm(self) -> None:
        mdl = ChatModel["Anthropic"](key="k", model_name="glm-4.6", base_url=ZHIPU_ANTHROPIC, provider="Anthropic")
        args = mdl._construct_completion_args(history=[{"role": "user", "content": "hi"}], stream=False, tools=False)
        assert args["api_base"] == ZHIPU_ANTHROPIC
        assert args["model"] == "anthropic/glm-4.6"

    def test_vision_channel_accepts_positional_lang(self) -> None:
        # TenantLLMService.model_instance 按位置传 lang，缺形参会 TypeError
        mdl = CvModel["Anthropic"]("k", "claude-sonnet-4-5-20250929", "English", base_url="https://api.anthropic.com/v1")
        assert mdl.lang == "English"
        assert str(mdl.client.base_url) == "https://api.anthropic.com"

    def test_vision_clean_conf_always_sets_max_tokens(self) -> None:
        mdl = CvModel["Anthropic"]("k", "claude-sonnet-4-5-20250929", "Chinese")
        conf = mdl._clean_conf({"temperature": 0.1, "presence_penalty": 0.4, "frequency_penalty": 0.7, "max_token": 1024})
        assert conf == {"temperature": 0.1, "max_tokens": 1024}
        assert mdl._clean_conf({})["max_tokens"] == mdl.max_tokens
