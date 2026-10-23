# 1c0eaa50 后续 20 项本地交付

2026-10-24 本批 20/20 已完成逐项评估、适用实现与定向验收，串行合入实际后端 `main` 与 Web `master`。
本地交付，未 push、未部署；远程 `ci-required` 待运行，本记录不代表完整 core、全部集成或生产验收。

## 冻结范围与组合

- 后端起点：`419751322aeca164a1900438a047255028e2df63`；Web 起点：`7c9576840f2e4b7c7dbd0cd99c8c33f63dc3aff2`。
- 上游 first-parent：排除 `1c0eaa504bc477ba704f5845b85648624e925046`，连续 20 项至 `41072ed44d1b8fb10fac040970b9df8da394459a`；后续修复查阅上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。20 份完整 diff 的顺序和 SHA256 已独立复核。
- 最终后端源码/测试组合：`a8e317351fbf02f9c03c435fbc851d586e5facb1`；Web 实际 `master`：`f7767371e87ce59208a28b5e8ee2761f7e741fa1`，由 [consumer pin](../../.github/consumer-web.json) 固定完整 SHA。
- 完整 diff、审计、候选映射、原始命令/退出码/JUnit、资源与独立读回在本机 `/Users/xldu/.local/state/multirag/validation/20261023-batch-after-1c0eaa50`。文档提交及清理后的主干 SHA 见 `delivery-complete.json`，避免提交自引用。
- 原有 28 个未提交文件不属于受验组合；隔离工作树验证后串行合入，保留其原有字节（PROGRESS 仅叠加本批最小索引）。
- 主 Web 在本批 fast-forward 后出现其他并行 WIP；其前后状态和 hash 留在 `integration/later-web-wip/`，不清理或回滚，不用 dirty 主 Web 运行证明已提交 pin。

## 逐项结论

