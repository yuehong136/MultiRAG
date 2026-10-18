# Agent Hub 联验问题归因与处理

2026-10-18 核对 RAGFlow `origin/main=543e16738b4883fcb7e70520e0b5849e7e18c9b7`，
以及删除 Python 前的 `90767771f37a2ca2c41b2e795e486f9a0c8529e3`。上游 remote 为
`git@github.com:infiniflow/ragflow.git`。以下针对本地 Python 生产链路，不迁移上游 Go 服务。

| 反馈 | 上游依据与归因 | 本地处理 |
| --- | --- | --- |
| MR-01：新库上传未解析文档后手动添加切片，写入成功但向量检索失败 | 上游没有 Milvus 实现；上游 Infinity 的插入路径从 `q_<dim>_vec` 推断维度并创建对应列。本地 Milvus 旧 lazy 建库路径只创建固定 `vector(768)`，把实际 `q_1024_vec` 留在动态字段中 | `MilvusConnection.create_idx` 复用 worker 的 mapping 建库器，同时创建实际维度的 dense 字段、BM25 函数及索引。新建集合不再走不完整的通用 fallback schema |
| MR-02：普通文档列表泄露数据库异常 | 上游旧 Python `api/utils/api_utils.py:server_error_response` 直接返回 `repr(e)`；当前 Go `internal/handler/document.go:ListDocuments` 已使用固定 `failed to get documents`。此安全处理从 Go API 初版 `70e9743ef1` 即存在，不能称为 Python 后续补丁 | 参照当前上游固定错误文案，将权限读取、元数据过滤、查询和响应映射异常统一转换为 REST 数据错误。保留本地 `code=102` 和正常鉴权拒绝语义；不返回 SQL、角色名或驱动错误 |
| MR-03：首个 worker 文档失败，重试后正常 | 上游无对应 Milvus/本地历史快照实现。现场日志显示集合创建后，另一任务在索引构建、加载完成前读取历史，Milvus 返回 `101 collection not loaded` | 集合存在时仍确认加载就绪；历史快照先等待全部向量索引完成再加载。索引失败、加载失败及超时向上传递，不将失败读转换为空历史 |
| MR-04：图片写入失败但索引已确认更新 | 上游 Python 和当前 Go 都分步更新图片与索引；上游图片写入先于索引写入，也没有跨存储事务。本地先写索引，失败时可保留旧图，并明确报告部分结果 | 保留本地 `outcome=partial`、`index=acknowledged`、图片状态及 `retry_safe=false`。这不是原子回滚承诺；不盲目重发 append，也不为形式对齐上游反转写入顺序 |

MR-03 的就绪等待上限为 60 秒。Milvus 集合已经加载时只检查加载状态；未加载时等待
实际 schema 中各向量字段的索引完成，再由 SDK 等待加载完成。已有坏 schema 的集合
拒绝将 `q_<dim>_vec` 继续写入动态字段，写入前报错并保留原行；不会自动重建、删除或迁移，
需要先确认数据与重建方案。

回归入口：

```sh
uv run --no-sync pytest -q tests/unit/test_restful_document_list_route.py tests/unit/test_milvus_safe_mutations.py tests/unit/test_task_executor_pool_lifecycle.py tests/unit/test_document_ingest_history.py tests/unit/test_document_parse_routes.py
make integration TESTS=tests/integration/test_milvus_safe_mutations.py PYTEST_ARGS=-q
make integration TESTS=tests/integration/test_document_metadata_filter_http.py PYTEST_ARGS=-q
make integration TESTS=tests/integration/test_document_source_recovery.py PYTEST_ARGS='-q -k source_recovery_preserves_authenticated_same_chunk_winner'
```

真实 Milvus 回归独立读回 schema、1024 维向量和 dense/BM25 命中，控制并发建库窗口验证
历史快照等待，并检查释放集合后的完整历史读取。PostgreSQL 回归仅在 scratch 库对自有角色
拒绝元数据表 SELECT，验证 token/会话请求的安全失败、权限恢复和数据保全。
BM25 schema 引入的 protobuf repeated 字段由测试证据序列化器转为列表，保留完整证据。

这些检查不替代现有常驻进程的更新、真实模型联验、生产部署验收或最终组合 CI。
MR-05 原混合 auto/semi_auto 两次空结果未捕获同请求的生成谓词，仍保留为未归因的质量限制。
另一次独立生成诊断添加了不存在的 `color` 条件，不能将它当作原失败的确定根因。
简单无歧义正例通过也不能消除该限制。
