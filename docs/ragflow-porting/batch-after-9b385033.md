# 9b385033 后续连续 20 项交付

2026-10-23：20/20 已审结并串行合入本地后端 `main`、Web `master`，未 push。
这是定向组合验收；远程 `ci-required` 尚未运行，不代表生产升级或真实供应商验收。

## 冻结范围与组合

- 上游 first-parent 起点（不含）：`9b3850339bc0ea29eb691dbad28811bb9dd81e31`；终点（第 20 项）：`d08bf02d9bd4f66c9b4f9c1b62f021a4e1a92e15`。
- 规划上界：`519e7d98a5651564d4e35d6648f006cba4baaf4f`。后来观察到的 `247f3c8ed1749b34545e15dcdbe70987b008d9f3` 只用于追查直接后续修复，未扩大本批游标。
- 后端初始 main：`077af4055c67113d8022558ecea27fe8e9d7bc56`；最终可执行源码及消费者 pin：`6af489c54eb47f6c00651dee58868e5ec57d13d4`。随后仅增加本交付文档和进度索引。
- Web 初始 master：`1a64e953d50822b04d2e874c11e5546d33df5efc`；最终 master：`1250ebfbdabe4771e416d3f982b3bda0b1a310b3`。保留初始 README/视觉资产提交；[consumer pin](../../.github/consumer-web.json) 已对齐最终 master。
- 20 项编号、原始完整 SHA 与冻结 scope 一一核对；后端/Web 候选先独立验收，再由唯一集成者串行 cherry-pick/fast-forward。共享 provider 改动先合 A/B 基线，再接受 C20。

## 逐项结论

