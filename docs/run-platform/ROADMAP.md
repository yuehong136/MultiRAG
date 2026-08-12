# Run Platform 路线图

> 状态：F0 已完成；RUN-F1a 进行中
> 最后更新：2026-08-13
> 说明：工作量为工程估算，不是承诺日期；实施前必须按当前代码、依赖和环境重新校准

## 1. 维护协议

每个实现任务使用 `RUN-F<n>` 标识，并在本文件记录状态、范围、证据和部署半态。状态：

- `⬜` 未开始；
- `🔵` 进行中；
- `⏸` 被明确依赖阻塞；
- `✅` 完成且有验证证据；
- `❌` 放弃，并在 [DECISIONS](DECISIONS.md) 记录替代方案。

完成定义不是“代码合并”：涉及 migration、dispatcher、worker 或客户端切换时，必须分别记录代码、
迁移、部署、灰度和现场证据。任何阶段不得修改或删除当前 v1 以换取 v2 进度。

## 2. 阶段总览

| ID | 阶段 | 状态 | 估算 | 核心交付 |
|---|---|---:|---:|---|
| RUN-F0 | 架构/契约/ADR 基线 | ✅ 完成（2026-08-13） | 后端 2–3 人日 | 本目录五份权威文档 + README 入口；两仓 F0 总计 4–6 人日 |
| RUN-F1 | 机器契约与领域骨架 | 🔵 进行中（2026-08-13） | 6–8 人日 | F1a Python schema/reducer/JSON Schema 已落；完整事件 data、TS 生成/消费与 N/N-1 fixture 待完成 |
| RUN-F2 | PostgreSQL ledger + outbox | ⬜ | 10–14 人日 | additive migration、Run/event/dedup/outbox、CAS |
| RUN-F3 | Dispatcher + Valkey Streams | ⬜ | 7–10 人日 | transactional outbox publisher、去重、backpressure、对账 |
| RUN-F4 | Worker claim 与 target adapter | ⬜ | 11–16 人日 | lease/fence、Dialog 首适配、四终态、崩溃收口 |
| RUN-F5 | v2 API、interaction、WS/SSE 与取消授权 | ⬜ | 13–22 人日 | create/get/events/interactions/cancel、多 Run WS、SSE fallback、policy |
| CLP-SC | Web/Desktop 客户端与灰度 | ⬜，Web 路线 | **另计 28–43 人日** | 生成 client、共享 reducer、恢复 UX、单执行 canary |
| RUN-F7 | Channel 可选扩面 | ⏸，首版后 | 另行估算 | v1 endpoint 的 v2 投影、绑定灰度、ReplySession 不变 |
| RUN-F8 | 生产韧性与更多 target 扩面 | ⬜，首版后 | 另行估算 | 深度容量/灾备、归档、第二 target、运营闸门 |

**首版 Run Service v2 后端实现固定为 47–70 人日**：RUN-F1～F5 合计 47–70，算术可核对且不另立第二套总数。RUN-F0 后端 2–3 人日归入两仓 F0 的 4–6 人日，不重复计入 Run Service 实现。Web/Desktop 的 CLP-SC 28–43、Channel 扩面和首版后的
生产韧性不计入 47–70。2–3 名熟悉现有执行/身份体系的工程师可并行 schema/client、
存储/dispatcher、target adapter，但 F2 的状态/事务基线必须先冻结。

### 责任与完成证据

| ID | 负责角色 | 前置依赖 | 最小完成证据 |
|---|---|---|---|
| RUN-F0 | Backend/Runtime owner；Client/Web owner | 无 | 五份后端文档、九份客户端文档、两仓入口；相对链接与 `git diff --check` 通过；独立纯文档提交 |
| RUN-F1 | Backend contract owner；Client contract owner | RUN-F0 | canonical schema、生成物零漂移、状态 property/compatibility fixtures |
| RUN-F2 | Backend storage owner；DB/Platform owner | RUN-F1；PostgreSQL 运维评审 | migration、真实 PostgreSQL 并发/CAS/seq/outbox 原子性与升级证据 |
| RUN-F3 | Backend runtime owner；Platform owner | RUN-F2；Valkey 运维评审 | dispatcher crash/duplicate/outage/trim 恢复、backlog 告警和对账证据 |
| RUN-F4 | Runtime owner；Dialog/Canvas domain owner | RUN-F2/F3；target terminal barrier | lease/fence、kill-point、单执行、四终态与首个真实 target smoke |
| RUN-F5 | API owner；Identity/authorization owner；QA | RUN-F1–F4；canonical Principal/policy | API/WS/SSE/interaction/cancel 鉴权、重放/慢消费者、N/N-1 与安全集成报告 |
| CLP-SC | Client/Web owner；Backend contract owner | RUN-F5 canary；Web CLP-P0 | 生成客户端、同一 golden corpus、Web/Desktop 恢复与单执行灰度证据 |