| 项 | 原始 RAGFlow SHA | 线 | 最终处置 | 实际提交（短 SHA） |
|---|---|---|---|---|
| 1 | `b89878c593c059a2106494291cfaf5747ad1cddf` | D | 已有 canonical 鉴权预览/下载和 source provenance；保留 owner/admin、普通 SDK 与 beta 明确兼容，不复制上游弱化检查。 | 已有等价 |
| 2 | `f038a341544304a586449a6bdaa33cb17dfd27f3` | H | 逐文件核对上游版本足迹，保留 MultiRAG 自有产品与包版本。 | 产品身份保留 |
| 3 | `851b16b91399880f632124c558aae96c803d889f` | H | 发布说明草稿按本地已有 REST、标签、Widget、模型能力去重。 | 已核对，无重复发布声明 |
| 4 | `8dc5b1b42d1492c0ab2c5a3b8fd076b38ec99adc` | H | 完整 diff 仅一处注释，AST 无执行差异；本地 rerank 严格校验保留。 | 无执行改动 |
| 5 | `cc21dc7f007ea753e2c2063922abac6122f7c8fc` | C | Loop/VariableAssigner 缺失首项在读写前抛 ValueError；保留 0、False、空串与无需参数的移除操作。 | `72eceddf` |
| 6 | `d46bbd30f7f6f03de6ec3c8ba301b246576abfd6` | F | 显式持有 Langfuse generation，记录有效 prompt/answer 用量；计数失败与合法零值分开，失败/取消可靠结束。 | `9b497a1c` |
| 7 | `ef46005ef1358bf5b50ee9a72d3014c98ffd6535` | B | MiniMax TTS/SSE 与严格 hex、业务状态、最终帧；CLI params/format/save/play 按显式命令执行。 | `6496bb09`, `4a14f188` |
| 8 | `4bfdb1e12373e1c901a54d91968c496941bdf7b0` | C | 嵌套赋值遍历父级路径，保留兄弟值；实际 Canvas 调度与序列化回归。 | `72eceddf` |
| 9 | `48b4aa3e93b2a76b89776b0848ab5d60b7271494` | G | HTML→PDF 覆盖正常等待、超时和失败；成功创建的 WebDriver 在 finally 中恰好释放。 | `8b86cd86` |
| 10 | `82e06db8c3c099ea11e5bbe28d95967c1bbbba7a` | H | 补 Code 业务输出、系统字段、错误与图表产物的当前合同文档。 | `a69957ae` |
| 11 | `f0122179dd8d13c12f6c8d8a29501f9d2f7a9772` | A | Go BaseModel hooks 统一毫秒，保留显式时间及历史读取；独立 Skill/CoreState 时间模型保持。 | `4916b628` |
| 12 | `714f777fa0418b5fd800f254b0c72589082f6c88` | D | Agent 上传/下载绑定可信身份、canvas 和会话；Web beta multipart/XHR 同步迁移，运行附件按可信空间恢复。 | `88520151`, `6667b32b`；Web `f7767371` |
| 13 | `0450400efd7325f6a26cce058121a6120209a32f` | A | 两条成功登录路径一次保存 token 和 LastLoginTime；拒绝/SQL 失败不旋转凭据。 | `d13b542b` |
| 14 | `ba8cb9dd4ace50e6aed22bf5732e397eaca2ce90` | E | Graph/RAPTOR 文档列表默认 None，调用方列表复制为快照；并发调用互不共享。 | `c5c05d23` |
| 15 | `3c68ad03be642bc23992ad92637426741e98f319` | A | 用户设置四个可选字段落库，省略/null 保留，显式空值可清除。 | `25ff9437` |
| 16 | `bd99a22661b12ca8d8375f2b28512afb509a1ad3` | E | 文档/知识库计数 SQL 算术更新、双资源作用域和 savepoint；失败不留下半次更新，保留现代 ledger/fencing。 | `2b12f6b3` |
| 17 | `63df01fe3f18a5b54634c8ab68761b0487aee20f` | I | 现有 canonical MCP binding 已覆盖同名工具；补实际 Agent 注册、双同名服务与拒绝零签发/零 HTTP 验收。 | `8f7d7e76` |
| 18 | `a98994ff918a227771944239409a6846d95ba095` | D | 数据库 probe 分支可靠释放 engine/result/cursor/connection，固定安全目标并约束 Trino 后续 HTTP；主错误优先。 | `88520151`, `6667b32b` |
| 19 | `106f4b777efb8911adc3966497f79a9dfd5dc6b6` | B | FishAudio TTS/ASR、OpenRouter TTS 与目录、SQL→驱动参数合同；未支持流式明确 501。 | `6496bb09`, `4a14f188` |
| 20 | `41072ed44d1b8fb10fac040970b9df8da394459a` | J | 保留递归 React label/group alias、cmdk 模糊排序和原有受控输入；无需复制 includes 过滤。 | 已有等价 |

## 候选到实际主干映射

以下实际提交均以 stable patch-id 核对，且已进入实际主干。J 无源代码改动，不制造空提交。

