# 任务日志长度配置

对照上游 `1d114f034bf8783ae52f048e5ee5c1053d711a22` 的完整五行 diff；冻结上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`，未 fetch。后修 `ff163764b` 的 NULL
日志保护本地已有，保留；知识编译计数、embedding 状态传播等独立合同不在本项扩展。

采用 `TASK_MAX_LOG_LENGTH` 环境变量，在 worker 进程导入时读取，默认 3000 个 Python
字符。正整数有效，超过 61440 时钳制；零、负数、非整数和空字符串警告后回退默认值，
不在警告中回显原始环境值。无需新增共享 AppConfig 字段或修改依赖。运行中改环境需
重新启动对应进程，本次未重启常驻服务。

上游机制使固定上限可配置，但没有解决本地同样存在的超长无换行消息不截断，以及 UTF-8
字符数不等于字节数的问题。当前保留有界尾部、尽量保留完整行；单行超长时也会截断，
不截断 UTF-8 编码。另设 60 KiB 字节预算，为 MySQL TEXT 的 65535 字节上限及状态/
取消附加提示留余量。任务接收提示使用同一规则；多任务投影到 Document.progress_msg
时也受 60 KiB 容量保护，避免单条 Task 合法而聚合后溢出。既有短日志、换行、进度单调
和失败恢复规则保留，NULL 子任务日志可参与投影。

原 macOS 分支没有锁，其他平台 advisory lock 又在调用方提交前释放；加之 Session
identity map 可返回旧对象，并发追加会覆盖消息。现在使用事务行锁与 populate_existing
读取当前值，直到调用方 commit/rollback 才释放，不再需要重复 advisory lock 或平台分支。
取消 SQL 谓词保留；相关旧读取消用例改为真实 SQL 锁前屏障并保留缓存对象，继续验证
取消先提交后旧 worker 不得覆盖。未改任务调度/队列协议或重建整个任务系统。

2026-10-17 对当前配置库只读核对，Task.progress_msg 实列为 PostgreSQL text、无字符
长度上限；MySQL 当前模型编译为 TEXT（不是实库验收）。证据
`/tmp/multirag-ports-6-8-14-20261017/task-log-schema.json`。未迁移列或批量改写旧行；
旧日志仅在后续追加/文档投影时受限制。

验证基于本 owner 隔离源码，原始基线 `9582bdb80f1a1be4505f4061d1abe71aa4cac63a`
叠加编号 6 与本项，未改旧批验证输入：

- `make verify` exit=0：Ruff、8 项依赖契约、async DB 门禁、mypy 144 文件、unit
  6314 passed，见 `log-limit-verify.log`。定向日志/取消单测 41 passed。
- PostgreSQL scratch 的数据库专项 3 passed，补正 fixture Tenant 必填字段后，接收/
  聚合受影响单项另 1 passed；无 skip。两种 MACOS 环境各 12 并发写者、已缓存旧对象、
  事务外读不到未提交值、回滚、取消 marker、普通日志/进度、中文 emoji 长单行均读回。
  octet_length 精确为 61440；多任务 Document 投影为 40040 字节。见
  `log-limit-integration.log`、`log-projection-recheck.log`、`log-evidence/*.json`。
- 真实 HTTP/SQL/Redis 取消旧读竞争与 worker 回调 2 passed；无取消 marker 的普通失败
  恢复另 1 passed，见 `log-cancel-regression.log`、`log-retry-regression.log`。未削弱断言。
- 数据库专项仅要求 PostgreSQL，按已审查依赖登记到 db 组，不为普通日志检查启动全量
  存储套件；所有用例使用隔离资源，结束后校验 scratch 库不存在，清理读回见总清单。

MySQL 物理库仍因本机 Docker blob 提交失败不可供给，未将模型编译或字节单测当成实库
通过；未验证真实 provider/解析 worker，也未部署或 push。
