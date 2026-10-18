# 文件关联知识库的授权核对

本项对照 RAGFlow `65477519366adc9dff1368aa8dc368889703d659`，
上游冻结上限为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
MultiRAG 核对基线为 `db517cd394b33774bf1d1b49f0dc8560303eb00a`。

## 结论与机制比较

生产实现已覆盖上游授权修复，本项保留生产代码并补充真实 HTTP 回归证据。
当前合同由 [HTTP API 参考](../references/http_api_reference.md#文件关联数据集) 维护。

| 上游改动 | 当前实现与处置 |
| --- | --- |
| 展开目录后，逐个文件调用 `check_file_team_permission` | `file2document_api.py` 在调度前批量加载展开文件，逐项检查存在性和权限；另检查原始目录本身，并对重复展开的文件去重。保留本地实现。 |
| 逐个目标知识库调用 `check_kb_team_permission` | 使用同一同步 facade session 加载知识库并调用现有权限 helper；共享知识库只接受有效团队的 normal/admin 成员。无需另建权限框架。 |
| 检查失败时不调用 `run_in_executor` | FastAPI 的 `BackgroundTasks.add_task` 位于完整 `_collect` 返回成功之后。所有原始文件、展开文件和目标知识库先检查；replace 还先检查待移除来源知识库。 |
| 新增请求成功、拒绝及查找失败日志 | 上游日志包含整批文件及知识库 ID；不引入这些敏感列表。后台异常已有 `logging.exception`，HTTP/SQL/后台边界测试补足本项可验证性。 |
| 替换模块的单元测试桩 | 复用现有真实注册应用及隔离 SQL fixture，不移植上游 `sys.modules` 整包替换。新增用例观察实际调度并执行真实转换器。 |

原始文件和 replace 来源检查由本地 `2d336be5f46dcd308b67d3c4acd28541eae42a43`
引入，展开文件及目标授权已在 `49a58d0fc3bd7e4ae1e21e2c7ef99c3e77b6a12b`
的 REST 迁移中实现。
上游后续 `e65bac238e198eb6c9468bf5bd0bd016bb8d2252`、
`55d5879622c9d2d23ee7788eb8b17c7e0fd99216`、
`b4a3ff3c64fd61bf9a07b31e7b7f01ccb6e5b55a` 调整了保留已有关联、add/replace
以及保留仍选中文档的行为；当前转换器已经包含这些同问题链行为。
本项没有追随冻结上限中的 Python 删除或引入无关功能。

## 实际调用与事务边界

`api/apps/__init__.py` 动态注册
`api/apps/restful_apis/file2document_api.py`，对外入口为
`POST /api/v1/files/link-to-datasets`。`async_current_tenant_id` 在同一异步请求
session 中验证 Web JWT 或 API Key，再由 `run_sync(_collect)` 执行现有同步权限链。
SDK 墓碑文件不再重复注册此入口。独立 Web 基线
`6ad9577e3d76bdd91d2df9915b6e0c21e447f5ac` 没有调用此关联入口；本项不需要改 Web。

后台 `convert_files_with_new_session` 自开 session，调用真实 `convert_files`。
add 仅创建缺失文件/知识库对；replace 仅删除不再选中的文档。
删除通过 `DocumentService.remove_document(strict=True)` 执行 SQL 删除、任务取消和
对象/索引清理。创建文档与映射沿现有 service 各自提交；这是后台调度合同，
并非整批原子事务。后台出错会记录异常，客户端的调度成功响应不能证明转换已完成。
本项没有扩大为事务或任务系统重构。

## 回归证据范围

[集成用例](../../tests/integration/test_file_link_contract.py) 使用真实 HTTP 注册路由、
真实 API Key 身份及权限 helper、随机 PostgreSQL scratch 库、独占 MinIO 桶和 Redis key。
仅将后台连接绑定到 scratch 库，将存储和队列命名绑定到本测试资源；调度及转换器的观察
wrapper 继续调用原实现。

- 在 add/replace 下拒绝混合授权文件、含两层目录的其他租户叶子文件、无权原始目录
  下的自有叶子文件，以及混合授权目标知识库。
- 拒绝拥有文件或通过团队知识库取得文件访问权、但无权删除另一来源知识库的 replace；
  add 可保留无权旧关联并增加有权目标，之后 replace 仍拒绝。
- 每次拒绝均断言未增加后台任务；独立连接读回 SQL 全列及 xmin，比较文件、知识库、
  文档、映射和任务；独立 MinIO 客户端比较对象字节；读回 Redis 流及取消标记。
- 成功路径执行真实转换器，覆盖目录展开去重、团队文件访问、add 保留既有文件/文档/映射
  并增加目标知识库文档计数，及获得来源
  权限后的 replace 清理文档、任务及缩略图，同时保留源文件字节和仍选中的文档。
- 资源清理在创建前注册，读回确认 SQL 行、独占桶及 Redis key 删除；测试框架释放 scratch 库。

本线定向报告与源码绑定由批次外置交接索引保存。全批最终组合门禁由集成线执行。
验证不包含生产环境、真实云模型、权限在预检与后台执行之间并发撤销，或转换中途故障的
整批回滚保证；这些行为不能由本项拒绝零副作用证据推出。
