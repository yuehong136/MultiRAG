# 飞书企业应用申请与接入清单

本文件是管理员、开发和运维共同使用的交付清单。飞书控制台名称可能调整；权限 scope ID 和 API
文档链接最近于 2026-08-09 核验，实施当天仍须在具体 API/事件页面重新确认。

---

## 1. 首期应用类型

首期选择：**企业自建应用 + 机器人能力 + 长连接**。

理由：

- 飞书官方推荐已集成 SDK 的企业自建应用使用长连接；
- 不需要公网 callback URL；
- SDK 封装建连鉴权；
- 适合当前“一家企业一个飞书应用账户”的部署形态；
- Contact `user_id` 字段权限对自建应用可用。

飞书官方限制：长连接只支持企业自建应用；每个应用最多 50 个连接；多个客户端收到的是集群
分发而非广播；回调必须在 3 秒内完成。因此 SDK handler 只能规范化并投递现有有界队列。

官方文档：[使用长连接接收事件](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case?lang=zh-CN)

未来若发布飞书商店应用：单独立项使用 Webhook、安装事件、商店 app/tenant token 流程和每租户
数据范围；不要在首期自建应用代码里提前混入商店分支。

---

## 2. 谁需要参与

| 角色 | 必须提供/确认 |
|---|---|
| 飞书企业管理员 | 创建/审批应用、权限、可用范围、通讯录数据范围、发布版本 |
| AI 平台管理员 | Customer Organization/Tenant（首期 1:1）、Provider Account、Channel link、JIT/link policy、默认 NORMAL 角色 |
| HR/OA 负责人 | `employee_no` 是否稳定、是否等于 talent_id/workcode、离职同步延迟 |
| 安全/基础设施 | issuer/resource 域名、TLS、JWT signing key/JWKS、密钥轮换和审计留存 |
| of_mcp 服务负责人 | 每个 service 的 audience、scope、业务授权接口、下游服务账户 |
| medic/Jira 负责人 | 测试环境、真实接口凭据、幂等字段、撤销/回滚能力、审批规则 |

没有 HR/OA 对工号语义的书面确认时，只能完成 `open_id -> user_id -> platform_user_id`，不能
宣称已经得到可靠 `talent_id`。

---

## 3. 创建应用并获取凭据

