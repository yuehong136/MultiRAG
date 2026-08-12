# Run Platform 决策记录

> 本文件记录目标架构决策，不表示代码已经实现。
> 改变 API major、四终态、真相源、执行所有权、取消授权或部署顺序前，必须先修订/新增 ADR。

## 索引

| ADR | 决策 | 状态 |
|---|---|---|
| [RUN-ADR-01](#run-adr-01--run-platform-独占远程运行契约) | Run Platform 独占远程运行契约 | 采纳，目标态 |
| [RUN-ADR-02](#run-adr-02--保留-v1新增-v2而不是原地改线) | 保留 v1，新增 v2，不原地改线 | 采纳，目标态 |
| [RUN-ADR-03](#run-adr-03--postgresql-是真相源valkey-streams-只做投递) | PostgreSQL 真相源，Valkey Streams 只做投递 | 采纳，目标态 |
| [RUN-ADR-04](#run-adr-04--事件与-outbox-同事务再异步发布) | 事件与 outbox 同事务，再异步发布 | 采纳，目标态 |
| [RUN-ADR-05](#run-adr-05--每次尝试就是一个-run不引入二级尝试资源) | 每次尝试就是一个 Run，不引入二级尝试资源 | 采纳，目标态 |
| [RUN-ADR-06](#run-adr-06--终态精确为四种且不可逆) | 终态精确为四种且不可逆 | 采纳，目标态 |
| [RUN-ADR-07](#run-adr-07--取消是受权的持久意图) | 取消是受权的持久意图 | 采纳，目标态 |
| [RUN-ADR-08](#run-adr-08--同一请求只有一个执行所有者) | 同一请求只有一个执行所有者 | 采纳，目标态 |
| [RUN-ADR-09](#run-adr-09--协议由服务端-schema-生成客户端) | 服务端 Schema 生成客户端 | 采纳，目标态 |
| [RUN-ADR-10](#run-adr-10--依赖版本使用受支持稳定通道) | 依赖版本使用受支持稳定通道 | 采纳，目标态 |
| [RUN-ADR-11](#run-adr-11--多-run-websocket-为首选企业代理阻断时回退-sse) | 多 Run WebSocket 首选，代理阻断时回退 SSE | 采纳，目标态 |
| [RUN-ADR-12](#run-adr-12--公共消息事件使用-message-命名) | 公共消息事件使用 `message.*` 命名 | 采纳，目标态 |

## RUN-ADR-01 · Run Platform 独占远程运行契约

**日期**：2026-08-13
**状态**：采纳，目标态；F0 仅文档

**背景**：Channel 文档已有 Provider/target、历史事务和 CHN-O14 背景，EIM 文档已有 Principal/MCP
授权；若在这些项目分别定义 Run API/事件/状态，会出现多个真相源。

**决策**：`docs/run-platform/` 独占远程 Run API、事件 Schema、PostgreSQL 状态机、outbox → Valkey、
取消授权依赖和 v1/v2 部署。Channel/EIM 只链接本目录，并继续独占各自领域。

**后果**：Run 变更只在一个地方评审；相邻项目不复制表格。代价是跨项目任务必须显式链接依赖。

**失效条件**：Run Platform 被拆成独立服务/仓库时，权威契约随服务迁移，本目录改为版本化指针。

---

## RUN-ADR-02 · 保留 v1，新增 v2，而不是原地改线

**日期**：2026-08-13
**状态**：采纳，目标态

**背景**：当前 `POST /api/v1/internal/channel-bindings/.../executions` 是 request-bound SSE，DTO
`extra="forbid"`，worker 是长驻进程。直接加 durable 字段或改终止语义会让新旧 API/worker 互相拒绝，
并把一次基础设施升级变成 Channel 全量切换。

**决策**：新增 `/api/v2/runs` 资源和事件协议。v1 wire 保持不变；后期服务端 adapter 可以让选中
v1 请求由 v2 独占执行并投影回旧三事件/`[DONE]`。

**否决**：

- 在 v1 SSE 中直接加入 Run 状态并要求旧 worker 理解；
- 首次 v2 上线时同时删除 v1；
- 前端先切 v2、后端随后补路由。

**后果**：迁移时间更长，但可以 additive-first、逐 tenant/binding 回滚。v1 删除必须独立立项。

---

## RUN-ADR-03 · PostgreSQL 是真相源，Valkey Streams 只做投递

**日期**：2026-08-13
**状态**：采纳，目标态

**背景**：Valkey Streams 提供低延迟消费与 consumer group，但 trim、flush、重启、重复投递和
consumer ownership 都不适合作为企业 Run 的最终状态证明。

**决策**：Run 状态、事件、dedup、lease/fence 和 outbox 以 PostgreSQL 为唯一权威。Valkey Streams
承载 ready/event 通知；worker/gateway 被唤醒后仍按 PostgreSQL 状态/watermark 行动。

**否决**：

- 只在 Valkey hash/stream 保存 Run；
- 以 pending entries list 判断 Run 是否 running；
- Valkey 消息被 ack 就认为业务 terminal；
- 每个 SSE 连接轮询整张 PostgreSQL 表。

**后果**：多一次持久化写和 dispatcher 组件；换来可审计、可恢复、与缓存生命周期解耦的状态。

**失效条件**：未来采用具备同等事务、回放、约束和灾备保证的专用 durable log；迁移需独立 ADR。

---

## RUN-ADR-04 · 事件与 outbox 同事务，再异步发布

**日期**：2026-08-13
**状态**：采纳，目标态

**背景**：先改数据库后 `XADD` 会在进程崩溃时丢通知；先 `XADD` 后提交数据库会让消费者看到从未
成立的状态。跨 PostgreSQL/Valkey 不存在本项目可用的分布式事务。

**决策**：状态迁移、不可变 RunEvent、RunOutbox 同一个 PostgreSQL 事务。Dispatcher 以
at-least-once 发布稳定 `event_id`；下游幂等，PG watermark 负责补洞。

**后果**：允许重复，不允许丢失已提交意图。需要 outbox backlog、重试、dead-letter 和对账运维。

**失效条件**：只有统一存储能同时提供约束事务和低延迟订阅，并通过故障测试，才评估去除 outbox。

---

## RUN-ADR-05 · 每次尝试就是一个 Run，不引入二级尝试资源

**日期**：2026-08-13
**状态**：采纳，目标态

**背景**：两层公共执行生命周期会让客户端、事件 cursor、取消和 retry 产生双重状态；
用户真正关心的是“这一次尝试”。

**决策**：每次 message/regenerate/retry 都有唯一新 `run_id`。关系用 `retry_of_run_id`、
`supersedes_run_id` 表达。worker lease/fence 是内部执行所有权，不进入公共资源或事件 envelope。

同一 Run 只有在 target 提供安全 checkpoint/resume 时才能跨 worker 继续；否则崩溃/未知结果收口为
`interrupted`，用户重试创建新 Run。

**否决**：

- terminal Run 改回 running；
- 在同一 `run_id` 下静默从头执行第二遍；
- 让客户端选择内部 lease generation。

**后果**：协议和 UX 简单，审计清晰；相同业务意图的多次尝试需要通过关系字段聚合。

---

## RUN-ADR-06 · 终态精确为四种且不可逆

**日期**：2026-08-13
**状态**：采纳，目标态

**决策**：公共 Run 终态集合精确为：

- `completed`：权威输出/产物和 terminal barrier 已确认；
- `failed`：执行确认失败，安全错误已记录；
- `cancelled`：取消赢得 CAS，且没有需要伪装为 completed 的已提交结果；
- `interrupted`：崩溃、断点无法安全恢复、提交/副作用结果未知或被平台中断，需要恢复/对账/新 Run。

任何终态不可回到非终态；对应 terminal event 精确为 `run.completed`、`run.failed`、`run.cancelled`、
`run.interrupted`，且每 Run 只有一个。

**否决**：使用其他完成/错误同义状态；增加第五种“未知结果”公开终态；把 SSE 断开直接记
cancelled。

**后果**：跨客户端 reducer 与分析口径稳定；`interrupted` 需要明确 UX 和运营对账，不能等同 failed。

---

## RUN-ADR-07 · 取消是受权的持久意图

**日期**：2026-08-13
**状态**：采纳，目标态

**背景**：取消可能来自 Web、Desktop、Channel workload 或管理员；HTTP 断开不是身份和授权证据，
进程内 task cancellation 也无法跨实例恢复。

**决策**：cancel 固定为 authenticate → authorize current Run → 持久化 cancel intent/event/outbox →
worker 协作终止 → terminal CAS。服务端从 Run 解析 tenant/owner/target，重验当前 membership/policy。

**否决**：

- 信任请求体 `tenant_id/principal_id/is_admin`；
- 把 Channel `actor.subject` 直接当 Principal；
- SSE disconnect 自动取消；
- 已 completed 后仍向用户显示 cancelled；
- cancel 承诺回滚已发生的外部副作用。

**后果**：取消可能先返回 `cancel_requested`，最终状态稍后确定；客户端必须展示 pending/actual winner。

---

## RUN-ADR-08 · 同一请求只有一个执行所有者

**日期**：2026-08-13
**状态**：采纳，目标态

**背景**：为了灰度而 v1/v2 双跑会重复模型成本、历史写入、MCP/OA 副作用；v2 已 commit 后因响应
超时 fallback v1 同样会双执行。

**决策**：服务端在接受前为新请求选择 `v1|v2_canary|v2` 中一个执行所有者。v2 已持久化后，所有
重试都使用原 idempotency key/run_id；回滚只影响新请求，已有 v2 Run 由 v2 收口。

**允许的 shadow**：离线脱敏 fixture、schema/reducer 比较、不调用 target 的控制面演练。

**后果**：不能在线比较两份真实答案；换来副作用和成本安全。灰度质量用 canary cohort 比较。

---

## RUN-ADR-09 · 协议由服务端 Schema 生成客户端

**日期**：2026-08-13
**状态**：采纳，目标态

**背景**：MultiRAG 与 Web 是独立仓库，手工维护 Python/TypeScript event union 会漂移；长期还会有
Desktop、Channel 和 SDK 消费者。

**决策**：MultiRAG 拥有 canonical OpenAPI 和 event JSON Schema，提交/发布生成物或版本化 client。
消费者使用生成类型并在自己的 adapter/reducer 内归一；CI 检查生成物无漂移和 N/N-1 compatibility。

**版本规则**：v2 公共 envelope 必需字段固定为
`v:2,event_id,run_id,thread_id,turn_id,seq,type,created_at,data`。内部 commit/trace/lease 字段不得替换
这些公共字段；不兼容变更另开 major。先部署 tolerant consumer，再 emit 新 additive event。

**后果**：需要 codegen/release lane；换来跨仓可审计契约。UI-specific view model 不反向进入后端 schema。

---

## RUN-ADR-10 · 依赖版本使用受支持稳定通道

**日期**：2026-08-13
**状态**：采纳，目标态

**背景**：Python、PostgreSQL、Valkey、FastAPI、Pydantic、驱动和观测栈的 patch 版本会持续更新；在
多份架构文档复制具体版本会快速漂移，并把架构决策与维护清单混为一谈。

**决策**：Run Platform 设计只要求仓库批准的、仍受支持的稳定通道和锁文件可复现。精确升级基线由
专门版本核验/依赖任务维护；任何升级必须跑真实 PostgreSQL、Valkey、SSE、driver 和滚动兼容测试。

**否决**：

- 在本目录维护第二份精确 patch 版本表；
- 为追新版本绕过 lock/CI；
- 未验证就把 preview/beta runtime 作为 Run Platform 生产基础。

**后果**：架构文档低漂移；实施任务必须在开工时重新核验版本与官方支持状态，不能照抄 F0 日期。

---

## RUN-ADR-11 · 多 Run WebSocket 为首选，企业代理阻断时回退 SSE

**日期**：2026-08-13
**状态**：采纳，目标态

**背景**：Web/Desktop 需要同时观察多个 Run，逐 Run 长连接会放大连接和网关成本；但企业代理、
VPN 或安全网关可能拒绝 WebSocket upgrade。传输差异不能改变事件语义或迫使客户端重建 Run。

**决策**：

- `WS /api/v2/runs/stream` 是多 Run 实时订阅首选，每个 Run 携带自己的 `after` cursor；
- `GET /api/v2/runs/{run_id}/events?after={seq}` 是权威分页回放；单 Run SSE 使用同一 `after` cursor；
- 企业代理阻断 WS 时，客户端自动切到每个活跃 Run 的 SSE catch-up/tail；
- PostgreSQL 始终负责 gap fill，Valkey 只唤醒，无论 WS/SSE 都发送同一 v2 event envelope；
- 发送缓冲有界；慢消费者收到 `slow_consumer + cursor` control 后释放旧连接并主动重连，
  WS 仍不可用时以该 cursor 转 SSE；control 不进入 Run event seq。

**否决**：

- WS 失败后重新 `POST /runs`；
- WS 与 SSE 使用不同事件 shape/cursor；
- 为慢客户端无限扩展内存队列；
- `slow_consumer` 冒充 RunEvent 或改变 Run 状态；
- 企业代理环境改用高频状态轮询且放弃事件回放。

**后果**：需要两种 transport 和共享 contract/golden fixtures；换来多 Run 性能、企业网络兼容和一致
恢复语义。WS/SSE 可分别开关，但事件查询端点始终是恢复底座。

---

## RUN-ADR-12 · 公共消息事件使用 message 命名

**日期**：2026-08-13
**状态**：采纳，目标态

**背景**：批准的 v2 envelope 示例使用 `message.delta`。若后端再公开 `output.delta`，生成客户端和迁移 fixture 会从 F0 起产生两套名称。

**决策**：首期公共文本事件固定为 `message.delta` 和权威 `message.snapshot`。Runner 内部可以使用 output/chunk 等实现术语，但不得进入公共 Schema。只有可替代的 delta/progress 可以合并；终态、tool-call 和 approval/interaction 生命周期事件不得合并、采样或丢弃。

**后果**：公共协议与批准示例一致；v1 adapter 显式将 `message.delta` / `message.snapshot + run.completed` 投影为旧 `message_delta` / `message_completed`。
