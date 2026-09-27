# Dataset REST API

本目录路由挂载在 `/api/v1`。数据集业务由 `api/apps/services/dataset_api_service.py`
和 `document_api_service.py` 提供。

图谱读取使用 `GET /datasets/{id}/graph/search`；旧 `/knowledge_graph` 读取和删除
路径继续作为 deprecated 兼容入口，删除的新路径为 `DELETE /datasets/{id}/graph`。

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
