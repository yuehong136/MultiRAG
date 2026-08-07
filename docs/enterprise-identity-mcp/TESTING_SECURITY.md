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
- 审计日志的完整性、幂等记录和确认记录。

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
- `open_id` 的唯一键必须包含 `provider_account_id`，`user_id` 的唯一键必须包含 `tenant_key`；
- 目录验证失败、用户停用、企业映射冲突时必须 fail closed；
- `platform_user_id` 始终是 MultiRAG `User.id`，不是飞书 `open_id`；
- `enterprise_subject_id` 与 `platform_user_id` 分离，员工号不能成为公开认证凭据；
- MCP access token 必须校验 `iss`、`aud/resource`、`exp`、`nbf`、`jti`、算法和签名；
- MultiRAG 不把飞书 access token、用户 OAuth token或收到的外部 bearer token透传给 `of_mcp`；
- `of_mcp` 不采信参数中的 `workcode`/`talent_id` 作为调用者身份；
- 高风险工具必须同时满足授权、用户确认、短时有效和幂等；
- 日志不得记录 secret、完整 bearer token、OAuth code、手机号或患者敏感正文。

## 3. 测试分层

### 3.1 MultiRAG 单元与 HTTP 契约测试

遵循仓库根 [AGENTS.md](../../AGENTS.md) 的三形态规范，新增测试放在 `tests/unit/`。

| 范围 | 必测内容 | 推荐测试形态 |
|---|---|---|
| Channel DTO | tolerate/emit/remove 各半步、`extra="forbid"`、旧/新进程组合 | Pydantic 纯测试 + private HTTP 契约测试 |
| Feishu assertion | 保留 `open_id/user_id/union_id/tenant_key/app_id`，不再 first-nonempty | 纯函数测试 |
| IdentityService | JIT、link-only、冲突、停用、缓存、事件失效 | async service + monkeypatch 官方客户端 |
| DB | 唯一约束、事务并发、别名归一化、幂等事件 | `tests/integration/` 真 PostgreSQL |
| Principal | 未验证 subject 不提升；验证后携带 tenant/membership | execution 路由契约测试 |
| MCP token | claims、TTL、JWKS、轮换、scope 交集 | 纯密码学 + HTTP 契约测试 |
| MCP client | resource/audience、失败映射、无静态用户 header | mock transport/官方 SDK 测试 |

最少必须覆盖这些命名场景：

1. 同一个飞书用户通过同一企业的两个应用进入：`open_id` 不同、`user_id` 相同，最终只能有一个
   enterprise subject 和一个已绑定平台账号。
2. 两个企业碰巧出现相同 `user_id`：因为 `tenant_key` 不同，绝不能合并。
3. 同一个 `open_id` 字符串出现在不同应用：因为 `provider_account_id` 不同，绝不能合并。
4. `open_id`、`user_id` 同时出现但目录返回的用户不一致：返回 `IDENTITY_CONFLICT`，不猜测。
5. 飞书返回用户 `status.is_activated=false`、已离职或不可见：拒绝建立/使用会话。
6. 已存在 alias 的用户更换部门或姓名：身份主体不变化，只更新 profile 快照。
7. 两个并发首次消息同时 JIT：数据库最终只能生成一条 identity、一条 subject link；另一事务安全重读。
8. 目录接口超时且无可用缓存：拒绝首次登录；已有短期正缓存可按策略工作并打 `stale` 指标。
9. 负缓存命中后收到 `contact.user.updated_v3`：负缓存立即失效，可重新验证。
10. `ChannelActor.subject` 被任意伪造：在 C3 之后仍不能直接进入 `Principal.id`。

### 3.2 MultiRAG 集成测试

涉及 identity 表、唯一约束、事件收件箱和 token replay 表时，必须使用
`bootstrapped_async_engine`，并至少验证：

- schema upgrade 与 downgrade/rollback 路径；
- 部署旧代码读取新表时不受影响；
- 部分唯一索引能允许 nullable alias，同时拒绝同一边界内重复；
- `SELECT ... FOR UPDATE` 或唯一约束重试能收敛首次绑定竞态；
- event receipt 的 `(provider_account_id, event_id)` 幂等；
- `jti`/confirmation/idempotency key 在并发提交下只消费一次；
- 删除/停用不是只清缓存，数据库状态也能阻断下一次请求。

改 DB 后除了 `make verify`，还必须按 AGENTS.md 运行 `make integration`。服务不可用导致 skip
不能作为验收；CI 或受控环境必须用 `REQUIRE_SERVICES=1` 跑出真实结果。

### 3.3 `of_mcp` 测试

`of_mcp` 自己必须建立以下测试层，不得只依赖 MultiRAG 测试：

- auth middleware：无 token、错 issuer、错 audience、错 resource、过期、未来 `nbf`、未知 `kid`、
  `alg=none`/算法混淆全部拒绝；
