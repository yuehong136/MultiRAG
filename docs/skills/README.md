# 技能资产库与兼容核心

独立管理 SKILL.md、目录与版本，提供上传、下载、索引和检索。Web 入口为 `/skills`。
上传内容不会被执行，也不会自动装入 Agent。生产 Python 不依赖 Go 服务。

Python、Go 是各自管理数据的可替换后端。两者分别使用独立数据库（包括 File、租户和
模型绑定）、对象前缀与索引命名空间。不能将两个服务指向同一业务库再轮流写入。
迁移通过显式导出原始包、在目标导入并重新绑定模型和重建索引；当前没有全空间自动迁移工具。

## 选择协议

| 能力 | 固定 API | CLI | 合同 |
|---|---|---|---|
| Python/Go 核心：空间、Files 目录、最高版本、同步索引与搜索 | `/api/v1/skill-core` | `skill-core`、`ls/search/cat skills/...`、`install-skill` | [核心合同](CORE_CONTRACT.md) |
| Python 资产增强：不可变版本、活动发布、持久任务、严格 rerank | `/api/v1/skill-assets` | `skills` | [资产合同](CONTRACT.md) |

`SKILLS_API_PROTOCOL` 选择 `/api/v1/skills` 的部署别名，允许 `multirag-assets-v1`（默认）
或 `ragflow-skills-v1`。固定 namespace 不随别名改变；非法配置拒绝启动。
Web 使用 `VITE_SKILLS_API_PROTOCOL` 明确选择协议。消费者不能根据返回 payload 猜测协议。
通过 `GET /api/v1/skill-protocols` 和协议能力接口确认可用能力，Go 运行边界见
[Go 核心](GO_CORE.md)。Go 核心模式中的旧资产协议仅供读取和排空已有任务。

所有核心配置、索引与检索请求必须显式指定已创建、已授权的 `space_id`。
没有隐式 default 空间。模型 ID 使用十进制字符串，禁止转换为 JavaScript number。

## 运行与操作

1. Python 通过现有 Alembic 流程应用资产迁移 `c5e7f9a1b3d5` 和核心迁移
   `d6f8a0b2c4e6` 及各自前置迁移。Go 在自己的数据库管理两张 Go 核心私表。
   开发测试不会自动迁移业务库或切换部署协议。
2. 启动所选后端；Python 正常启动 FastAPI 会启动所需恢复 worker。
   核对数据库、MinIO 和 Milvus 能力后，由 Web/CLI 创建实际空间。
3. 核心通过 Files 创建技能/版本目录、上传正文与附件，再配置 embedding 并索引。
   资产增强接受目录或根含 SKILL.md 的 ZIP，等待 operation 成功后读回版本与活动发布。
4. 空间删除的 HTTP202 只表示已受理：核心轮询空间至404；资产增强轮询 operation。
   索引删除只移除检索条目；Files 删除由服务端联动索引，不要求客户端先删索引再删文件。
5. 构建失败保留旧索引；清理失败保留隐藏状态和持久地址，后台恢复。
   embedding 配置改变后需重建索引，不能把旧向量当作新配置可用。

核心受管树只允许领域适配过的创建、上传、读取和删除；普通 Files move/rename 明确拒绝，
避免绕过索引一致性流程。Python 资产扩展的不可变目录继续禁止普通 Files 修改。
不要直接删 SQL 行、手动置成功或绕过领域服务删除对象。非空核心/资产表禁止破坏性降级。

核心保存 rerank 配置但不执行，Python 资产扩展提供真正的 rerank 能力。当前已实现的
组合为 PostgreSQL、MinIO、Milvus；其他引擎不能宣称支持。外部模型需部署环境单独验收。
CLI 完整参数见 [CLI 用法](../../internal/cli/README.md#skills-core)。

## Python 只读 MCP 分发

设置 `MULTIRAG_MCP_SKILLS_RESOURCES_ENABLED=true`，通过当前身份发现活动发布，
按需读取固定版本正文、manifest 和附件。FastMCP 官方客户端可下载，读取逐项验证摘要。
默认关闭，不执行技能。协议限制与命令见 [MCP README](../../mcp/README.md#只读-skills-resources)。

## 复验

```sh
make verify
make integration INTEGRATION_WORKERS=0 TESTS='tests/integration/test_skill_core.py tests/integration/test_skill_core_state.py tests/integration/test_skill_assets.py tests/integration/test_skill_search_store.py tests/integration/test_skill_http.py tests/integration/test_mcp_skill_distribution.py tests/integration/test_file_batch_delete.py tests/integration/test_db_bootstrap.py'
make integration INTEGRATION_WORKERS=0 TESTS=tests/integration/skill_core_go_acceptance.py
make mcp-compat
make smoke
```

Go 验收需要符合 go.mod 的工具链，可设置 `MULTIRAG_TEST_GO` 为其绝对路径。
集成仅使用隔离资源，模型请求使用本地 HTTP 替身，不等于外部付费模型或生产验收。
浏览器 fixture 为 `tests/integration/skill_ui_server.py`，需显式启动和指定新的私有
`SKILL_UI_HANDOFF_PATH`；该文件含临时身份，不得归档。验收后写同路径 `.done` 清理。
本轮实际结果与剩余限制见 [交付状态](PROGRESS.md)，历史通过数不作为本轮证据。
