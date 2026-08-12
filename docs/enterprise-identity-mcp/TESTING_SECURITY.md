# 企业身份与 MCP · 测试、安全和运维基线

> 状态：规范性文档。本文中的“必须”是上线门禁，不是建议。
> 入口：[README](README.md) · 数据与令牌契约：[CONTRACTS](CONTRACTS.md) · 实施任务：[ROADMAP](ROADMAP.md)

## 1. 验证目标

整套能力只有同时证明以下四件事，才算“企业级身份接入”完成：

1. **身份真实**：消息里的 `open_id` 不能被直接当成 MultiRAG 用户；只有飞书官方目录接口验证过的
   `(tenant_key, user_id)` 才能关联平台账号。
2. **租户隔离**：任何 ID、缓存、幂等键和数据库唯一约束都必须包含企业边界，不能跨企业碰撞。
3. **权限收口**：MultiRAG 决定“谁能用哪个 Agent/知识库”，`of_mcp` 决定“这个工具能否执行这个动作”；
   渠道只传递身份断言，不授予业务权限。
4. **可撤销、可审计**：离职、停用、权限收回、密钥轮换和重放攻击都能被及时阻断并留下不含敏感值的证据。

## 2. 威胁模型

### 2.1 受保护资产

- MultiRAG 平台账号、租户成员关系、Agent/知识库权限；
- 飞书 `app_secret`、OAuth refresh token、MCP 签名私钥和企业 IdP 凭据；
- 员工号、邮箱、手机号、组织关系等个人信息；
- `of_mcp` 中诊疗、患者、处方等业务数据和有副作用的工具；
- 审计日志的完整性、InteractionSession、opaque `requestState`、幂等记录和确认记录。

### 2.2 攻击者与失效来源

| 来源 | 能力 | 必须防住的结果 |
|---|---|---|
| 普通员工 | 可发送任意 prompt、重复点击卡片 | 越权调用、身份注入、重复副作用 |
| 群成员/外部成员 | 可在群里 @机器人，可能不在企业通讯录 | 把群成员误认成内部员工 |
| 恶意渠道/伪造调用方 | 构造 private API JSON、伪造 `open_id` | 绕过飞书验证直接成为 Principal |
| 被攻陷的 Agent/工具 | 可操纵参数、诱导模型泄露 token | token 透传、权限扩大、敏感日志 |
| 运维误配置 | 错 scope、错可见范围、旧密钥、时钟漂移 | 全员误授权、不可撤销或大面积中断 |
| 上游异常 | 飞书/OpenAPI/JWKS/Redis/Postgres 暂不可用 | fail-open、缓存污染、重复执行 |

### 2.3 不变量

以下不变量必须有自动化测试；任何一条失败都禁止上线：

- prompt、工具参数、卡片表单字段不得决定 `principal_id`、`tenant_id`、`employee_no` 或 scopes；
- `open_id` 的唯一键必须包含 tenant-scoped `provider_account_key`，`user_id` 的唯一键必须包含
  tenant-scoped `provider_tenant_key`；
- 首期数据库必须强制 `(provider, provider_tenant_key) -> exactly one tenant_id`；同一
  `(provider, provider_tenant_key, provider_account_key)` 与同一 Channel 也各自只能归属一个 Tenant；
  account↔Channel link 必须同时匹配两端 tenant/provider scope，任何消息或动态路由不能改写 ownership；
- 目录验证失败、用户停用、企业映射冲突时必须 fail closed；
- identity core 只接受服务端构造的 ProviderContext；无效 account/scope/revision 与“有效 account 下
  alias 未链接”必须是两个不同结果。后者只可产生 verification-gated plan，不能直接创建用户、激活
  identity 或生成 Principal；
- identity resolution 每次从数据库重查 live User/UserTenant；重复 active membership、停用/匿名用户、
  非法 role 或 stale revision 都 fail closed，不能任选第一条或沿用旧缓存授权；
- provisioning policy 必须是显式数据库 row，mode、TTL 和 revision 一起进入 snapshot；缺行/非法值/
  读取失败不得使用默认 JIT。I3 plan 携 revision，I6 锁后重新核对 action/mode/revision；
- link code 必须由 exact 24-byte CSPRNG 产生、raw value 只返回一次，持久层只见 domain-separated
  HMAC-SHA256 digest + key id。TTL 只能来自权威 policy（60～900 秒），新签发撤销同 account + target
  旧 pending code，所有可探测 invalid/conflict 结果统一要求重新 link；
- 所有阻塞 policy/account/canonical/target/grant 锁后必须用 PostgreSQL `clock_timestamp()` 重新检查
  Provider proof 的 5 分钟窗口与 pending code expiry；不能用冻结的事务起点时间延长凭据有效期；
- preprovisioned 不开户，link_only target 只来自 authenticated Principal 创建的 grant，JIT 只创建
  external-only User + active `UserTenant(NORMAL)`；全状态 reverse identity unique 与 active membership
  partial unique 必须由数据库承担并发最终保护；
- explicit link 不是双 User merge；姓名、display name、邮箱、手机号和 employee_no 永不用于 target
  匹配。I6 不写 EnterpriseSubject；inactive/conflict/revoked identity 不因新 proof 自动恢复；
- first-binding event append-only，raw subject/alias/code/PII 不入表；server-owned provider tenant/account
  scope natural keys 只为复合 FK 存在且 safe projection 隐藏；
- ProviderContext 的 account revision 与 scope marker 必须同时匹配；read 必须携 alias proof 时间。scope
  变化后的旧 alias 要求 Provider 重验，旧 proof 不能写入或倒退已存 proof；identity conflict 的安全
  结论优先于 freshness，不能被降格成普通重验；
- Principal 只能由进程内 trusted adapter 经 evidence builder 构造；direct constructor 、wire DTO、
  未验证 subject、非 `RESOLVED`、非 `owner/admin/normal` membership 或带
  error/provision/reverification 的 I3 结果都不能提升；
- C1 tolerate 期间 structured assertion 必须保持可选；legacy `provider/subject/conversation` 仍必填，
  旧 actor 序列化不得新增 `identity:null`。resolver 必须对“无 assertion”和“有攻击者哨兵 assertion”
  产生相同 authority context，且 worker/client 生产的旧 request body 不得变化；
- `DIRECTORY_VERIFIED` 必须绑定 I3 identity 的原始 `verified_at`，`ENTERPRISE_VERIFIED`
  必须绑定 enterprise subject 的原始 `verified_at`；cache/请求命中不得刷新 proof 时间；
- legacy Web/API Principal 每请求活查 active User 与恰好一条 `tenant_id == user.id` 的
  active OWNER membership；它不是多 Tenant selector，valid JWT 的用户/成员失效也不得降级
  重解释为 API token；
- `platform_user_id` 始终是 MultiRAG `User.id`，不是飞书 `open_id`；
- `enterprise_subject_id` 与 `platform_user_id` 分离，员工号不能成为公开认证凭据；
- MCP token 必须校验 `typ/alg/kid`、`iss`、单一精确 `aud/resource`、`iat/nbf/exp/max_ttl`、`jti`、
  `token_use`、必需 claim 类型、scope registry 和签名，compact token 默认不超过 4096 bytes；
- MultiRAG 不把飞书 access token、用户 OAuth token或收到的外部 bearer token透传给 `of_mcp`；
- Channel `ExternalIdentityAssertion`、`mcp_access` 与 `mcp_internal_actor` 是三类不可互换的信任工件；
  gateway、proxy service 和 MultiRAG inbound Resource Server 必须按自己的 profile/resource 互相拒绝；
- MultiRAG 出站 MCP Client 与入站 MCP Resource Server 使用不同 resource/audience；任一方向的
  bearer、session 或 continuation 不得在另一方向复用；
- `requestState`、form value、H5 nonce 和卡片 action 不能决定 Principal、tenant、scope 或确认状态；
- InteractionSession 的响应必须绑定 verified operator、tenant、resource、tool、revision 和 expiry，
  并以 compare-and-set 只消费一次；
- `of_mcp` 不采信参数中的 `workcode`/`talent_id` 作为调用者身份；
- 高风险工具必须同时满足授权、用户确认、短时有效和幂等；
- 日志不得记录 secret、完整 bearer token、OAuth code、手机号或患者敏感正文。

## 3. 测试分层

### 3.1 MultiRAG 单元与 HTTP 契约测试

遵循仓库根 [AGENTS.md](../../AGENTS.md) 的三形态规范，新增测试放在 `tests/unit/`。

