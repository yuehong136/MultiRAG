# after-32559d2d 本地批次交付

本批次完成 20 项评估、适用实现和定向组合验收，覆盖 96 条上游路径记录（92 个不同路径）。
Python 是生产后端；Go 是并行实现，Web 单独交付。全部提交仅在本地，未 push、部署或执行远程 CI。
第 7 项独立本地解析、分块和 EXPLAIN 仍未实现，不能把现有 provider-task API 或占位命令称为等价完成。

## 固定范围与版本

- 后端起点：`c17fd797f60c214968754d203ee3ecd558f44c34`，保留既有恢复和类型修复祖先。
- 上游范围：排除 `32559d2dfca8909749f65475a0d7cd3bf8e0c332`，按 first-parent 取之后 20 项，
  从 `238a01d9e3d56e8ccace49e8759c9012b88845b9` 到 `2971849783b879749ae25732f202e42acf0a3943`。
  上界固定为 `519e7d98a5651564d4e35d6648f006cba4baaf4f`，本轮没有 fetch 或扩展下一批。
- 验收代码提交：`931abf08858e3fbdaa3e1fa5a09a11b383c2f8cd`；最终包含本文的 main SHA 见交付回执。
- Web master 与 [消费者 pin](../../.github/consumer-web.json)：`3e473d614e70e9ccdd57c4ea4c715833effbd6e2`。
  保留并合入其他任务先行提交的 `13394df3642adfe62fc86bf5752b69fca4c4100d`；原先 4 项 Web WIP 的字节没有改变。
- 后端原先 28 项 WIP 按状态、路径、SHA256 保留；本批次不修改或暂存 `PROGRESS.md`。

下列候选均经 I 审阅后串行接收；最终组合验收使用新的完整 Python 3.12 环境，
不是旧 `.venv` 的验收结果。LiteLLM 锁定为 `1.82.5`。

| 线 | 本地接收候选 |
| --- | --- |
| P：模型、依赖与凭证 | `45bc5f5715c4d24acf1d4bf2cdfe068e7bced822` |
| A：Agent、会话、默认模型与空 KB | `a3d453ad2394d77708ac0bef81274e3bfb631643` |
| D：Discord / WebDAV | `4263243d3a88c25cebd41a5d9f652dd87c5c0e64` |
| S：存储、停止解析与约束 | `49cd3f87220803748ae5c2aa97da6b12d647ecc4` |
| G：Go 推理与模型发现 | `6626db331e7810e1ead275a1c78782259a905b2a` |
| C：Go CLI | `c18d61ea764ca0f8fdd236506d998d781738a61d` |
| W：标题规则与预览 | `489454fa0f26ea6ac32c50d47b5e1dc26af300c0` |

## 逐项处置

“保留”表示检查了当前实现、调用链和回归证据，不表示跳过该项。
完整逐路径映射、质量比较和证据索引保存在本地 `item-ledger.json`，不在仓库复制上游分析笔记。

