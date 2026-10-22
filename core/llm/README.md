# 模型入口与调用契约

正式模型注册由 [`core/llm/__init__.py`](./__init__.py) 扫描
[`chat.py`](./chat.py) 完成。DeepSeek 使用 `LiteLLMBase` 的异步入口；
[`chat_model/base.py`](./chat_model/base.py) 与 `chat_model/models/` 仍提供同步入口。
排查模型行为时先确认实际注册和调用方，不只按模型文件名定位。

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
和不完整结果不作为零分成功。HuggingFace 支持带/不带协议、路径前缀和完整 `/rerank`
端点，8 条一批并按各批局部索引还原输入顺序；后批失败立即终止。
Jina 子类使用各自配置的端点；302.AI 实现同样的重排返回合同。

```sh
uv run --no-sync pytest tests/unit/test_bedrock_cv.py tests/unit/test_llm_http_timeouts.py tests/unit/test_rerank_robustness.py -q
make integration TESTS=tests/integration/test_llm_tool_capability.py::test_saved_bedrock_and_huggingface_models_use_registered_consumers
```
