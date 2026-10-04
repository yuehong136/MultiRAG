# MultiRAG 开发协作规范

本文件是仓库级开发与验证约定的唯一入口；`CLAUDE.md` 和
`.github/copilot-instructions.md` 只作映射。用户本次明确指令优先于项目 Skills 和文档。
实现、版本和命令以当前代码、`pyproject.toml`、[Makefile](Makefile) 与 CI 为准。

## 工作边界与完成标准

- 从用户目标和当前上下文确定范围、完成证据；普通实现选择自行判断。已授权的工作持续到实现、适用验证、回归修复和必要文档完成；包含运行验收时，实际运行并检查结果。只读审计按审计范围交付。
- 本地编辑、隔离测试、修复本次失败及复跑受影响检查可连续完成，无需逐步确认。关键不确定性会改变目标、兼容性或外部影响时才澄清；已有授权不重复索取。需要批准的动作先准备具体方案和可审查结果，再询问。
- 先检查 `git status` 与相关 diff，保留其他任务改动；格式化、回滚、清理和暂存均限定本任务范围。生产操作先查现状、影响和回退方式，遵守用户的只读、暂停与审批边界。
- 修根因，不靠吞异常、伪造成功、削弱断言或放宽门禁换绿。质量策略确需调整时单独说明依据与影响；无关失败不擅自修复。

## 按需导航

MultiRAG 的当前生产后端使用 Python、FastAPI、SQLAlchemy 和 uv。
只读与任务有关的实现、调用方及下列章节；普通修订无需先通读架构、账本或部署文档。
`server/` 与 Go 侧 `cmd/`、`internal/` 是停滞的并行实现，不是 Python 后端的扩展入口。

| 任务 | 入口 / 适用约定 |
|---|---|
| API / 业务服务 / 数据模型 | `api/apps/`（新 REST v1 在 `restful_apis/`）、`api/db/services/`、`api/db/db_models.py`；改 DB/session 时读 [异步 SQLAlchemy](docs/development.md#异步-sqlalchemy-编码规范) |
| 模型 / 解析 / 后台任务 | `core/llm/`、`core/flow/`、`core/app/`、`core/svr/task_executor.py`、`agent/`、`deepdoc/`、`core/graphrag/` |
| 配置 / 资源 / 数据连接器 | `common/app_config.py`、`common/resources.py`、`common/data_source/`；读 [配置与资源](docs/development.md#配置与资源) |
| 新增或修改测试 | [写测试](docs/development.md#写测试)；优先复用 unit 假件与 integration scratch 库 |
| 启动 / 运行诊断 / 部署 / MCP | [服务与运行](docs/development.md#服务与运行)、[Docker](docker/README.md)、[MCP](mcp/README.md) |
| Channel | 修改 `api/channels/`、`api/channel_control/`、`api/channel_execution/`、`api/channel_runtime/` 时读 [Channel README](docs/channel-program/README.md)，按协议更新账本，提交标题带对应 CHN ID |
| EIM / Run Platform | 分别从 [EIM README](docs/enterprise-identity-mcp/README.md)、[Run Platform README](docs/run-platform/README.md) 定位当前任务和契约，不扩展相邻任务 |
| 跟进指定 RAGFlow commit / PR | 使用项目 [port-ragflow-commit](.agents/skills/port-ragflow-commit/SKILL.md) Skill |
| 修改协作指令 / Skills / 派工示例 | [维护协作指令](docs/development.md#维护协作指令) |

## 验证

| 改动 | 交付要求 |
|---|---|
| 仅文档、注释，不改变可执行行为 | 检查 diff、路径、链接和实现一致性；无需全套 Python 测试 |
| Python 代码或影响其行为的配置、依赖 | 开发中跑相关检查，交付前跑 `make verify`（lint + typecheck + unit） |
| DB、事务、存储或检索路径 | 另跑 `make integration`，必要时补所选后端专项验证 |
| 启动流程、路由或健康检查 | 另跑 `make smoke`，并验证改动端点的行为 |
| 特定协议或程序契约 | 加跑对应检查（例如 MCP 兼容矩阵），不替代通用门禁 |

- 无 `make` 时运行目标内的等价命令。Ruff 局部修复用 `uv run --no-sync ruff check --fix <paths>` 和 `uv run --no-sync ruff format <paths>`；`make fix` 会修改全库，不作为局部任务的默认步骤。
- 不要求开工跑全套基线；只在复杂重构或失败归因需要时建立本机对照。适用检查通过后，仅在新改动、失败或未解决疑点需要时扩大或重复验证。
- `make integration` 在收集前准备核心套件所需服务，以 `REQUIRE_SERVICES=1` 运行并保存证据；分组、后端与独立消费者验收见 [测试说明](tests/README.md)。使用隔离资源，禁止对业务库做破坏性测试；Testcontainers 回退可用 `INTEGRATION_NO_TESTCONTAINERS=1` 关闭。
- 交付写清实际结果、本次回归、已有问题与环境阻塞。集成 skip 不算通过，历史通过数不能充当本次证据；阻塞时完成其余可验证部分并说明限制。

## UI 快照审阅

审阅多张应用截图时，先拼成带标注的 contact sheet，只读拼贴图，不要逐张读。
每格标明文件名、页面/路由、视口。
截图过多时拆成多张 contact sheet，每张仍一次读完。
仅当某格看不清（小字、像素级对齐），或只有 1 张图时，才读原图。

## 代码约束

- 新增或修改的 Python 函数使用完整类型注解，沿用现代类型、Pydantic v2 和现有接口，注意 beartype 运行时校验。遵守 Ruff、mypy、import-linter 和 async DB 门禁的当前纳管范围。
- 新 service 使用 `AsyncSession`，对应 handler 用 `async def` + `Depends(get_async_db)`；遗留同步链保持请求事务一致，不为局部修改扩大迁移。细则按上表读取。
- 本机差异放已忽略的本地覆盖，不为测试改共享配置，不提交密钥或在日志中暴露凭据。

## 零上下文交接

有 CHN / EIM 等 ID 时用它定位最新契约和状态；普通需求直接按目标开展。交接补充范围、完成证据、
已做工作、待决问题及未入库事实即可，无需固定模板。记忆和本地笔记辅助定位，不能替代当前代码与本机验证。

本文件放跨模块规则，[开发细则](docs/development.md) 放按需工程约定，`docs/<program>/` 放设计、契约和任务状态，
模块 README 放当前行为与排障边界；同一事实只维护一处。`internal/*.md` 为本地笔记，不得暂存提交；
上游对照分层见 [CHN-ADR-05](docs/channel-program/DECISIONS.md#chn-adr-05--文档分层入库讲我们的代码本地讲别人的代码)。
