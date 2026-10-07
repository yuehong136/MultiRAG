# Agent 运行与接口迁移验收

本批来源为本机 `../ragflow` 的指定八项提交，冻结上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。八项均在其祖先链中；
remote 为 `infiniflow/ragflow`，本轮不 fetch。当前 `origin/main` 已超过冻结上限，
不作为本批代码基准。完整目标 diff 与冻结范围内后修已先核对，所有实现由同一 owner 串行交付。

冻结前 `670e68872` 删除 Python 后端，不视为本地 FastAPI 功能撤回。
沿真实路由检查，不恢复上游 `backward_compat.py` 或直接调用带 Depends 的 handler。

## 4 · e4aee25b4b158dcc7bee40639a1c2065a61ac732

旧 `/agents/{id}/completions` 在本地已退出；仍活动的演示模式
`MultiRAGAgentClient` 却使用该路径，已迁到当前 canonical 入口，并显式传 `agent_id`。
原 Bearer API Key、发布模式、question、session 续聊和失败分类保留。
managed Channel 使用自己的私有 execution 接口，不受本次演示客户端迁移影响。
第 12 项继续统一 plural 路径；本项不增加旧路由。

附带 footprint 均核：Chat session 更新使用 PATCH，Chunk 更新使用 PATCH；
退休 PUT 不恢复。相关 question 接口正确拼为 `chat/recommendation`，独立 SDK
`sessions/related_questions` 保持其 API-key 合同，不当成旧转发。
后修 `d3542463c` 修正文档拼写；`64bd0130d` 的兼容扩展不构成本地活动消费者证据；
`20180790e` 的 OpenAI 授权缺口在本地 `prepare_agent_run` 已有更完整的开流前授权。
本地保留固定发布快照、session 来源与调用者绑定，不采用上游 handler 转发机制。

本次证据：`test_native_client_uses_authorized_canonical_agent` 实际 loopback HTTP
调用原生客户端两轮，scratch PostgreSQL 独立读回 session 的 agent、user 和消息；
JWT/API-key 缺失、错误与跨租户均拒绝，旧入口 HTTP 404、OpenAPI 缺失且 SQL 无新增。
与 `test_chat_session_compat.py` 合跑 **3 passed**，unit 三文件 **90 passed**。
真实 SQL/Redis/HTTP 使用隔离资源并由 fixture 清理；未发真实飞书消息、未部署或 push。
集成报告：`.test-results/20261007-181629-73012`。

## 11 · a190a6d67f4e93974716515310b75417adb66599

`files/link-to-datasets` 通过 FastAPI 依赖注入执行；未发现 Web、当前 SDK、admin/CLI
使用退休 `file/convert`，该入口保持 HTTP 404/OpenAPI 缺失。SDK 路由文件的旧注释已纠正。
后修 `654775193` 的文件/数据集预检本地已有，补强为选中目录自身也需授权、展开结果稳定去重，
replace 删除来源逐一授权后才调度。`e65bac238` → `55d587962` → `b4a3ff3c6` 的有效
add/replace 链已采用：默认 replace 只删除未选中的关联，保留仍选中的文档及解析成果；
add 只添加缺失关联。重复文件/数据集 ID 不重复创建，`d8a6cacd0` 的重名处理亦采用。
`mode` 以 FastAPI 枚举 query 校验，非法值 HTTP 422、零调度。

本次真实 HTTP/SQL **1 passed**：空的跨租户目录、含越权子文件目录、跨租户目标与缺失 ID
均零写；退休入口 404；JWT/API-key 的真实认证、真实后台转换服务、scratch 新 session，
重复关联保留原文档 ID/时间/内容、add 保留旧关联、replace 删除未选中的关联且保留原文档，
独立 SQL 读回及 fixture 清理。定向 unit **18 passed**，Ruff/async 门禁/mypy 143 文件通过。
最初被其他并行 GraphRAG WIP 的导入窗口阻断，稳定后复跑通过；首轮测试对 envelope
误用 code 已修为实际 retcode。报告：`.test-results/20261007-182233-81762`。
HTTP 成功表示任务已调度，后台仍沿用错误日志合同，不承诺跨存储整批事务。
本次无解析后的向量/对象关联删除验收，不把 SQL 文档保留证明外推为完整后台解析验收。
