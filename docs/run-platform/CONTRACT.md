# Run Platform v2 契约

> 状态：F0 规范草案，**路由、表和事件尚未实现**
> 目标前缀：`/api/v2/runs`
> 当前 v1 仍以代码和 [Channel CONTRACT](../channel-program/CONTRACT.md) 为准

本文独占远程 Run API 与事件 Schema。示例用于冻结语义，不代表已经生成 OpenAPI 或数据库模型；
F1 必须把最终 Schema 变成机器可校验的唯一真相源，并由客户端生成类型，禁止手工复制 shape。

## 1. 契约原则

1. API 是资源式控制面；SSE 是同一资源的事件投影，不是执行所有者。
2. 创建成功的定义是 PostgreSQL 已持久化，不是 worker 已开始或首 token 已到达。
3. 所有 server-controlled identity、tenant、target revision、policy decision 不接受客户端覆盖。
4. 每次写操作使用 `Idempotency-Key`；重复网络请求不产生第二个 Run/取消/恢复。
5. 事件按 Run 严格排序；客户端只用 `seq` 驱动 reducer，不用到达时间猜状态。
6. 未知事件类型可按协商规则忽略，但未知 schema major 必须拒绝，不能错误解释。
7. v2 接受 Run 后不自动回退 v1，避免模型/tool 双执行。

## 2. 资源与标识

| 资源 | 标识 | 说明 |
|---|---|---|
| Run | `run_id` | 一次不可逆生命周期；retry/regenerate 创建新 Run |
| Event | `event_id`, `run_id + seq` | 不可变；`seq` 是客户端 cursor |
| Artifact | `artifact_id` | 大结果的授权引用；不能直接暴露内部对象存储 URL |
| Thread / Conversation | `thread_id` | 业务会话关系；不是 Run 状态容器 |
| Turn | `turn_id` | 本次 Run 所属的服务端权威轮次；retry/regenerate 的轮次关系由 target 语义决定 |
| ToolCall | `tool_call_id` | Run 内工具调用及其持久状态；副作用、授权和幂等语义必须可审计 |
| Interaction | `interaction_id` | 等待用户输入/审批的持久资源；一次性提交并绑定参数摘要与 Principal |

标识是不可枚举的 opaque ID。API 响应不能因跨租户 ID 是否存在而泄露资源。

## 3. API 清单

| 方法 | 路径 | 语义 | 首期 |
|---|---|---|---|
| `POST` | `/api/v2/runs` | 幂等创建并持久化 Run | 必须 |
| `GET` | `/api/v2/runs/{run_id}` | 获取当前快照/watermark | 必须 |
| `GET` | `/api/v2/runs/{run_id}/events?after={seq}&limit={limit}` | 按 cursor 分页回放事件 | 必须 |
| `GET` | `/api/v2/runs/{run_id}/stream?after={seq}` | 单 Run SSE catch-up + tail | 必须 |
| `WS` | `/api/v2/runs/stream` | 一条连接订阅多个 Run；支持各自 cursor | 必须 |
| `POST` | `/api/v2/runs/{run_id}:cancel` | 鉴权后持久化取消意图 | 必须 |
| `POST` | `/api/v2/runs/{run_id}:retry` | 创建关联的新 `run_id` | 可后置 |
| `POST` | `/api/v2/runs/{run_id}/interactions` | 幂等提交当前 Run 要求的输入/确认 | 必须 |

不提供“把 terminal Run 改回 running”的接口。列表/管理端点必须另行定义查询授权、分页和保留策略，
不从单 Run 契约自然推出。

## 4. 创建 Run

### 4.1 请求

```http
POST /api/v2/runs
Authorization: Bearer <credential>
Idempotency-Key: <opaque-client-key>
Content-Type: application/json
```

```json
{
  "target": {
    "type": "multirag.dialog",
    "id": "target-reference",
    "revision": "optional-client-observed-revision"
  },
  "thread_id": "optional-thread-reference",
  "operation": "message",
  "input": {
    "parts": [
      {"type": "text", "text": "user input"}
    ]
  },
  "client_context": {
    "surface": "web"
  }
}
```