| 范围 | 必测内容 | 推荐测试形态 |
|---|---|---|
| Channel DTO | tolerate/emit/consume/remove 各半步、`extra="forbid"`、旧/新进程组合；C1 必证 legacy dump/request 不变且 resolver 不消费 | Pydantic 纯测试 + private HTTP 契约测试 |
| Feishu assertion | 保留 `open_id/user_id/union_id/tenant_key`，不再 first-nonempty；`app_id`/provider account 只取服务端 ownership，assertion 夹带必须拒绝 | 纯函数测试 |
| F1 `lark-oapi` contract | 1.7.2 lock、平台 import 隔离、SDK 已知 idle loop、Contact V3 typed request/response | 隔离子进程 + 固定无 PII fixture；不访问真实飞书 |
| I3 IdentityService | context/alias 结构校验、account health、无效 context 与 alias miss 区分、live membership、三态 plan、policy failure | 框架无关 async unit；只 mock lookup/policy ports |
| I3 repository | 单 SQL authority snapshot、account generation/alias freshness、ordinary/verified/account-control/ownership 分权、CAS/锁、审计时间、输入/driver 脱敏 | `tests/integration/` 真 PostgreSQL |
| I4 Provider flow | Auth V3 generated async request/resource/transport + strict live top-level adapter、Tenant V2/Contact V3 typed nested response、阶段化 error、cache/single-flight/限流、credential link/换钥 | ✅ I4.1：live adapter sandbox 三步通过；I4+F1 118、真 PG 1、完整 verify/integration 全绿 |
| I6 Identity write flow | 权威 policy/revision/TTL，JIT/link/preprovisioned 的 User/UserTenant/identity/alias/code/event 原子事务，post-lock freshness | ✅ framework-neutral async service + 真 PostgreSQL；完整门禁全绿 |
| DB schema/event | 唯一约束、事务并发、别名归一化、幂等事件 | `tests/integration/` 真 PostgreSQL |
| P1 Principal | 单一 canonical class、sealed constructor、深不可变/脱敏 repr、evidence 一致、proof time、legacy owner 活查与 JWT fallback 分界 | 纯 domain/auth unit + 真 PostgreSQL owner-membership 行为 |
| MCP token | 两个 profile、claims/types、固定时钟、TTL、JWKS、轮换、scope registry/交集、cross-resource | 两库独立纯密码学 corpus + HTTP 契约测试 |
| MCP client | resource/audience、失败映射、无静态用户 header | mock transport/官方 SDK 测试 |
| MCP resource server | modern/legacy 协议、独立 audience、scope、无 bearer 透传 | ASGI/官方 Client 契约测试 |
| InteractionSession | MRTR 多轮、revision/CAS、decline/cancel/expire、重启恢复 | service 单测 + 真库集成 |
| Structured result | `structuredContent`/`outputSchema` 一致性和安全事件转换 | schema/golden tests |

截至 2026-08-13，EIM-I3 已完成 ProviderContext 驱动的本地 identity lookup、
verification-gated policy plan 与窄 repository/CAS seam；EIM-P1 已完成 canonical Principal、
AuthenticationContext、I3 promotion builder 和 legacy Web/API personal-owner adapter；EIM-I6 已完成
权威 policy/link/event schema、framework-neutral service 与三种原子 provisioning transaction，并通过
完整门禁。I6 不包含 Channel 组装，也没有把 Principal 传进
Agent/Memory/Workflow/MCP。EIM-F3/F8 只完成
MCP SDK 2/FastMCP 4 的协议运行时迁移和 `InputRequiredResult` 的 transport-level 暴露。
EIM-A1 已固定 token/JWKS test vectors；A7 的前置虽已满足，独立 audience/scope 和 OAuth
Resource Server 仍未实现；InteractionSession 仍属于依赖 P3/A4/C3 的 EIM-U14。不能因
P1 或 modern/legacy 协议测试通过就把这些安全测试标为已满足。

EIM-C1 / CHN-X5 当前源码处于 tolerate 收口中：private API 可解析可选、有界、extra-forbid 的
`ExternalIdentityAssertion`，同时保持 legacy actor 必填；runtime client/worker 未 emit，resolver 未
consume，C2/C3/C4 均未完成。C1 必须拒绝 command/actor/assertion/identifier 任一层的 Tenant、Principal、
Provider Account/app_id、role/scope/audience/confirmation/token 夹带，拒绝重复 kind 和 legacy/
structured provider 不一致；未知但有界 kind 只允许保留，不产生信任。等价完整 `make verify` 和 C1
三文件定向均已在隔离 clean tree 通过。用户批准后的新 API 切换确认加载 `536a1ea5`，但在 serving
前被存量 schema bootstrap 顺序缺陷阻断，并未产生旧 worker -> 新 API 请求；EIM-I2.2 / CHN-O15
修复与安全恢复完成前，C1 继续为 `🔵`。

EIM-F1 已以 1.7.2 contract fixture 收口，并准确区分两个 import 边界：平台 control/provider/
verification/identity 模块导入不得加载 `lark_oapi` 或创建 event loop；显式导入官方 SDK 则允许其已知
模块级 loop，但只在 loop idle、无 task、无新增 thread 且 Client build 不启动工作的前提下通过。
Contact fixture 只证明官方 typed request/response seam；不证明真实 app scope、token 获取、目录可用性
或身份映射。SDK `TokenManager` 的 cache miss 没有 single-flight，项目级并发刷新/隔离负向已由 I4
主体实现，I4.1 的 live Auth response 修正与复验已完成。

I4 的可执行安全边界是：Auth V3 使用官方 generated async request/resource/transport，再由项目
adapter 严格解析 live 顶层 token/expire；Tenant V2/Contact V3 保持 generated typed nested response，
分别精确验证 `provider_tenant_key` 和查询单人。Tenant/Contact 显式传递 project token，不走 SDK
同步 TokenManager cold path。Provider DTO 只投影白名单字段，并在 SDK 宽松 primitive unmarshal
之后再严格校验 required string 和五个 status bool。官方 SDK 仍 lazy import。

1.7.2 generated Auth response 只声明 `data`，live 成功响应却把 token/expire 放在顶层。I4.1 已增加
真实顶层 shape regression，并用修正后的 adapter sandbox 证明 Auth -> Tenant -> Contact；测试不再
以手造 `response.data` fixture 代替 live wire 契约。

token/directory cache 按 account id/revision/scope marker/Secret version/domain 分区，容量、in-flight、TTL
和限流队列都有硬上限；同 key cold miss single-flight，cancelled waiter 不取消 producer，失败
不缓存，跨 account/generation/domain 不共享。当前参数为 identity 正/负 cache 300/30 秒、
token safety 600 秒、Contact 15 calls/s/account 与 2 秒最长排队。临时 credential adapter
以精确 account/link/channel/secret 单 SQL + 注入 SecretStore 获取凭据；不 cache、commit、猜测
`app_id` 或接受 Channel JSON 明文 Secret。

Auth/Tenant/Contact 三个 adapter response 都必须保留 HTTP/business envelope；非 2xx + code 0
仍是失败。业务码按 endpoint stage 分类，不能跨阶段共用：`10003` 不是 credential code；Auth
credential mismatch 当前为 `10015/20002`。Auth/Tenant 控制面失败不能产生目录 identity
`NOT_FOUND/NOT_IN_SCOPE`，只有 Contact 用户查询面可以；且 HTTP 403/404 fallback 只在 code 0
时成立，未知/瞬时非零 Contact code 绝不能降级成 JIT 可消费的 identity miss。

先前 sandbox 把 Python 源码与 credential 同时放进 stdin，导致脚本实际读到空参数，相关证据
全部作废。新的有效直连 sandbox 使用非空
credential，Auth V3、Tenant V2、Contact V3 均为 HTTP 200/code 0，tenant 精确匹配、用户 active；
修正后的 production adapter sandbox 还确认 token present/expiry valid、tenant/user present、
asserted open_id match、stable user id present，以及 activated true、frozen/resigned/exited/unjoin
全 false。只记录这些状态，不保存 credential/token/真实 tenant、account、user ID 或个人字段。

最少必须覆盖这些命名场景：

1. 同一个飞书用户通过同一企业的两个应用进入：`open_id` 不同、`user_id` 相同，最终只能有一个
   canonical identity 和一个已绑定平台账号；两个应用安装实例都必须归属同一个 MultiRAG Tenant。
   enterprise subject 要等 I5 才另行证明，不能把 I6 canonical identity 当工号映射。
2. 两个企业碰巧出现相同 `user_id`：因为 `tenant_key` 不同，绝不能合并。
3. 同一个 `open_id` 字符串出现在不同应用：因为 `provider_account_key` 不同，绝不能合并。
4. `open_id`、`user_id` 同时出现但目录返回的用户不一致：返回 `IDENTITY_CONFLICT`，不猜测。
5. 飞书返回用户 `status.is_activated=false`、已离职或不可见：拒绝建立/使用会话。
6. 已存在 alias 的用户更换部门或姓名：身份主体不变化，只更新 profile 快照。
7. 两个并发首次消息同时 JIT：数据库最终只能生成一条 User、active membership、identity 与首次
   binding event；另一事务安全重读。I6 不生成 EnterpriseSubjectLink。
8. 目录接口超时且无可用缓存：拒绝首次登录；已有短期正缓存可按策略工作并打 `stale` 指标。
9. 负缓存命中后收到 `contact.user.updated_v3`：负缓存立即失效，可重新验证。
10. C1 的 `ChannelActor.subject` 或 `identity` 被任意伪造：resolver authority 与不带 assertion 时相同；
    在 C3 之后也只能经 Provider/IdentityService 验证，永不能直接进入 `Principal.id`。
11. 同一 `(provider, tenant_key)` 以不同 app/account 写入另一个 MultiRAG Tenant：provider tenant
    ownership 唯一约束必须拒绝；不能因 app_id 不同而绕过首期企业单 Tenant。
12. 同一 provider account 或 `channel_id` 换一个 Tenant/Provider 建 link：link 的两组复合外键必须
    拒绝；未来集团级多 Tenant 即使经新 ADR/schema 迁移开放，也只能用不同安装实例和 binding。
13. Provider Account 无 Channel 独立创建：允许持久化但不能调用 Provider API；删除 Channel 不级联
    删除 account；已有 link 时删除任一端因 `ON DELETE RESTRICT` 失败。
14. 同一个 Channel 关联两个 account，或同一个 account 关联两个 Channel：首期双唯一必须拒绝；
    service 不能用 `app_id` 猜测、任选第一条或自动换绑。
15. I2 旧 account/channel 数据升级：每条 account 恰好 backfill 一条同 Tenant/Provider link 后才删除
    旧列；account/link 任一有数据时 downgrade 必须 fail closed，不能丢弃独立企业连接。
16. 精确 Provider Account 不存在，或 tenant/provider/key/revision 任一不符：lookup 返回 invalid
    context，policy resolver 零调用；不能伪装为普通 alias miss 后进入 JIT plan。
