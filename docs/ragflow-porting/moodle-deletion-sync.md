# Moodle 完整快照与删除同步

对照 `59bb184e63e47b7cb706a104f8cc3096bca02821` 的完整 diff；后续演进只查到冻结
`519e7d98a5651564d4e35d6648f006cba4baaf4f`，未 fetch。Python 使用当前严格完整快照
协调器，Web 使用独立仓的设置页。未扩展元数据后端、主路由或 Turkish locale。

## 行为与上游比较

`MoodleConnector` 实现 slim 接口，Moodle 加入严格删除来源，Web 开关默认关闭。
正文与 slim 共用全部课程、section、module 遍历、索引资格和 `moodle_<type>_<module_id>`
标识。支持 resource、page、forum、assign、quiz、book；不索引的空内容/无描述活动、
无文件页面、无讨论论坛以及不支持的 module 同时从两条链排除。ID 保持原有格式，
无需 ID 迁移。slim 不下载文件，不构建正文；论坛为判断资格读取讨论列表。

上游按类型生成 slim ID、逐课程捕获异常后继续，可能把部分读取当作权威结果，并保留
实际无法索引的空 module。本地从 SDK 原始 REST `post` 读回并校验列表，避免 SDK
将 `None` 静默转成 `[]`。课程枚举/任一课程读取、错误结构、重复 ID、论坛权限警告
均失败。只有成功枚举全部课程才完成快照；成功空列表可删除最后一条。正文下载、
任一 book 章节下载、上传失败也不删旧数据、不推进成功时间，不生成部分 book。

论坛调用显式 `page=-1, perpage=0`，使用 Moodle REST 的全部讨论约定，检查 warnings；
官方实现见 [Moodle forum externallib](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/mod/forum/externallib.php)。
单 module 的讨论结果在当前遍历内复用，下一次遍历重读；增量判断包含讨论时间，
已知时间使用 `(start, end]`，无可用时间的 module 保守重读。文件使用配置服务器同源 URL，
query token 统一替换，拒绝把凭据转发到其他 origin；异常只记录类型，避免 URL/token
回显。外部仓储直链需先确认 Moodle 是否可提供同源下载入口。

冻结范围后修 `73061944d` 修复富文本中仅含空白的 inline 元素丢失词间空格。本机
`First<b> </b>Last` 原来确实输出 `FirstLast`；采用上游有类型的 converter helper，
本次仅接入 Moodle 的论坛、活动、book 正文，未扩大其他解析器范围。
`b85b908bb` 的 factory 结构不替换本地活动协调器；删除 Python 的后续架构不适用。

## Web 的等价部分

原提交 Chat/Search `reference_metadata` diff 是格式化及未用 import 清理，公式测试仅
补文件末尾换行，没有新增公式行为。本地 Chat 和 Search 已共用
`src/components/chat/reference-metadata-settings.tsx`：共享 key 查询、保存字段选择、
区分 null/省略（全部）与 []（显式无字段）、保留远端暂缺的选项，查询失败不会清空选择。
上游 effect 会按当前 key 列表裁剪并在关闭时清空；本地避免部分 key 读取消坏已存配置，
因此保留当前实现。现有 reference-metadata 与 latex-rendering 测试均在此次完整 CI 通过。
本项 Web 可执行改动只有 Moodle 删除开关及其来源合同测试。

## 本次验证与限制

隔离源码基线 `9582bdb80f1a1be4505f4061d1abe71aa4cac63a` 加编号 6、8、14 本 owner
文件；不替换旧批验证源码，不重启共享服务或改工具链。
证据根 `/tmp/multirag-ports-6-8-14-20261017/`：

- `moodle-unit.log`：24 passed，覆盖全部课程、六类资格/ID、空结果、空响应、后续课程
  失败、论坛 warnings、文件/章节失败、分页参数、时间边界及 styled whitespace。
- `moodle-verify.log`：make verify exit=0，unit 6339 passed；Ruff、8 项 import 合同、
  async DB 门禁、mypy 145 文件通过。
- `moodle-integration.log`：9 passed，无 skip。Moodle REST/下载为控制边界；实际
  PostgreSQL scratch、Redis、MinIO 与 Milvus，独立读回 Document/File 关联、KB 数量、
  对象列表及 Strong 一致性向量结果。含成功/成功空、枚举/后续课程/结构/下载/上传失败、
  关闭开关及真实 HTTP 业务码/配置 SQL 读回。
- `moodle-browser.log`：明暗两例 passed；真实 Web 键盘操作、PATCH 业务码、GET 与独立
  SQL 读回、重载、未知配置保留及溢出检查。查看带文件名/路由/视口的
  `moodle-web/contact-sheet.jpg`；开关可见，布局正常。CDN 字体为受控系统字体回退。
- Web 完整 `test:ci`：source node 771、Vitest 593、desktop 81、tooling 10 passed；
  lint（零错误，有已有警告）、build、file-size 均 exit=0。归档源码使用只读 Git inventory
  绑定；最初缺 `.git` 的运行失败已纠正，不作为通过证据。
- `resource-readback.json`：只读复查全部留存证据中的 11 个随机数据库、29 个随机源
  角色均不存在；60 条证据记录，11 个 HTTP listener 关闭，Moodle fixture 对象桶已删除，
  基础 SQL 清理已读回。每例随机向量 collection 的删除由 fixture teardown 断言确认。

未接真实 Moodle SaaS，未证明特定租户插件、权限配置或外部文件仓储；受控 REST 响应
不等于 SaaS 验收。未跑解析/embedding worker、未部署、未 push。MySQL 实源限制见
[RDBMS 说明](rdbms-deletion-sync.md)，本项目标持久化实测为 PostgreSQL。
