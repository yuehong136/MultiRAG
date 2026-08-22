# 身份、数据与授权契约

本文件是实现级契约。示例字段名是规范目标；实施中若因仓库命名规则调整，必须同步更新本文和
[ROADMAP](ROADMAP.md)，不能只改代码。

---

## 1. 标识术语

| 规范名 | 飞书字段/当前字段 | 唯一范围 | 用途 |
|---|---|---|---|
| `provider` | `feishu` | 全平台枚举 | Provider 路由 |
| `provider_tenant_key` | 飞书 `tenant_key` | 飞书租户 | 外部企业边界 |
| `provider_account_key` | 飞书 `app_id` | 飞书应用 | open_id alias 边界 |
| `provider_user_id` | 飞书 `user_id` | 一个飞书租户 | canonical external subject |
| `open_id` alias | 飞书 `open_id` | 一个 App | 消息入口查找 |
| `union_id` alias | 飞书 `union_id` | 同一开发商多个 App | 辅助关联，不是主键 |
| `platform_user_id` | MultiRAG `User.id` | MultiRAG | 平台主体 |
| `tenant_id` | MultiRAG `Tenant.id` | MultiRAG | 平台企业边界 |
| `enterprise_subject` | employee_no/talent_id/workcode | 企业业务域 | MCP 业务主体 |

禁止使用裸 `open_id`、姓名、邮箱或手机号做跨 App/跨租户唯一键。

`CustomerOrganization` 是目标架构中的真实客户企业/合同与治理边界，不是当前数据库字段。首期固定
`CustomerOrganization 1:1 Tenant`，不新增表、JWT claim、API 参数或动态路由；实现和安全约束仍
以 `Tenant.id` 为平台边界。未来若需一个 Organization 管理多个 Tenant，必须用新 ADR/schema 明确
成员、Provider Tenant、凭据和策略继承关系，不能复用 payload 字段临时路由。

首期租户映射是服务端固定关系，而不是请求级路由：

```text
(provider, provider_tenant_key) -> exactly one tenant_id
(provider, provider_tenant_key, provider_account_key) -> exactly one tenant_id
provider_account_id <-> exactly one channel_id（首期显式 link）
link(provider_account_id, channel_id, provider, tenant_id) -> 两端完全同 scope
```

一个飞书企业默认且首期强制只对应一个 MultiRAG Tenant；一个应用安装实例/provider account 和一个
Channel binding 在整个生命周期内也只能指向一个 Tenant。消息、卡片、prompt、worker command 或
resolver 返回值都不能选择/覆盖目标 `tenant_id`。未来集团级多 Tenant 只能作为受控例外启用，并为
每个目标 Tenant 使用不同的 Provider Account/应用安装实例；即使进入该例外，一个 Provider Account
或 binding 仍不得动态路由多个 Tenant。完整决策见
[EIM-ADR-23](DECISIONS.md#eim-adr-23首期一个飞书企业一个-tenantprovider-account-与-binding-固定单-tenant)。

---

## 2. Channel 私有输入契约

### 2.1 当前 C1 tolerate + C2 emit DTO 与最终 DTO

EIM-C1 / CHN-X5 先改变 private execution API 的消费侧，EIM-C2 / CHN-X6 再让飞书 worker 发送同一
形状。`provider/subject/conversation` 仍全部必填，`identity` 仍是不可信材料：

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExternalIdentityIdentifier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=255)


