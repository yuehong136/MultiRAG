# bbb0798c 后续 20 项本地交付

2026-10-23 本批 20/20 已完成逐项评估、适用实现与定向验收，串行合入实际后端 `main` 与 Web `master`。
本地交付，未 push、未部署，远程 `ci-required` 待运行；本记录不代表完整 core、全部集成或生产验收。

## 冻结范围与组合

- 后端起点：`ed42a4c7f8059cb6e633f07f9d546351d340a91f`；Web 起点：`9cb81b046e409690241495ee893dc077c5a37bf3`。
- 上游 first-parent：排除 `bbb0798c41e760b9870f98b6f0c667d7659b2491`，连续 20 项至 `1c0eaa504bc477ba704f5845b85648624e925046`；后续修复查阅上限 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。
- 最终后端源码/测试组合：`80d1a3521a43f4d2a2f2b88f6158bce935983b19`；Web 实际 `master`：`7c9576840f2e4b7c7dbd0cd99c8c33f63dc3aff2`，由 [consumer pin](../../.github/consumer-web.json) 固定完整 SHA。
- 原始完整 diff、逐项审计、候选映射、命令/退出码/JUnit、资源与独立读回保存在本机 `/Users/xldu/.local/state/multirag/validation/20261023-batch-after-bbb0798c`。最终文档提交和清理后的主干 SHA 见该目录 `delivery-complete.json`，避免提交自引用。
- 既有未提交工作不属于受验组合；受验源码使用隔离工作树，实际主干合入后保留这些改动。

## 逐项结论

