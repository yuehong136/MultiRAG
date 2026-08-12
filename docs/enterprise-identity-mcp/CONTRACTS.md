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

---

## 2. Channel 私有输入契约

### 2.1 最终 DTO

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
    provider_tenant_key: str | None = Field(default=None, max_length=255)
    identifiers: tuple[ExternalIdentityIdentifier, ...] = Field(min_length=1, max_length=8)


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
- `app_id` 不从消息 assertion 读取；它来自解密后的 provider account credential/config。
- `event_id`/事件时间属于受限 transport envelope，用于时效与重放校验，不是主体字段；
  `provider_account_key` 也始终取服务端 binding，不接受 payload 覆盖。
- `tenant_id/target/revision/session/principal/scopes` 不存在于 worker command body。
- role、group、department、audience、确认状态同样不得出现在 assertion 中或参与主体提升。
- 即使事件直接带 `user_id`，首次仍要验证其处于应用数据范围且 status 可用。

### 2.3 兼容升级

当前 `ChannelActor` 只有 `provider/subject/conversation`，且私有模型 `extra="forbid"`。必须走：

1. **tolerate**：API 同时接受 legacy `subject` 和新 `identity`，只读旧字段；部署 API/execution。
2. **emit**：worker 开始发送新 `identity`，resolver 优先新字段并保留 legacy 回退；重启 supervisor/
   worker，观察无 `extra_forbidden`。
3. **use**：启用 verified resolver 和 Principal；完成活体联调。
4. **remove**：所有 runner 已升级后删除 legacy `subject`，再次按先 API 后 worker 部署。

每一步是独立 PR/任务，映射的 `CHN-*` 见 [ROADMAP](ROADMAP.md)。

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
- 后续绑定本地/OIDC 登录时显式升级 `hybrid`；禁止按相同邮箱静默合并。
- 密码登录、找回密码、邮件通知入口必须对 `email is null` 给出确定行为，不能 500。
- `account_kind=external` 在数据库和 service 层都禁止本地密码；密码登录/找回只查
  `local/hybrid`，对 external-only 账号按无可用密码凭据 fail closed，不对 null 密码调用 bcrypt。
- 新建密码账号显式写 `local`。I1 不新建 OAuth-only 账号；未来 I6/JIT 只有在 Provider subject
  绑定验证完成后才可新建 `external` 或通过显式 link/merge 升级为 `hybrid`。
- 现有 Web OAuth callback 在 I1 期间完成 Provider 回调校验后固定返回
  `oauth_identity_binding_required`，**不按 email 登录、注册或合并任何平台账号**；OAuth `state`
  必须存在且逐字匹配 session 中的一次性值。I6 建立显式 Provider subject binding/link/merge 后才可
  重新开放账号动作；Channel/JIT 也不得复用 email callback。存量 `login_channel != password`
  账户仅由迁移保守分类为 `external/hybrid`，不因此获得可用的 verified external identity。

### 3.2 `t_ai_external_identities`

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
UNIQUE(provider, provider_tenant_key, subject_type, subject_value)
```

状态变化使用行锁/乐观 revision。`revoked` 不物理删除，不允许普通 JIT 自动复活。

### 3.3 `t_ai_external_identity_aliases`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | string PK | opaque ID |
| `external_identity_id` | non-null, indexed | canonical identity |
| `provider` | non-null | 冗余用于唯一约束/查询隔离 |
| `provider_tenant_key` | non-null | 外部租户 |
| `provider_account_key` | non-null | 飞书 app_id；union alias 可使用开发商 key |
| `alias_type` | non-null | `open_id` / `union_id` |
| `alias_value` | non-null | alias |
| `verified_at` | timestamptz | 最近验证 |

唯一约束：

```text
UNIQUE(provider, provider_tenant_key, provider_account_key, alias_type, alias_value)
```

同一个 canonical identity 可以有多个 App 的 open_id alias。alias 冲突进入 `conflict`，不得
“后写覆盖前写”。

### 3.4 `t_ai_enterprise_subject_links`

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
个”。

### 3.5 `t_ai_identity_event_receipts`

用于 Contact 事件幂等：

```text
UNIQUE(provider, provider_account_key, event_type, event_id)
```

只保存 hash/ID、处理状态、时间、错误码和 identity ID；不保存完整事件体。

### 3.6 Provider account 状态

在现有 `ChatChannel` 控制面增加或关联 identity provider state：

```text
provider_tenant_key
identity_revision
last_scope_change_at
last_directory_event_at
identity_health_state
identity_health_error_code
```

这些字段属于私有/管理员面，公开响应必须脱敏。`provider_tenant_key` 不能从普通 Channel update
请求任意修改；只通过 verified onboarding/rotation 流程更新。

---

## 4. Identity Service 接口

```python
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class IdentityAssurance(StrEnum):
    CACHED = "cached"
    DIRECTORY_VERIFIED = "directory_verified"
    ENTERPRISE_VERIFIED = "enterprise_verified"


