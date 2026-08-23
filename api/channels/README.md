# MultiRAG Chat Channel

`api/channels` 是 MultiRAG 的外部消息通道运行时。它把飞书等平台事件规范化后交给
MultiRAG 自己的执行边界，不在传输层拼提示词，也不让外部消息决定租户、目标、发布版本或权限。

> **本文件描述的是「已上线的形态」。正在进行的加固与通用化工作看
> [`docs/channel-program/`](../../docs/channel-program/README.md)**——那里有任务账本（CHN ID）、
> 决策记录、前后端契约与跨仓部署顺序规则。动到 `api/channels/`、`api/channel_control/`、
> `api/channel_execution/`、`api/channel_runtime/` 时提交要带 CHN ID（AGENTS.md 核心规则 5）。
> 下面的「已实现 / 尚未实现或不能宣称」两节仍是**已上线行为的权威描述**，账本不重复它。

当前提供两种运行模式：

| 模式 | 入口 | 绑定来源 | 适用场景 |
|---|---|---|---|
| Demo worker | `python -m api.channels.worker --channel feishu` | 环境变量中的固定已发布 Agent | 单机器人、本地演示、快速验证 |
| Managed supervisor | `python -m api.channels.supervisor` | MultiRAG Channel 控制面和数据库 binding | 管理页面配置、多机器人、长期部署 |

两种模式都必须作为独立进程运行，不能嵌入 Uvicorn worker。这样可避免 reload 或多
worker 重复建立飞书长连接，也能隔离 `lark-oapi` 1.x 的进程级全局事件循环。

## MultiRAG 命名与系统边界

Channel 运行时只允许以下 MultiRAG 目标类型：

- `multirag.canvas_agent`：MultiRAG 已发布的 Agent 画布及其发布版本。
- `multirag.dialog`：MultiRAG 自己的 Dialog。

兼容字段 `chat_id` 也只映射为 `multirag.dialog`。Channel 代码、数据库约束、API DTO、
日志和执行服务不得使用其他产品的运行时命名空间，也不得调用其他产品的 API、数据库或
Dialog 服务。

本包的传输布局参考了上游开源项目 commit
`d6f1475c5c1fe266a6eab2c0acee9722d6720fea`（各源文件保留其原始版权头）。该上游在这里仅是 Apache-2.0
代码来源、实现风格和后续 Git 跟进参考；MultiRAG 的控制面、binding、执行服务、状态、
安全模型和运行进程全部是本项目自己的实现。

## 共同的传输边界

```text
飞书 SDK
  -> Channel 事件规范化
  -> 有界 asyncio 队列
  -> 消息过滤、去重、会话顺序控制
  -> MultiRAG 执行边界
  -> 回复原飞书消息
```

必须遵守以下边界：

- SDK 回调只规范化事件并投递队列，不等待模型或工具执行。
- `IncomingMessage.sender_id` 是不可信的外部标识，不是已认证的 MultiRAG 用户。
- 用户消息不能覆盖租户、binding、目标、版本、session、Principal 或工具权限。
- 不把外部身份伪装成 `Principal`，也不把身份字段拼进提示词要求模型“自报身份”。
- 只接受私聊文本；群聊和机器人消息静默忽略，非文本返回固定提示。
- 发送失败、上游异常和日志均不得包含问题、答案、原始事件、凭据或完整外部 ID。
- Redis 用于去重、会话状态和单 App ID leader lease；Redis 不可用时 fail closed。
- Agent/工具可能已经开始执行后，不自动重试同一次请求；副作用工具还必须在自身边界实现幂等。

## 模式一：Demo worker

Demo 模式用于当前“小丽”替换演示。一个进程固定连接一个飞书 App，并调用一个固定的
MultiRAG 已发布 Agent：

```text
飞书长连接
  -> Redis 去重和飞书用户会话映射
  -> POST /api/v1/agents/{agent_id}/completions
  -> 聚合最终 SSE 文本
  -> 回复飞书消息
```

该模式需要标准 MultiRAG Agent API Token，不使用 beta embed token。请求固定使用
`stream=true` 和 `release=true`，且不会发送飞书身份、`user_id`、`custom_header`、
`inputs` 或 `metadata`。

### 飞书应用准备

1. 启用机器人能力。
2. 事件接收方式选择长连接。
3. 订阅 `im.message.receive_v1`。
4. 开通 `im:message.p2p_msg:readonly` 和 `im:message:send_as_bot`。
5. 发布应用版本，并把演示账号加入可用范围。

长连接模式不需要公网回调 URL、Verification Token 或 Encrypt Key。

### Demo 环境变量

```text
MULTIRAG_CHANNELS__FEISHU__ENABLED=true
MULTIRAG_CHANNELS__FEISHU__APP_ID=<feishu-app-id>
MULTIRAG_CHANNELS__FEISHU__APP_SECRET=<from-secret-manager>
MULTIRAG_CHANNELS__FEISHU__MULTIRAG_BASE_URL=http://127.0.0.1:8123
MULTIRAG_CHANNELS__FEISHU__AGENT_ID=<published-multirag-agent-id>
MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN=<standard-multirag-api-token>
MULTIRAG_CHANNELS__FEISHU__RELEASE_MARKER=leadership-demo-v1
MULTIRAG_CHANNELS__FEISHU__DOMAIN=feishu
MULTIRAG_CHANNELS__FEISHU__ALLOWED_OPEN_IDS=[]
```

远程 API 地址必须使用 HTTPS；明文 HTTP 只接受 `localhost` 或字面量回环 IP。Base URL
必须是 origin 根地址，不得包含 userinfo、路径、查询参数或 fragment。

`ALLOWED_OPEN_IDS=[]` 表示只依赖飞书应用可用范围。定向演示建议同时使用非空白名单和
尽可能窄的飞书可用范围。

### 启动 Demo worker

Windows PowerShell：

```powershell
# 环境变量可在当前 PowerShell 或本机未提交的脚本中注入
uv run python -m api.channels.worker --channel feishu

# 仓库还提供仅供本地演示的模板
Copy-Item scripts/run_feishu_channel.example.ps1 scripts/run_feishu_channel.local.ps1
powershell -ExecutionPolicy Bypass -File scripts/run_feishu_channel.local.ps1
```

macOS、Linux 或 Ubuntu（bash/zsh）：

```bash
export MULTIRAG_CHANNELS__FEISHU__ENABLED=true
export MULTIRAG_CHANNELS__FEISHU__APP_ID='<feishu-app-id>'
export MULTIRAG_CHANNELS__FEISHU__APP_SECRET='<from-secret-manager>'
export MULTIRAG_CHANNELS__FEISHU__MULTIRAG_BASE_URL='http://127.0.0.1:8123'
export MULTIRAG_CHANNELS__FEISHU__AGENT_ID='<published-multirag-agent-id>'
export MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN='<standard-multirag-api-token>'
export MULTIRAG_CHANNELS__FEISHU__RELEASE_MARKER='leadership-demo-v1'
export MULTIRAG_CHANNELS__FEISHU__ALLOWED_OPEN_IDS='[]'

uv run python -m api.channels.worker --channel feishu
```

健康启动日志应依次出现 `ws_connected` 和 `worker_started`。`/reset` 删除当前飞书会话的
Agent session 映射；重新发布 Agent 后应更新 `RELEASE_MARKER`，避免继续复用旧 DSL 会话。

Demo worker 只完成应用级认证，不能宣称已经实现飞书用户级 SQL/MCP 数据授权。

## 模式二：Managed supervisor

Managed 模式是管理页面和生产部署使用的长期架构：

```text
管理员 /settings/channels
  -> MultiRAG 公共 Channel 管理 API
  -> PostgreSQL：channel + encrypted secret + binding + runtime status

独立 Channel supervisor
  -> 私有 desired-state API（仅 binding_id/provider/generation）
  -> 每个 binding 启动一个隔离 worker 进程
  -> worker 从私有 runtime-config API 取一次解密后的飞书凭据
  -> worker 从 execution-capabilities API 取一次脱敏能力交集
  -> 飞书事件
  -> POST /api/v1/internal/channel-bindings/{binding_id}/executions
  -> 服务端解析 tenant/target/revision/session
  -> PublishedTargetExecutionService
  -> multirag.canvas_agent 或 multirag.dialog
  -> 原有 execution SSE
  -> MultiRAGBindingExecutionClient.stream()
  -> ChannelWorker per-conversation bounded queue
  -> BindingBridge lifecycle -> transport-neutral ReplySession
     ├── 飞书：Typing -> CardKit 2.0 -> throttled patch -> final flush/finish
     └── 普通 Provider：buffered complete -> Channel.send() 一次

MCP execution 需要补充输入时
  -> API 持久化 U14 InteractionSession + U15 presentation/outbox
  -> SSE interaction_required（不进入模型）
  -> worker 结束当前流式卡，claim safe projection，整卡替换为 native form，ACK delivery
  -> 飞书 app-bound WS callback -> durable receipt -> 快速 toast ACK
  -> API 后台重新解析 Principal / claim revision / 恢复 U14
  -> worker claim terminal 或下一轮 form，更新同一张卡
```