| 项 | 原始 RAGFlow SHA | 线 | 最终处置 | 实际提交（短 SHA） |
|---|---|---|---|---|
| 1 | `bf90c8948afd80f63a17154ccaf7517c8d64b778` | A | 适配 Go Zhipu 实时模型发现，保留取消、错误与真实绑定；CLI 使用 Bearer 鉴权。 | `fbc0a557`, `a3af0210`, `a96296c9`, `3f141f76`, `0cbae2fb` |
| 2 | `8cb2bf04fb670ed8485e620469297355958b5e00` | B | 保留本地先读旧凭据再按厂商合并的更强实现，补提交/失败零写回归；Web 改 Model ID 文案，payload 仍为 endpoint_id。 | `d3e9f0a2`, `1453c157` |
| 3 | `b18640d2287f3b68b47c97b8fc1a790946b04bfa` | A | 沿可选媒体接口接入 Gitee 文件/URL OCR；保留严格 base64、大小、超时与失败语义，其余未实现媒体能力仍为 501。 | `e4747f00`, `3f141f76`, `055c939a`, `0cbae2fb` |
| 4 | `f3b3596c2961640484d575ff3def5ad9f3526651` | C | API 入口在承重 import 前 setdefault 本地 LiteLLM 成本表，保留显式覆盖。 | `586389b9` |
| 5 | `3182fd0789e3fa5c6b8ca5cc7bea8469b5a323b4` | D | 31 处版本足迹按本地实际产品与包版本核对；不复制上游产品版本身份，仅修 Admin CLI 两处过时产品名。 | `3dbc94ea` |
| 6 | `0a4b733b2a20ef97db03af4a9c376aff93a02888` | A | 完整适配 LocalAI chat/历史/SSE/embedding/rerank/发现与目录，三处消费者允许无 key；严格索引、维度、有限值与 TopN。 | `3f9d0cb3`, `a3af0210`, `a96296c9`, `3f141f76`, `055c939a`, `0cbae2fb` |
| 7 | `9ed4da74b8e8cddacd9e55a07c1d7ad202481275` | D | 发布说明草稿链按最终有效内容去重；无独立本地代码目标。 | 已有等价 / 文档链去重 |
| 8 | `87516edadfba44f4bfb54633cfdc7552c5de8538` | E | Infinity SDK 与 Compose 镜像同步 dev7，冻结锁文件仅对应包与根依赖变化；dev6 记录标注为历史。 | `ac7a98ae` |
| 9 | `bb1a6259d330266dbcf00bafa23e88036f78cf23` | D | 发布说明后续草稿并入最终审计，无重复实现。 | 已有等价 / 文档链去重 |
| 10 | `7f699d12020c5ffdb98775cf8a90911e11115f3b` | F | rerank 在读取凭据前限定可信授权租户集合及完整数据集范围；沿请求 AsyncSession，空范围拒绝，管理入口合同保持。 | `01e0726a` |
| 11 | `30d1c1dc28c6c478fcd38346e16ee30202035fa0` | A | 现有可选 OCR 接口已避免上游签名冲突，以编译证据接受等价；不制造空提交。 | 已有等价 / 文档链去重 |
| 12 | `e994051eb9f815b3ca54f9d4ee23e6c1c1b99fbf` | G+J | Python 通用 REST connector 与 Web 配置/显式测试共同适配，G/J 合为一项；GET/POST、四种鉴权、分页、JSONPath、metadata、身份与失败检查点。每跳 Requests 规范 URL 后校验并绑定 DNS；混入 UI 路径逐一保留本地更强行为。 | `e49d58fa`, `246d798a` |
| 13 | `8c5845f6ca5e7add9c4e02660f24477750467fe7` | C | pdfplumber context manager 位于既有锁内，异常出口关闭资源，保留畸形输入/空页/缩放与 None 合同。 | `1a4148d5` |
| 14 | `9e0f9767296fb28a8682083d2a67f60d82d9667f` | H | 17 个 Widget 设置沿既有 DSL globals 保存，重新 GET canonical Agent；安全颜色/文本/链接、静音音频生命周期及当前 iframe 边界。保存保持草稿语义，已发布版本不变。 | `7c957684` |
| 15 | `cb49f47c38bf583bcd56a711714009388b7aa128` | D | 发布说明编辑演进按最终内容核对，无独立本地改动。 | 已有等价 / 文档链去重 |
| 16 | `dd76653dc14921988196e64396c38795e3858538` | I | Agent 标签 additive SQL/Alembic，owner 更新、有效 own/team 聚合、精确 token OR 过滤与排序；Web 编辑、筛选、计数与缓存刷新。 | `c258ad12`, `a3d8020f`, `67594f1e` |
| 17 | `cb01529d8bfaff487c2fed4c9cadccd447b34f80` | A | Voyage embedding/rerank 使用实际协议字段，严格输出；不提供静态发现或隐含付费探测。混入 LocalAI/Novita/LongCat 测试足迹已逐一映射。 | `f224cb1c`, `a96296c9`, `3f141f76`, `055c939a`, `0cbae2fb` |
| 18 | `e577901388c084b5adb0c1f847d21a323ae36fe1` | D | 发布说明格式演进已合并最终审计，无独立本地改动。 | 已有等价 / 文档链去重 |
| 19 | `b2b63600f164e94ce45bdece9b3f61377cc06986` | B | Python/Go OpenAI 目录补 gpt-5.4-mini/nano 的 400k、chat/vision/tools 元数据；目录不等于真实供应商验收。 | `b2a9ff2e` |
| 20 | `1c0eaa504bc477ba704f5845b85648624e925046` | D | 最终发布说明的 14 项能力声明与本地代码逐一核对；不引入上游发布身份、全兼容或性能声明。 | 已有等价 / 文档链去重 |

## 候选到实际主干映射

下表均经 stable patch-id 对比；G/J 为同一项的前后端，H 无后端业务提交。

