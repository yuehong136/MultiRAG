# RAGFlow 逐提交跟进记录

本记录只写单次提交的处理结论。稳定路径映射见
[RAGFLOW_PORTING_MAP](../enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md)；后续提交按各自任务处理。

## 0f2778efe744b5aef879f1743c3ec50fd1143aab · Agent 更新支持发布

- 上游 #14396，2026-04-27；2026-10-02 确认 origin 并 fetch 后，
  `origin/main` 为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
  从本仓 `771d6864` 开始，完整核对指定 SHA 的一个文件、两处 diff。

| 上游 diff | 本项处置 |
|---|---|
| `api/apps/restful_apis/agent_api.py:update_agent` 归一化 `release` 并更新 Canvas | 保留字段并写入 Canvas；兼容明确的布尔字符串，避免上游 `bool("false")` 误发布。DSL 保存缺省/空值为草稿；纯元数据增量更新保留现有发布标志。 |
| 同方法把 `release` 传给 `save_or_replace_latest` | 同一个同步 Session 保存 Canvas 与版本；release-only 使用当前 DSL 保存/发布。请求持有 owner Canvas 行锁，版本服务支持 `commit=False`，异常向调用方传播；插入、版本清理和 Canvas 更新一次提交，失败回滚。 |

当前本地的关联读回缺口一并修复：`UserCanvasService.get_by_canvas_id` 的选列新增
`release`，GET 返回当前状态；首次 `/agents/chat/completion` 的 `release=true`
进入已有会话/发布版本运行链，避免使用 Redis 草稿副本。已建会话继续使用其自己的 DSL。
相同 DSL 重复发布不另建快照、版本标题不变；发布后草稿另建版本，已发布快照不删除，
草稿最多 20 个。新快照的创建顺序在同毫秒保存时仍明确，更新时间不早于创建时间。
API 的请求、响应及 Redis 部分失败合同见 [HTTP API 参考](../references/http_api_reference.md#更新与发布画布)。

未找到本项 revert/re-land。后续 `569a29500` (#17576) 改更新响应为 update_time，
`a0438517b` (#18056) 补组件参数验证，分别属于相邻目标；本项保持当前 `data=true`
与 DSL 归一化合同。版本服务后续 `006747090` 修正无序分页，未回退发布行为；
上游整体删除 Python 后端不在本次范围。下一项 `10e28e5c` 等待派发。

调用方核对：旧 `/v1/canvas` 只保留任务取消，无 set/get/save 消费者；REST 是当前
唯一创建/更新链。复制/模板导入经 POST 创建新画布，未把原发布历史复制到新 ID。
SDK API Key 通过同一 PUT 入口及 owner 校验，实际 HTTP 已验证；`api/apps/sdk/session.py`
的已发布 DSL 选择帮助函数与 `canvas_service.completion` 都使用最新 released 快照。
独立 Python SDK 工作树 `multirag-rest-first-python-sdk` 的 `d0d34818` 无 Agent 更新资源。
独立 web `5c7a77382cb87400bf70ff492b1b310cb89ce3bd` 的 `src/api/agent.ts:setAgent`
透传 release；主编辑器与嵌入编辑器发布时传 true，普通保存省略。`useSetAgent` 成功后
失效详情及版本查询，详情/发布组件读取 release/last_publish_time，API client 兼容 retcode。
没有发现需修改的前端合同，本项不编辑 web，也未做本项的真实浏览器验收。
列表重命名目前也发送 DSL，按既有“保存 DSL 即草稿”语义处理；仅元数据请求仍保持发布状态。

Go 实际核对 `internal/router/router.go`、handler/service、`internal/dao/user_canvas.go`、
`internal/entity/canvas.go` 和 `internal/cli/{parser,client}.go`：无 Agent 更新、发布、版本
或运行路由及服务消费者。DAO 只有泛用 Canvas CRUD，UserCanvas entity 已有 release，
版本 entity 尚无 release；CLI 的 LIST AGENTS 仅解析，执行分派落入未实现分支。
没有相同目标链，本项不新增 Go API/字段，不运行无改动的 Go 编译门禁。稳定路径映射未改变。

验证与实测：unit 覆盖 true/false、字符串、非法类型、缺省/None、元数据与 release-only，
并拒绝版本失败回执。隔离 PostgreSQL scratch 库中的真实 HTTP 完成发布、相同 DSL
重复保存/发布、改 DSL 草稿、再次发布、GET/版本列表与独立 SQL Canvas/version 读回；
缺/坏认证、其他 owner JWT/API Key、非法 release/DSL 均无错误写入。
发布后改草稿，再创建 release=true 会话，独立 SQL 确认 session DSL/prologue/version_title；
首次发布运行用真实 Begin/Message 输出旧发布内容，Redis 独立读回仍为新草稿。
没有模型 provider 假件或远程模型调用。
真实 PostgreSQL trigger 分别拒绝版本 INSERT、Canvas UPDATE、版本清理 DELETE，
HTTP 返回非零 retcode，Canvas/版本/Redis 均保持原值；移除 trigger 后重试成功。
Redis 同步 False 回执另有验收：返回“已保存但副本同步失败”，数据库已提交，
重试恢复副本；数据库与 Redis 不是分布式事务。未声称生产部署或生产数据验收。
全部临时账户、membership、token、Canvas、版本、会话、trigger/function、自身 Redis key
和 HTTP 监听均已清理并检查。隔离 API 内实际执行 `make smoke` 通过。

最终门禁：`make verify` 通过（Ruff、8 条依赖契约、async DB 检查、mypy 126 文件、
3346 unit passed）；`make integration` 253 passed、无 skip。最终树复跑了上述
真实 HTTP/SQL/Redis 与运行验收，含 `make smoke`；没有沿用历史通过数。

## 61a24a2c14dde696244646e1ec69e5f150eeda54 · 聊天附件上传迁入 REST

- 上游 #14359，2026-04-27；本轮确认 `origin` 并 fetch，`origin/main` 为
  `519e7d98a5651564d4e35d6648f006cba4baaf4f`。目标代码取指定 SHA tree，完整核对 7 文件 diff。
  从本仓 `70dd89a4` 开始，只处理本项；下一项 `0f2778efe744b5aef879f1743c3ec50fd1143aab` 等待派发。

| 上游文件/行为 | 处置 |
|---|---|
| `api/apps/restful_apis/document_api.py` | 采纳 `/api/v1/documents/upload`，重复 `file` / query `url` 互斥且须其一，单对象/多数组。PR 描述的 `documentss` 拼写错误不采纳。FastAPI + 异步 Principal + 请求级 AsyncSession；直接 await 已有 async service，不复制上游线程池包装异步函数。 |
| `api/apps/document_app.py` 删除旧路由和导入 | 开工时 web 的 conversation/use-chat-upload/use-mcp-upload 使用旧路由，迁移期间先保留 deprecated。web `5c7a773` 完成真实跨端验收后，本项收尾删除 `/v1/document/upload_info` 及唯一专用导入 `UploadInfoArgumentError`。 |
| `test/testcases/test_web_api/test_common.py` | 不复制上游 HTTP harness；重建当前 unit fixture 与 scratch PostgreSQL/MinIO 的真实 HTTP 验收。 |
| `test/.../test_upload_info_unit.py` | 采纳测试意图，但不采用只看 data 存在、条件性数组断言；明确验证类型、元素字段、业务码、鉴权、owner、独立字节读回和实际消费者。 |
| `web/src/hooks/use-chat-request.ts` | 前端另派；本项核对并修复后端聊天/MCP 附件消费链，未修改 web。 |
| `web/src/services/next-chat-service.ts` | 前端 `5c7a773` 已完成代码与隔离跨端验收；准确 multipart/响应/认证契约，SDK 既有入口保留。 |
| `web/src/utils/api.ts` | 新 URL 已落实两仓，web 当前 tracked 文件无旧路径活动调用，真实上传和消费者验收通过。 |

未查到本项的 revert/re-land；后续 `a4f325be2` (#16264) 恢复旧路径兼容，
本仓初期据已查明活动消费者保留兼容；当前 web 完成迁移验收，按用户授权和已约定退出条件
删除旧别名。`e35860ad7` (#16269) 另补 Go 上传与 metadata batch；
其中 downloads descriptor、正确字节数、HTTP 错误与 URL 内容类型归一化作为交叉核验。
后续整体 Python 后端删除另属其他提交。已实际核对本仓 Go `internal/router/router.go`、
`internal/handler/document.go`、`internal/service/file.go` 与 `internal/cli/{client,http_client,contextengine/*}.go`：
Go 无 upload_info 路由/descriptor 消费者；活跃 CLI 文件 provider 使用 `/files`、dataset provider 使用
`/datasets/{id}/documents`，本次新增附件入口未改变它们的路径/数据合同，因此本项不改 Go。

必要本地适配：当前 REST 文档与 SDK 文件两个网关共用 `FileService.upload_infos`，
SDK `/files/upload_info` 保留字段 `files` 与已有鉴权；旧 web metadata 网关已退役。
新入口的 JWT / SDK Key 都校验
有效用户与个人 owner membership，`created_by` 使用服务器 Principal 的 platform_user_id。
存储仍为 `<owner>-downloads`，没有写入 dataset/File/Document 表或宣称解析完成。
`size` 改为实际 bytes 长度，URL 生成的 PDF 使用正确 MIME。
存储回执按现有 adapter 处理：GCS False 失败，MinIO 有效对象成功；S3/OSS/OpenDAL
的 None 要核对象存在，失败不返回附件 id。上传服务在同 owner 空间登记 `<id>.upload.json`，
登记失败补偿文件；批量失败只补偿本批 location 与描述，明确暴露清理未确认；
取消请求屏蔽对写任务的直接取消、等待完成并补偿，强制进程终止及存储不可达不保证回滚。

同项 MCP 最小适配：保持 `ChatRequest.files: list[str]`，按 Principal.platform_user_id
读取服务器登记，校验 id/owner/字段及对象存在；不信前端自造 descriptor、created_by 或
助手消息。真实 Canvas 读取/解析后，文字加入本轮用户消息，图片放入 visual_files_var
再走 Agent 的既有模型 images 路径。普通/structured、tools/no-tools 与相关非流式共用，
每次调用清空旧附件且预解析只消费一次，重复调用重新验证；缺失/越权/描述或 blob 缺失/
解析失败流前 HTTP 400。structured error 不再包装成功 retcode；流中错误发 500 后终止，
普通流亦不再跟成功完成帧。非流式 structured 实际走结构化分支并累积全文。

URL 保留原初始/DNS/redirect 防护，并修复 HTTP 预检与浏览器第二次请求之间的绕过：
浏览器每个 HTTP GET/HEAD 经 `common/safe_crawl.py` 转发，每个新目标与 redirect 在请求前
校验并 DNS 绑定，不用环境代理重新解析目标；浏览器旁路走自持有、拒绝连接的代理，
禁用 loopback bypass 和非代理 WebRTC UDP，并拦截 WebSocket。抓取失败、空内容和
安全阻断返回可识别参数错误，不存成成功附件。CI 的 integration job 安装 Chromium 运行资源。

实际验收：scratch PostgreSQL 的两位 owner、真实 JWT 与 API Key、随机 MinIO bucket/
owner key、随机 Redis descriptor key、完整 API 的临时 HTTP 监听（不启后台 lifespan）。
单文件与重复 file 的文本+PNG 多文件验证完整元数据、逐对象独立字节读回、错误 owner key 无对象；
实际 `split_file_attachments` 和 `Canvas.get_files_async` 解析文字/图片，再把 Redis 读回的
descriptor 交给消费者。核心阶段旧 web/SDK 入口亦实际上传并读回；退役收尾改为验证
旧路径拒绝且不写对象，REST/SDK 继续上传并独立读回字节和可信描述。缺/无效认证、失效 membership、
缺/混合/空字段、内网 URL、真实 MinIO 失败和第二次写失败均核业务码与无成功 data。
第二次失败后独立列举对象，证明本批无残留且既有对象仍在；unit 另覆盖 False/None 回执、
清理失败诊断、描述登记失败补偿和请求取消后无对象。真实补偿验收发现 nest_asyncio 的
Python Task 与 beartype 的 C Task 注解不相容，drain 边界改用 Awaitable，已复跑真存储补偿通过。

另用真实 HTTP 上传文本+PNG，独立 MinIO 字节/可信描述读回，再仅提交 IDs 到 MCP。
使用真实 ChatAgentAdapter、Agent、Canvas 和解析器，普通/structured、tools/no-tools、
流式/非流式 8 分支在模型调用边界捕获到了文件正文和正确 image data URI；REST/SDK
上传登记也经 ID-only 聊天实际消费。missing/foreign ID、删除 blob、真实损坏 DOCX 解析
失败均覆盖普通/structured/非流式，模型未被调用。provider exception/错误标记均无成功完成帧。
模型 provider 为捕获假件；tools 分支选用工具存在假件，没有调用远程 LLM 或外部 MCP 工具。

真实 Chromium 控制测试：只把已验证测试域的 HTTP transport 映射到本机 origin，
真实 SSRF guard 对其他目标保持启用；预检 200 后第二次返回内网 302、JS 导航、iframe、
图片、fetch 与 WebSocket 均未命中内网 trap，错误 code=101 且无存储对象；正常测试 origin
生成 PDF 并独立读回。外部 URL `https://1.1.1.1/cdn-cgi/trace` 亦实际抓取、存储、
解析后读到 `h=1.1.1.1`。`httpbin.org` 首次尝试因本机代理 Fake-IP `198.18.*` 被拒绝，
没有放宽共享防护；缺 Chromium 时明确失败，安装运行资源后验收成功。
所有 scratch 用户、membership、token、MinIO 对象/bucket、Redis key 和 HTTP 监听都在退出时清理。

核心提交 `691a889c` 门禁：`make verify` 通过（Ruff、8 个分层契约、async DB 检查、mypy 126 文件、
3324 unit）；带外部 URL 选项的 `make integration` 249 passed、无 skip。
隔离 HTTP 验收内实际运行 `make smoke` 通过；URL、真实浏览器防绕过、附件可信恢复与
模型输入捕获均在当前树复跑。未将旧运行结果当本次门禁证据。

2026-10-02 收尾再次 fetch，上游 `origin/main` 仍为上述 SHA。前端
`5c7a77382cb87400bf70ff492b1b310cb89ce3bd` 对后端
`691a889c` 的真实浏览器隔离验收完成。ExplorePage 文件选择器上传 TXT/PNG，实际
conversation/completion 的 FileService 解析结果进入 system prompt/images；MCPChatPage
上传后只传两个字符串 ID，经 Canvas 解析进入模型边界。structured 分支用临时入口挂载
相同 useMcpUpload/streamStructuredChat；模型 provider 为边界假件，未验工具配置 UI
或执行外部 MCP 工具。401 后恢复凭据重试、即时取消无可发送 ID、具同等模型配置的
第二 owner/missing ID 流前 400、普通/structured 错误非零无成功完成帧及非流式 500 通过。
独立 MinIO 逐字节/owner/size/可信描述核对 10 个前端对象和 2 个 SDK 对象；隔离库 10 张相关
SQL 表均为 0 行，测试桶/对象/描述清除，专用 Redis 清空后容器和匿名卷移除，四个自有监听关闭。
主浏览器 origin 存储清空；旧错误/失联标签页因策略或超时未强行处理，测试账号与有效凭据已失效。
当前两仓活动调用核对未见旧别名消费者；本项仅移除该别名，保留 REST/SDK、
`/v1/document/upload_and_parse` 及 Agent 共用 helper，相关单测改为退役断言。

退役收尾门禁：本轮 `make verify`（Ruff、8 个分层契约、async DB 检查、mypy 126 文件、
3323 unit）与 `make integration`（249 passed、无 skip）通过。后者在隔离 HTTP API 内
实际运行 `make smoke` 通过；旧路径返回 HTTP/code 404、data=null，不写对象，路由与
OpenAPI 均无该别名。REST JWT 单文件/API Key 多文件及 SDK `files` 继续 code=0，
逐对象独立读回字节、owner/size 和可信描述；MCP 内容消费回归保留。随机测试桶及对象/描述、
scratch 用户/token/membership、Redis key 与自有 API 监听退出时均已清理，scratch DB 由 fixture 删除。
首轮新断言误将统一 404 的 data=null 视作异常，按现有错误协议改为完整响应断言后复跑通过。

当前契约集中见 [HTTP API](../references/http_api_reference.md#上传运行时附件)。
本项未运行远程大模型或完整外部 MCP 工具调用。
没有修改既有 Agent 上传/同步 webhook 的鉴权和事务链。同步 webhook 尚有旧线程池调用
async upload_info 的既有签名问题，本项未为附件路径迁移扩大整个 webhook session 改造。

## c446c403deb749e8e290de83bbf5f18d29f9a265 · PDF bbox 分批与 OCR 裁图懒转换

- 上游：`infiniflow/ragflow` #14385，2026-04-27；核对完整 2 文件 diff。
  2026-10-01 核对 remote 并 fetch，`origin/main` 为
  `519e7d98a5651564d4e35d6648f006cba4baaf4f`。本项从 `eae7f094` 开始，
  下一指定 `61a24a2c` 等待另行派发。

| 上游 diff | 本项处置 |
|---|---|
| `deepdoc/parser/pdf_parser.py` 的 `img_np` 懒转换 | 采纳：OCR 检测仍需要一次图像数组；只在确实缺文本、需要裁图时再执行一次 `np.asarray`，同页多个待识别框共用该数组。保留本地旋转 quad 和乱码回退。 |
| 同文件的 bbox 分批与 `_to_global_boxes` | 采纳并适配：默认 50 页、保留明确范围与 100000 默认上界；大小窗口一律转换为原 PDF 全局 1-based 页码。累计高度跨批连续，文字/表格的全部位置和标签只偏移一次。加载下一窗之前释放整页图像和局部状态；返回裁图已是独立副本。 |
| `deepdoc/vision/layout_recognizer.py` 的 DLA 提前初始化 | 采纳配置选择及提前初始化控制，覆盖 `DEEPDOC_URL` 和旧 `TENSORRT_DLA_SVR`，并使现有 `forward` 走同一客户端。两仓与目标 tree 均缺 `dla_cli.py`，后续历史也没有可信协议实现：不采纳或编造远端传输实现，缺客户端时在本地模型/下载前明确失败，本地模式保留。假客户端只用于测试分支和后处理，不是远端可用证据。 |

`self.outlines = extract_pdf_outlines(fnm)` 是目标 diff 的原有上下文，本项保留其兼容性，
没有将其记作新增书签能力。本仓的 `2846a939` 页上界与明确范围、`0d87ceca` 书签路径
继续保留。上游后续没有修复小窗口与分批窗口页码不一致的问题，本项统一修正；
`26ce0ed54` 另对超大 OCR crop 做 OpenCV 尺寸限制，本项未提前移植该独立优化。
布局 recognizer 后续的垃圾门槛、科学 PDF 图像合并和 Python 删除不是本项目标范围。

必要本地适配：`core/flow/parser/pdf_chunk_metadata.py` 多栏排序改读首个选中页面的
`bbox_page_width`，避免从最后一批首张图取混合页幅的错误宽度；最终裁图就绪后不要求
整页图像仍存在。旋转表格重 OCR 原有代码混用 `page_from`，改为在窗口内使用
1-based 局部页码；最后统一偏移。跨页文字和表格保存每页的位置，不仅保留最后/首个页。
窗口重置清理文字、语言判断、布局、表格、图像及 PDF 引用；真实混合扫描/文本测试
证明下一窗能再次用嵌入文字。进度把批内 OCR/布局/表格反馈映射到整体范围，单调到 1。
空范围直接返回，无法确定总页数时明确失败，避免默认 100000 上界跑空窗。
运行选项在新 `common/deepdoc_config.py` 类型化读取正常应用配置，兼容上游环境名，
正整数校验与显式空 URL 语义见 [DeepDOC 运行说明](../../deepdoc/README_zh.md#pdf-bbox-分批与运行选项)。

验证：真实 53 页混合页幅 PDF，在批大小 1、7、50、100 下对照全量与非零范围
`[3,53)`、`[49,53)`；页号/末页、文字、布局、位置、标签、累计高度、裁图尺寸与
像素摘要完全一致，书签含第 51 页。弱引用观测当前窗口最多 7 张整页图像，返回时已释放。
另覆盖复用解析器、空范围/计数失败、无文本数组的文字页、同页两框只转换一次、
扫描→文字窗口、跨页文字/表格、旋转表格局部页码、多栏排序，以及两种远程环境名、
标准配置优先级、缺客户端失败和本地模型/下载回退。远程选择的测试明确禁止执行
本地模型构造和下载；没有真实远端推理验收。

独立进程渲染对照：同一 121 页 PDF，612×792 点，72 dpi（612×792 像素），
OCR/布局/表格推理与合并使用假件，PDF 渲染、文字提取、bbox/裁图真实执行。
三种窗口均返回 242 个 bbox、末页 121，包含裁图像素的结果摘要一致
（`3b3eb4a80d938e84b4a74f30aee9d8b0b1312efcae83859ca9489c08c605d614`）。

| 批大小 | 整页图像驻留峰值 | RGB 整页像素 MiB | 初始/峰值 RSS MiB | 解析秒数 |
|---|---:|---:|---:|---:|
| 7 | 7 | 9.71 | 581.80 / 703.62 | 0.60 |
| 50 | 50 | 69.34 | 574.88 / 796.89 | 0.40 |
| 1000（本样本全量窗） | 121 | 167.80 | 571.73 / 838.67 | 0.40 |

最终代码复跑批大小 7：结果摘要相同，驻留仍为 7 张，初始/峰值 RSS
564.62 / 710.73 MiB、解析 0.59 秒；单次 RSS 受依赖与运行环境影响有波动。
这些是单次本机进程观测，初始 RSS 含依赖加载，峰值还包括 bbox/裁图副本和 PDF 对象，
不能用整页像素上限代替进程总内存上限。复现命令入库到
`tests/manual/pdf_bbox_batch_memory.py`，每种批大小应在独立进程运行。
另用本地模型执行 2 页、72 dpi、每批 1 页的文本 PDF，实际 OCR/布局与文本合并
成功返回页 1–2 的 bbox；这不是大型高分辨率完整 OCR 或复杂长表的模型压力验收。
flow 的 preview restore 仍全文渲染 216 dpi；全部输出裁图随返回列表驻留，跨批文本/长表
合并粒度也可能变化，本项不声称完整 flow 的内存压力或跨批合并语义已全部解决。

交付：最终代码 `make verify` 通过（Ruff、8 条 import contracts、async DB 门禁、
mypy 125 个源文件、unit 3256 passed）；由于 bbox 位置会进入后续索引元数据，
另跑 `make integration`，246 passed、无 skip。没有更改启动、路由或健康检查，
未加跑 `make smoke`。文档链接、scoped diff 检查通过；没有修改 web、Go、上游工作树
或其他会话的 Channel 文件。

## 4303be223fba929fe2982249ce6faafd764cd1b3 · 保留升级后的元数据 Schema

- 上游：`infiniflow/ragflow` #14383，提交于 2026-04-27；完整 diff 仅含
  `rag/svr/task_executor.py`。2026-10-01 fetch 后 `origin/main` 为
  `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
  从本地 `c1c24fe1` 开始处理，本项止于该 SHA，下一指定 `c446c403` 等待另行派发。

上游修复 v0.24 → v0.25 升级后 `list(metadata_dict)` 误把 JSON Schema 变成键名
列表的问题。本仓原链已用 `turn2jsonschema` 接受 schema，但没有合并
`built_in_metadata`。现由 `common.metadata_utils.build_metadata_config` 在每次解析
构建有效配置；缓存读取、缓存写入和生成 schema 都来自这份配置。

| 输入 | 当前行为 |
|---|---|
| Schema dict，`properties` 为 dict | 保留 `$schema`、`required`、`additionalProperties`、组合约束及用户属性；把内置字段转换后并入属性，同名内置属性优先。 |
| Dict 的 `properties` 缺失或非法 | 归一为 `{type: object, properties: {}}`，再合并内置属性，不保留失效 schema 的其他约束。 |
| 旧字段 list | 拼接内置字段 list，保持既有列表转 schema 规则。 |
| 其他类型 | 使用内置字段 list。关闭提取或最终无属性时跳过模型和缓存，不写入元数据。 |

配置合并和提示词枚举注释使用独立副本，避免修改原 parser_config、跨切片共享的
schema 或缓存键。`gen_metadata` 能处理没有 description 的 enum 属性以及合法的
布尔属性 schema。生成结果从切片中消费并删除 `metadata_obj`，不传给索引；
空模型结果不再触发 KeyError。多切片合并沿用字符串和标签列表规则，原有标量字段
参与最后一轮合并；数字、布尔值、null、对象和结构化列表均可保留。结构化值按原子
值处理，同名已存在时保留先写入的值，避免把 PDF outline 和字符串标签拼在一起。
保存返回 False 时任务报错，不发送元数据完成进度。

后续链核对：`f0cb7a544`、`b36314699` 将上游执行器拆层，合并配置仍进入缓存与生成；
`3eff41361` 修复列表字段的 enum/description 为 null 时失效，本项包含该必要修复；
`e9cace9a0` 修复元数据合并丢弃非字符串值，本项包含对应语义，并保留合法空数组。
没有发现目标修复被回退。`c8d1b21ae` 另增加 file_name/update_time 的确定性填充，
属于独立行为，本项不提前移植；Python 删除与 Go 迁移也不在范围内。

调用链核对：文件上传与 REST 文档创建沿用完整 KB parser_config，
`TaskService.get_task` 从 Document 读取配置；KB/文档设置接受 schema 或旧 list，
文档列表与 KB 详情的 `turn2jsonschema` 消费继续兼容。专用 REST 自动元数据
`fields` 接口仍为列表契约，本项没有将其改成 schema 编辑器。`gen_metadata` 当前
只有标准任务解析这一处调用；dataflow 在索引前聚合并删除临时 `metadata`，复用相同
合并函数，因此也保留非字符串值。`analyze_v2` 的 metadata_fields 是独立提取配置，
没有套用本次 schema 合并。Milvus 配置下经 `DocMetadataService` 写独立
`t_ai_document_metadata` 表；ES/Infinity 继续使用既有 metadata store，本项未改变
其映射或写入协议，也未修改 web、Go 或其他会话的 Channel 文件。

验证：新增单测覆盖 schema 顶层与字段约束、同名内置属性、旧列表、非法/空配置、
null 字段、提示词不改配置、缓存读写参数一致及内置字段变化、空回复、关闭提取、
元数据合并与保存失败。新增 5 个集成场景，在一次性 PostgreSQL scratch 库创建
Tenant/KB/Document/Task，由 `TaskService.get_task` 取任务，真实 MinIO 取件和 naive
文本解析，经真实 Redis 缓存、实际元数据 service 写入，再用独立 Session 和表记录
读回。Schema、旧 list、非法 properties、仅内置字段、空配置均覆盖；第二次解析
命中缓存，修改文档内置字段后重新生成并读回新增字段。模型和模型配置查询使用假件，
未调用真实 LLM，也未运行后续 embedding/向量索引。临时记录、对象、bucket、缓存键
及 scratch 数据库均在验收后清理。

交付门禁：`make verify` 通过（Ruff、8 条 import contracts、async DB 门禁、
mypy 124 个源文件、unit 3229 passed）；`make integration` 通过（246 passed，
无 skip）。本项不涉及启动流程、路由或健康检查，未加跑 `make smoke`。
ES/Infinity 未做本次真实后端写入验收；本次解析与持久化运行证据对应 Milvus 配置下
的 SQL metadata store。独立 web 工作树保持干净，scoped diff 与文档路径检查通过。

## d88f7ac8d2a573997d8a9c46e077ff068cbb38b4 · 删除未使用的旧评估与 KB 入口

- 上游：`infiniflow/ragflow` #14394，提交于 2026-04-27，父提交为 `290f0294`。
  2026-10-01 核对 remote 并 fetch；`origin/main` 为
  `519e7d98a5651564d4e35d6648f006cba4baaf4f`。核对目标 3 文件完整 diff（1500 行删除）。
- 初次提交 `1e25c6a0` 依旧兼容规则仅标记评估 API 为 deprecated。用户确认应删除
  查明无调用的接口后，本项收敛为实际删除，并更新共用 Skill 的逐接口退役判据。
  本项止于这个 SHA，后续提交单独派工。

| 上游 diff | 当前处置 |
|---|---|
| 删除 `api/apps/evaluation_app.py`（479 行） | 删除本仓同名文件，移除全部 17 个 `/v1/evaluation` 操作。web、可见 SDK、MCP、HTTP/benchmark 和后台均未发现消费者；用户明确下线这个未使用的入口，无需新增替代 API。评估 service、4 张数据表和历史数据保留；入口退役没有执行数据清理。 |
| 删除 `api/apps/kb_app.py`（446 行） | 上游父提交中 10 个旧操作已在三引号注释内。本仓删除对应 10 个 handler、6 个专用请求模型及无用导入，清理其旧引用和 async DB 基线条目。剩余 16 个本地扩展入口保留，包括 web 实际使用的文件日志能力。 |
| 删除 `test_evaluation_routes_unit.py`（575 行） | 本仓没有该 Quart 假件测试文件。保留已有 REST/鉴权/存储回归，增加真实 FastAPI 27 个已移除操作的 404、OpenAPI 边界及文件日志业务响应/鉴权测试。 |

已移除的评估入口（路径前缀 `/v1/evaluation`）：

| 操作组 | 删除范围 |
|---|---|
| 数据集（5 个） | `POST /dataset/create`、`GET /dataset/list`、`GET/PUT/DELETE /dataset/{dataset_id}` |
| 案例（4 个） | `POST /dataset/{dataset_id}/case/add`、`POST /dataset/{dataset_id}/case/import`、`GET /dataset/{dataset_id}/cases`、`DELETE /case/{case_id}` |
| 运行（5 个） | `POST /run/start`、`GET /run/{run_id}`、`GET /run/{run_id}/results`、`GET /run/list`、`DELETE /run/{run_id}` |
| 分析与导出（3 个） | `GET /run/{run_id}/recommendations`、`POST /compare`、`GET /run/{run_id}/export` |

上游的 `POST /evaluate_single` 在本仓从未注册，不新增空成功桩。知识库 REST 数据集
不承接评估数据集；本次下线不声称评估能力已迁移。旧评估导入和 `run/list` 的已有问题
不再通过 HTTP 暴露，保留的 service 没有在本次任务中重构。

已移除的 KB 入口（旧前缀 `/v1/kb`，替代前缀 `/api/v1`）：

| 旧操作 | 当前替代 |
|---|---|
| `POST /create` | `POST /datasets` |
| `POST /update` | `PUT /datasets/{id}` |
| `POST /list` | `GET /datasets` |
| `POST /rm` | `DELETE /datasets`，请求体 `ids` |
| `GET /{kb_id}/knowledge_graph` | `GET /datasets/{id}/graph/search` |
| `DELETE /{kb_id}/knowledge_graph` | `DELETE /datasets/{id}/index?type=graph`；新契约同时处理任务绑定 |
| `POST /run_graphrag`、`GET /trace_graphrag` | `POST/GET /datasets/{id}/index?type=graph` |
| `POST /run_raptor`、`GET /trace_raptor` | `POST/GET /datasets/{id}/index?type=raptor` |

消费者核对：独立 web `fa30aef2` 工作树干净，`knowledge.ts` 已用 REST 管理 KB，
`knowledge-index.ts` 已用统一索引 API；上述 10 个旧操作及评估 API 均无调用。
文件日志页面 `use-log-list-state.ts` → `knowledge-ingestions.ts:listFileLogs` 仍调用
`POST /v1/kb/list_pipeline_logs`，现有 REST 摄取列表不含这些文件下载日志，因此保留
该能力。两个可见 SDK checkout（`multirag-python-sdk-v1`、`multirag-rest-first-python-sdk`）、
MCP、HTTP 参考和 Go benchmark 使用数据集接口；Python benchmark 直接使用知识库
service 和检索器。未发现后台导入被删 handler 或通过其他入口调用评估 service。

Go 核对：旧 KB update/graph 有独立的鉴权 router、handler/service；CLI 还有 KB
标签与元数据请求。评估实体参与 DAO 初始化，没有评估 handler/service。目标没有 Go diff，
本次 Python API 删除不修改 Go 的独立实现。

后续链：两份旧 API 文件未恢复；`faf77a5a8` 后来补评估 token usage，
`a0e65637e` (#16614) 再删评估 service，`670e68872` 移除更多 Python API。
本项只跟进 API 删除，不提前删除本地 service、数据表或迁移到 Go。

验证：删除后的 `make verify` 通过（Ruff、8 条 import contracts、mypy 124 个源文件、
unit 3207 passed）；async DB 基线随被删图谱路由收缩 1 条，存量由 58 降为 57，
门禁仍检查新增违规和过期条目。`make integration` 通过（241 passed）。
一次性 PostgreSQL scratch 库启动真实 HTTP API，`make smoke` 通过，全部健康组件 `ok`。
实际请求逐项确认全部 27 个旧操作返回 404；OpenAPI 无评估入口，KB 保留 16 个操作。
文件日志业务码 0、匿名请求 401；REST 数据集创建/列表/详情/更新/删除、空图谱读取、
GraphRAG/RAPTOR 状态读取与解绑成功，无文档的索引启动返回明确非零业务码。
写入、删除和解绑均独立 SQL 读回，预置评估数据行仍存在。未运行真实模型驱动的完整
GraphRAG/RAPTOR 构建；任务排队、分派与权限由既有单元及集成回归覆盖。
AST 对比确认剩余 KB handler 与请求模型行为未改；文档路径、链接和 scoped diff 检查通过。
临时进程和数据库已清理，本项未修改独立 web 工作树。

前端交接：上述旧操作的调用方已迁移，本项无需前端修改。将来退役其余 KB 本地入口
时逐项查实际调用；文件日志需先建立等价 REST 契约，再迁移 `listFileLogs` 及页面消费。

## 290f0294d6e043f64fb1c79b5780421cfc48d045 · 沙箱产物下载迁移到 REST

- 上游：`infiniflow/ragflow` #14348，提交于 2026-04-27；核对目标提交的 4 文件完整 diff。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
  本项止于此提交；下一指定 `d88f7ac8` 单独处理。

| 上游 diff | 本项结论 |
|---|---|
| `agent/tools/code_exec.py` | 产物 URL 改为 `/api/v1/documents/artifact/{filename}`。上传时把随机文件名精确绑定到可信用户、本次运行 ID 和可选会话 ID，URL 携带 `run_id` 与可选 `session_id`。绑定失败则尝试移除对象且不输出不可下载链接。 |
| `api/apps/document_app.py` 删除旧入口 | 本仓有旧调用方，保留 `/v1/document/artifact/{filename}` 并标为 deprecated；旧新路径共用鉴权、绑定校验、文件名白名单、原始字节和安全响应头。 |
| `api/apps/restful_apis/document_api.py` 新增入口 | 新增 `/api/v1/documents/artifact/{filename}`，由异步请求会话和当前用户身份校验；实际下载在工作线程，HTML/SVG 强制 attachment。 |
| `web/src/components/next-markdown-content/index.tsx` 更新链接识别 | 前端属于独立 `../web` 仓；其当前 `6894adf` 已在 `src/lib/agent/artifact-url.ts` 和 `src/components/chat/MarkdownArtifact.tsx` 识别新旧路径并保留查询参数。本项未编辑前端仓。 |

后续链核对：`212429bf9` 增加上游基于助手消息文本的归属检查，但文本可被提示诱导
复述已知的其他用户 URL，不能作为文件所有权凭据。`93f6d647d` 为流式预览加
`session_id` 兜底；仅验证该会话可访问仍不能证明指定文件属于该会话。本仓采用上传时
登记的精确文件、用户、运行和会话绑定，下载不读取助手消息文本。Agent SSE 在
`canvas.run` 完毕后才持久化助手消息；精确绑定使流式上传后、落库前即可下载。
没有持久化会话的调试运行按用户、文件和运行绑定授权。历史上未登记的裸链接无法
安全证明归属，不能继续下载。登记按沙箱产物保留期到期；对象沿用现有生命周期配置，
配置失败时可能滞留，但没有登记仍不可下载。

前端对接：保留 `?run_id=<运行 ID>`，会话运行还保留 `&session_id=<会话 ID>`；
不要只凭 `session_id` 构造其他文件 URL。新旧地址都用同一凭据拉取原始二进制，
401 和非零业务码均当失败处理。独立 web 仓的 `artifact-url.ts`、
`MarkdownArtifact.tsx`、`markdown-artifact.test.ts` 和单步调试附件列表是对应联调点；
当前前端单测只覆盖 `session_id` 查询参数，完整 `run_id` 链接尚待前端仓补契约测试与联调。
另需修正前端 `fetchArtifactBlob` 对合法 `.json`、`.html` 附件的处理：它调用的通用
`assertPreviewResponse` 将 `application/json`、`text/html` 一律视为错误，当前这两类
附件即使后端返回正确原始字节也无法在页面下载。合法附件带 `Content-Disposition`
文件名，业务错误 JSON 没有；应结合已识别的产物 URL 与响应头区分业务错误、登录页
和合法附件，再做对应回归；本项未编辑独立前端仓。

验证：定向单元与 scratch PostgreSQL/Redis 测试 15 passed，覆盖流式落库前放行、他人和
错运行拒绝、助手复述他人 URL 拒绝、会话删除后的拒绝。一次性 API + scratch PostgreSQL +
真实 MinIO 运行 CodeExec 上传方法，并验收新旧路由有效令牌原始字节、无令牌和他人令牌、
错误路径、伪造助手引用、删除对象后的读回；临时对象、登记、数据库和进程均清理。
`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3177 passed），
`make integration` 通过（241 passed），`make smoke` 通过。真实模型驱动的完整 Agent SSE
未运行；落库前时序由独立会话行、CodeExec 上传和真实下载组合验收。

## 2846a939981b41e155ef9975727bfb0e7f7a0ca8 · 修正大型 PDF 页数截断

- 上游：`infiniflow/ragflow` #14382，提交于 2026-04-27；按目标 SHA 核对完整 24 文件 diff。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
  此项只移植 Python 解析与任务链；下一指定提交 `290f0294` 留待单独处理。

| 上游 diff | 本项结论 |
|---|---|
| `common/constants.py`；`deepdoc/parser/{pdf,docling,mineru,opendataloader,paddleocr,docx}_parser.py` | 增加解析页上界 `100000` 与独立的任务标记 `100000000`。DeepDOC、Vision 默认不再在第 299 页截止，Docling、MinerU、OpenDataLoader 不再默认在第 600 页截止；PaddleOCR、DOCX 同步统一默认值。DeepDOC 的 `parse_into_bboxes` 接收并传递明确页范围，文字提取失败时只为实际渲染的页分配空列表。纳入后续 `3a829fb6d` 的 Vision 指定页范围原页码修正。 |
| `rag/app/{book,email,laws,manual,naive,one,paper,presentation,qa,resume,table}.py` | 对应本仓 `core/app/` 同名模块，统一“全部页”默认值与 DOCX/PPT/Excel 调用处的哨兵；保留现有解析模式选择和显式页范围。 |
| `api/db/db_models.py`、`api/db/services/{document,file,task}_service.py` | 统一任务表默认标记、排队分页区间、非分页任务和复用任务判断；本仓文档/文件直接解析入口沿用解析页上界。 |
| 两个上游 SDK route 测试文件 | 仅调整其整包伪造的 `common.constants` 测试桩；本仓没有同类测试桩，不复制。新增本地真实 PDF、任务分片和 scratch DB 回归。 |

本仓还有 `document_analysis_service.py`、`pipeline_analysis_service.py`、
`guard_detection_app.py` 和 `core/flow/parser/parser.py` 的直接解析入口，已同步页上界；
`core/svr/task_executor.py` 的非分页任务进度标记也同步使用任务常量。
`core/flow/parser/parser.py` 的 DeepDOC bbox 路径未配置指定页范围，当前使用新默认值。

后续链核对：`3a829fb6d` 修正 Vision 指定范围的原始页码，本项已纳入；
`c446c403d` 再将 bbox 解析分批并延迟加载页面图像，是单独的内存优化，当前 302 页
轻量 PDF 验收不代表高分辨率完整 OCR 对超长 PDF 的内存压力已解决。
`81361c210` 后续修正 MinerU API 对页范围的转发；本仓现有 MinerU API 模式
仍将页码固定为全量，Docling/OpenDataLoader 调用链也尚未转发显式范围，
这些后端的范围控制不能用本次 DeepDOC/Plain/Vision 验收结果推断。

验证：生成 302 页轻量 PDF，实际经 `pdfplumber` 渲染、提取并读回末页 `PAGE302`；
另验证第 301–302 页指定范围、短 PDF、提取失败时的回退列表长度、Vision 原页码、
任务切分覆盖末页及 scratch PostgreSQL 中的非分页任务标记。
`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3163 passed），
`make integration` 通过（240 passed）。未运行完整 DeepDOC OCR/版面模型及真实外部解析服务。

## c3eac4103a0408f9b8d25948e625e58821b5d54a · 阿里云 Go 模型提供商

- 上游：`infiniflow/ragflow` #14379，提交于 2026-04-27；父提交为上项 `0b46ab07`。
  核对了目标的 9 文件完整 diff。2026-09-27 fetch 后 `origin/main` 为
  `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
- 本项已移植到 Go 并行实现。本仓 Go 服务从 `configs/models/` 加载提供商；
  Python 从独立的 `configs/llm_factories.json` 加载模型，已有 `Tongyi-Qianwen`
  和 DashScope 调用实现，本项没有改 Python 代码或共享配置。

| 上游 diff | 本项结论 |
|---|---|
| `conf/models/aliyun.json` | 映射为 `configs/models/aliyun.json`，保留初版 `Aliyun` 名称、`qwen-flash` 和三个地域；模型列表 URL 采用后续 `a75e733b3` 修正的 OpenAI 兼容路径。初版 `series: "deepseek"` 与模型不符，不带入。Embedding/Rerank 的模型目录和能力属于后续提交。 |
| `internal/entity/model.go` | 将模型/提供商的 `Series` 改为后续 `f670913bb` 确立的 `Class`，同时迁移本仓六份使用 `series` 的 Go 提供商 JSON，保持原有类别值。模型名无 `-` 时保留本仓安全推断；提供商显式 `class` 值直接传给模型，不照搬初版取提供商名称的错误。 |
| `internal/entity/models/common.go`、`internal/entity/models/types.go` | 思考解析参数与 `ChatConfig` 同步使用 `ModelClass`；原有空指针处理和 `qwen3` 解析语义保留。 |
| `internal/entity/models/factory.go`、`internal/service/model_service.go` | 工厂注册 `Aliyun` 驱动；聊天服务继续从模型目录把类别传到驱动配置。 |
| `internal/entity/models/gitee.go`、`internal/entity/models/siliconflow.go` | 思考解析调用同步传 `ModelClass`，不改变两家原有请求协议。 |
| 新增 `internal/entity/models/aliyun.go` | 实现单消息和多角色同步聊天、sender 流式聊天、模型列表及连接检查。修正初版 `Name()` 误报 `siliconflow`、流式默认 `false`、未知地域产生空 URL、默认 scanner 丢弃大事件、异常结束仍发送 `[DONE]` 等问题；对 HTTP/业务错误显式返回错误。Embedding、余额和旧 channel 流式接口明确返回不支持，不伪装成功。 |

后续链核对：`effc84a04` 重构 Go 模型服务，`f670913bb` 将初版 `Type` 改为
`Class`；`a82ae4a99`、`2ad854c58` 后补 Embedding 和 Rerank，`827cceccb`
修正驱动名称及未知地域 URL，`04aa8d04e` 扩大 SSE 缓冲区，`a75e733b3`
修正模型列表路径。当前上游主线又迁移到共享 `BaseModel` 和 `Tongyi-Qianwen`
提供商名；这些架构及名称变动不在本次提交范围。

验证：用本机已有的 `golang:1.25` Docker 镜像运行 `gofmt`，
`go test ./internal/entity/models ./internal/entity`、`go build ./internal/...`、
`go vet ./internal/...` 均通过。mock HTTP 测试覆盖提供商注册、类别推断、
同步/多角色消息、地域、128 KiB 流式事件、`[DONE]` 和异常响应。
额外的 `go test ./internal/service` 因缺少 `internal/cpp/cmake-build-release/`
下的 C++ tokenizer 静态库而在链接阶段失败；本项没有修改该 native 构建链。
未配置真实 DashScope 凭据，因此没有对外部服务发 live 请求；Python 文件未改，
不运行 Python 门禁；已检查本次 diff、路径和链接。
下一指定提交 `2846a939` 不在本项范围。

## 0b46ab07c59eb715cbb4c1623724a11bda57b398 · 恢复 OpenAI 兼容聊天补全

- 上游：`infiniflow/ragflow` #14380，提交于 2026-04-27；核对目标的 10 文件完整 diff。
  2026-09-27 fetch 后 `origin/main` 为 `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。

| 上游 diff | 本项结论 |
|---|---|
| 新增 `api/apps/restful_apis/openai_api.py`，从 SDK session 迁移 `/chats_openai/{id}/chat/completions` | 新建同名 FastAPI 路由模块，提供 `/api/v1/openai/{id}/chat/completions`；旧路径由同一 handler 保留并标为 deprecated。使用已有 API Key 异步鉴权和请求级 `AsyncSession`，按租户及有效状态读取聊天助手。`/chats/{id}/completions` 是本仓仍有调用方的原有契约，本项保留；`/chat/completions` 是另一种统一会话 API，不复用为 OpenAI 协议入口。 |
| `model` 占位符及指定模型 | `"model"` 使用助手配置；具体模型从当前租户的聊天模型记录验证并选择其 `tenant_llm_id`。对本次请求使用深拷贝的 Dialog，避免 ORM 自动 flush 将覆盖模型写回助手。响应中的 `model` 是实际使用的模型名。 |
| `messages`、同步 JSON、SSE、引用及元数据 | 校验消息角色、最后一条用户消息与文本内容；文本数组拼接，非文本内容显式报参数错误。新路径省略 `stream` 时默认非流式，旧别名保持默认流式。SSE 只发增量正文，完整最终正文放扩展字段 `final_content`，末帧携带 usage 和可选引用后发 `[DONE]`；错误帧不伪装为成功结束。引用元数据经当前异步会话的 `run_sync` 查询，可筛选字段；`metadata_condition` 无匹配时传 `-999` 防止退化成无过滤检索。Python OpenAI 客户端会将 `extra_body` 合并到 JSON 顶层，因此同时支持顶层和历史嵌套格式。 |
| HTTP/Python 参考与 benchmark 路径 | 更新本仓 HTTP 参考、OpenAPI 筛选示例与稳定路径映射；本仓没有上游 `python_api_reference.md` 和 benchmark 脚本，不复制文件。实际用本机 OpenAI SDK 的 MockTransport 核对 URL 拼接：`base_url` 须止于 `/openai/{chat_id}`，上游文档末尾再加 `/chat` 会得到重复的 `/chat/chat/completions`。 |
| HTTP 测试 helper 的 related questions 路径、音频单测及旧 session 测试删除 | 本仓已有 `/searchbots/related_questions`，旧 `/sessions/related_questions` 保留兼容；音频路由在本仓独立实现且本次生产 diff 未改动，不复制上游 Quart 测试。使用本仓 FastAPI 测试覆盖新旧路由、鉴权、模型、同步与流式响应、引用、元数据及错误语义。 |

后续链核对：`bd6251f46` 将新路径默认响应改为非流式，`09d0a1745` 处理数组消息内容，
`5b02fe484` 消除流式最终答案重复；本项纳入这三个必要修复。
`e6dd39753` 的 session ID 改动由 `bb148edf4` 回退，未纳入；
`a75ea7ba7` 的生成参数覆盖、`3bfad1f00`/`6a77523bf` 的后续模型映射及
`24af0875e` 的引用元数据展示配置属于后续独立行为，未提前移植。
本地适配额外修复上游引用元数据 helper 缺少数据库参数、流式异常仍发 `stop` 的假成功，
以及 SDK `extra_body` 实际在顶层的请求形状。下一指定提交 `c3eac410` 不在本项范围。

验证：定向路由单测 23 passed；`make verify` 通过（8 条 import contracts、mypy 124 个源文件、unit 3156 passed），
`make integration` 通过（239 passed）。隔离 PostgreSQL scratch 库启动真实 HTTP API，
`make smoke` 通过（ping/healthz 全部组件 `ok`）；新路由非流式、SSE `[DONE]`、旧别名默认流式、
缺失 API Key 401、未知模型业务码 101、指定模型响应均通过实际请求，独立查询确认 Dialog 模型未改变。
回答生成在隔离进程中用固定模型桩替代外部 LLM；真实模型推理与真实检索引用元数据没有端到端运行，
对应路由行为由定向测试覆盖。临时数据库和进程已清理。

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
| `api/apps/kb_app.py` 删去旧路由与相应旧测试 | 当次将删除范围内仍存在的旧路由标为 deprecated，新行为在 `/api/v1/datasets` 实现。其中后续 `d88f7ac8` 涉及的 10 个操作现已删除，见该项最新记录；其余本地入口按实际调用逐项退役。 |
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

## 6a23dfeec1632d25736fcbff33cc9fb2a53d4e1c · 共享 UI 组件目录约定（仅审查）

- 上游：`infiniflow/ragflow` #14381，提交于 2026-04-27；完整 diff 只有
  `web/CLAUDE.md` 新增 8 行。2026-09-27 fetch 后 `origin/main` 为
  `313ca90f6abd7682fe8523e16fd67b3653a3fa84`。
- 这是一条前端协作约定，不改运行代码、API 或协议；MultiRAG Python 后端没有可移植项。
  本仓 `AGENTS.md` 是后端协作规则唯一入口，`CLAUDE.md` 只映射它，
  不把独立前端的目录保护规则复制进后端指令。

| 上游 diff | 独立 `../web` 仓的评估交接 |
|---|---|
| `web/CLAUDE.md` 的 Shared UI Component Lock | 将整个 `src/components/ui/`（含子目录）视为共享组件库；常规需求不直接修改、重构或改样式。先在目录外的 `src/components/` 或功能目录包装、组合，通过 `className`、props 等定制。确需修改现有共享组件，应先取得当前会话用户明确许可；新增共享组件或用 shadcn CLI 升级原语，仅在用户明确要求时做。 |

后续链核对：上游 `cad7c46be` 把 `web/CLAUDE.md` 原样改名为
`web/AGENTS.md`，删除根目录单行 `CLAUDE.md`；当前主线的锁定条款仍在
`web/AGENTS.md`，不是行为回退。本地 `../web` HEAD 为 `0834bb9`，
`src/components/ui/` 确有共享原语和项目组件，但本地
`AGENTS.md`、`CLAUDE.md` 尚无此锁定条款。两份本地文件明定为同一规则集的
中英文版本：若前端任务决定采纳，应在**同一次前端提交**同步中文和英文，
并核对现有“`src/components/ui/` 仅原子组件”的分层措辞，避免相互矛盾。
按本次派工，前端会话仍在补 `a9e5724b` 的实际运行验收；本项不跨仓编辑或运行前端门禁，
也不将该工作状态作为本次实测结论。
下一指定提交 `0b46ab07` 不在本项范围。

验证：核对目标完整 diff、上游后续改名和当前条款、本地两个工作树状态、
目录及双语指令；仅更新本记录并检查文档 diff、路径与链接，不运行 Python 或前端测试。
