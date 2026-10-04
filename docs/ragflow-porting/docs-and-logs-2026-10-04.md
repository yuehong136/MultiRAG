# 标题分块说明与摄取日志核对

2026-10-04，使用上游 `infiniflow/ragflow` 本地对象，冻结上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`；未 fetch、未 push。
四个指定提交均通过祖先关系检查，按完整 diff 顺序评估。后端保持 Python / FastAPI /
SQLAlchemy，前端在独立 `web` 仓。既有共享工作区修改不纳入本次提交。

## 9280c64518209a91bf39afb974305ea256ff03e7

结论：按本地能力适配文档和中英文文案；不改变分块算法或数据库。

- 新增 [Title chunker 说明](../title-chunker.md)，描述模式、正文分离、首块上下文、默认
  级别和自定义规则。没有复制上游 Token → Title 必定报错的断言，本地输入接受 `chunks`。
- Web 的 `TitleChunkerForm` 增加两种模式和开关的可见说明，补齐中英文标签，避免英文
  页面回退到中文“正则表达式”或中文页面回退到英文 Group/Rule。保留现有参数与序列化。
- 首块上下文只在本地结构化 JSON/chunks 路径执行；文本类路径提前返回。隐藏参数在模式
  切换后仍保留，已如实说明，不借文案任务修改保存合同。
- [数据库指南](../database-migration.md) 已由 `d4b091d8` 提供本地等价内容：`usr_ai`、
  空库建表后 stamp、存量库 Alembic upgrade、容器先尝试建表、启动无独立 skip 开关、
  连接目标及备份恢复边界。本次重新核对 bootstrap、模型升级函数、Alembic env 与容器入口；
  复用该唯一指南，不引入上游 MySQL/Peewee 脚本、表数量或 v0.25 升级要求。
- 冻结范围内 `7f85a5776` 重组上游用户指南，`0ae1c0aef` 调整后续文案；不将文档路径删除
  误判为本地 Title chunker 能力撤回。

验证：已有 Python 分块及工具入口定向单测 **12 passed**；只读 `alembic heads` 返回单
head `b0d2e4f6a8c0`。它仅证明当前迁移文件图，不代表实际库 schema；没有运行迁移、
备份恢复或外部解析服务。纯文档不触发 Python `make verify` / integration / smoke。
Web 提交：`043c64e`。`npm run lint`、`lint:i18n-agent`、`lint:file-size`、
`lint:typed`、`typecheck:agent-strict`、`build` 均 exit=0；Agent T1 合同测试 **74 passed**。
Lint 有既有 warning，build 有大包 warning，未放宽门禁。pre-commit 正常通过。

实际浏览器验收使用临时页面挂载真实 `TitleChunkerForm` 与 `LogTable`，日志为合成 fixture，
没有后端写入。1280×720 下中英 × 明暗四组合显示正常；键盘 Space 切换正文分离开关、
Group 模式隐藏两个开关并显示分组说明、H5 可选均已读回，控制台无 warning/error。
截图先组成带文件名、路由、视口的 contact sheet 后审阅，临时入口已删除。该证据不代表
生产业务页的真实日志请求、流程保存或完整解析入库 E2E。

## 1692f0928ff2bade2f553e010653064bc0d9cbf7

结论：已等价，无需改共享文案或日志组件。Web 当前
`src/pages/knowledge/logs/LogTable.tsx` 表头使用独立 key
`knowledge.logs.table.pipeline`；对应 `locales/en-US/knowledge-logs.ts` 为 `Pipeline`，
`locales/zh-CN/knowledge-logs.ts` 为“数据管道”。两者都是标题，不使用设置界面的
pipeline 说明文本，符合当前产品用语。

完整上游 diff 还删除了列表转换时的 `console.log`；本地日志页面和列表 hook 没有对应
调试输出，无需机械增删。后续 `0ae1c0aef` 继续区分说明与标题，不影响本地结论。

验证：静态追踪表头到实际 locale；前述真实 LogTable 合成数据页已在中英和明暗主题下
渲染，表头分别读回 `Pipeline` / “数据管道”，未泄漏说明文本或 key。没有修改日志请求、
分页、操作和 pipeline 名称显示。仅提交核对结论，不新增无行为差量的业务代码；真实 API
日志加载未在本项验收。

## c4d0b0ebcfd87c033bd4c671e189037c11a21629

结论：已等价，保留现有来源显示。Web `log-table-row.tsx` 对缺省、空串和 `local`
使用 `MonitorUp` 图标，其他字符串直接作为 React 文本显示，没有访问
`dataSourceInfo[source_from].icon` 这类不安全的映射。未知和新增来源可读且不会因查不到
配置崩溃，不替换成会丢失来源身份的统一图标。

详情 `log-detail-sections.tsx` 同样以 `source_from || 'local'` 回退，来源文本由 React
渲染。`source_from` 的 API 类型为可选字符串；这里的等价结论覆盖协议内新增字符串和
缺省值，不把任意对象等协议外载荷视作合法来源。

验证：真实 LogTable 的合成行覆盖 `local`、空串、缺省、`s3`、`gmail`、
`future_connector`、`knowledge_graph`，中英文和明暗主题下均完成渲染，控制台无错误；
已有字符串显示与本地上传图标保留。无需修改组件或重复增加测试框架。
后续上游 `7c0584a2b` 修复图谱图标、`0cfa30087` 增加 wiki 图标属独立展示扩展，
本项不引入上游图标注册表。仅提交结论；未连接外部连接器产生真实摄取日志。
