# 41072ed4 后续 20 项本地交付

本批 20 项的逐项评估、适用实现和定向验证已完成，范围内源码串行合入本地后端 `main` 与 Web `master`。
本地检查保留下述既存/无关失败；完整组合未全绿。未 push、未部署，远程 `ci-required` 待运行。

## 冻结输入与组合

- 上游 first-parent：排除 `41072ed44d1b8fb10fac040970b9df8da394459a`，连续 20 项至 `9d94527b1d98f18b8c09a32e24f0721c9a2cc5b1`；后续修复参考上限固定 `519e7d98a5651564d4e35d6648f006cba4baaf4f`。20 份完整 diff 的顺序与 SHA256 已核对，不开启下一批。
- 后端起点 `ccef3d305bce1dc0f6d5b3833c7818e81c86508c`；Web 起点 `f7767371e87ce59208a28b5e8ee2761f7e741fa1`。
- 最终源码/测试组合 `b2d2e8a38f5e412783cc5566717fdb533c0904f6`；Web 实际 `master` 为 `170315a8b7d46b1b752b3f574e41b86738c7c2b3`，由 [consumer pin](../../.github/consumer-web.json) 固定完整 SHA。
- 原始 diff、审计、命令/退出码/JUnit、运行输入与资源读回保存在本机 `/Users/xldu/.local/state/multirag/validation/20261024-batch-after-41072ed4`。文档提交后实际主干 SHA、恢复包及最终清理结果见该目录的 `delivery-complete.json`，避免提交自引用。
- 受验源码来自独立工作树；共享后端原有 28 项 WIP 保留。主 Web 处于独立 `codex/frontend-modernization` 分支，外部任务已提交到 `156d23c781460553ff80c6537df42211a8255508`；该分支不属于本批接受组合。推进 `master` 的窗口中，当前分支 HEAD、index 与 2421 个 tracked/非忽略文件字节均未变化。

## 逐项结论

