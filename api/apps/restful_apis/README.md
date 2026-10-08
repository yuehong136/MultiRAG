# Dataset REST API

本目录路由挂载在 `/api/v1`。数据集业务由 `api/apps/services/dataset_api_service.py`
、`dataset_search_service.py` 和 `document_api_service.py` 提供。

图谱读取使用 `GET /datasets/{id}/graph`；不带 `doc_id` 时返回数据集聚合图，
带 `doc_id` 时返回该数据集文档的子图和思维导图。`/graph/search` 仍返回聚合图；
旧 REST `/knowledge_graph` 读取和删除继续作为 deprecated 兼容入口。
`DELETE /datasets/{id}/graph` 和旧删除入口都会取消任务、清理图谱产物并解绑任务 ID/完成时间。
旧入口固定全量清理并返回 `true`；正式入口接受 `wipe`/`task_id`，返回空对象。

## 数据集保存与文档状态过滤

数据集创建使用 `POST /datasets`，部分更新使用 `PUT /datasets/{id}`。
创建与更新均接受 `avatar: ""` 和 `avatar: null`；非空 avatar 继续校验图片 MIME 前缀和长度。
更新省略字段时保留现值；`embedding_model: null` 保留当前模型，`parser_config: {}` 或
`null` 在未切换解析器时保留配置。切换解析器且未提供有效配置时使用该解析器默认值。
parser 配置按层合并，只校验显式传入的已知字段；未知扩展字段保留，显式
`built_in_metadata: []` 清空内置元数据选择，省略则保留。`pagerank` 只接受整数 0–100。
非法请求体由 FastAPI 返回 HTTP 422，权限、模型不可用等业务失败返回 `code=102`；
成功信封仍为 `code/data`，列表总数位于 `total_datasets`。名称重名时拒绝，不自动追加后缀。

`GET /datasets/{id}/documents` 的 `run`、`run_status`、`run[]`、`run_status[]`
均可重复传值并混用，支持大小写状态名和数字状态（含 `0`）；先去首尾空白并忽略空值。
非法非空状态返回 `code=102`。普通文档列表与 `type=filter` 聚合使用相同状态条件。

## 聊天会话兼容

聊天会话更新以 `PATCH /api/v1/chats/{chat_id}/sessions/{session_id}` 为正式入口。
旧 PUT 已在聊天路由迁移中退出，当前无活动消费者；此前 HTTP 参考中的 PUT 是漏更新，
不据此恢复接口。同一会话路径的 PUT 请求返回 405，不进入更新处理器。PATCH 保留
JWT/API key 认证、聊天归属校验和请求验证。
`message`/`messages`/`reference` 不可改，客户端不能改写会话 ID、聊天 ID 或用户归属。

`POST /api/v1/chat/completions` 接收非空 `messages`，或仅传 `question`；最后一条消息须为 user。
有会话时默认使用服务器历史并追加本次最后一条 user 消息；显式传布尔
`pass_all_history_messages=true` 才使用提交的完整分支历史，并保留匹配轮次的已有引用。
`session_id` 缺省或为空时回落 `conversation_id`；两者同时非空但不一致时拒绝请求。
布尔、消息和附件格式在调用前校验；附件通过认证用户的上传登记恢复服务器 descriptor。
成功生成并保存后才发送成功结束帧；失败或取消不保存半轮历史，自动会话也不提前落库。
活动 Web `/v1/conversation/completion` 仍使用其独立完整分支历史合同。
SDK 的 `POST /api/v1/chats/{chat_id}/completions` 仍接收 `question`，无 session 时返回
开场白并创建会话；它不是 messages 转发入口。SDK 的 `/sessions/related_questions`
保留 API key 和 `industry`，REST `/chat/recommendation` 使用 `search_id`。
`bot_api.py` 承接 SDK/beta 会话与公开 bot 路由，`dify_retrieval_api.py` 提供 Dify 检索，
`chunk_api.py` 承接 SDK 解析、停止和 `/retrieval`，`document_download_api.py` 保留
SDK 二进制下载与 beta-key 资源归属校验；路径和响应合同不因代码搬迁改变。
本地 SDK dataset/files 模块仍有调用，继续通过注册表挂载 `/api/v1`。
OpenAI 新旧补全路径继续由同一处理器提供；请求中的 session 提示不会读取或保存会话，详见 [HTTP API](../../../docs/references/http_api_reference.md)。

无活动消费者或兼容承诺的 `/api/v1/file/{get,list,create,upload,mv,rename,rm,...}`、
chunk PUT 和 DELETE chats 的旧 `chat_id` body 不恢复；分别使用 files REST、chunk PATCH
以及单项 path/批量 `ids`。旧 run、图片、change_parser 与 upload_info 的退出合同保持。
旧 `POST /agents_openai/{agent_id}/chat/completions` 也已在 Agent REST 合并时退出；
当前调用使用 `POST /agents/chat/completions`，body 传 `agent_id`、`openai-compatible=true`
和 `messages`。旧路径返回 404，不运行 Agent。

## 文档与分块更新

文档更新使用 `PATCH /datasets/{dataset_id}/documents/{document_id}`，分块更新使用
`PATCH /datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}`。
FastAPI 直接按名称绑定路径、请求体和鉴权依赖，没有 Quart 位置参数兼容转发层。
Web 的 parser/metadata/chunk 客户端及 Agent Hub 的 MultiRAG 适配器均使用 PATCH，
两条更新路径的 PUT 返回 405，不因旧测试或过时示例恢复别名。