class ExternalIdentityAssertion(BaseModel):
    """Provider SDK 给出的不可信外部身份断言。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    provider_tenant_key: str | None = Field(default=None, min_length=1, max_length=255)
    identifiers: tuple[ExternalIdentityIdentifier, ...] = Field(min_length=1, max_length=8)


class ChannelActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=255)
    conversation: str = Field(min_length=1, max_length=255)
    identity: ExternalIdentityAssertion | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
```

`exclude_if` 是 C1/C2 的 wire 兼容闸门：旧 actor 做 `model_dump(mode="json")` 时不新增
`"identity": null`，runtime client 收到 `identity=None` 时仍产生旧请求字节。Channel transport 内使用
冻结、脱敏 repr 的 `IncomingIdentityAssertion/IncomingIdentityIdentifier`；飞书规范化后由 Bridge
传给 runtime client，regenerate 也保留原 assertion，再映射为上面的 private DTO。C3 execution
composition 只在成功 claim 后消费 `identity`，且只能结合服务端 binding/account link、Provider proof、
I3/I6 与 P1 得出 Principal；assertion 仍不能改写 Tenant、目标、session 或 Principal。

飞书 C2 的 Provider-specific 约束比通用 DTO 更窄：事件 header `tenant_key` 和 sender `open_id` 必填，
存在的 `user_id/union_id` 全部按 kind 保留；sender tenant 有值时必须匹配 header tenant。header app
有值时只与 worker 本地配置账户核对，既不进入 assertion，也不替代后续服务端 Provider Account
ownership。legacy `subject` 固定继续使用同一个 `open_id`，`conversation` 继续使用 chat ID。

C4 完成、所有 runner 浸泡并具备部署证据后，目标形状才删除 legacy `subject`，收敛为：

```python
class ChannelActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: ExternalIdentityAssertion
    conversation: str = Field(min_length=1, max_length=255)
```

飞书 Provider 必须尽可能同时保留：

```json
{
  "provider": "feishu",
  "provider_tenant_key": "7365...",
  "identifiers": [
    {"kind": "open_id", "value": "ou_..."},
    {"kind": "user_id", "value": "u_..."},
    {"kind": "union_id", "value": "on_..."}
  ]
}
```

顺序不表达信任优先级；Identity Provider 根据 `kind` 选择。拒绝重复 kind、未知超长 ID 和空值。
未知 kind 可以保留给 Provider resolver，但不能自动成为 canonical subject。

### 2.2 信任约束

- 首期只支持平台托管 adapter。受管 worker 按 transport 验证 webhook 签名/加密或受认证的长连接，
  再校验应用、租户、事件时间和重放；worker 随后用 binding-scoped workload credential 调用
  private API。单独通过任一层都不能把外部主体提升为 Principal。
- `provider` 必须与服务端 binding 的 channel provider 一致。
- `provider_tenant_key` 必须与 provider account 首次已验证 tenant key 一致；首次绑定时通过应用
  凭据和 Contact/event 共同确认后保存。
- 服务端还必须验证 provider account 与 Channel binding 的固定 `tenant_id`；首期若同一
  `(provider, provider_tenant_key)` 已绑定其他 Tenant，onboarding fail closed，不创建第二条映射。
- `app_id` 不从消息 assertion 读取；它来自解密后的 provider account credential/config。
- `event_id`/事件时间属于受限 transport envelope，用于时效与重放校验，不是主体字段；
  `provider_account_key` 也始终取服务端 binding，不接受 payload 覆盖。
- `tenant_id/target/revision/session/principal/scopes` 不存在于 worker command body。
- role、group、department、audience、确认状态同样不得出现在 assertion 中或参与主体提升。
- 即使事件直接带 `user_id`，首次仍要验证其处于应用数据范围且 status 可用。

### 2.3 兼容升级

旧 worker 只有 `provider/subject/conversation`，且私有模型 `extra="forbid"`。必须走：

1. **tolerate**：API 同时接受 legacy `subject` 和新 `identity`，只读旧字段；部署 API/execution。
2. **emit**：worker 开始发送新 `identity`；execution 仍不消费它并保留 legacy 行为；重启 supervisor/
   worker，观察无 `extra_forbidden`。
3. **consume**：由服务端 binding/account link 构造 ProviderContext，经 Provider verification、
   IdentityService 与 provisioning 后，才把已验证平台用户提升为 Principal；完成活体联调。
4. **remove**：所有 runner 已升级后删除 legacy `subject`，再次按先 API 后 worker 部署。

每一步是独立 PR/任务，映射的 `CHN-*` 见 [ROADMAP](ROADMAP.md)。

截至 2026-08-13，C1 tolerate 与 C2 emit 均已完成。C2 提交 `896c582d` 的定向 **104 passed in
2.03s**、独立兼容矩阵 **153 passed**、安全扫描 **0 findings**；完整 `make verify` 为 8 条 import
contracts、mypy 86 source files、unit **2265 passed in 37.20s**，smoke 六组件全绿。supervisor 重启后
两个飞书 worker 均 connected；真实消息只记录 identity/tenant 是否存在、identifier kinds/count 的
脱敏结构，随后 private execution HTTP 200 并完成。resolver 未 consume，Canvas 仍为 `user_id=""`、
`exp_user_id=null`，10 张 EIM sidecar 零写入；不得把已 emit 的 assertion 描述为已验证身份、数据库映射
或已上线 Principal。该句是 C2 当时的 live 边界；C3/X7 后续已完成新 API 部署和真实飞书 live，
不得再用这段历史快照解释当前行为。

C3 consume 的当前私有契约是：pre-claim 的 upgrade/run-policy/target capability deny/cancel 行为不变；
成功 Redis claim 后才按 authority→initial I3→I4→I6→final I3→P1 解析身份，并在 session/target 前完成。
每个 linked event 逻辑上执行 I4，允许命中同 account generation 的有界 cache，但 cache hit 沿用原 proof
时间。NO_LINK 保留 legacy anonymous；LINKED 缺 assertion、authority 非唯一/损坏、Provider/Tenant/
subject 不一致、identity inactive/conflict/revoked 或 proof/canonical drift 均 fail closed。

成功 claim 的初始 NX 直接占满完整 dedupe TTL；之后 identity、session、target、stream、session put 或
complete 的错误与取消都写同一 full-window failure tombstone，不能在短 TTL 后重放。会话值为带版本的
tenant/principal owner envelope；legacy raw 仅 NO_LINK 可续用，linked principal 不复用 raw 或其他 owner
会话。Dialog/Canvas 已有数据库行还要校验 owner 等于本次 `principal_id`。这只完成 C3 Principal
promotion 与 target/session ownership，不把 Principal 传播进 Agent/RAG/Memory/Workflow/MCP；P2 仍负责
后者。自动证据为 C3 定向 **203 passed**、`make verify` **2403 passed**、强制 integration
**162 passed**。12:42 API 已重启到 `v0.9.9-579-g2b0482c7`，`make smoke` 六组件全绿；真实飞书
live 覆盖 **2/2** account，四条 alias 收敛到一个 active ExternalIdentity/一个 canonical User，只有
一条 valid NORMAL membership 与一条 BindingEvent。Canvas、Dialog 各一条本次 Principal owner 记录，
空 owner 为 **0**；Redis completed/replied 存在且 processing/failed 为 **0**。这些是脱敏计数/状态证据，
不包含完整 app/tenant/account/user 标识或 Secret。下一主线 P2 负责全链 Principal context；不在本契约
中提前实现 A2/P3 token，CHN-O9 可并行，C4/CHN-X8 仍等待全部 runner 的 deployment soak。

### 2.4 Channel 交互契约的所有权

身份 assertion 之外，入站消息最终还要结构化保留 `root_id/parent_id/thread_id`、mention、quote、
content type 和 attachments；这些字段是 transport data，不能自动成为 Principal 或授权证据。
完整形状和升级顺序由 [FEISHU_BOT_UX §4](FEISHU_BOT_UX.md#4-transport-neutral-契约) 负责。

出站不把飞书卡片字段塞进执行事件。EIM-U0 已落地的 worker 内事件只有用户可见
`MessageDeltaEvent`、带必要 session 及可选权威正文的 `MessageCompletedEvent` 和稳定错误码
`ExecutionFailedEvent`；白名单 status、脱敏 references/artifacts 是后续加法契约。
Provider-neutral ReplySession 已管理 `begin/append/replace/complete/fail` 和明确终态，飞书 adapter 才能拥有
`card_id/message_id/sequence/reaction_id`。

两层幂等分别固定为：

```text
业务执行：binding + event_id/message_id -> Redis atomic claim
飞书发送：binding + event_id + delivery_stage -> deterministic uuid
```

同一个网络结果未知的 delivery stage 不得换 UUID 重发；渲染降级不得重新执行 Agent 或 MCP。
该契约由 EIM-U0/CHN-X9 以加法提供 `stream()`/ReplySession：`stream()` 是 HTTP/SSE/校验/错误
处理的唯一核心路径，BindingBridge 已直接消费它；`ask()` 仅为迁移与回滚安全保留为薄聚合层，
不是新代码入口。生产调用归零且 EIM-U1/CHN-U8 稳定后再单独删除；U0 未改变现有 SSE wire，
也未实现 CardKit。

---

## 3. 数据模型

所有新表位于 `usr_ai` schema，继承现有 `BaseModel` 审计字段。新代码 async-first。DB 迁移必须
同时覆盖：fresh install 模型建表、存量 Alembic upgrade、真 PostgreSQL 唯一约束与并发行为。

### 3.1 `User` 演进

目标变更：

| 字段 | 目标 |
|---|---|
| `email` | nullable；非空时保持唯一 |
| `password` | external-only 用户为 null |
| `login_channel` | 保留来源提示，不作为唯一身份 |
| `account_kind` | 新增：`local` / `external` / `hybrid`，非空 |
| `is_active/status` | 平台账户状态；外部身份失效会阻止 Channel 执行，但不自动删除用户历史数据 |

迁移规则：

- 存量行按旧字段可逆分类：`login_channel` 为空或为 `password` 时写 `local`；非密码渠道且
  `password is null` 时写 `external`；非密码渠道且仍有密码时写 `hybrid`。这只是兼容分类，
  不把旧 OAuth 邮箱升级为已验证外部身份。只要 `email` 非空且 `account_kind` 仍能由旧字段
  重建，upgrade → downgrade → upgrade 必须逐字段恢复；否则 downgrade fail closed。
- JIT 创建：`email=null`、`password=null`、`account_kind=external`、`login_channel=feishu`。
- I6 JIT 同一事务只创建一条 active `UserTenant(NORMAL)`；数据库以 partial unique index
  `UNIQUE(tenant_id, user_id) WHERE status='1'` 拒绝同一用户在同一 Tenant 出现两条 active
  membership。该约束不删除历史 inactive membership，也不把普通成员提升为管理员。
- 后续绑定本地/OIDC 登录时显式升级 `hybrid`；禁止按相同邮箱静默合并。
- 密码登录、找回密码、邮件通知入口必须对 `email is null` 给出确定行为，不能 500。
- `account_kind=external` 在数据库和 service 层都禁止本地密码；密码登录/找回只查
  `local/hybrid`，对 external-only 账号按无可用密码凭据 fail closed，不对 null 密码调用 bcrypt。
- 新建密码账号显式写 `local`。I1 不新建 OAuth-only 账号；I6/JIT 只有在 Provider subject
  绑定验证完成后才可新建 `external`，或通过显式 ExternalIdentity→既有 User 绑定把该 User 升级为
  `hybrid`；这不是两个 User 的 merge。
- 现有 Web OAuth callback 在 I1 期间完成 Provider 回调校验后固定返回
  `oauth_identity_binding_required`，**不按 email 登录、注册或合并任何平台账号**；OAuth `state`
  必须存在且逐字匹配 session 中的一次性值。I6 只提供显式身份绑定事务，不会自行重新开放 Web
  OAuth；未来仍须受控 OAuth adapter/composition 复用该绑定契约后，才可恢复相应账号动作。
  Channel/JIT 也不得复用 email callback。存量 `login_channel != password` 账户仅由迁移保守分类为
  `external/hybrid`，不因此获得可用的 verified external identity。

### 3.2 `t_ai_identity_provider_tenants`

Provider Tenant ownership 在数据库层把一个已验证外部企业固定到一个 MultiRAG Tenant：

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | string PK | opaque ownership ID |
| `tenant_id` | non-null | 唯一归属的 MultiRAG Tenant |
| `provider` | non-null | `feishu` 等 Provider 路由 |
| `provider_tenant_key` | non-null | 已验证外部企业边界 |
| `verified_at` | timestamptz, non-null | ownership 的权威验证时间 |

```text
UNIQUE(provider, provider_tenant_key)
UNIQUE(tenant_id, provider, provider_tenant_key)
FOREIGN KEY(tenant_id) -> t_ai_tenants(id) ON DELETE RESTRICT
```

第一条唯一约束就是首期“一飞书企业恰好一个 MultiRAG Tenant”的机器防线；第二条是 Provider
Account 复合外键的引用目标。`provider/provider_tenant_key` 不允许空白。未来集团级多 Tenant 必须
以新 ADR 和显式 schema 迁移改变此约束，不在首期代码中设置可跳过开关或动态路由。默认安全
projection 省略 `provider_tenant_key`。

### 3.3 `t_ai_identity_provider_accounts`

Provider Account 是服务端管理的独立企业连接/应用安装 ownership 边界，也是 alias/receipt 进入身份
库前的固定 Tenant 锚点。它不再从属于 Channel，可以在没有 Channel 时先创建；但在通用 Provider
credential vault 落地前，无 Channel link 的 account 不得调用 Provider API：

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | string PK | opaque ownership ID |
| `tenant_id` | non-null | 唯一归属的 MultiRAG Tenant |
| `provider` | non-null | `feishu` 等 Provider 路由 |
| `provider_tenant_key` | non-null | 首次 onboarding 验证的外部企业边界 |
| `provider_account_key` | non-null | 应用安装实例/account；飞书为 `app_id` |
| `identity_revision` | bigint, default 1 | scope/状态变化时单调增加；与 scope marker 一起进入 ProviderContext |
| `last_scope_change_at` | timestamptz/null | 最近目录 scope 变化的单调 marker；read/write context 必须精确匹配 |
| `last_directory_event_at` | timestamptz/null | 最近可信目录事件；只允许保留或前进 |
| `identity_health_state` | non-null | `pending/healthy/degraded/error/disabled` |
| `identity_health_error_code` | string/null | 只在 `error` 状态存在的稳定脱敏错误码 |

数据库约束：

```text
UNIQUE(provider, provider_tenant_key, provider_account_key)
UNIQUE(id, tenant_id, provider)
UNIQUE(tenant_id, provider, provider_tenant_key, provider_account_key)
FOREIGN KEY(tenant_id, provider, provider_tenant_key)
  -> t_ai_identity_provider_tenants(tenant_id, provider, provider_tenant_key) ON DELETE RESTRICT
FOREIGN KEY(tenant_id) -> t_ai_tenants(id) ON DELETE RESTRICT
```

第三条复合唯一键是 alias/receipt tenant-scoped 外键的引用目标；第一条保证同一 Provider Account
不能在另一个 Tenant 重建；第二条是 §3.4 link 复合外键的引用目标。
`identity_revision >= 1`。`identity_health_state=error` 时 error code 必填，其他状态必须为空。

每条 account 还通过复合 `ON DELETE RESTRICT` 外键
`(tenant_id, provider, provider_tenant_key)` 引用 §3.2 的 enterprise ownership；因此同一企业即使
使用不同 app/account，也不能写入另一个 Tenant。默认安全 projection 省略
`provider_tenant_key/provider_account_key/identity_health_error_code`。

### 3.4 `t_ai_identity_provider_channel_links`

Channel 通过显式 link 引用 Provider Account；link 自身重复保存 `tenant_id/provider`，以数据库复合
外键同时证明两端 scope 相同，而不是只依赖 service 先查后写：

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | string PK | opaque link ID |
| `tenant_id` | non-null | 两端共同的 MultiRAG Tenant |
| `provider` | non-null | 两端共同的 Provider；必须非空白 |
| `provider_account_id` | non-null, unique | §3.3 Provider Account；首期一个 account 至多一个 Channel |
| `channel_id` | non-null, unique | `ChatChannel.id`；一个 Channel 至多一个 account |
| `linked_at` | timestamptz, non-null | 服务端完成绑定的时间 |

```text
UNIQUE(provider_account_id)
UNIQUE(channel_id)
FOREIGN KEY(provider_account_id, tenant_id, provider)
  -> t_ai_identity_provider_accounts(id, tenant_id, provider) ON DELETE RESTRICT
FOREIGN KEY(channel_id, tenant_id, provider)
  -> t_ai_chat_channels(id, tenant_id, channel) ON DELETE RESTRICT
FOREIGN KEY(tenant_id) -> t_ai_tenants(id) ON DELETE RESTRICT
```

`ChatChannel` 为第二条复合外键提供 `UNIQUE(id, tenant_id, channel)` parent key。跨 Tenant、跨
Provider、一个 Channel 两个 account、一个 account 两个 Channel 均由数据库拒绝。默认安全
projection 省略 `tenant_id/provider_account_id/channel_id`。

link 是首期不可变 ownership 证据：普通 Channel update 不得 unlink/rebind，删除任一端因
`ON DELETE RESTRICT` 失败。当前凭据仍由 `ChannelSecret` 持有，所以这一步只解耦身份数据模型，
不能宣称 Provider credential 已从 Channel 解耦。I4 只能沿唯一 link 取 ChannelSecret；无 link 的
account 可以处于 onboarding/pending，但不能执行目录或身份 Provider 调用。

### 3.5 `t_ai_external_identities`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | string PK | opaque ID |
| `tenant_id` | non-null, indexed | MultiRAG tenant |
| `user_id` | non-null, indexed | `User.id` |
| `provider` | non-null | `feishu` 等 |
| `provider_tenant_key` | non-null | 外部租户边界 |
| `subject_type` | non-null | 飞书固定 `user_id` |
| `subject_value` | non-null | canonical provider user ID |
| `state` | non-null | `pending_link/active/inactive/revoked/conflict` |
| `verified_at` | timestamptz | 最近权威验证 |
| `last_seen_at` | timestamptz | 最近消息/登录 |
| `identity_revision` | bigint | 失效/范围变化版本 |
| `attributes` | JSONB, default `{}` | 只存允许的非敏感显示/状态快照；禁止 token 和权限 |

唯一约束：

```text
UNIQUE(tenant_id, provider, provider_tenant_key, subject_type, subject_value)
UNIQUE(tenant_id, user_id, provider, provider_tenant_key, subject_type)
```

第二条是全状态 reverse slot：同一平台用户在同一 Provider Tenant、同一 subject type 下只能绑定
一个 canonical subject。它不是只约束 active 行；把旧 identity 改成 inactive/revoked 也不能绕过后
另绑一个 subject。真正换绑或历史纠错必须由未来受控流程审计处理。

`tenant_id` 和 `user_id` 分别以 `ON DELETE RESTRICT` 引用现有 Tenant/User；另有复合
`ON DELETE RESTRICT` 外键
`(tenant_id, provider, provider_tenant_key) -> t_ai_identity_provider_tenants`，因此 canonical identity
也不能伪造另一个外部企业 scope。该 tenant-scoped 唯一键是数据库隔离和未来受控多 Tenant 例外的
防御纵深，**不是**允许同一飞书企业或同一 binding 在首期映射多个 Tenant；映射层仍必须遵守 §1
的固定关系。

`attributes` v1 是关闭的 allowlist，只允许：

| key | 值类型 | 用途 |
|---|---|---|
| `display_name` | string/null | 最近一次受信目录显示名；仅用于受控显示，仍按个人信息处理 |
| `provider_status` | string/null | Provider 的低敏状态快照；不据此授予权限 |

JSON 顶层必须是 object；未知 key、数组/数字/布尔/嵌套对象一律拒绝。不得保存 role、group、department、
scope、tenant/app/user access token、Secret、原始 Provider 响应或任意 metadata bag。默认安全
projection 整体省略 `provider_tenant_key`、`subject_value` 和 `attributes`。

状态变化使用行锁/乐观 revision。`identity_revision >= 1`；`active` 必须有 `verified_at`；`revoked`
不物理删除，不允许普通 JIT 自动复活。

### 3.6 `t_ai_external_identity_aliases`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | string PK | opaque ID |
| `tenant_id` | non-null | MultiRAG tenant；不可从消息覆盖 |
| `external_identity_id` | non-null, indexed | canonical identity |
| `provider` | non-null | 冗余用于唯一约束/查询隔离 |
| `provider_tenant_key` | non-null | 外部租户 |
| `provider_account_key` | non-null | 飞书 app_id；union alias 可使用开发商 key |
| `alias_type` | non-null | `open_id` / `union_id` |
| `alias_value` | non-null | alias |
| `verified_at` | timestamptz | 最近 Provider 验证 proof；刷新只允许保留或前进 |

唯一约束：

```text
UNIQUE(tenant_id, provider, provider_tenant_key, provider_account_key, alias_type, alias_value)
```

同一个 canonical identity 可以有多个 App 的 open_id alias。alias 冲突进入 `conflict`，不得
“后写覆盖前写”。复合 `ON DELETE RESTRICT` 外键同时绑定
`(external_identity_id, tenant_id, provider, provider_tenant_key)`，因此不能把另一个 Tenant、Provider
或外部企业的 canonical identity 挂到当前 alias；另有 tenant 外键阻止孤立租户。`alias_type` v1 只
接受 `open_id/union_id`，`verified_at` 必填。默认安全 projection 省略
`provider_tenant_key/provider_account_key/alias_value`。

### 3.7 `t_ai_enterprise_subject_links`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | string PK | opaque ID |
| `tenant_id` | non-null | MultiRAG tenant |
| `user_id` | non-null | platform user |
| `subject_type` | non-null | `employee_no/talent_id/workcode` |
| `subject_value` | non-null | 业务主体；API 默认不回显完整值 |
| `issuer` | non-null | `feishu_contact/oa/hr` |
| `issuer_tenant` | non-null | issuer 内的企业/namespace；来自 resolver 配置或权威响应，不接受调用方覆盖 |
| `state` | non-null | `active/inactive/conflict` |
| `verified_at` | timestamptz | 最近验证 |
| `source_revision` | string/null | OA/HR 版本或飞书 identity revision |

唯一约束根据企业配置：默认

```text
UNIQUE(tenant_id, subject_type, subject_value)
```

一个 user 可以有不同业务域的多个 subject；MCP service 必须声明需要哪种类型，不能“随便取第一
个”。`tenant_id/user_id` 均使用 `ON DELETE RESTRICT`；同一 user、subject type、issuer namespace
只能占一个 resolver slot：

```text
UNIQUE(tenant_id, user_id, subject_type, issuer, issuer_tenant)
```

`active` 必须有 `verified_at`。默认安全 projection 省略 `subject_value` 和 `issuer_tenant`；只有受授权
的内部 resolver/token issuer 可以读取原值。

### 3.8 `t_ai_identity_event_receipts`

用于 Contact 事件幂等：

```text
UNIQUE(tenant_id, provider, provider_tenant_key, provider_account_key, event_type, event_id)
```

精确字段：

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | string PK | opaque receipt ID |
| `tenant_id` | non-null | 固定 binding 解析出的 MultiRAG tenant |
| `provider` | non-null | Provider 路由 |
| `provider_tenant_key` | non-null | 外部企业边界 |
| `provider_account_key` | non-null | 应用安装实例/account 边界 |
| `event_type` | non-null | Provider 事件类型 |
| `event_id` | non-null | Provider 事件 ID |
| `event_hash` | 64-char lowercase hex | 事件摘要；v1 为 SHA-256 形状，具体规范化输入由 I7 固定 |
| `processing_state` | non-null | `processing/succeeded/failed` |
| `event_at` | timestamptz/null | Provider 声明的事件时间 |
| `processed_at` | timestamptz/null | 本次处理完成时间 |
| `error_code` | string/null | 稳定、脱敏的内部错误码 |
| `external_identity_id` | string/null | 已解析 identity；事件尚未绑定时允许 null |

状态组合固定为：`processing` 时 `processed_at/error_code` 都为 null；`succeeded` 时
`processed_at` 非空且 `error_code` 为 null；`failed` 时两者均非空。receipt 不存在 body/payload/
headers/attributes 列，不保存完整事件体、聊天正文或 Provider token。若关联 identity，复合
`ON DELETE RESTRICT` 外键强制 identity 与 receipt 的 `tenant_id/provider/provider_tenant_key` 一致；
tenant 本身也为 `RESTRICT`。默认安全 projection 省略
`provider_tenant_key/provider_account_key/event_id/event_hash`。

该 tenant-scoped 幂等键防止跨 Tenant 碰撞，也不放宽 §1：同一个 Provider Account/binding 仍只能
对应一个 Tenant。I7 的处理器必须在固定 binding 上原子 claim；不得因换一个 `tenant_id` 就绕过
重复事件。

### 3.9 迁移、删除与回滚约束

- I2 六张表与 I2.1 link 表的 Tenant/User/Channel/provider ownership/parent identity 外键统一
  `ON DELETE RESTRICT`。
  解绑、停用和 revoked 保留历史，不能靠级联删除清理认证证据。
- I2 migration 在 `t_ai_chat_channels` 增加的 `UNIQUE(id, tenant_id)` 保留；I2.1 另加
  `UNIQUE(id, tenant_id, channel)`，作为 link `(channel_id, tenant_id, provider)` 复合外键的 parent
  key，不改变 Channel 公共 API。若同名约束形状不符则 upgrade fail closed。
- Alembic upgrade 遇到同名表时逐列、server default、主键、唯一约束、**规范化后的 CHECK SQL**、
  索引和外键核对完整 shape；不兼容即 fail closed。只比较 CHECK 名称或只看列存在不够，不能把 ORM
  `create_all` 生成、弱化默认值/检查表达式或人工残留的近似表当成已迁移。错误 server default 和
  “六表仅部分存在”的半迁移 schema 都必须有真库负向测试并 fail closed，不能补齐后继续启动。
- downgrade 按 receipt → enterprise subject → alias → canonical identity → provider account →
  provider tenant 的依赖逆序删除。计数与 DROP 前先在同一事务对所有现存 I2 表取得
  `ACCESS EXCLUSIVE` 锁，消除“空表检查后、DROP 前并发写入”的 TOCTOU；只有六表全部为空时允许
  执行。任一表存在身份历史都拒绝 downgrade，不能静默丢失映射或幂等证据。真库门禁还必须让并发
  writer 被锁阻断；当前固定证据为 PostgreSQL SQLSTATE `55P03`。
- fresh install、Alembic upgrade、空表 downgrade → upgrade 与有数据 downgrade 拒绝都必须在真
  PostgreSQL 验证。生产迁移/删除仍需独立批准；本契约不授权执行外部环境操作。

I2.1 是对已提交 I2 的 forward migration `9a3b5c7d8e0f`，不重写 `8f2c4d6e7a9b`。upgrade 新建
link、把每条旧 `IdentityProviderAccount.channel_id` 原样 backfill 为一条同 Tenant/Provider link，
验证后才删除旧列。已有 model-first 目标 shape 必须逐字段/约束一致，否则 fail closed。downgrade
当前采取更保守的零数据策略：只要 Provider Account 或 link 任一表有数据就拒绝；只有两表都为空时
才恢复旧 `channel_id NOT NULL UNIQUE` 并删除 link/新 parent keys。这样无 link account 必然 fail
closed，也不会静默丢掉独立企业连接或误恢复 ownership。

### 3.10 Provider account 控制面与已实现 onboarding

I2 已把下列字段持久化到 §3.3 Provider Account；I2.1 用 §3.4 link 解耦 account 与 Channel；I3
已经提供窄 repository/CAS seam，I6.1 已把现有 `ChatChannel` 接入受控 ownership onboarding；I7
后续再接 scope event 和 rotation 控制面：

```text
provider_tenant_key
identity_revision
last_scope_change_at
last_directory_event_at
identity_health_state
identity_health_error_code
```

这些字段属于私有/管理员面，公开响应必须脱敏。`provider_tenant_key` 不能从普通 Channel update
请求任意修改；只通过 verified onboarding/rotation 流程更新。I3 只能从 provider tenant/account
ownership 取得固定 `tenant_id`，不能信任 payload 或另建旁路 mapping。六表约束是持久化防线，但
普通 Channel update 仍不能直接创建、换绑或覆盖 ownership。

EIM-I3 repository 已保持这条不可变边界：普通业务路径不得修改 provider tenant/account 的
`tenant_id/provider/provider_tenant_key`，也不得 unlink/rebind link 的
`tenant_id/provider/provider_account_id/channel_id`，不得 hard-delete ownership 或其 identity 历史。
verified onboarding/rotation 只能通过显式领域操作执行；account/identity 状态、scope 与 revision 更新
使用 `identity_revision` compare-and-set，或在事务中 `SELECT ... FOR UPDATE` 后复核 revision。并发
冲突 fail closed，禁止先查后写、last-write-wins 或删除重建来“换绑”。

### 3.11 EIM-I2/I2.1/I3/I6/P1 与 FastMCP 4 的边界

I2/I2.1 实现 MultiRAG 领域身份持久化、Alembic 和数据库不变量；I3 增加 identity contracts、policy、
service 与 repository；I6 增加权威 provisioning policy、一次性 link grant、首次绑定审计及完整
事务；P1 在 `api.identity.principal` 定义 MultiRAG 唯一的 Principal 与认证证据 contract。这些领域层
都不 import FastMCP，也不复制 FastMCP 的工具/provider/auth 类型。这不是
重复造工具开放层：FastMCP 4 的
`RemoteAuthProvider`、`AccessToken`、root `on_list_tools/on_call_tool` middleware 与 HTTP
Host/Origin 防护已经分别由 A3/A4 使用；未来 A7/P3 继续在 MCP composition/adapter 边界复用。

FastMCP 不拥有 MultiRAG 的 Tenant、`User/UserTenant`、飞书 provider account/binding、canonical
external identity、业务 subject 或事件 receipt，也不能替代上述数据库唯一约束、目录验证和固定
Tenant 映射。领域 Principal/identity service 继续保持框架无关；只有进入 MCP Resource Server 或
tool adapter 时才投影到 FastMCP 类型。

### 3.12 EIM-I3 repository 权限与事务契约

I3 把仓储拆成五个最小权限 Protocol；其中三类 write/control capability 与 ownership 相互独立，业务
`IdentityService` 只持有 lookup：

| Port | 允许的方法 | 明确禁止 |
|---|---|---|
| `IdentityLookupRepository` | 精确读取 Provider Account；解析 account + alias + identity + live membership | insert/update/delete/commit、ownership、Channel link 操作 |
| `IdentityMutationRepository` | 新建 `pending_link` identity；ordinary 单向收紧 state CAS | alias 写/刷新、activation、account health/scope、ownership、generic CRUD |
| `VerifiedIdentityMutationRepository` | 持有 fresh Provider proof 后新建/刷新 alias；显式 verified activation | ordinary 调用方注入、account control、ownership、hard delete |
| `ProviderAccountControlRepository` | account health、scope marker、directory-event marker CAS | identity/alias 写入、ownership、Channel link 操作 |
| `VerifiedOwnershipRepository` | `insert_verified_provider_tenant`、`insert_verified_provider_account` | 普通 service 注入、Channel unlink/rebind、任意 CRUD |

聚合 `IdentityRepository` 只等于 lookup + ordinary mutation + verified identity mutation + provider-account
control；它不继承 `VerifiedOwnershipRepository`。这是能力隔离，不是建议式命名：普通 provisioning
用例不能拿 alias/activation 或 account-control capability，fresh Provider verifier 也不能顺带换绑
ownership。

`SqlAlchemyIdentityRepository` 使用 `AsyncSession`，但**调用方拥有事务和 commit/rollback**；repository
不得把半个 identity/alias 事务自行提交。所有 identity/alias mutation command 都携带完整
`ProviderContext`。写入前锁定精确 Provider Account，并重新核对
`tenant_id/provider/provider_tenant_key/provider_account_key/identity_revision/last_scope_change_at`，后续
scope 只从锁住的 account 派生，不能从请求字段重新拼出另一个租户。read 也同时匹配 account revision
与 scope marker，避免 scope 已变化但 revision/context 仍被旧调用链复用。

状态规则固定为：

- ordinary `insert_identity` 只创建 `pending_link`，`verified_at/last_seen_at` 初始为空；调用前还要证明目标
  `User` 活跃、非匿名且在同一 Tenant 有且只有一条有效 `UserTenant` membership；
- ordinary `cas_identity_state` 只允许
  `pending_link/active -> inactive/conflict/revoked` 与 `inactive -> conflict/revoked`，不能进入
  `active`；`active` 只能通过 verified capability 中携带 `verified_at` 的
  `activate_verified_identity` 从 `pending_link/inactive` 进入；
- `conflict/revoked` 是终态，彼此也不能转换，不能由 JIT、重复消息或普通 CAS 自动复活；
- alias 新建/刷新只存在于 `VerifiedIdentityMutationRepository`。proof 早于 account
  `last_scope_change_at` 时以 revision conflict 拒绝；同一 alias/identity 的更新只允许
  `verified_at` 前进，相等或更旧 proof 不会把已有时间倒退；
- verified activation 同样要求 healthy account、live membership，且 proof 不得早于 scope marker；
- 相同自然键的幂等 identity insert 只在 ownership-relevant scope 与 user 相同、且已有 identity 非
  `conflict/revoked` 时重读；已有 mutable state/revision/attributes 保持数据库权威且绝不覆盖。若已被
  不同 user/identity 占用则返回稳定 `IDENTITY_LINK_CONFLICT`；
- account health 与 identity state 更新都以当前 revision 为 CAS 条件；并发只允许一个 writer 成功，
  stale writer 得到 `revision_conflict`。health command 省略 `last_scope_change_at` 或
  `last_directory_event_at` 表示保留当前值，不是清空；显式时间只能等于或晚于当前值，倒退返回
  `invalid_transition`；
- 所有 I3 SQLAlchemy Core INSERT/UPDATE 显式写入 BaseModel 的 `create_date/update_date` 与
  `create_time/update_time`（update 只刷新 update 字段），不依赖 ORM event 在 Core DML 上碰巧生效；
- repository error 只携稳定 error code，DTO 默认 `repr` 隐去 provider tenant/account key、alias、
  subject、user/identity ID，不能把 SQL/Provider 原值混入错误。

输入边界在 SQL 之前 fail closed：context 的 tenant/account ID、provider/key、int64 revision 与 aware
scope marker，alias type/value，
onboarding scope、identity subject/user 与 attributes 都做非空和最大长度检查；attributes 只接受
`display_name/provider_status` 且值为 string/null，重复 key、未知 key、嵌套/超长值拒绝。所有公开
repository 方法将 SQLAlchemy/driver 异常统一转换为
`IDENTITY_REPOSITORY_UNAVAILABLE`，不传播 statement、bind parameter 或底层异常文本。

verified ownership insert 只是**持久化权限边界**，不等于外部所有权已经验证。I4/onboarding composition
必须先完成 Provider 凭据和企业/安装实例证明，再构造 `VerifiedProviderTenantOnboarding` 或
`VerifiedProviderAccountOnboarding`；普通 `IdentityService` 永远拿不到这个 port。I3 没有提供
Channel link 写 API、credential vault、目录调用或管理员 HTTP API。

### 3.13 EIM-I6 provisioning 持久化与并发不变量

I6 增加三张表，并在同一 forward migration 中加固 §3.1/§3.5 的 active membership 与 reverse
identity 唯一性。migration 不为任何 Tenant 回填默认策略；发现存量重复 ownership 时 upgrade fail
closed，而不是任选一行或自动合并。

#### `t_ai_identity_tenant_policies`

每个 Tenant 至多一条权威策略；`id=tenant_id`，并以 `ON DELETE RESTRICT` 引用 active Tenant：

| 字段 | 约束 | 说明 |
|---|---|---|
| `tenant_id` | unique, non-null | 策略所属 Tenant；默认 projection 不回显 |
| `mode` | `preprovisioned/link_only/jit` | 三种模式之一 |
| `revision` | bigint, `>=1` | 每次受控 CAS 更新加一 |
| `link_code_ttl_seconds` | integer `60..900` | link code 的权威 TTL |
| `changed_at` | aware timestamptz | 受控变更时间；不得倒退 |

缺行、非法 mode/TTL/revision 或读取失败都返回 `IDENTITY_POLICY_UNAVAILABLE`；resolver 不采用环境
默认值、历史默认值或“先放行再补表”。首次 onboarding 必须经独立
`ProvisioningPolicyAdministrationRepository.create_policy()` 显式写 mode + TTL；后续只用
`cas_policy(expected_revision, mode, ttl)`，无 delete、generic CRUD 或普通业务写入口。普通 I3/I6
service 只持有 read/provisioning capability，不持有该管理 port。

I3 的 verification-gated plan 必须携该 snapshot 的 `revision`。I6 在完整事务内锁住策略行，要求
plan 的 action、mode 与 revision 精确一致；策略变化使旧 plan fail closed，调用方须从 I3 重新规划，
不能把旧 action 当成长期授权。

#### `t_ai_identity_link_codes`

表内只保存一次性 grant 的 keyed digest，不存在 `code/raw_code/token/secret/payload` 列：

```text
tenant/provider/provider_tenant/provider_account scope
target_user_id
digest_key_id + HMAC-SHA256 code_digest
policy_revision
provider_account_revision + provider_account_last_scope_change_at
state = pending | consumed | revoked
issued_at / expires_at / consumed_at? / revoked_at?
consumed_external_identity_id?
```

raw code 由 24-byte（192-bit）CSPRNG 产生，base64url 编码后只在签发结果中返回一次；DTO `repr` 隐藏
raw code、digest、key id 和目标 ID。digest 使用独立 domain 的 HMAC-SHA256，key 至少 256 bit，并与
`digest_key_id` 一起存储以支持受控轮换；禁止存裸 SHA-256 或可逆密文。解码消费严格要求 24 bytes，
不能用较短 token 降级熵。

TTL 只能来自已锁定 policy snapshot；数据库同时强制 `expires_at>issued_at` 且不超过 15 分钟。签发
新码会在同一事务撤销该 account + target 的旧 pending 码，partial unique index 再保证最多一个
active pending grant。grant 绑定 policy revision、Provider Account revision 与 scope marker；三者
任一变化，旧码统一表现为 `IDENTITY_LINK_REQUIRED`，不向调用方泄露是过期、撤销、猜错、scope
变化还是目标冲突。`consumed` 只允许一次，并通过复合外键绑定最终 ExternalIdentity。

#### `t_ai_identity_binding_events`

该表保存每个 ExternalIdentity 的**首次**绑定证据：`jit/preprovisioned/link_code`、目标 User、可选
actor/link grant、绑定前后 account kind、policy revision、Provider proof 时间、发生时间，以及
`request_digest_key_id + request_digest`。request digest 使用与 link code 不同 domain 的 keyed
HMAC-SHA256。表内不保存 raw subject/alias/open_id/union_id、raw code、姓名、邮箱、手机号、工号或
请求正文；为复合外键完整性保留的 server-owned `provider_tenant_key/provider_account_key` 是 scope
natural-key 列，不是消息断言，并在 safe projection 中隐藏。

repository/application contract 是 append-only：没有 update/delete port；数据库以
`UNIQUE(external_identity_id)`、`UNIQUE(link_code_id)` 和
`UNIQUE(request_digest_key_id, request_digest)` 锁定一个 identity/一次 grant/一次规范请求的首次
事实，并以复合 `ON DELETE RESTRICT` 外键锁定 account、identity 与 grant scope。`link_code_id` 的
nullable unique 按 PostgreSQL 语义只约束非 null code；JIT/preprovisioned 仍由 identity/request 唯一
兜底。downgrade 只有 policy/code/event 三表全空时才允许，任何 provisioning 历史都拒绝销毁。

I6 migration 只增加上述表、约束和索引，不创建 EnterpriseSubject；§3.7 仍是 EIM-I5 的目标表，I6
既不读取也不写 `employee_no`/`EnterpriseSubjectLink`。

---

## 4. Identity Service 接口

### 4.1 I3 当前已实现的解析契约

I3 的核心输入由服务端可信配置构造，不是 Channel payload：

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ProviderContext:
    tenant_id: str
    provider: str
    provider_tenant_key: str
    provider_account_id: str
    provider_account_key: str
    provider_account_revision: int
    provider_account_last_scope_change_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AliasKey:
    alias_type: ProviderAliasType  # v1: open_id | union_id
    alias_value: str


@dataclass(frozen=True, slots=True)
class IdentityResolutionRequest:
    context: ProviderContext
    alias: AliasKey
```

`ProviderContext` 必须来自 server-side Provider Account ownership。revision 与
`provider_account_last_scope_change_at` 共同构成 account generation；两者任一不匹配都不是 alias miss，
而是无效/stale context。它故意没有 `channel_id`；Channel、目录事件、Web OAuth 与管理员预绑定将来
都先解析同一种 context，再复用 identity core。

lookup repository 用**一条 SQL**从精确 account 起点 outer join alias、canonical identity、活跃非匿名
`User` 与当前有效 `UserTenant`，契约严格区分：

```text
None
  = ProviderContext 不存在、scope/revision 不匹配；不得当成 alias miss 或触发 provisioning

IdentityResolutionSnapshot(account=..., identity=None, membership=None)
  = account 有效且健康状态可另行判定，但该 alias 尚未链接；可以进入 policy plan

IdentityResolutionSnapshot(account=..., identity=..., membership=None)
  = link 存在，但当前 User/membership 不满足；必须 inactive/fail closed

IdentityResolutionSnapshot(..., alias_verified_at=<time>)
  = alias proof 时间的 read projection；若早于 account last_scope_change_at，必须 Provider 重验
```

重复有效 membership、跨 scope identity、非 healthy account、非 active identity、无效/匿名/停用用户都
fail closed。解析每次重查 `User/UserTenant`，不能把旧缓存或“以前有效”当当前 membership。

判定顺序也是契约：identity `state=conflict/revoked` 两个终态都先于 alias proof freshness 分类；
conflict 返回 `IDENTITY_LINK_CONFLICT`，revoked 返回 `IDENTITY_INACTIVE`，两者都不提示可重验。这样
scope marker 变化不会把终态降格成普通“请重验”，verified port 也不能复活它们。非终态 alias 早于
marker 时返回 inactive + `provider_verification_required=true`，且不进入 provisioning policy；fresh
alias、active identity 和 live membership 才能 resolved。

当前 service 接口是：

```python
class IdentityService:
    def __init__(
        self,
        repository: IdentityLookupRepository,
        policy_resolver: ProvisioningPolicyResolver,
    ) -> None: ...

    async def resolve_external_identity(
        self,
        request: IdentityResolutionRequest,
    ) -> IdentityResolutionResult: ...
```

`IdentityService` 不持有 ordinary mutation、verified identity mutation、provider-account control 或
ownership repository。返回状态只有
`resolved/missing/inactive/conflict`；resolved 才携带 immutable identity/membership record，仍然不是
P1 Principal。缺 alias 时三种策略只产生：

| mode | action plan | I3 是否立即写库 |
|---|---|:---:|
| `preprovisioned` | `bind_preprovisioned` | 否 |
| `link_only` | `require_link` + `IDENTITY_LINK_REQUIRED` | 否 |
| `jit` | `create_normal_member`（角色上限 `NORMAL`） | 否 |

三种 plan 都必须 `provider_verification_required=true`，并携权威 policy snapshot 的
`provisioning_policy_revision`；plan 不携 link TTL，也不能由调用方覆盖 action/revision。未知 mode、
policy exception、缺失 policy row 或无法取得 policy
返回 `IDENTITY_POLICY_UNAVAILABLE` 并 fail closed；I3 不调用 Contact、不开户、不激活 identity，也不
把“plan”误当成已验证用户。

### 4.2 P1 已实现的 Principal 契约

```python
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AuthenticationSource(StrEnum):
    WEB_SESSION = "web_session"
    SDK_API_TOKEN = "sdk_api_token"
    ENTERPRISE_IDENTITY = "enterprise_identity"


class IdentityAssurance(StrEnum):
    AUTHENTICATED = "authenticated"
    DIRECTORY_VERIFIED = "directory_verified"
    ENTERPRISE_VERIFIED = "enterprise_verified"


@dataclass(frozen=True, slots=True)
class EnterpriseSubject:
    subject_type: str
    subject: str
    issuer: str
    issuer_tenant: str
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticationContext:
    source: AuthenticationSource
    assurance: IdentityAssurance
    validated_at: datetime
    authenticated_at: datetime | None = None
    assurance_verified_at: datetime | None = None
    provider: str | None = None
    external_identity_id: str | None = None


@dataclass(frozen=True, slots=True, init=False)
class Principal:
    platform_user_id: str
    tenant_id: str
    authentication: AuthenticationContext
    enterprise_subject: EnterpriseSubject | None = None
    display_name: str = ""
```

`validated_at` 是 MultiRAG 对本次凭据完成校验的时间；`authenticated_at` 只能是凭据真实
证明的上游人类认证时间，不得用请求时间、token 过期时间或 cache 命中时间伪造。
`assurance_verified_at` 记录权威目录/企业主体 proof 的原始时间，cache 或新请求不得刷新它。
当前 Web session 与 SDK API token 只能产生 `AUTHENTICATED`，`authenticated_at=None`，且不得
填写 provider/external identity/directory proof。企业身份上下文则必须同时有 provider、内部
`external_identity_id` 和 proof 时间；`DIRECTORY_VERIFIED` 严格绑定 I3
`identity.verified_at`，`ENTERPRISE_VERIFIED` 严格绑定 `EnterpriseSubject.verified_at`。

`Principal` 的 direct constructor 已封闭，只能经过两个证据 builder 构造：

```python
build_principal_from_authenticated_actor(
    actor: AuthenticatedActor,
    membership: TenantMembershipEvidence,
    authentication: AuthenticationContext,
) -> Principal

build_principal_from_resolved_identity(
    result: IdentityResolutionResult,
    authentication: AuthenticationContext,
    enterprise_subject_evidence: VerifiedEnterpriseSubjectEvidence | None = None,
) -> Principal
```

这两个 builder 是**进程内 trusted adapter seam**，不是 wire/security boundary；不得把请求 JSON、
Channel command 或其他跨信任边界的任意 DTO 直接传入。企业 builder 只接受 I3 产生的
`RESOLVED` 结果，并再次要求 active identity、live 同 Tenant membership、无 error、无
provisioning action、无 `provisioning_policy_revision`、无 re-verification flag、membership role
严格为 `owner/admin/normal`，以及
provider/internal identity/proof time 逐项一致。
`missing/inactive/conflict` 或仅有 verification-gated plan 的 I3 结果永远不能提升为 Principal。

canonical owner 唯一是 `api.identity.principal.Principal`。`api.utils.api_utils.Principal` 只是对
同一 class object 的兼容 re-export，不是第二个 Principal；存量 route 迁移到
`api.identity.principal` 后删除该 facade。`.id/.nickname` 也只是存量消费方的兼容 property。
Principal 不包含 email、tenant role、groups、scopes、业务权限集合、Provider token 或飞书
原始标识；展示名不是身份键或授权依据，敏感主体字段默认不进 `repr`。角色和业务
权限是变化更快的授权状态，在对应边界重查，不由 Principal 快照携带。

### 4.3 legacy Web/API owner context 与后续组合边界

`api.identity.legacy_owner` 只为现有 Web JWT 与 SDK API token 保留 RAGFlow 个人 owner 语义。
它每次请求重查 active/non-anonymous `User`，并用窄 SQL 要求恰好一条 active
`UserTenant(OWNER)` 且 `tenant_id == user.id`；缺失、重复或停用都 fail closed。它不是通用
active-Tenant selector，不会根据 header/payload 在多 Tenant 间动态选择。P2/C3 必须传入它们自己
经服务端验证的 membership tenant，不能复用该兼容规则。

Web JWT 只有在 JWT 解析明确返回预期的 `not_authenticated` 时才能进入 API-token fallback。
已成功解析的 JWT 如果 User/membership 无效，必须直接拒绝，不得再解释为另一类凭据；
未预期 verifier/runtime 异常必须向上传播，`DISABLE_SDK` 必须继续阻断 fallback。

这个兼容面只覆盖 async `async_current_user`。存量 sync `Depends(manager)` 仍可能返回 ORM User，
`async_current_tenant_id` 的 broad fallback 也是既有债务；P1 不得宣称所有入口已统一为 Principal。
P1 此轮本身不包含 C3 Channel 组装、P2 全链传播、A2/P3 动态委托或 A7 inbound Resource Server；
C3 已在后续独立 composition 中实现并完成部署 live。

旧提案中的 `EnterpriseIdentityService.resolve_channel_actor(tenant_id, channel_id, ...) -> Principal`
**不再是 identity core 契约**。当前 C3 adapter 位于 `api.identity_adapters.channel_runtime`，按
`channel_id -> IdentityProviderChannelLink -> ProviderContext` 组合 I3、I4、I6 与 P1 builder；
`channel_id` 只留在该 adapter，不重新进入通用 identity repository/service。active resolved identity 的
每消息重验由 I6 的窄 `reverify_resolved_identity` 用例完成；目录事件消费与批量兜底仍分别属于 I7/I8。

Provider SPI：

```python
class EnterpriseIdentityProvider:
    async def resolve(
        self,
        context: ProviderContext,
        assertion: ExternalIdentityAssertion,
    ) -> ProviderIdentityResult: ...

    async def refresh(
        self,
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult: ...
```

I4/I4.1 已将此 SPI 与 live Auth response 兼容落到 `api.identity.providers`。domain contract
只接受服务端已固定的
`ProviderContext`，不接受 `channel_id/app_id/tenant_id` 等 payload 路由字段。精确结果形状是：

```text
ProviderIdentityResult
  status = resolved | not_found | not_in_scope | inactive | unavailable | invalid | conflict
  identity = ProviderIdentity | null
  error_code = stable ProviderErrorCode | null
  retryable = bool
  from_cache = bool

ProviderIdentity（只有 resolved 携带）
  provider
  provider_tenant_key
  provider_account_id
  provider_user_id
  verified_at
  open_id? / union_id? / employee_no? / display_name?
  provider_status = active
```

ID/Secret/个人字段均在 dataclass `repr` 中隐去。`verified_at` 是本次真实 Contact proof
时间；cache hit 只返回原 proof，不用当前请求时间伪造更新的验证时间。

Feishu 运行时链固定为：

```text
ProviderContext
  -> ProviderCredentialResolver.resolve(context)
  -> official Auth V3 tenant_access_token.ainternal()
  -> official Tenant V2 tenant.aquery() == context.provider_tenant_key
  -> official Contact V3 user.aget(user_id_type=open_id|user_id)
  -> strict primitive/status validation
  -> allowlisted ProviderIdentityResult
```

Auth V3 使用 `lark-oapi==1.7.2` generated async request/resource/transport + strict live top-level
wire response adapter；Tenant V2/Contact V3 保持 generated typed nested response，并显式传递
project-scoped `tenant_access_token`。不走 SDK 同步 `TokenManager` cold path，也不重写官方 HTTP/签名。
SDK import 保持 lazy，只在实际 Provider call 发生，导入 `api.identity.providers` 本身不安装 SDK
模块级 loop。

live contract 另有一个 1.7.2 generated-model mismatch：`InternalTenantAccessTokenResponse` 的生成
类型只声明 `data: InternalTenantAccessTokenResponseBody`，但有效 Auth V3 成功响应将
`tenant_access_token/expire` 放在 JSON 顶层。I4.1 继续复用官方 async transport/签名，同时对实际
顶层字段做有界、严格、脱敏的兼容解析；production adapter sandbox 已验证该链。

三个 endpoint 的 adapter response 都必须保留 `(http_status, business_code)` envelope：业务码为 0
也不能覆盖非 2xx HTTP 失败。业务码只能结合 endpoint stage 解释，禁止使用跨 Auth/Tenant/Contact
的全局 code 集合：`10003` 不是 credential code；Auth credential mismatch 当前为 `10015/20002`。
Auth V3 与 Tenant V2 是 credential/tenant ownership 控制面；其 403/404 永远不能映射为 identity
`not_in_scope/not_found`，否则 I6 会被错误诱导进入 link/JIT。只有 Contact V3 用户查询面可以产生
这两个 identity 结果；Contact 也只有在 business code 为 0 时才允许用 HTTP 403/404 fallback。
未知或瞬时非零 Contact code 即使搭配 403/404，也必须保持 invalid/unavailable 等非 JIT 结果，
不得降级为 `not_in_scope/not_found`。

`ProviderCredentialResolver` 是独立 port。当前唯一 concrete adapter 在
`api.identity_adapters.channel_credentials`；它用 account-rooted 单 SQL 同时要求精确
Provider Account revision/scope marker、唯一 link、同 Tenant/Provider Channel、公开 `app_id/domain`
和一条 `ChannelSecret`，再由注入的 `SecretStore` 解密。credential 只在调用期存活；
`credential_generation=ChannelSecret.version`。明文 `app_secret` 出现在 Channel JSON、缺失/多条
结果、scope/account/revision 不匹配、Provider Account 停用或解密失败都 fail closed，不会按
`app_id` 猜测。这是 I2.1 到通用 Provider credential vault 之间的过渡 adapter，不改变
`ChannelSecret` 仍是当前 credential owner 的事实。

I4 cache/rate contract：

- account generation key 包含 provider account id、identity revision、scope marker、credential generation
  和 Feishu/Lark domain；不把 app secret/token 本身放入 key；
- token cache 最多 512 项，directory cache 最多 2048 项，distinct in-flight 最多 512；
  LRU/expiry 使用注入的 monotonic clock；
- resolved 正向 cache 300 秒；`not_found/not_in_scope/inactive` 负向 cache 30 秒；其他失败
  不缓存；token 在 Provider expiry 前 600 秒失效；
- 同 key cold miss single-flight；跨 account/revision/scope/secret-version/domain 不合并；一个等待者
  取消不会取消 shared producer，producer 失败/取消不进 cache 且会释放 slot；
- Contact 每 account 最多 15 calls/s，排队超过 2 秒即可重试 unavailable；这个限流
  不放宽 Provider 自己的 429/配额判定。

Contact response 只投影 `user_id/open_id/union_id/employee_no/name/status`；空的可选字符串
归一为 `None`，required ID 仍拒绝空值。`UserStatus` 的 `is_frozen/is_resigned/is_activated/
is_exited/is_unjoin` 必须全部是真 `bool`；只有 activated 且其余四个全为 false 才产生
resolved。SDK 对 primitive 的宽松 unmarshal 不是项目的安全判定。

I4 只返回 Provider proof，不调用 I3 write capabilities，不保存 Provider tenant ownership/
ExternalIdentity/Alias/User/UserTenant，也不构造 Principal。首次 ownership 持久化与 verified
identity 事务仍属 I6/onboarding composition，Contact event invalidation 的持久化属 I7。

Enterprise subject SPI：

```python
class EnterpriseSubjectResolver:
    async def resolve(self, provider_identity: ProviderIdentity) -> EnterpriseSubjectResolution: ...
```

Provider SPI 属于 I4，Enterprise subject SPI 属于 I5；P1 没有实现这两个 SPI。

### 4.4 P2 execution Principal 传播契约（已实现）

P2/CHN-X18 的唯一可信输入是 C3 已构造并放入 `TrustedChannelContext.principal` 的 canonical
`api.identity.principal.Principal`。worker assertion、legacy subject、DSL、模型输入、MCP arguments、
custom header 或调用方传入的 `user_id` 都不能创建、替换或补全它。LINKED context 必须同时满足
`principal_id == principal.platform_user_id` 与 tenant/provider 证据一致；任一缺失或漂移 fail closed。
NO_LINK 才允许服务端明确选择 legacy anonymous，不能把 linked failure 降级成 `""`。

传播链已实现为显式参数/不可变 run context：

```text
TrustedChannelContext.principal
  -> target executor / driver
  -> Dialog or Canvas completion
  -> Canvas Graph/component construction (before load/prewarm)
  -> Agent / RAG / Memory / Canvas workflow / MCP call context seam
```

MultiRAG 内部最终形状为 `api.identity.run_context.RunContext`：它是 `frozen + slots` 的进程内
值对象，只持 `tenant_id` 与 `principal: Principal | None`，两个字段均不进入 repr。LINKED 只允许
`principal` 非空且 tenant 一致；`principal.platform_user_id` 是 session/Memory user key 的唯一可信
来源，不再复制一份可漂移的 `principal_id`。`principal=None` 只表达服务端已经明确判定的 NO_LINK
legacy；非 Channel 调用方省略 run context 时继续保持原兼容行为。

Canvas/Graph 持有该对象的实例级引用，并在 `Graph.load()` 构造组件前完成注入；组件只能通过
Graph getter 读取，不能从 DSL、`sys.*` 或模型参数重建。MCP client wrapper 只接收一个不透明的
instance-local call-context 引用，供本次工具调用的后续授权层消费；P2 不解释它、不序列化它，也不
据此修改 header、transport 或 arguments。`common` 继续不依赖 `api.identity`。

- Canvas 组件与 MCP session 在 `run()` 前构造，因此只在 `Canvas.run(user_id=...)` 写 globals 太晚；
- Principal 或 run context 不得进入 DSL、`sys.*` globals、prompt、model-visible history、SSE、数据库
  message payload、static MCP headers、日志或 repr；不得用 module-global/隐式 ContextVar 在并发 run 间传递；
- session/history 与 Memory 读写至少按 `(tenant_id, platform_user_id)` 隔离。同租户两个 Principal 即使
  复用同一 Channel conversation、Agent、Memory 配置或攻击者 DSL `user_id`，也不得互读/互写；
- retry、regenerate、cancel 与同一 run 的 MCP/tool call 必须携带同一个不可变 Principal context；
  target owner 的现有 C3 纵深校验继续保留，P2 不重做 identity mapping；
- MCP 侧本轮只接收 request/run context seam。P2 不签发 token、不查用户 credential、不写
  `Authorization`/bearer、不修改静态 server headers；A2/P3 才实现动态委托；
- 这里的 Workflow 是当前 Channel 可达的 Canvas Graph/component workflow，不扩到独立
  `workflow/`、`workflow_v2/` 或 `api.run_platform`，也不顺手改公开 Memory CRUD/PDP。

失败优先测试已证明 linked 不再命中 `principal_id or ""`，同时保留 NO_LINK legacy；Dialog 与
Canvas 均覆盖同对象传播，Memory 覆盖可信 user 覆盖、跨 tenant 拒绝与当前 `msgStoreConn` 的同租户
双用户隔离，MCP session 的 context 为实例级引用，repr/DSL/序列化不出现平台或外部主体原值。

### 4.5 I6 当前已实现的 provisioning/link 契约

I6 的 framework-neutral application service 只接受 I3 plan 与 I4 proof，不接受调用方提供
`target_user_id`、mode、action、policy revision、membership role 或 account kind：

```python
class IdentityProvisioningService:
    async def provision_verified_identity(
        self,
        request: ProvisionIdentityRequest,
    ) -> ProvisioningResult: ...

    async def issue_link_code(
        self,
        request: LinkCodeIssueRequest,
    ) -> LinkCodeIssueResult: ...
```

`ProvisionIdentityRequest` 必须逐项包含原 I3 `IdentityResolutionRequest`、对应
`IdentityResolutionResult`、fresh I4 `ProviderIdentityResult`，以及 `link_only` 时可选 raw code。
service 只接受两类 verification-gated 输入：

1. `MISSING + action + provisioning_policy_revision + provider_verification_required=true`；
2. scope marker 变化产生的 `INACTIVE + IDENTITY_INACTIVE + provider_verification_required=true`，且
   action/revision 都为 null。第二类只用于 fresh proof 后按 canonical active identity 刷新 stale alias；
   真正 state 为 `inactive/conflict/revoked` 的 identity 仍 fail closed，不能自动恢复。

I4 proof 必须为 `RESOLVED/active`，provider、tenant、account、asserted `open_id` 与 context 精确
一致，`verified_at` 为 aware time、不晚于 service 捕获的当前时间、距该时间不超过 5 分钟，也不得
早于 account scope marker。合法 101～512 字符 display name 不会因为 `User.nickname` 上限而被拒绝：I6
只做 NFKC + 空白规范化并截为 100 字符；空值使用稳定非 PII 展示名。display name 从不参与匹配。

service 把 proof 归一成 `VerifiedProvisioningCommand`：canonical subject 固定为飞书 `user_id`，alias
只允许本次已验证的 `open_id` 和可选 `union_id`；request fingerprint 使用 keyed、domain-separated
HMAC 并同时携 `request_digest_key_id`。repository port 只有两个完整用例：

```python
class IdentityProvisioningRepository(Protocol):
    async def provision_verified_identity(
        self, command: VerifiedProvisioningCommand
    ) -> ProvisioningResult: ...

    async def issue_link_code(
        self, command: LinkCodeIssueCommand
    ) -> LinkCodeGrantRecord: ...
```

port 不暴露 CRUD、session、commit/rollback 或单表半写。SQLAlchemy adapter 为每个完整用例开启一
个 fresh async transaction，写入前锁定/recheck Provider Account generation 与 policy generation。
事务开始取得的数据库时间只可用于廉价初筛；等待所有可能阻塞的 policy/account/canonical/target/
grant 锁之后，必须以 PostgreSQL `clock_timestamp()` 重新取得真实墙钟，再检查
`proof.verified_at <= post_lock_now`、proof 未超过 5 分钟以及 pending link code 尚未过期。首次写入、
grant consume/revoke 与 binding event 的发生时间统一使用这个 post-lock time，不能让 lock wait
冻结 `CURRENT_TIMESTAMP` 而延长 proof/code 的有效窗口。Provider API 已在事务外完成；事务内只消费
fresh proof 与服务端权威行，避免外部网络调用占住数据库锁。

三种模式的原子结果固定为：

| mode | target 来源 | 事务写入 | 明确禁止 |
|---|---|---|---|
| `preprovisioned` | 已存在 `pending_link` identity 的 `user_id` | 验证 live User + active membership，写 alias、activation、首次 event；local 变 hybrid | 创建 User/Tenant/membership；按显示字段找人 |
| `link_only` | 只来自已登录 Principal 签发并锁住的 grant | 消费同 scope/revision 的 pending code，绑定/激活 identity，写首次 event；local 变 hybrid | caller 传 target、猜码枚举、把两个 User 合并 |
| `jit` | repository 新生成 opaque User ID | 创建 external-only User + active `UserTenant(NORMAL)` + canonical identity + alias + 首次 event | email/password/access token、个人 Tenant、OWNER/ADMIN、employee_no/subject |

所有写要么一起提交，要么一起回滚。canonical identity 自然键和 reverse slot、active membership
partial unique、advisory/row lock 共同保证并发首次消息收敛；数据库约束而不是“先查后插”承担最后
防线。已有 active canonical identity 可在 fresh proof 下返回 `ALREADY_BOUND` 并只前进 alias/
verified/last-seen 时间；不存在 code 时不因 `link_only` plan 再要求绑定。但如果调用方显式提交 code，
repository 必须验证它与 canonical target/首次 event 一致，不能悄悄忽略冲突的显式绑定尝试。

这里的“显式 link/绑定”是把**一个外部 canonical identity**绑定到**一个已经认证的现有 User**，并
可能把该 User 从 `local` 提升为 `hybrid`；它绝不是把两个 `User` 行及其会话、知识库、Memory 或
业务数据合并。若 canonical identity 已属于另一个 User、reverse slot 被占用、User/membership
inactive/歧义，均 fail closed。姓名、邮箱、手机号和 `employee_no` 永不用于选择 target。

link code 的 invalid/expired/revoked/stale/scope-changed/target-conflict 等用户可探测分支统一返回
`IDENTITY_LINK_REQUIRED`，避免持 fresh Provider proof 的调用方把响应当成 code-validity oracle；
`IDENTITY_POLICY_UNAVAILABLE` 与 `IDENTITY_REPOSITORY_UNAVAILABLE` 保留为运维可诊断的稳定失败。

I6 当前只交付 domain、schema/migration、async PostgreSQL repository/application transaction 与测试。
它不提供 HTTP route、管理员或用户 UI、Channel adapter、C3 Principal 构造/传播、I5
EnterpriseSubject、I7 事件消费，也不修改 FastMCP/of_mcp 的工具开放或授权运行时。

### 4.6 I6.1 已实现的企业连接 onboarding 契约

受控 CLI 只接受现有 Feishu Channel ID、明确 mode 与 TTL；App ID/Secret、tenant key 和 Provider
Account 都不能作为命令行 authority。`plan()` 先验证独立 Identity HMAC keyring readiness，再关闭
数据库读 session、解密 Channel Secret，并在无数据库事务时调用官方 Auth V3 + Tenant V2。公开 plan
只含 mode、TTL 和 action 枚举；tenant/account/channel/credential/proof 都留在进程内 sealed intent，
同一 service 实例只允许 exact plan object apply 一次，复制、篡改或重放均拒绝。

`apply()` 在一个 fresh async transaction 中按固定顺序锁 Channel、Tenant、tenant/provider advisory
domain、policy、Provider Tenant/Account/link 与 Secret，重新核对 Channel generation、公开配置 digest、
Secret version/envelope digest、外部 tenant proof 的 5 分钟窗口和 policy shape。允许同一外部企业在
同一 MultiRAG Tenant 下有多个 Provider Account；跨 Tenant ownership、account/channel rebind、已有
policy 的 mode/TTL 漂移均 fail closed。网络验证不进入写事务；最终 Provider Tenant/Account/Policy/
Link 全成或全败，重复 dry-run/apply 对已匹配状态产生零 action。

Identity HMAC keyring 与 Channel Secret 加密 key 独立，每把 key 至少 32 bytes；生产材料只能注入 API
进程的 mode `0600` secrets env，不进入仓库、supervisor/worker 参数或日志。I6.1 是运维 CLI，不是
公开 HTTP/UI，不创建 ExternalIdentity/User/UserTenant，也不让 execution consume assertion；C3/X7
仍负责消息到 Principal 的组合。

---

## 5. 身份状态与错误码

稳定 error code：

| Code | HTTP/执行语义 | 含义 |
|---|---|---|
| `IDENTITY_ASSERTION_INVALID` | 400/private command reject | ID 缺失、重复或不合法 |
| `IDENTITY_PROVIDER_MISMATCH` | 403 | assertion provider 与 binding 不一致 |
| `IDENTITY_TENANT_MISMATCH` | 403 + security audit | 飞书 tenant_key 与已验证 account 不一致 |
| `IDENTITY_NOT_IN_SCOPE` | 403 | 用户不在应用通讯录数据范围 |
| `IDENTITY_INACTIVE` | 403 | 冻结、离职、退出或平台禁用 |
| `IDENTITY_LINK_REQUIRED` | safe user result | `link_only` 需要一次性绑定 |
| `IDENTITY_LINK_CONFLICT` | 409/fail closed | canonical/alias/企业主体冲突 |
| `IDENTITY_NOT_FOUND` | safe domain result | mutation 目标不存在；不泄露其他 Tenant 是否存在同 ID |
| `IDENTITY_REVISION_CONFLICT` | 409/retry from fresh context | account/identity revision 已变化，stale writer 不得覆盖 |
| `IDENTITY_TRANSITION_INVALID` | 409/fail closed | 非法状态转换；普通路径不能激活或复活终态 identity |
| `IDENTITY_OWNERSHIP_CONFLICT` | 409/security audit | verified onboarding 或写命令与固定 tenant/provider ownership 不符 |
| `IDENTITY_POLICY_UNAVAILABLE` | 503/safe domain result | provisioning policy 缺失、无效或读取失败；不采用默认放行 |
| `IDENTITY_REPOSITORY_UNAVAILABLE` | 503/safe domain result | repository 无法安全判定结果；错误不含 SQL/Provider 标识 |
| `IDENTITY_PROVIDER_UNAVAILABLE` | 503 | 飞书/OA 暂时不可用且无可接受 cache |
| `IDENTITY_PROVIDER_CREDENTIAL_UNAVAILABLE` | 503 | 精确 Provider Account 无唯一、scope-safe 且可解密的 credential |
| `ENTERPRISE_SUBJECT_REQUIRED` | 403/tool error | 当前工具需要 talent/workcode，但未解析 |
| `IDENTITY_ASSURANCE_INSUFFICIENT` | 403/step-up | cache freshness 不满足操作风险 |

Provider 的 `IDENTITY_NOT_FOUND/IDENTITY_NOT_IN_SCOPE` 只允许来自 Contact 用户查询面；Auth/Tenant
控制面即使返回 HTTP 403/404，也必须保持 provider/credential unavailable，不能形成开户、绑定或
JIT 所消费的 identity miss。business code 必须先按 endpoint stage 解释；Auth 的
`10015/20002` 才是当前 credential mismatch，`10003` 不得再作为全局 credential code。Contact
HTTP fallback 必须满足 code 0；任何未知/瞬时非零 code 都不能借 403/404 触发 JIT。

用户可见文案不包含具体权限、内部 ID、组织状态细节；管理员通过 trace/audit 查原因。

---

## 6. MCP token profiles

EIM-A1 冻结的是本项目私有、**RFC 9068-shaped** 的 JWT profile，不表示 MCP 规范要求所有实现
使用 JWT 或 ES256。MCP Client 把 token 当 opaque bearer；只有 issuer 与 Resource Server 解释
claims。Channel `ExternalIdentityAssertion` 不属于本节，不能被任一 Resource Server 当作 token。

### 6.1 两个 profile 的共同 JOSE、时间与 resource 规则

- JOSE header 必须且只能使用 `typ="at+jwt"`、`alg="ES256"` 和非空 `kid`；`alg` 不从 token
  动态选择。拒绝 `none`、其他算法、未知 `crit`，以及试图通过 `jku`、`x5u`、内联 `jwk` 改变
  服务端固定信任源的 header。
- compact JWT UTF-8 序列化后默认不得超过 4096 bytes，超限在密码学解析前拒绝。
- `iss` 和 `aud` 都是大小写敏感的 canonical HTTPS URI。`aud` v1 必须是单个 JSON string，拒绝
  array/multiple audience；校验不做尾斜杠、默认端口、host alias 或 path 的隐式归一化。
- `iat`、`nbf`、`exp` 都是 integer NumericDate。issuer 默认令 `nbf=iat`；verifier 独立校验
  `nbf<=exp`、`iat<exp`、最大 TTL 和当前时间。允许的 clock skew 固定 30 秒；超出该窗口的未来
  `iat/nbf` 或过期 `exp` 拒绝。
- scope 使用 RFC 6749 的 `scope-token` 字符范围和单个 ASCII space 分隔；拒绝空项、重复项、控制
  字符和未在目标 resource registry 登记的 token。一个 token 包含调用当前工具不需要、但已登记
  且对同一 resource 有效的额外 scope 时，token 仍然有效。
- token、JWKS 和校验上下文都按 profile 固定 issuer、keyset、resource、允许的 `token_use` 和
  claim allowlist/scope registry；未知 claim fail closed，不能因两个 profile 使用相同用户或 scope 名
  就互相接受。

### 6.2 `mcp_access`（MultiRAG -> of_mcp Gateway）

算法固定 ES256；默认/最大 TTL 为 300 秒。必需 claims：

| Claim | 语义 |
|---|---|
| `iss` | MultiRAG Authorization issuer canonical HTTPS URL |
| `sub` | 不透明 `platform_user_id`；不是 Provider ID、邮箱或工号 |
| `aud` | of_mcp gateway canonical MCP resource URI，单个精确 string |
| `client_id` | 预注册 MultiRAG MCP client |
| `iat` / `nbf` / `exp` | 短时委托；`exp-iat<=300` |
| `jti` | 全局不可预测的唯一 token ID；日志只记录 hash |
| `scope` | 目标 resource 已登记 scope 的空格分隔字符串 |
| `tenant_id` | MultiRAG tenant；签发前从 Principal/服务端执行上下文取得 |
| `agent_id` | 发起工具选择的已发布 Agent |
| `token_use` | 固定 `mcp_access` |

条件 claims：

| Claim | 出现条件与语义 |
|---|---|
| `auth_time` | 上游确实证明认证时间时；integer NumericDate，不能晚于 `iat`；A4 只携带该值，未实现 freshness/max-age |
| `acr` | 已验证的 assurance class；A1 按 profile `allowed_acr_values` 校验，A4 可按工具 policy 要求允许值集合 |
| `amr` | 已验证的 authentication methods；A1 校验非空、无重复且均在 `allowed_amr_values`，A4 可按工具 policy 要求完整 method 集合 |
| `enterprise_subject` | 目标 service 明确要求时；`{type, issuer, subject, tenant}` |

`enterprise_subject.subject` 是面向目标 resource 的不透明业务主体，`type` 固定为 service 声明的
`workcode/talent_id/...` 类型；`tenant` 是该 issuer 的局部边界，不等于可由调用方选择的 MultiRAG
`tenant_id`。issuer 只选择该类型的已验证 link 后签发；Resource Server 必须同时匹配 `type`、
`issuer` 和 `tenant`，不能“随便取第一个”。主体缺失不使普通
低风险 token 的认证失败，只会让要求该 assurance 的操作在授权层拒绝。

不允许的 claims：`authn_provider`、role、group、department、`open_id`、`union_id`、Provider 原始
user ID、姓名、邮箱、手机号、员工号明文、飞书/OA access token、Channel message/chat ID、表单值、
确认状态、模型提示词。Provider 和认证来源留在 MultiRAG Principal/审计；只有可验证保证才映射为
标准 `auth_time/acr/amr`。

首期 canonical resource 示例：

```text
https://mcp.example.internal/mcp
```

Token Broker 最终签发的 scopes 固定取：当前工具需要 scopes、已发布 Agent policy、tenant policy、
当前用户 grants 与 assurance policy 的交集；调用方请求不能扩大结果。

### 6.3 认证、scope 与业务授权的错误分层

| 情况 | HTTP / OAuth 语义 | `failure_reason` / 运维指标族 |
|---|---|---|
| JOSE、签名、issuer、audience、时间、必需 claim、类型、profile 或 `token_use` 无效 | 401 `invalid_token` | 精确 reason（如 `signature_invalid`/`audience_invalid`）/ `MCP_TOKEN_INVALID` |
| token 有效，但缺当前工具全部所需 scope | 403 `insufficient_scope`；一次返回完整所需 scopes | `required_scope_missing` / `MCP_SCOPE_DENIED` |
| tenant/membership/role/policy/业务对象拒绝 | 403；`oauth_error=null`，不返回 scope challenge | `tenant_mismatch` 或策略 reason / `MCP_AUTHORIZATION_DENIED` |
| 当前工具要求企业身份保证但 token 未携带/类型不符 | 403；`oauth_error=null`，不返回 scope challenge | `enterprise_subject_required`/`enterprise_subject_type_mismatch` / `MCP_ASSURANCE_REQUIRED` |
| 工具 policy 声明需要 external resolver，但 resolver 缺失、失败或返回非法结果 | 500 `authorization_invariant_failure`，fail closed；不伪装成用户 403 | 内部 `policy_resolver_required`/`policy_resolver_failed`/`policy_resolver_invalid`；公开响应不泄露细节 |
| JWKS 不可用且没有仍新鲜的可信缓存 | 503，fail closed | `verifier_unavailable` / `MCP_VERIFIER_UNAVAILABLE` |

缺少或畸形 `scope` 属于无效 token；缺少某个合法工具 scope 属于认证成功后的
`insufficient_scope`。额外的合法已登记 scope 不导致 token 失效。`tenant_id` claim 缺失/类型错误
是 401；有效 token 的 tenant 与当前服务端 resource context 不匹配是 403 authorization denial。
角色或业务策略拒绝不得伪装成 step-up scope，否则会泄露策略并诱导客户端无意义重试。

以上表格是 **A3+A4 的当前错误契约**，不能把它误读为 A3 单独交付全部分支。A3 已实现的
Resource Server 认证边界公开两类结果：客户端 bearer/profile 无效为 401；verifier/JWKS trust source
不可用且无新鲜可信 cache 为 503。FastMCP 的 endpoint-level `required_scopes` 仍保留并有独立 403
HTTP 回归，但生产 Gateway 固定传空列表；否则把所有 enabled services 的 scope 放在一个 endpoint
门槛上，会错误要求最小权限 token 同时拥有所有服务权限。

A4 已把验证后的 claims 构造为 immutable Principal，再按当前 tool 的完整 required scopes、tenant、
enterprise assurance 和配置化业务 policy 决策。外层 ASGI authorization preflight 对直接 MCP
`tools/call` 返回真实 HTTP 403；内层 FastMCP middleware 用同一 registry 过滤 `tools/list`，并在
`tools/call` 实际执行前再次授权。二次检查是 TOCTOU 防线，列表不可见和一次 preflight 都不能替代
执行点授权。FastMCP 默认 SSE 可能先发 HTTP 200，因此外层 response guard 必须保留响应起始消息，
直到内层检查允许或给出覆盖结果；二次拒绝与授权基础设施故障仍分别输出真实 403/500。A3 前后都
不得因为 token 在认证层通过，就推断它有权调用列表中的工具。

当前 external membership/role/business resolver 只是 fail-closed 扩展接口，启用的 service policy
没有声明这些 external requirements。resolver 输入虽使用不可变 canonical JSON bytes，但尚未形成所有
工具共享的 schema-normalized 业务对象 contract；因此不能据此宣称 production business-object
authorization 已完成。DISCOVER 阶段没有具体业务对象上下文，带 external requirement 的 future tool
当前会保守隐藏；M2 在任何实际 service 启用前还必须冻结 discovery UX 与对象输入契约。该能力必须在
M2 结合验证后的 tool input 与权威业务系统再落地。

#### 6.3.1 逐工具 policy registry 与 contract snapshot

`service.toml.scopes` 只登记服务可用的 scope 词表；`tool_policies` 才给每个工具分配
`required_scopes`、`effect`、`replay_mode`、`accepted_acr_values`、`required_amr`、可选
`enterprise_subject` 和 `external_requirements`。`effect` 只允许 `read|prepare|side_effect`，
`replay_mode` 只允许 `reusable|single_use`，且所有模型层都必须强制 `side_effect => single_use`；不能
根据工具名、description、scope 名或 annotation 临时猜风险。registry 必须在所有 mount/namespace
assembly 完成后，针对最终 canonical tool catalog 一次性构造：

- 每个暴露工具必须且只能有一个 policy；缺失或孤儿 policy fail closed；
- policy scope 必须属于当前 service 的 scope 词表；secure profile 是否属于 A1 resource registry 仍由
  A3 的 profile assembly 门禁单独校验，local 的 `hello:greet` 不因此伪装成 production scope；
- 重复 canonical tool name、service namespace/canonicalization collision 在启动时拒绝；
- policy model、registry 和投影后的 Principal 都是不可变对象，调用期间不能被原位改写；
- 发现与执行只消费同一个 registry，不允许维护两套逐渐漂移的策略表。

`ofmcp contract` 必须生成并校验排序稳定的 `apps/gateway/contract/tool-policies.json`。快照包含
`snapshot_format`、`profile`、service id/namespace/scope 词表，以及每个 canonical tool 的 service id、
required scopes、effect/replay mode、ACR/AMR、enterprise subject 和 external requirements。
`policy_revision` 等于移除 revision 字段后的 canonical JSON document 的 SHA-256；当前值为
`7bf9e09082ca4f1d529e51bf3fe8e6dd5c4334c62deaf9a5b6be204af9fca446`（snapshot format 2）。策略变更
必须显式刷新快照并经过 contract review；Gateway runtime、A6 审计和后续 P3 token/cache key 必须
调用同一个 canonical builder/已发布 snapshot 获取同一 revision，不从硬编码、文件 mtime 或未排序
映射推导。

#### 6.3.2 A6 执行审计与单 capability replay contract

A6 的 security coordinator 是 framework-independent domain boundary。Gateway 只能在 A4 内层
`tools/call` **最终授权允许之后、实际业务 `call_next` 之前**调用它，并必须传入同一次 evaluation 的
完整 immutable `ToolPolicy`、runtime `policy_revision`、已验证 Principal 与 canonical arguments；
禁止把外层 preflight 的旧决定、调用方 `_meta` 或 header 重新解释成执行许可。

`single_use` 的 replay claim key 固定为 domain-separated digest：

```text
sha256(domain, token_use, issuer, audience, jti)
```

它表示整枚 token/JTI 的**一个 capability**，不是“每个工具一个 key”。request fingerprint 才使用
keyed HMAC 绑定完整 Principal（platform user、tenant、agent、client）、canonical tool name、
`policy_revision` 与 canonical JSON arguments。由此得到以下硬语义：

- 同一 JTI + 同一 fingerprint：`409 duplicate_operation`，只拒绝，不返回或重放先前结果；
- 同一 JTI + 不同参数、主体、工具或 policy revision：`403 replay_detected`；
- replay store 或 pre-execution audit 不能给出可信结果：`503`，fail closed；
- 上述响应都 `Cache-Control: no-store`，不发 OAuth `WWW-Authenticate`，且业务函数必须零调用；
- `read/reusable` 不消费 replay key，但仍必须写 pre-execution audit；它不是绕过 A4 的匿名快路。

P3 已按每次逻辑工具执行签发新的短期 token/JTI；把一枚 token 用于多个副作用工具调用仍被本契约
有意拒绝。该接线默认关闭且尚未 rollout，secure 又因没有 production-ready coordinator fail-fast，
因此这条纪律尚未取得真实 MultiRAG delegated bearer E2E 证据。

claim 生命周期是 `CLAIMED -> DISPATCHED -> SUCCEEDED|FAILED_NO_EFFECT|OUTCOME_UNKNOWN`。进入业务
代码前必须已完成原子 claim、pre-execution audit 和 `DISPATCHED`；成功才能记 `SUCCEEDED`。当前
Gateway 对 `ToolResult.is_error`、业务异常和取消都保守记录 `OUTCOME_UNKNOWN`，因为 transport error
不能证明外部副作用未发生。post-dispatch outcome store 写失败时保留 `DISPATCHED`，只产生脱敏安全
日志，不得篡改已形成的业务响应或释放 claim。只有未来能以权威证据证明“未产生副作用”时才可记
`FAILED_NO_EFFECT`。

审计 schema 必须 frozen/allowlisted，禁止任意 `metadata`/attributes bag。v1 允许字段包括：

```text
schema/event type/event id/time, trace_id, mcp_call_id
runtime, token_use, token issuer/audience, token_jti_hash
principal/tenant/agent/client HMACs
tool_name, effect, replay_mode, policy_revision, request_fingerprint
decision, reason_code, replay_state
```

低熵主体标识必须使用 secret-keyed HMAC；JTI 作为高熵关联值只存 issuer-domain-separated SHA-256，
避免不同 issuer 恰好复用同一 JTI 时审计关联碰撞。绝不保存 bearer、原始 JTI、
tool arguments/results、Provider ID、姓名/邮箱/电话/员工号、enterprise subject、聊天/表单/医疗正文或
任意异常原文。HMAC key 至少 256 bit；当前只接受显式注入，生产 KMS ownership、generation、轮换和
旧摘要查询窗口仍是 A6 未完成项。

OTel 是可观测面，不是安全依赖。phase 1 adapter 只通过 OpenTelemetry API 丰富 current span，并用
`effect/replay_mode/result` 等低基数属性计数；tool name 与 policy revision 只放 span，不做 metric
dimension，用户/tenant/client/JTI/fingerprint/参数/结果一律不进入 telemetry。不配置 SDK/exporter 时
no-op，任何 telemetry 异常都不能改变认证、授权、replay 或业务结果。跨仓 W3C trace propagation、
SDK/provider/exporter/collector 和 retention policy 仍未实现。

`ReplayClaimStore.multi_instance_safe` 与 `AuditSink.durable` 都为真时 coordinator 才可
`production_ready`；这两个属性必须由实现结构保证，不能由普通配置布尔值伪造。内置 memory store/
sink 有界、进程内、重启丢失，仅供单元测试与显式 test-only 启动。secure Gateway 必须显式注入
coordinator；无 production-ready 后端时真实 secure CLI 必须启动失败，remote-release gate 不得解除。

本 replay contract 不是业务幂等或结果缓存：它只能阻止相同 capability 再次进入本进程执行边界，
不能证明 OA/Jira 是否已经提交，duplicate 也不会回放结果。M3/M4 仍须用业务 idempotency key、状态
查询和 unknown-outcome 对账闭环。A5 已在 audit schema v2 加入 actor token 的
`parent_jti_hash`；这只建立父子 capability 的不可逆关联，不替代 durable audit 或业务幂等。

### 6.4 JWKS 和轮换

- issuer 通过 HTTPS 提供只含 public EC keys 的 JWKS；private key 只在 issuer secret/KMS。
- 每个 key 固定 `kty=EC`、`crv=P-256`、`use=sig`、`alg=ES256` 和唯一 `kid`；拒绝重复 `kid`、
  私钥参数或与 profile 不一致的 key metadata。
- 新 key 先加入 JWKS，再开始签发；旧 key 保留至少“最大 token TTL + clock skew + cache TTL”。
- verifier 只访问配置的 issuer/JWKS URI，按 `kid` 缓存；未知 `kid` 最多触发一次受限刷新。刷新失败
  且 cache 过期时返回 verifier unavailable，不把基础设施故障误报成用户 token 无效。
- A3 fetcher 固定 absolute HTTPS URI，拒绝 userinfo/fragment、重定向和环境代理；网络超时、响应字节数
  和 key 数均有界。完整 JWKS 只有在所有 keys 都通过 public EC P-256 metadata/坐标/唯一 `kid` 校验后
  才原子替换 last-known-good snapshot，不能逐 key 局部更新。
- 同一刷新窗口使用 single-flight；未知 `kid` 有短负缓存，不同随机 `kid` 共享全局 refresh cooldown，
  负缓存条目有硬上限；刷新故障有短 backoff，防止 attacker 放大 issuer 流量或内存。慢刷新失败的
  backoff 从请求**完成时钟**开始，不能让上游延迟吃掉退避窗口。只有**仍在 freshness TTL 内**的
  last-known-good snapshot 可在刷新故障时继续判断已知/未知 key；snapshot 过期后 fail closed 为 503，
  不能把旧 key 无限延寿。所有浮点 cache/timeout 配置必须为 finite positive number，NaN/Infinity
  在启动时拒绝。

FastMCP `JWTVerifier` 可以承担基础 JWT/JWKS 解析，但 4.0.0b2 没有强制本项目全部规则。A3 实现
因此使用独立 `StrictMcpAccessVerifier`：由 joserfc 完成真实 ES256 验签，由项目 validator 补齐
header、必需 claims、类型、max TTL、`token_use`、tenant、scope registry 和 cross-profile 规则，
再投影为 FastMCP `AccessToken`。领域 Principal 不依赖 FastMCP 类型，A3 runtime 也不 import A1
test oracle。

Gateway 部署契约额外固定：`resource_auth_mode` 与历史 `oauth_enabled` 是不同配置维度；`local` 为
`disabled`，`secure` 为 `enforce`。secure 启动时必须提供 canonical issuer、
固定 JWKS URI、resource base URL 和精确 expected audience，且
`expected_audience == resource_base_url + "/mcp"`；缺失、非 HTTPS、尾斜杠漂移或 service scope 超出
A1 registry 都在装配期失败。默认 secure profile 继续只装配 mount service；A5 proxy 必须显式提供
独立 internal issuer/keyset、精确 service resource、私有 composition root 与 request-scoped provider，
不能形成绕过 Gateway 的直连面。production profile/scope constants 必须以测试逐字段匹配 A1 manifest，
不得只改运行时词表。

A4 完成后 `local` 与 `secure` 两种 profile 仍必须在 CLI/启动门禁机器拒绝非 loopback host。
`fastmcp.json` 固定 loopback，CLI 与 JSON 启动面都启用 `host_origin_protection=auto`，防止
Host/Origin/DNS rebinding 绕开本机边界。工具授权完成不会自动解除这条独立 remote-release gate；
至少在 A2/P3 request-scoped delegation、权威企业主体、A6 审计/重放与远程发布证据完成前，secure
仍只能用于本机验证。

### 6.5 A2 issuer、配置与 public JWKS

首期 A2 是与 MultiRAG API 同进程但独立包/配置的逻辑 Authorization Server。唯一公共 route 固定为：

```http
GET /.well-known/jwks.json
```

该 route 不要求 Web session/bearer，不接收任何用户输入，只返回当前原子 public JWKS snapshot，并设置
与配置一致的有界 public cache header；disabled/unready 时稳定返回 503，不把路径、PEM、异常或配置
内容写入响应。A2 不提供公开 token endpoint；P3 后续只从进程内 issuer service 获取 token。

`identity.mcp_issuer` 默认 disabled，启用时必须完整给出 canonical HTTPS `issuer`、first-party
`client_id`、1～300 秒 TTL、固定 30 秒 skew、JWKS cache TTL、resource name 到精确 HTTPS
audience/registered scopes/可选 enterprise subject type+issuer+issuer-tenant authority 的映射，以及 file key provider。配置中的 private PEM path
使用 secret 类型，必须是 absolute、非 symlink、regular file，且 POSIX 下必须归当前进程 owner、只能由
owner 读写；active
private P-256 key 必须与 public keyset 中同 `kid` 的 key 完全匹配。production code 只依赖
`cryptography`，不得 import A1 test oracle 或依赖 MCP SDK 传递的 JWT 包。

进程内签发输入分成两部分：

- immutable `Principal`、已发布 `agent_id`、resource name、requested scopes/claims；
- 由调用它的服务端策略层计算出的 allowed scopes。A2 验证 resource registry 与 requested/allowed
  关系，但不虚构 P3 尚不存在的 Agent/tenant/user policy source。

签发必须先执行 A1 七条 `issuance_policy_cases` 对应的 production policy：raw Provider subject、caller-
selected tenant、unknown scope、scope elevation、forbidden claim 或缺 verified assurance 时根本不生成
token。成功 token 固定 `typ=at+jwt`、`alg=ES256`、active `kid`、`token_use=mcp_access`、精确单值
audience、不可预测 JTI、`nbf=iat` 和 `exp-iat<=300`，compact bytes 不超过 4096。

`sub/tenant_id` 只从 Principal 取得；`auth_time` 仅在 Principal 真实携带时投影且不得晚于 `iat`；
`acr` 首期只在 `ENTERPRISE_VERIFIED` 时投影为冻结值；因当前 Principal 不含已冻结 method evidence，
首期不签 `amr`。`enterprise_subject` 只有 resource 显式允许、调用方请求且 Principal 是 matching
enterprise-verified 时，才投影 `{type, issuer, subject, tenant}`；普通 token 不携带它。

`SigningKeyProvider` 只暴露 active `kid`、完整 public JWKS snapshot 和 ES256 signing operation；未来
KMS adapter 不得迫使 issuer 读取 private bytes。轮换 successor contract 要求 next active key 已存在于
上一版 JWKS，切换后旧 active key 继续存在；旧 key 的 removal deadline 至少是 switch time 加
`max TTL + skew + JWKS cache TTL`。单进程 contract 不替代 O1 的多副本发布/回滚演练。

#### 6.5.1 EIM-P3 request-scoped credential provider

P3 不从 Agent DSL、模型可见工具名、MCP tool description、静态 headers 或数据库中的 MCP server
名称猜授权。首期 authority 是两个启动时一次性加载、深度不可变且可独立评审的 JSON 工件：

- A4 `tool-policies.json`：必须是 format 2，并重新计算 canonical SHA-256 验证
  `policy_revision`；逐 canonical tool 提供 `required_scopes`、`effect`、`replay_mode` 与 assurance；
- MultiRAG `mcp-grants.json`：format 1，带 canonical `grant_revision`、正整数
  `credential_generation`、MCP server → resource/audience 的 delegated binding，以及精确到
  `(tenant_id, platform_user_id, published agent_id, agent_revision_id, resource_name)` 的 grants。

两者只经 `identity.mcp_delegation` 的绝对路径装配，默认 disabled。启动时验证 grant policy revision
与 A4 snapshot 完全相同、grant scopes 属于 snapshot scope registry、server/resource/audience 唯一；
启用后任何缺项、hash 漂移或 authority 歧义都 fail fast/fail closed。现有未登记 server 继续走 legacy
static auth；登记为 delegated 的 server 只允许 Streamable HTTP，URL 必须与 A2 resource audience 和
binding audience 三者逐字相同，且不得再携静态 `Authorization`。SSE delegated 首期拒绝，不静默降级。

`RunContext` 的 `agent_id/agent_revision_id` 只能由服务端已经验证的 execution target 填入；没有完整
Principal 或 published revision 时 delegated call 在网络前拒绝。模型可见 alias 只解决同名工具路由，
授权、token、cache 与审计始终使用 MCP server 上的 original canonical tool name。一个 binding 至少固定：

```text
model alias -> MCP server id -> resource name/audience -> canonical tool -> A4 policy
```

Provider 只缓存 immutable grant/scope decision，不缓存 bearer；请求的 ACR、AMR 与 enterprise subject
每次重新校验。A2 当前不签 `amr`，所以 policy 的 `required_amr` 非空时必须在发网前拒绝。decision key 完整包含 principal、
tenant、agent+revision、server/resource/canonical tool、required scopes、A4 policy revision、grant revision
与 credential generation。每次逻辑 `tools/call` 都调用 A2 取得新 token/JTI；`side_effect/single_use`
由此满足 A6 单 capability 约束。该 bearer 作为一个 logical-operation credential lease 注入 MCP SDK 2
`create_mcp_http_client(auth=httpx2.Auth)`，initialize、call 与 transport retry 可复用同一 lease；下一次
逻辑调用必须新签。Agent/session/global 对象均不得保存某位用户的 token，异常、repr、日志与 tool meta
也不得出现 bearer。

为避免初始化阶段需要宽 scope token，delegated session 不在 Agent 构造时建立用户连接；它在每次逻辑
调用取得最小 tool scopes 后创建 operation-scoped SDK 2 Client，完成 initialize + call 后关闭。legacy
static session 的预热、SSE/HTTP 兼容和 headers 行为保持不变。本步不新增 DB、公开 token endpoint、
OAuth grant、refresh token、外部管理后台操作或 remote release，也不完成 A5/A6 production backend、
M1/M2 业务对象授权、U14 interaction resume。

### 6.6 EIM-A1 corpus wire contract

MultiRAG 与 of_mcp 各自保存字节一致、无需网络的 `eim-a1/v1` corpus：

```text
manifest.json
manifest.schema.json
jwks/access.json
jwks/internal_actor.json
jwks/invalid/*.json
tokens/*.jwt
SHA256SUMS
```

`manifest.json` 必含：

```text
contract_version
validation_time
clock_skew_seconds
max_token_bytes
profiles
resources
scope_registry
cases[]
issuance_policy_cases[]
delegation_cases[]
```

每个 `profiles` entry 固定 `issuer/token_use/max_ttl_seconds`、`required_claims/allowed_claims` 与
`allowed_acr_values/allowed_amr_values`；Resource Server 不能在运行时从 token 自行扩充这些 registry。

每个 `cases[]` case 必含：

```text
id
expected_profile
token_file
jwks_file
request_context:
  expected_resource
  required_scopes
  expected_tenant
  requires_enterprise_subject
  required_enterprise_subject_type
expected:
  authentication = accept | reject
  authorization = allow | deny | not_evaluated
  http_status
  oauth_error
  failure_reason
  normalized_claims
```

成功 case 的 `oauth_error/failure_reason` 为 null。失败 case 的 `oauth_error` 只保存可公开的 OAuth
错误（如 `invalid_token`/`insufficient_scope`），`failure_reason` 保存本项目稳定内部分类；不得把
PyJWT/joserfc 异常类型或原文写入 manifest/public response。

`issuance_policy_cases[]` 使用 `id/request/expected.{issuance,failure_reason}` 固定 signer 输入；
`request` 明确携带 `subject_source`、`tenant_source`、`requested_claims`、`assurance_verified`、
`requires_assurance` 和 requested/allowed/registered scopes。raw Provider subject、调用方选择 tenant、
禁止 claim、未登记/未授权 scope 或未验证 assurance 必须**拒绝签发**；它们不是伪造一个 token 后
期待 Resource Server 返回 401。
`delegation_cases[]` 使用
`id/parent_case_id/actor_case_id/required_preserved_claims/expected.{delegation,failure_reason}` 固定父
`mcp_access` 与 actor token 的关系。actor 不能新增或改写 assurance/enterprise subject；低风险目标
可以省略不需要的条件 claims，要求保留时则必须按 `required_preserved_claims` 逐字段相同。scope
扩大、service audience 串用或 internal token 超过父 token 剩余时间必须在 gateway 换发阶段拒绝。

`validation_time` 是唯一测试时钟；测试不得读取当天时间。合法 token 的 `normalized_claims` 在两个
仓逐字段相同；非法 token 比较本项目稳定错误码，不比较密码库异常文本。corpus 提交固定 token
bytes 和只读 public JWKS；canonical PEP 723 generator 使用 test-only key 和 deterministic RFC 6979
ES256，只负责可复现地产生 corpus。验证仍必须读取提交的固定 token，不能靠运行时重新签发替代。
两仓 CI 不做 sibling import、网络下载或运行时共享 verifier。

### 6.7 首期标准化程度

首期 MultiRAG 是唯一预注册 MCP client，issuer 根据已经认证的内部 Principal 签发 access token；
不对外宣称支持任意第三方 OAuth grant。of_mcp 仍按标准 protected resource 实现 metadata、
challenge、audience 和 bearer validation。

未来外部客户端接入时，优先接真实企业 IdP 的 EMA/ID-JAG；机器后台任务使用 OAuth Client
Credentials extension。不能通过自定义 Header 扩张首期协议，也不能把飞书事件字段伪造成 ID
Token/SAML/Identity Assertion。

### 6.8 MultiRAG 入站 MCP Resource Server

`mcp/server/` 是不同于 `of_mcp` gateway 的另一个 protected resource。正式启用用户级或企业级
访问前必须为它定义独立的 canonical HTTPS resource URI、audience、issuer policy、keyset 和最小
scope 集；发给 `of_mcp` 的 token 即使签名和 Principal 都合法，也必须因 resource/profile 不匹配
被拒绝，反向同理。

入站 token 可以复用本节的基础 JOSE/时间/JWKS 规则，但不能复用目标 resource 或未经重新计算的
scopes。MultiRAG MCP Server 调后端 API 时使用受控 internal actor credential 或显式 token
exchange；不得把外部 bearer 原样当后端 API token 透传。当前 API-key 模式只算现状兼容，不代表
本契约已完成，边界见 [`mcp/README.md`](../../mcp/README.md)。

---

## 7. mount/proxy 内部委托

外部 `mcp_access` 的 audience 是 gateway，不得原样透传到 proxy service。

| runtime | Principal 到 service 的方式 |
|---|---|
| `mount` | root gateway middleware 注入 request-scoped Principal dependency |
| `proxy` | gateway 为精确 service resource 换发 `mcp_internal_actor`；远端 composition root 验证后注入同一 Principal |

`mcp_internal_actor` 继续使用 `typ=at+jwt`/ES256，但使用独立 issuer、P-256 keyset/`kid` 和精确
service HTTPS audience。TTL 最长 60 秒，且 `exp` 不得晚于父 `mcp_access.exp`。必需 claims：

| Claim | 语义 |
|---|---|
| `iss` / `aud` | internal issuer 与单个精确 proxy service resource |
| `sub` | 原已验证 `platform_user_id`，与父 token 相同 |
| `client_id` | gateway 的预注册 workload client |
| `iat` / `nbf` / `exp` / `jti` | 最长 60 秒的新内部委托 |
| `scope` | 父 token scope 与目标 service/tool 所需 scope 的严格子集或相等集合 |
| `tenant_id` / `agent_id` | 从已验证父 Principal/执行上下文复制，不接受请求覆盖 |
| `auth_time` / `acr` / `amr` | 条件；父 token 已携带且目标 service 需要 assurance 时原样复制，不能提升 |
| `enterprise_subject` | 条件；只有父 token 已携带且目标 service 需要时原样复制，不能新增或改写 |
| `act` | RFC 8693-shaped `{sub: <gateway workload id>}`；当前 actor，不改变顶层用户 `sub` |
| `parent_jti_hash` | `base64url(sha256(parent_jti))`，无 padding；只作关联，不泄露父 jti |
| `trace_id` | 跨 gateway/service 的不透明 correlation ID |
| `token_use` | 固定 `mcp_internal_actor` |

internal token 只能在受控私网 TLS/mTLS 链路使用。gateway 必须拒绝
`mcp_internal_actor`；proxy service 必须拒绝 `mcp_access`；service A 必须拒绝 service B 的
audience。proxy 形态重新验证 token 和本地业务策略，不能把 gateway 的“已验证”当成跳过授权的
理由。A1 corpus 用同一份合法 compact bytes 同时证明：它按 `mcp_internal_actor` profile 在目标
proxy 可接受，但按 Gateway 的 `mcp_access` profile/resource 必须拒绝；畸形或 hybrid token 不能
替代这条 cross-profile 断言。

这不是 token passthrough，而是 gateway 在已验证主体基础上的内部下游委托。mount/proxy 等价测试
必须证明 service 获得的 Principal、工具可见性、直接调用授权和审计结果一致。

A5 当前实现额外固定以下运行语义：Gateway 使用 FastMCP 4 的异步、逐请求
`ProxyProvider.client_factory`，在真实 request context 中读取 Principal、`ToolPolicy` 和前端协议版本；
每个逻辑目录/调用操作都创建新的 `ProxyClient(..., auth=<actor bearer>)`，不复用带用户 bearer 的
client/session。modern `2026-07-28` 前端必须使后端使用同一 protocol version，legacy 前端必须使用
`legacy`，不能以 `auto` 静默降级。ProxyProvider 自带 cache 只能缓存远端 raw metadata，用户级
visibility/auth 必须在每个请求后应用；远端 direct call 仍在执行前重验本地 policy。

Gateway 必须关闭透明 incoming-header forwarding，使用官方 `ProxyClient` auth 面携带 actor bearer；
内建 proxy `_meta` 与当前 OTel span 负责协议/trace 传播，token `trace_id` 只作安全关联，调用方 `_meta`
不能成为授权证据。modern sessionless 模式不得依赖跨请求 `Context.set_state` 或 transport session 保存
Principal、token、policy、replay 状态。这些实现事实已经由 of_mcp `5b4162a` 的 modern/legacy 真实 HTTP、
并发 visibility isolation、每操作新 token/client、cross-profile/resource drift 与 audit v2 测试覆盖；
真实私网 TLS/mTLS、key/config、部署和跨仓 rollout 仍不属于完成事实。

---

## 8. of_mcp Principal dependency

A4 已提供 request-scoped `current_principal()`：外层把 A3 verified claims 投影为不可变领域 Principal，
内层 list/call middleware 与后续工具 dependency 读取同一 context-local 值，并在请求结束时恢复上下文。
领域 Principal 只包含稳定平台主体、tenant/agent/client/resource/token metadata、scopes 与可验证的条件
assurance；role/group/department、Provider 原始字段和上游 token 不属于其模型。service/domain 不 import
FastMCP 或 auth provider 类型。

当前 leave/medic 业务工具尚未消费 Principal；下面是 M1/M2 需要落地的工具适配层目标，而不是 A4 已完成
的业务主体注入证明：

```python
async def submit_wtd(
    payload: SubmissionInput,
    principal: Principal = Depends(current_principal),
) -> SubmissionResult: ...
```

身份参数不得出现在 tool schema。`medic` 现有 `workcode` 等字段目前仍由调用方提供；M1/M2 为保持兼容
可暂时保留，但必须完成以下迁移：

- 改为 optional；
- description 标注 deprecated/ignored；
- 无论模型传什么都忽略；
- 使用 `principal.require_enterprise_subject("workcode" | configured type)`；
- 契约 diff 预计 additive/behavioral，按 of_mcp 门禁核实。

A4 的 external resolver interface 不能替代上述迁移，也不能直接信任 raw tool arguments。生产业务对象
授权必须先由各工具的 Pydantic/input schema 完成类型、默认值与 canonicalization，再把最小不可变
授权上下文交给权威 resolver/PDP；resolver 缺失、失败或结果非法必须走 500 invariant failure。
`auth_time` 当前可从 Principal 读取，但没有 freshness/max-age 判定；要求“最近重新认证”的工具必须在
后续策略契约完成前保持不可发布，不能只检查字段存在。

---

## 9. Interaction、Confirmation 与幂等契约

### 9.1 InteractionSession

MCP 多轮输入是持久化业务交互，不依赖 MCP transport session 或进程内 future。规范记录：

```text
interaction_id            opaque random
tenant_id
platform_user_id
agent_id
resource_uri              canonical MCP resource/audience
tool_name
call_digest               canonical tool + original arguments hash
mode                      form/url
requested_schema          approved canonical schema; null for url mode
schema_digest             canonical JSON hash
request_state             encrypted opaque continuation
input_response            encrypted normalized response; null before submit
result                    encrypted validated structured result; null before completion
state                     awaiting_input/resuming/completed/declined/cancelled/expired/failed
revision                  monotonically increasing integer
expires_at
provider                  feishu/web/other
presentation_ref          opaque provider card/page locator
created_at
updated_at
```

不变量：

- `platform_user_id`、tenant、resource 和 tool 来自服务端执行上下文，不从表单、URL 或
  `requestState` 读取；
- `agent_id` 与 `call_digest` 绑定发起调用和原始参数；恢复时不能把本轮输入附加到另一个 Agent、工具
  或参数集合；
- `request_state` 按敏感 continuation 加密存储，不写日志、不进入模型上下文、不放入卡片 value 或
  URL；用户响应必须先规范化并加密落库，再快速 ACK 和异步恢复；结构化结果也按其数据分级加密或
  脱敏保存；URL mode 只携带另一个短期一次性 opaque nonce；
- form mode 只接受已批准的有限 schema；Host 先校验 `inputResponses`，resource server 恢复调用后
  必须再次验证；密码、API key、access token、OAuth code 和支付凭据禁止经 form mode 收集；
- 回调先把飞书 operator 解析为 verified Principal，再以
  `(interaction_id, revision, state=awaiting_input)` compare-and-set 进入 `resuming`；换人、跨 tenant、
  过期 revision、重复响应和过期会话全部拒绝；
- 一次恢复可以再次得到 `InputRequiredResult`；此时生成下一 revision 并回到 `awaiting_input`。成功
  结果必须按工具 `outputSchema` 校验后保存为结构化结果，再由 Provider 渲染；
- decline、cancel 和 expire 是持久化终态。网络超时不能擅自当作 cancel，也不能自动重放可能已有
  副作用的工具调用。

InteractionSession 只证明用户对一次输入请求作出了响应，不代表敏感动作已经获批。需要副作用确认
时，服务端从已验证的规范化输入生成下面的 Confirmation record，并通过 `interaction_id` 建立审计
关联。

### 9.2 Confirmation record

```text
confirmation_id          opaque random
interaction_id           optional originating interaction
tenant_id
platform_user_id
tool_name
action_digest            canonical JSON hash, excludes secrets
summary                   safe card display
state                     pending/confirmed/cancelled/expired/consumed
expires_at
confirmed_at
consumed_at
```

确认回调必须校验点击者解析后的 `platform_user_id`，并用 compare-and-set 从 `pending` 进入
`confirmed`。确认只授权 digest 对应的精确参数，任何参数变化都需要新确认。

### 9.3 Idempotency record

键：

```text
tenant_id + tool_name + confirmation_id + action_digest
```

状态：`started/succeeded/failed_retryable/failed_terminal`。外部系统支持 idempotency key 时透传新的
service-specific key；不支持时先持久化 started 并对外部返回 ID 做原子记录。未知结果不能自动
重复创建，进入人工/查询恢复流程。

---

## 10. 管理/API 契约

下面是目标管理能力，具体路由名称在实现任务中按现有 API 风格定稿。EIM-I6.1 已提供默认 dry-run、
显式 apply 的受控运维 CLI；其余 HTTP/API/UI 尚未接线，不能把直接实例化 repository 或人工 SQL
当成生产管理面：

| 能力 | 调用者 | 关键规则 |
|---|---|---|
| 查看 tenant identity policy | tenant admin | 不回显 Secret/完整企业主体 |
| 更新 provisioning/TTL/subject resolver | tenant owner/admin | 审计，禁止用户自改 |
| 创建一次性 link code | 已登录用户 | 短 TTL、单次、绑定 tenant/provider |
| 列出自己的 external identities | 当前用户 | 只显示脱敏 provider/account/status |
| 解绑 | 当前用户或管理员 | 高风险最后一个登录方式要二次确认 |
| 管理员查看 identity conflicts | tenant admin | 完整 ID 仍默认脱敏，显式 break-glass |
| revalidate | tenant admin/system job | 限流、幂等、不接受明文 Secret |
| issuer JWKS/metadata | of_mcp/verifier | HTTPS、cache headers、无用户数据 |

任何新 Channel 前后端字段还需更新 `docs/channel-program/CONTRACT.md` 并按其语义版本规则处理。
