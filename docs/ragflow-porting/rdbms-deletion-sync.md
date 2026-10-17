# RDBMS 查询、游标与删除同步

对照上游 `911671cef0810835207de4e4a7c68157c2c476c5` 完整四文件 diff，冻结上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`，未 fetch。后修 `5fc254eb2` 的等值
游标重读已采用；`8abe627e6` 的自动内容列与本地可选表单一致，予以适配。MSSQL、
自定义文件扩展名和无关 BigQuery 能力没有本项活动入口，未扩展这些产品范围。

SQL 在派生表外加条件，保留原查询的 WHERE/ORDER/LIMIT/CTE 范围；列名按方言引用，
游标通过 DB-API 参数绑定，不复制上游手工 SQL 值拼接。每次读操作有独立连接、只读
REPEATABLE READ 事务、UTC session 和超时，正常耗尽、失败及 generator 关闭均释放。
正文/slim 仅读配置的索引资格字段，共用行校验、非空内容资格、ID 和类型渲染；不存在跳过坏行后发布完整快照。

保留普通 ID 的历史 `DatabaseType.<type>:<database>:<value>` 格式；未配置 id_column
或 ID 为 NULL 时，使用同一正文内容的 MD5。二进制/内存视图以稳定 base64 表示，避免
内存地址进入 hash、标题、元数据或 ID；JSON 中的 Decimal/date/bytes/UUID 同样可序列化。
跨表或重复 ID 指向不同记录时明确拒绝，要求配置有唯一 ID 的查询，不静默覆盖记录。
同标题文档的元数据现在按实际上传文档 ID 匹配，写入失败阻止任务完成和游标推进。

数字、Decimal、datetime、date、bytes、UUID 等游标按 JSON 类型标记精确保留；datetime
规范到 UTC。每个 KB 独立保存 `rdbms_sync_scopes`，不再将 generic poll_range_start
的墙钟时间当数据库游标。无旧状态、首次或重建从完整范围开始，重建不借其他 KB 的游标；
任务失败、取消或删除失败不推进。状态与 task done、下一调度在同一事务提交，行锁合并
多 KB 状态；客户端配置保存保留 worker 状态，不能伪造或清空，未知配置键仍保留。
主机、账户、数据库、查询和列映射变化要求受影响 KB 重建，密码轮换不改变范围签名。

显式查询在抓取前捕获源端上界，等值边界重读，数值游标回退时完整重读，避免空窗口锁死。
字符串排序取决于数据库 collation，因此采用完整有界重读。自动发现表每次完整读取，
不把多表不同序列/新增表混进同一个全局增量游标。源端更新仍须按其游标约定更新；不保证
发现任意低于已保存数值游标的原地修改。NULL 游标、缺列和身份歧义失败关闭。

删除清单始终读取完整配置查询，绝不使用增量窗口。普通行、全部分页/表成功耗尽后，
统一协调器才交给删除服务；成功空快照可以删除最后一条。首次、重建、开关关闭不删除。
本地已有 `file_list is not None`、上传失败禁止删除、本轮新 ID 保留、KB/连接器归属
与历史 ID 协调机制，本项继续复用；上游对 SeaFile/WebDAV 的日志删减及失败后继续分支
没有引入。PostgreSQL 发现使用不隐藏失权表的 pg_catalog，SELECT 失权即失败；MySQL
自动发现要求明确的全库/数据库级 SELECT 且无部分 revoke，角色/表级权限可改用显式
查询，不将权限过滤后的 SHOW TABLES 误当完整清单。

2026-10-17 改动前只读调查当前配置 PostgreSQL：mysql/postgresql 连接器、对应文档均
为 0，证据 `/tmp/multirag-ports-6-8-14-20261017/rdbms-inventory.json`。本环境无需
ID 迁移；其他部署不能据此推断无存量，尤其旧 NULL/binary/hash ID 应先调查再做受控
重建。旧任务无本地新状态时会保守完整重读，未猜测上游未在本地使用的全局游标。

本 owner 源码在 `9582bdb80f1a1be4505f4061d1abe71aa4cac63a` 隔离副本上实现和验证；
旧批完整 integration 在另一独立 worktree 中持续运行，未打断、替换输入或修改其服务。
证据根目录 `/tmp/multirag-ports-6-8-14-20261017/`：

- 完整 `make verify` exit=0：Ruff、8 个分层契约、async DB 门禁、mypy 144 文件、
  unit 6293 passed（`rdbms-verify-publish.log`）。首次隔离缺 OCR 资产曾在收集期触发
  下载，已停止本 owner 的收集进程，引用已有本机资产并以 HF_HUB_OFFLINE=1 复跑。
- PostgreSQL 实源 + 真实目标 PostgreSQL/MinIO/Milvus/Redis，10 passed，无 skip；
  数字游标、查询范围、元数据同名隔离、重启重读、游标回退、删除最后一条、成功空结果、
  部分读取/SQL/上传失败、配置保存、并发 KB、事务回滚及权限失效均有读回。
  最终 `rdbms-pg-publish.log` 亦包含二进制/hash/字符串游标专项与四轮最后一条删除读回。
  早期 fixture 无密码与字面状态值错误均已修复后复跑，未放宽断言。
- Web 表单 11 passed，完整 lint/build exit=0（已有 warnings）；文件体积门禁通过，
  使用只读 Git inventory 绑定隔离工作树。MySQL/PostgreSQL × 明暗 Chromium 4 passed：
  真实 PATCH 业务码、独立 GET/SQL、未知键保留、重载、键盘及无横向溢出；首屏与删除
  开关区域 contact sheet 已审阅。Google Fonts 采用受控系统字体回退，无业务 API mock。
  自有 Vite 进程组销毁后端口关闭读回为 true。

MySQL 实源未验收：官方 tag 两次和相同 arm64 manifest digest 的缓存镜像一次，均因
Docker content-store blob/attestation 提交缺失文件而失败（exit=127）。没有重启 Docker
或清理共享缓存，以免影响旧批 gate；目标容器未创建，临时凭证已清理，证据见
`mysql-container-start.log`、`mysql-digest-start.log` 和资源清理清单。没有把 PostgreSQL、
模拟 SQL 或浏览器表单检查计作 MySQL 实源成功。未运行真实解析/embedding worker、生产库迁移或部署，不 push。
