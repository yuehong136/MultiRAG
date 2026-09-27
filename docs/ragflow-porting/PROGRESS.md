# RAGFlow 逐提交跟进记录

本记录只写单次提交的处理结论。稳定路径映射见
[RAGFLOW_PORTING_MAP](../enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md)；后续提交按各自任务处理。

## 4dcc42e0e14ad4a93373f08b325757cba285ac54 · 统一数据集与索引 API

- 上游：`infiniflow/ragflow` #14222，提交于 2026-04-27；核对了完整 51 文件 diff。
  2026-09-27 fetch 后的 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
- MultiRAG 主体已有提交：`a2f10eed`、`def8625c`、`b17e9a8b`。本次补齐图谱读取新路径，
  并标明旧路径的兼容状态；下一上游提交 `a9e5724b` 不在本项范围。

| 上游 diff | 本项结论 |
|---|---|
| `api/apps/restful_apis/dataset_api.py`、`api/apps/services/dataset_api_service.py` 的数据集详情、摄取概览/日志、标签聚合/修改、元数据配置及统一 `graph/raptor/mindmap` 索引 | 主体由上述三次 MultiRAG 提交语义移植；保留 FastAPI、AsyncSession、显式删除路径和旧接口兼容层。本次发现并补齐 `GET /datasets/{id}/graph/search`，复用现有图谱 service。 |
| `api/apps/restful_apis/document_api.py` 的 `POST /metadata/update`、文档 parse/stop | 已由本地 `document_api.py` 与 `document_api_service.py` 实现；混合有效/无效 ID 的 parse 响应保留非零业务码及部分执行结果。 |
| `api/db/services/doc_metadata_service.py` 的 ES 元数据整字段替换 | 已由 `metadata_store_engine.py` 等价实现：用脚本替换 `meta_fields`，避免对象深合并留下被删除的 key。 |
| `api/apps/kb_app.py` 删去旧路由与相应旧测试 | 暂不删除仍供调用方使用的 `/v1/kb` 路由；本次将上游删除范围内仍存在的旧路由标为 deprecated。新行为只在 `/api/v1/datasets` 实现，旧路由待调用方迁移后按兼容策略退役。 |
| `POST /datasets/{id}/embedding` | 暂不采纳：上游后续 `70a49c947` (#16936) 明确删除这个未使用的端点和 service。现有 `/v1/kb/check_embedding` 是抽样校验，不是该端点的等价替代。 |
| `sdk/python/ragflow_sdk/modules/dataset.py` 将自动元数据配置改用 `/metadata/config` | 本仓无该客户端 SDK 源码；服务端新路径已存在，旧 `/auto_metadata` 仍为 deprecated 兼容入口。客户端方法迁移留给 SDK 所在任务。`api/apps/sdk/dataset.py` 是服务端空 router，并非该客户端模块。 |
| `web/src/...` 九个文件的调用路径调整 | 前端是独立 `../web` 仓，按移植 Skill 另排；本次只保证新增的图谱读取路径在服务端可用，未改前端。 |
| `sdk/python/test`、`test/playwright`、`test/testcases` 的新增、改写与删除 | 不复制上游 Quart/Peewee 测试 harness；本仓现有 dataset/index、文档解析、元数据及存储回归测试覆盖已移植行为。本次扩充图谱路由契约测试和异步依赖树检查。 |

后续链核对：`35f6d81b7` (#14402) 后续扩展图谱检索 REST 行为，不能作为本项的额外实现范围；
`70a49c947` (#16936) 删除本提交新增的 embedding 入口，因此该入口不移植。

验证：`make verify` 全绿（8 条 import contracts、mypy 124 个源文件、unit 3100 passed）；
图谱新旧路径的成功/鉴权失败响应及 OpenAPI deprecated 状态由 unit 覆盖。
`make smoke` 未通过：本机没有运行中的 API，Redis 与 MinIO 不可达；本次未启动会初始化配置数据库的服务入口。

## a9e5724b46e9f006b90ddd70f812fb59840c6806 · 统一文档创建入口

- 上游：`infiniflow/ragflow` #14345，提交于 2026-04-27；目标父提交是上项 `4dcc42e0`。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`；
  本项按目标提交的 10 文件完整 diff 评估。

| 上游 diff | 本项结论 |
|---|---|
| `api/apps/restful_apis/document_api.py` 新增 `type=local|web|empty` | 移植到现有 FastAPI `POST /api/v1/datasets/{id}/documents`。`local` 保留已有 `file`/`files` 批量上传与返回数组契约；`web` 接收表单名称和 URL，校验租户与 URL 后转换 PDF，复用现有 `FileService.upload_document`；`empty` 接收 JSON 名称，创建虚拟文档与文件关联。网页的 Selenium 与同步存储链在自有会话的工作线程执行；空白文档经请求会话桥接遗留同步 service。 |
| `api/apps/document_app.py` 删除旧 `/web_crawl`、`/create` | 本地仍有旧调用方，两个路由保留可用并标记 deprecated，待消费方迁移后退役。 |
| `docs/references/http_api_reference.md` 的三种请求示例 | 更新本仓同名参考文档和 REST 模块 README；按本地契约说明 `file`/`files`、返回形状和不自动解析。 |
| `test/testcases/test_web_api` 四个文件的 helper/验收改写 | 不复制上游 Quart/Peewee harness；本仓增加 FastAPI 路由、鉴权、URL 阻断、空白文档与文件关联等单元回归，保留已有上传回归。 |
| `web/src` 三个文件的调用切换 | 本地前端在独立 `../web` 仓，本项不改该仓；服务端新路径已就绪，旧路径供其迁移期间使用。 |

后续链核对：`a339e8a57` 处理批量文件部分成功，属于后续上传契约变更；
`6e0e49592` 修复阻塞式上传/网页处理，这里已经按 MultiRAG 异步边界将网页链放入线程，
两条后续提交均不作为本项额外范围。现有 `is_valid_url` 检查输入 URL 的出网地址；
浏览器抓取过程仍依赖既有 Selenium 实现。

验证：`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3115 passed）；
新增创建模式与既有上传路由定向测试通过，另增 OpenAPI 兼容标记检查。
`make integration` 因 Redis `127.0.0.1:6379`、MinIO `127.0.0.1:9020` 不可达而未运行测试；
`make smoke` 因 API `127.0.0.1:8123` 未启动而失败。本项未启动会初始化配置数据库的服务入口，
因此当时真实存储写入与网页抓取端到端验收尚未完成。

补验收（2026-09-27，基础服务启动后）：`make integration` 通过 235 个测试。
API 使用仅供本次验收的新建 PostgreSQL scratch 库启动，`make smoke` 通过；健康检查的
数据库、Redis、文档引擎和存储状态均为 `ok`。通过真实 REST 请求创建了 `empty` 虚拟文档、
`local` 文本文档和 `web` PDF 文档，逐个通过列表接口读回，并独立查验 scratch 库中的
文档行和文件关联。删除文档、读回空列表、删除数据集后，API 停止且 scratch 库已删除；
未对配置的 `xldu` 库运行启动迁移。公网域名在本机被 DNS 映射到保留地址而被 SSRF 校验拒绝，
网页模式改用通过校验的 `https://1.1.1.1/cdn-cgi/trace` 完成验收。

补验收发现 webdriver-manager 在本机将可执行路径指向 `THIRD_PARTY_NOTICES.chromedriver`，
导致网页创建返回业务错误。已在共享 `html2pdf` helper 中选择同目录真实 `chromedriver`，
并补齐其执行权限；增加对此缓存布局的回归测试。修复后网页创建成功并完成上述读回与清理。
修复后重跑 `make verify`（unit 3116 passed）与 `make integration`（235 passed），均通过。

## 3ad3241ae06f414d2ccd2c92fda8c576bb96a96a · 保存 RAPTOR 摘要层级

- 上游：`infiniflow/ragflow` #13286，提交于 2026-04-27；父提交为 `a9e5724b`。
  核对了目标提交的 3 文件完整 diff；2026-09-27 fetch 后的 `origin/main` 为
  `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。

| 上游 diff | 本项结论 |
|---|---|
| `rag/raptor.py` 返回 `(chunks, layers)`，少于两个输入返回 `([], [])` | 对齐到 `core/raptor.py`，保留原有聚类和摘要过程；`layers[0]` 是原始节点，后续边界对应逐级摘要。 |
| `rag/svr/task_executor.py` 为索引中的摘要写入 `raptor_layer_int` | 对齐到 `run_raptor_for_kb`：按摘要节点在完整结果中的下标映射层级，第一层为 1，第二层为 2；保留本地 `pk` 和文档 ID 规则。另有四处本地分析调用点消费原来的纯列表返回值，均解包新返回值并继续使用原摘要列表。 |
| `conf/infinity_mapping.json` 新增整数列，ES 由 `*_int` 动态模板处理 | 更新 `configs/infinity_mapping.json`；本地 Milvus 是默认文档后端，其显式 schema 另在 `configs/mapping.json` 增加 INT64 字段。ES/OpenSearch 已有 `*_int` 动态模板。 |

后续链核对：`bf4864e61` 为后续新增的 RAPTOR `extra` 字段修补 Infinity 写入，
本项摘要未写 `extra`；`2717ee283` 引入新的 Psi RAPTOR 构树流程，
`62f94cd59` 将实现拆到 `raptor_service.py`，`0c2fb622e` 调整小层聚类。
这些是后续独立行为；当前上游主线仍在任务执行和重构后的 RAPTOR service 写入
`raptor_layer_int`，未回退本项语义。

验证：新增单测覆盖 RAPTOR 返回边界和 1、1、2 多层摘要索引字段；
`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3118 passed），
`make integration` 通过（235 passed）。隔离 Milvus 集合实测新 schema 为 INT64，
摘要层 2 和未带字段的普通切片默认值 0 均可在 flush 后读回；用旧 schema
模拟的动态字段集合也读回层 2，两个临时集合均已删除。
Infinity 未配置运行实例，映射和既有列迁移逻辑仅经代码核对，未做真实后端写入验收。
VastBase 的现有映射连 `raptor_kwd` 也未定义，本项不扩大到修复该后端原有的
RAPTOR 写入契约；该后端的 RAPTOR 层级落库仍待单独处理。

## 33bb464ce3f5598bf3107a8598d86fef9a4011d7 · 聊天共享页误发 Agent 请求（仅审查）

- 上游：`infiniflow/ragflow` #14190，提交于 2026-04-27；父提交为上项 `3ad3241a`。
  核对了 5 文件完整 diff。2026-09-27 fetch 后 `origin/main` 为
  `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
- 独立前端 checkout `../web` 的 HEAD 为 `75db97866bc496294e5db55fe829fd47006750ef`，
  本次只读审查，未跨仓编辑。其当前路由有 `/agent/share`、`/chats/widget`，
  没有上游的 `/chats/share` 页面。

| 上游 diff | 本项结论 |
|---|---|
| `web/src/hooks/use-agent-request.ts` | 实际 diff 将 `useFetchSharedAgent` 改名为 `useFetchFlowSSE`，并给查询加 `enabled: !!sharedId`；并未新增可由调用方传入的 `enabled` 参数。查询函数仍调用 `agentService.getAgent(sharedId)`，对应 `/api/v1/agents/{id}`，且目标提交中该 hook 已无调用方。本地 `../web/src/hooks/use-agent-query.ts` 另有按 Agent ID 启用的同名 hook，不应照搬上游的改名。 |
| `web/src/pages/next-chats/share/index.tsx` | 移除共享聊天页的 Agent 查询、`from` 分支和 Agent 头像读取，统一使用聊天信息的 `avatar`。根因是把聊天 `dialog_id` 当 Agent/Canvas ID 请求，触发权限错误；本地没有此页面，当前无需移植。若以后实现聊天共享页，应从聊天信息取头像，且不得用聊天 ID 调 Agent 查询。 |
| `web/src/locales/zh.ts` | `rootAsHeadingTip` 仅换行排版，无文案变化；不移植。 |
| `web/src/pages/user-setting/data-source/data-source-detail-page/index.tsx` | `useAddDataSource` 调用只调整空格，无行为变化；不移植。 |
| `web/src/pages/user-setting/data-source/hooks.ts` | import 与函数参数仅调整排版，无行为变化；不移植。 |

独立 web 仓待办：若新增 `/chats/share?shared_id=...&from=chat`，先确认后端的聊天共享信息及
公开访问契约，再实现独立聊天数据查询；不要复用 Agent/Canvas 详情查询。
增加覆盖请求边界的回归：聊天共享页实际打开后网络请求中没有
`/api/v1/agents/{dialog_id}`（也没有 Canvas 详情请求），聊天标题、头像与消息正常显示；
同时验证 `/agent/share` 的 Agent 信息与头像仍正常、缺失 ID 时不发详情请求。
改动发生在 `../web` 时运行其 `npm run lint`、`npm run build`、`npm run test:unit`，
再用浏览器核对请求与业务码，不能只凭 HTTP 200 判断。当前本地 Agent 共享页使用
`useFetchExternalAgentInputs`，并不调用同名 `useFetchFlowSSE`。

MultiRAG 后端无需移植：目标提交没有后端 diff；其修复是去掉错误的前端请求，
不能通过放宽 `/api/v1/agents/{canvas_id}` 的 Canvas 权限校验来掩盖问题。
上游后续链中未发现对此删除行为的 revert 或直接修补；当前 `origin/main` 的聊天共享页
仍不调用 Agent 查询。后续 `5a2cd36b4` 修改共享页语言同步，属独立问题。

验证仅针对审查文档：核对目标 diff、上游当前状态、本地路由和调用链；
检查本次文档 diff、路径与链接。不改可执行代码，因此本项不运行 Python 或 web 门禁。

## f3b7d55a1e4f2fa2748979caaef42f93651d41c8 · Infinity 更新遇到缺表

- 上游：`infiniflow/ragflow` #14153，提交于 2026-04-27；核对目标的 3 文件完整 diff。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。

| 上游 diff | 本项结论 |
|---|---|
| `rag/utils/infinity_conn.py` 的 `update` | 移植到 `core/utils/infinity_conn.py`：Infinity 抛出精确的 `TABLE_NOT_EXIST`（3022）时记录缺表并返回 `False`，其余错误原样抛出；连接始终释放。本地实现同时覆盖查表与更新期间删表。 |
| `memory/utils/infinity_conn.py` 的 `update` | 本仓确有活跃的 memory Infinity 存储路径，按相同规则移植，并覆盖查表、更新与连接释放。 |
| `api/apps/document_app.py` 的 `change_status` | 移除按异常文字包含 `3022` 分类的逻辑；存储返回 `False` 或抛异常均返回非零业务码及每文档错误，不把状态更新报告为成功。 |

本地还有 `api/apps/services/document_api_service.py:update_document_status_only` 供 REST 文档状态路径调用；
原逻辑未检查存储 `update` 的布尔结果。本项使已有切片的文档在 `False` 时返回业务错误，
未解析、无切片的文档沿用旧路由规则跳过存储更新。其他 Infinity 错误仍进入原有服务端错误响应。

后续链核对：上游 `a536980e2` 将旧 `change_status` 路由迁移到 REST 批量状态接口，
不是本次缺表处理的回退；当前 `origin/main` 的文档与记忆 Infinity `update` 仍保留
`TABLE_NOT_EXIST` 判断。下一指定提交 `0d87ceca` 不在本项范围。

验证：新增单测覆盖两种 Infinity 存储的查表和写入阶段 3022、其他错误、正常更新与连接释放，
以及旧路由和 REST 路由的失败业务码、未解析文档跳过存储更新。`make verify` 通过
（8 条 import contracts、mypy 124 个源文件、unit 3132 passed）。
`make integration` 通过（235 passed）。
本机无 Infinity 容器，`127.0.0.1:23817` 也不可连接，本项未做真实 Infinity 写入验收。

## 0d87cecae2e47b3f9b46836d2c3d06b97f082f4d · 持久化 PDF 原始书签

- 上游：`infiniflow/ragflow` #13287，提交于 2026-04-27；核对目标的 3 文件完整 diff。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。

| 上游 diff | 本项结论 |
|---|---|
| `rag/app/naive.py` | 对齐到 `core/app/naive.py`：PDF 解析器已有书签时，在首个切片附临时 `__outline__`。按后续 `907587243` 修复，从实际的 `(title, depth, page)` 三元组取前两项，避免原提交的解包异常。 |
| `rag/app/manual.py` | 对齐到 `core/app/manual.py`：复用本地 manual 解析时已经提取的书签列表，不依赖部分解析器没有提供的 `pdf_parser.outlines` 属性；无书签不附临时字段。 |
| `rag/svr/task_executor.py` | 对齐到 `core/svr/task_executor.py`：移除切片中的 `__outline__`，把 `[{title, depth}]` 写入 `DocMetadataService` 的文档元数据，并按布尔返回值区分保存成功与失败。 |

本地 `FACTORY` 还含 book、paper、laws、one、presentation 等 PDF 解析路径；
当解析器未传 `__outline__` 时，任务执行器从原始 PDF 在工作线程提取书签。
本地 `run_dataflow` 绕过 `build_chunks`，也在索引前从关联的 PDF 文件提取并保存书签；
`analyze_v2` 使用临时文档 ID、没有 Document 记录，不属于此持久化路径。
书签写入放在自动元数据生成之后，避免通用 `update_metadata_to` 将已有的字典列表过滤掉；
写入时保留其他已有元数据，并以新书签替换旧 `outline`。无书签不写入。
保存失败时记录警告或异常，沿用上游的解析继续语义，不记录虚假的持久化成功。
本地 Milvus 配置下，`DocMetadataService` 将 `meta_fields` 存在独立的
`t_ai_document_metadata` 表，而不是直接改 `Document.meta_fields` 列。

后续链核对：`907587243` 修正上游的书签三元组解包崩溃，本项已包含；
`f0cb7a544` 将上游任务执行器拆层并记录元数据写入返回值，未回退书签行为。
当前上游主线仍有 naive/manual 的临时书签传递与任务执行器写入；下一指定提交
`6a23dfee` 不在本项范围。

验证：单测覆盖 naive 真 PDF 有/无书签、manual 三元组、其他 PDF 解析模式的回退、
临时字段从全部切片清理、元数据合并与保存返回 `False` 或异常时不记录成功。
隔离 PostgreSQL scratch 库用生成的 PDF 运行标准解析，再通过
`DocMetadataService` 独立读回 `outline` 和原有字段；另直接验收 dataflow 补充路径
的取件、保存与读回。带书签和无书签各覆盖两条路径，随后删除临时文档、数据集与元数据；
`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3141 passed），
`make integration` 通过（239 passed）。未启动完整 Canvas dataflow 管道；其书签
取件与元数据写入部分由上述隔离测试验证。