| 线 / 仓库 | 候选完整 SHA | 实际主干完整 SHA |
|---|---|---|
| H / backend | `a69957ae0bb0772d22ffdc5ebefa36e32cb9bdce` | `a69957ae0bb0772d22ffdc5ebefa36e32cb9bdce` |
| C / backend | `bec64a10adba9982d930f21681c3e6ca233acad4` | `72eceddf1ce2cfb9e4ce99d996cdcdf2e25de34e` |
| I / backend | `f8f55897db838160161705e66b11dd6aae20f25d` | `8f7d7e7699b41c199f8877e42e4a9c053e4140d9` |
| F / backend | `6b20613c20e68c2247f26129c2c649cfe159a15e` | `9b497a1c812a5f708de00e34e839bf39421f109a` |
| G / backend | `45a04339c26e32130c625f41eea11d75950a09d7` | `8b86cd8696a513a5de498b2ba0e4f5d4f9402577` |
| A / backend | `efdfd6d6cd31dc53fa28ad36743293aae8105e44` | `4916b628d38a9c54dbee683fb6a07050dfd35a2d` |
| A / backend | `f55279d1462b269d4c9adacab849481260f71f1e` | `d13b542b9d07f509dde4f326b7a729d51c50d1c2` |
| A / backend | `30cafd0511ffe592ec75b7e4100293ec2ef1c573` | `25ff9437d16c3c18c4ab3adbdce0bbcd02487766` |
| E / backend | `2427da14a99778db95c645d340078a5b435924b4` | `c5c05d23b728eb86b700f7ef61be26faa48ab7ed` |
| E / backend | `d7b3b911147f0c4d2e461e8d2b2334abdfb63751` | `2b12f6b3b3bcd6825a66d3ae421aaed26bc9db58` |
| B / backend | `f26fa9fca1286dc59e524b10995863c472d80fc4` | `6496bb09d7c9ee78d066955f789a668125de0726` |
| B / backend | `2d80306458cdd3dfed16ef223da67e1771521260` | `4a14f188ba8dddefe0a9766dc75dcdfd4690ee9c` |
| D / backend | `3c56b935b05f39810de6c512c1a35f5e327f04d3` | `885201514c4df5f7559719650b465661bb86b947` |
| D / backend | `2f70006ca06beb9c6b7df93dee225f947958d999` | `6667b32b6022853ebb4e34765a21fe400630fc17` |
| D / Web | `27da5ce0ea0cebc9ab2e4164bb744ae04de2ecb6` | `f7767371e87ce59208a28b5e8ee2761f7e741fa1` |

集成人单独登记 MCP 必需专项、Go live opt-in、串行存储测试与消费者 pin。
[稳定路径映射](../enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md) 只记录当前落点和长期合同。

## 实际检查与证据复用

| 层次 | 本批结果与边界 |
|---|---|
| Python 定向组合 | `integration/logs/python-combo-unit.xml`：933 passed，零失败/skip；Ruff/format、mypy 纳管范围与新增模块、8 个 import 契约、async DB 均通过。 |
| CI/分组 | 18 项登记合同通过；两份新增集成文件显式串行、完整四服务、由 core 四分片分配；MCP 矩阵与实际 Agent probe 纳入必需 core 第 0 分片。 |
| 真实 SQL/HTTP/对象与消费者 | `integration/python-integration-auth-report/junit.xml`：77 passed，零失败/skip。新原子计数、鉴权/运行附件、会话引用、上传及聊天附件、SDK/beta 下载和最终 Web 预览/下载，另含 16 项实际 worker 回归；实际 PostgreSQL、Redis、MinIO、Milvus、Elasticsearch 和 OpenSearch 就绪，`REQUIRE_SERVICES=1`，禁用 Testcontainers 回退。外部模型与数据库 probe 目标校验的测试接缝单列受控。 |
| Go 组合 | `4a14f188ba8dddefe0a9766dc75dcdfd4690ee9c` 下 build/vet、三个独立 cmd、race 与实际组合 CLI/HTTP/JWT/SQL 通过；1565 个去重叶子用例，零失败/skip。冻结 Go/模型/锁/native 输入与最终组合核对；Darwin arm64，供应商与播放器受控，未验证真实音频设备、MySQL 或 Linux native。 |
| 可选驱动专项 | D 的官方 Trino 0.340.0 / Requests 2.33.1 在独立环境中完成 17 项自有 TLS 传输测试；同源分页/取消、混合大小写 DNS、redirect、跨源、spooling 与凭据边界。首跳目标准入仅为自有 loopback fixture 替换；生产锁文件和默认 CI 不安装 Trino，不声明真实公网服务或其他客户端版本。 |
| MCP | 22/22 modern/legacy、inbound/outbound HTTP/SSE 矩阵；实际 Agent 注册和两个同名工具服务 probe 通过，拒绝零凭据签发/零服务请求。授权配置、签发与模型为受控边界。 |
| Web 组合 | `f7767371e87ce59208a28b5e8ee2761f7e741fa1`：245 API、34 产品和 49 选择器相关测试通过，零失败/skip。D 的完整 TypeScript/build、bundle/file-size、lint/格式证据按相同源码、锁和配置复用；lint 0 errors，保留既有 warnings。 |
| 浏览器与其他实测复用 | D 修复后在最终 Web 对应的相同源码树上完成 Debug、Share、Widget 三个真实页面的文件上传和正常键盘提交/运行；独立读回对象字节、可信所属空间和 Share/Widget 附件会话。最终浅色 contact sheet 已审阅；早期 Debug 被开发检查面板遮挡的失败与旧深色截图另保留。J 42 项浏览器检查与 G 10 项真实 Chrome/PDF 场景按未变输入复用；只审阅带文件名/路由/视口的 contact sheet。G 的 SQL/auth/storage 消费者边界受控。F 的 5 次真实 HTTP/SSE trace 故障注入按未变输入复用；E 的 16 项 worker/HTTP/SQL/多存储回归因 PostgreSQL 验收环境变化在最终组合重跑，原始候选报告另保留。输入比较见 `integration/evidence-reuse-proof.json`，不把重叠重跑相加。 |

