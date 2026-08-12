# EIM 架构决策记录

本文件只记录已经定案、后续实现不得擅自改变的决策。任务状态见 [ROADMAP](ROADMAP.md)。
确需改变某项决策时，追加新的 ADR 取代旧项，禁止直接改写历史结论。

---

## EIM-ADR-01：一个企业客户映射一个 MultiRAG Tenant

**状态**：Superseded in part by EIM-ADR-23
**日期**：2026-08-07

**失效范围**：下文“同一飞书租户可以映射多个 MultiRAG Tenant”的例外已被
[EIM-ADR-23](#eim-adr-23首期一个飞书企业一个-tenantprovider-account-与-binding-固定单-tenant)
收紧。企业边界必须由服务端决定、消息不得选择 Tenant，以及默认一个企业客户一个 Tenant 的部分
继续有效。

默认模型：一个企业客户对应一个 MultiRAG `Tenant`，员工通过 `UserTenant` 成为该 Tenant 的
成员。飞书租户 `tenant_key` 通过服务端管理的 Channel account/binding 映射到 MultiRAG
`tenant_id`，不能由消息体覆盖。

同一集团确需多业务租户时，可以把一个飞书租户映射到多个 MultiRAG Tenant，但必须由不同
Channel binding、应用或明确的服务端路由策略区分；绝不能让用户消息选择目标 tenant。

---

## EIM-ADR-02：飞书 `user_id` 是 Canonical Provider Subject

**状态**：Accepted

飞书身份键使用：

```text
(provider="feishu", provider_tenant_key=tenant_key, provider_user_id=user_id)
```

`open_id` 只作为 `(tenant_key, app_id, open_id)` 入口别名；`union_id` 只作为同一开发商多应用
关联的辅助索引。不能把裸 `open_id` 设为全局唯一，也不能把它直接当 `platform_user_id`。

如果事件因字段权限没有返回 `user_id`，必须调用通讯录 V3 按 `open_id` 查询；查询失败、用户
不在应用数据范围内或状态不可用时 fail closed，不能降级为“相信 open_id 就是员工”。

---

## EIM-ADR-03：`platform_user_id` 使用现有 `User.id`

**状态**：Accepted

不新建一套与 `User` 平行的平台用户主表。正式 `platform_user_id` 就是现有 `t_ai_users.id`，
外部身份表只负责把 Provider subject 链接到它。

理由：Agent、会话、Memory、租户成员关系和既有 API 已围绕 `User.id` 工作。平行用户表会把
每条数据路径都变成双主体，并制造长期迁移债。

JIT 所需的账户兼容必须通过显式模型演进完成：允许 identity-only 用户没有邮箱/密码，并用
`account_kind`（最终名称以任务实现为准）区分本地登录账户、外部身份账户和已合并账户。
禁止用 `@identity.invalid` 假邮箱长期绕开当前非空约束。

---

## EIM-ADR-04：外部身份和企业业务身份分表

**状态**：Accepted

飞书 `user_id/open_id/union_id` 属于 `ExternalIdentity`；`employee_no/talent_id/workcode` 属于
`EnterpriseSubjectLink`。不能把所有字段塞进 `User`，也不能假设所有企业的
`employee_no == talent_id`。

Provider 负责证明“这是飞书租户里的哪位成员”；EnterpriseSubjectResolver 负责证明“这个成员
在业务系统里的主体是什么”。内置 `FeishuEmployeeNumberResolver`，企业不满足时可替换为
`OAResolver/HRResolver`，接口和核心流程不变。

---

## EIM-ADR-05：不全量复制组织架构

**状态**：Accepted

采用 JIT、事件失效和已链接活跃用户兜底校验。不建立部门、上级、角色和公司权限的完整镜像，
除非未来出现独立、经过评审的业务需求。

缓存的姓名、头像、员工状态只用于显示和身份可用性；不得把飞书部门或职位直接转换为
MultiRAG 管理员、MCP scope 或业务权限。

---

## EIM-ADR-06：身份解析部署在 MultiRAG，of_mcp 不接飞书

**状态**：Accepted

第一阶段 `EnterpriseIdentityService` 与 Provider adapter 部署在 MultiRAG，因为它们解决的是
Channel 用户到平台用户的认证。of_mcp 只消费标准 bearer token 和 Principal。

身份接口必须保持独立，满足条件后可抽成 `identity-broker`，但不提前建立独立进程。

---

## EIM-ADR-07：请求级委托，不使用静态 MCP 用户 Header

**状态**：Accepted

MultiRAG 每次 MCP 请求根据当前 Principal 和目标 resource 获取短期、audience-bound access
token。不得把用户身份写进 MCP Server 的静态 `headers`，不得在 Agent 初始化时建立携带某位
用户身份的长生命周期 MCP session，也不得把身份字段放进提示词让模型转述。

token 最长生命周期初始目标为 5 分钟；具体值由安全配置控制。每个 HTTP 请求都验证 token，
不能依赖 MCP session 或连接状态承载身份。

---

## EIM-ADR-08：MCP Server 是 OAuth Protected Resource

**状态**：Accepted

of_mcp 对外 HTTP 入口按 MCP Authorization 规范实现资源服务器：

- 提供 RFC 9728 Protected Resource Metadata；
- 返回标准 `WWW-Authenticate` challenge；
- 校验 `iss`、`aud/resource`、`exp/nbf`、签名、scope 和必要的 tenant/client claims；
- token 只用于 of_mcp，不原样透传 OA/Jira/ERP；
- 下游系统需要 token 时，由 MCP service 以自己的 client 身份获取独立下游凭据。

第一阶段可以由 MultiRAG 内部 issuer 签发 ES256 JWT；未来企业已有成熟 IdP 时升级到 MCP
Enterprise-Managed Authorization（EMA/ID-JAG）。不能把飞书 `open_id` 包装成伪造的 OIDC
Identity Assertion。

---

## EIM-ADR-09：权限分为三层，任何一层都不能替代其他层

**状态**：Accepted

1. **平台成员资格**：用户是否属于 MultiRAG tenant，由 `UserTenant` 决定。
2. **MCP 能力授权**：该委托是否包含 `medic:submit` 等 scope，由 issuer 和 of_mcp 校验。
3. **业务对象授权**：是否能提交/查询特定工单、工资、合同等，由业务系统或外部 PDP 决定。

飞书应用数据范围只是“应用能读到哪些通讯录成员”，不是上面任何业务授权层。

---

## EIM-ADR-10：Provisioning 是租户策略，不是 Provider 特例

**状态**：Accepted

`preprovisioned/link_only/jit` 是所有企业身份 Provider 共用的策略。原力首期使用 `jit`；通用产品
默认使用 `link_only`。JIT 自动创建的租户成员角色固定为 `NORMAL`，管理员提升只能走现有受保护
的租户成员管理流程。

账号合并必须显式完成，并保留审计记录；不能通过相同姓名、邮箱或手机号静默合并。

---

## EIM-ADR-11：飞书传输优先评估官方独立 Channel SDK，但不预设迁移成功

**状态**：Accepted

`lark-channel-sdk` 是 Channel transport 的优先迁移候选，完整 OpenAPI 始终保留 `lark-oapi`。
依据是飞书官方已经把 `lark_oapi.channel` 标为迁移期 legacy 入口，并声明新 Channel 功能进入独立
SDK；这证明的是产品演进方向，**不证明这个新仓库已经比成熟的 `lark-oapi` 更稳定**。

MultiRAG 当前使用的是更底层的 `lark_oapi.ws.Client`，并依赖模块级 event loop 与私有停止字段；
独立 SDK 的价值是争取换成受支持的公共生命周期，而不是接管平台控制面。即使迁移成功，managed
binding、加密 Secret、worker、Redis 原子去重、会话顺序、租户解析和执行边界仍归 MultiRAG。

迁移必须作为独立 PoC，在 `compat -> audit -> strict` 阶段推进，不能与身份表或 Principal
改造混成一个 PR。只有身份字段无损、公开 start/stop/reconnect 可用、SDK 内置去重不与 Redis
冲突、日志不泄露连接凭据、回滚不改变 `IncomingMessage` 契约全部成立时才切换；任一关键门禁
失败就保留现有 transport。长连接回调只入队，不执行模型或外部 API。

---

## EIM-ADR-12：敏感副作用必须“身份 + 授权 + 确认 + 幂等”四项齐全

**状态**：Accepted

`medic` 等产生真实外部副作用的工具只有同时满足以下条件才能执行：

1. token 中存在经过验证的企业主体；
2. scope 与业务授权都允许；
3. 用户通过飞书卡片或等价可信 UI 明确确认具体动作；
4. 服务端持久化幂等键，重试和重复点击不重复执行。

当前工具参数 `workcode` 将改为可选且被忽略，从 Principal 注入真实值，以保持契约向后兼容。

---

## EIM-ADR-13：日志和 token 不携带原始 Channel 标识

**状态**：Accepted

MCP access token 不携带 `open_id`、飞书 access token、手机号、邮箱或姓名。审计可以记录
`platform_user_id`、企业主体的不可逆哈希、tenant、agent、tool、scope、decision、jti 和 trace。

应用 Secret、tenant/user access token、完整事件、问题答案和 MCP 参数继续遵守 Channel
日志脱敏不变量。

---

## EIM-ADR-14：技术版本升级必须与功能改造分离

**状态**：Accepted

`lark-oapi 1.7.1 -> 1.7.2`、引入 `lark-channel-sdk 1.2.0`、MultiRAG MCP SDK v2 迁移和
`FastMCP 4.0.0b1 -> b2` 都必须各自有独立验证和回滚面。版本事实会过期；每个版本任务开工前
按 [VERSION_BASELINE](VERSION_BASELINE.md) 重新核验官方源，不得因为本文写了某个版本号就
跳过检查。

---

## EIM-ADR-15：飞书体验能力不以 Channel SDK 迁移为前置条件

**状态**：Accepted

MultiRAG 的流式执行、ReplySession、CardKit 渲染、reaction、富文本、发送幂等和降级链是独立
能力。`lark-oapi` 已提供完整 IM、Reaction 与 CardKit OpenAPI，EIM-U0/U1 可以在当前 transport
上实现；EIM-C5/CHN-P14 只评估是否用官方 `lark-channel-sdk` 替换 Provider 内部 transport 和
outbound adapter。

因此，SDK PoC 失败不能阻塞用户体验改造；UX 任务也不能顺手引入或切换 SDK。两条支线只在稳定
Provider 接口汇合，并以同一组消息规范化、流式、幂等、日志和回滚契约验收。

---

## EIM-ADR-16：渐进式回复展示安全状态，不展示模型推理或原始工具轨迹

**状态**：Accepted

Channel 只消费服务端白名单 `status_changed`、用户可见 `message_delta`、脱敏 references/artifacts
和稳定错误码。卡片可以展示“检索资料”“调用已授权服务”“整理答案”等粗粒度状态，但不得展示
chain-of-thought、MCP 参数、SQL、企业工号、token、文件正文、异常栈或内部 URL。

所有 Provider 通过 transport-neutral ReplySession 表达 `begin/append/status/complete/fail`；
飞书 CardKit 只是一个 renderer。CardKit、reaction 或客户端兼容失败时只降级渲染，不能重新执行
Agent、放宽身份授权或自动重试已有副作用的工具。具体契约见
[FEISHU_BOT_UX](FEISHU_BOT_UX.md)。

---

## EIM-ADR-17：MultiRAG 的 MCP Host/Client 与 MCP Resource Server 是独立安全和发布面

**状态**：Accepted
**日期**：2026-08-12

MultiRAG 同时承担两个方向相反的 MCP 角色：

1. **出站 Host/Client**：Agent 代表当前 request-scoped Principal 调用 `of_mcp` 等外部资源；
2. **入站 Resource Server**：`mcp/server/` 向外部 MCP Client 暴露 MultiRAG 检索能力。

两者可以共享标准 MCP 类型和契约测试思想，但必须使用不同的 resource URI、audience、credential
生命周期、scope policy、依赖边界、部署顺序和回滚面。任一方向收到的 bearer、连接状态或
`requestState` 都不得被另一个方向复用，也不得把 MultiRAG 后端 API token 当成通用委托凭据。

协议升级前先证明两个运行时的依赖图可解且可以独立回滚。若同一 Python 环境中的 SDK/FastMCP
版本约束互斥，先建立可独立部署的依赖边界，再分别迁移 Client 与 Resource Server；禁止为追求
“一次升级到最新”把两个方向、身份功能和授权功能合进一个 big-bang 变更。

---

## EIM-ADR-18：MCP 多轮输入由持久化 InteractionSession 承载，不复用 ReplySession 或连接状态

**状态**：Accepted
**日期**：2026-08-12

MCP Multi-Round-Trip Request 返回 `InputRequiredResult` 时，由 MultiRAG 作为 Host 创建或更新
provider-neutral `InteractionSession`，持久化当前 Principal、tenant、resource、tool、输入请求、
opaque `requestState`、revision 和过期时间。飞书卡片或 H5 只是该会话的 UI renderer；用户响应经
验证、幂等 claim 和 schema 校验后，才以 `inputResponses + requestState` 恢复同一逻辑工具调用。

`ReplySession` 只负责单次回答的渐进式输出和终态交付，不保存待输入业务状态；MCP transport
connection/session 也不承载身份或恢复语义。`requestState` 是服务端不透明 continuation，不是
Principal、授权凭据或防重放令牌，必须绑定 InteractionSession 后再原样回传。

InteractionSession 只证明“哪次交互收到了什么输入”。敏感副作用仍必须独立满足 ADR-12 的可信
身份、scope/业务授权、持久化 Confirmation 和端到端幂等；不能用 MRTR、表单提交或一次按钮点击
替代执行前重新授权。

---

## EIM-ADR-19：当前无 FastMCP 3 生产消费者，MCP 2/FastMCP 4 采用全根协同切换

**状态**：Accepted
**日期**：2026-08-12
**取代范围**：只取代 ADR-17 中“版本冲突时默认先拆独立 Server 运行时”的本次部署选择；
Host/Client 与 Resource Server 的安全、resource、audience、scope 和发布边界继续由 ADR-17 约束。

EIM-F6 已复现 FastMCP 3 `mcp<2` 与 MCP SDK 2 `mcp>=2` 的依赖冲突。由于当前没有生产环境使用
MultiRAG 的 FastMCP 3 服务，用户明确批准不保留该生产运行时，选择把 MultiRAG 根依赖协同切换到
MCP SDK 2/FastMCP 4，而不是为旧 Server 新建独立 project/venv。`of_mcp` 的对应纯版本升级锚点为
EIM-F4 commit `23dd1fd`。

当前协议基线是 MCP SDK `2.0.0`/FastMCP `4.0.0b2`：

- MultiRAG outbound 使用官方 MCP 2 高层 `Client`，HTTP 现代优先并自动协商 legacy；
- MultiRAG inbound 使用 FastMCP 4/MCP 2 的 `2026-07-28` modern Server，同时保留 legacy 协议兼容；
- FastMCP 3 只以 PEP 723 + 独立 lock 的真实子进程 fixture 存在，不与根运行时混装；
- `make mcp-compat` 的双向协议矩阵为 13/13，通过依据包括实际协商分支，而不只是 tools/list/call
  的业务结果。

这次依赖协同切换不把两个 MCP 角色合并成同一安全主体，也不证明身份/授权链已经落地。EIM-A1 先
固定 token/JWKS test vectors；EIM-A7 仍依赖 A1/P1，才实现 inbound OAuth Resource Server、
Principal、audience/scope 和工具可见性；EIM-U14 仍依赖 P3/A4/C3，才把 `InputRequiredResult` 接入
持久化 InteractionSession、CAS 和恢复前重授权。协议层可以暴露输入请求，但在这些任务完成前不得
宣称 delegated token、企业级授权或飞书表单闭环完成。

HTTP timeout 的边界是取消本地等待和底层本地调用 task，并消除旧单 server FIFO 导致的 HOL；远端
是否停止仍依赖 transport/server 协作，可能在调用方超时后完成。因此 timeout 不是业务“未执行”证明，
写工具必须继续使用 Confirmation、幂等键和结果未知对账。回滚以 MultiRAG commit + 根 lock 为单位；
PEP 723 legacy fixture 是兼容证据，不是生产回滚运行时。

---

## EIM-ADR-20：Channel assertion、MCP access token 与 internal actor token 是三类独立信任工件

**状态**：Accepted
**日期**：2026-08-12

首期第三方 Channel 采用**平台托管 adapter**。飞书等 Provider 的事件先由受管 worker/adapter 按
transport 验证 webhook 签名/加密或受认证的长连接，并校验应用、租户、时间和重放，再把结构化但
仍不可信的外部标识交给 MultiRAG。
MultiRAG 只能结合服务端 binding、provider account 和目录查询，把它解析为自己的 Principal；
Channel payload 不得声明或覆盖 `principal_id`、`tenant_id`、role、scope、audience 或确认状态。

端到端链路存在三类不得互换的工件：

1. **ExternalIdentityAssertion**：Provider 事件中的外部身份材料，不是 OAuth/JWT access token；
2. **`mcp_access`**：MultiRAG 根据已验证 Principal 为一个精确 MCP resource 签发的短时用户委托；
3. **`mcp_internal_actor`**：of_mcp gateway 验证外部委托后，为一个精确 proxy service 换发的更短、
   scope 只减不增的内部委托。

EIM-A1 为后两类工件冻结项目私有、RFC 9068-shaped 的 ES256/JWKS profile。这里的
“RFC 9068-shaped”只描述本项目选择的 JWT claims 和验证规则，不表示 MCP 规范要求所有实现使用
JWT 或 ES256。两个 profile 使用不同 issuer/keyset/resource audience 和 `token_use`；gateway 与
proxy service 必须互相拒绝对方 profile，即使签名、用户和 scope 看起来合法。cross-profile 门禁
必须让合法 internal actor 在目标 proxy 可接受、同一 compact token 到 Gateway 被拒绝，不能只依赖
malformed/hybrid 样本。

`mcp_access.sub` 只使用不透明的 `platform_user_id`。Provider 原始 ID、姓名、邮箱、员工号、role、
group、department 和上游 access token 不进入 token。Provider 与认证方式保留在 Principal/审计；
只有上游确实证明认证保证时才映射到标准 `auth_time`、`acr`、`amr`。只有目标 resource 明确要求
时，才以最小、resource-local 的 `enterprise_subject={type, issuer, subject, tenant}` 签发；
`subject` 为不透明值，仍须独立通过业务对象授权。

外部托管 Connector、EMA/ID-JAG 和多个真实 issuer 仍属于 EIM-A8 闸门。没有真实需求时，不为
Channel 事件伪造 Identity Assertion grant，也不因为 FastMCP 提供 MultiAuth 就提前扩大 A1 profile。

---

## EIM-ADR-21：Resource Server 认证与工具授权分阶段、同入口 fail closed

**状态**：Accepted
**日期**：2026-08-12

of_mcp 的远程认证只能在 Gateway composition root 装配，service `build_server()` 不得持有 `auth=`。
EIM-A3 先交付一个可独立验证的 OAuth Protected Resource 认证边界：严格验证 `mcp_access`、发布
RFC 9728 metadata/challenge，并把客户端 token 错误与 verifier 基础设施故障分别映射为 401/503。
EIM-A4 已在同一个受保护入口内把 A3 allowlisted verified claims 投影为框架无关的 immutable
Principal，并执行工具 scope、tenant、assurance 与业务 policy。Principal 不复制 role/group/department、
Provider 原始标识或上游 token；FastMCP `AccessToken` 只存在于 auth adapter/composition 边界。分阶段
实现不允许产生第二条绕过 Gateway 的业务入口。

A3 production verifier 的 FastMCP `required_scopes` 固定为空。原因不是 scope 可选，而是 Gateway
承载多个工具：把所有 enabled service scopes 合并成 endpoint-level required set，会迫使只调用
`leave:read` 的最小权限 token 同时拥有 `medic:submit`，破坏 least privilege；只要求任意一个 scope
又会让它越权调用其他工具。FastMCP global scope 403 作为 typed HTTP seam 保留测试，真实
`tool -> required scopes` 已由 A4 在 post-assembly canonical tool catalog 上显式注册，并对
`tools/list` 和 direct `tools/call` 使用同一策略。`service.toml.scopes` 是词表，逐工具 policy 才是
执行授权；缺失/孤儿 policy、重复 canonical name、namespace collision 和未在当前 service scope 词表
登记的 scope 均 fail closed。
Gateway 生成确定性 `tool-policies.json` contract snapshot，`policy_revision` 是不含自身 revision 字段的
canonical policy document 的 SHA-256；A4 落地时的 format-1 revision 为
`6f79e7ddf8f630993a054f284ebd5213424ffe39b252c661d16a2967ed6fdd67`，A6 按 ADR-22 把 effect/replay
mode 纳入 format 2 后已产生新 revision。

A4 刻意保留两层重验。外层 ASGI preflight 在受保护 MCP HTTP request 上解析有界请求并返回真实
HTTP 403；内层 FastMCP middleware 用同一个 registry 过滤 `tools/list`，且在 `tools/call` 进入实际工具
前再次授权。这样既不把“不可见”误当成不可调用，也不把 preflight 结果跨越请求解析/执行边界当成
永久 authority proof。FastMCP 默认 SSE 会提前发 response start，项目 response guard 因此把它保留到
内层检查完成，确保 TOCTOU 二次拒绝/基础设施故障仍是 HTTP 403/500。缺 scope 返回
`insufficient_scope` 和完整 required scopes；tenant、assurance、
membership/role/business denial 不伪装 scope challenge。resolver 缺失、返回非法值或自身失败属于
服务端 policy infrastructure failure，不能降成普通用户 403，更不能 fail open。

部署 profile 同样分层但不能静默降级：

- `local` 的 `resource_auth_mode=disabled` 只用于 loopback 开发，不能绑定远程地址后冒充安全模式；
- `secure` 的 `resource_auth_mode=enforce` 缺 canonical issuer/JWKS/resource 配置即启动失败，只允许
  mount，且只装配 A1 scope registry 内服务；
- A5 完成 `mcp_internal_actor` 换发前，proxy 在 secure profile 下拒绝装配，外部 bearer 永不透传；
- RFC 9728 metadata 与最小 health 是显式公开面，health 不泄露 profile/服务拓扑/scope；这些公开
  route 和未知 route 上的 Authorization 不得触发 JWKS I/O，受保护业务 path 固定为精确 `/mcp`。
- A4 完成后 secure 与 local 仍由 CLI 机器拒绝绑定非 loopback；CLI 和 `fastmcp.json` 都启用
  `host_origin_protection=auto`。这是独立 remote-release gate，而不是 A4 完成条件的自动副作用；
  A2/P3 动态委托、企业主体、A6 审计/重放与上线证据未完成前继续保持 loopback。

因此，A3 通过只证明“Gateway 能正确认证属于自己的短 token，并在 trust source 故障时 fail
closed”；A4 通过证明“Gateway 能把该 token 构造成 Principal，并按完整工具策略快照同时约束发现与
执行”。两者仍不证明 MultiRAG issuer/request-scoped bearer 或飞书用户逐请求委托已闭环。

A4 的 external membership/role/business resolver 是 future seam，当前启用的 service policy 没有依赖
这些 resolver；业务对象输入也尚未经过统一 tool schema normalization，所以不能宣称已经具备生产级
对象授权。`auth_time` 目前只作为已验证 claim 携带并满足 `auth_time<=iat`，没有 freshness/max-age
策略。secure 继续保持 loopback；A6 phase 1 已接续但尚非生产完成态。MultiRAG 先推进身份数据、
binding、Channel assertion 与 P1/P2，再进入 A2/P3。

---

## EIM-ADR-22：工具副作用显式分类；JTI 是单 capability，replay guard 不冒充业务幂等

**状态**：Accepted
**日期**：2026-08-12

每个 of_mcp 工具 policy 必须显式声明 `effect=read|prepare|side_effect` 和
`replay_mode=reusable|single_use`。所有 policy model/registry/auth projection 都强制
`side_effect => single_use`；不得从工具名、description、scope 或 FastMCP annotation 推断风险。
effect/replay mode 属于可评审授权快照并进入 canonical `policy_revision`，Gateway runtime 与 contract
devkit 复用同一 builder。A6 format-2 local revision 为
`7bf9e09082ca4f1d529e51bf3fe8e6dd5c4334c62deaf9a5b6be204af9fca446`。

single-use replay key 只由 domain-separated `(token_use, issuer, audience, jti)` digest 构造，代表整枚
token/JTI 的一个 capability；它故意不包含 tool。完整 Principal、tool、policy revision 和 canonical
arguments 进入另一个 secret-keyed HMAC fingerprint：同 key/同 fingerprint 是 duplicate；同 key/不同
fingerprint 是 replay conflict。这样可阻止拿同一授权 capability 改参数、换主体或换工具。相应代价是
一枚 JTI 只能承载一个高风险逻辑执行；未来 P3 必须每次执行换发新的短期 token/JTI，不能给一个 Agent
run 发一枚“多次提交券”。

coordinator 只能插在 A4 inner final allow 后、业务执行前。它先原子 claim、写 pre-execution audit、
标记 dispatched，再给出不可伪造的 process-local permit。duplicate/conflict/replay-or-audit unavailable
分别稳定映射为 409/403/503，均 no-store、无 OAuth scope challenge 且零业务调用。成功记录
`SUCCEEDED`；工具错误、异常和取消保守记录 `OUTCOME_UNKNOWN`。已经 dispatch 后的 outcome 持久化
失败不得释放 claim、自动重试或覆盖业务响应。

审计 schema 采用 frozen allowlist，不提供任意 metadata bag；低熵主体用 keyed HMAC，JTI 只保留
issuer-domain-separated digest，参数/结果/token/Provider PII/enterprise subject/业务正文禁止进入审计。
OTel 只是 best-effort 观察面：API adapter 可丰富 current span 和低基数 counter，但 exporter 故障不能
改变安全决定。

这个 replay guard **不是业务幂等、结果缓存或外部系统事实查询**。duplicate 只拒绝，不返回先前
结果；`OUTCOME_UNKNOWN` 也不能证明 OA/Jira 未执行。M3/M4 仍需业务 idempotency key、查询恢复和
unknown-outcome 对账。A5 未完成前也没有 `parent_jti_hash` 链。

采用分阶段交付：phase 1 落 domain contract、内存测试 oracle、OTel API 和 Gateway execution seam，
但 A6 保持 `🔵`。只有 shared multi-instance replay store、durable append-only audit、HMAC key 的 KMS
ownership/rotation、OTel SDK/exporter/collector 与 W3C 跨仓 trace、A5/P3/M3/M4 集成和 remote-release
演练完成后才可标 `✅`。memory store/sink 的结构属性固定不是 production-ready；真实 secure Gateway
没有生产 coordinator 时必须 fail-fast，不能用普通配置布尔值解锁。

---

## EIM-ADR-23：首期一个飞书企业一个 Tenant，Provider Account 与 binding 固定单 Tenant

**状态**：Accepted；Provider Account 与 Channel 的所有权形状由 EIM-ADR-24 取代
**日期**：2026-08-12
**取代范围**：收紧 ADR-01 中“一个飞书租户可由不同 binding、应用或服务端路由策略映射多个
MultiRAG Tenant”的例外措辞；ADR-01 的企业边界由服务端决定、消息不得选择 Tenant 继续有效。

MultiRAG 平台本身保持多租户，但首期每个接入企业采用单租户模型：一个已验证飞书
`tenant_key` 恰好对应一个 MultiRAG `Tenant.id`。一个飞书应用安装实例/Provider Account 恰好归属
一个 Tenant；一个 Channel binding 也恰好绑定一个 Provider Account 和一个 Tenant。该关系由服务端
onboarding 持久化并以数据库唯一约束强制，不能由消息、卡片、prompt、worker command、目录返回值
或运行时路由规则动态选择。

EIM-I2 因此新增两级 ownership 表，在身份数据入口机器强制：

```text
t_ai_identity_provider_tenants:
  UNIQUE(provider, provider_tenant_key) -> one tenant_id

t_ai_identity_provider_accounts:
  FOREIGN KEY(tenant_id, provider, provider_tenant_key) -> provider tenant ownership
  UNIQUE(provider, provider_tenant_key, provider_account_key) -> one tenant_id
  UNIQUE(channel_id) -> the same provider account and tenant_id
```

alias 与 event receipt 必须用 tenant/provider/provider-tenant/provider-account 的复合外键引用该
account ownership；account 又必须引用企业 ownership。只有 canonical identity 没有 app/account 维度。
这样，调用方即使篡改 `tenant_id`，也不能在另一个 Tenant 下重用同一安装实例、alias 或 event ID。
所有外键 `ON DELETE RESTRICT`，
解绑或停用不能级联抹掉身份历史。

未来集团级多 Tenant 只允许作为经过独立 ADR、迁移和上线评审的受控例外。每个目标 Tenant 必须使用
**不同**的 Provider Account/应用安装实例（以及对应独立 binding）；同一个 Provider Account 或
binding 在任何阶段都不能路由多个 Tenant。未来若要放开同一
`(provider, provider_tenant_key)` 归属多个 Tenant，必须新增 ADR 和显式 schema 迁移、数据冲突审计、
控制面策略及上线门禁；首期 schema 不预埋能绕过 enterprise ownership 的动态路由旁路，且无论如何
不能放开 account/binding 的单 Tenant 不变量。

这项 ownership 是 MultiRAG 身份/Channel 控制面的领域约束，不属于 MCP 或 FastMCP 的工具开放能力。
FastMCP 4 继续负责 MCP auth、工具发现/调用 middleware 和 transport 防护；它既不识别飞书安装实例，
也不能替代 Provider Account 到 Tenant 的持久映射。

I2 只建立持久化不变量；I3 repository 不得把 ownership 暴露为普通 CRUD。普通 Channel/管理员更新
禁止换绑 `tenant_id/provider/provider_tenant_key/channel_id`，所有 ownership 与 identity 历史禁止
hard-delete。verified onboarding/rotation 使用显式领域命令；状态、scope 和 revision 变化必须通过
`identity_revision` 的 compare-and-set，或同一事务内的行锁 + revision 复核。冲突 fail closed，不能
last-write-wins、删除重建或靠 FastMCP middleware 修补身份库竞态。

EIM-ADR-23 对 `ProviderTenant -> Tenant` 的固定单 Tenant 关系继续完整有效；其中
`IdentityProviderAccount.channel_id NOT NULL UNIQUE` 以及“Provider Account 从属于 Channel”的实现
形状，由下述 EIM-ADR-24 取代。Channel/binding 固定单 Tenant 的安全结论没有放宽。

---

## EIM-ADR-24：Provider Account 是独立企业连接，Channel 通过显式 link 引用

**状态**：Accepted
**日期**：2026-08-12
**取代范围**：取代 EIM-ADR-23 中 Provider Account 直接持有 `channel_id`、随 Channel 存在的关系
形状；不改变一个 Provider Tenant、Provider Account 和 Channel 均固定单 Tenant 的安全不变量。

Provider Account 表示一个服务端验证、平台管理的外部企业连接/应用安装实例。它可以先于 Channel
创建，也可以在没有聊天入口时用于目录同步、管理员预绑定，或未来的 Web OAuth/SSO adapter；因此
核心身份服务不能把 Channel 当成 Provider Account 的所有者。

Channel 是该企业连接的一个消费者。首期通过独立的
`IdentityProviderChannelLink` 建立 tenant-safe、provider-safe 的一对一引用：

```text
IdentityProviderAccount
  UNIQUE(id, tenant_id, provider)

IdentityProviderChannelLink
  UNIQUE(provider_account_id)
  UNIQUE(channel_id)
  FOREIGN KEY(provider_account_id, tenant_id, provider)
    -> IdentityProviderAccount(id, tenant_id, provider) ON DELETE RESTRICT
  FOREIGN KEY(channel_id, tenant_id, provider)
    -> ChatChannel(id, tenant_id, channel) ON DELETE RESTRICT
```

数据库同时拒绝跨 Tenant、跨 Provider、一个 Channel 关联两个 account，以及首期一个 account 关联
多个 Channel。后一条是一对一的首期保守约束：当前飞书 `app_secret` 仍由 `ChannelSecret` 持有，
在通用 Provider credential vault 落地前，不允许多个 worker/Channel 猜测或共享同一个 account 的
凭据。将来需要一对多必须另立 ADR、迁移凭据所有权并补并发、轮换和撤销门禁，不能只删除唯一约束。

EIM-I2.1 只完成持久化关系解耦。凭据关系仍是
`IdentityProviderAccount -> IdentityProviderChannelLink -> ChatChannel -> ChannelSecret`；这不是
Provider credential 已与 Channel 解耦，也不支持无 Channel account 调用 Provider API。I4 在通用
凭据库出现前必须沿唯一 link 精确取得 ChannelSecret；找不到、多条或 scope 不一致一律 fail closed，
不得按 `app_id` 猜测 Channel。

目标架构增加 `CustomerOrganization` 术语，表示真实客户企业/合同与治理边界。首期保持
`CustomerOrganization 1:1 Tenant`，只存在于架构语义，不新增表、claim、API 或运行时路由；
`IdentityProviderTenant` 仍直接固定到 `Tenant`。未来集团级多 Tenant 由新 ADR 决定
Organization、Tenant 和 Provider Tenant 的关系，不能借本 ADR 预埋动态路由。

MCP 规范、MCP Python SDK 2 和 FastMCP 4 都不定义 MultiRAG 的 Customer Organization、Provider
Account、Channel 或其数据库 schema。主流企业平台可借鉴的是“稳定企业连接/凭据资源与消费入口、
授权资源分层”的职责边界，不是某套可直接复制的统一表结构；本领域模型继续保持框架无关。

MultiRAG 仍按仓内 `port-ragflow-commit` Skill 对本地 RAGFlow 上游逐 commit 跟进。本轮只新增
加法式表、约束和迁移，不为假想 Git 冲突搬移 `api/db/db_models.py` 中的模型；遇到真实上游 commit
时再由 Skill 的语义移植、契约测试和适配层规则处理。