| 项 | 原始 RAGFlow SHA | 线 | 最终处置 | 实际提交（短 SHA） |
|---|---|---|---|---|
| 1 | `5a5bbee948c7dbd27996167c7039e6928e9d9a8d` | J | 发布说明按本地现有能力逐段核对，保留 MultiRAG 产品版本与实际发布事实，不复制上游发布声明。 | 已有等价/产品身份保留 |
| 2 | `58819f5d3e1a52bded15cd0693092cc7f69aa980` | B | 已有更强的文档/租户/存储来源授权；普通与 beta SDK 保持空文件错误，canonical REST 原始下载允许合法零字节。实际 Web 和目标 SDK 验证等价，不削弱鉴权。 | 已有等价/产品身份保留 |
| 3 | `d887b578c5070807cb3d57fa2122a8fc69ecc59d` | F | 附件描述符快照已有等价；修正实际浏览器暴露的旧会话 history/create/stream 与 rename/delete 迟到回写，切换时终止旧流；无全局 file cache。 | `28c8d9e0 Web`, `170315a8 Web` |
| 4 | `ef2969a46207e050c9f7ebedbd993ea8363027f0` | C | 活动 QWenEmbed 使用逐请求 DashScope 原生 base_address，映射国内/国际根地址；严格批量索引、维度、有限数值与用量，保留合法零值和有限重试。 | `98ac6c24` |
| 5 | `4c68a6b86ce0f126b4bbbc5df9f83d74ea8833a4` | G | Python retrieval 显式传 top_k；Web Agent/Tool 的关键词权重与 Search 向量权重已有正确互补合同，按实际消费者证明等价。 | `1fcbf97a` |
| 6 | `547b8cf9d84fe14f677fff1175664a44bd3351e0` | B | 始终使用 RestrictedUnpickler，退出不安全环境变量分支；保留 Buffer/NumPy 输入兼容。不将已有 NumPy whitelist 宣称完整沙箱。 | `e3403f96` |
| 7 | `3a5df08c76debe0add51c598f26996a14ce35ebb` | A | Go 真实异步 parse submit/list/show、Gitee 任务与 CLI；保留显式 MinIO 配置优先级及旧 minio_0 回退。补缺失 index 拒绝与当前选中模型回归。 | `6244c12c`, `53e05d49`, `184c0913` |
| 8 | `c9622d0924beb773e9fe5d28a288bcb020538e9b` | D | 已注册 beta agentbots 非流式 completions 复用共享聚合器；保留 structured/reference/trace/pause 与 iterator 释放，完整 SSE chunk 严格解码。 | `1e113869` |
| 9 | `eaa5d9921bbc0f0c4c8c601f91d8392837871493` | H | Python 同步与 Web 新建表单 PR/issue 默认 true；显式 false 保持，缺失字段按 true 兼容。真实 REST/SQL、有限同步、浏览器保存重载。 | `deebb330`, `d87f92dd`, `5bc67420 Web` |
| 10 | `86bcf9767d3311341502f76a8337480b38a551f9` | A | Go vLLM 复用严格 rerank HTTP 合同、允许无 key；实际 SQL 绑定、CLI、检索分数与拒绝副作用验证。 | `c022ce88` |
| 11 | `1a25191b138e7fc15c3c27f9c715865ef103eb35` | J | 新增与当前 Python registry、独立 Web、Compose 和 keep_alive 一致的 Ollama 文档；静态与链接验证，不声称真实 Ollama 运行。 | `439adaf3` |
| 12 | `335dd5a263ef8ff66158509f47c4eb9bc0958f20` | A | Go dataset 文档列表先授权后读取；名称/UUID1、分页/total、SQL 独立模式与配置引擎 metadata 分别适配。真实 Infinity 读取 id/KB，畸形 metadata 明确失败，CLI 正常 exit 1。 | `32b2d7c0`, `290d3d19`, `ceff7542` |
| 13 | `c2863173b070647d9ba49f1ea4eb05dcae84fa30` | A | Go SiliconFlow ASR/二进制 TTS、StepFun base64 SSE/结束合同及默认 voice；真实多实例 SQL 绑定与 CLI 保存字节读回，修正目录尾空格。 | `8ffd1f85` |
| 14 | `cb606e1c38d9466b44098c13ae6d7df9a2fac23a` | E | 四个活动 PDF 构造器统一 model_species 与完整类型注解，保留解析链及无关 manual.py WIP。 | `93fa2033` |
| 15 | `14c0985182700f11dd568cba9aa75a73e0618fdd` | I | 保留活跃 3.12/3.13 支持；枚举等迁移已有等价，移除无用 TensorFlow。真实复现 3.13 NumPy wheel 缺口后按版本条件固定 1.26.4/2.1.3，其他包版本保持。 | `2a93ddf8`, `8f813946` |
| 16 | `bf41d35729872088273b5b1a8b6d8f9822cb5fd9` | A | Go Baidu/Mistral 多页 OCR、PaddleOCR Job/有限轮询/严格 JSONL 与受限结果下载、CoHere ASR；复用可选媒体能力和 SQL 实例，按新增能力更新精确测试断言。 | `009090ef`, `b2d2e8a3` |
| 17 | `09d45046e5f46fb2300f8860a3819568d71916d3` | F | 共享 XMarkdown 配置与文档 Markdown preview 支持单换行；现有 Mermaid 路由等价。保持代码/数学/HTML 净化，新增 remark-breaks 及其一项依赖。 | `ab57f210 Web` |
| 18 | `ff318aba7a9f6a7b7eec5f47747b1cf51b3095b9` | D | literal_eval 正确分派，bool 先于 number，clear 返回 False；保留 falsy 值、授权范围及无参数 remove，实际 Canvas/SQL 验证。 | `792884b7` |
| 19 | `e194027b01112f3f7ad91558c19830dcec7881ed` | E | 普通文本 trim；Markdown/HTML 仅过滤空行，保留有效行缩进/尾空格；结构化 JSON/chunks、图片/位置和根标题保持，不引入上游提前空返回回退。 | `a6587cd4` |
| 20 | `9d94527b1d98f18b8c09a32e24f0721c9a2cc5b1` | I | SDK/lock/Compose 固定 Infinity 0.7.0 正式版；本地无 Helm。补实际 LIKE/NOT LIKE/ESCAPE 7 项，保留当前存储、身份、竞争重试与连接归还合同。 | `778b7654` |