17. Provider Account 有效但 alias 不存在：单 SQL snapshot 必须保留 account 且 `identity=None`；三种
    policy 只返回 `provider_verification_required=true` 的 plan，数据库和用户表零写入。
18. alias 命中但 `User` 停用/匿名/无效，或同 Tenant `UserTenant` 缺失/无效/角色非法：每次解析都
    fail closed；不能依赖过去的 identity active 状态继续执行。
19. 同一 user/tenant 出现多条 active membership：repository 返回稳定 link conflict，而不是任选
    第一条；错误和 DTO repr 不包含 account/alias/subject/user 原值。
20. identity/alias 重复 insert：完全相同请求幂等重读；不同 user/identity 占用同一自然键稳定冲突，
    并发两个 session 只有一个 winner，不发生后写覆盖。
21. identity insert 固定落 `pending_link`；ordinary CAS 只能单向收紧，不能转 `active`。只有 verified
    identity mutation 能写/刷新 alias，并显式从 `pending_link/inactive` activation；
    `conflict/revoked` 是终态且彼此不能转换。
22. stale identity/account revision 不能更新；两个并发 writer 只有一个 `applied`，另一个明确
    `revision_conflict`。持有 account `FOR UPDATE` 时另一 writer 确实被数据库锁阻断。
23. lookup、ordinary mutation、verified identity mutation、provider-account control 与 ownership
    ports 权限精确且互不越权；聚合 identity repository 不继承 ownership，所有 port 均无 generic
    save/update/delete/commit/rollback、Channel bind/rebind/unlink。
24. account scope marker 晚于 alias proof：普通 active identity 返回 inactive + provider verification
    required，同一 identity fresh proof 后恢复 resolved。早于 marker 的 proof 写入 revision conflict，
    早于当前 alias 但仍合法的 proof 不得倒退 `verified_at`；conflict/revoked 两个终态都先于 freshness
    分类，分别保持 conflict/inactive 且不提示可重验，verified port 也不能复活。
25. account health CAS 省略 scope/event 时间时保留原值；显式时间只能单调前进，rewind 返回 invalid
    transition。并发 CAS 只有一个 winner，ownership 字段保持不变。
26. 所有 Core insert/update 都更新预期 BaseModel audit 时间；事务失败时此前 identity insert 与后续
    alias conflict 一起回滚，不能留下半条映射。
27. 超长/空白/未知 enum、重复或未知 attributes 在发 SQL 前拒绝；故意携敏感 bind parameter 的
    SQLAlchemy/driver exception 只暴露 `IDENTITY_REPOSITORY_UNAVAILABLE`，错误文本与 repr 无原值。
28. Principal direct constructor、actor/membership 跨 user，enterprise subject 跨 user/tenant，以及
    wrong provider/internal identity 全部拒绝；错误只携稳定 code，不回显输入标识。
29. I3 `RESOLVED` 如果同时带 error、provision action、`provider_verification_required` 或伪造
    membership role，不得构造 Principal；directory proof 缺失、早于 identity proof 或晚于本次
    validation 都拒绝。
30. Web/API 当前不得伪造 `authenticated_at`、directory/enterprise assurance；Principal 中不存在
    email/role/groups/scopes/access token，PII 和 Provider 标识默认不进 repr。
31. valid JWT 下 User 停用或 personal OWNER membership 缺失/重复时 API-token query 零调用；
    只有预期 JWT invalid 才 fallback，意外 verifier/runtime error 原样传播。
32. 平台模块在 `asyncio.set_event_loop(None)` 后逐个导入：`lark_oapi*` 模块集合保持空且当前 loop 仍
    不存在；显式 SDK import/Client build 只产生一个 idle loop，无 task、新 thread 或后台工作。
33. Contact V3 固定成功 fixture 必须经 `GetUserResponse/User/UserStatus` typed model 解码，request 精确为
    `GET /open-apis/contact/v3/users/:user_id?user_id_type=open_id`；把 `status` 换成字符串等错误 shape
    必须由 SDK unmarshal 拒绝。
34. 不得用 F1 fixture 宣称 token 并发刷新已安全：1.7.2 `TokenManager` 的 cache miss 可并发发起请求；
    I4 必须另测同一 Provider Account single-flight、跨 account 不合并、失败后可恢复且不缓存错误结果。
35. I4 官方 adapter 必须按 Auth V3 -> Tenant V2 -> Contact V3 调用，Tenant/Contact 显式
    放入同一 project token，SDK 同步 `TokenManager.get_self_tenant_token` 零调用；空可选
    employee/display 字段归一 `None`，空/非字符串 required ID、非 bool 或缺失任一 status 拒绝；
    三 endpoint 的非 2xx + code 0 envelope 必须完整保留，不能被 SDK business success 覆盖；
    Auth 成功 fixture 必须覆盖 live 顶层 `tenant_access_token/expire`，不能只构造 `response.data`。
36. Tenant V2 返回与 `ProviderContext` 不一致时返回 `IDENTITY_TENANT_MISMATCH`，token 不进
    cache、Contact 零调用；Auth/Tenant 403/404 也不能映射为 identity not-found/not-in-scope。
    Contact 的 scope/not-found/inactive/invalid/transient 各自映射稳定脱敏结果；错误码按 stage
    分类，Auth `10015/20002` 为 credential mismatch，`10003` 不得作为全局 credential code；
    Contact 只有 code 0 可用 HTTP 403/404 fallback，未知/瞬时非零 code 不得降级 JIT。
37. identity 正向 cache 和 not-found/not-in-scope/inactive 负向 cache 按注入时钟分别在 300/30 秒
    过期；unavailable/invalid/conflict 不缓存。account revision、scope marker、Secret version 或 domain 变化
    都必须产生新 key，不复用旧 token/identity。
38. 精确 Channel-backed credential 在真 PostgreSQL 必须同时命中 account/link/channel/secret；
    缺 link、多结果、跨 Tenant/Provider、stale revision/scope marker、disabled account、明文污染、
    缺 Secret 或 decrypt 失败都只返稳定 credential unavailable；Secret version 旋转必须进入
    `credential_generation`。
39. Provider/credential DTO、exception、repr、调试输出和真实 sandbox 证据中不得出现
    app secret、tenant token、raw response/body、完整 account/tenant/user/open/union ID、姓名、手机、邮箱、
    头像或工号。
40. policy 缺行/非法 TTL、create 并发和 stale CAS：缺行必须让 I3/I6 fail closed；create 只有
    `CREATED + EXISTS`，CAS 只有正确 revision 能把 mode+TTL 一起更新并 revision+1；普通 provisioning
    port 无 admin create/update/delete。
41. I3 alias miss 结果必须携 policy revision，P1 builder 必须拒绝带 action/revision 的 plan。I6
    action/mode/revision 任一不符、策略在 I3 后改变，均在零 identity/User 写入下返回 policy unavailable。
42. entropy source 返回 bytes subclass、非 bytes、23/25 bytes 均拒绝；合法 raw code 解码必须正好
    24 bytes。签发结果 `repr` 不含 raw code/key/digest，数据库列集合不存在 raw code/token/secret。
43. link code 签发 TTL 只取 locked policy，60 秒与900 秒边界通过，超界拒绝；同 account + target
    新码原子撤销旧 pending 码且最多一个 pending。旧码、猜码、过期、撤销、policy/account revision 或
    scope marker 变化、target/canonical conflict 的 outward result 都是 `IDENTITY_LINK_REQUIRED`。
44. proof exact 5 分钟边界通过，早一瞬间拒绝，任何 future proof 拒绝。事务排队等待 policy/account/
    canonical/target/grant 锁跨过 proof age 或 code expiry 后，post-lock `clock_timestamp()` 重验必须拒绝
    且零半写；binding event/consume 时间使用 post-lock wall clock。
45. preprovisioned 对不存在 canonical pending identity 返回 not found 且 User count 不变；对已有 pending
    identity 只在 live User + active membership 下激活。link_only 只能从 grant 得到 target，caller
    command 无 target_user 字段；JIT 只创建 external User + `NORMAL` membership，无 email/password/
    access token、OWNER/ADMIN 或个人 Tenant。
46. canonical subject natural key、全状态 reverse slot 和 active membership partial unique 在并发下
    只有一个 winner；inactive/revoked 行不能通过状态变化释放 reverse slot。任一 alias/event/code
    冲突使 User/UserTenant/identity/alias/code/event 整体回滚。
47. explicit link 到 local User 只允许 local→hybrid，不能生成或合并第二个 User；同名、同邮箱、同
    手机、同 display_name/employee_no 都不得查询或选 target。合法 101～512 字符 display name 仅
    NFKC/空白规范化并截到 100，不得成为 proof 拒绝或匹配依据。
48. active canonical identity + fresh proof 可刷新缺失/stale alias并 `ALREADY_BOUND`；真正
    inactive/conflict/revoked 保持 fail closed。active canonical 若显式提交 code，必须核对 target 与
    原首次 binding event，不能静默忽略冲突 code。
49. 每个 identity 只有一条 append-only first-binding event；同 link grant 或 keyed request digest
    不能产生第二条。event 无 update/delete port，safe projection 不含 provider tenant/account scope
    natural key、subject/alias、target/actor、digest；表中 scope natural keys 仅服务复合 FK。
50. I6 import graph 不包含 FastMCP、Channel、HTTP route 或 UI；写事务不触发 Provider 网络调用；
    employee_no 即使出现在 I4 proof 也不进入 I6 command/schema/identity attributes/BindingEvent。

### 3.2 MultiRAG 集成测试

涉及 identity 表、唯一约束、事件收件箱和 token replay 表时，必须使用
`bootstrapped_async_engine`，并至少验证：

