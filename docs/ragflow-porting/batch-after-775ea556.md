# 775ea556 后续二十项本地集成

冻结范围为排除 `775ea5567948ea12783c514be12abb62defcbe63`，至 `f9ce07ced14640730fc66d0b64ad4608718cae1f` 的 first-parent 连续二十项；直接后修上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。

后端基线 `b3abd777069803d1e380368f7d6bc54b277c9787`；原始证据目录为 `~/.local/state/multirag/validation/20261025-batch-after-775ea556/`。源码组合为 `1c2aa5ea4fce3b9205b2c22ec91b534f04ed7b9e`；此后的收口提交只修改文档。实际 Web master 与 consumer pin 均为 `0f4e3f69814968ae52ec10a82ee3a9d82b27cec8`。最终 main SHA、恢复包和清理读回见外部目录的 `delivery-complete.json`。

|编号|上游|实际处置和质量比较|
|---|---|---|
|1|`c5a46fda`|按实际竞争/取消场景将模块级 semaphore 隔离到各事件 loop；保留闭环清理。|
|2|`1ece1c81`|TogetherAI 复用严格 rerank、multipart ASR、binary TTS 与 base64 SSE 传输。|
|3|`8995662e`|不复制上游发布说明和未经本地实测的版本/性能声明。|
|4|`57ddd791`|本地不存在上游非法锚点；不把发布说明修订当部署修复。|
|5|`ea1764a7`|保留按登记文件名推断 MIME 和先授权再读取；按有效重落 bd49fd70 评估。|
|6|`04bdb419`|缺少或 None 的语言回落 Chinese；显式值按合同保留。|
|7|`b2053cc3`|PPIO factory/catalog/chat 接线，完整15路径质量比较；保留本地已实现能力与严格错误。|
|8|`b2bf9155`|Zhipu ASR 和完整 diff 中 TTS/stream 接线；实例和凭据隔离。|
|9|`6ab25bf7`|头像 helper 逐跳 SSRF、request scoped DNS pin、有界字节/超时、无敏感日志；不新增活动 OAuth 下载。|
|10|`11ff848b`|六个 curl/requests 示例使用本地 REST、业务码、SSE 终止和精确清理；不虚构本地 SDK。|
|11|`a9ec78cb`|GraphRAG 配置、重试、超时、取消、checkpoint 与 writer lease 串行适配；有效 NER 合同。|
|12|`f4e63ef3`|复用现有 CI smoke，补有界 payload readiness 与失败 server log；不重建旧 workflow。|
|13|`c33d0b80`|保留 User 白名单，邮箱仅自身响应；不记录 OAuth state 或凭据。|
|14|`8f90740d`|Agent 对象参数逐入口与每轮副本传递，保留空对象/false/0，经模型 SDK extra_body 接线。|
|15|`b1ef5d36`|OpenRouter bounded JSON base64 ASR；现有 TTS 保留。|
|16|`faf77a5a`|同步评测 JSONB token 用量估算携带 estimated/tokenizer/prompt_source；真实提交与失败零结果读回。|
|17|`ed048934`|TokenPony factory/catalog/chat 与严格发现、推理和 sender 生命周期。|
|18|`71a52d57`|保留原文档和生成附件独立资源、权限、存储来源和当前 Web 活动路由。|
|19|`893980ed`|在真实 Studio/Explore 保存面携带授权元数据解析的 model_type；上游多聊页面本地无活动对应。|
|20|`f9ce07ce`|Groq factory/catalog/chat、推理 fallback 和严格流终止；不扩大冻结范围外媒体能力。|

## 接收与验证

以下候选均已审阅完整差异、原始报告和输入哈希，并串行进入本地主干；#18 的服务端和 Web
由 A/C 共同覆盖。它们不代表远程 CI 或生产版本。

|归属|接受候选|实际边界|
|---|---|---|
|A|`5bc4e313100f52864abc8cff7c56e6818dbc43af`|387 个最新单测结果的并集；18 项受控 HTTP、SQL、对象存储及旧固定 Web 验收。|
|B|`1758480be29156723008edbfc7f74afe19941102`|246 个最新单测结果的并集；首候选 7 项、线程排空后修 3 项真实 SQL/Redis/Milvus 验收；早期未提交快照的 33 项仅作补充。|
|C|`0f4e3f69814968ae52ec10a82ee3a9d82b27cec8`|固定 Web 的 API、产品序列化、hook 和 selector 定向检查、构建；真实 Chromium 保存与 SQL 读回，键盘、明暗主题及附件组件 contact sheet。|
|D|`9f4a235349b4ab1cd207aa8b4add18df7b067b82`|模型/目录 race 2679 个测试和子测试通过事件（2419 个叶子）；实际 JWT/SQL/CLI race 13 个通过事件（12 个叶子）、121 个 CLI 进程、116 个受控供应商请求；build/vet 与三个 cmd 构建。|
|E|`c54020eee542487aff867d42dd052b3d7cc0ee56`|2 项 readiness 单测，4 项示例真实 HTTP/SQL/Milvus/MinIO；覆盖读模式、明确写模式、故障、超时和精确清理。|
|F|`7a4c25edd3b6aaad3abdb0961b7c9ec479d448be`|12 项单测、3 项真实 SQL 提交与失败零结果读回。|