## 3. 分阶段范围与验收

### RUN-F0 · 架构与契约基线

范围：

- 明确当前 request-bound SSE 与目标 durable v2；
- 冻结 PostgreSQL 真相源、outbox → Valkey Streams、四终态、取消鉴权和无双执行 fallback；
- 建立 API/event/灰度/部署文档；
- Channel/EIM 只链接，不复制。

验收：五份文档互链有效；根 README 有入口；`git diff --check` 通过；没有代码、依赖、配置或 migration
变化。F0 不代表任何 v2 能力已实现。

### RUN-F1 · 机器契约与领域骨架

范围：

- 冻结 Run 状态：非终态与精确终态 `completed|failed|cancelled|interrupted`；
- canonical OpenAPI/event JSON Schema；公共 envelope 固定为
  `v:2,event_id,run_id,thread_id,turn_id,seq,type,created_at,data`；
- 纯领域 transition reducer、idempotency digest、event envelope；
- import-linter/架构测试确保 protocol 不依赖 route/DB/Valkey/Channel；
- feature flag 默认关闭。

当前切片（RUN-F1a，2026-08-13）：`domain.py/schemas.py/events.py`、两份生成 JSON Schema、schema
drift 检查、合法/非法转换与 envelope/replay 单测已落地；没有路由、migration、worker、配置或生产流量。
只冻结文档已有 exact shape 的 envelope 与 `message.delta`，没有猜创建请求、snapshot、interaction、
terminal summary 或 `message.snapshot` 的 wire ABI。身份依赖只记录为 EIM 前置，未实现 SDK service principal、
Channel workload 或 tenant role 推导。

剩余：其余核心 event data 的 ADR/schema、OpenAPI 组合、TypeScript 生成物与 Web 只读 golden consumer、
N/N-1 additive fixtures、默认关闭的 composition flag。上述证据齐全前不得把 RUN-F1 标为完成。

验收：

- 合法/非法转换表全覆盖，terminal 不能复活；
- schema generation/check 无漂移，N/N-1 additive fixture 通过；
- TS client 能消费 golden events，但不发生产请求；
- 不新增运行路由，不改变 v1 wire。

### RUN-F2 · PostgreSQL ledger 与 transactional outbox

范围：

- additive Alembic migration：Run、RunEvent、RunRequestDedup、RunOutbox；
- tenant/subject/idempotency 唯一约束、`run_id + seq` 唯一约束、terminal/CAS invariants；
- async repository/service；
- 创建、状态迁移、事件、outbox 同事务；
- 数据保留和索引容量评审。

验收：

- 真 PostgreSQL 并发测试证明 cancel/completed 只赢一个终态；
- commit 后响应丢失，同 key 返回同 Run；不同 digest 冲突；
- 100 个并发 writer 的 per-Run seq 无重复/空洞；
- migration fresh install、upgrade、downgrade policy 和多实例 `SKIP LOCKED` 通过；
- 老 API/worker 在只有新表的部署半态中行为不变。

### RUN-F3 · Outbox dispatcher 与 Valkey Streams

范围：

- 独立 dispatcher 生命周期；有界 batch、claim、retry/backoff、dead-letter/告警；
- `event_id` 稳定投递、partition key、stream trim 与 PG watermark 对账；
- Valkey outage/recovery；
- ready/event topic 分离，避免 UI 慢消费者影响 worker 调度。

验收：

- commit 前永不发布；commit 后最终可发布；dispatcher crash 可重复但不丢；
- Valkey flush/trim/重启后从 PostgreSQL 重建，不改变 Run 状态；
- 重复 `XADD` 不触发第二次 target 执行；
- backlog、publish latency、retry/dead-letter 有无 PII 的指标和告警；
- dispatcher 停止时 v1 完全不受影响。

### RUN-F4 · Worker claim 与首个 target adapter

范围：

- PostgreSQL Run lease/fence，consumer group 只作唤醒；
- 首期只选一个低副作用 target（优先 Dialog）验证完整链路；
- target-neutral adapter、输出合并、terminal snapshot；
- 同一 Run 的安全 resume 条件；不安全恢复变 `interrupted`；
- retry/regenerate 创建新 `run_id`，不引入第二层公共执行尝试资源。

验收：

