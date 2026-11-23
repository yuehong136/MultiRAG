# Memory 修订与来源撤回

Memory 可显式接管已有派生条目，保存修订历史，并通过操作记录发布新正文。
原 RAW、消息类型和消息存储 schema 保持原义；修订、来源状态与操作记录保存在独立 SQL 表中。
权限继续使用服务器认证的 owner/team 规则，请求不能指定 tenant、actor、模型或已验证状态。

## 启用前确认

先读取 `GET /api/v1/memories/{memory_id}/capabilities`。
`revisions.supported` 表示当前后端具有严格读回、完整有界计量和受管筛选能力；当前已验证 Milvus 路径。
缺少这些能力的存储拒绝接管，不先创建受管状态再尝试降级写入。

接管是显式操作，GET 不会接管或迁移旧数据。**首次接管会启用整个 Memory 的受管写入约束：**

- 具有 `writes.supported=true` 的后端通过同一协调入口处理普通追加、自动提取与 FIFO；启用前仍须停止并排空旧版本进程。
- 已有未接管条目仍按原语义读取，不因接管一条而批量隐藏或重写。
- 单条状态和遗忘通过协调入口执行；临时停用可以重新启用，永久来源撤回不允许重新启用。
- 具有 `deletions.supported=true` 的后端先关闭写入、等待实际 IO 排空，再清理索引与 SQL；删除中立即停供。

这些边界分别由 `activation_scope=memory_bank`、`ordinary_writes_after_activation`、
`coordinated_state_changes` 和 `managed_delete_supported` 声明；调用者须检查实际值。
本阶段不提供自动退出受管模式或全库批量接管。

## 普通写入与自动提取

```http
POST /api/v1/memories/{memory_id}/messages
Content-Type: application/json

{"operation_id":"append-conversation-001","user_input":"项目使用端口 8080","agent_response":"已记录","agent_id":"assistant","session_id":"session-001"}
```

服务器保存原始输入与回答组成的 RAW。Web 会话中的 `user_id` 由认证用户确定；SDK token 可提供外部主体，
但它不授予租户或 Memory 访问权。新命令拒绝 tenant、actor、模型和验证状态等额外字段。
旧 `POST /api/v1/messages` 保留默认信封，可提供 `operation_id` 获取可恢复的逐库操作结果。

未受管和受管 Memory 均登记 writer fence、admission 与 publication outbox；普通写入不会自动接管旧条目。
模型输出通过校验后，先保存批准内容、模型绑定和固定消息 ID，再写入 inactive 投影并读回。
SQL 发布与物理启用均确认后，读取策略才允许供给。恢复已批准结果不会重新提取或分配新 ID。

RAW 配置只保存原始记录，不创建提取任务。配置了派生记忆类型时，RAW 的批准事务登记固定 Task 和子操作；
Redis 只传任务身份，不重复保存对话正文。相同 Task 的重投复用批准结果，不能凭队列中伪造的 actor 获得权限。
自动提取保留真实 RAW 时间和来源；提取结果不会自行变成用户已核验的事实。

追加结果提供 `raw_source_id`、`message_ids`、`extraction_task_id`、`queue_state` 与 `evicted_sources`。
整体写入成功须同时满足 `status=ready`、`index_sync_status=ready`，且 `queue_state` 为 `not_required` 或 `submitted`。
新命令的 `pending` / `unknown` 返回 HTTP 202，保留原操作 ID 对账；旧批量接口仍使用原信封和非零业务码。
`submitted` 仅说明任务入队，提取是否完成要读取
`extraction_task_id` 对应的操作。任务成功前不会把未发布的派生正文交给检索。

FIFO 按 RAW 及其相关派生记录形成固定清理计划，使用稳定子操作，并在新的容量决定前核对实际占用。
已有来源撤回、迟到写入和恢复任务都受来源状态与 generation 检查；不会为了腾出容量只删一个派生片段、遗留来源闭包。
物理库存达到 10,000 行时明确拒绝继续，不能用不完整扫描宣布容量足够。
若旧来源已经淘汰而后续新写入失败，`evicted_sources` 仍保留清理结果；这不是能回滚全部副作用的跨库事务。

## 接管与读取

```http
POST /api/v1/memories/{memory_id}/entries
Content-Type: application/json

{"operation_id":"adopt-001","legacy_message_id":"910"}
```

只接管具有有效真实 RAW 祖先的 semantic、episodic、procedural 条目。
接管时保存当下可见的正文和模型绑定，建立 baseline revision 1；这不是恢复旧记录创建以来的历史。
RAW 本文不在此入口修改，不能伪造 RAW 祖先。

| 读取 | 用途 |
| --- | --- |
| `GET /memories/{memory_id}/messages/{message_id}/entry` | 根据旧消息或历史投影查当前 entry；未接管返回 `data:null` |
| `GET /memories/{memory_id}/entries/{entry_id}` | 当前版本、供给状态及来源 |
| `GET /memories/{memory_id}/entries/{entry_id}/revisions?limit=20` | 版本倒序历史；通过 `next_cursor` 续取，单页最多 100 条 |
| `GET /memories/{memory_id}/operations/{operation_id}` | 读取操作结果，不执行模型、发布或恢复 |