I 在冻结组合上补充持久的 Web 模型保存消费者回归，检查实际生产 serializer/API、
`chat`/`image2text`、合法零值、禁用参数、兄弟助手保全和未知模型无 HTTP 副作用。
评测数据库测试纳入 database 分组，新增 Go opt-in 只登记精确函数，不放宽 skip 规则。

最终定向 Python 单测为 721 项最新结果全部通过：首轮 715 通过、6 失败；原因为 Agent
组件的 `nest_asyncio` 使测试中的 `asyncio.run()` 复用 loop。修复只让测试显式创建和关闭
loop，保留竞争、隔离与取消断言，包含 Agent 加载场景的 138 项复跑全部通过。
失败原始日志和 JUnit 均保留，没有将首轮称为全绿或重复累计复跑数量。

改动 Python 文件 Ruff、format、import-linter、async DB 检查、当前纳管 mypy、
`uv lock --check` 和 diff 检查通过。真实固定 Web 消费者 8 项、core 55 项通过，均零 skip；
两次选择互补的 deselect 分别为 12 和 5，不是缺失服务导致的 skip。
core 包含文件/密码重置、示例、评测 SQL、GraphRAG 重试、checkpoint 生命周期以及 10005 条
NER 原生源快照。原始报告在 `reports/integration/final/`，单测并集为 `unit-coverage.json`。
Go 的 653 个源码/配置/锁文件输入与 D 的最终候选逐项相同，复用上述已接受报告；
I 对当前源码重新链接 CLI、主服务和管理服务三个 cmd，均成功，二进制摘要单独保留。
native 静态库复用另核对 135 个源码输入、编译器/CMake/PCRE2 和固定字典清单，
不把历史测试次数算成本次执行。来源见 `integration/native-reuse.json` 和 `go-reuse.json`。

## 质量取舍与限制

- #7 完整比较 15 个上游路径；#8 包含 TTS/stream，不缩成 ASR；#14 包含普通、结构化、流式及工具全部轮次与 fallback。
- #11 后修 `10e86908` 的 NER string-list、无 batching 已等价；完整审阅 `c5862929` 后保留本地图/config 修订绑定的 checkpoint 和现有模型缓存。未引入缺少图内容、模型与 prompt 修订的 Redis 子阶段缓存；未完成阶段可能重算。同步线程排空可能超过配置的阶段超时，见 [GraphRAG 执行说明](../../core/graphrag/README.md)。
- #5 按有效重落 `bd49fd70` 评估，不恢复先读存储或按请求名猜 MIME；#18 保留两个不同资源的权限、来源和下载路线，见 [文件合同](DOCUMENT_FILES.md)。
- #3/#4 本地没有对应发布说明和非法锚点；#19 本地没有上游 multi-chat 活动页面，实际落到 Studio/Explore。没有为凑条目新增死页面或虚构 SDK。
- A/B 的首次收集、beartype、fixture 和环境问题保留首错及定向补跑证据。候选独立测试和最终组合测试分开；受控 SDK/供应商 HTTP 不等于真实付费服务。
- 旧模型消费者 fixture 原先仅在进程内断言清理；I 补充外部 socket 及 ORM flush 资源登记后复跑该项。两份补充通过回执保留，最终明确登记 3 个 owner 标识与 4 个监听端口，不把复跑加入 8 项独立用例数。
- GraphRAG 验收使用生产入口和真实队列/Task SQL，但在进程内调用 worker，未运行常驻外部队列消费者。C 的 Chromium 使用同一 Web 提交及旧后端基线；最终后端组合重跑真实 HTTP 客户端，未重新声称完整浏览器/Agent 图验收。Web 没有新增/删除测试文件，按本次影响面执行定向检查和构建，未运行完整 `test:ci`。
- 本地借用既有 venv 和配置，无共享配置改写或依赖同步；实际解释器、550 个包版本、配置摘要及服务版本随报告保存。未覆盖完整 Python/Go 通用门禁、真实模型质量、远程 CI、部署或生产。Web pin 尚未推送，远程 consumer checkout 仍需先发布该提交；未 push、未部署。

## 保全与恢复

原始 28 项 WIP 保留；验收针对隔离目录中的已提交组合，不包含主目录未提交的其他工作。
共享 PROGRESS 仅暂存本批新增块，其余 WIP 不进入提交。
所有候选接收时已独立确认各自 scratch 数据库、对象桶、集合、Redis 标识、监听端口及临时资产清理。
最终仅删除本批登记的普通 worktree/分支和运行资产，保留七个历史 managed worktree、共享服务、借用模型、venv 和字典。
恢复 bundle 对照全部主干和候选 refs，执行 bundle verify、全新 bare clone 与 connectivity fsck；
准确哈希、清理读回及最终 main 由外部 `delivery-complete.json` 绑定。