规则：

- `tenant_id`、`principal_id`、role、scope、resolved target configuration 不在请求体；
- `target.revision` 是并发前置条件或客户端观察值，服务端必须重新加载和授权，不能直接信任；
- `operation` 首期至少支持 `message|regenerate`，regenerate 的业务前置条件由 target adapter 验证；
- input part 使用判别联合；附件只接受已授权 artifact/upload reference；
- `client_context.surface` 只用于非安全 UX/指标，不参与授权；
- canonical digest 覆盖所有影响执行的字段，不覆盖 trace header 等传输元数据。

### 4.2 响应

```http
HTTP/1.1 202 Accepted
Location: /api/v2/runs/run_...
```

```json
{
  "run": {
    "id": "run_...",
    "status": "queued",
    "desired_state": "running",
    "version": 1,
    "last_event_seq": 2,
    "thread_id": "thread_...",
    "turn_id": "turn_...",
    "target": {
      "type": "multirag.dialog",
      "id": "target-reference",
      "revision": "server-resolved-revision"
    },
    "created_at": "2026-08-13T00:00:00Z",
    "updated_at": "2026-08-13T00:00:00Z"
  },
  "links": {
    "self": "/api/v2/runs/run_...",
    "events": "/api/v2/runs/run_.../events",
    "stream": "/api/v2/runs/run_.../stream"
  }
}
```

同一主体/tenant/operation 的相同 Idempotency-Key：

- digest 相同：返回原 `run_id` 和当前 snapshot；
- digest 不同：`409 IDEMPOTENCY_KEY_REUSED`；
- 首请求结果未知：客户端必须复用原 key，不能生成新 key 猜测重试。

## 5. Run snapshot

`GET /runs/{id}` 的 snapshot 至少包含：

- `id`, `status`, `desired_state`, `version`；
- server-resolved target reference；
- `thread_id`, `turn_id`, relationship IDs（若授权可见）；
- `last_event_seq`, terminal code/reason（安全枚举）；
- created/updated/started/terminal timestamps；
- 可选 authoritative output/artifact summary，受大小和授权限制。

snapshot 是当前聚合，不代替事件日志。客户端先按事件 reducer 更新 UI，检测 gap 时以 snapshot +
事件回放重新对账。

## 6. 事件 envelope 与不变量

### 6.1 Envelope

```json
{
  "v": 2,
  "event_id": "evt_...",
  "run_id": "run_...",
  "thread_id": "thread_...",
  "turn_id": "turn_...",
  "seq": 7,
  "type": "message.delta",
  "created_at": "2026-08-13T00:00:01.130Z",
  "data": {
    "part_id": "answer",
    "delta": "text"
  }
}
```

字段规则：

- 上述九个字段是 v2 公共事件 envelope 的必需字段；不得用内部数据库/观测字段替换；
- `v` 是整数常量 `2`；不兼容协议必须使用新 major，不能改变 `v: 2` 既有字段含义；
- `thread_id` 与 `turn_id` 由服务端在接受 Run 时解析或分配，同一 Run 内保持不变；
- `seq` 由 PostgreSQL 为单 Run 分配，严格递增且已提交序列无空洞；
- `created_at` 是事件成为权威事实的服务端时间；排序只看 `seq`，不用时间戳排序；
- `data` 由 `type` 对应的 JSON Schema 校验；禁止任意未验证对象穿透。

数据库可另存 commit metadata、内部 lease/fence 和 trace context，但这些不是公共 envelope 字段。
transport control frame 也不是 RunEvent，不占 `seq`。

### 6.2 首期核心事件