| # / 上游提交 | 本地行为与处置 |
| --- | --- |
| 1 / `238a01d9` | 保留稳定、线性复杂度的能力标签合并和注册输入约束；补重复目录声明、REST 与实际 Web API 消费回归。 |
| 2 / `a079c085` | 排除 LiteLLM `1.82.6`，最小更新锁至 `1.82.5`；保留 `1.82.7/8` 与 fork 排除。新环境实际导入和受控 HTTP/SSE 验证。 |
| 3 / `c50f9c59` | Categorize 接受严格非负整数窗口，仍拒绝 bool/float/string；零窗口不取历史。保留 Canvas 既有完整 reset 和继续会话合同，实际 HTTP/SSE/SQL 验证新旧会话。 |
| 4 / `19104168` | 保留 Discord list/tuple/set/CSV 严格规范化；错误项拒绝，不静默丢弃导致 scope 扩大。执行实际 Web serializer，验证 SDK、线程和事件循环生命周期。 |
| 5 / `4b10c0b8` | Go 保留 nil-safe 推理默认值、调用方配置隔离和 sync/stream 共享构造；增加实际受控 HTTP 与 race 回归。 |
| 6 / `84edf539` | Go 保留 `[]string` driver 和服务层元数据注入，逐条评估 51 路径、48 provider；修 SiliconFlow 精确 ID 失配后的有限后缀查表，复用严格有界发现解析、错误和取消清理。原始发布 ID、顺序不变。 |
| 7 / `ca00d23a` | 不引入打印日志后返回成功的 parse/chunk 占位实现。独立本地解析、分块和 EXPLAIN 不可用，真实 `PARSE FILE` 仅为 Go provider-task 提交。修复 CHUNK 词法 EOF 越界，验证失败退出和 JSON/HTTP 边界。 |
| 8 / `906618fb` | 保留共享 think 流解析器，验证真实 MiniMax SDK→模型 bundle→Agent 的思考/正文、取消与关闭；没有新建私有重复解析器。 |
| 9 / `5d3f8bbf` | Web 先规范化原始 form/DSL 再合默认值，按 mode 重挂 field array，保留两套 UI 规则和 active-only 序列化；group 默认 hierarchy 为 0。验证明暗主题、键盘切换、保存重载及实际 Python HTTP/SQL。 |
| 10 / `a1dc2da7` | Python 优先使用已授权数值绑定或明确名称；只有未绑定才回退租户默认。Web 没有对应 chatbot `model_name` 活跃入口，保留现有 Agentbot 外部参数合同。真实回复同时独立读回会话提交。 |
| 11 / `3f929e39` | ES 生成 SQL 的 BadRequest 降为 warning，仍传播错误并保留业务重试/fallback；连接故障保持 error traceback，不伪造空成功。 |
| 12 / `9871a7e0` | Replicate 六模型目录、canonical `llm_name`、共享 raw/envelope 凭证解析；使用原生异步 SDK 增量流、独占 transport、取消清理和整段绝对期限，过期后已缓冲事件也不能输出。批量 embedding 校验维度/数量，未支持的 tools/rerank 明确不可用。 |
| 13 / `47fb462e` | 保留文档服务删除委托，缺失 File2Document 时不猜测文件关系；真实 SQL 验证提交、关联残留和外租户拒绝，控制对象/索引/取消边界。 |
| 14 / `a851228d` | 预览仅向配置的 HTTP(S) API origin 携带授权，拒绝 URL 凭据、协议相对/外域地址和重定向；保留 abort、晚到响应及 blob 回收。Python 普通预览与 beta 授权下载继续分离，实际 JWT/API/拒绝路径和字节读回通过。 |
| 15 / `b978e262` | 本地 SQLAlchemy 没有目标 Peewee 自动索引，不引入猜测性 DDL；保留 provider/name 两条唯一约束，真实 fresh/upgrade 验证空 key 与跨 provider 隔离。 |
| 16 / `c15b2b3f` | 保留三种 size 字段、严格非负数字和 fail-closed 全量快照；0、阈值等号接受，大于阈值在 GET 前排除。增加共享脱敏警告；真实 WebDAV HTTP/SQL 验证失败不 prune、不推进 cursor。 |
| 17 / `de18313f` | canonical stop 明确走 `clear=True`，复用现有锁、补偿和代际 fencing，删除 partial 并重置计数；重复 stop、旧 worker 写入和失败回滚有独立读回。一般 ingest 取消仍保留 partial。 |
| 18 / `381091df` | 已授权空/无效 KB 在模型绑定和检索前产生最终错误，覆盖流式与同步消费和相邻 dialog 分支。 |
| 19 / `d4fbc013` | 活跃 Azure embedding/vision factory 共用 raw-string/JSON-object 凭证解析，保留 endpoint/version；不迁移未被使用的拆分包装器。 |
| 20 / `29718497` | ES/OS、Infinity、Milvus 使用原生存在性语义；只把明确不存在视为可跳过，空索引和探测失败不混淆。严格删除避免遗留 wrapper 吞错返回 0，验证确认、独立读回与恢复，旧路由继续 404。 |

真实 chatbot HTTP 回归还发现附着 ORM JSON 对象丢失 assistant 的问题：
授权后使用临时深拷贝输入，完成后一次提交完整回合；失败不提前提交 user 历史。
该修复在 A 的接收范围内，并由 31 项实际 HTTP/SSE/SQL 模型默认与会话测试覆盖。

## 本地验收与证据