| # | 原始完整 SHA / 项目 | 处理与收益比较 |
|---|---|---|
| 1 | `39ee2fb12086e0566258dce9bf4d9eb393ca2e88`<br>Go: implement Rerank in NVIDIA driver (#14778) | 适配 NVIDIA NIM query/passages 与索引/logit 返回，严格 TopN、有限值、取消和默认 endpoint 映射；自定义地址仍优先。 |
| 2 | `daf8a58c4b26a2e78c5ed5b074ea82ccf40cd4e8`<br>Fix: add codeexec attachments output (#14787) | CodeExec 明确 content/attachments/raw/type 契约，失败与取消清空旧输出；Web 复用共享 ArtifactLink，CSV 单次下载，图片安全预览。 |
| 3 | `3e90d303e0355bb0305fb6476efd233ddb29def3`<br>Go: implement provider: CoHere and FishAudio (#14790) | Cohere V2 和 FishAudio 进入现有绑定工厂，复用严格传输/结果边界；只采用已核实的目录修正，保留尚未证明应删除的 NVIDIA 模型与 GLM 命名。FishAudio 音频仍明确不支持。 |
| 4 | `2f2d1569e6c5a36800c1f44211e985e236620441`<br>Go: fix retrieval test error (#14794) | 规范 dataset_ids 并迁移实际 Context Engine 调用方；私有创建者/有效成员范围在 SQL 中绑定，传播取消，保留更严格的 embedding 配置解码。 |
| 5 | `765cdc2ec2bcb79f69eb474ccc251540f95e6e53`<br>[Bug]: REDIS error #12870 (#13875) | Redis 词典改为惰性、0.5 秒有界读取、零重试及 last-good 快照；真实全文和 Memory 查询器均验证。Memory 每次查询的一次 GET 成本仍保留。 |
| 6 | `415169d49772baa51a308ca2ba7287f71aba0601`<br>fix(dify): add GET method support to /dify/retrieval for health check (#13837) | GET /dify/retrieval 为无鉴权、无检索副作用的健康响应；POST AST 保持一致。移除已无调用方的旧 GET 检索分支。 |
| 7 | `2717ee283f3485878f8a56ecfc4410f62fa29246`<br>feat(raptor): add Psi tree builder with original-space ranking and safe migration (#14679) | 保留 GMM/AHC，增加原空间 cosine 的 Psi 有界聚类、方法感知 cache/layer/ext 和安全替换；不照搬缺少完整写回与恢复核验的迁移。 |
| 8 | `139b76d2b1485c241ea2840d3625fe3da8475acb`<br>Chore(deps): Bump urllib3 from 2.6.3 to 2.7.0 in /agent/sandbox (#14824) | 仅更新 sandbox 锁文件到 urllib3 2.7.0；独立 frozen sandbox 环境验证真实 gzip 流的 4000 精确字节。 |
| 9 | `128a64eae5061df23092ff8c767d4ab34d0bb9d4`<br>Refactor(Go): remove hardcode in huggingface provider (#14822) | 现有 HuggingFace 配置/传输已覆盖去硬编码目标，保留本地严格实现并补充 endpoint/region/失败/取消回归。 |
| 10 | `02c2587ca4309833c4574681a0505ae24554f628`<br>fix(agent): support iteration item aliases in child nodes (#14146) | 子节点 item/index/result 短别名限定当前直接 iteration scope；精确引用保留 0/false/object 类型，普通文本中的同名单词不被替换。 |
| 11 | `558ea51a0f9fb3808071d6f52aec9dfb9569c94f`<br>Go: implement provider: StepFun (#14815) | StepFun 复用本地实例绑定、chat/SSE 和 indexed embedding 合同，不复制宽松的供应商成功判断。 |
| 12 | `a02b456720851d015a69621018efa1c1806a3e98`<br>fix(docs): correct broken knowledge graph construction link (#13838) | 原提交与其父树相同；核对有效前序链接与当前文档已覆盖。不制造空实现提交。 |
| 13 | `e8adc977bd44df10049e4c9d21ff215e8cb285d0`<br>Fix: some agent bug (#14829) | Message 同步/流式渲染、StringTransform 与 kwargs 副本处理保留合法假值；Web options 保留 number/boolean，长表单可滚动提交，DataOperations 对齐 object 操作。 |
| 14 | `f85e18afbc0d195067bb363f0a3a75e8cd664567`<br>Refact: sandbox quickstart.md & add tutorial for code exec component (#14786) | 复用现有数学包环境白名单，补真实子进程测试、配置说明和教程入口，不扩大默认可用包。 |
| 15 | `2cc206ee859f39e54c8a28ddb818cf7c77102e5b`<br>Test : aggregation edge cases for list and scalar values (#14170) | 生产聚合 helper 已等价，补 list/scalar 边界测试，生产 AST 不变；无需启动 OceanBase。 |
| 16 | `ebab3513c4a715626600008d0d60040b3f237382`<br>Go: implement provider: Baichuan (#14832) | Baichuan 复用公共 HTTP/SSE/结果校验，保持真实实例凭据和上下文隔离。 |
| 17 | `eaa2e46b1e2601584e82c52facc2352c54c62f30`<br>Go: implement Embed (embeddings) in Upstage driver (#14819) | Upstage embedding 采用 32 项批次、全局 indexed 重建、query 区分、严格维度/有限值与取消。 |
| 18 | `4374e07a29eb170fe16bcc109e313d2e2d89b0b2`<br>Speed up start time (#14833) | 最早 worker 入口以 setdefault 设置本地成本表，显式 False 保留；RAPTOR/GraphRAG 延迟导入，受控首次 Psi 调用可运行。 |
| 19 | `9ee481807fdb576da0133c1ff13649e4be982f0c`<br>GO: implement GET /api/v1/datasets/:dataset_id (#14834) | 注册受保护的 GET dataset，UUID1 归一化，合法零字节和稳定 connector 投影；外租户、失效成员和异常连接不泄漏部分数据。 |
| 20 | `d08bf02d9bd4f66c9b4f9c1b62f021a4e1a92e15`<br>Go: add ASR, TTS, OCR command (#14836) | 新增 ASR/TTS/OCR CLI 与三条受保护路由，采用可选能力接口，避免 120 个重复不支持方法及 ParseFile panic；内置驱动明确 501，受控可用 adapter 验证完整合同。 |

## 候选到实际主干映射

下表为本地实际提交，候选提交不是运行服务版本。重复目标代表同一交付单元覆盖多项，计数仍按上述原始 20 项。

| 项目 | 候选后端 → 实际 main | 实际 Web master 中的提交 |
|---|---|---|
| 1 | `5cc8a8b1c2a164b6a877e9989f52fd957199cda5` → `50d3684d5bbdb67dec3c58148e599a44cb4257bb` | 无需修改 |
| 2 | `7e062fbc7e5cf786eca6426532d13956187fef9e` → `f0858ba11fb591fced6200abd114d3771c228ca5` | `4ee89afd1e3817fb91df8fb0e028727f313e8600` |
| 3 | `81095746100fe6df6afdb1c6322797876f1be0ab` → `0fbcf8437ca61af8c3870e3682c70fed62334af7` | 无需修改 |
| 4 | `51d71e8c8d5008fb77c8c9fb83d04df05da5e6d3` → `cb2694d57c10f309baaf661ab4a512a15e9c2904` | 无需修改 |
| 5 | `fb78023b218525118bf17b52c50133257480c1d7` → `337ff9e66900c6b24496b49be772444e8ed9a904` | 无需修改 |
| 6 | `8c18a19663455084db85d1032b36c8804d3b9be1` → `bf5f25200856b5def925cd7af32b7ff5d486fcf5` | 无需修改 |
| 7 | `42c85881ffec0d9e71775a34ba7b715ddf6ab73c` → `a7d80ee65f737da378a2c8b226c56522cdd08397`<br>`6a136bf1c85ea2b5471552d88b5ab1cfa3110dfd` → `bdb636187cc9a587f7af13a00e1ef1e3e5c476f1`<br>`8deaf8946cbce13fb2075f0638dea8848b9f3d4f` → `9fa32db30df31f76cc1ceeb5b942d2b7b481e8f4`<br>`1f3abe4efa7c9741d08aedda4547a324c0bd3f7c` → `885d33ae0be08efd4885602f9b2f2ac122972ed7`<br>`62a83dea6ae4c9084623fbf5599b91953e76ded5` → `63697e6854703e9c343f0dc6e45706de0a05534d`<br>`5a778aa5889c3187a52bec6f2fe4b7725ceb3a92` → `5b1e016688c16edb1b043d5ebf1fe7c2bb87991b` | `1250ebfbdabe4771e416d3f982b3bda0b1a310b3` |
| 8 | `5d7c12334bf3f9af608ba0c582ad608d1be05a16` → `7c4778f12247a173d9a63b5f58dd7f2503bbfbc0` | 无需修改 |
| 9 | `4fc39c88e151a119030063b62820437e5ddf6017` → `84707b835db52e1947a0f1fc12373f3605d7321d` | 无需修改 |
| 10 | `bfdb9de49eda6d5899385e1d8891d57a455e9cdd` → `6e5aef803a8b05b5e96873b5348ed019cd17e70f`<br>`538302f3189a3bdba232fb181a04ce319dded2c5` → `e73227fe1f4cf5a385ff127c071e464994adeb93` | 无需修改 |
| 11 | `81095746100fe6df6afdb1c6322797876f1be0ab` → `0fbcf8437ca61af8c3870e3682c70fed62334af7` | 无需修改 |
| 12 | 无新增提交，空 diff / 当前等价 | 无需修改 |
| 13 | `bfdb9de49eda6d5899385e1d8891d57a455e9cdd` → `6e5aef803a8b05b5e96873b5348ed019cd17e70f`<br>`538302f3189a3bdba232fb181a04ce319dded2c5` → `e73227fe1f4cf5a385ff127c071e464994adeb93` | `646afb437da46924288aad887ba68de6a79452eb` |
| 14 | `911109f45fdbe19b884640fa1c83cbdef67c11e4` → `3ba3b1452075b82d3eedcf8abf53b555fa74af45` | `9b3b0e71de795043e4be633dfa227bfcd2c84ee0` |
| 15 | `1d1283e757de3f9134edb10d55a7e5135f3a9026` → `0fe2c49d57df9672ebf694d8d2c1a5c79247e349` | 无需修改 |
| 16 | `81095746100fe6df6afdb1c6322797876f1be0ab` → `0fbcf8437ca61af8c3870e3682c70fed62334af7` | 无需修改 |
| 17 | `81095746100fe6df6afdb1c6322797876f1be0ab` → `0fbcf8437ca61af8c3870e3682c70fed62334af7` | 无需修改 |
| 18 | `c0ea032bf24e51e88cb6f663f7f38be44c3b8898` → `61e881bf6230df33d99f5675cebb765830c4f30c` | 无需修改 |
| 19 | `51d71e8c8d5008fb77c8c9fb83d04df05da5e6d3` → `cb2694d57c10f309baaf661ab4a512a15e9c2904` | 无需修改 |
| 20 | `b003971960da9fca3a7f6513123e09fc168c3769` → `8c5d1a22d5e13ca18cfeea5a57ed13b0da14d0e6`<br>`a109d20d8cc09dd8c984effd6eecb976188f4c9b` → `5694f3cc2e41d92425b2107e96c12185c8fb501e`<br>`c498b5f68a9f8540900832c46f2aff025698d3a9` → `3144801bfe23d3394af792d35db7d8d58e47c649` | 无需修改 |

B3 的目录协同提交 `906ccfe6e42c531e11a380719d13ce5b8cbd32d7` → `42d58e23843e7e5e029f32ff9435ef32886ab999`。
组合审查另外发现 CodeExec 使用 `kwargs.get()` 丢失显式假值，并绕过短别名解析；已用
`dacf2c82f9278e3e46c813f8118351b8ac14c632` 修复。实际 Canvas → Iteration → CodeExec → LocalProvider → Message
覆盖 `0`、`false`、空值和嵌套对象，补在 [组合回归](../../tests/unit/test_code_exec_iteration_inputs.py)。
最终消费者固定提交为 `6af489c54eb47f6c00651dee58868e5ec57d13d4`。

## 机制审查中的关键取舍

- RAPTOR 保留原空间排序和有界 bucket，拒绝空 summary 后再 embedding。模型 I/O 后重新绑定 KB/Task/tenant/index；精确 task ID、typed receipt 和独立 native/SQL 读回共同约束写入。SQL 失败恢复比较完整已确认 postwrite 行及向量，先核对全批，保留后来同 ID 的 content/vector/extra winner。取消先 drain 原生写入，再释放 KB 锁；clear 不能复活已取消树，非法 receipt 不解绑 Task/KB。兼容生产 Milvus `pk/id/extra` 与 float32 规范化；原生替换入口见 [raptor_migration](../../core/utils/raptor_migration.py)，其他引擎的实际验收边界见下文。
- Go 模型链沿用服务端可信实例绑定，不接受请求覆盖凭据；共享 helper 严格验证 shape、indexed 顺序、维度、有限值和上下文。C20 限定 20 MiB 输入/结果/流累计值、最长 120 秒上下文，CLI 同时限制普通响应和多行 SSE 缓冲。stream 出错或无事件不能发送成功 done，CLI 返回失败退出码。
- ASR/OCR 显式空 text 是合法静音/空白页；缺字段、null 和空对象失败。TTS 必须有非空 audio 与匹配 format。当前内置驱动均明确返回 501；受控 adapter 的成功不声称真实供应商能力。具体调用方式见 [Go media](go-media.md)。
- Dify GET 健康检查与 POST 检索合同分别验证。实际 Dify 1.14 源码的 bodyless POST 验证行为也被单独记录，未把该调用当成 GET 健康检查或部署证明。

## 最终定向组合验收

原始命令、完整 source/lock/config 哈希、退出码、JUnit、HTTP/SQL/native 读回及截图均保存在本地外部证据根：
`~/.local/state/multirag/validation/20261023-batch-after-9b385033/`。
`scope.json` 是冻结范围，`integration/item-map.json`、`handoffs/*-review.json` 和 `*-accepted.json` 是逐项审查和映射。
没有把 Web、模型资产、私有配置或评估数据入库。

| 检查层 | 实际结果与证据（相对证据根） |
|---|---|
| Python unit 组合 | 26 个相关文件，711 个不同用例最终通过：`integration/python-unit.xml` 中 666 pass；仅因缺失 basetemp 父目录未启动的 45 项，补建后 `python-unit-repaired.xml` 45 pass。两次源码、锁文件和配置一致，补跑选择保存于 `python-unit-temp-repaired-selection.json`；没有重复累计用例。 |
| Python SQL/native/Redis 组合 | 33 个不同用例通过：Psi 真实 SQL/Milvus 10、Redis 4，见 `python-native-report/junit.xml`；同一 basetemp 问题阻止启动的配置 16 + Dify/owner 1，见 `python-native-repaired-report/junit.xml` 17 pass；真实 CodeExec debug/download/LocalProvider 2，见 `python-browser-report/junit.xml`。服务在收集前准备，`REQUIRE_SERVICES=1`，未用 skip 替代必需环境。 |
| Python 静态 | 改动文件 Ruff/format；配置纳管范围 mypy；8 个 import contracts；async DB 检查全部 exit 0。见 `integration/python-{ruff,format,mypy,imports,asyncdb}/`。 |
| Go 最终组合 | `go build ./internal/...`、`go vet ./internal/...`、CLI/server/admin 三个入口构建通过；models/entity/CLI/router/nlp race 1713 pass events，1 个未提供独立 opt-in DSN 的既有 retrieval 存储测试 skip 单列，不计入通过。相关服务 race 20 pass events；真实 dataset HTTP/SQL/Infinity 与 media HTTP/JWT/SQL/CLI 各 1 pass，零 skip。见 `integration/go-*`；事件包含父/子用例，不与 Python 用例数相加。 |
| Web 最终组合 | 在独立工作树和 COW 依赖副本上，完整 TS + Vite build、agent strict、ESLint（0 error，既有 warnings）、i18n、文件体积与 security 均通过；agent T1 75、附件/表单/draft 47、settings 12、真实 DOM parser 15 各自通过。见 `integration/web-*`。security 单独执行，不并入前述数量。 |
| 最终浏览器消费者 | 当前最终 Web 实际 CodeForm / SingleStepDebugSheet 调真实已注册 Python API、隔离 SQL/Redis/S3 与 LocalProvider：CSV 精确 21 bytes、单次下载；PNG blob 预览 opener 为 null；空附件与失败清旧值。实际 H 组件键盘提交 0/false 保留类型，object 操作及 40 项长表单可达并提交。实际 RAPTOR settings 路由经受控 HTTP 保存 AHC/max_cluster=317 并刷新读回；键盘移动焦点 + Space 激活。明暗主题完成拼图审阅；G/H 使用生产 setTheme 并关闭截图过渡重拍。`integration/browser-evidence/contact-sheet.png` 六格均带文件名、路由、视口，已先拼图审阅。 |

最终可执行组合均绑定后端 `6af489c5` / Web `1250ebfb`。文档提交后不重复启动业务运行时。
单线已核对输入哈希、仍适用的证据直接复用：sandbox urllib3 frozen runtime、G 的 ES replacement、worker 冷启动/首次 lazy Psi、A/B 受控供应商和各自原生读回。
报告中的历史 pass 不冒充后续修改版本的全量结果；此次新增的 CodeExec/iteration 组合问题已在最终源码补验。

本轮保留了初始失败：缺失临时目录父目录、macOS tsx Unix socket 路径过长、最初冷导入中断、CodeExec 假值/别名红例以及 G 完整恢复读回审查修复。
临时目录问题只修隔离运行目录；浏览器第一次截图捕捉到主题过渡中间帧，已改用生产 setTheme 与禁用动画重拍。浏览器探索中错误引用/等待条件不计为通过证据。
H 曾单独复现的 10 个 TokenizerParam ingestion-template 既有失败不在此次 711 项组合中；未扩展修改该无关基线。

## 清理与保留

A–H 均在接受后清理；主集成逐项验证容器/卷、原生存储、端口、工作树、恢复 bundle 和 patch 等价，再删除本批候选分支。
D 的 managed worktree 已在其所属 chat 读回为 archived；不是仅凭 queued 结果判断完成。
主集成同样以注册 ID 清理自建 PG、Valkey、MinIO、Infinity，独立核对精确 SQL DB、Milvus collection、Redis key 和 S3 bucket 不存在；关闭自身浏览器/runner/listener，删除独立依赖、私有配置和临时目录。
主集成工作树保留可恢复归档，普通 Web 工作树移除；外部原始报告与恢复 bundle 保留。
初始 28 个无关 WIP 的原始字节与状态保持，PROGRESS 中既有 Manual/cancel 变更未暂存；只加入本批顶部索引。
旧 SDK/ironwood/g129 等工作树、共享 `.venv`、`node_modules`、公共缓存及已有运行服务均保留。
独立完成记录见 `integration/*-cleanup-independent.json`、`integration/cleanup-services.json`、`cleanup.json` 与 `delivery-complete.json`。

## 未覆盖范围

未运行完整本地 verify/core 分片、远程 merge queue/`ci-required`、生产切换、真实付费 LLM/embedding/audio/OCR。
冷启动与受控首次调用不等于完整队列 worker 的 DONE/ACK；Dify 源码消费者不等于运行中的 Dify/SSRF proxy。
此次真实原生覆盖为 PG、Milvus、Redis、S3/MinIO、Infinity 及单线隔离 ES；OceanBase/SeekDB/OpenSearch 的迁移未作真实服务验收。
所有本地提交均未 push，后续最终远程组合仍须通过适用 CI。