| type | 关键 data | 语义 |
|---|---|---|
| `run.accepted` | request/target 摘要 | Run 已持久化 |
| `run.queued` | 可选 queue class | 可以被 worker claim，不承诺预计时间 |
| `run.started` | 安全的执行阶段摘要 | 内部 lease/fence 已建立，但不暴露第二层 attempt 资源 |
| `message.delta` | `part_id`, `delta` | 有界合并后的增量，不是最终历史 |
| `message.snapshot` | 完整可见 message 或 artifact ref | 权威终态输出 |
| `run.cancel_requested` | 安全 reason code | 取消意图已持久化，不等于已取消 |
| `interaction.required` | interaction ID、revision、expires_at、受控 schema | Run 等待持久输入/确认 |
| `interaction.submitted` | interaction ID、响应摘要 | 已鉴权、校验并 one-shot 接受；不回显敏感输入 |
| `run.completed` | usage/terminal summary | 唯一完成终态 |
| `run.failed` | 安全 error code、retryable hint | 唯一失败终态 |
| `run.cancelled` | cancellation stage | 唯一取消终态 |
| `run.interrupted` | reconciliation code | 崩溃、断点不可安全恢复或副作用结果未知，禁止原 Run 自动重演 |

后续扩展：`tool.call.*`、`approval.*`、`artifact.created`、`usage.updated`。这些事件只有在授权和
副作用幂等契约同步完成后才能 emit；不能只加前端渲染器。interaction 端点与上述两个基础事件属于
v2 必备线协议，但具体表单/确认类型仍须对应 schema 与 EIM 安全契约完成后才对目标开放。

### 6.3 强不变量

1. 第一个事件是 `run.accepted`；唯一 terminal lifecycle event 精确属于
   `run.completed|run.failed|run.cancelled|run.interrupted`，并且是最后一个状态迁移事件。
2. `run.completed` 同事务必须有 authoritative `message.snapshot`，或显式指向已提交 artifact/history。
3. terminal 后不得再出现 output/tool/state 事件；仅允许独立的审计/交付观测面，不进入 Run event seq。
4. 只有可替代的 `message.delta` / progress 可以为空缺、合并或延迟，客户端不得拼接它们作为唯一最终答案。
5. 每个 `event_id` 全局稳定；outbox 重发不能生成新 event ID 或新 seq。
6. 同一 Run 的 Valkey partition 保序；即使 transport 乱序，gateway 仍从 PostgreSQL 按 seq 输出。
7. error message 不含上游响应、prompt、tool 参数、凭据或内部堆栈；详细诊断只进受控审计。
8. 公开 Dialog/Canvas 历史只由 target terminal barrier 提交，RunEvent 不直接冒充业务消息。
9. 终态、tool-call 生命周期和 approval/interaction 生命周期事件一旦启用，必须持久化且不得合并、采样或丢弃；大 payload 可以改为授权 artifact 引用，但生命周期事实不能消失。

## 7. 事件查询和 SSE

### 7.1 分页回放

```http
GET /api/v2/runs/{run_id}/events?after=6&limit=100
```

响应包含严格升序 events、`cursor`、`last_event_seq` 和 `terminal`。`cursor` 表示客户端已安全处理到的
最后 `seq`；`limit` 有服务端上限；
客户端必须跟随 cursor，不用 offset。

### 7.2 单 Run SSE 回放与续传

```http
GET /api/v2/runs/{run_id}/stream?after=6
Last-Event-ID: 6
Accept: text/event-stream
```

若 query 和 header 同时存在且不同，返回 `400 CURSOR_CONFLICT`，不能猜一个。Wire：

```text
id: 7
event: message.delta
data: {完整 event envelope}

```

规则：

- 建连先从 PostgreSQL catch-up，再 tail；每次通知只触发按 watermark 查库；
- 重复 cursor 返回 `seq > cursor`，不会重发已确认事件；
- gap/重复由 gateway 处理；客户端仍按 `seq` 去重并检测非连续序列；
- heartbeat 是 SSE comment，不分配 event seq；
- terminal event 发出后服务端正常结束；v2 客户端不依赖 `data:[DONE]`；
- 客户端断开只取消订阅，不取消 Run；
- cursor 已超出保留窗口时返回明确的 snapshot/resync 错误，不从中间静默继续。

### 7.3 多 Run WebSocket 订阅

首选实时传输是一条多路复用连接：

```text
WS /api/v2/runs/stream
```