- 多 worker 竞争只有一个 fence 可以写事件/终态；
- worker 在 claim 前、执行中、terminal commit 前后被 kill 的行为有确定证据；
- 无 checkpoint/未知副作用时不会自动从头重演，Run 收口为 `interrupted`；
- delta 不按 token 写库，终态 snapshot 与公开 history barrier 一致；
- target adapter 测试不依赖 Channel Provider。

### RUN-F5 · v2 API、interaction、WS/SSE replay 与取消授权

范围：

- `POST/GET runs`、`GET events?after={seq}`、`POST interactions`、cancel；
- `WS /api/v2/runs/stream` 多 Run 订阅；企业代理阻断时按同 cursor 降级单 Run SSE；
- canonical Principal/workload dependency 与 RunAuthorization port；
- PG catch-up + Valkey wake-up + gap closure；
- 速率、租户配额、慢客户端 `slow_consumer + cursor` 主动重连和审计；
- OpenAPI/生成 client 发布。

验收：

- create 返回 202 时 Run 必已持久化；
- 首连、断线、重复 cursor、header/query 冲突、Valkey 丢通知均无丢序；
- 多 Run WS 各自保序；企业代理拒绝 upgrade 后 SSE 回放同一事件；慢消费者收到
  `slow_consumer + cursor` 后主动重连且不丢事件；
- interaction 的身份、revision、expiry、one-shot CAS 和重授权 fail closed；
- 断开 SSE 不产生 cancel；
- cross-tenant ID、伪造 owner/tenant、失效 membership、越权 workload 全部 fail closed；
- queued/running cancel 与 completed race 只产生四终态之一；
- v2 feature flag 关闭时不暴露半成品入口。

### CLP-SC · Web/Desktop 客户端与灰度（Web 路线，另计 28–43 人日）

范围：

- Web 仓消费生成 client，不手写事件 shape；
- Run reducer/adapter 把 v2 事件归一到现有 UI；
- Desktop/Web 共用 remote Run client，平台差异只在 transport/capability adapter；
- 多 Run WS、企业代理阻断后的 SSE fallback、`slow_consumer + cursor` 重连；
- 恢复、interaction、interrupted、取消 pending、cursor expired UX；
- 服务端按 tenant/user/target allowlist 做单执行灰度。

验收：

- 同一业务请求只走 v1 或 v2，日志/数据库可证明没有双执行；
- 浏览器刷新、桌面重启和网络切换后继续同一 `run_id`；
- v2 已接受后的网络错误只重连同一 Run，不调用 v1 fallback；
- v1 用户行为零回归；
- Web 与 Desktop 使用相同 golden event corpus。

### RUN-F7 · Channel 可选扩面（首版后，不计入 47–70）

依赖：RUN-F5 与 CLP-SC 稳定、Channel 当前现场 smoke 完成、身份/Principal 依赖满足；若涉及高风险 MCP，再依赖
EIM 对应 delegated authorization 和 replay/confirmation 阶段。

范围：当前 v1 endpoint 可按 binding allowlist 由 v2 独占执行，再投影回 v1 SSE；Channel worker、
ReplySession 和 Provider card wire 保持不变。

验收：

- v1/v2 mapping fixtures 全覆盖；未知 v2 additive event 不泄漏进 v1；
- queued/running cancel、regenerate/retry、graceful shutdown 语义不倒退；
- worker 不直连 Run DB；
- v2 接受后连接断开不发起第二次 execution；
- 每个 binding 可单独回到 v1，已接受的 v2 Run 继续由 v2 收口。

### RUN-F8 · 生产韧性与更多 target 扩面（首版后，不计入 47–70）

范围：

- 第二 target、interaction/tool events（只有授权/幂等契约具备时）；
- 数据归档/删除、租户配额、公平调度、灾备；
- SLO、容量压测、故障演练、运营 runbook；
- 评审 v1 退出条件，但不预设删除日期。

验收：目标吞吐/延迟/恢复时间由容量模型冻结；kill、滚动升级、PG failover、Valkey outage、慢消费者、
outbox backlog 和未知副作用演练通过；安全/隐私审查无 blocker。

## 4. 外部依赖与阻塞门

| 依赖 | 所有者 | Run Platform 的等待条件 |
|---|---|---|
| canonical Principal / AuthenticationContext | EIM | Web/API 与 resolved external identity 有稳定 dependency |
| tenant/target authorization | EIM + target domain | create/read/cancel policy 可按当前 membership 重验 |
| MCP delegated auth / confirmation / replay | EIM/of_mcp | 高风险 tool event 与自动恢复前完成 |
| Dialog/Canvas terminal barrier | target adapters | 能区分 delta、权威终态、未知 commit |
| Client generated protocol | Web/client | schema 生成与 N/N-1 门禁 |
| PostgreSQL/Valkey 生产运维 | platform ops | 备份、容量、告警和滚动部署方案批准 |