## 候选到实际主干映射

以下候选与串行摘入提交已逐项核对 stable patch-id；无源代码改动项不制造空提交。

| 项 / 线 / 仓库 | 候选完整 SHA | 实际完整 SHA |
|---|---|---|
| 3 / F / web | `d4c61b8f1c6d7e35253a36f3cca1dc7b800c0f87` | `28c8d9e03216ff33239bce9b18cce32a725f9675` |
| 3 / F / web | `7b01cf5a9d3a96eb8d974f8f19e3b3fe42b4c8b1` | `170315a8b7d46b1b752b3f574e41b86738c7c2b3` |
| 4 / C / backend | `94de72b95aa9691131492411d8087a9c48fda64b` | `98ac6c24cca1273efc5ac5f034368cef24496d70` |
| 5 / G / backend | `3884f3ac8c7c548b2ecf52b4ad95616878e8c320` | `1fcbf97a532f2a41dcda7173fa4d9666d3cd1235` |
| 6 / B / backend | `918a3f028523a566a57dbf67981c4ec5971fed8a` | `e3403f967758c51ee95011d77ac704fedaec3e0c` |
| 7 / A / backend | `d2151440852830a37cd4bd7311c2f203cbf48b37` | `6244c12ca80cea1e258e9428ac6c14db57fe022e` |
| 7 / A / backend | `a2816b97eb674cef36ee0965aceb71e2ac9ccafe` | `53e05d4912f2fb29252dc07924683e12c76053b3` |
| 7 / A / backend | `0ba8ff7657beb5cb2fb3c53c8e0fd35942c06323` | `184c0913c1d15d7d03b9875f8cf2eafd75a8c29f` |
| 8 / D / backend | `9ea42c1254c9dfe0e670129075e9d626d0175cfc` | `1e113869f3d6b8eb8d8c50b9b41b1b3c44139ef8` |
| 9 / H / backend | `9fe08d0e12087453a140637d994ee57f2fd97a68` | `deebb3308b6812ab4efc8aec875a1c0855724ad8` |
| 9 / H / backend | `6bf3477e1c550cdb6c0421fe26ac7a3cea179798` | `d87f92ddce194b9ba05230c81e953e2d1467fb1f` |
| 9 / H / web | `866f7a0e1a01f5e763a979e0c5ce16d8309c5f29` | `5bc67420b0ab3208460a1957dd7cea1bdb09a275` |
| 10 / A / backend | `30746cbc28f191e4a9dfc3085bbcac26c0fb6368` | `c022ce88f80ced8f1e081af18d90b342d273b8f1` |
| 11 / J / backend | `2897d1c7ab759810ddd52cc7abc50725752e15c1` | `439adaf38abcb90e9987e9947f3e71ea900a4e97` |
| 12 / A / backend | `81562b3008a4719e946932b24c56ad6e7803b8e1` | `32b2d7c01c99c7b43821c7d39bdcbda019d90182` |
| 12 / A / backend | `2534e9b1918bfd4dcf246f0e2d832e96d8535bad` | `290d3d190658635091b4a72e8a0a58a0c203886c` |
| 12 / A / backend | `a7b97ded220c42bfdd3d87c782861ba64db5e5df` | `ceff7542a97207ac6349b80f2409004fb44655e0` |
| 13 / A / backend | `ef948da9aa16c31cf48b49db953bca82bb8cf51a` | `8ffd1f85ea9ef79138c78ad1e711697305b28eb1` |
| 14 / E / backend | `fa369cd7ca9483c7f4b4565b77b559dbd63ec368` | `93fa203372e73ec3d4b72aac5aefade729428aaa` |
| 15 / I / backend | `cbaf86453bfbe80359c240e37dd49979a2dd5b2e` | `2a93ddf8a38eb2bfec42f6f2313e58377211ccf4` |
| 15 / I / backend | `8293a7eefef67ca1d49ecc59246bbcbdcc2820e9` | `8f8139468e49631cbab2d225e675113b3a9a8008` |
| 16 / A / backend | `be826e9fffc74d5b1214538a5e61c2bcc85f952c` | `009090ef79876eed243fb3e8d567263ecb63aef1` |
| 16 / A / backend | `0ed5ac0ddb239d4bce6526ee4cf335aecbe064b2` | `b2d2e8a38f5e412783cc5566717fdb533c0904f6` |
| 17 / F / web | `c3ce72e58766c5554fee742e1c740bc7dd09a60a` | `ab57f210a71305f3c0264f1f547ec5319de68f19` |
| 18 / D / backend | `92292fe88f705b67c3a9ec18ed9ee16e581d2bdd` | `792884b7e0caedfb899207f678b229f64d1be730` |
| 19 / E / backend | `04a82e98cba5699240ac175283df0a3d3d369aad` | `a6587cd4523b0c20fe968bb1a96fdc72920795a1` |
| 20 / I / backend | `c4ed7ed5550ac1138db2347e8ed26cf87a074111` | `778b765452a69dfe9f8114e49a682b2561e4c12b` |