认证在 upgrade 边界完成。连接建立后，客户端发送每个 Run 独立 cursor 的订阅命令：

```json
{
  "action": "subscribe",
  "runs": [
    {"run_id": "run_a", "after": 6},
    {"run_id": "run_b", "after": 18}
  ]
}
```

服务端逐个 Run 鉴权，先从 PostgreSQL 补齐 `seq > after`，再通过同一连接发送 §6 的完整事件
envelope。取消订阅只影响连接，不取消 Run。新增/移除订阅是 transport control，不产生 RunEvent。

企业网络代理若阻断 WebSocket upgrade，客户端必须自动降级为每个活跃 Run 的 §7.2 SSE，并使用
相同 `after` cursor 从 PostgreSQL 回放；不能退化为重新创建 Run，也不能因传输切换丢事件。

### 7.4 慢消费者

每个 WS/SSE 连接都有有界发送缓冲。客户端落后到无法继续安全推送时，服务端先发送/暴露
`slow_consumer` control 和可恢复 cursor，再主动结束该 transport：

```json
{
  "type": "slow_consumer",
  "cursor": {
    "run_a": 42,
    "run_b": 19
  }
}
```

客户端收到后必须释放旧连接，并以这些 cursor 主动重连 WS；若企业代理仍阻断 WS，则以 cursor
转为 SSE 回放。`slow_consumer` 不是 RunEvent，不使用 `v/event_id/seq`，也不改变任何 Run 状态。

## 8. Interaction 契约

```http
POST /api/v2/runs/{run_id}/interactions
Authorization: Bearer <credential>
Idempotency-Key: <opaque-interaction-key>
Content-Type: application/json
```

```json
{
  "interaction_id": "interaction_...",
  "response": {
    "type": "confirmation",
    "decision": "confirm"
  }
}
```

- 服务端加载当前 Run 和尚未消费的 interaction，再按当前 Principal、tenant、target policy 与
  assurance 重验；请求体中的身份/权限声明无效；
- `interaction_id`、响应 canonical digest、expiry、revision 与 one-shot CAS 共同防止重放；
- 相同 key + 相同 digest 返回相同结果，不同 digest 冲突；过期、已消费或非当前 interaction fail closed；
- 接受后写入不可变 interaction event/outbox，并按状态机恢复同一个 Run；
- decline/cancel/expire 是显式状态事件，不把网络超时猜成用户选择；
- 涉及 MCP/OA 副作用时仍需 EIM/MCP 的重授权、确认凭据和业务幂等，Run interaction 不替代它们。

## 9. 取消契约

```http
POST /api/v2/runs/{run_id}:cancel
Authorization: Bearer <credential>
Idempotency-Key: <opaque-cancel-key>
Content-Type: application/json
```

```json
{
  "reason_code": "user_requested",
  "observed_run_version": 5
}
```

- 服务端先加载 Run 再按当前 Principal/membership/target policy 授权；
- `observed_run_version` 用于向用户暴露 stale UI，但不得取代服务端 CAS；
- 接受取消返回 `202` 和 `cancel_requested` snapshot；尚未 dispatch 时可在同事务直接 `cancelled`；
- 重复 cancel key 返回同一结果；不同 key 的重复取消也必须幂等地返回当前状态；
- Run 已 terminal 时返回 `200` 当前实际 terminal，不修改历史；
- cancel 与 completion 并发时返回 PostgreSQL 实际胜者；
- `cancel_requested` 不承诺撤销已经提交的 DB/MCP/OA 副作用。

授权失败使用不可枚举语义；管理员取消、service workload 取消和用户本人取消是不同 policy branch，
必须分别测试。外部 actor ID、Channel subject 或客户端传入 owner ID 都不是授权证据。

## 10. 认证与授权依赖

| 调用面 | Authentication | Authorization |
|---|---|---|
| Web/Desktop | canonical `Principal` / `AuthenticationContext` | 当前 tenant membership + target permission + Run ownership/policy |
| Channel worker | authenticated workload + binding generation | server-resolved binding/target；用户级行为还需 resolved Principal |
| 内部 service | workload identity / service credential | 明确 audience、scope、tenant/resource binding；不能获得全局隐式权限 |
| MCP/tool | Run Principal 的受控派生凭据 | resource server/PDP 对具体 tool/object 再授权 |

