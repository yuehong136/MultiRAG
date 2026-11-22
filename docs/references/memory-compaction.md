# 显式上下文压缩

上下文压缩将调用者选定的有序 RAW 前缀提炼成独立摘要，剩余 RAW 完整保留，供人工检查和外部调用者显式使用。
它复用现有 Memory、来源登记、写入操作和排空协议；不改旧 RAW 写入默认，不创建或发布长期记忆，不自动替换会话历史。
长期记忆仍走[候选审核与发布](memory-maintenance.md)和[版本化修订](memory-revisions.md)。
显式选择产物后，可使用[消费引用与调用示例](memory-compaction-consumption.md)逐次重新复核并构造完整请求。

## 显式生成

```http
POST /api/v1/memories/{memory_id}/context-compactions
Content-Type: application/json

{
  "operation_id": "caller-stable-operation",
  "source_ids": ["9007199254741003", "9007199254740999"],
  "prefix_count": 1,
  "input_budget_tokens": 16384,
  "output_budget_tokens": 1024,
  "budget_tokens": 4096
}
```

`source_ids` 必须是 1–32 个唯一的正 int64 十进制字符串，顺序由调用者明确提供，不按 ID、时间或存储返回顺序重排。
`prefix_count` 是压缩整个前缀的来源数量，取值为 1 到来源总数；后缀逐条完整保留。
来源必须属于当前有权访问的 Memory，且是同一主体、同一 Agent 的已登记、有效根 RAW；允许跨会话。
Memory 必须已显式启用托管状态，不能通过生成命令隐式启用或接管旧来源。

旧合并文本仍标记 `role_unknown`，结构化角色及事件时间仍是调用者声明，不变成经认证的发言或执行证据。
不能将旧摘要或派生记忆当作根来源再次压缩。首版拒绝压缩包含声明 `tool` 角色的前缀，完整后缀可保留为数据；
现有来源合同没有足够的工具调用 ID 绑定，不能据角色标签推断工具调用已经配对或实际执行。

能力发现 `context_compactions` 声明支持情况和 `automatic=false`。只有显式 POST 会调用聊天模型；
该功能没有自动触发器，也不为每条消息额外生成向量。沿用现有严格存储能力检查，不新增 Milvus 专用摘要存储。

## 独立产物、来源和预算

响应中的 `operation` 与 `artifact` 区分执行状态和独立产物。`generated` 只表示生成成功，不能解释为已应用、已核验或已发布。
产物包括原始来源版本、代次、内容与出处指纹、主体／Agent／会话、模型配置指纹、覆盖范围和预算估算。
原文通过来源定位回看；摘要内容与原文均按不可信数据处理，不能赋予工具权限或修改系统规则。

`artifact.package` 是唯一待消费的数据包：

- `summary`：仅前缀的纯文本摘要。
- `covered`：每条前缀完整覆盖的 Unicode codepoint 区间，起点为 0。
- `retained`：后缀各条完整 RAW、格式和角色声明状态。
- `lineage`：全部必要根来源，保留调用者顺序和绑定。
- `omitted`：首版固定为零；预算不足直接拒绝，不静默删来源或截断后缀。

摘要输出预算为 256–8,192 个 token，模型输入与最终数据包预算各为 1,024–32,768。
生成前预检模型输入和完整后缀的最低包装；生成后再次计量完整数据包，要求严格小于相同格式的未压缩包且不超过预算。
空摘要、仅推理内容、错误文本、无缩减和超预算均不能成为可用产物。
前后对照使用同一 JSON 格式，包含重复出现的来源／角色／出处包装及完整保留正文；
管理 DTO、HTTP 信封、调用者追加的提示模板和真实模型协议开销不在该数据包预算内。
调用者必须在最终 SDK 请求处重新计量，并为已有历史及模型输出留出空间。

API 的 `usage` 明确是 tokenizer 估算，`provider_tokens=null`、`provider_billing_verified=false`。
不能用模型配置表的累计计数差冒充本次调用账单；真实供应商用量与费用由独立评估或供应商回执记录。
压缩调用关闭本层模型自动重试，并对本次持有的官方 OpenAI SDK 客户端关闭 SDK 重试；其他供应商内部行为不作等同保证。
一次失败可能已经产生供应商费用，未知结果不能据此标为零费用或自动再次调用。
每个 Memory 最多同时保留 1,000 个未清理产物。所有未清理状态均占用配额；达到上限时拒绝新建，须显式清理后才释放名额。

## 回看、取消与失效

```text
GET  /api/v1/memories/{memory_id}/context-compaction-operations/{operation_id}
GET  /api/v1/memories/{memory_id}/context-compactions/{artifact_id}
GET  /api/v1/memories/{memory_id}/context-compactions?limit=20&cursor=...
POST /api/v1/memories/{memory_id}/context-compaction-operations/{operation_id}/cancellations
```

