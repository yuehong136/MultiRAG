# Agent 运行与接口迁移验收

本批来源为本机 `../ragflow` 的指定八项提交，冻结上限
`519e7d98a5651564d4e35d6648f006cba4baaf4f`。八项均在其祖先链中；
remote 为 `infiniflow/ragflow`，本轮不 fetch。当前 `origin/main` 已超过冻结上限，
不作为本批代码基准。完整目标 diff 与冻结范围内后修已先核对，所有实现由同一 owner 串行交付。

冻结前 `670e68872` 删除 Python 后端，不视为本地 FastAPI 功能撤回。
沿真实路由检查，不恢复上游 `backward_compat.py` 或直接调用带 Depends 的 handler。

## 4 · e4aee25b4b158dcc7bee40639a1c2065a61ac732

旧 `/agents/{id}/completions` 在本地已退出；仍活动的演示模式
`MultiRAGAgentClient` 却使用该路径，已迁到当前 canonical 入口，并显式传 `agent_id`。
原 Bearer API Key、发布模式、question、session 续聊和失败分类保留。
managed Channel 使用自己的私有 execution 接口，不受本次演示客户端迁移影响。
第 12 项继续统一 plural 路径；本项不增加旧路由。

附带 footprint 均核：Chat session 更新使用 PATCH，Chunk 更新使用 PATCH；
退休 PUT 不恢复。相关 question 接口正确拼为 `chat/recommendation`，独立 SDK
`sessions/related_questions` 保持其 API-key 合同，不当成旧转发。
后修 `d3542463c` 修正文档拼写；`64bd0130d` 的兼容扩展不构成本地活动消费者证据；
`20180790e` 的 OpenAI 授权缺口在本地 `prepare_agent_run` 已有更完整的开流前授权。
本地保留固定发布快照、session 来源与调用者绑定，不采用上游 handler 转发机制。

本次证据：`test_native_client_uses_authorized_canonical_agent` 实际 loopback HTTP
调用原生客户端两轮，scratch PostgreSQL 独立读回 session 的 agent、user 和消息；
JWT/API-key 缺失、错误与跨租户均拒绝，旧入口 HTTP 404、OpenAPI 缺失且 SQL 无新增。
与 `test_chat_session_compat.py` 合跑 **3 passed**，unit 三文件 **90 passed**。
真实 SQL/Redis/HTTP 使用隔离资源并由 fixture 清理；未发真实飞书消息、未部署或 push。
集成报告：`.test-results/20261007-181629-73012`。

## 11 · a190a6d67f4e93974716515310b75417adb66599

`files/link-to-datasets` 通过 FastAPI 依赖注入执行；未发现 Web、当前 SDK、admin/CLI
使用退休 `file/convert`，该入口保持 HTTP 404/OpenAPI 缺失。SDK 路由文件的旧注释已纠正。
后修 `654775193` 的文件/数据集预检本地已有，补强为选中目录自身也需授权、展开结果稳定去重，
replace 删除来源逐一授权后才调度。`e65bac238` → `55d587962` → `b4a3ff3c6` 的有效
add/replace 链已采用：默认 replace 只删除未选中的关联，保留仍选中的文档及解析成果；
add 只添加缺失关联。重复文件/数据集 ID 不重复创建，`d8a6cacd0` 的重名处理亦采用。
`mode` 以 FastAPI 枚举 query 校验，非法值 HTTP 422、零调度。

本次真实 HTTP/SQL **1 passed**：空的跨租户目录、含越权子文件目录、跨租户目标与缺失 ID
均零写；退休入口 404；JWT/API-key 的真实认证、真实后台转换服务、scratch 新 session，
重复关联保留原文档 ID/时间/内容、add 保留旧关联、replace 删除未选中的关联且保留原文档，
独立 SQL 读回及 fixture 清理。定向 unit **18 passed**，Ruff/async 门禁/mypy 143 文件通过。
最初被其他并行 GraphRAG WIP 的导入窗口阻断，稳定后复跑通过；首轮测试对 envelope
误用 code 已修为实际 retcode。报告：`.test-results/20261007-182233-81762`。
HTTP 成功表示任务已调度，后台仍沿用错误日志合同，不承诺跨存储整批事务。
本次无解析后的向量/对象关联删除验收，不把 SQL 文档保留证明外推为完整后台解析验收。

## 12 · f32034e83e2aed71943ee9d095d4e5fe467c1121