入口：[飞书开放平台开发者后台](https://open.feishu.cn/app)

操作：

1. 进入目标企业，创建“企业自建应用”。
2. 在“添加应用能力”中启用“机器人”。
3. 进入“凭证与基础信息”。
4. 获取：
   - `App ID`，格式通常为 `cli_...`；
   - `App Secret`。
5. 在 MultiRAG 身份控制面完成企业/安装实例所有权验证后，创建独立 Feishu Provider Account，固定
   已验证的 Tenant、provider、`tenant_key/app_id`。该 account 可以先于 Channel 存在，但在首期凭据
   仍归 Channel 的过渡期，无 Channel link 时不能调用飞书 API。EIM-I3 只提供
   `VerifiedOwnershipRepository` 的两个窄持久化命令；它不是所有权证明来源，也不是可直接调用的
   管理 API。
6. 为目标 Tenant 显式创建唯一 `IdentityTenantPolicy`，同时确定
   `preprovisioned/link_only/jit` 与 link code TTL（60～900 秒，首期建议显式写 600）。缺 policy
   必须 fail closed；resolver 不提供默认值或自动 backfill。后续 mode/TTL 只能一起用 expected revision
   CAS 更新。EIM-I6 已提供最小权限 admin repository port，但 HTTP/UI/onboarding composition 尚未
   接线，生产 onboarding 不得用人工 SQL 或把 admin port 暴露给普通 Channel 请求代替。
7. 在 MultiRAG Channel 管理端创建 Feishu Channel，由现有 Channel Secret 写入流程保存
   `app_secret`；不得写进 Git、普通 YAML、日志、截图、聊天记录或本文档。
8. 创建 tenant/provider-safe 的 `IdentityProviderChannelLink`。首期一个 account 和一个 Channel
   都只能各有一条 link；任一端 scope 不一致就终止 onboarding，不自动纠正或重建。

凭据属性：

| 值 | 是否 Secret | 保存位置 | 备注 |
|---|:---:|---|---|
| `app_id` | 否 | 独立 Provider Account | 仍不应在普通日志完整输出 |
| `app_secret` | 是 | MultiRAG `ChannelSecret` 加密存储 | 只写不读回，支持主密钥环 |
| `tenant_access_token` | 是 | I4 Provider 有界进程 cache；官方 Auth V3 generated async request/resource/transport + strict live top-level adapter | 不由管理员复制，不持久化到 Channel/identity 表 |
| `tenant_key` | 敏感标识 | ExternalIdentity/provider account | 来自可信事件/安装上下文，不接受用户消息覆盖 |
| verification token | 是 | 仅 Webhook 模式需要 | 长连接首期不申请/不配置 |
| encrypt key | 是 | 仅 Webhook 加密模式需要 | 长连接首期不申请/不配置 |

Channel 建立 link 后使用现有“测试连接”能力验证 `app_id/app_secret`，但该验证只证明应用凭据有效，
不证明通讯录字段权限、数据范围和事件订阅都正确。

当前管理 API/界面可能仍按 Channel-first 顺序创建配置；EIM-I2.1 只落数据库关系，EIM-I3 只落解析
seam，EIM-I6 虽已落权威 policy/admin port 和 provisioning transaction，但仍没有管理 HTTP/UI。
EIM-I4 已在 `api.identity_adapters` 增加一个过渡 credential resolver：
它只从精确 Provider Account 出发，沿唯一 tenant/provider-safe link 读取 Channel 公开 `app_id/domain`
与加密 `ChannelSecret`，用注入 `SecretStore` 解密，并把 Secret version 作为 credential generation。
它不提供管理 API，不创建/rebind link，不在无 link account 上调 Provider API，也不改变
`ChannelSecret` 仍是当前 credential owner 的事实。

因此，当前仍不能用人工 SQL、直接实例化 privileged repository 或普通 Channel update 代替
正式 onboarding，也不得按 `app_id` 猜测 Provider Account 和 Channel 的关系。控制面编排、
外部 ownership verification 和通用 Provider credential vault 仍未完成。

I4/I7 组合代码接入 I3 时仍必须按 capability 注入：只有目录/account 控制路径能拿 health/scope/
event CAS，ownership 只给 onboarding。I6 的完整 provisioning use-case port 自己拥有 fresh async
transaction，普通 resolver 不得取得 policy admin capability，也不能为了方便把完整 repository 暴露
给所有路径。

I4 Provider 当前按以下顺序验证 credential 与企业边界：官方 Auth V3 generated async
request/resource/transport + strict live top-level response adapter -> 官方 Tenant V2 typed nested
`tenant_key` 精确匹配 -> 官方 Contact V3 typed nested 单用户查询。Tenant/Contact
显式传递 project-scoped token，不走 SDK 同步/global TokenManager cold path；项目层按
account revision/scope marker/Secret version/domain 做有界 cache 和 single-flight。这只解决目录验证；
I6 另在数据库事务中重新锁 policy/account generation，所有阻塞锁后以 PostgreSQL
`clock_timestamp()` 重验 proof 的 5 分钟窗口与 code expiry，再原子写 identity/user/event。

`lark-oapi==1.7.2` 生成的 Auth response model 期待 `data`，但 live 成功响应的 token/expire 位于
顶层。I4.1 已针对该 live shape 修正 production adapter，同时继续使用官方 async transport/签名；
production adapter sandbox 与全量门禁均已完成。

三个 endpoint 都保留 HTTP status + business code envelope；非 2xx + code 0 仍按失败。Auth/Tenant
是控制面，不会把 403/404 解释成 identity not-found/not-in-scope；只有 Contact 查询面可以。业务码
按 endpoint stage 分类：`10003` 不是 credential code，Auth credential mismatch 当前为
`10015/20002`。Contact 只有 code 0 才允许 HTTP 403/404 fallback；未知/瞬时非零 code 不能
降级成 link/JIT 可消费的 identity miss。

先前 sandbox 将 Python 源码与 credential 共用 stdin，脚本实际读到空参数；由此得到的记录全部
作废，不能用于判断 credential 或权限。

修正 production adapter 后，使用用户批准的测试应用完成了真实三步 sandbox，只保留下列
脱敏状态证据：

```text
Auth V3 HTTP status: 200
Auth V3 business code: 0
tenant access token present: true
token expiry valid: true
Tenant V2 HTTP status: 200
Tenant V2 business code: 0
tenant present: true
tenant matched ProviderContext: true
Contact V3 HTTP status: 200
Contact V3 business code: 0
user present: true
asserted open_id matched: true
stable provider user id present: true
is_activated: true
is_frozen/is_resigned/is_exited/is_unjoin: all false
```

这证明修正后的 production adapter 已真实完成 Auth -> Tenant -> Contact、租户匹配与 active status
判定。证据不包含 app/tenant/account/user ID 原值、Secret、token、姓名、手机、邮箱、头像、工号
或任何可逆摘要；任何后续复验也只能保留同等级脱敏状态。I4.1 广义定向
**118 passed in 5.29s**、credential 真 PostgreSQL **1 passed in 0.76s**；完整门禁见 ROADMAP。

---

## 4. 申请 API 权限

路径：应用详情 -> 开发配置 -> 权限管理 -> API 权限 -> 应用身份。

### 4.1 首期必需

| Scope ID | 飞书名称 | 用途 |
|---|---|---|
| `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息 | 私聊消息事件 |
| `im:message:send_as_bot` | 以应用身份发消息 | 回复用户和发送状态卡片 |
| `contact:contact.base:readonly` | 获取通讯录基本信息 | Contact V3 API 和人员事件 |
| `contact:user.base:readonly` | 获取用户基本信息 | 姓名/头像等最小显示字段 |
| `contact:user.employee_id:readonly` | 获取用户 user ID | 从 app-scoped open_id 得到 tenant-scoped user_id |
| `contact:user.employee:readonly` | 获取用户受雇信息 | 用户 status、employee_no、employee_type；用于状态校验和原力工号映射 |

`contact:user.employee:readonly` 范围较宽。如果企业不允许，可评估更窄的
`contact:user.employee_number:read` 只读工号，但仍必须找到一个受支持、可验证离职/冻结状态的
来源；没有状态来源不能启用自动 JIT。

官方来源：

- [接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive?lang=zh-CN)
- [API 权限列表](https://open.feishu.cn/document/server-docs/application-scope/scope-list?lang=zh-CN)
- [用户资源字段](https://open.feishu.cn/document/server-docs/contact-v3/user/field-overview?lang=zh-CN)

### 4.2 渐进式回复阶段必需

EIM-U1/CHN-U8 代码已落地；部署到目标租户前，按飞书开发者后台当时展示的最小可用组合申请并验证：

| Scope ID | 用途 |
|---|---|
| `cardkit:card:write` | 创建和更新 CardKit 2.0 卡片实体、结束流式模式 |
| `im:message` | CardKit 引用发送、消息回复和 reaction 所需的消息能力；若后台提供更细权限则取窄 |

`im:message:send_as_bot` 已在首期必需权限中。不同租户 UI 可能仍显示旧的
`cardkit:card:read/cardkit:card:update` 拆分；以开工时官方文档和测试租户 API 实测为准，并把
最终 scope ID 记录到 ROADMAP 任务证据。修改权限后要重新发布/安装应用，旧安装获得的 token
不会自动证明新 scope 已生效。

Reaction 用于即时 acknowledgement，失败只能降级为初始卡片/文本，不能阻塞回答。
当前实现正是这个边界：`cardkit:card:write` 缺失会让同一次执行在完成时降级为 post/text，
reaction 权限缺失只影响 Typing，不会重跑 Agent。manifest 声明能力不等于某个租户已经授权；
上线检查必须实际观察到 Typing、单卡渐进更新和 final finish，再逐项撤掉权限验证降级。

官方来源：

- [流式更新卡片](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview)
- [添加消息表情回复](https://open.feishu.cn/document/server-docs/im-v1/message-reaction/create?lang=zh-CN)
- [回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply?lang=zh-CN)

### 4.3 群聊后续可选

| Scope ID | 用途 |
|---|---|
| `im:message.group_at_msg:readonly` | 接收群聊中 @ 机器人消息 |

首期不申请 `im:message.group_msg` 全群消息权限。群聊必须等待 EIM-U2 身份管理能力和 EIM-U3
独立风险评审完成，默认只响应 @、按 tenant/provider account/chat/thread 策略隔离会话；高风险
工具默认关闭。

### 4.4 明确不申请

没有独立需求和评审时，不申请：

- 通讯录写权限；
- 手机号、个人邮箱；
- 完整组织树/部门路径；
- 群聊全部消息；
- 飞书云文档、日历、任务的用户级写权限；
- `offline_access`。

最小权限不只看 scope，还要看下节的数据范围。

---

## 5. 配置应用可用范围和通讯录数据范围

### 应用可用范围

路径通常位于应用发布/版本配置或企业管理后台。决定哪些员工能看到和使用机器人。

### 通讯录数据范围

路径：开发配置 -> 权限管理 -> 对应通讯录权限的“可访问的数据范围”；企业管理员也可以在
管理后台 -> 工作台 -> 应用管理 -> 通讯录设置维护。

飞书规则：scope 决定“能调用什么”，数据权限决定“能看到哪些人”。只有 scope 没有数据范围，
Contact API 仍会权限失败。官方说明：[配置应用数据权限](https://open.feishu.cn/document/home/introduction-to-scope-and-authorization/configure-app-data-permissions)、
[通讯录权限范围](https://open.feishu.cn/document/server-docs/contact-v3/scope/scope_authority?lang=zh-CN)

原力首期建议：

- 应用可用范围：实际允许使用 AI 平台的员工；
- 通讯录数据范围：同一范围，或管理员批准的全员范围；
- MultiRAG tenant policy：例如显式 `mode=jit, link_code_ttl_seconds=600`，并记录当前 revision。

Provider active 校验和 JIT `UserTenant(NORMAL)` 是 I6 固定安全不变量，不是可由 tenant policy 放宽的
`require_active/default_role` 字段。

如果数据范围只覆盖部分部门，范围外员工收到明确的“企业身份未在应用授权范围内”提示，不能
悄悄创建匿名平台用户。

---

## 6. 配置事件与回调

路径：应用详情 -> 事件与回调。

### 事件订阅方式

选择“使用长连接接收事件”。保存配置前至少一个 SDK client 必须在线。

### 应用身份事件

| Event type | 用途 | 阶段 |
|---|---|---|
| `im.message.receive_v1` | 接收消息 | 现有 Channel |
| `contact.user.created_v3` | 员工加入/入职 | EIM-I7 |
| `contact.user.updated_v3` | 状态、工号等变更后刷新已链接身份 | EIM-I7 |
| `contact.user.deleted_v3` | 员工离职，立即禁用 | EIM-I7 |
| `contact.scope.updated_v3` | 应用通讯录范围变化，清缓存并重验 | EIM-I7 |

飞书可能重复推送事件。消息以 `message_id` 去重；通讯录事件以 `event_id + event_type` 去重，
同时数据库更新必须幂等。不要依赖“只会收到一次”。

`contact.scope.updated_v3` 后，account control 必须以 CAS 单调推进 revision/scope marker；后续 read
若发现 alias proof 早于 marker，只返回“需要 Provider 重验”，不能继续使用旧 link。新 proof
`verified_at` 早于 marker 时写入拒绝，早于当前 alias proof 时也不能倒退已有时间。省略 event/scope
时间表示保留旧值，不是清空；乱序旧事件不得回拨 marker。

### 卡片回调

纯流式输出 EIM-U1 不需要 `card.action.trigger`。EIM-U4 的重新生成/反馈或 EIM-U7 的敏感确认
上线时，才在“回调配置”中使用长连接订阅：

| Callback | 用途 |
|---|---|
| `card.action.trigger` | 低风险反馈/重新生成，或敏感操作确认、取消、查看状态 |

卡片 action payload 中的用户 ID 仍要走外部身份 resolver，并校验与待确认操作的
`platform_user_id` 相同；不能只信 action 中的按钮 value。

官方教程：[卡片交互机器人](https://open.feishu.cn/document/develop-a-card-interactive-bot/introduction?lang=zh-CN)

---

## 7. 发布与管理员审批

飞书权限、事件、可用范围修改通常需要：

1. 创建应用版本；
2. 提交发布；
3. 企业管理员审核；
4. 等待配置生效；
5. 在正式员工账号上验证。

开发阶段优先使用飞书测试企业/测试版本，避免每次权限调整打扰正式管理员。正式发布只申请已在
测试环境实测的最小权限集合。

管理员最终应交付给开发/运维的不是 Secret 文档，而是一份无敏感值的确认表：

```text
app_id: cli_...（可记录）
app_secret: 已通过 MultiRAG Channel 管理端写入（不可记录）
app_type: enterprise_self_built
transport: long_connection
app_available_scope: <范围说明>
contact_data_scope: <范围说明>
approved_scopes: <scope IDs>
subscribed_events: <event types>
subscribed_callbacks: <callback types>
published_version: <版本号>
approved_by: <管理员>
approved_at: <时间>
```

---

## 8. 首次身份联调

本节是 I4/I6/C3 的联调清单，**不是 EIM-I3 或 EIM-I6 单独可执行的 Channel 能力**。I3 不持有飞书
SDK/Secret，不调用 Contact，不创建 `User/UserTenant`，也不返回 Principal。I4.1 修正后的
production adapter sandbox 与 I4.1 全量门禁已完成；I6 的 policy/link/JIT 写事务与完整门禁也已完成，
但 C1/C2 structured assertion 与 C3/P1 组装仍未实现。只有这些依赖全部满足后，才能按本节宣称消息到
Principal 的端到端结果。

使用一个普通员工测试账号和一个管理员控制账号，执行：

1. 普通员工给机器人发私聊。
2. 记录脱敏 trace ID，不记录完整 open_id。
3. 验证事件中能得到 `tenant_key/app_id/open_id`，有权限时也可能直接得到 `user_id`。
4. 调用 `GET /open-apis/contact/v3/users/{open_id}?user_id_type=open_id`。
5. 验证响应：
   - `user_id` 非空；
   - `status.is_activated=true`；
   - 未冻结、未离职、未主动退出；
   - 原力启用工号映射时 `employee_no` 非空。
6. 再发一条消息，验证不再次调用 Contact API。
7. 模拟本地 identity cache 过期，验证只刷新一次且并发请求 single-flight。
8. 使用范围外/冻结测试用户，验证 fail closed 且不创建 `UserTenant`。

I6 三种 mode 的联调还必须分别确认：preprovisioned 未命中不开户；link_only code 只绑定已登录用户
本人、过期/撤销/旧 generation 统一要求新码；JIT 只创建 external-only User + NORMAL membership。
任何 name/email/mobile/employee_no 相同都不能触发匹配或双 User merge。I5 尚未实现，所以本轮
不得因 Contact 返回 employee_no 就宣称 EnterpriseSubject 已建立。

官方 Contact API：[获取单个用户信息](https://open.feishu.cn/document/server-docs/contact-v3/user/get?lang=zh-CN)

---

## 9. HR/OA 工号确认清单

在启用 `FeishuEmployeeNumberResolver` 前，HR/OA 必须回答：

- `employee_no` 是否全员唯一，唯一范围是公司、法人还是租户？
- 是否可能复用离职员工工号？
- OA 的 `talent_id/workcode` 是否逐字等于 `employee_no`？是否需要补零、大小写或前缀转换？
- 外包、实习、机器人账号是否有工号？能否使用 medic？
- OA -> 飞书同步最大延迟是多少？离职是否在同一链路同步？
- 历史用户工号变更如何通知？

允许的解析结果只有：

```text
RESOLVED       -> 返回不可变 enterprise_subject
NOT_FOUND      -> 禁止需要企业主体的工具，普通 RAG 可按租户策略继续
AMBIGUOUS      -> fail closed，必须人工处理
UNAVAILABLE    -> 高风险操作 fail closed；普通 RAG 使用已有未过期映射
INACTIVE       -> 禁止执行和新会话
```

如果任一唯一性问题无法回答，使用 OA/HR resolver，通过 `(tenant, feishu_user_id/employee_no)`
查询真实业务主体；不要在 MultiRAG 写原力特例 if/else。

---

## 10. 可选的飞书用户 OAuth，必须与聊天身份分离

仅当 Agent 需要“以员工本人权限”访问飞书文档、日历、任务时才实施：

1. 在飞书应用启用网页应用/OAuth 能力；
2. 配置 HTTPS redirect URI；
3. 按具体 API 申请**用户身份** scope；
4. 使用 OAuth 2.0 authorization code + PKCE；
5. 仅在确需后台持续访问时申请 `offline_access`；
6. refresh token 一次性轮换，进入独立加密 token vault；
7. token 按 `(tenant, platform_user_id, provider, granted_scopes)` 存储并支持撤销。

聊天消息里的 `open_id` 不能替代 OAuth 授权。`tenant_access_token` 也不能代表用户访问其个人文档。
官方最新接口：[获取 user_access_token](https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token)

---

## 11. 上线前飞书侧验收

- [ ] App ID/Secret 已写入 MultiRAG 加密 Secret store，无明文副本。
- [ ] Tenant provisioning policy 已由受控 onboarding 显式创建，mode、TTL、revision 已记录；缺行不
      使用默认值，普通 Channel/identity service 无 admin write capability。
- [ ] 权限列表与本文必需 scope 对账，无多余高危权限。
- [ ] 应用可用范围与通讯录数据范围已由管理员确认。
- [ ] 消息和四个 Contact 事件已订阅并发布生效。
- [ ] `cardkit:card:write`、消息/reaction 权限已在测试租户实测，应用重新发布/安装。
- [ ] card action 仅在 EIM-U4 或 EIM-U7 实际上线时订阅；纯流式输出不提前申请。
- [ ] 正常、范围外、冻结/离职测试账号行为符合预期。
- [ ] `employee_no -> talent_id` 语义已经 HR/OA 负责人签字确认，或已配置 OA resolver。
- [ ] 事件 callback 3 秒内返回，模型和 Contact API 不在 SDK callback 内执行。
- [ ] 飞书后台事件日志、MultiRAG 脱敏 trace 和身份审计能关联排障。
- [ ] Secret 轮换和应用下线联系人明确。