Python 单测/静态与 MCP 专项实际运行于 `6667b32b6022853ebb4e34765a21fe400630fc17`；之后唯一可执行提交差异是集成测试 fixture
显式绑定可信附件 owner，该文件的 lint/format 与最终 77 项运行验收已通过。其他差异只在非执行文档，
生产、单测、MCP、锁文件和配置输入均一致，逐文件 hash 核对见 `integration/final-input-qualification.json`。

独立源码审阅在 D 初版找到附件请求类型绕过和 Trino 后续 URI 问题；已补修后才接受，
原候选、发现、失败日志及后续回归全部保留。其他早期失败包括本机 Chrome/driver 版本、
测试 fixture ID/路径/协议断言、缺少 CREATEDB、已清理模型链接，以及根组合的本机 PostgreSQL trust 认证和绕过构造器的旧 Canvas fixture；分别修正环境或测试根因，
最终改用独占且要求密码认证、与 CI 同为 16 系列的本机 PostgreSQL 16.14，fixture 明确提供可信 owner；不在生产代码添加身份 fallback，不靠吞异常、跳过必需服务或放宽断言换绿。

## 清理与尚未验证

逐线先保留并验证恢复 bundle，再移除本批候选分支/worktree；各线登记的数据库、角色、
桶、collection、Redis keys、容器、端口、进程、私有配置和生成文件已独立核对。
集成工作树在文档提交后移除，最终精确清单、bundle hashes、主干/消费者 pin 及 WIP 读回见
本机 `delivery-complete.json`。其他 worktree、分支、业务资源和 prunable 记录保持。

最终集成读回覆盖 7 个 scratch 数据库、81 个桶、33 个 collection 和 156 个精确 Redis key；
已移除 3 个专用容器、本地临时角色及其空 admin 库。旧 ingest fixture 漏登记的 5 个任务图片归属 key
由本批已登记 task ID 推导，删除前记录所有权、类型和 dump hash，精确删除后再次确认清零；
证据见 `integration/native-owned-image-key-cleanup.json`，未修改共享服务配置或未归属资源。
ES/OS 在这组用例中完成必需服务就绪；没有选择创建 search index 的 fixture，临时 OpenSearch 仅留下
内部插件/遥测索引，随独占容器移除，不把服务就绪称为真实搜索路径写入验收。

远程组合 CI 尚未运行，固定的 Web commit 尚未推送，consumer job 要在明确授权发布依赖后才能 checkout。
生产部署/切流、真实供应商、真实公网数据库及原生 MSSQL/DB2/Trino 服务未验；本地定向证据不替代这些层次。