Supervisor 只协调 desired state，不接触飞书 App Secret。每个 child worker 只在内存中获得
自己 binding 的凭据；凭据不出现在命令行、环境变量或日志中。由于 `lark-oapi` 的限制，
一个 binding/account 对应一个独立子进程。binding 被禁用、删除或 generation 改变时，
supervisor 会停止或重启相应进程；异常退出采用有上限的指数退避。

Managed worker 每次启动会为当前 binding generation 调一次 workload-authenticated 的
`execution-capabilities` preflight；进程内缓存的结果只含六个 reply 布尔值与加法的
`identity_event_receipt` 服务能力，不含 target type/id/revision 或图结构。同 generation 的 worker
崩溃重启允许重新读取；普通消息、模型 delta 和卡片 patch 不查目标数据库。旧 API 缺少新字段、
preflight 404/超时或非法响应时继续交付 buffered 回答，但不签发交互 action ID，也不订阅 Contact。

Managed worker 的唯一核心**执行**路径是 `MultiRAGBindingExecutionClient.stream()`：它统一构造请求、
读取和校验 SSE、检查 completion/`[DONE]`、传播 session、执行跨 delta reasoning 过滤，并映射安全
错误码。`BindingBridge` 直接按序把类型化 delta 写入 `ReplySession`，不再调用 `ask()`；普通
Provider 的默认 buffered session 只在成功完成时发送一次。飞书仅在协商出的
`progressive_reply=true` 时 override `begin_reply()`，把同一批 delta 渲染到一个 CardKit 2.0
streaming card；否则回退 buffered reply。卡片失败只切换交付方式，不会重新执行 Agent。
`reply_to_message_id`、Redis claim 和 executed/replied tombstone 语义保持不变。同会话串行只有
`ChannelWorker` 一个所有者；Bridge 不再叠第二把会话锁。

### Structured actor identity 的 C1/C2 transport 与 C3 consume

EIM-C1 / CHN-X5 已让 private execution API **tolerate** 可选的结构化
`ExternalIdentityAssertion`。它位于 `ChannelActor.identity`，包含 provider、可选
`provider_tenant_key` 和 1～8 个有界 `{kind, value}`；同一 assertion 的 kind 必须唯一，所有层级
继续 `extra="forbid"`。未知但有界的 kind 可保留给未来 Provider resolver，不能自动成为 canonical
subject。

当前兼容规则：

- `actor.provider/subject/conversation` 仍全部必填；`identity` 缺失时 model dump 不出现 null 字段。
- 飞书 adapter 要求事件 header `tenant_key` 与 sender `open_id`，保留所有存在的 `user_id/union_id`；
  sender tenant 有值时必须匹配 header tenant，header app 有值时只与本地配置账户核对且不进入 assertion。
- `IncomingIdentityAssertion/IncomingIdentityIdentifier` 冻结且 repr 隐藏 tenant key 与 identifier values；Bridge、
  regenerate 与 `MultiRAGBindingExecutionClient` 透传 identity。legacy subject 仍为 open ID，conversation
  仍为 chat ID；identity 缺失时旧 request bytes 不变。
- C3 execution composition 只在成功 claim 后消费 `identity`，并且只能结合服务端 binding/account link、
  I3/I4/I6 与 P1 得出 Principal；assertion 仍不能改变 Tenant/目标/版本或直接成为 Principal。
- `app_id`、Provider Account、`tenant_id`、`principal_id`、role/scopes/audience/confirmation/token 都不是
  assertion 字段，夹带会被拒绝；前两者只来自服务端 ownership/link/credential。

因此当前源码能解析新旧两种 private command，飞书 managed worker 已 emit structured identity；C3 已沿
Provider Account link、I3/I4/I6/P1 验证并 consume，C4 才会在全部 runner 浸泡后删除 legacy
`subject`。这项 private 加法不改变公开 `channel-api/v1`。C2 提交 `896c582d` 通过定向 **104 passed**、
独立兼容 **153 passed**、安全扫描 **0 findings** 与完整 `make verify`；smoke 六组件全绿。重启后的两个
飞书 worker connected，真实消息只记录 identity/tenant presence、identifier kinds/count 的安全结构，
private execution HTTP 200 并完成。resolver `principal_id` 为空，Canvas 为 `user_id=""`、
`exp_user_id=null`，10 张 EIM sidecar 零写入；这是 C2 的历史 live 边界。

C3/X7 已完成源码、自动门禁和部署 live。成功 claim 后按 authority→initial I3→I4→I6→final
I3→P1 提升 Principal；每个 linked event 逻辑上执行 I4（允许有界 cache），NO_LINK 保留 legacy
anonymous。初始 claim 与 post-claim failure/cancel tombstone 都使用完整 dedupe TTL。Redis session 使用
tenant/principal owner envelope，legacy raw 仅 NO_LINK 可续用；Dialog/Canvas existing row 再校验
`principal_id` owner。C3 当时只完成 Principal promotion 与 target/session ownership；后续
EIM-P2 / CHN-X18 已用同一 frozen `RunContext` 传播到 Agent/RAG/Memory/Canvas workflow 与 MCP
instance-local call context。C3 定向 **203 passed**、`make verify` **2403 passed**、强制 integration
**162 passed**。12:42 API 已重启到 `v0.9.9-579-g2b0482c7`，smoke 六组件全绿；真实飞书 live 覆盖
**2/2** account，四条 alias 收敛到一个 active ExternalIdentity/一个 canonical User，仅有一条 valid
NORMAL membership 与一条 BindingEvent。Canvas、Dialog 各一条本次 Principal owner 记录，空 owner
为 **0**；Redis completed/replied 存在、processing/failed 为 **0**。

### Managed Contact directory event（EIM-I7 / CHN-X21）

当前 managed composition 只有在 `execution-capabilities` 返回
`identity_event_receipt=true` 且 transport 实现 runtime-checkable `IdentityEventChannel` 时，才于
`Channel.start()` 前安装 `ChannelRuntimeClient.submit_identity_event`。Feishu 只有在这个 handler 已安装
时才向 SDK 注册 `contact.user.created_v3`、`contact.user.updated_v3`、
`contact.user.deleted_v3`、`contact.scope.updated_v3`；demo/legacy runner 不安装 handler，因此不会
订阅 Contact processor。

Feishu callback 只投影 bounded header proof、`open_id/user_id/union_id` 与五个 bool status，姓名、
邮箱、手机、工号、部门和 scope 用户列表都不进入 DTO。`event_id` 必须是 HTTP header-safe ASCII
token，并作为 `Idempotency-Key`；runtime client 用 2 秒 HTTP window，SDK callback 外层保留 2.5 秒，
只在 private API 已 durable terminal 后 ACK。callback 对外只抛固定 safe error，SDK 日志不得格式化
原 ValidationError 或 identifier。

private endpoint 只接受精确 `application/json`、最大 4 KiB、extra-forbid body；observed app/tenant
只是 event-header proof，MultiRAG tenant/account/revision 不在 body。服务端从 binding 重建 authority，
按 `Channel→Tenant→ProviderTenant/Account+Link→Binding→receipt→Alias/Identity` 锁定并复核
generation/enabled/provider。无 account link 的合法 managed Channel 保持 C3 NO_LINK compatibility：
返回 no-store 204 且零 receipt/零 mutation；只有 linked account 执行 receipt/CAS。

linked path 的 account revision 是跨 API 进程 cache fence，post-commit provider invalidate 只是当前进程
加速。stale distinct event 不重复 bump；unknown created/updated 不 JIT 但 bump negative cache；inactive
update 只收紧为 inactive，deleted 收紧为 terminal revoked，created/active update 不激活，scope 不枚举。
durable semantic conflict 写 failed receipt、保留 safe code、只 bump 一次并 ACK 204；只有未提交的
repository/timeout 才让飞书重试。

这是本地代码/契约完成边界，不表示飞书后台已订阅、API/supervisor/worker 已重启、staging/live 或
生产 rollout。I8 reconciliation、管理员观测与可配置 freshness fast path 尚未实现；C3 仍按现有 I4
逻辑重验，不能因 I7 落地就无条件跳过 Contact。

EIM-U12 / CHN-U14 允许 `message_completed` 携带可选的用户可见权威正文。
新 worker 收到时先用 `ReplySession.replace()` 整体替换内存中的 delta，再执行 `complete()`；这能表达
引用标记插入正文中间等非 append-only 终态。旧 API 缺字段时继续聚合 delta，旧 worker 也会忽略该
额外字段并继续消费原 delta。按 CHN-ADR-06，consumer/tolerate 已先部署到 generation 8 worker，随后
Dialog producer 才开始 emit；Canvas 的终态 wire 保持不变。

