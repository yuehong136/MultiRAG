# 企业身份接入与 MCP 授权项目 · 权威入口

> 项目代号：**EIM**（Enterprise Identity & MCP Authorization）
> 建立日期：2026-08-07
> 适用仓库：**MultiRAG**（本仓）与 **`of_mcp`**（另一个独立 checkout）。
> 两者的本地路径随机器而变（本仓同时被 Windows 与 macOS 开发机使用），本目录一律按仓名指代；
> 需要绝对路径时以你当前机器上的实际 checkout 为准。
> 外部事实最近核验：2026-08-12（版本与上游提交见 [VERSION_BASELINE](VERSION_BASELINE.md)）

本目录是后续实现“企业级飞书身份接入、MultiRAG 平台用户、MCP 身份委托、of_mcp
授权和敏感操作确认”的**项目级单一事实来源**。后续 Agent 可以没有任何历史对话，但必须从
本文件进入，并按 [ROADMAP](ROADMAP.md) 中的任务 ID 开工。

仓库自己的 `AGENTS.md` 仍然是编码和验证规范的最高优先级。本目录负责回答“做什么、为什么、
先后顺序和跨仓契约”；`AGENTS.md` 负责回答“在该仓库怎样改才算完成”。涉及 Channel 路径时，
还必须同时遵守 [`docs/channel-program/README.md`](../channel-program/README.md) 和其
`CHN-*` 记账规则。

---

## 1. 五分钟上手

1. 读本文件，先建立术语和系统边界。
2. 读 [DECISIONS](DECISIONS.md)，不要重新发明已经定案的架构。
3. 在 [ROADMAP](ROADMAP.md) 找到用户指定的 `EIM-*` 任务；没有指定时，不要自行并行开多条，
   先根据依赖图选择第一个未阻塞任务。
4. 按任务的“开工前必读”和“验收证据”执行。
5. 改 MultiRAG 的 Channel 子系统时，同一提交还必须带任务表指定的 `CHN-*` ID，并更新
   Channel 进度账本。
6. 改 of_mcp 时先完整读取**那个仓自己的** `AGENTS.md`（在它的 checkout 根目录），最终必须执行
   `uv run ofmcp verify`；改 MultiRAG 时最终必须执行 `make verify`，涉及身份表、迁移或
   token 持久化时另跑 `make integration`。

推荐的派工方式：

```text
读 docs/enterprise-identity-mcp/README.md，然后完成 ROADMAP 中当前主线任务
（EIM-P2 / CHN-X18 已完成；下一条接线主线需按 ROADMAP 重新选择）。
先复核任务锚点和依赖，把准备修改的文件与验收标准告诉我；确认后再写代码。
```

一次只派一个任务。`EIM-*` 是跨项目工作包 ID；某些任务同时映射一个 `CHN-*` ID，这是
Channel 仓内的强制记账，不是重复任务。

---

## 2. 最终结论

完整链路固定为：

```text
(CustomerOrganization 1:1 Tenant；首期仅目标术语，不落表)
  -> MultiRAG tenant_id
(provider, provider_tenant_key)
  -> exactly one MultiRAG tenant_id（首期机器强制）
  -> independent Provider Account / app installation 固定同一 tenant_id
  -> tenant/provider-safe IdentityProviderChannelLink
  -> Channel binding 固定同一 tenant_id
  -> open_id alias
  -> 飞书 tenant-scoped user_id
  -> MultiRAG platform_user_id
  -> 可选的 enterprise_subject（employee_no / talent_id / workcode）
  -> audience-bound MCP access token
  -> of_mcp Principal + scope + 业务侧授权
```

`CustomerOrganization` 表示真实客户企业/合同与治理边界；首期与 Tenant 1:1，只是目标架构术语，
不新增表、claim、API 或运行时路由。MultiRAG 平台仍是多租户系统，但首期单个飞书企业接入强制为
单 Tenant。同一企业可以安装多个应用，这些 Provider Account 必须归属同一个 Tenant；同一个安装实例
或 Channel binding 在任何阶段都不能
动态路由多个 Tenant。未来集团级多 Tenant 需要每个目标 Tenant 使用不同应用安装实例/binding，且
必须通过新的 ADR、schema 迁移和上线评审显式开放；当前 schema 不预埋绕过路径。

首期第三方 Channel 固定为**平台托管 adapter**：Provider 事件先在受管 worker/adapter 按 transport
验证 webhook 签名/加密或受认证的长连接，并校验应用、租户、时间和重放，再把结构化外部标识交给
MultiRAG。该结构仍只是
`ExternalIdentityAssertion`，不是 Principal、OAuth Identity Assertion 或 MCP access token；
MultiRAG 必须结合服务端 binding/provider account 和目录验证才能构造 Principal。外部托管 Connector、
EMA/ID-JAG 与多个真实 issuer 继续由 EIM-A8 守门。

三类信任工件严格分离：

```text
Provider ExternalIdentityAssertion
  -> MultiRAG verified Principal
  -> resource-bound mcp_access
  -> of_mcp Gateway verified Principal
  -> 必要时换发 service-bound mcp_internal_actor
```

