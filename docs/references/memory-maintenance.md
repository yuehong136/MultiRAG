# Dreaming 候选整理与审核发布

Dreaming 将显式选择的来源和当前记忆整理成候选，供用户查看差异、批准或拒绝。
相关旧记忆可先通过[有界目标推荐](memory-targets.md)审查并明确选入，推荐不自动改写当前记忆。
候选保存在独立 SQL 表中，不参与普通消息检索；批准后仍须显式发布，才能成为当前记忆。
人工批准只表示允许采用该候选，`assessment` 仍是 `hypothesis` / `unverified`，不代表事实已验证。
需要记录对已发布版本的人工判断时，使用独立的[人工核验与争议待办](memory-verification.md)；
核验不代替候选批准或显式发布，也不改变原 `assessment`。

本功能面向 Python 后端及配套 Web，不依赖上层应用。它不提供 Wiki、上下文压缩、定时夜间运行、
消费者历史抓取或任意工具执行；模型只有受限的候选输出，没有修改记忆、执行代码或联网的工具。

需要处理较多来源时，可使用[增量选源与分批整理](memory-incremental.md)：按授权范围显式预览、创建和提交有界计划，
由来源版本覆盖账本避免重复分析。子任务仍使用本页的候选、审核和发布协议；不会自动定时运行或发布。

## 开始前

先读取 `GET /api/v1/memories/{memory_id}/capabilities`，检查 `maintenance.supported`。
当前严格存储路径要求 active 的受管 Memory，以及每条选中 RAW 已登记且 active 的来源版本。
未登记返回 `source_adoption_required`；Web 可显式登记选中来源后再创建任务，登记会启用整个 Memory 的受管约束。
详情见[来源登记与修订](memory-revisions.md)。GET 和模型调用不会静默接管来源。

一个任务只属于一个 Memory 和租户。必须显式提供 1–32 个 RAW 来源 ID，可选 0–32 个逻辑 entry ID；
空范围不会扩展为全库。选择旧 entry 时，必须同时选入其全部必要来源，且当前版本必须已经就绪。
同一候选可以关联跨会话来源，但 user/agent 身份必须相同，不把不同用户的内容归入同一条记忆。

所有物理消息和来源 ID 在新接口中使用规范十进制字符串，保留 int64 精度；逻辑 entry/job/candidate ID 使用服务器分配的 ID。
tenant、actor、模型行、可信状态不能由请求或模型覆盖，权限沿用当前 owner/team 规则，每次动作重新核验。

## 使用步骤

以下路径都在 `/api/v1/memories/{memory_id}` 下：

| 操作 | 路径与输入 | 含义 |
| --- | --- | --- |
| 创建任务 | `POST /maintenance-jobs` | 保存授权输入快照，不调用模型 |
| 查看任务 | `GET /maintenance-jobs` 或 `GET /maintenance-jobs/{job_id}` | 只读状态、来源版本和覆盖量 |
| 提交分析 | `POST /maintenance-jobs/{job_id}/runs`，`{operation_id}` | 持久提交后台分析，受理请求不执行模型 |
| 查看候选 | `GET /maintenance-jobs/{job_id}/candidates` | 查看最终正文、原文、精确 patch、diff、理由和来源 |
| 审核候选 | `POST /maintenance-jobs/{job_id}/candidates/{candidate_id}/reviews` | 提交操作 ID、`expected_revision` 和 `approve` / `reject` |
| 发布批准项 | `POST /maintenance-jobs/{job_id}/publications` | 提交操作 ID及选中的 candidate ID/最新审核版本，最多 16 项 |
| 取消整理 | `POST /maintenance-jobs/{job_id}/cancellations`，`{operation_id}` | 使分析与未发布候选失效，不撤销已发布记忆 |
| 查看与对账发布 | 既有 `GET /operations/{operation_id}`、`POST /operations/{operation_id}/reconciliations` | 复用原批次操作及固定消息 ID |
| 发现待办 | `GET /operations?view=pending`，或 `view=all` | 找回分析、发布、追加与删除等原操作，支持游标分页 |

创建请求示例：

```json
{
  "operation_id": "dream-project-decisions",
  "source_ids": ["900", "901"],
  "entry_ids": [],
  "policy": "decisions",
  "input_budget_tokens": 16384,
  "output_budget_tokens": 4096
}
```

策略为 `general`、`preferences`、`decisions`、`procedures`；它们影响分析方向，不创建新的记忆类型。
输入预算为 1,024–32,768，输出预算为 256–8,192，均是 token 估算预算，不是费用承诺。
输入过大时整条延期，保留 `coverage=deferred` 及原因；不会静默截断正文后声称已经分析。
元数据或完整目标正文自身超预算时明确拒绝。没有任何来源能进入预算时也拒绝创建。

任务提供 selected/included/analyzed/deferred 覆盖量，只有实际送入并成功完成分析的输入才计入 analyzed。
单个手动任务不维护全局完成水位。后续新增来源、新来源版本或新范围需要新的任务；
跨任务的版本覆盖由[增量整理账本](memory-incremental.md)提供，不根据上次时间猜测全量已经处理。
RAW 作为 `legacy_combined` / `role_unknown` 使用，不解析正文中的 `User Input:` 等字样来重建可信角色，
也不把模型或历史 Agent 的自述当成实际工具验证结果。

