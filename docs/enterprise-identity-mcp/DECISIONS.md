# EIM 架构决策记录

本文件只记录已经定案、后续实现不得擅自改变的决策。任务状态见 [ROADMAP](ROADMAP.md)。
确需改变某项决策时，追加新的 ADR 取代旧项，禁止直接改写历史结论。

---

## EIM-ADR-01：一个企业客户映射一个 MultiRAG Tenant

**状态**：Accepted
**日期**：2026-08-07

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