- JWKS：正常刷新、缓存、key rotation 双钥窗口、未知 key 强制刷新一次、上游失败 fail closed；
- scope policy：token scope 与工具声明 scope 取交集，缺一项即在调用工具前拒绝；
- business subject：只能从已验证 claim 读取 `enterprise_subject_id`，请求参数的 `workcode` 被忽略；
- sensitive tool：确认挑战、过期、不同用户/不同参数重放、双击和并发只执行一次；
- audit：成功、拒绝、确认、业务异常都生成同一 correlation chain，且无 token/患者正文。

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

## 4. 安全专项测试

### 4.1 身份和租户攻击

- 修改 private API 的 `tenant_key`、`provider_account_id`、所有 ID 排列组合；
- 把另一个租户合法 assertion 搬到当前 binding；binding 的服务端配置必须否决它；
- 用 `union_id`/`open_id` 伪装成企业 `user_id`；类型化 identifier 必须阻止混用；
- 员工号重复、复用、空值、前导零、大小写、离职后重新入职；任何歧义都进入人工处理；
- 解绑后立刻重新绑定另一个账号；必须有审计、冷却或管理员确认策略。

### 4.2 令牌和 confused-deputy 攻击

- A 用户 token 调 B 用户确认记录；A Agent token 调 B Agent 资源；
- 为 MultiRAG API 签发的 token 拿去调用 `of_mcp`；audience/resource 不同必须拒绝；
- 把飞书 tenant access token 放进 MCP Authorization；issuer/格式不符必须拒绝；
- JWT header 注入 `jku`/`x5u` 或外部 JWKS URL；服务端只能使用静态配置的 issuer/JWKS；
- `kid` 路径注入、超大 JWT、重复 claim、时钟边界；限制大小并使用成熟库解析；
- 下游返回 token 请求继续委托；禁止 token exchange 之外的任意 bearer 转发。

### 4.3 Channel 与卡片攻击

- 伪造或重放 `card.action.trigger`，确认 nonce 必须绑定用户、租户、工具、参数摘要和过期时间；
- 长连接重复投递、进程重连、两个连接同时收到同一事件；dedupe 必须覆盖；
- prompt 声称“我的工号是…”、“切换到管理员”；身份上下文不可由模型文本覆盖；
- 卡片字段夹带 `principal_id`、scope、任意 URL；服务端按 allowlist 读取字段；
- 群聊/话题串会话键碰撞；键中必须包含 provider、tenant/account、conversation/thread。

### 4.4 高风险工具攻击

- 参数排序、空白、等价 JSON 试图绕过确认摘要；先 canonicalize 再 hash；
- 第一次执行超时后再次确认；业务幂等键必须能区分“未执行”和“结果未知”；
- 用户确认后管理员撤权、患者状态改变；执行前重新做授权和业务前置检查；
- Agent 把读工具伪装成写工具或反之；风险级别由服务端注册表定义，不由模型声明。

## 5. 可观测与审计契约

每条用户消息生成 `trace_id`；渠道事件同时保留 provider `event_id`，MCP 调用生成 `mcp_call_id`。
审计记录建议字段：

```text
occurred_at, trace_id, event_id, mcp_call_id
provider, provider_account_id_hash, tenant_id
platform_user_id, enterprise_subject_id
agent_id, tool_name, decision, reason_code
token_issuer, token_audience, token_jti_hash, scopes
confirmation_id, idempotency_key_hash, latency_ms, result
```

必须 hash 或省略：`open_id`、`user_id`、`employee_no`、邮箱、手机号。绝不记录：secret、token
原文、OAuth code、医疗正文、完整工具参数。只有受控审计库可保存业务必要的可逆映射，应用日志不可保存。

最低指标：

- identity resolve 的成功/拒绝/冲突/目录错误和 p50/p95/p99；
- JIT 创建、人工待审批、停用命中、缓存命中与 stale 使用量；
- contact event lag、对账扫描滞后、漏事件修复数；
- MCP auth 拒绝原因、工具授权拒绝、确认过期、幂等命中；
- 每 binding 收/丢/重复消息数和端到端时延。

告警不得只报“500”。至少按 `IDENTITY_*`、`MCP_TOKEN_*`、`MCP_SCOPE_DENIED`、
`CONFIRMATION_*`、`DIRECTORY_UNAVAILABLE` 分组。

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

涉及数据库/身份存储：

```bash
REQUIRE_SERVICES=1 make integration
```

涉及启动、路由、JWKS 端点：启动受控服务后追加：

```bash
make smoke
```

`of_mcp` 代理必须先读该仓自己的 `AGENTS.md`/README/CI，再记录等价的 lint、typecheck、unit、
integration 命令；不得把 MultiRAG 的门禁命令机械复制过去。真实飞书和业务沙箱 E2E 需要管理员
明确批准，且必须使用测试企业/测试患者数据。
