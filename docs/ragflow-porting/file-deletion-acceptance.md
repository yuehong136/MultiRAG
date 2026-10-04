# 文件与 Dataset 删除验收（2026-10-04）

上游 checkout `/Users/xldu/project/ragflow`，remote 为 `infiniflow/ragflow`；
冻结上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，两项提交均在该上限内。
逐项使用目标 SHA diff，不使用冻结上限中后来的 Python 退役实现替换当前生产 FastAPI。
稳定消费者行为见 [删除合同](../references/file-deletion.md)。

## 47129fdd0832d2631613bef061c67824d46c8b7c

实现提交 `bdbe93e0a786d147c4142e458aa43be19f063a43`。
保留本仓异步鉴权、线程内独立 session 和服务分层；成功/部分失败均保留统计载荷。
必要的本地修正包括底层存储异常传播、删除行数与残余关系检查、文档清理严格结果、
全部关联文档预鉴权、递归子项权限与失败父目录保留、重复及父子重叠去重。
上游“记录已删但底层清理失败仍计数”没有直接照搬；KB 来源文件改为显式拒绝。

独立 Web 当前没有 `/files` 消费者或通用文件页面，未新增无调用方的页面/API/hook。
刷新、错误反馈和分页要求已写入合同，交由 Skills Space 后续消费者实现；浏览器行为
不列作已验收。CLI FileProvider 当前只实现列表/读文件等能力，注释提及 DELETE
不代表有删除调用；本次无需修改 Go 并行服务或 CLI。

- `make verify` 首次通过：lint、8 条依赖合同、异步门禁、mypy 137 文件、5200 单测。
- 随后关联文档 ID 去重及补充预鉴权用例的定向测试：17 passed。
- 真实 HTTP 批删验收：缺失/越权/重复/递归/对象存储故障混合批次，返回 102、
  success_count=4；独立 PG/MinIO/Milvus 读回合法项、关联文档/关系/计数/索引删除，
  失败子项/父目录保留；解除故障后重试 0、success_count=2。
- `make smoke` 在已有 8123 服务通过；变更端点则由加载本次代码的隔离 HTTP 服务验证。

## 06c6da5d94d530d82025d08ab2d14216f91b41d3

生产代码完整等价，未重复修改。依据为既有提交
`b6e413e995997b77fd3244428c4f8ccfe382ef21` 中
`api/apps/restful_apis/document_api.py:delete_documents`：访问检查、ids/delete_all
互斥、整批 dataset ID 校验均在 `FileService.delete_docs` 前；delete_all 只取本 dataset。

本项只增加真实集成验证及合同说明。路由既有单测 9 passed；新增真实 JWT HTTP 验收：

- 合法/不存在 ID 的两种顺序、同 owner 跨 dataset、跨 owner、有效或无效 ids 与
  delete_all 同时提供、对无权限 dataset 的 delete_all、空请求，共 8 类拒绝。
- 每次拒绝返回 HTTP 200/code 102，并以新连接读回全部 SQL 列（含文档/关系/任务/
  计数/元数据）、物理对象字节、Milvus 完整 payload/向量及专用 Redis 队列，快照完全一致。
- 合法 delete_all 返回 deleted=1：目标记录/关系/元数据/任务/对象/索引清除，其他
  dataset 的文档、文件、计数、对象和索引保留；随后空 dataset delete_all 为 deleted=0。

两项集成最终同跑 `make integration TESTS='tests/integration/test_file_batch_delete.py
 tests/integration/test_document_delete_scope.py'`：**2 passed，零 skip，退出码 0**。
本机机器可读报告 `.test-results/20261004-205718-54857/`；实际响应与快照分别在
`.test-results/file-deletion-acceptance-20261004/file-batch-delete.readback.json`、
`document-delete-scope.readback.json`；这些是忽略入库的本机证据。

## 验证限制

最终全仓 `make verify` 复验在格式检查停止：并行 Skills 工作的
`api/db/db_models.py`、`api/skills/package.py` 当时需要格式化。本任务保留这些改动，
不把先前通过视作后续工作区全绿。新集成代码的 Ruff 检查通过；全仓门禁的后续状态
以运行当时结果为准。随后 `make -k typecheck test` 的 mypy 仍通过；单测 5378 passed、
1 failed，失败为 Skills WIP 新外键引入的
`test_db_table_creation_order.py::test_parent_tables_are_created_before_their_children`，
与本次删除路径无关，已交接 Skills owner。两个集成资源 manifest 均确认 cleanup=true、
桶/集合/队列/监听关闭且所有自有行计数为 0。

没有运行完整集成矩阵；实际后端为 PostgreSQL/MinIO/Milvus，
未做其他云对象后端、MySQL、生产部署或 Web 浏览器验收。

跨存储失败可能已有部分提交，无持久补偿/清理账本；Skills owner 必须按合同做独立
状态与索引协调。未 push，其他任务改动保留。