- schema upgrade 与 downgrade/rollback 路径；
- 部署旧代码读取新表时不受影响；
- provider tenant ownership 拒绝同一外部企业跨 Tenant；provider account 独立存在合法；显式 link 的
  account/channel 复合外键拒绝 tenant/provider 任一错配，双唯一拒绝一端多绑；
- alias/receipt 的 provider-account 复合外键、canonical identity 复合外键都拒绝 tenant/provider/
  provider-tenant 任一维度错配；
- canonical、alias、enterprise subject 与 receipt 在各自 tenant-scoped 唯一边界内拒绝重复，在合法的
  不同 Tenant/不同 account 边界不误合并；
- `SELECT ... FOR UPDATE` 或唯一约束重试能收敛首次绑定竞态；
- I3 identity resolution 必须以精确 Provider Account 为 SQL 起点，在一条 statement 中携回可选
  alias/identity 与 live User/UserTenant；无 account row 与 account 存在但 alias miss 使用不同结果；
- I3 mutation 写前锁 account 并复核 revision + scope marker；ordinary pending insert/单向收紧 CAS、
  verified alias refresh/activation、provider health CAS、proof freshness/不倒退、终态、Core 审计时间、
  输入/driver 脱敏和事务回滚都由真库测试证明；
- I6 policy table 精确强制每 Tenant 一行、mode/revision、TTL 60～900 且无默认 backfill；create concurrency
  收敛为 CREATED/EXISTS，CAS 对 stale revision 不覆盖。upgrade 遇 active membership/reverse identity
  重复 group fail closed；
- I6 link-code table 不存在 raw code/token/secret 列，digest + key id、policy/account generation、状态/
  时间组合、15 分钟硬上限、同 account + target 仅一条 pending、scope 复合 FK 都由真库约束；
- I6 first-binding event 对 identity、link grant 与 keyed request digest 唯一，method/account-kind shape、
  `provider_verified_at<=occurred_at` 和 account/identity/grant scope 复合 FK 由真库强制；有任一 policy/
  code/event 时 downgrade fail closed；
- 三种 I6 transaction 在真实并发下只产生一个 canonical identity/reverse slot/active membership/首次
  event；任一后段失败整笔回滚。所有阻塞锁后以 `clock_timestamp()` 重验 5 分钟 proof/code expiry，
  人为排队跨界必须零写入；
- event receipt 的 `(tenant_id, provider, provider_tenant_key, provider_account_key, event_type,
  event_id)` 幂等，且表结构不存在原始 body/payload/headers/metadata 列；
- 所有 Tenant/User/Channel/identity/provider-account/link 外键均为 `ON DELETE RESTRICT`；有任意 ownership/
  identity/alias/subject/receipt 历史时 downgrade fail closed，空表才能按依赖逆序回滚再升级；
- model-first upgrade 对现存表逐项比较 server default 与规范化 CHECK SQL，不只比较名称；任一默认值、
  检查表达式或 shape 漂移都 fail closed；错误 server default 与六表部分存在的半迁移 schema 分别有
  真库负向，不能自动补齐或接受；
- downgrade 在“空表计数 + DROP”的完整临界区先取得六表 `ACCESS EXCLUSIVE` 锁；并发 writer 不能在
  计数后插入历史。真 PostgreSQL 测试固定覆盖锁竞争，并断言 SQLSTATE `55P03`；
- `attributes` ORM 与 PostgreSQL 双层只接受 `display_name/provider_status` 的 string/null object；安全
  projection 不回显 Provider 原始 ID、事件 ID/hash、业务 subject 或 health error；
- `jti`/confirmation/idempotency key 在并发提交下只消费一次；
- `(interaction_id, revision)` 在两个并发 callback 下只有一个进入 `resuming`，重启后仍可恢复或明确过期；
- opaque `requestState`、规范化用户响应和敏感结构化结果按数据分级加密保存；跨 tenant、跨 Principal、
  跨 agent/tool/call digest、跨 resource 搬运全部拒绝；
- 删除/停用不是只清缓存，数据库状态也能阻断下一次请求。

改 DB 后除了 `make verify`，还必须按 AGENTS.md 运行 `make integration`。服务不可用导致 skip
不能作为验收；CI 或受控环境必须用 `REQUIRE_SERVICES=1` 跑出真实结果。

EIM-I2 完成基线（2026-08-12）：Alembic 单 head `8f2c4d6e7a9b`；模型安全单元测试
**10 passed**；identity 真 PostgreSQL integration **11 passed**，覆盖精确 schema/default、错误
server default、六表部分存在的半迁移 schema、model-first shape、空表 downgrade → upgrade、有历史
拒绝、六表锁竞争、唯一/复合外键和双 AsyncSession 并发 alias 单 winner；完整 `make verify`
**1925 passed** 且 lint/import/async/mypy 全绿；`REQUIRE_SERVICES=1 make integration` **52 passed**。

EIM-I2.1 完成基线（2026-08-12）：Alembic 单 head `9a3b5c7d8e0f`；I2.1 identity integration
**23 passed**，覆盖旧数据 backfill、fresh/model-first 精确 shape、unlinked account、link 双唯一、
跨 Tenant/Provider 复合外键、两端 RESTRICT、空表 downgrade round trip，以及 account/link 任一有
数据 downgrade fail closed；相关 I2.1 unit（identity models + table order）**14 passed**；
identity schema + Channel control persistence **25 passed**，相关 identity + Channel control unit
**67 passed**；完整 `make verify` unit **1929 passed**，`REQUIRE_SERVICES=1 make integration`
**65 passed**，`git diff --check` 通过。

EIM-I3 完成基线（2026-08-12）：`tests/unit/test_identity_domain.py`
**40 passed**，覆盖三态 plan、无效 context/alias miss 分层、healthy/active/live membership、不可变脱敏
DTO、最小权限 ports 和无 FastMCP/Channel import；`tests/integration/test_identity_repository.py` 真
PostgreSQL **17 passed**，覆盖无 Channel account、verified ownership 幂等/冲突、单 SQL resolution、
live/重复 membership、account scope marker + alias proof 重验、identity/alias 幂等与并发、状态/account
CAS、health 时间 preserve/monotonic、Core 审计时间、行锁、事务回滚、输入与 driver 错误脱敏。
两者合计 I3 定向 **57 passed**；repository + identity schema 连续真库 **40 passed**，包含每例按
child→parent 显式清理，证明测试数据不污染共享 scratch schema。完整 `make verify` 全绿：Ruff
format/check、7 import contracts、async gate、mypy 71 files、unit **1969 passed in 28.85s**；
`REQUIRE_SERVICES=1 make integration` **82 passed in 12.48s**。

EIM-P1 完成基线（2026-08-12）：`tests/unit/test_principal.py` 与
`tests/unit/test_async_auth_deps.py` 定向 **49 passed**，覆盖单一 canonical class、sealed constructor、证据
builder、proof-time 绑定、深不可变/脱敏、legacy owner 和凭据 fallback 分界；
`tests/integration/test_principal_auth.py` 真 PostgreSQL **1 passed**，覆盖 active User + 唯一
personal OWNER Tenant 的活查与歧义拒绝。完整 `make verify` 全绿：7 条 import contracts、
async gate、mypy **73 files**、unit **2005 passed**；`REQUIRE_SERVICES=1 make integration`
**83 passed**；安全复核无 blocker，`git diff --check` 通过。

EIM-I6 完成基线（2026-08-13）：domain/model unit **143 passed in 5.49s**；identity schema、provisioning
schema、I3 repository 与 I6 repository 真 PostgreSQL **84 passed in 7.80s**，覆盖上述三 mode、policy
CAS、digest-only code、append-only event、并发/回滚与 post-lock freshness。`make verify` 全绿：Ruff
format **1241 files**、Ruff check、**7** 条 import contracts（832 files/2611 dependencies）、async DB
gate、mypy **81 source files**、unit **2188 passed**；`REQUIRE_SERVICES=1 make integration`
**128 passed in 16.40s**；Alembic 单 head `b4c6d8e0f2a4`。首次完整 integration 暴露旧 P1 测试的重复 active membership seed；该用例已
改为断言 I6 新增的数据库唯一约束并聚焦 **1 passed**，最终完整 integration 全绿。current-tree 安全
终审无 blocker。

### 3.3 `of_mcp` 测试

`of_mcp` 自己必须建立以下测试层，不得只依赖 MultiRAG 测试：

- **A3 auth middleware**：无 token、错 `typ/issuer/audience/resource/token_use`、过期、未来 `iat/nbf`、
  超过 max TTL、未知 `kid`、`alg=none`/算法混淆、超长 token 全部拒绝；
- **A3 JWKS**：正常刷新、cache、key rotation 双钥窗口、并发 single-flight、未知 key 受限刷新、
  per-key 负缓存、跨未知 key 全局 cooldown、负缓存硬上限、完成时钟退避、原子 last-known-good 替换、
  上游失败且无新鲜 snapshot 时 fail closed；
- **A3/A4 scope policy**：A3 证明已登记额外 scope 不使 token 无效、未知/畸形 scope fail closed，并以
  test-only endpoint 门槛钉住 FastMCP 403 seam；A4 证明缺当前工具所需 scope 在认证成功后返回 403
  `insufficient_scope`，一次给全所需 scopes；
- **A4 Principal bridge**：A1 Gateway authentication-accepted vectors 经 A3 production verifier 后全部能
  投影为 immutable Principal；mutable claims 后改不影响 Principal，role/group/department/Provider 原始
  字段被 allowlist 拒绝，跨请求/并发 context 不串主体且请求结束后清空；