依赖未满足时缩小 target/surface allowlist，不用固定 service credential 冒充用户权限。

## 5. 验证矩阵

每个代码阶段至少覆盖：

| 层 | 必测 |
|---|---|
| domain | 状态 property tests、四终态、非法复活、digest |
| PostgreSQL | 并发 CAS、seq、dedup、outbox 原子性、migration、lease/fence |
| Valkey | duplicate、outage、trim、consumer restart、backpressure |
| API | auth、tenant isolation、idempotency、interaction、cursor、多 Run WS、SSE fallback、slow consumer、rate/capacity |
| target | terminal barrier、delta coalescing、cancel、interrupted、artifact auth |
| compatibility | v1 projection、N/N-1、tolerate-before-emit、generated client |
| E2E | refresh/reconnect、desktop restart、worker kill、rolling deploy、真实 target smoke |
| operations | metrics/log redaction、backlog alert、reconciliation、rollback |

代码改动仍须遵守根 [AGENTS.md](../../AGENTS.md) 的 `make verify`，DB/Valkey 路径加
`REQUIRE_SERVICES=1 make integration`；部署/真实 smoke 证据不能由单元测试替代。

## 6. 部署顺序与 v1/v2 灰度

### 6.1 Additive-first 顺序

1. **Schema consumer first**：发布生成类型和 tolerant consumer，仍不 emit v2；
2. **数据库 migration**：只加表/索引/约束，旧 API/worker 不读写；
3. **Repository dark launch**：部署状态/outbox 代码，feature flag 关闭；
4. **Dispatcher**：部署但只处理 v2 topic；验证 backlog/retry；
5. **Worker**：部署 claim/adapter，target allowlist 为空；
6. **v2 API internal canary**：只允许测试 tenant/Principal/target；
7. **Web/Desktop canary**：服务端 allowlist 选择执行所有者；
8. **扩大 v2**：按 tenant → target → surface 分批，观察完整终态和恢复指标；
9. **Channel opt-in**：最后按 binding 切换 v1 endpoint 的执行后端；
10. **v1 退出评审**：只有长期证据满足才单独立项，不能与首次 v2 上线同批删除。

若 private worker DTO/wire 改变，仍按 Channel 的 tolerate-then-emit 规则拆成两次发布，中间重启相应
长期进程；API 部署不会自动重启 supervisor/worker。

### 6.2 灰度模式

服务端为**新请求**选择以下一种模式：

| 模式 | 执行所有者 | 用途 |
|---|---|---|
| `v1` | 当前 request-bound path | 默认和回滚 |
| `v2_canary` | durable v2 | allowlist 的真实单执行试点 |
| `v2` | durable v2 | 扩大后的默认 |

不设置“双跑比较答案”模式。若需要 shadow，只允许离线重放脱敏 fixture、schema 校验或不调用 target 的
控制面演练；不得让同一用户请求同时调用两次模型、工具或写两份历史。

### 6.3 回滚规则

- 回滚开关只影响**尚未接受的新请求**；
- 已返回 v2 `run_id` 的 Run 必须继续由 v2 worker/对账器终态化；
- v2 create 在 PostgreSQL commit 后响应丢失，客户端复用 idempotency key 查询，不能 fallback v1；
- dispatcher/Valkey 故障不允许把已持久化 Run 转交 v1；
- migration 在 v2 writers/历史数据仍存在时不回滚删表；代码回滚必须兼容 additive schema；
- Channel binding 回 v1 前先停止接收新的 v2 Run，已有 v2 Run 继续完成或明确 `interrupted`。

### 6.4 扩量/暂停指标

扩量至少观察：create/cancel p95/p99、queue age、terminal ratio、`interrupted` ratio、lease takeover、
outbox oldest age/publish retry、SSE reconnect/gap、Valkey lag、PG lock/transaction latency、按 target 的
terminal commit/交付不一致。任何租户隔离、双执行、terminal 复活、事件缺口或未知副作用自动重演
立即停止扩量。

## 7. v1 退出门槛

只有同时满足以下条件才允许提出删除 v1：

- Web/Desktop/Channel 的目标流量均已在 v2 稳定运行完整观察周期；
- N/N-1、断线、kill、滚动升级、PG/Valkey 故障演练通过；
- 没有生产消费者依赖 `[DONE]` 或 v1 三事件之外的隐式行为；
- v2 的授权覆盖所有 v1 入口，不存在以 service credential 冒充用户的降级；
- 数据保留、删除、审计、SLO 和值班 runbook 已批准；
- 删除走独立 ADR、独立发布和可验证的消费者清单。

F0 不设 v1 删除日期。
