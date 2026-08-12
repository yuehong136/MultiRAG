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
读 docs/enterprise-identity-mcp/README.md，然后做 EIM-I1。
先复核任务锚点和依赖，把准备修改的文件与验收标准告诉我；确认后再写代码。
```

一次只派一个任务。`EIM-*` 是跨项目工作包 ID；某些任务同时映射一个 `CHN-*` ID，这是
Channel 仓内的强制记账，不是重复任务。

---

## 2. 最终结论

完整链路固定为：

```text
(provider_tenant_key, app_id, open_id)
  -> 飞书 tenant-scoped user_id
  -> MultiRAG platform_user_id
  -> 可选的 enterprise_subject（employee_no / talent_id / workcode）
  -> audience-bound MCP access token
  -> of_mcp Principal + scope + 业务侧授权
```

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
  - auth audit / OTel / replay protection（EIM-A6）
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

不做“每天把全公司组织架构完整复制到 MultiRAG”。使用：

```text
JIT 解析 + 通讯录事件失效 + 已链接活跃用户的周期兜底校验
```

- 首次消息、本地映射缺失、缓存过期或高风险操作前才调用飞书通讯录。
- 订阅 `contact.user.created_v3`、`contact.user.updated_v3`、
  `contact.user.deleted_v3`、`contact.scope.updated_v3`。
- 离职、冻结、主动退出、不可见或数据权限被收窄时 fail closed。
- 周期任务只校验已链接且近期活跃的身份，不抓取全量组织树。
- 正常 RAG 对话命中本地映射后不再调用飞书或 OA。

企业策略支持三种 provisioning 模式：

| 模式 | 行为 | 推荐场景 |
|---|---|---|
| `preprovisioned` | 只有管理员预建的 MultiRAG 用户可绑定 | 高合规、禁止自动开户 |
| `link_only` | 用户先登录 Web，再用一次性码绑定飞书 | 通用 SaaS 默认、可避免重复账号 |
| `jit` | 飞书目录验证通过后自动创建 `User` + `UserTenant(NORMAL)` | 单公司部署、全员可用；原力推荐 |

任何模式都不得把首次用户创建成 `OWNER` 或 `ADMIN`。

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

- [`api/channels/README.md`](../../api/channels/README.md) 已明确：`IncomingMessage.sender_id`
  是不可信外部标识，不能直接作为 Principal。这条边界必须保留。
- `api/channels/feishu/channel.py::_normalize` 当前只从 `open_id/union_id/user_id` 中取第一个
  非空字符串，无法支持正式身份解析。
- `api/channel_execution/adapters.py::SqlAlchemyBindingResolver.resolve` 当前把
  `principal_id` 固定为 `None`，这里是验证后身份提升的装配点。
- `api/utils/api_utils.py::Principal` 当前只有 `id/email/nickname`，不足以表达租户、认证方式、
  企业业务主体和委托上下文。
- `User.email` 当前非空且唯一；JIT 不能靠伪造邮箱长期绕过，必须按 ROADMAP 先完成账户模型
  兼容设计和迁移。
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
  **Dialog/Canvas 真实飞书 smoke 仍然欠着**，下一项是 CHN-O9 可观测，随后稳定浸泡。
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
  `6f79e7ddf8f630993a054f284ebd5213424ffe39b252c661d16a2967ed6fdd67`；生产 verifier 的
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
- A2/P1/P2/P3 尚未实现，因此 MultiRAG 还不会为当前 Principal 签发并逐请求发送 token；飞书的
  `ExternalIdentityAssertion -> binding/directory -> Principal` 链也尚未完成，通用 MCP Client 仍无完整
  OAuth 获取 token 流。of_mcp 仍缺 A5 proxy internal actor、A6 审计/OTel/replay、M1/M2 企业主体与
  业务对象授权；持久 Interaction/Confirmation/Idempotency 也未完成。下一项 of_mcp 任务是 A6；
  MultiRAG 应并行推进 `I1 -> I2 -> I3 -> P1`、`F1 + I3 -> I4 -> I6` 与 `C1 -> C2`，不能跳过
  `C3 -> P2` 直接做 A2/P3。本仓文档收口后的 `make verify` 全绿、**1904 passed**。

---

## 7. 完成定义

整个项目只有同时满足以下条件才可宣布完成：

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