- **A4 tool registry**：真实 assembly 后的 canonical tool catalog 与 `service.toml.tool_policies` 一一对应；
  缺失/孤儿 policy、未在当前 service scope 词表登记的 scope、重复 canonical name、namespace
  collision 拒绝；`effect/replay_mode` 必填且 `side_effect=>single_use`，当前真实工具分类逐项锁定；
  snapshot 排序稳定、
  `policy_revision` 可复算且 contract drift 被门禁发现；
- **A4 business subject 边界**：已验证 `{type, issuer, subject, tenant}` 与 policy 所需类型/issuer/tenant
  精确匹配；当前 service policy 未启用 external business requirement，使调用参数 `workcode` 真正被
  忽略仍是 M1/M2 的未来验收，不能记入 A4 完成证据；
- **A3/A4 policy error**：A3 的 verifier 故障在认证 middleware 内返回 503，且第三方异常不泄露；A4
  的 tenant/membership/role/business denial 返回 403 且不伪装 scope challenge，external resolver
  缺失/失败/非法结果返回 500 invariant failure 而不是用户 403；没有可用的新鲜 JWKS cache 时返回
  503 verifier unavailable，不把基础设施故障误报为 401；
- **A6 replay state machine**：同一 `(token_use,issuer,audience,jti)` + 同 fingerprint 只有一个
  execution permit；32 路并发也只允许一个。相同 key/不同 Principal、tool、policy revision 或 canonical
  arguments 为 conflict；claim 容量耗尽、store 故障、过期和非法状态迁移全部 fail closed，
  `OUTCOME_UNKNOWN`/`DISPATCHED` 不释放 capability；
- **A6 Gateway execution boundary**：必须证明 coordinator 在 A4 inner final allow 后、业务函数前运行，
  并使用同一 evaluation policy 与 runtime revision；duplicate/conflict/dependency failure 分别是
  HTTP 409/403/503、`no-store`、无 OAuth challenge、业务零调用。成功记 `SUCCEEDED`；tool error、异常、
  cancellation 记 `OUTCOME_UNKNOWN`；post-dispatch outcome 持久化失败不覆盖业务响应；
- **A6 audit/privacy**：冻结 schema 无 `metadata`/arguments/result 字段；序列化 event/permit/log/OTel 中
  不存在 bearer、原始 JTI、Provider ID、低熵主体、enterprise subject、患者/请假正文或异常原文。
  不同 issuer 的相同 JTI 得到不同 audit digest；HMAC domain separation、key 长度和 deterministic
  fingerprint 有正反测试；audit/replay 前置依赖失败阻断副作用；
- **A6 OTel/cardinality**：只验证 API adapter 对 current span/counters 的受控属性；metric dimensions
  不含 tool、policy revision、user/tenant/client/JTI/fingerprint，参数/结果永不记录；API/SDK/exporter
  抛错不改变安全决策。不能用 unit fake meter 宣称 exporter/collector 或跨仓 trace 已完成；
- **A6 production gate**：memory replay/audit 的 `multi_instance_safe/durable` 固定为 false；secure 未显式
  注入 coordinator、或 coordinator 非 production-ready 时启动失败。test-only 内存开关不得成为真实
  CLI 默认，local/secure remote gate 均保持关闭；
- sensitive tool：确认挑战、过期、不同用户/不同参数重放、双击和并发只执行一次；
- MRTR：合法 `inputResponses + requestState` 可恢复，篡改/过期/错误 schema/revision 明确失败；再次
  返回 `InputRequiredResult` 时不丢失 actor/resource 绑定；
- structured result：输出不符合 `outputSchema` 时返回稳定 tool error，不把未经验证的 JSON 交给 UI；
- audit：成功、拒绝、确认、业务异常都生成同一 correlation chain，且无 token/患者正文。

A3 完成态的最小回归集合包括：

- production strict verifier 逐条执行 A1 中 **68** 个 Gateway authentication cases，合法 case 比较
  normalized claims，非法 case 比较稳定 `failure_reason`；scope/tenant/assurance 的 authorization
  deny case 在这里仍只断言 authentication accept，不提前冒充 A4；
- 真实 HTTP 覆盖 RFC 9728 metadata、缺 token 与 malformed token 的 401/challenge、typed JWKS outage
  的 503/no challenge/no-store/retry-after，以及 test-only global-scope 403 seam；
- metadata、最小 health 和未知 route 携带恶意 Authorization 时不触发 verifier/JWKS；重复
  Authorization、错误 scheme、credential 内空白全部 fail closed；
- JWKS 覆盖 fixed HTTPS/no redirect/bounded response、严格 public P-256 document、fresh cache、
  unknown-`kid` refresh/negative cache/global cooldown/bounded entries、rotation、single-flight、慢失败按
  完成时钟 backoff、过期 snapshot 的 503，以及 NaN/Infinity cache/timeout 配置拒绝；
- Gateway composition 覆盖 local 匿名 loopback、secure 配置 fail-fast、resource/audience 精确一致、
  scope/profile constants 与 A1 manifest 锁定、hello 关闭、secure mount-only/proxy 拒绝、最小 health
  输出；CLI 与 `fastmcp.json` 启用 `host_origin_protection=auto`，A3 handoff 时 local/secure 都拒绝非
  loopback。

完成证据：of_mcp `e4ab560` 定向 **126 passed**；`uv run --locked ofmcp verify` 六步全绿、
**342 passed、2 existing skipped**，contract 无漂移。该段是 A3 历史证据，不用 A4 后来的测试结果
倒填。

A4 完成态的最小回归集合包括：

- 所有 A1 Gateway authentication-accepted vectors 经过 production verifier 与
  `principal_from_verified_claims()`，normalized subject/tenant/scopes 等关键字段一致；
- Principal、enterprise subject、policy model 和 registry 深度不可变；forbidden role/provider claims
  fail closed；同一进程中的并发请求获得各自 Principal，结束后 context reset；
- enabled service 的实际工具清单逐项匹配 policy，缺失/孤儿/重复/namespace collision 负向全部执行；
  `tool-policies.json` 无漂移且 revision 为
  `6f79e7ddf8f630993a054f284ebd5213424ffe39b252c661d16a2967ed6fdd67`；
- `tools/list` 与 direct `tools/call` 使用同一 registry；外层 preflight 通过后，内层仍在执行前重验，
  resolver/策略在两次检查之间变化时不得执行工具；default SSE 模式不得提前固定 HTTP 200，二次
  deny 和 resolver/invariant failure 必须分别保持真实 403/500；
- 真实 HTTP 分层断言 scope 缺失为 403 `insufficient_scope` 且 challenge 给出完整 scopes，tenant/ACR/
  AMR/enterprise subject 为 403 `authorization_denied` 或 `assurance_required` 且无 scope challenge，
  external resolver invariant failure 为 500；
- malformed/超界 MCP request、方法/工具路由不一致、未知或未注册 tool 必须在业务函数前 fail closed；
  body size、现代 MCP header 与 JSON 最大嵌套深度 64 需单独测试，900 层参数必须返回 400 且不执行，
  不能只靠 FastMCP parser 的后置错误；
- local 保持匿名 loopback，secure 保持 bearer-authenticated loopback，Host/Origin protection 与 A3
  401/503 负向不回归。

A4 提交锚点为 of_mcp `74117a0`；定向 **201 passed**，`uv run --locked ofmcp verify` 六步全绿、
**417 passed、2 existing skipped**，contract snapshot 无漂移。这些测试仍不证明
A2/P3 token 获取与逐请求委托、飞书 Channel identity→Principal、M1/M2 业务主体/对象授权、A5 internal
actor、A6 audit/replay 或 `auth_time` freshness；secure 仍被机器限制为仅本机验证，不得做远程业务发布
验收。

A6 phase 1 的最小回归集合包括：

- 所有实际工具的 effect/replay policy 与 format-2 snapshot 完整一致；canonical builder 在不同输入
  顺序下给出同一 revision，local/secure profile 给出各自 revision，devkit/runtime 不出现第二套 hash；
- replay store 的 acquire/duplicate/conflict、并发单赢家、状态机、容量/过期和 fail-closed 故障；
- coordinator 的 request fingerprint、single-capability JTI key、审计顺序、permit 防伪、outcome 保守
  语义，以及所有错误对象/日志/record 的敏感串扫描；
- 真实 FastMCP/ASGI wire 覆盖 inner final-allow 到 execution 的边界和 409/403/503；没有 coordinator
  或仅内存 coordinator 的 secure production path 必须 fail-fast；
- OTel fake span/meter 覆盖允许属性与低基数 counters，exploding adapter 不影响 execution decision；
- `uv run --locked ofmcp contract diff`、完整 `uv run --locked ofmcp verify` 与 `git diff --check`。

当前实现只满足上述 phase-1 自动化形状；A6/Gateway 定向 **65 passed**，完整
`uv run --locked ofmcp verify` 六步全绿、**453 passed、2 existing skipped**，提交锚点统一以 ROADMAP
变更日志为准。A6 必须保持 `🔵`：自动化尚未覆盖真实多副本 durable store、
进程重启后 claim/audit、KMS/HMAC key rotation、真实 OTel SDK/exporter/collector/W3C 跨仓 trace、A5
`parent_jti_hash`、P3 每执行新 token/JTI、业务 idempotency/result lookup 和 remote-release 演练。

### 3.4 跨仓端到端测试

至少准备两个飞书测试账号、两个 MultiRAG 用户、两个权限不同的 Agent，以及一个 `of_mcp`
低风险读工具和一个沙箱写工具。上线前跑完：