@dataclass(frozen=True, slots=True)
class EnterpriseSubject:
    type: str
    value: str
    issuer: str
    issuer_tenant: str
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticationContext:
    provider: str
    external_identity_id: str
    assurance: IdentityAssurance
    authenticated_at: datetime


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    tenant_id: str
    authentication: AuthenticationContext
    enterprise_subject: EnterpriseSubject | None = None
    display_name: str = ""
```

Principal 不包含 tenant role、业务权限集合或飞书 access token。角色和业务权限是变化更快的授权
状态，按对应边界查询。

核心接口：

```python
class EnterpriseIdentityService:
    async def resolve_channel_actor(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        assertion: ExternalIdentityAssertion,
        required_assurance: IdentityAssurance,
    ) -> Principal: ...

    async def consume_directory_event(self, event: DirectoryIdentityEvent) -> None: ...

    async def revalidate_linked_identity(self, external_identity_id: str) -> None: ...
```

Provider SPI：

```python
class EnterpriseIdentityProvider:
    async def resolve(self, context: ProviderContext, assertion: ExternalIdentityAssertion) -> ProviderIdentity: ...

    async def refresh(self, context: ProviderContext, provider_user_id: str) -> ProviderIdentity: ...
```

Enterprise subject SPI：

```python
class EnterpriseSubjectResolver:
    async def resolve(self, provider_identity: ProviderIdentity) -> EnterpriseSubjectResolution: ...
```

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
| `IDENTITY_PROVIDER_UNAVAILABLE` | 503 | 飞书/OA 暂时不可用且无可接受 cache |
| `ENTERPRISE_SUBJECT_REQUIRED` | 403/tool error | 当前工具需要 talent/workcode，但未解析 |
| `IDENTITY_ASSURANCE_INSUFFICIENT` | 403/step-up | cache freshness 不满足操作风险 |

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
`required_scopes`、`accepted_acr_values`、`required_amr`、可选 `enterprise_subject` 和
`external_requirements`。registry 必须在所有 mount/namespace assembly 完成后，针对最终 canonical
tool catalog 一次性构造：

- 每个暴露工具必须且只能有一个 policy；缺失或孤儿 policy fail closed；
- policy scope 必须属于当前 service 的 scope 词表；secure profile 是否属于 A1 resource registry 仍由
  A3 的 profile assembly 门禁单独校验，local 的 `hello:greet` 不因此伪装成 production scope；
- 重复 canonical tool name、service namespace/canonicalization collision 在启动时拒绝；
- policy model、registry 和投影后的 Principal 都是不可变对象，调用期间不能被原位改写；
- 发现与执行只消费同一个 registry，不允许维护两套逐渐漂移的策略表。

`ofmcp contract` 必须生成并校验排序稳定的 `apps/gateway/contract/tool-policies.json`。快照包含
`snapshot_format`、`profile`、service id/namespace/scope 词表，以及每个 canonical tool 的 service id、
required scopes、ACR/AMR、enterprise subject 和 external requirements。`policy_revision` 等于移除
revision 字段后的 canonical JSON document 的 SHA-256；当前值为
`6f79e7ddf8f630993a054f284ebd5213424ffe39b252c661d16a2967ed6fdd67`。策略变更必须显式刷新快照并
经过 contract review；A6 运行时审计和后续 P3 token/cache key 必须使用同一 revision，不从文件
mtime 或未排序映射推导。

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
A1 registry 都在装配期失败。secure 在 A5 前只接受 mount service，proxy 不能形成绕过 Gateway 的
直连面。production profile/scope constants 必须以测试逐字段匹配 A1 manifest，不得只改运行时词表。

A4 完成后 `local` 与 `secure` 两种 profile 仍必须在 CLI/启动门禁机器拒绝非 loopback host。
`fastmcp.json` 固定 loopback，CLI 与 JSON 启动面都启用 `host_origin_protection=auto`，防止
Host/Origin/DNS rebinding 绕开本机边界。工具授权完成不会自动解除这条独立 remote-release gate；
至少在 A2/P3 request-scoped delegation、权威企业主体、A6 审计/重放与远程发布证据完成前，secure
仍只能用于本机验证。

### 6.5 EIM-A1 corpus wire contract

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

### 6.6 首期标准化程度

首期 MultiRAG 是唯一预注册 MCP client，issuer 根据已经认证的内部 Principal 签发 access token；
不对外宣称支持任意第三方 OAuth grant。of_mcp 仍按标准 protected resource 实现 metadata、
challenge、audience 和 bearer validation。

未来外部客户端接入时，优先接真实企业 IdP 的 EMA/ID-JAG；机器后台任务使用 OAuth Client
Credentials extension。不能通过自定义 Header 扩张首期协议，也不能把飞书事件字段伪造成 ID
Token/SAML/Identity Assertion。

### 6.7 MultiRAG 入站 MCP Resource Server

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

需要的管理能力，具体路由名称在实现任务中按现有 API 风格定稿：

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