取消请求体为 `{}`，只有操作创建者可执行。列表只给元数据，`validity_checked=false`、`valid_for_current=null`，
不能依据列表中的 `generated` 直接消费。实体回看重新检查当前权限、实际存储正文和全部版本绑定，最终复核后才返回包；
GET 不调用模型，不修复状态或写入数据库。响应禁止缓存，当前有效性只代表此次检查的快照。

来源撤回／遗忘、版本或出处改变、Memory 删除均使必要依赖不再可用，包含后缀的非锚点来源。
持久化撤回会清除摘要和保留正文副本；仅由读时发现的物理变化使本次读取停供，GET 不代替持久化失效命令。
取消或撤回发生于模型执行中时，保留活动写入凭证直到实际 IO 结束，迟到结果不能恢复数据包。
Memory 删除沿用原排空与严格清理协议；没有真实完成证据时不能把正在执行的模型操作当作已停止。

客户端必须在发出生成请求前保留原 `operation_id`。相同身份、相同顺序和参数重放只读取原操作，不再生成；
同一身份换参数返回冲突。`failed`、`stale`、`canceled` 不自动重试；未知提交或连接中断继续回看原身份。
HTTP 404／409／422 本身不能证明没有受理：旧操作参数冲突、最终权限变化也可能返回这些状态。
即使未知结果后 GET 为 404，也不能据此换 ID 再付费调用。无法证明在途工作已排空时，沿用现有运维核对协议。
通用修订 reconcile 入口拒绝压缩操作，不能把它误当作可重新执行的记忆发布。

Web 在 Memory 详情的压缩页面提供有序输入、预算、显式生成、原操作恢复、产物预览和来源回看。
URL 只保留操作身份；关闭、刷新或切换账号后不从本地持久缓存恢复摘要正文，不自动重新发出 POST。
使用摘要仍需人工核对其条件、否定、时间、标识符和待办；本接口不持久化一条“已应用”或长期记忆审核结论。

## 库存与手动清理

```text
GET  /api/v1/memories/{memory_id}/context-compactions/inventory
GET  /api/v1/memories/{memory_id}/context-compactions/{artifact_id}/retention-preview
POST /api/v1/memories/{memory_id}/context-compactions/{artifact_id}/retirements
```

库存以产物为单位，返回保留数、已清理数、可用名额、未清理产物的状态分布及未排空计数。
预览说明单个产物涉及的来源与引用、能否清理及阻止原因，不返回摘要正文，也不修改任何数据。
清理仅允许原创建者在仍有 Memory 权限时确认；产物必须已结束，并且有持久证据证明写入与发布都已排空。
状态未知或取消后模型仍在运行时，保持阻止清理。

确认请求体为 `{"expected_preview_sha256":"<最近一次预览返回的哈希>"}`。
服务端在写入锁内重新校验权限、排空证据与预览快照；快照改变返回 409，须重新预览和确认。
清理释放一个产物名额，并清掉该产物的摘要包、保留正文副本、根来源绑定及请求副本。
原 RAW、长期记忆和审核结论仍可按原规则使用；清理不会调用模型，也不代表底层备份已物理擦除。

清理后保留原生成操作身份、请求哈希和审计回执；原操作回看返回 `stale` / `compaction_retired`，不再提供数据包。
有完整排空凭证的清理回执不占写入排空清单的名额；库存仍会统计历史回执。
旧消费引用失效。相同预览哈希重放清理只回看该回执；原生成操作重放不再生成。
连接中断或提交结果未知时，先回看原操作。Web 支持库存、清理预览、手动确认与原操作刷新恢复，刷新不会提交清理。

## 验证与直接参考

固定回归覆盖顺序、完整后缀、角色／时间声明、空输出、预算、操作复用、取消、来源变化和写入排空。
真实 SQL／HTTP 与真实 Web 消费者另行验证；模型效果及总用量方法见[记忆评估](memory-evaluation.md)。
清理回归覆盖旧产物迁移、只读预览、确认重放、取消后排空及旧引用失效。用 10,001 条历史回执验证了
普通 RAW 写入和读取、新建压缩产物及 Memory 删除；有矛盾的活动写入记录仍会阻止删除。
部署需要先应用正式 Alembic 迁移。测试仅迁移隔离数据库；已有非空审计表拒绝直接 downgrade，以免丢弃恢复与来源记录。

实现时于 2026-11-17 直接核对以下源码和测试。引用固定提交便于复核，不表示运行过上游测试或完整移植其实现：