统一 canonical `/agents/chat/completions`；Search 已有 `/searches/{id}/completions`。
独立 Web `b45f319` 将 Agent 单数调用和 Search 的 `conversation/ask` 摘要调用迁移；
原生演示客户端、runtime replica 文档、执行来源文档和全部活动 Python 验收一起迁移。
无活动消费者后 singular 与旧 Agent 路径保持 HTTP 404/OpenAPI 缺失，不增加兼容别名。
上游 `477f2fceb` 后修同样删除 singular；其“无消费者”注释未作为本地退出依据。

admin/CLI `chat_on_session` 已使用 canonical `/chat/completions` 和 chat_id/session_id，
本次通过真实 HttpClient 执行并独立 SQL 读回用户输入与助手回答。
已检查 SDK-v1 `ed38de7f` 的实际 `sdk/python/src/multirag_sdk`，以及正在开发的 REST-first SDK：
两者没有 Agent/Search completion 方法，不能机械修改上游 `ragflow_sdk.modules.Session`。
当前 SDK-v1 的 Chat Session 专用 scoped completions 与本批 Agent/Search 合同是独立工作，未声称验收。
停滞 Go 验收脚本保留它自己的旧路径，不把 Go 测试当 Python 活动消费者。

实际 Node/tsx 导入独立 Web 领域 API，只有测试 origin 被映射到隔离监听器，路径、body、
鉴权和流解析不替换；Agent 两轮、失效 token、SQL session 归属与消息读回，Search SQL
配置/受控模型输出、拒绝、旧路径 404，以及真实 admin/CLI 均通过。
原生客户端 **1 passed**；Web Agent **1 passed**（包含绑定隔离 API 的 `make smoke`）；
Web Search/admin **1 passed**。Web API **236 passed**，lint 0 errors，文件体积门禁通过；
共享树 build 被其他 WIP 的 TS7030 阻断，HEAD + 本次五个文件的隔离 `npm run build` 通过。
全量 Python 初跑 **6106 passed / 1 failed**：新增集成复用误从测试模块导入 fixture，已移至
`tests/support/reference_metadata_http.py`；专用 harness 复跑和最终批次 gate 记录在后续验收。
模型输出受控，未证明外部模型、浏览器画面、真实飞书或部署。证据日志在本机
`/tmp/multirag-runtime-20261007/item12-*`，集成报告随 `.test-results` 保存。

## 13 · c9513e5ecb9d319d9ce596df541a4798e4064fc0

缺少 runtime replica 时，从已授权 SQL Canvas 的当前草稿 bootstrap，无需先 GET；
先验证 canvas 存在、实际资源 owner、当前有效成员与 draft 执行边界，拒绝后 Redis 零写。
body tenant/user_id 不构成权限；模型与运行上下文取资源 owner，runtime_user 仅选择隔离副本。
副本 key 与 payload 均绑定 canvas/tenant/runtime_user，混错归属 payload 明确拒绝。
Redis 读取/bootstrap 放工作线程，不在 event loop 做同步 IO。

本地改进超过上游简单 read-then-set：缺失初始化使用 editor save 同一分布式锁，
锁内二次检查，已有执行状态不会被并发旧草稿覆盖；busy/写失败返回非零业务结果，
不构造 Canvas。`set_obj=False` 不再被当作持久化成功。并发运行结果仍按最后一次提交覆盖，
本项不引入全运行租约或乐观版本冲突协议。

真实 HTTP/SQL/Redis 五项验收：无需 fetch 首跑使用 SQL 草稿而非旧发布版本，
team 草稿拒绝与零副本写入；24 次/12 线程并发初始化得到同一赢家，后续 bootstrap
保留 editor save；tenant/runtime_user 双维隔离与 TTL；写/锁/归属异常均无执行、无 SQL 改动。
定向 unit **95 passed**；实际结果及 gate 的最终累计见末尾。
首轮错误响应对 data 字段有过强形状假设，修为既有错误信封允许省略空 data，非零业务码
与零执行断言保留。报告：`.test-results/20261007-183345-93351`。

## 16 · 04c5f1b3b621ffc191e783dd7b10c86f176f905a

无 session runtime 按 `query or question` 选输入，query 非空时优先；custom_header
送入真实 Canvas 构造，与 session 分支同一约定。不把请求 user_id 或伪造 prepared_run/
run_context 当身份；tenant 与 Principal 仍来自开流前授权，sys.user_id 保持当前不进入模型
输入的空值。现有模型工厂、消息格式、MCP credential provider 与请求事务不改。
后修 `8f90740d2` 的 chat_template_kwargs 是独立模型控制能力，本项不借输入兼容扩展模型参数。

