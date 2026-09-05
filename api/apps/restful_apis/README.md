# Dataset REST API

本目录路由挂载在 `/api/v1`。数据集业务由 `api/apps/services/dataset_api_service.py`
和 `document_api_service.py` 提供。

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
