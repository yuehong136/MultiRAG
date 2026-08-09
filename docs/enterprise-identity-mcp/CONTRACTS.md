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

- `provider` 必须与服务端 binding 的 channel provider 一致。
- `provider_tenant_key` 必须与 provider account 首次已验证 tenant key 一致；首次绑定时通过应用
  凭据和 Contact/event 共同确认后保存。
- `app_id` 不从消息 assertion 读取；它来自解密后的 provider account credential/config。
- `tenant_id/target/revision/session/principal/scopes` 不存在于 worker command body。
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

出站不把飞书卡片字段塞进 `ExecutionEvent`。执行层只发用户可见 delta、白名单 status、脱敏
references/artifacts 和稳定错误码；Provider-neutral ReplySession 管理
`begin/append/status/complete/fail`，飞书 adapter 才拥有 `card_id/message_id/sequence/reaction_id`。

两层幂等分别固定为：

```text
业务执行：binding + event_id/message_id -> Redis atomic claim
飞书发送：binding + event_id + delivery_stage -> deterministic uuid
```

同一个网络结果未知的 delivery stage 不得换 UUID 重发；渲染降级不得重新执行 Agent 或 MCP。
该契约由 EIM-U0/CHN-X9 先以加法提供 `stream()`/ReplySession 并保留 `ask()`，再由
EIM-U1/CHN-U8 消费；第一步不改变现有 SSE wire。

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

- 现有用户初始 `account_kind=local`；有明确外部登录关联的账号可在后续 link 时变成 `hybrid`。
- JIT 创建：`email=null`、`password=null`、`account_kind=external`、`login_channel=feishu`。
- 后续绑定本地/OIDC 登录时显式升级 `hybrid`；禁止按相同邮箱静默合并。
- 密码登录、找回密码、邮件通知入口必须对 `email is null` 给出确定行为，不能 500。

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

## 6. MCP access token

### 6.1 外部 token（MultiRAG -> of_mcp Gateway）

算法：ES256；header 必须有 `kid`。对称 HS256 不用于跨服务生产部署。

Claims：

| Claim | 必需 | 语义 |
|---|:---:|---|
| `iss` | 是 | Authorization issuer canonical HTTPS URL |
| `sub` | 是 | `platform_user_id`，不是 open_id/工号 |
| `aud` | 是 | of_mcp gateway canonical MCP resource URI |
| `iat` / `nbf` / `exp` | 是 | 短期 token；默认 5 分钟 |
| `jti` | 是 | 唯一 token ID，用于审计/高风险防重放 |
| `scope` | 是 | 空格分隔 OAuth scope，例如 `medic:submit` |
| `tenant_id` | 是 | MultiRAG tenant |
| `client_id` | 是 | 预注册 MultiRAG MCP client |
| `agent_id` | 是 | 发起工具选择的已发布 Agent |
| `authn_provider` | 是 | `feishu` / `oidc` / `web` |
| `auth_time` | 是 | 最近满足该操作 assurance 的时间 |
| `enterprise_subject` | 条件 | `{type, value, issuer}`；仅目标 service 需要时签发 |
| `token_use` | 是 | 固定 `mcp_access` |

不允许的 claims：`open_id`、`union_id`、姓名、邮箱、手机号、飞书/OA access token、完整
Channel message/chat ID、模型提示词。

`enterprise_subject.value` 对目标 of_mcp 是必要业务身份，不属于公开日志；审计默认记录 hash。

### 6.2 audience 和 scope

首期 canonical resource 示例：

```text
https://mcp.example.internal/mcp
```

token 只允许该完整 audience。scope 必须来自服务 `service.toml` 与平台策略交集；请求工具不在
scope 中时，of_mcp 返回标准 403/scope challenge。

### 6.3 JWKS 和轮换

- issuer 提供 HTTPS JWKS；private key 只在 issuer secret/KMS。
- 新 key 先加入 JWKS，再开始签发；旧 key 保留至少“最大 token TTL + 时钟偏差 + cache TTL”。
- verifier 按 `kid` 缓存，未知 `kid` 触发一次受限刷新；刷新失败且 cache 过期则 fail closed。
- verifier 允许的算法白名单固定 ES256，拒绝 `none` 和 header 指定的任意算法。

### 6.4 首期标准化程度

首期 MultiRAG 是唯一预注册 MCP client，issuer 根据已经认证的内部 Principal 签发 access token；
不对外宣称支持任意第三方 OAuth grant。of_mcp 仍按标准 protected resource 实现 metadata、
challenge、audience 和 bearer validation。

未来外部客户端接入时，优先接真实企业 IdP 的 EMA/ID-JAG；机器后台任务使用 OAuth Client
Credentials extension。不能通过自定义 Header 扩张首期协议。

---

## 7. mount/proxy 内部委托

外部 user token 的 audience 是 gateway，不得原样透传到 proxy service。

| runtime | Principal 到 service 的方式 |
|---|---|
| `mount` | root gateway middleware 注入 request-scoped Principal dependency |
| `proxy` | gateway 生成短期 internal actor token，audience=`ofmcp-service:<id>`；远端 composition root 验证后注入同一 Principal |

internal actor token：

- TTL 不超过外部 token 剩余 TTL，建议 60 秒；
- scope 只能缩减；
- 包含原 `jti` 的 hash/parent token ID、gateway workload identity 和 trace ID；
- 只能在私网 TLS/mTLS 链路使用；
- service 不接受外部 gateway audience token。

这不是 token passthrough，而是 gateway 在已验证主体基础上的内部下游委托。mount/proxy
等价测试必须证明 service 获得的 Principal 和授权结果一致。

---

## 8. of_mcp Principal dependency

service/domain 不 import auth framework。工具适配层通过 FastMCP dependency 取得只读 Principal：

```python
async def submit_wtd(
    payload: SubmissionInput,
    principal: Principal = Depends(current_principal),
) -> SubmissionResult: ...
```

身份参数不出现在 tool schema。`medic` 现有 `workcode` 为保持兼容可暂时保留：

- 改为 optional；
- description 标注 deprecated/ignored；
- 无论模型传什么都忽略；
- 使用 `principal.require_enterprise_subject("workcode" | configured type)`；
- 契约 diff 预计 additive/behavioral，按 of_mcp 门禁核实。

---

## 9. Confirmation 与幂等契约

### Confirmation record

```text
confirmation_id          opaque random
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

### Idempotency record

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
