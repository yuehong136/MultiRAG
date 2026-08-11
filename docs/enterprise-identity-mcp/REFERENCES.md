# 外部项目参考矩阵

本文件回答两个问题：后续开发应该去哪个项目看什么；哪些设计不能照搬。上游提交快照见
[VERSION_BASELINE](VERSION_BASELINE.md)。引用源码前先确认许可证和当前提交；默认以“读设计、
按本仓接口重写”为主，不整目录复制。

---

## 1. 参考优先级

1. 飞书开放平台官方文档和官方 SDK：协议、权限、ID 语义、事件、传输安全。
2. MCP 官方规范、SDK 和 ext-auth：MCP wire/auth 语义。
3. 飞书官方 OpenClaw 插件：飞书 AI 交互、卡片、确认和风险经验。
4. DeerFlow：平台账户绑定、Channel connection、用户隔离和 Agent gateway。
5. MultiRAG/RAGFlow 现有实现：本项目控制面、worker 和上游可移植性。
6. LangBot、lark-channel：多 Provider 与交互细节。

如果第三方项目与官方规范冲突，以官方规范为准；如果外部项目与本项目
[DECISIONS](DECISIONS.md) 冲突，以本项目决策为准。

---

## 2. 飞书官方 Channel SDK

项目：[`larksuite/channel-sdk-python`](https://github.com/larksuite/channel-sdk-python)

### 后续任务应该参考

| 目标 | 精确参考位置 | 用法 |
|---|---|---|
| SDK 迁移 | `docs/migration-from-lark-oapi.md` | EIM-C5 的 import、ws/webhook 迁移清单；不作为 U0/U1 依赖 |
| 生产安全 | `docs/security.md` | `compat -> audit -> strict`、签名、WS 限额、安全文本和审计 recorder |
| 高层接口 | `docs/reference.md` | typed inbound、policy、public lifecycle、send opts、reaction、media 和 Reply/stream 能力 |
| 流式卡片 | `docs/cardkit-streaming.md` | EIM-U1 的 CardKit 创建、sequence、patch、最终 finish 与错误状态 |
| 流式刷新调度 | `lark_channel/channel/outbound/streaming/markdown_stream.py`、`throttle.py`、`update_queue.py` | EIM-U10 的 producer 非阻塞、定时触发、最多 1 running + 1 latest pending 和终态 drain |
| 去重 | `docs/dedup-architecture.md` | 对照现有 Redis 去重，确定 SDK 去重与平台去重的唯一责任边界 |
| 对话入口 | `lark_channel/__init__.py` 公开导出 | 只从稳定 public API import，不依赖内部模块 |
| CardKit OpenAPI | `lark_channel/api/cardkit/v1/` | 卡片服务的 typed request/response；不要手拼 HTTP |
| Contact V3 类型 | `lark_channel/api/contact/v3/` | 只用于判断独立 SDK 是否够用；完整 OpenAPI 仍以 `lark-oapi` 为主 |

### 应吸收的设计

- Channel transport 和完整 OpenAPI 分包共存；
- 回调规范化、policy、safety、outbound 和 card callback 是 Provider 内部能力；
- strict mode 与审计 recorder；
- WebSocket 资源上限和安全文本渲染；
- 卡片 streaming 的明确生命周期。
- `InboundMessage` 对 thread、mention、resources、safe text 的结构化表达；只映射到本项目 DTO，
  不让 SDK 对象穿透执行域。

### 不能照搬

- 该仓库较新，官方后继定位不等于生产成熟度证明；先按 EIM-C5 做 PoC，不以 star、版本号或
  “官方”标签替代项目实测；
- SDK 内置 policy 不能替代 MultiRAG tenant/binding/Principal；
- SDK 内存 token/dedup store 不能自动替代现有 Redis 多进程状态；
- SDK 的 `InboundMessage` 不是可信身份；
- 不把 SDK 对象穿透到 Agent/Identity domain。

---

## 3. 飞书完整 OpenAPI SDK

项目：[`larksuite/oapi-sdk-python`](https://github.com/larksuite/oapi-sdk-python)

### 后续任务应该参考

- `lark_oapi/api/contact/v3/`：按 `open_id` 获取单个用户、User/Status typed model；
- `lark_oapi/api/im/v1/`、`lark_oapi/api/cardkit/v1/`：EIM-U1 在不迁移 transport 的情况下实现
  reply、uuid、reaction、卡片创建/更新/完成和媒体资源；
- SDK Client 的 tenant access token 自动管理；
- v2 event dispatcher 的 `contact.user.*_v3` 事件类型；
- SDK 示例仓中的“获取部门用户”和机器人 quickstart，仅用于 API 调用形状。

### 不能照搬

- `lark_oapi.channel` 已是兼容期 legacy Channel 入口，新功能在独立
  `lark-channel-sdk`；不要新建依赖这个高级 legacy 入口的代码。现有底层 `lark_oapi.ws.Client`
  适配在 C5 PoC 未通过前继续保留，不为追逐新包而强制迁移。
- SDK 只托管 `tenant_access_token`；未来飞书用户委托资源所需的 `user_access_token` 必须进入
  独立加密 token vault，不能塞进 Channel secret 或普通配置。

---

## 4. 飞书官方 OpenClaw 插件与 OpenClaw 主项目

项目：

- [`larksuite/openclaw-lark`](https://github.com/larksuite/openclaw-lark)
- [`openclaw/openclaw`](https://github.com/openclaw/openclaw) 的
  `docs/channels/feishu.md` 和生产 Feishu channel

这是飞书 AI 体验最值得参考的实现，但它的 README 明确警告用户身份授权、prompt injection、
群聊滥用和数据泄漏风险。它更接近“私人 Agent + 飞书资源工具”，不能作为企业共享权限模型。

### 后续任务应该参考

| 目标 | 精确参考位置 | 用法 |
|---|---|---|
| 流式回复控制 | `src/card/streaming-card-controller.ts`、`flush-controller.ts` | 卡片节流、最终 flush、失败收口 |
| 工具过程展示 | `src/card/tool-use-display.ts`、`tool-use-trace-store.ts` | 只借鉴状态模型，不泄露原始工具参数 |
| 卡片回复调度 | `src/card/reply-dispatcher.ts` | queued/running/final/error 生命周期 |
| 敏感确认 | `src/core/card-action-operator.ts`、相关 `tests/card-action-operator.test.ts` | 一次性确认、操作人绑定、回调验证 |
| 入站去重 | `src/messaging/inbound/dedup.ts` | 对照飞书重复投递，平台仍以 `message_id` 做最终去重 |
| Channel 队列 | `src/channel/chat-queue.ts` | 同会话顺序和快速 follow-up 缓冲 |
| 事件与交互 | `src/channel/event-handlers.ts`、`interactive-dispatch.ts` | 消息与 card action 分流 |
| 安全检查 | `src/core/security-check.ts`、`src/core/auth-errors.ts` | 错误分类和用户可见安全文案 |
| 飞书 OAuth UI | `src/tools/oauth*.ts` | 仅用于未来飞书文档/日历用户授权，不用于聊天身份确认 |
| 主项目能力矩阵 | `openclaw/docs/channels/feishu.md` | typing reaction、streaming fallback、媒体上限、群 mention policy、话题 hydration |

### 不能照搬

- 不用某个用户的飞书 `user_access_token` 代表整个共享机器人；
- 不把飞书文档/日历权限等同于 MCP 业务权限；
- 不把 prompt、群配置或 allowlist 当企业授权事实来源；
- 不在 MultiRAG 首期开放群聊高风险工具。群聊必须在 EIM-U2 身份管理与 EIM-U3 风险评审后启用。

---

## 5. ByteDance DeerFlow

项目：[`bytedance/deer-flow`](https://github.com/bytedance/deer-flow)

它是与本项目最接近的“AI 平台用户 + IM Channel”参考。

### 后续任务应该参考

| 目标 | 精确参考位置 | 用法 |
|---|---|---|
| IM 身份绑定 | `backend/docs/IM_CHANNEL_CONNECTIONS.md` | `link_only`、一次性 connect code、账号解绑和连接状态 |
| 身份设计 | `backend/docs/AUTH_DESIGN.md` | 浏览器用户、OIDC、IM binding、internal auth 的边界比较 |
| 外部身份解析 | `backend/app/channels/connection_identity.py` | Channel 身份查 connection，不把原始 ID 当 owner |
| Channel connection API | `backend/app/gateway/routers/channel_connections.py` | link code 生命周期与路由授权 |
| 持久化模型 | `backend/packages/harness/deerflow/persistence/channel_connections/` | 唯一约束、active connection 和迁移测试 |
| 飞书 transport | `backend/app/channels/feishu.py`、`feishu_run_policy.py` | 单张 running card 持续 patch、reaction 和 run policy |
| 快速追问 | `backend/AGENTS.md` 描述的 follow-up buffer/状态机 | 同话题消息 queued -> running -> final、容量上限和来源预览 |
| Principal/授权 | `backend/packages/harness/deerflow/authz/principal.py`、`enforcement.py`、`tool_filter.py` | request-scoped Principal 与工具可见性 |
| MCP OAuth | `backend/packages/harness/deerflow/mcp/oauth.py` | 客户端 OAuth 缓存和错误流，仅作对照 |
| 中间件顺序 | `backend/docs/middleware-execution-flow.md` | 认证必须早于授权、审计和工具执行 |

### 应吸收的设计

- 外部身份链接到已注册平台用户，而不是直接成为平台用户；
- `channel_connections` 和 conversation/thread 分表；
- 浏览器绑定码适合通用 `link_only`；
- owner 级线程、文件、Memory 隔离；
- Channel worker 只能通过内部认证调用平台 gateway。
- 一次执行只维护一张可恢复的回复卡片，后续消息有独立 queued/running/final 状态。

### 不能照搬

- DeerFlow Internal Auth 明确只验证共享平台 token，不验证 owner ID 是否真实；本项目不能把
  `X-Owner-User-Id` 这种 Header 当最终身份证明。
- 原力 `jit` 模式优先通过飞书 Contact V3 验证，不要求每个员工先打开 Web 输入绑定码。
- 不把 channel user ID 暴露到 sandbox 环境变量或任意工具；只有受控 Principal dependency
  能读取企业主体。
- 不把其共享 internal auth 对 `channel_user_id` 的信任前提移植到 MultiRAG；本项目仍必须经
  binding-scoped workload 身份和 EnterpriseIdentityService 验证。

---

## 6. LangBot

项目：[`langbot-app/LangBot`](https://github.com/langbot-app/LangBot)

### 后续任务应该参考

- `src/langbot/pkg/platform/sources/lark.py`：Provider adapter 最小接口、非阻塞 WebSocket，以及
  CardKit `print_step` / `print_frequency_ms` / `fast` 流式打印配置；
- `src/langbot/pkg/platform/sources/lark.yaml`：Provider 声明式元数据；
- `tests/unit_tests/platform/test_lark_adapter.py`：事件到平台消息契约测试；
- `tests/unit_tests/platform/test_lark_ws_nonblocking.py`：SDK callback 不阻塞；
- `src/langbot/pkg/api/http/authz.py`：管理 API 授权分层；
- `docs/API_KEY_AUTH.md`：面向平台操作者的 API key 文档组织方式。

### 不能照搬

- 多平台 allowlist、限流是 Channel policy，不是企业身份或业务权限；
- LangBot MCP 管理 API key 不适合作为每位员工的 on-behalf-of token；
- 不在前端或 YAML 中硬编码 Provider 身份字段，MultiRAG ProviderSpec 仍是单一真源。

---

## 7. shareAI-lab/lark-channel

项目：[`shareAI-lab/lark-channel`](https://github.com/shareAI-lab/lark-channel)

参考 `src/lark.ts`、`src/reply.ts` 和 README 中的：

- 每群/每线程会话隔离；
- streaming card 的块级展示；
- 单 patch 在途、待发快照 latest-value 覆盖和 final flush；
- reaction 表达 queued/running/done/error；
- follow-up 保持在原线程。

不能照搬其本地工作区、Claude Code 权限模式和文件系统会话作为企业平台安全边界；该项目的
allowlist 不能替代本项目 identity resolver。

---

## 8. MCP 官方实现

核心规范与 SDK：

- [MCP 2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [MCP 2026-07-28 release](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [MCP 2026-07-28 Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [`modelcontextprotocol/python-sdk`](https://github.com/modelcontextprotocol/python-sdk) 与
  [SDK v2 What's New](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md)
- [MCP Authorization](https://modelcontextprotocol.io/docs/tutorials/security/authorization) 与
  [Authorization extensions](https://modelcontextprotocol.io/extensions/auth/overview)

官方扩展/提案：

- [`modelcontextprotocol/ext-auth`](https://github.com/modelcontextprotocol/ext-auth) 与
  [Enterprise-Managed Authorization](https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization)
- [`modelcontextprotocol/ext-apps`](https://github.com/modelcontextprotocol/ext-apps)
- [`modelcontextprotocol/ext-tasks`](https://github.com/modelcontextprotocol/ext-tasks) 与
  [SEP-2663 Tasks extension](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2663)
- [SEP-2322 Multi-Round Tool Results](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2322)
- [SEP-2575 stateless protocol](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2575)
- [IETF ID-JAG draft](https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-assertion-authz-grant/)

FastMCP 实现/产品资料（不是 MCP 标准）：

- [FastMCP MultiAuth](https://gofastmcp.com/servers/auth/multi-auth)
- [FastMCP token verification](https://gofastmcp.com/servers/auth/token-verification)
- [FastMCP Remote OAuth](https://gofastmcp.com/servers/auth/remote-oauth)
- [FastMCP component authorization](https://gofastmcp.com/servers/authorization)
- [FastMCP 4.0.0b2 `JWTVerifier` source](https://github.com/PrefectHQ/fastmcp/blob/v4.0.0b2/fastmcp_slim/fastmcp/server/auth/providers/jwt.py)
- [Prefect Horizon](https://gofastmcp.com/deployment/prefect-horizon)
- [FastMCP PyPI releases](https://pypi.org/project/fastmcp/#history)

### 后续任务应该参考

| 目标 | 官方内容 |
|---|---|
| MultiRAG SDK v2 客户端 | Python SDK `docs/whats-new.md`、migration、Client、OAuth for clients |
| resource metadata | [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) well-known、`WWW-Authenticate resource_metadata=...` |
| audience/resource | [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707) Resource Indicators 和 server audience validation |
| 项目 JWT profile | [RFC 9068](https://www.rfc-editor.org/rfc/rfc9068) 的 `at+jwt`、标准 access-token claims 和非对称签名；只称 RFC 9068-shaped，不声称 MCP 强制 JWT |
| 用户/actor 委托 | [RFC 8693](https://www.rfc-editor.org/rfc/rfc8693) 的 `act` 语义；顶层 `sub` 保持用户，内部 token 另做 resource/scope attenuation |
| 禁止 token passthrough | MCP Security Best Practices |
| 机器到机器 | `io.modelcontextprotocol/oauth-client-credentials` extension |
| 企业 IdP | `io.modelcontextprotocol/enterprise-managed-authorization`、[SEP-990](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/990)、ID-JAG |
| 现代无会话协议 | 2026-07-28 发布说明和 SDK v2 protocol compatibility |
| 缺参后恢复工具调用 | MRTR/SEP-2322 的 `input_required` 与后续 `inputResponses`；映射到 EIM-U14，不替代授权/持久化 |
| 富 UI | `ext-apps` 的 host/UI resource 协议；host 不支持时不能假设可渲染 |
| 长任务实验 | 2026 发布说明、SEP-2663、`ext-tasks` README 与 Python SDK 实现状态交叉核验，按最保守状态采用 |

### 采用边界（截至 2026-08-12）

| 概念 | 来源层级 | 本项目结论 |
|---|---|---|
| MCP 2026 + MRTR | 核心协议/最终 SEP | 采用；U14 自己持久化 pause/resume，MRTR 只承载跨请求补充输入 |
| EMA | 官方稳定 auth extension | 有真实企业 IdP、多 issuer 需求时才启动 A8；Feishu `open_id/user_id` 不是 assertion |
| ID-JAG | IETF draft，EMA 的底层工作项 | 实施前重核 draft、库和威胁模型；不把未定稿字段写死为内部主身份 |
| MultiAuth | FastMCP server 实现 | 可作为多 issuer 组合候选；不写入跨框架 domain contract，也不能代替 tool/business policy |
| Horizon | Prefect/FastMCP 托管产品 | 只作 hosting/auth/registry 的 build-vs-buy；不是“企业 MCP 服务中心”的标准架构依赖 |
| MCP Apps | 官方 extension，依赖 host 支持和隔离 UI | 飞书 CardKit 不是 Apps host；U15 直接走飞书官方 CardKit callback，未来 Web host 再评估 Apps |
| Tasks | 官方材料状态尚未完全收敛：发布说明/SEP 指向 extension，当前 `ext-tasks` README 仍标 experimental/not official，Python SDK v2 尚未实现 | 按实验性处理；不阻塞 U14/U15、Confirmation Store 或 durable run ledger |

### 不能照搬或误解

- OAuth Client Credentials 只证明机器 client，单独使用不能代表当前员工；
- EMA 需要真实企业 IdP Identity Assertion，不能把飞书事件字段伪造成 ID Token/SAML；
- MCP auth 只定义 transport/resource 授权，不替代 medic/Jira 的业务对象授权；
- legacy `initialize` 和 session sticky 只能作为迁移兼容，不是新架构目标。
- MRTR `input_required` 不是“已获用户确认”；飞书表单提交也不是 authorization decision。
- MultiAuth/Horizon 是 FastMCP 生态能力，不得写成 MCP 标准或升级 Python SDK v2 后自动获得的能力。
- Tasks 和 Apps 都不能替代本项目的 Principal、scope、业务授权、幂等和持久化 interaction state。

### FastMCP 4 的采用边界

- `RemoteAuthProvider` 适合在 EIM-A3 组合 token verifier 与 RFC 9728 protected-resource metadata；
  `require_scopes`/component auth 适合在 A4 同时过滤 `tools/list` 和保护 direct call。
- `JWTVerifier` 可以复用成熟 JOSE/JWKS、issuer/audience 和基础 scope 检查，但 4.0.0b2 的实现不是
  EIM-A1 profile oracle；项目仍需检查 `typ/kid`、必需 claims/types、`iat/nbf/max_ttl`、
  `token_use`、tenant、scope registry 和 cross-profile/resource。
- MultiAuth 只有出现多个真实 token issuer 后才评估；“第一个 verifier 成功”不等于 tenant、角色、
  业务对象或 assurance 已授权。
- 领域 Principal、policy 和业务 service 不 import FastMCP。auth/provider 对象只存在 composition root
  与 tool adapter 边界，避免框架升级改写业务契约。

### 企业 MCP 平台的可复用模式

| 项目 | 参考内容 | 本项目采用/拒绝 |
|---|---|---|
| [agentgateway](https://agentgateway.dev/docs/standalone/latest/configuration/security/mcp-authz/) | MCP JWT/resource policy、工具可见性、backend credential exchange | 采用 gateway 验证、`tools/list`/direct call 同策略、resource-bound 下游换发；不透传外部 bearer |
| [IBM ContextForge](https://ibm.github.io/mcp-context-forge/latest/manage/rbac/) | resource visibility 与 RBAC 双层授权、server-side membership | 采用“可见不等于可执行”和 fail closed；不把易变 team/role 复制进短 token |
| [LibreChat MCP authority](https://github.com/danny-avila/LibreChat/blob/d89b11d34dce834ed600c48e2f2856500c806af3/packages/api/src/mcp/authority/index.ts) | Principal/resource credential 隔离、执行前 authority proof | 采用 per-principal/per-resource 隔离和 TOCTOU 重验；不照搬其应用数据模型 |
| [Open WebUI tool grants](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/tools.py) | user/group grants 在工具暴露前过滤 | 采用服务端工具可见性思想；group grant 不能替代 MultiRAG tenant 和业务对象授权 |

这些项目用于校准网关/策略/凭据隔离，不构成引入另一个 MCP 平台的决定。MultiRAG 继续拥有
Principal 与 Agent/知识库权限，of_mcp 继续拥有 Resource Server、工具策略和业务 adapter；任何
平台的 unsigned identity header、全局 server credential 或 UI 可见性都不能作为最终授权证据。

---

## 9. MultiRAG 与 RAGFlow 上游兼容边界

这里的 `RAGFlow` 只表示外部源码来源和兼容基线。本仓中的表、函数、方法、路由和运行时一律称为
MultiRAG 实体；不得把外部项目名当作本仓运行时组件名。

必须保留并优先复用：

- `api/channels/`：managed worker、传输边界、队列、去重、回复；
- `api/channel_control/`：tenant-owned channel、binding、Secret、ProviderSpec；
- `api/channel_execution/`：不可信 command 到 trusted context 的唯一提升边界；
- `api/channel_runtime/`：supervisor/worker 私有契约；
- `common/channel_secret_crypto.py`：Channel credential 加密和密钥环；
- `api/db/db_models.py::User/UserTenant`：现有平台用户与租户成员关系。

不要为了采用外部项目而替换现有控制面。外部参考只填补 transport、身份链接、授权和 UX
能力；MultiRAG 已经具备更适合多租户产品的 managed binding 和 Secret 生命周期。

---

## 10. 对话执行与重新生成的最新参考

本节只记录形成 MultiRAG 决策所需的外部事实；最终架构以
[`EXECUTION_ARCHITECTURE`](../channel-program/EXECUTION_ARCHITECTURE.md) 为准。
RAGFlow 是 MultiRAG Canvas/Agent/Channel 持续迭代的主要上游；DeerFlow、LangGraph、Open WebUI、
Vercel AI SDK 等项目用于校准 terminal publish、run/history 分离、checkpoint、幂等和副作用边界，
不构成另起框架替换 RAGFlow Canvas 的授权。具体收敛规则见
[CHN-ADR-08](../channel-program/DECISIONS.md#chn-adr-08--canvas-与-channel-演进以上游同步为主只在适配层吸收现代执行不变量)。

### RAGFlow 上游

核验快照：`b5bffa0fa3213bbc0fee046422c7de4a3db2e39c`。

- Dialog Chat API 支持客户端显式传完整历史；`store_history_messages=false` 时不创建/更新持久化
  会话，并要求 `pass_all_history_messages=true`；
- Web 侧重新生成会截断到目标问题之前，再提交原问题和明确的截断历史，包括空历史；
- Canvas completion 仍会自行读取/创建 `API4Conversation`，并在流结束后 append message、reference、
  DSL 和 errors，尚无对等的 no-store 参数。

因此 MultiRAG 不应让 Dialog 永久承担 Canvas 的候选写放大，也不能在 Canvas 无替代端口时直接删除
隔离候选。参考：

- [`chat_api.py`](https://github.com/infiniflow/ragflow/blob/b5bffa0fa3213bbc0fee046422c7de4a3db2e39c/api/apps/restful_apis/chat_api.py#L1225-L1397)
- [`logic-hooks.ts`](https://github.com/infiniflow/ragflow/blob/b5bffa0fa3213bbc0fee046422c7de4a3db2e39c/web/src/hooks/logic-hooks.ts#L729-L749)
- [`canvas_service.py`](https://github.com/infiniflow/ragflow/blob/b5bffa0fa3213bbc0fee046422c7de4a3db2e39c/api/db/services/canvas_service.py#L357-L426)

### DeerFlow 与 LangGraph

DeerFlow 当前把 run 与 thread 分开：run 有独立状态、owner lease、cancel request，并用数据库条件
更新解决取消与成功终态竞争；数据库部分唯一索引保证一个 thread 最多一个 active run。重新生成先
准备 checkpoint/input，再创建新 run，不把旧问题当普通 follow-up 追加。

LangGraph 官方把 thread state 保存为 checkpoints，replay 从指定 checkpoint 之后重新执行；官方也
明确说明 replay 会再次触发后续 LLM、API 和 interrupt。因此 MultiRAG 只借鉴 run/thread 分离、
checkpoint/CAS 和恢复边界，绝不把未知副作用图的 replay 当安全重新生成。

- [DeerFlow run model](https://github.com/bytedance/deer-flow/blob/17531d7c118d6111b863f945ff910a7889a235b0/backend/packages/harness/deerflow/persistence/run/model.py)
- [DeerFlow regenerate API](https://github.com/bytedance/deer-flow/blob/17531d7c118d6111b863f945ff910a7889a235b0/backend/app/gateway/routers/thread_runs.py#L814-L833)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

### Open WebUI

Open WebUI 当前用独立 chat message 表的 `parent_id` 重建 `childrenIds`，能表达真正的消息分支。
MultiRAG 目前 Dialog/Canvas 是线性公开会话，因此只采用“新 run + 替换最新完成尾轮”的产品语义，
不提前引入只有 Channel 理解的半套分支 DAG。

- [Open WebUI chat message model](https://github.com/open-webui/open-webui/blob/01f4282f1ffe0d6212f58d3afbeae21fffd0c4be/backend/open_webui/models/chat_messages.py#L130-L195)

### Vercel AI SDK

官方持久化指南把完整消息保存放在流 `onFinish`，并建议持久化场景使用服务端消息 ID；这支持
MultiRAG “流式展示不等于已提交历史、终态才发布”的选择。其恢复流文档同时明确 abort 与 resume
存在取舍，所以 MultiRAG 不把取消和跨进程恢复混成一个首期功能。

- [Chatbot Message Persistence](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-message-persistence)
- [Chatbot Resume Streams](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-resume-streams)

### 飞书官方边界

飞书仍只负责 Provider 交互：CardKit 单卡流式更新、关闭 streaming 后再进入终态交互、回调快速
确认和耗时逻辑异步执行。它不拥有 MultiRAG Dialog/Canvas 历史事务。

- [流式更新卡片](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview)
- [接收并处理回调](https://open.feishu.cn/document/event-subscription-guide/callback-subscription/receive-and-handle-callbacks)

## 11. 引用与复制规则

- 参考代码前固定上游 SHA，并在 PR 描述中写明路径和许可证。
- 复制代码时保留原许可证要求和 notices；能按接口重写就不复制。
- 外部测试思路可以重写成符合本仓测试基建的契约测试。
- 不复制外部项目的 Secret、示例 token、默认宽权限配置或安全降级开关。
- 上游 main 漂移后，先对比基线 SHA，不要让同一 `EIM-*` 任务在执行中途换参考版本。
