# Dataset RAPTOR scope

Dataset 的创建与设置接口使用 `parser_config.raptor.scope` 表示生成范围，
接受 `"file"`（单文件）和 `"dataset"`（全数据集）。创建时省略该字段默认
`"file"`；历史配置缺少 scope 时，现有表单和执行消费者也使用 `"file"`。
无需迁移历史 JSON 配置。

```json
{"parser_config":{"raptor":{"scope":"dataset"}}}
```

- `POST /api/v1/datasets` 的 RAPTOR 模型声明该字段；未知创建扩展可放在
  `parser_config.ext` 或 `parser_config.raptor.ext`，其他既有严格字段校验保持。
- `PUT /api/v1/datasets/{id}` 合并明确提供的字段。只修改 scope 不会重置
  RAPTOR 其他设置、未知配置、自动元数据或 Pipeline 关联；省略 scope 保留原值。
  显式非法值返回 HTTP 422，包含其他修改的请求不会部分写入。
- 旧的 `ext.parser_config` 覆盖入口同样检查有效配置中的 scope，保留原有覆盖顺序。
  RAPTOR 的执行 scope 位于 RAPTOR 顶层，`raptor.ext` 保持扩展数据的含义。
- Web 设置表单在读取、校验、提交和重载时保留 parser、RAPTOR、GraphRAG 及
  metadata 字段中的未知属性，包含 `ext` 和元数据限制。显式关闭 RAPTOR 不会
  因表单默认值被重新启用。
- 显式选择内置 `chunk_method` 仍按现有规则退出 Pipeline；scope 更新不会
  扩展 Pipeline 选择能力，也不改变 document PATCH 的严格局部更新合同。

保存配置不会提交解析或索引任务。配置读回只证明持久化；执行 RAPTOR、生成摘要、
写入索引及任务完成必须另做运行验收。

行为回归见 [模型与局部更新](../../tests/unit/test_dataset_raptor_scope.py) 和
[真实 HTTP 与独立 SQL 读回](../../tests/integration/test_dataset_raptor_scope.py)。
后者使用隔离 PostgreSQL，覆盖两种范围、默认值、非法值原子拒绝、扩展配置、
自动元数据、Pipeline 保留及显式退出现有 Pipeline，并检查保存未新增 Task。