Run Platform 不定义 Principal 如何产生，也不签发 MCP token；它依赖
[企业身份与 MCP](../enterprise-identity-mcp/README.md) 的 canonical identity 和 authorization ports。
身份依赖未满足时 fail closed，不能把 fixed service header 当用户委托。

## 11. 错误契约

```json
{
  "error": {
    "code": "RUN_STATE_CONFLICT",
    "message": "The run state changed. Refresh and retry if the operation remains valid.",
    "trace_id": "opaque-trace-id",
    "details": {}
  }
}
```

首期稳定错误类别：

| HTTP | code | 场景 |
|---|---|---|
| 400 | `RUN_REQUEST_INVALID`, `CURSOR_CONFLICT` | schema/cursor 无效 |
| 401 | `AUTHENTICATION_REQUIRED` | credential 缺失/无效 |
| 403/404 | policy-controlled non-enumerable result | 无权访问/跨租户 |
| 409 | `IDEMPOTENCY_KEY_REUSED`, `RUN_STATE_CONFLICT`, `TARGET_REVISION_CONFLICT` | 幂等或状态冲突 |
| 410 | `EVENT_CURSOR_EXPIRED` | 事件已按已公布保留策略归档 |
| 409/410 | `INTERACTION_STATE_CONFLICT`, `INTERACTION_EXPIRED` | interaction 已消费、漂移或过期 |
| 422 | `RUN_OPERATION_UNSUPPORTED` | target 不支持请求的 operation |
| 429 | `RUN_CAPACITY_EXCEEDED` | 有界准入/租户配额 |
| 503 | `RUN_CONTROL_PLANE_UNAVAILABLE` | 无法可信持久化/授权/排队 |

`retryable` 只能作为安全提示；客户端不得对创建、tool dispatch 或结果未知的 `interrupted` Run
无条件自动重试。

## 12. v1/v2 兼容映射

当前 v1 wire 保持：`message_delta`、`message_completed`、`execution_failed`、`[DONE]`。未来 v1 adapter
可以在服务端**独占执行**一个 v2 Run，并映射：

| v2 | v1 投影 |
|---|---|
| `message.delta` | `message_delta` |
| `message.snapshot` + `run.completed` | `message_completed`，优先权威正文 |
| `run.failed` | `execution_failed` 安全 error code |
| `run.cancelled` | `execution_failed` 的兼容 cancellation code |
| `run.interrupted` | `execution_failed` 的兼容 interruption code |
| 其他 additive v2 event | v1 不输出 |

约束：

- 适配发生在服务端，不能给当前 `extra="forbid"` 的 v1 DTO 偷加字段；
- 单请求只选 v1 旧执行或 v2 adapter，禁止同时运行再比较答案；
- v2 已接受后，v1 HTTP 连接失败也只允许重连/查询该 Run，不能重启旧执行；
- v1 的 `[DONE]` 仅是兼容终止标记，不进入 v2 event log；
- 删除 v1 必须满足 [ROADMAP](ROADMAP.md) 的灰度与退出门槛，F0 不设删除日期。

## 13. Schema、版本与契约测试

F1 必须建立：

- 服务端 canonical OpenAPI + 每个 event `data` 的 JSON Schema；
- 由 canonical schema 生成的 TypeScript/Python client/types；
- `v: 2` envelope 兼容检查和生成物无漂移门禁；
- golden fixtures：正常、失败、cancel/completion race、未知 additive event、重连 gap、重复 outbox；
- N/N-1 producer-consumer 契约测试；
- 安全测试：跨租户枚举、伪造 principal/tenant、过期 membership、workload 越权、日志脱敏。

协议 major 升级必须新增 Run ADR 和独立 endpoint/media negotiation；不得在相同 `2.x` envelope 中
改变已有字段含义。