上述路径均在 `/api/v1` 下。新资源中的物理消息／来源 ID 使用十进制字符串，避免 JavaScript
超过安全整数范围时丢失精度。接管输入支持规范十进制字符串或严格正整数；版本号使用严格正整数。
旧消息接口的数字 ID 合同保留，调用者不能把已舍入的数字当成真实 ID。

## 预览与修订

```http
POST /api/v1/memories/{memory_id}/entries/{entry_id}/revisions
Content-Type: application/json

{
  "operation_id":"correct-port-001",
  "expected_revision":1,
  "patch":{"old_text":"port 80","new_text":"port 8080"},
  "reason":"explicit user correction",
  "dry_run":true
}
```

`content` 整条替换与 `patch` 二选一。patch 只替换唯一匹配的字面片段；零次或多次匹配均冲突，
不隐式规范化 Unicode、不让模型猜目标。最终正文必须非空，最多 65,535 UTF-8 字节及 8,192 个估算 token；
reason 最多 1,024 字符。

预览返回最终正文、diff、内容哈希、来源检查和 token 估算，不写 SQL、不分配消息 ID、不调用 embedding。
预览不锁住未来状态。正式提交使用同一内容和 `expected_revision`，将 `dry_run` 改为 false。

正式修订重新 embedding，冻结服务器解析的模型身份、配置指纹与向量维度；来源、权限或生效模型改变时拒绝发布。
模型和存储调用在 SQL 事务外进行。新物理投影先以 inactive 写入，读回校验后再切换 SQL current 和同步启停。
旧 current 在发布决定之前继续供给；发布后的索引同步窗口可能暂时少结果，不供给旧版本冒充新版本。

人工修订标记 `origin=manual`、`assessment.kind=user_asserted`、`verification_status=unverified`，
保存 actor 和 operation。保留 RAW 引用只说明上下文关系，不证明人工新正文受到原文支持。
客户端及模型不能通过该入口直接写入 verified。
对当前版本的人工核验结论通过独立的[核验记录](memory-verification.md)保存；
正文或必要来源变化后旧核验不再适用于当前版本，原修订的 `assessment` 保持不变。

## 操作状态、幂等与恢复

命令使用 `operation_id`。同一范围、同一操作 ID 和同一请求返回原结果；相同 ID 携带不同请求或 actor 时冲突。
修改正文不需要 LLM，embedding 和索引写入仍可能失败。

操作响应包含 `operation_id`、`kind`、`status`、`entry_id`、`new_revision`、`index_sync_status`、
`retryable`、`error_code` 和可用的 `outbox_id`。只有 `status=ready` / `index_sync_status=ready`
表示对应投影同步已经确认。HTTP 202 表示已受理，不能据此显示“记忆已修改”。
HTTP 200 的操作记录仍需检查业务状态，终态失败不表示发布成功。

```http
POST /api/v1/memories/{memory_id}/operations/{operation_id}/reconciliations
```

受理后在请求内尝试推进一次；未完成的内容保留在 SQL outbox，由调用者显式对账。
GET 和页面轮询本身不会自动推进修订或清理。Dreaming 分析由独立持久 worker 执行，见[维护文档](memory-maintenance.md)。结果未知时保留原 operation 和物理 ID，先读回再决定后续动作，
不换 ID 重发、也不以异常证明“没有写入”。配置指纹不保存凭据正文。

每次外部执行都有 attempt/lease。**租约过期不代表旧 IO 已结束。**
旧 active attempt 未排空时返回 busy；`publication_needs_operator_reconciliation` 表示需要管理员核对实际 IO 与排空证据，
不能通过提交人工核验记录解除该状态。
正常取消会等待本次 IO 排空后释放 attempt；硬崩溃留下的未知 attempt 不自动放行重放。
SQL 与消息库不提供分布式原子事务，恢复能力不能解释为任意崩溃都能无人干预完成。

## 来源登记与永久撤回

`GET /memories/{memory_id}/sources/{source_id}/state` 读取来源状态。
未登记时返回 `managed:false`、`source_revision:null` 和当前正文哈希，不伪装成已持久化版本。
先用 `POST .../adoptions` 提交 `operation_id` 和 `expected_content_sha256`，显式登记 baseline 来源。

```http
POST /api/v1/memories/{memory_id}/sources/{source_id}/withdrawals
Content-Type: application/json

{"operation_id":"withdraw-001","expected_source_revision":1,"reason":"original statement retracted"}
```

撤回先提交持久来源状态，再清理相关投影。正常读取立即按来源及全部已登记依赖停供，
迟到的旧提取不能重新供给；历史和待发布正文随依赖撤回而清除，返回 `redacted:true`。
多个来源中一个必要来源撤回时，整个相关条目停供，不凭正文拆出“可能仍正确”的部分。