证据根目录：
`/Users/xldu/.local/state/multirag/validation/20261101-batch-after-32559d2d`。
`scope.json` 固定范围和补丁哈希；`item-ledger.json` 记录全部路径；`intake.json` 记录串行接收；
`validation.json`、`cleanup.json`、`delivery-complete.json` 记录实际输入、结果、清理和最终 SHA。
运行日志、JUnit、case 资源 journal、恢复包和独立读回均保留在该目录。

| 范围 | 实际结果 |
| --- | --- |
| Python 生产后端 | 新完整锁环境中 38 个相关 unit 文件：796 通过；10 个相关 integration 文件：71 通过；均零失败、零 skip。集成收集前准备真实 PostgreSQL、Redis、MinIO、Milvus、ES 和本批专用 Infinity，使用 `REQUIRE_SERVICES=1`。 |
| Python 静态检查 | 全部 44 个改动 Python 文件 Ruff check/format；当前分层依赖、async/sync DB 门禁通过。不声称完整 mypy 通过。 |
| Go 并行实现 | `go build ./internal/...`、`go vet ./internal/...`、server/admin/CLI 三入口原生链接通过。定向 race 12 个顶层/89 个含子测试，实际 SQL/HTTP/CLI race 2 个顶层/2 个含子测试，零失败、零 skip。另复用逐线 48-provider 矩阵和受控 CLI 的审阅证据。 |
| Web | 固定最终 `3e473d61` 组合：`npm run test:ci` 2344 项通过，`npm run build` 通过；真实 Chrome 6 组产品流程通过。5 张 1600×1000 截图先拼带文件名、路由、视口的 contact sheet 后审阅。 |
| 跨仓消费者 | 当前固定 Web APIClient/serializer 实际请求 Python，覆盖标题规则两模式保存/重载并独立 SQL 读回；文档预览/下载、授权与拒绝路径使用实际 HTTP 和对象字节读回。Go 使用最终 P 目录及源码构建 CLI。 |
| 清理与可恢复性 | 各候选接收后实际恢复 bundle 并核对变更 blobs，再清理精确自有路径；I 最后退出。独立读回检查 SQL/对象/向量/Redis 命名空间、容器、监听端口、进程组、缓存、私有配置、worktree/ref/admin；借用依赖和原 WIP 保留。终态见 `cleanup.json`。 |

最终组合的源码字节、锁、配置、消费者 SHA 和原始退出码在 runner journal 中绑定。
逐线通过证据只用于它实际覆盖的边界；旧锁环境的证据不代替新锁组合。
最终文档提交不改变已验收的执行代码，最终 main SHA 与验收代码字节的核对见交付回执。

## 既有失败与验收限制

- 扩大到完整 `test_integration_harness.py` 时为 46 通过、1 失败：既有 consumer 清单包含未标记
  `external_consumer` 的 `test_search_detail_guard.py`。相关文件与 c17 基线一致，未放宽断言；本批数据库分类专项通过。
- S 两个 common 文件的定向 mypy 有 5 条错误，在冻结 c17 文件副本重现相同 5 条；没有把它称为通过，也未扩大为无关类型迁移。
- `strictProtocolPassed=false`：I 初次登记失败后提前创建树/ref/admin；P/A/D/S 初次导入涉及精确资产、
  token cache 或日志路径预登记不足；G 的旧测试存在 31 个未先登记的监听器；W 强制 lint-staged hook 自动暂存 stash。
  各线历史原始记录保留，后续补登记、复跑和清理不追认历史协议通过。
- 供应商 SDK 使用真实库和受控传输。未验收付费端点、真实 Discord/WebDAV 账号、供应商端取消/账单、
  真实网络丢确认或 Replicate 模型输出上限；本轮依赖修复也不表示完成范围外的后续 CVE 升级。
- OpenSearch 只有实际 adapter 的受控测试，未运行真实 OS 服务；未验收持久 worker、生产业务库、完整历史升级或云端操作。
- Markdown 相对图片仍按 UI 相对路径请求且不带 JWT；没有引入受保护相邻资产映射。
- 未运行全库 Python unit/core/typecheck、完整 Go race 或远程 `ci-required`。PR/merge queue 的最终组合仍须通过适用 CI。
