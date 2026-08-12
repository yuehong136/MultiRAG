# 企业身份与 MCP · 零上下文 Agent 执行手册

> 目标：一个没有任何历史对话的 Agent，只凭本目录就能安全接手一个独立任务。
> 本文规定工作方式；技术事实以 [CONTRACTS](CONTRACTS.md)、[DECISIONS](DECISIONS.md) 和任务行本身为准。
> 跨程序共通的四条不变量（说 ID 不说需求 / 提示词只补哪三样 / 验证基线要自测 /
> agent 记忆不跨机器）在 [`AGENTS.md` 的「零上下文交接」](../../AGENTS.md#零上下文交接)；
> 本文是 EIM 特有的加码（跨两个仓、外部管理后台操作要批准），不重复那四条。

## 1. 接单格式

用户应尽量按 ID 派工：

```text
读 docs/enterprise-identity-mcp/README.md 和 AGENT_RUNBOOK.md，执行 EIM-P1。
先复核依赖和当前代码；按 ROADMAP 维护协议记账。涉及部署或外部管理后台操作时先停下来征得批准。
```

一个 Agent 一次只负责一个 ROADMAP ID。任务本身确实要求跨仓原子完成时，才在同一次工作中操作
MultiRAG 与 `of_mcp`；不要把相邻任务“顺手”并入一个提交。

## 2. 开工前必须完成

### 2.1 读文档

所有任务先完整阅读：

1. [README](README.md)：最终结论、边界、文档索引；
2. [ROADMAP](ROADMAP.md)：任务状态、依赖、验收、部署顺序；
3. 本文；
4. 当前仓库的 `AGENTS.md`。

再按任务类型补读：

| 任务前缀 | 必读 |
|---|---|
| EIM-F | [VERSION_BASELINE](VERSION_BASELINE.md)、[REFERENCES](REFERENCES.md) |
| EIM-I | [DECISIONS](DECISIONS.md)、[CONTRACTS](CONTRACTS.md)、[ARCHITECTURE](ARCHITECTURE.md) |
| EIM-C | 上述三份 + `docs/channel-program/README.md`、`PROGRESS.md`、`CONTRACT.md` |
| EIM-P / EIM-A | [CONTRACTS](CONTRACTS.md)、[TESTING_SECURITY](TESTING_SECURITY.md)、`of_mcp` 本仓说明 |
| EIM-M | [ARCHITECTURE](ARCHITECTURE.md)、[REFERENCES](REFERENCES.md)、[TESTING_SECURITY](TESTING_SECURITY.md) |
| EIM-U | [FEISHU_BOT_UX](FEISHU_BOT_UX.md)、[FEISHU_ONBOARDING](FEISHU_ONBOARDING.md)、[TESTING_SECURITY](TESTING_SECURITY.md)、Channel `PROGRESS.md` |
| EIM-O | [FEISHU_ONBOARDING](FEISHU_ONBOARDING.md)、[TESTING_SECURITY](TESTING_SECURITY.md) |

### 2.2 核实工作区与锚点

```bash
pwd
git status --short
git rev-parse --show-toplevel
rg -n "目标符号或旧契约名" api common tests docs
```

- 工作树里的既有改动属于用户；不覆盖、不 reset、不混入本任务。
- ROADMAP 和 REFERENCES 里的路径是时点快照。先用 `rg` 找当前符号，再更新任务锚点。
- 在 `of_mcp` 仓工作时（另一个独立 checkout，路径随机器而变），重新读取**那里实际存在的**
  `AGENTS.md`；MultiRAG 规则不能代替另一个仓库的规则。
- 核对 ROADMAP 依赖均为 `✅`。依赖未满足时不要跳过半步，记录阻塞并停止。

### 2.3 确认任务尚未完成

不要因为任务行是 `⬜` 就假定代码没落地。必须同时检查：

```bash
git log --oneline --all --grep='EIM-I3'
rg -n "相关模型、迁移、测试或错误码" .
```

如果代码已存在但账本未更新，先验证实现与本文契约是否一致；只补账也必须写清证据，不能重复实现。

## 3. 设计约束速查

任何实现都不得违背：

- MultiRAG 是多租户平台，但首期一个飞书企业强制对应一个 MultiRAG tenant；同一 Provider Account/
  应用安装实例和 Channel binding 永远固定单 Tenant。未来集团级多 Tenant 必须使用不同安装实例，
  经新 ADR/schema 迁移受控开放，不允许一个 binding 动态路由；全体员工是“可识别主体”，不等于
  自动拥有所有资源权限；
- Provider Account 是独立企业连接，Channel 通过 tenant/provider-safe 的显式一对一 link 引用；
  account 可无 Channel，但在 Provider credential vault 完成前不得调用 Provider API。飞书 Secret
  仍在 ChannelSecret，不能把 I2.1 宣称为凭据已解耦；
- `CustomerOrganization` 首期只作目标术语并与 Tenant 1:1，不落表、claim、API 或运行时路由；
- `platform_user_id = MultiRAG User.id`；飞书长连接返回的 `open_id` 只是外部 alias；
- 飞书规范主键为 `(tenant_key, user_id)`；`(provider_account_key, open_id)` 是解析入口；
- 不做每日全量组织复制：首次 JIT + Contact 事件失效 + 周期性增量/对账；
- 身份服务和表归 MultiRAG；`of_mcp` 是受保护资源，不拥有企业主身份库；
- MCP 使用短时 ES256 access token、JWKS、resource/audience 校验；不透传外部 token；
- 业务员工号是 enterprise subject 映射，不是登录凭据；歧义时人工处理；
- 敏感写操作必须授权 + 确认 + 幂等；参数里的 `workcode` 不可信；
- 先平台资源授权，再 MCP 工具 scope，再业务对象授权，三层都不可省略。
- MCP 2026 MRTR 只承载“需要补充输入/恢复调用”；不能把 `input_required`、飞书表单 `confirm`
  或按钮点击当授权事实，恢复前仍要重验 Principal、scope、业务授权、参数 digest 和幂等键。
- EMA 是官方 auth extension，MultiAuth 是 FastMCP 实现，Horizon 是厂商托管产品；三者不可互换。
  Tasks/Apps 也不是飞书表单闭环的首期前置依赖。

出现与这些约束冲突的新事实时，不要自行改方向。新增 ADR/任务 ID，写证据并请用户决策。

## 4. 实施步骤

### 4.1 把任务置为进行中

在 [ROADMAP](ROADMAP.md) 对应行改成 `🔵`，同时写清更新后的文件/符号锚点。如果任务映射到
`CHN-*`，也按 Channel `PROGRESS.md` 协议同步置为进行中。一个任务不要同时让两个 Agent 修改。

### 4.2 先写契约/测试，再改实现

推荐顺序：

1. 把 CONTRACTS 的提案形状与当前任务需要的最终形状对齐；
2. 写失败测试，包含至少一个拒绝路径；
3. 做最小实现；
4. 跑快速测试；
5. 处理迁移、兼容半步和文档；
6. 跑完整门禁。

Channel private DTO 必须按 tolerate → emit → consume → remove 分 PR。共享模型有默认值也可能被
FastAPI 自动序列化出去，因此 tolerate PR 必须用线格测试证明旧 payload **逐字节不变**。

### 4.3 数据库任务

- 新 service 一律 async-first，使用 `AsyncSession`，遵守根 AGENTS.md 的 session 规则；
- 迁移只新增表/列/索引时先保证老代码可运行，再切读写，最后才删除旧字段；
- 用数据库约束守住身份唯一性，不只靠应用层“先查再插”；
- I2 必须先用 provider-tenant ownership 强制外部企业单 Tenant，再让 provider account、alias 和
  receipt 通过复合外键逐级继承同一 tenant/provider scope；只给每张业务表加 `tenant_id` 不等于守住
  account/binding ownership；
- I2.1 必须保留已提交 I2 revision，使用 forward migration 把旧 `account.channel_id` 无损 backfill
  到显式 link；link 两端同时绑定 tenant/provider scope、首期双唯一且 `ON DELETE RESTRICT`。
  无 link account 合法；downgrade 只有 account/link 两表都为空时允许，任一有数据都 fail closed；
- I3 不把 ownership 实现成普通 CRUD：普通路径禁止修改 account 的 tenant/provider scope，禁止
  unlink/rebind link 的 account/channel scope，也禁止 hard-delete；verified onboarding/rotation 用
  显式领域方法。状态和 revision 更新必须使用
  `identity_revision` CAS，或在同一事务 `SELECT ... FOR UPDATE` 后复核 revision，冲突 fail closed；
- 身份历史外键使用 `ON DELETE RESTRICT`；有数据 downgrade 必须 fail closed，不用 CASCADE 或清表
  换取回滚成功；
- 迁移前输出冲突审计，只读脚本不得自动猜测合并；
- 测试里使用 scratch 数据库，绝不连接配置中的生产 dbname。

#### EIM-I3 当前边界与后续接手

I3 已完成，代码锚点为 `api/identity/contracts.py`、`policy.py`、`service.py`、
`validation.py`、`repository.py`。接手者先保持以下边界，不要把 I4/I6/P1/C3 混回本任务：

1. identity core 输入是服务端构造的 `ProviderContext + AliasKey`，没有 `channel_id`；
2. `IdentityService` 只依赖 `IdentityLookupRepository` 和 policy resolver，不取得 ordinary mutation、
   verified identity mutation、provider-account control 或 verified-ownership port；
3. resolution 是一条 account-rooted SQL snapshot，同时带 account revision/scope marker、alias proof、
   identity 与 live User/UserTenant；`None` 仅表示 context 无效，`identity=None` 才表示有效 account 下
   alias miss；旧 alias proof 要求 Provider 重验，identity conflict/revoked 终态优先于 freshness且不提示
   可重验；
4. provisioning 三态只返回 `provider_verification_required` plan，不调用飞书、不创建
   `User/UserTenant`、不激活 identity、不构造 Principal；
5. write 前锁 account 并复核 revision + scope marker，从 account 派生 scope；ordinary mutation 只允许
   pending identity insert/单向收紧 CAS，verified identity mutation 独占 alias refresh/activation，
   provider-account control 独占 health/scope/event CAS；conflict/revoked 终态；
6. 旧 alias proof 早于 scope marker 时拒绝，较旧 proof 不倒退已有时间；health CAS 省略时间保留旧值、
   显式时间只前进。Core DML 显式写审计时间，输入和 SQLAlchemy/driver 异常稳定脱敏；
7. ownership 只有两个 verified insert 命令且不属于聚合 identity repository；所有 port 禁止 generic
   CRUD/commit/delete 和 Channel unlink/rebind，调用方拥有事务；
8. 完成证据为 unit **40 passed**、真 PostgreSQL **17 passed**，定向共 **57 passed**；repository +
   schema 连续真库 **40 passed**；`make verify` unit **1969 passed in 28.85s** 且静态门禁全绿；
   `REQUIRE_SERVICES=1 make integration` **82 passed in 12.48s**。

I3 完成后优先可做 `P1`。`I4` 还依赖 `F1`，只能在 F1 完成后接入 Contact Provider；`I6` 再拥有
User/UserTenant/link 的完整事务，C3 才把 Channel assertion 组合成 Principal。FastMCP 4 的 auth 与
tool list/call 能力仍在 MCP composition/adapter 层复用，不进入上述 identity domain。

### 4.4 外部 API 和 SDK 任务

- 优先官方文档和官方 SDK；确实需要搜索时只采信官方文档、PyPI/npm 和官方 GitHub release；
- 版本任务必须重新执行 [VERSION_BASELINE](VERSION_BASELINE.md) 的查询，不能沿用日期已过的“最新”；
- 参考项目只借设计和测试思路，按 [REFERENCES](REFERENCES.md) 的“采用/不采用”边界执行；
- 复制代码前核对 license、保留版权要求，并在 PR 说明具体来源路径和 commit SHA；
- 不把 SDK 的全局事件循环或重依赖 eager import 到 API 进程，provider/transport 按需加载。
- EIM-U0/U1 不得顺手迁移 `lark-channel-sdk`；EIM-C5 也不得改变 ReplySession、执行事件或用户
  体验语义。先证明稳定 Provider 契约，再让两条支线独立演进。
- 飞书流式更新必须有节流、严格 sequence、final flush、finish 和 post/text fallback；不得每个
  token 调一次 OpenAPI，也不得在 fallback 时重新执行 Agent。
- 卡片状态只用服务端白名单；不展示 chain-of-thought、原始 tool trace、MCP 参数或底层异常。

MCP Foundation 的 F2/F3/F4/F6/F7/F8 已于 2026-08-12 完成。冷启动 Agent 必须先区分下面两层：

1. **历史迁移顺序（保留为审计/重做约束）**：F2 先用独立解释器、独立 lock、独立进程固定
   MCP 1/2 双向 characterization；F6 再证明 FastMCP 3 `mcp<2` 与 MCP SDK 2 `mcp>=2` 的
   依赖冲突并选择边界；F4 在 `of_mcp` 独立完成，MultiRAG 的 F7 依赖切换、F3 outbound Client
   与 F8 inbound Server 则在 F6 决策后按整根方案原子落地。不得把尚未完成的身份或授权功能
   混进协议升级。
2. **当前根代码运行时基线**：MultiRAG 根运行时统一使用 MCP SDK 2/FastMCP 4；outbound HTTP 使用
   官方高层 `Client` 的现代优先、legacy 自动协商，inbound Server 进入 `2026-07-28` modern era
   并保留 legacy 协议兼容。F6 因当前没有 FastMCP 3 生产消费者，经用户明确批准选择了全根协同
   升级，而不是原默认的 `mcp/server` 独立 project/venv。
3. **真实 legacy 只留在测试边界**：FastMCP 3 通过 PEP 723 锁定的真实子进程 fixture 运行，
   不与根环境在同一解释器导入。`make mcp-compat` 当前协议矩阵为 **13/13 PASS**；任何后续
   SDK/FastMCP/transport 改动都必须复跑，而不能用 mock 或 sibling import 替代。
4. **下一协议相关入口**：EIM-A1 已完成并固定 token/JWKS test vectors；of_mcp EIM-A3 已由
   `e4ab560` 完成 strict Resource Server，EIM-A4 已由 `74117a0` 接续 immutable Principal、
   per-tool policy 与真实 403。MultiRAG EIM-A7 的 A1 前置已满足，仍必须等待 P1，才实现自己独立的
   inbound OAuth Resource Server、Principal/scope 和工具可见性；
   `InputRequiredResult` 目前只由 EIM-F3 暴露，EIM-U14 还必须等待 P3/A4/C3，才实现持久化暂停/恢复、
   Principal 绑定、revision/CAS 和重授权。当前协议升级**不证明** Principal、scope、delegated token、
   OAuth Resource Server 或 InteractionSession 已在 MultiRAG/Channel 端到端实现；of_mcp A4 的完成态
   不能替代 P1/P2、A2/P3 或飞书 identity resolver。

HTTP 调用超时的已验证契约是：本地等待及时取消，直接并发调用不再被单 server FIFO 队列产生
head-of-line blocking；远端是否停止取决于 transport/server 的协作式取消，服务端仍可能完成调用。
有副作用的工具因此仍必须依靠授权、Confirmation 和业务幂等，不能把客户端 timeout 当作“未执行”。

### 4.5 密钥和外部配置

开发前按 [FEISHU_ONBOARDING](FEISHU_ONBOARDING.md) 取得测试应用配置。Agent 只能说明或消费用户
明确提供的 secret，不能把 secret 写进源码、测试快照、命令输出或文档。

本地配置应进入已 gitignore 的 secret/env 载体；测试使用低熵、明显虚假的占位值。需要管理员在
飞书后台授权、发布版本、修改可见范围或重启线上进程时，先给出精确操作和影响，等待用户批准。

### 4.6 EIM-A1 专用执行契约

A1 是跨仓 test/docs/schema/corpus 任务，不是生产 auth 实现。开工先确认 F3/F4 为 `✅`，然后：

1. 在 MultiRAG `tests/fixtures/eim_a1/v1/` 和 of_mcp
   `packages/ofmcp-contracts/tests/fixtures/eim_a1/v1/` 保存字节一致的 corpus；
2. 使用 MultiRAG canonical `scripts/generate_eim_a1_vectors.py` PEP 723 脚本，以 test-only P-256 key
   和 deterministic RFC 6979 ES256 生成固定 token、manifest/schema、public JWKS 和 `SHA256SUMS`；
3. `manifest.json` 必须分开 `cases`、`issuance_policy_cases`、`delegation_cases`；token case 用
   `token_file/jwks_file` 引用 corpus，公开/内部错误分别用 `oauth_error/failure_reason`；不能把 signer
   拒绝、gateway 换发拒绝和 Resource Server 401 混成一种结果；cross-profile 至少用同一份合法
   `mcp_internal_actor` compact bytes 证明“目标 proxy 接受、Gateway 拒绝”，不能只测 malformed hybrid；
4. MultiRAG 用显式 `PyJWT[crypto]==2.13.0` direct dev dependency，of_mcp 用显式
   `joserfc==1.7.4` direct test dependency；两边各有项目 profile oracle，不共享 verifier；
5. corpus 测试关闭密码库自身 wall-clock `exp/nbf/iat` 判断，统一用 manifest `validation_time`；
   签名、algorithm、key 和 issuer/audience 仍真实校验；
6. 先提交 of_mcp 的 corpus/tests，再把同一 corpus/tests/docs 提交到 MultiRAG；每个仓独立运行本仓
   全量门禁，最后比较文件集合和 SHA-256。

A1 交付阶段在门禁满足前，ROADMAP 必须保持 `🔵`；尚未产生的 commit 可以明确写 `pending`，但不能
预填或猜测 SHA，corpus digest 必须从实际文件计算。本轮已完成两仓独立 oracle、全部 case 无
skip/xfail 和 corpus 字节一致性检查，ROADMAP 保持 `✅`。最终 corpus 为 91 files、79 token +
7 issuance + 5 delegation，摘要为
`59f82684aa06365f45623ce9bfad336d487f2c9351879266a6b2ab21bf8fe208`。当前验证证据为 MultiRAG
定向 **96 passed**；完整 `make verify` 的 Ruff format/check、6 条 import contracts、async DB gate、
mypy 65 files 全绿，unit **1904 passed in 25.76s**。of_mcp `3e1d5ac` 定向 **100 passed**、完整
门禁 **216 passed、2 existing skipped**。

A1 禁止顺手添加生产 issuer/verifier、FastMCP `auth=`、JWKS HTTP route、KMS/DB、Channel Principal、
动态 Authorization 或真实 Secret。A3 才把基础 JWT/JWKS verifier 与严格项目 validator 装配到
of_mcp composition root；A2/P3 才签发和传递 request-scoped token。

### 4.7 EIM-A3/A4 完成基线与下一任务边界

修改 A3 认证基线时，以 of_mcp `e4ab560` 为完成锚点。A3 实现包含
`packages/ofmcp-auth/`、Gateway `auth.py/settings.py`、`resource_auth_mode` 和
`deploy/profiles/secure.toml`。不得回退以下不变量：

1. production strict verifier 真实验 ES256，并独立校验 A1 profile；runtime 不 import
   `ofmcp-contracts` 的测试 oracle；
2. FastMCP `RemoteAuthProvider` 只承载 RFC 9728 metadata/discovery 与框架接口，精确 `/mcp` bearer
   和 typed `VerifierUnavailable -> 503` 由项目 middleware 控制；公开 route 不触发 JWKS；
3. JWKS 只从固定 HTTPS URI 拉取，拒绝 redirect/env proxy，大小与 key 数有界；cache 是 fresh
   last-known-good + atomic replacement + single-flight + per-key negative cache + 全局 unknown-kid
   refresh cooldown + 负缓存硬上限；慢失败从完成时钟开始 backoff，过期后 fail closed，NaN/Infinity
   配置拒绝；
4. `local` 是匿名开发 profile；`secure` 缺配置即 fail-fast、hello disabled、mount-only。A5 前 proxy
   不能进入 secure Gateway；production scope/profile constants 必须逐字段匹配 A1 manifest；
5. production `required_scopes=[]`。这不是放弃授权，而是避免 endpoint 级 union-of-all-scopes；
   test-only 403 seam 继续保留，真实 `tool -> required scopes` 由 A4 registry 独立执行；
6. health 只返回最小状态；日志、HTTP 错误和 fixture 不包含 bearer、claims、subject、患者或真实配置；
7. A3 handoff 时 local/secure 都由 CLI 机器拒绝非 loopback；CLI 与 `fastmcp.json` 都启用
   `host_origin_protection=auto`。A4 后这条限制继续作为独立 remote-release gate。

在 `of_mcp` checkout 至少执行并记录：

```bash
uv run --locked pytest packages/ofmcp-auth/tests apps/gateway/tests packages/ofmcp-core/tests packages/ofmcp-devkit/tests
uv lock --check
uv run --locked ofmcp verify
git diff --check
```

以上 A3 完成证据为定向 **126 passed**，production verifier 其中逐条消费 A1 的 **68** 个 Gateway
authentication cases；`uv run --locked ofmcp verify` 六步全绿、**342 passed、2 existing skipped**，
contract 无漂移。后续触碰 A3 路径必须至少复跑上述命令并保持这些安全负向。

A4 以 of_mcp `74117a0` 为完成锚点；冷启动 agent 必须同时阅读以下实现面，不能只看
FastMCP middleware：

1. `packages/ofmcp-auth/.../claims.py` 与 `context.py`：A3 allowlisted claims 立即投影成 framework-
   independent immutable Principal；role/group/department/Provider raw ID 不进入模型，请求结束恢复
   context；A1 Gateway accept vectors 全部走 bridge；
2. `packages/ofmcp-core/.../tool_policy.py`、`assembly.py` 与各 service `service.toml`：registry 针对
   post-assembly canonical tool catalog 完整构造；缺失/孤儿/重复/namespace collision/未在当前
   service scope 词表登记的 scope fail closed；
3. `apps/gateway/contract/tool-policies.json` 与 devkit contract：snapshot 排序稳定，
   A4 历史 revision 为 `6f79e7ddf8f630993a054f284ebd5213424ffe39b252c661d16a2967ed6fdd67`；A6
   把 effect/replay mode 纳入 format 2 后当前 revision 为
   `7bf9e09082ca4f1d529e51bf3fe8e6dd5c4334c62deaf9a5b6be204af9fca446`。策略
   变更必须通过 snapshot review，不能只改运行时；
4. 外层 ASGI authorization preflight：对真实 MCP `tools/call` 返回标准 HTTP 403；内层 FastMCP
   middleware：用同一 registry 过滤 `tools/list`，并在工具执行前再次授权。两层都要保留，不能因为
   component denial 最终也能产生 MCP error 就删除外层 HTTP 语义，也不能因为 preflight 存在就删除
   call-before-execute 的 TOCTOU 重验；default SSE response guard 还会把响应起始消息保留到内层检查
   完成，确保第二次拒绝/基础设施故障仍是真实 403/500，而不是 FastMCP 默认的 HTTP 200 tool error；
5. tenant、ACR/AMR 与可选 enterprise subject 已进入 A4 policy；external membership/role/business
   resolver 只是 fail-closed future seam，当前 service policy 未启用。resolver missing/invalid/failure
   是 500 invariant failure；raw business arguments 尚未形成 schema-normalized object authorization；
6. `auth_time` 只携带并校验 `<=iat`，没有 freshness/max-age；local anonymous 与 secure bearer profile
   继续 machine-enforced loopback。A4 不交付 A2/P3、飞书 Principal、A5/A6、M1/M2 或 U14/U15。

在 `of_mcp` checkout 对 A4 至少执行并记录：

```bash
uv lock --check
uv run --locked pytest packages/ofmcp-auth/tests packages/ofmcp-core/tests packages/ofmcp-devkit/tests apps/gateway/tests
uv run --locked ofmcp contract diff
uv run --locked ofmcp verify
git diff --check
```

A4 定向 **201 passed**；`uv run --locked ofmcp verify` 六步全绿、**417 passed、2 existing skipped**，
contract snapshot 无漂移。A6 phase 1 已接续且保持 `🔵`；MultiRAG I3 已完成，当前可并行做
`P1`、`F1 -> I4 -> I6`、`C1 -> C2`，只有
`C3 -> P2` 后才能进入 A2/P3。secure 在独立远程发布闸门解除前仍不能作为远程业务入口。

### 4.8 EIM-A6 phase 1 接手与完成边界

冷启动 agent 先确认 ROADMAP 中 A6 仍是 `🔵`，然后按以下顺序读当前 of_mcp 实现：

1. `ofmcp.core.service/tool_policy/policy_snapshot` 与三份 `service.toml`：effect/replay mode 必填，
   `side_effect=>single_use`，真实工具全覆盖，format-2 revision 只有一个 canonical builder；
2. `ofmcp.auth.replay`：atomic `claim` 和不释放的 outcome state machine；memory store 只供测试；
3. `ofmcp.auth.audit`：无 extension bag 的 frozen allowlist、主体 HMAC 与安全 reason code；
4. `ofmcp.auth.security_execution`：单 capability replay key、完整 request fingerprint、pre-execution
   audit、ExecutionPermit 与 outcome；
5. `ofmcp.auth.security_telemetry`：OTel API-only、低基数 counters、异常隔离；
6. `ToolAuthorizationMiddleware.on_call_tool` 与 Gateway composition：A4 final allow 后才 prepare，传入
   同一 policy/revision；secure 要求显式 production-ready coordinator。

不得回退的 phase-1 不变量：

- replay key 只绑定 `(token_use,issuer,audience,jti)`，是整枚 token/JTI 的单一 capability，不按工具
  分桶；fingerprint 才绑定完整 Principal、tool、policy revision 与 canonical arguments。同一 JTI
  只能对应一个高风险逻辑操作，未来 P3 每次执行必须换新短 token/JTI；
- duplicate 只返回 409，不回放结果；不同 fingerprint 返回 403 replay detected；store/audit 前置
  故障返回 503。均 no-store、无 OAuth challenge、业务零调用；
- claim 在业务 dispatch 后不释放。成功为 `SUCCEEDED`；tool error/异常/取消保守为
  `OUTCOME_UNKNOWN`。outcome 持久化失败保留 `DISPATCHED` 且不能篡改业务响应；
- audit/permit/telemetry/log 不含 token、参数、结果、Provider PII、enterprise subject 或医疗正文；
  JTI digest 必须隔离 issuer domain，低熵主体用 keyed HMAC；
- OTel 失败不改变安全决定；API 已接线不等于 SDK/exporter/collector/跨仓 trace 已部署；
- `production_ready` 只能由 shared replay + durable audit 的实现属性成立。memory backend 不得通过
  配置伪装生产；当前真实 secure CLI 应因缺 production backend fail-fast，remote gate 保持关闭。

修改 A6 代码至少运行：

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

本轮 A6/Gateway 定向 **65 passed**，完整 `uv run --locked ofmcp verify` 六步全绿、
**453 passed、2 existing skipped**；提交锚点统一以 ROADMAP 变更日志为准。即使以上全绿，也只有完成
production multi-instance durable replay/audit、HMAC KMS/rotation、OTel SDK/exporter/W3C 跨仓 trace、A5
`parent_jti_hash`、P3 动态 bearer、M3/M4 业务幂等/结果查询和 remote-release 演练后，才能把 A6
改为 `✅`。这类后续工作若涉及真实基础设施、KMS、DNS、Secret 或部署，必须先获得用户批准。

## 5. 跨仓协调

| 变更 | 生产者 | 消费者 | 安全部署顺序 |
|---|---|---|---|
| Channel structured assertion | worker | MultiRAG private API | tolerate API → emit worker → consume API → remove legacy |
| MCP access token | MultiRAG signer | `of_mcp` verifier/authorizer | A3 verifier/JWKS + A4 Principal/tool policy + A6 phase-1 execution guard 已先行并保持业务未远程发布 → P1/P2 → A2 signer emit → P3 每次执行换新短 token/JTI → A6 production durable backend/跨仓 trace + 企业主体/上线证据 → 独立闸门决定 secure 远程入口；不得把固定测试 token 或内存 replay 通过误作 Channel 委托闭环 |
| EIM-A1 corpus | MultiRAG canonical generator + 两仓本地副本 | PyJWT/joserfc 独立 oracle | 已完成：of_mcp `3e1d5ac` → MultiRAG 本次 A1 变更；91-file corpus 字节一致，digest `59f82684aa06365f45623ce9bfad336d487f2c9351879266a6b2ab21bf8fe208`；运行时无依赖 |
| 新 scope/tool metadata | `of_mcp` policy snapshot | MultiRAG Agent/MCP config、P3 cache/audit | resource 端先提交包含 effect/replay mode 的 canonical `tool-policies.json` 与 `policy_revision` → 调用端按 revision 重算请求与缓存；未知 scope fail closed，不从运行时可见列表反推权限，也不把 revision 自动塞入当前 A1 token profile |
| confirmation contract | `of_mcp` challenge | MultiRAG card/channel | resource 端先返回可识别 challenge → UI 接线 → 强制确认 |
| MCP 双向兼容 fixture | MCP SDK 2/FastMCP 4 主运行时 + PEP 723 FastMCP 3 真实 legacy 子进程 | 两仓 compatibility test | F2/F3/F4/F6/F7/F8 已完成并形成 13/13 基线；后续每次协议/transport 变更逐格复跑；`of_mcp` F4 锚点 `23dd1fd`；不得用本机 sibling import 代替可复现安装 |
| MRTR interaction | `of_mcp` `input_required`/legacy adapter | MultiRAG U14 state machine，再到 U15 renderer | 先固定 transport-neutral request/response 与恢复语义 → 飞书表单渲染 → 敏感动作最后强制 U7/M3/M4 |

跨仓任务必须在两边都留下同一个 `EIM-*` ID；完成日志列出两个 SHA。不能只改一侧后把另一侧
写成“后续处理”。如果本次任务只负责 tolerate 半步，要明确写成安全中间态，并保留旧行为。

## 6. 验证规则

### 6.1 MultiRAG

快速回路可按改动范围选择，但宣布完成前必须：

```bash
make fix
make verify
```

涉及 DB/identity storage：

```bash
REQUIRE_SERVICES=1 make integration
```

涉及路由/启动/JWKS：

```bash
make smoke
```

如果工具链版本不满足 `pyproject.toml`，这是环境阻塞，不得改低项目要求换绿。记录实际版本、失败
命令和用户需要执行的升级动作；仍可运行不会破坏环境的静态检查，但不能宣称完整门禁通过。

### 6.2 `of_mcp`

以该仓实时规则为准。最低证据必须包含 formatter/lint、typecheck、unit、auth negative tests、
integration；FastMCP/MCP 升级任务还要跑协议版本、legacy client 和 sessionless/stateless 组合测试。

### 6.3 F2/F3/F4/F6/F7/F8 跨仓矩阵

跨仓版本任务不能只在某一 checkout 绿。至少保留以下可复现证据：

| 运行面 | 当前基线 | 必测 |
|---|---|---|
| MultiRAG outbound Host/Client -> `of_mcp` | 两侧主运行时均为 MCP SDK 2/FastMCP 4；`of_mcp` F4 commit `23dd1fd` | modern 优先与 legacy 协商、list/call、401/403、tool error、caller cancel/timeout、本地调用无 HOL |
| 外部 Client -> MultiRAG inbound Server | MultiRAG MCP SDK 2/FastMCP 4 modern Server，同时保留 legacy 协议面 | `2026-07-28` discover/header/sessionless/structured result，以及真实旧 Client 回退 |
| 历史兼容方向 | PEP 723 + 独立 lock 启动的真实 FastMCP 3 server/client 子进程 | 不能与根解释器混装；必须观测实际协商分支、终止并确认无残留进程 |

- server 以随机本机端口启动，输出结构化 ready/protocol 证据；测试负责终止并检查退出，不遗留进程。
- legacy fixture 必须能从自己的 PEP 723 lock 在干净 cache/临时环境冷安装；不能依赖父进程
  `sys.path`、绝对 sibling checkout，或把 FastMCP 3/MCP 1 混入根运行时。
- 当前 `make mcp-compat` 必须保持 **13/13 PASS**。完成日志同时记录 MultiRAG SHA、of_mcp SHA、
  主/legacy lock 摘要和每格协商分支；新增或减少格子必须说明协议风险，不能只改分母换绿。
- timeout/caller cancel 必须证明本地等待有界、同 server 快调用不被慢调用阻塞；远端取消是协作式，
  远端仍完成不等于矩阵失败，但必须被观测，副作用路径还要另证业务幂等与结果未知处置。
- 两仓分别执行各自 `AGENTS.md` 的完整 verify；matrix 通过不能替代任一仓本地门禁，skip 也不能
  记作通过。

### 6.4 手工/线上验证

只有自动化覆盖不到长连接、管理后台授权或真实卡片时才做。记录：测试企业、脱敏 binding、时间、
预期、实际、日志查询、回滚点。不得用生产患者数据。重启/换钥/发布飞书应用属于外部状态变更，
必须先获批准。

## 7. 完工记账

任务真正完成后：

1. ROADMAP 行改 `✅`；
2. 追加变更日志：日期、ID、仓库/SHA、改动、精确测试、部署状态、剩余风险；
3. Channel 任务同步更新 `docs/channel-program/PROGRESS.md` 与必要的 `CONTRACT.md`；
4. 如果版本、上游 HEAD 或官方语义变化，更新 VERSION_BASELINE/REFERENCES；
5. 如果改变长期决策，新增 ADR，不覆盖旧 ADR 的历史；
6. 提交标题尾部带 ID，例如：

```text
feat(identity): resolve verified Feishu users (EIM-I6)
feat(channel): emit structured external identity (EIM-C2, CHN-X6)
feat(auth): verify delegated MCP access tokens (EIM-A2)
```

完成汇报模板：

```text
结果：EIM-XX 已完成/阻塞。
实现：列出关键契约和文件，不复述所有步骤。
验证：命令 + 精确测试结果；未运行项明确说未运行。
迁移/部署：当前处于哪个兼容半步、下一步和安全顺序。
记录：ROADMAP/CHN 账本已更新；提交 SHA。
```

## 8. 必须停止并请用户决策的情况

- 需要在飞书管理后台申请权限、发布应用、扩大通讯录范围；
- 需要重启线上 API/supervisor、轮换 secret/签名密钥、执行生产迁移；
- employee_no/talent_id 存在一对多或企业没有权威 HR 主键；
- 需要从 JIT 改为全量组织镜像，或要把身份服务拆成独立项目；
- 必须变更一个企业对应一个 tenant 的默认模型；
- 上游最新版是 prerelease，且升级会改变生产协议或公共 API；
- resolver 需要本轮已批准范围之外的新 prerelease/major，或不能确定性复现当前 MCP SDK 2/
  FastMCP 4 主 lock 与 PEP 723 FastMCP 3 legacy fixture lock；
- 依赖方案只有本机 editable/sibling path、已有 cache 或未提交 lock 能工作，干净环境不能复现；
- MCP 兼容矩阵无法证明实际协商分支、本地 timeout/cancel 等待无界、仍出现 HOL，或不能安全终止
  fixture 进程；远端因协作式取消而完成调用本身不是此处的“悬挂”判据；
- 官方发布说明、extension 仓和 SDK 对 Tasks/Apps/EMA 的成熟度不一致，而任务又要把它当生产硬依赖；
- 发现依赖任务未完成、现有实现与 ADR 冲突、无法构造安全兼容半态；
- 需要复制第三方代码但许可证或归属不清楚。

停止时提供已核实事实、受影响任务、可选方案和推荐项；不要用猜测填补企业治理决策。

## 9. 禁止事项

- 不把 `open_id` 直接写入 `Principal.id`；
- 不以虚构邮箱创建飞书用户；
- 不把全员入租户等同于全员可读全部知识库；
- 不每日全量复制整棵飞书组织树作为默认架构；
- 不从 prompt/工具参数接受调用者工号或 scope；
- 不使用静态 `X-User-Id` 之类 header 模拟生产身份；
- 不关闭 `extra="forbid"`、删除测试、放宽 lint/mypy/import-linter 换绿；
- 不在同一请求混用 sync/async session；
- 不在未经批准时修改外部后台、生产数据、线上进程或密钥；
- 不把“代码合并”写成“已部署”，也不把“测试被 skip”写成“验证通过”。
