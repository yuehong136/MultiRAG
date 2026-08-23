# 企业身份与 MCP · 零上下文 Agent 执行手册

> 目标：一个没有任何历史对话的 Agent，只凭本目录就能安全接手一个独立任务。
> 本文规定工作方式；技术事实以 [CONTRACTS](CONTRACTS.md)、[DECISIONS](DECISIONS.md) 和任务行本身为准。
> 跨程序共通的四条不变量（说 ID 不说需求 / 提示词只补哪三样 / 验证基线要自测 /
> agent 记忆不跨机器）在 [`AGENTS.md` 的「零上下文交接」](../../AGENTS.md#零上下文交接)；
> 本文是 EIM 特有的加码（跨两个仓、外部管理后台操作要批准），不重复那四条。

## 1. 接单格式

用户应尽量按 ID 派工：

```text
读 docs/enterprise-identity-mcp/README.md 和 AGENT_RUNBOOK.md，执行用户指定的 EIM-<ID>。
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
| EIM-L | [CONTRACTS](CONTRACTS.md)、[TESTING_SECURITY](TESTING_SECURITY.md)、`of_mcp` 的 `AGENTS.md`/README/service contract；涉及 CardKit 时再读 Channel `PROGRESS.md`/`CONTRACT.md` |
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

P1 已接续 I3 完成；具体边界见下节。`F1/I4.1` 已完成，精确边界见本章后续小节。
`I6` 已完成 User/UserTenant/link/event 的完整事务，C3 才把 Channel assertion
组合成 Principal。FastMCP 4 的 auth 与 tool list/call 能力仍在 MCP composition/adapter 层复用，
不进入上述 identity domain。

#### EIM-P1 完成边界与后续接手

P1 已完成，代码锚点为 `api/identity/principal.py`、`api/identity/legacy_owner.py` 与
`api/utils/api_utils.py::async_current_user`。后续任务必须保留：

1. `api.identity.principal.Principal` 是唯一 canonical class；`api.utils.api_utils.Principal` 只是
   同一 class object 的临时 re-export，存量 route import 迁完即删；
2. Principal direct constructor 封闭，两个 evidence builder 只是进程内 trusted adapter seam，
   不得直接消费 wire/Channel 任意 DTO；
3. I3 builder 只提升无 error/provision action/provisioning policy revision/reverification 的
   `RESOLVED`，同时要求 active identity、
   live 同 Tenant membership、合法 `owner/admin/normal` role 和一致 provider/internal identity/proof time；
4. `validated_at`、真实 `authenticated_at` 与 `assurance_verified_at` 分离；当前 Web/API
   `authenticated_at=None`，directory 与 enterprise proof 分别绑 identity/subject 的原始验证时间；
5. Principal 不包含 email/role/groups/scopes/token，PII 默认不进 repr；`.id/.nickname` 只是兼容
   property，不得成为新代码的领域命名；
6. legacy Web/API adapter 每请求活查 active User + 唯一 personal OWNER Tenant，不是多
   Tenant selector；valid JWT 用户/成员失效不 fallback，意外 verifier/runtime error 不吞；
7. 存量 sync `Depends(manager)` 仍可能返回 ORM User，`async_current_tenant_id` broad fallback
   也不属于 P1；不得宣称所有 auth 入口已统一；
8. 完成证据为定向 unit **49 passed**、真 PostgreSQL **1 passed**、`make verify` unit
   **2005 passed** 且 7 import contracts/async gate/mypy 73 files 全绿，强制 integration **83 passed**，
   安全复核无 blocker。

P1 当时不交付 C3/P2、A2/P3 或 A7。C3、P2 已在后续独立任务完成源码、自动门禁和本机飞书
live；A7 的代码前置虽已满足，仍须作为独立 inbound Resource Server 实现/发布。当前 token 主线是
P3；不得重做 C3/P2/A2，也不得把 A2 的独立 signer 当成 bearer 已接线。
I5 已在后续独立任务完成且默认关闭；I7/CHN-X21 也已完成本地 managed event consumer、未 rollout，
I8 的 I6 + I7 依赖现已闭合，默认关闭的 durable reconciliation slice 也已作为
CHN-X22 本地代码落地；但风险感知的 local fast path、公开管理员面与 rollout 仍 deferred，
所以 EIM-I8 继续保持 `🔵`，不得把局部落地写成完整 freshness 闭环。

#### EIM-P2 / CHN-X18 已完成边界

P2 开工前先复核下列当前事实；发现符号漂移先更新锚点，不要按历史行号机械修改：

1. C3 已把完整、immutable `Principal` 放入 `TrustedChannelContext.principal`，并让
   `principal_id == principal.platform_user_id`；execution service 进入 target 前仍保留完整 context；
2. 第一处真实断点在 `api/channel_execution/executors.py` 与 driver Protocol：Canvas/Dialog executor
   只把 `principal_id` 字符串传下去，Canvas completion、Dialog history/`async_chat` 仍有
   `principal_id or ""`。LINKED 不得再静默匿名；NO_LINK 只能作为服务端明确判定的 legacy 分支；
3. Canvas Graph 在 `Canvas.run()` 前就 `load()` 组件并预热 MCP session。Principal/run context 必须在
   Graph/component 构造前显式注入，不能晚到 `run()`，也不能依赖 module-global 或隐式 ContextVar；
4. Principal 不得序列化到 worker/private API、DSL、`sys.*` globals、prompt、custom header、SSE、日志
   或 MCP arguments。模型和 DSL 都不能覆盖服务端 tenant/platform user；
5. Memory save/query 必须同时绑定 tenant 与 platform user；同租户两个 Principal 的会话、Memory、
   retry/regenerate/cancel 均不得串线。不要顺手重构公开 Memory CRUD 或实现完整数据 PDP；
6. MCP 本轮只增加 request/run context seam，让每次工具调用知道当前 immutable Principal；不签 token、
   不取 credential、不添加 Authorization/bearer、不改 static headers，后者严格属于 A2/P3；
7. 本任务中的 Workflow 指 Channel 当前实际运行的 Canvas Graph/component workflow，不含独立
   `workflow/`、`workflow_v2/` 或 `api.run_platform` durable-run 主线；
8. 本任务会改 `api/channel_execution/`，因此与 Channel 账本映射为 CHN-X18；开工、提交标题、
   `ROADMAP`、Channel `PROGRESS/CONTRACT` 和变更日志都必须同时带 EIM-P2 与 CHN-X18。

最低验收不是“删掉三个空字符串”，而是证明同一个可信 Principal 从 service 到 target driver、
Dialog/Canvas Graph、Agent/RAG/Memory/workflow/MCP context seam 保持一致；LINKED fail closed、NO_LINK
兼容不回归；同租户双用户与并发 run 不串 session/Memory/context；repr、日志和 wire 无主体原值。

P2 实现提交为 `549cc9c6`，本机 API 后续运行于包含该提交的 `4165d439`。Canvas、Dialog 各完成一条
真实飞书新对话，均形成非空 owner 并进入 completed；`make smoke` 六组件全绿，两个 runtime connected，
runtime/API error 为 0。Canvas 当次装配 Agent/Retrieval/Message 与 MCP 配置但没有 `memory_ids`，所以该
live 只证明 Principal/owner/context 装配，不替代完整 Channel UX、真实 Memory 写读或 MCP 工具调用证据。

#### EIM-A2 已完成边界与 P3 交接

A2 的代码锚点是 `api/identity/mcp_issuer/{contracts,keys,service,runtime}.py`、
`api/apps/well_known.py` 与 `common/app_config.py::McpIssuerConfig`。后续必须保留：

1. 唯一公共面是 unauthenticated `GET /.well-known/jwks.json`；disabled/unready 返回 safe 503，A2
   没有公开 token endpoint、OAuth grant、refresh token 或 third-party client registration；
2. `identity.mcp_issuer` 默认 disabled。启用时 canonical HTTPS issuer、first-party client、resource→
   exact audience/scope/可选 enterprise authority 与 key provider 缺一即 fail-fast；disabled 不读 key；
3. production signer 只用 `cryptography`，PyJWT 只作测试 oracle。`SigningKeyProvider` 只暴露 active
   `kid`、public snapshot 与 ES256 operation；file provider 要求 absolute、非 symlink、regular、当前
   进程 owner 的 mode 0400/0600 private PEM，并校验 active P-256 private/public 完全匹配；
4. `sub/tenant_id` 只来自 immutable Principal；resource/audience 与 registered scopes 只来自 registry，
   allowed scopes 由服务端 grant 提供。A2 执行 A1 七条 issuance policy，但没有虚构 P3 所需的 Agent/
   tenant/user policy source；
5. token 固定 `typ=at+jwt`、ES256、active `kid`、`mcp_access`、单值 audience、`nbf=iat`、至多 300 秒、
   不可预测 JTI 和 4096-byte 上限。`auth_time` 只投影真实 NumericDate；`acr` 只投影
   `ENTERPRISE_VERIFIED`；首期不猜 `amr`，enterprise subject 要精确匹配 resource authority；
6. key 轮换是 prepublish → switch → retain/remove contract；旧 public key 默认至少保留
   `300 + 30 + 300 = 630` 秒。真实多副本发布、key generation/KMS、DNS/TLS 与演练仍属 O1；
7. A2 当时未暗含 P3、A5、A7、A8；后续 P3 已作为独立任务完成本地代码与自动门禁。2026-08-24
   曾经用户批准，以单机临时 key/policy/grant、loopback TLS 和严格 verifier 加载 P3 并完成一次真实
   Channel/MCP bearer + U15 live；源码默认关闭，不能把该本机证据写成生产 key/rollout 已完成。

A2 失败基线为新增模块收集 **2 errors**；A1+A2 定向 **130 passed**，定向 mypy **8 source files**。
另用临时 P-256 key 动态签发且不输出 token/key，由 of_mcp production
`StrictMcpAccessVerifier` + `parse_jwks_document` 成功验收。`make fix` **1275 files unchanged**；
`make verify` 的 Ruff、8 条 import contracts（848 files/2679 dependencies）、async DB gate、mypy
**95 source files** 与 unit **2450 passed** 全绿。只读 `make smoke` 对仍运行旧代码的 API 显示 ping/
healthz HTTP 200、六组件 `ok`；这只是运行基线，不是 A2 rollout 或 JWKS live 证据。

#### EIM-P3 已完成边界与后续 rollout 交接

P3 的代码锚点是 `api/identity/mcp_delegation/`、`RunContext.agent_id/agent_revision_id`、
`common.mcp_tool_call_conn.MCPToolBinding/MCPRequestCredentialProvider`、Agent MCP 装配和 API lifespan。
后续必须保留：

1. authority 只来自 A4 `secure` format-2 snapshot、MultiRAG format-1 grant snapshot、C3/P2 Principal 与
   服务端已验证 Canvas release；不从模型/DSL/description/static headers/runtime tool list 猜授权；
2. 两个 snapshot absolute、regular、non-symlink、有界且 revision 可复算；POSIX group/world writable、
   policy drift、重复 server/resource/audience、未知 scope 均 fail fast；hash 是 drift check，不冒充制品签名；
3. binding 精确核对 server tenant、Streamable HTTP、URL、A2 resource audience 与 grant audience；
   delegated SSE、静态 Authorization、缺 Principal/revision、Dialog/non-Channel context 均 fail closed，
   不回退 legacy。未登记 server 才保持原 static mode；
4. grant 绑定 tenant/platform user/published agent+revision/resource。model alias 与 canonical wire name
   分离；未授权 tool 不进入模型 metadata，执行仍用 canonical name 二次判定；
5. 只缓存有界 grant/scope decision，key 含 principal/tenant/agent+revision/server/resource/canonical tool/
   scopes/policy+grant revision/credential generation；ACR/AMR/enterprise subject 每次重新验证。A2 当前
   不签 AMR，所以非空 `required_amr` 在发网前拒绝；
6. 每次逻辑调用新签 token/JTI；SDK 2 operation-scoped `httpx2.Auth` 只在该次 initialize/call/retry
   复用 bearer，完成即关闭。Agent/session/global/static headers/repr/error/meta 不保存或泄露 token；
7. 配置默认 disabled。当前没有真实 key、secure snapshot、grant、重启、部署、remote release 或真实
   MCP bearer E2E；Dialog 没有 published revision，只有 Channel Canvas 具备现有动态委托上下文。

P3 失败优先收集到新增模块 `ModuleNotFoundError`；定向 **109 passed**，扩展 Agent/MCP/A2 回归
**178 passed**，Channel 执行回归 **88 passed**，`make mcp-compat` **13/13 PASS**。`make fix` 全绿；
`make verify` 为 8 条 import contracts（853 files/2695 dependencies）、mypy 100 files、unit
**2470 passed**；只读 `make smoke` 六组件全绿，但命中的是未重启的旧 runtime，不是 P3 rollout。
未跑 integration（无 DB/存储/检索变更）。A5 后续已在 of_mcp 完成本地实现，U14/U15 也已完成；
2026-08-24 的用户批准只覆盖本机临时 live。任何生产 artifact/key/config/restart/secure endpoint、
多实例部署或对外 MCP 流量仍须另行精确批准。

#### EIM-U15 / CHN-X15 当前实现与 rollout 交接

U15 当前为 `✅`：源码、完整门禁、integration、账本与 `41675183` 已完成；2026-08-24 又在用户批准
的单机临时环境完成真实飞书/P3/of_mcp E2E。源码默认值仍关闭，该 live 不等于生产部署。接手时按
以下顺序读当前锚点：

1. U14/P0：`common/mcp_interactions.py`、`common/mcp_tool_call_conn.py` 与
   `api/identity/mcp_interactions/{contracts,repository,runtime,service}.py`。恢复必须携
   `job_id + owner + attempt`，lease/renew/complete/retry 均要求未过期；每轮活查真实
   `identity_revision`，connector TTL 取服务端配置，tool gate 只允许 `read/prepare`，不得靠 renderer
   绕过；
2. API 持久化与 private contract：`api/channel_execution/{interaction_forms,
   interaction_presentations,interaction_worker}.py`、
   `api/apps/restful_apis/channel_interaction_api.py` 和 migration
   `d8f0a2b4c6e8_add_mcp_interaction_presentations.py`；
3. worker/provider：`api/channels/{interaction_models,runtime_client,binding_bridge,worker}.py` 与
   `api/channels/feishu/{channel,interaction_renderer,interaction_presenter,reply}.py`；
4. 线格与状态：`docs/channel-program/CONTRACT.md#13-私有-interaction-delivery--callbackeim-u15--chn-x15`、
   `FEISHU_BOT_UX.md#82-mcp-结构化表单与-h5url-elicitation`，再核对两份账本仍为同一状态。

必须保留的边界：

- 原生 mapper 只支持服务端 allowlist 的 text、integer/number、boolean、date、enum 与 enum array；
  H5/URL、人员/附件、多步骤和 credential 输入 deferred；
- execution pause 只在 MultiRAG 自有 composition/service 层登记 presentation，不改 RAGFlow
  `core/llm`、Canvas/Agent 主循环或 `of_mcp` 生产 runtime/contract，也不启用 U7 敏感写；2026-08-24
  live 只给 `of_mcp` testing compat server 增加 opt-in 严格 P3 fixture；
- worker 只能 claim 安全 projection、更新原卡并 ACK delivery；form callback 只有在 PostgreSQL
  receipt 提交后才快速应答，不能同步解析 Principal 或调用 MCP；
- 当前 managed transport 是 app-bound WebSocket。adapter 校验 tenant/app/operator/conversation/
  message/event lineage；不要把官方 SDK 的 webhook request-signature 规则写成当前已实现；
- API background processor 必须重新读取 binding/generation/enabled/provider、解析 verified Principal，
  再让 U14 对当前 revision 做 CAS/重授权/恢复；duplicate、lease loss、identity TTL、tool gate 与未知
  outcome 都 fail closed；
- terminal card 默认只显示固定安全状态；只有 EIM-L2/CHN-X20 精确定义的 direct/FastMCP-wrapper
  四键 envelope 才可投影最多 240 字符的单行 trimmed printable message，其他结果仍用 generic；字段
  错误重新投递 form，renderer failure 只重试 delivery，绝不重跑工具。

安全部署顺序固定为：先执行 migration，在 `identity.mcp_interactions.enabled=false` 下部署并重启新
API；再重启所有 supervisor/child consumer，并确认新 generation 能消费 `interaction_required`、
claim/ACK 和 durable callback；最后才启用 producer 并重启 API。回滚先关闭 producer 并重启 API，
停止产生新 interaction；保留新 consumer 处理/终态化已持久记录，再考虑退代码。新增表有数据时不得
destructive downgrade。真实迁移、重启、配置 key 和飞书 live 都属于外部状态操作，仍需单独批准。
2026-08-24 已批准并执行的仅是当次单机临时 rollout；后续重新启用、生产 key、私网 TLS、多实例或
正式飞书流量仍按本条重新取得批准。

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

#### EIM-F1 完成边界与 I4 交接

F1 已以 `pyproject.toml`/`uv.lock` 中的 `lark-oapi` 1.7.2 和
`tests/unit/test_lark_oapi_contract.py` 完成。接手者必须保持以下精确边界：

1. F1 只做官方 SDK patch 升级与可执行契约，不新增 Provider、token wrapper、cache、数据库、
   Principal、Channel DTO 或 transport 迁移；
2. Contact V3 fixture 固定 `user_id_type=open_id` request 与
   `GetUserResponse/GetUserResponseBody/User/UserStatus` typed response，只使用明显虚假的测试标识；
3. 平台 `api.channel_control`、`api.channel_providers`、`api.channels.verification`、`api.identity`
   import 不得加载 SDK，也不得安装进程 event loop；
4. 显式顶层导入 1.7.2 SDK 会安装其模块级 event loop，这是已知边界。测试要求它 idle、未运行、
   无 task、无新 thread，且构造 Client 不启动后台工作；禁止把结果误写成“SDK 无 import 副作用”；
5. 1.7.2 `TokenManager` 有进程内 cache，但 cache miss 不带 single-flight。F1 不重写官方 token 获取；
   I4 在 Provider Account scope 上实现项目级并发折叠，并覆盖跨 account 隔离、失败恢复和过期刷新；
6. F1 完成证据为新 contract **7 passed**、广义 Feishu/Channel 定向 **101 passed**；
   `uv lock --check` 通过；`make verify` 的 Ruff 1222 files、7 import contracts、async DB gate、
   mypy 73 source files 全绿，unit **2012 passed in 30.95s**；`git diff --check` 通过。

F1 的 `✅` 只代表 I4 的 SDK/fixture 前置已满足。I4/I4.1 已完成 Contact 调用、字段白名单、
token/cache/single-flight、live Auth response 与阶段化错误码；F1 fixture 不能替代最终 production
adapter sandbox。

#### EIM-I4.1 完成边界与 I6 交接

I4.1 已完成并交接 I6。接手者必须保留以下边界：

1. `api.identity.providers` 是框架无关边界；导出 `FeishuEnterpriseIdentityProvider`
   的 `resolve(context, assertion)`、`refresh(context, provider_user_id)` 与受信事件使用的
   `invalidate(context)`。它不 import Channel、SQLAlchemy、FastMCP 或 Principal。
2. `LarkOapiFeishuDirectoryClient` 只在首次调用时 import `lark_oapi==1.7.2`；按 Auth V3 generated
   async request/resource/transport + strict live top-level adapter -> Tenant V2/Contact V3 generated
   typed nested response 顺序执行。
   Tenant/Contact 显式携 project token，不走 SDK 同步 `TokenManager` cold path；没有自建
   HTTP/签名客户端。1.7.2 生成 Auth response 期待 `data`，live 成功响应的 token/expire 位于
   顶层；I4.1 已在不重写 transport/签名的前提下严格兼容真实 wire shape。
3. `api.identity_adapters.channel_credentials.ChannelProviderCredentialResolver` 是唯一知道
   I2.1 过渡 credential 形状的 composition seam。它用一条 account-rooted SQL 要求精确一条
   account/link/channel/secret，再用注入的 `SecretStore` 解密；不会 cache、commit、猜测
   `app_id` 或从 Channel JSON 接受明文 Secret。它返回的 `credential_generation`
   就是 `ChannelSecret.version`。
4. token/directory cache key 包含 account id/revision/scope marker/credential generation/domain；同 key
   single-flight，跨 account/generation/domain 隔离，等待者取消不会取消 shared producer，失败不进
   cache 并释放 slot。cache 与 in-flight 均有硬上限；identity 正/负 TTL 为 300/30 秒，
   token 预留 600 秒 safety window，Contact 每 account 15 calls/s、排队最多 2 秒。
5. Provider 响应只保留 `user_id/open_id/union_id/employee_no/display_name/status`；可选空字符串
   归一为 `None`，status 的五个字段都必须是真 `bool`。完整 SDK model/raw body、
   token、Secret、tenant/user/account 标识和个人信息不得进入 repr/log/error/snapshot。
6. 三个 SDK endpoint 都保留 HTTP status + business code envelope；非 2xx + code 0 仍是失败。
   业务码按 endpoint stage 分类，`10003` 不是 credential code；Auth credential mismatch 当前为
   `10015/20002`。Auth/Tenant 控制面失败永不产生目录 identity `NOT_FOUND/NOT_IN_SCOPE`；Contact
   只有 code 0 可用 HTTP 403/404 fallback，未知/瞬时非零 code 不得降级 JIT。
7. 旧 stdin-collision sandbox 因实际使用空参数而作废。有效直连与修正后 production adapter
   sandbox 均为 Auth/Tenant/Contact 三步 HTTP 200/code 0、tenant match、user active；adapter 另
   确认 token/expiry、asserted open_id、stable user id 和五项 status，且无 Secret/token/标识/PII 落盘。
8. I4.1 完成证据：广义 I4+F1 **118 passed in 5.29s**；Channel credential 真 PostgreSQL
   **1 passed in 0.76s**；`make verify` 全绿（Ruff format 1234 files、Ruff check、7 import
   contracts、async gate、mypy 78 files、unit **2123 passed in 34.53s**）；强制 integration
   **84 passed in 12.52s**。
9. I4 不持久化 Provider tenant/account ownership、ExternalIdentity/Alias 或 User/UserTenant，
   不实现 I5/I6/I7/C3/Principal/MCP token。它只产生 verified Provider result；I6 现已在独立用例
   事务层消费该 proof，Principal 组装仍须由 C3/P1 接续。

#### EIM-I5 / CHN-X19 完成边界与业务 resolver 交接

I5 的代码锚点为 `api/identity/enterprise_subjects/{contracts,feishu_employee_number,repository,service}.py`、
`api/identity_adapters/channel_runtime.py` 与 `api/channel_execution/dependencies.py`。接手者必须保留：

1. 功能由 `identity.enterprise_subject_resolution.enabled=false` 默认关闭，resolver 名称只能来自
   server-owned canonical registry；关闭时不构造 repository/resolver、不写表，原 C3
   `DIRECTORY_VERIFIED` 行为逐项兼容；
2. 通用 authority 显式声明 provider、subject type、issuer、issuer-tenant 来源与 proof 来源。
   `FeishuEmployeeNumberResolver` 只证明本次 I4 proof 中逐字的 `employee_no`，issuer 固定
   `feishu_contact`、issuer tenant 固定取 Provider tenant、proof time 固定取 I4 `verified_at`；不 trim、
   补零、改大小写或推断 OA `workcode/talent_id`；非 canonical value 返回 normal `UNAVAILABLE`、零写；
3. resolver 外调在数据库事务外。五态只有 `RESOLVED` 可携 subject；`UNAVAILABLE` 不带 proof 且
   零写。malformed authority/result、resolver exception、repository exception/readback mismatch 是
   fatal fail closed，不得伪装成 `NOT_FOUND`；
4. repository 每次使用 fresh `AsyncSession`/短事务；同槽同值幂等且 proof/revision 只前进，同槽换值
   或跨 user/slot subject 占用隔离为 conflict，negative result 只单向收紧，陈旧结果不倒退或复活。
   `RESOLVED` evidence 必须从数据库回读重建，不能从 resolver DTO 直通；
5. 并发碰撞可能让第一 claimant 在第二 claimant 揭示冲突前短暂返回；冲突事务后 subsequent readback
   不再返回 active subject。I5 没有 retroactive token revocation，禁止把收敛测试写成已撤回此前结果；
6. Channel 只在本次 I4 + I6 + final I3 成功后尝试 I5。`NOT_FOUND/UNAVAILABLE` 保留 ordinary RAG
   directory-only，subject-required 工具仍拒绝；`AMBIGUOUS/INACTIVE` 拒绝本次 linked execution；
7. I5 没有新 migration、公开 API/UI、真实 OA/HR adapter、leave wrapper、I7/I8 event/freshness/
   reconciliation、生产 enable/rollout 或真实企业 subject live。U14 对 active subject link 的旧读取
   不因此获得 freshness 保证；
8. 完成时必须跑 resolver/service/Channel 定向 unit、subject repository 真 PostgreSQL、`make verify`、
   `REQUIRE_SERVICES=1 make integration`，并在 EIM 与 CHN 双账本回填精确数字；本节不预填本轮尚未
   完成的验证结果。

#### EIM-L1 代码边界与最终门禁交接（`✅`，默认关闭）

L1 仓库为 of_mcp，依赖 A5 + I5。代码、snapshot 与最终 verify 已完成。冷启动先读 of_mcp 根
`AGENTS.md`、`README.md`，再读 `packages/ofmcp-core` 的 service/tool-policy binding、Gateway
settings/assembly、`services/leave/service.toml`、identity、五个工具、operations 与 Ecology adapter；
不得把 local test default、A4 通用 seam 或 I5 的 Feishu resolver 当成真实 OA authority。

实施边界固定如下：

1. composition root 使用唯一、server-owned 命名 binding `leave_applicant`。service 声明把
   `subject_type=ecology_userid` 固定为不可变语义锚点；secure override 只能提供/替换权威
   issuer/tenant，不能覆盖 type。把 type 改成 `employee_no`、binding 缺失/未知/空白、与 tool policy
   漂移或 authority 不匹配时，在发布工具目录前 fail-fast。local default 只供 auth-disabled contract
   tests，不产生 Principal 或可信证明；
2. `get_leave_balance` 是 read/reusable，`preview_leave` 是 prepare/reusable，
   `verify_leave_request` 是 read/reusable；三者都引用 exact `leave_applicant` 并使用独立
   `current_verified_read_subject` dependency。`list_leave_types` 无主体数据，保持 read/reusable；
3. 所有工具保留 optional `oa_user_id` 仅为 schema 兼容，但 local/secure、mount/proxy、direct test
   等所有模式始终忽略它。三个读/预览工具只消费当前 request-scoped Principal；缺 subject 或
   type/issuer/tenant 不符必须在 OA 调用前稳定拒绝；dependency 还要独立重验 service-owned
   `ecology_userid`，即使 policy + Principal 同时漂移成 `employee_no` 也必须 OA 零调用；
4. `verify_leave_request` 的同一个 OA 响应必须同时证明 owner 等于 verified subject、workflow id 和
   form id 等于服务配置；任一缺失/不符都走同一安全错误，不泄露存在性。读结果还必须最小化：不回显
   subject/raw/form/contact，兼容字段使用 `current`/`redacted`，vacation 仅有界 allowlist；
5. read dependency 已与写工具的 compatibility seam 分离。`create_leave_draft`、`submit_leave` 现已
   无条件稳定返回 `LEAVE_WRITE_DISABLED`；任何 Principal、scope 或参数都不能解除，测试必须证明
   连 Ecology client 都不构造、OA 请求数为零；
6. `employee_no@feishu_contact` 不等于 Ecology userid/workcode。若权威 OA 标识无法表达为
   `ecology_userid`，另立 identity/schema/resolver 任务；L1 不 trim、改名、映射、覆盖 service type
   或猜测；
7. focused core + gateway + proxy + leave **146 passed、2 skipped**；contract diff 为
   **breaking 0 / behavioral 7 / additive 0**（service + gateway）；已审查 snapshot 与当前生成结果
   一致，`uv run --locked ofmcp verify` 六步全绿（test **556 passed、2 skipped**）；
8. 真实 OA/HR resolver、leave 写入、Confirmation、业务幂等/unknown-outcome、L2、U7、I7/I8、
   配置 Secret、重启、部署、远程发布和生产 rollout 均不属于 L1。M1 始终只管 medic。

#### EIM-L2 / CHN-X20 原生低敏请假试算交接（`✅`，未 rollout）

L2 是 L1 + U15 的窄组合，不是 leave 写入阶段。当前代码锚点为 of_mcp
`services/leave/src/ofmcp/services/leave/{interaction.py,tools/preview_leave_form.py}`、
`services/leave/service.toml`、`deploy/profiles/secure.toml` 与 core requestState helper；MultiRAG 只在
`api/channel_execution/interaction_presentations.py` 增加严格 terminal 投影。不得改 RAGFlow 主循环、
升级依赖、扩展 H5/URL，或借此解除写工具围栏。

接手者必须逐项保留：

1. `preview_leave_form` 的外层业务参数 schema 为空，身份只来自 `current_verified_read_subject`；
   policy 固定 `leave:read`、prepare/reusable、`leave_applicant`。任何用户可控 `oa_user_id`、Principal、
   tenant、scope、PM/CC/人员字段都不能进入工具 schema 或授权输入；
2. response schema 必须 flat/ref-free 且恰好 7 字段：`leave_type` 仅事假/年假/星光假/阳光假，起止各
   date + `0..23` hour + `00|30` minute。reason、身份、项目人员/项目经理、CC、remark、附件、H5/URL
   一律 deferred；当前只允许 p2p，不得从一次私聊 live 外推群聊；
3. 首次 input-required、decline、cancel、expire、换人/跨 tenant、身份 TTL、tool gate、revision 冲突均
   OA 零调用。accepted 仍须让 U14 重验 Principal/scope/policy/lease，并只对当前 `leave_applicant` 执行
   OA preview；固定 `person_type=non_project`、服务端低敏 reason，且 `doCreateRequest` 调用数为 0；不得调用
   draft/submit。`create_leave_draft`/`submit_leave` 继续 `LEAVE_WRITE_DISABLED`；
4. terminal 只接受 direct envelope 或 top-level **仅含** `result` 的 FastMCP wrapper；内层精确四键
   `kind/version/message/preview_only`，kind 固定 `com.ofmcp/interaction-terminal`、version 是 int 1、
   preview_only 是 bool true，message 非空、单行、trimmed、printable、最多 240 字符。任何漂移显示
   `处理已完成。`，不得把 raw result、异常或 OA payload交给卡片；
5. `secure` profile 显式 `interactions_enabled=true`，缺稳定 `OFMCP_REQUEST_STATE_KEY` 必须 fail-fast；
   key ring active-first，requestState TTL 900 秒严格大于 MR 600 秒，旧 key 至少保留覆盖最大 TTL。
   这只完成 state 安全接线；真实 OA authority、A6 production backend、remote-release 与 rollout 仍未配置；
6. 当前 `end > start` 在资源端 cross-field validator 中执行。无效窗口恢复后会 terminal failed，而不是
   Host 的 fresh-nonce form 重投；测试与文档必须诚实固定这个限制，后续改善要另立任务；
7. 两仓最终门禁已经回填：of_mcp focused **114 passed**、verify **580 passed / 2 skipped**、contract
   diff **breaking 0 / behavioral 0 / additive 1**；MultiRAG focused **89 passed**、`make verify`
   **2762 passed**、强制 integration **182 passed**、MCP compatibility **22/22**。这足以标代码/契约
   `✅`，但真实 authority、部署与生产 rollout 仍须另行批准。

#### EIM-I8 / CHN-X22 durable reconciliation slice 交接（CHN 本地代码 `✅`；EIM `🔵`）

I8 当前只完成默认关闭的 durable reconciliation slice，不是完整 freshness 产品。代码锚点为
`api/identity/reconciliation/`、migration `e1f3a5c7b9d0`、identity reconciliation lifespan 与共享
identity provider runtime。接手者必须保留：

1. 候选集只来自本地已链接/alias、active identity、active User、合法 active UserTenant，且在近期活跃
   窗口内的 subject；禁止 provider 全员枚举，也不得用 reconciliation 刷新 `last_seen_at`；
2. 每 account 持久化 checkpoint/target；同 account checkpoint 与 target 全部加锁后才读取 DB clock，
   以 keyset/target 推进替代进程内游标；`NOT_FOUND` 首次/末次/确认窗口也只能用锁后数据库时钟；
   provider I/O 必须在数据库事务外；
3. reconciliation 使用共享 provider registry，但目录读取是独立 uncached 路径并受每 account **1/s**
   限速；credential/KMS/token/tenant/limiter/Contact 全链预算不超过
   `min(lease remaining, configured lease)-safety margin`（默认 `60-5=55s`），timeout 只作
   `UNAVAILABLE` 退避；不得复用交互请求的 TTL cache，也不得制造第二套 provider cache；
4. `NOT_FOUND` 需要跨确认窗口连续两次才收紧 canonical；`NOT_IN_SCOPE` 不计入 canonical 收紧；
   pending 到期必须重验 active window，过期则 Provider 零调用；`UNAVAILABLE` 只退避，不得把暂态
   失败写成身份撤销；
5. 只有 clean full-cycle 才能把 account health 恢复 healthy；连续收紧达到阈值会打开 circuit breaker，
   防止目录异常造成批量破坏；
6. 当前只有 repository/admin snapshot，**没有 public admin route**；snapshot 的 repr-hidden stable
   opaque `account_ref` 是 domain-separated SHA-256 对 tenant ID + 随机 provider account ID 取
   128-bit 截断的诊断引用，不是 authority，并且不返回 raw checkpoint/account ID、provider
   tenant/natural key 或 employee ID。HTTP disabled 路径不应构造 DB/provider，且没有重启、真实目录
   live、配置变更或 rollout；
7. Tenant 软禁用必须在 claim 与 apply 两段都按锁后状态 fail closed：清理已有 cycle/lease、保持
   canonical identity 与 account health 不扩权，不能让 in-flight Provider observation 落库；
8. CHN-X22 可按本地 Channel composition/生命周期切片标 `✅`；EIM-I8 必须继续 `🔵`，因为风险感知
   local fast path、公开管理员面与 rollout 仍 deferred；
9. 本轮完整 MultiRAG 最终门禁数字由根任务完成后回填；在此之前只能写“待回填”，不得从 focused
   测试或历史 `make verify` 数字推算。

#### EIM-I6 完成边界与 C1/C2/C3 接线交接

I6 的代码锚点为 `api/identity/provisioning_contracts.py`、`provisioning.py`、
`provisioning_repository.py`、`db_models.py::IdentityTenantPolicy/IdentityLinkCode/IdentityBindingEvent`
及 forward migration `b4c6d8e0f2a4`。接手者必须保留：

1. 每 Tenant policy 是数据库权威事实：显式 mode + `link_code_ttl_seconds(60..900)` + revision；缺
   policy fail closed。管理 capability 只有 explicit create/CAS，无 delete/default/generic CRUD，普通
   `IdentityService`/I6 write 不持有 admin port；
2. I3 alias-miss plan 携 policy revision。I6 只接受原 I3 request/result + fresh I4 proof，不接受 caller
   自选 target/action/mode/revision/role。stale-alias reverify 可刷新 canonical active identity；真正
   inactive/conflict/revoked 不自动恢复；
3. proof 必须精确匹配 provider/tenant/account/asserted open_id/scope marker，aware、非未来且最多 5
   分钟。所有可能阻塞的锁之后用 PostgreSQL `clock_timestamp()` 重新校验 proof/code，write/consume/
   event 使用 post-lock time；事务起点时间不能延长排队期间的有效窗口；
4. link code 使用 exact 24-byte/192-bit CSPRNG；raw value 只返回一次且 `repr` 隐藏，repository 只接
   HMAC-SHA256 digest + key id。新签发撤销同 account + target 旧 pending code；policy/account
   revision 或 scope marker 改变、过期/撤销/猜错/target conflict 对外统一 `IDENTITY_LINK_REQUIRED`；
5. preprovisioned 只激活已有 pending identity + live membership，不开户；link_only target 只来自
   当前 authenticated Principal 创建的 grant；JIT 只创建 external-only User 与 active
   `UserTenant(NORMAL)`，不建个人 Tenant/OWNER/ADMIN，也不写 email/password/access token；
6. active membership partial unique 与全状态 reverse external-identity unique 是最终数据库防线。
   User/UserTenant/identity/alias/code/first-binding event 在一个 fresh async transaction 中全成或全败，
   Provider 网络调用不进入事务；
7. `IdentityBindingEvent` 是 append-only 首次事实，按 identity/link grant/keyed request fingerprint
   唯一。它不保存 raw subject/alias/open_id/union_id、raw code 或 PII；复合 FK 所需的 server-owned
   account scope natural keys 仍在表中且 safe projection 隐藏；
8. explicit link 是 ExternalIdentity 到一个现有 User 的绑定与可选 local→hybrid，不是双 User merge。
   name/email/mobile/employee_no 不参与 target 匹配；合法 display name 只作展示并截到 100 字符；
   “I5 EnterpriseSubject 未实现”是 I6 完成时的历史边界，后续 I5 仍是独立后置 use case，不得回塞 I6；
9. I6 不接 HTTP/UI、Channel/C3、Principal 传播、I7 或 FastMCP/of_mcp。必须先由 C1/C2 建立
   structured assertion 与 execution contract，C3 adapter 才能把 binding/Channel assertion 组合成
   ProviderContext，调用 I3→I4→I6，并把最终 active records 重新解析/构造 P1 Principal；不得把 I6
   的成功 DTO 当 wire Principal；
10. 完成证据：domain/model unit **143 passed in 5.49s**；identity schema + provisioning schema +
    I3 repository + I6 repository 真 PostgreSQL **84 passed in 7.80s**；`make verify` 的 Ruff format
    **1241 files**、Ruff check、**7** 条 import contracts（832 files/2611 dependencies）、async DB gate、
    mypy **81 source files** 与 unit **2188 passed** 全绿；强制 integration **128 passed in 16.40s**；
    current-tree 安全终审无 blocker。

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
   不与根环境在同一解释器导入。`make mcp-compat` 当前协议矩阵为 **22/22 PASS**：双方向逐一
   覆盖 SDK 2 registry 的五个已发布 revision 并拒绝未知 revision；任何后续
   SDK/FastMCP/transport 改动都必须复跑，而不能用 mock 或 sibling import 替代。
4. **当前协议相关入口**：EIM-A1 已完成并固定 token/JWKS test vectors；of_mcp EIM-A3 已由
   `e4ab560` 完成 strict Resource Server，EIM-A4 已由 `74117a0` 接续 immutable Principal、
   per-tool policy 与真实 403。MultiRAG EIM-A7 的 A1/P1 前置已满足，但仍须通过 A7 独立实现
   inbound OAuth Resource Server、Principal/scope 和工具可见性。EIM-U14 已在 P3/A4/C3 完成后实现
   默认关闭的持久化暂停/恢复、Principal 绑定、revision/CAS、DB lease、恢复前重授权，以及 modern MRTR
   与声明式 legacy ask-before-effect 适配；它尚未启用、部署或接 U15 飞书 renderer。当前协议升级本身
   仍**不证明** inbound OAuth Resource Server 或生产 rollout；of_mcp A4 的完成态也不能替代 P2、A2/P3
   或飞书 identity resolver。

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

### 4.7 EIM-A3/A4/A5 完成基线与下一任务边界

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
4. `local` 是匿名开发 profile；`secure` 缺配置即 fail-fast、hello disabled，默认 service 保持
   mount-only。A5 proxy 只能通过显式 internal issuer/key/service resource 与 request-scoped provider
   装配；production scope/profile constants 必须逐字段匹配 A1 manifest；
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
contract snapshot 无漂移。A4 的历史边界不能倒填后续 A5 证据。

A5 以 of_mcp `5b4162a` 为完成锚点；冷启动 agent 必须保持以下不变量：

1. internal actor 使用独立 issuer/P-256 keyset/workload client 与精确 service audience；scope/TTL/
   assurance 只衰减，外部 Authorization 与透明 incoming headers 不转发；
2. FastMCP 4 异步 `ProxyProvider.client_factory` 从真实 request context 读取 Principal、ToolPolicy 与
   protocol version；每个逻辑目录/调用操作新建 `ProxyClient(auth=...)` 和 bearer，不共享用户 session；
3. backend 对 SDK registry 中 modern `2026-07-28` 精确镜像，对四个 handshake revision 使用官方
   `legacy` initialize 协商；未知/缺失 revision 在建 client 前 fail closed，不能用 `auto` 静默降级
   或按日期字符串猜测；ProxyProvider cache 只保存 raw catalog，用户 visibility 每请求执行，远端
   direct call 在执行前重验；
4. 独立 proxy composition root 使用 `RemoteAuthProvider`/PRM 与 strict internal verifier；Gateway 拒绝
   actor、proxy 拒绝 access、service resource 漂移 fail closed；audit schema v2 严格区分 access record
   与带 `parent_jti_hash` 的 actor record；
5. modern sessionless 不把 Principal/token/policy/replay 状态存在跨请求 Context 或 transport session；
   真实 key/config、私网 TLS/mTLS、部署、跨仓 E2E 与 remote release 均未完成。

A5 失败优先覆盖缺模块、默认 auto era、raw catalog mode 与 service resource drift；定向
**256 passed**，完整 `uv run --locked ofmcp verify` 六步全绿、**484 passed、2 skipped**，3 条 import
contracts 与 contract snapshot 无漂移。后续触碰 proxy factory、internal issuer/verifier、mixed assembly
或 audit schema 必须至少复跑 A5 定向矩阵、contract diff 和完整 verify。

EIM-F9 后续把 A5 的 era 分类扩成 SDK registry 驱动的完整门禁：四个已发布 handshake revision
统一使用 `legacy`，modern revision 精确镜像，未知/缺失 revision fail closed；MultiRAG 双方向真实
进程矩阵为 **22/22 PASS**，of_mcp 分类/真实 proxy HTTP 定向 **20 passed**，完整 verify 为
**499 passed、2 existing skipped**。FastMCP 4.0.0b3 升级仍须另立版本任务，不能借 F9 静默升级。

A6 phase 1 已接续且保持 `🔵`；MultiRAG F1/I3/I4/I5/I6/I7/P1/P2/C1/C2/C3/A2/P3/U14/U15 已完成。
I7 未做飞书后台订阅、服务重启或 live；I8 默认关闭的 durable reconciliation slice 已落地，但
风险感知 local fast path、公开管理员面与 rollout 仍待实现，因此保持 `🔵`。L1 代码、契约与最终门禁已完成并标
`✅`，但仍默认关闭；真实
`leave_applicant` authority 未配置，不能由 Feishu employee_no resolver 推断。M1 继续 medic-only；secure 在独立远程
发布闸门解除前仍不能作为远程业务入口。

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
  只能对应一个高风险逻辑操作，P3 已实现每次逻辑执行换新短 token/JTI；
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
production multi-instance durable replay/audit、HMAC KMS/rotation、OTel SDK/exporter/W3C 跨仓 trace、
P3/A5 动态 bearer 的真实跨仓证据、M3/M4 业务幂等/结果查询和 remote-release 演练后，才能把 A6
改为 `✅`。这类后续工作若涉及真实基础设施、KMS、DNS、Secret 或部署，必须先获得用户批准。

## 5. 跨仓协调

| 变更 | 生产者 | 消费者 | 安全部署顺序 |
|---|---|---|---|
| Channel structured assertion | worker | MultiRAG private API | tolerate API → emit worker → consume API → remove legacy |
| MCP access token | MultiRAG signer | `of_mcp` verifier/authorizer | A3 verifier/JWKS + A4 Principal/tool policy + A6 phase-1 execution guard 已先行并保持业务未远程发布 → P1/C3/P2 已完成 → A2 signer/JWKS + P3 每执行新短 token/JTI 已完成代码但默认 disabled、未部署 → A6 production durable backend/跨仓 trace + 企业主体/上线证据 → 独立闸门决定 secure 远程入口；不得把自动门禁或内存 replay 通过误作 Channel 委托闭环 |
| EIM-A1 corpus | MultiRAG canonical generator + 两仓本地副本 | PyJWT/joserfc 独立 oracle | 已完成：of_mcp `3e1d5ac` → MultiRAG 本次 A1 变更；91-file corpus 字节一致，digest `59f82684aa06365f45623ce9bfad336d487f2c9351879266a6b2ab21bf8fe208`；运行时无依赖 |
| 新 scope/tool metadata | `of_mcp` policy snapshot | MultiRAG Agent/MCP config、P3 cache/audit | resource 端先提交包含 effect/replay mode 的 canonical `tool-policies.json` 与 `policy_revision` → 调用端按 revision 重算请求与缓存；未知 scope fail closed，不从运行时可见列表反推权限，也不把 revision 自动塞入当前 A1 token profile |
| confirmation contract | `of_mcp` challenge | MultiRAG card/channel | resource 端先返回可识别 challenge → UI 接线 → 强制确认 |
| MCP 双向兼容 fixture | MCP SDK 2/FastMCP 4 主运行时 + PEP 723 FastMCP 3 真实 legacy 子进程 | 两仓 compatibility test | F2/F3/F4/F6/F7/F8/F9 已完成并形成 22/22 基线；双方向覆盖五个已发布 revision、未知版本拒绝及既有 auth/error/cancel 行为；后续每次协议/transport 变更逐格复跑；`of_mcp` F4 锚点 `23dd1fd`；不得用本机 sibling import 代替可复现安装 |
| MRTR interaction | `of_mcp` `input_required`/legacy adapter | MultiRAG U14 state machine，再到 U15 renderer | 先固定 transport-neutral request/response 与恢复语义 → 飞书表单渲染 → L2 只开放 p2p/read-only preview 与严格 terminal envelope → 敏感动作最后强制 U7/M3/M4；L2 不把 form submit 变成授权或确认 |

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
- 当前 `make mcp-compat` 必须保持 **22/22 PASS**。完成日志同时记录 MultiRAG SHA、of_mcp SHA、
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
feat(auth): issue short-lived MCP access tokens (EIM-A2)
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