来源撤回是永久操作，旧 `PUT status=true` 不能恢复。临时 disable→reenable 则允许同一来源版本、哈希和身份的
已发布内容恢复；旧 pending 操作仍因 generation 变化而失效，不能穿过停用／启用的并发边界。

`stop_supply`、`projection_cleanup_status` 和 `physical_cleanup_complete` 分别表达供给、投影清理和全部副本清除。
当前清理覆盖已登记修订及有界可观察的来源相关记录，`untraceable_copies:true` 时不宣称全部物理副本消失。
接管前未受控的在途写入、备份及调用者已经取得的正文不属于可追溯收回保证。

## 兼容和错误

普通检索、recent、query context、prompt 和按 ID 供给统一排除 pending、历史、非 current 及撤回依赖。
受管查询在候选截断前应用资格筛选，返回前复核；期间发生变化时返回冲突。
受管稳定分页和物理计量最多完整扫描 10,000 行，达到上限明确拒绝，不把截断扫描当成完整结果。
管理界面可以查看临时停用的当前记录，永久撤回正文仍不可读取。

新资源使用 `retcode/retmsg/data`：404 资源不可见；409 版本、来源、容量或在途状态冲突；
422 请求／后端能力不满足；503 SQL 提交结果未知，携带原公共 operation ID；500 其他内部错误。
旧状态／遗忘接口保留响应信封；受管分支通过来源与版本协议执行，未完成时返回原操作 ID。
严格后端中，未受管的旧派生条目使用独立 writer 操作，不隐式启用受管模式；目标 producer 未发布完成时拒绝修改。
未受管 RAW 的状态或遗忘先要求显式登记来源，返回 `source_adoption_required` 时，可在来源面板登记或调用
`.../sources/{source_id}/adoptions`，再重试原动作；这样来源闭包及在途提取才能使用同一套 generation 约束。
配置更新会为仅有模型名称的旧记录回填当前 owner 解析出的模型 ID；相同模型的绑定回填不更换已有向量，
实际 embedding 模型或记忆类型变化仍受非空库限制。客户端传入的外租户模型 ID 不会成为绑定依据。

SQL 旁表通过正式 Alembic 迁移创建，部署前完成备份并核对当前 migration head。
空库引导、存量升级与不兼容旁表拒绝分别验证，不能只 stamp 版本。
相关读取预算和来源摘录见 [Memory 查询](memory-query.md)；候选整理与审核发布见 [Dreaming 维护](memory-maintenance.md)。
按请求生成的小份背景另见[只读启动上下文](memory-startup.md)；Wiki 和摘要压缩不包含在这些能力中。

## 整库删除与排空

```http
POST /api/v1/memories/{memory_id}/deletions
Content-Type: application/json

{"operation_id":"delete-memory-001"}
```

严格后端支持受管和未受管库。请求先重新鉴权并锁住 Memory，关闭 writer fence、取消排队写入并停止正常读取。
实际 active/unknown attempt 不会因租约超时被当成已经结束。清理确认后删除原 Memory SQL 行，仅保留必要的
租户、权限、操作与审计信息；删除后仍可通过原操作 ID 读取或对账，team 权限继续依据当前成员关系核验。

操作分别报告 `lifecycle`、`writer_drain`、`blocked_by` 和 `needs_operator_reconciliation`。
只有 `lifecycle=deleted` 且 `tracked_cleanup_confirmed=true` 表示本协议追踪的清理完成。
`physical_cleanup_complete=false`、`untraceable_copies=true` 不承诺备份、客户端副本和未受控历史副本消失。
Web 删除对话框在等待排空或清理中保持未完成状态，并允许对同一操作重试；独立操作页可在刷新或关闭页面后，
通过服务端待办及 URL 中的原操作 ID 重新读取。发现和读取不自动执行对账，清理完成仍以原回执为准。

升级部署时，先备份、应用 Alembic 迁移，再停止旧 API/worker，确认旧远程调用结束后统一切换。
不能混用绕过 admission 的旧进程与新进程。历史 Task 即使 `progress=1` 也不构成旧 IO 排空证据，删除时会作为
未追踪任务阻断。管理员可通过 [memory_writer_admin.py](../../scripts/memory_writer_admin.py) 先 `audit`，
独立确认进程及远程请求均已结束后，再以精确 generation、任务集合哈希或 attempt/token 进行 `attest-legacy`
或 `attest-attempt`。命令须通过 [登记入口](../development.md#事前登记验证命令) 执行，提供 evidence reference
及显式 `--confirm-all-selected-io-settled`；不提供普通用户 HTTP 放行开关。

管理员声明只是有审计的人工证据，不是程序证明 IO 已结束。新任务、generation 或 attempt 改变时旧声明失效。
对实际 attempt 的声明仅解除选定的在途阻断，保留原发布状态；随后仍须使用原操作对账，不能据声明直接显示成功。