真实 HTTP/SQL/Redis **3 passed**：query 优先、空串与 null 回退 question，观察真实 Canvas
构造收到 header，内部上下文绑定真实 owner，伪造 tenant/run_context 无效，Redis 读回
正确 sys.query 与空 sys.user_id，SQL Canvas/version 不变。unit completion/context/MCP
**79 passed**。测试启动先后触发旧 nest_asyncio/uvicorn 导入顺序问题，按已有测试先导入
uvicorn 后复跑通过，未放宽启动断言。未调用真实外部模型或 MCP 服务。
报告：`.test-results/20261007-183731-98577`。

## 17 · 67e1de50abbcf4b0acd6ae0560f370b2f7ffb74c

**已被第 20 项完整撤回，不实现其权限扩大。** 目标将组件输入、更新、删除从 owner
放宽为 accessible，并把 async runtime 的异步 DB 包装换为直接同步调用。
最危险的 team 删除权没有有效重落，本地继续仅 owner 删除；FastAPI AsyncSession 与
执行线程分工保持。单独记录撤回结论，不先上线再回滚，也不生成空的权限实现提交。

后修链完整区分：`53a4edfde` 整理访问装饰器，`c50028b1f` 再允许上游团队编辑与组件输入，
删除仍 owner；后来的 `6ce014c23` 再整理阻塞 IO，`20180790e` 补 OpenAI 首跑授权。
本地 management 与受信 draft/publication 的合同原本更严格，是否采用后修的团队编辑
按第 20 项的本地消费者和权限验收决定，不把它误标为第 17 项“已移植”。

## 20 · 15dcdd7b5b1cb7c2433a8cf667d749ae6f5eb8c6

撤回结论成立；与本地 owner 管理边界等价，保留本地异步执行适配。冻结内
`c50028b1f` 的重新放开 team 编辑/组件输入已比较，但**未采用该额外扩权**：
本地更新/发布是 owner 事务及受信草稿/发布来源合同，Web input-form 的实际消费者是
`single-step-debug-sheet`（草稿调试），发布用户表单随执行事件交付。
没有活动消费者要求成员重写 owner 的草稿与发布快照；不借此次撤回扩大发布者权限。
本地 row lock、Canvas/version 原子更新、执行来源校验及开流前授权比装饰器式 accessible
更合适。该结果是明确的本地合同选择，不能声称与 `c50028b1f` 全等价。

| 操作 | owner | 当前有效 normal/admin TEAM 成员 | 邀请/失效/外部用户 |
| --- | --- | --- | --- |
| 组件输入、更新/发布、删除 | 允许 | 拒绝 | 拒绝 |
| Agent 草稿 runtime | 允许 | 拒绝 | 拒绝 |
| 已发布 Agent 首跑/续聊 | 允许 | 允许（当前 canvas 为 team；session 来源绑定调用者） | 拒绝 |
| DataFlow runtime | 允许 | 当前 team + 有效成员校验 | 拒绝 |

新增真实 HTTP/SQL/Redis 六种调用者矩阵与既有执行来源七项验收；拒绝不改 Canvas/version/
session 或 replica，body user_id 不能冒充 owner。允许的发布调用使用 owner 模型租户，
session.user_id 却归真实调用者；owner 删除用 SQL 读回确认，既有 route 只删 Canvas，
不声称关联行级联删除，测试显式清理其自有版本/session。
首轮输入测试错选无表单的 Message，改用真实支持表单的 Begin 后复跑 owner 分支；
其他五种调用者及七项 origin 测试已通过。最终批次复跑覆盖完整矩阵。

## 18 · faae91d34fde8c371fb50ac63e16bc0d15033932

采用 runtime stream=false 聚合意图，映射到 FastAPI 响应及现有 CAS/资源清理合同。
统一 accumulator 汇总 content/reference/structured/可选 trace，保留终态 outputs。
流式和非流式使用同一执行迭代器；成功输出需已终止且有实际结果，纯计时信息、空或 null
structured 不能冒充结果。成功 CAS 与 replica 写入确认后才释放终态和 `[DONE]`，
提交锁与 editor/bootstrap 共用；写入、读取异常或损坏 payload 均明确失败。
没有把 Redis get 的吞错 None 当缺失再覆盖旧状态，阻塞提交放线程执行。

