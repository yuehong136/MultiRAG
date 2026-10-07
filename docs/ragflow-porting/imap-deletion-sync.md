# IMAP 邮件及附件删除同步

对照上游 `5672be0652577de7101cfcfd374e88e4e36db706` 完整三文件 diff；
冻结上限为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，未 fetch。冻结区间
后续格式化和验证调整不改变本次 ID/范围契约；上游 Python 后端整体移除不适用于本仓。

正文和 slim 共用邮件头、附件资格、MIME 文件名解码及附件 ID。缺失 Message-ID 时采用
上游的内容/原始邮件 SHA256 生成机制，避免每次 UUID 导致重复导入；无 Date 的邮件以
IMAP INTERNALDATE 判定时间，生成 ID 不含抓取时钟。有效 Message-ID 的既有邮件和
已解码附件 ID 保持不变；无效字符集按 UTF-8 替换解码，大小限制在两个路径一致。

原本已有的完整快照协调器继续负责收集、成功耗尽后才删除、上传失败禁止删除，以及本轮
新导入 ID 保留。新增 IMAP 默认关闭的删除开关。所有可选 mailbox 均枚举，非可选父级
排除；LIST/SELECT/UID SEARCH/UID FETCH 失败、非法响应、UID 不一致或后续 mailbox
失败都抛出，不能成为权威空快照。明确的空邮箱/空搜索是成功空结果。读取使用
BODY.PEEK，避免修改已读状态；连接在正文 checkpoint、快照和登录失败路径均关闭。

每个连接器/KB 的初始 `(start, end]` 下界在网络访问前持久化到 `config.imap_sync_scopes`。
后续正文使用上次成功游标，快照始终使用初始下界和同次捕获的结束时刻。无历史状态的
增量任务使用 0 作为保守清单上界集合，不猜初始范围。重建只重置目标 KB；同一重建任务
重试保持首次下界。主机、端口、用户或 mailbox 集合改变要求该 KB 重建。密码轮换和
mailbox 顺序不改变范围签名。行锁避免并发 KB 覆盖，配置保存保留 worker 状态，客户端
不能伪造或清空；未知配置键仍完整保留。

源 Date 和 INTERNALDATE 可能不同，故 SEARCH ALL 后以同一 UTC 邮件头时间精确筛选，
避免用服务器按天的 SINCE/BEFORE 把本应索引的邮件漏出快照。代价是每次快照读取所有
范围邮箱邮件和附件，未声称服务端增量清单或跨 mailbox 的一致性版本快照。

2026-10-07 在修改 ID 前，对当前配置的 PostgreSQL 做只读聚合，IMAP 连接器、关联、
日志和文档数均为 0（`/tmp/multirag-connector-ports-20261007/imap-inventory.log`）。
本环境无需迁移。不能据此推断其他部署无历史数据；其他部署若曾导入无 Message-ID
邮件，旧随机 ID 无法由新稳定 ID 逆推，应先查存量再做该 KB 的受控重建。现有本地
历史/连接器/KB 范围 ID 解析协调机制继续复用，不做盲目数据库回填。

验证证据位于 `/tmp/multirag-connector-ports-20261007/`：

- 定向单测 60 passed，覆盖全邮箱、邮件/附件一致性、稳定生成、UTC 开闭边界、资格、
  空结果、部分失败及 driver 开关/首次/重建/持久化失败。后补登录失败连接清理两例。
- 真实隔离 PostgreSQL scratch、MinIO 桶、Milvus 集合、Redis：删除用例及 HTTP 配置
  15 passed；补正测试自身的模型字段与 beartype float 参数后，重复导入/对象 bytes 读回、
  范围重启/并发/重建/配置保存两例 2 passed，无 skip。SQL 文档与关联、KB 计数、Strong
  向量查询、对象枚举/内容及 cleanup 均有断言。报告分别为隔离副本
  `.test-results/20261007-183242-92207/` 和 `imap-integration-final-two.log` 所列目录。
- Web 表单 22 passed，独立源码完整 `npm run build` exit=0；共享工作区存在其他 Studio
  类型错误，未修改。明暗 Chromium 2 passed：实际 PATCH 业务码、独立 GET/SQL 配置读回、
  重载与键盘切换通过。带文件名/路由/1440×1000 的 contact sheet 已审阅，页面显示正常；
  Vite 进程组销毁后端口读回关闭。Google Fonts 外部请求失败后仅提供系统字体 CSS，未
  mock 业务接口，未声称 CDN 验收通过。
- 隔离源码在 `ea6b7dca67f7e120e45adaabe2af1624a48fa54e` 上叠加本 owner 两项改动，
  完整 `make verify` 的 lint、依赖/async DB 门禁、mypy 和单测通过，最终 unit 6153 passed，exit=0（`imap-verify-final.log`）。

未连接真实 IMAP 邮件 SaaS、真实账户、权限、超大附件或大邮箱；受控 imaplib 响应配合
真实后端资源验证不等于外部服务器实测。MySQL 删除链未在本项运行，PostgreSQL 是本次
真实存储后端。