Channel payload 不得声明 `principal_id`、`tenant_id`、role、scope、audience 或确认状态；外部 token
也不得原样穿过 gateway 到 proxy service。完整决策见 [EIM-ADR-20](DECISIONS.md#eim-adr-20channel-assertionmcp-access-token-与-internal-actor-token-是三类独立信任工件)。

不可混用的四类身份：

| 标识 | 含义 | 谁生成 | 是否可直接做 MCP 业务身份 |
|---|---|---|---|
| `open_id` | 某个飞书应用内的用户 ID | 飞书 | 否；换 App 会变 |
| `provider_user_id` | 飞书租户内稳定的 `user_id` | 飞书 | 否；先映射平台用户 |
| `platform_user_id` | MultiRAG 用户主键，即当前 `User.id` | MultiRAG | 可做审计主体，不等于业务工号 |
| `enterprise_subject` | 企业业务系统认可的 `employee_no/talent_id/workcode` | 飞书通讯录或 OA/HR | 是，但仍需业务授权 |

最重要的边界：

- 飞书是交互入口和企业身份来源，不是公司全部业务权限的事实来源。
- MultiRAG 持有平台用户、租户成员关系、外部身份链接和执行上下文，不保存“能否查工资”一类
  业务权限副本。
- of_mcp 是能力开放和资源服务器层，验证委托 token、scope、audience、业务策略并审计；
  不接入飞书 SDK，不同步组织树，不信任模型或工具参数自报的工号。
- OA/HR 仅在飞书无法提供企业业务主键，或业务系统需要实时授权时参与；它通过可插拔接口接入，
  不能侵入 Channel 或 MCP 通用内核。

---

## 3. 推荐的部署边界

第一阶段不新建独立服务：

```text
MultiRAG
  - Feishu Channel transport
  - EnterpriseIdentityService
  - FeishuEnterpriseIdentityProvider
  - platform_user / external_identity / enterprise_subject_link
  - Principal construction
  - MCP token issuer（模块化部署，逻辑上是 Authorization Server）

of_mcp
  - OAuth/MCP protected-resource metadata
  - bearer token verifier
  - immutable Principal / per-tool policy middleware
  - auth audit / OTel API / replay protection（EIM-A6 phase 1 已落；生产持久后端未落）
  - business service adapters
```

MultiRAG 同时还有第二个、方向相反的 MCP 角色：

```text
outbound：MultiRAG Host/Client -> of_mcp Resource Server
inbound： 外部 MCP Client -> MultiRAG RAG Resource Server -> MultiRAG API
```

这两个方向的 resource URI、token audience、Principal、工具策略和发布/回滚面必须独立；当前又共享
Python 依赖和一个 lock。EIM-F6/F7 已基于兼容证据选择整仓协调切换，当前精确锁定
`fastmcp==4.0.0b2` 与 `mcp==2.0.0`，未拆出独立 Server runtime。这是技术基线，不会合并
两个方向的 resource、credential 或授权策略。完整术语、当前事实、依赖拓扑、能力取舍、
MRTR/飞书交互和分阶段门禁见
[Modern MCP 与企业服务中心](MCP_ENTERPRISE_PLATFORM.md)。

当出现下面任一条件时，再把 MultiRAG 内的身份模块抽成独立 `identity-broker`：

- 两个以上 AI 平台都需要复用同一企业身份；
- 飞书、钉钉、企业微信、OIDC 等三个以上 Provider 同时运行；
- 企业 IdP/SSO 团队需要独立部署和密钥治理；
- token 签发、撤销和审计需要独立扩缩容或合规边界。

提前拆服务只会增加分布式事务、密钥、部署和联调成本；接口从第一天独立，部署可以后移。

---

## 4. 同步策略定案

不做“每天把全公司组织架构完整复制到 MultiRAG”。目标稳态使用：

```text
JIT 解析 + 通讯录事件失效 + 已链接活跃用户的周期兜底校验
```

- 首次消息、本地映射缺失、缓存过期或高风险操作前才调用飞书通讯录。
- EIM-I7 落地后订阅 `contact.user.created_v3`、`contact.user.updated_v3`、
  `contact.user.deleted_v3`、`contact.scope.updated_v3`。
- 离职、冻结、主动退出、不可见或数据权限被收窄时 fail closed。
- 周期任务只校验已链接且近期活跃的身份，不抓取全量组织树。
- I7/I8 与可配置 freshness policy 落地后，正常 RAG 对话命中仍新鲜的本地映射可不发外部
  Contact/OA 请求。

**当前实现边界（2026-08-13）**：I7/I8 尚未实现，C3 对每个 LINKED event 都逻辑调用 I4；同一
account generation、subject 与 scope 的请求可以命中 I4 的有界正缓存，因此不等于每条消息都发一次
Contact 网络请求，cache hit 也不会把 proof 时间伪装成当前请求时间。不要把上面的目标稳态写成
当前已订阅 Contact 事件或已有 24 小时本地快速路径。

企业策略支持三种 provisioning 模式：

| 模式 | 行为 | 推荐场景 |
|---|---|---|
| `preprovisioned` | 只有管理员预建的 MultiRAG 用户可绑定 | 高合规、禁止自动开户 |
| `link_only` | 用户先登录 Web，再用一次性码绑定飞书 | 通用 SaaS 默认、可避免重复账号 |
| `jit` | 飞书目录验证通过后自动创建 `User` + `UserTenant(NORMAL)` | 单公司部署、全员可用；原力推荐 |

任何模式都不得把首次用户创建成 `OWNER` 或 `ADMIN`。

I3 仍只把权威 policy snapshot 的三种 mode 映射为携 revision 的
`bind_preprovisioned/require_link/create_normal_member` verification-gated plan；它本身不写库。I6 已
完成消费 fresh I4 proof、重新锁定 policy/account generation 并原子执行表中绑定/开户动作的 domain、
schema 与 PostgreSQL transaction。I6.1 已提供从既有 Channel 加密凭据出发、默认 dry-run 且显式
apply 的受控企业连接 CLI。C3 消息侧 verified consume 已完成源码、自动门禁和真实飞书 live；
公开 HTTP/UI 与 P2 Principal 全链传播仍未接线。

---

## 5. 文档索引与单一职责

| 文档 | 负责回答 | 不负责回答 |
|---|---|---|
| [DECISIONS](DECISIONS.md) | 已定案的架构选择、禁止项和替代方案 | 任务状态 |
| [ARCHITECTURE](ARCHITECTURE.md) | 端到端组件、时序、失败语义、部署边界 | 精确字段契约 |
| [Modern MCP 与企业服务中心](MCP_ENTERPRISE_PLATFORM.md) | modern protocol、双 MCP 角色、企业服务中心、EMA/MultiAuth/MRTR 和开发门禁 | 精确字段与任务状态 |
| [MCP 兼容矩阵](MCP_COMPATIBILITY.md) | EIM-F2 可执行 fixture、实际协商结果、已知缺口和 F3/F8 迁移清单 | 长期目标架构与业务权限模型 |
| [CONTRACTS](CONTRACTS.md) | DTO、数据库约束、Principal、JWT、错误码和 API 契约 | 飞书后台操作步骤 |
| [FEISHU_ONBOARDING](FEISHU_ONBOARDING.md) | 去哪里申请 App、拿什么凭据、开什么权限和事件 | MCP 内部授权实现 |
| [FEISHU_BOT_UX](FEISHU_BOT_UX.md) | 飞书流式卡片、ReplySession、话题、队列、多模态和体验验收 | 身份/JWT 的最终字段 |
| [Channel 执行架构](../channel-program/EXECUTION_ARCHITECTURE.md) | Provider × Dialog/Canvas 正交边界、历史事务、能力与 I/O 预算 | 飞书卡片细节 |
| [REFERENCES](REFERENCES.md) | 每个开源/官方项目参考什么、不参考什么、当前提交锚点 | 我们自己的最终架构 |
| [VERSION_BASELINE](VERSION_BASELINE.md) | 已核验版本、目标版本、升级闸门和重新核验命令 | 功能排期 |
| [ROADMAP](ROADMAP.md) | `EIM-*` 任务、依赖、仓库、锚点、完成证据和进度 | 背景论证 |
| [TESTING_SECURITY](TESTING_SECURITY.md) | 威胁模型、测试矩阵、上线与运维门禁 | 任务分配 |
| [AGENT_RUNBOOK](AGENT_RUNBOOK.md) | 零上下文 Agent 如何开工、交接、记账和停止 | 具体业务实现细节 |

出现重复陈述冲突时，以表中“负责回答”的文档为准。外部版本事实以
[VERSION_BASELINE](VERSION_BASELINE.md) 为准；安全不变量以
[TESTING_SECURITY](TESTING_SECURITY.md) 为准。

---

## 6. 当前代码事实与已知缺口

### MultiRAG

- EIM-F1 已把官方 `lark-oapi` 下界和 lock 从 1.7.1 升到 **1.7.2**，并新增无真实 PII/Secret 的
  Contact V3 fixture：`user_id_type=open_id` request 与
  `GetUserResponse/GetUserResponseBody/User/UserStatus` typed response 分别由官方 SDK 构建/解析。平台
  `channel_control/channel_providers/channels.verification/identity` 模块导入不会加载 SDK 或安装 loop；
  显式顶层导入 SDK 的已知行为是安装一个 idle、无 task、无新增 thread 的模块级 loop，不能误写成
  “完全无 import-time 副作用”。1.7.2 `TokenManager` 有 SDK cache，但 cache miss 没有 single-flight；
  按 Provider Account scope 的并发刷新与 live Auth response 兼容已由 I4/I4.1 补齐。
  F1 证据为 contract **7 passed**、广义 Feishu/
  Channel **101 passed**，`make verify` unit **2012 passed in 30.95s**；它只解锁 I4，不代表目录身份链
  已实现。
- EIM-I4.1 已完成：`api.identity.providers` 提供框架无关 Provider SPI/DTO、有界
  cache/single-flight/限流与 `FeishuEnterpriseIdentityProvider`；按需加载的 1.7.2 adapter 通过官方
  Auth V3 generated async request/resource/transport + strict live top-level adapter，再接 Tenant V2/
  Contact V3 generated typed nested response，并显式传递 project-scoped
  `tenant_access_token`，避开 SDK 同步且只按 `app_id` 分区的 global TokenManager cold path。
  正向/负向 identity cache 为 300/30 秒，token 在上游 expiry 前 600 秒失效，Contact
  每 account 15 calls/s 且最多排队 2 秒；所有 cache key 包含 account revision/scope marker、
  `ChannelSecret.version` 和 Feishu/Lark domain。
- 1.7.2 生成的 Auth response model 期待 `data`，但有效 live Auth V3 成功响应将
  `tenant_access_token/expire` 放在顶层；I4.1 保留官方 SDK transport/签名并严格兼容该 live shape。
- Auth/Tenant/Contact 三个官方 endpoint 都同时保留 HTTP status 与业务 code；非 2xx 即使业务
  code 为 0 也不会丢失 transport failure。错误码按 endpoint stage 解释，不能全局套用：`10003`
  不是 credential code，Auth credential mismatch 当前为 `10015/20002`；Auth/Tenant 控制面失败
  不会被解释为目录用户 `NOT_FOUND/NOT_IN_SCOPE`。Contact 只有 code 0 才允许 HTTP 403/404
  fallback；未知/瞬时非零 code 不能降级成 link/JIT 可消费的 identity miss。
- I4 的临时 credential adapter 独立放在 `api.identity_adapters`，从精确 Provider Account 沿
  唯一 tenant/provider-safe link 取 Channel 公开配置与加密 `ChannelSecret`；任一歧义、
  scope 漂移、明文污染或解密失败均 fail closed，不按 `app_id` 猜测。这仍是
  Channel-owned credential 的过渡形态，不是通用 Provider credential vault。
- 先前 sandbox 将 Python 源码与 credential 共用 stdin，脚本实际读到空参数，相关记录全部作废。
  新的有效直连 sandbox 已得到 Auth V3、
  Tenant V2、Contact V3 三步 HTTP 200/code 0，tenant 匹配且用户 active；没有保存 credential、
  token、真实 tenant/user 标识或个人字段。修正后的 production adapter sandbox 同样三步
  HTTP 200/code 0，并确认 token present/expiry valid、tenant present且匹配、user present、asserted
  open_id 匹配、stable user id present、activated true 且 frozen/resigned/exited/unjoin 全 false；
  没有原始标识、PII、Secret 或 token 落盘。
- I4.1 完成证据：广义 I4+F1 **118 passed in 5.29s**，Channel credential 真 PostgreSQL
  **1 passed in 0.76s**；`make verify` unit **2123 passed in 34.53s**，Ruff format 1234 files、
  Ruff check、7 import contracts、async gate、mypy 78 files 全绿；强制 integration
  **84 passed in 12.52s**。I4 不写 identity 数据，也不实现 I5/I6/I7/C3/Principal 传播。
- EIM-I6 已落下权威 `IdentityTenantPolicy(mode/revision/link TTL)`、digest-only
  `IdentityLinkCode`、append-only 首次 `IdentityBindingEvent`，并加固 active membership partial
  unique 与全状态 reverse identity unique。policy 缺失不使用默认值；I3 plan 携 revision，I6 在锁后
  重新核对 generation。
- I6 link code 是 exact 24-byte/192-bit CSPRNG，raw value 只返回一次；数据库只保存 domain-separated
  HMAC-SHA256 digest + key id、target、policy/account generation 与生命周期。TTL 由权威 policy 限定
  60～900 秒，新码撤销同 account + target 的旧 pending 码；所有可探测的 invalid/conflict 分支统一
  要求重新 link。
- I6 只接受 I3 plan + 不超过 5 分钟且不晚于 post-lock PostgreSQL `clock_timestamp()` 的 I4 proof。
  `preprovisioned` 不开户，`link_only` target 只来自 authenticated Principal 创建的 grant，`jit` 只
  创建 external-only User + active `UserTenant(NORMAL)`。User/UserTenant/identity/alias/code/event 同
  事务提交或回滚；所有阻塞锁后重新校验 proof/code expiry，锁等待不会延长有效窗口。
- “显式 link”只把一个 verified ExternalIdentity 绑定到一个现有 User，并可能 local→hybrid；不是
  两个 User 及其数据的 merge。name/email/mobile/employee_no 都不用于匹配，I6 也不写 I5
  EnterpriseSubject。inactive/conflict/revoked 不自动恢复。
- I6 当前只交付 framework-neutral domain、migration/schema 与 async PostgreSQL repository/use-case；
  不提供公开 HTTP/UI、消息侧 C3、Principal 传播、I7 或 FastMCP/of_mcp 运行时接线。完成证据为 domain/
  model unit **143 passed in 5.49s**、四个 identity 真 PostgreSQL 面 **84 passed in 7.80s**；完整
  `make verify` 全绿并收集/执行 unit **2188 passed**，强制 integration **128 passed in 16.40s**。
- EIM-I6.1 / CHN-X17 已完成受控企业连接 onboarding。CLI 只接受现有 Channel 引用，不接受命令行
  明文 app/tenant/Secret；默认 dry-run 在数据库事务外经官方 Auth V3 + Tenant V2 验证 credential 与
  企业 ownership，再生成有时效、进程内一次性且不可篡改的脱敏计划。显式 apply 在 fresh transaction
  中锁定并复核 Channel/Secret generation 与 tenant/provider ownership，幂等创建或复用 Provider
  Tenant/Account、Tenant policy 与 link，拒绝 rebind、policy drift 和计划重放。
- 两个真实 Channel 在飞书侧补齐并重新发布 Tenant V2 企业信息只读权限后，Auth/Tenant dry-run 均
  通过且属于同一外部企业；最终 Provider Tenant/Account/Policy/Link 为 **1/2/1/2**，两 account
  healthy/revision 1，policy 为 `jit`、TTL 300、revision 1。两个 Channel 各重放一次均零 action；六张
  用户身份 sidecar 为零，`User/UserTenant` 不变。Identity HMAC 使用独立至少 32-byte keyring，真实
  key 仅注入 API 的 mode `0600` secrets env，不进仓库、supervisor/worker 参数或日志。
- I6.1 完成门禁：`make verify` **2316 passed in 36.68s**，强制 integration
  **159 passed in 17.36s**，smoke 六组件全绿，安全终审 **NO BLOCKER**。它没有新增公开管理 UI/API，
  也没有让 execution consume assertion；该边界已由后续 C3/X7 源码接续。
- [`api/channels/README.md`](../../api/channels/README.md) 已明确：`IncomingMessage.sender_id`
  是不可信外部标识，不能直接作为 Principal。这条边界必须保留。
- EIM-C2 已让 `api/channels/feishu/channel.py::_normalize` 要求 header tenant 与 sender open ID，并
  保留所有存在的 user/union ID；sender tenant/app 分别与 header tenant/本地配置账户核对。冻结、脱敏
  的 transport assertion 经 Bridge/regenerate/runtime client 发往 private API，legacy subject 仍为 open ID。
  C2 本身只完成 transport emit；C3 现通过服务端 authority/link 与 IdentityService consume。
- EIM-C3 / CHN-X7 已完成并部署 live：成功 claim 后按
  authority→initial I3→I4→I6→final I3→P1 提升 `TrustedChannelContext.principal`/`principal_id`，linked
  event 逻辑上每次执行 I4（可命中有界 cache）；NO_LINK 保留 legacy anonymous，LINKED 缺失/损坏
  assertion 或 authority/proof 漂移均 fail closed。Redis 会话按 tenant/principal owner envelope 隔离，
  legacy raw 仅 NO_LINK 可续用，Dialog/Canvas 再校验 `principal_id` owner。它尚未把 Principal 传播到
  Agent/RAG/Memory/Workflow/MCP，那是 P2。
- C3 自动证据为定向 **203 passed**、`make verify` **2403 passed**、强制 integration **162 passed**。
  12:42 API 已重启到 `v0.9.9-579-g2b0482c7`，`make smoke` 六组件全绿。真实飞书 live 覆盖
  **2/2** Provider Account，四条 alias 收敛到一个 active ExternalIdentity 和一个 canonical User；仅有
  一条 valid NORMAL membership 与一条 first-binding event。Canvas、Dialog 各有一条本次 Principal
  owner 记录且空 owner 为 **0**；Redis 仅保留对应 completed/replied 状态，processing/failed 为 **0**。
  证据只记录计数和状态，不包含完整 app/tenant/account/user 标识或任何 Secret。
- EIM-P1 已将 canonical Principal 的唯一 owner 固定为 `api.identity.principal`；
  `api.utils.api_utils.Principal` 只 re-export 同一 class，待存量 route import 迁移后删除。Principal
  direct constructor 已封闭，不含 email/role/groups/scopes/token，`.id/.nickname` 只是兼容 property。
- EIM-I1 已使 `User.email` 可空，并引入 `local/external/hybrid` 账户类型；external-only 账户不能使用
  本地密码登录或找回密码。它只解决账户承载形状，尚未建立 Provider identity/binding 或 Channel
  Principal。
- EIM-I2 已完成六张身份持久化表：provider tenant ownership、provider account ownership、canonical
  identity、tenant-scoped alias、enterprise subject 与 body-free event receipt。首期
  `(provider, provider_tenant_key)` 机器固定一个 Tenant；canonical identity、alias/receipt 再由唯一
  约束和复合 `ON DELETE RESTRICT` 外键继承同一 scope。I2 当时的 account/channel 直接关系已由
  I2.1 forward migration 解耦。I2 Alembic head 为
  `8f2c4d6e7a9b`；identity 真 PostgreSQL integration 为 **11 passed**，完整 `make verify` 为
  **1925 passed**，完整 integration 为 **52 passed**。
  这仍只证明持久化边界，不代表 Channel 已有 Principal。
- EIM-I2.1 已用第七张 `IdentityProviderChannelLink` 表把 Provider Account 从 Channel 所有权中
  解耦：account 可以独立存在，Channel 通过 tenant/provider-safe 双复合外键和首期双唯一引用。
  Alembic 单 head 为 `9a3b5c7d8e0f`；I2.1 identity integration **23 passed**，相关 I2.1 unit
  **14 passed**，完整 `make verify` unit **1929 passed**，完整 integration **65 passed**。飞书 Secret
  仍在 `ChannelSecret`，因此这里只能宣称数据模型解耦，不能宣称凭据已解耦。
- EIM-I3 已完成 `api/identity/` 的框架无关 contracts、policy、service、共享输入 validation 与纯异步 SQLAlchemy
  repository。核心解析只接收服务端构造的 `ProviderContext + AliasKey`，不接收 `channel_id`；
  repository 用一条 SQL 同时取得精确 Provider Account、alias、canonical identity 与**当前有效的**
  `User/UserTenant`。因此 `None` 只表示 Provider Context 无效，
  `snapshot.identity=None` 才表示“account 有效但 alias 未绑定”。`IdentityService` 只持有
  `IdentityLookupRepository` 和 provisioning policy resolver，不持有写仓储。`ProviderContext` 同时
  固定 account revision 与 `last_scope_change_at` marker，read snapshot 投影 `alias_verified_at`；alias
  早于最近 scope 变化时即使 identity 仍 active 也必须要求 Provider 重验。
- I3 的三种 provisioning policy 目前只产生
  `bind_preprovisioned/require_link/create_normal_member` 的**待 Provider 验证计划**；三种结果都带
  `provider_verification_required=true`。它不会调用飞书、创建 `User/UserTenant`、激活 identity、
  在 I3 内构造 Principal 或接入 Channel。写侧按 capability 分权：ordinary mutation 只能创建
  `pending_link` identity 与执行单向收紧状态 CAS；verified identity mutation 独占 alias 新建/刷新和
  显式 activation；provider-account control 独占 health/scope/event marker CAS；verified ownership
  仍是独立的两个窄 insert 命令。`conflict/revoked` 是终态，两者都优先于 alias freshness 且不提示
  可重验；
  所有 port 都不暴露 CRUD、commit、delete、unlink 或 rebind。
- alias proof 早于 account scope marker 时写入会以 revision conflict 拒绝；proof 不早于 marker 但比
  当前 alias proof 旧时不会倒退 `verified_at`。health CAS 对省略的 scope/event 时间保留旧值，只接受
  时间单调前进；所有 SQLAlchemy Core INSERT/UPDATE 显式维护审计时间。输入在发 SQL 前做长度、枚举
  和 attributes allowlist 校验，SQLAlchemy/driver 异常统一映射为不含 bind parameter 的稳定错误码。
- I3 定向证据为 `test_identity_domain.py` **40 passed**、真 PostgreSQL
  `test_identity_repository.py` **17 passed**，合计 **57 passed**；repository + identity schema 连续真库
  **40 passed**。`make verify` 全绿：Ruff format/check、7 import contracts、async gate、mypy 71 files、
  unit **1969 passed in 28.85s**；`REQUIRE_SERVICES=1 make integration` **82 passed in 12.48s**。
  本层仍不 import FastMCP：FastMCP 4 的 auth、tool list/call middleware 继续只在 MCP
  composition/adapter 边界复用。
- P1 的 `build_principal_from_resolved_identity()` 只提升 I3 `RESOLVED` + active identity + live
  `UserTenant(owner/admin/normal)` 的一致快照，并绑定 provider/internal identity/proof time；任何
  error、provision action、provisioning policy revision、需重验或非法 membership role 都 fail
  closed。builder 只是进程内 trusted adapter seam，不接受 wire/Channel 任意 DTO。
- `api.identity.legacy_owner` 为存量 Web/API 凭据每请求活查 active User + 唯一
  `tenant_id == user.id` 的 personal OWNER Tenant。它不是通用多 Tenant selector；valid JWT 的用户/
  membership 失效不会降级重解释为 API token，意外 verifier 错误也不会被吞。
  存量 sync `Depends(manager)` 仍可能返回 ORM User，所以 P1 不代表所有 auth 入口已统一。
- I2/I2.1/I3/I6/P1 不 import FastMCP，因为 FastMCP 不拥有平台 Tenant、飞书安装实例或身份库。后续 MCP 入口仍
  优先复用 FastMCP 4 的 `RemoteAuthProvider`、`AccessToken`、`on_list_tools/on_call_tool` middleware
  和 transport 防护；领域 identity/Principal 保持框架无关，避免重复实现框架已有工具开放能力。
- P1 已解锁 A7 的代码前置，但 A7 仍是独立 inbound Resource Server 任务，不能立即宣称
  可发布。F1/I4.1/I6/I6.1/C1/C2/C3/P2 已完成；下一条 token 主线是 A2。CHN-O9 是不阻塞 A2 的
  Channel 可观测并行支线；C4/CHN-X8 仍须等待全部 runner 升级与 deployment soak。I5 与 I7 已由
  I4 解锁，可作为不共文件的并行支线；I8 仍需 I6 + I7，
  不能跳依赖；
  C3/P2、A2/P3/A7 均不属于 P1 完成面；其中 C3/P2 已由后续独立任务完成。
- MCP 出站已使用官方 SDK 2 `Client`：Streamable HTTP 使用 `mode="auto"` 和 SDK
  `create_mcp_http_client()` 受管 client（30 秒 connect/write/pool、300 秒 read），SSE 使用
  `mode="legacy"`，业务代码不再手调 `initialize()`。HTTP
  response hook 保留 `401`/`403` 分类，但 headers 仍来自 Server 配置/调用参数，不是
  request-scoped Principal 委托 token。
- 出站旧串行队列已删除，并发调用不再形成 HOL；超时会取消本地 task，但远端取消仍是
  协作式，不能由本地超时推断业务未执行。
- `InputRequiredResult` 已表面化为 `interaction_required` 结构和旁路 metadata，但尚无
  持久 `InteractionSession`、resume 或自动重试；这只完成 EIM-F3 的协议接线，不等于 U14/U15。
- MultiRAG 还通过 [`mcp/server/server.py`](../../mcp/server/server.py) 对外提供 `/mcp` 与 legacy
  `/sse`。入站已升级到 FastMCP `4.0.0b2` / MCP SDK `2.0.0`；真实 Server 已验证
  `2026-07-28` `server/discover`、路由 headers 和无 session 的 Streamable HTTP，同时保留
  legacy HTTP/SSE 兼容。这不代表 Principal、scope 或 OAuth Resource Server 已完成。
- EIM-F2 兼容矩阵已转化为 EIM-F6/F7/F3/F8 实施与回归；当前共享运行时的 exact pins
  是 `fastmcp==4.0.0b2` 与 `mcp==2.0.0`。顶层 `mcp/` 目录故意不包含
  `__init__.py`，避免遮蔽官方 `mcp` 依赖。模块当前行为与不能宣称的能力见
  [`mcp/README.md`](../../mcp/README.md)。
- Channel Execution 已通过 `stream()` 直接向 transport-neutral ReplySession 交付类型化事件；
  飞书已实现 CardKit 渐进式回复，`ask()` 只保留为兼容聚合入口。Provider/Target capabilities、
  Dialog detached CAS 与 Canvas candidate sidecar/周期 GC 已分别由 EIM-U11～U13 收口。CHN-U15 迁移与
  API/supervisor 重启已完成，CHN-U16 优雅停机终态化与跨层测试已于 2026-08-11 完成；
  **Dialog/Canvas 完整 UX 矩阵的真实飞书 smoke 仍然欠着**；C3 已完成的各一条 Principal-owner live
  不替代该矩阵。Channel 稳定性下一项是 CHN-O9 可观测，随后稳定浸泡。
  EIM-F5 / CHN-X14 保留为 upstream-first 长期任务，但挂起到
  Channel 稳定、且用户恢复从约 2026-04-24 本地同步点逐 commit 跟进 RAGFlow 时再启动；不预设自行
  实现 Canvas runtime。
- CHN-U16 只收口**正常、可控的 worker/supervisor 停机**：停止接收后清空队列（queued 项因此零
  执行调用），把已创建的 queued/running 卡片更新为明确终态，已越过 terminal barrier 的 run 则
  允许交付完，再退出进程。它不持久化队列或 action，也不声称恢复
  `kill -9`、主机掉电、跨实例取消、数据库 COMMIT 结果未知等场景；Windows 上 supervisor 的
  `TerminateProcess` 同样没有合作窗口。后者仍属于挂起的 EIM-O4 /
  CHN-O14。CHN-U16 与 CHN-O9 是 Channel 稳定性任务，不新增或挪用 EIM 映射。
- 当前 `IncomingMessage`/`OutgoingMessage` 不表达 thread、mention、attachment、reference、
  card handle 或 delivery UUID；这些目标契约统一见 [FEISHU_BOT_UX](FEISHU_BOT_UX.md)。

### of_mcp

- EIM-A3 已在 of_mcp `e4ab560` 完成：新增独立 `ofmcp-auth` 平台包，使用 joserfc 执行 ES256
  签名验证，并叠加项目自己的严格
  `mcp_access` profile verifier；Gateway composition root 通过 FastMCP 4
  `RemoteAuthProvider` 发布 RFC 9728 Protected Resource Metadata 和标准 bearer challenge。
- 对外 `/mcp` 已按精确 resource/audience、issuer、JOSE header、必需 claims/types、固定时钟偏差、
  最大 TTL、`token_use` 和 scope 词表执行认证。无效 bearer 为 `401 invalid_token`；JWKS 不可用且
  无新鲜 last-known-good cache 时为 `503 verifier_unavailable`，不把基础设施故障伪装成用户 token
  无效。固定 HTTPS JWKS 拉取拒绝重定向和环境代理，响应/key 数有界；cache 使用原子替换、
  single-flight、轮换重叠、未知 `kid` 负缓存与跨 `kid` 全局刷新冷却、负缓存条目硬上限和刷新退避。
  慢失败从请求**完成时钟**开始计算 backoff，所有浮点 cache/timeout 配置拒绝 NaN/Infinity。
- profile 现在把“是否是 OAuth Resource Server”与历史 `oauth_enabled` 分开：`local` 保持匿名且
  `secure` 强制认证、缺 issuer/JWKS/resource 配置即启动失败，只允许 mount，并因 A1 scope registry
  未登记 `hello:greet` 而关闭 hello。生产 profile/scope constants 与 A1 manifest 有机器锁定测试，
  不能单边漂移。A5 完成 internal actor token 之前，proxy 在 secure profile 下直接拒绝装配。
- A4 完成后 `local` 和 `secure` 仍由 CLI 机器拒绝绑定非 loopback；`fastmcp.json` 固定
  `127.0.0.1`，CLI 与 JSON 启动面都启用 FastMCP `host_origin_protection=auto`。这条限制现在是独立的
  remote-release gate：工具授权完成不等于 A2/P3 动态委托、企业主体绑定、A6 审计或远程发布条件已经
  完成，不能因 secure 已有 bearer 认证与 A4 授权就直接开放远程业务入口。
- Protected Resource Metadata 与两条最小 health route 是显式公开面；它们不消费 bearer、不触发
  JWKS I/O，health 只返回单一状态，不暴露 profile、服务拓扑或 scope。其余未知 route 也不会被
  attacker-controlled bearer 诱导访问 trust source；真正受保护面固定为精确 `/mcp`。
- `service.toml.scopes` 继续只定义服务的 scope 词表；A4 新增逐工具 policy，把 post-assembly 的 canonical
  tool name 精确映射到 required scopes/tenant/assurance 等约束。缺 policy、孤儿 policy、重复 canonical
  name、namespace collision 或未在当前 service scope 词表登记的 scope 都在启动/contract check 时
  fail closed。确定性
  `tool-policies.json` 快照的 `policy_revision` 为
  `7bf9e09082ca4f1d529e51bf3fe8e6dd5c4334c62deaf9a5b6be204af9fca446`；生产 verifier 的
  endpoint-level `required_scopes=[]` 保持不变，避免把所有服务 scope 错误合并成一个全局门槛。
- EIM-A4 已把 A3 严格 verifier 产出的 allowlisted claims 投影为框架无关、不可变的领域 Principal，并用
  独立测试证明所有 A1 Gateway authentication-accepted vectors 都能经过该桥接。Principal 不携带
  role/group/department、Provider 原始 ID 或上游 token；`auth_time` 只被携带并满足
  `auth_time<=iat`，本轮没有实现 freshness/max-age step-up。
- A4 使用两层同策略重验：外层 ASGI preflight 对真实 MCP 调用返回标准 HTTP 403；内层 FastMCP
  middleware 过滤 `tools/list`，并在 `tools/call` 的实际执行前再次授权，防止“列表不可见”或一次
  preflight 被误当作最终授权。缺 scope 返回 `insufficient_scope` 和完整 required scopes；tenant、
  enterprise assurance 与业务拒绝不伪装为 scope challenge。外部 membership/role/business resolver
  当前只是 fail-closed 扩展接口，现有 service policy 未启用它，也没有完成 schema-normalized 的业务
  对象授权。
- EIM-A6 已进入 `🔵` phase 1。每个工具 policy 现在显式声明 `effect=read|prepare|side_effect` 和
  `replay_mode=reusable|single_use`，并强制 `side_effect -> single_use`；leave 的 create/submit 与
  四个 medic submit 被归为单次副作用，其余当前工具为可复用只读。canonical policy snapshot
  升级为 format 2，Gateway 运行时与 contract devkit 复用同一计算，不从磁盘文件或硬编码读取
  revision。
- A6 的框架无关安全执行协调器已接在 A4 **内层最终 allow 之后、业务 `call_next` 之前**，并消费
  同一次 evaluation 的 immutable policy 与当前 runtime revision。单次工具先原子 claim，再写
  pre-execution audit，成功后才进入业务函数；同一逻辑请求重复返回 HTTP 409
  `duplicate_operation`，同一 JTI 被换参数或主体使用返回 HTTP 403 `replay_detected`，replay/audit
  前置依赖不可用返回 HTTP 503，均 `no-store`、不发 OAuth scope challenge 且不执行工具。
- 审计 schema 是冻结白名单，没有任意 `metadata`：只保存 resource/tool/effect/replay mode/
  policy revision、trace/call correlation、request fingerprint 和脱敏主体；低熵主体标识使用 keyed
  HMAC，JTI 只存摘要，不记录 bearer、工具参数/结果、Provider PII、企业 subject 或医疗正文。
  `ToolResult.is_error`、异常和取消都保守记为 `OUTCOME_UNKNOWN`，不会释放 claim 或自动重放副作用。
- 当前 OTel 只使用官方 API 丰富 current span 并产生低基数 counters；未配置 SDK/exporter 时为 no-op，
  exporter 故障不改变安全决定。`MemoryReplayClaimStore`/`MemoryAuditSink` 只用于单进程测试；secure
  必须显式注入 coordinator，且只有 shared multi-instance replay store + durable audit sink 才
  `production_ready`。真实 secure CLI 目前因没有生产后端 fail-fast，remote-release gate 继续关闭。
- `medic` 的 `workcode` 和其他业务主体字段目前仍是工具调用者自报，且工具会产生真实副作用；从
  verified Principal 注入并忽略调用方同名参数仍属于 M1/M2，不能把 A4 的 resolver seam 解释为该缺口
  已经修复。
- EIM-F4 已独立完成：of_mcp 精确升级到 FastMCP `4.0.0b2`，没有把身份或授权改造混入
  版本升级。
- EIM-A1 已完成：两仓冻结了字节一致的 91-file JWT/JWKS corpus、严格 claims 和正反互操作测试；
  79 个 token cases 包含同一合法 `mcp_internal_actor` bytes 在目标 proxy 被接受、转交 Gateway 时
  被 `mcp_access` profile fail closed 的 cross-profile 向量，不能只靠畸形 hybrid token 证明隔离。
  MultiRAG 定向 **96 passed**，完整 `make verify` 的 Ruff format/check、6 条 import contracts、
  async DB gate、mypy 65 files 全绿，unit **1904 passed in 25.76s**；of_mcp `3e1d5ac` 定向
  **100 passed**、完整门禁 **216 passed、2 existing skipped**；
  `sha256(SHA256SUMS raw bytes)` 为
  `59f82684aa06365f45623ce9bfad336d487f2c9351879266a6b2ab21bf8fe208`。A1 仍只交付
  test/docs/schema/corpus，不包含生产 issuer、verifier、Channel Principal 或动态 bearer。
- A3 用生产 verifier 逐条回归 A1 中面向 Gateway 的 **68** 个认证 case；定向 **126 passed**，
  `uv run --locked ofmcp verify` 六步全绿、**342 passed、2 existing skipped**，contract 无漂移；
  本仓文档账本的 `make verify` 同样全绿、**1904 passed**。
- EIM-A4 已在 of_mcp `74117a0` 完成；定向 **201 passed**，`uv run --locked ofmcp verify`
  六步全绿、**417 passed、2 existing skipped**，contract snapshot 无漂移。该完成态只证明 of_mcp
  能把已验证 token 构造成 Principal，并按确定性逐工具策略做发现/调用授权；不证明 MultiRAG 已经
  产生这样的 token 或第三方 Channel 身份已经进入该 Principal。
- P1 已实现领域 Principal 与存量 Web/API adapter；C3 已取得部署 live 证据，P2 已完成本地传播与
  自动门禁但未部署；A2/P3 尚未实现，因此 MultiRAG
  还不会为当前 Principal 签发并逐请求发送 token；飞书的
  `ExternalIdentityAssertion -> binding/directory -> Principal` 已完成到 target/session owner，通用 MCP Client 仍无完整
  OAuth 获取 token 流。of_mcp 仍缺 A5 proxy internal actor；A6 虽已有 phase-1 domain/runtime
  安全边界，但仍缺生产多实例 replay/audit、HMAC/KMS 轮换、OTel SDK/exporter 与跨仓 trace，因此
  保持 `🔵`。M1/M2 企业主体与业务对象授权、持久 Interaction/Confirmation/Idempotency 也未完成。
  F1/I4.1/I6/I6.1/C1/C2/C3/P2 已完成；MultiRAG 下一条 token 主线是 A2；I5/I7 是已解锁
  并行支线，但 I8 仍需 I6 + I7。P3 仍必须等待 A2，不能把 P2 的 context seam 当成 token 能力。
  A7 虽已解锁前置，仍须作为独立入站
  安全面实现和验收。

---

## 7. 完成定义

整个项目只有同时满足以下条件才可宣布完成：

- 首期同一飞书企业、应用安装实例和 Channel binding 都只能解析到一个服务端固定 MultiRAG Tenant；
  消息不能动态选 Tenant，集团级例外未经过新 ADR/schema 迁移不得启用。
- 飞书企业身份可以稳定映射到同一 `platform_user_id`，跨 App 不重复开户、跨租户不碰撞。
- 离职/冻结/权限范围收窄后身份及时失效，缓存和故障场景 fail closed。
- Channel 原始身份不再被直接当 Principal；Principal 只由服务端 resolver 构造。
- MultiRAG 的 Agent、Memory、Workflow 和 MCP 调用全程获得同一 request-scoped Principal。
- of_mcp 对所有远程入口强制鉴权、audience、scope 和审计；mount/proxy 形态语义一致。
- medic 不再信任 `workcode` 参数，敏感提交需要确认且具备端到端幂等性。
- 飞书普通对话不会每次查询通讯录/OA；没有全量日同步依赖。
- 飞书普通对话具备即时 acknowledgement、渐进式回复、最终 flush 和明确降级；CardKit 故障不会
  吞掉最终答案，同一事件不会重复执行或重复发送同阶段回复。
- 群聊、话题、多模态和敏感卡片只在各自身份/授权依赖满足后开放，不因 transport SDK 切换而
  绕过安全闸门。
- 两个仓库各自验证门禁全绿，跨仓契约测试和真实集成测试通过。
- [ROADMAP](ROADMAP.md) 所有必做任务为 `✅ 完成`，变更日志记录了提交和具体证据。
