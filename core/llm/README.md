# 模型入口与调用契约

正式模型注册由 [`core/llm/__init__.py`](./__init__.py) 扫描
[`chat.py`](./chat.py) 完成。DeepSeek 使用 `LiteLLMBase` 的异步入口；
[`chat_model/base.py`](./chat_model/base.py) 与 `chat_model/models/` 仍提供同步入口。
排查模型行为时先确认实际注册和调用方，不只按模型文件名定位。

## OpenAI 内置目录

2026-10-29 按 [OpenAI 模型目录](https://developers.openai.com/api/docs/models)和
[退役公告](https://developers.openai.com/api/docs/deprecations)核对。
Python 使用 `configs/llm_factories.json`，并行 Go 实现使用
`configs/models/openai.json`；两份声明的模型名、能力与上下文保持一致，沿用各自 JSON schema。
RAGFlow Go 的参照入口是 `conf/models/openai.json`，不是 Python 总目录；供应商声明仍需与官方接口核对。

内置聊天目录保留 GPT-6 / 6.1、GPT-5.6、5.5、5.4 / mini 和 5.2。
不再为新实例列出 GPT-3.5、GPT-4 系列、旧 o 系列、已弃用的 GPT-5 / 5.1 / 5.4-nano
以及 ChatGPT 专用别名。`gpt-5.2-pro` 只支持 Responses，当前 Chat Completions 适配器不列出它。
这只调整内置候选，不删除租户已保存的模型，也不更换其默认模型。

[`gpt-6.1-sol`](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、
GPT-6.1 Terra 与 GPT-6 Astra 的工具调用需要 Responses；GPT-6 Sol / Luna 的
Chat Completions 工具调用要求 `reasoning_effort=none`。当前目录的 `is_tools` 无法表达
这些条件，因此 GPT-6 系列仅声明聊天/图像能力，暂不声明工具能力。
聊天请求过滤 GPT-6 不支持的采样参数，保持调用方配置不变；接口限制见
[官方兼容说明](https://developers.openai.com/api/docs/guides/latest-model#update-api-and-model-parameters)。
Go 的 GPT-5 / GPT-6 请求同样省略采样参数，输出上限使用 `max_completion_tokens`，
显式推理强度通过 `reasoning_effort` 传递；其他模型仍使用原请求字段。

嵌入保留 text-embedding-3-small / large 与兼容用 ada-002，不自动迁移已建向量。
文件转写增加 [`gpt-transcribe`](https://developers.openai.com/api/docs/models/gpt-transcribe)。
whisper-1（2027-02-26 停用）与 tts-1（2027-01-06 停用）暂保留现有接口兼容；
Realtime 替代品不冒充 `/audio/speech` 模型。音频模型没有可核实的文本上下文时使用 `max_tokens=0`，
不把文件字节上限当成 token 数。

仅输入凭据时，连接验证使用厂商的 `default_probe_model`，OpenAI 为 `gpt-6-luna`；
显式选择或自定义模型时验证所选模型，不按名称排序覆盖选择。探测仍验证一个代表模型，
不代表账号可访问全部内置候选；保存实例时重新验证。失败只返回固定分类和上游 HTTP 状态，
不回显异常正文、凭据、URL 或供应商响应载荷。

## 工具调用后的历史

两种入口共用 [`ToolHistoryMixin`](./chat_model/tool_history.py)。同一轮的所有工具调用
写入一条 assistant 消息，随后按调用 ID 写入每条 tool 结果，包括失败结果。
非流式 SDK 工具调用没有 `index`，历史写入不能要求该字段存在。
同步调用保留顺序执行，异步调用保留现有并发执行和同步 session 的线程池桥接。

DeepSeek 的工具调用 assistant 消息携带 `reasoning_content`，供下一轮请求使用。
流式片段按轮累积；reasoning 与 tool_calls 同时到达也会保留。读取兼容
`reasoning_content` 与 `reasoning` 两种响应字段，空推理使用空字符串。
其他 provider 不因这一回传规则增加该历史字段，其已有推理展示继续保留。

## Agent 工具返回

Agent 的配置 `user_prompt` 是工具参数 schema 的 `default`，参数本身仍是带
`type` 和 `description` 的字符串 schema。

本地组件的 invoke 返回 `None` 时，工具 session 读取一次组件 `output()`。
若输出字典的 `content` 不是 `None` 或空字符串，使用该值；否则使用整个输出。
invoke 明确返回的空字符串、`False`、`0`、列表或字典都保持原值。
没有输出 accessor 的工具仍返回 `None`；读取输出失败交给现有工具错误处理。
callback 接收与模型相同的最终结果。

MCP 调用继续使用可信 binding 的原始工具名与既有 session、超时和授权机制，
不使用本地组件的输出回退。

相关回归：

```sh
uv run --no-sync pytest tests/unit/test_agent_tool_result_contract.py tests/unit/test_llm_tool_reasoning_history.py tests/unit/test_llm_stream_reasoning_content.py tests/unit/test_mcp_tool_binding.py -q
```

## Qwen 文本 rerank

`RerankModel["Tongyi-Qianwen"]` 由注册器扫描 [`rerank.py`](./rerank.py) 构造，
租户模型服务与模型验证入口均使用该工厂；`rerank_model/qwen_rerank.py` 的拆分副本
没有接入当前注册或生产调用链。排查时以注册类为准。

模型名以 `qwen3-rerank` 开头时，调用 `dashscope.TextReRank.call` 省略
`return_documents`；其他模型传 `False`。SDK 1.25.11 对省略参数不会生成对应请求字段。
这与[官方参数表](https://www.alibabacloud.com/help/zh/model-studio/text-rerank-api)
列出的支持范围一致：`return_documents` 支持 `gte-rerank-v2`、`qwen3-vl-rerank`，
后者的多模态接口不属于当前文本接口的验收范围。

构造器保留 `key`、`model_name`、`base_url` 与扩展关键字的兼容入口；Qwen 原生 SDK
沿用自己的端点配置。显式模型名原样传递，默认及 `None` 仍回退到 `gte-rerank`。
成功响应按结果 `index` 写回与输入等长的浮点数组，合法零分保持零；缺失、重复、越界
索引和缺失、非有限分数均报错，返回值仍为
`(scores, used_tokens)`，供 `LLMBundle.similarity` 与检索混合排序消费。

每次 SDK 请求使用 30 秒 `request_timeout`；失败响应优先读取 `text`、`message`、`code`，
没有 `text` 时仍保留供应商错误。空查询或空文档不发请求，返回等长零数组与零 token。
受控回归覆盖注册类、真实 SDK 参数构造及响应转换、乱序/零分映射、token 返回和错误传播，
没有证明真实云端或当前默认模型可用性。

```sh
uv run --no-sync pytest tests/unit/test_qwen_rerank_contract.py -q
```

## Bedrock 图像描述

`CvModel["Bedrock"]` 注册 [`cv.py`](./cv.py) 的 `BedrockCV`，供租户模型服务、
`LLMBundle.describe_with_prompt` 和文档解析调用。通过 LiteLLM 的 `bedrock/` 模型前缀
传递图像消息，返回描述和实际 token 用量；空描述或供应商失败报错。

凭据沿用添加模型接口保存的 JSON 对象及 `is_tools` 信封。`access_key_secret` 使用
`bedrock_ak` / `bedrock_sk`；`iam_role` 用 `aws_role_arn` 调用 STS；Web 的 `assume_role`
选项使用 AWS 默认凭据链。`bedrock_region` 缺省或为空时为 `us-east-1`。
STS 凭据按调用获取并关闭客户端，避免持有过期 session token。

## HTTP 超时与重排错误

活动模型模块中的直接 HTTP 调用均有有界超时：embedding / rerank 通常 30 秒，
视觉与音频 60 秒；智谱 embedding 保留原有 120 秒。TTS 流结束、报错或消费者关闭
生成器后关闭响应。超时仍由既有异常或显式 `**ERROR**` 合同向调用方报告。

HTTP 重排先检查状态码，再严格校验与输入一一对应的索引及有限数值分数；供应商错误
和不完整结果不作为零分成功。Cohere 在 SDK 转换前校验原始响应，防止布尔值或数字字符串
被 SDK 自动转换成合法索引、分数。HuggingFace 支持带/不带协议、路径前缀和完整 `/rerank`
端点，8 条一批并按各批局部索引还原输入顺序；后批失败立即终止。
Jina 子类使用各自配置的端点；302.AI 实现同样的重排返回合同。

```sh
uv run --no-sync pytest tests/unit/test_bedrock_cv.py tests/unit/test_llm_http_timeouts.py tests/unit/test_rerank_robustness.py -q
make integration TESTS=tests/integration/test_llm_tool_capability.py::test_saved_bedrock_and_huggingface_models_use_registered_consumers
```

## 会话语音缓存

Canvas 和 dialog 的 TTS 入口清理文本后共用 Redis 音频缓存。键对租户、已保存的模型、
供应商、端点、凭据及声音配置和文本分别取哈希，不包含明文凭据或正文；用量、状态和时间戳
不参与身份。命中直接复用完整音频，不再次调用供应商或增加用量。并发未命中可各自合成。

`RAGFLOW_TTS_CACHE_TTL_SECONDS` 默认七天；非正值禁用读写，无效整数回退默认值。
Redis 失败按原路径合成，损坏缓存、空音频、非法音频块及中途失败不会写缓存。
独立 `/chat/audio/speech` 路由仍直接执行其语音合同。真实 Redis 与 SQL 计量读回见
`tests/integration/test_tts_cache.py`；受控供应商结果不代表真实模型音质或供应商可用性。

## 模型发现

`POST /api/v1/providers/{provider_name}/models/discovery` 接受 body 中的
`api_key`、`base_url`、`region`，query 只接受 `page` / `page_size`。
响应使用现有分页信封，返回模型名称、能力、上下文、工具/思考 features、展示名与
static/remote 来源。结果是候选，没有已注册模型的 numeric ID 或启用状态；响应不缓存。

Ollama 使用 `/api/tags` 和 `/api/show`，最多发现 100 个模型，最多并发 4 个请求，
单响应最多 1 MiB、请求超时 10 秒、整体超时 30 秒。凭据仅发送至 literal loopback IP；
远程地址可无凭据发现。禁止用户信息、query、重定向和环境代理携带凭据。
任何远程失败、坏响应或超出限制都明确失败，不以空列表或旧静态目录掩盖。

Fish Audio 固定官方 HTTPS origin，以 `self=true` 读取当前 workspace 自有声音并完成
最多 100 项的分页；稳定 `_id` 用于调用，title 只用于展示。原始 token 和明确的结构化
token 字段均可用；已配置的声音引用与所选 ID 冲突时拒绝。其他 origin 不接收 Fish 凭据。
MinerU 的生产协议为 `/file_parse`，发现接口只提供 OCR 声明候选，后续仍验证真实 PDF 解析。
未实现的 VolcEngine metadata driver 不注册；普通静态目录仍可用。

选择多个远程模型时，使用 `ConnectionCandidate.custom_models`（最多 100 项），
将 `model_ids: []` 明确设置为空静态选择。展示元数据不作为注册字段提交。
保留现有单项 `custom_model` 合同，两个自定义选择字段不能同时使用。
connection 对代表模型只读验证，instances 创建时服务端重新验证并在一个事务内保存
实例及全部模型。验证或任一模型持久化失败时整笔回滚，成功读回后才获得 numeric ID。