| 来源 | 采纳的机制与测试边界 |
| --- | --- |
| [RAGFlow RAW 保存实现](https://github.com/infiniflow/ragflow/blob/35e83822f56a10f3117a82f98907939fcec977bb/internal/service/memory_message_service.go)及[测试](https://github.com/infiniflow/ragflow/blob/35e83822f56a10f3117a82f98907939fcec977bb/internal/service/memory_message_service_test.go) | 保留旧 RAW 合并语义与时间边界；新功能放在独立模块，只在现有写入／删除协议接依赖失效。 |
| [OpenClaw 摘要格式测试](https://github.com/openclaw/openclaw/blob/cbf2a5b07e3f4032f18eabf434351fd7ddbabc1b/src/agents/compaction.summary-format.test.ts)及[工具细节测试](https://github.com/openclaw/openclaw/blob/cbf2a5b07e3f4032f18eabf434351fd7ddbabc1b/src/agents/compaction.tool-result-details.test.ts) | 空白／仅推理不重复请求；显式输出上限，保留不透明标识符，不能把运行时内部细节当作普通摘要来源。 |
| [DeepSeek Harness 压缩区域](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/compaction/compaction-basic/src/region.ts)、[手动压缩测试](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/compaction/compaction-basic/tests/manual-compaction.spec.ts)及[工具配对测试](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/compaction/compaction/tests/tool-pairing.spec.ts) | 保留有序区域和完整单位，预算计入真实包装；配对边界检查不等于工具执行认证，不引入其会话执行引擎。 |
| [lossless-claw 发布保护](https://github.com/Martian-Engineering/lossless-claw/blob/15548bfcdd76dbf05365f3c82637fc81b8cf5f67/src/pending-summary-publisher.ts)、[发布测试](https://github.com/Martian-Engineering/lossless-claw/blob/15548bfcdd76dbf05365f3c82637fc81b8cf5f67/test/engine-publication-guards.test.ts)及[来源测试](https://github.com/Martian-Engineering/lossless-claw/blob/15548bfcdd76dbf05365f3c82637fc81b8cf5f67/test/leaf-summary-provenance.test.ts) | 生成独立于消费，保留根来源与失效保护；不引入 SQLite DAG、递归摘要、自动替换或语义无损承诺。 |

[Anthropic 原文](https://www.anthropic.com/research/components-of-agent-memory)将 compaction 与跨会话记忆区分；
它支持这里将短期上下文压缩与长期候选审核分开的边界，不是本功能准确率或无损性的证明。

清理实现于 2026-11-22 另对照以下源码与测试：

| 来源 | 采用的边界 |
| --- | --- |
| [RAGFlow RAW 写入](https://github.com/infiniflow/ragflow/blob/91c702e86be3dcb3cabc9f6d81e485d02241bef8/internal/service/memory_message_service.go)及[测试](https://github.com/infiniflow/ragflow/blob/91c702e86be3dcb3cabc9f6d81e485d02241bef8/internal/service/memory_message_service_test.go#L77) | 继续区分明确失败与不确定结果；清理压缩产物不接管 RAW 写入。 |
| [OpenClaw 清理实现](https://github.com/openclaw/openclaw/blob/ae8155cf5f5434132b578504716a60c62c3de4e6/src/commands/sessions-cleanup.runtime.ts)及[配额回归](https://github.com/openclaw/openclaw/blob/ae8155cf5f5434132b578504716a60c62c3de4e6/src/config/sessions/cleanup-service.cap-archive.test.ts#L25) | 预览与应用使用一致的配额口径；清理后的审计记录不占产物名额。 |
| [lossless-claw 清理实现](https://github.com/Martian-Engineering/lossless-claw/blob/f68be585258cac17eef13363bf845a193d914fc0/src/prune.ts#L327)及[只读预览测试](https://github.com/Martian-Engineering/lossless-claw/blob/f68be585258cac17eef13363bf845a193d914fc0/test/prune.test.ts#L155) | 先只读预览再显式确认，确认时在事务内复核；这里限定单个产物并校验预览哈希。 |
| [DeepSeek Harness 排空测试](https://github.com/deepseek-ai/deepseek-harness/blob/d98f5d68519215da48f3002da2cb9c9d2068d81d/packages/compaction/compaction-basic/tests/manual-compaction.spec.ts#L838) | 取消后等待持久化检查点才释放执行资格。MultiRAG 读取现有写入凭证判断是否已排空。 |
| [QwenPaw 压缩钩子](https://github.com/nwahlk/QwenPaw/blob/0280fc969c10e1cfd597eac00d4932075cae2b06/src/copaw/agents/hooks/memory_compaction.py#L177) | 对照会话运行时持有历史时的自动压缩和标记行为。MultiRAG 沿用显式产物接口，清理不改原会话状态。 |

定时触发仍后置，Wiki 等主线，本功能不涉及 AgentHub。
