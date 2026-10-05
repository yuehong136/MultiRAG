# 引用中的文档元数据

聊天应用的 `prompt_config.reference_metadata` 和搜索应用的
`search_config.reference_metadata` 使用同一合同：

```json
{"include": true, "fields": ["author", "year"]}
```

- `include` 默认 `false`。关闭时，prompt 和返回引用都不附带文档元数据。
- `fields` 缺省或 `null` 表示全部字段；`[]` 表示不选字段。
- 请求中的 `reference_metadata` 按提供的属性覆盖应用配置。只传 `fields`
  保留应用的 `include`；只传 `include` 保留应用的字段选择。
- 字段值直接来自当前文档 metadata 字典，不读取历史包装或替代协议。
- 选中的字段通过引用 chunk 的 `document_metadata` 返回，也用于知识 prompt。
  `kb_prompt` 不再独立查询元数据，避免关闭引用元数据后仍把字段放入 prompt。

适用入口包括聊天 completion（普通和流式）、搜索检索与摘要、embedded retrieval
和 OpenAI 兼容聊天。OpenAI Python SDK 使用 `extra_body`：

```python
response = client.chat.completions.create(
    model="model",
    messages=[{"role": "user", "content": "检索问题"}],
    extra_body={
        "reference": True,
        "reference_metadata": {"include": True, "fields": ["author"]},
    },
)
references = response.choices[0].message.model_dump()["reference"]
```

流式模式的引用位于最终 completion chunk 的 `choices[0].delta.reference`。
Web 聊天及搜索设置支持全部字段、指定字段和明确清空，并在来源列表及详情中显示字段。
字段名查询失败会显示错误且保留已有选择。

`GET /api/v1/datasets/metadata/keys?dataset_ids=id1,id2` 返回排序去重的字段名。
接口检查每个知识库的访问权限；读取失败返回业务错误，不返回伪成功空列表。

补充数据严格按 `(dataset_id, document_id)` 配对读取。SQL 查询引用保留来源知识库；
只有单知识库且缺少来源列时才使用该知识库作为来源。多知识库来源缺失、来源不属于
所选知识库或跨多个知识库的图谱 chunk 不猜测元数据。SQL 聚合查询的来源查询也携带
知识库列。字段筛选不改变已有的知识库访问控制。

## 验证入口

- `tests/unit/test_reference_metadata.py`：覆盖字段存在性、同文档 ID 的知识库隔离、
  SQL 普通/聚合来源与 prompt 来源。
- `tests/integration/test_reference_metadata_http.py`：隔离 PostgreSQL、真实 HTTP
  鉴权及保存/GET/DB 读回；普通/流式聊天、搜索、embedded retrieval、keys 错误边界、
  OpenAI Python SDK 与 SQL 引用。模型生成和检索命中受控，不代表真实供应商或生产库验收。

```sh
make integration TESTS=tests/integration/test_reference_metadata_http.py
```
