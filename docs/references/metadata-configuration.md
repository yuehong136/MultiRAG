# 元数据模板配置

模板保存在数据集或文档的 `parser_config`，与已抽取的 `meta_fields` 分离。
保存模板不会启动解析，也不会重写已有文档的抽取值。

## 数据集合同

`GET /api/v1/datasets/{id}/metadata/config` 返回业务信封 `code=0`，
`data.metadata`、`data.built_in_metadata` 是当前模板，`data.enabled` 是自动抽取开关。
兼容读字段 `data.fields` 继续提供历史字段列表。

`PUT` 同路径接受：

```json
{
  "metadata": [{"key": "year", "type": "number", "enum": ["2026"]}],
  "built_in_metadata": [{"key": "source", "type": "string"}],
  "enabled": false
}
```

新信封只替换显式提供的模板，省略项保持不变；`[]` 明确清空对应模板。
`enabled` 映射到 `parser_config.enable_metadata`，省略不会开启自动抽取。
其他 parser 配置、Pipeline 绑定及已保存的未知字段保留。

兼容旧信封 `enabled/fields`、旧 `/auto_metadata` 路径以及创建/更新数据集的
`auto_metadata_config`。仅用旧信封时保留历史默认：省略 enabled 为 true，省略
fields 为空列表，因此 `{}` 仍表示旧式清空并开启。新旧字段同时出现且内容冲突，
或显式 null，返回 422，不写入。调用方要只调整开关且保留模板时，应同时提交当前
`metadata`，不要依赖旧信封的省略规则。

## 字段与迁移

- 新字段使用 `key/type/enum`；type 可为 string、list、time、number。历史无 type
  字段仍可读写，不批量改写存量数据。
- `name/examples/restrict_values` 继续接受。examples 默认只是提示示例；仅
  restrict_values=true 时作为枚举约束。原有 enum 仍为约束。
- number 的数字字符串在抽取 JSON Schema 中转为数值；拒绝非数字和非有限值。
  存储保留提交表示。list 转为字符串数组，枚举约束作用于元素；time 使用 string。
- 字段未知扩展保留。完整 JSON Schema 对象保留 properties、required 和其他约束。
  内建字段与自定义字段同名时，沿用内建字段优先的合并规则。
- Web 编辑器保存小写 type，重载同时读取字段列表和 Schema 的 type；取消限制值
  后写为 examples，不继续发送 enum。仅保存模板不会擅自打开自动抽取。

迁移顺序为先升级后端双读写合同，再升级 Web。SDK 通用 parser_config 映射无需
改名；调用方如继续使用旧信封，保留其既有默认行为。无需数据库迁移或全量回填。

## 文档与执行边界

文档专用 `PUT .../documents/{document_id}/metadata/config` 替换完整 metadata
模板，删除的 Schema 属性不会因递归合并重新出现；其他 parser 配置保留。
通用 document PATCH 仍按原局部更新、校验、事务与解析状态规则执行，增加对 typed
metadata 字段的校验支持，不把专用 PUT 的替换规则扩散到通用 PATCH。

应用知识库配置到文档时，同时复制 metadata、built_in_metadata、enable_metadata
和 llm_id。普通解析仍受 enable_metadata 控制；Pipeline 沿用自己的执行分支。
模型输出是否满足 Schema 仍依赖现有生成链，本次没有增加输出强校验或实际供应商保证。

## 回归入口

- `tests/unit/test_metadata_field_contract.py`：字段兼容、DTO、类型与 Schema 投影。
- `tests/integration/test_metadata_config_contract.py`：真实 HTTP、业务码、重载及独立 SQL。
- `tests/integration/test_task_metadata_generation.py`：实际存储、任务抽取与 number Schema。
- `tests/integration/test_document_ingest.py`：应用 KB 配置及已有 Pipeline/清理边界。
