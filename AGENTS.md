# MultiRAG 验证与开发规范（权威文档）

任何 AI 编码助手（Claude Code / Copilot / Cursor / Codex …）与人类贡献者在本仓库工作时，
都以本文档为验证流程的唯一权威来源。CLAUDE.md、copilot-instructions 等工具专属文件均指向这里。

MultiRAG：基于深度文档理解的企业级 RAG 后端（Python >=3.12,<3.15，FastAPI，uv 管理依赖，
非打包库、无 build-system）。主要包：`api/`（服务端）、`core/`（RAG 核心）、`agent/`、
`common/`、`deepdoc/`、`workflow*/`、`memory/`、`mcp/`、`admin/`。
`server/`、`internal/` 是停滞的并行实现，**不在质量门禁范围内**。

## 验证金字塔

| Tier | 命令 | 耗时 | 依赖 | 何时跑 |
|---|---|---|---|---|
| 0 | `make lint` | 秒级 | 无 | 每次改动 |
| 1 | `make typecheck` | ~10s | 无 | 每次改动 |
| 2 | `make test` | 1–2 min | 无 | 每次改动 |
| **0+1+2** | **`make verify`** | **~2 min** | **无** | **任何任务宣布完成之前必须全绿** |
| 3 | `make integration` | 分钟级 | docker compose base 服务 | 改动 DB/存储/检索路径时 |
| 4 | `make smoke` | 秒级 | 运行中的服务器 | 改动启动流程/路由/健康检查时 |
| — | `make fix` | 秒级 | 无 | 自动修复格式与 lint（不要手工排版） |

`make help` 列出全部目标。CI（`.github/workflows/ci.yml`）跑的就是同一套命令。

## 核心规则

1. **编码后必跑 `make verify`**，全绿才算完成。改动涉及 DB/存储/检索时加跑 `make integration`。
2. **修根因，不改门禁**。禁止用以下手段换绿：删除/跳过测试、往 ruff `ignore` 加规则、
   扩大 mypy `exclude`、放宽 marker。pyproject 中的门禁配置视为变更受控——确需调整时
   单独说明理由。（燃尽清单里的规则是遗留债，只出不进。）
3. **新代码从严**：新增/修改的函数必须写完整类型注解（py3.12 风格：`list[str]`、`str | None`）。
   运行时另有 beartype 校验，注解错误会直接在测试中暴露。
4. **格式交给工具**：提交前 `make fix`。全库曾做过一次性 `ruff format`
   （见 `.git-blame-ignore-revs`；本地执行
   `git config blame.ignoreRevsFile .git-blame-ignore-revs` 可让 blame 跳过该提交）。

## 写测试

- **单元测试 → `tests/unit/`**（平铺）：纯净、无网络、不连真实服务。外部依赖一律
  monkeypatch；DB 参数用未绑定 `Session()` 过 beartype（见 `tests/unit/conftest.py`
  的 `db`/`fake_kb` fixtures）；需要绕过会查库的 `__init__` 时用 `object.__new__(Cls)`。
- **集成测试 → `tests/integration/`**：需要真实 PostgreSQL/Redis/MinIO。conftest 会自动探测：
  服务缺失时整体 skip，`REQUIRE_SERVICES=1` 时硬失败（CI 用）。marker 自动附加。
- **`tests/manual/`**：性能/压测脚本，不被收集，手动运行（见其 README）。
- marker 只有三个：`integration`、`slow`、`smoke`（`--strict-markers` 强制）。
- `async def test_*` 直接写即可（pytest-asyncio `asyncio_mode=auto`）。

## mypy 棘轮（渐进式类型检查）

当前检查范围见 `pyproject.toml [tool.mypy] files`（common 核心 + api/utils + scripts；
`common/data_source/`、`common/doc_store/` 是待清理的燃尽目标）。晋升流程：

1. 清理某个包的 mypy 错误；
2. 把它加进 `files`（或从燃尽 exclude 中移出）——CI 从此保证它不回退；
3. 包内质量再上台阶时，用 `[[tool.mypy.overrides]]` 提升 `check_untyped_defs` /
   `disallow_untyped_defs`。

只进不退：不允许把已纳管的包移出范围。

## 配置与资源（重构后的规范）

架构（详见 internal/config_bootstrap_refactor_plan.md）：
`common/app_config.py`（类型化配置，env `MULTIRAG_<SECTION>__<FIELD>` >
local.service_conf.yaml > service_conf.yaml）→ `common/resources.py`（有状态资源，
懒加载选中后端）→ `common/bootstrap.ensure_initialized()`（入口点统一初始化）→
`common/settings.py`（PEP 562 兼容 facade，上游移植 diff 照抄的官方访问面）。

**新代码规则**：
1. 读配置用 `get_app_config()` 的类型化字段，不要新增 `settings.大写名`；
2. 路由层资源用 `api/apps/deps.py` 的 `Depends(get_storage)` 等注入
   （测试用 `app.dependency_overrides` 替换，见 tests/unit/test_api_deps.py 示范），
   不要直接引用 `settings.docStoreConn`/`settings.STORAGE_IMPL`；
3. **例外：紧跟 ragflow 上游的文件保持 `settings.X` 风格**，保证上游 diff
   可照抄（映射表：internal/ragflow_settings_porting_map.md）；
4. 新入口点（脚本/服务）先调 `common.bootstrap.ensure_initialized()`；
   核心资源未初始化即访问会 fail-fast 抛 `ResourcesNotInitialized`。

## 服务与运行

```bash
# 基础服务（PostgreSQL + Redis/valkey + MinIO）
docker compose -f docker/docker-compose-base.yml up -d

# API 服务器（先起基础服务；端口/地址来自 configs/service_conf.yaml 的 multirag 段）
uv run python -m api.multirag_server

# 任务执行器
uv run python -m core.svr.task_executor
```

- **本地配置覆盖**：`configs/local.service_conf.yaml`（已 gitignore）按**顶层 section 整体替换**
  `configs/service_conf.yaml`——覆盖某 section 时必须把该 section 的字段写全。
- **健康端点**：`GET /api/v1/system/ping`（→ pong）、`GET /api/v1/system/healthz`
  （分组件状态，`scripts/smoke.py` 消费）。
- 依赖同步：`make install`（等价 `uv sync --group dev --frozen`）。

## 自动化钩子

- **pre-commit**（可选，人各一次）：`uv run pre-commit install`——commit 时自动跑
  ruff check/format 与基础卫生检查。
- **Claude Code**：`.claude/settings.json` 的 PostToolUse 钩子对每次编辑的 .py 单文件
  即时 ruff（自动修复 + 残留问题回灌）。其他 AI 工具没有钩子，务必遵守规则 1。

## 编码规范（速查）

- Python 3.12+ 类型：`list[str]` / `dict[str, Any]` / `str | None`（禁用 `List`/`Optional`/`Union`）
- FastAPI 0.128+：`Query(pattern=)`、`Body(examples=[...])`、`FastAPI(lifespan=...)`
- Pydantic v2：`model_config = ConfigDict(...)`、`.model_dump()`、`.model_validate()`、`@field_validator`
- SQLAlchemy 2.0：`Mapped[...]` + `mapped_column()`、`select(M).where(...)`、
  主键查询用 `session.get(Model, pk)`
- lint/format 全部由 ruff 承担，规则见 `pyproject.toml [tool.ruff.lint]`
