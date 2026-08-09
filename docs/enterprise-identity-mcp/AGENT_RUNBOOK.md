# 企业身份与 MCP · 零上下文 Agent 执行手册

> 目标：一个没有任何历史对话的 Agent，只凭本目录就能安全接手一个独立任务。
> 本文规定工作方式；技术事实以 [CONTRACTS](CONTRACTS.md)、[DECISIONS](DECISIONS.md) 和任务行本身为准。

## 1. 接单格式

用户应尽量按 ID 派工：

```text
读 docs/enterprise-identity-mcp/README.md 和 AGENT_RUNBOOK.md，执行 EIM-I3。
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
- 在 `/Users/xldu/project/of/of_mcp` 工作时，重新读取那里实际存在的 `AGENTS.md`；MultiRAG 规则
  不能代替另一个仓库的规则。
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

- 默认一个企业对应一个 MultiRAG tenant，全体员工是“可识别主体”，不等于自动拥有所有资源权限；
- `platform_user_id = MultiRAG User.id`；飞书长连接返回的 `open_id` 只是外部 alias；
- 飞书规范主键为 `(tenant_key, user_id)`；`(provider_account_id, open_id)` 是解析入口；
- 不做每日全量组织复制：首次 JIT + Contact 事件失效 + 周期性增量/对账；
- 身份服务和表归 MultiRAG；`of_mcp` 是受保护资源，不拥有企业主身份库；
- MCP 使用短时 ES256 access token、JWKS、resource/audience 校验；不透传外部 token；
- 业务员工号是 enterprise subject 映射，不是登录凭据；歧义时人工处理；
- 敏感写操作必须授权 + 确认 + 幂等；参数里的 `workcode` 不可信；
- 先平台资源授权，再 MCP 工具 scope，再业务对象授权，三层都不可省略。

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
- 迁移前输出冲突审计，只读脚本不得自动猜测合并；
- 测试里使用 scratch 数据库，绝不连接配置中的生产 dbname。

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

### 4.5 密钥和外部配置

开发前按 [FEISHU_ONBOARDING](FEISHU_ONBOARDING.md) 取得测试应用配置。Agent 只能说明或消费用户
明确提供的 secret，不能把 secret 写进源码、测试快照、命令输出或文档。

本地配置应进入已 gitignore 的 secret/env 载体；测试使用低熵、明显虚假的占位值。需要管理员在
飞书后台授权、发布版本、修改可见范围或重启线上进程时，先给出精确操作和影响，等待用户批准。

## 5. 跨仓协调

| 变更 | 生产者 | 消费者 | 安全部署顺序 |
|---|---|---|---|
| Channel structured assertion | worker | MultiRAG private API | tolerate API → emit worker → consume API → remove legacy |
| MCP access token | MultiRAG signer | `of_mcp` verifier | verifier/JWKS 能力先 → signer emit → 强制 auth → 移除旧 auth |
| 新 scope/tool metadata | `of_mcp` policy | MultiRAG Agent/MCP config | resource 端先兼容 → 调用端请求；未知 scope fail closed |
| confirmation contract | `of_mcp` challenge | MultiRAG card/channel | resource 端先返回可识别 challenge → UI 接线 → 强制确认 |

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

### 6.3 手工/线上验证

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