EIM-U13 / CHN-U15 在 API 执行层为 Canvas `candidate_cas` 增加 MultiRAG 自有 sidecar。已有会话的
候选副本与 owner metadata 在同一短事务创建；新会话仍由原有 Canvas completion 创建，但
execution-scoped `Session.info` capture 会由 `before_flush` observer 在**同一次 flush**附加 metadata，
保存终态需要恢复的发布身份，并把候选 `dialog_id` 暂时移入自身私有命名空间；普通 Canvas
list/delete-all 因而不会发现或误删活跃候选。commit/abort 只认不透明 owner token，名称/用户哨兵只
保留为旧版本兼容形状，不再作为所有权证明；新会话发布时再恢复真实 target。公开 Dialog/Canvas 表
均未增加 Channel 私有列。

候选 TTL 扫描不再出现在每条消息的 prepare 热路径。MultiRAG API 的 Channel execution router
lifespan 启动一个进程内 collector：启动即执行，之后按配置带 jitter 周期运行；每个 batch 使用新
`AsyncSession`、共享 `LIMIT` 和 `FOR UPDATE ... SKIP LOCKED` 预算，每个 cycle 也有 batch 上限。
多个 API 实例依靠候选行锁处理互不重叠的行，不需要 Redis leader。collector 同时严格兼容回收无
sidecar 的旧 Canvas 候选和 U14 前 Dialog 候选；只匹配完整哨兵且已经过期的行。TTL 仅处理 crash
orphan，不能充当运行 lease、取消或终态判断，worker 仍不导入数据库代码。

部署 U15 必须先完成数据库迁移，再重启 MultiRAG API，让新 sidecar 和 router lifespan 同时生效。
当前 API 启动顺序会先按 ORM 补建缺表，再由迁移严格校验已有 sidecar 的列/约束/索引/FK；存量环境
也可预先执行 `uv run alembic upgrade head`。它没有修改 worker runtime DTO 或 execution SSE wire，
因此不需要仅为 U15 重启 supervisor/worker；是否重启仍以同一发布中是否包含其他运行时契约变更为准。

`ask()` 仅是消费同一个 `stream()` 并聚合为 `AgentReply` 的阶段性兼容 facade，用于迁移和回滚
安全；它不是推荐接口，新代码不得增加调用。生产调用归零且 EIM-U1 稳定后应在独立任务中删除。
默认实现仍是最终单条纯文本；飞书已实现 EIM-U1/CHN-U8 渐进式 Provider session。

### MCP 原生 CardKit 表单（EIM-U15 / CHN-X15）

当前源码在默认关闭的 `identity.mcp_interactions` gate 后，把 U14 的持久 InteractionSession 接到
飞书原生 Card JSON 2.0 form。2026-08-24 已在用户批准的单机临时配置下完成真实飞书/P3/of_mcp
native-form E2E；源码默认值仍关闭，这不表示生产配置、生产 secret 或多实例 rollout 已完成。execution
遇到 `MCPInteractionPaused` 时不进入 RAGFlow/Canvas/Agent 主循环重试，而是在 API 事务边界登记
presentation，完成原 event claim，并发送终态 `interaction_required`。Bridge 先结束正在编辑的流式卡，
再用原回复 message ID 领取并投递安全 projection；form 和 terminal 都使用整卡更新，避免 stream
patch 与用户编辑竞争。

原生 mapper 是严格 allowlist，不接受任意 MCP schema/card JSON：每轮最多 4 个 request、合计 12 个
字段、每个 enum 20 个选项；只支持 text、integer/number、boolean、date、single enum 与 enum array，
文本上限 1000 字符。field/option name 都是服务端生成的不透明 ID，反向映射加密持久化；未知关键字、
nested/ref/remote schema、pattern、非法日期/数值、schema 外字段和重复多选 fail closed。卡片只带
opaque action ID、one-time nonce 和 revision，不带 `requestState`、Principal、scope、工具参数、
credential 或原始 schema。submit 与 cancel 可用；复杂联动、人员/附件、多步骤、密码/API key/token/
OAuth/支付凭据的 H5/URL mode 明确后置。

飞书 CardKit `date_picker` 的 callback 会把日期表示为 `YYYY-MM-DD ±HHMM`；服务端只在加密映射已证明
opaque 字段是 date 后校验 ASCII 形状、实际日历日与最大 `±14:00` offset，再归一为 `YYYY-MM-DD`，
普通 text 不做该转换。字段响应不合法时只拒绝当前 receipt，presentation 重新进入 pending 并投递
fresh-nonce form；密文、映射或不可解释状态损坏仍终态 fail closed。form/terminal Card JSON 2.0 均
设置 `update_multi=true`，满足原消息整卡更新要求。

worker 与 API 使用三条 generation-scoped private route：delivery `claim`、精确 lease/token `ack`、
以及 callback durable receipt。claim/ACK 只负责卡片交付，不执行 MCP。飞书 form callback 在当前
app-bound WebSocket 事件中核对 header tenant、operator tenant、配置 app account、operator open ID、
conversation/message/event lineage；这里不宣称 webhook request-signature 验证。Bridge 只把有界
typed assertion、`form_value` 和 opaque routing material 送到 callback route，并在 receipt 事务提交后
快速返回 toast；同步路径不等待 Principal 解析、数据库长事务或 MCP。

API-local callback processor 另取有 owner/attempt/expiry fence 的 lease，重新读取当前
binding/generation/enabled/provider，经 I3/I4/I6/P1 提升 verified Principal，再调用 U14 对当前 revision
执行一次 accept/decline/cancel。U14 继续拥有 interaction lease、tool gate、identity TTL、恢复前重授权、
幂等和多轮上限。字段错误重新排队安全 form；完成、拒绝、取消、过期或失败只生成白名单 terminal
摘要并更新原卡，不展示原始工具结果、MCP 参数或底层异常。renderer/worker 不访问数据库，只通过
私有 API 消费 durable outbox/receipt。

部署不是普通“新 API 先上即可”的加法窗口：先执行 migration，并在
`identity.mcp_interactions.enabled=false` 下部署/重启 API；再重启所有 supervisor/child worker，确认
它们已能消费 `interaction_required`、claim/ACK delivery 与 durable callback；最后才启用 producer 并
重启 API。回滚第一步必须关闭 producer 并重启 API，停止制造新的 interaction；保留新 consumer 处理
或终态化已持久记录后，才考虑回退 worker/API。新增表是 additive，存在数据时不得用 destructive
downgrade 换取回滚。

### 飞书渐进式回复

`FeishuProgressiveReplySession` 在执行开始时并发启动 best-effort `Typing` reaction 与 CardKit 创建，
慢 reaction 不阻塞首卡；迟到终态之后的 reaction 会立即清理。卡片使用 `schema=2.0`、
`streaming_mode=true` 并以 `interactive` 回复原消息；`streaming_config` 固定为 70ms、每步 1 字和
`fast` 策略，避免客户端因默认值或旧动画积压而出现不同节奏。正文 delta 只写入内存并唤醒后台
单写者，`append()` 不等待 CardKit 网络；单写者按 250ms 窗口合并最新全文，任何时刻最多一个 patch
在途，期间到达的多个快照只保留最新值。定时刷新不依赖下一条 delta，因此短尾不会悬空；网络 RTT
也不会反向阻塞 execution SSE。正常更新不超过 4 QPS。`complete()` 会取消尚未开始的定时刷新或
等待在途 patch，再无条件重渲染并 flush 最终正文，最后用更大的 sequence 关闭 streaming。所有
卡片更新在发请求前先消费 sequence，失败或结果不明时不会复用旧 sequence。

消息发送和卡片 OpenAPI 使用确定性 SHA-256 delivery UUID：输入只包含 provider account 的不透明
标识、飞书 event/message ID 和稳定 stage；普通日志只记录这些值的短哈希。`running_card`、
`final_fallback`、`error` 分 stage，卡片 patch/finish 还把 sequence 纳入 stage。post 到 text 的兼容
降级复用同一个 fallback UUID，避免第一次响应结果不明时换键制造重复消息。

Renderer 独立于 Bridge：CardKit Markdown 只保留 `https` 链接，模型生成的 `@all/@everyone` 不会
变成真实 mention，原始 `<at>` 标签被转义，表格降级为可读等宽块，未闭合代码块在每次 patch 时
暂态闭合。卡片正文按 UTF-8 24KB 安全预算截断并明确显示后缀；只有 post/text fallback 沿用渠道的
`max_answer_chars`。CardKit create/reply/update 失败时继续消费同一次 execution stream，并在完成时
发送一条 post；post 不可用再发 text。最终正文已成功 patch、只有 finish 失败时不再补发文本，避免
重复交付。执行失败则用安全固定文案覆盖当前卡片并 finish，覆盖失败才走 fallback。