### 候选选择与修订

分析器读取完整的已纳入来源正文；当前没有 entry 时也可以提出 `add`。
选择方向包括带适用条件的长期偏好、同一 user/agent 在同项目不同会话中的互补决定，以及有依据的操作经验。
明确的后续决定应替代旧的待定或冲突状态；尚未解决的条件仍需保留。
候选正文与理由应归因于来源，不根据 RAW 中的角色字样推断说话者，也不增加未经支持的保证或未来承诺。
一次性指令、攻击引文和缺少具体内容的猜测应排除，不能为了说明拒绝原因又把它们存成记忆。

修订优先使用唯一原文片段的最小 patch，保留同条记忆的其他事实；已有条目应修订，避免另添重复记录。
`patch.source_input_ids` 必须列出目标 entry 的全部 `source_ids` 对应的输入 ID，以及本次纠正来源，
即使某个旧事实已被替代也要保留证据链。缺少必要来源时宿主拒绝候选，不由模型自行撤销旧来源。

以上是模型的选择指引，不是正确性保证。宿主继续校验输出结构、主体和证据依赖，并将结果标记为未验证假设；
审核者仍须检查遗漏、过度归纳、条件和时序。真实模型对照与适用限制见 [Memory 基线评估](memory-evaluation.md)。

## 持久后台执行与刷新恢复

`runs` 将分析操作、writer admission、索引 outbox 和 execution 在接受事务中登记，返回 HTTP 202。
已完成的同操作重放可以返回 200。请求断开或页面关闭不会删除 SQL 中的任务；独立 worker 领取显式提交的 execution，
继续复用原分析和候选校验，不自动审核或发布。没有 worker 时任务保持 queued，受理成功不代表已经开始分析。

job 的原 `state` 与后台 `execution.state` 分开：

| execution.state | 含义 |
| --- | --- |
| `queued` / `claimed` | 等待执行，或已领取但尚未进入分析阶段 |
| `running` | 本次分析已启动，可能仍有实际模型 IO |
| `succeeded` | 候选保存与后台执行结算均已确认，不表示候选已发布 |
| `failed` | 本次失败；允许的重试必须显式使用原操作，并重新核验来源、权限及次数上限 |
| `canceled` | 已取消并结算；已发布记忆仍按原发布协议保留 |
| `needs_reconciliation` | 状态或实际 IO 未能确认，需要恢复或由管理员独立核对实际 IO |

execution 只返回状态、尝试次数、时间和安全错误码，不返回 worker/claim/writer token。
metadata 领取超时只允许重新领取尚未进入真实工作的派发；running 的实际 token 不能靠心跳或租约过期清除。
终态记账结果未知时 worker 停止，后续 worker 按原记录恢复；job 已 ready、execution 尚未结算时仍会出现在待办中，
恢复不会重新生成已经保存的候选。每个 execution 最多启动三次分析，显式重试不能绕过上限；模型 SDK 内部重试次数另计。

启动独立 worker 使用 `python -m memory.maintenance.worker`；`--once` 对账既有执行元数据并执行至多一个排队任务，
`--poll-interval`、`--claim-timeout-seconds`、`--heartbeat-seconds` 控制轮询和派发元数据的监测。
停止信号要求排空当前 owned work。该 worker 不扫描来源、不维护全局水位，也不提供定时整理。

操作待办 `view=pending` 包含尚未结算、仍需通知提取任务或索引对账的操作；`view=all` 可找回终态句柄。
分页按创建时间和操作 ID 继续，游标绑定认证用户、Memory、筛选和页大小，每次重新检查当前 owner/team 权限。
列出公开操作句柄和安全 metadata，不列 Dreaming 内部发布子操作。库删除后沿最小审计权限仅提供原删除操作，不依赖 Memory 配置仍存在。

`can_reconcile` 表达当前用户是否可显式对账；它不是授权凭证，POST 仍检查当前权限和原操作 actor。
团队成员能阅读的操作不一定能代替原 actor 执行。`writer_busy=true` 或需要管理员核对实际 IO 时，不提供直接重放入口。
RAW 已存但 `queue_state` 尚未 submitted/not_required，或实际 writer 未结算时，不能仅凭 ready 状态显示整体完成。

Web 通过服务端待办与 URL 中的 job/operation ID 恢复选择；独立操作页支持原库删除后的审计读取。
刷新和轮询只执行 GET，不自动调用模型、对账或发布。候选审核、失败重试和发布继续由用户显式发起。

## 候选与审核

允许的动作只有新增 `add`、精确片段修改 `patch`、无需变化 `noop`。
patch 必须在选定 entry 的完整原文中唯一匹配，由宿主组成最终正文，保留同条未修改的其他事实；
零匹配、多匹配、越界来源、伪造验证状态、额外字段或超预算输出会拒绝整份结果，不保存半份候选。
每个正文最多 65,535 UTF-8 字节、8,192 个估算 token，每次分析最多 32 个提议。
发布 patch 保留目标原有记忆类型、身份、来源锚点和事件时间，包括有效、失效与遗忘时间；新增项使用选定 RAW 的锚点元数据。

