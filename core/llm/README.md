# 聊天模型与工具历史

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