集成人另提交 CI/分组登记 `e1ec905be97e96c5f08d8a84cbafea711f8f5321`、consumer pin
`f2e1e0596731b3a61be57ab093e0eb8bcd2247a5`；Web 文件体积基线按最终 Explore 从 1742 收紧到
1690 行，提交 `3655c7a3eb8f8f463b3927093018ce5fb531b413`。单次结论保存在本页，
长期合同只补入[稳定路径映射](../enterprise-identity-mcp/RAGFLOW_PORTING_MAP.md)。

## 实际检查与边界

| 层次 | 实际证据 |
|---|---|
| Python 两版本定向组合 | Python 3.12.9 / 3.13.14 各选 46 个文件、967 项；各 **965 passed、2 failed、零 skip**，原 exit=1 保留在 `integration/unit-final-{312,313}.xml` 及对应原始报告。两处失败见下段，不称全量通过。 |
| Python 静态 | `integration/reports/static-final`：改动 Python 文件 Ruff/format、mypy 148 文件、8 项 import 契约、async DB 和 frozen lock check 通过。 |
| 原生 HTTP/SQL/存储 | `integration/core-final/junit.xml`：**51 passed，零失败/skip**。实际 PostgreSQL 17.11、Redis、MinIO、Milvus 2.6.22 就绪，`REQUIRE_SERVICES=1`、禁止 Testcontainers 回退；覆盖 SDK/beta/REST 下载、beta Agent/Canvas/会话、布尔值、检索 top_k/租户范围及 GitHub 有限同步。模型节点/检索供应商边界受控。 |
| 最终 pin 的真实消费者 | `integration/consumer-final/junit.xml`：**7 passed，零失败/skip**；最终 Web 真实 TypeScript 下载客户端与实际浏览器 GitHub 表单，包含 JSON/零字节下载、明暗主题新建、显式 false 保存重载、旧缺失字段及 SQL 独立读回。共享基础服务继续运行。 |
| Go 组合 | 在候选 `0ed5ac0ddb239d4bce6526ee4cf335aecbe064b2` 上 build/vet、三个独立 cmd 构建通过。相关 7 包 race 有 2113 个测试/子测试通过事件，去重叶子 **1872**；实际 CLI/JWT/SQL/Infinity 有 7 个事件、6 条叶子验收链，35 次 CLI、43 次受控供应商请求；零失败/skip。原始 `A/reports/final-*-candidate.{json,log}` 绑定同一 Go 输入；532 项 Go/cmd/lock/catalog 源码与最终组合逐文件一致。Darwin arm64，供应商/播放器受控。 |
| Infinity 正式版 | I 线最终稳定锁文件在 3.12/3.13 各 **278 unit、31 real Infinity passed**；集成线同锁另跑 3.12 **31 passed**。实际 Linux arm64 `v0.7.0-0-544d5b876`，SDK/镜像同为 0.7.0；独立 SDK 读回 scratch DB 清零。详见 [Infinity stable](../references/infinity-stable.md)。 |
| Web 最终组合 | `170315a8` 的受影响回归 **41 passed**，完整类型/build、bundle/file-size 通过；完整源码 lint 0 errors、1369 warnings，后续 5 行 Explore guard 的 scoped lint 按 F 相同源字节复用。最终 `test:ci` exit=1：source-node **804 passed**，source-vitest **663 passed、1 failed**，随后 Desktop/tooling lane 未启动。失败见下段，不用较早输入的全绿结果代替。 |
| 浏览器/目标 SDK 复用 | F 的最终 Explore/Share/Widget/Markdown 多表面、附件两轮/切换/重载、迟到响应和下载 33 字节 SHA 读回复用 33 项相同源码/锁/配置 hash；外部 HTTP 与持久化为内存 fixture，不声明原生 DB/worker。G 的 Agent/Tool/Search 控件 12 场依未改源码和相同既有锁节点限定复用，已安装缓存 hash 不同，控制存储仍为 fixture。B 的实际目标 RAGFlow SDK 0.25.4 binary download 与跨 dataset 拒绝按未变服务源码复用。F/G/H 及最终消费者只审带标注 contact sheet；未逐张读取截图。 |