审核使用 candidate revision 的 CAS；批准或拒绝会递增版本。发布必须携带批准后的新版本，
不能用审核前版本发布，不能发布 pending/rejected/noop 项。审核不会自行执行 embedding 或切换当前记忆。
来源失效或目标版本变化时重新核验失败，不能在发布时搜索相似文本并改动另一个条目。

任务失败不推进覆盖量、不覆盖旧记忆。有效回包的模型/配置指纹、已知用量与失败记录保存在有限审计中，
无有效回包时标记 `usage_unavailable`；`provider_billing_verified=false`，估算值不能当作实际账单或提取质量评价。
模型输出、原始来源和候选理由都是数据，不能改变身份、权限、工具或审核规则。

## 发布、恢复与取消

同一操作 ID 和同一请求复用原结果；不同请求或不同 actor 冲突。发布使用已经保存并批准的最终正文，
不再次让模型生成内容。先保存稳定消息 ID、向量及整个计划，再暂存并逐项严格读回 inactive 投影。
全部准备完成后，在同一 SQL 事务中校验选定来源、目标和候选版本并切换所有 current；任一前置条件失配都不能只发布其余部分。
每项再通过独立索引同步回执启用新投影、停用旧投影。SQL 与远程消息存储不是分布式原子事务。

只有批次 `status=ready` 且 `index_sync_status=ready` 才表示整个批次已确认。
HTTP 202、已受理、SQL 已发布或单项已同步，都不能显示为整批完成。结果未知时先读取原操作，
再显式对账；不换 ID 重发。子项操作不能绕过父批次单独触发通用发布。

SQL 决定之前来源失效会中止整批；SQL 决定之后来源撤回只清理依赖该来源的条目，
无关且已确认的条目可以继续供给。父批次以 `status=stale`、`index_sync_status=unknown` 等状态及错误码报告部分失效，
不把部分完成冒充整批成功。只要仍有 pending/unknown/sync_pending 子项，`retryable=true`；须继续使用原父操作对账，不能仅凭 stale 判定无需恢复。
临时停用后，已发布内容只有来源仍为同版本、哈希和身份且重新 active 时才可能恢复；永久撤回不恢复。

分析、发布与清理期间的慢模型或存储 IO 持有实际 writer attempt。创建任务的来源快照为只读 IO，保存前重新核验来源与关闭状态。
租约超时不代表旧 IO 已结束，取消也必须等待本次 owned work 结算。
硬崩溃留下的未知 attempt 需按[写入排空工具](memory-revisions.md#整库删除与排空)独立核验，不能通过重试抢占。
取消整理不等于已受理发布操作的所有物理暂存已经清理：未 SQL 发布的批次仍须用原发布操作对账，
确认暂存清理并释放容量预留；清理未知时继续保留原 ID 和预留。已经 SQL 发布的记忆不因取消整理而倒退。

来源永久撤回、entry 遗忘、整库删除会在同一 SQL 决定中清理相关输入快照、目标正文副本、候选正文、patch、diff、理由和待发布载荷，
并阻断迟到模型结果。整库删除仍等待实际 writer 排空。Web 确认撤回后也会清候选缓存并挡住旧请求回填。
备份、调用者已取得的内容和不可追踪副本不在物理清除认证范围内。

## 部署边界

部署前备份并应用正式 Alembic 迁移，统一切换已排空旧进程的 API 和维护 worker，并单独运行上述 worker 入口。
API 不在请求内启动后台线程或创建脱离资源所有权的进程。后台任务由持久 SQL 记录和受管理的 worker 执行，页面不承担调度职责。
本地受控模型和真实 scratch 存储测试验证的是权限、状态、事务与恢复协议；真实模型质量、供应商费用和生产部署需要另外验收。
小规模合成效果与成本基线的范围见 [Memory 基线评估](memory-evaluation.md)。

## 维护接点

候选流程集中在 `memory/maintenance/`，使用独立的 job/input/candidate SQL 表和迁移，
继续复用现有模型适配器、消息存储、来源版本、writer admission 与索引 outbox，不新增第三方记忆引擎依赖。
后续跟进上游时，除独立模块外，重点回归以下已有接点：

| 接点 | 本功能依赖的行为 |
| --- | --- |
| 来源停用、撤回与 entry 遗忘 | 同一 SQL 决定使候选失效，排空实际 writer 并清理相关投影 |
| 整库删除 | 关闭分析与发布，清理候选快照，复用现有排空和删除协议 |
| 普通读取 gate | consolidated 版本须同时具有就绪的子项 outbox 和父 writer admission |
| 人工修订 | 按每条 source ref 的真实 scope 校验跨会话来源，旧 ref 沿用目标 scope |
| 操作查询与对账、capabilities | 分发到原 Dreaming 操作，并向 Web 提供明确的支持范围和恢复状态 |

这些接点包含自有发布与失效语义，仍需随上游相关改动验证，不能仅凭独立目录认定不会产生合并冲突。
