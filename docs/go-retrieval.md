# Go 检索删除残留过滤

Go `internal/` 是并行实现；当前生产 Python 检索及其派生产物生命周期见
[REST 说明](../api/apps/restful_apis/README.md)。本页说明 Go
[`RetrievalService`](../internal/service/nlp/retrieval.go) 及实际消费者
[`ChunkService.RetrievalTest`](../internal/service/chunk.go)，不扩展生产入口。

候选从索引返回后、任何本地评分或外部 rerank 前，使用带请求 context 的短 SQL 查询
核对文档存活。查询只读 ID 和数据集 ID，去重且不缓存。文档物理删除、缺少文档 ID、
缺少候选字段、候选与 SQL 父级数据集不匹配时剔除；SQL 查询失败返回错误。权限、
指定文档、元数据及可用性过滤继续由现有消费者和索引执行。
`Document.status=0` 是禁用状态，不能替代物理删除判据。

全库 RAPTOR 摘要同时具有虚拟文档 ID `graph_raptor_x`、`raptor_kwd=raptor`，并绑定
有效的所选 SQL 数据集时保留。Infinity 固定投影读取该标记；标量和单元素数组均可识别。
文件 RAPTOR 仍要求 SQL 文档存在。源文档删除后的全库 RAPTOR/KG 重建或删除沿独立
生命周期处理，检索不执行索引清理。显式 KG 检索在当前 Go 消费者仍未实现。

剔除时按原候选顺序同步重建 IDs、Chunks、Field 和 Highlight；保留后端 Total、查询向量、
关键词与搜索元数据。分页仍使用原候选窗口和偏移，剔除后不补取，可能出现短页或空页。
`doc_aggs` 在分页前统计当前窗口内存活并通过既有相似度规则的候选；Go `RetrievalTest`
响应的 `total` 仍是当前页经过 parent 合并后的 chunk 数，不是全索引匹配总数。
parent 展开按文档/数据集分组，只接受与已验证 child 同源的 parent；跨文档残留或旧 ID
碰撞保留原 child。既有合法 parent 合并和评分方式继续保留。

存活检查观察查询时已提交的 SQL 状态，不提供 SQL、索引和并发删除之间的原子快照。
当前 Elasticsearch 的字段/IDs/高亮提取仍是既有未实现函数，Milvus Search 显式返回未实现
错误；本次没有用假成功扩展这些后端。实际索引验收使用 Infinity。

Go session 目前是绑定模型文本聊天，未调用上述检索器。其存储设置和请求配置在
`buildGenConf` 合并，再经 `ChatModel` 的 typed JSON 转换传给 provider；整数
`max_tokens` 支持 JSON 数字与本地 int，`stop` 支持 JSON 数组与本地字符串数组。
小数/溢出 token 数和数字 stop 元素返回配置错误，不截断或静默丢弃。

专项测试遵循[开发细则](development.md#go-并行实现的本地验证)。
`TestRetrievalScratchPostgresInfinity` 需要 `MULTIRAG_GO_RETRIEVAL_DSN` 指向名称以
`multirag_go_retrieval_` 开头的自有 PostgreSQL scratch 库，以及
`MULTIRAG_GO_RETRIEVAL_INFINITY_URI`；测试创建随机同前缀 Infinity 数据库并清理，
SQL scratch 的创建、删除由调用者负责。缺少环境变量时 opt-in 测试跳过，不能作为集成通过。
`TestChatSessionScratchPostgres` 按既有 `MULTIRAG_GO_MODELS_DSN` scratch 约定运行。
