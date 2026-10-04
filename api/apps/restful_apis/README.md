# Dataset REST API

本目录路由挂载在 `/api/v1`。数据集业务由 `api/apps/services/dataset_api_service.py`
、`dataset_search_service.py` 和 `document_api_service.py` 提供。

图谱读取使用 `GET /datasets/{id}/graph`；不带 `doc_id` 时返回数据集聚合图，
带 `doc_id` 时返回该数据集文档的子图和思维导图。`/graph/search` 仍返回聚合图；
旧 REST `/knowledge_graph` 读取和删除继续作为 deprecated 兼容入口。
`DELETE /datasets/{id}/graph` 删除索引任务及产物，旧删除入口仅删图谱产物，二者语义不同。

## 聊天会话兼容

聊天会话更新以 `PATCH /api/v1/chats/{chat_id}/sessions/{session_id}` 为正式入口。
旧 PUT 已在聊天路由迁移中退出，当前无活动消费者；此前 HTTP 参考中的 PUT 是漏更新，
不据此恢复接口。同一会话路径的 PUT 请求返回 405，不进入更新处理器。PATCH 保留
JWT/API key 认证、聊天归属校验和请求验证。
`message`/`messages`/`reference` 不可改，客户端不能改写会话 ID、聊天 ID 或用户归属。

`POST /api/v1/chat/completions` 接收 `messages` 和 body 中的 `chat_id`/`session_id`。
SDK 的 `POST /api/v1/chats/{chat_id}/completions` 仍接收 `question`，无 session 时返回
开场白并创建会话；它不是 messages 转发入口。SDK 的 `/sessions/related_questions`
保留 API key 和 `industry`，REST `/chat/recommendation` 使用 `search_id`。
OpenAI 新旧补全路径继续由同一处理器提供，详见 [HTTP API](../../../docs/references/http_api_reference.md)。

无活动消费者或兼容承诺的 `/api/v1/file/{get,list,create,upload,mv,rename,rm,...}`、
chunk PUT 和 DELETE chats 的旧 `chat_id` body 不恢复；分别使用 files REST、chunk PATCH
以及单项 path/批量 `ids`。旧 run、图片、change_parser 与 upload_info 的退出合同保持。

## 数据集检索

`POST /datasets/{id}/search` 接收 `question`，返回 REST `code/data` 信封，
`data` 含 `chunks`、检索总数 `total`、`doc_aggs` 和 `labels`。JWT 与个人 API Key
共用数据集成员访问校验。默认检索路径中的数据集；本地多数据集消费者可传完整
`dataset_ids` 列表，必须包含路径 ID，所有选中数据集均须可访问且 embedding 一致。
联合检索一次完成排序、分页和文档聚合，不把每个数据集的分页结果拼接成联合结果。

保留 `doc_ids`、`page/size`、`top_k`（上限 2048）、相似度/向量权重、
`search_mode`（sparse/dense/hybrid/fusion）、高亮、跨语言、关键词、rerank 和 KG 参数。
`search_id` 指定有权访问的搜索应用时沿用其元数据配置，否则使用请求中的
`meta_data_filter`；手动过滤无匹配仍传递空结果哨兵，不放宽为全文检索。
普通检索过滤禁用 chunk；图谱读取保留隐藏图谱产物，文档子图还过滤 `removed_kwd=Y`。
Web 检索工作台、搜索应用和 Python 管理 CLI 已迁入这些 REST 入口；
旧 `/v1/chunk/retrieval_test`、`/v1/chunk/knowledge_graph` 已移除。
其他 chunk 管理入口及独立 SDK `/retrieval`、`/searchbots/retrieval_test` 不随之退役。

普通文档检索的[公共检索器](../../../core/nlp/search.py)在索引候选返回后、rerank 前，
用短异步 SQL 会话批量核对文档是否存在。SQL 行已删除、缺少文档 ID 或缺少候选字段的
普通 chunk 会被剔除，高亮随候选同步过滤；存活状态不跨查询缓存。`Document.status=0`
是禁用状态，禁用 chunk 仍沿用索引的 `available_int` 过滤，不把禁用误判成物理删除。

这是索引清理不完整时的检索兜底，不能替代[文档删除](../../db/services/document_service.py)
对索引的清理，也不会在检索时执行删除。索引结果的后端 `total` 保留原值；响应中的
`total` 和 `doc_aggs` 按当前候选窗口内存活且通过现有相似度规则的结果，在分页前计算，
不代表全索引的精确匹配数。候选窗口与分页偏移保持原语义，剔除后不补取，可能出现短页。
该检查观察查询时已经提交的 SQL 删除，不提供 SQL、索引和并发删除之间的原子快照；
全库 RAPTOR 摘要只有保留虚拟 ID `graph_raptor_x`、`raptor_kwd=raptor` 且绑定
有效的所选 SQL 数据集时才保留；文件级 RAPTOR 仍检查 SQL 文档。Milvus 动态集合
会读取该摘要标记。全库摘要及显式 KG 派生结果沿用独立生命周期：源文档删除后的
派生产物重建、删除不由本兜底负责。

## 文档创建