| 编号 | 场景 | 期望 |
|---|---|---|
| E2E-01 | 员工首次私聊 | 目录验证、按策略创建/绑定、回答成功；审计链完整 |
| E2E-02 | 未授权员工私聊 | 明确拒绝或给管理员批准入口；不创建普通用户权限 |
| E2E-03 | 离职/停用后再次发送 | 事件失效后立即拒绝；漏事件时对账任务最终收敛 |
| E2E-04 | 群聊外部成员 @机器人 | 不认作内部成员；按 private-only 策略拒绝 |
| E2E-05 | 有 Agent 权限、无 MCP scope | Agent 可对话，但工具在 `of_mcp` 边界拒绝 |
| E2E-06 | 高风险工具 | 先出确认卡；确认后一次成功，重复确认不重复执行 |
| E2E-07 | token 过期/换钥 | 过期拒绝；双钥窗口内新旧合法 token 均按计划工作 |
| E2E-08 | 飞书目录临时不可用 | 首次身份 fail closed；已验证会话按缓存策略降级且报警 |
| E2E-09 | MCP 服务不可用 | 返回可诊断错误，不重试有副作用调用，不伪装为成功 |
| E2E-10 | 回滚一个版本 | 满足 ROADMAP 部署矩阵，旧新组合不出现 `extra_forbidden` |
| E2E-11 | 普通私聊流式回答 | 500ms 内 ack、单卡渐进更新、最终 flush/finish；无推理或工具参数泄漏 |
| E2E-12 | CardKit/reaction 权限缺失或限流 | 自动降级 post/text，最终答案只执行/交付一次，错误指标可诊断 |
| E2E-13 | 重复事件、发送响应丢失、WS 重连 | Redis claim 只执行一次；确定性 delivery uuid 不重复发送同阶段消息 |
| E2E-14 | 同会话快速连续追问 | 每条来源消息 queued -> running -> final；容量溢出明确 busy，不静默 drop |
| E2E-15 | 普通群/话题群 follow-up | mention/allowlist 生效；thread session 不串群、不串人、不串 tenant |
| E2E-16 | 图片/文件/保密或超限资源 | 合法附件受控下载；不匹配、保密、超限和不支持类型明确拒绝，无临时 URL 泄漏 |
| E2E-17 | 只读 MCP 返回一次或多次 `InputRequiredResult` | 飞书表单逐 revision 恢复；最终结果按 `outputSchema` 渲染；不重跑已完成轮次 |
| E2E-18 | 同一 form callback 双击/重放/换人点击 | 只有 verified operator 的当前 revision 被消费一次，其余明确拒绝 |
| E2E-19 | H5 URL mode | URL 只含短期一次性 nonce；免登同人校验；`requestState`/token 不出现在 URL、卡片或日志 |
| E2E-20 | MultiRAG 双 MCP 角色 audience 混用 | 发给 of_mcp 的 token 不能调用 MultiRAG MCP Server，反向同样拒绝 |
| E2E-21 | Host 或 Resource Server 版本回滚 | 现代/legacy 兼容矩阵内可回滚，不要求两个 MCP 方向同时升级或同时回滚 |

### 3.5 MCP Foundation 当前兼容基线

EIM-F2/F3/F4/F6/F7/F8 完成后的可复现基线是 MCP SDK 2/FastMCP 4 主运行时，加上 PEP 723 锁定的
真实 FastMCP 3 legacy 子进程 fixture；`of_mcp` F4 commit 为 `23dd1fd`。执行：

```bash
make mcp-compat
```

当前结果必须为 **13/13 PASS**。矩阵至少证明双方向真实协商分支、tools/list/call、401/403、tool
error、caller cancel/timeout、现代 inbound discover/sessionless/structured result、legacy 回退和
fixture 有界退出。PEP 723 fixture 必须使用自己的 lock/解释器，不得因根依赖已经升级而改成 mock，
也不得用 sibling checkout 或父进程 `sys.path` 假装可复现。

HTTP timeout/caller cancellation 的安全断言只到本地边界：等待必须有界，被取消的本地调用不能继续
占用旧式串行队列，同 server 的快调用不能被慢调用形成 HOL。远端取消是协作式；服务端在调用方
timeout 后仍完成是允许且必须观测的结果，不得据此推断副作用没有发生。高风险工具另以 Confirmation、
业务幂等、结果未知对账和恢复前重授权验收。

这份 13/13 是协议兼容证据，不是 EIM-A1 的 token/JWKS test vectors、EIM-A7 的 inbound OAuth
Resource Server/Principal/scope，也不是 EIM-U14 的持久化 InteractionSession 证据。上述任务完成时
必须在本矩阵之外增加各自的安全正反路径。

### 3.6 EIM-A1 固定 corpus 与两仓互操作门禁

canonical corpus 位置固定为：

```text
MultiRAG: tests/fixtures/eim_a1/v1/
of_mcp:  packages/ofmcp-contracts/tests/fixtures/eim_a1/v1/
```

两个目录必须字节一致，至少包含 `manifest.json`、`manifest.schema.json`、
`jwks/access.json`、`jwks/internal_actor.json`、`jwks/invalid/*.json`、`tokens/*.jwt` 和覆盖这些
输出的 `SHA256SUMS`。两仓都从本地目录读取，不能通过 sibling checkout、
editable install、网络 URL、父进程 `sys.path` 或共享 validator 偷渡另一仓实现。完成日志记录两个
commit SHA 和实际 corpus aggregate digest；aggregate digest 定义为对 `SHA256SUMS` 文件原始 bytes
再做一次 SHA-256。实现尚未提交时写 `pending`，不能使用占位或猜测 hash。

canonical `scripts/generate_eim_a1_vectors.py` 是 PEP 723 脚本，依赖精确锁定，并用 test-only P-256
key + deterministic RFC 6979 ES256 可复现生成 corpus。生成器不得读取环境 Secret；JWKS 只有 public
parameters；测试验证的是提交的固定 token，不得在测试运行时重新签发来掩盖 corpus 漂移。

`manifest.json` 把三种不同责任拆开：

| 集合 | 验证边界 | 典型断言 |
|---|---|---|
| `cases` | Resource Server 认证 + 操作授权 | JOSE/claims/profile 为 401；缺 scope、wrong tenant、assurance 为 403 |
| `issuance_policy_cases` | MultiRAG signer policy | raw Provider subject、越权 tenant、未登记 scope、缺 assurance 时不签发 |
| `delegation_cases` | of_mcp gateway 换发 policy | service audience 精确、scope 只减不增、TTL 不超过 60 秒/父 token；按 `required_preserved_claims` 条件保持 assurance |

Resource Server 测试不能把 issuance/delegation denial 伪造成 `invalid_token`；反之，signer policy
测试也不能靠构造一个事后必被拒绝的 token 代替“根本不签发”。

每个 `cases[]` 用相对 `token_file`/`jwks_file` 引用 corpus 文件；公开错误写 `oauth_error`，稳定内部
分类写 `failure_reason`。成功两者都为 null；失败不能把第三方异常类型或原文当稳定契约。

互操作实现必须独立：

- MultiRAG 声明显式 direct dev dependency `PyJWT[crypto]==2.13.0`；
- of_mcp 声明显式 direct test dependency `joserfc==1.7.4`；
- 两边都在密码库完成 ES256 签名校验后执行项目 profile oracle；不能假设库的默认 required claims、
  clock、audience 或 scope 行为就是 EIM 契约；
- `validation_time` 是唯一时钟。PyJWT 等库内置的 wall-clock `exp/nbf/iat` 校验在 corpus 测试中关闭，
  再由项目 oracle 用 manifest 时间、30 秒 skew 和 profile max TTL 确定性判断；签名、alg、key 和
  issuer/audience 校验仍必须真实执行；
- A1 冻结 assurance claim 的结构和 profile registry：`auth_time<=iat`，`acr` 必须在
  `allowed_acr_values`，`amr` 为非空无重复且每项在 `allowed_amr_values`。A4 已支持逐工具的 ACR
  允许值、完整 required AMR 与 enterprise subject 匹配；`auth_time` freshness/max-age 仍未实现，不能
  只因 claim 存在就宣称完成近期 step-up；
- 合法 case 的 normalized claims 必须逐字段相同；非法 case 比较项目稳定错误码，不锁第三方异常
  类型/文案。所有 case 必须执行，无 skip/xfail。

A1 只交付 test/docs/schema/corpus。生产 issuer、FastMCP auth adapter、JWKS HTTP route、KMS、DB、
Channel Principal 和动态 Authorization 分别属于 A2/A3/C3/P3，不能为让 A1 测试通过提前混入。

2026-08-12 完成基线为 **91 个文件：79 token cases、7 issuance policy cases、5 delegation cases**；
`sha256(SHA256SUMS raw bytes)` 是
`59f82684aa06365f45623ce9bfad336d487f2c9351879266a6b2ab21bf8fe208`。两仓目录字节一致，MultiRAG
定向 **96 passed**；完整 `make verify` 的 Ruff format/check、6 条 import contracts、async DB gate、
mypy 65 files 全绿，unit **1904 passed in 25.76s**。of_mcp `3e1d5ac` 定向 **100 passed**、完整
门禁 **216 passed、2 existing skipped**。计数/摘要不替代逐 case 执行；任一生成物变化都必须重新
生成摘要、复制整目录并同时更新两仓证据。

## 4. 安全专项测试

### 4.1 身份和租户攻击

- 修改 private API 的 `tenant_key`、`provider_account_key`、所有 ID 排列组合；
- 把另一个租户合法 assertion 搬到当前 binding；binding 的服务端配置必须否决它；
- 用 `union_id`/`open_id` 伪装成企业 `user_id`；类型化 identifier 必须阻止混用；
- 员工号重复、复用、空值、前导零、大小写、离职后重新入职；任何歧义都进入人工处理；
- 解绑后立刻重新绑定另一个账号；必须有审计、冷却或管理员确认策略。

### 4.2 令牌和 confused-deputy 攻击

