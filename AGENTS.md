# MultiRAG 开发协作规范

本文件是仓库级开发与验证约定的唯一维护入口；`CLAUDE.md`、
`.github/copilot-instructions.md` 只引用这里。用户本次明确指令优先；模块文档补充局部约束。
版本、命令和实现状态以当前代码、`pyproject.toml`、`Makefile` 与 CI 为准，本文只保留需要判断的原则和项目特有的边界。

## 项目导航

MultiRAG 是基于深度文档理解的企业级 RAG 后端，使用 Python、FastAPI、SQLAlchemy 和 uv。

| 工作内容 | 入口 |
|---|---|
| API / 业务服务 / 数据模型 | `api/apps/`（新 REST v1 端点在 `restful_apis/`）、`api/db/services/`、`api/db/db_models.py` |
| 模型接入 / 文档处理 / 后台任务 | `core/llm/`、`core/flow/`、`core/app/`、`core/svr/task_executor.py` |
| Agent / 文档解析 / 知识图谱 | `agent/`、`deepdoc/`、`core/graphrag/` |
| 配置 / 资源 / 数据连接器 | `common/app_config.py`、`common/resources.py`、`common/data_source/` |
| 部署 / MCP | [docker/README.md](docker/README.md)、[mcp/README.md](mcp/README.md) |

`server/` 与 Go 侧 `cmd/`、`internal/` 是停滞的并行实现，不是当前 Python 后端的扩展入口。

## 核心规则

1. **围绕结果推进。** 先读相关实现和调用方，再做足以解决问题的改动；在已授权范围内自主完成实现、验证和必要文档。只有会改变目标、兼容性或外部影响的关键不确定性才需要澄清，普通实现选择自行判断。
2. **保护工作现场。** 开始检查 `git status` 和相关 diff，保留用户及其他任务的改动；只暂存本任务路径。格式化、回滚和清理都要限定范围，避免用全库操作处理局部问题。生产变更先查明现状、影响与回退方式，遵守本次授权范围。
3. **修根因，保持边界。** 不靠吞异常、伪造成功、削弱断言或放宽门禁掩盖问题。允许随行为变更更新测试；确需调整质量策略时，单独说明依据与影响，不能把既有失败当作降低标准的理由。
4. **用证据交付。** 按下节选择验证，区分本次回归、已有问题和环境阻塞；报告实际执行结果与未验证范围。历史通过数、耗时和其他机器的基线不能充当本次证据。
5. **维护程序契约。** 修改 `api/channels/`、`api/channel_control/`、`api/channel_execution/`、`api/channel_runtime/` 时，先读 [Channel README](docs/channel-program/README.md)，按其协议更新账本，提交标题带对应 CHN ID。EIM 与 Run Platform 工作分别从 [EIM README](docs/enterprise-identity-mcp/README.md)、[Run Platform README](docs/run-platform/README.md) 进入；只读与当前任务有关的章节，不顺手扩展相邻任务。

## 验证

| 改动 | 验证要求 |
|---|---|
| 仅文档、注释，且不改变可执行行为 | 检查 diff、路径、链接及内容与实现的一致性；无需全套 Python 测试 |
| Python 代码或影响其行为的配置、依赖 | 开发中跑相关检查，交付前跑 `make verify`（lint + typecheck + unit） |
| DB、事务、存储或检索路径 | 在 `make verify` 之外跑 `make integration`，必要时补所选后端的专项验证 |
| 启动流程、路由或健康检查 | 在 `make verify` 之外跑 `make smoke`，并验证改动端点的行为；健康检查不覆盖全部业务契约 |
| 特定协议或程序契约 | 加跑对应程序要求的检查，例如 MCP 兼容矩阵；不能用专项检查替代通用门禁 |

- 命令以 [Makefile](Makefile) 为准；`make help` 列出目标。无 `make` 时执行目标内的等价命令，不减少检查项。
- 由 Ruff 负责 Python 格式与 lint。局部修改用 `uv run --no-sync ruff check --fix <paths>` 和 `uv run --no-sync ruff format <paths>`；`make fix` 会处理全库，仅在确认不会混入无关修改时使用。
- 不要求每个任务开工都跑全套基线；涉及复杂重构、已有故障或失败归因时，再建立所需的本机对照。失败后先定位，只有新改动或新证据才值得重跑。
- 本次引入的失败必须修复。无关失败或依赖不可用时，继续完成可验证的部分并明确交付限制；不得声称全绿，也不擅自修复其他任务。集成测试 skip 不算验证通过。
- `make integration` 会检查服务并以 `REQUIRE_SERVICES=1` 运行；测试用隔离资源，禁止拿业务库做破坏性验证。直接运行集成 pytest 时，fixture 可能用 testcontainers 补服务；`INTEGRATION_NO_TESTCONTAINERS=1` 可关闭该行为。

## 写测试

测试锁定行为与契约，优先复用现有 fixture；修 bug 时增加能暴露该问题的回归用例，避免只复述实现步骤的测试。

| 被测对象 | 方式 |
|---|---|
| HTTP 状态、业务 retcode、响应载荷 | `tests/unit/` 的 `client` fixture + `dependency_overrides`，在服务边界 monkeypatch |
| 编排、算法、错误处理 | `tests/unit/` 的直接调用 + 显式依赖替换 |
| SQL、事务、迁移 | `tests/integration/` 的 `pg_scratch_engine` / `bootstrapped_engine` / `bootstrapped_async_engine` |