保留本地无 session 草稿 runtime，不机械采用后期自动创建 workflow session 的结构。
不采用目标异常转成功字符串、空执行 data={} 成功、提交失败只记日志的实现；
`09dc4c884` 对空输出仅回 session_id 的成功也不采用。取消/disconnect 继续传播，
close callback 负责已绑定运行的清理；bind/setup 失败仍执行 finish 清理，
仅已绑定运行写 cancel 标记，未注册 attempt 不产生新的取消状态。
finish 已获胜后的既有终态竞态语义保持，不承诺回滚已越过屏障的成功提交。

后修 `f0f10b609`：转发 user_inputs/workflow_finished，表单与终态等 SQL/Redis 保存后交付；
`e9cdd09b6`：session 引用逐轮追加，读取 citation map/列表与历史包形状；
`38f8f8a65`：事件及组件参数的 deferred callable 归一为 null，不泄漏 partial 的 repr，
其他不可序列化值仍报错，不把任意对象 stringify 成成功输出。
已有 session 的空执行保存失败轮及用户输入，不追加成功助手答案；SQL 与 replica 分支分离。

本次真实 HTTP/SQL/Redis：正常流式/非流式 runtime/session、content/reference/structured/
trace、真实 Message 执行加明确的受控输出边界、两轮引用及 citation ID 读回；异常、晚于
终态的 error、零事件/空内容、真实 POST Task 取消、真实写失败/锁失败均零副本提交、
无成功终态。真实 UserFillUp 四种路径持久化 pause、交付表单并 resume；网络读失败和
损坏缓存不能初始化覆盖。结果与清理均独立读回。引用模型与外部 provider 仍受控。

初轮新增测试对 SQL reference/dsl 的形状假设错误，已按真实存储兼容包/数组/字符串 DSL
修正；旧引用写入覆盖的问题作为本项回归修复，原单轮 SQL 断言改为完整包数组断言，
新增两轮断言锁定前轮引用保留。24 项结果/故障/已有图片引用集成、4 项表单集成通过，
最终存储读失败与最新源码专项/通用门禁结果补记在下方。

## 交付范围与环境

八项按同一 owner 串行执行，无 push。独立交付分支为
`codex/agent-runtime-alignment-20261007`，工作树为 Codex 附加的隔离 worktree。
第 11 项首次误带共享 index 的其他任务暂存，纠正时主分支已被其他提交推进，原子校验
阻止改写其他任务历史；独立修正版为 `4919bcbf`，本分支排除混合提交，主分支历史保留。
Web 使用 `git commit --only` 限定五个文件，其余 studio/GraphRAG 等 WIP 保留。

本机既有 .venv/DeepDOC 资源供隔离 worktree 复用，仅复制已忽略的本地配置，未更改共享
配置或安装依赖；缺失模型资源的首次测试初始化触发下载尝试后已中断，改用本机现有资源。
真实 HTTP 使用自有监听器；PostgreSQL scratch 库、fixture-owned Redis/对象/索引按现有
fixture 清理，未重启生产/常驻服务，未运行线上迁移或部署。

最终源码已通过 `make verify`：8 条 import contracts、async DB 门禁、mypy 144 文件、
unit **6115 passed**（3 条既有 transformers deprecation warnings）。最新 runtime completion
及 replica 专项 `make integration` **34 passed，无 skip**。完整 core 门禁实际为
**993 passed / 2 failed / 0 skip / 2 deselected**，报告 `.test-results/20261007-184952-5940`。
其中草稿绑定失败后的 close callback 曾新建 cancel 标记，已修复为仅已绑定运行写标记；
不放宽原有 SQL/Redis 零残留断言。修复后的取消、终态、close、runtime completion/replica
六个文件复跑 **158 passed / 1 failed / 0 skip**，草稿与发布的绑定失败两分支均通过，
报告 `.test-results/20261007-195205-40151`；修复后 `make verify` 再次 **6115 passed**。
剩余失败为无关 Skill core 的 Go CLI 编译：默认 PATH 无 go，指定现有 Go 1.25.14 后，
该工具链的 `src/fmt` 等标准库目录为空，编译仍失败。没有修改无关 Go 代码或安装工具链，
因此完整 core 门禁尚未全绿；本批 Python admin/client 的真实 HTTP/SQL 验收已通过。
Web 的 236 项 API 测试、lint 0 errors、文件体积门禁、隔离完整 build，以及
真实 Web Agent/Search、admin/CLI、绑定隔离 API 的 smoke 均通过。
本机日志与源码 SHA256 manifest 在 `/tmp/multirag-runtime-20261007/`，服务准备/测试/清理
证据在各次 `.test-results`，不以此前历史通过数替代本轮结果。

