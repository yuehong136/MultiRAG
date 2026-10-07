# 钉钉 AI 表格删除同步

对照 `89961962c0281560fbe9daeb4d00ef6ea4161c8a` 的完整三文件 diff；上游
`infiniflow/ragflow` 使用冻结上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，
未 fetch。冻结区间后续仅有格式化、统一验证及 Python 后端整体移除，不将整体移除应用到
仍使用 Python 后端的本仓。

正文和 slim 共用 `dingtalk_ai_table:{table_id}:{sheet_id}:{record_id}`，覆盖所有工作表
和记录分页。ID 和字段对象校验相同，空字段对象仍可索引；无效记录不会变成 `unknown`
正文或被 slim 静默跳过。分页循环、缺失响应、权限或后续工作表失败均抛出。明确的空列表
是成功空结果，完整快照协调器耗尽枚举后才交给删除服务。

上游新增 slim 能减少 blob 构建成本，集中 ID 能避免正文与删除偏离；两点已采用。记录流
同时替代正文路径整张工作表的列表缓存。本地已有完整快照、KB/连接器 ID 归属、上传失败
禁止删除、刚导入记录保留和失败重试窗口机制，继续复用；不复制上游捕获快照失败后仍完成
任务的分支。首次导入、重建或开关关闭不删除；成功空快照允许删除该 KB 中此连接器的文档。

`ListRecords` 当前客户端仅提供 ID/fields，没有源端修改时间。原正文按抓取时刻筛选，会将
抓取前记录的结束时间之后生成的所有文档排除。当前每次轮询刷新完整表格，保留抓取时刻
作为文档更新时刻，不用它筛掉记录。这也意味着轮询会重读所有记录，未声称具备按源端时间
增量或跨分页的一致性版本快照。

独立 Web 仓原来没有该数据源入口。现补齐类别、配置表单、默认值、中英文文案和默认关闭
的删除开关；使用当前 REST 连接器合同，无新增后端路由。

2026-10-07 验证：

- 定向单测 47 passed：跨工作表重复 record ID、完整分页、空表、无 blob 构建、无效 ID/fields、
  循环分页、部分失败，以及开关/首次/重建的协调行为。
- `make integration TESTS=tests/integration/test_dingtalk_deleted_sync.py INTEGRATION_WORKERS=0`：
  7 passed，无 skip。受控 SDK 边界，真实独立 PostgreSQL scratch 库、MinIO 桶、Milvus 集合，
  生产上传和删除服务；独立 SQL、Strong 向量查询、对象枚举、HTTP 业务码及配置 GET 读回。
  资源 teardown 校验通过。报告 `.test-results/20261007-181554-72418/`。
- Web 表单两文件 22 passed；`npm run lint` exit=0（已有 warnings），文件体积门禁通过。
  `npm run build` 被本批范围外 Studio `save-lifecycle.test.tsx` 的三处 Mock 类型错误阻塞。
- `make verify`：lint、8 个依赖契约、async DB 门禁、mypy 143 文件通过；单测最初
  6158 passed / 3 failed，失败均来自并行 Memory 测试参数修改，与本项文件无调用关系。
  修正后定向复查结果及浏览器验证见下文。

未使用真实钉钉租户、令牌、权限或大型表格；受控 SDK 及真实后端验收不代表 SaaS 实测。

隔离源码验证基于 `ea6b7dca67f7e120e45adaabe2af1624a48fa54e`，仅叠加本项文件，
复用已安装依赖和本机模型资源：完整 `make verify` exit=0，unit 6123 passed。
初次隔离副本缺少忽略目录中的 OCR 资源，补齐现有资源引用后复跑通过，未下载新模型。
独立 Web HEAD 加本项文件的完整 `npm run build` exit=0，证明上述共享工作区 Studio
错误没有由本项引入。

`make integration INTEGRATION_SUITE=consumer TESTS=tests/integration/test_connector_web_sync.py`
实际启动自有 Vite 和 Chromium：明暗主题 2 passed，键盘 Space 切换开关、真实 PATCH
业务码、独立 HTTP GET/SQL 对未知配置与凭证键完整读回、重载、无页面横向溢出均通过。
按文件名、路由和 1440×1000 视口标注的 contact sheet 已审阅，配置区和开关显示正常。
API scratch 资源与 Vite 整个进程组销毁；端口关闭读回为 true。
浏览器用例属于显式 consumer 套件，不为普通核心 CI 添加 Web 安装依赖。
