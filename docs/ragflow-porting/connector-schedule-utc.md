# 连接器调度间隔与 UTC 存储

对照上游 `5e01feb755920dcc291df169aa713ffdc3e76b55` 的完整两文件 diff，冻结上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`，未 fetch。冻结区间的 dashboard、多 KB、
授权、跨 KB ID 和排序后续改动没有撤回时间比较思路；Python 移除前仍使用
`NOW() AT TIME ZONE TIMEZONE - make_interval(...)`。本项只处理当前 SQLAlchemy 调度/
时间问题，不引入相邻的独立 prune task、排序或其他数据库产品。

上游的改进点是将数据库当前时刻与无偏移存储时间置于同一时区，并修正间隔表达式。
本地应用时钟、服务写入、毫秒 epoch 与同步 checkpoint 已采用 UTC，`poll_range_*` 和
`time_started` 已为 timezone-aware 列；这部分等价，保留现状，不新增默认北京时间的
TIMEZONE 环境变量。原 PostgreSQL 拼接整数与字符串再转 INTERVAL 在正确时区下算术
可用，但受 session 时区影响，且依赖 DB_TYPE 环境变量选语法。原 MySQL
TIMESTAMPDIFF(MINUTE) 会截断秒数，导致超过阈值一秒仍不调度。

当前按 session 实际绑定的方言选择 typed SQLAlchemy 表达式：PostgreSQL UTC now
减 make_interval 的整数分钟参数，MySQL UTC_TIMESTAMP(6) 经 TIMESTAMPADD 减整数
分钟，统一严格 `update_date < cutoff`。阈值恰好相等不调度，超过一秒即调度，无动态
SQL/配置字符串插值。未知方言明确失败，不误用 MySQL 语法。

连接工厂给 PostgreSQL libpq 设置 `timezone=UTC`，同步和 psycopg 异步共享参数；
MySQL 初始化 session 为 `+00:00`，支持带 driver 后缀的配置。未修改全局服务器时区。
BaseModel 的无偏移 DateTime 保持 UTC wall-clock 合同，未改列类型；同步 checkpoint
仍是 aware UTC。MySQL DATETIME 返回的无偏移游标在服务读出时显式设为 UTC，避免
worker 的 astimezone 按本机时区解释。日志 `update_date` 同样返回带 UTC 偏移的时间，
Web 的日志显示和预计下一次调度（new Date）可正确解析，无需改其表单或时区设置。

2026-10-07 对当前配置 PostgreSQL 的只读检查发现：未配置连接的 session 时区为
Asia/Shanghai，连接器和同步日志两表均为空。证据为
`/tmp/multirag-connector-ports-20261007/schedule-existing-time-audit.json`。
本环境这两表无需历史修正；其他表或其他部署的历史无偏移日期未回写、未推断其原始
时区。若部署有旧日志，应先以对应 epoch 与日期核对，不能按 TZ 盲目全库平移。

验证证据在 `/tmp/multirag-connector-ports-20261007/`：

- 定向单测 52 passed；实际共享源码的本 owner 三项单测合跑 86 passed。
- 隔离源码完整 `make verify` 的 Ruff、8 项依赖契约、async DB 门禁、mypy 143 文件和
  unit 6162 passed，exit=0（最终 `schedule-verify-final.log`）。源码基于
  `ea6b7dca67f7e120e45adaabe2af1624a48fa54e`，仅叠加本 owner 三项变更；共享目录的
  其他 owner 修改未包含在该门禁结论中。
- 真实 PostgreSQL 17.11 scratch：调度边界/状态过滤、实际方言而非误导 DB_TYPE、同步
  持久化、异步 UTC 默认、已有 checkpoint 原子性回归 4 passed，无 skip。最后补充的
  真实日志 GET 业务码/UTC 字符串与独立 SQL 读回另 1 passed。测试首次遗漏嵌套 fixture
  注册，补正后复跑该 HTTP 项，无生产代码修补。报告见 `schedule-pg-final.log` 和
  `schedule-http-final.log`；`pg-schedule/*.json` 保存 UTC/+08 session 的独立读回，
  测试退出后以 pg_database 再查，记录的两次 scratch 库均不存在。
- 真实 MySQL 8.4.11/PyMySQL：自有随机库中运行生产查询/调度服务，精确阈值前一秒、
  相等、后一秒，零间隔、取消 connector/task、event 源、日志 bypass、非 UTC 输入
  checkpoint 与 UTC ORM/service 写入均通过。UTC 和 +08 session 两次读回一致；
  四表所有 owned rows 为 0，DROP 后 information_schema 中库不存在，全局时区仍 SYSTEM。
  证据 `mysql-schedule-readback-final.json`；私有凭证临时文件已删除。

MySQL 验收仅 materialize 四张调度相关表，将 PostgreSQL JSONB 映射为 MySQL JSON，
日期为 DATETIME(6)，使用 schema_translate_map 指向自有库；未改生产 metadata。
这证明真实 MySQL 查询/session/日期行为，不代表现有整库 PostgreSQL 专用 DDL/Alembic、
完整 MySQL API 或 worker 已验收。MySQL 异步驱动当前未实现，OceanBase/MariaDB 及其他
版本未实测，未扩大此次迁移。

额外启动的共享目录全 core integration 在 360 passed 后主动中止（exit=2），不作为
全套通过证据；本项必要的聚焦 integration 均完整结束且无 skip。runner 已退出，记录的
10 个自有 scratch 数据库 manifest 均标记 absent；该额外广泛运行未形成全链完成结论。
未重启常驻服务、未部署、不 push。
