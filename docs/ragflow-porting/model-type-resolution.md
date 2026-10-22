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

## 提前修复与连续跟进游标

本次本地修复提交为 `205868fffcdf654bd94c98b28ef0f35fd115ec3b`，是独立的生产故障
修复，不是后续上游提交的整项移植，也不推进连续跟进游标。

2026-10-22 核实的已完成连续批次为 [1046042e 后续 20 项](batch-after-1046042e.md)，
末项是 `7ec87f7cb78889180a1cd67d46d10a247977ae7e`（2026-05-11，北京时间）。
交付报告与外置 `scope.json` / `delivery-complete.json` 一致；对应本地组合已包含在
故障前部署的 `3dae701e` 中。下一项是 `ed01ac999408fd3b3109785eacbbf430d3bf039f`。
批次的 `519e7d98` 是核对后修的冻结上限，不是完成游标；本次故障核对的
`20fbe3b7` 也不替代该游标。

5 月 29 日的 `dc4b82523bf85d93db067dd339101f4c6951e23b` 位于上述游标之后，
按上游 first-parent 顺序是后续第 331 项，不属于已完成的连续批次。
在 `7ec87f7c` 中，上游旧 helper 同样只按模型名查询，但 Peewee `Model.get()`
匹配多条时取首条；MultiRAG 的 SQLAlchemy `one_or_none()` 则抛出多行异常。
本地 `a6ff1fa7` 已保留该无租户条件的查询；本次 500 属于存量 SQLAlchemy 适配
及回归覆盖缺口，不能归因为已完成批次漏掉了 5 月 29 日提交。

## 上游对照

本次刷新并冻结的上游为
[`20fbe3b79668b7a08df5f7d2fd141e0c637901b0`](https://github.com/infiniflow/ragflow/commit/20fbe3b79668b7a08df5f7d2fd141e0c637901b0)。

对照了以下演进：

- [`dc4b82523bf85d93db067dd339101f4c6951e23b`](https://github.com/infiniflow/ragflow/commit/dc4b82523bf85d93db067dd339101f4c6951e23b)
  （2026-05-29，PR #14595）已将活动 LLM/Agent 链改为
  `get_model_type_by_name(tenant_id, model_name)`，按租户、供应商和实例查询。
- [`6a77523bf0d18096561dc809f31b9eb88179ea50`](https://github.com/infiniflow/ragflow/commit/6a77523bf0d18096561dc809f31b9eb88179ea50)
  （2026-07-09，PR #16744）引入统一 `resolve_model_type`，兼容模型 ID 与名称引用；
  LLM/Agent 优先使用 chat 类型。租户范围解析在前述 5 月提交中已经存在。
- [`4c97c9ad64a0134eddf6f0766febf4735fb473c4`](https://github.com/infiniflow/ragflow/commit/4c97c9ad64a0134eddf6f0766febf4735fb473c4)
  （2026-09-10，PR #19449）删除包含 `llm_id2llm_type` 的废弃接口。
- `670e68872` 删除 Python 源码前，活动 LLM、Agent、对话和提示词链已经使用
  `resolve_model_type(tenant_id, model_ref)`；最新 Go 的
  `internal/service/model_factory.go:ResolveModelType` 也通过租户访问上下文解析模型身份。

采用上游的租户范围和已登记模型类型作为权威来源。MultiRAG 保留 SQLAlchemy 和
现行单表 `TenantLLM`，一条登记仍对应一个 `mdl_type`，不在本次故障修复中迁移上游
provider/instance/bitmask 整套模型结构。本项是该故障链的本地适配，不代表完整移植
PR #16744 的全部 51 文件。

## 后续提交的衔接规则

下表是提前覆盖的片段及后续核对要求，三个上游提交均未因本次修复标为整项完成。

| 上游提交 | 本次已覆盖片段 | 到达该提交时仍须处理 |
| --- | --- | --- |
| `dc4b8252` / #14595 | 模型类型解析限定当前租户和显式供应商，聊天/Agent 等活动入口退出旧 helper。 | 完整评估 148 文件的 provider/instance/model/group、配置与数据迁移、API 及消费者足迹。当前单表解析不能视为整套模型体系等价；新结构落地时将解析器和调用链一起收敛。 |
| `6a77523b` / #16744 | 统一解析入口的本地适配、类型列表返回及 chat 优先选择。 | 完整评估 51 文件；模型 ID、provider/instance、类型位掩码、配置解析及其余调用方均不能因函数同名而跳过。保留现行 SQLAlchemy Session/async 事务规则。 |
| `4c97c9ad` / #19449 | 已删除 `TenantLLMService.llm_id2llm_type`，活动调用方已迁移。 | 仅该删除片段可核为已等价；仍须审查两个文件中其余废弃方法和 joint-service 清理，不重复删除，也不整项略过。 |

实施后续移植前先核对当前基线是否包含 `205868ff` 或等价补丁，再按完整上游 diff
区分已覆盖片段与剩余范围。本次与 5 月、7 月提交分别有 7、6 个同路径文件交集；
这只是文件交集，不是冲突数或完整影响面，`rag/` → `core/` 等映射另行核对。
需要替换当前解析实现时直接迁移并收敛到新的权威入口，不为套补丁回退此次修复，
不恢复无租户范围的全局类型查询，也不长期叠加两套解析器。

复用下述真实 PostgreSQL 回归；数据结构迁移时同步调整夹具和断言，持续证明跨租户
同名互不影响、显式供应商精确选择、含糊裸名拒绝，以及缺失/禁用登记失败。
当前首页仍提交裸模型名，后续收紧引用格式前先完成真实消费者迁移。
逐项记录采用、等价、剩余范围与验证证据；只有整项足迹审结后才更新该项状态和连续游标。

## 回归范围

`tests/integration/test_model_type_resolution.py` 使用隔离 PostgreSQL，覆盖跨租户
同名、同租户跨供应商同名、登记类型覆盖目录、compatible 后缀、禁用及缺失登记，
并独立读回确认没有配置写入。相关聊天、附件、记忆、检索引用和模型配置消费者
分别运行定向 unit/integration；供应商运行可用性需另以真实 HTTP 请求验收。
