# FuturMix

FuturMix 使用现有租户模型配置与 OpenAI 兼容适配器。模型目录包含 8 个 chat、
1 个 image2text、2 个 embedding、1 个 rerank、1 个 speech2text 和 2 个 TTS 条目。
目录中的名称表示可配置项，实际可用性取决于网关账户及其服务能力。

API Key 必填；`api_base` 留空时使用 `https://futurmix.ai/v1`，也可填写自定义网关
基址。Web 的模型提供商列表读取后端目录，通用 API Key 表单支持可选 Base URL。
六类运行入口均经 `TenantLLMService.model_instance` 选择 `core/llm/` 中的适配器。

| 能力 | 请求路径 | 本地处理 |
|---|---|---|
| Chat / Vision | `/chat/completions` | OpenAI SDK；Vision 包含图像消息 |
| Embedding | `/embeddings` | 文本批次与 query 向量、服务 token 统计 |
| Rerank | `/rerank` | 按响应 index 还原输入顺序，沿用本地分数归一化 |
| Speech2Text | `/audio/transcriptions` | multipart 音频文件，保留自定义基址 |
| TTS | `/audio/speech` | 返回音频字节流 |

Rerank 接受基址或以 `/rerank` 结尾的完整端点；公共 URL helper 避免重复追加。
公共归一化方法位于 rerank `Base`，供兼容适配器调用。

`tests/unit/test_futurmix_provider.py` 在 HTTP 边界替换外部服务，验证租户模型构造、
默认/空/自定义 URL、鉴权、请求内容及响应处理。六类能力均未使用真实 FuturMix
账户验证；这不构成真实模型可用性、流式聊天、工具调用或音频质量验收。