文档读取使用专用的 `/documents/{doc_id}/preview` 与 `/documents/{doc_id}/download`；
`/v1/document/get/{doc_id}` 已退出。预览按当前可见性校验，下载继续要求数据集 owner/admin，
授权实现见[文档读取服务](../../db/services/document_file_service.py)。
更新、下载和预览的授权策略各自保留，不通过兼容入口绕过。
生成附件使用 `/documents/runtime/{attachment_id}/download`，与原始文档下载保持资源分离；
不把原始文档重定向到 Agent 生成附件命名空间。

## 数据集检索

`POST /datasets/search` 接收非空 `dataset_ids` 和 `question`，返回 REST `code/data` 信封，
`data` 含 `chunks`、检索总数 `total`、`doc_aggs` 和 `labels`。JWT 与个人 API Key
共用数据集成员访问校验。路径入口 `POST /datasets/{id}/search` 继续服务 Python 管理 CLI，
省略列表时默认检索路径数据集；传完整
`dataset_ids` 列表，必须包含路径 ID，所有选中数据集均须可访问且 embedding 一致。
`tenant_rerank_id` 接收整数或 null；指定模型须属于认证租户或所选数据集的所属租户，
在读取模型配置、元数据或执行检索前拒绝越界模型 ID。
联合检索一次完成排序、分页和文档聚合，不把每个数据集的分页结果拼接成联合结果。
检索按每个选中库的租户、名称和 ID 绑定独立索引；同一租户拥有多个库时也保留逐库对应，
并将完整 `kb_ids` 下推到存储过滤。Infinity 按索引与库 ID 的绑定查询物理表，合并候选后分页。

保留 `doc_ids`、`page/size`、`top_k`（上限 2048）、相似度/向量权重、
`search_mode`（sparse/dense/hybrid/fusion）、高亮、跨语言、关键词、rerank 和 KG 参数。
chunk 的 `similarity`、`vector_similarity`、`term_similarity` 语义（未计算的分量为 `null`、
排序分数可大于 1）见 [检索分数字段](../../../docs/references/http_api_reference.md#检索分数字段)。
`search_id` 指定有权访问的搜索应用时沿用其元数据配置，否则使用请求中的
`meta_data_filter`。非空 `doc_ids` 与元数据命中求交；`doc_ids=[]` 沿用未限定文档的语义。
manual 及 auto/semi_auto 生成的非空条件均保留 AND/OR 空集，条件无匹配时传递空结果哨兵，
不放宽为全文检索。未设置过滤、manual 零条件或 auto 生成零条件时沿用原有文档范围与回退。
semi_auto 的全部选中字段必须存在且有可匹配文档值；空选择、任一未知/已删除字段、
空候选、空推断条件或推断使用未选中字段均返回确定零命中，不忽略无效字段后扩大范围。
REST 检索在推断后重新读取元数据并按该快照匹配，覆盖目录读取或模型等待期间的字段删除。
`is/not is/>=/<=/!=` 与 `=/≠/≥/≤/≠` 等价。列表值的元素只允许字符串、布尔值和有限数值；
含 null、对象、嵌套列表或非有限数值时整组条件返回零命中（包括 OR），不把非法排除条件当作恒真。
`in/not in` 保留原有标量字符串匹配与合法列表行为。缺字段视为无匹配（包括否定及 empty 条件）。独立 SDK `/retrieval` 的
`document_ids` 和非空 `metadata_condition.conditions` 也求交，保留原请求及响应字段。
`use_kg` 的图谱聚合增强为库级输出：与非空 `doc_ids`、非空 `meta_data_filter` 或
`search_id` 组合时返回业务 `code=400`，在模型、元数据和图谱检索前拒绝。
SDK `/retrieval` 对 `document_ids`、非空 `metadata_condition` 与 `use_kg` 的组合也返回
业务 `code=400`。未限定文档、元数据或搜索应用时保留 KG 增强；`doc_ids=[]` 不限定文档。
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
上述索引旧接口保留 deprecated 标记。元数据配置统一使用 `/metadata/config`；
旧 `/auto_metadata` 路由与 `fields` 信封已移除，详见[模板配置合同](../../../docs/references/metadata-configuration.md)。

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

### 嵌入模型兼容性

`POST /api/v1/datasets/{dataset_id}/embedding/check` 接受 `embd_id` 和可选的
`check_num`（默认 5，严格整数 1–100）。鉴权后使用数据集 owner 的模型配置和逐库索引，
在前 1000 个启用分块中随机抽样，比较内容及 10% 标题混合向量，返回 `summary`、`results`。
未舍入的平均余弦相似度至少 0.9 才返回 `code=0`；低于门槛返回 `code=10` 并保留结果。
空样本、维度不符、非有限数或零向量返回业务错误，不能作为兼容证据。检查不写向量或数据集配置。

`PUT /api/v1/datasets/{dataset_id}` 可以在已有分块时更新 `embedding_model`，同时更新租户模型行绑定；
这不会重建已有向量。Web 设置页在模型变化且已有分块时先调用检查，失败保留表单并停止保存。
调用方直接使用 PUT 时自行决定切换策略；旧 `/datasets/{id}/embedding` 执行入口仍未注册。

`id`、`tenant_id`、`created_by` 和 `status` 由服务端管理，创建/更新不能通过旧 `ext`
对象覆盖。解析器的未知扩展保留，已知字段按服务层展开 `parser_config.ext` 后的生效值校验。