- JOSE/header：缺失或错误 `typ`，缺失/未知 `kid`，`alg=none`、HS/RS/ES 混淆，错误
  `kty/crv/use/alg`，重复 `kid`、JWKS 私钥参数，`jku/x5u/jwk` 和未知 critical header；
- compact token：header/payload/signature 任一篡改、malformed Base64/JSON、重复 claim、超过 4096
  bytes；必须在有界资源内稳定拒绝；
- 时间：expired、not-yet-valid、超过 30 秒 skew 的未来 `iat/nbf`、`exp<=iat`、超过 profile max TTL；
- claims：必需 claim 缺失、空值、类型错误、多 audience、wrong `token_use`；
- scope：空项/重复/控制字符/未知 scope 拒绝；额外合法已登记 scope 认证成功；缺工具所需 scope
  返回 403 `insufficient_scope`，不能错误变成 401；
- issuance policy：prompt/Channel payload 夹带 `principal_id/tenant_id/role/scope`，要求把 raw
  `open_id`/Provider subject 签成 `sub`，或提供不满足 assurance 的 enterprise subject；MultiRAG
  signer 必须拒绝签发，而不是生成一个危险 token 再依赖 Resource Server 拦截；
- delegation policy：gateway 请求比父 token 更多的 scope、更长的剩余 TTL、另一个 service audience
  或替换 `sub/tenant/agent`；必须在换发前拒绝，不能只在 proxy service 事后兜底；
- A 用户 token 调 B 用户确认记录；A Agent token 调 B Agent 资源；
- 为 MultiRAG API 签发的 token 拿去调用 `of_mcp`；audience/resource 不同必须拒绝；
- 为 `of_mcp` 签发的 token 拿去调用 MultiRAG RAG MCP Resource Server，或反向搬运其 token；两个
  入站 resource 都必须按自己的 canonical audience 拒绝；
- Channel workload credential/`ExternalIdentityAssertion` 放入 of_mcp Authorization；不是
  `mcp_access`，必须拒绝；
- 同一合法 `mcp_internal_actor` compact bytes 在目标 proxy 通过、调 Gateway 时按 access profile
  fail closed；另测 `mcp_access` 调 proxy service、service A token 调 service B；
  即使签名、用户和 scope 合法，也必须因 profile/resource 不匹配拒绝；
- 把飞书 tenant access token 放进 MCP Authorization；issuer/格式不符必须拒绝；
- 下游返回 token 请求继续委托；禁止 token exchange 之外的任意 bearer 转发。

错误层也必须钉板：A3 负责无效 profile/claims/签名的 401 与 JWKS 故障且无新鲜 cache 的 503；A4
负责有效 token 的 tenant/membership/role/business 403 和工具所需 scope/assurance 403。A3 的
test-only endpoint scope seam 可以证明框架保持标准 `insufficient_scope`，但不能替代 A4 对真实工具
策略的测试。external resolver 缺失、失败或返回非法结果是 500 `authorization_invariant_failure`，
不是 `authorization_denied`；外层 HTTP preflight 与内层 FastMCP call-before-execute 两层都必须
fail closed。所有分层都必须断言第三方库异常和内部 resolver reason 不会直接成为公开错误文本。

corpus 还要扫描 normalized claims/JWKS/token payload，确认不存在 Provider 原始 ID、姓名、邮箱、
手机号、员工号明文、role/group/department、飞书/OA token、Channel/表单正文、确认状态或真实 Secret。

### 4.3 Channel 与卡片攻击

- 伪造或重放 `card.action.trigger`，确认 nonce 必须绑定用户、租户、工具、参数摘要和过期时间；
- 长连接重复投递、进程重连、两个连接同时收到同一事件；dedupe 必须覆盖；
- prompt 声称“我的工号是…”、“切换到管理员”；身份上下文不可由模型文本覆盖；
- 卡片字段夹带 `principal_id`、scope、任意 URL；服务端按 allowlist 读取字段；
- 篡改 `interaction_id`、revision、H5 nonce 或 form component name，把 A 用户响应搬到 B 用户、
  tenant 或 resource；Interaction Store 必须在恢复工具前拒绝；
- 在 form value 中提交超深 JSON、超长文本、schema 外字段、伪造 enum、无效日期和凭据字段；Host
  必须限制大小、按批准 schema 校验，并拒绝密码/token/API key 的 form-mode 收集；
- `requestState` 被放入卡片 value、H5 URL、模型 prompt 或日志；测试必须扫描并阻断这些泄漏面；
- 群聊/话题串会话键碰撞；键中必须包含 provider、tenant/account、conversation/thread。
- Markdown 伪造 `@all`、`javascript:`/隐藏跳转链接、未闭合代码块或超大卡片；renderer 必须转义、
  限制和安全降级；模型文本不能产生真实 mention。
- 重放同一出站 stage、故意制造 API 成功但响应丢失；确定性 UUID 不变，不能换键盲重试。
- CardKit patch 乱序、sequence 复用、完成后继续 patch、double finish；ReplySession 状态机必须拒绝。
- 卡片/Reaction 失败触发 fallback 时，不能重跑 Agent、放宽身份或绕过 Confirmation。
- 附件 file_key 与 message_id 不匹配、路径穿越、压缩炸弹、伪造 MIME、SSRF URL；下载层 fail closed。

### 4.4 高风险工具攻击

- 参数排序、空白、等价 JSON 试图绕过确认摘要；先 canonicalize 再 hash；
- 第一次执行超时后再次确认；业务幂等键必须能区分“未执行”和“结果未知”；
- 用户确认后管理员撤权、患者状态改变；执行前重新做授权和业务前置检查；
- Agent 把读工具伪装成写工具或反之；风险级别由服务端注册表定义，不由模型声明。
- 把 `InputRequiredResult`、form submit 或 H5 完成伪装成最终确认；没有独立 Confirmation record、
  当前授权和幂等键时，Resource Server 必须拒绝执行副作用。

## 5. 可观测与审计契约

每条用户消息生成 `trace_id`；渠道事件同时保留 provider `event_id`，MCP 调用生成 `mcp_call_id`，
多轮输入另生成 `interaction_id`。跨仓传播最终遵循 W3C Trace Context；当前 A6 phase 1 只读取
of_mcp current span，尚未实现 MultiRAG → of_mcp 的 `traceparent/tracestate` 注入/提取和 collector
联调，不能把随机/本地 trace id 当成跨仓 trace 已完成。

平台级受控审计的目标字段：

```text
occurred_at, trace_id, event_id, mcp_call_id, interaction_id
provider, provider_account_key_hash, tenant_id
platform_user_id, enterprise_subject_id
agent_id, tool_name, decision, reason_code
token_issuer, token_audience, token_jti_hash, scopes
interaction_revision, confirmation_id, idempotency_key_hash, latency_ms, result
```

必须 hash 或省略：`open_id`、`user_id`、`employee_no`、邮箱、手机号。绝不记录：secret、token
原文、OAuth code、医疗正文、完整工具参数。只有受控审计库可保存业务必要的可逆映射，应用日志不可保存。

of_mcp A6 的当前 security audit 更严格：它使用 frozen v1 schema，不接受扩展 `metadata`，只记录
`trace_id/mcp_call_id`、runtime/token profile、issuer/audience、issuer-domain-separated JTI digest、
主体/tenant/agent/client keyed HMAC、tool/effect/replay mode/policy revision、request fingerprint、
decision/reason/replay state。它不记录 Provider/event/interaction 原文、enterprise subject、参数、结果
或异常文本。`parent_jti_hash` 要等 A5 internal actor 才能进入该链；不得填空值冒充已实现。

最低指标：

- identity resolve 的成功/拒绝/冲突/目录错误和 p50/p95/p99；
- JIT 创建、人工待审批、停用命中、缓存命中与 stale 使用量；
- contact event lag、对账扫描滞后、漏事件修复数；
- MCP auth 拒绝原因、工具授权拒绝、确认过期、幂等命中；
- A6 security preparation 的 prepared/duplicate/conflict/store unavailable/audit unavailable；
- A6 execution outcome 的 succeeded/failed-no-effect/outcome-unknown/recording-failed；指标维度只使用
  effect/replay mode/result 等闭集，不使用 user/tenant/tool/JTI/policy revision 等高基数值；
- InteractionSession 创建/恢复/拒绝/过期、form/H5 mode、revision 冲突和 output schema 失败；
- 每 binding 收/丢/重复消息数和端到端时延；
- `first_ack_ms/first_card_ms/first_delta_ms` 的 p50/p95/p99；
- reply queue wait/depth/overflow、CardKit create/patch/final flush、节流和按类型 fallback；
- reaction add/remove、duplicate delivery、thread hydration 和附件 download/scan/parse。