两个 Python 失败均为未修改的 `test_integration_harness.py` 守卫：既存
`test_app_list_web_consumers.py` / `test_shared_chunk_owner_contract.py` 未登记在 consumer 分组，
以及 `test_retrieval_parent_http.py` 两处跨 integration test 导入。不可变基线 Git blob 的同一 AST
谓词对照显示相同缺口、新增缺口为零，见 `integration/baseline-harness-delta.json`。
该对照不是一次基线 pytest 运行记录；本次没有运行基线 pytest，也没有修无关文件或放宽守卫。
本批新增 consumer/Infinity/core/Go opt-in 均已明确登记，GitHub 共享假件移到 `tests/support/`。

Web 失败是未改动 `mcp-query-ownership.test.tsx` 的旧通知时序断言；测试只等待一次 0ms timer。
原样定向复跑通过，较早的 `de1ccd67` 组合完整 1559 项也曾通过；包含最终 Explore guard 的
`170315a8` 完整运行仍失败，原 exit=1 与日志保留，不继续重复跑绿。最终受影响 41 项另有 JUnit。
没有把旧完整结果写成最终完整通过，也没有修改该无关测试或生产 hook。

Python unit/static/core 实际受验 SHA 为 `e1ec905b`，最终 consumer 为 `f2e1e059`；其后差异为 Go
或文档，所有 Python 文件、锁文件、共享配置和 Makefile 字节一致，见
`integration/Python-final-input-qualification.json`。Go/候选摘入、Web/F/G 复用、依赖增量和
截图审阅分别留有独立资格记录；重叠重跑不累加为更多覆盖。新环境和最终稳定 lock 的原生检查
均已实际执行，旧版本报告只作历史材料。

Go 首次扩大范围时发现两处旧能力清单与新增能力冲突，已精确更新正反向断言并重跑；另误带
四项缺少其独立环境的旧 live 测试，原失败保留。最终 unit 与显式 opt-in 按原独立环境边界区分，
本批 live 单独真实运行。I 冷启动预检、B 安全反例及各线早期 fixture/环境失败均留在原始报告，
没有吞异常、伪造空成功、跳过必需服务或削弱断言换绿。

## 清理与未验证项

先核恢复 bundle，再按精确清单移除本批候选/集成工作树和分支；共享后端 WIP、独立前端分支、
其他 worktree、业务资源与旧 prunable 记录保持。完整 bundle hash、实际主干/pin、资源及工作树
清理读回保存在本机 `delivery-complete.json` 和 `integration/`，原始证据目录保留。
SQL、对象、collection、Redis 与 listener 按各 fixture 的实际标识独立只读复核，不用宽泛清理。

本轮仅完成本地范围交付。两个 Python 守卫失败、Web 旧时序失败及远程 CI 待运行均单列保留；
当前固定 Web SHA 尚未推送，远程 consumer checkout 需在另获发布授权后进行。
生产升级/回退/切流、真实公网模型供应商、Linux amd64/Windows/native、实际音频设备和完整
core 四分片/全部专项未验证；共享 dirty 主 checkout 的 HEAD 不作为加载代码证明。
F 的既有暗色 Mermaid 对比度和范围外 MemoryMessageTable 软换行行为保持并记录。