启用这项能力前，飞书应用需要在测试租户验证 `cardkit:card:write`、消息回复和 reaction 所需权限，
修改权限后重新发布并安装应用。Reaction 始终是 best-effort；缺权限不会阻塞卡片或文本回答。
应用若订阅 `im.message.message_read_v1`，worker 会消费并忽略该回执，避免 SDK 把无业务用途的已读
事件记成 `processor not found` ERROR。

### 连续追问、取消和反馈

Managed binding 默认允许当前运行项之后再排 5 条 follow-up，可用 provider 配置的
`followup_queue_size` 调整。每条可执行的来源消息会立刻创建自己的卡片，生命周期是
`queued -> running -> final/error/cancelled`；queued 卡只显示当前位置，不承诺等待时间。会话队列或
worker 全局队列满时，Bridge 会先 claim 消息再回复固定 busy 文案，不静默丢弃。重新生成不是旁路
调用：它保留原来源消息用于 reply threading，以飞书回调 event ID 建立新的 `request_id`，然后重新
进入同一会话队列，因此不会与当前生成并发写同一会话。只有当前会话最新的完成卡可重新生成；
已有后续追问时点击旧卡会直接提示过期。完成卡发出显式 `regenerate` operation；Channel execution
反腐层从公开会话头创建目标私有 working state，撤回最新且问题匹配的 user/assistant 对，再调用
既有 MultiRAG 生成能力。Dialog 使用 detached 内存副本并在完整终态执行一次 CAS；Canvas 使用带
MultiRAG sidecar 所有权的数据库候选并原子晋升。terminal commit barrier 前的失败、取消或并发
冲突不改公开历史，
连续点击不会重复追加同一句 user。数据库 COMMIT 已发出但结果不明、或提交后卡片交付失败的窗口不
伪装成可回滚；U15 的 TTL GC 只清理仍带 metadata 的过期孤儿，不能判定这类跨存储终态。CHN-O14
durable run ledger 继续挂起，只有确认需要 `kill -9`/主机故障、跨实例取消或终态结果未知恢复后才会
另行启动；正常可控重启的卡片终态化已由 CHN-U16 落地（见下方「安全过期不是重启恢复」）。
error/cancelled 卡使用独立 retry action。普通消息失败后的 retry 仍是 `message`；若失败的是一次
`regenerate`，retry 会继承 `regenerate`，因为公开历史中的旧成功尾轮仍然存在，降成普通消息反而会
重复追加问题。完成卡的 regenerate 始终显式使用 `regenerate`。

Channel 目标的 working state 和提交后的持久化历史只使用用户可见答案，不保存或回灌 `<think>`
reasoning；terminal commit barrier 前的 reasoning-only、取消或失败不会提交半轮消息。Canvas 额外从可见 transcript 重建内部
history，修复存量 raw reasoning 污染。Canvas capability
按最新发布图动态解析：未知组件、工具/MCP、文档/Excel 持久输出、带附件输出或 Memory 保存的
Message 同时关闭 regenerate 与 retry；纯文本 Message 仍可安全重放。
`operation` 是私有 execution command 的可选加法字段：普通消息仍省略它；action 请求必须显式发送。
部署窗口里，新 API 遇到未携带 operation 的旧 worker action 会返回
`CHANNEL_RUNTIME_UPGRADE_REQUIRED`，新 worker 调旧 API 则因 `extra="forbid"` 失败；两种方向都只让
卡片操作失败，不会猜测并污染历史。部署顺序仍是 API 在前、随后立即重启 supervisor/worker。
新 API 还会在 claim action event 前按 binding RunPolicy 和目标动态能力授权 regenerate/retry；拒绝
返回 `CHANNEL_OPERATION_NOT_ALLOWED` 且不占用 event claim。worker 隐藏按钮和 opaque action 校验是
第一道 UX/进程边界，服务端授权是独立第二道边界。

当 Provider、Target 与 RunPolicy 的交集允许时，飞书完成卡片在关闭 `streaming_mode` 后使用 CardKit
batch update 添加“重新生成 / 有帮助 / 没帮助”，生成中的卡片提供“停止生成”；不允许的动作不生成、
不注册 action ID。失败或取消卡片提供独立的“重试”，不会把未成功提交的 run 伪装成完成答案的
重新生成。交互区使用 JSON 2.0 的 `column_set` 直接承载 `button`，
按钮通过 callback behavior 只回传 24 小时有效的不透明 action ID；禁止使用 JSON 2.0 已移除的
`tag: action` 旧交互模块，否则飞书会拒绝整张卡片并触发 post/text fallback。worker
内存记录把 action 绑定到当前 binding 进程、操作者、chat 和实际回复卡片 ID，并执行 one-shot claim；
重复点击不会重复取消、重新生成或反馈。worker 重启后旧卡片按钮安全过期，不会恢复执行。
`card.action.trigger` 长连接回调的同步路径只做规范化、绑定校验、幂等 claim 和入队，在飞书 3 秒
窗口内返回 toast；Redis、execution 和卡片更新均在回调确认之后异步完成。

这里的“安全过期”不是“重启恢复”。当前 queue、execution record 和 action registry 都在 worker
内存中。**CHN-U16 已完成**，为**可控正常停机**做了终态化：停止接收后立刻清空队列（因此 queued
项不会在停机窗口里被启动，executor 调用次数为 0），已经显示的 queued 卡直接改为“已停止生成”，
running 卡先关闭私有 execution SSE 再改为同一终态，**已经拿到完整终态答案、正在交付卡片的那次
run 不被取消**——目标已提交时报“已停止”是撒谎。重复 close 幂等，退出时不留后台 task。