## 合并 main 验证

2026-10-07 基于 main `2592ff26` 合并交付分支。main 已包含第 4、11 项，
文件关联实现等价保留；两处文档冲突保留本批新增内容及 main 其他合同，代码自动合并。
合并结果 `make verify`：**6259 passed**、mypy 144 文件、lint/import/async DB 门禁通过；
本批权限、执行来源、runtime、文件关联、原生/Web/admin 消费者、取消与 close 集成
**175 passed / 0 skip**，报告 `.test-results/20261007-195659-43390`，其中真实 Web Agent
验收绑定隔离 API 的 `make smoke` 通过。未重跑无关 Skill Go 编译，前述环境限制仍保留。
main 的其他未提交修改与 untracked 文件保留；重叠 README 的本地改动单独保存后恢复，
不将它们纳入合并提交。无 push。合并日志与 WIP 校验记录在原本机证据目录。

## 2026-10-17 · Go 工具链恢复与完整 main 门禁

前述 Go 工具链阻塞已解除。从 [Go 官方发布页](https://go.dev/dl/#go1.25.14) 下载并校验
`go1.25.14.darwin-arm64.tar.gz`，SHA256 为
`5b26c0b6f308240fca2614fb02f622cfcc8c0cc3b69c78bba4845489a4590259`。
完整工具链位于 `~/.local/share/multirag/toolchains/go1.25.14/go`，用户级
`~/.local/bin/go` 已可从普通 shell 解析；保留本机 `integration-go.env` 供固定版本复现。
真实 CLI 构建与 Skill HTTP/CLI 共享合同均通过，未更改 Go/Python 依赖锁或共享配置。

main 在验收期间持续合入其他任务，每轮均从对应提交的隔离快照运行。
按用户确认，本次正式交付锁定 `3bd0bfc77e0c114c2f505a0e40aef7f645f95112`；
后续提交另行验收，已额外完成的 `f2dc7ba2` 复验单列为补充证据，不扩大本次交付范围。
实际结果如下：

| main 基准 | make verify（unit） | 完整 make integration（默认 core） |
| --- | --- | --- |
| `5bd47c61` | 6259 passed | 1048 passed，0 failed，0 skip，6 deselected |
| `3bd0bfc7`（本次正式交付） | **6259 passed** | **1048 passed，0 failed，0 skip，6 deselected** |
| `f2dc7ba2`（独立补充复验） | 6339 passed | 1071 passed，0 failed，0 skip，12 deselected |

正式交付基准的完整集成退出码 0，耗时 4334.40 秒；无 TESTS 局部选择、无门禁或断言调整。
6 个 deselected 为默认 core 排除的独立 consumer 组（4 个钉钉与 IMAP 明暗主题页面用例及
2 个数据集 Web 消费者用例），见[测试分组](../../tests/README.md)；不当作 skip 或通过。
`make verify` 的 lint、8 条 import contracts、async DB、mypy 144 文件均通过；
unit 仅有 3 条既有 transformers deprecation warnings。真实 Web Agent 的隔离 API smoke 亦通过。

独立补充复验 `f2dc7ba2` 的完整集成退出码 0，耗时 4408.89 秒；新增任务日志 4 项、
Moodle 9 项、关系库同步 10 项均通过。其 12 个 deselected 为默认 core 排除的独立
consumer 组（10 个明暗主题连接器页面用例及 2 个数据集 Web 消费者用例）。

依赖使用本次专用 loopback 端口、PostgreSQL 16.14、Milvus 2.6.22、Redis 与 MinIO，
fixture-owned scratch 数据独立读回并清理。结束后 5 个自有容器、4 个卷及专用网络已删除，
私有服务覆盖已移除；未重启常驻服务或执行生产操作。主工作区 28 个未提交文件的哈希保持，
本次记录提交仅限本文件，无 push。临时隔离 worktree 在报告另存后归档，不新增交付分支。

持久化证据根目录为 `~/.local/state/multirag/validation/20261017-main-integration/`：
正式交付的 `latest-main-3bd0bfc7/report/` 保存 run.json、JUnit 与逐用例事件，
同级 verify.log、integration-full.log 与 result.json 绑定 SHA 和门禁结果。
独立补充证据位于 `final-main-f2dc7ba2/`，其 service-cleanup.json 保存最终清理读回；
父目录保留官方工具链校验元数据及首轮报告。