- 单元测试不得依赖真实外部服务。使用 [unit conftest](tests/unit/conftest.py) 提供的 `db`、`async_db`、`client` 等假件与覆盖，不新增 `sys.modules` 整包伪造。
- SQL 语义用真库验证；[integration conftest](tests/integration/conftest.py) 提供一次性 scratch 库，不操作配置中的业务数据。
- 异步测试直接写 `async def test_*`；marker 和收集规则以 `pyproject.toml` 为准。手工性能脚本放 `tests/manual/`。

## 配置与资源

- 新代码通过 `get_app_config()` 读类型化配置，路由通过 `api/apps/deps.py` 注入资源；资源生命周期由 `common/resources.py` 管理。需要应用资源的新入口先调用 `common.bootstrap.ensure_initialized()`。
- `common/settings.py` 是兼容 facade。紧跟 RAGFlow 的文件可以保留 `settings.X`，避免无关重构增加上游合并成本；对应关系见 [RAGFLOW_PORTING_MAP](docs/enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md)。移植上游提交时使用项目 `port-ragflow-commit` skill。
- 配置优先级：`MULTIRAG_<SECTION>__<FIELD>` 环境变量 > `MULTIRAG_CONFIG_OVERLAY_FILE` 外部文件 > `configs/local.service_conf.yaml` > `configs/service_conf.yaml`。文件覆盖按顶层 section **整体替换**，覆盖时必须写全该 section 所需字段；具体语义见 `common/config_utils.py` 与 `common/app_config.py`。
- 本机差异放在已忽略的本地覆盖中；不为跑测试改写共享配置，不提交密钥或在日志中暴露凭据。
- 遵守 `pyproject.toml` 的 import-linter 依赖契约。共享逻辑下沉或通过接口注入，避免底层反向依赖路由和业务层；运行代码不依赖停滞实现。

## 异步 SQLAlchemy 编码规范

新 service 使用 `AsyncSession`，对应 handler 用 `async def` + `Depends(get_async_db)`。
修改遗留同步路径时保持请求内事务一致，不为小改动扩大到整条链路迁移。

- 一个请求或 task 管理自己的 session；同一请求内不混用同步与异步 session，不跨并发 task 共享 `AsyncSession`。需要原子性的操作保持在同一事务中。
- 使用 SQLAlchemy 2.0 查询与映射方式。显式预载 relationship，新 relationship 默认 `lazy="raise_on_sql"`；确需延迟加载时使用 `awaitable_attrs`，避免隐式 IO。
- 异步工厂保持 `expire_on_commit=False`；需要数据库新值时显式 `refresh`。大结果集按需流式处理，避免无界加载。
- async 路径使用异步网络/存储客户端；无法立刻迁移的阻塞调用可放入 `asyncio.to_thread`，但不能把当前 session 一起传给并发线程。
- `run_sync` 只用于遗留桥接，保留 `TODO(async-phase4)` 标记。它只适配传入 session 的 IO，不能让回调内自开连接的同步 helper 变成非阻塞。
- `run_sync` 的同步 facade session 不得逸出回调。遗留 `LLMBundle` 构造后若持有它，必须在回调内剥离（`bundle.db = None`），避免后续 rollback 过期 ORM 状态并触发 `MissingGreenlet`。

新增或修改的函数使用完整类型注解，沿用 Python 现代类型、Pydantic v2 和项目现有接口；注意 beartype 的运行时类型校验。Ruff、mypy 的具体范围以配置为准，已纳管范围不回退。

## 零上下文交接

- 有 CHN / EIM 等任务 ID 时，用 ID 定位最新契约和状态；没有 ID 的普通需求直接按目标开展。交接补充目标、范围、已做改动、验证结果、待决问题和未入库事实即可，不要求固定提示词模板。
- 仓库文档写可供下一位维护者复用的事实，明确当前行为、已知限制与计划。持久记忆和本地笔记只能辅助定位，不能替代代码、契约或本机验证。
- 文档分层：`AGENTS.md` 放跨模块规则；`docs/<program>/` 放设计、契约、任务状态；模块 `README.md` 放当前行为与排障边界。更新事实发生的那一层，避免多处复制。
- `internal/*.md` 是用户本地笔记，可按任务需要读写，但不得暂存提交。上游对照细节与入库文档的分工见 [CHN-ADR-05](docs/channel-program/DECISIONS.md#chn-adr-05--文档分层入库讲我们的代码本地讲别人的代码)。

## 服务与运行

先检查已有进程、监听端口和服务状态，复用可用实例；需要启动时使用以下入口：

```bash
make install  # uv sync --group dev --frozen

docker compose -f docker/docker-compose-base.yml up -d
uv run python -m api.multirag_server
uv run python -m core.svr.task_executor
```

地址和端口以生效配置为准。健康端点为 `GET /api/v1/system/ping` 与
`GET /api/v1/system/healthz`；诊断还需核对组件状态、业务响应和相关日志，HTTP 200 本身不代表业务成功。
部署与恢复见 [docker/README.md](docker/README.md)，工具钩子以 `.claude/settings.json` 和实际脚本为准，不能替代交付验证。
