# 测试执行与证据

通用交付门禁以 [AGENTS.md](../AGENTS.md#验证) 为准。单元与集成在独立进程运行，避免单元测试的资源假件污染真实服务；`make test-all` 顺序执行两者。模型质量回归使用独立命令和报告。

## 执行范围

| 命令 | 范围与依赖 |
| --- | --- |
| `make integration` | 已审核核心文件默认双进程；包括 SQL、真实 HTTP、存储、受控模型及恢复测试 |
| `make integration TESTS=tests/integration/test_async_engine.py` | 仅指定文件；也支持 `文件::用例`，在导入应用前按文件准备依赖 |
| `make integration-db` | 已确认仅需 PostgreSQL 的文件，不收集 HTTP、OCR、向量库测试 |
| `make integration INTEGRATION_WORKERS=0` | 串行调试；显式 pytest `-n 0` 也会覆盖默认并行数 |
| `make integration-system` | 数据库进程事务恢复，以及真实 worker 被终止后的消息重投、取消恢复 |
| `make integration-infinity` | Infinity 真库契约；可用 `INFINITY_TEST_URI` 明确选择实例 |
| `make integration-consumer` | 独立 Web 检索、导入日志、模型配置与生成文档下载客户端；需要已安装依赖的 `WEB_DATASET_CHECKOUT` / `WEB_COMPLETION_CHECKOUT` |
| `make integration-all` | 核心、Infinity 和 Web 消费者全部执行 |
| `make eval` | 固定版本本地 OCR、中文向量模型、解析与真实 Milvus 检索回归 |
| `make eval-generation` | 显式真实模型回答、引用、拒答和工具选择评测 |

`PYTEST_ARGS` 可传 `-k`、`-x` 等 pytest 参数。文件集合定义在
[`integration_suites.py`](support/integration_suites.py)，新增文件默认纳入核心完整依赖，
经确认后才能加入数据库或并行分组。标签负责用例筛选，文件分组负责避免收集无关模块。

核心门禁显式排除独立 Infinity 文件和 `external_consumer` 用例，不将缺失环境的 skip
计作成功；CI 为 Infinity 单独运行必过 job，Web 消费者由对应专项命令证明。
涉及 Infinity 或 Web 契约的修改必须运行专项。
`test_provider_web_consumers.py` 在 consumer 专项中使用固定 `WEB_COMPLETION_CHECKOUT`
（HEAD 与 `.github/consumer-web.json` 一致且相关源码/config 干净），执行实际 Web 模型与
Microsoft connector domain clients、完整应用/JWT、隔离 SQL/对象及独立读回。
直接运行必须显式加 `--include-external-consumer`；缺 Node/tsx/固定脚本不能跳过。
三个阶段的嵌套 canonical runner 原始退出码分别保留；供应商 Graph 受控，不等同浏览器或真实租户。
`test_infinity_raptor_extra.py` 覆盖新表与既有 chunk 表补列、`extra` 写入/更新、
原生历史字段驱动的 RAPTOR 生成，以及替换失败后的独立读回与精确恢复。
该用例使用受控摘要模型和真实 Infinity；不代表真实模型、SQL 任务提交或生产升级已验收。
直接 `pytest tests/integration` 仍可用，但应用在收集期导入时可能访问服务；需要自动准备
临时端点时使用 Makefile 入口。混合收集时 `integration` 标签仅作用于集成目录。
质量评测不被普通 pytest 递归收集，使用以上显式入口运行。

每次运行会先在报告目录写入 `overlay-<run_id>.json`，登记临时配置目录、文件、PID、
进程启动时间及入口哈希，再创建私有配置。正常退出和 SIGTERM 会清理并读回精确路径；
SIGKILL 等无法执行收尾的情况保留登记记录，恢复时核对所属进程后只清这些路径。
登记文件不包含配置内容；清理状态与测试退出码分别保存。

REST 各族的生产入口、已有测试和执行边界见 [REST 覆盖索引](REST_COVERAGE.md)。

### Windows

没有 `make` 时直接运行目标里的命令，例如
`uv run --no-sync python scripts/run_integration.py --suite infinity --workers 2 -- -q`；
用例调用的 `make smoke` 由 [`smoke_command()`](support/smoke.py) 回退为用当前解释器运行同一脚本，
本身即不同步环境，`UV=` 等 make 变量只传给 make。Compose、模板、语料和测试源码按 UTF-8 读取，
不依赖 `PYTHONUTF8=1`。async psycopg 拒绝 Windows 默认的 Proactor 循环，因此连接 async psycopg 的
scratch HTTP 服务（进程内或子进程）与生产一样使用 selector 循环，同步用例驱动异步数据库时用
[`event_loop.run()`](support/event_loop.py) 代替 `asyncio.run()`；pytest-asyncio 用例由集成 conftest 的
`event_loop_policy` 处理。用例自建容器的端点经 `services.container_host()` 取 `127.0.0.1`。
Web 消费者用例经 [`tsx_runner()`](support/web_consumer.py) 执行 npm 在同目录生成的
`node_modules/.bin/tsx.cmd`。用例自启的 npm/Vite 服务在 Windows 上独占控制台进程组，由
[`stop_console_group()`](support/process_group.py) 以 Ctrl+Break 停止，无共享控制台或 10 秒内未退出时
`taskkill /T /F` 结束进程树；POSIX 仍对新会话 `killpg`。
`tests/unit/test_secure_leave_e2e.py` 覆盖的部署脚本依赖 POSIX `fcntl`，在 Windows 上无法收集；
类型检查不受影响：mypy 的目标平台在 [`pyproject.toml`](../pyproject.toml) 的 `[tool.mypy]` 中固定为
Linux，Windows 上 `uv run --no-sync mypy` 仍检查该脚本，结果与 CI 一致。
`tests/unit/test_integration_harness.py` 的中断恢复用例依赖 POSIX 信号与进程标识，在 Windows 上显式跳过；
覆盖文件的权限位断言只在 POSIX 上执行。
资源登记 journal（[`resource_journal.py`](../scripts/resource_journal.py)）在 Windows 上以二进制模式追加，
写入方用 `msvcrt` 锁住远在文件末尾之后的一个字节互斥（Windows 字节锁是强制锁，不挡读取与追加）；
没有 `O_NOFOLLOW`，打开后检查路径本身不是链接；目录无法作为描述符 fsync，依赖 NTFS 元数据日志。
未指定 `REST_DOCUMENT_RESOURCE_DIR` 时，用例 journal 在 Windows 上写入系统临时目录，POSIX 仍为 `/tmp`。
严格取证入口 [`run_registered.py`](../scripts/run_registered.py) 依赖 POSIX 会话、信号屏蔽和保持 PID 的
exec，在 Windows 上登记任何内容前即拒绝；其单测与经它运行的 `test_provider_web_consumers.py` 只在 POSIX 上有效。

## CI 分层与证据

本地验证范围与证据复用以 [AGENTS.md](../AGENTS.md#验证) 为准。CI 入口为
[ci.yml](../.github/workflows/ci.yml)：PR 使用 GitHub 提供的 merge commit，merge queue
响应 `merge_group`，main push 与手动 dispatch 也验证各自实际 checkout SHA。
PR 后续更新取消尚未结束的旧运行；已有同一提交的适用通过证据不要求本地重复全量。
手动 dispatch 表示明确重验，仍执行完整运行时检查。

- `changes` 仅对明确的纯文档路径省去运行时；未知基线、空差异、workflow/工具链/锁文件及测试语料变化均保守执行。lint/security 始终运行。
- lint、typecheck、unit、Go、Infinity、smoke 与 consumer 分别执行。core 按当前文件集合稳定切成四片，每片独立 runner/服务并串行运行其文件，不扩大进程内并行白名单。新增 core 文件自动分配；汇总检查核对同 SHA、四片完整且不重复、runner/JUnit 成功及零意外 skip（保留既有 xfail 语义）。
- `ci-required` 为唯一汇总入口，拒绝任何适用 job 的失败、取消或跳过；文档 job 的合法跳过由范围判定约束。**仓库保护规则应要求 `ci-required`，启用 merge queue 时也要求此检查。** 远程保护规则属于仓库设置，不会随本地工作流提交自动启用。
- consumer 使用 [consumer-web.json](../.github/consumer-web.json) 的仓库与完整 SHA，安装真实 npm 依赖并执行现有 Web helper/浏览器合同。调整消费者版本需明确更新该文件，不跟随移动的 master；必须先发布所固定的 Web commit。若固定提交尚未推送，发布依赖后此 job 才能远程 checkout；不能用 skip 或旧客户端代替。
- Go job 显式准备与 `go.mod` 同系列的 Go patch、CMake 4、编译器、PCRE2/SIMDe、真实 native 静态库，以及固定提交的 `infiniflow/resource` 字典。准备独立 MinIO 后，再跑 build/vet、三个独立 cmd 和默认 race 套件。需要真实外部服务的 Go opt-in 由 `scripts/ci_checks.py` 明确列出；JSON 审计拒绝其他 skip、失败、空报告或未完成的包，不将这些 opt-in skip 称为已验收。
- Go job 的 legacy Skill embedding SQL 回归使用 job 独占的 PostgreSQL 服务，动态发布端口并以 tmpfs 保存数据；DSN 只供该步，服务随 job 清理。该测试实际运行，不通过增加 skip 例外绕过；其他显式 live opt-in 仍按各自私有配置运行。
- 必需的 core 第 0 分片另运行 `make mcp-compat` 和实际 Agent 注册/双同名服务 probe；现代/legacy、inbound/outbound 矩阵及绑定结果保存在 `.test-results/mcp/`。该专项使用自有 loopback 进程及受控模型、授权配置和签发边界，无需真实供应商密钥。
- 本地真实模型质量评测从 core/smoke 拆到 [quality.yml](../.github/workflows/quality.yml)，每天 20:30 UTC 及手动运行；模型仍按原不可变版本校验。不移走 core 的事务、故障恢复或权限用例；修改质量资产/评测时本地运行相关检查，质量结果独立报告，不冒充组合合同通过。

本批新增 Go 实际验收入口如下；它们在普通无服务 race 中仅登记为未验证的 opt-in。
设置 `REQUIRE_SERVICES=1` 后缺失配置必须失败，不能以 skip 代替实际运行。

| 测试 | 自有输入与边界 |
|---|---|
| `handler.TestBatchGoAPI`、`service.TestRegistrationCatalogZeroASR` | 私有 `MULTIRAG_BATCH_G_CONFIG` 仅指向 loopback scratch PostgreSQL `multirag_batch_g_*`；前者需当前源码 CLI `MULTIRAG_BATCH_G_CLI` 与预登记 `MULTIRAG_GO_OPT_IN_RESOURCES` 的 journal/listener 映射，后者需精确 `MULTIRAG_BATCH_G_ZERO_ADDR`。建库和 Go 必须使用同一 TCP 端点，不能假设 Docker exec 与同端口宿主数据库相同。实际 JWT/SQL/CLI、注册与受控模型 HTTP，原始失败和独立清理读回保留；不声明真实供应商或 MySQL 全应用启动。 |
| `router.TestConnectorDetailHTTPJWTAndSQL` | `MULTIRAG_CONNECTOR_DETAIL_DSN` 仅指向 loopback scratch DB `multirag_after0fb_g_connector_*`，另需预登记的 `MULTIRAG_CONNECTOR_DETAIL_ADDRESS`、`MULTIRAG_CONNECTOR_DETAIL_REPORT_DIR` 与认证配置 `MULTIRAG_CONNECTOR_DETAIL_CONFIG`。实际注册路由/JWT/SQL 检查安全详情 DTO、成员角色、拒绝响应一致及零写入/数据源外连；每个子场景保留独立 journal。 |
| `service.TestAfter0fbNewModelsHTTPCLIAndSQL` | 私有 `MULTIRAG_AFTER0FB_B_CONFIG`、当前源码新构建的 `MULTIRAG_AFTER0FB_B_CLI` 和预登记 `MULTIRAG_AFTER0FB_B_RESOURCES`。HuaweiCloud/ModelScope/OrcaRouter/Qiniu 的实际 factory、SQL 实例、HTTP/CLI 消费者；供应商 HTTP 受控，无真实供应商验收声明。 |
| `service.TestProviderFixesHTTPCLIAndSQL` | `MULTIRAG_AFTER0FB_D_CONFIG` 仅指向 loopback scratch DB `multirag_after0fb_d_*`，配合同源码 `MULTIRAG_AFTER0FB_D_CLI`、逐案 `MULTIRAG_AFTER0FB_D_RUN` 与不可覆盖的 `MULTIRAG_PROVIDER_TEST_JOURNAL`。实际 SQL/HTTP/CLI 检查现有供应商发现、rerank、音频和 hosted/local OCR/解析，拒绝请求零外连/SQL 变化；供应商端点受控。 |
| `handler.TestAfter4776ProvidersHTTPCLIAndSQL` | 私有 `MULTIRAG_AFTER4776_G_CONFIG`、当前源码新构建 CLI `MULTIRAG_AFTER4776_G_CLI` 和预登记 `MULTIRAG_AFTER4776_G_RESOURCES`；loopback scratch DB 必须为 `multirag_after4776_g_20261025`。真实 SQL、JWT、HTTP 与 CLI，供应商/STS 使用受控端点；不访问真实 AWS、IMDS 或付费服务。 |
| `handler.TestAfterF9ProvidersHTTPCLIAndSQL` | 私有 `MULTIRAG_AFTERF9_I_CONFIG`、同源码新构建 CLI `MULTIRAG_AFTERF9_I_CLI` 与预登记 `MULTIRAG_AFTERF9_I_RESOURCES`；仅使用 loopback scratch DB `multirag_afterf9_i_20261025`。真实 JWT/SQL/CLI、双实例与受控 provider HTTP；覆盖新媒体、索引向量及无 key 的 Local PaddleOCR，拒绝路径独立读回 SQL/请求计数，无真实供应商验收声明。 |
| `handler.TestAfter775ProvidersHTTPCLIAndSQL` | 私有 `MULTIRAG_AFTER775_D_CONFIG`、同源码新构建 CLI `MULTIRAG_AFTER775_D_CLI` 和预登记资源清单 `MULTIRAG_AFTER775_D_RESOURCES`；loopback scratch DB 必须为 `multirag_after775_d_20261025`。实际 JWT、SQL、CLI 与受控供应商 HTTP；未覆盖真实付费供应商。 |
| `handler.TestAfter440ProvidersHTTPCLIAndSQL` | 私有 `MULTIRAG_AFTER440_D_CONFIG` 和同源码新构建 CLI `MULTIRAG_AFTER440_D_CLI`；loopback scratch DB 必须为 `multirag_after440_d_20261025`。真实 JWT/SQL/CLI，供应商 HTTP 受控；未配置时作为已登记 opt-in 单列，不称真实供应商验收。 |
| `handler.TestAfterD7ProvidersHTTPCLIAndSQL` | `MULTIRAG_AFTERD7_A_CONFIG` 为私有 Go 配置，loopback scratch DB 名 `multirag_afterd7_a_20261024`；`MULTIRAG_AFTERD7_A_CLI` 必须为当前源码新构建 CLI。真实 JWT/SQL/CLI，供应商 HTTP 受控。 |
| `engine.TestEngineStoresLive` | 私有 `MULTIRAG_ENGINE_LIVE_CONFIG`；显式 `MULTIRAG_ENGINE_LIVE_KIND=elasticsearch` 或 `infinity`；固定稳定 Infinity 0.7.0、精确自有 tenant/索引/库。 |
| `handler.TestDatasetGraphHTTPAndSQL` | 同一自有 engine 配置与 PostgreSQL scratch；tenant/库名以 `multirag_afterd7_b_` 开头，禁止指向业务库。可附加固定 Web API 客户端验收，不等同浏览器 UI。 |
| `handler.TestDatasetChunkMetadataCLIAndStores` | 私有 `MULTIRAG_GO_DATASET_CHUNK_CONFIG`、同源码新构建的 `MULTIRAG_GO_DATASET_CHUNK_CLI`，以及固定 Web 提交的 `WEB_DATASET_CHECKOUT`。分别设置 `MULTIRAG_ENGINE_LIVE_KIND=elasticsearch`、`infinity` 运行；使用 `multirag_after2836_e_` 自有 SQL/Infinity namespace，真实 CLI、JWT/SQL、chunk/tag/metadata 与 Web API 客户端，不等同浏览器 UI。 |
| `service.TestAfter2836ModelsHTTPCLIAndSQL` | 私有 `MULTIRAG_AFTER2836_F_CONFIG`、当前源码 CLI `MULTIRAG_AFTER2836_F_CLI` 和预登记资源账本 `MULTIRAG_AFTER2836_F_RESOURCES`。仅访问 loopback scratch DB `multirag_after2836_f_20261024`；真实 SQL/JWT/CLI，模型供应商 HTTP 由用例控制。 |
| `handler.TestDatasetReadHTTPAndSQL` | 私有 `MULTIRAG_DATASET_READ_CONFIG`，SQL 库必须为 `multirag_after9b_c`；`MULTIRAG_DATASET_READ_INFINITY_URI` 指向独占 Infinity 0.7.0 的同名库，并准备固定 NLP/native 字典。运行真实 HTTP、进程内 CLI、SQL 与 Infinity，身份及 embedding HTTP 受控。 |
| `handler.TestGoogleEmbeddingLiveHTTPAndSQL` | 私有 `MULTIRAG_GOOGLE_EMBED_CONFIG`，SQL 库前缀 `multirag_google_embed_`。真实 genai SDK、HTTP handler 与 SQL；两个模型实例由 loopback HTTP 服务控制。 |
| `handler.TestMediaHTTPCLIAndSQL` | 私有 `MULTIRAG_MEDIA_CONFIG`，SQL 库必须为 `multirag_after9b_c_media`；`MULTIRAG_MEDIA_CLI` 为同源码新构建 CLI。真实注册路由、JWT、SQL 与 CLI 子进程，模型驱动受控，覆盖流式、取消及失败语义。 |
| `handler.TestNVIDIASiliconFlowEmbeddingLiveCLIHTTPAndSQL` | 私有 `MULTIRAG_H_EMBEDDING_CONFIG`，SQL 库前缀 `multirag_inference_h_`。真实进程内 CLI、handler 与 SQL 绑定，供应商 HTTP 受控，覆盖 33 个输入、顺序还原、后批失败及权限拒绝。 |

这四个入口可通过 `MULTIRAG_GO_OPT_IN_RESOURCES` 指定预登记的资源清单（`journal`、
`http`、`directories`）：HTTP 必须使用确切的 loopback 地址，日志在创建前写入，清理后
独立核对 listener、目录与索引不存在。默认无配置的 skip 仅表示未运行；实际验收使用
`REQUIRE_SERVICES=1`，报告另存运行时测试二进制、CLI、配置、字典和后端版本。

CI 在 native cache 命中时也执行 OpenCC 输入边界的 ASan/UBSan 专项。新增 Python
connector controls、prune worker、TTS cache、Manual filter 与 REST chat 用例保持保守串行；
只有实际依赖外部 Web 的模块归入 consumer。Agent draft 和 connector dashboard 使用固定 Web SHA。

时间与语音的 Go opt-in 需要以下自有资源；设置 `REQUIRE_SERVICES=1` 后缺失配置必须失败。
测试不得指向业务库，CLI 必须从受验的同一源码组合构建。

| 测试 | 服务与执行输入 |
|---|---|
| `dao.TestBaseModelTimeScratchPostgres` | PostgreSQL；`MULTIRAG_GO_TIME_DSN`，数据库名以 `multirag_after1c_a_time` 开头。 |
| `handler.TestUserTimeHTTPCLIAndSQL` | 同一 PostgreSQL scratch；`MULTIRAG_GO_TIME_DSN`、私有 `MULTIRAG_GO_USER_CONFIG` 和 `MULTIRAG_GO_TIME_CLI`。 |
| `handler.TestSpeechProvidersHTTPCLIAndSQL` | PostgreSQL scratch（名称匹配 `^multirag_after1c_b_speech_[a-z0-9_]{6,32}$`）、私有 `MULTIRAG_SPEECH_CONFIG`、`MULTIRAG_SPEECH_CLI`；HTTP、供应商和播放器由用例自建或控制。 |
| `handler.TestAfter410GoHTTPCLIAndSQL` | PostgreSQL scratch（数据库必须为 `multirag_after410_a_20261024`）、私有 `MULTIRAG_AFTER410_CONFIG` 与同源码构建的 `MULTIRAG_AFTER410_CLI`，另需独占 Infinity 0.7.0 的 `MULTIRAG_AFTER410_INFINITY_URI`（仅用自有 `multirag_after410_a` 数据库）；真实 CLI、注册 JWT 路由与 SQL，外部模型供应商由用例控制。 |

`test_agent_file_authorization.py` 与 `test_legacy_document_counters.py` 明确登记在
`SERIAL_STORAGE_TESTS`，由 core 四分片完整分配；准备 PostgreSQL、Redis、MinIO 和 Milvus，
使用 scratch SQL、独占对象桶和队列。前者覆盖真实鉴权及附件运行，后者覆盖原子计数和实际
Graph/RAPTOR worker 分支；涉及进程级运行时接缝，不加入并行白名单，也不归入仅数据库套件。

Agent 数据库 probe 的可选 Trino 驱动另有显式专项：在独立 Python 环境安装固定版本的
Trino 及测试依赖后，运行 `python -m pytest tests/manual/test_agent_trino_probe_transport.py -q`，
报告需记录实际版本和源码输入。该文件缺少依赖直接失败，不用 skip 代替；普通 unit/core
不自动安装此可选驱动。专项使用自有 TLS coordinator，仅替换首跳地址准入以访问自有端点，
保留真实官方客户端、域名/证书、分页 GET、取消 DELETE、redirect、DNS 固定和凭据边界；
不代表真实 Trino 服务验收。

core/consumer/smoke 共用 [隔离环境准备](../.github/actions/setup-integration/action.yml)，
各 hosted job 只清理自己创建的服务；Testcontainers 的生命周期仍由原有 ServiceManager 管理。
质量 lane 额外安装 eval 组；普通 core 不再下载中文 embedding 模型。
证据 artifacts 保存 14 天，包含实际 backend SHA、consumer Web SHA、分片 manifest、
逐用例报告和故障诊断；Go artifact 另含工具链和资源提交及 JSON 测试事件。
CI 的服务镜像、工具链和测试资源缓存只复用准备产物，**不缓存“测试通过”状态**。

## 可重复的产品验收

```sh
make acceptance      # 完整流程 + 真实 Chromium + 明暗主题截图
make acceptance-api  # 显式只跑 API，报告不宣称已检查页面
```

完整入口自动启动相邻 `../web` 的已安装 Vite 项目，使用独立端口；其他目录可用
`uv run --no-sync python scripts/run_product_acceptance.py --web-checkout /path/to/web`。
`--web-base-url http://127.0.0.1:5173` 复用已运行的前端，`--headed` 显示浏览器操作。
需要 dev 依赖和 Playwright Chromium（首次准备：`uv run --no-sync playwright install chromium`），
基础服务由既有集成框架探测/准备；缺失依赖或选中项失败使命令非零退出，不以 skip 计成功。

验收创建独立 PostgreSQL scratch 库、MinIO 桶、Milvus 集合、Redis 队列和测试身份。
直接运行当前 API 代码，不使用常驻服务的登录态；前端请求仅转发到本次 scratch API，
保留真实请求体、鉴权和业务响应，不伪造接口成功。结束时读回 SQL 行、对象、索引及队列清理结果。
临时 worker 配置只写权限受限的临时目录，用后删除；不修改共享配置。

当前锁定的行为：上传后独立读取名称/大小，生产 worker 解析后完成状态与非零分块，
分块保留合成文件内容，检索返回该文档与预期文本；配置保存后独立 GET，比对省略字段保留、
显式空数组清空及父子分块关闭。真实页面执行上传、开始解析、配置保存/重载和 Enter 提交检索。
检索还打开详情核对完整原文。知识库列表、文档列表、分块、检索、数据集设置、个人设置与
API 文档在明暗主题、1440×1000 和 1024×768 视口检查路由、脚本/接口错误、页面横向溢出
及键盘焦点，并截图。切页取消的过时请求另列记录，当前操作仍须返回真实成功响应并通过内容断言。

证据保存在每次独立 `.test-results/<运行标识>/product/`：`acceptance.json`、
`acceptance.md`、完整 PNG、带文件名/路由/视口/主题的 `contact-sheet-*.jpg`。
脚本逐项写入结果，前置失败会标记依赖项 blocked；截图保留失败状态，清理结束前整体状态仍为 running。
自动检查通过后，`visual_review` 仍为 `pending_human_review`，最终视觉判断由人完成；
本入口不使用像素基线自动批准设计。报告目录可用 `--report-dir` 指定，必须为空，避免混入旧证据。

首版用合成 TXT 与受控 768 维 embedding；解析、后台进程及存储/检索实现均为生产代码。
它验证流程契约，不证明真实模型的召回质量，也不覆盖 PDF/OCR、生产部署或任意文件格式。
真实模型质量仍用下文的 `make eval` / `make eval-generation`。产品验收为显式独立套件，
不替代 `make verify`、适用 `make integration` 或消费者专项门禁。

## 服务与隔离

入口先做协议就绪检查，再导入应用。PG、Redis、MinIO 探测包含认证；Milvus 查询版本；
Infinity 在子进程内完成真实 Thrift 握手和数据库列表读取：SDK 导入（pandas/polars，
Windows 冷导入可达十几秒）与握手分别限时，握手仍以 5 秒为限。
metadata pushdown 与 `test_es_conn_keyword_ids.py` 的 ES 合同在收集前准备 Elasticsearch 8：显式
`METADATA_TEST_ES_URL` 指向可丢弃的专用实例，否则由 Testcontainers 创建和回收。
这些测试不会隐式使用应用的 ES 地址，显式地址不可用或版本不符时直接失败。
ID 合同仅需要 ES，用独立索引验证 keyword 映射、缺失字段排序、精确过滤及历史 text ID 兼容，
并覆盖 memory 维护查询的缺失日期字段和已有日期字段排序。索引清理后独立读回删除结果；
更新模板不会迁移已有索引。
已有实例只读探测、不会由测试框架停止；缺失的 PG、Redis、MinIO、Infinity 可用
Testcontainers 启动，镜像从 Compose 读取；本机 Docker 的端点记为 `127.0.0.1`，避免
`localhost` 先解析到未被转发的 `::1`。Milvus 必须预先提供，当前不自动创建整套
Milvus 依赖。`INTEGRATION_NO_TESTCONTAINERS=1` 禁用自动启动。

临时端点通过进程内配置和 0600 外部覆盖文件同步给应用及子进程，不修改共享配置。
测试使用随机 scratch 库、桶、集合和 key。注册清理后再创建资源；初始化失败也执行清理。
普通业务测试应以独立连接读回提交结果，不通过事务回滚替代对真实 commit 的验证。
迁移测试需使用隔离 schema/数据库，不能并发修改同一个 schema。

共享 fixture 与辅助函数放在 [`support/`](support/)，普通 `test_*.py` 之间禁止互相导入。
上传、Agent、数据集、图片、解析与取消场景复用这些入口。关键 fixture 使用 ExitStack
独立回收 SQL、对象、向量、Redis、监听端口和线程；某项清理失败不会阻断其余回收，
失败仍会使验收变红。初始化故障测试覆盖 SQL 提交后失败、桶创建后丢失响应、
数据库创建后丢失响应，并通过独立连接检查没有遗留资源。

xdist 的每个进程使用独立 scratch 库和随机外部资源，进程内全局补丁不跨进程共享。
只有 `PARALLEL_TESTS` 显式登记的文件可并行；新文件、Infinity 和质量评测不自动获得许可。
默认仅对全部已审核的多文件选择启用两个进程和 worksteal 调度；单文件或包含未审核文件时
自动串行，显式要求未审核文件并行则失败。`PYTEST_ADDOPTS` / `PYTEST_ARGS` 中的 `-n`
优先于默认值，已有 `--dist` 选择保持不变。
`--integration-seed=20261004` 可复现乱序，同一文件内用例顺序也会改变，模块 fixture 保持连续。
两组互不依赖的解析集合可并行创建；`--fixture-jobs=1` 恢复原先串行创建，供性能对照。

普通 pytest 在用例执行阶段使用 pytest-socket，允许 loopback 与 Unix socket；集成入口还允许配置中的
服务主机，生成评测额外允许专用模型地址。未授权的 Python socket 连接在发出前失败。
收集期应用导入不受该插件完整保护，OCR 资产需要预先准备，避免触发遗留模型下载逻辑。
这是 Python 层的意外联网防护，不是操作系统网络沙箱；原生数据库驱动、浏览器及其他
子进程不由该补丁完整约束。真实 worker 测试的 Python 子进程显式安装相同保护。

## 失败与耗时证据

每次 Makefile 集成或质量运行创建独立 `.test-results/<运行标识>/`：

- `run.json`：套件、选中/未选择的文件（仓库相对 POSIX 路径）、依赖服务、复用/临时来源、准备与总耗时、退出码、Python/平台/关键包版本。
- `execution-main.json` / `execution-gw*.json`：收集耗时、选中/排除用例、乱序种子、fixture 并发数和 setup/call/teardown 结果。
- `events-main.jsonl` / `events-gw*.jsonl`：逐用例开始和阶段完成记录；进程中途退出也能定位停顿。
- `junit.xml`：pytest 标准结果；控制台同时打印最慢 25 项。

`REQUIRE_SERVICES=1` 下，选中集成/质量用例及模块收集时的意外 skip 转为失败，正常 xfail
保持 pytest 语义。CI 保存报告和失败时服务日志 14 天。证据目录忽略入库，不写配置或凭据。
现有业务用例的专门资源快照继续保留。

性能对照必须使用相同用例、种子、服务和缓存状态，且不能同时运行两轮全量测试：

```sh
uv run --no-sync python scripts/run_integration.py --suite core --workers 0 --report-dir .test-results/serial -- -q --integration-seed=20261004 --fixture-jobs=1
uv run --no-sync python scripts/run_integration.py --suite core --report-dir .test-results/parallel -- -q -n 2 --integration-seed=20261004 --fixture-jobs=2
uv run --no-sync python scripts/compare_integration_runs.py .test-results/serial .test-results/parallel
```

比较器拒绝失败、skip、缺少阶段、重复执行、选中但未执行的用例，以及用例集合/环境指纹/
已知服务版本不同的结果。当前直接探测 PG 和 Milvus 版本，其余服务版本未知时记为 null；
因此仍需操作者确认服务实例、缓存和后台负载一致。阶段耗时之和在并行运行中会重叠，
单次对照只能说明该机器上的一次观察，不能承诺每次固定加速比例。

## 恢复覆盖边界

`integration-system` 使用独立子进程，数据库案例验证已提交事务保留、未提交事务回滚。
worker 案例运行生产 `handle_task()`、真实 Redis pending 消息、解析器和 SQL/MinIO/Milvus，
仅控制 embedding 输出与终止检查点。它验证：

- 结果已落库、XACK 前进程被终止：重启后清空 pending，SQL 计数、操作日志、原始对象及向量内容不重复改变。
- 解析结束后进程被终止、随后通过真实 API 取消：重启消费后仍保持取消状态，不能继续写入索引。

这些证据覆盖指定的两个崩溃窗口，不代表任意跨存储中间状态都获得 exactly-once 保证，也不等同于部署恢复验收。

## 本地质量与显式生成评测

先安装可选依赖组，再显式准备资产；质量评测在导入解析器前校验缓存，缺失即失败：

```sh
uv sync --group dev --group eval --frozen
make eval-assets
make eval
```

OCR 与中文 BGE-small 向量模型都按不可变 revision 和 SHA256 校验；下载失败不替换缓存。
OCR 使用 `core/res/deepdoc`，向量模型使用已忽略的 `.cache/evals/`。缓存命中时不访问网络。
CI 在独立准备步骤下载、校验并缓存，然后以单独的 `make eval` 步骤运行质量门禁。

[`evals/corpus/v1/`](evals/corpus/v1/) 是版本化的合成中文回归语料，含采购审批、数据保留、
套餐表格、支持时限和固定收据图片。10 个正相关检索样本走真实解析、本地 embedding、
Milvus 和生产 Dealer；另外检查表格事实与真实 OCR 的编号、数量和日期。
报告保存语料版本/哈希、模型 revision、依赖版本、Recall@3、MRR、延迟及本地模型调用成本 0。
本地 CPU/基础设施成本不包含在该数值中。这套小语料尚未经过生产领域人工校准，不代表业务质量上限。

生成评测按需配置本机环境变量，再运行 `make eval-generation`：

- `MULTIRAG_EVAL_BASE_URL`：专用 OpenAI 兼容服务地址。
- `MULTIRAG_EVAL_API_KEY`：专用评测密钥，仅放环境变量或本机秘密管理器。
- `MULTIRAG_EVAL_MODEL`：评测候选模型名。

缺少配置时命令明确失败，`quality-generation.json` 记录阻塞原因和执行 0 项，不 skip。
配置完整时，13 个样本通过生产聊天/工具适配器检验回答事实、引用覆盖与来源、无答案拒答、
工具名称及参数；工具服务使用版本化的只读合成工单。报告记录模型、提示词版本、token、
延迟及完整断言。费用在没有账单证据时保持 null，不凭 token 总数猜测。
事实检查以预期短语/独立数字匹配为主，并非语义裁判；下一步引入真实业务集时，应先人工审定
预期、校准阈值再升级评分，不能用普通集成绿色替代真实生成质量证据。