飞书体验首期目标：p95 first ack 不超过 500ms、p95 first card 不超过 1s、正常卡片更新不超过
4 QPS、CardKit 故障仍交付最终文本。同一事件必须只有一次执行和每个 delivery stage 一次发送。
详细计时边界见 [FEISHU_BOT_UX §13](FEISHU_BOT_UX.md#13-指标和-slo)。

告警不得只报“500”。至少按 `IDENTITY_*`、`MCP_TOKEN_*`、`MCP_SCOPE_DENIED`、
`MCP_AUTHORIZATION_DENIED`、`MCP_ASSURANCE_REQUIRED`、`MCP_VERIFIER_UNAVAILABLE`、
`MCP_INTERACTION_*`、`CONFIRMATION_*`、`DIRECTORY_UNAVAILABLE` 分组。

## 6. 上线和回滚演练

### 6.1 上线前证据包

每个 ROADMAP ID 完成时必须把以下内容写入 ROADMAP 变更日志：

- 实际提交 SHA 和受影响仓库；
- 精确测试名、通过数量和门禁命令；
- tolerate/emit/remove 的生产者、消费者版本和部署顺序；
- 数据迁移前后计数、冲突记录数和人工处理结果；
- 如果是线上实测：时间、binding、脱敏日志查询与回滚点。

### 6.2 必做演练

1. 飞书 `app_secret` 轮换：先放新值、验证连接、撤旧值；日志确认无泄漏。
2. MCP ES256 换钥：先发布新 JWKS，再用新 key 签发，等待最大 TTL，最后移除旧 key。
3. 联系人 scope/可见范围被缩小：已有用户不得被误标为离职；进入 `directory_unreachable` 人工态。
4. Contact 事件停收：对账任务在目标窗口发现并修复停用用户。
5. 回滚 API 与 supervisor：严格遵循 [ROADMAP](ROADMAP.md) 的兼容半步，不跨越 remove 闸门。
6. Redis、PostgreSQL、飞书 OpenAPI、JWKS、`of_mcp` 分别故障；确认各边界都 fail closed 或按文档降级。
7. MCP 出站 Client 与入站 Resource Server 分别回滚一个版本；一侧回滚不得要求另一侧同时回滚，
   legacy compatibility 退出前必须有调用方清单和零流量证据。
8. Interaction worker 在 `awaiting_input`、CAS 后 `resuming` 和收到 structured result 三个位置重启；
   分别证明可恢复、不会双消费，结果未知时不自动重放副作用。

### 6.3 事故处置优先级

- **疑似越权**：先停相应 MCP tool/provider binding，保全审计，再调查；不要先清日志或幂等表。
- **密钥泄露**：吊销/轮换密钥，缩短或等待 token TTL，检查 `jti` 与调用审计；仅改代码不算处置。
- **错误停用全员**：暂停对账写入，保留事件收件箱，回滚状态批次；不要删除 identity 记录。
- **重复副作用**：先禁用写工具，依据 idempotency key 对账业务事实，再决定补偿。

## 7. 验证命令

MultiRAG 每个实现任务完成前：

```bash
make fix
make verify
```

改动 MCP SDK、FastMCP、transport、`common/mcp_tool_call_conn.py` 或 `mcp/server/` 时追加：

```bash
make mcp-compat
```

必须得到完整 **13/13 PASS**；单仓 `make verify` 或只跑 modern happy path 不能替代该矩阵。

涉及数据库/身份存储：

```bash
REQUIRE_SERVICES=1 make integration
```

EIM-F1 快速契约命令固定为：

```bash
uv lock --check
uv run pytest tests/unit/test_lark_oapi_contract.py
```

它覆盖四个不加载 SDK 的平台 import、SDK idle-loop 边界和 Contact V3 typed fixture；不需要真实
飞书应用、网络或 Secret。完成结果为新 contract **7 passed**、广义 Feishu/Channel 定向
**101 passed**；`uv lock --check` 通过；`make verify` 全绿：Ruff 1222 files、7 import contracts、
async DB gate、mypy 73 source files、unit **2012 passed in 30.95s**。I4/I4.1 已补 token/cache
single-flight、scope/status/error、live Auth top-level response 与阶段化 error regression；F1 的纯
fixture 不能替代这些证据。

EIM-I4.1 完成时的快速回路（不替代最终 `make verify`/integration）：

```bash
uv run pytest tests/unit/test_identity_provider_runtime.py \
  tests/unit/test_feishu_identity_provider.py \
  tests/unit/test_lark_oapi_identity_adapter.py \
  tests/unit/test_identity_channel_credentials.py
REQUIRE_SERVICES=1 uv run pytest tests/integration/test_identity_channel_credentials.py
```

最终命令覆盖 tenant mismatch、scope/status/error classification、identity cache、refresh/invalidate、
live Auth 顶层 token/expire、三 endpoint envelope 与阶段化分类。完成证据：广义 I4+F1
**118 passed in 5.29s**；credential 真 PostgreSQL **1 passed in 0.76s**；`make verify` 全绿
（Ruff format 1234 files、Ruff check、7 import contracts、async gate、mypy 78 files、unit
**2123 passed in 34.53s**）；`REQUIRE_SERVICES=1 make integration` **84 passed in 12.52s**；
production adapter 三步脱敏 sandbox 通过。旧 stdin-collision sandbox 不计入验收。

EIM-I3 的快速证据必须分别保留纯领域与真库边界；它们不能替代上面的完整门禁：

```bash
uv run pytest tests/unit/test_identity_domain.py
REQUIRE_SERVICES=1 uv run pytest tests/integration/test_identity_repository.py
```

当前定向结果分别为 **40 passed** 与 **17 passed**，合计 **57 passed**；连续 repository + schema
**40 passed**。完整结果：`make verify` unit **1969 passed in 28.85s**，静态门禁全绿；
`REQUIRE_SERVICES=1 make integration` **82 passed in 12.48s**。

EIM-I6 的快速证据必须覆盖 domain/model 与四个真库面，且不能替代最终完整门禁：

```bash
uv run pytest tests/unit/test_identity_domain.py \
  tests/unit/test_identity_provisioning_domain.py \
  tests/unit/test_identity_models.py \
  tests/unit/test_principal.py
REQUIRE_SERVICES=1 uv run pytest tests/integration/test_identity_schema.py \
  tests/integration/test_identity_provisioning_schema.py \
  tests/integration/test_identity_repository.py \
  tests/integration/test_identity_provisioning.py
```

完成结果为 domain/model unit **143 passed in 5.49s**，四个 identity 真 PostgreSQL 面
**84 passed in 7.80s**。完整 `make verify` unit **2188 passed** 且静态门禁全绿；完整强制
integration **128 passed in 16.40s**。

EIM-C1 / CHN-X5 的快速回路必须同时覆盖 DTO、private HTTP 与 resolver authority 不变性：

```bash
uv run pytest tests/unit/test_channel_identity_assertion.py \
  tests/unit/test_channel_execution_api.py \
  tests/unit/test_channel_runtime_client.py
make verify
make smoke
```

定向断言至少要证明：旧 actor dump 不出现 `identity:null`；runtime client 的 legacy body 不变；新
assertion 可解析但不能替代 legacy subject；重复 kind、Provider mismatch 与 authority 字段夹带返回
422；有无 assertion 得到同一服务端 Tenant/target/execution owner。

2026-08-13 本地证据：以 `HEAD a0581f2f` 为基线、只应用 C1 13 条路径的隔离 clean tree 执行等价
完整 `make verify` 全绿——Ruff format **1242 files**、Ruff check、**7** 条 import contracts
（832 files / 2611 dependencies）、async DB gate、mypy **81 source files**、unit
**2223 passed in 32.28s**。C1 三文件定向 **74 passed in 12.94s**。当前运行 API 的通用
`make smoke` 也为 **PASS**：ping/healthz 均 HTTP 200，db/chat/db_pool/redis/doc_engine/storage
全部 `ok`；但该进程仍是旧 API，没有加载 C1，因此这不是新 private DTO 的部署证据。

用户已批准并尝试切换；新 API 在 serving 前被存量 schema bootstrap 顺序缺陷阻断，未产生混合版本
请求。先完成 EIM-I2.2 / CHN-O15 的备份、无 CASCADE 安全恢复及新 API smoke，再让仍运行的旧 worker
调用新 API，确认请求成功、日志无 `extra_forbidden`，且日志不含 assertion tenant key、完整 external
ID、token 或请求正文。完成前 C1/X5 保持 `🔵`，不得宣称 deployed，也不得进入 C2 emit。

涉及启动、路由、JWKS 端点：启动受控服务后追加：

```bash
make smoke
```

`of_mcp` 代理必须先读该仓自己的 `AGENTS.md`/README/CI，再记录等价的 lint、typecheck、unit、
integration 命令；不得把 MultiRAG 的门禁命令机械复制过去。真实飞书和业务沙箱 E2E 需要管理员
明确批准，且必须使用测试企业/测试患者数据。

EIM-A4 的最终证据至少要记录以下命令，不得只运行一个 happy-path HTTP 测试：

```bash
uv lock --check
uv run --locked pytest packages/ofmcp-auth/tests packages/ofmcp-core/tests \
  packages/ofmcp-devkit/tests apps/gateway/tests
uv run --locked ofmcp contract diff
uv run --locked ofmcp verify
git diff --check
```

当前提交锚点为 of_mcp `74117a0`：上述定向命令 **201 passed**，完整 `ofmcp verify` 六步全绿、
**417 passed、2 existing skipped**，contract snapshot 无漂移；MultiRAG 文档收口后的 `make verify`
全绿、**1904 passed**。`contract diff` 的可读输出不能替代 `ofmcp verify` 的 no-drift gate，A4 单仓
全绿也不能替代未来 A2/P3/飞书链的跨仓 E2E。

EIM-A6 phase 1/后续收口复用同一完整门禁，并必须显式包含 replay/audit/execution/telemetry 测试：

```bash
uv lock --check
uv run --locked pytest packages/ofmcp-auth/tests/test_replay.py \
  packages/ofmcp-auth/tests/test_security_execution.py \
  packages/ofmcp-auth/tests/test_security_telemetry.py \
  packages/ofmcp-auth/tests/test_fastmcp_adapter.py \
  packages/ofmcp-core/tests/test_tool_policy_registry.py \
  apps/gateway/tests/test_auth.py
uv run --locked ofmcp contract diff
uv run --locked ofmcp verify
git diff --check
```

当前 A6/Gateway 定向 **65 passed**，完整 verify **453 passed、2 existing skipped**；提交锚点统一以
ROADMAP 变更日志为准。不得因此把当前 phase 1 改成 `✅`。生产 durable
store/KMS/collector 与跨仓 E2E
需要各自额外证据，单元 fake 和内存 store 不能替代。
