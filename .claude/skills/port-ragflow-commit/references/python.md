# Python 移植适配

本文件只用于 Python 生产代码移植，通用工程规则仍由 [AGENTS.md](../../../../AGENTS.md) 及其按需细则维护。

## 行为与兼容性

我方缺失的修复或新行为，尽量保留上游结构和方法名，降低后续合并成本；框架差异和本地契约优先。
我方已有带类型的 helper、Protocol 或等价抽象时，按 [改进价值判断](../SKILL.md#等价之后仍须比较改进价值)
比较上游机制；优先复用适合的本地抽象，必要时改进其内部实现，不为形式对齐撤掉本地改进。
无抽象分歧的代码可以直接对齐；发现上游缺陷时做保证本次行为正确的适配，说明差异及必要的日落点。

函数返回形状或字段变化时，沿生产者、本仓全部活跃调用方（含本地扩展入口）、存储映射和读回逐段核对。
解析器传递元数据时确认真实数据形状、合并或覆盖顺序，并检查每条写入路径在索引前消费和移除临时字段。

上游删除或替换路由、请求字段、响应别名、默认行为或兼容分支时，逐合同核对目标 SHA、
后续演进及本仓与独立 web、SDK、MCP、后台和相关 Go 的活动调用。不能只检查路由路径；
新路由继续接受旧信封、返回旧别名或套用旧默认，同样会留下兼容债。
已完成迁移且没有活动调用的旧接口，或任务明确下线的未使用入口，直接删除，
无需为删除补一个替代 API。不能仅凭上游称“未使用”判断本仓，也不因假设存在未知客户端
而一律保留。只为查明的活动调用或明确的兼容承诺保留入口并标为 `deprecated=True`，
记录具体调用位置、仍使用的旧合同、替代契约和退出条件；未迁移的调用方在授权范围内时，
完成迁移和验收后删除兼容层。既有单元测试、DTO 定义、旧文档或“前端仍调用”的注释
不能代替活动调用证据；文件内有一个活动入口不构成保留其他无用接口的理由。
移除入口时清理专用请求模型、导入和过时引用；数据库表、历史数据及独立 service 的删除
另按实际任务范围处理，不随 API 退役自动执行。
共享 helper 和相似命名的接口按实际调用分别判断；例如临时文件转文本与数据集异步解析
不是同一合同。验收已删除入口的实际 HTTP 404、OpenAPI 移除及无执行副作用，
并验证保留调用链的输入、业务码和适用存储读回。
移除旧信封时，将兼容成功测试改为旧载荷拒绝且无写入、旧字段从 OpenAPI 移除的断言，
不要为了保留旧测试结果继续提供旧语义。另验证新合同的省略、显式空值与启停行为，
特别防止旧默认把启停或空请求变成清空字段、自动开启。上游仍存在的数据丢失缺陷
应按本地正确合同修复并说明差异，不能以“与上游一致”为由复制。
新增路由前查已有 REST / SDK 入口，避免重复注册；集中兼容层存在时复用。

## 框架映射

| 上游 | MultiRAG |
|---|---|
| Quart/Flask 路由与请求对象 | FastAPI `APIRouter`、Pydantic body、`Depends` |
| peewee 查询 | SQLAlchemy 2.0；新 service 用 `AsyncSession`，handler 用 `async def` + `Depends(get_async_db)` |
| settings 定义变更 | 查 [RAGFLOW_PORTING_MAP](../../../../docs/enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md)；消费者的 `settings.X` 保留兼容 facade 用法 |
| `rag/` | `core/`；具体文件名核实当前树，例如 `rag/llm/chat_model.py` → `core/llm/chat.py` |
| `api/apps/restful_apis/*_api.py` | 同名路由；路由层 helper 不可反向引入底层 service，遵守 import-linter |
| llm_factories `model_type` | `mdl_type` |

## async 与验证

新 service 的 async 基建已经落地，不再套用旧的“上游 async handler 默认改成 def”规则。
修改遗留同步链时保持事务一致；禁止在 async handler 直接回灌同步 DB IO。
涉及 session、阻塞桥接或 `LLMBundle` 时读 [异步 SQLAlchemy 细则](../../../../docs/development.md#异步-sqlalchemy-编码规范)，
其中包含 `run_sync` 的范围与 facade session 不得逸出的约束。

按我方 [测试选型](../../../../docs/development.md#写测试) 重建行为测试，不逐字搬上游测试结构。
通用验证见 [AGENTS.md](../../../../AGENTS.md#验证)；额外兼容矩阵按受影响协议执行。