| 线 / 仓库 | 候选完整 SHA | 实际主干完整 SHA |
|---|---|---|
| A / backend | `62c75d9058f0dcabbb3a8adafb438848b486dcbb` | `fbc0a557d9d281767b65600d3699daeb6728599b` |
| A / backend | `2e836ef9b5f2f0804102df1a36f7728708b64ea0` | `e4747f00f72e944f96fc638b8d0a769e185dcb0e` |
| A / backend | `16f0464551b1876cf2061fd14276dcd13b3c688d` | `3f9d0cb30947f00c2464efdeaa24784b86e4f081` |
| A / backend | `48a9a456f1b89429f0e8f816cbd8a60e6df2ec58` | `f224cb1cdfd6c24f687bca9fd21a555d282e10bc` |
| A / backend | `51c7740b9db76b00bf95317c49c99f89eb312faf` | `a3af02103f2a358bd4a70d8f80489f6e2fabadaa` |
| A / backend | `78d92f7c53c61d9e822824a3f86d2644f37a54e6` | `a96296c9dc688331f3963f4e8e9ba781e831b208` |
| A / backend | `0d91ce135ba790d9baad40e97537621a6cb43601` | `3f141f76f84bbb8b8e9d1dc03762003e16a8d53b` |
| A / backend | `f24d134f22d1782be1c34ef81af526c87bcfe7b5` | `055c939ab78511549ecd718a46ac9b36b5abce38` |
| A / backend | `e9360271ef6e62b5baeff979a3adf7eef10c9937` | `0cbae2fb89e23af662a6a2875ab1a5500cc2d366` |
| B / backend | `91cfa66f0b833b5bf4569e60d018faceb0d4178c` | `d3e9f0a2a5a89cd89751204df8d1ac6d35f2bce3` |
| B / backend | `d534e632e528f8a6a6d7584075bd7e15cf75ccda` | `b2a9ff2e4a3db04b67b86d26890b24970614caed` |
| B / web | `fd9a66702fd3deb2cefa6b33e0a1eb7a7425755a` | `1453c157cf2aa0490820698ffd1e9405481dbce4` |
| C / backend | `1b0763c072f03735aefae5604df10d0bc60d5a27` | `586389b94905921ed43fc486eeb4806fef7e5f07` |
| C / backend | `7242b088c7cd31899198b3f61184aa839b488571` | `1a4148d5c7985b7280037508498d31c1fd796443` |
| D / backend | `fa3a37476b4536b8d314cb171996d1fe50a43ef1` | `3dbc94ea6722bf558047c27190e96399e2451eb4` |
| E / backend | `c5a28a7a12e9d0ab3a2118fd9fc763b707bcae38` | `ac7a98ae87c5d977a66b634e0f2363013d2cde36` |
| F / backend | `e3bac1eefd4c154821243e088eac2b30c8e57b6e` | `01e0726a7c1b7379274c9d05cacccfffd061959e` |
| G / backend | `0f2c68d52a5444c25d1bef73b2e757b34b5a7ae2` | `e49d58fa87d27d36b3f63d576c30e587b634b1b1` |
| H / web | `93a7357c53cab011ef7b912d2d240daeb7439642` | `7c9576840f2e4b7c7dbd0cd99c8c33f63dc3aff2` |
| I / backend | `0976a9ced3ad9d08ba079c4441542921dd5897b5` | `c258ad12966d61e00b987ff7d19249502a6ad11d` |
| I / web | `9ad17d4b2f9c6142de428cd075e9d85d6197e9ed` | `a3d8020f63d89e7e7eff8295ac212506541440ce` |
| I / web | `debaf1bf6f5da433289660ffd54ea8c595618456` | `67594f1e823243da239dc280fa980ec6f9c366ad` |
| J / web | `4f6181b654d8d49092a7bf330f298648b9d6ad07` | `246d798ac691490d66e370b06f60379236a4c8fb` |

集成 owner 补充：`0844ec78` 登记 Go opt-in、测试分组和消费者 pin；`17100d6e` 更新稳定路径映射；`ccf20c71` 修复已缓存 API import 下的测试资源初始化；`a6ef9960` / `80d1a352` 补齐 Agent 详情读产生的 Redis 副本清理及依赖。Agent 标签测试保守使用完整服务栈，不进入仅 PostgreSQL 分组；rerank 的模型/检索外部边界受控，数据库分组保留。

## 实际检查与证据复用

最终集成在 `80d1a3521a43f4d2a2f2b88f6158bce935983b19` 完成；以下较早 SHA 的证据只在输入不变时复用，差异与哈希比较见 `integration/evidence-reuse-proof.json`。不把重复复跑相加为新增覆盖。

