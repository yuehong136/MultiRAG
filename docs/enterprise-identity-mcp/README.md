# 企业身份接入与 MCP 授权项目 · 权威入口

> 项目代号：**EIM**（Enterprise Identity & MCP Authorization）
> 建立日期：2026-08-07
> 适用仓库：`/Users/xldu/project/multirag`、`/Users/xldu/project/of/of_mcp`
> 外部事实核验日期：2026-08-09（版本与上游提交见 [VERSION_BASELINE](VERSION_BASELINE.md)）

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
6. 改 of_mcp 时先完整读取 `/Users/xldu/project/of/of_mcp/AGENTS.md`，最终必须执行
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
  - Principal / scope / audit middleware
  - business service adapters
```

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
- MCP 客户端仍使用旧式 `ClientSession + initialize` 和静态 headers，需要升级为请求级 token
  与 MCP SDK 2 客户端。
- Channel Execution 已通过 `stream()` 直接向 transport-neutral ReplySession 交付类型化事件；
  飞书已实现 CardKit 渐进式回复，`ask()` 只保留为兼容聚合入口。下一项执行层缺口不是 transport，
  而是把 Provider/Target capabilities 与 Dialog/Canvas 各自的提交策略按
  [执行架构](../channel-program/EXECUTION_ARCHITECTURE.md) 收口。
- 当前 `IncomingMessage`/`OutgoingMessage` 不表达 thread、mention、attachment、reference、
  card handle 或 delivery UUID；这些目标契约统一见 [FEISHU_BOT_UX](FEISHU_BOT_UX.md)。

### of_mcp

- 当前 `oauth_enabled=false`，gateway 没有认证。
- `house_middleware()` / `house_extensions()` 是 P3 平台能力装配点。
- `service.toml` 已声明 `scopes`，但尚未形成真实请求授权。
- `medic` 的 `workcode` 是工具调用者自报，且工具会产生真实副作用。
- of_mcp 使用 FastMCP 4.0.0b1；2026-08-07 已有 b2，必须先做独立兼容升级，不能和身份改造
  混在同一个 PR。

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
