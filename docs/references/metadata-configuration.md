# 元数据模板配置

模板保存在数据集或文档的 `parser_config`，与已抽取的 `meta_fields` 分离。
保存模板不会启动解析，也不会重写已有文档的抽取值。

## 数据集合同

`GET /api/v1/datasets/{id}/metadata/config` 返回业务信封 `code=0`，
`data.metadata`、`data.built_in_metadata` 是当前模板，`data.enabled` 是自动抽取开关。

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

创建/更新数据集的 `auto_metadata_config` 使用相同信封。只调整开关时可提交
`{"enabled": false}`，模板保持不变；`{}` 不修改模板或开关。显式 null 和未知字段
返回 422，不写入。旧 `fields` 信封已移除，`GET/PUT .../auto_metadata` 返回 404，
响应不再提供 `data.fields` 别名。

旧信封的移除对齐 RAGFlow `501845911`；刷新后的 `origin/main`（`2400ca8e`）
也只注册 `/metadata/config`，配置使用 `enabled/metadata/built_in_metadata`。
本仓保留现有 Python 存储结构、Schema 对象和显式项更新语义；省略项不清空模板。

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

Web 与 Hub 已使用 `/metadata/config` 和新信封。SDK 通用 parser_config 映射无需
改名；旧调用方需改用 `metadata` 替代 `fields`。无需数据库迁移或全量回填。

## 检索成员条件

检索 `in/not in` 列表按保存的 JSON 标量类型匹配：数值 `0`、文本 `"0"`、布尔
`false` 和文本 `"False"` 各自独立，布尔值也不等同于数字 0/1。字符串仍忽略大小写；
数值 int/float 按数值相等比较。聚合的公开字符串视图保持原格式，显示值碰撞时合并
文档列表，内部保留类型索引供成员匹配。
不从显示字符串猜测原类型。非法列表元素继续使完整条件组无匹配，不能让否定条件
变为全匹配。其他比较运算、字符串成员操作数与空选择合同保持原语义。

非比较运算统一归一化输入值与条件值：字符串及列表中的字符串转小写，数字、布尔
和空值不转字符串、不改变类型。直接列表输入的 `contains/not contains` 也忽略字符串
大小写；`in/not in` 的直接列表输入仍要求每项满足条件。生产文档聚合会先展开数组，
按匹配项收集文档 ID，因此同一文档有匹配和不匹配项时，可同时出现在 `in` 和
`not in` 的结果中；两者结果集不一定互补。无匹配仍使用 `-999`，不放宽已有文档范围。

## 元数据过滤执行

Agent、dataset search、聊天/Ask/mindmap、embedded search 和 SDK retrieval 共用
惰性过滤入口。无过滤、手工空条件及非法 membership 条件不读取元数据；有效条件先按
当前调用方已授权的 KB 集合求值，再与非空文档选择取交集。无匹配使用 `-999`。

| 后端 / 运算 | 执行方式 | 完整性与失败边界 |
| --- | --- | --- |
| SQL metadata（Milvus）非比较运算 | 按 tenant/KB 绑定，以服务端游标逐行读取，保留 JSON 类型，复用原匹配器 | 每批 1000 行，无结果条数上限；只积累匹配 ID，不构建全库值→文档聚合 |
| Elasticsearch 非比较运算 | 已知且完整索引的标量字段可下推存在性候选条件；读取原始 `_source` 后精确求值 | 映射不明、复杂键、结构值、`ignore_above/ignore_malformed` 等改为仅按 KB 扫描，不假定值 DSL 等价 |
| 比较运算及其他引擎 | 保留聚合匹配回退 | 不借性能优化改变既有 literal coercion、负运算或类型语义；不宣称其他引擎已获得值下推 |
| auto / semi_auto | 生成条件前按需读取聚合；单次请求首次读取缓存复用 | semi_auto 仍按选中字段及已有刷新回调验证当前目录，不被另一条索引查询覆盖 |

ES 候选与聚合回退均完整消费 scroll 快照，跨越默认 10k 窗口，并在结束或异常时清理
cursor；缺索引是正常空结果，超时、分片失败、RPC/DB 错误和越界响应均传播为失败。
不把部分页、失败枚举或异常清理改成空/全量成功。能力不支持的 `None` 与已确认无匹配
的 `[]` 明确区分。执行保留展开数组后按值桶收集 ID 的现行语义，不把 `not contains`
或 `not in` 改写为排除整个文档。ES 值 DSL 的 Unicode、CSV、空字段和多值负运算
假设不适用于这一合同。

这些优化只减少聚合内存与不必要读取；仍需读取候选原始值，匹配 ID 列表按命中数增长。
元数据阶段不截断结果，不改变后续 chunk 检索的候选窗口、排名或分页合同。

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

## 表格列角色

数据集 `parser_config.table_column_mode` 为 `auto`（省略或 null 同效）时，所有列同时
进入索引文本与元数据。设为 `manual` 后，`table_column_roles` 按原始列名指定
`indexing`、`metadata` 或 `both`；未指定的列仍为 `both`。旧合法值 `vectorize`
在保存时归一为 `indexing`，其他角色或模式拒绝。Web 设置页在首次解析发现
`table_column_names` 后显示选择器；保存配置不改写已解析的数据，需重新解析生效。

`indexing` 仅进入向量和全文索引文本，`metadata` 仅保留可筛选属性，`both` 同时保留。
JSON 存储使用原始列名；ES 使用 `table_` 前缀的有类型字段并额外保留原始文本值，避免用户列覆盖 `title_tks` 等内置搜索字段。多 sheet 按同一
列名统一推断类型并合并字段清单；同 sheet 重名列、ES 归一后冲突列拒绝解析。
空单元格忽略，合法零值保留；无法用统一类型无损表示的列保留为文本，避免丢弃单元格。

任务将原始元数据聚合为文档 `meta_fields`，保留数值类型与完整单元格文本。文档筛选
依然是文档范围：某列匹配后，检索可读取该文档的多行，不能当作行级 SQL 筛选。
Milvus 的元数据与私有 `table_metadata_state` 存在独立 SQL 表；后者记录各行任务的
贡献及本次解析新增的值，不进入 API 元数据。重新解析、移除列或改成仅索引时，只移除
解析器曾新增且仍存在的值，保留原有同名手工值和其他用户字段；用户把某生成值以完全
相同的内容重新加入已有列表时，系统无法区分其来源，该值仍按生成值处理。

多段行任务按解析摘要合并，复用旧 chunk 的新任务 ID 不丢贡献；已删除、取消或被替换
的任务不能更新元数据。SQL 读合并前通过不覆盖已有数据的插入建立稳定行锁，首次解析与
手工保存也会串行；解析失败回滚占位行。元数据保存失败向任务抛错，不能报告解析成功。升级需运行
Alembic 迁移 `e7a9b1c3d5f0`；本批未执行生产迁移。真实运行证据以交付报告列明的后端为准，
ES、Infinity 与 OceanBase 的适配分支没有因单测覆盖而获得真实后端验收。
