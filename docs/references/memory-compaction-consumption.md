# 显式消费压缩产物

现有[上下文压缩](memory-compaction.md)生成独立、可审查的产物。本页的调用示例只读取已经生成的产物，
构造一份带完整预算的模型请求；不会生成第二份摘要、替换聊天历史、核验事实或发布长期记忆。
旧 RAW 默认和长期记忆人工审核流程保持原样。

## 从 Web 取得引用

在产物详情检查摘要、完整后缀及来源后，选择“复制消费引用”。复制前重新读取原操作并检查当前有效性，
只导出以下四个字段，不复制正文、摘要或凭据：

```json
{
  "memory_id": "<Memory ID>",
  "operation_id": "<原生成操作 ID>",
  "artifact_id": "<独立产物 ID>",
  "input_manifest_sha256": "<64 位来源清单指纹>"
}
```

保存为本地 `reference.json`。引用只是定位及绑定条件，不授予 Memory 权限，也不表示人工事实核验或外部上下文已应用。
复制时的检查不能替代下次消费时的重新读取。切换账号／操作、取消或来源变化使旧结果失效时，不导出旧引用。

## 先预览，再显式发送

从仓库根目录执行示例模块 `examples.python.memory_compaction_consumer`。
通过环境提供 `MULTIRAG_API_BASE`（包含 `/api/v1`）与 `MULTIRAG_API_TOKEN`；凭据不要写进引用或命令参数。
API 使用当前身份授权，不接受引用中的 ID 或调用范围作为额外权限。

```bash
uv run --no-sync python -m examples.python.memory_compaction_consumer reference.json \
  --user-id '<来源主体>' --agent-id '<来源 Agent>' \
  --question '这些记录中的约束是什么？' \
  --context-window-tokens 16384
```

默认仅预览：每次 GET 原 `context-compaction-operations/{operation_id}`，检查四字段绑定、当前状态和全部根来源，
验证调用者明确提供的主体／Agent／可选会话范围，再计量完整请求。只输出引用和预算，不创建模型客户端或打印正文。
`--session-id` 省略时不限制会话，提供空字符串时精确匹配空会话；主体和 Agent 的空字符串也不是通配符。

需要调用模型时，额外设置 `OPENAI_API_KEY` 并显式添加 `--send`：

```bash
uv run --no-sync python -m examples.python.memory_compaction_consumer reference.json \
  --user-id '<来源主体>' --agent-id '<来源 Agent>' \
  --question '这些记录中的约束是什么？' --send
```

示例使用官方 OpenAI 端点，默认模型 `gpt-6.1-terra`，调用一次且关闭 SDK 重试；可产生费用。
它是外部调用者示例，不替换服务端 `LLMBundle`，也不构建新的供应商适配层。
当前请求格式面向支持 `max_completion_tokens` 的现代 OpenAI 模型。
示例的标准输出包含实际回答；成功时附供应商用量（有回执才返回）。失败时退出并只打印异常类别，
不输出可能含凭据或正文的异常内容，也不把未知费用解释成零。再次执行 `--send` 是用户发起的新回答调用，
不是对未知供应商结果的安全对账；程序本身不会自动重试。

## 每次重新复核和完整预算

`memory.compaction.consumption.prepare_compaction_request` 接收原引用和读取回调，每次调用重新读取一次原操作。
纯构造器 `build_compaction_request` 供已有调用器复用；其调用者必须保证传入的响应刚刚读取，不能用列表或旧缓存代替。
构造器检查生成状态、排空状态、来源清单指纹、完整后缀哈希、顺序和精确范围，拒绝错误身份、失效或不完整产物。

请求固定包含模型名、三条消息和 `max_completion_tokens`：固定数据边界说明、完整产物 JSON、调用者问题。
不截断摘要或后缀，不从外部追加未登记事实；上下文中的指令和角色声明始终按数据处理。
预算计入完整请求 JSON，再加输出预留与安全余量。默认输出预留 512、安全余量 256；
返回 `token_count_is_estimate=true`、计量器名称和请求指纹，不把本地编码器当作目标模型精确 tokenizer。
调用者须提供适合目标模型的窗口；额外系统说明、已有历史、工具定义或包装都须重新计量。

来源撤回、失去权限、绑定改变、结果未知或预算不足时立即停止，不发送模型请求，不自动重新压缩或改用旧 RAW。
调用者原有上下文不会被修改。重新读取只能证明此次返回的快照，不能保证 GET 与外部模型发送之间的跨系统原子性，
也不能收回已经发送的内容。示例没有 `applied` 状态或会话头；实际持有会话的消费者仍须核对自己的上下文前置条件。

## 验证与成本

确定性回归覆盖引用导出、逐次重新读取、完整 SDK 请求与预算、范围不符、来源撤回、权限拒绝、未知结果及无重试。
固定 Web 提交的真实后端消费者与实际组件的受控 HTTP 浏览器验收分别取证；
官方 SDK 的受控故障测试不算真实供应商调用。重复复用的真实模型方法和结果见[记忆评估](memory-evaluation.md)。
摘要只生成一次不代表总费用更低；应同时记录摘要生成、各次回答、共享来源准备、供应商缓存回执及未知金额。
定时触发后置，Wiki 等主线，不涉及 AgentHub。

本组于 2026-11-17 继续直接核对源码与测试定义，借鉴范围限定如下；没有运行上游测试套件：

- DeepSeek Harness 的[来源稳定检查](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/compaction/compaction-basic/src/region.ts#L444)及[取消后不提交测试](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/compaction/compaction-basic/tests/manual-compaction.spec.ts#L822)：消费前复核，生成与实际采用分开，不引入其会话运行时。
- lossless-claw 的[已有产物处理](https://github.com/Martian-Engineering/lossless-claw/blob/15548bfcdd76dbf05365f3c82637fc81b8cf5f67/src/pending-summary-publisher.ts#L108)及[发布保护测试](https://github.com/Martian-Engineering/lossless-claw/blob/15548bfcdd76dbf05365f3c82637fc81b8cf5f67/test/engine-publication-guards.test.ts#L7)：复用产物不重复生成，费用统计不把读取当新摘要。
- OpenClaw 的[压缩实现](https://github.com/openclaw/openclaw/blob/c8d5cf52b417a99e0009cf5732d3d5502ac09132/src/agents/compaction.ts#L75)及[确定性失败测试](https://github.com/openclaw/openclaw/blob/c8d5cf52b417a99e0009cf5732d3d5502ac09132/src/agents/compaction.abort-http.test.ts#L11)：失败不盲重试，不把错误或纯推理内容当可用摘要。
- RAGFlow 的[RAW 构造](https://github.com/infiniflow/ragflow/blob/441273988d4c1126b32aef02f21e7cfeb9eeeb33/internal/service/memory_message_service.go#L330)及[兼容测试](https://github.com/infiniflow/ragflow/blob/441273988d4c1126b32aef02f21e7cfeb9eeeb33/internal/service/memory_message_service_test.go#L163)：旧 RAW 默认保持，消费能力作为独立 helper／示例，不修改各存储后端的业务语义。
