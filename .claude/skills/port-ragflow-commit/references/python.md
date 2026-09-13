# Python 移植适配

本文件只用于 Python 生产代码移植，通用工程规则仍由 [AGENTS.md](../../../../AGENTS.md) 及其按需细则维护。

## 行为与兼容性

我方缺失的修复或新行为，尽量保留上游结构和方法名，降低后续合并成本；框架差异和本地契约优先。
我方已有带类型的 helper、Protocol 或等价抽象时复用，不为上游顺带重构撤掉本地改进。
无抽象分歧的代码可以直接对齐；发现上游缺陷时做保证本次行为正确的适配，说明差异及必要的日落点。

上游删除或注释的生产 API 保留可用兼容入口并标记 `deprecated=True`；只有明确迁移/下线范围才移除。
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
