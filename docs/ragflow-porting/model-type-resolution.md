# 租户模型类型解析修复（2026-10-22）

## 故障与行为

聊天及 Agent 构造仍使用全局模型名推断类型。不同租户注册同名自定义模型时，
`TenantLLMService.get_or_none(llm_name=...)` 会触发 SQLAlchemy
`MultipleResultsFound`，使聊天请求在调用供应商之前返回 500。
目录中的同名模型类型也可能覆盖当前租户实际登记的类型。

当前调用方统一使用 `tenant_model_service.resolve_model_type(db, tenant_id, model_ref)`：

- 类型来自当前租户登记，限定显式供应商；复用当前 Session。
- 保留现有 `name@factory` 引用及 OpenAI-compatible 存储后缀解析。
- 首页仍提交裸模型名，因此保留当前租户内唯一名称的解析；同租户多个供应商
  存在同名登记时要求明确供应商，不任选第一条。
- 缺失、禁用、类型缺失的登记失败；不借用其他租户或全局目录的类型。
- async 请求经现有 `run_sync` 桥接查询，不在线程池中另开全局查询连接。

没有修改模型凭据、供应商地址、知识库向量或数据库结构。供应商的余额不足、
模型不存在等错误仍是真实失败，不能用类型解析修复将其标为可用。

## 上游对照

本次刷新并冻结的上游为
[`20fbe3b79668b7a08df5f7d2fd141e0c637901b0`](https://github.com/infiniflow/ragflow/commit/20fbe3b79668b7a08df5f7d2fd141e0c637901b0)。

对照了以下演进：

- [`6a77523bf0d18096561dc809f31b9eb88179ea50`](https://github.com/infiniflow/ragflow/commit/6a77523bf0d18096561dc809f31b9eb88179ea50)
  引入 `resolve_model_type`，按租户模型引用解析类型；LLM/Agent 优先使用 chat 类型。
- [`4c97c9ad64a0134eddf6f0766febf4735fb473c4`](https://github.com/infiniflow/ragflow/commit/4c97c9ad64a0134eddf6f0766febf4735fb473c4)
  删除包含 `llm_id2llm_type` 的废弃接口。
- `670e68872` 删除 Python 源码前，活动 LLM、Agent、对话和提示词链已经使用
  `resolve_model_type(tenant_id, model_ref)`；最新 Go 的
  `internal/service/model_factory.go:ResolveModelType` 也通过租户访问上下文解析模型身份。

采用上游的租户范围和已登记模型类型作为权威来源。MultiRAG 保留 SQLAlchemy 和
现行单表 `TenantLLM`，一条登记仍对应一个 `mdl_type`，不在本次故障修复中迁移上游
provider/instance/bitmask 整套模型结构。本项是该故障链的本地适配，不代表完整移植
PR #16744 的全部 51 文件。

## 回归范围

`tests/integration/test_model_type_resolution.py` 使用隔离 PostgreSQL，覆盖跨租户
同名、同租户跨供应商同名、登记类型覆盖目录、compatible 后缀、禁用及缺失登记，
并独立读回确认没有配置写入。相关聊天、附件、记忆、检索引用和模型配置消费者
分别运行定向 unit/integration；供应商运行可用性需另以真实 HTTP 请求验收。