它仍然不保存或恢复队列/action。`kill -9`、主机掉电、跨实例取消以及 terminal commit / delivery
结果未知仍属于挂起的 CHN-O14，不能用“优雅重启”验收替代。**另一条必须知道的边界**：Windows 上
supervisor 停子 worker 用的是 `TerminateProcess`（`asyncio` 在 Windows 对 `terminate()` 的实现），
子进程收不到任何信号，上面这套清理**一行都不会跑**；Linux/macOS（含 docker 部署）是真 SIGTERM，
Ctrl+C 两边都是真 SIGINT。所以在 Windows 开发机上改配置触发 generation 切换、看到悬空卡，
不是回归——判据见 [PROGRESS 待决事项 CHN-Q3](../../docs/channel-program/PROGRESS.md#待决事项)。

“停止生成”会取消当前 execution SSE 子任务或阻止 queued 项启动，并把卡片改为已停止；它只表示
停止等待后续模型/RAG 输出。由于现有 execution event 还没有可验证的 tool side-effect boundary，
卡片明确提示“外部操作不视为已撤销”，不会把网络结果未知或已经发起的副作用伪装成回滚成功。
低风险反馈会幂等更新原卡片并发出不含原文/身份明文的 `feedback_recorded` 结构化事件；产品级反馈
仓库与分析面板仍不在本阶段范围。`/new` 清空未运行 follow-up 与中途 steering 也继续等待执行引擎
具备明确语义后单独实现。

CHN-X13 已将两者拆为 `SqlAlchemyDialogTargetDriver` / `SqlAlchemyCanvasTargetDriver` 与各自的
history transaction；CHN-U14 已在 worker tolerate 部署后完成 Dialog `detached_cas` 与权威终态
快照 emit；CHN-U15 已完成 Canvas sidecar ownership 与 API bounded GC。目标架构见
[`docs/channel-program/EXECUTION_ARCHITECTURE.md`](../../docs/channel-program/EXECUTION_ARCHITECTURE.md)：
Provider 与执行目标正交；Dialog 使用内存副本 + 终态 CAS，Canvas 在 MultiRAG 提供无落库
执行端口前保留专属候选；worker 始终只走内部 API/SSE，不为卡片、delta 或候选 GC 访问数据库。

### Canvas 发布版本兼容策略

最新上游基线中的 Canvas 执行契约仍是 `release=true` 时读取“最新已发布版本”，并不
支持按历史 revision ID 直接执行。MultiRAG Channel 不修改 release/revision 选择语义，也不向
Canvas/Dialog completion 增加 Channel 私有参数。重新生成、reasoning 隔离、候选会话与成功晋升
全部由 `api/channel_execution` 的反腐层拥有：

1. 管理端只能把 `multirag.canvas_agent` 绑定到当时的最新已发布版本，并把该版本 ID 保存为
   服务端 revision guard。
2. 每次执行前，Channel 适配器重新校验该 guard 仍等于最新已发布版本。
3. 校验通过后，适配器以私有候选 session ID 调用原生 `release=true` 执行路径；只传上游已有参数，
   不会把 revision ID、`regenerate`、owner token 或其他 Channel 生命周期字段注入 Canvas；候选所有权
   只存在于独立的 MultiRAG execution sidecar。
4. Agent 发布新版本后，旧 binding 会 fail closed。管理员更新 binding 后 generation 增加，
   服务端使用新的会话命名空间，避免复用旧 DSL 会话。

这不是“按版本精确执行历史 DSL”。在上游提供相应能力前，如确需历史版本执行，
应在 MultiRAG 自有适配层增加独立执行器并保持 Canvas 核心不变，不能直接扩写上游同步文件。

### 启动前提

1. PostgreSQL、Redis 和 MultiRAG API 已启动。
2. 已执行当前 Alembic migrations：

   ```bash
   uv run alembic upgrade head
   ```

3. MultiRAG API 进程配置了持久的 Channel 主加密密钥和内部 workload token。
4. 管理员在 `/settings/channels` 创建飞书 Channel，填写 App ID/App Secret，选择
   `multirag.canvas_agent` 或 `multirag.dialog`，然后启用 binding。
5. 独立 supervisor 能通过 HTTPS 或同机回环地址访问 MultiRAG API。

### 控制面环境变量与最小权限

每台机器执行一次初始化脚本即可。它就地生成两个值、直接写入仓库外的 per-process env
文件，**不把任何密钥打印到终端或命令历史**，只回显非机密的 key 指纹（等于数据库里的
`ChannelSecret.key_id`，日后可用来确认某条密文是哪把密钥加密的）：

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts/init_channel_secrets.example.ps1
```

```bash
# macOS / Linux
sh scripts/init_channel_secrets.example.sh
```

写入位置（可用 `MULTIRAG_SECRETS_DIR` 覆盖）：Windows 是
`%LOCALAPPDATA%\MultiRAG\secrets\`（ACL 收紧到当前用户），macOS/Linux 是
`${XDG_CONFIG_HOME:-$HOME/.config}/multirag/secrets/`（目录 `0700`、文件 `0600`）。
脚本默认**拒绝覆盖已存在的 `api.env`**——它写的是单把密钥，覆盖等于把旧密钥从环上抹掉，
所有已存凭据永久无法解密。要轮换请手工把新密钥**插到列表最前面并保留旧密钥**
（见下方「主密钥是一个有序密钥环」）；`-Force` / `--force` 是覆盖，不是轮换。

生成的密钥请立即备份到密码管理器或 secret manager。仍可手工生成：

```bash
# AES-256-GCM 主密钥：URL-safe base64 编码的 32 个随机字节
uv run python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"

# API 与 supervisor 共享的内部 workload token，至少 32 个字符
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**主密钥必须经环境变量注入，不能写进 `configs/local.service_conf.yaml`。**
`common/config_utils.py::read_config` 对任何调用 `get_app_config()` 的进程都会加载该
YAML，而 supervisor fork 出的 worker 子进程正以仓库根目录为 cwd 运行——密钥一旦落在配置
文件里，worker 就能直接读到，`_spawn_worker` 里剥离环境变量的加固随之失效。同理也不要把
它设成用户级环境变量：那样 supervisor 会继承到它，`run_channel_supervisor.example.*`
的安全门禁会直接拒绝启动。

配套启动脚本会读取上面对应的 env 文件（已存在的环境变量优先，便于 CI/生产覆盖），
并在缺少密钥时**启动即报错**，而不是等到运行期才 fail closed：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_api.example.ps1
powershell -ExecutionPolicy Bypass -File scripts/run_channel_supervisor.example.ps1
```

```bash
sh scripts/run_api.example.sh
sh scripts/run_channel_supervisor.example.sh
```

进程权限应按下表拆分：

| 环境变量 | MultiRAG API | Supervisor/child worker | 说明 |
|---|---:|---:|---|
| `MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY` | 必需 | 禁止 | AES-256-GCM 主密钥**环**（见下）；必须稳定保存，仍无存量密文自动重加密 |
| `MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN` | 必需 | supervisor 必需；child 自动派生 | API 与 supervisor 共享主 workload token；supervisor 为每个 child 派生仅限 binding + generation 的 token，不把主 token 传给 child |
| `MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL` | 可选 | 必需 | API origin；远程必须 HTTPS，同机开发可用 `http://127.0.0.1:8123` |
| `MULTIRAG_CHANNELS__CONTROL__RECONCILE_INTERVAL_SECONDS` | 可选 | 可选 | desired-state 对账间隔，默认 10 秒 |
| `MULTIRAG_CHANNELS__CONTROL__RUNTIME_HEARTBEAT_SECONDS` | 可选 | 可选 | worker 状态心跳间隔，默认 15 秒 |
| `MULTIRAG_CHANNELS__CONTROL__SESSION_TTL_SECONDS` | 可选 | 可选 | 服务端 binding 会话 TTL，默认 86400 秒 |
| `MULTIRAG_CHANNELS__CONTROL__DEDUPE_TTL_SECONDS` | 可选 | 可选 | 服务端 execution 幂等窗口，默认 86400 秒 |
| `MULTIRAG_CHANNELS__EXECUTION__CANDIDATE_GC__ENABLED` | 可选 | 禁止 | API 侧 Canvas 孤儿候选回收开关，默认 `true` |
| `MULTIRAG_CHANNELS__EXECUTION__CANDIDATE_GC__MAX_AGE_SECONDS` | 可选 | 禁止 | 显式候选/legacy 回收保留期，默认 86400 秒，必须大于所有 Provider 硬执行超时 |
| `MULTIRAG_CHANNELS__EXECUTION__CANDIDATE_GC__INTERVAL_SECONDS` | 可选 | 禁止 | 启动清理后的周期，默认 3600 秒；实际等待叠加 jitter |
| `MULTIRAG_CHANNELS__EXECUTION__CANDIDATE_GC__BATCH_SIZE` | 可选 | 禁止 | 三类候选共享的单批上限，默认 100 |
| `MULTIRAG_CHANNELS__EXECUTION__CANDIDATE_GC__MAX_BATCHES_PER_CYCLE` | 可选 | 禁止 | 单周期最多批数，默认 4 |
| `MULTIRAG_CHANNELS__EXECUTION__CANDIDATE_GC__JITTER_RATIO` | 可选 | 禁止 | 多 API 实例周期错峰比例，默认 0.1 |

API 进程示例（值仅表示由 secret manager 注入）：

```text
MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY=<persistent-key-from-secret-manager>
MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN=<shared-workload-token>
```

#### 主密钥是一个有序密钥环（CHN-O7）

`SECRET_ENCRYPTION_KEY` 收的是**列表**：**第 0 把是 active**，负责加密新凭据；其余的只
用于解密它们各自写下的存量密文（每行密文都存了写它那把密钥的指纹 `ChannelSecret.key_id`，
所以这是一次查表，不是逐把试解）。**轮换 = 前插新密钥、旧密钥留在后面**，而不是原地替换。

单把密钥的标量写法完全不变，等价于长度为 1 的密钥环。列表写法：

```yaml
# configs/*.yaml —— 只是示意形状；主密钥不能写进配置文件，理由见上一节
channels:
  control:
    secret_encryption_key:
      - <new-active-key>
      - <previous-key-still-decrypting-old-rows>
```

```text
# env 只能传字符串，所以走 YAML flow sequence（引号可加可不加）
MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY=[<new-active-key>, <previous-key>]
```

留空（含 `...SECRET_ENCRYPTION_KEY=` 这种空串写法）= 未配置 = 控制面 fail closed，
不是配置非法。环上**任意一把**密钥格式不合法则整个环被拒绝——退役密钥打错字是运维
错误，必须报出来，不能被静默跳过。

Supervisor 进程示例：

```text
MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL=http://127.0.0.1:8123
MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN=<shared-workload-token>
```

不要把主加密密钥发给 supervisor。即使运维平台误把它注入 supervisor，supervisor 也会在
创建 child worker 前显式删除该环境变量；正确部署仍应从源头实行最小权限。Supervisor 使用
主 workload token 拉取 desired state，启动 child 时会把它替换成 HMAC 派生的 binding +
generation 专用 token。旧 generation 或其他 binding 的 runtime-config、状态、执行和 reset
请求都会被服务端拒绝。

### Windows 启动 Managed supervisor

```powershell
$env:MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL = "http://127.0.0.1:8123"
$env:MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN = "<from-secret-manager>"

uv run python -m api.channels.supervisor

# 或使用只校验既有环境变量、不保存 secret 的跨目录启动包装器
powershell -ExecutionPolicy Bypass -File scripts/run_channel_supervisor.example.ps1
```

### macOS 开发机启动（两个终端，不用包装脚本）

密钥建议交给系统 Keychain，而不是 `~/.zshrc`——写进 shell profile 会让 supervisor 一并
继承主加密密钥，破坏最小权限。**只存一次**：

```bash
security add-generic-password -a "$USER" -s multirag-channel-key \
  -w "$(python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))')"
security add-generic-password -a "$USER" -s multirag-internal-token \
  -w "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

终端 1 —— API（**唯一**持有加密密钥的进程）：

```bash
cd ~/path/to/MultiRAG
env MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY="$(security find-generic-password -w -s multirag-channel-key)" \
    MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN="$(security find-generic-password -w -s multirag-internal-token)" \
    uv run python -m api.multirag_server
```

终端 2 —— Supervisor（**不带**加密密钥）：

```bash
cd ~/path/to/MultiRAG
env MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL='http://127.0.0.1:8123' \
    MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN="$(security find-generic-password -w -s multirag-internal-token)" \
    uv run python -m api.channels.supervisor
```

用 `env VAR=... 命令` 前缀而不是 `export`，可以把变量限制在单条命令内，避免泄漏到同一
终端的其他进程。worker 由 supervisor 自行 fork，不需要手工启动。

### Linux 源码方式启动（不走 systemd 时）

```bash
cd /opt/multirag
# API：从 0600 的文件读取，不让密钥出现在命令行或 shell 历史
env $(grep -v '^#' /etc/multirag/api.env | xargs) uv run python -m api.multirag_server

# Supervisor：另一个终端 / 另一个 shell
env $(grep -v '^#' /etc/multirag/channel-supervisor.env | xargs) uv run python -m api.channels.supervisor
```

生产长期运行请用下一节的 systemd，而不是裸终端。

`uv run` 在所有平台执行的是同一个 Python 模块；仓库里的 PowerShell/sh 包装脚本只是本地
便利层，不是任何平台的专有启动机制。

### systemd 建议

Ubuntu/Linux 生产环境建议使用独立 systemd service，而不是把 supervisor 放进 API
service。仓库提供两套可直接安装的 unit 与对应 env 模板，**权限按进程拆开**：

| Unit | Env 文件 | 是否持有主加密密钥 |
|---|---|---:|
| `deploy/systemd/multirag-api.service` | `deploy/systemd/api.env.example` → `/etc/multirag/api.env` | **是**（唯一持有者） |
| `deploy/systemd/multirag-channel-supervisor.service` | `deploy/systemd/channel-supervisor.env.example` → `/etc/multirag/channel-supervisor.env` | 否 |

两个 env 文件都装成 `0640`（或 `0600`）且不入 Git。两个 unit 都开了
`ProtectSystem=strict`；差别在于 API 会初始化滚动文件日志，所以它额外声明
`ReadWritePaths=/opt/multirag/logs`，而 supervisor 只写 journald、不需要可写路径。
示例中的路径、用户和环境文件应按部署目录调整：

```ini
[Unit]
Description=MultiRAG Channel Supervisor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=multirag
Group=multirag
WorkingDirectory=/opt/multirag
EnvironmentFile=/etc/multirag/channel-supervisor.env
ExecStart=/opt/multirag/.venv/bin/python -m api.channels.supervisor
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict

[Install]
WantedBy=multi-user.target
```

`/etc/multirag/channel-supervisor.env` 只应包含 runtime API base URL、主 internal token 和可选
tuning，权限设为 root/服务账号可读（例如 `0600`）；不要放主加密密钥、飞书 App Secret
或 Demo Agent Token。更成熟的环境应由 Vault/KMS/云 Secret Manager 在启动时注入。

升级后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now multirag-channel-supervisor
sudo systemctl status multirag-channel-supervisor
```

### 容器和 Kubernetes 建议

- API 与 Channel supervisor 使用同一 MultiRAG 镜像、不同 command 和不同权限集合。
- Supervisor command 使用 `python -m api.channels.supervisor`；不要在容器中启动 Uvicorn
  来承载 Channel。
- 配置 init 进程和至少 30 秒 termination grace period，确保 supervisor 能终止全部 child。
- 当前一个 supervisor 会协调全部 desired bindings，因此 deployment 设为 **1 个副本**；
  滚动升级优先使用 `Recreate`。Redis leader lease 是故障保护，不应被当成多 supervisor
  主动扩容机制。
- API 容器获得主加密密钥和 internal token；supervisor 容器只获得 runtime API URL 和
  internal token。飞书凭据由 worker 经私有 API 按 binding 获取，不作为 Pod 环境变量。
- 非回环的集群内地址也必须使用 HTTPS。若必须走明文开发流量，只能把 API 放在同一
  Pod/主机并通过回环地址访问。
- 当前 supervisor 不暴露 HTTP health endpoint。进程存活用于 liveness，管理 API 中的
  `heartbeat_at`、`observed_generation` 和 `state=connected` 用于 readiness/运维判断。

#### docker compose 最小形态

> **已随仓库落地（CHN-O5）**：`docker/docker-compose.yml` 里的 `multirag-channel-supervisor`
> 服务就是下面这个形态的实现，用 `channel` profile 开关。运维步骤、要配哪些变量、
> 怎么排查，见 [docker/README.md](../../docker/README.md) 的「Channel supervisor」一节。
> 下面保留原理示意。

同一镜像、两个 service、两套 secret，**只有 API 那个挂加密密钥**：

```yaml
services:
  multirag-api:
    image: multirag:latest
    command: ["python", "-m", "api.multirag_server"]
    env_file: [/etc/multirag/api.env]          # 含 SECRET_ENCRYPTION_KEY
    ports: ["8123:8123"]
    restart: unless-stopped

  multirag-channel-supervisor:
    image: multirag:latest
    command: ["python", "-m", "api.channels.supervisor"]
    env_file: [/etc/multirag/channel-supervisor.env]   # 不含 SECRET_ENCRYPTION_KEY
    environment:
      MULTIRAG_CHANNELS__CONTROL__RUNTIME_API_BASE_URL: http://multirag-api:8123
    depends_on: [multirag-api]
    init: true                 # supervisor 会 fork worker 子进程，需要 PID 1 收割僵尸
    stop_grace_period: 40s     # 留足时间优雅停止全部 child
    deploy:
      replicas: 1              # 单副本：一个 supervisor 协调全部 binding
    restart: unless-stopped
```

```bash
docker compose up -d multirag-api multirag-channel-supervisor
docker compose logs -f multirag-channel-supervisor   # 应出现 ws_connected / worker_started
```

两点容易踩：`init: true` 不加会积累僵尸进程（worker 是 supervisor fork 出来的）；
`stop_grace_period` 太短会让 child 被硬杀，runtime 行留在 `connected` 直到心跳超时才被
判定过期。另外 API 对 Milvus 是**硬启动依赖**（探活 10 秒失败即退出），所以
`depends_on` 应配合向量库的健康检查，或依赖 `restart: unless-stopped` 自愈。

## 安全模型和当前边界

### 已实现

- 管理 API 使用当前 MultiRAG 登录 Principal，并按 tenant 校验 Channel 和目标归属。
- 飞书 App Secret 只写入，数据库中使用 AES-256-GCM 加密；AAD 绑定 tenant ID 和
  channel ID，防止密文跨行搬移后仍可解密。
- 公共 Channel API 只返回 `secret.configured` 和版本，不回显密文或明文。
- 私有 desired-state API 不返回 tenant、target、revision 或凭据。
- 私有 runtime/execution API 使用 workload token；目标、租户和发布版本只从服务端
  binding 解析，不接受 worker 或用户消息覆盖。
- Supervisor 的主 workload token 不下发给 child；child token 绑定 binding ID 和 generation，
  修改配置、轮换凭据、禁用或更新 binding 后，旧 worker 会被服务端 generation fence 拒绝。
- execution 使用事件幂等键和 owner-aware Redis 会话隔离；NO_LINK 外部用户保持 transport actor，
  LINKED 用户只能经 C3 的服务端 verified consume 提升为 MultiRAG Principal。
- private execution API 可解析 C1/C2 structured assertion，飞书 worker 已 emit；C3 resolver 只消费经
  binding/account authority 与 I3/I4/I6/P1 验证的 identity。legacy subject 与 structured IDs 都不能直接
  成为 Principal，服务端 ownership 字段不接受 payload 覆盖。
- C3 新 API 与真实飞书 live 已验证：两个 account 的 alias 收敛到同一 active canonical identity/user，
  Canvas/Dialog owner 均为当前平台 Principal 且无空 owner，Redis 只有 completed/replied 终态。证据只含
  计数和状态，不含完整外部标识、PII 或 Secret。
- Canvas 候选 owner/create/state/expiry 位于 MultiRAG 自有 sidecar；公开 Canvas/Dialog 表无 Channel
  私有列。API 侧 collector 有 batch/cycle 上限并用 `SKIP LOCKED` 协调多实例；worker 不查数据库。
- 默认关闭的 U15 源码提供 generation-scoped interaction delivery outbox、durable callback receipt、
  API 后台 Principal 重验/U14 恢复和飞书 native form/terminal 原卡更新；callback 202/飞书 toast 只在
  receipt 持久提交后返回，worker 不直接恢复 MCP。单机临时 live 已验证一次 form → receipt → resume →
  第二次 MCP → terminal ACK；默认关闭与生产 rollout 边界不变。
- Supervisor 不记录原始 binding ID，worker 不记录原始飞书 ID、问题、答案或 SSE 帧。

### 尚未实现或不能宣称

- API 与 supervisor 之间的主 internal token 目前仍是静态 workload token，不等于 mTLS 或
  短期 delegated token；child token 虽已缩小作用域，仍由该主 token 确定性派生。
- `RunContext.principal` 已经由 P2 到达 MCP call-context seam；A2/P3 的 token issuance、
  request-scoped credential provider 与动态 bearer 源码也已实现且默认关闭。2026-08-24 单机临时
  key/policy/grant + loopback TLS 已产生真实 Channel/MCP bearer 流量并完成 U15 E2E，但这不是生产
  key 管理、私网 TLS、多实例发布或业务 PDP/SQL 授权证据，不能写成生产委托已打通。
- 主加密密钥支持在线轮换（密钥环，见上），但**没有存量密文重加密流程**：旧密文要靠旧
  密钥留在环上才读得到，只有该渠道下次保存新凭据时才会改用 active 密钥重写。因此
  **仍然不得直接替换旧 key**——替换 ≠ 轮换。
- 当前支持飞书私聊文本、CardKit 渐进式回复，以及默认关闭但已完成一次单机临时 live 的 native MCP
  form 源码；生产 rollout 仍未完成，也仍不支持群聊、图片、文件、语音或 H5/URL elicitation。
- native form 本阶段不开放 U7 敏感写；密码/API key/token/OAuth/支付凭据、复杂联动、附件、人员选择
  和多步骤表单均不进入 CardKit。`lark-channel-sdk` 也未安装或迁移，仍由后续 EIM-C5/CHN-P14 PoC
  单独决定。
- 停机终态化只覆盖**有合作窗口**的停机（POSIX SIGTERM、两端的 Ctrl+C、显式 close）。
  `kill -9`、主机掉电、容器被强杀，以及 **Windows 上 supervisor 触发的 worker 停止**都没有这个
  窗口，卡片会停在最后一次显示的状态；这不是 CHN-U16 的回归，也不能靠它验收。
- CHN-O9 当前实现只有**进程内有界聚合**，没有 Prometheus/OpenTelemetry exporter、HTTP 指标端点、
  跨进程汇总或持久化时序存储；未部署前也不能把它写成生产监控已经生效。

因此，生产 binding 仍应绑定只读、最小权限的 Agent/Dialog；涉及副作用的 MCP 工具必须
自行验证授权和幂等键。正式 Principal/ToolRuntime 接入后，可替换内部执行适配器，而无需
重写飞书传输、队列、状态或 supervisor。

上述体验缺口的目标契约、任务拆分和安全边界统一见
[`docs/enterprise-identity-mcp/FEISHU_BOT_UX.md`](../../docs/enterprise-identity-mcp/FEISHU_BOT_UX.md)。
其中 EIM-U0 已使用现有 execution SSE 完成 transport-neutral 流式执行契约，EIM-U1 已基于
`lark-oapi` OpenAPI 落地且没有等待 `lark-channel-sdk` transport PoC。群聊、多模态和敏感确认仍分别
受 verified identity、资源可见性和 Confirmation/幂等依赖约束。

## 运维与故障判断

- 新增或启用 binding：下一次 reconcile 启动 worker。
- 修改 App ID、App Secret、allowlist、domain、目标或发布版本：generation 增加并重启
  对应 worker。
- 禁用或删除 binding：supervisor 优雅停止对应 worker。**在 Linux/macOS 上**这是真 SIGTERM，
  worker 会先清空队列、把已显示的 queued/running 卡改成“已停止生成”再退出（CHN-U16）；
  正常日志会出现 `channel_event=queue_abandoned` 与 `channel_event=shutdown_finalized`。
  **在 Windows 上不会**——`terminate()` 是 `TerminateProcess`，进程直接消失，旧卡会悬空。
- Child 异常退出：supervisor 记录脱敏错误并指数退避重启；新进程恢复接收后续消息，但不恢复旧进程
  的内存队列、action 或未完成 run。异常退出没有合作窗口，因此**不适用**上面那套卡片终态化。
- MultiRAG API/Redis 不可用：停止执行，不能降级到进程内无状态模式。
- API 启动后会立即执行一次 Canvas candidate GC，之后按
  `channels.execution.candidate_gc` 的 interval/jitter 周期执行；它只清理超过 TTL 的孤儿候选和严格
  匹配的旧版候选。正常运行不应依赖等待 TTL，终态应由 commit/abort 立即收口。
- internal token 轮换：协调更新 API 和 supervisor，并重启 supervisor；旧 child 派生 token
  会立即失效并由 supervisor 重建。
- 主加密密钥轮换：把新密钥插到 `SECRET_ENCRYPTION_KEY` 列表**最前面**、旧密钥留在后面，
  重启 API 即可（只有 API 持有主密钥，supervisor/worker 不受影响）。新凭据用新密钥加密，
  存量密文继续由旧密钥解密。确认没有旧密钥的密文了，才能把旧密钥从列表里摘掉——
  摘早了那些渠道会直接报 `CHANNEL_SECRET_STORE_UNAVAILABLE`。
- 主加密密钥丢失：现有飞书凭据无法恢复；必须从 secret manager 备份恢复或重新录入。

### CHN-O9 可观测边界（本地实现与全门禁完成，未 rollout）

- `ChannelTelemetry` 是 Provider-neutral、同步、无 await/无外部 I/O 的 seam；组件默认注入 no-op。
  `InMemoryChannelTelemetry` 只保存有界 series、histogram samples 和 recent events，快照用于进程内诊断与测试。
- managed `BindingBridge` composition 会把同一个进程级 recorder 显式注入 worker 与 Bridge；API 侧
  candidate GC 也显式注入进程级 recorder。legacy/demo `FeishuAgentBridge` 仍使用 no-op，因为它的兼容
  `None` outcome 不能准确区分 duplicate、policy drop、Redis/Agent/provider 失败，不能用假 `OK` 污染指标。
- 封闭指标覆盖 ingress 最终 disposition、queue depth/wait/abandoned、execution duration、首卡、首个可见正文、
  delivery/fallback、shutdown finalization，以及 candidate GC cycle/duration/deleted/batches/remaining。
  每条 accepted/rejected/dropped ingress 最终只发布一次 disposition；已经 dequeue 但等待同会话前序的 ticket、
  provider cancel failure、状态写失败和 shutdown timeout 都有确定的 closed outcome。
- label 只有 provider、operation、result、reason、stage；原始 binding/sender/chat/message/session ID 与正文不进入
  recorder。首卡和首个可见正文已有 latency；**首 ACK latency 尚未提供**，因为当前没有可靠且 Provider-neutral
  的 ingress/SDK ACK 完成钩子，不能用 enqueue 时间冒充。
- 生产外部仍只能从既有 `channel_event=queue_abandoned` 与 `channel_event=shutdown_finalized` 生命周期日志核对
  对应停机事实；其余进程内聚合尚无跨进程 exporter 或 HTTP 读取面。本轮没有新增或宣称 Prometheus endpoint。

### 一次性升级代价：Redis 命名空间 v1 → v2（CHN-S3）

Redis 命名空间从「按 provider 账号」改为「按 binding」（`multirag:channel:v1` →
`multirag:channel:v2`），修掉的是一个跨租户拒绝服务面：leader lease 在凭据校验**之前**获取，
而命名空间只由非机密的 App ID 派生，于是任何租户都能用别人的 App ID 加一个假 secret 建渠道
并启用，抢走租约，让原租户的 worker 在下一次重启后再也起不来。

**接住这次改动的那一次 worker 重启会有一次性影响**，之后恢复正常：

- 飞书用户的 Agent 会话映射重置一次——下一条消息开启新会话，不接续上文。
- 消息去重窗口空一次——重启瞬间在途的消息有可能被回答两次。
- v1 的旧 key 不再被读取，按自身 TTL（会话与去重均为 24 小时）自然过期，无需清理。

顺带修掉一个既有缺陷：删除渠道后用同一个 App ID 重建，过去会复用旧的去重命名空间，
导致重建后一段时间内老 message_id 被判重复而**静默丢消息**；binding ID 每次重建都是新的，
这条路径不复存在。

正常日志可包含：

```text
channel_supervisor_event=worker_started
channel_event=ws_connected
channel_event=worker_started
```

日志不得包含 App Secret、internal token、tenant access token、完整 WebSocket URL、原始
事件、问题、答案、完整用户/会话/message ID 或 MCP 参数。

## 近期稳定性收口顺序

upstream-first 长期原则不变，但当前不立即执行 EIM-F5 / CHN-X14。近期顺序固定为：

1. U15 数据库迁移与 API/supervisor 重启已经完成；**完整 UX 矩阵的真实飞书 smoke 仍然欠着**，Dialog、Canvas
   都要覆盖短答/长答、Markdown/公式、连续追问、queue full、queued/running cancel、retry、
   regenerate 和 feedback；Canvas 额外核对 candidate 收口及公开历史无 reasoning/半轮污染。
   U16 先行落地不改变这条；C3 已完成的各一条 Principal-owner live 也不替代这组完整 UX smoke。
2. ✅ CHN-U16 / CHN-U17 已完成（2026-08-13）：可控正常停机不再遗留 queued/running 卡，跨层测试从 worker
   队列穿过私有 HTTP/SSE 直到 ReplySession 的卡片调用；queued cancel 不启动执行、queue full
   确实向用户交付 busy 都已钉住。Python 3.12 下 execution 与 consumer 同轮取消时，Bridge 在完成
   回复终态化后继续传播 consumer 自身的取消，不会回到下一轮队列等待并卡死 worker close。
   目标侧（Dialog/Canvas driver）那一段仍由
   `tests/integration/test_channel_history_manager.py` 覆盖，单元层不接真库。
3. ✅ CHN-O9 本地实现与全门禁已补最小进程内可观测：首卡/首正文、queue wait/depth/overflow、
   CardKit update/fallback、terminal、shutdown outcome 与 candidate GC；U16 的
   `channel_event=shutdown_finalized`（含 `queued=`/`running=`）和 `channel_event=queue_abandoned`
   已收编。源码尚未 rollout；首 ACK 和跨进程 exporter 不在本轮完成面。
4. 稳定浸泡期间不新增执行架构；记录失败率、悬空卡、重复执行/交付、候选孤儿和重启结果。
5. Channel 稳定后，等待用户恢复从约 2026-04-24 本地同步点逐 commit 跟进 RAGFlow，再解除
   EIM-F5 / CHN-X14 挂起并把本轮改动随上游迭代一并审计。

CHN-U16、CHN-U17、CHN-O9 是 Channel 账本任务，不新增 EIM 映射。正常停机终态化不等于 durable recovery；
没有 `kill -9`/跨实例/终态不确定性等明确恢复需求时，CHN-O14 继续挂起。
EIM-P2 / CHN-X18 已完成实现、自动门禁与本机 API rollout；Canvas/Dialog 真实飞书各一条均
completed，Redis owner envelope 与数据库 owner 非空，smoke 六组件全绿。该证据不替代完整 UX
矩阵，也未触发真实 Memory 或 MCP 工具调用。后续 A2/P3 仍是独立任务；CHN-O9 可并行。C4/X8
不抢跑，必须等待全部 runner 升级以及由可观测证据支撑的 deployment soak。

## 与上游同步策略

MultiRAG 会持续跟进 RAGFlow 的 Canvas、Agent 和 Channel 源码。默认选择是采用或语义移植上游
原生架构，而不是在 Channel 层建立一套长期平行 runtime。DeerFlow、LangGraph、Open WebUI 等项目
只用于校准 terminal publish、CAS、run/history 分离、幂等和副作用门禁；这些不变量由
MultiRAG 目标适配层承接，不反向污染上游同步区。完整决策见
[`CHN-ADR-08`](../../docs/channel-program/DECISIONS.md#chn-adr-08--canvas-与-channel-演进以上游同步为主只在适配层吸收现代执行不变量)。

Canvas 当前的 candidate + sidecar 是对自持久化 completion 的兼容桥。如果上游继续保持该形态，
它可以继续存在；如果上游提供稳定 no-store/snapshot 缝，先做语义等价审计再评估 detached CAS；
如果上游提供原生 checkpoint/runtime，则优先适配上游模型。`CanvasExecutionPort` 只是条件性执行缝
的称呼，不代表已经决定自研接口或删除 candidate。

EIM-F5 / CHN-X14 当前为挂起状态；本文保留的是恢复同步时的审计方法，不是立即拉齐最新 HEAD 的
指令。恢复时从 MultiRAG 当前约 2026-04-24 的本地同步点确定准确 commit，按顺序逐个跟进，不跳过
中间提交做一次性大合并。

### Feishu / Lark 域名兼容

MultiRAG 的规范公开字段是 `config.domain`。为兼容新版上游的请求结构，控制面在根字段
缺失时也接受 `config.credential.domain`；两处同时存在时以根字段为准。兼容值会被提升到公开
配置，不会随 App Secret 一起进入加密凭据存储。这样国际版的 `lark` 不会因为字段位置差异，
在请求正常保存后被 Pydantic 默认值静默替换成国区 `feishu`。

| 路径 | 所有权 | 跟进方式 |
|---|---|---|
| `core/registry.py` | 上游形态、低差异 | 优先对比并语义移植注册机制变化 |
| `core/base.py` | 兼容的本地 contract fork | 保留兼容构造器和 MultiRAG 安全日志，再语义移植 |
| `feishu/channel.py` | 加固后的上游派生 | 保留 readiness、shutdown、SDK seam、raw-event 清理和日志脱敏 |
| `agent_bridge.py`、`binding_bridge.py` | MultiRAG | 不用上游文件覆盖 |
| `state_store.py`、`runtime_client.py` | MultiRAG | 不用上游文件覆盖 |
| `worker.py`、`supervisor.py` | MultiRAG | 不用上游 Bootstrap/进程模型覆盖 |
| `api/channel_control`、`api/channel_execution`、`api/channel_runtime` | MultiRAG | 作为本项目长期主线维护 |
| `api/db/services/canvas_service.py`、`conversation_service.py`、`dialog_service.py`、`user_canvas_version.py` | MultiRAG 上游同步区 | Channel 不加参数、不改历史/发布语义；只从独立适配器调用公开契约 |

跟进新版上游时：

1. 区分三类版本：项目建立时的历史来源基线不覆盖；完成语义核验后才刷新滚动兼容审计基线；
   每个实际跟进的上游 commit 单独进入移植记录。定义见
   [`VERSION_BASELINE`](../../docs/enterprise-identity-mcp/VERSION_BASELINE.md)。
2. 固定开工时 RAGFlow HEAD，逐 commit 对比 `api/channels/core/{base,registry}.py`、
   `api/channels/feishu/channel.py` 的传输层变化，同时核对 Canvas/Agent 发布、completion、
   no-store、checkpoint、取消和副作用契约。
3. 每项差异明确归入“直接跟进 / 语义移植 / 适配层吸收 / 暂不采纳”，按上表处理；不整文件覆盖
   加固版本，也不复制完整 Canvas/Agent 编排。
4. 每项本地差异记录上游 SHA/路径、本地落点、保护的不变量、契约测试和删除条件；上游出现等价
   或更好的原生能力时，优先删除兼容债，不长期并行维护两套 runtime。
5. 把上游产品名、模型、路由、表名和运行时值翻写为 MultiRAG 自己的实现；本仓表、函数、方法、
   路由和运行时不得称为“RAGFlow 的”。
6. 禁止引入任何指向上游运行服务的 HTTP、RPC、数据库或消息队列依赖。
7. 运行 Channel 契约测试和全仓验证，通过后再更新滚动兼容审计 SHA。

## 验证

Channel 相关快速验证：

```bash
uv run pytest tests/unit/test_channel_config.py tests/unit/test_channel_secret_crypto.py tests/unit/test_channel_secret_store.py
uv run pytest tests/unit/test_chat_channel_control.py tests/unit/test_channel_execution.py tests/unit/test_channel_execution_api.py
uv run pytest tests/unit/test_channel_execution_adapters.py tests/unit/test_channel_runtime_api.py tests/unit/test_channel_runtime_client.py
uv run pytest tests/unit/test_channel_candidate_gc.py tests/unit/test_channel_config.py
uv run pytest tests/unit/test_channel_telemetry.py tests/unit/test_channel_graceful_shutdown.py
uv run pytest tests/integration/test_channel_history_manager.py tests/integration/test_channel_candidate_gc.py
uv run pytest tests/unit/test_feishu_agent_bridge.py tests/unit/test_binding_bridge.py tests/unit/test_reply_session.py tests/unit/test_feishu_channel.py
uv run pytest tests/unit/test_feishu_state_store.py tests/unit/test_feishu_worker.py tests/unit/test_channel_supervisor.py
uv run ruff check api/channels api/channel_control api/channel_execution api/channel_runtime
```

提交前仍必须执行仓库总门禁：

```bash
make verify
```
