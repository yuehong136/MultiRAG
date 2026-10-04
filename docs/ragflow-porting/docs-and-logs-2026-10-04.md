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

## d4147efc66688d2118f17bf1d867bf64faec0752

结论：没有需要新增的本地版本发布说明，仅保留审计结论。上游此提交新增 **RAGFlow
v0.25.1** 发布记录，不能作为 MultiRAG 版本号或整版交付声明。后续 `8aaf0942b` 将
“新增连接器”更正为“同步源端删除”，并去除大 PDF 优化的重复表述，本次按其含义核对。

下面基于本仓已提交快照 `2ebc41b2` 核对；未提交的连接器/配置等并行改动不作为已交付依据。

| 上游发布条目 | 本地证据与采用边界 |
|---|---|
| 全部 Web API REST 化、统一创建/索引、保留兼容 | [REST API](../../api/apps/restful_apis/) 已存在；[文档旧入口](../../api/apps/document_app.py) 仍有活动路由与 deprecated 兼容路由。不能将部分完成写成全部端点迁移完成，既有逐接口账本继续有效。 |
| OpenDataLoader、Docling 路由 | [General 解析器](../../core/app/naive.py) 和 [流程 Parser](../../core/flow/parser/parser.py) 有实际路由，[解析实现](../../deepdoc/parser/) 已存在。本项只静态核对入口，未调用远程解析服务。 |
| 大 PDF 懒加载/分批降低内存 | [PDF parser](../../deepdoc/parser/pdf_parser.py) 的 `parse_into_bboxes` 按窗口加载并释放，[DeepDOC 配置](../../common/deepdoc_config.py) 默认窗口 50 页。不是所有 PDF 入口的统一保证，也没有本次内存基准；不照搬“显著降低”效果或固定大于 50 页阈值。 |
| Bitbucket、Gmail、Google Drive、Airtable 删除同步 | 已提交 [连接器 README](../../common/data_source/README.md) 明确默认关闭、完整清单、权限失败阻断和存储尽力清理等合同。沿用已有实现与验证记录，不将其写成新增四个连接器或云端实际删除已验收。 |
| DeepSeek v4 | [模型目录](../../configs/llm_factories.json) 存在 `deepseek-v4-flash/pro`，但目录可选不等于全部接口或真实供应商验收。本次不作新的模型运行能力声明。 |
| UCloud | 已提交提供商配置和 Python 模型实现中未找到 UCloud 专用接入；通用兼容入口不能替代专用支持证明，不写为完成。 |
| v0.24→v0.25 元数据升级可见性 | 本地使用 SQLAlchemy/Alembic schema 和独立版本，无对应上游版本升级声明可直接复用。元数据接口存在不足以证明所有历史数据迁移，继续引用本地数据库指南。 |
| 重复聊天输出 | [OpenAI SSE](../../api/apps/restful_apis/openai_api.py) 已将最终全文放在 `final_content`，正文走增量输出；既有逐提交记录包含对应修复。不扩大成所有聊天渠道均无重复输出的保证。 |

验证为完整 diff、冻结范围内后续修订、已提交源文件与文档路径核对。没有修改生产代码、
版本文件或模型目录；未执行生产迁移、云端连接器、真实模型、内存基准或全量聊天 E2E。
此文是适用性审计，不是发布公告；既有运行验收记录不能充当本次新增运行证据。