`POST /datasets/{id}/documents` 的 `type` 查询参数选择创建方式：省略或 `local` 使用
`multipart/form-data` 的 `file`（兼容字段 `files`）上传一个或多个文件，返回文档数组；
`web` 使用表单字段 `name`、`url` 抓取网页并存为 PDF，返回单个文档；`empty` 使用
JSON `{"name": "..."}` 创建虚拟文档及文件关联，也返回单个文档。三种方式都不自动开始解析。
旧 `/v1/document/web_crawl`、`/v1/document/create` 仍可用，并在 OpenAPI 中标为 deprecated。

代码沙箱生成的附件使用 `GET /api/v1/documents/artifact/{filename}`。请求须携带用户登录令牌或
个人 API Key；文件名必须是生成时的 32 位小写十六进制 ID 和允许的扩展名。链接带 `run_id`，
会话运行另带 `session_id`。CodeExec 上传时在 Redis 登记该文件名、请求用户、运行 ID 和可选
会话 ID；两个路由都核对精确登记。会话运行还检查会话存在及当前 Canvas 访问权，因此助手消息
保存前的流式产物可以下载。调试运行没有持久化会话，只凭文件名、用户和运行 ID 的精确登记放行。
登记和对象按沙箱产物保留期过期；历史上未登记的裸链接不能据助手消息文本推定归属。
旧 `GET /v1/document/artifact/{filename}` 共用相同鉴权与响应，标为 deprecated。
HTML、SVG 作为附件下载；其他允许的类型按文件类型返回。

网页抓取在工作线程中完成 URL 校验、PDF 转换和现有文件上传链；数据库会话在该线程内短暂创建，
不跨线程传递请求的 AsyncSession。空白文档经请求会话的 `run_sync` 桥接遗留同步文件服务。

## 索引与兼容接口

`POST/GET/DELETE /datasets/{id}/index?type=graph|raptor|mindmap` 分别执行、查询和删除索引任务。
查询参数 `type` 对上述三种合法类型忽略大小写；空值、其他类型、`GraphRAG` 和含空白的值仍拒绝。
UI 标签 `GraphRAG` 由前端映射为请求 `graph`；入队/trace 的真实任务类型为 `graphrag`，
知识库保存到 `graphrag_task_id`，摄取日志类型为 `GraphRAG`。RAPTOR 对应请求/任务 `raptor`、日志 `RAPTOR`。
DELETE 发送取消信号、删除任务行、解绑任务 ID/完成时间，并仅删除该数据集的 Graph `graph/subgraph/entity/relation` 或 RAPTOR `raptor` 产物；
普通文档和摄取日志保留，mindmap 当前不删除产物。前端删除统一使用 `/index?type=`。
删除操作另有三个明确的路径别名：`/graph`、`/raptor`、`/mindmap`。
不注册任意 `{index_type}` 路径，避免跨 router 抢占 `/documents` 的 DELETE。

旧 `run_graphrag` / `run_raptor` 复用 `run_index`，只将 `task_id` 映射回
`graphrag_task_id` / `raptor_task_id`；旧 trace 接口复用 `trace_index`。
旧 RAPTOR trace 在任务 ID 已设置、任务行已丢失时仍返回历史错误，新 trace 返回空对象。
旧 `auto_metadata` 与新 `metadata/config` 共用配置服务。上述旧接口保留 deprecated 标记。

## 元数据与标签契约

`POST /datasets/{id}/metadata/update` 和 `PATCH /datasets/{id}/documents/metadatas`
由同一个 handler 处理，复用已有 selector、更新和删除语义。

单数据集 `GET /datasets/{id}/tags` 返回 `[[tag, count], ...]`，保持上游契约；
`GET /datasets/tags/aggregation?dataset_ids=...` 返回 `[{"value": tag, "count": n}, ...]`。
检索器按各数据集的 `name` 构造独立索引，逐个查询、合并计数；缺失一个索引不会隐藏其他数据集。
标签功能仍受底层检索引擎已有能力限制，这些路由不新增 Milvus 标签聚合能力。

## 解析、停止与异步边界

`POST /datasets/{id}/documents/parse` 接收非空 `document_ids`。
请求同时含有效和不存在的文档时，有效文档仍排队，但响应业务 `code` 非零，
`message` 说明缺失 ID；同时保留 `data.success_count` 和 `data.errors`，便于识别部分执行结果。
不要将 HTTP 200 当作全部成功。停止接口先校验全部 ID，存在缺失 ID 时不执行停止。

详情、摄取概览、日志和索引 trace 使用原生 AsyncSession 查询，包括租户成员资格检查。
日志日期参数在 HTTP 入口转换为 datetime，非法日期返回 422。

解析/停止依赖的遗留队列流程仍交错使用同步数据库、对象存储、PDF 读取和 Redis。
其兼容边界在工作线程内创建、使用和关闭完整会话；不能用 `run_sync` 包住整个流程，
也不能把 AsyncSession 的 facade 传入线程。索引执行/删除和标签等同步外部操作同样由现有
工作线程入口承接。共享队列与检索辅助函数尚未完成原生异步迁移，不能宣称整个执行链已纯异步化。