| 层次 | 本批结果与报告 |
|---|---|
| Python 定向单测 | `python-unit.xml`：487 passed，0 failure/error/skip；受验 `17100d6e` 到最终只变四个集成 fixture/分组文件，生产代码及这些单测不变。 |
| CI/分组合同 | `final-registry-and-fixture-contracts.xml`：22 passed，含分组依赖、隔离并发与 skip 失败证据。 |
| 静态 | 受改 Python Ruff/format；mypy 147 文件；8 个 import 契约；async DB 门禁均通过。后续 fixture 的 Ruff/format 已补跑。 |
| 真实 SQL/HTTP/消费者 | `native-final-4-report/junit.xml`：14 passed，0 failure/error/skip；模型凭据、rerank 权限、REST sync、Agent tags，含最终 Web 的实际 APIClient 消费者。PostgreSQL、MinIO、Milvus、Redis 先经服务协议探测，`REQUIRE_SERVICES=1`，禁用 Testcontainers 回退。 |
| Infinity dev7 | `infinity-report/junit.xml`：24 passed，0 failure/error/skip；真实 SDK/服务、并发 DDL、独立读回与 lease 回收。E 的 dev6/dev7 tokenizer 对照、165 单测及原始 24 用例另保留；未重复相加。 |
| Go | `go-combination-race`：3 个受影响包 race 通过，1658 pass 事件、1485 leaf cases，0 fail/skip；488 个 Go/模型输入与最终一致。复用 A 精确候选的 build/vet/三个真实二进制及 JWT/CLI/SQL/受控供应商 HTTP 验收。Go 为停滞并行实现，不等于 Python 生产服务。 |
| Web | 最终完整 `npm run test:ci`：1541 passed（801 source node + 649 Vitest + 81 desktop + 10 tooling）；225 文件 inventory、build、Agent strict、bundle/file-size 和 lint 均 exit 0。lint 为 0 error / 1368 warnings，构建保留大 chunk 提示。 |
| 浏览器 | B/I/H/J 真实页面和标注拼贴图已审阅。J 最终后端/同一 Web 复测 create/test success/401，独立 SQL 证明测试不保存/不入队；H 真实 APIClient→FastAPI→PG 保存、重开、reload，发布版本保全。H 使用的 update_agent/get_agent AST 与最终一致，Web 文件不变。SSE/音频和供应商故障注入边界单独标明受控。 |
| 启动 smoke | `startup-smoke/run.json` / `runtime-result.json`：实际独立 API 进程，父环境不设成本表变量，ping/version、401、真实登录与 token 列表通过；scratch SQL 83 表、1 用户、0 token。healthz 的 DB/Redis/doc engine/storage 为 ok，chat 刻意不配置导致整体 HTTP 500；非 strict smoke exit 0。最终仅测试 fixture 变化，启动生产输入不变。 |

## 失败修复与未验证范围

- 组合首跑 10 passed / 4 setup errors 暴露缓存应用与部分资源状态的顺序依赖；修复数据库 fixture 的明确替换及存储 fixture 的显式初始化后通过。中途冻结修订产生的主动中断记录保留，不计通过。
- 清理读回发现 Agent 详情生成的 Redis 副本，先验证随机 owner、payload 和 hash，再 CAS 删除精确键；夹具新增清理。第一次 teardown 因 decode_responses 返回 str 而失败，修复后最终 14 用例含 teardown 全通过，独立读回无副本残留。
- G 的 IDNA / redirect DNS pinning 修复后才接受；早期失败、断言修正与 Web 初版受控持久化记录均保留，不用后来的通过覆盖历史。
- E 已在同输入基线复现 `uv pip check` 的 3 项既有冲突（xgboost 平台、moodlepy 与 attrs/cattrs）；此次不改无关依赖。此前登记的宽 CI harness 两项基线失败和 Go 其他用例失败见 [上一批记录](batch-after-d08bf02d.md)，本轮未重跑或宣称解决。
- 未运行完整 `make verify` / 完整 core 四分片、远程 CI、生产迁移/部署、Linux amd64 或既有生产卷升级，也未调用真实付费供应商。局部受控 HTTP、scratch SQL 和浏览器通过不证明这些层次。
- Litellm 在解释器退出时仍输出 LoggingWorker 清理告警；相关测试进程 exit 0，未吞异常或放宽测试断言。

## 资源、WIP 与清理

- A–J 精确候选工作树/分支均先校验候选映射、生成并 verify 恢复 bundle，再逐一移除；H 的测试后端和 F 的无改动 Web 也保留恢复材料。不做全局 prune，不操作其他工作树。
- 本批 runtime 的随机数据库、角色、bucket、索引、Redis 键、容器/卷、listener 与私有配置均按登记和独立读回清理；集成 owner 的 PostgreSQL、Infinity dev7、Redis 也已关闭。共享模型资产和现有业务服务保留。迁移及 scratch 数据库清单从 durable basetemp 归档到永久证据目录。
- 开始的 28 个无关 WIP 路径保留：除两个重叠文件外字节相等；Compose 只合入 Infinity dev6→dev7，原 MinIO WIP 保留；PROGRESS 只插入本批索引，移除该块后恢复最初字节。最终 index 空。
- 最终主干 SHA、恢复包哈希、无残留检查、WIP 读回和全部命令报告摘要在本机 `delivery-complete.json`。未 push，远程 CI 待运行。
